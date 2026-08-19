"""``PostgresTaskQueue`` 的共同基座（Issue #239 拆分）：构造参数与超时装配，以及
``finish``/``confirm_delivery`` 两条不同读写边界共用的会话覆盖排队小工具。

``PostgresTaskQueue`` 本体在 ``_task_queue.py`` 里以多重继承组合本文件与
``_queue_lifecycle.py``/``_queue_outbox.py``/``_queue_session_cleanup.py``/
``_queue_gateway_delivery.py`` 四个按读写边界拆开的 mixin；拆分只搬动方法的物理
位置，不改变任何方法体或调用顺序，各 mixin 之间仍通过同一个实例的 ``self`` 互相
调用（Python 方法解析顺序天然支持），行为与拆分前逐位相同。
"""

from __future__ import annotations

from typing import Any

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts
from lingxi.config.content import ContentCatalog, default_content_catalog
from lingxi.core.ids import new_id


class _TaskQueueBase:
    def __init__(
        self,
        dsn: str,
        *,
        timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS,
        content_catalog: ContentCatalog | None = None,
    ) -> None:
        self._dsn = dsn
        self._timeouts = timeouts
        # 系统代为收口路径（见下 `_write_system_terminal`）渲染用户可见文案要用到
        # 目录；默认与 `apps.worker.service.WorkerService` 相同的
        # `default_content_catalog()`（lru_cache，全进程唯一实例），调用方可注入
        # 假目录做测试，不需要为此单独构造一个 worker 实体。
        self._content_catalog = content_catalog or default_content_catalog()

    @staticmethod
    def _queue_overwritten_session(
        cursor: Any,
        *,
        user_id: str,
        previous_session_id: str | None,
        new_session_id: str | None,
    ) -> None:
        """``agent_session_id`` 被新值覆盖时，把旧值排队做物理清理（PR #173
        独立复核 P2-4）。

        ``finish()``/``confirm_delivery()`` 都用
        ``agent_session_id = COALESCE(%s, agent_session_id)`` 写回：新值非空且
        与旧值不同时，旧的 ``agent_session_id`` 从此不会再被任何触发点排队——
        既有三处触发点（``/new``、空闲到点扫描、停用/权限变化）都不覆盖这条
        路径。两个可达场景：话题闲置未满两小时就被新一轮任务覆盖（scheduler
        的扫描还没轮到）；或真实 Claude Code CLI 的 ``--resume`` 返回了一个
        新的 session id，每次续用都会留下一个永远不会被清理的旧 JSONL。

        与调用方共用同一个 cursor/事务：旧值排队和 ``conversation`` 的写回
        要么一起提交、要么一起回滚，不产生"任务已经收口但清理请求丢了"的
        中间态。
        """

        if (
            new_session_id is None
            or not previous_session_id
            or previous_session_id == new_session_id
        ):
            return
        cursor.execute(
            """
            INSERT INTO agent_session_cleanup (id, user_id, agent_session_id, reason)
            VALUES (%s, %s, %s, 'session_overwritten')
            ON CONFLICT (agent_session_id) DO NOTHING
            """,
            (new_id("asc"), user_id, previous_session_id),
        )
