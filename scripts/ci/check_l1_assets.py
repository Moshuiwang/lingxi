#!/usr/bin/env python3
"""L1 用户可见资产轻门禁（Issue #498）。

L1 只覆盖三份已批准的用户可见事实源：``content.toml``、它的版本锁，以及
``admin_metric_alias_map.toml``。这里复用 #190 的内容摘要/锁校验，断言内容目录的键
集合与运行时登记表精确相等（Issue #520 F1），并对别名表与管理员可见出口做
fail-closed 术语扫描。门禁不导入业务包、不连接数据库、不读取生产数据或凭据，因此它
明显轻于 Story Fast / Epic Full，但不会把「轻」误当成「不校验」。

提示词（L2）不在本脚本中实现；将来若产品批准事实源，应先登记到分类器并另行补合同。
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import re
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONTENT_PATH = REPOSITORY_ROOT / "src" / "lingxi" / "config" / "content.toml"
LOCK_PATH = REPOSITORY_ROOT / "src" / "lingxi" / "config" / "content.lock.toml"
ALIAS_PATH = REPOSITORY_ROOT / "src" / "lingxi" / "config" / "admin_metric_alias_map.toml"
ADMIN_SOURCE_ROOT = REPOSITORY_ROOT / "src" / "lingxi" / "core" / "admin"
DAILY_REPORT_PATH = REPOSITORY_ROOT / "src" / "lingxi" / "core" / "daily_report.py"
CONTENT_MODULE_PATH = REPOSITORY_ROOT / "src" / "lingxi" / "config" / "content.py"

# Issue #520 F1：运行时 ``ContentCatalog.from_mapping`` 对 ``[texts]`` / ``[cards]``
# 的键集合做**精确相等**比对（``_require_exact_keys``），少一个键或多一个键都会抛
# ``ContentValidationError``，进程在加载内容目录时就起不来。此前这道门禁只校验版本
# 锁与术语，不看键集合：删一条文案键、或新增一条没人消费的键，只要递增版本并刷新锁
# 就全绿，而线上必然加载失败。下面这张表把「运行时要求什么键」与「配置里有什么键」
# 绑到一起，**用 ast 静态读取，不 import lingxi**，门禁保持零业务依赖。
CONTENT_KEY_DECLARATIONS = (
    ("texts", "REQUIRED_TEXT_KEYS", "文案"),
    ("cards", "REQUIRED_CARD_KEYS", "卡片"),
)

# 这些是用户出口已退役的中文动词；内部数据库/回调 token 是 ASCII，不在此扫描。
RETIRED_TERMS = ("收回", "抑制", "新增授权", "新增抑制")
METRIC_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9_.@:一-鿿-]{1,128}$")


def _load_content_checker():
    path = Path(__file__).with_name("check_content_version.py")
    spec = importlib.util.spec_from_file_location("content_version_for_l1", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载内容版本门禁：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CONTENT_CHECKER = _load_content_checker()


def _display(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def load_aliases(path: Path) -> tuple[dict[str, str] | None, list[str]]:
    """严格解析别名表；运行时适配器的便利 fail-open 不适用于 CI。"""

    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except FileNotFoundError:
        return None, [f"L1 别名表不存在：{path}"]
    except (OSError, tomllib.TOMLDecodeError) as error:
        return None, [f"L1 别名表无法解析：{path}（{error}）"]

    if not isinstance(document, dict):
        return None, [f"L1 别名表顶层必须是表：{path}"]
    errors: list[str] = []
    unknown = sorted(set(document) - {"aliases"})
    if unknown:
        errors.append("L1 别名表出现未登记顶层表：" + "、".join(unknown))
    aliases = document.get("aliases")
    if not isinstance(aliases, Mapping):
        errors.append("L1 别名表缺少 [aliases] 表")
        return None, errors

    valid: dict[str, str] = {}
    for key, value in aliases.items():
        if not isinstance(key, str) or not key:
            errors.append("L1 别名表的别名键必须是非空文本")
            continue
        if not isinstance(value, str) or not METRIC_VALUE_PATTERN.fullmatch(value):
            errors.append(
                f"L1 别名表的 {key!r} 右值必须匹配指标 token 形状 "
                "[A-Za-z0-9_.@:一-鿿-]{1,128}"
            )
            continue
        valid[key] = value
    return (valid if not errors else None), errors


def _bound_names(node: ast.AST) -> set[str]:
    """返回 ``node`` 这一条语句直接绑定（或解绑）的名字。

    只认 ``Store``/``Del`` 上下文里的 ``ast.Name``，所以 ``mapping[X] = v``（``X``
    是 Load）和 ``obj.X = v``（``X`` 是属性名、不是 Name 节点）都不会被误判成对
    ``X`` 的绑定；``__all__`` 里的字符串同理不算。
    """

    def _targets(*expressions: ast.expr | None) -> set[str]:
        names: set[str] = set()
        for expression in expressions:
            if expression is None:
                continue
            for child in ast.walk(expression):
                if isinstance(child, ast.Name) and isinstance(
                    child.ctx, (ast.Store, ast.Del)
                ):
                    names.add(child.id)
        return names

    if isinstance(node, ast.Assign):
        return _targets(*node.targets)
    if isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
        return _targets(node.target)
    if isinstance(node, (ast.For, ast.AsyncFor)):
        return _targets(node.target)
    if isinstance(node, ast.withitem):
        return _targets(node.optional_vars)
    if isinstance(node, ast.Delete):
        return _targets(*node.targets)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    if isinstance(node, ast.ExceptHandler):
        return {node.name} if node.name else set()
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return {alias.asname or alias.name.split(".")[0] for alias in node.names}
    return set()


def required_content_keys(module_path: Path) -> tuple[dict[str, frozenset[str]] | None, list[str]]:
    """静态读出 ``content.py`` 里登记的必需键集合。

    刻意用 ``ast`` 而不是 import：这道门禁的合同是「不导入业务包」，import 会把
    ``lingxi`` 的依赖闭包和副作用带进 CI 的轻量路径。只认**模块顶层**的直接赋值，
    并要求右值是字面量元组/列表——任何需要求值才能确定的写法（拼接、条件、函数
    调用）都当作读不出来而失败关闭，绝不猜一个可能不完整的集合。

    「唯一一处顶层字面量赋值」这件事在**整棵语法树**上判定（rc24 fable 审查 P2-1）。
    旧实现只遍历 ``tree.body`` 且只认 ``Assign``/``AnnAssign``，于是两类改写能骗过
    它：模块顶层的 ``REQUIRED_TEXT_KEYS += (...)``（``ast.AugAssign``）被整条跳过，
    ``if``/``try``/函数体里的重新绑定压根看不见——门禁读到的是前面那条字面量，
    运行时读到的却是改过的值，是一条真实可用的绕过路径。现在扫全树：目标名在任何
    位置出现第二次绑定（增广赋值、重绑、``del``、``import as``、同名函数/类定义
    ……），或者唯一那次绑定不在模块顶层，都失败关闭。
    """

    try:
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    except (OSError, SyntaxError) as error:
        return None, [
            f"L1 键集合检查无法解析 {_display(module_path, REPOSITORY_ROOT)}：{error}"
        ]

    wanted = {name for _, name, _ in CONTENT_KEY_DECLARATIONS}
    top_level = {id(statement) for statement in tree.body}
    bindings: dict[str, list[ast.AST]] = {name: [] for name in wanted}
    for node in ast.walk(tree):
        for name in _bound_names(node) & wanted:
            bindings[name].append(node)

    display = _display(module_path, REPOSITORY_ROOT)
    found: dict[str, frozenset[str]] = {}
    errors: list[str] = []
    for name in sorted(wanted):
        nodes = bindings[name]
        if not nodes:
            errors.append(
                f"{display} 里找不到模块顶层的 {name}："
                "运行时必需键的登记表被改名或移动了，L1 门禁不能再证明配置与运行时一致"
            )
            continue
        if len(nodes) > 1:
            lines = "、".join(
                str(getattr(node, "lineno", "?")) for node in nodes
            )
            errors.append(
                f"{display} 的 {name} 在模块里被绑定了 {len(nodes)} 次（第 {lines} 行）："
                "增广赋值或重新绑定会让门禁读到的集合与运行时实际生效的集合不一致，"
                "只允许一处模块顶层的字面量赋值"
            )
            continue
        node = nodes[0]
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or id(node) not in top_level:
            errors.append(
                f"{display} 的 {name} 不是模块顶层的直接赋值"
                f"（第 {getattr(node, 'lineno', '?')} 行 {type(node).__name__}）："
                "门禁只静态读取模块顶层的字面量赋值，其他写法一律失败关闭"
            )
            continue
        value = node.value
        try:
            literal = ast.literal_eval(value) if value is not None else None
        except (ValueError, TypeError, SyntaxError):
            literal = None
        if not isinstance(literal, (tuple, list)) or not all(
            isinstance(item, str) for item in literal
        ):
            errors.append(
                f"{display} 的 {name} "
                "必须是模块顶层的字面量字符串元组，否则门禁无法静态确定必需键"
            )
            continue
        found[name] = frozenset(literal)

    if errors:
        return None, errors
    return found, []


def _content_key_failures(
    content: Mapping[str, Any], content_path: Path, module_path: Path
) -> list[str]:
    """content.toml 的键集合必须与运行时登记表**精确相等**（Issue #520 F1）。"""

    declared, errors = required_content_keys(module_path)
    if declared is None:
        return errors

    failures: list[str] = []
    display = _display(content_path, REPOSITORY_ROOT)
    for table, name, kind in CONTENT_KEY_DECLARATIONS:
        actual = content.get(table)
        if not isinstance(actual, Mapping):
            failures.append(
                f"{display} 缺少 [{table}] 表或它不是表；运行时会直接拒绝加载内容目录"
            )
            continue
        required = declared[name]
        missing = sorted(required - set(actual))
        extra = sorted(set(actual) - required)
        if not missing and not extra:
            continue
        details: list[str] = []
        if missing:
            details.append("缺少 " + "、".join(missing))
        if extra:
            details.append("多余 " + "、".join(extra))
        failures.append(
            f"{display}:[{table}] 的{kind}键与 "
            f"{_display(module_path, REPOSITORY_ROOT)} 的 {name} 不是精确相等："
            + "；".join(details)
            + "。运行时 ContentCatalog.from_mapping 就是精确比对，键集合不等会抛 "
            "ContentValidationError，进程加载内容目录时直接失败"
        )
    return failures


