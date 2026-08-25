"""管理员确认卡片的真实出站：实现 ``core.admin.notification.AdminCardTransport``。

``lark_oapi`` 在函数内延迟导入，与仓库既有惯例一致（``adapters/feishu_outbound.py``、
``adapters/feishu_delivery.py``）：不碰这个类的测试无需装 SDK。

与 ``adapters/feishu_delivery.LarkCardTransport`` 同构（同一套 CardKit 建卡 + 回复
发送、``DeliveryRejected`` 白名单判别姿态），但渲染的是确认卡片专属的按钮回调形状
（见 ``core/admin/notification.py`` 模块文档"为什么不复用 RenderedCard"）。

**本模块的真实行为未验证（证据等级 1）。** 全部 L2 断言跑在注入的假实现上；真实
CardKit 字段与真实回调闭环属 `biai-stage` L4a 受控验收（本 Story 明确留待验收窗口，
见 PR 描述"未验证事项"）。红线（v13 §6.4 第 7 条）：测试中一律假 transport，不真实
发送任何飞书卡片。批次二审查修复（外部审查交叉裁定，opus P2-1）把 ``update()`` 的
载荷形状（``card.type``/``card.data``/``sequence``）用真实 SDK 在本机做了离线构造
核实（不发请求），比"未经验证的推断"更进一步，但仍不构成真实网络往返的 L4a 证据。

**2026-08-25 建卡环节已被真实探针证伪并修复**：产品负责人真实走查
``/admin suspend`` 时确认卡片发送失败（用户看到"确认卡片发送失败，本次操作不会
执行"，失败关闭正确）。编排者随后用受控探针在 `biai-stage` 对 CardKit
``POST /open-apis/cardkit/v1/cards`` 实测定位：此前 ``_card_payload()`` 把按钮包进
``{"tag": "action", "actions": [...]}`` 容器，这个形状被真实 CardKit 拒绝——
``code=200861`` ``msg="cards of schema V2 no longer support this capability"``
``ErrPath: elements->[1](tag: action)``（schema 2.0 已不支持 action 容器元素）。
同一轮探针里，纯 markdown 建卡成功，按钮直接作为 ``body.elements`` 顶层元素（不套
action 容器）建卡同样成功——``behaviors`` 回调式与旧式直接在按钮 ``value`` 字段上
写值两种形态都被接受，两个按钮并列直挂也成功。``_card_payload()`` 据此把按钮从
action 容器移到顶层元素，回调形态选用飞书《配置卡片交互》文档描述的 2.0 标准写法
``"behaviors": [{"type": "callback", "value": {...}}]``
（https://open.feishu.cn/document/feishu-cards/configuring-card-interactions），
而不是探针里同样能通过、但不是文档标准形态的"按钮顶层直接放 value"写法。飞书
《卡片回传交互回调》文档
（https://open.feishu.cn/document/feishu-cards/card-callback-communication）的
示例代码把 ``behaviors`` 里的自定义回传参数标注为回调事件 ``action.value`` 字段的
来源，``adapters/feishu_events.parse_card_action_event`` 读的正是这个路径，形状上
不需要改动（该函数文档记录了这条核实）。**这次探针与文档核实只覆盖建卡请求本身
能否被接受、字段路径是否对应——不覆盖真实点击是否真的触发回调、事件体是否逐字
符合文档描述、卡片视觉表现是否符合预期**，这些仍是本段落上下两处已经登记的
`biai-stage` L4a 受控验收范围，本次改动不改变它们的证据等级。

**已知边界（L4a 未验证）**：:meth:`LarkAdminCardTransport.create` 的 ``chat_id`` 参数
在方法体内从未被使用——真正决定卡片送到哪个私聊的是 ``reply_to_message_id``（回复
触发这条命令的那条消息），``chat_id`` 只是 Protocol 签名里携带的展示性参数（与
``adapters/feishu_delivery.py`` 的既有取舍相同）。"卡片结构上只会出现在发起管理员
本人与机器人的私聊里"这条安全性质，因此完全依赖"飞书的回复接口确实总是把回复投递到
被回复消息所在的那个会话，不存在别的路由方式"这条对真实 API 行为的假设——这个假设
尚未在真实 SDK/API 上核实，留给 `biai-stage` L4a；本模块结构上不做、也做不到额外的
收件人核对（Protocol 层面拿不到"这条消息原本属于哪个会话"以外的信息）。
"""

