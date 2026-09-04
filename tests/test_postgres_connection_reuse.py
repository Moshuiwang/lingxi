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
        connection.execute("CREATE TEMP TABLE reuse_probe_close (x int)")
        connection.close()
        with connect(DSN) as reused:
            self.assertEqual(_backend_pid(reused), _backend_pid(reused))
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

    def test_a_connection_terminated_while_idle_fails_once_then_recovers_without_probe(self) -> None:
        with connect(DSN) as connection:
            victim = _backend_pid(connection)
        _terminate(victim)
        with self.assertRaises(Exception):
            with connect(DSN) as connection:
                _backend_pid(connection)
        self.assertEqual(idle_connection_count(), 0, "坏掉的连接不得留在空闲栈里")
        with connect(DSN) as connection:
            self.assertNotEqual(_backend_pid(connection), victim)

    def test_idle_stack_is_bounded_and_the_overflow_is_really_closed(self) -> None:
        connections = [connect(DSN) for _ in range(MAX_IDLE_CONNECTIONS_PER_KEY + 2)]
        for connection in connections:
            connection.close()
        self.assertEqual(idle_connection_count(), MAX_IDLE_CONNECTIONS_PER_KEY)
        self.assertEqual(sum(1 for c in connections if c.closed), 2)

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
            self.assertEqual(
                other_connection.execute("SHOW statement_timeout").fetchone()[0], "4s"
            )
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
        with mock.patch(
            "lingxi.adapters.postgres_conversation._queue_base.connect", fake_connect
        ):
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

        with mock.patch(
            "lingxi.adapters.postgres_conversation._listener.connect", fake_connect
        ):
            with PostgresTaskQueueListener("postgresql://test/db"):
                pass
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0].get("dedicated"), True)
        self.assertIs(calls[0].get("autocommit"), True)
