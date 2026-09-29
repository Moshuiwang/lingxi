"""群消息与主动私聊两个出口的 ``send_notice``：先发卡片，明确拒绝才补发一次纯文本。

断言跑在注入的假传输层上，真实送达属预发。否定面：

- 飞书明确拒绝卡片 → 纯文本**恰好补发一次**，且用原有文本去重前缀；
- 结果不明（传输异常、缺码、缺回读标识） → **不补发**，按现有语义上抛；
- 同一去重键重试 → 卡片去重 ID 不变，服务端只留**一条**；
- 卡片与文本的去重 ID **不同**，各自在飞书 50 字符上限内；
- ``send_text`` 的请求形状与改动前**逐字相同**。
"""

from __future__ import annotations

import json
import unittest
from dataclasses import replace

from lingxi.adapters.feishu_group_message import (
    DAILY_REPORT_UUID_PREFIX,
    DELIVERY_UUID_MAX_LENGTH,
    DELIVERY_UUID_PREFIX,
    FeishuGroupMessageError,
    FeishuGroupMessages,
    card_uuid_prefix_for,
    delivery_uuid,
    with_group_prefix,
)
from lingxi.adapters.feishu_user_message import (
    NOTICE_UUID_PREFIX,
    FeishuUserMessageError,
    FeishuUserMessages,
)
from lingxi.core.delivery.notice_card import NoticeCard, NoticeTone

BASE_URL = "https://open.feishu.cn/open-apis"
CHAT_ID = "oc_fake_chat_for_tests"
OPEN_ID = "ou_fake_open_id_for_tests"
CARD = NoticeCard(
    title="内测每日通报",
    tone=NoticeTone.DONE,
    sections=[("概况", ("今日新开通 2 人。",))],
    fallback_text="内测每日通报\n概况：今日新开通 2 人。",
)
ACCEPTED = {"code": 0, "data": {"message_id": "om-fake"}}
REJECTED = {"code": 230099, "msg": "card rejected"}


class TransportTimeoutError(Exception):
    """模拟传输层超时：请求可能已被服务端收下，调用方听不到回音。"""


class FakeFeishu:
    """按消息类型脚本化响应，并像飞书服务端一样按 ``uuid`` 去重计数已送达消息。"""

    def __init__(self, *, card=ACCEPTED, text=ACCEPTED) -> None:
        self.calls: list[dict] = []
        self.delivered_uuids: set[str] = set()
        self._scripts = {"interactive": list(card) if isinstance(card, list) else [card]}
        self._scripts["text"] = list(text) if isinstance(text, list) else [text]

    def __call__(self, method, url, *, body=None, token=None, **kwargs):
        if "tenant_access_token" in url:
            return {"code": 0, "tenant_access_token": "t-fake"}
        self.calls.append({"url": url, "body": body})
        script = self._scripts[body["msg_type"]]
        step = script.pop(0) if len(script) > 1 else script[0]
        if step == "deliver_then_timeout":
            self.delivered_uuids.add(body["uuid"])
            raise TransportTimeoutError("timeout")
        if isinstance(step, BaseException):
            raise step
        if step.get("code") == 0:
            self.delivered_uuids.add(body["uuid"])
        return step

    def sent(self, msg_type: str) -> list[dict]:
        return [call["body"] for call in self.calls if call["body"]["msg_type"] == msg_type]


def _group(fake: FakeFeishu, *, prefix: str = DAILY_REPORT_UUID_PREFIX, outcomes=None):
    return FeishuGroupMessages(
        base_url=BASE_URL,
        app_id="cli_fake",
        app_secret="fake-secret",
        transport=fake,
        uuid_prefix=prefix,
        on_send_outcome=None if outcomes is None else lambda op, ok: outcomes.append((op, ok)),
    )


def _user(fake: FakeFeishu) -> FeishuUserMessages:
    return FeishuUserMessages(
        base_url=BASE_URL, app_id="cli_fake", app_secret="fake-secret", transport=fake
    )


