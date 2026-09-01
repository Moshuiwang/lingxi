#!/usr/bin/env python3
"""Read and verify the GitHub OWNER attestation for an L3 permission PR.

The permission-impact count manifest is intentionally a PR artifact: a PR author
can edit it.  This reader obtains the PR and its ordinary issue comments from the
official GitHub REST API and accepts exactly one immutable comment issued by the
repository owner.  The caller must execute this file from the trusted base commit; the reader
also rejects a PR that changes any workflow or CI trust-root file.

The reader does not call a business system, does not write GitHub state, and never
prints the API token or response bodies.  It emits the existing permission-impact
provenance shape plus a separate non-secret evidence record containing identifiers
and SHA-256 digests only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Hashable, Mapping, Sequence


REPOSITORY_ID = 1_309_889_651
REPOSITORY_FULL_NAME = "Moshuiwang/lingxi"
OWNER_LOGIN = "Moshuiwang"
OWNER_ID = 200_755_707
OWNER_NODE_ID = "U_kgDOC_dJ-w"
OWNER_TYPE = "User"
OWNER_ASSOCIATION = "OWNER"

ATTESTATION_SCHEMA = "lingxi.permission-impact-attestation/v1"
PROVENANCE_SCHEMA = "lingxi.permission-impact-provenance/v2"
PROVENANCE_SOURCE = "github-owner-attestation"
COMMENT_PAGE_SIZE = 100
MAX_COMMENT_PAGES = 100
MAX_CHANGED_FILES = 3_000
MAX_ATTESTATION_TTL_SECONDS = 15 * 60
MAX_BODY_BYTES = 64 * 1024
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")

ATTESTATION_KEYS = frozenset(
    {
        "schema",
        "repository",
        "pull_request",
        "manifest_sha256",
        "base_facts_sha256",
        "head_facts_sha256",
        "grant_surface_sha256",
        "shrink_surface_sha256",
        "counts",
        "exporter",
        "query_version",
        "captured_at",
        "issued_at",
        "expires_at",
        "nonce",
    }
)
REPOSITORY_KEYS = frozenset({"id", "full_name"})
PULL_REQUEST_KEYS = frozenset({"number", "base_sha", "head_sha"})
COUNTS_KEYS = frozenset({"grant", "shrink"})
MANIFEST_KEYS = frozenset(
    {
        "schema",
        "base_facts_sha256",
        "head_facts_sha256",
        "grant_surface_sha256",
        "shrink_surface_sha256",
        "counts",
        "source",
    }
)
MANIFEST_SOURCE_KEYS = frozenset(
    {"kind", "environment", "dataset", "query_version", "captured_at"}
)
EXPORTER_COMMIT_KEYS = frozenset({"commit", "blob"})
EXPORTER_IMAGE_KEYS = frozenset({"image_digest"})
EVIDENCE_KEYS = frozenset(
    {
        "schema",
        "repository",
        "pr_number",
        "pr_mode",
        "base_sha",
        "head_sha",
        "run_id",
        "run_sha",
        "comment",
        "attestation_nonce",
        "manifest_sha256",
        "api_response_sha256",
        "api_response_count",
        "challenge_sha256",
    }
)
EVIDENCE_COMMENT_KEYS = frozenset(
    {"id", "url", "user_id", "user_login", "body", "body_sha256", "created_at"}
)
PROVENANCE_KEYS = frozenset(
    {
        "schema",
        "manifest_sha256",
        "base_facts_sha256",
        "head_facts_sha256",
        "grant_surface_sha256",
        "shrink_surface_sha256",
        "source",
        "registered_at",
        "attestation",
    }
)
PROVENANCE_SOURCE_KEYS = frozenset(
    {"kind", "environment", "dataset", "query_version", "captured_at"}
)
PROVENANCE_ATTESTATION_KEYS = frozenset(
    {
        "comment_id",
        "comment_url",
        "repository",
        "pr_number",
        "base_sha",
        "head_sha",
        "user_id",
        "nonce",
        "body_sha256",
        "response_sha256",
        "run_id",
        "run_sha",
        "pr_mode",
        "challenge_sha256",
    }
)

# A permission PR is allowed to change only the two fact files, the count claim,
# and ordinary documentation/tests.  Every workflow and CI script is a trust root:
# accepting a head-edited reader or consumer would turn this check into self-review.
TRUST_ROOT_PREFIXES = (".github/workflows/", "scripts/ci/")
TRUST_ROOT_FILES = frozenset(
    {
        # The stage exporter is the only non-CI executable whose output is used
        # to form the owner-signed manifest; changing it in the same PR would
        # invalidate the attestation's exporter binding.
        "scripts/ops/export_permission_impact_counts.py",
        "scripts/ops/render_permission_impact_owner_attestation.py",
    }
)

EVIDENCE_ONLY_SENTINEL = ".github/permission-impact-evidence-only"
EVIDENCE_ONLY_SENTINEL_CONTENT = "lingxi.permission-impact-evidence-only/v1\n"
PERMISSION_FACT_FILES = frozenset(
    {
        "src/lingxi/config/galaxy_role_function_map.toml",
        "src/lingxi/config/company_function_metric_map.toml",
    }
)
PERMISSION_CLAIM_FILES = frozenset({".github/permission-impact-counts.json"})
PERMISSION_SUPPORT_FILES = frozenset(
    {
        "docs/traces/502-rc23清仓批/合同.md",
        "docs/traces/502-rc23清仓批/任务表.md",
        "docs/traces/502-rc23清仓批/验收.md",
        "README.md",
        "tests/test_asset_gates.py",
        "tests/test_deploy_checks.py",
        "tests/test_owner_attestation.py",
        "tests/test_owner_attestation_payload.py",
    }
)
PERMISSION_PR_ALLOWLIST = (
    PERMISSION_FACT_FILES
    | PERMISSION_CLAIM_FILES
    | PERMISSION_SUPPORT_FILES
    | {EVIDENCE_ONLY_SENTINEL}
)


class AttestationError(RuntimeError):
    """The attestation or an API response is not safe to consume."""


class ApiError(AttestationError):
    """GitHub could not provide a complete, trusted response."""


@dataclass(frozen=True)
class ApiResponse:
    value: Any
    raw: bytes
    headers: Mapping[str, str]
    url: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never carry a GitHub token to a different host on an API redirect."""

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        redirected = super().redirect_request(request, file_pointer, code, message, headers, new_url)
        if redirected is None:
            return None
        old_host = urllib.parse.urlsplit(request.full_url).netloc
        new_host = urllib.parse.urlsplit(new_url).netloc
        if old_host != new_host:
            redirected.remove_header("Authorization")
        return redirected


