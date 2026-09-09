"""两个只读入口共享的任务投影；不读取问题、答案或身份资料。"""

from __future__ import annotations

from typing import Any

from lingxi.core.failure_signature import sanitize_failure_signature
from lingxi.core.task_reference import parse_reference

_TASK_PROJECTION = """
    SELECT task.status, task.error_kind, task.failure_code,
           task.failure_signature, task.ended_at,
           delivery.status, delivery.last_error, delivery.body_degraded_reason,
           task.started_at
      FROM task
      LEFT JOIN task_document_delivery_request AS delivery ON delivery.task_id = task.id
"""
_EVENT_TASK_SQL = (
    _TASK_PROJECTION
    + """
      JOIN inbound_event ON inbound_event.feishu_event_id = task.inbound_event_id
     WHERE inbound_event.trace_id = %s AND inbound_event.expires_at > now()
       AND task.created_at + interval '2160 hours' > now()
     ORDER BY task.created_at DESC, delivery.created_at DESC NULLS LAST LIMIT 1
"""
)
_REFERENCE_TASK_SQL = (
    _TASK_PROJECTION
    + """
     WHERE task.id = %s AND task.created_at + interval '2160 hours' > now()
     ORDER BY delivery.created_at DESC NULLS LAST LIMIT 1
"""
)


def fetch_trace_task(cursor: Any, reference: str) -> tuple | None:
    """无效形状零查询；T-号始终精确指向任务，与源事件是否恢复无关。"""
    parsed = parse_reference(reference)
    if parsed is None:
        return None
    if parsed.reference_kind == "task":
        cursor.execute(_REFERENCE_TASK_SQL, ("tsk_" + parsed.reference[2:],))
    else:
        cursor.execute(_EVENT_TASK_SQL, (parsed.reference,))
    row = cursor.fetchone()
    if row is None:
        return None
    values = list(row)
    if values[3] is not None:
        values[3] = sanitize_failure_signature(values[3])
    return tuple(values)
