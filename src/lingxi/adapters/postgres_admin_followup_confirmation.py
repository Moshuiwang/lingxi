"""把确认后的业务工作与运营审计账的终态行一起挂进现有确认事务。

确认、取消与执行的账目行在这里追加：它们与阶段登记共用调用方的确认事务，账写不进去
即整笔回滚，状态不会在没有审计的情况下改变。私聊命令面与受限通道发起的动作走同一个
确认事务，因此两条入口的终态行形状完全相同。
"""

from __future__ import annotations

from lingxi.adapters.postgres_admin_followup import enqueue_followups
from lingxi.adapters.postgres_innertest import admin_roles_snapshot
from lingxi.adapters.postgres_operation_audit import record_operation_audit
from lingxi.core.admin.followup import FollowupSpec
from lingxi.core.admin.management_card_states import (
    DISPATCH_IDLE,
    DISPATCH_INCOMPLETE,
    DISPATCH_PUBLISHING,
    STATE_DISPATCHING,
    STATE_INCOMPLETE,
    STATE_READY,
)
from lingxi.core.admin.operation_audit import (
    EntryPoint,
    OperationAuditEntry,
    OperationPhase,
    executor_label,
)
from lingxi.core.admin.pending_action import PendingActionStatus
from lingxi.core.admin.restricted_tools import ledger_identifier, operation_target

#: 终态 → 账上的阶段：执行成功与执行失败都先有一行「已确认」（本人点了确认），过期
#: 没有决定可言，记「已拒绝」；取消只记「已取消」。
_DECISION_PHASES = {
    PendingActionStatus.EXECUTED: OperationPhase.CONFIRMED,
    PendingActionStatus.FAILED: OperationPhase.CONFIRMED,
    PendingActionStatus.CANCELLED: OperationPhase.CANCELLED,
    PendingActionStatus.EXPIRED: OperationPhase.REJECTED,
}


def enqueue_confirmation(connection, *, pending, target_user_id, trace_id, notify_group):
    """只有本次新增终态才登记，不为旧执行记录补发。"""
    if target_user_id is None:
        raise ValueError("既有管理动作缺少已核验的目标用户")
    stages = ["terminal_card_refresh", "management_card_refresh"]
    if pending.status is PendingActionStatus.EXECUTED:
        stages.append("permission_recompute")
    if notify_group:
        stages.append("group_notify")
    refs = enqueue_followups(
        connection,
        pending_action_id=pending.id,
        trace_id=trace_id,
        items=tuple(
            FollowupSpec(subject_key=target_user_id, stage=stage, target_user_id=target_user_id)
            for stage in stages
        ),
    )
    record_decision(connection, pending=pending, refs=refs, trace_id=trace_id)
    if pending.origin_card_message_id:
        states = {
            PendingActionStatus.EXECUTED: (STATE_DISPATCHING, DISPATCH_PUBLISHING),
            PendingActionStatus.CANCELLED: (STATE_READY, DISPATCH_IDLE),
        }
        state, dispatch = states.get(pending.status, (STATE_INCOMPLETE, DISPATCH_INCOMPLETE))
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE management_card_context SET state=%s,"
                "dispatch_status=%s,state_version=state_version+1,card_sequence=card_sequence+1,"
                "needs_refresh=TRUE,"
                "updated_at=now() WHERE message_id=%s AND state<>'closed' "
                "AND %s=(SELECT id FROM pending_action WHERE origin_card_message_id=%s "
                "ORDER BY created_at DESC,id DESC LIMIT 1)",
                (
                    state,
                    dispatch,
                    pending.origin_card_message_id,
                    pending.id,
                    pending.origin_card_message_id,
                ),
            )
    return refs


def record_decision(connection, *, pending, refs, trace_id):
    """在确认事务内追加终态行：决定行（已确认 / 已取消 / 已拒绝）与执行行。

    执行行的证据指向本次登记的权限重算阶段；执行失败没有重算阶段，证据退回待确认
    操作本身，结果码带失败原因。
    """
    phase = _DECISION_PHASES.get(pending.status)
    if phase is None:
        return
    common = dict(
        operation_id=pending.id,
        operation="admin." + pending.action_type.value,
        initiated_by=pending.initiated_by_open_id,
        actor_roles=admin_roles_snapshot(connection, pending.initiated_by_open_id),
        pending_action_id=pending.id,
        trace_id=ledger_identifier(trace_id),
        **operation_target(pending),
    )
    record_operation_audit(
        connection,
        OperationAuditEntry(
            phase=phase,
            entry_point=EntryPoint.FEISHU_CARD,
            decided_by=pending.decided_by_open_id,
            result_code=pending.reason if phase is OperationPhase.REJECTED else None,
            evidence_ref="pending_action:" + pending.id,
            **common,
        ),
    )
    if phase is not OperationPhase.CONFIRMED:
        return
    recompute = next((ref.id for ref in refs if ref.stage == "permission_recompute"), None)
    executed = pending.status is PendingActionStatus.EXECUTED
    record_operation_audit(
        connection,
        OperationAuditEntry(
            phase=OperationPhase.EXECUTED,
            entry_point=EntryPoint.FEISHU_CARD,
            executor=executor_label("gateway", run_id=ledger_identifier(trace_id) or pending.id),
            result_code="executed" if executed else "failed:" + str(pending.reason or "unknown"),
            evidence_ref=("followup:" + recompute if recompute else "pending_action:" + pending.id),
            **common,
        ),
    )
