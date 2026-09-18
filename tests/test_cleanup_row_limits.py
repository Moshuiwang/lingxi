"""三条清理路径的单轮行级上限（`V-投递-11`）：真库断言。

空闲会话正文清理（``sweep_idle_conversations``）、二十四小时到期收敛
（``expire_undelivered_terminals``）与心跳超时回收（``reclaim_stale``）各为一条带
``LIMIT`` 的语句：单轮最多处理 500 行候选、积压由下一轮继续、未处理行逐字不变、
一处行锁冲突只作废这一条语句、日志与返回值不含行内容。

判定值全部由测试实跑写出：隔离真库里的表内容与被测方法的返回值；前置状态
（多少会话、多少行、谁占着锁）由用例自己构造。大批量行用 ``generate_series``
直接落库，绕过逐条写入路径——被测的是清理语句，不是写入语句。
"""

from __future__ import annotations

import logging
import os
import time
import unittest
from datetime import timedelta

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, connect
from lingxi.adapters.postgres_conversation import PostgresTaskQueue
from lingxi.adapters.postgres_conversation._queue_outbox import _CLEANUP_ROW_LIMIT
from lingxi.apps.scheduler.retention import IDLE_CONVERSATION_SWEEP_AFTER, IdleConversationSweepDuty

SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，清理路径行级上限的真库断言未验证"
    if not os.environ.get("LINGXI_POSTGRES_DSN")
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，清理路径行级上限的真库断言未验证"
)

IDLE = timedelta(hours=2)
# 正文里带一个不会自然出现在任何日志文案里的标记，用来断言日志/返回值不含行内容。
CONTENT_MARKER = "机密正文标记"

_CANDIDATE_COLUMNS = "id, content, event_type, sequence, terminal_kind, platform_received_at"


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN") and psycopg_available(), SKIP_REASON)
class CleanupRowLimitTestCase(unittest.TestCase):
    """真库底座：进程内建一次库，每个用例前只清行。"""

    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        cls._psycopg = psycopg
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        reset_production_rows(self._dsn)
        # 默认超时（语句 3 秒、锁等待 2 秒）：上限是否成立必须在仓库默认预算下判定。
        self.queue = PostgresTaskQueue(self._dsn)
        self._connection = self._psycopg.connect(self._dsn, autocommit=True)
        self.addCleanup(self._connection.close)
        self.execute(
            """INSERT INTO app_user
               (id, feishu_open_id, feishu_user_id, feishu_union_id,
                display_name, department, tenant_key, provisioning_state)
               VALUES ('usr-1','ou-1','u-1','un-1','张三','数据部','tk-1','active')"""
        )

    # -- 小工具 -----------------------------------------------------------

    def query(self, sql: str, parameters: tuple = ()) -> list[tuple]:
        with self._connection.cursor() as cursor:
            cursor.execute(sql, parameters)
            return cursor.fetchall()

    def execute(self, sql: str, parameters: tuple = ()) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(sql, parameters)

    def scalar(self, sql: str, parameters: tuple = ()):
        rows = self.query(sql, parameters)
        return rows[0][0] if rows else None

    def seed_conversation(
        self,
        conversation_id: str,
        *,
        events: int,
        idle_for: str = "3 hours",
        delivered: bool = True,
    ) -> list[str]:
        """一个已空闲 ``idle_for`` 的会话、一个已结束任务、``events`` 条 terminal 行。

        事件 id 形如 ``tde-<会话>-<六位序号>``，因此「按事件 id 升序」在用例里是可以
        手算的；返回本会话全部事件 id（升序）。``delivered=False`` 造的是送达前的
        正文——它走独立的二十四小时到期路径，空闲清理不得碰它。
        """
        self.execute(
            """INSERT INTO conversation
               (id, user_id, feishu_chat_id, feishu_thread_id, running_task_id, last_task_ended_at)
               VALUES (%s, 'usr-1', %s, %s, NULL, now() - %s::interval)""",
            (conversation_id, f"chat-{conversation_id}", f"topic-{conversation_id}", idle_for),
        )
        task_id = f"tsk-{conversation_id}"
        self.execute(
            """INSERT INTO task
               (id, conversation_id, user_id, inbound_event_id, prompt, status,
                target_worker_version, worker_id, heartbeat_at, attempts, content_expires_at)
               VALUES (%s, %s, 'usr-1', %s, '问题', 'succeeded', 'stable', 'worker-1',
                       now(), 1, now())""",
            (task_id, conversation_id, f"event-{task_id}"),
        )
        received_sql = "now()" if delivered else "NULL"
        self.execute(
            f"""INSERT INTO task_delivery_event
                (id, task_id, sequence, event_type, terminal_kind, content, worker_id,
                 idempotency_key, platform_received_at)
                SELECT %s || lpad(g::text, 6, '0'), %s, g, 'terminal', 'success',
                       %s || g, 'worker-1', %s || g, {received_sql}
                  FROM generate_series(1, %s) AS g""",
            (f"tde-{conversation_id}-", task_id, CONTENT_MARKER, f"{task_id}:", events),
        )
        return [f"tde-{conversation_id}-{sequence:06d}" for sequence in range(1, events + 1)]

    def cleared_ids(self) -> list[str]:
        return [
            row[0]
            for row in self.query(
                "SELECT id FROM task_delivery_event WHERE content IS NULL ORDER BY id"
            )
        ]

    def rows_with_content(self) -> int:
        return self.scalar("SELECT count(*) FROM task_delivery_event WHERE content IS NOT NULL")

    def snapshot(self) -> dict[str, tuple]:
        return {
            row[0]: row[1:]
            for row in self.query(f"SELECT {_CANDIDATE_COLUMNS} FROM task_delivery_event")
        }


