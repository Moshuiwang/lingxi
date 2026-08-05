-- #17 S3：银河权限导出的导入批次。
--
-- 首期的权限来源是银河后台的**导出快照**而非接口（Issue #17 待决策 3 仍未回答
-- 刷新机制）。因此每次导入自成一个批次：五张表的行都挂在批次上，批次未确认
-- 完成之前没有任何一行会被下游读到，回滚时整批级联删除。
--
-- 落库只保留原始导出的结构，不在这里做任何解释：`全非` 哨兵、A 前缀角色映射、
-- 公司范围展开都发生在解释层（src/lingxi/core/permission/）。
--
-- 该导出含可识别人员数据，按合同的最长九十天保留：`expires_at` 交给受控清理
-- 流程回收；当前有效批次由刷新机制维持（属 Issue #17 待决策 3）。

CREATE TABLE galaxy_import_batch (
    id                          TEXT PRIMARY KEY,
    -- 来源标注：由导入人填写的可核对说明（例如「银河后台导出 2026-07-28」）。
    source_label                TEXT NOT NULL,
    -- 导出内容的 sha256 摘要，用于识别同一份导出被重复导入；不含人员数据。
    source_digest               TEXT NOT NULL,
    status                      TEXT NOT NULL
        CHECK (status IN ('staging', 'complete', 'failed', 'superseded')),
    started_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at                TIMESTAMPTZ,
    expires_at                  TIMESTAMPTZ NOT NULL DEFAULT now() + interval '2160 hours',
    user_row_count              INTEGER NOT NULL DEFAULT 0,
    user_role_row_count         INTEGER NOT NULL DEFAULT 0,
    role_menu_row_count         INTEGER NOT NULL DEFAULT 0,
    user_datacountry_row_count  INTEGER NOT NULL DEFAULT 0,
    country_row_count           INTEGER NOT NULL DEFAULT 0,
    error_code                  TEXT,
    -- 校验告警等结构信息（表名、列名、行号、计数），不写人员数据值。
    metadata                    JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- 同一份导出最多有一个已完成批次：重复导入据此返回既有批次而不是写第二份。
CREATE UNIQUE INDEX galaxy_import_batch_digest_uniq
    ON galaxy_import_batch (source_digest)
    WHERE status = 'complete';

CREATE INDEX galaxy_import_batch_status_idx
    ON galaxy_import_batch (status, started_at DESC);
CREATE INDEX galaxy_import_batch_expiry_idx
    ON galaxy_import_batch (expires_at);

COMMENT ON TABLE galaxy_import_batch IS
    '银河权限导出的导入批次；未确认完成的批次不对下游可见。';
COMMENT ON COLUMN galaxy_import_batch.source_digest IS
    '导出文件内容的 sha256；用于重复导入的幂等判定，不含人员数据。';
