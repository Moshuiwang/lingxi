"""#891 甲-b：排队提示、流式问数卡、文档 / 表格交付通知的卡片化。

断言跑在注入的假传输层与测试夹具目录上（夹具只在内存里给默认目录加卡片键，
不改正式目录）。覆盖面：

- 卡片键**未进目录**：各入口发出的文本与今天逐字相同，不发卡片；
- 卡片键**进目录**：改发卡片，卡片带与原文本逐字相同的等价纯文本；
- 飞书**明确拒绝**卡片：纯文本恰好补发一次；**结果不明**：不补发、不记已通知；
- 已受理排队 ≠ 未受理：排队提示不叫用户重新发送；
- 文档降级文案不对所有来源声称「没有删减」；追溯号完整保留；
- 流式卡终态在同一张卡的同一元素上改标题与正文，不留「正在查询」、不加标题栏；
  改用文字消息前的最后一帧在卡片键进目录后同样不再停在「正在查询」。
"""

from __future__ import annotations

import json
import sys
import types
import unittest
from datetime import timedelta
from typing import Any

from lingxi.adapters.feishu_delivery import LarkDeliveryText, _card_payload
from lingxi.adapters.feishu_user_message import FeishuUserMessages
from lingxi.apps.gateway.delivery import DeliveryConsumer
from lingxi.apps.gateway.document_delivery import DocumentDeliveryConsumer
from lingxi.config import content as content_module
from lingxi.config.content import ContentCatalog, default_content_catalog
from lingxi.core.delivery.catalog_notice import (
    DELIVERY_NOTICE_TONES,
    NOTICE_TONES,
    build_notice_card,
)
from lingxi.core.delivery.notice_card import NoticeCard, NoticeTone
from lingxi.core.execution.card_stream import (
    CardCreated,
    CardStream,
    DeliveryRejectedError,
    DeliveryUncertainError,
)

OPEN_ID = "ou_fake_open_id_for_tests"
DOC_URL = "https://example.feishu.cn/docx/doc-1"

#: 夹具卡片：模板形状即交给整合路的清单（正文以空行分段）。
FIXTURE_CARDS: dict[str, dict[str, Any]] = {
    "notice.gateway.busy_hint_queued": {
        "title": "已在排队",
        "body": "你的问题已在排队，无需重复发送。\n\n前面还有任务在处理，请稍等。",
        "button_labels": [],
    },
    "notice.delivery.document_ready": {
        "title": "文档已生成",
        "body": "**[打开文档]({url})**\n**你的权限**：可管理",
        "button_labels": [],
    },
    "notice.delivery.document_ready_simplified": {
        "title": "文档已生成 · 排版已简化",
        "body": (
            "**[打开文档]({url})**\n**你的权限**：可管理\n\n**排版说明**\n"
            "排版由飞书自动完成，格式做了简化，个别结构可能没有呈现，建议打开后核对。"
        ),
        "button_labels": [],
    },
    "notice.delivery.sheet_uncertain": {
        "title": "表格生成结果待确认",
        "body": "表格生成结果暂无法确认，已转人工核对。\n\n**追溯号**：{reference}",
        "button_labels": [],
    },
    "notice.delivery.sheet_failed": {
        "title": "表格生成失败",
        "body": "你可以重新发起问数再试一次。\n\n**追溯号**：{reference}",
        "button_labels": [],
    },
    "notice.delivery.document_failed": {
        "title": "文档生成失败",
        "body": "你可以重新发起问数再试一次。",
        "button_labels": ["重试"],
    },
    "notice.worker.card_handoff_notice": {
        "title": "结果将以新消息发送",
        "body": "本次处理时间较长，此卡片不再更新；处理完成后结果将以新消息发送，请留意。",
        "button_labels": [],
    },
}


def fixture_catalog(*keys: str) -> ContentCatalog:
    """剔除 ``notice.*`` 的正式目录 + 指定的夹具卡片键；不传键时等于「卡片键未进目录」。

    正式目录已登记通知卡片键，所以基底先剔除它们，演练「未进目录」与单键进目录；
    正式目录本身的卡片由 :class:`OfficialCatalogCardTests` 钉住。
    """
    base = default_content_catalog()
    cards = {k: v for k, v in base._cards.items() if not k.startswith("notice.")}
    for key in keys:
        cards[key] = content_module._parse_card_template(key, FIXTURE_CARDS[key])
    return ContentCatalog(version=base.version, texts=base._texts, cards=cards)


