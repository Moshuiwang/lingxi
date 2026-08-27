"""``task_document_delivery_request`` 的 Postgres 存取（迁移 0074，Issue #341 S-ES-3）。

只服务 gateway 侧独立消费循环（``apps/gateway/document_delivery.py``）：认领
``pending`` 行、在建档成功的那一刻单独提交检查点列 ``document_id``、把最终结果
（``succeeded``/``uncertain``/``failed``）写回。worker 侧的插入不在这里——那一步
在写终态事件的同一事务里完成（``adapters/postgres_conversation/_queue_outbox.py``
的 ``write_terminal_event``），这个模块从不插入新行，只认领与更新既有行。

**检查点纪律**（见迁移 0074 文件头部）：:meth:`mark_document_created` 是一次独立
提交，与四步里的其余动作不共享事务——飞书建文档没有幂等键，一旦调用成功就必须
立刻、单独地把 ``document_id`` 落盘，崩溃重启后的续做逻辑只看这一列是否非空来
判断"是否已经建过档"，绝不二次调用。

**认领用 ``UPDATE ... WHERE status='pending' ... RETURNING``**，不是"先 SELECT
再 UPDATE"两条语句——单条原子语句配合 ``FOR UPDATE SKIP LOCKED`` 让并发的多个
gateway 实例（理论上不会真的并发部署多份，但不假设这一点）天然互斥，不会出现
两边都认领到同一行的双认领。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.ids import new_id

#: 崩溃恢复重试上限（#341 评论 5434520679 审定设计第 5 条）：认领时超过这个
#: attempts 计数的 pending 行不再参与认领，由 :meth:`PostgresDocumentDeliveryStore.
#: fail_exhausted_pending` 直接转终态 ``failed``——避免一行反复卡在建档中途的
#: 请求无限期占用消费循环。不是"重试次数"本身的产品承诺，是消费循环自身的止损。
MAX_CLAIM_ATTEMPTS = 5

#: 认领后判定为"卡住"（消费进程崩溃、四步执行到一半、从未落任何终态更新）的静默
#: 窗口。取值远大于单次真实飞书 HTTP 调用的超时上限
#: （``feishu_docx_delivery.REQUEST_TIMEOUT_SECONDS`` = 20 秒）：四步顺序执行、
#: 每步都可能各自超时一次，留出数倍余量避免把正常处理中的行误判为卡住。
STALE_PROCESSING_AFTER_SECONDS = 180


@dataclass(frozen=True)
class DocumentDeliveryClaim:
    """gateway 消费循环认领到的一行文档投递请求。"""

    id: str
    task_id: str
    requester_open_id: str
    title: str
    paragraphs: tuple[str, ...]
    document_id: str | None
    attempts: int


def _row_to_claim(row: tuple[Any, ...]) -> DocumentDeliveryClaim:
    paragraphs = row[4]
    return DocumentDeliveryClaim(
        id=row[0],
        task_id=row[1],
        requester_open_id=row[2],
        title=row[3],
        paragraphs=tuple(paragraphs) if isinstance(paragraphs, list) else tuple(),
        document_id=row[5],
        attempts=row[6],
    )


class PostgresDocumentDeliveryStore:
    """``task_document_delivery_request`` 的唯一读写实现（gateway 侧消费循环用）。"""

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        self._dsn = dsn
        self._timeouts = timeouts

    def claim_pending(
        self, *, limit: int, max_attempts: int = MAX_CLAIM_ATTEMPTS
    ) -> list[DocumentDeliveryClaim]:
        """认领至多 ``limit`` 行 ``pending``，原子转 ``processing`` 并自增 ``attempts``。

        ``attempts < max_attempts`` 排除已经耗尽重试预算的行——它们只能由
        :meth:`fail_exhausted_pending` 转终态 ``failed``，不会被这里认领到。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE task_document_delivery_request
                   SET status = 'processing', attempts = attempts + 1, updated_at = now()
                 WHERE id IN (
                     SELECT id FROM task_document_delivery_request
                      WHERE status = 'pending' AND attempts < %s
                      ORDER BY created_at
                        FOR UPDATE SKIP LOCKED
                      LIMIT %s
                 )
                RETURNING id, task_id, requester_open_id, title, paragraphs, document_id, attempts
                """,
                (max_attempts, limit),
            )
            return [_row_to_claim(row) for row in cursor.fetchall()]

    def mark_document_created(self, *, request_id: str, document_id: str) -> None:
        """检查点：建档成功后单独提交，不与四步里的其余动作共享事务。

        ``document_id IS NULL`` 守卫（而不仅是 ``status = 'processing'``）：即使
        因为某种竞态被调用第二次，也不会用一次新的调用覆盖已经持久化的
        ``document_id``——第一次成功写入的那个值才是真正建出来的那篇文档。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE task_document_delivery_request
                   SET document_id = %s, updated_at = now()
                 WHERE id = %s AND status = 'processing' AND document_id IS NULL
                """,
                (document_id, request_id),
            )

    def mark_succeeded(self, *, request_id: str) -> None:
        """read_members 已确认目标 open_id 具备 full_access：终态 succeeded。"""

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE task_document_delivery_request
                   SET status = 'succeeded', permission_confirmed_at = now(), updated_at = now()
                 WHERE id = %s AND status = 'processing'
                """,
                (request_id,),
            )

    def mark_uncertain(self, *, request_id: str, last_error: str) -> None:
        """结果不明（网络类异常、或读回不含目标/权限档位不对）：终态 uncertain。

        V-交付-03：``uncertain`` 不会被消费循环自动重试——见
        ``apps/gateway/document_delivery.py`` 的模块说明。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE task_document_delivery_request
                   SET status = 'uncertain', last_error = %s, updated_at = now()
                 WHERE id = %s AND status = 'processing'
                """,
                (last_error, request_id),
            )

    def mark_failed(self, *, request_id: str, last_error: str) -> None:
        """飞书明确拒绝（业务错误码）：终态 failed。"""

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE task_document_delivery_request
                   SET status = 'failed', last_error = %s, updated_at = now()
                 WHERE id = %s AND status = 'processing'
                """,
                (last_error, request_id),
            )

    def reclaim_stale_processing(
        self,
        *,
        older_than: timedelta = timedelta(seconds=STALE_PROCESSING_AFTER_SECONDS),
        max_attempts: int = MAX_CLAIM_ATTEMPTS,
    ) -> tuple[int, int]:
        """回收卡在 ``processing`` 太久的行（消费进程崩溃、从未落任何终态）。

        与 ``apps/worker/service.py`` 的 ``reclaim_stale_with_outcomes`` 同一
        姿态：``attempts``（认领时已经自增过）未超上限的退回 ``pending`` 等待
        下一次认领——``document_id`` 若已经在崩溃前完成检查点提交，续做时会跳过
        重新建档（见模块说明）；超过上限的直接终态 ``failed``，不再无限期占用
        消费循环。返回 ``(退回 pending 的行数, 转 failed 的行数)``。
        """

        requeued = 0
        failed = 0
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    """
                    SELECT id, attempts FROM task_document_delivery_request
                     WHERE status = 'processing' AND updated_at < now() - %s::interval
                     ORDER BY updated_at
                     FOR UPDATE SKIP LOCKED
                    """,
                    (older_than,),
                )
                rows = cursor.fetchall()
                for request_id, attempts in rows:
                    if attempts >= max_attempts:
                        cursor.execute(
                            """
                            UPDATE task_document_delivery_request
                               SET status = 'failed', last_error = 'attempts_exhausted', updated_at = now()
                             WHERE id = %s AND status = 'processing'
                            """,
                            (request_id,),
                        )
                        failed += 1
                    else:
                        cursor.execute(
                            """
                            UPDATE task_document_delivery_request
                               SET status = 'pending', updated_at = now()
                             WHERE id = %s AND status = 'processing'
                            """,
                            (request_id,),
                        )
                        requeued += 1
        return requeued, failed

    def fail_exhausted_pending(self, *, max_attempts: int = MAX_CLAIM_ATTEMPTS) -> int:
        """把 attempts 已经耗尽、却仍然停在 ``pending`` 的行直接转终态 ``failed``。

        正常情况下这类行不会出现——:meth:`claim_pending` 的 ``attempts <
        max_attempts`` 谓词让它们不会被再次认领，真正把 attempts 推过上限的只有
        :meth:`reclaim_stale_processing`（回收时已经直接判定转 ``failed``，不会
        经过这条路径再退回 ``pending``）。这里是纵深防线：任何未来改动如果不小心
        在别处把一行卡耗尽 attempts 的行留在 ``pending``，本方法保证它最终会被
        清出消费循环，而不是无限期占着索引却永远认领不到。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE task_document_delivery_request
                   SET status = 'failed', last_error = 'attempts_exhausted', updated_at = now()
                 WHERE status = 'pending' AND attempts >= %s
                """,
                (max_attempts,),
            )
            return cursor.rowcount


__all__ = [
    "MAX_CLAIM_ATTEMPTS",
    "STALE_PROCESSING_AFTER_SECONDS",
    "DocumentDeliveryClaim",
    "PostgresDocumentDeliveryStore",
]
