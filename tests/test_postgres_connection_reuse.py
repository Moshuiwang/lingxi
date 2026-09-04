"""Issue #593：``adapters.postgres.connect`` 的进程内连接复用。

真库用例（``LINGXI_POSTGRES_DSN``）逐条钉住模块说明里的承诺：同一 DSN 的连续
``with connect()`` 复用同一条物理连接；嵌套取到不同连接；``with`` 块抛异常时回滚后
再归还，下一位借用者看不到半截事务；``dedicated=True`` / 额外 kwargs 拿到驱动原生
连接且 ``close()`` 真关；服务端掐断的连接被丢弃重建；空闲栈有上界；会话属性复位。

变异对照（#593 完成标准 3）：把 ``reset_for_reuse`` 里的 ``rollback()`` 删掉，
``test_an_exception_inside_with_rolls_back_before_the_connection_is_reused`` 必红——
上一位借用者未提交的临时表会被下一位在同一会话里看见。
"""

from __future__ import annotations

import os
import time
import unittest
from unittest import mock

from lingxi.adapters import postgres
from lingxi.adapters.postgres import (
    MAX_IDLE_CONNECTIONS_PER_KEY,
    PostgresTimeouts,
    close_idle_connections,
    connect,
    idle_connection_count,
)

DSN = os.environ.get("LINGXI_POSTGRES_DSN", "")


def _backend_pid(connection: object) -> int:
    with connection.cursor() as cursor:  # type: ignore[attr-defined]
        cursor.execute("SELECT pg_backend_pid()")
        return int(cursor.fetchone()[0])


def _terminate(pid: int) -> None:
    with connect(DSN, dedicated=True) as killer:
        killer.execute("SELECT pg_terminate_backend(%s)", (pid,))
        killer.commit()
    # 服务端处理终止是异步的；给 libpq 一点时间收到 FIN。
    time.sleep(0.3)


