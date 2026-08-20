"""迟到就绪恢复职责的纯逻辑验收（V-开通-18）。

十五分钟同步超时之后仍然确认成功的用户，最终会被写成 ``active`` 并得到「可以开始使用」
的主动通知；在此之前他不会收到任何暗示已经可用、实际却发不了问数的消息——这是
`V-开通-18` 的完整断言，本文件承担它的编排半边（判定层的断言在
``tests/test_mcp_readiness_machine.py`` 的 ``ReadinessRecoveryTickerTest``）。

否定断言（合同的"不得 / 不允许"必须有对应否定测试，验证与门禁第八节）：

1. **未就绪（等待中 / 技术失败 / 探针未接线）绝不推进 ``active``、绝不发通知**——
   这正是 V-开通-18 断言的后半句；
2. **通知恰一次**：状态推进成功之后这个人立刻退出候选集，同一个人不可能被通知第二次
   （``test_running_twice_notifies_and_activates_only_once``）；
3. **推进被数据库拒绝（账号在竞态窗口里被停用）不发通知**；
4. **收件人不可用不发通知，但不影响已经完成的状态推进**；
5. 通知失败**有限重试**，仍失败**不抛异常**、不影响状态；
6. 停止信号之后**零推进、零通知**（未处理的候选原样留给下一轮）；
7. 单个用户失败不带走整轮；
8. 报告与审计**只有计数**，不含权限值、open_id 或渲染后的正文。
"""

from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace

from lingxi.apps.scheduler.late_readiness_recovery import (
    DEFAULT_NOTIFY_ATTEMPTS,
    DEFAULT_RECOVERY_INTERVAL_SECONDS,
    DEFAULT_RECOVERY_LIMIT,
    LateReadinessRecoveryDuty,
    LateReadinessRecoveryReport,
)
from lingxi.core.identity.onboarding_runner import (
    FIRST_ONBOARDING_REASON,
    KEY_COMPLETED,
    STATE_ACTIVE,
)
from lingxi.core.permission.mcp_readiness import (
    ReadinessAttempt,
    ReadinessBinding,
    ReadinessOutcome,
)

USER_A = "usr_01JQZX3M5N7P9R1T3V5W7Y9A0B"
USER_B = "usr_01JQZX3M5N7P9R1T3V5W7Y9A0C"
OPEN_ID = "ou_fake_open_id_for_tests"
PERMISSIONS = '{"1011":["日活"]}'
VERSION = 3


def _candidate(
    *,
    user_id: str = USER_A,
    version: int = VERSION,
    permissions: str = PERMISSIONS,
    next_attempt_no: int = 8,
    already_ready: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=user_id,
        permission_version=version,
        permissions=permissions,
        next_attempt_no=next_attempt_no,
        already_ready=already_ready,
    )


def _attempt(outcome: ReadinessOutcome) -> ReadinessAttempt:
    from datetime import datetime, timezone

    moment = datetime(2026, 8, 19, 3, 0, tzinfo=timezone.utc)
    kwargs: dict = {}
    if outcome is ReadinessOutcome.READY:
        kwargs = {"metric_count": 4}
    elif outcome is ReadinessOutcome.WAITING:
        kwargs = {"error_code": "empty_metrics", "metric_count": 0}
    else:
        kwargs = {"error_code": "probe_overran_timeout"}
    return ReadinessAttempt(
        binding=ReadinessBinding(USER_A, VERSION),
        attempt_no=8,
        outcome=outcome,
        started_at=moment,
        finished_at=moment,
        **kwargs,
    )


class FakeCandidates:
    """按脚本返回一份固定候选列表；也可用作 :class:`FakeUsers` 手动搭配的读取口。"""

    def __init__(self, *candidates: SimpleNamespace) -> None:
        self._candidates = list(candidates)
        self.calls: list[dict] = []

    def late_onboarding_recovery_candidates(
        self, *, reason: str, recovery_interval_seconds: int, limit: int = 50
    ):
        self.calls.append(
            {"reason": reason, "interval": recovery_interval_seconds, "limit": limit}
        )
        return tuple(self._candidates[:limit])


