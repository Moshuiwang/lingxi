"""`apps/scheduler/daily_report.py` 的职责层断言（Issue #303 S-O-01）。

覆盖：V-通报-01（挖掉一段数据源→该段不可判定、其余照常，职责层的调度接线）、
V-通报-03（每日一条，同日不重发）、V-通报-04（节流状态只在成功送达后提交）、
V-通报-07（送达失败降级为结构化日志、不静默、下一轮重试、水位不置位）。

纯渲染与聚合断言在 `tests/test_daily_report_render.py`；真库断言在
`tests/test_postgres_daily_report.py`；装配（前置判定、告警接线）断言在
`tests/test_scheduler_daily_report_assembly.py`。
"""

from __future__ import annotations

import threading
import unittest
from datetime import date, datetime, timedelta, timezone

from lingxi.apps.scheduler.daily_report import DailyReportDuty

FAKE_CHAT_ID = "oc_fake_admin_group_for_tests"


class FakeSource:
    """四段数据源的可注入替身：任意一段可以配置成抛异常，模拟「这段数据源没了」。"""

    def __init__(
        self,
        *,
        active_counts: tuple[int, ...] = (1, 2, 3),
        outcome_rows: tuple = (("succeeded", None, 5), ("failed", "session_failed", 1)),
        durations: tuple[float, ...] = (100.0, 200.0),
        delivery_rows: tuple = (("card", True, False, 5),),
        raise_on: str | None = None,
        error: Exception | None = None,
    ) -> None:
        self._active_counts = active_counts
        self._outcome_rows = outcome_rows
        self._durations = durations
        self._delivery_rows = delivery_rows
        self._raise_on = raise_on
        self._error = error or RuntimeError("模拟数据源故障")
        self.calls: dict[str, list[tuple[datetime, datetime]]] = {
            "active_user_task_counts": [],
            "task_outcomes": [],
            "task_durations_seconds": [],
            "delivery_outcomes": [],
        }

    def _maybe_raise(self, name: str) -> None:
        if self._raise_on == name:
            raise self._error

    def active_user_task_counts(self, *, window_start: datetime, window_end: datetime):
        self.calls["active_user_task_counts"].append((window_start, window_end))
        self._maybe_raise("active_users")
        return self._active_counts

    def task_outcomes(self, *, window_start: datetime, window_end: datetime):
        self.calls["task_outcomes"].append((window_start, window_end))
        self._maybe_raise("task_outcomes")
        return self._outcome_rows

    def task_durations_seconds(self, *, window_start: datetime, window_end: datetime):
        self.calls["task_durations_seconds"].append((window_start, window_end))
        self._maybe_raise("latency")
        return self._durations

    def delivery_outcomes(self, *, window_start: datetime, window_end: datetime):
        self.calls["delivery_outcomes"].append((window_start, window_end))
        self._maybe_raise("delivery_outcome")
        return self._delivery_rows


class FakeSender:
    def __init__(self, *, failures: int = 0, error: Exception | None = None) -> None:
        self._failures = failures
        self._error = error
        self.attempts: list[dict[str, str]] = []
        self.payloads: list[dict[str, str]] = []

    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None:
        self.attempts.append({"chat_id": chat_id, "text": text, "dedupe_key": dedupe_key})
        if self._failures > 0:
            self._failures -= 1
            raise self._error or RuntimeError("模拟发送失败")
        self.payloads.append({"chat_id": chat_id, "text": text, "dedupe_key": dedupe_key})


class RecordingAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, object]]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, dict(fields)))

    def actions(self) -> list[str]:
        return [action for action, _ in self.records]

    def fields_for(self, action: str) -> list[dict[str, object]]:
        return [fields for recorded_action, fields in self.records if recorded_action == action]


class FixedClock:
    def __init__(self, start: date = date(2026, 8, 24)) -> None:
        self.today = start

    def __call__(self) -> datetime:
        return datetime(self.today.year, self.today.month, self.today.day, 9, 0, tzinfo=timezone.utc)

    def advance(self, days: int = 1) -> None:
        self.today = self.today + timedelta(days=days)


