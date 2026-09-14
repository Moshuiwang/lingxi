"""从一次回合报告里读出供终态判定、落库与低敏审计日志使用的字段。

纯函数：不碰网络、不碰数据库、不持有任何状态。回合报告由执行层投影而来，这里
只做结构校验与字面判定——形状不对就如实返回「取不到」，不猜测、不编造。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lingxi.core.failure_signature import sanitize_failure_signature

# 拒绝文案对用户承诺"问题已经被记录"：一次回合里模型可能反复撞同一个越界
# 工具，工具名列表上界防止一次异常回合把收口日志撑大。
_MAX_LOG_DENIED_TOOL_NAMES = 20


def _denied_tool_summary(report: Mapping[str, Any]) -> tuple[int, tuple[str, ...]]:
    """从一次回合报告里取出被拒工具调用的计数与工具名。

    这里只取计数与工具名——不取 ``tool_input``，工具参数正文与用户资料值
    不属于这条低敏审计事件。早退分支（未真正跑过 ``PreToolUse`` 判定）取不
    到就如实记 0/空，不假装有据可查。**已知边界（如实登记、不修）**：executor
    在拒绝**之后**异常退出时，已发生的拒绝计数会跟着不完整的 ``report``
    一起丢失，同样如实返回 0/空——回合本身已落到响亮的失败终态，唯一代价
    是看不到补充事实。
    """
    audit = report.get("audit") if isinstance(report, Mapping) else None
    if not isinstance(audit, Mapping):
        return 0, ()
    count = audit.get("denied_count")
    count = count if isinstance(count, int) else 0
    names: list[str] = []
    denied_entries = audit.get("denied")
    if isinstance(denied_entries, list):
        for entry in denied_entries:
            if isinstance(entry, Mapping):
                name = entry.get("tool_name")
                if isinstance(name, str):
                    names.append(name)
    return count, tuple(names[:_MAX_LOG_DENIED_TOOL_NAMES])


def _report_guard_denied_count(report: Mapping[str, Any]) -> int | None:
    """从一次回合报告里取出**供落库**的守卫拒绝计数（迁移 ``0070``）。

    与 :func:`_denied_tool_summary` 故意**不共享**同一个返回值：这里要写进
    ``task.guard_denied_count`` 供统计聚合，聚合层必须能区分"真的查过、结果
    是零次拒绝"与"没有可用的审计数据"，把后者算成 0 会悄悄低估真实拒绝次数
    且没有任何信号能让读者发现。``report["audit"]`` 不存在，或 ``denied_
    count`` 不是 ``int``/是负数（结构性地不可信）都返回 ``None``；其余情况
    原样返回真实整数，包括合法的 0。
    """
    audit = report.get("audit") if isinstance(report, Mapping) else None
    if not isinstance(audit, Mapping):
        return None
    count = audit.get("denied_count")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        return count
    return None


#: ``core/execution/message_stream.py::_usage_summary`` 产出的四个已知 token
#: 计数字段名，与该模块同源——这里独立列一份常量而不是 import 那个私有名字，
#: 避免为四个字面量常量新增一条跨层 import 边界。字段名一旦那边改动，这里
#: 也要跟着改（无自动化保证，人工同步）。
_TOKEN_USAGE_FIELD_NAMES = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _report_token_usage(report: Mapping[str, Any]) -> dict[str, int] | None:
    """从一次回合报告里取出**供落库**的 token 用量（迁移 ``0070``）。

    ``report["resources"]["usage"]`` 恒为 ``{"status": "known"|"unknown",
    ["fields": {...}]}``。只有 ``status == "known"`` 时才有真正可信的计数
    ——``"unknown"`` 覆盖三种取不到的原因，共同点是没有可入库的数字，返回
    ``None`` 如实反映，不编造 0。早退分支同样返回 ``None``；返回值只包含
    ``fields`` 里实际出现的键，不为缺失的字段补零。
    """
    resources = report.get("resources") if isinstance(report, Mapping) else None
    usage = resources.get("usage") if isinstance(resources, Mapping) else None
    if not isinstance(usage, Mapping) or usage.get("status") != "known":
        return None
    fields = usage.get("fields")
    if not isinstance(fields, Mapping):
        return None
    result: dict[str, int] = {}
    for name in _TOKEN_USAGE_FIELD_NAMES:
        candidate = fields.get(name)
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
            result[name] = candidate
    return result or None


def _report_document_request(report: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """从一次回合报告里取出**供落库**的文档投递请求。

    ``report["document_request"]`` 由 ``build_report`` 投影：``None`` 或已过
    检查的 ``{"title", "paragraphs", "markdown"}``。这里只做结构校验，形状
    不对一律返回 ``None``——结构性地不可信就不传，不猜测、不编造。
    ``markdown`` 单独降级：它是段落之外的附加值，不是幂等判据也不是兜底
    路径依赖的字段，形状不对时只丢弃这一个字段（落库为 ``NULL``），不因此
    拒绝整条本来合法的登记请求。
    """
    request = report.get("document_request") if isinstance(report, Mapping) else None
    if not isinstance(request, Mapping):
        return None
    title = request.get("title")
    paragraphs = request.get("paragraphs")
    if not isinstance(title, str) or not title:
        return None
    if (
        not isinstance(paragraphs, list)
        or not paragraphs
        or not all(isinstance(paragraph, str) for paragraph in paragraphs)
    ):
        return None
    markdown = request.get("markdown")
    return {
        "title": title,
        "paragraphs": paragraphs,
        "markdown": markdown if isinstance(markdown, str) and markdown else None,
    }


def _report_sheet_request(report: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """从一次回合报告里取出**供落库**的表格投递请求。

    与 :func:`_report_document_request` 逐项对称：``report["sheet_request"]``
    由 ``apps/worker/report.py::build_report`` 投影为 ``None`` 或
    ``{"title": str, "rows": list[list[str]]}``，这里只做结构校验，形状不对一律
    返回 ``None``——同一纪律：结构性地不可信就不传，不猜测、不编造。
    """
    request = report.get("sheet_request") if isinstance(report, Mapping) else None
    if not isinstance(request, Mapping):
        return None
    title = request.get("title")
    rows = request.get("rows")
    if not isinstance(title, str) or not title:
        return None
    if (
        not isinstance(rows, list)
        or not rows
        or not all(
            isinstance(row, list) and row and all(isinstance(cell, str) for cell in row)
            for row in rows
        )
    ):
        return None
    return {"title": title, "rows": rows}


def _tool_result_count(report: Mapping[str, Any]) -> int:
    """从一次回合报告里取出这一轮**真实**工具调用次数，补一处可观测性缺口。

    运维此前没有任何字段能直接回答"这一轮到底有没有真的调用过工具"；这里
    把 ``report["audit"]["tool_result_count"]`` 取出并写进
    ``worker.task.terminal``。``== 0`` **单独不构成**异常信号——闲聊类问题
    本来就不需要调用工具；真正的判定见 ``_protocol_breakdown_reasons``，与
    这里的调用次数无关。早退分支同样如实返回 0。
    """
    audit = report.get("audit") if isinstance(report, Mapping) else None
    if not isinstance(audit, Mapping):
        return 0
    count = audit.get("tool_result_count")
    return count if isinstance(count, int) else 0


# P0 护栏：模型正文里出现内部工具名或过程标记（协议细节），**永远**是模型把
# 工具调用协议写成了正文散文，不是"内容需要脱敏但业务结论还在"。净化层职责
# 到"遮蔽敏感片段"为止，"这段正文根本不该被当成答案交付"的判断只能发生在这里。
# 刻意排除其余原因码：那三类是"内容含有已知敏感值/系统提示"，withheld 分支
# 已按"是否还有幸存业务内容"正确处理，收紧过窄的边界会误伤正常业务回答。
_PROTOCOL_BREAKDOWN_REASON_CODES = frozenset({"internal_tool_name", "process_marker"})


def _protocol_breakdown_reasons(output_safety: Mapping[str, Any] | None) -> tuple[str, ...]:
    """从 ``turn.output_safety.reasons`` 里取出命中 P0 护栏的原因码（如果有）。

    只做字面判定，不猜测：``reasons`` 形状不对就如实返回空元组，交给上层按
    "没有命中"处理——护栏要收紧的是"命中了却被当成成功"，不是"形状可疑就一律
    判失败"（那会把真实的执行器异常伪装成协议异常，污染审计）。
    """
    if not isinstance(output_safety, Mapping):
        return ()
    raw_reasons = output_safety.get("reasons")
    if not isinstance(raw_reasons, (list, tuple)):
        return ()
    return tuple(
        str(reason) for reason in raw_reasons if str(reason) in _PROTOCOL_BREAKDOWN_REASON_CODES
    )


#: 「这次失败没有人给它起名字」时按报告里已有事实推出的三个显式码：三个都
#: 落进默认分支，用户可见文案逐字不变，新增的区分度只留在审计/日志侧。
#: ``gate_bypassed`` 有工具调用绕过了 ``PreToolUse`` 判定；``unnamed_
#: failure`` 报告带了 ``failure`` 但没有 ``code``；``turn_not_closed`` 回合
#: 就是没收口。取值与 ``report.py`` 的 ``termination_reason`` 一致，不另造词。
GATE_BYPASSED_FAILURE_CODE = "gate_bypassed"
UNNAMED_FAILURE_CODE = "unnamed_failure"
TURN_NOT_CLOSED_FAILURE_CODE = "turn_not_closed"


def _report_failure_signature(report: Mapping[str, Any]) -> str | None:
    """从一次回合报告里取出失败签名；没有（例如失败根本不来自异常）返回 ``None``。

    通常签名是底层异常的固定类别摘要；少数结构化外因也可以携带
    稳定的分类签名，例如指标 MCP 的 ``mcp.query.http_502``。两种形状都必须经过
    同一条严格形状校验，不能让跨进程报告把自由文本带进审计出口。

    ``None`` 是精确语义、不是"以后补"：``turn_timeout``/``drain_timeout``/
    ``cancelled`` 这些失败码本身已经把原因说全了，没有底层异常可签名，编一个
    占位符只会让"有签名"这件事失去信息量。
    """
    failure = report.get("failure") if isinstance(report, Mapping) else None
    if not isinstance(failure, Mapping):
        return None
    signature = failure.get("signature")
    if not isinstance(signature, str) or not signature:
        return None
    return sanitize_failure_signature(signature)


def _unnamed_failure_code(report: Mapping[str, Any]) -> str:
    """失败终态但 ``failure`` 没给出 ``code`` 时，按报告里已有的事实推一个显式码。

    **不推断成 ``stopped``、也不推断成任何用户可见语义**：这里只回答"这次失败
    在报告里长什么样"，三个取值各自对应一个可核对的事实（见上方常量说明）。
    """
    failure = report.get("failure") if isinstance(report, Mapping) else None
    if isinstance(failure, Mapping) and failure:
        return UNNAMED_FAILURE_CODE
    turn = report.get("turn") if isinstance(report, Mapping) else None
    if isinstance(turn, Mapping) and turn.get("gate_bypassed"):
        return GATE_BYPASSED_FAILURE_CODE
    return TURN_NOT_CLOSED_FAILURE_CODE
