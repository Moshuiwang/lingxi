"""首次开通编排在 scheduler 的装配用例（Epic D / S-D-02，搬迁后）。

这份用例守的是**只会在生产暴露**的那几条：Epic C 交办的三条装配断言（执行级硬截止、
探针超时与就绪节奏一致、注入单调时钟）、本轮新增的第四条（认领量必须被执行器容量压住）、
以及「前置不齐就不注册，而不是注册一个会把事件认领走再烧掉的实现」。
"""

from __future__ import annotations

import threading
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from lingxi.apps.scheduler import SchedulerConfig, _build_onboarding_duty
from lingxi.apps.scheduler.onboarding import (
    DISPATCH_AFTER,
    PROBE_WATCHDOG_MARGIN_SECONDS,
    HardDeadlineProbe,
    OnboardingExecutor,
    assert_claim_limit_follows_capacity,
    assert_probe_timeouts_agree,
    monotonic_utc_clock,
)
from lingxi.core.conversation.onboarding_recovery import OnboardingReconciler
from lingxi.core.permission.mcp_readiness import McpProbeError, ReadinessSchedule

# 32 字节 base64 主密钥（非生产值，只为过形状校验）。
MASTER_KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
BASE_ENV = {
    "LINGXI_POSTGRES_DSN": "postgresql://localhost/lingxi",
    "LINGXI_DELEGATED_CREDENTIAL_KEY": "x" * 44,
    "LINGXI_DELEGATED_CREDENTIAL_PATH": "/tmp/lingxi-credential",
    "LINGXI_FEISHU_APP_ID": "cli_test",
    "LINGXI_FEISHU_APP_SECRET": "secret",
}
WIRED_ENV = {
    **BASE_ENV,
    "LINGXI_MCP_TOKEN_ENCRYPT_KEY": MASTER_KEY,
    "LINGXI_QUERY_MCP_ENDPOINT": "https://mcp.example.internal/query",
    "LINGXI_USER_ENV_ROOT": "/var/lib/lingxi/users",
}


class RecordingAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, object]]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, dict(fields)))

    def actions(self) -> list[str]:
        return [action for action, _ in self.records]


def build(env: dict[str, str], *, token: Any = None) -> tuple[Any, RecordingAudit]:
    audit = RecordingAudit()
    duty = _build_onboarding_duty(
        SchedulerConfig.from_env(env),
        stop=threading.Event(),
        audit=audit,
        employment_access_token=token,
    )
    return duty, audit


class PrerequisiteTests(unittest.TestCase):
    """前置不齐就**不注册**，并留下恰一条审计（`V-花名册-29` 的同一条纪律）。

    不注册的后果是**没有任何人认领** `auto_provisioning` 事件——它们原样留在库里。
    这比搬迁前的失败关闭桩安全：桩会「认领即平账」，把事件永久烧掉。
    """

    def test_missing_master_key_is_reported_by_variable_name_only(self) -> None:
        duty, audit = build(BASE_ENV)
        self.assertIsNone(duty)
        self.assertEqual(audit.actions(), ["onboarding.duty_not_registered"])
        self.assertEqual(audit.records[0][1]["variable"], "LINGXI_MCP_TOKEN_ENCRYPT_KEY")

    def test_missing_query_endpoint_stops_the_assembly(self) -> None:
        duty, audit = build({**BASE_ENV, "LINGXI_MCP_TOKEN_ENCRYPT_KEY": MASTER_KEY})
        self.assertIsNone(duty)
        self.assertEqual(audit.records[0][1]["variable"], "LINGXI_QUERY_MCP_ENDPOINT")

    def test_missing_user_environment_root_stops_the_assembly(self) -> None:
        env = {
            **BASE_ENV,
            "LINGXI_MCP_TOKEN_ENCRYPT_KEY": MASTER_KEY,
            "LINGXI_QUERY_MCP_ENDPOINT": "https://mcp.example.internal/query",
        }
        duty, audit = build(env)
        self.assertIsNone(duty)
        self.assertEqual(audit.records[0][1]["variable"], "LINGXI_USER_ENV_ROOT")

    def test_an_unwired_employment_token_supply_stops_the_assembly(self) -> None:
        """在职状态是产品合同的硬门槛（`V-开通-07`），不能「先跳过这一步」。"""

        duty, audit = build(WIRED_ENV)
        self.assertIsNone(duty)
        self.assertEqual(audit.actions(), ["onboarding.duty_not_registered"])
        self.assertEqual(audit.records[0][1]["reason"], "employment_access_token_unwired")

    def test_a_fully_wired_assembly_produces_a_claiming_duty(self) -> None:
        duty, audit = build(WIRED_ENV, token=lambda: "u-token")

        self.assertIsInstance(duty, OnboardingReconciler)
        self.assertEqual(audit.actions(), [], "装配成功时不该留「未装配」审计")
        self.assertIsNotNone(duty.capacity_source, "认领量必须绑上执行器剩余容量")


