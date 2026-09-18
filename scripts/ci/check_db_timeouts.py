#!/usr/bin/env python3
"""校验正式与受控脚本的 PostgreSQL 连接都经过统一工厂、借用不逃出 ``with``，且迁移连接有有限边界。

这条检查只看仓库代码，不连接数据库。它故意按 AST 识别 ``psycopg.connect`` 和
``self._psycopg.connect``，避免把注释或字符串里的历史文字误判为连接入口；工厂本身
是唯一允许直接调用驱动的文件。

连接借用围栏（``check_connection_borrow_scope``）守的是工厂默认复用路径的调用方义务：
``connect()`` 归还后旧引用会随下一次借出恢复操作能力，所以默认复用路径的 ``connect()``
必须写在 ``with`` 语句的上下文表达式位置，块内取得的连接与游标不得存进属性 / 容器 /
块外名字、不得随 return 逃出、不得在块退出后继续使用，块内 ``close()`` 之后不得再取
``connect()``。识别工厂时覆盖 ``from … import connect``、``import … as x; x.connect``、
``from … import connect as y``、``from lingxi.adapters import postgres``、相对 import、
``getattr(模块, "connect")`` 与文件内的别名赋值；``dedicated=True`` 字面量或任何额外的
字面量关键字参数在工厂里就走驱动原生连接，不进空闲栈，不受本围栏约束；``**kwargs`` 展开
或 ``dedicated=<变量>`` 静态判不出走哪条路，一律按默认复用路径要求。

围栏的已知边界（如实登记，不构成绕过许可）：只看语法位置，连接 / 游标作为实参传给
别的函数、被 ``yield`` 交给消费者、被解包赋值（``a, b = …``）、经嵌套函数闭包捕获，
以及 ``close()`` 藏在辅助函数里再取 ``connect()``、``getattr`` 的属性名不是字面量、
``importlib`` 等动态取名，都判不出；名字追踪不分先后、以文件 / 块为单位，宁可多判。
围栏不是修复：归还前取得的游标（``Cursor.connection`` 回指原始连接）、旧 ``pgconn``、
以及块内提前 ``close()`` 后再取 ``connect()`` 的间接写法，围栏堵不住。
"""

from __future__ import annotations

import ast
import pathlib
import sys
import types
from collections.abc import Mapping
from typing import NamedTuple

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]
RUNTIME_SOURCE_ROOT = REPOSITORY_ROOT / "src" / "lingxi"
CONTROLLED_SCRIPTS_ROOT = REPOSITORY_ROOT / "scripts"
MIGRATION_ENV = REPOSITORY_ROOT / "migrations" / "alembic" / "env.py"
MIGRATION_DSN = REPOSITORY_ROOT / "migrations" / "alembic" / "migration_dsn.py"


def _relative(path: pathlib.Path, root: pathlib.Path) -> str:
    return str(path.relative_to(root.parent))


def _is_self_psycopg_connect(node: ast.Call) -> bool:
    function = node.func
    return (
        isinstance(function, ast.Attribute)
        and function.attr == "connect"
        and isinstance(function.value, ast.Attribute)
        and function.value.attr == "_psycopg"
        and isinstance(function.value.value, ast.Name)
        and function.value.value.id == "self"
    )


# 建连相关的 psycopg 命名空间：顶层 `psycopg`（`psycopg.connect`）和它承载
# 连接类的两个子模块 `psycopg.connection`（`Connection`）、
# `psycopg.connection_async`（`AsyncConnection`）。只匹配字面量 `"psycopg"` 会漏过
# `from psycopg.connection import Connection` 与 `import psycopg.connection`——两者
# 的 `module`/`name` 都不等于 `"psycopg"`，但仍是同一个建连入口（Issue #116）。
#
# 故意不做成“任意 `psycopg.*` 子模块都禁止”：`psycopg.types.json` 之类的类型
# 适配子模块与建连无关，adapters 层已有合法的 `from psycopg.types.json import Json`
# 用法，不应被这条门禁误杀。
_PSYCOPG_CONNECTION_MODULES = frozenset(
    {"psycopg", "psycopg.connection", "psycopg.connection_async"}
)


