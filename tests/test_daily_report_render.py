"""`core/daily_report.py` 的纯函数断言（Issue #303 S-O-01）。

覆盖新增断言：V-通报-01（每段可独立不可判定）、V-通报-02（不可判定必须显式呈现，
不得渲染成零或省略）、V-通报-04（节流语义）、V-通报-05（正文不含用户标识/原文）、
V-通报-06（UTC+北京双时区标注）。真库聚合断言在
`tests/test_postgres_daily_report.py`；职责层调度、判重与送达失败断言在
`tests/test_daily_report_duty.py`。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from lingxi.core.daily_report import (
    CALL_COUNT_BASELINE_UNAVAILABLE_REASON,
    DENIED_COUNT_ALL_NULL_REASON,
    RESOURCE_USAGE_ALL_NULL_REASON,
    ActiveUserStats,
    DailyReportInputs,
    DeliveryOutcomeStats,
    FailureReasonCount,
    LatencyStats,
    LocalOverrideActivity,
    MetricCoverageGap,
    PartialCount,
    Section,
    StatusDistribution,
    ThrottledFailureLine,
    TokenUsageStats,
    apply_repeat_throttle,
    build_active_user_stats,
    build_delivery_outcome,
    build_denied_count_stats,
    build_failure_top,
    build_guard_triggered_count,
    build_latency_stats,
    build_local_override_activity,
    build_metric_coverage_gap,
    build_status_distribution,
    build_token_usage_stats,
    render_daily_report,
)

WINDOW_START = datetime(2026, 8, 23, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 8, 24, tzinfo=timezone.utc)
# 投递结果段独立窗口（opus 批量审查 P2 修复）：比其余段落的统计窗口早一整天，
# 见 `core/daily_report.py` 模块文档「投递结果段为什么用一个独立、更早的窗口」。
DELIVERY_WINDOW_START = datetime(2026, 8, 22, tzinfo=timezone.utc)
DELIVERY_WINDOW_END = datetime(2026, 8, 23, tzinfo=timezone.utc)

# 用于「正文绝不含用户标识/原文」断言的固定敏感值：真实姓名、工号、邮箱、飞书标识、
# 内部 ULID 各一个，一个都不许出现在渲染结果里。
FORBIDDEN_VALUES = ("张三", "E1001", "zhangsan@example.com", "ou_person_0001", "usr_01JQZX3M5N7P9R1T3V5W7Y9A0B")


def _all_determined_inputs(
    *,
    active_users: ActiveUserStats | None = None,
    status_distribution: StatusDistribution | None = None,
    failure_top: tuple[FailureReasonCount, ...] = (),
    guard_triggered: int = 0,
    denied_count: PartialCount | None = None,
    latency: LatencyStats | None = None,
    resource_usage: TokenUsageStats | None = None,
    delivery_outcome: DeliveryOutcomeStats | None = None,
    delivery_window_start: datetime = DELIVERY_WINDOW_START,
    delivery_window_end: datetime = DELIVERY_WINDOW_END,
) -> DailyReportInputs:
    return DailyReportInputs(
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        active_users=Section.of(active_users or ActiveUserStats(0, ())),
        status_distribution=Section.of(
            status_distribution or StatusDistribution(success=0, failed=0, timeout=0, stopped=0, in_progress=0)
        ),
        failure_top=Section.of(failure_top),
        guard_triggered=Section.of(guard_triggered),
        denied_count=Section.of(denied_count or PartialCount(total=0, covered_tasks=0, uncovered_tasks=0)),
        latency=Section.of(latency),
        resource_usage=Section.of(
            resource_usage
            or TokenUsageStats(
                input_tokens=0,
                output_tokens=0,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
                covered_tasks=0,
                uncovered_tasks=0,
            )
        ),
        delivery_outcome=Section.of(
            delivery_outcome or DeliveryOutcomeStats(delivered_card=0, delivered_fallback_text=0, expired=0, pending=0)
        ),
        delivery_window_start=delivery_window_start,
        delivery_window_end=delivery_window_end,
    )


class SectionConstructionTests(unittest.TestCase):
    def test_of_is_determined_and_carries_the_value(self) -> None:
        section = Section.of(42)
        self.assertTrue(section.is_determined)
        self.assertEqual(section.value, 42)
        self.assertIsNone(section.undetermined_reason)

    def test_undetermined_carries_no_value(self) -> None:
        section = Section.undetermined("查询超时")
        self.assertFalse(section.is_determined)
        self.assertIsNone(section.value)
        self.assertEqual(section.undetermined_reason, "查询超时")

    def test_an_empty_reason_is_refused(self) -> None:
        """否定断言：不可判定必须给出理由，不能只留一个空段落——空理由等于
        「不可判定」被静默降级成「什么都没说」，与 #303 的显式呈现要求相反。"""

        with self.assertRaises(ValueError):
            Section.undetermined("")
        with self.assertRaises(ValueError):
            Section.undetermined("   ")


