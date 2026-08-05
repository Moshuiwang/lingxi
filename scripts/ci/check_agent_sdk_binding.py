#!/usr/bin/env python3
"""用**真实** Claude Agent SDK 构造一次 hooks 配置和一次 worker 会话选项。

`tests/test_claude_agent_hooks_adapter.py`、`tests/test_claude_agent_session_adapter.py`
和 `tests/test_worker_entry.py` 用桩模块替换整个 `claude_agent_sdk`，因此它们锁得住
我们这一侧的形状，却抓不到三类问题：锁定的 SDK 版本装不上、`HookMatcher` 的构造签名
变了、`ClaudeAgentOptions` 不再接受我们传的字段或消息类型改名。这一步补上那一段。

**不调用模型、不使用任何业务凭据、不发起网络请求**：只是构造对象并检查形状。
真实事件是否触发、事件名是否仍然有效，只有 L4a 受控验证能回答——本检查不声称
覆盖那一层。
"""

from __future__ import annotations

import sys

# 只用于构造对象的固定假配置：不是真实工具名，也不连接任何 MCP 服务。
FAKE_ENV = {
    "LINGXI_WORKER_QUESTION": "CI 构造冒烟，不会发给模型",
    "LINGXI_WORKER_READONLY_TOOL": "mcp__ci-smoke__list_metrics",
    "LINGXI_WORKER_TRACE_ID": "01J00000000000000000000CI",
}


def main() -> int:
    try:
        import claude_agent_sdk
        from claude_agent_sdk import HookMatcher
    except ImportError as error:
        print(f"真实 SDK 冒烟：导入 claude_agent_sdk 失败（{error}）", file=sys.stderr)
        return 1

    from lingxi.adapters.claude_agent_hooks import build_hook_matchers
    from lingxi.adapters.claude_agent_session import MESSAGE_TYPE_NAMES, load_message_types
    from lingxi.core.execution.audit import TurnAudit
    from lingxi.core.execution.hooks import HOOK_EVENTS, OBSERVATION_ONLY_EVENTS, ToolGateway
    from lingxi.core.execution.tool_policy import ToolPolicy

    gateway = ToolGateway(
        policy=ToolPolicy(allowed_tools=("mcp__bi-metric__list_metrics",)),
        audit=TurnAudit(),
    )

    try:
        matchers = build_hook_matchers(gateway)
    except TypeError as error:
        print(f"真实 SDK 冒烟：HookMatcher 构造签名不兼容（{error}）", file=sys.stderr)
        return 1

    expected = set(HOOK_EVENTS) | set(OBSERVATION_ONLY_EVENTS)
    if set(matchers) != expected:
        print(f"真实 SDK 冒烟：注册的事件集合是 {sorted(matchers)}，预期 {sorted(expected)}", file=sys.stderr)
        return 1

    for event, entries in matchers.items():
        if len(entries) != 1 or not isinstance(entries[0], HookMatcher):
            print(f"真实 SDK 冒烟：事件 {event} 的 matcher 形状不对", file=sys.stderr)
            return 1

    try:
        load_message_types()
    except AttributeError as error:
        print(f"真实 SDK 冒烟：依赖的消息类型不存在（{error}）", file=sys.stderr)
        return 1

    failure = check_worker_entry(expected)
    if failure is not None:
        print(f"真实 SDK 冒烟：{failure}", file=sys.stderr)
        return 1

    version = getattr(claude_agent_sdk, "__version__", "未知")
    print(f"真实 SDK 冒烟：claude-agent-sdk {version} 可构造 {len(matchers)} 个事件的 hooks 配置")
    print(f"真实 SDK 冒烟：worker 入口可构造 ClaudeAgentOptions，消息类型齐备（{', '.join(MESSAGE_TYPE_NAMES)}）")
    print("（本检查不覆盖「事件是否真的触发」，那一层只有 L4a 能答）")
    return 0


def check_worker_entry(expected_events: set[str]) -> str | None:
    """worker 入口必须能用**真实** ``ClaudeAgentOptions`` 建出装好屏障的会话选项。

    桩测试证明了"我们把 hooks 传进去了"，证明不了真实 dataclass 接受这些字段。
    `V-执行-18` 的真实入口那一半就落在这里；不建会话、不调模型。
    """

    from lingxi.apps.worker.config import load_config
    from lingxi.apps.worker.turn import WorkerTurnExecutor

    config = load_config(FAKE_ENV)
    executor = WorkerTurnExecutor(config)
    try:
        options = executor.build_session_options()
    except TypeError as error:
        return f"ClaudeAgentOptions 不接受 worker 传入的字段（{error}）"

    hooks = getattr(options, "hooks", None)
    if not hooks or set(hooks) != expected_events:
        return f"worker 会话选项里的 hooks 是 {sorted(hooks or [])}，预期 {sorted(expected_events)}"
    if hooks["PreToolUse"][0].hooks != [executor.gateway.on_hook_event]:
        return "worker 会话选项的 PreToolUse 上挂的不是本次构造的 ToolGateway"
    if list(getattr(options, "allowed_tools", [])) != [config.read_only_tool]:
        return f"worker 只应放行一个只读工具，实际是 {getattr(options, 'allowed_tools', None)}"
    if list(getattr(options, "disallowed_tools", [])) != []:
        return "disallowed_tools 必须留空，否则规则层会抢在我们的 PreToolUse 之前拦截"
    return None


if __name__ == "__main__":
    raise SystemExit(main())
