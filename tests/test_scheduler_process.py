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
from lingxi.adapters.postgres_credentials import StoredCredential
from lingxi.apps.scheduler import CredentialRotationLoop, SchedulerConfig
from lingxi.core.identity.credentials import AuthorizationGrant, SecretToken


REPOSITORY_ROOT = Path(__file__).parents[1]
FAKE_TOKEN = "fake-refresh-token-for-tests-only"
COMPLETE_ENV = {
    "LINGXI_POSTGRES_DSN": "postgresql://user@localhost:5432/lingxi",
    "LINGXI_DELEGATED_CREDENTIAL_KEY": "ZmFrZS1mZXJuZXQta2V5LWZvci11bml0LXRlc3RzLTA9",
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
    def __init__(self, claims: list[StoredCredential | None], *, save_failures: int = 0) -> None:
        self._claims = list(claims)
        self._save_failures = save_failures
        self.saved: list[tuple[str, AuthorizationGrant]] = []
        self.revoked: list[str] = []

    def claim_due(self):
        return self._claims.pop(0) if self._claims else None

    def save(self, *, subject_open_id: str, grant: AuthorizationGrant, issued_at=None) -> None:
        if self._save_failures > 0:
            self._save_failures -= 1
            raise RuntimeError("模拟写库失败")
        self.saved.append((subject_open_id, grant))

    def revoke(self, *, reason: str) -> bool:
        self.revoked.append(reason)
        return True

    def revoke_stale_consumed(self, **_kwargs) -> bool:
        self.stale_sweeps = getattr(self, "stale_sweeps", 0) + 1
        return False


class FakeAuthorization:
    def __init__(self, result) -> None:
        self._result = result

    def refresh(self, current: AuthorizationGrant):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result, SecretToken("fake-access")


class SchedulerConfigTest(unittest.TestCase):
    def test_every_setting_comes_from_an_environment_variable(self) -> None:
        config = SchedulerConfig.from_env({**COMPLETE_ENV, "LINGXI_SCHEDULER_INTERVAL_SECONDS": "30"})

        self.assertEqual(config.postgres_dsn, COMPLETE_ENV["LINGXI_POSTGRES_DSN"])
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
            def save(self, *, subject_open_id, grant, issued_at=None):
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
