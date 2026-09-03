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


def card_action_event(
    *, event_id: str = "evt_card_1", operator_open_id: str = "ou_admin", decision: str = "confirm"
) -> dict:
    return {
        "header": {"event_id": event_id, "event_type": "card.action.trigger"},
        "event": {
            "operator": {"open_id": operator_open_id},
            "action": {
                "tag": "button",
                "value": {"pending_action_id": "pac_1", "decision": decision},
            },
        },
    }


class CardActionDispatchTests(unittest.TestCase):
    """Issue #96 S-M-02：``card.action.trigger`` 事件在 ``card_callback_handler``
    被显式装配时才处理；未装配（``None``，既有默认值）时行为与
    ``HandlerFailureTests.test_unsubscribed_event_type_does_not_break_the_
    connection`` 逐字节一致——那条既有用例本身就是这一条不变量的回归哨兵，本类
    只新增"确实装配了"这一侧的正向与否定覆盖。
    """

    def test_wired_handler_receives_the_parsed_operator_and_decision(self) -> None:
        from lingxi.apps.gateway import make_event_handler

        class Audit:
            def record(self, action: str, /, **fields: object) -> None:
                pass

        class ExplodingPipeline:
            def handle_message(self, message: object) -> None:  # pragma: no cover
                raise AssertionError("卡片回调事件不该进消息管线")

        calls: list[dict[str, str]] = []

        class FakeCardCallbackHandler:
            def handle(
                self, *, operator_open_id: str, pending_action_id: str, decision: str, trace_id: str
            ) -> object:
                calls.append(
                    {
                        "operator_open_id": operator_open_id,
                        "pending_action_id": pending_action_id,
                        "decision": decision,
                    }
                )
                return None

        handler = make_event_handler(
            ExplodingPipeline(), audit=Audit(), card_callback_handler=FakeCardCallbackHandler()
        )
        transport = ScriptedTransport([[card_action_event(operator_open_id="ou_admin")]])

        _, reason, _, _ = run(transport, handler)

        self.assertEqual(reason, TerminationReason.STOPPED)
        self.assertEqual(
            calls,
            [{"operator_open_id": "ou_admin", "pending_action_id": "pac_1", "decision": "confirm"}],
        )

    def test_unparsable_card_event_is_recorded_and_does_not_break_the_connection(self) -> None:
        """否定断言：卡片回调伪造/畸形事件体（缺 operator）→ 记审计、不崩溃、
        不当成"未订阅事件类型"（区分 ``event.unparsable`` 与 ``event.ignored``，
        便于运维定位"到底是没装配还是读不懂"）。"""

        from lingxi.apps.gateway import make_event_handler

        recorded: list[tuple[str, dict]] = []

        class Audit:
            def record(self, action: str, /, **fields: object) -> None:
                recorded.append((action, dict(fields)))

        class ExplodingPipeline:
            def handle_message(self, message: object) -> None:  # pragma: no cover
                raise AssertionError("不该进消息管线")

        class ExplodingCardCallbackHandler:
            def handle(self, **kwargs: object) -> None:  # pragma: no cover
                raise AssertionError("解析失败的事件不该到达回调处理器")

        handler = make_event_handler(
            ExplodingPipeline(),
            audit=Audit(),
            card_callback_handler=ExplodingCardCallbackHandler(),
        )
        malformed_event = {
            "header": {"event_id": "evt_card_bad", "event_type": "card.action.trigger"},
            "event": {},  # 缺 operator/action
        }
        transport = ScriptedTransport([[malformed_event]])

        _, reason, _, _ = run(transport, handler)

        self.assertEqual(reason, TerminationReason.STOPPED)
        self.assertIn("event.unparsable", [action for action, _ in recorded])

    def test_message_events_still_flow_normally_when_card_callback_handler_is_wired(
        self,
    ) -> None:
        """两种事件类型共存时互不干扰：装配了 ``card_callback_handler`` 不改变
        既有 ``im.message.receive_v1`` 的处理路径。"""

        from lingxi.apps.gateway import make_event_handler

        class Audit:
            def record(self, action: str, /, **fields: object) -> None:
                pass

        class NoOpCardCallbackHandler:
            def handle(self, **kwargs: object) -> None:  # pragma: no cover
                raise AssertionError("消息事件不该被当成卡片回调处理")

        received: list[str] = []

        class StubPipeline:
            def handle_message(self, message) -> None:
                received.append(message.event_id)

        handler = make_event_handler(
            StubPipeline(),  # type: ignore[arg-type]
            audit=Audit(),
            card_callback_handler=NoOpCardCallbackHandler(),
        )
        transport = ScriptedTransport([[event("evt_normal_1")]])

        _, reason, _, _ = run(transport, handler)

        self.assertEqual(reason, TerminationReason.STOPPED)
        self.assertEqual(received, ["evt_normal_1"])

    def test_card_branch_returns_the_callback_handlers_response(self) -> None:
        """Issue #96 载体修复：``card_callback_handler.handle(...)`` 的返回值必须
        被 ``make_event_handler`` 的 ``handle()`` 原样 ``return``，而不是像此前
        那样被丢弃——这正是卡片回调应答链路的入口，返回值最终经
        ``LongConnectionSupervisor._dispatch`` 回报给传输层，再由 SDK marshal
        进飞书要的应答帧。直接调用 ``handle()`` 而不经过 supervisor，只验证这
        一层的透传本身。"""

        from lingxi.apps.gateway import make_event_handler

        class Audit:
            def record(self, action: str, /, **fields: object) -> None:
                pass

        class ExplodingPipeline:
            def handle_message(self, message: object) -> None:  # pragma: no cover
                raise AssertionError("卡片回调事件不该进消息管线")

        expected_response = {"toast": {"type": "success", "content": "已确认执行。"}}

        class FakeCardCallbackHandler:
            def handle(self, **kwargs: object) -> dict:
                return expected_response

        handler = make_event_handler(
            ExplodingPipeline(), audit=Audit(), card_callback_handler=FakeCardCallbackHandler()
        )

        result = handler(card_action_event())

        self.assertEqual(result, expected_response)

    def test_message_branch_still_returns_none(self) -> None:
        """普通消息事件分支必须保持返回 ``None``——本参数加入之前的既有行为
        逐字节不变，只有卡片回调分支的返回值现在被透传。"""

        from lingxi.apps.gateway import make_event_handler

        class Audit:
            def record(self, action: str, /, **fields: object) -> None:
                pass

        received: list[str] = []

        class StubPipeline:
            def handle_message(self, message) -> None:
                received.append(message.event_id)

        handler = make_event_handler(StubPipeline(), audit=Audit())  # type: ignore[arg-type]

        result = handler(event("evt_normal_direct"))

        self.assertIsNone(result)
        self.assertEqual(received, ["evt_normal_direct"])


