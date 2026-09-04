"""`apps/scheduler/daily_report.py` 的职责层断言（Issue #303 S-O-01；判重水位
持久化与重聚合挪出主线程为 Issue #325）。

覆盖：V-通报-01（挖掉一段数据源→该段不可判定、其余照常，职责层的调度接线）、
V-通报-03（每日一条，同日不重发）、V-通报-04（节流状态只在成功送达后提交）、
V-通报-07（送达失败降级为结构化日志、不静默、下一轮重试、水位不置位）、
V-通报-10（判重水位持久化，跨进程重启不重发同一窗口）、V-通报-11（重聚合耗时
不占用调用方所在的线程）。

纯渲染与聚合断言在 `tests/test_daily_report_render.py`；真库断言在
`tests/test_postgres_daily_report.py`/`tests/test_postgres_daily_report_watermark.py`；
装配（前置判定、告警接线）断言在 `tests/test_scheduler_daily_report_assembly.py`。
"""

from __future__ import annotations

import threading
import time
import unittest
from datetime import UTC, date, datetime, timedelta

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


class BlockingSource(FakeSource):
    """`task_outcomes` 阻塞直到测试放行——用于证明聚合耗时不再占用调用方所在的
    线程（Issue #325，`DailyReportDuty` 类文档「重聚合为什么要挪出主线程」）。
    """

    def __init__(self, *, gate: threading.Event, released: threading.Event, **kwargs) -> None:
        super().__init__(**kwargs)
        self._gate = gate
        self._released = released

    def task_outcomes(self, *, window_start: datetime, window_end: datetime):
        self._gate.wait(timeout=5.0)
        result = super().task_outcomes(window_start=window_start, window_end=window_end)
        self._released.set()
        return result


class FakeWatermark:
    """判重水位的内存替身（Issue #325）——模拟 `daily_report_watermark` 表的行为：
    ``already_sent``/``mark_sent`` 按 ``(report_date, chat_id)`` 判存在，
    ``mark_sent`` 幂等（重复调用不追加第二条记录）。**跨 `DailyReportDuty` 实例
    共享同一个 `FakeWatermark` 正是「模拟进程重启」的手法**——重启在真实世界里
    产生一个全新的进程（因此是全新的 `_completed_on` 内存状态），但读到的是
    同一个数据库；这里用「共享同一个 FakeWatermark、构造第二个 DailyReportDuty
    实例」还原这个形状，不需要真的启动第二个进程。

    ``raise_on_already_sent``/``raise_on_mark_sent``（#325 尾账，opus 批次 5
    审查留痕）：注入一次性异常，模拟这两次库调用各自失败——两者此前都没有被
    `_fetch` 那套单段失败保护包住，抛出会被更上层的通用 except 静默吞掉。异常
    只抛一次（第一次调用），之后恢复正常返回，方便用例断言"下一轮重试能恢复"。
    """

    def __init__(
        self,
        *,
        raise_on_already_sent: Exception | None = None,
        raise_on_mark_sent: Exception | None = None,
    ) -> None:
        self._sent: set[tuple[str, str]] = set()
        self.already_sent_calls: list[tuple[str, str]] = []
        self.mark_sent_calls: list[tuple[str, str]] = []
        self._raise_on_already_sent = raise_on_already_sent
        self._raise_on_mark_sent = raise_on_mark_sent

    def already_sent(self, *, report_date: date, chat_id: str) -> bool:
        key = (report_date.isoformat(), chat_id)
        self.already_sent_calls.append(key)
        if self._raise_on_already_sent is not None:
            error, self._raise_on_already_sent = self._raise_on_already_sent, None
            raise error
        return key in self._sent

    def mark_sent(self, *, report_date: date, chat_id: str) -> None:
        key = (report_date.isoformat(), chat_id)
        self.mark_sent_calls.append(key)
        if self._raise_on_mark_sent is not None:
            error, self._raise_on_mark_sent = self._raise_on_mark_sent, None
            raise error
        self._sent.add(key)


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
        return datetime(self.today.year, self.today.month, self.today.day, 9, 0, tzinfo=UTC)

    def advance(self, days: int = 1) -> None:
        self.today = self.today + timedelta(days=days)


