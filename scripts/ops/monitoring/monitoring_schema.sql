-- 监控库 DDL（S-RC20-410，Issue #410；产品负责人 2026-08-29 落点裁定：不新建独立
-- Supabase 专用实例，改落"测试实例内独立 schema"——监控 schema 与业务库不同实例
-- 天然解耦，测试与生产宿主的监控数据都推到这一份，零新增成本）。
--
-- 由具备建库权限的连接（Supabase 项目的 `postgres` 管理角色，不是下面新建的最小
-- 权限应用角色）手动执行一次；之后的读写全部经 `lingxi_monitoring_app` 这一个
-- 最小权限角色进行——脚本（`push_to_monitoring.sh`）与 AI 巡检会话共用同一份
-- 凭据，不复用业务库凭据（issue 开工事实「新增凭据」一条）。
--
-- 用法：
--   psql "<Supabase 项目管理连接串>" -v password="<新角色的强随机密码，编排者线下生成>" \
--        -f scripts/ops/monitoring/monitoring_schema.sql
-- 密码只经 psql `-v` 变量传入，本文件不含任何明文凭据；重复执行是安全的（幂等）
-- ——`CREATE SCHEMA/TABLE IF NOT EXISTS`、`\gexec` 前置 `WHERE NOT EXISTS` 判断
-- 角色是否已存在、`GRANT` 本身可重复执行。
--
-- 三张表的设计意图：
--   1. sample         —— 原始样本，`resource_sample.sh`/`db_business_sample.sh`
--      产出的每一行本机 JSON 原样落一行；层/时间戳/主机/JSONB payload 四要素，
--      不对 payload 内部结构做约束（下游按需用 jsonb 操作符查询，样本内部字段
--      随脚本迭代变化不需要跟着改表结构）。30 天留存窗口与本机文件源头兜底同一
--      时间口径,清理由后续运维批次接入,本文件只建结构。
--   2. daily_summary  —— 每日聚合摘要，长期保留（超过 30 天原始样本窗口后仍可
--      查询趋势）。本批不含自动写入该表的作业——issue 阶段一范围是"先手动周期
--      跑校准口径,稳定后再定时化",写入方是后续的定时聚合任务或 AI 巡检产出,
--      这里只落结构,留空表不算未完成。
--   3. patrol_record  —— AI 每日巡检记录（`deploy/监控告警.md`「AI 巡检」一节的
--      格式约定），巡检会话用同一个最小权限角色写入。
CREATE SCHEMA IF NOT EXISTS lingxi_monitoring;

CREATE TABLE IF NOT EXISTS lingxi_monitoring.sample (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- 'resource'：resource_sample.sh；'db_business'：db_business_sample.sh
    -- （数据库层与业务层合并成一个 layer 标签，理由见该脚本头注）。留作 TEXT 而
    -- 不是 CHECK 约束固定取值——未来新增层不需要一次迁移改约束，够用即可。
    layer        TEXT NOT NULL,
    host         TEXT NOT NULL,
    -- 样本自带的 UTC 时间戳（脚本 payload 里的 "ts" 字段），不是入库时间；
    -- ingested_at 才是入库时间，两者刻意分开,便于诊断"上推延迟了多久"。
    sampled_at   TIMESTAMPTZ NOT NULL,
    payload      JSONB NOT NULL,
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 幂等上推的唯一防线：push_to_monitoring.sh 靠 ON CONFLICT DO NOTHING 依赖
    -- 这条约束防止 cursor 异常导致的重复推送产生重复行。
    UNIQUE (layer, host, sampled_at)
);

CREATE INDEX IF NOT EXISTS sample_layer_time_idx ON lingxi_monitoring.sample (layer, sampled_at DESC);
CREATE INDEX IF NOT EXISTS sample_host_time_idx ON lingxi_monitoring.sample (host, sampled_at DESC);

CREATE TABLE IF NOT EXISTS lingxi_monitoring.daily_summary (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    layer         TEXT NOT NULL,
    host          TEXT NOT NULL,
    summary_date  DATE NOT NULL,
    summary       JSONB NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (layer, host, summary_date)
);

CREATE INDEX IF NOT EXISTS daily_summary_date_idx ON lingxi_monitoring.daily_summary (summary_date DESC);

CREATE TABLE IF NOT EXISTS lingxi_monitoring.patrol_record (
    id                     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    patrol_date            DATE NOT NULL,
    reviewed_window_start  TIMESTAMPTZ NOT NULL,
    reviewed_window_end    TIMESTAMPTZ NOT NULL,
    -- 巡检结论正文（趋势/异常/疑点），格式与字段约定见
    -- deploy/监控告警.md「AI 巡检」一节；本表只固定"有这么一段文本"，不解析结构。
    findings               TEXT NOT NULL,
    anomaly_found          BOOLEAN NOT NULL DEFAULT FALSE,
    -- 若巡检产出了后续 issue（issue #410「AI 巡检机制」"累计约两周产出代码级
    -- 优化建议并落 issue"），记录编号或 URL；未产出则为 NULL。
    follow_up_issue        TEXT,
    created_by             TEXT NOT NULL DEFAULT 'ai-patrol',
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS patrol_record_date_idx ON lingxi_monitoring.patrol_record (patrol_date DESC);

-- 最小权限应用角色：只对本 schema 有 USAGE + 三张表 SELECT/INSERT + 对应序列
-- USAGE/SELECT（IDENTITY 列的默认值需要非属主角色显式拿到序列权限），不给
-- CREATEDB/CREATEROLE/SUPERUSER/REPLICATION,也不触碰 public schema 或业务库
-- 的任何对象——与业务库凭据完全隔离（issue「监控库使用独立最小权限凭据，不复用
-- 业务库凭据」）。
SELECT 'CREATE ROLE lingxi_monitoring_app LOGIN PASSWORD ' || quote_literal(:'password') ||
       ' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lingxi_monitoring_app')
\gexec

GRANT USAGE ON SCHEMA lingxi_monitoring TO lingxi_monitoring_app;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA lingxi_monitoring TO lingxi_monitoring_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA lingxi_monitoring TO lingxi_monitoring_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA lingxi_monitoring GRANT SELECT, INSERT ON TABLES TO lingxi_monitoring_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA lingxi_monitoring GRANT USAGE, SELECT ON SEQUENCES TO lingxi_monitoring_app;
