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
        reconciled = self.log.fields("audit.onboarding.dispatched")
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0]["event_id"], "evt_orphan_1")
        self.assertEqual(reconciled[0]["state"], "not_authorized")

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

            def start(self, *, event_id: str, open_id: str, trace_id: str, claim_token=None):
                del claim_token
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

    def test_a_runner_that_never_started_gets_its_claim_released(self) -> None:
        """``start`` 自己抛异常 = 编排根本没跑：认领必须放回去，否则事件被永久烧掉。"""

        self.state.stale_onboardings.append(orphan())
        runner = FakeOnboarding(fail_with=RuntimeError("外部权限服务不可用"))
        reconciler = self.build(onboarding=runner)

        recovered = reconciler.run_once()

        self.assertEqual(recovered, 1)
        failures = self.log.fields("audit.onboarding.dispatch_failed")
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["error"], "RuntimeError")
        self.assertEqual(self.log.count("audit.onboarding.dispatched"), 0)
        self.assertEqual(self.log.count("store.release_onboarding_claim"), 1)
        self.assertEqual(set(self.state.onboarding_dispatched), set(), "放回之后账本必须是空的")
        self.assertNotIn("外部权限服务不可用", repr(self.log.entries))

    def test_a_terminal_conclusion_is_not_retried(self) -> None:
        """跑过了、得到了结论（哪怕是失败终态）就**不**放回：重跑不会改变结论。"""

        self.state.stale_onboardings.append(orphan())
        runner = FakeOnboarding(result=OnboardingResult(state=OnboardingState.NOT_AUTHORIZED))

        self.build(onboarding=runner).run_once()

        self.assertEqual(self.log.count("store.release_onboarding_claim"), 0)
        self.assertEqual(self.log.count("audit.onboarding.dispatched"), 1)

    def test_a_declined_dispatch_gets_its_claim_released(self) -> None:
        """执行器满位 / 停机 / 同一个人已有链在跑：三条都是"没跑成"，都要放回。"""

        for reason in ("executor_unavailable", "stopping", "already_running"):
            with self.subTest(reason=reason):
                self.setUp()
                self.state.stale_onboardings.append(orphan())
                runner = FakeOnboarding(
                    result=OnboardingResult(
                        state=OnboardingState.STARTED, failure_reason=reason
                    )
                )

                self.build(onboarding=runner).run_once()

                declined = self.log.fields("audit.onboarding.dispatch_declined_failed")
                self.assertEqual(len(declined), 1)
                self.assertEqual(declined[0]["reason"], reason)
                self.assertEqual(self.log.count("store.release_onboarding_claim"), 1)
                self.assertEqual(set(self.state.onboarding_dispatched), set())

    def test_a_started_dispatch_without_a_reason_is_not_released(self) -> None:
        """异步接手成功（``started`` 且没有原因码）不放回——链正在跑。"""

        self.state.stale_onboardings.append(orphan())
        runner = FakeOnboarding(result=OnboardingResult(state=OnboardingState.STARTED))

        self.build(onboarding=runner).run_once()

        self.assertEqual(self.log.count("store.release_onboarding_claim"), 0)

    def test_a_failing_release_only_degrades_this_round(self) -> None:
        self.state.stale_onboardings.append(orphan())
        runner = FakeOnboarding(fail_with=RuntimeError("boom"))

        self.build(onboarding=runner, fail_on="release_onboarding_claim").run_once()

        self.assertEqual(self.log.count("audit.onboarding.release_claim_failed"), 1)

    def test_an_invalid_runner_result_is_a_failure_not_a_success(self) -> None:
        self.state.stale_onboardings.append(orphan())

        class BogusRunner:
            def start(self, *, event_id: str, open_id: str, trace_id: str, claim_token=None):
                return "已开通"

        self.build(onboarding=BogusRunner()).run_once()

        self.assertEqual(self.log.count("audit.onboarding.dispatch_failed"), 1)
        self.assertEqual(self.log.count("audit.onboarding.dispatched"), 0)

    def test_a_scan_failure_ends_the_round_without_raising(self) -> None:
        self.state.stale_onboardings.append(orphan())
        runner = FakeOnboarding()

        recovered = self.build(onboarding=runner, fail_on="claim_stale_onboarding").run_once()

        self.assertEqual(recovered, 0)
        self.assertEqual(self.log.count("audit.onboarding.reconcile_scan_failed"), 1)
        self.assertEqual(runner.calls, [])


