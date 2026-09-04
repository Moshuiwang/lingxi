"""``adapters.feishu_delivery`` 的失败分类白名单断言（Issue #192）。

认领断言：`V-投递-03` 的后半句「超时或响应不明确记为 ``uncertain`` 且不自动重发」，
以及 `V-卡片-03` 依赖的同一条分界——**只有服务端已完整返回且明确业务拒绝**才算
"明确失败"。

为什么必须在这一层单独测：`adapters/feishu_delivery.py` 是"明确失败 vs 结果不明"的
**唯一裁定点**，也是"不重复投递"红线的最后一道闸。消费侧
（`tests/test_gateway_delivery.py`、`tests/test_worker_queue_consumer.py`）的用例注入的是
假 transport 直接抛出的 ``DeliveryRejectedError``/``LookupError``/``TimeoutError``——它们验证的是
"拿到某类异常之后怎么办"，**模拟**了本层的产出，却覆盖不到"哪种 ``lark_oapi`` 响应形状
产出哪类异常"这一步。日后有人把"缺可回读标识"那条 ``LookupError`` 改成
``DeliveryRejectedError``（或把 ``raise DeliveryRejectedError`` 加到不该有的位置），红线会重新
打开而全量门禁仍然全绿。这正是独立审核 O-2 / N-3 登记的缺口。

测试手段：按仓库既有惯例（`tests/test_gateway_transport.py`、
`tests/test_claude_agent_hooks_adapter.py`）用 ``sys.modules`` 把 ``lark_oapi`` 换成桩，
再用假 client 精确注入响应形状。不连真实飞书、不需要真实数据库、不需要装 SDK。
用例蓝本是 PR #172 第 4 轮独立复核的一次性人工探针（10 种形状），本文件把它固化成回归并
补齐探针没走到的穿透路径。

覆盖原则：**每个外发点都要有两侧用例**——「业务拒绝 → ``DeliveryRejectedError``」与「异常
原样穿透 → 结果不明」。只测其中一侧的外发点等于没有守住那一侧（PR #199 二级独立审查
P1 实证：卡片发送点起初只有拒绝侧，给该点的外呼包一层 ``except Exception: raise
DeliveryRejectedError`` 时全部用例仍然全绿）。

分类规则本身不在本 Issue 范围内改动；本文件只把现有规则钉死。
"""

from __future__ import annotations

import json
import sys
import types
import unittest
from typing import Any

from lingxi.config.content import default_content_catalog
from lingxi.core.execution.card_stream import CardCreated, DeliveryRejectedError

# --------------------------------------------------------------------------------------
# 桩 SDK：只提供 adapters.feishu_delivery 真正用到的 builder 与模型
# --------------------------------------------------------------------------------------


class _Built:
    """builder 产出的结果对象：把收集到的字段原样挂成属性。

    ``_settings_to_dict`` 会读 ``settings.config.streaming_mode``，因此这里必须是真属性
    而不是字典。
    """

    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


class _Builder:
    """把 ``X.builder().a(1).b(2).build()` 这种链式调用收集成 ``_Built``。

    真实 SDK 的每个 builder 方法名各不相同，但形态统一（每个方法收一个值并返回自身），
    所以这里用 ``__getattr__`` 兜住全部方法名，不逐个复制 SDK 的签名——本文件要钉的是
    分类分支，不是 SDK 的字段名（字段名由 `tests/test_feishu_delivery_card_payload.py`
    与 Bot-Test 的 L4a 负责）。
    """

    def __init__(self) -> None:
        self._fields: dict[str, Any] = {}

    def __getattr__(self, name: str):
        def collect(value: Any) -> _Builder:
            self._fields[name] = value
            return self

        return collect

    def build(self) -> _Built:
        return _Built(**self._fields)


class _StubModel:
    @classmethod
    def builder(cls) -> _Builder:
        return _Builder()


_CARDKIT_NAMES = (
    "CreateCardRequest",
    "CreateCardRequestBody",
    "ContentCardElementRequest",
    "ContentCardElementRequestBody",
    "Config",
    "Settings",
    "SettingsCardRequest",
    "SettingsCardRequestBody",
)
_IM_NAMES = ("ReplyMessageRequest", "ReplyMessageRequestBody")


