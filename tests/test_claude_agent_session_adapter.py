"""会话绑定层的形状断言（V-执行-16 的延伸）。

``lingxi.adapters.claude_agent_session`` 与 ``claude_agent_hooks`` 一起构成唯一 import
Claude Agent SDK 的地方。本文件用桩模块顶替 SDK，锁住三件事：``ClaudeAgentOptions``
被传了哪些字段、消息流被规范化成什么形状、``ClaudeSDKClient`` 的调用次序。

桩只能证明**本侧形状没变**。真实 SDK 装得上、``ClaudeAgentOptions`` 接受这些字段、
消息类型名仍然存在，由 ``scripts/ci/check_agent_sdk_binding.py`` 用真实 SDK 冒烟；
事件是否真的触发只有 L4a 能答。
"""

from __future__ import annotations

import asyncio
import sys
import types
import unittest


class StubHookMatcher:
    def __init__(self, matcher=None, hooks=None, timeout=None) -> None:
        self.matcher = matcher
        self.hooks = hooks or []
        self.timeout = timeout


class StubAgentOptions:
    def __init__(
        self,
        *,
        allowed_tools=None,
        max_turns=None,
        disallowed_tools=None,
        hooks=None,
        mcp_servers=None,
        cwd=None,
        model=None,
        system_prompt=None,
        setting_sources=None,
        permission_mode=None,
        stderr=None,
        strict_mcp_config=None,
    ) -> None:
        self.allowed_tools = allowed_tools
        self.max_turns = max_turns
        self.disallowed_tools = disallowed_tools
        self.hooks = hooks
        self.mcp_servers = mcp_servers
        self.cwd = cwd
        self.model = model
        self.system_prompt = system_prompt
        self.setting_sources = setting_sources
        self.permission_mode = permission_mode
        self.stderr = stderr
        self.strict_mcp_config = strict_mcp_config


class StubTextBlock:
    def __init__(self, text) -> None:
        self.text = text


class StubThinkingBlock:
    def __init__(self, thinking="") -> None:
        self.thinking = thinking


class StubToolUseBlock:
    def __init__(self, id, name, input) -> None:  # noqa: A002 - 对齐 SDK 字段名
        self.id = id
        self.name = name
        self.input = input


class StubToolResultBlock:
    def __init__(self, tool_use_id, content=None, is_error=None) -> None:
        self.tool_use_id = tool_use_id
        self.content = content
        self.is_error = is_error


class StubAssistantMessage:
    def __init__(self, content) -> None:
        self.content = content


class StubUserMessage:
    def __init__(self, content) -> None:
        self.content = content


class StubSystemMessage:
    def __init__(self, subtype="init", data=None) -> None:
        self.subtype = subtype
        self.data = data or {}


class StubResultMessage:
    def __init__(
        self,
        subtype="success",
        is_error=False,
        session_id=None,
        *,
        usage=None,
        num_turns=None,
        terminal_reason=None,
        error=None,
    ) -> None:
        self.subtype = subtype
        self.is_error = is_error
        self.session_id = session_id
        if usage is not None:
            self.usage = usage
        if num_turns is not None:
            self.num_turns = num_turns
        if terminal_reason is not None:
            self.terminal_reason = terminal_reason
        if error is not None:
            self.error = error


class _StubSDK(unittest.TestCase):
    messages: list = []
    calls: dict = {}

    def setUp(self) -> None:
        self.calls = {"clients": [], "prompts": [], "closed": 0}
        outer = self

        class StubClient:
            def __init__(self, options=None) -> None:
                self.options = options
                outer.calls["clients"].append(options)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                outer.calls["closed"] += 1
                return False

            async def query(self, prompt, session_id="default"):
                outer.calls["prompts"].append(prompt)

            async def receive_response(self):
                for message in outer.messages:
                    yield message

        module = types.ModuleType("claude_agent_sdk")
        module.HookMatcher = StubHookMatcher
        module.ClaudeAgentOptions = StubAgentOptions
        module.ClaudeSDKClient = StubClient
        module.AssistantMessage = StubAssistantMessage
        module.UserMessage = StubUserMessage
        module.SystemMessage = StubSystemMessage
        module.ResultMessage = StubResultMessage
        module.TextBlock = StubTextBlock
        module.ThinkingBlock = StubThinkingBlock
        module.ToolUseBlock = StubToolUseBlock
        module.ToolResultBlock = StubToolResultBlock

        self._saved = sys.modules.get("claude_agent_sdk")
        sys.modules["claude_agent_sdk"] = module
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._saved is None:
            sys.modules.pop("claude_agent_sdk", None)
        else:
            sys.modules["claude_agent_sdk"] = self._saved

    def gateway(self):
        from lingxi.core.execution.audit import TurnAudit
        from lingxi.core.execution.hooks import ToolGateway
        from lingxi.core.execution.tool_policy import ToolPolicy

        return ToolGateway(policy=ToolPolicy(allowed_tools=("mcp__q__list",)), audit=TurnAudit())