class ActiveUserBucketingTests(unittest.TestCase):
    def test_buckets_partition_by_fixed_edges(self) -> None:
        stats = build_active_user_stats([1, 2, 5, 6, 10, 11, 50])
        self.assertEqual(stats.active_user_count, 7)
        self.assertEqual(
            dict(stats.task_count_buckets),
            {"1 条": 1, "2-5 条": 2, "6-10 条": 2, "11+ 条": 2},
        )

    def test_no_users_is_a_determined_zero_not_undetermined(self) -> None:
        """「窗口内没有活跃用户」是一个真实取得的结论（零），不是「取不到」——
        两者渲染的文案必须不同，调用方不得把「查了、答案是零」误当成「没查到」。"""

        stats = build_active_user_stats([])
        self.assertEqual(stats.active_user_count, 0)
        self.assertEqual(sum(count for _, count in stats.task_count_buckets), 0)


class StatusDistributionTests(unittest.TestCase):
    def test_success_failed_timeout_stopped_are_classified_independently(self) -> None:
        rows = [
            ("succeeded", None, 10),
            ("failed", "session_failed", 2),
            ("failed", "turn_timeout", 1),
            ("failed", "running_timeout", 1),
            ("stopped", "stopped", 3),
            ("queued", None, 4),
            ("running", None, 1),
        ]
        distribution = build_status_distribution(rows)
        self.assertEqual(distribution.success, 10)
        self.assertEqual(distribution.failed, 2)
        self.assertEqual(distribution.timeout, 2)
        self.assertEqual(distribution.stopped, 3)
        self.assertEqual(distribution.in_progress, 5)

    def test_max_turns_exceeded_is_a_failure_not_a_timeout(self) -> None:
        """`max_turns_exceeded` 是轮数超限，不是墙钟超时——两者是 `V-护栏-01/02`
        刻意区分的两个互不相同的原因码，进「失败」桶而不进「超时」桶。"""

        distribution = build_status_distribution([("failed", "max_turns_exceeded", 1)])
        self.assertEqual(distribution.failed, 1)
        self.assertEqual(distribution.timeout, 0)


class FailureTopTests(unittest.TestCase):
    def test_ordered_by_count_descending_then_reason_code(self) -> None:
        rows = [
            ("failed", "session_failed", 1),
            ("failed", "context_too_long", 5),
            ("stopped", "stopped", 5),
            ("failed", "redacted_withheld", 2),
        ]
        top = build_failure_top(rows)
        self.assertEqual(
            [entry.reason_code for entry in top],
            ["context_too_long", "stopped", "redacted_withheld", "session_failed"],
        )

    def test_succeeded_and_in_progress_rows_are_excluded(self) -> None:
        rows = [("succeeded", None, 100), ("queued", None, 5)]
        self.assertEqual(build_failure_top(rows), ())

    def test_limit_truncates_the_tail(self) -> None:
        rows = [("failed", f"reason_{i}", i + 1) for i in range(5)]
        top = build_failure_top(rows, limit=2)
        self.assertEqual(len(top), 2)
        self.assertEqual(top[0].reason_code, "reason_4")

    def test_a_non_positive_limit_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            build_failure_top([], limit=0)


class GuardTriggeredTests(unittest.TestCase):
    def test_sums_only_known_guard_reason_codes(self) -> None:
        rows = [
            ("failed", "turn_timeout", 2),
            ("failed", "max_turns_exceeded", 3),
            ("failed", "drain_timeout", 1),
            ("failed", "session_failed", 10),
            ("succeeded", None, 99),
        ]
        self.assertEqual(build_guard_triggered_count(rows), 6)

    def test_no_guard_reasons_present_is_a_determined_zero(self) -> None:
        self.assertEqual(build_guard_triggered_count([("succeeded", None, 5)]), 0)


class RepeatThrottleTests(unittest.TestCase):
    """`V-通报-04`：重复出现的失败分类节流，语义见 `core/daily_report.py` 模块文档。"""

    def test_a_first_appearance_has_streak_one_and_is_not_throttled(self) -> None:
        lines, updated = apply_repeat_throttle({}, [FailureReasonCount("session_failed", 3)])
        self.assertEqual(lines, (ThrottledFailureLine("session_failed", 3, streak_days=1, throttled=False),))
        self.assertEqual(updated, {"session_failed": 1})

    def test_the_seventh_consecutive_day_is_still_not_throttled(self) -> None:
        lines, updated = apply_repeat_throttle(
            {"session_failed": 6}, [FailureReasonCount("session_failed", 3)]
        )
        self.assertFalse(lines[0].throttled)
        self.assertEqual(lines[0].streak_days, 7)
        self.assertEqual(updated, {"session_failed": 7})

    def test_the_eighth_consecutive_day_is_throttled(self) -> None:
        lines, updated = apply_repeat_throttle(
            {"session_failed": 7}, [FailureReasonCount("session_failed", 3)]
        )
        self.assertTrue(lines[0].throttled)
        self.assertEqual(lines[0].streak_days, 8)
        self.assertEqual(updated, {"session_failed": 8})

    def test_a_gap_day_resets_the_streak(self) -> None:
        """今天没出现在 Top 榜的原因码，从更新后的字典里消失——连续计数归零，
        它哪天再出现按 1 重新计，而不是延续旧的连续天数。"""

        _, updated = apply_repeat_throttle({"session_failed": 9}, [])
        self.assertEqual(updated, {})
        lines, updated_again = apply_repeat_throttle(updated, [FailureReasonCount("session_failed", 1)])
        self.assertEqual(lines[0].streak_days, 1)
        self.assertFalse(lines[0].throttled)

    def test_independent_reason_codes_track_independent_streaks(self) -> None:
        lines, updated = apply_repeat_throttle(
            {"session_failed": 8, "turn_timeout": 1},
            [FailureReasonCount("session_failed", 1), FailureReasonCount("turn_timeout", 1)],
        )
        by_code = {line.reason_code: line for line in lines}
        self.assertTrue(by_code["session_failed"].throttled)
        self.assertFalse(by_code["turn_timeout"].throttled)
        self.assertEqual(updated, {"session_failed": 9, "turn_timeout": 2})


