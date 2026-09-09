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
            "SELECT id,pending_action_id,stage,status,result_code,trace_id,updated_at,attempt "
            "FROM admin_action_followup WHERE trace_id=%s "
            "AND created_at>now()-interval '90 days' ORDER BY created_at,id LIMIT 100",
            (trace_id,),
        )
        return tuple(AdminFollowupView(*row) for row in cursor.fetchall())