def _send_group(fake: FakeFeishu, **kwargs) -> None:
    _group(fake, **kwargs).send_notice(chat_id=CHAT_ID, card=CARD, dedupe_key="2026-09-27")


def _send_user(fake: FakeFeishu) -> None:
    _user(fake).send_notice(open_id=OPEN_ID, card=CARD, dedupe_key="perm-change-1")


class CardUuidPrefixTest(unittest.TestCase):
    def test_every_existing_text_prefix_derives_a_distinct_card_prefix_within_limit(self) -> None:
        prefixes = (
            DELIVERY_UUID_PREFIX,
            DAILY_REPORT_UUID_PREFIX,
            NOTICE_UUID_PREFIX,
            "lingxi-perm-fix-",
            "lingxi-unreach-",
            "lingxi-content-",
            "lingxi-admin-",
            "lingxi-doc-ready-",
        )
        derived = {card_uuid_prefix_for(prefix) for prefix in prefixes}
        self.assertEqual(len(derived), len(prefixes))
        self.assertTrue(derived.isdisjoint(prefixes))
        for prefix in derived:
            self.assertLessEqual(
                len(delivery_uuid(CHAT_ID, "k", prefix=prefix)), DELIVERY_UUID_MAX_LENGTH
            )


class GroupSendNoticeTest(unittest.TestCase):
    def test_accepted_card_sends_no_text(self) -> None:
        fake = FakeFeishu()
        outcomes: list = []
        _send_group(fake, outcomes=outcomes)
        [card_body] = fake.sent("interactive")
        self.assertEqual(fake.sent("text"), [])
        self.assertEqual(
            json.loads(card_body["content"]),
            replace(CARD, title=with_group_prefix(CARD.title)).to_payload(),
        )
        self.assertEqual(
            card_body["uuid"],
            delivery_uuid(
                CHAT_ID, "2026-09-27", prefix=card_uuid_prefix_for(DAILY_REPORT_UUID_PREFIX)
            ),
        )
        self.assertEqual(outcomes, [("message_final", True)])

    def test_definite_rejection_falls_back_to_text_exactly_once_with_text_prefix(self) -> None:
        fake = FakeFeishu(card=REJECTED)
        outcomes: list = []
        _send_group(fake, outcomes=outcomes)
        self.assertEqual(len(fake.sent("interactive")), 1)
        [text_body] = fake.sent("text")
        self.assertEqual(
            json.loads(text_body["content"]), {"text": with_group_prefix(CARD.fallback_text)}
        )
        self.assertEqual(
            text_body["uuid"], delivery_uuid(CHAT_ID, "2026-09-27", prefix=DAILY_REPORT_UUID_PREFIX)
        )
        self.assertEqual(outcomes, [("message_final", True)])

    def test_rejected_fallback_raises_definite_and_is_not_retried(self) -> None:
        fake = FakeFeishu(card=REJECTED, text=REJECTED)
        outcomes: list = []
        with self.assertRaises(FeishuGroupMessageError) as caught:
            _send_group(fake, outcomes=outcomes)
        self.assertTrue(caught.exception.definite)
        self.assertEqual((len(fake.sent("interactive")), len(fake.sent("text"))), (1, 1))
        self.assertEqual(outcomes, [("message_final", False)])

    def test_uncertain_card_result_never_falls_back(self) -> None:
        for step in (TransportTimeoutError("t"), {}, {"code": 0, "data": {}}):
            with self.subTest(step=step):
                fake = FakeFeishu(card=step)
                outcomes: list = []
                with self.assertRaises(Exception) as caught:
                    _send_group(fake, outcomes=outcomes)
                self.assertFalse(getattr(caught.exception, "definite", False))
                self.assertEqual(fake.sent("text"), [])
                self.assertEqual(outcomes, [("message_final", False)])

    def test_retry_with_same_dedupe_key_leaves_one_message(self) -> None:
        fake = FakeFeishu(card=["deliver_then_timeout", ACCEPTED])
        messages = _group(fake)
        with self.assertRaises(TransportTimeoutError):
            messages.send_notice(chat_id=CHAT_ID, card=CARD, dedupe_key="2026-09-27")
        messages.send_notice(chat_id=CHAT_ID, card=CARD, dedupe_key="2026-09-27")
        uuids = [body["uuid"] for body in fake.sent("interactive")]
        self.assertEqual(len(uuids), 2)
        self.assertEqual(len(set(uuids)), 1)
        self.assertEqual(len(fake.delivered_uuids), 1)
        self.assertEqual(fake.sent("text"), [])

    def test_send_text_request_shape_is_unchanged(self) -> None:
        fake = FakeFeishu()
        _group(fake, prefix=DELIVERY_UUID_PREFIX).send_text(
            chat_id=CHAT_ID, text="日报", dedupe_key="d"
        )
        [body] = fake.sent("text")
        self.assertEqual(
            body,
            {
                "receive_id": CHAT_ID,
                "msg_type": "text",
                "content": json.dumps({"text": "[lingxi] 日报"}, ensure_ascii=False),
                "uuid": delivery_uuid(CHAT_ID, "d", prefix=DELIVERY_UUID_PREFIX),
            },
        )


