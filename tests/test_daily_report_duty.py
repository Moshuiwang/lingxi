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
    """六段数据源的可注入替身：任意一段可以配置成抛异常，模拟「这段数据源没了」。

    ``guard_denied_stats``/``token_usage_stats_value`` 默认给出一份**全部覆盖、
    零拒绝、零用量**的确定结果（``uncovered_tasks=0``）——Issue #303/#304 批次 4
    起这两段不再恒为不可判定，默认值因此也不该再是"没有这个数据源"，而是"这个
    数据源正常答完，答案恰好是零"，与其余四段的既有默认值同一条纪律。
    """

    def __init__(
        self,
        *,
        active_counts: tuple[int, ...] = (1, 2, 3),
        outcome_rows: tuple = (("succeeded", None, 5), ("failed", "session_failed", 1)),
        durations: tuple[float, ...] = (100.0, 200.0),
        delivery_rows: tuple = (("card", True, False, 5),),
        guard_denied_stats: tuple[int, int, int] = (0, 0, 0),
        token_usage_stats_value: tuple[int, int, int, int, int, int] = (0, 0, 0, 0, 0, 0),
        raise_on: str | None = None,
        error: Exception | None = None,
    ) -> None:
        self._active_counts = active_counts
        self._outcome_rows = outcome_rows
        self._durations = durations
        self._delivery_rows = delivery_rows
        self._guard_denied_stats = guard_denied_stats
        self._token_usage_stats = token_usage_stats_value
        self._raise_on = raise_on
        self._error = error or RuntimeError("模拟数据源故障")
        self.calls: dict[str, list[tuple[datetime, datetime]]] = {
            "active_user_task_counts": [],
            "task_outcomes": [],
            "task_durations_seconds": [],
            "delivery_outcomes": [],
            "guard_denied_count_stats": [],
            "token_usage_stats": [],
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

    def guard_denied_count_stats(self, *, window_start: datetime, window_end: datetime):
        self.calls["guard_denied_count_stats"].append((window_start, window_end))
        self._maybe_raise("denied_count")
        return self._guard_denied_stats

    def token_usage_stats(self, *, window_start: datetime, window_end: datetime):
        self.calls["token_usage_stats"].append((window_start, window_end))
        self._maybe_raise("resource_usage")
        return self._token_usage_stats


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

    def test_three_of_four_sources_receive_the_same_window(self) -> None:
        """`active_user_task_counts`/`task_outcomes`/`task_durations_seconds`
        三段共用同一个窗口；`delivery_outcomes` 独立用一个更早的窗口——见下一条
        用例（opus 批量审查 P2 修复，`core/daily_report.py` 模块文档「投递结果段
        为什么用一个独立、更早的窗口」）。"""

        duty, parts = build_duty()
        duty.run_once()
        calls = parts["source"].calls
        windows = {
            name: calls[name][0]
            for name in ("active_user_task_counts", "task_outcomes", "task_durations_seconds")
        }
        self.assertEqual(len(set(windows.values())), 1, "三段数据源必须使用同一个统计窗口")

    def test_delivery_outcomes_receives_an_independent_window_one_day_earlier(self) -> None:
        duty, parts = build_duty()
        duty.run_once()
        calls = parts["source"].calls
        main_window_start, _main_window_end = calls["task_outcomes"][0]
        delivery_window_start, delivery_window_end = calls["delivery_outcomes"][0]

        # 首尾相接：投递结果窗口的终点正好是其余段落窗口的起点，整整早一天。
        self.assertEqual(delivery_window_end, main_window_start)
        self.assertEqual(main_window_start - delivery_window_start, timedelta(days=1))


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

    def test_resource_usage_and_denied_count_render_real_numbers_when_the_source_has_data(
        self,
    ) -> None:
        """Issue #303/#304 批次 4：两段脱离「恒不可判定」——数据源答得出真实数字
        时，正文渲染这两段的真实聚合，不再是固定的不可判定文案。"""

        duty, _ = build_duty(
            source=FakeSource(guard_denied_stats=(10, 0, 4), token_usage_stats_value=(10, 0, 1000, 200, 0, 0))
        )
        text = duty.run_once()
        assert text is not None
        self.assertIn("工具调用拒绝计数（PreToolUse 拒绝）：4 次", text)
        self.assertIn("token 用量：input=1000，output=200", text)

    def test_resource_usage_and_denied_count_source_failing_only_marks_those_sections(
        self,
    ) -> None:
        """`V-通报-01` 对这两段的延伸：数据源查询本身失败时只标这一段不可判定，
        不拖累其余段落——与 active_users/task_outcomes/delivery_outcome 同一条
        `_fetch` 纪律。"""

        duty, parts = build_duty(source=FakeSource(raise_on="denied_count"))
        text = duty.run_once()
        assert text is not None
        self.assertIn("工具调用拒绝计数（PreToolUse 拒绝）：不可判定", text)
        # token 用量这一段独立查询，不受 denied_count 失败影响；默认夹具是确定的零。
        self.assertIn("token 用量：input=0，output=0", text)
        self.assertIn("活跃用户：", text)
        failed_sections = [
            fields["section"] for fields in parts["audit"].fields_for("daily_report.section_read_failed")
        ]
        self.assertEqual(failed_sections, ["denied_count"])

    def test_all_null_window_marks_undetermined_with_the_all_null_reason(self) -> None:
        """查询成功，但窗口内的任务在这两个字段上全部是 NULL（例如全是迁移 0070
        之前的历史任务）——与"查询失败"是不同的不可判定原因。"""

        duty, _ = build_duty(
            source=FakeSource(guard_denied_stats=(0, 6, 0), token_usage_stats_value=(0, 6, 0, 0, 0, 0))
        )
        text = duty.run_once()
        assert text is not None
        self.assertIn("工具调用拒绝计数（PreToolUse 拒绝）：不可判定", text)
        self.assertIn("token 用量：不可判定", text)
        # 批次 4 opus 审查 P3-1：原因文案改为白话复述、不再点 Python 常量名
        # （见 core/daily_report.py 的 RESOURCE_USAGE_ALL_NULL_REASON 文档），
        # 断言随之改锚定新文案里的关键短语，不是原来的「全部为 NULL」字面量。
        self.assertIn("全部没有可用数字", text)
        # F5：ALL_NULL 原因必须包含"窗口内任务仍在排队/运行中"这一最常见成因，
        # 不能只列"迁移之前"与"从未真正进入过执行回合"两种。
        self.assertIn("仍在排队或运行中", text)


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


class SendFailureReasonClassificationTests(unittest.TestCase):
    """`daily_report.send_failed` 审计的安全原因分类（opus 批量审查 P3 修复）：
    只看异常类型，不看异常消息文本，供运维快速区分"我们自己的 uuid 预算算错了"
    还是"飞书/网络那一侧的问题"。"""

    def _reason(self, *, error: Exception) -> str:
        duty, parts = build_duty(sender=FakeSender(failures=1, error=error))
        duty.run_once()
        records = [fields for action, fields in parts["audit"].records if action == "daily_report.send_failed"]
        self.assertEqual(len(records), 1)
        return records[0]["reason"]

    def test_a_value_error_is_classified_as_uuid_budget(self) -> None:
        """`delivery_uuid()` 唯一会抛的异常类型——A1 修复的那类 bug。"""

        self.assertEqual(self._reason(error=ValueError("投递去重 ID 超过飞书的 50 字符上限")), "uuid_budget")

    def test_a_feishu_group_message_error_is_classified_as_transport(self) -> None:
        from lingxi.adapters.feishu_group_message import FeishuGroupMessageError

        self.assertEqual(
            self._reason(error=FeishuGroupMessageError("feishu_code_99991663")), "transport"
        )

    def test_an_unrecognized_exception_is_classified_as_other(self) -> None:
        self.assertEqual(self._reason(error=RuntimeError("模拟发送失败")), "other")

    def test_the_reason_field_never_carries_the_exception_message_text(self) -> None:
        """分类只是一个粗粒度类别字符串，异常消息本身（可能带正文片段）不得
        出现在审计字段里的任何一处。"""

        secret_looking_message = "投递到 oc_admin_group_secret 失败：正文片段泄露样例"
        duty, parts = build_duty(sender=FakeSender(failures=1, error=RuntimeError(secret_looking_message)))
        duty.run_once()
        records = [fields for action, fields in parts["audit"].records if action == "daily_report.send_failed"]
        self.assertNotIn(secret_looking_message, repr(records))


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
        # 节流行必须真的更短（opus 批量审查 P3-4 修复），不再带"在榜"与"仅计数
        # 不再展开明细"这类装饰性文字。
        self.assertIn("连续第 8 天，已节流", last_text)

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