def _install_stub_sdk(test_case: unittest.TestCase) -> None:
    """把 ``lark_oapi`` 及其两个子模块换成桩，并在用例结束后还原。"""

    cardkit_v1 = types.ModuleType("lark_oapi.api.cardkit.v1")
    for name in _CARDKIT_NAMES:
        setattr(cardkit_v1, name, type(name, (_StubModel,), {}))

    im_v1 = types.ModuleType("lark_oapi.api.im.v1")
    for name in _IM_NAMES:
        setattr(im_v1, name, type(name, (_StubModel,), {}))

    cardkit = types.ModuleType("lark_oapi.api.cardkit")
    cardkit.v1 = cardkit_v1
    im = types.ModuleType("lark_oapi.api.im")
    im.v1 = im_v1
    api = types.ModuleType("lark_oapi.api")
    api.cardkit = cardkit
    api.im = im
    root = types.ModuleType("lark_oapi")
    root.api = api

    modules = {
        "lark_oapi": root,
        "lark_oapi.api": api,
        "lark_oapi.api.cardkit": cardkit,
        "lark_oapi.api.cardkit.v1": cardkit_v1,
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


# --------------------------------------------------------------------------------------
# 假响应与假 client
# --------------------------------------------------------------------------------------


class _Response:
    """与 ``lark_oapi`` 响应同形的最小假响应：``success()`` / ``code`` / ``msg`` /
    ``get_log_id()`` / ``data``。
    """

    def __init__(
        self,
        *,
        ok: bool,
        code: int = 0,
        msg: str = "success",
        log_id: str = "log-fake-1",
        data: Any = None,
    ) -> None:
        self._ok = ok
        self.code = code
        self.msg = msg
        self._log_id = log_id
        self.data = data

    def success(self) -> bool:
        return self._ok

    def get_log_id(self) -> str:
        return self._log_id


def _rejected(code: int = 230001, msg: str = "permission denied") -> _Response:
    """服务端完整返回且业务错误码明确不为 0——唯一的"明确失败"形状。"""

    return _Response(ok=False, code=code, msg=msg, log_id="log-rejected")


def _created(card_id: str = "card-1") -> _Response:
    return _Response(ok=True, data=_Built(card_id=card_id))


def _sent(message_id: str = "om-msg-1") -> _Response:
    return _Response(ok=True, data=_Built(message_id=message_id))


def _accepted() -> _Response:
    """流式更新 / 关闭流式的成功响应：这两个接口不回读标识。"""

    return _Response(ok=True, data=_Built())


class _UnknownSDKError(Exception):
    """既不是网络类、也不是解析类、更不是缺标识——SDK 未来可能新增的任何异常。

    默认拒绝规则要用一个"不在任何名单中的未知对象"证明（验证与门禁第八节第 4 条），
    不能只证明某个已知形状被正确归类。
    """


class _Endpoint:
    """一个假接口：按注入的脚本依次返回响应或抛出异常，并记录调用次数。"""

    def __init__(self, outcome: Any) -> None:
        self._outcome = outcome
        self.calls: list[Any] = []

    def __call__(self, request: Any) -> Any:
        self.calls.append(request)
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


class _FakeClient:
    """``lark.Client`` 的最小替身，只暴露被调到的四条路径。"""

    def __init__(
        self,
        *,
        create: Any = None,
        content: Any = None,
        settings: Any = None,
        reply: Any = None,
    ) -> None:
        self.create = create if isinstance(create, _Endpoint) else _Endpoint(create or _created())
        self.content = (
            content if isinstance(content, _Endpoint) else _Endpoint(content or _accepted())
        )
        self.settings = (
            settings if isinstance(settings, _Endpoint) else _Endpoint(settings or _accepted())
        )
        self.reply = reply if isinstance(reply, _Endpoint) else _Endpoint(reply or _sent())

        self.cardkit = types.SimpleNamespace(
            v1=types.SimpleNamespace(
                card=types.SimpleNamespace(create=self.create, settings=self.settings),
                card_element=types.SimpleNamespace(content=self.content),
            )
        )
        self.im = types.SimpleNamespace(
            v1=types.SimpleNamespace(message=types.SimpleNamespace(reply=self.reply))
        )


def _card():
    return default_content_catalog().card("query.status", status="正在处理 · 3 秒")


def _json_decode_error() -> json.JSONDecodeError:
    """``lark_oapi`` 内部解析响应体失败时向上抛的真实异常类型。"""

    try:
        json.loads("<html>502 Bad Gateway</html>")
    except json.JSONDecodeError as error:
        return error
    raise AssertionError("构造 JSONDecodeError 失败")


class _ClassificationTestCase(unittest.TestCase):
    """两类映射的公共断言入口。

    刻意不写成"断言抛了某个异常就算过"：那样把 ``DeliveryRejectedError`` 与"结果不明"混在
    一起也能全绿。这里每次都同时断言**是哪一类**。
    """

    def setUp(self) -> None:
        _install_stub_sdk(self)

    def _transports(self, client: _FakeClient):
        from lingxi.adapters.feishu_delivery import LarkCardTransport, LarkDeliveryText

        return LarkCardTransport(client), LarkDeliveryText(client)

    def assert_explicit_rejection(
        self, call, *, expected_code: Any = 230001
    ) -> DeliveryRejectedError:
        """应归"明确失败"：消费侧会据此清预留位、允许重试或降级文本。"""

        with self.assertRaises(DeliveryRejectedError) as caught:
            call()
        error = caught.exception
        self.assertEqual(error.code, expected_code, "平台业务错误码必须透传，否则告警无法定位")
        self.assertEqual(error.log_id, "log-rejected", "log_id 必须透传，供人工按 log 核对")
        return error

    def assert_result_unknown(self, call, expected_type: type[BaseException]) -> BaseException:
        """应归"结果不明"：消费侧不重发、不降级、留预留位、落 ``uncertain``。"""

        with self.assertRaises(expected_type) as caught:
            call()
        error = caught.exception
        self.assertNotIsInstance(
            error,
            DeliveryRejectedError,
            "这是「结果不明」形状，一旦被判成 DeliveryRejectedError，消费侧就会重发或降级，"
            "重复投递红线被打开",
        )
        return error


class DeliveryRejectedTypeBoundaryTests(unittest.TestCase):
    """白名单成立的前提：``DeliveryRejectedError`` 不能和"结果不明"那几类异常有继承关系。"""

    def test_it_is_not_a_supertype_of_the_result_unknown_families(self) -> None:
        # 若哪天有人让它继承 OSError / LookupError / ValueError，消费侧的
        # `except DeliveryRejectedError` 会把网络异常、缺标识、JSON 解析失败一起吞成"明确失败"。
        for family in (OSError, LookupError, ValueError):
            self.assertFalse(
                issubclass(family, DeliveryRejectedError),
                f"{family.__name__} 不得是 DeliveryRejectedError 的子类",
            )
            self.assertFalse(
                issubclass(DeliveryRejectedError, family),
                f"DeliveryRejectedError 不得继承 {family.__name__}，否则白名单会漏成黑名单",
            )
        self.assertTrue(issubclass(DeliveryRejectedError, Exception))


class CardCreateClassificationTests(_ClassificationTestCase):
    """建卡 + 发送卡片（``LarkCardTransport.create``）的响应形状。"""

    def test_create_business_error_is_an_explicit_rejection(self) -> None:
        """形状 1：建卡 ``success()=False``。"""

        client = _FakeClient(create=_rejected())
        card_transport, _ = self._transports(client)

        self.assert_explicit_rejection(
            lambda: card_transport.create(
                chat_id="oc-1", thread_id=None, reply_to_message_id="om-in-1", card=_card()
            )
        )
        self.assertEqual(len(client.reply.calls), 0, "建卡被明确拒绝后不应继续发消息")

    def test_create_success_without_data_is_result_unknown(self) -> None:
        """形状 2：建卡 ``success()=True`` 但 ``data`` 为 ``None``。"""

        client = _FakeClient(create=_Response(ok=True, data=None))
        card_transport, _ = self._transports(client)

        error = self.assert_result_unknown(
            lambda: card_transport.create(
                chat_id="oc-1", thread_id=None, reply_to_message_id="om-in-1", card=_card()
            ),
            LookupError,
        )
        self.assertIn("card_id", str(error))
        self.assertEqual(len(client.reply.calls), 0)

    def test_create_success_with_blank_card_id_is_result_unknown(self) -> None:
        """形状 3：建卡 ``success()=True`` 但 ``card_id`` 是空串。"""

        client = _FakeClient(create=_created(card_id=""))
        card_transport, _ = self._transports(client)

        self.assert_result_unknown(
            lambda: card_transport.create(
                chat_id="oc-1", thread_id=None, reply_to_message_id="om-in-1", card=_card()
            ),
            LookupError,
        )

    def test_create_json_decode_failure_passes_through_as_result_unknown(self) -> None:
        """形状 4：``lark_oapi`` 解析响应体失败（网关返回 HTML）——原样穿透。"""

        client = _FakeClient(create=_json_decode_error())
        card_transport, _ = self._transports(client)

        self.assert_result_unknown(
            lambda: card_transport.create(
                chat_id="oc-1", thread_id=None, reply_to_message_id="om-in-1", card=_card()
            ),
            json.JSONDecodeError,
        )

    def test_create_read_timeout_passes_through_as_result_unknown(self) -> None:
        """形状 5：读超时（``requests`` 的超时异常继承自内置 ``OSError``）。"""

        client = _FakeClient(create=TimeoutError("read timed out"))
        card_transport, _ = self._transports(client)

        self.assert_result_unknown(
            lambda: card_transport.create(
                chat_id="oc-1", thread_id=None, reply_to_message_id="om-in-1", card=_card()
            ),
            TimeoutError,
        )

    def test_create_connection_reset_passes_through_as_result_unknown(self) -> None:
        """形状 6：连接中断——服务端可能已经建好卡片，绝不能当成明确失败。"""

        client = _FakeClient(create=ConnectionResetError("connection reset by peer"))
        card_transport, _ = self._transports(client)

        self.assert_result_unknown(
            lambda: card_transport.create(
                chat_id="oc-1", thread_id=None, reply_to_message_id="om-in-1", card=_card()
            ),
            ConnectionResetError,
        )

    def test_send_business_error_is_an_explicit_rejection(self) -> None:
        """形状 7：卡片已建好、发送消息 ``success()=False``。"""

        client = _FakeClient(reply=_rejected())
        card_transport, _ = self._transports(client)

        self.assert_explicit_rejection(
            lambda: card_transport.create(
                chat_id="oc-1", thread_id=None, reply_to_message_id="om-in-1", card=_card()
            )
        )
        self.assertEqual(len(client.create.calls), 1)

    def test_send_success_without_message_id_is_result_unknown(self) -> None:
        """形状 8：发送响应成功但缺可回读标识 ``message_id``。

        没有 ``message_id`` 就拿不到 ``platform_received`` 的凭据，但消息**可能已经发出去**
        ——这正是"结果不明"，重发会让用户看到两条。
        """

        client = _FakeClient(reply=_Response(ok=True, data=_Built(message_id=None)))
        card_transport, _ = self._transports(client)

        error = self.assert_result_unknown(
            lambda: card_transport.create(
                chat_id="oc-1", thread_id=None, reply_to_message_id="om-in-1", card=_card()
            ),
            LookupError,
        )
        self.assertIn("message_id", str(error))

    def test_send_success_without_data_is_result_unknown(self) -> None:
        """形状 9：发送响应 ``success()=True`` 但整个 ``data`` 为 ``None``。"""

        client = _FakeClient(reply=_Response(ok=True, data=None))
        card_transport, _ = self._transports(client)

        self.assert_result_unknown(
            lambda: card_transport.create(
                chat_id="oc-1", thread_id=None, reply_to_message_id="om-in-1", card=_card()
            ),
            LookupError,
        )

    def test_send_read_timeout_passes_through_as_result_unknown(self) -> None:
        """形状 10：卡片**已经建好**、发送消息时读超时——``create()`` 里误判后果最重的一格。

        飞书可能已经把这张卡片发出去了，我们只是没等到响应。判成 ``DeliveryRejectedError``
        会让消费侧清预留位并重试或降级文本，用户于是收到两遍同一个答案——正是不重复
        投递红线要挡的事。PR #199 二级独立审查 P1：这一格此前零覆盖，给该外呼包一层
        ``except Exception: raise DeliveryRejectedError`` 时整组用例仍然全绿。
        """

        client = _FakeClient(create=_created(), reply=TimeoutError("read timed out"))
        card_transport, _ = self._transports(client)

        self.assert_result_unknown(
            lambda: card_transport.create(
                chat_id="oc-1", thread_id=None, reply_to_message_id="om-in-1", card=_card()
            ),
            TimeoutError,
        )
        self.assertEqual(len(client.create.calls), 1, "应当已经越过建卡、失败在发送这一步")
        self.assertEqual(len(client.reply.calls), 1, "发送只应尝试一次，本层不重试")

    def test_send_unknown_exception_passes_through_as_result_unknown(self) -> None:
        """形状 11：卡片已建好、发送消息抛出不在任何名单里的未知异常。

        与上一条同一个位置，但换成默认拒绝的反向证明（验证与门禁第八节第 4 条）：
        不能只证明已知的超时形状被正确归类。
        """

        client = _FakeClient(create=_created(), reply=_UnknownSDKError("SDK 内部未知失败"))
        card_transport, _ = self._transports(client)

        self.assert_result_unknown(
            lambda: card_transport.create(
                chat_id="oc-1", thread_id=None, reply_to_message_id="om-in-1", card=_card()
            ),
            _UnknownSDKError,
        )
        self.assertEqual(len(client.reply.calls), 1)

    def test_the_happy_path_returns_both_readback_identifiers(self) -> None:
        """对照组：两段都成功时必须原样返回可回读标识，不被分类分支误伤。"""

        client = _FakeClient(create=_created(card_id="card-77"), reply=_sent(message_id="om-77"))
        card_transport, _ = self._transports(client)

        result = card_transport.create(
            chat_id="oc-1", thread_id="omt-1", reply_to_message_id="om-in-1", card=_card()
        )
        self.assertEqual(result, CardCreated(card_id="card-77", message_id="om-77"))


class CardUpdateAndCloseClassificationTests(_ClassificationTestCase):
    """流式更新与关闭流式（``update`` / ``close``）的响应形状。"""

    def test_stream_update_business_error_is_an_explicit_rejection(self) -> None:
        """形状 12：流式更新 ``success()=False``。"""

        client = _FakeClient(content=_rejected())
        card_transport, _ = self._transports(client)

        self.assert_explicit_rejection(
            lambda: card_transport.update(card_id="card-1", sequence=2, card=_card())
        )

    def test_stream_update_network_failure_is_result_unknown(self) -> None:
        """形状 13：流式更新读超时。"""

        client = _FakeClient(content=TimeoutError("read timed out"))
        card_transport, _ = self._transports(client)

        self.assert_result_unknown(
            lambda: card_transport.update(card_id="card-1", sequence=2, card=_card()),
            TimeoutError,
        )

    def test_stream_close_business_error_is_an_explicit_rejection(self) -> None:
        """形状 14：关闭流式 ``success()=False``。"""

        client = _FakeClient(settings=_rejected())
        card_transport, _ = self._transports(client)

        self.assert_explicit_rejection(
            lambda: card_transport.close(card_id="card-1", sequence=3, card=_card())
        )

    def test_stream_close_json_decode_failure_is_result_unknown(self) -> None:
        """形状 15：关闭流式时 ``lark_oapi`` 解析响应体失败。"""

        client = _FakeClient(settings=_json_decode_error())
        card_transport, _ = self._transports(client)

        self.assert_result_unknown(
            lambda: card_transport.close(card_id="card-1", sequence=3, card=_card()),
            json.JSONDecodeError,
        )

    def test_the_happy_path_of_update_and_close_raises_nothing(self) -> None:
        client = _FakeClient()
        card_transport, _ = self._transports(client)

        card_transport.update(card_id="card-1", sequence=2, card=_card())
        card_transport.close(card_id="card-1", sequence=3, card=_card())

        self.assertEqual(len(client.content.calls), 1)
        self.assertEqual(len(client.settings.calls), 1)


class TextFallbackClassificationTests(_ClassificationTestCase):
    """文本兜底通道（``LarkDeliveryText.send_text``）的响应形状。

    这条通道的误判后果最直接：判成"明确失败"就会按退避重试，用户会收到多条一模一样的
    文本终态（PR #172 第 4 轮 A/B 对照里旧实现真的发出过 6 条）。
    """

    def _send(self, client: _FakeClient):
        _, text_transport = self._transports(client)
        return lambda: text_transport.send_text(
            chat_id="oc-1", thread_id=None, reply_to_message_id="om-in-1", text="查询完成"
        )

    def test_text_business_error_is_an_explicit_rejection(self) -> None:
        """形状 16：文本 ``success()=False``。"""

        client = _FakeClient(reply=_rejected())
        self.assert_explicit_rejection(self._send(client))

    def test_text_success_without_data_is_result_unknown(self) -> None:
        """形状 17：文本 ``success()=True`` 但 ``data`` 为 ``None``。"""

        client = _FakeClient(reply=_Response(ok=True, data=None))
        self.assert_result_unknown(self._send(client), LookupError)

    def test_text_success_with_blank_message_id_is_result_unknown(self) -> None:
        """形状 18：文本 ``success()=True`` 但 ``message_id`` 是空串。"""

        client = _FakeClient(reply=_sent(message_id=""))
        self.assert_result_unknown(self._send(client), LookupError)

    def test_text_json_decode_failure_is_result_unknown(self) -> None:
        """形状 19：文本兜底时 ``lark_oapi`` 解析响应体失败。"""

        client = _FakeClient(reply=_json_decode_error())
        self.assert_result_unknown(self._send(client), json.JSONDecodeError)

    def test_text_connection_reset_is_result_unknown(self) -> None:
        """形状 20：文本兜底时连接中断——文本可能已经发出，重发用户会收到两条。"""

        client = _FakeClient(reply=ConnectionResetError("connection reset by peer"))
        self.assert_result_unknown(self._send(client), ConnectionResetError)

    def test_the_happy_path_returns_the_message_id(self) -> None:
        client = _FakeClient(reply=_sent(message_id="om-text-9"))
        self.assertEqual(self._send(client)(), "om-text-9")


class UnexpectedExceptionTests(_ClassificationTestCase):
    """默认拒绝的反向证明：一个不在任何名单里的未知异常必须落"结果不明"。

    验证与门禁第八节第 4 条——默认规则要用一个"不在名单中的未知对象"证明，不能只证明
    某个已知形状被正确归类。
    """

    def test_an_unknown_exception_is_never_promoted_to_an_explicit_rejection(self) -> None:
        for endpoint, invoke in (
            (
                "create",
                lambda transports: transports[0].create(
                    chat_id="oc-1", thread_id=None, reply_to_message_id="om-in-1", card=_card()
                ),
            ),
            (
                "update",
                lambda transports: transports[0].update(card_id="c", sequence=1, card=_card()),
            ),
            (
                "close",
                lambda transports: transports[0].close(card_id="c", sequence=1, card=_card()),
            ),
            (
                "send_text",
                lambda transports: transports[1].send_text(
                    chat_id="oc-1", thread_id=None, reply_to_message_id="om-in-1", text="查询完成"
                ),
            ),
        ):
            with self.subTest(endpoint=endpoint):
                failure = _UnknownSDKError("SDK 内部未知失败")
                client = _FakeClient(
                    create=failure, content=failure, settings=failure, reply=failure
                )
                transports = self._transports(client)
                self.assert_result_unknown(lambda: invoke(transports), _UnknownSDKError)


if __name__ == "__main__":
    unittest.main()
