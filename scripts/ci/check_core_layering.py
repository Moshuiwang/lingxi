#!/usr/bin/env python3
"""`core/` 分层 import 门禁（Issue #238）：固化代码框架第二节的第一条规则。

代码框架「二、三层之间的 import 规则」写了三条边界，第一条是：

    `core/` 不 import `adapters/`、`apps/`、任何外部 SDK，也不做网络或文件 I/O。

此前这条规则「目前由测试与代码审查把守；如出现违反案例，再把 import 检查加进 CI，
不预建工具」——纯散文约束。编排者在 2026-08-19 实测：`core/` 当前**零违规**（除
标准库外没有任何外部 SDK import）。这正是加门禁最省成本的时机：不需要豁免名单，
因为此刻没有任何东西需要被豁免；等出现第一处违规再补门禁，就得先处理一份存量清单。

**范围与不做什么**：

- 本脚本只检查静态 `import` 语句——`core/` 不 import ``adapters/``、``apps/``、
  任何非标准库第三方模块。SDK 类型不泄漏进 `core/` 函数签名是同一条规则的另一种
  表述：没有静态 import，签名里就不可能出现一个未导入的类型名。
- **已知未覆盖的残余风险**（2026-08-19 三路独立复查后完整列出，接受不修）：
  ① 字符串形式的前向引用（`from __future__ import annotations` 下用字符串写死
  一个未导入的类型名）；② 动态 import——`importlib.import_module(...)`、
  `__import__(...)`、`exec(...)`、直接读写 `sys.modules`——这些都不是静态
  `ast.Import`/`ast.ImportFrom` 节点，本脚本的 AST 遍历看不到。这四种写法目前
  在 `core/` 里都**没有实例**（2026-08-19 复查时确认），且在 `core/` 里写动态
  import 几乎不可能是无意为之——不像"函数体内的普通 import"那样是本仓库常见的
  惯用写法，一旦出现更容易在代码审查中被发现。若未来要补，`importlib.
  import_module("常量字符串")` 这一档可以低成本加一条 `ast.Call` 检查；其余三种
  （非常量参数、`exec`、直接操纵 `sys.modules`）本质上是运行期才能确定目标，
  静态分析做不到，只能继续靠代码审查。
- `core/` 不做网络或文件 I/O 这半句**不在本脚本的检查范围**：静态判定"是否发起了
  I/O" 需要远比 import 检查更大的分析（例如追踪任意变量是否指向一个打开的文件
  描述符），Issue #238 的验收标准只要求"分层 import 门禁"，本脚本只做这一件事。

**必须扫得到函数体内的延迟导入**：本仓库的第三方 import 全部写在函数体里
（`check_runtime_dependencies.py` 已经踩过这个坑，见该脚本头注释）。只扫模块级
`tree.body` 等于没扫——一个写在 `core/` 某个函数里的 `import httpx` 会完全逃过
检查。因此这里跟 `check_runtime_dependencies.py` 一样，用 `ast.walk` 遍历整棵树，
不区分嵌套层级，并用一条会红的用例证明这一点（`tests/test_core_layering_check.py`）。

扫描失败必须失败关闭：`core/` 目录缺失、任何一个 `.py` 文件解析失败，都直接判红，
不能把"扫不动"悄悄当成"没有违规"。
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "src" / "lingxi"
CORE_ROOT = SOURCE_ROOT / "core"

# core/ 不得落进的两个内部层。
FORBIDDEN_LINGXI_PREFIXES = ("lingxi.adapters", "lingxi.apps")


class LayeringCheckError(ValueError):
    """扫描本身失败（目录缺失、文件读不出来、解析失败）——必须失败关闭。"""


def iter_core_files(core_root: Path = CORE_ROOT) -> list[Path]:
    if not core_root.is_dir():
        raise LayeringCheckError(f"core/ 目录不存在：{core_root}")
    files = sorted(core_root.rglob("*.py"))
    if not files:
        raise LayeringCheckError(f"core/ 目录下一个 .py 文件都没扫到：{core_root}")
    return files


def _module_name(path: Path, source_root: Path) -> str:
    relative = path.relative_to(source_root)
    parts = list(relative.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][: -len(".py")]
    return ".".join(["lingxi", *parts])


def _display_path(path: Path) -> Path:
    """相对仓库根显示；扫描根被指到仓库之外时（单元测试用临时目录会这么做），
    退化成绝对路径即可——出处只是给人看的诊断信息，不参与判定。
    """

    try:
        return path.relative_to(REPOSITORY_ROOT)
    except ValueError:
        return path


def find_violations(path: Path, source_root: Path = SOURCE_ROOT) -> list[str]:
    """返回这个文件里的违规描述列表（空列表表示干净）。

    用 ``ast.walk`` 而不是只看 ``tree.body``：后者只看模块顶层语句，
    走不进函数体、类体、``try`` 块——而这些恰恰是本仓库真实第三方 import 所在的地方。
    """

    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise LayeringCheckError(f"无法读取 {_display_path(path)}：{error}") from error

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as error:
        raise LayeringCheckError(f"{_display_path(path)} 解析失败：{error}") from error

    own_module = _module_name(path, source_root)
    package = own_module if path.name == "__init__.py" else own_module.rsplit(".", 1)[0]
    stdlib = sys.stdlib_module_names
    violations: list[str] = []

    for node in ast.walk(tree):
        targets: list[tuple[str, int]] = []
        if isinstance(node, ast.Import):
            targets = [(alias.name, node.lineno) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # 相对 import：按当前包深度解析成绝对模块名，
                # 这样 core/ 内部深处用 `from ...adapters import x` 绕过绝对路径
                # 拼写也一样能被抓到。
                anchor = package.split(".")
                cut = len(anchor) - node.level + 1
                base = anchor[: max(cut, 0)]
                resolved = ".".join([*base, node.module] if node.module else base)
                module_target = resolved
            elif node.module:
                module_target = node.module
            else:
                module_target = None

            if module_target is not None:
                targets = [(module_target, node.lineno)]
                # `from lingxi.adapters import postgres_conversation` 与
                # `from lingxi import adapters` 长得不一样，但都要抓到：后者的
                # `node.module` 只是 `"lingxi"`，被 import 的子模块名字全部
                # 挂在 `node.names` 上——只看 `node.module` 会让 `from lingxi
                # import adapters` 完全穿透（`top == "lingxi"` 直接放行）。
                # 每个别名都可能是子模块，静态语法无法区分"是子模块"还是
                # "是符号"，因此保守地把 `module.alias` 也当成候选目标一并核对
                # （同一姿态见 check_runtime_dependencies.py 的 `_split_imports`）。
                if module_target == "lingxi" or module_target.startswith("lingxi."):
                    targets += [
                        (f"{module_target}.{alias.name}", node.lineno)
                        for alias in node.names
                    ]

        for target, lineno in targets:
            top = target.split(".")[0]
            location = f"{_display_path(path)}:{lineno}"
            if target.startswith(FORBIDDEN_LINGXI_PREFIXES):
                violations.append(f"{location}：`core/` import 了内部层外的模块 `{target}`")
                continue
            if top == "lingxi":
                continue  # 其余 lingxi.* 路径（core、config 等）允许
            if top in stdlib:
                continue
            violations.append(f"{location}：`core/` import 了外部第三方模块 `{target}`（core/ 不得 import 任何外部 SDK）")

    return violations


INSTALLED_PACKAGE_CHECKER_PATH = REPOSITORY_ROOT / "scripts" / "ci" / "check_installed_package.py"


def _load_installed_package_checker():
    """动态加载 `check_installed_package.py`，复用它已经写好、测过的 lingxi
    内部 import 解析（`source_module_files`/`_source_imports`）——M2（2026-08-19
    三方复查最后一轮）要求的传递闭包计算完全依赖同一套"相对 import 解析、
    模块 vs 符号歧义兜底"的逻辑，`check_installed_package.py` 的
    `process_source_closure` 已经把这套逻辑写对、写全（含父包 `__init__` 沿途
    加载），照抄一份等于给同一个 bug 留两处要同步修的地方，因此在此处直接复用，
    不重新实现。两个脚本都以 `if __name__ == "__main__"` 收尾，模块级只有函数
    与静态字面量常量，`exec_module` 不会跑 `main()` 或触发任何 I/O。
    """

    spec = importlib.util.spec_from_file_location(
        "check_installed_package_for_layering", INSTALLED_PACKAGE_CHECKER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def find_transitive_violations(source_root: Path = SOURCE_ROOT) -> list[str]:
    """M2：`core/` 里的文件即使不直接 import adapters/apps，也可能通过一个内部
    模块间接触达——例如 `core/x.py` 写 `from lingxi.config import y`，而 `y`
    自己 import 了 `lingxi.adapters.z`。`find_violations` 只看每个文件的**直接**
    import 目标，看不到这种传递关系；2026-08-19 三方复查指出这是"无意可达"的
    （`core/` 已经在 import `lingxi.config.*`，将来任何人往被 import 的那个
    模块里加一句 adapters 导入，旧检查全程无感）。

    做法：对**整个** `src/lingxi/` 建一次 lingxi-internal import 图（每个源码
    模块 → 它直接 import 的其他 lingxi 模块，用 `check_installed_package.py`
    的 `_source_imports` 计算，只解析一次每个文件的 AST，图建好后每个 `core/`
    起点的遍历只是查字典，不重复解析）；再从每一个 `core/` 模块出发做 BFS，
    一旦触达 `lingxi.adapters.*` 或 `lingxi.apps.*`，报出完整的传递链条方便
    定位（"core/x.py → lingxi.config.y → lingxi.adapters.z"这种形式），而不是
    只说"违反了"却不知道从哪条路径违反的。
    """

    installed_pkg = _load_installed_package_checker()
    all_files = installed_pkg.source_module_files(source_root)
    if not all_files:
        raise LayeringCheckError(f"未能枚举到任何源码模块：{source_root}")

    # 图只建一次：每个模块的直接 lingxi-internal import 目标（含相对 import、
    # 父包沿途加载与"目标可能是子模块也可能是符号"两种情况的兜底）。
    edges: dict[str, set[str]] = {}
    for module, path in all_files.items():
        try:
            direct = installed_pkg._source_imports(module, all_files)
        except SyntaxError as error:
            raise LayeringCheckError(f"{_display_path(path)} 解析失败：{error}") from error
        ancestors = {
            name
            for name in installed_pkg._ancestor_packages(module, all_files)
        }
        edges[module] = direct | ancestors

    core_modules = sorted(m for m in all_files if m == "lingxi.core" or m.startswith("lingxi.core."))
    violations: list[str] = []
    for start in core_modules:
        # 逐起点 BFS；记录到每个已访问节点的最短路径，命中 adapters/apps 时
        # 把完整链条拼出来，报一次就够（同一个起点没必要报出它能触达的每一条
        # 违规路径，第一条已经足够定位问题）。
        visited: dict[str, str | None] = {start: None}
        queue = [start]
        hit: str | None = None
        while queue and hit is None:
            current = queue.pop(0)
            for neighbor in sorted(edges.get(current, ())):
                if neighbor in visited:
                    continue
                visited[neighbor] = current
                if neighbor.startswith(FORBIDDEN_LINGXI_PREFIXES):
                    hit = neighbor
                    break
                queue.append(neighbor)
        if hit is not None:
            chain = [hit]
            cursor = visited[hit]
            while cursor is not None:
                chain.append(cursor)
                cursor = visited[cursor]
            chain.reverse()
            location = _display_path(all_files[start])
            violations.append(
                f"{location}：`core/` 的 import 闭包经由 {' → '.join(chain)} "
                "间接触达了内部层外的模块（不是直接 import，是传递可达）"
            )
    return violations


def run_check() -> int:
    try:
        files = iter_core_files()
        violations: list[str] = []
        for path in files:
            violations.extend(find_violations(path))
        violations.extend(find_transitive_violations())
    except LayeringCheckError as error:
        print(f"core/ 分层 import 检查失败：{error}", file=sys.stderr)
        return 1

    if violations:
        print("core/ 分层 import 检查失败：", file=sys.stderr)
        for violation in violations:
            print(f"- {violation}", file=sys.stderr)
        return 1

    print(f"core/ 分层 import：通过（扫描 {len(files)} 个源文件，含函数内延迟导入，零违规）")
    return 0


def main(argv: list[str] | None = None) -> int:
    # 无任何可选参数——但仍要显式解析：一个空 parser 的默认行为对未识别参数
    # 直接报错退出（argparse 的标准姿态），不是像 `del argv` 那样把整份 argv
    # 静默吞掉。`allow_abbrev=False` 关掉前缀缩写匹配，本仓上一批次正是栽在
    # `--e` 缩写意外命中了另一个脚本里有真实副作用的选项。
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.parse_args(argv)
    return run_check()


if __name__ == "__main__":
    raise SystemExit(main())