def management_form_submit_event(
    *,
    event_id: str = "evt_card_form_1",
    operator_open_id: str = "ou_admin",
    admin_action: str = "grant",
    identifier: str = "u@example.invalid",
    company_id: str = "1011",
    metric_name: str = "示例指标",
    reason: str = "特批",
    chat_id: str = "oc_admin_dm",
    message_id: str = "om_card_1",
) -> dict:
    """管理卡「新增授权/新增抑制」表单提交的原始事件体（`core/admin/
    management_card.py` 的 ``form``/``select_static``/``input`` 组件形状 +
    飞书卡片 2.0 表单回传的 ``action.form_value``，证据等级 1，见
    ``apps/gateway/__init__.py`` ``_management_card_form_value`` 文档）。"""

    return {
        "header": {"event_id": event_id, "event_type": "card.action.trigger"},
        "event": {
            "operator": {"open_id": operator_open_id},
            "action": {
                "tag": "form",
                "value": {"admin_action": admin_action, "identifier": identifier},
                "form_value": {
                    "company_id": company_id,
                    "metric_name": metric_name,
                    "reason": reason,
                },
            },
            "context": {"open_chat_id": chat_id, "open_message_id": message_id},
        },
    }


def management_revoke_event(
    *,
    event_id: str = "evt_card_revoke_1",
    operator_open_id: str = "ou_admin",
    override_id: str = "lpo_1",
    identifier: str = "u@example.invalid",
    chat_id: str = "oc_admin_dm",
    message_id: str = "om_card_2",
) -> dict:
    """管理卡逐行「收回」按钮点击的原始事件体（`core/admin/management_card.py`
    ``_override_row_elements`` 建的按钮 ``behaviors[].value`` 形状）。"""

    return {
        "header": {"event_id": event_id, "event_type": "card.action.trigger"},
        "event": {
            "operator": {"open_id": operator_open_id},
            "action": {
                "tag": "button",
                "value": {
                    "admin_action": "revoke",
                    "override_id": override_id,
                    "identifier": identifier,
                },
            },
            "context": {"open_chat_id": chat_id, "open_message_id": message_id},
        },
    }


