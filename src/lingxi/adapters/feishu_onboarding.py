"""把飞书私聊与首次开通规则连接起来。

本模块只负责用户可见的引导和按钮回调。授权成功后的身份资料仍必须由
飞书授权回调完整返回后才可写入；这里绝不根据消息或按钮建立用户记录。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

from lingxi.core.identity.onboarding import OnboardingResponse, OnboardingService, ResponseKind


class CardSender(Protocol):
    def send_card(self, chat_id: str, card: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class IncomingPrivateMessage:
    chat_id: str
    sender_open_id: str
    content: str
    chat_type: str = "p2p"
    message_type: str = "text"


class OnboardingCards:
    """只放用户能看见的开通卡片，避免在外部回调中拼接业务状态。"""

    @staticmethod
    def full_guide() -> dict[str, Any]:
        return OnboardingCards._card(
            title="欢迎使用灵犀",
            content="灵犀会在你确认后，完成企业身份与已获批准数据范围的开通。开通前发送的业务内容不会被保存或执行。",
            actions=(
                OnboardingCards._callback_button("开始使用", "primary", "start"),
                OnboardingCards._callback_button("暂不需要", "default", "decline"),
            ),
        )

    @staticmethod
    def short_guide() -> dict[str, Any]:
        return OnboardingCards._card(
            title="灵犀",
            content="需要时，点击“开始使用”即可完成开通。",
            actions=(OnboardingCards._callback_button("开始使用", "primary", "start"),),
        )

    @staticmethod
    def authorization_required() -> dict[str, Any]:
        return OnboardingCards._card(
            title="准备开通",
            content="你已确认开始使用。下一步需要完成飞书授权；在授权完成前，灵犀不会创建用户记录。",
            actions=(),
        )

    @staticmethod
    def _callback_button(label: str, button_type: str, action: str) -> dict[str, Any]:
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": button_type,
            "width": "default",
            "size": "medium",
            "behaviors": [{"type": "callback", "value": {"action": action}}],
        }

    @staticmethod
    def _card(title: str, content: str, actions: tuple[dict[str, Any], ...]) -> dict[str, Any]:
        elements: list[dict[str, Any]] = [
            {"tag": "markdown", "content": content, "text_align": "left"},
        ]
        if actions:
            elements.append({"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px", "columns": [
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [action]} for action in actions
            ]})
        return {
            "schema": "2.0",
            "body": {"direction": "vertical", "elements": elements},
            "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
        }


class FeishuOnboardingController:
    """将可验证的领域结果转为飞书卡片。"""

    def __init__(self, service: OnboardingService, sender: CardSender) -> None:
        self._service = service
        self._sender = sender

    def receive_private_message(self, message: IncomingPrivateMessage) -> None:
        if message.chat_type != "p2p" or message.message_type != "text":
            return
        response = self._service.receive_message(message.sender_open_id, message.content)
        self._sender.send_card(message.chat_id, self._card_for(response))

    def receive_card_action(self, chat_id: str, open_id: str, action: str) -> dict[str, Any]:
        if action == "decline":
            response = self._service.decline_guide(open_id)
            return self._card_for(response)
        if action == "start":
            response = self._service.confirm_start(open_id, "开始使用")
            return self._card_for(response)
        return OnboardingCards.short_guide()

    @staticmethod
    def _card_for(response: OnboardingResponse) -> dict[str, Any]:
        if response.kind == ResponseKind.FULL_GUIDE:
            return OnboardingCards.full_guide()
        if response.kind == ResponseKind.SHORT_GUIDE:
            return OnboardingCards.short_guide()
        if response.kind == ResponseKind.AUTHORIZATION_REQUIRED:
            return OnboardingCards.authorization_required()
        return OnboardingCards.short_guide()


class LarkCardSender:
    """真实发送器；SDK 只在运行时导入，使基础逻辑测试不依赖外网。"""

    def __init__(self, app_id: str, app_secret: str) -> None:
        import lark_oapi as lark

        self._lark = lark
        self._client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()

    def send_card(self, chat_id: str, card: dict[str, Any]) -> None:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(json.dumps(card, ensure_ascii=False))
                .build()
            )
            .build()
        )
        response = self._client.im.v1.message.create(request)
        if not response.success():
            raise RuntimeError("飞书未确认开通引导已送达")


def run_long_connection_bot() -> None:
    """在 biai-stage 运行 Bot-Test 长连接。"""

    app_id = os.environ["FEISHU_APP_ID"]
    app_secret = os.environ["FEISHU_APP_SECRET"]
    import lark_oapi as lark
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTrigger,
        P2CardActionTriggerResponse,
    )

    from lingxi.core.identity.onboarding import InMemoryOnboardingStore

    controller = FeishuOnboardingController(OnboardingService(InMemoryOnboardingStore()), LarkCardSender(app_id, app_secret))

    def receive_message(data: Any) -> None:
        event = data.event
        if event is None or event.message is None or event.sender is None or event.sender.sender_id is None:
            return
        message_content = json.loads(event.message.content or "{}")
        controller.receive_private_message(
            IncomingPrivateMessage(
                chat_id=event.message.chat_id or "",
                sender_open_id=event.sender.sender_id.open_id or "",
                content=message_content.get("text", ""),
                chat_type=event.message.chat_type or "",
                message_type=event.message.message_type or "",
            )
        )

    def receive_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        event = data.event
        if event is None or event.operator is None or event.context is None or event.action is None:
            return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "未识别本次操作"}})
        value = event.action.value or {}
        card = controller.receive_card_action(event.context.open_chat_id or "", event.operator.open_id or "", value.get("action", ""))
        return P2CardActionTriggerResponse({"card": {"type": "raw", "data": card}})

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(receive_message)
        .register_p2_card_action_trigger(receive_action)
        .build()
    )
    lark.ws.Client(app_id, app_secret, event_handler=event_handler, log_level=lark.LogLevel.INFO).start()


if __name__ == "__main__":
    run_long_connection_bot()