class AgentOptionsShapeTest(_StubSDK):
    def test_hooks_are_installed_and_the_deep_defence_lists_are_explicit(self) -> None:
        from lingxi.adapters.claude_agent_session import build_agent_options
        from lingxi.core.execution.hooks import HOOK_EVENTS, OBSERVATION_ONLY_EVENTS

        gateway = self.gateway()
        options = build_agent_options(gateway, allowed_tools=("mcp__q__list",), stderr_sink=lambda line: None)

        self.assertEqual(set(options.hooks), set(HOOK_EVENTS) | set(OBSERVATION_ONLY_EVENTS))
        self.assertEqual(options.hooks["PreToolUse"][0].hooks, [gateway.on_hook_event])
        self.assertEqual(options.allowed_tools, ["mcp__q__list"])
        self.assertEqual(options.disallowed_tools, [])

    def test_optional_fields_are_only_passed_when_configured(self) -> None:
        from lingxi.adapters.claude_agent_session import build_agent_options

        bare = build_agent_options(self.gateway(), allowed_tools=("mcp__q__list",), stderr_sink=lambda line: None)
        self.assertIsNone(bare.cwd)
        self.assertIsNone(bare.model)
        self.assertIsNone(bare.mcp_servers)

        full = build_agent_options(
            self.gateway(),
            stderr_sink=lambda line: None,
            allowed_tools=("mcp__q__list",),
            mcp_servers={"q": {"type": "http", "url": "https://example.invalid/mcp"}},
            cwd="/tmp/lingxi-workspace",
            model="claude-sonnet-4-5",
            system_prompt="只读问数",
        )
        self.assertEqual(full.cwd, "/tmp/lingxi-workspace")
        self.assertEqual(full.model, "claude-sonnet-4-5")
        self.assertEqual(full.system_prompt, "只读问数")
        self.assertEqual(set(full.mcp_servers), {"q"})

    def test_max_turns_is_passed_when_configured(self) -> None:
        from lingxi.adapters.claude_agent_session import build_agent_options

        options = build_agent_options(
            self.gateway(),
            allowed_tools=("mcp__q__list",),
            max_turns=7,
            stderr_sink=lambda line: None,
        )

        self.assertEqual(options.max_turns, 7)


