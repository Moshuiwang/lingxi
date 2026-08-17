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
    PendingOnboarding,
    UserRecord,
    UserState,
)
from lingxi.config.content import ContentCatalog, default_content_catalog
from lingxi.core.delivery.ports import DeliveryEventType, TerminalKind, resolve_delivered_outcome
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
            WITH target AS (
                SELECT id, user_id, agent_session_id AS previous_session_id
                  FROM conversation
                 WHERE id = %s AND running_task_id IS NULL
                 FOR UPDATE
            )
            UPDATE conversation AS c
               SET agent_session_id = NULL
              FROM target
             WHERE c.id = target.id
            RETURNING target.user_id, target.previous_session_id
            """,
            (conversation_id,),
        )
        row = cursor.fetchone()
        cleared = row is not None
        if cleared:
            # /new 同一触发点同时清除该会话已保留的安全结果正文（产品合同「数据
            # 保留与删除」第一条、数据库设计「问数结果投递事件与会话保留 Outbox」）；
            # 与上面的会话上下文重置同一事务提交或回滚，不逐位置单独判断或延后。
            self.clear_delivered_content_for_conversation(conversation_id=conversation_id)
            # target.previous_session_id 取的是本次 UPDATE 之前的旧值（CTE 在
            # 更新发生前就已求值并被 FOR UPDATE 锁定），不是清空后的 NULL——
            # Agent 会话 JSONL 的物理清理（Issue #153）按这个旧值排队。
            user_id, old_session_id = row
            if old_session_id:
                self._queue_session_cleanup(
                    user_id=user_id, agent_session_id=old_session_id, reason="new_command"
                )
        return cleared

    def _queue_session_cleanup(self, *, user_id: str, agent_session_id: str, reason: str) -> None:
        """登记一条 Agent 会话 JSONL 物理清理请求（Issue #153）。

        只登记"哪个 session id 不会再被 resume 了"，不在这里碰文件系统——本类的
        调用方可能是 Gateway 或 scheduler 的事务，两者都没有挂载用户环境目录。真正
        的物理删除延后到 Worker 的周期性收口（唯一挂载了该卷的常驻进程）。
        ``ON CONFLICT DO NOTHING``：同一个 session id 只需要排队一次，重复触发
        （例如 /new 与空闲到点扫描撞在同一时刻）不产生第二条待办。
        """

        self._execute(
            """
            INSERT INTO agent_session_cleanup (id, user_id, agent_session_id, reason)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (agent_session_id) DO NOTHING
            """,
            (new_id("asc"), user_id, agent_session_id, reason),
        )

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

    def consume_delivery_expired_notice(self, *, conversation_id: str) -> bool:
        """本话题是否有尚未提示过的「投递已过期」任务；命中即原子标记为已提示。

        `V-投递-06` 后半句：正文因二十四小时未投递而到期后，只在用户下一条主动消息
        上提示一次「请重新提问」，不主动推送。这里只找**尚未提示过**的
        ``delivery_expired`` 行并原子置位，因此同一次到期只提示一次；调用方决定
        提示文案，本方法只回答"要不要提示"。查询与标记落在同一条 ``UPDATE``，
        与调用方所在的入站消息事务一起提交或回滚，不需要额外的读锁。
        """

        cursor = self._execute(
            """
            UPDATE task SET delivery_expired_notice_sent_at = now()
             WHERE id = (
                 SELECT id FROM task
                  WHERE conversation_id = %s AND error_kind = 'delivery_expired'
                    AND delivery_expired_notice_sent_at IS NULL
                  ORDER BY ended_at DESC NULLS LAST, created_at DESC
                  LIMIT 1
             )
            RETURNING id
            """,
            (conversation_id,),
        )
        return cursor.fetchone() is not None


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

    def mark_onboarding_dispatched(self, *, event_id: str) -> None:
        """记下"这条事件已经交给开通编排了"（迁移 0062、Issue #65 轻审 P2-2）。

        条件里带上 ``onboarding_dispatched_at IS NULL``：真正的交接时刻是第一次，
        重复调用不把时间戳往后推——那个时间戳是"账上什么时候平的"的唯一证据。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    """
                    UPDATE inbound_event
                       SET onboarding_dispatched_at = now()
                     WHERE feishu_event_id = %s
                       AND onboarding_dispatched_at IS NULL
                    """,
                    (event_id,),
                )

    def claim_stale_onboarding(self, *, older_than: timedelta) -> PendingOnboarding | None:
        """认领一条超时仍未交接的未开通首聊事件，并原子记账。

        ``FOR UPDATE SKIP LOCKED`` + ``onboarding_dispatched_at IS NULL`` 两道条件
        一起保证多实例扫描时同一条只被一个实例拿走：跳过被别人锁住的行，拿到锁之后
        再确认它还没被记账。少了后半句，两个实例可以先后拿到同一行并各触发一次开通。

        ``user_open_id`` 理论上可空（列本身允许 NULL），这里显式排除：交给编排的
        三元组缺了它就没有任何可匹配的身份，取回来也只能立刻失败。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    """
                    UPDATE inbound_event
                       SET onboarding_dispatched_at = now()
                     WHERE feishu_event_id IN (
                               SELECT feishu_event_id
                                 FROM inbound_event
                                WHERE handled_as = 'auto_provisioning'
                                  AND onboarding_dispatched_at IS NULL
                                  AND user_open_id IS NOT NULL
                                  AND received_at < now() - %s::interval
                                ORDER BY received_at
                                LIMIT 1
                                  FOR UPDATE SKIP LOCKED
                           )
                       AND onboarding_dispatched_at IS NULL
                    RETURNING feishu_event_id, user_open_id, trace_id
                    """,
                    (older_than,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return PendingOnboarding(event_id=row[0], open_id=row[1], trace_id=row[2])


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


@dataclass(frozen=True)
class DeliveryEventRecord:
    """Gateway 消费循环读回的一条 outbox 事件（Issue #152）。"""

    sequence: int
    event_type: str
    terminal_kind: str | None
    content: str | None
    elapsed_seconds: int | None


