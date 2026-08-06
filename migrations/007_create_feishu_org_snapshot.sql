-- #16 S2：关联组织成员快照的正式表。
--
-- **本迁移替代测试资产 migrations/005_create_feishu_org_snapshot.sql。**
-- 005 是受控验收用的快照表，为了取证保留了全部原始接口响应；正式实现只保留
-- 完成身份关联所必需的标准化字段。005 仍留在仓库里供 scripts/ 的受控脚本使用，
-- 由验收人在 PR 阶段决定废弃时机。
--
-- 与 005 相比的正式化改动：
--   1. **删掉 status 列。** 可见范围不做在职过滤（710 人实测含 5 名冻结、
--      1 名未加入），存下来的在职状态一定是陈旧的，而这个字段的唯一用途恰恰是
--      拦截陈旧状态。正式链路每次开通判定时实时回读，见断言 V-开通-07。
--   2. **删掉 app_records / user_records / detail_record / avatar 等原始响应列。**
--      原始响应含大量与身份关联无关的可识别资料，正式表只存必需字段。
--   3. **三类标识改为 NOT NULL 且非空白。** 两条路径 710/710 全部可得已复验；
--      缺任何一个都属于完整性校验不该放过的情况，不允许落成半行。
--   4. **新增"完成批次必须自洽"的 CHECK。** 完整性校验不通过就不提交半轮快照
--      是硬规则：只有应用层校验会漏，这里让数据库也拒绝一个数量对不上的
--      complete 批次。
--   5. **expires_at 由触发器固定为 started_at + 2160 小时**，调用方传什么都会被
--      覆盖，也不允许后移——九十天是可识别内容的上限，不是可协商的默认值。
--
-- 绝不入库：OAuth token、App Secret、授权码、数据库凭据。

CREATE TABLE feishu_org_sync_run (
    id                          TEXT PRIMARY KEY,
    source_app_id               TEXT NOT NULL,
    status                      TEXT NOT NULL
        CHECK (status IN ('staging', 'complete', 'failed', 'superseded')),
    started_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at                TIMESTAMPTZ,
    expires_at                  TIMESTAMPTZ NOT NULL,
    tenant_count                INTEGER NOT NULL DEFAULT 0,
    department_count            INTEGER NOT NULL DEFAULT 0,
    member_count                INTEGER NOT NULL DEFAULT 0,
    error_code                  TEXT,
    -- 只放不含个人资料的批次统计与校验摘要。
    metadata                    JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- 一个 complete 批次必须自洽：有完成时间、有租户、有成员。数量对不上的
    -- 半轮快照在下游表现为"部分在职员工定位不到"，而原因完全看不出来。
    CHECK (
        status <> 'complete'
        OR (completed_at IS NOT NULL AND tenant_count > 0 AND member_count > 0)
    ),
    -- 失败批次不得声称自己有内容：失败即整轮回滚，计数必须归零。
    CHECK (
        status <> 'failed'
        OR (tenant_count = 0 AND department_count = 0 AND member_count = 0)
    )
);

CREATE INDEX feishu_org_sync_run_status_idx
    ON feishu_org_sync_run (status, started_at DESC);
CREATE INDEX feishu_org_sync_run_expiry_idx
    ON feishu_org_sync_run (expires_at);

-- 到期时间由来源时间推导，调用方不能自定义，也不能后移。
CREATE OR REPLACE FUNCTION feishu_org_sync_run_fix_expiry() RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.expires_at := NEW.started_at + INTERVAL '2160 hours';
    IF TG_OP = 'UPDATE' AND NEW.started_at <> OLD.started_at THEN
        RAISE EXCEPTION '不允许修改组织快照批次的来源时间';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER feishu_org_sync_run_expiry
    BEFORE INSERT OR UPDATE ON feishu_org_sync_run
    FOR EACH ROW EXECUTE FUNCTION feishu_org_sync_run_fix_expiry();