class ManagementCardDispatchTests(unittest.TestCase):
    """Issue #439 B 档：管理卡表单提交与逐行收回按钮的原始事件体解析与路由。

    只验证 ``make_event_handler`` 把回传值分流到正确的方法、传对干净参数——
    ``AdminCardCallbackHandler.handle_management_form_submit``/
    ``handle_management_revoke`` 自身的必填校验、``route()`` 转译已经在
    ``tests/test_admin_card_callback.py`` 钉过，这里不重复。
    """

    def test_grant_form_submission_routes_to_handle_management_form_submit(self) -> None:
        from lingxi.apps.gateway import make_event_handler

        class Audit:
            def record(self, action: str, /, **fields: object) -> None:
                pass

        class ExplodingPipeline:
            def handle_message(self, message: object) -> None:  # pragma: no cover
                raise AssertionError("卡片回调事件不该进消息管线")

        calls: list[dict[str, object]] = []

        class FakeCardCallbackHandler:
            def handle_management_form_submit(self, **kwargs: object) -> dict:
                calls.append(kwargs)
                return {"toast": {"type": "success", "content": "ok"}}

            def handle_management_revoke(self, **kwargs: object) -> dict:  # pragma: no cover
                raise AssertionError("表单提交不该路由到收回分支")

            def handle(self, **kwargs: object) -> dict:  # pragma: no cover
                raise AssertionError("表单提交不该路由到确认/取消既有分支")

        handler = make_event_handler(
            ExplodingPipeline(), audit=Audit(), card_callback_handler=FakeCardCallbackHandler()
        )

        result = handler(
            management_form_submit_event(
                admin_action="grant",
                identifier="u@example.invalid",
                company_id="1011",
                metric_name="示例指标",
                reason="特批",
                chat_id="oc_admin_dm",
                message_id="om_card_1",
            )
        )

        self.assertEqual(result, {"toast": {"type": "success", "content": "ok"}})
        self.assertEqual(
            calls,
            [
                {
                    "operator_open_id": "ou_admin",
                    "admin_action": "grant",
                    "identifier": "u@example.invalid",
                    "company_id": "1011",
                    "metric_name": "示例指标",
                    "reason": "特批",
                    "chat_id": "oc_admin_dm",
                    "thread_id": None,
                    "message_id": "om_card_1",
                    "trace_id": calls[0]["trace_id"],
                }
            ],
        )

    def test_suppress_form_submission_is_no_longer_routed(self) -> None:
        """**否定用例**：「屏蔽指标」提交按钮随 ``/admin suppress_permission`` 一起
        撤除（Trace #544 D-5）——旧卡上残留的这个按钮点下去不再进表单处理口。"""

        from lingxi.apps.gateway import make_event_handler

        class Audit:
            def record(self, action: str, /, **fields: object) -> None:
                pass

        class ExplodingPipeline:
            def handle_message(self, message: object) -> None:  # pragma: no cover
                raise AssertionError("卡片回调事件不该进消息管线")

        calls: list[dict[str, object]] = []

        fallbacks: list[dict[str, object]] = []

        class FakeCardCallbackHandler:
            def handle_management_form_submit(self, **kwargs: object) -> dict:
                calls.append(kwargs)
                return {"toast": {"type": "success", "content": "ok"}}

            def handle(self, **kwargs: object) -> dict:
                # 认不出的 admin_action 落回确认/取消这条通用路径，那里对一个不是
                # confirm/cancel 的 decision 只回一句"操作不存在或已失效"。
                fallbacks.append(kwargs)
                return {"toast": {"type": "error", "content": "操作不存在或已失效"}}

        handler = make_event_handler(
            ExplodingPipeline(), audit=Audit(), card_callback_handler=FakeCardCallbackHandler()
        )

        result = handler(management_form_submit_event(admin_action="suppress"))

        self.assertEqual(calls, [], "撤除的动作不得再进表单处理口")
        self.assertEqual(len(fallbacks), 1)
        self.assertEqual(result["toast"]["type"], "error")

    def test_real_click_shape_missing_value_routes_via_button_name(self) -> None:
        """W0-1 追加结论（2026-08-30，真实点击实测坐实）：真实回调里 form 内
        提交按钮的 ``action.value`` 经常不带 ``admin_action``（本用例构造的
        ``action.value`` 缺失，只有 ``action.form_value``）——此前这类事件会
        在 ``adapters/feishu_events.py`` 被整体拒绝（``CardActionParseError``），
        管理卡补充授权/屏蔽指标从此全部静默失效。修复后应改用按钮自己的
        ``action.name``（``grant_submit``）兜底路由，仍然到达
        ``handle_management_form_submit``。"""

        from lingxi.apps.gateway import make_event_handler

        class Audit:
            def __init__(self) -> None:
                self.records: list[tuple[str, dict]] = []

            def record(self, action: str, /, **fields: object) -> None:
                self.records.append((action, fields))

        class ExplodingPipeline:
            def handle_message(self, message: object) -> None:  # pragma: no cover
                raise AssertionError("卡片回调事件不该进消息管线")

        calls: list[dict[str, object]] = []

        class FakeCardCallbackHandler:
            def handle_management_form_submit(self, **kwargs: object) -> dict:
                calls.append(kwargs)
                return {"toast": {"type": "success", "content": "ok"}}

            def handle_management_revoke(self, **kwargs: object) -> dict:  # pragma: no cover
                raise AssertionError("表单提交不该路由到收回分支")

            def handle(self, **kwargs: object) -> dict:  # pragma: no cover
                raise AssertionError("表单提交不该静默落回确认/取消既有分支")

        audit = Audit()
        handler = make_event_handler(
            ExplodingPipeline(), audit=audit, card_callback_handler=FakeCardCallbackHandler()
        )

        event = {
            "header": {"event_id": "evt_real_click_1", "event_type": "card.action.trigger"},
            "event": {
                "operator": {"open_id": "ou_admin"},
                "action": {
                    "tag": "form",
                    "name": "grant_submit",
                    # 真实实测形态：value 缺失（本用例）或是需要反序列化的
                    # JSON 字符串（下一个用例覆盖）。
                    "form_value": {
                        "company_id": "1011",
                        "metric_name": "sub_new_count",
                        "reason": "特批",
                    },
                },
                "context": {"open_chat_id": "oc_admin_dm", "open_message_id": "om_card_1"},
            },
        }

        result = handler(event)

        self.assertEqual(result, {"toast": {"type": "success", "content": "ok"}})
        self.assertEqual(audit.records, [], "不应该落进 event.unparsable")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["admin_action"], "grant")
        self.assertEqual(calls[0]["company_id"], "1011")
        self.assertEqual(calls[0]["metric_name"], "sub_new_count")
        self.assertEqual(calls[0]["reason"], "特批")
        # value 缺失时 identifier 无法恢复（value 是它唯一的载体），
        # card_callback.py 既有的必填校验会给出"未识别到目标用户标识"这类
        # 明确提示，不是本层的职责——本用例只钉住"事件确实到达了
        # handle_management_form_submit，不是被静默丢弃"。
        self.assertEqual(calls[0]["identifier"], "")

    def test_real_click_shape_json_string_value_recovers_identifier(self) -> None:
        """真实实测的另一种形态：``action.value`` 到达时是一段 JSON 字符串——
        解码后 ``identifier`` 等字段能完整恢复，不需要依赖按钮名兜底。"""

        import json

        from lingxi.apps.gateway import make_event_handler

        class Audit:
            def record(self, action: str, /, **fields: object) -> None:
                pass

        class ExplodingPipeline:
            def handle_message(self, message: object) -> None:  # pragma: no cover
                raise AssertionError("卡片回调事件不该进消息管线")

        calls: list[dict[str, object]] = []

        class FakeCardCallbackHandler:
            def handle_management_form_submit(self, **kwargs: object) -> dict:
                calls.append(kwargs)
                return {"toast": {"type": "success", "content": "ok"}}

        handler = make_event_handler(
            ExplodingPipeline(), audit=Audit(), card_callback_handler=FakeCardCallbackHandler()
        )

        event = {
            "header": {"event_id": "evt_real_click_2", "event_type": "card.action.trigger"},
            "event": {
                "operator": {"open_id": "ou_admin"},
                "action": {
                    "tag": "form",
                    "name": "grant_submit",
                    "value": json.dumps(
                        {"admin_action": "grant", "identifier": "u@example.invalid"}
                    ),
                    "form_value": {
                        "company_id": "1012",
                        "metric_name": "channel_rate",
                        "reason": "临时限制",
                    },
                },
                "context": {"open_chat_id": "oc_admin_dm", "open_message_id": "om_card_1"},
            },
        }

        handler(event)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["admin_action"], "grant")
        self.assertEqual(calls[0]["identifier"], "u@example.invalid")
        self.assertEqual(calls[0]["company_id"], "1012")

    def test_revoke_button_routes_to_handle_management_revoke(self) -> None:
        from lingxi.apps.gateway import make_event_handler

        class Audit:
            def record(self, action: str, /, **fields: object) -> None:
                pass

        class ExplodingPipeline:
            def handle_message(self, message: object) -> None:  # pragma: no cover
                raise AssertionError("卡片回调事件不该进消息管线")

        calls: list[dict[str, object]] = []

        class FakeCardCallbackHandler:
            def handle_management_revoke(self, **kwargs: object) -> dict:
                calls.append(kwargs)
                return {"toast": {"type": "success", "content": "已收回"}}

            def handle_management_form_submit(self, **kwargs: object) -> dict:  # pragma: no cover
                raise AssertionError("收回按钮不该路由到表单提交分支")

            def handle(self, **kwargs: object) -> dict:  # pragma: no cover
                raise AssertionError("收回按钮不该路由到确认/取消既有分支")

        handler = make_event_handler(
            ExplodingPipeline(), audit=Audit(), card_callback_handler=FakeCardCallbackHandler()
        )

        result = handler(
            management_revoke_event(
                override_id="lpo_9", chat_id="oc_admin_dm", message_id="om_card_2"
            )
        )

        self.assertEqual(result, {"toast": {"type": "success", "content": "已收回"}})
        self.assertEqual(
            calls,
            [
                {
                    "operator_open_id": "ou_admin",
                    "override_id": "lpo_9",
                    "chat_id": "oc_admin_dm",
                    "thread_id": None,
                    "message_id": "om_card_2",
                    "trace_id": calls[0]["trace_id"],
                }
            ],
        )

    def test_unrecognized_admin_action_falls_back_to_the_existing_confirm_cancel_branch(
        self,
    ) -> None:
        """否定断言：``admin_action`` 缺失（确认/取消卡片的既有形状）时维持既有
        兜底行为逐字节不变——路由到 ``handle()``，不是两个新分支。"""

        from lingxi.apps.gateway import make_event_handler

        class Audit:
            def record(self, action: str, /, **fields: object) -> None:
                pass

        class ExplodingPipeline:
            def handle_message(self, message: object) -> None:  # pragma: no cover
                raise AssertionError("卡片回调事件不该进消息管线")

        calls: list[dict[str, object]] = []

        class FakeCardCallbackHandler:
            def handle(self, **kwargs: object) -> dict:
                calls.append(kwargs)
                return {"toast": {"type": "success", "content": "已确认执行"}}

            def handle_management_form_submit(self, **kwargs: object) -> dict:  # pragma: no cover
                raise AssertionError("确认/取消卡片不该路由到表单提交分支")

            def handle_management_revoke(self, **kwargs: object) -> dict:  # pragma: no cover
                raise AssertionError("确认/取消卡片不该路由到收回分支")

        handler = make_event_handler(
            ExplodingPipeline(), audit=Audit(), card_callback_handler=FakeCardCallbackHandler()
        )

        result = handler(card_action_event(operator_open_id="ou_admin", decision="confirm"))

        self.assertEqual(result, {"toast": {"type": "success", "content": "已确认执行"}})
        self.assertEqual(
            calls,
            [
                {
                    "operator_open_id": "ou_admin",
                    "pending_action_id": "pac_1",
                    "decision": "confirm",
                    "trace_id": calls[0]["trace_id"],
                }
            ],
        )

    def test_unknown_admin_action_value_also_falls_back_not_a_new_toast(self) -> None:
        """``admin_action`` 存在但取值既不是 grant/suppress 也不是
        revoke（伪造或未来新增未接线的取值）：结构上按「无法识别」处理，落到
        既有确认/取消分支（会因缺 ``decision`` 被 ``handle()`` 自己的
        ``unknown_decision`` 拒绝），不新造一条分流逻辑之外的行为。"""

        from lingxi.apps.gateway import make_event_handler

        class Audit:
            def record(self, action: str, /, **fields: object) -> None:
                pass

        class ExplodingPipeline:
            def handle_message(self, message: object) -> None:  # pragma: no cover
                raise AssertionError("卡片回调事件不该进消息管线")

        calls: list[str] = []

        class FakeCardCallbackHandler:
            def handle(self, **kwargs: object) -> dict:
                calls.append("handle")
                return {"toast": {"type": "error", "content": "操作不存在或已失效"}}

            def handle_management_form_submit(self, **kwargs: object) -> dict:  # pragma: no cover
                raise AssertionError("未知 admin_action 不该路由到表单提交分支")

            def handle_management_revoke(self, **kwargs: object) -> dict:  # pragma: no cover
                raise AssertionError("未知 admin_action 不该路由到收回分支")

        handler = make_event_handler(
            ExplodingPipeline(), audit=Audit(), card_callback_handler=FakeCardCallbackHandler()
        )

        result = handler(management_form_submit_event(admin_action="__unknown__"))

        self.assertEqual(calls, ["handle"])
        self.assertEqual(result, {"toast": {"type": "error", "content": "操作不存在或已失效"}})


