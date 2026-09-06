"""向用户本人主动发文本消息的适配器（Issue #156 / S-C-03b）。

**不认领 `V-*` 断言**：真实私聊行为（应用能不能主动给一个从未与它对话过的用户发消息、
平台的 uuid 去重窗口有多长）属 L4a，本文件全部断言跑在注入的假传输层上。这里钉的是
**代码事实**：发到哪个接口、带哪些字段、重试是不是携带同一个 uuid、失败怎么分类、
以及凭据与外部标识不进日志与异常消息。

否定面：

- **群 ``chat_id`` 发不进来**（收件人错位在发出去之前就失败）；
- 空正文、空去重键**发不出去**；
- ``app_secret`` **不进 URL**，只进请求体；
- 错误消息里**不出现** open_id、令牌与正文；
- 去重 ``uuid`` 的前缀与日报**不同**，且长度在飞书的 50 字符上限内。
"""

from __future__ import annotations

import unittest

from lingxi.adapters.feishu_group_message import DELIVERY_UUID_MAX_LENGTH, delivery_uuid
from lingxi.adapters.feishu_user_message import (
    NOTICE_UUID_PREFIX,
    FeishuUserMessageError,
    FeishuUserMessages,
    validate_user_open_id,
)

BASE_URL = "https://open.feishu.cn/open-apis"
OPEN_ID = "ou_fake_open_id_for_tests"
SECRET = "fake-app-secret-for-tests"
TEXT = "你的数据查询可用范围已更新，当前暂无可用的数据范围。如有疑问请联系管理员。"


class FakeTransport:
    """按调用次序返回脚本；记录全部请求供断言。"""

    def __init__(self, *, token_response=None, send_response=None, error=None) -> None:
        self.calls: list[dict] = []
        self._token_response = token_response or {"code": 0, "tenant_access_token": "t-fake"}
        # 真实成功响应带 ``data.message_id``：这是「通知」这一档的必要回读标识
        # （见 ``core.delivery.ports.DELIVERY_OPERATIONS``），缺它一律判"结果不明"。
        self._send_response = (
            send_response
            if send_response is not None
            else {"code": 0, "data": {"message_id": "om-fake-1"}}
        )
        self._error = error

    def __call__(self, method, url, *, body=None, token=None, **kwargs):
        self.calls.append({"method": method, "url": url, "body": body, "token": token})
        if "tenant_access_token" in url:
            return self._token_response
        if self._error is not None:
            raise self._error
        return self._send_response

    @property
    def send_call(self) -> dict:
        return self.calls[-1]


def _messages(transport: FakeTransport) -> FeishuUserMessages:
    return FeishuUserMessages(
        base_url=BASE_URL, app_id="cli_fake", app_secret=SECRET, transport=transport
    )


class OpenIdValidationTest(unittest.TestCase):
    def test_a_user_open_id_passes(self) -> None:
        self.assertEqual(validate_user_open_id(f"  {OPEN_ID} "), OPEN_ID)

    def test_a_group_chat_id_is_rejected(self) -> None:
        """否定断言：**收件人错位在发出去之前就失败**。

        群 ID 与用户 open_id 都是字符串，混用不会有任何报错——只会把一个人的权限范围
        发进一个群。
        """

        with self.assertRaises(ValueError) as caught:
            validate_user_open_id("oc_some_group")
        self.assertNotIn("oc_some_group", str(caught.exception), "不回显收到的值")

    def test_blank_and_whitespace_are_rejected(self) -> None:
        for value in ("", "   ", "ou_", "ou_with space"):
            with self.subTest(value):
                with self.assertRaises(ValueError):
                    validate_user_open_id(value)


