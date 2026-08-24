#!/usr/bin/env python3
"""开工必读集体量预算（产品负责人 2026-08-24 批准的防膨胀门禁）。

`AGENTS.md` 把「实现或修改正式代码前必读」定为代码框架 + 验证与门禁两份文档。
本检查给这两份文档的合计字节数设硬上限：超限即红，逼迫瘦身而不是继续堆积——
与代码的体量棘轮（check_size_ratchet.py）同一思路。历史教训：这两份文档曾分别
长到 53KB 与 18.5KB，其中大半是住错地方的实现编年史（2026-08-24 维护批实测）。

上限调整必须与实际瘦身 / 扩容一起发生在同一次改动里，且留下可审阅的 diff；
不接受"先抬上限再慢慢写"。
"""

from __future__ import annotations

import sys
from pathlib import Path

BUDGET_FILES = (
    "docs/技术设计/代码框架.md",
    "docs/技术设计/验证与门禁.md",
)

# 合计上限（字节）。2026-08-24 瘦身后两文件合计约 27KB，留少量余量。
TOTAL_BUDGET_BYTES = 30 * 1024


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    sizes: list[tuple[str, int]] = []
    missing: list[str] = []
    for relative in BUDGET_FILES:
        path = root / relative
        if not path.is_file():
            missing.append(relative)
            continue
        sizes.append((relative, path.stat().st_size))

    if missing:
        print("开工必读集体量预算：以下受预算约束的文件不存在（改名或删除时必须同步更新本检查）：", file=sys.stderr)
        for relative in missing:
            print(f"  - {relative}", file=sys.stderr)
        return 1

    total = sum(size for _, size in sizes)
    detail = "、".join(f"{relative}={size}B" for relative, size in sizes)
    if total > TOTAL_BUDGET_BYTES:
        print(
            f"开工必读集体量预算超限：合计 {total}B > 上限 {TOTAL_BUDGET_BYTES}B（{detail}）。"
            "请瘦身文档（编年史移到 Issue / PR / 模块 docstring），或在同一改动里带理由调整上限。",
            file=sys.stderr,
        )
        return 1

    print(f"开工必读集体量预算：合计 {total}B ≤ {TOTAL_BUDGET_BYTES}B（{detail}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
