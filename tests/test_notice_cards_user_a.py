"""#891 甲-a：用户侧状态通知卡片化（开通、忙碌 / 会话 / 命令、记忆、权限范围变化）。

卡片键 ``notice.<原文本键>`` 由整合路写进内容目录；本模块的用例用测试夹具把
:data:`PROPOSED_CARDS` 注入一份目录副本，不改正式目录。钉住的事：

- 卡片键未进目录时，四个出口（gateway 同步回复、开通终态私聊、权限变化通知、
  同步回复适配器）发出的仍是原文本，逐字相同，且不调用卡片发送；
- 卡片键进目录后改发卡片，等价纯文本就是原文本；飞书明确拒绝才回落纯文本一次，
  结果不明不补发；
- 否定断言：未受理≠已排队、入组≠已开通、缺失不填零、无授权有效期、卡片无按钮 /
  链接 / 回调、动态值转义后不能成为链接；
- :data:`PROPOSED_CARDS` 与 :data:`PROPOSED_TEXTS` 是交给整合路的清单正文，本模块
  逐条校验它们能通过内容目录的装载校验、占位集合与原文本键一致。
"""

from __future__ import annotations

import sys
import types
import unittest
import unittest.mock
from datetime import UTC, datetime
from types import SimpleNamespace

from gateway_fakes import (
    CallLog,
    FakeAudit,
    FakeConversation,
    FakeReactions,
    FakeReplies,
    FakeState,
    FakeStore,
    FakeTask,
    provisioned_user,
)

from lingxi.config import content as content_module
from lingxi.config.content import ContentCatalog, default_content_catalog, text_placeholders
from lingxi.core.conversation import EventPipeline, GatewayTexts, InboundMessage
from lingxi.core.delivery.catalog_notice import (
    NOTICE_TONES,
    build_notice_card,
    notice_card_key,
    recover_values,
    reply_notice_card,
    send_catalog_notice,
)
from lingxi.core.delivery.notice_card import NoticeCard, assert_no_actionable_elements
from lingxi.core.permission.notification import PermissionNoticeDispatcher, render_scope_notice
from lingxi.core.user_memory import UserMemoryEntry

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
NEXT = "**下一步**"
_OFFICIAL = default_content_catalog()
#: 「卡片键未进目录」的目录：正式目录已登记 ``notice.*``，这里剔除后代表撤键退回原文本。
_BASE = ContentCatalog(
    version=_OFFICIAL.version,
    texts=_OFFICIAL._texts,  # noqa: SLF001
    cards={k: v for k, v in _OFFICIAL._cards.items() if not k.startswith("notice.")},  # noqa: SLF001
)
#: 色调登记在 ``NOTICE_TONES`` 但不属于本组清单的键（乙组管理群补齐汇总）。
_OTHER_GROUP_TONE_KEYS = frozenset({"permission.management_correction_summary"})
#: 内测未开放提示里的联系方式原样取自正式目录，不在代码里另抄一份。
_INNERTEST_CONTACT = _BASE.text("onboarding.innertest_not_open").text.split("。", 1)[1]

