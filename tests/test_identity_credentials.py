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
from datetime import UTC, date, datetime, timedelta
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
        self.issued_at = datetime(2026, 8, 5, 3, 0, tzinfo=UTC)

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
        self.now = datetime(2026, 8, 5, 3, 0, tzinfo=UTC)

    def test_states_are_derived_from_the_two_stored_moments(self) -> None:
        cases = (
            (self.now + timedelta(hours=1), self.now + timedelta(days=7), CredentialState.ACTIVE),
            (self.now - timedelta(seconds=1), self.now + timedelta(days=7), CredentialState.DUE),
            (
                self.now - timedelta(days=8),
                self.now - timedelta(seconds=1),
                CredentialState.EXPIRED,
            ),
        )
        for refresh_at, expires_at, expected in cases:
            with self.subTest(expected=expected):
                self.assertIs(
                    credential_state(refresh_at=refresh_at, expires_at=expires_at, now=self.now),
                    expected,
                )

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
                self.assertIn(
                    decide_after_refresh(outcome),
                    (CredentialAction.ROTATE, CredentialAction.REVOKE),
                )


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

    def test_no_parameter_can_relax_the_due_check_without_the_rate_ceilings(self) -> None:
        """收口轮 P2-a、Issue #276 延伸：``for_supply`` 把三件事**捆死**，没有任何参数
        能把它们拆开。

        放开到期判定而不带频率上界，等于把一条**一次性**凭据交给一个没有任何频率约束的
        循环——一次性令牌被高频消费正是 2026-08-08 授权码被烧那次事故的形状。此前这条
        靠一道"两个参数必须成对"的运行时守卫，守卫本身就说明 API 允许拆开；现在从形状上
        就拆不开，因此这里断言的是**签名**：新增的 ``min_interval``/``daily_limit`` 只能
        调整门槛大小（``test_the_ceiling_cannot_be_disabled_by_a_sentinel_value`` 钉住
        "不能传值把检查关掉"这一半），不能新增一个让检查整体消失的开关。
        """

        import inspect

        from lingxi.adapters.delegated_credentials import HostFileDelegatedCredentialVault

        parameters = inspect.signature(HostFileDelegatedCredentialVault.claim_due).parameters

        self.assertEqual(
            sorted(name for name in parameters if name != "self"),
            ["daily_limit", "for_supply", "lease_seconds", "min_interval", "now"],
            "多出来的旋钮就是一条可以绕过频率上界的路",
        )
        self.assertIs(parameters["for_supply"].default, False, "默认必须是到期驱动的那条路径")


