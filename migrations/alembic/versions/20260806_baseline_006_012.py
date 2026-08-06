"""基线：编号 SQL 006–012 合并为单条 revision

Revision ID: 20260806_baseline
Revises: 无（这是链首）
Create Date: 2026-08-06

本 revision 是 `migrations/006`–`012` 六个编号 SQL 的**逐字节副本**，按编号顺序执行。
它不是"等价重写"，是同一段字节——这一点是刻意的，也是整个 Issue #53 的关键约束：

**PostgreSQL 的匿名约束名、匿名索引名、被 NAMEDATALEN 截断的名字，都由声明顺序
决定。** 全链有 12 个匿名主键、8 个匿名外键、带序号后缀的匿名 CHECK、一个被截断的
UNIQUE 名。把 SQL 改写成 `op.create_table(...)` 会重新生成这些名字，两条血统（旧库
走编号 SQL、新库走 alembic）从此拿到**结构相同但名字不同**的库；之后任何一条按名字
`DROP CONSTRAINT` 的迁移，会在一边成功、在另一边失败。这类分歧不会在建库时报错，
只会在几个月后的某次迁移里炸开。

所以这里既不重写也不"顺手加 `IF NOT EXISTS`"。**基线必须在已有对象的库上失败**，
这正是旧库接管流程要求先 `alembic stamp` 的原因（见 migrations/README.md）：一个
能在旧库上"跑得通"的基线，会把"版本号对不上"这种响亮的错误，换成"迁移记录说做过了、
实际什么都没做"这种沉默的错误。

`migrations/006`–`012` 在过渡期保留（旧库补跑到 012 需要它们），并由门禁
`scripts/ci/check_migration_chain.sh` 持续核对两条链建出的库完全一致；副本与原文
一旦分叉，门禁就红。退休条件见 migrations/README.md。
"""

from __future__ import annotations

from alembic import op

revision: str = "20260806_baseline"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


_SQL_006 = r"""-- #16 S2：「四达文档会议助手」专用组织资料授权的**主体登记表**。
--
-- **本迁移替代测试资产 migrations/testing/003_create_feishu_user_refresh_token.sql。**
-- 003 把用户凭据密文存进数据库，那是 Bot-Test 受控验证的临时形态；正式边界由
-- 产品负责人 2026-08-05 在 Issue #16 决策（选项 A）定死：**凭据不进业务数据库**
--（数据库设计原则 3、代码框架横切约定）。`refresh_token` 的 Fernet 密文保存在
-- 宿主机受控文件（见 src/lingxi/adapters/delegated_credentials.py），不随
-- Supabase 及其备份出司界；数据库只保留本登记表：
--
--   1. 主体 `subject_open_id`——它是 V-身份-02 **双向触发器**的数据源
--      （migrations/008）：专用授权账号不能被建成员工记录，员工记录的
--      open_id 也不能被写成专用授权主体。撤销与收殓都只动凭据文件，
--      不动本表，防线不随凭据失效而消失；
--   2. 配置状态（原则 3 允许保存的「是否已配置」布尔事实与时间）。
--
-- purpose 主键 + CHECK：全库最多一个专用授权主体。新增用途要走迁移，
-- 不能靠调用方传字符串。
-- 绝不入库：refresh_token（含密文）、access_token、授权码、App Secret、
-- 解密密钥、任何明文凭据。普通员工不经过 OAuth（2026-07-28 决策）。

CREATE TABLE feishu_delegated_subject (
    purpose                   TEXT PRIMARY KEY
        CHECK (purpose = 'org_directory_sync'),
    subject_open_id           TEXT NOT NULL
        CHECK (NULLIF(BTRIM(subject_open_id), '') IS NOT NULL),
    configured_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


_SQL_007 = r"""-- #16 S2：关联组织成员快照的正式表。
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
"""


_SQL_008 = r"""-- #16 S2：统一用户记录（`app_user`）的正式表，身份部分。
--
-- **本迁移替代测试资产 migrations/001_create_app_user.sql。**
-- 001 服务于已被决策排除的浏览器 OAuth 授权路径（2026-07-28 决策：普通员工
-- 不经过授权卡片或浏览器 OAuth），它的 `authorizing` 状态和
-- `record_authorized_identity()` 函数在正式路径上没有对应动作。001 仍留在仓库里
-- 供既有受控测试使用，由验收人在 PR 阶段决定废弃时机。
--
-- **依赖 migrations/006**：本迁移的触发器要读 feishu_delegated_subject，
-- 必须在 006 之后执行。
--
-- 与 001 相比的正式化改动：
--   1. 去掉 `authorizing` 状态，状态集合与数据库设计第三节对齐；
--   2. 补上 permission_record_id / permission_version / permission_checked_at。
--      **它们在本切片一律保持默认值**——断言 V-开通-01 要求"匹配确认前
--      permission_record_id 为 NULL，不先占位再回填"，而"字段根本不存在"
--      证明不了这条：只有列存在且确实为 NULL 才是可验证的事实；
--   3. 新增触发器：专用授权账号本身永远不能被建成 app_user（断言 V-身份-02）。
--      应用层也有同一条判断，这里是绕不过去的那一道；
--   4. 保留 001 已验证的"身份四元组要么全空要么全非空"约束——不写半条记录。
--
-- **本表没有、也不会有在职状态列。** 可见范围不做在职过滤，存下来的状态一定
-- 陈旧，而它的唯一用途就是拦截陈旧状态（断言 V-开通-07）。
-- 本表也不存任何令牌、短期凭据或聊天内容。