def build_duty(
    *,
    source: FakeSource | None = None,
    sender: FakeSender | None = None,
    audit: RecordingAudit | None = None,
    clock: FixedClock | None = None,
    stop: threading.Event | None = None,
) -> tuple[DailyReportDuty, dict[str, object]]:
    parts = {
        "source": source or FakeSource(),
        "sender": sender or FakeSender(),
        "audit": audit or RecordingAudit(),
        "clock": clock or FixedClock(),
    }
    duty = DailyReportDuty(
        source=parts["source"],
        sender=parts["sender"],
        audit=parts["audit"],
        chat_id=FAKE_CHAT_ID,
        clock=parts["clock"],
        stop=stop,
    )
    return duty, parts


class OncePerDayTests(unittest.TestCase):
    """`V-通报-03`：每日至多一条，同日不重发。"""

    def test_a_second_run_on_the_same_day_sends_nothing(self) -> None:
        duty, parts = build_duty()
        first = duty.run_once()
        second = duty.run_once()
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(parts["sender"].payloads), 1)

    def test_a_new_utc_day_sends_again(self) -> None:
        duty, parts = build_duty()
        duty.run_once()
        parts["clock"].advance(1)
        second = duty.run_once()
        self.assertIsNotNone(second)
        self.assertEqual(len(parts["sender"].payloads), 2)

    def test_stopping_before_the_first_run_sends_nothing(self) -> None:
        stop = threading.Event()
        stop.set()
        duty, parts = build_duty(stop=stop)
        result = duty.run_once()
        self.assertIsNone(result)
        self.assertEqual(parts["sender"].payloads, [])

    def test_completed_on_reports_none_before_the_first_successful_send(self) -> None:
        duty, _ = build_duty()
        self.assertIsNone(duty.completed_on)
        duty.run_once()
        self.assertEqual(duty.completed_on, date(2026, 8, 24))


class WindowComputationTests(unittest.TestCase):
    """通报窗口固定为「昨天」的完整 UTC 自然日。"""

    def test_the_window_covers_the_full_utc_day_before_today(self) -> None:
        duty, parts = build_duty(clock=FixedClock(date(2026, 8, 24)))
        duty.run_once()
        window_start, window_end = parts["source"].calls["task_outcomes"][0]
        self.assertEqual(window_start, datetime(2026, 8, 23, tzinfo=timezone.utc))
        self.assertEqual(window_end, datetime(2026, 8, 24, tzinfo=timezone.utc))

    def test_all_four_sources_receive_the_same_window(self) -> None:
        duty, parts = build_duty()
        duty.run_once()
        windows = {name: calls[0] for name, calls in parts["source"].calls.items()}
        self.assertEqual(len(set(windows.values())), 1, "四段数据源必须使用同一个统计窗口")


class SectionIndependentFailureTests(unittest.TestCase):
    """`V-通报-01`：挖掉一段数据源，该段显式不可判定，其余段落照常——职责层接线。"""

    def test_active_users_source_failing_only_marks_that_section(self) -> None:
        duty, parts = build_duty(source=FakeSource(raise_on="active_users"))
        text = duty.run_once()
        assert text is not None
        self.assertIn("活跃用户与任务量分布：不可判定", text)
        # 其余段落照常：任务结果分布与投递结果的真实数字原样出现。
        self.assertIn("成功 5，失败 1", text)
        self.assertIn("成功(卡片) 5", text)
        self.assertIn("daily_report.section_read_failed", parts["audit"].actions())
        failed_sections = [
            fields["section"] for fields in parts["audit"].fields_for("daily_report.section_read_failed")
        ]
        self.assertEqual(failed_sections, ["active_users"])

    def test_task_outcomes_source_failing_marks_three_dependent_sections_only(self) -> None:
        """状态分布、失败分类 Top、守卫触发计数三段同出一次查询，一起标不可判定；
        活跃用户与投递结果两段来自独立查询，不受影响。"""

        duty, parts = build_duty(source=FakeSource(raise_on="task_outcomes"))
        text = duty.run_once()
        assert text is not None
        self.assertIn("任务结果分布：不可判定", text)
        self.assertIn("失败分类 Top：不可判定", text)
        self.assertIn("守卫触发计数（超轮数/超时/收尾超时）：不可判定", text)
        self.assertIn("活跃用户：", text)
        self.assertNotIn("活跃用户与任务量分布：不可判定", text)
        self.assertIn("成功(卡片)", text)

    def test_delivery_outcome_source_failing_only_marks_that_section(self) -> None:
        duty, parts = build_duty(source=FakeSource(raise_on="delivery_outcome"))
        text = duty.run_once()
        assert text is not None
        self.assertIn("投递结果分布：不可判定", text)
        self.assertIn("活跃用户：", text)
        self.assertIn("成功 5，失败 1", text)

    def test_all_sources_succeeding_records_no_section_failures(self) -> None:
        duty, parts = build_duty()
        duty.run_once()
        self.assertNotIn("daily_report.section_read_failed", parts["audit"].actions())

    def test_resource_usage_and_denied_count_are_always_undetermined_even_when_everything_else_succeeds(
        self,
    ) -> None:
        duty, _ = build_duty()
        text = duty.run_once()
        assert text is not None
        self.assertIn("token 用量与成本估算：不可判定", text)
        self.assertIn("工具调用拒绝计数（PreToolUse 拒绝）：不可判定", text)