class ManagementCardContextRecoveryTests(unittest.TestCase):
    """Trace #469 修复包 B，B-1：``action.value`` 整体缺失（真实点击已实测的
    第三种形态，前两种见 ``ManagementCardDispatchTests``）时，用发送侧登记的
    ``message_id -> identifier`` 映射补回 identifier，不再一律落进
    ``handle_management_form_submit`` 的「未识别到目标用户标识」拒绝路径。"""

    @staticmethod
    def _missing_value_event(*, message_id: str = "om_card_1") -> dict:
        return {
            "header": {"event_id": "evt_missing_value_1", "event_type": "card.action.trigger"},
            "event": {
                "operator": {"open_id": "ou_admin"},
                "action": {
                    "tag": "form",
                    "name": "grant_submit",
                    "form_value": {
                        "company_id": "1011",
                        "metric_name": "sub_new_count",
                        "reason": "特批",
                    },
                },
                "context": {"open_chat_id": "oc_admin_dm", "open_message_id": message_id},
            },
        }

    def test_missing_value_with_context_hit_recovers_identifier(self) -> None:
        from lingxi.apps.gateway import make_event_handler
        from lingxi.core.admin.card_dispatch import ManagementCardContextStore

        class Audit:
            def record(self, action: str, /, **fields: object) -> None:
                pass

        class ExplodingPipeline:
            def handle_message(self, message: object) -> None:  # pragma: no cover
                raise AssertionError("卡片回调事件不该进消息管线")

        calls: list[dict[str, object]] = []

        class FakeCardCallbackHandler:
            def handle_management_form_submit(self, **kwargs: object) -> dict:
                calls.append(kwargs)
                return {"toast": {"type": "success", "content": "ok"}}

        store = ManagementCardContextStore()
        store.remember(message_id="om_card_1", identifier="u@example.invalid")

        handler = make_event_handler(
            ExplodingPipeline(),
            audit=Audit(),
            card_callback_handler=FakeCardCallbackHandler(),
            management_card_context_store=store,
        )

        handler(self._missing_value_event(message_id="om_card_1"))

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["identifier"], "u@example.invalid")

    def test_missing_value_with_context_miss_keeps_existing_rejection_path(self) -> None:
        """映射未命中（从未登记过这个 message_id）：identifier 仍是空串，与
        本修复之前完全一致——不猜测、不伪造。"""

        from lingxi.apps.gateway import make_event_handler
        from lingxi.core.admin.card_dispatch import ManagementCardContextStore

        class Audit:
            def record(self, action: str, /, **fields: object) -> None:
                pass

        class ExplodingPipeline:
            def handle_message(self, message: object) -> None:  # pragma: no cover
                raise AssertionError("卡片回调事件不该进消息管线")

        calls: list[dict[str, object]] = []

        class FakeCardCallbackHandler:
            def handle_management_form_submit(self, **kwargs: object) -> dict:
                calls.append(kwargs)
                return {"toast": {"type": "success", "content": "ok"}}

        store = ManagementCardContextStore()  # 未登记任何 message_id

        handler = make_event_handler(
            ExplodingPipeline(),
            audit=Audit(),
            card_callback_handler=FakeCardCallbackHandler(),
            management_card_context_store=store,
        )

        handler(self._missing_value_event(message_id="om_card_never_sent"))

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["identifier"], "")

    def test_expired_entry_keeps_existing_rejection_path(self) -> None:
        """TTL 过期：与"从未登记过"得到同一个结论（空串），不额外区分。"""

        from lingxi.apps.gateway import make_event_handler
        from lingxi.core.admin.card_dispatch import ManagementCardContextStore

        class Audit:
            def record(self, action: str, /, **fields: object) -> None:
                pass

        class ExplodingPipeline:
            def handle_message(self, message: object) -> None:  # pragma: no cover
                raise AssertionError("卡片回调事件不该进消息管线")

        calls: list[dict[str, object]] = []

        class FakeCardCallbackHandler:
            def handle_management_form_submit(self, **kwargs: object) -> dict:
                calls.append(kwargs)
                return {"toast": {"type": "success", "content": "ok"}}

        clock = [0.0]
        store = ManagementCardContextStore(ttl_seconds=1800.0, clock=lambda: clock[0])
        store.remember(message_id="om_card_1", identifier="u@example.invalid")
        clock[0] = 1800.0  # 恰好到期

        handler = make_event_handler(
            ExplodingPipeline(),
            audit=Audit(),
            card_callback_handler=FakeCardCallbackHandler(),
            management_card_context_store=store,
        )

        handler(self._missing_value_event(message_id="om_card_1"))

        self.assertEqual(calls[0]["identifier"], "")

    def test_capacity_eviction_keeps_existing_rejection_path(self) -> None:
        """容量上限逐出后查不到，与其余未命中情形同一结论。**变异验红要点**：
        若把 ``make_event_handler`` 里的查表调用去掉（例如恒使用
        ``action_value`` 里已经确定为空的 identifier，不再尝试查
        ``management_card_context_store``），本用例与上面两个"未命中"用例
        全部照样通过（因为它们的期望本来就是空串）——真正能证伪"去掉查表"
        这个变异的是 ``test_missing_value_with_context_hit_recovers_identifier``
        那一条"命中"用例，本用例只覆盖"逐出确实生效"这条独立事实。"""

        from lingxi.apps.gateway import make_event_handler
        from lingxi.core.admin.card_dispatch import ManagementCardContextStore

        class Audit:
            def record(self, action: str, /, **fields: object) -> None:
                pass

        class ExplodingPipeline:
            def handle_message(self, message: object) -> None:  # pragma: no cover
                raise AssertionError("卡片回调事件不该进消息管线")

        calls: list[dict[str, object]] = []

        class FakeCardCallbackHandler:
            def handle_management_form_submit(self, **kwargs: object) -> dict:
                calls.append(kwargs)
                return {"toast": {"type": "success", "content": "ok"}}

        store = ManagementCardContextStore(max_entries=1)
        store.remember(message_id="om_card_evicted", identifier="u@example.invalid")
        store.remember(message_id="om_card_other", identifier="someone-else@example.invalid")

        handler = make_event_handler(
            ExplodingPipeline(),
            audit=Audit(),
            card_callback_handler=FakeCardCallbackHandler(),
            management_card_context_store=store,
        )

        handler(self._missing_value_event(message_id="om_card_evicted"))

        self.assertEqual(calls[0]["identifier"], "")