@unittest.skipUnless(DSN, "需要 LINGXI_POSTGRES_DSN 指向可用的测试库")
class ConnectionReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        close_idle_connections()
        self.addCleanup(close_idle_connections)

    def test_consecutive_with_blocks_reuse_one_physical_connection(self) -> None:
        pids = set()
        for _ in range(50):
            with connect(DSN) as connection:
                pids.add(_backend_pid(connection))
        self.assertEqual(len(pids), 1, "50 次连续操作必须只用一条物理连接")
        self.assertEqual(idle_connection_count(), 1)

    def test_nested_with_blocks_get_distinct_connections(self) -> None:
        with connect(DSN) as outer:
            with connect(DSN) as inner:
                self.assertNotEqual(_backend_pid(outer), _backend_pid(inner))
                self.assertEqual(idle_connection_count(), 0, "在用的连接不在空闲栈里")
        self.assertEqual(idle_connection_count(), 2)

    def test_an_exception_inside_with_rolls_back_before_the_connection_is_reused(self) -> None:
        with self.assertRaises(RuntimeError):
            with connect(DSN) as connection:
                connection.execute("CREATE TEMP TABLE reuse_probe (x int)")
                connection.execute("INSERT INTO reuse_probe VALUES (1)")
                raise RuntimeError("模拟业务异常")
        with connect(DSN) as connection:
            from psycopg.pq import TransactionStatus

            self.assertEqual(
                TransactionStatus(connection.pgconn.transaction_status),
                TransactionStatus.IDLE,
            )
            gone = connection.execute("SELECT to_regclass('reuse_probe') IS NULL").fetchone()[0]
            self.assertTrue(gone, "上一位借用者未提交的临时表不得残留到下一次复用")

    def test_close_without_commit_discards_uncommitted_work(self) -> None:
        connection = connect(DSN)
        pid = _backend_pid(connection)
        connection.execute("CREATE TEMP TABLE reuse_probe_close (x int)")
        connection.close()
        self.assertEqual(idle_connection_count(), 1)
        with connect(DSN) as reused:
            self.assertEqual(_backend_pid(reused), pid, "close() 归还的必须是同一条物理连接")
            gone = reused.execute("SELECT to_regclass('reuse_probe_close') IS NULL").fetchone()[0]
            self.assertTrue(gone)

    def test_dedicated_and_kwargs_connections_bypass_the_pool_and_really_close(self) -> None:
        dedicated = connect(DSN, dedicated=True)
        autocommit = connect(DSN, autocommit=True)
        try:
            self.assertEqual(type(dedicated).__name__, "Connection")
            self.assertEqual(type(autocommit).__name__, "Connection")
            self.assertTrue(autocommit.autocommit)
        finally:
            dedicated.close()
            autocommit.close()
        self.assertTrue(dedicated.closed)
        self.assertTrue(autocommit.closed)
        self.assertEqual(idle_connection_count(), 0)

    def test_a_connection_terminated_while_idle_is_replaced_transparently_after_probe(self) -> None:
        with connect(DSN) as connection:
            victim = _backend_pid(connection)
        _terminate(victim)
        with mock.patch.object(postgres, "IDLE_PROBE_AFTER_SECONDS", 0.0):
            with connect(DSN) as connection:
                self.assertNotEqual(_backend_pid(connection), victim)
        self.assertEqual(idle_connection_count(), 1)

    def test_a_connection_terminated_while_idle_is_replaced_transparently_within_the_probe_window(
        self,
    ) -> None:
        """空闲不足 30 秒也要透明：被掐断的连接 socket 变为可读，取用前看 socket 就够。"""

        with connect(DSN) as connection:
            victim = _backend_pid(connection)
        _terminate(victim)
        with connect(DSN) as connection:
            self.assertNotEqual(_backend_pid(connection), victim)
        self.assertEqual(idle_connection_count(), 1, "坏掉的连接不得留在空闲栈里，新连接归还")

    def test_a_healthy_idle_connection_is_not_probed_within_the_window(self) -> None:
        """看 socket 不等于每次都探活：健康连接在窗口内取用不额外发语句。"""

        with connect(DSN) as connection:
            pid = _backend_pid(connection)
        with mock.patch.object(type(connection), "probe", side_effect=AssertionError("不该探活")):
            with connect(DSN) as reused:
                self.assertEqual(_backend_pid(reused), pid)

    def test_idle_stack_is_bounded_and_the_overflow_is_really_closed(self) -> None:
        connections = [connect(DSN) for _ in range(MAX_IDLE_CONNECTIONS_PER_KEY + 2)]
        pids = [_backend_pid(connection) for connection in connections]
        for connection in connections:
            connection.close()
        self.assertEqual(idle_connection_count(), MAX_IDLE_CONNECTIONS_PER_KEY)
        self.assertTrue(all(c.closed for c in connections), "归还后对借用者一律是已关闭")
        with connect(DSN, dedicated=True) as observer:
            alive = observer.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE pid = ANY(%s)", (pids,)
            ).fetchone()[0]
        self.assertEqual(alive, MAX_IDLE_CONNECTIONS_PER_KEY, "超出上限的两条必须真关")

    def test_session_attributes_are_reset_before_reuse(self) -> None:
        with connect(DSN) as connection:
            connection.read_only = True
        with connect(DSN) as connection:
            self.assertIsNone(connection.read_only)
            self.assertFalse(connection.autocommit)

    def test_different_timeouts_do_not_share_connections(self) -> None:
        other = PostgresTimeouts(statement_timeout_seconds=4)
        with connect(DSN) as default_connection:
            default_pid = _backend_pid(default_connection)
        with connect(DSN, timeouts=other) as other_connection:
            self.assertNotEqual(_backend_pid(other_connection), default_pid)
            self.assertEqual(other_connection.execute("SHOW statement_timeout").fetchone()[0], "4s")
        self.assertEqual(idle_connection_count(), 2)

    def test_close_idle_connections_really_closes_them(self) -> None:
        with connect(DSN) as connection:
            pid = _backend_pid(connection)
        self.assertEqual(close_idle_connections(), 1)
        self.assertEqual(idle_connection_count(), 0)
        with connect(DSN, dedicated=True) as observer:
            alive = observer.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE pid = %s", (pid,)
            ).fetchone()[0]
        self.assertEqual(alive, 0)

    def test_close_is_idempotent_and_the_object_enters_the_idle_stack_once(self) -> None:
        """审核 P1-1：重复 close() 不得把同一对象压栈两次（否则两位借用者共用一条连接）。"""

        connection = connect(DSN)
        connection.close()
        connection.close()
        self.assertEqual(idle_connection_count(), 1)
        self.assertTrue(connection.closed, "归还后对借用者而言就是已关闭")
        first = connect(DSN)
        second = connect(DSN)
        try:
            self.assertIsNot(first, second)
            self.assertNotEqual(_backend_pid(first), _backend_pid(second))
        finally:
            first.close()
            second.close()

    def test_close_inside_with_block_is_safe(self) -> None:
        with connect(DSN) as connection:
            _backend_pid(connection)
            connection.close()
        self.assertEqual(idle_connection_count(), 1)
        with connect(DSN) as again:
            with connect(DSN) as other:
                self.assertNotEqual(_backend_pid(again), _backend_pid(other))

    def test_a_returned_connection_cannot_be_used_again(self) -> None:
        connection = connect(DSN)
        connection.close()
        for action in (connection.cursor, connection.commit, connection.rollback):
            with self.assertRaises(Exception, msg=action.__name__):
                action()
        with self.assertRaises(Exception):
            connection.execute("SELECT 1")
        self.assertEqual(idle_connection_count(), 1, "误用不得把连接弄坏或弄丢")

    def test_concurrent_threads_never_share_a_physical_connection(self) -> None:
        import threading

        lock = threading.Lock()
        in_use: dict[int, int] = {}
        violations: list[str] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                for _ in range(40):
                    with connect(DSN) as connection:
                        pid = _backend_pid(connection)
                        with lock:
                            if in_use.get(pid):
                                violations.append(f"pid {pid} 被两个线程同时持有")
                            in_use[pid] = in_use.get(pid, 0) + 1
                        _backend_pid(connection)
                        with lock:
                            in_use[pid] -= 1
            except BaseException as error:  # noqa: BLE001 - 收集后在主线程断言
                errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(violations, [])
        self.assertLessEqual(idle_connection_count(), MAX_IDLE_CONNECTIONS_PER_KEY)

    def test_connections_idle_for_too_long_are_closed_on_the_next_release(self) -> None:
        """审核 P2-2：后进先出让栈底连接永远轮不到，按空闲时长回收。"""

        with connect(DSN) as outer:
            with connect(DSN) as inner:
                outer_pid, inner_pid = _backend_pid(outer), _backend_pid(inner)
        # inner 先归还（栈底、更旧），outer 后归还（栈顶）
        self.assertEqual(idle_connection_count(), 2)
        with mock.patch.object(postgres, "MAX_IDLE_AGE_SECONDS", 0.0):
            with connect(DSN) as connection:
                self.assertEqual(_backend_pid(connection), outer_pid, "栈顶先出")
            # 归还时栈底（inner）已超龄 → 真关；刚归还的 outer 仍在
        self.assertEqual(idle_connection_count(), 1)
        with connect(DSN, dedicated=True) as observer:
            alive = observer.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE pid = %s", (inner_pid,)
            ).fetchone()[0]
        self.assertEqual(alive, 0)

    def test_tcp_keepalive_is_fixed_and_prepared_statements_stay_off(self) -> None:
        """审核 P2-1 / P2-4：保活参数由入口固定；复用连接不预编译，保持改前逐位相同。"""

        with connect(DSN) as connection:
            parameters = connection.info.get_parameters()
            self.assertEqual(parameters.get("keepalives_idle"), "30")
            self.assertEqual(parameters.get("tcp_user_timeout"), "15000")
            self.assertIsNone(connection.prepare_threshold)
            for _ in range(7):
                connection.execute("SELECT 42").fetchone()
            prepared = connection.execute("SELECT count(*) FROM pg_prepared_statements").fetchone()[
                0
            ]
            self.assertEqual(prepared, 0)
        with connect(DSN, dedicated=True) as dedicated:
            self.assertEqual(dedicated.info.get_parameters().get("keepalives_idle"), "30")
            self.assertEqual(dedicated.prepare_threshold, 5, "独占连接保持驱动默认")
        with self.assertRaises(TypeError):
            connect(DSN, keepalives_idle=1)


