"""飞书应用身份令牌（``tenant_access_token``）的获取（Issue #226 裁定 3）。

产品负责人 2026-08-18 裁定：权限发布表用**应用身份**写入。理由（产品负责人原文）：
「没有凭据生命周期——不需要他再点授权、不会过期、不需要轮换」。2026-08-18 上午刚为
专用授权凭据到期紧急处理过一次（部署 scheduler 接管轮换），再增加一条会轮换的凭据是
代价最高的选项。已知代价（产品负责人已知情）：写入不绑定到某个具体授权人；需要把
应用加为该 Base 的协作者。

## 与专用授权凭据（#215）的区别：这里**没有一次性凭据**

``tenant_access_token`` 由 ``app_id``/``app_secret`` 换取——这两个值是**静态、可
重复使用**的应用配置，本来就是 scheduler 的必需配置（``LINGXI_FEISHU_APP_ID`` /
``LINGXI_FEISHU_APP_SECRET``，见 :class:`~lingxi.apps.scheduler.SchedulerConfig`），
其余 Feishu 适配器早已在用同一对值。因此：

- **零新增凭据材料**：不新增任何环境变量、不新增任何需要产品负责人操作的授权步骤；
- **不是一次性凭据**：与 :mod:`lingxi.core.identity.credentials` 的
  ``refresh_token``（消费一次就换代、丢了要人工重新授权）完全不同类，可以按需
  重复换取，不需要"唯一消费者"这类频率保护；
- **本模块因此不碰、也不需要碰** ``refresh_token``——那是另一条完全不同的路
  （专用授权凭据轮换，见 :mod:`lingxi.core.identity.access_token_supply`）。

## 本模块的边界

只做一件事：POST 一次 ``/auth/v3/tenant_access_token/internal``，解析出令牌与寿命，
包进 :class:`~lingxi.core.identity.credentials.DerivedAccessToken`。**不缓存、不做
任何续期节奏判断**——那是 :mod:`lingxi.core.permission.tenant_token_supply` 的事
（``core/`` 不做网络 I/O，本模块是它唯一的真实调用来源）。

请求路径与响应字段吸收自已受控验证过的 ``scripts/sync_feishu_org_snapshot.py`` 的
``app_access_token()``，但补上它没做的两件事：解析 ``expire`` 字段（供上层缓存判断
"这份还能不能用"）与 ``adapters/`` 层统一的传输错误分类（同
:mod:`lingxi.adapters.feishu_directory` 的 ``urllib_transport`` 姿态）。

**本模块的真实调用不在本次测试范围内**：传输层可注入（见
``tests/test_feishu_tenant_token.py``），真实链路属 L4a，留给
`biai-stage` + `Bot-Test`。凭据边界：``app_secret`` 只出现在**请求体**里，不进 URL、
不进日志、不进异常消息。
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
    def __call__(self, method: str, url: str, *, body: Mapping[str, Any] | None = ...) -> Any: ...


def urllib_transport(method: str, url: str, *, body: Mapping[str, Any] | None = None) -> Any:
    """默认传输层：只发 HTTPS，不重试有副作用的请求（换令牌本身就是一次性调用，
    重试会在飞书那一侧产生第二次计次；调用方按分类失败关闭，下一轮自然会再试）。
    """

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
    """应用身份令牌的换取调用。上层只得到 :class:`DerivedAccessToken`，不接触飞书
    响应字典。"""

    def __init__(
        self,
        *,
        base_url: str,
        app_id: str,
        app_secret: str,
        transport: Callable[..., Any] | None = None,
    ) -> None:
        if not app_id or not app_secret:
            raise ValueError("缺少应用身份凭据")
        self._base_url = _require_https(base_url)
        self._app_id = app_id
        self._app_secret = app_secret
        self._transport: Callable[..., Any] = transport or urllib_transport

    def fetch(self) -> DerivedAccessToken:
        """换一份新的应用身份令牌。**每次调用都真的发起一次请求**——本方法不缓存，
        缓存与"该不该现在去换"的判断在
        :mod:`lingxi.core.permission.tenant_token_supply`。

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
