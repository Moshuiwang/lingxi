"""文档投递请求：问数结果需要生成飞书文档时的持久化检查点。

Revision ID: 0074_task_document_delivery
Revises: 0071_daily_report_watermark
Create Date: 2026-08-27

Issue #341 S-ES-3（依据 #341 评论 5434520679 审定设计）。S-ES-2（``apps/worker/
turn.py``/``apps/worker/report.py``）已经把"这次问数是否需要交付一份文档"落成
报告契约里的 ``document_request`` 字段（``None`` 或 ``{"title", "paragraphs"}``）；
本迁移建的这张表是那个字段与真正的飞书 docx 交付（S-ES-1
``adapters/feishu_docx_delivery.py``）之间的持久化桥梁——worker 侧终态事务插入一行
``pending``，gateway 侧独立消费循环（``apps/gateway/document_delivery.py``）认领并
驱动"建档 → 写正文 → 授权 → 读回确认"四步。

**幂等键即业务实体本身**：``task_id`` 加 ``UNIQUE``——一次问数至多产生一份文档，
不需要再建一个 ULID 主键去回答"这是哪一次问数的文档请求"（``id`` 仍然单独存在，
是 gateway 消费循环认领/更新时的操作句柄，与 ``task_id`` 服务不同的读——同一形状
见 ``daily_report_watermark`` 的复合主键，或本表这种"业务列 UNIQUE + 操作句柄
主键"变体）。

**``document_id`` 是本表存在的核心原因（检查点列）**：飞书建文档
（``LarkDocxDelivery.create_document``）一旦调用成功、拿到 ``document_id``，这个
动作**不可安全重放**——没有幂等键，重放只会在飞书侧多建一篇孤儿文档。gateway
消费循环因此必须在建档成功的**那一刻**、与后续"写正文/授权/读回"三步分开、单独
提交这一列（见 ``adapters/postgres_document_delivery.py`` 的
``mark_document_created``），崩溃重启后的续做逻辑靠读这一列是否非空来判断"这一步
是否已经做过"，绝不二次 ``create_document``。

**``status`` 五取值与自洽 CHECK**：与迁移 0068（``pending_action``）同一纪律——
``(status = 'succeeded') = (permission_confirmed_at IS NOT NULL)``，数据库层直接
挡住"标成功却没有权限读回确认时间"或"有确认时间却不是成功状态"这两种自相矛盾的行，
不依赖应用层每次都记得同步写两个字段。``uncertain``（结果不明：网络类异常、或
``read_members`` 读回不含目标 open_id 或权限档位不是 ``full_access``）是独立终态，
不与 ``failed``（飞书明确拒绝）合并——V-交付-03：未确认成功的结果不明**不自动
重发**，只有明确失败才会被消费循环判定为"这条请求已经尽力过了"。

**``attempts``/``last_error``**：``attempts`` 由 gateway 侧认领时原子自增
（``UPDATE ... SET status='processing', attempts=attempts+1 ... WHERE
status='pending'``），供崩溃恢复的重试上限判断（应用层常量，当前 5 次，见
``adapters/postgres_document_delivery.py``）；``last_error`` 只存错误类名/错误码，
不落正文、不落凭据。

**``requester_open_id`` 不是外键**：它是这次问数发起用户的飞书 ``open_id``——
建表时 worker 侧终态事务需要的是"当时的收件人是谁"这一份不可变快照，而不是随
``app_user`` 后续变化（例如账号停用、``open_id`` 理论上不会变但设计上不应该假设
它必然存在一条可 JOIN 的活跃行）联动的引用；与 ``pending_action.target_open_id``
（迁移 0068）同一取舍。

``downgrade()`` 真实可执行：本表是本 revision 新建，直接整表删除，不存在需要
还原的历史行。

---

**原地修订（opus 审查 R-S/#341 review round，P1-3）**：本 revision 在提交这次修订
时**尚未在任何环境执行过**（未合并进 main、未部署），因此按仓库既有纪律（迁移
只在真正落过地之后才只能"新增 revision 修正"，落地前发现的问题原地改这个文件
本身更安全，不留一段从未被任何库真正跑过的历史形态）直接原地改写，不新增
revision。补两件事：

1. **``content_expires_at`` 列 + 到期触发器**（对齐 ``V-投递-06``：待投递/失败/
   结果不明的正文最迟 24 小时清除，与 ``task_delivery_event``——迁移 0059——
   同一条产品口径与同一个 24 小时窗口）。形状照 0059
   ``task_delivery_event_fix_expiry()``/0066
   ``onboarding_completion_notice_fix_expiry()``：``BEFORE INSERT`` 触发器把
   ``content_expires_at`` 锁定为 ``created_at + 24 小时``，应用层传什么都会被
   覆盖。**只在 ``INSERT`` 上触发，不覆盖 ``UPDATE``**——与 0059/0066 的
   ``BEFORE INSERT OR UPDATE``（那两张表需要额外挡住"通过 UPDATE 悄悄改写
   created_at/幂等键"）不同，是刻意收窄：本表全部应用层 ``UPDATE`` 语句（认领、
   四个 ``mark_*``、两条回收/死信扫描）都不触碰 ``title``/``paragraphs``/
   ``created_at``/``content_expires_at`` 本身，没有需要在 ``UPDATE`` 路径上
   额外防御的攻击面，加一层不服务任何真实调用点的触发器分支只会增加维护成本。
   到期擦除职责见 ``adapters/postgres_document_delivery.py`` 的
   ``redact_expired_content``（正文擦空、行保留运行事实——``status``/
   ``attempts``/``document_id``/时间戳原样留下，形状照 0064 ``publish_outbox``
   的 ``redact_expired_payloads``：那张表擦 ``payload`` 成 ``'{}'::jsonb``，
   这里擦 ``title``/``paragraphs`` 成空字符串/空数组），由
   ``apps/scheduler/document_delivery_dead_letter.py`` 的定时职责调用（Issue
   #341 R-2，与死信扫描共用同一个轻量周期职责，不为一次 ``UPDATE`` 语句单开
   一整个职责）。
2. **``title``/``paragraphs`` 形状 CHECK**（P3 顺手）：真实内容态要求非空标题
   与非空段落数组；擦除态要求两者都为空——**两者都合法，之间没有第三态**，
   数据库层直接挡住"标题空了但段落还在"或"两者都非空却是一个空数组"这类
   自相矛盾的行，不依赖应用层每次都记得同步擦两个字段。
3. **``notified_at`` 列**（P2-2，opus 审查）：成功建档、写正文、授权并确认读回
   （``status='succeeded'``）之后，"文档已就绪"这条追加消息可能因为一次瞬时的
   出站故障没有真正发出去——此前这一步失败只落日志/告警，没有任何机制补发，
   用户可能永远收不到这条通知，即使文档本身已经建好、他也已经拿到了可管理
   权限。``notified_at`` 只在通知**真正确认送达**（``FeishuUserMessages.
   send_text`` 未抛异常）时置位；``NULL`` 即"还没确认送达"。gateway 侧独立
   消费循环每轮**优先**扫描 ``status='succeeded' AND notified_at IS NULL`` 且
   已经过了退避窗口（``NOTIFY_RETRY_AFTER`` = 10 分钟，见
   ``adapters/postgres_document_delivery.py``）的行补发，不放在 scheduler——
   补发本质是"重放一次 gateway 侧已有的出站信道调用"，scheduler 没有面向单个
   用户的出站信道。
"""

