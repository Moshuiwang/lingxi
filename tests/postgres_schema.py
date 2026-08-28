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
import importlib.util
import os
import threading
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIRECTORY = REPOSITORY_ROOT / "migrations"
ALEMBIC_INI = REPOSITORY_ROOT / "alembic.ini"
VERSIONS_DIRECTORY = MIGRATIONS_DIRECTORY / "alembic" / "versions"
RETENTION_REVISION = VERSIONS_DIRECTORY / "0054_retention_cleanup.py"


def psycopg_available() -> bool:
    """``psycopg`` 驱动是否可以导入（只探测 spec，不实际 import、不发起连接）。

    真库用例此前只按 ``LINGXI_POSTGRES_DSN`` 门控：DSN 设了但环境缺 ``psycopg``
    时，``ensure_production_schema``/`` connect``（经
    ``lingxi.adapters.postgres.connect`` 里的惰性 ``import psycopg``）会在
    ``setUpClass`` 内直接抛 ``ModuleNotFoundError``，表现成一串 ``ERROR`` 而不是
    清晰的 skip，掩盖了"只是没装驱动"这个事实——与数据库本身是否健康无关
    （Issue #370 修 2）。真库测试类应把门控统一成 ``DSN 存在 且 本函数为真``
    两个条件都满足才跑，缺哪个就在跳过原因里点名哪个。

    与 ``scripts/ci/verify_repository.sh`` 「容器设了却没 DSN 直接失败」的半开
    守卫互补、不冲突：那条守卫防的是「有容器却漏配 DSN」的误配置假通过；这里防
    的是「有 DSN 却没装驱动」这另一半的假通过（ERROR 假装成红，实则是环境缺
    依赖）。
    """

    return importlib.util.find_spec("psycopg") is not None

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
    from lingxi.adapters.postgres import connect

    return connect(dsn, autocommit=True)


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


# 清行时**不碰**的表。
#
# 前两张是 `migrations/testing/` 的测试资产，不属于生产链，由使用它们的用例自己管理；
# `alembic_version` 建完库就被丢掉（见 `_drop_version_table`），正常不存在，列在这里
# 是为了万一它被留下时清行也别去动它。
#
# inbound_event 自 0057 起是生产表，**不在**这份名单里——留在排除名单里会让 gateway 的
# 真库用例在用例之间互相看见对方的事件行（幂等断言会假红/假绿）。
_PRESERVED_FROM_ROW_RESET = (
    "feishu_user_refresh_token",
    "onboarding_progress",
    "alembic_version",
)

_PRODUCTION_TABLES_SQL = (
    "SELECT tablename FROM pg_tables "
    " WHERE schemaname = 'public' "
    "   AND tablename <> ALL(%s) "
    " ORDER BY tablename"
)

# 外键的「子表 → 父表」边。清行按这个顺序从子往父删，父表那条 DELETE 执行时子表已经空了。
_FOREIGN_KEY_EDGES_SQL = (
    "SELECT child.relname, parent.relname "
    "  FROM pg_constraint k "
    "  JOIN pg_class child ON child.oid = k.conrelid "
    "  JOIN pg_class parent ON parent.oid = k.confrelid "
    "  JOIN pg_namespace n ON n.oid = child.relnamespace "
    " WHERE k.contype = 'f' AND n.nspname = 'public'"
)

# 装了 DELETE 触发器的表——**这些表只能 TRUNCATE，不能 DELETE**。
#
# 0054 给 `galaxy_import_batch` 与 `feishu_org_sync_run` 装了 BEFORE DELETE 触发器
# `lingxi_reject_premature_delete()`：未到期的行谁来删都拒绝（连保留清理专用角色也不例外，
# 那道防线故意按到期时间判定、不按角色判定）。用例刚插进去的批次 2160 小时后才到期，
# `DELETE` 会当场抛异常。`TRUNCATE` 不触发行级触发器，是这两张表唯一清得掉的手段。
#
# 判据从 `pg_trigger` 现查而不是写死表名：将来哪条 revision 给别的表加上 DELETE 触发器，
# 清行会自动把那张表改走 TRUNCATE，而不是在某个无关模块里冒出一条看不懂的异常。
# `tgtype & 8` 是 DELETE 位；行级与语句级一并算上（两者都会被 DELETE 触发、都不会被
# TRUNCATE 触发）。`tgenabled = 'D'` 是被显式禁用的触发器，不会触发，不必回避。
_DELETE_TRIGGER_TABLES_SQL = (
    "SELECT DISTINCT c.relname "
    "  FROM pg_trigger t "
    "  JOIN pg_class c ON c.oid = t.tgrelid "
    "  JOIN pg_namespace n ON n.oid = c.relnamespace "
    " WHERE NOT t.tgisinternal "
    "   AND t.tgenabled <> 'D' "
    "   AND n.nspname = 'public' "
    "   AND (t.tgtype & 8) <> 0"
)


