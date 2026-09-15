#!/usr/bin/env python3
"""预发单用户开通记录复位：备份 → 干跑 → 确认删除 → 回退（预发专用，生产禁用）。

## 用途与边界

让一个**预发**测试账号回到「从未开通」，以便重走「扩员 → 欢迎卡 → 首聊开通 → 问数」。
只动两处：预发库里该用户的一行 ``app_user`` 及其派生行，以及**预发权限发布表**
（``LINGXI_PERMISSION_BITABLE_*``）里该用户的一行。**不碰正式表**
（``LINGXI_STOCK_TOKEN_BITABLE_*``）、不吊销、不解密任何令牌——令牌库那一行随用户删除，
存量令牌本身仍在正式表里，重走首聊时按存量沿用路径重新采纳。

## 必须在 ``lingxi-scheduler`` 容器内运行，且脚本本体经 stdin 喂进去

与 ``scripts/ops/qa_corpus.py`` 同一形态：``.dockerignore`` 排除了 ``scripts/``，镜像里没有
这个文件；连接串与飞书凭据只有 scheduler 容器持有。子命令写在 ``-`` 之后：

    docker compose --env-file deploy/.env.stage \\
      -f deploy/compose.yaml -f deploy/compose.stage.yaml \\
      exec -T scheduler python -B - plan --email <邮箱> --open-id <open_id> \\
      < scripts/ops/stage_user_reset.py

``restore`` 要从标准输入读备份 JSON，脚本本体改由 ``-c "$(cat …)"`` 传入（见运维文档）。

## 四个子命令（都要求 ``--email`` 与 ``--open-id`` 双重定位，不一致即拒；单次只处理一个用户）

- ``plan``：只读。按表列出将删行数与每行稳定摘要（主键 + 非敏感列的 sha256 前 16 位；
  不含令牌密文、飞书 secret、正文），查预发表里该用户的行（0 或 1 行，多行即拒），输出
  绑定全部将删行与预发表记录标识的 ``summary_sha256``。
- ``backup``：只读。把将删行的**完整字段**（含密文列，回退要用）以 JSON 写到标准输出，
  由宿主重定向到 0600 文件；退出 0。进度信息只走标准错误，标准输出是纯 JSON。
- ``apply --confirm <summary_sha256>``：先在同一事务里重算摘要，与 ``--confirm`` 不等或与
  ``plan`` 之后表已变化即拒（退出 3，零写）；库侧单事务按外键顺序删除；预发表删该一行并
  读回确认不存在；每步打印「表 / 行数」，结束打印回读结果。
- ``restore``：从标准输入读 ``backup`` 的 JSON，库侧单事务按外键顺序插回（主键与时间戳
  原样），预发表按备份字段重建该行（``record_id`` 会变化），读回。

## 删 / 不删清单（范围判定；迁移头 ``0098_qa_corpus``）

引用 ``app_user`` 的表逐一判定。**删**（该用户回到「从未开通」所需的最小集合）：
``app_user`` 本行；``publish_outbox``（发布意图与外部记录标识）；``mcp_access_token``
（令牌库该用户行，只是密文副本）；``mcp_sync_check``（就绪探针）；
``onboarding_completion_notice``（开通完成通知）；``local_permission_override``（本地覆盖）；
``user_memory``（用户记忆）；``outreach_message``（欢迎卡幂等事实，删掉才能重发）；
``innertest_check``（扩员批次的逐人检查结果）；``innertest_membership``（按 open_id 的内测
资格，无外键）；``conversation`` / ``task`` / ``task_delivery_event`` /
``task_document_delivery_request`` / ``agent_session_cleanup``（会话与任务历史：外键
``NO ACTION``，不删就删不掉 ``app_user``；从未开通的人没有会话）。
**不删**（历史事实）：``operation_audit``、``pending_action``、``admin_action_followup``
（其 ``target_user_id`` 外键为 ``ON DELETE SET NULL``，删用户时被置空——备份记下原值、
回退时写回）、``management_card_context``、``onboarding_failure``、``inbound_event``（说过话
的证据，重建用户时由触发器自动采纳）、``innertest_batch`` / ``innertest_batch_item``、
``admin_registry`` / ``qa_corpus_reader``、快照与银河副本。
**留存内容，默认不删、拦停待裁定**：``qa_corpus``（外键 ``ON DELETE CASCADE``，删用户必随删）
与 ``innertest_content_capture``（随 ``task`` 级联）。两张表有行时 ``plan`` 判「被拦」、
``apply`` 退出 2；产品负责人裁定随删后加 ``--delete-retained-content`` 重新 ``plan``（摘要
绑定该选项）。脚本另按 ``pg_constraint`` 现查引用图：出现清单之外的引用表即拒绝，不静默级联。

## 硬闸（退出码 2，零写）

① ``LINGXI_PERMISSION_BITABLE_APP_TOKEN`` 等于 ``LINGXI_STOCK_TOKEN_BITABLE_APP_TOKEN``
（预发写入面指向正式表；生产两者同一 Base，因此这道闸在生产必拦）或后者缺失；
② ``LINGXI_DEPLOY_ENVIRONMENT`` 自称生产（判定与 ``apps/worker/config.py`` 同一张表；
scheduler 容器目前不声明它，所以真正拦生产的是闸①）；③ 没有按邮箱模糊或多用户模式；
④ 除 ``backup`` 的标准输出外，任何输出不含令牌密文与正文。

## 退出码

``0`` 跑完；``2`` 什么都没做（参数、闸门、配置、定位或被拦）；``3`` 摘要不符或 ``plan`` 之后
表已变化，零写；``4`` 预发表那一步结果不明（库侧已回滚零写，或已提交但回读不符）——重新
``plan`` 看现状再决定。

## 未覆盖（如实登记）

用户环境目录（``<LINGXI_USER_ENV_ROOT>/<user_id>/``，含该用户 ``.mcp.json``）不在本脚本范围，
复位后成为孤儿目录；Agent 会话 JSONL 的清理队列行随用户删除，对应文件不再被清理。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from lingxi.adapters.postgres import connect
from lingxi.apps.worker.config import declares_production
from lingxi.core.permission.account_match import normalize_email
from lingxi.core.permission.publish_row import readback_text

SCHEMA_VERSION = 1
ENVIRONMENT = "stage"
DEFAULT_FEISHU_BASE_URL = "https://open.feishu.cn/open-apis"
PAGE_SIZE = 500
MAX_PAGES = 50
REQUEST_TIMEOUT_SECONDS = 30
#: 飞书按记录标识读一行：HTTP 404 或业务码 1254005（RecordIdNotFound）都表示不存在。
#: 删行接口与读回形态未经真实调用验证（撤回演示未做），首次在预发执行时以输出为准。
RECORD_NOT_FOUND_CODES = frozenset({1254005})
BITABLE_FIELD_NAMES: tuple[str, ...] = (
    "record_key",
    "email",
    "name",
    "permissions",
    "status",
    "updated_at",
    "token_cipher",
)
TOKEN_CIPHER_FIELD = "token_cipher"

DSN_VAR = "LINGXI_POSTGRES_DSN"
FEISHU_APP_ID_VAR = "LINGXI_FEISHU_APP_ID"
FEISHU_APP_SECRET_VAR = "LINGXI_FEISHU_APP_SECRET"
FEISHU_BASE_URL_VAR = "LINGXI_FEISHU_BASE_URL"
PERMISSION_APP_TOKEN_VAR = "LINGXI_PERMISSION_BITABLE_APP_TOKEN"
PERMISSION_TABLE_ID_VAR = "LINGXI_PERMISSION_BITABLE_TABLE_ID"
STOCK_APP_TOKEN_VAR = "LINGXI_STOCK_TOKEN_BITABLE_APP_TOKEN"
REQUIRED_VARS: tuple[str, ...] = (
    DSN_VAR,
    FEISHU_APP_ID_VAR,
    FEISHU_APP_SECRET_VAR,
    PERMISSION_APP_TOKEN_VAR,
    PERMISSION_TABLE_ID_VAR,
    STOCK_APP_TOKEN_VAR,
)

EXIT_DONE = 0
EXIT_NOTHING_DONE = 2
EXIT_SUMMARY_MISMATCH = 3
EXIT_EXTERNAL_UNCERTAIN = 4

_OPEN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class GateRejectedError(RuntimeError):
    """闸门、参数、配置或定位不合格：什么都没做，退出码 2。"""


class SummaryMismatchError(RuntimeError):
    """确认摘要与现状不符、或回读与预期不符：退出码 3。"""


class BitableError(RuntimeError):
    """预发表调用失败，只带分类码，不带响应正文与凭据。"""

    def __init__(self, code: str) -> None:
        """记录可安全输出的分类码。"""
        self.code = code
        super().__init__(code)


# ---------------------------------------------------------------- 删 / 不删清单


@dataclass(frozen=True)
class TableSpec:
    """一张将删表：定位方式、显示用主键、留存标记与摘要不含的敏感列。"""

    name: str
    key_columns: tuple[str, ...]
    locate: str
    retained: bool = False
    digest_excluded: frozenset[str] = frozenset()


#: 删除顺序 = 外键子表在前；回退按倒序插回。``locate``：``user`` 按 ``user_id``、``task`` 按
#: 该用户任务的 ``task_id``、``self`` 是 ``app_user`` 本行、``open_id`` 按飞书 open_id。
DELETE_ORDER: tuple[TableSpec, ...] = (
    TableSpec("task_delivery_event", ("id",), "task", digest_excluded=frozenset({"content"})),
    TableSpec(
        "task_document_delivery_request",
        ("id",),
        "task",
        digest_excluded=frozenset({"title", "paragraphs", "markdown"}),
    ),
    TableSpec(
        "innertest_content_capture",
        ("id",),
        "task",
        retained=True,
        digest_excluded=frozenset({"question_content", "answer_content", "tool_calls"}),
    ),
    TableSpec(
        "qa_corpus",
        ("id",),
        "user",
        retained=True,
        digest_excluded=frozenset(
            {"question_content", "answer_delivered", "answer_model_raw", "tool_calls"}
        ),
    ),
    TableSpec("task", ("id",), "user", digest_excluded=frozenset({"prompt", "token_usage"})),
    TableSpec("conversation", ("id",), "user"),
    TableSpec("agent_session_cleanup", ("id",), "user"),
    TableSpec("innertest_check", ("id",), "user"),
    TableSpec("local_permission_override", ("id",), "user"),
    TableSpec(
        "mcp_access_token", ("user_id",), "user", digest_excluded=frozenset({TOKEN_CIPHER_FIELD})
    ),
    TableSpec("mcp_sync_check", ("id",), "user"),
    TableSpec("onboarding_completion_notice", ("id",), "user"),
    TableSpec("outreach_message", ("id",), "user"),
    TableSpec("publish_outbox", ("id",), "user", digest_excluded=frozenset({"payload"})),
    TableSpec("user_memory", ("id",), "user", digest_excluded=frozenset({"memory_value"})),
    TableSpec("app_user", ("id",), "self"),
    TableSpec("innertest_membership", ("scope", "open_id"), "open_id"),
)
DELETE_SET: frozenset[str] = frozenset(spec.name for spec in DELETE_ORDER)
#: 外键指向将删表、但本身**不删**只被置空的表：``admin_action_followup.target_user_id``。
RELINK_TABLE = "admin_action_followup"
RELINK_COLUMN = "target_user_id"

_REFERENCING_SQL = (
    "SELECT child.relname, parent.relname"
    "  FROM pg_constraint k"
    "  JOIN pg_class child ON child.oid = k.conrelid"
    "  JOIN pg_class parent ON parent.oid = k.confrelid"
    "  JOIN pg_namespace n ON n.oid = child.relnamespace"
    " WHERE k.contype = 'f' AND n.nspname = 'public'"
)


def verify_reference_graph(cursor: Any) -> None:
    """现查外键图：任何指向将删表、又不在清单里的引用表都拒绝，不让级联静默删掉它。"""
    cursor.execute(_REFERENCING_SQL)
    unknown = sorted(
        {
            f"{child} → {parent}"
            for child, parent in cursor.fetchall()
            if parent in DELETE_SET and child not in DELETE_SET and child != RELINK_TABLE
        }
    )
    if unknown:
        raise GateRejectedError(
            "发现清单之外的引用表，请先更新本脚本的删 / 不删清单：" + "；".join(unknown)
        )


# ---------------------------------------------------------------- 配置与闸门


@dataclass(frozen=True, repr=False)
class Settings:
    """本次运行需要的连接串与飞书坐标；密钥只存在进程内。"""

    dsn: str
    app_id: str
    app_secret: str
    base_url: str
    app_token: str
    table_id: str

    def __repr__(self) -> str:
        """不回显任何值。"""
        return "Settings(<redacted>)"


def production_gate(environment: Mapping[str, str]) -> str | None:
    """硬闸 ①②：返回拒绝理由，``None`` 表示可以继续。"""
    if declares_production(environment):
        return "环境自称生产，本脚本只在预发使用。"
    permission = (environment.get(PERMISSION_APP_TOKEN_VAR) or "").strip()
    stock = (environment.get(STOCK_APP_TOKEN_VAR) or "").strip()
    if not permission or not stock:
        return (
            f"缺少 {PERMISSION_APP_TOKEN_VAR} 或 {STOCK_APP_TOKEN_VAR}，"
            "无法证明预发表与正式表分离。"
        )
    if permission == stock:
        return "预发权限发布表与正式表指向同一个 Base，拒绝一切子命令。"
    return None


def _https_base_url(value: str) -> str:
    text = value.strip() or DEFAULT_FEISHU_BASE_URL
    parsed = urlsplit(text)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise GateRejectedError(f"{FEISHU_BASE_URL_VAR} 必须是不含凭据的 HTTPS 地址。")
    return text.rstrip("/")


def load_settings(environment: Mapping[str, str]) -> Settings:
    """读环境变量；缺哪一个只报变量名，不回显值。"""
    missing = [name for name in REQUIRED_VARS if not (environment.get(name) or "").strip()]
    if missing:
        raise GateRejectedError("缺少环境变量：" + "、".join(missing))
    return Settings(
        dsn=environment[DSN_VAR].strip(),
        app_id=environment[FEISHU_APP_ID_VAR].strip(),
        app_secret=environment[FEISHU_APP_SECRET_VAR].strip(),
        base_url=_https_base_url(environment.get(FEISHU_BASE_URL_VAR) or ""),
        app_token=environment[PERMISSION_APP_TOKEN_VAR].strip(),
        table_id=environment[PERMISSION_TABLE_ID_VAR].strip(),
    )


@dataclass(frozen=True)
class Identity:
    """命令行给出的双重定位键：规范化邮箱 + 飞书 open_id。"""

    email: str
    open_id: str


def parse_identity(email: str, open_id: str) -> Identity:
    """校验两个定位键的形状；不做模糊匹配。"""
    normalized = normalize_email(email)
    if not normalized or "@" not in normalized or any(ch.isspace() for ch in normalized):
        raise GateRejectedError("--email 不是一个完整邮箱。")
    open_id = open_id.strip()
    if not _OPEN_ID.match(open_id):
        raise GateRejectedError("--open-id 不是合法的 open_id 形状。")
    return Identity(normalized, open_id)


# ---------------------------------------------------------------- 预发表客户端


@dataclass(frozen=True)
class HttpResponse:
    """传输层返回的状态码与解析后的正文。"""

    status: int
    payload: Any


@dataclass(frozen=True)
class TableRow:
    """预发表一行：记录标识 + 七字段文本。"""

    record_id: str
    fields: dict[str, str]


def _decode_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise BitableError("feishu_response_invalid") from error


def urllib_transport(
    method: str, url: str, *, body: Mapping[str, Any] | None, headers: Mapping[str, str]
) -> HttpResponse:
    """默认传输：标准库 HTTPS，一次一发、不重试任何外部写入。"""
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request_headers = {"Accept": "application/json", **headers}
    if body is not None:
        request_headers["Content-Type"] = "application/json; charset=utf-8"
    request = Request(url, data=payload, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # 地址来自受控配置
            return HttpResponse(response.status, _decode_json(response.read()))
    except HTTPError as error:
        try:
            payload_value = _decode_json(error.read())
        except BitableError:
            payload_value = {}
        return HttpResponse(error.code, payload_value)
    except (URLError, OSError, TimeoutError) as error:
        raise BitableError("feishu_transport_error") from error


def _response(value: Any) -> HttpResponse:
    """兼容标准传输、测试传输与 ``(status, payload)`` 简化返回形状。"""
    if isinstance(value, HttpResponse):
        return value
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], int):
        return HttpResponse(value[0], value[1])
    return HttpResponse(200, value)


def _json_body(response: HttpResponse, *, allow_empty: bool = False) -> Mapping[str, Any]:
    if response.status < 200 or response.status >= 300:
        raise BitableError(f"feishu_http_{response.status}")
    if not isinstance(response.payload, Mapping):
        if allow_empty and response.payload in (None, ""):
            return {}
        raise BitableError("feishu_response_invalid")
    code = response.payload.get("code")
    if code not in (None, 0, "0"):
        label = f"feishu_code_{code}" if isinstance(code, int) else "feishu_code_invalid"
        raise BitableError(label)
    data = response.payload.get("data")
    if isinstance(data, Mapping):
        return data
    if allow_empty and data is None:
        return {}
    raise BitableError("feishu_response_invalid")


def _record_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or any(ch.isspace() for ch in value):
        raise BitableError("record_id_invalid")
    return value


def _fields(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or any(name not in value for name in BITABLE_FIELD_NAMES):
        raise BitableError("record_fields_incomplete")
    return {name: readback_text(value[name]) for name in BITABLE_FIELD_NAMES}


def _matches(value: Any, wanted: str) -> bool:
    return bool(wanted) and readback_text(value).strip().casefold() == wanted.casefold()


def _not_found(response: HttpResponse) -> bool:
    if response.status == 404:
        return True
    payload = response.payload
    return isinstance(payload, Mapping) and payload.get("code") in RECORD_NOT_FOUND_CODES


class StageTableClient:
    """预发权限发布表的分页读、按标识读回、删行与建行；令牌只放请求头。"""

    def __init__(self, settings: Settings, *, transport: Callable[..., Any] | None = None) -> None:
        """只存配置与传输，不做任何 I/O。"""
        self._settings = settings
        self._transport = transport or urllib_transport
        self._access_token: str | None = None

    @property
    def _records_url(self) -> str:
        app_token = quote(self._settings.app_token, safe="")
        table_id = quote(self._settings.table_id, safe="")
        return f"{self._settings.base_url}/bitable/v1/apps/{app_token}/tables/{table_id}/records"

    def _record_url(self, record_id: str) -> str:
        return f"{self._records_url}/{quote(_record_id(record_id), safe='')}"

    def _request(
        self, method: str, url: str, *, body: Mapping[str, Any] | None = None
    ) -> HttpResponse:
        headers = {"Authorization": f"Bearer {self._access_token}"} if self._access_token else {}
        try:
            value = self._transport(method, url, body=body, headers=headers)
        except BitableError:
            raise
        except Exception as error:
            raise BitableError("feishu_transport_error") from error
        return _response(value)

    def _ensure_token(self) -> None:
        if self._access_token is not None:
            return
        response = self._request(
            "POST",
            f"{self._settings.base_url}/auth/v3/tenant_access_token/internal",
            body={"app_id": self._settings.app_id, "app_secret": self._settings.app_secret},
        )
        payload = response.payload if isinstance(response.payload, Mapping) else {}
        token = payload.get("tenant_access_token")
        if response.status != 200 or payload.get("code") not in (None, 0, "0") or not token:
            raise BitableError("feishu_token_error")
        self._access_token = str(token)

    def _page(self, page_token: str | None) -> Mapping[str, Any]:
        parameters: dict[str, Any] = {"page_size": PAGE_SIZE}
        if page_token:
            parameters["page_token"] = page_token
        return _json_body(self._request("GET", f"{self._records_url}?{urlencode(parameters)}"))

    def list_rows(self) -> list[TableRow]:
        """整表分页（500/页、最多 50 页）；撞上上界即失败，不把「可能漏读」当成读完。"""
        self._ensure_token()
        rows: list[TableRow] = []
        page_token: str | None = None
        for _ in range(MAX_PAGES):
            data = self._page(page_token)
            items = data.get("items")
            if items is None and data.get("has_more") is None and page_token is None:
                return rows
            if not isinstance(items, list):
                raise BitableError("feishu_page_invalid")
            for item in items:
                if not isinstance(item, Mapping):
                    raise BitableError("feishu_page_invalid")
                rows.append(
                    TableRow(_record_id(item.get("record_id")), _fields(item.get("fields")))
                )
            has_more = data.get("has_more")
            if not isinstance(has_more, bool):
                raise BitableError("feishu_pagination_invalid")
            if not has_more:
                return rows
            candidate = data.get("page_token")
            if not isinstance(candidate, str) or not candidate or candidate == page_token:
                raise BitableError("feishu_pagination_stalled")
            page_token = candidate
        raise BitableError("feishu_pagination_limit")

    def find_user_rows(self, identity: Identity) -> list[TableRow]:
        """按 ``record_key`` / ``email`` 命中该用户的行（与发布侧同一把归一尺子）。"""
        return [
            row
            for row in self.list_rows()
            if _matches(row.fields.get("record_key"), identity.email)
            or _matches(row.fields.get("email"), identity.email)
        ]

    def read_row(self, record_id: str) -> dict[str, str] | None:
        """按标识读一行；不存在返回 ``None``。"""
        self._ensure_token()
        response = self._request("GET", self._record_url(record_id))
        if _not_found(response):
            return None
        record = _json_body(response).get("record")
        if not isinstance(record, Mapping) or _record_id(record.get("record_id")) != record_id:
            raise BitableError("feishu_record_invalid")
        return _fields(record.get("fields"))

    def delete_row(self, record_id: str) -> None:
        """删一行；调用方随后必须读回。"""
        self._ensure_token()
        _json_body(self._request("DELETE", self._record_url(record_id)), allow_empty=True)

    def create_row(self, fields: Mapping[str, str]) -> str:
        """按七字段新建一行，返回新的记录标识。"""
        self._ensure_token()
        data = _json_body(self._request("POST", self._records_url, body={"fields": dict(fields)}))
        record = data.get("record")
        if not isinstance(record, Mapping):
            raise BitableError("feishu_record_invalid")
        return _record_id(record.get("record_id"))


# ---------------------------------------------------------------- 足迹采集与摘要


@dataclass(frozen=True)
class TableRows:
    """一张表上将删的行（完整字段，列名 → 值）。"""

    spec: TableSpec
    rows: list[dict[str, Any]]


@dataclass(frozen=True)
class Footprint:
    """该用户在预发库与预发表的全部足迹。"""

    identity: Identity
    user_id: str | None
    task_ids: list[str]
    tables: list[TableRows]
    relink: list[dict[str, Any]] = field(default_factory=list)
    bitable_row: TableRow | None = None

    def locator(self) -> Locator:
        """本足迹的定位键。"""
        return Locator(self.user_id, self.task_ids, self.identity)

    def retained_hits(self) -> list[tuple[str, int]]:
        """有行的留存内容表：默认不删、拦停待裁定。"""
        return [(t.spec.name, len(t.rows)) for t in self.tables if t.spec.retained and t.rows]

    def deletable(self, *, delete_retained: bool) -> list[TableRows]:
        """按选项给出真正进入删除集合的表。"""
        return [t for t in self.tables if delete_retained or not t.spec.retained]


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes | memoryview):
        return bytes(value).hex()
    raise TypeError(f"不支持序列化的列类型：{type(value).__name__}")


def canonical_json(value: Any) -> str:
    """稳定序列化：键排序、无空白、日期用 ISO 8601。"""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default
    )


def row_digest(spec: TableSpec, row: Mapping[str, Any]) -> str:
    """主键 + 非敏感列的 sha256 前 16 位。"""
    visible = {name: value for name, value in row.items() if name not in spec.digest_excluded}
    return hashlib.sha256(canonical_json(visible).encode("utf-8")).hexdigest()[:16]


def row_key(spec: TableSpec, row: Mapping[str, Any]) -> str:
    """显示用主键文本。"""
    return "/".join(str(row.get(column)) for column in spec.key_columns)


def bitable_digest(fields: Mapping[str, str]) -> str:
    """七字段去掉令牌密文后的 sha256 前 16 位。"""
    visible = {
        name: fields.get(name, "") for name in BITABLE_FIELD_NAMES if name != TOKEN_CIPHER_FIELD
    }
    return hashlib.sha256(canonical_json(visible).encode("utf-8")).hexdigest()[:16]


def summary_sha256(footprint: Footprint, *, delete_retained: bool) -> str:
    """绑定全部将删行的摘要清单、置空清单、预发表记录标识与留存选项。"""
    bitable = None
    if footprint.bitable_row is not None:
        bitable = {
            "record_id": footprint.bitable_row.record_id,
            "digest": bitable_digest(footprint.bitable_row.fields),
        }
    payload = {
        "schema": SCHEMA_VERSION,
        "email": footprint.identity.email,
        "open_id": footprint.identity.open_id,
        "user_id": footprint.user_id,
        "delete_retained_content": delete_retained,
        "tables": {
            t.spec.name: sorted(row_digest(t.spec, row) for row in t.rows)
            for t in footprint.deletable(delete_retained=delete_retained)
        },
        "relink": sorted(str(row["id"]) for row in footprint.relink),
        "bitable": bitable,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _fetch_rows(cursor: Any, sql: str, parameters: tuple) -> list[dict[str, Any]]:
    cursor.execute(sql, parameters)
    columns = [column.name for column in cursor.description]
    return [dict(zip(columns, values, strict=True)) for values in cursor.fetchall()]


def locate_user(cursor: Any, identity: Identity) -> str | None:
    """邮箱与 open_id 必须指向同一条 ``app_user``；都不命中表示库侧无此人。"""
    by_email = _fetch_rows(
        cursor, "SELECT id FROM public.app_user WHERE lower(btrim(email)) = %s", (identity.email,)
    )
    by_open_id = _fetch_rows(
        cursor, "SELECT id FROM public.app_user WHERE feishu_open_id = %s", (identity.open_id,)
    )
    email_ids = {str(row["id"]) for row in by_email}
    open_ids = {str(row["id"]) for row in by_open_id}
    if not email_ids and not open_ids:
        return None
    if len(email_ids) != 1 or email_ids != open_ids:
        raise GateRejectedError(
            "--email 与 --open-id 指向的不是同一条 app_user 记录，未做任何操作。"
        )
    return next(iter(email_ids))


@dataclass(frozen=True)
class Locator:
    """定位一个用户足迹所需的三把键：内部用户标识、其任务标识、命令行定位键。"""

    user_id: str | None
    task_ids: list[str]
    identity: Identity


def _where(spec: TableSpec, locator: Locator) -> tuple:
    """给出定位谓词与参数；用户不存在时按用户定位的表一律为空。"""
    if spec.locate == "open_id":
        return "open_id = %s", (locator.identity.open_id,)
    if locator.user_id is None:
        return None, ()
    if spec.locate == "task":
        return "task_id = ANY(%s)", (locator.task_ids,)
    if spec.locate == "self":
        return "id = %s", (locator.user_id,)
    return "user_id = %s", (locator.user_id,)


def _count(cursor: Any, spec: TableSpec, locator: Locator) -> int | None:
    where, parameters = _where(spec, locator)
    if where is None:
        return None
    cursor.execute(f'SELECT count(*) FROM public."{spec.name}" WHERE {where}', parameters)
    return int(cursor.fetchone()[0])


def collect_footprint(cursor: Any, client: StageTableClient, identity: Identity) -> Footprint:
    """在当前事务快照内读全该用户的足迹；预发表按 record_key / email 定位。"""
    verify_reference_graph(cursor)
    user_id = locate_user(cursor, identity)
    task_ids: list[str] = []
    if user_id is not None:
        cursor.execute("SELECT id FROM public.task WHERE user_id = %s ORDER BY id", (user_id,))
        task_ids = [str(row[0]) for row in cursor.fetchall()]
    locator = Locator(user_id, task_ids, identity)
    tables: list[TableRows] = []
    for spec in DELETE_ORDER:
        where, parameters = _where(spec, locator)
        rows: list[dict[str, Any]] = []
        if where is not None:
            order = ", ".join(spec.key_columns)
            sql = f'SELECT * FROM public."{spec.name}" WHERE {where} ORDER BY {order}'
            rows = _fetch_rows(cursor, sql, parameters)
        if spec.name == "innertest_membership":
            for row in rows:
                if normalize_email(row.get("email")) != identity.email:
                    raise GateRejectedError("内测资格行的邮箱与 --email 不一致，未做任何操作。")
        tables.append(TableRows(spec, rows))
    relink: list[dict[str, Any]] = []
    if user_id is not None:
        relink = _fetch_rows(
            cursor,
            f'SELECT id, {RELINK_COLUMN} FROM public."{RELINK_TABLE}"'
            f" WHERE {RELINK_COLUMN} = %s ORDER BY id",
            (user_id,),
        )
    matches = client.find_user_rows(identity)
    if len(matches) > 1:
        raise GateRejectedError("预发权限发布表里该用户命中多行，拒绝处理，请人工核对。")
    return Footprint(identity, user_id, task_ids, tables, relink, matches[0] if matches else None)


# ---------------------------------------------------------------- 数据库事务与输出


def open_transaction(dsn: str) -> Any:
    """独占连接 + 可重复读快照：读到的行集就是随后要删（或要插回）的行集。"""
    connection = connect(dsn, dedicated=True)
    with connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    return connection


@dataclass(frozen=True)
class Streams:
    """输入输出与传输的注入点（单测替换真实网络与标准流）。"""

    out: TextIO
    err: TextIO
    stdin: TextIO
    transport: Callable[..., Any] | None

    def log(self, message: str) -> None:
        """进度只走标准错误：``backup`` 的标准输出必须是纯 JSON。"""
        print(message, file=self.err, flush=True)


def _discard(connection: Any) -> None:
    """回滚并关闭；这里的失败不能盖过调用方正在处理的那个异常。"""
    try:
        connection.rollback()
    except Exception:  # 连接已断：无事务可回滚
        pass
    finally:
        connection.close()


def _commit_or_report(connection: Any, io: Streams, external_note: str) -> bool:
    """提交；失败即回滚并说明外部那一步可能已发生，由调用方以退出码 4 收口。"""
    try:
        connection.commit()
    except Exception as error:
        try:
            connection.rollback()
        except Exception:  # 连接已断：提交本就没发生
            pass
        io.log(
            f"库侧提交失败（{type(error).__name__}）：库侧零写入；{external_note}，请重新 plan。"
        )
        return False
    return True


def _read_only_footprint(settings: Settings, identity: Identity, io: Streams) -> Footprint:
    connection = open_transaction(settings.dsn)
    try:
        with connection.cursor() as cursor:
            client = StageTableClient(settings, transport=io.transport)
            return collect_footprint(cursor, client, identity)
    finally:
        _discard(connection)


# ---------------------------------------------------------------- plan / backup


def _plan_lines(footprint: Footprint, *, delete_retained: bool) -> list[str]:
    lines = ["预发用户复位 · 干跑（零写入）", f"user_id={footprint.user_id or '（库侧无此人）'}"]
    for table in footprint.tables:
        marker = ""
        if table.spec.retained:
            marker = "【留存内容，随删】" if delete_retained else "【留存内容，默认不删】"
        lines.append(f"表 {table.spec.name}：{len(table.rows)} 行{marker}")
        lines.extend(
            f"  {row_key(table.spec, row)} {row_digest(table.spec, row)}" for row in table.rows
        )
    lines.append(f"外键置空 {RELINK_TABLE}.{RELINK_COLUMN}：{len(footprint.relink)} 行")
    if footprint.bitable_row is None:
        lines.append("预发权限发布表：无行")
    else:
        lines.append(
            f"预发权限发布表：1 行 record={footprint.bitable_row.record_id}"
            f" 摘要={bitable_digest(footprint.bitable_row.fields)}"
        )
    lines.append(f"summary_sha256={summary_sha256(footprint, delete_retained=delete_retained)}")
    hits = footprint.retained_hits()
    if hits and not delete_retained:
        detail = "、".join(f"{name} {count} 行" for name, count in hits)
        lines.append(
            f"结论：被拦——留存内容有行（{detail}），默认不删；"
            "需产品负责人裁定随删后加 --delete-retained-content 重新 plan。"
        )
    else:
        lines.append("结论：可执行（apply --confirm <summary_sha256>）。")
    return lines


def run_plan(
    settings: Settings, identity: Identity, arguments: argparse.Namespace, io: Streams
) -> int:
    """只读：列表、摘要与结论。"""
    footprint = _read_only_footprint(settings, identity, io)
    for line in _plan_lines(footprint, delete_retained=arguments.delete_retained_content):
        print(line, file=io.out)
    return EXIT_DONE


def _serialize_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(canonical_json(row))


def backup_payload(footprint: Footprint, *, delete_retained: bool) -> dict[str, Any]:
    """备份 JSON：将删行的完整字段（含密文列）、置空清单、预发表该行与确认摘要。"""
    bitable_row = None
    if footprint.bitable_row is not None:
        bitable_row = {
            "record_id": footprint.bitable_row.record_id,
            "fields": dict(footprint.bitable_row.fields),
        }
    return {
        "schema": SCHEMA_VERSION,
        "taken_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "environment": ENVIRONMENT,
        "email": footprint.identity.email,
        "open_id": footprint.identity.open_id,
        "user_id": footprint.user_id,
        "delete_retained_content": delete_retained,
        "tables": {
            t.spec.name: [_serialize_row(row) for row in t.rows]
            for t in footprint.deletable(delete_retained=delete_retained)
        },
        "relink": {RELINK_TABLE: [_serialize_row(row) for row in footprint.relink]},
        "bitable_row": bitable_row,
        "summary_sha256": summary_sha256(footprint, delete_retained=delete_retained),
    }


def run_backup(
    settings: Settings, identity: Identity, arguments: argparse.Namespace, io: Streams
) -> int:
    """只读：完整字段写标准输出（宿主落 0600），进度只走标准错误。"""
    delete_retained = arguments.delete_retained_content
    footprint = _read_only_footprint(settings, identity, io)
    payload = backup_payload(footprint, delete_retained=delete_retained)
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=io.out, flush=True)
    hits = footprint.retained_hits()
    if hits and not delete_retained:
        io.log("备份未含留存内容表（默认不删）：" + "、".join(f"{n} {c} 行" for n, c in hits))
    rows = sum(len(rows) for rows in payload["tables"].values())
    io.log(
        f"备份完成：{len(payload['tables'])} 表 {rows} 行"
        f" · 预发表 {1 if payload['bitable_row'] else 0} 行"
        f" · summary_sha256={payload['summary_sha256']}"
    )
    return EXIT_DONE


# ---------------------------------------------------------------- apply


def _delete_rows(cursor: Any, spec: TableSpec, rows: Sequence[Mapping[str, Any]]) -> int:
    """按主键逐行删；删掉的行数必须等于快照里的行数，否则视为表已变化。"""
    deleted = 0
    predicate = " AND ".join(f'"{column}" = %s' for column in spec.key_columns)
    for row in rows:
        cursor.execute(
            f'DELETE FROM public."{spec.name}" WHERE {predicate}',
            tuple(row[column] for column in spec.key_columns),
        )
        deleted += cursor.rowcount
    if deleted != len(rows):
        raise SummaryMismatchError(
            f"表 {spec.name} 在 plan 之后已变化（预期删 {len(rows)} 行，实删 {deleted}）"
        )
    return deleted


def _apply_deletes(
    cursor: Any, footprint: Footprint, *, delete_retained: bool, io: Streams
) -> None:
    """先显式置空外键、再按外键顺序删；置空写在删 ``app_user`` 之前，行数才是真实的。"""
    if footprint.relink:
        cursor.execute(
            f'UPDATE public."{RELINK_TABLE}" SET {RELINK_COLUMN} = NULL WHERE {RELINK_COLUMN} = %s',
            (footprint.user_id,),
        )
        if cursor.rowcount != len(footprint.relink):
            raise SummaryMismatchError(
                f"表 {RELINK_TABLE} 在 plan 之后已变化"
                f"（预期置空 {len(footprint.relink)} 行，实置 {cursor.rowcount}）"
            )
        io.log(f"已置空 {RELINK_TABLE}.{RELINK_COLUMN} / {cursor.rowcount} 行")
    for table in footprint.deletable(delete_retained=delete_retained):
        if table.rows:
            io.log(f"已删 {table.spec.name} / {_delete_rows(cursor, table.spec, table.rows)} 行")


def _delete_bitable_row(client: StageTableClient, row: TableRow, io: Streams) -> None:
    client.delete_row(row.record_id)
    if client.read_row(row.record_id) is not None:
        raise BitableError("delete_readback_present")
    io.log(f"已删 预发权限发布表 / 1 行（record={row.record_id}，读回不存在）")


def _readback_after_apply(
    cursor: Any, client: StageTableClient, footprint: Footprint, *, delete_retained: bool
) -> None:
    for spec in DELETE_ORDER:
        if spec.retained and not delete_retained:
            continue
        remaining = _count(cursor, spec, footprint.locator())
        if remaining:
            raise SummaryMismatchError(f"回读：表 {spec.name} 仍有 {remaining} 行")
    if footprint.bitable_row is not None and client.read_row(footprint.bitable_row.record_id):
        raise BitableError("delete_readback_present")


def _gate_apply(footprint: Footprint, confirm: str, *, delete_retained: bool) -> None:
    hits = footprint.retained_hits()
    if hits and not delete_retained:
        raise GateRejectedError(
            "留存内容表有行（默认不删）：" + "、".join(f"{n} {c} 行" for n, c in hits)
        )
    if confirm != summary_sha256(footprint, delete_retained=delete_retained):
        raise SummaryMismatchError("--confirm 与现状重算的 summary_sha256 不等，零写入。")


def run_apply(
    settings: Settings, identity: Identity, arguments: argparse.Namespace, io: Streams
) -> int:
    """同一事务内重算摘要 → 按外键顺序删 → 预发表删行并读回 → 才提交。"""
    confirm = arguments.confirm.strip().lower()
    if not _SHA256.match(confirm):
        raise GateRejectedError("--confirm 必须是 plan 输出的 64 位十六进制 summary_sha256。")
    delete_retained = arguments.delete_retained_content
    client = StageTableClient(settings, transport=io.transport)
    connection = open_transaction(settings.dsn)
    try:
        with connection.cursor() as cursor:
            footprint = collect_footprint(cursor, client, identity)
            _gate_apply(footprint, confirm, delete_retained=delete_retained)
            _apply_deletes(cursor, footprint, delete_retained=delete_retained, io=io)
            if footprint.bitable_row is not None:
                _delete_bitable_row(client, footprint.bitable_row, io)
        committed = _commit_or_report(connection, io, "预发表该行可能已删")
    except BitableError as error:
        _discard(connection)
        io.log(f"预发表删行未确认（{error.code}）：库侧已回滚零写入；预发表现状请重新 plan。")
        return EXIT_EXTERNAL_UNCERTAIN
    except BaseException:
        _discard(connection)
        raise
    if not committed:
        connection.close()
        return EXIT_EXTERNAL_UNCERTAIN
    try:
        with connection.cursor() as cursor:
            _readback_after_apply(cursor, client, footprint, delete_retained=delete_retained)
    except Exception as error:
        io.log(f"复位已提交，但回读不符（{error}）：请重新 plan 核对现状。")
        return EXIT_EXTERNAL_UNCERTAIN
    finally:
        _discard(connection)
    io.log("回读：库侧该用户行已不存在；预发权限发布表该行已不存在。复位完成。")
    return EXIT_DONE


# ---------------------------------------------------------------- restore


def load_backup(stream: TextIO, identity: Identity) -> dict[str, Any]:
    """读并校验备份：结构、环境、定位键必须与命令行一致。"""
    try:
        payload = json.load(stream)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise GateRejectedError(
            f"标准输入不是合法的备份 JSON（{type(error).__name__}）。"
        ) from None
    if not isinstance(payload, Mapping) or payload.get("schema") != SCHEMA_VERSION:
        raise GateRejectedError("备份结构或版本不符。")
    if payload.get("environment") != ENVIRONMENT:
        raise GateRejectedError("备份不是预发环境产出。")
    if payload.get("email") != identity.email or payload.get("open_id") != identity.open_id:
        raise GateRejectedError("备份里的邮箱 / open_id 与命令行不一致。")
    tables = payload.get("tables")
    if not isinstance(tables, Mapping) or any(name not in DELETE_SET for name in tables):
        raise GateRejectedError("备份的表清单与本脚本的删 / 不删清单不符。")
    if any(not isinstance(rows, list) for rows in tables.values()):
        raise GateRejectedError("备份里某张表的行不是列表。")
    return dict(payload)


def _column_types(cursor: Any, table: str) -> dict[str, str]:
    cursor.execute(
        "SELECT column_name, data_type FROM information_schema.columns"
        " WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    )
    return {str(name): str(kind) for name, kind in cursor.fetchall()}


def _insert_rows(cursor: Any, table: str, rows: Sequence[Mapping[str, Any]]) -> int:
    """按备份列名插回，值按列类型显式转换（时间戳与 jsonb 走文本转换）。"""
    if not rows:
        return 0
    types = _column_types(cursor, table)
    inserted = 0
    for row in rows:
        columns = list(row.keys())
        unknown = [column for column in columns if column not in types]
        if unknown:
            raise GateRejectedError(f"表 {table} 没有备份里的列：{'、'.join(unknown)}")
        values = [
            json.dumps(row[column], ensure_ascii=False) if types[column] == "jsonb" else row[column]
            for column in columns
        ]
        placeholders = ", ".join(f"%s::{types[column]}" for column in columns)
        names = ", ".join(f'"{column}"' for column in columns)
        cursor.execute(f'INSERT INTO public."{table}" ({names}) VALUES ({placeholders})', values)
        inserted += cursor.rowcount
    return inserted


def _restore_tables(cursor: Any, payload: Mapping[str, Any], io: Streams) -> None:
    tables: Mapping[str, Any] = payload["tables"]
    for spec in reversed(DELETE_ORDER):
        rows = tables.get(spec.name) or []
        if rows:
            io.log(f"已插回 {spec.name} / {_insert_rows(cursor, spec.name, rows)} 行")
    relink = (payload.get("relink") or {}).get(RELINK_TABLE) or []
    restored = 0
    for row in relink:
        cursor.execute(
            f'UPDATE public."{RELINK_TABLE}" SET {RELINK_COLUMN} = %s'
            f" WHERE id = %s AND {RELINK_COLUMN} IS NULL",
            (row[RELINK_COLUMN], row["id"]),
        )
        restored += cursor.rowcount
    if relink:
        io.log(f"已写回 {RELINK_TABLE}.{RELINK_COLUMN} / {restored} 行（备份 {len(relink)} 行）")


def _restore_bitable_row(
    client: StageTableClient, backup_row: Mapping[str, Any], identity: Identity, io: Streams
) -> None:
    fields = _fields(backup_row.get("fields"))
    if client.find_user_rows(identity):
        raise GateRejectedError(
            "预发权限发布表已有该用户的行，不重建第二行；请先 plan / apply 清掉再回退。"
        )
    record_id = client.create_row(fields)
    actual = client.read_row(record_id)
    mismatch = [
        name for name in BITABLE_FIELD_NAMES if actual is None or actual.get(name) != fields[name]
    ]
    if mismatch:
        raise BitableError("create_readback_mismatch:" + ",".join(mismatch))
    io.log(f"已重建 预发权限发布表 / 1 行（新 record={record_id}，七字段读回一致）")


def _readback_after_restore(cursor: Any, payload: Mapping[str, Any], identity: Identity) -> None:
    user_id = payload.get("user_id")
    if user_id is not None and locate_user(cursor, identity) != user_id:
        raise SummaryMismatchError("回读：app_user 行与备份不符")
    tables: Mapping[str, Any] = payload["tables"]
    task_ids = [str(row["id"]) for row in tables.get("task") or []]
    locator = Locator(user_id, task_ids, identity)
    for spec in DELETE_ORDER:
        actual = _count(cursor, spec, locator)
        expected = len(tables.get(spec.name) or [])
        if actual is not None and actual != expected:
            raise SummaryMismatchError(f"回读：表 {spec.name} 行数 {actual} ≠ 备份 {expected}")


def run_restore(
    settings: Settings, identity: Identity, arguments: argparse.Namespace, io: Streams
) -> int:
    """备份 JSON → 单事务插回 → 预发表重建并读回 → 才提交。"""
    payload = load_backup(io.stdin, identity)
    client = StageTableClient(settings, transport=io.transport)
    connection = open_transaction(settings.dsn)
    try:
        with connection.cursor() as cursor:
            verify_reference_graph(cursor)
            if locate_user(cursor, identity) is not None:
                raise GateRejectedError("库里已有该用户的 app_user 行，不重复插回。")
            _restore_tables(cursor, payload, io)
            if payload.get("bitable_row") is not None:
                _restore_bitable_row(client, payload["bitable_row"], identity, io)
        committed = _commit_or_report(connection, io, "预发表该行可能已重建")
    except BitableError as error:
        _discard(connection)
        io.log(f"预发表重建未确认（{error.code}）：库侧已回滚零写入；预发表现状请重新 plan。")
        return EXIT_EXTERNAL_UNCERTAIN
    except BaseException:
        _discard(connection)
        raise
    if not committed:
        connection.close()
        return EXIT_EXTERNAL_UNCERTAIN
    try:
        with connection.cursor() as cursor:
            _readback_after_restore(cursor, payload, identity)
    except Exception as error:
        io.log(f"回退已提交，但回读不符（{error}）：请重新 plan 核对现状。")
        return EXIT_EXTERNAL_UNCERTAIN
    finally:
        _discard(connection)
    io.log("回读：库侧各表行数与备份一致。回退完成。")
    return EXIT_DONE


# ---------------------------------------------------------------- 入口


_CLI_DESCRIPTION = (
    "预发单用户开通记录复位：plan（干跑）/ backup（备份到标准输出）/ apply（确认删除）"
    "/ restore（回退）。【硬约束】只在预发使用；必须在 lingxi-scheduler 容器内运行，"
    "脚本本体经 stdin 或 -c 传入。"
)


def build_parser() -> argparse.ArgumentParser:
    """四个子命令；关闭前缀缩写——对一个会删库的命令，手滑半个词不等于授权。"""
    parser = argparse.ArgumentParser(description=_CLI_DESCRIPTION, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True, metavar="<子命令>")
    for name, help_text in (
        ("plan", "只读干跑：列将删行与 summary_sha256"),
        ("backup", "只读备份：完整字段 JSON 写标准输出"),
        ("apply", "确认删除：--confirm 须等于现状重算的 summary_sha256"),
        ("restore", "回退：从标准输入读备份 JSON 插回"),
    ):
        command = commands.add_parser(name, allow_abbrev=False, help=help_text)
        command.add_argument("--email", required=True, help="该用户邮箱（规范化后比对）")
        command.add_argument("--open-id", required=True, dest="open_id", help="该用户飞书 open_id")
        if name != "restore":
            command.add_argument(
                "--delete-retained-content",
                action="store_true",
                dest="delete_retained_content",
                help="产品负责人裁定后才用：qa_corpus / innertest_content_capture 该用户行一并删除",
            )
        if name == "apply":
            command.add_argument("--confirm", required=True, help="plan 输出的 summary_sha256")
    return parser


RUNNERS: dict[str, Callable[..., int]] = {
    "plan": run_plan,
    "backup": run_backup,
    "apply": run_apply,
    "restore": run_restore,
}


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    transport: Callable[..., Any] | None = None,
    stdin: TextIO | None = None,
) -> int:
    """闸门 → 配置 → 定位键 → 子命令；一切拒绝都在任何写入之前。"""
    arguments = build_parser().parse_args(argv)
    env = os.environ if environment is None else environment
    io = Streams(sys.stdout, sys.stderr, stdin or sys.stdin, transport)
    try:
        rejection = production_gate(env)
        if rejection is not None:
            raise GateRejectedError(rejection)
        settings = load_settings(env)
        identity = parse_identity(arguments.email, arguments.open_id)
        return RUNNERS[arguments.command](settings, identity, arguments, io)
    except GateRejectedError as error:
        io.log(f"未做任何操作：{error}")
        return EXIT_NOTHING_DONE
    except SummaryMismatchError as error:
        io.log(f"零写入：{error}")
        return EXIT_SUMMARY_MISMATCH
    except BitableError as error:
        io.log(f"未做任何操作：预发表调用失败（{error.code}）。")
        return EXIT_NOTHING_DONE
    except Exception as error:  # 库侧异常：事务已回滚，只报类型不带 DSN 与行值
        io.log(f"未做任何操作：意外失败（{type(error).__name__}），库侧已回滚。")
        return EXIT_NOTHING_DONE


if __name__ == "__main__":
    raise SystemExit(main())
