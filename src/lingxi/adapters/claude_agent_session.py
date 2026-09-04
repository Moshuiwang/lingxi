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
import re
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from lingxi.core.execution.hooks import ToolGateway

from .claude_agent_hooks import build_hook_matchers

DEFAULT_DRAIN_GRACE_SECONDS = 30.0

# SDK 读流的单条消息缓冲上限。不传时 SDK 默认 1MiB，而真实链路实测过问数
# MCP 一次未加窄过滤的查询能返回近 10MiB 的单条工具回执，会把整个会话读流
# 打崩。取 32MiB（留足安全余量）；这是传输层健壮性常量，不是部署旋钮。
# 超过它的回执按 ``result_too_large`` 分类失败（见 ``apps/worker/turn.py``）。
DEFAULT_MAX_SDK_MESSAGE_BYTES = 32 * 1024 * 1024

# SDK 把读流侧的缓冲超限压平成裸 Exception 投回业务迭代器，类型信息已丢失，
# 只能按错误文本识别；匹配 SDK 固定模板的完整形状（含字节数尾部），不用更
# 通用的子串，避免别的子系统报错误报成「查询结果过大」。升级 SDK 版本时
# 需核对该模板是否仍在。
_MESSAGE_BUFFER_OVERFLOW_PATTERN = re.compile(
    r"JSON message exceeded maximum buffer size of \d+ bytes"
)


def is_message_buffer_overflow(error: BaseException) -> bool:
    """这次失败是否为「单条 SDK 消息超过读流缓冲上限」（工具回执过大的典型形状）。"""
    return _MESSAGE_BUFFER_OVERFLOW_PATTERN.search(str(error)) is not None


class AgentSessionInterruptedError(Exception):
    """worker 收到 /stop 后要求 SDK 尽快中断当前回合。"""


class DrainTimeoutError(Exception):
    """会话收尾（``ClaudeSDKClient.__aexit__``）超过独立宽限。

    收尾宽限与业务执行预算彼此独立、各自有界：业务墙钟超时不得连带截断收尾。
    这个异常只在业务阶段没有别的异常在传播、且收尾本身也挂起时才触发；业务
    阶段已有异常在传播时（如业务墙钟 ``TimeoutError``）收尾又同时超时，不
    抛这个类——原始业务异常继续传播，不被替换成一个更含糊的收尾失败。
    """


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
    ``PreToolUse`` 默认拒绝——只列允许的 MCP 工具不等于其他内置工具不可执行。

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
        # （可能含令牌）会绕过结构化日志与出口脱敏原样落到进程 stderr。
        # 调用方必须提供脱敏后的结构化落点。
        "stderr": stderr_sink,
        # 隔离边界：不加载用户/项目设置源。不显式传空，宿主机
        # ~/.claude/settings.json 里的 permissions/hooks/mcpServers 就可能与
        # 屏障并存，PreToolUse 单点判定的绕过面会被无声打开。
        "setting_sources": [],
        # setting_sources=[] 挡不住 MCP 来源：SDK 的 strict_mcp_config 默认
        # False，项目/用户级/插件 MCP 仍会被加载，甚至可能有同名服务器顶替
        # 白名单工具，必须显式收紧为「只用我们传入的 mcp_servers」。
        "strict_mcp_config": True,
        # L4a 已验证的取值（dontAsk 下 hook 拒绝真的阻止执行）；不传则落到
        # SDK 默认值，行为未经验证。
        "permission_mode": "dontAsk",
        # 不传则落到 SDK 默认 1MiB，问数 MCP 的真实回执已经撞穿过一次
        # （见模块顶部 DEFAULT_MAX_SDK_MESSAGE_BYTES 的说明）。
        "max_buffer_size": DEFAULT_MAX_SDK_MESSAGE_BYTES,
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


