"""管理卡视觉回写与每日纠偏标记的持久水位（#493）。

Revision ID: 0083_management_card_recovery
Revises: 0082_document_delivery_degraded
Create Date: 2026-08-31

``management_card_context.state`` 是数据库业务状态，不能同时充当 CardKit 视觉
状态：gateway 可能在状态提交后重启，或 CardKit update 暂时失败。下面三列把两者
分开，允许启动/周期扫描从库中重试，并且只有外部 update 成功后才推进视觉水位。
``daily_correction_pending`` 也把“确实由 daily refresh/batch 补齐”与迟到的 instant
outbox 成功区分开，避免后者伪称每日批处理。
"""

from __future__ import annotations

from alembic import op

revision: str = "0083_management_card_recovery"
down_revision: str | None = "0082_document_delivery_degraded"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
ALTER TABLE management_card_context
    ADD COLUMN daily_correction_pending BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE management_card_context
    ADD COLUMN needs_refresh BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE management_card_context
    ADD COLUMN visual_sequence INTEGER NOT NULL DEFAULT 2
        CHECK (visual_sequence > 0);

-- 代码层的 TTL clamp 之外再加数据库纵深防线，避免任何绕过适配器的配置/写入把
-- 上下文有效窗口延长到 24 小时之后。
ALTER TABLE management_card_context
    ADD CONSTRAINT management_card_context_deadline_max_24h
    CHECK (context_deadline_at <= created_at + INTERVAL '24 hours');
"""

_DOWNGRADE_SQL = r"""
ALTER TABLE management_card_context
    DROP CONSTRAINT IF EXISTS management_card_context_deadline_max_24h;
ALTER TABLE management_card_context DROP COLUMN IF EXISTS visual_sequence;
ALTER TABLE management_card_context DROP COLUMN IF EXISTS needs_refresh;
ALTER TABLE management_card_context DROP COLUMN IF EXISTS daily_correction_pending;
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与既有 revision 同型：不走 ``op.execute()``，避免空参数集触发插值模式。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
