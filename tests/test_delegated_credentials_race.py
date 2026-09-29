"""Issue #903：委托凭据 ``load()`` 的判断与撤销必须和读取落在同一把文件锁里。

被防的三条路径（均为「A 读完释放锁 → B 改写 → A 按旧快照判断」的形状）：

1. A 读到未消费的凭据 G，B ``claim_due()`` 给 G 置消费标记，A 仍把 G 返回；
2. A 读到已过期的 G，B 为同一主体 ``save()`` 新凭据 H，A 的过期撤销删掉 H；
3. A 读到主体不一致 / 字段解不出来的 G，B ``save()`` 新凭据 H，A 的清除删掉 H。

**确定性交错，不靠线程赌时序**：B 的操作挂在 A 的判断点上，两种钩子——

- 消费判断（路径 1）：A 读到的载荷被包一层，A 读取 ``consumed_at`` 的那一刻先用
  非阻塞方式探一下文件锁。锁空着说明 A 在锁外判断（修前的形状），B 当场执行，
  置位落在 A 的读取与判断之间；锁被占着说明 A 仍在锁内（修后的形状），B 插不进去，
  推迟到 ``load()`` 返回之后执行，等价于 A、B 串行。
- 撤销 / 清除（路径 2、3）：A **第一次放锁**的那一刻执行 B。修前 A 读完就放锁，
  B 的新凭据落在 A 的删除之前；修后 A 在同一把锁里判断并删除完才放锁，B 在其后。

两种顺序都按「串行执行下应有的结果」断言，因此修前必红、修后必绿，与机器快慢无关。
本文件不连真库：模块里的 ``connect`` 换成内存里的登记表桩。
"""

from __future__ import annotations

import fcntl
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from lingxi.core.identity.credentials import AuthorizationGrant, SecretToken

SUBJECT = "ou_delegated_authorization_subject"
OTHER_SUBJECT = "ou_delegated_authorization_subject_new"
OLD_TOKEN = "fake-refresh-token-old-generation"
NEW_TOKEN = "fake-refresh-token-new-generation"
OLD_GENERATION = "01JRACEOLDGENERATION000001"


class _FakeRegistry:
    """内存里的 ``feishu_delegated_subject`` 登记表。"""

    def __init__(self, registered: str | None) -> None:
        self.registered = registered

    def connect(self, dsn: str, *, timeouts: Any = None) -> _FakeConnection:
        del dsn, timeouts
        return _FakeConnection(self)


class _FakeConnection:
    def __init__(self, registry: _FakeRegistry) -> None:
        self._registry = registry

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._registry)


class _FakeCursor:
    def __init__(self, registry: _FakeRegistry) -> None:
        self._registry = registry
        self._row: tuple[str] | None = None

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        statement = " ".join(sql.split()).upper()
        if statement.startswith("SELECT SUBJECT_OPEN_ID"):
            registered = self._registry.registered
            self._row = None if registered is None else (registered,)
            return
        if (
            statement.startswith("INSERT INTO FEISHU_DELEGATED_SUBJECT")
            and "DO UPDATE" in statement
        ):
            self._registry.registered = str(params[1])
            self._row = None
            return
        raise AssertionError(f"桩登记表不认识这条语句：{statement}")

    def fetchone(self) -> tuple[str] | None:
        return self._row