from __future__ import annotations

import json
from typing import Any

from lingxi.core.admin.notification import AdminCardCreated, AdminCardDeliveryRejected, RenderedConfirmCard


def _card_payload(card: RenderedConfirmCard) -> dict[str, Any]:
    """CardKit JSON 2.0 的 ``data`` 载荷。

    按钮是 ``body.elements`` 的顶层元素，不套 ``{"tag": "action", "actions": [...]}``
    容器——后者已被真实 CardKit 拒绝（``code=200861``），见模块文档"2026-08-25
    建卡环节已被真实探针证伪并修复"。回调形态用
    ``"behaviors": [{"type": "callback", "value": {...}}]``（飞书文档《配置卡片
    交互》描述的 2.0 标准写法），``value`` 内容仍是 ``pending_action_id``/
    ``decision`` 两个键，只是回传数据的落点从按钮自身的 ``value`` 属性挪到了
    ``behaviors[0]["value"]``——回调事件里 ``action.value`` 是否原样等于这里写入的
    内容，字段路径已经过飞书文档核实（见
    ``adapters/feishu_events.parse_card_action_event`` 文档），真实点击触发的事件体
    是否逐字符合文档描述仍是 L4a 未验证项。

    ``buttons`` 为空（终态卡片）时只有一个 markdown 元素、没有任何按钮元素——这就是
    "卡片更新为不可再次操作的最终状态"在 CardKit 层面的落点：终态卡片结构上不存在
    任何可点击的按钮，不是靠禁用态按钮或前端约定"这张卡片已经不能点了"。
    """

    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": f"**{card.title}**\n\n{card.body}"}
    ]
    for button in card.buttons:
        elements.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": button.label},
                "type": "primary" if button.value.get("decision") == "confirm" else "default",
                "behaviors": [
                    {
                        "type": "callback",
                        # 按钮回传值：飞书文档标注 behaviors 里的 value 会原样出现在
                        # card.action.trigger 事件的 action.value 字段（见函数文档、
                        # adapters/feishu_events.parse_card_action_event）。
                        "value": dict(button.value),
                    }
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

    def update(self, *, card_id: str, sequence: int, card: RenderedConfirmCard) -> None:
        """把已建好的卡片整体替换为新内容（用于终态更新：去掉按钮、展示结果）。

        **载荷形状与 sequence 已用真实 SDK（``lark-oapi==1.7.1``）本地构造核实**
        （外部审查交叉裁定，opus P2-1；不发真实请求，见类文档红线）：
        ``UpdateCardRequestBody.card`` 的类型是 ``Card``（一个带 ``type``/``data``
        两个字符串字段的独立模型，``lark_oapi/api/cardkit/v1/model/card.py``），
        不是像 ``CreateCardRequestBody.data`` 那样直接接受 JSON 字符串——此前这里
        错误地把整份 JSON 字符串直接传给 ``.card()``，且完全没有调用
        ``.sequence()``。CardKit 要求同一 ``card_id`` 的每次更新调用携带严格递增
        的 ``sequence``（与 ``adapters/feishu_delivery.py`` 的 ``update()``/
        ``close()`` 同一要求，那两处一直正确地传了 ``sequence``），调用方
        （``core/admin/card_callback.py``）通过
        ``PostgresPendingActionStore.next_card_sequence()`` 换取本次要用的号。
        真实网络往返仍未验证，留给 `biai-stage` L4a。
        """

        from lark_oapi.api.cardkit.v1 import Card, UpdateCardRequest, UpdateCardRequestBody

        request = (
            UpdateCardRequest.builder()
            .card_id(card_id)
            .request_body(
                UpdateCardRequestBody.builder()
                .card(
                    Card.builder()
                    .type("card_json")
                    .data(json.dumps(_card_payload(card), ensure_ascii=False))
                    .build()
                )
                .sequence(sequence)
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