class LatencyStatsTests(unittest.TestCase):
    def test_no_samples_returns_none_not_a_zeroed_struct(self) -> None:
        """窗口内没有已完成任务是一个真实的、可判定的结论（"没有样本"），不应该
        伪装成一组全零的假统计——调用方据此区分「查了、没有样本」与「不可判定」。"""

        self.assertIsNone(build_latency_stats([]))

    def test_a_single_sample_is_its_own_average_median_and_p90(self) -> None:
        stats = build_latency_stats([262.0])
        assert stats is not None
        self.assertEqual(stats.sample_count, 1)
        self.assertEqual(stats.average_seconds, 262.0)
        self.assertEqual(stats.median_seconds, 262.0)
        self.assertEqual(stats.p90_seconds, 262.0)
        self.assertEqual(stats.max_seconds, 262.0)

    def test_percentiles_use_linear_interpolation_on_sorted_samples(self) -> None:
        # 0,10,...,90（10 个样本）：中位数落在 45（4.5 位插值），P90 落在索引 8.1 处。
        samples = [float(value) for value in range(0, 100, 10)]
        stats = build_latency_stats(samples)
        assert stats is not None
        self.assertEqual(stats.sample_count, 10)
        self.assertAlmostEqual(stats.median_seconds, 45.0)
        self.assertAlmostEqual(stats.p90_seconds, 81.0)
        self.assertEqual(stats.max_seconds, 90.0)

    def test_unsorted_input_is_handled_correctly(self) -> None:
        stats = build_latency_stats([300.0, 100.0, 200.0])
        assert stats is not None
        self.assertEqual(stats.max_seconds, 300.0)
        self.assertEqual(stats.median_seconds, 200.0)


class DeliveryOutcomeTests(unittest.TestCase):
    def test_four_buckets_are_classified_independently(self) -> None:
        rows = [
            ("card", True, False, 15),
            ("text", True, False, 3),
            (None, False, True, 2),
            (None, False, False, 1),
        ]
        stats = build_delivery_outcome(rows)
        self.assertEqual(stats.delivered_card, 15)
        self.assertEqual(stats.delivered_fallback_text, 3)
        self.assertEqual(stats.expired, 2)
        self.assertEqual(stats.pending, 1)

    def test_an_unexpected_kind_with_received_true_falls_back_to_pending_not_silently_dropped(
        self,
    ) -> None:
        """未知的 `platform_message_kind` 取值不得被吞掉：既不是卡片也不是文本，
        既然平台还没给出可分类的结论就归入「待定」，总数因此仍然对得上。"""

        stats = build_delivery_outcome([("unknown_kind", True, False, 1)])
        self.assertEqual(stats.pending, 1)
        self.assertEqual(stats.delivered_card, 0)
        self.assertEqual(stats.delivered_fallback_text, 0)


