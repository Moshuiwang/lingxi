"""运营审计账 ``operation_audit`` 的落库：同事务追加、自开连接追加、只读回读、到期删除。

两个写口对应两种调用姿态：``record_operation_audit(connection, entry)`` 在调用方已经
打开的事务里追加，让审计与业务写入一起提交或一起回滚；``PostgresOperationAudit.record``
自开连接、自带事务，供脚本这类没有业务事务可挂的调用方。回读只给预发核对与后续的
查看入口用，按操作号或按时间倒序取，不做任何过滤以外的加工。

模块内不把任何列的取值写进日志：条目本身已由核心模型按形状拒绝过，但日志仍只记
条数与异常类型名。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.admin.operation_audit import (
    EntryPoint,
    OperationAuditEntry,
    OperationAuditRecord,
    OperationPhase,
    format_actor_roles,
    parse_actor_roles,
)
from lingxi.core.ids import new_id

DEFAULT_PURGE_LIMIT = 200

_COLUMNS = (
    "id",
    "operation_id",
    "operation",
    "phase",
    "initiated_by",
    "actor_roles",
    "decided_by",
    "executor",
    "entry_point",
    "purpose",
    "target_kind",
    "target_count",
    "target_digest",
    "target_user_id",
    "result_code",
    "result_counts",
    "evidence_ref",
    "pending_action_id",
    "trace_id",
    "created_at",
    "expires_at",
)
_INSERT_SQL = (
    "INSERT INTO operation_audit ("
    + ", ".join(_COLUMNS[:-2])
    + ") VALUES ("
    + ", ".join("%s::jsonb" if name == "result_counts" else "%s" for name in _COLUMNS[:-2])
    + ")"
)
_SELECT_SQL = "SELECT " + ", ".join(_COLUMNS) + " FROM operation_audit"


def _row_values(entry_id: str, entry: OperationAuditEntry) -> tuple:
    return (
        entry_id,
        entry.operation_id,
        entry.operation,
        entry.phase.value,
        entry.initiated_by,
        format_actor_roles(entry.actor_roles),
        entry.decided_by,
        entry.executor,
        entry.entry_point.value,
        entry.purpose,
        entry.target_kind,
        entry.target_count,
        entry.target_digest,
        entry.target_user_id,
        entry.result_code,
        json.dumps(dict(entry.result_counts), sort_keys=True),
        entry.evidence_ref,
        entry.pending_action_id,
        entry.trace_id,
    )


def _record_from_row(row: tuple) -> OperationAuditRecord:
    values = dict(zip(_COLUMNS, row, strict=True))
    entry = OperationAuditEntry(
        operation_id=values["operation_id"],
        operation=values["operation"],
        phase=OperationPhase(values["phase"]),
        initiated_by=values["initiated_by"],
        actor_roles=parse_actor_roles(values["actor_roles"]),
        entry_point=EntryPoint(values["entry_point"]),
        decided_by=values["decided_by"],
        executor=values["executor"],
        purpose=values["purpose"],
        target_kind=values["target_kind"],
        target_count=values["target_count"],
        target_digest=values["target_digest"],
        target_user_id=values["target_user_id"],
        result_code=values["result_code"],
        result_counts=values["result_counts"],
        evidence_ref=values["evidence_ref"],
        pending_action_id=values["pending_action_id"],
        trace_id=values["trace_id"],
    )
    return OperationAuditRecord(
        id=values["id"],
        entry=entry,
        created_at=values["created_at"],
        expires_at=values["expires_at"],
    )


def _checked_entry(entry: object) -> OperationAuditEntry:
    if not isinstance(entry, OperationAuditEntry):
        raise TypeError("只能记录 OperationAuditEntry")
    return entry


def record_operation_audit(connection, entry: OperationAuditEntry) -> str:
    """在调用方事务内追加一行并返回其标识；失败原样冒泡，由调用方决定是否回滚。"""
    checked = _checked_entry(entry)
    entry_id = new_id("opa")
    with connection.cursor() as cursor:
        cursor.execute(_INSERT_SQL, _row_values(entry_id, checked))
    return entry_id


def _checked_moment(now: datetime | None) -> datetime:
    """到期判定时间必须带时区：朴素时刻会按服务端时区被静默解释，判定可能差整整八小时。"""
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("到期判定时间必须带时区")
    return moment


def purge_expired_operation_audit(
    connection, *, now: datetime | None = None, limit: int = DEFAULT_PURGE_LIMIT
) -> int:
    """在调用方事务内删除已到期的行，返回删除条数。

    到期判据只有 ``expires_at <= now`` 一个条件（期限由触发器写死，多加业务条件等于
    给「某些记录可以留过九十天」开口子）；小批量、每次调用一批，积压交给下一轮。
    """
    moment = _checked_moment(now)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit 必须是正整数")
    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM operation_audit WHERE id IN ("
            "SELECT id FROM operation_audit WHERE expires_at <= %s "
            "ORDER BY expires_at, id LIMIT %s)",
            (moment, limit),
        )
        return cursor.rowcount


class PostgresOperationAudit:
    """自开连接的写口与只读回读口。构造时不连接数据库，每个方法自带事务。"""

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        """记下 DSN 与超时配置；不在构造时连接数据库。"""
        self._dsn = dsn
        self._timeouts = timeouts

    def record(self, entry: OperationAuditEntry) -> str:
        """追加一行并提交，返回其标识。"""
        checked = _checked_entry(entry)
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                return record_operation_audit(connection, checked)

    def recent(self, limit: int = 50) -> tuple[OperationAuditRecord, ...]:
        """最近写入的若干行，按时间倒序。"""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        return self._fetch(_SELECT_SQL + " ORDER BY created_at DESC, id DESC LIMIT %s", (limit,))

    def for_operation(self, operation_id: str) -> tuple[OperationAuditRecord, ...]:
        """同一操作号的全部阶段，按时间正序。"""
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("operation_id 必须是非空字符串")
        return self._fetch(
            _SELECT_SQL + " WHERE operation_id = %s ORDER BY created_at, id", (operation_id,)
        )

    def _fetch(self, sql: str, parameters: tuple) -> tuple[OperationAuditRecord, ...]:
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, parameters)
                rows = cursor.fetchall()
        return tuple(_record_from_row(row) for row in rows)
