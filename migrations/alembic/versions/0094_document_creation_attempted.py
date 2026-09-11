"""``task_document_delivery_request`` 新增可空 ``creation_attempted_at`` 列：
在发起一次外部建档/建表调用之前先落一个"已经打算发起"的时间戳。

Revision ID: 0094_document_creation_attempted
Revises: 0093_admin_followup_depends_idx

## 要封住的窗口

外部建档/建表调用（``create_document``/``create_document_with_markdown``/
``create_spreadsheet``）**成功之后**才轮到 ``mark_document_created`` 提交
``document_id`` 检查点——两次动作之间有一段进程可以整个消失的缝隙。消费进程在
这条缝隙里被打断时，``document_id`` 仍是 ``NULL``；此前的回收逻辑只看
``status``/``updated_at`` 就会把这一行判定为"还没真的开始"，退回 ``pending``，
下一次认领又会从头调用一次外部建档/建表——外部已经真实创建的那一份不会被
发现，用户会收到两份内容相同的文档/表格。

新增列记录的是"即将发起外部调用"这个意图本身，在**发起调用之前**单独提交
（与 ``document_id`` 同一检查点纪律）。``document_id`` 仍为 ``NULL`` 但这一列
已非 ``NULL``，代表"外部是否已经创建成功"本地无法判断——回收逻辑据此把这一行
转入人工核对，不再当作"还没开始"重新发起调用。

## 为什么可空、不带默认值

绝大多数行不会经历这条缝隙（一次调用要么在同一次进程存活期内跑完全部步骤，
要么根本没跑到发起调用这一步）；``NULL`` 就是"没有需要额外核对的未决意图"，
新增列不改变任何既有行的可解释性。

## 回滚

``downgrade()`` 直接 ``DROP COLUMN``：这一列只是本地判定用的时间戳，不含用户
资料，删除不损失业务内容，也不受表内是否已有行影响。
"""

from __future__ import annotations

from alembic import op

revision: str = "0094_document_creation_attempted"
down_revision: str | None = "0093_admin_followup_depends_idx"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
ALTER TABLE task_document_delivery_request
    ADD COLUMN creation_attempted_at TIMESTAMPTZ;
"""

_DOWNGRADE_SQL = r"""
ALTER TABLE task_document_delivery_request DROP COLUMN IF EXISTS creation_attempted_at;
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与既有 revision 同型：不走 ``op.execute()``，避免空参数集触发插值模式。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
