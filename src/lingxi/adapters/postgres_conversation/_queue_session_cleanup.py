"""Agent 会话 JSONL 物理清理队列（Issue #239 从 ``postgres_conversation.
PostgresTaskQueue`` 按读写边界拆分而来）。原班注释见类体内的小节说明。
"""

from __future__ import annotations

from typing import Sequence

from lingxi.adapters.postgres import connect

from ._dataclasses import SessionCleanupTask


class _SessionCleanupMixin:
    # -----------------------------------------------------------------
    # Agent 会话 JSONL 物理清理队列（Issue #153）
    #
    # 三类触发点（``/new``、空闲到点、停用/权限变化感知）只负责在各自的事务里往
    # ``agent_session_cleanup`` 排队（见 ``_Transaction._queue_session_cleanup``、
    # ``clear_delivered_content_for_user``、``sweep_idle_conversations``）；真正碰
    # 文件系统的物理删除由这里的两个方法服务——常驻 Worker 的周期性收口调用
    # ``claim_session_cleanups`` 认领一批，删完文件后调用
    # ``mark_session_cleanups_done`` 标记完成。
    # -----------------------------------------------------------------

    def claim_session_cleanups(self, *, limit: int = 20) -> list[SessionCleanupTask]:
        """认领至多 ``limit`` 条待清理请求。

        ``FOR UPDATE SKIP LOCKED`` 与任务队列的领取同一手法：即使未来出现第二个
        Worker 实例，两边也不会认领到同一行。``claimed_at`` 只是一个软标记（见迁移
        0061 头部注释），十分钟内已被认领但还没标记完成的行不会被重新捞出——这个
        窗口只用于"上一次认领的进程异常退出、物理删除没跑完"的兜底重试，不是强互斥。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    """
                    UPDATE agent_session_cleanup
                       SET claimed_at = now()
                     WHERE id IN (
                         SELECT id FROM agent_session_cleanup
                          WHERE done_at IS NULL
                            AND (claimed_at IS NULL OR claimed_at <= now() - INTERVAL '10 minutes')
                          ORDER BY queued_at
                          LIMIT %s
                          FOR UPDATE SKIP LOCKED
                     )
                    RETURNING id, user_id, agent_session_id, reason
                    """,
                    (limit,),
                )
                return [
                    SessionCleanupTask(
                        id=row[0], user_id=row[1], agent_session_id=row[2], reason=row[3]
                    )
                    for row in cursor.fetchall()
                ]

    def mark_session_cleanups_done(self, *, ids: Sequence[str]) -> None:
        """标记一批清理请求已完成（物理文件已删除或确认原本就不存在）。"""

        if not ids:
            return
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    "UPDATE agent_session_cleanup SET done_at = now() WHERE id = ANY(%s)",
                    (list(ids),),
                )
