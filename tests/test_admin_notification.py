"""``core/admin/notification.py`` 的卡片与群通知渲染断言（Issue #96 S-M-02）。

只测纯函数：卡片/文案内容形状、按钮回传值、脱敏边界（管理群通知不得含 open_id
明文）。真实 CardKit 出站字段见 ``tests/test_feishu_admin_card_payload.py``。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from lingxi.core.admin.notification import (
    DECISION_CANCEL,
    DECISION_CONFIRM,
    render_confirm_card,
    render_group_notice,
    render_terminal_card,
)
from lingxi.core.admin.pending_action import PendingAction, PendingActionStatus, PendingActionType

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)
TARGET_OPEN_ID = "ou_target_user_masked"


def _pending(
    *,
    action_type: PendingActionType = PendingActionType.SUSPEND_USER,
    status: PendingActionStatus = PendingActionStatus.PENDING,
    reason: str | None = None,
) -> PendingAction:
    return PendingAction(
        id="pac_notif_test000000000000",
        action_type=action_type,
        target_open_id=TARGET_OPEN_ID,
        target_state_snapshot="enabled",
        initiated_by_open_id="ou_admin",
        status=status,
        card_delivered=True,
        card_id="cardkit_id_1",
        reason=reason,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        decided_at=None,
        decided_by_open_id=None,
    )


class RenderConfirmCardTests(unittest.TestCase):
    def test_card_has_two_buttons_with_correct_bound_values(self) -> None:
        pending = _pending()
        card = render_confirm_card(pending, target_label=TARGET_OPEN_ID)

        self.assertEqual(len(card.buttons), 2)
        decisions = {button.value["decision"] for button in card.buttons}
        self.assertEqual(decisions, {DECISION_CONFIRM, DECISION_CANCEL})
        for button in card.buttons:
            self.assertEqual(button.value["pending_action_id"], pending.id)
        self.assertFalse(card.is_terminal)

    def test_suspend_card_mentions_suspend_action(self) -> None:
        pending = _pending(action_type=PendingActionType.SUSPEND_USER)
        card = render_confirm_card(pending, target_label=TARGET_OPEN_ID)
        self.assertIn("停用", card.title)

    def test_resume_card_mentions_resume_action(self) -> None:
        pending = _pending(action_type=PendingActionType.RESUME_USER)
        card = render_confirm_card(pending, target_label=TARGET_OPEN_ID)
        self.assertIn("恢复", card.title)

    def test_card_echoes_only_the_target_label_the_admin_already_typed(self) -> None:
        """脱敏最小必要：卡片只回显管理员自己输入的标识，不引入姓名等新披露。"""

        pending = _pending()
        card = render_confirm_card(pending, target_label=TARGET_OPEN_ID)
        self.assertIn(TARGET_OPEN_ID, card.body)

    def test_card_states_the_ttl(self) -> None:
        pending = _pending()
        card = render_confirm_card(pending, target_label=TARGET_OPEN_ID)
        self.assertIn("10", card.body)
        self.assertIn("分钟", card.body)

    def test_card_does_not_contain_any_credential_like_content(self) -> None:
        pending = _pending()
        card = render_confirm_card(pending, target_label=TARGET_OPEN_ID)
        lowered = (card.title + card.body).lower()
        for forbidden in ("token", "secret", "password", "凭据"):
            self.assertNotIn(forbidden, lowered)


class RenderTerminalCardTests(unittest.TestCase):
    def test_terminal_card_has_no_buttons(self) -> None:
        pending = _pending(status=PendingActionStatus.EXECUTED)
        card = render_terminal_card(pending, target_label=TARGET_OPEN_ID, outcome_text="已确认执行")
        self.assertEqual(card.buttons, ())
        self.assertTrue(card.is_terminal)

    def test_terminal_card_shows_the_outcome_text(self) -> None:
        pending = _pending(status=PendingActionStatus.CANCELLED)
        card = render_terminal_card(pending, target_label=TARGET_OPEN_ID, outcome_text="已取消")
        self.assertIn("已取消", card.body)


class RenderGroupNoticeTests(unittest.TestCase):
    """否定断言：管理群通知不得含 open_id 明文（`V-管理-11` 同一要求：群里不提供
    任何执行入口，本函数的返回值本身也不含可执行内容——按钮/命令语法）。"""

    def test_notice_does_not_contain_target_open_id(self) -> None:
        pending = _pending()
        notice = render_group_notice(pending, outcome_text="已确认执行")
        self.assertNotIn(TARGET_OPEN_ID, notice)

    def test_notice_does_not_contain_initiator_open_id(self) -> None:
        pending = _pending()
        notice = render_group_notice(pending, outcome_text="已确认执行")
        self.assertNotIn(pending.initiated_by_open_id, notice)

    def test_notice_contains_internal_pending_action_id_and_outcome(self) -> None:
        pending = _pending()
        notice = render_group_notice(pending, outcome_text="已确认执行")
        self.assertIn(pending.id, notice)
        self.assertIn("已确认执行", notice)

    def test_notice_has_no_button_or_command_like_markers(self) -> None:
        pending = _pending()
        notice = render_group_notice(pending, outcome_text="已确认执行")
        for forbidden in ("/admin", "confirm", "cancel", "点击"):
            self.assertNotIn(forbidden, notice)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
