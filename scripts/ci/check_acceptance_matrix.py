#!/usr/bin/env python3
"""校验验收矩阵的状态列与合同条款覆盖清单（见 docs/技术设计/验证与门禁.md 的 10.1 / 10.3）。

只读两份 Markdown，不访问网络、不依赖标准库以外的东西。它挡的是两类**沉默**的漏洞：

1. **断言无人认领**：矩阵里加了一条断言却没写状态、状态留空、或写成三态以外的词
   （例如把 Issue 正文用的"待实现"抄进来）。没有这道检查时，这条断言不属于任何切片，
   谁都不必为它负责，而门禁照样全绿。
2. **合同条款无断言覆盖**：产品合同新增或改名一节而没有登记到覆盖清单，或某一节的
   断言编号被清空、写错。规则先加、断言忘了加是这类缺口最常见的形态。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
GATE_DOCUMENT = REPOSITORY_ROOT / "docs" / "技术设计" / "验证与门禁.md"
CONTRACT_DOCUMENT = REPOSITORY_ROOT / "docs" / "产品合同与外部边界.md"

# 10.1 定义的三态。状态列只允许这三个词，多一个同义词就等于多一套不受检查的语义。
ASSERTION_STATES = ("未认领", "已认领", "已验证")
MATRIX_HEADER = ("#", "可验证断言", "层级", "状态")
COVERAGE_HEADER = ("合同章节", "对应断言", "说明")
NO_ASSERTION = "无对应断言"
EMPTY_NOTE = "—"

ASSERTION_ID = re.compile(r"^V-[一-鿿]+-\d{2}$")
ASSERTION_RANGE = re.compile(r"^(V-[一-鿿]+)-(\d{2})…(\d{2})$")
# 编号列以 V- 开头的行都必须是合格的断言行；用宽匹配捕获，再逐条校验，
# 否则写坏的编号（例如把补充条款写进编号格）会直接从检查里消失。
LOOSE_ASSERTION_ROW = re.compile(r"^V-")
SEPARATOR_ROW = re.compile(r"^\|[\s:|-]+\|$")
HEADING = re.compile(r"^(#{2,3})\s+(.+?)\s*$")


def split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def is_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 1


def iter_tables(text: str):
    """产出 (表头 tuple, 行号, 单元格 list)，自动跳过围栏代码块。

    10.2 的 Issue 正文模板里就有一张示例矩阵（状态写的是"待实现"），它在围栏里，
    是文档示例不是登记表——不跳过围栏，这份检查从第一次运行就会误报。
    """
    header: tuple[str, ...] | None = None
    in_fence = False
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            header = None
            continue
        if in_fence:
            continue
        if not is_table_row(line):
            header = None
            continue
        if SEPARATOR_ROW.match(line.strip()):
            continue
        following = lines[index + 1] if index + 1 < len(lines) else ""
        if header is None and SEPARATOR_ROW.match(following.strip()):
            header = tuple(split_row(line))
            continue
        yield header, index + 1, split_row(line)


def parse_matrix(text: str) -> tuple[dict[str, str], list[str]]:
    """读出「断言编号 → 状态」，并报出所有不合格的断言行。"""
    statuses: dict[str, str] = {}
    seen_at: dict[str, int] = {}
    errors: list[str] = []

    for header, line_number, cells in iter_tables(text):
        in_matrix = header == MATRIX_HEADER
        if not in_matrix and not (cells and LOOSE_ASSERTION_ROW.match(cells[0])):
            continue
        if not in_matrix:
            errors.append(
                f"第 {line_number} 行：断言 {cells[0]!r} 所在的表没有「状态」列表头"
                f"（应为 {' | '.join(MATRIX_HEADER)}）"
            )
            continue
        if len(cells) != len(MATRIX_HEADER):
            errors.append(
                f"第 {line_number} 行：断言行有 {len(cells)} 格，应为 {len(MATRIX_HEADER)} 格"
                f"（缺状态列？）：{cells[0]!r}"
            )
            continue

        identifier, _, _, state = cells
        if not ASSERTION_ID.match(identifier):
            errors.append(f"第 {line_number} 行：编号格不是合法断言编号：{identifier!r}")
            continue
        if identifier in seen_at:
            errors.append(f"第 {line_number} 行：断言编号重复，已见于第 {seen_at[identifier]} 行：{identifier}")
            continue
        if state not in ASSERTION_STATES:
            errors.append(
                f"第 {line_number} 行：{identifier} 的状态是 {state!r}，"
                f"只允许 {' / '.join(ASSERTION_STATES)}"
            )
            continue
        statuses[identifier] = state
        seen_at[identifier] = line_number

    if not statuses and not errors:
        errors.append("验收矩阵里一条断言都没解析到：表头或表格结构被改动了")
    return statuses, errors


def expand_reference(reference: str) -> tuple[list[str], str | None]:
    """把一个引用展开成断言编号列表；`V-管理-01…18` 这类区间逐个展开。"""
    if ASSERTION_ID.match(reference):
        return [reference], None
    range_match = ASSERTION_RANGE.match(reference)
    if not range_match:
        return [], f"不是合法的断言编号或区间：{reference!r}"
    group, start, end = range_match.groups()
    if int(start) >= int(end):
        return [], f"区间的起止顺序不对：{reference!r}"
    return [f"{group}-{number:02d}" for number in range(int(start), int(end) + 1)], None


def parse_coverage(text: str) -> tuple[dict[str, tuple[list[str], str]], list[str]]:
    """读出「合同章节 → (断言编号, 说明)」。"""
    coverage: dict[str, tuple[list[str], str]] = {}
    errors: list[str] = []
    found_table = False

    for header, line_number, cells in iter_tables(text):
        if header != COVERAGE_HEADER:
            continue
        found_table = True
        if len(cells) != len(COVERAGE_HEADER):
            errors.append(f"第 {line_number} 行：覆盖清单行有 {len(cells)} 格，应为 {len(COVERAGE_HEADER)} 格")
            continue

        section, raw_references, note = cells
        if not section:
            errors.append(f"第 {line_number} 行：合同章节为空")
            continue
        if section in coverage:
            errors.append(f"第 {line_number} 行：合同章节在覆盖清单里重复：{section}")
            continue

        if raw_references == NO_ASSERTION:
            if not note or note == EMPTY_NOTE:
                errors.append(
                    f"第 {line_number} 行：「{section}」写了 {NO_ASSERTION} 却没有说明理由；"
                    "不允许静默宣布某节不需要断言"
                )
            coverage[section] = ([], note)
            continue

        if not raw_references:
            errors.append(f"第 {line_number} 行：「{section}」没有任何对应断言，也没有写 {NO_ASSERTION}")
            coverage[section] = ([], note)
            continue

        identifiers: list[str] = []
        for reference in (part.strip() for part in raw_references.split("、")):
            if not reference:
                errors.append(f"第 {line_number} 行：「{section}」的断言列表里有空项")
                continue
            expanded, error = expand_reference(reference)
            if error:
                errors.append(f"第 {line_number} 行：「{section}」{error}")
                continue
            for identifier in expanded:
                if identifier in identifiers:
                    errors.append(f"第 {line_number} 行：「{section}」重复引用 {identifier}")
                    continue
                identifiers.append(identifier)
        coverage[section] = (identifiers, note)

    if not found_table:
        errors.append(f"没找到合同条款覆盖清单（表头应为 {' | '.join(COVERAGE_HEADER)}）")
    return coverage, errors


def contract_sections(text: str) -> list[str]:
    sections = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        heading = HEADING.match(line)
        if heading:
            sections.append(heading.group(2))
    return sections


def cross_check(
    statuses: dict[str, str],
    coverage: dict[str, tuple[list[str], str]],
    sections: list[str],
) -> list[str]:
    errors: list[str] = []

    if not sections:
        errors.append("产品合同里一个章节标题都没解析到")
    duplicates = sorted({name for name in sections if sections.count(name) > 1})
    if duplicates:
        errors.append(f"产品合同存在同名章节，无法逐节登记：{'、'.join(duplicates)}")

    missing = [name for name in sections if name not in coverage]
    if missing:
        errors.append(
            "产品合同的以下章节没有登记到覆盖清单（新增规则时必须同时登记它由哪些断言承担）："
            + "、".join(missing)
        )
    unknown = sorted(set(coverage) - set(sections))
    if unknown:
        errors.append("覆盖清单登记了产品合同里不存在的章节（章节改名后未同步？）：" + "、".join(unknown))

    for section, (identifiers, _) in coverage.items():
        for identifier in identifiers:
            if identifier not in statuses:
                errors.append(f"「{section}」引用了验收矩阵里不存在的断言：{identifier}")
    return errors


def main() -> int:
    gate_text = GATE_DOCUMENT.read_text(encoding="utf-8")
    contract_text = CONTRACT_DOCUMENT.read_text(encoding="utf-8")

    statuses, failures = parse_matrix(gate_text)
    coverage, coverage_errors = parse_coverage(gate_text)
    failures = failures + coverage_errors
    sections = contract_sections(contract_text)
    failures = failures + cross_check(statuses, coverage, sections)

    if failures:
        print("验收矩阵与合同覆盖清单检查失败：", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    counted = "，".join(
        f"{state} {sum(1 for value in statuses.values() if value == state)}" for state in ASSERTION_STATES
    )
    covered = sum(1 for identifiers, _ in coverage.values() if identifiers)
    print(f"验收矩阵状态列：通过（{len(statuses)} 条断言；{counted}）")
    print(f"合同条款覆盖清单：通过（{len(coverage)} 个章节，其中 {covered} 个已映射到断言）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
