"""``lingxi-scheduler`` 进程入口：配置装配、续期扫描循环与 SIGTERM 退出语义。

认领断言：
- V-部署-03（进程收到 ``SIGTERM`` 后停止领取新任务、完成在途任务、超时内退出）——
  用**真实子进程 + 真实信号**验证，不用 mock 信号；
- V-身份-04（续期失败或结果不明确时撤销，不重放旧凭据）在循环层的落地；
- V-部署-01（不硬编码主机、端口、密钥；全部来自环境变量）。

续期扫描属于定时职责，按架构设计归 ``lingxi-scheduler``，不再挂在长连接进程里
（[Issue #16 复验记录](https://github.com/Moshuiwang/lingxi/issues/16#issuecomment-5188063325)）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import textwrap
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lingxi.adapters.feishu_directory import FeishuDirectoryError
from lingxi.adapters.delegated_credentials import StoredCredential
from lingxi.apps.scheduler import (
    IDLE_CONVERSATION_SWEEP_AFTER,
    CredentialRotationLoop,
    IdleConversationSweepDuty,
    RetentionCleanupDuty,
    RotationReport,
    SchedulerConfig,
    SchedulerLoop,
    build_loop,
)
from lingxi.core.identity.credentials import (
    AuthorizationGrant,
    DerivedAccessToken,
    RefreshDailyLimitReached,
    SecretToken,
)


REPOSITORY_ROOT = Path(__file__).parents[1]
FAKE_TOKEN = "fake-refresh-token-for-tests-only"
FAKE_ACCESS_TOKEN = "fake-access-token-for-tests-only"
COMPLETE_ENV = {
    "LINGXI_POSTGRES_DSN": "postgresql://user@localhost:5432/lingxi",
    "LINGXI_DELEGATED_CREDENTIAL_KEY": "ZmFrZS1mZXJuZXQta2V5LWZvci11bml0LXRlc3RzLTA9",
    "LINGXI_DELEGATED_CREDENTIAL_PATH": "/var/lib/lingxi/credentials/delegated.enc",
    "LINGXI_FEISHU_APP_ID": "cli_fake",
    "LINGXI_FEISHU_APP_SECRET": "secret_fake",
}


def replacement_grant(*, seconds: int = 604800) -> AuthorizationGrant:
    return AuthorizationGrant(SecretToken("fake-next-token"), seconds, "offline_access")


def credential(*, subject: str = "ou_delegated", seconds: int = 604800) -> StoredCredential:
    now = datetime.now(timezone.utc)
    return StoredCredential(
        subject_open_id=subject,
        grant=AuthorizationGrant(SecretToken(FAKE_TOKEN), seconds, "offline_access"),
        refresh_at=now,
        expires_at=now + timedelta(seconds=seconds),
    )


class FakeVault:
    """凭据库替身。

    ``claim_due`` / ``save`` 的关键字与真实凭据库保持一致（含 Issue #215 新增的
    ``require_due`` / ``refuse_if_consumed_on`` / ``refresh_consumed_at``）：签名对不上时
    这里会直接 ``TypeError``，这正是"装配把参数传错了"应该有的响亮失败。
    """

    def __init__(self, claims: list[StoredCredential | None], *, save_failures: int = 0) -> None:
        self._claims = list(claims)
        self._save_failures = save_failures
        self.saved: list[tuple[str, AuthorizationGrant]] = []
        self.revoked: list[str] = []
        # 落盘记下来的"今天已经消费过一次续期"判据，供频率上界断言。
        self.consumed_days: list[object] = []
        # 与派生令牌持有者共享的事件序列，用于断言"先落盘、后交出"。
        self.events: list[str] = []

    def claim_due(self, *, require_due: bool = True, refuse_if_consumed_on=None):
        self.last_require_due = require_due
        self.last_refuse_if_consumed_on = refuse_if_consumed_on
        if refuse_if_consumed_on is not None and refuse_if_consumed_on in self.consumed_days:
            raise RefreshDailyLimitReached(consumed_at=datetime.now(timezone.utc))
        return self._claims.pop(0) if self._claims else None

    def save(
        self,
        *,
        subject_open_id: str,
        grant: AuthorizationGrant,
        issued_at=None,
        replacing_generation=None,
        expected_registered_subject_open_id=None,
        refresh_consumed_at=None,
        refresh_consumed_count=None,
    ) -> bool:
        if self._save_failures > 0:
            self._save_failures -= 1
            raise RuntimeError("模拟写库失败")
        self.events.append("save")
        self.saved.append((subject_open_id, grant))
        if refresh_consumed_at is not None:
            self.consumed_days.append(refresh_consumed_at.date())
        return True

    def revoke(self, *, reason: str, generation=None) -> bool:
        self.revoked.append(reason)
        return True

    def revoke_stale_consumed(self, **_kwargs) -> bool:
        self.stale_sweeps = getattr(self, "stale_sweeps", 0) + 1
        return False


class FakeAuthorization:
    def __init__(self, result, *, access_token_lifetime: int | None = 7200) -> None:
        self._result = result
        self._access_token_lifetime = access_token_lifetime
        self.calls = 0

    def refresh(self, current: AuthorizationGrant):
        self.calls += 1
        if isinstance(self._result, Exception):
            raise self._result
        return self._result, DerivedAccessToken(
            SecretToken(FAKE_ACCESS_TOKEN), self._access_token_lifetime
        )


class SchedulerConfigTest(unittest.TestCase):
    def test_every_setting_comes_from_an_environment_variable(self) -> None:
        config = SchedulerConfig.from_env({**COMPLETE_ENV, "LINGXI_SCHEDULER_INTERVAL_SECONDS": "30"})

        self.assertEqual(config.postgres_dsn, COMPLETE_ENV["LINGXI_POSTGRES_DSN"])
        self.assertEqual(config.credential_path, COMPLETE_ENV["LINGXI_DELEGATED_CREDENTIAL_PATH"])
        self.assertEqual(config.feishu_app_id, "cli_fake")
        self.assertEqual(config.interval_seconds, 30)
        self.assertTrue(config.feishu_base_url.startswith("https://"))

    def test_a_missing_setting_names_the_variable_without_echoing_any_value(self) -> None:
        for missing in COMPLETE_ENV:
            with self.subTest(missing=missing):
                environment = {key: value for key, value in COMPLETE_ENV.items() if key != missing}
                with self.assertRaises(ValueError) as raised:
                    SchedulerConfig.from_env(environment)

                message = str(raised.exception)
                self.assertIn(missing, message)
                self.assertNotIn("secret_fake", message)
                self.assertNotIn(COMPLETE_ENV["LINGXI_DELEGATED_CREDENTIAL_KEY"], message)

    def test_the_config_repr_never_echoes_the_secret_values(self) -> None:
        config = SchedulerConfig.from_env(COMPLETE_ENV)

        for secret in ("secret_fake", COMPLETE_ENV["LINGXI_DELEGATED_CREDENTIAL_KEY"], COMPLETE_ENV["LINGXI_POSTGRES_DSN"]):
            with self.subTest(secret=secret[:8]):
                self.assertNotIn(secret, repr(config))

    def test_a_legal_roster_does_not_appear_in_the_config_repr(self) -> None:
        """opus 批量审查 P2 修复：`innertest_roster_open_ids` 是一批飞书用户
        open_id，与本文件其余凭据字段同一条纪律——不进 `repr(config)`，即使
        名单本身合法、值不敏感到需要单独脱敏类包装，也不该随手一个
        `logger.info("配置 %s", config)` 就把整份名单写进日志。"""

        legal_member = "ou_rostermembera00000000000"
        config = SchedulerConfig.from_env(
            {**COMPLETE_ENV, "LINGXI_INNERTEST_ROSTER_OPEN_IDS": legal_member}
        )

        self.assertEqual(config.innertest_roster_open_ids, frozenset({legal_member}))
        self.assertNotIn(legal_member, repr(config))

    def test_an_unusable_interval_is_refused_instead_of_silently_defaulted(self) -> None:
        for value in ("0", "-5", "abc"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    SchedulerConfig.from_env({**COMPLETE_ENV, "LINGXI_SCHEDULER_INTERVAL_SECONDS": value})

    def test_there_is_no_target_tenant_setting(self) -> None:
        """硬约束 1：租户是查出来的结果，不是配置项。"""
        self.assertNotIn("tenant", " ".join(SchedulerConfig.ENVIRONMENT_KEYS).lower())


class RotationLoopTest(unittest.TestCase):
    def test_a_successful_refresh_rotates_the_stored_credential(self) -> None:
        vault = FakeVault([credential()])
        replacement = AuthorizationGrant(SecretToken("fake-next-token"), 604800, "offline_access")
        loop = CredentialRotationLoop(vault=vault, authorization=FakeAuthorization(replacement), interval_seconds=1)

        report = loop.run_once()

        self.assertEqual(report.claimed, 1)
        self.assertEqual(report.rotated, 1)
        self.assertEqual(report.revoked, 0)
        self.assertEqual(vault.saved[0][0], "ou_delegated")
        self.assertEqual(vault.saved[0][1].refresh_token.reveal(), "fake-next-token")
        self.assertEqual(vault.revoked, [])

    def test_a_stop_during_the_sweep_prevents_a_new_claim(self) -> None:
        """终轮 Codex：SIGTERM 落在收殓等待文件锁期间时，不得再领取并启动
        新的续期请求。"""
        loop_holder: list[CredentialRotationLoop] = []

        class StoppingVault(FakeVault):
            def __init__(self) -> None:
                super().__init__([credential()])
                self.claims = 0

            def revoke_stale_consumed(self, **_kwargs) -> bool:
                loop_holder[0].request_stop()
                return False

            def claim_due(self):
                self.claims += 1
                return super().claim_due()

        vault = StoppingVault()
        loop = CredentialRotationLoop(vault=vault, authorization=FakeAuthorization(replacement_grant()), interval_seconds=0.01)
        loop_holder.append(loop)

        report = loop.run_once()

        self.assertEqual(vault.claims, 0)
        self.assertEqual((report.claimed, report.rotated, report.revoked), (0, 0, 0))

    def test_every_round_sweeps_stale_consumed_credentials_first(self) -> None:
        """崩溃窗口收殓：每轮先清「已消费未落库」的行，防止旧令牌在租期后被重放
        （Codex 复查发现）。"""
        vault = FakeVault([None])
        loop = CredentialRotationLoop(vault=vault, authorization=FakeAuthorization(replacement_grant()), interval_seconds=0.01)

        loop.run_once()

        self.assertEqual(getattr(vault, "stale_sweeps", 0), 1)

    def test_a_transient_save_failure_is_retried_and_the_credential_survives(self) -> None:
        """写库瞬时失败要重试：一次数据库抖动不该报废一条一次性凭据（独立复查发现）。"""
        vault = FakeVault([credential()], save_failures=1)
        loop = CredentialRotationLoop(vault=vault, authorization=FakeAuthorization(replacement_grant()), interval_seconds=0.01)

        report = loop.run_once()

        self.assertEqual((report.claimed, report.rotated, report.revoked), (1, 1, 0))
        self.assertEqual(len(vault.saved), 1)
        self.assertEqual(vault.revoked, [])

    def test_a_persistent_save_failure_revokes_with_a_distinct_reason(self) -> None:
        """续期成功但新凭据始终写不进库：旧的已被飞书作废，必须撤销并以
        可区分的日志请求人工重新授权，不得抛异常带着新凭据一起消失。"""
        vault = FakeVault([credential()], save_failures=99)
        loop = CredentialRotationLoop(vault=vault, authorization=FakeAuthorization(replacement_grant()), interval_seconds=0.01)

        with self.assertLogs("lingxi.apps.scheduler", level="ERROR") as captured:
            report = loop.run_once()

        self.assertEqual((report.claimed, report.rotated, report.revoked), (1, 0, 1))
        self.assertEqual(vault.revoked, ["rotation_persist_failed"])
        self.assertTrue(any("不可恢复" in line for line in captured.output))
        self.assertTrue(all(FAKE_TOKEN not in line for line in captured.output))

    def test_run_forever_survives_an_exception_in_one_round(self) -> None:
        """定时职责不因一轮异常而终止（独立复查发现：claim_due 抛错会带崩进程）。"""

        class ExplodingVault(FakeVault):
            def __init__(self) -> None:
                super().__init__([])
                self.calls = 0

            def claim_due(self):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("模拟一次领取异常")
                return None

        vault = ExplodingVault()
        loop = CredentialRotationLoop(vault=vault, authorization=FakeAuthorization(replacement_grant()), interval_seconds=0.01)

        def stop_after_second_round() -> None:
            deadline = time.monotonic() + 5
            while vault.calls < 2 and time.monotonic() < deadline:
                time.sleep(0.005)
            loop.request_stop()

        stopper = threading.Thread(target=stop_after_second_round)
        stopper.start()
        with self.assertLogs("lingxi.apps.scheduler", level="ERROR"):
            loop.run_forever()
        stopper.join()

        self.assertGreaterEqual(vault.calls, 2)

    def test_a_failed_refresh_revokes_and_never_replays_the_old_credential(self) -> None:
        """V-身份-04：``refresh_token`` 一次性有效，重放等于用一个已作废的凭据。"""
        vault = FakeVault([credential()])
        loop = CredentialRotationLoop(
            vault=vault,
            authorization=FakeAuthorization(FeishuDirectoryError("feishu_code_20037")),
            interval_seconds=1,
        )

        report = loop.run_once()

        self.assertEqual(report.rotated, 0)
        self.assertEqual(report.revoked, 1)
        self.assertEqual(vault.saved, [])
        self.assertEqual(len(vault.revoked), 1)

    def test_an_indeterminate_transport_result_also_revokes(self) -> None:
        for error in (FeishuDirectoryError("transport_error"), TimeoutError("boom"), RuntimeError("boom")):
            with self.subTest(error=type(error).__name__):
                vault = FakeVault([credential()])
                loop = CredentialRotationLoop(vault=vault, authorization=FakeAuthorization(error), interval_seconds=1)

                loop.run_once()

                self.assertEqual(vault.saved, [])
                self.assertEqual(len(vault.revoked), 1)

    def test_nothing_due_is_a_no_op(self) -> None:
        vault = FakeVault([None])
        loop = CredentialRotationLoop(vault=vault, authorization=FakeAuthorization(None), interval_seconds=1)

        report = loop.run_once()

        self.assertEqual((report.claimed, report.rotated, report.revoked), (0, 0, 0))

    def test_a_stopping_loop_claims_nothing_further(self) -> None:
        vault = FakeVault([credential(), credential()])
        loop = CredentialRotationLoop(vault=vault, authorization=FakeAuthorization(None), interval_seconds=1)

        loop.request_stop()
        report = loop.run_once()

        self.assertTrue(loop.stopping)
        self.assertEqual(report.claimed, 0)


class SchedulerLoopTest(unittest.TestCase):
    """多职责编排：失败隔离与停止语义（认领 V-保留-15、V-保留-17 的可注入部分）。

    这些用例不碰数据库——它们要证明的是**结构**：一个职责抛异常时另一个职责这一轮
    照样跑。用真库跑反而更难构造"清理必然失败"的场景。真库侧的观察点在
    `tests/test_retention_postgres.py`。
    """

    class RecordingDuty:
        def __init__(self, name: str, *, explode: bool = False) -> None:
            self.name = name
            self.calls = 0
            self._explode = explode

        def run_once(self) -> str:
            self.calls += 1
            if self._explode:
                raise RuntimeError(f"{self.name} 模拟失败")
            return f"{self.name}-ok"

    def test_a_failing_duty_does_not_skip_the_other_one_in_the_same_round(self) -> None:
        """V-保留-15（验收 F-02）：隔离由 `SchedulerLoop.run_once` 的逐职责 try 保证。

        两个方向都验：清理炸不影响轮换、轮换炸不影响清理。只验一个方向的话，
        把 try 写在循环外面（第一个职责失败就跳过其余）仍然能通过其中一半。
        """
        for exploding_index in (0, 1):
            with self.subTest(exploding=exploding_index):
                duties = [
                    self.RecordingDuty("凭据轮换", explode=exploding_index == 0),
                    self.RecordingDuty("保留清理", explode=exploding_index == 1),
                ]
                loop = SchedulerLoop(duties=duties, interval_seconds=0.01)

                with self.assertLogs("lingxi.apps.scheduler", level="ERROR") as captured:
                    reports = loop.run_once()

                self.assertEqual([duty.calls for duty in duties], [1, 1], "两个职责本轮都必须被调用")
                self.assertEqual(reports[exploding_index], None)
                self.assertEqual(reports[1 - exploding_index], f"{duties[1 - exploding_index].name}-ok")
                self.assertTrue(any(duties[exploding_index].name in line for line in captured.output))

    def test_repeated_failures_never_stop_the_process(self) -> None:
        """V-保留-15（验收 F-03）：连续失败不退出；日志只记异常类型，不记正文。"""
        exploding = self.RecordingDuty("保留清理", explode=True)
        loop = SchedulerLoop(duties=(exploding,), interval_seconds=0.01)

        with self.assertLogs("lingxi.apps.scheduler", level="ERROR") as captured:
            for _round in range(5):
                loop.run_once()

        self.assertEqual(exploding.calls, 5)
        self.assertTrue(all("模拟失败" not in line for line in captured.output), "异常正文不得进日志")
        self.assertTrue(any("RuntimeError" in line for line in captured.output))

    def test_a_stopping_loop_lets_no_duty_claim_new_work(self) -> None:
        """V-保留-17（验收 F-06）：停止之后一个职责都不再领取。"""
        duties = [self.RecordingDuty("凭据轮换"), self.RecordingDuty("保留清理")]
        loop = SchedulerLoop(duties=duties, interval_seconds=0.01)

        loop.request_stop()
        reports = loop.run_once()

        self.assertTrue(loop.stopping)
        self.assertEqual([duty.calls for duty in duties], [0, 0])
        self.assertEqual(reports, (None, None))

    def test_one_stop_signal_reaches_every_duty(self) -> None:
        """SIGTERM 只设一个标志，全部职责必须同时停止领取。"""
        stop = threading.Event()
        rotation = CredentialRotationLoop(
            vault=FakeVault([credential()]),
            authorization=FakeAuthorization(replacement_grant()),
            interval_seconds=0.01,
            stop=stop,
        )
        cleanup = RetentionCleanupDuty(cleaner=self.RecordingDuty("保留清理"), stop=stop)
        loop = SchedulerLoop(duties=(rotation, cleanup), interval_seconds=0.01, stop=stop)

        loop.request_stop()

        self.assertTrue(rotation.stopping)
        self.assertTrue(cleanup.stopping)
        self.assertEqual(rotation.run_once(), RotationReport())
        self.assertIsNone(cleanup.run_once())

    def test_the_retention_duty_calls_the_cleaner_exactly_once_per_round(self) -> None:
        """每轮只调一次：一次调用就是一个事务，"删空为止"会让退出时间失去上界。"""
        cleaner = self.RecordingDuty("清理器")
        duty = RetentionCleanupDuty(cleaner=cleaner)

        with self.assertLogs("lingxi.apps.scheduler", level="INFO"):
            for _round in range(3):
                duty.run_once()

        self.assertEqual(cleaner.calls, 3)

    class RecordingQueue:
        """记录 ``sweep_idle_conversations`` 调用参数的假 queue，不碰真库。"""

        def __init__(self, *, cleared: int = 0) -> None:
            self.calls: list[timedelta] = []
            self._cleared = cleared

        def sweep_idle_conversations(self, *, idle_after: timedelta) -> int:
            self.calls.append(idle_after)
            return self._cleared

    def test_the_idle_sweep_duty_calls_the_queue_with_the_two_hour_window_every_round(self) -> None:
        """内审 P2-2：空闲会话清理必须真的接到 scheduler 的周期驱动上，且窗口
        固定两小时（合同值，不接受配置漂移，见 P2-1 同一取舍）。"""

        queue = self.RecordingQueue(cleared=2)
        duty = IdleConversationSweepDuty(queue=queue, idle_after=IDLE_CONVERSATION_SWEEP_AFTER)

        for _round in range(3):
            report = duty.run_once()
            self.assertEqual(report, 2)

        self.assertEqual(queue.calls, [timedelta(hours=2)] * 3)

    def test_a_stopping_idle_sweep_duty_does_not_call_the_queue(self) -> None:
        stop = threading.Event()
        queue = self.RecordingQueue(cleared=5)
        duty = IdleConversationSweepDuty(queue=queue, idle_after=IDLE_CONVERSATION_SWEEP_AFTER, stop=stop)

        stop.set()
        report = duty.run_once()

        self.assertIsNone(report)
        self.assertEqual(queue.calls, [])

    def test_a_process_without_any_duty_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            SchedulerLoop(duties=(), interval_seconds=1)


@unittest.skipUnless(
    importlib.util.find_spec("psycopg") and importlib.util.find_spec("cryptography"),
    "跳过：build_loop 会真的构造凭据保管与清理适配器，需要 psycopg 与 cryptography",
)
class BuildLoopTest(unittest.TestCase):
    def test_the_assembled_process_carries_all_scheduled_duties(self) -> None:
        """"谁会调用它"的落点：清理与空闲会话扫描职责由**已存在**的
        `lingxi-scheduler` 进程装配。

        `build_loop` 是 `main()` 唯一的装配入口（本模块 `main` 内），因此这条断言
        就是"新增的清理代码真的有调用方"的证据（空闲会话清理一节认领内审 P2-2；
        权限链到期清理一节认领 Epic C 冻结缺陷 F1 —— 同一个形状的缺陷第二次发生：
        `redact_expired_payloads` / `purge_expired_checks` 交付时同样零调用方，
        逐条断言在 `tests/test_permission_retention_duty.py`）。
        """
        import tempfile

        from cryptography.fernet import Fernet

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = SchedulerConfig.from_env(
            {
                **COMPLETE_ENV,
                # 装配会真的构造凭据保管对象，需要一个有效的 Fernet 密钥与可写路径；
                # 两者都是本地临时物，不涉及任何真实凭据。
                "LINGXI_DELEGATED_CREDENTIAL_KEY": Fernet.generate_key().decode(),
                "LINGXI_DELEGATED_CREDENTIAL_PATH": str(Path(directory.name) / "delegated.enc"),
            }
        )

        loop = build_loop(config)

        self.assertEqual(
            [duty.name for duty in loop.duties],
            # 组织快照同步（Issue #250）的两个令牌供给 `build_loop` 总能建出默认值，
            # 因此总会真实注册，排在这个夹具唯二会注册的职责之后；迟到就绪恢复
            # （V-开通-18）与开通中途停摆收口（Issue #282，V-开通-19）都没有可选
            # 前置会让它们整体不装配，因此也总会注册，排在最后（后者紧跟前者）。
            [
                "凭据轮换",
                "保留清理",
                "空闲会话清理",
                "权限链到期清理",
                "组织快照同步",
                "迟到就绪恢复",
                "开通中途停摆收口",
            ],
        )
        self.assertIsInstance(loop, SchedulerLoop)
        from lingxi.adapters.retention import RETENTION_CLEANUP_TIMEOUTS

        cleanup_duty = loop.duties[1]
        self.assertEqual(cleanup_duty._cleaner._timeouts, RETENTION_CLEANUP_TIMEOUTS)
        idle_sweep_duty = loop.duties[2]
        self.assertEqual(idle_sweep_duty._idle_after, timedelta(hours=2))
        # 一个停止标志贯穿全部职责。
        loop.request_stop()
        self.assertTrue(all(duty.stopping for duty in loop.duties))


class SigtermTest(unittest.TestCase):
    """真实子进程 + 真实 SIGTERM。mock 出来的信号证明不了退出语义。"""

    SCRIPT = textwrap.dedent(
        """
        import json, sys, time
        from lingxi.apps.scheduler import CredentialRotationLoop, install_signal_handlers
        from lingxi.core.identity.credentials import AuthorizationGrant, SecretToken

        state = {"claims": 0, "claims_after_stop": 0, "started": 0, "completed": 0}

        class Vault:
            def claim_due(self):
                state["claims"] += 1
                if loop.stopping:
                    state["claims_after_stop"] += 1
                return type("C", (), {
                    "subject_open_id": "ou_delegated",
                    "grant": AuthorizationGrant(SecretToken("fake"), 604800, ""),
                })()
            def save(
                self,
                *,
                subject_open_id,
                grant,
                issued_at=None,
                replacing_generation=None,
                expected_registered_subject_open_id=None,
            ):
                pass
            def revoke(self, *, reason):
                return True

        class Authorization:
            def refresh(self, current):
                state["started"] += 1
                time.sleep(0.4)          # 在途任务：SIGTERM 到达时正在做
                state["completed"] += 1
                return AuthorizationGrant(SecretToken("fake-next"), 604800, ""), SecretToken("fake-access")

        loop = CredentialRotationLoop(vault=Vault(), authorization=Authorization(), interval_seconds=0.05)
        install_signal_handlers(loop)
        print("ready", flush=True)
        loop.run_forever()
        print(json.dumps(state), flush=True)
        """
    )

    def test_sigterm_stops_claiming_finishes_the_in_flight_rotation_and_exits_cleanly(self) -> None:
        environment = {**os.environ, "PYTHONPATH": str(REPOSITORY_ROOT / "src"), "PYTHONUNBUFFERED": "1"}
        process = subprocess.Popen(
            [sys.executable, "-c", self.SCRIPT],
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stdout is not None
            self.assertEqual(process.stdout.readline().strip(), "ready")
            time.sleep(0.6)
            sent_at = time.monotonic()
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=10)
        finally:
            if process.poll() is None:  # pragma: no cover - 只在断言失败路径上发生
                process.kill()
                process.communicate()

        self.assertEqual(process.returncode, 0, msg=stderr)
        self.assertLess(time.monotonic() - sent_at, 5, "SIGTERM 后必须在超时内退出")
        state = json.loads(stdout.strip().splitlines()[-1])
        self.assertEqual(state["claims_after_stop"], 0, "收到 SIGTERM 后不得再领取新任务")
        self.assertEqual(state["started"], state["completed"], "在途轮换必须跑完，不能被截断")
        self.assertGreaterEqual(state["completed"], 1)


class ModuleEntryPointTest(unittest.TestCase):
    def test_the_process_starts_with_python_m_and_refuses_an_empty_configuration(self) -> None:
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(REPOSITORY_ROOT / "src"),
            "PYTHONUNBUFFERED": "1",
        }
        result = subprocess.run(
            [sys.executable, "-m", "lingxi.apps.scheduler"],
            cwd=REPOSITORY_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LINGXI_POSTGRES_DSN", result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
