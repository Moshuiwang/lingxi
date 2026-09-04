"""年份接地护栏第二层的纯逻辑断言（Issue #326，批次 5 卡 E）。

这些用例不依赖数据库、飞书或 Claude Agent SDK：核心逻辑全部在
``lingxi.core.year_grounding_guard``，可以在 CI 的 gate 中强制执行。worker 侧
接线（``_capture_content_if_enabled``/``_check_year_grounding_suspect``）的告警
出口、防御包装与既有流程零回归见 ``tests/test_worker_queue_consumer.py`` 的
``YearGroundingSuspectAlertTests``。
"""

from __future__ import annotations

import unittest
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from lingxi.core.year_grounding_guard import (
    QUERY_METRIC_TOOL_NAME,
    YearGroundingSuspect,
    detect_year_grounding_suspect,
    extract_query_years,
    find_relative_time_terms,
)


@dataclass(frozen=True)
class _FakeToolCall:
    """满足 ``ToolCallLike`` 最小形状的独立测试夹具——不依赖
    ``core.innertest_content_capture.CapturedToolCall``，证明本模块真的只按
    duck typing 消费两个属性，不绑定具体类型。"""

    tool_name: str
    tool_input: Mapping[str, Any]


def _query_call(
    *,
    start_date: Any = None,
    end_date: Any = None,
    tool_name: str = QUERY_METRIC_TOOL_NAME,
    filters: Any = None,
) -> _FakeToolCall:
    tool_input: dict[str, Any] = {}
    if start_date is not None:
        tool_input["start_date"] = start_date
    if end_date is not None:
        tool_input["end_date"] = end_date
    if filters is not None:
        tool_input["filters"] = filters
    return _FakeToolCall(tool_name=tool_name, tool_input=tool_input)


def _real_captured_tool_input(*, start_date: str, end_date: str) -> dict[str, Any]:
    """还原 Issue #326 L4a 追评记录的真实 ``query_metric`` 入参形状（2026-08-26 部署
    ``20260826-cbf16354a85b`` 后容器内直测）：日期字段嵌在 ``filters`` 对象下，取值是
    紧凑 ``YYYYMMDD`` 八位数字。``business``/``group_by``/``metric_id`` 用占位内容
    还原"这些键确实存在"这一形状事实，不含真实业务线或指标名称。
    """

    return {
        "filters": {
            "business": ["示例业务线A", "示例业务线B"],
            "end_date": end_date,
            "start_date": start_date,
        },
        "group_by": ["day"],
        "metric_id": "示例指标ID",
    }


class RelativeTimeTermDetectionTests(unittest.TestCase):
    """``find_relative_time_terms``：Issue #326 举例的词表命中，及未命中的空返回。"""

    def test_matches_literal_terms_from_the_issue_examples(self) -> None:
        self.assertEqual(find_relative_time_terms("最近数据下滑得厉害"), ("最近",))
        self.assertEqual(find_relative_time_terms("今年的整体表现怎么样"), ("今年",))
        self.assertEqual(find_relative_time_terms("本月充值有没有回暖"), ("本月",))
        self.assertEqual(find_relative_time_terms("上周对比上个月"), ("上周", "上个月"))

    def test_matches_variable_patterns_not_covered_by_the_literal_list(self) -> None:
        """"近N天""N月之后"这类 N 是变量的表述，字面量列表覆盖不到，靠正则补。"""

        self.assertEqual(find_relative_time_terms("近7天的充值趋势"), ("近7天",))
        self.assertEqual(find_relative_time_terms("尤其是7月之后下滑明显"), ("7月之后",))
        # "以来"本身也在字面量词表里，"8月份以来"这句因此会同时命中字面量与正则
        # 两条规则——这是预期行为（两条规则各自独立生效，不互斥），不是重复统计。
        self.assertEqual(find_relative_time_terms("8月份以来持续走低"), ("以来", "8月份以来"))

    def test_returns_matched_terms_deduplicated_in_scan_order(self) -> None:
        self.assertEqual(
            find_relative_time_terms("最近怎么样，最近的数据呢，最近是不是还在最近"),
            ("最近",),
        )

    def test_plain_questions_without_relative_time_wording_do_not_match(self) -> None:
        """否定用例：不在词表里的表述不命中——纯"7月怎么样"不含"之后/以来"。"""

        self.assertEqual(find_relative_time_terms("查一下2025年1月到8月的充值数据"), ())
        self.assertEqual(find_relative_time_terms("8月的收入是多少"), ())

    def test_defensive_on_non_string_or_empty_question(self) -> None:
        self.assertEqual(find_relative_time_terms(""), ())
        self.assertEqual(find_relative_time_terms(None), ())  # type: ignore[arg-type]


