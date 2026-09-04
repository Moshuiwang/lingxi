"""内测轮内容级采集的落库适配器。

只有一个写路径：把 ``core/innertest_content_capture.py`` 已经构造好的
``ContentCaptureRecord``（凭据形状已过滤、按裁定口径全量保留问题/回答原文）原样
写进 ``innertest_content_capture`` 表，不做任何脱敏或裁剪——那是 core 层唯一的
职责边界，避免同一份内容在两个层各自过滤一遍、口径逐渐漂移。

写入失败的处置权在调用方：``apps/worker/service.py`` 把整次调用包在
``try/except`` 里降级为审计日志、不影响任务主流程；本模块自己不再重复兜底，
调用失败就是真的失败、原样向上抛。``read_recent_for_task`` 只服务测试与受控
运维核对，不是面向使用者的查询界面。九十天到期删除在
:mod:`lingxi.adapters.postgres_content_capture_retention`，不在本模块——那条
删除路径的调用方是 scheduler，没有理由背上写入侧拉进的 ``core.execution`` 依赖。
"""

from __future__ import annotations

from typing import Any

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.ids import new_id
from lingxi.core.innertest_content_capture import ContentCaptureRecord


class PostgresContentCaptureWriter:
    """写 ``innertest_content_capture`` 表的唯一入口。"""

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        """记下 DSN 与超时配置；不在构造时连接数据库。"""
        self._dsn = dsn
        self._timeouts = timeouts

    def write(self, record: ContentCaptureRecord) -> str:
        """插入一条采集记录，返回新行的内部标识（``icc_`` 前缀 ULID）。

        不做幂等去重：调用方在一次任务处理里只调用一次（见
        ``apps/worker/service.py`` 的 ``_process_task``），没有"同一次逻辑写入
        被重试"的场景，与 ``task_delivery_event`` 需要 ``idempotency_key`` 的
        场景不同。
        """
        row_id = new_id("icc")
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    """
                    INSERT INTO innertest_content_capture
                        (id, task_id, worker_id, question_content,
                         question_redaction_count, answer_content,
                         answer_redaction_count, tool_calls,
                         tool_calls_redaction_count)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        row_id,
                        record.task_id,
                        record.worker_id,
                        record.question_content,
                        record.question_redaction_count,
                        record.answer_content,
                        record.answer_redaction_count,
                        _jsonb(record.tool_calls_payload()),
                        record.tool_calls_redaction_count,
                    ),
                )
        return row_id

    def read_recent_for_task(self, task_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """按 ``task_id`` 回读，测试与受控核对用；不是面向使用者的查询界面。"""
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    """
                    SELECT id, task_id, worker_id, question_content,
                           question_redaction_count, answer_content,
                           answer_redaction_count, tool_calls,
                           tool_calls_redaction_count, created_at, expires_at
                    FROM innertest_content_capture
                    WHERE task_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (task_id, limit),
                )
                rows = cursor.fetchall()
        return [
            {
                "id": row[0],
                "task_id": row[1],
                "worker_id": row[2],
                "question_content": row[3],
                "question_redaction_count": row[4],
                "answer_content": row[5],
                "answer_redaction_count": row[6],
                "tool_calls": row[7],
                "tool_calls_redaction_count": row[8],
                "created_at": row[9],
                "expires_at": row[10],
            }
            for row in rows
        ]


def _jsonb(value: Any) -> Any:
    """把 JSON 安全结构交给 psycopg 的 JSONB 适配。延迟导入：没有驱动的机器仍能 import 本模块。"""
    from psycopg.types.json import Jsonb

    return Jsonb(value)
