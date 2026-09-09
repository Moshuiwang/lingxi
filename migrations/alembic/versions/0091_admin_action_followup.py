"""管理确认后处理的持久阶段，不回填历史通知。"""

from alembic import op

revision = "0091_admin_action_followup"
down_revision = "0090_delivery_retry_backoff"
branch_labels = None
depends_on = None

_UPGRADE_SQL = """
CREATE TABLE admin_action_followup (
 id TEXT PRIMARY KEY,
 pending_action_id TEXT NOT NULL REFERENCES pending_action(id) ON DELETE CASCADE,
 subject_key TEXT NOT NULL,
 stage TEXT NOT NULL,
 contract_version INTEGER NOT NULL DEFAULT 1,
 trace_id TEXT,
 target_user_id TEXT REFERENCES app_user(id) ON DELETE SET NULL,
 target_version BIGINT,
 batch_id TEXT,
 batch_item_id TEXT,
 depends_on_id TEXT REFERENCES admin_action_followup(id) ON DELETE SET NULL,
 status TEXT NOT NULL DEFAULT 'pending'
  CHECK (status IN ('pending','running','retry_wait','succeeded','skipped','failed','unknown')),
 attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
 failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
 lease_owner TEXT,
 lease_until TIMESTAMPTZ,
 next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 effect_started_at TIMESTAMPTZ,
 external_ref TEXT,
 result_code TEXT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 finished_at TIMESTAMPTZ,
 UNIQUE (pending_action_id, subject_key, stage),
 CHECK (batch_item_id IS NULL OR subject_key = batch_item_id)
);
CREATE INDEX admin_followup_due ON admin_action_followup(next_attempt_at,id)
 WHERE status IN ('pending','retry_wait');
CREATE INDEX admin_followup_lease ON admin_action_followup(lease_until)
 WHERE status = 'running';
CREATE INDEX admin_followup_action ON admin_action_followup(pending_action_id);
CREATE INDEX admin_followup_item ON admin_action_followup(batch_item_id);
CREATE INDEX admin_followup_trace ON admin_action_followup(trace_id,created_at,id);
CREATE INDEX admin_followup_operation_retention ON pending_action(retention_expires_at,id);
"""


def upgrade():
    with op.get_bind().connection.cursor() as cursor:
        cursor.execute(_UPGRADE_SQL)


def downgrade():
    # 有工作时不能删除唯一持久去向；应用回退使用兼容 v1 的制品。
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM admin_action_followup) THEN "
        "RAISE EXCEPTION 'followup records require compatible recovery'; END IF; END $$"
    )
    op.execute("DROP TABLE admin_action_followup")
    op.execute("DROP INDEX admin_followup_operation_retention")
