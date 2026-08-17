"""未开通首聊交接对账扫描的可注入断言（Issue #65 轻审 P2-2）。

判定面落在既有 `V-开通-14`（重试、并发、重启不重复创建用户 / 环境 / 权限）与
`V-开通-10`（首条只启动一次、正文不进编排）上，不新增断言编号：对账扫描要么把一条
从没交出去的事件补交一次，要么什么都不做——它不允许成为"同一条事件被开通两次"的
新来源，也不允许把用户正文带进编排。

真库那一面（迁移 0062 的账本列与原子认领）在 ``test_gateway_postgres.py``。
"""

from __future__ import annotations

import unittest
from datetime import timedelta

from gateway_fakes import CallLog, FakeAudit, FakeOnboarding, FakeState, FakeStore
from lingxi.core.conversation.onboarding_recovery import (
    DEFAULT_MAX_PER_SWEEP,
    DEFAULT_MIN_INTERVAL_SECONDS,
    DEFAULT_STALE_AFTER,
    OnboardingReconciler,
)
from lingxi.core.conversation.ports import (
    OnboardingResult,
    OnboardingState,
    PendingOnboarding,
)


def orphan(index: int = 1) -> PendingOnboarding:
    return PendingOnboarding(
        event_id=f"evt_orphan_{index}",
        open_id=f"ou_orphan_{index}",
        trace_id=f"trc_orphan_{index}",
    )


class ReconcilerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.log = CallLog()
        self.state = FakeState()
        # 时钟由测试驱动：真实实现用 ``time.monotonic`` 自限扫描频率，断言不该靠 sleep。
        self.now = 1000.0

    def build(
        self,
        *,
        onboarding=None,
        should_stop=None,
        fail_on: str | None = None,
        **kwargs,
    ) -> OnboardingReconciler:
        return OnboardingReconciler(
            store=FakeStore(self.state, self.log, fail_on=fail_on),
            onboarding=onboarding or FakeOnboarding(),
            audit=FakeAudit(self.log),
            should_stop=should_stop,
            monotonic=lambda: self.now,
            **kwargs,
        )


class OrphanRecoveryTests(ReconcilerTestCase):
    """一条从没交出去的事件必须被补交一次，且只补一次。"""

    def test_orphan_is_handed_to_the_runner_with_identity_only(self) -> None:
        self.state.stale_onboardings.append(orphan())
        runner = FakeOnboarding(result=OnboardingResult(state=OnboardingState.NOT_AUTHORIZED))

        recovered = self.build(onboarding=runner).run_once()

        self.assertEqual(recovered, 1)
        self.assertEqual(
            runner.calls,
            [
                {
                    "event_id": "evt_orphan_1",
                    "open_id": "ou_orphan_1",
                    "trace_id": "trc_orphan_1",
                }
            ],
            "补交与首次触发走同一条边界：只有事件身份，没有用户正文",
        )
        reconciled = self.log.fields("audit.onboarding.reconciled")
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0]["state"], "not_authorized")
        self.assertIs(
            reconciled[0]["notified"],
            False,
            "这条路径没有用户可见提示，审计必须如实标注，不能让待办只活在文档里",
        )

    def test_a_recovered_orphan_is_not_handed_over_twice(self) -> None:
        self.state.stale_onboardings.append(orphan())
        runner = FakeOnboarding()
        reconciler = self.build(onboarding=runner)

        first = reconciler.run_once()
        self.now += DEFAULT_MIN_INTERVAL_SECONDS
        second = reconciler.run_once()

        self.assertEqual((first, second), (1, 0))
        self.assertEqual(len(runner.calls), 1, "认领即记账：重复扫描不得重复触发开通")

    def test_no_orphan_returns_zero_not_none(self) -> None:
        """「跑了但没有孤儿」与「这一轮根本没跑」必须能区分开。"""

        self.assertEqual(self.build().run_once(), 0)

    def test_one_sweep_stops_at_the_cap(self) -> None:
        self.state.stale_onboardings.extend(orphan(index) for index in range(30))

        recovered = self.build().run_once()

        self.assertEqual(recovered, DEFAULT_MAX_PER_SWEEP)
        self.assertEqual(
            len(self.state.stale_onboardings),
            30 - DEFAULT_MAX_PER_SWEEP,
            "剩下的必须留在库里等下一轮，不能在一轮里把调用它的循环占死",
        )


