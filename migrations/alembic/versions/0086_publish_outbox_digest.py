"""发布意图的内容摘要列——摘要独立于九十天擦除（Trace #544 P-3）。

Revision ID: 0086_publish_outbox_digest
Revises: 0085_app_user_email_unique
Create Date: 2026-09-02

``publish_outbox.payload`` 过了九十天会被 ``redact_expired_payloads`` 擦成 ``'{}'``
（内容里有邮箱与姓名，到期必须消失）。可是"这一版权限和上一版一样吗"这个判断此前
**只能**读 ``payload``：擦过之后它读到的是空对象，于是一份内容完全没变的权限被判成
"变了"——重排一条发布意图，并且按「权限变化感知即清」的规则把该用户的 ``user_memory``
与全部会话已送达正文一并清空。用户侧看到的是：什么都没发生，记忆和历史答案却没了。

擦除是对的，判据不该依赖被擦的那份内容。这条迁移给发布意图加两列一次性算好的摘要：

- ``content_digest``：整行内容（去掉每轮都变的 ``updated_at``）的 SHA-256；回答
  "要不要排一条新的发布意图"。
- ``permissions_digest``：``permissions`` 单字段的 SHA-256；回答"这个人**实际可用
  权限**变了吗"——只有这一个答案能决定要不要清空记忆与已送达正文。

两列**不参与擦除**：摘要是单向的，说不出邮箱、姓名或权限内容本身，只能回答"和另一份
一不一样"。存量行按同一口径回填（见 ``_BACKFILL_SQL``，与
``core/permission/publish_row.py`` 的 ``content_digest``/``permissions_digest`` 逐字节
同一算法）；已经擦过的历史行没有内容可回填，保持 ``NULL``——读侧遇到 ``NULL`` 退回原来
的 ``payload`` 比较，行为与本迁移之前逐字相同。
"""

from __future__ import annotations

from alembic import op

revision: str = "0086_publish_outbox_digest"
down_revision: str | None = "0085_app_user_email_unique"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
ALTER TABLE publish_outbox
    ADD COLUMN content_digest TEXT,
    ADD COLUMN permissions_digest TEXT;
"""

#: 回填只覆盖**还没被擦除**的行（``payload ? 'permissions'`` 即"内容还在"，与
#: ``postgres_permission_publish.py`` 判定"快照说得出当时发布了什么"用的是同一条判据）。
#: 摘要文本的拼法必须与 Python 侧逐字节一致：字段顺序取自 ``PUBLISHED_FIELD_NAMES``
#: 去掉 ``updated_at``，每项写成 ``名字=值``，项间用换行连接（发布行的每个字段在
#: ``PublishRow.__post_init__`` 里已经拒绝了换行，因此换行是无歧义的分隔符）。
_BACKFILL_SQL = r"""
UPDATE publish_outbox
   SET content_digest = encode(
           sha256(convert_to(
               concat_ws(
                   E'\n',
                   'record_key=' || coalesce(payload ->> 'record_key', ''),
                   'email=' || coalesce(payload ->> 'email', ''),
                   'name=' || coalesce(payload ->> 'name', ''),
                   'permissions=' || coalesce(payload ->> 'permissions', ''),
                   'status=' || coalesce(payload ->> 'status', '')
               ),
               'UTF8'
           )),
           'hex'
       ),
       permissions_digest = encode(
           sha256(convert_to(coalesce(payload ->> 'permissions', ''), 'UTF8')),
           'hex'
       )
 WHERE payload ? 'permissions';
"""

_DOWNGRADE_SQL = r"""
ALTER TABLE publish_outbox
    DROP COLUMN IF EXISTS permissions_digest,
    DROP COLUMN IF EXISTS content_digest;
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与既有 revision 同型：直接使用 psycopg cursor 执行 DDL。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    bind = op.get_bind()
    _execute_verbatim(bind, _UPGRADE_SQL)
    _execute_verbatim(bind, _BACKFILL_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
