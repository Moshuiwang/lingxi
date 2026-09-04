"""飞书出站：CardKit 流式卡片与投递文本兜底。

与 ``adapters/feishu_outbound.py`` 的加表情/简单回复分开成独立模块：这里
需要拿到并透传 ``message_id`` 作为 ``confirm_delivery`` 的
``platform_message_id``，两者失败语义不同，不共用同一个类。

**下面几个方法按白名单分类外发结果，不捕获任何未预期异常，都是刻意的**：
只有 ``response.success()`` 为假时才显式抛出 ``DeliveryRejected``；响应
成功但缺失可回读标识时显式抛 ``LookupError``；其余一律不捕获、原样向上
传播，判定为"结果不明"。

**卡片 JSON 2.0 载荷形状未经真实发送验证（证据等级 L1）**，留给 L4a。
``reply_to_message_id`` 理论上可为空但两个类都不用 ``chat_id``/
``thread_id`` 兜底（已知限制，未消除，上游总会填好这个字段）。
"""

from __future__ import annotations

import json
from typing import Any

from lingxi.config.content import RenderedCard
from lingxi.core.execution.card_stream import CardCreated, DeliveryRejected

# 卡片模板里唯一的可流式更新元素；标题与正文合并渲染进它的 content（见模块说明）。
_STATUS_ELEMENT_ID = "lingxi_status"


def _card_markdown(card: RenderedCard) -> str:
    """把 ``RenderedCard`` 的标题与正文合并成一段 markdown。"""
    return f"**{card.title}**\n\n{card.body}"


def _card_payload(card: RenderedCard) -> dict[str, Any]:
    """CardKit JSON 2.0 的 ``data`` 载荷：只有一个 markdown 元素，不含 ``header``。

    卡片不单独带 ``header``——阶段标题只在 ``_card_markdown`` 合并出的正文
    里承载一份，避免「标题在 header 与正文各写一遍」「终态后 header 仍停在
    建卡时的进度用词」两个问题。``update_multi=true`` 是流式卡片的必要
    开关；``streaming_mode=true`` 与建卡时就打开流式，之后靠
    ``elements.content`` 增量更新，最终用 ``settings`` 把它关闭。
    """
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "streaming_mode": True},
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "element_id": _STATUS_ELEMENT_ID,
                    "content": _card_markdown(card),
                }
            ]
        },
    }


def build_client(*, app_id: str, app_secret: str, timeout_seconds: float) -> Any:
    """构造官方 SDK 客户端。

    理由与 ``feishu_outbound.build_client`` 相同（不复制，调用方各自持有
    一份，见架构设计「Gateway 只持有飞书出站凭据」）。
    """
    import lark_oapi as lark

    return (
        lark.Client.builder().app_id(app_id).app_secret(app_secret).timeout(timeout_seconds).build()
    )


def _create_card(client: Any, card: RenderedCard) -> str:
    """建一张卡片（``POST cardkit/v1/cards``），返回 ``card_id``。"""
    from lark_oapi.api.cardkit.v1 import CreateCardRequest, CreateCardRequestBody

    create_request = (
        CreateCardRequest.builder()
        .request_body(
            CreateCardRequestBody.builder()
            .type("card_json")
            .data(json.dumps(_card_payload(card), ensure_ascii=False))
            .build()
        )
        .build()
    )
    create_response = client.cardkit.v1.card.create(create_request)
    if not create_response.success():
        raise DeliveryRejected(
            f"建卡失败：code={create_response.code} msg={create_response.msg} "
            f"log_id={create_response.get_log_id()}",
            code=create_response.code,
            log_id=create_response.get_log_id(),
        )
    if create_response.data is None or not create_response.data.card_id:
        # 结果不明：响应本身表示成功，但拿不到可回读标识——不能确定服务端是否
        # 真的建好了卡片，不属于 DeliveryRejected。
        raise LookupError(
            "建卡响应缺少可回读标识 card_id："
            f"code={create_response.code} msg={create_response.msg} "
            f"log_id={create_response.get_log_id()}"
        )
    return create_response.data.card_id


def _reply_with_card(
    client: Any, *, card_id: str, thread_id: str | None, reply_to_message_id: str
) -> str:
    """把已建好的卡片作为回复消息发出，返回 ``message_id``。"""
    from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

    send_body = json.dumps({"type": "card", "data": {"card_id": card_id}}, ensure_ascii=False)
    send_request = (
        ReplyMessageRequest.builder()
        .message_id(reply_to_message_id)
        .request_body(
            ReplyMessageRequestBody.builder()
            .content(send_body)
            .msg_type("interactive")
            .reply_in_thread(thread_id is not None)
            .build()
        )
        .build()
    )
    send_response = client.im.v1.message.reply(send_request)
    if not send_response.success():
        raise DeliveryRejected(
            f"卡片发送失败：code={send_response.code} msg={send_response.msg} "
            f"log_id={send_response.get_log_id()}",
            code=send_response.code,
            log_id=send_response.get_log_id(),
        )
    if send_response.data is None or not send_response.data.message_id:
        # 结果不明：同上，响应成功但缺可回读标识。
        raise LookupError(
            "卡片发送响应缺少可回读标识 message_id："
            f"code={send_response.code} msg={send_response.msg} "
            f"log_id={send_response.get_log_id()}"
        )
    return send_response.data.message_id


