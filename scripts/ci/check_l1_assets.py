#!/usr/bin/env python3
"""L1 用户可见资产轻门禁（Issue #498）。

L1 只覆盖三份已批准的用户可见事实源：``content.toml``、它的版本锁，以及
``admin_metric_alias_map.toml``。这里复用 #190 的内容摘要/锁校验，并对别名表与
管理员可见出口做 fail-closed 术语扫描。门禁不导入业务包、不连接数据库、不读取生产
数据或凭据，因此它明显轻于 Story Fast / Epic Full，但不会把「轻」误当成「不校验」。

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
) -> list[str]:
    """返回 L1 失败列表；空列表表示通过。"""

    failures: list[str] = []
    content, content_errors = CONTENT_CHECKER.load_content(content_path)
    lock, lock_errors = CONTENT_CHECKER.load_lock(lock_path)
    failures.extend(content_errors)
    failures.extend(lock_errors)
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
    print("L1 用户可见资产门禁：通过（content 版本/锁、别名形状、术语扫描）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--content", type=Path, default=CONTENT_PATH)
    parser.add_argument("--lock", type=Path, default=LOCK_PATH)
    parser.add_argument("--aliases", type=Path, default=ALIAS_PATH)
    parser.add_argument("--admin-source-root", type=Path, default=ADMIN_SOURCE_ROOT)
    parser.add_argument("--daily-report", type=Path, default=DAILY_REPORT_PATH)
    args = parser.parse_args()
    return run_check(
        content_path=args.content,
        lock_path=args.lock,
        alias_path=args.aliases,
        admin_source_root=args.admin_source_root,
        daily_report_path=args.daily_report,
    )


if __name__ == "__main__":
    raise SystemExit(main())