class RenderUndeterminedSegmentTests(unittest.TestCase):
    """`V-通报-01`/`V-通报-02`：挖掉一段数据源，该段显式「不可判定」，其余段落照常。

    每个测试独立地把**一个**段落换成 `Section.undetermined(...)`，其余段落保持
    正常取值，断言只有被换掉的那一段在正文里出现「不可判定」字样，其余段落的
    真实数值原样出现——这就是编排者要求的「挖掉一段数据源→该段显式不可判定、
    其余段正常」用例，逐段各做一次。
    """

    def _render(self, inputs: DailyReportInputs) -> str:
        return render_daily_report(inputs)

    def test_active_users_alone_undetermined(self) -> None:
        inputs = _all_determined_inputs(
            status_distribution=StatusDistribution(5, 1, 0, 0, 0),
            latency=LatencyStats(3, 100.0, 100.0, 100.0, 100.0),
        )
        inputs = DailyReportInputs(**{**inputs.__dict__, "active_users": Section.undetermined("模拟数据源故障")})
        text = self._render(inputs)
        self.assertIn("活跃用户与任务量分布：不可判定（原因：模拟数据源故障）", text)
        # 其余段落照常：状态分布与时延的真实数字原样出现，没有被一起打成不可判定。
        self.assertIn("成功 5，失败 1", text)
        self.assertIn("均值 100.0s", text)

    def test_status_distribution_alone_undetermined_does_not_affect_active_users(self) -> None:
        inputs = _all_determined_inputs(active_users=ActiveUserStats(3, (("1 条", 3),)))
        inputs = DailyReportInputs(
            **{**inputs.__dict__, "status_distribution": Section.undetermined("查询超时")}
        )
        text = self._render(inputs)
        self.assertIn("任务结果分布：不可判定（原因：查询超时）", text)
        self.assertIn("活跃用户：3 人", text)

    def test_latency_alone_undetermined_does_not_affect_delivery_outcome(self) -> None:
        inputs = _all_determined_inputs(
            delivery_outcome=DeliveryOutcomeStats(delivered_card=9, delivered_fallback_text=0, expired=0, pending=0)
        )
        inputs = DailyReportInputs(**{**inputs.__dict__, "latency": Section.undetermined("连接被拒绝")})
        text = self._render(inputs)
        self.assertIn("时延分布", text)
        self.assertIn("不可判定（原因：连接被拒绝）", text)
        self.assertIn("成功(卡片) 9", text)

    def test_delivery_outcome_alone_undetermined(self) -> None:
        inputs = _all_determined_inputs(active_users=ActiveUserStats(2, (("1 条", 2),)))
        inputs = DailyReportInputs(
            **{**inputs.__dict__, "delivery_outcome": Section.undetermined("表不可访问")}
        )
        text = self._render(inputs)
        self.assertIn("投递结果分布：不可判定（原因：表不可访问）", text)
        self.assertIn("活跃用户：2 人", text)

    def test_call_count_baseline_is_always_undetermined_by_construction(self) -> None:
        """MCP 调用次数对照（#296 基线）当前架构下恒为不可判定——这一条与
        `denied_count`/`resource_usage` 不同：批次 4 只给后两者接了真实落库，
        调用次数对照仍然没有任何数据源，见 `core/daily_report.py` 模块文档。"""

        inputs = _all_determined_inputs()
        text = self._render(inputs)
        self.assertIn(CALL_COUNT_BASELINE_UNAVAILABLE_REASON, text)

    def test_resource_usage_and_denied_count_render_real_aggregates_when_determined(self) -> None:
        """Issue #303/#304 批次 4：两段脱离「恒不可判定」，`Section.of(...)` 时
        渲染真实数字，不再是固定的不可判定文案。"""

        inputs = _all_determined_inputs(
            denied_count=PartialCount(total=3, covered_tasks=10, uncovered_tasks=0),
            resource_usage=TokenUsageStats(
                input_tokens=1000,
                output_tokens=200,
                cache_creation_input_tokens=50,
                cache_read_input_tokens=25,
                covered_tasks=10,
                uncovered_tasks=0,
            ),
        )
        text = self._render(inputs)
        self.assertIn("工具调用拒绝计数（PreToolUse 拒绝）：3 次", text)
        self.assertNotIn("工具调用拒绝计数（PreToolUse 拒绝）：不可判定", text)
        self.assertIn("token 用量：input=1000，output=200，cache_creation=50，cache_read=25", text)
        self.assertNotIn("token 用量：不可判定", text)
        # 完全覆盖（uncovered_tasks=0）时不画蛇添足附加覆盖度说明。
        self.assertNotIn("因字段缺失未计入", text)

    def test_partial_coverage_is_shown_explicitly_not_silently_zeroed(self) -> None:
        """窗口内一部分任务的字段是 NULL 时，覆盖度必须显式出现在正文里——不能
        让读者误以为渲染出的总数就是窗口内全部任务的准确总和（模块文档「NULL
        行归入不可判定、不静默计为零」）。"""

        inputs = _all_determined_inputs(
            denied_count=PartialCount(total=7, covered_tasks=8, uncovered_tasks=2),
            resource_usage=TokenUsageStats(
                input_tokens=500,
                output_tokens=100,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
                covered_tasks=8,
                uncovered_tasks=2,
            ),
        )
        text = self._render(inputs)
        self.assertIn("工具调用拒绝计数（PreToolUse 拒绝）：7 次（覆盖 8/10 个任务；另有 2 个任务因字段缺失未计入，不计为零）", text)
        self.assertIn("token 用量：input=500，output=100，cache_creation=0，cache_read=0（覆盖 8/10 个任务；另有 2 个任务因字段缺失未计入，不计为零）", text)

    def test_resource_usage_and_denied_count_undetermined_when_window_is_entirely_null(self) -> None:
        """窗口内有任务，但这两个字段全部是 NULL（例如全是迁移 0070 之前产生的
        历史任务）——纯函数返回 ``None``，调用方据此构造 `Section.undetermined`，
        正文必须显式说明，不能悄悄渲染成 0。"""

        inputs = _all_determined_inputs()
        inputs = DailyReportInputs(
            **{
                **inputs.__dict__,
                "denied_count": Section.undetermined(DENIED_COUNT_ALL_NULL_REASON),
                "resource_usage": Section.undetermined(RESOURCE_USAGE_ALL_NULL_REASON),
            }
        )
        text = self._render(inputs)
        self.assertIn(DENIED_COUNT_ALL_NULL_REASON, text)
        self.assertIn(RESOURCE_USAGE_ALL_NULL_REASON, text)


