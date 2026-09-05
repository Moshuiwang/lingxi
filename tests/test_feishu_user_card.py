"""面向用户 open_id 的卡片出站适配器（Issue #586）。

**不认领 `V-*` 断言**：真实私聊卡片渲染、平台能否向从未对话的用户主动发起私聊
（Wave 0 G-1，仍未验证）属 L4a；本文件全部断言跑在注入的假传输层上，钉的是
**代码事实**：发到哪个接口、``msg_type`` 是什么、卡片以 JSON 字符串进 ``content``、
重试是不是带同一个 uuid、失败怎么分类、以及凭据与外部标识不进 URL、日志与异常。

否定面：群 ``chat_id`` **发不进来**；空卡片、空去重键**发不出去**；``app_secret``
**不进 URL**；去重 ``uuid`` 前缀与权限通知**不同**且不超平台上限。
"""

from __future__ import annotations

import json
import unittest

from lingxi.adapters.feishu_group_message import DELIVERY_UUID_MAX_LENGTH, delivery_uuid
from lingxi.adapters.feishu_user_card import (
    OUTREACH_UUID_PREFIX,
    FeishuUserCardError,
    FeishuUserCards,
)
from lingxi.adapters.feishu_user_message import NOTICE_UUID_PREFIX, FeishuUserMessageError

BASE_URL = "https://open.feishu.cn/open-apis"
OPEN_ID = "ou_fake_open_id_for_tests"
GROUP_ID = "oc_fake_group_id_for_tests"
SECRET = "fake-app-secret-for-tests"
CARD = {
    "schema": "2.0",
    "config": {"update_multi": True},
    "header": {"title": {"tag": "plain_text", "content": "BI Plus"}},
    "body": {"elements": [{"tag": "markdown", "content": "你好。"}]},
}
DEDUPE = "outreach.welcome:apply:usr_fake"


class FakeTransport:
    """按调用次序返回脚本；记录全部请求供断言。"""

    def __init__(self, *, send_response=None, error=None) -> None:
        self.calls: list[dict] = []
        self._send_response = (
            send_response
            if send_response is not None
            else {"code": 0, "data": {"message_id": "om_x"}}
        )
        self._error = error

    def __call__(self, method, url, *, body=None, token=None, **kwargs):
        self.calls.append({"method": method, "url": url, "body": body, "token": token})
        if "tenant_access_token" in url:
            return {"code": 0, "tenant_access_token": "t-fake"}
        if self._error is not None:
            raise self._error
        return self._send_response

    @property
    def send_call(self) -> dict:
        return self.calls[-1]


def _cards(transport: FakeTransport) -> FeishuUserCards:
    return FeishuUserCards(
        base_url=BASE_URL, app_id="cli_fake", app_secret=SECRET, transport=transport
    )


class ProtocolShapeTest(unittest.TestCase):
    def test_the_card_goes_to_the_message_endpoint_as_an_interactive_message(self) -> None:
        transport = FakeTransport()
        message_id = _cards(transport).send_card(open_id=OPEN_ID, card=CARD, dedupe_key=DEDUPE)
        call = transport.send_call
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], f"{BASE_URL}/im/v1/messages?receive_id_type=open_id")
        self.assertEqual(call["body"]["msg_type"], "interactive")
        self.assertEqual(call["body"]["receive_id"], OPEN_ID)
        self.assertEqual(message_id, "om_x")

    def test_the_card_is_carried_as_a_json_string_not_an_object(self) -> None:
        """飞书把 ``content`` 定义成一段 JSON 字符串；传对象会被拒。"""
        transport = FakeTransport()
        _cards(transport).send_card(open_id=OPEN_ID, card=CARD, dedupe_key=DEDUPE)
        content = transport.send_call["body"]["content"]
        self.assertIsInstance(content, str)
        self.assertEqual(json.loads(content), CARD)

    def test_a_response_without_a_message_id_yields_none_rather_than_a_fake_one(self) -> None:
        transport = FakeTransport(send_response={"code": 0})
        self.assertIsNone(
            _cards(transport).send_card(open_id=OPEN_ID, card=CARD, dedupe_key=DEDUPE)
        )

    def test_the_app_secret_never_appears_in_a_url(self) -> None:
        transport = FakeTransport()
        _cards(transport).send_card(open_id=OPEN_ID, card=CARD, dedupe_key=DEDUPE)
        for call in transport.calls:
            self.assertNotIn(SECRET, call["url"])
        self.assertEqual(transport.calls[0]["body"]["app_secret"], SECRET)