class ClaimCapacityTests(unittest.TestCase):
    """装配断言 4：认领量必须被执行器剩余容量压住。"""

    def test_an_unbound_claim_limit_fails_the_assembly(self) -> None:
        executor = OnboardingExecutor(workers=2)
        duty = OnboardingReconciler(
            store=object(), onboarding=object(), audit=RecordingAudit()
        )
        with self.assertRaises(RuntimeError):
            assert_claim_limit_follows_capacity(duty, executor)

    def test_binding_a_different_executor_fails_the_assembly(self) -> None:
        mine = OnboardingExecutor(workers=2)
        other = OnboardingExecutor(workers=2)
        duty = OnboardingReconciler(
            store=object(),
            onboarding=object(),
            audit=RecordingAudit(),
            capacity=other.free_slots,
        )
        with self.assertRaises(RuntimeError):
            assert_claim_limit_follows_capacity(duty, mine)

    def test_the_correct_binding_passes(self) -> None:
        executor = OnboardingExecutor(workers=2)
        duty = OnboardingReconciler(
            store=object(),
            onboarding=object(),
            audit=RecordingAudit(),
            capacity=executor.free_slots,
        )
        assert_claim_limit_follows_capacity(duty, executor)

    def test_free_slots_shrinks_as_the_queue_fills(self) -> None:
        executor = OnboardingExecutor(workers=2, backlog=3)

        self.assertEqual(executor.free_slots(), 3)
        executor.submit(lambda: None)
        self.assertEqual(executor.free_slots(), 2)
        executor.submit(lambda: None)
        executor.submit(lambda: None)
        self.assertEqual(executor.free_slots(), 0)
        self.assertFalse(executor.submit(lambda: None))

    def test_a_stopping_executor_reports_no_free_slots(self) -> None:
        stopping = {"value": False}
        executor = OnboardingExecutor(workers=2, should_stop=lambda: stopping["value"])

        self.assertGreater(executor.free_slots(), 0)
        stopping["value"] = True
        self.assertEqual(executor.free_slots(), 0)
        self.assertFalse(executor.submit(lambda: None))


class DispatchWindowTests(unittest.TestCase):
    """搬迁之后认领窗口只剩一个用途：让 gateway 的第一条提示先落地。"""

    def test_the_window_is_seconds_not_the_old_half_hour(self) -> None:
        # 旧窗口是三十分钟，为的是不把「正在同步的那条」判成孤儿。搬迁之后认领即记账，
        # 正在跑的那条压根不在候选里，因此窗口不再需要覆盖十五分钟的同步预算。
        self.assertLess(DISPATCH_AFTER, timedelta(minutes=1))
        self.assertGreater(DISPATCH_AFTER, timedelta(0))