@dataclass(frozen=True)
class PendingDeliveryTask:
    """Gateway 一轮消费的候选任务：既带上投递目的地，也带上它自己的消费进度。

    ``card_id``/``card_seq``/``message_id``/``fallback_text`` 是上一轮（或崩溃前）
    持久化的进度，供 :class:`~lingxi.core.execution.card_stream.CardStream` 以
    ``initial_*`` 恢复，不从零建卡（`V-卡片-01`、状态合同第 7 条）。
    """

    task_id: str
    conversation_id: str
    chat_id: str
    thread_id: str | None
    reply_to_message_id: str | None
    status: str
    card_id: str | None
    card_seq: int
    message_id: str | None
    fallback_text: bool
    consumed_sequence: int


@dataclass(frozen=True)
class UncertainDeliveryTask:
    """外发前预留位没有被清空、原因不明的任务（Issue #151 审核 P3-6）。

    只用于告警与人工核对；消费循环不会自动处理这类任务，见
    :meth:`PostgresTaskQueue.reserve_dispatch` 的说明。
    """

    task_id: str
    reserved_kind: str


@dataclass(frozen=True)
class SessionCleanupTask:
    """一条待物理清理的 Agent 会话 JSONL 请求（Issue #153）。"""

    id: str
    user_id: str
    agent_session_id: str
    reason: str


# ``sweep_idle_conversations`` 每次扫描新增排队的会话数上限（PR #173 独立复核
# P2-5）：查询本身已经用 NOT EXISTS 把候选集收窄到"尚未排过队"的那些，这里只是
# 给单次扫描一个防护上限，避免一次异常的大批量话题同时到点时单条查询/单次事务
# 处理过多行。500 是与 Worker 侧消费能力（`claim_session_cleanups` 默认单批 20、
# 每约 2 秒的收口轮询一次）比较后留出的宽裕量，不是精确调校值。
_IDLE_SESSION_CLEANUP_SWEEP_LIMIT = 500

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