class AckReportingTests(unittest.TestCase):
    """派发结果必须回报给传输层，SDK 才知道该向飞书回 OK 还是 500。"""

    class ReportingTransport(ScriptedTransport):
        def __init__(self, episodes):
            super().__init__(episodes)
            self.reports: list[tuple[str, str | None, dict | None]] = []

        def report(self, payload, error, response=None):
            self.reports.append(
                (payload["header"]["event_id"], type(error).__name__ if error else None, response)
            )

    def test_a_successful_dispatch_reports_no_error(self) -> None:
        transport = self.ReportingTransport([[event("evt_ok")]])
        run(transport, lambda payload: None)

        self.assertEqual(transport.reports, [("evt_ok", None, None)])

    def test_a_failed_dispatch_reports_the_error(self) -> None:
        """注入落库失败 → 必须回报错误，否则飞书会把丢失的消息当成已投递。"""

        def explode(payload):
            raise RuntimeError("注入落库失败")

        transport = self.ReportingTransport([[event("evt_bad")]])
        run(transport, explode)

        self.assertEqual(transport.reports, [("evt_bad", "RuntimeError", None)])

    def test_a_transport_without_report_still_works(self) -> None:
        """假传输层不实现 report 时不得报错——它是可选协议。"""

        transport = ScriptedTransport([[event("evt_ok")]])
        _, reason, _, _ = run(transport, lambda payload: None)
        self.assertEqual(reason, TerminationReason.STOPPED)


