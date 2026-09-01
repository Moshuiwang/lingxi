"""管理卡视觉恢复的业务状态代数 CAS（#493）。

Revision ID: 0084_management_card_state_cas
Revises: 0083_management_card_recovery
Create Date: 2026-09-01

``card_sequence`` 是发给 CardKit 的整卡序号；它会在状态写入与视觉回写取号时
递增，不能单独表示 scanner 渲染的业务状态。``state_version`` 是独立的逻辑状态
代数，恢复 scanner 读到的两项版本必须仍与数据库相同，旧视觉才有资格取号并在
CardKit 成功后清除 ``needs_refresh``。
"""

from __future__ import annotations

from alembic import op

revision: str = "0084_management_card_state_cas"
down_revision: str | None = "0083_management_card_recovery"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
ALTER TABLE management_card_context
    ADD COLUMN state_version INTEGER NOT NULL DEFAULT 1
        CHECK (state_version > 0);
"""

_DOWNGRADE_SQL = r"""
ALTER TABLE management_card_context
    DROP COLUMN IF EXISTS state_version;
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与既有 revision 同型：直接使用 psycopg cursor 执行 DDL。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
