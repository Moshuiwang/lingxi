"""``PostgresTaskQueue`` 的共同基座（Issue #239 拆分）：构造参数与超时装配，以及
``finish``/``confirm_delivery`` 两条不同读写边界共用的会话覆盖排队小工具。

``PostgresTaskQueue`` 本体在 ``_task_queue.py`` 里以多重继承组合本文件与
``_queue_lifecycle.py``/``_queue_outbox.py``/``_queue_session_cleanup.py``/
``_queue_gateway_delivery.py`` 四个按读写边界拆开的 mixin；拆分只搬动方法的物理
位置，不改变任何方法体或调用顺序，各 mixin 之间仍通过同一个实例的 ``self`` 互相
调用（Python 方法解析顺序天然支持），行为与拆分前逐位相同。
"""

from __future__ import annotations

from typing import Any, Callable, TypeVar

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
        # 新建，不在本卡范围内改变行为（见 `_run_polling_operation` 的调用点）。
        self._reuse_polling_connection = reuse_polling_connection
        self._pooled_connection: Any = None

    def _run_polling_operation(self, operation: Callable[[Any], _T]) -> _T:
        """常驻轮询路径专用的连接获取与操作执行（S-H1-6；P2-1 复用连接首次
        失败重试一次；P1-A 执行阶段/提交阶段分离，Trace #373 H1 批终修复包②
        codex 外审）。

        ``operation`` 接收一条已经打开的数据库连接，自行开游标、执行 SQL、取
        结果并返回；本方法只管连接的获取/提交/失效检测与（复用命中时）失败
        重试，不关心 ``operation`` 内部具体做什么。之所以从"直接把连接
        ``yield`` 给调用方的 ``with`` 块"改成"接一个可调用对象"：复用连接首次
        失败要重建连接、**把同一次查询原样重跑一遍**，而 ``with`` 语句的块体
        在异常抛出后不可能被同一个上下文管理器再次执行——只有先把"这次要做
        什么"整体收进一个可调用对象，失败重试才有东西可以重新调用。

        默认（``reuse_polling_connection=False``）：每次调用都新建一条连接执行
        一次 ``operation``，成功提交、失败按原样上抛，不重试——这是目前所有
        其它调用点（``finish``/``reserve_dispatch`` 等业务处理方法）仍在用、
        本卡不改动的既有行为，与改动前逐字节一致。

        打开复用后，失败语义按**失败发生在哪个阶段**区分，而不再只按连接来源
        区分（**失败语义与改动前不再完全一致**）：

        - **`operation()` 执行阶段（提交之前）失败**，且是复用命中：视为"服务端
          在两次轮询之间悄悄掐断了这条连接、而数据库本身健康"这一种具体场景
          （例如空闲连接超时）——丢弃这条连接、重新建立一条、把 ``operation``
          **完整重新执行一次**。这一次重试（无论是重建连接本身失败，还是重试的
          ``operation`` 在提交前又失败）都原样向上抛出，不再重试第二次、不吞
          任何最终失败。
        - **`operation()` 执行阶段失败，且是新建连接**（缓存为空或已关闭，本次
          调用现建的连接）：失败直接原样上抛，不重试——一条刚建好就用不了的
          连接大概率是数据库真的不可达，重试没有意义，也要避免"一次调用悄悄
          建两条新连接才放弃"的行为膨胀。
        - **`commit()` 阶段失败，不论首次尝试还是重试之后**：一律丢弃连接、
          原样上抛，**绝不重试**。COMMIT 收到异常时，数据库端这次操作到底有没
          有真的提交是不确定的（例如网络在 COMMIT 回执途中断开、但服务端其实
          已经提交）——如果把这种"结果不确定"当成"结果是失败"去重放，会在
          COMMIT 其实已经成功的场景下把 ``operation`` 完整多执行一次一遍（例如
          常驻轮询里的 ``claim()``：第二次调用会再抢一批任务，而第一批因为
          COMMIT 其实已经成功、已经被标记 ``running``，此后不会再被任何一次
          ``claim()`` 认领，静默遗失，只能靠 stalled recovery 事后补救）。这一
          条不区分复用命中还是新建、也不区分是不是重试之后的提交——结果不确定
          必须如实向上抛出，不允许任何形式的自动重放。

        调用方因此看到的失败语义：新建连接在提交前失败与旧行为完全一致；复用
        连接在提交前失败从"必然让这一轮轮询失败一次"改善为"服务端悄悄断开这
        一种具体场景对调用方大多透明"；提交阶段失败（不论首次还是重试之后）
        一律原样上抛、不重试，避免把「结果不确定」误判为「可以安全重放」。
        """

        if not self._reuse_polling_connection:
            with connect(self._dsn, timeouts=self._timeouts) as connection:
                return operation(connection)

        connection = self._pooled_connection
        reused = connection is not None and not connection.closed
        if not reused:
            connection = connect(self._dsn, timeouts=self._timeouts)
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
            connection = connect(self._dsn, timeouts=self._timeouts)
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
