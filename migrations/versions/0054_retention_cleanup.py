"""#54 S9：expires_at 保留清理机制（触发器 + 受限清理函数 + 数据库角色）。

DDL 正文不写在本文件里，而是执行 ``migrations/013_create_retention_cleanup.sql``
与 ``migrations/downgrades/013_drop_retention_cleanup.sql``。这样做的理由是**同一份
DDL 只有一个来源**：

* 生产前滚链的门禁（``scripts/ci/verify_repository.sh`` 的"空库整链前滚"）按
  ``migrations/*.sql`` 逐个应用，Alembic 落地前它是唯一真正执行这条迁移的地方；
* 真库用例通过 ``tests/postgres_schema.py`` 应用同一批文件；
* 本 revision 落地后由 Alembic 应用同一批文件。

三条路径执行的是同一段文本，不会出现"迁移文件改了、Alembic 没改"的分叉。

Revision ID: 0054_retention_cleanup
Revises: 20260806_baseline（#53 的单条基线 revision；合并顺序 #53 → #54）
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0054_retention_cleanup"
# #53 的单条基线 revision（基线兼 head）。合并顺序 #53 → #54；
# 若 #53 最终落地的 id 与此不同，缝链只需改这一行。
down_revision = "20260806_baseline"
branch_labels = None
depends_on = None

_UPGRADE_SQL = "migrations/013_create_retention_cleanup.sql"
_DOWNGRADE_SQL = "migrations/downgrades/013_drop_retention_cleanup.sql"


def _repository_root() -> Path:
    """向上找到仓库根，而不是按本文件位置写死相对层级。

    #53 若把 versions/ 放到别的目录下，写死的 ``parents[N]`` 会静默指向错误的
    路径；按标志文件向上找则跟着一起移动。
    """

    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file() and (candidate / "migrations").is_dir():
            return candidate
    raise RuntimeError("找不到仓库根（向上没有同时含 pyproject.toml 与 migrations/ 的目录）")


def _run(relative_path: str) -> None:
    sql = (_repository_root() / relative_path).read_text(encoding="utf-8")
    # 直接用底层驱动游标执行，**不能**走 op.execute / text() / exec_driver_sql：
    #
    #   * text() 会把 `:=` 与 `$$` 里的内容按绑定参数解析；
    #   * exec_driver_sql 即使不传参数，也会向 psycopg 传一个空参数集，于是
    #     psycopg 进入占位符插值模式，撞上 DO 块里 `format('CREATE ROLE %I', …)`
    #     的 `%I` 直接报 “only '%s', '%b', '%t' are allowed as placeholders”。
    #
    # 只传 SQL、不传参数时 psycopg 完全跳过插值，`%I` / `%` 原样送到服务端。
    # 这条路径已在 PostgreSQL 16 上用 `alembic upgrade head` / `downgrade` 实跑验证。
    connection = op.get_bind()
    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _run(_UPGRADE_SQL)


def downgrade() -> None:
    _run(_DOWNGRADE_SQL)
