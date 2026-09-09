"""把确认后的业务工作与现有确认事务一起提交。"""

from __future__ import annotations

from lingxi.adapters.postgres_admin_followup import enqueue_followups
from lingxi.core.admin.followup import FollowupSpec
from lingxi.core.admin.pending_action import PendingActionStatus


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
    if pending.origin_card_message_id:
        states = {
            PendingActionStatus.EXECUTED: ("dispatching", "publishing"),
            PendingActionStatus.CANCELLED: ("ready", "idle"),
        }
        state, dispatch = states.get(pending.status, ("incomplete", "incomplete"))
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
