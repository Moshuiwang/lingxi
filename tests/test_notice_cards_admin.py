"""乙组管理卡与管理群通知的卡片化（#891 入口 #14–#17、#19–#21）。

卡片目录用测试夹具注入，不改正式目录。每个入口两面都钉住：

- 卡片键**未进**目录：渲染与发送和今天逐字相同（仍走 ``send_text`` / 原卡片正文）；
- 卡片键**进**目录：按「状态 / 结果 → 字段 → 下一步」出卡片，管理群卡无按钮、
  链接、回调，管理员自由文本转义，不展示授权有效期，「已记录」不写成已下发 /
  已生效 / 可用，入组不写成已开通，缺失值不填零；明确拒绝只回落一次纯文本，
  结果不明不补发；非本人与重复点击不产生新写入。
"""

from __future__ import annotations

import json
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest import mock

import lingxi.core.admin.notification as notification
from lingxi.adapters.feishu_group_message import FeishuGroupMessages
from lingxi.apps.gateway.admin_followups import GatewayFollowupHandlers
from lingxi.config import content as content_module
from lingxi.config.content import ContentCatalog, default_content_catalog
from lingxi.core.admin import management_card
from lingxi.core.admin.card_callback import AdminCardCallbackHandler
from lingxi.core.admin.management_status import PUBLISHING_STATUS_TEXT
from lingxi.core.admin.pending_action import PendingAction, PendingActionStatus, PendingActionType
from lingxi.core.admin.views import AdminUserStatusView
from lingxi.core.delivery import catalog_notice
from lingxi.core.delivery.notice_card import (
    NoticeCard,
    NoticeTone,
    assert_no_actionable_elements,
)
from lingxi.core.outreach.contact_reachability import (
    TODO_DEDUPE_PREFIX,
    ContactReachabilityRecorder,
    admin_todo_text,
)

NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
TARGET_OPEN_ID = "ou_target_user_masked"
CHAT_ID = "oc_admin_group_fake"
TARGET_LABEL = "某某人（masked@example.com）"

#: 交给整合路的卡片模板原样（``handoff-乙.md``）：夹具与清单同一份，避免两处漂移。
NOTICE_CARDS: dict[str, dict[str, object]] = {
    "notice.admin.confirm": {
        "title": "待本人确认 · {action}",
        "body": "请先核对对象、范围和期限；确认后才执行，取消不做任何变更。",
        "button_labels": [],
    },
    "notice.admin.terminal": {
        "title": "{action} · {outcome}",
        "body": "本卡已结束，不能再次操作。",
        "button_labels": [],
    },
    "notice.admin.group_notice": {
        "title": "管理操作 · {action} · {outcome}",
        "body": "本群消息只通知结果，不能在群内操作；需要处理请在私聊查询管理卡。",
        "button_labels": [],
    },
    "notice.admin.innertest_confirm": {
        "title": "待本人确认 · 加入内测资格",
        "body": "请核对名单后确认；取消不新增资格。",
        "button_labels": [],
    },
    "notice.admin.management_card": {
        "title": "用户权限管理卡",
        "body": "写操作都要在随后的确认卡上由本人确认后才执行。",
        "button_labels": [],
    },
    "notice.permission.management_correction_summary": {
        "title": "每日权限批处理 · 已补齐下发",
        "body": (
            "**已补齐**：{count} 条此前未完成的权限下发\n\n"
            "**下一步**\n无需处理；个别用户的权限状态可在私聊查询其管理卡。"
        ),
        "button_labels": [],
    },
    "notice.admin.contact_todo": {
        "title": "联系待办 · 需人工触达",
        "body": "请改用其它方式手动触达这位用户；本条是联系待办，不是故障告警。",
        "button_labels": [],
    },
}

#: 卡片化后任何管理卡 / 管理群卡都不得出现的说法。
_FORBIDDEN_CLAIMS = ("授权有效期",)
_RECORDED_OVERCLAIMS = ("权限已下发", "已生效", "可用")


