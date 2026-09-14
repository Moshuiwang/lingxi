"""不依赖触发消息的确认卡发送口：登记持久发卡阶段，由 gateway 用既有飞书凭据直发本人。

受限通道跑在 scheduler 里，手上没有飞书会话、话题或消息标识，也不持飞书发送凭据。
这里的「发送」只做两件同事务的事：登记 ``confirmation_card_send`` 阶段、写运营审计账的
``prepared`` 行——登记失败即把待确认操作转为发送失败，本次操作不会执行。真正的卡片由
gateway 的持久阶段消费者按待确认动作类型分派发出，卡片送达才置 ``card_delivered``。
"""

from __future__ import annotations

from dataclasses import dataclass

from lingxi.adapters.postgres_admin_followup import enqueue_followups
from lingxi.adapters.postgres_innertest import admin_roles_snapshot
from lingxi.adapters.postgres_operation_audit import record_operation_audit
from lingxi.core.admin.followup import FollowupSpec
from lingxi.core.admin.operation_audit import EntryPoint, OperationAuditEntry, OperationPhase
from lingxi.core.admin.pending_action import PendingAction
from lingxi.core.admin.restricted_tools import operation_target

CONFIRMATION_STAGE = "confirmation_card_send"


@dataclass(frozen=True)
class FollowupDispatchResult:
    """``delivered`` 在这里表示「发卡意图已持久登记」，不表示卡片已送达。"""

    delivered: bool


class FollowupConfirmCardSender:
    """``ConfirmCardSender`` 的持久阶段实现；三个飞书形参一律忽略。"""

    requires_reply_context = False

    def __init__(self, *, store, tracker, audit):
        """``store`` 提供短事务，``tracker`` 只用它的发送失败落库口，``audit`` 记异常类型。"""
        self._store, self._tracker, self._audit = store, tracker, audit

    def send(self, *, pending: PendingAction, chat_id="", thread_id=None, reply_to_message_id=""):
        """同事务登记发卡阶段与 ``prepared`` 行；任一步失败即转发送失败并回「未送达」。"""
        del chat_id, thread_id, reply_to_message_id
        try:
            with self._store.transaction() as connection:
                enqueue_followups(
                    connection,
                    pending_action_id=pending.id,
                    trace_id=pending.id,
                    items=(FollowupSpec(subject_key=pending.id, stage=CONFIRMATION_STAGE),),
                )
                record_operation_audit(connection, prepared_entry(connection, pending))
        except Exception as error:
            self._audit.record(
                "admin.card_dispatch.send_failed",
                pending_action_id=pending.id,
                error=type(error).__name__,
            )
            self._tracker.mark_send_failed(pending_action_id=pending.id)
            return FollowupDispatchResult(delivered=False)
        return FollowupDispatchResult(delivered=True)


def prepared_entry(connection, pending: PendingAction) -> OperationAuditEntry:
    """``prepared`` 行：入口是受限通道，角色在调用方事务内现读，目标字段按动作类型投影。"""
    return OperationAuditEntry(
        operation_id=pending.id,
        operation="admin." + pending.action_type.value,
        phase=OperationPhase.PREPARED,
        initiated_by=pending.initiated_by_open_id,
        actor_roles=admin_roles_snapshot(connection, pending.initiated_by_open_id),
        entry_point=EntryPoint.RESTRICTED_CHANNEL,
        pending_action_id=pending.id,
        evidence_ref="pending_action:" + pending.id,
        **operation_target(pending),
    )
