"""开通中途停摆收口职责的纯逻辑验收（Issue #282，`V-开通-19` 的另一半）。

`abort_stalled_provisioning` 与候选查询各自的真库半边分别在
``tests/test_identity_postgres_records.py`` 与
``tests/test_postgres_stalled_provisioning.py``；本文件只钉编排层：**先通知，后
收口**——通知送达才 CAS 收口，通知送不到就留在原状态，下一轮重来（幂等）。

否定断言（合同的"不得 / 不允许"必须有对应否定测试，验证与门禁第八节）：

1. **通知失败时状态必须仍是原状态**：注入抛异常的 notifier，证明"改了状态却不告诉
   人"不可能发生——``test_a_failed_notification_never_touches_the_state``。
2. **`notifier=None` 时一条候选都不处理**：连候选查询都不该发起，只记一次
   ``notifier_not_wired``。
3. **停机中 `run_once()` 返回 `None`**：不认领、不发送。
4. **审计字段里不出现 `open_id` 与任何资料值**。
5. **CAS 返回 0 行不重发**：通知已经发出去了，重发只会制造第二条相互矛盾的消息。
6. **单个候选的失败不带走整轮**。
7. **零候选时不记完成审计**。
"""

from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from typing import Any

from lingxi.apps.scheduler.stalled_provisioning import (
    DEFAULT_STALLED_LEASE_SECONDS,
    DEFAULT_STALLED_LIMIT,
    StalledProvisioningDuty,
    StalledProvisioningReport,
)
from lingxi.core.identity.onboarding_runner import (
    KEY_INTERNAL_ERROR,
    STATE_MCP_SYNCING,
    STATE_PROVISIONING,
)

USER_A = "usr_stalled_a"
USER_B = "usr_stalled_b"
OPEN_ID_A = "ou_stalled_a"
OPEN_ID_B = "ou_stalled_b"


def _candidate(
    *,
    user_id: str = USER_A,
    open_id: str = OPEN_ID_A,
    event_id: str = "evt_a",
    trace_id: str = "trc_a",
    provisioning_state: str = "provisioning",
) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=user_id,
        open_id=open_id,
        event_id=event_id,
        trace_id=trace_id,
        provisioning_state=provisioning_state,
    )


class FakeCandidates:
    def __init__(self, items: Any = ()) -> None:
        self._items = list(items)
        self.calls: list[tuple[int, int]] = []

    def stalled_provisioning_candidates(
        self, *, lease_seconds: int, limit: int = 50
    ) -> tuple[Any, ...]:
        self.calls.append((lease_seconds, limit))
        return tuple(self._items)


class FakeAborter:
    def __init__(
        self, *, result: bool = True, error: Exception | None = None, order: list[str] | None = None
    ) -> None:
        self.calls: list[tuple[str, tuple[str, ...], str]] = []
        self._result = result
        self._error = error
        self._order = order

    def abort_stalled_provisioning(
        self, *, user_id: str, expected_states: Any, reason: str
    ) -> bool:
        if self._order is not None:
            self._order.append("abort")
        self.calls.append((user_id, tuple(expected_states), reason))
        if self._error is not None:
            raise self._error
        return self._result


class FakeNotifier:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        fail_for: frozenset[str] = frozenset(),
        order: list[str] | None = None,
    ) -> None:
        self.sent: list[tuple[str, str, dict[str, object], str]] = []
        self._error = error
        self._fail_for = fail_for
        self._order = order

    def send(
        self, *, open_id: str, key: str, values: Any, dedupe_key: str
    ) -> None:
        if self._order is not None:
            self._order.append("notify")
        if self._error is not None and (not self._fail_for or open_id in self._fail_for):
            raise self._error
        self.sent.append((open_id, key, dict(values), dedupe_key))


class RecordingAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, object]]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, dict(fields)))

    def actions(self) -> list[str]:
        return [action for action, _ in self.records]

    def facts(self, action: str) -> dict[str, object]:
        for name, fields in self.records:
            if name == action:
                return fields
        raise AssertionError(f"没有记到 {action}：{self.actions()}")


def build_duty(**overrides: Any) -> tuple[StalledProvisioningDuty, dict[str, Any]]:
    parts: dict[str, Any] = {
        "candidates": FakeCandidates(),
        "aborter": FakeAborter(),
        "notifier": FakeNotifier(),
        "audit": RecordingAudit(),
    }
    parts.update({key: value for key, value in overrides.items() if key in parts})
    duty = StalledProvisioningDuty(
        candidates=parts["candidates"],
        aborter=parts["aborter"],
        notifier=parts["notifier"],
        audit=parts["audit"],
        lease_seconds=overrides.get("lease_seconds", DEFAULT_STALLED_LEASE_SECONDS),
        limit=overrides.get("limit", DEFAULT_STALLED_LIMIT),
        stop=overrides.get("stop"),
    )
    return duty, parts


