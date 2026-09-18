"""三条清理路径的单轮行级上限（`V-投递-11`）：真库断言。

空闲会话正文清理（``sweep_idle_conversations``）、二十四小时到期收敛
（``expire_undelivered_terminals``）与心跳超时回收（``reclaim_stale``）各为一条带
``LIMIT`` 的语句：单轮最多处理 500 行候选、积压由下一轮继续、未处理行逐字不变、
一处行锁冲突只作废这一条语句、日志与返回值不含行内容。空闲清理的候选窗口按
(会话 id, 事件 id) 升序锁行，与 ``/new``、按用户清理同一锁序，交叉布局下并发零死锁。

判定值全部由测试实跑写出：隔离真库里的表内容与被测方法的返回值；前置状态
（多少会话、多少行、谁占着锁）由用例自己构造。大批量行用 ``generate_series``
直接落库，绕过逐条写入路径——被测的是清理语句，不是写入语句。
"""

from __future__ import annotations

import logging
import os
import threading
import time
import unittest
from datetime import timedelta

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, connect
from lingxi.adapters.postgres_conversation import PostgresTaskQueue, _Transaction
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
        event_prefix: str | None = None,
    ) -> list[str]:
        """一个已空闲 ``idle_for`` 的会话、一个已结束任务、``events`` 条 terminal 行。

        事件 id 默认形如 ``tde-<会话>-<六位序号>``，因此「按 (会话 id, 事件 id) 升序」
        在用例里是可以手算的；``event_prefix`` 可以换掉前缀，造出会话 id 与事件 id
        顺序相反的布局。返回本会话全部事件 id（升序）。``delivered=False`` 造的是
        送达前的正文——它走独立的二十四小时到期路径，空闲清理不得碰它。
        """
        prefix = event_prefix if event_prefix is not None else f"tde-{conversation_id}-"
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
            (prefix, task_id, CONTENT_MARKER, f"{task_id}:", events),
        )
        # 批量 generate_series 插入中途若被自动分析取到 reltuples=0 的快照，规划器会把
        # 外层 UPDATE 排成非参数化 Nested Loop（6 万行实测 14 秒、撞 3 秒语句超时）——
        # 这是测试造数方式引起的假红，不是产品缺陷；造数后显式分析让统计与行数一致。
        self.execute("ANALYZE task_delivery_event")
        return [f"{prefix}{sequence:06d}" for sequence in range(1, events + 1)]

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
        """上限生效：1 个会话 3000 行 + 200 个会话各 10 行，单轮只清按 (会话 id, 事件 id)
        升序的前 500 行——上限是行级的，会在一个会话中间切开，不是「清完整个会话」。
        这份数据的事件 id 内嵌会话 id，两级排序与单按事件 id 排序给出同一批行。"""

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


class _PauseBetweenConversations(_Transaction):
    """测试专用：按用户清理清完前一个会话、正要清 ``pause_before`` 这个会话时暂停，
    等外部放行——用来确定性地让空闲扫描在这个空档进场并在数据库层排队，不依赖
    线程调度运气。暂停点在两条逐会话语句之间，此时本事务已持有前一个会话全部
    投递事件的行锁。"""

    def __init__(
        self,
        connection,
        *,
        pause_before: str,
        ready_event: threading.Event,
        go_event: threading.Event,
    ) -> None:
        super().__init__(connection)
        self._pause_before = pause_before
        self._ready_event = ready_event
        self._go_event = go_event

    def clear_delivered_content_for_conversation(self, *, conversation_id: str) -> int:
        if conversation_id == self._pause_before:
            self._ready_event.set()
            self._go_event.wait(timeout=15)
        return super().clear_delivered_content_for_conversation(conversation_id=conversation_id)


