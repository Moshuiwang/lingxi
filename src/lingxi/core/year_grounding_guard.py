"""年份接地护栏第二层：相对时间问句 + 全部查询年份不含当前年份的结构性检测（Issue #326）。

## 背景与分层

Issue #326 记录的内测事故：用户问「最近、尤其是 7 月之后」，模型 69 次查询全部取
2025 年区间（当时的当前年份是 2026），产出一份专业完整、整年错误、且未声明数据
年份的深度报告。处置分两层：第一层（提示词 v6，已上线）是行为约束——要求模型
自己把相对时间换算成当前年份、在回答开头声明取数区间；提示词条款此前已有过同类
约束、仍被真实忽略过一次，因此不可单独依赖。本模块是第二层——结构性检测：不改
模型行为，只在 worker 侧观察"问句用了相对时间表述，但全部工具调用取的年份都不是
当前年份"这个组合，命中就产出一条不含问句/答案正文的结构化信号，交给调用方决定
怎么处置。

本卡（批次 5 卡 E）范围只到"检测 + 告警"，不做拦截、不做自动纠正、不改答案投递
路径——调用方见 :mod:`lingxi.apps.worker.service`，其调用点是纯旁路且自带防御
包装，检测异常不影响任务终态。

## 为什么是一个独立模块，不是塞进 ``core/innertest_content_capture.py``

``innertest_content_capture.py`` 自己的模块文档已经讲过一次同样的道理："把两件
目的不同的事塞进同一个模块，迟早会有一次改动为了满足一边的需要而悄悄影响另一边"。
内容采集的职责是"收集原始素材"；本模块的职责是"对已经收集到的素材做一次时间
语义判断"，两者是消费关系（本模块读内容采集产出的 ``question_content``/
``tool_calls`` 形状），不是同一件事，因此独立成模块。本模块不反过来 import 内容
采集模块——只需要"工具名 + 工具入参"这个最小形状，用结构化（duck-typed）参数而
不是具体类型，调用方不需要为了喂给本模块而绑定到内容采集这一条已经打上"内测轮
采集"标签的通道。

## 检测口径

- **相对时间词命中**：命中 :data:`RELATIVE_TIME_LITERAL_TERMS` 中任意字面量，或
  :data:`RELATIVE_TIME_PATTERNS` 中任意正则（"近N天/月/年""N月之后/以来"一类，
  N 是变量）。词表刻意留在模块顶层常量，扩展只需要加一行，不是本模块要解决的问题。
- **查询年份**：只看 ``mcp__query__query_metric``（:data:`QUERY_METRIC_TOOL_NAME`）
  一种工具调用的 ``start_date``/``end_date`` 两个字段，**顶层与 ``filters`` 嵌套
  对象下各读一遍**——Issue #326 L4a 追评（2026-08-26，部署 ``20260826-cbf16354a85b``
  后容器内直测）确认真实 ``query_metric`` 入参把两个日期字段放在 ``filters`` 对象
  下，形如 ``{"filters": {"start_date": "20260819", "end_date": "20260826", ...},
  "group_by": [...], "metric_id": ...}``；顶层同名字段继续保留读取，两处都可能有，
  未来形状变化留冗余，不因为只观察到一种真实形状就收窄成单一读取路径。年份解析
  支持两类形状：分隔符日期（``"2025-01-01"`` 一类）抠取 19xx/20xx 四位年份 token；
  以及真实观测到的紧凑 ``YYYYMMDD`` 八位数字（例如 ``"20260819"``），取前四位为
  年份，同时校验整体恰为 8 位纯数字、年份前缀在 1900-2099、月份 01-12、日期 01-31
  合法范围内，避免把任意 8 位数字误认成日期，代价见下面「已知边界」。
- **判定**：问句命中词表 **且** 至少解析出一个查询年份 **且** 当前年份不在解析出
  的年份集合里，三者同时成立才判定为可疑。三个条件都取"宁可不报，不可错报"的
  保守方向——见「已知边界」。

## 已知边界（如实登记，不假装覆盖全部情形）

- 一次任务里如果全部 ``query_metric`` 调用的 ``start_date``/``end_date`` 都不是
  本模块认得的格式（提取不出任何年份 token），本模块判定为"不可疑"——不是因为
  真的没问题，而是没有证据支持"全部查询年份都不是当前年份"这句话。这会让参数
  格式异常的调用漏报，而不是错误地对着一批解析失败的调用报警（宁可漏报，不可
  在证据不足时误报）。
- 只看 ``start_date``/``end_date`` 两个字段名（顶层或 ``filters`` 嵌套均可）；
  外部问数 MCP 若换成别的参数名（例如单一的 ``date_range``）或换到 ``filters``
  以外的第三个位置，本模块会读不到年份，同样保守地判定为"不可疑"，需要跟着
  现网真实参数形状同步更新——这是结构性护栏依赖外部工具参数约定的固有局限，
  不是实现缺陷。
- 紧凑 ``YYYYMMDD`` 解析不做逐月天数上限校验（例如不会拒绝"2月30日"）——月份
  01-12、日期 01-31 的范围校验目的是过滤明显不是日期的 8 位数字，不是实现完整
  日历校验；真实问数 MCP 不会给出日历上不存在的日期，这不是本层要防的风险。
- 相对时间词表是启发式列表，不是自然语言理解——不在词表里的表述（例如纯"7月
  怎么样"不含"之后/以来"）不会被命中；这类问句本来就无法仅从字面判断是否隐含
  "相对当前"的语义，不属于本层能力范围。
- **年初边界（已知误报，本批起由死路变活路）**：年初（例如 1 月）问「上个月/
  上周」一类相对时间表述时，如果模型正确地把它换算成了上一年（1 月的"上个月"
  应查上一年 12 月），查询年份会全部落在上一年、``current_year`` 不在解析出的
  集合里——这是模型算对了，却会被本模块三个条件字面同时成立判定为可疑，是已知
  误报，本模块只有结构性判据、不理解"这次换算对不对"这层语义。处置已限定为
  仅产出结构化信号交给调用方记 stderr 告警（见模块顶部「背景与分层」），不进
  用户侧回复、不进管理群通知，代价可接受。**在本批修复 ``filters`` 嵌套与紧凑
  ``YYYYMMDD`` 两处解析之前，``extract_query_years`` 对真实 ``query_metric``
  入参恒返回空集，这条误报路径从未被真实触发过（死路）；本批修复后年初场景才会
  真正走到这条路径（变活）**，如实登记供后续判断是否需要为"模型换算正确"这类
  情形单独排除。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol, Sequence

from lingxi.core.mcp_naming import QUERY_MCP_SERVER_NAME

#: 触发词表，取自 Issue #326 的举例：最近/今年/本月/上周/上个月/近N天/N月之后/以来。
#: 刻意保持纯字面量列表，扩展只需要加一行——不引入配置文件或正则以外的机制。
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

#: 只看这两个字段名——见模块文档「已知边界」。
_DATE_FIELD_NAMES: tuple[str, ...] = ("start_date", "end_date")

#: 四位年份 token：1900-2099，前后不能紧跟其它数字（避免把更长数字里的中间四位
#: 误认成年份）。
_YEAR_TOKEN_PATTERN = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")

#: 紧凑 8 位日期 ``YYYYMMDD``（真实问数 MCP 观测形状，见模块文档「检测口径」）：
#: 年份仍按 1900-2099（与四位年份 token 同一约束），月份 01-12，日期 01-31。
#: 前后不能紧跟其它数字，与四位年份 token 同一处理方式——避免把更长数字串里的
#: 中间 8 位误认成日期，也避免把 6 位/7 位残缺数字误当成 8 位紧凑日期解析。
#: 只捕获年份这一段（第 1 组），月/日两段只参与校验、不需要取值。
_COMPACT_DATE_PATTERN = re.compile(
    r"(?<!\d)((?:19|20)\d{2})(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?!\d)"
)


class ToolCallLike(Protocol):
    """本模块对"一次工具调用"的最小形状要求（duck typing，见模块文档）。

    与 :class:`~lingxi.core.innertest_content_capture.CapturedToolCall` 结构兼容，
    但本模块不 import 它——见模块文档「为什么是一个独立模块」。
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

    顶层与 ``filters`` 嵌套对象下各读一遍——见模块文档「检测口径」，真实
    ``query_metric`` 入参把日期字段放在 ``filters`` 下，顶层保留读取是为未来
    形状变化留冗余，不是假设两处会同时出现。去重排序返回；同一次调用、同一个
    位置的两个字段都能解析出年份时两个都计入（例如跨年区间）。非 ``query_metric``
    调用、缺失字段、非字符串取值、``filters`` 不是 Mapping 都安静跳过——本函数
    只负责"抠出看得懂的年份"，形状异常不算错误，见模块文档「已知边界」。
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
    """从一个日期字段取值里解析年份，支持分隔符日期与紧凑 ``YYYYMMDD`` 两类形状。

    两个正则的 ``(?<!\\d)``/``(?!\\d)`` 边界互斥（一个要求命中后紧跟非数字，
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
    """三个条件全部成立才判定可疑，任一不成立返回 ``None``（见模块文档「检测口径」）。

    ``query_years`` 为空（一次查询都没有、或全部日期字段都解析不出年份）时同样
    返回 ``None``——"存在至少一次查询"与"全部查询年份都不是当前年份"合起来要求
    的是"确实看到了指向别的年份的证据"，不是"看不出年份就当作可疑"。
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