class FakeTicker:
    """按用户脚本返回一次判定；``None`` 表示"探针未接线"（构造时整体传 ``probe=None``
    等价，这里用一个脚本值 ``UNWIRED`` 表达同一件事，避免为它单独建一个 fake 类）。
    """

    UNWIRED = object()

    def __init__(self, script: dict[str, object] | None = None) -> None:
        self._script = script or {}
        self.calls: list[tuple[str, int, int]] = []

    def probe_after_timeout(self, binding: ReadinessBinding, *, attempt_no: int):
        self.calls.append((binding.user_id, binding.permission_version, attempt_no))
        step = self._script.get(binding.user_id, ReadinessOutcome.READY)
        if step is self.UNWIRED:
            return None
        if isinstance(step, BaseException):
            raise step
        return _attempt(step)


class FakeUsers:
    def __init__(self, *, allow: dict[str, bool] | None = None) -> None:
        self._allow = allow or {}
        self.calls: list[tuple[str, str]] = []

    def advance_provisioning_state(self, user_id: str, *, to: str) -> bool:
        self.calls.append((user_id, to))
        return self._allow.get(user_id, True)


class FakeRecipients:
    def __init__(self, *, open_ids: dict[str, str | None] | None = None) -> None:
        self._open_ids = {USER_A: OPEN_ID, USER_B: OPEN_ID} if open_ids is None else open_ids
        self.calls: list[str] = []

    def notice_recipient_open_id(self, user_id: str) -> str | None:
        self.calls.append(user_id)
        return self._open_ids.get(user_id)


class FakeNotifier:
    def __init__(self, *, fail_times: int = 0, error: Exception | None = None) -> None:
        self._fail_times = fail_times
        self._error = error
        self.calls: list[dict] = []

    def send(self, *, open_id: str, key: str, values, dedupe_key: str) -> None:
        self.calls.append(
            {"open_id": open_id, "key": key, "values": dict(values), "dedupe_key": dedupe_key}
        )
        if len(self.calls) <= self._fail_times:
            raise (self._error or RuntimeError("send_failed"))


class RecordingAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, dict(fields)))

    def fields_for(self, action: str) -> list[dict]:
        return [fields for name, fields in self.records if name == action]

    def actions(self) -> list[str]:
        return [name for name, _ in self.records]


class RecordingSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


_UNSET = object()


def build_duty(
    *,
    candidates=_UNSET,
    ticker=_UNSET,
    users: FakeUsers | None = None,
    recipients: FakeRecipients | None = None,
    notifier: FakeNotifier | None = None,
    audit: RecordingAudit | None = None,
    sleep: RecordingSleep | None = None,
    reason: str = FIRST_ONBOARDING_REASON,
    stop: threading.Event | None = None,
    **kwargs,
):
    candidates = FakeCandidates(_candidate()) if candidates is _UNSET else candidates
    ticker = FakeTicker() if ticker is _UNSET else ticker
    users = users or FakeUsers()
    recipients = recipients or FakeRecipients()
    notifier = notifier or FakeNotifier()
    audit = audit or RecordingAudit()
    sleep = sleep or RecordingSleep()
    duty = LateReadinessRecoveryDuty(
        candidates=candidates,
        ticker=ticker,
        users=users,
        recipients=recipients,
        notifier=notifier,
        audit=audit,
        sleep=sleep,
        reason=reason,
        stop=stop,
        **kwargs,
    )
    return duty, {
        "candidates": candidates,
        "ticker": ticker,
        "users": users,
        "recipients": recipients,
        "notifier": notifier,
        "audit": audit,
        "sleep": sleep,
    }


class ConstructionTest(unittest.TestCase):
    def test_reason_is_required(self) -> None:
        for reason in ("", "   "):
            with self.subTest(reason=reason):
                with self.assertRaises(ValueError):
                    build_duty(reason=reason)

    def test_sleep_must_be_callable(self) -> None:
        with self.assertRaises(TypeError):
            LateReadinessRecoveryDuty(
                candidates=FakeCandidates(),
                ticker=FakeTicker(),
                users=FakeUsers(),
                recipients=FakeRecipients(),
                notifier=FakeNotifier(),
                audit=RecordingAudit(),
                sleep=None,  # type: ignore[arg-type]
                reason=FIRST_ONBOARDING_REASON,
            )

    def test_recovery_interval_must_be_a_positive_integer(self) -> None:
        for bad in (0, -1, True, 1.5):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    build_duty(recovery_interval_seconds=bad)

    def test_limit_must_be_a_positive_integer(self) -> None:
        for bad in (0, -1, True):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    build_duty(limit=bad)

    def test_notify_attempts_must_be_a_positive_integer(self) -> None:
        for bad in (0, -1, True):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    build_duty(notify_attempts=bad)

    def test_defaults_match_the_documented_choices(self) -> None:
        self.assertEqual(DEFAULT_RECOVERY_INTERVAL_SECONDS, 900)
        self.assertEqual(DEFAULT_RECOVERY_LIMIT, 50)
        self.assertGreaterEqual(DEFAULT_NOTIFY_ATTEMPTS, 1)