class GitHubApi:
    """Small REST client with response digests and fail-closed errors."""

    def __init__(self, token: str, api_url: str = "https://api.github.com") -> None:
        if not token:
            raise ApiError("缺少 GITHUB_TOKEN，不能读取 GitHub OWNER attestation")
        parsed = urllib.parse.urlsplit(api_url)
        if parsed.scheme != "https" or parsed.netloc != "api.github.com" or parsed.path not in {"", "/"}:
            raise ApiError("GitHub API 地址必须是官方 https://api.github.com")
        self._token = token
        self._api_url = api_url.rstrip("/")
        self._opener = urllib.request.build_opener(SafeRedirectHandler())

    def get(self, path: str) -> ApiResponse:
        if not path.startswith("/") or "#" in path:
            raise ApiError("GitHub API 路径异常")
        url = f"{self._api_url}{path}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "lingxi-owner-attestation",
            },
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=30) as response:
                final_host = urllib.parse.urlsplit(response.geturl()).netloc
                if final_host != "api.github.com":
                    raise ApiError("GitHub API 重定向到非官方主机")
                raw = response.read()
                status = getattr(response, "status", 200)
                headers = {str(key): str(value) for key, value in response.headers.items()}
                if status != 200:
                    raise ApiError(f"GitHub API 返回 HTTP {status}")
        except ApiError:
            raise
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as error:
            # Do not include the exception text: HTTP errors can contain a URL or
            # server detail and are not useful evidence for the public summary.
            raise ApiError("GitHub API 请求失败") from error
        try:
            value = json.loads(
                raw.decode("utf-8"),
                parse_constant=_reject_json_constant,
                object_pairs_hook=_reject_duplicate_keys,
            )
        except (UnicodeDecodeError, ValueError) as error:
            raise ApiError("GitHub API 返回了不可解析的 JSON") from error
        return ApiResponse(value=value, raw=raw, headers=headers, url=url)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"JSON 常量 {value} 不受支持")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Do not let a duplicate JSON name hide a different value in a strict body."""

    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"重复 JSON 字段：{key}")
        document[key] = value
    return document


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AttestationError(f"{label} 必须是 JSON 对象")
    return value


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    if set(value) != expected:
        raise AttestationError(f"{label} 字段不完整或含未知字段")


def _non_bool_int(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise AttestationError(f"{label} 必须是非负整数")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise AttestationError(f"{label} 必须是小写 SHA-256 摘要")
    return value


def _sha1(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA1_RE.fullmatch(value) is None:
        raise AttestationError(f"{label} 必须是小写 Git SHA")
    return value


def _timestamp(value: Any, label: str) -> tuple[str, dt.datetime]:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise AttestationError(f"{label} 必须是带时区的 ISO-8601 时间")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise AttestationError(f"{label} 必须是带时区的 ISO-8601 时间") from error
    if parsed.tzinfo is None:
        raise AttestationError(f"{label} 不得省略时区")
    return value, parsed.astimezone(dt.timezone.utc)


def _json_document(raw: str, label: str) -> Mapping[str, Any]:
    if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > MAX_BODY_BYTES:
        raise AttestationError(f"{label} 不是有效的严格 JSON body")
    decoder = json.JSONDecoder(
        parse_constant=_reject_json_constant,
        object_pairs_hook=_reject_duplicate_keys,
    )
    whitespace = " \t\r\n"
    offset = len(raw) - len(raw.lstrip(whitespace))
    candidate = raw[offset:]
    try:
        value, end = decoder.raw_decode(candidate)
    except (ValueError, json.JSONDecodeError) as error:
        raise AttestationError(f"{label} 不是有效的严格 JSON body") from error
    # JSON whitespace around an object is harmless; any Markdown fence or prose
    # after it is deliberately rejected.
    if candidate[end:].strip(whitespace):
        raise AttestationError(f"{label} 不得包含 Markdown 包装或额外文本")
    return _mapping(value, label)


def _canonical_digest(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _load_manifest(path: Path) -> tuple[dict[str, Any], str]:
    if path.is_symlink() or not path.is_file():
        raise AttestationError(f"权限影响面 manifest 缺失或不是普通文件：{path}")
    try:
        raw = path.read_text(encoding="utf-8")
        document = json.loads(
            raw,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise AttestationError("权限影响面 manifest 无法解析") from error
    value = dict(_mapping(document, "权限影响面 manifest"))
    _exact_keys(value, MANIFEST_KEYS, "权限影响面 manifest")
    if value.get("schema") != "lingxi.permission-impact-counts/v2":
        raise AttestationError("权限影响面 manifest schema 不受支持")
    for key in (
        "base_facts_sha256",
        "head_facts_sha256",
        "grant_surface_sha256",
        "shrink_surface_sha256",
    ):
        _sha256(value.get(key), f"manifest {key}")
    counts = _mapping(value.get("counts"), "manifest counts")
    _exact_keys(counts, COUNTS_KEYS, "manifest counts")
    _non_bool_int(counts.get("grant"), "manifest counts.grant")
    _non_bool_int(counts.get("shrink"), "manifest counts.shrink")
    source = _mapping(value.get("source"), "manifest source")
    _exact_keys(source, MANIFEST_SOURCE_KEYS, "manifest source")
    if dict(source)["kind"] != "biai-stage-read-only-aggregate-claim":
        raise AttestationError("非空权限影响面 manifest 必须来自 biai-stage 聚合声明")
    if (source.get("environment"), source.get("dataset"), source.get("query_version")) != (
        "biai-stage",
        "galaxy_user_role",
        "permission-impact-users/v1",
    ):
        raise AttestationError("manifest source 元数据不符合 biai-stage 查询合同")
    _timestamp(source.get("captured_at"), "manifest source.captured_at")
    return value, _canonical_digest(value)


def _validate_exporter(value: Any) -> dict[str, str]:
    exporter = _mapping(value, "attestation exporter")
    keys = frozenset(exporter)
    if keys == EXPORTER_COMMIT_KEYS:
        return {
            "commit": _sha1(exporter.get("commit"), "attestation exporter.commit"),
            "blob": _sha1(exporter.get("blob"), "attestation exporter.blob"),
        }
    if keys == EXPORTER_IMAGE_KEYS:
        digest = exporter.get("image_digest")
        if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise AttestationError("attestation exporter.image_digest 必须是 sha256:<64 hex>")
        return {"image_digest": digest}
    raise AttestationError("attestation exporter 必须是 commit/blob 或 image_digest")


def validate_attestation(
    document: Mapping[str, Any],
    *,
    repository_id: int,
    repository_full_name: str,
    pr_number: int,
    base_sha: str,
    head_sha: str,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    comment_created_at: str,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Validate all owner-supplied bindings without trusting the PR author."""

    _exact_keys(document, ATTESTATION_KEYS, "OWNER attestation")
    if document.get("schema") != ATTESTATION_SCHEMA:
        raise AttestationError(f"OWNER attestation schema 必须是 {ATTESTATION_SCHEMA}")
    repository = _mapping(document.get("repository"), "attestation repository")
    _exact_keys(repository, REPOSITORY_KEYS, "attestation repository")
    if repository.get("id") != repository_id or repository.get("full_name") != repository_full_name:
        raise AttestationError("OWNER attestation repository 不匹配")
    pull_request = _mapping(document.get("pull_request"), "attestation pull_request")
    _exact_keys(pull_request, PULL_REQUEST_KEYS, "attestation pull_request")
    if pull_request.get("number") != pr_number:
        raise AttestationError("OWNER attestation PR 编号不匹配")
    if pull_request.get("base_sha") != base_sha or pull_request.get("head_sha") != head_sha:
        raise AttestationError("OWNER attestation base/head SHA 不匹配")
    _sha1(base_sha, "当前 base SHA")
    _sha1(head_sha, "当前 head SHA")

    if document.get("manifest_sha256") != manifest_sha256:
        raise AttestationError("OWNER attestation manifest 摘要不匹配")
    for key in (
        "manifest_sha256",
        "base_facts_sha256",
        "head_facts_sha256",
        "grant_surface_sha256",
        "shrink_surface_sha256",
    ):
        _sha256(document.get(key), f"attestation {key}")
        if key != "manifest_sha256" and document.get(key) != manifest.get(key):
            raise AttestationError(f"OWNER attestation {key} 不匹配 manifest")
    counts = _mapping(document.get("counts"), "attestation counts")
    _exact_keys(counts, COUNTS_KEYS, "attestation counts")
    manifest_counts = _mapping(manifest.get("counts"), "manifest counts")
    for key in ("grant", "shrink"):
        _non_bool_int(counts.get(key), f"attestation counts.{key}")
        if counts.get(key) != manifest_counts.get(key):
            raise AttestationError(f"OWNER attestation counts.{key} 不匹配 manifest")

    _validate_exporter(document.get("exporter"))
    query_version = document.get("query_version")
    if query_version != "permission-impact-users/v1":
        raise AttestationError("OWNER attestation query_version 不受支持")
    source = _mapping(manifest.get("source"), "manifest source")
    if source.get("query_version") != query_version:
        raise AttestationError("OWNER attestation query_version 未绑定 manifest")
    captured_raw, captured = _timestamp(document.get("captured_at"), "attestation captured_at")
    issued_raw, issued = _timestamp(document.get("issued_at"), "attestation issued_at")
    expires_raw, expires = _timestamp(document.get("expires_at"), "attestation expires_at")
    comment_raw, comment_time = _timestamp(comment_created_at, "GitHub comment created_at")
    source_captured_raw, source_captured = _timestamp(source.get("captured_at"), "manifest source.captured_at")
    if captured_raw != source_captured_raw or captured != source_captured:
        raise AttestationError("OWNER attestation captured_at 未绑定 manifest source")
    if captured > issued:
        raise AttestationError("OWNER attestation captured_at 不得晚于 issued_at")
    # The trusted stage prepares the payload before the owner posts it.  The
    # immutable GitHub creation timestamp therefore follows issued_at; requiring
    # the reverse relation would reject the normal pre-generated-payload flow.
    if issued > comment_time:
        raise AttestationError("OWNER attestation issued_at 晚于评论创建时间")
    current = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    if captured > current or issued > current or comment_time > current:
        raise AttestationError("OWNER attestation 时间不得在未来")
    if expires <= issued or expires <= current:
        raise AttestationError("OWNER attestation 已过期或 expires_at 顺序错误")
    if (expires - issued).total_seconds() > MAX_ATTESTATION_TTL_SECONDS:
        raise AttestationError("OWNER attestation TTL 过长")
    nonce = document.get("nonce")
    if not isinstance(nonce, str) or NONCE_RE.fullmatch(nonce) is None:
        raise AttestationError("OWNER attestation nonce 必须是随机标识")
    return {
        "schema": ATTESTATION_SCHEMA,
        "repository": dict(repository),
        "pull_request": dict(pull_request),
        "manifest_sha256": manifest_sha256,
        "base_facts_sha256": document["base_facts_sha256"],
        "head_facts_sha256": document["head_facts_sha256"],
        "grant_surface_sha256": document["grant_surface_sha256"],
        "shrink_surface_sha256": document["shrink_surface_sha256"],
        "counts": {"grant": counts["grant"], "shrink": counts["shrink"]},
        "exporter": _validate_exporter(document.get("exporter")),
        "query_version": query_version,
        "captured_at": captured_raw,
        "issued_at": issued_raw,
        "expires_at": expires_raw,
        "nonce": nonce,
    }


