"""内测每日通报的统计读取（Issue #303 S-O-01）。**本模块只读，一个写语句都没有。**

四个方法各对应 `core/daily_report.py` 里独立的一段统计，刻意分开而不是一次查询
返回全部——这样`apps/scheduler/daily_report.py` 才能在其中任意一段查询失败时，
只把那一段标记「不可判定」，其余段落照常渲染（#303 的「不可判定」显式呈现要求）。

SQL 只做**哑分组**（`GROUP BY` 若干列后 `COUNT(*)`），不做任何业务分类判断——
「哪个原因码算超时」「哪个算护栏触发」这类规则全部留在 `core/daily_report.py` 的
纯函数里，可以脱离数据库单测，也避免同一条分类规则在 SQL 与 Python 里各写一份、
迟早漂移。

**`active_user_task_counts` 从不把 `user_id` 取回调用方**：`GROUP BY user_id` 只用于
计数，`SELECT` 列表里没有它——这是「用户标识不进管理群正文」这条约束在类型层面的
第一道防线，见 `core/daily_report.py` 模块文档「用户标识为什么不出现在正文里」一节。
"""

from __future__ import annotations

import logging

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.daily_report import DeliveryOutcomeRow, TaskOutcomeRow

logger = logging.getLogger(__name__)

_ACTIVE_USER_TASK_COUNTS_SQL = """
SELECT COUNT(*)
  FROM task
 WHERE created_at >= %(window_start)s AND created_at < %(window_end)s
 GROUP BY user_id
"""

_TASK_OUTCOME_ROWS_SQL = """
SELECT status, error_kind, COUNT(*)
  FROM task
 WHERE created_at >= %(window_start)s AND created_at < %(window_end)s
 GROUP BY status, error_kind
"""

_TASK_DURATIONS_SQL = """
SELECT EXTRACT(EPOCH FROM (ended_at - started_at))
  FROM task
 WHERE created_at >= %(window_start)s AND created_at < %(window_end)s
   AND started_at IS NOT NULL AND ended_at IS NOT NULL
"""

_DELIVERY_OUTCOME_ROWS_SQL = """
SELECT platform_message_kind,
       (platform_received_at IS NOT NULL) AS received,
       (platform_received_at IS NULL AND expires_at < now()) AS expired,
       COUNT(*)
  FROM task_delivery_event
 WHERE event_type = 'terminal'
   AND created_at >= %(window_start)s AND created_at < %(window_end)s
 GROUP BY 1, 2, 3
"""


class PostgresDailyReportSource:
    """每日通报四段真实数据的读取口。构造时不连接数据库，每次调用自带连接。"""

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        self._dsn = dsn
        self._timeouts = timeouts

    def active_user_task_counts(self, *, window_start, window_end) -> tuple[int, ...]:
        """窗口内每个活跃用户的任务数——**只有计数，不含 user_id**。"""

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                _ACTIVE_USER_TASK_COUNTS_SQL,
                {"window_start": window_start, "window_end": window_end},
            )
            rows = cursor.fetchall()
        counts = tuple(int(row[0]) for row in rows)
        logger.info("每日通报：活跃用户任务量已读取 活跃用户数=%s", len(counts))
        return counts

    def task_outcomes(self, *, window_start, window_end) -> tuple[TaskOutcomeRow, ...]:
        """窗口内按 `(status, error_kind)` 分组的任务计数——哑分组，不做分类判断。"""

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                _TASK_OUTCOME_ROWS_SQL,
                {"window_start": window_start, "window_end": window_end},
            )
            rows = cursor.fetchall()
        return tuple((str(status), error_kind, int(count)) for status, error_kind, count in rows)

    def task_durations_seconds(self, *, window_start, window_end) -> tuple[float, ...]:
        """窗口内已完成任务的 Agent 执行耗时样本（秒），只统计已经有始末时间的行。"""

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                _TASK_DURATIONS_SQL,
                {"window_start": window_start, "window_end": window_end},
            )
            rows = cursor.fetchall()
        return tuple(float(row[0]) for row in rows if row[0] is not None)

    def delivery_outcomes(self, *, window_start, window_end) -> tuple[DeliveryOutcomeRow, ...]:
        """窗口内投递终态按 `(卡片/文本, 是否已确认送达, 是否已过 24h 到期)` 的分组计数。"""

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                _DELIVERY_OUTCOME_ROWS_SQL,
                {"window_start": window_start, "window_end": window_end},
            )
            rows = cursor.fetchall()
        return tuple(
            (kind, bool(received), bool(expired), int(count)) for kind, received, expired, count in rows
        )
