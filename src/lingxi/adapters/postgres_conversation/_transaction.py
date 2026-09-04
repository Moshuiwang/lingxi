"""入站事件幂等去重、会话与话题的单事务写操作。

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
    PendingPreprovisionNotice,
    UserRecord,
    UserState,
)
from lingxi.core.ids import new_id
from lingxi.core.user_memory import MAX_MEMORY_ENTRIES_PER_USER, UserMemoryEntry

# 入队后发的通知通道名。worker 监听它以便"提交即领取"，但**不能只靠它**：
# NOTIFY 在连接断开、监听方重启的窗口里会整批丢失，兜底轮询是必需的（`V-队列-05`）。
TASK_QUEUED_CHANNEL = "task_queued"


#: 开通已经启动、还没收口的两格。``matching`` 不在里面：那是建档默认值，编排此刻可能
#: 还没被认领，用户应当继续看到「已收到，正在核对」。
_PROVISIONING_IN_FLIGHT = frozenset({"provisioning", "mcp_syncing"})


def _user_state(provisioning_state: str, account_state: str) -> UserState:
    """把 ``app_user`` 的两列映射成管线关心的三态。

    **先判停用再判开通。** 两个判断只在一种输入上分歧：一个既未开通、又已停用的
    用户——本实现判他 ``SUSPENDED``，反过来写会判 ``NOT_PROVISIONED``。选停用是
    因为两条分支的后果不同：未开通分支会去启动自动匹配与开通，而这个人的账号已经
    被管理员停掉了，替他开通是把管理员刚撤销的东西又装回去；停用分支只回一句
    说明，是两者中可逆的那个。
    """
    if account_state != "enabled":
        return UserState.SUSPENDED
    if provisioning_state in _PROVISIONING_IN_FLIGHT:
        # 开通已经启动、还没收口：合同对这个阶段规定的提示与「还没开始核对」不是同一条
        # （见 ``UserState.PROVISIONING``）。
        return UserState.PROVISIONING
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
            SELECT c.id, c.agent_session_id, c.last_task_ended_at, c.running_task_id,
                   t.status
              FROM conversation AS c
              LEFT JOIN task AS t ON t.id = c.running_task_id
             WHERE c.user_id = %s AND c.feishu_chat_id = %s
               AND COALESCE(c.feishu_thread_id, '') = COALESCE(%s, '')
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
            # `LEFT JOIN`：`running_task_id` 为 NULL 时 `t.status` 天然也是
            # NULL，不需要额外判空；忙碌提示"排队中/处理中"如实区分的唯一数据来源。
            running_task_status=row[4],
        )

    def claim_conversation(self, *, conversation_id: str, task_id: str) -> bool:
        """条件更新抢占话题。

        ``AND running_task_id IS NULL`` 不能省：省掉之后第二个任务会直接覆盖第一个，
        两个任务在同一话题上并行。
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
            # Agent 会话 JSONL 的物理清理按这个旧值排队。
            user_id, old_session_id = row
            if old_session_id:
                self._queue_session_cleanup(
                    user_id=user_id, agent_session_id=old_session_id, reason="new_command"
                )
        return cleared

    def discard_stale_agent_session(self, *, conversation_id: str) -> None:
        """入队判定「不续用」时，把已判废的 ``agent_session_id`` 置空并排队物理清理。

        与 ``clear_agent_session``（/new）的三点差异：调用点在**已抢占成功**的入队
        事务里（``running_task_id`` 是当前任务），不做忙碌判定；不清除已送达正文
        ——数据保留边界仍由 /new 与空闲到点扫描负责；清理原因复用 ``idle_timeout``
        （触发类别就是空闲超窗，只是在入队时观测到而不是被扫描发现，且 0061 的
        CHECK 约束不含新值）。CTE 先锁定并读出旧值再置空，理由同
        ``clear_agent_session``：直接 RETURNING 只会拿到刚写入的 NULL。
        """
        cursor = self._execute(
            """
            WITH target AS (
                SELECT id, user_id, agent_session_id AS stale_session_id
                  FROM conversation
                 WHERE id = %s AND agent_session_id IS NOT NULL
                 FOR UPDATE
            )
            UPDATE conversation AS c
               SET agent_session_id = NULL
              FROM target
             WHERE c.id = target.id
            RETURNING target.user_id, target.stale_session_id
            """,
            (conversation_id,),
        )
        row = cursor.fetchone()
        if row is not None:
            user_id, stale_session_id = row
            self._queue_session_cleanup(
                user_id=user_id, agent_session_id=stale_session_id, reason="idle_timeout"
            )

    def _queue_session_cleanup(self, *, user_id: str, agent_session_id: str, reason: str) -> None:
        """登记一条 Agent 会话 JSONL 物理清理请求。

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

    def clear_delivered_content_for_user(
        self, *, user_id: str, reason: str = "user_cleared"
    ) -> int:
        """停用感知、权限变化感知触发：清除该用户名下全部会话已送达的投递正文。

        并使当前 Agent 会话失效、排队物理清理其 JSONL。运行在**调用方已经开启
        的事务**里，不建连接不提交不回滚（清理须与账号/权限决定同一事务）；没
        有独立事务的调用方改用 ``_queue_outbox._OutboxMixin`` 同名方法。**硬
        失效**触发，不等两小时空闲阈值。加锁顺序固定**先锁全部 conversation
        （按 id 升序）、再逐个清 tde**，与 ``/new``、空闲到点扫描同一顺序，避免
        交叉持锁死锁（并发回归见
        ``test_purge_and_new_command_never_deadlock_across_twenty_rounds``）。
        """
        # 一次性锁定该用户名下全部会话（按 id 升序），同时把 agent_session_id 现值
        # 一并读出——两次 FOR UPDATE 打在同一批行上纯属浪费，这一条 SELECT 之后
        # 全部目标行已经锁到本事务提交为止，后面的 UPDATE 不需要再 FOR UPDATE 一次。
        cursor = self._execute(
            "SELECT id, agent_session_id FROM conversation WHERE user_id = %s ORDER BY id FOR UPDATE",
            (user_id,),
        )
        rows = cursor.fetchall()
        conversation_ids = [row[0] for row in rows]
        # RETURNING 反映的是 UPDATE 之后的行，直接对 UPDATE 语句 RETURNING
        # agent_session_id 只会拿到刚写入的 NULL——旧值必须在这里、UPDATE 之前，
        # 从已经锁定的行本身读出（与 clear_agent_session 的 CTE 手法同一目的，
        # 只是这里锁已经在上一条语句里拿到，不需要再借 CTE 现场取）。
        sessions_to_retire = [(row[0], row[1]) for row in rows if row[1] is not None]

        cleared_events = 0
        for conversation_id in conversation_ids:
            cleared_events += self.clear_delivered_content_for_conversation(
                conversation_id=conversation_id
            )

        if sessions_to_retire:
            self._execute(
                "UPDATE conversation SET agent_session_id = NULL"
                " WHERE user_id = %s AND agent_session_id IS NOT NULL",
                (user_id,),
            )
            for _conversation_id, agent_session_id in sessions_to_retire:
                self._queue_session_cleanup(
                    user_id=user_id, agent_session_id=agent_session_id, reason=reason
                )
        return cleared_events

    def clear_delivered_content_for_conversation(self, *, conversation_id: str) -> int:
        """把已确认送达（``platform_received``）但仍随会话保留的投递正文清空。

        只清 ``content``：``event_type``/``sequence``/``terminal_kind``/
        ``error_kind`` 等低敏事实按状态合同保留最长九十天，供审计与到期
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

    def peek_preprovision_notice(self, *, user_id: str) -> PendingPreprovisionNotice | None:
        """这个人有没有一句"你已经被提前开通了"还没说；有则只读返回渲染所需的权限快照。

        **不消费**（合同与调用次序见
        ``core/conversation/ports.GatewayTransaction.peek_preprovision_notice``）。
        权限快照的读取姿势与 ``postgres_late_readiness_recovery`` 的候选查询逐字同一
        套判据：``status = 'published'``、``payload ? 'permissions'``（九十天保留期
        会把 payload 擦成 ``'{}'``）、``permission_version`` 与 ``app_user`` 当前版本
        对齐；``UNIQUE (user_id, permission_version)``（迁移 ``0064``）保证至多一行，
        LEFT JOIN 落空时挂起仍然成立、快照为 ``None``，渲染失败与否交调用方裁量。
        """
        cursor = self._execute(
            """
            SELECT o.payload ->> 'permissions'
              FROM app_user u
              LEFT JOIN publish_outbox o
                ON o.user_id = u.id
               AND o.permission_version = u.permission_version
               AND o.status = 'published'
               AND o.payload ? 'permissions'
             WHERE u.id = %s
               AND u.preprovision_notice_armed_at IS NOT NULL
               AND u.preprovision_notice_sent_at IS NULL
            """,
            (user_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return PendingPreprovisionNotice(permissions=row[0])

    def consume_preprovision_notice(self, *, user_id: str) -> bool:
        """把"你已经被提前开通了"那句一次性提示原子标记为已提示。

        合同见 ``core/conversation/ports.ConversationTransaction.consume_preprovision_
        notice``。挂起（``preprovision_notice_armed_at``）由预开通链在静默完成时写下，
        本方法只把它消费成 ``preprovision_notice_sent_at``——两列而不是一个布尔的理由
        见迁移 ``0087``：一个布尔分不清"从来没挂起过"与"挂起过、已经提示掉了"，同一份
        名单重跑会把已经提示过的人重新挂起。调用次序（先渲染成功、后消费）见
        :meth:`peek_preprovision_notice`。
        """
        cursor = self._execute(
            """
            UPDATE app_user SET preprovision_notice_sent_at = now()
             WHERE id = %s
               AND preprovision_notice_armed_at IS NOT NULL
               AND preprovision_notice_sent_at IS NULL
            RETURNING id
            """,
            (user_id,),
        )
        return cursor.fetchone() is not None

    # 用户记忆：四个方法服务两条调用路径——``/memory`` 命令面（复用当次事件事务的
    # ``tx``）与两处清除钩子（停用、权限真变分支，用调用方已经持有行锁的
    # ``connection`` 直接调用 ``clear_user_memory``），与
    # ``clear_delivered_content_for_user`` 同一姿态：清除与账号/权限决定同一事务。

    def list_user_memory(self, *, user_id: str) -> list[UserMemoryEntry]:
        """按用户取全部记忆，按登记时间升序。

        ``/memory list`` 与 worker 注入共用的天然读路径。
        """
        cursor = self._execute(
            """
            SELECT id, memory_type, memory_key, memory_value, created_at
              FROM user_memory
             WHERE user_id = %s
             ORDER BY created_at ASC
            """,
            (user_id,),
        )
        return [
            UserMemoryEntry(
                memory_id=row[0],
                memory_type=row[1],
                memory_key=row[2],
                memory_value=row[3],
                created_at=row[4],
            )
            for row in cursor.fetchall()
        ]

    def remember_user_memory(
        self, *, user_id: str, memory_type: str, memory_key: str, memory_value: str
    ) -> str | None:
        """登记一条记忆；同一用户同一类型同一 key 已存在时是更新，否则是新增。

        已存在时更新不计入上限、不产生第二行。返回新增/更新后那一行的内部
        标识；**新增**时若该用户已达
        :data:`~lingxi.core.user_memory.MAX_MEMORY_ENTRIES_PER_USER` 条上限，
        返回 ``None`` 且不写入任何行，调用方据此渲染"已达上限"的拒绝文案、不做
        静默截断。``SELECT ... FOR UPDATE`` 锁定目标 key 那一行，让"是否超过
        上限"这条业务判断能在写入前拿到确定的计数（不同 key 之间的计数竞态是
        已知的从紧简化）。
        """
        cursor = self._execute(
            """
            SELECT id FROM user_memory
             WHERE user_id = %s AND memory_type = %s AND memory_key = %s
             FOR UPDATE
            """,
            (user_id, memory_type, memory_key),
        )
        existing = cursor.fetchone()
        if existing is not None:
            memory_id = existing[0]
            self._execute(
                "UPDATE user_memory SET memory_value = %s, updated_at = now() WHERE id = %s",
                (memory_value, memory_id),
            )
            return memory_id

        count_cursor = self._execute(
            "SELECT count(*) FROM user_memory WHERE user_id = %s", (user_id,)
        )
        (count,) = count_cursor.fetchone()
        if count >= MAX_MEMORY_ENTRIES_PER_USER:
            return None

        memory_id = new_id("mem")
        self._execute(
            """
            INSERT INTO user_memory (id, user_id, memory_type, memory_key, memory_value)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (memory_id, user_id, memory_type, memory_key, memory_value),
        )
        return memory_id

    def forget_user_memory(self, *, user_id: str, memory_id: str) -> UserMemoryEntry | None:
        """删除**属于该用户**的一条记忆；返回被删除那一行的内容，未删除时返回 ``None``。

        ``AND user_id = %s`` 结构性地堵死跨用户删除，不需要先查一次"这条记忆是
        不是我的"再判断，避免 TOCTOU 窗口。用 ``RETURNING`` 原子获得"删没删
        成功"与"删的是哪一条"：调用方需要在回执里回显被删条目供用户自行核对
        （短序号解析到具体 id 之间存在集合变化的边缘情形）。
        """
        cursor = self._execute(
            """
            DELETE FROM user_memory
             WHERE id = %s AND user_id = %s
            RETURNING memory_type, memory_key, memory_value, created_at
            """,
            (memory_id, user_id),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return UserMemoryEntry(
            memory_id=memory_id,
            memory_type=row[0],
            memory_key=row[1],
            memory_value=row[2],
            created_at=row[3],
        )

    def clear_user_memory(self, *, user_id: str) -> int:
        """清空该用户的全部记忆，返回清掉的行数。

        ``/memory clear`` 与两处清除钩子（停用、权限真变）共用同一个方法，硬
        ``DELETE``、不可恢复。**锁顺序不变量**：本方法只对 ``user_memory``
        单表做无条件 ``DELETE``，不参与 ``clear_delivered_content_for_user``
        那组死锁面；两处清除钩子都固定先调用它再调用本方法，新增调用点须保持
        这个顺序，不要让本方法先执行或与其并发调用。
        """
        cursor = self._execute("DELETE FROM user_memory WHERE user_id = %s", (user_id,))
        return cursor.rowcount