class MessageNormalisationTest(_StubSDK):
    def test_assistant_text_blocks_become_one_final_text_event(self) -> None:
        from lingxi.adapters.claude_agent_session import normalize_message

        events = normalize_message(StubAssistantMessage([StubTextBlock("日活 "), StubTextBlock("1024。")]))

        self.assertEqual(events, ({"kind": "assistant_message", "text": "日活 1024。"},))

    def test_an_assistant_message_without_any_text_block_does_not_clear_the_final_text(self) -> None:
        from lingxi.adapters.claude_agent_session import normalize_message

        events = normalize_message(StubAssistantMessage([StubToolUseBlock("toolu_1", "mcp__q__list", {})]))

        self.assertEqual(events, ())

    def test_an_explicitly_empty_assistant_text_is_reported_as_empty(self) -> None:
        """空正文是必须如实上报的失败事实，不能靠"跳过空串"掩盖。"""

        from lingxi.adapters.claude_agent_session import normalize_message

        events = normalize_message(StubAssistantMessage([StubTextBlock("")]))

        self.assertEqual(events, ({"kind": "assistant_message", "text": ""},))

    def test_tool_results_carry_id_content_and_error_flag(self) -> None:
        from lingxi.adapters.claude_agent_session import normalize_message

        events = normalize_message(
            StubUserMessage([StubToolResultBlock("toolu_1", '{"data": []}', False)])
        )

        self.assertEqual(
            events,
            ({"kind": "tool_result", "tool_use_id": "toolu_1", "content": '{"data": []}', "is_error": False},),
        )

    def test_result_message_is_normalised_without_being_counted_as_a_tool_call(self) -> None:
        from lingxi.adapters.claude_agent_session import normalize_message

        events = normalize_message(StubResultMessage(subtype="success", is_error=False))

        self.assertEqual(events, ({"kind": "result", "subtype": "success", "is_error": False},))

    def test_result_message_carries_usage_and_termination_metadata_without_model_text(self) -> None:
        from lingxi.adapters.claude_agent_session import normalize_message

        events = normalize_message(
            StubResultMessage(
                usage={"input_tokens": 3, "output_tokens": 4},
                num_turns=2,
                terminal_reason="max_turns",
            )
        )

        self.assertEqual(events[0]["usage"], {"input_tokens": 3, "output_tokens": 4})
        self.assertEqual(events[0]["num_turns"], 2)
        self.assertEqual(events[0]["terminal_reason"], "max_turns")
        self.assertNotIn("result", events[0])

    def test_every_normalised_kind_is_one_the_core_recorder_knows(self) -> None:
        """适配器产出的事件种类必须与 core 记账端的约定完全一致。

        两层各改一半是这类"接线"最容易出的错：适配器改了事件名，记账端照旧忽略，
        审计静默变空，而所有单测各自都还是绿的。
        """

        from lingxi.adapters.claude_agent_session import normalize_message
        from lingxi.core.execution.message_stream import STREAM_EVENT_KINDS

        messages = [
            StubAssistantMessage([StubTextBlock("正文")]),
            StubUserMessage([StubToolResultBlock("toolu_1", "{}", False)]),
            StubResultMessage(),
        ]
        kinds = {event["kind"] for message in messages for event in normalize_message(message)}

        self.assertEqual(kinds, set(STREAM_EVENT_KINDS))

    def test_unknown_message_types_are_ignored_rather_than_guessed(self) -> None:
        from lingxi.adapters.claude_agent_session import normalize_message

        self.assertEqual(normalize_message(StubSystemMessage()), ())
        self.assertEqual(normalize_message(object()), ())


