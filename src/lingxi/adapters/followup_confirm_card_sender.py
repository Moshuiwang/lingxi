"""不依赖触发消息的确认卡发送口：登记持久发卡阶段，由 gateway 用既有飞书凭据直发本人。

受限通道跑在 scheduler 里，手上没有飞书会话、话题或消息标识，也不持飞书发送凭据。
这里的「发送」只做两件同事务的事：登记 ``confirmation_card_send`` 阶段、写运营审计账的
``prepared`` 行——登记失败即把待确认操作转为发送失败，本次操作不会执行；随后再用一条
新连接**尽力**补一行 ``rejected``（``result_code=prepare_ledger_failed:<异常类型>``），让这条
``failed`` 的待确认操作在账上有痕迹——补不进只记结构化日志，不改变前面的结论。真正的
卡片由 gateway 的持久阶段消费者按待确认动作类型分派发出，卡片送达才置 ``card_delivered``。
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
            self._record_rejected_best_effort(pending, error)
            return FollowupDispatchResult(delivered=False)
        return FollowupDispatchResult(delivered=True)

    def _record_rejected_best_effort(self, pending: PendingAction, error: Exception) -> None:
        """待确认操作已转 ``failed`` 之后，用新连接尽力补一行 ``rejected``；补不进只记日志。"""
        try:
            with self._store.transaction() as connection:
                record_operation_audit(
                    connection, rejected_entry(connection, pending, failure=error)
                )
        except Exception as ledger_error:
            self._audit.record(
                "operation_audit.write_failed",
                pending_action_id=pending.id,
                phase=OperationPhase.REJECTED.value,
                error=type(ledger_error).__name__,
            )


def _entry(connection, pending: PendingAction, *, phase: OperationPhase, **fields):
    """受限通道发起的动作在账上的公共列：角色在调用方事务内现读，目标字段按动作类型投影。"""
    return OperationAuditEntry(
        operation_id=pending.id,
        operation="admin." + pending.action_type.value,
        phase=phase,
        initiated_by=pending.initiated_by_open_id,
        actor_roles=admin_roles_snapshot(connection, pending.initiated_by_open_id),
        entry_point=EntryPoint.RESTRICTED_CHANNEL,
        pending_action_id=pending.id,
        evidence_ref="pending_action:" + pending.id,
        **operation_target(pending),
        **fields,
    )


def prepared_entry(connection, pending: PendingAction) -> OperationAuditEntry:
    """``prepared`` 行：与发卡阶段登记同事务提交。"""
    return _entry(connection, pending, phase=OperationPhase.PREPARED)


def rejected_entry(
    connection, pending: PendingAction, *, failure: Exception
) -> OperationAuditEntry:
    """``rejected`` 行：准备阶段登记失败的痕迹，结果码只带异常类型名，不带异常正文。"""
    return _entry(
        connection,
        pending,
        phase=OperationPhase.REJECTED,
        result_code="prepare_ledger_failed:" + type(failure).__name__,
    )
