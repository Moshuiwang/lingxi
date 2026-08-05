"""把一个 Agent SDK 会话的**形状**接到执行层上：会话选项、单回合驱动、消息规范化。

与 ``claude_agent_hooks`` 一样，本模块只做形状转换，不含任何判定或归类逻辑——
判定在 ``lingxi.core.execution.tool_policy``，回执归类在 ``…execution.audit``，
消息流记账在 ``…execution.message_stream``，三者都能在没有 SDK 的 CI 里被完整覆盖。

本层承担的风险恰好是 CI 覆盖不到的那一部分：SDK 的字段名、消息类型名与调用次序。
桩模块锁住我们这一侧（``tests/test_claude_agent_session_adapter.py``），真实 SDK 的
构造冒烟在 ``scripts/ci/check_agent_sdk_binding.py``，事件是否真的触发只有 L4a 能答。
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

from lingxi.core.execution.hooks import ToolGateway

from .claude_agent_hooks import build_hook_matchers

# 依赖到的 SDK 类型名。集中列出，便于真实 SDK 冒烟一次性核对它们是否还在。
MESSAGE_TYPE_NAMES: tuple[str, ...] = (
    "AssistantMessage",
    "UserMessage",
    "ResultMessage",
    "TextBlock",
    "ToolResultBlock",
)


def build_agent_options(
    gateway: ToolGateway,
    *,
    allowed_tools: Iterable[str],
    mcp_servers: Mapping[str, Any] | None = None,
    cwd: str | None = None,
    model: str | None = None,
    system_prompt: str | None = None,
    observe_permission_events: bool = True,
) -> Any:
    """构造装好只读屏障的 ``ClaudeAgentOptions``。

    ``allowed_tools`` 在这里**只是纵深防御**：唯一的判定层是 ``hooks`` 里的
    ``PreToolUse`` 默认拒绝（Issue #23 实测过，只列允许的 MCP 工具不等于其他内置
    工具不可执行）。

    ``disallowed_tools`` 固定留空，这是**有意**的：规则层一旦先拦下调用，该调用就
    不再经过我们的 ``PreToolUse``，拒绝在审计里只剩一条"本层未判定"（`V-执行-09`）。
    屏障的证据价值高于多一层重复拦截。
    """

    from claude_agent_sdk import ClaudeAgentOptions

    kwargs: dict[str, Any] = {
        "allowed_tools": list(allowed_tools),
        "disallowed_tools": [],
        "hooks": build_hook_matchers(gateway, observe_permission_events=observe_permission_events),
    }
    # 未配置的字段一律不传，交给 SDK 自己的默认值；传 None 覆盖默认值是另一种错。
    if mcp_servers:
        kwargs["mcp_servers"] = dict(mcp_servers)
    if cwd:
        kwargs["cwd"] = cwd
    if model:
        kwargs["model"] = model
    if system_prompt:
        kwargs["system_prompt"] = system_prompt
    return ClaudeAgentOptions(**kwargs)


async def run_single_turn(
    *,
    options: Any,
    prompt: str,
    sink: Callable[[Mapping[str, Any]], None],
) -> None:
    """跑**一个**回合：建会话、发一次提问、把消息流规范化后交给 ``sink``。

    用 ``ClaudeSDKClient``（流式会话）而不是一次性的 ``query()``：hook 回调走的是
    控制协议，只有流式会话才谈得上"屏障在生效"。
    """

    from claude_agent_sdk import ClaudeSDKClient

    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            for event in normalize_message(message):
                sink(event)


def normalize_message(message: Any) -> tuple[dict[str, Any], ...]:
    """把一条 SDK 消息转成普通字典事件，认不出的消息返回空元组。

    **不产出事件与产出空正文事件是两回事**：只带工具调用的助手消息不产出事件（它
    不该覆盖已有的最终正文），而带着空文本块的助手消息产出 ``text=""``——"最后一句
    话是空的"是必须如实上报的失败事实，不能靠跳过空串把它盖掉。
    """

    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        TextBlock,
        ToolResultBlock,
        UserMessage,
    )

    if isinstance(message, AssistantMessage):
        texts = [block.text for block in _blocks(message) if isinstance(block, TextBlock)]
        if not texts:
            return ()
        return ({"kind": "assistant_message", "text": "".join(texts)},)

    if isinstance(message, UserMessage):
        return tuple(
            {
                "kind": "tool_result",
                "tool_use_id": block.tool_use_id,
                "content": block.content,
                "is_error": block.is_error,
            }
            for block in _blocks(message)
            if isinstance(block, ToolResultBlock)
        )

    if isinstance(message, ResultMessage):
        return (
            {
                "kind": "result",
                "subtype": getattr(message, "subtype", None),
                "is_error": bool(getattr(message, "is_error", False)),
            },
        )

    return ()


def load_message_types() -> dict[str, Any]:
    """按名取回本模块依赖的 SDK 类型，供真实 SDK 冒烟核对它们是否还存在。"""

    import claude_agent_sdk

    return {name: getattr(claude_agent_sdk, name) for name in MESSAGE_TYPE_NAMES}


def _blocks(message: Any) -> tuple[Any, ...]:
    content = getattr(message, "content", None)
    if isinstance(content, (str, bytes)) or content is None:
        return ()
    try:
        return tuple(content)
    except TypeError:
        return ()
