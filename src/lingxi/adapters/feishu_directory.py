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
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from lingxi.core.identity.credentials import AuthorizationGrant, DerivedAccessToken, SecretToken

logger = logging.getLogger(__name__)

# 单页上限与翻页安全上界。上界存在的理由是 page_token 异常时不能无限翻页；
# 撞到上界是错误，不是"读完了"。
PAGE_SIZE = 100
MAX_PAGES = 200
REQUEST_TIMEOUT_SECONDS = 20
DEPARTMENT_ID_TYPES = frozenset({"open_department_id", "department_id"})


def department_identifier(entity: Mapping[str, Any]) -> tuple[str, str] | None:
    """从部门实体取出不可拆散的 ``(ID 值, ID 类型)``。

    飞书同一实体可能同时返回两种 ID。沿用已受控验证的选择顺序，优先使用
    ``open_department_id``；关键是把字段名作为类型一起交给下一次下钻调用，
    不能只留下裸值让服务端按默认类型猜测（Issue #16 B3）。
    """

    for id_type in ("open_department_id", "department_id"):
        value = entity.get(id_type)
        if isinstance(value, str) and value:
            return value, id_type
    return None


class FeishuDirectoryError(RuntimeError):
    """飞书调用失败。``code`` 供程序判断，消息里不含任何凭据。

    ``definite`` 表示"飞书明确拒绝"（收到了业务错误码）而非"结果不明确"
    （传输层异常、超时等）。这是协议细节的唯一出口：apps 层只读该属性，
    不解析 ``code`` 的字符串形状（代码框架第二节）。
    """

    def __init__(self, code: str, *, definite: bool | None = None) -> None:
        super().__init__(f"飞书目录接口调用失败：{code}")
        self.code = code
        self.definite = definite if definite is not None else code.startswith("feishu_code_")


@dataclass(frozen=True)
class AuthorizationExchange:
    """授权码换回的最小正式结果。

    短期 ``access_token`` 只用于本次读取 ``user_info``，刻意不放进返回对象；
    调用方因此不能把它顺手交给凭据 vault 或日志。长期只保留用来轮换的
    ``AuthorizationGrant``。
    """

    # 正式重授权只需要把飞书回读的主体与长期凭据交给上层；完整的
    # ``IdentityProfile`` 属于 Bot-Test 开通资产，不能成为正式换码的传递依赖。
    subject_open_id: str
    grant: AuthorizationGrant


def _require_https(base_url: str) -> str:
    """飞书出站必须 HTTPS（接口设计二）：误配 http:// 会把 Bearer token、
    App Secret 与一次性 refresh token 明文上路（Codex 复查发现）。"""

    return _require_https_uri(base_url, "飞书 base_url").rstrip("/")