# 已知观测边界：固定版 SDK 的 receive_response() 在第一条 ResultMessage 后
# 立即返回，因此「底层重复发出的第二条终止消息」在本层结构性不可见；这里的
# 双计数只守本侧接线与桩级回归，不声称能检测真实 SDK 的重复投递。
async def run_single_turn(
    *,
    options: Any,
    prompt: str,
    sink: Callable[[Mapping[str, Any]], None],
    timeout_seconds: float,
    resume_session_id: str | None = None,
    stop_event: asyncio.Event | None = None,
    drain_grace_seconds: float = DEFAULT_DRAIN_GRACE_SECONDS,
    clock: Callable[[], float] | None = None,
    on_business_duration: Callable[[float], None] | None = None,
    on_interrupt_requested: Callable[[], None] | None = None,
    on_mcp_status: Callable[[Any], None] | None = None,
) -> None:
    """跑**一个**回合：建会话、发一次提问、把消息流规范化后交给 ``sink``。

    用流式 ``ClaudeSDKClient`` 而不是一次性的 ``query()``，只有流式会话才谈得
    上"屏障在生效"。``timeout_seconds`` 只盖住业务执行预算；收尾用独立有界的
    ``drain_grace_seconds``，不共用业务墙钟也不被其连带截断。
    ``on_interrupt_requested`` 在本进程真的发出 ``interrupt()`` 的那一刻调用
    一次，是"这一轮被本地 stop 打断"的本地事实出口——没有它，``aborted_*``
    类收尾只能靠"有人 stop 过"反推因果，而它在无人 stop 时也会出现。
    ``on_mcp_status`` 见 :func:`_probe_mcp_status`。
    """
    from claude_agent_sdk import ClaudeSDKClient

    session_options = _resolve_session_options(options, resume_session_id)
    measure = clock or time.monotonic
    business_start = measure()
    client = ClaudeSDKClient(options=session_options)
    entered = False
    try:
        # 业务墙钟上限盖住"建连 + 发问 + 收流"：SDK 传输保持连接却不发终止
        # 消息时，receive_response() 的迭代器不会自行结束。
        try:
            async with asyncio.timeout(timeout_seconds):
                await client.__aenter__()
                entered = True
                if on_mcp_status is not None:
                    await _probe_mcp_status(client, session_options, on_mcp_status)
                monitor, interrupted = _start_interrupt_monitor(
                    client, stop_event=stop_event, on_interrupt_requested=on_interrupt_requested
                )
                try:
                    await _stream_turn(client, prompt=prompt, sink=sink, interrupted=interrupted)
                finally:
                    monitor.cancel()
                    try:
                        await monitor
                    except asyncio.CancelledError:
                        pass
        finally:
            if on_business_duration is not None:
                on_business_duration(max(0.0, measure() - business_start))
    finally:
        if entered:
            await _drain_session(client, drain_grace_seconds=drain_grace_seconds)


def _resolve_session_options(options: Any, resume_session_id: str | None) -> Any:
    """按 ``resume_session_id`` 派生本次会话选项；未指定时原样返回 ``options``。

    SDK options 是 dataclass；测试桩未必是，因此保留一个不改变原对象的浅拷贝
    回退。resume 只在明确续用时传入，新的会话不会偷偷复用旧上下文。
    """
    if not resume_session_id:
        return options
    try:
        from dataclasses import replace

        return replace(options, resume=resume_session_id)
    except (TypeError, ValueError):
        session_options = copy.copy(options)
        setattr(session_options, "resume", resume_session_id)
        return session_options


def _start_interrupt_monitor(
    client: Any,
    *,
    stop_event: asyncio.Event | None,
    on_interrupt_requested: Callable[[], None] | None,
) -> tuple[asyncio.Task, asyncio.Event]:
    """起一个后台任务：等 ``stop_event``，一旦置位就向 SDK 发 ``interrupt()``。

    返回的 ``Event`` 在中断确实发生后置位，供调用方判断本轮是否被本地
    stop 打断。
    """
    interrupted = asyncio.Event()

    async def interrupt_when_requested() -> None:
        if stop_event is None:
            return
        await stop_event.wait()
        interrupt = getattr(client, "interrupt", None)
        if callable(interrupt):
            # 本地事实必须在**发出调用之前**记下：SDK 完全可能在
            # `interrupt()` 返回之前就以 `aborted_streaming`/`aborted_tools`
            # 收完这一轮，此时下面的 `interrupted` 永远来不及置位，回合走
            # 的是"正常结束 + SDK 自报 abort"这条路。记在调用之后就正好
            # 漏掉这一版，用户只会看到通用失败文案。
            if on_interrupt_requested is not None:
                on_interrupt_requested()
            result = interrupt()
            if hasattr(result, "__await__"):
                await result
        interrupted.set()

    return asyncio.create_task(interrupt_when_requested()), interrupted


