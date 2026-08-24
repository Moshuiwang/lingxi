"""`core/daily_report.py` 的纯函数断言（Issue #303 S-O-01）。

覆盖新增断言：V-通报-01（每段可独立不可判定）、V-通报-02（不可判定必须显式呈现，
不得渲染成零或省略）、V-通报-04（节流语义）、V-通报-05（正文不含用户标识/原文）、
V-通报-06（UTC+北京双时区标注）。真库聚合断言在
`tests/test_postgres_daily_report.py`；职责层调度、判重与送达失败断言在
`tests/test_daily_report_duty.py`。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from lingxi.core.daily_report import (
    CALL_COUNT_BASELINE_UNAVAILABLE_REASON,
    DENIED_COUNT_UNAVAILABLE_REASON,
    RESOURCE_USAGE_UNAVAILABLE_REASON,
    ActiveUserStats,
    DailyReportInputs,
    DeliveryOutcomeStats,
    FailureReasonCount,
    LatencyStats,
    Section,
    StatusDistribution,
    ThrottledFailureLine,
    apply_repeat_throttle,
    build_active_user_stats,
    build_delivery_outcome,
    build_failure_top,
    build_guard_triggered_count,
    build_latency_stats,
    build_status_distribution,
    render_daily_report,
)

WINDOW_START = datetime(2026, 8, 23, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 8, 24, tzinfo=timezone.utc)

# 用于「正文绝不含用户标识/原文」断言的固定敏感值：真实姓名、工号、邮箱、飞书标识、
# 内部 ULID 各一个，一个都不许出现在渲染结果里。
FORBIDDEN_VALUES = ("张三", "E1001", "zhangsan@example.com", "ou_person_0001", "usr_01JQZX3M5N7P9R1T3V5W7Y9A0B")


def _all_determined_inputs(
    *,
    active_users: ActiveUserStats | None = None,
    status_distribution: StatusDistribution | None = None,
    failure_top: tuple[FailureReasonCount, ...] = (),
    guard_triggered: int = 0,
    latency: LatencyStats | None = None,
    delivery_outcome: DeliveryOutcomeStats | None = None,
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
        denied_count=Section.undetermined(DENIED_COUNT_UNAVAILABLE_REASON),
        latency=Section.of(latency),
        resource_usage=Section.undetermined(RESOURCE_USAGE_UNAVAILABLE_REASON),
        delivery_outcome=Section.of(
            delivery_outcome or DeliveryOutcomeStats(delivered_card=0, delivered_fallback_text=0, expired=0, pending=0)
        ),
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

    def test_resource_usage_and_denied_count_are_always_undetermined_by_construction(self) -> None:
        """当前架构下这两段恒为不可判定，是产品架构的真实现状——不是测试构造出的
        假设，见 `core/daily_report.py` 模块文档「数据从哪来」一节。"""

        inputs = _all_determined_inputs()
        text = self._render(inputs)
        self.assertIn(RESOURCE_USAGE_UNAVAILABLE_REASON, text)
        self.assertIn(DENIED_COUNT_UNAVAILABLE_REASON, text)
        self.assertIn(CALL_COUNT_BASELINE_UNAVAILABLE_REASON, text)


class RenderContentSafetyTests(unittest.TestCase):
    """`V-通报-05`：正文不含用户对话原文、姓名、工号、邮箱或任何形式的用户标识。"""

    def test_no_forbidden_values_appear_in_a_fully_populated_report(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