class IdleSweepRowLimitTests(CleanupRowLimitTestCase):
    """路径一：空闲会话正文清理。"""

    def test_the_limit_is_five_hundred_rows(self) -> None:
        self.assertEqual(_CLEANUP_ROW_LIMIT, 500)

    def test_one_round_clears_at_most_the_limit_and_exactly_the_first_rows_by_event_id(
        self,
    ) -> None:
        """上限生效：1 个会话 3000 行 + 200 个会话各 10 行，单轮只清按事件 id 排序的前
        500 行——上限是行级的，会在一个会话中间切开，不是「清完整个会话」。"""

        candidates: list[str] = []
        for index in range(201):
            conversation_id = f"cnv-{index:03d}"
            candidates += self.seed_conversation(
                conversation_id, events=3000 if index == 25 else 10
            )
        # 仍在活跃期的会话与送达前的正文都不在候选集里。
        untouched = self.seed_conversation("cnv-active", events=10, idle_for="5 minutes")
        untouched += self.seed_conversation("cnv-undelivered", events=10, delivered=False)
        expected = sorted(candidates)[:_CLEANUP_ROW_LIMIT]
        # 前 500 行确实横跨 cnv-000…cnv-024 全部 250 行与 cnv-025 的前 250 行。
        self.assertEqual(expected[-1], "tde-cnv-025-000250")

        cleared = self.queue.sweep_idle_conversations(idle_after=IDLE)

        self.assertEqual(cleared, _CLEANUP_ROW_LIMIT)
        self.assertEqual(self.cleared_ids(), expected)
        self.assertEqual(self.rows_with_content(), len(candidates) + len(untouched) - cleared)

    def test_repeated_rounds_drain_the_backlog_monotonically_to_zero(self) -> None:
        """连续前进：剩余待清行数每轮严格下降直到 0，不出现固定残留。"""

        seeded = self.seed_conversation("cnv-big", events=1200)
        for index in range(20):
            seeded += self.seed_conversation(f"cnv-{index:03d}", events=10)
        remaining = [len(seeded)]
        returned = []
        for _round in range(4):
            returned.append(self.queue.sweep_idle_conversations(idle_after=IDLE))
            remaining.append(self.rows_with_content())

        self.assertEqual(returned, [500, 500, 400, 0])
        self.assertEqual(remaining, [1400, 900, 400, 0, 0])
        self.assertEqual(sum(returned), len(seeded))

    def test_rows_outside_this_round_keep_content_and_low_sensitivity_columns_verbatim(
        self,
    ) -> None:
        """未处理行逐字不变：本轮未覆盖的行，正文与 event_type / sequence /
        terminal_kind / platform_received_at 一字不改；被清的行也只动 content。"""

        candidates = self.seed_conversation("cnv-big", events=700)
        candidates += self.seed_conversation("cnv-small", events=5)
        self.seed_conversation("cnv-active", events=5, idle_for="1 minute")
        self.seed_conversation("cnv-undelivered", events=5, delivered=False)
        before = self.snapshot()
        this_round = set(sorted(candidates)[:_CLEANUP_ROW_LIMIT])

        self.queue.sweep_idle_conversations(idle_after=IDLE)

        after = self.snapshot()
        self.assertEqual(set(after), set(before))
        for event_id, row in before.items():
            if event_id in this_round:
                self.assertEqual(after[event_id], (None, *row[1:]), event_id)
            else:
                self.assertEqual(after[event_id], row, event_id)

    def test_a_single_conversation_with_sixty_thousand_rows_advances_under_the_default_timeout(
        self,
    ) -> None:
        """大单会话仍前进：6 万行待清正文、仓库默认 3 秒语句超时，单轮正常返回并清 500 行。"""

        self.assertIs(self.queue._timeouts, DEFAULT_POSTGRES_TIMEOUTS)
        self.assertEqual(DEFAULT_POSTGRES_TIMEOUTS.statement_timeout_seconds, 3)
        with connect(self._dsn) as connection:
            self.assertEqual(connection.execute("SHOW statement_timeout").fetchone()[0], "3s")
        seeded = self.seed_conversation("cnv-huge", events=60_000)

        # 语句超时由数据库执行：超过 3 秒会以 QueryCanceled 抛出，正常返回本身就是判据。
        cleared = self.queue.sweep_idle_conversations(idle_after=IDLE)

        self.assertEqual(cleared, _CLEANUP_ROW_LIMIT)
        self.assertEqual(self.cleared_ids(), seeded[:_CLEANUP_ROW_LIMIT])
        self.assertEqual(self.rows_with_content(), len(seeded) - _CLEANUP_ROW_LIMIT)

    def test_a_row_locked_elsewhere_fails_only_this_round_and_the_next_round_clears_all(
        self,
    ) -> None:
        """冲突只作废一条语句：另一会话 ``FOR UPDATE`` 占住排序靠后的一行，本轮按
        ``lock_timeout`` 失败并整体回滚（已清的行不留下）；解除占用后下一轮清空。"""

        seeded = self.seed_conversation("cnv-1", events=20)
        holder = self._psycopg.connect(self._dsn)
        self.addCleanup(holder.close)
        holder.execute("SELECT id FROM task_delivery_event WHERE id = %s FOR UPDATE", (seeded[-1],))

        started = time.monotonic()
        with self.assertRaises(self._psycopg.errors.LockNotAvailable) as raised:
            self.queue.sweep_idle_conversations(idle_after=IDLE)
        elapsed = time.monotonic() - started

        self.assertEqual(raised.exception.sqlstate, "55P03")
        self.assertGreaterEqual(elapsed, DEFAULT_POSTGRES_TIMEOUTS.lock_timeout_seconds - 0.5)
        self.assertEqual(self.cleared_ids(), [], "失败的一轮必须整体回滚，不留半清状态")
        self.assertEqual(self.rows_with_content(), len(seeded))

        holder.rollback()

        self.assertEqual(self.queue.sweep_idle_conversations(idle_after=IDLE), len(seeded))
        self.assertEqual(self.rows_with_content(), 0)

    def test_log_and_return_value_carry_counts_only(self) -> None:
        """日志与返回值只有数量：scheduler 职责那条 INFO 与返回的整数都不含任何正文。"""

        seeded = self.seed_conversation("cnv-1", events=7)
        duty = IdleConversationSweepDuty(queue=self.queue, idle_after=IDLE_CONVERSATION_SWEEP_AFTER)

        with self.assertLogs("lingxi", level=logging.DEBUG) as captured:
            report = duty.run_once()

        self.assertEqual(report, len(seeded))
        self.assertIsInstance(report, int)
        messages = [record.getMessage() for record in captured.records]
        self.assertEqual(messages, ["空闲会话清理：本轮清空 7 条已送达投递正文"])
        for record in captured.records:
            self.assertNotIn(CONTENT_MARKER, record.getMessage())
            self.assertNotIn("tde-cnv-1", record.getMessage())
        self.assertEqual(IDLE_CONVERSATION_SWEEP_AFTER, IDLE)


