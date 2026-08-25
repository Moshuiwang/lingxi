"""内测每日通报的统计读取（Issue #303 S-O-01；`guard_denied_count_stats`/
`token_usage_stats` 两个方法为 Issue #304 批次 4 新增）。**本模块只读，一个写
语句都没有。**

六个方法各对应 `core/daily_report.py` 里独立的一段统计，刻意分开而不是一次查询
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

# 通报补数（Issue #303/#304 批次 4，迁移 0070）：两段哑聚合，只做 COUNT/SUM，
# 不判断"取不到算不算不可判定"——那条判定在 core/daily_report.py 的
# build_denied_count_stats/build_token_usage_stats（模块文档「SQL 只做哑分组」
# 同一条纪律）。`SUM` 对 SQL NULL 天然跳过不计入，`->>'字段名'` 对整行是 NULL
# 或该字段缺失都返回 NULL，因此"覆盖的任务只对有值的部分求和"不需要额外的
# CASE 分支。
_GUARD_DENIED_COUNT_STATS_SQL = """
SELECT
    COUNT(*) FILTER (WHERE guard_denied_count IS NOT NULL) AS covered_tasks,
    COUNT(*) FILTER (WHERE guard_denied_count IS NULL) AS uncovered_tasks,
    COALESCE(SUM(guard_denied_count), 0) AS total
  FROM task
 WHERE created_at >= %(window_start)s AND created_at < %(window_end)s
"""

_TOKEN_USAGE_STATS_SQL = """
SELECT
    COUNT(*) FILTER (WHERE token_usage IS NOT NULL) AS covered_tasks,
    COUNT(*) FILTER (WHERE token_usage IS NULL) AS uncovered_tasks,
    COALESCE(SUM((token_usage->>'input_tokens')::bigint), 0) AS input_tokens,
    COALESCE(SUM((token_usage->>'output_tokens')::bigint), 0) AS output_tokens,
    COALESCE(SUM((token_usage->>'cache_creation_input_tokens')::bigint), 0)
        AS cache_creation_input_tokens,
    COALESCE(SUM((token_usage->>'cache_read_input_tokens')::bigint), 0)
        AS cache_read_input_tokens
  FROM task
 WHERE created_at >= %(window_start)s AND created_at < %(window_end)s
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
        """窗口内投递终态按 `(卡片/文本, 是否已确认送达, 是否已过 24h 到期)` 的分组计数。

        **调用方通常传入与其余三个方法不同的窗口**（opus 批量审查 P2 修复，见
        `core/daily_report.py` 模块文档「投递结果段为什么用一个独立、更早的
        窗口」）：`expires_at = created_at + 24h`，如果这里查的是"昨天"这个刚
        结束不久的窗口，绝大多数行的 24 小时确认期在通报运行时还没关闭，"过期"
        这一桶会结构上恒为零。本方法自己不做任何日期偏移——完全信任调用方传入
        的 `window_start`/`window_end` 就是它想要问的那个窗口，不在这里重新
        计算或假设"应该"是哪一天。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                _DELIVERY_OUTCOME_ROWS_SQL,
                {"window_start": window_start, "window_end": window_end},
            )
            rows = cursor.fetchall()
        return tuple(
            (kind, bool(received), bool(expired), int(count)) for kind, received, expired, count in rows
        )

    def guard_denied_count_stats(self, *, window_start, window_end) -> tuple[int, int, int]:
        """窗口内 ``task.guard_denied_count`` 的哑聚合：``(covered_tasks,
        uncovered_tasks, total)``——分别是"该字段非 NULL 的任务数"「该字段是
        NULL 的任务数」「非 NULL 那些任务的求和」。分类判断（是否整段不可判定）
        留给 ``core/daily_report.py::build_denied_count_stats``。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                _GUARD_DENIED_COUNT_STATS_SQL,
                {"window_start": window_start, "window_end": window_end},
            )
            covered, uncovered, total = cursor.fetchone()
        return int(covered), int(uncovered), int(total)

    def token_usage_stats(
        self, *, window_start, window_end
    ) -> tuple[int, int, int, int, int, int]:
        """窗口内 ``task.token_usage`` 的哑聚合：``(covered_tasks,
        uncovered_tasks, input_tokens, output_tokens,
        cache_creation_input_tokens, cache_read_input_tokens)``。四个 token
        计数各自独立求和（`SUM` 对 SQL NULL 天然跳过，取到几个算几个）；分类
        判断留给 ``core/daily_report.py::build_token_usage_stats``。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                _TOKEN_USAGE_STATS_SQL,
                {"window_start": window_start, "window_end": window_end},
            )
            covered, uncovered, input_tokens, output_tokens, cache_creation, cache_read = (
                cursor.fetchone()
            )
        return (
            int(covered),
            int(uncovered),
            int(input_tokens),
            int(output_tokens),
            int(cache_creation),
            int(cache_read),
        )
