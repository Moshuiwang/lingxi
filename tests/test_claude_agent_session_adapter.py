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
        disallowed_tools=None,
        hooks=None,
        mcp_servers=None,
        cwd=None,
        model=None,
        system_prompt=None,
    ) -> None:
        self.allowed_tools = allowed_tools
        self.disallowed_tools = disallowed_tools
        self.hooks = hooks
        self.mcp_servers = mcp_servers
        self.cwd = cwd
        self.model = model
        self.system_prompt = system_prompt


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
    def __init__(self, subtype="success", is_error=False) -> None:
        self.subtype = subtype
        self.is_error = is_error


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
        options = build_agent_options(gateway, allowed_tools=("mcp__q__list",))

        self.assertEqual(set(options.hooks), set(HOOK_EVENTS) | set(OBSERVATION_ONLY_EVENTS))
        self.assertEqual(options.hooks["PreToolUse"][0].hooks, [gateway.on_hook_event])
        self.assertEqual(options.allowed_tools, ["mcp__q__list"])
        self.assertEqual(options.disallowed_tools, [])

    def test_optional_fields_are_only_passed_when_configured(self) -> None:
        from lingxi.adapters.claude_agent_session import build_agent_options

        bare = build_agent_options(self.gateway(), allowed_tools=("mcp__q__list",))
        self.assertIsNone(bare.cwd)
        self.assertIsNone(bare.model)
        self.assertIsNone(bare.mcp_servers)

        full = build_agent_options(
            self.gateway(),
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
        options = build_agent_options(self.gateway(), allowed_tools=("mcp__q__list",))
        seen: list = []

        asyncio.run(run_single_turn(options=options, prompt="问题", sink=seen.append))

        self.assertEqual(self.calls["clients"], [options])
        self.assertEqual(self.calls["prompts"], ["问题"])
        self.assertEqual(self.calls["closed"], 1)
        self.assertEqual([event["kind"] for event in seen], ["assistant_message", "result"])


if __name__ == "__main__":
    unittest.main()