class QueryYearExtractionTests(unittest.TestCase):
    """``extract_query_years``：只看 ``query_metric`` 调用的 ``start_date``/
    ``end_date``，年份 token 解析与已知边界（解析不出年份时安静跳过）。"""

    def test_extracts_years_from_iso_formatted_dates(self) -> None:
        calls = [_query_call(start_date="2025-01-01", end_date="2025-08-25")]
        self.assertEqual(extract_query_years(calls), (2025,))

    def test_collects_distinct_years_across_multiple_calls_and_a_cross_year_range(self) -> None:
        """跨年区间（同一次调用两个字段年份不同）与多次调用都要合并进同一个集合。"""

        calls = [
            _query_call(start_date="2025-01-01", end_date="2025-08-25"),
            _query_call(start_date="2024-12-01", end_date="2025-01-01"),
        ]
        self.assertEqual(extract_query_years(calls), (2024, 2025))

    def test_ignores_calls_to_other_tools(self) -> None:
        calls = [_query_call(start_date="2025-01-01", tool_name="mcp__query__list_metrics")]
        self.assertEqual(extract_query_years(calls), ())

    def test_ignores_calls_with_missing_or_malformed_date_fields(self) -> None:
        """已知边界：解析不出年份时安静跳过，不是错误（模块文档「已知边界」）。"""

        calls = [
            _query_call(),  # 两个字段都没有
            _query_call(start_date=20250101, end_date=None),  # 非字符串
            _query_call(start_date="不是日期", end_date="也不是"),
        ]
        self.assertEqual(extract_query_years(calls), ())

    def test_ignores_tool_input_that_is_not_a_mapping(self) -> None:
        """防御性：``tool_input`` 形状异常（非 Mapping）不得让整体解析抛异常。"""

        calls = [_FakeToolCall(tool_name=QUERY_METRIC_TOOL_NAME, tool_input="not-a-mapping")]  # type: ignore[arg-type]
        self.assertEqual(extract_query_years(calls), ())

    def test_empty_tool_calls_yield_no_years(self) -> None:
        self.assertEqual(extract_query_years(()), ())

    # -- L4a 真实形状修复（Issue #326 追评，2026-08-26）：以下用例覆盖两处根因 --

    def test_extracts_years_from_filters_nested_iso_formatted_dates(self) -> None:
        """**变异验红锚点 A**：只隔离"读 ``filters`` 嵌套"这一处修复。

        用分隔符日期（不是紧凑格式），确保本用例只会因为"没有读 ``filters``
        嵌套"而失败，不会被紧凑日期解析的正确性掩盖。把 ``extract_query_years``
        里读 ``filters`` 嵌套的两行（``years.update(_years_from_date_fields(
        filters))`` 那一段）删掉/注释掉，本用例必须变红；人工执行记录见本卡
        最终报告的「变异验红记录」一节。
        """

        calls = [_query_call(filters={"start_date": "2025-01-01", "end_date": "2025-08-25"})]
        self.assertEqual(extract_query_years(calls), (2025,))

    def test_extracts_years_from_top_level_compact_dates(self) -> None:
        """**变异验红锚点 B**：只隔离"紧凑 YYYYMMDD 解析"这一处修复。

        用顶层字段（不走 ``filters`` 嵌套），确保本用例只会因为"紧凑日期解析
        不到年份"而失败，不会被 filters 嵌套读取的正确性掩盖。把
        ``_years_in_value`` 里 ``_COMPACT_DATE_PATTERN`` 相关的那一行删掉/
        注释掉，本用例必须变红；人工执行记录见本卡最终报告的「变异验红记录」
        一节。
        """

        calls = [_query_call(start_date="20260819", end_date="20260826")]
        self.assertEqual(extract_query_years(calls), (2026,))

    def test_extracts_years_from_the_real_captured_shape(self) -> None:
        """正例：Issue #326 L4a 追评记录的真实捕获形状——``filters`` 嵌套 **且**
        紧凑 ``YYYYMMDD`` 日期同时出现，两处修复合起来才能让本用例通过。"""

        calls = [_FakeToolCall(tool_name=QUERY_METRIC_TOOL_NAME, tool_input=_real_captured_tool_input(start_date="20260819", end_date="20260826"))]
        self.assertEqual(extract_query_years(calls), (2026,))

    def test_collects_years_from_both_top_level_and_nested_filters_when_both_present(self) -> None:
        """两处"都可能有"是刻意的冗余设计，不是假设互斥：同一次调用顶层与
        ``filters`` 各给一个不同年份时，两个年份都要被收集到（模块文档「检测
        口径」）。"""

        calls = [_query_call(start_date="2025-01-01", filters={"start_date": "20260101"})]
        self.assertEqual(extract_query_years(calls), (2025, 2026))

    def test_ignores_an_eight_digit_number_that_is_not_a_valid_compact_date(self) -> None:
        """负例：8 位纯数字但月/日不在合法范围内，不得被误当成紧凑日期提取
        出年份——即使它的前四位恰好数值上"看起来像"年份。"""

        calls = [
            _query_call(start_date="12345678"),  # 年份前缀非 19xx/20xx，月份"56"也非法
            _query_call(start_date="20261301"),  # 月份 13 非法
            _query_call(start_date="20260132"),  # 日期 32 非法
            _query_call(start_date="20260100"),  # 日期 00 非法
        ]
        self.assertEqual(extract_query_years(calls), ())

    def test_ignores_a_longer_digit_run_with_an_embedded_valid_compact_date_leading_extra_digit(
        self,
    ) -> None:
        """负例 + **变异验红锚点 C（前边界）**：长数字串中间嵌了一段合法的
        8 位 ``YYYYMMDD``（"20260819"，年月日都合法），但前面多出一位数字
        （"1"）——不得因为中间恰好存在一段合法日期就提取出年份。本用例只在
        ``_COMPACT_DATE_PATTERN`` 的前边界 ``(?<!\\d)`` 生效时通过：把
        ``year_grounding_guard.py`` 定义 ``_COMPACT_DATE_PATTERN`` 那三行
        （现约 119-121 行）里的 ``(?<!\\d)`` 删掉，本用例必须变红——正则会在
        "120260819" 中间的 "20260819" 处匹配出年份 2026。"""

        calls = [_query_call(start_date="120260819")]
        self.assertEqual(extract_query_years(calls), ())

    def test_ignores_a_longer_digit_run_with_an_embedded_valid_compact_date_trailing_extra_digit(
        self,
    ) -> None:
        """负例 + **变异验红锚点 D（后边界）**：与上一条对称——合法的 8 位
        ``YYYYMMDD``（"20260819"）出现在数字串开头，但后面多出一位数字
        （"12"里的"1"），不得提取出年份。本用例只在 ``_COMPACT_DATE_PATTERN``
        的后边界 ``(?!\\d)`` 生效时通过：把同一处定义里的 ``(?!\\d)`` 删掉，
        本用例必须变红——正则会在 "2026081912" 开头的 "20260819" 处匹配出
        年份 2026。与上一条合起来覆盖两处边界各自独立被删除的情形。"""

        calls = [_query_call(start_date="2026081912")]
        self.assertEqual(extract_query_years(calls), ())

    def test_ignores_filters_that_is_not_a_mapping(self) -> None:
        """防御性：``filters`` 键存在但形状异常（非 Mapping）不得让整体解析
        抛异常，安静跳过该处、不影响顶层字段的正常解析。"""

        calls = [_query_call(start_date="2025-01-01", filters="not-a-mapping")]
        self.assertEqual(extract_query_years(calls), (2025,))