class SendFailureTests(unittest.TestCase):
    """`V-通报-07`：送达失败不静默——降级为结构化审计日志，下一轮重试，水位不置位。"""

    def test_a_send_failure_is_audited_and_does_not_set_the_watermark(self) -> None:
        duty, parts = build_duty(sender=FakeSender(failures=1))
        result = duty.run_once()
        self.assertIsNone(result)
        self.assertIsNone(duty.completed_on)
        self.assertIn("daily_report.send_failed", parts["audit"].actions())

    def test_the_next_round_retries_and_can_succeed(self) -> None:
        duty, parts = build_duty(sender=FakeSender(failures=1))
        duty.run_once()
        result = duty.run_once()
        self.assertIsNotNone(result)
        self.assertEqual(duty.completed_on, date(2026, 8, 24))
        self.assertEqual(len(parts["sender"].payloads), 1)

    def test_retries_within_the_same_day_share_one_dedupe_key(self) -> None:
        duty, parts = build_duty(sender=FakeSender(failures=1))
        duty.run_once()
        duty.run_once()
        keys = {attempt["dedupe_key"] for attempt in parts["sender"].attempts}
        self.assertEqual(keys, {"daily-report:2026-08-24"})

    def test_a_failed_send_does_not_commit_the_throttle_streak(self) -> None:
        """否定断言：送达失败时节流状态不得被提交——下一轮成功发送的应当仍是
        「今天第一次看到这个原因码」，不是延续一次从未真正送达的连续计数。"""

        source = FakeSource(outcome_rows=(("failed", "session_failed", 1),))
        duty, parts = build_duty(source=source, sender=FakeSender(failures=1))
        duty.run_once()
        self.assertEqual(duty._reason_streaks, {})  # noqa: SLF001 - 白盒断言内部未提交
        text = duty.run_once()
        assert text is not None
        self.assertIn("session_failed：1 次", text)
        self.assertNotIn("连续第", text)


class ThrottleAcrossDaysTests(unittest.TestCase):
    """节流状态随成功送达按天推进；由 `apply_repeat_throttle` 的纯函数断言覆盖具体
    数值语义（`tests/test_daily_report_render.py`），这里只验证职责层真的把状态
    跨轮传递下去。"""

    def test_a_reason_code_present_eight_days_running_gets_throttled_on_the_eighth(self) -> None:
        source = FakeSource(outcome_rows=(("failed", "session_failed", 1),))
        clock = FixedClock(date(2026, 8, 1))
        duty, parts = build_duty(source=source, clock=clock)
        last_text = None
        for _ in range(8):
            last_text = duty.run_once()
            clock.advance(1)
        assert last_text is not None
        self.assertIn("连续第 8 天在榜，已节流", last_text)

    def test_a_gap_day_resets_the_streak_across_rounds(self) -> None:
        clock = FixedClock(date(2026, 8, 1))
        present = FakeSource(outcome_rows=(("failed", "session_failed", 1),))
        absent = FakeSource(outcome_rows=())
        duty, parts = build_duty(source=present, clock=clock)
        for _ in range(7):
            duty.run_once()
            clock.advance(1)
        # 第八天该原因码没有出现：连续计数归零。
        duty._source = absent  # noqa: SLF001 - 白盒切换本轮数据源
        duty.run_once()
        clock.advance(1)
        duty._source = present  # noqa: SLF001 - 第二天恢复出现
        text = duty.run_once()
        assert text is not None
        self.assertIn("session_failed：1 次", text)
        self.assertNotIn("连续第", text)


if __name__ == "__main__":
    unittest.main()