#: 交给整合路的卡片清单：卡片键 notice.<原文本键> → (标题, 正文模板)。色调在代码里
#: （``NOTICE_TONES``）。正文按空行分段；小标题直接用粗体写在模板里。
PROPOSED_CARDS: dict[str, tuple[str, str]] = {
    "onboarding.checking": (
        "正在核对身份",
        f"已收到你的消息，正在核对你的身份和银河权限。\n\n{NEXT}\n请稍候，无需重复发送。",
    ),
    "onboarding.matched": (
        "正在开通",
        "已核对到你的银河权限，正在完成 BI Plus 开通和权限同步。\n\n"
        f"{NEXT}\n请稍候，同步完成后会通知你。",
    ),
    "onboarding.syncing": (
        "权限正在同步",
        "已核对到你的银河权限，正在完成 BI Plus 开通和权限同步。\n"
        f"预计最多需要十五分钟。\n\n{NEXT}\n无需重复开通，同步完成后会通知你。",
    ),
    "onboarding.innertest_waiting": (
        "已加入内测 · 等待开通",
        f"你已加入内测，**开通尚未开始**。\n\n{NEXT}\n请稍后重新发送消息。",
    ),
    "onboarding.completed": (
        "开通完成",
        "你现在可以开始使用 BI Plus。\n\n**当前可用范围**\n公司：{company_name}\n"
        f"职能：{{function_name}}\n\n{NEXT}\n开通过程中发送的内容不会被处理；"
        "如果仍然需要那个问题的答案，请现在重新发送。",
    ),
    "onboarding.preprovisioned_first_chat": (
        "已开通",
        "你的 BI Plus 已经开通。\n\n**当前可用范围**\n公司：{company_name}\n"
        f"职能：{{function_name}}\n\n{NEXT}\n你可以直接提问；不知道从哪里开始，"
        "可以直接问我“你能查什么？”。",
    ),
    "onboarding.not_authorized": (
        "没有可用的银河权限",
        "当前没有可用的银河权限。BI Plus 不能代替你申请或扩大银河权限。\n\n"
        f"{NEXT}\n请先在银河申请或补充权限；生效并完成同步后，再回到 BI Plus 使用。\n"
        "如果你在银河已经有权限但仍看到此提示，请联系银河管理员。",
    ),
    "onboarding.sync_timeout": (
        "权限同步未完成",
        f"权限同步未完成，已转交处理。\n追溯号：{{reference}}\n\n{NEXT}\n无需重复开通。",
    ),
    "onboarding.innertest_not_open": (
        "暂未向你开放",
        f"BI Plus 目前处于内测阶段，暂未向你开放。\n\n{NEXT}\n{_INNERTEST_CONTACT}",
    ),
    "onboarding.delegated_subject": (
        "专用账号不提供问数",
        "这个账号是组织资料同步的专用账号，不提供问数服务，也不会建立使用记录。",
    ),
    "onboarding.internal_error": (
        "暂时无法完成开通",
        "当前暂时无法完成开通，已转交管理员处理。\n错误码：LX-ONBOARD-001\n"
        f"追溯号：{{reference}}\n\n{NEXT}\n你可以稍后重新发送消息重试；处理完成后我们会通知你。",
    ),
    "onboarding.stalled": (
        "开通已停止等待",
        "距离你发起开通已经过了较长时间，仍然没有得到结果，本次已经停止等待。\n"
        f"错误码：LX-ONBOARD-001\n追溯号：{{reference}}\n\n{NEXT}\n你可以再发一条消息重新开始。",
    ),
    "gateway.busy_hint": (
        "这条消息未受理",
        "当前任务仍在处理中，这条新消息**未受理**，也不会自动排队。\n\n"
        f"{NEXT}\n如仍需要，请等当前任务结束后重新发送。",
    ),
    "gateway.busy_hint_rejected": (
        "这条消息未受理",
        "前面还有任务在排队，这条新消息**未受理**，也不会自动排队。\n\n"
        f"{NEXT}\n如仍需要，请等前面的任务结束后重新发送。",
    ),
    "gateway.suspended": ("账号已停用", "你的 BI Plus 账号当前已停用，暂时无法发起新的问数。"),
    "gateway.delivery_expired": (
        "上次结果已过期",
        f"上一次问数结果因超过投递时限未能确认送达，已过期。\n\n{NEXT}\n请重新提问。",
    ),
    "gateway.slash_rejected": (
        "暂不支持系统命令",
        "以 / 开头的内容会被识别为系统命令，暂不支持。\n\n"
        f"{NEXT}\n请去掉开头的斜杠，用自然语言重新描述你的问题。",
    ),
    "gateway.queue_failed": (
        "暂时无法开始处理",
        f"消息已收到，但当前暂时无法开始处理。\n错误码：LX-QUEUE-001\n\n{NEXT}\n请稍后重试。",
    ),
    "gateway.unexpected_error": (
        "处理出现内部问题",
        "处理这条消息时出现了内部问题，暂时无法完成。\n错误码：LX-GATEWAY-001\n"
        f"追溯号：{{reference}}\n\n{NEXT}\n请稍后重试。",
    ),
    "gateway.new_session": ("已开启新会话", "可以开始提问。"),
    "gateway.session_rotated": (
        "已开启新会话",
        "距上次对话已超过两小时，本次提问不携带此前上下文。",
    ),
    "memory.usage_help": (
        "记忆命令用法",
        "/memory list：查看已登记记忆（附带序号）\n"
        "/memory remember 〈类型〉 〈关键词〉 => 〈说明〉：登记\n"
        "/memory forget 〈序号〉：删除一条（序号见 /memory list，也可以直接用完整 id）\n"
        "/memory clear：清空全部\n\n**类型**\n"
        "term\\_mapping（术语映射）/ calibration\\_preference（口径偏好）/ "
        "convention\\_template（惯例模板）\n\n**须知**\n"
        "记忆只接受“关键词 => 说明”这一种登记形状，不会存查询结果、数字等数据本身。\n"
        "最多登记 50 条，达到上限后新的登记会被拒绝，需要先用 /memory clear 或 "
        "/memory forget 删除几条。\n"
        "账号被停用或数据权限发生变化时，已登记的记忆会被清空且无法恢复。",
    ),
    "memory.remember_unsafe": (
        "记忆未能登记",
        "其中包含疑似系统指令或内部标识的文本，无法安全展示或使用。\n\n"
        f"{NEXT}\n请修改关键词或说明后重试。",
    ),
    "memory.limit_exceeded": (
        "记忆已达上限",
        "已达到记忆条数上限（50 条）。\n\n"
        f"{NEXT}\n请先用 /memory clear 或 /memory forget 〈序号〉 删除几条后再登记。",
    ),
    "memory.forget_not_found": (
        "没有找到这条记忆",
        "可能已被删除、不属于你，或序号已过期。\n\n"
        f"{NEXT}\n可以先用 /memory list 重新查看当前序号。",
    ),
    "memory.remembered": (
        "已登记这条记忆",
        f"下一次提问开始生效。\n\n{NEXT}\n可以用 /memory list 查看全部。",
    ),
    "memory.forgotten": (
        "已删除这条记忆",
        "类型：{type_label}\n关键词：{memory_key}\n说明：{memory_value}",
    ),
    "memory.forgotten_unsafe": ("已删除这条记忆", "类型：{type_label}\n该条内容无法安全展示。"),
    "memory.cleared": ("已清空全部记忆", "共清空 {count} 条。"),
    "memory.list_empty": (
        "还没有登记记忆",
        f"你还没有登记任何记忆。\n\n{NEXT}\n"
        "可以用 /memory remember 〈类型〉 〈关键词〉 => 〈说明〉 登记，类型可选 "
        "term\\_mapping（术语映射）/ calibration\\_preference（口径偏好）/ "
        "convention\\_template（惯例模板）。",
    ),
    "memory.list": ("已登记的记忆", "{entries}"),
    "permission.range_updated": (
        "可用范围已更新",
        "**当前可用范围**\n公司：{company_name}\n职能：{function_name}\n\n"
        f"{NEXT}\n你可以直接发起一次查询验证；如有疑问请联系管理员。",
    ),
    "permission.range_revoked": (
        "可用范围已更新",
        f"当前暂无可用的数据范围。\n\n{NEXT}\n如有疑问请联系管理员。",
    ),
}

