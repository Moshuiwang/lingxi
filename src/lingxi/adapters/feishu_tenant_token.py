"""飞书应用身份令牌（``tenant_access_token``）的获取。

权限发布表用应用身份写入：没有凭据生命周期，不需要人再点授权、不会过期、
不需要轮换，代价是写入不绑定到某个具体授权人、需要把应用加为该 Base 协作者。
与专用授权凭据的区别：这里没有一次性凭据。``tenant_access_token`` 由
``app_id``/``app_secret`` 换取，这两个值静态、可重复使用，是 scheduler 的
必需配置，其余 Feishu 适配器早已在用——零新增凭据材料，也不是消费一次就
换代、丢了要人工重新授权的 ``refresh_token`` 那一类；本模块因此不碰它。

本模块边界：只做一件事——POST 一次换令牌接口，解析令牌与寿命，包进
:class:`~lingxi.core.identity.credentials.DerivedAccessToken`；不缓存、不做
续期节奏判断，那是 :mod:`lingxi.core.permission.tenant_token_supply` 的事。
``app_secret`` 只出现在请求体里，不进 URL、不进日志、不进异常消息。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from lingxi.core.identity.credentials import DerivedAccessToken, SecretToken

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 20
_TENANT_TOKEN_PATH = "/auth/v3/tenant_access_token/internal"


class FeishuTenantTokenError(RuntimeError):
    """应用身份令牌获取失败。``code`` 供程序判断，消息里不含任何凭据。"""

    def __init__(self, code: str) -> None:
        """记录失败分类码，消息体只含 `code`、不带任何凭据。"""
        self.code = code
        super().__init__(code)


def _require_https(base_url: str) -> str:
    """飞书出站必须 HTTPS：误配 ``http://`` 会把 App Secret 明文上路。"""
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("base_url 必须由配置注入，不得写死在代码里")
    text = base_url.strip()
    parsed = urlparse(text)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("飞书 base_url 必须使用不含凭据的 HTTPS 地址")
    if parsed.fragment:
        raise ValueError("飞书 base_url 不得包含 URL fragment")
    return text.rstrip("/")


class Transport(Protocol):
    """传输层的可替换形状：给定方法、URL 与可选请求体，返回已解析的响应。"""

    def __call__(self, method: str, url: str, *, body: Mapping[str, Any] | None = ...) -> Any:
        """发起一次请求并返回已解析的响应；测试可注入假实现替换真实网络调用。"""
        ...


def urllib_transport(method: str, url: str, *, body: Mapping[str, Any] | None = None) -> Any:
    """默认传输层：只发 HTTPS，不重试有副作用的请求（换令牌本身就是一次性调用，重试会在飞书那一侧产生第二次计次；调用方按分类失败关闭，下一轮自然会再试）。"""
    payload = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {"Content-Type": "application/json; charset=utf-8"} if body is not None else {}
    request = Request(url, data=payload, headers=headers, method=method)
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # 地址来自受控配置
            return json.loads(response.read())
    except HTTPError as error:
        try:
            return json.loads(error.read())
        except Exception as parse_error:
            raise FeishuTenantTokenError(f"http_{error.code}") from parse_error
    except (URLError, OSError, TimeoutError) as error:
        raise FeishuTenantTokenError("transport_error") from error
    except ValueError as error:
        raise FeishuTenantTokenError("invalid_json") from error


class FeishuTenantTokenClient:
    """应用身份令牌的换取调用。上层只得到 :class:`DerivedAccessToken`，不接触飞书响应字典。"""

    def __init__(
        self,
        *,
        base_url: str,
        app_id: str,
        app_secret: str,
        transport: Callable[..., Any] | None = None,
    ) -> None:
        """校验应用身份凭据并接入 base_url 与传输层。"""
        if not app_id or not app_secret:
            raise ValueError("缺少应用身份凭据")
        self._base_url = _require_https(base_url)
        self._app_id = app_id
        self._app_secret = app_secret
        self._transport: Callable[..., Any] = transport or urllib_transport

    def fetch(self) -> DerivedAccessToken:
        """换一份新的应用身份令牌。**每次调用都真的发起一次请求**——本方法不缓存，缓存与"该不该现在去换"的判断在 :mod:`lingxi.core.permission.tenant_token_supply`。

        令牌与寿命任一缺失、类型不符或寿命非正都失败关闭：这条调用没有"部分成功"
        （不像专用授权续期那样还要落盘一份新的 ``refresh_token``），因此这里不采用
        "寿命未知就把判断推给缓存层"的姿态——没有别的东西需要抢救，直接拒绝更简单、
        更早暴露问题。
        """
        response = self._transport(
            "POST",
            f"{self._base_url}{_TENANT_TOKEN_PATH}",
            body={"app_id": self._app_id, "app_secret": self._app_secret},
        )
        data = response if isinstance(response, Mapping) else {}
        code = data.get("code")
        if code not in (None, 0, "0"):
            raise FeishuTenantTokenError(f"feishu_code_{code}")
        token = data.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise FeishuTenantTokenError("tenant_access_token_missing")
        expire = data.get("expire")
        if not isinstance(expire, int) or isinstance(expire, bool) or expire <= 0:
            raise FeishuTenantTokenError("tenant_access_token_lifetime_missing")
        return DerivedAccessToken(SecretToken(token), expire)


__all__ = [
    "FeishuTenantTokenClient",
    "FeishuTenantTokenError",
    "Transport",
    "urllib_transport",
]