# ----------------------------------------------------------------------------------
# 文档 / 表格交付通知（#11）
# ----------------------------------------------------------------------------------


class _SpyNotifier:
    """用户私聊出口替身：分别记录文本与卡片，可让卡片发送抛出指定异常。"""

    def __init__(self, *, notice_error: Exception | None = None) -> None:
        self.texts: list[tuple[str, str, str]] = []
        self.notices: list[tuple[str, NoticeCard, str]] = []
        self._notice_error = notice_error

    def send_text(self, *, open_id: str, text: str, dedupe_key: str) -> None:
        self.texts.append((open_id, text, dedupe_key))

    def send_notice(self, *, open_id: str, card: NoticeCard, dedupe_key: str) -> None:
        if self._notice_error is not None:
            raise self._notice_error
        self.notices.append((open_id, card, dedupe_key))


class _Store:
    def __init__(self) -> None:
        self.notified: list[str] = []

    def mark_notified(self, *, request_id: str) -> None:
        self.notified.append(request_id)


class _Docx:
    def document_url(self, document_id: str) -> str:
        return f"https://example.feishu.cn/docx/{document_id}"


def _claim(delivery_type: str = "sheet") -> Any:
    return types.SimpleNamespace(
        id="tdd-1",
        task_id="tsk-01J8ZK5V6Y7W8X9A0B1C2D3E4F",
        requester_open_id=OPEN_ID,
        delivery_type=delivery_type,
    )


def _document_consumer(notifier: Any, catalog: ContentCatalog, alerts: list | None = None):
    store = _Store()
    consumer = DocumentDeliveryConsumer(
        store=store,
        docx=_Docx(),
        notifier=notifier,
        catalog=catalog,
        on_alert=lambda kind, task_id: (alerts if alerts is not None else []).append(
            (kind, task_id)
        ),
    )
    return consumer, store


def _send_ready(consumer: DocumentDeliveryConsumer, **overrides: Any) -> None:
    arguments = {
        "request_id": "tdd-1",
        "task_id": "tsk-1",
        "requester_open_id": OPEN_ID,
        "document_id": "doc-1",
    }
    arguments.update(overrides)
    consumer._send_ready_notice(**arguments)


class DocumentNoticeWithoutCardKeyTests(unittest.TestCase):
    def test_ready_notice_is_the_same_text_as_today(self) -> None:
        notifier = _SpyNotifier()
        consumer, store = _document_consumer(notifier, fixture_catalog())

        _send_ready(consumer)

        expected = default_content_catalog().text("delivery.document_ready", url=DOC_URL).text
        self.assertEqual(notifier.texts, [(OPEN_ID, expected, "document-ready:tdd-1")])
        self.assertEqual(notifier.notices, [])
        self.assertEqual(store.notified, ["tdd-1"])

    def test_terminal_notice_is_the_same_text_as_today(self) -> None:
        notifier = _SpyNotifier()
        consumer, _ = _document_consumer(notifier, fixture_catalog())

        consumer._send_terminal_notice(
            _claim(),
            key="delivery.sheet_uncertain",
            dedupe_prefix="sheet-uncertain",
            template_variables={"reference": _claim().task_id},
        )

        expected = (
            default_content_catalog()
            .text("delivery.sheet_uncertain", reference=_claim().task_id)
            .text
        )
        self.assertEqual(notifier.texts, [(OPEN_ID, expected, "sheet-uncertain:tdd-1")])
        self.assertEqual(notifier.notices, [])


