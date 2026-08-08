"""把一个 Agent SDK 会话的**形状**接到执行层上：会话选项、单回合驱动、消息规范化。

与 ``claude_agent_hooks`` 一样，本模块只做形状转换，不含任何判定或归类逻辑——
判定在 ``lingxi.core.execution.tool_policy``，回执归类在 ``…execution.audit``，
消息流记账在 ``…execution.message_stream``，三者都能在没有 SDK 的 CI 里被完整覆盖。

本层承担的风险恰好是 CI 覆盖不到的那一部分：SDK 的字段名、消息类型名与调用次序。
桩模块锁住我们这一侧（``tests/test_claude_agent_session_adapter.py``），真实 SDK 的
构造冒烟在 ``scripts/ci/check_agent_sdk_binding.py``，事件是否真的触发只有 L4a 能答。
"""

from __future__ import annotations

import asyncio
import copy

from typing import Any, Callable, Iterable, Mapping

from lingxi.core.execution.hooks import ToolGateway

from .claude_agent_hooks import build_hook_matchers


class AgentSessionInterrupted(Exception):
    """worker 收到 /stop 后要求 SDK 尽快中断当前回合。"""

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
    max_turns: int | None = None,
    mcp_servers: Mapping[str, Any] | None = None,
    cwd: str | None = None,
    model: str | None = None,
    system_prompt: str | None = None,
    observe_permission_events: bool = True,
    stderr_sink: Callable[[str], None],
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
        # SDK 子进程的 stderr 不设回调就直接继承 fd 2：启动失败的原始错误
        # （可能含令牌）会绕过结构化日志与出口脱敏原样落到进程 stderr
        # （Codex 复查发现）。调用方必须提供脱敏后的结构化落点。
        "stderr": stderr_sink,
        # 架构设计 5.3 的隔离边界：不加载用户/项目设置源。不显式传空，宿主机
        # ~/.claude/settings.json 里的 permissions/hooks/mcpServers 就可能与屏障
        # 并存，PreToolUse 单点判定的绕过面被无声打开（独立复查发现）。
        "setting_sources": [],
        # setting_sources=[] 挡不住 MCP 来源：SDK 0.2.128 的 strict_mcp_config
        # 默认 False，项目 .mcp.json / 用户级 / 插件 MCP 仍会被加载，甚至可能
        # 有同名服务器顶替白名单工具（终轮 Codex 复查发现）。必须显式收紧为
        # 「只用我们传入的 mcp_servers」。
        "strict_mcp_config": True,
        # L4a 已验证的取值（当前能力 2026-07-28 定向补测就是在 dontAsk 下确认
        # hook 拒绝真的阻止执行）；不传则落到 SDK 默认值，行为未经验证。
        "permission_mode": "dontAsk",
    }
    # 未配置的字段一律不传，交给 SDK 自己的默认值；传 None 覆盖默认值是另一种错。
    if mcp_servers:
        kwargs["mcp_servers"] = dict(mcp_servers)
    if max_turns is not None:
        kwargs["max_turns"] = max_turns
    if cwd:
        kwargs["cwd"] = cwd
    if model:
        kwargs["model"] = model
    if system_prompt:
        kwargs["system_prompt"] = system_prompt
    return ClaudeAgentOptions(**kwargs)


# 已知观测边界：固定版 SDK 的 receive_response() 在第一条 ResultMessage 后立即
# 返回，因此「底层重复发出的第二条终止消息」在本层结构性不可见；双计数守的是
# 本侧接线与桩级回归，不声称能检测真实 SDK 的重复投递（终轮 Codex 复查确认）。
async def run_single_turn(
    *,
    options: Any,
    prompt: str,
    sink: Callable[[Mapping[str, Any]], None],
    timeout_seconds: float,
    resume_session_id: str | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """跑**一个**回合：建会话、发一次提问、把消息流规范化后交给 ``sink``。

    用 ``ClaudeSDKClient``（流式会话）而不是一次性的 ``query()``：hook 回调走的是
    控制协议，只有流式会话才谈得上"屏障在生效"。
    """

    from claude_agent_sdk import ClaudeSDKClient

    # 墙钟上限盖住整个回合：SDK 传输保持连接却不发终止消息时，
    # receive_response() 的迭代器不会自行结束（Codex 复查发现）。
    session_options = options
    if resume_session_id:
        # SDK options 是 dataclass；测试桩未必是 dataclass，所以保留一个不改变原对象的
        # 浅拷贝回退。resume 只在明确续用时传入，新的会话不会偷偷复用旧上下文。
        try:
            from dataclasses import replace

            session_options = replace(options, resume=resume_session_id)
        except (TypeError, ValueError):
            session_options = copy.copy(options)
            setattr(session_options, "resume", resume_session_id)

    async with asyncio.timeout(timeout_seconds):
        async with ClaudeSDKClient(options=session_options) as client:
            interrupted = asyncio.Event()

            async def interrupt_when_requested() -> None:
                if stop_event is None:
                    return
                await stop_event.wait()
                interrupt = getattr(client, "interrupt", None)
                if callable(interrupt):
                    result = interrupt()
                    if hasattr(result, "__await__"):
                        await result
                interrupted.set()

            monitor = asyncio.create_task(interrupt_when_requested())
            try:
                await client.query(prompt)
                async for message in client.receive_response():
                    for event in normalize_message(message):
                        sink(event)
                    if interrupted.is_set():
                        raise AgentSessionInterrupted()
            finally:
                monitor.cancel()
                try:
                    await monitor
                except asyncio.CancelledError:
                    pass


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
        event: dict[str, Any] = {
            "kind": "result",
            "subtype": getattr(message, "subtype", None),
            "is_error": bool(getattr(message, "is_error", False)),
        }
        # ResultMessage 的字段随 CLI / SDK 版本演进；只把存在的观测字段传给
        # recorder，不把 result 或 structured_output 这类模型正文带入 usage 摘要。
        for name in (
            "usage",
            "num_turns",
            "duration_ms",
            "duration_api_ms",
            "terminal_reason",
            "usage_source",
        ):
            value = getattr(message, name, None)
            if value is not None:
                event[name] = value
        session_id = getattr(message, "session_id", None)
        if isinstance(session_id, str) and session_id:
            event["session_id"] = session_id
        error_text = getattr(message, "error", None)
        if isinstance(error_text, str) and error_text:
            event["error"] = error_text[:500]
        return (event,)

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