class HandoffAssertionTests(unittest.TestCase):
    """Epic C 交办的三条装配断言。"""

    def test_a_probe_that_never_returns_is_cut_off_at_the_execution_level(self) -> None:
        """①执行级硬截止：就绪状态机管得住「返回得太晚」，管不住「永远不返回」。"""

        started = threading.Event()
        release = threading.Event()

        class HangingProbe:
            timeout_seconds = 1

            def list_metrics(self, *, user_id: str) -> int:
                started.set()
                release.wait(timeout=10)
                return 3

        guarded = HardDeadlineProbe(probe=HangingProbe(), timeout_seconds=0.2)
        try:
            with self.assertRaises(McpProbeError) as caught:
                guarded.list_metrics(user_id="usr_1")
            self.assertTrue(started.is_set())
            self.assertEqual(caught.exception.code, "probe_hard_deadline_exceeded")
            self.assertFalse(caught.exception.denied, "硬截止必须落技术失败，不是「MCP 拒绝了」")
        finally:
            release.set()

    def test_a_normal_probe_passes_through_untouched(self) -> None:
        class Probe:
            timeout_seconds = 20

            def list_metrics(self, *, user_id: str) -> int:
                return 5

        guarded = HardDeadlineProbe(probe=Probe(), timeout_seconds=5)
        self.assertEqual(guarded.list_metrics(user_id="usr_1"), 5)
        self.assertEqual(guarded.timeout_seconds, 20, "传输超时必须原样透出，供断言②比对")

    def test_a_probe_error_is_re_raised_unchanged(self) -> None:
        class Probe:
            timeout_seconds = 20

            def list_metrics(self, *, user_id: str) -> int:
                raise McpProbeError("denied_by_mcp", denied=True)

        guarded = HardDeadlineProbe(probe=Probe(), timeout_seconds=5)
        with self.assertRaises(McpProbeError) as caught:
            guarded.list_metrics(user_id="usr_1")
        self.assertTrue(caught.exception.denied, "看门狗不得把明确拒绝改写成技术失败")

    def test_mismatched_probe_timeouts_fail_the_assembly(self) -> None:
        """②传输超时必须与就绪节奏的单次超时逐值相等。"""

        class Probe:
            timeout_seconds = 30

        with self.assertRaises(RuntimeError):
            assert_probe_timeouts_agree(probe=Probe(), schedule=ReadinessSchedule())

    def test_matching_probe_timeouts_pass(self) -> None:
        class Probe:
            timeout_seconds = 20

        assert_probe_timeouts_agree(probe=Probe(), schedule=ReadinessSchedule())

    def test_the_schedule_itself_refuses_a_timeout_longer_than_the_interval(self) -> None:
        with self.assertRaises(ValueError):
            ReadinessSchedule(interval_seconds=10, budget_seconds=100, probe_timeout_seconds=30)

    def test_the_injected_clock_never_goes_backwards(self) -> None:
        """③注入单调时钟：墙钟回拨会让一次已经超窗的成功被算成有效。"""

        clock = monotonic_utc_clock()
        first = clock()
        second = clock()
        self.assertIsNotNone(first.tzinfo)
        self.assertGreaterEqual(second, first)
        self.assertLess(abs(second - datetime.now(timezone.utc)), timedelta(seconds=5))

    def test_the_watchdog_margin_is_above_the_transport_timeout(self) -> None:
        """看门狗只该在传输层**根本不遵守**超时时动手，不该抢在它前面。"""

        self.assertGreater(PROBE_WATCHDOG_MARGIN_SECONDS, 0)


class ExecutorTests(unittest.TestCase):
    def test_zero_workers_is_refused(self) -> None:
        for value in (0, -1, True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    OnboardingExecutor(workers=value)  # type: ignore[arg-type]

    def test_a_task_failure_does_not_kill_the_worker_thread(self) -> None:
        executor = OnboardingExecutor(workers=1, backlog=4)
        executor.start()
        self.addCleanup(executor.stop)
        done = threading.Event()

        self.assertTrue(executor.submit(lambda: (_ for _ in ()).throw(RuntimeError("boom"))))
        self.assertTrue(executor.submit(done.set))
        self.assertTrue(done.wait(timeout=5), "一条链的失败不得带走执行线程")

    def test_work_actually_runs_on_another_thread(self) -> None:
        executor = OnboardingExecutor(workers=1, backlog=4)
        executor.start()
        self.addCleanup(executor.stop)
        seen: list[int] = []
        done = threading.Event()

        def task() -> None:
            seen.append(threading.get_ident())
            done.set()

        executor.submit(task)
        self.assertTrue(done.wait(timeout=5))
        self.assertNotEqual(seen[0], threading.get_ident())

    def test_stopping_does_not_discard_already_queued_work(self) -> None:
        """已排队的任务对应的事件**已经被记账**，丢掉就是永久烧掉。

        它们照常出队，各自在第一步看到停机标志、中止并把认领放回去。
        """

        executor = OnboardingExecutor(workers=1, backlog=4)
        ran = threading.Event()
        executor.submit(ran.set)
        executor.stop()
        executor.start()
        self.addCleanup(executor.join, 2.0)

        self.assertTrue(ran.wait(timeout=5), "停机不得丢弃已排队的任务")


class SchedulerConfigTests(unittest.TestCase):
    def test_defaults_keep_the_duty_optional(self) -> None:
        config = SchedulerConfig.from_env(BASE_ENV)
        self.assertIsNone(config.user_env_root)
        self.assertGreater(config.onboarding_workers, 0)

    def test_the_worker_count_is_bounded(self) -> None:
        with self.assertRaises(ValueError):
            SchedulerConfig.from_env({**BASE_ENV, "LINGXI_ONBOARDING_WORKERS": "0"})
        with self.assertRaises(ValueError):
            SchedulerConfig.from_env({**BASE_ENV, "LINGXI_ONBOARDING_WORKERS": "9999"})
        with self.assertRaises(ValueError):
            SchedulerConfig.from_env({**BASE_ENV, "LINGXI_ONBOARDING_WORKERS": "x"})

    def test_the_worker_count_is_configurable(self) -> None:
        config = SchedulerConfig.from_env({**BASE_ENV, "LINGXI_ONBOARDING_WORKERS": "16"})
        self.assertEqual(config.onboarding_workers, 16)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