-- 硬依赖断言：注释挡不住乱序执行，PL/pgSQL 函数体在 CREATE 时又不解析表名，
-- 缺 006 时本迁移会「成功」、然后在第一次建档时才运行期爆炸（独立复查发现）。
-- 在这里直接失败，把静默错误变成迁移期的显式错误。
DO $$
BEGIN
    IF to_regclass('public.feishu_delegated_subject') IS NULL THEN
        RAISE EXCEPTION '008 依赖 006（feishu_delegated_subject 登记表），请先执行 006';
    END IF;
END
$$;

CREATE TABLE app_user (
    id                      TEXT PRIMARY KEY,               -- ULID, usr_*

    -- 飞书信息。open_id 是首聊事件里唯一直接可得的标识，因此它是去重键；
    -- user_id / union_id 不设唯一约束：账号复用换人的判定按 #34 方案 C 留给
    -- 管理员侧审计，不在自动化链路里变成硬失败。
    feishu_open_id          TEXT UNIQUE,
    feishu_user_id          TEXT,
    feishu_union_id         TEXT,
    display_name            TEXT,
    display_name_locale     TEXT,
    department              TEXT,
    tenant_key              TEXT,

    -- 花名册字段（2026-08-05《花名册身份链与工号邮箱匹配》决策）：工号是匹配
    -- 银河的主键、邮箱是辅键，均来自公司花名册多维表格，由花名册读取步骤填充。
    -- 建档不以它们为前提，因此可空，且不参与下方身份字段的全有全无约束。
    employee_no             TEXT,
    email                   TEXT,

    -- 权限信息。本切片只建列不写值。
    permission_record_id    TEXT,
    permission_version      BIGINT NOT NULL DEFAULT 0,
    permission_checked_at   TIMESTAMPTZ,

    provisioning_state      TEXT NOT NULL DEFAULT 'guest'
        CHECK (provisioning_state IN (
            'guest', 'matching', 'manual_review',
            'provisioning', 'mcp_syncing', 'active', 'aborted'
        )),
    account_state           TEXT NOT NULL DEFAULT 'enabled'
        CHECK (account_state IN ('enabled', 'suspended', 'deleting', 'deleted')),

    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- 定位失败或资料不完整时不得留下半条记录（断言 V-开通-06）。
    CHECK (
        (
            NULLIF(BTRIM(feishu_open_id), '') IS NULL
            AND NULLIF(BTRIM(feishu_user_id), '') IS NULL
            AND NULLIF(BTRIM(feishu_union_id), '') IS NULL
            AND NULLIF(BTRIM(display_name), '') IS NULL
            AND NULLIF(BTRIM(department), '') IS NULL
            AND NULLIF(BTRIM(tenant_key), '') IS NULL
        ) OR (
            NULLIF(BTRIM(feishu_open_id), '') IS NOT NULL
            AND NULLIF(BTRIM(feishu_user_id), '') IS NOT NULL
            AND NULLIF(BTRIM(feishu_union_id), '') IS NOT NULL
            AND NULLIF(BTRIM(display_name), '') IS NOT NULL
            -- 部门属产品合同明列的必要资料（终轮 Codex：数据库也要落实，
            -- 否则直连 SQL / 手工构造 draft 可绕过应用层的人工核对）。
            AND NULLIF(BTRIM(department), '') IS NOT NULL
            AND NULLIF(BTRIM(tenant_key), '') IS NOT NULL
        )
    )
);