class ReadyCandidateTest(unittest.TestCase):
    """核心正向：超时后就绪 → 写 ``active`` + 恰一次「开通完成」通知。"""

    def test_a_late_success_activates_the_user_and_sends_the_completion_message(self) -> None:
        duty, seams = build_duty(
            candidates=FakeCandidates(_candidate()),
            ticker=FakeTicker({USER_A: ReadinessOutcome.READY}),
        )

        report = duty.run_once()

        self.assertEqual(report.examined, 1)
        self.assertEqual(report.ready, 1)
        self.assertEqual(report.activated, 1)
        self.assertEqual(report.notified, 1)
        self.assertEqual(report.failed, 0)
        self.assertEqual(seams["users"].calls, [(USER_A, STATE_ACTIVE)])
        self.assertEqual(len(seams["notifier"].calls), 1)
        sent = seams["notifier"].calls[0]
        self.assertEqual(sent["key"], KEY_COMPLETED)
        self.assertEqual(sent["open_id"], OPEN_ID)
        self.assertEqual(sent["dedupe_key"], f"onboarding:recovery:{USER_A}:{VERSION}")

    def test_the_completion_text_reports_the_users_actual_scope(self) -> None:
        """文案里的公司/职能位必须来自这个人**实际**发布出去的那一份权限，不是写死值。"""

        duty, seams = build_duty(
            candidates=FakeCandidates(
                _candidate(permissions='{"2022":["收入","留存"]}')
            ),
            ticker=FakeTicker({USER_A: ReadinessOutcome.READY}),
        )

        duty.run_once()

        values = seams["notifier"].calls[0]["values"]
        self.assertEqual(values["company_name"], "2022")
        self.assertEqual(sorted(values["function_name"].split("、")), ["收入", "留存"])

    def test_an_already_ready_candidate_skips_the_probe_and_finishes_the_job(self) -> None:
        """崩溃恢复：上一轮已经探到 ready，只是没来得及推进 + 通知。"""

        duty, seams = build_duty(
            candidates=FakeCandidates(_candidate(already_ready=True)),
            ticker=FakeTicker({USER_A: ReadinessOutcome.WAITING}),  # 若被误调用会走到这里
        )

        report = duty.run_once()

        self.assertEqual(report.ready, 1)
        self.assertEqual(report.activated, 1)
        self.assertEqual(report.notified, 1)
        self.assertEqual(seams["ticker"].calls, [], "已经探到过 ready 的候选不该再探一次")


