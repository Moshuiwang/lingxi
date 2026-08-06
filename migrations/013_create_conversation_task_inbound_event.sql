-- S4 前半（Issue #57）：会话、任务队列与飞书入站事件幂等三张表。
--
-- 蓝本是 docs/技术设计/数据库设计.md 第五节，本文件在蓝本之上补了三处，逐条写明理由：
--
--   1. task.target_worker_version：#45 灰度决策提前到 S4 的两项预留之一。字段名不叫
--      target_version，是为了与 pending_action.target_version（权限版本号，语义完全
--      不同）区分——架构设计「灰度发布」一节的既有命名要求。
--   2. 三个 BEFORE 触发器：把「入队时固化的版本不可改写」「保留期由来源时间推导」
--      做成数据库约束，而不是只靠应用层自觉。理由见每个触发器上方。
--   3. task_queue_idx 把 target_worker_version 放进索引键：领取 SQL 带版本条件
--      （#45），不进索引的话按版本领取会退化成全 queued 扫描。
--
-- 依赖 008（app_user）。conversation.running_task_id 刻意不设外键：它与 task.
-- conversation_id 构成环，任一方向的外键都会让「先插任务再抢占」或反之其中一种
-- 顺序无法在单事务内完成，而抢占与入队必须同事务（V-队列-01）。

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
-- 二、task：任务队列
-- ---------------------------------------------------------------------------

CREATE TABLE task (
    id                    TEXT PRIMARY KEY,                  -- ULID, tsk_*
    conversation_id       TEXT NOT NULL REFERENCES conversation(id),
    user_id               TEXT NOT NULL REFERENCES app_user(id),
    -- 溯源到飞书事件。**刻意不设外键**：inbound_event 的九十天保留清理（#54）与
    -- task 的内容保留期是两条独立的清理链，外键会让其中一条的删除顺序依赖另一条。
    inbound_event_id      TEXT NOT NULL,
    prompt                TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','running','succeeded','failed','stopped')),
    resumed_session       BOOLEAN NOT NULL DEFAULT FALSE,    -- 入队时判定，排队时长不改变它
    -- #45 灰度预留。**刻意不给默认值**：有默认值时「忘记求值」会静默落成 stable，
    -- 而 V-灰度-01 要求的是 gateway 在入队时显式求值一次并写入。没有默认值时漏写
    -- 直接 NOT NULL 违约，是一个响亮的失败。
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
-- content_expires_at 同理由来源时间推导，调用方不能自定义，也不能后移。
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
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER task_freeze
    BEFORE INSERT OR UPDATE ON task
    FOR EACH ROW EXECUTE FUNCTION task_freeze_invariants();

-- ---------------------------------------------------------------------------
-- 三、inbound_event：飞书事件幂等
-- ---------------------------------------------------------------------------

-- feishu_event_id 是**单列主键**，保证同一事件在保留窗口内重投时都发生冲突。
-- 不能把 received_at 放进主键，否则每次重投都因接收时间不同而绕过去重。
CREATE TABLE inbound_event (
    feishu_event_id  TEXT        PRIMARY KEY,
    received_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_type       TEXT        NOT NULL,
    user_open_id     TEXT,
    handled_as       TEXT
        CHECK (handled_as IS NULL OR handled_as IN
               ('task_queued','busy_hint','auto_provisioning','command','dropped')),
    trace_id         TEXT        NOT NULL,
    expires_at       TIMESTAMPTZ NOT NULL                    -- 触发器固定为 received_at + 2160 小时
);

CREATE INDEX inbound_event_received_at_idx ON inbound_event (received_at);

-- 九十天保留期由来源时间推导，调用方不能自定义，也不能后移（数据保留合同：
-- 事件里含 user_open_id，属可识别内容，保留期不得被写入方悄悄延长）。
-- 写法参照 007 的 feishu_org_sync_run_fix_expiry；清理机制本身由 #54 交付。
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
