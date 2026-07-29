#!/usr/bin/env python3
"""用**真实** Claude Agent SDK 构造一次 hooks 配置。

`tests/test_claude_agent_hooks_adapter.py` 用桩模块替换整个 `claude_agent_sdk`，
因此它锁得住我们这一侧的形状，却抓不到两类问题：锁定的 SDK 版本装不上，或者
`HookMatcher` 的构造签名变了。这一步补上那一段。

**不调用模型、不使用任何业务凭据、不发起网络请求**：只是构造对象并检查形状。
真实事件是否触发、事件名是否仍然有效，只有 L4a 受控验证能回答——本检查不声称
覆盖那一层。
"""

from __future__ import annotations

import sys


def main() -> int:
    try:
        import claude_agent_sdk
        from claude_agent_sdk import HookMatcher
    except ImportError as error:
        print(f"真实 SDK 冒烟：导入 claude_agent_sdk 失败（{error}）", file=sys.stderr)
        return 1

    from lingxi.adapters.claude_agent_hooks import build_hook_matchers
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

    version = getattr(claude_agent_sdk, "__version__", "未知")
    print(f"真实 SDK 冒烟：claude-agent-sdk {version} 可构造 {len(matchers)} 个事件的 hooks 配置")
    print("（本检查不覆盖「事件是否真的触发」，那一层只有 L4a 能答）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
