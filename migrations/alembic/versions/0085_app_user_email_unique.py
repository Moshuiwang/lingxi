"""``app_user`` 规范化邮箱的部分唯一索引（rc25 S-2a，对抗审查 X-1）。

Revision ID: 0085_app_user_email_unique
Revises: 0084_management_card_state_cas
Create Date: 2026-09-02

正式表 ``user_company_permissions`` 的行键 ``record_key`` 是**规范化邮箱**
（``core/permission/publish_row.py``：去首尾空白 + 转小写），而开通链在签发问数
MCP 令牌之前会按邮箱查正式表存量行、单行且可解密就把那份密文**采纳**成本用户的
令牌。``app_user.email`` 此前没有任何唯一性约束，于是花名册里两名**不同员工**
共用一个邮箱时，后开通的人会拿到先开通那个人的令牌，并用同一个 ``record_key``
把正式表那一行的权限覆写成自己的范围——两个人以同一个身份查数，可见范围由最后
一次发布者决定。

本 revision 把"一个规范化邮箱至多绑定一个 ``app_user``"变成**结构性**保证：

- 索引表达式 ``lower(btrim(email))`` 与 ``normalize_email`` 逐字同口径；口径不同
  的索引挡不住真正的碰撞（``A@x.com`` 与 ``a@x.com `` 在业务上是同一行键）。
- ``WHERE email IS NOT NULL AND btrim(email) <> ''`` 把"没有邮箱"排除在唯一性之外：
  建档不以邮箱为前提（基线 ``app_user`` 的注释：工号与邮箱可空、不参与身份字段的
  全有全无约束），若把空值也纳入，第二个没有邮箱的用户就建不了档。
  条件用 ``btrim(email) <> ''`` 而不是 ``email <> ''``：纯空白的邮箱在
  ``normalize_email`` 之后同样是空，应用层不会拿它当行键，索引也不该拿它当键——
  否则两个"只填了空格"的用户会互相碰撞在一个空字符串上。

应用层的同名闸在 ``core/identity/onboarding_guards.reject_email_bound_to_another_
user``：它在写入之前就读到冲突，给出有名字的原因码与审计。两者是纵深关系，不是
二选一——只有索引时诊断信息丢失，只有应用层判定时并发两条链仍可能各自读到"无冲突"。

**前置数据条件已核实**：2026-09-02 生产库预检 ``app_user`` 同邮箱分组为 0
（全表 1 行），因此本 revision 可以直接建索引，不需要先清数据。若在某个环境上
建索引失败，那正是"该环境已经存在共用邮箱的两个人"的响亮证据，必须先人工处置
（决定谁保留该邮箱）再重跑，**不得**改成非唯一索引绕过。

``downgrade()`` 删除该索引，真正逆转 upgrade（无数据损失：索引不持有业务内容）。
"""

from __future__ import annotations

from alembic import op

revision: str = "0085_app_user_email_unique"
down_revision: str | None = "0084_management_card_state_cas"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
CREATE UNIQUE INDEX app_user_normalized_email_key
    ON app_user (lower(btrim(email)))
    WHERE email IS NOT NULL AND btrim(email) <> '';
"""

_DOWNGRADE_SQL = r"""
DROP INDEX IF EXISTS app_user_normalized_email_key;
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与既有 revision 同型：直接使用 psycopg cursor 执行 DDL。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
