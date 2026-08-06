"""真实传输层 ``LarkEventTransport`` 的断言（桩 SDK，不连真实飞书）。

此前这一层零覆盖：ack 时机、事件循环、异常翻译全靠读代码判断。桩模块沿用仓库既有
写法（``tests/test_claude_agent_hooks_adapter.py`` 的 ``sys.modules`` 替换）。

认领断言：`V-接入-06`（异常翻译）、`V-部署-03`（空闲心跳）、以及两条本轮修复的回归：

- **ack 必须晚于落库**：SDK 在 ``_do_without_validation`` 返回后立刻回 OK 帧
  （``lark_oapi/ws/client.py:336`` 起），因此该方法必须阻塞到落库有结果；
  失败与超时都要抛给 SDK，让它回 500 由飞书重投。
- **重连不得撞上旧的事件循环**：SDK 的 loop 是模块级全局，旧连接的 ``start()``
  永不返回、把它占着 running，第二次建连会在 ``run_until_complete`` 上直接
  ``RuntimeError``——重连一次就永久失聪。
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import types
import unittest

from lingxi.adapters.feishu_longconn import (
    AUTH_FAILED,
    EXCEED_CONN_LIMIT,
    FORBIDDEN,
    FailureSource,
    PendingEvent,
    _RawEventSink,
    translate_sdk_exception,
)


class AckTimingTests(unittest.TestCase):
    """``_RawEventSink`` 的 ack 语义。"""

    def _sink(self, *, timeout: float = 1.0):
        submitted: list[PendingEvent] = []
        return _RawEventSink(submitted.append, ack_timeout_seconds=timeout), submitted

    def test_it_blocks_until_the_pipeline_reports_success(self) -> None:
        sink, submitted = self._sink()
        returned = threading.Event()

        def deliver() -> None:
            sink._do_without_validation(b'{"header": {"event_id": "e"}}')
            returned.set()

        thread = threading.Thread(target=deliver, daemon=True)
        thread.start()

        # 落库还没有结果时，handler 绝不能返回——返回就等于告诉飞书「已投递」。
        self.assertFalse(
            returned.wait(0.3), "落库尚无结果时 handler 就返回了，消息会在崩溃窗口里丢失"
        )
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0].payload["header"]["event_id"], "e")

        submitted[0].complete(None)
        self.assertTrue(returned.wait(2), "落库成功后 handler 必须返回")

    def test_a_failed_pipeline_raises_so_feishu_will_redeliver(self) -> None:
        sink, submitted = self._sink()
        outcome: list[BaseException] = []

        def deliver() -> None:
            try:
                sink._do_without_validation(b"{}")
            except BaseException as error:  # noqa: BLE001
                outcome.append(error)

        thread = threading.Thread(target=deliver, daemon=True)
        thread.start()
        while not submitted:
            pass
        submitted[0].complete(RuntimeError("注入落库失败"))
        thread.join(timeout=2)

        self.assertEqual(len(outcome), 1, "落库失败必须向 SDK 抛出，让它回 500")
        self.assertIsInstance(outcome[0], RuntimeError)

    def test_it_gives_up_after_the_ack_timeout(self) -> None:
        """永远等不到结果时也必须抛，而不是无限期占住 SDK 的接收协程。"""

        sink, _ = self._sink(timeout=0.2)
        with self.assertRaises(TimeoutError):
            sink._do_without_validation(b"{}")


class ExceptionTranslationTests(unittest.TestCase):
    """`V-接入-06`：SDK 异常 → 我们自己的 ``HandshakeFailure``。"""

    def setUp(self) -> None:
        module = types.ModuleType("lark_oapi.ws.exception")

        class ClientException(Exception):
            def __init__(self, code, msg=""):
                super().__init__(msg)
                self.code = code

        class ServerException(Exception):
            def __init__(self, code, msg=""):
                super().__init__(msg)
                self.code = code

        module.ClientException = ClientException
        module.ServerException = ServerException
        self.module = module
        saved = sys.modules.get("lark_oapi.ws.exception")
        sys.modules["lark_oapi.ws.exception"] = module

        def restore() -> None:
            if saved is None:
                sys.modules.pop("lark_oapi.ws.exception", None)
            else:
                sys.modules["lark_oapi.ws.exception"] = saved

        self.addCleanup(restore)

    def test_ws_handshake_403_becomes_a_terminal_shaped_failure(self) -> None:
        failure = translate_sdk_exception(self.module.ClientException(FORBIDDEN))
        self.assertEqual(failure.source, FailureSource.WS_HANDSHAKE)
        self.assertEqual(failure.status_code, FORBIDDEN)

    def test_client_exception_514_carries_the_exceed_connection_autherrcode(self) -> None:
        """SDK 只在「514 且超连接数上限」这一种组合下抛 ClientException。"""

        failure = translate_sdk_exception(self.module.ClientException(AUTH_FAILED))
        self.assertEqual(failure.auth_errcode, EXCEED_CONN_LIMIT)

    def test_server_exception_is_the_endpoint_http_path(self) -> None:
        failure = translate_sdk_exception(self.module.ServerException(500))
        self.assertEqual(failure.source, FailureSource.ENDPOINT_HTTP)
        self.assertIsNone(failure.auth_errcode)

    def test_an_unrelated_exception_is_a_stream_failure(self) -> None:
        failure = translate_sdk_exception(OSError("连接被重置"))
        self.assertEqual(failure.source, FailureSource.STREAM)


class _StubSDKTestCase(unittest.TestCase):
    """把 ``lark_oapi`` 换成桩，让 ``LarkEventTransport.stream()`` 可以真的跑一遍。"""

    THREAD_NAME = "lingxi-gateway-longconn"

    def setUp(self) -> None:
        test_case = self

        class StubClient:
            def __init__(self, app_id, app_secret, event_handler=None, auto_reconnect=None):
                self.event_handler = event_handler
                self._conn = None
                test_case.clients.append(self)

            def start(self):
                # 与真实 SDK 同形：建连成功后驻留在**模块级** loop 上不返回。
                self._conn = object()
                test_case.started.append(self)
                ws_client = sys.modules["lark_oapi.ws.client"]
                ws_client.loop.run_until_complete(asyncio.sleep(30))

        self.clients: list[StubClient] = []
        self.started: list[StubClient] = []

        ws_client_module = types.ModuleType("lark_oapi.ws.client")
        ws_client_module.loop = asyncio.new_event_loop()
        self.initial_loop = ws_client_module.loop

        ws_module = types.ModuleType("lark_oapi.ws")
        ws_module.Client = StubClient
        ws_module.client = ws_client_module

        lark_module = types.ModuleType("lark_oapi")
        lark_module.ws = ws_module

        self._saved = {
            name: sys.modules.get(name)
            for name in ("lark_oapi", "lark_oapi.ws", "lark_oapi.ws.client")
        }
        sys.modules["lark_oapi"] = lark_module
        sys.modules["lark_oapi.ws"] = ws_module
        sys.modules["lark_oapi.ws.client"] = ws_client_module
        self.ws_client_module = ws_client_module
        self.addCleanup(self._restore)
        self.addCleanup(self.initial_loop.close)

    def _restore(self) -> None:
        for name, saved in self._saved.items():
            if saved is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved

    def _living_pump_threads(self) -> list[threading.Thread]:
        return [t for t in threading.enumerate() if t.name == self.THREAD_NAME and t.is_alive()]


class EventLoopCollisionTests(_StubSDKTestCase):
    """重连必须拿到一个干净的事件循环，且旧连接的线程不能泄漏。"""

    def _transport(self):
        from lingxi.adapters.feishu_longconn import LarkEventTransport

        return LarkEventTransport(
            app_id="cli_fake", app_secret="fake", poll_seconds=0.05, ack_timeout_seconds=1
        )

    def test_each_connection_gets_a_fresh_loop(self) -> None:
        transport = self._transport()
        loops = []

        for _ in range(2):
            stream = transport.stream()
            next(stream)  # 触发建连；空闲时拿到一个心跳
            loops.append(self.ws_client_module.loop)
            stream.close()

        self.assertEqual(len(self.started), 2, "两次都应真的建过连")
        self.assertIsNot(
            loops[0],
            loops[1],
            "第二次建连复用了上一条连接占着的事件循环——真实 SDK 会在 "
            "run_until_complete 上直接 RuntimeError，重连一次就永久失聪",
        )
        self.assertIsNot(loops[0], self.initial_loop, "首次建连也应换成自己的 loop")

    def test_the_previous_connection_thread_does_not_leak(self) -> None:
        transport = self._transport()

        stream = transport.stream()
        next(stream)
        self.assertEqual(len(self._living_pump_threads()), 1)
        stream.close()

        deadline = threading.Event()
        for _ in range(100):
            if not self._living_pump_threads():
                break
            deadline.wait(0.05)

        self.assertEqual(
            self._living_pump_threads(),
            [],
            "旧连接的线程没有退出：每次重连都会漏一个永久阻塞的线程",
        )


class HeartbeatTests(_StubSDKTestCase):
    """空闲时必须产出心跳，否则停机信号在没有消息时永远看不见（`V-部署-03`）。"""

    def test_an_idle_connection_yields_heartbeats(self) -> None:
        from lingxi.adapters.feishu_longconn import LarkEventTransport

        transport = LarkEventTransport(
            app_id="cli_fake", app_secret="fake", poll_seconds=0.02, ack_timeout_seconds=1
        )
        stream = transport.stream()
        try:
            beats = [next(stream) for _ in range(3)]
        finally:
            stream.close()

        self.assertEqual(beats, [None, None, None], "空闲时应持续产出心跳而不是阻塞")

    def test_a_delivered_event_flows_through_and_is_acknowledged(self) -> None:
        """事件穿过传输层，report 之后 SDK 侧的 handler 才解除阻塞。"""

        from lingxi.adapters.feishu_longconn import LarkEventTransport

        transport = LarkEventTransport(
            app_id="cli_fake", app_secret="fake", poll_seconds=0.02, ack_timeout_seconds=5
        )
        stream = transport.stream()
        try:
            next(stream)  # 建连
            client = self.clients[0]
            acked = threading.Event()

            def deliver() -> None:
                client.event_handler._do_without_validation(
                    json.dumps({"header": {"event_id": "evt_1"}}).encode("utf-8")
                )
                acked.set()

            threading.Thread(target=deliver, daemon=True).start()

            payload = None
            for _ in range(200):
                payload = next(stream)
                if payload is not None:
                    break
            self.assertIsNotNone(payload, "投递的事件应当从 stream 里出来")
            self.assertEqual(payload["header"]["event_id"], "evt_1")
            self.assertFalse(acked.wait(0.2), "还没 report，SDK 侧不该已经收到 ack")

            transport.report(payload, None)
            self.assertTrue(acked.wait(2), "report 之后 SDK 侧才应解除阻塞")
        finally:
            stream.close()


if __name__ == "__main__":
    unittest.main()
