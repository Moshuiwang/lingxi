"""飞书出站：CardKit 流式卡片与投递文本兜底（Issue #152）。

与 ``adapters/feishu_outbound.py`` 的加表情/简单回复分开成独立模块，理由是这里的
两个类服务的是投递语义（需要拿到并透传 ``message_id`` 作为 ``confirm_delivery`` 的
``platform_message_id``），而 ``feishu_outbound.LarkReplies`` 服务的是管线里"尽力而为、
不追踪送达标识"的普通提示回复（忙碌、停用、排队失败……）——两者的调用方与失败语义都
不同，不应该共用同一个类。

按 2026-08-06 决策走官方 ``lark-oapi``；``lark_oapi`` 在函数内延迟导入，与仓库既有惯例
一致，不碰这两个类的测试无需装 SDK。

**卡片 JSON 2.0 载荷形状未经真实发送验证（证据等级 L1）。** 字段名与调用形态（
``cardkit/v1/cards`` 的 ``create``/``elements.content`` 流式更新/``settings`` 关闭、
共用整卡级 ``sequence``）已经由 G-CARD Bot-Test 探针实测确认（#162 评论
5291111636）；卡片模板本身的 JSON 结构（``header`` + 一个固定 ``element_id`` 的
markdown 元素）依据官方 CardKit JSON 2.0 文档惯例编写，尚未经过真实发送验证——
这一点留给 Bot-Test/Stage 的 L4a（issue #152 可观察完成标准明确本 Story 只到
Runtime Assembled）。``CardStream`` 把标题与正文合并成一段 markdown 文本后交给
``update()``：流式内容更新接口只更新**一个**元素的 ``content``，而
``RenderedCard`` 同时含标题与正文，合并成一段是让"一次 ``CardStream.update()``
调用对应一次外部调用"这条既有约束继续成立的最小改法，不引入第二个元素、第二个
序号消耗点。
"""

from __future__ import annotations

import json
from typing import Any

from lingxi.config.content import RenderedCard
from lingxi.core.execution.card_stream import CardCreated

# 卡片模板里唯一的可流式更新元素；标题与正文合并渲染进它的 content（见模块说明）。
_STATUS_ELEMENT_ID = "lingxi_status"


def _card_markdown(card: RenderedCard) -> str:
    """把 ``RenderedCard`` 的标题与正文合并成一段 markdown。"""

    return f"**{card.title}**\n\n{card.body}"


def _card_payload(card: RenderedCard) -> dict[str, Any]:
    """CardKit JSON 2.0 的 ``data`` 载荷：一个 header + 一个 markdown 元素。

    ``update_multi=true`` 是流式卡片的必要开关（issue 状态合同第 1 条）；
    ``streaming_mode=true`` 与建卡时就打开流式，之后靠 ``elements.content`` 增量更新，
    最终用 ``settings`` 把它关闭（G-CARD 实测的生命周期）。
    """

    return {
        "schema": "2.0",
        "config": {"update_multi": True, "streaming_mode": True},
        "header": {"title": {"tag": "plain_text", "content": card.title}},
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
    """构造官方 SDK 客户端；理由与 ``feishu_outbound.build_client`` 相同（不复制，
    调用方各自持有一份，见架构设计「Gateway 只持有飞书出站凭据」）。
    """

    import lark_oapi as lark

    return (
        lark.Client.builder()
        .app_id(app_id)
        .app_secret(app_secret)
        .timeout(timeout_seconds)
        .build()
    )


class LarkCardTransport:
    """实现 ``core.execution.card_stream.CardTransport``。"""

    def __init__(self, client: Any) -> None:
        self._client = client

    def create(
        self,
        *,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
        card: RenderedCard,
    ) -> CardCreated:
        """建卡并把它作为消息发出，一次调用消费掉 ``CardStream.start()`` 的一次外部
        调用预算（本类不单独暴露"只建卡不发送"）。

        建卡（``POST cardkit/v1/cards``）本身没有飞书原生幂等键；调用方
        （``apps.gateway.delivery``）必须在调用这个方法之前已经完成"外发前预留位"
        的持久化提交（Issue #151 审核 P3-6），本方法不负责幂等，只负责把外部调用
        做对。
        """

        from lark_oapi.api.cardkit.v1 import CreateCardRequest, CreateCardRequestBody
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

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
        create_response = self._client.cardkit.v1.card.create(create_request)
        if not create_response.success():
            raise RuntimeError(
                "建卡失败："
                f"code={create_response.code} msg={create_response.msg} "
                f"log_id={create_response.get_log_id()}"
            )
        card_id = create_response.data.card_id

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
        send_response = self._client.im.v1.message.reply(send_request)
        if not send_response.success():
            raise RuntimeError(
                "卡片发送失败："
                f"code={send_response.code} msg={send_response.msg} "
                f"log_id={send_response.get_log_id()}"
            )
        return CardCreated(card_id=card_id, message_id=send_response.data.message_id)

    def update(self, *, card_id: str, sequence: int, card: RenderedCard) -> None:
        from lark_oapi.api.cardkit.v1 import ContentCardElementRequest, ContentCardElementRequestBody

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
            raise RuntimeError(
                "卡片流式更新失败："
                f"code={response.code} msg={response.msg} log_id={response.get_log_id()}"
            )

    def close(self, *, card_id: str, sequence: int, card: RenderedCard) -> None:
        """把 ``streaming_mode`` 关闭；G-CARD 实测确认这一步与 ``update`` 共用同一
        整卡级 ``sequence`` 计数器，必须无缝递增（跨接口拼接见探针记录）。
        """

        from lark_oapi.api.cardkit.v1 import Config, Settings, SettingsCardRequest, SettingsCardRequestBody

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
            raise RuntimeError(
                "卡片关闭流式失败："
                f"code={response.code} msg={response.msg} log_id={response.get_log_id()}"
            )


def _settings_to_dict(settings: Any) -> dict[str, Any]:
    """``SettingsCardRequestBody.settings`` 要的是 JSON **字符串**，而 SDK 的
    ``Settings`` 模型只提供 builder，没有现成的 to-dict；这里只取本类唯一用到
    的一个布尔字段，不引入通用序列化逻辑。
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
        self._client = client

    def send_text(
        self, *, chat_id: str, thread_id: str | None, reply_to_message_id: str, text: str
    ) -> str:
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
            raise RuntimeError(
                f"发送投递文本失败：code={response.code} msg={response.msg} "
                f"log_id={response.get_log_id()}"
            )
        return response.data.message_id