class DocumentNoticeCardTests(unittest.TestCase):
    def test_ready_notice_becomes_a_card_with_the_link_first(self) -> None:
        notifier = _SpyNotifier()
        catalog = fixture_catalog("notice.delivery.document_ready")
        consumer, store = _document_consumer(notifier, catalog)

        _send_ready(consumer)

        self.assertEqual(notifier.texts, [], "卡片键进目录后不再直接发文本")
        self.assertEqual(len(notifier.notices), 1)
        open_id, card, dedupe_key = notifier.notices[0]
        self.assertEqual((open_id, dedupe_key), (OPEN_ID, "document-ready:tdd-1"))
        self.assertEqual(card.title, "文档已生成")
        self.assertIs(card.tone, NoticeTone.DONE)
        self.assertIn(f"({DOC_URL})", card.sections[0].lines[0], "结果 / 链接排在首行")
        self.assertEqual(
            card.fallback_text, catalog.text("delivery.document_ready", url=DOC_URL).text
        )
        self.assertEqual(store.notified, ["tdd-1"], "发送成功才记已通知，且只记一次")

    def test_uncertain_card_send_is_not_marked_notified_and_not_retried_as_text(self) -> None:
        alerts: list = []
        notifier = _SpyNotifier(notice_error=TimeoutError("timeout"))
        consumer, store = _document_consumer(
            notifier, fixture_catalog("notice.delivery.document_ready"), alerts
        )

        _send_ready(consumer)

        self.assertEqual(notifier.texts, [], "结果不明不补发纯文本")
        self.assertEqual(store.notified, [], "结果不明不记已通知")
        self.assertEqual(alerts, [("document_delivery_notice_failed", "tsk-1")])

    def test_simplified_card_keeps_the_same_fallback_and_never_claims_nothing_was_cut(
        self,
    ) -> None:
        notifier = _SpyNotifier()
        catalog = fixture_catalog("notice.delivery.document_ready_simplified")
        consumer, _ = _document_consumer(notifier, catalog)

        _send_ready(consumer, body_degraded_reason="server_simplified_body")

        card = notifier.notices[0][1]
        self.assertEqual(card.tone, NoticeTone.DONE)
        self.assertEqual(
            card.fallback_text,
            catalog.text("delivery.document_ready_simplified", url=DOC_URL).text,
        )
        rendered = json.dumps(card.to_payload(), ensure_ascii=False)
        self.assertNotIn("没有删减", rendered)
        self.assertNotIn("没有删减", card.fallback_text)

    def test_sheet_uncertain_card_keeps_the_full_reference(self) -> None:
        notifier = _SpyNotifier()
        catalog = fixture_catalog("notice.delivery.sheet_uncertain")
        consumer, _ = _document_consumer(notifier, catalog)
        reference = _claim().task_id

        consumer._send_terminal_notice(
            _claim(),
            key="delivery.sheet_uncertain",
            dedupe_prefix="sheet-uncertain",
            template_variables={"reference": reference},
        )

        _, card, dedupe_key = notifier.notices[0]
        self.assertEqual(dedupe_key, "sheet-uncertain:tdd-1")
        self.assertIs(card.tone, NoticeTone.ATTENTION)
        self.assertIn(reference, card.sections[-1].lines[0], "追溯号完整、不被转义改写")
        self.assertIn(reference, card.fallback_text)


class _RecordingUserTransport:
    """``FeishuUserMessages`` 的假传输层：按消息类型脚本化响应。"""

    def __init__(self, *, card: Any, text: Any = None) -> None:
        self.calls: list[dict] = []
        self._scripts = {"interactive": card, "text": text or {"code": 0, "data": {}}}

    def __call__(self, method: str, url: str, *, body=None, token=None, **kwargs: Any):
        if "tenant_access_token" in url:
            return {"code": 0, "tenant_access_token": "t-fake", "expire": 7200}
        self.calls.append(body)
        outcome = self._scripts[body["msg_type"]]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def sent(self, msg_type: str) -> list[dict]:
        return [body for body in self.calls if body["msg_type"] == msg_type]


def _real_notifier(transport: _RecordingUserTransport) -> FeishuUserMessages:
    return FeishuUserMessages(
        base_url="https://feishu.invalid/open-apis",
        app_id="cli_fake_app",
        app_secret="fake_app_secret",
        uuid_prefix="lingxi-doc-ready-",
        transport=transport,
    )


