#!/usr/bin/env python3
"""校验验收矩阵的状态列与合同条款覆盖清单（见 docs/技术设计/验收矩阵.md 及其分册）。

**读取范围（Issue #479 分册后）**：矩阵从「一个文件」变成「总册 `验收矩阵.md`
+ 同目录全部 `验收矩阵-*.md` 分册」。本检查按这个集合**整体**解析，判定标准一个
字没改：状态三态不变、断言编号唯一性不变（跨分册重号照样判红）、合同章节覆盖
跨查不变。分册用**目录扫描**发现而不是写死清单——写死清单意味着新增一册忘了登
记就会静默脱离检查，而这正是本检查存在的理由。扫描到的每一册还必须能在总册的
分册索引里被链接到，否则判红：磁盘上有、总册导航里没有的分册，读者找不到它。

只读 Markdown（矩阵集合 + 产品合同），不访问网络、不依赖标准库以外的东西。它挡的是两类**沉默**的漏洞：

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
MATRIX_DOCUMENT = REPOSITORY_ROOT / "docs" / "技术设计" / "验收矩阵.md"
# 分册命名空间：与总册同目录、同前缀。改这个 glob 等于改读取范围，须与总册
# 「体量预算」小节同步。
MATRIX_VOLUME_GLOB = "验收矩阵-*.md"
CONTRACT_DOCUMENT = REPOSITORY_ROOT / "docs" / "产品合同与外部边界.md"


def matrix_volumes() -> list[Path]:
    """总册同目录下的全部分册，按文件名排序（稳定的报错顺序）。"""
    return sorted(MATRIX_DOCUMENT.parent.glob(MATRIX_VOLUME_GLOB))


def matrix_documents() -> dict[str, str]:
    """「显示名 → 正文」：总册在前，分册按文件名排序跟在后面。"""
    documents = {MATRIX_DOCUMENT.name: MATRIX_DOCUMENT.read_text(encoding="utf-8")}
    for volume in matrix_volumes():
        documents[volume.name] = volume.read_text(encoding="utf-8")
    return documents


def as_documents(text_or_documents: str | dict[str, str]) -> dict[str, str]:
    """允许单份正文直接传入（单元测试与单文件调用），统一成「显示名 → 正文」。"""
    if isinstance(text_or_documents, str):
        return {MATRIX_DOCUMENT.name: text_or_documents}
    return text_or_documents


# 验收矩阵定义的三态。状态列只允许这三个词，多一个同义词就等于多一套不受检查的语义。
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
# 断言正文里出现字面竖线是合法的（正则、命令行管道），Markdown 用 `\|` 转义。
# 不认这个转义就会把一行切成 5 格，然后报"缺状态列"——错的地方和错的原因都不对。
UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")
ESCAPED_PIPE = re.compile(r"\\\|")


def split_row(line: str) -> list[str]:
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|") and not inner.endswith("\\|"):
        inner = inner[:-1]
    return [ESCAPED_PIPE.sub("|", cell.strip()) for cell in UNESCAPED_PIPE.split(inner)]


def is_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 1


def iter_tables(text: str):
    """产出 (表头 tuple, 行号, 单元格 list)，自动跳过围栏代码块。

    文档里的 Issue 正文模板可能包含示例矩阵，它在围栏里，
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


