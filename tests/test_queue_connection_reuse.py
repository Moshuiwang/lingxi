"""S-H1-6（#359 根因取证方案第 2 条）：常驻轮询路径的连接复用逻辑单测。

不连接真实数据库——只验证 ``_TaskQueueBase._connect_for_polling`` 本身的三条
路径（复用命中、失效重建、重建失败如实上抛）与默认关闭时逐字节保留的旧行为，
以及 ``PostgresTaskQueue.claim`` / ``list_pending_delivery_tasks`` 两个真正的
常驻轮询调用点确实经它取得连接（不是只有辅助方法本身对但没接线）。真实 SQL
正确性已经由 ``tests/test_gateway_postgres.py`` 等要求 ``LINGXI_POSTGRES_DSN``
的集成测试覆盖，不在本文件重复。
"""

from __future__ import annotations

import unittest
from unittest import mock

from lingxi.adapters.postgres_conversation import PostgresTaskQueue
from lingxi.adapters.postgres_conversation._queue_base import _TaskQueueBase


class _FakeCursor:
    def __init__(self, rows: list[tuple] | None = None) -> None:
        self.rows = rows or []
        self.executed: list[tuple] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self.executed.append((sql, params))

    def fetchall(self) -> list[tuple]:
        return self.rows

    def fetchone(self) -> tuple | None:
        return self.rows[0] if self.rows else None


class _FakeConnection:
    """模拟 psycopg3 ``Connection``：既支持"作为自己的上下文管理器"（旧的
    ``with connect(...) as connection:`` 路径——退出时提交/回滚并关闭），也支持
    被 ``_connect_for_polling`` 复用分支直接调用 ``.commit()``/``.close()``（不
    经它自己的 ``__exit__``，因此不会在每次使用后被关闭）。
    """

    def __init__(self, rows: list[tuple] | None = None) -> None:
        self.closed = False
        self.commit_calls = 0
        self.rollback_calls = 0
        self.close_calls = 0
        self._rows = rows or []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._rows)

    def commit(self) -> None:
        self.commit_calls += 1

    def rollback(self) -> None:
        self.rollback_calls += 1

    def close(self) -> None:
        self.closed = True
        self.close_calls += 1

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()
        return False


