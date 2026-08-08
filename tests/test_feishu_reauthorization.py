"""正式重授权入口的 state、身份绑定和失败关闭断言。

认领 Issue #67 阶段 A、`V-身份-03`、`V-身份-04` 与 `V-身份-06` 的新增安全面。传输、换码
和 vault 都使用可注入替身；真实飞书授权属于 E1 的 biai-stage L4a，不在本地
单测里冒充通过。
"""

from __future__ import annotations

import json
import logging
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from lingxi.apps.reauthorize import handle_bridge_message
from lingxi.adapters.feishu_directory import AuthorizationExchange
from lingxi.adapters.feishu_reauthorization import (
    FeishuReauthorizationEntry,
    HostFileAuthorizationStateStore,
    ReauthorizationResult,
)
from lingxi.adapters.oauth_bridge_client import OAuthBridgeClient, OAuthBridgeMessage
from lingxi.core.identity.credentials import AuthorizationGrant, SecretToken


NOW = datetime(2026, 8, 8, 7, 0, tzinfo=timezone.utc)
EXPECTED_SUBJECT = "ou_delegated_subject"
OTHER_SUBJECT = "ou_other_subject"
FAKE_CODE = "fake-one-time-code"
FAKE_ACCESS_TOKEN = "fake-access-token"
FAKE_REFRESH_TOKEN = "fake-refresh-token"
_EXPECTED_SUBJECT_MISSING = object()


def exchange(subject: str = EXPECTED_SUBJECT) -> AuthorizationExchange:
    return AuthorizationExchange(
        subject_open_id=subject,
        grant=AuthorizationGrant(
            SecretToken(FAKE_REFRESH_TOKEN),
            604800,
            "auth:user.id:read offline_access",
        ),
    )


class FakeExchanger:
    def __init__(self, result: AuthorizationExchange | Exception) -> None:
        self.result = result
        self.calls: list[tuple[str, str, str]] = []

    def exchange_authorization_code(
        self,
        code: str,
        *,
        redirect_uri: str,
        required_scope: str,
    ) -> AuthorizationExchange:
        self.calls.append((code, redirect_uri, required_scope))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeVault:
    def __init__(self, registered_subject: str | None = EXPECTED_SUBJECT, save_result: bool = True) -> None:
        self.registered_subject = registered_subject
        self.save_result = save_result
        self.saved: list[tuple[str, AuthorizationGrant]] = []
        self.after_registered_subject_read = None

    def registered_subject_open_id(self) -> str | None:
        subject = self.registered_subject
        if self.after_registered_subject_read is not None:
            self.after_registered_subject_read()
        return subject

    def save(
        self,
        *,
        subject_open_id: str,
        grant: AuthorizationGrant,
        expected_registered_subject_open_id: object = _EXPECTED_SUBJECT_MISSING,
        **_kwargs: object,
    ) -> bool:
        if expected_registered_subject_open_id is not _EXPECTED_SUBJECT_MISSING:
            if expected_registered_subject_open_id != self.registered_subject:
                return False
        if self.save_result:
            self.saved.append((subject_open_id, grant))
        return self.save_result


class FakeBridgeResultSender:
    def __init__(self) -> None:
        self.results: list[tuple[str, str]] = []

    def send_result(self, state: str, status: str, **_kwargs: object) -> None:
        self.results.append((state, status))


class RecordingDefaultProcessor:
    def __init__(self, messages: list[OAuthBridgeMessage]) -> None:
        self.messages = messages

    def process(self, message: OAuthBridgeMessage) -> None:
        self.messages.append(message)


class StateStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "reauth-state.json"
        self.store = HostFileAuthorizationStateStore(str(self.path), "state-integrity-key")

    def test_state_is_opaque_one_time_and_file_is_restricted(self) -> None:
        state, expires_at = self.store.issue(EXPECTED_SUBJECT, ttl_seconds=600, now=NOW)

        self.assertRegex(state, r"^[A-Za-z0-9_-]{32,256}$")
        self.assertGreater(expires_at, NOW)
        self.assertTrue(self.path.exists())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        contents = self.path.read_text(encoding="utf-8")
        self.assertNotIn(state, contents)
        self.assertNotIn(FAKE_CODE, contents)
        self.assertNotIn(FAKE_REFRESH_TOKEN, contents)

        claimed = self.store.claim(state, now=NOW + timedelta(seconds=1))
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed.expected_subject_open_id, EXPECTED_SUBJECT)
        self.assertFalse(self.path.exists(), "state 成功领取后必须立即消费")
        self.assertIsNone(self.store.claim(state, now=NOW + timedelta(seconds=2)))

    def test_missing_expired_and_mismatched_states_are_rejected(self) -> None:
        self.assertIsNone(self.store.claim("s" * 32, now=NOW))

        expired, _ = self.store.issue(EXPECTED_SUBJECT, ttl_seconds=1, now=NOW)
        self.assertIsNone(self.store.claim(expired, now=NOW + timedelta(seconds=1)))
        self.assertFalse(self.path.exists())

        valid, _ = self.store.issue(EXPECTED_SUBJECT, ttl_seconds=600, now=NOW)
        self.assertIsNone(self.store.claim("x" * len(valid), now=NOW + timedelta(seconds=1)))
        self.assertTrue(self.path.exists(), "错配 state 不得消费正确 state")
        self.assertIsNotNone(self.store.claim(valid, now=NOW + timedelta(seconds=2)))

    def test_tampered_state_record_fails_closed(self) -> None:
        self.store.issue(EXPECTED_SUBJECT, ttl_seconds=600, now=NOW)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["expected_subject_open_id"] = OTHER_SUBJECT
        self.path.write_text(json.dumps(raw), encoding="utf-8")

        self.assertIsNone(self.store.claim("s" * 43, now=NOW))
        self.assertFalse(self.path.exists())


class ReauthorizationEntryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_store = HostFileAuthorizationStateStore(
            str(Path(self.directory.name) / "reauth-state.json"),
            "state-integrity-key",
        )
        self.vault = FakeVault()
        self.exchanger = FakeExchanger(exchange())
        self.entry = FeishuReauthorizationEntry(
            app_id="cli_fake",
            redirect_uri="https://stage.example.test/reauth/callback",
            scope="auth:user.id:read offline_access",
            authorization_endpoint="https://accounts.example.test/open-apis/authen/v1/authorize",
            state_store=self.state_store,
            vault=self.vault,
            exchanger=self.exchanger,
            state_ttl_seconds=600,
        )

    def _begin(self, *, now: datetime = NOW) -> str:
        start = self.entry.begin(now=now)
        query = parse_qs(urlparse(start.authorization_url).query)
        self.assertEqual(query["client_id"], ["cli_fake"])
        self.assertEqual(query["redirect_uri"], ["https://stage.example.test/reauth/callback"])
        self.assertEqual(query["state"], [start.state])
        self.assertEqual(query["scope"], ["auth:user.id:read offline_access"])
        self.assertNotIn(FAKE_REFRESH_TOKEN, start.authorization_url)
        return start.state

    def test_success_requires_state_then_exchanges_and_saves_formal_grant(self) -> None:
        state = self._begin()

        result = self.entry.handle_callback(state, code=FAKE_CODE, now=NOW + timedelta(seconds=1))

        self.assertEqual(result, ReauthorizationResult(True, "completed", "专用授权已更新，可以继续组织目录同步。", False))
        self.assertEqual(
            self.exchanger.calls,
            [(FAKE_CODE, "https://stage.example.test/reauth/callback", "auth:user.id:read offline_access")],
        )
        self.assertEqual(len(self.vault.saved), 1)
        self.assertEqual(self.vault.saved[0][0], EXPECTED_SUBJECT)
        self.assertEqual(self.vault.saved[0][1].refresh_token.reveal(), FAKE_REFRESH_TOKEN)

    def test_oauth_bridge_injection_routes_to_formal_reauthorization_without_onboarding(self) -> None:
        state = self._begin(now=datetime.now(timezone.utc))
        sender = FakeBridgeResultSender()
        onboarding_messages: list[OAuthBridgeMessage] = []
        bridge = OAuthBridgeClient(
            "wss://bridge.example.test/oauth/bridge",
            "bridge-token-for-test",
            processor=RecordingDefaultProcessor(onboarding_messages),
        )
        results: list[ReauthorizationResult] = []
        bridge.register_state_handler(
            state,
            lambda message: results.append(handle_bridge_message(self.entry, sender, message)),
        )

        bridge.handle_message(OAuthBridgeMessage("oauth_code", state, FAKE_CODE))

        self.assertEqual(results, [ReauthorizationResult(True, "completed", "专用授权已更新，可以继续组织目录同步。", False)])
        self.assertEqual(onboarding_messages, [])
        self.assertEqual(sender.results, [(state, "identity_confirmed")])
        self.assertEqual(self.vault.saved[0][0], EXPECTED_SUBJECT)
        self.assertEqual(self.exchanger.calls[0][0], FAKE_CODE)

    def test_oauth_bridge_cancellation_uses_formal_retry_result(self) -> None:
        state = self._begin(now=datetime.now(timezone.utc))
        sender = FakeBridgeResultSender()

        result = handle_bridge_message(
            self.entry,
            sender,
            OAuthBridgeMessage("oauth_cancelled", state),
        )

        self.assertEqual(result, ReauthorizationResult(False, "cancelled", "已取消本次授权，未修改凭据，请重新发起授权。", True))
        self.assertEqual(sender.results, [(state, "retry")])
        self.assertEqual(self.exchanger.calls, [])
        self.assertEqual(self.vault.saved, [])

    def test_success_result_and_logs_never_contain_authorization_values(self) -> None:
        state = self._begin()

        with self.assertLogs("lingxi.adapters.feishu_reauthorization", level=logging.INFO) as captured:
            result = self.entry.handle_callback(state, code=FAKE_CODE, now=NOW)

        self._assert_authorization_values_are_absent(result, captured.output)

    def test_missing_expired_and_replayed_states_are_rejected_and_mismatch_does_not_consume_valid_state(self) -> None:
        self.assertFalse(self.entry.handle_callback("s" * 32, code=FAKE_CODE).ok)

        expired = self.entry.begin(now=NOW)
        self.assertFalse(
            self.entry.handle_callback(expired.state, code=FAKE_CODE, now=NOW + timedelta(seconds=601)).ok
        )

        state = self._begin()
        self.assertFalse(self.entry.handle_callback("x" * len(state), code=FAKE_CODE, now=NOW + timedelta(seconds=1)).ok)
        self.assertEqual(self.exchanger.calls, [])
        self.assertTrue(self.entry.handle_callback(state, code=FAKE_CODE, now=NOW + timedelta(seconds=2)).ok)
        self.assertEqual(len(self.exchanger.calls), 1)
        self.assertFalse(self.entry.handle_callback(state, code=FAKE_CODE, now=NOW + timedelta(seconds=3)).ok)
        self.assertEqual(len(self.vault.saved), 1)

    def test_callback_identity_is_read_from_feishu_and_mismatch_does_not_save(self) -> None:
        state = self._begin()
        self.exchanger.result = exchange(OTHER_SUBJECT)

        result = self.entry.handle_callback(state, code=FAKE_CODE, now=NOW + timedelta(seconds=1))

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "identity_mismatch")
        self.assertEqual(self.vault.saved, [])

    def test_subject_registration_change_between_begin_and_callback_blocks_save(self) -> None:
        state = self._begin()
        self.vault.registered_subject = OTHER_SUBJECT

        result = self.entry.handle_callback(state, code=FAKE_CODE, now=NOW + timedelta(seconds=1))

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "subject_changed")
        self.assertEqual(self.vault.saved, [])

    def test_missing_subject_registration_after_begin_is_rejected_without_saving(self) -> None:
        state = self._begin()
        self.vault.registered_subject = None

        result = self.entry.handle_callback(state, code=FAKE_CODE, now=NOW + timedelta(seconds=1))

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "subject_missing")
        self.assertEqual(self.vault.saved, [])

    def test_subject_registration_change_between_read_and_save_is_rejected_by_cas(self) -> None:
        state = self._begin()
        self.vault.after_registered_subject_read = lambda: setattr(self.vault, "registered_subject", OTHER_SUBJECT)

        result = self.entry.handle_callback(state, code=FAKE_CODE, now=NOW + timedelta(seconds=1))

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "persistence_failed")
        self.assertEqual(self.vault.saved, [])
        self.assertEqual(self.vault.registered_subject, OTHER_SUBJECT)

    def test_incomplete_exchange_result_fails_closed_without_saving(self) -> None:
        state = self._begin()
        self.exchanger.result = object()  # type: ignore[assignment]

        result = self.entry.handle_callback(state, code=FAKE_CODE, now=NOW + timedelta(seconds=1))

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "exchange_failed")
        self.assertEqual(self.vault.saved, [])

    def test_returned_scope_must_cover_configuration_before_saving(self) -> None:
        state = self._begin()
        self.exchanger.result = AuthorizationExchange(
            subject_open_id=EXPECTED_SUBJECT,
            grant=AuthorizationGrant(SecretToken(FAKE_REFRESH_TOKEN), 604800, "offline_access"),
        )

        result = self.entry.handle_callback(state, code=FAKE_CODE, now=NOW + timedelta(seconds=1))

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "exchange_failed")
        self.assertEqual(self.vault.saved, [])

    def test_cancel_and_exchange_failure_consume_state_without_saving(self) -> None:
        cancelled_state = self._begin()
        cancelled = self.entry.handle_callback(cancelled_state, error="access_denied", now=NOW + timedelta(seconds=1))
        self.assertEqual(cancelled.code, "cancelled")
        self.assertEqual(self.exchanger.calls, [])
        self.assertIsNone(
            self.state_store.claim(cancelled_state, now=NOW + timedelta(seconds=2)),
            "取消回调也必须先消耗当前 state，不能被同一 state 再次领取",
        )

        failed_state = self._begin()
        self.exchanger.result = RuntimeError("secret error must not be logged")
        with self.assertLogs("lingxi.adapters.feishu_reauthorization", level=logging.INFO) as captured:
            failed = self.entry.handle_callback(failed_state, code=FAKE_CODE, now=NOW + timedelta(seconds=2))
        self.assertEqual(failed.code, "exchange_failed")
        self.assertEqual(self.vault.saved, [])
        self._assert_authorization_values_are_absent(failed, captured.output)

    def test_save_failure_returns_recovery_result_and_does_not_report_success(self) -> None:
        state = self._begin()
        self.vault.save_result = False

        result = self.entry.handle_callback(state, code=FAKE_CODE, now=NOW + timedelta(seconds=1))

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "persistence_failed")
        self.assertTrue(result.retryable)
        self.assertEqual(self.vault.saved, [])

    def test_invalid_callback_shape_does_not_consume_a_valid_state(self) -> None:
        state = self._begin()

        invalid = self.entry.handle_callback(state, code=FAKE_CODE, error="access_denied", now=NOW)

        self.assertEqual(invalid.code, "invalid_callback")
        self.assertEqual(self.exchanger.calls, [])
        valid = self.entry.handle_callback(state, code=FAKE_CODE, now=NOW + timedelta(seconds=1))
        self.assertTrue(valid.ok)

    def test_result_and_logs_never_contain_authorization_values(self) -> None:
        state = self._begin()
        self.exchanger.result = RuntimeError(f"{FAKE_CODE} {FAKE_ACCESS_TOKEN} {FAKE_REFRESH_TOKEN}")

        with self.assertLogs("lingxi.adapters.feishu_reauthorization", level=logging.INFO) as captured:
            result = self.entry.handle_callback(state, code=FAKE_CODE, now=NOW)

        self._assert_authorization_values_are_absent(result, captured.output)

    def _assert_authorization_values_are_absent(self, result: ReauthorizationResult, logs: list[str]) -> None:
        rendered = repr(result) + "\n".join(logs)
        for secret in (FAKE_CODE, FAKE_ACCESS_TOKEN, FAKE_REFRESH_TOKEN):
            self.assertFalse(secret in rendered, "结果或日志包含授权原值")


if __name__ == "__main__":
    unittest.main()
