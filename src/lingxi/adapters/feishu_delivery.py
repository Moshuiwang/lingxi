"""飞书出站：CardKit 流式卡片与投递文本兜底（Issue #152）。

与 ``adapters/feishu_outbound.py`` 的加表情/简单回复分开成独立模块，理由是这里的
两个类服务的是投递语义（需要拿到并透传 ``message_id`` 作为 ``confirm_delivery`` 的
``platform_message_id``），而 ``feishu_outbound.LarkReplies`` 服务的是管线里"尽力而为、
不追踪送达标识"的普通提示回复（忙碌、停用、排队失败……）——两者的调用方与失败语义都
不同，不应该共用同一个类。

按 2026-08-06 决策走官方 ``lark-oapi``；``lark_oapi`` 在函数内延迟导入，与仓库既有惯例
一致，不碰这两个类的测试无需装 SDK。

**下面四个方法按白名单分类外发结果，不捕获任何未预期异常，都是刻意的**（独立
审核 B-1 首次修复于 2026-08-14；独立审核 R-1 于同日把判别方向从黑名单反转为
白名单）。只有 HTTP/RPC 已经完整返回、且 ``response.success()`` 为假（业务错误码
明确不为 0，``code``/``msg`` 字段可读，服务端已经明确拒绝这次调用）时，才显式
抛出 ``core.execution.card_stream.DeliveryRejected``——这是唯一被
``core.execution.card_stream``/``apps.gateway.delivery`` 当作"明确失败"处理的
异常类型。除此之外的一切都不捕获、原样向上传播，被调用方判定为"结果不明"：

- ``lark_oapi`` 内部解析响应体失败时抛出的 ``json.JSONDecodeError``；
- ``requests.exceptions.RequestException`` 及其子类（读超时、连接重置等，
  全部继承自内置 ``OSError``）——``lark_oapi`` 的 transport 是
  ``requests.request(...)``（``lark_oapi/core/http/transport.py``），这里不做
  任何异常翻译；
- ``response.success()`` 为真、但响应缺失可回读标识（``data`` 为 ``None`` 或
  ``card_id``/``message_id`` 缺失）——这种情况下面四个方法会显式检查并抛出
  ``LookupError``，同样不属于 ``DeliveryRejected``；
- 任何其它未预期的异常。

这个模块因此不需要在多种"结果不明"的成因之间做任何区分，也不需要 import
``requests``。

**卡片 JSON 2.0 载荷形状未经真实发送验证（证据等级 L1）。** 字段名与调用形态（
``cardkit/v1/cards`` 的 ``create``/``elements.content`` 流式更新/``settings`` 关闭、
共用整卡级 ``sequence``）已经由 G-CARD Bot-Test 探针实测确认（#162 评论
5291111636）；卡片模板本身的 JSON 结构（不含 ``header``、只有一个固定
``element_id`` 的 markdown 元素，Issue #175 起去掉 header，阶段标题只在正文承载
一份）依据官方 CardKit JSON 2.0 文档惯例编写，尚未经过真实发送验证——这一点留给
Bot-Test/Stage 的 L4a（issue #152 可观察完成标准明确本 Story 只到 Runtime
Assembled；无 header 卡片的真实接受度是 #175 新增的 L4a 未验证项）。
``CardStream`` 把标题与正文合并成一段 markdown 文本后交给 ``update()``：流式内容
更新接口只更新**一个**元素的 ``content``，而 ``RenderedCard`` 同时含标题与正文，
合并成一段是让"一次 ``CardStream.update()`` 调用对应一次外部调用"这条既有约束
继续成立的最小改法，不引入第二个元素、第二个序号消耗点。

**已知限制（独立审核 P3-4，未消除）：`reply_to_message_id` 可为空。** 迁移
``0058`` 起 ``task.reply_to_message_id`` 可空（``ALTER TABLE task ADD COLUMN
reply_to_message_id TEXT``），但下面两个类的卡片与文本通道都只走
``ReplyMessageRequest.message_id(reply_to_message_id)``——完全不使用调用方已经
传入的 ``chat_id``/``thread_id`` 做兜底。为空时两条通道都会被飞书拒绝，叠加
``apps.gateway.delivery.DeliveryConsumer`` 的重试退避会变成持续失败循环（不会
造成重复投递，只是持续无法送达）。当前 ``core.conversation.pipeline`` 总会填
``message.message_id``（生产路径下这个空值目前不会发生），因此判定为可接受的
已知限制而非本次必须修复的红线，留给下一次改动这个模块的人一并处理。
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

    Issue #175（2026-08-16 产品负责人定稿）：卡片不再单独带 ``header``——阶段标题
    只在 ``_card_markdown`` 合并出的正文里承载一份，同时修复「建卡时标题在 header
    与正文各写一遍」与「终态后 header 仍停在建卡时的进度用词」两个问题。

    ``update_multi=true`` 是流式卡片的必要开关（issue 状态合同第 1 条）；
    ``streaming_mode=true`` 与建卡时就打开流式，之后靠 ``elements.content`` 增量更新，
    最终用 ``settings`` 把它关闭（G-CARD 实测的生命周期）。
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
            raise DeliveryRejected(
                f"建卡失败：code={create_response.code} msg={create_response.msg} "
                f"log_id={create_response.get_log_id()}",
                code=create_response.code,
                log_id=create_response.get_log_id(),
            )
        if create_response.data is None or not create_response.data.card_id:
            # 结果不明（独立审核 R-1）：响应本身表示成功，但拿不到可回读标识——
            # 不能确定服务端是否真的建好了卡片，不属于 DeliveryRejected。
            raise LookupError(
                "建卡响应缺少可回读标识 card_id："
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
            raise DeliveryRejected(
                f"卡片发送失败：code={send_response.code} msg={send_response.msg} "
                f"log_id={send_response.get_log_id()}",
                code=send_response.code,
                log_id=send_response.get_log_id(),
            )
        if send_response.data is None or not send_response.data.message_id:
            # 结果不明（独立审核 R-1）：同上，响应成功但缺可回读标识。
            raise LookupError(
                "卡片发送响应缺少可回读标识 message_id："
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
            raise DeliveryRejected(
                f"卡片流式更新失败：code={response.code} msg={response.msg} "
                f"log_id={response.get_log_id()}",
                code=response.code,
                log_id=response.get_log_id(),
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
            raise DeliveryRejected(
                f"卡片关闭流式失败：code={response.code} msg={response.msg} "
                f"log_id={response.get_log_id()}",
                code=response.code,
                log_id=response.get_log_id(),
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
            raise DeliveryRejected(
                f"发送投递文本失败：code={response.code} msg={response.msg} "
                f"log_id={response.get_log_id()}",
                code=response.code,
                log_id=response.get_log_id(),
            )
        if response.data is None or not response.data.message_id:
            # 结果不明（独立审核 R-1）：响应成功但缺可回读标识 message_id。
            raise LookupError(
                "发送投递文本响应缺少可回读标识 message_id："
                f"code={response.code} msg={response.msg} log_id={response.get_log_id()}"
            )
        return response.data.message_id
