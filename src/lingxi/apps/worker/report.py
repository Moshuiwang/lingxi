"""把一个回合的审计结论投影成可以离开进程的 JSON。

**投影不是格式化，它是最后一道出口。** 审计明细在记账时已经脱敏，但有两处模型
可控文本会在这里第一次离开进程：

1. **最终正文**——模型说什么都可能，出口再过一道自由文本脱敏；
2. **不在白名单里的工具名**——审计对象里**有意**原样保留（`V-审计-03` 残余缺口表：
   "模型试图调用什么"是拒绝式白名单最重要的一条审计事实），但那条知情接受的前提
   是"组件尚无调用方、不处理真实凭据"。有了调用方之后，出口必须收紧。

收紧的代价写在这里，不藏着：含数字的 16+ 字符未知工具名会被投影成长度，可读性
让位于凭据不外泄。**白名单内的工具名不过这道**——它们是我们自己配的，形态已知，
抹掉只会让正常审计难读。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from lingxi.core.execution.audit import ToolCallAudit, TurnAuditSummary, redact_free_text
from lingxi.core.execution.message_stream import TurnStreamRecorder

_GUARD_FAILURE_CODES = frozenset({"max_turns_exceeded", "turn_timeout", "cancelled"})


def build_report(
    *,
    trace_id: str,
    question: str,
    allowed_tools: Iterable[str],
    summary: TurnAuditSummary,
    stream: TurnStreamRecorder,
    final_text: str,
    duration_seconds: float,
    failure: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """构造 worker 的输出报告。

    ``closed`` 同时要求两件事：审计侧恰好一次终止结果（来自 ``Stop`` hook）与消息流
    侧恰好一条 ``ResultMessage``。两个来源分开计数、都要成立——把它们并成一个数字，
    "hook 没触发"就会被"消息流正常"掩盖，而那正是屏障失效的样子。
    """

    whitelist = frozenset(allowed_tools)
    redacted_text = redact_free_text(final_text)
    calls = [_project_call(call, whitelist) for call in summary.calls]
    failure_code = failure.get("code") if failure else None
    guard_triggered = failure_code in _GUARD_FAILURE_CODES
    termination_reason = failure_code or ("completed" if summary.terminal_ok else "turn_not_closed")
    termination_state = "guarded" if guard_triggered else ("completed" if failure is None else "failed")
    result_delivery = "confirmed" if summary.user_result.value == "obtained" else "not_confirmed"
    usage = stream.usage_summary
    resources = {
        "agent_turns": stream.agent_turns,
        "agent_turns_status": "known" if stream.agent_turns is not None else "unknown",
        "duration_seconds": duration_seconds,
        "tool_call_count": len(calls),
        "executed_tool_call_count": sum(1 for call in summary.calls if call.executed),
        "usage": usage,
    }
    return {
        "trace_id": trace_id,
        "question_bytes": len(question.encode("utf-8")),
        "turn": {
            # 收口＝审计侧恰好一次终止 + 流侧恰好一条 ResultMessage 且它不自报
            # 错误 + 没有失败 + 没有绕过屏障的调用。SDK 说这轮错了（如
            # error_max_turns 截断）却被报成正常收口，受控验证会把半截回合记成
            # 通过；ungated>0 是「hook 未触发」唯一的可观察形状（独立复查发现）。
            "closed": bool(
                summary.terminal_ok
                and stream.result_message_count == 1
                and stream.result_is_error is not True
                and failure is None
                and not summary.ungated_calls
            ),
            "gate_bypassed": bool(summary.ungated_calls),
            "final_text": redacted_text,
            "final_text_bytes": summary.final_text_bytes,
            "user_result": summary.user_result.value,
            "terminal_result_count": summary.terminal_result_count,
            "sdk_result_message_count": stream.result_message_count,
            "sdk_result_is_error": stream.result_is_error,
            "sdk_result_subtype": redact_free_text(stream.result_subtype) if stream.result_subtype else None,
            "sdk_terminal_reason": redact_free_text(stream.terminal_reason) if stream.terminal_reason else None,
            "termination_state": termination_state,
            "termination_reason": termination_reason,
            "guard_triggered": guard_triggered,
            "result_delivery": result_delivery,
            "duration_seconds": duration_seconds,
        },
        "audit": {
            "call_count": len(calls),
            "denied_count": len(summary.denied_calls),
            "failed_count": len(summary.failed_calls),
            "ungated_count": len(summary.ungated_calls),
            "tool_result_count": stream.tool_result_count,
            "termination_reason": termination_reason,
            "guard_triggered": guard_triggered,
            "usage": usage,
            "executed_tool_names": [
                _project_tool_name(name, whitelist) for name in summary.executed_tool_names
            ],
            "denied": [call for call in calls if call["allowed"] is False],
            "calls": calls,
        },
        "resources": resources,
        "failure": dict(failure) if failure else None,
    }


def config_error_report(*, trace_id: str, message: str) -> dict[str, Any]:
    """配置错误也走同一个输出契约：stdout 永远是一个 JSON 对象。"""

    return {
        "trace_id": trace_id,
        "turn": None,
        "audit": None,
        "failure": {"code": "config_error", "message": message},
    }


def _project_call(call: ToolCallAudit, whitelist: frozenset[str]) -> dict[str, Any]:
    return {
        "tool_use_id": call.tool_use_id,
        "tool_name": _project_tool_name(call.tool_name, whitelist),
        "tool_input": dict(call.tool_input),
        # 三态：True 放行、False 拒绝、None 本层未判定（`V-执行-09` / `V-执行-10`）。
        # 不许压成布尔——"没经过我们"和"我们放行了"是完全不同的两件事。
        "allowed": call.allowed,
        "deny_reason_code": call.deny_reason_code.value if call.deny_reason_code else None,
        "executed": call.executed,
        "error": call.error,
        "result_kind": call.result_kind.value if call.result_kind else None,
    }


def _project_tool_name(tool_name: str, whitelist: frozenset[str]) -> str:
    if tool_name in whitelist:
        return tool_name
    return redact_free_text(tool_name)