class DocumentNoticeThroughRealAdapterTests(unittest.TestCase):
    """真实 ``FeishuUserMessages``：拒绝回落只一次、结果不明不补发。"""

    def test_definite_card_rejection_falls_back_to_text_exactly_once(self) -> None:
        transport = _RecordingUserTransport(
            card={"code": 230099, "msg": "card rejected"},
            text={"code": 0, "data": {"message_id": "om-1"}},
        )
        catalog = fixture_catalog("notice.delivery.document_ready")
        consumer, store = _document_consumer(_real_notifier(transport), catalog)

        _send_ready(consumer)

        self.assertEqual(len(transport.sent("interactive")), 1)
        texts = transport.sent("text")
        self.assertEqual(len(texts), 1, "明确拒绝只回落一次")
        self.assertEqual(
            json.loads(texts[0]["content"])["text"],
            catalog.text("delivery.document_ready", url=DOC_URL).text,
        )
        self.assertTrue(texts[0]["uuid"].startswith("lingxi-doc-ready-"))
        self.assertEqual(store.notified, ["tdd-1"])

    def test_uncertain_card_result_sends_no_text(self) -> None:
        transport = _RecordingUserTransport(card={})
        consumer, store = _document_consumer(
            _real_notifier(transport), fixture_catalog("notice.delivery.document_ready")
        )

        _send_ready(consumer)

        self.assertEqual(len(transport.sent("interactive")), 1)
        self.assertEqual(transport.sent("text"), [], "结果不明不补发")
        self.assertEqual(store.notified, [])


class DocumentWordingNegativeTests(unittest.TestCase):
    def test_degraded_texts_other_than_the_paragraph_path_never_claim_nothing_was_cut(
        self,
    ) -> None:
        catalog = default_content_catalog()
        for key in (
            "delivery.document_ready_simplified",
            "delivery.document_ready_degraded_too_long",
            "delivery.document_ready_degraded_title",
        ):
            with self.subTest(key=key):
                self.assertNotIn("没有删减", catalog.text(key, url=DOC_URL).text)


def build_catalog_notice(catalog, text_key, variables=None):
    """按文本键渲染原文本并配卡：与消费循环走同一个 ``build_notice_card``。"""
    values = dict(variables or {})
    return build_notice_card(catalog, catalog.text(text_key, **values), values)


class BuildCatalogNoticeTests(unittest.TestCase):
    """卡片渲染本身：动态值转义、链接开关、色调与按钮的拒绝规则。"""

    def test_free_text_values_are_escaped_and_cannot_mention_or_link(self) -> None:
        catalog = fixture_catalog("notice.delivery.sheet_failed")
        hostile = "<at id=all></at>[点我](https://evil.example)"

        card = build_catalog_notice(catalog, "delivery.sheet_failed", {"reference": hostile})

        self.assertFalse(card.allow_links, "没有链接变量就不放开链接限制")
        markdown = json.dumps(card.to_payload(), ensure_ascii=False)
        self.assertNotIn("<at", markdown)
        self.assertNotIn("](https://", markdown)
        self.assertNotIn("https://", markdown, "地址被断开，不能成为可点链接")
        self.assertIs(card.tone, NoticeTone.FAILURE)

    def test_unregistered_card_key_returns_none(self) -> None:
        self.assertIsNone(
            build_catalog_notice(fixture_catalog(), "delivery.sheet_failed", {"reference": "r1"})
        )

    def test_card_with_buttons_is_not_built_and_the_text_goes_out_unchanged(self) -> None:
        catalog = fixture_catalog("notice.delivery.document_failed")
        self.assertIsNone(build_catalog_notice(catalog, "delivery.document_failed"))

        notifier = _SpyNotifier()
        consumer, _ = _document_consumer(notifier, catalog)
        consumer._send_terminal_notice(
            _claim("docx"), key="delivery.document_failed", dedupe_prefix="document-failed"
        )
        expected = catalog.text("delivery.document_failed").text
        self.assertEqual(notifier.texts, [(OPEN_ID, expected, "document-failed:tdd-1")])
        self.assertEqual(notifier.notices, [])

    def test_text_key_without_a_registered_tone_never_becomes_a_card(self) -> None:
        base = default_content_catalog()
        cards = dict(base._cards)
        cards["notice.admin.unknown_command"] = content_module._parse_card_template(
            "notice.admin.unknown_command", {"title": "t", "body": "b", "button_labels": []}
        )
        catalog = ContentCatalog(version=base.version, texts=base._texts, cards=cards)
        self.assertNotIn("admin.unknown_command", {**NOTICE_TONES, **DELIVERY_NOTICE_TONES})
        self.assertIsNone(build_catalog_notice(catalog, "admin.unknown_command"))

    def test_this_groups_keys_are_registered_once_with_their_tones(self) -> None:
        expected = {
            "gateway.busy_hint_queued": NoticeTone.PROCESSING,
            "delivery.document_ready": NoticeTone.DONE,
            "delivery.sheet_ready": NoticeTone.DONE,
            "delivery.document_failed": NoticeTone.FAILURE,
            "delivery.sheet_failed": NoticeTone.FAILURE,
            "delivery.document_uncertain": NoticeTone.ATTENTION,
            "delivery.sheet_uncertain": NoticeTone.ATTENTION,
        }
        for key, tone in expected.items():
            with self.subTest(key=key):
                self.assertIs(DELIVERY_NOTICE_TONES[key], tone)
                self.assertNotIn(key, NOTICE_TONES, "两张色调表的键不重复")


