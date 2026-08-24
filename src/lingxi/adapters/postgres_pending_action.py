"""待确认操作（``pending_action``，迁移 ``0068``）的唯一 PostgreSQL 落点。

三个职责，与 ``core/admin/pending_action.py`` 的三个纯函数一一对应：

1. :meth:`PostgresPendingActionStore.prepare` —— 读目标当前状态、调用
   ``decide_prepare``，通过则插入一行 ``pending`` 记录（``card_delivered=FALSE``）。
2. :meth:`PostgresPendingActionStore.confirm`/:meth:`~.cancel` —— ``SELECT ... FOR
   UPDATE`` 锁定待确认操作与目标 ``app_user`` 行，调用对应纯函数取得决策，按决策
   执行 ``UPDATE``，同一事务内写审计；审计失败则整个事务回滚（写路径
   ``audit.record`` 必须与状态变更同事务，代码框架第三节）。
3. :meth:`~.mark_card_delivered`/:meth:`~.mark_send_failed` —— 卡片发送结果的落库。

真实并发下"最多成功一次"的机制（``SELECT ... FOR UPDATE`` 序列化并发确认）见迁移
``0068`` 文件头部。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

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
from lingxi.core.admin.registry import AdminRegistryEntry
from lingxi.core.ids import new_id


class AdminRegistryLookup(Protocol):
    """与 ``core/admin/router.AdminRegistryLookup`` 结构相同的独立 Protocol——
    :meth:`~PostgresPendingActionStore.confirm` 需要在确认时刻重新读一次登记表
    （合同"确认时重新读取……管理 MCP 当前角色"）。本模块刻意不 import
    ``core/admin/router.py``，避免两个只是"判定 + 执行"关系的模块产生循环依赖，
    与 ``router.py`` 自己不 import ``core/conversation`` 的既有取舍同一姿态。
    """

    def active_entry(self, *, open_id: str) -> AdminRegistryEntry | None: ...


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
    " status, card_delivered, card_id, reason, created_at, expires_at, decided_at,"
    " decided_by_open_id"
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
        expires_at,
        decided_at,
        decided_by_open_id,
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
        expires_at=expires_at,
        decided_at=decided_at,
        decided_by_open_id=decided_by_open_id,
    )


class PostgresPendingActionStore:
    """``pending_action`` 表的唯一真实实现。"""

    def __init__(
        self,
        dsn: str,
        *,
        timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS,
        registry: AdminRegistryLookup,
        audit: AuditSink,
    ) -> None:
        self._dsn = dsn
        self._timeouts = timeouts
        self._registry = registry
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

                    expires_at = moment + timedelta(seconds=PENDING_ACTION_TTL_SECONDS)
                    cursor.execute(
                        """
                        INSERT INTO pending_action
                            (id, action_type, target_open_id, target_state_snapshot,
                             initiated_by_open_id, status, card_delivered, created_at,
                             expires_at)
                        VALUES (%s, %s, %s, %s, %s, 'pending', FALSE, %s, %s)
                        """,
                        (
                            pending_id,
                            action_type.value,
                            target_open_id,
                            current_state,
                            initiated_by_open_id,
                            moment,
                            expires_at,
                        ),
                    )
        pending = self.get(pending_action_id=pending_id)
        assert pending is not None  # 刚提交的行，同一连接的下一次读必然可见
        return PrepareOutcome(decision=decision, pending=pending)

    def mark_card_delivered(self, *, pending_action_id: str, card_id: str) -> None:
        """卡片确认送达（收到权威"平台已接收"回读）后置真。"""

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE pending_action SET card_delivered = TRUE, card_id = %s"
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

    def confirm(
        self, *, pending_action_id: str, clicker_open_id: str, now: datetime | None = None
    ) -> ConfirmOutcome:
        """确认卡片"确认执行"按钮的完整事务：详见 ``core/admin/pending_action.
        decide_confirm`` 的分支文档。审计写入失败时整个 ``with connection.
        transaction()`` 块回滚，包装为 :class:`PendingActionAuditWriteFailed` 向上
        抛出——``pending_action``、目标 ``app_user`` 均保持事务开始前的状态，调用方
        （``core/admin/card_callback.py``）不得将其视为任何一种终态。
        """

        moment = now or datetime.now(timezone.utc)
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

                    registry_entry = (
                        self._registry.active_entry(open_id=pending.initiated_by_open_id)
                        if pending is not None
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
                            " decided_at = %s, decided_by_open_id = %s WHERE id = %s",
                            (
                                decision.terminal_status.value,
                                decision.reason,
                                moment,
                                clicker_open_id,
                                pending.id,
                            ),
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
        先写审计、失败即整体回滚。"""

        moment = now or datetime.now(timezone.utc)
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
                            " decided_at = %s, decided_by_open_id = %s WHERE id = %s",
                            (
                                decision.terminal_status.value,
                                decision.reason,
                                moment,
                                clicker_open_id,
                                pending.id,
                            ),
                        )

        if pending is None:
            return CancelOutcome(decision=decision, pending=None)
        refreshed = self.get(pending_action_id=pending_action_id)
        assert refreshed is not None
        return CancelOutcome(decision=decision, pending=refreshed)
