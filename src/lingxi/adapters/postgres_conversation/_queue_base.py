"""``PostgresTaskQueue`` 的共同基座。

构造参数与超时装配，以及 ``finish``/``confirm_delivery`` 两条不同读写边界共用
的会话覆盖排队小工具。``PostgresTaskQueue`` 本体在 ``_task_queue.py`` 里以多重继承组合本文件与
``_queue_lifecycle.py``/``_queue_outbox.py``/``_queue_session_cleanup.py``/
``_queue_gateway_delivery.py`` 四个按读写边界拆开的 mixin；各 mixin 之间仍通过
同一个实例的 ``self`` 互相调用（Python 方法解析顺序天然支持）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.config.content import ContentCatalog, default_content_catalog
from lingxi.core.ids import new_id

_T = TypeVar("_T")


class _TaskQueueBase:
    def __init__(
        self,
        dsn: str,
        *,
        timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS,
        content_catalog: ContentCatalog | None = None,
        reuse_polling_connection: bool = False,
    ) -> None:
        """记下构造参数：DSN、超时、内容目录与是否复用轮询连接。"""
        self._dsn = dsn
        self._timeouts = timeouts
        # 系统代为收口路径（见下 `_write_system_terminal`）渲染用户可见文案要用到
        # 目录；默认与 `apps.worker.service.WorkerService` 相同的
        # `default_content_catalog()`（lru_cache，全进程唯一实例）。
        self._content_catalog = content_catalog or default_content_catalog()
        # 常驻轮询路径（gateway 投递发现查询、worker claim()）每 tick 新建一条
        # 物理连接是数据库空闲基线的主要来源；默认关闭，只由各自常驻进程装配
        # PostgresTaskQueue 时显式打开，其它调用方保持逐次新建。
        self._reuse_polling_connection = reuse_polling_connection
        self._pooled_connection: Any = None

    def _run_polling_operation(self, operation: Callable[[Any], _T]) -> _T:
        """常驻轮询路径专用的连接获取与操作执行；失败重试仅在打开复用时生效。

        默认（不复用）每次新建连接执行一次 ``operation``，失败原样上抛，不重试。
        打开复用后，失败按**发生在哪个阶段**区分：执行阶段（提交前）失败且是
        复用命中——丢弃连接、重建一条、完整重试一次，重试仍失败原样上抛；
        执行阶段失败但连接是本次新建——直接原样上抛；``commit()`` 阶段失败
        （无论首次还是重试后）——是否已提交不确定，一律丢弃连接、原样上抛、
        绝不重试（重放可能让已提交的操作被执行两次）。
        """
        if not self._reuse_polling_connection:
            with connect(self._dsn, timeouts=self._timeouts) as connection:
                return operation(connection)

        connection = self._pooled_connection
        reused = connection is not None and not connection.closed
        if not reused:
            connection = connect(self._dsn, timeouts=self._timeouts, dedicated=True)
            self._pooled_connection = connection

        try:
            result = operation(connection)
        except Exception:
            self._discard_pooled_connection(connection)
            if not reused:
                raise
            # 复用连接首次失败，且失败发生在 operation 执行阶段（提交之前）：
            # 重建一条连接，把同一次操作完整重试一次。重建本身失败（下面这行
            # `connect()`），或重试的 `operation` 在提交前又失败，都不再兜底，
            # 原样向上抛出——只有"提交前失败"才走这条重试分支；重试之后的
            # `commit()` 与首次尝试共用下面同一段"提交阶段绝不重试"的逻辑。
            connection = connect(self._dsn, timeouts=self._timeouts, dedicated=True)
            self._pooled_connection = connection
            try:
                result = operation(connection)
            except Exception:
                self._discard_pooled_connection(connection)
                raise

        try:
            connection.commit()
        except Exception:
            # COMMIT 阶段异常：结果不确定，绝不重放——丢弃连接后原样上抛。见
            # 上面文档字符串「commit() 阶段失败」一条。
            self._discard_pooled_connection(connection)
            raise
        return result

    def _discard_pooled_connection(self, connection: Any) -> None:
        """丢弃一条复用中的连接：清空缓存，尽力关闭底层 socket。

        关闭本身失败不覆盖调用方即将看到的原始异常——这里只负责"不再让下一次
        轮询复用一条已知坏掉的连接"，不负责诊断关闭失败的原因。
        """
        if self._pooled_connection is connection:
            self._pooled_connection = None
        try:
            connection.close()
        except Exception:  # 关闭一条已经坏掉的连接失败，不需要覆盖原始异常
            pass

    @staticmethod
    def _queue_overwritten_session(
        cursor: Any,
        *,
        user_id: str,
        previous_session_id: str | None,
        new_session_id: str | None,
    ) -> None:
        """``agent_session_id`` 被新值覆盖时，把旧值排队做物理清理。

        ``finish()``/``confirm_delivery()`` 都用
        ``agent_session_id = COALESCE(%s, agent_session_id)`` 写回：新值非空且
        与旧值不同时，旧的 ``agent_session_id`` 从此不会再被既有三处触发点
        （``/new``、空闲到点扫描、停用/权限变化）排队——两个可达场景是话题闲置
        未满两小时就被新一轮任务覆盖，或 ``--resume`` 返回了新的 session id。
        与调用方共用同一个 cursor/事务：旧值排队和 ``conversation`` 的写回要么
        一起提交、要么一起回滚。
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
