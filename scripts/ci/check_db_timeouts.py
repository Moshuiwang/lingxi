#!/usr/bin/env python3
"""校验正式与受控脚本的 PostgreSQL 连接都经过统一工厂，并且迁移连接有有限边界。

这条检查只看仓库代码，不连接数据库。它故意按 AST 识别 ``psycopg.connect`` 和
``self._psycopg.connect``，避免把注释或字符串里的历史文字误判为连接入口；工厂本身
是唯一允许直接调用驱动的文件。
"""

from __future__ import annotations

import ast
import pathlib
import sys

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
_PSYCOPG_CONNECTION_MODULES = frozenset({"psycopg", "psycopg.connection", "psycopg.connection_async"})


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
            failures.append(f"{_relative(path, source_root)}：无法解析，数据库连接门禁无法判断（{type(error).__name__}）")
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
                if direct or class_connect or _is_self_psycopg_connect(node) or (
                    isinstance(function, ast.Name) and function.id in raw_connect_names
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
        failures.append("migrations/alembic/env.py：create_engine 必须传入 migration_connect_args()")
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


def main() -> int:
    failures = [
        *check_runtime_connections(),
        *check_runtime_connections(CONTROLLED_SCRIPTS_ROOT),
        *check_migration_connection(),
    ]
    if failures:
        print("数据库超时门禁：不通过", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("数据库超时门禁：通过（正式连接统一工厂，迁移连接有限且独立配置）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
