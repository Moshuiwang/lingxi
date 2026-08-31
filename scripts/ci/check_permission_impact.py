#!/usr/bin/env python3
"""L3 权限事实配置影响面门禁（Issue #498）。

只读取两个 Git ref 中的两份 TOML 配置，不连接数据库、不读取生产快照、不需要凭据。
脚本把角色→职能与公司→职能→指标的有效笛卡尔面做集合差，报告新增授予面与收缩面
两栏；报告中的「受影响用户数量」只有在调用方显式提供经过批准的纯计数 JSON 时才会
出现数字。当前 CI 不提供该数据源，因此如实输出 ``not_provided``，绝不拿角色数、
配置行数或任何内部 ID 冒充用户数。
"""

from __future__ import annotations

import argparse
import json
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

SurfaceEntry = tuple[str, str, str, str]


class ConfigShapeError(ValueError):
    """权限事实配置形状不符合生产解析器的 fail-closed 合同。"""


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


def _validate_user_counts(user_counts: Mapping[str, Any] | None) -> dict[str, int | None]:
    if user_counts is None:
        return {"grant": None, "shrink": None}
    if not isinstance(user_counts, Mapping) or set(user_counts) != {"grant", "shrink"}:
        raise ValueError("用户数量输入只能包含 grant 与 shrink 两个纯计数字段")
    result: dict[str, int | None] = {}
    for key in ("grant", "shrink"):
        value = user_counts[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"用户数量 {key} 必须是非负整数；不接受用户 ID 或其他明细")
        result[key] = value
    return result


def build_report(
    base_role_document: Mapping[str, Any],
    head_role_document: Mapping[str, Any],
    base_metric_document: Mapping[str, Any],
    head_metric_document: Mapping[str, Any],
    *,
    user_counts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造可序列化的影响面报告；不包含用户明细。"""

    base_roles = _role_map(base_role_document, "base 角色映射")
    head_roles = _role_map(head_role_document, "head 角色映射")
    base_metrics = _metric_map(base_metric_document, "base 公司指标映射")
    head_metrics = _metric_map(head_metric_document, "head 公司指标映射")
    base_surface = build_surface(base_roles, base_metrics)
    head_surface = build_surface(head_roles, head_metrics)
    counts = _validate_user_counts(user_counts)
    return {
        "schema": REPORT_SCHEMA,
        "grant": _surface_rows(head_surface - base_surface),
        "shrink": _surface_rows(base_surface - head_surface),
        "grant_entry_count": len(head_surface - base_surface),
        "shrink_entry_count": len(base_surface - head_surface),
        "role_mapping_changes": _role_changes(base_roles, head_roles),
        "metric_mapping_changes": _metric_changes(base_metrics, head_metrics),
        "affected_user_counts": {
            "grant": counts["grant"],
            "shrink": counts["shrink"],
            "status": "provided" if user_counts is not None else "not_provided",
            "reason": (
                "CI 没有经批准的用户数量事实源；未用配置行数或内部 ID 推算"
                if user_counts is None
                else "调用方提供了经过批准的纯数量输入；报告不包含用户 ID"
            ),
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
    role_raw = _git_file(repository, ref, ROLE_MAP_PATH)
    metric_raw = _git_file(repository, ref, METRIC_MAP_PATH)
    # A missing file is never treated as an empty permission source. This also makes a
    # delete/rename fail closed instead of turning a shrink into an apparently harmless diff.
    return (
        _parse_toml(role_raw, ref, ROLE_MAP_PATH),
        _parse_toml(metric_raw, ref, METRIC_MAP_PATH),
    )


def _load_user_counts(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"用户数量输入无法读取或解析：{path}（{error}）") from error
    if not isinstance(value, Mapping):
        raise ValueError("用户数量输入必须是 JSON 对象，只允许 grant/shrink 数量")
    return value


def render_report(report: Mapping[str, Any]) -> str:
    """渲染公开门禁摘要；只展示权限事实与数量，不展示用户明细。"""

    lines = ["L3 权限影响面 diff：通过（静态、无生产数据/凭据）"]
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
        lines.append(f"  {label}={value if value is not None else '未提供'}")
    lines.append(f"  说明：{counts['reason']}")
    return "\n".join(lines)


def run_check(
    base_ref: str,
    head_ref: str,
    *,
    repository: Path = REPOSITORY_ROOT,
    user_counts_path: Path | None = None,
    output: Path | None = None,
) -> int:
    base_roles, base_metrics = _load_ref_documents(repository, base_ref)
    head_roles, head_metrics = _load_ref_documents(repository, head_ref)
    counts = _load_user_counts(user_counts_path) if user_counts_path is not None else None
    report = build_report(
        base_roles,
        head_roles,
        base_metrics,
        head_metrics,
        user_counts=counts,
    )
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
    parser.add_argument("--user-counts", type=Path, help="经批准的仅含 grant/shrink 数量 JSON")
    parser.add_argument("--output", type=Path, help="可选 JSON 报告路径")
    args = parser.parse_args()
    try:
        return run_check(
            args.base_ref,
            args.head_ref,
            repository=args.repository,
            user_counts_path=args.user_counts,
            output=args.output,
        )
    except (ConfigShapeError, RuntimeError, ValueError) as error:
        print(f"L3 权限影响面 diff 失败关闭：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
