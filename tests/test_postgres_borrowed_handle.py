"""Issue #835：``adapters.postgres.connect`` 默认路径返回的委托句柄、包装游标与事务上下文。

真库用例（``LINGXI_POSTGRES_DSN``）钉住模块说明里的借用合同：句柄只转发显式清单里的
驱动接口（会话属性读写经句柄生效于物理连接，``pgconn`` 与清单外属性一律 ``AttributeError``）；
归还后旧句柄、旧游标、旧事务上下文一律报「连接已关闭」且不触达数据库；迟到的 ``close()``
不会把别人在用的连接压回空闲栈（有判别力的时序：a 归还 → b 借出 → a 迟到 close → c 借出，
c 与 b 后端 pid 不同）；``with … as tx`` 里 ``raise psycopg.Rollback(tx)`` 与驱动语义相同。

变异对照（#835 必做项 4d）：把 ``transaction()`` 的包装去掉、直接返回驱动的上下文，
``test_a_transaction_context_taken_before_return_is_dead_afterwards`` 必红——旧上下文
会在别人的连接上 BEGIN。
"""

from __future__ import annotations

import os
import unittest

from lingxi.adapters.postgres import close_idle_connections, connect, idle_connection_count

DSN = os.environ.get("LINGXI_POSTGRES_DSN", "")


def _backend_pid(connection: object) -> int:
    return int(connection.execute("SELECT pg_backend_pid()").fetchone()[0])  # type: ignore[attr-defined]


