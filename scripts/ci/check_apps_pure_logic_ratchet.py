#!/usr/bin/env python3
"""``apps/`` 纯逻辑棘轮门禁（Issue #656）：新命中只许缩不许涨、不许新增。

代码框架「二、三层之间的 import 规则」第 3 条早就写着「``apps/`` 只做组装……不写业务
规则」，但此前只有 1、2 两条各自有 ``check_core_layering.py`` 或口头约束，第 3 条
**从未有过任何自动检查**——一段业务规则悄悄住进 ``apps/`` 不会让任何门禁变红。本脚本
补上这一条，采用与 ``check_size_ratchet.py`` 相同的棘轮骨架（同样不 import 它，
独立可运行）：已登记的命中只许缩不许涨，未登记的不得新出现。

**判据（比最初设想收紧一档，降噪）**：``src/lingxi/apps/`` 下的一个 ``.py`` 文件，
同时满足①至少出现一处 ``import lingxi.core`` 或 ``from lingxi.core...`` 形式的
import（含 ``from lingxi import core``），②「代码行数」（总行数减去空行、独占一行的
``#`` 注释、模块/类/函数开头的 docstring span）达到或超过 ``MIN_CODE_LINES``（10），
才算一次命中。只看「导入了 core」不够——``apps/`` 按架构设计天然要"注入 core"，几乎
每个入口文件都会 import 一些 core 类型来装配；行数下限把「只是引用了几个 core 类型
签名的三五行胶水代码」排除在外，留下真正体量足以承载业务判断的文件。

**已知边界，不是缺陷**：这条判据在真实扫描中命中了 ``apps/`` 里 50 / 71 个 ``.py``
文件（**七成**；这两个数由本脚本自己扫出来，改动后重扫即可复算）——绝大多数是职责循环（``*_refresh.py``/``*_sync.py``/``*Duty``）、进程装配
（``assembly.py``/``__init__.py``）、类型化配置读取（``config.py``）或端口协议
（``*_ports.py``），它们 import core 类型是为了构造/转发/声明协议，不是因为业务
判断本身长在这里；这类装配件在基线里占绝大多数，逐条豁免见基线文件头注释。启发式
本身**只在整个文件的粒度上生效**：一个文件一旦命中就整份进登记表，
门禁分辨不出文件内部哪几行是组装、哪几行可能真的是业务判断（两者常常交错在同一个
函数里）——**命中文件合计约 12,365 行代码，占 ``apps/`` 全部约 17,085 行的七成**，
也就是说「登记在案」覆盖的行很多，但其中真正属于业务判断的比例本门禁分辨不出；
反过来，不 import core 的重复业务逻辑（例如某处把本该调用 core 判定函数
的分支自己重新写了一遍）完全不在这条门禁的视野内。这是静态扫描能负担的成本与
"抓到点什么"之间的取舍，不是宣称"命中的都是违规、干净的都没问题"。

命中越权是否需要真正搬进 ``core/`` 是产品/架构判断，本脚本不代为决定：基线只是
"棘轮生效前已存在的现状快照"，新增命中才是这道门禁真正要挡住的东西。
"""

from __future__ import annotations

import argparse
import ast
import io
import sys
import tokenize
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
APPS_ROOT = REPOSITORY_ROOT / "src" / "lingxi" / "apps"
BASELINE_PATH = REPOSITORY_ROOT / "scripts" / "ci" / "apps_pure_logic_ratchet_baseline.txt"

#: 命中门槛：核对 import 之外，还要求「代码行数」达到这个数字才算命中——见模块
#: docstring「判据」一节。
MIN_CODE_LINES = 10

