"""从执行器报告里读出判定输入，并据此选出这一轮的终态。

**终态优先级是硬的**：真中断 → 其他失败 → 协议残骸 → 正文被拒发 → 成功。这个顺序本身
就是几条产品红线的落点，调换任意两级都会让某一类真实事实被另一类掩盖。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from lingxi.apps.worker.report_extraction import (
    _denied_tool_summary,
    _protocol_breakdown_reasons,
    _report_document_request,
    _report_failure_signature,
    _report_guard_denied_count,
    _report_sheet_request,
    _report_token_usage,
    _tool_result_count,
    _unnamed_failure_code,
)
from lingxi.apps.worker.turn import MCP_BAD_GATEWAY_FAILURE_CODE
from lingxi.config.content import ContentCatalog, RenderedContent
from lingxi.core.delivery.ports import TerminalKind

#: 模型把工具调用协议写成正文散文时的失败码。
MODEL_PROTOCOL_BREAKDOWN_FAILURE_CODE = "model_protocol_breakdown"


def failure_content(catalog: ContentCatalog, code: object) -> tuple[str, RenderedContent]:
    """把失败码翻成可查询的原因与用户可见文案。

    几个确定性失败有自己的专属文案：对"问题本身步骤太多""回执太大"这类失败，「请稍后
    重试」是误导，专属文案给出可行动的建议。协议残骸刻意**不**新增专属文案——那是运维
    需要知道的事实，说给用户听本身就是又一次过程泄漏，专属性只保留在失败码里。

    Returns:
        ``(可查询的 error_kind, 用户可见文案)``。
    """
    if code == "context_too_long":
        return "context_too_long", catalog.text("worker.context_too_long")
    if code == "turn_timeout":
        return "running_timeout", catalog.text("worker.running_timeout")
    if code == "side_effect_uncertain":
        return "side_effect_uncertain", catalog.text("worker.side_effect_uncertain")
    if code == "max_turns_exceeded":
        return "max_turns_exceeded", catalog.text("worker.max_turns")
    if code == MODEL_PROTOCOL_BREAKDOWN_FAILURE_CODE:
        return MODEL_PROTOCOL_BREAKDOWN_FAILURE_CODE, catalog.text("worker.failed")
    if code == "result_too_large":
        return "result_too_large", catalog.text("worker.result_too_large")
    if code == MCP_BAD_GATEWAY_FAILURE_CODE:
        return MCP_BAD_GATEWAY_FAILURE_CODE, catalog.text("worker.mcp_bad_gateway")
    return "session_failed", catalog.text("worker.failed")


def empty_outcome() -> TurnOutcome:
    """开工前就被停止的任务：没有任何回合事实可记。"""
    return TurnOutcome(
        failure_code=None,
        failure_signature=None,
        final_text="",
        session_id=None,
        output_safety=None,
        withheld=False,
        deliverable=False,
        protocol_breakdown=False,
        denied_count=0,
        denied_tool_names=(),
        tool_result_count=0,
        guard_denied_count=None,
        token_usage=None,
        document_request=None,
        sheet_request=None,
        unnamed_failure_code="",
    )


@dataclass(frozen=True)
class TurnOutcome:
    """一轮回合结束后，从执行器报告里读出来的全部判定输入。"""

    failure_code: object
    failure_signature: str | None
    final_text: str
    session_id: str | None
    output_safety: Mapping[str, Any] | None
    withheld: bool
    deliverable: bool
    protocol_breakdown: bool
    denied_count: int
    denied_tool_names: tuple[str, ...]
    tool_result_count: int
    guard_denied_count: int | None
    token_usage: Mapping[str, int] | None
    document_request: Mapping[str, Any] | None
    sheet_request: Mapping[str, Any] | None
    unnamed_failure_code: str

    @classmethod
    def from_report(cls, report: Mapping[str, Any]) -> TurnOutcome:
        """解析一份回合报告。

        ``withheld`` 只对"本来会成功交付内容"的回合有意义，因此它与 ``deliverable``
        必须分别求值：失败回合的残余正文一旦触发出口安全，把 ``withheld`` 排在失败
        判定之前就会把一次真超时改写成"正文被拒发"——运维丢失真实失败原因，验收拿到
        假阳性证据。

        协议残骸单独判定、不依赖 ``withheld``：模型把工具调用协议写成正文散文时，出口
        安全会正确遮蔽敏感片段，但仍把遮蔽后的残余正文当成有效业务内容放行。
        """
        turn = report.get("turn") or {}
        failure = report.get("failure") or {}
        final_text = turn.get("final_text") if isinstance(turn, Mapping) else ""
        output_safety = turn.get("output_safety") if isinstance(turn, Mapping) else None
        denied_count, denied_tool_names = _denied_tool_summary(report)
        return cls(
            failure_code=failure.get("code") if isinstance(failure, Mapping) else None,
            failure_signature=_report_failure_signature(report),
            final_text=final_text if isinstance(final_text, str) else "",
            session_id=turn.get("session_id") if isinstance(turn, Mapping) else None,
            output_safety=output_safety if isinstance(output_safety, Mapping) else None,
            withheld=bool(isinstance(output_safety, Mapping) and output_safety.get("withheld")),
            deliverable=bool(turn.get("closed")) and not failure,
            protocol_breakdown=bool(_protocol_breakdown_reasons(output_safety)),
            denied_count=denied_count,
            denied_tool_names=denied_tool_names,
            tool_result_count=_tool_result_count(report),
            # 通报补数落库值与上面供日志用的计数**故意分开求值**：这两个是"取不到就留
            # 空、不编造"，服务的是统计聚合；上面那个取不到时如实记 0。两套"取不到"的
            # 语义不同，不能共用同一次求值结果。
            guard_denied_count=_report_guard_denied_count(report),
            token_usage=_report_token_usage(report),
            document_request=_report_document_request(report),
            sheet_request=_report_sheet_request(report),
            unnamed_failure_code=_unnamed_failure_code(report),
        )

    @property
    def stopped_by_interrupt(self) -> bool:
        """这一轮是不是**真的**被停止打断了。

        只认执行层给出的真中断——它是观测到本地停止信号已置位、真正调用过中断之后才抛出的，
        是这条链路上唯一带因果的信号。队列侧的「有人请求过停止」只说明某一刻有人按过：让一次
        并发到达的停止压过所有已经发生的事实，会把真实失败原因改写成「已停止」，也会把一个已经
        产出结果的成功降级掉、连同只有成功分支才写的会话标识一起丢失——那是「重启与重试不得
        造成用户结果丢失」这条红线。也不能用「没有失败码」反推成停止，或把执行层自报的「已中止」
        当成它的别名——后者与本地停止信号之间没有任何因果绑定。
        """
        return self.failure_code == "interrupted"


@dataclass(frozen=True)
class TerminalDecision:
    """这一轮该写哪种终态、给用户看什么。"""

    terminal_kind: str
    error_kind: str | None
    content: RenderedContent
    failure_code: object
    session_id: str | None = None
    document_request: Mapping[str, Any] | None = None
    sheet_request: Mapping[str, Any] | None = None


def decide_terminal(outcome: TurnOutcome, *, catalog: ContentCatalog) -> TerminalDecision:
    """按硬优先级选出终态。

    Args:
        outcome: 已经解析好的回合结果。
        catalog: 用户可见文案的取值口。

    Returns:
        终态种类、可查询的原因与用户可见正文。
    """
    if outcome.stopped_by_interrupt:
        return _stopped(outcome, catalog)
    if not outcome.deliverable:
        return _failed(outcome, catalog)
    if outcome.protocol_breakdown:
        return _protocol_breakdown(catalog)
    if outcome.withheld:
        return _withheld(outcome, catalog)
    return _success(outcome, catalog)


def _stopped(outcome: TurnOutcome, catalog: ContentCatalog) -> TerminalDecision:
    """执行途中被真中断：有残余正文就连正文一起交付。"""
    content = (
        catalog.text("worker.stopped_result", result=outcome.final_text, contains_model_text=True)
        if outcome.final_text
        else catalog.text("worker.stopped")
    )
    return TerminalDecision(
        terminal_kind=TerminalKind.STOPPED.value,
        error_kind="stopped",
        content=content,
        failure_code=outcome.failure_code,
    )


def _failed(outcome: TurnOutcome, catalog: ContentCatalog) -> TerminalDecision:
    """回合没有收口或带着失败：按失败码选用户文案与终态种类。

    失败终态的失败码不允许为空——"没有失败码"不是失败原因，只是没人起名字。补码发生在
    用户文案与终态种类按**原始**失败码求值之后，因此补码不改变用户看到的内容。
    """
    error_kind, content = failure_content(catalog, outcome.failure_code)
    terminal_kind = (
        TerminalKind.TIMEOUT.value
        if outcome.failure_code == "turn_timeout"
        else TerminalKind.FAILED.value
    )
    return TerminalDecision(
        terminal_kind=terminal_kind,
        error_kind=error_kind,
        content=content,
        failure_code=outcome.failure_code or outcome.unnamed_failure_code,
    )


def _protocol_breakdown(catalog: ContentCatalog) -> TerminalDecision:
    """正文里出现内部工具名或过程标记：不得判成功。

    这永远是模型把工具调用协议写成了正文散文，不是一个可以交付给用户的答案。复用通用
    失败文案——"模型把协议写成了正文"是运维需要知道的事实，说给用户听本身就是又一次
    过程泄漏；专属性只保留在失败码里。
    """
    error_kind, content = failure_content(catalog, MODEL_PROTOCOL_BREAKDOWN_FAILURE_CODE)
    return TerminalDecision(
        terminal_kind=TerminalKind.FAILED.value,
        error_kind=error_kind,
        content=content,
        failure_code=MODEL_PROTOCOL_BREAKDOWN_FAILURE_CODE,
    )


def _withheld(outcome: TurnOutcome, catalog: ContentCatalog) -> TerminalDecision:
    """整段正文因安全策略被拒发：即使回合收口也不得记成功。

    用户没有拿到结果，必须走独立、可查询的终态；失败码同样不留空，否则运维按失败码
    过滤时会唯独漏掉这一类。
    """
    return TerminalDecision(
        terminal_kind=TerminalKind.REDACTED_WITHHELD.value,
        error_kind="redacted_withheld",
        content=catalog.text("worker.redacted_withheld"),
        failure_code=outcome.failure_code or "redacted_withheld",
    )


def _success(outcome: TurnOutcome, catalog: ContentCatalog) -> TerminalDecision:
    """回合已收口、有结果、没有失败、正文也干净：照常交付。

    即使停止信号晚到，这份已经产出的结果照常交付，会话标识照常持久化。文档与表格投递
    请求**只在这一支**转发：其余分支即使字段恰好非空也绝不建投递请求——用户没拿到
    问答结果，就不该收到对应的文档。
    """
    return TerminalDecision(
        terminal_kind=TerminalKind.SUCCESS.value,
        error_kind=None,
        content=RenderedContent(
            key="worker.result", version=catalog.version, text=outcome.final_text
        ),
        failure_code=outcome.failure_code,
        session_id=outcome.session_id,
        document_request=outcome.document_request,
        sheet_request=outcome.sheet_request,
    )
