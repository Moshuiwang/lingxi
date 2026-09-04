"""待确认操作（``pending_action``，迁移 ``0068``）的唯一 PostgreSQL 落点。

三个职责，与 ``core/admin/pending_action.py`` 的三个纯函数一一对应：
:meth:`~.prepare`（判定并插入 ``pending`` 行）、:meth:`~.confirm`/:meth:`~.cancel`
（锁定后调用对应纯函数取得决策，同一事务内写审计与状态变更）、
:meth:`~.mark_card_delivered`/:meth:`~.mark_send_failed`/:meth:`~.next_card_sequence`
（卡片发送结果落库、sequence 记账）。真实并发下"最多成功一次"靠行锁本身而不是
条件更新的影响行数，见迁移 ``0068`` 文件头部；本地权限三类动作复用与
``suspend_user``/``resume_user`` 相同的骨架，差异见
:mod:`~lingxi.adapters.postgres_pending_action_execution`（``_ExecutionMixin``，
本模块的 :class:`PostgresPendingActionStore` 组合它，收纳不自己开连接的纯
计算/写入辅助方法）；自己开连接的方法留在这里——测试用
``mock.patch("...connect", ...)`` 换假连接，只有 ``connect`` 留在本模块命名
空间才生效。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from lingxi.adapters.postgres import (
    DEFAULT_POSTGRES_TIMEOUTS,
    PostgresTimeouts,
    connect,
)
from lingxi.adapters.postgres_pending_action_execution import (
    PrepareOutcome,
    _ExecutionMixin,
    _PendingInsertFields,
)
from lingxi.core.admin.pending_action import (
    CancelDecision,
    ConfirmDecision,
    PendingAction,
    PendingActionAuditWriteFailed,
    PendingActionStatus,
    PendingActionTransientFailure,
    PendingActionType,
    PrepareDecision,
    decide_cancel,
    decide_confirm,
    decide_prepare,
)
from lingxi.core.ids import new_id
from lingxi.core.permission.position_override import PositionPermissionExpansion

#: ``prepare()`` 撞上同目标在途唯一索引时对外的错误码：这条拒绝发生在插入
#: 阶段（数据库约束触发），不经过 ``decide_prepare`` 这个纯函数，因此不适合
#: 定义在那个模块的 ``ConfirmResultKind``/``CancelResultKind`` 枚举里——那两个
#: 枚举描述"读到一行之后怎么判"，这里是"连一行都插不进去"。
TARGET_HAS_PENDING_ACTION_CODE = "target_has_pending_action"

#: 兜底文案，只在结构上不应该发生的分支使用（撞了唯一索引、但紧接着的
#: SELECT 却查不到那一条 'pending' 行——理论上不可能）；正常路径下的拦截
#: 文案由 ``core/admin/pending_action.format_in_flight_conflict_message``
#: 按查到的在途行动态生成，带摘要与自助指引。
TARGET_HAS_PENDING_ACTION_MESSAGE = (
    "该用户当前已有一条待确认操作在途，请先处理（确认或取消）后再重新发起。"
)

_UPDATE_TERMINAL_STATUS_SQL = (
    "UPDATE pending_action SET status = %s, reason = %s,"
    " decided_at = %s, decided_by_open_id = %s"
    " WHERE id = %s AND status = 'pending'"
)


class AuditSink(Protocol):
    """与 ``core/admin/router.AuditSink`` 结构相同。

    签名本身不接收连接对象，"审计与状态变更同事务"由
    :meth:`~PostgresPendingActionStore.confirm`/:meth:`~.cancel` 在同一个
    ``with connection.transaction()`` 块内调用它来实现：调用失败即异常向上
    传播，事务整体回滚（见两个方法的实现与 :class:`PendingActionAuditWriteFailed`）。
    """

    def record(self, action: str, /, **fields: object) -> None:
        """写一条结构化审计日志；失败（抛异常）即让调用方的事务整体回滚。"""
        ...


@dataclass(frozen=True)
class ConfirmOutcome:
    """:meth:`~PostgresPendingActionStore.confirm` 的返回形状。"""

    decision: ConfirmDecision
    #: 仅当待确认操作 ID 从未存在过（含伪造）时为 ``None``——其余全部分支
    #: （含"未送达"）都有一个真实存在的行可以返回。
    pending: PendingAction | None = None


@dataclass(frozen=True)
class CancelOutcome:
    """:meth:`~PostgresPendingActionStore.cancel` 的返回形状。"""

    decision: CancelDecision
    pending: PendingAction | None = None


_SELECT_COLUMNS = (
    "id, action_type, target_open_id, target_state_snapshot, initiated_by_open_id,"
    " status, card_delivered, card_id, reason, created_at, confirm_deadline_at,"
    " decided_at, decided_by_open_id, card_sequence, payload, origin_card_message_id"
)


def _row_to_pending_action(row: tuple) -> PendingAction:
    (
        id_,
        action_type,
        target_open_id,
        target_state_snapshot,
        initiated_by_open_id,
        status,
        card_delivered,
        card_id,
        reason,
        created_at,
        confirm_deadline_at,
        decided_at,
        decided_by_open_id,
        card_sequence,
        payload,
        origin_card_message_id,
    ) = row
    return PendingAction(
        id=id_,
        action_type=PendingActionType(action_type),
        target_open_id=target_open_id,
        target_state_snapshot=target_state_snapshot,
        initiated_by_open_id=initiated_by_open_id,
        status=PendingActionStatus(status),
        card_delivered=card_delivered,
        card_id=card_id,
        reason=reason,
        created_at=created_at,
        confirm_deadline_at=confirm_deadline_at,
        decided_at=decided_at,
        decided_by_open_id=decided_by_open_id,
        card_sequence=card_sequence,
        payload=payload,
        origin_card_message_id=origin_card_message_id,
    )


class PostgresPendingActionStore(_ExecutionMixin):
    """``pending_action`` 表的唯一真实实现。

    **不接受 ``registry`` 参数**：``confirm()`` 对发起人角色的核对直接在
    自己的事务、自己的连接上查询 ``admin_registry``（见该方法文档），不经过
    独立注入的登记表查询端口——那条独立查询会走另一条连接，读到的角色状态
    不受本事务任何行锁保护，在"读到角色"与"提交这次确认"之间的窗口里，一次
    并发撤权可能悄悄溜过去而不被察觉（TOCTOU）。
    """

    def __init__(
        self,
        dsn: str,
        *,
        timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS,
        audit: AuditSink,
        metric_map_path: Path | None,
    ) -> None:
        """记下连接参数、审计出口与指标映射外置路径。

        ``metric_map_path``：「公司+职能→指标名」映射的外置路径，由装配层
        从环境变量读出后注入；``None`` 表示这台机器没配外置文件，落回随包
        默认。**刻意没有默认值**：:meth:`prepare` 的职位+公司范围展开会把这份映射
        直接变成一批"公司×指标"授权对写进 ``pending_action.payload``——管理员
        确认后就是真实权限，构造参数没有默认值，新调用点就不可能"忘了传"而
        悄悄退回随包默认（此前无条件读随包默认、与 scheduler 日批读外置文件
        分叉过一次，同一个职位在两条路径上给出不同指标集合）。
        """
        self._dsn = dsn
        self._timeouts = timeouts
        self._audit = audit
        self._metric_map_path = metric_map_path

    def get(self, *, pending_action_id: str) -> PendingAction | None:
        """按 ID 回读一条待确认操作；查无返回 ``None``。"""
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                f"SELECT {_SELECT_COLUMNS} FROM pending_action WHERE id = %s",
                (pending_action_id,),
            )
            row = cursor.fetchone()
        return _row_to_pending_action(row) if row is not None else None

    def prepare(
        self,
        *,
        action_type: PendingActionType,
        target_open_id: str,
        initiated_by_open_id: str,
        company_id: str | None = None,
        metric_name: str | None = None,
        reason: str | None = None,
        now: datetime | None = None,
        trace_id: str | None = None,
        position_name: str | None = None,
        company_scope: str | None = None,
        origin_card_message_id: str | None = None,
    ) -> PrepareOutcome:
        """读目标当前状态、判定是否可以发起，通过则插入一行 ``pending`` 记录。

        ``card_delivered=FALSE``，调用方随后发卡片，成功再调用
        :meth:`mark_card_delivered`，失败调用 :meth:`mark_send_failed`。
        ``company_id``/``metric_name``/``reason`` 只有本地权限三类动作（授权/
        抑制/收回）才会被使用，``position_name``/``company_scope`` 只有授权
        动作支持，具体判定与写入见 :meth:`_prepare_locked` 与
        ``_resolve_prepare_state``。
        """
        moment = now or datetime.now(UTC)
        pending_id = new_id("pac")
        inputs = self._prepare_inputs(
            action_type=action_type,
            target_open_id=target_open_id,
            initiated_by_open_id=initiated_by_open_id,
            position_name=position_name,
            company_scope=company_scope,
            company_id=company_id,
            metric_name=metric_name,
            reason=reason,
        )
        if isinstance(inputs, PrepareOutcome):
            return inputs

        result = self._prepare_locked(
            inputs,
            action_type=action_type,
            target_open_id=target_open_id,
            initiated_by_open_id=initiated_by_open_id,
            reason=reason,
            company_id=company_id,
            metric_name=metric_name,
            pending_id=pending_id,
            moment=moment,
            trace_id=trace_id,
            origin_card_message_id=origin_card_message_id,
        )
        if isinstance(result, PrepareOutcome):
            return result
        pending = self.get(pending_action_id=pending_id)
        assert pending is not None  # 刚提交的行，同一连接的下一次读必然可见
        return PrepareOutcome(decision=result, pending=pending)

    def _prepare_locked(
        self,
        inputs: tuple[bool, bool, bool, PositionPermissionExpansion | None, str | None],
        *,
        action_type: PendingActionType,
        target_open_id: str,
        initiated_by_open_id: str,
        reason: str | None,
        company_id: str | None,
        metric_name: str | None,
        pending_id: str,
        moment: datetime,
        trace_id: str | None,
        origin_card_message_id: str | None,
    ) -> PrepareOutcome | PrepareDecision:
        """独立开一条连接/事务，执行 :meth:`prepare` 的判定与写入。

        ``inputs`` 是 ``_prepare_inputs`` 的返回值（住在
        :mod:`~lingxi.adapters.postgres_pending_action_execution`）。收回的
        落库目标是被收回那一行的真实属主 open_id，不是本方法收到的
        ``target_open_id`` 形参（那里装的是 override_id），见
        ``_resolve_prepare_state`` 文档。
        """
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    resolution = self._resolve_prepare_state(
                        cursor,
                        inputs,
                        action_type=action_type,
                        target_open_id=target_open_id,
                        initiated_by_open_id=initiated_by_open_id,
                        reason=reason,
                        company_id=company_id,
                        metric_name=metric_name,
                        resolved_target_open_id=target_open_id,
                    )
                    if isinstance(resolution, PrepareOutcome):
                        return resolution
                    current_state, payload, resolved_target_open_id = resolution

                    decision = decide_prepare(
                        action_type=action_type, current_account_state=current_state
                    )
                    if not decision.ok:
                        return PrepareOutcome(decision=decision)

                    fields = _PendingInsertFields(
                        pending_id=pending_id,
                        action_type=action_type,
                        resolved_target_open_id=resolved_target_open_id,
                        current_state=current_state,
                        initiated_by_open_id=initiated_by_open_id,
                        moment=moment,
                        payload=payload,
                        origin_card_message_id=origin_card_message_id,
                    )
                    return self._finalize_prepare_insert(
                        connection, cursor, fields, trace_id=trace_id, decision=decision
                    )

    def mark_card_delivered(self, *, pending_action_id: str, card_id: str) -> None:
        """卡片确认送达（收到权威"平台已接收"回读）后置真。

        **同时把 ``card_sequence`` 基线抬到至少 2**：CardKit 建卡与随后作为
        消息发出各自消耗一次整卡级 sequence，序号 1、2 两次全量更新会被平台
        判定为"内容未变"而幂等吞掉（卡片视觉不刷新）；真正生效的更新必须
        携带 ``sequence>=3``，``next_card_sequence()`` 因此在卡片送达后第一次
        调用会返回 3，与真实 CardKit 的整卡计数器对齐。用 ``GREATEST`` 而不是
        覆盖式赋值：即使被重复调用也不会把已经前进过的 sequence 倒退回 2。
        """
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "UPDATE pending_action SET card_delivered = TRUE, card_id = %s,"
                " card_sequence = GREATEST(card_sequence, 2)"
                " WHERE id = %s AND status = 'pending'",
                (card_id, pending_action_id),
            )

    def mark_send_failed(self, *, pending_action_id: str, now: datetime | None = None) -> None:
        """卡片确认发送失败（明确拒绝，不是结果不明）：立即转终态 ``failed``。

        ``card_delivered`` 保持 ``FALSE``——合同"卡片发送失败时本次操作不
        执行"。提前终态化是清洁收尾，不是正确性所必需的第二道防线
        （``card_delivered=FALSE`` 本身已经让 ``decide_confirm``/``decide_cancel``
        一律拒绝），只是避免这一行无限期停留在看似"仍在等待"的 ``pending``。
        """
        moment = now or datetime.now(UTC)
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "UPDATE pending_action SET status = 'failed', reason = 'card_send_failed',"
                " decided_at = %s WHERE id = %s AND status = 'pending'",
                (moment, pending_action_id),
            )

    def next_card_sequence(self, *, pending_action_id: str) -> int:
        """CardKit 整卡级 sequence 记账：原子自增并取号。

        供 ``core/admin/card_callback.py`` 在终态更新前换取本次调用要用的
        sequence。单条 ``UPDATE ... RETURNING`` 语句本身就是原子操作：并发
        的两次调用各自拿到不同的、严格递增的返回值，不需要额外加锁或包一层
        显式事务。
        """
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "UPDATE pending_action SET card_sequence = card_sequence + 1"
                " WHERE id = %s RETURNING card_sequence",
                (pending_action_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise LookupError(f"待确认操作不存在，无法分配 card sequence：{pending_action_id}")
        return row[0]

    def confirm(
        self,
        *,
        pending_action_id: str,
        clicker_open_id: str,
        now: datetime | None = None,
    ) -> ConfirmOutcome:
        """确认卡片"确认执行"按钮的完整事务，详见 ``decide_confirm`` 分支文档。

        审计写入失败时整个事务回滚，包装为 :class:`PendingActionAuditWriteFailed`
        向上抛出。**"同一事务"只在失败方向成立**：``audit.record()`` 是结构化
        日志出口，不参与 PostgreSQL 的提交/回滚（全仓 ``AuditSink`` 抽象的
        既有性质）。角色核对同样在这个事务、这个连接内完成（见
        :meth:`_lock_admin_registry_entry`），消除 TOCTOU 窗口；数据库瞬时
        故障转译为 ``PendingActionTransientFailure``；执行体是
        :meth:`_confirm_locked`。
        """
        from psycopg.errors import OperationalError

        try:
            pending, decision = self._confirm_locked(
                pending_action_id=pending_action_id,
                clicker_open_id=clicker_open_id,
                now=now,
            )
        except OperationalError as error:
            raise PendingActionTransientFailure(type(error).__name__) from error

        if pending is None:
            return ConfirmOutcome(decision=decision, pending=None)
        refreshed = self.get(pending_action_id=pending_action_id)
        assert refreshed is not None
        return ConfirmOutcome(decision=decision, pending=refreshed)

    def _confirm_locked(
        self, *, pending_action_id: str, clicker_open_id: str, now: datetime | None
    ) -> tuple[PendingAction | None, ConfirmDecision]:
        """:meth:`confirm` 的事务体本身。

        拆成独立方法只是为了让 :meth:`confirm` 能在外层包一层不影响这里任何
        缩进的 ``try/except OperationalError``。不单独对外暴露，不是任何
        协议的一部分。
        """
        pending: PendingAction | None = None
        decision: ConfirmDecision
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    pending = self._lock_pending_action_row(cursor, pending_action_id)
                    registry_entry = (
                        self._lock_admin_registry_entry(cursor, pending)
                        if pending is not None
                        else None
                    )
                    target_user_id, current_account_state = (
                        self._lock_target_account(cursor, pending)
                        if pending is not None
                        else (None, None)
                    )
                    # 两把行锁（待确认操作、目标账号）与 admin_registry 的
                    # FOR SHARE 都已经拿到——现在才取时钟：等锁期间如果被并发
                    # 事务卡住，锁前取的时间会比真正执行判定的时刻更早，让
                    # 本该过期的操作因为一段等锁窗口而被误判为"仍然有效"。
                    moment = now if now is not None else datetime.now(UTC)

                    decision = decide_confirm(
                        pending=pending,
                        clicker_open_id=clicker_open_id,
                        now=moment,
                        registry_entry=registry_entry,
                        current_account_state=current_account_state,
                    )

                    if pending is not None and decision.terminal_status is not None:
                        if decision.ok:
                            decision = self._execute_confirm_decision(
                                cursor,
                                connection,
                                pending=pending,
                                decision=decision,
                                current_account_state=current_account_state,
                                target_user_id=target_user_id,
                                moment=moment,
                            )
                        self._finalize_confirm_decision(
                            cursor,
                            pending=pending,
                            decision=decision,
                            clicker_open_id=clicker_open_id,
                            moment=moment,
                        )

        return pending, decision

    def cancel(
        self,
        *,
        pending_action_id: str,
        clicker_open_id: str,
        now: datetime | None = None,
    ) -> CancelOutcome:
        """取消按钮的事务：核对链更短，同样在同一事务内先写审计、失败即整体回滚。

        见 ``decide_cancel``。"审计与时钟"两条注意事项与 :meth:`~.confirm`
        文档同一姿态（结构化日志出口的"同一事务"只在失败方向成立；时钟在
        拿到行锁之后才取）。数据库瞬时故障的捕获与转译同 :meth:`~.confirm`：
        本方法风险面更小（不涉及清送达正文那组容易撞锁序的写入），但
        ``pending_action``/``app_user`` 的 ``FOR UPDATE`` 本身仍可能撞上并发
        持锁超过 ``lock_timeout``，同一姿态兜底，不留一个不对称的缺口。
        """
        from psycopg.errors import OperationalError

        try:
            pending, decision = self._cancel_locked(
                pending_action_id=pending_action_id,
                clicker_open_id=clicker_open_id,
                now=now,
            )
        except OperationalError as error:
            raise PendingActionTransientFailure(type(error).__name__) from error

        if pending is None:
            return CancelOutcome(decision=decision, pending=None)
        refreshed = self.get(pending_action_id=pending_action_id)
        assert refreshed is not None
        return CancelOutcome(decision=decision, pending=refreshed)

    def _cancel_locked(
        self, *, pending_action_id: str, clicker_open_id: str, now: datetime | None
    ) -> tuple[PendingAction | None, CancelDecision]:
        """:meth:`cancel` 的事务体本身，拆分理由与 :meth:`_confirm_locked` 相同。"""
        pending: PendingAction | None = None
        decision: CancelDecision
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"SELECT {_SELECT_COLUMNS} FROM pending_action WHERE id = %s FOR UPDATE",
                        (pending_action_id,),
                    )
                    row = cursor.fetchone()
                    pending = _row_to_pending_action(row) if row is not None else None

                    moment = now if now is not None else datetime.now(UTC)

                    decision = decide_cancel(
                        pending=pending, clicker_open_id=clicker_open_id, now=moment
                    )

                    if pending is not None and decision.terminal_status is not None:
                        try:
                            self._audit.record(
                                "admin.pending_action.cancelled",
                                pending_action_id=pending.id,
                                action_type=pending.action_type.value,
                                outcome=decision.kind.value,
                                initiated_by=pending.initiated_by_open_id,
                                clicker=clicker_open_id,
                            )
                        except Exception as error:  # 见 confirm() 同一姿态
                            raise PendingActionAuditWriteFailed(
                                "取消操作的审计写入失败，事务已回滚，操作未执行"
                            ) from error

                        cursor.execute(
                            _UPDATE_TERMINAL_STATUS_SQL,
                            (
                                decision.terminal_status.value,
                                decision.reason,
                                moment,
                                clicker_open_id,
                                pending.id,
                            ),
                        )
                        if cursor.rowcount != 1:
                            # 锁失效场景下的纵深防线，与 confirm() 同一姿态。
                            raise RuntimeError(
                                "待确认操作状态在本事务持锁期间被改变，预期影响 1 行，"
                                f"实际 {cursor.rowcount} 行"
                            )

        return pending, decision
