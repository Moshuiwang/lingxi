"""把一个回合的审计结论投影成可以离开进程的 JSON。

投影不是格式化，是最后一道出口。审计明细在记账时已经脱敏，但两处模型可控
文本会在这里第一次离开进程：最终正文（出口再过一道自由文本脱敏）；不在
白名单里的工具名（有意原样保留——"模型试图调用什么"是拒绝式白名单最重要
的一条审计事实，但这个知情接受的前提是"尚无调用方、不处理真实凭据"，有
调用方之后出口必须收紧）。含数字的 16+ 字符未知工具名投影成长度；白名单
内的工具名不过这道，形态已知，抹掉只会让正常审计难读。

输出安全的诚实终态：``output_safety.withheld`` 为真时（正文因无法安全展示
被拒发），``turn.user_result`` 改写为独立可查询的 ``redacted_withheld``，不
再沿用审计侧的 ``obtained``——那个值只回答"工具是否调用成功"，回答不了
"用户是否拿到内容"，继续展示 ``obtained`` 就是合同明令禁止的"伪装成功"。
原始工具调用分类改名保留在 ``audit.tool_call_result``，不丢失只是不再冒充。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from lingxi.core.execution.audit import ToolCallAudit, TurnAuditSummary, redact_free_text
from lingxi.core.execution.document_delivery import DocumentRequest, SheetRequest
from lingxi.core.execution.input_safety import OutputConstraintResult, constrain_output
from lingxi.core.execution.message_stream import TurnStreamRecorder

_GUARD_FAILURE_CODES = frozenset(
    {"max_turns_exceeded", "turn_timeout", "cancelled", "drain_timeout"}
)
#: 独立、可查询的安全拒发终态；运营/审计据此过滤这个约定名字，不猜测某个
#: 字符串巧合。
REDACTED_WITHHELD_RESULT = "redacted_withheld"


@dataclass(frozen=True)
class _TerminationContext:
    """一次回合的失败/终止/用户结果判定结果，供各投影小节共用。"""

    effective_failure: dict[str, str] | None
    termination_reason: str
    termination_state: str
    guard_triggered: bool
    effective_user_result: str
    result_delivery: str


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
    external_texts: Iterable[str] = (),
    system_prompt: str | None = None,
    business_execution_budget_seconds: float | None = None,
    business_duration_seconds: float | None = None,
    drain_duration_seconds: float | None = None,
    document_request: DocumentRequest | None = None,
    sheet_request: SheetRequest | None = None,
) -> dict[str, Any]:
    """构造 worker 的输出报告：出口安全裁决 + 终止判定，交给 :func:`_assemble_report` 打包。

    出口安全裁决（``constrain_output``）与工具调用的白名单投影必须先算出来，
    下游的终止判定、各小节组装都依赖它们的结果，因此留在这一层，不下放。
    """
    allowed_tool_names = tuple(allowed_tools)
    whitelist = frozenset(allowed_tool_names)
    output_safety = constrain_output(
        final_text,
        forbidden_values=external_texts,
        internal_tool_names=tuple(call.tool_name for call in summary.calls) + allowed_tool_names,
        system_prompt=system_prompt,
    )
    calls = [_project_call(call, whitelist) for call in summary.calls]
    ctx = _resolve_termination_context(
        failure=failure, stream=stream, summary=summary, output_safety=output_safety
    )
    return _assemble_report(
        trace_id=trace_id,
        question=question,
        summary=summary,
        stream=stream,
        whitelist=whitelist,
        calls=calls,
        output_safety=output_safety,
        ctx=ctx,
        duration_seconds=duration_seconds,
        business_execution_budget_seconds=business_execution_budget_seconds,
        business_duration_seconds=business_duration_seconds,
        drain_duration_seconds=drain_duration_seconds,
        document_request=document_request,
        sheet_request=sheet_request,
    )


def _assemble_report(
    *,
    trace_id: str,
    question: str,
    summary: TurnAuditSummary,
    stream: TurnStreamRecorder,
    whitelist: frozenset[str],
    calls: list[dict[str, Any]],
    output_safety: OutputConstraintResult,
    ctx: _TerminationContext,
    duration_seconds: float,
    business_execution_budget_seconds: float | None,
    business_duration_seconds: float | None,
    drain_duration_seconds: float | None,
    document_request: DocumentRequest | None,
    sheet_request: SheetRequest | None,
) -> dict[str, Any]:
    """把已经算好的裁决结果拼成最终的输出报告字典。"""
    resources = _build_resources(
        stream=stream,
        duration_seconds=duration_seconds,
        business_execution_budget_seconds=business_execution_budget_seconds,
        business_duration_seconds=business_duration_seconds,
        drain_duration_seconds=drain_duration_seconds,
        calls=calls,
        summary=summary,
    )
    turn_section = _build_turn_section(
        summary=summary,
        stream=stream,
        output_safety=output_safety,
        ctx=ctx,
        duration_seconds=duration_seconds,
    )
    audit_section = _build_audit_section(
        summary=summary, stream=stream, calls=calls, whitelist=whitelist, ctx=ctx
    )
    return {
        "trace_id": trace_id,
        "question_bytes": len(question.encode("utf-8")),
        "turn": turn_section,
        "audit": audit_section,
        "resources": resources,
        "failure": ctx.effective_failure,
        "document_request": _project_document_request(document_request),
        "sheet_request": _project_sheet_request(sheet_request),
    }


def _resolve_effective_failure(
    failure: Mapping[str, str] | None, stream: TurnStreamRecorder
) -> dict[str, str] | None:
    """给定显式失败与流侧信号，推断出真正生效的失败原因。

    SDK 有时不通过 ``failure`` 显式报错，而是把"上下文过长"编码进
    ``result_subtype`` 或 ``result_error`` 的自由文本里；两条路径任一命中都
    改写为统一的 ``context_too_long``，否则沿用调用方传入的 ``failure``。
    """
    if failure:
        return dict(failure)
    result_error = (stream.result_error or "").casefold()
    context_error = "context" in result_error and any(
        marker in result_error for marker in ("long", "length", "limit", "window")
    )
    if (
        stream.result_subtype
        in {"error_context_too_long", "context_length_exceeded", "context_too_long"}
        or context_error
    ):
        return {"code": "context_too_long", "message": "agent_context_too_long"}
    return None


def _resolve_termination(
    effective_failure: Mapping[str, str] | None, summary: TurnAuditSummary
) -> tuple[str, str, bool]:
    """从生效失败推出 (termination_reason, termination_state, guard_triggered)。"""
    failure_code = effective_failure.get("code") if effective_failure else None
    guard_triggered = failure_code in _GUARD_FAILURE_CODES
    termination_reason = failure_code or ("completed" if summary.terminal_ok else "turn_not_closed")
    termination_state = (
        "guarded" if guard_triggered else ("completed" if effective_failure is None else "failed")
    )
    return termination_reason, termination_state, guard_triggered


def _resolve_user_result(
    output_safety: OutputConstraintResult, summary: TurnAuditSummary
) -> tuple[str, str]:
    """用户是否真的拿到结果：同时看工具调用结论与出口安全裁决。

    只看审计侧的 ``obtained`` 会漏掉"工具调用成功但最终正文因安全策略整段
    拒发"这种分岔；``withheld`` 时改写为独立终态，不伪装成功。
    """
    effective_user_result = (
        REDACTED_WITHHELD_RESULT if output_safety.withheld else summary.user_result.value
    )
    result_delivery = "confirmed" if effective_user_result == "obtained" else "not_confirmed"
    return effective_user_result, result_delivery


def _resolve_termination_context(
    *,
    failure: Mapping[str, str] | None,
    stream: TurnStreamRecorder,
    summary: TurnAuditSummary,
    output_safety: OutputConstraintResult,
) -> _TerminationContext:
    """依次推断失败原因、终止状态与用户结果，打包成共用上下文。"""
    effective_failure = _resolve_effective_failure(failure, stream)
    termination_reason, termination_state, guard_triggered = _resolve_termination(
        effective_failure, summary
    )
    effective_user_result, result_delivery = _resolve_user_result(output_safety, summary)
    return _TerminationContext(
        effective_failure=effective_failure,
        termination_reason=termination_reason,
        termination_state=termination_state,
        guard_triggered=guard_triggered,
        effective_user_result=effective_user_result,
        result_delivery=result_delivery,
    )


def _build_resources(
    *,
    stream: TurnStreamRecorder,
    duration_seconds: float,
    business_execution_budget_seconds: float | None,
    business_duration_seconds: float | None,
    drain_duration_seconds: float | None,
    calls: list[dict[str, Any]],
    summary: TurnAuditSummary,
) -> dict[str, Any]:
    """Resources 小节：耗时口径与工具调用计数的直接投影。

    ``business_execution_budget_seconds`` 是配置的预算，不是耗时承诺；
    ``business_duration_seconds``/``drain_duration_seconds`` 是分别测得的
    业务执行与收尾耗时；``duration_seconds`` 是实际总耗时——用户看到的必须
    是这一个，不是配置值。三个字段由调用方按需提供，缺省保持未知。
    """
    return {
        "agent_turns": stream.agent_turns,
        "agent_turns_status": "known" if stream.agent_turns is not None else "unknown",
        "duration_seconds": duration_seconds,
        "business_execution_budget_seconds": business_execution_budget_seconds,
        "business_duration_seconds": business_duration_seconds,
        "drain_duration_seconds": drain_duration_seconds,
        "tool_call_count": len(calls),
        "executed_tool_call_count": sum(1 for call in summary.calls if call.executed),
        "usage": stream.usage_summary,
    }


def _build_turn_section(
    *,
    summary: TurnAuditSummary,
    stream: TurnStreamRecorder,
    output_safety: OutputConstraintResult,
    ctx: _TerminationContext,
    duration_seconds: float,
) -> dict[str, Any]:
    """Turn 小节：用户可见的终态与正文。

    ``closed`` 要求审计侧恰好一次终止结果与消息流侧恰好一条不自报错误的
    ``ResultMessage`` 同时成立，且没有失败、没有绕过屏障的调用——分开计数、
    都要成立，避免"hook 没触发"被"消息流正常"掩盖；``gate_bypassed`` 是这条
    屏障失效唯一可观察的形状。
    """
    return {
        "closed": bool(
            summary.terminal_ok
            and stream.result_message_count == 1
            and stream.result_is_error is not True
            and ctx.effective_failure is None
            and not summary.ungated_calls
        ),
        "gate_bypassed": bool(summary.ungated_calls),
        "final_text": redact_free_text(output_safety.text),
        "output_safety": {
            "blocked": output_safety.blocked,
            "withheld": output_safety.withheld,
            "reasons": output_safety.reasons,
        },
        "final_text_bytes": summary.final_text_bytes,
        "user_result": ctx.effective_user_result,
        "terminal_result_count": summary.terminal_result_count,
        "sdk_result_message_count": stream.result_message_count,
        "sdk_result_is_error": stream.result_is_error,
        "sdk_result_subtype": redact_free_text(stream.result_subtype)
        if stream.result_subtype
        else None,
        "sdk_terminal_reason": redact_free_text(stream.terminal_reason)
        if stream.terminal_reason
        else None,
        "termination_state": ctx.termination_state,
        "termination_reason": ctx.termination_reason,
        "guard_triggered": ctx.guard_triggered,
        "result_delivery": ctx.result_delivery,
        "duration_seconds": duration_seconds,
        "session_id": stream.session_id,
    }


def _build_audit_section(
    *,
    summary: TurnAuditSummary,
    stream: TurnStreamRecorder,
    calls: list[dict[str, Any]],
    whitelist: frozenset[str],
    ctx: _TerminationContext,
) -> dict[str, Any]:
    """Audit 小节：工具调用本身的分类，独立于出口安全策略。

    用户实际拿到了什么以 ``turn.user_result`` 为准，两者一旦分岔以后者为准
    （见文件头）。``oversize_rewrite_count`` 记录 MCP 结果被截断提示改写为
    重查引导的次数——这份 JSON 是 worker 离开进程时唯一的出口，只能在这里
    查到，不能只留在进程内的 ``TurnAuditSummary`` 属性上。
    """
    return {
        "call_count": len(calls),
        "denied_count": len(summary.denied_calls),
        "failed_count": len(summary.failed_calls),
        "ungated_count": len(summary.ungated_calls),
        "oversize_rewrite_count": summary.oversize_rewrite_count,
        "tool_call_result": summary.user_result.value,
        "tool_result_count": stream.tool_result_count,
        "termination_reason": ctx.termination_reason,
        "guard_triggered": ctx.guard_triggered,
        "usage": stream.usage_summary,
        "executed_tool_names": [
            _project_tool_name(name, whitelist) for name in summary.executed_tool_names
        ],
        "denied": [call for call in calls if call["allowed"] is False],
        "calls": calls,
    }


def _project_document_request(document_request: DocumentRequest | None) -> dict[str, Any] | None:
    """把已通过出口安全检查的文档请求投影为可序列化字典。

    ``None`` 表示本轮未调用 ``deliver_document``；非 ``None`` 时只做投影，
    不重复上游已做过的校验。``markdown`` 随段落一起投影，供 gateway 侧转换
    路径消费，段落仍是兜底与幂等判据的来源。
    """
    if document_request is None:
        return None
    return {
        "title": document_request.title,
        "paragraphs": list(document_request.paragraphs),
        "markdown": document_request.markdown,
    }


def _project_sheet_request(sheet_request: SheetRequest | None) -> dict[str, Any] | None:
    """把已通过出口安全检查的表格请求投影为可序列化字典。

    形状与 :func:`_project_document_request` 对称；调用方保证两者至多一个
    非 ``None``，互斥校验不在这一层重复（真正的互斥校验在写终态事件那层）。
    """
    if sheet_request is None:
        return None
    return {"title": sheet_request.title, "rows": [list(row) for row in sheet_request.rows]}


def config_error_report(*, trace_id: str, message: str) -> dict[str, Any]:
    """配置错误也走同一个输出契约：stdout 永远是一个 JSON 对象。"""
    return {
        "trace_id": trace_id,
        "turn": None,
        "audit": None,
        "failure": {"code": "config_error", "message": message},
    }


def _project_call(call: ToolCallAudit, whitelist: frozenset[str]) -> dict[str, Any]:
    """把一条工具调用审计记录投影成可序列化字典。"""
    return {
        "tool_use_id": call.tool_use_id,
        "tool_name": _project_tool_name(call.tool_name, whitelist),
        "tool_input": dict(call.tool_input),
        # 三态：True 放行、False 拒绝、None 本层未判定。不许压成布尔——
        # "没经过我们"和"我们放行了"是完全不同的两件事。
        "allowed": call.allowed,
        "deny_reason_code": call.deny_reason_code.value if call.deny_reason_code else None,
        "executed": call.executed,
        "error": call.error,
        "result_kind": call.result_kind.value if call.result_kind else None,
    }


def _project_tool_name(tool_name: str, whitelist: frozenset[str]) -> str:
    """白名单内原样返回；否则脱敏，避免未知工具名把凭据带出进程。"""
    if tool_name in whitelist:
        return tool_name
    return redact_free_text(tool_name)
