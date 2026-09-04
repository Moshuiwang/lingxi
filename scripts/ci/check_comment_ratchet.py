#!/usr/bin/env python3
"""注释卫生棘轮门禁：三类"注释腐烂"信号已超过阈值的只许变少、不许变多。

与 ``check_function_size_ratchet.py`` 同一骨架（同样不 import 它，独立可运行）。
范围同样只覆盖 ``src/lingxi/``，理由也相同：``tests/``/``migrations/``/``scripts/``
不在结构性清理的批次范围内。

三类信号按**文件**计数：

- ``docstring_over``：本文件里"过长"的 docstring 个数——模块 docstring 超过
  15 行、类或函数（含方法与嵌套函数）docstring 超过 10 行，各算一个。长度按
  AST 的 ``end_lineno - lineno + 1``（docstring 自身的行span，含三引号所在的
  首尾行）。
- ``hash_block_over``：本文件里连续 ``#`` 注释行组成的"块"超过 5 行的个数。
  块的边界是"非注释行"或"空行"——用 ``tokenize`` 找出全部 COMMENT token 里
  行首到注释起点之间只有空白的那些（排除代码行尾的行内注释，只算独占一行的
  注释），按行号分组出连续区间，区间长度 > 5 的记一个。
- ``provenance``：本文件的注释（含行尾注释）与 docstring 正文里，命中"来历"
  正则的**行数**（一行命中多个模式只算一次）。正则族见 ``PROVENANCE_PATTERN``，
  无条件覆盖 Issue/PR 编号简写（``#238``）、日期（``2026-09-04``）、``Issue``、
  ``PR #123``、``修复包``、``codex``/``Codex``、``复盘``；``审查``/``审核``/
  ``复核``/``裁定``四词只在贴着来历标记时命中——独立审核实测坐实裸词会连坐
  "内容审核器""这道复核要挡的是"这类正常语义，因此收窄为
  「审核范围限定词（独立/外部/批量/对抗/二级/opus/codex/元守护）紧邻其后」
  或「后面紧跟一个编号（可带括号与最多三个字母前缀）」两种贴线形态才命中
  ——精确正则见下方 ``PROVENANCE_PATTERN`` 源码，这里只描述判定条件，不复述
  转义字符——这些词是"这行在交代来历，不是在交代约束"的信号（代码框架「三、横切
  约定」：注释与 docstring 说明"为什么"，不留审查过程/修复包/日期这类会
  腐烂的痕迹）。

基线文件 ``scripts/ci/comment_ratchet_baseline.txt``，行格式「数量<TAB>类别
<TAB>路径」——键是 (类别, 路径) 二元组，一个文件最多三行（三类各一行，仅当
该类计数 > 0 时才登记）。棘轮纪律与 ``check_function_size_ratchet.py`` 一致：
已登记 (类别, 路径) 的数量必须与实测精确相等；未登记的 (类别, 路径) 组合实测
必须为 0（这三类信号的目标终态就是"处处为零"，阈值本身是 0，不是像文件/函数
体量那样有一个"合理但不必清零"的自然基线）；``--refresh`` 只缩不增；
``--bootstrap`` 仅在基线文件不存在时可用。

与文件/函数体量两条棘轮同一纪律：这里 ``current`` 只保留非零计数，一个
(类别, 路径) 从 ``current`` 里消失——无论是文件被删还是计数真的清零到
0——都等价于"实测 0"，与基线记录的非零值不一致，同样需要显式 ``--refresh``
才能清空登记，不允许陈旧登记静默残留（另外两条棘轮此前对"键在实测中彻底
消失"静默放行，是已经修正的同类缺陷，见各自门禁头注释）。

已知边界：①与 ``check_size_ratchet.py`` 相同，基线与净增内容若在同一次提交里
同步改动，本门禁看到的是"记录==实测"，判绿，不构成算法上不可绕过的证明；
②只有函数体的**第一条语句**会被当作 docstring 扫描长度与来历，函数体中间
非首句的裸字符串字面量语句不参与 ``docstring_over``/``provenance`` 统计；
③连续 ``#`` 注释块按"连续行号"分组，中间穿插一个空行即拆成两个独立块分别
计数；④不遍历 ``ast.Lambda``，lambda 表达式内部的注释不计入统计。
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import sys
import tokenize
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "src" / "lingxi"
BASELINE_PATH = REPOSITORY_ROOT / "scripts" / "ci" / "comment_ratchet_baseline.txt"

MODULE_DOCSTRING_THRESHOLD_LINES = 15
SCOPED_DOCSTRING_THRESHOLD_LINES = 10
HASH_BLOCK_THRESHOLD_LINES = 5

CATEGORIES = ("docstring_over", "hash_block_over", "provenance")

#: "来历"正则族：命中即视为这一行在交代来历（审查过程、修复包、日期、Issue/PR
#: 编号），而不是在交代约束本身。``代码规范.md`` 引用的就是这份常量。
#: ``审查``/``审核``/``复核``/``裁定`` 四词裸形态会连坐"内容审核器""这道复核
#: 要挡的是"这类正常语义（独立审核实测坐实），因此收窄为只在贴着来历标记时
#: 命中：前面紧跟审核范围限定词（``独立审查``/``codex 审查`` 等），或后面紧跟
#: 编号（``审查（2026-09-04）``/``裁定 D-15`` 等）。其余词（Issue/PR 编号、
#: 日期、``修复包``、``codex``/``Codex``、``复盘``）保持无条件命中。
PROVENANCE_PATTERN = re.compile(
    r"#\d{2,4}\b"
    r"|\b20\d{2}-\d{2}-\d{2}\b"
    r"|Issue"
    r"|PR\s*#?\d"
    r"|修复包"
    r"|[Cc]odex"
    r"|复盘"
    r"|(独立|外部|批量|对抗|二级|opus|codex|元守护)\s*(审查|审核|复核)"
    r"|(审查|审核|复核|裁定)\s*[（(]?[A-Za-z]{0,3}-?\d"
)

BASELINE_HEADER = (
    "# 注释卫生棘轮基线：登记当前非零的三类信号计数与其上限。",
    "# 由 scripts/ci/check_comment_ratchet.py --refresh 生成，请不要手工调大数值——",
    "# 门禁会重新丈量实际计数，任何比这里记录的更大的实测值都直接判红；",
    "# --refresh 只会把数值调小或整条移除（计数归零），拒绝写入任何增长。",
    "# 行格式「数量<TAB>类别<TAB>路径」，类别取值 docstring_over/hash_block_over/",
    "# provenance。一个 (类别, 路径) 组合从未出现过、实测又非零时，不会被",
    "# --refresh 自动登记进来：先清理；确有理由要接受它作为新的棘轮登记对象，",
    "# 人工在下面加一行，门禁会核对这一行是否等于实际计数。",
)


class BaselineError(ValueError):
    """基线文件读取或格式错误，或源码解析失败——必须失败关闭。"""


def iter_scope_files() -> list[Path]:
    if not SOURCE_ROOT.is_dir():
        raise BaselineError(f"源码根目录不存在：{SOURCE_ROOT}")
    files = sorted(SOURCE_ROOT.rglob("*.py"))
    if not files:
        raise BaselineError(f"源码根目录下一个 .py 文件都没扫到：{SOURCE_ROOT}")
    return files


def _docstring_spans(tree: ast.Module) -> list[tuple[str, int, int]]:
    """返回 (kind, lineno, end_lineno) 列表，kind 为 module/class/function。"""

    spans: list[tuple[str, int, int]] = []

    def _leading_docstring(body: list[ast.stmt], kind: str) -> None:
        if not body:
            return
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
            and first.end_lineno is not None
        ):
            spans.append((kind, first.lineno, first.end_lineno))

    _leading_docstring(tree.body, "module")

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _leading_docstring(child.body, "function")
                walk(child)
            elif isinstance(child, ast.ClassDef):
                _leading_docstring(child.body, "class")
                walk(child)
            else:
                walk(child)

    walk(tree)
    return spans


def _pure_comment_line_numbers(source: str) -> set[int]:
    """独占一行的 ``#`` 注释行号集合（不含代码行尾的行内注释）。"""

    lines = source.splitlines()
    pure: set[int] = set()
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in tokens:
            if tok.type != tokenize.COMMENT:
                continue
            row, col = tok.start
            if row - 1 < len(lines) and lines[row - 1][:col].strip() == "":
                pure.add(row)
    except (tokenize.TokenError, IndentationError, SyntaxError) as error:
        raise BaselineError(f"分词失败：{error}") from error
    return pure