def _require_https_uri(value: object, label: str) -> str:
    """校验外部 OAuth URL，不把配置原值带进错误消息。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}不能为空")
    text = value.strip()
    parsed = urlparse(text)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{label}必须使用不含凭据的 HTTPS 地址")
    if parsed.fragment:
        raise ValueError(f"{label}不得包含 URL fragment")
    return text


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
        self._base_url = _require_https(base_url)
        self._transport: Callable[..., Any] = transport or urllib_transport

    def _pages(self, path: str, *, token: str, query: Mapping[str, Any], keys: tuple[str, ...]) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        page_token: str | None = None
        for _ in range(MAX_PAGES):
            parameters = {**query, "page_size": PAGE_SIZE}
            if page_token:
                parameters["page_token"] = page_token
            url = f"{self._base_url}{path}?{urlencode(parameters)}"
            data = _payload(self._transport("GET", url, body=None, token=token))
            items = None
            for candidate in keys:
                value = data.get(candidate)
                if isinstance(value, list):
                    items = value
                    break
            if items is None:
                raise FeishuDirectoryError(f"missing_{keys[0]}")
            for item in items:
                if not isinstance(item, Mapping):
                    # 静默丢弃会让被丢的租户躲过完整性校验、半轮快照被标完成
                    # （终轮 Codex）：任何非对象项都是响应形状错误。
                    raise FeishuDirectoryError("invalid_page_item")
                collected.append(item)
            next_token = data.get("page_token")
            if data.get("has_more") is not True:
                return collected
            # has_more=true 但游标缺失或停滞：服务端明确说还有数据，把已收集的
            # 半截结果当成功返回会让调用方用它替换旧快照（Codex 复查发现）。
            if not isinstance(next_token, str) or not next_token or next_token == page_token:
                raise FeishuDirectoryError("pagination_stalled")
            page_token = next_token
        raise FeishuDirectoryError("pagination_limit")


class FeishuDirectoryClient(_PagedClient):
    """以专用授权用户身份读取关联组织的租户、部门与成员。

    刻意**没有**目标租户构造参数：标识跨租户唯一，租户归属是查出来的结果
    （Issue #16 硬约束 1）。租户键一律作为调用参数传入。
    """

    def list_collaboration_tenants(self, *, token: str) -> list[dict[str, Any]]:
        # 字段链取自 2026-07-29 真实探针（scripts/verify_feishu_association.sh）：
        # 实测主字段是 target_tenant_list；此前写的 collaboration_tenant_list 在真实
        # 响应里不存在，会让每次成功响应都被判失败（Codex 复查发现）。
        return self._pages(
            "/trust_party/v1/collaboration_tenants",
            token=token,
            query={},
            keys=("target_tenant_list", "items", "collaboration_tenants", "tenants"),
        )

    def list_visible_organization(
        self,
        *,
        token: str,
        tenant_key: str,
        department_id: str | None = None,
        department_id_type: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """返回 (部门实体, 成员实体)。飞书把两者混在同一个列表里，这里拆开。"""

        query: dict[str, Any] = {}
        if department_id is not None or department_id_type is not None:
            if (
                not isinstance(department_id, str)
                or not department_id
                or department_id_type not in DEPARTMENT_ID_TYPES
            ):
                # 不回显 ID：调用方误把令牌接到这里时，配置错误也不能泄露原值。
                raise ValueError("部门 ID 值与类型必须成对，类型只能是 open_department_id 或 department_id")
            query["target_department_id"] = department_id
            query["department_id_type"] = department_id_type
        entities = self._pages(
            f"/trust_party/v1/collaboration_tenants/{quote(tenant_key, safe='')}/visible_organization",
            token=token,
            query=query,
            keys=("collaboration_entity_list", "entities", "items"),
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
    """专用授权凭据的换码与续期调用。

    两条调用都在本适配器内完成协议细节；上层只得到正式领域对象，不会接触
    飞书响应字典或短期 ``access_token``。
    """

    def __init__(self, *, base_url: str, app_id: str, app_secret: str, transport: Callable[..., Any] | None = None) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url 必须由配置注入，不得写死在代码里")
        if not app_id or not app_secret:
            raise ValueError("缺少专用授权应用凭据")
        self._base_url = _require_https(base_url)
        self._app_id = app_id
        self._app_secret = app_secret
        self._transport: Callable[..., Any] = transport or urllib_transport

    def exchange_authorization_code(
        self,
        code: str,
        *,
        redirect_uri: str,
        required_scope: str,
    ) -> AuthorizationExchange:
        """用一次性授权码取得身份和可轮换凭据。

        ``code`` 与短期访问令牌只在本次方法调用的内存里存在；只返回身份资料
        和 ``AuthorizationGrant``。没有明确的 ``offline_access`` 或完整的
        refresh 凭据时失败关闭，调用方不能把一次性的半成品交给 vault。
        """

        if not isinstance(code, str) or not code.strip():
            raise ValueError("授权码不能为空")
        redirect_uri = _require_https_uri(redirect_uri, "授权回跳地址")
        response = self._transport(
            "POST",
            f"{self._base_url}/authen/v2/oauth/token",
            body={
                "grant_type": "authorization_code",
                "client_id": self._app_id,
                "client_secret": self._app_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
            token=None,
        )
        data = response if isinstance(response, Mapping) else {}
        if data.get("code") not in (None, 0, "0"):
            raise FeishuDirectoryError(f"feishu_code_{data.get('code')}")
        access_token_value = data.get("access_token")
        refresh_token = data.get("refresh_token")
        expires_in = data.get("refresh_token_expires_in")
        scope = data.get("scope")
        if not isinstance(access_token_value, str) or not access_token_value:
            raise FeishuDirectoryError("access_token_missing")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise FeishuDirectoryError("refresh_token_missing")
        if not isinstance(expires_in, int) or isinstance(expires_in, bool) or expires_in <= 0:
            raise FeishuDirectoryError("refresh_token_lifetime_missing")
        if not isinstance(scope, str) or not scope_covers(required_scope, scope):
            raise FeishuDirectoryError("scope_incomplete")

        access_token = SecretToken(access_token_value)
        info = self._transport(
            "GET",
            f"{self._base_url}/authen/v1/user_info",
            body=None,
            token=access_token.reveal(),
        )
        if not isinstance(info, Mapping) or info.get("code") not in (None, 0, "0"):
            code_value = info.get("code") if isinstance(info, Mapping) else "invalid_response"
            raise FeishuDirectoryError(f"feishu_code_{code_value}")
        raw_profile = info.get("data", info)
        if not isinstance(raw_profile, Mapping):
            raise FeishuDirectoryError("identity_profile_invalid")

        subject_open_id = _text_value(raw_profile.get("open_id"))
        if not subject_open_id:
            raise FeishuDirectoryError("identity_open_id_missing")
        return AuthorizationExchange(
            subject_open_id=subject_open_id,
            grant=AuthorizationGrant(SecretToken(refresh_token), expires_in, scope),
        )

    def refresh(self, current: AuthorizationGrant) -> tuple[AuthorizationGrant, DerivedAccessToken]:
        """用当前 ``refresh_token`` 换一组新凭据。

        返回 ``(新的长期凭据, 派生短期令牌)``。**长期凭据**的任何字段缺失、类型不符或
        有效期非正都抛 :class:`FeishuDirectoryError`——调用方据此按
        :func:`lingxi.core.identity.credentials.decide_after_refresh` 撤销，
        **绝不重放旧凭据**。

        短期令牌的寿命（``expires_in``）自 Issue #215 起被记下来交给调用方：日报改成
        按日取一次令牌之后，"缓存的这份还能不能用"没有别的判据。但它**缺失时不判整轮
        续期失败**，只是让派生令牌的寿命成为"未知"：续期成功这件事已经发生，新的一次性
        ``refresh_token`` 必须照常落盘，为一个附带字段把凭据判死会让"需人工重新授权"
        因为一个非凭据原因而发生，方向是反的。寿命未知的后果留在令牌供给那一侧
        （持有者拒绝缓存 → 供给按分类失败关闭），不会伪装成别的东西。
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
        access_token_lifetime = data.get("expires_in")
        if (
            not isinstance(access_token_lifetime, int)
            or isinstance(access_token_lifetime, bool)
            or access_token_lifetime <= 0
        ):
            access_token_lifetime = None
        logger.info(
            "专用授权续期成功 lifetime_seconds=%s access_token_lifetime_seconds=%s",
            expires_in,
            access_token_lifetime,
        )
        return (
            AuthorizationGrant(SecretToken(next_token), expires_in, scope if isinstance(scope, str) else ""),
            DerivedAccessToken(SecretToken(access_token), access_token_lifetime),
        )