def _catalog(*keys: str) -> ContentCatalog:
    """在正式目录上叠加若干通知卡片模板；不传键 = 全部叠加。"""
    base = default_content_catalog()
    chosen = keys or tuple(NOTICE_CARDS)
    extra = {key: content_module._parse_card_template(key, NOTICE_CARDS[key]) for key in chosen}
    return ContentCatalog(version=base.version, texts=base._texts, cards={**base._cards, **extra})


def _plain_catalog() -> ContentCatalog:
    """正式目录去掉本组全部通知卡片键：整合路把键写进正式目录之后仍代表「键未进目录」。"""
    base = default_content_catalog()
    cards = {key: card for key, card in base._cards.items() if key not in NOTICE_CARDS}
    return ContentCatalog(version=base.version, texts=base._texts, cards=cards)


def _pending(
    *,
    action_type: PendingActionType = PendingActionType.LOCAL_PERMISSION_GRANT,
    status: PendingActionStatus = PendingActionStatus.PENDING,
    payload: str | None = None,
    reason: str | None = None,
) -> PendingAction:
    if payload is None and action_type in {
        PendingActionType.LOCAL_PERMISSION_GRANT,
        PendingActionType.LOCAL_PERMISSION_SUPPRESS,
    }:
        payload = json.dumps(
            {"company_id": "1011", "metric_name": "daily_active", "reason": "特批"},
            ensure_ascii=False,
        )
    return PendingAction(
        id="pac_notice_test0000000000",
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


_HOSTILE_REASON = "看[这里](https://evil.example)<at id=all></at>**加粗**"


def _hostile_payload() -> str:
    return json.dumps(
        {"company_id": "1011", "metric_name": "daily_active", "reason": _HOSTILE_REASON},
        ensure_ascii=False,
    )


def _payload_text(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 假实现


class _TextOnlyGroup:
    def __init__(self) -> None:
        self.texts: list[dict] = []

    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None:
        self.texts.append({"chat_id": chat_id, "text": text, "dedupe_key": dedupe_key})


class _NoticeGroup(_TextOnlyGroup):
    def __init__(self, *, raises: Exception | None = None) -> None:
        super().__init__()
        self.notices: list[dict] = []
        self._raises = raises

    def send_notice(self, *, chat_id: str, card: NoticeCard, dedupe_key: str) -> None:
        self.notices.append({"chat_id": chat_id, "card": card, "dedupe_key": dedupe_key})
        if self._raises is not None:
            raise self._raises


class _DisplayNames:
    def user_label(self, *, open_id: str) -> str:
        return TARGET_LABEL

    def company_label(self, *, company_id: str) -> str:
        return f"某某公司（{company_id}）"

    def metric_label(self, *, metric_id: str) -> str:
        return f"某某指标（{metric_id}）"


@dataclass(frozen=True)
class _Decision:
    ok: bool
    message: str
    terminal_status: PendingActionStatus | None


@dataclass(frozen=True)
class _Outcome:
    decision: _Decision
    pending: PendingAction | None


class _PendingActions:
    def __init__(self, outcome: _Outcome) -> None:
        self._outcome = outcome
        self.sequences = 0

    def confirm(self, *, pending_action_id: str, clicker_open_id: str):
        return self._outcome

    def cancel(self, *, pending_action_id: str, clicker_open_id: str):
        return self._outcome

    def next_card_sequence(self, *, pending_action_id: str) -> int:
        self.sequences += 1
        return self.sequences

    def get(self, *, pending_action_id: str):
        return self._outcome.pending


class _CardTransport:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    def update(self, *, card_id: str, sequence: int, card) -> None:
        self.updates.append({"card_id": card_id, "card": card})


class _Audit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, fields))