def _validate_pr_response(
    value: Any,
    *,
    repository_id: int,
    repository_full_name: str,
    pr_number: int,
    base_sha: str,
    head_sha: str,
) -> dict[str, Any]:
    pr = _mapping(value, "GitHub PR response")
    number = pr.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number != pr_number:
        raise AttestationError("GitHub API PR 编号不匹配")
    base = _mapping(pr.get("base"), "GitHub PR base")
    head = _mapping(pr.get("head"), "GitHub PR head")
    base_repo = _mapping(base.get("repo"), "GitHub PR base.repo")
    if (
        not isinstance(base.get("sha"), str)
        or not isinstance(head.get("sha"), str)
        or base.get("sha") != base_sha
        or head.get("sha") != head_sha
    ):
        raise AttestationError("GitHub API PR base/head SHA 已变化")
    if (
        isinstance(base_repo.get("id"), bool)
        or not isinstance(base_repo.get("id"), int)
        or not isinstance(base_repo.get("full_name"), str)
        or base_repo.get("id") != repository_id
        or base_repo.get("full_name") != repository_full_name
    ):
        raise AttestationError("GitHub API PR repository 不匹配")
    changed_files = pr.get("changed_files")
    if (
        isinstance(changed_files, bool)
        or not isinstance(changed_files, int)
        or changed_files < 0
        or changed_files > MAX_CHANGED_FILES
    ):
        raise AttestationError(
            f"GitHub API PR changed_files 必须是 0..{MAX_CHANGED_FILES} 的整数"
        )
    author = _mapping(pr.get("user"), "GitHub PR author")
    for key in ("login", "id", "node_id", "type"):
        if key not in author:
            raise AttestationError("GitHub API PR author 字段缺失")
    if (
        not isinstance(author["login"], str)
        or isinstance(author["id"], bool)
        or not isinstance(author["id"], int)
        or not isinstance(author["node_id"], str)
        or not isinstance(author["type"], str)
    ):
        raise AttestationError("GitHub API PR author 字段类型异常")
    # GitHub user IDs are immutable; reject a PR author that collides with any
    # fixed OWNER identity field instead of relying on a mutable login alone.
    if (
        author.get("login") == OWNER_LOGIN
        or author.get("id") == OWNER_ID
        or author.get("node_id") == OWNER_NODE_ID
    ):
        raise AttestationError("PR 作者不得与 OWNER attestation 信任根相同")
    normalized = dict(pr)
    normalized["changed_files"] = changed_files
    return normalized