class FeishuEmploymentReader:
    """首次开通链上的**实时**在职状态读取（Epic D / S-D-02）。

    组织快照刻意不保存在职状态（会立刻产生"库里在职、飞书已冻结"的陈旧窗口，而这个字段
    的唯一用途恰恰是拦截这种情况，见 ``core/identity/first_contact`` 与 `V-开通-07`），
    因此每次开通判定都要现读一次成员详情。

    **令牌供给是注入的可调用对象，不是构造参数**：这条读取用的是专用授权主体派生出来的
    短期令牌，而那条 ``refresh_token`` 是**一次性**的、全系统只允许一个消费者
    （2026-08-08 授权码被烧的事故形状）。把"怎么拿到令牌"留成注入口，是为了让"谁是那个
    唯一消费者"始终是一次显式的装配决定，而不是某个类的默认行为。

    读不到、读不懂都**不返回 ``False``**：``EmploymentStatus.from_feishu`` 对缺字段返回
    ``None``（判定层按"不可判定"拒绝开通），传输失败原样上抛（编排层归成本侧故障）。
    默认成在职是这条链上最危险的错误——它会给一个已冻结的账号开通并发布权限。
    """

    def __init__(
        self,
        *,
        client: "FeishuDirectoryClient",
        access_token: Callable[[], str],
    ) -> None:
        if not callable(access_token):
            raise TypeError("在职状态读取必须注入令牌供给")
        self._client = client
        self._access_token = access_token

    def status(self, *, tenant_key: str, open_id: str) -> Any:
        from lingxi.core.identity.first_contact import EmploymentStatus

        detail = self._client.get_member_detail(
            token=self._access_token(), tenant_key=tenant_key, member_id=open_id
        )
        return EmploymentStatus.from_feishu(detail.get("status"))


def _text_value(value: object) -> str:
    """只接受飞书身份字段的字符串形态，不把对象错误转成可识别文本。"""

    return value.strip() if isinstance(value, str) else ""


def scope_covers(requested_scope: str, returned_scope: str) -> bool:
    """判断飞书返回的 scope 是否覆盖本次配置请求的全部 scope。"""

    if not isinstance(requested_scope, str) or not isinstance(returned_scope, str):
        return False
    requested = frozenset(requested_scope.split())
    returned = frozenset(returned_scope.split())
    return bool(requested) and requested.issubset(returned)
