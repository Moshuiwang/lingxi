"""``adapters.feishu_admin_card.LarkAdminCardTransport`` 的 builder 产物结构断言
（外部审查交叉裁定，opus P2-1）。

opus 装真实 ``lark-oapi==1.7.1`` 本地构造（不发请求）实测：候选原来的 ``update()``
把整份 JSON 字符串直接传给 ``UpdateCardRequestBody.builder().card(...)``，但那个
字段的真实类型是 ``Card``（``lark_oapi/api/cardkit/v1/model/card.py``，一个带
``type``/``data`` 两个字符串字段的独立模型，不是字符串本身），且完全没有调用
``.sequence()``。真实 SDK 在本机不可用（v13 §6.4 红线：不真实发送任何飞书卡片，
且门禁 fast/full 两层都不装 ``gateway`` extras——见 pyproject.toml 的 extras 分组
与 story.yml/ci.yml 的 pip install 行），因此本文件按仓库既有惯例
（``tests/test_feishu_delivery_classification.py``）用 ``sys.modules`` 把
``lark_oapi.api.cardkit.v1``/``lark_oapi.api.im.v1`` 换成桩：桩的 ``X.builder()``
只是把每一次链式调用的参数原样收集成属性，不做任何真实校验——它不能替代真实网络
往返（那仍是 `biai-stage` 的 L4a），但足以钉住"生产代码调用 builder 时传的是什么
参数"，回归就是"哪个字段又变回了错误的形状"。

真实 CardKit 载荷的**内容**形状（``schema``/``body``/``elements``）已经由
``tests/test_feishu_admin_card_payload.py`` 覆盖，本文件不重复断言那一层，只断言
"builder 调用参数" 这一层——两者合起来才是"``_card_payload()`` 产出的字典被正确
交给了真实 SDK 的正确字段"这条完整链路。
"""

from __future__ import annotations

import json
import sys
import types
import unittest
from typing import Any

from lingxi.core.admin.notification import ConfirmCardButton, RenderedConfirmCard


class _Built:
    """builder 产出的结果对象：把收集到的字段原样挂成属性。"""

    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