#: 交给整合路的改措辞清单：原文本键 → 新措辞（``gateway.busy_hint`` 为已知必改）。
PROPOSED_TEXTS: dict[str, str] = {
    "gateway.busy_hint": (
        "当前任务仍在处理中，这条新消息未受理，也不会自动排队；"
        "如仍需要，请等当前任务结束后重新发送。"
    ),
    "gateway.busy_hint_rejected": (
        "前面还有任务在排队，这条新消息未受理，也不会自动排队；"
        "如仍需要，请等前面的任务结束后重新发送。"
    ),
}


def catalog_with_cards(
    cards: dict[str, tuple[str, str]] | None = None,
    *,
    buttons: tuple[str, ...] = (),
    base: ContentCatalog = _BASE,
) -> ContentCatalog:
    """测试夹具：不传参数时就是正式目录（卡片键已登记）；传入模板或按钮文字时在剔除
    通知卡片键的目录上追加这些模板，走同一道模板校验。"""
    if cards is None and not buttons and base is _BASE:
        return _OFFICIAL
    registered = dict(base._cards)  # noqa: SLF001 - 夹具只读正式目录的已校验模板
    for text_key, (title, body) in (PROPOSED_CARDS if cards is None else cards).items():
        key = notice_card_key(text_key)
        registered[key] = content_module._parse_card_template(  # noqa: SLF001
            key, {"title": title, "body": body, "button_labels": list(buttons)}
        )
    return ContentCatalog(version=base.version, texts=base._texts, cards=registered)  # noqa: SLF001