def _fetch_production_tables(cursor: Any) -> tuple[str, ...]:
    cursor.execute(_PRODUCTION_TABLES_SQL, (list(_PRESERVED_FROM_ROW_RESET),))
    return tuple(row[0] for row in cursor.fetchall())


def production_tables(dsn: str) -> tuple[str, ...]:
    """当前库里由生产链建立的**全部**表，按表名排序。"""

    with _connect(dsn) as connection, connection.cursor() as cursor:
        return _fetch_production_tables(cursor)


def _fetch_tables_holding_rows(cursor: Any, tables: tuple[str, ...]) -> frozenset[str]:
    """当前**确实存有行**的表；一条语句、一次往返，结果是精确集合而不是估算。

    判据是逐表 `EXISTS (SELECT 1 FROM …)`，故意**不用** `pg_stat_user_tables.n_live_tup`
    或 `pg_class.relpages`——那两个由统计收集器与 VACUUM 滞后刷新，一张刚被写过的表在
    它们眼里可以仍然是「零行」。漏掉一张脏表的后果不是慢一点，是下一条用例看得见上一条
    写进去的数据：把一个性能问题换成一个正确性问题。

    空表上的 `EXISTS` 是一次零页的顺序扫描，既不取排他锁也不写 WAL；23 张表实测 0.9 毫秒。
    """

    if not tables:
        return frozenset()
    query = "\nUNION ALL\n".join(
        f'SELECT %s WHERE EXISTS (SELECT 1 FROM public."{name}")' for name in tables
    )
    cursor.execute(query, list(tables))
    return frozenset(row[0] for row in cursor.fetchall())


def production_tables_with_rows(dsn: str) -> tuple[str, ...]:
    """当前确实存有行的生产表，按表名排序。"""

    with _connect(dsn) as connection, connection.cursor() as cursor:
        tables = _fetch_production_tables(cursor)
        return tuple(sorted(_fetch_tables_holding_rows(cursor, tables)))


def _child_first_order(cursor: Any, tables: tuple[str, ...]) -> tuple[tuple[str, ...], frozenset[str]]:
    """把生产表排成「子表在前、父表在后」的顺序，并返回排不进去的那些表。

    返回的第二项是外键成环（或自环之外的循环引用）而无法定序的表。它们不参与 DELETE，
    交给 TRUNCATE 处理——宁可慢一点，也不要在清行里出现一条顺序错误的 DELETE。

    自引用（child 与 parent 是同一张表）直接忽略：`DELETE FROM t` 一次删光全表，
    外键在语句结束时检查，自引用必然满足。
    """

    cursor.execute(_FOREIGN_KEY_EDGES_SQL)
    known = set(tables)
    parents: dict[str, set[str]] = {name: set() for name in tables}
    predecessor_count: dict[str, int] = {name: 0 for name in tables}
    for child, parent in cursor.fetchall():
        if child == parent or child not in known or parent not in known:
            continue
        if parent in parents[child]:
            continue
        parents[child].add(parent)
        predecessor_count[parent] += 1

    ready = sorted(name for name in tables if predecessor_count[name] == 0)
    ordered: list[str] = []
    while ready:
        name = ready.pop(0)
        ordered.append(name)
        for parent in sorted(parents[name]):
            predecessor_count[parent] -= 1
            if predecessor_count[parent] == 0:
                ready.append(parent)
        ready.sort()
    return tuple(ordered), frozenset(known - set(ordered))


