-- #17 S3：银河 sys_user_datacountry / sys_country 的落库结构（公司范围链路）。
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
    region_name      TEXT,
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
