"""把工具边界与审计记账接到 hook 事件上的纯逻辑层。

这里刻意不 import Claude Agent SDK：hook 回调的输入输出都是普通字典，因此
"越界调用是否被拒、拒绝有没有被记账"这类判定可以在没有 SDK、没有模型额度的
CI 里被完整覆盖。SDK 绑定见 ``lingxi.adapters.claude_agent_hooks``。
"""

from __future__ import annotations

from typing import Any, Mapping

from .audit import TurnAudit
from .tool_policy import ToolPolicy

# 需要注册的 hook 事件。``PostToolUseFailure`` 是工具抛错的唯一来源；
# ``PermissionDenied`` / ``PermissionRequest`` 实测从不触发（Issue #23），
# 保留注册只为持续验证这一结论，不作为审计依据。
HOOK_EVENTS: tuple[str, ...] = (
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "Stop",
)

OBSERVATION_ONLY_EVENTS: tuple[str, ...] = (
    "PermissionRequest",
    "PermissionDenied",
)


class ToolGateway:
    """执行层的唯一工具判定入口。

    默认拒绝由 :class:`~lingxi.core.execution.tool_policy.ToolPolicy` 保证；本类
    负责在返回拒绝**之前**先记账，这样即使模型此后不再提及该次调用，审计里也有
    这次拒绝及其理由。
    """

    def __init__(self, *, policy: ToolPolicy, audit: TurnAudit) -> None:
        self._policy = policy
        self._audit = audit

    @property
    def audit(self) -> TurnAudit:
        return self._audit

    async def on_hook_event(
        self,
        hook_input: Mapping[str, Any],
        tool_use_id: str | None = None,
        _context: Any = None,
    ) -> dict[str, Any]:
        """Agent SDK 的 hook 回调签名。返回空字典表示不干预。"""

        event = hook_input.get("hook_event_name")
        tool_name = hook_input.get("tool_name")
        tool_input = hook_input.get("tool_input")
        call_id = tool_use_id or hook_input.get("tool_use_id")

        if event == "PreToolUse":
            return self._on_pre_tool_use(tool_name, tool_input, call_id)
        if event == "PostToolUse" and isinstance(tool_name, str):
            self._audit.record_executed(tool_name=tool_name, tool_use_id=call_id)
        elif event == "PostToolUseFailure" and isinstance(tool_name, str):
            self._audit.record_failure(
                tool_name=tool_name,
                tool_input=tool_input,
                tool_use_id=call_id,
                error=hook_input.get("error"),
            )
        return {}

    def _on_pre_tool_use(self, tool_name: Any, tool_input: Any, call_id: str | None) -> dict[str, Any]:
        verdict = self._policy.decide(tool_name, tool_input if isinstance(tool_input, Mapping) else None)
        self._audit.record_decision(
            tool_name=verdict.tool_name,
            tool_input=tool_input,
            tool_use_id=call_id,
            verdict=verdict,
        )
        if not verdict.denied:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": verdict.model_reason,
            }
        }