class NotReadyCandidateTest(unittest.TestCase):
    """核心负向：未就绪 → 不写 ``active``、不发任何暗示可用的消息。"""

    def test_waiting_does_not_activate_or_notify(self) -> None:
        duty, seams = build_duty(ticker=FakeTicker({USER_A: ReadinessOutcome.WAITING}))

        report = duty.run_once()

        self.assertEqual(report.waiting, 1)
        self.assertEqual(report.activated, 0)
        self.assertEqual(report.notified, 0)
        self.assertEqual(seams["users"].calls, [], "未就绪绝不能推进 provisioning_state")
        self.assertEqual(seams["notifier"].calls, [], "未就绪绝不能发送任何消息")

    def test_technical_failure_does_not_activate_or_notify(self) -> None:
        duty, seams = build_duty(
            ticker=FakeTicker({USER_A: ReadinessOutcome.TECHNICAL_FAILURE})
        )

        report = duty.run_once()

        self.assertEqual(report.technical_failures, 1)
        self.assertEqual(report.activated, 0)
        self.assertEqual(report.notified, 0)
        self.assertEqual(seams["users"].calls, [])
        self.assertEqual(seams["notifier"].calls, [])

    def test_probe_unwired_leaves_the_candidate_untouched(self) -> None:
        duty, seams = build_duty(ticker=FakeTicker({USER_A: FakeTicker.UNWIRED}))

        report = duty.run_once()

        self.assertEqual(report.probe_unwired, 1)
        self.assertEqual(report.activated, 0)
        self.assertEqual(report.notified, 0)
        self.assertEqual(seams["users"].calls, [])
        self.assertEqual(seams["notifier"].calls, [])

    def test_the_duty_reports_whether_the_probe_face_is_wired(self) -> None:
        duty, _ = build_duty(ticker=None)
        self.assertFalse(duty.probe_wired)

        duty2, _ = build_duty()
        self.assertTrue(duty2.probe_wired)

    def test_a_missing_ticker_does_not_advance_a_candidate_that_needs_probing(self) -> None:
        duty, seams = build_duty(candidates=FakeCandidates(_candidate()), ticker=None)

        report = duty.run_once()

        self.assertEqual(report.probe_unwired, 1)
        self.assertEqual(report.activated, 0)
        self.assertEqual(seams["users"].calls, [])
        self.assertEqual(seams["notifier"].calls, [])

    def test_a_missing_ticker_still_finishes_an_already_ready_candidate(self) -> None:
        """探针未接线时，``already_ready`` 那一条罕见的崩溃恢复路径仍然要走完。"""

        duty, seams = build_duty(
            candidates=FakeCandidates(_candidate(already_ready=True)), ticker=None
        )

        report = duty.run_once()

        self.assertEqual(report.activated, 1)
        self.assertEqual(report.notified, 1)


class AdvanceAndNotifyEdgeCaseTest(unittest.TestCase):
    def test_advance_refused_does_not_notify(self) -> None:
        """账号在候选查到与这里之间被停用：推进被数据库拒绝，绝不发通知。"""

        duty, seams = build_duty(
            ticker=FakeTicker({USER_A: ReadinessOutcome.READY}),
            users=FakeUsers(allow={USER_A: False}),
        )

        report = duty.run_once()

        self.assertEqual(report.ready, 1)
        self.assertEqual(report.activated, 0)
        self.assertEqual(report.advance_refused, 1)
        self.assertEqual(report.notified, 0)
        self.assertEqual(seams["notifier"].calls, [])
        self.assertIn("late_readiness_recovery.advance_refused", seams["audit"].actions())

    def test_missing_recipient_is_skipped_without_raising(self) -> None:
        duty, seams = build_duty(
            ticker=FakeTicker({USER_A: ReadinessOutcome.READY}),
            recipients=FakeRecipients(open_ids={USER_A: None}),
        )

        report = duty.run_once()

        self.assertEqual(report.activated, 1, "状态已经真实推进，不因为通知不了而回滚")
        self.assertEqual(report.notice_skipped, 1)
        self.assertEqual(report.notified, 0)
        self.assertEqual(seams["notifier"].calls, [])

    def test_notification_retries_then_succeeds(self) -> None:
        duty, seams = build_duty(
            ticker=FakeTicker({USER_A: ReadinessOutcome.READY}),
            notifier=FakeNotifier(fail_times=1),
        )

        report = duty.run_once()

        self.assertEqual(report.notified, 1)
        self.assertEqual(report.notice_failed, 0)
        self.assertEqual(len(seams["notifier"].calls), 2)
        self.assertEqual(len(seams["sleep"].calls), 1, "只在失败之后退避一次")

    def test_notification_exhausts_retries_and_is_counted_as_failed(self) -> None:
        duty, seams = build_duty(
            ticker=FakeTicker({USER_A: ReadinessOutcome.READY}),
            notifier=FakeNotifier(fail_times=99),
            notify_attempts=3,
        )

        report = duty.run_once()

        self.assertEqual(report.activated, 1, "通知发不出去不回滚已经完成的状态推进")
        self.assertEqual(report.notified, 0)
        self.assertEqual(report.notice_failed, 1)
        self.assertEqual(len(seams["notifier"].calls), 3)
        self.assertEqual(
            len(seams["audit"].fields_for("late_readiness_recovery.notify_failed")), 3
        )