def _all_comment_tokens(source: str) -> list[tuple[int, str]]:
    """全部注释 token 的 (行号, 文本)，含行内注释——供来历扫描用。"""

    result: list[tuple[int, str]] = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in tokens:
            if tok.type == tokenize.COMMENT:
                result.append((tok.start[0], tok.string))
    except (tokenize.TokenError, IndentationError, SyntaxError) as error:
        raise BaselineError(f"分词失败：{error}") from error
    return result


def _count_hash_blocks_over_threshold(pure_comment_lines: set[int]) -> int:
    """把连续行号分组成块，返回长度 > 阈值的块数。"""

    if not pure_comment_lines:
        return 0
    ordered = sorted(pure_comment_lines)
    blocks = 0
    run_length = 1
    for previous, current in zip(ordered, ordered[1:]):
        if current == previous + 1:
            run_length += 1
            continue
        if run_length > HASH_BLOCK_THRESHOLD_LINES:
            blocks += 1
        run_length = 1
    if run_length > HASH_BLOCK_THRESHOLD_LINES:
        blocks += 1
    return blocks


def _analyze_file(path: Path) -> dict[str, int]:
    relative = path.relative_to(REPOSITORY_ROOT).as_posix()
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise BaselineError(f"无法读取 {relative}：{error}") from error
    try:
        tree = ast.parse(source, filename=relative)
    except SyntaxError as error:
        raise BaselineError(f"{relative} 解析失败：{error}") from error

    lines = source.splitlines()
    docstring_spans = _docstring_spans(tree)

    docstring_over = 0
    for kind, lineno, end_lineno in docstring_spans:
        threshold = (
            MODULE_DOCSTRING_THRESHOLD_LINES
            if kind == "module"
            else SCOPED_DOCSTRING_THRESHOLD_LINES
        )
        if (end_lineno - lineno + 1) > threshold:
            docstring_over += 1

    pure_comment_lines = _pure_comment_line_numbers(source)
    hash_block_over = _count_hash_blocks_over_threshold(pure_comment_lines)

    provenance_lines: set[int] = set()
    for lineno, text in _all_comment_tokens(source):
        if PROVENANCE_PATTERN.search(text):
            provenance_lines.add(lineno)
    for _kind, lineno, end_lineno in docstring_spans:
        for line_number in range(lineno, end_lineno + 1):
            if line_number - 1 < len(lines) and PROVENANCE_PATTERN.search(lines[line_number - 1]):
                provenance_lines.add(line_number)

    return {
        "docstring_over": docstring_over,
        "hash_block_over": hash_block_over,
        "provenance": len(provenance_lines),
    }


