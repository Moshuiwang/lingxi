"""恢复重算只采用目标当前账号状态，绝不重演旧确认动作。"""

from __future__ import annotations

from dataclasses import replace

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, connect
from lingxi.core.admin.pending_action import PendingActionType


class CurrentPermissionRecompute:
    """既有重算器负责版本守卫，这里只把旧动作转换为当前账号事实。"""

    def __init__(self, delegate, dsn, *, timeouts=DEFAULT_POSTGRES_TIMEOUTS):
        """复用原重算器及连接预算，不新增权限来源。"""
        self.delegate, self.dsn, self.timeouts = delegate, dsn, timeouts

    def trigger(self, pending):
        """停用时撤回，启用时重算当前权限，不按历史按钮决定扩缩权。"""
        with connect(self.dsn, timeouts=self.timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT account_state FROM app_user WHERE feishu_open_id=%s",
                (pending.target_open_id,),
            )
            row = cursor.fetchone()
        if row is None:
            from lingxi.core.permission.targeted_recompute import (
                RecomputeKind,
                TargetedRecomputeOutcome,
            )

            return TargetedRecomputeOutcome(kind=RecomputeKind.SKIPPED, reason="target_unresolved")
        action = (
            PendingActionType.SUSPEND_USER if row[0] != "enabled" else PendingActionType.RESUME_USER
        )
        return self.delegate.trigger(replace(pending, action_type=action))
