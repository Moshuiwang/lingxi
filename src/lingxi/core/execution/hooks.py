"""把工具边界与审计记账接到 hook 事件上的纯逻辑层。

这里刻意不 import Claude Agent SDK：hook 回调的输入输出都是普通字典，因此
"越界调用是否被拒、拒绝有没有被记账"这类判定可以在没有 SDK、没有模型额度的
CI 里被完整覆盖。SDK 绑定见 ``lingxi.adapters.claude_agent_hooks``。
"""

from __future__ import annotations

import re
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

# CLI 对超大工具输出的通用截断形态（Issue #323，2026-08-26 内测实证
# task `tsk_01M0YA8C1P1PFTZVWF3PY92P4Q`）：把结果存成本地临时文件，提示模型
# "exceeds maximum allowed tokens...Output has been saved to .../tmp/...
# Use offset and limit parameters to read specific portions of the file"。
# worker 白名单里没有 Read/Bash，模型照着这条提示走只会连续撞两种必然被拒的
# 工具——实测同一问句因此多花约四成时长（19 次调用/4 次被拒/488s）。
#
# 正则只锁「exceeds maximum allowed tokens」与「offset and limit parameters」
# 这两段——它们是该提示模板里最不像随版本措辞漂移的部分（中间的字节数、文件路径
# 每次都不同，两头的固定短语是模板骨架）。用 DOTALL + 非贪婪跨段匹配，能扛住
# 中间内容变化；两段都要求命中，是为了不误伤"截断"以外、只是恰好提到其中一个
# 短语的正常业务结果（例如指标名称、说明文字里偶然出现"tokens"）。
# 大小写不敏感只是防御性的，未在真实样本里见过大小写变体。
#
# **这条边界与 `_MESSAGE_BUFFER_OVERFLOW_PATTERN`（见
# ``lingxi.adapters.claude_agent_session``）不是同一个问题**：那个匹配的是
# SDK 读流缓冲上限压平成的裸异常（会话级、整条消息读不出来）；这个匹配的是
# 单次 MCP 工具调用**正常返回**、但被 CLI 自己截断改写过的回执内容
# （PostToolUse 能正常收到，只是文本在骗模型去读文件）。
_MCP_OVERSIZE_RESULT_PATTERN = re.compile(
    r"exceeds maximum allowed tokens.*?offset and limit parameters",
    re.IGNORECASE | re.DOTALL,
)

# 替换后模型看到的完整文本。三条硬要求（对应 #323 的可观测完成标准）：
# 不含 /tmp 路径、不含"读文件""offset/limit"这类分页引导、明确给出可执行的
# 下一步（缩小范围重新查询）。措辞与运行时提示词 v5 现行口径核对一致——
# 提示词原文「不要尝试读取那个文件（会被拒绝），直接改用更粗的聚合粒度、更短的
# 时间段或更少的维度重新查询」（ssh biai-stage 只读核对，2026-08-27）；
# 这里是结构性兜底，提示词未来裁剪掉这条时改写仍然生效，因此各自独立措辞，
# 不做字符串复用。
MCP_OVERSIZE_RESULT_REWRITE = (
    "本次查询返回的数据量超过单次可处理的上限，原始结果不可用。"
    "请缩小查询范围后重新调用查询工具——例如缩短时间范围、减少指标或公司数量、"
    "或提高聚合粒度。不要尝试读取任何本地文件路径。"
)


def _is_oversize_tool_result(tool_response: Any) -> bool:
    """``tool_response`` 是否命中 CLI 截断提示的特征。

    只做粗略拉平：CLI 截断提示是纯文本，真实 MCP 正常回执要么是字符串，要么是
    ``[{"type": "text", "text": ...}]`` 这类内容块数组，要么是 ``{"text": ...}``。
    拿不出文本时返回空串——正则必然不命中，等价于"不识别"，不是误判为截断。
    这里不追求 :mod:`audit` 模块 ``_coerce`` 那种完整归类，两者目的不同：那边要把
    回执分类成功/失败，这里只要够用来判断"是不是那条截断提示"。
    """

    if isinstance(tool_response, str):
        text = tool_response
    elif isinstance(tool_response, Mapping):
        value = tool_response.get("text")
        text = value if isinstance(value, str) else ""
    elif isinstance(tool_response, (list, tuple)):
        parts = [
            block.get("text")
            for block in tool_response
            if isinstance(block, Mapping) and isinstance(block.get("text"), str)
        ]
        text = "\n".join(parts)
    else:
        text = ""
    return bool(_MCP_OVERSIZE_RESULT_PATTERN.search(text))


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
            # 范围刻意只限只读问数 MCP：改写权限本身就是"能替换模型看到的工具
            # 结果"，只在本 Story 唯一放行的只读面上开这个口子。非 MCP 工具即使
            # 输出恰好命中同一段截断特征，也不改写——`updatedMCPToolOutput` 本来
            # 就只对 MCP 生效（SDK 类型声明），这里的前缀判断是双重把关，不依赖
            # SDK 那一侧的字段隔离单独兜底。
            if tool_name.startswith("mcp__") and _is_oversize_tool_result(
                hook_input.get("tool_response")
            ):
                self._audit.record_oversize_rewrite(tool_name=tool_name, tool_use_id=call_id)
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "updatedMCPToolOutput": MCP_OVERSIZE_RESULT_REWRITE,
                    }
                }
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