CREATE INDEX app_user_provisioning_state_pending_idx
    ON app_user (provisioning_state)
    WHERE provisioning_state <> 'active';

-- 姓名可查但不是键：建普通索引，绝不建唯一索引（710 人实测含 1 对重名）。
CREATE INDEX app_user_display_name_idx ON app_user (display_name);

-- 专用授权账号不是员工：它由组织资料同步使用，不提供问数服务，也不建用户记录。
-- 应用层已有同一条判断；这里让它成为任何代码路径都绕不过去的数据库事实。
CREATE OR REPLACE FUNCTION app_user_reject_delegated_subject() RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    -- 与凭据侧触发器争用同一 advisory 锁：两个 BEFORE 触发器各自的 EXISTS 在
    -- MVCC 下看不见对方未提交的行，并发提交会让双向防线同时失守（终轮 Codex）。
    PERFORM pg_advisory_xact_lock(4217003);
    IF NEW.feishu_open_id IS NOT NULL
       AND EXISTS (
           SELECT 1 FROM feishu_delegated_subject
            WHERE subject_open_id = NEW.feishu_open_id
       ) THEN
        RAISE EXCEPTION '专用授权账号不能被建成用户记录';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER app_user_no_delegated_subject
    BEFORE INSERT OR UPDATE OF feishu_open_id ON app_user
    FOR EACH ROW EXECUTE FUNCTION app_user_reject_delegated_subject();

-- 反方向同样要守（Codex 复查发现）：若先有员工记录、后把同一 open_id 写成
-- 专用授权主体，上面的触发器不会执行（写入发生在凭据表一侧），两边会共存。
-- V-身份-02 必须由数据库独立保证，两个方向都绕不过去。
CREATE OR REPLACE FUNCTION credential_reject_app_user_subject() RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(4217003);
    IF EXISTS (
        SELECT 1 FROM app_user WHERE feishu_open_id = NEW.subject_open_id
    ) THEN
        RAISE EXCEPTION '该 open_id 已是员工用户记录，不能成为专用授权主体';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER credential_no_app_user_subject
    BEFORE INSERT OR UPDATE OF subject_open_id ON feishu_delegated_subject
    FOR EACH ROW EXECUTE FUNCTION credential_reject_app_user_subject();
"""


_SQL_010 = r"""-- #17 S3：银河权限导出的导入批次。
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
        -- 'staging' 只存在于导入事务内部（提交即 complete，失败整体回滚）；
        -- 'failed' 与 error_code 预留给未来拆步导入的失败留痕路径，当前代码不产生。
        -- 任何按 expires_at 的清理流程必须排除当前有效批次（见 #17 待决策 3 登记）。
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
"""


_SQL_011 = r"""-- #17 S3：银河 user / user_role / role_menu 三张表的落库结构（职能范围链路）。
--
-- 连接规则（docs/参考证据/银河用户权限数据结构.md）：
--   galaxy_user.user_id ──< galaxy_user_role >── role_id ──< galaxy_role_menu
-- **一律按 user_id 连接**；姓名列不是账号，按它连接会取到语义相反的值。
--
-- 这些表是银河导出的落库副本，不是 Lingxi 的权限权威：`app_user` 与
-- `user_permission_scope` 才是准入与发布依据（#16 / S5）。因此这里**不引用
-- app_user**，也不改动身份切片的任何结构。

