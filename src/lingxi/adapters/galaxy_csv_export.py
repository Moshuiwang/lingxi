"""读取银河导出目录里的五个 CSV（标准库 csv，无新增依赖）。

首期的权限来源是产品负责人从银河后台导出的 Excel，各 sheet 另存为 CSV 后放进
同一个目录。本模块只负责把文件读成「源列名 → 文本」的行，不做任何类型推断：
`country_key`、工号这类值是标识符，一旦被当成数字解析就会丢前导零、变成科学计数。

导出含全部内部人员的姓名、邮箱与逐人授权明细，**不得进入仓库、日志或任何交付物**
（见 docs/参考证据/银河用户权限数据结构.md 的处理边界）。本模块不打印任何行内容。
"""

from __future__ import annotations

import csv
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from lingxi.core.permission.galaxy_export import SOURCE_TABLES

EXPORT_FILE_NAMES: Mapping[str, str] = {name: f"{name}.csv" for name in SOURCE_TABLES}


@dataclass(frozen=True)
class ExportBundle:
    """一次导出的全部内容与其内容摘要。

    `digest` 只由文件内容算出，用于识别「同一份导出被重复导入」，本身不含人员数据。
    """

    tables: Mapping[str, list[dict[str, str]]]
    digest: str
    directory: Path


def read_csv_table(path: Path) -> list[dict[str, str]]:
    """把一个 CSV 读成文本行；缺列补空串，**字段数超过表头的行整文件拒绝**。

    未转义逗号或损坏导出会让 `DictReader` 把溢出值挂在 `None` 键下；静默丢弃
    意味着该行的邮箱/姓名/公司字段可能已整体错位，却仍能通过校验并取代有效
    批次（Codex 复查发现）。错误信息只报行号，不回显内容。
    """

    # utf-8-sig：Excel 导出的 CSV 常带 BOM，不去掉会让首列列名对不上。
    with path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        # strict 方言：引号错误抛 csv.Error 而不是把文件余下内容吞进当前字段
        # （终轮 Codex：未闭合引号的错位行可能带着关键 ID 通过校验）。
        reader = csv.DictReader(csv_file, strict=True)
        rows: list[dict[str, str]] = []
        overflow_rows: list[int] = []
        try:
            enumerated = list(enumerate(reader, start=1))
        except csv.Error as error:
            raise ValueError(f"{path.name} 不是可靠的 CSV（解析错误，疑似未闭合引号），整文件拒绝") from error
        for row_number, raw_row in enumerated:
            if None in raw_row and raw_row.get(None):
                overflow_rows.append(row_number)
                continue
            row = {
                str(key): ("" if value is None else value)
                for key, value in raw_row.items()
                if key is not None
            }
            rows.append(row)
    if overflow_rows:
        shown = "、".join(str(number) for number in overflow_rows[:10])
        raise ValueError(
            f"{path.name} 有 {len(overflow_rows)} 行字段数超过表头（数据行 {shown}"
            f"{'…' if len(overflow_rows) > 10 else ''}）：疑似未转义逗号或损坏导出，整文件拒绝"
        )
    return rows


def load_export_directory(directory: Path) -> ExportBundle:
    """读取目录下的五个 CSV；缺文件立即失败并列出缺哪几个。"""

    directory = Path(directory)
    missing = [file_name for file_name in EXPORT_FILE_NAMES.values() if not (directory / file_name).is_file()]
    if missing:
        raise FileNotFoundError(f"导出目录缺少以下文件：{'、'.join(sorted(missing))}（目录：{directory}）")

    tables: dict[str, list[dict[str, str]]] = {}
    digest = hashlib.sha256()
    for source_table in SOURCE_TABLES:
        file_path = directory / EXPORT_FILE_NAMES[source_table]
        tables[source_table] = read_csv_table(file_path)
        digest.update(EXPORT_FILE_NAMES[source_table].encode("utf-8"))
        digest.update(file_path.read_bytes())

    return ExportBundle(tables=tables, digest=digest.hexdigest(), directory=directory)