def _is_psycopg_connection_module(module: str | None) -> bool:
    return module in _PSYCOPG_CONNECTION_MODULES


# psycopg 对外暴露的连接类：``Connection.connect(dsn)`` / ``AsyncConnection.connect(dsn)``
# 是与 ``psycopg.connect(dsn)`` 等价的建连入口，只是绕开了模块级函数，从
# ``psycopg`` 顶层或 ``psycopg.connection`` / ``psycopg.connection_async`` 子模块导入。
_PSYCOPG_CONNECTION_CLASS_NAMES = frozenset({"Connection", "AsyncConnection"})


def check_runtime_connections(source_root: pathlib.Path = RUNTIME_SOURCE_ROOT) -> list[str]:
    """拒绝绕过 ``lingxi.adapters.postgres.connect`` 的驱动连接。"""

    factory = source_root / "adapters" / "postgres.py"
    failures: list[str] = []
    for path in sorted(source_root.rglob("*.py")):
        if "__pycache__" in path.parts or path == factory:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError) as error:
            failures.append(
                f"{_relative(path, source_root)}：无法解析，数据库连接门禁无法判断（{type(error).__name__}）"
            )
            continue

        psycopg_names = {"psycopg"}
        raw_connect_names: set[str] = set()
        connection_class_names: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                for item in node.names:
                    if item.name in _PSYCOPG_CONNECTION_MODULES:
                        # `import psycopg.connection` 不带 `as` 时，Python 绑定的本地
                        # 名字仍是顶层 `psycopg`；带 `as` 时绑定的是子模块对象本身，
                        # 同样可能拿来 `.connect(...)`，两种都要记入 psycopg_names。
                        psycopg_names.add(item.asname or item.name.split(".", 1)[0])
            elif isinstance(node, ast.ImportFrom) and _is_psycopg_connection_module(node.module):
                for item in node.names:
                    if item.name == "connect":
                        raw_connect_names.add(item.asname or "connect")
                    elif item.name in _PSYCOPG_CONNECTION_CLASS_NAMES:
                        connection_class_names.add(item.asname or item.name)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                item.name in _PSYCOPG_CONNECTION_MODULES for item in node.names
            ):
                failures.append(
                    f"{_relative(path, source_root)}:{node.lineno} 直接导入 psycopg："
                    "正式代码只能由 lingxi.adapters.postgres.connect 延迟导入驱动"
                )
            elif isinstance(node, ast.ImportFrom) and _is_psycopg_connection_module(node.module):
                failures.append(
                    f"{_relative(path, source_root)}:{node.lineno} 直接从 psycopg 导入："
                    "正式代码只能由 lingxi.adapters.postgres.connect 延迟导入驱动"
                )
            elif isinstance(node, ast.Call):
                function = node.func
                direct = (
                    isinstance(function, ast.Attribute)
                    and function.attr == "connect"
                    and isinstance(function.value, ast.Name)
                    and function.value.id in psycopg_names
                )
                class_connect = (
                    isinstance(function, ast.Attribute)
                    and function.attr == "connect"
                    and isinstance(function.value, ast.Name)
                    and function.value.id in connection_class_names
                )
                if (
                    direct
                    or class_connect
                    or _is_self_psycopg_connect(node)
                    or (isinstance(function, ast.Name) and function.id in raw_connect_names)
                ):
                    failures.append(
                        f"{_relative(path, source_root)}:{node.lineno} 发现裸 PostgreSQL 连接："
                        "必须调用 lingxi.adapters.postgres.connect"
                    )
    return failures


def check_migration_connection() -> list[str]:
    """迁移工具链必须显式接入独立、有限的连接参数。"""

    failures: list[str] = []
    env_source = MIGRATION_ENV.read_text(encoding="utf-8")
    dsn_source = MIGRATION_DSN.read_text(encoding="utf-8")
    if "connect_args=migration_connect_args()" not in env_source:
        failures.append(
            "migrations/alembic/env.py：create_engine 必须传入 migration_connect_args()"
        )
    required_names = (
        "MIGRATION_CONNECT_TIMEOUT_SECONDS",
        "MIGRATION_STATEMENT_TIMEOUT_SECONDS",
        "MIGRATION_LOCK_TIMEOUT_SECONDS",
        "def migration_connect_args",
    )
    for name in required_names:
        if name not in dsn_source:
            failures.append(f"migrations/alembic/migration_dsn.py：缺少有限迁移连接配置 {name}")
    return failures