def _payload_text(card: NoticeCard) -> str:
    payload = card.to_payload()
    parts = [payload["header"]["title"]["content"]]
    parts += [element.get("content", "") for element in payload["body"]["elements"]]
    return "\n".join(parts)


class FakeNoticeReplies(FakeReplies):
    """同步回复假实现，多一个卡片发送口；``notice_error`` 模拟卡片结果不明。"""

    def __init__(self, log: CallLog, *, notice_error: Exception | None = None) -> None:
        super().__init__(log)
        self.notice_error = notice_error

    def send_notice(
        self, *, chat_id: str, thread_id: str | None, reply_to_message_id: str, card: NoticeCard
    ) -> None:
        self._log.add("reply.send_notice", thread_id=thread_id, card=card)
        if self.notice_error is not None:
            raise self.notice_error


class FakeUserSender:
    """主动私聊假实现；前 ``notice_failures`` 次卡片发送抛错，模拟结果不明。"""

    def __init__(self, *, notice_failures: int = 0) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._notice_failures = notice_failures

    def send_text(self, *, open_id: str, text: str, dedupe_key: str) -> None:
        self.calls.append(("text", {"open_id": open_id, "text": text, "dedupe_key": dedupe_key}))

    def send_notice(self, *, open_id: str, card: NoticeCard, dedupe_key: str) -> None:
        self.calls.append(("card", {"open_id": open_id, "card": card, "dedupe_key": dedupe_key}))
        if sum(1 for kind, _ in self.calls if kind == "card") <= self._notice_failures:
            raise RuntimeError("结果不明")


def _message(event_id: str = "evt_1", text: str = "本月销售额是多少") -> InboundMessage:
    return InboundMessage(
        event_id=event_id,
        event_type="im.message.receive_v1",
        sender_open_id="ou_1",
        chat_id="oc_1",
        thread_id=None,
        message_id=f"om_{event_id}",
        text=text,
        trace_id=f"trc_{event_id}",
    )


# ----------------------------------------------------------------------
# 交给整合路的清单本身
# ----------------------------------------------------------------------


class ProposedCatalogTests(unittest.TestCase):
    def test_every_eligible_key_has_a_proposal_and_nothing_else(self) -> None:
        self.assertEqual(set(PROPOSED_CARDS), set(NOTICE_TONES) - _OTHER_GROUP_TONE_KEYS)
        self.assertLessEqual(_OTHER_GROUP_TONE_KEYS, set(NOTICE_TONES))

    def test_the_official_catalog_registers_exactly_the_proposals(self) -> None:
        for key, (title, body) in PROPOSED_CARDS.items():
            with self.subTest(key=key):
                template = _OFFICIAL._cards[notice_card_key(key)]  # noqa: SLF001
                self.assertEqual((template.title.template, template.body.template), (title, body))
                self.assertEqual(template.button_labels, ())
        for key, text in PROPOSED_TEXTS.items():
            with self.subTest(text_key=key):
                self.assertEqual(_OFFICIAL.text(key).text, text)

    def test_card_placeholders_match_the_text_key_one_for_one(self) -> None:
        for key, (title, body) in PROPOSED_CARDS.items():
            with self.subTest(key=key):
                expected = text_placeholders(key, _BASE.text_template(key))
                self.assertEqual(
                    text_placeholders(key, title) | text_placeholders(key, body), expected
                )

    def test_proposals_pass_catalog_validation_and_render_actionless_cards(self) -> None:
        catalog = catalog_with_cards()
        for key in PROPOSED_CARDS:
            with self.subTest(key=key):
                names = text_placeholders(key, _BASE.text_template(key))
                values = {name: f"值{index}" for index, name in enumerate(sorted(names))}
                if "reference" in values:
                    values["reference"] = "trc_1"
                content = catalog.text(key, **values)
                card = build_notice_card(catalog, content, values)
                self.assertIsNotNone(card)
                self.assertEqual(card.fallback_text, content.text)
                assert_no_actionable_elements(card.to_payload())

    def test_proposed_rewording_keeps_placeholders_and_passes_safety(self) -> None:
        for key, text in PROPOSED_TEXTS.items():
            with self.subTest(key=key):
                changed = _BASE.with_text_overrides({key: text})
                self.assertEqual(changed.text(key).text, text)

    def test_registered_real_cards_if_any_keep_placeholders_aligned(self) -> None:
        """整合路合入后生效：正式目录里已有的 ``notice.*`` 卡片与原文本键占位一致。"""
        for key in NOTICE_TONES:
            with self.subTest(key=key):
                self.assertTrue(_OFFICIAL.has_card(notice_card_key(key)))
                content = _OFFICIAL.text(key, **_sample(key))
                self.assertIsNotNone(recover_values(_OFFICIAL, content))


