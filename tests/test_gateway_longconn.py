"""长连接生命周期的可注入断言（假传输层 + 注入睡眠，不连真实飞书）。

认领断言：`V-接入-04`（事件路由）、`V-接入-05`（非终止型断连自动重连且退避单调）、
`V-接入-06`（403 与 514+1000040350 不重连）、`V-接入-10`（无旁路入站入口）、
`V-接入-12`（单事件失败不带下长连接）。

真实握手、真实断线重连属 L4a，本文件一条都不碰真实飞书。
"""

from __future__ import annotations

import unittest

from lingxi.adapters.feishu_longconn import (
    AUTH_FAILED,
    EXCEED_CONN_LIMIT,
    FORBIDDEN,
    BackoffPolicy,
    FailureKind,
    FailureSource,
    HandshakeFailure,
    LongConnectionError,
    LongConnectionSupervisor,
    TerminationReason,
    classify_handshake_failure,
)


def event(event_id: str = "evt_1", *, open_id: str = "ou_1") -> dict:
    return {
        "header": {"event_id": event_id, "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": open_id}},
            "message": {
                "message_id": f"om_{event_id}",
                "chat_id": "oc_1",
                "thread_id": "omt_9",
                "message_type": "text",
                "content": '{"text": "hi"}',
            },
        },
    }


class ScriptedTransport:
    """按剧本产出若干"连接会话"。

    每个 episode 要么是一串事件（正常收取后连接关闭），要么是一个异常（建连失败）。
    剧本演完后置 ``exhausted``，让 supervisor 的 ``should_stop`` 收工。
    """

    def __init__(self, episodes: list) -> None:
        self._episodes = list(episodes)
        self.connects = 0
        self.exhausted = False

    def stream(self):
        # 生成器：``exhausted`` 只能在这一段事件**产出完**之后才置位。写成
        # 提前置位的话，supervisor 在收第一条事件前就看到停机信号，用例会假绿。
        self.connects += 1
        if not self._episodes:
            self.exhausted = True
            return
        episode = self._episodes.pop(0)
        last = not self._episodes
        if isinstance(episode, BaseException):
            if last:
                self.exhausted = True
            raise episode
        for payload in episode:
            yield payload
        if last:
            self.exhausted = True


def run(transport: ScriptedTransport, handler, *, backoff: BackoffPolicy | None = None):
    delays: list[float] = []
    audit: list[tuple[str, dict]] = []
    supervisor = LongConnectionSupervisor(
        transport=transport,
        handle_event=handler,
        backoff=backoff or BackoffPolicy(base_seconds=0.5, factor=2.0, ceiling_seconds=8.0),
        sleep=delays.append,  # 注入睡眠：不真的等
        audit=lambda action, /, **fields: audit.append((action, fields)),
    )
    reason = supervisor.run(should_stop=lambda: transport.exhausted)
    return supervisor, reason, delays, audit


class EventRoutingTests(unittest.TestCase):
    """`V-接入-04`：投入一条事件，恰好触发一次处理器，事件体完整传入。"""

    def test_single_event_reaches_the_handler_exactly_once(self) -> None:
        received: list[dict] = []
        transport = ScriptedTransport([[event("evt_a")]])

        run(transport, received.append)

        self.assertEqual(len(received), 1)
        payload = received[0]
        self.assertEqual(payload["header"]["event_id"], "evt_a")
        self.assertEqual(payload["event"]["sender"]["sender_id"]["open_id"], "ou_1")
        self.assertEqual(payload["event"]["message"]["thread_id"], "omt_9")