class CapacityCouplingTests(ReconcilerTestCase):
    """认领量必须被执行器剩余容量压住（Epic D / S-D-02 修复包 P1-并发-1）。

    认领即记账，而执行器满位时只能拒绝。两者不联动时差额就是被**永久烧掉**的事件数：
    默认组合曾经是「一轮最多认领 20 条、执行器容量 12」，第 13 条起必然如此。
    """

    def test_the_sweep_claims_no_more_than_the_capacity(self) -> None:
        for index in range(10):
            self.state.stale_onboardings.append(orphan(index))
        runner = FakeOnboarding()

        recovered = self.build(onboarding=runner, capacity=lambda: 3).run_once()

        self.assertEqual(recovered, 3)
        self.assertEqual(len(runner.calls), 3)
        self.assertEqual(
            len(self.state.stale_onboardings), 7, "没被认领的那些必须原样留在候选里"
        )

    def test_zero_capacity_claims_nothing_at_all(self) -> None:
        self.state.stale_onboardings.append(orphan())
        runner = FakeOnboarding()

        recovered = self.build(onboarding=runner, capacity=lambda: 0).run_once()

        self.assertEqual(recovered, 0)
        self.assertEqual(self.log.count("store.claim_stale_onboarding"), 0)
        self.assertEqual(runner.calls, [])

    def test_the_capacity_source_is_readable_for_the_assembly_assertion(self) -> None:
        source = (lambda: 5)
        self.assertIs(self.build(capacity=source).capacity_source, source)
        self.assertIsNone(self.build().capacity_source)


class ClaimGenerationTests(ReconcilerTestCase):
    """释放必须带认领代次，只能撤销**自己那一次**（ABA）。

    没有代次时的序列：A 释放 → B 重新认领 → A 的重试再释放一次 → **B 的认领被清掉**，
    那条链于是在没人看着的情况下被第三方解锁，可能被并发认领两次。
    """

    def test_the_runner_receives_the_claim_generation(self) -> None:
        self.state.stale_onboardings.append(orphan())
        runner = FakeOnboarding()

        self.build(onboarding=runner).run_once()

        self.assertEqual(len(runner.claim_tokens), 1)
        self.assertIsNotNone(runner.claim_tokens[0], "认领代次必须一路传给编排")

    def test_the_release_carries_the_generation_it_claimed(self) -> None:
        self.state.stale_onboardings.append(orphan())
        runner = FakeOnboarding(
            result=OnboardingResult(state=OnboardingState.STARTED, failure_reason="stopping")
        )

        self.build(onboarding=runner).run_once()

        released = self.log.fields("store.release_onboarding_claim")
        self.assertEqual(len(released), 1)
        self.assertEqual(released[0]["claim_token"], runner.claim_tokens[0])

    def test_a_stale_release_cannot_undo_somebody_elses_claim(self) -> None:
        """A 释放 → B 认领 → A 重试释放：B 的认领必须还在。"""

        self.state.stale_onboardings.append(orphan())
        store = FakeStore(self.state, self.log)

        first = store.claim_stale_onboarding(older_than=DEFAULT_STALE_AFTER)
        assert first is not None
        store.release_onboarding_claim(
            event_id=first.event_id, claim_token=first.claim_token
        )
        second = store.claim_stale_onboarding(older_than=DEFAULT_STALE_AFTER)
        assert second is not None
        self.assertNotEqual(second.claim_token, first.claim_token)

        # A 的重试：拿旧代次再释放一次。
        store.release_onboarding_claim(
            event_id=first.event_id, claim_token=first.claim_token
        )

        self.assertIn(
            second.event_id,
            self.state.onboarding_dispatched,
            "陈旧的释放不得撤销别人的认领",
        )

    def test_a_release_without_a_generation_does_nothing(self) -> None:
        """宁可留着不放，也不能撤销一次不知道是谁的认领。"""

        self.state.stale_onboardings.append(orphan())
        store = FakeStore(self.state, self.log)
        claimed = store.claim_stale_onboarding(older_than=DEFAULT_STALE_AFTER)
        assert claimed is not None

        store.release_onboarding_claim(event_id=claimed.event_id)

        self.assertIn(claimed.event_id, self.state.onboarding_dispatched)


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