# ---------------------------------------------------------------- 连接借用围栏

FACTORY_MODULE = "lingxi.adapters.postgres"
FACTORY_CONNECT = f"{FACTORY_MODULE}.connect"

#: 借用围栏的逐调用点豁免：键 = (``_relative`` 给出的路径, 行号)，例如
#: ``("lingxi/adapters/x.py", 42)``，值 = 理由；只放行「这一行的默认复用 ``connect()``
#: 可以不在 with 上下文位置」，精确到文件 + 行，不接受目录或文件名通配。直接从字面量
#: 构造只读映射：运行期 ``[...] = ...`` 抛 TypeError，扫描函数只用定义时绑定的默认值，
#: 钉住测试在 import 期读快照——运行期扩容既改不动也看不见。四处独占路径不用登记：
#: ``dedicated=True`` 字面量在工厂里就走驱动原生连接、不进空闲栈，按调用形态识别。
BORROW_SCOPE_EXEMPTIONS: Mapping[tuple[str, int], str] = types.MappingProxyType({})

_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPE_NODES = (*_FUNCTION_NODES, ast.ClassDef, ast.Lambda)


class BorrowScopeScan(NamedTuple):
    """一次围栏扫描的结果：判红清单与三类调用点计数。"""

    failures: list[str]
    reused_calls: int
    dedicated_calls: int
    exempted_calls: int


def _module_name(relative: str) -> str:
    parts = relative[: -len(".py")].split("/") if relative.endswith(".py") else relative.split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolve_import_from(node: ast.ImportFrom, module_name: str, is_package: bool) -> str | None:
    """把相对 import 解析成绝对模块名；绝对 import 原样返回。"""
    if node.level == 0:
        return node.module
    package = module_name.split(".") if module_name else []
    if not is_package:
        package = package[:-1]
    ascend = node.level - 1
    if ascend > len(package):
        return None
    base = package[: len(package) - ascend] if ascend else package
    joined = ".".join(base)
    if node.module:
        return f"{joined}.{node.module}" if joined else node.module
    return joined or None


def _collect_bindings(tree: ast.Module, module_name: str, is_package: bool) -> dict[str, str]:
    """本文件里每个本地名字对应的绝对点分名（import 与别名赋值，全文件、不分作用域）。

    不分作用域是有意的：一个名字只要在文件任何位置绑定到工厂，所有同名调用都按工厂
    调用审查——宁可多判、不给别名留缝。
    """
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.asname:
                    bindings[item.asname] = item.name
                else:
                    top = item.name.split(".", 1)[0]
                    bindings[top] = top
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_import_from(node, module_name, is_package)
            if base is None:
                continue
            for item in node.names:
                if item.name != "*":
                    bindings[item.asname or item.name] = f"{base}.{item.name}"
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if len(targets) != 1 or not isinstance(targets[0], ast.Name):
                continue
            canonical = _dotted(node.value, bindings)
            if canonical is None or not (
                canonical == FACTORY_MODULE or canonical.startswith(f"{FACTORY_MODULE}.")
            ):
                continue
            if bindings.get(targets[0].id) != canonical:
                bindings[targets[0].id] = canonical
                changed = True
    return bindings


def _dotted(node: ast.AST, bindings: Mapping[str, str]) -> str | None:
    """``Name`` / ``Attribute`` 链或 ``getattr(<链>, "常量")`` 的绝对点分名；解析不了返回 None。"""
    if isinstance(node, ast.Call):
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) in (2, 3)
            and not node.keywords
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            owner = _dotted(node.args[0], bindings)
            return None if owner is None else f"{owner}.{node.args[1].value}"
        return None
    attributes: list[str] = []
    while isinstance(node, ast.Attribute):
        attributes.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name) or node.id not in bindings:
        return None
    return ".".join([bindings[node.id], *reversed(attributes)])