class _Builder:
    """把 ``X.builder().a(1).b(2).build()`` 这种链式调用收集成 ``_Built``——与
    ``tests/test_feishu_delivery_classification.py`` 的同名类同一手法（不逐个复制
    SDK 的签名，本文件要钉的是"传了什么参数"，不是 SDK 的字段名本身是否存在）。
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


_CARDKIT_NAMES = ("CreateCardRequest", "CreateCardRequestBody", "Card", "UpdateCardRequest", "UpdateCardRequestBody")
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


class _Response:
    """与 ``lark_oapi`` 响应同形的最小假响应：``success()`` / ``code`` / ``msg`` /
    ``get_log_id()`` / ``data``。"""

    def __init__(self, *, ok: bool = True, data: Any = None) -> None:
        self._ok = ok
        self.code = 0
        self.msg = "success"
        self.data = data

    def success(self) -> bool:
        return self._ok

    def get_log_id(self) -> str:
        return "log-fake-1"


class _Endpoint:
    """记录每一次调用收到的 request 对象，不做任何断言——断言在测试方法里做。"""

    def __init__(self, response: _Response) -> None:
        self._response = response
        self.calls: list[Any] = []

    def __call__(self, request: Any) -> Any:
        self.calls.append(request)
        return self._response


class _FakeClient:
    def __init__(self) -> None:
        self.create_endpoint = _Endpoint(_Response(data=_Built(card_id="cardkit_fake_1")))
        self.update_endpoint = _Endpoint(_Response())
        self.reply_endpoint = _Endpoint(_Response(data=_Built(message_id="om_fake_1")))
        self.cardkit = types.SimpleNamespace(
            v1=types.SimpleNamespace(
                card=types.SimpleNamespace(
                    create=self.create_endpoint, update=self.update_endpoint
                )
            )
        )
        self.im = types.SimpleNamespace(
            v1=types.SimpleNamespace(message=types.SimpleNamespace(reply=self.reply_endpoint))
        )


def _card_with_buttons() -> RenderedConfirmCard:
    return RenderedConfirmCard(
        title="待确认：停用用户",
        body="动作：停用用户\n目标：ou_target",
        buttons=(
            ConfirmCardButton(
                label="确认执行", value={"pending_action_id": "pac_1", "decision": "confirm"}
            ),
        ),
    )


def _terminal_card() -> RenderedConfirmCard:
    return RenderedConfirmCard(title="停用用户 · 已结束", body="结果：已确认执行", buttons=())


class UpdateBuilderShapeTests(unittest.TestCase):
    """核心回归用例：候选原来在这里传了一个字符串，真实类型是 ``Card``。"""

    def setUp(self) -> None:
        _install_stub_sdk(self)

    def test_update_passes_a_card_object_not_a_raw_json_string(self) -> None:
        from lingxi.adapters.feishu_admin_card import LarkAdminCardTransport

        client = _FakeClient()
        transport = LarkAdminCardTransport(client)

        transport.update(card_id="cardkit_fake_1", sequence=7, card=_terminal_card())

        self.assertEqual(len(client.update_endpoint.calls), 1)
        request = client.update_endpoint.calls[0]
        self.assertEqual(request.card_id, "cardkit_fake_1")
        body = request.request_body
        # 核心回归断言：card 字段不是字符串——真实 SDK 的 UpdateCardRequestBody.card
        # 类型是 Card，字符串会在真实序列化时产出错误的请求体（opus 实测）。
        self.assertNotIsInstance(body.card, str)
        self.assertEqual(body.card.type, "card_json")
        self.assertIsInstance(body.card.data, str)
        payload = json.loads(body.card.data)
        self.assertEqual(payload["schema"], "2.0")
        self.assertEqual(body.sequence, 7)

    def test_update_sequence_is_forwarded_verbatim_for_repeated_calls(self) -> None:
        """同一张卡片被回调重投触发两次 update：两次调用必须携带调用方各自传入的
        sequence（本类不做递增决策，递增决策属于
        ``adapters/postgres_pending_action.py`` 的 ``next_card_sequence()``）。"""

        from lingxi.adapters.feishu_admin_card import LarkAdminCardTransport

        client = _FakeClient()
        transport = LarkAdminCardTransport(client)

        transport.update(card_id="cardkit_fake_1", sequence=1, card=_terminal_card())
        transport.update(card_id="cardkit_fake_1", sequence=2, card=_terminal_card())

        sequences = [call.request_body.sequence for call in client.update_endpoint.calls]
        self.assertEqual(sequences, [1, 2])


class CreateBuilderShapeTests(unittest.TestCase):
    """建卡（``create``）此前已经是正确形状（``type``/``data`` 均为字符串，
    与 ``adapters/feishu_delivery.py`` 同构），本类只是把它也纳入回归覆盖——避免
    以后有人在这条路径上引入与 ``update()`` 同类的错误却没有测试网住。"""

    def setUp(self) -> None:
        _install_stub_sdk(self)

    def test_create_passes_flat_type_and_data_strings(self) -> None:
        from lingxi.adapters.feishu_admin_card import LarkAdminCardTransport

        client = _FakeClient()
        transport = LarkAdminCardTransport(client)

        created = transport.create(
            chat_id="oc_fake",
            thread_id=None,
            reply_to_message_id="om_trigger",
            card=_card_with_buttons(),
        )

        self.assertEqual(created.card_id, "cardkit_fake_1")
        self.assertEqual(created.message_id, "om_fake_1")

        request = client.create_endpoint.calls[0]
        body = request.request_body
        self.assertEqual(body.type, "card_json")
        self.assertIsInstance(body.data, str)
        payload = json.loads(body.data)
        self.assertEqual(payload["schema"], "2.0")

        reply_request = client.reply_endpoint.calls[0]
        self.assertEqual(reply_request.message_id, "om_trigger")
        reply_body = reply_request.request_body
        self.assertEqual(reply_body.msg_type, "interactive")
        sent_content = json.loads(reply_body.content)
        self.assertEqual(sent_content["data"]["card_id"], "cardkit_fake_1")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
