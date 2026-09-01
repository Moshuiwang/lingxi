#!/usr/bin/env python3
"""L3 权限事实配置影响面门禁（Issue #498）。

只读取两个 Git ref 中的两份 TOML 配置，不连接数据库、不读取生产快照、不需要凭据。
脚本把角色→职能与公司→职能→指标的有效笛卡尔面做集合差，报告新增授予面与收缩面
两栏；受影响用户数量必须来自绑定两份权限事实内容、影响面摘要和来源时间的纯计数证明。
权限面为空时才由 Git diff 严格推出 0；权限面非空而没有经编排者在仓库外登记的
biai-stage 只读聚合证明会失败关闭，绝不拿角色数、配置行数或任何内部 ID 冒充用户数。

PR 内的计数清单只能是 stage 导出声明（claim），不是可信的 stage 证据。可信链路需要一
份不在 PR 工作树中的 hash registration；当前仓库没有受保护 stage job/artifact/attestation
来自动提供它，所以缺 registration 时必须明确报告 PM 门，而不是把自报来源升格。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ROLE_MAP_PATH = "src/lingxi/config/galaxy_role_function_map.toml"
METRIC_MAP_PATH = "src/lingxi/config/company_function_metric_map.toml"
REPORT_SCHEMA = "lingxi.permission-impact/v1"
COUNT_SCHEMA = "lingxi.permission-impact-counts/v2"
PROVENANCE_SCHEMA = "lingxi.permission-impact-provenance/v1"
EMPTY_COUNT_SOURCE = "derived-static-empty"
# 这个值故意标明它只是 PR 可携带的声明；不能把它当作受保护 stage 证据。
STAGE_COUNT_CLAIM_SOURCE = "biai-stage-read-only-aggregate-claim"
# 旧的 Python 调用方可继续引用该名字，但值已降级为 claim，避免旧代码误称可信来源。
STAGE_COUNT_SOURCE = STAGE_COUNT_CLAIM_SOURCE
STAGE_PROVENANCE_SOURCE = "out-of-band-biai-stage-hash-registration"
COUNT_SOURCE_KEYS = frozenset(
    {"kind", "environment", "dataset", "query_version", "captured_at"}
)
COUNT_MANIFEST_KEYS = frozenset(
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
    }
)
COUNT_KEYS = frozenset({"grant", "shrink"})
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

SurfaceEntry = tuple[str, str, str, str]


class ConfigShapeError(ValueError):
    """权限事实配置形状不符合生产解析器的 fail-closed 合同。"""


class CountEvidenceError(ValueError):
    """权限影响用户计数缺少可审计、绑定候选的证据。"""


def _parse_toml(raw: bytes | None, ref: str, path: str) -> Mapping[str, Any]:
    if raw is None:
        raise ConfigShapeError(f"{ref}:{path} 不存在；L3 权限配置不能静默按空文件处理")
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigShapeError(f"{ref}:{path} 无法解析：{error}") from error
    if not isinstance(document, Mapping):
        raise ConfigShapeError(f"{ref}:{path} 顶层必须是表")
    return document


def _role_map(document: Mapping[str, Any], label: str) -> dict[str, str]:
    roles = document.get("roles")
    if not isinstance(roles, Mapping):
        raise ConfigShapeError(f"{label} 缺少 [roles] 表")
    result: dict[str, str] = {}
    for raw_role, raw_function in roles.items():
        role = raw_role.strip() if isinstance(raw_role, str) else ""
        function = raw_function.strip() if isinstance(raw_function, str) else ""
        if not role:
            raise ConfigShapeError(f"{label} 存在空的角色名")
        if not function:
            raise ConfigShapeError(f"{label} 的职能必须是非空文本：{role}")
        if role in result:
            raise ConfigShapeError(f"{label} 存在归一化后重复的角色名：{role}")
        result[role] = function
    return result


def _metric_map(
    document: Mapping[str, Any], label: str
) -> dict[str, dict[str, tuple[str, ...]]]:
    companies = document.get("companies")
    if not isinstance(companies, Mapping):
        raise ConfigShapeError(f"{label} 缺少 [companies] 表")
    result: dict[str, dict[str, tuple[str, ...]]] = {}
    for raw_company, raw_functions in companies.items():
        company = raw_company.strip() if isinstance(raw_company, str) else ""
        if not company:
            raise ConfigShapeError(f"{label} 存在空的公司键")
        if company in result:
            raise ConfigShapeError(f"{label} 存在归一化后重复的公司键：{company}")
        if not isinstance(raw_functions, Mapping):
            raise ConfigShapeError(f"{label} 公司 {company} 下的职能表必须是映射")
        functions: dict[str, tuple[str, ...]] = {}
        for raw_function, raw_metrics in raw_functions.items():
            function = raw_function.strip() if isinstance(raw_function, str) else ""
            if not function:
                raise ConfigShapeError(f"{label} 公司 {company} 下存在空的职能标签")
            if function in functions:
                raise ConfigShapeError(
                    f"{label} 公司 {company} 下存在归一化后重复的职能标签：{function}"
                )
            if not isinstance(raw_metrics, (list, tuple)) or not raw_metrics:
                raise ConfigShapeError(
                    f"{label} 公司 {company} 职能 {function} 的指标列表必须非空"
                )
            metrics: list[str] = []
            for metric in raw_metrics:
                if not isinstance(metric, str) or not metric:
                    raise ConfigShapeError(
                        f"{label} 公司 {company} 职能 {function} 的指标必须是非空文本"
                    )
                metrics.append(metric)
            functions[function] = tuple(metrics)
        result[company] = functions
    return result


def build_surface(
    role_map: Mapping[str, str],
    metric_map: Mapping[str, Mapping[str, Sequence[str]]],
) -> set[SurfaceEntry]:
    """把两份事实配置展开成可比较的 role×function×company×metric 集合。"""

    return {
        (role, function, company, metric)
        for role, function in role_map.items()
        for company, functions in metric_map.items()
        for metric in functions.get(function, ())
    }


def _surface_rows(entries: set[SurfaceEntry]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], set[str]] = {}
    for role, function, company, metric in entries:
        grouped.setdefault((role, function, company), set()).add(metric)
    return [
        {"role": role, "function": function, "company": company, "metrics": sorted(metrics)}
        for (role, function, company), metrics in sorted(grouped.items())
    ]


def _role_changes(before: Mapping[str, str], after: Mapping[str, str]) -> list[dict[str, Any]]:
    return [
        {
            "role": role,
            "before_function": before.get(role),
            "after_function": after.get(role),
        }
        for role in sorted(set(before) | set(after))
        if before.get(role) != after.get(role)
    ]


def _metric_changes(
    before: Mapping[str, Mapping[str, Sequence[str]]],
    after: Mapping[str, Mapping[str, Sequence[str]]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for company in sorted(set(before) | set(after)):
        before_functions = before.get(company, {})
        after_functions = after.get(company, {})
        for function in sorted(set(before_functions) | set(after_functions)):
            old = sorted(set(before_functions.get(function, ())))
            new = sorted(set(after_functions.get(function, ())))
            if old != new:
                rows.append(
                    {
                        "company": company,
                        "function": function,
                        "before_metrics": old,
                        "after_metrics": new,
                    }
                )
    return rows


def _facts_digest(role_raw: bytes, metric_raw: bytes) -> str:
    """为两份权限事实计算不泄露内容的绑定摘要。"""

    digest = hashlib.sha256()
    for path, raw in ((ROLE_MAP_PATH, role_raw), (METRIC_MAP_PATH, metric_raw)):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(raw)
        digest.update(b"\0")
    return digest.hexdigest()


def _surface_digest(entries: set[SurfaceEntry]) -> str:
    """为一个 grant/shrink 集合计算稳定摘要，不把用户事实带进报告。"""

    encoded = json.dumps(
        sorted(entries), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_json_bytes(document: Mapping[str, Any]) -> bytes:
    """为跨进程 hash registration 生成不受缩进/键序影响的 JSON 表示。"""

    return json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _document_digest(document: Mapping[str, Any]) -> str:
    """计算清单语义摘要；manifest 加入 Git 提交不会改变这份摘要。"""

    return hashlib.sha256(_canonical_json_bytes(document)).hexdigest()


def _validate_count_value(value: Any, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CountEvidenceError(
            f"用户数量 {key} 必须是非负整数；不接受用户 ID 或其他明细"
        )
    return value


def _validate_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise CountEvidenceError("计数来源 captured_at 必须是带时区的 ISO-8601 时间")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CountEvidenceError(
            "计数来源 captured_at 必须是带时区的 ISO-8601 时间"
        ) from error
    if parsed.tzinfo is None:
        raise CountEvidenceError("计数来源 captured_at 不得省略时区")
    return value


def _validate_count_manifest(
    manifest: Mapping[str, Any],
    *,
    expected_base_facts_sha256: str,
    expected_head_facts_sha256: str,
    expected_grant_surface_sha256: str,
    expected_shrink_surface_sha256: str,
    # 保留旧调用方的关键字参数，但刻意不再读取或比较它们。把 head SHA 写入随 PR
    # 提交的 manifest 会形成自引用：提交 manifest 后 head SHA 必然变化，非空 diff
    # 永远无法闭合。可信绑定改用两份权限事实内容摘要和 grant/shrink 面摘要。
    expected_base_ref: str | None = None,
    expected_head_ref: str | None = None,
) -> dict[str, Any]:
    """校验 stage 计数声明的形状与事实绑定，拒绝未知字段以避免夹带行级资料。"""

    del expected_base_ref, expected_head_ref

    if set(manifest) != COUNT_MANIFEST_KEYS:
        raise CountEvidenceError(
            "用户数量证明字段不完整或含未知字段；只接受绑定候选的纯计数清单"
        )
    if manifest.get("schema") != COUNT_SCHEMA:
        raise CountEvidenceError(f"用户数量证明 schema 必须是 {COUNT_SCHEMA}")
    for key, expected in (
        ("base_facts_sha256", expected_base_facts_sha256),
        ("head_facts_sha256", expected_head_facts_sha256),
        ("grant_surface_sha256", expected_grant_surface_sha256),
        ("shrink_surface_sha256", expected_shrink_surface_sha256),
    ):
        value = manifest.get(key)
        if (
            not isinstance(value, str)
            or value != expected
            or (key.endswith("sha256") and HEX64_RE.fullmatch(value) is None)
        ):
            raise CountEvidenceError(f"用户数量证明 {key} 未绑定当前候选事实")

    counts = manifest.get("counts")
    if not isinstance(counts, Mapping) or set(counts) != COUNT_KEYS:
        raise CountEvidenceError("用户数量证明 counts 只能包含 grant 与 shrink")
    normalized_counts = {
        key: _validate_count_value(counts[key], key) for key in ("grant", "shrink")
    }

    source = manifest.get("source")
    if not isinstance(source, Mapping) or set(source) != COUNT_SOURCE_KEYS:
        raise CountEvidenceError(
            "用户数量证明 source 字段不完整或含未知字段；不得携带行级资料"
        )
    kind = source.get("kind")
    environment = source.get("environment")
    dataset = source.get("dataset")
    query_version = source.get("query_version")
    captured_at = _validate_timestamp(source.get("captured_at"))
    if kind == EMPTY_COUNT_SOURCE:
        expected_source = (
            "repository",
            "permission-facts",
            "static-diff/v1",
        )
    elif kind == STAGE_COUNT_CLAIM_SOURCE:
        expected_source = (
            "biai-stage",
            "galaxy_user_role",
            "permission-impact-users/v1",
        )
    else:
        raise CountEvidenceError(
            "用户数量证明 source.kind 只能来自空 diff 推导或 biai-stage 只读聚合声明"
        )
    if (environment, dataset, query_version) != expected_source:
        raise CountEvidenceError("用户数量证明来源元数据与 kind 不一致")

    return {
        "grant": normalized_counts["grant"],
        "shrink": normalized_counts["shrink"],
        "status": (
            "unverified-claim"
            if kind == STAGE_COUNT_CLAIM_SOURCE
            else "derived"
        ),
        "source": {
            "kind": kind,
            "environment": environment,
            "dataset": dataset,
            "query_version": query_version,
            "captured_at": captured_at,
            "provenance": (
                "unverified-stage-claim"
                if kind == STAGE_COUNT_CLAIM_SOURCE
                else "repository-derived"
            ),
        },
    }


def _validate_stage_provenance(
    provenance: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    expected_base_facts_sha256: str,
    expected_head_facts_sha256: str,
    expected_grant_surface_sha256: str,
    expected_shrink_surface_sha256: str,
) -> dict[str, Any]:
    """验证仓库外的 stage hash registration，不把 PR 自报当成可信来源。

    这里的 registration 是编排者在 biai-stage 完成只读聚合后保存的最小绑定材料，
    不是由当前仓库声明的 GitHub/OIDC 签名 attestation。调用方必须把它从 PR 工作树
    外部注入；当前没有受保护 stage workflow，所以普通 CI 缺少该文件时应失败关闭。
    """

    if set(provenance) != PROVENANCE_KEYS:
        raise CountEvidenceError(
            "stage provenance registration 字段不完整或含未知字段；"
            "不能把 PR 自报升级为可信证据"
        )
    if provenance.get("schema") != PROVENANCE_SCHEMA:
        raise CountEvidenceError(
            f"stage provenance registration schema 必须是 {PROVENANCE_SCHEMA}"
        )

    expected_bindings = {
        "manifest_sha256": _document_digest(manifest),
        "base_facts_sha256": expected_base_facts_sha256,
        "head_facts_sha256": expected_head_facts_sha256,
        "grant_surface_sha256": expected_grant_surface_sha256,
        "shrink_surface_sha256": expected_shrink_surface_sha256,
    }
    for key, expected in expected_bindings.items():
        value = provenance.get(key)
        if not isinstance(value, str) or value != expected or HEX64_RE.fullmatch(value) is None:
            raise CountEvidenceError(f"stage provenance registration {key} 未绑定当前事实")

    manifest_source = manifest.get("source")
    if (
        not isinstance(manifest_source, Mapping)
        or manifest_source.get("kind") != STAGE_COUNT_CLAIM_SOURCE
    ):
        raise CountEvidenceError(
            "stage provenance registration 只能绑定 biai-stage 聚合声明，"
            "不能把其他来源伪装成 stage 证据"
        )

    source = provenance.get("source")
    if not isinstance(source, Mapping) or set(source) != COUNT_SOURCE_KEYS:
        raise CountEvidenceError(
            "stage provenance registration source 字段不完整或含未知字段"
        )
    if source.get("kind") != STAGE_PROVENANCE_SOURCE:
        raise CountEvidenceError(
            "stage provenance registration source.kind 必须是仓库外 hash registration"
        )
    expected_source = {
        "kind": STAGE_PROVENANCE_SOURCE,
        "environment": "biai-stage",
        "dataset": "galaxy_user_role",
        "query_version": "permission-impact-users/v1",
        "captured_at": manifest_source.get("captured_at"),
    }
    if dict(source) != expected_source:
        raise CountEvidenceError(
            "stage provenance registration 来源元数据与 stage 聚合声明不一致"
        )
    registered_at = _validate_timestamp(provenance.get("registered_at"))

    return {
        "kind": STAGE_PROVENANCE_SOURCE,
        "environment": source["environment"],
        "dataset": source["dataset"],
        "query_version": source["query_version"],
        "captured_at": source["captured_at"],
        "registered_at": registered_at,
        "status": "out-of-band-hash-registered",
    }


def _validate_user_counts(
    user_counts: Mapping[str, Any] | None,
    *,
    grant_surface: set[SurfaceEntry],
    shrink_surface: set[SurfaceEntry],
    expected_base_ref: str | None = None,
    expected_head_ref: str | None = None,
    expected_base_facts_sha256: str | None = None,
    expected_head_facts_sha256: str | None = None,
    strict_manifest: bool = False,
) -> dict[str, Any]:
    """返回纯计数及来源；权限变化没有可审计计数时必须失败关闭。"""

    if user_counts is None:
        if grant_surface or shrink_surface:
            raise CountEvidenceError(
                "权限事实发生变化但没有计数证明；请从 biai-stage 生成纯聚合清单，"
                "CI 不读取业务凭据或调用公司系统"
            )
        # 空 diff 是 Git 事实，不是猜测的 0；将其绑定到当前事实摘要以便审计回读。
        return {
            "grant": 0,
            "shrink": 0,
            "status": "derived",
            "source": {
                "kind": EMPTY_COUNT_SOURCE,
                "environment": "repository",
                "dataset": "permission-facts",
                "query_version": "static-diff/v1",
                "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            },
        }

    if not isinstance(user_counts, Mapping):
        raise CountEvidenceError("用户数量证明必须是 JSON 对象")

    # 保留 build_report 的小型纯函数调用兼容性；真实 CLI 使用 strict_manifest，
    # 不接受没有来源/时间/候选绑定的裸 grant/shrink 数字。
    if set(user_counts) == COUNT_KEYS:
        if strict_manifest:
            raise CountEvidenceError(
                "CI 不接受未绑定来源/时间/候选的裸 grant/shrink 数字"
            )
        return {
            "grant": _validate_count_value(user_counts["grant"], "grant"),
            "shrink": _validate_count_value(user_counts["shrink"], "shrink"),
            "status": "provided",
            "source": {
                "kind": "test-explicit",
                "environment": "test",
                "dataset": "pure-counts",
                "query_version": "test/v1",
                "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            },
        }

    if not strict_manifest:
        raise CountEvidenceError(
            "用户数量输入必须是 grant/shrink 纯计数，或完整的候选绑定证明"
        )
    # Ref 参数保留给旧的纯函数调用方，但不再是严格证明的绑定项；把它们写入
    # 随 PR 提交的 manifest 会重新引入 head 自引用。事实内容和 surface digest
    # 才是 manifest 与候选之间的稳定绑定。
    del expected_base_ref, expected_head_ref
    if None in (expected_base_facts_sha256, expected_head_facts_sha256):
        raise CountEvidenceError("严格计数证明校验缺少候选绑定事实")
    return _validate_count_manifest(
        user_counts,
        expected_base_facts_sha256=expected_base_facts_sha256,
        expected_head_facts_sha256=expected_head_facts_sha256,
        expected_grant_surface_sha256=_surface_digest(grant_surface),
        expected_shrink_surface_sha256=_surface_digest(shrink_surface),
    )


def build_report(
    base_role_document: Mapping[str, Any],
    head_role_document: Mapping[str, Any],
    base_metric_document: Mapping[str, Any],
    head_metric_document: Mapping[str, Any],
    *,
    user_counts: Mapping[str, Any] | None = None,
    base_facts_sha256: str | None = None,
    head_facts_sha256: str | None = None,
    base_ref: str | None = None,
    head_ref: str | None = None,
    strict_count_manifest: bool = False,
) -> dict[str, Any]:
    """构造可序列化的影响面报告；不包含用户明细。"""

    base_roles = _role_map(base_role_document, "base 角色映射")
    head_roles = _role_map(head_role_document, "head 角色映射")
    base_metrics = _metric_map(base_metric_document, "base 公司指标映射")
    head_metrics = _metric_map(head_metric_document, "head 公司指标映射")
    base_surface = build_surface(base_roles, base_metrics)
    head_surface = build_surface(head_roles, head_metrics)
    grant_surface = head_surface - base_surface
    shrink_surface = base_surface - head_surface
    counts = _validate_user_counts(
        user_counts,
        grant_surface=grant_surface,
        shrink_surface=shrink_surface,
        expected_base_ref=base_ref,
        expected_head_ref=head_ref,
        expected_base_facts_sha256=base_facts_sha256,
        expected_head_facts_sha256=head_facts_sha256,
        strict_manifest=strict_count_manifest,
    )
    return {
        "schema": REPORT_SCHEMA,
        "grant": _surface_rows(grant_surface),
        "shrink": _surface_rows(shrink_surface),
        "grant_entry_count": len(grant_surface),
        "shrink_entry_count": len(shrink_surface),
        "role_mapping_changes": _role_changes(base_roles, head_roles),
        "metric_mapping_changes": _metric_changes(base_metrics, head_metrics),
        "permission_facts": {
            "base_ref": base_ref,
            "head_ref": head_ref,
            "base_sha256": base_facts_sha256,
            "head_sha256": head_facts_sha256,
        },
        "affected_user_counts": {
            "grant": counts["grant"],
            "shrink": counts["shrink"],
            "status": counts["status"],
            "source": counts["source"],
        },
    }


def _git_file(repository: Path, ref: str, path: str) -> bytes | None:
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=repository,
        check=False,
        capture_output=True,
    )
    if result.returncode == 0:
        return result.stdout
    # Missing base files can be represented as an empty initial mapping only for a newly
    # introduced file; for head a missing file is rejected by _parse_toml below. Keeping
    # this distinction in the caller prevents a deleted permission fact source from being
    # misread as a legitimate zero-surface configuration.
    if b"exists on disk, but not in" in result.stderr or b"does not exist" in result.stderr:
        return None
    raise RuntimeError(
        f"git show 读取 {ref}:{path} 失败：{result.stderr.decode('utf-8', 'replace').strip()}"
    )


def _load_ref_documents(repository: Path, ref: str) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    role_document, metric_document, _, _ = _load_ref_documents_with_raw(repository, ref)
    return role_document, metric_document


def _load_ref_documents_with_raw(
    repository: Path, ref: str
) -> tuple[Mapping[str, Any], Mapping[str, Any], bytes, bytes]:
    role_raw = _git_file(repository, ref, ROLE_MAP_PATH)
    metric_raw = _git_file(repository, ref, METRIC_MAP_PATH)
    # A missing file is never treated as an empty permission source. This also makes a
    # delete/rename fail closed instead of turning a shrink into an apparently harmless diff.
    return (
        _parse_toml(role_raw, ref, ROLE_MAP_PATH),
        _parse_toml(metric_raw, ref, METRIC_MAP_PATH),
        role_raw,
        metric_raw,
    )


def _load_json_document(path: Path) -> dict[str, Any]:
    """读取普通 JSON 文件；拒绝符号链接，避免把 runner 外文件悄悄带入门禁。"""

    if path.is_symlink() or not path.is_file():
        raise ValueError(f"JSON 证明必须是普通文件，不接受符号链接或缺失路径：{path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"JSON 证明无法读取或解析：{path}（{error}）") from error
    if not isinstance(value, dict):
        raise ValueError(f"JSON 证明必须是对象：{path}")
    return value


def _load_external_provenance(path: Path, *, repository: Path) -> dict[str, Any]:
    """只接受仓库外的 registration，阻止 PR 自己提交一份“可信”证明。"""

    if path.is_symlink():
        raise CountEvidenceError(
            "stage provenance registration 不得是符号链接；需由受控 stage 从仓库外注入"
        )
    try:
        resolved = path.resolve(strict=True)
        repository_root = repository.resolve(strict=True)
    except OSError as error:
        raise CountEvidenceError(
            "stage provenance registration 路径无法解析；需由受控 stage 从仓库外注入"
        ) from error
    if resolved == repository_root or repository_root in resolved.parents:
        raise CountEvidenceError(
            "stage provenance registration 不能位于 PR 工作树内；"
            "当前 workflow 没有受保护 stage artifact，需经 PM 门补齐注入"
        )
    return _load_json_document(path)


def _load_user_counts(path: Path) -> Mapping[str, Any]:
    return _load_json_document(path)


def render_report(report: Mapping[str, Any]) -> str:
    """渲染公开门禁摘要；只展示权限事实与数量，不展示用户明细。"""

    lines = [
        "L3 权限影响面 diff：通过（权限事实静态；stage 数量需另有仓库外 hash registration）"
    ]
    for label, key, count_key in (
        ("新增授予面（grant）", "grant", "grant_entry_count"),
        ("收缩面（shrink）", "shrink", "shrink_entry_count"),
    ):
        rows = report[key]
        lines.append(f"{label}：{report[count_key]} 条 role×function×company×metric")
        for row in rows:
            metrics = ", ".join(row["metrics"])
            lines.append(
                f"  role={row['role']!r} function={row['function']!r} "
                f"company={row['company']!r} metrics=[{metrics}]"
            )
    counts = report["affected_user_counts"]
    lines.append("受影响用户数量（仅数量，不含内部 ID）：")
    for label, key in (("新增授予面", "grant"), ("收缩面", "shrink")):
        value = counts[key]
        lines.append(f"  {label}={value}")
    source = counts["source"]
    lines.append(
        "  来源："
        f"{source['kind']} environment={source['environment']} "
        f"dataset={source['dataset']} query_version={source['query_version']} "
        f"captured_at={source['captured_at']}"
    )
    if "provenance" in source:
        lines.append(f"  provenance={source['provenance']}")
    if "registered_at" in source:
        lines.append(f"  registered_at={source['registered_at']}")
    return "\n".join(lines)


def run_check(
    base_ref: str,
    head_ref: str,
    *,
    repository: Path = REPOSITORY_ROOT,
    user_counts_path: Path | None = None,
    trusted_provenance_path: Path | None = None,
    output: Path | None = None,
) -> int:
    base_roles, base_metrics, base_role_raw, base_metric_raw = _load_ref_documents_with_raw(
        repository, base_ref
    )
    head_roles, head_metrics, head_role_raw, head_metric_raw = _load_ref_documents_with_raw(
        repository, head_ref
    )
    base_facts_sha256 = _facts_digest(base_role_raw, base_metric_raw)
    head_facts_sha256 = _facts_digest(head_role_raw, head_metric_raw)
    counts = _load_user_counts(user_counts_path) if user_counts_path is not None else None
    base_surface = build_surface(
        _role_map(base_roles, "base 角色映射"),
        _metric_map(base_metrics, "base 公司指标映射"),
    )
    head_surface = build_surface(
        _role_map(head_roles, "head 角色映射"),
        _metric_map(head_metrics, "head 公司指标映射"),
    )
    grant_surface = head_surface - base_surface
    shrink_surface = base_surface - head_surface

    # 先验证数量清单的事实绑定，再验证仓库外的 stage registration。这样即便 PR
    # 提交了一个看似完整的 stage JSON，缺少受控 stage 的 hash 登记也不会被 CI 采信。
    provenance: dict[str, Any] | None = None
    if grant_surface or shrink_surface:
        if counts is None:
            # build_report 会给出同一失败语义；这里不读取任何 provenance 路径。
            provenance = None
        else:
            validated_counts = _validate_count_manifest(
                counts,
                expected_base_facts_sha256=base_facts_sha256,
                expected_head_facts_sha256=head_facts_sha256,
                expected_grant_surface_sha256=_surface_digest(grant_surface),
                expected_shrink_surface_sha256=_surface_digest(shrink_surface),
            )
            if validated_counts["source"]["kind"] != STAGE_COUNT_CLAIM_SOURCE:
                raise CountEvidenceError(
                    "非空权限影响面不能使用静态 0 或其他自报来源；"
                    "请从 biai-stage 取得聚合声明并由仓库外 registration 绑定"
                )
            if trusted_provenance_path is None:
                raise CountEvidenceError(
                    "PR 内 biai-stage 聚合只是未验证声明；缺少仓库外 hash registration。"
                    "当前没有受保护 stage artifact/attestation，需经 PM 门补齐注入"
                )
            provenance_document = _load_external_provenance(
                trusted_provenance_path, repository=repository
            )
            provenance = _validate_stage_provenance(
                provenance_document,
                manifest=counts,
                expected_base_facts_sha256=base_facts_sha256,
                expected_head_facts_sha256=head_facts_sha256,
                expected_grant_surface_sha256=_surface_digest(grant_surface),
                expected_shrink_surface_sha256=_surface_digest(shrink_surface),
            )
    report = build_report(
        base_roles,
        head_roles,
        base_metrics,
        head_metrics,
        user_counts=counts,
        base_facts_sha256=base_facts_sha256,
        head_facts_sha256=head_facts_sha256,
        base_ref=base_ref,
        head_ref=head_ref,
        strict_count_manifest=True,
    )
    if provenance is not None:
        report["affected_user_counts"]["status"] = "provided-registered"
        report["affected_user_counts"]["source"]["provenance"] = provenance["status"]
        report["affected_user_counts"]["source"]["registered_at"] = provenance[
            "registered_at"
        ]
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(render_report(report))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--head-ref", required=True)
    parser.add_argument("--repository", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument(
        "--user-counts",
        type=Path,
        help="绑定两份权限事实和 grant/shrink 面的 biai-stage 计数声明 JSON",
    )
    parser.add_argument(
        "--trusted-provenance",
        type=Path,
        help=(
            "仓库外的 biai-stage hash registration；PR 内清单只是 claim，"
            "没有该文件时非空权限面失败关闭"
        ),
    )
    parser.add_argument("--output", type=Path, help="可选 JSON 报告路径")
    args = parser.parse_args()
    try:
        return run_check(
            args.base_ref,
            args.head_ref,
            repository=args.repository,
            user_counts_path=args.user_counts,
            trusted_provenance_path=args.trusted_provenance,
            output=args.output,
        )
    except (ConfigShapeError, RuntimeError, ValueError) as error:
        print(f"L3 权限影响面 diff 失败关闭：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
