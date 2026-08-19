"""投递 outbox（Issue #239 从 ``postgres_conversation.PostgresTaskQueue`` 按读写
边界拆分而来）：Worker 侧写事件（``append_delivery_event``/``write_terminal_event``）、
Gateway 侧确认送达（``confirm_delivery``）、二十四小时到期强制收敛
（``expire_undelivered_terminals``），以及三类会话边界触发共用的投递正文清除与
两小时空闲扫描——原文件里这几个方法紧跟在同一段「投递事件 outbox（Issue #151）」
注释之后，没有另起小节，因此归为同一条读写边界。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from lingxi.adapters.postgres import connect
from lingxi.core.delivery.ports import DeliveryEventType, resolve_delivered_outcome
from lingxi.core.ids import new_id

from ._dataclasses import AppendedEvent, TerminalTask
from ._transaction import _Transaction

# ``sweep_idle_conversations`` 每次扫描新增排队的会话数上限（PR #173 独立复核
# P2-5）：查询本身已经用 NOT EXISTS 把候选集收窄到"尚未排过队"的那些，这里只是
# 给单次扫描一个防护上限，避免一次异常的大批量话题同时到点时单条查询/单次事务
# 处理过多行。500 是与 Worker 侧消费能力（`claim_session_cleanups` 默认单批 20、
# 每约 2 秒的收口轮询一次）比较后留出的宽裕量，不是精确调校值。
_IDLE_SESSION_CLEANUP_SWEEP_LIMIT = 500


class _OutboxMixin:
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
