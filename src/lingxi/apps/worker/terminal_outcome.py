"""任务终态的失败文案映射与低敏审计事件。

审计事件**严禁**记录正文内容、用户标识、提示词、模型输出片段或工具入参正文；只记
分类性的失败码、终态种类、安全判定的布尔与原因码，以及几个计数。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from lingxi.apps.worker.report_extraction import _cap_log_token, sanitize_failure_signature
from lingxi.apps.worker.service_ports import TerminalOutcomeCallback
from lingxi.apps.worker.turn import MCP_BAD_GATEWAY_FAILURE_CODE
from lingxi.config.content import ContentCatalog, RenderedContent

logger = logging.getLogger("lingxi.apps.worker.service")

_MODEL_PROTOCOL_BREAKDOWN_FAILURE_CODE = "model_protocol_breakdown"


class TerminalOutcomeAudit:
    """终态收口的低敏结构化审计出口。"""

    def __init__(self, on_terminal_outcome: TerminalOutcomeCallback | None) -> None:
        self._on_terminal_outcome = on_terminal_outcome

    def log(
        self,
        *,
        task_id: str,
        failure_code: object,
        failure_signature: str | None,
        error_kind: str | None,
        terminal_kind: str,
        output_safety: Mapping[str, Any] | None,
        denied_count: int = 0,
        denied_tool_names: tuple[str, ...] = (),
        tool_result_count: int = 0,
        system_prompt_digest: str | None = None,
    ) -> None:
        """queue 收口低敏结构化审计事件（Issue #90 评论 5306860255）：queue 链路
        此前失败码与安全命中规则完全不可回读，r13 只能靠猜直接原因。这里只记
        分类性的失败码、落库 ``error_kind``、``terminal_kind``、安全判定的
        布尔/原因码、本回合被 ``ToolPolicy`` 拒绝的调用计数与工具名，以及这一轮
        真实的工具调用次数——**严禁**记录正文内容、用户 open_id、prompt、模型
        输出片段或工具入参正文。

        ``denied_count``/``denied_tool_names`` 是 Issue #291 独立审查补的一项：
        ``tool_policy.py`` 的拒绝文案对用户承诺"这是系统侧的临时限制、问题
        已经被记录"，但此前 queue 链路从未把 ``report["audit"]["denied_count"]``
        （早就算出来了，见 ``report.py``）写进任何运维可见的地方——白名单配错
        导致的拒绝只能像 #291 真实事故那样，靠用户反馈才会被发现。
        ``failure_signature``/``failure_code``（#495/#496）与 ``tool_result_count``（#291）仅
        提供低敏固定类别/分类线索，不记录动态类型、异常正文或工具入参。
        独立复核 P1：事件经装配层 ``on_terminal_outcome`` 接入结构化 stderr；没有
        装配方（``None``）时跳过，不假装写出实际不存在的日志。
        """
        if self._on_terminal_outcome is None:
            return

        blocked = bool(isinstance(output_safety, Mapping) and output_safety.get("blocked"))
        withheld = bool(isinstance(output_safety, Mapping) and output_safety.get("withheld"))
        reasons: tuple[str, ...] = ()
        if isinstance(output_safety, Mapping):
            raw_reasons = output_safety.get("reasons")
            if isinstance(raw_reasons, (list, tuple)):
                reasons = tuple(str(reason) for reason in raw_reasons)

        # P3-2：失败码与每个原因码入日志前截到长度上界，避免未来某次改动不小心
        # 把自由文本塞进这两个字段时，审计日志变成新的正文泄漏面。被拒工具名
        # 同一惯例：内置工具名/已知 MCP 工具名很短，真正会撑长的是模型臆造的
        # 畸形名字或凭据形态的字符串，同样不能不设上界。
        truncated = False
        capped_failure_code: str | None = None
        if failure_code is not None:
            capped_failure_code, code_truncated = _cap_log_token(str(failure_code))
            truncated = truncated or code_truncated
        capped_failure_signature: str | None = None
        if failure_signature is not None:
            capped_failure_signature, signature_truncated = _cap_log_token(
                sanitize_failure_signature(failure_signature)
            )
            truncated = truncated or signature_truncated
        capped_reasons: list[str] = []
        for reason in reasons:
            capped_reason, reason_truncated = _cap_log_token(reason)
            capped_reasons.append(capped_reason)
            truncated = truncated or reason_truncated
        capped_denied_tool_names: list[str] = []
        for name in denied_tool_names:
            capped_name, name_truncated = _cap_log_token(name)
            capped_denied_tool_names.append(capped_name)
            truncated = truncated or name_truncated

        fields = {
            "task_id": task_id,
            "failure_code": capped_failure_code,
            "failure_signature": capped_failure_signature,
            "error_kind": error_kind,
            "terminal_kind": terminal_kind,
            "output_safety_blocked": blocked,
            "output_safety_withheld": withheld,
            "output_safety_reasons": tuple(capped_reasons),
            "denied_count": denied_count,
            "denied_tool_names": tuple(capped_denied_tool_names),
            "tool_result_count": tool_result_count,
            # 「这一轮**选定**的默认提示词版本」的唯一追溯依据（sha256 前 12 位；
            # 未配置提示词文件或本轮降级时为 None；口径见 _process_task 的初始化
            # 注释——记录"选定并交给执行器装配的版本"，不声称模型已收到）。摘要
            # 是固定形态短标识，不过 _cap_log_token——它不可能携带自由文本。
            "system_prompt_digest": system_prompt_digest,
            "truncated": truncated,
        }
        try:
            self._on_terminal_outcome(fields)
        except Exception as error:  # noqa: BLE001 - 观测失败不能带走任务职责，参照 _append_event
            logger.error("终态收口审计事件回调失败，任务收口继续 error=%s", type(error).__name__)


def failure_content(catalog: ContentCatalog, code: object) -> tuple[str, RenderedContent]:
    if code == "context_too_long":
        return "context_too_long", catalog.text("worker.context_too_long")
    if code == "turn_timeout":
        return "running_timeout", catalog.text("worker.running_timeout")
    if code == "side_effect_uncertain":
        return "side_effect_uncertain", catalog.text("worker.side_effect_uncertain")
    if code == "max_turns_exceeded":
        # Issue #90 评论 5306860255：turn 模式（apps/worker/turn.py 的
        # `_sdk_termination_failure`）早已把撞满 Agent 轮数上限分类为
        # `max_turns_exceeded`，但 queue 收口此前落进这里的默认分支，
        # 被压平成通用 `session_failed` 文案——用户看到的是「请稍后重试」，
        # 而重试对"问题本身步骤太多"这种失败原因没有意义。这里给它一个
        # 独立、可查询的 error_kind 和产品负责人定稿的专属文案。
        return "max_turns_exceeded", catalog.text("worker.max_turns")
    if code == _MODEL_PROTOCOL_BREAKDOWN_FAILURE_CODE:
        # Issue #291 L6 取证结论：不新增专属用户文案——「模型把工具调用协议
        # 写成了正文」是运维需要知道的事实，不是用户需要（或应该）知道的
        # 过程细节；把它说给用户听本身就是又一次过程泄漏。复用通用失败文案，
        # 专属性只保留在 `failure_code`（审计/日志可查）。
        return _MODEL_PROTOCOL_BREAKDOWN_FAILURE_CODE, catalog.text("worker.failed")
    if code == "result_too_large":
        # 2026-08-23 真实故障：未加窄过滤的指标查询回执超过 SDK 读流缓冲上限
        # （分类在 apps/worker/turn.py）。与 max_turns_exceeded 同一姿态——
        # 「请稍后重试」对确定性失败是误导，专属文案给出可行动的建议。
        return "result_too_large", catalog.text("worker.result_too_large")
    if code == MCP_BAD_GATEWAY_FAILURE_CODE:
        return MCP_BAD_GATEWAY_FAILURE_CODE, catalog.text("worker.mcp_bad_gateway")
    return "session_failed", catalog.text("worker.failed")
