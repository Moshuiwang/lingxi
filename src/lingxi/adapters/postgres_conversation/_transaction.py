"""入站事件幂等去重、会话与话题的单事务写操作（Issue #239 从 ``postgres_conversation.py``
按读写边界拆分而来）。

**事务边界是这一层最重要的部分。** ``_Transaction`` 交出的对象上才有写方法，拿不到
"事务外顺手写一条"的入口——`V-队列-01` 要求 ``inbound_event`` 插入、``conversation``
抢占、``task`` 插入落在同一事务里，这条约束由类型形状承担，不靠调用方记得。事务的
唯一开启入口是 ``_gateway_store.PostgresGatewayStore.transaction()``。
"""

from __future__ import annotations

from typing import Any

from lingxi.core.conversation.ports import (
    ConversationRecord,
    HandledAs,
    UserRecord,
    UserState,
)
from lingxi.core.ids import new_id

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