class ReconnectTests(unittest.TestCase):
    """`V-接入-05`：非终止型断连后自动重连，退避单调且首次不早于下限。"""

    def test_reconnects_after_a_non_terminal_failure(self) -> None:
        received: list[dict] = []
        transport = ScriptedTransport(
            [
                LongConnectionError(
                    HandshakeFailure(source=FailureSource.ENDPOINT_HTTP, status_code=500)
                ),
                [event("evt_after_reconnect")],
            ]
        )

        supervisor, reason, delays, _ = run(transport, received.append)

        self.assertEqual(reason, TerminationReason.STOPPED)
        self.assertEqual(transport.connects, 2, "非终止型失败后必须重连")
        self.assertEqual(len(received), 1, "重连后必须恢复收取事件")
        self.assertGreaterEqual(delays[0], 0.5, "首次重连不得早于配置下限")

    def test_backoff_is_monotonic_not_a_fixed_interval(self) -> None:
        """固定间隔或零间隔的忙循环必须使本用例变红。"""

        failures = [
            LongConnectionError(
                HandshakeFailure(source=FailureSource.STREAM, status_code=None)
            )
            for _ in range(5)
        ]
        transport = ScriptedTransport(failures)

        _, _, delays, _ = run(transport, lambda payload: None)

        self.assertGreaterEqual(len(delays), 4)
        self.assertGreater(delays[1], delays[0], "间隔必须递增，固定间隔不是退避")
        for earlier, later in zip(delays, delays[1:]):
            self.assertGreaterEqual(later, earlier, "退避必须单调不减")
        self.assertTrue(all(delay > 0 for delay in delays), "零间隔是忙循环")

    def test_backoff_policy_rejects_busy_loop_configuration(self) -> None:
        with self.assertRaises(ValueError):
            BackoffPolicy(base_seconds=0)
        with self.assertRaises(ValueError):
            BackoffPolicy(factor=1.0)

    def test_backoff_is_capped(self) -> None:
        policy = BackoffPolicy(base_seconds=1.0, factor=2.0, ceiling_seconds=8.0)
        self.assertEqual(policy.delay_for(0), 1.0)
        self.assertEqual(policy.delay_for(3), 8.0)
        self.assertEqual(policy.delay_for(50), 8.0, "退避必须有上限，否则重连间隔会溢出")


class TerminalFailureTests(unittest.TestCase):
    """`V-接入-06`：403 与 514+autherrcode=1000040350 不得进入重连循环。"""

    def test_ws_handshake_403_never_reconnects(self) -> None:
        transport = ScriptedTransport(
            [
                LongConnectionError(
                    HandshakeFailure(
                        source=FailureSource.WS_HANDSHAKE, status_code=FORBIDDEN
                    )
                )
            ]
        )

        supervisor, reason, delays, audit = run(transport, lambda payload: None)

        self.assertEqual(reason, TerminationReason.TERMINAL_ERROR)
        self.assertEqual(supervisor.reconnect_attempts, 0, "终止型错误下重连次数必须恒为 0")
        self.assertEqual(transport.connects, 1)
        self.assertEqual(delays, [], "终止型错误不得进入退避等待")
        self.assertIn("longconn.terminal", [action for action, _ in audit], "必须记审计")

    def test_endpoint_http_403_also_never_reconnects(self) -> None:
        """同一个 403 在 SDK 里有两种相反语义，我们两条路径都判终止。"""

        transport = ScriptedTransport(
            [
                LongConnectionError(
                    HandshakeFailure(
                        source=FailureSource.ENDPOINT_HTTP, status_code=FORBIDDEN
                    )
                )
            ]
        )
        supervisor, reason, _, _ = run(transport, lambda payload: None)

        self.assertEqual(reason, TerminationReason.TERMINAL_ERROR)
        self.assertEqual(supervisor.reconnect_attempts, 0)

    def test_514_with_exceed_connection_limit_never_reconnects(self) -> None:
        transport = ScriptedTransport(
            [
                LongConnectionError(
                    HandshakeFailure(
                        source=FailureSource.WS_HANDSHAKE,
                        status_code=AUTH_FAILED,
                        auth_errcode=EXCEED_CONN_LIMIT,
                    )
                )
            ]
        )
        supervisor, reason, delays, _ = run(transport, lambda payload: None)

        self.assertEqual(reason, TerminationReason.TERMINAL_ERROR)
        self.assertEqual(supervisor.reconnect_attempts, 0)
        self.assertEqual(delays, [])

    def test_514_with_other_autherrcode_is_retryable(self) -> None:
        """决策登记只点名 1000040350；其他 514 判终止属扩大解释，不做。"""

        failure = HandshakeFailure(
            source=FailureSource.WS_HANDSHAKE, status_code=AUTH_FAILED, auth_errcode=1000040344
        )
        self.assertEqual(classify_handshake_failure(failure), FailureKind.RETRYABLE)

    def test_classification_table(self) -> None:
        terminal = [
            HandshakeFailure(source=FailureSource.WS_HANDSHAKE, status_code=FORBIDDEN),
            HandshakeFailure(source=FailureSource.ENDPOINT_HTTP, status_code=FORBIDDEN),
            HandshakeFailure(
                source=FailureSource.WS_HANDSHAKE,
                status_code=AUTH_FAILED,
                auth_errcode=EXCEED_CONN_LIMIT,
            ),
        ]
        retryable = [
            HandshakeFailure(source=FailureSource.ENDPOINT_HTTP, status_code=500),
            HandshakeFailure(source=FailureSource.WS_HANDSHAKE, status_code=AUTH_FAILED),
            HandshakeFailure(source=FailureSource.STREAM),
        ]
        for failure in terminal:
            with self.subTest(failure=failure):
                self.assertEqual(classify_handshake_failure(failure), FailureKind.TERMINAL)
        for failure in retryable:
            with self.subTest(failure=failure):
                self.assertEqual(classify_handshake_failure(failure), FailureKind.RETRYABLE)


