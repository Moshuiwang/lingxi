"""飞书开放平台的目录读取与专用授权续期客户端。

这是 `adapters/` 层：协议细节、分页、错误码映射都止步于此，飞书的返回结构
不得泄漏进 `core/` 的函数签名。传输、节流、分页遍历与错误映射的实现见
:mod:`lingxi.adapters.feishu_paged_client`；本模块只组装具体的业务端点。
真实调用不在本次测试范围内，分页与错误映射的形状由注入的假传输层锁住
（tests/test_feishu_directory_adapter.py），真实链路属 L4a。

凭据边界：`app_secret` 与 `refresh_token` 只出现在**请求体**里，不进 URL、
日志或异常消息。已知边界（登记不改行为，触发都需要飞书响应形状真的变化，
具体判据见对应方法文档字符串）：`_pages`/`_pages_multi` 的空结果判据不
完全对齐，飞书响应漂移时由 `org_snapshot.verify_batch` 的交叉校验兜底。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from lingxi.adapters.feishu_paged_client import (
    FEISHU_RATE_LIMIT_ERROR_CODE,
    RATE_LIMIT_RETRY_BACKOFFS_SECONDS,
    REQUEST_PAUSE_JITTER_SECONDS,
    REQUEST_PAUSE_SECONDS,
    FeishuDirectoryError,
    _PagedClient,
    _require_https,
    _require_https_uri,
    _safe_feishu_code,
    urllib_transport,
)
from lingxi.core.identity.credentials import AuthorizationGrant, DerivedAccessToken, SecretToken

__all__ = [
    "FEISHU_RATE_LIMIT_ERROR_CODE",
    "RATE_LIMIT_RETRY_BACKOFFS_SECONDS",
    "REQUEST_PAUSE_JITTER_SECONDS",
    "REQUEST_PAUSE_SECONDS",
    "AuthorizationExchange",
    "FeishuAuthorizationClient",
    "FeishuDirectoryClient",
    "FeishuDirectoryError",
    "FeishuEmploymentReader",
    "department_identifier",
    "urllib_transport",
]

logger = logging.getLogger(__name__)

DEPARTMENT_ID_TYPES = frozenset({"open_department_id", "department_id"})


def department_identifier(entity: Mapping[str, Any]) -> tuple[str, str] | None:
    """从部门实体取出不可拆散的 ``(ID 值, ID 类型)``。

    飞书同一实体可能同时返回两种 ID；优先使用 ``open_department_id``，并把
    字段名作为类型一起交给下一次下钻调用，不能只留下裸值让服务端按默认
    类型猜测。
    """
    for id_type in ("open_department_id", "department_id"):
        value = entity.get(id_type)
        if isinstance(value, str) and value:
            return value, id_type
    return None


@dataclass(frozen=True)
class AuthorizationExchange:
    """授权码换回的最小正式结果。

    短期 ``access_token`` 只用于本次读取 ``user_info``，刻意不放进返回对象；
    调用方因此不能把它顺手交给凭据 vault 或日志。长期只保留用来轮换的
    ``AuthorizationGrant``。
    """

    # 完整的 IdentityProfile 属于 Bot-Test 开通资产，不能成为正式换码的
    # 传递依赖；正式重授权只需要主体与长期凭据。
    subject_open_id: str
    grant: AuthorizationGrant


class FeishuDirectoryClient(_PagedClient):
    """读取关联组织的租户、部门与成员，覆盖两条互相独立的身份路径。

    专用授权用户身份（``trust_party/v1/*``）是开通链在职状态实时回读的唯一
    来源；应用身份（``directory/v1/*``）供组织快照批次完整性校验交叉核对
    ——同一租户在两条路径下的成员集合须彼此相等，缺一条就没有另一半输入。
    刻意**没有**目标租户构造参数：标识跨租户唯一，租户归属是查出来的结果，
    租户键一律作为调用参数传入。
    """

    def list_collaboration_tenants(self, *, token: str) -> list[dict[str, Any]]:
        """以专用授权用户身份读取当前可见的关联租户列表。

        这条路径没有目标租户可传，返回"当前看不到任何关联组织"是合法结果，
        不代表该主体一定看不到关联组织——那需要独立证据，不是本方法的隐含
        语义。
        """
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
                raise ValueError(
                    "部门 ID 值与类型必须成对，类型只能是 open_department_id 或 department_id"
                )
            query["target_department_id"] = department_id
            query["department_id_type"] = department_id_type
        entities = self._pages(
            f"/trust_party/v1/collaboration_tenants/{quote(tenant_key, safe='')}/visible_organization",
            token=token,
            query=query,
            keys=("collaboration_entity_list", "entities", "items"),
        )
        departments = [
            item for item in entities if item.get("open_department_id") or item.get("department_id")
        ]
        members = [
            item
            for item in entities
            if not (item.get("open_department_id") or item.get("department_id"))
        ]
        return departments, members

    def list_collaboration_tenants_as_app(self, *, token: str) -> list[dict[str, Any]]:
        """以应用身份读取关联租户列表（``directory/v1/collaboration_tenants``）。

        字段名回退列表沿用 :meth:`list_collaboration_tenants` 已验证过的顺序
        并补上 ``tenant_list``；真正命中哪个字段由响应决定，回退列表里任何
        一个命中都按同一套逻辑处理，不影响分页与校验语义。
        """
        return self._pages(
            "/directory/v1/collaboration_tenants",
            token=token,
            query={},
            keys=("tenant_list", "items", "collaboration_tenants", "tenants", "target_tenant_list"),
        )

    def list_share_entities(
        self, *, token: str, tenant_key: str, department_id: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """以应用身份读取一层共享范围（``directory/v1/share_entities``）。

        返回 ``(部门实体, 成员实体)``——这个端点原生就是两个独立列表
        （``share_departments``/``share_users``），与 :meth:`list_visible_organization`
        混在同一个列表里不同。递归下钻子部门与伪根记录过滤不在这里做，属于
        编排逻辑而不是协议细节，交给 :mod:`lingxi.adapters.feishu_org_snapshot_reader`。
        """
        if not isinstance(department_id, str) or not department_id:
            raise ValueError("target_department_id 不能为空；根部门请传 '0'")
        query: dict[str, Any] = {
            "target_tenant_key": tenant_key,
            "is_select_subject": "false",
            "target_department_id": department_id,
        }
        collected = self._pages_multi(
            "/directory/v1/share_entities",
            token=token,
            query=query,
            list_keys=("share_departments", "share_users"),
        )
        return collected["share_departments"], collected["share_users"]

    def get_member_detail(
        self, *, token: str, tenant_key: str, member_id: str, id_type: str = "open_id"
    ) -> dict[str, Any]:
        """读取成员详情。**在职状态只能从这里实时取，不从快照取。**"""
        url = (
            f"{self._base_url}/trust_party/v1/collaboration_tenants/{quote(tenant_key, safe='')}"
            f"/collaboration_users/{quote(member_id, safe='')}?{urlencode({'target_user_id_type': id_type})}"
        )
        data = self._request(url, token=token)
        detail = data.get("target_user")
        if not isinstance(detail, Mapping):
            raise FeishuDirectoryError("member_detail_missing")
        return dict(detail)


class FeishuAuthorizationClient:
    """专用授权凭据的换码与续期调用。

    两条调用都在本适配器内完成协议细节；上层只得到正式领域对象，不会接触
    飞书响应字典或短期 ``access_token``。
    """

    def __init__(
        self,
        *,
        base_url: str,
        app_id: str,
        app_secret: str,
        transport: Callable[..., Any] | None = None,
    ) -> None:
        """校验 ``base_url`` 是不含凭据的 HTTPS 地址，且应用凭据均已提供。"""
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url 必须由配置注入，不得写死在代码里")
        if not app_id or not app_secret:
            raise ValueError("缺少专用授权应用凭据")
        self._base_url = _require_https(base_url)
        self._app_id = app_id
        self._app_secret = app_secret
        self._transport: Callable[..., Any] = transport or urllib_transport

    def _exchange_token(self, *, code: str, redirect_uri: str) -> dict[str, Any]:
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
            raise FeishuDirectoryError(_safe_feishu_code(data.get("code")))
        return data

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
        data = self._exchange_token(code=code, redirect_uri=redirect_uri)
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
        raw_profile = self._fetch_user_profile(access_token)
        subject_open_id = _text_value(raw_profile.get("open_id"))
        if not subject_open_id:
            raise FeishuDirectoryError("identity_open_id_missing")
        return AuthorizationExchange(
            subject_open_id=subject_open_id,
            grant=AuthorizationGrant(SecretToken(refresh_token), expires_in, scope),
        )

    def _fetch_user_profile(self, access_token: SecretToken) -> Mapping[str, Any]:
        info = self._transport(
            "GET",
            f"{self._base_url}/authen/v1/user_info",
            body=None,
            token=access_token.reveal(),
        )
        if not isinstance(info, Mapping) or info.get("code") not in (None, 0, "0"):
            code_value = info.get("code") if isinstance(info, Mapping) else "invalid_response"
            raise FeishuDirectoryError(_safe_feishu_code(code_value))
        raw_profile = info.get("data", info)
        if not isinstance(raw_profile, Mapping):
            raise FeishuDirectoryError("identity_profile_invalid")
        return raw_profile

    def refresh(self, current: AuthorizationGrant) -> tuple[AuthorizationGrant, DerivedAccessToken]:
        """用当前 ``refresh_token`` 换一组新凭据。

        返回 ``(新的长期凭据, 派生短期令牌)``。**长期凭据**的任何字段缺失、类型不符或
        有效期非正都抛 :class:`FeishuDirectoryError`——调用方据此撤销，**绝不重放
        旧凭据**。短期令牌寿命（``expires_in``）缺失时**不判整轮续期失败**，只让
        派生令牌寿命成为"未知"：续期成功这件事已经发生，新的一次性 ``refresh_token``
        必须照常落盘，不能因附带字段把凭据判死。
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
            raise FeishuDirectoryError(_safe_feishu_code(code))
        return self._parsed_refresh_result(data)

    def _parsed_refresh_result(
        self, data: Mapping[str, Any]
    ) -> tuple[AuthorizationGrant, DerivedAccessToken]:
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
            AuthorizationGrant(
                SecretToken(next_token), expires_in, scope if isinstance(scope, str) else ""
            ),
            DerivedAccessToken(SecretToken(access_token), access_token_lifetime),
        )


class FeishuEmploymentReader:
    """首次开通链上的**实时**在职状态读取。

    组织快照刻意不保存在职状态——那个字段的唯一用途是拦截"库里在职、飞书
    已冻结"的陈旧窗口，因此每次开通判定都要现读一次成员详情。令牌供给是
    注入的可调用对象而不是构造参数：``refresh_token`` 是一次性、全系统仅
    允许一个消费者的凭据，"谁是唯一消费者"必须始终是显式装配决定。读不到、
    读不懂都**不返回 ``False``**——默认在职是这条链上最危险的错误，会给
    已冻结账号开通并发布权限。
    """

    def __init__(
        self,
        *,
        client: FeishuDirectoryClient,
        access_token: Callable[[], str],
    ) -> None:
        """校验令牌供给确实可调用；拒绝把默认行为当作"谁是消费者"的隐含答案。"""
        if not callable(access_token):
            raise TypeError("在职状态读取必须注入令牌供给")
        self._client = client
        self._access_token = access_token

    def status(self, *, tenant_key: str, open_id: str) -> Any:
        """读取一次实时在职状态；读不到、读不懂都交给调用方判定，不猜测。"""
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
