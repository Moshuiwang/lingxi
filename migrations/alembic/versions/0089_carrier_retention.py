"""四个内容载体的九十天到期清理接线：`pending_action` 补到期列，另三张表补扫描索引。

Revision ID: 0089_carrier_retention
Revises: 0088_outreach_message
Create Date: 2026-09-06

[Issue #648](https://github.com/Moshuiwang/lingxi/issues/648)（Trace #643 质量整改批一
IN-04）。这一条 revision **不新建任何表**，它补的是四个既有载体上"九十天上限执行不
下去"的那一小块：`pending_action` 根本没有到期列，另外三张表有到期列却没有能让扫描
走索引的入口。清理职责本身在 `apps/scheduler/retention.py`，适配器在
`adapters/postgres_carrier_retention.py`。

## 这一批修的是"机制在、执行没有"

产品合同「数据保留与删除」写死了两句：内容连同承载它的**任务问题**、异常处理详情与
**待确认操作参数**最长保留九十天；每份可识别内容自写入起不得晚于九十天删除或不可逆
脱敏。四个载体此前各自缺的东西不同：

* `task.content_expires_at`（承载 `task.prompt`，用户问题原文）——`0057` 的触发器把
  期限写进了列里，全仓没有任何消费者读它。
* `inbound_event.expires_at`（含 `user_open_id`）——同上，`0057` 写入、无人读取。
* `queue_failure_notice.expires_at`——`0058` 写入、无人读取。
* `pending_action`——`0068` 文件尾把这条欠账写得很清楚（"由既有 retention 基础设施在
  未来批次接入"），并预告了届时会是一个**新增的、独立的**保留到期列，不复用那条十分钟
  的 `confirm_deadline_at`。本 revision 就是那个批次。

## 为什么 `pending_action` 是脱敏保留行，不是删整行

`0068` 把"清空整行还是仅脱敏身份列"留成了口子。取脱敏，两条理由：

1. 合同原文是"删除**或**不可逆脱敏"，两条路都兑现承诺，不构成承诺变更；
2. `local_permission_override.pending_action_id` 是 **NOT NULL** 外键（`0072`），删整行
   会把一条现行本地权限覆盖的成立依据一并带走——那是"继续提供服务所需的当前状态"，
   不在九十天删除范围里。

因此新增两列：`retention_expires_at`（`created_at + 2160 小时`，触发器写死，写入方
改不动也后移不了，与全库 `*_expiry` 触发器同型）与 `content_redacted_at`（脱敏水位，
兼作幂等判据与扫描索引的部分条件）。

## 为什么三列 `open_id` 的脱敏值带行标识而不是一个常量

`pending_action_single_pending_target_idx` 是 `target_open_id` 上的**部分唯一索引**
（`WHERE status = 'pending'`）。九十天后仍停在 `pending`（十分钟窗口早过、只是没人点）
的行不止一条时，把它们的 `target_open_id` 全部脱敏成同一个常量会**撞唯一索引**，让
整批清理失败——一条本该静默完成的合规动作会变成每轮都失败的告警。脱敏值取
`'redacted:' || id`：`id` 是内部 ULID、不是可识别内容，值因此逐行唯一、稳定、不可逆
（原 `open_id` 不可能从它算回来）。

`payload` 不置空而是擦成 `'{}'`：`pending_action_payload_matches_action_type`
（`0073`）要求本地权限三类动作**必须**携带非空白 `payload`，置 NULL 或空串会违反
CHECK。擦成 `'{}'` 与 `publish_outbox.payload` 的既有做法（`0064`/
`adapters/postgres_permission_publish.py`）是同一形态：结构还在、内容没了。
`suspend_user`/`resume_user` 两类的 `payload` 本来就必须是 NULL，脱敏时保持 NULL。

## 为什么不进 `0054` 的受限清理函数

`0054` 的 `SECURITY DEFINER` 清理函数与 `BEFORE DELETE` 双条件防线只挂在
`galaxy_import_batch`/`feishu_org_sync_run` 两张父表上，属主是无登录角色
`lingxi_retention_owner`。本批四个载体走应用层小批量语句，与
`mcp_sync_check`/`onboarding_completion_notice`/`innertest_content_capture`
（`V-保留-26`）同一先例：**不动**那个函数、不动属主授权面、不扩大删除权限。

## 索引

三条到期扫描索引，都按"扫描面随清理而缩小"来建：

* `task_content_expiry_idx`：部分索引，条件 `prompt <> ''`。脱敏后的行（`prompt = ''`）
  自动离开索引，因此索引规模只与"还没脱敏的行"相关，不随历史任务总量增长。
* `inbound_event_expiry_idx` / `queue_failure_notice_expiry_idx`：两张表到期即整行删除，
  普通索引随删除自然收缩。`inbound_event` 已有的 `received_at` 索引不够用：清理判据
  写在 `expires_at` 上，让它去猜"`expires_at` 是 `received_at` 的单调函数"是把触发器
  的实现细节焊进查询计划。
* `pending_action_retention_idx`：部分索引，条件 `content_redacted_at IS NULL`。

## 回滚

回滚删掉本 revision 建的两列、一个函数、一个触发器与四条索引。**两列上的信息会丢**：
`content_redacted_at` 消失后，回滚前已经脱敏过的行在重新前滚时会被再脱敏一次——脱敏
是幂等的（内容早已不在），不构成数据破坏。已经被删掉的 `inbound_event` /
`queue_failure_notice` 行不会回来，这与九十天承诺一致：它们本来就到期了。
"""