def _reaches_factory(canonical: str) -> bool:
    """这个绝对点分名是工厂模块本身、它的成员，或它的某级父包（``import lingxi`` 也算）。"""
    return (
        canonical == FACTORY_MODULE
        or canonical.startswith(f"{FACTORY_MODULE}.")
        or FACTORY_MODULE.startswith(f"{canonical}.")
    )


def _is_factory_call(node: ast.AST, bindings: Mapping[str, str]) -> bool:
    return isinstance(node, ast.Call) and _dotted(node.func, bindings) == FACTORY_CONNECT


def _classify_factory_call(node: ast.Call) -> str:
    """按工厂运行期会走的路径分类：``reused`` / ``dedicated`` / ``unknown``。

    ``dedicated=True`` 字面量与任何额外的字面量关键字参数在工厂里都走驱动原生连接
    （不进空闲栈）；``**kwargs`` 展开或 ``dedicated=<非字面量>`` 静态判不出，记为
    ``unknown``，围栏按最严的默认复用路径对待。
    """
    unpacked = False
    dynamic_dedicated = False
    for keyword in node.keywords:
        if keyword.arg is None:
            unpacked = True
        elif keyword.arg == "timeouts":
            continue
        elif keyword.arg == "dedicated":
            literal = isinstance(keyword.value, ast.Constant)
            if literal and keyword.value.value is True:
                return "dedicated"
            if not (literal and keyword.value.value is False):
                dynamic_dedicated = True
        else:
            return "dedicated"
    return "unknown" if unpacked or dynamic_dedicated else "reused"


def _walk_scope(node: ast.AST):
    """遍历一个作用域自己的节点，不进入嵌套的函数 / 类 / lambda。"""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        yield child
        if not isinstance(child, _SCOPE_NODES):
            stack.extend(ast.iter_child_nodes(child))


def _is_cursor_expr(node: ast.AST, connections: set[str], cursors: set[str]) -> bool:
    """``连接.cursor(...)`` / ``连接.execute(...)`` / ``游标.execute(...)``（返回游标自身）/ 游标名。"""
    if isinstance(node, ast.Name):
        return node.id in cursors
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    owner = node.func.value
    if node.func.attr == "cursor":
        return isinstance(owner, ast.Name) and owner.id in connections
    if node.func.attr == "execute":
        return (isinstance(owner, ast.Name) and owner.id in connections) or _is_cursor_expr(
            owner, connections, cursors
        )
    return False


def _handle_names(node: ast.AST, connections: set[str], cursors: set[str]) -> bool:
    """连接名或游标名本身（不含表达式）。"""
    return isinstance(node, ast.Name) and (node.id in connections or node.id in cursors)


def _assignment_pairs(node: ast.AST) -> list[tuple[ast.AST, ast.AST]]:
    if isinstance(node, ast.Assign):
        return [(target, node.value) for target in node.targets]
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        return [(node.target, node.value)]
    if isinstance(node, ast.NamedExpr):
        return [(node.target, node.value)]
    return []


def _borrow_with_items(with_node: ast.With, bindings: Mapping[str, str]) -> set[str]:
    """``with`` 各项里默认复用路径的工厂调用绑定的连接名。"""
    connections: set[str] = set()
    for item in with_node.items:
        if (
            _is_factory_call(item.context_expr, bindings)
            and _classify_factory_call(item.context_expr) != "dedicated"
        ):
            if isinstance(item.optional_vars, ast.Name):
                connections.add(item.optional_vars.id)
    return connections


def _tracked_names_in(with_node: ast.With, seed: set[str]) -> tuple[set[str], set[str]]:
    """``with`` 体内（含各项）绑定到连接与游标的名字；不分先后、取到不动点，宁可多判。"""
    connections, cursors = set(seed), set()
    pairs: list[tuple[ast.AST, ast.AST]] = []
    for node in ast.walk(with_node):
        if isinstance(node, ast.With):
            pairs.extend(
                (item.optional_vars, item.context_expr)
                for item in node.items
                if isinstance(item.optional_vars, ast.Name)
            )
        pairs.extend(_assignment_pairs(node))
    changed = True
    while changed:
        changed = False
        for target, value in pairs:
            if not isinstance(target, ast.Name):
                continue
            if isinstance(value, ast.Name) and value.id in connections:
                bucket = connections
            elif _is_cursor_expr(value, connections, cursors):
                bucket = cursors
            else:
                continue
            if target.id not in bucket:
                bucket.add(target.id)
                changed = True
    return connections, cursors