BASELINE_HEADER = (
    "# apps/ 纯逻辑棘轮基线（Issue #656）：登记当前已命中「import lingxi.core.* 且",
    "# 代码行数 >= 10」的 apps/ 文件与其代码行数。",
    "# 由 scripts/ci/check_apps_pure_logic_ratchet.py --refresh 生成，请不要手工调大",
    "# 数值——门禁会重新丈量文件的实际代码行数，任何比这里记录的更大的实测值都直接",
    "# 判红；--refresh 只会把数值调小或整条移除（文件缩到命中线以下或不再 import",
    "# core），拒绝写入任何增长。一个从未命中过的文件第一次命中时，不会被 --refresh",
    "# 自动登记进来：先把业务判断挪回 core/，或者确认这确实是装配代码；确有理由要",
    "# 接受它作为新的棘轮登记对象，人工在下面加一行「代码行数<TAB>路径」，门禁会核对",
    "# 这一行是否等于该文件的当前实际代码行数。",
    "#",
    "# 归类（人工判断，随基线刷新可能需要复核，不逐条钉死理由）：",
    "#",
    "# 「放错层，等 #658/#659 搬走」——目前只挑出一条最清楚的候选：",
    "#   - apps/scheduler/daily_report_sections.py：模块 docstring 自称「纯函数，",
    "#     只依赖显式传参，不碰任何实例状态」，且注明搬出 daily_report.py 的理由是",
    "#     把原模块压回体量棘轮阈值以内（工程理由），不是因为它依赖任何 apps/",
    "#     或 adapters/ 的类型。是本批复核里唯一一处「自己承认是纯逻辑」的文件。",
    "#",
    "# 「装配件豁免」——其余全部登记条目：module docstring 自称「职责」「装配」",
    "#   「配置」「端口协议」「消费循环」的入口/胶水文件，import core 是为了构造、",
    "#   转发或声明协议类型；未逐条精读函数体确认零业务判断，只按模块自陈的定位与",
    "#   命名模式归类——这本身就是「覆盖率约 8%」这条已知边界的体现，供 #658/#659",
    "#   按需复核，不构成「已逐条人工验证零违规」的断言。",
)


class BaselineError(ValueError):
    """基线文件读取或格式错误，或扫描本身失败——必须失败关闭。"""


def iter_scope_files() -> list[Path]:
    if not APPS_ROOT.is_dir():
        raise BaselineError(f"apps 根目录不存在：{APPS_ROOT}")
    files = sorted(APPS_ROOT.rglob("*.py"))
    if not files:
        raise BaselineError(f"apps 根目录下一个 .py 文件都没扫到：{APPS_ROOT}")
    return files


def _docstring_line_numbers(tree: ast.Module) -> set[int]:
    """模块、类、函数（含方法与嵌套函数）开头 docstring 占用的全部行号。"""

    spans: list[tuple[int, int]] = []

    def _leading_docstring(body: list[ast.stmt]) -> None:
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

    _leading_docstring(tree.body)

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                _leading_docstring(child.body)
                walk(child)
            else:
                walk(child)

    walk(tree)

    lines: set[int] = set()
    for start, end in spans:
        lines.update(range(start, end + 1))
    return lines


def _pure_comment_line_numbers(source: str) -> set[int]:
    """独占一行的 ``#`` 注释行号集合（不含代码行尾的行内注释——那一行仍是代码）。"""

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


def _module_dotted_name(relative_posix: str) -> str:
    """把 ``src/lingxi/apps/gateway/foo.py`` 还原成 ``lingxi.apps.gateway.foo``。"""

    stem = relative_posix.removeprefix("src/").removesuffix(".py")
    parts = [segment for segment in stem.split("/") if segment]
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_relative(module_name: str, level: int, target: str) -> str:
    """把一处相对 import 还原成绝对模块名；越过包根时返回空串。"""

    package = module_name.rsplit(".", 1)[0] if "." in module_name else ""
    parts = package.split(".") if package else []
    if level - 1 > len(parts):
        return ""
    base = parts[: len(parts) - (level - 1)] if level > 1 else parts
    return ".".join([*base, target]) if target else ".".join(base)


def _is_core(name: str) -> bool:
    return name == "lingxi.core" or name.startswith("lingxi.core.")


