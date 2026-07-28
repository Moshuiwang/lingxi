"""biai-stage 到 OAuth Worker 的仅出站桥接。

Worker 只中转一次性 code；本模块在 biai-stage 完成飞书身份读取和本地核验，
绝不记录 code、访问令牌或刷新令牌。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import threading
import time
from urllib.request import Request, urlopen
from dataclasses import dataclass
from typing import Callable, Protocol

from lingxi.core.identity.onboarding import IdentityProfile, OnboardingService


logger = logging.getLogger(__name__)


class AuthorizationStateStore(Protocol):
    def claim_authorizing_state(self, state: str) -> bool: ...
    def complete_authorizing_state(self, state: str, open_id: str) -> bool: ...
    def cancel_authorizing_state(self, state: str) -> bool: ...


class IdentityLoader(Protocol):
    def from_authorization_code(self, code: str) -> IdentityProfile: ...


class OAuthBridgeResultSender(Protocol):
    def send_result(self, state: str, status: str) -> None: ...


class FeishuOAuthIdentityLoader:
    """只在 biai-stage 用一次性 code 换取身份；令牌绝不离开本方法。"""

    def __init__(self, app_id: str, app_secret: str, redirect_uri: str) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._redirect_uri = redirect_uri

    @staticmethod
    def _json(request: Request) -> dict[str, object]:
        with urlopen(request, timeout=10) as response:  # noqa: S310 -- 固定飞书 HTTPS 地址
            value = json.loads(response.read())
        if not isinstance(value, dict):
            raise ValueError("飞书授权响应格式异常")
        return value

    def from_authorization_code(self, code: str) -> IdentityProfile:
        token_request = Request(
            "https://open.feishu.cn/open-apis/authen/v2/oauth/token",
            data=json.dumps({"grant_type": "authorization_code", "client_id": self._app_id, "client_secret": self._app_secret, "code": code, "redirect_uri": self._redirect_uri}).encode(),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        token = self._json(token_request)
        if token.get("code") not in (0, "0") or not isinstance(token.get("access_token"), str):
            raise ValueError("飞书未确认本次授权")
        info = self._json(Request("https://passport.feishu.cn/suite/passport/oauth/userinfo", headers={"Authorization": f"Bearer {token['access_token']}"}))
        data = info.get("data", info)
        if not isinstance(data, dict):
            raise ValueError("飞书身份资料格式异常")
        return IdentityProfile(
            str(data.get("open_id") or ""), str(data.get("user_id") or ""), str(data.get("union_id") or ""), str(data.get("name") or ""),
            str(data["department"]) if data.get("department") else None,
            str(data["tenant_key"]) if data.get("tenant_key") else None,
            str(data["locale"]) if data.get("locale") else None,
        )


@dataclass(frozen=True)
class OAuthBridgeMessage:
    type: str
    state: str
    code: str | None = None

    @classmethod
    def parse(cls, raw: str) -> "OAuthBridgeMessage":
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("type") not in {"oauth_code", "oauth_cancelled"}:
            raise ValueError("未识别的 OAuth 桥接消息")
        state = value.get("state")
        if not isinstance(state, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", state):
            raise ValueError("无效的 OAuth 状态")
        code = value.get("code")
        if value["type"] == "oauth_code" and (not isinstance(code, str) or not code):
            raise ValueError("授权结果缺少一次性 code")
        return cls(value["type"], state, code)


class OAuthResultProcessor:
    """每次结果都先验证原用户绑定，再允许建立身份。"""

    def __init__(self, store: AuthorizationStateStore, service: OnboardingService, loader: IdentityLoader, result_sender: OAuthBridgeResultSender, event_key: str) -> None:
        self._store = store
        self._service = service
        self._loader = loader
        self._result_sender = result_sender
        self._event_key = event_key.encode()

    def process(self, message: OAuthBridgeMessage) -> None:
        if message.type == "oauth_cancelled":
            self._store.cancel_authorizing_state(message.state)
            self._result_sender.send_result(message.state, "retry")
            return

        try:
            if not self._store.claim_authorizing_state(message.state):
                self._result_sender.send_result(message.state, "retry")
                return
            profile = self._loader.from_authorization_code(message.code or "")
            if not self._store.complete_authorizing_state(message.state, profile.open_id):
                self._result_sender.send_result(message.state, "retry")
                return
            event_id = "oauth:" + hmac.new(self._event_key, (message.code or "").encode(), hashlib.sha256).hexdigest()
            self._service.authorization_succeeded(event_id, profile)
        except Exception as error:
            # 无论外部授权失败、资料不完整或连接异常，都不把细节暴露给用户。
            # 仅记录异常类别，便于受控验收定位；绝不记录 code、令牌或身份资料。
            logger.warning("OAuth identity load failed: %s", type(error).__name__)
            self._store.cancel_authorizing_state(message.state)
            self._result_sender.send_result(message.state, "retry")
            return
        self._result_sender.send_result(message.state, "identity_confirmed")


class OAuthBridgeClient:
    """可自动重连的出站 WebSocket；连接断开不缓存授权结果。"""

    def __init__(self, url: str, token: str, processor: OAuthResultProcessor | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        self._url = url
        self._token = token
        self._processor = processor
        self._sleep = sleep
        self._stop = threading.Event()
        self._socket: object | None = None

    def set_processor(self, processor: OAuthResultProcessor) -> None:
        self._processor = processor

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run_forever, name="lingxi-oauth-bridge", daemon=True)
        thread.start()
        return thread

    def run_forever(self) -> None:
        from websockets.sync.client import connect

        while not self._stop.is_set():
            try:
                with connect(self._url, additional_headers={"Authorization": f"Bearer {self._token}"}, open_timeout=10) as socket:
                    self._socket = socket
                    for raw in socket:
                        if self._stop.is_set():
                            break
                        try:
                            if self._processor is not None:
                                self._processor.process(OAuthBridgeMessage.parse(raw))
                        except ValueError:
                            continue
            except Exception:
                # 断线仅触发重连；不输出凭据、授权码或身份资料。
                self._sleep(3)
            finally:
                self._socket = None

    def send_result(self, state: str, status: str) -> None:
        socket = self._socket
        if socket is None:
            return
        socket.send(json.dumps({"type": "oauth_result", "state": state, "status": status}))

    def stop(self) -> None:
        self._stop.set()
        socket = self._socket
        if socket is not None:
            socket.close()