class SingleTurnSessionTest(_StubSDK):
    def test_one_turn_opens_one_session_sends_the_prompt_and_closes_it(self) -> None:
        from lingxi.adapters.claude_agent_session import build_agent_options, run_single_turn

        self.messages = [
            StubAssistantMessage([StubTextBlock("日活 1024。")]),
            StubResultMessage(),
        ]
        options = build_agent_options(self.gateway(), allowed_tools=("mcp__q__list",), stderr_sink=lambda line: None)
        seen: list = []

        asyncio.run(run_single_turn(options=options, prompt="问题", sink=seen.append, timeout_seconds=30))

        self.assertEqual(self.calls["clients"], [options])
        self.assertEqual(self.calls["prompts"], ["问题"])
        self.assertEqual(self.calls["closed"], 1)
        self.assertEqual([event["kind"] for event in seen], ["assistant_message", "result"])

    def test_resume_is_explicit_and_result_session_id_is_observable(self) -> None:
        from lingxi.adapters.claude_agent_session import build_agent_options, run_single_turn

        self.messages = [
            StubAssistantMessage([StubTextBlock("续接结果")]),
            StubResultMessage(session_id="session-new"),
        ]
        options = build_agent_options(self.gateway(), allowed_tools=("mcp__q__list",), stderr_sink=lambda line: None)
        seen: list = []

        asyncio.run(
            run_single_turn(
                options=options,
                prompt="续接问题",
                sink=seen.append,
                timeout_seconds=30,
                resume_session_id="session-old",
            )
        )

        self.assertEqual(getattr(self.calls["clients"][0], "resume"), "session-old")
        self.assertEqual(seen[-1]["session_id"], "session-new")

    def test_business_duration_callback_fires_once_before_drain(self) -> None:
        """#143：业务耗时在收尾之前单独测得一次，不与总耗时混在一起。"""

        from lingxi.adapters.claude_agent_session import build_agent_options, run_single_turn

        self.messages = [StubAssistantMessage([StubTextBlock("日活 1024。")]), StubResultMessage()]
        options = build_agent_options(self.gateway(), allowed_tools=("mcp__q__list",), stderr_sink=lambda line: None)
        ticks = iter((100.0, 101.2, 101.25))
        durations: list[float] = []

        asyncio.run(
            run_single_turn(
                options=options,
                prompt="问题",
                sink=lambda event: None,
                timeout_seconds=30,
                clock=lambda: next(ticks),
                on_business_duration=durations.append,
            )
        )

        self.assertEqual(len(durations), 1)
        self.assertAlmostEqual(durations[0], 1.2, places=9)

    def test_drain_grace_is_independent_and_bounded_from_the_business_budget(self) -> None:
        """#143：收尾宽限独立且有界——业务阶段早已完成，收尾自己挂起时必须在
        它自己的宽限内失败，而不是被业务墙钟提前判定、也不是永久挂起。"""

        from lingxi.adapters.claude_agent_session import (
            DrainTimeoutError,
            build_agent_options,
            run_single_turn,
        )

        self.messages = [StubAssistantMessage([StubTextBlock("日活 1024。")]), StubResultMessage()]
        module = sys.modules["claude_agent_sdk"]

        class HangingDrainClient(module.ClaudeSDKClient):  # type: ignore[misc]
            async def __aexit__(self, *exc_info):
                await asyncio.sleep(3600)
                return await super().__aexit__(*exc_info)

        module.ClaudeSDKClient = HangingDrainClient
        options = build_agent_options(self.gateway(), allowed_tools=("mcp__q__list",), stderr_sink=lambda line: None)

        with self.assertRaises(DrainTimeoutError):
            asyncio.run(
                run_single_turn(
                    options=options,
                    prompt="问题",
                    sink=lambda event: None,
                    timeout_seconds=30,
                    drain_grace_seconds=0.01,
                )
            )

    def test_business_timeout_still_drains_within_its_own_grace(self) -> None:
        """业务阶段超时不得连带跳过收尾：__aexit__ 必须仍然被调用一次。"""

        from lingxi.adapters.claude_agent_session import build_agent_options, run_single_turn

        module = sys.modules["claude_agent_sdk"]

        class HangingBusinessClient(module.ClaudeSDKClient):  # type: ignore[misc]
            def receive_response(self):
                async def _gen():
                    await asyncio.sleep(3600)
                    yield  # pragma: no cover

                return _gen()

        module.ClaudeSDKClient = HangingBusinessClient
        options = build_agent_options(self.gateway(), allowed_tools=("mcp__q__list",), stderr_sink=lambda line: None)

        with self.assertRaises(TimeoutError):
            asyncio.run(
                run_single_turn(
                    options=options,
                    prompt="问题",
                    sink=lambda event: None,
                    timeout_seconds=0.01,
                )
            )

        self.assertEqual(self.calls["closed"], 1)

    def test_business_timeout_and_drain_timeout_together_do_not_mask_turn_timeout(self) -> None:
        """复查发现：业务墙钟超时与收尾同时超时时，不得让 DrainTimeoutError 替换
        掉正在传播的业务 TimeoutError——那会把 turn_timeout 终态压成
        drain_timeout，且失败文案会谎称"已完成业务执行"（业务阶段其实也没跑
        完）。改坏 ``run_single_turn`` 收尾里的异常吞掉逻辑（让它重新 raise
        DrainTimeoutError）必须让本用例变红。"""

        from lingxi.adapters.claude_agent_session import (
            DrainTimeoutError,
            build_agent_options,
            run_single_turn,
        )

        module = sys.modules["claude_agent_sdk"]

        class DoubleHangClient(module.ClaudeSDKClient):  # type: ignore[misc]
            def receive_response(self):
                async def _gen():
                    await asyncio.sleep(3600)
                    yield  # pragma: no cover

                return _gen()

            async def __aexit__(self, *exc_info):
                await asyncio.sleep(3600)
                return await super().__aexit__(*exc_info)  # pragma: no cover

        module.ClaudeSDKClient = DoubleHangClient
        options = build_agent_options(self.gateway(), allowed_tools=("mcp__q__list",), stderr_sink=lambda line: None)

        with self.assertRaises(TimeoutError) as ctx:
            asyncio.run(
                run_single_turn(
                    options=options,
                    prompt="问题",
                    sink=lambda event: None,
                    timeout_seconds=0.01,
                    drain_grace_seconds=0.01,
                )
            )

        # DrainTimeoutError 不是 TimeoutError 的子类：断言异常类型明确排除它，
        # 不只是靠 assertRaises(TimeoutError) 顺带覆盖。
        self.assertNotIsInstance(ctx.exception, DrainTimeoutError)
        # 收尾自己也挂起、被墙钟取消，从未真正跑完一次。
        self.assertEqual(self.calls["closed"], 0)

    def test_aenter_failure_does_not_call_aexit(self) -> None:
        """建连本身失败（entered 仍为 False）时不得调用 __aexit__——与
        ``async with`` 的真实协议一致，避免对一个没建成的客户端做收尾。"""

        from lingxi.adapters.claude_agent_session import build_agent_options, run_single_turn

        module = sys.modules["claude_agent_sdk"]

        class FailingEnterClient(module.ClaudeSDKClient):  # type: ignore[misc]
            async def __aenter__(self):
                raise RuntimeError("连接失败")

        module.ClaudeSDKClient = FailingEnterClient
        options = build_agent_options(self.gateway(), allowed_tools=("mcp__q__list",), stderr_sink=lambda line: None)

        with self.assertRaises(RuntimeError):
            asyncio.run(
                run_single_turn(
                    options=options,
                    prompt="问题",
                    sink=lambda event: None,
                    timeout_seconds=30,
                )
            )

        self.assertEqual(self.calls["closed"], 0)