def _declared_outward(scope: ast.AST, with_node: ast.With) -> set[str]:
    """所在作用域（不含嵌套函数）与 ``with`` 体内（含嵌套函数）``global`` / ``nonlocal`` 过的名字。"""
    names: set[str] = set()
    for node in [*_walk_scope(scope), *ast.walk(with_node)]:
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
    return names


def _escapes_inside(
    scope: ast.AST,
    with_node: ast.With,
    connections: set[str],
    cursors: set[str],
    bindings: Mapping[str, str],
    *,
    relative: str,
) -> list[str]:
    """``with`` 体内的逃逸：存进属性 / 容器 / 外层名字、随 return 逃出、close() 后再 connect()。"""
    failures: list[str] = []
    module_scope = isinstance(scope, (ast.Module, ast.ClassDef))
    outward = _declared_outward(scope, with_node)
    close_lines: list[int] = []
    for node in ast.walk(with_node):
        for target, value in _assignment_pairs(node):
            leaks = _is_cursor_expr(value, connections, cursors) or _handle_names(
                value, connections, cursors
            )
            if not leaks:
                continue
            if isinstance(target, (ast.Attribute, ast.Subscript)):
                failures.append(
                    f"{relative}:{node.lineno} 连接或游标存进了属性 / 容器：它们只在 with 块内"
                    "有效，归还后旧引用会随下一次借出恢复操作能力"
                )
            elif isinstance(target, ast.Name) and (target.id in outward or module_scope):
                failures.append(
                    f"{relative}:{node.lineno} 游标或连接赋给了 with 块外的名字"
                    "（global / nonlocal / 模块级），逃出 with 块"
                )
        if isinstance(node, ast.Return) and node.value is not None:
            if _is_cursor_expr(node.value, connections, cursors) or _handle_names(
                node.value, connections, cursors
            ):
                failures.append(f"{relative}:{node.lineno} 连接或游标随 return 逃出 with 块")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "close"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in connections
        ):
            close_lines.append(node.lineno)
    if close_lines:
        first_close = min(close_lines)
        for node in ast.walk(with_node):
            if (
                _is_factory_call(node, bindings)
                and _classify_factory_call(node) != "dedicated"
                and node.lineno > first_close
            ):
                failures.append(
                    f"{relative}:{node.lineno} with 块内已 close() 归还的连接，同一块内又取"
                    " connect()：同一对象会被再次借出，外层 with 退出成为迟到操作者"
                )
    return failures


def _uses_after(scope: ast.AST, with_node: ast.With, names: set[str], relative: str) -> list[str]:
    """``with`` 退出后、同一作用域（不含嵌套函数）里再次读取连接名或游标名；重新绑定后不再追。"""
    end = with_node.end_lineno or with_node.lineno
    rebound: dict[str, int] = {}
    loads: list[tuple[int, str]] = []
    for node in _walk_scope(scope):
        if not isinstance(node, ast.Name) or node.id not in names or node.lineno <= end:
            continue
        if isinstance(node.ctx, ast.Load):
            loads.append((node.lineno, node.id))
        else:
            rebound[node.id] = min(rebound.get(node.id, node.lineno), node.lineno)
    failures: list[str] = []
    for lineno, name in sorted(set(loads)):
        if lineno < rebound.get(name, lineno + 1):
            failures.append(
                f"{relative}:{lineno} with 块已退出，名字 {name} 指向的连接或游标仍在使用"
            )
    return failures


