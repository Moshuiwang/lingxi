"""管理阶段只读投影，精确追溯关联并守九十天保留期限。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, connect


@dataclass(frozen=True)
class AdminFollowupView:
    """可查询状态不携带用户身份、权限正文或凭据。"""

    followup_id: str
    pending_action_id: str
    stage: str
    status: str
    result_code: str | None
    trace_id: str | None
    updated_at: datetime
    attempt: int


def fetch_followups(dsn, *, trace_id, timeouts=DEFAULT_POSTGRES_TIMEOUTS):
    """只按阶段实际 trace_id 精确匹配，缺入站事件仍可查询确认阶段。"""
    with connect(dsn, timeouts=timeouts) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT f.id,f.pending_action_id,f.stage,f.status,f.result_code,f.trace_id,f.updated_at,f.attempt "
            "FROM admin_action_followup f JOIN pending_action p ON p.id=f.pending_action_id "
            "WHERE f.trace_id=%s AND p.retention_expires_at>now() "
            "ORDER BY f.created_at,f.id LIMIT 100",
            (trace_id,),
        )
        return tuple(AdminFollowupView(*row) for row in cursor.fetchall())
