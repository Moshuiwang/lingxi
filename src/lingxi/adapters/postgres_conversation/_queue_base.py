"""``PostgresTaskQueue`` 的共同基座（Issue #239 拆分）：构造参数与超时装配，以及
``finish``/``confirm_delivery`` 两条不同读写边界共用的会话覆盖排队小工具。

``PostgresTaskQueue`` 本体在 ``_task_queue.py`` 里以多重继承组合本文件与
``_queue_lifecycle.py``/``_queue_outbox.py``/``_queue_session_cleanup.py``/
``_queue_gateway_delivery.py`` 四个按读写边界拆开的 mixin；拆分只搬动方法的物理
位置，不改变任何方法体或调用顺序，各 mixin 之间仍通过同一个实例的 ``self`` 互相
调用（Python 方法解析顺序天然支持），行为与拆分前逐位相同。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.config.content import ContentCatalog, default_content_catalog
from lingxi.core.ids import new_id


class _TaskQueueBase:
    def __init__(
        self,
        dsn: str,
        *,
        timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS,
        content_catalog: ContentCatalog | None = None,
        reuse_polling_connection: bool = False,
    ) -> None:
        self._dsn = dsn
        self._timeouts = timeouts
        # 系统代为收口路径（见下 `_write_system_terminal`）渲染用户可见文案要用到
        # 目录；默认与 `apps.worker.service.WorkerService` 相同的
        # `default_content_catalog()`（lru_cache，全进程唯一实例），调用方可注入
        # 假目录做测试，不需要为此单独构造一个 worker 实体。
        self._content_catalog = content_catalog or default_content_catalog()
        # S-H1-6（#359 根因取证方案第 2 条）：常驻轮询路径（gateway 投递发现查询、
        # worker `claim()`）每 tick 经 `connect()` 新建一条物理连接是数据库空闲
        # 基线约 18.7 提交事务/秒的主要来源。默认关闭——只有 `assemble_delivery_
        # consumer`（gateway）与 `apps/worker/cli.py`（worker）在装配各自常驻
        # 进程的 `PostgresTaskQueue` 时显式打开；scheduler 等其它调用方保持逐次
        # 新建，不在本卡范围内改变行为（见 `_connect_for_polling` 的调用点）。
        self._reuse_polling_connection = reuse_polling_connection
        self._pooled_connection: Any = None

    @contextmanager
    def _connect_for_polling(self) -> Iterator[Any]:
        """常驻轮询路径专用的连接获取（S-H1-6）。

        默认（``reuse_polling_connection=False``）与既有行为逐字节一致：每次
        调用新建一条连接，``with`` 块结束时提交（或异常时回滚）并关闭——这是
        目前所有其它调用点（``finish``/``reserve_dispatch`` 等业务处理方法）
        仍在用、本卡不改动的路径。

        打开时改为持有并复用同一条连接：命中即直接复用，不重新握手；连接缺失
        或已关闭时才新建。**失效检测不靠猜——任何一次使用（含最终的
        ``commit()``）抛出异常都视为这条连接不可再信任**：立即丢弃并关闭它，
        让下一次调用重新建立，同时把原始异常原样向上抛出（不吞、不在本方法内
        部重试、不把"复用旧连接侥幸成功"伪装成健康）。调用方因此看到的失败语义
        与关闭复用之前完全一致，只是失败路径不再需要一次新的 TCP 握手才能确认
        连接确实坏了。
        """

        if not self._reuse_polling_connection:
            with connect(self._dsn, timeouts=self._timeouts) as connection:
                yield connection
            return

        connection = self._pooled_connection
        if connection is None or connection.closed:
            connection = connect(self._dsn, timeouts=self._timeouts)
            self._pooled_connection = connection
        try:
            yield connection
            connection.commit()
        except Exception:
            self._discard_pooled_connection(connection)
            raise

    def _discard_pooled_connection(self, connection: Any) -> None:
        """丢弃一条复用中的连接：清空缓存，尽力关闭底层 socket。

        关闭本身失败不覆盖调用方即将看到的原始异常——这里只负责"不再让下一次
        轮询复用一条已知坏掉的连接"，不负责诊断关闭失败的原因。
        """

        if self._pooled_connection is connection:
            self._pooled_connection = None
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - 关闭一条已经坏掉的连接失败，不需要覆盖原始异常
            pass

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
