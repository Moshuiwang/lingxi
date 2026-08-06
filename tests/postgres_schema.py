"""真库用例共用的建库底座：按 glob 应用整条生产迁移链。

在这个模块之前，三个真库测试文件各自硬编码一份迁移文件名清单
（`test_galaxy_import_postgres.py`、`test_identity_postgres_records.py`，以及
`test_refresh_token_postgres.py` 的测试资产清单）。清单会过期，而过期的表现是
最坏的一种失败：**测试库里根本没有新迁移建的对象，用例照样全绿**。新加一条建了
触发器和清理函数的迁移，如果没人记得同步这三处，所有"触发器不让后移到期时间"
之类的断言都会在一个没有触发器的库上通过。

这里把"要应用哪些迁移"从人工维护的清单换成 `migrations/*.sql` 的 glob，把
"要先删掉哪些对象"从人工维护的清单换成对同一批文件的 `CREATE TABLE` /
`CREATE FUNCTION` 扫描。两者都跟着目录自动走，新增迁移不需要改任何测试文件。

`migrations/testing/` 的测试资产**不属于**这条链，也不会被这里删除：
`feishu_user_refresh_token`、`onboarding_progress` 等表由使用它们的用例自己管理。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIRECTORY = REPOSITORY_ROOT / "migrations"

# 顶层编号 SQL 是生产链；子目录（testing/、downgrades/）不参与。
# 与 scripts/ci/verify_repository.sh 的"空库整链前滚"取的是同一个 glob。
_PRODUCTION_GLOB = "*.sql"

_CREATE_TABLE = re.compile(r"^CREATE TABLE (?:IF NOT EXISTS )?(?:public\.)?([a-z_][a-z0-9_]*)", re.MULTILINE)
_CREATE_FUNCTION = re.compile(r"^CREATE (?:OR REPLACE )?FUNCTION (?:public\.)?([a-z_][a-z0-9_]*)", re.MULTILINE)


def production_migrations() -> tuple[Path, ...]:
    """生产迁移链，按文件名排序（编号即顺序）。"""

    paths = tuple(sorted(MIGRATIONS_DIRECTORY.glob(_PRODUCTION_GLOB)))
    if not paths:
        raise RuntimeError(f"{MIGRATIONS_DIRECTORY} 下没有找到任何生产迁移；建库底座失效")
    return paths


def production_objects() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """扫描生产链，返回 (表名, 函数名)。用于重建前的清场。"""

    tables: list[str] = []
    functions: list[str] = []
    for path in production_migrations():
        text = path.read_text(encoding="utf-8")
        tables.extend(_CREATE_TABLE.findall(text))
        functions.extend(_CREATE_FUNCTION.findall(text))
    # dict.fromkeys 去重且保序：同名对象在链里被重复创建时只删一次。
    return tuple(dict.fromkeys(tables)), tuple(dict.fromkeys(functions))


def drop_production_objects(cursor: Any) -> None:
    """删掉生产链建的全部表与函数；不碰测试资产的表。"""

    tables, functions = production_objects()
    for name in tables:
        cursor.execute(f'DROP TABLE IF EXISTS public."{name}" CASCADE')
    if functions:
        # 按目录里的实际签名删：函数可能有重载，也可能属主不是当前角色
        # （清理函数属主是 lingxi_retention_owner），按名字猜签名会漏。
        cursor.execute(
            "SELECT p.oid::regprocedure::text FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.proname = ANY(%s)",
            (list(functions),),
        )
        for (signature,) in cursor.fetchall():
            cursor.execute(f"DROP FUNCTION IF EXISTS {signature} CASCADE")


def apply_production_chain(cursor: Any) -> tuple[str, ...]:
    """按顺序应用整条生产链，返回实际应用的文件名。"""

    applied: list[str] = []
    for path in production_migrations():
        # 只传 SQL、不传参数：psycopg 在无参数时完全跳过占位符插值，
        # 迁移里 `format('… %I …')` 与 RAISE 的 `%` 才能原样送到服务端。
        cursor.execute(path.read_text(encoding="utf-8"))
        applied.append(path.name)
    return tuple(applied)


def rebuild_production_schema(cursor: Any) -> tuple[str, ...]:
    """清场 + 整链前滚。返回实际应用的迁移文件名，供用例断言"链没有被跳过"。"""

    drop_production_objects(cursor)
    return apply_production_chain(cursor)