class HappyPathTests(unittest.TestCase):
    def test_a_candidate_is_notified_then_aborted(self) -> None:
        candidates = FakeCandidates([_candidate()])
        aborter = FakeAborter()
        notifier = FakeNotifier()
        audit = RecordingAudit()
        duty, _ = build_duty(candidates=candidates, aborter=aborter, notifier=notifier, audit=audit)

        report = duty.run_once()

        assert report is not None
        self.assertEqual((report.examined, report.notified, report.aborted), (1, 1, 1))
        self.assertEqual(
            notifier.sent, [(OPEN_ID_A, KEY_INTERNAL_ERROR, {}, "onboarding:stalled:evt_a")]
        )
        self.assertEqual(
            aborter.calls, [(USER_A, (STATE_PROVISIONING, STATE_MCP_SYNCING), "stalled_lease_expired")]
        )
        self.assertIn("stalled_provisioning.aborted", audit.actions())

    def test_the_dedupe_key_is_distinct_from_the_terminal_notice_key(self) -> None:
        """`onboarding:stalled:{event_id}` 刻意与终态通知的 `onboarding:{event_id}`
        不同键，两者不会互相去重掉。"""

        notifier = FakeNotifier()
        duty, _ = build_duty(candidates=FakeCandidates([_candidate(event_id="evt_x")]), notifier=notifier)

        duty.run_once()

        dedupe_key = notifier.sent[0][3]
        self.assertEqual(dedupe_key, "onboarding:stalled:evt_x")
        self.assertNotEqual(dedupe_key, "onboarding:evt_x")

    def test_the_lease_and_limit_are_passed_through_to_the_candidate_query(self) -> None:
        candidates = FakeCandidates([])
        duty, _ = build_duty(candidates=candidates, lease_seconds=123, limit=7)

        duty.run_once()

        self.assertEqual(candidates.calls, [(123, 7)])

    def test_zero_candidates_records_no_completion_audit(self) -> None:
        audit = RecordingAudit()
        duty, _ = build_duty(candidates=FakeCandidates([]), audit=audit)

        report = duty.run_once()

        assert report is not None
        self.assertEqual(report.examined, 0)
        self.assertEqual(audit.actions(), [])


