"""通报补数：task 表新增 token 用量与守卫拒绝计数落库列。

Revision ID: 0070_task_report_metrics
Revises: 0069_innertest_content_capture
Create Date: 2026-08-25

Issue #303/#304 批次 4。`core/daily_report.py` 模块文档记录过一个结构性缺口：
token 用量与 PreToolUse 拒绝计数只存在于 worker 进程自己的结构化日志行
``worker.task.terminal``（``resources.usage``/``audit.denied_count``，见
``apps/worker/report.py``），scheduler 与 worker 是两个独立部署的进程、不共享
文件系统或日志聚合通道，daily_report 因此把这两段恒判「不可判定」。本迁移新增
两列，供 ``apps/worker/service.py`` 的终态收口点（``_finish_terminal`` →
``PostgresTaskQueue.write_terminal_event``）同事务落库，脱离「不可判定」。

**形状为什么这样定**：

- ``token_usage``（``JSONB``，可空）——直接对应 ``apps/worker/report.py`` 里
  ``resources["usage"]``/``audit["usage"]`` 的真实产出形状（同一个对象，两处
  引用）；那个对象由 ``core/execution/message_stream.py`` 的 ``_usage_summary``
  构造，恒为 ``{"status": "known"|"unknown", "source": ..., "fields": {...}}``
  （``status="known"`` 时才有 ``fields``，四个已知 token 计数字段：
  ``input_tokens``/``output_tokens``/``cache_creation_input_tokens``/
  ``cache_read_input_tokens``，取到几个算几个）。本列**只落 ``fields`` 那部分**
  （``status="known"`` 时），不落整个信封——``status``/``source`` 是"这次能不能
  取到"的元信息，落库后仍然只需要回答"取到了什么"；能不能取到已经由列本身是否
  ``NULL`` 表达，不需要重复编码。``status="unknown"`` 时列写 ``NULL``。
- ``guard_denied_count``（``INT``，可空）——直接对应
  ``report["audit"]["denied_count"]``（``len(summary.denied_calls)``），一个
  单纯的非负整数，不需要复合结构。

**两列都允许 ``NULL``，且 ``NULL`` 是精确语义、不是"暂时留空以后补"**：
``apps/worker/service.py`` 的早退分支（开工前已 ``stop_requested``、读用户
MCP 配置失败、执行器抛出未预期异常）从未真正跑过一次回合，``report`` 不带
``audit``/``resources`` 字段，这两个值在那些分支下**结构性地取不到**——写
``NULL`` 如实反映"取不到"，不编造 0 或空对象；`core/daily_report.py` 的聚合
逻辑据此把 ``NULL`` 行计入"不可判定"而不是静默当成零（沿用模块文档「逐段
不可判定」纪律，见该文件与 ``adapters/postgres_daily_report.py`` 的改动）。

**不需要回填历史行**：本 revision 之前产生的全部 ``task`` 行在这两列上原本就
没有任何可靠来源——回填成 0 或猜测值是编造数据，回填成 NULL 就是默认值本身，
不需要一条 ``UPDATE``。这与迁移 ``0062``（``onboarding_dispatched_at`` 需要回填
才能不误伤历史行）是两种不同的场景：那里回填是为了不产生错误的重新触发，这里
"没有回填"本身就是唯一诚实的历史状态。

``downgrade()`` 真实可执行：两列都是本 revision 新增，直接 ``DROP COLUMN``。
"""

from __future__ import annotations

from alembic import op

revision: str = "0070_task_report_metrics"
down_revision: str | None = "0069_innertest_content_capture"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
ALTER TABLE task ADD COLUMN token_usage JSONB;
ALTER TABLE task ADD COLUMN guard_denied_count INT;
"""

_DOWNGRADE_SQL = r"""
ALTER TABLE task DROP COLUMN IF EXISTS guard_denied_count;
ALTER TABLE task DROP COLUMN IF EXISTS token_usage;
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与 0057/0058/0059/0060/0061/0062 同型：不走 ``op.execute()``，避免空参数集触发插值模式。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
