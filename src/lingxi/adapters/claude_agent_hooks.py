"""把 :class:`ToolGateway` 绑定到 Claude Agent SDK 的 hook 配置上。

本模块是唯一 import SDK 的地方，且只做形状转换，不含判定逻辑——判定在
``lingxi.core.execution`` 里，可在无 SDK 的 CI 中完整验证。
"""

from __future__ import annotations

from typing import Any

from lingxi.core.execution.hooks import HOOK_EVENTS, OBSERVATION_ONLY_EVENTS, ToolGateway

_HOOK_TIMEOUT_SECONDS = 30


def build_hook_matchers(gateway: ToolGateway, *, observe_permission_events: bool = True) -> dict[str, list[Any]]:
    """构造 ``ClaudeAgentOptions.hooks`` 需要的映射。

    ``observe_permission_events`` 默认开启：``PermissionDenied`` /
    ``PermissionRequest`` 实测从不触发，继续注册是为了在 SDK 或 CLI 升级后能第一
    时间发现这一结论失效，不构成审计依据。
    """

    from claude_agent_sdk import HookMatcher

    matcher = HookMatcher(matcher=None, hooks=[gateway.on_hook_event], timeout=_HOOK_TIMEOUT_SECONDS)
    events = list(HOOK_EVENTS)
    if observe_permission_events:
        events.extend(OBSERVATION_ONLY_EVENTS)
    return {event: [matcher] for event in events}
