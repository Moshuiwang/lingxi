#!/usr/bin/env python3
"""迁移里新建的数据库函数必须在定义语句内固定 ``search_path``（Issue #661 立规）。

只读 ``migrations/alembic/versions/*.py`` 的源码，不连库、不 import alembic。

**为什么要固定。** PL/pgSQL 在**执行时**按调用方会话的 ``search_path`` 解析函数体里
未限定的表名、函数名、类型名。谁能在搜索路径靠前的 schema 里建同名对象，谁就能让
函数访问到假表；对 ``SECURITY DEFINER`` 函数更是能让调用方代码以属主身份运行
（Supabase 巡检项 ``function_search_path_mutable`` 报的正是这一条）。固定为
``pg_catalog, pg_temp`` 后函数体只认系统目录，正式对象一律在函数体里写 schema 限定
（``public.app_user``），数据库设计第十节是规则正文。

**为什么 ``pg_temp`` 必须写、而且必须在末尾。** 不显式列出时 PostgreSQL 把 ``pg_temp``
**隐式排在最前面**——调用方 ``CREATE TEMP TABLE timestamptz (...)`` 就能顶掉同名类型，
让 DECLARE 里的赋值走到自己的类型转换函数上（``0054`` 的清理函数注释记录了 PG16 实测）。
写在末尾则 ``pg_catalog`` 先命中，临时对象永远排最后；写了但不在末尾等于没写。

**为什么取值必须恰好是 ``pg_catalog, pg_temp``。** 多放一个 ``public`` 在前面，``public``
里的同名函数就能顶掉 ``pg_catalog`` 的内建函数；函数体既然已经全限定，就不需要任何
业务 schema 出现在搜索路径里。

**判定范围。** 每一条 ``CREATE [OR REPLACE] FUNCTION | PROCEDURE`` 语句（任何语言：
``LANGUAGE sql`` 的函数体同样在执行时解析名字，Supabase 巡检也不分语言）；``LANGUAGE``
与 ``SET`` 子句在 ``AS $body$ … $body$`` 之前或之后都认，大小写、空白、``=`` 与 ``TO``、
自定义美元引号标签都不影响判定。**只看同一条语句**：同文件里另写一句
``ALTER FUNCTION … SET search_path`` 不算数——定义与固定分离时，下一次
``CREATE OR REPLACE`` 会把 ``proconfig`` 一并抹掉，那正是本门禁要挡的漂移。

**只判 upgrade 路径。** 只能从 ``downgrade()`` 到达的字符串（直接写在其中的字面量，
或只被它引用的模块级常量）不判：降级恢复的是历史定义，历史定义已由基线登记；
同时被 ``upgrade()`` 引用的字符串照判。文档字符串跳过。

**基线只许缩不许涨。** 历史上 19 个不带 ``SET search_path`` 的函数登记在
``plpgsql_search_path_baseline.txt``（「revision 文件名<TAB>函数名」），由
``0096_plpgsql_search_path`` 事后 ``ALTER FUNCTION … SET`` 固定；基线外任何缺它的定义
判红；登记的函数已带它或已不存在时判红并要求删掉那一行。没有 ``--refresh``：登记只会
因为改动历史 revision 而过期，而历史 revision 本就不许改；新增豁免要改基线文件，
且 ``tests/test_plpgsql_search_path_check.py`` 钉住了清单只能变短。

**响亮失败。** 目录不存在、一个函数定义都没解析到、SQL 词法解析不完整（引号或美元
引号没闭合）、函数定义由 f-string 动态拼接——都判红而不是静默通过：「检查了 0 个
函数还判绿」是本仓反复踩过的假绿。

已知边界：``DO`` 块或 ``EXECUTE`` 里动态拼出的 ``CREATE FUNCTION`` 藏在字符串体内，
静态扫描看不见；非 raw 字符串里的转义换行会让报出的行号偏移。
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
VERSIONS_DIR = REPOSITORY_ROOT / "migrations" / "alembic" / "versions"
BASELINE_PATH = REPOSITORY_ROOT / "scripts" / "ci" / "plpgsql_search_path_baseline.txt"
REQUIRED_SEARCH_PATH = ("pg_catalog", "pg_temp")

# 先做一次便宜的粗筛：只有可能含函数定义的字符串才进词法解析，避免把普通提示文案
# 里的一个撇号当成没闭合的 SQL 字符串。
_MAYBE_DEFINITION = re.compile(r"\bcreate\s+(?:or\s+replace\s+)?(?:function|procedure)\b", re.I)
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
_STRING_PREFIXES = frozenset("eEnNbBxX")


class CheckError(ValueError):
    """任何让判定无法进行的输入问题——必须失败关闭，不能当作「没有函数」继续跑。"""


class Token(NamedTuple):
    """SQL 词法单元：kind 是 word / number / string / ident / body / punct 之一。

    word 的 text 已折成小写（PostgreSQL 对未加引号的标识符与关键字同样折叠）；
    string / ident 保留原文（加引号的标识符区分大小写）；body 是美元引号包起来的整段。
    """

    kind: str
    text: str
    line: int


class Definition(NamedTuple):
    """一条函数或过程定义语句的判定结果；verdict 为 None 表示合规。"""

    path: Path
    line: int
    name: str
    qualified_name: str
    language: str | None
    verdict: str | None


def _read_quoted(sql: str, start: int, quote: str, backslash_escapes: bool) -> tuple[str, int]:
    """读一段以 quote 开头的引号串，返回（内容，结束后的下标）；连写两个引号是转义。"""

    index = start + 1
    pieces: list[str] = []
    while index < len(sql):
        char = sql[index]
        if backslash_escapes and char == "\\" and index + 1 < len(sql):
            pieces.append(sql[index : index + 2])
            index += 2
            continue
        if char == quote:
            if sql.startswith(quote, index + 1):
                pieces.append(quote)
                index += 2
                continue
            return "".join(pieces), index + 1
        pieces.append(char)
        index += 1
    raise CheckError(f"引号 {quote} 没有闭合")


def _read_block_comment(sql: str, start: int, line: int) -> int:
    """PostgreSQL 的块注释可以嵌套，按深度找到闭合处，返回其后的下标。"""

    depth, end = 1, start + 2
    while end < len(sql) and depth:
        if sql.startswith("/*", end):
            depth, end = depth + 1, end + 2
        elif sql.startswith("*/", end):
            depth, end = depth - 1, end + 2
        else:
            end += 1
    if depth:
        raise CheckError(f"第 {line} 行：块注释没有闭合")
    return end


def _read_dollar_quoted(sql: str, start: int, line: int) -> tuple[Token, int] | None:
    tag = _DOLLAR_TAG.match(sql, start)
    if tag is None:
        return None
    closing = sql.find(tag.group(0), tag.end())
    if closing < 0:
        raise CheckError(f"第 {line} 行：美元引号 {tag.group(0)} 没有闭合")
    return Token("body", sql[tag.end() : closing], line), closing + len(tag.group(0))


def _read_word_or_prefixed_string(sql: str, word: re.Match[str], line: int) -> tuple[Token, int]:
    end = word.end()
    # E'…' 这类带前缀的字符串：前缀字母紧贴单引号，E 前缀还允许反斜杠转义。
    if len(word.group(0)) == 1 and word.group(0) in _STRING_PREFIXES and sql.startswith("'", end):
        text, end = _read_quoted(sql, end, "'", backslash_escapes=word.group(0) in "eE")
        return Token("string", text, line), end
    return Token("word", word.group(0).lower(), line), end


def tokenize(sql: str) -> list[Token]:
    """按 PostgreSQL 词法把 SQL 切成单元：注释丢弃，引号串与美元引号体各成一个单元。"""

    tokens: list[Token] = []
    index, line = 0, 1
    while index < len(sql):
        char = sql[index]
        start = index
        if char.isspace():
            index += 1
        elif sql.startswith("--", index):
            end = sql.find("\n", index)
            index = len(sql) if end < 0 else end
        elif sql.startswith("/*", index):
            index = _read_block_comment(sql, index, line)
        elif char == "'":
            text, index = _read_quoted(sql, index, "'", backslash_escapes=False)
            tokens.append(Token("string", text, line))
        elif char == '"':
            text, index = _read_quoted(sql, index, '"', backslash_escapes=False)
            tokens.append(Token("ident", text, line))
        elif char == "$" and (dollar := _read_dollar_quoted(sql, index, line)):
            token, index = dollar
            tokens.append(token)
        elif word := _WORD.match(sql, index):
            token, index = _read_word_or_prefixed_string(sql, word, line)
            tokens.append(token)
        elif number := _NUMBER.match(sql, index):
            tokens.append(Token("number", number.group(0), line))
            index = number.end()
        else:
            tokens.append(Token("punct", char, line))
            index += 1
        line += sql.count("\n", start, index)
    return tokens


def split_statements(tokens: list[Token]) -> Iterator[list[Token]]:
    """按语句级的分号切分；引号串与美元引号体里的分号已经被词法阶段包进单元里。"""

    current: list[Token] = []
    for token in tokens:
        if token.kind == "punct" and token.text == ";":
            if current:
                yield current
            current = []
        else:
            current.append(token)
    if current:
        yield current


def _is_word(token: Token, text: str) -> bool:
    return token.kind == "word" and token.text == text


def _is_punct(token: Token, text: str) -> bool:
    return token.kind == "punct" and token.text == text


def _definition_head(statement: list[Token]) -> tuple[str, int] | None:
    """语句若以 CREATE [OR REPLACE] FUNCTION|PROCEDURE 开头，返回（限定名，参数表起点）。"""

    if not statement or not _is_word(statement[0], "create"):
        return None
    index = 1
    if (
        index + 1 < len(statement)
        and _is_word(statement[index], "or")
        and _is_word(statement[index + 1], "replace")
    ):
        index += 2
    if index >= len(statement) or not (
        _is_word(statement[index], "function") or _is_word(statement[index], "procedure")
    ):
        return None
    index += 1
    parts: list[str] = []
    while index < len(statement) and statement[index].kind in ("word", "ident"):
        parts.append(statement[index].text)
        index += 1
        if index < len(statement) and _is_punct(statement[index], "."):
            index += 1
            continue
        break
    if not parts or index >= len(statement) or not _is_punct(statement[index], "("):
        raise CheckError(
            f"第 {statement[0].line} 行：CREATE FUNCTION 之后不是「名字 (」的形状，无法判定"
        )
    return ".".join(parts), index


def _skip_parenthesised(statement: list[Token], index: int) -> int:
    depth = 0
    while index < len(statement):
        token = statement[index]
        index += 1
        if _is_punct(token, "("):
            depth += 1
        elif _is_punct(token, ")"):
            depth -= 1
            if depth == 0:
                return index
    return index


def _read_search_path_value(statement: list[Token], index: int) -> tuple[list[str] | str, int]:
    """读 ``SET search_path`` 后面的取值；返回（元素列表或特殊形态说明，下一个下标）。"""

    if index < len(statement) and (
        _is_punct(statement[index], "=") or _is_word(statement[index], "to")
    ):
        index += 1
    else:
        return "缺 = 或 TO", index
    if index + 1 < len(statement) and _is_word(statement[index], "from"):
        return "FROM CURRENT", index + 2
    if index < len(statement) and _is_word(statement[index], "default"):
        return "DEFAULT", index + 1
    elements: list[str] = []
    while index < len(statement):
        token = statement[index]
        if token.kind not in ("word", "string", "ident"):
            break
        elements.append(token.text)
        index += 1
        if index < len(statement) and _is_punct(statement[index], ","):
            index += 1
            continue
        break
    return elements, index


def _inspect_attributes(statement: list[Token], index: int) -> tuple[str | None, list]:
    """扫参数表之后的子句：取 LANGUAGE 与全部 SET search_path 的取值（括号内不看）。"""

    language: str | None = None
    values: list = []
    depth = 0
    while index < len(statement):
        token = statement[index]
        if _is_punct(token, "("):
            depth += 1
        elif _is_punct(token, ")"):
            depth -= 1
        elif depth == 0 and _is_word(token, "language") and index + 1 < len(statement):
            following = statement[index + 1]
            if following.kind in ("word", "string", "ident"):
                language = following.text.lower()
                index += 1
        elif (
            depth == 0
            and _is_word(token, "set")
            and index + 1 < len(statement)
            and statement[index + 1].kind in ("word", "ident")
            and statement[index + 1].text.lower() == "search_path"
        ):
            value, index = _read_search_path_value(statement, index + 2)
            values.append(value)
            continue
        index += 1
    return language, values


def judge_search_path(values: list) -> str | None:
    """全部 SET search_path 子句都必须是 pg_catalog, pg_temp；返回不合规的原因。"""

    if not values:
        return "缺 SET search_path = pg_catalog, pg_temp"
    for value in values:
        if isinstance(value, str) or not value:
            return f"SET search_path 的取值不是固定清单（写的是 {value or '空'}）"
        if "pg_temp" not in value:
            return "SET search_path 里没有 pg_temp（不写时它会被隐式排到最前面）"
        if value[-1] != "pg_temp":
            return f"pg_temp 不在 search_path 末尾（实际是 {', '.join(value)}）"
        if tuple(value) != REQUIRED_SEARCH_PATH:
            return f"search_path 取值应为 pg_catalog, pg_temp，实际是 {', '.join(value)}"
    return None


def definitions_in_sql(sql: str, path: Path, base_line: int) -> list[Definition]:
    """一段 SQL 里全部函数 / 过程定义的判定；base_line 是这段 SQL 首行在文件里的行号。"""

    found: list[Definition] = []
    for statement in split_statements(tokenize(sql)):
        head = _definition_head(statement)
        if head is None:
            continue
        qualified_name, index = head
        language, values = _inspect_attributes(statement, _skip_parenthesised(statement, index))
        found.append(
            Definition(
                path=path,
                line=base_line + statement[0].line - 1,
                name=qualified_name.rsplit(".", 1)[-1],
                qualified_name=qualified_name,
                language=language,
                verdict=judge_search_path(values),
            )
        )
    return found


def _docstring_ids(tree: ast.Module) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _module_constants_and_functions(
    tree: ast.Module,
) -> tuple[dict[str, ast.Constant], dict[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    constants: dict[str, ast.Constant] = {}
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions[node.name] = node
            continue
        target: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        value = getattr(node, "value", None)
        if (
            isinstance(target, ast.Name)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            constants[target.id] = value
    return constants, functions


def _downgrade_only_ids(tree: ast.Module) -> set[int]:
    """只能从 downgrade() 到达的字符串节点 id：其字面量与只被它引用的模块级字符串常量。"""

    constants, functions = _module_constants_and_functions(tree)

    def reachable_strings(root: str) -> set[int]:
        seen: set[str] = set()
        stack = [root]
        ids: set[int] = set()
        while stack:
            name = stack.pop()
            if name in seen or name not in functions:
                continue
            seen.add(name)
            for node in ast.walk(functions[name]):
                if isinstance(node, ast.Name):
                    stack.append(node.id)
                    if node.id in constants:
                        ids.add(id(constants[node.id]))
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    ids.add(id(node))
        return ids

    return reachable_strings("downgrade") - reachable_strings("upgrade")


def scan_revision(path: Path) -> list[Definition]:
    """一个 revision 文件里全部会在 upgrade 路径执行到的函数 / 过程定义。"""

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError) as error:
        raise CheckError(f"{path.name}：无法解析（{type(error).__name__}: {error}）") from error
    skipped = _docstring_ids(tree) | _downgrade_only_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for part in node.values:
                skipped.add(id(part))
                if isinstance(part, ast.Constant) and _MAYBE_DEFINITION.search(str(part.value)):
                    raise CheckError(
                        f"{path.name}:{node.lineno} 用 f-string 拼接函数定义，静态检查判定不了"
                        "它最终固定了什么 search_path：请写成完整的字符串常量"
                    )
    found: list[Definition] = []
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Constant)
            or not isinstance(node.value, str)
            or id(node) in skipped
            or not _MAYBE_DEFINITION.search(node.value)
        ):
            continue
        try:
            found.extend(definitions_in_sql(node.value, path, node.lineno))
        except CheckError as error:
            raise CheckError(f"{path.name}:{node.lineno} 起的 SQL：{error}") from error
    return found


def parse_baseline(text: str) -> set[tuple[str, str]]:
    """解析「revision 文件名<TAB>函数名」登记表；任何一行格式不对都直接抛错。"""

    entries: set[tuple[str, str]] = set()
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip("\n")
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 2 or not all(parts) or not parts[0].endswith(".py"):
            raise CheckError(
                f"基线文件第 {line_number} 行格式不合法（应为「revision 文件名<TAB>函数名」）：{line!r}"
            )
        entry = (parts[0], parts[1])
        if entry in entries:
            raise CheckError(f"基线文件第 {line_number} 行重复登记：{line!r}")
        entries.add(entry)
    return entries


def load_baseline(path: Path) -> set[tuple[str, str]]:
    if not path.is_file():
        raise CheckError(f"基线文件不存在：{path}")
    try:
        return parse_baseline(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise CheckError(f"无法读取基线文件 {path}：{error}") from error


def _location(definition: Definition) -> str:
    path = definition.path
    if path.is_relative_to(REPOSITORY_ROOT):
        path = path.relative_to(REPOSITORY_ROOT)
    return f"{path.as_posix()}:{definition.line}"


def evaluate(definitions: list[Definition], baseline: set[tuple[str, str]]) -> list[str]:
    """基线外缺 search_path 的定义判红；基线登记却已无对应缺失定义的也判红（只许缩）。"""

    failures: list[str] = []
    used: set[tuple[str, str]] = set()
    for definition in definitions:
        if definition.verdict is None:
            continue
        key = (definition.path.name, definition.name)
        if key in baseline:
            used.add(key)
            continue
        failures.append(
            f"{_location(definition)} {definition.qualified_name}：{definition.verdict}"
        )
    for file_name, function_name in sorted(baseline - used):
        failures.append(
            f"基线登记的 {file_name} / {function_name} 已经没有对应的缺 SET search_path 的定义"
            "（文件不存在、函数改名，或定义已带 SET search_path）："
            f"基线只能变短，请从 {BASELINE_PATH.name} 删掉这一行。"
        )
    return failures


def run(versions_dir: Path, baseline_path: Path) -> int:
    if not versions_dir.is_dir():
        print(
            f"数据库函数 search_path 门禁失败：revision 目录不存在：{versions_dir}", file=sys.stderr
        )
        return 1
    files = sorted(versions_dir.glob("*.py"))
    if not files:
        print(
            f"数据库函数 search_path 门禁失败：{versions_dir} 下一个 revision 都没有",
            file=sys.stderr,
        )
        return 1
    try:
        baseline = load_baseline(baseline_path)
    except CheckError as error:
        print(f"数据库函数 search_path 门禁失败：{error}", file=sys.stderr)
        return 1

    failures: list[str] = []
    definitions: list[Definition] = []
    for path in files:
        try:
            definitions.extend(scan_revision(path))
        except CheckError as error:
            failures.append(str(error))
    if not definitions:
        failures.append(
            f"扫描了 {len(files)} 个 revision 文件却没有解析到任何函数定义——仓库里明明有触发器函数，"
            "这说明解析没工作，不能当作「没有违规」放行。"
        )
    failures.extend(evaluate(definitions, baseline))
    if failures:
        print("数据库函数 search_path 门禁：不通过", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    compliant = sum(1 for definition in definitions if definition.verdict is None)
    print(
        f"数据库函数 search_path 门禁：通过（{len(files)} 个 revision 文件、"
        f"{len(definitions)} 个函数定义：{compliant} 个带 SET search_path = pg_catalog, pg_temp，"
        f"{len(definitions) - compliant} 个在 {len(baseline)} 条历史基线豁免内）"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="数据库函数 search_path 门禁", allow_abbrev=False)
    parser.add_argument("--versions-dir", type=Path, default=VERSIONS_DIR, help=argparse.SUPPRESS)
    parser.add_argument("--baseline", type=Path, default=BASELINE_PATH, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    return run(args.versions_dir, args.baseline)


if __name__ == "__main__":
    raise SystemExit(main())
