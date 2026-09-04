#!/usr/bin/env python3
"""只读的可读性体检报表——不进任何门禁，供批次前后人工对照读数。

打印一棵目录树（默认 ``src/lingxi``）的：文件数、总行数、纯代码／注释＋
docstring／空行占比；函数总数与 > 40 / > 60 行个数（与
``check_function_size_ratchet.py`` 同口径：AST 遍历 ``FunctionDef``/
``AsyncFunctionDef``，长度 = ``end_lineno - lineno + 1``，不含装饰器）；
docstring > 10 行个数；连续 ``#`` 注释块 > 5 行个数；来历标记命中的行数与
文件数（与 ``check_comment_ratchet.py`` 同一份 ``PROVENANCE_PATTERN``）。

``--root <目录>`` 支持指向任意目录（不要求在本仓库内），用于对旧树/别的
分支做前后对照——因此本文件刻意不依赖两条棘轮脚本里那些绑定
``REPOSITORY_ROOT`` 的函数，只复用它们的纯计算部分（正则、AST 遍历逻辑），
自己驱动一遍独立的文件遍历。

不写基线、不判红、不影响任何门禁退出码——纯粹的读数工具。
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import io
import sys
import tokenize
from pathlib import Path


def _load_provenance_pattern():
    """按路径装载 ``check_comment_ratchet.py`` 取它的正则常量。

    ``scripts/ci/`` 不是一个包（没有 ``__init__.py``），仓库既有测试一律用
    ``importlib.util.spec_from_file_location`` 按路径直接装载，这里沿用同一
    先例，不新增 ``sys.path`` 改动的写法。
    """

    script = Path(__file__).resolve().parents[1] / "ci" / "check_comment_ratchet.py"
    spec = importlib.util.spec_from_file_location("check_comment_ratchet_for_report", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.PROVENANCE_PATTERN


PROVENANCE_PATTERN = _load_provenance_pattern()

FUNCTION_THRESHOLDS = (40, 60)
DOCSTRING_THRESHOLD_LINES = 10
HASH_BLOCK_THRESHOLD_LINES = 5


def _iter_functions(tree: ast.Module) -> list[tuple[int, int]]:
    """返回全部函数的 (lineno, end_lineno)，含方法与任意深度嵌套。"""

    spans: list[tuple[int, int]] = []

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.end_lineno is not None:
                    spans.append((child.lineno, child.end_lineno))
                walk(child)
            else:
                walk(child)

    walk(tree)
    return spans


def _iter_docstrings(tree: ast.Module) -> list[tuple[int, int]]:
    """返回全部 docstring（模块/类/函数）的 (lineno, end_lineno)。"""

    spans: list[tuple[int, int]] = []

    def leading(body: list[ast.stmt]) -> None:
        if not body:
            return
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
            and first.end_lineno is not None
        ):
            spans.append((first.lineno, first.end_lineno))

    leading(tree.body)

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                leading(child.body)
                walk(child)
            else:
                walk(child)

    walk(tree)
    return spans


def _pure_comment_line_numbers(source: str) -> set[int]:
    lines = source.splitlines()
    pure: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type != tokenize.COMMENT:
                continue
            row, col = tok.start
            if row - 1 < len(lines) and lines[row - 1][:col].strip() == "":
                pure.add(row)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return pure
    return pure


def _all_comment_texts(source: str) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                result.append((tok.start[0], tok.string))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return result
    return result


def _count_blocks_over(pure_comment_lines: set[int], threshold: int) -> int:
    if not pure_comment_lines:
        return 0
    ordered = sorted(pure_comment_lines)
    blocks = 0
    run_length = 1
    for previous, current in zip(ordered, ordered[1:]):
        if current == previous + 1:
            run_length += 1
            continue
        if run_length > threshold:
            blocks += 1
        run_length = 1
    if run_length > threshold:
        blocks += 1
    return blocks


class Report:
    def __init__(self) -> None:
        self.file_count = 0
        self.total_lines = 0
        self.code_lines = 0
        self.comment_and_docstring_lines = 0
        self.blank_lines = 0
        self.function_count = 0
        self.function_over_40 = 0
        self.function_over_60 = 0
        self.docstring_over_10 = 0
        self.hash_block_over_5 = 0
        self.provenance_line_count = 0
        self.provenance_file_count = 0

    def add_file(self, path: Path) -> None:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return
        lines = source.splitlines()
        self.file_count += 1
        self.total_lines += len(lines)

        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            return

        docstring_spans = _iter_docstrings(tree)
        docstring_line_numbers: set[int] = set()
        for lineno, end_lineno in docstring_spans:
            for line_number in range(lineno, end_lineno + 1):
                docstring_line_numbers.add(line_number)
            if (end_lineno - lineno + 1) > DOCSTRING_THRESHOLD_LINES:
                self.docstring_over_10 += 1

        for lineno, end_lineno in _iter_functions(tree):
            length = end_lineno - lineno + 1
            self.function_count += 1
            if length > FUNCTION_THRESHOLDS[0]:
                self.function_over_40 += 1
            if length > FUNCTION_THRESHOLDS[1]:
                self.function_over_60 += 1

        pure_comment_lines = _pure_comment_line_numbers(source)
        self.hash_block_over_5 += _count_blocks_over(pure_comment_lines, HASH_BLOCK_THRESHOLD_LINES)

        comment_line_numbers = {lineno for lineno, _text in _all_comment_texts(source)}
        comment_or_docstring_lines = comment_line_numbers | docstring_line_numbers

        # 每一行只归入唯一一类，优先级 空行 > 注释/docstring > 代码——避免
        # docstring 内部的空行既被算进 docstring 行数又被算进空行数，重复计数。
        for line_number, text in enumerate(lines, start=1):
            if text.strip() == "":
                self.blank_lines += 1
            elif line_number in comment_or_docstring_lines:
                self.comment_and_docstring_lines += 1
            else:
                self.code_lines += 1

        provenance_lines: set[int] = set()
        for lineno, text in _all_comment_texts(source):
            if PROVENANCE_PATTERN.search(text):
                provenance_lines.add(lineno)
        for lineno, end_lineno in docstring_spans:
            for line_number in range(lineno, end_lineno + 1):
                if line_number - 1 < len(lines) and PROVENANCE_PATTERN.search(
                    lines[line_number - 1]
                ):
                    provenance_lines.add(line_number)
        if provenance_lines:
            self.provenance_file_count += 1
        self.provenance_line_count += len(provenance_lines)

    def render(self, root: Path) -> str:
        code_ratio = self.code_lines / self.total_lines if self.total_lines else 0.0
        comment_ratio = (
            self.comment_and_docstring_lines / self.total_lines if self.total_lines else 0.0
        )
        blank_ratio = self.blank_lines / self.total_lines if self.total_lines else 0.0
        lines = [
            f"可读性读数：{root}",
            f"  文件数：{self.file_count}",
            f"  总行数：{self.total_lines}",
            f"  纯代码占比：{code_ratio:.1%}",
            f"  注释＋docstring 占比：{comment_ratio:.1%}",
            f"  空行占比：{blank_ratio:.1%}",
            f"  函数总数：{self.function_count}",
            f"    > 40 行：{self.function_over_40}",
            f"    > 60 行：{self.function_over_60}",
            f"  docstring > 10 行个数：{self.docstring_over_10}",
            f"  连续 # 注释块 > 5 行个数：{self.hash_block_over_5}",
            f"  来历标记命中行数：{self.provenance_line_count}",
            f"  来历标记命中文件数：{self.provenance_file_count}",
        ]
        return "\n".join(lines)


def build_report(root: Path) -> Report:
    report = Report()
    for path in sorted(root.rglob("*.py")):
        report.add_file(path)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "src" / "lingxi",
        help="要体检的目录（默认 src/lingxi；可指向任意目录，包括本仓库之外的旧树）",
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    if not root.is_dir():
        print(f"目录不存在：{root}", file=sys.stderr)
        return 1

    report = build_report(root)
    print(report.render(root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
