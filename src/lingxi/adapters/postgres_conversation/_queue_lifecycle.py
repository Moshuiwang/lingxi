"""任务队列领取与收口。

worker 侧的 ``claim``/``finish``/心跳续期，以及 scheduler 侧的心跳超时
回收（``reclaim_stale``）与 queued 超时/灰度版本不可用收口
（``_fail_queued`` 及其两个入口）。

``_write_system_terminal`` 只被本文件内的 ``reclaim_stale``/``_fail_queued``
调用（系统代为收口从未被真实 worker 执行完的任务），因此仍归在这条边界里；
它通过 ``self`` 调用 ``_queue_outbox.py`` 里的
``_find_by_idempotency_key``/``_insert_new_event``——拆分只搬动方法的物理
位置，两个 mixin 组合进同一个 ``PostgresTaskQueue`` 后仍是同一个实例上的
方法查找，调用顺序和行为与拆分前逐位相同。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from lingxi.adapters.postgres import connect
from lingxi.core.delivery.ports import DeliveryEventType, TerminalKind

from ._dataclasses import ClaimedTask, TaskContext, TerminalTask

# 三条"系统代为收口"路径共用的哨兵 worker_id：这些任务从未被一个仍然存活
# 的 worker 正常执行完，没有真实持有者可以填进
# ``task_delivery_event.worker_id``。该列只是 ``TEXT NOT NULL``，没有外键
# 约束，写一个固定、可在诊断查询里一眼认出的哨兵值即可，不影响 Gateway
# 消费循环（它不按 worker_id 过滤）。
_SYSTEM_DELIVERY_WORKER_ID = "system"

# error_kind → 用户可见文案键：全部复用 config/content.toml 已经登记、已过
# 产品审校的既有文案，不发明新文案。这里列出的四个键在被本文件引用之前
# 从未被任何生产代码渲染过，是此前 outbox 改造遗留的孤儿键。
_SYSTEM_TERMINAL_CONTENT_KEYS: dict[str, str] = {
    "queued_timeout": "worker.queued_timeout",
    "worker_version_unavailable": "worker.version_unavailable",
    "retry_exhausted": "worker.running_timeout",
    "side_effect_uncertain": "worker.side_effect_uncertain",
}

_FINISH_TASK_SQL = """
UPDATE task
   SET status = %s,
       ended_at = now(),
       error_kind = COALESCE(%s, error_kind),
       side_effect_state = COALESCE(%s, side_effect_state)
 WHERE id = %s AND worker_id = %s AND status = 'running'
"""

_RELEASE_CONVERSATION_SQL = """
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
"""

_RECLAIM_STALE_CANDIDATES_SQL = """
SELECT id, conversation_id, attempts, side_effect_state
  FROM task
 WHERE status = 'running'
   AND heartbeat_at < now() - %s::interval
 ORDER BY heartbeat_at, id
 FOR UPDATE SKIP LOCKED
