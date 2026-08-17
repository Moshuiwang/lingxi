"""未开通首聊「已交给开通编排」的账本列。

Revision ID: 0062_onboarding_dispatch_ledger
Revises: 0061_agent_session_cleanup
Create Date: 2026-08-17

Issue #65 轻审 P2-2。#65 的接线把「认领这条事件」放在 gateway 事务里
（``handled_as = 'auto_provisioning'``），把「把它交给开通编排」放在**事务提交之后**
——这个顺序本身是对的（不提交就触发会在回滚时重复触发），但它留下一个窗口：提交成功、
进程在调用编排之前崩溃或被停机拿走，事件行已经存在，飞书重投时被幂等去重挡下，编排
**永远不会被调用**，用户那边只剩一个"已收到"的表情。此前没有任何东西会发现这种行。

因此新增一列 ``onboarding_dispatched_at``：``NULL`` 表示"认领了，但还没确认交给编排"。
在途与已了结因此可判定，对账扫描（``core/conversation/onboarding_recovery.py``）据此
把这些孤儿重新交给编排一次。这不是第二套开通状态机——真正的开通结论仍然只由开通编排
自己的持久化决定，这一列只回答"gateway 这一侧的交接完成了没有"。

**部分索引**只覆盖"待交接"的行：扫描只查这一小撮，索引也只有这一小撮那么大；一旦交接
完成，行就从索引里消失，不会随九十天保留期内的全部入站事件一起膨胀。

**回填**把本 revision 之前的 ``auto_provisioning`` 行一律记成"已交接"。它们产生于账本
存在之前，交接要么早已发生、要么已经不可恢复；把几周前的一条首聊重新交给开通编排，
等于给一个早就走开的人凭空建环境和权限——比不恢复更糟。这不是写业务数据，是给新增的
簿记列一个不会误伤历史行的初值。

``downgrade()`` 真实可执行：列与索引都是本 revision 新增，直接删除；回填写进的是这一列
本身，随列一起消失，不存在"原值已不可知"的问题。
"""

from __future__ import annotations

from alembic import op

revision: str = "0062_onboarding_dispatch_ledger"
down_revision: str | None = "0061_agent_session_cleanup"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
ALTER TABLE inbound_event ADD COLUMN onboarding_dispatched_at TIMESTAMPTZ;

-- 历史行一律视为已交接，理由见文件头部。
UPDATE inbound_event
   SET onboarding_dispatched_at = received_at
 WHERE handled_as = 'auto_provisioning'
   AND onboarding_dispatched_at IS NULL;

CREATE INDEX inbound_event_onboarding_pending_idx
    ON inbound_event (received_at)
 WHERE handled_as = 'auto_provisioning' AND onboarding_dispatched_at IS NULL;
"""

_DOWNGRADE_SQL = r"""
DROP INDEX IF EXISTS inbound_event_onboarding_pending_idx;
ALTER TABLE inbound_event DROP COLUMN IF EXISTS onboarding_dispatched_at;
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与 0057/0058/0059/0060 同型：不走 ``op.execute()``，避免空参数集触发插值模式。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