class PostgresTaskQueue:
    """worker 与 scheduler 侧的队列操作。

    worker 与 scheduler 共用这些原子操作。所有连接都经过仓库唯一的 PostgreSQL
    factory；没有连接字符串或 psycopg 直连旁路。
    """

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
                conversation_row = cursor.fetchone()
                if conversation_row is None:
                    # 上面两条写入（事件确认、task 收敛）已经在这个事务里执行，
                    # 但还没提交：整个 with 块以异常退出，psycopg 回滚整个事务，
                    # 三条写入因此要么全部不生效。宁可响亮失败也不要悄悄丢弃
                    # agent_session_id、或者用一个和"没有可确认的东西"含义相同
                    # 的 False 掩盖"已经确认到一半"的竞态（内审 P2-4）。
                    raise RuntimeError(
                        f"任务 {task_id} 确认送达时 conversation {conversation_id} "
                        "的话题占用状态发生了竞态"
                    )
                self._queue_overwritten_session(
                    cursor,
                    user_id=conversation_row[0],
                    previous_session_id=conversation_row[1],
                    new_session_id=agent_session_id,
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
        """停用感知、权限变化感知触发：清除该用户名下全部会话已送达的投递正文，
        并使该用户全部会话的当前 Agent 会话失效、排队物理清理其 JSONL（Issue #153）。

        这是留给识别停用/权限变化的模块（身份、权限、管理域，均「待建立」）在
        感知到事件那一刻调用的冻结接口；本 Story 不实现那两类事件的探测本身。

        与 ``/new``、空闲到点两类触发不同，停用/权限变化是**硬失效**：即使这个
        会话本来还没到两小时空闲阈值，也不该在下一次消息到来时被 resume——因此
        这里显式把 ``conversation.agent_session_id`` 置空（另外两类触发要么本来
        就在清空它，要么依赖既有的两小时时间戳比较，不需要在这里提前置空）。
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
                cleared_events = cursor.rowcount

                # 与 _Transaction.clear_agent_session 同一手法：RETURNING 反映的是
                # UPDATE 之后的行，直接 RETURNING agent_session_id 只会拿到刚写入的
                # NULL。用 CTE 在置空前先锁定并读出旧值（实测：改前的写法会让
                # _queue_session_cleanup 拿到 None，撞上 agent_session_id 的
                # NOT NULL 约束——见本方法对应的真库负向用例）。
                cursor.execute(
                    """
                    WITH targets AS (
                        SELECT id, agent_session_id AS previous_session_id
                          FROM conversation
                         WHERE user_id = %s AND agent_session_id IS NOT NULL
                         FOR UPDATE
                    )
                    UPDATE conversation AS c
                       SET agent_session_id = NULL
                      FROM targets
                     WHERE c.id = targets.id
                    RETURNING targets.previous_session_id
                    """,
                    (user_id,),
                )
                retired_sessions = [row[0] for row in cursor.fetchall()]
                transaction = _Transaction(connection)
                for agent_session_id in retired_sessions:
                    transaction._queue_session_cleanup(
                        user_id=user_id,
                        agent_session_id=agent_session_id,
                        reason="user_cleared",
                    )
                return cleared_events

    def sweep_idle_conversations(self, *, idle_after: timedelta) -> int:
        """会话空闲满两小时由 scheduler 周期调用：到点主动清除已送达的投递正文，
        不依赖下一次任务入队（2026-08-14 补充决定、`V-投递-10`）；同一轮里，凡是
        到点且仍持有当前 Agent 会话的话题，还会排队物理清理其 JSONL（Issue #153）。

        两类到点动作的候选集**不同**，因此分两条查询、不能合并：投递正文清理只挑
        「仍持有未清正文」的会话（没有正文可清的会话不必碰）；Agent 会话物理清理
        只看「话题两小时规则本身已经到点、且当前仍有一个不会再被 resume 的
        ``agent_session_id``」——与这个会话有没有投递正文无关（架构设计 5.2 节：
        下一次任务领取时会因为超过两小时阈值而不带 ``resume``，这个 session 从那一刻
        起就已经是孤儿，不需要等它同时"有正文可清"才处理）。这里**不**把
        ``conversation.agent_session_id`` 置空——是否 ``resume`` 仍然只由领取任务时
        的时间戳比较决定（`V-会话-04`），本方法只负责让物理文件不在数据库判定之外
        继续占着磁盘。

        天然幂等：清正文与排队清理都只在满足各自条件时才发生，重复调用不产生第二次
        副作用（`agent_session_cleanup` 的 `agent_session_id` 唯一索引兜底去重）。
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

                cursor.execute(
                    """
                    SELECT c.user_id, c.agent_session_id
                      FROM conversation AS c
                     WHERE c.running_task_id IS NULL
                       AND c.agent_session_id IS NOT NULL
                       AND c.last_task_ended_at IS NOT NULL
                       AND c.last_task_ended_at <= now() - %s::interval
                       -- PR #173 独立复核 P2-5：这条查询刻意不清空
                       -- agent_session_id（取舍本身是对的，见上方文档），于是每个
                       -- 曾经用过会话、之后闲置的话题会永久留在候选集里；不加这条
                       -- NOT EXISTS，每 60 秒的扫描会把候选集整个重新捞出来，对
                       -- 早就已经排过队的会话再跑一次纯浪费的
                       -- INSERT ... ON CONFLICT DO NOTHING。迁移 0061 头部注释
                       -- 写的就是这个去重谓词，之前代码里没有落地。
                       AND NOT EXISTS (
                           SELECT 1 FROM agent_session_cleanup AS a
                            WHERE a.agent_session_id = c.agent_session_id
                       )
                     LIMIT %s
                    """,
                    (idle_after, _IDLE_SESSION_CLEANUP_SWEEP_LIMIT),
                )
                for user_id, agent_session_id in cursor.fetchall():
                    transaction._queue_session_cleanup(
                        user_id=user_id, agent_session_id=agent_session_id, reason="idle_timeout"
                    )
                return len(conversation_ids)

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

    # -----------------------------------------------------------------
    # Gateway 投递消费（Issue #152）
    #
    # 这一组方法服务的是「读 outbox、驱动 CardKit/文本、记消费进度」这一条 Gateway
    # 侧的独立读写路径，与上面 Worker 侧写 outbox 的那组方法各自独立提交、不共用
    # 事务——两者本来就是不同进程。持久化的进度字段（``delivery_consumed_sequence``/
    # ``card_id``/``card_seq``/``delivery_message_id``/``fallback_text``/
    # ``dispatch_reserved_kind``）见迁移 0060 头部注释。
    # -----------------------------------------------------------------

    def list_pending_delivery_tasks(self, *, limit: int = 20) -> list[PendingDeliveryTask]:
        """列出本轮需要处理的任务：还有未消费的 outbox 事件，或已经拿到
        ``delivery_message_id`` 但尚未确认送达（``confirm_delivery`` 上一次失败或
        还没调用）。**不含**``dispatch_reserved_kind`` 非空的任务——那些是崩溃恢复后
        outcome 不明的任务，必须被上层单独识别为 ``uncertain``，不能混进正常消费。

        只读查询，不加锁：外发前预留位（``reserve_dispatch``）才是真正的并发互斥点，
        这里允许多个候选同时被读到，抢占失败的一方在预留时自然让路。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT t.id, t.conversation_id, c.feishu_chat_id, c.feishu_thread_id,
                       t.reply_to_message_id, t.status, t.card_id, t.card_seq,
                       t.delivery_message_id, t.fallback_text, t.delivery_consumed_sequence
                  FROM task AS t
                  JOIN conversation AS c ON c.id = t.conversation_id
                 WHERE t.status IN ('running', 'awaiting_delivery')
                   AND t.dispatch_reserved_kind IS NULL
                   AND (
                        EXISTS (
                            SELECT 1 FROM task_delivery_event AS e
                             WHERE e.task_id = t.id AND e.sequence > t.delivery_consumed_sequence
                        )
                        OR (t.status = 'awaiting_delivery' AND t.delivery_message_id IS NOT NULL)
                   )
                 ORDER BY t.created_at
                 LIMIT %s
                """,
                (limit,),
            )
            return [
                PendingDeliveryTask(
                    task_id=row[0],
                    conversation_id=row[1],
                    chat_id=row[2],
                    thread_id=row[3],
                    reply_to_message_id=row[4],
                    status=row[5],
                    card_id=row[6],
                    card_seq=row[7],
                    message_id=row[8],
                    fallback_text=row[9],
                    consumed_sequence=row[10],
                )
                for row in cursor.fetchall()
            ]

    def list_uncertain_delivery_tasks(self, *, limit: int = 50) -> list[UncertainDeliveryTask]:
        """列出外发前预留位卡住的任务，供告警。见 ``reserve_dispatch`` 的说明。

        ``status IN ('running', 'awaiting_delivery')``（独立审核 P2-4）：
        ``expire_undelivered_terminals`` 的二十四小时强制收敛不读、也不清
        ``dispatch_reserved_kind``——它只管把业务结论收敛为 ``failed``，预留位
        字段本身是 Gateway 消费循环私有的运行时簿记。没有这条过滤，一个卡在
        预留位里、后来被到期路径收敛为 ``failed`` 的任务会在 ``dispatch_reserved_kind``
        永远不被清空的情况下被这里永远查出来，按默认 1 秒轮询造成不会停止的
        告警——即使任务本身早已经不再需要任何人处理。任务到期收敛之后这个字段
        本身也不再有意义（不会再被任何投递路径读取或写入）。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, dispatch_reserved_kind FROM task
                 WHERE dispatch_reserved_kind IS NOT NULL
                   AND status IN ('running', 'awaiting_delivery')
                 ORDER BY created_at
                 LIMIT %s
                """,
                (limit,),
            )
            return [
                UncertainDeliveryTask(task_id=row[0], reserved_kind=row[1])
                for row in cursor.fetchall()
            ]

    def read_delivery_events(
        self, *, task_id: str, after_sequence: int
    ) -> list[DeliveryEventRecord]:
        """按序号升序读回一个任务尚未消费的 outbox 事件。"""

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT sequence, event_type, terminal_kind, content, elapsed_seconds
                  FROM task_delivery_event
                 WHERE task_id = %s AND sequence > %s
                 ORDER BY sequence
                """,
                (task_id, after_sequence),
            )
            return [
                DeliveryEventRecord(
                    sequence=row[0],
                    event_type=row[1],
                    terminal_kind=row[2],
                    content=row[3],
                    elapsed_seconds=row[4],
                )
                for row in cursor.fetchall()
            ]

    def reserve_dispatch(self, *, task_id: str, kind: str) -> bool:
        """在一次结果不可事后消歧的外发调用（建卡、终态卡片更新+关闭、文本兜底
        发送）之前提交预留位。三者共同点是：一旦崩溃重启，消费循环单靠"重放同一次
        调用"本身无法安全判断上一次是否已经外发成功——建卡与文本发送没有飞书原生
        幂等键；终态卡片更新+关闭虽然有整卡级 ``sequence`` 天然拒绝重复帧，但拒绝
        本身只保证不出现第二次可见卡片帧，不保证调用方的错误处理不会把这次拒绝
        误判成"卡片链路整体失败"进而降级到文本兜底、造成跨通道的重复投递（迁移
        0060 头部注释）。

        返回 ``False`` 表示没能预留到——任务已经不在可处理状态，或已经被预留
        （正常情况下同一时刻只有一个消费者处理同一任务，命中说明上一轮处理到一半
        就中断了；调用方此时不应该继续外发，而应把这个任务视为 ``uncertain``）。
        预留成功即**独立提交**：这条写入必须在真正发起外部调用之前落盘可见，
        否则"进程崩溃后能不能从数据库看出上一次是否已经外发"这件事无从谈起。
        """

        if kind not in ("card_create", "card_finish", "text_send"):
            raise ValueError("dispatch_reserved_kind 只能是 card_create、card_finish 或 text_send")
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE task SET dispatch_reserved_kind = %s
                 WHERE id = %s AND dispatch_reserved_kind IS NULL
                   AND status IN ('running', 'awaiting_delivery')
                """,
                (kind, task_id),
            )
            return cursor.rowcount == 1

    def clear_dispatch_reservation(self, *, task_id: str) -> None:
        """外发调用**同步捕获到明确失败**（不是进程崩溃）时清空预留位。

        明确失败与进程崩溃的区别决定下一轮的行为：明确失败允许下一轮重试同一次
        外发（游标不会被推进过这个事件）；进程崩溃则让预留位原样留在数据库里，
        由 ``list_pending_delivery_tasks`` 的过滤条件与 ``list_uncertain_delivery_tasks``
        把它路由到人工核对，不自动重发（Issue #151 审核 P3-6、状态合同第 6 条）。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE task SET dispatch_reserved_kind = NULL WHERE id = %s",
                (task_id,),
            )

    def record_delivery_progress(
        self,
        *,
        task_id: str,
        consumed_sequence: int,
        card_id: str | None = None,
        message_id: str | None = None,
        card_sequence: int | None = None,
        fallback_text: bool = False,
    ) -> None:
        """把一次已经明确知道结果的外发进度写回。总是清空预留位——调用这个方法本身
        就意味着调用方已经拿到了确定的结果（成功，或同步捕获的失败）。

        ``card_id``/``message_id``/``card_sequence`` 用 ``COALESCE`` 只增不减：
        一旦写入就不会被后续调用误置回 ``NULL``；``consumed_sequence`` 用
        ``GREATEST`` 防止乱序调用把游标往回拨；``fallback_text`` 一旦置真就不会
        被置回假（`V-卡片-03`：首次失败后永久走文本通道）。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE task
                   SET delivery_consumed_sequence = GREATEST(delivery_consumed_sequence, %s),
                       card_id = COALESCE(%s, card_id),
                       delivery_message_id = COALESCE(%s, delivery_message_id),
                       card_seq = COALESCE(%s, card_seq),
                       fallback_text = fallback_text OR %s,
                       dispatch_reserved_kind = NULL
                 WHERE id = %s
                """,
                (
                    consumed_sequence,
                    card_id,
                    message_id,
                    card_sequence,
                    fallback_text,
                    task_id,
                ),
            )


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