class ResponsePipelineTests(unittest.TestCase):
    """Issue #96 卡片回调应答修复：处理器的返回值必须原样回报给传输层
    （``report(payload, error, response)``），最终由
    ``adapters/feishu_longconn._RawEventSink._do_without_validation`` 交给 SDK
    marshal 进应答帧 ``resp.data``——SDK 据此决定要不要把飞书卡片换成新内容
    （见 ``core/admin/card_callback.py`` 模块文档「载体 #96」）。"""

    def test_a_handler_returning_a_response_dict_is_reported_alongside_no_error(
        self,
    ) -> None:
        """卡片回调分支：handler 返回一个应答字典（模拟
        ``AdminCardCallbackHandler.handle`` 的返回值），必须原样出现在
        ``report`` 的第三个参数里。"""

        card_response = {
            "toast": {"type": "success", "content": "已确认执行。"},
            "card": {"type": "raw", "data": {"schema": "2.0"}},
        }
        transport = AckReportingTests.ReportingTransport([[event("evt_card")]])

        run(transport, lambda payload: card_response)

        self.assertEqual(transport.reports, [("evt_card", None, card_response)])

    def test_a_normal_message_handler_returning_none_reports_no_response(self) -> None:
        """普通消息事件分支：handler 隐式返回 ``None``——本通道加入之前的既有
        行为必须逐字节保持不变，不能悄悄变成空字典或其它哨兵值。"""

        transport = AckReportingTests.ReportingTransport([[event("evt_msg")]])

        run(transport, lambda payload: None)

        self.assertEqual(transport.reports, [("evt_msg", None, None)])

    def test_a_failed_dispatch_reports_the_error_and_no_response(self) -> None:
        """处理器抛异常时不产出应答——``error`` 已经足够让传输层向飞书回失败，
        ``response`` 必须保持 ``None``，不能把处理器崩溃前算出的半成品当应答。"""

        def explode(payload: dict) -> dict:
            raise RuntimeError("处理失败")

        transport = AckReportingTests.ReportingTransport([[event("evt_bad")]])

        run(transport, explode)

        self.assertEqual(transport.reports, [("evt_bad", "RuntimeError", None)])


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
