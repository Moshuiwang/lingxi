"""管理卡上下文的 PostgreSQL 持久适配器（#493）。

管理卡的 message/card 关联和整卡 sequence 必须跨 gateway 重启保留。该模块只负责
参数化 SQL 与事务边界；读取保留到期上下文交给调用层做惰性关闭，不能启动后台扫描。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.admin.card_dispatch import ManagementCardContext


# 内部缓存保留窗口，不对管理员承诺；用户可见的确认窗口仍由 pending_action 的
# 产品合同控制。它只避免永久保留消息映射，具体值可随部署配置调整。
_INTERNAL_CONTEXT_TTL_SECONDS = 1800.0

_SELECT_COLUMNS = (
    "message_id, card_id, identifier, chat_id, initiated_by_open_id, card_sequence,"
    " snapshot_fingerprint, context_deadline_at, state, dispatch_status, last_trace_id,"
    " daily_correction_reported_at"
)

_INSERT_COLUMNS = (
    "message_id, card_id, identifier, chat_id, initiated_by_open_id, card_sequence,"
    " snapshot_fingerprint, context_deadline_at, state, dispatch_status, last_trace_id"
)


def _row_to_context(row: tuple) -> ManagementCardContext:
    return ManagementCardContext(
        message_id=row[0],
        card_id=row[1],
        identifier=row[2],
        chat_id=row[3],
        initiated_by_open_id=row[4],
        card_sequence=int(row[5]),
        snapshot_fingerprint=row[6],
        context_deadline_at=row[7],
        state=row[8],
        dispatch_status=row[9],
        last_trace_id=row[10],
        daily_correction_reported_at=row[11],
    )


class PostgresManagementCardContextStore:
    """``management_card_context`` 的唯一真实读写实现。"""

    def __init__(
        self,
        dsn: str,
        *,
        timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS,
        ttl_seconds: float = _INTERNAL_CONTEXT_TTL_SECONDS,
    ) -> None:
        self._dsn = dsn
        self._timeouts = timeouts
        self._ttl_seconds = ttl_seconds

    def remember(
        self,
        *,
        message_id: str,
        identifier: str,
        card_id: str,
        chat_id: str,
        initiated_by_open_id: str,
        snapshot_fingerprint: str,
        card_sequence: int = 2,
        context_deadline_at: datetime | None = None,
        state: str = "ready",
        dispatch_status: str = "idle",
        last_trace_id: str | None = None,
    ) -> None:
        if not all((message_id, identifier, card_id, chat_id, initiated_by_open_id, snapshot_fingerprint)):
            raise ValueError("管理卡持久上下文缺少必填字段")
        deadline = context_deadline_at or (
            datetime.now(timezone.utc) + timedelta(seconds=self._ttl_seconds)
        )
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO management_card_context
                    ({_INSERT_COLUMNS})
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (message_id) DO UPDATE SET
                    -- A retry/replay must never move a persisted card backwards.
                    -- ``next_card_sequence`` is the normal update path; keeping
                    -- the other fields from the first registration also prevents
                    -- a replay from reopening a closed/submitted card, changing
                    -- its target, or extending its context deadline.
                    card_sequence = GREATEST(
                        management_card_context.card_sequence,
                        EXCLUDED.card_sequence
                    ),
                    updated_at = now()
                """,
                (
                    message_id,
                    card_id,
                    identifier,
                    chat_id,
                    initiated_by_open_id,
                    max(1, int(card_sequence)),
                    snapshot_fingerprint,
                    deadline,
                    state,
                    dispatch_status,
                    last_trace_id,
                ),
            )

    def lookup(self, *, message_id: str) -> str | None:
        context = self.lookup_context(message_id=message_id)
        if context is None or context.context_deadline_at <= datetime.now(timezone.utc):
            return None
        return context.identifier

    def lookup_context(self, *, message_id: str) -> ManagementCardContext | None:
        """读取完整上下文，包括已过内部保留窗口的行。

        管理卡回调需要在惰性路径上把旧实体刷新为不可操作状态，因此这里不在 SQL
        层过滤 deadline；严格的 ``lookup()`` 仍会把到期项当作未命中。数据库保留由
        后续显式清理策略负责，当前不启动后台扫描。
        """

        if not message_id:
            return None
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {_SELECT_COLUMNS} FROM management_card_context"
                " WHERE message_id = %s",
                (message_id,),
            )
            row = cursor.fetchone()
        return _row_to_context(row) if row is not None else None

    def next_card_sequence(self, *, message_id: str) -> int:
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE management_card_context"
                " SET card_sequence = card_sequence + 1, updated_at = now()"
                " WHERE message_id = %s"
                " RETURNING card_sequence",
                (message_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise KeyError(message_id)
        return int(row[0])

    def update_state(
        self,
        *,
        message_id: str,
        state: str | None = None,
        dispatch_status: str | None = None,
        snapshot_fingerprint: str | None = None,
        last_trace_id: str | None = None,
    ) -> ManagementCardContext | None:
        values: list[object] = []
        assignments: list[str] = []
        for column, value in (
            ("state", state),
            ("dispatch_status", dispatch_status),
            ("snapshot_fingerprint", snapshot_fingerprint),
            ("last_trace_id", last_trace_id),
        ):
            if value is not None:
                assignments.append(f"{column} = %s")
                values.append(value)
        if state == "effective":
            # 即时成功（旧状态为 dispatching/submitted）不应被当成每日批补齐；
            # 但若旧状态已经是 incomplete，则保留 NULL，交给汇总发送逻辑置水位。
            assignments.append(
                "daily_correction_reported_at = CASE "
                "WHEN state = 'incomplete' THEN daily_correction_reported_at "
                "ELSE COALESCE(daily_correction_reported_at, now()) END"
            )
        if not assignments:
            return self.lookup_context(message_id=message_id)
        values.append(message_id)
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE management_card_context SET "
                + ", ".join(assignments)
                + ", updated_at = now() WHERE message_id = %s"
                " RETURNING "
                + _SELECT_COLUMNS,
                tuple(values),
            )
            row = cursor.fetchone()
        return _row_to_context(row) if row is not None else None

    def latest_publish_state_for_message(self, *, message_id: str) -> str | None:
        """读取管理卡关联操作对应的最新权限发布状态。

        该查询只用于 gateway 的短暂状态观察：``published`` 才能显示「已生效」，
        ``pending/publishing`` 保持等待，``failed`` 才进入诚实的未完成态。它不改变
        outbox，也不把「已经排入 outbox」误当成外部表已读回一致。
        """

        if not message_id:
            return None
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT o.status
                  FROM pending_action pa
                  JOIN app_user u ON u.feishu_open_id = pa.target_open_id
                  JOIN publish_outbox o ON o.user_id = u.id
                 WHERE pa.origin_card_message_id = %s
                   -- 只接受这次确认之后创建的发布意图；管理员此前同一用户的
                   -- 旧 outbox 即使后来成功，也不能把本次管理卡误报成已生效。
                   AND o.created_at >= pa.decided_at
                   AND o.reason IN (
                       'admin_action_instant_recompute',
                       'admin_action_instant_revoke',
                       'daily_permission_refresh',
                       'daily_permission_revoke'
                   )
                 ORDER BY o.permission_version DESC, o.created_at DESC, o.id DESC
                 LIMIT 1
                """,
                (message_id,),
            )
            row = cursor.fetchone()
        return str(row[0]) if row is not None else None

    def settle_published_contexts(self) -> tuple[str, ...]:
        """把已被发布消费面读回一致的管理卡置为 ``effective``。

        返回本次从 ``incomplete`` 补齐的消息 ID，供每日批发一条汇总；从
        ``submitted``/``dispatching`` 正常收口的上下文不会计入汇总。把
        ``submitted`` 纳入是为了覆盖 gateway 在确认已提交、但尚未来得及把原管理卡
        刷成 ``dispatching`` 时重启的恢复窗口；关联条件以确认时间为下界，避免把
        该管理员操作之前的旧发布误认成这次操作已完成。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH candidates AS (
                    SELECT DISTINCT ON (c.message_id)
                           c.message_id,
                           c.state AS previous_state
                      FROM management_card_context c
                      JOIN pending_action pa
                        ON pa.origin_card_message_id = c.message_id
                      JOIN app_user u
                        ON u.feishu_open_id = pa.target_open_id
                      JOIN publish_outbox o
                        ON o.user_id = u.id
                     WHERE c.state IN ('submitted', 'dispatching', 'incomplete')
                       AND pa.status = 'executed'
                       AND pa.decided_at IS NOT NULL
                       AND o.status = 'published'
                       AND o.published_at IS NOT NULL
                       AND o.published_at >= pa.decided_at
                       AND o.reason IN (
                           'admin_action_instant_recompute',
                           'admin_action_instant_revoke',
                           'daily_permission_refresh',
                           'daily_permission_revoke'
                       )
                     ORDER BY c.message_id, o.permission_version DESC,
                              o.published_at DESC, o.id DESC
                ), updated AS (
                    UPDATE management_card_context c
                       SET state = 'effective',
                           dispatch_status = 'effective',
                           daily_correction_reported_at = CASE
                               WHEN candidates.previous_state = 'incomplete' THEN NULL
                               ELSE COALESCE(c.daily_correction_reported_at, now())
                           END,
                           updated_at = now()
                      FROM candidates
                     WHERE c.message_id = candidates.message_id
                    RETURNING c.message_id, candidates.previous_state
                )
                SELECT message_id, previous_state FROM updated
                """
            )
            rows = cursor.fetchall()
        return tuple(str(row[0]) for row in rows if row[1] == "incomplete")

    def unreported_daily_correction_ids(self) -> tuple[str, ...]:
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT message_id FROM management_card_context
                    WHERE state = 'effective' AND daily_correction_reported_at IS NULL
                    ORDER BY updated_at, message_id"""
            )
            rows = cursor.fetchall()
        return tuple(str(row[0]) for row in rows)

    def mark_daily_corrections_reported(self, *, message_ids: tuple[str, ...]) -> None:
        if not message_ids:
            return
        placeholders = ", ".join("%s" for _ in message_ids)
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE management_card_context SET daily_correction_reported_at = now() "
                f"WHERE message_id IN ({placeholders}) "
                "AND state = 'effective' AND daily_correction_reported_at IS NULL",
                message_ids,
            )


__all__ = ["PostgresManagementCardContextStore"]
