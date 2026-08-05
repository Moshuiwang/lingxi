"""花名册多维表格的只读 adapter（正式壳）。

它承接的是受控验证已经证明的读取模式（`scripts/read_feishu_bitable_association.py`
与 `adapters/feishu_bitable_association.py` 这两个**测试资产**），把其中会长期存在的
部分固定下来：分页读取、对象型单元格取文本、只取匹配链路需要的字段。

本切片**不做真实调用**：传输由调用方注入（`RecordPageReader`），本模块不 import
任何 SDK、不读凭据、不发网络请求。真实读取的凭据、scope 与可复跑方式见
[飞书组织快照与多维表格关联](../../../docs/技术设计/飞书组织快照与多维表格关联.md)。

边界（同上文与产品合同）：花名册是**下游人员信息表**，不是权限权威，不得据此
创建或扩大任何 Lingxi 权限；这里只把它当作「飞书 user_id → 邮箱」的查表。
同一人员 ID 的重复行**保留不去重**——实测存在，去重会把歧义变成静默的错误选择。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, NamedTuple, Protocol

# 匹配链路只需要这四个字段；其余业务字段一律不读入内存，减少可识别数据的暴露面。
ROSTER_FIELD_NAMES: tuple[str, ...] = ("人员ID", "邮箱", "人员姓名", "工号")

_FIELD_TEXT_KEYS = ("text", "value", "name", "default_value", "zh_cn", "en_us")


class RosterRow(NamedTuple):
    """一行花名册记录的归一化形态，可直接作为匹配层的输入映射。"""

    personnel_id: str
    email: str
    name: str
    # 命名与匹配层输入键一致（employee_no）：曾叫 work_no，与
    # core/permission/account_match 的键名对不上，工号在接线处会静默丢失
    # （独立复查实测：该形态下全部退化为纯邮箱匹配）。
    employee_no: str
    record_id: str


class RecordPageReader(Protocol):
    """按页返回多维表格记录的只读传输。"""

    def list_records(self, page_token: str | None = None) -> tuple[Sequence[Any], str | None]:
        ...


def field_text(value: Any) -> str:
    """把多维表格的单元格值取成文本。

    多维表格的同一列在不同字段类型下可能是字符串、数字、对象或对象数组，
    受控读取中三种形态都真实出现过。
    """

    if value is None or value is False:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        for child in value:
            text = field_text(child)
            if text:
                return text
        return ""
    if isinstance(value, dict):
        for key in _FIELD_TEXT_KEYS:
            if key in value:
                text = field_text(value[key])
                if text:
                    return text
        for child in value.values():
            text = field_text(child)
            if text:
                return text
    return ""


def normalize_record(record: Any) -> RosterRow:
    """把一条原始记录归一成 `RosterRow`；缺字段给空串，不给 `None`。"""

    fields = record.get("fields", {}) if isinstance(record, dict) else {}
    return RosterRow(
        personnel_id=field_text(fields.get("人员ID")),
        email=field_text(fields.get("邮箱")),
        name=field_text(fields.get("人员姓名")),
        employee_no=field_text(fields.get("工号")),
        record_id=field_text(record.get("record_id")) if isinstance(record, dict) else "",
    )


def read_roster_records(reader: RecordPageReader, *, max_pages: int = 1000) -> tuple[RosterRow, ...]:
    """读完全部分页并归一；不去重、不过滤、不写回多维表格。

    `max_pages` 是防御性上限：`page_token` 若因外部异常一直非空，宁可失败也不空转。
    """

    rows: list[RosterRow] = []
    page_token: str | None = None
    for _ in range(max_pages):
        records, page_token = reader.list_records(page_token)
        rows.extend(normalize_record(record) for record in records)
        if not page_token:
            return tuple(rows)
    raise RuntimeError(f"花名册分页读取超过 {max_pages} 页仍未结束，已停止")