# ----------------------------------------------------------------------------------
# 排队超时提示（#4）
# ----------------------------------------------------------------------------------


class _StaleQueue:
    def __init__(self) -> None:
        self.rows = [
            types.SimpleNamespace(
                task_id="tsk-stale",
                chat_id="chat-1",
                thread_id="topic-1",
                reply_to_message_id="reply-1",
                trace_id=None,
            )
        ]

    def list_stale_queued_tasks(self, *, older_than: timedelta, limit: int):
        del older_than, limit
        return list(self.rows)


class _SpyReplyTexts:
    def __init__(self, *, notice_error: Exception | None = None) -> None:
        self.texts: list[dict] = []
        self.notices: list[dict] = []
        self._notice_error = notice_error

    def send_text(self, **kwargs: Any) -> str:
        self.texts.append(kwargs)
        return "om-text"

    def send_notice(self, **kwargs: Any) -> str:
        if self._notice_error is not None:
            raise self._notice_error
        self.notices.append(kwargs)
        return "om-card"


class OfficialCatalogCardTests(unittest.TestCase):
    """正式目录（卡片键已登记）：本组每个文本键都配得出无按钮的卡片，等价纯文本就是原文本。"""

    _SAMPLES: dict[str, dict[str, str]] = {
        "url": {"url": DOC_URL},
        "reference": {"reference": "r1"},
    }

    def test_every_delivery_key_builds_an_actionless_card_from_the_official_catalog(self) -> None:
        catalog = default_content_catalog()
        for key in DELIVERY_NOTICE_TONES:
            with self.subTest(key=key):
                names = catalog._texts[key].variables
                values = {k: v for n in names for k, v in self._SAMPLES[n].items()}
                card = build_catalog_notice(catalog, key, values)
                self.assertIsNotNone(card)
                self.assertIs(card.tone, DELIVERY_NOTICE_TONES[key])
                self.assertEqual(card.fallback_text, catalog.text(key, **values).text)
                self.assertEqual(card.allow_links, "url" in values)

    def test_official_degraded_cards_other_than_the_paragraph_path_never_claim_nothing_was_cut(
        self,
    ) -> None:
        catalog = default_content_catalog()
        for key in (
            "delivery.document_ready_simplified",
            "delivery.document_ready_degraded_too_long",
            "delivery.document_ready_degraded_title",
        ):
            with self.subTest(key=key):
                card = build_catalog_notice(catalog, key, {"url": DOC_URL})
                self.assertNotIn("没有删减", json.dumps(card.to_payload(), ensure_ascii=False))

    def test_official_queued_hint_card_never_asks_to_resend(self) -> None:
        card = build_catalog_notice(default_content_catalog(), "gateway.busy_hint_queued")
        payload = json.dumps(card.to_payload(), ensure_ascii=False)
        self.assertIn("无需重复发送", payload)
        self.assertNotIn("重新发送", payload)


def _queued_consumer(texts: Any, catalog: ContentCatalog) -> DeliveryConsumer:
    return DeliveryConsumer(queue=_StaleQueue(), cards=object(), texts=texts, catalog=catalog)


