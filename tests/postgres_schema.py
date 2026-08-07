"""真库用例共用的建库底座：整条 alembic 链建库。

#53 之后**编号 SQL 已冻结**，表结构的权威来源是 `migrations/alembic/versions/`
的 revision 链。这个模块因此不再自己拼迁移清单，而是直接 `alembic upgrade head`
——建库路径与生产、与 `scripts/ci/check_migration_chain.sh` 完全同源。

它替换掉的是三个真库测试文件各自硬编码的一份迁移文件名清单（#54 验收清单 H-02）。
清单会过期，而过期的表现是最坏的一种失败：**测试库里根本没有新迁移建的对象，
用例照样全绿**。新加一条建了触发器和清理函数的 revision，如果没人记得同步这三处，
所有"触发器不让后移到期时间"之类的断言都会在一个没有触发器的库上通过。
现在没有清单可过期——多一条 revision 就自动多建一条。

`migrations/testing/` 的测试资产**不属于**这条链，也不会被这里删除：
`feishu_user_refresh_token`、`onboarding_progress` 等表由使用它们的用例自己管理。
"""

from __future__ import annotations

import ast
import os
import threading
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIRECTORY = REPOSITORY_ROOT / "migrations"
ALEMBIC_INI = REPOSITORY_ROOT / "alembic.ini"
VERSIONS_DIRECTORY = MIGRATIONS_DIRECTORY / "alembic" / "versions"
RETENTION_REVISION = VERSIONS_DIRECTORY / "0054_retention_cleanup.py"

# 建库只做一次：alembic 整链前滚每次约几百毫秒，而 IdentityPostgresTestCase 是
# **每个用例**都要重置的。重复建库会把真库这一段的耗时抬到门禁超时的量级。
# 后续重置只清行不重建结构；确实改了结构的用例自己调 force_rebuild_schema。
_BUILD_LOCK = threading.Lock()
_BUILT_FOR_DSN: str | None = None
_APPLIED_HEAD: str = ""


def applied_head() -> str:
    """本进程建库时前滚到的 head revision id。

    **它是脚本目录算出来的 head，不是从库里读回来的观察值**——建完库之后版本表就被
    丢弃了（见 `_drop_version_table`）。用它断言"链跑到了哪一条"是够的（值来自
    真正执行过的那次 `upgrade`），但它证明不了"库里现在确实是这个版本"，
    需要后者时应当直接查库内对象（内审 P3-3）。
    """

    return _APPLIED_HEAD


def revision_sql() -> tuple[str, str]:
    """取出 #54 revision 内联的 upgrade / downgrade DDL 原文。

    DDL 自 #53 起只存在于 revision 文件里，用例要重放它就得从那里取，
    不能再去读 `migrations/*.sql`——那些编号文件已冻结且不含本切片。

    用 `ast` 静态取常量，**不 import 那个模块**：revision 文件顶部有
    `from alembic import op`，import 它就等于要求每台跑单测的机器都装 alembic。
    这个函数在模块导入期就会被调用（用例文件的模块级常量），于是没装 alembic 的
    裸机上整个测试**发现阶段**就崩，而不是干净地跳过几条真库用例。
    静态取常量既不需要 alembic，也不会执行 revision 里的任何代码。
    """

    tree = ast.parse(RETENTION_REVISION.read_text(encoding="utf-8"), filename=str(RETENTION_REVISION))
    found: dict[str, str] = {}
    for node in tree.body:
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        if (
            isinstance(target, ast.Name)
            and node.value is not None
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            found[target.id] = node.value.value
    missing = [name for name in ("_UPGRADE_SQL", "_DOWNGRADE_SQL") if name not in found]
    if missing:
        raise RuntimeError(f"{RETENTION_REVISION.name} 里找不到 {'、'.join(missing)}")
    return found["_UPGRADE_SQL"], found["_DOWNGRADE_SQL"]


def _connect(dsn: str) -> Any:
    import psycopg

    return psycopg.connect(dsn, autocommit=True)


def drop_production_objects(dsn: str) -> None:
    """清掉 public 下由生产链建立的全部表与函数，外加 alembic 版本表。

    保留 `migrations/testing/` 那几张测试资产表：它们不属于生产链，
    删了会打断 `test_refresh_token_postgres` 等用例。

    **`inbound_event` 自 `0057_gateway_tables` 起是生产表，不再在保留名单里。**
    它此前只存在于测试资产 `migrations/testing/001` 中，所以被当成测试资产保留；
    现在生产链自己建它，继续保留会让残留的测试资产版本挡住
    `alembic upgrade head` 的 `CREATE TABLE`（同名对象已存在）。测试资产那一份由
    `tests/test_identity_postgres.sh` 自己先 DROP 再建，不依赖这里保留它。
    """

    preserved = ("feishu_user_refresh_token", "onboarding_progress")
    with _connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> ALL(%s)",
            (list(preserved),),
        )
        for (table,) in cursor.fetchall():
            cursor.execute(f'DROP TABLE IF EXISTS public."{table}" CASCADE')
        # 测试资产的函数（record_authorized_identity）随其表一起保留。
        cursor.execute(
            "SELECT p.oid::regprocedure::text FROM pg_proc p "
            "  JOIN pg_namespace n ON n.oid = p.pronamespace "
            " WHERE n.nspname = 'public' AND p.proname <> 'record_authorized_identity'"
        )
        for (signature,) in cursor.fetchall():
            cursor.execute(f"DROP FUNCTION IF EXISTS {signature} CASCADE")