from __future__ import annotations

from alembic import op

revision: str = "0089_carrier_retention"
down_revision: str | None = "0088_outreach_message"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
-- ---------------------------------------------------------------------------
-- 一、pending_action：补一条独立的九十天保留到期列与脱敏水位
-- ---------------------------------------------------------------------------

-- 先允许 NULL 建列，回填既有行，再收紧成 NOT NULL：这张表在 stage 与生产都已有行，
-- 一步到位的 NOT NULL 会因为没有默认值而直接失败。
ALTER TABLE pending_action ADD COLUMN retention_expires_at TIMESTAMPTZ;
UPDATE pending_action SET retention_expires_at = created_at + INTERVAL '2160 hours'
 WHERE retention_expires_at IS NULL;
ALTER TABLE pending_action ALTER COLUMN retention_expires_at SET NOT NULL;

-- 脱敏水位：非空即"这一行的可识别内容已经不可逆移除"。它同时是幂等判据——重复执行
-- 的清理不会把同一行再算一次，也不会把水位往后挪。
ALTER TABLE pending_action ADD COLUMN content_redacted_at TIMESTAMPTZ;

-- 与 inbound_event/task/queue_failure_notice 的同型触发器一致：期限由来源时间推导，
-- 写入方给什么都会被覆盖，也不能靠改 created_at 把期限往后挪。
CREATE OR REPLACE FUNCTION pending_action_fix_retention_expiry() RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.retention_expires_at := NEW.created_at + INTERVAL '2160 hours';
    IF TG_OP = 'UPDATE' AND NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION '不允许修改待确认操作的创建时间';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER pending_action_retention_expiry
    BEFORE INSERT OR UPDATE ON pending_action
    FOR EACH ROW EXECUTE FUNCTION pending_action_fix_retention_expiry();

-- 部分索引：已脱敏的行离开索引，扫描面只剩"还欠一次脱敏"的行。
CREATE INDEX pending_action_retention_idx
    ON pending_action (retention_expires_at) WHERE content_redacted_at IS NULL;

-- ---------------------------------------------------------------------------
-- 二、三张已有到期列的表：补到期扫描索引
-- ---------------------------------------------------------------------------

-- 脱敏后 prompt = ''，行自动离开索引；索引规模只与未脱敏行相关。
CREATE INDEX task_content_expiry_idx
    ON task (content_expires_at) WHERE prompt <> '';

CREATE INDEX inbound_event_expiry_idx ON inbound_event (expires_at);

CREATE INDEX queue_failure_notice_expiry_idx ON queue_failure_notice (expires_at);
"""

_DOWNGRADE_SQL = r"""
DROP INDEX IF EXISTS queue_failure_notice_expiry_idx;
DROP INDEX IF EXISTS inbound_event_expiry_idx;
DROP INDEX IF EXISTS task_content_expiry_idx;
DROP INDEX IF EXISTS pending_action_retention_idx;
DROP TRIGGER IF EXISTS pending_action_retention_expiry ON pending_action;
DROP FUNCTION IF EXISTS pending_action_fix_retention_expiry();
ALTER TABLE pending_action DROP COLUMN IF EXISTS content_redacted_at;
ALTER TABLE pending_action DROP COLUMN IF EXISTS retention_expires_at;
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与 0057–0088 同型：不走 ``op.execute()``，避免空参数集触发插值模式。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