class NegativeAssertionTests(unittest.TestCase):
    """合同"不得"类规则的主动违规测试。"""

    def test_a_failed_notification_never_touches_the_state(self) -> None:
        """**否定断言**：通知失败时绝不能收口——「改了状态却不告诉人」不可能发生。"""

        aborter = FakeAborter()
        notifier = FakeNotifier(error=RuntimeError("feishu down"))
        audit = RecordingAudit()
        duty, _ = build_duty(
            candidates=FakeCandidates([_candidate()]), aborter=aborter, notifier=notifier, audit=audit
        )

        report = duty.run_once()

        assert report is not None
        self.assertEqual(aborter.calls, [], "通知没送到，绝不能尝试收口")
        self.assertEqual((report.notified, report.aborted, report.notify_failed), (0, 0, 1))
        self.assertIn("stalled_provisioning.notify_failed", audit.actions())

    def test_notify_happens_strictly_before_abort(self) -> None:
        """**顺序断言**：先通知，后收口——把顺序写反一次会让这条变红（人工验红，见 PR）。"""

        order: list[str] = []
        aborter = FakeAborter(order=order)
        notifier = FakeNotifier(order=order)
        duty, _ = build_duty(candidates=FakeCandidates([_candidate()]), aborter=aborter, notifier=notifier)

        duty.run_once()

        self.assertEqual(order, ["notify", "abort"])

    def test_notifier_not_wired_processes_nothing(self) -> None:
        """**否定断言**：缺通知出口时一条候选都不处理，绝不"改状态不告知"。"""

        candidates = FakeCandidates([_candidate()])
        aborter = FakeAborter()
        audit = RecordingAudit()
        duty, _ = build_duty(candidates=candidates, aborter=aborter, notifier=None, audit=audit)

        report = duty.run_once()

        assert report is not None
        self.assertFalse(report.notifier_wired)
        self.assertEqual(candidates.calls, [], "连候选查询都不该发起")
        self.assertEqual(aborter.calls, [])
        self.assertEqual(audit.actions(), ["stalled_provisioning.notifier_not_wired"])

    def test_stopping_mid_run_returns_none_and_touches_nothing(self) -> None:
        stop = threading.Event()
        stop.set()
        candidates = FakeCandidates([_candidate()])
        aborter = FakeAborter()
        duty, _ = build_duty(candidates=candidates, aborter=aborter, stop=stop)

        report = duty.run_once()

        self.assertIsNone(report)
        self.assertEqual(candidates.calls, [])
        self.assertEqual(aborter.calls, [])

    def test_a_stop_signal_mid_sweep_halts_further_processing(self) -> None:
        stop = threading.Event()

        class StoppingAborter(FakeAborter):
            def abort_stalled_provisioning(self, *, user_id, expected_states, reason):
                stop.set()
                return super().abort_stalled_provisioning(
                    user_id=user_id, expected_states=expected_states, reason=reason
                )

        aborter = StoppingAborter()
        candidates = FakeCandidates(
            [_candidate(user_id=USER_A, event_id="evt_a"), _candidate(user_id=USER_B, event_id="evt_b")]
        )
        duty, _ = build_duty(candidates=candidates, aborter=aborter, stop=stop)

        report = duty.run_once()

        assert report is not None
        self.assertTrue(report.interrupted)
        self.assertEqual(len(aborter.calls), 1, "停止信号落在遍历中间，后面的候选不再处理")

    def test_a_cas_that_finds_zero_rows_is_not_renotified(self) -> None:
        """**否定断言**：CAS 0 行（状态在候选查到与收口之间被别的路径改写）不重发——
        通知已经发出去了，重发只会制造第二条相互矛盾的消息。"""

        notifier = FakeNotifier()
        aborter = FakeAborter(result=False)
        audit = RecordingAudit()
        duty, _ = build_duty(
            candidates=FakeCandidates([_candidate()]), aborter=aborter, notifier=notifier, audit=audit
        )

        report = duty.run_once()

        assert report is not None
        self.assertEqual(len(notifier.sent), 1, "只发一次，不因为 CAS 被拒而重试")
        self.assertEqual(report.advance_refused, 1)
        self.assertIn("stalled_provisioning.advance_refused", audit.actions())

    def test_one_candidate_s_failure_does_not_take_down_the_round(self) -> None:
        aborter = FakeAborter(error=RuntimeError("boom"))
        candidates = FakeCandidates(
            [_candidate(user_id=USER_A, event_id="evt_a"), _candidate(user_id=USER_B, event_id="evt_b")]
        )
        audit = RecordingAudit()
        duty, _ = build_duty(candidates=candidates, aborter=aborter, audit=audit)

        report = duty.run_once()

        assert report is not None
        self.assertEqual(report.examined, 2)
        self.assertEqual(report.failed, 2, "两个候选都撞上同一个抛异常的 aborter")
        self.assertEqual(audit.actions().count("stalled_provisioning.user_failed"), 2)

    def test_audit_facts_never_contain_open_id_or_profile_values(self) -> None:
        """**否定断言**：审计只记 user_id / state / reason / trace_id / 计数，不记
        open_id、不记任何资料值。"""

        audit = RecordingAudit()
        duty, _ = build_duty(
            candidates=FakeCandidates([_candidate(open_id="ou_should_never_appear")]), audit=audit
        )

        duty.run_once()

        aborted_facts = audit.facts("stalled_provisioning.aborted")
        self.assertNotIn("open_id", aborted_facts)
        for value in aborted_facts.values():
            self.assertNotEqual(value, "ou_should_never_appear")
        completed_facts = audit.facts("stalled_provisioning.completed")
        self.assertNotIn("open_id", completed_facts)


class ConstructionTests(unittest.TestCase):
    def test_a_non_positive_lease_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            build_duty(lease_seconds=0)

    def test_a_non_positive_limit_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            build_duty(limit=0)

    def test_notifier_wired_reflects_construction(self) -> None:
        wired, _ = build_duty()
        unwired, _ = build_duty(notifier=None)
        self.assertTrue(wired.notifier_wired)
        self.assertFalse(unwired.notifier_wired)

    def test_report_carries_only_counts_and_fixed_fields(self) -> None:
        report = StalledProvisioningReport()
        self.assertEqual(
            set(report.audit_facts()),
            {
                "examined",
                "notified",
                "aborted",
                "notify_failed",
                "advance_refused",
                "failed",
                "notifier_wired",
            },
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
