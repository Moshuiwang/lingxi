#!/usr/bin/env python3
"""函数体量棘轮门禁：已超过阈值的函数——增长立即失败，缩小后需 ``--refresh``
同步基线。

与代码框架第一节「文件体量棘轮」（``check_size_ratchet.py``）同一思路，维度从
「文件行数」换成「函数长度」——文件体量棘轮挡不住「文件本身没超阈值，但里面
藏了一个几百行的函数」这种情况，需要一道独立的、更细粒度的棘轮。

**刻意不 import ``check_size_ratchet.py``**：同 ``check_matrix_row_size_ratchet.py``
的先例，仓库里没有 CI 脚本互相 import 内部实现的惯例，两份骨架各自独立可运行、
失败原因不牵连，靠约定与两边测试分别验证保证行为一致。

范围只覆盖 ``src/lingxi/``：``tests/`` 按用例数量自然变长，拆分测试函数的成本
收益与拆分业务逻辑完全不同；``migrations/`` 是 Alembic 生成的迁移脚手架；
``scripts/`` 不在结构性拆分的批次范围内。

扫描对象是 ``ast.FunctionDef``/``ast.AsyncFunctionDef``，含类方法与任意深度的
嵌套函数；键是「相对路径::限定名」，方法的限定名为 ``类名.方法名``，嵌套函数为
``外层函数名.内层函数名``（多层嵌套逐级拼接）。长度取 ``end_lineno - lineno + 1``
——``lineno`` 是 ``def``/``async def`` 关键字所在行（Python 3.8 起 AST 不再把
装饰器行计入 ``FunctionDef.lineno``，实测确认），因此长度天然不含装饰器，但含
函数自己的 docstring 与内部注释（它们是函数体的一部分）。

**同一限定名在同一文件里出现多次时取最大值**：property 的 getter/setter
（同名、不同 ``lineno``）、``if TYPE_CHECKING:`` 分支下的重定义、
``@overload`` 的多个签名重载，都会让同一个「相对路径::限定名」键对应多个
``FunctionDef`` 节点——键的形状不变，登记与比较的是这组同名定义里最长的那个，
不能让后一次定义静默覆盖前一次已经超阈值的登记（独立审查坐实：覆盖会让
一个真实超过 60 行的 getter 因为后面跟着一个 3 行的 setter 而在门禁眼里
"缩水"成 3 行）。

已知边界：①与 ``check_size_ratchet.py`` 相同，基线与净增函数若在同一次提交里
同步改动，本门禁看到的是「记录==实测」，判绿——精确相等只保证净增留下一处可
审阅的 diff，不构成算法上不可绕过的证明；②不遍历 ``ast.Lambda``，lambda 表达式
本身不计入函数体量统计。

阈值 60 行：与「新函数目标 40 行、硬限 60 行」的编码纪律对齐（代码框架未成文、
仅作为审查口径的数字，这里第一次落成会变红的门禁）。

基线登记、``--refresh``（只缩不增）与 ``check_size_ratchet.py`` 同一纪律：已登记
函数长度必须与基线精确相等；未登记函数首次超阈值判红；``--refresh`` 只会把数值
调小或整条移除，拒绝写入任何增长。

新增 ``--bootstrap``：**仅当基线文件不存在时**一次性写出当前全部超阈值函数，
供门禁刚接线时建立初始基线；基线文件已存在时拒绝执行（非零退出）——避免有人
在存量违规未清空前重复"重新起个基线"绕开棘轮。
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "src" / "lingxi"
BASELINE_PATH = REPOSITORY_ROOT / "scripts" / "ci" / "function_size_ratchet_baseline.txt"
THRESHOLD_LINES = 60

BASELINE_HEADER = (
    f"# 函数体量棘轮基线：登记当前已超过 {THRESHOLD_LINES} 行的函数与其行数上限。",
    "# 由 scripts/ci/check_function_size_ratchet.py --refresh 生成，请不要手工调大",
    "# 数值——门禁会重新丈量函数的实际行数，任何比这里记录的更大的实测值都直接",
    "# 判红；--refresh 只会把数值调小或整条移除（函数缩到阈值以下），拒绝写入",
    "# 任何增长。键的形状是「相对路径::限定名」，方法为「类名.方法名」，嵌套函数",
    "# 为「外层函数名.内层函数名」。一个从未超过阈值的函数第一次超过阈值时，不会",
    "# 被 --refresh 自动登记进来：先拆分或精简；确有理由要接受它作为新的棘轮登记",
    "# 对象，人工在下面加一行「行数<TAB>键」，门禁会核对这一行是否等于实际行数。",
)


class BaselineError(ValueError):
    """基线文件读取或格式错误——必须失败关闭，不能当作空基线继续跑。"""


def iter_scope_files() -> list[Path]:
    if not SOURCE_ROOT.is_dir():
        raise BaselineError(f"源码根目录不存在：{SOURCE_ROOT}")
    files = sorted(SOURCE_ROOT.rglob("*.py"))
    if not files:
        raise BaselineError(f"源码根目录下一个 .py 文件都没扫到：{SOURCE_ROOT}")
    return files


def _qualified_names(tree: ast.Module, relative_path: str) -> dict[str, int]:
    """遍历一棵模块 AST，返回该文件里全部函数的「限定名 -> 行数」。"""

    lengths: dict[str, int] = {}

    def walk(node: ast.AST, prefix: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = ".".join((*prefix, child.name))
                if child.end_lineno is None:
                    raise BaselineError(
                        f"{relative_path}::{qualname}：AST 节点没有 end_lineno，"
                        "无法丈量长度（本仓要求的 Python 版本应当总是提供它）"
                    )
                length = child.end_lineno - child.lineno + 1
                # 同一限定名可能对应多个节点（property getter/setter、
                # `if TYPE_CHECKING:` 重定义、`@overload` 多签名）：取 max()，
                # 不能让后一次定义覆盖前一次已经超阈值的登记。
                previous = lengths.get(qualname)
                lengths[qualname] = length if previous is None else max(previous, length)
                walk(child, (*prefix, child.name))
            elif isinstance(child, ast.ClassDef):
                walk(child, (*prefix, child.name))
            else:
                walk(child, prefix)

    walk(tree, ())
    return lengths


def measure(paths: list[Path]) -> dict[str, int]:
    """路径下全部函数的「相对路径::限定名 -> 长度」；解析失败直接抛错（失败关闭）。"""

    counts: dict[str, int] = {}
    for path in paths:
        relative = path.relative_to(REPOSITORY_ROOT).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise BaselineError(f"无法读取 {relative}：{error}") from error
        try:
            tree = ast.parse(source, filename=relative)
        except SyntaxError as error:
            raise BaselineError(f"{relative} 解析失败：{error}") from error
        for qualname, length in _qualified_names(tree, relative).items():
            counts[f"{relative}::{qualname}"] = length
    return counts


def parse_baseline(text: str) -> dict[str, int]:
    """解析「行数<TAB>键」登记表；任何一行格式不对都直接抛错。"""

    entries: dict[str, int] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip("\n")
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 2 or not parts[0].isdigit():
            raise BaselineError(
                f"基线文件第 {line_number} 行格式不合法（应为「行数<TAB>键」）：{line!r}"
            )
        count_text, key_text = parts
        if key_text in entries:
            raise BaselineError(f"基线文件第 {line_number} 行重复登记同一键：{key_text}")
        entries[key_text] = int(count_text)
    return entries


def render_baseline(entries: dict[str, int]) -> str:
    lines = list(BASELINE_HEADER)
    lines.append("")
    for key in sorted(entries):
        lines.append(f"{entries[key]}\t{key}")
    return "\n".join(lines) + "\n"


def load_baseline(path: Path) -> dict[str, int]:
    if not path.is_file():
        raise BaselineError(f"基线文件不存在：{path}（先跑 --bootstrap 建立初始基线）")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise BaselineError(f"无法读取基线文件 {path}：{error}") from error
    return parse_baseline(text)


def evaluate(baseline: dict[str, int], current: dict[str, int]) -> list[str]:
    """核对棘轮的两条规则，返回失败原因列表；空列表表示通过。"""

    failures: list[str] = []

    for key, recorded in sorted(baseline.items()):
        actual = current.get(key)
        if actual is None:
            # 函数已经不在扫描范围内（删除、改名或被移出 src/lingxi）：陈旧登记
            # 不允许静默保留在基线文件里，必须显式判红并提示 --refresh 清除——
            # 静默放行会让已经清空的登记条目在基线文件里堆积多年都没人发现。
            failures.append(
                f"{key}：棘轮基线登记了 {recorded} 行，但当前扫描范围内已经找不到"
                "这个函数（可能已删除、改名或被移出 src/lingxi/）。基线记录必须与"
                "实测精确匹配，陈旧登记不允许静默保留。运行 "
                "python3 scripts/ci/check_function_size_ratchet.py --refresh 移除。"
            )
            continue
        if actual > recorded:
            failures.append(
                f"{key}：当前 {actual} 行，超过棘轮基线记录的上限 {recorded} 行。"
                "规则是「已超过阈值的函数只许变小、不许变大」——"
                "请把新增内容移出这个函数，或拆分其中一部分职责。"
            )
        elif actual < recorded:
            failures.append(
                f"{key}：棘轮基线记录 {recorded} 行，与实测 {actual} 行不一致。"
                "基线必须与实际行数精确相等，不允许留有余量。运行 "
                "python3 scripts/ci/check_function_size_ratchet.py --refresh 校准。"
            )

    for key, actual in sorted(current.items()):
        if actual > THRESHOLD_LINES and key not in baseline:
            failures.append(
                f"{key}：{actual} 行，新超过函数体量棘轮阈值（{THRESHOLD_LINES} 行）"
                "且未登记在基线里。规则是「未超过阈值的函数不得新超过阈值」——"
                "请拆分或精简这个函数；如果确有理由要接受它作为新的棘轮登记对象，"
                f"在 {BASELINE_PATH.relative_to(REPOSITORY_ROOT)} 里人工加一行"
                f"「{actual}\\t{key}」并在 PR 里说明理由（--refresh 不会自动添加新函数）。"
            )

    return failures


def run_check() -> int:
    try:
        baseline = load_baseline(BASELINE_PATH)
        current = measure(iter_scope_files())
    except BaselineError as error:
        print(f"函数体量棘轮检查失败：{error}", file=sys.stderr)
        return 1

    failures = evaluate(baseline, current)
    if failures:
        print("函数体量棘轮检查失败：", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    over_threshold = sum(1 for length in current.values() if length > THRESHOLD_LINES)
    print(
        f"函数体量棘轮：通过（扫描 {len(current)} 个函数，阈值 {THRESHOLD_LINES} 行，"
        f"{over_threshold} 个函数在棘轮基线内，{len(baseline)} 条基线登记）"
    )
    return 0


def run_refresh() -> int:
    try:
        baseline = load_baseline(BASELINE_PATH)
        current = measure(iter_scope_files())
    except BaselineError as error:
        print(f"函数体量棘轮刷新失败：{error}", file=sys.stderr)
        return 1

    # 同 check_size_ratchet.py 的纪律：--refresh 只能安全处理「基线记录 > 实测」
    # 这一类（函数已缩小/被登记者手工调大），另外两类（超过自己的登记上限、
    # 未登记函数新超阈值）都不是它该代为解决的。
    blocking_failures = [
        failure
        for failure in evaluate(baseline, current)
        if "超过棘轮基线记录的上限" in failure or "新超过函数体量棘轮阈值" in failure
    ]
    if blocking_failures:
        print(
            "拒绝刷新：仓库当前存在 --refresh 无法代为解决的失败——"
            "「超过棘轮基线记录的上限」是函数违反了棘轮，先把函数缩回基线记录的"
            "行数以内；「新超过函数体量棘轮阈值…且未登记在基线里」--refresh 从不"
            "自动添加新登记，需要人工按提示处理：",
            file=sys.stderr,
        )
        for failure in blocking_failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    new_baseline = {
        key: current[key] for key in baseline if key in current and current[key] > THRESHOLD_LINES
    }

    if new_baseline == baseline:
        print(f"函数体量棘轮基线：已是最新（{len(baseline)} 条登记），无需刷新")
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
        print("已移除（已缩到阈值以下或已删除）：" + "、".join(removed))
    print(f"函数体量棘轮基线已刷新：{len(new_baseline)} 条登记")
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
        print(f"函数体量棘轮建立初始基线失败：{error}", file=sys.stderr)
        return 1

    initial_baseline = {key: length for key, length in current.items() if length > THRESHOLD_LINES}
    BASELINE_PATH.write_text(render_baseline(initial_baseline), encoding="utf-8")
    files_touched = len({key.split("::", 1)[0] for key in initial_baseline})
    print(
        f"函数体量棘轮初始基线已建立：{len(initial_baseline)} 条登记，涉及 {files_touched} 个文件"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    # allow_abbrev=False：与 check_size_ratchet.py 同一纪律，--refresh/--bootstrap
    # 都有写入副作用，缩写匹配（如 --r/--b）绝不能被 argparse 默认放行。
    parser = argparse.ArgumentParser(description="函数体量棘轮门禁", allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--refresh",
        action="store_true",
        help="重新丈量已登记函数的实际行数，只调小或移除；函数比登记的更大时拒绝写入",
    )
    mode.add_argument(
        "--bootstrap",
        action="store_true",
        help="仅当基线文件不存在时，一次性写出当前全部超阈值函数作为初始基线",
    )
    args = parser.parse_args(argv)
    if args.refresh:
        return run_refresh()
    if args.bootstrap:
        return run_bootstrap()
    return run_check()


if __name__ == "__main__":
    raise SystemExit(main())
