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

外部独立审查 P2-2 修复的两条新增用例：**通知退避**（同一用户在退避窗口内不得被
再次尝试）与**过采样不饿死新候选**（退避期内的候选不得把 `limit` 名额占满）。
"""

from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from typing import Any

from lingxi.apps.scheduler.stalled_provisioning import (
    DEFAULT_STALLED_LEASE_SECONDS,
    DEFAULT_STALLED_LIMIT,
    STALLED_FETCH_OVERSAMPLE,
    StalledProvisioningDuty,
    StalledProvisioningReport,
)
from lingxi.core.identity.onboarding_runner import (
    KEY_STALLED,
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
        # 真实 SQL 的 ``LIMIT`` 会截断结果——不模拟这一点，退避 + 过采样的用例就
        # 测不出"查询本身取回了几条"这件事，starvation 场景会显得永远不存在。
        return tuple(self._items[:limit])


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
        #: 每一次调用（无论成败）的 open_id，供退避节奏用例断言"到底打了几次飞书"。
        self.calls: list[str] = []
        self._error = error
        self._fail_for = fail_for
        self._order = order

    def send(self, *, open_id: str, key: str, values: Any, dedupe_key: str) -> None:
        self.calls.append(open_id)
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
        "alert": None,
        # 默认 None：哨兵——不注入失败原因落库口时，行为必须与接线之前逐字节一致
        # （Issue #337）。
        "failure_reasons": None,
    }
    parts.update({key: value for key, value in overrides.items() if key in parts})
    duty = StalledProvisioningDuty(
        candidates=parts["candidates"],
        aborter=parts["aborter"],
        notifier=parts["notifier"],
        alert=parts["alert"],
        audit=parts["audit"],
        failure_reasons=parts["failure_reasons"],
        lease_seconds=overrides.get("lease_seconds", DEFAULT_STALLED_LEASE_SECONDS),
        limit=overrides.get("limit", DEFAULT_STALLED_LIMIT),
        notify_backoff_seconds=overrides.get("notify_backoff_seconds", 300),
        clock=overrides.get("clock"),
        stop=overrides.get("stop"),
    )
    return duty, parts


class RecordingFailureReasons:
    """``FailureReasonRecorder`` 的内存假实现（Issue #337）。"""

    def __init__(self, *, raise_error: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self.raise_error = raise_error

    def record_failure(self, *, trace_id: str, failure_reason: str, event_type: str) -> None:
        if self.raise_error:
            raise RuntimeError("模拟失败原因落库故障")
        self.calls.append(
            {"trace_id": trace_id, "failure_reason": failure_reason, "event_type": event_type}
        )


class FakeClock:
    """受控单调时钟：测试自己推进时间，不依赖真实 `time.monotonic`。"""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


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
            notifier.sent,
            [(OPEN_ID_A, KEY_STALLED, {"reference": "trc_a"}, "onboarding:stalled:evt_a")],
        )
        self.assertEqual(
            aborter.calls,
            [(USER_A, (STATE_PROVISIONING, STATE_MCP_SYNCING), "stalled_lease_expired")],
        )
        self.assertIn("stalled_provisioning.aborted", audit.actions())

    def test_the_dedupe_key_is_distinct_from_the_terminal_notice_key(self) -> None:
        """`onboarding:stalled:{event_id}` 刻意与终态通知的 `onboarding:{event_id}`
        不同键，两者不会互相去重掉。"""

        notifier = FakeNotifier()
        duty, _ = build_duty(
            candidates=FakeCandidates([_candidate(event_id="evt_x")]), notifier=notifier
        )

        duty.run_once()

        dedupe_key = notifier.sent[0][3]
        self.assertEqual(dedupe_key, "onboarding:stalled:evt_x")
        self.assertNotEqual(dedupe_key, "onboarding:evt_x")

    def test_the_lease_is_passed_through_and_the_query_is_oversampled(self) -> None:
        """候选查询按 `limit × STALLED_FETCH_OVERSAMPLE` 过采样（外部独立审查
        P2-2）：`limit` 本身管的是"这一轮真正处理几个"，不是"查询取几条"。"""

        candidates = FakeCandidates([])
        duty, _ = build_duty(candidates=candidates, lease_seconds=123, limit=7)

        duty.run_once()

        self.assertEqual(candidates.calls, [(123, 7 * STALLED_FETCH_OVERSAMPLE)])

    def test_the_oversample_query_is_capped(self) -> None:
        candidates = FakeCandidates([])
        duty, _ = build_duty(candidates=candidates, limit=1000)

        duty.run_once()

        self.assertEqual(candidates.calls[0][1], 200)

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
            candidates=FakeCandidates([_candidate()]),
            aborter=aborter,
            notifier=notifier,
            audit=audit,
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
        duty, _ = build_duty(
            candidates=FakeCandidates([_candidate()]), aborter=aborter, notifier=notifier
        )

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
            [
                _candidate(user_id=USER_A, event_id="evt_a"),
                _candidate(user_id=USER_B, event_id="evt_b"),
            ]
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
            candidates=FakeCandidates([_candidate()]),
            aborter=aborter,
            notifier=notifier,
            audit=audit,
        )

        report = duty.run_once()

        assert report is not None
        self.assertEqual(len(notifier.sent), 1, "只发一次，不因为 CAS 被拒而重试")
        self.assertEqual(report.advance_refused, 1)
        self.assertIn("stalled_provisioning.advance_refused", audit.actions())

    def test_one_candidate_s_failure_does_not_take_down_the_round(self) -> None:
        aborter = FakeAborter(error=RuntimeError("boom"))
        candidates = FakeCandidates(
            [
                _candidate(user_id=USER_A, event_id="evt_a"),
                _candidate(user_id=USER_B, event_id="evt_b"),
            ]
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


class NotifyBackoffTests(unittest.TestCase):
    """外部独立审查 P2-2：同一用户通知持续失败时不得每一轮 tick 都重新尝试；
    退避期内的候选也不得把 `limit` 名额占满、饿死后面真正等待处理的新候选。"""

    def test_a_repeatedly_failing_candidate_is_skipped_within_the_backoff_window(self) -> None:
        clock = FakeClock()
        notifier = FakeNotifier(error=RuntimeError("feishu down"))
        candidates = FakeCandidates([_candidate()])
        duty, _ = build_duty(
            candidates=candidates, notifier=notifier, notify_backoff_seconds=300, clock=clock
        )

        first = duty.run_once()
        clock.advance(60)  # 一个 SchedulerLoop tick 之后
        second = duty.run_once()

        assert first is not None and second is not None
        self.assertEqual(first.examined, 1, "第一轮必须真的尝试一次")
        self.assertEqual(len(notifier.calls), 1, "第二轮还在退避窗口内，不该再打一次飞书")
        self.assertEqual(second.examined, 0, "退避期内跳过，不计入本轮已处理")
        self.assertEqual(second.skipped_in_backoff, 1, "跳过的候选必须被单独计数（N-2）")

    def test_a_round_entirely_skipped_by_backoff_still_gets_an_audit_record(self) -> None:
        """**否定断言**（编排者第二轮定向复核 N-2）：一轮取到的候选全部处于退避期
        （`examined == 0`）不等于"这一轮没有任何停摆候选"——如果只用 `examined`
        做记审计的门槛，这种情况会整轮静默，运维读到"没有停摆候选"，实际是
        "有 N 个人还停在中途格，只是刚打过一次飞书"。"""

        clock = FakeClock()
        notifier = FakeNotifier(error=RuntimeError("feishu down"))
        candidates = FakeCandidates([_candidate()])
        audit = RecordingAudit()
        duty, _ = build_duty(
            candidates=candidates,
            notifier=notifier,
            notify_backoff_seconds=300,
            clock=clock,
            audit=audit,
        )

        duty.run_once()  # 第一轮：真的尝试一次，进入退避期。
        second = duty.run_once()  # 第二轮：唯一的候选还在退避期内，examined=0。

        assert second is not None
        self.assertEqual(second.examined, 0)
        self.assertEqual(second.skipped_in_backoff, 1)
        completed_records = [
            fields for action, fields in audit.records if action == "stalled_provisioning.completed"
        ]
        self.assertEqual(len(completed_records), 2, "两轮都必须各记一条完成审计，第二轮不能静默")
        self.assertEqual(
            completed_records[1],
            {
                "examined": 0,
                "notified": 0,
                "aborted": 0,
                "notify_failed": 0,
                "advance_refused": 0,
                "failed": 0,
                "skipped_in_backoff": 1,
                "silenced_system": 0,
                "notifier_wired": True,
            },
        )

    def test_the_candidate_is_retried_once_the_backoff_window_elapses(self) -> None:
        clock = FakeClock()
        notifier = FakeNotifier(error=RuntimeError("feishu down"))
        candidates = FakeCandidates([_candidate()])
        duty, _ = build_duty(
            candidates=candidates, notifier=notifier, notify_backoff_seconds=300, clock=clock
        )

        duty.run_once()
        clock.advance(301)
        second = duty.run_once()

        assert second is not None
        self.assertEqual(len(notifier.calls), 2, "退避窗口已过，必须重新尝试")
        self.assertEqual(second.examined, 1)

    def test_backed_off_poison_candidates_do_not_starve_a_fresh_one(self) -> None:
        """`limit=3`：候选查询里排在最前面的三个是持续通知失败、已进入退避期的
        "毒候选"，第四个是全新候选。**不过采样**的话查询只会取回前三个（正好是
        `limit` 条，全部在退避期内），全新候选压根不会出现在结果集里、永远轮不到；
        过采样之后查询能看到全部四个，进程内跳过退避中的三个，全新候选必须被
        处理到——这正是本用例要证明的差异。
        """

        clock = FakeClock()
        poisoned = [
            _candidate(
                user_id=f"usr_poison_{i}", open_id=f"ou_poison_{i}", event_id=f"evt_poison_{i}"
            )
            for i in range(3)
        ]
        fresh = _candidate(user_id="usr_fresh", open_id="ou_fresh", event_id="evt_fresh")
        failing_notifier = FakeNotifier(
            error=RuntimeError("feishu down"),
            fail_for=frozenset(candidate.open_id for candidate in poisoned),
        )
        candidates = FakeCandidates(list(poisoned))
        duty, _ = build_duty(
            candidates=candidates,
            notifier=failing_notifier,
            limit=3,
            notify_backoff_seconds=300,
            clock=clock,
        )

        # 第一轮：这个人还没有全新候选出现（他这时候才刚刚停摆），查询只看到三个
        # 毒候选，全部被尝试一次、通知失败、进入退避期。
        duty.run_once()
        self.assertEqual(len(failing_notifier.calls), 3, "三个毒候选第一轮都必须被尝试过一次")

        # 第二轮：候选查询现在能看到全部四个（三个毒候选依然排在最前面，因为它们
        # 的认领时刻更早）；时钟没有推进，三个毒候选仍在退避期内。
        candidates._items = [*poisoned, fresh]
        report = duty.run_once()

        assert report is not None
        self.assertIn("ou_fresh", failing_notifier.calls, "全新候选必须被真正尝试到")
        self.assertEqual(report.examined, 1, "本轮真正处理的只有全新候选，三个毒候选都被退避跳过")
        self.assertEqual(report.notify_failed, 0, "全新候选通知应当成功")
        self.assertEqual(report.notified, 1)

    def test_notify_backoff_seconds_must_be_non_negative(self) -> None:
        with self.assertRaises(ValueError):
            build_duty(notify_backoff_seconds=-1)

    def test_a_zero_backoff_retries_every_round(self) -> None:
        """退避设为 0 等于不节流——用于明确表达"这条功能可以被关掉"，不是隐藏行为。"""

        clock = FakeClock()
        notifier = FakeNotifier(error=RuntimeError("feishu down"))
        candidates = FakeCandidates([_candidate()])
        duty, _ = build_duty(
            candidates=candidates, notifier=notifier, notify_backoff_seconds=0, clock=clock
        )

        duty.run_once()
        duty.run_once()

        self.assertEqual(len(notifier.calls), 2)


class StalledAlertCallbackTests(unittest.TestCase):
    """停摆计数送达管理群（Issue #280 §7.3 步 2）：只在本轮真的收口了至少一个候选
    时上报聚合计数，不含任何单个候选的 user_id / open_id / 追溯号。"""

    def test_a_successful_round_reports_the_aborted_count(self) -> None:
        calls: list[int] = []
        candidates = FakeCandidates(
            [_candidate(), _candidate(user_id=USER_B, open_id=OPEN_ID_B, event_id="evt_b")]
        )
        duty, _ = build_duty(candidates=candidates, alert=calls.append)

        report = duty.run_once()

        assert report is not None
        self.assertEqual(report.aborted, 2)
        self.assertEqual(calls, [2], "只传聚合计数")

    def test_zero_aborted_never_calls_the_alert(self) -> None:
        """否定断言：零候选、或候选全部通知失败/CAS 被拒时，一次都不该报警——
        「有人卡住了」这句话必须是真的，不能空喊。"""

        calls: list[int] = []
        duty, _ = build_duty(alert=calls.append)  # 零候选

        duty.run_once()

        self.assertEqual(calls, [])

    def test_no_alert_injected_is_a_no_op(self) -> None:
        """默认 `alert=None`：不注入时行为与此前一致，不抛异常。"""

        candidates = FakeCandidates([_candidate()])
        duty, _ = build_duty(candidates=candidates)

        report = duty.run_once()

        assert report is not None
        self.assertEqual(report.aborted, 1)

    def test_a_raising_alert_does_not_discard_the_rounds_result(self) -> None:
        """否定断言：告警回调失败不得带走本轮已经做完的收口结果。"""

        def boom(count: int) -> None:
            raise RuntimeError("alert sink down")

        candidates = FakeCandidates([_candidate()])
        audit = RecordingAudit()
        duty, _ = build_duty(candidates=candidates, alert=boom, audit=audit)

        report = duty.run_once()

        assert report is not None
        self.assertEqual(report.aborted, 1)
        self.assertIn("stalled_provisioning.alert_callback_failed", audit.actions())


class FailureReasonRecordingTests(unittest.TestCase):
    """失败原因落库（Issue #337，可选，见 ``FailureReasonRecorder`` 协议
    文档）：紧邻既有 ``stalled_provisioning.aborted`` 审计，只在真的收口成功
    （CAS 命中）时落一行——覆盖 `onboarding.result` 从未写出的那一半（链本身
    死掉、由本职责租约到期收口）。"""

    def test_an_aborted_candidate_records_the_failure_reason(self) -> None:
        recorder = RecordingFailureReasons()
        duty, _ = build_duty(candidates=FakeCandidates([_candidate()]), failure_reasons=recorder)

        report = duty.run_once()

        assert report is not None
        self.assertEqual(report.aborted, 1)
        self.assertEqual(
            recorder.calls,
            [
                {
                    "trace_id": "trc_a",
                    "failure_reason": "stalled_lease_expired",
                    "event_type": "stalled_provisioning.aborted",
                }
            ],
        )

    def test_a_cas_refused_candidate_never_records_anything(self) -> None:
        """否定断言：CAS 返回 0 行（状态被别的路径改写）时不收口，也不该落一行
        「失败」——那个人此刻的真实状态未必真的失败了。"""

        recorder = RecordingFailureReasons()
        duty, _ = build_duty(
            candidates=FakeCandidates([_candidate()]),
            aborter=FakeAborter(result=False),
            failure_reasons=recorder,
        )

        report = duty.run_once()

        assert report is not None
        self.assertEqual(report.aborted, 0)
        self.assertEqual(recorder.calls, [])

    def test_a_notify_failed_candidate_never_records_anything(self) -> None:
        """否定断言：通知失败时不收口，同样不该落任何失败原因行。"""

        recorder = RecordingFailureReasons()
        duty, _ = build_duty(
            candidates=FakeCandidates([_candidate()]),
            notifier=FakeNotifier(error=RuntimeError("feishu down")),
            failure_reasons=recorder,
        )

        duty.run_once()

        self.assertEqual(recorder.calls, [])

    def test_a_raising_recorder_does_not_discard_the_rounds_result(self) -> None:
        """否定断言：落库失败不得带走本轮已经完成的收口结果。"""

        recorder = RecordingFailureReasons(raise_error=True)
        audit = RecordingAudit()
        duty, _ = build_duty(
            candidates=FakeCandidates([_candidate()]),
            failure_reasons=recorder,
            audit=audit,
        )

        report = duty.run_once()

        assert report is not None
        self.assertEqual(report.aborted, 1)
        self.assertIn("stalled_provisioning.failure_reason_record_failed", audit.actions())

    def test_no_recorder_injected_keeps_prior_behavior(self) -> None:
        """默认 ``failure_reasons=None``：不注入时行为与接线之前逐字节一致，
        不抛异常。"""

        duty, _ = build_duty(candidates=FakeCandidates([_candidate()]))

        report = duty.run_once()

        assert report is not None
        self.assertEqual(report.aborted, 1)


class PreprovisionSilenceTests(unittest.TestCase):
    """rc25 修复包 F3：系统触发（预开通）的停摆收口**全程静默**。

    产品负责人裁定 4：预开通期间不向用户发送任何消息；而 ``onboarding.stalled``
    文案说「你发起开通」——预开通用户没有发起过任何东西，这句对他是假话。判据
    沿用候选查询为无入站事件用户合成的 ``preprovision:<user_id>`` 事件标识
    （``is_system_trigger``）。收口写入、审计与失败原因落库照旧；用户自己发起的
    链一字不变（本文件其余用例全部跑在真实事件标识上，就是那半边的钉子）。
    """

    def test_a_preprovisioned_chain_is_closed_silently_with_zero_outbound(self) -> None:
        notifier = FakeNotifier()
        aborter = FakeAborter()
        audit = RecordingAudit()
        reasons = RecordingFailureReasons()
        duty, _ = build_duty(
            candidates=FakeCandidates(
                (_candidate(event_id="preprovision:usr_a", trace_id="preprovision:usr_a"),)
            ),
            notifier=notifier,
            aborter=aborter,
            audit=audit,
            failure_reasons=reasons,
        )

        report = duty.run_once()

        self.assertEqual(notifier.calls, [], "预开通 origin 不产生任何出站消息")
        self.assertEqual(notifier.sent, [])
        self.assertEqual(len(aborter.calls), 1, "静默不等于不收口：卡住的链照常收口")
        self.assertEqual(report.aborted, 1)
        self.assertEqual(report.silenced_system, 1)
        self.assertEqual(report.notified, 0, "没发过的消息不得计成已通知")
        self.assertIn("stalled_provisioning.aborted", audit.actions(), "审计照旧")
        self.assertEqual(len(reasons.calls), 1, "失败原因落库照旧（/admin trace 可查）")

    def test_a_user_initiated_chain_still_gets_the_notice_first(self) -> None:
        """否定断言的另一半：真实事件标识（用户自己发起）逐字走既有「先通知、送达
        才收口」路径——F3 不改这半边。"""

        notifier = FakeNotifier()
        duty, _ = build_duty(
            candidates=FakeCandidates((_candidate(event_id="evt_real"),)),
            notifier=notifier,
        )

        report = duty.run_once()

        self.assertEqual(len(notifier.sent), 1)
        self.assertEqual(report.notified, 1)
        self.assertEqual(report.silenced_system, 0)


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
                "skipped_in_backoff",
                "silenced_system",
                "notifier_wired",
            },
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