def _handler(outcome: _Outcome, *, notifier, content_catalog=None):
    audit = _Audit()
    cards = _CardTransport()
    kwargs = {} if content_catalog is None else {"content_catalog": content_catalog}
    handler = AdminCardCallbackHandler(
        pending_actions=_PendingActions(outcome),
        confirm_cards=cards,
        group_notifier=notifier,
        group_chat_id=CHAT_ID,
        audit=audit,
        display_names=_DisplayNames(),
        **kwargs,
    )
    return handler, audit, cards


def _executed_outcome(pending: PendingAction) -> _Outcome:
    return _Outcome(
        decision=_Decision(ok=True, message="已确认执行。", terminal_status=pending.status),
        pending=pending,
    )


class _FakeFeishu:
    """按消息类型脚本化飞书响应；记录每一次真实外发请求。"""

    def __init__(self, *, card: object) -> None:
        self.calls: list[dict] = []
        self._card = card

    def __call__(self, method, url, *, body=None, token=None, **kwargs):
        if "tenant_access_token" in url:
            return {"code": 0, "tenant_access_token": "t-fake"}
        self.calls.append(body)
        if body["msg_type"] == "interactive":
            if isinstance(self._card, BaseException):
                raise self._card
            return self._card
        return {"code": 0, "data": {"message_id": "om-text"}}

    def sent(self, msg_type: str) -> list[dict]:
        return [call for call in self.calls if call["msg_type"] == msg_type]


def _real_group(fake: _FakeFeishu) -> FeishuGroupMessages:
    return FeishuGroupMessages(
        base_url="https://open.feishu.cn/open-apis",
        app_id="cli_fake",
        app_secret="fake-secret",
        transport=fake,
    )


# ---------------------------------------------------------------------------
# #14 管理写操作确认卡


class ConfirmCardTests(unittest.TestCase):
    def test_without_card_key_render_is_byte_identical(self) -> None:
        card = notification.render_confirm_card(
            _pending(), target_label=TARGET_LABEL, content_catalog=_plain_catalog()
        )
        self.assertEqual(card.title, "待确认：补充授权用户")
        self.assertIsNone(card.tone)
        self.assertEqual(
            card.body,
            f"动作：补充授权用户\n目标：{TARGET_LABEL}\n"
            "范围：公司 1011 · 指标 daily_active\n原因：特批\n"
            "影响：该用户将获得下方指定公司×指标的问数权限（补充授权，独立于银河翻译结果，"
            "不影响其余已有权限）。\n有效期：10 分钟内有效，过期后需重新查询并发起。",
        )
        payload = notification.render_card_payload(card)
        self.assertNotIn("header", payload)
        self.assertEqual(
            payload["body"]["elements"][0]["content"], f"**{card.title}**\n\n{card.body}"
        )

    def test_with_card_key_splits_object_scope_impact_and_real_confirm_deadline(self) -> None:
        card = notification.render_confirm_card(
            _pending(), target_label=TARGET_LABEL, content_catalog=_catalog()
        )
        payload = notification.render_card_payload(card)
        self.assertEqual(payload["header"]["title"]["content"], "待本人确认 · 补充授权")
        self.assertEqual(payload["header"]["template"], "orange")
        for field in ("**操作对象**", "**操作范围**", "**影响**", "**本次确认有效期**"):
            self.assertIn(field, card.body)
        self.assertIn("10 分钟", card.body)
        self.assertNotIn("下方", card.body)
        self.assertEqual(len(card.buttons), 2)
        for claim in _FORBIDDEN_CLAIMS:
            self.assertNotIn(claim, _payload_text(payload))

    def test_admin_free_text_is_escaped(self) -> None:
        card = notification.render_confirm_card(
            _pending(payload=_hostile_payload()),
            target_label=TARGET_LABEL,
            content_catalog=_catalog(),
        )
        self.assertNotIn("https://", card.body)
        self.assertNotIn("<at", card.body)
        self.assertNotIn("](", card.body)

    def test_scope_unavailable_is_explicit_not_blank(self) -> None:
        card = notification.render_confirm_card(
            _pending(payload="{broken"), target_label=TARGET_LABEL, content_catalog=_catalog()
        )
        self.assertIn("范围信息不可用", card.body)


