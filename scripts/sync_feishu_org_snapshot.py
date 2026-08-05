#!/usr/bin/env python3
"""把飞书关联组织的完整非凭据响应写入测试库快照。

本脚本是一次性受控验收入口，不是正式员工开通入口。它保存组织接口返回的
原始业务字段（包括 ID、姓名、状态及接口实际返回的可选资料），但不会保存
OAuth token、App Secret、授权码或数据库连接材料。所有成员数据先在内存中
完成两条路径的完整性校验，再在一个数据库事务中写入同一轮快照。
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import psycopg
from psycopg.types.json import Jsonb

from lingxi.adapters.oauth_bridge import FeishuOAuthIdentityLoader, OAuthTokenGrant
from lingxi.adapters.refresh_tokens import PostgresRefreshTokenVault


BASE_URL = "https://open.feishu.cn/open-apis"
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "migrations", "005_create_feishu_org_snapshot.sql")
REQUEST_PAUSE_SECONDS = 0.12
DETAIL_PAUSE_SECONDS = 0.12
RETENTION_DAYS = 90


class SyncError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class AppTenantData:
    record: dict[str, Any]
    pages: list[dict[str, Any]]
    departments: dict[str, list[dict[str, Any]]]
    members: dict[str, list[dict[str, Any]]]


@dataclass
class UserTenantData:
    record: dict[str, Any]
    pages: list[dict[str, Any]]
    departments: dict[str, list[dict[str, Any]]]
    department_entities: dict[str, dict[str, Any]]
    department_details: dict[str, dict[str, Any]]
    members: dict[str, list[dict[str, Any]]]
    member_details: dict[str, dict[str, Any]]


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SyncError(f"missing_env_{name}")
    return value


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def request_json(
    url: str,
    *,
    token: str | None = None,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    retry_transport: bool = True,
) -> dict[str, Any]:
    payload_bytes = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {"Content-Type": "application/json; charset=utf-8"} if body is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    attempts = 3 if retry_transport else 1
    for attempt in range(attempts):
        if REQUEST_PAUSE_SECONDS:
            time.sleep(REQUEST_PAUSE_SECONDS)
        request = Request(url, data=payload_bytes, headers=headers, method=method)
        try:
            with urlopen(request, timeout=20) as response:  # noqa: S310 -- 固定飞书 HTTPS 地址
                value = json.loads(response.read())
        except HTTPError as error:
            try:
                value = json.loads(error.read())
            except Exception as parse_error:
                raise SyncError(f"http_{error.code}") from parse_error
        except (URLError, OSError, TimeoutError) as error:
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise SyncError("transport_error") from error
        except (TypeError, ValueError) as error:
            raise SyncError("invalid_json") from error
        if not isinstance(value, dict):
            raise SyncError("invalid_response_shape")
        if value.get("code") not in (None, 0, "0"):
            raise SyncError(f"feishu_code_{value.get('code')}")
        return value
    raise SyncError("transport_error")


def app_access_token(app_id: str, app_secret: str) -> str:
    response = request_json(
        f"{BASE_URL}/auth/v3/tenant_access_token/internal",
        method="POST",
        body={"app_id": app_id, "app_secret": app_secret},
        retry_transport=False,
    )
    token = response.get("tenant_access_token")
    if not isinstance(token, str) or not token:
        raise SyncError("tenant_access_token_missing")
    return token


def refresh_saved_user_token() -> str:
    vault = PostgresRefreshTokenVault(required("LINGXI_POSTGRES_DSN"), required("LINGXI_OAUTH_REFRESH_TOKEN_KEY"))
    with vault._psycopg.connect(vault._dsn) as connection, connection.cursor() as cursor:  # noqa: SLF001
        cursor.execute(
            "SELECT feishu_open_id, encrypted_refresh_token "
            "FROM feishu_user_refresh_token ORDER BY updated_at DESC LIMIT 2"
        )
        rows = cursor.fetchall()
    if len(rows) != 1:
        raise SyncError("saved_user_credential_not_unique")
    open_id, encrypted = str(rows[0][0]), bytes(rows[0][1])
    refresh_token = vault._cipher.decrypt(encrypted).decode()  # noqa: SLF001
    response = request_json(
        f"{BASE_URL}/authen/v2/oauth/token",
        method="POST",
        body={
            "grant_type": "refresh_token",
            "client_id": required("FEISHU_APP_ID"),
            "client_secret": required("FEISHU_APP_SECRET"),
            "refresh_token": refresh_token,
        },
        retry_transport=False,
    )
    access_token = response.get("access_token")
    next_refresh = response.get("refresh_token")
    expires = response.get("refresh_token_expires_in")
    if not isinstance(access_token, str) or not isinstance(next_refresh, str) or not isinstance(expires, int) or expires <= 0:
        raise SyncError("user_token_refresh_incomplete")
    # refresh_token 一次性有效：成功响应后的第一项持久化动作必须轮换写回。
    scope = response.get("scope")
    vault.replace(open_id, OAuthTokenGrant(next_refresh, expires, scope if isinstance(scope, str) else ""))
    return access_token


def walk_dicts(value: object):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def tenant_key(record: dict[str, Any]) -> str | None:
    value = record.get("tenant_key") or record.get("target_tenant_key")
    return value if isinstance(value, str) and value else None


def tenant_records(response: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in walk_dicts(response.get("data", {})):
        key = tenant_key(record)
        if key:
            result.setdefault(key, record)
    return result


def entity_key(entity: dict[str, Any], kind: str) -> tuple[str, str] | None:
    if kind == "department":
        choices = (("open_department_id", "open_department_id"), ("department_id", "department_id"))
    else:
        choices = (
            ("open_user_id", "open_id"),
            ("open_id", "open_id"),
            ("user_id", "user_id"),
            ("union_user_id", "union_id"),
            ("union_id", "union_id"),
        )
    for field, identifier_type in choices:
        value = entity.get(field)
        if isinstance(value, str) and value:
            return value, identifier_type
    return None


def app_scope(token: str, target_tenant: str) -> AppTenantData:
    departments: dict[str, list[dict[str, Any]]] = defaultdict(list)
    members: dict[str, list[dict[str, Any]]] = defaultdict(list)
    pages: list[dict[str, Any]] = []
    queue: list[str | None] = [None]
    visited: set[str | None] = set()
    while queue:
        department_id = queue.pop(0)
        if department_id in visited:
            continue
        visited.add(department_id)
        page_token: str | None = None
        for _ in range(50):
            query: dict[str, Any] = {
                "target_tenant_key": target_tenant,
                "is_select_subject": "false",
                "page_size": 100,
            }
            if department_id:
                query["target_department_id"] = department_id
            if page_token:
                query["page_token"] = page_token
            response = request_json(f"{BASE_URL}/directory/v1/share_entities?{urlencode(query)}", token=token)
            pages.append(response)
            data = response.get("data") if isinstance(response.get("data"), dict) else {}
            for department in data.get("share_departments") or []:
                if not isinstance(department, dict):
                    continue
                identifier = entity_key(department, "department")
                if identifier is None:
                    raise SyncError("app_department_id_missing")
                key = identifier[0]
                departments[key].append(department)
                if key not in visited:
                    queue.append(key)
            for member in data.get("share_users") or []:
                if not isinstance(member, dict):
                    continue
                identifier = entity_key(member, "user")
                if identifier is None:
                    raise SyncError("app_member_id_missing")
                members[identifier[0]].append(member)
            has_more = data.get("has_more") is True
            next_token = data.get("page_token")
            if not has_more or not isinstance(next_token, str) or not next_token or next_token == page_token:
                break
            page_token = next_token
        else:
            raise SyncError("app_scope_pagination_limit")
    return AppTenantData({}, pages, dict(departments), dict(members))


def user_scope(loader: FeishuOAuthIdentityLoader, token: str, target_tenant: str) -> UserTenantData:
    pages: list[dict[str, Any]] = []
    departments: dict[str, list[dict[str, Any]]] = defaultdict(list)
    department_entities: dict[str, dict[str, Any]] = {}
    members: dict[str, list[dict[str, Any]]] = defaultdict(list)
    queue: list[dict[str, Any] | None] = [None]
    visited: set[tuple[str, str]] = set()

    while queue:
        department = queue.pop(0)
        department_identifier = entity_key(department, "department") if department else None
        visit_key = department_identifier or ("root", "root")
        if visit_key in visited:
            continue
        visited.add(visit_key)
        page_token: str | None = None
        for _ in range(50):
            query: dict[str, Any] = {"page_size": 100}
            if department_identifier:
                query["target_department_id"] = department_identifier[0]
                query["department_id_type"] = department_identifier[1]
            if page_token:
                query["page_token"] = page_token
            response = request_json(
                f"{BASE_URL}/trust_party/v1/collaboration_tenants/{target_tenant}/visible_organization?{urlencode(query)}",
                token=token,
            )
            pages.append(response)
            data = response.get("data") if isinstance(response.get("data"), dict) else {}
            entities = data.get("collaboration_entity_list")
            if not isinstance(entities, list):
                raise SyncError("user_visible_entity_list_missing")
            for entity in entities:
                if not isinstance(entity, dict):
                    continue
                department_id = entity_key(entity, "department")
                if department_id:
                    departments[department_id[0]].append(entity)
                    department_entities.setdefault(department_id[0], entity)
                    if department_id not in visited:
                        queue.append(entity)
                    continue
                member_id = entity_key(entity, "user")
                if member_id is None:
                    raise SyncError("user_member_id_missing")
                members[member_id[0]].append(entity)
            has_more = data.get("has_more") is True
            next_token = data.get("page_token")
            if not has_more or not isinstance(next_token, str) or not next_token or next_token == page_token:
                break
            page_token = next_token
        else:
            raise SyncError("user_visible_pagination_limit")

    department_details: dict[str, dict[str, Any]] = {}
    for department_id, entity in department_entities.items():
        time.sleep(DETAIL_PAUSE_SECONDS)
        detail = detail_request(token, target_tenant, entity, "department")
        if detail is None:
            raise SyncError("department_detail_missing")
        department_details[department_id] = detail

    member_details: dict[str, dict[str, Any]] = {}
    for index, (member_id, entities) in enumerate(members.items(), start=1):
        time.sleep(DETAIL_PAUSE_SECONDS)
        detail = detail_request(token, target_tenant, entities[0], "user")
        if detail is None:
            raise SyncError("member_detail_missing")
        member_details[member_id] = detail
        if index % 50 == 0:
            print(json.dumps({"stage": "user_details", "tenant_prefix": target_tenant[:6], "details": index}, ensure_ascii=False), flush=True)

    return UserTenantData({}, pages, dict(departments), dict(department_entities), department_details, dict(members), member_details)


def detail_request(token: str, target_tenant: str, entity: dict[str, Any], kind: str) -> dict[str, Any] | None:
    identifier = entity_key(entity, kind)
    if identifier is None:
        return None
    value, identifier_type = identifier
    noun = "collaboration_users" if kind == "user" else "collaboration_departments"
    query_name = "target_user_id_type" if kind == "user" else "target_department_id_type"
    response = request_json(
        f"{BASE_URL}/trust_party/v1/collaboration_tenants/{quote(target_tenant, safe='')}/{noun}/{quote(value, safe='')}?"
        f"{urlencode({query_name: identifier_type})}",
        token=token,
    )
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    detail_key = "target_user" if kind == "user" else "target_department"
    detail = data.get(detail_key)
    return detail if isinstance(detail, dict) else None


def nonempty(value: object) -> bool:
    return value not in (None, "", [], {})


def first_value(*values: object) -> object:
    for value in values:
        if nonempty(value):
            return value
    return None


def json_value(value: object, default: object) -> object:
    return default if value is None else value


def ensure_schema(connection: psycopg.Connection[Any]) -> None:
    expected = {
        "feishu_org_sync_run",
        "feishu_org_tenant_snapshot",
        "feishu_org_department_snapshot",
        "feishu_org_member_snapshot",
    }
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND c.relname = ANY(%s)",
            (list(expected),),
        )
        existing = {str(row[0]) for row in cursor.fetchall()}
    if not existing:
        with open(SCHEMA_PATH, encoding="utf-8") as schema_file:
            schema = schema_file.read()
        connection.execute(schema)
        connection.commit()
    elif existing != expected:
        raise SyncError("snapshot_schema_partial")


def insert_failed_run(dsn: str, run_id: str, app_id: str, code: str) -> None:
    try:
        with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO feishu_org_sync_run "
                "(id, source_app_id, status, completed_at, expires_at, error_code) "
                "VALUES (%s, %s, 'failed', now(), now() + interval '90 days', %s) "
                "ON CONFLICT (id) DO NOTHING",
                (run_id, app_id, code[:120]),
            )
    except Exception:
        pass


def write_snapshot(
    dsn: str,
    run_id: str,
    app_id: str,
    app_tenants: dict[str, dict[str, Any]],
    app_data: dict[str, AppTenantData],
    user_tenants: dict[str, dict[str, Any]],
    user_data: dict[str, UserTenantData],
) -> dict[str, int | str]:
    expires_at = datetime.now(timezone.utc) + timedelta(days=RETENTION_DAYS)
    tenant_rows: list[tuple[Any, ...]] = []
    department_rows: list[tuple[Any, ...]] = []
    member_rows: list[tuple[Any, ...]] = []
    total_departments = 0
    total_members = 0
    total_details = 0
    completeness = {"open_id": 0, "user_id": 0, "union_id": 0}

    for target_tenant, app_tenant in app_tenants.items():
        app = app_data[target_tenant]
        user = user_data[target_tenant]
        tenant_rows.append(
            (
                new_id("tenant"), run_id, target_tenant,
                target_tenant in user_tenants,
                Jsonb({"directory_tenant": app_tenant, "share_scope_pages": app.pages}),
                Jsonb({"trust_party_tenant": user_tenants.get(target_tenant, {}), "visible_organization_pages": user.pages}),
            )
        )
        department_keys = sorted(set(app.departments) | set(user.departments))
        total_departments += len(department_keys)
        for department_key in department_keys:
            entity = user.department_entities.get(department_key) or (app.departments.get(department_key) or [{}])[0]
            identifier = entity_key(entity, "department")
            department_rows.append(
                (
                    new_id("dept"), run_id, target_tenant, department_key,
                    entity.get("open_department_id"), entity.get("department_id"),
                    Jsonb(app.departments.get(department_key, [])),
                    Jsonb(user.departments.get(department_key, [])),
                    Jsonb(user.department_details.get(department_key, {})),
                )
            )
        member_keys = sorted(set(app.members) | set(user.members))
        total_members += len(member_keys)
        for member_key in member_keys:
            app_records = app.members.get(member_key, [])
            user_records = user.members.get(member_key, [])
            detail = user.member_details.get(member_key, {})
            if not detail:
                # visible organization 的成员键可能用 user_id/union_id；按详情里的 open_id 再做一次归并。
                detail_key = first_value(detail.get("open_id"), detail.get("user_id")) if detail else None
                if isinstance(detail_key, str):
                    detail = user.member_details.get(detail_key, {})
            open_user_id = first_value(
                *(record.get("open_user_id") for record in app_records),
                *(record.get("open_user_id") for record in user_records),
            )
            open_id = first_value(detail.get("open_id"), *(record.get("open_id") for record in user_records))
            user_id = first_value(detail.get("user_id"), *(record.get("user_id") for record in user_records))
            union_id = first_value(detail.get("union_id"), *(record.get("union_id") for record in user_records))
            union_user_id = first_value(detail.get("union_user_id"), *(record.get("union_user_id") for record in user_records))
            if not (isinstance(open_id, str) and isinstance(user_id, str) and (isinstance(union_id, str) or isinstance(union_user_id, str))):
                raise SyncError("member_identity_fields_incomplete")
            total_details += 1
            completeness["open_id"] += isinstance(open_id, str)
            completeness["user_id"] += isinstance(user_id, str)
            completeness["union_id"] += isinstance(union_id, str) or isinstance(union_user_id, str)
            source = detail or (user_records[0] if user_records else (app_records[0] if app_records else {}))
            member_rows.append(
                (
                    new_id("member"), run_id, target_tenant, member_key,
                    open_user_id, open_id, user_id, union_id, union_user_id,
                    Jsonb(json_value(source.get("name"), {})),
                    Jsonb(json_value(source.get("i18n_name", source.get("i18n_user_name")), {})),
                    Jsonb(json_value(source.get("avatar", source.get("user_avatar")), {})),
                    Jsonb(json_value(source.get("status", source.get("user_status")), {})),
                    Jsonb(json_value(source.get("parent_department_ids"), [])),
                    Jsonb(app_records), Jsonb(user_records), Jsonb(detail),
                )
            )

    metadata = {
        "retention_days": RETENTION_DAYS,
        "source_paths": ["directory_share_entities", "trust_party_visible_organization", "trust_party_collaboration_details"],
        "raw_api_fields_saved": True,
        "credentials_saved": False,
        "id_completeness": completeness,
    }
    with psycopg.connect(dsn) as connection:
        ensure_schema(connection)
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO feishu_org_sync_run "
                    "(id, source_app_id, status, completed_at, expires_at, app_tenant_count, "
                    "user_visible_tenant_count, tenant_count, department_count, member_count, "
                    "member_detail_count, metadata) VALUES (%s, %s, 'complete', now(), %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        run_id, app_id, expires_at, len(app_tenants), len(user_tenants), len(tenant_rows),
                        total_departments, total_members, total_details, Jsonb(metadata),
                    ),
                )
                cursor.executemany(
                    "INSERT INTO feishu_org_tenant_snapshot "
                    "(id, sync_run_id, tenant_key, visible_to_user_identity, app_record, user_record) "
                    "VALUES (%s, %s, %s, %s, %s, %s)", tenant_rows
                )
                cursor.executemany(
                    "INSERT INTO feishu_org_department_snapshot "
                    "(id, sync_run_id, tenant_key, department_key, open_department_id, department_id, "
                    "app_records, user_records, user_detail) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    department_rows,
                )
                cursor.executemany(
                    "INSERT INTO feishu_org_member_snapshot "
                    "(id, sync_run_id, tenant_key, member_key, open_user_id, open_id, user_id, union_id, "
                    "union_user_id, name, i18n_name, avatar, status, parent_department_ids, app_records, "
                    "user_records, detail_record) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    member_rows,
                )
    return {
        "run_id_prefix": run_id[:14] + "…",
        "tenants": len(tenant_rows),
        "departments": total_departments,
        "members": total_members,
        "details": total_details,
        "open_id": completeness["open_id"],
        "user_id": completeness["user_id"],
        "union_id": completeness["union_id"],
    }


def main() -> int:
    test_dsn = required("LINGXI_TEST_POSTGRES_DSN")
    app_id = required("FEISHU_APP_ID")
    app_secret = required("FEISHU_APP_SECRET")
    run_id = new_id("orgsync")
    try:
        with psycopg.connect(test_dsn) as connection:
            ensure_schema(connection)
        app_token = app_access_token(app_id, app_secret)
        app_listing = request_json(
            f"{BASE_URL}/directory/v1/collaboration_tenants?{urlencode({'page_size': 100})}",
            token=app_token,
        )
        app_tenants = tenant_records(app_listing)
        if not app_tenants:
            raise SyncError("app_tenant_list_empty")
        print(json.dumps({"stage": "app_tenants", "count": len(app_tenants)}, ensure_ascii=False), flush=True)

        user_token = refresh_saved_user_token()
        user_listing = request_json(
            f"{BASE_URL}/trust_party/v1/collaboration_tenants?{urlencode({'page_size': 100})}",
            token=user_token,
        )
        user_tenants = tenant_records(user_listing)
        missing = sorted(set(app_tenants) - set(user_tenants))
        if missing:
            raise SyncError("user_visible_tenant_set_incomplete")
        print(json.dumps({"stage": "user_tenants", "count": len(user_tenants)}, ensure_ascii=False), flush=True)

        loader = FeishuOAuthIdentityLoader("probe", "probe", "https://example.test")
        app_data: dict[str, AppTenantData] = {}
        user_data: dict[str, UserTenantData] = {}
        for index, target_tenant in enumerate(sorted(app_tenants), start=1):
            print(json.dumps({"stage": "tenant_start", "index": index, "total": len(app_tenants)}, ensure_ascii=False), flush=True)
            app_result = app_scope(app_token, target_tenant)
            app_result.record = app_tenants[target_tenant]
            app_data[target_tenant] = app_result
            user_result = user_scope(loader, user_token, target_tenant)
            user_result.record = user_tenants[target_tenant]
            app_ids = set(app_result.members)
            user_ids = set(user_result.member_details)
            if app_ids != user_ids:
                raise SyncError("app_user_member_sets_differ")
            if not app_ids:
                raise SyncError("tenant_member_set_empty")
            user_data[target_tenant] = user_result
            print(
                json.dumps(
                    {"stage": "tenant_complete", "index": index, "app_members": len(app_ids), "user_details": len(user_ids)},
                    ensure_ascii=False,
                ),
                flush=True,
            )

        summary = write_snapshot(test_dsn, run_id, app_id, app_tenants, app_data, user_tenants, user_data)
        print(json.dumps({"status": "complete", "database": "test", **summary}, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        code = error.code if isinstance(error, SyncError) else type(error).__name__
        insert_failed_run(test_dsn, run_id, app_id, code)
        print(json.dumps({"status": "failed", "error_code": code}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
