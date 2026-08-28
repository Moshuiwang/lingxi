"""S-H1-6（#359 根因取证方案第 2 条）：常驻轮询路径的连接复用逻辑单测。

不连接真实数据库——只验证 ``_TaskQueueBase._run_polling_operation`` 本身的路径
（复用命中、失效重建重试成功、重试仍失败如实上抛、新建连接失败不重试、默认关闭时
逐字节保留的旧行为）与 ``PostgresTaskQueue.claim`` / ``list_pending_delivery_tasks``
两个真正的常驻轮询调用点确实经它取得连接（不是只有辅助方法本身对但没接线）。真实
SQL 正确性已经由 ``tests/test_gateway_postgres.py`` 等要求 ``LINGXI_POSTGRES_DSN``
的集成测试覆盖，不在本文件重复。

P2-1（opus 批量审查 · Trace #373 H1 批终修复包）：``_run_polling_operation`` 此前
（``_connect_for_polling``）任何失败都「丢弃连接 + 原样上抛」——复用连接在两次轮询
之间被服务端悄悄掐断（库本身健康）这种场景会让 worker 的 ``claim()`` 上抛一次，而
worker 主循环没有兜底，代价是整个进程崩溃、靠容器重启。本文件下半部分新增的三条
用例钉住修复后的行为：复用连接首次失败→重建重试成功（调用方完全看不到这次失败）；
重试仍失败（含重建连接本身失败）→原样上抛，不吞、不再重试第二次；新建的连接失败
→不重试，直接原样上抛（避免"一次调用悄悄建两条新连接才放弃"）。方法从直接
``yield`` 连接的上下文管理器改成接收一个 ``operation`` 可调用对象：重试要把同一次
查询完整重跑一遍，而 ``with`` 块体在异常抛出后不可能被同一个上下文管理器再次执行，
只有把"这次要做什么"整体收进一个可调用对象，失败重试才有东西可以重新调用（见
``_queue_base.py::_run_polling_operation`` 的文档字符串）。
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
    """模拟 psycopg3 ``Connection``：既支持"作为自己的上下文管理器"（默认关闭
    复用时的 ``with connect(...) as connection:`` 路径——退出时提交/回滚并
    关闭），也支持被 ``_run_polling_operation`` 复用分支直接调用
    ``.commit()``/``.close()``（不经它自己的 ``__exit__``，因此不会在每次使用
    后被关闭）。
    """

    def __init__(self, rows: list[tuple] | None = None, *, commit_error: Exception | None = None) -> None:
        self.closed = False
        self.commit_calls = 0
        self.rollback_calls = 0
        self.close_calls = 0
        self._rows = rows or []
        # P1-A（codex 外审 · Trace #373 H1 批终修复包②）：可选地让 `commit()`
        # 抛出，模拟"COMMIT 回执因断线丢失"这种结果不确定的场景——不管抛不抛，
        # `commit_calls` 都照常计数，用来断言 `commit()` 确实被调用过一次。
        self._commit_error = commit_error

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._rows)

    def commit(self) -> None:
        self.commit_calls += 1
        if self._commit_error is not None:
            raise self._commit_error

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
    """直接测 ``_TaskQueueBase._run_polling_operation``，不经任何 mixin。"""

    def test_default_disabled_creates_a_fresh_connection_every_call(self) -> None:
        """回归防线：`reuse_polling_connection` 默认关闭，行为必须与本卡改动前
        逐字节一致——每次调用新建、用后即关。"""

        connections: list[_FakeConnection] = []
        seen: list[_FakeConnection] = []

        def fake_connect(dsn: str, *, timeouts: object = None) -> _FakeConnection:
            connection = _FakeConnection()
            connections.append(connection)
            return connection

        def operation(connection: _FakeConnection) -> None:
            seen.append(connection)

        base = _TaskQueueBase("postgresql://test/db")
        with mock.patch(
            "lingxi.adapters.postgres_conversation._queue_base.connect", fake_connect
        ):
            base._run_polling_operation(operation)
            base._run_polling_operation(operation)

        self.assertEqual(len(connections), 2)
        self.assertIsNot(seen[0], seen[1])
        self.assertTrue(connections[0].closed)
        self.assertTrue(connections[1].closed)
        self.assertEqual(connections[0].commit_calls, 1)

    def test_reuse_hit_avoids_a_new_physical_connection(self) -> None:
        """正路径：命中缓存直接复用，不重新握手，也不在正常使用后关闭。"""

        connections: list[_FakeConnection] = []
        seen: list[_FakeConnection] = []

        def fake_connect(dsn: str, *, timeouts: object = None) -> _FakeConnection:
            connection = _FakeConnection()
            connections.append(connection)
            return connection

        def operation(connection: _FakeConnection) -> None:
            seen.append(connection)

        base = _TaskQueueBase("postgresql://test/db", reuse_polling_connection=True)
        with mock.patch(
            "lingxi.adapters.postgres_conversation._queue_base.connect", fake_connect
        ):
            base._run_polling_operation(operation)
            base._run_polling_operation(operation)
            base._run_polling_operation(operation)

        self.assertEqual(len(connections), 1, "命中缓存时不应该新建物理连接")
        self.assertIs(seen[0], seen[1])
        self.assertIs(seen[1], seen[2])
        self.assertFalse(connections[0].closed)
        self.assertEqual(connections[0].commit_calls, 3)
        self.assertEqual(connections[0].close_calls, 0)

    def test_reused_connection_first_failure_retries_once_and_succeeds(self) -> None:
        """P2-1 正路径：复用连接首次失败——服务端在两次轮询之间悄悄断开、库本身
        健康——丢弃后重建连接重试一次；重试成功时调用方完全看不到中间那次失败
        （不上抛），且新连接留在缓存里供下一轮继续复用。"""

        connections: list[_FakeConnection] = []

        def fake_connect(dsn: str, *, timeouts: object = None) -> _FakeConnection:
            connection = _FakeConnection()
            connections.append(connection)
            return connection

        base = _TaskQueueBase("postgresql://test/db", reuse_polling_connection=True)
        with mock.patch(
            "lingxi.adapters.postgres_conversation._queue_base.connect", fake_connect
        ):
            base._run_polling_operation(lambda connection: None)  # 先建立缓存里的第一条连接
            self.assertEqual(len(connections), 1)
            first = connections[0]

            calls = {"count": 0}
            seen: list[_FakeConnection] = []

            def flaky_operation(connection: _FakeConnection) -> str:
                calls["count"] += 1
                seen.append(connection)
                if calls["count"] == 1:
                    raise RuntimeError("模拟服务端悄悄断开这条复用连接")
                return "ok"

            result = base._run_polling_operation(flaky_operation)

        self.assertEqual(result, "ok", "重试成功后调用方应该拿到正常结果，看不到中间那次失败")
        self.assertEqual(calls["count"], 2, "必须真的重试了一次，不是吞掉失败什么都不做")
        self.assertEqual(len(connections), 2, "复用连接失败后必须重建一条新的物理连接")
        self.assertIs(seen[0], first, "第一次尝试用的应该是缓存里那条复用连接")
        self.assertIsNot(seen[1], first, "重试用的必须是新建的连接，不能是同一条已经坏掉的连接")
        self.assertTrue(first.closed, "失效的复用连接必须被真的关闭")
        self.assertIs(
            base._pooled_connection, connections[1], "重试成功后新连接应留在缓存里供下一轮复用"
        )
        self.assertEqual(connections[1].commit_calls, 1)

    def test_reused_connection_retry_still_fails_propagates_honestly(self) -> None:
        """P2-1 否定用例：重试（用重建的新连接）仍然失败时必须原样上抛，不吞、
        不再重试第二次——不得把"重试过一次"伪装成"最终成功"。"""

        connections: list[_FakeConnection] = []

        def fake_connect(dsn: str, *, timeouts: object = None) -> _FakeConnection:
            connection = _FakeConnection()
            connections.append(connection)
            return connection

        base = _TaskQueueBase("postgresql://test/db", reuse_polling_connection=True)
        with mock.patch(
            "lingxi.adapters.postgres_conversation._queue_base.connect", fake_connect
        ):
            base._run_polling_operation(lambda connection: None)
            self.assertEqual(len(connections), 1)

            def always_fails(connection: _FakeConnection) -> None:
                raise RuntimeError("每次使用都失败")

            with self.assertRaises(RuntimeError):
                base._run_polling_operation(always_fails)

        self.assertEqual(len(connections), 2, "应该恰好重试一次（新建一条），不会无限重试")
        self.assertIsNone(base._pooled_connection, "重试仍失败后不能把坏连接留在缓存里")
        self.assertTrue(connections[0].closed)
        self.assertTrue(connections[1].closed)

    def test_reused_connection_rebuild_itself_fails_propagates_honestly(self) -> None:
        """P2-1 否定用例的另一种形状：重建阶段本身失败（数据库不可达），同样必须
        如实上抛，不能假装拿到了一条可用连接。"""

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
            base._run_polling_operation(lambda connection: None)

            def fails_once(connection: _FakeConnection) -> None:
                raise RuntimeError("先让首次使用失败，逼出下一次重建")

            with self.assertRaises(ConnectionError):
                base._run_polling_operation(fails_once)

        self.assertIsNone(
            base._pooled_connection, "重建失败后不能把半成品连接留在缓存里"
        )

    def test_newly_built_connection_failure_does_not_retry(self) -> None:
        """P2-1 边界：新建的连接（缓存为空/已关闭，本次调用现建）首次失败直接
        原样上抛，不重试——重试只对"复用命中"生效，避免一次调用悄悄建两条新
        连接才放弃。"""

        connections: list[_FakeConnection] = []

        def fake_connect(dsn: str, *, timeouts: object = None) -> _FakeConnection:
            connection = _FakeConnection()
            connections.append(connection)
            return connection

        base = _TaskQueueBase("postgresql://test/db", reuse_polling_connection=True)
        with mock.patch(
            "lingxi.adapters.postgres_conversation._queue_base.connect", fake_connect
        ):

            def fails(connection: _FakeConnection) -> None:
                raise RuntimeError("新连接刚建好就失败")

            with self.assertRaises(RuntimeError):
                base._run_polling_operation(fails)

        self.assertEqual(len(connections), 1, "新建连接失败不应该重试、不应该再建第二条连接")
        self.assertIsNone(base._pooled_connection)
        self.assertTrue(connections[0].closed)

    def test_commit_failure_is_never_replayed(self) -> None:
        """P1-A（codex 外审 · Trace #373 H1 批终修复包②）：**复用命中**的连接上
        ``operation()`` 成功、但 ``commit()`` 抛错（模拟 COMMIT 回执因断线丢失、
        数据库端其实已经提交）——这次异常必须原样上抛，绝不能被当成"复用连接
        首次失败"走重试分支，否则会把已经提交过的 ``operation``（例如
        ``claim()``）完整重放一遍，第二次再抢一批任务，第一批已经 ``running``
        却无人认领。

        必须先建立一条**真正复用命中**（``reused=True``）的缓存连接再触发这次
        commit 失败——不这样做的话，`_run_polling_operation` 第一次调用本身走的
        是"新建连接"分支（``reused=False``），那条分支任何失败都不重试，测不出
        "复用命中 + commit 失败"这条专属路径的行为。
        """

        connections: list[_FakeConnection] = []

        def fake_connect(dsn: str, *, timeouts: object = None) -> _FakeConnection:
            connection = _FakeConnection()
            connections.append(connection)
            return connection

        base = _TaskQueueBase("postgresql://test/db", reuse_polling_connection=True)
        with mock.patch(
            "lingxi.adapters.postgres_conversation._queue_base.connect", fake_connect
        ):
            base._run_polling_operation(lambda connection: None)  # 建立缓存里的复用连接
            self.assertEqual(len(connections), 1)
            first = connections[0]
            first._commit_error = RuntimeError("COMMIT 回执因断线丢失")

            calls = {"count": 0}

            def operation(connection: _FakeConnection) -> str:
                calls["count"] += 1
                return "claimed"

            with self.assertRaises(RuntimeError):
                base._run_polling_operation(operation)

        self.assertEqual(calls["count"], 1, "commit() 失败不得触发 operation 重放")
        self.assertEqual(len(connections), 1, "commit() 阶段失败不应该重建第二条连接去重试")
        self.assertEqual(
            first.commit_calls,
            2,
            "建立缓存那一次成功 commit 一次，本次失败 commit 又一次，一共两次",
        )
        self.assertTrue(first.closed, "commit() 失败的连接必须被丢弃")
        self.assertIsNone(base._pooled_connection, "commit() 失败后不能把这条连接留在缓存里")

    def test_commit_failure_after_a_successful_retry_is_also_not_replayed(self) -> None:
        """P1-A 的重试分支同样拆分：复用连接首次失败触发重试，重试的
        ``operation()`` 成功后如果重建连接的 ``commit()`` 又失败，同样只丢弃 +
        原样上抛，不再重试第二次。"""

        connections: list[_FakeConnection] = []

        def fake_connect(dsn: str, *, timeouts: object = None) -> _FakeConnection:
            connection = _FakeConnection()
            connections.append(connection)
            return connection

        base = _TaskQueueBase("postgresql://test/db", reuse_polling_connection=True)
        with mock.patch(
            "lingxi.adapters.postgres_conversation._queue_base.connect", fake_connect
        ):
            base._run_polling_operation(lambda connection: None)  # 建立缓存里的第一条连接
            self.assertEqual(len(connections), 1)

            # 第二条连接（重试用）的 operation 阶段能成功，但 commit() 会抛错。
            def flaky_connect(dsn: str, *, timeouts: object = None) -> _FakeConnection:
                connection = _FakeConnection(commit_error=RuntimeError("重试后 COMMIT 依然失败"))
                connections.append(connection)
                return connection

            calls = {"count": 0}

            def operation(connection: _FakeConnection) -> str:
                calls["count"] += 1
                if calls["count"] == 1:
                    raise RuntimeError("模拟服务端悄悄断开这条复用连接")
                return "ok"

            with mock.patch(
                "lingxi.adapters.postgres_conversation._queue_base.connect", flaky_connect
            ):
                with self.assertRaises(RuntimeError):
                    base._run_polling_operation(operation)

        self.assertEqual(calls["count"], 2, "operation 应该恰好重试一次（首次失败 + 重试成功）")
        self.assertEqual(len(connections), 2, "重试只应该重建一条新连接，不因 commit() 失败再建第三条")
        self.assertEqual(connections[1].commit_calls, 1)
        self.assertTrue(connections[1].closed, "重试后 commit() 失败的连接必须被丢弃")
        self.assertIsNone(base._pooled_connection, "commit() 失败后不能把这条连接留在缓存里")


class QueueHotPathWiringTests(unittest.TestCase):
    """确认真正的常驻轮询调用点（`claim`/`list_pending_delivery_tasks`）真的
    接了 `_run_polling_operation`，不是只有辅助方法本身对但没接线。"""

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

    def test_claim_recovers_from_a_reused_connection_dropped_between_polls(self) -> None:
        """P2-1 端到端：``claim()`` 真正接住了重试——服务端在两次 poll 之间悄悄
        断开复用连接，第三次 ``claim()`` 不再让 worker 主循环看到异常。"""

        connections: list[_FakeConnection] = []

        class _DroppingThenFakeConnection(_FakeConnection):
            def __init__(self, *, fail: bool, rows: list[tuple] | None = None) -> None:
                super().__init__(rows=rows)
                self._fail = fail

            def cursor(self) -> _FakeCursor:
                if self._fail:
                    raise RuntimeError("模拟服务端悄悄断开这条复用连接")
                return super().cursor()

        def fake_connect(dsn: str, *, timeouts: object = None) -> _FakeConnection:
            # 新建的连接一律健康；第一条连接会在第一次 claim() 之后被外部手动
            # 标记为"已断开"，模拟服务端在两次轮询之间悄悄掐断它。
            connection = _DroppingThenFakeConnection(fail=False, rows=[])
            connections.append(connection)
            return connection

        queue = PostgresTaskQueue("postgresql://test/db", reuse_polling_connection=True)
        with mock.patch(
            "lingxi.adapters.postgres_conversation._queue_base.connect", fake_connect
        ):
            queue.claim(worker_id="w1", target_worker_version="stable", limit=1)
            connections[0]._fail = True  # 模拟服务端在两次轮询之间悄悄掐断
            result = queue.claim(worker_id="w1", target_worker_version="stable", limit=1)

        self.assertEqual(result, [], "重试成功后 claim() 必须正常返回，不向 worker 主循环上抛")
        self.assertEqual(len(connections), 2, "复用连接失效之后必须重建一条新连接")
        self.assertTrue(connections[0].closed)


if __name__ == "__main__":
    unittest.main()
