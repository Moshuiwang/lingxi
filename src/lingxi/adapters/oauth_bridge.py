"""Bot-Test 受控验证：biai-stage 到 OAuth Worker 的仅出站桥接。

Worker 只中转一次性 code；本模块在 biai-stage 完成飞书身份读取和本地核验。
它是正式开发的参考实现，不属于正式 Lingxi 用户入口。
短期访问令牌绝不记录；用户同意离线访问后得到的轮换刷新令牌，仅交给受控的
加密凭据库，绝不写入日志、回调 Worker 或调试页面。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import unicodedata
from urllib.request import Request, urlopen
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote, urlencode

from lingxi.core.identity.onboarding import IdentityProfile, OnboardingService

from .oauth_bridge_client import OAuthBridgeClient, OAuthBridgeMessage, OAuthBridgeResultSender


logger = logging.getLogger(__name__)


class AuthorizationStateStore(Protocol):
    def claim_authorizing_state(self, state: str) -> bool: ...
    def complete_authorizing_state(self, state: str, open_id: str) -> bool: ...
    def cancel_authorizing_state(self, state: str) -> bool: ...


class IdentityLoader(Protocol):
    def from_authorization_code(self, code: str) -> "LoadedOAuthIdentity": ...


class RefreshTokenVault(Protocol):
    def save(self, open_id: str, grant: "OAuthTokenGrant") -> None: ...


@dataclass(frozen=True)
class LoadedOAuthIdentity:
    """授权所得身份与仅在受控页面实时展示的调试资料。"""

    profile: IdentityProfile
    debug_details: dict[str, object] | None = None
    token_grant: "OAuthTokenGrant | None" = None


@dataclass(frozen=True)
class OAuthTokenGrant:
    """只在当前进程内短暂存在的飞书令牌结果。"""

    refresh_token: str
    refresh_token_expires_in: int
    scope: str


class FeishuOAuthIdentityLoader:
    """按飞书 v3 OAuth 链路换取身份及可轮换的续期凭证。"""

    def __init__(self, app_id: str, app_secret: str, redirect_uri: str, debug_identity_display: bool = False, target_tenant_key: str | None = None) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._redirect_uri = redirect_uri
        self._debug_identity_display = debug_identity_display
        self._target_tenant_key = target_tenant_key

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

    @staticmethod
    def _bounded_debug_value(value: object, depth: int = 0) -> object:
        """回调页只接受有界调试资料，避免大组织响应被 Worker 丢弃。"""
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return value[:512]
        if depth >= 7:
            return "（层级过深，已省略）"
        if isinstance(value, list):
            return [FeishuOAuthIdentityLoader._bounded_debug_value(item, depth + 1) for item in value[:20]]
        if isinstance(value, dict):
            return {
                str(key)[:80]: FeishuOAuthIdentityLoader._bounded_debug_value(item, depth + 1)
                for key, item in list(value.items())[:40]
            }
        return str(value)[:512]

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

    @staticmethod
    def _walk(value: object, depth: int = 0):
        if isinstance(value, dict):
            yield value, depth
            for child in value.values():
                yield from FeishuOAuthIdentityLoader._walk(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                yield from FeishuOAuthIdentityLoader._walk(child, depth + 1)

    @staticmethod
    def _text(value: object) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return " ".join(FeishuOAuthIdentityLoader._text(item) for item in value.values())
        if isinstance(value, list):
            return " ".join(FeishuOAuthIdentityLoader._text(item) for item in value)
        return ""

    @staticmethod
    def _normalized_text(value: object) -> str:
        return "".join(unicodedata.normalize("NFKC", FeishuOAuthIdentityLoader._text(value)).replace("\u200b", "").split())

    @staticmethod
    def _masked(value: object) -> str:
        text = str(value or "")
        return text[:6] + ("…" if len(text) > 6 else "")

    @staticmethod
    def _entity_identifier(entity: dict[str, object], kind: str) -> tuple[str, str] | None:
        """把可见组织条目的标识转成详情接口所需的标识类型。"""
        choices = (
            (("open_user_id", "open_id"), ("user_id", "user_id"), ("union_user_id", "union_id"))
            if kind == "user"
            else (("open_department_id", "open_department_id"), ("department_id", "department_id"))
        )
        for field, identifier_type in choices:
            value = entity.get(field)
            if isinstance(value, str) and value:
                return value, identifier_type
        return None

    @classmethod
    def _redact_identifiers(cls, value: object, key: str = "") -> object:
        """调试页面和日志均不暴露 open/user/union 等可关联标识。"""
        normalized_key = key.lower()
        sensitive = (
            normalized_key in {"id", "ids", "open_id", "union_id", "user_id", "tenant_key", "target_tenant_key"}
            or normalized_key.endswith(("_id", "_ids"))
        )
        if sensitive and isinstance(value, str):
            return cls._masked(value)
        if isinstance(value, list):
            return [cls._redact_identifiers(item, key) for item in value]
        if isinstance(value, dict):
            return {str(item_key): cls._redact_identifiers(item, str(item_key)) for item_key, item in value.items()}
        return value

    def _detail(self, token: str, tenant_key: str, entity: dict[str, object], kind: str) -> dict[str, object] | None:
        identifier = self._entity_identifier(entity, kind)
        if identifier is None:
            logger.info("Organization probe timeline: %s detail skipped because no usable identifier", kind)
            return None
        value, identifier_type = identifier
        noun = "collaboration_users" if kind == "user" else "collaboration_departments"
        query_name = "target_user_id_type" if kind == "user" else "target_department_id_type"
        response = self._optional_json(Request(
            "https://open.feishu.cn/open-apis/trust_party/v1/collaboration_tenants/"
            + quote(tenant_key, safe="")
            + "/"
            + noun
            + "/"
            + quote(value, safe="")
            + "?"
            + urlencode({query_name: identifier_type}),
            headers={"Authorization": f"Bearer {token}"},
        ))
        data = response.get("data") if isinstance(response, dict) and isinstance(response.get("data"), dict) else {}
        detail_key = "target_user" if kind == "user" else "target_department"
        detail = data.get(detail_key)
        ok = isinstance(detail, dict)
        logger.info(
            "Organization probe timeline: %s detail queried id=%s type=%s success=%s returned_fields=%s",
            kind, self._masked(value), identifier_type, ok, sorted(detail) if ok else [],
        )
        return detail if ok else None

    def _visible_entities(
        self, token: str, tenant_key: str, department: dict[str, object] | None = None,
    ) -> tuple[list[dict[str, object]], int]:
        """读取一个可见范围（根组织或某部门）的所有分页条目。"""
        department_identifier = self._entity_identifier(department, "department") if department else None
        all_entities: list[dict[str, object]] = []
        page_token: str | None = None
        for page_number in range(1, 51):
            query: dict[str, object] = {"page_size": 100}
            if department_identifier is not None:
                query["target_department_id"] = department_identifier[0]
                query["department_id_type"] = department_identifier[1]
            if page_token:
                query["page_token"] = page_token
            visible = self._optional_json(Request(
                "https://open.feishu.cn/open-apis/trust_party/v1/collaboration_tenants/"
                + quote(tenant_key, safe="")
                + "/visible_organization?"
                + urlencode(query),
                headers={"Authorization": f"Bearer {token}"},
            ))
            data = visible.get("data") if isinstance(visible, dict) and isinstance(visible.get("data"), dict) else {}
            entities = data.get("collaboration_entity_list")
            if not isinstance(entities, list):
                logger.info("Organization probe timeline: visible organization page=%s returned no entity list fields=%s", page_number, sorted(data))
                break
            page_entities = [entity for entity in entities if isinstance(entity, dict)]
            all_entities.extend(page_entities)
            has_more = data.get("has_more") is True
            next_token = data.get("page_token")
            logger.info(
                "Organization probe timeline: visible organization scope=%s page=%s entities=%s has_more=%s",
                self._masked(department_identifier[0]) if department_identifier else "root", page_number, len(page_entities), has_more,
            )
            if not has_more or not isinstance(next_token, str) or not next_token or next_token == page_token:
                return all_entities, page_number
            page_token = next_token
        return all_entities, 50

    def _organization_probe(self, token: str) -> dict[str, object]:
        """严格按本次用户身份：列组织、选四达、读部门和成员详情、找王志鹏。"""
        tenants = self._optional_json(Request(
            "https://open.feishu.cn/open-apis/trust_party/v1/collaboration_tenants?" + urlencode({"page_size": 100}),
            headers={"Authorization": f"Bearer {token}"},
        ))
        tenant_data = tenants.get("data") if isinstance(tenants, dict) and isinstance(tenants.get("data"), dict) else {}
        candidates = [record for record, _ in self._walk(tenant_data) if "tenant_key" in record or "target_tenant_key" in record]
        target = next((record for record in candidates if "四达集团" in self._text(record)), None)
        logger.info("Organization probe timeline: visible tenant list received candidates=%s target_found=%s", len(candidates), bool(target))
        if target is None:
            return {"关联组织列表": "已读取，但未找到名称含“四达集团”的组织", "候选组织数": len(candidates)}
        target_key = target.get("tenant_key") or target.get("target_tenant_key")
        if not isinstance(target_key, str) or not target_key:
            return {"关联组织列表": "已找到四达集团，但接口未返回可查询组织标识"}
        root_entities, page_count = self._visible_entities(token, target_key)
        if not root_entities:
            return {"命中关联组织": self._redact_identifiers(self._bounded_debug_value(target)), "组织遍历": "未返回可见部门和成员条目"}
        # 根范围只会给出一级部门。以每个部门再次作为范围读取，直到没有新部门。
        entity_by_identifier: dict[tuple[str, str], dict[str, object]] = {}
        pending_departments: list[tuple[dict[str, object], int]] = []
        target_user_seen = False

        def collect(entities: list[dict[str, object]], depth: int) -> None:
            nonlocal target_user_seen
            for entity in entities:
                identifier = self._entity_identifier(entity, "department") or self._entity_identifier(entity, "user")
                if identifier is None or identifier in entity_by_identifier:
                    continue
                entity_by_identifier[identifier] = entity
                if self._entity_identifier(entity, "department"):
                    pending_departments.append((entity, depth))
                elif "王志鹏" in self._normalized_text(entity.get("user_name")):
                    target_user_seen = True
        collect(root_entities, 0)
        max_depth = 0
        department_scopes_read = 0
        while pending_departments and department_scopes_read < 100 and not target_user_seen:
            department, depth = pending_departments.pop(0)
            children, child_pages = self._visible_entities(token, target_key, department)
            page_count += child_pages
            department_scopes_read += 1
            max_depth = max(max_depth, depth + 1)
            collect(children, depth + 1)
        entity_records = list(entity_by_identifier.values())
        departments = [entity for entity in entity_records if self._entity_identifier(entity, "department")]
        users = [entity for entity in entity_records if self._entity_identifier(entity, "user")]
        logger.info(
            "Organization probe timeline: recursive visible organization completed entities=%s departments=%s users=%s scopes=%s max_depth=%s",
            len(entity_records), len(departments), len(users), department_scopes_read, max_depth,
        )

        department_details: list[dict[str, object]] = []
        for department in departments[:100]:
            detail = self._detail(token, target_key, department, "department")
            if detail is not None:
                department_details.append(detail)
        logger.info(
            "Organization probe timeline: department detail traversal completed requested=%s succeeded=%s minimum_one_level=%s",
            len(departments[:100]), len(department_details), bool(department_details),
        )

        wang_source = ""
        wang_entity = next((entity for entity in users if "王志鹏" in self._normalized_text(entity.get("user_name"))), None)
        if wang_entity is not None:
            wang_source = "可见组织成员条目"
        # 逐个取成员详情：名单只负责定位，资料以成员详情接口的返回为准。
        wang_detail: dict[str, object] | None = None
        user_details_attempted = 0
        # 已在成员条目命中时优先读取该人的详情；避免为找一个人遍历所有成员资料。
        users_to_query = [wang_entity] if wang_entity is not None else users[:100]
        for user in users_to_query:
            detail = self._detail(token, target_key, user, "user")
            user_details_attempted += 1
            if detail is not None and "王志鹏" in self._normalized_text(detail.get("name")):
                wang_detail = detail
                wang_entity = user
                wang_source = "成员详情"
                break
        logger.info(
            "Organization probe timeline: member detail traversal completed requested=%s wang_found=%s source=%s",
            user_details_attempted, wang_detail is not None or wang_entity is not None, wang_source or "none",
        )
        wang_result: object = "未在该用户可见的关联组织成员范围中找到"
        if wang_detail is not None:
            parent_identifiers: set[str] = set()
            for parent in wang_detail.get("parent_department_ids", []):
                if isinstance(parent, dict):
                    parent_identifiers.update(
                        value for value in (parent.get("department_id"), parent.get("open_department_id"))
                        if isinstance(value, str) and value
                    )
            matched_departments = [
                detail for detail in department_details
                if any(value in parent_identifiers for value in (detail.get("department_id"), detail.get("open_department_id")))
            ]
            enriched_detail = dict(wang_detail)
            enriched_detail["parent_departments"] = matched_departments
            wang_result = self._redact_identifiers(self._bounded_debug_value(enriched_detail))
        elif wang_entity is not None:
            # 即使成员详情暂不可读，也明确给出名单层已经找到的事实，避免误报“不存在”。
            wang_result = {"名单匹配": "已找到；成员详情接口未返回", "成员条目": self._redact_identifiers(self._bounded_debug_value(wang_entity))}
        logger.info(
            "Organization probe timeline: Fourda probe finished tenant=%s wang_found=%s",
            self._masked(target_key), wang_result != "未在该用户可见的关联组织成员范围中找到",
        )
        return {
            "命中关联组织": self._redact_identifiers(self._bounded_debug_value(target)),
            "组织遍历": {
                "已读取分页数": page_count,
                "部门范围下钻数": department_scopes_read,
                "最大部门下钻层级": max_depth,
                "可见部门条目数": len(departments),
                "可见成员条目数": len(users),
                "部门详情读取数": len(department_details),
                "成员详情读取数": user_details_attempted,
                "部门下钻": "对根组织及每个可见部门调用可见组织接口；部门详情接口用于补充部门资料，成员以可见条目逐个调用成员详情接口读取。",
            },
            "王志鹏": wang_result,
        }

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
        visible_organization: object = self._organization_probe(token)
        return {
            "说明": "仅供 Bot-Test 当前用户身份组织查询；短期 access token 不写入日志、数据库或 Worker 存储。",
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
            "可见关联组织": visible_organization,
        }

    def from_authorization_code(self, code: str) -> LoadedOAuthIdentity:
        token_request = Request(
            "https://open.feishu.cn/open-apis/authen/v2/oauth/token",
            data=json.dumps({"grant_type": "authorization_code", "client_id": self._app_id, "client_secret": self._app_secret, "code": code, "redirect_uri": self._redirect_uri}).encode(),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        token = self._json(token_request)
        refresh_token = token.get("refresh_token")
        refresh_token_expires_in = token.get("refresh_token_expires_in")
        if (
            token.get("code") not in (0, "0")
            or not isinstance(token.get("access_token"), str)
            or not isinstance(refresh_token, str)
            or not refresh_token
            or not isinstance(refresh_token_expires_in, int)
            or refresh_token_expires_in <= 0
        ):
            raise ValueError("飞书未返回可续期的用户授权")
        info = self._json(Request("https://open.feishu.cn/open-apis/authen/v1/user_info", headers={"Authorization": f"Bearer {token['access_token']}"}))
        data = info.get("data", info)
        if not isinstance(data, dict):
            raise ValueError("飞书身份资料格式异常")
        profile = IdentityProfile(
            str(data.get("open_id") or ""), str(data.get("user_id") or ""), str(data.get("union_id") or ""), str(data.get("name") or ""),
            str(data["department"]) if data.get("department") else None,
            str(data["tenant_key"]) if data.get("tenant_key") else None,
            str(data["locale"]) if data.get("locale") else None,
        )
        scope = token.get("scope")
        return LoadedOAuthIdentity(
            profile,
            self._debug_details(str(token["access_token"]), data, profile) if self._debug_identity_display else None,
            OAuthTokenGrant(refresh_token, refresh_token_expires_in, scope if isinstance(scope, str) else ""),
        )


class OAuthResultProcessor:
    """每次结果都先验证原用户绑定，再允许建立身份。"""

    def __init__(
        self,
        store: AuthorizationStateStore,
        service: OnboardingService,
        loader: IdentityLoader,
        result_sender: OAuthBridgeResultSender,
        event_key: str,
        token_vault: RefreshTokenVault | None = None,
        organization_probe_only: bool = False,
        persist_probe_credential: bool = False,
        debug_identity_display: bool = False,
    ) -> None:
        self._store = store
        self._service = service
        self._loader = loader
        self._result_sender = result_sender
        self._event_key = event_key.encode()
        self._token_vault = token_vault
        self._organization_probe_only = organization_probe_only
        self._persist_probe_credential = persist_probe_credential
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
            logger.info("OAuth timeline: user cancelled authorization")
            self._store.cancel_authorizing_state(message.state)
            self._result_sender.send_result(message.state, "retry")
            return

        try:
            logger.info("OAuth timeline: authorization callback received")
            if not self._store.claim_authorizing_state(message.state):
                logger.info("OAuth timeline: rejected replayed or expired authorization callback")
                self._result_sender.send_result(message.state, "retry")
                return
            logger.info("OAuth timeline: callback state claimed; exchanging authorization code")
            loaded = self._loader.from_authorization_code(message.code or "")
            logger.info("OAuth timeline: identity and renewable authorization received")
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
                logger.info("OAuth timeline: authorized identity did not match original private chat")
                self._store.cancel_authorizing_state(message.state)
                self._result_sender.send_result(message.state, "retry", debug_identity, loaded.debug_details if self._debug_identity_display else None)
                return
            if self._organization_probe_only:
                if self._persist_probe_credential:
                    if self._token_vault is None or loaded.token_grant is None:
                        raise ValueError("本次飞书授权未返回可安全保存的续期凭证")
                    # 探针不建档；仅按用户明确同意的 offline_access 保存加密 refresh_token。
                    self._token_vault.save(profile.open_id, loaded.token_grant)
                    logger.info("OAuth timeline: organization probe credential encrypted and stored")
                else:
                    logger.info("OAuth timeline: organization probe completed; no identity record or credential stored")
                self._result_sender.send_result(message.state, "identity_confirmed", debug_identity, loaded.debug_details if self._debug_identity_display else None)
                return
            event_id = "oauth:" + hmac.new(self._event_key, (message.code or "").encode(), hashlib.sha256).hexdigest()
            self._service.authorization_succeeded(event_id, profile)
            logger.info("OAuth timeline: authorized identity recorded")
            if self._token_vault is not None:
                if loaded.token_grant is None:
                    raise ValueError("本次飞书授权未返回续期凭证")
                self._token_vault.save(profile.open_id, loaded.token_grant)
                logger.info("OAuth timeline: encrypted refresh credential stored")
        except Exception as error:
            # 无论外部授权失败、资料不完整或连接异常，都不把细节暴露给用户。
            # 仅记录异常类别，便于受控验收定位；绝不记录 code、令牌或身份资料。
            logger.warning("OAuth identity load failed: %s", type(error).__name__)
            self._store.cancel_authorizing_state(message.state)
            self._result_sender.send_result(message.state, "retry")
            return
        self._result_sender.send_result(message.state, "identity_confirmed", debug_identity, loaded.debug_details if self._debug_identity_display else None)
        logger.info("OAuth timeline: authorization completed")