# ---------------------------------------------------------------------------
# #15 确认卡终态


class TerminalCardTests(unittest.TestCase):
    def _render(self, pending: PendingAction, outcome_text: str, catalog=None):
        kwargs = {} if catalog is None else {"content_catalog": catalog}
        return notification.render_terminal_card(
            pending, target_label=TARGET_LABEL, outcome_text=outcome_text, **kwargs
        )

    def test_without_card_key_render_is_byte_identical(self) -> None:
        pending = _pending(status=PendingActionStatus.EXECUTED)
        card = self._render(pending, "操作已记录，权限正在下发", _plain_catalog())
        self.assertEqual(card.title, "补充授权用户 · 已结束")
        self.assertEqual(
            card.body,
            f"目标：{TARGET_LABEL}\n范围：公司 1011 · 指标 daily_active\n原因：特批\n"
            "结果：操作已记录，权限正在下发",
        )
        self.assertIsNone(card.tone)
        self.assertEqual(card.buttons, ())

    def test_recorded_grant_is_not_claimed_as_published(self) -> None:
        pending = _pending(status=PendingActionStatus.EXECUTED)
        card = self._render(pending, "操作已记录，权限正在下发", _catalog())
        payload = notification.render_card_payload(card)
        self.assertEqual(card.buttons, ())
        self.assertEqual(payload["header"]["template"], "blue")
        self.assertIn("权限正在下发", payload["header"]["title"]["content"])
        rendered = _payload_text(payload)
        for claim in _RECORDED_OVERCLAIMS:
            self.assertNotIn(claim, rendered)
        for key in ('"button"', '"behaviors"', '"callback"', '"url"'):
            self.assertNotIn(key, rendered)

    def test_suspend_and_resume_use_account_result_not_permission_dispatch(self) -> None:
        for action_type, expected in (
            (PendingActionType.SUSPEND_USER, "账号已停用"),
            (PendingActionType.RESUME_USER, "账号已恢复"),
        ):
            pending = _pending(action_type=action_type, status=PendingActionStatus.EXECUTED)
            card = self._render(pending, "操作已记录，权限正在下发", _catalog())
            self.assertIn(expected, card.title)
            self.assertIn(expected, card.body)
            self.assertNotIn("权限正在下发", card.title + card.body)

    def test_cancel_expire_fail_tones_are_distinct(self) -> None:
        tones = {}
        for status, text in (
            (PendingActionStatus.CANCELLED, "已取消"),
            (PendingActionStatus.EXPIRED, "已过期，未执行"),
            (PendingActionStatus.FAILED, "未执行（内部原因）"),
        ):
            card = self._render(_pending(status=status), text, _catalog())
            tones[status] = notification.render_card_payload(card)["header"]["template"]
            self.assertIn(text, card.body)
        self.assertEqual(tones[PendingActionStatus.FAILED], "red")
        self.assertEqual(tones[PendingActionStatus.CANCELLED], "orange")


# ---------------------------------------------------------------------------
# #16 管理群终态广播


