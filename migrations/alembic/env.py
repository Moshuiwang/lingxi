"""Alembic 运行环境：建连接、跑在线迁移。

本文件只做**接线**。连接串的解析与校验在同目录的 `migration_dsn.py`，那是一段不依赖
alembic 的纯逻辑，因此它的用例在没装 migrate extra 的机器上也能跑（见该模块说明）。

这里只剩两条边界：

1. **连接串只来自 ``LINGXI_MIGRATION_DSN``。** ``alembic.ini`` 里没有 ``sqlalchemy.url``，
   也不回落到 ``LINGXI_POSTGRES_DSN``——迁移需要 DDL 权限、业务进程不需要，两者本就
   该是不同的数据库角色；门禁的等价性检查要在一次性 scratch 库上跑，环境里同时存在
   业务 DSN 时不能有任何路径让它被顺手用上（断言 V-迁移-05）。
2. **不支持 offline（``--sql``）模式。** 基线 revision 通过 DBAPI 游标按原文执行整段
   SQL（见该 revision 的说明），offline 模式没有游标，生成出来的脚本会**静默缺掉整个
   基线**——一个看起来成功的空脚本。这里直接拒绝。

本文件不 import ``lingxi``：迁移工具链不进运行时（断言 V-迁移-04），
``grep -rn "sqlalchemy\\|alembic" src/`` 必须为空。
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

# `alembic.ini` 的 prepend_sys_path 把本目录放进 sys.path，所以这个 import 成立。
from migration_dsn import DSN_VARIABLE, migration_connect_args, normalize_database_url

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def run_migrations_online() -> None:
    url = normalize_database_url(os.environ.get(DSN_VARIABLE))
    engine = create_engine(url, poolclass=pool.NullPool, connect_args=migration_connect_args())
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                # 每个 revision 各自一个事务：整链中途失败时，库停在**某个 revision
                # 边界**上而不是半个 revision 里，alembic_version 说的就是实际状态。
                # PostgreSQL 的 DDL 在事务内，所以单个 revision 仍然是全有或全无——
                # 旧库未 stamp 直接 upgrade 时，基线整体回滚，对象与数据不变
                # （断言 V-迁移-02 的否定面）。
                transaction_per_migration=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise RuntimeError(
        "本仓库的迁移不支持 offline（--sql）模式：基线 revision 按原文执行整段 SQL，"
        "offline 模式会生成一个缺掉基线的空脚本。请连库执行 alembic upgrade。"
    )

run_migrations_online()