async def _stream_turn(
    client: Any,
    *,
    prompt: str,
    sink: Callable[[Mapping[str, Any]], None],
    interrupted: asyncio.Event,
) -> None:
    """发一次提问并把消息流规范化后交给 ``sink``；中断标志置位即抛异常。"""
    await client.query(prompt)
    async for message in client.receive_response():
        for event in normalize_message(message):
            sink(event)
        if interrupted.is_set():
            raise AgentSessionInterruptedError()


async def _drain_session(client: Any, *, drain_grace_seconds: float) -> None:
    """会话收尾：把在途异常原样转给 ``__aexit__``，独立宽限超时不覆盖业务异常。

    把当前正在传播的异常（如果有）原样转给 ``__aexit__``，与 ``async with``
    的真实协议一致；本层不解读它的返回值是否要求"吞掉"该异常。收尾自己超时
    时，若业务阶段已有异常在传播（如业务墙钟 ``TimeoutError``），不得用
    ``DrainTimeoutError`` 替换掉它——那会让 ``turn_timeout`` 终态被压成
    ``drain_timeout``，且失败文案谎称"已完成业务执行"；`finally` 块正常收尾
    时 Python 会自动继续传播那个原始异常，不需要手动重新 raise。业务阶段
    没有异常在途时，收尾超时本身才是需要上报的失败。
    """
    exc_type, exc_val, exc_tb = sys.exc_info()
    try:
        async with asyncio.timeout(drain_grace_seconds):
            await client.__aexit__(exc_type, exc_val, exc_tb)
    except TimeoutError:
        if exc_type is None:
            raise DrainTimeoutError(f"会话收尾超过独立宽限 {drain_grace_seconds:g} 秒") from None


async def _probe_mcp_status(
    client: Any, options: Any, on_mcp_status: Callable[[Any], None]
) -> None:
    """建连后查一次 MCP server 连接状态，原样交给调用方。

    只查一次，不轮询：固定版 SDK+CLI 下建连返回时状态已经是终态（已知边界：
    真实链路"慢连接"场景可能仍是 ``pending``，如实报告，判定只认
    ``failed``/``needs-auth``，见 ``apps/worker/turn.py``）。
    ``get_mcp_status()`` 自身异常不得让整个回合失败：这里包住它，经已经接在
    ``options.stderr`` 上的既有出口留痕再继续，不新开落点。
    """
    try:
        status = await client.get_mcp_status()
    except Exception as error:  # 决不能让观测探针拖垮整个回合
        stderr_sink = getattr(options, "stderr", None)
        if callable(stderr_sink):
            stderr_sink(f"get_mcp_status probe failed: {type(error).__name__}: {error}")
        return
    on_mcp_status(status)


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
        return (_result_event(message),)

    return ()


_RESULT_OBSERVATION_FIELDS = (
    "usage",
    "num_turns",
    "duration_ms",
    "duration_api_ms",
    "terminal_reason",
    "usage_source",
)


def _result_event(message: Any) -> dict[str, Any]:
    """把一条 ``ResultMessage`` 收窄成只含观测字段的事件字典。

    字段随 CLI/SDK 版本演进；只传存在的观测字段，不把 ``result`` 或
    ``structured_output`` 这类模型正文带入 usage 摘要。
    """
    event: dict[str, Any] = {
        "kind": "result",
        "subtype": getattr(message, "subtype", None),
        "is_error": bool(getattr(message, "is_error", False)),
    }
    for name in _RESULT_OBSERVATION_FIELDS:
        value = getattr(message, name, None)
        if value is not None:
            event[name] = value
    session_id = getattr(message, "session_id", None)
    if isinstance(session_id, str) and session_id:
        event["session_id"] = session_id
    error_text = getattr(message, "error", None)
    if isinstance(error_text, str) and error_text:
        event["error"] = error_text[:500]
    return event


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