def parse_matrix(text_or_documents: str | dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """读出「断言编号 → 状态」，并报出所有不合格的断言行。

    入参是「显示名 → 正文」的多份文档（总册 + 分册）；只传一份正文时按单文档处理。
    断言编号的唯一性在**整个集合**里判定，跨分册重号与同一份文档内重号同样判红。
    """
    documents = as_documents(text_or_documents)
    statuses: dict[str, str] = {}
    seen_at: dict[str, str] = {}
    errors: list[str] = []

    for source, text in documents.items():
        for header, line_number, cells in iter_tables(text):
            where = f"{source} 第 {line_number} 行"
            in_matrix = header == MATRIX_HEADER
            if not in_matrix and not (cells and LOOSE_ASSERTION_ROW.match(cells[0])):
                continue
            if not in_matrix:
                errors.append(
                    f"{where}：断言 {cells[0]!r} 所在的表没有「状态」列表头"
                    f"（应为 {' | '.join(MATRIX_HEADER)}）"
                )
                continue
            if len(cells) != len(MATRIX_HEADER):
                errors.append(
                    f"{where}：断言行有 {len(cells)} 格，应为 {len(MATRIX_HEADER)} 格；"
                    f"若正文需要字面竖线请写成 \\| 转义：{cells[0]!r}"
                )
                continue

            identifier, _, _, state = cells
            if not ASSERTION_ID.match(identifier):
                errors.append(f"{where}：编号格不是合法断言编号：{identifier!r}")
                continue
            if identifier in seen_at:
                errors.append(f"{where}：断言编号重复，已见于{seen_at[identifier]}：{identifier}")
                continue
            if state not in ASSERTION_STATES:
                errors.append(
                    f"{where}：{identifier} 的状态是 {state!r}，"
                    f"只允许 {' / '.join(ASSERTION_STATES)}"
                )
                continue
            statuses[identifier] = state
            seen_at[identifier] = where

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


def parse_coverage(
    text_or_documents: str | dict[str, str],
) -> tuple[dict[str, tuple[list[str], str]], list[str]]:
    """读出「合同章节 → (断言编号, 说明)」。

    分册后覆盖清单仍然只有一份（住在总册），但仍按整个文档集合扫描：清单被搬到某个
    分册、或有人在分册里写出第二份，都必须被看见，而不是因为「只读总册」静默漏掉。
    """
    documents = as_documents(text_or_documents)
    coverage: dict[str, tuple[list[str], str]] = {}
    errors: list[str] = []
    found_table = False

    for source, text in documents.items():
        for header, line_number, cells in iter_tables(text):
            if header != COVERAGE_HEADER:
                continue
            where = f"{source} 第 {line_number} 行"
            found_table = True
            if len(cells) != len(COVERAGE_HEADER):
                errors.append(
                    f"{where}：覆盖清单行有 {len(cells)} 格，应为 {len(COVERAGE_HEADER)} 格"
                )
                continue

            section, raw_references, note = cells
            if not section:
                errors.append(f"{where}：合同章节为空")
                continue
            if section in coverage:
                errors.append(f"{where}：合同章节在覆盖清单里重复：{section}")
                continue

            if raw_references == NO_ASSERTION:
                if not note or note == EMPTY_NOTE:
                    errors.append(
                        f"{where}：「{section}」写了 {NO_ASSERTION} 却没有说明理由；"
                        "不允许静默宣布某节不需要断言"
                    )
                coverage[section] = ([], note)
                continue

            if not raw_references:
                errors.append(f"{where}：「{section}」没有任何对应断言，也没有写 {NO_ASSERTION}")
                coverage[section] = ([], note)
                continue

            identifiers: list[str] = []
            for reference in (part.strip() for part in raw_references.split("、")):
                if not reference:
                    errors.append(f"{where}：「{section}」的断言列表里有空项")
                    continue
                expanded, error = expand_reference(reference)
                if error:
                    errors.append(f"{where}：「{section}」{error}")
                    continue
                for identifier in expanded:
                    if identifier in identifiers:
                        errors.append(f"{where}：「{section}」重复引用 {identifier}")
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
        errors.append(
            "覆盖清单登记了产品合同里不存在的章节（章节改名后未同步？）：" + "、".join(unknown)
        )

    for section, (identifiers, _) in coverage.items():
        for identifier in identifiers:
            if identifier not in statuses:
                errors.append(f"「{section}」引用了验收矩阵里不存在的断言：{identifier}")
    return errors


def check_volume_registry(documents: dict[str, str]) -> list[str]:
    """每个磁盘上的分册都必须在总册的分册索引里被链接到。

    分册靠目录扫描发现，所以「加了文件忘了登记」不会让断言脱离检查；但读者仍然会
    找不到它。这条检查补上导航面：总册正文里必须出现指向该分册的 Markdown 链接。
    """
    hub_text = documents.get(MATRIX_DOCUMENT.name, "")
    return [
        f"分册 {name} 没有登记在总册 {MATRIX_DOCUMENT.name} 的分册索引里"
        f"（总册正文需要出现指向它的 Markdown 链接 `]({name})`）"
        for name in documents
        if name != MATRIX_DOCUMENT.name and f"]({name})" not in hub_text
    ]


def main() -> int:
    documents = matrix_documents()
    contract_text = CONTRACT_DOCUMENT.read_text(encoding="utf-8")

    statuses, failures = parse_matrix(documents)
    coverage, coverage_errors = parse_coverage(documents)
    failures = failures + coverage_errors + check_volume_registry(documents)
    sections = contract_sections(contract_text)
    failures = failures + cross_check(statuses, coverage, sections)

    if failures:
        print("验收矩阵与合同覆盖清单检查失败：", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    counted = "，".join(
        f"{state} {sum(1 for value in statuses.values() if value == state)}"
        for state in ASSERTION_STATES
    )
    covered = sum(1 for identifiers, _ in coverage.values() if identifiers)
    volumes = len(documents) - 1
    print(f"验收矩阵状态列：通过（{len(statuses)} 条断言；{counted}；总册 + {volumes} 个分册）")
    print(f"合同条款覆盖清单：通过（{len(coverage)} 个章节，其中 {covered} 个已映射到断言）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