def _imports_lingxi_core(tree: ast.Module, relative_posix: str) -> bool:
    """是否存在至少一处 import ``lingxi.core``（或其子模块）的语句。

    **相对 import 一样算**：本仓大量使用 ``from ..core.x import y`` 这种写法，
    只认绝对模块名等于给判据留了一个改写 import 形态就能绕过去的口子——独立审查
    实测坐实：同一个判定模块换成相对 import 即判绿。这里按文件位置把相对 import
    还原成绝对名再判。
    """

    module_name = _module_dotted_name(relative_posix)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_core(alias.name):
                    return True
        elif isinstance(node, ast.ImportFrom):
            level = node.level or 0
            module = node.module or ""
            absolute = _resolve_relative(module_name, level, module) if level else module
            if not absolute:
                continue
            if _is_core(absolute):
                return True
            if absolute == "lingxi" and any(alias.name == "core" for alias in node.names):
                return True
            if level and absolute.endswith(".core"):
                # `from ..core import x`：还原后可能停在包名 `lingxi.core` 上
                if _is_core(absolute):
                    return True
    return False


def _code_line_count(source: str, tree: ast.Module) -> int:
    """总行数减去空行、独占注释行与开头 docstring span 之后剩下的行数。"""

    lines = source.splitlines()
    blank = {index + 1 for index, line in enumerate(lines) if line.strip() == ""}
    non_code = blank | _pure_comment_line_numbers(source) | _docstring_line_numbers(tree)
    return len(lines) - len(non_code)


def measure(paths: list[Path]) -> dict[str, int]:
    """路径 -> 代码行数；只保留命中（import core 且代码行数达标）的文件。

    任何一个文件读不出来或解析失败都直接抛错（失败关闭），与 ``check_size_ratchet.py``
    同一纪律：扫描失败不能悄悄被当成"没有命中"。
    """

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
        if not _imports_lingxi_core(tree, relative):
            continue
        count = _code_line_count(source, tree)
        if count >= MIN_CODE_LINES:
            counts[relative] = count
    return counts


def parse_baseline(text: str) -> dict[str, int]:
    """解析「代码行数<TAB>路径」登记表；任何一行格式不对都直接抛错。"""

    entries: dict[str, int] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip("\n")
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 2 or not parts[0].isdigit():
            raise BaselineError(
                f"基线文件第 {line_number} 行格式不合法（应为「代码行数<TAB>路径」）：{line!r}"
            )
        count_text, path_text = parts
        if path_text in entries:
            raise BaselineError(f"基线文件第 {line_number} 行重复登记同一路径：{path_text}")
        entries[path_text] = int(count_text)
    return entries


def render_baseline(entries: dict[str, int]) -> str:
    lines = list(BASELINE_HEADER)
    lines.append("")
    for path in sorted(entries):
        lines.append(f"{entries[path]}\t{path}")
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

    for path, recorded in sorted(baseline.items()):
        actual = current.get(path)
        if actual is None:
            failures.append(
                f"{path}：棘轮基线登记了 {recorded} 行代码，但当前扫描中该文件已经不再命中"
                "（可能已删除、改名、移出 apps/，或不再 import lingxi.core）。基线记录必须"
                "与实测精确匹配，陈旧登记不允许静默保留。运行 "
                "python3 scripts/ci/check_apps_pure_logic_ratchet.py --refresh 移除。"
            )
            continue
        if actual > recorded:
            failures.append(
                f"{path}：当前 {actual} 行代码，超过棘轮基线记录的上限 {recorded} 行。"
                "规则是「已登记的命中只许变小、不许变大」——"
                "请把新增的业务判断挪回 core/，或拆分这个文件里的组装与判断两部分。"
            )
        elif actual < recorded:
            failures.append(
                f"{path}：棘轮基线记录 {recorded} 行代码，与实测 {actual} 行不一致。"
                "基线必须与实际代码行数精确相等，不允许留有余量。运行 "
                "python3 scripts/ci/check_apps_pure_logic_ratchet.py --refresh 校准。"
            )

    for path, actual in sorted(current.items()):
        if path not in baseline:
            failures.append(
                f"{path}：{actual} 行代码，新命中「import lingxi.core.* 且代码行数 >= "
                f"{MIN_CODE_LINES}」且未登记在基线里。规则是「apps/ 只做组装，不写业务"
                "规则」——请把业务判断挪回 core/；如果确有理由要接受它作为新的棘轮登记"
                f"对象，在 {BASELINE_PATH.relative_to(REPOSITORY_ROOT)} 里人工加一行"
                f"「{actual}\\t{path}」并在 PR 里说明理由（--refresh 不会自动添加新登记）。"
            )

    return failures


