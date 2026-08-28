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

from typing import Any, Callable, Iterable, Mapping

from lingxi.core.execution.hooks import ToolGateway

from .claude_agent_hooks import build_hook_matchers

DEFAULT_DRAIN_GRACE_SECONDS = 30.0

# SDK 读流的单条消息缓冲上限。SDK 0.2.128 不传时默认 1MiB，而真实链路 2026-08-23
# 实测：问数 MCP 一次未加窄过滤的指标查询返回约 9.3MiB 的**单条**工具回执消息
# （CLI 的"大输出只留 2KB 预览"机制并没有阻止完整数据内嵌在同一条 NDJSON 里），
# 默认上限直接把整个会话读流打崩，用户只看到通用失败文案。取 32MiB（约为实测
# 最大回执的 3.4 倍）；这守的是传输层健壮性，不是部署旋钮，因此是常量而非配置。
# 超过它的回执按 ``result_too_large`` 分类失败（见 ``apps/worker/turn.py``）。
DEFAULT_MAX_SDK_MESSAGE_BYTES = 32 * 1024 * 1024

# SDK 0.2.128 把读流侧的缓冲超限压平成**裸 Exception** 投回业务迭代器（reader 把
# ``{"type": "error", "error": 文本}`` 放进消息流，query 侧再
# ``raise Exception(error_text)``），类型信息在那一步已经丢失，只能按错误文本识别。
# 匹配 SDK 固定模板的完整形状（``_internal/transport/subprocess_cli.py``：
# "JSON message exceeded maximum buffer size of {n} bytes"，含字节数尾部）——
# 外部独立审查 2026-08-23 两轮逐步收窄：裸的 "exceeded maximum buffer size"
# 太通用，别的子系统报错撞上就会被误报成「查询结果过大」。升级 SDK 版本时随
# 验证与门禁 10.0 的重跑一并核对该模板是否仍在。
_MESSAGE_BUFFER_OVERFLOW_PATTERN = re.compile(
    r"JSON message exceeded maximum buffer size of \d+ bytes"
)


def is_message_buffer_overflow(error: BaseException) -> bool:
    """这次失败是否为「单条 SDK 消息超过读流缓冲上限」（工具回执过大的典型形状）。"""

    return _MESSAGE_BUFFER_OVERFLOW_PATTERN.search(str(error)) is not None


class AgentSessionInterrupted(Exception):
    """worker 收到 /stop 后要求 SDK 尽快中断当前回合。"""


