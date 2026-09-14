"""问答留存语料 ``qa_corpus`` 的落库与读取，以及窄读取角色表 ``qa_corpus_reader`` 的读写。

``record_qa_corpus(connection, record)`` 在调用方已经打开的事务里追加；``PostgresQaCorpus``
自开连接、自带事务，供 worker 收口这类没有业务事务可挂的调用方。写入按 ``task_id``
``ON CONFLICT DO NOTHING``：同一任务被重领、重试时不双写，返回值如实说这次有没有写进去。
读取侧（检索、分批导出、容量）只发 ``SELECT``；关键词走 ``ILIKE`` 子串匹配，转义与模式由
核心模块给出。读取前的鉴权与每次读取的审计不在这里——它们是调用方（运维脚本）的顺序义务，
本模块不知道谁在读。

写入失败的处置权在调用方（``apps/worker/qa_corpus_capture.py`` 降级为一条只含任务标识
与异常类型名的日志）；本模块自己不兜底、不写日志——这张表每一行都是正文。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.ids import new_id
from lingxi.core.innertest_content_capture import CapturedToolCall
from lingxi.core.qa_corpus import (
    CorpusFilter,
    QaCorpusReaderEntry,
    QaCorpusRecord,
    checked_read_limit,
    like_pattern,
)

_COLUMNS = (
    "id",
    "task_id",
    "conversation_id",
    "user_id",
    "trace_id",
    "task_created_at",
    "question_content",
    "question_redaction_count",
    "answer_delivered",
    "answer_delivered_redaction_count",
    "answer_model_raw",
    "answer_model_raw_redaction_count",
    "tool_calls",
    "tool_calls_redaction_count",
    "terminal_kind",
    "user_result",
    "failure_code",
    "output_safety_withheld",
    "worker_id",
    "worker_version",
    "target_worker_version",
    "system_prompt_digest",
    "model",
    "created_at",
)
_INSERT_SQL = (
    "INSERT INTO qa_corpus ("
    + ", ".join(_COLUMNS[:-1])
    + ") VALUES ("
    + ", ".join("%s::jsonb" if name == "tool_calls" else "%s" for name in _COLUMNS[:-1])
    + ") ON CONFLICT (task_id) DO NOTHING"
)
_SELECT_SQL = "SELECT " + ", ".join(_COLUMNS) + " FROM qa_corpus"
#: 检索与导出共用的顺序：最新在前；同一时刻按标识倒序，让分批导出的键集翻页没有歧义。
_ORDER_SQL = " ORDER BY created_at DESC, id DESC"
#: 关键词只查问题列与实收正文列——模型原文不作检索目标，也没有索引。转义字符写死为
#: 反斜线，与核心模块 ``LIKE_ESCAPE`` 同一个字符，写成常量是为了让规划器看到常量模式。
_KEYWORD_SQL = "(question_content ILIKE %s ESCAPE '\\' OR answer_delivered ILIKE %s ESCAPE '\\')"
#: 分批导出每批取多少行：每批一条独立语句，不占长事务、不依赖服务端游标。
EXPORT_BATCH_SIZE = 500

_READER_COLUMNS = (
    "id",
    "feishu_open_id",
    "label",
    "entry_status",
    "granted_by",
    "granted_at",
    "revoked_at",
)
_READER_SELECT_SQL = "SELECT " + ", ".join(_READER_COLUMNS) + " FROM qa_corpus_reader"
_READER_ACTIVE_SQL = _READER_SELECT_SQL + " WHERE feishu_open_id = %s AND entry_status = 'active'"


@dataclass(frozen=True)
class QaCorpusRow:
    """库里的一行：内部标识、写入时刻与记录本身。"""

    id: str
    created_at: datetime
    record: QaCorpusRecord


def _row_values(row_id: str, record: QaCorpusRecord) -> tuple:
    return (
        row_id,
        record.task_id,
        record.conversation_id,
        record.user_id,
        record.trace_id,
        record.task_created_at,
        record.question_content,
        record.question_redaction_count,
        record.answer_delivered,
        record.answer_delivered_redaction_count,
        record.answer_model_raw,
        record.answer_model_raw_redaction_count,
        json.dumps(record.tool_calls_payload(), ensure_ascii=False, default=str),
        record.tool_calls_redaction_count,
        record.terminal_kind,
        record.user_result,
        record.failure_code,
        record.output_safety_withheld,
        record.worker_id,
        record.worker_version,
        record.target_worker_version,
        record.system_prompt_digest,
        record.model,
    )


def _tool_call_from_payload(item: Any) -> CapturedToolCall:
    if not isinstance(item, dict):
        raise ValueError("tool_calls 里的每一项都必须是对象")
    return CapturedToolCall(
        tool_use_id=item.get("tool_use_id"),
        tool_name=str(item.get("tool_name") or ""),
        tool_input=item.get("tool_input") or {},
        result_summary=item.get("result_summary") or {},
        redaction_count=int(item.get("redaction_count") or 0),
    )


def _row_from_values(row: tuple) -> QaCorpusRow:
    values = dict(zip(_COLUMNS, row, strict=True))
    tool_calls = values["tool_calls"]
    record = QaCorpusRecord(
        task_id=values["task_id"],
        conversation_id=values["conversation_id"],
        user_id=values["user_id"],
        trace_id=values["trace_id"],
        task_created_at=values["task_created_at"],
        question_content=values["question_content"],
        question_redaction_count=values["question_redaction_count"],
        answer_delivered=values["answer_delivered"],
        answer_delivered_redaction_count=values["answer_delivered_redaction_count"],
        answer_model_raw=values["answer_model_raw"],
        answer_model_raw_redaction_count=values["answer_model_raw_redaction_count"],
        tool_calls=tuple(_tool_call_from_payload(item) for item in (tool_calls or [])),
        terminal_kind=values["terminal_kind"],
        user_result=values["user_result"],
        failure_code=values["failure_code"],
        output_safety_withheld=values["output_safety_withheld"],
        worker_id=values["worker_id"],
        worker_version=values["worker_version"],
        target_worker_version=values["target_worker_version"],
        system_prompt_digest=values["system_prompt_digest"],
        model=values["model"],
    )
    return QaCorpusRow(id=values["id"], created_at=values["created_at"], record=record)


def _checked_record(record: object) -> QaCorpusRecord:
    if not isinstance(record, QaCorpusRecord):
        raise TypeError("只能记录 QaCorpusRecord")
    return record


def record_qa_corpus(connection, record: QaCorpusRecord) -> bool:
    """在调用方事务内追加一行；``task_id`` 已有语料时不写、返回 ``False``。

    失败原样冒泡，由调用方决定是否回滚。
    """
    checked = _checked_record(record)
    with connection.cursor() as cursor:
        cursor.execute(_INSERT_SQL, _row_values(new_id("qac"), checked))
        return cursor.rowcount == 1


@dataclass(frozen=True)
class CorpusStats:
    """容量事实：行数与表总字节数（含索引与 TOAST），不含任何正文。"""

    rows: int
    total_bytes: int


def _filter_clause(chosen: CorpusFilter) -> tuple[str, list[Any]]:
    conditions: list[str] = []
    parameters: list[Any] = []
    if chosen.user_id is not None:
        conditions.append("user_id = %s")
        parameters.append(chosen.user_id)
    if chosen.since is not None:
        conditions.append("created_at >= %s")
        parameters.append(chosen.since)
    if chosen.until is not None:
        conditions.append("created_at < %s")
        parameters.append(chosen.until)
    if chosen.keyword is not None:
        pattern = like_pattern(chosen.keyword)
        conditions.append(_KEYWORD_SQL)
        parameters.extend((pattern, pattern))
    return " WHERE " + " AND ".join(conditions), parameters


def _checked_filter(chosen: object) -> CorpusFilter:
    if not isinstance(chosen, CorpusFilter):
        raise TypeError("过滤条件必须是 CorpusFilter")
    return chosen


class PostgresQaCorpus:
    """自开连接的写口、按任务回读口与读取侧查询口。构造时不连接数据库，每个方法自带事务。"""

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        """记下 DSN 与超时配置；不在构造时连接数据库。"""
        self._dsn = dsn
        self._timeouts = timeouts

    def record(self, record: QaCorpusRecord) -> bool:
        """追加一行并提交，返回这次是否真的写入（重领任务的第二次写入返回 ``False``）。"""
        checked = _checked_record(record)
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                return record_qa_corpus(connection, checked)

    def for_task(self, task_id: str) -> QaCorpusRow | None:
        """按任务标识回读那一行；测试与受控核对用，不是面向读取角色的检索入口。"""
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task_id 必须是非空字符串")
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.cursor() as cursor:
                cursor.execute(_SELECT_SQL + " WHERE task_id = %s", (task_id,))
                row = cursor.fetchone()
        return None if row is None else _row_from_values(row)

    def search(self, chosen: CorpusFilter, *, limit: int) -> tuple[QaCorpusRow, ...]:
        """按过滤条件取最新的若干行；``limit`` 过核心模块的硬顶。只发一条 ``SELECT``。"""
        where, parameters = _filter_clause(_checked_filter(chosen))
        bound = checked_read_limit(limit)
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.cursor() as cursor:
                cursor.execute(_SELECT_SQL + where + _ORDER_SQL + " LIMIT %s", (*parameters, bound))
                rows = cursor.fetchall()
        return tuple(_row_from_values(row) for row in rows)

    def iter_export(self, chosen: CorpusFilter) -> Iterator[QaCorpusRow]:
        """按同一过滤条件分批产出全部命中行，顺序同 :meth:`search`。

        键集翻页：每批带上一批末行的 ``(created_at, id)`` 作为上界，每批一条独立的短语句；
        表只追加不改，翻页期间新写入的行时刻更新、落在已翻过的上方，不会被重复或漏计。
        """
        where, parameters = _filter_clause(_checked_filter(chosen))
        boundary: tuple[datetime, str] | None = None
        while True:
            sql = _SELECT_SQL + where
            batch_parameters = list(parameters)
            if boundary is not None:
                sql += " AND (created_at, id) < (%s, %s)"
                batch_parameters.extend(boundary)
            with connect(self._dsn, timeouts=self._timeouts) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        sql + _ORDER_SQL + " LIMIT %s", (*batch_parameters, EXPORT_BATCH_SIZE)
                    )
                    rows = cursor.fetchall()
            for row in rows:
                yield _row_from_values(row)
            if len(rows) < EXPORT_BATCH_SIZE:
                return
            last = rows[-1]
            boundary = (last[_COLUMNS.index("created_at")], last[_COLUMNS.index("id")])

    def stats(self) -> CorpusStats:
        """行数与表总字节数；给容量水位与 ``stats`` 子命令用，不碰任何正文列。"""
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*), pg_total_relation_size('qa_corpus') FROM qa_corpus"
                )
                rows, total_bytes = cursor.fetchone()
        return CorpusStats(rows=int(rows), total_bytes=int(total_bytes))


def _reader_from_row(row: tuple) -> QaCorpusReaderEntry:
    values = dict(zip(_READER_COLUMNS, row, strict=True))
    return QaCorpusReaderEntry(**values)


def _checked_open_id(open_id: object) -> str:
    if not isinstance(open_id, str) or not open_id.strip():
        raise ValueError("open_id 必须是非空字符串")
    return open_id


def reader_entry(connection, open_id: str) -> QaCorpusReaderEntry | None:
    """在调用方连接上查一条 ``active`` 登记；没有就是 ``None``。每次都是新查询，不缓存。"""
    with connection.cursor() as cursor:
        cursor.execute(_READER_ACTIVE_SQL, (_checked_open_id(open_id),))
        row = cursor.fetchone()
    return None if row is None else _reader_from_row(row)


def grant_reader(
    connection, *, open_id: str, label: str, granted_by: str
) -> tuple[QaCorpusReaderEntry, bool]:
    """在调用方事务内授予读取角色；已有 ``active`` 登记时原样返回它、第二项为 ``False``。

    幂等靠部分唯一索引：``ON CONFLICT`` 命中即不插入，随后读回既有那行。
    """
    if not isinstance(label, str) or not label.strip():
        raise ValueError("label 必须是非空字符串")
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO qa_corpus_reader (id, feishu_open_id, label, granted_by)"
            " VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (feishu_open_id) WHERE entry_status = 'active' DO NOTHING",
            (new_id("qcr"), _checked_open_id(open_id), label, _checked_open_id(granted_by)),
        )
        created = cursor.rowcount == 1
    entry = reader_entry(connection, open_id)
    if entry is None:
        raise RuntimeError("授予后读不回 active 登记")
    return entry, created


def revoke_reader(connection, *, open_id: str) -> QaCorpusReaderEntry | None:
    """在调用方事务内撤销 ``active`` 登记并返回撤销后的那行；没有生效登记时返回 ``None``。"""
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE qa_corpus_reader SET entry_status = 'revoked', revoked_at = now()"
            " WHERE feishu_open_id = %s AND entry_status = 'active'"
            " RETURNING " + ", ".join(_READER_COLUMNS),
            (_checked_open_id(open_id),),
        )
        row = cursor.fetchone()
    return None if row is None else _reader_from_row(row)


class PostgresQaCorpusReaders:
    """窄读取角色表的自开连接读写口。

    授予与撤销都接收一个 ``audit`` 回调，在**同一事务**里由调用方写审计行：审计写不进去，
    角色变更一起回滚——「每次授予 / 撤销各留一行审计」不靠调用方记得补写。
    """

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        """记下 DSN 与超时配置；不在构造时连接数据库。"""
        self._dsn = dsn
        self._timeouts = timeouts

    def entry(self, open_id: str) -> QaCorpusReaderEntry | None:
        """当前 ``active`` 的登记；只读 ``qa_corpus_reader``，不碰语料表。"""
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            return reader_entry(connection, open_id)

    def grant(
        self,
        open_id: str,
        label: str,
        *,
        granted_by: str,
        audit: Callable[[Any, QaCorpusReaderEntry, bool], str],
    ) -> tuple[QaCorpusReaderEntry, bool, str]:
        """授予并在同一事务写审计；返回登记、是否新建、审计行标识。"""
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                entry, created = grant_reader(
                    connection, open_id=open_id, label=label, granted_by=granted_by
                )
                audit_id = audit(connection, entry, created)
        return entry, created, audit_id

    def revoke(
        self, open_id: str, *, audit: Callable[[Any, QaCorpusReaderEntry], str]
    ) -> tuple[QaCorpusReaderEntry, str] | None:
        """撤销并在同一事务写审计；没有生效登记时不写任何东西、返回 ``None``。"""
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                entry = revoke_reader(connection, open_id=open_id)
                if entry is None:
                    return None
                audit_id = audit(connection, entry)
        return entry, audit_id