def _string_literals_without_docstrings(path: Path) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as error:
        raise ValueError(f"术语扫描无法解析 {_display(path, REPOSITORY_ROOT)}：{error}") from error

    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            docstring_ids.add(id(body[0].value))

    literals: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstring_ids:
            literals.append((node.lineno, node.value))
    return literals


def _source_term_failures(source_root: Path, daily_report: Path) -> list[str]:
    files = sorted(source_root.glob("*.py")) + [daily_report]
    failures: list[str] = []
    for path in files:
        try:
            literals = _string_literals_without_docstrings(path)
        except ValueError as error:
            failures.append(str(error))
            continue
        for line, value in literals:
            hits = [term for term in RETIRED_TERMS if term in value]
            if hits:
                failures.append(
                    f"{_display(path, REPOSITORY_ROOT)}:{line}: 出现退役术语 "
                    f"{hits}（管理员可见字符串不得使用；统一为补充授权/屏蔽指标/撤销）"
                )
    return failures


def _content_term_failures(content: Mapping[str, Any], path: Path) -> list[str]:
    pairs, errors = CONTENT_CHECKER.flatten_visible(content)
    if errors:
        return errors
    failures: list[str] = []
    for key, value in pairs:
        hits = [term for term in RETIRED_TERMS if term in value]
        if hits:
            failures.append(
                f"{_display(path, REPOSITORY_ROOT)}:{key}: 出现退役术语 {hits}；"
                "用户可见文案统一为补充授权/屏蔽指标/撤销"
            )
    return failures