class RecipientShapeTest(unittest.TestCase):
    def test_a_group_chat_id_is_rejected_before_anything_is_sent(self) -> None:
        """否定断言：收件人错位在发出去之前就失败。

        群 ID 与用户 open_id 都是字符串，混用不会有任何报错——只会把一个人的权限
        范围发进一个群。
        """
        transport = FakeTransport()
        with self.assertRaises(ValueError):
            _cards(transport).send_card(open_id=GROUP_ID, card=CARD, dedupe_key=DEDUPE)
        self.assertEqual(transport.calls, [])

    def test_an_empty_card_is_refused(self) -> None:
        transport = FakeTransport()
        with self.assertRaises(ValueError):
            _cards(transport).send_card(open_id=OPEN_ID, card={}, dedupe_key=DEDUPE)
        self.assertEqual(transport.calls, [])

    def test_an_empty_dedupe_key_is_refused(self) -> None:
        """忘了传去重键等于回到"结果不明时必然重复投递"。"""
        transport = FakeTransport()
        with self.assertRaises(ValueError):
            _cards(transport).send_card(open_id=OPEN_ID, card=CARD, dedupe_key="  ")
        self.assertEqual(transport.calls, [])


class DedupeUuidTest(unittest.TestCase):
    def test_a_retry_carries_the_very_same_uuid(self) -> None:
        transport = FakeTransport()
        cards = _cards(transport)
        cards.send_card(open_id=OPEN_ID, card=CARD, dedupe_key=DEDUPE)
        first = transport.send_call["body"]["uuid"]
        cards.send_card(open_id=OPEN_ID, card=CARD, dedupe_key=DEDUPE)
        self.assertEqual(first, transport.send_call["body"]["uuid"])

    def test_the_prefix_differs_from_the_permission_notice_chain(self) -> None:
        """两条投递语义共用一个 uuid 命名空间，运维在飞书侧就分不出谁是谁。"""
        self.assertNotEqual(OUTREACH_UUID_PREFIX, NOTICE_UUID_PREFIX)

    def test_the_uuid_stays_inside_the_platform_limit(self) -> None:
        value = delivery_uuid(OPEN_ID, DEDUPE, prefix=OUTREACH_UUID_PREFIX)
        self.assertTrue(value.startswith(OUTREACH_UUID_PREFIX))
        self.assertLessEqual(len(value), DELIVERY_UUID_MAX_LENGTH)


class FailureClassificationTest(unittest.TestCase):
    def test_a_business_error_code_is_a_definite_rejection(self) -> None:
        transport = FakeTransport(send_response={"code": 230013, "msg": "no permission"})
        with self.assertRaises(FeishuUserCardError) as caught:
            _cards(transport).send_card(open_id=OPEN_ID, card=CARD, dedupe_key=DEDUPE)
        self.assertEqual(caught.exception.code, "feishu_code_230013")
        self.assertTrue(caught.exception.definite)

    def test_a_transport_error_is_translated_but_keeps_its_code(self) -> None:
        """共用的无重定向传输层抛的异常在模块边界翻译，错误码逐字保留。"""
        transport = FakeTransport(error=FeishuUserMessageError("transport_error"))
        with self.assertRaises(FeishuUserCardError) as caught:
            _cards(transport).send_card(open_id=OPEN_ID, card=CARD, dedupe_key=DEDUPE)
        self.assertEqual(caught.exception.code, "transport_error")
        self.assertFalse(caught.exception.definite)

    def test_an_error_message_leaks_neither_the_recipient_nor_the_card(self) -> None:
        transport = FakeTransport(send_response={"code": 230013})
        with self.assertRaises(FeishuUserCardError) as caught:
            _cards(transport).send_card(open_id=OPEN_ID, card=CARD, dedupe_key=DEDUPE)
        message = str(caught.exception)
        self.assertNotIn(OPEN_ID, message)
        self.assertNotIn("你好", message)
        self.assertNotIn(SECRET, message)

    def test_a_non_https_base_url_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            FeishuUserCards(
                base_url="http://open.feishu.cn/open-apis", app_id="cli_fake", app_secret=SECRET
            )


if __name__ == "__main__":
    unittest.main()
