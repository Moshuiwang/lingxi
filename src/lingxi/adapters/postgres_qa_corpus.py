"""问答留存语料 ``qa_corpus`` 的落库：同事务追加、自开连接追加、按任务回读。

``record_qa_corpus(connection, record)`` 在调用方已经打开的事务里追加；``PostgresQaCorpus``
自开连接、自带事务，供 worker 收口这类没有业务事务可挂的调用方。写入按 ``task_id``
``ON CONFLICT DO NOTHING``：同一任务被重领、重试时不双写，返回值如实说这次有没有写进去。
按任务回读只服务测试与受控核对；面向读取角色的检索与导出是另一个工作项。

写入失败的处置权在调用方（``apps/worker/qa_corpus_capture.py`` 降级为一条只含任务标识
与异常类型名的日志）；本模块自己不兜底、不写日志——这张表每一行都是正文。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.ids import new_id
from lingxi.core.innertest_content_capture import CapturedToolCall
from lingxi.core.qa_corpus import QaCorpusRecord

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


class PostgresQaCorpus:
    """自开连接的写口与按任务回读口。构造时不连接数据库，每个方法自带事务。"""

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
