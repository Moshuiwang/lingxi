#!/usr/bin/env python3
"""预发权限发布表的备份、台账、差异与撤回守卫。

本脚本运行在宿主机，只依赖 Python 标准库和可选的 ``psql``。它不导入
``lingxi``，因为首次升级前旧镜像也必须能够完成备份。日志只写步骤、计数、记录
标识和字段名；备份是唯一允许保存权限表正文（含 ``token_cipher``）的产物。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urlsplit
from urllib.request import Request, urlopen

SCHEMA_VERSION = 1
ENVIRONMENT = "stage"
DEFAULT_FEISHU_BASE_URL = "https://open.feishu.cn/open-apis"
DEFAULT_PAGE_SIZE = 500
MAX_PAGE_SIZE = 500
DEFAULT_MAX_PAGES = 50
REQUEST_TIMEOUT_SECONDS = 30
PSQL_TIMEOUT_SECONDS = 30
FIELD_NAMES: tuple[str, ...] = (
    "record_key",
    "email",
    "name",
    "permissions",
    "status",
    "updated_at",
    "token_cipher",
)
FEISHU_APP_KEYS: tuple[str, ...] = (
    "LINGXI_FEISHU_APP_ID",
    "LINGXI_FEISHU_APP_SECRET",
    "LINGXI_PERMISSION_BITABLE_APP_TOKEN",
    "LINGXI_PERMISSION_BITABLE_TABLE_ID",
)


class GuardError(RuntimeError):
    """可安全输出的失败分类；实例消息不携带配置值或表格正文。"""

    def __init__(self, code: str) -> None:
        """保存可安全输出的错误代码。"""
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, repr=False)
class GuardConfig:
    """守卫所需的配置；密钥只存在于当前进程内，不参与日志和文件名。"""

    app_id: str = ""
    app_secret: str = ""
    app_token: str = ""
    table_id: str = ""
    base_url: str = DEFAULT_FEISHU_BASE_URL
    dsn: str = ""

    def __repr__(self) -> str:
        """返回不含敏感值的表示。"""
        return "GuardConfig(<redacted>)"


@dataclass(frozen=True)
class HttpResponse:
    """传输层返回的状态码和解析后的正文。"""

    status: int
    payload: Any


@dataclass(frozen=True)
class TableRow:
    """权限表中一行经过文本归一后的七字段快照。"""

    record_id: str
    fields: dict[str, str]


@dataclass(frozen=True)
class TableSnapshot:
    """一次整表分页读取的结果。"""

    rows: tuple[TableRow, ...]
    pages: int


@dataclass(frozen=True)
class BackupSnapshot:
    """已校验的备份文件内容。"""

    taken_at: str
    rows: dict[str, dict[str, str]]
    file_sha256: str


@dataclass(frozen=True)
class LedgerEntry:
    """一条发布台账记录；没有外部记录标识时只能列为未解析。"""

    record_id: str | None
    record_key: str | None
    published_at: str
    outbox_id: str


@dataclass(frozen=True)
class RowState:
    """一个撤回目标行在计划时刻的存在状态与内容摘要；只有摘要，不含字段值。"""

    present: bool
    current_sha256: str | None
    backup_sha256: str | None


@dataclass(frozen=True)
class RevertPlan:
    """根据同一时刻的表、备份和台账计算出的最小撤回范围。"""

    restore_ids: tuple[str, ...]
    delete_ids: tuple[str, ...]
    skipped_ids: tuple[str, ...]
    unresolved_outbox_ids: tuple[str, ...]
    row_states: dict[str, RowState]
    summary_sha256: str


def _log(message: str, *, error: bool = False) -> None:
    """输出一行脱敏步骤信息。"""
    print(message, file=sys.stderr if error else sys.stdout, flush=True)


def _secure_regular_file(path: Path, label: str) -> None:
    """要求文件为当前用户所有且权限严格为 0600。"""
    try:
        if path.is_symlink() or not path.is_file():
            raise GuardError(f"{label}_not_file")
        file_stat = path.stat()
    except OSError as error:
        raise GuardError(f"{label}_unreadable") from error
    if stat.S_IMODE(file_stat.st_mode) != 0o600:
        raise GuardError(f"{label}_permission_unsafe")
    if file_stat.st_uid != os.getuid():
        raise GuardError(f"{label}_owner_mismatch")


def _ensure_work_dir(path: Path) -> None:
    """创建或核对 0700 工作目录，避免安全文件落入宽权限目录。"""
    try:
        if not path.exists():
            path.mkdir(parents=True, mode=0o700)
        if path.is_symlink() or not path.is_dir():
            raise GuardError("work_dir_not_directory")
        directory_stat = path.stat()
    except OSError as error:
        raise GuardError("work_dir_unavailable") from error
    if stat.S_IMODE(directory_stat.st_mode) != 0o700:
        raise GuardError("work_dir_permission_unsafe")
    if directory_stat.st_uid != os.getuid():
        raise GuardError("work_dir_owner_mismatch")


def _parse_env_file(path: Path) -> dict[str, str]:
    """解析 ``KEY=VALUE``，只剥离一层首尾引号，不展开变量。"""
    _secure_regular_file(path, "env_file")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise GuardError("env_file_unreadable") from error
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise GuardError("env_file_malformed")
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key or any(character.isspace() for character in key):
            raise GuardError("env_file_malformed")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        values[key] = value
    return values


def _required(values: Mapping[str, str], keys: Sequence[str]) -> None:
    """只报告缺失变量名，不回显任何变量值。"""
    if any(not values.get(key, "").strip() for key in keys):
        raise GuardError("env_file_missing_required")


def _base_url(value: str) -> str:
    """校验飞书地址为不带凭据、查询串和片段的 HTTPS 地址。"""
    text = value.strip() or DEFAULT_FEISHU_BASE_URL
    parsed = urlsplit(text)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise GuardError("feishu_base_url_invalid")
    return text.rstrip("/")


def load_config(
    path: Path, *, need_database: bool = False, need_feishu: bool = False
) -> GuardConfig:
    """读取并校验本次命令需要的配置。"""
    values = _parse_env_file(path)
    if need_database:
        _required(values, ("LINGXI_POSTGRES_DSN",))
    if need_feishu:
        _required(values, FEISHU_APP_KEYS)
    return GuardConfig(
        app_id=values.get("LINGXI_FEISHU_APP_ID", ""),
        app_secret=values.get("LINGXI_FEISHU_APP_SECRET", ""),
        app_token=values.get("LINGXI_PERMISSION_BITABLE_APP_TOKEN", ""),
        table_id=values.get("LINGXI_PERMISSION_BITABLE_TABLE_ID", ""),
        base_url=_base_url(values.get("LINGXI_FEISHU_BASE_URL", DEFAULT_FEISHU_BASE_URL)),
        dsn=values.get("LINGXI_POSTGRES_DSN", ""),
    )


def _text_value(value: Any) -> str:
    """把多维表格文本列的常见返回形态归一成可写回的文字。"""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "".join(_text_value(item) for item in value)
    if isinstance(value, Mapping):
        for key in ("text", "value", "name"):
            if key in value:
                return _text_value(value[key])
    return ""


def _record_id(value: Any) -> str:
    """校验记录标识，不把标识值写入异常消息。"""
    if not isinstance(value, str) or not value.strip() or any(ch.isspace() for ch in value):
        raise GuardError("record_id_invalid")
    return value


def _fields(value: Any) -> dict[str, str]:
    """抽取并校验正式表的七个单行文本字段。"""
    if not isinstance(value, Mapping) or any(name not in value for name in FIELD_NAMES):
        raise GuardError("record_fields_incomplete")
    result = {name: _text_value(value[name]) for name in FIELD_NAMES}
    if any("\n" in item or "\r" in item for item in result.values()):
        raise GuardError("record_field_multiline")
    return result


def _json_body(response: HttpResponse, *, allow_empty: bool = False) -> Mapping[str, Any]:
    """检查 HTTP 与飞书业务码，失败只返回分类码。"""
    if response.status < 200 or response.status >= 300:
        raise GuardError("feishu_http_error")
    if not isinstance(response.payload, Mapping):
        if allow_empty and response.payload in (None, ""):
            return {}
        raise GuardError("feishu_response_invalid")
    code = response.payload.get("code")
    if code not in (None, 0, "0"):
        raise GuardError("feishu_api_error")
    data = response.payload.get("data")
    if isinstance(data, Mapping):
        return data
    if allow_empty and data is None:
        return {}
    raise GuardError("feishu_response_invalid")


def _urllib_transport(
    method: str, url: str, *, body: Mapping[str, Any] | None, headers: Mapping[str, str]
) -> HttpResponse:
    """使用标准库发起一次 HTTPS 请求；不重试任何外部写入。"""
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request_headers = {"Accept": "application/json", **headers}
    if body is not None:
        request_headers["Content-Type"] = "application/json; charset=utf-8"
    request = Request(url, data=payload, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read()
            return HttpResponse(response.status, _decode_json(raw))
    except HTTPError as error:
        try:
            payload_value = _decode_json(error.read())
        except GuardError:
            payload_value = {}
        return HttpResponse(error.code, payload_value)
    except (URLError, OSError, TimeoutError) as error:
        raise GuardError("feishu_transport_error") from error


def _decode_json(raw: bytes) -> Any:
    """解析响应正文，不把正文带入错误。"""
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise GuardError("feishu_response_invalid") from error


def _response(value: Any) -> HttpResponse:
    """兼容标准传输、测试传输以及 ``(status, payload)`` 简化返回形状。"""
    if isinstance(value, HttpResponse):
        return value
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], int):
        return HttpResponse(value[0], value[1])
    if isinstance(value, Mapping) and isinstance(value.get("__status__"), int):
        payload = {key: item for key, item in value.items() if key != "__status__"}
        return HttpResponse(value["__status__"], payload)
    return HttpResponse(200, value)


class PermissionTableClient:
    """宿主侧飞书记录接口客户端，令牌只放请求头。"""

    def __init__(
        self,
        config: GuardConfig,
        *,
        transport: Callable[..., Any] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        """初始化分页读取与记录操作所需的客户端配置。"""
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= MAX_PAGE_SIZE
        ):
            raise GuardError("page_size_invalid")
        if not isinstance(max_pages, int) or max_pages < 1:
            raise GuardError("max_pages_invalid")
        self._config = config
        self._transport = transport or _urllib_transport
        self._page_size = page_size
        self._max_pages = max_pages
        self._access_token: str | None = None

    @property
    def _records_path(self) -> str:
        app_token = quote(self._config.app_token, safe="")
        table_id = quote(self._config.table_id, safe="")
        return f"/bitable/v1/apps/{app_token}/tables/{table_id}/records"

    def _request(
        self, method: str, url: str, *, body: Mapping[str, Any] | None = None
    ) -> HttpResponse:
        token = self._access_token
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            value = self._transport(method, url, body=body, headers=headers)
        except GuardError:
            raise
        except Exception as error:
            raise GuardError("feishu_transport_error") from error
        return _response(value)

    def _fetch_token(self) -> str:
        """换取租户令牌；令牌不写日志、不写文件、不进 URL。"""
        url = f"{self._config.base_url}/auth/v3/tenant_access_token/internal"
        response = self._request(
            "POST",
            url,
            body={"app_id": self._config.app_id, "app_secret": self._config.app_secret},
        )
        if (
            response.status < 200
            or response.status >= 300
            or not isinstance(response.payload, Mapping)
        ):
            raise GuardError("feishu_token_error")
        if response.payload.get("code") not in (None, 0, "0"):
            raise GuardError("feishu_token_error")
        token = response.payload.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise GuardError("feishu_token_error")
        self._access_token = token
        return token

    def _ensure_token(self) -> None:
        if self._access_token is None:
            self._fetch_token()

    def list_rows(self) -> TableSnapshot:
        """按既有适配器的 500/50 纪律整表分页；撞上上界即失败。"""
        self._ensure_token()
        rows: list[TableRow] = []
        page_token: str | None = None
        for page_number in range(1, self._max_pages + 1):
            parameters: dict[str, Any] = {"page_size": self._page_size}
            if page_token:
                parameters["page_token"] = page_token
            url = f"{self._config.base_url}{self._records_path}?{urlencode(parameters)}"
            data = _json_body(self._request("GET", url))
            items_value = data.get("items")
            items_absent = items_value is None
            items = [] if items_value is None else items_value
            if not isinstance(items, list):
                raise GuardError("feishu_page_invalid")
            for item in items:
                if not isinstance(item, Mapping):
                    raise GuardError("feishu_page_invalid")
                rows.append(
                    TableRow(_record_id(item.get("record_id")), _fields(item.get("fields")))
                )
            has_more = data.get("has_more")
            if not isinstance(has_more, bool):
                if page_token is None and items_absent and has_more is None:
                    return TableSnapshot(tuple(rows), page_number)
                raise GuardError("feishu_pagination_invalid")
            if not has_more:
                return TableSnapshot(tuple(rows), page_number)
            candidate = data.get("page_token")
            if not isinstance(candidate, str) or not candidate or candidate == page_token:
                raise GuardError("feishu_pagination_stalled")
            page_token = candidate
        raise GuardError("feishu_pagination_limit")

    def read_row(self, record_id: str) -> dict[str, str] | None:
        """读一行；只有 HTTP 404 才表示已经不存在。"""
        self._ensure_token()
        url = f"{self._config.base_url}{self._records_path}/{quote(_record_id(record_id), safe='')}"
        response = self._request("GET", url)
        if response.status == 404:
            return None
        data = _json_body(response)
        record = data.get("record")
        if not isinstance(record, Mapping):
            raise GuardError("feishu_record_invalid")
        returned_id = _record_id(record.get("record_id"))
        if returned_id != record_id:
            raise GuardError("feishu_record_id_mismatch")
        return _fields(record.get("fields"))

    def update_row(self, record_id: str, fields: Mapping[str, str]) -> None:
        """向指定记录部分更新七个字段。"""
        self._ensure_token()
        url = f"{self._config.base_url}{self._records_path}/{quote(_record_id(record_id), safe='')}"
        _json_body(self._request("PUT", url, body={"fields": dict(fields)}), allow_empty=True)

    def delete_row(self, record_id: str) -> None:
        """删除指定记录；删除接口的真实调用留给 S-2-6 首次演示验证。"""
        self._ensure_token()
        url = f"{self._config.base_url}{self._records_path}/{quote(_record_id(record_id), safe='')}"
        _json_body(self._request("DELETE", url, body=None), allow_empty=True)


def _timestamp() -> str:
    """返回 UTC 文件名时刻。"""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _iso_now() -> str:
    """返回秒级 UTC 时间戳。"""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    """只接受带时区的 ISO 时间，统一转 UTC。"""
    if not isinstance(value, str) or not value.strip():
        raise GuardError("timestamp_invalid")
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise GuardError("timestamp_invalid") from error
    if parsed.tzinfo is None:
        raise GuardError("timestamp_timezone_missing")
    return parsed.astimezone(UTC)


def _secure_json_write(work_dir: Path, prefix: str, payload: Any) -> Path:
    """以 0600 原子写入新 JSON 文件，不覆盖已有产物。"""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    destination = work_dir / f"{prefix}-{_timestamp()}.json"
    temporary: Path | None = None
    try:
        for _ in range(4):
            candidate = work_dir / f".{prefix}-{uuid.uuid4().hex}.tmp"
            try:
                descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            temporary = candidate
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
            temporary = None
            os.chmod(destination, 0o600)
            _secure_regular_file(destination, "output_file")
            return destination
    except (OSError, UnicodeError) as error:
        raise GuardError("output_write_failed") from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
    raise GuardError("output_name_collision")


def _read_json(path: Path, label: str) -> Any:
    """读取严格权限保护的 JSON。"""
    _secure_regular_file(path, label)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise GuardError(f"{label}_invalid") from error


def _file_sha256(path: Path) -> str:
    """计算文件摘要。"""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise GuardError("file_read_failed") from error
    return digest.hexdigest()


def _row_map(rows: Sequence[TableRow]) -> dict[str, dict[str, str]]:
    """建立记录标识到字段的映射，并拒绝重复标识。"""
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        if row.record_id in result:
            raise GuardError("duplicate_record_id")
        result[row.record_id] = dict(row.fields)
    return result


def _validate_backup(path: Path, config: GuardConfig) -> BackupSnapshot:
    """校验备份结构、坐标摘要、权限和行数。"""
    payload = _read_json(path, "backup_file")
    if not isinstance(payload, Mapping) or payload.get("schema") != SCHEMA_VERSION:
        raise GuardError("backup_schema_invalid")
    if payload.get("environment") != ENVIRONMENT:
        raise GuardError("backup_environment_invalid")
    table = payload.get("table")
    if not isinstance(table, Mapping):
        raise GuardError("backup_table_invalid")
    expected_digest = hashlib.sha256(config.app_token.encode("utf-8")).hexdigest()
    if table.get("app_token_sha256") != expected_digest or table.get("table_id") != config.table_id:
        raise GuardError("backup_table_mismatch")
    taken_at = payload.get("taken_at")
    _parse_timestamp(taken_at if isinstance(taken_at, str) else "")
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list) or payload.get("row_count") != len(raw_rows):
        raise GuardError("backup_row_count_invalid")
    rows: dict[str, dict[str, str]] = {}
    for raw_row in raw_rows:
        if not isinstance(raw_row, Mapping):
            raise GuardError("backup_row_invalid")
        identifier = _record_id(raw_row.get("record_id"))
        if identifier in rows:
            raise GuardError("backup_duplicate_record_id")
        rows[identifier] = _fields(raw_row.get("fields"))
    return BackupSnapshot(str(taken_at), rows, _file_sha256(path))


def _validate_ledger(path: Path) -> tuple[LedgerEntry, ...]:
    """校验台账数组；无外部记录标识的项保持未解析状态。"""
    payload = _read_json(path, "ledger_file")
    if not isinstance(payload, list):
        raise GuardError("ledger_schema_invalid")
    entries: list[LedgerEntry] = []
    for item in payload:
        if not isinstance(item, Mapping):
            raise GuardError("ledger_row_invalid")
        raw_id = item.get("record_id")
        record_id = None if raw_id in (None, "") else _record_id(raw_id)
        record_key = item.get("record_key")
        if record_key is not None and not isinstance(record_key, str):
            raise GuardError("ledger_record_key_invalid")
        published_at = item.get("published_at")
        outbox_id = item.get("outbox_id")
        if not isinstance(published_at, str) or not published_at:
            raise GuardError("ledger_published_at_invalid")
        if not isinstance(outbox_id, str) or not outbox_id:
            raise GuardError("ledger_outbox_id_invalid")
        entries.append(LedgerEntry(record_id, record_key, published_at, outbox_id))
    return tuple(entries)


def run_backup(
    config: GuardConfig, work_dir: Path, *, client: PermissionTableClient | None = None
) -> int:
    """备份整张权限表并读回校验行数与文件摘要。"""
    active_client = client or PermissionTableClient(config)
    snapshot = active_client.list_rows()
    _log(f"backup=fetch pages={snapshot.pages} rows={len(snapshot.rows)}")
    rows = [{"record_id": row.record_id, "fields": dict(row.fields)} for row in snapshot.rows]
    payload = {
        "schema": SCHEMA_VERSION,
        "taken_at": _iso_now(),
        "environment": ENVIRONMENT,
        "table": {
            "app_token_sha256": hashlib.sha256(config.app_token.encode("utf-8")).hexdigest(),
            "table_id": config.table_id,
        },
        "row_count": len(rows),
        "rows": rows,
    }
    path = _secure_json_write(work_dir, "backup", payload)
    checked = _validate_backup(path, config)
    if len(checked.rows) != len(rows):
        raise GuardError("backup_self_check_failed")
    digest = _file_sha256(path)
    _log(f"backup={path} rows={len(checked.rows)} sha256={digest}")
    return 0


LEDGER_SQL = r"""
SELECT COALESCE(
    json_agg(
        json_build_object(
            'record_id', o.external_record_id,
            'record_key', COALESCE(NULLIF(o.payload ->> 'record_key', ''), u.email),
            'published_at', to_char(o.published_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
            'outbox_id', o.id
        ) ORDER BY o.published_at, o.id
    ), '[]'::json
)::text
  FROM publish_outbox o
  LEFT JOIN app_user u ON u.id = o.user_id
 WHERE o.status = 'published'
   AND o.published_at >= :'since'::timestamptz;
"""


URL_DSN_SCHEME = re.compile(r"^postgres(?:ql)?(?:\+[A-Za-z0-9_]+)?$")
PSQL_URL_SCHEME = "postgresql"


def _safe_url_dsn(text: str) -> tuple[str, str | None]:
    """拆出 URL 形 DSN 里 userinfo 与 ``?password=`` 两处口令，并归一方案名。

    接受 ``postgresql://``、``postgres://`` 与 ``postgresql+<驱动>://``（例如
    ``postgresql+psycopg://``），交给 psql 前统一写成 ``postgresql://``；其他方案名
    一律拒绝。查询串里除 ``password`` 之外的参数原样保留，不重新编码。两处都写了
    口令时取查询参数那一个，与 libpq 的解析顺序一致。
    """
    parsed = urlsplit(text)
    if not URL_DSN_SCHEME.match(parsed.scheme):
        raise GuardError("database_dsn_invalid")
    password: str | None = None
    if parsed.password is not None:
        password = unquote(parsed.password)
    userinfo, separator, host_part = parsed.netloc.rpartition("@")
    safe_netloc = f"{userinfo.split(':', 1)[0]}@{host_part}" if separator else host_part
    kept: list[str] = []
    for pair in parsed.query.split("&"):
        if not pair:
            continue
        key, _, value = pair.partition("=")
        if unquote(key).lower() == "password":
            password = unquote(value)
            continue
        kept.append(pair)
    # 不用 urlunsplit：netloc 为空（Unix socket 写法 ``postgresql:///db?host=…``）时它会
    # 丢掉 ``//``，libpq 就认不出这是 URL。
    safe_url = f"{PSQL_URL_SCHEME}://{safe_netloc}{parsed.path}"
    if kept:
        safe_url = f"{safe_url}?{'&'.join(kept)}"
    if parsed.fragment:
        safe_url = f"{safe_url}#{parsed.fragment}"
    return safe_url, password


def _safe_keyword_dsn(text: str) -> tuple[str, str | None]:
    """去掉键值形 DSN 里的 ``password=`` 项；写法认不出时拒绝而不是放行。"""
    try:
        tokens = shlex.split(text)
    except ValueError as error:
        raise GuardError("database_dsn_invalid") from error
    safe_tokens: list[str] = []
    password_value: str | None = None
    for token in tokens:
        if token.lower().startswith("password="):
            password_value = token.partition("=")[2]
            continue
        safe_tokens.append(token)
    if "password" in text.lower() and password_value is None:
        raise GuardError("database_dsn_invalid")
    return " ".join(shlex.quote(token) for token in safe_tokens), password_value


def _safe_psql_dsn(dsn: str) -> tuple[str, str | None]:
    """把口令移到 ``PGPASSWORD``，避免出现在 psql 命令行参数（同机其他用户可见）。"""
    if not isinstance(dsn, str) or not dsn.strip():
        raise GuardError("database_dsn_missing")
    text = dsn.strip()
    if "://" in text:
        return _safe_url_dsn(text)
    return _safe_keyword_dsn(text)


def _run_psql(dsn: str, since: datetime, *, psql_bin: str = "psql") -> Any:
    """执行只读台账查询；stderr 只影响退出分类，不向日志转发。"""
    safe_dsn, password = _safe_psql_dsn(dsn)
    environment = os.environ.copy()
    environment.pop("PGPASSWORD", None)
    if password is not None:
        environment["PGPASSWORD"] = password
    arguments = [
        psql_bin,
        "--no-psqlrc",
        "--quiet",
        "--tuples-only",
        "--no-align",
        "-v",
        f"since={since.isoformat()}",
        "-d",
        safe_dsn,
        "-f",
        "-",
    ]
    try:
        result = subprocess.run(
            arguments,
            input=LEDGER_SQL,
            capture_output=True,
            text=True,
            timeout=PSQL_TIMEOUT_SECONDS,
            check=False,
            env=environment,
        )
    except FileNotFoundError:
        raise GuardError("psql_not_found") from None
    except subprocess.TimeoutExpired:
        # 子进程异常自带完整命令行；不链到守卫异常上，避免 DSN 随回溯回显。
        raise GuardError("psql_timeout") from None
    except OSError:
        raise GuardError("psql_failed") from None
    if result.returncode != 0:
        raise GuardError("psql_failed")
    try:
        return json.loads(result.stdout.strip() or "[]")
    except json.JSONDecodeError as error:
        raise GuardError("psql_output_invalid") from error


def run_ledger(config: GuardConfig, work_dir: Path, since: str, *, psql_bin: str = "psql") -> int:
    """从预发库导出指定时间之后已发布的外部记录台账。"""
    parsed_since = _parse_timestamp(since)
    raw_rows = _run_psql(config.dsn, parsed_since, psql_bin=psql_bin)
    if not isinstance(raw_rows, list):
        raise GuardError("ledger_query_shape_invalid")
    output: list[dict[str, Any]] = []
    unresolved = 0
    for item in raw_rows:
        if not isinstance(item, Mapping):
            raise GuardError("ledger_query_shape_invalid")
        raw_id = item.get("record_id")
        record_id = None if raw_id in (None, "") else _record_id(raw_id)
        record_key = item.get("record_key")
        if record_key is not None and not isinstance(record_key, str):
            raise GuardError("ledger_query_shape_invalid")
        published_at = item.get("published_at")
        outbox_id = item.get("outbox_id")
        if not isinstance(published_at, str) or not isinstance(outbox_id, str):
            raise GuardError("ledger_query_shape_invalid")
        entry: dict[str, Any] = {
            "record_id": record_id,
            "record_key": record_key,
            "published_at": published_at,
            "outbox_id": outbox_id,
        }
        if record_id is None:
            entry["unresolved"] = True
            unresolved += 1
        output.append(entry)
    path = _secure_json_write(work_dir, "ledger", output)
    checked = _validate_ledger(path)
    if len(checked) != len(output):
        raise GuardError("ledger_self_check_failed")
    _log(f"ledger={path} rows={len(checked)} unresolved={unresolved} sha256={_file_sha256(path)}")
    return 0


def _load_inputs(
    backup_path: Path, ledger_path: Path, config: GuardConfig
) -> tuple[BackupSnapshot, tuple[LedgerEntry, ...], str]:
    """读取撤回所需的两个受保护输入并核对表格坐标。"""
    backup = _validate_backup(backup_path, config)
    ledger = _validate_ledger(ledger_path)
    return backup, ledger, _file_sha256(ledger_path)


def _changed_ids(
    backup: Mapping[str, Mapping[str, str]], current: Mapping[str, Mapping[str, str]]
) -> set[str]:
    """计算三类差异的记录标识并合并。"""
    common = set(backup) & set(current)
    updates = {
        identifier for identifier in common if dict(backup[identifier]) != dict(current[identifier])
    }
    return updates | (set(current) - set(backup)) | (set(backup) - set(current))


def _ledger_ids(entries: Sequence[LedgerEntry]) -> set[str]:
    """去掉未解析项和重复项，形成唯一写入范围。"""
    return {entry.record_id for entry in entries if entry.record_id is not None}


def _fields_sha256(fields: Mapping[str, str]) -> str:
    """七字段内容的摘要；计划文件与确认摘要只携带它，不携带字段值。"""
    encoded = json.dumps(
        {name: fields.get(name, "") for name in FIELD_NAMES},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _row_states(
    backup_rows: Mapping[str, Mapping[str, str]],
    current: Mapping[str, Mapping[str, str]],
    identifiers: Sequence[str],
) -> dict[str, RowState]:
    """记录每个撤回目标行的存在状态、当前内容摘要与备份值摘要。"""
    states: dict[str, RowState] = {}
    for identifier in identifiers:
        current_fields = current.get(identifier)
        backup_fields = backup_rows.get(identifier)
        states[identifier] = RowState(
            present=current_fields is not None,
            current_sha256=None if current_fields is None else _fields_sha256(current_fields),
            backup_sha256=None if backup_fields is None else _fields_sha256(backup_fields),
        )
    return states


def _row_states_payload(states: Mapping[str, RowState]) -> dict[str, dict[str, Any]]:
    """把目标行状态压成可写入计划文件与摘要输入的纯字典。"""
    return {
        identifier: {
            "present": state.present,
            "current_sha256": state.current_sha256,
            "backup_sha256": state.backup_sha256,
        }
        for identifier, state in sorted(states.items())
    }


def _plan(
    backup: BackupSnapshot,
    ledger: Sequence[LedgerEntry],
    current: Mapping[str, Mapping[str, str]],
    ledger_sha256: str,
) -> RevertPlan:
    """只按台账外部记录标识生成可确认摘要。

    摘要绑定备份与台账两个文件的摘要、撤回记录清单、未解析清单，以及每个撤回目标行
    此刻的存在状态、当前七字段内容摘要和备份中对应值的摘要。目标行在确认前被改或被
    删，摘要就会变化；台账外的行不进入摘要——撤回从不写它们，它们只在计划里列为跳过。
    """
    scoped = _ledger_ids(ledger)
    restore_ids = tuple(sorted(scoped & set(backup.rows)))
    delete_ids = tuple(sorted(scoped - set(backup.rows)))
    skipped_ids = tuple(sorted(_changed_ids(backup.rows, current) - scoped))
    unresolved = tuple(sorted(entry.outbox_id for entry in ledger if entry.record_id is None))
    row_states = _row_states(backup.rows, current, (*restore_ids, *delete_ids))
    summary = {
        "schema": SCHEMA_VERSION,
        "backup_sha256": backup.file_sha256,
        "ledger_sha256": ledger_sha256,
        "restore": restore_ids,
        "delete": delete_ids,
        "unresolved": unresolved,
        "rows": _row_states_payload(row_states),
    }
    encoded = json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return RevertPlan(restore_ids, delete_ids, skipped_ids, unresolved, row_states, digest)


def _plan_payload(plan: RevertPlan) -> dict[str, Any]:
    """生成不含表格正文的计划文件；``rows`` 只有摘要，供两次干跑之间定位变化行。"""
    return {
        "schema": SCHEMA_VERSION,
        "restore": list(plan.restore_ids),
        "delete": list(plan.delete_ids),
        "skip": list(plan.skipped_ids),
        "unresolved": list(plan.unresolved_outbox_ids),
        "rows": _row_states_payload(plan.row_states),
        "summary_sha256": plan.summary_sha256,
    }


def _ids_text(values: Sequence[str]) -> str:
    """把记录标识压成一行；空集用短横线表示。"""
    return ",".join(values) if values else "-"


def _write_and_log_plan(work_dir: Path, plan: RevertPlan) -> Path:
    """写入 0600 计划并输出撤回范围。"""
    path = _secure_json_write(work_dir, "revert-plan", _plan_payload(plan))
    _log(
        "revert=plan "
        f"restore={len(plan.restore_ids)} ids={_ids_text(plan.restore_ids)} "
        f"delete={len(plan.delete_ids)} ids={_ids_text(plan.delete_ids)} "
        f"skip={len(plan.skipped_ids)} ids={_ids_text(plan.skipped_ids)} "
        f"unresolved={len(plan.unresolved_outbox_ids)} "
        f"plan={path} summary_sha256={plan.summary_sha256}"
    )
    return path


def _same_fields(expected: Mapping[str, str], actual: Mapping[str, str] | None) -> tuple[str, ...]:
    """返回七字段中不一致的字段名，不返回字段值。"""
    if actual is None:
        return FIELD_NAMES
    return tuple(name for name in FIELD_NAMES if actual.get(name) != expected.get(name))


def _report_revert_failure(
    completed: Sequence[str], current: str, remaining: Sequence[str], reason: str
) -> None:
    """报告可重跑的部分完成状态，不暴露正文。"""
    _log(
        f"revert=stopped reason={reason} completed={_ids_text(completed)} "
        f"uncompleted={_ids_text((current, *remaining))}",
        error=True,
    )


def _execute_revert(
    client: PermissionTableClient,
    backup: BackupSnapshot,
    plan: RevertPlan,
    current: Mapping[str, Mapping[str, str]],
) -> int:
    """按计划顺序逐行写入并读回；第一处不符即停止。"""
    completed: list[str] = []
    restore_remaining = list(plan.restore_ids)
    delete_remaining = list(plan.delete_ids)
    for index, identifier in enumerate(plan.restore_ids):
        expected = backup.rows[identifier]
        if identifier in current and not _same_fields(expected, current[identifier]):
            completed.append(identifier)
            _log(f"revert=restore record={identifier} result=already_equal")
            continue
        try:
            client.update_row(identifier, expected)
            actual = client.read_row(identifier)
        except GuardError as error:
            _report_revert_failure(
                completed,
                identifier,
                (*restore_remaining[index + 1 :], *delete_remaining),
                error.code,
            )
            return 1
        mismatch = _same_fields(expected, actual)
        if mismatch:
            _report_revert_failure(
                completed,
                identifier,
                (*restore_remaining[index + 1 :], *delete_remaining),
                "readback_mismatch:" + ",".join(mismatch),
            )
            return 1
        completed.append(identifier)
        _log(f"revert=restore record={identifier} result=written")
    for index, identifier in enumerate(plan.delete_ids):
        if identifier not in current:
            completed.append(identifier)
            _log(f"revert=delete record={identifier} result=already_absent")
            continue
        try:
            client.delete_row(identifier)
            actual = client.read_row(identifier)
        except GuardError as error:
            _report_revert_failure(
                completed, identifier, tuple(delete_remaining[index + 1 :]), error.code
            )
            return 1
        if actual is not None:
            _report_revert_failure(
                completed,
                identifier,
                tuple(delete_remaining[index + 1 :]),
                "delete_readback_present",
            )
            return 1
        completed.append(identifier)
        _log(f"revert=delete record={identifier} result=deleted")
    _log(f"revert=complete rows={len(completed)}")
    return 0


def run_diff(
    config: GuardConfig,
    backup_path: Path,
    ledger_path: Path | None = None,
    *,
    client: PermissionTableClient | None = None,
) -> int:
    """只读列出备份与现表的更新、新建、删除及台账标记。"""
    backup = _validate_backup(backup_path, config)
    ledger_ids = set()
    if ledger_path is not None:
        ledger_ids = _ledger_ids(_validate_ledger(ledger_path))
    active_client = client or PermissionTableClient(config)
    snapshot = active_client.list_rows()
    current = _row_map(snapshot.rows)
    common = set(backup.rows) & set(current)
    updates = sorted(
        identifier for identifier in common if backup.rows[identifier] != current[identifier]
    )
    created = sorted(set(current) - set(backup.rows))
    deleted = sorted(set(backup.rows) - set(current))
    _log(f"diff=fetch pages={snapshot.pages} rows={len(current)}")
    for label, identifiers in (("updates", updates), ("new", created), ("deleted", deleted)):
        marked = (
            ",".join(
                f"{identifier}[{'ledger' if identifier in ledger_ids else 'outside'}]"
                for identifier in identifiers
            )
            or "-"
        )
        _log(f"diff={label} count={len(identifiers)} ids={marked}")
    return 0


def run_revert(
    config: GuardConfig,
    work_dir: Path,
    backup_path: Path,
    ledger_path: Path,
    confirm: str | None = None,
    *,
    client: PermissionTableClient | None = None,
) -> int:
    """默认只写计划；摘要确认正确后才执行台账范围内的撤回。

    确认摘要相符后、发出第一条写入前，再取一次现表逐行比对撤回目标行的存在状态与
    七字段内容；任一行与确认时刻不同即停止、零写入、退出 1，只打印变化行的记录标识。
    """
    backup, ledger, ledger_sha256 = _load_inputs(backup_path, ledger_path, config)
    active_client = client or PermissionTableClient(config)
    snapshot = active_client.list_rows()
    current = _row_map(snapshot.rows)
    plan = _plan(backup, ledger, current, ledger_sha256)
    _log(f"revert=fetch pages={snapshot.pages} rows={len(current)}")
    _write_and_log_plan(work_dir, plan)
    if confirm is None:
        _log("revert=dry_run writes=0")
        return 0
    if confirm != plan.summary_sha256:
        _log("revert=refused reason=confirm_mismatch writes=0", error=True)
        return 1
    _log("revert=confirmed")
    refreshed_snapshot = active_client.list_rows()
    refreshed = _row_map(refreshed_snapshot.rows)
    changed = tuple(
        identifier
        for identifier in (*plan.restore_ids, *plan.delete_ids)
        if current.get(identifier) != refreshed.get(identifier)
    )
    _log(
        f"revert=recheck pages={refreshed_snapshot.pages} rows={len(refreshed)} "
        f"changed={len(changed)}"
    )
    if changed:
        _log(
            "revert=stopped reason=rows_changed_before_execute "
            f"changed={_ids_text(changed)} writes=0",
            error=True,
        )
        return 1
    return _execute_revert(active_client, backup, plan, refreshed)


def run_verify(
    config: GuardConfig,
    backup_path: Path,
    ledger_path: Path,
    *,
    client: PermissionTableClient | None = None,
) -> int:
    """只读核对台账记录已还原到备份值或已被删除。"""
    backup, ledger, _ = _load_inputs(backup_path, ledger_path, config)
    active_client = client or PermissionTableClient(config)
    snapshot = active_client.list_rows()
    current = _row_map(snapshot.rows)
    failures: list[str] = []
    checked = 0
    for identifier in sorted(_ledger_ids(ledger)):
        checked += 1
        if identifier not in backup.rows:
            if identifier in current:
                failures.append(f"{identifier}:not_deleted")
            continue
        mismatch = _same_fields(backup.rows[identifier], current.get(identifier))
        if mismatch:
            failures.append(f"{identifier}:{','.join(mismatch)}")
    _log(f"verify=fetch pages={snapshot.pages} rows={len(current)}")
    if failures:
        _log(f"verify=failed checked={checked} failures={','.join(failures)}", error=True)
        return 1
    _log(
        f"verify=ok checked={checked} unresolved={sum(entry.record_id is None for entry in ledger)}"
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(description="预发权限发布表备份与撤回守卫")
    parser.add_argument(
        "--env-file", required=True, help="scheduler 环境文件，必须为 0600 且属主为当前用户"
    )
    parser.add_argument("--work-dir", required=True, help="0700 工作目录")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("backup", help="备份整张权限发布表")
    ledger = commands.add_parser("ledger", help="导出发布台账")
    ledger.add_argument("--since", required=True, help="备份 taken_at 的带时区 ISO 时间")
    diff = commands.add_parser("diff", help="只读比较现表与备份")
    diff.add_argument("--backup", required=True)
    diff.add_argument("--ledger")
    revert = commands.add_parser("revert", help="默认干跑，确认摘要后撤回")
    revert.add_argument("--backup", required=True)
    revert.add_argument("--ledger", required=True)
    revert.add_argument("--confirm")
    verify = commands.add_parser("verify", help="只读核对撤回结果")
    verify.add_argument("--backup", required=True)
    verify.add_argument("--ledger", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """执行一个子命令，统一把未预期外部失败收口为非零退出。"""
    args = _parser().parse_args(argv)
    work_dir = Path(args.work_dir)
    try:
        _ensure_work_dir(work_dir)
        if args.command == "backup":
            config = load_config(Path(args.env_file), need_feishu=True)
            return run_backup(config, work_dir)
        if args.command == "ledger":
            config = load_config(Path(args.env_file), need_database=True)
            return run_ledger(config, work_dir, args.since)
        config = load_config(Path(args.env_file), need_feishu=True)
        if args.command == "diff":
            return run_diff(config, Path(args.backup), Path(args.ledger) if args.ledger else None)
        if args.command == "revert":
            return run_revert(
                config,
                work_dir,
                Path(args.backup),
                Path(args.ledger),
                args.confirm,
            )
        if args.command == "verify":
            return run_verify(config, Path(args.backup), Path(args.ledger))
    except GuardError as error:
        _log(f"error={error.code}", error=True)
        return 2
    except (OSError, ValueError, TypeError) as error:
        _log(f"error=unexpected_{type(error).__name__}", error=True)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