class UndecryptableCredentialFileTest(unittest.TestCase):
    """对抗审查 2026-09-02 C-2：主密钥配错**不得**删掉凭据文件。

    这条**不需要数据库**，而且"不需要"本身就是被测的性质：修好的实现在解密失败
    那一刻就返回，根本走不到主体核对（唯一要连库的一步）。因此这里用一个故意
    不可达的 DSN——一旦有人把"先判解密"挪回"先判主体"之后，这条用例会因为连不上
    ``.invalid`` 而**响亮失败**，而不是安静地变成一条"看起来通过了"的用例。

    被防的事故形状：scheduler 与 reauthorize 两侧的
    ``LINGXI_DELEGATED_CREDENTIAL_KEY`` 不一致时，轮换扫描会把"解不开"读成
    "文件主体与登记不一致"并 ``unlink`` 掉密文——一次性 ``refresh_token`` 就此
    永久丢失，把密钥改回正确值也救不回来，只能请产品负责人重新走一次 OAuth Bridge
    授权。删是不可逆的，留是可逆的。
    """

    UNREACHABLE_DSN = "postgresql://lingxi-guard-test.invalid:1/never-reached"

    def _vault_and_path(self, key: str):
        from lingxi.adapters.delegated_credentials import HostFileDelegatedCredentialVault

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "delegated-credential.enc"
        return HostFileDelegatedCredentialVault(self.UNREACHABLE_DSN, key, str(path)), path

    @staticmethod
    def _ciphertext(key: str, payload: dict) -> bytes:
        import json

        from cryptography.fernet import Fernet

        return Fernet(key.encode()).encrypt(json.dumps(payload).encode())

    def _valid_payload(self) -> dict:
        moment = datetime.now(UTC)
        return {
            "generation": "01JCREDENTIALGENERATION0001",
            "subject_open_id": "ou_delegated_authorization_subject",
            "refresh_token": FAKE_TOKEN,
            "scope": "offline_access",
            "issued_at": moment.isoformat(),
            "refresh_at": (moment + timedelta(days=5)).isoformat(),
            "expires_at": (moment + timedelta(days=7)).isoformat(),
            "consumed_at": None,
            "refresh_consumed_at": None,
            "refresh_consumed_count": 0,
        }

    def test_reading_with_the_wrong_master_key_keeps_the_file_byte_for_byte(self) -> None:
        from cryptography.fernet import Fernet

        writing_key = Fernet.generate_key().decode()
        reading_key = Fernet.generate_key().decode()
        vault, path = self._vault_and_path(reading_key)
        ciphertext = self._ciphertext(writing_key, self._valid_payload())
        path.write_bytes(ciphertext)

        # 两条读取路径都不得删文件，也都不得连库（DSN 不可达即证）。
        self.assertIsNone(vault.load(), "解不开时不得返回凭据")
        self.assertIsNone(vault.claim_due(), "解不开时不得领取")

        self.assertTrue(path.exists(), "主密钥配错不得删除凭据文件")
        self.assertEqual(path.read_bytes(), ciphertext, "密文必须字节不变")

    def test_the_right_key_still_reads_the_same_file_afterwards(self) -> None:
        """ "留着"的意义在于可恢复：密钥换回来，同一份文件照常可用。"""

        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()
        wrong_vault, path = self._vault_and_path(Fernet.generate_key().decode())
        path.write_bytes(self._ciphertext(key, self._valid_payload()))

        self.assertIsNone(wrong_vault.load())
        self.assertTrue(path.exists())

        from lingxi.adapters.delegated_credentials import HostFileDelegatedCredentialVault

        # 换成配对的密钥后能真的解开——用 `_read_payload` 断言到 payload 级别，
        # `load()` 那一步要连库核对主体，本用例刻意不提供数据库。
        right_vault = HostFileDelegatedCredentialVault(self.UNREACHABLE_DSN, key, str(path))
        payload = right_vault._read_payload()
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["subject_open_id"], "ou_delegated_authorization_subject")

    def test_a_corrupt_file_is_reported_as_undecryptable_not_as_an_empty_payload(self) -> None:
        """哨兵与"解开了、内容为空"必须分得开——同形正是 C-2 的根因。"""

        from cryptography.fernet import Fernet

        from lingxi.adapters.delegated_credentials import UNDECRYPTABLE

        key = Fernet.generate_key().decode()
        vault, path = self._vault_and_path(key)

        path.write_bytes(b"\x01\x02broken")
        self.assertIs(vault._read_payload(), UNDECRYPTABLE)

        # 没有文件 / 空文件仍然是 None，不能被哨兵吞掉。
        path.unlink()
        self.assertIsNone(vault._read_payload())
        path.write_bytes(b"")
        self.assertIsNone(vault._read_payload())

        # 真的空 payload 仍然是 dict：它才是"解开了、但什么都没有"。
        path.write_bytes(self._ciphertext(key, {}))
        self.assertEqual(vault._read_payload(), {})

    def test_write_and_targeted_revoke_paths_also_refuse_to_touch_the_file(self) -> None:
        """代际判据同样读不出来：写回与定向撤销都必须放弃，且都不删文件。"""

        from cryptography.fernet import Fernet

        writing_key = Fernet.generate_key().decode()
        vault, path = self._vault_and_path(Fernet.generate_key().decode())
        ciphertext = self._ciphertext(writing_key, self._valid_payload())
        path.write_bytes(ciphertext)

        self.assertFalse(
            vault.save(
                subject_open_id="ou_delegated_authorization_subject",
                grant=AuthorizationGrant(SecretToken(FAKE_TOKEN), 3600, ""),
                replacing_generation="01JCREDENTIALGENERATION0001",
            ),
            "代际核对不了就不得覆盖",
        )
        self.assertFalse(
            vault.revoke(reason="test", generation="01JCREDENTIALGENERATION0001"),
            "代际核对不了就不得定向撤销",
        )
        self.assertFalse(vault.revoke_stale_consumed(), "读不出消费时刻就不得收殓")

        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), ciphertext, "四条路径都不得改动密文")


class ConsumptionMomentConversionTest(unittest.TestCase):
    """每日上界读取消费时刻的换算本体（O6②）。

    这一段此前没有任何直接断言：把换算整个删掉、直接取字符串前十位，全量用例依然全绿。
    它是"今天"这件事在**凭据文件**那一侧的唯一定义。
    """

    def test_a_non_utc_offset_is_converted_before_the_day_is_taken(self) -> None:
        from lingxi.adapters.delegated_credentials import _parse_utc

        # 同一时刻的两种写法：UTC 8-18 23:30 == 东八区 8-19 07:30。
        self.assertEqual(_parse_utc("2026-08-18T23:30:00+00:00").date(), date(2026, 8, 18))
        self.assertEqual(_parse_utc("2026-08-19T07:30:00+08:00").date(), date(2026, 8, 18))
        # 反向：西五区的 8-18 21:00 已经是 UTC 的 8-19。
        self.assertEqual(_parse_utc("2026-08-18T21:00:00-05:00").date(), date(2026, 8, 19))

    def test_a_naive_moment_is_read_as_utc(self) -> None:
        from lingxi.adapters.delegated_credentials import _parse_utc

        moment = _parse_utc("2026-08-18T23:30:00")

        self.assertEqual(moment.date(), date(2026, 8, 18))
        self.assertEqual(moment.utcoffset(), timedelta(0), "读出来必须是带时区的 UTC 时刻")

    def test_anything_unparsable_yields_nothing(self) -> None:
        """读不出时刻时返回 ``None``＝"没有消费记录"，上界因此不拦。

        方向是刻意的：一份读不懂的旧载荷不该把凭据永久锁死；真正的防线是文件本身
        只由本模块写入。
        """

        from lingxi.adapters.delegated_credentials import _parse_utc

        for broken in (None, "", "not-a-moment", 12345, {}):
            with self.subTest(value=broken):
                self.assertIsNone(_parse_utc(broken))


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
