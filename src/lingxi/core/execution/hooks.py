"""把工具边界与审计记账接到 hook 事件上的纯逻辑层。

这里刻意不 import Claude Agent SDK：hook 回调的输入输出都是普通字典，因此
"越界调用是否被拒、拒绝有没有被记账"这类判定可以在没有 SDK、没有模型额度的
CI 里被完整覆盖。SDK 绑定见 ``lingxi.adapters.claude_agent_hooks``。
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

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

# 认得的事件名全集。收到不在这里、却带着 tool_name 的事件时留痕，不当作无事发生。
#
# **这条留痕的覆盖面比字面看起来窄。** 适配器只按 HOOK_EVENTS 与
# OBSERVATION_ONLY_EVENTS 里的名字注册；SDK 真把 `PreToolUse` 改名的话，我们注册
# 的那个名字根本不会再被调用，本分支永远进不去。它只在「SDK 用一个我们认不出的
# 事件名回调我们已注册的 matcher」时生效。**不得据此声称"事件改名会自动被发现"**
# ——事件名是否仍然有效只有真实 SDK 的冒烟检查与 L4a 能回答，见 V-执行-11。
KNOWN_EVENTS: frozenset[str] = frozenset(HOOK_EVENTS) | frozenset(OBSERVATION_ONLY_EVENTS)


class ToolGateway:
    """执行层的唯一工具判定入口。

    默认拒绝由 :class:`~lingxi.core.execution.tool_policy.ToolPolicy` 保证；本类
    负责在返回拒绝**之前**先记账，这样即使模型此后不再提及该次调用，审计里也有
    这次拒绝及其理由。
    """

    def __init__(
        self,
        *,
        policy: ToolPolicy,
        audit: TurnAudit,
        mark_external_side_effect: Callable[[], None] | None = None,
        raw_pre_tool_use: Callable[[str | None, Any], None] | None = None,
    ) -> None:
        self._policy = policy
        self._audit = audit
        self._mark_external_side_effect = mark_external_side_effect
        # 内测轮内容级采集的唯一原始入参出口（Issue #251/#304 批次 3，可选、默认
        # None）：`self._audit` 记的是**经字段白名单裁剪过**的入参（见
        # `AuditRedactor.redact`），采集要的是裁剪之前的原始值——因此不能从
        # `self._audit` 反推，必须在这里另开一个独立分支，在传给审计之前把原始
        # `tool_input` 递给调用方注入的收集器。默认 `None` 时这个分支整体不存在，
        # 不产生任何额外调用、不额外持有一份原始入参——这是"默认关闭"在这一层的
        # 具体形状：不是多一个 if 分支跳过写库，而是这份收集器压根没被构造出来
        # （构造方见 apps/worker/turn.py）。失败必须被这里兜住、不得影响工具判定
        # 本身（同 `_mark_side_effect` 的既有姿态）。
        self._raw_pre_tool_use = raw_pre_tool_use
        # 语义化进度的工具调用开始通知（Issue #321 方向 C）：默认 ``None``，由
        # ``set_tool_call_listener`` 按回合装配（见 ``apps/worker/turn.py`` 的
        # ``run_turn``）。不做成构造参数——``ToolGateway`` 在
        # ``WorkerTurnExecutor.__init__`` 里只建一次，而这个监听器要跟着每一次
        # ``run_turn()`` 调用传入的回调走（回调闭包了那一次任务的进度状态），
        # 因此需要一个可以在构造之后重新挂载的入口，与固定在构造期的
        # ``raw_pre_tool_use``（内容级采集，语义上跟着整个执行器实例、不是单次
        # 回合）用途不同。
        self._on_tool_call: Callable[[str], None] | None = None

    @property
    def audit(self) -> TurnAudit:
        return self._audit

    def set_tool_call_listener(self, callback: Callable[[str], None] | None) -> None:
        """登记（或清除）本回合的工具调用开始通知（Issue #321 方向 C）。

        回调收到的是 :class:`~lingxi.core.execution.tool_policy.PolicyVerdict` 的
        ``tool_name``——判定之后的规范化值（合法工具名原样、畸形输入已经被
        ``ToolPolicy._display_name`` 投影成 ``"<空>"``/``"<类型名>"`` 这类占位符，
        见 ``tool_policy.py``），不是 hook 事件里未经校验的原始 ``tool_name``。
        既被允许也被拒绝的调用都会通知——这只是"用户可见的语义化进度"要看的
        「模型发起过一次调用」信号，不代表调用真的执行了；调用是否真的执行、
        是否成功由 ``PostToolUse``/``PostToolUseFailure`` 记账，两者互不影响、
        互不覆盖。回调异常必须被 ``_on_pre_tool_use`` 兜住，不能影响工具判定
        本身（与 ``raw_pre_tool_use`` 同一姿态）。
        """

        self._on_tool_call = callback

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
            if self._is_side_effecting_tool(tool_name):
                self._mark_side_effect()
            self._audit.record_executed(tool_name=tool_name, tool_use_id=call_id)
        elif event == "PostToolUseFailure" and isinstance(tool_name, str):
            if self._is_side_effecting_tool(tool_name):
                self._mark_side_effect()
            self._audit.record_failure(
                tool_name=tool_name,
                tool_input=tool_input,
                tool_use_id=call_id,
                error=hook_input.get("error"),
            )
        elif event == "Stop":
            self._audit.record_terminal_result()
        elif event not in KNOWN_EVENTS and isinstance(tool_name, str):
            # 认不出的事件名 + 带工具名 = 判定分支已经失效。本层挡不住（没有别的
            # 手段），但绝不能连痕迹都不留。
            self._audit.record_executed(tool_name=tool_name, tool_use_id=call_id)
        return {}

    def _mark_side_effect(self) -> None:
        if self._mark_external_side_effect is None:
            return
        try:
            self._mark_external_side_effect()
        except Exception:  # noqa: BLE001 - 失败也必须保守地继续记审计
            self._audit.record_audit_fault(tool_name="external_side_effect", tool_use_id=None)

    @staticmethod
    def _is_side_effecting_tool(tool_name: str) -> bool:
        # 本 Story 的唯一放行能力是只读 MCP；其它真正执行到的工具都按可能有副作用
        # 处理。未经过 PreToolUse 的旁路仍由报告的 ungated_calls 拦截收口。
        return not tool_name.startswith("mcp__")

    def _on_pre_tool_use(self, tool_name: Any, tool_input: Any, call_id: str | None) -> dict[str, Any]:
        verdict = self._policy.decide(tool_name, tool_input if isinstance(tool_input, Mapping) else None)
        if self._raw_pre_tool_use is not None:
            try:
                self._raw_pre_tool_use(call_id, tool_input)
            except Exception:  # noqa: BLE001 - 采集失败不得影响工具判定本身
                pass
        if self._on_tool_call is not None:
            try:
                self._on_tool_call(verdict.tool_name)
            except Exception:  # noqa: BLE001 - 进度通知失败不得影响工具判定本身
                pass
        # 先把响应算出来，再记账：记账处理的是模型可控的入参，一旦它抛异常，
        # 异常会沿 hook 回调向上抛，把这次拒绝一起带走。审计可以失败，拒绝不能。
        response: dict[str, Any] = {}
        if verdict.denied:
            response = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": verdict.model_reason,
                }
            }
        try:
            self._audit.record_decision(
                tool_name=verdict.tool_name,
                tool_input=tool_input,
                tool_use_id=call_id,
                verdict=verdict,
            )
        except Exception:  # noqa: BLE001 - 见上：审计失败不得降级为放行
            self._audit.record_audit_fault(tool_name=verdict.tool_name, tool_use_id=call_id)
        return response
