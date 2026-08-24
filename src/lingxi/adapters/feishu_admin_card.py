"""管理员确认卡片的真实出站：实现 ``core.admin.notification.AdminCardTransport``。

``lark_oapi`` 在函数内延迟导入，与仓库既有惯例一致（``adapters/feishu_outbound.py``、
``adapters/feishu_delivery.py``）：不碰这个类的测试无需装 SDK。

与 ``adapters/feishu_delivery.LarkCardTransport`` 同构（同一套 CardKit 建卡 + 回复
发送、``DeliveryRejected`` 白名单判别姿态），但渲染的是确认卡片专属的按钮回调形状
（见 ``core/admin/notification.py`` 模块文档"为什么不复用 RenderedCard"）。

**本模块的真实行为未验证（证据等级 1）。** 全部 L2 断言跑在注入的假实现上；真实
CardKit 字段与真实回调闭环属 `biai-stage` L4a 受控验收（本 Story 明确留待验收窗口，
见 PR 描述"未验证事项"）。红线（v13 §6.4 第 7 条）：测试中一律假 transport，不真实
发送任何飞书卡片。
"""

from __future__ import annotations

import json
from typing import Any

from lingxi.core.admin.notification import AdminCardCreated, AdminCardDeliveryRejected, RenderedConfirmCard


def _card_payload(card: RenderedConfirmCard) -> dict[str, Any]:
    """CardKit JSON 2.0 的 ``data`` 载荷。

    ``buttons`` 为空（终态卡片）时只有一个 markdown 元素、没有 ``action`` 元素——
    这就是"卡片更新为不可再次操作的最终状态"在 CardKit 层面的落点：终态卡片结构上
    不存在任何可点击的按钮，不是靠禁用态按钮或前端约定"这张卡片已经不能点了"。
    """

    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": f"**{card.title}**\n\n{card.body}"}
    ]
    if card.buttons:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": button.label},
                        "type": "primary" if button.value.get("decision") == "confirm" else "default",
                        # 按钮回传值：card.action.trigger 事件的 action.value 原样带回
                        # 这个 mapping（见 adapters/feishu_events.parse_card_action_event）。
                        "value": dict(button.value),
                    }
                    for button in card.buttons
                ],
            }
        )
    return {"schema": "2.0", "config": {"update_multi": True}, "body": {"elements": elements}}


class LarkAdminCardTransport:
    """实现 ``core.admin.notification.AdminCardTransport``。"""

    def __init__(self, client: Any) -> None:
        self._client = client

    def create(
        self,
        *,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
        card: RenderedConfirmCard,
    ) -> AdminCardCreated:
        """建卡并作为消息发出，回复触发命令的那条消息——卡片因此结构上只会出现在
        发起管理员本人与机器人的私聊里（合同"卡片只发送到……本人飞书账号，不能
        改发他人"由"回复同一条私聊消息"这个机制天然保证，不依赖额外的收件人校验）。
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
            raise AdminCardDeliveryRejected(
                f"建卡失败：code={create_response.code} msg={create_response.msg} "
                f"log_id={create_response.get_log_id()}",
                code=create_response.code,
                log_id=create_response.get_log_id(),
            )
        if create_response.data is None or not create_response.data.card_id:
            # 结果不明：响应本身表示成功，但拿不到可回读标识——不能确定服务端是否
            # 真的建好了卡片，不属于 AdminCardDeliveryRejected（与
            # feishu_delivery.LarkCardTransport 同一白名单姿态）。
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
            raise AdminCardDeliveryRejected(
                f"卡片发送失败：code={send_response.code} msg={send_response.msg} "
                f"log_id={send_response.get_log_id()}",
                code=send_response.code,
                log_id=send_response.get_log_id(),
            )
        if send_response.data is None or not send_response.data.message_id:
            raise LookupError(
                "卡片发送响应缺少可回读标识 message_id："
                f"code={send_response.code} msg={send_response.msg} "
                f"log_id={send_response.get_log_id()}"
            )
        return AdminCardCreated(card_id=card_id, message_id=send_response.data.message_id)

    def update(self, *, card_id: str, card: RenderedConfirmCard) -> None:
        """把已建好的卡片整体替换为新内容（用于终态更新：去掉按钮、展示结果）。

        **SDK 精确方法名未经真实验证**（证据等级 1）：``lark_oapi.api.cardkit.v1``
        是否确实提供 ``UpdateCardRequest``/``card.update`` 这个方法名、字段是否叫
        ``card``，依据的是与 ``CreateCardRequest``/``CreateCardRequestBody`` 同一
        命名惯例的推断，尚未在真实 SDK 上核实——留给 `biai-stage` L4a：若方法名
        或字段名对不上，只需要改这一个方法体，不影响 ``AdminCardTransport``
        Protocol 或调用方（``core/admin/card_dispatch.py``、
        ``core/admin/card_callback.py``）的任何一行。
        """

        from lark_oapi.api.cardkit.v1 import UpdateCardRequest, UpdateCardRequestBody

        request = (
            UpdateCardRequest.builder()
            .card_id(card_id)
            .request_body(
                UpdateCardRequestBody.builder()
                .card(json.dumps(_card_payload(card), ensure_ascii=False))
                .build()
            )
            .build()
        )
        response = self._client.cardkit.v1.card.update(request)
        if not response.success():
            raise AdminCardDeliveryRejected(
                f"卡片更新失败：code={response.code} msg={response.msg} "
                f"log_id={response.get_log_id()}",
                code=response.code,
                log_id=response.get_log_id(),
            )
