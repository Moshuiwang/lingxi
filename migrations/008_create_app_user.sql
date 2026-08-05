-- #16 S2：统一用户记录（`app_user`）的正式表，身份部分。
--
-- **本迁移替代测试资产 migrations/001_create_app_user.sql。**
-- 001 服务于已被决策排除的浏览器 OAuth 授权路径（2026-07-28 决策：普通员工
-- 不经过授权卡片或浏览器 OAuth），它的 `authorizing` 状态和
-- `record_authorized_identity()` 函数在正式路径上没有对应动作。001 仍留在仓库里
-- 供既有受控测试使用，由验收人在 PR 阶段决定废弃时机。
--
-- **依赖 migrations/006**：本迁移的触发器要读 feishu_delegated_credential，
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
    IF to_regclass('public.feishu_delegated_credential') IS NULL THEN
        RAISE EXCEPTION '008 依赖 006_create_feishu_delegated_credential.sql，请先执行 006';
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
            AND NULLIF(BTRIM(tenant_key), '') IS NULL
        ) OR (
            NULLIF(BTRIM(feishu_open_id), '') IS NOT NULL
            AND NULLIF(BTRIM(feishu_user_id), '') IS NOT NULL
            AND NULLIF(BTRIM(feishu_union_id), '') IS NOT NULL
            AND NULLIF(BTRIM(display_name), '') IS NOT NULL
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
    IF NEW.feishu_open_id IS NOT NULL
       AND EXISTS (
           SELECT 1 FROM feishu_delegated_credential
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
