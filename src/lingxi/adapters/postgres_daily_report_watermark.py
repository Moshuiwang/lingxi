"""内测每日通报的送达水位：跨进程重启的持久判重标记（Issue #325）。

表结构与逐条理由以迁移 ``0071_daily_report_watermark`` 为准，本模块不复述。这里只
落实两件在**代码里**才成立的事：

1. **判重只看行是否存在**：:meth:`~PostgresDailyReportWatermark.already_sent` 是
   一次按主键的存在性查询，不解释、不校验 ``sent_at``——"这一天这个目的地发过了"
   是一个布尔事实，没有第二个维度。
2. **标记幂等，且绝不覆盖**：:meth:`~PostgresDailyReportWatermark.mark_sent` 用
   ``INSERT ... ON CONFLICT DO NOTHING``，与
   ``adapters/postgres_mcp_token.py::_insert_new_token`` 同一条纪律——同一个
   ``(report_date, chat_id)`` 反复标记只会留下**一行**，不比较、不更新已有行的
   ``sent_at``。这不是性能优化，是正确性要求：`apps/scheduler/daily_report.py::
   DailyReportDuty` 只在**发送成功之后**才调用它，调用点本身已经不会重复触发；
   这里的幂等是最后一道防线——防的是"发送成功与水位写入之间那道极窄缝隙里恰好
   发生进程崩溃重启，下一次启动的重试也会走到同一次 `mark_sent`"这类极端情形，
   不是常规路径（常规路径靠 `already_sent` 提前挡住）。
"""

from __future__ import annotations

import logging
from datetime import date

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect

logger = logging.getLogger(__name__)


def _validate_arguments(report_date: date, chat_id: str) -> None:
    if not isinstance(report_date, date):
        raise TypeError("report_date 必须是 datetime.date")
    if not isinstance(chat_id, str) or not chat_id.strip():
        raise ValueError("chat_id 不能为空")


class PostgresDailyReportWatermark:
    """内测每日通报送达水位的读写。构造时不连接数据库，每次调用自带连接（adapters 既有惯例）。"""

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        self._dsn = dsn
        self._timeouts = timeouts

    def already_sent(self, *, report_date: date, chat_id: str) -> bool:
        """本统计窗口（``report_date`` 对应「今天」）是否已经向 ``chat_id`` 成功
        发送过——只回答存在性，不回读 ``sent_at``。"""

        _validate_arguments(report_date, chat_id)
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM daily_report_watermark WHERE report_date = %s AND chat_id = %s",
                (report_date, chat_id),
            )
            row = cursor.fetchone()
        return row is not None

    def mark_sent(self, *, report_date: date, chat_id: str) -> None:
        """记下"这一天已经向这个目的地发送成功"；已存在即静默跳过，绝不覆盖
        （见模块文档「标记幂等，且绝不覆盖」）。"""

        _validate_arguments(report_date, chat_id)
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO daily_report_watermark (report_date, chat_id)
                        VALUES (%s, %s)
                   ON CONFLICT (report_date, chat_id) DO NOTHING""",
                (report_date, chat_id),
            )
            created = cursor.rowcount == 1
        if created:
            logger.info("内测每日通报送达水位已记录 report_date=%s", report_date.isoformat())
        else:
            # 已经有这一行：不是错误（见模块文档「标记幂等」），但值得留一条低噪声
            # 的观察——正常路径靠 already_sent 提前挡住，真的走到这里说明命中了
            # 文档描述的那道极窄缝隙。
            logger.info(
                "内测每日通报送达水位已存在，本次标记未新增行 report_date=%s",
                report_date.isoformat(),
            )


__all__ = ["PostgresDailyReportWatermark"]