class GroupNoticeTests(unittest.TestCase):
    def test_without_card_key_callback_sends_identical_text(self) -> None:
        pending = _pending(status=PendingActionStatus.EXECUTED)
        notifier = _NoticeGroup()
        handler, _, _ = _handler(
            _executed_outcome(pending), notifier=notifier, content_catalog=_plain_catalog()
        )
        handler.handle(
            operator_open_id="ou_admin",
            pending_action_id=pending.id,
            decision="confirm",
            trace_id="t",
        )
        expected = notification.render_group_notice(
            pending,
            target_label=TARGET_LABEL,
            company_label="某某公司（1011）",
            metric_label="某某指标（daily_active）",
        )
        self.assertEqual(notifier.notices, [])
        self.assertEqual(
            notifier.texts, [{"chat_id": CHAT_ID, "text": expected, "dedupe_key": pending.id}]
        )

    def test_with_card_key_group_card_has_no_action_and_equivalent_fallback(self) -> None:
        pending = _pending(status=PendingActionStatus.EXECUTED, payload=_hostile_payload())
        notifier = _NoticeGroup()
        handler, _, _ = _handler(
            _executed_outcome(pending), notifier=notifier, content_catalog=_catalog()
        )
        handler.handle(
            operator_open_id="ou_admin",
            pending_action_id=pending.id,
            decision="confirm",
            trace_id="t",
        )
        self.assertEqual(notifier.texts, [])
        self.assertEqual(len(notifier.notices), 1)
        sent = notifier.notices[0]
        self.assertEqual(sent["dedupe_key"], pending.id)
        card: NoticeCard = sent["card"]
        payload = card.to_payload()
        assert_no_actionable_elements(payload)
        rendered = _payload_text(payload)
        for key in ('"button"', '"behaviors"', '"callback"', '"url"', "https://", "<at"):
            self.assertNotIn(key, rendered)
        for claim in (*_FORBIDDEN_CLAIMS, *_RECORDED_OVERCLAIMS):
            self.assertNotIn(claim, rendered)
        self.assertEqual(payload["header"]["template"], "blue")
        self.assertEqual(
            card.fallback_text,
            notification.render_group_notice(
                pending,
                target_label=TARGET_LABEL,
                company_label="某某公司（1011）",
                metric_label="某某指标（daily_active）",
            ),
        )

    def test_followup_consumer_path_uses_same_card(self) -> None:
        pending = _pending(status=PendingActionStatus.CANCELLED)
        notifier = _NoticeGroup()
        handler, _, _ = _handler(
            _executed_outcome(pending), notifier=notifier, content_catalog=_catalog()
        )
        followups = GatewayFollowupHandlers(
            store=None, pending_actions=None, callback=handler, recompute=None, cards=None
        )
        # 持久后处理路径读进程默认目录（与回调同一份），这里替换它。
        with mock.patch.object(notification, "default_content_catalog", return_value=_catalog()):
            result = followups._notify(None, pending)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(notifier.texts, [])
        self.assertEqual(len(notifier.notices), 1)
        self.assertEqual(notifier.notices[0]["card"].to_payload()["header"]["template"], "orange")

    def test_followup_text_only_notifier_keeps_text(self) -> None:
        pending = _pending(status=PendingActionStatus.CANCELLED)
        notifier = _TextOnlyGroup()
        handler, _, _ = _handler(
            _executed_outcome(pending), notifier=notifier, content_catalog=_catalog()
        )
        followups = GatewayFollowupHandlers(
            store=None, pending_actions=None, callback=handler, recompute=None, cards=None
        )
        with mock.patch.object(notification, "default_content_catalog", return_value=_catalog()):
            followups._notify(None, pending)
        self.assertEqual(len(notifier.texts), 1)
        self.assertIn("已取消", notifier.texts[0]["text"])

    def test_rejected_card_falls_back_to_text_exactly_once(self) -> None:
        pending = _pending(status=PendingActionStatus.EXECUTED)
        fake = _FakeFeishu(card={"code": 230099, "msg": "card rejected"})
        handler, audit, _ = _handler(
            _executed_outcome(pending), notifier=_real_group(fake), content_catalog=_catalog()
        )
        handler.handle(
            operator_open_id="ou_admin",
            pending_action_id=pending.id,
            decision="confirm",
            trace_id="t",
        )
        self.assertEqual(len(fake.sent("interactive")), 1)
        self.assertEqual(len(fake.sent("text")), 1)
        self.assertNotIn(
            "admin.card_callback.group_notify_failed", [name for name, _ in audit.records]
        )

    def test_unknown_result_does_not_resend_text(self) -> None:
        pending = _pending(status=PendingActionStatus.EXECUTED)
        fake = _FakeFeishu(card=TimeoutError("timeout"))
        handler, audit, _ = _handler(
            _executed_outcome(pending), notifier=_real_group(fake), content_catalog=_catalog()
        )
        handler.handle(
            operator_open_id="ou_admin",
            pending_action_id=pending.id,
            decision="confirm",
            trace_id="t",
        )
        self.assertEqual(len(fake.sent("interactive")), 1)
        self.assertEqual(fake.sent("text"), [])
        self.assertIn(
            "admin.card_callback.group_notify_failed", [name for name, _ in audit.records]
        )

    def test_non_initiator_and_replayed_clicks_cause_no_new_writes(self) -> None:
        still_pending = _pending(status=PendingActionStatus.PENDING)
        replay = _pending(status=PendingActionStatus.EXECUTED)
        for outcome in (
            _Outcome(_Decision(False, "只有发起人本人可以确认。", None), still_pending),
            _Outcome(_Decision(True, "该操作已执行。", None), replay),
        ):
            notifier = _NoticeGroup()
            handler, _, cards = _handler(outcome, notifier=notifier, content_catalog=_catalog())
            handler.handle(
                operator_open_id="ou_other",
                pending_action_id=outcome.pending.id,
                decision="confirm",
                trace_id="t",
            )
            self.assertEqual(notifier.notices, [])
            self.assertEqual(notifier.texts, [])
        self.assertEqual(cards.updates[0]["card"].buttons, ())


