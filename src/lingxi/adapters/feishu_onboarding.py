"""把飞书私聊与首次开通规则连接起来。

本模块只负责用户可见的引导和按钮回调。授权成功后的身份资料仍必须由
飞书授权回调完整返回后才可写入；这里绝不根据消息或按钮建立用户记录。
"""

from __future__ import annotations

import json
import os
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import urlencode, urlparse

from lingxi.core.identity.onboarding import OnboardingResponse, OnboardingService, ResponseKind


class CardSender(Protocol):
    def send_card(self, chat_id: str, card: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class IncomingPrivateMessage:
    chat_id: str
    sender_open_id: str
    content: str
    event_id: str
    chat_type: str = "p2p"
    message_type: str = "text"


class CardProgressStore(Protocol):
    def claim_inbound_event(self, event_id: str) -> bool: ...
    def create(self, open_id: str, chat_id: str, step: str) -> str: ...
    def valid(self, open_id: str, chat_id: str, nonce: str) -> bool: ...


class AuthorizationUrlFactory(Protocol):
    def create(self, state: str) -> str: ...


@dataclass(frozen=True)
class FeishuAuthorizationUrlFactory:
    """只构造用户可点的地址；授权码只会回到受控服务端。"""

    app_id: str
    redirect_uri: str
    scope: str

    def create(self, state: str) -> str:
        return "https://accounts.feishu.cn/open-apis/authen/v1/authorize?" + urlencode(
            {"client_id": self.app_id, "redirect_uri": self.redirect_uri, "state": state, "scope": self.scope}
        )


class InMemoryCardProgressStore:
    """单元测试替身；真实 Bot-Test 必须使用 PostgreSQL 实现。"""

    def __init__(self, key: str = "test-onboarding-state-key") -> None:
        self._key = key.encode()
        self._events: set[str] = set()
        self._cards: dict[str, tuple[str, str, str, datetime]] = {}

    def _hash(self, value: str) -> str:
        return hmac.new(self._key, value.encode(), hashlib.sha256).hexdigest()

    def claim_inbound_event(self, event_id: str) -> bool:
        if event_id in self._events:
            return False
        self._events.add(event_id)
        return True

    def create(self, open_id: str, chat_id: str, step: str) -> str:
        subject_hash, chat_hash = self._hash(open_id), self._hash(chat_id)
        for nonce, card in self._cards.items():
            if card[0] == subject_hash and card[1] == chat_hash and card[2] in {"authorizing", "processing"} and card[3] > datetime.now(timezone.utc):
                return nonce
        nonce = secrets.token_urlsafe(24)
        self._cards[nonce] = (subject_hash, chat_hash, step, datetime.now(timezone.utc) + timedelta(hours=24))
        return nonce

    def valid(self, open_id: str, chat_id: str, nonce: str) -> bool:
        card = self._cards.get(nonce)
        return bool(card and card[3] > datetime.now(timezone.utc) and hmac.compare_digest(card[0], self._hash(open_id)) and hmac.compare_digest(card[1], self._hash(chat_id)))


class OnboardingCards:
    """只放用户能看见的开通卡片，避免在外部回调中拼接业务状态。"""

    @staticmethod
    def full_guide(nonce: str) -> dict[str, Any]:
        return OnboardingCards._card(
            title="欢迎使用灵犀",
            content="灵犀会在你确认后，完成企业身份与已获批准数据范围的开通。开通前发送的业务内容不会被保存或执行。",
            actions=(
                OnboardingCards._callback_button("开始使用", "primary", "start", nonce),
                OnboardingCards._callback_button("暂不需要", "default", "decline", nonce),
            ),
        )

    @staticmethod
    def short_guide(nonce: str) -> dict[str, Any]:
        return OnboardingCards._card(
            title="灵犀",
            content="需要时，点击“开始使用”即可完成开通。",
            actions=(OnboardingCards._callback_button("开始使用", "primary", "start", nonce),),
        )

    @staticmethod
    def authorization_required(authorization_url: str) -> dict[str, Any]:
        return OnboardingCards._card(
            title="准备开通",
            content="你已确认开始使用。请点击下方按钮完成飞书授权；在授权完成前，灵犀不会创建用户记录。",
            actions=(OnboardingCards._link_button("去飞书授权", "primary", authorization_url),),
        )

    @staticmethod
    def _callback_button(label: str, button_type: str, action: str, nonce: str) -> dict[str, Any]:
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": button_type,
            "width": "default",
            "size": "medium",
            "behaviors": [{"type": "callback", "value": {"action": action, "nonce": nonce}}],
        }

    @staticmethod
    def _link_button(label: str, button_type: str, url: str) -> dict[str, Any]:
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": button_type,
            "width": "default",
            "size": "medium",
            "behaviors": [{"type": "open_url", "default_url": url}],
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

    def __init__(self, service: OnboardingService, sender: CardSender, progress: CardProgressStore, authorization_url: AuthorizationUrlFactory | None = None) -> None:
        self._service = service
        self._sender = sender
        self._progress = progress
        self._authorization_url = authorization_url

    def receive_private_message(self, message: IncomingPrivateMessage) -> None:
        if message.chat_type != "p2p" or message.message_type != "text":
            return
        if not self._progress.claim_inbound_event(message.event_id):
            return
        response = self._service.receive_message(message.sender_open_id, message.content)
        self._sender.send_card(message.chat_id, self._card_for(response, message.sender_open_id, message.chat_id))

    def receive_card_action(self, chat_id: str, open_id: str, action: str, nonce: str) -> dict[str, Any] | None:
        if not self._progress.valid(open_id, chat_id, nonce):
            return None
        if action == "decline":
            response = self._service.decline_guide(open_id)
            return self._card_for(response, open_id, chat_id)
        if action == "start":
            response = self._service.confirm_start(open_id, "开始使用")
            return self._card_for(response, open_id, chat_id)
        return None

    def _card_for(self, response: OnboardingResponse, open_id: str, chat_id: str) -> dict[str, Any]:
        if response.kind == ResponseKind.FULL_GUIDE:
            return OnboardingCards.full_guide(self._progress.create(open_id, chat_id, "guide"))
        if response.kind == ResponseKind.SHORT_GUIDE:
            return OnboardingCards.short_guide(self._progress.create(open_id, chat_id, "declined"))
        if response.kind == ResponseKind.AUTHORIZATION_REQUIRED:
            if self._authorization_url is None:
                raise RuntimeError("未配置安全授权回跳地址，不能开始授权")
            nonce = self._progress.create(open_id, chat_id, "authorizing")
            return OnboardingCards.authorization_required(self._authorization_url.create(nonce))
        return OnboardingCards.short_guide(self._progress.create(open_id, chat_id, "declined"))


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

    from lingxi.adapters.postgres_onboarding import PostgresCardProgressStore
    from lingxi.adapters.oauth_bridge import FeishuOAuthIdentityLoader, OAuthBridgeClient, OAuthResultProcessor

    dsn = os.environ.get("LINGXI_POSTGRES_DSN")
    state_key = os.environ.get("LINGXI_ONBOARDING_STATE_KEY")
    redirect_uri = os.environ.get("LINGXI_OAUTH_REDIRECT_URI")
    oauth_scope = os.environ.get("LINGXI_OAUTH_SCOPE")
    bridge_url = os.environ.get("LINGXI_OAUTH_BRIDGE_URL")
    bridge_token = os.environ.get("LINGXI_OAUTH_BRIDGE_TOKEN")
    if not dsn or not state_key or not redirect_uri or not oauth_scope or not bridge_url or not bridge_token:
        raise RuntimeError("Bot-Test 入口缺少 PostgreSQL 或安全授权回跳配置，不能安全启动")
    debug_identity_display = os.environ.get("LINGXI_OAUTH_DEBUG_IDENTITY_DISPLAY") == "enabled"
    if debug_identity_display and urlparse(redirect_uri).netloc != "biai-test.chunbai.com":
        raise RuntimeError("身份资料页面调试只允许 biai-test 回跳地址")
    persistent_store = PostgresCardProgressStore(dsn, state_key)
    bridge = OAuthBridgeClient(bridge_url, bridge_token)
    processor = OAuthResultProcessor(
        persistent_store,
        OnboardingService(persistent_store),
        FeishuOAuthIdentityLoader(app_id, app_secret, redirect_uri, debug_identity_display=debug_identity_display),
        bridge,
        state_key,
        debug_identity_display=debug_identity_display,
    )
    bridge.set_processor(processor)
    bridge.start()
    controller = FeishuOnboardingController(
        OnboardingService(persistent_store),
        LarkCardSender(app_id, app_secret),
        persistent_store,
        FeishuAuthorizationUrlFactory(app_id, redirect_uri, oauth_scope),
    )

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
                event_id=getattr(getattr(data, "header", None), "event_id", "") or "",
                chat_type=event.message.chat_type or "",
                message_type=event.message.message_type or "",
            )
        )

    def receive_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        event = data.event
        if event is None or event.operator is None or event.context is None or event.action is None:
            return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "未识别本次操作"}})
        value = event.action.value or {}
        card = controller.receive_card_action(event.context.open_chat_id or "", event.operator.open_id or "", value.get("action", ""), value.get("nonce", ""))
        if card is None:
            return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "该入口已失效或不属于当前私聊，请重新发消息开始"}})
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
