#!/usr/bin/env python3
"""只读读取飞书多维表格，并与测试库中的组织快照做关联统计。

本脚本不写入多维表格、不把多维表格原始记录写入数据库，也不输出员工资料或凭据。
它会按既有安全机制刷新并轮换加密保存的用户续期凭据；短期 user_access_token
只在本进程内使用。实际关联结果由 ``人员ID = user_id`` 的精确 ID 关系给出。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from lingxi.adapters.postgres import connect
from lingxi.adapters.feishu_bitable_association import analyze_association


BASE_URL = "https://open.feishu.cn/open-apis"


class BitableReadError(RuntimeError):
    def __init__(self, stage: str, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.stage = stage
        self.code = code
        self.detail = detail


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise BitableReadError("configuration", f"missing_env_{name}")
    return value


def _request_json(stage: str, path: str, token: str, params: Mapping[str, object] | None = None) -> dict[str, Any]:
    url = f"{BASE_URL}{path}"
    if params:
        url += "?" + urlencode(params)
    request = Request(url, headers={"Authorization": f"Bearer {token}"}, method="GET")
    try:
        with urlopen(request, timeout=20) as response:  # noqa: S310 -- fixed Feishu HTTPS base URL
            body = response.read()
            http_status = response.status
    except HTTPError as error:
        body = error.read()
        http_status = error.code
    except (URLError, OSError, TimeoutError) as error:
        raise BitableReadError(stage, "transport_error", type(error).__name__) from error
    try:
        value = json.loads(body)
    except (TypeError, ValueError) as error:
        raise BitableReadError(stage, "invalid_json", f"http_{http_status}") from error
    if not isinstance(value, dict):
        raise BitableReadError(stage, "invalid_response_shape", f"http_{http_status}")
    code = value.get("code")
    if http_status >= 400 or code not in (None, 0, "0"):
        message = value.get("msg")
        raise BitableReadError(
            stage,
            f"feishu_code_{code}",
            json.dumps({"http_status": http_status, "code": code, "msg": message}, ensure_ascii=False),
        )
    return value


def _data(response: Mapping[str, Any]) -> dict[str, Any]:
    value = response.get("data")
    return value if isinstance(value, dict) else {}


def _page_items(stage: str, path: str, token: str, item_key: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    page_token: str | None = None
    for _ in range(100):
        params: dict[str, object] = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        data = _data(_request_json(stage, path, token, params))
        items = data.get(item_key)
        if not isinstance(items, list):
            raise BitableReadError(stage, "items_missing", item_key)
        result.extend(item for item in items if isinstance(item, dict))
        if data.get("has_more") is not True:
            return result
        next_token = data.get("page_token")
        if not isinstance(next_token, str) or not next_token or next_token == page_token:
            raise BitableReadError(stage, "pagination_stalled")
        page_token = next_token
    raise BitableReadError(stage, "pagination_limit")


def _read_latest_snapshot(dsn: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        with connect(dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, completed_at, expires_at, member_count "
                "FROM feishu_org_sync_run WHERE status = 'complete' "
                "ORDER BY completed_at DESC LIMIT 1"
            )
            run = cursor.fetchone()
            if run is None:
                raise BitableReadError("snapshot", "complete_run_missing")
            run_id, completed_at, expires_at, member_count = run
            cursor.execute(
                "SELECT tenant_key, name, open_id, user_id, union_id, union_user_id "
                "FROM feishu_org_member_snapshot WHERE sync_run_id = %s",
                (run_id,),
            )
            rows = [
                {
                    "tenant_key": tenant_key,
                    "name": name,
                    "open_id": open_id,
                    "user_id": user_id,
                    "union_id": union_id,
                    "union_user_id": union_user_id,
                }
                for tenant_key, name, open_id, user_id, union_id, union_user_id in cursor.fetchall()
            ]
    except ImportError as error:  # pragma: no cover - controlled runtime dependency
        raise BitableReadError("snapshot", "psycopg_missing") from error
    return {
        "run_id": str(run_id),
        "completed_at": completed_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "member_count_from_run": member_count,
    }, rows


def _read_bitable(app_token: str, table_id: str | None, table_name: str | None, user_token: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    base_path = f"/bitable/v1/apps/{quote(app_token, safe='')}"
    tables = _page_items("tables", f"{base_path}/tables", user_token, "items")
    candidates = [table for table in tables if not table_id or table.get("table_id") == table_id]
    if table_name:
        candidates = [table for table in candidates if table.get("name") == table_name]
    if len(candidates) != 1:
        raise BitableReadError("tables", "table_not_unique", str(len(candidates)))
    table = candidates[0]
    selected_table_id = table.get("table_id")
    if not isinstance(selected_table_id, str) or not selected_table_id:
        raise BitableReadError("tables", "table_id_missing")
    table_path = f"{base_path}/tables/{quote(selected_table_id, safe='')}"
    fields = _page_items("fields", f"{table_path}/fields", user_token, "items")
    records = _page_items("records", f"{table_path}/records", user_token, "items")
    metadata = {
        "table_id": selected_table_id,
        "table_name": table.get("name"),
        "revision": table.get("revision"),
        "field_count": len(fields),
        "field_names": [field.get("field_name") for field in fields],
    }
    rows = [record.get("fields") for record in records if isinstance(record.get("fields"), dict)]
    return metadata, rows, records


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="只读关联飞书多维表格与测试库组织快照")
    parser.add_argument("--app-token", default=os.environ.get("FEISHU_BITABLE_APP_TOKEN"))
    parser.add_argument("--table-id", default=os.environ.get("FEISHU_BITABLE_TABLE_ID"))
    parser.add_argument("--table-name", default=os.environ.get("FEISHU_BITABLE_TABLE_NAME"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        app_token = (args.app_token or "").strip()
        if not app_token:
            raise BitableReadError("configuration", "missing_app_token")
        # 复用现有安全刷新入口：成功后立即轮换 refresh_token，access token 只留在内存。
        from sync_feishu_org_snapshot import refresh_saved_user_token

        user_token = refresh_saved_user_token()
        bitable, bitable_rows, raw_records = _read_bitable(app_token, args.table_id, args.table_name, user_token)
        snapshot, snapshot_rows = _read_latest_snapshot(_required_env("LINGXI_TEST_POSTGRES_DSN"))
        report = analyze_association(bitable_rows, snapshot_rows)
        report.update(
            {
                "status": "association_analysis_success",
                "read_only": True,
                "bitable": {**report["bitable"], **bitable, "record_rows": len(raw_records)},
                "snapshot": {**report["snapshot"], **snapshot},
                "recommended_join": "bitable.人员ID = feishu_org_member_snapshot.user_id; open_id 仅作待核对字段",
            }
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except BitableReadError as error:
        print(json.dumps({"status": "failed", "stage": error.stage, "error": error.code}, ensure_ascii=False))
        return 1
    except Exception as error:  # 终端只输出异常类别，避免外部错误携带资料或凭据。
        print(json.dumps({"status": "failed", "error": type(error).__name__}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
