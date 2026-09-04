"""管理员确认卡片的真实出站：实现 ``core.admin.notification.AdminCardTransport``。

``lark_oapi`` 在函数内延迟导入：不碰这个类的测试无需装 SDK。卡片 JSON 的
渲染住在不 import 任何飞书 SDK 的 ``core/admin/notification.py``（代码框架
第二节不得被 ``adapters/`` 持有）；本文件只从那里导入复用。

**本模块的真实行为未验证（证据等级 1）**：全部断言跑在注入的假实现上，真实
CardKit 字段与真实回调闭环属 `biai-stage` L4a 受控验收。

**已知边界（L4a 未验证）**：``create()`` 的 ``chat_id`` 参数从未在方法体内
使用，真正决定卡片送到哪个私聊的是 ``reply_to_message_id``；"卡片只会出现
在发起管理员本人与机器人的私聊里"这条安全性质因此依赖对飞书回复接口真实
路由行为的假设，尚未核实，本模块结构上也做不到额外的收件人核对。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from lingxi.core.admin.management_card import ManagementCardCreated
from lingxi.core.admin.notification import (
    AdminCardCreated,
    AdminCardDeliveryRejected,
    RenderedConfirmCard,
    render_card_payload,
)

logger = logging.getLogger(__name__)


def _create_card(client: Any, payload: dict[str, Any]) -> str:
    """建一张卡片，返回 ``card_id``。"""
    from lark_oapi.api.cardkit.v1 import CreateCardRequest, CreateCardRequestBody

    create_request = (
        CreateCardRequest.builder()
        .request_body(
            CreateCardRequestBody.builder()
            .type("card_json")
            .data(json.dumps(payload, ensure_ascii=False))
            .build()
        )
        .build()
    )
    create_response = client.cardkit.v1.card.create(create_request)
    if not create_response.success():
        raise AdminCardDeliveryRejected(
            f"建卡失败：code={create_response.code} msg={create_response.msg} "
            f"log_id={create_response.get_log_id()}",
            code=create_response.code,
            log_id=create_response.get_log_id(),
        )
    if create_response.data is None or not create_response.data.card_id:
        # 结果不明：拿不到可回读标识，不属于 AdminCardDeliveryRejected 那个
        # 白名单判别姿态。
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
    return send_response.data.message_id


def _create_and_reply_card(
    client: Any, *, payload: dict[str, Any], thread_id: str | None, reply_to_message_id: str
) -> tuple[str, str]:
    """建卡并作为回复消息发出，返回 ``(card_id, message_id)``。

    :class:`LarkAdminCardTransport` 与 :class:`LarkAdminManagementCardTransport`
    的 ``create()`` 共用这套 CardKit 建卡 + 回复发送流程，只是卡片载荷不同。
    """
    card_id = _create_card(client, payload)
    message_id = _reply_with_card(
        client, card_id=card_id, thread_id=thread_id, reply_to_message_id=reply_to_message_id
    )
    return card_id, message_id


class LarkAdminCardTransport:
    """实现 ``core.admin.notification.AdminCardTransport``。"""

    def __init__(self, client: Any) -> None:
        """持有真实 lark_oapi 客户端。"""
        self._client = client

    def create(
        self,
        *,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
        card: RenderedConfirmCard,
    ) -> AdminCardCreated:
        """建卡并作为消息发出，回复触发命令的那条消息。

        卡片因此结构上只会出现在发起管理员本人与机器人的私聊里——由"回复
        同一条私聊消息"这个机制天然保证，不依赖额外的收件人校验。
        """
        card_id, message_id = _create_and_reply_card(
            self._client,
            payload=render_card_payload(card),
            thread_id=thread_id,
            reply_to_message_id=reply_to_message_id,
        )
        return AdminCardCreated(card_id=card_id, message_id=message_id)

    def update(self, *, card_id: str, sequence: int, card: RenderedConfirmCard) -> None:
        """把已建好的卡片整体替换为新内容（用于终态更新：去掉按钮、展示结果）。

        ``UpdateCardRequestBody.card`` 的类型是 ``Card``（带 ``type``/``data``
        两个字符串字段的独立模型），不是像建卡那样直接接受 JSON 字符串。
        CardKit 要求同一 ``card_id`` 的每次更新调用携带严格递增的
        ``sequence``（与 ``adapters/feishu_delivery.py`` 的 ``update()``/
        ``close()`` 同一要求），调用方通过
        ``PostgresPendingActionStore.next_card_sequence()`` 换取本次要用的号。
        载荷形状已用真实 SDK 本地构造核实（不发请求），真实网络往返仍未验证。
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
    """实现 ``core.admin.management_card.CompanyMetricCatalog``：真实公司/指标下拉选项目录。

    读取 ``config/company_function_metric_map.toml``；每次调用现读文件、不
    缓存，读哪一份由装配层注入（``metric_map_path``，无默认值，``None``
    是显式的"这台机器没配外置文件"）。失败关闭的形状是**空目录**，不是
    "随包默认"——不会用一份"看起来正常、其实是另一份真相"的内容顶替；
    真正会写出权限的路径各自独立读一次并各自失败关闭。
    """

    def __init__(self, *, metric_map_path: Path | None) -> None:
        """记录外置映射文件路径；``None`` 表示这台机器没配，用随包默认。"""
        self._metric_map_path = metric_map_path

    def companies(self) -> Sequence[str]:
        """返回可选择的公司编号。"""
        mapping = self._load()
        return tuple(sorted(key for key in mapping if key != "*"))

    def metrics(self) -> Sequence[str]:
        """返回可选择的指标 ID（跨全部公司/职能去重）。"""
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
        except Exception as error:  # 管理卡降级为空目录
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
            return dict(load_company_function_metric_map(self._metric_map_path))
        except Exception as error:  # 展示层降级，不让管理卡渲染失败
            logger.warning(
                "admin.management_card.catalog_load_failed error=%s", type(error).__name__
            )
            return {}