class ConnectionReuseHelperTests(unittest.TestCase):
    """直接测 ``_TaskQueueBase._connect_for_polling``，不经任何 mixin。"""

    def test_default_disabled_creates_a_fresh_connection_every_call(self) -> None:
        """回归防线：`reuse_polling_connection` 默认关闭，行为必须与本卡改动前
        逐字节一致——每次调用新建、用后即关。"""

        connections: list[_FakeConnection] = []

        def fake_connect(dsn: str, *, timeouts: object = None) -> _FakeConnection:
            connection = _FakeConnection()
            connections.append(connection)
            return connection

        base = _TaskQueueBase("postgresql://test/db")
        with mock.patch(
            "lingxi.adapters.postgres_conversation._queue_base.connect", fake_connect
        ):
            with base._connect_for_polling() as first:
                pass
            with base._connect_for_polling() as second:
                pass

        self.assertEqual(len(connections), 2)
        self.assertIsNot(first, second)
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)
        self.assertEqual(first.commit_calls, 1)

    def test_reuse_hit_avoids_a_new_physical_connection(self) -> None:
        """正路径：命中缓存直接复用，不重新握手，也不在正常使用后关闭。"""

        connections: list[_FakeConnection] = []

        def fake_connect(dsn: str, *, timeouts: object = None) -> _FakeConnection:
            connection = _FakeConnection()
            connections.append(connection)
            return connection

        base = _TaskQueueBase("postgresql://test/db", reuse_polling_connection=True)
        with mock.patch(
            "lingxi.adapters.postgres_conversation._queue_base.connect", fake_connect
        ):
            with base._connect_for_polling() as first:
                pass
            with base._connect_for_polling() as second:
                pass
            with base._connect_for_polling() as third:
                pass

        self.assertEqual(len(connections), 1, "命中缓存时不应该新建物理连接")
        self.assertIs(first, second)
        self.assertIs(second, third)
        self.assertFalse(first.closed)
        self.assertEqual(first.commit_calls, 3)
        self.assertEqual(first.close_calls, 0)

    def test_failed_connection_is_discarded_and_rebuilt_not_silently_reused(self) -> None:
        """失效重建 + 否定用例：一次使用中途失败之后，那条连接必须立即被丢弃
        （否定用例：不得被下一次调用静默复用），下一次调用重新建立一条新连接，
        原始异常原样向上抛出（不吞）。"""

        connections: list[_FakeConnection] = []

        def fake_connect(dsn: str, *, timeouts: object = None) -> _FakeConnection:
            connection = _FakeConnection()
            connections.append(connection)
            return connection

        base = _TaskQueueBase("postgresql://test/db", reuse_polling_connection=True)
        with mock.patch(
            "lingxi.adapters.postgres_conversation._queue_base.connect", fake_connect
        ):
            with base._connect_for_polling() as first:
                pass
            self.assertEqual(len(connections), 1)

            with self.assertRaises(RuntimeError):
                with base._connect_for_polling() as broken:
                    self.assertIs(broken, first)
                    raise RuntimeError("模拟查询时连接失效")

            # 否定用例：失败之后立刻丢弃，不留在缓存里等下一次被静默复用。
            self.assertIsNone(base._pooled_connection)
            self.assertTrue(first.closed, "失效的连接必须被真的关闭，不能假装还能用")

            with base._connect_for_polling() as rebuilt:
                pass

        self.assertEqual(len(connections), 2, "失效之后下一次调用必须重新建立物理连接")
        self.assertIsNot(rebuilt, first)

    def test_rebuild_failure_after_invalidation_propagates_honestly(self) -> None:
        """重建失败如实上抛：连接已失效、且重新建连本身也失败（数据库不可达）时，
        必须让原始异常穿透，不得吞掉、也不得假装拿到了一条可用连接。"""

        calls = {"count": 0}

        def flaky_connect(dsn: str, *, timeouts: object = None) -> _FakeConnection:
            calls["count"] += 1
            if calls["count"] == 1:
                return _FakeConnection()
            raise ConnectionError("数据库不可达")

        base = _TaskQueueBase("postgresql://test/db", reuse_polling_connection=True)
        with mock.patch(
            "lingxi.adapters.postgres_conversation._queue_base.connect", flaky_connect
        ):
            with base._connect_for_polling() as first:
                pass

            with self.assertRaises(RuntimeError):
                with base._connect_for_polling() as broken:
                    raise RuntimeError("先让这次使用失败，逼出下一次重建")

            with self.assertRaises(ConnectionError):
                with base._connect_for_polling():
                    pass  # 不会走到这里——重建阶段就应该抛出

        self.assertIsNone(
            base._pooled_connection, "重建失败后不能把半成品连接留在缓存里"
        )


class QueueHotPathWiringTests(unittest.TestCase):
    """确认真正的常驻轮询调用点（`claim`/`list_pending_delivery_tasks`）真的
    接了 `_connect_for_polling`，不是只有辅助方法本身对但没接线。"""

    def test_claim_reuses_the_same_connection_across_polls(self) -> None:
        connections: list[_FakeConnection] = []

        def fake_connect(dsn: str, *, timeouts: object = None) -> _FakeConnection:
            connection = _FakeConnection(rows=[])
            connections.append(connection)
            return connection

        queue = PostgresTaskQueue("postgresql://test/db", reuse_polling_connection=True)
        with mock.patch(
            "lingxi.adapters.postgres_conversation._queue_base.connect", fake_connect
        ):
            queue.claim(worker_id="w1", target_worker_version="stable", limit=1)
            queue.claim(worker_id="w1", target_worker_version="stable", limit=1)
            queue.claim(worker_id="w1", target_worker_version="stable", limit=1)

        self.assertEqual(len(connections), 1, "worker 主循环连续三次 claim() 不应该建三条新连接")
        self.assertEqual(connections[0].commit_calls, 3)
        self.assertFalse(connections[0].closed)

    def test_gateway_delivery_discovery_queries_reuse_the_same_connection(self) -> None:
        connections: list[_FakeConnection] = []

        def fake_connect(dsn: str, *, timeouts: object = None) -> _FakeConnection:
            connection = _FakeConnection(rows=[])
            connections.append(connection)
            return connection

        queue = PostgresTaskQueue("postgresql://test/db", reuse_polling_connection=True)
        with mock.patch(
            "lingxi.adapters.postgres_conversation._queue_base.connect", fake_connect
        ):
            queue.list_pending_delivery_tasks(limit=20)
            queue.list_uncertain_delivery_tasks(limit=50)
            queue.list_pending_delivery_tasks(limit=20)

        self.assertEqual(
            len(connections), 1, "gateway 投递循环一轮内的两条发现查询不应该各建一条新连接"
        )
        self.assertEqual(connections[0].commit_calls, 3)


if __name__ == "__main__":
    unittest.main()
