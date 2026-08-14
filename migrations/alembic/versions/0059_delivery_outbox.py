"""投递事件 outbox：任务等待投递态、``task_delivery_event`` 与二十四小时到期。

Revision ID: 0059_delivery_outbox
Revises: 0058_worker_execution
Create Date: 2026-08-14

Issue #151。数据库设计[「问数结果投递事件与会话保留 Outbox」](../../../docs/技术设计/数据库设计.md#问数结果投递事件与会话保留-outbox)
冻结的语义在这条 revision 落地：

1. **``task.status`` 新增 ``awaiting_delivery``**——不复用 ``succeeded`` 冒充"等待
   最终投递"（Issue 正文「失败与等待分支」）。这是纯新增取值，不改变旧值语义，
   旧代码继续按原有五个状态工作，属前滚兼容变更，不需要两阶段迁移。
2. **``task_delivery_event``**：每任务按 ``(task_id, sequence)`` 唯一、严格递增
   记录 ``started``/``progress``/``safely_releasable_answer``/``terminal``；
   ``idempotency_key`` 由调用方在应用层生成（含 ``task.attempts`` 前缀，允许重启后
   的新尝试重新写 ``started``，同时让同一次尝试内的网络重试落到同一行，不产生
   第二条事件），不是从 ``task_id``/``sequence`` 派生的生成列——两者服务不同目的：
   ``sequence`` 由数据库在锁定 ``task`` 行后计算，保证顺序；``idempotency_key`` 由
   调用方持有，保证"同一次逻辑写入"的重试不产生第二条**不同**的事件。
3. **冻结"已转入会话保留阶段"标记字段**（数据库设计 :594 要求本 Issue 决定）：
   本迁移把它定为 ``task_delivery_event.platform_received_at``——非空即表示该任务
   的终态已经过飞书权威接口确认收到、不再受 ``expires_at`` 到期判断约束，转为
   随会话保留（触发点见状态合同）。选它而不是新增一个专门的布尔/时间戳列，理由
   是"已送达"与"已转入会话保留阶段"在 #163 方案 B 下是同一件事的两个名字——
   数据库设计原文明确写着"取得 platform_received 后转为随会话保留"，新增一个总是
   与 ``platform_received_at`` 同步非空的独立列只会制造第二个真相来源。未来
   ``chat_message.retention_class='result_delivery'`` 落地同一语义时，应复用
   "有没有对应的已确认送达记录"这一判断方式，而不是各自发明一个同名列。
4. **``expires_at`` 由触发器固定为 ``created_at + 24 小时``**，与 ``0057``/``0058``
   同型：调用方传入值被忽略，``created_at``、``task_id``、``sequence``、
   ``event_type`` 一经写入不可再改——它们是幂等键与顺序的锚点。这个 24 小时上限
   只在 ``platform_received_at IS NULL``（送达前）时被清理与告警读取；一旦确认
   送达，任何读取路径都必须改看 ``platform_received_at`` 而不是这一列
   （数据库设计 :594）。

本迁移不建 ``chat_message``：那张表当前仓库尚未创建（未来 Story 的范围），本 Story
只负责它自己拥有的 Outbox。会话边界清理（``/new``、空闲两小时到点、停用/权限变化
感知）落在 ``src/lingxi/adapters/postgres_conversation.py`` 的应用层方法上，不在
这条迁移里建额外的受限清理函数——``task_delivery_event`` 不在数据库设计第十节的
"仅 INSERT" 高治理表名单里（那份名单目前只有 ``audit_event``/``chat_message``/
``inbound_event``），本迁移因此让 ``lingxi_app`` 按既有 ``task``/``conversation``
同等权限直接读写它；升级到 ``lingxi_retention_owner`` 受限清理函数模式是可预见的
后续加固项，不是本 Story 的完成标准（PR 描述中登记为已知留白）。

``downgrade()`` 真实可执行：``task_delivery_event`` 是本 revision 新建的表，直接
整体删除；``task.status`` 的 CHECK 收窄回五个旧值——若届时仍有
``awaiting_delivery`` 行，收窄会如实失败（与 ``0058`` 的既有先例一致，不做静默
数据修复）。
"""

from __future__ import annotations

from alembic import op

revision: str = "0059_delivery_outbox"
down_revision: str | None = "0058_worker_execution"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
-- ---------------------------------------------------------------------------
-- 一、task.status 新增 awaiting_delivery
-- ---------------------------------------------------------------------------
-- 纯新增取值：旧代码不写它、也不必识别它，前滚兼容，不需要两阶段迁移。
ALTER TABLE task DROP CONSTRAINT task_status_check;
ALTER TABLE task ADD CONSTRAINT task_status_check
    CHECK (status IN ('queued','running','awaiting_delivery','succeeded','failed','stopped'));

