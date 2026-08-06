-- #org-snapshot：测试库中的飞书关联组织完整资料快照。
--
-- 这是目录同步快照，不是正式 app_user。外部身份标识不作内部主键；
-- 每轮同步以独立 run_id 隔离，失败或回滚时可级联删除整轮数据。
-- raw_* 只接收飞书组织接口响应，不接收 OAuth token、App Secret 或授权码。
-- 可识别资料按同步批次过期，默认由 expires_at + 受控清理流程回收。

CREATE TABLE feishu_org_sync_run (
    id                          TEXT PRIMARY KEY,
    source_app_id               TEXT NOT NULL,
    status                      TEXT NOT NULL
        CHECK (status IN ('staging', 'complete', 'failed', 'superseded')),
    started_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at                TIMESTAMPTZ,
    expires_at                  TIMESTAMPTZ NOT NULL,
    app_tenant_count            INTEGER NOT NULL DEFAULT 0,
    user_visible_tenant_count   INTEGER NOT NULL DEFAULT 0,
    tenant_count                INTEGER NOT NULL DEFAULT 0,
    department_count            INTEGER NOT NULL DEFAULT 0,
    member_count                INTEGER NOT NULL DEFAULT 0,
    member_detail_count         INTEGER NOT NULL DEFAULT 0,
    error_code                  TEXT,
    metadata                    JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX feishu_org_sync_run_status_idx
    ON feishu_org_sync_run (status, started_at DESC);
CREATE INDEX feishu_org_sync_run_expiry_idx
    ON feishu_org_sync_run (expires_at);

CREATE TABLE feishu_org_tenant_snapshot (
    id                          TEXT PRIMARY KEY,
    sync_run_id                 TEXT NOT NULL REFERENCES feishu_org_sync_run(id) ON DELETE CASCADE,
    tenant_key                  TEXT NOT NULL,
    visible_to_user_identity    BOOLEAN NOT NULL DEFAULT FALSE,
    app_record                  JSONB NOT NULL DEFAULT '{}'::jsonb,
    user_record                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (sync_run_id, tenant_key)
);

CREATE INDEX feishu_org_tenant_snapshot_lookup_idx
    ON feishu_org_tenant_snapshot (tenant_key, created_at DESC);

CREATE TABLE feishu_org_department_snapshot (
    id                          TEXT PRIMARY KEY,
    sync_run_id                 TEXT NOT NULL REFERENCES feishu_org_sync_run(id) ON DELETE CASCADE,
    tenant_key                  TEXT NOT NULL,
    department_key              TEXT NOT NULL,
    open_department_id          TEXT,
    department_id               TEXT,
    app_records                 JSONB NOT NULL DEFAULT '[]'::jsonb,
    user_records                JSONB NOT NULL DEFAULT '[]'::jsonb,
    user_detail                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (sync_run_id, tenant_key, department_key)
);

CREATE INDEX feishu_org_department_snapshot_lookup_idx
    ON feishu_org_department_snapshot (tenant_key, department_key);

CREATE TABLE feishu_org_member_snapshot (
    id                          TEXT PRIMARY KEY,
    sync_run_id                 TEXT NOT NULL REFERENCES feishu_org_sync_run(id) ON DELETE CASCADE,
    tenant_key                  TEXT NOT NULL,
    member_key                  TEXT NOT NULL,
    open_user_id                TEXT,
    open_id                     TEXT,
    user_id                     TEXT,
    union_id                    TEXT,
    union_user_id               TEXT,
    name                        JSONB NOT NULL DEFAULT '{}'::jsonb,
    i18n_name                   JSONB NOT NULL DEFAULT '{}'::jsonb,
    avatar                      JSONB NOT NULL DEFAULT '{}'::jsonb,
    status                      JSONB NOT NULL DEFAULT '{}'::jsonb,
    parent_department_ids      JSONB NOT NULL DEFAULT '[]'::jsonb,
    app_records                 JSONB NOT NULL DEFAULT '[]'::jsonb,
    user_records                JSONB NOT NULL DEFAULT '[]'::jsonb,
    detail_record               JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (sync_run_id, tenant_key, member_key)
);

CREATE INDEX feishu_org_member_snapshot_user_id_idx
    ON feishu_org_member_snapshot (user_id);
CREATE INDEX feishu_org_member_snapshot_open_id_idx
    ON feishu_org_member_snapshot (open_id);
CREATE INDEX feishu_org_member_snapshot_union_id_idx
    ON feishu_org_member_snapshot (union_id);
CREATE INDEX feishu_org_member_snapshot_expiry_join_idx
    ON feishu_org_member_snapshot (sync_run_id, created_at DESC);
