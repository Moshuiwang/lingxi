"""OAuth Worker 到 biai-stage 的用户隔离断言。"""

from __future__ import annotations

import json
import unittest

from lingxi.adapters.oauth_bridge import LoadedOAuthIdentity, OAuthBridgeMessage, OAuthResultProcessor, OAuthTokenGrant
from lingxi.core.identity.onboarding import IdentityProfile, InMemoryOnboardingStore, OnboardingService


class FakeStateStore:
    def __init__(self, accepted_open_id: str = "ou_expected") -> None:
        self.accepted_open_id = accepted_open_id
        self.claimed = True
        self.cancelled: list[str] = []

    def claim_authorizing_state(self, _state: str) -> bool:
        return self.claimed

    def complete_authorizing_state(self, _state: str, open_id: str) -> bool:
        return open_id == self.accepted_open_id

    def cancel_authorizing_state(self, state: str) -> bool:
        self.cancelled.append(state)
        return True


class FakeLoader:
    def __init__(self, profile: IdentityProfile, debug_details: dict[str, object] | None = None) -> None:
        self.profile = profile
        self.debug_details = debug_details
        self.codes: list[str] = []

    def from_authorization_code(self, code: str) -> LoadedOAuthIdentity:
        self.codes.append(code)
        return LoadedOAuthIdentity(self.profile, self.debug_details)


class FakeGrantLoader(FakeLoader):
    def __init__(self, profile: IdentityProfile, grant: OAuthTokenGrant, debug_details: dict[str, object] | None = None) -> None:
        super().__init__(profile, debug_details)
        self.grant = grant

    def from_authorization_code(self, code: str) -> LoadedOAuthIdentity:
        self.codes.append(code)
        return LoadedOAuthIdentity(self.profile, self.debug_details, self.grant)


class FakeResultSender:
    def __init__(self) -> None:
        self.results: list[tuple[str, str]] = []
        self.debug_identities: list[dict[str, str | None] | None] = []
        self.debug_details: list[dict[str, object] | None] = []

    def send_result(
        self,
        state: str,
        status: str,
        debug_identity: dict[str, str | None] | None = None,
        debug_details: dict[str, object] | None = None,
    ) -> None:
        self.results.append((state, status))
        self.debug_identities.append(debug_identity)
        self.debug_details.append(debug_details)


class FakeRefreshVault:
    def __init__(self) -> None:
        self.saved: list[tuple[str, OAuthTokenGrant]] = []

    def save(self, open_id: str, grant: OAuthTokenGrant) -> None:
        self.saved.append((open_id, grant))


class FailingLoader:
    def from_authorization_code(self, code: str) -> LoadedOAuthIdentity:
        raise RuntimeError("authorization code must never enter logs")


class OAuthResultProcessorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.state = "s" * 32
        self.profile = IdentityProfile("ou_expected", "user_expected", "union_expected", "测试用户", None, None, None)
        self.identity_store = InMemoryOnboardingStore()
        self.state_store = FakeStateStore()
        self.loader = FakeLoader(self.profile)
        self.sender = FakeResultSender()
        self.processor = OAuthResultProcessor(self.state_store, OnboardingService(self.identity_store), self.loader, self.sender, "test-event-key")

    def test_matching_authorization_creates_identity_only_after_state_check(self) -> None:
        self.processor.process(OAuthBridgeMessage("oauth_code", self.state, "one-time-code"))

        self.assertEqual(self.identity_store.user_count(), 1)
        self.assertEqual(self.sender.results, [(self.state, "identity_confirmed")])

    def test_wrong_identity_is_rejected_without_creating_user(self) -> None:
        self.state_store.accepted_open_id = "ou_other"
        self.processor.process(OAuthBridgeMessage("oauth_code", self.state, "one-time-code"))

        self.assertEqual(self.identity_store.user_count(), 0)
        self.assertEqual(self.sender.results, [(self.state, "retry")])
        self.assertEqual(self.state_store.cancelled, [self.state])

    def test_replayed_state_never_exchanges_the_authorization_code(self) -> None:
        self.state_store.claimed = False
        self.processor.process(OAuthBridgeMessage("oauth_code", self.state, "one-time-code"))

        self.assertEqual(self.loader.codes, [])
        self.assertEqual(self.identity_store.user_count(), 0)
        self.assertEqual(self.sender.results, [(self.state, "retry")])

    def test_cancelled_authorization_never_loads_identity(self) -> None:
        self.processor.process(OAuthBridgeMessage("oauth_cancelled", self.state))

        self.assertEqual(self.loader.codes, [])
        self.assertEqual(self.identity_store.user_count(), 0)
        self.assertEqual(self.sender.results, [(self.state, "retry")])

    def test_failed_identity_load_records_only_the_failure_kind(self) -> None:
        self.processor._loader = FailingLoader()

        with self.assertLogs("lingxi.adapters.oauth_bridge", level="WARNING") as logs:
            self.processor.process(OAuthBridgeMessage("oauth_code", self.state, "one-time-code"))

        self.assertEqual(self.identity_store.user_count(), 0)
        self.assertEqual(self.sender.results, [(self.state, "retry")])
        self.assertIn("RuntimeError", logs.output[0])
        self.assertNotIn("one-time-code", logs.output[0])
        self.assertNotIn("authorization code must never enter logs", logs.output[0])

    def test_loaded_profile_records_only_three_id_presence(self) -> None:
        with self.assertLogs("lingxi.adapters.oauth_bridge", level="WARNING") as logs:
            self.processor.process(OAuthBridgeMessage("oauth_code", self.state, "one-time-code"))

        self.assertIn("open_id=True user_id=True union_id=True", logs.output[0])
        self.assertNotIn("ou_expected", logs.output[0])
        self.assertNotIn("user_expected", logs.output[0])
        self.assertNotIn("union_expected", logs.output[0])

    def test_timeline_log_records_stages_without_authorization_code_or_identity(self) -> None:
        with self.assertLogs("lingxi.adapters.oauth_bridge", level="INFO") as logs:
            self.processor.process(OAuthBridgeMessage("oauth_code", self.state, "one-time-code"))

        timeline = "\n".join(logs.output)
        self.assertIn("authorization callback received", timeline)
        self.assertIn("callback state claimed", timeline)
        self.assertIn("authorized identity recorded", timeline)
        self.assertIn("authorization completed", timeline)
        self.assertNotIn("one-time-code", timeline)
        self.assertNotIn("ou_expected", timeline)

    def test_organization_probe_returns_result_without_creating_identity_or_storing_credential(self) -> None:
        profile = IdentityProfile("ou_expected", "", "union_expected", "测试用户", None, None, None)
        sender = FakeResultSender()
        processor = OAuthResultProcessor(
            self.state_store,
            OnboardingService(self.identity_store),
            FakeLoader(profile, {"所属组织": {"名字": "测试组织"}}),
            sender,
            "test-event-key",
            organization_probe_only=True,
            debug_identity_display=True,
        )

        processor.process(OAuthBridgeMessage("oauth_code", self.state, "one-time-code"))

        self.assertEqual(self.identity_store.user_count(), 0)
        self.assertEqual(sender.results, [(self.state, "identity_confirmed")])
        self.assertEqual(sender.debug_details, [{"所属组织": {"名字": "测试组织"}}])

    def test_normal_authorization_saves_only_the_renewable_grant_after_identity_binding(self) -> None:
        vault = FakeRefreshVault()
        grant = OAuthTokenGrant("rotating-refresh-token", 604800, "offline_access")

        processor = OAuthResultProcessor(
            self.state_store,
            OnboardingService(self.identity_store),
            FakeGrantLoader(self.profile, grant),
            self.sender,
            "test-event-key",
            token_vault=vault,
        )
        processor.process(OAuthBridgeMessage("oauth_code", self.state, "one-time-code"))

        self.assertEqual(vault.saved, [("ou_expected", grant)])

    def test_probe_can_save_renewable_grant_without_creating_identity_record(self) -> None:
        vault = FakeRefreshVault()
        grant = OAuthTokenGrant("rotating-refresh-token", 604800, "offline_access")
        profile = IdentityProfile("ou_expected", "", "union_expected", "测试用户", None, None, None)
        processor = OAuthResultProcessor(
            self.state_store,
            OnboardingService(self.identity_store),
            FakeGrantLoader(profile, grant, {"可见关联组织": {}}),
            self.sender,
            "test-event-key",
            token_vault=vault,
            organization_probe_only=True,
            persist_probe_credential=True,
        )

        processor.process(OAuthBridgeMessage("oauth_code", self.state, "one-time-code"))

        self.assertEqual(self.identity_store.user_count(), 0)
        self.assertEqual(vault.saved, [("ou_expected", grant)])
        self.assertEqual(self.sender.results, [(self.state, "identity_confirmed")])

    def test_identity_values_are_only_returned_when_test_debug_is_explicitly_enabled(self) -> None:
        self.processor.process(OAuthBridgeMessage("oauth_code", self.state, "one-time-code"))
        self.assertEqual(self.sender.debug_identities, [None])
        self.assertEqual(self.sender.debug_details, [None])

        debug_sender = FakeResultSender()
        processor = OAuthResultProcessor(
            self.state_store,
            OnboardingService(InMemoryOnboardingStore()),
            self.loader,
            debug_sender,
            "test-event-key",
            debug_identity_display=True,
        )
        processor.process(OAuthBridgeMessage("oauth_code", self.state, "another-one-time-code"))

        self.assertEqual(debug_sender.debug_identities, [{
            "open_id": "ou_expected",
            "user_id": "user_expected",
            "union_id": "union_expected",
            "name": "测试用户",
            "department": None,
            "tenant_key": None,
            "locale": None,
        }])

    def test_test_debug_forwards_only_the_explicit_identity_report(self) -> None:
        report = {"所属部门": [{"id": "od-child", "name": "子部门", "children": []}]}
        sender = FakeResultSender()
        processor = OAuthResultProcessor(
            self.state_store,
            OnboardingService(InMemoryOnboardingStore()),
            FakeLoader(self.profile, report),
            sender,
            "test-event-key",
            debug_identity_display=True,
        )

        processor.process(OAuthBridgeMessage("oauth_code", self.state, "one-time-code"))

        self.assertEqual(sender.debug_details, [report])

    def test_malformed_messages_are_rejected_before_processing(self) -> None:
        with self.assertRaises(ValueError):
            OAuthBridgeMessage.parse('{"type":"oauth_code","state":"short","code":"x"}')
        with self.assertRaises(ValueError):
            OAuthBridgeMessage.parse(json.dumps({"type": "oauth_code", "state": "x" * 32 + ".", "code": "x"}))


if __name__ == "__main__":
    unittest.main()