def _alias_term_failures(aliases: Mapping[str, str], path: Path) -> list[str]:
    failures: list[str] = []
    for key, value in aliases.items():
        hits = [term for term in RETIRED_TERMS if term in key or term in value]
        if hits:
            failures.append(
                f"{_display(path, REPOSITORY_ROOT)}:[aliases].{key!r}: "
                f"出现退役术语 {hits}；用户可见别名统一为补充授权/屏蔽指标/撤销"
            )
    return failures


def check_l1_assets(
    content_path: Path = CONTENT_PATH,
    lock_path: Path = LOCK_PATH,
    alias_path: Path = ALIAS_PATH,
    *,
    admin_source_root: Path = ADMIN_SOURCE_ROOT,
    daily_report_path: Path = DAILY_REPORT_PATH,
    content_module_path: Path = CONTENT_MODULE_PATH,
) -> list[str]:
    """返回 L1 失败列表；空列表表示通过。"""

    failures: list[str] = []
    content, content_errors = CONTENT_CHECKER.load_content(content_path)
    lock, lock_errors = CONTENT_CHECKER.load_lock(lock_path)
    failures.extend(content_errors)
    failures.extend(lock_errors)
    if content is not None and not content_errors:
        # 与版本锁无关的一道独立断言：版本锁只证明「文案变了版本也跟着变了」，
        # 证明不了「键集合仍然是运行时要求的那一套」。递增版本 + 刷新锁能让锁校验
        # 全绿，但删键/加键仍会在运行时加载失败，所以这里必须单独判一次。
        failures.extend(_content_key_failures(content, content_path, content_module_path))
    if content is not None and lock is not None and not content_errors and not lock_errors:
        failures.extend(CONTENT_CHECKER.evaluate(content, lock))
        failures.extend(_content_term_failures(content, content_path))

    aliases, alias_errors = load_aliases(alias_path)
    failures.extend(alias_errors)
    if aliases is not None and not alias_errors:
        failures.extend(_alias_term_failures(aliases, alias_path))

    failures.extend(_source_term_failures(admin_source_root, daily_report_path))
    return failures


def run_check(**kwargs: Any) -> int:
    failures = check_l1_assets(**kwargs)
    if failures:
        print("L1 用户可见资产门禁失败：", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("L1 用户可见资产门禁：通过（content 键集合/版本/锁、别名形状、术语扫描）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--content", type=Path, default=CONTENT_PATH)
    parser.add_argument("--lock", type=Path, default=LOCK_PATH)
    parser.add_argument("--aliases", type=Path, default=ALIAS_PATH)
    parser.add_argument("--admin-source-root", type=Path, default=ADMIN_SOURCE_ROOT)
    parser.add_argument("--daily-report", type=Path, default=DAILY_REPORT_PATH)
    parser.add_argument("--content-module", type=Path, default=CONTENT_MODULE_PATH)
    args = parser.parse_args()
    return run_check(
        content_path=args.content,
        lock_path=args.lock,
        alias_path=args.aliases,
        admin_source_root=args.admin_source_root,
        daily_report_path=args.daily_report,
        content_module_path=args.content_module,
    )


if __name__ == "__main__":
    raise SystemExit(main())
