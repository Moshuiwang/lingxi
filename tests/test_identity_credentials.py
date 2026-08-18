"""专用授权凭据的生命周期规则（纯逻辑，无数据库、无网络）。

认领断言：V-身份-03（凭据只以密文保存且不进入日志）的**进程内一半**——
密文落库那一半在 tests/test_identity_postgres_records.py；这里证明明文
不会经由 ``repr`` / ``str`` / 日志格式化泄漏。
V-身份-04（专用授权失效时不重放旧凭据、直接撤销）的判定规则。
V-身份-11 的**参数守卫**那一半（两种 CAS 不能同时传），见
``VaultSaveArgumentGuardTest``：被测的是一条纯内存 ``ValueError``，
真库那一半（``ON CONFLICT DO NOTHING`` 的反向 CAS）仍在
tests/test_identity_postgres_records.py。
"""

from __future__ import annotations

import importlib.util
import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lingxi.core.identity.credentials import (
    AuthorizationGrant,
    CredentialAction,
    CredentialState,
    RefreshOutcome,
    SecretToken,
    credential_state,
    decide_after_refresh,
    expiry_moment,
    rotation_deadline,
)


FAKE_TOKEN = "fake-refresh-token-for-tests-only"


class SecretTokenTest(unittest.TestCase):
    """V-身份-03：明文只在进程内被显式 reveal，不进入任何字符串化出口。"""

    def test_repr_and_str_never_contain_the_plaintext(self) -> None:
        secret = SecretToken(FAKE_TOKEN)

        self.assertNotIn(FAKE_TOKEN, repr(secret))
        self.assertNotIn(FAKE_TOKEN, str(secret))
        self.assertNotIn(FAKE_TOKEN, f"{secret}")
        self.assertNotIn(FAKE_TOKEN, "{}".format(secret))  # noqa: UP032 - 显式覆盖 format 出口
        self.assertEqual(secret.reveal(), FAKE_TOKEN)

    def test_grant_repr_and_log_formatting_never_contain_the_plaintext(self) -> None:
        grant = AuthorizationGrant(SecretToken(FAKE_TOKEN), 604800, "offline_access")
        logger = logging.getLogger("lingxi.test.credentials")

        with self.assertLogs(logger, level=logging.INFO) as captured:
            logger.info("凭据状态 grant=%s repr=%r", grant, grant)

        self.assertNotIn(FAKE_TOKEN, repr(grant))
        self.assertNotIn(FAKE_TOKEN, str(grant))
        for line in captured.output:
            self.assertNotIn(FAKE_TOKEN, line)

    def test_empty_secret_is_rejected_at_construction(self) -> None:
        for value in ("", "   "):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    SecretToken(value)

    def test_grant_requires_a_positive_lifetime_from_feishu(self) -> None:
        for seconds in (0, -1):
            with self.subTest(seconds=seconds):
                with self.assertRaises(ValueError):
                    AuthorizationGrant(SecretToken(FAKE_TOKEN), seconds, "")


class RotationDeadlineTest(unittest.TestCase):
    """轮换点跟着飞书返回的有效期走，不写死周期。"""

    def setUp(self) -> None:
        self.issued_at = datetime(2026, 8, 5, 3, 0, tzinfo=timezone.utc)

    def test_rotation_is_scheduled_at_eighty_percent_of_the_returned_lifetime(self) -> None:
        seven_days = 7 * 24 * 3600

        deadline = rotation_deadline(self.issued_at, seven_days)

        # 实测有效期 7 天时计划轮换点在发放后 5.6 天，与 80% 公式吻合。
        self.assertEqual(deadline, self.issued_at + timedelta(seconds=int(seven_days * 0.8)))
        self.assertEqual(deadline, self.issued_at + timedelta(days=5.6))

    def test_rotation_follows_a_different_lifetime_instead_of_a_fixed_period(self) -> None:
        short = rotation_deadline(self.issued_at, 3600)
        long = rotation_deadline(self.issued_at, 30 * 24 * 3600)

        self.assertEqual(short, self.issued_at + timedelta(seconds=2880))
        self.assertEqual(long, self.issued_at + timedelta(seconds=int(30 * 24 * 3600 * 0.8)))
        self.assertNotEqual(short - self.issued_at, long - self.issued_at)

    def test_rotation_point_is_always_strictly_before_expiry(self) -> None:
        for seconds in (1, 2, 5, 60, 3600, 604800):
            with self.subTest(seconds=seconds):
                deadline = rotation_deadline(self.issued_at, seconds)
                expiry = expiry_moment(self.issued_at, seconds)
                self.assertLess(deadline, expiry)

    def test_naive_datetimes_are_rejected_because_storage_is_utc_only(self) -> None:
        with self.assertRaises(ValueError):
            rotation_deadline(datetime(2026, 8, 5, 3, 0), 3600)


class CredentialStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 5, 3, 0, tzinfo=timezone.utc)

    def test_states_are_derived_from_the_two_stored_moments(self) -> None:
        cases = (
            (self.now + timedelta(hours=1), self.now + timedelta(days=7), CredentialState.ACTIVE),
            (self.now - timedelta(seconds=1), self.now + timedelta(days=7), CredentialState.DUE),
            (self.now - timedelta(days=8), self.now - timedelta(seconds=1), CredentialState.EXPIRED),
        )
        for refresh_at, expires_at, expected in cases:
            with self.subTest(expected=expected):
                self.assertIs(credential_state(refresh_at=refresh_at, expires_at=expires_at, now=self.now), expected)

    def test_expired_wins_over_due_so_a_dead_credential_is_never_replayed(self) -> None:
        state = credential_state(
            refresh_at=self.now - timedelta(days=9),
            expires_at=self.now - timedelta(days=1),
            now=self.now,
        )

        self.assertIs(state, CredentialState.EXPIRED)


class RefreshOutcomeTest(unittest.TestCase):
    """V-身份-04：只有明确成功才轮换；失败与结果不明确一律撤销。"""

    def test_only_a_confirmed_rotation_keeps_the_credential(self) -> None:
        self.assertIs(decide_after_refresh(RefreshOutcome.ROTATED), CredentialAction.ROTATE)

    def test_failure_and_indeterminate_results_both_revoke(self) -> None:
        for outcome in (RefreshOutcome.FAILED, RefreshOutcome.INDETERMINATE):
            with self.subTest(outcome=outcome):
                self.assertIs(decide_after_refresh(outcome), CredentialAction.REVOKE)

    def test_every_outcome_is_covered_so_a_new_outcome_cannot_default_to_keeping(self) -> None:
        # refresh_token 一次性有效：新增一个未处理的结果分支绝不能默认成「保留」。
        for outcome in RefreshOutcome:
            with self.subTest(outcome=outcome):
                self.assertIn(decide_after_refresh(outcome), (CredentialAction.ROTATE, CredentialAction.REVOKE))