"""


def _row_to_claimed_task(row: Any) -> ClaimedTask:
    return ClaimedTask(
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


class _TaskLifecycleMixin:
    def claim(
        self, *, worker_id: str, target_worker_version: str, limit: int = 1
    ) -> list[ClaimedTask]:
        """领取任务。

        两条约束都在这句 SQL 里：``FOR UPDATE SKIP LOCKED`` 保证并发领取
        不重复不阻塞；``target_worker_version = %s`` 保证声明版本的 worker
        只领得到匹配任务，去掉它 canary 任务会被 stable worker 领走。
        **本方法不写 ``target_worker_version``**——入队时已固化，重试与回收
        都不得改写，迁移 013 的触发器会让误写直接抛异常。
        """
        # 这是 worker 主循环每个 poll_interval 都会执行一次的发现查询——即使
        # 空转也照样命中，因此走 `_run_polling_operation`（默认逐字节等价于
        # 原来的 `connect(...)`，只有装配方显式打开复用时才改为持有常驻
        # 连接；打开时复用连接首次失败会重建重试一次，见该方法文档）。

        # SQL 就地内联、不提到模块常量：两条真库结构性用例直接用
        # `inspect.getsource(...claim)` 扫这个方法自身的源码文本找 SET
        # 子句/`FOR UPDATE SKIP LOCKED`，挪到别处会让判据落空、断言失明。

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
                return [_row_to_claimed_task(row) for row in cursor.fetchall()]

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

        两层条件缺一不可：任务更新带 ``worker_id = %s AND status = 'running'``
        ——仅凭 task_id 不够，僵尸 worker 心跳超时后被重排、由另一个 worker
        领取，task_id 不变但持有者已经换人，若只判任务归属会把仍在执行的
        新持有者的话题提前释放，打破同话题串行；话题释放带
        ``running_task_id = %s`` 保证只有持有者能释放。``last_task_ended_at``
        在这里落，是两小时规则的唯一依据。
        """
        if status not in {"succeeded", "failed", "stopped"}:
            raise ValueError("任务只能以 succeeded、failed 或 stopped 收口")
        if side_effect_state not in {None, "none", "possible"}:
            raise ValueError("side_effect_state 只能是 none 或 possible")
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    _FINISH_TASK_SQL,
                    (status, error_kind, side_effect_state, task_id, worker_id),
                )
                if cursor.rowcount != 1:
                    # 不是这一代执行者（或任务早已结束）：什么都不改，也不释放话题。
                    return False
                return self._release_conversation_after_finish(
                    cursor,
                    conversation_id=conversation_id,
                    task_id=task_id,
                    agent_session_id=agent_session_id,
                )

    def _release_conversation_after_finish(
        self,
        cursor: Any,
        *,
        conversation_id: str,
        task_id: str,
        agent_session_id: str | None,
    ) -> bool:
        cursor.execute(
            _RELEASE_CONVERSATION_SQL,
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
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                UPDATE task SET heartbeat_at = now()
                 WHERE id = %s AND worker_id = %s AND status = 'running'
                """,
                (task_id, worker_id),
            )
            return cursor.rowcount == 1

    def stop_requested(self, *, task_id: str, worker_id: str) -> bool:
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
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
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                UPDATE task SET side_effect_state = 'possible'
                 WHERE id = %s AND worker_id = %s AND status = 'running'
                """,
                (task_id, worker_id),
            )
            return cursor.rowcount == 1

    def task_context(self, *, task_id: str, worker_id: str) -> TaskContext | None:
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
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
        被分到的那个版本（`V-灰度-01` 的回收路径）。安全重试的 ``UPDATE`` 就地内联、
        不提到模块常量：``tests/test_gateway_postgres.py::WorkerVersionTests``
        用 ``inspect.getsource(...reclaim_stale)`` 直接扫本方法自身的源码文本
        确认 SET 子句没有写 ``target_worker_version``，挪到别处会让这条判据
        落空。
        """
        if isinstance(max_auto_retries, bool) or max_auto_retries < 0:
            raise ValueError("max_auto_retries 必须是非负整数")

        requeued: list[str] = []
        terminal: list[TerminalTask] = []
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(_RECLAIM_STALE_CANDIDATES_SQL, (older_than,))
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
                    terminal_task = self._reclaim_stale_terminal(
                        cursor,
                        task_id=task_id,
                        conversation_id=conversation_id,
                        side_effect_state=side_effect_state,
                    )
                    if terminal_task is not None:
                        terminal.append(terminal_task)
        if _with_outcomes:
            return requeued, terminal
        return requeued

    def _reclaim_stale_terminal(
        self,
        cursor: Any,
        *,
        task_id: str,
        conversation_id: str,
        side_effect_state: str | None,
    ) -> TerminalTask | None:
        """不可安全重试的心跳超时任务：代为收口为失败终态。"""
        error_kind = "side_effect_uncertain" if side_effect_state != "none" else "retry_exhausted"
        # 这类任务从未被一个仍然存活的 worker 正常收口，不能只改 task.status
        # 就直接释放话题——必须写出与真实 worker 完全同型的投递事件序列并
        # 转入 awaiting_delivery，见 `_write_system_terminal` 的说明。
        if not self._write_system_terminal(
            cursor, task_id=task_id, error_kind=error_kind, from_status="running"
        ):
            return None  # pragma: no cover - 见该方法文档的竞态防御说明
        return TerminalTask(
            task_id=task_id,
            conversation_id=conversation_id,
            status="awaiting_delivery",
            error_kind=error_kind,
        )

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
                    # 这个任务从未被任何 worker 领取过，同样必须写出唯一、可投递
                    # 的用户终态，不能只改 task.status 就直接释放话题，见
                    # `reclaim_stale` 同一处注释。
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
        """系统代为收口一个从未被真实 worker 执行完的任务。

        写出与真实 worker 完全同型的投递事件序列（先 ``started`` 后
        ``terminal``）并转入 ``awaiting_delivery``，复用既有 outbox 消费/
        到期路径；不释放 ``conversation.running_task_id``，由 Gateway 确认
        或二十四小时到期兜底释放。调用方必须已用 ``FOR UPDATE [SKIP LOCKED]``
        锁定该行；``started``/``terminal`` 各自按固定 idempotency_key 幂等，
        重复调用不会产生第二条事件，命中已存在终态时返回 ``False``。
        """
        content_key = _SYSTEM_TERMINAL_CONTENT_KEYS.get(error_kind)
        if content_key is None:
            raise ValueError(f"没有为 error_kind={error_kind!r} 登记用户可见文案键")
        content = self._content_catalog.text(content_key).text

        self._ensure_system_started_event(cursor, task_id=task_id)

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

    def _ensure_system_started_event(self, cursor: Any, *, task_id: str) -> None:
        """幂等写入系统代为收口的 ``started`` 哨兵事件。

        Gateway 消费循环只在见过 ``started`` 事件后才会建卡或判定文本兜底；
        没有它，终态事件会被无声消费（游标推进）而不产生任何外发，任务只能
        在 ``awaiting_delivery`` 里静默沉底、靠二十四小时到期兜底才勉强收口
        ——写一条不带正文的 ``started`` 事件即可复用既有消费路径。
        """
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
