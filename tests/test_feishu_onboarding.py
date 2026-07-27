"""飞书私聊入口的用户可见断言。"""

from __future__ import annotations

import unittest

from lingxi.adapters.feishu_onboarding import FeishuOnboardingController, IncomingPrivateMessage
from lingxi.core.identity.onboarding import InMemoryOnboardingStore, OnboardingService


class FakeCardSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    def send_card(self, chat_id: str, card: dict) -> None:
        self.sent.append((chat_id, card))


class FeishuOnboardingControllerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryOnboardingStore()
        self.sender = FakeCardSender()
        self.controller = FeishuOnboardingController(OnboardingService(self.store), self.sender)

    def test_private_business_message_only_receives_full_guide(self) -> None:
        self.controller.receive_private_message(IncomingPrivateMessage("oc_test", "ou_test", "帮我查一下销量"))

        chat_id, card = self.sender.sent[-1]
        self.assertEqual(chat_id, "oc_test")
        self.assertEqual(card["header"]["title"]["content"], "欢迎使用灵犀")
        self.assertEqual(self.store.user_count(), 0)
        self.assertEqual(self.store.saved_business_messages(), ())

    def test_decline_replaces_full_guide_with_short_restart_entry(self) -> None:
        card = self.controller.receive_card_action("oc_test", "ou_test", "decline")

        self.assertEqual(card["header"]["title"]["content"], "灵犀")
        self.assertEqual(self.store.user_count(), 0)
        self.assertEqual(len(card["body"]["elements"][1]["columns"]), 1)

    def test_start_requires_authorization_without_creating_user(self) -> None:
        card = self.controller.receive_card_action("oc_test", "ou_test", "start")

        self.assertEqual(card["header"]["title"]["content"], "准备开通")
        self.assertEqual(self.store.user_count(), 0)

    def test_group_message_is_not_a_private_onboarding_path(self) -> None:
        self.controller.receive_private_message(IncomingPrivateMessage("oc_group", "ou_test", "开始使用", chat_type="group"))

        self.assertEqual(self.sender.sent, [])
        self.assertEqual(self.store.user_count(), 0)


if __name__ == "__main__":
    unittest.main()