class LarkCardTransport:
    """实现 ``core.execution.card_stream.CardTransport``。"""

    def __init__(self, client: Any) -> None:
        """持有真实 lark_oapi 客户端。"""
        self._client = client

    def create(
        self,
        *,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
        card: RenderedCard,
    ) -> CardCreated:
        """建卡并把它作为消息发出（本类不单独暴露"只建卡不发送"）。

        一次调用消费掉 ``CardStream.start()`` 的一次外部调用预算；建卡本身
        没有飞书原生幂等键，调用方必须在调用这个方法之前已经完成"外发前
        预留位"的持久化提交，本方法不负责幂等，只负责把外部调用做对。
        """
        card_id = _create_card(self._client, card)
        message_id = _reply_with_card(
            self._client,
            card_id=card_id,
            thread_id=thread_id,
            reply_to_message_id=reply_to_message_id,
        )
        return CardCreated(card_id=card_id, message_id=message_id)

    def update(self, *, card_id: str, sequence: int, card: RenderedCard) -> None:
        """流式增量更新卡片正文所在的那个元素的 content。"""
        from lark_oapi.api.cardkit.v1 import (
            ContentCardElementRequest,
            ContentCardElementRequestBody,
        )

        request = (
            ContentCardElementRequest.builder()
            .card_id(card_id)
            .element_id(_STATUS_ELEMENT_ID)
            .request_body(
                ContentCardElementRequestBody.builder()
                .content(_card_markdown(card))
                .sequence(sequence)
                .build()
            )
            .build()
        )
        response = self._client.cardkit.v1.card_element.content(request)
        if not response.success():
            raise DeliveryRejected(
                f"卡片流式更新失败：code={response.code} msg={response.msg} "
                f"log_id={response.get_log_id()}",
                code=response.code,
                log_id=response.get_log_id(),
            )

    def close(self, *, card_id: str, sequence: int, card: RenderedCard) -> None:
        """把 ``streaming_mode`` 关闭。

        这一步与 ``update`` 共用同一整卡级 ``sequence`` 计数器，必须无缝
        递增。
        """
        from lark_oapi.api.cardkit.v1 import (
            Config,
            Settings,
            SettingsCardRequest,
            SettingsCardRequestBody,
        )

        settings = Settings.builder().config(Config.builder().streaming_mode(False).build()).build()
        request = (
            SettingsCardRequest.builder()
            .card_id(card_id)
            .request_body(
                SettingsCardRequestBody.builder()
                .settings(json.dumps(_settings_to_dict(settings), ensure_ascii=False))
                .sequence(sequence)
                .build()
            )
            .build()
        )
        response = self._client.cardkit.v1.card.settings(request)
        if not response.success():
            raise DeliveryRejected(
                f"卡片关闭流式失败：code={response.code} msg={response.msg} "
                f"log_id={response.get_log_id()}",
                code=response.code,
                log_id=response.get_log_id(),
            )


def _settings_to_dict(settings: Any) -> dict[str, Any]:
    """把 ``Settings`` 构造对象收窄成 JSON 字符串所需的字典。

    ``SettingsCardRequestBody.settings`` 要的是 JSON 字符串，而 SDK 的
    ``Settings`` 模型只提供 builder，没有现成的 to-dict；这里只取本类
    唯一用到的一个布尔字段，不引入通用序列化逻辑。
    """
    return {"config": {"streaming_mode": settings.config.streaming_mode}}


class LarkDeliveryText:
    """实现 ``core.execution.card_stream.TextTransport``（能返回 ``message_id``）。

    与 ``feishu_outbound.LarkReplies.send_text`` 走的是同一条飞书接口（回复消息），
    区别只是这里**必须**把响应里的 ``message_id`` 透传出去，供
    ``confirm_delivery`` 绑定为 ``platform_message_id``——G-CARD 实测：卡片与文本
    共用同一发送接口与响应结构，因此文本通道同样在这一步拿到「平台已接收」的可回读
    标识（issue 状态合同第 4 条）。
    """

    def __init__(self, client: Any) -> None:
        """持有真实 lark_oapi 客户端。"""
        self._client = client

    def send_text(
        self, *, chat_id: str, thread_id: str | None, reply_to_message_id: str, text: str
    ) -> str:
        """发送一条投递文本并返回 ``message_id``。"""
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

        content = json.dumps({"text": text}, ensure_ascii=False)
        request = (
            ReplyMessageRequest.builder()
            .message_id(reply_to_message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .content(content)
                .msg_type("text")
                .reply_in_thread(thread_id is not None)
                .build()
            )
            .build()
        )
        response = self._client.im.v1.message.reply(request)
        if not response.success():
            raise DeliveryRejected(
                f"发送投递文本失败：code={response.code} msg={response.msg} "
                f"log_id={response.get_log_id()}",
                code=response.code,
                log_id=response.get_log_id(),
            )
        if response.data is None or not response.data.message_id:
            # 结果不明：响应成功但缺可回读标识 message_id。
            raise LookupError(
                "发送投递文本响应缺少可回读标识 message_id："
                f"code={response.code} msg={response.msg} log_id={response.get_log_id()}"
            )
        return response.data.message_id
