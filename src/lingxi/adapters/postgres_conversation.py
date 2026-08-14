"""会话、任务队列与入站事件的 PostgreSQL 存取。

沿用仓库既有惯例（``adapters/postgres_identity.py``）：``psycopg`` 在 ``__init__`` 里
延迟导入，构造时不连库，每次调用自带连接。

**事务边界是这个模块最重要的部分。** ``transaction()`` 交出的对象上才有写方法，
拿不到"事务外顺手写一条"的入口——`V-队列-01` 要求 ``inbound_event`` 插入、
``conversation`` 抢占、``task`` 插入落在同一事务里，这条约束由类型形状承担，
不靠调用方记得。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Iterator, Sequence

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.conversation.ports import (
    ConversationRecord,
    HandledAs,
    UserRecord,
    UserState,
)
from lingxi.core.delivery.ports import DeliveryEventType, resolve_delivered_outcome
from lingxi.core.ids import new_id

logger = logging.getLogger(__name__)

# 入队后发的通知通道名。worker 监听它以便"提交即领取"，但**不能只靠它**：
# NOTIFY 在连接断开、监听方重启的窗口里会整批丢失，兜底轮询是必需的（`V-队列-05`）。
TASK_QUEUED_CHANNEL = "task_queued"


def _user_state(provisioning_state: str, account_state: str) -> UserState:
    """把 ``app_user`` 的两列映射成管线关心的三态。

    **先判停用再判开通。** 两个判断只在一种输入上分歧：一个**既未开通、又已停用**的
    用户（``provisioning_state != 'active'`` 且 ``account_state != 'enabled'``）——
    本实现判他 ``SUSPENDED``，反过来写会判 ``NOT_PROVISIONED``。
    选停用是因为两条分支的后果不同：未开通分支在后续切片里会去**启动自动匹配与开通**
    （#65），而这个人的账号已经被管理员停掉了，替他开通是把管理员刚撤销的东西又装回去。
    停用分支只回一句说明，是两者中可逆的那个。

    （早先这里写的理由是"否则被停用的老用户会被当成正常用户继续排任务"，那是错的：
    ``provisioning_state='active'`` 且 ``account_state='suspended'`` 的用户在两种
    顺序下都会落进停用分支。理由写错比没写更糟，一并纠正。）
    """

    if account_state != "enabled":
        return UserState.SUSPENDED
    if provisioning_state != "active":
        return UserState.NOT_PROVISIONED
    return UserState.ACTIVE


class _Transaction:
    """一个事务内的写操作。实现 ``core.conversation.ports.GatewayTransaction``。"""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def _execute(self, sql: str, parameters: tuple = ()) -> Any:
        cursor = self._connection.cursor()
        cursor.execute(sql, parameters)
        return cursor

    def insert_inbound_event(
        self, *, event_id: str, event_type: str, user_open_id: str, trace_id: str
    ) -> bool:
        """插入事件行；已存在返回 ``False``。

        ``ON CONFLICT DO NOTHING`` 是幂等的实现手段：并发投递时第二个事务会**阻塞**到
        第一个提交，然后什么也不做、影响 0 行（`V-接入-02`）。``expires_at`` 随便传一个
        值即可，迁移 013 的触发器会按 ``received_at`` 改写它。
        """

        cursor = self._execute(
            """
            INSERT INTO inbound_event
                (feishu_event_id, event_type, user_open_id, trace_id, expires_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (feishu_event_id) DO NOTHING
            """,
            (event_id, event_type, user_open_id, trace_id),
        )
        return cursor.rowcount == 1

    def mark_handled_as(self, *, event_id: str, handled_as: HandledAs) -> None:
        self._execute(
            "UPDATE inbound_event SET handled_as = %s WHERE feishu_event_id = %s",
            (handled_as.value, event_id),
        )

    def lookup_user(self, *, open_id: str) -> UserRecord | None:
        cursor = self._execute(
            "SELECT id, provisioning_state, account_state FROM app_user WHERE feishu_open_id = %s",
            (open_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return UserRecord(user_id=row[0], state=_user_state(row[1], row[2]))

    def ensure_conversation(
        self, *, user_id: str, chat_id: str, thread_id: str | None
    ) -> ConversationRecord:
        """取回该话题的会话行，没有就建一个。

        ``ON CONFLICT`` 的目标必须写成与 ``conversation_scope_uniq`` **完全一致**的表达式
        （含 ``COALESCE``），否则 PostgreSQL 找不到对应的唯一索引，并发首条消息会撞成
        真实错误而不是走冲突分支。
        """

        self._execute(
            """
            INSERT INTO conversation (id, user_id, feishu_chat_id, feishu_thread_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id, feishu_chat_id, COALESCE(feishu_thread_id, ''))
            DO NOTHING
            """,
            (new_id("cnv"), user_id, chat_id, thread_id),
        )
        cursor = self._execute(
            """
            SELECT id, agent_session_id, last_task_ended_at, running_task_id
              FROM conversation
             WHERE user_id = %s AND feishu_chat_id = %s
               AND COALESCE(feishu_thread_id, '') = COALESCE(%s, '')
            """,
            (user_id, chat_id, thread_id),
        )
        row = cursor.fetchone()
        if row is None:  # pragma: no cover - 插入与查询同事务，不应发生
            raise RuntimeError("会话行插入后立刻查不到，事务隔离级别被改动了？")
        return ConversationRecord(
            conversation_id=row[0],
            agent_session_id=row[1],
            last_task_ended_at=row[2],
            running_task_id=row[3],
        )

    def claim_conversation(self, *, conversation_id: str, task_id: str) -> bool:
        """条件更新抢占话题。

        ``AND running_task_id IS NULL`` 不能省：省掉之后第二个任务会直接覆盖第一个，
        两个任务在同一话题上并行（PR #12 的原始错误，数据库设计已实测记录）。
        """

        cursor = self._execute(
            """
            UPDATE conversation SET running_task_id = %s
             WHERE id = %s AND running_task_id IS NULL
            """,
            (task_id, conversation_id),
        )
        return cursor.rowcount == 1

    def insert_task(
        self,
        *,
        task_id: str,
        conversation_id: str,
        user_id: str,
        inbound_event_id: str,
        prompt: str,
        resumed_session: bool,
        target_worker_version: str,
        reply_to_message_id: str | None = None,
    ) -> None:
        self._execute(
            """
            INSERT INTO task
                (id, conversation_id, user_id, inbound_event_id, prompt,
                resumed_session, target_worker_version, reply_to_message_id,
                content_expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
            """,
            (
                task_id,
                conversation_id,
                user_id,
                inbound_event_id,
                prompt,
                resumed_session,
                target_worker_version,
                reply_to_message_id,
            ),
        )

    def clear_agent_session(self, *, conversation_id: str) -> bool:
        """``/new``：清空当前对话上下文。返回是否真的清了。

        条件里的 ``id = %s`` 让它天然只影响这一行，其他话题的 ``agent_session_id``
        与 ``last_task_ended_at`` 都不动（`V-会话-05`）。

        ``AND running_task_id IS NULL`` 不能省：管线读到的忙碌状态是事务开始时的快照，
        另一条连接可能在这之间抢占成功并已经在执行。没有这个条件时，两个并发请求
        （一条 `/new` + 一条普通消息）会同时成功，把一个**正在运行**的任务的上下文
        清掉——合同规定忙碌期的 `/new` 只该得到提示。
        """

        cursor = self._execute(
            """
            UPDATE conversation SET agent_session_id = NULL
             WHERE id = %s AND running_task_id IS NULL
            """,
            (conversation_id,),
        )
        cleared = cursor.rowcount == 1
        if cleared:
            # /new 同一触发点同时清除该会话已保留的安全结果正文（产品合同「数据
            # 保留与删除」第一条、数据库设计「问数结果投递事件与会话保留 Outbox」）；
            # 与上面的会话上下文重置同一事务提交或回滚，不逐位置单独判断或延后。
            self.clear_delivered_content_for_conversation(conversation_id=conversation_id)
        return cleared

    def clear_delivered_content_for_conversation(self, *, conversation_id: str) -> int:
        """把已确认送达（``platform_received``）但仍随会话保留的投递正文清空。

        只清 ``content``：``event_type``/``sequence``/``terminal_kind``/
        ``error_kind`` 等低敏事实按 #151 状态合同保留最长九十天，供审计与到期
        清理链复用；只有正文本身在会话边界触发时被清除（数据库设计「问数结果
        投递事件与会话保留 Outbox」）。只匹配 ``platform_received_at IS NOT NULL``
        的行——送达前的正文走独立的二十四小时到期路径
        （``PostgresTaskQueue.expire_undelivered_terminals``），不受这个方法影响。
        返回值是本次清空的事件行数，只用于日志/断言，不承载业务语义。
        """

        cursor = self._execute(
            """
            UPDATE task_delivery_event AS e
               SET content = NULL
              FROM task AS t
             WHERE e.task_id = t.id
               AND t.conversation_id = %s
               AND e.platform_received_at IS NOT NULL
               AND e.content IS NOT NULL
            """,
            (conversation_id,),
        )
        return cursor.rowcount

    def request_stop(self, *, conversation_id: str) -> str | None:
        """``/stop``：给该话题当前占用的任务置 ``stop_requested``。

        目标任务从 ``conversation.running_task_id`` 取，因此**只可能**影响这个话题占用
        的那一个任务，其他话题的运行中任务不受影响（`V-会话-06`）。已结束的任务不再
        接受停止请求，故带 ``status`` 条件。
        """

        cursor = self._execute(
            """
            UPDATE task SET stop_requested = TRUE
             WHERE id = (SELECT running_task_id FROM conversation WHERE id = %s)
               AND status IN ('queued', 'running')
            RETURNING id
            """,
            (conversation_id,),
        )
        row = cursor.fetchone()
        return row[0] if row else None

    def notify_task_queued(self) -> None:
        """在事务内发通知：随提交一起对外可见，事务回滚则通知也不会发出。"""

        self._execute(f"NOTIFY {TASK_QUEUED_CHANNEL}")


# 保留旧名称作为兼容导出；默认值的唯一来源是 adapters.postgres。
DEFAULT_CONNECT_TIMEOUT_SECONDS = DEFAULT_POSTGRES_TIMEOUTS.connect_timeout_seconds
DEFAULT_STATEMENT_TIMEOUT_MS = DEFAULT_POSTGRES_TIMEOUTS.statement_timeout_seconds * 1000


def _seconds_from_milliseconds(name: str, value: int) -> int:
    """把旧构造参数转成统一配置；不允许丢失精度地改变等待边界。"""

    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value % 1000:
        raise ValueError(f"{name} 必须是正整数秒的毫秒数")
    return value // 1000


class PostgresGatewayStore:
    """实现 ``core.conversation.ports.GatewayStore``。"""

    def __init__(
        self,
        dsn: str,
        *,
        timeouts: PostgresTimeouts | None = None,
        connect_timeout: int | None = None,
        statement_timeout_ms: int | None = None,
        lock_timeout_ms: int | None = None,
    ) -> None:
        self._dsn = dsn
        if timeouts is not None and any(
            value is not None for value in (connect_timeout, statement_timeout_ms, lock_timeout_ms)
        ):
            raise ValueError("PostgreSQL 超时只能通过 timeouts 或兼容参数中的一种提供")
        if timeouts is not None:
            self._timeouts = timeouts
        else:
            self._timeouts = PostgresTimeouts(
                connect_timeout_seconds=(
                    DEFAULT_CONNECT_TIMEOUT_SECONDS if connect_timeout is None else connect_timeout
                ),
                statement_timeout_seconds=(
                    DEFAULT_POSTGRES_TIMEOUTS.statement_timeout_seconds
                    if statement_timeout_ms is None
                    else _seconds_from_milliseconds("statement_timeout_ms", statement_timeout_ms)
                ),
                lock_timeout_seconds=(
                    DEFAULT_POSTGRES_TIMEOUTS.lock_timeout_seconds
                    if lock_timeout_ms is None
                    else _seconds_from_milliseconds("lock_timeout_ms", lock_timeout_ms)
                ),
            )

    @contextmanager
    def transaction(self) -> Iterator[_Transaction]:
        """一个连接、一个事务。异常时整体回滚。"""

        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                yield _Transaction(connection)

    def claim_queue_failure_notice(self, *, event_id: str) -> bool:
        """在独立事务取得一次队列失败提示的发送权。"""

        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    """
                    INSERT INTO queue_failure_notice (feishu_event_id, expires_at)
                    VALUES (%s, now())
                    ON CONFLICT (feishu_event_id) DO NOTHING
                    RETURNING feishu_event_id
                    """,
                    (event_id,),
                )
                return cursor.fetchone() is not None


@dataclass(frozen=True)
class ClaimedTask:
    task_id: str
    conversation_id: str
    user_id: str
    prompt: str
    resumed_session: bool
    target_worker_version: str
    attempts: int
    reply_to_message_id: str | None = None
    stop_requested: bool = False
    side_effect_state: str = "none"


@dataclass(frozen=True)
class TaskContext:
    """worker 收口所需的同话题投递与会话上下文。"""

    task_id: str
    conversation_id: str
    user_id: str
    prompt: str
    resumed_session: bool
    target_worker_version: str
    attempts: int
    reply_to_message_id: str | None
    chat_id: str
    thread_id: str | None
    agent_session_id: str | None
    stop_requested: bool
    side_effect_state: str


@dataclass(frozen=True)
class TerminalTask:
    task_id: str
    conversation_id: str
    status: str
    error_kind: str


@dataclass(frozen=True)
class AppendedEvent:
    """一次投递事件写入的结果（Issue #151）。

    ``duplicate=True`` 表示 ``idempotency_key`` 已存在——调用方的这次写入是对同一
    次逻辑动作的重试，``sequence`` 是**已经写入的那一条**的序号，不是新分配的；
    调用方不应把它当作"又发生了一次"来计数或重复触发外部副作用。
    """

    sequence: int
    duplicate: bool


class PostgresTaskQueue:
    """worker 与 scheduler 侧的队列操作。

    worker 与 scheduler 共用这些原子操作。所有连接都经过仓库唯一的 PostgreSQL
    factory；没有连接字符串或 psycopg 直连旁路。
    """

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        self._dsn = dsn
        self._timeouts = timeouts

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

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
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
                    UPDATE conversation
                       SET running_task_id = NULL,
                           last_task_ended_at = now(),
                           agent_session_id = COALESCE(%s, agent_session_id)
                     WHERE id = %s AND running_task_id = %s
                    """,
                    (agent_session_id, conversation_id, task_id),
                )
                return cursor.rowcount == 1

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
                    cursor.execute(
                        """
                        UPDATE task SET status = 'failed', ended_at = now(), error_kind = %s
                         WHERE id = %s AND status = 'running'
                        """,
                        (error_kind, task_id),
                    )
                    cursor.execute(
                        """
                        UPDATE conversation SET running_task_id = NULL, last_task_ended_at = now()
                         WHERE id = %s AND running_task_id = %s
                        """,
                        (conversation_id, task_id),
                    )
                    terminal.append(
                        TerminalTask(
                            task_id=task_id,
                            conversation_id=conversation_id,
                            status="failed",
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
                    cursor.execute(
                        """
                        UPDATE task SET status = 'failed', ended_at = now(), error_kind = %s
                         WHERE id = %s AND status = 'queued'
                        """,
                        (error_kind, task_id),
                    )
                    cursor.execute(
                        """
                        UPDATE conversation SET running_task_id = NULL, last_task_ended_at = now()
                         WHERE id = %s AND running_task_id = %s
                        """,
                        (conversation_id, task_id),
                    )
                    terminals.append(
                        TerminalTask(
                            task_id=task_id,
                            conversation_id=conversation_id,
                            status="failed",
                            error_kind=error_kind,
                        )
                    )
        return terminals

    # -----------------------------------------------------------------
    # 投递事件 outbox（Issue #151）
    #
    # append_delivery_event（非终态）与 write_terminal_event（终态）共用同一套
    # 所有权与顺序保证：先 `SELECT ... FOR UPDATE` 锁定 task 行、核对 `worker_id`
    # 与 `status`，再在同一把锁下计算 `MAX(sequence)+1`——两个并发写者
    # 因此天然串行化，不需要额外的咨询锁（与数据库设计第五节「同话题串行靠条件
    # 更新」同一手法，只是这里锁的是行而不是靠影响行数判断）。
    # 幂等由调用方提供的 idempotency_key 承担：命中已有行时原样返回该行的
    # sequence 并标记 duplicate=True，不创建第二条事件、不重复触发调用方的
    # 副作用计数（V-投递-01/02）。
    # -----------------------------------------------------------------

    def append_delivery_event(
        self,
        *,
        task_id: str,
        worker_id: str,
        event_type: str,
        idempotency_key: str,
        elapsed_seconds: int | None = None,
        content: str | None = None,
    ) -> AppendedEvent | None:
        """写入 ``started``/``progress``/``safely_releasable_answer`` 事件。

        只有当前持有该任务的 worker（``worker_id`` 匹配且 ``status='running'``）能
        写；否则返回 ``None``——僵尸 worker 或已经离开 running 态的迟到写入必须被
        拒绝，不能悄悄创建游离事件。``content`` 必须是调用方已经过 #149 安全检查、
        允许展示给当前用户的文本；本方法不做安全判断，只负责持久化。终态请使用
        :meth:`write_terminal_event`（需要同时转移任务状态，语义不同）。

        **幂等判定先于所有权判定**：如果 ``idempotency_key`` 已经写过，即使此刻
        任务已经离开 ``running``（例如同一次终态写入之后又收到一次迟到的
        进度重试），也原样返回那一行并标记 ``duplicate=True``——这正是"重放一次
        已经成功的写入"该有的行为，不能因为状态已经前进就报告失败。只有
        ``idempotency_key`` 全新时才需要真正的所有权校验，防止僵尸 worker 借着
        一个新 key 悄悄插入游离事件。
        """

        if event_type not in (
            DeliveryEventType.STARTED.value,
            DeliveryEventType.PROGRESS.value,
            DeliveryEventType.SAFELY_RELEASABLE_ANSWER.value,
        ):
            raise ValueError("append_delivery_event 只处理非终态事件类型")
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    "SELECT worker_id, status FROM task WHERE id = %s FOR UPDATE",
                    (task_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                existing = self._find_by_idempotency_key(cursor, idempotency_key)
                if existing is not None:
                    return existing
                if row[0] != worker_id or row[1] != "running":
                    return None
                return self._insert_new_event(
                    cursor,
                    task_id=task_id,
                    worker_id=worker_id,
                    event_type=event_type,
                    idempotency_key=idempotency_key,
                    terminal_kind=None,
                    error_kind=None,
                    elapsed_seconds=elapsed_seconds,
                    content=content,
                )

    def write_terminal_event(
        self,
        *,
        task_id: str,
        worker_id: str,
        terminal_kind: str,
        error_kind: str | None,
        content: str | None,
        elapsed_seconds: int | None = None,
        agent_session_id: str | None = None,
    ) -> AppendedEvent | None:
        """写入 ``terminal`` 事件并把任务从 ``running`` 转为 ``awaiting_delivery``。

        与任务状态转换在**同一事务**提交或整体回滚（状态合同第 2 条）；
        ``conversation.running_task_id`` 在这里**不释放**——话题继续占用直到投递
        解析（:meth:`confirm_delivery` 或 :meth:`expire_undelivered_terminals`）。
        返回 ``None`` 表示当前调用方已不再持有该任务（僵尸 worker、任务已被回收
        或重复收口），调用方不应据此产生第二次用户可见的副作用。

        幂等判定同样先于所有权判定（见 :meth:`append_delivery_event` 的说明）：
        重复调用（同一次写入的网络重试）即使任务此刻已经不在 ``running``（正是
        第一次调用成功转移之后的样子），也应原样返回 ``duplicate=True``，而不是
        误判成"所有权已丢失"返回 ``None``。
        """

        idempotency_key = f"{task_id}:terminal"
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    "SELECT worker_id, status FROM task WHERE id = %s FOR UPDATE",
                    (task_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                existing = self._find_by_idempotency_key(cursor, idempotency_key)
                if existing is not None:
                    # 已经写过 terminal：拒绝第二条有效终态（V-投递-02），任务状态
                    # 不再改动——上一次写入时已经完成过这个转移。
                    return existing
                if row[0] != worker_id or row[1] != "running":
                    return None
                appended = self._insert_new_event(
                    cursor,
                    task_id=task_id,
                    worker_id=worker_id,
                    event_type=DeliveryEventType.TERMINAL.value,
                    idempotency_key=idempotency_key,
                    terminal_kind=terminal_kind,
                    error_kind=error_kind,
                    elapsed_seconds=elapsed_seconds,
                    content=content,
                    agent_session_id=agent_session_id,
                )
                cursor.execute(
                    """
                    UPDATE task
                       SET status = 'awaiting_delivery',
                           error_kind = COALESCE(%s, error_kind),
                           ended_at = now()
                     WHERE id = %s AND worker_id = %s AND status = 'running'
                    """,
                    (error_kind, task_id, worker_id),
                )
                if cursor.rowcount != 1:
                    # 上面的 FOR UPDATE 已经锁定并校验过持有者与状态；到这里还失败
                    # 说明状态机被绕过，宁可响亮失败也不要悄悄不释放/不占用。
                    raise RuntimeError(f"任务 {task_id} 在写终态事件时状态发生了竞态")
                return appended

    @staticmethod
    def _find_by_idempotency_key(cursor: Any, idempotency_key: str) -> AppendedEvent | None:
        """已存在则返回该行的 ``sequence`` 并标记 ``duplicate=True``；否则 ``None``。

        调用方必须先用 ``SELECT ... FOR UPDATE`` 锁定对应的 task 行，保证同一任务
        的并发写者在这里天然串行化，不会看到彼此尚未提交的插入。
        """

        cursor.execute(
            "SELECT sequence FROM task_delivery_event WHERE idempotency_key = %s",
            (idempotency_key,),
        )
        existing = cursor.fetchone()
        if existing is None:
            return None
        return AppendedEvent(sequence=existing[0], duplicate=True)

    @staticmethod
    def _insert_new_event(
        cursor: Any,
        *,
        task_id: str,
        worker_id: str,
        event_type: str,
        idempotency_key: str,
        terminal_kind: str | None,
        error_kind: str | None,
        elapsed_seconds: int | None,
        content: str | None,
        agent_session_id: str | None = None,
    ) -> AppendedEvent:
        """插入一条**确定尚不存在**的新事件；调用方必须已经确认
        ``idempotency_key`` 不重复、且已经用 ``SELECT ... FOR UPDATE`` 锁定了对应
        的 task 行（保证 ``sequence`` 的计算不会与并发写者相互覆盖）。
        """

        cursor.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM task_delivery_event WHERE task_id = %s",
            (task_id,),
        )
        next_sequence = cursor.fetchone()[0]
        cursor.execute(
            """
            INSERT INTO task_delivery_event
                (id, task_id, sequence, event_type, terminal_kind, error_kind,
                 elapsed_seconds, content, worker_id, idempotency_key, agent_session_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                new_id("tde"),
                task_id,
                next_sequence,
                event_type,
                terminal_kind,
                error_kind,
                elapsed_seconds,
                content,
                worker_id,
                idempotency_key,
                agent_session_id,
            ),
        )
        return AppendedEvent(sequence=next_sequence, duplicate=False)

    def confirm_delivery(
        self,
        *,
        task_id: str,
        platform_message_kind: str,
        platform_message_id: str,
    ) -> bool:
        """记录 ``platform_received`` 并收口投递：解析业务终态、释放话题。

        这是 Gateway 消费 outbox 后调用的冻结接口（Issue #151 与 #152 之间的调用
        合同）；本 Story 只实现并在 L2 真库验证它，不接入任何生产调用方——真正
        在成功送达后调用它属于 #152（Gateway 消费循环），不在本 Story 范围内。

        业务终态完全来自写终态事件时记录的 ``terminal_kind``/``error_kind``——
        投递确认成功不改写业务结果（`V-投递-04`）：业务失败的任务在这里仍然收敛
        为 ``failed``，不会因为飞书接受了失败卡片就变成 ``succeeded``。

        返回值只有两种合法含义：``True`` 表示三条写入（事件确认、任务收敛、
        会话回写）已经**全部**提交；``False`` 表示第一步查询就没找到可确认的
        东西（任务已经不在 ``awaiting_delivery``，或没有尚未确认的 terminal
        事件）——这种情况下**没有任何写入发生**。``conversation`` 更新失败
        （``running_task_id`` 与预期不符，通常意味着有其他路径已经在这中间改动
        了这个话题）不会走到这两种含义里的任何一种：它是内部不变量被破坏，
        与写终态事件时 ``task`` 行的 ``rowcount != 1`` 检查同一处理方式——整个
        事务 ``raise`` 回滚，不悄悄提交前两步再返回一个和"什么都没确认到"
        看起来一样的 ``False``。此前这里对 ``conversation`` 更新的失败只是
        ``return cursor.rowcount == 1``，会在事件与任务都已提交之后，把
        ``agent_session_id`` 静默丢弃、只留一个无法区分含义的 ``False``
        （内审 P2-4，真库负向用例见
        ``test_confirm_delivery_rolls_back_entirely_when_the_conversation_write_conflicts``）。
        """

        if platform_message_kind not in ("card", "text"):
            raise ValueError("platform_message_kind 只能是 card 或 text")
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    """
                    SELECT t.conversation_id, e.terminal_kind, e.error_kind, e.agent_session_id
                      FROM task AS t
                      JOIN task_delivery_event AS e
                        ON e.task_id = t.id AND e.event_type = 'terminal'
                     WHERE t.id = %s AND t.status = 'awaiting_delivery'
                       AND e.platform_received_at IS NULL
                     FOR UPDATE OF t
                    """,
                    (task_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return False
                conversation_id, terminal_kind, event_error_kind, agent_session_id = row
                outcome = resolve_delivered_outcome(
                    terminal_kind=terminal_kind, error_kind=event_error_kind
                )
                cursor.execute(
                    """
                    UPDATE task_delivery_event
                       SET platform_received_at = now(),
                           platform_message_kind = %s,
                           platform_message_id = %s
                     WHERE task_id = %s AND event_type = 'terminal'
                    """,
                    (platform_message_kind, platform_message_id, task_id),
                )
                cursor.execute(
                    """
                    UPDATE task SET status = %s, error_kind = %s
                     WHERE id = %s AND status = 'awaiting_delivery'
                    """,
                    (outcome.status, outcome.error_kind, task_id),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"任务 {task_id} 在确认送达时状态发生了竞态")
                # 只在业务成功时才有 agent_session_id；COALESCE 保证失败/停止/拒发
                # 终态不会把已有的会话延续状态清空——这些终态压根没有产生新会话，
                # 或者产生的会话按上面的取舍不该被继续使用。
                cursor.execute(
                    """
                    UPDATE conversation
                       SET running_task_id = NULL,
                           last_task_ended_at = now(),
                           agent_session_id = COALESCE(%s, agent_session_id)
                     WHERE id = %s AND running_task_id = %s
                    """,
                    (agent_session_id, conversation_id, task_id),
                )
                if cursor.rowcount != 1:
                    # 上面两条写入（事件确认、task 收敛）已经在这个事务里执行，
                    # 但还没提交：整个 with 块以异常退出，psycopg 回滚整个事务，
                    # 三条写入因此要么全部不生效。宁可响亮失败也不要悄悄丢弃
                    # agent_session_id、或者用一个和"没有可确认的东西"含义相同
                    # 的 False 掩盖"已经确认到一半"的竞态（内审 P2-4）。
                    raise RuntimeError(
                        f"任务 {task_id} 确认送达时 conversation {conversation_id} "
                        "的话题占用状态发生了竞态"
                    )
                return True

    def expire_undelivered_terminals(self) -> list[TerminalTask]:
        """二十四小时到期仍未确认送达：强制收敛为 ``failed``/``delivery_expired``，
        释放话题，清空事件正文，只留低敏事实（状态合同第 8 条、`V-投递-06`）。

        无论原始业务结论是什么都会被覆盖——二十四小时到期时系统不可把任务写成
        用户已取得结果，这是投递状态唯一允许改写业务结论的路径（`V-投递-04`
        的例外情形，在核心 :mod:`lingxi.core.delivery.ports` 中单独命名）。

        判定直接读 ``task_delivery_event.expires_at`` 本身——那一列由迁移 0059
        的触发器锁定为 ``created_at + 24 小时``、调用方写什么都会被覆盖，是这条
        二十四小时上限唯一的真相来源。**不接受调用方传入的窗口参数**：早先这里
        有一个 ``older_than`` 参数，由 ``WorkerConfig.delivery_expiry_seconds``
        （环境变量 ``DELIVERY_EXPIRY_SECONDS``）注入，实际查询因此从来没有读过
        ``expires_at`` 列本身，而是在应用层用这个可配置窗口重新计算
        ``created_at < now() - 窗口``——一次环境变量改动就能把触发器锁定的上限
        抬到任意长度，数据库完全不会阻止（内审 P2-1）。测试需要更短的等待窗口
        时，直接构造一条 ``created_at`` 已经在过去的行（触发器只锁定
        ``UPDATE`` 时的 ``created_at``，``INSERT`` 时调用方仍可以指定任意值），
        不再通过参数放大或缩小 24 小时这个业务常量。
        """

        terminals: list[TerminalTask] = []
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    """
                    SELECT t.id, t.conversation_id
                      FROM task AS t
                      JOIN task_delivery_event AS e
                        ON e.task_id = t.id AND e.event_type = 'terminal'
                     WHERE t.status = 'awaiting_delivery'
                       AND e.platform_received_at IS NULL
                       AND e.expires_at <= now()
                     ORDER BY e.created_at, t.id
                     FOR UPDATE OF t SKIP LOCKED
                    """
                )
                rows = cursor.fetchall()
                for task_id, conversation_id in rows:
                    cursor.execute(
                        """
                        UPDATE task
                           SET status = 'failed', error_kind = 'delivery_expired'
                         WHERE id = %s AND status = 'awaiting_delivery'
                        """,
                        (task_id,),
                    )
                    cursor.execute(
                        """
                        UPDATE conversation SET running_task_id = NULL, last_task_ended_at = now()
                         WHERE id = %s AND running_task_id = %s
                        """,
                        (conversation_id, task_id),
                    )
                    cursor.execute(
                        "UPDATE task_delivery_event SET content = NULL WHERE task_id = %s AND content IS NOT NULL",
                        (task_id,),
                    )
                    terminals.append(
                        TerminalTask(
                            task_id=task_id,
                            conversation_id=conversation_id,
                            status="failed",
                            error_kind="delivery_expired",
                        )
                    )
        return terminals

    def clear_delivered_content_for_conversation(self, *, conversation_id: str) -> int:
        """会话边界触发（``/new``、空闲到点、停用/权限变化感知）时清除该会话已
        送达的投递正文；独立开一个事务，供 scheduler 与非 gateway-事务调用方使用。
        `/new` 走的是 ``_Transaction`` 上的同名方法（同一事务），不经过这里。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                return _Transaction(connection).clear_delivered_content_for_conversation(
                    conversation_id=conversation_id
                )

    def clear_delivered_content_for_user(self, *, user_id: str) -> int:
        """停用感知、权限变化感知触发：清除该用户名下全部会话已送达的投递正文。

        这是留给识别停用/权限变化的模块（身份、权限、管理域，均「待建立」）在
        感知到事件那一刻调用的冻结接口；本 Story 不实现那两类事件的探测本身。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    """
                    UPDATE task_delivery_event AS e
                       SET content = NULL
                      FROM task AS t
                     WHERE e.task_id = t.id
                       AND t.user_id = %s
                       AND e.platform_received_at IS NOT NULL
                       AND e.content IS NOT NULL
                    """,
                    (user_id,),
                )
                return cursor.rowcount

    def sweep_idle_conversations(self, *, idle_after: timedelta) -> int:
        """会话空闲满两小时由 scheduler 周期调用：到点主动清除已送达的投递正文，
        不依赖下一次任务入队（2026-08-14 补充决定、`V-投递-10`）。

        只挑选当前空闲（``running_task_id IS NULL``）且仍持有未清正文的会话，
        天然幂等——同一个会话被反复调用不会产生第二次清理副作用或报错。
        返回本轮实际清理的会话数，供 scheduler 写运行日志。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    """
                    SELECT DISTINCT c.id
                      FROM conversation AS c
                      JOIN task AS t ON t.conversation_id = c.id
                      JOIN task_delivery_event AS e
                        ON e.task_id = t.id AND e.event_type = 'terminal'
                     WHERE c.running_task_id IS NULL
                       AND c.last_task_ended_at IS NOT NULL
                       AND c.last_task_ended_at <= now() - %s::interval
                       AND e.platform_received_at IS NOT NULL
                       AND e.content IS NOT NULL
                    """,
                    (idle_after,),
                )
                conversation_ids = [row[0] for row in cursor.fetchall()]
                transaction = _Transaction(connection)
                for conversation_id in conversation_ids:
                    transaction.clear_delivered_content_for_conversation(
                        conversation_id=conversation_id
                    )
                return len(conversation_ids)


class PostgresTaskQueueListener:
    """短生命周期 LISTEN 适配器；服务仍需配合轮询，不能只信 NOTIFY。"""

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        self._dsn = dsn
        self._timeouts = timeouts
        self._connection: Any | None = None

    def __enter__(self) -> "PostgresTaskQueueListener":
        self._connection = connect(self._dsn, timeouts=self._timeouts, autocommit=True)
        self._connection.execute(f"LISTEN {TASK_QUEUED_CHANNEL}")
        return self

    def wait(self, *, timeout_seconds: float) -> bool:
        if self._connection is None:
            raise RuntimeError("监听器尚未进入上下文")
        for _notify in self._connection.notifies(timeout=timeout_seconds, stop_after=1):
            return True
        return False

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
