"""运营操作的持久审计账 ``operation_audit``：只追加、到期固定九十天、无外键。

Revision ID: 0097_operation_audit
Revises: 0096_plpgsql_search_path

## 为什么

今天全仓的管理审计只是结构化日志，跨进程不可关联；唯一持久的例外是扩员专用的
窄表 ``innertest_audit``。预开通、欢迎卡、内测扩员三件运营操作需要一张能回答
「谁发起、谁确认、谁执行、目的与目标范围、结果、证据指针」的数据库账，后续的
管理动作与读取 / 导出审计都落在同一张表上。它不是数据库设计里那张「未建」的
分区表 ``audit_event``，后者继续未建。

## 形态

append-only 事件行：一次操作的各阶段各一行，用 ``operation_id`` 关联；脚本、
gateway 确认、scheduler 执行各自只 INSERT，不争行锁。表内没有自由文本列——
每一列要么是固定码、内部标识、计数或摘要，要么是时间；凭据与业务正文在应用层
按形状拒绝，不做脱敏。

- ``operation`` 是开放集合（正则 + 长度 CHECK），新增操作种类不需要迁移；
  ``phase`` / ``entry_point`` 是封闭集合。
- ``decided_by`` 在 confirmed / cancelled 两个阶段必填；``executor`` 与
  ``result_code`` 在 executed 阶段必填。
- **不建外键**：``ON DELETE SET NULL / CASCADE`` 会以 UPDATE / DELETE 触发行级
  触发器，与只追加的约束冲突；被指对象各有自己的生命周期，指针列只是指针。
- 两只触发器都固定 ``search_path``：BEFORE INSERT 把 ``expires_at`` 写死为
  ``created_at + 2160 小时``，调用方传什么都会被覆盖；BEFORE UPDATE 一律拒绝。
- 五个索引：按操作号回读、按时间回读、到期清理扫描、按追溯号与按目标用户各一个
  部分索引。

## 回滚

表非空即拒绝降级（与 ``0091`` / ``0092`` 同规则，应用回退只切镜像不降库）；
空表时删表并删两只触发器函数，完整逆转。
"""

from __future__ import annotations

from alembic import op

revision: str = "0097_operation_audit"
down_revision: str | None = "0096_plpgsql_search_path"
branch_labels: str | None = None
depends_on: str | None = None

_UPGRADE_SQL = r"""
CREATE TABLE operation_audit (
 id TEXT PRIMARY KEY,
 operation_id TEXT NOT NULL,
 operation TEXT NOT NULL
  CHECK (operation ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$' AND length(operation) <= 64),
 phase TEXT NOT NULL
  CHECK (phase IN ('prepared','confirmed','cancelled','rejected','executed')),
 initiated_by TEXT NOT NULL,
 actor_roles TEXT NOT NULL,
 decided_by TEXT,
 executor TEXT,
 entry_point TEXT NOT NULL
  CHECK (entry_point IN ('ops_script','restricted_channel','feishu_card','scheduler_followup')),
 purpose TEXT,
 target_kind TEXT,
 target_count INTEGER CHECK (target_count IS NULL OR target_count >= 0),
 target_digest TEXT
  CHECK (target_digest IS NULL OR target_digest ~ '^sha256:[0-9a-f]{64}$'),
 target_user_id TEXT,
 result_code TEXT,
 result_counts JSONB NOT NULL DEFAULT '{}'::jsonb
  CHECK (jsonb_typeof(result_counts) = 'object'),
 evidence_ref TEXT
  CHECK (evidence_ref IS NULL OR evidence_ref ~ '^[a-z_]+:[A-Za-z0-9_:.-]{1,128}$'),
 pending_action_id TEXT,
 trace_id TEXT CHECK (trace_id IS NULL OR trace_id ~ '^[A-Za-z0-9_-]{1,64}$'),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 expires_at TIMESTAMPTZ NOT NULL,
 CHECK (phase NOT IN ('confirmed','cancelled') OR decided_by IS NOT NULL),
 CHECK (phase <> 'executed' OR (executor IS NOT NULL AND result_code IS NOT NULL))
);

CREATE INDEX operation_audit_operation_idx
 ON operation_audit (operation_id, created_at, id);
CREATE INDEX operation_audit_recent_idx
 ON operation_audit (created_at DESC, id);
CREATE INDEX operation_audit_expiry_idx
 ON operation_audit (expires_at, id);
CREATE INDEX operation_audit_trace_idx
 ON operation_audit (trace_id) WHERE trace_id IS NOT NULL;
CREATE INDEX operation_audit_target_user_idx
 ON operation_audit (target_user_id) WHERE target_user_id IS NOT NULL;

CREATE FUNCTION operation_audit_fix_expiry() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    NEW.expires_at := NEW.created_at + INTERVAL '2160 hours';
    RETURN NEW;
END;
$$;

CREATE TRIGGER operation_audit_expiry
    BEFORE INSERT ON operation_audit
    FOR EACH ROW EXECUTE FUNCTION operation_audit_fix_expiry();

CREATE FUNCTION operation_audit_append_only() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    RAISE EXCEPTION '运营审计记录只追加、不允许修改';
END;
$$;

CREATE TRIGGER operation_audit_no_update
    BEFORE UPDATE ON operation_audit
    FOR EACH ROW EXECUTE FUNCTION operation_audit_append_only();
"""


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cursor:
        cursor.execute(_UPGRADE_SQL)


def downgrade() -> None:
    # 账上有记录时不能删掉唯一的持久去向；应用回退只切镜像，不降级数据库。
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM operation_audit) THEN "
        "RAISE EXCEPTION 'operation audit records require compatible recovery'; END IF; END $$"
    )
    op.execute("DROP TABLE operation_audit")
    op.execute("DROP FUNCTION operation_audit_fix_expiry()")
    op.execute("DROP FUNCTION operation_audit_append_only()")