def _sample(key: str) -> dict[str, str]:
    names = text_placeholders(key, _BASE.text_template(key))
    return {name: "trc_1" if name == "reference" else "示例" for name in names}


# ----------------------------------------------------------------------
# 否定断言
# ----------------------------------------------------------------------


class NegativeWordingTests(unittest.TestCase):
    def _visible(self, key: str) -> str:
        title, body = PROPOSED_CARDS[key]
        return "\n".join((title, body, PROPOSED_TEXTS.get(key, "")))

    def test_not_accepted_is_never_worded_as_queued(self) -> None:
        for key in ("gateway.busy_hint", "gateway.busy_hint_rejected"):
            with self.subTest(key=key):
                visible = self._visible(key)
                self.assertIn("未受理", visible)
                self.assertIn("不会自动排队", visible)
                for phrase in ("排队中", "已排队", "已受理", "无需重复发送"):
                    self.assertNotIn(phrase, visible)

    def test_joined_innertest_is_never_worded_as_provisioned(self) -> None:
        visible = self._visible("onboarding.innertest_waiting")
        for phrase in ("开通完成", "可以开始使用", "已开通", "已经开通"):
            self.assertNotIn(phrase, visible)

    def test_no_card_promises_an_authorization_validity_period(self) -> None:
        for key in PROPOSED_CARDS:
            with self.subTest(key=key):
                self.assertNotIn("有效期", self._visible(key))

    def test_missing_value_is_not_filled_with_zero(self) -> None:
        catalog = catalog_with_cards()
        content = catalog.text("memory.cleared", count=3)
        self.assertIsNone(build_notice_card(catalog, content, {}))
        card = build_notice_card(catalog, content, {"count": 3})
        self.assertIn("共清空 3 条", _payload_text(card))

    def test_button_labels_on_a_notice_card_are_refused(self) -> None:
        catalog = catalog_with_cards(
            {"gateway.new_session": ("已开启", "可以提问")}, buttons=("好",)
        )
        content = catalog.text("gateway.new_session")
        self.assertIsNone(build_notice_card(catalog, content, {}))

    def test_link_in_a_template_is_refused_rather_than_sent(self) -> None:
        catalog = catalog_with_cards({"gateway.new_session": ("已开启", "详见 https://x.example")})
        self.assertIsNone(build_notice_card(catalog, catalog.text("gateway.new_session"), {}))

    def test_dynamic_values_are_escaped_and_cannot_become_links_or_mentions(self) -> None:
        catalog = catalog_with_cards()
        values = {
            "type_label": "术语映射",
            "memory_key": "[点我](https://evil.example)",
            "memory_value": "<at id=all></at>",
        }
        card = build_notice_card(catalog, catalog.text("memory.forgotten", **values), values)
        self.assertIsNotNone(card)
        rendered = _payload_text(card)
        self.assertNotIn("](https://", rendered)
        self.assertNotIn("<at", rendered)
        assert_no_actionable_elements(card.to_payload())

    def test_keys_outside_the_group_stay_text_even_with_a_card_registered(self) -> None:
        catalog = catalog_with_cards({"admin.unknown_command": ("没看懂", "请重试")})
        content = catalog.text("admin.unknown_command")
        self.assertIsNone(reply_notice_card(catalog, content))


# ----------------------------------------------------------------------
# 变量取回（gateway 同步回复只拿得到成品文本）
# ----------------------------------------------------------------------


