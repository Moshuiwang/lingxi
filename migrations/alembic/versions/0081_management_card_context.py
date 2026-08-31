"""管理卡持久上下文与职位范围授权元数据。

Revision ID: 0081_management_card_context
Revises: 0080_task_failure_signature
Create Date: 2026-08-31

Issue #493。管理卡不能再依赖进程内 ``message_id -> identifier`` 映射：进程重启、
多实例切换或卡片更新后，回调必须仍能找回目标、卡片实体和最新 sequence。本 revision
只新增上下文与可空元数据列，不回填旧管理卡，也不改变既有管理员角色或权限范围。

本地补充授权的产品语义是「银河职位 + 公司范围」。确认执行时仍展开为现有
``local_permission_override`` 的公司×指标行，新增列只是让查询卡能把这些行重新
聚合回职位/范围。旧逐指标行的列保持 NULL，历史事实不被改写。
"""

from __future__ import annotations

from alembic import op

revision: str = "0081_management_card_context"
down_revision: str | None = "0080_task_failure_signature"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
CREATE TABLE management_card_context (
    message_id              TEXT PRIMARY KEY
        CHECK (NULLIF(BTRIM(message_id), '') IS NOT NULL),
    card_id                 TEXT NOT NULL UNIQUE
        CHECK (NULLIF(BTRIM(card_id), '') IS NOT NULL),
    identifier              TEXT NOT NULL
        CHECK (NULLIF(BTRIM(identifier), '') IS NOT NULL),
    chat_id                 TEXT NOT NULL
        CHECK (NULLIF(BTRIM(chat_id), '') IS NOT NULL),
    initiated_by_open_id    TEXT NOT NULL
        CHECK (NULLIF(BTRIM(initiated_by_open_id), '') IS NOT NULL),
    card_sequence           INTEGER NOT NULL DEFAULT 1
        CHECK (card_sequence > 0),
    snapshot_fingerprint    TEXT NOT NULL
        CHECK (NULLIF(BTRIM(snapshot_fingerprint), '') IS NOT NULL),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    context_deadline_at     TIMESTAMPTZ NOT NULL,
    state                   TEXT NOT NULL DEFAULT 'ready'
        CHECK (state IN ('ready', 'submitted', 'dispatching', 'effective', 'incomplete', 'closed')),
    dispatch_status         TEXT NOT NULL DEFAULT 'idle'
        CHECK (dispatch_status IN ('idle', 'publishing', 'effective', 'incomplete')),
    last_trace_id           TEXT,
    daily_correction_reported_at TIMESTAMPTZ,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX management_card_context_deadline_idx
    ON management_card_context (context_deadline_at);

ALTER TABLE pending_action
    ADD COLUMN IF NOT EXISTS origin_card_message_id TEXT
    REFERENCES management_card_context(message_id) ON DELETE SET NULL;

ALTER TABLE local_permission_override
    ADD COLUMN IF NOT EXISTS position_name TEXT;
ALTER TABLE local_permission_override
    ADD COLUMN IF NOT EXISTS company_scope TEXT;
ALTER TABLE local_permission_override
    ADD COLUMN IF NOT EXISTS permission_group_id TEXT;

CREATE INDEX local_permission_override_permission_group_idx
    ON local_permission_override (permission_group_id)
    WHERE permission_group_id IS NOT NULL AND entry_status = 'active';
"""

_DOWNGRADE_SQL = r"""
DROP INDEX IF EXISTS local_permission_override_permission_group_idx;
ALTER TABLE local_permission_override DROP COLUMN IF EXISTS permission_group_id;
ALTER TABLE local_permission_override DROP COLUMN IF EXISTS company_scope;
ALTER TABLE local_permission_override DROP COLUMN IF EXISTS position_name;
ALTER TABLE pending_action DROP COLUMN IF EXISTS origin_card_message_id;
DROP TABLE IF EXISTS management_card_context;
"""


def _execute_verbatim(connection, sql: str) -> None:
    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