def measure(paths: list[Path]) -> dict[tuple[str, str], int]:
    """(类别, 路径) -> 计数；只保留非零项，解析失败直接抛错（失败关闭）。"""

    counts: dict[tuple[str, str], int] = {}
    for path in paths:
        relative = path.relative_to(REPOSITORY_ROOT).as_posix()
        per_file = _analyze_file(path)
        for category in CATEGORIES:
            value = per_file[category]
            if value > 0:
                counts[(category, relative)] = value
    return counts


def parse_baseline(text: str) -> dict[tuple[str, str], int]:
    entries: dict[tuple[str, str], int] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip("\n")
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 3 or not parts[0].isdigit():
            raise BaselineError(
                f"基线文件第 {line_number} 行格式不合法（应为「数量<TAB>类别<TAB>路径」）：{line!r}"
            )
        count_text, category, path_text = parts
        if category not in CATEGORIES:
            raise BaselineError(f"基线文件第 {line_number} 行类别未知：{category!r}")
        key = (category, path_text)
        if key in entries:
            raise BaselineError(f"基线文件第 {line_number} 行重复登记同一 (类别, 路径)：{key}")
        entries[key] = int(count_text)
    return entries


def render_baseline(entries: dict[tuple[str, str], int]) -> str:
    lines = list(BASELINE_HEADER)
    lines.append("")
    for category, path in sorted(entries):
        lines.append(f"{entries[(category, path)]}\t{category}\t{path}")
    return "\n".join(lines) + "\n"


def load_baseline(path: Path) -> dict[tuple[str, str], int]:
    if not path.is_file():
        raise BaselineError(f"基线文件不存在：{path}（先跑 --bootstrap 建立初始基线）")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise BaselineError(f"无法读取基线文件 {path}：{error}") from error
    return parse_baseline(text)