class DeniedCountAndResourceUsageBuildTests(unittest.TestCase):
    """`build_denied_count_stats`/`build_token_usage_stats` 纯函数断言：NULL 行
    不静默计零、全 NULL 时返回 ``None``、无任务时是合法的确定零。"""

    def test_no_tasks_in_window_is_a_determined_zero_not_undetermined(self) -> None:
        stats = build_denied_count_stats(covered_tasks=0, uncovered_tasks=0, total=0)
        self.assertEqual(stats, PartialCount(total=0, covered_tasks=0, uncovered_tasks=0))

    def test_all_null_returns_none_so_the_caller_can_mark_the_section_undetermined(self) -> None:
        self.assertIsNone(build_denied_count_stats(covered_tasks=0, uncovered_tasks=5, total=0))

    def test_partial_coverage_only_sums_the_covered_tasks(self) -> None:
        stats = build_denied_count_stats(covered_tasks=3, uncovered_tasks=2, total=9)
        self.assertEqual(stats, PartialCount(total=9, covered_tasks=3, uncovered_tasks=2))

    def test_token_usage_no_tasks_in_window_is_a_determined_zero(self) -> None:
        stats = build_token_usage_stats(
            covered_tasks=0,
            uncovered_tasks=0,
            input_tokens=0,
            output_tokens=0,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )
        self.assertEqual(
            stats,
            TokenUsageStats(
                input_tokens=0,
                output_tokens=0,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
                covered_tasks=0,
                uncovered_tasks=0,
            ),
        )

    def test_token_usage_all_null_returns_none(self) -> None:
        self.assertIsNone(
            build_token_usage_stats(
                covered_tasks=0,
                uncovered_tasks=4,
                input_tokens=0,
                output_tokens=0,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            )
        )


class RenderContentSafetyTests(unittest.TestCase):
    """`V-通报-05`：正文不含用户对话原文、姓名、工号、邮箱或任何形式的用户标识。"""

    def test_no_forbidden_values_appear_in_a_fully_populated_report(self) -> None:
        """被动侧：干净输入（合法的 reason_code）渲染出的报告不含任何敏感值。"""

        top = (FailureReasonCount("session_failed", 1),)
        inputs = _all_determined_inputs(
            active_users=ActiveUserStats(3, (("1 条", 3),)),
            status_distribution=StatusDistribution(3, 1, 0, 0, 0),
            failure_top=top,
            guard_triggered=1,
            latency=LatencyStats(3, 200.0, 200.0, 200.0, 200.0),
            delivery_outcome=DeliveryOutcomeStats(3, 0, 0, 0),
        )
        throttled = (ThrottledFailureLine("session_failed", 1, streak_days=1, throttled=False),)
        text = render_daily_report(inputs, throttled_failure_lines=throttled)
        for forbidden in FORBIDDEN_VALUES:
            self.assertNotIn(forbidden, text)

    #: 白名单是**形状**校验（`^[a-z0-9_]{1,64}$`），不是语义校验——它能挡住的是
    #: "含有形状之外字符"的泄露（大写字母、CJK、``@``、``.``、空格……），挡不住
    #: 一个恰好通篇小写字母/数字/下划线、没有其他字符混入的标识符本身。
    #: `FORBIDDEN_VALUES` 里除 `"ou_person_0001"` 外的四个样例都带着形状之外的
    #: 字符（CJK、大写字母或 `@`/`.`），因此都会被挡住；`"ou_person_0001"` 恰好
    #: 全部落在白名单字符集内，是这条防线**已知且已在测试里诚实记录**的盲区——
    #: 见 `test_a_bare_lowercase_open_id_shaped_value_is_not_caught_by_the_shape_
    #: whitelist`。真实飞书 open_id 是否会恰好取到这个形状不由本模块控制。
    HOSTILE_VALUES_CAUGHT_BY_THE_SHAPE_WHITELIST = tuple(
        value for value in FORBIDDEN_VALUES if value != "ou_person_0001"
    )

    def test_a_hostile_reason_code_is_redacted_to_other(self) -> None:
        """主动侧（opus 批量审查 P1 修复）：`reason_code` 结构上来自
        `task.error_kind`，但本模块拿到的只是调用方传入的字符串，不持有任何
        保证它恒为蛇形小写枚举值的类型约束。这里主动把每一个带有"形状之外
        字符"的敏感样例（姓名/工号/邮箱/内部 ULID）当作分类逻辑意外产生的
        `reason_code` 注入进去，证明渲染层的形状白名单真的挡住了它——不出现在
        正文里，且被换成了 `other`，不是被静默丢弃成空白。"""

        for hostile in self.HOSTILE_VALUES_CAUGHT_BY_THE_SHAPE_WHITELIST:
            with self.subTest(hostile=hostile):
                top = (FailureReasonCount(hostile, 1),)
                inputs = _all_determined_inputs(failure_top=top)

                text = render_daily_report(inputs)

                self.assertNotIn(hostile, text)
                self.assertIn("- other：1 次", text)

    def test_a_hostile_reason_code_is_redacted_even_when_throttled(self) -> None:
        """同一条白名单必须覆盖节流分支——不能只顶住未节流那一条渲染路径。"""

        hostile = self.HOSTILE_VALUES_CAUGHT_BY_THE_SHAPE_WHITELIST[0]
        top = (FailureReasonCount(hostile, 3),)
        inputs = _all_determined_inputs(failure_top=top)
        throttled = (ThrottledFailureLine(hostile, 3, streak_days=8, throttled=True),)

        text = render_daily_report(inputs, throttled_failure_lines=throttled)

        self.assertNotIn(hostile, text)
        self.assertIn("- other：3 次（连续第 8 天，已节流）", text)

    def test_a_bare_lowercase_open_id_shaped_value_is_not_caught_by_the_shape_whitelist(
        self,
    ) -> None:
        """诚实记录已知盲区（不是回归）：白名单只校验**形状**，通篇小写字母/
        数字/下划线的标识符与合法的蛇形小写 reason_code（如
        ``"session_failed"``）在字符集层面完全无法区分。样例刻意使用**真实
        飞书 open_id 的标准形态**（``ou_`` + 32 位小写十六进制）——它恰好整体
        落在盲区里，即这道纵深对「它最想挡的那一类标识」覆盖率为零。这条用例
        证伪"形状白名单能挡住任何 open_id 泄露"这个过强说法，只留下可诚实
        复述的结论："挡住带大写/CJK/标点等形状外字符的泄露；对全小写标识符
        （含真实形态 open_id）无效"。当前全仓 ``task.error_kind`` 写入点均为
        固定蛇形小写字面量、无动态来源（见模块文档），本用例只记录纵深防线
        自身边界，不代表当前存在真实泄露路径；若引入动态 error_kind，先改
        枚举白名单。"""

        hostile = "ou_a1b2c3d4e5f60718293a4b5c6d7e8f90"
        top = (FailureReasonCount(hostile, 1),)
        inputs = _all_determined_inputs(failure_top=top)

        text = render_daily_report(inputs)

        self.assertIn(hostile, text)

    def test_the_report_contains_no_clickable_or_callback_entry(self) -> None:
        """管理群是通知面不是操作面，与花名册日报 `V-花名册-24` 同一条纪律：
        正文不含任何看起来像链接或按钮标记的片段。"""

        inputs = _all_determined_inputs()
        text = render_daily_report(inputs)
        for marker in ("http://", "https://", "[button", "<button"):
            self.assertNotIn(marker, text)