class IdempotencyTest(unittest.TestCase):
    """幂等：同一用户跑两轮，只有一次通知、只有一次状态写入。"""

    def test_running_twice_notifies_and_activates_only_once(self) -> None:
        state = {"provisioning_state": "mcp_syncing", "account_state": "enabled"}

        class MiniCandidates:
            def late_onboarding_recovery_candidates(self, *, reason, recovery_interval_seconds, limit=50):
                if state["provisioning_state"] != "mcp_syncing":
                    return ()
                return (_candidate(),)

        class MiniUsers:
            def advance_provisioning_state(self, user_id, *, to):
                if state["provisioning_state"] == "mcp_syncing" and state["account_state"] == "enabled":
                    state["provisioning_state"] = to
                    return True
                return False

        candidates = MiniCandidates()
        users = MiniUsers()
        ticker = FakeTicker({USER_A: ReadinessOutcome.READY})
        notifier = FakeNotifier()

        duty, _ = build_duty(candidates=candidates, ticker=ticker, users=users, notifier=notifier)

        first = duty.run_once()
        second = duty.run_once()

        self.assertEqual(first.activated, 1)
        self.assertEqual(first.notified, 1)
        self.assertEqual(second.examined, 0, "推进成功之后这个人不再是候选")
        self.assertEqual(second.activated, 0)
        self.assertEqual(second.notified, 0)
        self.assertEqual(len(notifier.calls), 1, "「开通完成」只能收到一次")
        self.assertEqual(len(ticker.calls), 1, "已经就绪的人不该被重复探针")


class RoundBehaviourTest(unittest.TestCase):
    def test_stop_signal_before_the_round_does_nothing(self) -> None:
        stop = threading.Event()
        stop.set()
        duty, seams = build_duty(stop=stop)

        report = duty.run_once()

        self.assertIsNone(report)
        self.assertEqual(seams["candidates"].calls, [])

    def test_stop_signal_mid_round_interrupts_and_leaves_the_rest_untouched(self) -> None:
        stop = threading.Event()

        class StoppingTicker(FakeTicker):
            def probe_after_timeout(self, binding, *, attempt_no):
                stop.set()
                return super().probe_after_timeout(binding, attempt_no=attempt_no)

        duty, seams = build_duty(
            candidates=FakeCandidates(_candidate(user_id=USER_A), _candidate(user_id=USER_B)),
            ticker=StoppingTicker({USER_A: ReadinessOutcome.READY, USER_B: ReadinessOutcome.READY}),
            stop=stop,
        )

        report = duty.run_once()

        self.assertTrue(report.interrupted)
        self.assertEqual(report.examined, 1, "停止信号之后不再处理下一个候选")
        self.assertEqual(seams["ticker"].calls, [(USER_A, VERSION, 8)])

    def test_a_single_user_failure_does_not_take_down_the_round(self) -> None:
        class ExplodingTicker(FakeTicker):
            def probe_after_timeout(self, binding, *, attempt_no):
                if binding.user_id == USER_A:
                    raise RuntimeError("boom")
                return super().probe_after_timeout(binding, attempt_no=attempt_no)

        duty, seams = build_duty(
            candidates=FakeCandidates(_candidate(user_id=USER_A), _candidate(user_id=USER_B)),
            ticker=ExplodingTicker({USER_B: ReadinessOutcome.READY}),
        )

        report = duty.run_once()

        self.assertEqual(report.failed, 1)
        self.assertEqual(report.activated, 1, "另一个用户照常处理完")
        self.assertIn("late_readiness_recovery.user_failed", seams["audit"].actions())

    def test_the_round_is_scoped_to_the_declared_reason(self) -> None:
        duty, seams = build_duty()

        duty.run_once()

        self.assertEqual(seams["candidates"].calls[0]["reason"], FIRST_ONBOARDING_REASON)

    def test_the_report_and_audit_carry_no_field_values(self) -> None:
        """报告与审计只有计数与固定分类，不含权限值、open_id 或渲染后的正文。"""

        duty, seams = build_duty(ticker=FakeTicker({USER_A: ReadinessOutcome.READY}))

        report = duty.run_once()

        for value in report.audit_facts().values():
            self.assertNotIn(OPEN_ID, str(value))
            self.assertNotIn(PERMISSIONS, str(value))
        for _, fields in seams["audit"].records:
            for value in fields.values():
                self.assertNotIn(OPEN_ID, str(value))
                self.assertNotIn(PERMISSIONS, str(value))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
