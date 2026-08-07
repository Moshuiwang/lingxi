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
                "chat_type": "p2p",
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
        # 注入睡眠：不真的等。签名与真实实现一致（带 should_stop），
        # 免得假实现悄悄退化成不可打断的那一种。
        sleep=lambda seconds, should_stop: delays.append(seconds),
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


class AckReportingTests(unittest.TestCase):
    """派发结果必须回报给传输层，SDK 才知道该向飞书回 OK 还是 500。"""

    class ReportingTransport(ScriptedTransport):
        def __init__(self, episodes):
            super().__init__(episodes)
            self.reports: list[tuple[str, str | None]] = []

        def report(self, payload, error):
            self.reports.append((payload["header"]["event_id"], type(error).__name__ if error else None))

    def test_a_successful_dispatch_reports_no_error(self) -> None:
        transport = self.ReportingTransport([[event("evt_ok")]])
        run(transport, lambda payload: None)

        self.assertEqual(transport.reports, [("evt_ok", None)])

    def test_a_failed_dispatch_reports_the_error(self) -> None:
        """注入落库失败 → 必须回报错误，否则飞书会把丢失的消息当成已投递。"""

        def explode(payload):
            raise RuntimeError("注入落库失败")

        transport = self.ReportingTransport([[event("evt_bad")]])
        run(transport, explode)

        self.assertEqual(transport.reports, [("evt_bad", "RuntimeError")])

    def test_a_transport_without_report_still_works(self) -> None:
        """假传输层不实现 report 时不得报错——它是可选协议。"""

        transport = ScriptedTransport([[event("evt_ok")]])
        _, reason, _, _ = run(transport, lambda payload: None)
        self.assertEqual(reason, TerminationReason.STOPPED)


class InterruptibleBackoffTests(unittest.TestCase):
    """退避等待必须能被停机信号打断（验收实测 SIGTERM 后 45 秒仍未退出）。"""

    def test_real_sleep_returns_promptly_once_stopping(self) -> None:
        import time

        from lingxi.adapters.feishu_longconn import _real_sleep

        stopping = False

        def should_stop() -> bool:
            return stopping

        # 先证明它确实会等满
        started = time.monotonic()
        _real_sleep(0.3, should_stop)
        self.assertGreaterEqual(time.monotonic() - started, 0.25)

        # 再证明停机信号能把它打断
        stopping = True
        started = time.monotonic()
        _real_sleep(60, should_stop)
        self.assertLess(
            time.monotonic() - started,
            1.0,
            "60 秒的退避没有被停机信号打断，停机会远超配置的超时",
        )

    def test_sleep_is_called_with_the_stop_predicate(self) -> None:
        """签名里带 should_stop，注入的假实现也就不会退化成不可打断的。"""

        seen: list[tuple[float, bool]] = []
        transport = ScriptedTransport(
            [
                LongConnectionError(HandshakeFailure(source=FailureSource.STREAM)),
                [event("evt_ok")],
            ]
        )
        supervisor = LongConnectionSupervisor(
            transport=transport,
            handle_event=lambda payload: None,
            backoff=BackoffPolicy(base_seconds=0.5, factor=2.0, ceiling_seconds=8.0),
            sleep=lambda seconds, should_stop: seen.append((seconds, callable(should_stop))),
        )
        supervisor.run(should_stop=lambda: transport.exhausted)

        self.assertTrue(seen)
        self.assertTrue(all(is_callable for _, is_callable in seen))


class BackoffResetTests(unittest.TestCase):
    """一次健康的连接之后，退避必须从下限重新起算。"""

    def test_backoff_restarts_after_events_flow_again(self) -> None:
        failure = LongConnectionError(HandshakeFailure(source=FailureSource.STREAM))
        transport = ScriptedTransport(
            [failure, failure, failure, [event("evt_ok")], failure, [event("evt_last")]]
        )

        _, _, delays, _ = run(transport, lambda payload: None)

        self.assertGreater(delays[2], delays[0], "连续失败期间应递增")
        self.assertEqual(
            delays[3],
            delays[0],
            "收到过真实事件之后，下一次断线的退避必须回到下限——否则健康跑很久的进程"
            "此后每次断线都直接等上限",
        )


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

    def test_a_real_delivery_attempt_from_outside_the_transport_is_refused(self) -> None:
        """真的**尝试投递**一条事件，而不是什么都不做然后断言什么都没发生。

        原先这条用例只是「不启动 supervisor，然后断言没有事件」——恒真，改坏实现也
        不会红。这里逐个试遍 supervisor 的公开面：任何一个能把 payload 送进处理器的
        入口都会让它变红。
        """

        received: list[dict] = []
        supervisor = LongConnectionSupervisor(
            transport=ScriptedTransport([]), handle_event=received.append
        )
        payload = event("evt_bypass")

        probed: list[str] = []
        for name in dir(supervisor):
            if name.startswith("_"):
                continue
            attribute = getattr(supervisor, name)
            if not callable(attribute):
                continue
            for arguments in ((payload,), (payload, None)):
                probed.append(name)
                try:
                    attribute(*arguments)
                except TypeError:
                    pass  # 签名根本不接受事件——正是我们想要的形状
                except Exception:
                    pass  # 接受了参数但自己失败，同样算被调用过

        self.assertTrue(probed, "至少要真的探过一个公开可调用项，否则本用例恒真")
        self.assertIn("run", probed, "公开面里的 run 必须被探到")
        self.assertEqual(
            received, [], "未经长连接通道的事件不得触发任何处理器"
        )


if __name__ == "__main__":
    unittest.main()
