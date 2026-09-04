"""任务队列领取与收口（Issue #239 从 ``postgres_conversation.PostgresTaskQueue``
按读写边界拆分而来）：worker 侧的 ``claim``/``finish``/心跳续期，以及 scheduler 侧
的心跳超时回收（``reclaim_stale``）与 queued 超时/灰度版本不可用收口
（``_fail_queued`` 及其两个入口）。

``_write_system_terminal`` 虽然要写投递 outbox 事件，但它只被本文件内的
``reclaim_stale``/``_fail_queued`` 调用（系统代为收口从未被真实 worker 执行完的
任务），因此仍归在这条"任务队列领取与收口"边界里；它通过 ``self`` 调用
``_queue_outbox.py`` 里的 ``_find_by_idempotency_key``/``_insert_new_event`` ——
拆分只搬动方法的物理位置，两个 mixin 组合进同一个 ``PostgresTaskQueue`` 后仍是
同一个实例上的方法查找，调用顺序和行为与拆分前逐位相同。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from lingxi.adapters.postgres import connect
from lingxi.core.delivery.ports import DeliveryEventType, TerminalKind

from ._dataclasses import ClaimedTask, TaskContext, TerminalTask

# 三条"系统代为收口"路径共用的哨兵 worker_id（Issue #178）：``reclaim_stale``
# 的心跳超时终态分支、``reclaim_queued``、``fail_unavailable_versions`` 收口的
# 任务从未被一个仍然存活的 worker 正常执行完，没有真实持有者可以填进
# ``task_delivery_event.worker_id``。该列只是 ``TEXT NOT NULL``，没有指向任何
# "worker" 表的外键（迁移 0059），写一个固定、可在诊断查询里一眼认出的哨兵值
# 即可，不影响 Gateway 消费循环（它不按 worker_id 过滤）。
_SYSTEM_DELIVERY_WORKER_ID = "system"

# error_kind → 用户可见文案键：全部复用 config/content.toml 已经登记、已过产品
# 审校的既有文案（Issue #178 明确要求"走 config/content 既有文案机制"，不发明
# 新文案）。`worker.queued_timeout`/`worker.version_unavailable` 在本次修复前
# 从未被任何生产代码引用过——它们是 #151 outbox 改造时被遗留的"文案已经写好、
# 但没有任何路径真正投递"的孤儿键，本次修复是它们第一次被真正渲染发出。
_SYSTEM_TERMINAL_CONTENT_KEYS: dict[str, str] = {
    "queued_timeout": "worker.queued_timeout",
    "worker_version_unavailable": "worker.version_unavailable",
    "retry_exhausted": "worker.running_timeout",
    "side_effect_uncertain": "worker.side_effect_uncertain",
}


class _TaskLifecycleMixin:
    def claim(
        self, *, worker_id: str, target_worker_version: str, limit: int = 1
    ) -> list[ClaimedTask]:
        """领取任务。

        两条关键约束都在这一句 SQL 里：

        - ``FOR UPDATE SKIP LOCKED``：两个 worker 并发领取时每个任务恰好被一个领到，
          既不重复也不互相阻塞（`V-队列-04`）。
        - ``target_worker_version = %s``：声明版本的 worker 只领得到匹配的任务，
          不匹配的保持 ``queued`` 不被误改状态（`V-灰度-02`）。去掉这个条件，
          canary 任务就会被 stable worker 领走。

        **本方法不写 ``target_worker_version``。** 它在入队时已固化，重试与回收都不得
        改写（`V-灰度-01`）；迁移 013 的触发器兜底，这里写它会直接抛异常。
        """

        # S-H1-6（#359 根因取证方案第 2 条）：这是 worker 主循环每个 poll_interval
        # 都会执行一次的发现查询——即使空转也照样命中，因此走
        # `_run_polling_operation`（默认逐字节等价于原来的 `connect(...)`，只有
        # 装配方显式打开复用时才改为持有常驻连接；打开时复用连接首次失败会重建
        # 重试一次，见该方法文档，P2-1）。

        def _claim(connection: Any) -> list[ClaimedTask]:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE task SET status = 'running',
                                    worker_id = %s,
                                    started_at = now(),
                                    heartbeat_at = now(),
                                    attempts = attempts + 1
                     WHERE id IN (
                         SELECT id FROM task
                          WHERE status = 'queued'
                            AND scheduled_at <= now()
                            AND target_worker_version = %s
                          ORDER BY scheduled_at, id
                            FOR UPDATE SKIP LOCKED
                          LIMIT %s
                     )
                    RETURNING id, conversation_id, user_id, prompt,
                              resumed_session, target_worker_version, attempts,
                              reply_to_message_id, stop_requested, side_effect_state
                    """,
                    (worker_id, target_worker_version, limit),
                )
                return [
                    ClaimedTask(
                        task_id=row[0],
                        conversation_id=row[1],
                        user_id=row[2],
                        prompt=row[3],
                        resumed_session=row[4],
                        target_worker_version=row[5],
                        attempts=row[6],
                        reply_to_message_id=row[7],
                        stop_requested=row[8],
                        side_effect_state=row[9],
                    )
                    for row in cursor.fetchall()
                ]

        return self._run_polling_operation(_claim)

    def finish(
        self,
        *,
        task_id: str,
        conversation_id: str,
        status: str,
        worker_id: str,
        agent_session_id: str | None = None,
        error_kind: str | None = None,
        side_effect_state: str | None = None,
    ) -> bool:
        """结束任务并释放话题。只有**这一代**的执行者能收口。

        两层条件，缺一不可：

        - 任务更新带 ``worker_id = %s AND status = 'running'``。只判「是不是这个任务
          占的话题」是不够的——任务被心跳超时回收、重排、由另一个 worker 重新领取
          之后，``task_id`` 并没有变，僵尸 worker 照样匹配得上。独立复查在真库上实测
          出这条：僵尸 w1 的收口会把话题释放掉，而 w2 仍在执行该任务，于是下一条
          消息可以再次抢占并与 w2 并行——同话题串行被打破。
        - 释放带 ``running_task_id = %s``：**只有持有者能释放**，防止清掉别人的占用。

        ``last_task_ended_at`` 在这里落，它是两小时规则的唯一依据。
        """

        if status not in {"succeeded", "failed", "stopped"}:
            raise ValueError("任务只能以 succeeded、failed 或 stopped 收口")
        if side_effect_state not in {None, "none", "possible"}:
            raise ValueError("side_effect_state 只能是 none 或 possible")
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    """
                    UPDATE task
                       SET status = %s,
                           ended_at = now(),
                           error_kind = COALESCE(%s, error_kind),
                           side_effect_state = COALESCE(%s, side_effect_state)
                     WHERE id = %s AND worker_id = %s AND status = 'running'
                    """,
                    (status, error_kind, side_effect_state, task_id, worker_id),
                )
                if cursor.rowcount != 1:
                    # 不是这一代执行者（或任务早已结束）：什么都不改，也不释放话题。
                    return False
                cursor.execute(
                    """
                    WITH target AS (
                        SELECT id, user_id, agent_session_id AS previous_session_id
                          FROM conversation
                         WHERE id = %s AND running_task_id = %s
                         FOR UPDATE
                    )
                    UPDATE conversation AS c
                       SET running_task_id = NULL,
                           last_task_ended_at = now(),
                           agent_session_id = COALESCE(%s, target.previous_session_id)
                      FROM target
                     WHERE c.id = target.id
                    RETURNING target.user_id, target.previous_session_id
                    """,
                    (conversation_id, task_id, agent_session_id),
                )
                row = cursor.fetchone()
                if row is None:
                    return False
                self._queue_overwritten_session(
                    cursor,
                    user_id=row[0],
                    previous_session_id=row[1],
                    new_session_id=agent_session_id,
                )
                return True

    def heartbeat(self, *, task_id: str, worker_id: str) -> bool:
        """只有当前 worker 这一代能续心跳；僵尸 worker 的续期会返回 False。"""

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE task SET heartbeat_at = now()
                 WHERE id = %s AND worker_id = %s AND status = 'running'
                """,
                (task_id, worker_id),
            )
            return cursor.rowcount == 1

    def stop_requested(self, *, task_id: str, worker_id: str) -> bool:
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT stop_requested FROM task
                 WHERE id = %s AND worker_id = %s AND status = 'running'
                """,
                (task_id, worker_id),
            )
            row = cursor.fetchone()
            return bool(row and row[0])

    def mark_side_effect(self, *, task_id: str, worker_id: str) -> bool:
        """在调用外部工具、卡片或文本发送前先落保守状态。"""

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE task SET side_effect_state = 'possible'
                 WHERE id = %s AND worker_id = %s AND status = 'running'
                """,
                (task_id, worker_id),
            )
            return cursor.rowcount == 1

    def task_context(self, *, task_id: str, worker_id: str) -> TaskContext | None:
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT t.id, t.conversation_id, t.user_id, t.prompt,
                       t.resumed_session, t.target_worker_version, t.attempts,
                       t.reply_to_message_id, t.stop_requested, t.side_effect_state,
                       c.feishu_chat_id, c.feishu_thread_id, c.agent_session_id
                  FROM task AS t
                  JOIN conversation AS c ON c.id = t.conversation_id
                 WHERE t.id = %s AND t.worker_id = %s AND t.status = 'running'
                """,
                (task_id, worker_id),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return TaskContext(
                task_id=row[0],
                conversation_id=row[1],
                user_id=row[2],
                prompt=row[3],
                resumed_session=row[4],
                target_worker_version=row[5],
                attempts=row[6],
                reply_to_message_id=row[7],
                stop_requested=row[8],
                side_effect_state=row[9],
                chat_id=row[10],
                thread_id=row[11],
                agent_session_id=row[12],
            )

    def reclaim_stale(
        self,
        *,
        older_than: timedelta,
        max_auto_retries: int = 1,
        _with_outcomes: bool = False,
    ) -> list[str] | tuple[list[str], list[TerminalTask]]:
        """回收心跳超时任务；可安全重试的第一轮回到 queued，其余进入失败终态。

        同样**不碰 ``target_worker_version``**：任务被回收重排后，用户仍然进入他当初
        被分到的那个版本（`V-灰度-01` 的回收路径）。
        """

        if isinstance(max_auto_retries, bool) or max_auto_retries < 0:
            raise ValueError("max_auto_retries 必须是非负整数")

        requeued: list[str] = []
        terminal: list[TerminalTask] = []
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    """
                    SELECT id, conversation_id, attempts, side_effect_state
                      FROM task
                     WHERE status = 'running'
                       AND heartbeat_at < now() - %s::interval
                     ORDER BY heartbeat_at, id
                     FOR UPDATE SKIP LOCKED
                    """,
                    (older_than,),
                )
                for task_id, conversation_id, attempts, side_effect_state in cursor.fetchall():
                    safe_retry = side_effect_state == "none" and attempts <= max_auto_retries
                    if safe_retry:
                        cursor.execute(
                            """
                            UPDATE task SET status = 'queued', worker_id = NULL,
                                            started_at = NULL, heartbeat_at = NULL
                             WHERE id = %s AND status = 'running'
                            """,
                            (task_id,),
                        )
                        requeued.append(task_id)
                        continue
                    error_kind = "side_effect_uncertain" if side_effect_state != "none" else "retry_exhausted"
                    # Issue #178（红线）：这个任务从未被一个仍然存活的 worker 正常
                    # 收口——旧实现在这里直接把 task 记 failed 并释放话题，跳过了
                    # outbox，用户永远收不到终态。改为写出与真实 worker 完全同型的
                    # 投递事件序列并转入 awaiting_delivery，见
                    # `_write_system_terminal` 的说明。
                    if not self._write_system_terminal(
                        cursor,
                        task_id=task_id,
                        error_kind=error_kind,
                        from_status="running",
                    ):
                        continue  # pragma: no cover - 见该方法文档的竞态防御说明
                    terminal.append(
                        TerminalTask(
                            task_id=task_id,
                            conversation_id=conversation_id,
                            status="awaiting_delivery",
                            error_kind=error_kind,
                        )
                    )
        if _with_outcomes:
            return requeued, terminal
        return requeued

    def reclaim_stale_with_outcomes(
        self, *, older_than: timedelta, max_auto_retries: int = 1
    ) -> tuple[list[str], list[TerminalTask]]:
        result = self.reclaim_stale(
            older_than=older_than,
            max_auto_retries=max_auto_retries,
            _with_outcomes=True,
        )
        if not isinstance(result, tuple):
            raise AssertionError("reclaim_stale(_with_outcomes=True) 必须返回回收结果")
        return result

    def reclaim_queued(self, *, max_wait: timedelta) -> list[TerminalTask]:
        """无 worker 领取的 queued 任务在等待上限后失败并释放话题。"""

        return self._fail_queued(
            older_than=max_wait,
            error_kind="queued_timeout",
            version_filter=None,
        )

    def fail_unavailable_versions(
        self, *, available_versions: Sequence[str], unavailable_for: timedelta
    ) -> list[TerminalTask]:
        """目标版本连续不可用时收口，绝不把任务改投到另一个版本。"""

        versions = tuple(dict.fromkeys(available_versions))
        return self._fail_queued(
            older_than=unavailable_for,
            error_kind="worker_version_unavailable",
            version_filter=versions,
        )

    def _fail_queued(
        self,
        *,
        older_than: timedelta,
        error_kind: str,
        version_filter: Sequence[str] | None,
    ) -> list[TerminalTask]:
        if error_kind not in {"queued_timeout", "worker_version_unavailable"}:
            raise ValueError("不允许的 queued 失败原因")
        terminals: list[TerminalTask] = []
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                if version_filter is None:
                    sql = """
                    SELECT id, conversation_id FROM task
                     WHERE status = 'queued' AND created_at < now() - %s::interval
                     ORDER BY created_at, id
                     FOR UPDATE SKIP LOCKED
                    """
                    params: tuple[object, ...] = (older_than,)
                elif version_filter:
                    sql = """
                    SELECT id, conversation_id FROM task
                     WHERE status = 'queued' AND created_at < now() - %s::interval
                       AND NOT (target_worker_version = ANY(%s))
                     ORDER BY created_at, id
                     FOR UPDATE SKIP LOCKED
                    """
                    params = (older_than, list(version_filter))
                else:
                    sql = """
                    SELECT id, conversation_id FROM task
                     WHERE status = 'queued' AND created_at < now() - %s::interval
                     ORDER BY created_at, id
                     FOR UPDATE SKIP LOCKED
                    """
                    params = (older_than,)
                cursor.execute(sql, params)
                rows = cursor.fetchall()
                for task_id, conversation_id in rows:
                    # Issue #178（红线）：见 `reclaim_stale` 同一处注释——这个任务
                    # 从未被任何 worker 领取过，同样必须写出唯一、可投递的用户
                    # 终态，不能只改 task.status 就直接释放话题。
                    if not self._write_system_terminal(
                        cursor,
                        task_id=task_id,
                        error_kind=error_kind,
                        from_status="queued",
                    ):
                        continue  # pragma: no cover - 见该方法文档的竞态防御说明
                    terminals.append(
                        TerminalTask(
                            task_id=task_id,
                            conversation_id=conversation_id,
                            status="awaiting_delivery",
                            error_kind=error_kind,
                        )
                    )
        return terminals

    def _write_system_terminal(
        self,
        cursor: Any,
        *,
        task_id: str,
        error_kind: str,
        from_status: str,
    ) -> bool:
        """系统代为收口一个从未被真实 worker 正常执行完的任务时，写出与真实
        worker 完全同型的投递事件序列并转入 ``awaiting_delivery``（Issue #178：
        ``reclaim_stale`` 的心跳超时终态分支、``reclaim_queued``、
        ``fail_unavailable_versions`` 此前只改 ``task.status`` 就直接把话题
        释放掉，跳过了 outbox——数据库里任务已经"结束"，但没有任何可投递的
        终态，用户永远停在处理态收不到结果）。

        **调用方必须已经用 ``SELECT ... FOR UPDATE [SKIP LOCKED]`` 锁定了这一
        行**（``reclaim_stale``/``_fail_queued`` 的既有查询本来就这样做）：这里
        只按 ``status = from_status`` 再确认一次并原子转移，不重复获取锁。

        **不释放 ``conversation.running_task_id``**：话题继续占用，直到 Gateway
        消费 outbox 并调用 ``confirm_delivery``，或二十四小时到期兜底
        （``expire_undelivered_terminals``）——与真实 worker 通过
        ``write_terminal_event`` 收口的既有语义完全一致（#151/#152 状态合同），
        也是"Gateway 不可用时由既有 24 小时到期路径兜底为 delivery_expired"
        这句话能够成立的唯一原因：这条路径复用的是同一张 outbox 表和同一组
        到期/确认机制，不是另起一套。

        **必须先写一条 ``started`` 事件，再写 ``terminal``，不能只写终态**：
        Gateway 消费循环（``apps/gateway/delivery.py`` 的 ``_handle_terminal``）
        只在 ``stream.card_id is not None`` 或 ``stream.fallback_needed`` 为真
        时才会建卡或走文本兜底；一个从未出现过 ``started`` 事件的任务两者都是
        假，终态事件会被无声消费（游标推进）而不产生任何外发，任务只能在
        ``awaiting_delivery`` 里静默沉底、靠 24 小时到期兜底才勉强收口——这正是
        本次要修复的"用户永远收不到终态"换一个更晚的时间点重演。写一条与真实
        worker 完全同形的 ``started``（不带正文，只是一个让 Gateway 建卡/判定
        兜底的信号）事件，复用的是已经过 #152 完整测试的既有消费路径，不需要
        改 Gateway 或卡片协议的任何一行代码。

        幂等：``started``/``terminal`` 各自用固定的 idempotency_key（与真实
        worker 写终态时用的 ``f"{task_id}:terminal"`` 同一形状），重复调用
        （同一批任务被 housekeeping 轮询两次、或写到一半崩溃后整个事务回滚重来）
        不会产生第二条事件——理论上不该发生第二次调用还命中已存在的终态
        （命中即说明状态已经离开 ``from_status``，行锁下不会被再次选中），但
        仍然按既有 `append_delivery_event`/`write_terminal_event` 同一原则做
        防御性检查，返回 ``False`` 表示"这一行已经被处理过，调用方不应该把它
        算作这一轮新产生的终态"。
        """

        content_key = _SYSTEM_TERMINAL_CONTENT_KEYS.get(error_kind)
        if content_key is None:
            raise ValueError(f"没有为 error_kind={error_kind!r} 登记用户可见文案键")
        content = self._content_catalog.text(content_key).text

        started_key = f"{task_id}:system:started"
        if self._find_by_idempotency_key(cursor, started_key) is None:
            self._insert_new_event(
                cursor,
                task_id=task_id,
                worker_id=_SYSTEM_DELIVERY_WORKER_ID,
                event_type=DeliveryEventType.STARTED.value,
                idempotency_key=started_key,
                terminal_kind=None,
                error_kind=None,
                elapsed_seconds=None,
                content=None,
            )

        terminal_key = f"{task_id}:terminal"
        if self._find_by_idempotency_key(cursor, terminal_key) is not None:
            return False

        self._insert_new_event(
            cursor,
            task_id=task_id,
            worker_id=_SYSTEM_DELIVERY_WORKER_ID,
            event_type=DeliveryEventType.TERMINAL.value,
            idempotency_key=terminal_key,
            terminal_kind=TerminalKind.FAILED.value,
            error_kind=error_kind,
            elapsed_seconds=None,
            content=content,
        )
        cursor.execute(
            """
            UPDATE task SET status = 'awaiting_delivery', error_kind = %s, ended_at = now()
             WHERE id = %s AND status = %s
            """,
            (error_kind, task_id, from_status),
        )
        if cursor.rowcount != 1:
            # 上面的 FOR UPDATE 已经锁定并校验过状态；到这里还失败说明状态机被
            # 绕过，宁可响亮失败也不要悄悄不释放/不占用（与 write_terminal_event
            # 的既有处理方式一致）。
            raise RuntimeError(f"任务 {task_id} 在系统代为收口时状态发生了竞态")
        return True