def build_duty(
    *,
    source: FakeSource | None = None,
    watermark: FakeWatermark | None = None,
    sender: FakeSender | None = None,
    audit: RecordingAudit | None = None,
    clock: FixedClock | None = None,
    stop: threading.Event | None = None,
    metric_coverage=None,
    local_override_activity=None,
) -> tuple[DailyReportDuty, dict[str, object]]:
    parts = {
        "source": source or FakeSource(),
        "watermark": watermark or FakeWatermark(),
        "sender": sender or FakeSender(),
        "audit": audit or RecordingAudit(),
        "clock": clock or FixedClock(),
    }
    duty = DailyReportDuty(
        source=parts["source"],
        watermark=parts["watermark"],
        sender=parts["sender"],
        audit=parts["audit"],
        chat_id=FAKE_CHAT_ID,
        clock=parts["clock"],
        stop=stop,
        metric_coverage=metric_coverage,
        local_override_activity=local_override_activity,
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
        self.assertEqual(window_start, datetime(2026, 8, 23, tzinfo=UTC))
        self.assertEqual(window_end, datetime(2026, 8, 24, tzinfo=UTC))

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
            fields["section"]
            for fields in parts["audit"].fields_for("daily_report.section_read_failed")
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
            source=FakeSource(
                guard_denied_stats=(10, 0, 4), token_usage_stats_value=(10, 0, 1000, 200, 0, 0)
            )
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
            fields["section"]
            for fields in parts["audit"].fields_for("daily_report.section_read_failed")
        ]
        self.assertEqual(failed_sections, ["denied_count"])

    def test_all_null_window_marks_undetermined_with_the_all_null_reason(self) -> None:
        """查询成功，但窗口内的任务在这两个字段上全部是 NULL（例如全是迁移 0070
        之前的历史任务）——与"查询失败"是不同的不可判定原因。"""

        duty, _ = build_duty(
            source=FakeSource(
                guard_denied_stats=(0, 6, 0), token_usage_stats_value=(0, 6, 0, 0, 0, 0)
            )
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
        # Trace #469 修复包 B，B-8：「失败分类 Top」的原因码自本批起渲染成
        # 中文显示名（见 core/daily_report.py `_humanize_error_kind`），
        # "session_failed" → "会话执行失败"，本用例只关心节流状态是否正确
        # 提交/归零，断言随之改用渲染后的中文文本。
        self.assertIn("会话执行失败：1 次", text)
        self.assertNotIn("连续第", text)


class SendFailureReasonClassificationTests(unittest.TestCase):
    """`daily_report.send_failed` 审计的安全原因分类（opus 批量审查 P3 修复）：
    只看异常类型，不看异常消息文本，供运维快速区分"我们自己的 uuid 预算算错了"
    还是"飞书/网络那一侧的问题"。"""

    def _reason(self, *, error: Exception) -> str:
        duty, parts = build_duty(sender=FakeSender(failures=1, error=error))
        duty.run_once()
        records = [
            fields
            for action, fields in parts["audit"].records
            if action == "daily_report.send_failed"
        ]
        self.assertEqual(len(records), 1)
        return records[0]["reason"]

    def test_a_value_error_is_classified_as_uuid_budget(self) -> None:
        """`delivery_uuid()` 唯一会抛的异常类型——A1 修复的那类 bug。"""

        self.assertEqual(
            self._reason(error=ValueError("投递去重 ID 超过飞书的 50 字符上限")), "uuid_budget"
        )

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
        duty, parts = build_duty(
            sender=FakeSender(failures=1, error=RuntimeError(secret_looking_message))
        )
        duty.run_once()
        records = [
            fields
            for action, fields in parts["audit"].records
            if action == "daily_report.send_failed"
        ]
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
        # Trace #469 修复包 B，B-8：「失败分类 Top」的原因码自本批起渲染成
        # 中文显示名（见 core/daily_report.py `_humanize_error_kind`），
        # "session_failed" → "会话执行失败"，本用例只关心节流状态是否正确
        # 提交/归零，断言随之改用渲染后的中文文本。
        self.assertIn("会话执行失败：1 次", text)
        self.assertNotIn("连续第", text)


class PersistedWatermarkTests(unittest.TestCase):
    """`V-通报-10`（Issue #325）：判重水位持久化，跨进程重启不重发同一窗口的
    通报。管理群实测坐实的形状——scheduler 每次重启都把 `_completed_on` 清零，
    同一窗口被重新判定成「还没发」。这里用「共享同一个 `FakeWatermark`、构造第二
    个 `DailyReportDuty` 实例」模拟重启（见 `FakeWatermark` 的文档字符串）。
    """

    def test_a_fresh_instance_sharing_the_watermark_does_not_resend(self) -> None:
        watermark = FakeWatermark()
        sender = FakeSender()
        audit = RecordingAudit()
        clock = FixedClock()
        first_duty, _ = build_duty(watermark=watermark, sender=sender, audit=audit, clock=clock)
        first_result = first_duty.run_once()
        self.assertIsNotNone(first_result)
        self.assertEqual(len(sender.payloads), 1)

        # "重启"：全新实例，进程内存里的 _completed_on 是 None，但水位是共享的
        # 持久存储——这正是重启后新进程读到旧进程写的水位这件事的还原。
        restarted_duty, _ = build_duty(watermark=watermark, sender=sender, audit=audit, clock=clock)
        self.assertIsNone(restarted_duty.completed_on)

        second_result = restarted_duty.run_once()

        self.assertIsNone(second_result)
        self.assertEqual(len(sender.payloads), 1, "重启后的新实例不得重发同一窗口的通报")
        self.assertEqual(restarted_duty.completed_on, date(2026, 8, 24))

    def test_four_simulated_restarts_within_the_same_window_send_exactly_once(self) -> None:
        """对应实测形状：2026-08-25 单日同窗口因多次部署重启收到四条通报——这里
        用四个共享同一份持久水位的独立实例模拟四次"重启"，断言恰一次发送。"""

        watermark = FakeWatermark()
        sender = FakeSender()
        clock = FixedClock()
        results = []
        for _ in range(4):
            duty, _ = build_duty(watermark=watermark, sender=sender, clock=clock)
            results.append(duty.run_once())

        self.assertEqual(
            sum(1 for result in results if result is not None), 1, "四次模拟重启只应有一次真正发送"
        )
        self.assertEqual(len(sender.payloads), 1)

    def test_a_successful_send_marks_the_persisted_watermark(self) -> None:
        watermark = FakeWatermark()
        duty, _ = build_duty(watermark=watermark, clock=FixedClock())
        duty.run_once()
        self.assertIn(("2026-08-24", FAKE_CHAT_ID), watermark.mark_sent_calls)
        self.assertTrue(watermark.already_sent(report_date=date(2026, 8, 24), chat_id=FAKE_CHAT_ID))

    def test_a_send_failure_does_not_mark_the_persisted_watermark(self) -> None:
        watermark = FakeWatermark()
        duty, _ = build_duty(watermark=watermark, sender=FakeSender(failures=1), clock=FixedClock())
        duty.run_once()
        self.assertEqual(watermark.mark_sent_calls, [])
        self.assertFalse(
            watermark.already_sent(report_date=date(2026, 8, 24), chat_id=FAKE_CHAT_ID)
        )

    def test_the_in_memory_fast_path_avoids_a_second_watermark_lookup_the_same_day(self) -> None:
        """判重水位查询是每一轮的"第一道最快的一关"之后才会碰的第二关：同一
        进程同一天里，一旦内存水位置位，后续轮次不该再去查持久水位——这条快速
        路径的性能理由（避免同一进程存活期间每一轮都查库）本身值得钉住。"""

        watermark = FakeWatermark()
        clock = FixedClock()
        duty, _ = build_duty(watermark=watermark, clock=clock)
        duty.run_once()
        calls_after_first_send = len(watermark.already_sent_calls)
        duty.run_once()
        duty.run_once()
        self.assertEqual(len(watermark.already_sent_calls), calls_after_first_send)


class WatermarkCallFailureAuditTests(unittest.TestCase):
    """`#325` 尾账（opus 批次 5 审查留痕）：`already_sent`/`mark_sent` 是主线程里
    仅有的两次没有被 `_fetch` 那套单段失败保护包住的库调用——此前抛出会被更
    上层的通用 except 静默吞掉，留不下任何 `daily_report.*` 审计。这里分别钉住
    两处修复后的审计留痕与降级语义。"""

    def test_already_sent_raising_is_audited_and_skips_the_round_fail_closed(self) -> None:
        """fail-closed：查不到持久水位时保守跳过本轮，不冒险重新聚合并可能
        重复发送——宁可晚发一轮，不可重发。"""

        watermark = FakeWatermark(raise_on_already_sent=RuntimeError("模拟水位查询故障"))
        duty, parts = build_duty(watermark=watermark)

        result = duty.run_once()

        self.assertIsNone(result, "查不到水位时本轮不得发送")
        self.assertIsNone(duty.completed_on)
        self.assertEqual(parts["sender"].payloads, [])
        self.assertIn("daily_report.watermark_check_failed", parts["audit"].actions())
        failed_fields = parts["audit"].fields_for("daily_report.watermark_check_failed")[0]
        self.assertEqual(failed_fields["error"], "RuntimeError")
        # 异常没有冒泡带走整轮——上面这次 run_once() 调用本身没有抛出。

        # 下一轮水位查询恢复正常：不是永久卡死，照常聚合并发送。
        second = duty.run_once()
        self.assertIsNotNone(second)
        self.assertEqual(len(parts["sender"].payloads), 1)

    def test_mark_sent_raising_still_audits_sent_and_marks_completed_in_memory(self) -> None:
        """消息已经真实发出的前提下，水位写入失败不得掩盖这件事：`daily_report.sent`
        仍然记录（带 `watermark_persisted=False`），额外留一条告警级
        `watermark_persist_failed`；内存态照常置位，本进程存活期间不重发。"""

        watermark = FakeWatermark(raise_on_mark_sent=RuntimeError("模拟水位写入故障"))
        duty, parts = build_duty(watermark=watermark)

        result = duty.run_once()

        self.assertIsNotNone(result, "水位写入失败不得阻止消息已经发出这件事")
        self.assertEqual(len(parts["sender"].payloads), 1)

        self.assertIn("daily_report.sent", parts["audit"].actions())
        sent_fields = parts["audit"].fields_for("daily_report.sent")[0]
        self.assertFalse(sent_fields["watermark_persisted"])

        self.assertIn("daily_report.watermark_persist_failed", parts["audit"].actions())
        persist_failed_fields = parts["audit"].fields_for("daily_report.watermark_persist_failed")[
            0
        ]
        self.assertEqual(persist_failed_fields["error"], "RuntimeError")

        # 内存态照常置位：本进程存活期间不会重发同一窗口的通报。
        self.assertEqual(duty.completed_on, date(2026, 8, 24))
        second = duty.run_once()
        self.assertIsNone(second)
        self.assertEqual(len(parts["sender"].payloads), 1)


class AggregationDoesNotBlockCallerTests(unittest.TestCase):
    """`V-通报-11`（Issue #325）：重聚合耗时不得占用调用方所在的线程（scheduler
    主循环：心跳与其余职责，含 `AlertingDuty` 的心跳评估，都在这条线程上）。
    用可注入的慢聚合桩（`BlockingSource`）证明：聚合还没收工时，`run_once` 已经
    把控制权交还调用方。
    """

    def test_run_once_returns_before_a_deliberately_slow_aggregation_finishes(self) -> None:
        gate = threading.Event()
        released = threading.Event()
        source = BlockingSource(gate=gate, released=released)
        self.addCleanup(gate.set)  # 防止后台线程残留到用例结束之后
        duty, parts = build_duty(source=source)
        duty._aggregation_join_timeout_seconds = 0.05  # noqa: SLF001 - 白盒调短等待上限

        started = time.monotonic()
        result = duty.run_once()
        elapsed = time.monotonic() - started

        self.assertIsNone(result, "聚合还没收工，调用方本轮拿不到正文")
        self.assertLess(elapsed, 1.0, "调用方必须在很短时间内拿回控制权，不等聚合收工")
        self.assertFalse(released.is_set(), "调用方返回时，聚合确实还没跑完（不是恰好跑完）")

        gate.set()
        self.assertTrue(released.wait(timeout=5.0), "放行后后台聚合应当很快完成")
        self._wait_for_completion(duty)
        self.assertEqual(duty.completed_on, date(2026, 8, 24))
        self.assertEqual(len(parts["sender"].payloads), 1)

    def test_a_fast_aggregation_still_returns_the_text_synchronously(self) -> None:
        """默认关口（`DEFAULT_AGGREGATION_JOIN_TIMEOUT_SECONDS`）下，正常速度的
        聚合（全部既有测试用的假数据源）行为与改动前完全一致：同步拿到正文。"""

        duty, parts = build_duty()
        text = duty.run_once()
        self.assertIsNotNone(text)
        self.assertEqual(len(parts["sender"].payloads), 1)

    def test_a_second_tick_while_aggregation_is_still_running_does_not_dispatch_twice(self) -> None:
        """派发去重：后台聚合还没收工时，下一轮 `run_once` 不得再起第二个后台
        线程——否则两轮同时读库、同时可能尝试发送。"""

        gate = threading.Event()
        released = threading.Event()
        source = BlockingSource(gate=gate, released=released)
        self.addCleanup(gate.set)
        duty, parts = build_duty(source=source)
        duty._aggregation_join_timeout_seconds = 0.05  # noqa: SLF001

        first = duty.run_once()  # 派发后台聚合，卡在闸门上
        second = duty.run_once()  # 同一轮还没收工，这次调用不该再派发

        self.assertIsNone(first)
        self.assertIsNone(second)
        gate.set()
        released.wait(timeout=5.0)
        self._wait_for_completion(duty)
        # `task_outcomes` 只应该被调用一次，证明第二次 run_once 没有起第二个
        # 后台聚合；发送也只应该真正发生一次。
        self.assertEqual(len(source.calls["task_outcomes"]), 1)
        self.assertEqual(len(parts["sender"].payloads), 1)

    @staticmethod
    def _wait_for_completion(duty: DailyReportDuty, *, timeout: float = 5.0) -> None:
        """轮询等待后台聚合线程收尾（`completed_on` 置位）；不引入 sleep 循环
        以外的同步原语，代价是有界轮询而不是事件通知——测试范围内可接受。"""

        deadline = time.monotonic() + timeout
        while duty.completed_on is None and time.monotonic() < deadline:
            time.sleep(0.02)


class MetricCoverageWiringTests(unittest.TestCase):
    """「未覆盖新指标」日检的职责层接线（Issue #320 并入项）：未接线不出现、
    接线且取数失败留不可判定审计、接线且有差异出现在正文与审计里。
    """

    def test_unwired_produces_no_coverage_section_and_no_audit_entry(self) -> None:
        duty, parts = build_duty(metric_coverage=None)

        text = duty.run_once()

        assert text is not None
        self.assertNotIn("待分配", text)
        sent_fields = parts["audit"].fields_for("daily_report.sent")[0]
        self.assertNotIn("metric_coverage", sent_fields["undetermined_sections"])

    def test_a_failing_coverage_check_is_marked_undetermined_and_the_rest_still_sends(self) -> None:
        def _boom() -> tuple[tuple[str, ...], tuple[str, ...]]:
            raise RuntimeError("模拟 MCP 查询失败")

        duty, parts = build_duty(metric_coverage=_boom)

        text = duty.run_once()

        assert text is not None
        self.assertIn("待分配", text)
        self.assertIn("不可判定", text)
        # 单段失败不拖累其余段落——活跃用户段的真实数字照常出现。
        self.assertIn("活跃用户", text)
        section_reads = parts["audit"].fields_for("daily_report.section_read_failed")
        self.assertTrue(any(entry["section"] == "metric_coverage" for entry in section_reads))
        sent_fields = parts["audit"].fields_for("daily_report.sent")[0]
        self.assertIn("metric_coverage", sent_fields["undetermined_sections"])

    def test_a_detected_gap_appears_in_the_sent_text(self) -> None:
        duty, parts = build_duty(
            metric_coverage=lambda: (("brand_new_metric", "exchange_rate"), ("exchange_rate",))
        )

        text = duty.run_once()

        assert text is not None
        self.assertIn("待分配", text)
        self.assertIn("brand_new_metric", text)
        sent_fields = parts["audit"].fields_for("daily_report.sent")[0]
        self.assertNotIn("metric_coverage", sent_fields["undetermined_sections"])

    def test_no_gap_produces_no_coverage_section(self) -> None:
        duty, parts = build_duty(
            metric_coverage=lambda: (("exchange_rate",), ("exchange_rate", "vat_rate"))
        )

        text = duty.run_once()

        assert text is not None
        self.assertNotIn("待分配", text)


class LocalOverrideActivityWiringTests(unittest.TestCase):
    """「本地权限覆盖活动」段的职责层接线（Issue #319 S-P-1c）：未接线不出现、
    接线且取数失败留不可判定审计（只关本段，其余段照常）、接线且有活动出现在
    正文与审计里、零活动零生效不出现。
    """

    @staticmethod
    def _stub(
        *,
        granted: int = 0,
        suppressed: int = 0,
        revoked: int = 0,
        active_grant: int = 0,
        active_suppress: int = 0,
        affected: int = 0,
    ):
        def _fetch(*, window_start, window_end):
            return (granted, suppressed, revoked, active_grant, active_suppress, affected)

        return _fetch

    def test_unwired_produces_no_section_and_the_rest_still_sends(self) -> None:
        """回调缺席：只关本段，其余段照常——`daily_report.sent` 恰一条，
        `undetermined_sections` 里不出现 `local_override_activity`。"""

        duty, parts = build_duty(local_override_activity=None)

        text = duty.run_once()

        assert text is not None
        self.assertNotIn("本地权限覆盖活动", text)
        self.assertIn("活跃用户", text)  # 其余段落照常渲染
        sent_fields = parts["audit"].fields_for("daily_report.sent")[0]
        self.assertNotIn("local_override_activity", sent_fields["undetermined_sections"])

    def test_a_failing_fetch_is_marked_undetermined_and_the_rest_still_sends(self) -> None:
        def _boom(*, window_start, window_end):
            raise RuntimeError("模拟本地权限覆盖查询失败")

        duty, parts = build_duty(local_override_activity=_boom)

        text = duty.run_once()

        assert text is not None
        self.assertIn("本地权限覆盖活动", text)
        self.assertIn("不可判定", text)
        # 单段失败不拖累其余段落——活跃用户段的真实数字照常出现。
        self.assertIn("活跃用户", text)
        section_reads = parts["audit"].fields_for("daily_report.section_read_failed")
        self.assertTrue(
            any(entry["section"] == "local_override_activity" for entry in section_reads)
        )
        sent_fields = parts["audit"].fields_for("daily_report.sent")[0]
        self.assertIn("local_override_activity", sent_fields["undetermined_sections"])

    def test_todays_activity_appears_in_the_sent_text_with_correct_counts(self) -> None:
        duty, parts = build_duty(
            local_override_activity=self._stub(
                granted=2, suppressed=1, revoked=1, active_grant=5, active_suppress=3, affected=6
            )
        )

        text = duty.run_once()

        assert text is not None
        self.assertIn("本地权限覆盖活动", text)
        # 术语统一（Trace #469 收尾批修复包 E）：同 tests/test_daily_report_
        # render.py 的完整词断言，职责层这条出口同样不许退回旧说法。
        self.assertIn("新增 补充授权 2 笔", text)
        self.assertIn("屏蔽指标 1 笔", text)
        self.assertIn("撤销 1 笔", text)
        self.assertNotIn("收回", text)
        self.assertNotIn("抑制", text)
        self.assertIn("涉及 6 位用户", text)
        sent_fields = parts["audit"].fields_for("daily_report.sent")[0]
        self.assertNotIn("local_override_activity", sent_fields["undetermined_sections"])

    def test_zero_activity_and_zero_active_total_produces_no_section(self) -> None:
        duty, parts = build_duty(local_override_activity=self._stub())

        text = duty.run_once()

        assert text is not None
        self.assertNotIn("本地权限覆盖活动", text)

    def test_the_fetch_is_called_with_the_primary_report_window_not_the_delivery_window(
        self,
    ) -> None:
        """本段与其余五段共用主统计窗口（`window_start`/`window_end`），不是
        投递结果段那个独立的更早窗口——对齐日报既有 UTC 日界口径（任务卡明确
        要求）。"""

        observed: list[tuple] = []

        def _fetch(*, window_start, window_end):
            observed.append((window_start, window_end))
            return (0, 0, 0, 0, 0, 0)

        duty, parts = build_duty(local_override_activity=_fetch)
        duty.run_once()

        self.assertEqual(len(observed), 1)
        active_users_window = parts["source"].calls["active_user_task_counts"][0]
        self.assertEqual(observed[0], active_users_window)


if __name__ == "__main__":
    unittest.main()