class RecoverValuesTests(unittest.TestCase):
    def test_unique_split_is_recovered(self) -> None:
        content = _BASE.text("gateway.unexpected_error", reference="trc_9")
        self.assertEqual(recover_values(_BASE, content), {"reference": "trc_9"})

    def test_ambiguous_split_is_not_guessed(self) -> None:
        content = _BASE.text(
            "memory.forgotten", type_label="术语映射", memory_key="a => b", memory_value="c"
        )
        self.assertIsNone(recover_values(_BASE, content))

    def test_text_not_rendered_by_the_catalog_is_not_matched(self) -> None:
        from lingxi.config.content import RenderedContent

        forged = RenderedContent(key="gateway.busy_hint", version=_BASE.version, text="别的话")
        self.assertIsNone(recover_values(_BASE, forged))


# ----------------------------------------------------------------------
# 出口 1：gateway 同步回复（开通过程、忙碌 / 会话 / 命令、记忆）
# ----------------------------------------------------------------------


class GatewayReplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.log = CallLog()
        self.state = FakeState()
        self.state.users["ou_1"] = provisioned_user()

    def _pipeline(self, catalog: ContentCatalog, *, notice_error: Exception | None = None):
        return EventPipeline(
            store=FakeStore(self.state, self.log),
            reactions=FakeReactions(self.log),
            replies=FakeNoticeReplies(self.log, notice_error=notice_error),
            audit=FakeAudit(self.log),
            texts=GatewayTexts(catalog=catalog),
        )

    def _busy(self, status: str) -> None:
        self.state.conversations[("usr_1", "oc_1", "")] = FakeConversation(
            conversation_id="cnv_busy", running_task_id="tsk_running"
        )
        self.state.tasks.append(
            FakeTask(
                task_id="tsk_running",
                conversation_id="cnv_busy",
                user_id="usr_1",
                inbound_event_id="evt_prior",
                prompt="之前的问题",
                resumed_session=False,
                target_worker_version="stable",
                status=status,
            )
        )

    def test_without_card_key_the_reply_is_the_same_text(self) -> None:
        self._busy("queued")
        self._pipeline(_BASE).handle_message(_message(), now=NOW)
        self.assertEqual(self.log.count("reply.send_notice"), 0)
        texts = [call["text"] for call in self.log.fields("reply.send_text")]
        self.assertEqual(texts, [_BASE.text("gateway.busy_hint_rejected").text])

    def test_with_card_key_the_busy_hint_goes_out_as_a_card(self) -> None:
        self._busy("running")
        catalog = catalog_with_cards()
        self._pipeline(catalog).handle_message(_message(), now=NOW)
        self.assertEqual(self.log.count("reply.send_text"), 0)
        cards = self.log.fields("reply.send_notice")
        self.assertEqual(len(cards), 1)
        card = cards[0]["card"]
        self.assertEqual(card.fallback_text, catalog.text("gateway.busy_hint").text)
        self.assertEqual(card.to_payload()["header"]["template"], "orange")
        self.assertEqual(self.log.fields("audit.reply.sent")[0]["content_key"], "gateway.busy_hint")

    def test_uncertain_card_result_is_recorded_as_failed_and_no_text_follows(self) -> None:
        self._busy("running")
        self._pipeline(catalog_with_cards(), notice_error=RuntimeError("超时")).handle_message(
            _message(), now=NOW
        )
        self.assertEqual(self.log.count("reply.send_notice"), 1)
        self.assertEqual(self.log.count("reply.send_text"), 0)
        self.assertEqual(len(self.log.fields("audit.reply.failed")), 1)

    def test_memory_list_values_are_escaped_inside_the_card(self) -> None:
        self.state.user_memory["usr_1"] = [
            UserMemoryEntry(
                memory_id="mem_1",
                memory_type="term_mapping",
                memory_key="[点我](https://evil.example)",
                memory_value="尼日利亚",
                created_at=NOW,
            )
        ]
        self._pipeline(catalog_with_cards()).handle_message(_message(text="/memory list"), now=NOW)
        card = self.log.fields("reply.send_notice")[0]["card"]
        self.assertIn("[点我](https://evil.example)", card.fallback_text)
        self.assertNotIn("](https://", _payload_text(card))

    def test_ambiguous_memory_receipt_falls_back_to_the_same_text(self) -> None:
        self.state.user_memory["usr_1"] = [
            UserMemoryEntry(
                memory_id="mem_1",
                memory_type="term_mapping",
                memory_key="a => b",
                memory_value="c",
                created_at=NOW,
            )
        ]
        self._pipeline(catalog_with_cards()).handle_message(
            _message(text="/memory forget 1"), now=NOW
        )
        self.assertEqual(self.log.count("reply.send_notice"), 0)
        self.assertIn("a => b", self.log.fields("reply.send_text")[0]["text"])

    def test_injected_custom_text_is_never_turned_into_a_card(self) -> None:
        self._busy("running")
        texts = GatewayTexts(busy_hint="自定义提示", catalog=catalog_with_cards())
        EventPipeline(
            store=FakeStore(self.state, self.log),
            reactions=FakeReactions(self.log),
            replies=FakeNoticeReplies(self.log),
            audit=FakeAudit(self.log),
            texts=texts,
        ).handle_message(_message(), now=NOW)
        self.assertEqual(self.log.count("reply.send_notice"), 0)
        self.assertEqual(self.log.fields("reply.send_text")[0]["text"], "自定义提示")

    def test_replies_without_a_card_port_keep_sending_text(self) -> None:
        self._busy("running")
        EventPipeline(
            store=FakeStore(self.state, self.log),
            reactions=FakeReactions(self.log),
            replies=FakeReplies(self.log),
            audit=FakeAudit(self.log),
            texts=GatewayTexts(catalog=catalog_with_cards()),
        ).handle_message(_message(), now=NOW)
        self.assertEqual(len(self.log.fields("reply.send_text")), 1)