class WindowHeaderTests(unittest.TestCase):
    """`V-通报-06`：正文同时标注 UTC 与北京时间的统计窗口起止。"""

    def test_both_timezones_are_present_with_full_dates_on_both_ends(self) -> None:
        inputs = _all_determined_inputs()
        text = render_daily_report(inputs)
        self.assertIn("2026-08-23 00:00–2026-08-24 00:00（UTC）", text)
        self.assertIn("2026-08-23 08:00–2026-08-24 08:00（北京时间）", text)


class DeliveryOutcomeIndependentWindowTests(unittest.TestCase):
    """投递结果段用一个独立、更早的窗口（opus 批量审查 P2 修复）：`[D-2, D-1)`，
    比其余段落的 `[D-1, D)` 整整早一天，且正文必须单独标注这段窗口。
    """

    def test_the_delivery_window_is_exactly_one_day_before_the_main_window(self) -> None:
        """钉住窗口边界：投递结果段的窗口终点等于其余段落窗口的起点（首尾相接，
        不重叠、不留缺口），起点比终点早整整一天。"""

        self.assertEqual(DELIVERY_WINDOW_END, WINDOW_START)
        self.assertEqual(WINDOW_START - DELIVERY_WINDOW_START, timedelta(days=1))
        self.assertEqual(WINDOW_END - WINDOW_START, timedelta(days=1))

    def test_the_delivery_section_states_its_own_window_distinct_from_the_header(self) -> None:
        inputs = _all_determined_inputs()
        text = render_daily_report(inputs)

        # 页首窗口（其余六段共用）与投递结果段窗口必须都出现，且是两个不同的
        # 日期区间——不能让读者以为整份报告只有一个统计窗口。
        self.assertIn("统计窗口：2026-08-23 00:00–2026-08-24 00:00（UTC）", text)
        self.assertIn("本段窗口：2026-08-22 00:00–2026-08-23 00:00（UTC）", text)
        self.assertIn("比上方统计窗口早一天", text)

    def test_a_non_zero_expired_count_renders_correctly(self) -> None:
        """正向证据：独立窗口不是只为了"看起来更早"，是为了让「过期」这一桶真的
        可能非零——本用例直接注入一个非零过期数，证明渲染路径把它如实透出。"""

        inputs = _all_determined_inputs(
            delivery_outcome=DeliveryOutcomeStats(
                delivered_card=5, delivered_fallback_text=1, expired=2, pending=0
            )
        )

        text = render_daily_report(inputs)

        self.assertIn("过期 2", text)

    def test_an_undetermined_delivery_section_still_states_the_window_it_asked_for(
        self,
    ) -> None:
        """即使本轮查询失败（不可判定），也必须说明"本来打算问哪个窗口"——
        `delivery_window_start`/`end` 与 `Section` 是否可判定相互独立。"""

        inputs = _all_determined_inputs()
        inputs = DailyReportInputs(
            **{**inputs.__dict__, "delivery_outcome": Section.undetermined("查询超时")}
        )

        text = render_daily_report(inputs)

        self.assertIn("投递结果分布：不可判定（原因：查询超时）", text)
        self.assertIn("本段窗口：2026-08-22 00:00–2026-08-23 00:00（UTC）", text)


