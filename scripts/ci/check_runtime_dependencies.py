#!/usr/bin/env python3
"""断言 V-部署-08 的执行器：运行时依赖全部在 `pyproject.toml` 声明，且安全边界组件锁精确版本。

这条断言在验证与门禁里被标为"已认领"很久了，但**一直没有执行脚本**（Issue #58 遗留）。
散文形式的约束不会被守住——AGENTS.md 已经把这条教训写死了：「只有给 X 一个断言编号
并让它在 CI 里变红，它才会被实装」。本脚本就是那个"会变红"的部分。

两个半边，缺一不可：

**上半边（--imports）：全部声明。**
`src/lingxi/` 里**没有任何模块级第三方 import，全部是函数内延迟导入**（pyproject.toml
与代码框架第六节都反复强调过这一点）。因此只扫模块级 import 等于没扫：一个写在函数体
里的 `import httpx` 会完全逃过检查，直到生产镜像里第一次走到那个分支才炸——而那时它
表现为一次用户请求失败，不是一次构建失败。所以这里用 `ast` 遍历**整棵树**，
不区分嵌套层级。

**下半边（--pins）：安全边界锁精确版本。**
安全边界所依赖的外部组件必须写 `==`，不能写范围。理由不是洁癖：范围区间会让依赖升级
在**代码一个字符都没改**的情况下改变安全边界的行为，而那种变化不会触发任何评审。
两个当前对象：
  - `cryptography`：Fernet 是宿主机凭据文件的加密边界（adapters/delegated_credentials.py）。
  - `claude-agent-sdk`：执行层的只读屏障建立在这个版本的 hook 事件名与工具名上
    （`PostToolUseFailure` 存在、子代理叫 `Agent` 不叫 `Task`、`HookMatcher` 的签名），
    全是实测得到、SDK 未作稳定性承诺的细节。

**本脚本只读 `pyproject.toml`，从不修改它。** 发现缺失声明时报告并失败，由人决定怎么
补——2026-08-06 编排者裁定⑥：#62 发现缺口回报编排者，不就地改（#57 同期在编辑
pyproject 新增 gateway extra，避免双线写入）。

用法：

    python3 scripts/ci/check_runtime_dependencies.py          # 两个半边都跑
    python3 scripts/ci/check_runtime_dependencies.py --imports
    python3 scripts/ci/check_runtime_dependencies.py --pins
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys
import tomllib

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]
PYPROJECT = REPOSITORY_ROOT / "pyproject.toml"
# 扫描范围是**正式代码根**。migrations/ 下的 alembic 脚本不在此列：迁移工具链本就
# 不该出现在 src/ 里（断言 V-迁移-04 反向扫描这一点），它的依赖由 migrate extra 声明。
SOURCE_ROOT = REPOSITORY_ROOT / "src" / "lingxi"

# 顶层 import 名 → PyPI 分发包名。
#
# **这张表必须手工维护，不能自动推导**——import 名与分发名没有可靠的机械对应
# （`psycopg[binary]` 装出来的顶层模块是 `psycopg`，`lark-oapi` 是 `lark_oapi`，
# `claude-agent-sdk` 是 `claude_agent_sdk`）。表里没有的第三方 import 一律判失败，
# 这正是新依赖被发现的方式：加依赖的人必须同时在这里登记它属于哪个分发包。
#
# 不在表里 ≠ 不合法，只是**没人登记过**。补表 + 补 pyproject 声明，两件事都要做。
IMPORT_TO_DISTRIBUTION = {
    "cryptography": "cryptography",
    "psycopg": "psycopg",
    "claude_agent_sdk": "claude-agent-sdk",
    "lark_oapi": "lark-oapi",
    "websockets": "websockets",
}

# 安全边界组件 → 为什么它必须锁精确版本。
# 值会原样出现在失败信息里：一条只说"请锁版本"的报错，下一个人多半会照做而不理解，
# 然后在某次"顺手放宽一下"时把它改回去。
EXACT_PIN_REQUIRED = {
    "cryptography": "Fernet 是宿主机专用授权凭据文件的加密边界；升级须重跑凭据保管验证",
    "claude-agent-sdk": "执行层只读屏障依赖该版本的 hook 事件名与工具名；升级须重跑 L4a",
}

# PEP 508 依赖串的最小解析：名字、可选 extras、其余全部当版本约束。
# 不引入 packaging——它不是本仓库的声明依赖，为了一个门禁脚本引入运行时依赖是本末倒置。
_REQUIREMENT = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?P<extras>\[[^\]]*\])?\s*(?P<specifier>.*?)\s*$"
)
# 精确锁：整个约束恰好是一条 `==<版本>`。`==1.2.*` 不算——它是一个范围。
_EXACT_PIN = re.compile(r"^==\s*[A-Za-z0-9][A-Za-z0-9.\-+!]*$")


def normalize_distribution_name(name: str) -> str:
    """PEP 503 归一化：小写，连续的 `-_.` 折成单个 `-`。"""

    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirement(raw: str) -> tuple[str, str]:
    """把一条依赖串拆成（归一化分发名, 版本约束）。解析不出名字时抛 ValueError。"""

    match = _REQUIREMENT.match(raw)
    if match is None or not match.group("name"):
        raise ValueError(f"无法解析依赖声明：{raw!r}")
    return normalize_distribution_name(match.group("name")), match.group("specifier") or ""


def declared_requirements() -> dict[str, list[tuple[str, str]]]:
    """读 pyproject 的全部依赖声明：分发名 → [(extra 组名, 版本约束)]。

    同一个包出现在多组里是正常的（psycopg 就在三组里），所以值是列表：任何一组
    写松了都要被抓到，不能只看第一处。
    """

    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = data.get("project", {})
    groups: dict[str, list[str]] = {"（主依赖）": list(project.get("dependencies", []))}
    for extra, items in (project.get("optional-dependencies") or {}).items():
        groups[extra] = list(items)

    found: dict[str, list[tuple[str, str]]] = {}
    for extra, items in groups.items():
        for raw in items:
            name, specifier = parse_requirement(raw)
            found.setdefault(name, []).append((extra, specifier))
    return found


def iter_source_files() -> list[pathlib.Path]:
    return sorted(SOURCE_ROOT.rglob("*.py"))


def imported_top_level_modules() -> dict[str, list[str]]:
    """遍历 `src/lingxi/` 的每一棵 AST，收集顶层 import 名 → [出处]。

    **不区分嵌套层级**：`ast.walk` 会走进函数体、类体、`try` 块与 `if TYPE_CHECKING`。
    本仓库的第三方 import 恰恰全部写在函数体里，只看 `tree.body` 会一个都扫不到。
    """

    found: dict[str, list[str]] = {}

    def record(module: str, path: pathlib.Path, line: int) -> None:
        top = module.split(".")[0]
        if not top:
            return
        try:
            location = path.relative_to(REPOSITORY_ROOT)
        except ValueError:
            # 扫描根被指到仓库之外（用例会这么做）。出处退化成绝对路径即可，
            # 不该为了一个显示用的相对路径把整次扫描炸掉。
            location = path
        found.setdefault(top, []).append(f"{location}:{line}")

    for path in iter_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    record(alias.name, path, node.lineno)
            elif isinstance(node, ast.ImportFrom):
                # 相对 import（level > 0）是包内引用，不是外部依赖。
                if node.level or not node.module:
                    continue
                record(node.module, path, node.lineno)
    return found


def check_imports(declared: dict[str, list[tuple[str, str]]]) -> list[str]:
    """每个第三方 import 都必须映射到某个 extra 已声明的分发包。"""

    failures: list[str] = []
    stdlib = sys.stdlib_module_names
    for module, sites in sorted(imported_top_level_modules().items()):
        if module in stdlib or module == "lingxi":
            continue
        distribution = IMPORT_TO_DISTRIBUTION.get(module)
        if distribution is None:
            failures.append(
                f"第三方 import `{module}`（{sites[0]}，共 {len(sites)} 处）没有登记在 "
                "IMPORT_TO_DISTRIBUTION 里。它属于哪个 PyPI 分发包？请补本脚本的映射表，"
                "并确认 pyproject.toml 的对应 extra 真的声明了它。"
            )
            continue
        if normalize_distribution_name(distribution) not in declared:
            failures.append(
                f"第三方 import `{module}`（{sites[0]}）映射到分发包 `{distribution}`，"
                "但 pyproject.toml 的任何一组依赖里都没有它——生产镜像不会装上，"
                "走到这行代码时才会 ImportError。"
            )
    return failures


def check_pins(declared: dict[str, list[tuple[str, str]]]) -> list[str]:
    """安全边界组件必须锁精确版本（`==`），不接受范围。"""

    failures: list[str] = []
    for distribution, reason in sorted(EXACT_PIN_REQUIRED.items()):
        key = normalize_distribution_name(distribution)
        occurrences = declared.get(key)
        if not occurrences:
            failures.append(
                f"安全边界组件 `{distribution}` 在 pyproject.toml 里找不到声明。"
                "它被移除了，还是改名了？"
            )
            continue
        for extra, specifier in occurrences:
            if not _EXACT_PIN.match(specifier.strip()):
                failures.append(
                    f"安全边界组件 `{distribution}` 在 extra `{extra}` 里写的是 "
                    f"`{specifier.strip() or '（无版本约束）'}`，必须是精确的 `==<版本>`。"
                    f"\n      原因：{reason}。"
                    "\n      范围区间会让依赖升级在代码未变的情况下改变这条安全边界，"
                    "而那种变化不触发任何评审。"
                )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--imports", action="store_true", help="只跑「全部声明」半边")
    parser.add_argument("--pins", action="store_true", help="只跑「精确锁」半边")
    args = parser.parse_args()
    run_imports = args.imports or not args.pins
    run_pins = args.pins or not args.imports

    if not PYPROJECT.is_file():
        print(f"V-部署-08：找不到 {PYPROJECT}", file=sys.stderr)
        return 1
    if not SOURCE_ROOT.is_dir():
        print(f"V-部署-08：找不到源码根 {SOURCE_ROOT}", file=sys.stderr)
        return 1

    declared = declared_requirements()
    failures: list[str] = []
    if run_imports:
        failures.extend(check_imports(declared))
    if run_pins:
        failures.extend(check_pins(declared))

    if failures:
        print("V-部署-08 运行时依赖声明：不通过", file=sys.stderr)
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        return 1

    if run_imports:
        modules = imported_top_level_modules()
        third_party = sorted(
            m for m in modules if m not in sys.stdlib_module_names and m != "lingxi"
        )
        print(
            f"V-部署-08 全部声明：扫描 {len(iter_source_files())} 个源文件的全部 import"
            f"（含函数内延迟导入），{len(third_party)} 个第三方顶层模块"
            f"（{', '.join(third_party)}）均已在 pyproject.toml 声明"
        )
    if run_pins:
        print(
            f"V-部署-08 精确锁：{len(EXACT_PIN_REQUIRED)} 个安全边界组件"
            f"（{', '.join(sorted(EXACT_PIN_REQUIRED))}）均为 `==` 精确版本"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
