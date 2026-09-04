"""管理卡上下文的 PostgreSQL 持久适配器（#493）。

管理卡的 message/card 关联、整卡 sequence 和视觉恢复水位必须跨 gateway 重启保留。
该模块只负责参数化 SQL 与事务边界；读取保留到期上下文交给调用层做惰性关闭，
gateway 的 needs_refresh scanner 只重试已落库状态的 CardKit 更新，不主动清扫到期行。
"""

from __future__ import annotations

from datetime import UTC, datetime

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.admin.card_dispatch import (
    MANAGEMENT_CARD_CONTEXT_DEFAULT_TTL_SECONDS,
    ManagementCardContext,
    bounded_management_card_deadline,
    bounded_management_card_ttl_seconds,
)

# 内部缓存保留窗口，不对管理员承诺；用户可见的确认窗口仍由 pending_action 的
# 产品合同控制。它只避免永久保留消息映射，具体值可随部署配置调整。
_INTERNAL_CONTEXT_TTL_SECONDS = MANAGEMENT_CARD_CONTEXT_DEFAULT_TTL_SECONDS

_SELECT_COLUMNS = (
    "message_id, card_id, identifier, chat_id, initiated_by_open_id, card_sequence,"
    " state_version,"
    " snapshot_fingerprint, context_deadline_at, state, dispatch_status, last_trace_id,"
    " daily_correction_reported_at, daily_correction_pending, needs_refresh, visual_sequence"
)

_INSERT_COLUMNS = (
    "message_id, card_id, identifier, chat_id, initiated_by_open_id, card_sequence,"
    " state_version,"
    " snapshot_fingerprint, context_deadline_at, state, dispatch_status, last_trace_id,"
    " needs_refresh, visual_sequence"
)