# ----------------------------------------------------------------------
# 出口 2 / 3：开通终态主动私聊、权限范围变化通知
# ----------------------------------------------------------------------


class ProactiveNoticeTests(unittest.TestCase):
    def test_catalog_notifier_without_card_key_sends_the_same_text(self) -> None:
        from lingxi.apps.scheduler.onboarding import CatalogNotifier

        sender = FakeUserSender()
        values = {"company_name": "1011", "function_name": "日活"}
        CatalogNotifier(sender=sender, catalog=_BASE).send(
            open_id="ou_1", key="onboarding.completed", values=values, dedupe_key="d1"
        )
        expected = _BASE.text("onboarding.completed", **values).text
        self.assertEqual(
            sender.calls, [("text", {"open_id": "ou_1", "text": expected, "dedupe_key": "d1"})]
        )

    def test_catalog_notifier_with_card_key_sends_a_card_with_the_same_dedupe_key(self) -> None:
        from lingxi.apps.scheduler.onboarding import CatalogNotifier

        sender, catalog = FakeUserSender(), catalog_with_cards()
        CatalogNotifier(sender=sender, catalog=catalog).send(
            open_id="ou_1", key="onboarding.stalled", values={"reference": "trc_1"}, dedupe_key="d1"
        )
        self.assertEqual([kind for kind, _ in sender.calls], ["card"])
        card = sender.calls[0][1]["card"]
        self.assertEqual(sender.calls[0][1]["dedupe_key"], "d1")
        self.assertEqual(card.to_payload()["header"]["template"], "red")
        self.assertEqual(
            card.fallback_text, catalog.text("onboarding.stalled", reference="trc_1").text
        )

    def test_send_catalog_notice_uses_text_when_the_sender_has_no_card_port(self) -> None:
        sender = SimpleNamespace(calls=[])
        sender.send_text = lambda **kwargs: sender.calls.append(kwargs)
        send_catalog_notice(sender, catalog_with_cards(), "ou_1", "gateway.suspended", {}, "d1")
        self.assertEqual(sender.calls[0]["text"], _BASE.text("gateway.suspended").text)

    def _dispatcher(self, sender, catalog):
        return PermissionNoticeDispatcher(
            sender=sender,
            audit=SimpleNamespace(record=lambda *a, **k: None),
            sleep=lambda _: None,
            catalog=catalog,
        )

    def test_permission_notice_without_card_key_is_unchanged(self) -> None:
        sender = FakeUserSender()
        result = self._dispatcher(sender, _BASE).notify(
            user_id="usr_1", open_id="ou_1", permission_version=3, permissions="{}"
        )
        self.assertTrue(result.delivered)
        self.assertEqual([kind for kind, _ in sender.calls], ["text"])
        self.assertIsNone(render_scope_notice("{}", catalog=_BASE).card)

    def test_permission_notice_with_card_key_sends_one_card(self) -> None:
        sender = FakeUserSender()
        result = self._dispatcher(sender, catalog_with_cards()).notify(
            user_id="usr_1",
            open_id="ou_1",
            permission_version=3,
            permissions='{"1011":["日活"]}',
        )
        self.assertTrue(result.delivered)
        self.assertEqual([kind for kind, _ in sender.calls], ["card"])
        card = sender.calls[0][1]["card"]
        self.assertIn("公司：1011", _payload_text(card))
        self.assertEqual(sender.calls[0][1]["dedupe_key"], "usr_1:3")

    def test_uncertain_card_is_retried_as_a_card_never_as_extra_text(self) -> None:
        sender = FakeUserSender(notice_failures=1)
        result = self._dispatcher(sender, catalog_with_cards()).notify(
            user_id="usr_1", open_id="ou_1", permission_version=3, permissions="{}"
        )
        self.assertTrue(result.delivered)
        self.assertEqual([kind for kind, _ in sender.calls], ["card", "card"])
        self.assertEqual({call["dedupe_key"] for _, call in sender.calls}, {"usr_1:3"})


