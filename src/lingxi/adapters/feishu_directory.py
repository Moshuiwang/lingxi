"""飞书开放平台的目录读取与专用授权续期客户端。

这是 `adapters/` 层：协议细节、分页、错误码映射都止步于此，飞书的返回结构
不得泄漏进 `core/` 的函数签名。规则在
:mod:`lingxi.core.identity.org_snapshot` 与 :mod:`lingxi.core.identity.credentials`。

**本模块的真实调用不在本次测试范围内。** 分页与错误映射的形状由注入的假传输层
锁住（tests/test_feishu_directory_adapter.py），真实链路属 L4a，留给
`biai-stage` + `Bot-Test`。请求路径与两条身份路径的用法吸收自已受控验证过的
`scripts/sync_feishu_org_snapshot.py`。

凭据边界：`app_secret` 与 `refresh_token` 只出现在**请求体**里，不进 URL、
不进日志、不进异常消息。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from lingxi.core.identity.credentials import AuthorizationGrant, SecretToken

logger = logging.getLogger(__name__)

# 单页上限与翻页安全上界。上界存在的理由是 page_token 异常时不能无限翻页；
# 撞到上界是错误，不是"读完了"。
PAGE_SIZE = 100
MAX_PAGES = 200
REQUEST_TIMEOUT_SECONDS = 20


class FeishuDirectoryError(RuntimeError):
    """飞书调用失败。``code`` 供程序判断，消息里不含任何凭据。"""

    def __init__(self, code: str) -> None:
        super().__init__(f"飞书目录接口调用失败：{code}")
        self.code = code


class Transport(Protocol):
    def __call__(self, method: str, url: str, *, body: Mapping[str, Any] | None = ..., token: str | None = ...) -> Any: ...


def urllib_transport(method: str, url: str, *, body: Mapping[str, Any] | None = None, token: str | None = None) -> Any:
    """默认传输层：只发 HTTPS，不重试有副作用的请求。"""

    payload = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {"Content-Type": "application/json; charset=utf-8"} if body is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=payload, headers=headers, method=method)
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310 - 地址来自受控配置
            return json.loads(response.read())
    except HTTPError as error:
        try:
            return json.loads(error.read())
        except Exception as parse_error:
            raise FeishuDirectoryError(f"http_{error.code}") from parse_error
    except (URLError, OSError, TimeoutError) as error:
        raise FeishuDirectoryError("transport_error") from error
    except ValueError as error:
        raise FeishuDirectoryError("invalid_json") from error


def _payload(response: Any) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        raise FeishuDirectoryError("invalid_response_shape")
    code = response.get("code")
    if code not in (None, 0, "0"):
        raise FeishuDirectoryError(f"feishu_code_{code}")
    data = response.get("data")
    return dict(data) if isinstance(data, Mapping) else {}


class _PagedClient:
    def __init__(self, *, base_url: str, transport: Callable[..., Any] | None = None) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url 必须由配置注入，不得写死在代码里")
        self._base_url = base_url.rstrip("/")
        self._transport: Callable[..., Any] = transport or urllib_transport

    def _pages(self, path: str, *, token: str, query: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        page_token: str | None = None
        for _ in range(MAX_PAGES):
            parameters = {**query, "page_size": PAGE_SIZE}
            if page_token:
                parameters["page_token"] = page_token
            url = f"{self._base_url}{path}?{urlencode(parameters)}"
            data = _payload(self._transport("GET", url, body=None, token=token))
            items = data.get(key)
            if not isinstance(items, list):
                raise FeishuDirectoryError(f"missing_{key}")
            collected.extend(item for item in items if isinstance(item, Mapping))
            next_token = data.get("page_token")
            # page_token 没变说明服务端不再前进；继续翻是死循环，不是读完。
            if data.get("has_more") is not True or not isinstance(next_token, str) or not next_token or next_token == page_token:
                return collected
            page_token = next_token
        raise FeishuDirectoryError("pagination_limit")


class FeishuDirectoryClient(_PagedClient):
    """以专用授权用户身份读取关联组织的租户、部门与成员。

    刻意**没有**目标租户构造参数：标识跨租户唯一，租户归属是查出来的结果
    （Issue #16 硬约束 1）。租户键一律作为调用参数传入。
    """

    def list_collaboration_tenants(self, *, token: str) -> list[dict[str, Any]]:
        return self._pages(
            "/trust_party/v1/collaboration_tenants",
            token=token,
            query={},
            key="collaboration_tenant_list",
        )

    def list_visible_organization(self, *, token: str, tenant_key: str, department_id: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """返回 (部门实体, 成员实体)。飞书把两者混在同一个列表里，这里拆开。"""

        query: dict[str, Any] = {}
        if department_id:
            query["target_department_id"] = department_id
        entities = self._pages(
            f"/trust_party/v1/collaboration_tenants/{quote(tenant_key, safe='')}/visible_organization",
            token=token,
            query=query,
            key="collaboration_entity_list",
        )
        departments = [item for item in entities if item.get("open_department_id") or item.get("department_id")]
        members = [item for item in entities if not (item.get("open_department_id") or item.get("department_id"))]
        return departments, members

    def get_member_detail(self, *, token: str, tenant_key: str, member_id: str, id_type: str = "open_id") -> dict[str, Any]:
        """读取成员详情。**在职状态只能从这里实时取，不从快照取。**"""

        url = (
            f"{self._base_url}/trust_party/v1/collaboration_tenants/{quote(tenant_key, safe='')}"
            f"/collaboration_users/{quote(member_id, safe='')}?{urlencode({'target_user_id_type': id_type})}"
        )
        data = _payload(self._transport("GET", url, body=None, token=token))
        detail = data.get("target_user")
        if not isinstance(detail, Mapping):
            raise FeishuDirectoryError("member_detail_missing")
        return dict(detail)


class FeishuAuthorizationClient:
    """专用授权凭据的续期调用。只做一件事：拿旧的换新的。"""

    def __init__(self, *, base_url: str, app_id: str, app_secret: str, transport: Callable[..., Any] | None = None) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url 必须由配置注入，不得写死在代码里")
        if not app_id or not app_secret:
            raise ValueError("缺少专用授权应用凭据")
        self._base_url = base_url.rstrip("/")
        self._app_id = app_id
        self._app_secret = app_secret
        self._transport: Callable[..., Any] = transport or urllib_transport

    def refresh(self, current: AuthorizationGrant) -> tuple[AuthorizationGrant, SecretToken]:
        """用当前 ``refresh_token`` 换一组新凭据。

        返回 ``(新的长期凭据, 短期 access_token)``。任何字段缺失、类型不符或
        有效期非正都抛 :class:`FeishuDirectoryError`——调用方据此按
        :func:`lingxi.core.identity.credentials.decide_after_refresh` 撤销，
        **绝不重放旧凭据**。
        """

        response = self._transport(
            "POST",
            f"{self._base_url}/authen/v2/oauth/token",
            body={
                "grant_type": "refresh_token",
                "client_id": self._app_id,
                "client_secret": self._app_secret,
                "refresh_token": current.refresh_token.reveal(),
            },
            token=None,
        )
        data = response if isinstance(response, Mapping) else {}
        code = data.get("code")
        if code not in (None, 0, "0"):
            raise FeishuDirectoryError(f"feishu_code_{code}")
        access_token = data.get("access_token")
        next_token = data.get("refresh_token")
        expires_in = data.get("refresh_token_expires_in")
        if not isinstance(access_token, str) or not access_token:
            raise FeishuDirectoryError("access_token_missing")
        if not isinstance(next_token, str) or not next_token:
            raise FeishuDirectoryError("refresh_token_missing")
        if not isinstance(expires_in, int) or isinstance(expires_in, bool) or expires_in <= 0:
            raise FeishuDirectoryError("refresh_token_lifetime_missing")
        scope = data.get("scope")
        logger.info("专用授权续期成功 lifetime_seconds=%s", expires_in)
        return AuthorizationGrant(SecretToken(next_token), expires_in, scope if isinstance(scope, str) else ""), SecretToken(access_token)
