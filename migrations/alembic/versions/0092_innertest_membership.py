"""动态内测资格、本人确认批次与受限通道绑定；不导入实际名单。"""

from alembic import op

revision = "0092_innertest_membership"
down_revision = "0091_admin_action_followup"
branch_labels = None
depends_on = None

_UPGRADE_SQL = """
ALTER TABLE pending_action DROP CONSTRAINT pending_action_action_type_check;
ALTER TABLE pending_action ADD CONSTRAINT pending_action_action_type_check CHECK
 (action_type IN ('suspend_user','resume_user','local_permission_grant',
 'local_permission_suppress','local_permission_revoke','innertest_additions'));
ALTER TABLE outreach_message ADD COLUMN effect_started_at TIMESTAMPTZ;
ALTER TABLE outreach_message DROP CONSTRAINT outreach_message_status_check;
ALTER TABLE outreach_message ADD CONSTRAINT outreach_message_status_check
 CHECK(status IN ('pending','delivered','failed','unknown'));
CREATE TABLE innertest_roster_version (
 scope TEXT PRIMARY KEY, version BIGINT NOT NULL DEFAULT 0 CHECK(version>=0),
 mode TEXT NOT NULL DEFAULT 'legacy' CHECK(mode IN ('legacy','database')),
 import_digest TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE innertest_admin_binding (
 id TEXT PRIMARY KEY, scope TEXT NOT NULL, open_id TEXT NOT NULL,
 version BIGINT NOT NULL CHECK(version>0), enabled BOOLEAN NOT NULL DEFAULT false,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(scope,open_id)
);
CREATE TABLE innertest_batch (
 id TEXT PRIMARY KEY, scope TEXT NOT NULL, initiated_by TEXT NOT NULL,
 binding_id TEXT NOT NULL REFERENCES innertest_admin_binding(id),
 binding_version BIGINT NOT NULL, request_key TEXT NOT NULL, intent_digest TEXT NOT NULL,
 roster_version BIGINT NOT NULL, target_digest TEXT NOT NULL, pending_action_id TEXT UNIQUE REFERENCES pending_action(id) ON DELETE SET NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','no_change','executed','cancelled','expired','failed')),
 trace_id TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(scope,initiated_by,request_key)
);
CREATE TABLE innertest_batch_item (
 id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES innertest_batch(id) ON DELETE CASCADE,
 email TEXT NOT NULL, open_id TEXT, personnel_id TEXT,
 result_code TEXT NOT NULL, followup_id TEXT REFERENCES admin_action_followup(id) ON DELETE SET NULL,
 UNIQUE(batch_id,email)
);
CREATE TABLE innertest_membership (
 scope TEXT NOT NULL, open_id TEXT NOT NULL, email TEXT NOT NULL,
 batch_id TEXT REFERENCES innertest_batch(id) ON DELETE SET NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(scope,open_id)
);
CREATE TABLE innertest_check (
 id TEXT PRIMARY KEY, batch_item_id TEXT REFERENCES innertest_batch_item(id) ON DELETE CASCADE,
 user_id TEXT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE, permission_version BIGINT NOT NULL,
 publish_version BIGINT NOT NULL, started_at TIMESTAMPTZ NOT NULL,
 finished_at TIMESTAMPTZ NOT NULL, result_code TEXT NOT NULL, metric_count INTEGER,
 trace_id TEXT NOT NULL
);
CREATE TABLE innertest_audit (
 id TEXT PRIMARY KEY, batch_id TEXT REFERENCES innertest_batch(id) ON DELETE CASCADE,
 action TEXT NOT NULL, subject TEXT NOT NULL, trace_id TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX innertest_batch_owner ON innertest_batch(scope,initiated_by,created_at);
CREATE INDEX innertest_check_item ON innertest_check(batch_item_id,finished_at);
"""


def upgrade():
    with op.get_bind().connection.cursor() as cursor:
        cursor.execute(_UPGRADE_SQL)


def downgrade():
    op.execute(
        "DO $$ BEGIN IF EXISTS(SELECT 1 FROM innertest_membership) OR "
        "EXISTS(SELECT 1 FROM innertest_batch) THEN "
        "RAISE EXCEPTION 'dynamic roster requires compatible recovery'; END IF; END $$"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS(SELECT 1 FROM outreach_message WHERE status='unknown' "
        "OR effect_started_at IS NOT NULL) THEN RAISE EXCEPTION "
        "'uncertain outreach requires compatible recovery'; END IF; END $$"
    )
    for table in (
        "innertest_audit",
        "innertest_check",
        "innertest_membership",
        "innertest_batch_item",
        "innertest_batch",
        "innertest_admin_binding",
        "innertest_roster_version",
    ):
        op.execute("DROP TABLE " + table)
    op.execute("ALTER TABLE pending_action DROP CONSTRAINT pending_action_action_type_check")
    op.execute(
        "ALTER TABLE pending_action ADD CONSTRAINT pending_action_action_type_check CHECK "
        "(action_type IN ('suspend_user','resume_user','local_permission_grant',"
        "'local_permission_suppress','local_permission_revoke'))"
    )

    op.execute("ALTER TABLE outreach_message DROP CONSTRAINT outreach_message_status_check")
    op.execute(
        "ALTER TABLE outreach_message ADD CONSTRAINT outreach_message_status_check "
        "CHECK(status IN ('pending','delivered','failed'))"
    )
    op.execute("ALTER TABLE outreach_message DROP COLUMN effect_started_at")