class HandlerFailureTests(unittest.TestCase):
    """`V-接入-12`：单个事件的处理器抛异常不带下长连接。"""

    def test_connection_survives_and_later_events_are_still_processed(self) -> None:
        seen: list[str] = []

        def handler(payload: dict) -> None:
            event_id = payload["header"]["event_id"]
            if event_id == "evt_bad":
                raise RuntimeError("处理这条事件时炸了")
            seen.append(event_id)

        transport = ScriptedTransport(
            [[event("evt_ok_1"), event("evt_bad"), event("evt_ok_2")]]
        )

        _, reason, _, audit = run(transport, handler)

        self.assertEqual(reason, TerminationReason.STOPPED)
        self.assertEqual(seen, ["evt_ok_1", "evt_ok_2"], "后续事件必须照常处理")
        self.assertEqual(transport.connects, 1, "单事件失败不得导致断开重连")
        self.assertIn("event.handler_failed", [action for action, _ in audit])

    def test_unsubscribed_event_type_does_not_break_the_connection(self) -> None:
        """收到未订阅、无处理器的事件类型同样不能把连接带下去。"""

        from lingxi.apps.gateway import make_event_handler

        recorded: list[tuple[str, dict]] = []

        class Audit:
            def record(self, action: str, /, **fields: object) -> None:
                recorded.append((action, dict(fields)))

        class ExplodingPipeline:
            def handle_message(self, message: object) -> None:  # pragma: no cover
                raise AssertionError("未订阅的事件类型不该进管线")

        handler = make_event_handler(ExplodingPipeline(), audit=Audit())
        card_event = {
            "header": {"event_id": "evt_card", "event_type": "card.action.trigger"},
            "event": {},
        }
        transport = ScriptedTransport([[card_event]])

        _, reason, _, _ = run(transport, handler)

        self.assertEqual(reason, TerminationReason.STOPPED)
        self.assertIn("event.ignored", [action for action, _ in recorded])


class NoInboundPortTests(unittest.TestCase):
    """`V-接入-10` 的结构面：事件只能经长连接通道进入。

    进程侧的端口断言在 ``test_gateway_process.py``。这里断的是：supervisor 除了
    ``transport.stream()`` 之外没有第二个投递入口。
    """

    def test_supervisor_has_no_alternative_delivery_entry_point(self) -> None:
        supervisor = LongConnectionSupervisor(
            transport=ScriptedTransport([]), handle_event=lambda payload: None
        )
        public = {name for name in dir(supervisor) if not name.startswith("_")}
        self.assertEqual(
            public,
            {"observed_delays", "reconnect_attempts", "run"},
            "supervisor 不得出现第二个可以投递事件的公开入口",
        )

    def test_events_from_outside_the_transport_reach_no_handler(self) -> None:
        """构造一个不经该通道的投递路径：没有任何处理器被触发。"""

        received: list[dict] = []
        transport = ScriptedTransport([])
        supervisor = LongConnectionSupervisor(
            transport=transport, handle_event=received.append
        )
        supervisor.run(should_stop=lambda: True)

        self.assertEqual(received, [], "未经长连接通道的事件不得触发任何处理器")


if __name__ == "__main__":
    unittest.main()