def reset_production_rows(dsn: str) -> tuple[str, ...]:
    """结构不动，只清掉**这一轮真的被写过**的那几张表；返回被清空的表名。

    Issue #234。上一版对**当前全部生产表**做一次 `TRUNCATE … CASCADE`，于是每条真库用例
    的固定开销与「库里一共有多少张表」成正比。本机实测（postgres:16-alpine 容器，与 CI 同款）：
    `TRUNCATE` 一张表约 90 毫秒，23 张表 2.05 秒——每条真库用例的 2.2 秒基本全在这里。
    2026-08-19 PR #232 门禁那 4 个
    `psycopg.errors.QueryCanceled: canceling statement due to statement timeout`
    也出在这条语句上：不是断言失败，是清场语句自己撞上了连接的 3 秒 statement_timeout。

    90 毫秒不是 fsync（`synchronous_commit = off` 实测无改善），是 `TRUNCATE` 要给表和它的
    每一个索引、TOAST 关系换一个新的 relfilenode——本库 23 张表背后是 91 个关系文件。
    这笔成本按**表的数量**收，与表里有没有数据无关：清一张从头到尾没被碰过的空表，
    和清一张写满的表一样贵。

    现在分三步，都在同一条连接上：

    1. 一条 `EXISTS` 语句精确问出「哪几张表真的有行」（0.9 毫秒，见
       `_fetch_tables_holding_rows`）。一张都没有就直接返回，什么都不做。
    2. 其中**装了 DELETE 触发器**的表（见 `_DELETE_TRIGGER_TABLES_SQL`）用
       `TRUNCATE … CASCADE` 清——那些表 `DELETE` 不动。当前只有
       `galaxy_import_batch` 与 `feishu_org_sync_run` 两张，绝大多数用例根本碰不到。
    3. 其余有行的表按外键「子→父」顺序一条 `DELETE FROM` 清掉。`DELETE` 只取
       ROW EXCLUSIVE 锁、不换 relfilenode、不重建索引文件；实测清空 23 张表 1.6 毫秒。

    **成本为什么不再与表总数成正比。** 昂贵的那部分——排他锁 + relfilenode 重建 + 索引与
    TOAST 文件重建——现在只发生在「这一轮真的写过、而且 DELETE 不动」的表上，典型用例是
    零张。剩下与表总数有关的只有第 1 步那条 `EXISTS` 语句：它对每张表做一次零页顺序扫描，
    不取排他锁、不写 WAL，实测 23 张表 0.9 毫秒（每张 0.04 毫秒），而上一版是每张 90 毫秒。
    新增一张表的边际成本从 90 毫秒降到 0.04 毫秒，量级差 2000 倍。

    **隔离强度没有变。** 被清掉的是「有行的表」这个**精确**集合（`EXISTS` 判定，不是估算），
    清完之后全库一行不剩，与上一版逐字相同。`tests/test_postgres_isolation_contract.py`
    把这条钉成契约，并做过「去掉本函数即变红」的变异复验。

    **为什么不用事务回滚隔离。** 不可行：被测代码自己按 DSN 建连接、自己开事务并提交
    （`src/lingxi/adapters/postgres.py:103-128` 的工厂，调用方形如 `postgres_identity.py:91`
    的 `with connect(self._dsn) as connection`），测试侧包不住一个它们不使用的事务；
    `tests/test_worker_process.py` 更是起真实子进程连同一个库，跨进程连事务都不共享。
    """

    ensure_production_schema(dsn)
    with _connect(dsn) as connection, connection.cursor() as cursor:
        tables = _fetch_production_tables(cursor)
        holding_rows = _fetch_tables_holding_rows(cursor, tables)
        if not holding_rows:
            return ()

        cursor.execute(_DELETE_TRIGGER_TABLES_SQL)
        delete_blocked = {row[0] for row in cursor.fetchall()}
        ordered, unordered = _child_first_order(cursor, tables)

        must_truncate = sorted(holding_rows & (delete_blocked | unordered))
        if must_truncate:
            quoted = ", ".join(f'public."{name}"' for name in must_truncate)
            # CASCADE 是必须的：外键引用它们的表哪怕是空的，PostgreSQL 也拒绝单独截断
            # 被引用方。CASCADE 带上的是外键图的局部闭包，与库里一共有多少张表无关。
            cursor.execute(f"TRUNCATE {quoted} CASCADE")

        # TRUNCATE … CASCADE 可能已经顺带清空了下面某几张表，那时这条 DELETE 是空转。
        to_delete = [name for name in ordered if name in holding_rows and name not in must_truncate]
        if to_delete:
            cursor.execute("; ".join(f'DELETE FROM public."{name}"' for name in to_delete))

        return tuple(sorted(holding_rows))
