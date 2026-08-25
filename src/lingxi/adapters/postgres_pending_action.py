"""待确认操作（``pending_action``，迁移 ``0068``）的唯一 PostgreSQL 落点。

三个职责，与 ``core/admin/pending_action.py`` 的三个纯函数一一对应：

1. :meth:`PostgresPendingActionStore.prepare` —— 读目标当前状态、调用
   ``decide_prepare``，通过则插入一行 ``pending`` 记录（``card_delivered=FALSE``）。
   同一 ``target_open_id`` 已有在途 ``pending`` 行时，插入会撞上迁移 ``0068`` 的
   部分唯一索引，本方法捕获后转译为友好拒绝（外部审查交叉裁定，codex P1-5，ABA）。
2. :meth:`PostgresPendingActionStore.confirm`/:meth:`~.cancel` —— ``SELECT ... FOR
   UPDATE`` 锁定待确认操作与目标 ``app_user`` 行，调用对应纯函数取得决策，按决策
   执行 ``UPDATE``，同一事务内写审计；审计失败则整个事务回滚（写路径
   ``audit.record`` 必须与状态变更同事务，代码框架第三节；这句"同一事务"的准确
   边界见 :meth:`~.confirm` 文档）。``confirm()`` 对发起人当前角色的核对同样在
   这个事务、这个连接内完成（``SELECT ... FROM admin_registry ... FOR SHARE``），
   不再经由独立注入的登记表查询端口——见 :meth:`~.confirm` 文档"为什么角色核对
   要在同一事务内完成"。
3. :meth:`~.mark_card_delivered`/:meth:`~.mark_send_failed` —— 卡片发送结果的落库。
4. :meth:`~.next_card_sequence` —— CardKit 整卡级 sequence 记账，供
   ``core/admin/card_callback.py`` 在终态更新前换取本次调用要用的 sequence
   （迁移 ``0068`` 文件头部「为什么需要 card_sequence 记账」）。

真实并发下"最多成功一次"的机制（``SELECT ... FOR UPDATE`` 序列化并发确认，真正提供
保证的是行锁本身而不是条件更新的影响行数）见迁移 ``0068`` 文件头部。

**已知边界**：``pending_action.result`` 列本模块从未写入（``confirm()``/``cancel()``
只写 ``status``/``reason``/``decided_at``/``decided_by_open_id``）——它是为未来消费方
（本地抑制、单用户重同步、登记表变更等，迁移 ``0068`` 文件头部）预留的空列，当前
读到的 ``result`` 恒为 ``NULL``，不代表"这条待确认操作没有可描述的执行结果"。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from lingxi.adapters.admin_registry import admin_registry_entry_from_row
from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.admin.pending_action import (
    PENDING_ACTION_TTL_SECONDS,
    CancelDecision,
    ConfirmDecision,
    PendingAction,
    PendingActionAuditWriteFailed,
    PendingActionStatus,
    PendingActionType,
    PrepareDecision,
    decide_cancel,
    decide_confirm,
    decide_prepare,
)
from lingxi.core.ids import new_id

#: ``prepare()`` 撞上同目标在途唯一索引时对外的错误码与文案（外部审查交叉裁定，
#: codex P1-5）——与 ``core/admin/pending_action.ERROR_CODE``/``CANCEL_ERROR_CODE``
#: 同一风格的字面量表，但这条拒绝发生在插入阶段（数据库约束触发），不经过
#: ``decide_prepare`` 这个纯函数，因此不适合定义在那个模块的 ``ConfirmResultKind``/
#: ``CancelResultKind`` 枚举里——那两个枚举描述的是"读到一行之后怎么判"，这里是
#: "连一行都插不进去"。
TARGET_HAS_PENDING_ACTION_CODE = "target_has_pending_action"
TARGET_HAS_PENDING_ACTION_MESSAGE = "该用户当前已有一条待确认操作在途，请先处理（确认或取消）后再重新发起。"


class AuditSink(Protocol):
    """与 ``core/admin/router.AuditSink`` 结构相同——签名本身不接收连接对象，
    "审计与状态变更同事务"由 :meth:`~PostgresPendingActionStore.confirm`/
    :meth:`~.cancel` 在同一个 ``with connection.transaction()`` 块内调用它来实现：
    调用失败即异常向上传播，事务整体回滚（见两个方法的实现与
    :class:`PendingActionAuditWriteFailed`）。
    """

    def record(self, action: str, /, **fields: object) -> None: ...


@dataclass(frozen=True)
class PrepareOutcome:
    decision: PrepareDecision
    pending: PendingAction | None = None


@dataclass(frozen=True)
class ConfirmOutcome:
    decision: ConfirmDecision
    #: 仅当待确认操作 ID 从未存在过（含伪造）时为 ``None``——其余全部分支
    #: （含"未送达"）都有一个真实存在的行可以返回。
    pending: PendingAction | None = None


@dataclass(frozen=True)
class CancelOutcome:
    decision: CancelDecision
    pending: PendingAction | None = None


_SELECT_COLUMNS = (
    "id, action_type, target_open_id, target_state_snapshot, initiated_by_open_id,"
    " status, card_delivered, card_id, reason, created_at, confirm_deadline_at,"
    " decided_at, decided_by_open_id, card_sequence"
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
    )


class PostgresPendingActionStore:
    """``pending_action`` 表的唯一真实实现。

    **不再接受 ``registry`` 参数（外部审查交叉裁定，codex P1-4）**：``confirm()``
    对发起人角色的核对现在直接在自己的事务、自己的连接上查询 ``admin_registry``
    （见该方法文档），不再通过独立注入的登记表查询端口去读——那条独立查询走的是
    另一条连接，读到的角色状态不受本事务任何行锁保护，在"读到角色"与"提交这次
    确认"之间的窗口里，一次并发撤权可能悄悄溜过去而不被察觉（TOCTOU）。
    """

    def __init__(
        self,
        dsn: str,
        *,
        timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS,
        audit: AuditSink,
    ) -> None:
        self._dsn = dsn
        self._timeouts = timeouts
        self._audit = audit

    def get(self, *, pending_action_id: str) -> PendingAction | None:
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {_SELECT_COLUMNS} FROM pending_action WHERE id = %s", (pending_action_id,)
            )
            row = cursor.fetchone()
        return _row_to_pending_action(row) if row is not None else None

    def prepare(
        self,
        *,
        action_type: PendingActionType,
        target_open_id: str,
        initiated_by_open_id: str,
        now: datetime | None = None,
    ) -> PrepareOutcome:
        """读目标当前状态、判定是否可以发起，通过则插入一行 ``pending`` 记录
        （``card_delivered=FALSE``，调用方随后发卡片，成功再调用
        :meth:`mark_card_delivered`，失败调用 :meth:`mark_send_failed`）。
        """

        from psycopg.errors import UniqueViolation

        moment = now or datetime.now(timezone.utc)
        pending_id = new_id("pac")
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT account_state FROM app_user WHERE feishu_open_id = %s",
                        (target_open_id,),
                    )
                    row = cursor.fetchone()
                    current_state = row[0] if row is not None else None
                    decision = decide_prepare(
                        action_type=action_type, current_account_state=current_state
                    )
                    if not decision.ok:
                        return PrepareOutcome(decision=decision)

                    confirm_deadline_at = moment + timedelta(seconds=PENDING_ACTION_TTL_SECONDS)
                    # 嵌套事务块 = psycopg 的 SAVEPOINT：撞上迁移 0068 的同目标在途
                    # 唯一索引时，只回滚这条 INSERT 本身，外层事务（目前只做了一次
                    # 只读 SELECT）保持可继续提交的干净状态（外部审查交叉裁定，
                    # codex P1-5，ABA）。
                    try:
                        with connection.transaction():
                            cursor.execute(
                                """
                                INSERT INTO pending_action
                                    (id, action_type, target_open_id, target_state_snapshot,
                                     initiated_by_open_id, status, card_delivered, created_at,
                                     confirm_deadline_at)
                                VALUES (%s, %s, %s, %s, %s, 'pending', FALSE, %s, %s)
                                """,
                                (
                                    pending_id,
                                    action_type.value,
                                    target_open_id,
                                    current_state,
                                    initiated_by_open_id,
                                    moment,
                                    confirm_deadline_at,
                                ),
                            )
                    except UniqueViolation:
                        return PrepareOutcome(
                            decision=PrepareDecision(
                                ok=False,
                                code=TARGET_HAS_PENDING_ACTION_CODE,
                                message=TARGET_HAS_PENDING_ACTION_MESSAGE,
                            )
                        )
        pending = self.get(pending_action_id=pending_id)
        assert pending is not None  # 刚提交的行，同一连接的下一次读必然可见
        return PrepareOutcome(decision=decision, pending=pending)

    def mark_card_delivered(self, *, pending_action_id: str, card_id: str) -> None:
        """卡片确认送达（收到权威"平台已接收"回读）后置真。

        **同时把 ``card_sequence`` 基线抬到至少 2**（Issue #96 卡片回调应答修复，
        序号基线正式化）：编排者在 `biai-stage` 用受控探针实测——CardKit 建卡
        （``cardkit.v1.card.create``）与随后作为消息发出（``im.v1.message.reply``）
        各自消耗一次整卡级 sequence，序号 1、2 两次全量更新会被平台判定为
        "内容未变"而幂等吞掉（``code=0`` 但卡片视觉不刷新）；真正生效的更新必须
        携带 ``sequence>=3``。这条基线此前只在 stage 用临时数据库触发器验证，本次
        改动把探针验证过的事实正式落进代码——部署本修复后，stage 侧的临时触发器
        随之撤除。``next_card_sequence()`` 因此在卡片送达后第一次调用会返回 3，
        与真实 CardKit 的整卡计数器对齐。

        用 ``GREATEST`` 而不是覆盖式赋值 ``= 2``：``mark_card_delivered`` 结构上
        不会被同一条待确认操作调用第二次，但 ``GREATEST`` 让这条语句即使被重复
        调用也不会把已经因为其它路径前进过的 sequence 倒退回 2。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE pending_action SET card_delivered = TRUE, card_id = %s,"
                " card_sequence = GREATEST(card_sequence, 2)"
                " WHERE id = %s AND status = 'pending'",
                (card_id, pending_action_id),
            )

    def mark_send_failed(self, *, pending_action_id: str, now: datetime | None = None) -> None:
        """卡片确认发送失败（明确拒绝，不是结果不明）：立即把待确认操作转终态
        ``failed``，``card_delivered`` 保持 ``FALSE``——合同"卡片发送失败时本次
        操作不执行"。提前终态化是清洁收尾，不是正确性所必需的第二道防线
        （``card_delivered=FALSE`` 本身已经让 :func:`~lingxi.core.admin.
        pending_action.decide_confirm`/``decide_cancel`` 一律拒绝），只是避免这一行
        无限期停留在看似"仍在等待"的 ``pending``。
        """

        moment = now or datetime.now(timezone.utc)
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE pending_action SET status = 'failed', reason = 'card_send_failed',"
                " decided_at = %s WHERE id = %s AND status = 'pending'",
                (moment, pending_action_id),
            )

    def next_card_sequence(self, *, pending_action_id: str) -> int:
        """CardKit 整卡级 sequence 记账（外部审查交叉裁定，opus P2-1）：原子自增并
        取号，供 :mod:`lingxi.core.admin.card_callback` 在终态更新前换取本次调用要用
        的 sequence——见迁移 ``0068`` 文件头部「为什么需要 card_sequence 记账」。

        单条 ``UPDATE ... RETURNING`` 语句本身就是原子操作：并发的两次调用各自拿到
        不同的、严格递增的返回值，不需要额外加锁或包一层显式事务。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
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
        self, *, pending_action_id: str, clicker_open_id: str, now: datetime | None = None
    ) -> ConfirmOutcome:
        """确认卡片"确认执行"按钮的完整事务：详见 ``core/admin/pending_action.
        decide_confirm`` 的分支文档。审计写入失败时整个 ``with connection.
        transaction()`` 块回滚，包装为 :class:`PendingActionAuditWriteFailed` 向上
        抛出——``pending_action``、目标 ``app_user`` 均保持事务开始前的状态，调用方
        （``core/admin/card_callback.py``）不得将其视为任何一种终态。

        **"同一事务"这句话的准确边界（外部审查交叉裁定，opus P2-2 附带项）**：只在
        失败方向成立——``audit.record()`` 抛异常时，本方法尚未提交，整个数据库事务
        连同已经写下的任何 ``UPDATE`` 一起回滚。成功方向不是原子的：``audit.record()``
        当前的真实实现是结构化日志出口（``audit_event`` 表本身仍未建，见[数据库设计
        「六、管理与待确认操作」](../../../docs/技术设计/数据库设计.md#六管理与待确认操作)），
        一旦这次调用返回（不抛异常）就视为"审计已完成"，但日志行本身不参与、也不可能
        参与 PostgreSQL 的提交/回滚——如果 ``audit.record()`` 成功之后、本方法真正
        ``COMMIT`` 之前又发生了其它导致事务回滚的故障（连接中断、后续语句超时……），
        已经输出的日志行不会被撤回，而 ``pending_action``/``app_user`` 确实回滚了，
        产生一条"审计说发生了，数据库说没发生"的幽灵记录。这是全仓 ``AuditSink``
        抽象的既有性质（结构化日志不是可回滚的存储），不是本方法独有的缺口。

        **为什么角色核对要在同一事务内完成（外部审查交叉裁定，codex P1-4）**：发起人
        当前角色是否仍然有效，判定用的是 :func:`~lingxi.core.admin.pending_action.
        decide_confirm` 需要的 ``registry_entry``——这条读取如果走一条独立的、不受本
        事务任何锁保护的连接，"读到角色"与"提交这次确认"之间就会存在一个 TOCTOU
        窗口：并发的一次撤权可以在这个窗口内悄悄发生而不被本次确认看见，本次确认仍然
        会用撤权前的旧角色执行。下面对 ``admin_registry`` 的查询改为在**同一个连接、
        同一个事务**里，并对目标行取 ``FOR SHARE``——这不要求本方法自己去修改
        ``admin_registry``（``FOR SHARE`` 是共享锁，允许其它事务同时也持共享锁读），
        但会与任何试图 ``UPDATE`` 该行（撤权）的并发事务互斥：撤权事务若已经拿到该行
        的排它锁但还没提交，本方法的 ``FOR SHARE`` 会等它提交或回滚，读到的必然是
        "撤权前"或"撤权后"两个确定状态之一，不会出现两者之间的中间态。
        """

        pending: PendingAction | None = None
        decision: ConfirmDecision
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"SELECT {_SELECT_COLUMNS} FROM pending_action WHERE id = %s FOR UPDATE",
                        (pending_action_id,),
                    )
                    row = cursor.fetchone()
                    pending = _row_to_pending_action(row) if row is not None else None

                    registry_entry = None
                    if pending is not None:
                        cursor.execute(
                            """
                            SELECT feishu_open_id, label, permission_admin_granted,
                                   ops_admin_granted, super_admin_granted, entry_status
                              FROM admin_registry
                             WHERE feishu_open_id = %s AND entry_status = 'active'
                             FOR SHARE
                            """,
                            (pending.initiated_by_open_id,),
                        )
                        registry_row = cursor.fetchone()
                        registry_entry = (
                            admin_registry_entry_from_row(registry_row)
                            if registry_row is not None
                            else None
                        )

                    current_account_state: str | None = None
                    if pending is not None:
                        cursor.execute(
                            "SELECT account_state FROM app_user WHERE feishu_open_id = %s"
                            " FOR UPDATE",
                            (pending.target_open_id,),
                        )
                        target_row = cursor.fetchone()
                        current_account_state = target_row[0] if target_row is not None else None

                    # 两把行锁（待确认操作、目标账号）与 admin_registry 的 FOR SHARE
                    # 都已经拿到——现在才取时钟（外部审查交叉裁定，codex P1-3）：等锁
                    # 期间如果被并发事务卡住，锁前取的时间会比真正执行判定的时刻更早，
                    # 让本该过期的操作因为一段等锁窗口而被误判为"仍然有效"。显式注入的
                    # ``now`` 原样使用，不重新取时钟（测试需要确定性时钟）。
                    moment = now if now is not None else datetime.now(timezone.utc)

                    decision = decide_confirm(
                        pending=pending,
                        clicker_open_id=clicker_open_id,
                        now=moment,
                        registry_entry=registry_entry,
                        current_account_state=current_account_state,
                    )

                    if pending is not None and decision.terminal_status is not None:
                        if decision.ok:
                            assert decision.new_account_state is not None
                            cursor.execute(
                                "UPDATE app_user SET account_state = %s, updated_at = now()"
                                " WHERE feishu_open_id = %s AND account_state = %s",
                                (
                                    decision.new_account_state,
                                    pending.target_open_id,
                                    current_account_state,
                                ),
                            )
                            if cursor.rowcount != 1:
                                # SELECT ... FOR UPDATE 已经把目标行锁到本事务提交为止，
                                # 这里理论上不可能发生；响亮失败好过静默当成"改成功了"。
                                raise RuntimeError(
                                    "并发修改目标账号状态，预期影响 1 行，"
                                    f"实际 {cursor.rowcount} 行"
                                )

                        try:
                            self._audit.record(
                                "admin.pending_action.confirmed",
                                pending_action_id=pending.id,
                                action_type=pending.action_type.value,
                                outcome=decision.kind.value,
                                initiated_by=pending.initiated_by_open_id,
                                clicker=clicker_open_id,
                            )
                        except Exception as error:  # noqa: BLE001 - 见类文档，失败即整体回滚
                            raise PendingActionAuditWriteFailed(
                                "确认操作的审计写入失败，事务已回滚，操作未执行"
                            ) from error

                        cursor.execute(
                            "UPDATE pending_action SET status = %s, reason = %s,"
                            " decided_at = %s, decided_by_open_id = %s"
                            " WHERE id = %s AND status = 'pending'",
                            (
                                decision.terminal_status.value,
                                decision.reason,
                                moment,
                                clicker_open_id,
                                pending.id,
                            ),
                        )
                        if cursor.rowcount != 1:
                            # 锁失效场景下的纵深防线（外部审查交叉裁定，opus P3-2）：
                            # 本方法从取得行锁到这里全程持有它，正常情况下不可能有
                            # 别的事务在期间把状态改离 'pending'；响亮失败好过静默
                            # 当成"改成功了"（与上面 app_user 的 rowcount 校验同一
                            # 姿态）。
                            raise RuntimeError(
                                "待确认操作状态在本事务持锁期间被改变，预期影响 1 行，"
                                f"实际 {cursor.rowcount} 行"
                            )

        if pending is None:
            return ConfirmOutcome(decision=decision, pending=None)
        refreshed = self.get(pending_action_id=pending_action_id)
        assert refreshed is not None
        return ConfirmOutcome(decision=decision, pending=refreshed)

    def cancel(
        self, *, pending_action_id: str, clicker_open_id: str, now: datetime | None = None
    ) -> CancelOutcome:
        """"取消"按钮的事务：核对链更短（见 ``decide_cancel``），同样在同一事务内
        先写审计、失败即整体回滚。"审计与时钟"两条注意事项与 :meth:`~.confirm`
        文档同一姿态（结构化日志出口的"同一事务"只在失败方向成立；时钟在拿到行锁
        之后才取，外部审查交叉裁定，codex P1-3）。
        """

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

                    moment = now if now is not None else datetime.now(timezone.utc)

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
                        except Exception as error:  # noqa: BLE001 - 见 confirm() 同一姿态
                            raise PendingActionAuditWriteFailed(
                                "取消操作的审计写入失败，事务已回滚，操作未执行"
                            ) from error

                        cursor.execute(
                            "UPDATE pending_action SET status = %s, reason = %s,"
                            " decided_at = %s, decided_by_open_id = %s"
                            " WHERE id = %s AND status = 'pending'",
                            (
                                decision.terminal_status.value,
                                decision.reason,
                                moment,
                                clicker_open_id,
                                pending.id,
                            ),
                        )
                        if cursor.rowcount != 1:
                            # 锁失效场景下的纵深防线，与 confirm() 同一姿态（外部审查
                            # 交叉裁定，opus P3-2）。
                            raise RuntimeError(
                                "待确认操作状态在本事务持锁期间被改变，预期影响 1 行，"
                                f"实际 {cursor.rowcount} 行"
                            )

        if pending is None:
            return CancelOutcome(decision=decision, pending=None)
        refreshed = self.get(pending_action_id=pending_action_id)
        assert refreshed is not None
        return CancelOutcome(decision=decision, pending=refreshed)
