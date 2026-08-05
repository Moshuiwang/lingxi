"""读取银河导出目录里的五个 CSV（标准库 csv，无新增依赖）。

首期的权限来源是产品负责人从银河后台导出的 Excel，各 sheet 另存为 CSV 后放进
同一个目录。本模块只负责把文件读成「源列名 → 文本」的行，不做任何类型推断：
`country_key`、工号这类值是标识符，一旦被当成数字解析就会丢前导零、变成科学计数。

导出含全部内部人员的姓名、邮箱与逐人授权明细，**不得进入仓库、日志或任何交付物**
（见 docs/参考证据/银河用户权限数据结构.md 的处理边界）。本模块不打印任何行内容。
"""

from __future__ import annotations

import csv
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
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
    """把一个 CSV 读成文本行；缺列补空串，多余列按 `None` 键丢弃。"""

    # utf-8-sig：Excel 导出的 CSV 常带 BOM，不去掉会让首列列名对不上。
    with path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        rows: list[dict[str, str]] = []
        for raw_row in reader:
            row = {
                str(key): ("" if value is None else value)
                for key, value in raw_row.items()
                if key is not None
            }
            rows.append(row)
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