# ---------------------------------------------------------------------------
# #17 管理卡（表单与状态行）


class _MetricCatalog:
    def companies(self):
        return ("1011",)

    def metrics(self):
        return ("daily_active",)

    def positions(self):
        return ("销售经理",)


class _ManagementNames(_DisplayNames):
    def company_labels(self, *, company_ids):
        return {company_id: f"某某公司（{company_id}）" for company_id in company_ids}

    def metric_labels(self, *, metric_ids):
        return {metric_id: f"某某指标（{metric_id}）" for metric_id in metric_ids}


def _status(account_state: str = "enabled") -> AdminUserStatusView:
    return AdminUserStatusView(
        identifier=TARGET_OPEN_ID,
        provisioning_state="active",
        account_state=account_state,
        permission_version=1,
        updated_at="2026-09-27 12:00 UTC",
    )


class ManagementCardTests(unittest.TestCase):
    """管理卡签名已到参数上限，内容目录经进程默认目录注入（测试里替换它）。"""

    def _render(self, **kwargs):
        catalog = kwargs.pop("content_catalog", _plain_catalog())
        with mock.patch.object(management_card, "default_content_catalog", return_value=catalog):
            return management_card.render_management_card(
                _status(kwargs.pop("account_state", "enabled")),
                display_identifier="someone@example.com",
                catalog=_MetricCatalog(),
                display_names=_ManagementNames(),
                **kwargs,
            )

    def test_without_card_key_render_is_byte_identical(self) -> None:
        legacy = self._render(dispatch_status="已生效")
        self.assertNotIn("header", legacy)
        self.assertEqual(
            legacy["body"]["elements"][0]["content"],
            "**用户权限管理卡** · 某某人（masked@example.com） · 标识 someone@example.com",
        )
        self.assertIn("当前状态：已生效", _payload_text(legacy))
        self.assertNotIn("权限已下发", _payload_text(legacy))

    def test_with_card_key_has_header_object_field_and_published_wording(self) -> None:
        card = self._render(dispatch_status="已生效", content_catalog=_catalog())
        self.assertEqual(card["header"]["title"]["content"], "用户权限管理卡")
        self.assertEqual(card["header"]["template"], "green")
        rendered = _payload_text(card)
        self.assertIn("**操作对象**", rendered)
        self.assertIn("当前状态：权限已下发", rendered)
        self.assertNotIn("当前状态：已生效", rendered)
        self.assertNotIn("授权有效期", rendered)

    def test_submitted_state_stays_processing_not_published(self) -> None:
        card = self._render(
            submitted=True, dispatch_status=PUBLISHING_STATUS_TEXT, content_catalog=_catalog()
        )
        self.assertEqual(card["header"]["template"], "blue")
        rendered = _payload_text(card)
        self.assertIn(PUBLISHING_STATUS_TEXT, rendered)
        self.assertNotIn("权限已下发", rendered)

    def test_disabled_account_line_is_attention_not_dispatching(self) -> None:
        not_enabled = (
            default_content_catalog().text("permission.management_account_not_enabled").text
        )
        card = self._render(
            account_state="suspended", dispatch_status=not_enabled, content_catalog=_catalog()
        )
        self.assertEqual(card["header"]["template"], "orange")
        self.assertNotIn("正在下发", _payload_text(card))


