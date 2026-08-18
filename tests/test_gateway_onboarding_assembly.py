"""首次开通编排在 gateway 的装配用例（Epic D / S-D-02）。

这份用例守的是**只会在生产暴露**的那几条：双注入点必须喂同一个 runner 实例；Epic C
在 PR #218 / #221 交办的三条装配断言（执行级硬截止、探针超时与就绪节奏一致、注入单调
时钟）；前置不齐时不装配且**恰一条**审计；以及 `started` 不得被 gateway 提前记账。
"""

from __future__ import annotations

import threading
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from lingxi.apps.gateway import _UnavailableOnboarding, build_onboarding_reconciler, build_supervisor
from lingxi.apps.gateway.config import GatewayConfig, GatewayConfigError, _Secret, load_config
from lingxi.apps.gateway.onboarding import (
    PROBE_WATCHDOG_MARGIN_SECONDS,
    HardDeadlineProbe,
    OnboardingExecutor,
    _assert_probe_timeouts_agree,
    assert_single_onboarding_runner,
    build_onboarding_runner,
    monotonic_utc_clock,
)
from lingxi.core.conversation.ports import OnboardingResult, OnboardingState
from lingxi.core.permission.mcp_readiness import McpProbeError, ReadinessSchedule

BASE_ENV = {
    "LINGXI_GATEWAY_APP_ID": "cli_test",
    "LINGXI_GATEWAY_APP_SECRET": "secret",
    "LINGXI_GATEWAY_POSTGRES_DSN": "postgresql://localhost/lingxi",
}
# 32 字节 base64 主密钥（非生产值，只为过形状校验）。
MASTER_KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
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


class StubRunner:
    def start(self, *, event_id: str, open_id: str, trace_id: str) -> OnboardingResult:
        del event_id, open_id, trace_id
        return OnboardingResult(state=OnboardingState.STARTED)


class DoubleInjectionPointTests(unittest.TestCase):
    """#65 开工卡必含项（PR #205 二级审查 P3-2）：两处必须是**同一个**实例。"""

    def test_two_different_instances_fail_the_assembly(self) -> None:
        with self.assertRaises(RuntimeError):
            assert_single_onboarding_runner(StubRunner(), StubRunner())

    def test_the_same_instance_passes(self) -> None:
        runner = StubRunner()
        assert_single_onboarding_runner(runner, runner)

    def test_a_single_argument_is_refused_so_the_assertion_cannot_degrade(self) -> None:
        """只拿到一路时断言会退化成永远成立的空话，因此直接判失败。"""

        with self.assertRaises(RuntimeError):
            assert_single_onboarding_runner(StubRunner())

    def test_the_two_builders_report_the_runner_they_actually_used(self) -> None:
        """把桩落回默认的那条路径必须被这条断言抓住。"""

        config = load_config(BASE_ENV)
        runner = StubRunner()
        reported: list[Any] = []
        build_supervisor(
            config,
            transport=object(),
            onboarding=runner,
            on_onboarding_assembled=reported.append,
        )
        reconciler = build_onboarding_reconciler(config, onboarding=runner)

        self.assertIs(reported[0], runner)
        self.assertIs(reconciler.onboarding, runner)
        assert_single_onboarding_runner(reported[0], reconciler.onboarding)

    def test_forgetting_one_injection_point_is_caught(self) -> None:
        """变异形状：只喂 supervisor，对账那一路落回失败关闭桩。"""

        config = load_config(BASE_ENV)
        runner = StubRunner()
        reported: list[Any] = []
        build_supervisor(
            config,
            transport=object(),
            onboarding=runner,
            on_onboarding_assembled=reported.append,
        )
        reconciler = build_onboarding_reconciler(config)  # 漏喂

        self.assertIsInstance(reconciler.onboarding, _UnavailableOnboarding)
        with self.assertRaises(RuntimeError):
            assert_single_onboarding_runner(reported[0], reconciler.onboarding)


