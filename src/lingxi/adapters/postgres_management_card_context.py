"""管理卡上下文的 PostgreSQL 持久适配器。

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

#: 重试/重放绝不能让一张已落库的卡倒退。``next_card_sequence()`` 是常规推进
#: 路径；首次登记的其余字段在冲突时保留原样，防止一次重放重新打开已关闭/
#: 已提交的卡、改变它的目标，或延长它的上下文截止时间。
_REMEMBER_CONTEXT_SQL = f"""
INSERT INTO management_card_context
    ({_INSERT_COLUMNS})
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s)
ON CONFLICT (message_id) DO UPDATE SET
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
"""


#: 一张管理卡在收口后可以继续操作，因此同一个 message_id 下会累积多条
#: pending_action。关联必须钉死**当前那一次**操作（按登记时间取最新的一条），
#: 否则上一次操作的历史 published 行会让这一次被误判成已生效。
_CURRENT_CARD_ACTION_SQL = """
    SELECT current_action.id
      FROM pending_action AS current_action
     WHERE current_action.origin_card_message_id = %s
     ORDER BY current_action.created_at DESC, current_action.id DESC
     LIMIT 1
"""

#: 把已被发布消费面读回一致的管理卡置为 effective：daily_batch_published 只在
#: reason 属于每日批时为真，previous_state='incomplete' 且命中每日批才把
#: daily_correction_pending 置真（供每日汇总补一句），其余收口路径不计入汇总。
_SETTLE_PUBLISHED_CONTEXTS_SQL = """
WITH latest_published AS (
    SELECT DISTINCT ON (c.message_id)
           c.message_id,
           c.state AS previous_state,
           o.reason
      FROM management_card_context c
      JOIN pending_action pa
        ON pa.id = (
               SELECT current_action.id
                 FROM pending_action AS current_action
                WHERE current_action.origin_card_message_id = c.message_id
                ORDER BY current_action.created_at DESC, current_action.id DESC
                LIMIT 1
           )
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
        """记下 DSN、超时配置与内部保留窗口；不在构造时连接数据库。"""
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
        """登记或刷新一张管理卡的持久上下文；重放绝不让已落库的卡倒退。"""
        if not all(
            (message_id, identifier, card_id, chat_id, initiated_by_open_id, snapshot_fingerprint)
        ):
            raise ValueError("管理卡持久上下文缺少必填字段")
        deadline = bounded_management_card_deadline(
            now=datetime.now(UTC),
            requested=context_deadline_at,
            ttl_seconds=self._ttl_seconds,
        )
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                _REMEMBER_CONTEXT_SQL,
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
        """未过内部保留窗口时返回该消息关联的 identifier，否则返回 ``None``。"""
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
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                f"SELECT {_SELECT_COLUMNS} FROM management_card_context WHERE message_id = %s",
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
        ``KeyError``。这里只用 ``card_sequence`` 一把 CAS：写入方每一次
        ``state_version`` 递增都与 ``card_sequence`` 递增同语句，而本方法还会
        在不动 ``state_version`` 的前提下推进 ``card_sequence``——两个恢复者
        拿着同一份状态快照并发领号时，只有 ``card_sequence`` 认得出来。
        ``state_version`` 列保留，只是不再参与 CAS。
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
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
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

    @staticmethod
    def _build_state_update(
        *,
        state: str | None,
        dispatch_status: str | None,
        snapshot_fingerprint: str | None,
        last_trace_id: str | None,
    ) -> tuple[list[str], list[object]]:
        """拼出 ``update_state`` 要用的 ``SET`` 片段与对应参数。

        ``state == "effective"``：即时成功不应被当成每日批补齐（迟到的 instant
        outbox 成功不具备每日批资格；只有 :meth:`settle_published_contexts`
        识别到 daily reason 时才重新置为待汇总水位）。任一字段真的改变时：
        状态写入和视觉恢复必须属于同一条单调版本链，``card_sequence`` 与
        ``state_version`` 同语句 +1（``state_version`` 不再参与任何 CAS，只作为
        状态代数继续维护，生产 drop column 不可逆）。
        """
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
            assignments.append(
                "daily_correction_reported_at = CASE"
                " WHEN daily_correction_pending THEN daily_correction_reported_at"
                " ELSE COALESCE(daily_correction_reported_at, now()) END"
            )
        if any(
            value is not None
            for value in (state, dispatch_status, snapshot_fingerprint, last_trace_id)
        ):
            assignments.append("card_sequence = card_sequence + 1")
            assignments.append("state_version = state_version + 1")
            assignments.append("needs_refresh = TRUE")
        return assignments, values

    def update_state(
        self,
        *,
        message_id: str,
        state: str | None = None,
        dispatch_status: str | None = None,
        snapshot_fingerprint: str | None = None,
        last_trace_id: str | None = None,
    ) -> ManagementCardContext | None:
        """按传入的非空字段更新一张管理卡；不传任何字段时只读回现状。"""
        assignments, values = self._build_state_update(
            state=state,
            dispatch_status=dispatch_status,
            snapshot_fingerprint=snapshot_fingerprint,
            last_trace_id=last_trace_id,
        )
        if not assignments:
            return self.lookup_context(message_id=message_id)
        values.append(message_id)
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "UPDATE management_card_context SET "
                + ", ".join(assignments)
                + ", updated_at = now() WHERE message_id = %s"
                " RETURNING " + _SELECT_COLUMNS,
                tuple(values),
            )
            row = cursor.fetchone()
        return _row_to_context(row) if row is not None else None

    def list_needing_refresh(self, *, limit: int = 20) -> tuple[ManagementCardContext, ...]:
        """列出数据库状态已改变但尚未被 CardKit 成功回写的管理卡。"""
        if limit < 1:
            return ()
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
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
        为同一状态另领了号。因此不再单独判 ``state_version``；未传期望值时保留
        旧适配器调用的单调序号姿态。
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

        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "UPDATE management_card_context"
                " SET visual_sequence = GREATEST(visual_sequence, %(sequence)s),"
                "     needs_refresh = CASE WHEN card_sequence <= %(sequence)s THEN FALSE ELSE TRUE END,"
                "     updated_at = now()"
                " WHERE " + " AND ".join(conditions),
                parameters,
            )
            return cursor.rowcount == 1

    def latest_publish_state_for_action(
        self, *, message_id: str, pending_action_id: str
    ) -> str | None:
        """读取**这一次**管理操作对应的最新权限发布状态。

        该查询只用于 gateway 的短暂状态观察：``published`` 才能显示「已生效」，
        ``pending/publishing`` 保持等待，``failed`` 才进入诚实的未完成态。它不改变
        outbox，也不把「已经排入 outbox」误当成外部表已读回一致。关联条件钉死本次
        pending action：同一张卡可以在收口后继续操作，只按卡片消息关联会让上一次
        操作的历史发布把这一次误报成已生效。
        """
        if not message_id or not pending_action_id:
            return None
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                SELECT o.status
                  FROM pending_action pa
                  JOIN app_user u ON u.feishu_open_id = pa.target_open_id
                  JOIN publish_outbox o ON o.user_id = u.id
                 WHERE pa.id = %(pending_action_id)s
                   AND pa.origin_card_message_id = %(message_id)s
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
                {"message_id": message_id, "pending_action_id": pending_action_id},
            )
            row = cursor.fetchone()
        return str(row[0]) if row is not None else None

    def is_current_card_action(self, *, message_id: str, pending_action_id: str) -> bool:
        """这条操作是否仍是该管理卡最新的一次操作。

        管理卡收口后可以被复用，迟到的旧回调不得覆盖后一次操作的卡片状态，也不得
        把它重新打开。**查不到任何关联操作时失败关闭**：一张连"当前是哪一次操作"都
        答不出来的卡，没有任何依据接受一条迟到回调的回写——那条回调只能是孤儿。
        参数缺失仍返回 ``True``，那是调用方没有给出判据、不是库里没有答案。
        """
        if not message_id or not pending_action_id:
            return True
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(_CURRENT_CARD_ACTION_SQL, (message_id,))
            row = cursor.fetchone()
        if row is None:
            return False
        return str(row[0]) == pending_action_id

    def settle_published_contexts(self) -> tuple[str, ...]:
        """把已被发布消费面读回一致的管理卡置为 ``effective``。

        返回本次从 ``incomplete`` 补齐的消息 ID，供每日批发一条汇总；从
        ``submitted``/``dispatching`` 正常收口的上下文不会计入汇总。把
        ``submitted`` 纳入是为了覆盖 gateway 在确认已提交、但尚未来得及把原管理卡
        刷成 ``dispatching`` 时重启的恢复窗口。关联规则与
        :meth:`latest_publish_state_for_action` 完全一致：只认这张卡当前那一次操作，
        并以它的确认时间为下界，历史操作与更早的发布都不能让这一次收口。
        """
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(_SETTLE_PUBLISHED_CONTEXTS_SQL)
            rows = cursor.fetchall()
        return tuple(str(row[0]) for row in rows if row[1] == "incomplete" and bool(row[2]))

    def unreported_daily_correction_ids(self) -> tuple[str, ...]:
        """待每日汇总补一句、但还没汇报过的消息 ID。"""
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
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
        """把指定消息的每日汇总标记为已上报，仅影响仍待汇报的那些行。"""
        if not message_ids:
            return
        placeholders = ", ".join("%s" for _ in message_ids)
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "UPDATE management_card_context SET daily_correction_reported_at = now(), "
                "daily_correction_pending = FALSE "
                f"WHERE message_id IN ({placeholders}) "
                "AND state = 'effective' AND daily_correction_pending = TRUE "
                "AND daily_correction_reported_at IS NULL",
                message_ids,
            )


__all__ = ["PostgresManagementCardContextStore"]
