-- #16 S2：「四达文档会议助手」专用组织资料授权的**主体登记表**。
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