class DrainTimeoutError(Exception):
    """会话收尾（``ClaudeSDKClient.__aexit__``）超过独立宽限（#143）。

    收尾宽限与业务执行预算彼此独立、各自有界：业务墙钟超时不得连带截断收尾——
    终态接收、用量回收要在自己的时间窗里跑完。这个异常**只在业务阶段没有别的
    异常在传播、且收尾本身也挂起时**才触发，是"宁可多等一段有界时间，也不静默
    丢终态"的最后一道保护，不是常态。业务阶段已经有异常在传播时（例如业务墙钟
    ``TimeoutError``）收尾又同时挂起，这个类不会被抛出——原始的业务异常会继续
    传播，不被这里替换（复查发现：替换会把 turn_timeout 终态压成
    drain_timeout，并让失败文案谎称"已完成业务执行"）。
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
    drain_grace_seconds: float = DEFAULT_DRAIN_GRACE_SECONDS,
    clock: Callable[[], float] | None = None,
    on_business_duration: Callable[[float], None] | None = None,
    on_interrupt_requested: Callable[[], None] | None = None,
    on_mcp_status: Callable[[Any], None] | None = None,
) -> None:
    """跑**一个**回合：建会话、发一次提问、把消息流规范化后交给 ``sink``。

    用 ``ClaudeSDKClient``（流式会话）而不是一次性的 ``query()``：hook 回调走的是
    控制协议，只有流式会话才谈得上"屏障在生效"。

    耗时口径（#143）：``timeout_seconds`` 只盖住"建连 + 发问 + 收流"这段业务
    执行，是业务执行预算，不是端到端总耗时的承诺；``client.__aexit__``（收尾：
    断开连接、终态接收、用量回收）用**独立、有界**的 ``drain_grace_seconds``，
    不共用业务墙钟，也不因业务阶段已经超时而被连带截断——真实链路已经观测到
    收尾本身有固定开销（约 6 秒），把它算进业务墙钟只会让配置值失去意义。
    ``on_business_duration`` 在业务阶段结束时收到一次耗时（不含收尾），供调用方
    与自己测得的总耗时相减得到收尾耗时；不提供时不影响任何行为。

    ``on_interrupt_requested``（#201）在**本进程真的向 SDK 发出 ``interrupt()``**
    的那一刻被调用一次，是"这一轮被本地 stop 打断"的本地事实出口。调用方据此把
    随后可能出现的 ``aborted_streaming``/``aborted_tools`` 收尾判为中断而不是通用
    失败；没有这个回调，同一件事只能靠"有人 stop 过 + SDK 自报 abort"反推因果，
    而 ``aborted_*`` 在无人 stop 时也会出现，反推会把真实的 SDK 终止失败改写成
    "已停止"（PR #198 一级独立审查 P1-2 裁定）。

    ``on_mcp_status``（Issue #349，Gate G-2 结论 A）在建连（``__aenter__()`` 返回）
    之后被调用**至多一次**，参数是 ``client.get_mcp_status()`` 的原始返回值——
    本层不解读其中任何一个字段，判定（哪些 server 算故障、要不要发审计事件）在
    ``apps/worker/turn.py``，与本模块"只做形状转换"的既有约定一致。不传时（或
    ``get_mcp_status()`` 本身失败时）不影响本函数其余行为，见 ``_probe_mcp_
    status`` 的说明。
    """

    from claude_agent_sdk import ClaudeSDKClient

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

    measure = clock or time.monotonic
    business_start = measure()
    client = ClaudeSDKClient(options=session_options)
    entered = False
    try:
        # 业务墙钟上限盖住"建连 + 发问 + 收流"：SDK 传输保持连接却不发终止
        # 消息时，receive_response() 的迭代器不会自行结束（Codex 复查发现）。
        try:
            async with asyncio.timeout(timeout_seconds):
                await client.__aenter__()
                entered = True
                if on_mcp_status is not None:
                    await _probe_mcp_status(client, session_options, on_mcp_status)
                interrupted = asyncio.Event()

                async def interrupt_when_requested() -> None:
                    if stop_event is None:
                        return
                    await stop_event.wait()
                    interrupt = getattr(client, "interrupt", None)
                    if callable(interrupt):
                        # 本地事实必须在**发出调用之前**记下（#201）：SDK 完全可能
                        # 在 `interrupt()` 返回之前就以 `aborted_streaming` /
                        # `aborted_tools` 收完这一轮，此时下面的 `interrupted` 永远
                        # 来不及置位、`AgentSessionInterrupted` 也不会抛出，回合走
                        # 的是"正常结束 + SDK 自报 abort"这条路。记在调用之后就正好
                        # 漏掉这一版，用户只会看到通用失败文案。
                        if on_interrupt_requested is not None:
                            on_interrupt_requested()
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
        finally:
            if on_business_duration is not None:
                on_business_duration(max(0.0, measure() - business_start))
    finally:
        if entered:
            # 把当前正在传播的异常（如果有）原样转给 __aexit__，与 ``async with``
            # 的真实协议一致；本层不解读它的返回值是否要求"吞掉"该异常——现实
            # 中的 SDK 客户端不会这样做，真的这样做由 L4a 暴露。
            exc_type, exc_val, exc_tb = sys.exc_info()
            try:
                async with asyncio.timeout(drain_grace_seconds):
                    await client.__aexit__(exc_type, exc_val, exc_tb)
            except TimeoutError:
                if exc_type is not None:
                    # 业务阶段已经有异常在传播（典型情况：业务墙钟 TimeoutError，
                    # 但不限于它——任何业务阶段异常都适用），收尾这次自己也超时。
                    # 不能用 DrainTimeoutError（或这里收尾自己的 TimeoutError）
                    # 替换掉正在传播的业务异常：那会让 turn_timeout 终态被压成
                    # drain_timeout，且失败文案会谎称"已完成业务执行"（业务阶段
                    # 其实也没跑完，复查发现，见 #149 修复说明）。这里刻意什么都
                    # 不 raise——``finally`` 块正常收尾时，Python 会自动继续传播
                    # 进入本 ``finally`` 前就已经在途的那个原始异常（``exc_val``），
                    # 不需要、也不应该手动重新 raise 它。
                    pass
                else:
                    raise DrainTimeoutError(
                        f"会话收尾超过独立宽限 {drain_grace_seconds:g} 秒"
                    ) from None


async def _probe_mcp_status(
    client: Any, options: Any, on_mcp_status: Callable[[Any], None]
) -> None:
    """建连后查一次 MCP server 连接状态，原样交给调用方（Issue #349，Gate G-2）。

    **只查一次，不轮询**：G-2 的独立探针（`__aenter__()` 一返回即为终态、3 秒后
    复查不变；SDK `initialize` 握手本身已经等待 MCP 首连结果）证实固定版 SDK+CLI
    下建连返回时状态已经是终态。**已知边界**（G-2 登记、未覆盖）：探针端点都是
    "立即失败"型，真实链路里"慢连接"时 `__aenter__()` 返回时可能仍是
    ``pending``——这种情况下这次查询会如实报告 ``pending``，本层与上层都不会把它
    误判成故障（判定只认 ``failed``/``needs-auth``，见 ``apps/worker/turn.py``），
    但也观察不到它之后是否变成 ``failed``。真的需要覆盖这条时序，需要另外加一次
    收尾前的复查——本次实现按 G-2 结论范围不做。

    ``get_mcp_status()`` 自身的异常不得让整个回合失败（观测缺口修复不能反过来
    制造新的故障面）：这里包住它，经**已经接在 ``options.stderr`` 上的既有 stderr
    通道**留一条痕迹再继续，不新开一条落点——``options.stderr`` 就是
    ``build_agent_options(..., stderr_sink=...)`` 传入的那个回调（调用方通常是
    ``WorkerTurnExecutor._sdk_stderr_sink``，最终落 ``worker.sdk.stderr`` 结构化
    审计行），本来就是"这次会话的诊断文本出口"，不是只服务子进程 stderr 一种来源。
    """

    try:
        status = await client.get_mcp_status()
    except Exception as error:  # noqa: BLE001 - 决不能让观测探针拖垮整个回合
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
