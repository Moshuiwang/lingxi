#!/usr/bin/env python3
"""开工必读集体量预算（产品负责人 2026-08-24 批准的防膨胀门禁）。

`AGENTS.md` 把「实现或修改正式代码前必读」定为代码框架 + 验证与门禁两份文档。
本检查给这两份文档的合计字节数设硬上限：超限即红，逼迫瘦身而不是继续堆积——
与代码的体量棘轮（check_size_ratchet.py）同一思路。历史教训：这两份文档曾分别
长到 53KB 与 18.5KB，其中大半是住错地方的实现编年史（2026-08-24 维护批实测）。

本检查还并列维护一组**单文件独立上限**（``PER_FILE_BUDGET_BYTES``）：那些不属于
开工必读集、但同样会无声膨胀的长文档各自单独封顶，互不共享余量，超限只指名超限的
那一份。两组预算每次都跑完再决定退出码，任一超限即非零。

上限调整（两组都算）必须与实际瘦身 / 扩容一起发生在同一次改动里，且留下可审阅的 diff；
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
# 上调到 40KB：预算文件从两份扩为三份，新增的 `代码规范.md` 当时实测约 4KB，上调幅度
# 就是照它的体量给的。它与前两份**共享这一个合计上限**——本模块从未给它单列过独立
# 上限，原注释写成「独立 4KB 硬顶」是笔误；要单独封顶得显式写进 PER_FILE_BUDGET_BYTES。
TOTAL_BUDGET_BYTES = 40 * 1024

# 单文件独立上限（字节）。与上面的「开工必读集合计预算」**并列且互不共享余量**：
# 这两份不属于开工必读集，各自单独封顶，一份超限只指名那一份。
#
# 2026-09-06 新登记两份（Issue #598 第二批「现值与沿革分离」/ Trace #624 Step S-3d /
# 合同裁定项 G-7）。按本模块 docstring 写死的规则，这两个数字与实际瘦身发生在
# **同一次改动、同一个 PR**，diff 可审阅；不接受"先抬上限再慢慢写"：
#
# - docs/当前能力.md = 54 * 1024（55296B）。本批把 20 条带日期的沿革条目整体移入
#   docs/参考证据/能力沿革记录.md，该文件从 100210B 瘦到 51311B（−48.8%，S-3a/S-3b），
#   S-3d 本次措辞纠正后实测 51414B，上限按实测留约 3882B（7.0%）余量。G-7 裁定
#   「按实测留小余量」——刻意不留大空间，下一次扩容仍须自证理由。
# - docs/产品合同与外部边界.md = 74461B（**零余量**，刻意不写成 K 的整数倍）。本批
#   验收明写「只加不增长棘轮、结构不改，以当前 74461B 封顶」：这份文件是对用户的
#   承诺正文，结构不动、只许缩不许涨，任何一个字节的增长都必须与实际瘦身同批发生
#   并在这里带理由改这个数。
#
#   2026-09-07 抬到 75473B（+1012B，Trace #606）：新增的不是沿革也不是实现细节，
#   是一条**外部平台约束**——机器人只能向曾经与它对话过的用户发起单聊，因此
#   「预开通完成即主动收到欢迎卡」对新用户不成立（证据见
#   docs/参考证据/飞书机器人主动私聊的跨组织前提.md）。这条前提直接改变承诺的成立
#   范围，不写进承诺正文就等于让合同继续承诺一件平台不允许的事，没有别处可放。
#   零余量规则照旧：抬到的就是实测值，下一次增长仍要在这里自证。措辞已由产品负责人
#   2026-09-08 确认（Trace #606 决策清单 Z-5 已裁定）。
#
#   2026-09-11 抬到 76451B（+978B，Trace #732 W4，Issue #673）：新增的是两条**对用户
#   的承诺**——①主动告知的结果会被记住：成功记「说过话」，明确失败记「联系不上」并
#   转管理群待办、下次开口前不再主动发送；②九十天上限的第二条例外：用户档案上的
#   入站时间与不可达结论是继续服务所需的当前状态、不按九十天删除。第二条是产品负责人
#   2026-09-10 逐条裁定同意新增的保留例外（#673 评论），不写进承诺正文就等于合同仍
#   承诺「一切可识别内容九十天内删除」而实现已经不是。同批未瘦身：Trace #732 合同
#   §4 明写本批对该文件的回写「允许抬上限，但必须同批并写明理由」。抬到的仍是实测值。
#
# docs/当前能力.md 下调到 35 * 1024（35840B）：此前余量只剩 15B，任何一句新事实都
# 写不进去。本批把六个域小节里与 docs/参考证据/能力沿革记录.md 重复的叙事细节
# （怎么做的、换过哪次机制）收窄为「现值+证据等级+已知边界」，只保留会影响用户
# 预期的结论；新出现、沿革记录里还没有位置的三批事件（载体到期清理四面接线、
# 体验批热修与预开通收口、版本维护分支首次提升）整段移入沿革记录，各自新开日期
# 小节。同批订正了一处与 deploy/compose.prod.yaml:72 相反的记载：该行注释显示
# gateway 的 runtime-config 挂载已在 2026-09-05 由 #585 补齐，不再是「scheduler
# 已接、gateway 未接」。瘦身后实测 33844B，上限按实测留约 1996B（5.9%）余量，
# 延续「按实测留小余量、下一次扩容仍须自证理由」的既定规则。
PER_FILE_BUDGET_BYTES = {
    "docs/当前能力.md": 35 * 1024,
    "docs/产品合同与外部边界.md": 76451,
}


def check_reading_set_budget(root: Path) -> int:
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


def check_per_file_budgets(root: Path) -> int:
    """逐文件核对 PER_FILE_BUDGET_BYTES：超限只指名超限的那一份，余量不共享。"""
    missing: list[str] = []
    sizes: list[tuple[str, int, int]] = []
    for relative, budget in PER_FILE_BUDGET_BYTES.items():
        path = root / relative
        if not path.is_file():
            missing.append(relative)
            continue
        sizes.append((relative, path.stat().st_size, budget))

    if missing:
        print(
            "文档单文件体量上限：以下受上限约束的文件不存在（改名或删除时必须同步更新本检查）：",
            file=sys.stderr,
        )
        for relative in missing:
            print(f"  - {relative}", file=sys.stderr)
        return 1

    over = [(relative, size, budget) for relative, size, budget in sizes if size > budget]
    if over:
        print("文档单文件体量上限超限：", file=sys.stderr)
        for relative, size, budget in over:
            print(
                f"  - {relative}={size}B > 上限 {budget}B（超出 {size - budget}B）",
                file=sys.stderr,
            )
        print(
            "请瘦身文档（沿革与实现编年史移到参考证据 / Issue / PR），"
            "或在同一改动里带理由调整上限。",
            file=sys.stderr,
        )
        return 1

    detail = "、".join(f"{relative}={size}B ≤ {budget}B" for relative, size, budget in sizes)
    print(f"文档单文件体量上限：{detail}")
    return 0


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    # 两组预算各自独立，都要跑完再决定退出码：一次运行把所有超限项一起报出来，
    # 不让第一组失败掩盖第二组的问题。有任一超限即非零。
    failures = check_reading_set_budget(root) + check_per_file_budgets(root)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