@unittest.skipUnless(DSN, "需要 LINGXI_POSTGRES_DSN 指向可用的测试库")
class BorrowedHandleTests(unittest.TestCase):
    def setUp(self) -> None:
        close_idle_connections()
        self.addCleanup(close_idle_connections)
        self.observer = connect(DSN, dedicated=True, autocommit=True)
        self.addCleanup(self.observer.close)

    def backend_state(self, pid: int) -> str | None:
        row = self.observer.execute(
            "SELECT state FROM pg_stat_activity WHERE pid = %s", (pid,)
        ).fetchone()
        return None if row is None else row[0]

    def test_a_late_close_never_hands_the_in_use_connection_to_a_third_borrower(self) -> None:
        """有判别力的共用时序：a 归还 → b 借出 → a 迟到 close() → c 借出，c 不是 b 那条连接。"""

        a = connect(DSN)
        a.close()
        b = connect(DSN)
        self.addCleanup(b.close)
        b_pid = _backend_pid(b)
        a.close()
        self.assertEqual(idle_connection_count(), 0, "迟到 close() 没有把 b 的连接压回空闲栈")
        c = connect(DSN)
        self.addCleanup(c.close)
        self.assertNotEqual(_backend_pid(c), b_pid, "c 必须拿到另一条物理连接")
        self.assertFalse(b.closed)
        self.assertFalse(c.closed)

    def test_a_transaction_context_taken_before_return_is_dead_afterwards(self) -> None:
        import psycopg
        from psycopg.pq import TransactionStatus

        a = connect(DSN)
        pid = _backend_pid(a)
        a.commit()
        tx = a.transaction()
        a.close()
        self.assertEqual(self.backend_state(pid), "idle")
        with self.assertRaises(psycopg.OperationalError):
            with tx:
                self.fail("已归还句柄的事务上下文不得进入块体")
        self.assertEqual(self.backend_state(pid), "idle", "旧事务上下文没有在这条连接上 BEGIN")
        with connect(DSN) as b:
            self.assertEqual(b.info.transaction_status, TransactionStatus.IDLE)
            self.assertEqual(_backend_pid(b), pid, "连接照常复用")
        with self.assertRaises(psycopg.OperationalError):
            a.transaction()

    def test_returning_the_handle_inside_a_transaction_block_discards_the_connection(
        self,
    ) -> None:
        """块内归还：驱动拒绝在事务块里显式回滚，连接被丢弃；退出报错、绝不去提交或回滚别人的连接。"""
        import psycopg

        a = connect(DSN)
        pid = _backend_pid(a)
        with self.assertRaises(psycopg.OperationalError):
            with a.transaction():
                a.execute("SELECT 1")
                a.close()
        self.assertTrue(a.closed)
        self.assertEqual(idle_connection_count(), 0, "带着事务块的连接不得进空闲栈")
        self.assertIsNone(self.backend_state(pid), "那条物理连接已经真正关闭")
        with connect(DSN) as b:
            self.assertNotEqual(_backend_pid(b), pid)

    def test_session_attribute_setters_reach_the_physical_connection_through_the_handle(
        self,
    ) -> None:
        import psycopg
        from psycopg import IsolationLevel
        from psycopg.pq import TransactionStatus

        with connect(DSN) as connection:
            connection.read_only = True
            connection.isolation_level = IsolationLevel.SERIALIZABLE
            self.assertIs(connection.read_only, True)
            self.assertEqual(connection.isolation_level, IsolationLevel.SERIALIZABLE)
            self.assertEqual(connection.execute("SHOW transaction_read_only").fetchone()[0], "on")
            self.assertEqual(
                connection.execute("SHOW transaction_isolation").fetchone()[0], "serializable"
            )
            connection.rollback()
            connection.read_only = None
            connection.isolation_level = None
            connection.autocommit = True
            self.assertTrue(connection.autocommit)
            connection.execute("SELECT 1")
            self.assertEqual(connection.info.transaction_status, TransactionStatus.IDLE)
            self.assertEqual(connection.info.backend_pid, _backend_pid(connection))
            self.assertIsNone(connection.prepare_threshold)
        with self.assertRaises(psycopg.OperationalError):
            connection.autocommit = False
        with self.assertRaises(psycopg.OperationalError):
            connection.read_only = True
        with self.assertRaises(psycopg.OperationalError):
            connection.isolation_level = None
        with self.assertRaises(psycopg.OperationalError):
            connection.info
        with connect(DSN) as reused:
            self.assertFalse(reused.autocommit, "归还时的复位照旧")
            self.assertIsNone(reused.read_only)

    def test_pgconn_and_anything_outside_the_forwarding_list_are_attribute_errors(self) -> None:
        with connect(DSN) as connection:
            for name in ("pgconn", "fileno", "deferrable", "pipeline", "discard", "probe"):
                with self.assertRaises(AttributeError, msg=name) as caught:
                    getattr(connection, name)
                self.assertIn("lingxi.adapters.postgres", str(caught.exception))
            self.assertFalse(hasattr(connection, "reset_for_reuse"))
            cursor = connection.cursor()
            for name in ("pgresult", "copy", "stream", "adapters", "row_factory"):
                with self.assertRaises(AttributeError, msg=name):
                    getattr(cursor, name)
        with self.assertRaises(AttributeError):
            connection.pgconn

    def test_cursors_point_back_to_the_handle_and_die_with_it(self) -> None:
        import psycopg

        connection = connect(DSN)
        explicit = connection.cursor()
        implicit = connection.execute("SELECT generate_series(1, 3)")
        self.assertIs(explicit.connection, connection)
        self.assertIs(implicit.connection, connection)
        self.assertIs(implicit.execute("SELECT 1"), implicit, "execute() 返回包装后的游标自身")
        rows = iter(connection.execute("SELECT generate_series(1, 3)"))
        self.assertEqual(next(rows), (1,))
        alive = connection.execute("SELECT generate_series(1, 2)")
        self.assertEqual(alive.rowcount, 2)
        self.assertEqual(alive.description[0].name, "generate_series")
        self.assertEqual(list(alive), [(1,), (2,)])
        connection.close()
        self.assertTrue(explicit.closed)
        self.assertTrue(implicit.closed)
        with self.assertRaises(psycopg.OperationalError):
            next(rows)
        for cursor in (explicit, implicit):
            with self.assertRaises(psycopg.OperationalError):
                cursor.execute("SELECT 1")
            with self.assertRaises(psycopg.OperationalError):
                cursor.fetchall()
            with self.assertRaises(psycopg.OperationalError):
                cursor.rowcount
            with self.assertRaises(psycopg.OperationalError):
                cursor.description
            with self.assertRaises(psycopg.OperationalError):
                with cursor:
                    self.fail("已失效的游标不得再进入 with 块")
            with self.assertRaises(psycopg.OperationalError):
                cursor.connection.commit()
        self.assertEqual(idle_connection_count(), 1)

    def test_transaction_blocks_keep_driver_semantics_through_the_wrapper(self) -> None:
        import psycopg

        with connect(DSN, dedicated=True) as setup:
            setup.execute("DROP TABLE IF EXISTS borrowed_tx_probe")
            setup.execute("CREATE TABLE borrowed_tx_probe (x int)")
            setup.commit()
        self.addCleanup(lambda: self.observer.execute("DROP TABLE IF EXISTS borrowed_tx_probe"))

        def committed() -> list[int]:
            rows = self.observer.execute("SELECT x FROM borrowed_tx_probe ORDER BY x").fetchall()
            return [row[0] for row in rows]

        with connect(DSN) as connection:
            with connection.transaction() as outer:
                self.assertIs(outer.connection, connection)
                self.assertFalse(outer.savepoint_name, "最外层事务没有保存点名")
                connection.execute("INSERT INTO borrowed_tx_probe VALUES (1)")
                with connection.transaction(savepoint_name="inner") as inner:
                    self.assertEqual(inner.savepoint_name, "inner")
                    connection.execute("INSERT INTO borrowed_tx_probe VALUES (2)")
                    raise psycopg.Rollback(inner)
                connection.execute("INSERT INTO borrowed_tx_probe VALUES (3)")
                self.assertEqual(outer.status, psycopg.Transaction.Status.ACTIVE)
            self.assertEqual(outer.status, psycopg.Transaction.Status.COMMITTED)
            self.assertEqual(committed(), [1, 3], "指名回滚只撤销内层保存点")
            with connection.transaction(force_rollback=True) as dry_run:
                self.assertTrue(dry_run.force_rollback)
                connection.execute("INSERT INTO borrowed_tx_probe VALUES (4)")
            self.assertEqual(committed(), [1, 3], "force_rollback 逐字转发")
            with self.assertRaises(psycopg.errors.DivisionByZero):
                with connection.transaction():
                    connection.execute("INSERT INTO borrowed_tx_probe VALUES (5)")
                    connection.execute("SELECT 1 / 0")
            self.assertEqual(committed(), [1, 3], "异常退出回滚")

    def test_notifies_is_forwarded_and_gated(self) -> None:
        import psycopg

        connection = connect(DSN)
        connection.execute("LISTEN borrowed_handle_probe")
        connection.commit()
        self.observer.execute("NOTIFY borrowed_handle_probe, 'ping'")
        received = list(connection.notifies(timeout=5, stop_after=1))
        self.assertEqual([n.payload for n in received], ["ping"])
        connection.close()
        with self.assertRaises(psycopg.OperationalError):
            connection.notifies(timeout=0)

    def test_dedicated_connections_are_the_driver_type_and_not_wrapped(self) -> None:
        with connect(DSN, dedicated=True) as dedicated:
            self.assertEqual(type(dedicated).__name__, "Connection")
            self.assertIsNotNone(dedicated.pgconn)
        with connect(DSN) as borrowed:
            self.assertEqual(type(borrowed).__name__, "_BorrowedConnection")
