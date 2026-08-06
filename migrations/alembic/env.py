"""Alembic 运行环境：解析连接串、建连接、跑在线迁移。

三条边界写在这里而不是文档里，因为它们必须**在错误发生时生效**，不是提醒：

1. **连接串只来自 ``LINGXI_MIGRATION_DSN``，缺失即失败。** ``alembic.ini`` 里没有
   ``sqlalchemy.url``，也不回落到 ``LINGXI_POSTGRES_DSN``。用独立变量而不是复用
   业务 DSN 有两个理由：迁移需要 DDL 权限、业务进程不需要，两者本就该是不同的
   数据库角色；门禁的等价性检查要在一次性 scratch 库上跑，环境里同时存在业务
   DSN 时不能有任何路径让它被顺手用上（断言 V-迁移-05）。
2. **URL scheme 归一到 ``postgresql+psycopg``。** 裸 ``postgresql://`` 会让
   SQLAlchemy 去找 psycopg2——本仓库只有 psycopg3，届时报的是"缺 psycopg2 模块"，
   与真实原因（scheme 没写驱动）差得很远。这里接受裸 scheme 并补齐驱动，
   其余驱动一律拒绝。
3. **不支持 offline（``--sql``）模式。** 基线 revision 通过 DBAPI 游标按原文执行
   整段 SQL（见该 revision 的说明），offline 模式没有游标，生成出来的脚本会
   **静默缺掉整个基线**——一个看起来成功的空脚本。这里直接拒绝。

本文件不 import ``lingxi``：迁移工具链不进运行时（断言 V-迁移-04），
``grep -rn "sqlalchemy\\|alembic" src/`` 必须为空。
"""

from __future__ import annotations

import os
from logging.config import fileConfig
from urllib.parse import urlsplit, urlunsplit

from alembic import context
from sqlalchemy import create_engine, pool

DSN_VARIABLE = "LINGXI_MIGRATION_DSN"
REQUIRED_SCHEME = "postgresql+psycopg"
# 允许写成裸 scheme（运维手写、复制自业务 DSN 都是这个形状），由本文件补齐驱动。
BARE_SCHEMES = ("postgresql", "postgres")

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def database_url() -> str:
    """从环境变量取连接串并归一化 scheme。

    错误信息里**不回显连接串本身**——它可能带口令，而门禁与部署日志都会留痕
    （断言 V-迁移-06）。只说变量名和 scheme。
    """

    raw = os.environ.get(DSN_VARIABLE, "").strip()
    if not raw:
        raise RuntimeError(
            f"缺少环境变量 {DSN_VARIABLE}，未连接任何数据库。"
            "迁移不使用 alembic.ini 里的默认连接串，也不回落到 LINGXI_POSTGRES_DSN。"
        )

    parts = urlsplit(raw)
    scheme = parts.scheme
    if scheme in BARE_SCHEMES:
        parts = parts._replace(scheme=REQUIRED_SCHEME)
    elif scheme != REQUIRED_SCHEME:
        raise RuntimeError(
            f"{DSN_VARIABLE} 的 scheme 是 {scheme!r}，本仓库只使用 psycopg3。"
            f"请写成 {REQUIRED_SCHEME}:// 或裸 postgresql://（由本文件补齐驱动）。"
        )
    if not parts.netloc and not parts.path:
        raise RuntimeError(f"{DSN_VARIABLE} 不是一个可用的数据库 URL。")
    return urlunsplit(parts)


def run_migrations_online() -> None:
    engine = create_engine(database_url(), poolclass=pool.NullPool)
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
