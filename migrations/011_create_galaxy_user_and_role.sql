-- #17 S3：银河 user / user_role / role_menu 三张表的落库结构（职能范围链路）。
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
