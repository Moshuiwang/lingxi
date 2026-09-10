"""`admin_action_followup.depends_on_id` 补索引：清场 DELETE 从平方级降回线性。

Revision ID: 0093_admin_followup_depends_idx
Revises: 0092_innertest_membership
Create Date: 2026-09-10

[Issue #706](https://github.com/Moshuiwang/lingxi/issues/706)。`depends_on_id` 是
`admin_action_followup` 上的自引用外键（`0091` 定义：``depends_on_id TEXT
REFERENCES admin_action_followup(id) ON DELETE SET NULL``），此前没有任何索引以它
打头。删除该表任意一行时，PostgreSQL 的外键触发器都要确认"有没有别的行的
`depends_on_id` 指向这一行、需要一并置空"——没有索引，这一步是对全表的顺序扫描；
删 N 行就是 N 次全表扫描，整条 `DELETE` 呈平方级。测试脚手架
`tests/test_admin_followup_postgres.py::test_hundred_rows_and_capacity_rejection_
roll_back_all` 会把该表堆到 10000 行，紧随其后那一次 `setUp` 清场
（`tests/postgres_schema.py:432`）就撞上这条平方级路径；改前改后的实测耗时见本单
PR 说明与 Trace 记录，不在这里重复。

## 索引方式：判定"算不算已有大表"（数据库设计.md 十一.2）

**判定：不算，用普通 `CREATE INDEX`，不用 `CONCURRENTLY`。**

`CONCURRENTLY` 存在的意义是避免长时间持锁挡住一张**已经有并发读写**的大表；本仓库
迄今全部 `CREATE INDEX` 都不用它，理由写在数据库设计文档：新建表随迁移创建的索引
不存在持锁问题，全仓尚无命中"已有大表追加索引"的场景。`admin_action_followup`
本身是 `0091`（2026-09-09）新增的表，**尚未上生产**（#700 的既定裁定：0091/0092 这一批
还在 2.4.2 才推进生产），目前只在 `biai-stage` 因 2.4.0 候选验收跑过——也就是说，
在本 revision 真正合入生产的那一刻，生产库里这张表还是空的、没有任何并发写入者。
这与全仓既有先例（`0085` 给已有真实数据的 `app_user` 加唯一索引时，同样因为生产
预检该维度分组为 0 而选用普通语句）是同一个判据方向：**决定要不要 `CONCURRENTLY`
的是这张表在目标环境的数据量与并发写入压力，不是"表是否已经建过"**。因此本次直接
用普通 `CREATE INDEX`：语法更简单、能包在 alembic 默认的单事务迁移里（`env.py` 的
``transaction_per_migration=True``），而 `CONCURRENTLY` 恰恰不能在事务块内执行、
与这个默认冲突，一旦需要它就必须先跟迁移框架的事务策略打架——本次判定不需要，
不碰这个口子。

## 回滚

`downgrade()` 只 `DROP INDEX`：索引不持有业务内容，删除不损失任何数据，也不受表内
是否已有行影响——真库前滚 / 回退 / 再前滚三步已实测通过（含表内有数据时的回退）。
"""

from __future__ import annotations

from alembic import op

revision: str = "0093_admin_followup_depends_idx"
down_revision: str | None = "0092_innertest_membership"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
CREATE INDEX admin_followup_depends ON admin_action_followup(depends_on_id);
"""

_DOWNGRADE_SQL = r"""
DROP INDEX IF EXISTS admin_followup_depends;
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与 `0085`/`0089` 同型：直接用 psycopg cursor 执行 DDL，不走 `op.execute()`。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
