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
import importlib.util
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
        max_buffer_size=None,
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
        self.max_buffer_size = max_buffer_size


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
    """与真实 ``claude_agent_sdk.ResultMessage``（0.2.128）字段对齐的桩。

    真实 dataclass 没有 ``error`` 单数字段——那是本仓库此前的读取错误（见
    ``adapters/claude_agent_session.py`` 的 IN-10/#650 修复）。真实字段是
    ``errors: list[str] | None`` 与 ``api_error_status: int | None``，这里只
    构造本模块用得到的子集，不追求覆盖 dataclass 全部字段。
    """

    def __init__(
        self,
        subtype="success",
        is_error=False,
        session_id=None,
        *,
        usage=None,
        num_turns=None,
        terminal_reason=None,
        errors=None,
        api_error_status=None,
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
        if errors is not None:
            self.errors = errors
        if api_error_status is not None:
            self.api_error_status = api_error_status


class _StubSDK(unittest.TestCase):
    messages: list = []
    calls: dict = {}

    def setUp(self) -> None:
        self.calls = {"clients": [], "prompts": [], "closed": 0, "mcp_status": 0}
        # MCP 会话级连接失败审计（Issue #349 剩余范围）：默认返回值形状对齐真实
        # SDK 的 ``McpStatusResponse``（``{"mcpServers": [...]}``），测试按需覆盖。
        self.mcp_status: object = {"mcpServers": []}
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

            async def get_mcp_status(self):
                outer.calls["mcp_status"] += 1
                return outer.mcp_status

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
        options = build_agent_options(
            gateway, allowed_tools=("mcp__q__list",), stderr_sink=lambda line: None
        )

        self.assertEqual(set(options.hooks), set(HOOK_EVENTS) | set(OBSERVATION_ONLY_EVENTS))
        self.assertEqual(options.hooks["PreToolUse"][0].hooks, [gateway.on_hook_event])
        self.assertEqual(options.allowed_tools, ["mcp__q__list"])
        self.assertEqual(options.disallowed_tools, [])

    def test_the_three_isolation_options_are_pinned_value_by_value(self) -> None:
        """W-2（Trace #544 S-2b）：``setting_sources`` / ``strict_mcp_config`` /
        ``permission_mode`` 三项**逐值**钉死，改任何一项都必须变红。

        此前这里只断言了装配形状（hooks 装上了、allow/disallow 两个列表是显式的），
        这三项一个都没被断言过——也就是说把它们改掉、甚至整项删掉，全套测试照样
        绿。它们三个各自堵的是一条**绕过只读屏障**的路，不是可调风格：

        - ``setting_sources=[]``：不加载用户/项目设置源。不显式传空，宿主机
          ``~/.claude/settings.json`` 里的 ``permissions`` / ``hooks`` /
          ``mcpServers`` 就会与屏障并存，``PreToolUse`` 单点判定的绕过面被无声
          打开（架构设计 5.3 的隔离边界）。
        - ``strict_mcp_config=True``：``setting_sources=[]`` **挡不住 MCP 来源**。
          SDK 0.2.128 的这个开关默认 ``False``，项目 ``.mcp.json`` / 用户级 /
          插件 MCP 仍会被加载，甚至可能有同名服务器顶替白名单工具。**与 W-1 的
          工作目录合同是同一条防线的两半**：即使 ``cwd`` 选错、目录里躺着别人的
          ``.mcp.json``，这一项也让它不会被当成 MCP 配置源加载。
        - ``permission_mode="dontAsk"``：L4a 已验证的取值——「当前能力」2026-07-28
          的定向补测就是在这个取值下确认 hook 拒绝真的阻止执行。不传就落到 SDK
          默认值，那个取值下屏障是否同样有效**从未验证过**。

        断言用 ``assertIs`` 而不是 ``assertEqual`` 判布尔：``1 == True`` 在
        Python 里成立，``assertEqual`` 会把 ``strict_mcp_config=1`` 判绿。
        """

        from lingxi.adapters.claude_agent_session import build_agent_options

        options = build_agent_options(
            self.gateway(), allowed_tools=("mcp__q__list",), stderr_sink=lambda line: None
        )

        self.assertEqual(
            options.setting_sources,
            [],
            "setting_sources 必须显式传空列表：不传（None）等于让宿主机的 "
            "~/.claude/settings.json 与屏障并存",
        )
        self.assertIs(
            options.strict_mcp_config,
            True,
            "strict_mcp_config 必须显式为 True：SDK 0.2.128 默认 False，"
            "项目/用户级/插件 MCP 仍会被加载，同名服务器可以顶替白名单工具",
        )
        self.assertEqual(
            options.permission_mode,
            "dontAsk",
            "permission_mode 必须是 L4a 实测验证过的 dontAsk；换成别的取值时"
            "「hook 拒绝真的阻止执行」这条结论未经验证",
        )

        # 带上全部可选配置之后三项仍然不许漂移：真实 queue 路径走的就是这一支
        # （逐用户 mcp_servers + 逐用户 cwd）。
        full = build_agent_options(
            self.gateway(),
            allowed_tools=("mcp__q__list",),
            stderr_sink=lambda line: None,
            mcp_servers={"q": {"type": "http", "url": "https://example.invalid/mcp"}},
            cwd="/tmp/lingxi-workspace",
            model="claude-sonnet-4-5",
            system_prompt="只读问数",
            max_turns=7,
        )
        self.assertEqual(full.setting_sources, [])
        self.assertIs(full.strict_mcp_config, True)
        self.assertEqual(full.permission_mode, "dontAsk")

    def test_optional_fields_are_only_passed_when_configured(self) -> None:
        from lingxi.adapters.claude_agent_session import build_agent_options

        bare = build_agent_options(
            self.gateway(), allowed_tools=("mcp__q__list",), stderr_sink=lambda line: None
        )
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

    def test_sdk_message_buffer_limit_is_always_raised_above_the_1mib_default(self) -> None:
        """2026-08-23 真实故障回归：问数 MCP 一条未加窄过滤的指标查询回执约
        9.3MiB，SDK 默认 1MiB 读流缓冲直接把整个会话打崩。上限必须显式传入，
        且大于实测过的回执体量。"""

        from lingxi.adapters.claude_agent_session import (
            DEFAULT_MAX_SDK_MESSAGE_BYTES,
            build_agent_options,
        )

        options = build_agent_options(
            self.gateway(), allowed_tools=("mcp__q__list",), stderr_sink=lambda line: None
        )

        self.assertEqual(options.max_buffer_size, DEFAULT_MAX_SDK_MESSAGE_BYTES)
        # 精确锁定取值（外部独立审查 2026-08-23 P2-1）：只断言「大于 10MiB」时，
        # 常量被误改到 1GiB 也全绿——上限同时是进程内存预算的一部分（并发 16 路
        # 每路一条最坏 32MiB 消息），涨它必须是一次显式、被审阅的决定。
        self.assertEqual(DEFAULT_MAX_SDK_MESSAGE_BYTES, 32 * 1024 * 1024)

    def test_buffer_overflow_is_recognised_from_the_flattened_sdk_error(self) -> None:
        """SDK 0.2.128 把缓冲超限压平成裸 Exception（文本来自
        subprocess_cli 的固定措辞），识别只能按文本；普通失败不得被误判。"""

        from lingxi.adapters.claude_agent_session import is_message_buffer_overflow

        self.assertTrue(
            is_message_buffer_overflow(
                Exception(
                    "Failed to decode JSON: JSON message exceeded maximum buffer "
                    "size of 1048576 bytes"
                )
            )
        )
        self.assertFalse(is_message_buffer_overflow(Exception("connection reset by peer")))
        self.assertFalse(is_message_buffer_overflow(TimeoutError()))
        # 收窄后的否定面（外部独立审查 2026-08-23 P2-2）：不带 SDK 固定前缀段的
        # 相似措辞不得命中——别的子系统的"buffer size"类报错不是查询结果过大。
        self.assertFalse(
            is_message_buffer_overflow(Exception("socket recv exceeded maximum buffer size"))
        )


class MessageNormalisationTest(_StubSDK):
    def test_assistant_text_blocks_become_one_final_text_event(self) -> None:
        from lingxi.adapters.claude_agent_session import normalize_message

        events = normalize_message(
            StubAssistantMessage([StubTextBlock("日活 "), StubTextBlock("1024。")])
        )

        self.assertEqual(events, ({"kind": "assistant_message", "text": "日活 1024。"},))

    def test_an_assistant_message_without_any_text_block_does_not_clear_the_final_text(
        self,
    ) -> None:
        from lingxi.adapters.claude_agent_session import normalize_message

        events = normalize_message(
            StubAssistantMessage([StubToolUseBlock("toolu_1", "mcp__q__list", {})])
        )

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
            (
                {
                    "kind": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": '{"data": []}',
                    "is_error": False,
                },
            ),
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

    def test_result_message_errors_and_api_error_status_are_carried_when_present(self) -> None:
        """IN-10/#650：真实字段是 ``errors``/``api_error_status``，不是 ``error``。"""
        from lingxi.adapters.claude_agent_session import normalize_message

        events = normalize_message(
            StubResultMessage(
                is_error=True,
                errors=["rate limited, retrying", "still rate limited"],
                api_error_status=429,
            )
        )

        self.assertEqual(events[0]["errors"], ["rate limited, retrying", "still rate limited"])
        self.assertEqual(events[0]["api_error_status"], 429)

    def test_result_message_without_errors_field_produces_no_errors_key(self) -> None:
        """``errors``/``api_error_status`` 为 ``None``（或字段不存在）时不写入事件——
        与其它"配了才传"的观测字段（usage/num_turns 等）同一约定。"""
        from lingxi.adapters.claude_agent_session import normalize_message

        events = normalize_message(StubResultMessage(errors=None, api_error_status=None))

        self.assertNotIn("errors", events[0])
        self.assertNotIn("api_error_status", events[0])

    def test_result_message_empty_or_non_string_errors_produce_no_errors_key(self) -> None:
        from lingxi.adapters.claude_agent_session import normalize_message

        empty = normalize_message(StubResultMessage(errors=[]))
        self.assertNotIn("errors", empty[0])

        only_non_strings = normalize_message(StubResultMessage(errors=[None, 42, ""]))
        self.assertNotIn("errors", only_non_strings[0])

    def test_result_message_overlong_error_item_is_truncated_with_a_visible_marker(self) -> None:
        """单条错误文本超过 500 字符时截断并留下可见标记，不做无界透传。"""
        from lingxi.adapters.claude_agent_session import (
            _MAX_RESULT_ERROR_ITEM_CHARS,
            normalize_message,
        )

        oversized = "x" * (_MAX_RESULT_ERROR_ITEM_CHARS + 100)
        events = normalize_message(StubResultMessage(errors=[oversized]))

        kept = events[0]["errors"][0]
        self.assertEqual(len(kept), _MAX_RESULT_ERROR_ITEM_CHARS + len("…[TRUNCATED]"))
        self.assertTrue(kept.endswith("…[TRUNCATED]"))
        self.assertTrue(kept.startswith("x" * _MAX_RESULT_ERROR_ITEM_CHARS))

    def test_result_message_too_many_errors_are_capped_with_a_visible_marker(self) -> None:
        """条数超过上限时截断并追加一条可见的"省略了几条"标记，不静默丢弃。"""
        from lingxi.adapters.claude_agent_session import (
            _MAX_RESULT_ERROR_ITEMS,
            normalize_message,
        )

        many = [f"error-{i}" for i in range(_MAX_RESULT_ERROR_ITEMS + 5)]
        events = normalize_message(StubResultMessage(errors=many))

        kept = events[0]["errors"]
        self.assertEqual(len(kept), _MAX_RESULT_ERROR_ITEMS)
        self.assertEqual(kept[:-1], many[: _MAX_RESULT_ERROR_ITEMS - 1])
        self.assertIn("omitted", kept[-1])

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


def _claude_agent_sdk_available() -> bool:
    return importlib.util.find_spec("claude_agent_sdk") is not None


# 与 tests/test_document_delivery.py 同款门控：未装 worker extras 的机器上直接
# import 真实 SDK 会在收集阶段就 ModuleNotFoundError，表现成一堆 ERROR 而不是
# 清晰的 skip，与代码框架"全量套件须可在无外部依赖机器上运行"的承诺冲突。
CLAUDE_AGENT_SDK_SKIP_REASON = (
    "跳过：未安装 claude_agent_sdk（worker extras），真实 SDK 字段契约未核对"
)


def _real_result_message(**overrides):
    """按固定版 0.2.128 的真实 ``ResultMessage`` 必填字段构造一个默认实例。

    真实 dataclass（``claude_agent_sdk/types.py``）里 ``subtype``/``duration_ms``/
    ``duration_api_ms``/``is_error``/``num_turns``/``session_id`` 没有默认值，
    必须显式给出；``errors``/``api_error_status`` 等可选字段按各测试用例覆盖。
    """
    from claude_agent_sdk import ResultMessage

    fields = {
        "subtype": "success",
        "duration_ms": 100,
        "duration_api_ms": 80,
        "is_error": False,
        "num_turns": 1,
        "session_id": "session-real",
    }
    fields.update(overrides)
    return ResultMessage(**fields)


@unittest.skipUnless(_claude_agent_sdk_available(), CLAUDE_AGENT_SDK_SKIP_REASON)
class RealResultMessageContractTest(unittest.TestCase):
    """用**真实** ``claude_agent_sdk.ResultMessage``（本仓库 worker extras 固定的
    0.2.128）核对 IN-10/#650 的字段名假设。

    全仓此前没有任何测试真正 ``import`` 过这个真实类型——本文件与
    ``check_agent_sdk_binding.py`` 都只核对 ``ClaudeAgentOptions``/``HookMatcher``
    这一侧，``ResultMessage`` 的字段名完全靠桩模块假设，而桩模块的假设（单数
    ``error``）恰恰与真实 dataclass 分岔了两个版本却没有任何测试发现。这里补
    上这条：直接用真实 dataclass 构造离线契约样例，不连接网络、不调用模型。
    """

    def test_errors_with_values_are_bounded_and_carried(self) -> None:
        from lingxi.adapters.claude_agent_session import normalize_message

        message = _real_result_message(
            is_error=True, errors=["boom", "boom again"], api_error_status=None
        )

        events = normalize_message(message)

        self.assertEqual(events[0]["errors"], ["boom", "boom again"])
        self.assertNotIn("api_error_status", events[0])

    def test_errors_none_produces_no_errors_key(self) -> None:
        from lingxi.adapters.claude_agent_session import normalize_message

        events = normalize_message(_real_result_message(errors=None))

        self.assertNotIn("errors", events[0])

    def test_errors_overlong_and_overcount_are_bounded_with_visible_markers(self) -> None:
        from lingxi.adapters.claude_agent_session import (
            _MAX_RESULT_ERROR_ITEM_CHARS,
            _MAX_RESULT_ERROR_ITEMS,
            normalize_message,
        )

        many_long = [
            f"e{i}:" + "y" * _MAX_RESULT_ERROR_ITEM_CHARS
            for i in range(_MAX_RESULT_ERROR_ITEMS + 3)
        ]
        events = normalize_message(_real_result_message(is_error=True, errors=many_long))

        kept = events[0]["errors"]
        self.assertEqual(len(kept), _MAX_RESULT_ERROR_ITEMS)
        self.assertTrue(kept[0].endswith("…[TRUNCATED]"))
        self.assertIn("omitted", kept[-1])

    def test_api_error_status_none_produces_no_key(self) -> None:
        from lingxi.adapters.claude_agent_session import normalize_message

        events = normalize_message(_real_result_message(api_error_status=None))

        self.assertNotIn("api_error_status", events[0])

    def test_api_error_status_429_is_carried(self) -> None:
        from lingxi.adapters.claude_agent_session import normalize_message

        events = normalize_message(_real_result_message(is_error=True, api_error_status=429))

        self.assertEqual(events[0]["api_error_status"], 429)

    def test_api_error_status_529_is_carried(self) -> None:
        from lingxi.adapters.claude_agent_session import normalize_message

        events = normalize_message(_real_result_message(is_error=True, api_error_status=529))

        self.assertEqual(events[0]["api_error_status"], 529)

    def test_real_dataclass_has_no_singular_error_field(self) -> None:
        """钉住这次要修的 bug 的前提事实：真实类型上不存在单数 ``error``。"""
        message = _real_result_message()

        self.assertFalse(hasattr(message, "error"))
        self.assertTrue(hasattr(message, "errors"))
        self.assertTrue(hasattr(message, "api_error_status"))


class SingleTurnSessionTest(_StubSDK):
    def test_one_turn_opens_one_session_sends_the_prompt_and_closes_it(self) -> None:
        from lingxi.adapters.claude_agent_session import build_agent_options, run_single_turn

        self.messages = [
            StubAssistantMessage([StubTextBlock("日活 1024。")]),
            StubResultMessage(),
        ]
        options = build_agent_options(
            self.gateway(), allowed_tools=("mcp__q__list",), stderr_sink=lambda line: None
        )
        seen: list = []

        asyncio.run(
            run_single_turn(options=options, prompt="问题", sink=seen.append, timeout_seconds=30)
        )

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
        options = build_agent_options(
            self.gateway(), allowed_tools=("mcp__q__list",), stderr_sink=lambda line: None
        )
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
        options = build_agent_options(
            self.gateway(), allowed_tools=("mcp__q__list",), stderr_sink=lambda line: None
        )
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
        options = build_agent_options(
            self.gateway(), allowed_tools=("mcp__q__list",), stderr_sink=lambda line: None
        )

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
        options = build_agent_options(
            self.gateway(), allowed_tools=("mcp__q__list",), stderr_sink=lambda line: None
        )

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
        options = build_agent_options(
            self.gateway(), allowed_tools=("mcp__q__list",), stderr_sink=lambda line: None
        )

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
        options = build_agent_options(
            self.gateway(), allowed_tools=("mcp__q__list",), stderr_sink=lambda line: None
        )

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

        这是 #201 的竞态形状：``AgentSessionInterruptedError`` 不会抛出，回合看起来
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


class McpStatusProbeTest(_StubSDK):
    """建连后 ``get_mcp_status()`` 的单次拉取（Issue #349 剩余范围，Gate G-2 结论
    A）。本模块"只做形状转换"：这里只证明本侧接线（调用一次、原样转发、异常不
    传播），不证明真实 SDK 会不会真的返回这些状态——那是 L4a 的职责。"""

    def test_probe_fires_once_after_aenter_and_passes_the_raw_dict_through(self) -> None:
        from lingxi.adapters.claude_agent_session import build_agent_options, run_single_turn

        self.messages = [StubResultMessage()]
        self.mcp_status = {"mcpServers": [{"name": "query", "status": "failed", "error": "boom"}]}
        options = build_agent_options(
            self.gateway(), allowed_tools=("mcp__q__list",), stderr_sink=lambda line: None
        )
        seen_status: list = []

        asyncio.run(
            run_single_turn(
                options=options,
                prompt="问题",
                sink=lambda event: None,
                timeout_seconds=30,
                on_mcp_status=seen_status.append,
            )
        )

        self.assertEqual(self.calls["mcp_status"], 1)
        self.assertEqual(seen_status, [self.mcp_status])
        # 原样转发：不是深拷贝也不是重新构造，本层不解读、不改写任何字段。
        self.assertIs(seen_status[0], self.mcp_status)

    def test_no_callback_means_the_probe_is_never_called(self) -> None:
        """回调未注入（``None``，默认值）→ 行为与改动前逐字相同：不多一次
        control-protocol 往返。"""

        from lingxi.adapters.claude_agent_session import build_agent_options, run_single_turn

        self.messages = [StubResultMessage()]
        options = build_agent_options(
            self.gateway(), allowed_tools=("mcp__q__list",), stderr_sink=lambda line: None
        )

        asyncio.run(
            run_single_turn(
                options=options,
                prompt="问题",
                sink=lambda event: None,
                timeout_seconds=30,
            )
        )

        self.assertEqual(self.calls["mcp_status"], 0)

    def test_get_mcp_status_failure_does_not_fail_the_turn_and_is_traced(self) -> None:
        """``get_mcp_status()`` 自身异常不得让回合失败：包住、经既有 ``options.
        stderr`` 通道留痕后继续——观测缺口修复不能反过来制造新的故障面。"""

        from lingxi.adapters.claude_agent_session import build_agent_options, run_single_turn

        self.messages = [StubAssistantMessage([StubTextBlock("日活 1024。")]), StubResultMessage()]
        module = sys.modules["claude_agent_sdk"]

        class FailingMcpStatusClient(module.ClaudeSDKClient):  # type: ignore[misc]
            async def get_mcp_status(self):
                raise RuntimeError("probe boom")

        module.ClaudeSDKClient = FailingMcpStatusClient
        stderr_lines: list = []
        options = build_agent_options(
            self.gateway(), allowed_tools=("mcp__q__list",), stderr_sink=stderr_lines.append
        )
        seen: list = []
        callback_calls: list = []

        asyncio.run(
            run_single_turn(
                options=options,
                prompt="问题",
                sink=seen.append,
                timeout_seconds=30,
                on_mcp_status=callback_calls.append,
            )
        )

        # 回合本身正常收口：消息流照常被处理，异常没有从这次探针"漏"到业务路径。
        self.assertEqual([event["kind"] for event in seen], ["assistant_message", "result"])
        self.assertEqual(callback_calls, [], "探针失败时不应该拿到任何伪造的状态")
        self.assertTrue(
            any("get_mcp_status probe failed" in line for line in stderr_lines),
            "探针异常必须经既有 stderr 通道留痕，不能静默吞掉",
        )


if __name__ == "__main__":
    unittest.main()
