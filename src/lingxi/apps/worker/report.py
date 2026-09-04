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

**输出安全的诚实终态**（[#141](https://github.com/Moshuiwang/lingxi/issues/141)、
[#149](https://github.com/Moshuiwang/lingxi/issues/149)）：``output_safety.withheld``
为真时（整段正文因无法安全展示而被拒发），本层把用户可见的 ``turn.user_result``
改写为独立、可查询的 ``redacted_withheld``，不再沿用审计侧按工具调用结果算出的
``obtained``——那个值只回答"工具调用有没有成功"，回答不了"用户有没有拿到内容"，
两者一旦分岔（例如一次正常查询成功、但最终正文因安全策略整段拒发），继续展示
``obtained`` 就是产品合同明令禁止的"伪装成功"。原始的工具调用分类改名保留在
``audit.tool_call_result``，不丢失、只是不再冒充"用户结果"。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from lingxi.core.execution.audit import ToolCallAudit, TurnAuditSummary, redact_free_text
from lingxi.core.execution.document_delivery import DocumentRequest, SheetRequest
from lingxi.core.execution.input_safety import constrain_output
from lingxi.core.execution.message_stream import TurnStreamRecorder

_GUARD_FAILURE_CODES = frozenset({"max_turns_exceeded", "turn_timeout", "cancelled", "drain_timeout"})
# 独立、可查询的安全拒发终态；命名与 #141/#149 产品决定一致，"最终命名可由实现
# 保持同义"——这里就是那个约定名字，运营/审计据此过滤，不猜测某个字符串巧合。
REDACTED_WITHHELD_RESULT = "redacted_withheld"


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
    """构造 worker 的输出报告。

    ``closed`` 同时要求两件事：审计侧恰好一次终止结果（来自 ``Stop`` hook）与消息流
    侧恰好一条 ``ResultMessage``。两个来源分开计数、都要成立——把它们并成一个数字，
    "hook 没触发"就会被"消息流正常"掩盖，而那正是屏障失效的样子。

    耗时口径（#143，产品负责人 2026-08-13 拍板）：``business_execution_budget_seconds``
    是配置的业务执行预算，不是端到端总耗时的承诺；``business_duration_seconds`` /
    ``drain_duration_seconds`` 是分别测得的业务执行与收尾耗时（收尾宽限独立、有
    界，不因预算耗尽被截断）；``duration_seconds`` 是实际总耗时——用户如果看到
    耗时，看到的必须是这一个，不是配置值。三个新字段由调用方按需提供，缺省时
    保持未知（``None``），不构造数据。

    ``document_request``（Issue #341 S-ES-2 报告契约；Issue #408 正式方案接线
    新增 ``markdown``）：``None`` 或已通过硬上限与出口安全检查的
    :class:`~lingxi.core.execution.document_delivery.
    DocumentRequest`，由调用方（``apps/worker/turn.py``）只在任务成功且模型本轮
    确实调用过 ``deliver_document`` 时传入非 ``None``。这里只做投影——把已经是
    受信任内部结构的 ``title``/``paragraphs``/``markdown`` 转成 JSON 可序列化的
    字典，不重复上游已经做过的校验或出口安全检查。

    ``sheet_request``（Issue #354 S-H3-2 表格分支）：与 ``document_request`` 同一
    机制新增的并列字段，形状对称——``None`` 或已通过硬上限与出口安全检查的
    :class:`~lingxi.core.execution.document_delivery.SheetRequest`，只在模型本轮
    调用过 ``deliver_spreadsheet`` 时非 ``None``。调用方（``apps/worker/turn.py``
    的回合级请求槽位）保证两者至多一个非 ``None``，这里不做互斥校验（真正的
    互斥校验在 ``adapters/postgres_conversation/_queue_outbox.py::
    write_terminal_event``，本层只做投影）。
    """

    allowed_tool_names = tuple(allowed_tools)
    whitelist = frozenset(allowed_tool_names)
    internal_tool_names = tuple(call.tool_name for call in summary.calls) + allowed_tool_names
    output_safety = constrain_output(
        final_text,
        forbidden_values=external_texts,
        internal_tool_names=internal_tool_names,
        system_prompt=system_prompt,
    )
    redacted_text = redact_free_text(output_safety.text)
    calls = [_project_call(call, whitelist) for call in summary.calls]
    effective_failure = dict(failure) if failure else None
    result_error = (stream.result_error or "").casefold()
    context_error = "context" in result_error and any(
        marker in result_error for marker in ("long", "length", "limit", "window")
    )
    if effective_failure is None and (
        stream.result_subtype
        in {
            "error_context_too_long",
            "context_length_exceeded",
            "context_too_long",
        }
        or context_error
    ):
        effective_failure = {
            "code": "context_too_long",
            "message": "agent_context_too_long",
        }

    failure_code = effective_failure.get("code") if effective_failure else None
    guard_triggered = failure_code in _GUARD_FAILURE_CODES
    termination_reason = failure_code or ("completed" if summary.terminal_ok else "turn_not_closed")
    termination_state = "guarded" if guard_triggered else (
        "completed" if effective_failure is None else "failed"
    )
    # 用户是否拿到了结果，必须同时满足"工具调用确认成功"与"最终正文没有因为
    # 安全策略被整段拒发"——只看前者正是 #141 描述的"内部记 obtained，用户拿到
    # 兜底文案"那个缺陷。withheld 时改写为独立终态，不伪装成功（合同第 2 条）。
    effective_user_result = (
        REDACTED_WITHHELD_RESULT if output_safety.withheld else summary.user_result.value
    )
    result_delivery = (
        "confirmed" if effective_user_result == "obtained" else "not_confirmed"
    )
    usage = stream.usage_summary
    resources = {
        "agent_turns": stream.agent_turns,
        "agent_turns_status": "known" if stream.agent_turns is not None else "unknown",
        "duration_seconds": duration_seconds,
        "business_execution_budget_seconds": business_execution_budget_seconds,
        "business_duration_seconds": business_duration_seconds,
        "drain_duration_seconds": drain_duration_seconds,
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
                and effective_failure is None
                and not summary.ungated_calls
            ),
            "gate_bypassed": bool(summary.ungated_calls),
            "final_text": redacted_text,
            "output_safety": {
                "blocked": output_safety.blocked,
                "withheld": output_safety.withheld,
                "reasons": output_safety.reasons,
            },
            "final_text_bytes": summary.final_text_bytes,
            "user_result": effective_user_result,
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
            "session_id": stream.session_id,
        },
        "audit": {
            "call_count": len(calls),
            "denied_count": len(summary.denied_calls),
            "failed_count": len(summary.failed_calls),
            "ungated_count": len(summary.ungated_calls),
            # #323：MCP 结果被截断提示改写为重查引导的次数。放在这里而不是只
            # 留在进程内的 TurnAuditSummary 上，是因为本函数的输出就是 worker
            # 离开进程时唯一的出口（见文件头）——批终 stage 冒烟要观测「改写确实
            # 发生过」，只能靠这份 JSON 里能查到，不能靠内部对象属性。
            "oversize_rewrite_count": summary.oversize_rewrite_count,
            # 工具调用本身的分类结论（不受出口安全策略影响）；用户实际拿到了
            # 什么必须看 turn.user_result，两者一旦分岔以后者为准（见文件头）。
            "tool_call_result": summary.user_result.value,
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
        "failure": effective_failure,
        "document_request": (
            {
                "title": document_request.title,
                "paragraphs": list(document_request.paragraphs),
                # Issue #408 正式方案接线：原始 markdown 随段落一起投影，供
                # gateway 侧官方转换路径消费；段落继续是兜底与幂等判据的来源。
                "markdown": document_request.markdown,
            }
            if document_request is not None
            else None
        ),
        "sheet_request": (
            {"title": sheet_request.title, "rows": [list(row) for row in sheet_request.rows]}
            if sheet_request is not None
            else None
        ),
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
