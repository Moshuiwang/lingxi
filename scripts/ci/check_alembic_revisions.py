#!/usr/bin/env python3
"""校验 alembic revision 链的结构性约束（Issue #53）。**不连数据库**。

真库那半边由 ``scripts/ci/check_migration_chain.sh`` 负责（两条链建库并对比）。
分开是因为这半边的四类缺陷根本不需要数据库就能判定，而"需要数据库"会让它们在
没有容器的环境里静默消失——恰恰是缺陷最容易溜进去的时候：

1. **多个 head。** ``alembic upgrade head`` 在多 head 时报错，但一条**孤儿 revision**
   （谁都不指向它）只是安静地不被执行。改造前的门禁写的是 ``for f in migrations/*.sql``，
   每个文件都保证跑到；换成 alembic 之后这个保证消失了，是本次改造引入的真实覆盖面
   缩水。这里显式断言 head 恰好一个、且全部 revision 都在从 base 到 head 的那条路上。
2. **``down_revision`` 指向不存在的 id。** 建图时就会失败。
3. **静默的空 downgrade。** ``def downgrade(): pass`` 会让 ``alembic downgrade``
   "成功"并把版本号退回去，而库结构没变；之后每一次 upgrade 都建立在假的起点上。
   要么真正逆转，要么显式 ``raise``（基线走后者，见裁定③）。
4. **文档与实现脱节。** head revision id 要写进 ``migrations/README.md``——旧库接管
   命令要用它，下游 Issue 接链也要用它。不核对，README 会在第二条 revision 落地时
   就过期，而过期的接管命令比没有命令更危险。

另外核对 ``alembic.ini`` 不含可用的默认连接串（断言 V-迁移-05）。

本脚本 import alembic：**没装就明确失败**，不 try/except 跳过。门禁里一个"因为缺
依赖所以跳过"的检查，和没有这个检查是一回事，但它看起来是绿的。
"""

from __future__ import annotations

import ast
import configparser
import pathlib
import sys

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPOSITORY_ROOT / "alembic.ini"
MIGRATIONS_ROOT = REPOSITORY_ROOT / "migrations"
MIGRATIONS_README = MIGRATIONS_ROOT / "README.md"
RUNTIME_SOURCE_ROOT = REPOSITORY_ROOT / "src"

# 迁移工具链不得进入运行时代码（断言 V-迁移-04）。引入 alembic 最容易走偏的方向，
# 是顺手用 SQLAlchemy 定义一套 ORM 模型再让业务代码去 import：那样"迁移工具"就变成了
# 运行时依赖，生产镜像必须装它，而代码框架第二节的三层 import 规则也被绕开
# （adapters/ 才是唯一允许 import 外部 SDK 的层，core/ 连 I/O 都不做）。
FORBIDDEN_IN_RUNTIME = ("sqlalchemy", "alembic")

# alembic_version.version_num 的默认列宽。超长的 id 要到写版本行时才失败，
# 那时库已经被改了一半。
MAX_REVISION_LENGTH = 32


def _fail_without_alembic() -> None:
    try:
        import alembic  # noqa: F401
    except ModuleNotFoundError as error:
        print(
            "alembic 没装，迁移链检查无法进行。这不是可跳过的情况："
            "门禁跳过迁移检查等于没有迁移检查。\n"
            "  安装：python3 -m pip install '.[migrate]'\n"
            f"  原始错误：{error}",
            file=sys.stderr,
        )
        raise SystemExit(1)


def check_ini(text: str) -> list[str]:
    """``alembic.ini`` 不得留下可用的默认连接串。

    留一个"能用的默认串"意味着有人在没设 ``LINGXI_MIGRATION_DSN`` 时把迁移跑进了
    别的库，而且不会有任何提示。缺环境变量必须是**明确失败**。
    """

    parser = configparser.RawConfigParser()
    parser.read_string(text)
    failures: list[str] = []
    for section in parser.sections():
        for key in ("sqlalchemy.url", "url"):
            value = parser.get(section, key, fallback="").strip()
            if value:
                failures.append(
                    f"alembic.ini 的 [{section}] 里 {key} 有值：迁移连接串只能来自环境变量，"
                    "留默认串会让没设变量的执行悄悄连上别的库。"
                )
    return failures