def alembic_upgrade_head(dsn: str) -> str:
    """在进程内跑 `alembic upgrade head`，返回 head revision id。

    进程内而不是起子进程：子进程每次要重新 import alembic 与 SQLAlchemy，
    在"每个用例重置一次"的调用频率下这笔开销比迁移本身还大。
    """

    import logging

    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(ALEMBIC_INI))
    previous = os.environ.get("LINGXI_MIGRATION_DSN")
    os.environ["LINGXI_MIGRATION_DSN"] = dsn

    # alembic 的 env.py 会 `fileConfig(...)` 读 alembic.ini 的日志配置，而
    # `logging.config.fileConfig` 默认带 `disable_existing_loggers=True`：它会把
    # **当时已经存在的**所有 logger 就地禁用。在生产里这没问题（迁移是独立进程），
    # 在测试进程里则是跨用例污染——建完库之后，`lingxi.apps.scheduler` 与
    # `lingxi.adapters.retention` 上的 assertLogs 全部拿不到记录，13 条与本次改动
    # 毫无关系的用例会一起变红，而失败信息完全不指向这里。
    # 因此进出各存取一次 disabled 位。
    manager = logging.root.manager
    disabled_before = {
        name: logger.disabled
        for name, logger in manager.loggerDict.items()
        if isinstance(logger, logging.Logger)
    }
    try:
        command.upgrade(config, "head")
    finally:
        if previous is None:
            os.environ.pop("LINGXI_MIGRATION_DSN", None)
        else:
            os.environ["LINGXI_MIGRATION_DSN"] = previous
        for name, was_disabled in disabled_before.items():
            logger = manager.loggerDict.get(name)
            if isinstance(logger, logging.Logger):
                logger.disabled = was_disabled
        # 只还原 disabled 位，不还原 root handler / level：alembic 的日志配置会往 root
        # 上挂自己的 handler 并留在那里。已知无害——测试断言的是具名 logger 上的记录，
        # root 多一个 handler 只影响控制台多打几行（内审 P3-3 登记）。
    return ScriptDirectory.from_config(config).get_current_head() or ""


def _drop_version_table(dsn: str) -> None:
    """建完结构后把 `alembic_version` 去掉。

    业务测试库不是一个"被 alembic 管理的库"，它每轮都重建。留着版本表会有两个坏处：
    一是 `scripts/ci/check_migration_chain.sh` 有一条明确的卫生断言——业务测试库里
    出现 `alembic_version` 即判定"门禁的迁移步骤碰了不该碰的库"，那条断言是对的，
    不该为了测试方便把它放宽；二是留着会让人以为这个库可以被 `alembic upgrade`
    增量维护，而它实际上随时会被 drop 重建。

    "链确实跑到了 head"这个事实由 `applied_head()` 记录，不依赖版本表。
    """

    with _connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS public.alembic_version")


def _build(dsn: str) -> str:
    global _BUILT_FOR_DSN, _APPLIED_HEAD
    drop_production_objects(dsn)
    head = alembic_upgrade_head(dsn)
    _drop_version_table(dsn)
    _BUILT_FOR_DSN = dsn
    _APPLIED_HEAD = head
    return head


def force_rebuild_schema(dsn: str) -> str:
    """清场 + 整链前滚。改动过结构的用例收尾时调它。"""

    with _BUILD_LOCK:
        return _build(dsn)


def ensure_production_schema(dsn: str) -> str:
    """保证结构已就位；同一个进程内只真正建一次。"""

    with _BUILD_LOCK:
        if _BUILT_FOR_DSN == dsn:
            return _APPLIED_HEAD
        return _build(dsn)


def production_tables(dsn: str) -> tuple[str, ...]:
    """当前库里由生产链建立的表；顺序不定，删行时用 TRUNCATE ... CASCADE。"""

    with _connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT tablename FROM pg_tables "
            " WHERE schemaname = 'public' "
            "   AND tablename <> ALL(%s) "
            " ORDER BY tablename",
            # inbound_event 自 0057 起是生产表，必须参与清行——留在排除名单里会让
            # gateway 的真库用例在用例之间互相看见对方的事件行（幂等断言会假红/假绿）。
            (["feishu_user_refresh_token", "onboarding_progress", "alembic_version"],),
        )
        return tuple(row[0] for row in cursor.fetchall())


def reset_production_rows(dsn: str) -> None:
    """结构不动，只把生产表清空。比重建结构快一个数量级。"""

    ensure_production_schema(dsn)
    tables = production_tables(dsn)
    if not tables:
        return
    quoted = ", ".join(f'public."{name}"' for name in tables)
    with _connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(f"TRUNCATE {quoted} CASCADE")