# ---------------------------------------------------------------------------
# #19 内测入组确认卡


class InnertestConfirmCardTests(unittest.TestCase):
    people = (("a_b@example.com", "P001"), ("c@example.com", None))

    def test_without_card_key_body_is_byte_identical(self) -> None:
        card = notification.render_innertest_confirm_card(
            "pac_x", self.people, content_catalog=_plain_catalog()
        )
        self.assertEqual(card.title, "确认加入内测资格")
        self.assertEqual(
            card.body,
            "加入内测资格，不授业务权限；资格与开通结果逐人查询。\n本批 2 项：\n"
            "a_b@example.com（P001）\nc@example.com（待执行阶段解析）\n"
            "待解析项仅在执行时确认唯一在职身份后加入资格；未知或冲突不加入。"
            "\n请本人在10分钟内确认；取消不新增资格。",
        )
        self.assertIsNone(card.tone)
        self.assertEqual(
            [button.value for button in card.buttons],
            [
                {"pending_action_id": "pac_x", "decision": "confirm"},
                {"pending_action_id": "pac_x", "decision": "cancel"},
            ],
        )

    def test_with_card_key_joining_is_not_claimed_as_provisioned(self) -> None:
        card = notification.render_innertest_confirm_card(
            "pac_x", self.people, content_catalog=_catalog()
        )
        payload = notification.render_card_payload(card)
        self.assertEqual(payload["header"]["template"], "orange")
        self.assertIn("**本次确认有效期**", card.body)
        for claim in ("已开通", "开通完成", "可以开始使用", "授权有效期"):
            self.assertNotIn(claim, card.body)
        self.assertEqual(len(card.buttons), 2)


# ---------------------------------------------------------------------------
# #20 每日权限补齐汇总


class _Store:
    def __init__(self) -> None:
        self.marked: list[tuple[str, ...]] = []

    def mark_daily_corrections_reported(self, *, message_ids) -> None:
        self.marked.append(tuple(message_ids))


class CorrectionSummaryTests(unittest.TestCase):
    """有原文本键，复用用户侧「文本键配卡片键」：色调登记在 ``NOTICE_TONES`` 才配卡。"""

    def _send(self, sender, catalog, *, tone_registered=True):
        from lingxi.apps.scheduler.assembly import _send_management_correction_summary

        store, audit = _Store(), _Audit()
        config = mock.Mock(admin_group_chat_id=CHAT_ID)
        tones = (
            {notification.CORRECTION_SUMMARY_TEXT_KEY: NoticeTone.DONE} if tone_registered else {}
        )
        with (
            mock.patch.object(notification, "default_content_catalog", return_value=catalog),
            # 色调表在模块导入时合并成 ``_ALL_TONES``，演练「已登记」只能替换合并后的表。
            mock.patch.dict(catalog_notice._ALL_TONES, tones),
        ):
            _send_management_correction_summary(
                config=config, audit=audit, sender=sender, store=store, message_ids=("m1", "m2")
            )
        return store, audit

    def test_card_key_without_registered_tone_keeps_text(self) -> None:
        sender = _NoticeGroup()
        self._send(sender, _catalog(), tone_registered=False)
        self.assertEqual(sender.notices, [])
        self.assertEqual(len(sender.texts), 1)

    def test_without_card_key_sends_identical_text(self) -> None:
        sender = _NoticeGroup()
        store, _ = self._send(sender, _plain_catalog())
        self.assertEqual(sender.notices, [])
        self.assertEqual(
            [sent["text"] for sent in sender.texts],
            ["每日权限批处理已补齐 2 条此前未完成的权限下发。"],
        )
        self.assertEqual(store.marked, [("m1", "m2")])

    def test_with_card_key_sends_card_with_same_dedupe_key(self) -> None:
        sender = _NoticeGroup()
        store, _ = self._send(sender, _catalog())
        self.assertEqual(sender.texts, [])
        card: NoticeCard = sender.notices[0]["card"]
        assert_no_actionable_elements(card.to_payload())
        self.assertEqual(card.to_payload()["header"]["template"], "green")
        self.assertIn("**已补齐**：2 条", _payload_text(card.to_payload()))
        self.assertEqual(card.fallback_text, "每日权限批处理已补齐 2 条此前未完成的权限下发。")
        self.assertTrue(sender.notices[0]["dedupe_key"].startswith("management-correction:"))
        self.assertEqual(store.marked, [("m1", "m2")])

    def test_unknown_result_keeps_watermark(self) -> None:
        sender = _NoticeGroup(raises=TimeoutError("timeout"))
        store, audit = self._send(sender, _catalog())
        self.assertEqual(store.marked, [])
        self.assertIn(
            "admin.management_correction_summary_failed", [name for name, _ in audit.records]
        )