class IdleSweepLockOrderTests(CleanupRowLimitTestCase):
    """路径一的锁序：候选窗口按 (会话 id, 事件 id) 升序锁行，与 ``/new``、按用户清理
    （先锁全部会话、再逐会话按事件 id 升序清）同一锁序。

    交叉布局：会话 id 低的 ``cnv-1`` 反而持有事件 id 高的行（``tde-2-*``），``cnv-2``
    持有 ``tde-1-*``；两会话各 300 行，单轮 500 行的窗口横跨两者。「按事件 id 单独
    升序」在这份数据上会先锁走 ``cnv-2`` 的行、再等 ``cnv-1`` 的行，与逐会话清理
    的顺序相反——两条路径并发时互持对方下一步需要的锁，被数据库判定为死锁。
    """

    ROWS_PER_CONVERSATION = 300
    ROUNDS = 10

    def seed_crossed_layout(self) -> tuple[list[str], list[str]]:
        """返回 (cnv-1 的事件 id, cnv-2 的事件 id)，各自升序。"""

        low_conversation = self.seed_conversation(
            "cnv-1", events=self.ROWS_PER_CONVERSATION, event_prefix="tde-2-"
        )
        high_conversation = self.seed_conversation(
            "cnv-2", events=self.ROWS_PER_CONVERSATION, event_prefix="tde-1-"
        )
        return low_conversation, high_conversation

    def test_the_window_is_ordered_by_conversation_then_event_id_not_by_event_id_alone(
        self,
    ) -> None:
        """单轮 500 行 = cnv-1 全部 300 行 + cnv-2 按事件 id 升序的前 200 行；单按事件 id
        升序会得到 cnv-2 全部 300 行 + cnv-1 的前 200 行，两者在这份数据上可区分。"""

        low_conversation, high_conversation = self.seed_crossed_layout()
        expected = sorted(low_conversation + high_conversation[:200])
        by_event_id_alone = sorted(low_conversation + high_conversation)[:_CLEANUP_ROW_LIMIT]
        self.assertNotEqual(expected, by_event_id_alone, "布局必须能区分两种排序")

        cleared = self.queue.sweep_idle_conversations(idle_after=IDLE)

        self.assertEqual(cleared, _CLEANUP_ROW_LIMIT)
        self.assertEqual(self.cleared_ids(), expected)
        self.assertEqual(self.rows_with_content(), 2 * self.ROWS_PER_CONVERSATION - cleared)

    def _wait_until_blocked_on_a_lock(self, pid: int, *, timeout: float) -> bool:
        """轮询 ``pg_stat_activity``，直到该后端在数据库层等锁（或超时）。"""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            waiting = self.scalar(
                "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = %s", (pid,)
            )
            if waiting:
                return True
            time.sleep(0.02)
        return False

    def _run_one_round(self) -> tuple[BaseException | None, BaseException | None]:
        """一轮：按用户清理锁两个会话并清完 cnv-1 后暂停；空闲扫描进场、在数据库层排队；
        放行后按用户清理继续清 cnv-2。两边都回滚，数据留给下一轮。返回两边的异常。"""

        conn_a = connect(self._dsn)
        conn_b = connect(self._dsn)
        a_ready = threading.Event()
        a_go = threading.Event()
        errors: dict[str, BaseException | None] = {"a": None, "b": None}

        def run_user_purge() -> None:
            try:
                _PauseBetweenConversations(
                    conn_a, pause_before="cnv-2", ready_event=a_ready, go_event=a_go
                ).clear_delivered_content_for_user(user_id="usr-1", reason="user_cleared")
            except BaseException as error:  # noqa: BLE001 - 收集到主线程再断言
                errors["a"] = error
            finally:
                a_ready.set()
                conn_a.rollback()
                conn_a.close()

        def run_sweep() -> None:
            try:
                PostgresTaskQueue._clear_stale_delivered_content(conn_b.cursor(), idle_after=IDLE)
            except BaseException as error:  # noqa: BLE001 - 收集到主线程再断言
                errors["b"] = error
            finally:
                conn_b.rollback()
                conn_b.close()

        sweep_pid = conn_b.execute("SELECT pg_backend_pid()").fetchone()[0]
        thread_a = threading.Thread(target=run_user_purge)
        thread_b = threading.Thread(target=run_sweep)

        thread_a.start()
        self.assertTrue(a_ready.wait(timeout=5), "按用户清理 5 秒内未到达暂停点")
        thread_b.start()
        self.assertTrue(
            self._wait_until_blocked_on_a_lock(sweep_pid, timeout=5),
            "空闲扫描 5 秒内没有在数据库层等到按用户清理持有的行锁",
        )
        a_go.set()

        thread_a.join(timeout=15)
        thread_b.join(timeout=15)
        self.assertFalse(thread_a.is_alive(), "按用户清理线程 15 秒内未结束")
        self.assertFalse(thread_b.is_alive(), "空闲扫描线程 15 秒内未结束")
        return errors["a"], errors["b"]

    def test_sweep_and_user_purge_never_deadlock_across_ten_rounds(self) -> None:
        """交叉布局下两条路径并发 10 轮：零死锁（40P01）、零锁等待超时，数据每轮原样保留。"""

        self.seed_crossed_layout()
        for round_index in range(self.ROUNDS):
            with self.subTest(round=round_index):
                a_error, b_error = self._run_one_round()
                self.assertIsNone(
                    a_error, f"round {round_index}: 按用户清理出现未预期异常：{a_error!r}"
                )
                self.assertIsNone(
                    b_error, f"round {round_index}: 空闲扫描出现未预期异常：{b_error!r}"
                )
                self.assertEqual(self.rows_with_content(), 2 * self.ROWS_PER_CONVERSATION)


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