def _row_to_context(row: tuple) -> ManagementCardContext:
    return ManagementCardContext(
        message_id=row[0],
        card_id=row[1],
        identifier=row[2],
        chat_id=row[3],
        initiated_by_open_id=row[4],
        card_sequence=int(row[5]),
        state_version=int(row[6]),
        snapshot_fingerprint=row[7],
        context_deadline_at=row[8],
        state=row[9],
        dispatch_status=row[10],
        last_trace_id=row[11],
        daily_correction_reported_at=row[12],
        daily_correction_pending=bool(row[13]),
        needs_refresh=bool(row[14]),
        visual_sequence=int(row[15]),
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
        self._ttl_seconds = bounded_management_card_ttl_seconds(ttl_seconds)

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
        state_version: int = 1,
        context_deadline_at: datetime | None = None,
        state: str = "ready",
        dispatch_status: str = "idle",
        last_trace_id: str | None = None,
    ) -> None:
        if not all((message_id, identifier, card_id, chat_id, initiated_by_open_id, snapshot_fingerprint)):
            raise ValueError("管理卡持久上下文缺少必填字段")
        deadline = bounded_management_card_deadline(
            now=datetime.now(UTC),
            requested=context_deadline_at,
            ttl_seconds=self._ttl_seconds,
        )
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO management_card_context
                    ({_INSERT_COLUMNS})
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s)
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
                    state_version = GREATEST(
                        management_card_context.state_version,
                        EXCLUDED.state_version
                    ),
                    visual_sequence = GREATEST(
                        management_card_context.visual_sequence,
                        EXCLUDED.visual_sequence
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
                    max(1, int(state_version)),
                    snapshot_fingerprint,
                    deadline,
                    state,
                    dispatch_status,
                    last_trace_id,
                    max(1, int(card_sequence)),
                ),
            )

    def lookup(self, *, message_id: str) -> str | None:
        context = self.lookup_context(message_id=message_id)
        if context is None or context.context_deadline_at <= datetime.now(UTC):
            return None
        return context.identifier

    def lookup_context(self, *, message_id: str) -> ManagementCardContext | None:
        """读取完整上下文，包括已过内部保留窗口的行。

        管理卡回调需要在惰性路径上把旧实体刷新为不可操作状态，因此这里不在 SQL
        层过滤 deadline；严格的 ``lookup()`` 仍会把到期项当作未命中。数据库保留由
            后续显式清理策略负责；needs_refresh scanner 不会清理这些上下文。
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

    def next_card_sequence(
        self,
        *,
        message_id: str,
        expected_card_sequence: int | None = None,
    ) -> int | None:
        """原子领取下一条 CardKit 序号，并可按 scanner 快照的序号做 CAS。

        恢复路径必须传入它读取的 CardKit 序号；不匹配就返回 ``None`` 且不消耗
        序号，未传期望值时保留旧调用方的无条件递增兼容姿态，未知消息仍抛
        ``KeyError``。

        这里只用 ``card_sequence`` 一把 CAS（#493 收敛，rc25 S-4a）。写入方里每一次
        ``state_version`` 递增都与 ``card_sequence`` 递增写在同一条 UPDATE 语句里，
        而本方法还会在**不动** ``state_version`` 的前提下推进 ``card_sequence``；
        所以「``card_sequence`` 未变」蕴含「``state_version`` 未变」，反之不成立——
        两个恢复者拿着同一份状态快照并发领号时，只有 ``card_sequence`` 认得出来。
        ``state_version`` 列保留（生产 drop column 不可逆），只是不再参与 CAS。
        """

        if expected_card_sequence is not None and (
            isinstance(expected_card_sequence, bool)
            or not isinstance(expected_card_sequence, int)
            or expected_card_sequence <= 0
        ):
            raise ValueError("expected_card_sequence 必须是正整数")
        conditions = ["message_id = %(message_id)s"]
        parameters: dict[str, object] = {"message_id": message_id}
        if expected_card_sequence is not None:
            conditions.append("card_sequence = %(expected_card_sequence)s")
            parameters["expected_card_sequence"] = expected_card_sequence
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE management_card_context"
                " SET card_sequence = card_sequence + 1, updated_at = now()"
                " WHERE " + " AND ".join(conditions) + " RETURNING card_sequence",
                parameters,
            )
            row = cursor.fetchone()
        if row is None:
            if expected_card_sequence is not None:
                return None
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
            # 即使旧状态已经是 incomplete，迟到的 instant outbox 成功也不具备每日批
            # 资格；只有 settle_published_contexts 识别到 daily reason 时才重新置为待
            # 汇总水位。若该水位已经由 daily settle 置上，后来的迟到 instant 成功
            # 不能把真实的每日批补齐事实抹掉。
            assignments.append(
                "daily_correction_reported_at = CASE"
                " WHEN daily_correction_pending THEN daily_correction_reported_at"
                " ELSE COALESCE(daily_correction_reported_at, now()) END"
            )
        # ``incomplete`` 只表示即时路径未在观察窗口内完成，不是 daily batch 已经
        # 修复的证据；daily_correction_pending 只能由 settle_published_contexts 置位。
        if any(
            value is not None
            for value in (state, dispatch_status, snapshot_fingerprint, last_trace_id)
        ):
            # 状态写入和视觉恢复必须属于同一条单调版本链。若 scanner 已经取出旧
            # 快照，随后有新的状态落库，版本抬高后旧回写只能留下 needs_refresh，
            # 不能把新状态误标为已交付。承担这件事的是 ``card_sequence``——它在这里
            # 与 ``state_version`` 同语句 +1，另外还会被 ``next_card_sequence()`` 单独
            # 推进，所以它的判别力严格覆盖 ``state_version``。``state_version`` 自
            # rc25 S-4a 起不再参与任何 CAS，只作为状态代数留在行上继续维护（生产
            # drop column 不可逆，按裁定 D-16 保留该列）。
            assignments.append("card_sequence = card_sequence + 1")
            assignments.append("state_version = state_version + 1")
            assignments.append("needs_refresh = TRUE")
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

    def list_needing_refresh(self, *, limit: int = 20) -> tuple[ManagementCardContext, ...]:
        """列出数据库状态已改变但尚未被 CardKit 成功回写的管理卡。"""

        if limit < 1:
            return ()
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {_SELECT_COLUMNS} FROM management_card_context"
                " WHERE needs_refresh = TRUE ORDER BY updated_at, message_id LIMIT %s",
                (limit,),
            )
            rows = cursor.fetchall()
        return tuple(_row_to_context(row) for row in rows)

    def mark_visual_refreshed(
        self,
        *,
        message_id: str,
        sequence: int,
        expected_card_sequence: int | None = None,
    ) -> bool:
        """仅在 CardKit update 成功后推进视觉水位，并按本次领到的序号做 CAS。

        ``expected_card_sequence`` 同时挡住两件事：新的状态写入把行推进了（状态写
        与 ``card_sequence`` 递增同语句，见 :meth:`update_state`），以及另一个恢复者
        为同一状态另领了号。因此不再单独判 ``state_version``（#493 收敛，rc25 S-4a）；
        未传期望值时保留旧适配器调用的单调序号姿态。
        """

        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise ValueError("sequence 必须是正整数")
        if expected_card_sequence is not None and (
            isinstance(expected_card_sequence, bool)
            or not isinstance(expected_card_sequence, int)
            or expected_card_sequence <= 0
        ):
            raise ValueError("expected_card_sequence 必须是正整数")
        conditions = [
            "message_id = %(message_id)s",
            "%(sequence)s >= visual_sequence",
        ]
        parameters: dict[str, object] = {
            "message_id": message_id,
            "sequence": sequence,
        }
        if expected_card_sequence is not None:
            conditions.append("card_sequence = %(expected_card_sequence)s")
            parameters["expected_card_sequence"] = expected_card_sequence

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE management_card_context"
                " SET visual_sequence = GREATEST(visual_sequence, %(sequence)s),"
                "     needs_refresh = CASE WHEN card_sequence <= %(sequence)s THEN FALSE ELSE TRUE END,"
                "     updated_at = now()"
                " WHERE " + " AND ".join(conditions),
                parameters,
            )
            return cursor.rowcount == 1

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
                WITH latest_published AS (
                    SELECT DISTINCT ON (c.message_id)
                           c.message_id,
                           c.state AS previous_state,
                           o.reason
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
                       AND o.created_at >= pa.decided_at
                       AND o.published_at >= pa.decided_at
                       AND o.reason IN (
                           'admin_action_instant_recompute',
                           'admin_action_instant_revoke',
                           'daily_permission_refresh',
                           'daily_permission_revoke'
                       )
                     ORDER BY c.message_id, o.permission_version DESC,
                              o.published_at DESC, o.created_at DESC, o.id DESC
                ), candidates AS (
                    SELECT message_id,
                           previous_state,
                           (reason IN (
                               'daily_permission_refresh',
                               'daily_permission_revoke'
                           )) AS daily_batch_published
                      FROM latest_published
                ), updated AS (
                    UPDATE management_card_context c
                       SET state = 'effective',
                           dispatch_status = 'effective',
                           card_sequence = c.card_sequence + 1,
                           state_version = c.state_version + 1,
                           needs_refresh = TRUE,
                           daily_correction_reported_at = CASE
                               WHEN candidates.previous_state = 'incomplete'
                                    AND candidates.daily_batch_published THEN NULL
                               ELSE COALESCE(c.daily_correction_reported_at, now())
                           END,
                           daily_correction_pending = (
                               candidates.previous_state = 'incomplete'
                               AND candidates.daily_batch_published
                           ),
                           updated_at = now()
                      FROM candidates
                     WHERE c.message_id = candidates.message_id
                    RETURNING c.message_id
                )
                SELECT updated.message_id, candidates.previous_state,
                       candidates.daily_batch_published
                  FROM updated
                  JOIN candidates ON candidates.message_id = updated.message_id
                """
            )
            rows = cursor.fetchall()
        return tuple(
            str(row[0])
            for row in rows
            if row[1] == "incomplete" and bool(row[2])
        )

    def unreported_daily_correction_ids(self) -> tuple[str, ...]:
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT message_id FROM management_card_context
                    WHERE state = 'effective'
                      AND daily_correction_pending = TRUE
                      AND daily_correction_reported_at IS NULL
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
                "UPDATE management_card_context SET daily_correction_reported_at = now(), "
                "daily_correction_pending = FALSE "
                f"WHERE message_id IN ({placeholders}) "
                "AND state = 'effective' AND daily_correction_pending = TRUE "
                "AND daily_correction_reported_at IS NULL",
                message_ids,
            )


__all__ = ["PostgresManagementCardContextStore"]