def run_check() -> int:
    try:
        baseline = load_baseline(BASELINE_PATH)
        current = measure(iter_scope_files())
    except BaselineError as error:
        print(f"apps 纯逻辑棘轮检查失败：{error}", file=sys.stderr)
        return 1

    failures = evaluate(baseline, current)
    if failures:
        print("apps 纯逻辑棘轮检查失败：", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print(
        f"apps 纯逻辑棘轮：通过（扫描 {len(iter_scope_files())} 个 apps/ 源文件，"
        f"命中门槛 import lingxi.core.* 且 >= {MIN_CODE_LINES} 行代码，"
        f"{len(baseline)} 条基线登记）"
    )
    return 0


def run_refresh() -> int:
    try:
        baseline = load_baseline(BASELINE_PATH)
        current = measure(iter_scope_files())
    except BaselineError as error:
        print(f"apps 纯逻辑棘轮刷新失败：{error}", file=sys.stderr)
        return 1

    blocking_failures = [
        failure
        for failure in evaluate(baseline, current)
        if "超过棘轮基线记录的上限" in failure or "新命中" in failure
    ]
    if blocking_failures:
        print(
            "拒绝刷新：仓库当前存在 --refresh 无法代为解决的失败——"
            "「超过棘轮基线记录的上限」是文件违反了棘轮，先把业务判断移出该文件；"
            "「新命中…且未登记在基线里」--refresh 从不自动添加新登记，需要人工按提示"
            "处理：",
            file=sys.stderr,
        )
        for failure in blocking_failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    new_baseline = {path: current[path] for path in baseline if path in current}

    if new_baseline == baseline:
        print(f"apps 纯逻辑棘轮基线：已是最新（{len(baseline)} 条登记），无需刷新")
        return 0

    lowered = sorted(
        path for path in new_baseline if path in baseline and new_baseline[path] < baseline[path]
    )
    removed = sorted(path for path in baseline if path not in new_baseline)

    BASELINE_PATH.write_text(render_baseline(new_baseline), encoding="utf-8")

    if lowered:
        print(
            "已调低："
            + "、".join(f"{path}（{baseline[path]}→{new_baseline[path]}）" for path in lowered)
        )
    if removed:
        print("已移除（已不再命中或已删除）：" + "、".join(removed))
    print(f"apps 纯逻辑棘轮基线已刷新：{len(new_baseline)} 条登记")
    return 0


def run_bootstrap() -> int:
    if BASELINE_PATH.exists():
        print(
            f"拒绝建立初始基线：{BASELINE_PATH} 已存在。--bootstrap 只能在基线文件"
            "彻底不存在时使用一次；已有基线要收紧请用 --refresh，要新增登记请人工"
            "编辑该文件。",
            file=sys.stderr,
        )
        return 1

    try:
        current = measure(iter_scope_files())
    except BaselineError as error:
        print(f"apps 纯逻辑棘轮建立初始基线失败：{error}", file=sys.stderr)
        return 1

    BASELINE_PATH.write_text(render_baseline(current), encoding="utf-8")
    print(f"apps 纯逻辑棘轮初始基线已建立：{len(current)} 条登记")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="apps 纯逻辑棘轮门禁（Issue #656）", allow_abbrev=False
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--refresh",
        action="store_true",
        help="重新丈量已登记文件的实际代码行数，只调小或移除；实测更大时拒绝写入",
    )
    mode.add_argument(
        "--bootstrap",
        action="store_true",
        help="仅当基线文件不存在时，一次性写出当前全部命中作为初始基线",
    )
    args = parser.parse_args(argv)
    if args.refresh:
        return run_refresh()
    if args.bootstrap:
        return run_bootstrap()
    return run_check()


if __name__ == "__main__":
    raise SystemExit(main())