class MetricCoverageGapBuildTests(unittest.TestCase):
    """:func:`build_metric_coverage_gap` 纯集合运算（Issue #320 并入项）：有差/无差。"""

    def test_a_metric_present_in_mcp_but_absent_from_the_mapping_is_reported(self) -> None:
        gap = build_metric_coverage_gap(
            mcp_metric_ids=("sub_new_count", "brand_new_metric"),
            mapped_metric_ids=("sub_new_count", "exchange_rate"),
        )

        self.assertIsNotNone(gap)
        assert gap is not None  # narrow for type checkers
        self.assertEqual(gap.uncovered_metric_ids, ("brand_new_metric",))

    def test_full_coverage_reports_no_difference(self) -> None:
        gap = build_metric_coverage_gap(
            mcp_metric_ids=("sub_new_count", "exchange_rate"),
            mapped_metric_ids=("exchange_rate", "sub_new_count", "vat_rate"),
        )

        self.assertIsNone(gap, "映射表覆盖面是 MCP 目录的超集时，无差异不报")

    def test_an_empty_mcp_catalog_reports_no_difference(self) -> None:
        self.assertIsNone(build_metric_coverage_gap(mcp_metric_ids=(), mapped_metric_ids=("exchange_rate",)))

    def test_the_result_is_sorted_and_deduplicated(self) -> None:
        gap = build_metric_coverage_gap(
            mcp_metric_ids=("z_metric", "a_metric", "z_metric", "a_metric"),
            mapped_metric_ids=(),
        )

        self.assertIsNotNone(gap)
        assert gap is not None
        self.assertEqual(gap.uncovered_metric_ids, ("a_metric", "z_metric"))

    def test_comparison_is_verbatim_no_case_or_width_folding(self) -> None:
        """与 ``publish_row.py`` 的「零归一」纪律一致：``OTT`` 与 ``ott`` 是不同的
        指标 ID，本函数不得替使用方做任何大小写/全半角折叠。"""

        gap = build_metric_coverage_gap(mcp_metric_ids=("OTT",), mapped_metric_ids=("ott",))

        self.assertIsNotNone(gap)
        assert gap is not None
        self.assertEqual(gap.uncovered_metric_ids, ("OTT",))


class MetricCoverageGapRenderTests(unittest.TestCase):
    """待分配段的渲染三态：未接线不出现、接线且无差异不出现、有差异才出现；
    以及查询失败时的「不可判定」提示（Issue #320 并入项）。
    """

    def test_unwired_section_produces_no_text_at_all(self) -> None:
        """``metric_coverage_gap`` 缺省为 ``None``（未接线）：正文完全不含「待分配」
        字样——不是"检查了，没有问题"，是"这一轮根本没有做这项检查"。
        """

        inputs = _all_determined_inputs()

        text = render_daily_report(inputs)

        self.assertNotIn("待分配", text)

    def test_wired_but_no_gap_produces_no_text(self) -> None:
        """接线了、真查了、两边一致：无差异不报，正文同样不含「待分配」字样。"""

        inputs = _all_determined_inputs()
        inputs = DailyReportInputs(
            **{**inputs.__dict__, "metric_coverage_gap": Section.of(None)}
        )

        text = render_daily_report(inputs)

        self.assertNotIn("待分配", text)

    def test_a_detected_gap_appears_in_the_pending_assignment_section(self) -> None:
        inputs = _all_determined_inputs()
        inputs = DailyReportInputs(
            **{
                **inputs.__dict__,
                "metric_coverage_gap": Section.of(
                    MetricCoverageGap(uncovered_metric_ids=("brand_new_metric", "another_new_metric"))
                ),
            }
        )

        text = render_daily_report(inputs)

        self.assertIn("待分配", text)
        self.assertIn("brand_new_metric", text)
        self.assertIn("another_new_metric", text)
        self.assertIn("company_function_metric_map.toml", text)

    def test_a_failed_check_is_shown_as_undetermined_not_silently_skipped(self) -> None:
        inputs = _all_determined_inputs()
        inputs = DailyReportInputs(
            **{
                **inputs.__dict__,
                "metric_coverage_gap": Section.undetermined("问数 MCP 查询超时"),
            }
        )

        text = render_daily_report(inputs)

        self.assertIn("待分配", text)
        self.assertIn("不可判定（原因：问数 MCP 查询超时）", text)


