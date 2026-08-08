"""worker 执行收口、回复定位与安全重试状态。

Revision ID: 0058_worker_execution
Revises: 0057_gateway_tables
Create Date: 2026-08-08

Issue #90：把任务交给 worker 后仍需保留同话题的回复定位，并把「是否已经可能产生
外部副作用」落在数据库里。worker 崩溃后的自动重试只能发生在 ``none`` 状态；任何
不确定性都进入失败终态，避免把外部写操作重放一遍。
"""

from __future__ import annotations

from alembic import op

revision: str = "0058_worker_execution"
down_revision: str | None = "0057_gateway_tables"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
ALTER TABLE task ADD COLUMN reply_to_message_id TEXT;
ALTER TABLE task ADD COLUMN side_effect_state TEXT NOT NULL DEFAULT 'none'
    CHECK (side_effect_state IN ('none', 'possible'));

CREATE INDEX task_queued_created_idx
    ON task (created_at)
    WHERE status = 'queued';

-- 入队事务失败时没有 task 行可挂载错误通知。这个小表只保存事件标识和保留期限，
-- 不保存用户正文；唯一键同时承担并发、重启和重复事件去重。
CREATE TABLE queue_failure_notice (
    feishu_event_id TEXT PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL
);

CREATE OR REPLACE FUNCTION queue_failure_notice_fix_expiry() RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.expires_at := NEW.created_at + INTERVAL '2160 hours';
    IF TG_OP = 'UPDATE' AND NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION '不允许修改队列失败通知的创建时间';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER queue_failure_notice_expiry
    BEFORE INSERT OR UPDATE ON queue_failure_notice
    FOR EACH ROW EXECUTE FUNCTION queue_failure_notice_fix_expiry();
"""

_DOWNGRADE_SQL = r"""
DROP TABLE IF EXISTS queue_failure_notice;
DROP FUNCTION IF EXISTS queue_failure_notice_fix_expiry();
DROP INDEX IF EXISTS task_queued_created_idx;
ALTER TABLE task DROP COLUMN IF EXISTS side_effect_state;
ALTER TABLE task DROP COLUMN IF EXISTS reply_to_message_id;
"""


def _execute_verbatim(connection, sql: str) -> None:
    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
