#!/usr/bin/env python3
"""Issue 分类与状态标签维持的只读自检（Issue #642）：先只报不拦。

`docs/协作约定.md`「Issue 分类与状态标签维持」把 GitHub Issue 分成两类：**工作项**
（`[task]`/`[story]`/`[feature]`/`[bug]`/`[epic]`/`[research]`，恰好一个状态标签）
和**不进状态链**的类别（`[tracking]` Trace、`[template]`／`长期参考`、`[board]`）。
GitHub 本身不保证这条互斥；本脚本只做四件机械的事，把违反项列成一张待修正清单：

1. 工作项类 Issue 的状态标签数（`待分诊`/`pre-ready`/`Ready`/`执行中`/`阻塞`/`待决策`）
   不等于 1。
2. `长期参考` 与任一状态标签共存（两者按新规则互斥，无论 Issue 属于哪个类别）。
3. 带 `执行中` 标签、且最近一条领取留言写明的有效期已过（提示回收）——**有效期是自由
   文本**，本检查只做尽力而为的日期提取，提取不到时明确标「无法自动判断」，不当作
   通过。
4. 开放 Issue 完全没有可识别的类型标签（`变更`/`缺陷`/`维护`/`研究`/`ops`/
   `documentation`/`bug` 一个都没有）。

本检查**先只报不拦**：无论命中多少异常、或调用 `gh` 本身失败，退出码恒为 0——它是
巡检工具，不是合并门禁；跑一段时间确认误报可控后再谈是否升级（#642 三、机器兜底）。
需要本机已登录 `gh` 且能访问 GitHub API；不可用时打印原因并照常以 0 退出，不假装
检查通过。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

REPOSITORY = "Moshuiwang/lingxi"

STATE_LABELS = {"待分诊", "pre-ready", "Ready", "执行中", "阻塞", "待决策"}
LONG_TERM_REFERENCE_LABEL = "长期参考"
TYPE_LABELS = {"变更", "缺陷", "维护", "研究", "ops", "documentation", "bug"}

# 标题方括号字段 → 是否属于「工作项」（进状态链，恰好一个状态标签）。
# `docs/协作约定.md`「Issue 标题字段」以英文为准（2026-08-06 起），但仓库里仍有
# 早于该约定的中文方括号（如「[缺陷]」），一并识别，不因写法新旧漏检。
WORK_ITEM_BRACKETS = {
    "task",
    "story",
    "feature",
    "bug",
    "epic",
    "research",
    "decision",
    "ops",
    "缺陷",
}
NON_CHAIN_BRACKETS = {"tracking", "template", "board"}

TITLE_BRACKET_PATTERN = re.compile(r"^\[([^\]]+)\]")

# 尽力而为地从领取留言里提取「有效期」后面的一个日期/时间片段；写法不受约束，
# 提取不到不算通过，只代表本检查这次没读懂，交由人工确认。
CLAIM_DEADLINE_PATTERN = re.compile(
    r"有效期[:：\s]*[0-9\-年月日 T:：~–—]*?"
    r"(\d{4}-\d{2}-\d{2}(?:[T ]\d{1,2}[:：]\d{2})?)"
)


@dataclass
class Issue:
    number: int
    title: str
    labels: list[str]

    @property
    def bracket(self) -> str | None:
        match = TITLE_BRACKET_PATTERN.match(self.title.strip())
        return match.group(1) if match else None

    @property
    def state_labels(self) -> list[str]:
        return [label for label in self.labels if label in STATE_LABELS]

    @property
    def is_long_term_reference(self) -> bool:
        return LONG_TERM_REFERENCE_LABEL in self.labels

    @property
    def has_type_label(self) -> bool:
        return any(label in TYPE_LABELS for label in self.labels)


def run_gh_json(args: list[str]) -> object:
    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"`gh {' '.join(args)}` 失败（退出码 {result.returncode}）：{result.stderr.strip()}"
        )
    return json.loads(result.stdout)


def fetch_open_issues() -> list[Issue]:
    raw = run_gh_json(
        [
            "issue",
            "list",
            "--repo",
            REPOSITORY,
            "--state",
            "open",
            "--json",
            "number,title,labels",
            "--limit",
            "200",
        ]
    )
    issues = []
    for item in raw:
        labels = [label["name"] for label in item["labels"]]
        issues.append(Issue(number=item["number"], title=item["title"], labels=labels))
    return issues


def fetch_latest_claim_deadline(issue_number: int) -> str | None:
    """尽力而为：在评论里找「有效期」后面最近一次出现的日期，找不到返回 None。"""
    raw = run_gh_json(
        [
            "issue",
            "view",
            str(issue_number),
            "--repo",
            REPOSITORY,
            "--json",
            "comments",
        ]
    )
    deadlines: list[str] = []
    for comment in raw.get("comments", []):
        for match in CLAIM_DEADLINE_PATTERN.finditer(comment.get("body", "")):
            deadlines.append(match.group(1))
    return deadlines[-1] if deadlines else None


def parse_deadline(raw_deadline: str) -> datetime | None:
    for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw_deadline, pattern).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def check_state_label_count(issues: list[Issue]) -> list[str]:
    findings = []
    for issue in issues:
        bracket = issue.bracket
        if bracket not in WORK_ITEM_BRACKETS:
            continue
        if issue.is_long_term_reference:
            continue  # 由 check_long_term_reference_conflict 单独报告，不重复计数
        count = len(issue.state_labels)
        if count != 1:
            findings.append(
                f"#{issue.number} 状态标签数={count}（{issue.state_labels or '无'}），"
                f"工作项类（[{bracket}]）须恰好一个"
            )
    return findings


def check_long_term_reference_conflict(issues: list[Issue]) -> list[str]:
    findings = []
    for issue in issues:
        if issue.is_long_term_reference and issue.state_labels:
            findings.append(
                f"#{issue.number} 同时带 `长期参考` 与状态标签 {issue.state_labels}，二者互斥，需二选一"
            )
    return findings


def check_expired_claims(issues: list[Issue]) -> list[str]:
    findings = []
    now = datetime.now(UTC)
    for issue in issues:
        if "执行中" not in issue.labels:
            continue
        try:
            raw_deadline = fetch_latest_claim_deadline(issue.number)
        except RuntimeError as error:
            findings.append(f"#{issue.number} 处于执行中，读取领取留言失败：{error}")
            continue
        if raw_deadline is None:
            findings.append(
                f"#{issue.number} 处于执行中，未能从评论提取『有效期』，需人工确认是否已过期"
            )
            continue
        deadline = parse_deadline(raw_deadline)
        if deadline is None:
            findings.append(
                f"#{issue.number} 处于执行中，提取到有效期文本『{raw_deadline}』但无法解析为日期，需人工确认"
            )
        elif deadline < now:
            findings.append(f"#{issue.number} 处于执行中，领取有效期 {raw_deadline} 已过，提示回收")
    return findings


def check_missing_type_label(issues: list[Issue]) -> list[str]:
    return [
        f"#{issue.number} 无任何类型标签（{TYPE_LABELS} 一个都没有）"
        for issue in issues
        if not issue.has_type_label
    ]


def main() -> int:
    print("Issue 分类与状态标签维持自检（先只报不拦，退出码恒为 0）")
    try:
        issues = fetch_open_issues()
    except (
        RuntimeError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
        json.JSONDecodeError,
    ) as error:
        print(f"无法读取开放 Issue 列表，本次检查跳过：{error}", file=sys.stderr)
        return 0

    findings = {
        "工作项状态标签数≠1": check_state_label_count(issues),
        "长期参考与状态标签共存": check_long_term_reference_conflict(issues),
        "执行中且领取有效期可能已过": check_expired_claims(issues),
        "开放 Issue 零类型标签": check_missing_type_label(issues),
    }

    total = sum(len(items) for items in findings.values())
    if total == 0:
        print(f"共检查 {len(issues)} 个开放 Issue，未发现异常。")
        return 0

    print(f"共检查 {len(issues)} 个开放 Issue，发现 {total} 项待修正：")
    for category, items in findings.items():
        if not items:
            continue
        print(f"\n[{category}]（{len(items)} 项）")
        for item in items:
            print(f"  - {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
