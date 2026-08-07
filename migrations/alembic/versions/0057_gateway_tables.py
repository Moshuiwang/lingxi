"""会话、任务队列与飞书入站事件幂等三张表

Revision ID: 0057_gateway_tables
Revises: 0054_retention_cleanup
Create Date: 2026-08-06

Issue #57 / 开发计划 S4 前半。蓝本是[数据库设计第五节](../../../docs/技术设计/数据库设计.md)，
在其上有四处差异，每处的理由都写在下面的 DDL 注释里：`task.target_worker_version`
（#45 灰度预留）、两个冻结/保留期触发器、`task_queue_idx` 把版本放进索引键、
`inbound_event.handled_as` 多一个 `not_provisioned`。

**DDL 直接内联在本文件里。** 编号 SQL 自 #53 起冻结：基线 `20260806_baseline` 逐字节
覆盖 `006`–`012`，`scripts/ci/check_alembic_revisions.py` 要求顶层每个编号 SQL 都被某条
revision 的 `CHAIN` 覆盖——新增一个 `013` 不会进入基线，两条血统就此分叉，门禁直接红。
本切片早期确实先写了 `migrations/013_*.sql`，缝链时按这条规则删除，DDL 全部并入本
revision；本 revision 没有 `CHAIN`（它不是任何磁盘文件的副本），逐字节比对不适用。

`downgrade()` 是**真实可执行**的：门禁的逐 revision 真往返会实际跑它。三张表都是本
revision 新建的，因此可以干净地整体删除，不存在"原值已不可知"的回填问题。删除顺序
与建表相反（`task` 引用 `conversation` 与 `app_user`），触发器随表一起消失，两个函数
显式删除。
"""

from __future__ import annotations

from alembic import op

revision: str = "0057_gateway_tables"
down_revision: str | None = "0054_retention_cleanup"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
-- #57 S4 前半：会话、任务队列与飞书入站事件幂等。
--
-- 依赖基线里的 app_user（008 部分）。conversation.running_task_id 与
-- task.inbound_event_id 都**不设外键**：前者与 task.conversation_id 构成环，任一
-- 方向的外键都会让「先插任务再抢占」或反之其中一种顺序无法在单事务内完成，而抢占
-- 与入队必须同事务；后者与入站事件的九十天保留清理（0054）是两条独立的清理链，
-- 外键会让其中一条的删除顺序依赖另一条。

-- ---------------------------------------------------------------------------
-- 一、conversation：私聊主窗口与话题
-- ---------------------------------------------------------------------------

