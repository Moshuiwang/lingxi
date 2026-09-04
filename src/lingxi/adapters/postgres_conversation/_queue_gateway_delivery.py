"""Gateway 投递消费。"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from lingxi.adapters.postgres import connect

from ._dataclasses import (
    DeliveryEventRecord,
    PendingDeliveryTask,
    StaleQueuedTask,
    UncertainDeliveryTask,
)


class _GatewayDeliveryMixin:
    # 这一组方法服务的是「读 outbox、驱动 CardKit/文本、记消费进度」这一条 Gateway
    # 侧的独立读写路径，与 Worker 侧写 outbox 的那组方法各自独立提交、不共用事务
    # ——两者本来就是不同进程。持久化的进度字段见迁移 0060 头部注释。

    def list_pending_delivery_tasks(self, *, limit: int = 20) -> list[PendingDeliveryTask]:
        """列出本轮需要处理的任务。

        还有未消费的 outbox 事件，或已经拿到 ``delivery_message_id`` 但尚未
        确认送达。**不含** ``dispatch_reserved_kind`` 非空的任务——那些是崩溃
        恢复后 outcome 不明的任务，必须被上层单独识别为 ``uncertain``。只读
        查询，不加锁：外发前预留位（``reserve_dispatch``）才是真正的并发
        互斥点，这里允许多个候选同时被读到，抢占失败的一方在预留时自然让路。
        """
        # gateway 投递循环每 poll_interval 都会跑这条发现查询，空转时也不例外
        # ——走 `_run_polling_operation`（默认逐字节等价于原来的 `connect(...)`，
        # 只有装配方显式打开复用时才改为持有常驻连接；见该方法文档）。

        def _list_pending(connection: Any) -> list[PendingDeliveryTask]:
            with connection.cursor() as cursor:
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

        return self._run_polling_operation(_list_pending)

    def list_stale_queued_tasks(
        self, *, older_than: timedelta, limit: int = 50
    ) -> list[StaleQueuedTask]:
        """列出已入队超过 ``older_than``、仍然 ``queued``（还没有 worker 领取）的任务。

        只读查询，不加锁、不写任何标记——是否已经通知过完全由调用方
        （``apps/gateway/delivery.DeliveryConsumer``）在进程内维护，重启清零是
        已知的可接受降级（尽力而为的体验提示，不是需要跨重启持久化的业务
        结论）。``status = 'queued'`` 复用既有 ``task_queue_idx`` 部分索引的
        过滤前提，积压量通常很小。
        """

        def _list_stale(connection: Any) -> list[StaleQueuedTask]:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT t.id, c.feishu_chat_id, c.feishu_thread_id, t.reply_to_message_id
                      FROM task AS t
                      JOIN conversation AS c ON c.id = t.conversation_id
                     WHERE t.status = 'queued' AND t.created_at < now() - %s::interval
                     ORDER BY t.created_at
                     LIMIT %s
                    """,
                    (older_than, limit),
                )
                return [
                    StaleQueuedTask(
                        task_id=row[0], chat_id=row[1], thread_id=row[2], reply_to_message_id=row[3]
                    )
                    for row in cursor.fetchall()
                ]

        return self._run_polling_operation(_list_stale)

    def list_uncertain_delivery_tasks(self, *, limit: int = 50) -> list[UncertainDeliveryTask]:
        """列出外发前预留位卡住的任务，供告警。见 ``reserve_dispatch`` 的说明。

        ``status IN ('running', 'awaiting_delivery')`` 过滤：
        ``expire_undelivered_terminals`` 的二十四小时强制收敛不读、也不清
        ``dispatch_reserved_kind``（预留位字段是 Gateway 消费循环私有的运行时
        簿记）。没有这条过滤，一个卡在预留位里、后来被到期路径收敛为
        ``failed`` 的任务会永远被这里查出来，造成不会停止的告警——即使任务
        本身早已不再需要任何人处理。
        """
        # 同 `list_pending_delivery_tasks`：每 poll_interval 都会跑，同样走
        # `_run_polling_operation`（复用连接首次失败会重建重试一次，见该方法文档）。

        def _list_uncertain(connection: Any) -> list[UncertainDeliveryTask]:
            with connection.cursor() as cursor:
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

        return self._run_polling_operation(_list_uncertain)

    def read_delivery_events(
        self, *, task_id: str, after_sequence: int
    ) -> list[DeliveryEventRecord]:
        """按序号升序读回一个任务尚未消费的 outbox 事件。"""
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
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
        """在一次结果不可事后消歧的外发调用之前提交预留位。

        三者是建卡、终态卡片更新+关闭、文本兜底发送，共同点：一旦崩溃重启，
        消费循环单靠"重放同一次调用"无法安全判断上一次是否已经外发成功。
        返回 ``False`` 表示没能预留到——任务已不在可处理状态、或已被预留
        （命中说明上一轮处理到一半中断了，调用方应把任务视为 ``uncertain``）。
        预留成功即独立提交：必须在真正发起外部调用之前落盘可见。
        """
        if kind not in ("card_create", "card_finish", "text_send"):
            raise ValueError("dispatch_reserved_kind 只能是 card_create、card_finish 或 text_send")
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
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
        把它路由到人工核对，不自动重发（状态合同第 6 条）。
        """
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
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
        """把一次已经明确知道结果的外发进度写回，总是清空预留位。

        调用这个方法本身就意味着调用方已经拿到了确定的结果（成功，或同步
        捕获的失败）。``card_id``/``message_id``/``card_sequence`` 用
        ``COALESCE`` 只增不减：
        一旦写入就不会被后续调用误置回 ``NULL``；``consumed_sequence`` 用
        ``GREATEST`` 防止乱序调用把游标往回拨；``fallback_text`` 一旦置真就不会
        被置回假（`V-卡片-03`：首次失败后永久走文本通道）。
        """
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
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
