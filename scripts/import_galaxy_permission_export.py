#!/usr/bin/env python3
"""把一份银河权限导出导入 Lingxi 数据库（受控运行脚本，不属于生产镜像）。

首期的权限来源是产品负责人从银河后台导出的快照。本脚本是这份快照进入数据库的
唯一入口：它按「校验 → 检查已有数据 → 写入 → 回读确认」执行，校验不通过时一行
都不写。刷新频率与由谁触发仍是 Issue #17 的待决策 3，本脚本不做定时调度。

用法（导出目录里放五个 CSV：user / user_role / role_menu /
sys_user_datacountry / sys_country）：

    export LINGXI_POSTGRES_DSN='postgresql://...'
    PYTHONPATH=src python3 scripts/import_galaxy_permission_export.py \\
        /path/to/export --source-label '银河后台导出 2026-07-28'

**处理边界**：导出含全部内部人员的姓名、邮箱与逐人授权明细，不得进入仓库、
Issue、PR 或日志。本脚本只输出表名、行数与告警规则名，不打印任何行内容。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from lingxi.adapters.galaxy_csv_export import load_export_directory
from lingxi.adapters.galaxy_import import ALREADY_IMPORTED, REJECTED, PostgresGalaxyImportStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入银河权限导出（CSV 目录）")
    parser.add_argument("export_directory", type=Path, help="含五个 CSV 的导出目录")
    parser.add_argument(
        "--confirm-unchanged",
        action="store_true",
        help="确认这是新一次真实导出、内容恰与历史批次相同（连续刷新无变化或权限回到旧状态）；"
        "默认为防误重导旧文件，同内容导入只返回既有批次",
    )
    parser.add_argument(
        "--source-label",
        required=True,
        help="来源标注，例如「银河后台导出 2026-07-28」；会写进批次表供回溯",
    )
    arguments = parser.parse_args(argv)

    dsn = os.environ.get("LINGXI_POSTGRES_DSN")
    if not dsn:
        print("缺少环境变量 LINGXI_POSTGRES_DSN，未做任何操作。", file=sys.stderr)
        return 2

    bundle = load_export_directory(arguments.export_directory)
    store = PostgresGalaxyImportStore(dsn)
    result = store.import_export(
        source_label=arguments.source_label,
        confirm_unchanged=arguments.confirm_unchanged,
        source_digest=bundle.digest,
        tables=bundle.tables,
    )

    def _issue_line(issue) -> str:
        # 行号是脱敏结论里唯一的定位抓手（V-银河-13：不回显姓名/邮箱/账号）；
        # 被拒批次不落库，不打印行号管理员就无从定位（终轮 Codex）。上限 10 个。
        text = f"{issue.table}/{issue.rule}：{issue.detail}"
        if issue.row_numbers:
            shown = "、".join(str(number) for number in issue.row_numbers[:10])
            text += f"（数据行 {shown}{'…' if len(issue.row_numbers) > 10 else ''}）"
        return text

    for issue in result.warnings:
        print(f"告警 {_issue_line(issue)}")

    if result.outcome == REJECTED:
        print("校验未通过，未写入任何一行：", file=sys.stderr)
        for issue in result.errors:
            print(f"  - {_issue_line(issue)}", file=sys.stderr)
        return 1

    if result.outcome == ALREADY_IMPORTED:
        print(f"这份导出已经导入过，沿用既有批次：{result.batch_id}")
        return 0

    print(f"导入完成，批次：{result.batch_id}")
    for table, count in result.row_counts.items():
        print(f"  {table}: {count} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