class QueuedHintTests(unittest.TestCase):
    def test_without_card_key_the_hint_is_the_same_text_as_today(self) -> None:
        texts = _SpyReplyTexts()
        consumer = _queued_consumer(texts, fixture_catalog())

        consumer._notify_stale_queued()

        self.assertEqual(
            texts.texts,
            [
                {
                    "chat_id": "chat-1",
                    "thread_id": "topic-1",
                    "reply_to_message_id": "reply-1",
                    "text": default_content_catalog().text("gateway.busy_hint_queued").text,
                }
            ],
        )
        self.assertEqual(texts.notices, [])

    def test_with_card_key_the_hint_is_a_processing_card_sent_once(self) -> None:
        texts = _SpyReplyTexts()
        catalog = fixture_catalog("notice.gateway.busy_hint_queued")
        consumer = _queued_consumer(texts, catalog)

        consumer._notify_stale_queued()
        consumer._notify_stale_queued()

        self.assertEqual(texts.texts, [])
        self.assertEqual(len(texts.notices), 1, "同一任务持续排队只提示一次")
        call = texts.notices[0]
        self.assertEqual(
            (call["chat_id"], call["thread_id"], call["reply_to_message_id"]),
            ("chat-1", "topic-1", "reply-1"),
        )
        card = call["card"]
        self.assertIs(card.tone, NoticeTone.PROCESSING)
        self.assertEqual(card.title, "已在排队")
        self.assertEqual(card.fallback_text, catalog.text("gateway.busy_hint_queued").text)
        self.assertEqual(len(card.sections), 2)

    def test_uncertain_card_is_retried_next_round_and_never_replaced_by_text(self) -> None:
        texts = _SpyReplyTexts(notice_error=DeliveryUncertainError(reason="missing_code"))
        consumer = _queued_consumer(texts, fixture_catalog("notice.gateway.busy_hint_queued"))

        consumer._notify_stale_queued()

        self.assertEqual(texts.texts, [], "结果不明不补发纯文本")
        self.assertNotIn("tsk-stale", consumer._queue_delay_notified, "沿用下一轮重试")

    def test_queued_hint_never_asks_the_user_to_resend(self) -> None:
        """已受理排队 ≠ 未受理：排队提示不得让用户重新发送。"""
        catalog = fixture_catalog("notice.gateway.busy_hint_queued")
        self.assertNotIn("重新发送", catalog.text("gateway.busy_hint_queued").text)
        template = FIXTURE_CARDS["notice.gateway.busy_hint_queued"]
        self.assertNotIn("重新发送", template["title"] + template["body"])


class _Response:
    def __init__(self, *, code: Any, message_id: str | None = None) -> None:
        self.code = code
        self.msg = "fake"
        self.data = types.SimpleNamespace(message_id=message_id) if message_id else None

    def success(self) -> bool:
        return self.code == 0

    def get_log_id(self) -> str:
        return "log-fake"


class _ReplyClient:
    """只暴露 ``im.v1.message.reply``；按消息类型脚本化响应。"""

    def __init__(self, *, card: Any, text: Any = None) -> None:
        self.requests: list[Any] = []
        self._scripts = {"interactive": card, "text": text or _Response(code=0, message_id="om-t")}
        self.im = types.SimpleNamespace(
            v1=types.SimpleNamespace(message=types.SimpleNamespace(reply=self._reply))
        )

    def _reply(self, request: Any) -> Any:
        self.requests.append(request)
        outcome = self._scripts[request.request_body.msg_type]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def sent(self, msg_type: str) -> list[Any]:
        return [r for r in self.requests if r.request_body.msg_type == msg_type]


def _queued_card(catalog: ContentCatalog) -> NoticeCard:
    texts = _SpyReplyTexts()
    _queued_consumer(texts, catalog)._notify_stale_queued()
    return texts.notices[0]["card"]


class _StubBuilder:
    """``X.builder().a(1).build()`` 链式调用的替身：收集字段，构造成简单对象。"""

    def __init__(self) -> None:
        self._fields: dict[str, Any] = {}

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def collect(value: Any) -> _StubBuilder:
            self._fields[name] = value
            return self

        return collect

    def build(self) -> Any:
        return types.SimpleNamespace(**self._fields)


class _StubModel:
    @classmethod
    def builder(cls) -> _StubBuilder:
        return _StubBuilder()


def _install_stub_im_sdk(test_case: unittest.TestCase) -> None:
    """把回复消息用到的 ``lark_oapi.api.im.v1`` 换成桩（快检环境不装飞书 SDK）。"""
    im_v1 = types.ModuleType("lark_oapi.api.im.v1")
    im_v1.ReplyMessageRequest = _StubModel
    im_v1.ReplyMessageRequestBody = _StubModel
    im = types.ModuleType("lark_oapi.api.im")
    im.v1 = im_v1
    api = types.ModuleType("lark_oapi.api")
    api.im = im
    root = types.ModuleType("lark_oapi")
    root.api = api
    modules = {
        "lark_oapi": root,
        "lark_oapi.api": api,
        "lark_oapi.api.im": im,
        "lark_oapi.api.im.v1": im_v1,
    }
    saved = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)

    def restore() -> None:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    test_case.addCleanup(restore)


