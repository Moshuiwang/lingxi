"""首次开通的用户体验断言。"""

from __future__ import annotations

import unittest

from lingxi.core.identity.onboarding import (
    IdentityProfile,
    InMemoryOnboardingStore,
    OnboardingService,
    ResponseKind,
)


class OnboardingServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryOnboardingStore()
        self.service = OnboardingService(self.store)
        self.open_id = "ou_test_user"

    def test_unconfirmed_message_only_shows_full_guide_without_identity_record(self) -> None:
        response = self.service.receive_message(self.open_id, "帮我查一下销量")

        self.assertEqual(response.kind, ResponseKind.FULL_GUIDE)
        self.assertEqual(self.store.user_count(), 0)
        self.assertEqual(self.store.saved_business_messages(), ())

    def test_declining_guide_replaces_it_with_a_short_entry_and_user_can_restart(self) -> None:
        self.service.decline_guide(self.open_id)

        response = self.service.receive_message(self.open_id, "你好")
        restart = self.service.confirm_start(self.open_id, "好的，開始使用！")

        self.assertEqual(response.kind, ResponseKind.SHORT_GUIDE)
        self.assertEqual(restart.kind, ResponseKind.AUTHORIZATION_REQUIRED)
        self.assertEqual(self.store.user_count(), 0)

    def test_cancelled_authorization_does_not_create_a_partial_identity_record(self) -> None:
        response = self.service.authorization_cancelled(self.open_id)

        self.assertEqual(response.kind, ResponseKind.AUTHORIZATION_CANCELLED)
        self.assertEqual(self.store.user_count(), 0)

    def test_same_feishu_identity_is_upserted_without_creating_a_second_user(self) -> None:
        profile = IdentityProfile(
            open_id=self.open_id,
            user_id="user_test_user",
            union_id="union_test_user",
            display_name="测试用户",
            department="测试部门",
            tenant_key="tenant_test",
            display_name_locale="zh-CN",
        )

        first = self.service.authorization_succeeded("evt_authorization_01", profile)
        second = self.service.authorization_succeeded("evt_authorization_01", profile)

        self.assertEqual(first.kind, ResponseKind.IDENTITY_CONFIRMED)
        self.assertEqual(second.kind, ResponseKind.IDENTITY_CONFIRMED)
        self.assertEqual(self.store.user_count(), 1)
        self.assertEqual(self.store.get_user(self.open_id).provisioning_state, "matching")
        self.assertEqual(self.store.get_user(self.open_id).profile.union_id, "union_test_user")

    def test_incomplete_authorization_profile_does_not_create_identity_record(self) -> None:
        incomplete_profile = IdentityProfile(
            open_id=self.open_id,
            user_id="user_test_user",
            union_id="",
            display_name="测试用户",
            department=None,
            tenant_key=None,
            display_name_locale=None,
        )

        with self.assertRaises(ValueError):
            self.service.authorization_succeeded("evt_authorization_02", incomplete_profile)

        self.assertEqual(self.store.user_count(), 0)

    def test_non_confirmation_text_does_not_start_authorization_or_store_content(self) -> None:
        response = self.service.confirm_start(self.open_id, "帮我查一下销量")

        self.assertEqual(response.kind, ResponseKind.FULL_GUIDE)
        self.assertEqual(self.store.user_count(), 0)
        self.assertEqual(self.store.saved_business_messages(), ())

    def test_business_message_containing_confirmation_word_does_not_start_authorization(self) -> None:
        response = self.service.confirm_start(self.open_id, "帮我确认昨天销量")

        self.assertEqual(response.kind, ResponseKind.FULL_GUIDE)
        self.assertEqual(self.store.user_count(), 0)


if __name__ == "__main__":
    unittest.main()