class DetectYearGroundingSuspectTests(unittest.TestCase):
    """``detect_year_grounding_suspect``：三条件与的完整判定（V-326 主断言）。"""

    def test_fires_when_relative_wording_and_all_query_years_are_not_current(self) -> None:
        """① 正例：相对时间问句 + 全部查询年份都不是当前年份 → 必须判定可疑。

        2025 是固定写死的过去年份（相对本仓库现在与可预见的将来恒成立，不会
        变成"当前年份"），因此本用例不依赖真实运行时钟。
        """

        calls = [_query_call(start_date="2025-01-01", end_date="2025-08-25")]
        suspect = detect_year_grounding_suspect(
            task_id="tsk-year-suspect-1",
            question="最近，尤其是7月之后数据下滑得厉害",
            tool_calls=calls,
            current_year=2026,
        )
        self.assertIsNotNone(suspect)
        assert suspect is not None  # for type-checkers
        self.assertEqual(suspect.task_id, "tsk-year-suspect-1")
        self.assertEqual(suspect.matched_terms, ("最近", "7月之后"))
        self.assertEqual(suspect.query_years, (2025,))
        self.assertEqual(suspect.current_year, 2026)

    def test_fires_end_to_end_on_the_real_captured_shape_with_relative_wording_and_a_past_compact_year(
        self,
    ) -> None:
        """端到端接线用例（Issue #326 L4a 追评）：相对时间问句 + ``filters`` 嵌套
        紧凑 ``YYYYMMDD`` 全 2025 年查询 → 告警必发。

        这是此前因两处形状根因（顶层读取/分隔符日期）测不到的真实路径——修复
        之前，同样的输入会因为 ``extract_query_years`` 恒返回空集而在
        ``if not query_years: return None`` 处静默放行，检测层在真实数据上
        形同虚设（Issue #326 L4a 追评原话）。本用例把这条真实路径钉死：任何
        回归都会让本用例从"必须命中"变回"静默不命中"。
        """

        calls = [
            _FakeToolCall(
                tool_name=QUERY_METRIC_TOOL_NAME,
                tool_input=_real_captured_tool_input(start_date="20250115", end_date="20250825"),
            )
        ]
        suspect = detect_year_grounding_suspect(
            task_id="tsk-real-shape-end-to-end",
            question="最近，尤其是7月之后数据下滑得厉害",
            tool_calls=calls,
            current_year=2026,
        )
        self.assertIsNotNone(suspect)
        assert suspect is not None  # for type-checkers
        self.assertEqual(suspect.query_years, (2025,))

    def test_mutation_anchor_does_not_fire_when_a_query_year_matches_the_current_year(self) -> None:
        """**变异验红锚点**：本用例是"全部查询年份均≠当前年份"这一条件的红线。

        把 ``detect_year_grounding_suspect`` 里 ``if current_year in query_years:
        return None`` 这一条年份比对删掉/注释掉，本用例必须变红——命中相对时间词、
        且确实发生过查询，但查询年份里含当前年份，不该判定可疑。人工执行记录见
        本卡最终报告的「变异验红记录」一节。
        """

        calls = [_query_call(start_date="2026-01-01", end_date="2026-08-25")]
        suspect = detect_year_grounding_suspect(
            task_id="tsk-current-year",
            question="最近的数据表现怎么样",
            tool_calls=calls,
            current_year=2026,
        )
        self.assertIsNone(suspect)

    def test_does_not_fire_without_relative_time_wording(self) -> None:
        """否定用例②：问句未命中词表，即使全部查询年份都不是当前年份也不报警。"""

        calls = [_query_call(start_date="2025-01-01", end_date="2025-08-25")]
        suspect = detect_year_grounding_suspect(
            task_id="tsk-no-relative-wording",
            question="查一下2025年1月到8月的充值数据",
            tool_calls=calls,
            current_year=2026,
        )
        self.assertIsNone(suspect)

    def test_does_not_fire_with_zero_queries(self) -> None:
        """否定用例③：命中相对时间词，但本任务一次查询都没有发生。"""

        suspect = detect_year_grounding_suspect(
            task_id="tsk-zero-queries",
            question="最近的数据表现怎么样",
            tool_calls=(),
            current_year=2026,
        )
        self.assertIsNone(suspect)

    def test_does_not_fire_when_no_year_can_be_parsed_from_any_call(self) -> None:
        """已知边界：全部调用的日期字段都解析不出年份时，保守判定不可疑（不是
        因为真的没问题，而是没有证据——模块文档「已知边界」第一条）。"""

        calls = [_query_call(start_date="不是日期", end_date=None)]
        suspect = detect_year_grounding_suspect(
            task_id="tsk-unparseable-dates",
            question="最近的数据表现怎么样",
            tool_calls=calls,
            current_year=2026,
        )
        self.assertIsNone(suspect)


class AlertFieldsTests(unittest.TestCase):
    """``YearGroundingSuspect.to_alert_fields``：结构化告警不携带问句/答案正文。"""

    def test_alert_fields_are_limited_to_the_structured_signal(self) -> None:
        suspect = YearGroundingSuspect(
            task_id="tsk-1",
            matched_terms=("最近", "7月之后"),
            query_years=(2025,),
            current_year=2026,
        )
        fields = suspect.to_alert_fields()
        self.assertEqual(
            fields,
            {
                "task_id": "tsk-1",
                "matched_relative_time_terms": ["最近", "7月之后"],
                "query_years": [2025],
                "current_year": 2026,
            },
        )
        # 显式否定断言：不得出现任何看起来像问句/答案正文的键。
        self.assertNotIn("question", fields)
        self.assertNotIn("answer", fields)
        self.assertNotIn("question_content", fields)
        self.assertNotIn("answer_content", fields)


if __name__ == "__main__":
    unittest.main()
