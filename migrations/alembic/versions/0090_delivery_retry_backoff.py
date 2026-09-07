"""投递重试退避落库：让"这个任务现在还不该被再试一次"进得了候选查询的过滤条件。

Revision ID: 0090_delivery_retry_backoff
Revises: 0089_carrier_retention
Create Date: 2026-09-06

[Issue #649](https://github.com/Moshuiwang/lingxi/issues/649)（Trace #643 质量整改批一
IN-06）。Gateway 投递消费循环每轮按 `ORDER BY created_at LIMIT n` 取候选，而"这条任务
的文本兜底刚失败、要等几秒再试"这件事此前只活在消费进程的内存字典里，**候选查询完全
看不见它**。后果是一条持续失败的老任务每轮都排在最前面、被选中、立刻返回"稍后重试"，
既零进展又占掉一个候选名额；批量上限之外的健康用户任务因此永远轮不到处理。

两列都加在 `task` 上，与 `0060` 的四列投递记账同表同性质——描述的是"这个任务的投递
正在被怎么处理"，不是审计事实，因此同样不加冻结触发器：

1. **`delivery_retry_attempts`**：连续没有成交的外发尝试次数，用来算指数退避的档位。
   任何一次"确定知道结果并推进了消费游标"的写入都会把它清零（见
   `adapters/postgres_conversation/_queue_gateway_delivery.py` 的
   `record_delivery_progress`）。
2. **`delivery_retry_after`**：在这个时刻之前不要把这条任务再选进候选。`NULL` ＝ 没有
   待还的退避，是绝大多数任务的常态。候选查询新增 `IS NULL OR <= now()` 这一条过滤。

**为什么落库而不是继续留在内存**：候选选择发生在数据库里，退避状态留在进程里就永远
只能在"已经选中之后"短路，一个名额已经花掉了。落库同时让退避跨进程重启保持有效——
此前重启即清零，一批正在退避的任务会在重启那一刻同时涌回候选。

**不构成重复投递风险**：这两列只影响"什么时候允许再尝试一次"，不放宽任何一道防重复的
闸。外发前预留位（`dispatch_reserved_kind`）与消费游标（`delivery_consumed_sequence`）
仍是唯一决定"能不能再外发一次""能不能确认送达"的依据，本 revision 一个字都没动。

`downgrade()` 真实可执行：两列都是本 revision 新增，直接 `DROP COLUMN`。回滚后正在
退避的任务会立刻重新成为候选，与本 revision 之前的行为一致，不存在需要回填的历史值。
"""

from __future__ import annotations

from alembic import op

revision: str = "0090_delivery_retry_backoff"
down_revision: str | None = "0089_carrier_retention"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
ALTER TABLE task ADD COLUMN delivery_retry_attempts INT NOT NULL DEFAULT 0
    CHECK (delivery_retry_attempts >= 0);
ALTER TABLE task ADD COLUMN delivery_retry_after TIMESTAMPTZ;
"""

_DOWNGRADE_SQL = r"""
ALTER TABLE task DROP COLUMN IF EXISTS delivery_retry_after;
ALTER TABLE task DROP COLUMN IF EXISTS delivery_retry_attempts;
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与 0057/0058/0059/0060 同型：不走 ``op.execute()``，避免空参数集触发插值模式。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
