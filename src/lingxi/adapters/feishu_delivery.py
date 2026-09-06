"""飞书出站：CardKit 流式卡片与投递文本兜底。

与 ``adapters/feishu_outbound.py`` 的加表情/简单回复分开成独立模块：这里需要拿到并
透传 ``message_id`` 作为 ``confirm_delivery`` 的 ``platform_message_id``，两者失败
语义不同，不共用同一个类。

**分类不看 SDK 的 ``response.success()``，改由 :func:`_verdict` 按操作分档核成功码与
必要回读标识**（表见 ``core.delivery.ports``）：空响应解析出 ``code=None``、
``success()`` 为假，旧写法据此判"明确拒绝"、消费侧改走另一条通道，而那次外发很可能
已经生效——缺码是"结果不明"。拒绝抛 ``DeliveryRejectedError``、不明抛
``DeliveryUncertainError``（缺标识沿用 ``LookupError``），未预期异常原样传播。

**卡片 JSON 2.0 载荷形状未经真实发送验证（L1）**，留给 L4a；
``reply_to_message_id`` 可为空但两个类都不用 ``chat_id``/``thread_id`` 兜底（已知限制，未消除）。
"""

from __future__ import annotations

import json
from typing import Any

from lingxi.config.content import RenderedCard
from lingxi.core.delivery.ports import (
    DeliveryOperation,
    DeliveryVerdict,
    classify_delivery_response,
)
from lingxi.core.execution.card_stream import (
    CardCreated,
    DeliveryRejectedError,
    DeliveryUncertainError,
)

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


def _readback(response: Any, field: str) -> Any:
    """从响应里取该操作的必要回读标识；``data`` 缺失时返回 ``None``（判为缺标识）。"""
    data = getattr(response, "data", None)
    if data is None:
        return None
    return getattr(data, field, None)


def _verdict(response: Any, *, operation: DeliveryOperation, field: str | None, label: str) -> None:
    """按操作分档裁定一次 SDK 响应；只在"明确拒绝"与"结果不明"两种情况下抛错。

    ``label`` 只用于人读的错误消息（"建卡"/"卡片发送"……），不参与判定。
    ``field`` 是该操作的必要回读标识字段名；不回读新标识的操作传 ``None``。

    **缺标识仍抛 ``LookupError``**：这是既有契约（"响应本身表示成功，但拿不到
    可回读标识"），消费侧与 ``DeliveryUncertainError`` 一视同仁地当作"不明"，
    这里不为了统一类型而改动一条已经被用例钉死的分界。
    """
    identifier = _readback(response, field) if field is not None else None
    outcome = classify_delivery_response(
        operation=operation,
        code=response.code,
        **({"identifier": identifier} if field is not None else {}),
    )
    if outcome.verdict is DeliveryVerdict.ACCEPTED:
        return
    detail = f"code={response.code} msg={response.msg} log_id={response.get_log_id()}"
    if outcome.verdict is DeliveryVerdict.REJECTED:
        raise DeliveryRejectedError(
            f"{label}失败：{detail}", code=response.code, log_id=response.get_log_id()
        )
    if field is not None and outcome.reason == f"missing_{field}":
        raise LookupError(f"{label}响应缺少可回读标识 {field}：{detail}")
    raise DeliveryUncertainError(
        f"{label}结果不明（{outcome.reason}）：{detail}",
        reason=outcome.reason,
        code=response.code,
        log_id=response.get_log_id(),
        retry_safe=outcome.retry_safe,
    )


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
    _verdict(
        create_response, operation=DeliveryOperation.CARD_CREATE, field="card_id", label="建卡"
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
    _verdict(
        send_response, operation=DeliveryOperation.CARD_REPLY, field="message_id", label="卡片发送"
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
        _verdict(
            response, operation=DeliveryOperation.CARD_UPDATE, field=None, label="卡片流式更新"
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
        _verdict(response, operation=DeliveryOperation.CARD_CLOSE, field=None, label="卡片关闭流式")


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
        _verdict(
            response,
            operation=DeliveryOperation.TEXT_SEND,
            field="message_id",
            label="发送投递文本",
        )
        return response.data.message_id