class ShutdownTests(ReconcilerTestCase):
    """停机语义：随时可停，且停下来不丢事件。"""

    def test_stopping_skips_the_sweep_entirely(self) -> None:
        self.state.stale_onboardings.append(orphan())
        runner = FakeOnboarding()

        recovered = self.build(onboarding=runner, should_stop=lambda: True).run_once()

        self.assertIsNone(recovered)
        self.assertEqual(self.log.count("store.claim_stale_onboarding"), 0)
        self.assertEqual(runner.calls, [])

    def test_a_stop_between_items_leaves_the_rest_unclaimed(self) -> None:
        self.state.stale_onboardings.extend(orphan(index) for index in range(3))
        stopping = [False]

        class StoppingRunner:
            """第一条交接完成的同时收到停机信号，模拟"扫描跑到一半被叫停"。"""

            def __init__(self) -> None:
                self.calls: list[dict[str, str]] = []

            def start(self, *, event_id: str, open_id: str, trace_id: str):
                self.calls.append(
                    {"event_id": event_id, "open_id": open_id, "trace_id": trace_id}
                )
                stopping[0] = True
                return OnboardingResult(state=OnboardingState.NOT_AUTHORIZED)

        runner = StoppingRunner()

        recovered = self.build(onboarding=runner, should_stop=lambda: stopping[0]).run_once()

        self.assertEqual(recovered, 1)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(
            len(self.state.stale_onboardings),
            2,
            "停机信号到达之后不得再认领——认领即记账，认了却不交就永远没人再看",
        )


class FailureTests(ReconcilerTestCase):
    """失败必须响亮，且不得变成对外部系统的无限重试。"""

    def test_runner_failure_is_audited_and_not_retried(self) -> None:
        self.state.stale_onboardings.append(orphan())
        runner = FakeOnboarding(fail_with=RuntimeError("外部权限服务不可用"))
        reconciler = self.build(onboarding=runner)

        recovered = reconciler.run_once()
        self.now += DEFAULT_MIN_INTERVAL_SECONDS
        again = reconciler.run_once()

        self.assertEqual((recovered, again), (1, 0))
        failures = self.log.fields("audit.onboarding.reconcile_failed")
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["error"], "RuntimeError")
        self.assertEqual(self.log.count("audit.onboarding.reconciled"), 0)
        self.assertNotIn("外部权限服务不可用", repr(self.log.entries))

    def test_an_invalid_runner_result_is_a_failure_not_a_success(self) -> None:
        self.state.stale_onboardings.append(orphan())

        class BogusRunner:
            def start(self, *, event_id: str, open_id: str, trace_id: str):
                return "已开通"

        self.build(onboarding=BogusRunner()).run_once()

        self.assertEqual(self.log.count("audit.onboarding.reconcile_failed"), 1)
        self.assertEqual(self.log.count("audit.onboarding.reconciled"), 0)

    def test_a_scan_failure_ends_the_round_without_raising(self) -> None:
        self.state.stale_onboardings.append(orphan())
        runner = FakeOnboarding()

        recovered = self.build(onboarding=runner, fail_on="claim_stale_onboarding").run_once()

        self.assertEqual(recovered, 0)
        self.assertEqual(self.log.count("audit.onboarding.reconcile_scan_failed"), 1)
        self.assertEqual(runner.calls, [])


class SweepIntervalTests(ReconcilerTestCase):
    """挂在一秒一轮的投递循环上，自限成分钟级，不做每秒一次的空查询。"""

    def test_a_second_sweep_within_the_interval_does_not_touch_the_store(self) -> None:
        reconciler = self.build()

        self.assertEqual(reconciler.run_once(), 0)
        calls_after_first = self.log.count("store.claim_stale_onboarding")
        self.now += DEFAULT_MIN_INTERVAL_SECONDS / 2

        self.assertIsNone(reconciler.run_once())
        self.assertEqual(self.log.count("store.claim_stale_onboarding"), calls_after_first)

    def test_the_next_sweep_runs_once_the_interval_elapsed(self) -> None:
        reconciler = self.build()
        reconciler.run_once()
        self.now += DEFAULT_MIN_INTERVAL_SECONDS

        self.assertEqual(reconciler.run_once(), 0)


class WindowTests(unittest.TestCase):
    """对账窗口必须宽于在途的正常调用，否则对账自己就是重复触发的来源。"""

    def test_the_stale_window_exceeds_the_contract_sync_ceiling(self) -> None:
        # 产品合同允许权限同步最长等到十五分钟；窗口短于它会把正在正常执行的开通
        # 判成孤儿，再并发触发一次。
        self.assertGreater(DEFAULT_STALE_AFTER, timedelta(minutes=15))

    def test_the_reconciler_asks_the_store_for_that_window(self) -> None:
        log = CallLog()
        state = FakeState()
        state.stale_onboardings.append(orphan())
        OnboardingReconciler(
            store=FakeStore(state, log),
            onboarding=FakeOnboarding(),
            audit=FakeAudit(log),
            monotonic=lambda: 0.0,
        ).run_once()

        self.assertEqual(
            [fields["older_than"] for fields in log.fields("store.claim_stale_onboarding")][0],
            DEFAULT_STALE_AFTER,
        )


if __name__ == "__main__":
    unittest.main()
