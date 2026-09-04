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
    "docs/技术设计/代码规范.md",
)

# 合计上限（字节）。2026-08-24 瘦身后两文件合计实测 29719B（瘦身 + 金字塔频率
# 纪律小节），上限按实测留约 10% 余量定为 32KB——按本脚本自己的规则，该数字
# 与瘦身/扩容在同一批调整并留下理由（opus 审查 P2-3）。
#
# 2026-09-01（Issue #520 F2/F3，rc24 正式上线批）上调到 35KB：本批在「验证与门禁」
# 新增「已知边界与上线后项」小节（+1794B），登记 L1 轻量档停用与 L3 影响面
# registration 降级两条边界——两条都是当前有效事实，不是住错地方的实现编年史。
# 调整前两文件合计 32741B，距 32KB 上限只剩 27B，任何一句话都写不进去。新上限按
# 调整后实测 34535B 留约 1.3KB 余量：**刻意不留大空间**，下一次扩容仍须自证理由。
#
# 上调到 40KB：预算文件从两份扩为三份，新增 `代码规范.md` 自身有独立的 4KB 硬顶，
# 不与前两份共享余量。
TOTAL_BUDGET_BYTES = 40 * 1024


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
        print(
            "开工必读集体量预算：以下受预算约束的文件不存在（改名或删除时必须同步更新本检查）：",
            file=sys.stderr,
        )
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