class LarkAdminManagementCardTransport:
    """实现 ``core.admin.management_card.ManagementCardTransport``：真实建卡并作为消息发出。

    与 :class:`LarkAdminCardTransport.create` 同构；``update()`` 用于在原
    卡片实体上刷新懒过期与异步下发状态。**真实行为未验证（证据等级 1）**：
    真实 CardKit 是否接受 ``select_static``/``input``/``form`` 三种组件、
    真实点击是否正确触发回调，均属 `biai-stage` L4a 受控验收，字段形状
    完全依据飞书公开文档撰写，尚未被真实探针核实过。
    """

    def __init__(self, client: Any) -> None:
        """持有真实 lark_oapi 客户端。"""
        self._client = client

    def create(
        self,
        *,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
        card: dict[str, Any],
    ) -> ManagementCardCreated:
        """建卡并作为消息发出，回复触发 ``/admin user`` 命令的那条消息。

        与 :meth:`LarkAdminCardTransport.create` 同一机制，卡片因此结构上
        只会出现在发起管理员本人与机器人的私聊里。
        """
        card_id, message_id = _create_and_reply_card(
            self._client, payload=card, thread_id=thread_id, reply_to_message_id=reply_to_message_id
        )
        return ManagementCardCreated(card_id=card_id, message_id=message_id)

    def update(self, *, card_id: str, sequence: int, card: dict[str, Any]) -> None:
        """原地替换管理卡；``sequence`` 由持久上下文存储原子递增。"""
        from lark_oapi.api.cardkit.v1 import Card, UpdateCardRequest, UpdateCardRequestBody

        request = (
            UpdateCardRequest.builder()
            .card_id(card_id)
            .request_body(
                UpdateCardRequestBody.builder()
                .card(
                    Card.builder()
                    .type("card_json")
                    .data(json.dumps(card, ensure_ascii=False))
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
                f"管理卡更新失败：code={response.code} msg={response.msg} "
                f"log_id={response.get_log_id()}",
                code=response.code,
                log_id=response.get_log_id(),
            )
