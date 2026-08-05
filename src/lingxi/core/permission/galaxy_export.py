"""银河导出五张表的落库前校验与列名规范化（纯函数）。

导入流程是「校验 → 检查已有数据 → 写入 → 回读确认」，本模块是第一步：
它把源导出（Excel 各 sheet 导出的 CSV 行）规范化成可写入的行，并给出可判定的
错误与告警。**校验不通过时调用方不得写入任何一行**，半批数据比没有数据更危险。

三处与源结构有关的处理，理由见 docs/参考证据/银河用户权限数据结构.md：

- `sys_user_datacountry` 的源列名是大写，`user` 等表是小写。列名一律按去空白 +
  转小写归一，落库列名唯一；导入器因此同时接受两种源样式。
- `user_role.user_name` 的值实测几乎全是中文姓名而非登录账号。落库列名改为
  `source_user_name`，让「按它连接」在代码里说不出口。
- `sys_country` 的 `boss_company_id` 保留：产品负责人 2026-08-05 决策 3 指出
  它是未来向 MCP 申请权限时的公司字段。

**脱敏纪律**：校验信息只写表名、列名、行号与计数，绝不回显姓名、邮箱、账号等
人员数据值——这些信息会进日志与 Issue。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

SOURCE_TABLES: tuple[str, ...] = (
    "user",
    "user_role",
    "role_menu",
    "sys_user_datacountry",
    "sys_country",
)

TARGET_TABLES: Mapping[str, str] = {
    "user": "galaxy_user",
    "user_role": "galaxy_user_role",
    "role_menu": "galaxy_role_menu",
    "sys_user_datacountry": "galaxy_user_datacountry",
    "sys_country": "galaxy_country",
}

# 报告里最多列出多少个行号：足够定位，又不会把整表行号倒进日志。
_MAX_REPORTED_ROWS = 20


@dataclass(frozen=True)
class ColumnSpec:
    """一列的源列名与落库列名。`required` 指该列必须出现在表头。"""

    canonical: str
    sources: tuple[str, ...]
    required: bool = False
    non_empty: bool = False


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: tuple[ColumnSpec, ...]
    primary_key: tuple[str, ...]


def _column(canonical: str, *sources: str, required: bool = False, non_empty: bool = False) -> ColumnSpec:
    return ColumnSpec(canonical, sources or (canonical,), required=required, non_empty=non_empty)


TABLE_SPECS: Mapping[str, TableSpec] = {
    "user": TableSpec(
        "user",
        (
            _column("user_id", required=True, non_empty=True),
            _column("dept_id"),
            _column("user_name"),
            _column("nick_name"),
            _column("email"),
            # 源导出的建号时间保持原样文本：格式未经核对，不在导入期猜测解析。
            _column("create_time_raw", "create_time"),
        ),
        ("user_id",),
    ),
    "user_role": TableSpec(
        "user_role",
        (
            _column("user_id", required=True, non_empty=True),
            _column("role_id", required=True, non_empty=True),
            _column("source_user_name", "user_name"),
            _column("role_name"),
        ),
        ("user_id", "role_id"),
    ),
    "role_menu": TableSpec(
        "role_menu",
        (
            _column("role_id", required=True, non_empty=True),
            _column("menu_id", required=True, non_empty=True),
            _column("role_name"),
            _column("menu_name"),
        ),
        ("role_id", "menu_id"),
    ),
    "sys_user_datacountry": TableSpec(
        "sys_user_datacountry",
        (
            _column("user_id", required=True, non_empty=True),
            _column("datacountry_id", required=True, non_empty=True),
            _column("source_user_name", "user_name"),
            _column("datacountry_name"),
        ),
        ("user_id", "datacountry_id"),
    ),
    "sys_country": TableSpec(
        "sys_country",
        (
            _column("source_id", "id", required=True, non_empty=True),
            _column("country_key"),
            _column("name"),
            _column("code"),
            _column("name_cn"),
            _column("region_key"),
            _column("region_name"),
            _column("boss_company_id"),
        ),
        ("source_id",),
    ),
}


@dataclass(frozen=True)
class Issue:
    """一条校验结论。`detail` 只允许出现结构信息与计数。"""

    table: str
    rule: str
    detail: str
    row_numbers: tuple[int, ...] = ()


@dataclass(frozen=True)
class ValidationReport:
    ok: bool
    errors: tuple[Issue, ...]
    warnings: tuple[Issue, ...]
    row_counts: Mapping[str, int]
    rows: Mapping[str, tuple[Mapping[str, str | None], ...]] = field(default_factory=dict)


def normalize_column_name(name: str) -> str:
    """源列名归一：去 BOM、去首尾空白、转小写。"""

    return name.replace("﻿", "").strip().lower()


def _normalize_value(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _rows_hint(rows: Sequence[int]) -> tuple[int, ...]:
    return tuple(rows[:_MAX_REPORTED_ROWS])


def _normalize_table(
    spec: TableSpec, raw_rows: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, str | None]], list[Issue], list[Issue]]:
    errors: list[Issue] = []
    warnings: list[Issue] = []

    source_to_canonical: dict[str, str] = {}
    for column in spec.columns:
        for source in column.sources:
            source_to_canonical[normalize_column_name(source)] = column.canonical

    present: set[str] = set()
    ignored: set[str] = set()
    for raw_row in raw_rows:
        for raw_name in raw_row:
            normalized = normalize_column_name(str(raw_name))
            if normalized in source_to_canonical:
                present.add(source_to_canonical[normalized])
            elif normalized:
                ignored.add(normalized)

    for column in spec.columns:
        if column.required and column.canonical not in present:
            errors.append(
                Issue(spec.name, "missing_column", f"缺少必需列：{column.canonical}（可接受的源列名：{'/'.join(column.sources)}）")
            )

    if ignored:
        warnings.append(
            Issue(spec.name, "ignored_column", f"源表含 {len(ignored)} 个未落库的列：{'、'.join(sorted(ignored))}")
        )

    if errors:
        return [], errors, warnings

    rows: list[dict[str, str | None]] = []
    empty_key_rows: list[int] = []
    for row_number, raw_row in enumerate(raw_rows, start=1):
        normalized_row: dict[str, str | None] = {column.canonical: None for column in spec.columns}
        for raw_name, raw_value in raw_row.items():
            canonical = source_to_canonical.get(normalize_column_name(str(raw_name)))
            if canonical is not None:
                normalized_row[canonical] = _normalize_value(raw_value)
        if any(
            column.non_empty and not normalized_row.get(column.canonical) for column in spec.columns
        ):
            empty_key_rows.append(row_number)
            continue
        rows.append(normalized_row)

    if empty_key_rows:
        errors.append(
            Issue(
                spec.name,
                "empty_key",
                f"{len(empty_key_rows)} 行的关键列为空",
                _rows_hint(empty_key_rows),
            )
        )

    return rows, errors, warnings


def _deduplicate(
    spec: TableSpec, rows: Sequence[Mapping[str, str | None]]
) -> tuple[list[Mapping[str, str | None]], list[Issue], list[Issue]]:
    errors: list[Issue] = []
    warnings: list[Issue] = []

    kept: dict[tuple[str | None, ...], Mapping[str, str | None]] = {}
    exact_duplicate_rows: list[int] = []
    conflicting_rows: list[int] = []

    for row_number, row in enumerate(rows, start=1):
        key = tuple(row.get(column) for column in spec.primary_key)
        existing = kept.get(key)
        if existing is None:
            kept[key] = row
        elif dict(existing) == dict(row):
            exact_duplicate_rows.append(row_number)
        else:
            conflicting_rows.append(row_number)

    if exact_duplicate_rows:
        warnings.append(
            Issue(
                spec.name,
                "duplicate_row",
                f"{len(exact_duplicate_rows)} 行与既有行完全相同，已合并",
                _rows_hint(exact_duplicate_rows),
            )
        )
    if conflicting_rows:
        errors.append(
            Issue(
                spec.name,
                "conflicting_duplicate",
                f"{len(conflicting_rows)} 行与同主键的既有行内容不一致（主键：{'+'.join(spec.primary_key)}）",
                _rows_hint(conflicting_rows),
            )
        )

    return list(kept.values()), errors, warnings


def _check_references(
    tables: Mapping[str, Sequence[Mapping[str, str | None]]]
) -> tuple[list[Issue], list[Issue]]:
    errors: list[Issue] = []
    warnings: list[Issue] = []

    known_users = {row["user_id"] for row in tables["user"]}
    known_country_keys = {row["country_key"] for row in tables["sys_country"] if row["country_key"]}
    menu_roles = {row["role_id"] for row in tables["role_menu"]}

    for source_table in ("user_role", "sys_user_datacountry"):
        dangling = [
            row_number
            for row_number, row in enumerate(tables[source_table], start=1)
            if row["user_id"] not in known_users
        ]
        if dangling:
            errors.append(
                Issue(
                    source_table,
                    "dangling_user_id",
                    f"{len(dangling)} 行的 user_id 在 user 表中不存在",
                    _rows_hint(dangling),
                )
            )

    dangling_country = [
        row_number
        for row_number, row in enumerate(tables["sys_user_datacountry"], start=1)
        if row["datacountry_id"] not in known_country_keys
    ]
    if dangling_country:
        errors.append(
            Issue(
                "sys_user_datacountry",
                "dangling_country_key",
                f"{len(dangling_country)} 行的 datacountry_id 连不到 sys_country.country_key"
                "（注意：连接键不是主键 id）",
                _rows_hint(dangling_country),
            )
        )

    roles_without_menu = sorted(
        {row["role_id"] for row in tables["user_role"] if row["role_id"] not in menu_roles and row["role_id"]}
    )
    if roles_without_menu:
        warnings.append(
            Issue(
                "user_role",
                "role_without_menu",
                f"{len(roles_without_menu)} 个被分配的角色在 role_menu 中没有任何菜单",
            )
        )

    countries_without_key = [
        row_number
        for row_number, row in enumerate(tables["sys_country"], start=1)
        if not row["country_key"]
    ]
    if countries_without_key:
        warnings.append(
            Issue(
                "sys_country",
                "country_without_key",
                f"{len(countries_without_key)} 行没有 country_key，无法从授权表连接到",
                _rows_hint(countries_without_key),
            )
        )

    return errors, warnings


def validate_export(raw_tables: Mapping[str, Sequence[Mapping[str, Any]]]) -> ValidationReport:
    """校验一份完整导出，返回可写入的规范化行与全部错误、告警。

    `ok` 为假时调用方必须整批放弃，不得写入任何一行。
    """

    errors: list[Issue] = []
    warnings: list[Issue] = []

    missing = [name for name in SOURCE_TABLES if name not in raw_tables]
    for name in missing:
        errors.append(Issue(name, "missing_table", "导出缺少该表"))
    unknown = [name for name in raw_tables if name not in TABLE_SPECS]
    if unknown:
        warnings.append(Issue("-", "ignored_table", f"导出含 {len(unknown)} 张未落库的表：{'、'.join(sorted(unknown))}"))
    if missing:
        return ValidationReport(False, tuple(errors), tuple(warnings), {}, {})

    normalized: dict[str, list[Mapping[str, str | None]]] = {}
    structural_failure = False
    for name in SOURCE_TABLES:
        spec = TABLE_SPECS[name]
        raw_rows = list(raw_tables[name])
        if not raw_rows:
            # 空表最可能是「导错了 sheet」。放行会写出一份看起来正常、实际缺一条
            # 授权链的快照，这比导入失败危险得多。
            errors.append(Issue(name, "empty_table", "该表没有任何数据行"))
            structural_failure = True
            normalized[name] = []
            continue
        rows, table_errors, table_warnings = _normalize_table(spec, raw_rows)
        errors.extend(table_errors)
        warnings.extend(table_warnings)
        if any(issue.rule == "missing_column" for issue in table_errors):
            # 表头都对不上时无法判断行内容，跨表引用检查失去意义。
            structural_failure = True
            normalized[name] = []
            continue
        rows, dedupe_errors, dedupe_warnings = _deduplicate(spec, rows)
        errors.extend(dedupe_errors)
        warnings.extend(dedupe_warnings)
        normalized[name] = rows

    if not structural_failure:
        reference_errors, reference_warnings = _check_references(normalized)
        errors.extend(reference_errors)
        warnings.extend(reference_warnings)

        for name in SOURCE_TABLES:
            if not normalized[name] and not any(issue.table == name for issue in errors):
                errors.append(Issue(name, "empty_table", "该表没有任何可写入的行"))

    ok = not errors
    return ValidationReport(
        ok=ok,
        errors=tuple(errors),
        warnings=tuple(warnings),
        row_counts={name: len(rows) for name, rows in normalized.items()},
        rows={name: tuple(rows) for name, rows in normalized.items()},
    )
