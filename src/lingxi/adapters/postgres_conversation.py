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
from typing import Any, Iterator

from lingxi.core.conversation.ports import (
    ConversationRecord,
    HandledAs,
    UserRecord,
    UserState,
)
from lingxi.core.ids import new_id

logger = logging.getLogger(__name__)

# 入队后发的通知通道名。worker 监听它以便"提交即领取"，但**不能只靠它**：
# NOTIFY 在连接断开、监听方重启的窗口里会整批丢失，兜底轮询是必需的（`V-队列-05`）。
TASK_QUEUED_CHANNEL = "task_queued"


def _user_state(provisioning_state: str, account_state: str) -> UserState:
    """把 ``app_user`` 的两列映射成管线关心的三态。

    **先判停用再判开通**：一个 ``provisioning_state='active'`` 但
    ``account_state='suspended'`` 的用户必须落进停用分支。反过来写的话，被停用的
    老用户会被当成正常用户继续排任务——合同「已停用用户不能继续使用 Lingxi」。
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
    ) -> None:
        self._execute(
            """
            INSERT INTO task
                (id, conversation_id, user_id, inbound_event_id, prompt,
                 resumed_session, target_worker_version, content_expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            """,
            (
                task_id,
                conversation_id,
                user_id,
                inbound_event_id,
                prompt,
                resumed_session,
                target_worker_version,
            ),
        )

    def clear_agent_session(self, *, conversation_id: str) -> None:
        """``/new``：清空当前对话上下文。

        条件只有 ``id = %s``，因此天然只影响这一行，其他话题的 ``agent_session_id``
        与 ``last_task_ended_at`` 都不动（`V-会话-05`）。
        """

        self._execute(
            "UPDATE conversation SET agent_session_id = NULL WHERE id = %s",
            (conversation_id,),
        )

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


class PostgresGatewayStore:
    """实现 ``core.conversation.ports.GatewayStore``。"""

    def __init__(self, dsn: str) -> None:
        import psycopg

        self._psycopg = psycopg
        self._dsn = dsn

    @contextmanager
    def transaction(self) -> Iterator[_Transaction]:
        """一个连接、一个事务。异常时整体回滚。"""

        with self._psycopg.connect(self._dsn) as connection:
            with connection.transaction():
                yield _Transaction(connection)


@dataclass(frozen=True)
class ClaimedTask:
    task_id: str
    conversation_id: str
    user_id: str
    prompt: str
    resumed_session: bool
    target_worker_version: str
    attempts: int


class PostgresTaskQueue:
    """worker 与 scheduler 侧的队列操作。

    本切片只交付队列**基座**：领取、释放、回收。真正跑 Agent 会话属 S4 下半。
    """

    def __init__(self, dsn: str) -> None:
        import psycopg

        self._psycopg = psycopg
        self._dsn = dsn

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

        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
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
                      ORDER BY scheduled_at
                        FOR UPDATE SKIP LOCKED
                      LIMIT %s
                 )
                RETURNING id, conversation_id, user_id, prompt,
                          resumed_session, target_worker_version, attempts
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
                )
                for row in cursor.fetchall()
            ]

    def finish(
        self, *, task_id: str, conversation_id: str, status: str, worker_id: str
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

        with self._psycopg.connect(self._dsn) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    """
                    UPDATE task SET status = %s, ended_at = now()
                     WHERE id = %s AND worker_id = %s AND status = 'running'
                    """,
                    (status, task_id, worker_id),
                )
                if cursor.rowcount != 1:
                    # 不是这一代执行者（或任务早已结束）：什么都不改，也不释放话题。
                    return False
                cursor.execute(
                    """
                    UPDATE conversation
                       SET running_task_id = NULL, last_task_ended_at = now()
                     WHERE id = %s AND running_task_id = %s
                    """,
                    (conversation_id, task_id),
                )
                return cursor.rowcount == 1

    def reclaim_stale(self, *, older_than: timedelta) -> list[str]:
        """scheduler 回收心跳超时的 running 任务，重置为 ``queued``。

        同样**不碰 ``target_worker_version``**：任务被回收重排后，用户仍然进入他当初
        被分到的那个版本（`V-灰度-01` 的回收路径）。
        """

        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE task SET status = 'queued',
                                worker_id = NULL,
                                started_at = NULL,
                                heartbeat_at = NULL
                 WHERE status = 'running'
                   AND heartbeat_at < now() - %s::interval
                RETURNING id
                """,
                (older_than,),
            )
            return [row[0] for row in cursor.fetchall()]