class ReplyNoticeAdapterTests(unittest.TestCase):
    """``LarkDeliveryText.send_notice``：同话题回复一张通知卡。"""

    def setUp(self) -> None:
        _install_stub_im_sdk(self)
        self.card = _queued_card(fixture_catalog("notice.gateway.busy_hint_queued"))
        self.target = {"chat_id": "chat-1", "thread_id": "topic-1", "reply_to_message_id": "m-1"}

    def test_accepted_card_is_one_interactive_reply_without_actions(self) -> None:
        client = _ReplyClient(card=_Response(code=0, message_id="om-card"))

        message_id = LarkDeliveryText(client).send_notice(**self.target, card=self.card)

        self.assertEqual(message_id, "om-card")
        self.assertEqual(len(client.requests), 1)
        request = client.requests[0]
        self.assertEqual(request.message_id, "m-1")
        self.assertTrue(request.request_body.reply_in_thread)
        payload = json.loads(request.request_body.content)
        self.assertEqual(payload, self.card.to_payload())
        self.assertNotIn("button", request.request_body.content)

    def test_definite_rejection_falls_back_to_text_exactly_once(self) -> None:
        client = _ReplyClient(card=_Response(code=230099))

        message_id = LarkDeliveryText(client).send_notice(**self.target, card=self.card)

        self.assertEqual(message_id, "om-t")
        texts = client.sent("text")
        self.assertEqual(len(texts), 1)
        self.assertEqual(
            json.loads(texts[0].request_body.content), {"text": self.card.fallback_text}
        )

    def test_uncertain_result_raises_and_sends_no_text(self) -> None:
        for outcome in (_Response(code=None), TimeoutError("timeout")):
            with self.subTest(outcome=type(outcome).__name__):
                client = _ReplyClient(card=outcome)
                with self.assertRaises((DeliveryUncertainError, TimeoutError)):
                    LarkDeliveryText(client).send_notice(**self.target, card=self.card)
                self.assertEqual(client.sent("text"), [], "结果不明不补发")

    def test_send_text_request_shape_is_unchanged(self) -> None:
        client = _ReplyClient(card=None)

        LarkDeliveryText(client).send_text(**self.target, text="你好")

        request = client.requests[0]
        self.assertEqual(request.request_body.msg_type, "text")
        self.assertEqual(json.loads(request.request_body.content), {"text": "你好"})
        self.assertTrue(request.request_body.reply_in_thread)


class QueuedHintEndToEndTests(unittest.TestCase):
    def test_rejected_card_then_text_marks_the_task_notified_once(self) -> None:
        _install_stub_im_sdk(self)
        client = _ReplyClient(card=_Response(code=230099))
        consumer = _queued_consumer(
            LarkDeliveryText(client), fixture_catalog("notice.gateway.busy_hint_queued")
        )

        consumer._notify_stale_queued()
        consumer._notify_stale_queued()

        self.assertEqual(len(client.sent("interactive")), 1)
        self.assertEqual(len(client.sent("text")), 1, "回落只一次，下一轮不再重发")


# ----------------------------------------------------------------------------------
# 流式问数卡（#8）
# ----------------------------------------------------------------------------------


class _Cards:
    def __init__(self, *, update_error: Exception | None = None) -> None:
        self.frames: list[tuple[str, int, Any]] = []
        self._update_error = update_error

    def create(self, *, chat_id, thread_id, reply_to_message_id, card) -> CardCreated:
        self.frames.append(("create", 0, card))
        return CardCreated(card_id="card-1", message_id="om-card-1")

    def update(self, *, card_id: str, sequence: int, card: Any) -> None:
        if self._update_error is not None:
            raise self._update_error
        self.frames.append(("update", sequence, card))

    def close(self, *, card_id: str, sequence: int, card: Any) -> None:
        self.frames.append(("close", sequence, card))


class _FallbackTexts:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def send_text(self, *, chat_id, thread_id, reply_to_message_id, text: str) -> str:
        self.calls.append(text)
        return "om-text-1"