class SendTest(unittest.TestCase):
    def test_the_message_goes_to_the_open_id_endpoint(self) -> None:
        transport = FakeTransport()

        _messages(transport).send_text(open_id=OPEN_ID, text=TEXT, dedupe_key="usr_1:3")

        call = transport.send_call
        self.assertEqual(call["url"], f"{BASE_URL}/im/v1/messages?receive_id_type=open_id")
        self.assertEqual(call["body"]["receive_id"], OPEN_ID)
        self.assertEqual(call["body"]["msg_type"], "text")
        self.assertEqual(call["body"]["content"], '{"text": "%s"}' % TEXT)
        self.assertEqual(call["token"], "t-fake")

    def test_only_text_is_supported(self) -> None:
        """否定断言：签名里根本没有卡片——权限通知不带任何可执行入口。"""

        import inspect

        parameters = set(inspect.signature(FeishuUserMessages.send_text).parameters)
        self.assertEqual(parameters, {"self", "open_id", "text", "dedupe_key"})

    def test_the_app_secret_never_reaches_the_url(self) -> None:
        transport = FakeTransport()

        _messages(transport).send_text(open_id=OPEN_ID, text=TEXT, dedupe_key="usr_1:3")

        for call in transport.calls:
            self.assertNotIn(SECRET, call["url"])
        self.assertEqual(transport.calls[0]["body"]["app_secret"], SECRET)

    def test_the_same_dedupe_key_yields_the_same_uuid(self) -> None:
        first, second = FakeTransport(), FakeTransport()

        _messages(first).send_text(open_id=OPEN_ID, text=TEXT, dedupe_key="usr_1:3")
        _messages(second).send_text(open_id=OPEN_ID, text="别的正文", dedupe_key="usr_1:3")

        self.assertEqual(first.send_call["body"]["uuid"], second.send_call["body"]["uuid"])

    def test_a_new_version_yields_a_new_uuid(self) -> None:
        first, second = FakeTransport(), FakeTransport()

        _messages(first).send_text(open_id=OPEN_ID, text=TEXT, dedupe_key="usr_1:3")
        _messages(second).send_text(open_id=OPEN_ID, text=TEXT, dedupe_key="usr_1:4")

        self.assertNotEqual(first.send_call["body"]["uuid"], second.send_call["body"]["uuid"])

    def test_the_uuid_namespace_is_separate_from_the_daily_report(self) -> None:
        transport = FakeTransport()

        _messages(transport).send_text(open_id=OPEN_ID, text=TEXT, dedupe_key="usr_1:3")

        value = transport.send_call["body"]["uuid"]
        self.assertTrue(value.startswith(NOTICE_UUID_PREFIX))
        self.assertNotEqual(value, delivery_uuid(OPEN_ID, "usr_1:3"))
        self.assertLessEqual(len(value), DELIVERY_UUID_MAX_LENGTH)

    def test_an_over_long_prefix_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            delivery_uuid(OPEN_ID, "k", prefix="x" * 20)

    def test_sending_to_a_group_chat_id_fails_before_any_request(self) -> None:
        """否定断言：**收件人错位在发出去之前就失败**，一次请求都不发。

        校验必须在 ``send_text`` 这条真实路径上，而不是只在校验函数上——只测函数的话，
        有人把发送里那一行校验拿掉，用例照样是绿的。
        """

        transport = FakeTransport()

        with self.assertRaises(ValueError):
            _messages(transport).send_text(open_id="oc_admin_group", text=TEXT, dedupe_key="k")

        self.assertEqual(transport.calls, [], "连取令牌都不发")

    def test_an_empty_body_is_rejected(self) -> None:
        transport = FakeTransport()

        for text in ("", "   "):
            with self.subTest(text):
                with self.assertRaises(ValueError):
                    _messages(transport).send_text(open_id=OPEN_ID, text=text, dedupe_key="k")

    def test_an_empty_dedupe_key_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _messages(FakeTransport()).send_text(open_id=OPEN_ID, text=TEXT, dedupe_key="  ")

    def test_a_business_error_code_is_definite(self) -> None:
        transport = FakeTransport(send_response={"code": 230002, "msg": "x"})

        with self.assertRaises(FeishuUserMessageError) as caught:
            _messages(transport).send_text(open_id=OPEN_ID, text=TEXT, dedupe_key="k")

        self.assertEqual(caught.exception.code, "feishu_code_230002")
        self.assertTrue(caught.exception.definite)

    def test_an_empty_response_body_is_not_success_and_not_a_definite_rejection(self) -> None:
        """IN-03：飞书只回一个空体（``{}``、HTTP 5xx 带空体）。

        旧写法 ``if code not in (None, 0, "0")`` 把**码缺失**当成放行——调用方
        据此把一条从未发出的通知记成已发送。真实成功响应一定带 ``code=0``，
        码缺失既不是成功也不是拒绝：必须落"结果不明"（``definite=False``），
        让调用方按同一 ``dedupe_key`` 安全重投，而不是当成"已经发过了"。
        """

        transport = FakeTransport(send_response={})

        with self.assertRaises(FeishuUserMessageError) as caught:
            _messages(transport).send_text(open_id=OPEN_ID, text=TEXT, dedupe_key="k")

        self.assertEqual(caught.exception.code, "missing_code")
        self.assertFalse(caught.exception.definite, "码缺失不是飞书明确拒绝")

    def test_a_response_without_message_id_is_result_unknown(self) -> None:
        """码成功但缺必要回读标识 ``message_id``：同样不得当成已发送。"""

        for response in ({"code": 0}, {"code": 0, "data": {}}, {"code": 0, "data": {"x": 1}}):
            with self.subTest(response=response):
                transport = FakeTransport(send_response=response)
                with self.assertRaises(FeishuUserMessageError) as caught:
                    _messages(transport).send_text(open_id=OPEN_ID, text=TEXT, dedupe_key="k")
                self.assertEqual(caught.exception.code, "missing_message_id")
                self.assertFalse(caught.exception.definite)

    def test_a_transport_failure_is_indeterminate(self) -> None:
        error = FeishuUserMessageError("transport_error", definite=False)
        transport = FakeTransport(error=error)

        with self.assertRaises(FeishuUserMessageError) as caught:
            _messages(transport).send_text(open_id=OPEN_ID, text=TEXT, dedupe_key="k")

        self.assertFalse(caught.exception.definite)

    def test_a_shapeless_response_is_not_treated_as_success(self) -> None:
        transport = FakeTransport(send_response="不是对象")

        with self.assertRaises(FeishuUserMessageError) as caught:
            _messages(transport).send_text(open_id=OPEN_ID, text=TEXT, dedupe_key="k")

        self.assertEqual(caught.exception.code, "invalid_response_shape")

    def test_a_missing_tenant_token_fails_before_sending(self) -> None:
        transport = FakeTransport(token_response={"code": 0})

        with self.assertRaises(FeishuUserMessageError) as caught:
            _messages(transport).send_text(open_id=OPEN_ID, text=TEXT, dedupe_key="k")

        self.assertEqual(caught.exception.code, "missing_tenant_access_token")
        self.assertEqual(len(transport.calls), 1, "取不到令牌就不发消息")

    def test_no_error_message_carries_the_recipient_or_the_body(self) -> None:
        for response in ({"code": 99, "msg": "x"}, "不是对象"):
            with self.subTest(response):
                transport = FakeTransport(send_response=response)
                with self.assertRaises(FeishuUserMessageError) as caught:
                    _messages(transport).send_text(open_id=OPEN_ID, text=TEXT, dedupe_key="k")
                message = str(caught.exception)
                self.assertNotIn(OPEN_ID, message)
                self.assertNotIn(TEXT, message)
                self.assertNotIn(SECRET, message)

    def test_the_constructor_does_no_io(self) -> None:
        """构造只存参数：不建 client、不发请求、不读凭据文件。"""

        transport = FakeTransport()

        FeishuUserMessages(
            base_url=BASE_URL, app_id="cli_fake", app_secret=SECRET, transport=transport
        )

        self.assertEqual(transport.calls, [])

    def test_a_plain_http_base_url_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            FeishuUserMessages(base_url="http://open.feishu.cn", app_id="a", app_secret="s")

    def test_the_default_transport_does_not_follow_redirects(self) -> None:
        """否定断言：**默认传输不跟随 3xx**——跟随会把应用身份令牌转发到新主机。

        与 ``query_mcp_probe`` 的同型处置一致。这里断言的是装配事实（默认传输就是那个
        不跟随重定向的实现）与它构造出来的 opener 里那个 handler 的行为。
        """

        import inspect

        from lingxi.adapters import feishu_user_message as module

        default = inspect.signature(FeishuUserMessages.__init__).parameters["transport"].default
        self.assertIsNone(default, "默认值在构造函数体里选择，不在签名里")
        client = FeishuUserMessages(base_url=BASE_URL, app_id="a", app_secret="s")
        self.assertIs(client._transport, module.no_redirect_transport)

        opener = module._no_redirect_opener()
        handlers = [
            handler for handler in opener.handlers if type(handler).__name__ == "_NoRedirect"
        ]
        self.assertEqual(len(handlers), 1)
        self.assertIsNone(
            handlers[0].redirect_request(None, None, 302, "Found", {}, "https://evil.invalid"),
            "redirect_request 返回 None，urllib 因此把 3xx 抛成 HTTPError",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
