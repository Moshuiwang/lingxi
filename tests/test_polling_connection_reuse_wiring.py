"""P2-2（opus 批量审查 · Trace #373 H1 批终修复包）：``reuse_polling_connection=True``
的两处真实装配接线断言。

S-H1-6/#359 打开的常驻轮询连接复用只有两个真实调用点会真的打开它——
``apps/gateway/__init__.py::assemble_delivery_consumer``（不传 ``queue`` 时自己
新建的实例）与 ``apps/worker/cli.py::main`` 的 queue 模式。``tests/test_queue_
connection_reuse.py`` 只测 ``_TaskQueueBase``/``PostgresTaskQueue`` 本身对不对
（直接传 ``reuse_polling_connection=True`` 构造），测不出"装配层真的把这个参数
接上了没有"——删掉这两处 ``reuse_polling_connection=True`` 关键字参数，全量测试
此前不会有任何一条变红。这里各补一条最小装配级断言钉住它们，形状参照
``tests/test_scheduler_onboarding_assembly.py::ShutdownWiringTests``（同一批
#381 先例）：断言装配产出的真实对象带着预期的接线状态，不打桩掉被测的那一个
关键字参数本身。
"""

from __future__ import annotations

import io
import sys
import tempfile
import types
import unittest
from unittest import mock

from lingxi.adapters.postgres_conversation import PostgresTaskQueue


class GatewayDeliveryConsumerReuseWiringTests(unittest.TestCase):
    """``assemble_delivery_consumer`` 不传 ``queue`` 时，自己新建的
    ``PostgresTaskQueue`` 必须打开常驻轮询连接复用。"""

    def setUp(self) -> None:
        # 同 tests/test_gateway_config.py::AssembleDeliveryConsumerCardInjectionTests：
        # build_client 会 import lark_oapi，CI 的 gate 只装 scheduler 组，没有它，
        # 用桩顶上。
        module = types.ModuleType("lark_oapi")

        class _Builder:
            def app_id(self, value: object) -> _Builder:
                return self

            def app_secret(self, value: object) -> _Builder:
                return self

            def timeout(self, value: object) -> _Builder:
                return self

            def build(self) -> object:
                return object()

        module.Client = types.SimpleNamespace(builder=lambda: _Builder())
        saved = sys.modules.get("lark_oapi")
        sys.modules["lark_oapi"] = module
        self.addCleanup(
            lambda: sys.modules.__setitem__("lark_oapi", saved)
            if saved is not None
            else sys.modules.pop("lark_oapi", None)
        )

    def test_the_self_built_queue_has_polling_connection_reuse_enabled(self) -> None:
        from lingxi.apps.gateway import assemble_delivery_consumer
        from lingxi.apps.gateway.config import ENV_PREFIX, load_config

        env = {
            f"{ENV_PREFIX}APP_ID": "cli_fake_app_id",
            f"{ENV_PREFIX}APP_SECRET": "fake-secret-for-tests-only-8Xq2",
            f"{ENV_PREFIX}POSTGRES_DSN": "postgresql://lingxi:x@db.invalid/lingxi",
        }
        config = load_config(env)

        with mock.patch("lingxi.apps.gateway.delivery.build_delivery_consumer") as builder:
            assemble_delivery_consumer(config)

        queue = builder.call_args.kwargs["queue"]
        self.assertIsInstance(queue, PostgresTaskQueue)
        self.assertTrue(
            queue._reuse_polling_connection,
            "gateway 常驻投递消费循环自己新建的队列必须打开常驻轮询连接复用"
            "（S-H1-6）——删掉这个关键字参数不该继续全绿",
        )


class WorkerQueueModeReuseWiringTests(unittest.TestCase):
    """queue 模式装配出的 ``WorkerService`` 持有的队列必须打开常驻轮询连接
    复用。"""

    def test_the_queue_worker_service_has_polling_connection_reuse_enabled(self) -> None:
        from lingxi.apps.worker.cli import main

        captured: dict[str, object] = {}

        async def _stub_run_queue_worker(service: object, **kwargs: object) -> None:
            # 立即返回，不真的跑队列消费循环、不连接数据库——只截获装配好的
            # WorkerService，供下面检查它持有的队列对象。
            captured["service"] = service
            return None

        with tempfile.TemporaryDirectory() as directory:
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch(
                "lingxi.apps.worker.cli._run_queue_worker", _stub_run_queue_worker
            ):
                code = main(
                    env={
                        "LINGXI_WORKER_MODE": "queue",
                        "LINGXI_WORKER_READONLY_TOOLS": "mcp__query__noop",
                        "LINGXI_WORKER_TRACE_ID": "01J00000000000000000000WKR",
                        "LINGXI_POSTGRES_DSN": "postgresql://user:pass@localhost:5432/does-not-matter",
                        "LINGXI_USER_ENV_ROOT": directory,
                    },
                    stdout=stdout,
                    stderr=stderr,
                )

        self.assertEqual(code, 0, "预检齐全时 queue 模式必须正常越过启动、进入队列消费循环")
        service = captured["service"]
        queue = service._queue
        self.assertIsInstance(queue, PostgresTaskQueue)
        self.assertTrue(
            queue._reuse_polling_connection,
            "queue 模式 worker 主循环使用的队列必须打开常驻轮询连接复用"
            "（S-H1-6）——删掉这个关键字参数不该继续全绿",
        )


if __name__ == "__main__":
    unittest.main()
