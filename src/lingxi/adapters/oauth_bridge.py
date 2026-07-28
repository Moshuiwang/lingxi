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
from urllib.parse import urlencode

from lingxi.core.identity.onboarding import IdentityProfile, OnboardingService


logger = logging.getLogger(__name__)


class AuthorizationStateStore(Protocol):
    def claim_authorizing_state(self, state: str) -> bool: ...
    def complete_authorizing_state(self, state: str, open_id: str) -> bool: ...
    def cancel_authorizing_state(self, state: str) -> bool: ...


class IdentityLoader(Protocol):
    def from_authorization_code(self, code: str) -> "LoadedOAuthIdentity": ...


class OAuthBridgeResultSender(Protocol):
    def send_result(
        self,
        state: str,
        status: str,
        debug_identity: dict[str, str | None] | None = None,
        debug_details: dict[str, object] | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class LoadedOAuthIdentity:
    """授权所得身份与仅在受控页面实时展示的调试资料。"""

    profile: IdentityProfile
    debug_details: dict[str, object] | None = None


class FeishuOAuthIdentityLoader:
    """只在 biai-stage 用一次性 code 换取身份；令牌绝不离开本方法。"""

    def __init__(self, app_id: str, app_secret: str, redirect_uri: str, debug_identity_display: bool = False) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._redirect_uri = redirect_uri
        self._debug_identity_display = debug_identity_display

    @staticmethod
    def _json(request: Request) -> dict[str, object]:
        with urlopen(request, timeout=10) as response:  # noqa: S310 -- 固定飞书 HTTPS 地址
            value = json.loads(response.read())
        if not isinstance(value, dict):
            raise ValueError("飞书授权响应格式异常")
        return value

    def _optional_json(self, request: Request) -> dict[str, object] | None:
        try:
            value = self._json(request)
        except Exception:
            return None
        return value if value.get("code") in (None, 0, "0") else None

    @staticmethod
    def _value(source: dict[str, object], key: str) -> str | None:
        value = source.get(key)
        return str(value) if value not in (None, "") else None

    def _department_tree(self, token: str, department_ids: object) -> list[dict[str, object]] | str:
        if not isinstance(department_ids, list):
            return "未返回"
        trees: list[dict[str, object]] = []
        for department_id in department_ids[:20]:
            if not isinstance(department_id, str) or not department_id:
                continue
            query = urlencode({"department_id": department_id, "department_id_type": "open_department_id", "page_size": 50})
            response = self._optional_json(Request(
                f"https://open.feishu.cn/open-apis/contact/v3/departments/parent?{query}",
                headers={"Authorization": f"Bearer {token}"},
            ))
            data = response.get("data") if isinstance(response, dict) else None
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list):
                trees.append({"id": department_id, "name": "未取得（无权限、不可见或无上级信息）", "children": []})
                continue
            path = [
                {"id": self._value(item, "open_department_id") or self._value(item, "department_id"), "name": self._value(item, "name") or "未返回"}
                for item in reversed(items)
                if isinstance(item, dict)
            ]
            node: dict[str, object] | None = None
            for item in reversed(path):
                node = {**item, "children": [] if node is None else [node]}
            trees.append(node or {"id": department_id, "name": "未返回", "children": []})
        return trees or "未返回"

    def _debug_details(self, token: str, oauth_data: dict[str, object], profile: IdentityProfile) -> dict[str, object]:
        contact = self._optional_json(Request(
            "https://open.feishu.cn/open-apis/contact/v3/users/"
            + profile.open_id
            + "?"
            + urlencode({"user_id_type": "open_id", "department_id_type": "open_department_id"}),
            headers={"Authorization": f"Bearer {token}"},
        ))
        contact_data = contact.get("data") if isinstance(contact, dict) and isinstance(contact.get("data"), dict) else {}
        contact_available = bool(contact_data)
        tenant = self._optional_json(Request(
            "https://open.feishu.cn/open-apis/tenant/v2/tenant/query",
            headers={"Authorization": f"Bearer {token}"},
        ))
        tenant_data = tenant.get("data") if isinstance(tenant, dict) and isinstance(tenant.get("data"), dict) else {}
        tenant_info = tenant_data.get("tenant") if isinstance(tenant_data.get("tenant"), dict) else {}
        return {
            "说明": "仅供 Bot-Test 当次授权验证；未写入日志、数据库或 Worker 存储。",
            "基础资料": {
                "姓名": self._value(oauth_data, "name") or "未返回",
                "邮箱": self._value(contact_data, "email") or self._value(oauth_data, "email") or "未返回",
                "电话": self._value(contact_data, "mobile") or self._value(oauth_data, "mobile") or "未返回",
                "英文名": self._value(contact_data, "en_name") or self._value(oauth_data, "en_name") or "未返回",
            },
            "所属组织": {
                "id": self._value(tenant_info, "tenant_key") or self._value(oauth_data, "tenant_key") or "未返回",
                "名字": self._value(tenant_info, "name") or "未返回（当前用户授权不能读取企业名称）",
            },
            "所属部门": self._department_tree(token, contact_data.get("department_ids")) if contact_available else "未取得（当前授权没有通讯录资料或组织可见范围）",
            "职务与任职资料": {
                "职务名称": self._value(contact_data, "job_title") or "未返回",
                "职级": self._value(contact_data, "job_level_id") or self._value(contact_data, "job_level") or "未返回",
                "工作序列": self._value(contact_data, "job_family_id") or self._value(contact_data, "job_family") or "未返回",
                "员工类型": self._value(contact_data, "employee_type") or "未返回",
                "工号": self._value(contact_data, "employee_no") or "未返回",
            },
            "通讯录查询": "已取得" if contact_available else "未取得（缺少权限、组织可见范围不足或接口不支持）",
        }

    def from_authorization_code(self, code: str) -> LoadedOAuthIdentity:
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
        profile = IdentityProfile(
            str(data.get("open_id") or ""), str(data.get("user_id") or ""), str(data.get("union_id") or ""), str(data.get("name") or ""),
            str(data["department"]) if data.get("department") else None,
            str(data["tenant_key"]) if data.get("tenant_key") else None,
            str(data["locale"]) if data.get("locale") else None,
        )
        return LoadedOAuthIdentity(profile, self._debug_details(str(token["access_token"]), data, profile) if self._debug_identity_display else None)


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

    def __init__(
        self,
        store: AuthorizationStateStore,
        service: OnboardingService,
        loader: IdentityLoader,
        result_sender: OAuthBridgeResultSender,
        event_key: str,
        debug_identity_display: bool = False,
    ) -> None:
        self._store = store
        self._service = service
        self._loader = loader
        self._result_sender = result_sender
        self._event_key = event_key.encode()
        self._debug_identity_display = debug_identity_display

    def _debug_identity(self, profile: IdentityProfile) -> dict[str, str | None] | None:
        if not self._debug_identity_display:
            return None
        # 仅供 biai-test 的实时页面调试：不写日志、不入库，且不包含授权码或令牌。
        return {
            "open_id": profile.open_id or None,
            "user_id": profile.user_id or None,
            "union_id": profile.union_id or None,
            "name": profile.display_name or None,
            "department": profile.department,
            "tenant_key": profile.tenant_key,
            "locale": profile.display_name_locale,
        }

    def process(self, message: OAuthBridgeMessage) -> None:
        if message.type == "oauth_cancelled":
            self._store.cancel_authorizing_state(message.state)
            self._result_sender.send_result(message.state, "retry")
            return

        try:
            if not self._store.claim_authorizing_state(message.state):
                self._result_sender.send_result(message.state, "retry")
                return
            loaded = self._loader.from_authorization_code(message.code or "")
            profile = loaded.profile
            # 受控验收只确认字段是否可得；不记录任何身份标识原文。
            logger.warning(
                "OAuth identity field presence: open_id=%s user_id=%s union_id=%s",
                bool(profile.open_id),
                bool(profile.user_id),
                bool(profile.union_id),
            )
            debug_identity = self._debug_identity(profile)
            if not self._store.complete_authorizing_state(message.state, profile.open_id):
                # 身份与原私聊人不能一一确认时，清除这次进度；不能留下不可恢复的“处理中”。
                self._store.cancel_authorizing_state(message.state)
                self._result_sender.send_result(message.state, "retry", debug_identity, loaded.debug_details if self._debug_identity_display else None)
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
        self._result_sender.send_result(message.state, "identity_confirmed", debug_identity, loaded.debug_details if self._debug_identity_display else None)


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

    def send_result(
        self,
        state: str,
        status: str,
        debug_identity: dict[str, str | None] | None = None,
        debug_details: dict[str, object] | None = None,
    ) -> None:
        socket = self._socket
        if socket is None:
            return
        payload: dict[str, object] = {"type": "oauth_result", "state": state, "status": status}
        if debug_identity is not None:
            payload["debug_identity"] = debug_identity
        if debug_details is not None:
            payload["debug_details"] = debug_details
        socket.send(json.dumps(payload))

    def stop(self) -> None:
        self._stop.set()
        socket = self._socket
        if socket is not None:
            socket.close()
