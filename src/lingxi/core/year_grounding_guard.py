"""年份接地护栏第二层：相对时间问句 + 全部查询年份不含当前年份的结构性检测。

内测事故背景：用户问相对时间问题，模型全部查询都取错了年份，产出一份整年错误、
且未声明数据年份的报告。第一层（提示词）已要求模型自行换算并声明取数区间，但
曾被真实忽略过；本模块是不改模型行为的结构性检测层——只在 worker 侧观察"问句
用了相对时间表述，但全部工具调用取的年份都不是当前年份"这个组合，命中就产出一条
不含问句/答案正文的结构化信号交给调用方，范围只到"检测 + 告警"，不拦截、不
自动纠正、不改投递路径（调用点是纯旁路，检测异常不影响任务终态）。

不并入 ``core/innertest_content_capture.py``（两者是消费关系，混在一起会让一边的
改动悄悄影响另一边，本模块不反向 import 它，只用最小 duck-typed 形状）。判定三
条件（:func:`detect_year_grounding_suspect`）：问句命中触发词表、解析出至少一个
查询年份、当前年份不在其中，三者同时成立才判定可疑——每个环节的判据细节与已知
误报/漏报边界见各常量与函数自己的 docstring（宁可漏报，不可错报）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from lingxi.core.mcp_naming import QUERY_MCP_SERVER_NAME

#: 触发词表：最近/今年/本月/上周/上个月/近N天/N月之后/以来一类。刻意保持纯字面量
#: 列表，扩展只需要加一行——不引入配置文件或正则以外的机制。是启发式列表，不是
#: 自然语言理解：不在词表里的表述（例如纯"7月怎么样"不含"之后/以来"）不会被
#: 命中，这类问句本来就无法仅从字面判断是否隐含"相对当前"的语义，不属于本层
#: 能力范围。
RELATIVE_TIME_LITERAL_TERMS: tuple[str, ...] = (
    "最近",
    "今年",
    "本年",
    "本月",
    "这个月",
    "本周",
    "这周",
    "上周",
    "上个月",
    "上月",
    "以来",
    "至今",
    "到现在为止",
    "目前为止",
)

#: 字面量列表覆盖不了的相对时间表述（"近N天""N月之后"一类，N 是变量），用正则补。
RELATIVE_TIME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"近\s*\d+\s*(?:个)?(?:天|日|周|月|年)"),
    re.compile(r"\d+\s*月(?:份)?\s*(?:之后|以后|以来)"),
    re.compile(r"\d+\s*(?:天|日)\s*(?:之内|以内|内)"),
)

#: 真实问数 MCP 经 Agent SDK 暴露的完整工具名——``mcp__{服务名}__{工具名}``，
#: 服务名单一事实来源见 ``core/mcp_naming.QUERY_MCP_SERVER_NAME``。
QUERY_METRIC_TOOL_NAME = f"mcp__{QUERY_MCP_SERVER_NAME}__query_metric"

#: 只看这两个字段名（顶层或 ``filters`` 嵌套均可）。外部问数 MCP 若换成别的参数名
#: 或换到第三个位置，本模块会读不到年份，同样保守地判定为"不可疑"，需要跟着现网
#: 真实参数形状同步更新——这是结构性护栏依赖外部工具参数约定的固有局限，不是
#: 实现缺陷。
_DATE_FIELD_NAMES: tuple[str, ...] = ("start_date", "end_date")

#: 四位年份 token：1900-2099，前后不能紧跟其它数字（避免把更长数字里的中间四位
#: 误认成年份）。
_YEAR_TOKEN_PATTERN = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")

#: 紧凑 8 位日期 ``YYYYMMDD``：年份仍按 1900-2099，月份 01-12，日期 01-31，前后
#: 不能紧跟其它数字（避免把更长数字串误认成日期，也避免残缺数字误当成 8 位紧凑
#: 日期）。不做逐月天数上限校验（例如不拒绝"2月30日"）——范围校验目的只是过滤
#: 明显不是日期的 8 位数字，不是实现完整日历校验，真实问数 MCP 不会给出日历上
#: 不存在的日期。只捕获年份这一段（第 1 组）。
_COMPACT_DATE_PATTERN = re.compile(
    r"(?<!\d)((?:19|20)\d{2})(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?!\d)"
)


class ToolCallLike(Protocol):
    """本模块对"一次工具调用"的最小形状要求（duck typing，见模块文档）。

    与 :class:`~lingxi.core.innertest_content_capture.CapturedToolCall` 结构兼容，
    但本模块不 import 它，见模块文档。
    """

    tool_name: str
    tool_input: Mapping[str, Any]


@dataclass(frozen=True)
class YearGroundingSuspect:
    """一次"相对时间问句 + 全部查询年份都不是当前年份"的可疑信号。

    刻意不含问句原文、答案正文或任何工具调用的完整入参/结果——调用方
    （``apps/worker/service.py``）的要求是"不携带问句与答案正文"，本类型的字段
    集合就是这条要求本身：只留判定所需的最小结构化事实。
    """

    task_id: str
    matched_terms: tuple[str, ...]
    query_years: tuple[int, ...]
    current_year: int

    def to_alert_fields(self) -> dict[str, object]:
        """结构化告警要携带的取值——调用方直接把这份字典喂给日志/告警出口。"""
        return {
            "task_id": self.task_id,
            "matched_relative_time_terms": list(self.matched_terms),
            "query_years": list(self.query_years),
            "current_year": self.current_year,
        }


def find_relative_time_terms(question: str) -> tuple[str, ...]:
    """返回问句里命中的相对时间表述，去重保序；未命中返回空元组。"""
    if not isinstance(question, str) or not question:
        return ()
    found: list[str] = []
    for term in RELATIVE_TIME_LITERAL_TERMS:
        if term in question and term not in found:
            found.append(term)
    for pattern in RELATIVE_TIME_PATTERNS:
        for match in pattern.finditer(question):
            text = match.group(0)
            if text not in found:
                found.append(text)
    return tuple(found)


def extract_query_years(tool_calls: Iterable[ToolCallLike]) -> tuple[int, ...]:
    """从全部 ``query_metric`` 调用的 ``start_date``/``end_date`` 里解析出年份集合。

    顶层与 ``filters`` 嵌套对象下各读一遍——真实 ``query_metric`` 入参把日期字段
    放在 ``filters`` 下，顶层保留读取是为未来形状变化留冗余，不是假设两处会同时
    出现。去重排序返回；同一次调用、同一个位置的两个字段都能解析出年份时两个都
    计入（例如跨年区间）。非 ``query_metric`` 调用、缺失字段、非字符串取值、
    ``filters`` 不是 Mapping、全部日期字段都不是本函数认得的格式都安静跳过并
    返回空集——不是没有问题，而是没有证据支持"全部查询年份都不是当前年份"这句
    话，宁可让参数格式异常的调用漏报，也不错误地对着解析失败的调用报警。
    """
    years: set[int] = set()
    for call in tool_calls:
        tool_name = getattr(call, "tool_name", None)
        if tool_name != QUERY_METRIC_TOOL_NAME:
            continue
        tool_input = getattr(call, "tool_input", None)
        if not isinstance(tool_input, Mapping):
            continue
        years.update(_years_from_date_fields(tool_input))
        filters = tool_input.get("filters")
        if isinstance(filters, Mapping):
            years.update(_years_from_date_fields(filters))
    return tuple(sorted(years))


def _years_from_date_fields(mapping: Mapping[str, Any]) -> set[int]:
    """从一个"可能含 start_date/end_date"的映射里抠出年份集合。

    顶层 ``tool_input`` 与嵌套的 ``filters`` 对象共用同一套字段名与解析规则，
    抽成一个函数避免两处各写一遍、日后修改字段名或解析规则时漏改一处。
    """
    years: set[int] = set()
    for field_name in _DATE_FIELD_NAMES:
        years.update(_years_in_value(mapping.get(field_name)))
    return years


def _years_in_value(value: Any) -> tuple[int, ...]:
    r"""从一个日期字段取值里解析年份，支持分隔符日期与紧凑 ``YYYYMMDD`` 两类形状。

    两个正则的 ``(?<!\d)``/``(?!\d)`` 边界互斥（一个要求命中后紧跟非数字，
    一个要求恰好 8 位纯数字且前后不紧跟数字），同一段文本不会被两条规则重复
    计入同一个年份来源；调用方 :func:`extract_query_years` 用 ``set`` 合并去重，
    这里不需要预先去重。
    """
    if not isinstance(value, str):
        return ()
    separated = (int(match.group(0)) for match in _YEAR_TOKEN_PATTERN.finditer(value))
    compact = (int(match.group(1)) for match in _COMPACT_DATE_PATTERN.finditer(value))
    return tuple(separated) + tuple(compact)


def detect_year_grounding_suspect(
    *,
    task_id: str,
    question: str,
    tool_calls: Sequence[ToolCallLike],
    current_year: int,
) -> YearGroundingSuspect | None:
    """三个条件全部成立才判定可疑：问句命中词表、解析出年份、当前年份不在其中。

    ``query_years`` 为空（一次查询都没有、或全部日期字段都解析不出年份）时同样
    返回 ``None``——"存在至少一次查询"与"全部查询年份都不是当前年份"合起来要求
    的是"确实看到了指向别的年份的证据"，不是"看不出年份就当作可疑"。

    已知误报：年初问「上个月」类表述时，若模型正确换算成了上一年，查询年份会
    全部落在上一年——这是模型算对了，却会被本函数字面判定为可疑；本函数只有
    结构性判据，不理解"这次换算对不对"，处置已限定为只产出信号供告警。
    """
    matched_terms = find_relative_time_terms(question)
    if not matched_terms:
        return None
    query_years = extract_query_years(tool_calls)
    if not query_years:
        return None
    if current_year in query_years:
        return None
    return YearGroundingSuspect(
        task_id=task_id,
        matched_terms=matched_terms,
        query_years=query_years,
        current_year=current_year,
    )