def check_runtime_isolation(source_root: pathlib.Path) -> list[str]:
    """``src/`` 里不得出现迁移工具链的任何字样（断言 V-迁移-04）。

    等价于验收口径的 ``grep -rn "sqlalchemy\\|alembic" src/`` 必须为空，
    做成门禁的一部分而不是一次性人工核对——它要挡的是**将来**某次改动，
    不是这一次。

    **跳过打包产物。** 在仓库里跑过 ``pip install .`` 之后，``src/lingxi.egg-info/``
    会出现，其中 ``requires.txt`` / ``PKG-INFO`` 如实列出 ``migrate`` extra 的
    ``alembic``。那是声明的回声，不是运行时代码引用了它；这些目录已在 ``.gitignore``
    里。裸 ``grep -rn ... src/`` 在装过包的机器上会因此报出两行——**验收时若看到，
    先确认路径是不是 ``*.egg-info/``**。
    """

    failures: list[str] = []
    for path in sorted(source_root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if any(part.endswith(".egg-info") for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lowered = text.lower()
        for name in FORBIDDEN_IN_RUNTIME:
            if name in lowered:
                line_number = next(
                    index for index, line in enumerate(text.splitlines(), 1) if name in line.lower()
                )
                failures.append(
                    f"{path.relative_to(source_root.parent)}:{line_number} 出现 {name!r}："
                    "迁移工具链不得进入运行时代码，生产进程不装 SQLAlchemy。"
                )
    return failures


def embedded_copy_failures(path: pathlib.Path, migrations_root: pathlib.Path) -> list[str]:
    """内嵌的编号 SQL 副本必须与磁盘上的原文逐字节相同。

    基线 revision 声称自己是 ``migrations/006``–``012`` 的**逐字节副本**，整个转换
    方案的正确性都压在这句话上。真库门禁靠"两条链建库后 schema 逐字节相等"来守它，
    但那道防线只看得见 schema：副本里多一句 ``INSERT``、``GRANT``、往非 public
    schema 建对象，或只改注释，两边 ``pg_dump --schema=public`` 依然相等，而副本
    与原文已经分叉了。而且真库那半边**没有容器就整体不跑**。

    这里做的是直接的字节比对，不需要数据库，因此处处都跑。识别方式是 revision 文件
    里的 ``CHAIN``（``(源文件名, 常量名)`` 的元组），只有基线有它；其余 revision
    没有 CHAIN，本函数直接返回空。
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    constants: dict[str, str] = {}
    chain_node: ast.expr | None = None
    for node in tree.body:
        target: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        if not isinstance(target, ast.Name) or node.value is None:
            continue
        if target.id == "CHAIN":
            chain_node = node.value
        elif isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            constants[target.id] = node.value.value

    if chain_node is None:
        return []
    if not isinstance(chain_node, ast.Tuple):
        return [f"{path.name}：CHAIN 不是元组字面量，无法核对内嵌副本"]

    failures: list[str] = []
    covered: set[str] = set()
    for element in chain_node.elts:
        if (
            not isinstance(element, ast.Tuple)
            or len(element.elts) != 2
            or not isinstance(element.elts[0], ast.Constant)
            or not isinstance(element.elts[1], ast.Name)
        ):
            failures.append(f"{path.name}：CHAIN 的元素不是 (源文件名, 常量名) 形状")
            continue
        source_name = element.elts[0].value
        variable = element.elts[1].id
        covered.add(source_name)
        embedded = constants.get(variable)
        if embedded is None:
            failures.append(f"{path.name}：CHAIN 引用了不存在的字符串常量 {variable}")
            continue
        source_path = migrations_root / source_name
        if not source_path.is_file():
            failures.append(f"{path.name}：CHAIN 引用的源文件不存在：migrations/{source_name}")
            continue
        original = source_path.read_text(encoding="utf-8")
        if embedded != original:
            failures.append(
                f"{path.name} 内嵌的 {variable} 与 migrations/{source_name} 已分叉"
                f"（内嵌 {len(embedded)} 字符、磁盘 {len(original)} 字符）。"
                "基线声称自己是逐字节副本，这条声明是整个转换方案正确性的前提；"
                "两边都要改，或者都不改。"
            )

    on_disk = {item.name for item in sorted(migrations_root.glob("*.sql"))}
    for missing in sorted(on_disk - covered):
        failures.append(
            f"migrations/{missing} 没有被任何 revision 的 CHAIN 覆盖："
            "编号 SQL 已冻结，新增文件不会进入基线，两条链会就此分叉。"
        )
    for extra in sorted(covered - on_disk):
        failures.append(f"{path.name}：CHAIN 覆盖了磁盘上不存在的 migrations/{extra}")
    return failures


def downgrade_failures(path: pathlib.Path) -> list[str]:
    """逐个 version 文件核对 ``downgrade()`` 不是静默空实现。"""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "downgrade":
            body = list(node.body)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                body = body[1:]  # 去掉文档字符串
            if not body or all(isinstance(statement, ast.Pass) for statement in body):
                return [
                    f"{path.name}：downgrade() 是空实现。空的 downgrade 会"
                    "「成功」地把版本号退回去而结构不变，之后的 upgrade 全建立在假起点上。"
                    "要么真正逆转，要么显式 raise。"
                ]
            has_raise = any(isinstance(child, ast.Raise) for child in ast.walk(node))
            has_operation = any(
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id == "op"
                for child in ast.walk(node)
            )
            if not has_raise and not has_operation:
                return [
                    f"{path.name}：downgrade() 既没有 op.* 操作也没有 raise，"
                    "判定不了它到底做没做事。"
                ]
            return []
    return [f"{path.name}：没有 downgrade() 函数"]


def main() -> int:
    _fail_without_alembic()

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    failures: list[str] = []
    failures.extend(check_ini(ALEMBIC_INI.read_text(encoding="utf-8")))
    failures.extend(check_runtime_isolation(RUNTIME_SOURCE_ROOT))

    # 建图本身就是一道检查：down_revision 指向不存在的 id 时这里抛异常。
    # alembic 抛的是 `KeyError: '<那个 id>'`，裸传上去只会得到一段 sqlalchemy 内部
    # 调用栈——红是红了，但读的人要自己认出「这是 down_revision 写错了」。
    try:
        script = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))
        script.get_heads()  # 触发 revision map 构建，把建图错误留在这里
    except Exception as error:  # noqa: BLE001 - 建图失败的形态由 alembic 决定
        print("alembic revision 链检查：不通过", file=sys.stderr)
        print(
            f"  - 无法构建 revision 图（{type(error).__name__}: {error}）。"
            "最常见的原因是某个 revision 的 down_revision 指向了不存在的 id，"
            "或 versions/ 下有重复的 revision id。",
            file=sys.stderr,
        )
        return 1

    heads = script.get_heads()
    bases = script.get_bases()
    if len(heads) != 1:
        failures.append(
            f"revision 链有 {len(heads)} 个 head（{'、'.join(heads) or '无'}），必须恰好 1 个。"
            "多 head 时 `alembic upgrade head` 会报错，而孤儿 revision 只是安静地不被执行。"
        )
    if len(bases) != 1:
        failures.append(
            f"revision 链有 {len(bases)} 个 base（{'、'.join(bases) or '无'}），必须恰好 1 个。"
        )

    all_revisions = list(script.walk_revisions())
    for revision in all_revisions:
        if len(revision.revision) > MAX_REVISION_LENGTH:
            failures.append(
                f"revision id {revision.revision!r} 长 {len(revision.revision)}，"
                f"超过 alembic_version.version_num 的 {MAX_REVISION_LENGTH}"
            )

    # 从 head 一路走到 base，能走到的必须是全部——走不到的就是孤儿。
    if len(heads) == 1:
        reachable = {item.revision for item in script.iterate_revisions(heads[0], "base")}
        orphans = sorted({item.revision for item in all_revisions} - reachable)
        if orphans:
            failures.append(
                "以下 revision 不在从 head 到 base 的路径上（孤儿，永远不会被 upgrade 执行）："
                + "、".join(orphans)
            )

    for revision in all_revisions:
        revision_path = pathlib.Path(revision.path)
        failures.extend(downgrade_failures(revision_path))
        failures.extend(embedded_copy_failures(revision_path, MIGRATIONS_ROOT))

    # head 与 base 的 id 必须出现在 README：接管旧库的命令要用 base（基线），
    # 下游 Issue 接链与部署核对要用 head。
    readme = MIGRATIONS_README.read_text(encoding="utf-8")
    documented: dict[str, list[str]] = {}
    for label, identifiers in (("head", heads), ("基线 base", bases)):
        for identifier in identifiers:
            documented.setdefault(identifier, []).append(label)
    for identifier, labels in documented.items():
        if identifier not in readme:
            failures.append(
                f"migrations/README.md 里没有 {'/'.join(labels)} revision id `{identifier}`。"
                "新增 revision 后必须同步 README，过期的接管命令比没有命令更危险。"
            )

    if failures:
        print("alembic revision 链检查：不通过", file=sys.stderr)
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        return 1

    print(
        f"alembic revision 链：通过（{len(all_revisions)} 条 revision，"
        f"head={heads[0]}，base={bases[0]}，无孤儿，downgrade 均非空实现；"
        "内嵌编号 SQL 副本与磁盘原文逐字节一致；"
        "alembic.ini 无默认连接串；src/ 无迁移工具链）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