@unittest.skipUnless(
    importlib.util.find_spec("cryptography"),
    "跳过：构造凭据保管对象需要 cryptography（绝不降级为明文保存）",
)
class VaultSaveArgumentGuardTest(unittest.TestCase):
    """V-身份-11 的参数守卫：两种 CAS 不能同时传（`delegated_credentials.save` 开头）。

    **这条不需要数据库**，因此不挂在 ``LINGXI_POSTGRES_DSN`` 门控下。被测的守卫是
    ``save()`` 第一行的纯内存 ``ValueError``：它在文件锁与连库之前，一次保存只能有
    一个判定条件，否则"到底以哪个为准"会变成调用方的隐式约定。放在真库门控里的后果
    是：没有数据库的机器上它**从来没跑过**，而它恰恰是那台机器上唯一跑得动的一条。

    真库那一半（登记表为空才写入的反向 CAS）留在
    ``tests/test_identity_postgres_records.py::SubjectBootstrapCasTest``。
    """

    # 故意不可达：一旦守卫被挪到连库之后，这里会因为连不上而**响亮失败**，
    # 不会变成一条"看起来通过了"的用例。`.invalid` 是保留后缀，不会解析到任何主机。
    UNREACHABLE_DSN = "postgresql://lingxi-guard-test.invalid:1/never-reached"

    def test_the_two_cas_conditions_cannot_be_combined(self) -> None:
        from cryptography.fernet import Fernet

        from lingxi.adapters.delegated_credentials import HostFileDelegatedCredentialVault

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "delegated-credential.enc"
        vault = HostFileDelegatedCredentialVault(
            self.UNREACHABLE_DSN, Fernet.generate_key().decode(), str(path)
        )

        # grant 在断言块**之外**构造：放进 assertRaises 里的话，将来
        # AuthorizationGrant 自己抛 ValueError 也会让这条用例通过，而被测的守卫
        # 根本没被执行过。
        grant = AuthorizationGrant(SecretToken(FAKE_TOKEN), 3600, "")

        with self.assertRaises(ValueError) as raised:
            vault.save(
                subject_open_id="ou_delegated_authorization_subject",
                grant=grant,
                require_absent_registration=True,
                expected_registered_subject_open_id="ou_delegated_authorization_subject",
            )

        # 断言具体消息：只断言"抛了 ValueError"分不清是守卫拒绝，还是参数校验、
        # 密钥解析之类的别的 ValueError 顺手把用例染绿了。
        self.assertIn("首次建立与既有主体 CAS 不能同时使用", str(raised.exception))

        # 守卫在文件锁之前：连锁文件都不该出现，密文更不该落盘。
        self.assertFalse(path.exists(), "被拒的保存不得写入密文")
        self.assertFalse(path.with_name(path.name + ".lock").exists(), "守卫应在取文件锁之前就拒绝")

    def test_a_naive_consumption_moment_is_refused_before_any_io(self) -> None:
        """频率上界的判据必须带时区（Issue #215）。

        它按 **UTC 日**与日报的日界对齐；一个不带时区的时刻会被部署机器的本地时区
        悄悄挪一天，而"挪一天"在这里的含义是多消费一次一次性凭据。守卫同样在文件锁与
        连库之前，因此没有数据库的机器上也跑得动。
        """

        from cryptography.fernet import Fernet

        from lingxi.adapters.delegated_credentials import HostFileDelegatedCredentialVault

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "delegated-credential.enc"
        vault = HostFileDelegatedCredentialVault(
            self.UNREACHABLE_DSN, Fernet.generate_key().decode(), str(path)
        )
        grant = AuthorizationGrant(SecretToken(FAKE_TOKEN), 3600, "")

        with self.assertRaises(ValueError) as raised:
            vault.save(
                subject_open_id="ou_delegated_authorization_subject",
                grant=grant,
                refresh_consumed_at=datetime(2026, 8, 18, 9, 0),
            )

        self.assertIn("续期消费时刻必须是带时区的时间", str(raised.exception))
        self.assertFalse(path.exists(), "被拒的保存不得写入密文")
        self.assertFalse(path.with_name(path.name + ".lock").exists(), "守卫应在取文件锁之前就拒绝")


class DerivedAccessTokenTest(unittest.TestCase):
    """派生短期令牌的载体（Issue #215）：明文包在 SecretToken 里，寿命可以未知。"""

    def test_the_plaintext_never_leaks_through_any_stringification(self) -> None:
        from lingxi.core.identity.credentials import DerivedAccessToken

        token = DerivedAccessToken(SecretToken("fake-access-token-for-tests-only"), 7200)

        for rendered in (repr(token), str(token), f"{token}", f"{token.token}"):
            self.assertNotIn("fake-access-token-for-tests-only", rendered)

    def test_a_bare_string_token_is_refused(self) -> None:
        from lingxi.core.identity.credentials import DerivedAccessToken

        with self.assertRaises(TypeError):
            DerivedAccessToken("fake-access-token-for-tests-only", 7200)  # type: ignore[arg-type]

    def test_an_unknown_lifetime_is_allowed_but_a_broken_one_is_not(self) -> None:
        """未知寿命是一种合法状态（飞书没给），但"给了一个说不通的值"不是。"""

        from lingxi.core.identity.credentials import DerivedAccessToken

        self.assertFalse(DerivedAccessToken(SecretToken(FAKE_TOKEN)).lifetime_known)
        self.assertTrue(DerivedAccessToken(SecretToken(FAKE_TOKEN), 1).lifetime_known)
        for broken in (0, -1, True, 1.5, "7200"):
            with self.subTest(lifetime=broken):
                with self.assertRaises(ValueError):
                    DerivedAccessToken(SecretToken(FAKE_TOKEN), broken)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
