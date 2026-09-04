"""``task_document_delivery_request`` 的 Postgres 存取。

主要服务 gateway 侧独立消费循环：认领 ``pending`` 行、在建档成功的那一刻单独
提交检查点列 ``document_id``、把最终结果写回。worker 侧的插入不在这里，这个
模块从不插入新行，只认领与更新既有行。

**两个例外都是 scheduler 侧的定时职责调用，不是 gateway**：``fail_expired_
pending``（补 gateway 独立消费循环从未配置/未部署/已经整条死掉这个洞）与
``redact_expired_content``（``V-投递-06`` 的 24 小时正文到期擦除）。

**检查点纪律**：:meth:`mark_document_created` 是一次独立提交，不与其余动作
共享事务——飞书建文档没有幂等键，成功后必须立刻单独落盘。认领用单条
``UPDATE ... WHERE status='pending' ... RETURNING`` 配合 ``FOR UPDATE SKIP
LOCKED``，让并发的多个 gateway 实例天然互斥。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect

#: 崩溃恢复重试上限：认领时超过这个 attempts 计数的 pending 行不再参与认领，
#: 由 :meth:`PostgresDocumentDeliveryStore.fail_exhausted_pending` 直接转终态
#: ``failed``——避免一行反复卡在建档中途的请求无限期占用消费循环。不是"重试
#: 次数"本身的产品承诺，是消费循环自身的止损。
MAX_CLAIM_ATTEMPTS = 5

#: 认领后判定为"卡住"（消费进程崩溃、四步执行到一半、从未落任何终态更新）的静默
#: 窗口。取值远大于单次真实飞书 HTTP 调用的超时上限
#: （``feishu_docx_delivery.REQUEST_TIMEOUT_SECONDS`` = 20 秒）：四步顺序执行、
#: 每步都可能各自超时一次，留出数倍余量避免把正常处理中的行误判为卡住。
STALE_PROCESSING_AFTER_SECONDS = 180

#: 死信面：``pending`` 行超过这个窗口仍未被任何 gateway 消费循环认领，就判定为
#: 死信——见 :meth:`PostgresDocumentDeliveryStore.fail_expired_pending` 的完整
#: 理由。30 分钟远大于 gateway 消费循环正常的轮询间隔与单轮批量处理耗时，避免
#: 把"这一轮批次排在后面还没轮到"的正常等待误判成死信。
PENDING_DEAD_LETTER_AFTER = timedelta(minutes=30)

#: ``succeeded`` 但"文档已就绪"通知还没确认送达（``notified_at IS NULL``）超过
#: 这个窗口的行，gateway 消费循环每轮优先补发一次——见
#: :meth:`PostgresDocumentDeliveryStore.claim_unnotified_succeeded` 的完整理由。
#: 10 分钟远大于单次通知调用的传输超时，给第一次尝试留出"可能只是瞬时抖动、
#: 下一刻自己就好了"的余量。
NOTIFY_RETRY_AFTER = timedelta(minutes=10)


class DocumentDeliveryOwnershipLostError(RuntimeError):
    """``mark_*`` 系列命中 0 行：这一行的持有权此刻已经不在本次调用手里。

    典型场景：一次慢速的飞书 HTTP 调用拖过了
    ``STALE_PROCESSING_AFTER_SECONDS``，:meth:`PostgresDocumentDeliveryStore.
    reclaim_stale_processing` 把这一行判定为"卡住"并回收；这个慢消费者随后
    自己也跑完了，写回结果时发现这一行早已不再是它认领时的状态。不检查
    ``rowcount`` 会让慢消费者继续重复写正文、重复授权，或覆盖一个已有真实
    终态的行。调用方收到这个信号必须当场中止本行续做。
    """

    def __init__(self, request_id: str) -> None:
        """记下丢失持有权的请求标识，并生成对应的异常消息。"""
        super().__init__(f"task_document_delivery_request id={request_id} 的持有权已丢失")
        self.request_id = request_id


#: 兼容别名：调用点（`apps/gateway/document_delivery.py`）仍按旧名导入（N818
#: 改名，收官批统一清）。
DocumentDeliveryOwnershipLost = DocumentDeliveryOwnershipLostError


#: 支持的交付类型（迁移 0078 CHECK 同一取值集合）：``docx`` 走
#: ``adapters/feishu_docx_delivery.py``，``sheet`` 走
#: ``adapters/feishu_sheets_delivery.py``。
DELIVERY_TYPE_DOCX = "docx"
DELIVERY_TYPE_SHEET = "sheet"


@dataclass(frozen=True)
class DocumentDeliveryClaim:
    """gateway 消费循环认领到的一行文档/表格投递请求。

    ``paragraphs``：docx 时是段落文本数组，sheet 时是行×列的单元格文本二维数组
    （复用同一列存两种内容形状）——调用方按 ``delivery_type`` 决定怎么解读。
    ``document_id``：docx 时是 ``document_id``，sheet 时是 ``spreadsheet_token``，
    同样是复用同一列的检查点标识。``markdown``：docx 类型的原始全文，``NULL``
    即没有可转换的原文（历史行，或 sheet 类型恒为 ``NULL``）；gateway 侧据此在
    "服务端一次建档写全文"与"两步段落路径"之间选择，非 ``None`` 才有资格走
    一次建档。
    """

    id: str
    task_id: str
    requester_open_id: str
    title: str
    paragraphs: tuple[Any, ...]
    document_id: str | None
    attempts: int
    # 默认值指向 docx（该列自身的 DEFAULT 'docx'）：既有直接构造
    # `DocumentDeliveryClaim(...)` 的调用点不必因为新增字段而逐个改写。
    delivery_type: str = DELIVERY_TYPE_DOCX
    resource_url: str | None = None
    # 默认 None（该列自身的默认值）：不传这个字段即等价于"没有 markdown 原文"。
    markdown: str | None = None
    # 非 None 即"这一行的正文已经被降级成纯文本段落路径写入"，取值是原因码。
    # 认领时就要读出来：检查点恢复路径（正文已写、直接跳过写正文步）永远不会
    # 再产生一次降级信号，不从库里带进来就会发出不带降级说明的"文档已生成"。
    body_degraded_reason: str | None = None


@dataclass(frozen=True)
class UnnotifiedSuccess:
    """一行已经 ``succeeded`` 但"文档已就绪"通知还没确认送达的请求。

    :meth:`PostgresDocumentDeliveryStore.claim_unnotified_succeeded` 的返回
    单元，只带补发通知所需的最小字段。``delivery_type``/``resource_url``：
    补发要拼哪种链接同样看类型——docx 本地拼接，sheet 直接读这一列。
    ``body_degraded_reason``：非 ``None`` 说明这一行的正文是降级写进去的，
    必须补发明示降级的那条就绪文案，而不是普通就绪文案。
    """

    id: str
    task_id: str
    requester_open_id: str
    document_id: str
    delivery_type: str
    resource_url: str | None
    # 补发通知是另一次进程调用，看不到原发送路径那次写正文的内存信号——不带上
    # 这一列，补发出去的就是不含"格式已简化"说明的就绪通知（静默降级）。
    body_degraded_reason: str | None = None


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
        delivery_type=row[7],
        resource_url=row[8],
        markdown=row[9],
        body_degraded_reason=row[10],
    )


class PostgresDocumentDeliveryStore:
    """``task_document_delivery_request`` 的唯一读写实现（gateway 侧消费循环用）。"""

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        """记下 DSN 与超时配置；不在构造时连接数据库。"""
        self._dsn = dsn
        self._timeouts = timeouts

    def claim_pending(
        self, *, limit: int, max_attempts: int = MAX_CLAIM_ATTEMPTS
    ) -> list[DocumentDeliveryClaim]:
        """认领至多 ``limit`` 行 ``pending``，原子转 ``processing`` 并自增 ``attempts``。

        ``attempts < max_attempts`` 排除已经耗尽重试预算的行——它们只能由
        :meth:`fail_exhausted_pending` 转终态 ``failed``，不会被这里认领到。
        """
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
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
                RETURNING id, task_id, requester_open_id, title, paragraphs, document_id, attempts,
                          delivery_type, resource_url, markdown, body_degraded_reason
                """,
                (max_attempts, limit),
            )
            return [_row_to_claim(row) for row in cursor.fetchall()]

    def mark_document_created(
        self, *, request_id: str, document_id: str, resource_url: str | None = None
    ) -> None:
        """检查点：建档/建表成功后单独提交，不与其余动作共享事务。

        ``document_id IS NULL`` 守卫（而不仅是 ``status = 'processing'``）：即使
        因为某种竞态被调用第二次，也不会用一次新的调用覆盖已经持久化的
        ``document_id``。``resource_url`` 只有 sheet 分支传非 None（docx 分支
        永远不传，保持默认 ``None``），同一条 ``UPDATE`` 原子写入两列。命中
        0 行时抛出 :class:`DocumentDeliveryOwnershipLostError`（见该类文档"慢消费者"
        场景），避免一次迟到的建档检查点提交在持有权已经转移之后继续覆盖。
        """
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                UPDATE task_document_delivery_request
                   SET document_id = %s, resource_url = %s, updated_at = now()
                 WHERE id = %s AND status = 'processing' AND document_id IS NULL
                """,
                (document_id, resource_url, request_id),
            )
            if cursor.rowcount != 1:
                raise DocumentDeliveryOwnershipLostError(request_id)

    def mark_body_degraded(self, *, request_id: str, reason: str) -> None:
        """检查点：正文已经**降级**写入——落 ``body_degraded_reason``，单独提交。

        姿态与 :meth:`mark_document_created` 一致：外部已经发生、本地必须记住
        的事实，晚一步提交就可能被一次崩溃带走。差别在守卫：这里不需要
        ``document_id IS NULL`` 那种防重复覆盖的守卫，因为同一行的降级原因码
        在同一次交付里只会由唯一一次写正文判定产生，重复写入同一个值是幂等的；
        ``status = 'processing'`` 守卫仍保留，命中 0 行同样抛出
        :class:`DocumentDeliveryOwnershipLostError`。不在这里把 ``NULL`` 写回去：
        本方法只在真的降级时被调用，没有"取消降级"这个动作。
        """
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                UPDATE task_document_delivery_request
                   SET body_degraded_reason = %s, updated_at = now()
                 WHERE id = %s AND status = 'processing'
                """,
                (reason, request_id),
            )
            if cursor.rowcount != 1:
                raise DocumentDeliveryOwnershipLostError(request_id)

    def mark_succeeded(self, *, request_id: str) -> None:
        """read_members 已确认目标 open_id 具备 full_access：终态 succeeded。

        P1-2：命中 0 行时抛出 :class:`DocumentDeliveryOwnershipLostError`。
        """
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                UPDATE task_document_delivery_request
                   SET status = 'succeeded', permission_confirmed_at = now(), updated_at = now()
                 WHERE id = %s AND status = 'processing'
                """,
                (request_id,),
            )
            if cursor.rowcount != 1:
                raise DocumentDeliveryOwnershipLostError(request_id)

    def claim_unnotified_succeeded(
        self, *, limit: int, older_than: timedelta = NOTIFY_RETRY_AFTER
    ) -> list[UnnotifiedSuccess]:
        """取 ``succeeded`` 但通知未确认送达、且已过退避窗口的行，供 gateway 优先补发。

        与四步流程的"认领"不同，这里不需要原子转态/``FOR UPDATE SKIP LOCKED``
        ——``mark_notified`` 的 ``WHERE ... AND notified_at IS NULL`` 本身就是
        幂等闸：即使两次调用都读到同一行都尝试补发，第二次调用会因为第一次
        已经置位而命中 0 行，不会把"已确认送达"覆盖成两次独立确认。
        """
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                SELECT id, task_id, requester_open_id, document_id, delivery_type, resource_url,
                       body_degraded_reason
                  FROM task_document_delivery_request
                 WHERE status = 'succeeded' AND notified_at IS NULL
                   AND document_id IS NOT NULL
                   AND updated_at < now() - %s::interval
                 ORDER BY updated_at
                 LIMIT %s
                """,
                (older_than, limit),
            )
            return [UnnotifiedSuccess(*row) for row in cursor.fetchall()]

    def mark_notified(self, *, request_id: str) -> None:
        """通知"文档已就绪"确认送达：置位 ``notified_at``。

        ``WHERE ... AND notified_at IS NULL`` 是幂等闸——已经置过位的行再次调用
        是无害的 no-op（0 行），不覆盖第一次确认的时间戳。命中 0 行不视为
        :class:`DocumentDeliveryOwnershipLostError`：``succeeded`` 是终态，不存在
        "持有权被别的消费者抢走"这个场景，0 行只可能是"已经补发过了"或"这一行
        已经不是 succeeded"（理论上不会发生，succeeded 是终态），两种情形都不需要
        调用方做任何补救，静默返回即可。
        """
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                UPDATE task_document_delivery_request
                   SET notified_at = now(), updated_at = now()
                 WHERE id = %s AND status = 'succeeded' AND notified_at IS NULL
                """,
                (request_id,),
            )

    def mark_uncertain(self, *, request_id: str, last_error: str) -> None:
        """结果不明（网络类异常、或读回不含目标/权限档位不对）：终态 uncertain。

        V-交付-03：``uncertain`` 不会被消费循环自动重试——见
        ``apps/gateway/document_delivery.py`` 的模块说明。

        P1-2：命中 0 行时抛出 :class:`DocumentDeliveryOwnershipLostError`。
        """
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                UPDATE task_document_delivery_request
                   SET status = 'uncertain', last_error = %s, updated_at = now()
                 WHERE id = %s AND status = 'processing'
                """,
                (last_error, request_id),
            )
            if cursor.rowcount != 1:
                raise DocumentDeliveryOwnershipLostError(request_id)

    def mark_failed(self, *, request_id: str, last_error: str) -> None:
        """飞书明确拒绝（业务错误码）：终态 failed。

        P1-2：命中 0 行时抛出 :class:`DocumentDeliveryOwnershipLostError`。
        """
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                UPDATE task_document_delivery_request
                   SET status = 'failed', last_error = %s, updated_at = now()
                 WHERE id = %s AND status = 'processing'
                """,
                (last_error, request_id),
            )
            if cursor.rowcount != 1:
                raise DocumentDeliveryOwnershipLostError(request_id)

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
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                UPDATE task_document_delivery_request
                   SET status = 'failed', last_error = 'attempts_exhausted', updated_at = now()
                 WHERE status = 'pending' AND attempts >= %s
                """,
                (max_attempts,),
            )
            return cursor.rowcount

    def fail_expired_pending(self, *, older_than: timedelta = PENDING_DEAD_LETTER_AFTER) -> int:
        """死信面：把停在 ``pending`` 超过 ``older_than`` 仍未被认领的行转终态 ``failed``。

        与 :meth:`fail_exhausted_pending` 补的是不同的洞：那个方法处理的是
        "已经被认领过、attempts 耗尽"的行，前提是 gateway 侧消费循环本来就在跑；
        这个方法处理的是 gateway 独立消费循环从未配置、进程未部署、或已经整条
        死掉的情形——那种情况下 ``attempts`` 永远是 0，行会无限期停在
        ``pending``。调用方是 scheduler 的定时职责，独立部署、独立崩溃域，只要
        scheduler 还活着这条死信面就恒在。单条原子 ``UPDATE``，不需要
        ``FOR UPDATE SKIP LOCKED``——判据与 :meth:`claim_pending` 互斥。
        """
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                UPDATE task_document_delivery_request
                   SET status = 'failed', last_error = 'pending_expired_unconsumed', updated_at = now()
                 WHERE status = 'pending' AND created_at < now() - %s::interval
                """,
                (older_than,),
            )
            return cursor.rowcount

    def redact_expired_content(self, *, limit: int = 500) -> int:
        """``V-投递-06``：把过了 24 小时上限的正文擦空，返回擦除条数。

        擦的是 ``title``/``paragraphs``/``markdown``（文档标题、正文与原始
        markdown 全文，同一次擦除必须一起清）；``status``/``document_id``/
        ``attempts``/``last_error``/时间戳留下——它们是运行事实，本身不含用户
        资料。``markdown`` 擦成 ``NULL`` 而不是空字符串，与"从未提供 markdown"
        是同一个可表达状态。**不区分 succeeded/uncertain/failed/pending**：
        已 ``succeeded`` 的文档正文本身也已在飞书侧独立留存。
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                UPDATE task_document_delivery_request
                   SET title = '', paragraphs = '[]'::jsonb, markdown = NULL, updated_at = now()
                 WHERE id IN (
                         SELECT id FROM task_document_delivery_request
                          WHERE content_expires_at <= now() AND title <> ''
                          ORDER BY content_expires_at
                          LIMIT %s
                       )
                """,
                (limit,),
            )
            return cursor.rowcount


__all__ = [
    "DELIVERY_TYPE_DOCX",
    "DELIVERY_TYPE_SHEET",
    "MAX_CLAIM_ATTEMPTS",
    "NOTIFY_RETRY_AFTER",
    "PENDING_DEAD_LETTER_AFTER",
    "STALE_PROCESSING_AFTER_SECONDS",
    "DocumentDeliveryClaim",
    "DocumentDeliveryOwnershipLostError",
    "PostgresDocumentDeliveryStore",
    "UnnotifiedSuccess",
]