# ----------------------------------------------------------------------
# 同步回复适配器：明确拒绝才回落一次，结果不明不补发
# ----------------------------------------------------------------------


class _Builder:
    def __init__(self) -> None:
        self.fields: dict = {}

    def __getattr__(self, name: str):
        def setter(value):
            self.fields[name] = value
            return self

        return setter

    def build(self):
        return SimpleNamespace(**self.fields)


def _fake_lark_modules() -> dict:
    v1 = types.ModuleType("lark_oapi.api.im.v1")
    for name in ("ReplyMessageRequest", "ReplyMessageRequestBody"):
        setattr(v1, name, SimpleNamespace(builder=_Builder))
    names = ("lark_oapi", "lark_oapi.api", "lark_oapi.api.im")
    modules = {name: types.ModuleType(name) for name in names}
    modules["lark_oapi.api.im.v1"] = v1
    return modules


class _Response:
    def __init__(self, code, message_id: str | None = "om_reply") -> None:
        self.code = code
        self.msg = "m"
        self.data = SimpleNamespace(message_id=message_id)

    def success(self) -> bool:
        return self.code == 0

    def get_log_id(self) -> str:
        return "log"


class LarkRepliesNoticeTests(unittest.TestCase):
    def _send(self, *responses):
        from lingxi.adapters.feishu_outbound import LarkReplies

        requests: list = []
        queue = list(responses)

        def reply(request):
            requests.append(request.request_body)
            outcome = queue.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        client = SimpleNamespace(
            im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(reply=reply)))
        )
        card = build_notice_card(catalog_with_cards(), _BASE.text("gateway.new_session"), {})
        with unittest.mock.patch.dict(sys.modules, _fake_lark_modules()):
            try:
                LarkReplies(client).send_notice(
                    chat_id="oc_1", thread_id="omt_1", reply_to_message_id="om_1", card=card
                )
            except Exception as error:  # 用例按结果断言
                return requests, error
        return requests, None

    def test_accepted_card_sends_nothing_else(self) -> None:
        requests, error = self._send(_Response(0))
        self.assertIsNone(error)
        self.assertEqual([request.msg_type for request in requests], ["interactive"])
        self.assertTrue(requests[0].reply_in_thread)

    def test_definite_rejection_falls_back_to_text_exactly_once(self) -> None:
        requests, error = self._send(_Response(230099), _Response(0))
        self.assertIsNone(error)
        self.assertEqual([request.msg_type for request in requests], ["interactive", "text"])
        self.assertIn(_BASE.text("gateway.new_session").text, requests[1].content)

    def test_rejected_fallback_is_not_retried(self) -> None:
        requests, error = self._send(_Response(230099), _Response(230099))
        self.assertIsNotNone(error)
        self.assertEqual(len(requests), 2)

    def test_uncertain_results_never_fall_back(self) -> None:
        for outcome in (_Response(None), _Response(0, message_id=None), TimeoutError("超时")):
            with self.subTest(outcome=outcome):
                requests, error = self._send(outcome)
                self.assertIsNotNone(error)
                self.assertEqual([request.msg_type for request in requests], ["interactive"])


if __name__ == "__main__":
    unittest.main()