def _scan_borrow_scope_file(
    path: pathlib.Path,
    source_root: pathlib.Path,
    exemptions: Mapping[tuple[str, int], str],
) -> BorrowScopeScan:
    relative = _relative(path, source_root)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError) as error:
        return BorrowScopeScan(
            [f"{relative}：无法解析，连接借用围栏无法判断（{type(error).__name__}）"], 0, 0, 0
        )
    bindings = _collect_bindings(tree, _module_name(relative), path.name == "__init__.py")
    if not any(_reaches_factory(value) for value in bindings.values()):
        return BorrowScopeScan([], 0, 0, 0)

    context_ids = {
        id(item.context_expr)
        for node in ast.walk(tree)
        if isinstance(node, ast.With)
        for item in node.items
    }
    failures: list[str] = []
    reused = dedicated = exempted = 0
    for node in ast.walk(tree):
        if not _is_factory_call(node, bindings):
            continue
        if _classify_factory_call(node) == "dedicated":
            dedicated += 1
            continue
        reused += 1
        if id(node) in context_ids:
            continue
        if (relative, node.lineno) in exemptions:
            exempted += 1
            continue
        failures.append(
            f"{relative}:{node.lineno} 默认复用路径的 connect() 没有写在 with 语句的上下文"
            "位置：借用必须随 with 退出归还，不得赋值、传参或返回后再用"
            "（独占连接请显式传 dedicated=True）"
        )

    scopes = [
        tree,
        *(n for n in ast.walk(tree) if isinstance(n, (*_FUNCTION_NODES, ast.ClassDef))),
    ]
    for scope in scopes:
        for with_node in _walk_scope(scope):
            if not isinstance(with_node, ast.With):
                continue
            if not _borrow_with_items(with_node, bindings):
                continue
            connections, cursors = _tracked_names_in(
                with_node, _borrow_with_items(with_node, bindings)
            )
            failures.extend(
                _escapes_inside(scope, with_node, connections, cursors, bindings, relative=relative)
            )
            failures.extend(_uses_after(scope, with_node, connections | cursors, relative))
    return BorrowScopeScan(failures, reused, dedicated, exempted)


def scan_connection_borrow_scope(
    source_root: pathlib.Path = RUNTIME_SOURCE_ROOT,
    *,
    exemptions: Mapping[tuple[str, int], str] = BORROW_SCOPE_EXEMPTIONS,
) -> BorrowScopeScan:
    """默认复用路径的连接与游标不得逃出 ``with`` 块：判红清单与计数。"""
    factory = source_root / "adapters" / "postgres.py"
    failures: list[str] = []
    reused = dedicated = exempted = 0
    for path in sorted(source_root.rglob("*.py")):
        if "__pycache__" in path.parts or path == factory:
            continue
        result = _scan_borrow_scope_file(path, source_root, exemptions)
        failures.extend(result.failures)
        reused += result.reused_calls
        dedicated += result.dedicated_calls
        exempted += result.exempted_calls
    return BorrowScopeScan(failures, reused, dedicated, exempted)


def check_connection_borrow_scope(
    source_root: pathlib.Path = RUNTIME_SOURCE_ROOT,
    *,
    exemptions: Mapping[tuple[str, int], str] = BORROW_SCOPE_EXEMPTIONS,
) -> list[str]:
    """默认复用路径的 ``connect()`` 必须写在 ``with`` 上下文位置，其连接与游标不得逃出块。"""
    return scan_connection_borrow_scope(source_root, exemptions=exemptions).failures


def main() -> int:
    runtime_scope = scan_connection_borrow_scope()
    scripts_scope = scan_connection_borrow_scope(CONTROLLED_SCRIPTS_ROOT)
    failures = [
        *check_runtime_connections(),
        *check_runtime_connections(CONTROLLED_SCRIPTS_ROOT),
        *runtime_scope.failures,
        *scripts_scope.failures,
        *check_migration_connection(),
    ]
    if failures:
        print("数据库超时门禁：不通过", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    reused = runtime_scope.reused_calls + scripts_scope.reused_calls
    dedicated = runtime_scope.dedicated_calls + scripts_scope.dedicated_calls
    exempted = runtime_scope.exempted_calls + scripts_scope.exempted_calls
    print(
        "数据库超时门禁：通过（正式连接统一工厂，迁移连接有限且独立配置；借用围栏："
        f"默认复用调用点 {reused} 处全部在 with 上下文位置，独占 {dedicated} 处，逐行豁免 {exempted} 处）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