class LoadDecidesUnderTheSameLockTest(unittest.TestCase):
    """三条路径各一条确定性交错用例；见模块说明。"""

    def setUp(self) -> None:
        from cryptography.fernet import Fernet

        from lingxi.adapters.delegated_credentials import HostFileDelegatedCredentialVault

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "delegated-credential.enc"
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.registry = _FakeRegistry(SUBJECT)
        patcher = mock.patch("lingxi.adapters.delegated_credentials.connect", self.registry.connect)
        patcher.start()
        self.addCleanup(patcher.stop)
        # DSN 不会被用到：所有连库都走上面的桩。
        self.vault = HostFileDelegatedCredentialVault(
            "postgresql://race-test.invalid:1/never-reached",
            Fernet.generate_key().decode(),
            str(self.path),
        )
        self.now = datetime.now(UTC)
        self.deferred: list[Any] = []
        self.interleaved: bool | None = None

    # ---- 场景搭建 ------------------------------------------------------------

    def _write_old_credential(self, **overrides: Any) -> None:
        payload: dict[str, Any] = {
            "generation": OLD_GENERATION,
            "subject_open_id": SUBJECT,
            "refresh_token": OLD_TOKEN,
            "scope": "offline_access",
            "issued_at": (self.now - timedelta(days=6)).isoformat(),
            "refresh_at": (self.now - timedelta(minutes=1)).isoformat(),
            "expires_at": (self.now + timedelta(days=1)).isoformat(),
            "consumed_at": None,
            "refresh_consumed_at": None,
            "refresh_consumed_count": 0,
        }
        payload.update(overrides)
        self.vault._write_encrypted(payload)

    def _lock_is_free(self) -> bool:
        with open(self.lock_path, "a+b") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return True

    def _interleave_at_consumption_check(self, operation: Any) -> None:
        """A 读 ``consumed_at`` 那一刻安排 B：锁空着当场插进去，锁被占着排到 A 之后。"""

        test = self

        class _WatchedPayload(dict):
            def _before_consumption_check(self) -> None:
                if test.interleaved is not None:
                    return
                if test._lock_is_free():
                    test.interleaved = True
                    operation()
                else:
                    test.interleaved = False
                    test.deferred.append(operation)

            def get(self, key: Any, default: Any = None) -> Any:
                if key == "consumed_at":
                    self._before_consumption_check()
                return super().get(key, default)

            def __getitem__(self, key: Any) -> Any:
                if key == "consumed_at":
                    self._before_consumption_check()
                return super().__getitem__(key)

        original_read = self.vault._read_payload
        wrapped = [False]

        def read_payload() -> Any:
            payload = original_read()
            if not wrapped[0] and isinstance(payload, dict):
                wrapped[0] = True
                return _WatchedPayload(payload)
            return payload

        self.vault._read_payload = read_payload

    def _interleave_at_first_unlock(self, operation: Any) -> None:
        """A 第一次放锁的那一刻执行 B（B 自己再按正常流程取锁）。"""

        original_locked = self.vault._locked
        armed = [True]

        class _UnlockHook:
            def __init__(self) -> None:
                self._inner = original_locked()

            def __enter__(self) -> _UnlockHook:
                self._inner.__enter__()
                return self

            def __exit__(self, *exc_info: object) -> None:
                self._inner.__exit__(*exc_info)
                if armed[0]:
                    armed[0] = False
                    operation()

        def locked() -> Any:
            return _UnlockHook() if armed[0] else original_locked()

        self.vault._locked = locked
        # 放锁钩子下 B 一定在 A 放锁之后执行；是否落在 A 的删除之前，由断言判定。
        self.interleaved = True

    def _load_as_process_a(self):
        result = self.vault.load(now=self.now)
        for operation in self.deferred:
            operation()
        self.assertIsNotNone(self.interleaved, "钩子没有触发：A 没有做消费判断，用例失去意义")
        return result

    def _save_new_credential_as_process_b(self, subject: str = SUBJECT) -> None:
        saved = self.vault.save(
            subject_open_id=subject,
            grant=AuthorizationGrant(SecretToken(NEW_TOKEN), 7 * 24 * 3600, "offline_access"),
            issued_at=self.now,
        )
        self.assertTrue(saved, "B 的新授权本身必须写入成功")

    def _assert_new_credential_survives(self) -> None:
        payload = self.vault._read_payload()
        self.assertIsInstance(payload, dict, "B 写入的新凭据被删掉了")
        self.assertNotEqual(payload["generation"], OLD_GENERATION, "磁盘上不该还是旧一代")
        self.assertEqual(payload["refresh_token"], NEW_TOKEN, "磁盘上必须是 B 的新凭据")

    # ---- 路径 1：不得返回已消费的凭据 --------------------------------------

    def test_load_never_hands_out_a_credential_consumed_before_its_decision(self) -> None:
        self._write_old_credential()
        claimed: list[Any] = []
        self._interleave_at_consumption_check(
            lambda: claimed.append(self.vault.claim_due(now=self.now))
        )

        result = self._load_as_process_a()

        self.assertEqual(len(claimed), 1)
        self.assertIsNotNone(claimed[0], "B 必须真的领取到了旧凭据，否则场景没搭起来")
        if self.interleaved:
            # B 的消费标记落在 A 判断之前：模块承诺「置位后旧密文对任何读取都不可见」。
            self.assertIsNone(result, "A 返回了一条在它判断之前已经被消费的凭据")
        else:
            # 串行：A 判断时 G 尚未消费，返回 G 是合法的；随后 B 照常领取。
            self.assertIsNotNone(result)
            self.assertEqual(result.generation, OLD_GENERATION)

    # ---- 路径 2：过期撤销不得删掉更新世代的凭据 ----------------------------

    def test_expired_revoke_in_load_never_deletes_a_newer_generation(self) -> None:
        self._write_old_credential(expires_at=(self.now - timedelta(minutes=1)).isoformat())
        self._interleave_at_first_unlock(self._save_new_credential_as_process_b)

        result = self._load_as_process_a()

        self.assertIsNone(result, "过期凭据不得返回")
        self._assert_new_credential_survives()

    # ---- 路径 3：主体不一致清除 / 解不出来撤销同理 --------------------------

    def test_subject_mismatch_cleanup_in_load_never_deletes_a_newer_generation(self) -> None:
        # 登记已指向新主体、文件仍是旧主体：A 会走「主体不一致、清除文件」分支。
        self.registry.registered = OTHER_SUBJECT
        self._write_old_credential()
        self._interleave_at_first_unlock(
            lambda: self._save_new_credential_as_process_b(OTHER_SUBJECT)
        )

        result = self._load_as_process_a()

        self.assertIsNone(result, "主体不一致的凭据不得返回")
        self._assert_new_credential_survives()

    def test_unparsable_credential_revoke_in_load_never_deletes_a_newer_generation(self) -> None:
        # 解密成功但缺少必需字段：A 会走「解不出来、撤销」分支。
        self._write_old_credential(refresh_token=None)
        self._interleave_at_first_unlock(self._save_new_credential_as_process_b)

        result = self._load_as_process_a()

        self.assertIsNone(result, "字段不全的凭据不得返回")
        self._assert_new_credential_survives()

    # ---- 串行语义不变 --------------------------------------------------------

    def test_without_interference_load_still_cleans_up_what_it_should(self) -> None:
        """修法只收紧并发窗口，不改变单独调用时的撤销结果。"""

        self._write_old_credential(expires_at=(self.now - timedelta(minutes=1)).isoformat())
        self.assertIsNone(self.vault.load(now=self.now))
        self.assertFalse(self.path.exists(), "过期凭据仍应被撤销")

        self._write_old_credential()
        self.registry.registered = OTHER_SUBJECT
        self.assertIsNone(self.vault.load(now=self.now))
        self.assertFalse(self.path.exists(), "主体不一致的凭据仍应被清除")

        self.registry.registered = SUBJECT
        self._write_old_credential(refresh_token=None)
        self.assertIsNone(self.vault.load(now=self.now))
        self.assertFalse(self.path.exists(), "字段不全的凭据仍应被撤销")

        self._write_old_credential(consumed_at=self.now.isoformat())
        self.assertIsNone(self.vault.load(now=self.now), "消费中的凭据不得返回")
        self.assertTrue(self.path.exists(), "消费中的凭据由收殓流程处理，load 不删")

        self._write_old_credential()
        loaded = self.vault.load(now=self.now)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.generation, OLD_GENERATION)


if __name__ == "__main__":
    unittest.main()
