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
    payload: str | None = None,
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
        confirm_deadline_at=NOW + timedelta(minutes=10),
        decided_at=None,
        decided_by_open_id=None,
        payload=payload,
    )


_GRANT_PAYLOAD = '{"company_id": "1011", "metric_name": "daily_active", "reason": "特批"}'

#: 收回的 payload 形状（卡 B）：比授权/抑制多一个 ``override_id``（confirm() 用它
#: 定位要收回的行）与 ``direction``（被收回那一行原本的方向，供渲染「含被收回的
#: 方向/公司/指标」）——见 ``adapters/postgres_pending_action.py`` 模块文档。
_REVOKE_GRANT_PAYLOAD = (
    '{"override_id": "lpo_01JGFJJZ008XSHEADGG8V74SPC", "direction": "grant",'
    ' "company_id": "1011", "metric_name": "daily_active", "reason": "离职交接"}'
)
_REVOKE_SUPPRESS_PAYLOAD = (
    '{"override_id": "lpo_01JGFJJZ008XSHEADGG8V74SPC", "direction": "suppress",'
    ' "company_id": "1011", "metric_name": "daily_active", "reason": "误抑制"}'
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


class LocalPermissionRenderingTests(unittest.TestCase):
    """本地权限授权/抑制（#319 S-P-1b）确认卡/终态卡/群通知的渲染扩展：含
    公司/指标/方向/理由，方向由 ``_ACTION_LABEL`` 隐式表达；不回显该用户的
    其余任何权限内容，只讲这一次动作涉及的单一键。"""

    def test_grant_card_mentions_grant_action_and_scope(self) -> None:
        pending = _pending(
            action_type=PendingActionType.LOCAL_PERMISSION_GRANT, payload=_GRANT_PAYLOAD
        )
        card = render_confirm_card(pending, target_label=TARGET_OPEN_ID)

        self.assertIn("授权", card.title)
        self.assertIn("1011", card.body)
        self.assertIn("daily_active", card.body)
        self.assertIn("特批", card.body)

    def test_suppress_card_mentions_suppress_action(self) -> None:
        pending = _pending(
            action_type=PendingActionType.LOCAL_PERMISSION_SUPPRESS, payload=_GRANT_PAYLOAD
        )
        card = render_confirm_card(pending, target_label=TARGET_OPEN_ID)

        self.assertIn("抑制", card.title)

    def test_suspend_card_has_no_scope_line(self) -> None:
        """非本地权限动作（``payload`` 恒为 ``None``）不产生任何范围行——
        既有 suspend/resume 卡片正文逐字节不变。"""

        pending = _pending(action_type=PendingActionType.SUSPEND_USER)
        card = render_confirm_card(pending, target_label=TARGET_OPEN_ID)

        self.assertNotIn("范围：", card.body)
        self.assertNotIn("原因：", card.body)

    def test_terminal_card_includes_scope_for_local_permission_action(self) -> None:
        pending = _pending(
            action_type=PendingActionType.LOCAL_PERMISSION_GRANT,
            status=PendingActionStatus.EXECUTED,
            payload=_GRANT_PAYLOAD,
        )
        card = render_terminal_card(pending, target_label=TARGET_OPEN_ID, outcome_text="已确认执行")

        self.assertIn("1011", card.body)
        self.assertIn("daily_active", card.body)

    def test_group_notice_includes_scope_suffix_for_local_permission_action(self) -> None:
        pending = _pending(
            action_type=PendingActionType.LOCAL_PERMISSION_GRANT,
            status=PendingActionStatus.EXECUTED,
            payload=_GRANT_PAYLOAD,
        )
        notice = render_group_notice(pending)

        self.assertIn("1011", notice)
        self.assertIn("daily_active", notice)
        self.assertIn("特批", notice)
        self.assertIn("授权", notice)

    def test_group_notice_has_no_scope_suffix_for_suspend(self) -> None:
        pending = _pending(action_type=PendingActionType.SUSPEND_USER, status=PendingActionStatus.EXECUTED)
        notice = render_group_notice(pending)

        self.assertNotIn("公司", notice)
        self.assertNotIn("指标", notice)

    def test_malformed_payload_does_not_crash_rendering(self) -> None:
        """脏数据兜底：``payload`` 不是合法 JSON 时渲染函数不得抛异常，只是
        跳过范围行——纯函数不允许因为一条历史脏数据整体崩溃。"""

        pending = _pending(
            action_type=PendingActionType.LOCAL_PERMISSION_GRANT, payload="not-json{"
        )
        card = render_confirm_card(pending, target_label=TARGET_OPEN_ID)

        self.assertNotIn("范围：", card.body)


class RevokeRenderingTests(unittest.TestCase):
    """本地权限收回（卡 B）确认卡/终态卡/群通知的渲染扩展：含被收回的方向/公司/
    指标，沿卡 A 形状——复用同一套 ``_permission_scope_block``/
    ``_permission_scope_suffix``，只多一行"方向"。"""

    def test_revoke_card_mentions_revoke_action_and_scope_and_direction(self) -> None:
        pending = _pending(
            action_type=PendingActionType.LOCAL_PERMISSION_REVOKE, payload=_REVOKE_GRANT_PAYLOAD
        )
        card = render_confirm_card(pending, target_label=TARGET_OPEN_ID)

        self.assertIn("收回", card.title)
        self.assertIn("1011", card.body)
        self.assertIn("daily_active", card.body)
        self.assertIn("离职交接", card.body)
        # 「含被收回的方向」：被收回的这一行原本是授权（grant），不是抑制。
        self.assertIn("授权", card.body)

    def test_revoke_card_mentions_suppress_direction_when_revoking_a_suppression(self) -> None:
        pending = _pending(
            action_type=PendingActionType.LOCAL_PERMISSION_REVOKE,
            payload=_REVOKE_SUPPRESS_PAYLOAD,
        )
        card = render_confirm_card(pending, target_label=TARGET_OPEN_ID)

        self.assertIn("抑制", card.body)

    def test_grant_and_suppress_cards_have_no_direction_line(self) -> None:
        """授权/抑制的 payload 从不携带 ``direction`` 键：既有卡片正文逐字节
        不变——抓"给 grant/suppress 也渲染了方向行"这类变异。"""

        pending = _pending(
            action_type=PendingActionType.LOCAL_PERMISSION_GRANT, payload=_GRANT_PAYLOAD
        )
        card = render_confirm_card(pending, target_label=TARGET_OPEN_ID)

        self.assertNotIn("方向：", card.body)

    def test_terminal_card_includes_scope_and_direction_for_revoke(self) -> None:
        pending = _pending(
            action_type=PendingActionType.LOCAL_PERMISSION_REVOKE,
            status=PendingActionStatus.EXECUTED,
            payload=_REVOKE_GRANT_PAYLOAD,
        )
        card = render_terminal_card(pending, target_label=TARGET_OPEN_ID, outcome_text="已确认执行")

        self.assertIn("1011", card.body)
        self.assertIn("daily_active", card.body)
        self.assertIn("授权", card.body)

    def test_group_notice_includes_scope_and_direction_suffix_for_revoke(self) -> None:
        pending = _pending(
            action_type=PendingActionType.LOCAL_PERMISSION_REVOKE,
            status=PendingActionStatus.EXECUTED,
            payload=_REVOKE_GRANT_PAYLOAD,
        )
        notice = render_group_notice(pending)

        self.assertIn("1011", notice)
        self.assertIn("daily_active", notice)
        self.assertIn("离职交接", notice)
        self.assertIn("收回", notice)
        self.assertIn("授权", notice)

    def test_malformed_revoke_payload_does_not_crash_rendering(self) -> None:
        pending = _pending(
            action_type=PendingActionType.LOCAL_PERMISSION_REVOKE, payload="not-json{"
        )
        card = render_confirm_card(pending, target_label=TARGET_OPEN_ID)

        self.assertNotIn("范围：", card.body)
        self.assertNotIn("方向：", card.body)


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
    任何执行入口，本函数的返回值本身也不含可执行内容——按钮/命令语法）。终态文案
    现在完全由 ``render_group_notice`` 内部按 ``pending.status``/``pending.reason``
    计算（不再接受调用方传入的 ``outcome_text``，见该函数文档），因此下面的用例
    通过构造带有对应 ``status`` 的 ``pending`` 来驱动出想要断言的文案。"""

    def test_notice_does_not_contain_target_open_id(self) -> None:
        pending = _pending(status=PendingActionStatus.EXECUTED)
        notice = render_group_notice(pending)
        self.assertNotIn(TARGET_OPEN_ID, notice)

    def test_notice_does_not_contain_initiator_open_id(self) -> None:
        pending = _pending(status=PendingActionStatus.EXECUTED)
        notice = render_group_notice(pending)
        self.assertNotIn(pending.initiated_by_open_id, notice)

    def test_notice_contains_internal_pending_action_id_and_outcome(self) -> None:
        pending = _pending(status=PendingActionStatus.EXECUTED)
        notice = render_group_notice(pending)
        self.assertIn(pending.id, notice)
        self.assertIn("已确认执行", notice)

    def test_notice_has_no_button_or_command_like_markers(self) -> None:
        pending = _pending(status=PendingActionStatus.EXECUTED)
        notice = render_group_notice(pending)
        for forbidden in ("/admin", "confirm", "cancel", "点击"):
            self.assertNotIn(forbidden, notice)

    def test_notice_covers_every_terminal_status(self) -> None:
        expected = {
            PendingActionStatus.EXECUTED: "已确认执行",
            PendingActionStatus.CANCELLED: "已取消",
            PendingActionStatus.EXPIRED: "已过期，未执行",
        }
        for status, expected_text in expected.items():
            with self.subTest(status=status):
                notice = render_group_notice(_pending(status=status))
                self.assertIn(expected_text, notice)


class RenderGroupNoticeReasonWhitelistTests(unittest.TestCase):
    """否定断言（外部审查交叉裁定，opus P3-8）：``FAILED`` 分支的 ``reason`` 主动
    注入敌意值时，不得原样进入群发正文——套用 ``core/daily_report.py`` 的同型形状
    白名单，不匹配就归入中性文案。"""

    def test_known_safe_reason_codes_pass_through_unchanged(self) -> None:
        for reason in ("role_revoked", "target_drifted", "card_send_failed"):
            with self.subTest(reason=reason):
                pending = _pending(status=PendingActionStatus.FAILED, reason=reason)
                notice = render_group_notice(pending)
                self.assertIn(reason, notice)

    def test_hostile_reason_injecting_markup_is_not_reflected_verbatim(self) -> None:
        hostile = "<script>steal()</script>"
        pending = _pending(status=PendingActionStatus.FAILED, reason=hostile)
        notice = render_group_notice(pending)
        self.assertNotIn(hostile, notice)
        self.assertNotIn("<script>", notice)

    def test_hostile_reason_shaped_like_an_open_id_is_not_reflected_verbatim(self) -> None:
        """形状白名单本身允许全小写字母数字下划线（与 ``daily_report.py`` 同一
        已知边界：真实 open_id 形态恰好落在这个盲区），但至少要挡住带大写/标点/
        CJK 等形状之外字符的注入——这条用例覆盖的是后者，不是声称挡住了前者。"""

        hostile = "ADMIN OVERRIDE: 已授权全部权限"
        pending = _pending(status=PendingActionStatus.FAILED, reason=hostile)
        notice = render_group_notice(pending)
        self.assertNotIn(hostile, notice)
        self.assertIn("other", notice)

    def test_missing_reason_falls_back_to_neutral_text(self) -> None:
        pending = _pending(status=PendingActionStatus.FAILED, reason=None)
        notice = render_group_notice(pending)
        self.assertIn("other", notice)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
