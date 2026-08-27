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
        #
        # `mcp__delivery__deliver_document`（Issue #341 S-ES-2）**不是**这条经验
        # 规则的例外（opus 审查 P2-1，撤销此前把它显式列为例外的修复）。这个工具
        # 调用一次确实会在进程内登记/覆盖本轮的文档交付请求，但它**没有任何跨进程
        # 副作用**：不发外部请求、不落任何持久化数据，只是回合级内存状态。真正落库
        # 的一步在 `adapters/postgres_conversation/_queue_outbox.py::
        # write_terminal_event`——那一步由 `task_document_delivery_request.task_id`
        # 的 UNIQUE 约束（迁移 0074）与"终态写入同一个事务"两条防线保证幂等：同一个
        # `task_id` 重放多次，至多插入一行。把这个工具错误地标成"有副作用"，代价是
        # 崩溃恢复路径（`adapters/postgres_conversation/_queue_lifecycle.py::
        # reclaim_stale`）会因为 `side_effect_state='possible'` 判定 `safe_retry=
        # False`，把本可以安全重排回 `queued` 的任务转判 `side_effect_uncertain`
        # 失败终态——一个只是想要一份文档的用户，因为这个工具调用被错误归类，反而
        # 从"重试一次就能拿到答案"变成"当场收到失败通知"，而它本该是全部 mcp__
        # 工具里最经得起重放的那一个。
        return not tool_name.startswith("mcp__")

    def _on_pre_tool_use(self, tool_name: Any, tool_input: Any, call_id: str | None) -> dict[str, Any]:
        verdict = self._policy.decide(tool_name, tool_input if isinstance(tool_input, Mapping) else None)
        if self._raw_pre_tool_use is not None:
            try:
                self._raw_pre_tool_use(call_id, tool_input)
            except Exception:  # noqa: BLE001 - 采集失败不得影响工具判定本身
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