from __future__ import annotations

from alembic import op

revision: str = "0074_task_document_delivery"
down_revision: str | None = "0071_daily_report_watermark"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
CREATE TABLE task_document_delivery_request (
    id                      TEXT        PRIMARY KEY,            -- ULID, tdd_*

    -- 幂等键：一次问数至多一份文档（见文件头部）。
    task_id                 TEXT        NOT NULL UNIQUE REFERENCES task(id),

    -- 这次问数发起用户的飞书 open_id 快照，是 full_access 的授予目标；不设外键，
    -- 理由见文件头部「requester_open_id 不是外键」。
    requester_open_id       TEXT        NOT NULL
        CHECK (NULLIF(BTRIM(requester_open_id), '') IS NOT NULL),

    title                   TEXT        NOT NULL,
    paragraphs              JSONB       NOT NULL,

    status                  TEXT        NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'succeeded', 'uncertain', 'failed')),

    -- 检查点列：非空即"建档已成功、不得二次 create_document"，见文件头部。
    document_id             TEXT,

    -- read_members 读回确认目标 open_id 具备 full_access 的时点；只有 succeeded
    -- 才有意义，见下方自洽 CHECK。
    permission_confirmed_at TIMESTAMPTZ,

    attempts                INT         NOT NULL DEFAULT 0,
    last_error              TEXT,

    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    content_expires_at      TIMESTAMPTZ NOT NULL,               -- 触发器固定为 created_at + 24 小时

    -- "文档已就绪"追加消息确认送达的时点（P2-2）；NULL 即还没确认送达，
    -- gateway 消费循环据此判断要不要补发，见文件头部「原地修订」第 3 条。
    notified_at             TIMESTAMPTZ,

    -- 与迁移 0068 同一纪律：两种自相矛盾的行（标成功却没有确认时间／有确认时间
    -- 却不是成功状态）都在数据库层面直接拒绝，见文件头部。
    CHECK ((status = 'succeeded') = (permission_confirmed_at IS NOT NULL)),

    -- P1-3 顺手（P3）：真实内容态（非空标题 + 非空段落数组）与到期擦除后的标记态
    -- （两者都为空）都合法，之间没有第三态——见文件头部「原地修订」第 2 条。
    CHECK (
        (title <> '' AND jsonb_typeof(paragraphs) = 'array' AND jsonb_array_length(paragraphs) > 0)
        OR (title = '' AND paragraphs = '[]'::jsonb)
    )
);