CREATE TABLE galaxy_user (
    batch_id        TEXT NOT NULL REFERENCES galaxy_import_batch(id) ON DELETE CASCADE,
    user_id         TEXT NOT NULL,
    dept_id         TEXT,
    -- 登录账号，实测绝大多数是纯数字工号；与 galaxy_user_role.source_user_name 不同义。
    user_name       TEXT,
    -- 中文姓名。按 Issue #34 决策它**不作匹配键**，只作人工核对的辅助信息。
    nick_name       TEXT,
    email           TEXT,
    -- 源导出的建号时间原文；格式未经核对，不在导入期猜测解析。
    create_time_raw TEXT,
    PRIMARY KEY (batch_id, user_id)
);

CREATE INDEX galaxy_user_email_idx ON galaxy_user (batch_id, lower(email));

CREATE TABLE galaxy_user_role (
    batch_id         TEXT NOT NULL REFERENCES galaxy_import_batch(id) ON DELETE CASCADE,
    user_id          TEXT NOT NULL,
    role_id          TEXT NOT NULL,
    -- 源列名是 user_name，值实测几乎全是中文姓名。改名落库，避免任何人按它连接。
    source_user_name TEXT,
    role_name        TEXT,
    PRIMARY KEY (batch_id, user_id, role_id)
);

CREATE INDEX galaxy_user_role_role_idx ON galaxy_user_role (batch_id, role_id);

CREATE TABLE galaxy_role_menu (
    batch_id  TEXT NOT NULL REFERENCES galaxy_import_batch(id) ON DELETE CASCADE,
    role_id   TEXT NOT NULL,
    menu_id   TEXT NOT NULL,
    role_name TEXT,
    -- 冗余副本，且**不唯一**（同名菜单出现在菜单树的不同位置）。判定一律用 menu_id。
    menu_name TEXT,
    PRIMARY KEY (batch_id, role_id, menu_id)
);

COMMENT ON COLUMN galaxy_user.nick_name IS
    '中文姓名；仅作人工核对辅助信息，按 Issue #34 决策不作为匹配键。';
COMMENT ON COLUMN galaxy_user_role.source_user_name IS
    '源列 user_name 的原值，实测几乎全是中文姓名；禁止用于连接，连接一律用 user_id。';
COMMENT ON COLUMN galaxy_role_menu.menu_name IS
    '菜单名冗余副本，存在同名不同 menu_id；判定与展示一律以 menu_id 为准。';
"""


_SQL_012 = r"""-- #17 S3：银河 sys_user_datacountry / sys_country 的落库结构（公司范围链路）。
--
-- 连接规则（docs/参考证据/银河用户权限数据结构.md）：
--   galaxy_user.user_id ──< galaxy_user_datacountry >── country_key ──< galaxy_country
-- **连接键是 country_key，不是 galaxy_country.source_id（源主键 id）**：两者在
-- 真实导出中几乎完全对不上，按主键连接会取到另一个国家。
--
-- 源列名为大写（USER_ID / DATACOUNTRY_ID）。落库列名统一小写，导入器同时接受
-- 两种源样式，避免「同一列两种写法」在查询里长期存在。
--
-- `全非` 哨兵行（country_key=0 / name=ALL / name_cn=全非）**原样落库**；把它解释成
-- 「所有国家所有公司」是解释层的事（产品负责人 2026-08-05 决策 1），落库不改写。

CREATE TABLE galaxy_country (
    batch_id         TEXT NOT NULL REFERENCES galaxy_import_batch(id) ON DELETE CASCADE,
    -- 源主键 id。保留是为了可回溯，**不是连接键**。
    source_id        TEXT NOT NULL,
    -- 与 galaxy_user_datacountry.datacountry_id 连接的键；约五分之一的行为空，
    -- 这些国家无法从授权表到达。
    country_key      TEXT,
    name             TEXT,
    code             TEXT,
    -- 中文国名，可直接用于向用户展示（展示本身属 S5）。
    name_cn          TEXT,
    region_key       TEXT,
    -- 大区名，实测未覆盖全部国家，不能据此汇总展示公司范围。
    region_nameX      TEXT,
    boss_company_id  TEXT,
    PRIMARY KEY (batch_id, source_id)
);

