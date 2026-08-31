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

**同日晚些时候：``_card_payload()`` 迁出本文件**（Issue #96 卡片回调应答修复，载体
#96）：``card.action.trigger`` 回调的应答帧需要携带同一份终态卡 JSON（见
``core/admin/card_callback.py`` 的 ``handle()`` 文档），而应答的构造方按代码框架
第二节不得 import ``adapters/``。函数改名为 ``render_card_payload()``，迁移到不 import
任何飞书 SDK 的 ``core/admin/notification.py``；本文件的 ``create()``/``update()``
改为从那里导入复用，不再持有本地定义——上面两段历史记叙提到的 ``_card_payload()``
就是这个函数搬家前的名字，行为未变。

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
import logging
from typing import Any, Sequence

from lingxi.core.admin.management_card import ManagementCardCreated
from lingxi.core.admin.notification import (
    AdminCardCreated,
    AdminCardDeliveryRejected,
    RenderedConfirmCard,
    render_card_payload,
)

logger = logging.getLogger(__name__)


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
                .data(json.dumps(render_card_payload(card), ensure_ascii=False))
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
                    .data(json.dumps(render_card_payload(card), ensure_ascii=False))
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


class TomlCompanyMetricCatalog:
    """实现 ``core.admin.management_card.CompanyMetricCatalog``：真实公司/指标
    下拉选项目录（#439 B 档），读取 ``config/company_function_metric_map.toml``
    （``adapters/company_function_metric_map_file.py``，已有、本 Story 未改动）。

    放在本文件而不是 ``adapters/admin_registry.py``：本类不需要数据库连接
    （``config/company_function_metric_map.toml`` 是随包发布的静态文件），语义上
    更接近"管理卡渲染所需的一个只读目录源"，与本文件其余"管理卡出站"职责同一
    分组；`admin_registry.py` 的 ``PostgresAdminQueries`` 保持只装配需要 DSN 的
    真实查询，不额外装配一个不需要 DSN 的静态文件读取器。

    **每次调用现读文件，不缓存**——与 ``adapters/admin_metric_alias_map_file.py``
    同一取舍（管理命令面低频，现读成本可忽略，换来编辑映射表立即生效，不需要
    重启 gateway）。读取或格式失败时返回空元组（fail-open，见
    ``core.admin.management_card.render_management_card`` 对空目录的降级渲染），
    不让整张管理卡因为一份可选的展示数据渲染失败。
    """

    def companies(self) -> Sequence[str]:
        mapping = self._load()
        return tuple(sorted(key for key in mapping if key != "*"))

    def metrics(self) -> Sequence[str]:
        mapping = self._load()
        metrics: set[str] = set()
        for functions in mapping.values():
            for values in functions.values():
                metrics.update(values)
        return tuple(sorted(metrics))

    def positions(self) -> Sequence[str]:
        """返回可选择的银河职位名；必须是映射文件中的精确 key。"""

        from lingxi.adapters.role_function_map_file import load_role_function_map

        try:
            return tuple(sorted(load_role_function_map().keys()))
        except Exception as error:  # noqa: BLE001 - 管理卡降级为空目录
            logger.warning(
                "admin.management_card.position_catalog_load_failed error=%s",
                type(error).__name__,
            )
            return ()

    def _load(self) -> dict[str, dict[str, tuple[str, ...]]]:
        from lingxi.adapters.company_function_metric_map_file import (
            load_company_function_metric_map,
        )

        try:
            return dict(load_company_function_metric_map())
        except Exception as error:  # noqa: BLE001 - 展示层降级，不让管理卡渲染失败
            logger.warning(
                "admin.management_card.catalog_load_failed error=%s", type(error).__name__
            )
            return {}


class LarkAdminManagementCardTransport:
    """实现 ``core.admin.management_card.ManagementCardTransport``：真实建卡并
    作为消息发出（#439 B 档）。与 :class:`LarkAdminCardTransport.create` 同构
    （同一套 CardKit 建卡 + 回复发送、``DeliveryRejected`` 白名单判别姿态）；
    ``update()`` 用于在原卡片实体上刷新 #493 的懒过期与异步下发状态。

    **本模块的真实行为未验证（证据等级 1）**：全部断言跑在注入的假实现上；真实
    CardKit 是否接受 ``select_static``/``input``/``form`` 三种组件、真实点击是否
    正确触发回调，均属 `biai-stage` L4a 受控验收（本 Story 未验证，见报告"未验证
    事项"）——与 ``LarkAdminCardTransport`` 当年在 2026-08-25 之前的状态相同（那次
    真实探针只验证过按钮/markdown 两种组件，见类文档），本类新增的三种组件字段
    形状完全依据飞书公开文档撰写，尚未被同等强度的真实探针核实过。
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def create(
        self,
        *,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
        card: dict[str, Any],
    ) -> ManagementCardCreated:
        """建卡并作为消息发出，回复触发 ``/admin user`` 命令的那条消息——与
        :meth:`LarkAdminCardTransport.create` 同一机制，卡片因此结构上只会出现在
        发起管理员本人与机器人的私聊里。"""

        from lark_oapi.api.cardkit.v1 import CreateCardRequest, CreateCardRequestBody
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

        create_request = (
            CreateCardRequest.builder()
            .request_body(
                CreateCardRequestBody.builder()
                .type("card_json")
                .data(json.dumps(card, ensure_ascii=False))
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
        return ManagementCardCreated(card_id=card_id, message_id=send_response.data.message_id)

    def update(self, *, card_id: str, sequence: int, card: dict[str, Any]) -> None:
        """原地替换管理卡；``sequence`` 由持久上下文存储原子递增。"""

        from lark_oapi.api.cardkit.v1 import Card, UpdateCardRequest, UpdateCardRequestBody

        request = (
            UpdateCardRequest.builder()
            .card_id(card_id)
            .request_body(
                UpdateCardRequestBody.builder()
                .card(Card.builder().type("card_json").data(json.dumps(card, ensure_ascii=False)).build())
                .sequence(sequence)
                .build()
            )
            .build()
        )
        response = self._client.cardkit.v1.card.update(request)
        if not response.success():
            raise AdminCardDeliveryRejected(
                f"管理卡更新失败：code={response.code} msg={response.msg} "
                f"log_id={response.get_log_id()}",
                code=response.code,
                log_id=response.get_log_id(),
            )
