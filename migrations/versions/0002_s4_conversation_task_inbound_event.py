"""S4 前半（Issue #57）：conversation / task / inbound_event 三张表。

**本文件当前不可执行，是给 #53 缝链用的半成品。** 仓库里还没有 Alembic 脚手架
（alembic.ini、env.py 都由 #53 交付，`alembic` 也尚未进 pyproject），因此：

- ``down_revision`` 是**占位符**，等 #53 的单条基线 revision 合并后由变基方填上真值；
- 真正被执行、被门禁验证的 DDL 是同目录上一级的
  ``migrations/013_create_conversation_task_inbound_event.sql``。
  `scripts/ci/verify_repository.sh` 的「生产迁移链顺序前滚」会实际跑它，
  `tests/test_gateway_postgres.py` 也从同一个文件建表——**DDL 只有那一份**。

本文件刻意不复制 DDL：两份 DDL 各自漂移是迁移目录最常见的事故，migrations/README.md
把编号 SQL 定为「正式表结构的唯一来源」正是为此。这里只做一件事——把那份 SQL 交给
Alembic 执行。

> 缝链时请注意：#53 的任务是把既有 006–012 基线化成一条 revision。**013 是在 #53
> 开工之后新增的**，两种收口都可以，但必须二选一，不能都做：
>   (a) #53 的基线只覆盖 006–012，本 revision 作为它的下一条挂上去；
>   (b) #53 的基线连 013 一起覆盖，则删除本文件。
> 两个都留会让 013 的 DDL 被执行两次。
"""

from __future__ import annotations

import pathlib

# Alembic 的标识约定。revision 用固定字符串而不是随机 hex，方便人工缝链时对照。
revision = "0002_s4_conversation_task_inbound_event"
# TODO(#53 缝链)：改成 #53 那条基线 revision 的标识。留 None 会让本条被当成第二个
# 根 revision，alembic 会报「多个 head」——是响亮的失败，不是静默错挂。
down_revision = None  # 占位符，等 #53 合并
branch_labels = None
depends_on = None

_SQL_FILE = pathlib.Path(__file__).resolve().parents[1] / "013_create_conversation_task_inbound_event.sql"

# 本批只交付前滚。回滚 DDL 留给 #53 统一约定（数据库设计「十一、迁移约定」要求
# 破坏性变更拆两次发布），这里不臆造一个没被任何用例跑过的 downgrade。
_DROP_SQL = """
DROP TRIGGER IF EXISTS inbound_event_expiry ON inbound_event;
DROP FUNCTION IF EXISTS inbound_event_fix_expiry();
DROP TRIGGER IF EXISTS task_freeze ON task;
DROP FUNCTION IF EXISTS task_freeze_invariants();
DROP TABLE IF EXISTS task;
DROP TABLE IF EXISTS inbound_event;
DROP TABLE IF EXISTS conversation;
"""


def upgrade() -> None:
    from alembic import op

    op.execute(_SQL_FILE.read_text(encoding="utf-8"))


def downgrade() -> None:
    from alembic import op

    op.execute(_DROP_SQL)