class LocalInterruptCausalityTest(_StubSDK):
    """#201：本进程发出的 ``interrupt()`` 必须留下**本地事实**。

    上层只能靠这条事实区分"这一轮是被本地 ``/stop`` 打断的"与"SDK 自己 abort 了"
    ——``terminal_reason=aborted_*`` 是 SDK 自报的，无人 stop 时也会出现，用"有人
    stop 过 + SDK 自报 abort"反推因果会掩盖真实的 SDK 终止失败
    （PR #198 一级独立审查 P1-2 裁定）。
    """

    def test_the_interrupt_fact_is_recorded_before_the_call_can_return(self) -> None:
        """SDK 抢在 ``interrupt()`` 返回之前就收完这一轮时，事实仍然成立。

        这是 #201 的竞态形状：``AgentSessionInterrupted`` 不会抛出，回合看起来
        完全就是"SDK 自己 abort 掉了"。把回调挪到 ``await result`` 之后（或删掉
        它）必须让本用例变红。
        """

        from lingxi.adapters.claude_agent_session import build_agent_options, run_single_turn

        module = sys.modules["claude_agent_sdk"]
        interrupt_sent = asyncio.Event()

        class RacingClient(module.ClaudeSDKClient):  # type: ignore[misc]
            async def interrupt(self):
                # 中断请求已经送到 SDK，但这次调用永远不返回——用一个确定的
                # 同步点复现竞态，不靠事件循环的调度巧合。
                interrupt_sent.set()
                await asyncio.sleep(3600)

            def receive_response(self):
                async def _gen():
                    await interrupt_sent.wait()
                    yield StubResultMessage(
                        subtype="error_during_execution",
                        is_error=True,
                        terminal_reason="aborted_streaming",
                    )

                return _gen()

        module.ClaudeSDKClient = RacingClient
        options = build_agent_options(
            self.gateway(), allowed_tools=("mcp__q__list",), stderr_sink=lambda line: None
        )
        seen: list = []
        marks: list = []

        async def scenario() -> None:
            stop_event = asyncio.Event()
            stop_event.set()
            await run_single_turn(
                options=options,
                prompt="问题",
                sink=seen.append,
                timeout_seconds=30,
                stop_event=stop_event,
                on_interrupt_requested=lambda: marks.append("interrupt"),
            )

        asyncio.run(scenario())

        self.assertEqual(marks, ["interrupt"])
        self.assertEqual(seen[-1]["terminal_reason"], "aborted_streaming")

    def test_without_a_stop_no_local_interrupt_fact_is_produced(self) -> None:
        """无人 stop 时 SDK 自行 abort：本地事实不得凭空出现（回归锁）。"""

        from lingxi.adapters.claude_agent_session import build_agent_options, run_single_turn

        self.messages = [
            StubResultMessage(
                subtype="error_during_execution", is_error=True, terminal_reason="aborted_streaming"
            )
        ]
        options = build_agent_options(
            self.gateway(), allowed_tools=("mcp__q__list",), stderr_sink=lambda line: None
        )
        marks: list = []

        asyncio.run(
            run_single_turn(
                options=options,
                prompt="问题",
                sink=lambda event: None,
                timeout_seconds=30,
                on_interrupt_requested=lambda: marks.append("interrupt"),
            )
        )

        self.assertEqual(marks, [])


if __name__ == "__main__":
    unittest.main()