def _link_pages(
    headers: Mapping[str, str],
    current_url: str,
    expected_paths: Sequence[str],
    page: int,
) -> int | None:
    allowed_paths = frozenset(expected_paths)
    current = urllib.parse.urlsplit(current_url)
    if (
        current.scheme != "https"
        or current.netloc != "api.github.com"
        or current.path not in allowed_paths
        or current.query != urllib.parse.urlencode({"per_page": COMMENT_PAGE_SIZE, "page": page})
    ):
        raise ApiError("GitHub API 当前分页 URL 异常")
    link = headers.get("Link")
    if not link:
        return None
    relations: dict[str, str] = {}
    for item in link.split(","):
        match = re.fullmatch(r'\s*<([^>]+)>\s*;\s*rel="([^"]+)"\s*', item)
        if match is None or match.group(2) not in {"first", "last", "next", "prev"} or match.group(2) in relations:
            raise ApiError("GitHub API 分页 Link header 异常")
        relations[match.group(2)] = match.group(1)

    def linked_page(relation: str) -> int:
        raw_url = relations[relation]
        try:
            parsed = urllib.parse.urlsplit(raw_url)
        except ValueError as error:
            raise ApiError("GitHub API 分页 URL 无法解析") from error
        if (
            parsed.scheme != "https"
            or parsed.netloc != "api.github.com"
            or parsed.path not in allowed_paths
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ApiError("GitHub API 分页跳到了意外资源")
        try:
            query = urllib.parse.parse_qs(
                parsed.query,
                keep_blank_values=True,
                strict_parsing=True,
            )
        except ValueError as error:
            raise ApiError("GitHub API 分页参数无法解析") from error
        if set(query) != {"per_page", "page"} or any(len(values) != 1 for values in query.values()):
            raise ApiError("GitHub API 分页参数异常")
        if query.get("per_page") != [str(COMMENT_PAGE_SIZE)]:
            raise ApiError("GitHub API 分页 page size 被改变")
        try:
            linked = int(query["page"][0])
        except (KeyError, ValueError, IndexError) as error:
            raise ApiError("GitHub API 分页 page 参数异常") from error
        if linked <= 0:
            raise ApiError("GitHub API 分页 page 必须是正整数")
        return linked

    first_page = linked_page("first") if "first" in relations else None
    last_page = linked_page("last") if "last" in relations else None
    next_page = linked_page("next") if "next" in relations else None
    previous_page = linked_page("prev") if "prev" in relations else None
    if first_page is not None and first_page != 1:
        raise ApiError("GitHub API 分页 first 不是第一页")
    if last_page is not None and last_page < page:
        raise ApiError("GitHub API 分页末页倒退")
    if previous_page is not None and previous_page != page - 1:
        raise ApiError("GitHub API 分页 prev 不是连续页")
    if next_page is not None and next_page != page + 1:
        raise ApiError("GitHub API 分页不是连续页")
    if last_page is not None and next_page is not None and next_page > last_page:
        raise ApiError("GitHub API 分页 next 超过末页")
    if last_page is not None and next_page is None and last_page != page:
        raise ApiError("GitHub API 分页缺少 next")
    return next_page


def _page_url(path: str, page: int) -> str:
    query = urllib.parse.urlencode({"per_page": COMMENT_PAGE_SIZE, "page": page})
    return f"{path}?{query}"


def _id_item_identity(entry: Mapping[str, Any]) -> Hashable:
    """Default identity for endpoints whose official item shape has an ``id``."""

    identifier = entry.get("id")
    if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier <= 0:
        raise AttestationError("GitHub API 条目缺少有效 id")
    return identifier


def _comment_item_identity(entry: Mapping[str, Any]) -> Hashable:
    """Issue comments are uniquely identified by GitHub's immutable numeric id."""

    return _id_item_identity(entry)


def _file_item_identity(entry: Mapping[str, Any]) -> Hashable:
    """Validate the actual ``GET /pulls/{number}/files`` item shape.

    Unlike issue comments, pull-file objects do not have an ``id`` field.  The
    stable endpoint key is the file name, optional rename source, blob SHA and
    status; the caller separately rejects duplicate names so a repeated file
    cannot be hidden behind a changed SHA/status tuple.
    """

    filename = entry.get("filename")
    if not isinstance(filename, str) or not filename:
        raise AttestationError("GitHub PR files 条目缺少 filename")
    previous = entry.get("previous_filename")
    if previous is not None and (not isinstance(previous, str) or not previous):
        raise AttestationError("GitHub PR files 条目的 previous_filename 异常")
    sha = entry.get("sha")
    _sha1(sha, "GitHub PR files 条目的 sha")
    status = entry.get("status")
    if status not in {"added", "modified", "deleted", "renamed", "copied", "changed", "unchanged"}:
        raise AttestationError("GitHub PR files 条目的 status 异常")
    return (filename, previous, sha, status)


def _fetch_pages(
    api: GitHubApi,
    path: str,
    *,
    label: str,
    pagination_paths: Sequence[str] | None = None,
    unique_key: Callable[[Mapping[str, Any]], Hashable] | None = None,
    max_items: int | None = None,
) -> tuple[list[Mapping[str, Any]], list[ApiResponse]]:
    values: list[Mapping[str, Any]] = []
    responses: list[ApiResponse] = []
    seen_keys: set[Hashable] = set()
    page = 1
    while page <= MAX_COMMENT_PAGES:
        response = api.get(_page_url(path, page))
        if not isinstance(response.value, list):
            raise ApiError(f"GitHub API {label} 分页不是数组")
        if len(response.value) > COMMENT_PAGE_SIZE:
            raise ApiError(f"GitHub API {label} 单页超过 page size")
        page_values: list[Mapping[str, Any]] = []
        for item in response.value:
            try:
                entry = _mapping(item, f"GitHub API {label} 条目")
            except AttestationError as error:
                raise ApiError(f"GitHub API {label} 条目不是对象") from error
            try:
                identity = (unique_key or _id_item_identity)(entry)
                hash(identity)
            except (AttestationError, TypeError, ValueError) as error:
                raise ApiError(f"GitHub API {label} 条目唯一键异常") from error
            if identity in seen_keys:
                raise ApiError(f"GitHub API {label} 出现跨页重复唯一键")
            seen_keys.add(identity)
            page_values.append(entry)
        if max_items is not None and len(values) + len(page_values) > max_items:
            raise ApiError(f"GitHub API {label} 条目数超过安全上限 {max_items}")
        values.extend(page_values)
        responses.append(response)
        next_page = _link_pages(
            response.headers,
            f"https://api.github.com{_page_url(path, page)}",
            pagination_paths or (path,),
            page,
        )
        if next_page is not None:
            page = next_page
            continue
        # A full page without a Link header may still have another page.  Probe
        # the next page so a proxy cannot silently truncate exactly 100 entries.
        if len(page_values) == COMMENT_PAGE_SIZE:
            page += 1
            continue
        return values, responses
    raise ApiError(f"GitHub API {label} 分页超过安全上限")


def _validate_comment_shape(comment: Mapping[str, Any]) -> None:
    for key in (
        "body",
        "created_at",
        "updated_at",
        "author_association",
        "user",
        "minimized",
        "performed_via_github_app",
        "url",
        "html_url",
    ):
        if key not in comment:
            raise ApiError("GitHub 普通评论缺少必要字段")
    if (
        not isinstance(comment["body"], str)
        or not isinstance(comment["created_at"], str)
        or not isinstance(comment["updated_at"], str)
        or not isinstance(comment["author_association"], str)
        or not isinstance(comment["url"], str)
        or not isinstance(comment["html_url"], str)
    ):
        raise ApiError("GitHub 普通评论字段类型异常")
    if comment["minimized"] is not None and not isinstance(comment["minimized"], bool):
        raise ApiError("GitHub 普通评论 minimized 字段类型异常")
    app = comment["performed_via_github_app"]
    if app is not None and not isinstance(app, Mapping):
        raise ApiError("GitHub 普通评论 performed_via_github_app 字段类型异常")
    # Validate both timestamps even for ordinary comments. A malformed API page
    # must not become an invisible way to hide a second candidate.
    _timestamp(comment["created_at"], "GitHub comment created_at")
    _timestamp(comment["updated_at"], "GitHub comment updated_at")


def _validate_comment_urls(
    comment: Mapping[str, Any],
    *,
    repository_full_name: str,
    pr_number: int,
) -> None:
    comment_id = comment["id"]
    try:
        api_url = urllib.parse.urlsplit(comment["url"])
        html_url = urllib.parse.urlsplit(comment["html_url"])
    except ValueError as error:
        raise ApiError("GitHub 普通评论 URL 无法解析") from error
    expected_api_path = f"/repos/{repository_full_name}/issues/comments/{comment_id}"
    if (
        api_url.scheme != "https"
        or api_url.netloc != "api.github.com"
        or api_url.path != expected_api_path
        or api_url.query
        or api_url.fragment
    ):
        raise ApiError("GitHub 普通评论 API URL 不匹配")
    expected_html_paths = {
        f"/{repository_full_name}/pull/{pr_number}",
        f"/{repository_full_name}/issues/{pr_number}",
    }
    if (
        html_url.scheme != "https"
        or html_url.netloc != "github.com"
        or html_url.path not in expected_html_paths
        or html_url.query
        or html_url.fragment != f"issuecomment-{comment_id}"
    ):
        raise ApiError("GitHub 普通评论 HTML URL 不匹配")


def _comment_is_attestation_candidate(comment: Mapping[str, Any]) -> bool:
    body = comment.get("body")
    return isinstance(body, str) and ATTESTATION_SCHEMA in body


def _validate_owner(comment: Mapping[str, Any]) -> None:
    user = _mapping(comment.get("user"), "GitHub 评论 user")
    if (
        user.get("login") != OWNER_LOGIN
        or user.get("id") != OWNER_ID
        or user.get("node_id") != OWNER_NODE_ID
        or user.get("type") != OWNER_TYPE
        or comment.get("author_association") != OWNER_ASSOCIATION
    ):
        raise AttestationError("OWNER attestation 评论作者不是固定 GitHub OWNER")
    if comment.get("performed_via_github_app") is not None:
        raise AttestationError("OWNER attestation 不接受 GitHub App 代发评论")


def _response_digest(responses: Sequence[ApiResponse]) -> str:
    digest = hashlib.sha256()
    for response in responses:
        digest.update(response.url.encode("utf-8"))
        digest.update(b"\0")
        digest.update(response.raw)
        digest.update(b"\0")
    return digest.hexdigest()


def _run_challenge_digest(
    *,
    repository: str,
    pr_number: int,
    base_sha: str,
    head_sha: str,
    run_id: int,
    run_sha: str,
    pr_mode: str,
    comment_id: int,
    body_sha256: str,
    api_response_sha256: str,
) -> str:
    """Bind this immutable comment transcript to this trusted Actions run.

    This is a per-run challenge/transcript digest, not a replay ledger.  With no
    new external state or write permission, a future run may still present the
    same immutable comment; the digest makes that limitation explicit and gives
    reviewers a verifiable run-specific binding instead of pretending nonce
    validation is global one-time-use enforcement.
    """

    return _canonical_digest(
        {
            "repository": repository,
            "pr_number": pr_number,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "run_id": run_id,
            "run_sha": run_sha,
            "pr_mode": pr_mode,
            "comment_id": comment_id,
            "body_sha256": body_sha256,
            "api_response_sha256": api_response_sha256,
        }
    )


def _changed_files(
    api: GitHubApi, *, pr_number: int, repository_full_name: str
) -> tuple[set[str], list[ApiResponse], int]:
    encoded_repo = urllib.parse.quote(repository_full_name, safe="/")
    path = f"/repos/{encoded_repo}/pulls/{pr_number}/files"
    pagination_paths = (
        path,
        f"/repositories/{REPOSITORY_ID}/pulls/{pr_number}/files",
    )
    entries, responses = _fetch_pages(
        api,
        path,
        label="PR files",
        pagination_paths=pagination_paths,
        unique_key=_file_item_identity,
        max_items=MAX_CHANGED_FILES,
    )
    paths: set[str] = set()
    for entry in entries:
        filename = entry.get("filename")
        if not isinstance(filename, str) or not filename or filename in paths:
            raise ApiError("GitHub PR files 响应缺少唯一 filename")
        paths.add(filename)
        previous = entry.get("previous_filename")
        if previous is not None:
            if not isinstance(previous, str) or not previous:
                raise ApiError("GitHub PR files previous_filename 异常")
            if previous in paths:
                raise ApiError("GitHub PR files 出现重复 previous_filename")
            paths.add(previous)
    return paths, responses, len(entries)


def _reject_trust_root_changes(paths: set[str]) -> None:
    protected = {
        path
        for path in paths
        if (path.startswith(TRUST_ROOT_PREFIXES) or path in TRUST_ROOT_FILES)
        and path != ".github/permission-impact-counts.json"
    }
    if protected:
        raise AttestationError(
            "同一 PR 不得修改 OWNER attestation/workflow/permission check 信任根："
            + ", ".join(sorted(protected))
        )


def _validate_pr_scope(repository: Path, paths: set[str]) -> str:
    """Return the PR mode after enforcing the pre-declared permission write set.

    A normal L3 PR and an evidence-only PR use the same reader, but they are not
    interchangeable: the latter carries a repository-local sentinel and is
    deliberately non-merge/non-deploy.  The allowlist is intentionally explicit;
    broad ``docs/**`` or ``tests/**`` matching would let runtime or deployment
    configuration hide beside an otherwise valid count claim.
    """

    evidence_only = EVIDENCE_ONLY_SENTINEL in paths
    allowed = PERMISSION_PR_ALLOWLIST if evidence_only else PERMISSION_PR_ALLOWLIST - {
        EVIDENCE_ONLY_SENTINEL
    }
    unexpected = sorted(path for path in paths if path not in allowed)
    if unexpected:
        raise AttestationError(
            "权限影响面 PR 含 allowlist 之外的 runtime/deploy/配置文件："
            + ", ".join(unexpected)
        )
    if not evidence_only:
        return "regular-l3"
    sentinel = repository / EVIDENCE_ONLY_SENTINEL
    if sentinel.is_symlink() or not sentinel.is_file():
        raise AttestationError("evidence-only PR 缺少固定 nonmerge/nondeploy sentinel")
    try:
        content = sentinel.read_bytes()
    except OSError as error:
        raise AttestationError("evidence-only PR sentinel 无法读取") from error
    if content != EVIDENCE_ONLY_SENTINEL_CONTENT.encode("utf-8"):
        raise AttestationError("evidence-only PR sentinel 内容不匹配")
    return "evidence-only"


def _provenance(attestation: Mapping[str, Any], evidence: Mapping[str, Any]) -> dict[str, Any]:
    comment = _mapping(evidence["comment"], "attestation evidence comment")
    return {
        "schema": PROVENANCE_SCHEMA,
        "manifest_sha256": attestation["manifest_sha256"],
        "base_facts_sha256": attestation["base_facts_sha256"],
        "head_facts_sha256": attestation["head_facts_sha256"],
        "grant_surface_sha256": attestation["grant_surface_sha256"],
        "shrink_surface_sha256": attestation["shrink_surface_sha256"],
        "source": {
            "kind": PROVENANCE_SOURCE,
            "environment": "biai-stage",
            "dataset": "galaxy_user_role",
            "query_version": attestation["query_version"],
            "captured_at": attestation["captured_at"],
        },
        "registered_at": attestation["issued_at"],
        "attestation": {
            "comment_id": comment["id"],
            "comment_url": comment["url"],
            "repository": evidence["repository"],
            "pr_number": evidence["pr_number"],
            "base_sha": evidence["base_sha"],
            "head_sha": evidence["head_sha"],
            "user_id": comment["user_id"],
            "nonce": evidence["attestation_nonce"],
            "body_sha256": comment["body_sha256"],
            "response_sha256": evidence["api_response_sha256"],
            "run_id": evidence["run_id"],
            "run_sha": evidence["run_sha"],
            "pr_mode": evidence["pr_mode"],
            "challenge_sha256": evidence["challenge_sha256"],
        },
    }


def read_attestation(
    api: GitHubApi,
    *,
    repository: Path,
    repository_id: int = REPOSITORY_ID,
    repository_full_name: str = REPOSITORY_FULL_NAME,
    pr_number: int,
    base_sha: str,
    head_sha: str,
    manifest_path: Path,
    run_id: int,
    run_sha: str,
    now: dt.datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return attestation, provenance and public evidence for one PR."""

    if repository_id != REPOSITORY_ID or repository_full_name != REPOSITORY_FULL_NAME:
        raise AttestationError("OWNER attestation repository trust root 不可覆盖")
    _sha1(base_sha, "当前 base SHA")
    _sha1(head_sha, "当前 head SHA")
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0:
        raise AttestationError("PR 编号必须是正整数")
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
        raise AttestationError("GitHub Actions run id 必须是正整数")
    _sha1(run_sha, "GitHub Actions run SHA")
    try:
        repository_root = repository.resolve(strict=True)
        expected_manifest = (repository_root / ".github" / "permission-impact-counts.json").resolve(
            strict=True
        )
        actual_manifest = manifest_path.resolve(strict=True)
    except OSError as error:
        raise AttestationError("PR 工作树或权限影响面 manifest 不存在") from error
    if not repository_root.is_dir() or actual_manifest != expected_manifest or manifest_path.is_symlink():
        raise AttestationError("权限影响面 manifest 必须位于 PR 工作树的固定路径")

    encoded_repo = urllib.parse.quote(repository_full_name, safe="/")
    pr_path = f"/repos/{encoded_repo}/pulls/{pr_number}"
    pr_response = api.get(pr_path)
    pr = _validate_pr_response(
        pr_response.value,
        repository_id=repository_id,
        repository_full_name=repository_full_name,
        pr_number=pr_number,
        base_sha=base_sha,
        head_sha=head_sha,
    )
    manifest, manifest_sha256 = _load_manifest(manifest_path)

    changed, file_responses, changed_file_count = _changed_files(
        api, pr_number=pr_number, repository_full_name=repository_full_name
    )
    if changed_file_count != pr["changed_files"]:
        raise AttestationError(
            "GitHub PR changed_files 与分页枚举数量不一致；可能是截断或响应不完整"
        )
    _reject_trust_root_changes(changed)
    pr_mode = _validate_pr_scope(repository, changed)

    comment_path = f"/repos/{encoded_repo}/issues/{pr_number}/comments"
    comment_pagination_paths = (
        comment_path,
        f"/repositories/{REPOSITORY_ID}/issues/{pr_number}/comments",
    )
    comments, comment_responses = _fetch_pages(
        api,
        comment_path,
        label="PR ordinary comments",
        pagination_paths=comment_pagination_paths,
        unique_key=_comment_item_identity,
    )
    valid: list[tuple[Mapping[str, Any], dict[str, Any], str]] = []
    for comment in comments:
        _validate_comment_shape(comment)
        _validate_comment_urls(
            comment,
            repository_full_name=repository_full_name,
            pr_number=pr_number,
        )
        if not _comment_is_attestation_candidate(comment):
            continue
        _validate_owner(comment)
        if comment["created_at"] != comment["updated_at"]:
            raise AttestationError("OWNER attestation 评论已被编辑")
        if comment["minimized"] is not False and comment["minimized"] is not None:
            raise AttestationError("OWNER attestation 评论已被 minimized")
        body = comment["body"]
        document = dict(_json_document(body, "OWNER attestation comment body"))
        attestation = validate_attestation(
            document,
            repository_id=repository_id,
            repository_full_name=repository_full_name,
            pr_number=pr_number,
            base_sha=base_sha,
            head_sha=head_sha,
            manifest=manifest,
            manifest_sha256=manifest_sha256,
            comment_created_at=comment["created_at"],
            now=now,
        )
        body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
        valid.append((comment, attestation, body_sha256))
    if len(valid) != 1:
        raise AttestationError(f"有效 OWNER attestation 数量为 {len(valid)}，要求恰好 1")

    comment, attestation, body_sha256 = valid[0]
    comment_url = comment.get("html_url") or comment.get("url")
    if not isinstance(comment_url, str) or not comment_url.startswith("https://github.com/"):
        raise AttestationError("OWNER attestation 评论缺少可信 URL")
    response_sha256 = _response_digest([pr_response, *file_responses, *comment_responses])
    evidence = {
        "schema": "lingxi.permission-impact-attestation-evidence/v1",
        "repository": repository_full_name,
        "pr_number": pr_number,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "pr_mode": pr_mode,
        "run_id": run_id,
        "run_sha": run_sha,
        "comment": {
            "id": comment["id"],
            "url": comment_url,
            "user_id": OWNER_ID,
            "user_login": OWNER_LOGIN,
            "body": body,
            "body_sha256": body_sha256,
            "created_at": comment["created_at"],
        },
        "attestation_nonce": attestation["nonce"],
        "manifest_sha256": manifest_sha256,
        "api_response_sha256": response_sha256,
        "api_response_count": len([pr_response, *file_responses, *comment_responses]),
    }
    evidence["challenge_sha256"] = _run_challenge_digest(
        repository=repository_full_name,
        pr_number=pr_number,
        base_sha=base_sha,
        head_sha=head_sha,
        run_id=run_id,
        run_sha=run_sha,
        pr_mode=pr_mode,
        comment_id=comment["id"],
        body_sha256=body_sha256,
        api_response_sha256=response_sha256,
    )
    provenance = _provenance(attestation, evidence)
    return attestation, provenance, evidence


def _write_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--repository-id", type=int, default=REPOSITORY_ID)
    parser.add_argument("--repository-name", default=REPOSITORY_FULL_NAME)
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--run-sha", required=True)
    parser.add_argument("--provenance-output", type=Path, required=True)
    parser.add_argument("--evidence-output", type=Path, required=True)
    parser.add_argument("--token-env", default="GITHUB_TOKEN")
    args = parser.parse_args()
    token = os.environ.get(args.token_env)
    try:
        _, provenance, evidence = read_attestation(
            GitHubApi(token or ""),
            repository=args.repository,
            repository_id=args.repository_id,
            repository_full_name=args.repository_name,
            pr_number=args.pr_number,
            base_sha=args.base_sha,
            head_sha=args.head_sha,
            manifest_path=args.manifest,
            run_id=args.run_id,
            run_sha=args.run_sha,
        )
        _write_json(args.provenance_output, provenance)
        _write_json(args.evidence_output, evidence)
    except (AttestationError, ValueError, OSError) as error:
        print(f"GitHub OWNER attestation 失败关闭：{error}", file=sys.stderr)
        return 1
    print(
        "GitHub OWNER attestation：通过 "
        f"PR #{args.pr_number} run={args.run_id} "
        f"comment_id={evidence['comment']['id']} user_id={evidence['comment']['user_id']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