-- ---------------------------------------------------------------------------
-- 二、task_delivery_event：投递事件 outbox
-- ---------------------------------------------------------------------------
CREATE TABLE task_delivery_event (
    id                     TEXT        PRIMARY KEY,           -- ULID, tde_*
    task_id                TEXT        NOT NULL REFERENCES task(id),
    sequence               INT         NOT NULL,
    event_type             TEXT        NOT NULL
        CHECK (event_type IN ('started','progress','safely_releasable_answer','terminal')),
    -- 只有 terminal 事件携带业务终态分类；其余事件类型必须为 NULL。
    terminal_kind          TEXT
        CHECK (terminal_kind IS NULL OR terminal_kind IN
               ('success','failed','stopped','redacted_withheld','timeout')),
    -- 与 task.error_kind 同一取值域，terminal 事件写入时镜像一份，便于诊断查询
    -- 不必等待投递解析；不做 CHECK 约束，理由与 task.error_kind 一致（无固定枚举）。
    error_kind             TEXT,
    elapsed_seconds        INT,
    -- 只有 safely_releasable_answer / terminal 允许携带正文；且必须是已经通过
    -- #149 安全检查、允许展示给当前用户的内容，不是原始模型片段（应用层保证，
    -- 数据库层通过下面的 CHECK 至少保证"没有正文的事件类型不可能夹带内容"）。
    content                TEXT,
    -- 只有 terminal_kind='success' 才有意义：任务执行期间可能续用或建立了一个新
    -- 的 Agent SDK 会话，但话题在 awaiting_delivery 期间继续占用（同一话题不会有
    -- 第二个任务插进来读它），因此把它随 terminal 事件一起持久化、留到投递解析
    -- 时才写回 conversation.agent_session_id 是安全的，不需要单独的写入时点。
    -- 到期未送达（delivery_expired）时不使用它：用户没有拿到这次回答，继续沿用
    -- 这个新会话会让模型"记得"一个已经判定不存在的答案（见 confirm_delivery /
    -- expire_undelivered_terminals 的取舍说明）。
    agent_session_id       TEXT,
    worker_id              TEXT        NOT NULL,
    -- 应用层生成，见文件头部第 2 条；不能省略也不能由数据库派生。
    idempotency_key        TEXT        NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 见文件头部第 3 条：非空即"已转入会话保留阶段"的冻结标记。
    platform_received_at   TIMESTAMPTZ,
    platform_message_kind  TEXT
        CHECK (platform_message_kind IS NULL OR platform_message_kind IN ('card','text')),
    platform_message_id    TEXT,
    expires_at              TIMESTAMPTZ NOT NULL,               -- 触发器固定为 created_at + 24 小时

    UNIQUE (task_id, sequence),
    UNIQUE (idempotency_key),
    CHECK (event_type = 'terminal' OR terminal_kind IS NULL),
    CHECK (event_type <> 'terminal' OR terminal_kind IS NOT NULL),
    CHECK (event_type IN ('safely_releasable_answer','terminal') OR content IS NULL),
    CHECK (event_type = 'terminal' OR agent_session_id IS NULL),
    -- platform_received_at 只可能出现在 terminal 行：只有最终形态才谈得上"送达"。
    CHECK (platform_received_at IS NULL OR event_type = 'terminal'),
    CHECK (platform_message_kind IS NULL OR platform_received_at IS NOT NULL),
    CHECK (platform_message_id IS NULL OR platform_received_at IS NOT NULL)
);

CREATE INDEX task_delivery_event_task_idx ON task_delivery_event (task_id, sequence);

-- 二十四小时到期清理只关心"尚未送达的 terminal 行"；服务这一条查询的部分索引，
-- 不随已送达或非终态事件增长而退化。
CREATE INDEX task_delivery_event_pending_expiry_idx
    ON task_delivery_event (expires_at)
    WHERE event_type = 'terminal' AND platform_received_at IS NULL;

-- 与 0057/0058 同型：到期时间由来源时间推导，调用方传什么都会被覆盖；
-- created_at/task_id/sequence/event_type 一经写入不可改——它们是幂等键、顺序
-- 与内容分类的锚点，改写任一项都等于伪造历史。
CREATE OR REPLACE FUNCTION task_delivery_event_fix_expiry() RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.expires_at := NEW.created_at + INTERVAL '24 hours';
    IF TG_OP = 'UPDATE' THEN
        IF NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION '不允许修改投递事件的创建时间';
        END IF;
        IF NEW.task_id IS DISTINCT FROM OLD.task_id THEN
            RAISE EXCEPTION '不允许修改投递事件所属的任务';
        END IF;
        IF NEW.sequence IS DISTINCT FROM OLD.sequence THEN
            RAISE EXCEPTION '不允许修改投递事件的序号';
        END IF;
        IF NEW.event_type IS DISTINCT FROM OLD.event_type THEN
            RAISE EXCEPTION '不允许修改投递事件的类型';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER task_delivery_event_expiry
    BEFORE INSERT OR UPDATE ON task_delivery_event
    FOR EACH ROW EXECUTE FUNCTION task_delivery_event_fix_expiry();
"""


_DOWNGRADE_SQL = r"""
DROP TABLE IF EXISTS task_delivery_event;
DROP FUNCTION IF EXISTS task_delivery_event_fix_expiry();

-- 收窄回旧的五个取值。若此时仍有 awaiting_delivery 行，这一步会如实失败——
-- 与 0058 的既有先例一致：downgrade 不做静默数据修复。
ALTER TABLE task DROP CONSTRAINT IF EXISTS task_status_check;
ALTER TABLE task ADD CONSTRAINT task_status_check
    CHECK (status IN ('queued','running','succeeded','failed','stopped'));
"""


def _execute_verbatim(connection, sql: str) -> None:
    """把整段 SQL 原样交给驱动执行；不能走 ``op.execute()``，理由与既有 revision 相同：
    本段 DDL 的 ``RAISE EXCEPTION`` 里有 ``%`` 占位符，插值模式下会被 psycopg 拒绝。
    """

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