class UserSendNoticeTest(unittest.TestCase):
    def test_accepted_card_sends_no_text(self) -> None:
        fake = FakeFeishu()
        _send_user(fake)
        [card_body] = fake.sent("interactive")
        self.assertEqual(fake.sent("text"), [])
        self.assertIn("receive_id_type=open_id", fake.calls[0]["url"])
        self.assertEqual(
            card_body["uuid"],
            delivery_uuid(
                OPEN_ID, "perm-change-1", prefix=card_uuid_prefix_for(NOTICE_UUID_PREFIX)
            ),
        )

    def test_definite_rejection_falls_back_to_text_exactly_once_with_text_prefix(self) -> None:
        fake = FakeFeishu(card=REJECTED)
        _send_user(fake)
        self.assertEqual(len(fake.sent("interactive")), 1)
        [text_body] = fake.sent("text")
        self.assertEqual(json.loads(text_body["content"]), {"text": CARD.fallback_text})
        self.assertEqual(
            text_body["uuid"], delivery_uuid(OPEN_ID, "perm-change-1", prefix=NOTICE_UUID_PREFIX)
        )

    def test_uncertain_card_result_never_falls_back(self) -> None:
        for step in (FeishuUserMessageError("transport_error"), {}, {"code": 0, "data": {}}):
            with self.subTest(step=step):
                fake = FakeFeishu(card=step)
                with self.assertRaises(FeishuUserMessageError) as caught:
                    _send_user(fake)
                self.assertFalse(caught.exception.definite)
                self.assertEqual(fake.sent("text"), [])

    def test_retry_with_same_dedupe_key_leaves_one_message(self) -> None:
        fake = FakeFeishu(card=["deliver_then_timeout", ACCEPTED])
        messages = _user(fake)
        with self.assertRaises(TransportTimeoutError):
            messages.send_notice(open_id=OPEN_ID, card=CARD, dedupe_key="perm-change-1")
        messages.send_notice(open_id=OPEN_ID, card=CARD, dedupe_key="perm-change-1")
        self.assertEqual(len({body["uuid"] for body in fake.sent("interactive")}), 1)
        self.assertEqual(len(fake.delivered_uuids), 1)

    def test_group_chat_id_is_rejected_before_any_send(self) -> None:
        fake = FakeFeishu()
        with self.assertRaises(ValueError):
            _user(fake).send_notice(open_id=CHAT_ID, card=CARD, dedupe_key="k")
        self.assertEqual(fake.calls, [])

    def test_non_notice_card_is_rejected_before_any_send(self) -> None:
        fake = FakeFeishu()
        with self.assertRaises(TypeError):
            _user(fake).send_notice(open_id=OPEN_ID, card={"schema": "2.0"}, dedupe_key="k")
        with self.assertRaises(TypeError):
            _group(fake).send_notice(chat_id=CHAT_ID, card={"schema": "2.0"}, dedupe_key="k")
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