-- gateway 消费循环的认领查询谓词（status='pending'，按 created_at 排队）；部分
-- 索引只覆盖 pending 行，索引大小只随积压量增长，不随历史成功/失败行退化——
-- 与 0057 的 task_queue_idx 同一手法。
CREATE INDEX task_document_delivery_request_pending_idx
    ON task_document_delivery_request (created_at)
    WHERE status = 'pending';

-- 崩溃恢复的"卡在 processing 太久"回收扫描谓词（见
-- adapters/postgres_document_delivery.py 的 reclaim_stale_processing）。
CREATE INDEX task_document_delivery_request_processing_idx
    ON task_document_delivery_request (updated_at)
    WHERE status = 'processing';

-- V-投递-06 到期擦除的扫描谓词（见 adapters/postgres_document_delivery.py 的
-- redact_expired_content）：只覆盖尚未擦除的行（title <> ''），已擦除的行永远
-- 不会再次命中这条查询，索引大小只随"尚未到期"的积压量增长。
CREATE INDEX task_document_delivery_request_content_purge_idx
    ON task_document_delivery_request (content_expires_at)
    WHERE title <> '';

-- P2-2：gateway 消费循环补发"文档已就绪"通知的扫描谓词（见
-- adapters/postgres_document_delivery.py 的 claim_unnotified_succeeded）：只覆盖
-- 已经成功但还没确认通知送达的行，一旦补发确认成功（notified_at 非空）就永久
-- 退出这条索引，索引大小只随"待补发"的积压量增长。
CREATE INDEX task_document_delivery_request_unnotified_idx
    ON task_document_delivery_request (updated_at)
    WHERE status = 'succeeded' AND notified_at IS NULL;

-- 到期时间由来源时间推导，与 0059/0066 同型：应用层传什么都会被覆盖。只在
-- INSERT 上触发的理由见文件头部「原地修订」第 1 条。
CREATE OR REPLACE FUNCTION task_document_delivery_request_fix_expiry() RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.content_expires_at := NEW.created_at + INTERVAL '24 hours';
    RETURN NEW;
END;
$$;

CREATE TRIGGER task_document_delivery_request_expiry
    BEFORE INSERT ON task_document_delivery_request
    FOR EACH ROW EXECUTE FUNCTION task_document_delivery_request_fix_expiry();
"""

_DOWNGRADE_SQL = r"""
DROP TABLE IF EXISTS task_document_delivery_request;
DROP FUNCTION IF EXISTS task_document_delivery_request_fix_expiry();
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与 0057–0071 同型：不走 ``op.execute()``，避免空参数集触发插值模式。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
