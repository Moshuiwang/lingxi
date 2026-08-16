"""Agent 会话 JSONL 物理清理队列。

Revision ID: 0061_agent_session_cleanup
Revises: 0060_gateway_delivery_dispatch
Create Date: 2026-08-14

Issue #153。#151 审核已经登记（P2-3、迁移 0059 头部与验收矩阵 V-投递-05/06 脚注）：
会话保留阶段的清理合同覆盖 Outbox 正文、Agent 会话 JSONL、在线临时文件与进程恢复
材料四类位置，但 #151/#152 只实现了 Outbox（``task_delivery_event.content``）
这一个位置——原因是清理触发点（``/new``、空闲到点、停用/权限变化感知）需要一个
可被调用的**常驻** Worker 进程去执行物理文件删除，而常驻 Worker 由本 Story 第一次
建立。

**为什么新增一张队列表，而不是让触发点直接同步删文件**：三个触发点分别落在
Gateway（``/new``，同一事务里）、scheduler（空闲到点扫描）和身份/权限模块（停用、
权限变化感知，均「待建立」）三个不同的进程里，而物理文件只有 Worker 才能碰到——
它是唯一挂载了 ``/var/lib/lingxi/users`` 持久卷、且以常驻形态存在的进程（架构设计
5.3 节）。因此触发点只负责在**同一个数据库事务**里登记"这个 ``agent_session_id``
需要物理清理"，真正的文件删除延后到 Worker 的周期性收口（``_housekeep``）里执行——
这与 Outbox 正文清理的"事件触发 + 到点扫描"两类触发路径完全对称，只是执行者从
"当前连接直接 UPDATE"换成了"排队，由唯一有文件访问权的进程消费"。

``reason`` 只记录触发类别（不含用户正文），供审计与故障排查用；不构成第二套业务
状态——真正的"要不要 resume 这个会话"仍然只由 ``conversation.agent_session_id``
和两小时规则的时间戳比较决定（架构设计 5.2 节），这张表不参与那个判定，只负责
"这个已经不会再被 resume 的 session id，它的物理文件该删了"。

``claimed_at`` 是一个宽松的软领取标记，不是强互斥锁：Worker 目前只有一个常驻实例
（本 Story 的部署契约），真正的并发安全来自 ``FOR UPDATE SKIP LOCKED``（见
``PostgresTaskQueue.claim_session_cleanups``），``claimed_at`` 只用于"领取后异常
退出、下一轮允许重新领取"的超时兜底，不追求跨进程强一致。

``downgrade()`` 真实可执行：整张表本 revision 新增，直接 ``DROP TABLE``。
"""

from __future__ import annotations

from alembic import op

revision: str = "0061_agent_session_cleanup"
down_revision: str | None = "0060_gateway_delivery_dispatch"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
CREATE TABLE agent_session_cleanup (
    id                 TEXT        PRIMARY KEY,           -- ULID, asc_*
    user_id            TEXT        NOT NULL REFERENCES app_user(id),
    agent_session_id   TEXT        NOT NULL,
    -- 只记触发类别，不含会话正文或用户可识别信息之外的内容。
    -- `session_overwritten`（PR #173 独立复核 P2-4）：`task.finish()` /
    -- `confirm_delivery()` 的 `agent_session_id = COALESCE(%s, agent_session_id)`
    -- 写回把旧值换成新值时，旧值单独排队——三个既有触发点（/new、空闲到点、
    -- 停用/权限变化）都不覆盖这条路径：闲置未满两小时就被新一轮任务覆盖、或
    -- CLI --resume 返回了新的 session id，旧的 JSONL 此前会永久留在磁盘上。
    reason             TEXT        NOT NULL
        CHECK (reason IN (
            'new_command', 'idle_timeout', 'user_cleared', 'session_overwritten'
        )),
    queued_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at         TIMESTAMPTZ,
    done_at            TIMESTAMPTZ
);

-- 同一个 agent_session_id 只需要排队一次：三个单条触发点（/new、user_cleared、
-- 空闲到点扫描本身）插入时都用 `ON CONFLICT (agent_session_id) DO NOTHING`
-- （见 postgres_conversation.py 的 `_queue_session_cleanup`）；空闲到点批量扫描
-- 那条查询另外用 NOT EXISTS 先把候选集收窄到"尚未排过队"的会话（PR #173 独立
-- 复核 P2-5），避免每 60 秒把整个历史候选集重新捞出来做一遍纯浪费的插入尝试。
-- 这条唯一索引则是数据库层面的最终保险，防止并发触发（例如空闲到点扫描恰好与
-- /new 撞在同一时刻）产生重复的清理请求。
CREATE UNIQUE INDEX agent_session_cleanup_session_uniq
    ON agent_session_cleanup (agent_session_id);

-- 待处理队列的领取查询只看这一类行；局部索引让它不随历史已完成行增长变慢。
CREATE INDEX agent_session_cleanup_pending_idx
    ON agent_session_cleanup (queued_at)
    WHERE done_at IS NULL;
"""

_DOWNGRADE_SQL = r"""
DROP TABLE IF EXISTS agent_session_cleanup;
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与 0057/0058/0059/0060 同型：不走 ``op.execute()``，避免空参数集触发插值模式。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
