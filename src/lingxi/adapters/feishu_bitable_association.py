"""飞书组织快照与多维表格记录的纯关联分析。

测试资产 / 不属于正式用户路径：Bot-Test 受控验证阶段产出，被
`feishu_roster_bitable.py` docstring 引用为已验证过的读取模式参考，本身不在
正式调用链上（代码框架「五、测试资产与正式代码的边界」现存件清单；
`scripts/ci/check_installed_package.py` 同步登记为豁免）。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any


def _text_values(value: Any) -> list[str]:
    if value is None or value is False:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [str(value)]
    if isinstance(value, list):
        result: list[str] = []
        for child in value:
            result.extend(_text_values(child))
        return result
    if isinstance(value, dict):
        result: list[str] = []
        for key in ("text", "value", "name", "default_value", "zh_cn", "en_us"):
            if key in value:
                result.extend(_text_values(value[key]))
        if result:
            return result
        for child in value.values():
            result.extend(_text_values(child))
        return result
    return []


def _first_text(value: Any) -> str | None:
    values = _text_values(value)
    return values[0] if values else None


def _index(rows: Iterable[Mapping[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        value = row.get(key)
        if value:
            result[str(value)].append(dict(row))
    return result


def _normalize_snapshot_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for row in rows:
        normalized.append(
            {
                "tenant_key": _first_text(row.get("tenant_key")),
                "user_id": _first_text(row.get("user_id")),
                "open_id": _first_text(row.get("open_id")),
                "union_id": _first_text(row.get("union_id")),
                "union_user_id": _first_text(row.get("union_user_id")),
                "name": _first_text(row.get("name")),
            }
        )
    return normalized


def _normalize_bitable_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for row in rows:
        normalized.append(
            {
                "personnel_id": _first_text(row.get("人员ID")),
                "open_id": _first_text(row.get("open_id")),
                "work_no": _first_text(row.get("工号")),
                "name": _first_text(row.get("人员姓名")),
            }
        )
    return normalized


def _match_stats(rows: list[dict[str, Any]], field: str, index: Mapping[str, list[dict[str, Any]]]) -> dict[str, int]:
    matched = [row for row in rows if row.get(field) and row[field] in index]
    return {
        "matched_rows": len(matched),
        "matched_values": len({row[field] for row in matched}),
    }


def analyze_association(
    bitable_rows: Iterable[Mapping[str, Any]],
    snapshot_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """返回不包含原始记录的关联统计，供脚本和测试共同使用。"""

    bitable = _normalize_bitable_rows(bitable_rows)
    snapshot = _normalize_snapshot_rows(snapshot_rows)
    by_user_id = _index(snapshot, "user_id")
    by_open_id = _index(snapshot, "open_id")
    by_name = _index(snapshot, "name")

    both_keys = []
    same_member = []
    for row in bitable:
        user_candidates = by_user_id.get(row["personnel_id"], []) if row["personnel_id"] else []
        open_candidates = by_open_id.get(row["open_id"], []) if row["open_id"] else []
        if not user_candidates or not open_candidates:
            continue
        both_keys.append(row)
        user_members = {(candidate.get("tenant_key"), candidate.get("user_id")) for candidate in user_candidates}
        open_members = {(candidate.get("tenant_key"), candidate.get("user_id")) for candidate in open_candidates}
        if user_members & open_members:
            same_member.append(row)

    name_matches = [row for row in bitable if row["name"] and row["name"] in by_name]
    personnel_to_open = [row for row in bitable if row["personnel_id"] and row["personnel_id"] in by_open_id]
    open_to_user = [row for row in bitable if row["open_id"] and row["open_id"] in by_user_id]

    return {
        "snapshot": {
            "member_rows": len(snapshot),
            "distinct_user_id": len(by_user_id),
            "distinct_open_id": len(by_open_id),
            "distinct_union_id": len({row["union_id"] or row["union_user_id"] for row in snapshot if row["union_id"] or row["union_user_id"]}),
        },
        "bitable": {
            "record_rows": len(bitable),
            "nonempty_personnel_id": sum(bool(row["personnel_id"]) for row in bitable),
            "distinct_personnel_id": len({row["personnel_id"] for row in bitable if row["personnel_id"]}),
            "nonempty_open_id": sum(bool(row["open_id"]) for row in bitable),
            "distinct_open_id": len({row["open_id"] for row in bitable if row["open_id"]}),
            "nonempty_work_no": sum(bool(row["work_no"]) for row in bitable),
            "distinct_work_no": len({row["work_no"] for row in bitable if row["work_no"]}),
        },
        "joins": {
            "personnel_id_to_user_id": _match_stats(bitable, "personnel_id", by_user_id),
            "open_id_to_open_id": _match_stats(bitable, "open_id", by_open_id),
            "both_keys_same_member": {
                "rows_with_both_keys": len(both_keys),
                "same_member_rows": len(same_member),
            },
            "name_only": {
                "matched_rows": len(name_matches),
                "unique_snapshot_name_matches": sum(len(by_name[row["name"]]) == 1 for row in name_matches),
                "ambiguous_snapshot_name_matches": sum(len(by_name[row["name"]]) > 1 for row in name_matches),
            },
            "work_no": {"matched_against_snapshot_identity_fields": 0},
            "cross_checks": {
                "personnel_id_to_snapshot_open_id": len(personnel_to_open),
                "open_id_to_snapshot_user_id": len(open_to_user),
            },
        },
    }
