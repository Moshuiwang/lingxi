"""终态收口的低敏结构化审计事件。

**严禁**记录正文内容、用户标识、提示词、模型输出片段或工具入参正文；只记分类性的失败
码与签名、终态种类、出口安全的布尔与原因码、几个计数，以及提示词版本摘要。

为什么必须由装配层注入出口而不是直接调标准库日志：本服务是纯组装对象，不知道自己会被
哪个进程入口装配，也不该假设日志已经配过 handler——真实队列 worker 刻意不调用日志初始化，
默认阈值会把 INFO 级记录悄悄吞掉，那就等于写了一条实际不存在的审计。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

from lingxi.apps.worker.report_extraction import _cap_log_token, sanitize_failure_signature
from lingxi.apps.worker.service_ports import TerminalOutcomeCallback
from lingxi.apps.worker.task_processing import TerminalDecision, TurnOutcome

logger = logging.getLogger("lingxi.apps.worker.service")


class TerminalOutcomeAudit:
    """终态收口的低敏结构化审计出口。

    这条事件是队列链路失败原因的唯一可回读来源：在它之前，失败码与安全命中规则完全查不到，
    白名单配错导致的工具拒绝只能靠用户反馈才会被发现。
    """

    def __init__(self, on_terminal_outcome: TerminalOutcomeCallback | None) -> None:
        """没有装配出口（``None``）时整体跳过，不假装写出实际不存在的日志。"""
        self._on_terminal_outcome = on_terminal_outcome

    def log(
        self,
        *,
        task_id: str,
        decision: TerminalDecision,
        outcome: TurnOutcome,
        system_prompt_digest: str | None,
    ) -> None:
        """记一条终态收口事件；出口本身失败只记日志，不带走任务收口。"""
        if self._on_terminal_outcome is None:
            return
        try:
            self._on_terminal_outcome(
                self._fields(
                    task_id=task_id,
                    decision=decision,
                    outcome=outcome,
                    system_prompt_digest=system_prompt_digest,
                )
            )
        except Exception as error:
            logger.error("终态收口审计事件回调失败，任务收口继续 error=%s", type(error).__name__)

    @classmethod
    def _fields(
        cls,
        *,
        task_id: str,
        decision: TerminalDecision,
        outcome: TurnOutcome,
        system_prompt_digest: str | None,
    ) -> dict[str, object]:
        """组装事件字段；自由文本一律先截到长度上界。

        截断是防漏面而不是省空间：真正会撑长的是模型臆造的畸形工具名或凭据形态的字符串，
        以及将来某次改动不小心塞进这些字段的自由文本。提示词摘要不过这道截断——它是固定
        形态的短标识，不可能携带自由文本。
        """
        blocked, withheld, reasons = cls._safety_flags(outcome.output_safety)
        failure_code = decision.failure_code
        capped_code, code_truncated = (
            _cap_log_token(str(failure_code)) if failure_code is not None else (None, False)
        )
        signature = outcome.failure_signature
        capped_signature, signature_truncated = (
            _cap_log_token(sanitize_failure_signature(signature))
            if signature is not None
            else (None, False)
        )
        capped_reasons, reasons_truncated = cls._cap_all(reasons)
        capped_tool_names, names_truncated = cls._cap_all(outcome.denied_tool_names)
        return {
            "task_id": task_id,
            "failure_code": capped_code,
            "failure_signature": capped_signature,
            "error_kind": decision.error_kind,
            "terminal_kind": decision.terminal_kind,
            "output_safety_blocked": blocked,
            "output_safety_withheld": withheld,
            "output_safety_reasons": tuple(capped_reasons),
            "denied_count": outcome.denied_count,
            "denied_tool_names": tuple(capped_tool_names),
            "tool_result_count": outcome.tool_result_count,
            "system_prompt_digest": system_prompt_digest,
            "truncated": (
                code_truncated or signature_truncated or reasons_truncated or names_truncated
            ),
        }

    @staticmethod
    def _safety_flags(
        output_safety: Mapping[str, Any] | None,
    ) -> tuple[bool, bool, tuple[str, ...]]:
        """从出口安全结果里取两个布尔与一组原因码。"""
        if not isinstance(output_safety, Mapping):
            return False, False, ()
        raw_reasons = output_safety.get("reasons")
        reasons = (
            tuple(str(reason) for reason in raw_reasons)
            if isinstance(raw_reasons, (list, tuple))
            else ()
        )
        return bool(output_safety.get("blocked")), bool(output_safety.get("withheld")), reasons

    @staticmethod
    def _cap_all(values: Iterable[str]) -> tuple[list[str], bool]:
        """把一组自由文本各自截到长度上界。

        Returns:
            ``(截断后的值, 是否发生过截断)``。
        """
        capped: list[str] = []
        truncated = False
        for value in values:
            capped_value, value_truncated = _cap_log_token(value)
            capped.append(capped_value)
            truncated = truncated or value_truncated
        return capped, truncated
