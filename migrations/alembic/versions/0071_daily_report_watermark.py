"""内测每日通报的送达水位：跨进程重启的持久判重标记。

Revision ID: 0071_daily_report_watermark
Revises: 0070_task_report_metrics
Create Date: 2026-08-26

Issue #325。`apps/scheduler/daily_report.py` 模块文档记录过一个已知残留：
``DailyReportDuty._completed_on``（"今天发过了没有"）只在进程内存里，scheduler
每次重启（部署升级、进程恢复）都会把这个水位清零，同一个统计窗口因此被重新
判定成"还没发"、重新跑一遍聚合与发送——管理群实测坐实：2026-08-25 单日同窗口
收到四条通报，对应当天多次部署重启。本迁移把这个水位从进程内存搬进数据库，
发送**成功**之后写入一行，下一次判重（含重启后的新进程）先查这张表，不再是
一个每次进程启动都归零的字典。

**形状为什么这样定**：

- ``(report_date, chat_id)`` 复合主键，不另建 ULID ``id``——这两列合起来就是
  这张表唯一要回答的问题本身（"这一天、这个目的地，通报发过了没有"），是
  [数据库设计「一、设计原则」第 5 条](../../../docs/技术设计/数据库设计.md#一设计原则)
  里 ``inbound_event.feishu_event_id`` 那类"幂等键例外"的同一形状，不是把
  外部标识误用作业务实体主键。
- 带 ``chat_id`` 而不是只用 ``report_date``：当前生产只有一个
  ``LINGXI_ADMIN_GROUP_CHAT_ID``，但水位如果只按日期判重，运维一旦更换目标群
  （配置轮换），新群会被误判成"今天已经发过"而永远收不到——这张表因此把目的地
  也算进"同一次逻辑投递"的判定里，与 ``adapters/feishu_group_message.py::
  delivery_uuid`` 把 ``chat_id`` 一起哈希进投递去重 ID 的理由相同。
- ``sent_at`` 只做运维排查用（"这一条到底是什么时候发出去的"），不参与判重
  逻辑本身——判重只看行是否存在。

**不需要回填**：全新表，此前不存在任何"已发送"的持久记录可回填；建表前的历史
发送（含已知的重复发送）无法也不需要补一份水位——本迁移只保证**从这一刻起**
恰一次，不重写历史。

**为什么不复用审计表**：调用方（``apps/scheduler/audit.py::AuditSink``）当前
只有一个实现——写结构化日志（``audit_event`` 表本身"未建"，见数据库设计
「二、表清单」）——审计动作只进 stdout，不落库、不可查询，无法承载跨重启判重
这种需要"能回读、能做存在性判断"的语义。

``downgrade()`` 真实可执行：本表是本 revision 新建，直接整表删除，不存在需要
还原的历史行。
"""

from __future__ import annotations

from alembic import op

revision: str = "0071_daily_report_watermark"
down_revision: str | None = "0070_task_report_metrics"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
CREATE TABLE daily_report_watermark (
    report_date DATE        NOT NULL,
    chat_id     TEXT        NOT NULL,
    sent_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (report_date, chat_id)
);
"""

_DOWNGRADE_SQL = r"""
DROP TABLE IF EXISTS daily_report_watermark;
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与 0057/0058/0059/0060/0061/0062/0063/0070 同型：不走 ``op.execute()``，避免
    空参数集触发插值模式。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