CREATE TABLE feishu_org_tenant_snapshot (
    id                          TEXT PRIMARY KEY,
    sync_run_id                 TEXT NOT NULL REFERENCES feishu_org_sync_run(id) ON DELETE CASCADE,
    tenant_key                  TEXT NOT NULL,
    -- 用户身份可见范围是两条路径能对上的前提；不可见的租户不允许进入完成批次。
    visible_to_user_identity    BOOLEAN NOT NULL,
    member_count                INTEGER NOT NULL DEFAULT 0,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (sync_run_id, tenant_key)
);

CREATE TABLE feishu_org_department_snapshot (
    id                          TEXT PRIMARY KEY,
    sync_run_id                 TEXT NOT NULL REFERENCES feishu_org_sync_run(id) ON DELETE CASCADE,
    tenant_key                  TEXT NOT NULL,
    department_key              TEXT NOT NULL,
    name                        TEXT,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (sync_run_id, tenant_key, department_key)
);

CREATE INDEX feishu_org_department_snapshot_lookup_idx
    ON feishu_org_department_snapshot (sync_run_id, tenant_key, department_key);

CREATE TABLE feishu_org_member_snapshot (
    id                          TEXT PRIMARY KEY,
    sync_run_id                 TEXT NOT NULL REFERENCES feishu_org_sync_run(id) ON DELETE CASCADE,
    tenant_key                  TEXT NOT NULL,
    member_key                  TEXT NOT NULL,
    open_id                     TEXT NOT NULL,
    user_id                     TEXT NOT NULL,
    union_id                    TEXT NOT NULL,
    -- 姓名不假定语言、不作唯一键：710 人实测含 11 人纯拉丁、9 人中拉混合、
    -- 1 对重名。locale 只作展示辅助，可为空。
    display_name                TEXT NOT NULL,
    display_name_locale         TEXT,
    department_names            JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (sync_run_id, tenant_key, member_key),
    CHECK (NULLIF(BTRIM(open_id), '') IS NOT NULL),
    CHECK (NULLIF(BTRIM(user_id), '') IS NOT NULL),
    CHECK (NULLIF(BTRIM(union_id), '') IS NOT NULL),
    CHECK (NULLIF(BTRIM(display_name), '') IS NOT NULL)
);

-- 首聊定位只按完整 open_id 查。前缀会碰撞（710 人中 57 组），不建前缀索引，
-- 也不建姓名唯一索引——姓名不是键。
CREATE INDEX feishu_org_member_snapshot_open_id_idx
    ON feishu_org_member_snapshot (open_id);
CREATE INDEX feishu_org_member_snapshot_user_id_idx
    ON feishu_org_member_snapshot (user_id);
CREATE INDEX feishu_org_member_snapshot_run_idx
    ON feishu_org_member_snapshot (sync_run_id);


-- 完成批次的计数必须与**实际子行**一致：声明值造假（或写路径缺陷）会让一个
-- 没有任何子行的 complete 批次被 lookup 选中，全员「定位不到」（终轮 Codex）。
-- 用 DEFERRABLE 约束触发器在提交时核对，迁移期即接线。
CREATE OR REPLACE FUNCTION feishu_org_sync_run_verify_children() RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    actual_tenants INTEGER;
    actual_members INTEGER;
BEGIN
    IF NEW.status <> 'complete' THEN
        RETURN NULL;
    END IF;
    SELECT count(*) INTO actual_tenants FROM feishu_org_tenant_snapshot WHERE sync_run_id = NEW.id;
    SELECT count(*) INTO actual_members FROM feishu_org_member_snapshot WHERE sync_run_id = NEW.id;
    IF actual_tenants <> NEW.tenant_count OR actual_members <> NEW.member_count THEN
        RAISE EXCEPTION '完成批次 % 的声明计数与实际子行不一致（租户 %/%、成员 %/%）',
            NEW.id, NEW.tenant_count, actual_tenants, NEW.member_count, actual_members;
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER feishu_org_sync_run_children_consistent
    AFTER INSERT OR UPDATE OF status ON feishu_org_sync_run
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION feishu_org_sync_run_verify_children();
