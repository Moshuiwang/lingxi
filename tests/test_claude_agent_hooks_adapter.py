"""SDK 绑定层的形状断言。

``lingxi.adapters.claude_agent_hooks`` 是唯一 import Claude Agent SDK 的地方，也是
把已验证的判定逻辑接到真实 SDK 上的唯一一处。SDK 是 ``pyproject.toml`` 里的可选
``worker`` extra，本文件**刻意不装它**：用桩模块顶替可以在没有 SDK 的环境里跑，锁住
三件事——注册了哪些事件、回调怎么被调用、回调签名的第三个参数会不会被按关键字传入。

桩模块只能证明"我们这一侧的形状没变"，**不能**证明 SDK 那一侧接受这些事件名。
CI 另有一步装上真实 SDK 做冒烟（``scripts/ci/check_agent_sdk_binding.py``），它能证明
"装得上、``HookMatcher`` 签名兼容"，仍然证明不了事件真的会触发——后者只有真实链路
（L4a）能回答。
"""

from __future__ import annotations

import asyncio
import sys
import types
import unittest


class _StubHookMatcher:
    def __init__(self, matcher=None, hooks=None, timeout=None) -> None:
        self.matcher = matcher
        self.hooks = hooks
        self.timeout = timeout


class _StubSDK(unittest.TestCase):
    def setUp(self) -> None:
        module = types.ModuleType("claude_agent_sdk")
        module.HookMatcher = _StubHookMatcher  # type: ignore[attr-defined]
        self._saved = sys.modules.get("claude_agent_sdk")
        sys.modules["claude_agent_sdk"] = module
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._saved is None:
            sys.modules.pop("claude_agent_sdk", None)
        else:
            sys.modules["claude_agent_sdk"] = self._saved


class HookMatcherShapeTest(_StubSDK):
    def test_registers_every_decision_event_and_the_observation_events(self) -> None:
        from lingxi.adapters.claude_agent_hooks import build_hook_matchers
        from lingxi.core.execution.audit import TurnAudit
        from lingxi.core.execution.hooks import HOOK_EVENTS, OBSERVATION_ONLY_EVENTS, ToolGateway
        from lingxi.core.execution.tool_policy import ToolPolicy

        gateway = ToolGateway(policy=ToolPolicy(allowed_tools=("mcp__q",)), audit=TurnAudit())
        matchers = build_hook_matchers(gateway)

        self.assertEqual(set(matchers), set(HOOK_EVENTS) | set(OBSERVATION_ONLY_EVENTS))
        self.assertIn("PreToolUse", matchers, "拒绝式白名单的唯一强制点")
        self.assertIn("Stop", matchers, "回合收口计数的唯一来源")
        for event, entries in matchers.items():
            with self.subTest(event=event):
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0].hooks, [gateway.on_hook_event])
                self.assertIsNone(entries[0].matcher)
                self.assertIsNotNone(entries[0].timeout)

    def test_observation_events_can_be_left_out_without_touching_decision_events(self) -> None:
        from lingxi.adapters.claude_agent_hooks import build_hook_matchers
        from lingxi.core.execution.audit import TurnAudit
        from lingxi.core.execution.hooks import HOOK_EVENTS, ToolGateway
        from lingxi.core.execution.tool_policy import ToolPolicy

        gateway = ToolGateway(policy=ToolPolicy(allowed_tools=("mcp__q",)), audit=TurnAudit())
        matchers = build_hook_matchers(gateway, observe_permission_events=False)

        self.assertEqual(set(matchers), set(HOOK_EVENTS))


class CallbackSignatureTest(_StubSDK):
    """SDK 可能按位置或按关键字传第三个参数；两种都不能让门禁塌掉。"""

    def _gateway(self):
        from lingxi.core.execution.audit import TurnAudit
        from lingxi.core.execution.hooks import ToolGateway
        from lingxi.core.execution.tool_policy import ToolPolicy

        return ToolGateway(policy=ToolPolicy(allowed_tools=("mcp__q",)), audit=TurnAudit())

    def test_callback_denies_with_positional_arguments(self) -> None:
        gateway = self._gateway()
        result = asyncio.run(
            gateway.on_hook_event({"hook_event_name": "PreToolUse", "tool_name": "Write", "tool_input": {}}, "toolu_1", None)
        )

        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_callback_denies_with_keyword_tool_use_id(self) -> None:
        gateway = self._gateway()
        result = asyncio.run(
            gateway.on_hook_event(
                {"hook_event_name": "PreToolUse", "tool_name": "Write", "tool_input": {}},
                tool_use_id="toolu_1",
            )
        )

        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