class WorkerSideRowLimitTests(CleanupRowLimitTestCase):
    """路径二、三：worker 侧到期收敛与心跳超时回收的候选集同型上限。"""

    def seed_tasks(self, count: int, *, status: str, heartbeat_sql: str) -> list[str]:
        """``count`` 个各自独占一个会话的任务，id 形如 ``tsk-<六位序号>``，升序返回。"""

        self.execute(
            """INSERT INTO conversation
               (id, user_id, feishu_chat_id, feishu_thread_id, running_task_id)
               SELECT 'cnv-' || lpad(g::text, 6, '0'), 'usr-1', 'chat-' || g, 'topic-' || g,
                      'tsk-' || lpad(g::text, 6, '0')
                 FROM generate_series(1, %s) AS g""",
            (count,),
        )
        self.execute(
            f"""INSERT INTO task
                (id, conversation_id, user_id, inbound_event_id, prompt, status,
                 target_worker_version, worker_id, heartbeat_at, attempts, content_expires_at)
                SELECT 'tsk-' || lpad(g::text, 6, '0'), 'cnv-' || lpad(g::text, 6, '0'), 'usr-1',
                       'event-' || g, '问题', %s, 'stable', 'worker-1', {heartbeat_sql}, 1, now()
                  FROM generate_series(1, %s) AS g""",
            (status, count),
        )
        return [f"tsk-{index:06d}" for index in range(1, count + 1)]

    def test_expire_undelivered_terminals_handles_at_most_the_limit_per_round(self) -> None:
        """600 条到期未送达终态：单轮只收敛按候选顺序排前的 500 条，下一轮继续、第三轮为空。"""

        task_ids = self.seed_tasks(600, status="awaiting_delivery", heartbeat_sql="now()")
        # 触发器把 expires_at 钉在 created_at + 24 小时：created_at 在 25 小时前即已到期。
        self.execute(
            """INSERT INTO task_delivery_event
               (id, task_id, sequence, event_type, terminal_kind, content, worker_id,
                idempotency_key, created_at)
               SELECT 'tde-' || id, id, 1, 'terminal', 'success', %s, 'worker-1',
                      id || ':terminal', now() - interval '25 hours'
                 FROM task""",
            (CONTENT_MARKER,),
        )

        first = self.queue.expire_undelivered_terminals()

        self.assertEqual(len(first), _CLEANUP_ROW_LIMIT)
        self.assertEqual(sorted(item.task_id for item in first), task_ids[:_CLEANUP_ROW_LIMIT])
        self.assertEqual(
            self.scalar("SELECT count(*) FROM task WHERE status = 'awaiting_delivery'"), 100
        )
        self.assertEqual(
            self.scalar("SELECT count(*) FROM task_delivery_event WHERE content IS NOT NULL"), 100
        )
        second = self.queue.expire_undelivered_terminals()
        self.assertEqual(sorted(item.task_id for item in second), task_ids[_CLEANUP_ROW_LIMIT:])
        self.assertEqual(self.queue.expire_undelivered_terminals(), [])
        self.assertEqual(self.scalar("SELECT count(*) FROM task WHERE status = 'failed'"), 600)

    def test_reclaim_stale_handles_at_most_the_limit_per_round(self) -> None:
        """600 个心跳超时任务：单轮只回收 500 个，下一轮继续、第三轮为空；回收结果类型不变。"""

        task_ids = self.seed_tasks(
            600, status="running", heartbeat_sql="now() - interval '10 minutes'"
        )

        first = self.queue.reclaim_stale(older_than=timedelta(minutes=5))

        self.assertIsInstance(first, list)
        self.assertEqual(len(first), _CLEANUP_ROW_LIMIT)
        self.assertEqual(sorted(first), task_ids[:_CLEANUP_ROW_LIMIT])
        self.assertEqual(self.scalar("SELECT count(*) FROM task WHERE status = 'running'"), 100)
        requeued, terminal = self.queue.reclaim_stale_with_outcomes(older_than=timedelta(minutes=5))
        self.assertEqual(sorted(requeued), task_ids[_CLEANUP_ROW_LIMIT:])
        self.assertEqual(terminal, [])
        self.assertEqual(self.queue.reclaim_stale(older_than=timedelta(minutes=5)), [])
        self.assertEqual(self.scalar("SELECT count(*) FROM task WHERE status = 'queued'"), 600)


if __name__ == "__main__":
    unittest.main()