def _stream(cards: _Cards, texts: _FallbackTexts, catalog: ContentCatalog) -> CardStream:
    clock = iter(float(n) for n in range(0, 10_000, 5))
    return CardStream(
        chat_id="chat-1",
        thread_id="topic-1",
        reply_to_message_id="m-1",
        transport=cards,
        fallback=texts,
        catalog=catalog,
        monotonic=lambda: next(clock),
    )


def _markdown(card: Any) -> str:
    return _card_payload(card)["body"]["elements"][0]["content"]


class StreamingCardTerminalTests(unittest.TestCase):
    def test_terminal_frame_updates_title_and_body_in_place_without_header(self) -> None:
        cards, texts = _Cards(), _FallbackTexts()
        stream = _stream(cards, texts, fixture_catalog())
        stream.start()
        stream.update(elapsed_seconds=3, action="composing")
        stream.finish(result="本月销售额 100 万元", elapsed_seconds=8)

        kinds = [frame[0] for frame in cards.frames]
        self.assertEqual(kinds, ["create", "update", "update", "close"])
        sequences = [frame[1] for frame in cards.frames[1:]]
        self.assertEqual(sequences, sorted(set(sequences)), "序号严格递增")
        final = _markdown(cards.frames[2][2])
        self.assertTrue(final.startswith("**查询完成**"))
        self.assertNotIn("正在查询", final)
        self.assertIn("本月销售额 100 万元", final)
        self.assertNotIn("header", _card_payload(cards.frames[2][2]), "不加卡片标题栏")
        self.assertEqual(texts.calls, [])

    def test_failure_terminal_title_changes_and_rejection_falls_back_once(self) -> None:
        cards, texts = _Cards(), _FallbackTexts()
        catalog = fixture_catalog()
        stream = _stream(cards, texts, catalog)
        stream.start()
        failure = catalog.text("worker.failed")
        stream.finish(failure=failure, elapsed_seconds=5)
        self.assertTrue(_markdown(cards.frames[-2][2]).startswith("**查询未完成**"))

        rejected = _Cards(update_error=DeliveryRejectedError(code=230099))
        texts = _FallbackTexts()
        stream = _stream(rejected, texts, catalog)
        stream.start()
        stream.finish(failure=failure, elapsed_seconds=5)
        self.assertTrue(stream.fallback_needed)
        stream.send_fallback(failure)
        self.assertEqual(texts.calls, [failure.text], "卡片失败回落同一结果、只一次")

    def test_uncertain_terminal_update_raises_and_sends_no_text(self) -> None:
        cards = _Cards(update_error=DeliveryUncertainError(reason="missing_code"))
        texts = _FallbackTexts()
        stream = _stream(cards, texts, fixture_catalog())
        stream.start()
        with self.assertRaises(DeliveryUncertainError):
            stream.finish(result="结果", elapsed_seconds=5)
        self.assertIsNone(stream.send_fallback(fixture_catalog().text("worker.failed")))
        self.assertEqual(texts.calls, [])


class StreamingCardHandoffTests(unittest.TestCase):
    def _handoff_frame(self, catalog: ContentCatalog) -> str:
        cards = _Cards()
        stream = _stream(cards, _FallbackTexts(), catalog)
        stream.start()
        stream.update(elapsed_seconds=600)
        self.assertTrue(stream.fallback_needed)
        return _markdown(cards.frames[-1][2])

    def test_without_card_key_the_handoff_frame_is_unchanged(self) -> None:
        catalog = fixture_catalog()
        expected = catalog.card(
            "query.status", status=catalog.text("worker.card_handoff_notice").text
        )
        self.assertEqual(self._handoff_frame(catalog), _markdown(expected))

    def test_official_catalog_handoff_frame_keeps_the_original_sentence(self) -> None:
        catalog = default_content_catalog()
        frame = self._handoff_frame(catalog)
        self.assertTrue(frame.startswith("**结果将以新消息发送**"))
        self.assertIn(catalog.text("worker.card_handoff_notice").text, frame)
        self.assertNotIn("正在查询", frame)

    def test_with_card_key_the_handoff_frame_no_longer_says_querying(self) -> None:
        frame = self._handoff_frame(fixture_catalog("notice.worker.card_handoff_notice"))
        self.assertTrue(frame.startswith("**结果将以新消息发送**"))
        self.assertNotIn("正在查询", frame)


if __name__ == "__main__":
    unittest.main()