# ---------------------------------------------------------------------------
# #21 联系不可达管理员待办


class _ContactStore:
    def __init__(self, email: str | None) -> None:
        self._email = email

    def record_contact_unavailable(self, *, open_id, when, code) -> bool:
        return True

    def record_contact_reachable(self, *, open_id, when) -> bool:
        return True

    def get_by_open_id(self, open_id):
        return None if self._email is None else mock.Mock(email=self._email)


class ContactTodoTests(unittest.TestCase):
    def _recorder(self, notifier, *, email, catalog):
        return ContactReachabilityRecorder(
            store=_ContactStore(email),
            notifier=notifier,
            chat_id=CHAT_ID,
            clock=lambda: NOW,
            content_catalog=catalog,
        )

    def test_without_card_key_sends_identical_text(self) -> None:
        notifier = _NoticeGroup()
        self._recorder(notifier, email="x@example.com", catalog=_plain_catalog())(
            "ou_unreach", False, "230013"
        )
        self.assertEqual(notifier.notices, [])
        self.assertEqual(
            notifier.texts,
            [
                {
                    "chat_id": CHAT_ID,
                    "text": admin_todo_text(
                        open_id="ou_unreach", email="x@example.com", error_code="230013"
                    ),
                    "dedupe_key": TODO_DEDUPE_PREFIX + "ou_unreach",
                }
            ],
        )

    def test_with_card_key_missing_values_are_explicit_not_blank(self) -> None:
        notifier = _NoticeGroup()
        self._recorder(notifier, email=None, catalog=_catalog())("ou_unreach", False, None)
        self.assertEqual(notifier.texts, [])
        sent = notifier.notices[0]
        self.assertEqual(sent["dedupe_key"], TODO_DEDUPE_PREFIX + "ou_unreach")
        card: NoticeCard = sent["card"]
        assert_no_actionable_elements(card.to_payload())
        rendered = _payload_text(card.to_payload())
        self.assertIn("邮箱未知", rendered)
        self.assertIn("unknown", rendered)
        self.assertIn("不是故障告警", rendered)
        self.assertEqual(card.to_payload()["header"]["template"], "orange")
        self.assertEqual(
            card.fallback_text,
            admin_todo_text(open_id="ou_unreach", email=None, error_code="unknown"),
        )

    def test_log_only_notifier_without_send_notice_keeps_text(self) -> None:
        notifier = _TextOnlyGroup()
        self._recorder(notifier, email="x@example.com", catalog=_catalog())(
            "ou_unreach", False, "230013"
        )
        self.assertEqual(len(notifier.texts), 1)


if __name__ == "__main__":
    unittest.main()