CREATE INDEX galaxy_country_key_idx ON galaxy_country (batch_id, country_key);

CREATE TABLE galaxy_user_datacountry (
    batch_id         TEXT NOT NULL REFERENCES galaxy_import_batch(id) ON DELETE CASCADE,
    -- 源列 USER_ID（大写）。
    user_id          TEXT NOT NULL,
    -- 源列 DATACOUNTRY_ID（大写）；连接到 galaxy_country.country_key。
    datacountry_id   TEXT NOT NULL,
    -- 源列 USER_NAME，与 galaxy_user_role.source_user_name 同样是姓名而非账号。
    source_user_name TEXT,
    datacountry_name TEXT,
    PRIMARY KEY (batch_id, user_id, datacountry_id)
);

CREATE INDEX galaxy_user_datacountry_country_idx
    ON galaxy_user_datacountry (batch_id, datacountry_id);

COMMENT ON COLUMN galaxy_country.boss_company_id IS
    '对 MCP 申请权限时使用的公司字段（产品负责人 2026-08-05 决策 3）；申请动作本身属 S5。';
COMMENT ON COLUMN galaxy_country.country_key IS
    '公司范围的连接键；不是主键 source_id，按主键连接会取到另一个国家。';
COMMENT ON COLUMN galaxy_country.source_id IS
    '银河 sys_country 的源主键 id，仅供回溯，不作连接键。';
COMMENT ON COLUMN galaxy_user_datacountry.datacountry_id IS
    '源列 DATACOUNTRY_ID；连接 galaxy_country.country_key。值为 0 时是「全非」哨兵，原样保留。';
"""


# 逐字节副本与源文件名的对应关系。顺序即执行顺序，**不可重排**：008 的触发器要读
# 006 建的表（文件内有硬依赖断言），而匿名对象的名字也由这个顺序决定。
CHAIN: tuple[tuple[str, str], ...] = (
    ("006_create_feishu_delegated_credential.sql", _SQL_006),
    ("007_create_feishu_org_snapshot.sql", _SQL_007),
    ("008_create_app_user.sql", _SQL_008),
    ("010_create_galaxy_import_batch.sql", _SQL_010),
    ("011_create_galaxy_user_and_role.sql", _SQL_011),
    ("012_create_galaxy_country_scope.sql", _SQL_012),
)


def _execute_verbatim(sql: str) -> None:
    """按原文执行整段 SQL，绕过参数插值。

    不能用 `op.execute()` 或 `exec_driver_sql()`：两者最终都会把 SQL 连同一个
    （空的）参数容器交给 psycopg，psycopg 于是把 SQL 里的 `%` 当占位符解析，
    在 `RAISE EXCEPTION '完成批次 % 的...'` 这一行直接报
    `incomplete placeholder: '%'`（本地实测）。

    绕过它的常见做法是把 `%` 写成 `%%`——那样这份副本就不再是逐字节副本，
    上面整段理由随之作废。改为取原生 DBAPI 游标、单参数调用 execute()：
    psycopg 只在传了参数时才做插值，不传就原样发给服务端。

    游标取自 alembic 当前的连接，因此仍在该 revision 自己的事务里；
    失败即整条 revision 回滚。
    """

    with op.get_bind().connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    for _source_file, statements in CHAIN:
        _execute_verbatim(statements)


def downgrade() -> None:
    """基线不支持 downgrade：显式拒绝，不静默成功。

    这是产品负责人 2026-08-06 裁定③的口径，与 TO PM 的"可前滚可回滚"承诺的差距
    写在 migrations/README.md：**基线之后的每条 revision 都必须可真往返**，
    基线本身不可逆。

    理由不是"实现麻烦"，而是基线的 downgrade 没有真实场景——它等于把整个生产库
    的全部表删掉。留一个能跑的 downgrade 只会让某次手滑的 `alembic downgrade base`
    真的执行成功。这里 raise，`alembic` 以非零退出。
    """

    raise NotImplementedError(
        "基线 revision 20260806_baseline 不支持 downgrade："
        "它对应整个生产表结构，回退等于删除全部业务表。"
        "需要重建空库时直接新建数据库，不要用 downgrade。"
    )
