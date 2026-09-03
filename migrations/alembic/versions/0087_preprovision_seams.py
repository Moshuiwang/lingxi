"""预开通（Issue #541）在 ``app_user`` 上要的三列：停摆租约起点 + 首聊补一句的一次性标志。

Revision ID: 0087_preprovision_seams
Revises: 0086_publish_outbox_digest
Create Date: 2026-09-03

预开通（rc25 S-8a）是「系统触发」的开通：名单里的人在与 BI Plus 发生任何对话之前就被
开通完，因此**没有 ``inbound_event`` 行**。这一条差异让两件原本成立的事在这条路径上
失效，各需要一列事实把它补回来。

## ``provisioning_started_at``——停摆兜底的租约起点

``StalledProvisioningDuty`` 的候选查询（``adapters/postgres_stalled_provisioning.py``）
用 ``inbound_event.onboarding_dispatched_at`` 当"这一次认领的代次"，也就是租约起点。
系统触发没有那一行 ⇒ 停在 ``provisioning``/``mcp_syncing`` 的人**结构上**永远捞不到，
永久卡住，而他一旦发消息 pipeline 只会照发「正在完成…请稍候」——与 Issue #282 修复前
的形状逐字相同。

本列由 ``PostgresAppUserStore.advance_provisioning_state(to='provisioning')`` 在**推进
到分水岭的同一条 UPDATE 里**写上，两条路径都写（首聊路径写了也用不上，它有认领代次
可用），因此它是"这一次开通尝试是什么时候进入中途格的"这个事实的唯一来源。

**刻意不用 ``updated_at`` 兜底**：那一列会被任何无关更新刷新（权限版本推进、账号状态
变化……），拿它当租约起点会让租约**永远不到期**——一个真正卡住的人因此永远等不到收口，
比没有兜底更糟，因为查询看起来是在工作的。

## ``preprovision_notice_armed_at`` / ``preprovision_notice_sent_at``——首聊补一句

产品负责人裁定：预开通期间**静默**，名单内用户第一次发消息时才补一句「你的 BI Plus
已经开通……」。形状照 ``task.delivery_expired_notice_sent_at``（`V-投递-06` 后半句）的
既有一次性提示：一列记"挂起了"，一列记"提示过了"。

**两列而不是一个布尔**：一个布尔分不清"从来没挂起过"和"挂起过、已经提示掉了"，于是
同一份名单重跑会把已经提示过的人重新挂起，用户第二次收到同一句话——而"同名单重跑零
变化"是本卡的验收硬条件。两列之后，挂起写入自带 ``armed_at IS NULL`` 守卫，重跑是一次
0 行的空写。

两列都不进任何对外发布内容，也不参与九十天擦除：它们只是"这句话说过没有"。
"""

from __future__ import annotations

from alembic import op

revision: str = "0087_preprovision_seams"
down_revision: str | None = "0086_publish_outbox_digest"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
ALTER TABLE app_user
    ADD COLUMN provisioning_started_at TIMESTAMPTZ,
    ADD COLUMN preprovision_notice_armed_at TIMESTAMPTZ,
    ADD COLUMN preprovision_notice_sent_at TIMESTAMPTZ;
"""

#: 停摆候选查询会对每一个停在中途格的用户读这一列。中途格用户本来就少（``app_user_
#: provisioning_state_pending_idx`` 已经覆盖了"不是 active"这一半），因此不另建索引。

_DOWNGRADE_SQL = r"""
ALTER TABLE app_user
    DROP COLUMN IF EXISTS preprovision_notice_sent_at,
    DROP COLUMN IF EXISTS preprovision_notice_armed_at,
    DROP COLUMN IF EXISTS provisioning_started_at;
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与既有 revision 同型：直接使用 psycopg cursor 执行 DDL。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