class IdleConnectionPoolDiscardsClosedConnectionsTests(unittest.TestCase):
    """#593 P2-a：弹出已物理关闭的空闲连接时必须显式 ``discard()``，不能只是跳过
    （否则底层 PGconn 要等 GC 才释放）。假连接对象，不需要真库。"""

    def test_acquire_discards_a_closed_connection_popped_from_the_stack(self) -> None:
        class _FakeConnection:
            def __init__(self) -> None:
                self.closed = True
                self.discard_calls = 0
                self._lingxi_idle = True

            def discard(self) -> None:
                self.discard_calls += 1

        pool = postgres._IdleConnectionPool()
        key = ("postgresql://test/db", PostgresTimeouts())
        stale = _FakeConnection()
        pool._idle[key] = [(stale, 0.0)]

        result = pool.acquire(key)

        self.assertIsNone(result, "栈里只有一条已关闭的连接，弹出丢弃后应无可用连接")
        self.assertEqual(stale.discard_calls, 1, "已关闭的连接必须被显式 discard()，不能只是 continue")


class DedicatedCallSiteWiringTests(unittest.TestCase):
    """三处长期手工持有连接的调用点必须声明 ``dedicated=True``，不进空闲栈。"""

    def test_polling_queue_holds_dedicated_connections(self) -> None:
        from lingxi.adapters.postgres_conversation._queue_base import _TaskQueueBase

        calls: list[dict[str, object]] = []

        class _Connection:
            closed = False

            def commit(self) -> None:
                pass

            def close(self) -> None:
                self.closed = True

        def fake_connect(dsn: str, **kwargs: object) -> _Connection:
            calls.append(kwargs)
            return _Connection()

        queue = _TaskQueueBase("postgresql://test/db", reuse_polling_connection=True)
        with mock.patch("lingxi.adapters.postgres_conversation._queue_base.connect", fake_connect):
            queue._run_polling_operation(lambda connection: None)
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0].get("dedicated"), True)

    def test_listener_holds_a_dedicated_autocommit_connection(self) -> None:
        from lingxi.adapters.postgres_conversation._listener import PostgresTaskQueueListener

        calls: list[dict[str, object]] = []

        class _Connection:
            def execute(self, _sql: str) -> None:
                pass

            def close(self) -> None:
                pass

        def fake_connect(dsn: str, **kwargs: object) -> _Connection:
            calls.append(kwargs)
            return _Connection()

        with mock.patch("lingxi.adapters.postgres_conversation._listener.connect", fake_connect):
            with PostgresTaskQueueListener("postgresql://test/db"):
                pass
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0].get("dedicated"), True)
        self.assertIs(calls[0].get("autocommit"), True)