class PrerequisiteTests(unittest.TestCase):
    """前置不齐就**不装配**，并留下恰一条审计（`V-花名册-29` 的同一条纪律）。"""

    def _build(self, env: dict[str, str]) -> tuple[Any, RecordingAudit]:
        audit = RecordingAudit()
        wired = build_onboarding_runner(
            load_config(env), audit=audit, should_stop=lambda: False
        )
        return wired, audit

    def test_missing_master_key_is_reported_by_variable_name_only(self) -> None:
        wired, audit = self._build(BASE_ENV)
        self.assertIsNone(wired)
        self.assertEqual(audit.actions(), ["onboarding.runner_not_wired"])
        self.assertEqual(audit.records[0][1]["variable"], "LINGXI_MCP_TOKEN_ENCRYPT_KEY")

    def test_missing_query_endpoint_stops_the_assembly(self) -> None:
        env = {**BASE_ENV, "LINGXI_MCP_TOKEN_ENCRYPT_KEY": MASTER_KEY}
        wired, audit = self._build(env)
        self.assertIsNone(wired)
        self.assertEqual(audit.records[0][1]["variable"], "LINGXI_QUERY_MCP_ENDPOINT")

    def test_missing_user_environment_root_stops_the_assembly(self) -> None:
        env = {
            **BASE_ENV,
            "LINGXI_MCP_TOKEN_ENCRYPT_KEY": MASTER_KEY,
            "LINGXI_QUERY_MCP_ENDPOINT": "https://mcp.example.internal/query",
        }
        wired, audit = self._build(env)
        self.assertIsNone(wired)
        self.assertEqual(audit.records[0][1]["variable"], "LINGXI_USER_ENV_ROOT")

    def test_an_unwired_employment_token_supply_stops_the_assembly(self) -> None:
        """在职状态是产品合同的硬门槛（`V-开通-07`），不能"先跳过这一步"。"""

        wired, audit = self._build(WIRED_ENV)
        self.assertIsNone(wired)
        self.assertEqual(audit.actions(), ["onboarding.runner_not_wired"])
        self.assertEqual(audit.records[0][1]["reason"], "employment_access_token_unwired")

    def test_a_fully_wired_assembly_produces_a_runner_and_its_executor(self) -> None:
        audit = RecordingAudit()
        wired = build_onboarding_runner(
            load_config(WIRED_ENV),
            audit=audit,
            should_stop=lambda: False,
            employment_access_token=lambda: "u-token",
        )
        self.assertIsNotNone(wired)
        assert wired is not None
        self.assertEqual(audit.actions(), [], "装配成功时不该留「未装配」审计")
        self.assertIsInstance(wired.executor, OnboardingExecutor)


class ConfigTests(unittest.TestCase):
    def test_a_plain_http_query_endpoint_fails_at_startup(self) -> None:
        with self.assertRaises(GatewayConfigError):
            load_config({**BASE_ENV, "LINGXI_QUERY_MCP_ENDPOINT": "http://mcp.example.internal"})

    def test_a_malformed_master_key_fails_at_startup(self) -> None:
        with self.assertRaises(GatewayConfigError):
            load_config({**BASE_ENV, "LINGXI_MCP_TOKEN_ENCRYPT_KEY": "too-short"})

    def test_an_out_of_range_probe_timeout_fails_at_startup(self) -> None:
        with self.assertRaises(GatewayConfigError):
            load_config({**BASE_ENV, "LINGXI_QUERY_MCP_TIMEOUT_SECONDS": "100000"})

    def test_the_shared_variables_have_no_gateway_prefix(self) -> None:
        """同一把主密钥在两个进程里必须是同一个值，加前缀会造出两份可漂移的配置。"""

        config = load_config({**BASE_ENV, "LINGXI_GATEWAY_MCP_TOKEN_ENCRYPT_KEY": MASTER_KEY})
        self.assertIsNone(config.mcp_token_encrypt_key)


class HandoffAssertionTests(unittest.TestCase):
    """Epic C 交办的三条装配断言。"""

    def test_a_probe_that_never_returns_is_cut_off_at_the_execution_level(self) -> None:
        """①执行级硬截止：就绪状态机管得住"返回得太晚"，管不住"永远不返回"。"""

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
            _assert_probe_timeouts_agree(probe=Probe(), schedule=ReadinessSchedule())

    def test_matching_probe_timeouts_pass(self) -> None:
        class Probe:
            timeout_seconds = 20

        _assert_probe_timeouts_agree(probe=Probe(), schedule=ReadinessSchedule())

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
    def test_a_saturated_queue_refuses_instead_of_blocking_the_caller(self) -> None:
        executor = OnboardingExecutor(workers=1, backlog=1)
        self.assertTrue(executor.submit(lambda: None))
        self.assertFalse(executor.submit(lambda: None), "队列满必须拒绝，不能阻塞长连接线程")

    def test_a_stopped_executor_refuses_new_work(self) -> None:
        executor = OnboardingExecutor(workers=1, backlog=4)
        executor.stop()
        self.assertFalse(executor.submit(lambda: None))

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


class GatewayConfigDefaultsTests(unittest.TestCase):
    def test_defaults_keep_the_runner_optional(self) -> None:
        config = load_config(BASE_ENV)
        self.assertIsNone(config.mcp_token_encrypt_key)
        self.assertIsNone(config.query_mcp_endpoint)
        self.assertIsNone(config.user_env_root)
        self.assertGreater(config.onboarding_workers, 0)

    def test_the_master_key_is_not_rendered_by_repr(self) -> None:
        config = GatewayConfig(
            app_id="a",
            app_secret=_Secret("s"),
            postgres_dsn=_Secret("d"),
            mcp_token_encrypt_key=_Secret(MASTER_KEY),
        )
        self.assertNotIn(MASTER_KEY, repr(config))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
