"""`adapters/postgres_daily_report.py` 的真库断言（Issue #303 S-O-01）。

只验证真库才能证伪的部分：窗口边界（`>= window_start AND < window_end`）在真实
`TIMESTAMPTZ` 比较下成立、`GROUP BY` 聚合的哑分组值确实原样透出、`active_user_
task_counts` 确实不把 `user_id` 选进结果集。分类规则（谁算超时、谁算护栏触发、
谁算投递成功/兜底/过期）已经在 `tests/test_daily_report_render.py` 用固定输入
覆盖，这里不重复。
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_daily_report import PostgresDailyReportSource

SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，内测每日通报的真库读取断言未验证"
    if not os.environ.get("LINGXI_POSTGRES_DSN")
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，内测每日通报的真库读取断言未验证"
)


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN") and psycopg_available(), SKIP_REASON)
class DailyReportPostgresTestCase(unittest.TestCase):
    """本文件全部真库用例的共同底座。"""

    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        cls._psycopg = psycopg
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        reset_production_rows(self._dsn)
        self.source = PostgresDailyReportSource(self._dsn)
        self._connection = self._psycopg.connect(self._dsn, autocommit=True)
        self.addCleanup(self._connection.close)
        self.execute(
            """INSERT INTO app_user
               (id, feishu_open_id, feishu_user_id, feishu_union_id,
                display_name, department, tenant_key, provisioning_state)
               VALUES ('usr-1','ou-1','u-1','un-1','张三','数据部','tk-1','active')"""
        )
        self.execute(
            """INSERT INTO app_user
               (id, feishu_open_id, feishu_user_id, feishu_union_id,
                display_name, department, tenant_key, provisioning_state)
               VALUES ('usr-2','ou-2','u-2','un-2','李四','销售部','tk-2','active')"""
        )

    # -- 小工具 -------------------------------------------------------------

    def execute(self, sql: str, parameters: tuple = ()) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(sql, parameters)

    def _seed_conversation(self, conversation_id: str, *, user_id: str = "usr-1") -> None:
        self.execute(
            """INSERT INTO conversation (id, user_id, feishu_chat_id, feishu_thread_id)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (id) DO NOTHING""",
            (conversation_id, user_id, f"chat-{conversation_id}", f"topic-{conversation_id}"),
        )

    def seed_task(
        self,
        *,
        task_id: str,
        user_id: str = "usr-1",
        conversation_id: str | None = None,
        status: str = "succeeded",
        error_kind: str | None = None,
        created_at_sql: str = "now()",
        started_at_sql: str | None = "now()",
        ended_at_sql: str | None = "now()",
        token_usage: dict | None = None,
        guard_denied_count: int | None = None,
    ) -> None:
        """插一条任意状态/时刻的任务，跳过完整入队/领取流程（与
        `test_delivery_outbox.py::seed_running_task` 同一手法）。`created_at_sql`/
        `started_at_sql`/`ended_at_sql` 是受控的 SQL 字面量表达式，不是绑定参数——
        测窗口边界需要精确控制 `created_at` 落在真实 `now()` 的哪一侧。

        `token_usage`/`guard_denied_count`（迁移 0070，Issue #303/#304 批次 4）：
        默认 `None`，与生产默认值一致——模拟"这个任务在这两列上结构性地没有落库
        值"（历史行、或早退分支从未真正跑过一次回合）。
        """

        conversation_id = conversation_id or f"conv-{task_id}"
        self._seed_conversation(conversation_id, user_id=user_id)
        started_sql = "NULL" if started_at_sql is None else started_at_sql
        ended_sql = "NULL" if ended_at_sql is None else ended_at_sql
        from psycopg.types.json import Jsonb

        self.execute(
            f"""
            INSERT INTO task
                (id, conversation_id, user_id, inbound_event_id, prompt, status, error_kind,
                 target_worker_version, attempts, created_at, started_at, ended_at,
                 content_expires_at, token_usage, guard_denied_count)
            VALUES (%s, %s, %s, %s, '问题', %s, %s, 'stable', 1,
                    {created_at_sql}, {started_sql}, {ended_sql}, {created_at_sql}, %s, %s)
            """,
            (
                task_id,
                conversation_id,
                user_id,
                f"event-{task_id}",
                status,
                error_kind,
                Jsonb(token_usage) if token_usage is not None else None,
                guard_denied_count,
            ),
        )

    def seed_delivery_event(
        self,
        *,
        task_id: str,
        sequence: int = 1,
        created_at_sql: str = "now()",
        platform_message_kind: str | None = None,
        platform_received: bool = False,
    ) -> None:
        received_sql = "now()" if platform_received else "NULL"
        self.execute(
            f"""
            INSERT INTO task_delivery_event
                (id, task_id, sequence, event_type, terminal_kind, worker_id,
                 idempotency_key, created_at, platform_received_at, platform_message_kind)
            VALUES (%s, %s, %s, 'terminal', 'success', 'worker-1', %s,
                    {created_at_sql}, {received_sql}, %s)
            """,
            (f"tde-{task_id}-{sequence}", task_id, sequence, f"{task_id}:terminal:{sequence}", platform_message_kind),
        )


class ActiveUserTaskCountsTests(DailyReportPostgresTestCase):
    def test_counts_are_grouped_per_user_and_never_expose_the_user_id_column(self) -> None:
        self.seed_task(task_id="t1", user_id="usr-1")
        self.seed_task(task_id="t2", user_id="usr-1")
        self.seed_task(task_id="t3", user_id="usr-2")

        window_start = datetime.now(timezone.utc) - timedelta(hours=1)
        window_end = datetime.now(timezone.utc) + timedelta(hours=1)
        counts = self.source.active_user_task_counts(window_start=window_start, window_end=window_end)

        self.assertEqual(sorted(counts), [1, 2])
        # 返回类型只可能是整数元组——`user_id` 字面上没有出现在返回值里，
        # 这是「本适配器从不把用户标识取回调用方」的类型层证据。
        self.assertTrue(all(isinstance(count, int) for count in counts))

    def test_the_window_end_is_exclusive(self) -> None:
        far_future_start = datetime.now(timezone.utc) + timedelta(days=365)
        self.seed_task(task_id="t-boundary", created_at_sql="now()")

        counts = self.source.active_user_task_counts(
            window_start=datetime.now(timezone.utc) - timedelta(hours=1), window_end=far_future_start
        )
        self.assertEqual(len(counts), 1)

        counts_after = self.source.active_user_task_counts(
            window_start=far_future_start, window_end=far_future_start + timedelta(hours=1)
        )
        self.assertEqual(counts_after, ())

    def test_tasks_outside_the_window_are_excluded(self) -> None:
        self.seed_task(task_id="t-old", created_at_sql="now() - interval '10 days'")

        window_start = datetime.now(timezone.utc) - timedelta(hours=1)
        window_end = datetime.now(timezone.utc) + timedelta(hours=1)
        counts = self.source.active_user_task_counts(window_start=window_start, window_end=window_end)
        self.assertEqual(counts, ())


class TaskOutcomesTests(DailyReportPostgresTestCase):
    def test_status_and_error_kind_are_returned_as_a_dumb_grouped_count(self) -> None:
        self.seed_task(task_id="t1", status="succeeded", error_kind=None)
        self.seed_task(task_id="t2", status="failed", error_kind="session_failed")
        self.seed_task(task_id="t3", status="failed", error_kind="session_failed")
        self.seed_task(task_id="t4", status="stopped", error_kind="stopped")

        window_start = datetime.now(timezone.utc) - timedelta(hours=1)
        window_end = datetime.now(timezone.utc) + timedelta(hours=1)
        rows = self.source.task_outcomes(window_start=window_start, window_end=window_end)

        self.assertIn(("succeeded", None, 1), rows)
        self.assertIn(("failed", "session_failed", 2), rows)
        self.assertIn(("stopped", "stopped", 1), rows)


class TaskDurationsTests(DailyReportPostgresTestCase):
    def test_only_tasks_with_both_timestamps_produce_a_duration_sample(self) -> None:
        self.seed_task(
            task_id="t-complete",
            started_at_sql="now() - interval '300 seconds'",
            ended_at_sql="now()",
        )
        self.seed_task(task_id="t-still-running", started_at_sql="now()", ended_at_sql=None)
        self.seed_task(task_id="t-never-started", started_at_sql=None, ended_at_sql=None, status="queued")

        window_start = datetime.now(timezone.utc) - timedelta(hours=1)
        window_end = datetime.now(timezone.utc) + timedelta(hours=1)
        durations = self.source.task_durations_seconds(window_start=window_start, window_end=window_end)

        self.assertEqual(len(durations), 1)
        self.assertAlmostEqual(durations[0], 300.0, delta=2.0)


class DeliveryOutcomesTests(DailyReportPostgresTestCase):
    def test_delivered_card_delivered_text_and_pending_are_distinguished(self) -> None:
        self.seed_task(task_id="t-card")
        self.seed_delivery_event(task_id="t-card", platform_message_kind="card", platform_received=True)
        self.seed_task(task_id="t-text")
        self.seed_delivery_event(task_id="t-text", platform_message_kind="text", platform_received=True)
        self.seed_task(task_id="t-pending")
        self.seed_delivery_event(task_id="t-pending", platform_received=False)

        window_start = datetime.now(timezone.utc) - timedelta(hours=1)
        window_end = datetime.now(timezone.utc) + timedelta(hours=1)
        rows = self.source.delivery_outcomes(window_start=window_start, window_end=window_end)

        self.assertIn(("card", True, False, 1), rows)
        self.assertIn(("text", True, False, 1), rows)
        self.assertIn((None, False, False, 1), rows)

    def test_an_undelivered_row_past_its_twenty_four_hour_expiry_is_expired(self) -> None:
        self.seed_task(task_id="t-expired", created_at_sql="now() - interval '25 hours'")
        self.seed_delivery_event(
            task_id="t-expired", created_at_sql="now() - interval '25 hours'", platform_received=False
        )

        # 窗口覆盖那一轮插入的时刻，不覆盖真实「现在」——`expires_at` 由触发器固定为
        # `created_at + 24 小时`，25 小时前插入的行此刻必然已经过期，与窗口边界无关。
        window_start = datetime.now(timezone.utc) - timedelta(hours=26)
        window_end = datetime.now(timezone.utc) - timedelta(hours=24)
        rows = self.source.delivery_outcomes(window_start=window_start, window_end=window_end)

        self.assertIn((None, False, True, 1), rows)


class GuardDeniedCountStatsTests(DailyReportPostgresTestCase):
    """`guard_denied_count_stats`（迁移 0070，Issue #303/#304 批次 4）：真实
    `SUM`/`COUNT(*) FILTER` 在真库上按窗口边界正确聚合，NULL 行只计入
    `uncovered_tasks`、不计入 `total`。"""

    def _window(self) -> tuple[datetime, datetime]:
        return (
            datetime.now(timezone.utc) - timedelta(hours=1),
            datetime.now(timezone.utc) + timedelta(hours=1),
        )

    def test_covered_and_uncovered_tasks_are_counted_separately_and_null_is_not_summed_as_zero(
        self,
    ) -> None:
        self.seed_task(task_id="t1", guard_denied_count=3)
        self.seed_task(task_id="t2", guard_denied_count=0)
        self.seed_task(task_id="t3", guard_denied_count=None)

        window_start, window_end = self._window()
        covered, uncovered, total = self.source.guard_denied_count_stats(
            window_start=window_start, window_end=window_end
        )

        self.assertEqual((covered, uncovered, total), (2, 1, 3))

    def test_no_tasks_in_window_is_a_real_zero_not_an_error(self) -> None:
        far_future_start = datetime.now(timezone.utc) + timedelta(days=365)
        covered, uncovered, total = self.source.guard_denied_count_stats(
            window_start=far_future_start, window_end=far_future_start + timedelta(hours=1)
        )
        self.assertEqual((covered, uncovered, total), (0, 0, 0))

    def test_tasks_outside_the_window_are_excluded(self) -> None:
        self.seed_task(
            task_id="t-old", guard_denied_count=99, created_at_sql="now() - interval '10 days'"
        )

        window_start, window_end = self._window()
        covered, uncovered, total = self.source.guard_denied_count_stats(
            window_start=window_start, window_end=window_end
        )
        self.assertEqual((covered, uncovered, total), (0, 0, 0))


class TokenUsageStatsTests(DailyReportPostgresTestCase):
    """`token_usage_stats`（迁移 0070，Issue #303/#304 批次 4）：JSONB 四字段
    各自求和，NULL 行只计入 `uncovered_tasks`、缺失的子字段视为 0 参与求和
    （`->>'字段名'` 对 NULL 返回 NULL，`SUM` 天然跳过）。"""

    def _window(self) -> tuple[datetime, datetime]:
        return (
            datetime.now(timezone.utc) - timedelta(hours=1),
            datetime.now(timezone.utc) + timedelta(hours=1),
        )

    def test_four_fields_are_summed_independently_across_covered_tasks(self) -> None:
        self.seed_task(
            task_id="t1",
            token_usage={
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_creation_input_tokens": 5,
                "cache_read_input_tokens": 1,
            },
        )
        self.seed_task(
            task_id="t2",
            token_usage={"input_tokens": 50, "output_tokens": 10},  # 只含两个字段，取到几个算几个
        )
        self.seed_task(task_id="t3", token_usage=None)

        window_start, window_end = self._window()
        covered, uncovered, input_tokens, output_tokens, cache_creation, cache_read = (
            self.source.token_usage_stats(window_start=window_start, window_end=window_end)
        )

        self.assertEqual(covered, 2)
        self.assertEqual(uncovered, 1)
        self.assertEqual(input_tokens, 150)
        self.assertEqual(output_tokens, 30)
        self.assertEqual(cache_creation, 5)
        self.assertEqual(cache_read, 1)

    def test_no_tasks_in_window_is_a_real_zero_not_an_error(self) -> None:
        far_future_start = datetime.now(timezone.utc) + timedelta(days=365)
        result = self.source.token_usage_stats(
            window_start=far_future_start, window_end=far_future_start + timedelta(hours=1)
        )
        self.assertEqual(result, (0, 0, 0, 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