def evaluate(
    baseline: dict[tuple[str, str], int], current: dict[tuple[str, str], int]
) -> list[str]:
    failures: list[str] = []

    for key, recorded in sorted(baseline.items()):
        category, path = key
        actual = current.get(key, 0)
        if actual > recorded:
            failures.append(
                f"{path}[{category}]：当前 {actual}，超过棘轮基线记录的上限 {recorded}。"
                "规则是「已登记的计数只许变小、不许变大」——请清理这个文件里对应类别的问题。"
            )
        elif actual < recorded:
            failures.append(
                f"{path}[{category}]：棘轮基线记录 {recorded}，与实测 {actual} 不一致。"
                "基线必须与实际计数精确相等，不允许留有余量。运行 "
                "python3 scripts/ci/check_comment_ratchet.py --refresh 校准。"
            )

    for key, actual in sorted(current.items()):
        if key not in baseline and actual > 0:
            category, path = key
            failures.append(
                f"{path}[{category}]：{actual}，未登记在基线里（该类信号的目标终态是"
                "处处为零，任何非零值首次出现即判红）。请清理；如果确有理由要接受它作为"
                f"新的棘轮登记对象，在 {BASELINE_PATH.relative_to(REPOSITORY_ROOT)} 里"
                f"人工加一行「{actual}\\t{category}\\t{path}」并在 PR 里说明理由"
                "（--refresh 不会自动添加新登记）。"
            )

    return failures


def run_check() -> int:
    try:
        baseline = load_baseline(BASELINE_PATH)
        current = measure(iter_scope_files())
    except BaselineError as error:
        print(f"注释卫生棘轮检查失败：{error}", file=sys.stderr)
        return 1

    failures = evaluate(baseline, current)
    if failures:
        print("注释卫生棘轮检查失败：", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print(
        f"注释卫生棘轮：通过（扫描 {len(list(iter_scope_files()))} 个源文件，"
        f"{len(baseline)} 条基线登记）"
    )
    return 0


def run_refresh() -> int:
    try:
        baseline = load_baseline(BASELINE_PATH)
        current = measure(iter_scope_files())
    except BaselineError as error:
        print(f"注释卫生棘轮刷新失败：{error}", file=sys.stderr)
        return 1

    blocking_failures = [
        failure
        for failure in evaluate(baseline, current)
        if "超过棘轮基线记录的上限" in failure or "未登记在基线里" in failure
    ]
    if blocking_failures:
        print(
            "拒绝刷新：仓库当前存在 --refresh 无法代为解决的失败——"
            "「超过棘轮基线记录的上限」是计数违反了棘轮，先清理；「未登记在基线里」"
            "--refresh 从不自动添加新登记，需要人工按提示处理：",
            file=sys.stderr,
        )
        for failure in blocking_failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    new_baseline = {key: current[key] for key in baseline if key in current}

    if new_baseline == baseline:
        print(f"注释卫生棘轮基线：已是最新（{len(baseline)} 条登记），无需刷新")
        return 0

    lowered = sorted(
        key for key in new_baseline if key in baseline and new_baseline[key] < baseline[key]
    )
    removed = sorted(key for key in baseline if key not in new_baseline)

    BASELINE_PATH.write_text(render_baseline(new_baseline), encoding="utf-8")

    if lowered:
        print(
            "已调低："
            + "、".join(f"{key}（{baseline[key]}→{new_baseline[key]}）" for key in lowered)
        )
    if removed:
        print("已移除（已清零或已删除）：" + "、".join(str(key) for key in removed))
    print(f"注释卫生棘轮基线已刷新：{len(new_baseline)} 条登记")
    return 0


def run_bootstrap() -> int:
    if BASELINE_PATH.exists():
        print(
            f"拒绝建立初始基线：{BASELINE_PATH} 已存在。--bootstrap 只能在基线文件"
            "彻底不存在时使用一次；已有基线要收紧请用 --refresh，要新增登记请"
            "人工编辑该文件。",
            file=sys.stderr,
        )
        return 1

    try:
        current = measure(iter_scope_files())
    except BaselineError as error:
        print(f"注释卫生棘轮建立初始基线失败：{error}", file=sys.stderr)
        return 1

    BASELINE_PATH.write_text(render_baseline(current), encoding="utf-8")
    files_touched = len({path for _category, path in current})
    print(f"注释卫生棘轮初始基线已建立：{len(current)} 条登记，涉及 {files_touched} 个文件")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="注释卫生棘轮门禁", allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--refresh",
        action="store_true",
        help="重新丈量已登记 (类别, 路径) 的实际计数，只调小或移除；实测更大时拒绝写入",
    )
    mode.add_argument(
        "--bootstrap",
        action="store_true",
        help="仅当基线文件不存在时，一次性写出当前全部非零计数作为初始基线",
    )
    args = parser.parse_args(argv)
    if args.refresh:
        return run_refresh()
    if args.bootstrap:
        return run_bootstrap()
    return run_check()


if __name__ == "__main__":
    raise SystemExit(main())