class LocalOverrideActivityBuildTests(unittest.TestCase):
    """:func:`build_local_override_activity` 纯集合运算（Issue #319 S-P-1c）：
    有活动/无差异。"""

    def test_any_today_activity_is_reported(self) -> None:
        activity = build_local_override_activity(
            granted_today=2,
            suppressed_today=1,
            revoked_today=0,
            active_grant_total=5,
            active_suppress_total=3,
            affected_user_count=6,
        )

        self.assertIsNotNone(activity)
        assert activity is not None
        self.assertEqual(activity.granted_today, 2)
        self.assertEqual(activity.suppressed_today, 1)
        self.assertEqual(activity.revoked_today, 0)
        self.assertEqual(activity.active_grant_total, 5)
        self.assertEqual(activity.active_suppress_total, 3)
        self.assertEqual(activity.affected_user_count, 6)

    def test_only_a_revocation_today_is_still_reported(self) -> None:
        """哪怕只有收回、没有新增，只要不是全零就要报——「无差异」专指全部为零。"""

        activity = build_local_override_activity(
            granted_today=0,
            suppressed_today=0,
            revoked_today=1,
            active_grant_total=0,
            active_suppress_total=0,
            affected_user_count=0,
        )

        self.assertIsNotNone(activity)

    def test_only_a_nonzero_active_total_with_no_activity_today_is_still_reported(self) -> None:
        """今天没有任何新增/收回，但历史上还有生效条目在——同样不是「无差异」。"""

        activity = build_local_override_activity(
            granted_today=0,
            suppressed_today=0,
            revoked_today=0,
            active_grant_total=2,
            active_suppress_total=0,
            affected_user_count=1,
        )

        self.assertIsNotNone(activity)

    def test_all_zero_reports_no_difference(self) -> None:
        activity = build_local_override_activity(
            granted_today=0,
            suppressed_today=0,
            revoked_today=0,
            active_grant_total=0,
            active_suppress_total=0,
            affected_user_count=0,
        )

        self.assertIsNone(activity, "当日零活动且当前生效总数为零时，无差异不报")


class LocalOverrideActivityRenderTests(unittest.TestCase):
    """本地权限覆盖活动段的渲染三态：未接线不出现、接线且无差异不出现、有活动
    才出现；以及查询失败时的「不可判定」提示（Issue #319 S-P-1c）。
    """

    def test_unwired_section_produces_no_text_at_all(self) -> None:
        """``local_override_activity`` 缺省为 ``None``（未接线）：正文完全不含
        「本地权限覆盖活动」字样——不是"检查了，没有问题"，是"这一轮根本没有
        做这项检查"。
        """

        inputs = _all_determined_inputs()

        text = render_daily_report(inputs)

        self.assertNotIn("本地权限覆盖活动", text)

    def test_wired_but_no_activity_produces_no_text(self) -> None:
        """接线了、真查了、当日零活动且当前生效总数为零：无差异不报。"""

        inputs = _all_determined_inputs()
        inputs = DailyReportInputs(
            **{**inputs.__dict__, "local_override_activity": Section.of(None)}
        )

        text = render_daily_report(inputs)

        self.assertNotIn("本地权限覆盖活动", text)

    def test_activity_appears_with_correct_counts(self) -> None:
        inputs = _all_determined_inputs()
        inputs = DailyReportInputs(
            **{
                **inputs.__dict__,
                "local_override_activity": Section.of(
                    LocalOverrideActivity(
                        granted_today=2,
                        suppressed_today=1,
                        revoked_today=1,
                        active_grant_total=5,
                        active_suppress_total=3,
                        affected_user_count=6,
                    )
                ),
            }
        )

        text = render_daily_report(inputs)

        self.assertIn("本地权限覆盖活动", text)
        self.assertIn("授权 2 笔", text)
        self.assertIn("抑制 1 笔", text)
        self.assertIn("收回 1 笔", text)
        self.assertIn("当前登记 授权 5 条", text)
        self.assertIn("抑制 3 条", text)
        self.assertIn("涉及 6 位用户", text)
        # P1-2（独立审查坐实并修复）：现网每日重算未跑，不得笼统断言「生效」——
        # 正文改用「登记」计数本身，另加一句口径说明区分开通链（已生效）与
        # 重算侧（待每日重算恢复运行），与 `daily_report.py` 模块措辞同步。
        self.assertIn("生效口径：开通链已生效，重算侧待每日重算恢复运行", text)
        self.assertNotIn("当前生效", text)
        # 正文只含计数，不含任何形式的用户标识、公司 ID 或指标名——
        # `LocalOverrideActivity` 本身就只携带六个整数字段，结构上没有承载
        # 这些值的字段，这里额外核对渲染结果不引入任何形状可疑的片段。
        for forbidden in FORBIDDEN_VALUES:
            self.assertNotIn(forbidden, text)

    def test_a_failed_check_is_shown_as_undetermined_not_silently_skipped(self) -> None:
        inputs = _all_determined_inputs()
        inputs = DailyReportInputs(
            **{
                **inputs.__dict__,
                "local_override_activity": Section.undetermined("数据库查询超时"),
            }
        )

        text = render_daily_report(inputs)

        self.assertIn("本地权限覆盖活动", text)
        self.assertIn("不可判定（原因：数据库查询超时）", text)


if __name__ == "__main__":
    unittest.main()