CREATE TABLE conversation (
    id                 TEXT PRIMARY KEY,                     -- ULID, cnv_*
    user_id            TEXT NOT NULL REFERENCES app_user(id),
    feishu_chat_id     TEXT NOT NULL,
    feishu_thread_id   TEXT,                                 -- NULL 表示私聊主窗口
    agent_session_id   TEXT,                                 -- Agent SDK 会话；NULL = 下次不 resume
    last_task_ended_at TIMESTAMPTZ,                          -- 两小时规则的唯一依据
    running_task_id    TEXT,                                 -- 同话题串行；见下方条件更新
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 主窗口与每个话题各自独立：COALESCE 把 NULL（主窗口）折进同一个唯一键，
-- 否则 NULL 在唯一索引里互不相等，同一个人的主窗口能建出任意多行。
CREATE UNIQUE INDEX conversation_scope_uniq
    ON conversation (user_id, feishu_chat_id, COALESCE(feishu_thread_id, ''));

CREATE INDEX conversation_user_idx ON conversation (user_id);

-- 同话题串行**靠条件更新，不靠唯一索引**（数据库设计第五节已实测确认）：
-- 每个话题只有一行，冲突发生在同一行的字段覆盖上，不是行的重复上。
--   抢占：UPDATE conversation SET running_task_id = $task
--          WHERE id = $conv AND running_task_id IS NULL;      -- 影响 0 行 = 忙碌
--   释放：UPDATE conversation SET running_task_id = NULL, last_task_ended_at = now()
--          WHERE id = $conv AND running_task_id = $task;      -- 只有持有者能释放
-- 释放条件里的 running_task_id = $task 不能省：省掉之后任何一个任务都能清掉别人的
-- 占用，两个任务会在同一话题上并行（PR #12 的原始错误）。

-- ---------------------------------------------------------------------------
-- 二、inbound_event：飞书事件幂等
-- ---------------------------------------------------------------------------

-- feishu_event_id 是**单列主键**，保证同一事件在保留窗口内重投时都发生冲突。
-- 不能把 received_at 放进主键，否则每次重投都因接收时间不同而绕过去重。
CREATE TABLE inbound_event (
    feishu_event_id  TEXT        PRIMARY KEY,
    received_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_type       TEXT        NOT NULL,
    user_open_id     TEXT,
    -- 取值域比数据库设计蓝本多一个 not_provisioned：本批**不**启动自动匹配与开通
    -- （正向接线属后续 Issue），把未开通用户的事件记成 auto_provisioning 等于让这
    -- 一列陈述一件没有发生的事；记成 dropped 又会与「已停用」混为一谈，而 0054 的
    -- 保留清理与后续的开通接线都要读这一列。auto_provisioning 保留在取值域里备用。
    handled_as       TEXT
        CHECK (handled_as IS NULL OR handled_as IN
               ('task_queued','busy_hint','not_provisioned','auto_provisioning',
                'command','dropped')),
    trace_id         TEXT        NOT NULL,
    expires_at       TIMESTAMPTZ NOT NULL                    -- 触发器固定为 received_at + 2160 小时
);

CREATE INDEX inbound_event_received_at_idx ON inbound_event (received_at);

-- 九十天保留期由来源时间推导，调用方不能自定义，也不能后移（数据保留合同：
-- 事件里含 user_open_id，属可识别内容，保留期不得被写入方悄悄延长）。
-- 写法与基线里 feishu_org_sync_run 的同型触发器一致。
CREATE OR REPLACE FUNCTION inbound_event_fix_expiry() RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.expires_at := NEW.received_at + INTERVAL '2160 hours';
    IF TG_OP = 'UPDATE' AND NEW.received_at <> OLD.received_at THEN
        RAISE EXCEPTION '不允许修改入站事件的接收时间';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER inbound_event_expiry
    BEFORE INSERT OR UPDATE ON inbound_event
    FOR EACH ROW EXECUTE FUNCTION inbound_event_fix_expiry();

-- ---------------------------------------------------------------------------
-- 三、task：任务队列
-- ---------------------------------------------------------------------------

CREATE TABLE task (
    id                    TEXT PRIMARY KEY,                  -- ULID, tsk_*
    conversation_id       TEXT NOT NULL REFERENCES conversation(id),
    user_id               TEXT NOT NULL REFERENCES app_user(id),
    -- 溯源到飞书事件，不设外键，理由见文件头部。
    inbound_event_id      TEXT NOT NULL,
    prompt                TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','running','succeeded','failed','stopped')),
    resumed_session       BOOLEAN NOT NULL DEFAULT FALSE,    -- 入队时判定，排队时长不改变它
    -- #45 灰度预留。**刻意不给默认值**：有默认值时「忘记求值」会静默落成 stable，
    -- 而断言要求 gateway 在入队时显式求值一次并写入。没有默认值时漏写直接
    -- NOT NULL 违约，是一个响亮的失败。字段名与 pending_action.target_version
    -- （权限版本号，语义完全不同）区分，见架构设计「灰度发布」。
    target_worker_version TEXT NOT NULL
        CHECK (target_worker_version <> ''),
    card_id               TEXT,
    card_seq              INT NOT NULL DEFAULT 0,
    fallback_text         BOOLEAN NOT NULL DEFAULT FALSE,
    worker_id             TEXT,
    heartbeat_at          TIMESTAMPTZ,
    attempts              INT NOT NULL DEFAULT 0,
    scheduled_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at            TIMESTAMPTZ,
    ended_at              TIMESTAMPTZ,
    stop_requested        BOOLEAN NOT NULL DEFAULT FALSE,    -- /stop
    error_kind            TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    content_expires_at    TIMESTAMPTZ NOT NULL               -- 触发器固定为 created_at + 2160 小时
);

-- 领取索引带上版本：按版本领取的 SQL 形如
--   WHERE status = 'queued' AND target_worker_version = $v AND scheduled_at <= now()
-- 部分索引只覆盖 queued 行，索引大小只与积压量相关，不随历史增长退化。
CREATE INDEX task_queue_idx ON task (target_worker_version, scheduled_at) WHERE status = 'queued';
-- scheduler 回收心跳超时的 running 任务。
CREATE INDEX task_stale_idx ON task (heartbeat_at) WHERE status = 'running';
CREATE INDEX task_conversation_idx ON task (conversation_id, created_at DESC);

-- target_worker_version 一经写入不可改写（#45 决策：重试、进程重启、心跳超时回收
-- 都不得改变用户进入的版本）。放在数据库而不是只放在应用层，是因为改写它的路径有
-- 三条（领取、重试、回收），每条都在不同进程里，靠代码审查逐条守住不现实。
-- resumed_session 同理：任务在队列里排多久都不能改变会话归属，否则用户的上下文
-- 归属会由队列长度决定而不是由他自己的发起时间决定。
-- content_expires_at 由来源时间推导，调用方不能自定义，也不能后移。
CREATE OR REPLACE FUNCTION task_freeze_invariants() RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.content_expires_at := NEW.created_at + INTERVAL '2160 hours';
    IF TG_OP = 'UPDATE' THEN
        IF NEW.target_worker_version IS DISTINCT FROM OLD.target_worker_version THEN
            RAISE EXCEPTION '不允许修改任务的目标 worker 版本（任务 %：% → %）',
                NEW.id, OLD.target_worker_version, NEW.target_worker_version;
        END IF;
        IF NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION '不允许修改任务的创建时间';
        END IF;
        IF NEW.resumed_session IS DISTINCT FROM OLD.resumed_session THEN
            RAISE EXCEPTION '不允许修改任务的会话续用判定（任务 %）', NEW.id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER task_freeze
    BEFORE INSERT OR UPDATE ON task
    FOR EACH ROW EXECUTE FUNCTION task_freeze_invariants();
"""


_DOWNGRADE_SQL = r"""
-- 三张表都是本 revision 新建的，可以干净地整体删除：不存在需要还原的历史行，
-- 也没有被本 revision 之外的对象引用。删除顺序与建表相反（task 引用
-- conversation 与 app_user）；触发器随表一起消失，两个函数显式删除。
DROP TABLE IF EXISTS task;
DROP FUNCTION IF EXISTS task_freeze_invariants();
DROP TABLE IF EXISTS inbound_event;
DROP FUNCTION IF EXISTS inbound_event_fix_expiry();
DROP TABLE IF EXISTS conversation;
"""


def _execute_verbatim(connection, sql: str) -> None:
    """把整段 SQL 原样交给驱动执行。

    与基线和 `0054` 用的是同一条路径，理由也相同：不能用 `op.execute()` 或
    `exec_driver_sql()`，两者最终都会把 SQL 连同一个（哪怕是空的）参数集交给
    psycopg，于是 psycopg 进入占位符插值模式。本段 DDL 的 `RAISE EXCEPTION` 里有
    `%` 占位符，插值模式下直接报
    `only '%s', '%b', '%t' are allowed as placeholders`。

    只传 SQL、不传参数时 psycopg 完全跳过插值，`%` 原样送到服务端。
    """

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
