"""``task_document_delivery_request`` 的 Postgres 存取（迁移 0074，Issue #341 S-ES-3/R-2；
迁移 0078 新增 ``delivery_type``/``resource_url`` 两列，Issue #354 S-H3-2 表格分支；
迁移 0079 新增 ``markdown`` 列，Issue #408 正式方案接线）。

主要服务 gateway 侧独立消费循环（``apps/gateway/document_delivery.py``）：认领
``pending`` 行、在建档成功的那一刻单独提交检查点列 ``document_id``、把最终结果
（``succeeded``/``uncertain``/``failed``）写回。worker 侧的插入不在这里——那一步
在写终态事件的同一事务里完成（``adapters/postgres_conversation/_queue_outbox.py``
的 ``write_terminal_event``），这个模块从不插入新行，只认领与更新既有行。

**两个例外都是 scheduler 侧的定时职责调用，不是 gateway**（opus 审查 R-2/P1-3）：

- :meth:`PostgresDocumentDeliveryStore.fail_expired_pending`——补的是"gateway
  独立消费循环从未配置/未部署/已经整条死掉"这个 gateway 自己够不到的洞，必须
  由一个独立部署、独立崩溃域的进程兜底，见该方法自己的文档。
- :meth:`PostgresDocumentDeliveryStore.redact_expired_content`——``V-投递-06``
  的 24 小时正文到期擦除，与死信扫描是同一个轻量周期职责
  （``apps/scheduler/document_delivery_dead_letter.py``）里的两件事，不为一次
  ``UPDATE`` 语句另开一整个职责。

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

#: R-2 死信面（Issue #341 opus 审查）：``pending`` 行超过这个窗口仍未被任何
#: gateway 消费循环认领，就判定为死信——见 :meth:`PostgresDocumentDeliveryStore.
#: fail_expired_pending` 的完整理由。30 分钟远大于 gateway 消费循环正常的轮询
#: 间隔与单轮批量处理耗时（``DEFAULT_BATCH_LIMIT`` = 5 行、每行至多四次 20 秒的
#: 飞书 HTTP 调用），避免把"这一轮批次排在后面还没轮到"的正常等待误判成死信。
PENDING_DEAD_LETTER_AFTER = timedelta(minutes=30)

#: P2-2（opus 审查）：``succeeded`` 但"文档已就绪"通知还没确认送达
#: （``notified_at IS NULL``）超过这个窗口的行，gateway 消费循环每轮优先补发一次
#: ——见 :meth:`PostgresDocumentDeliveryStore.claim_unnotified_succeeded` 的完整
#: 理由。10 分钟远大于单次通知调用（``FeishuUserMessages.send_text``）的传输超时，
#: 给第一次尝试留出"可能只是瞬时抖动、下一刻自己就好了"的余量，避免把一次刚刚
#: 失败、还没到重试节奏的通知也纳入这一轮的补发批次。
NOTIFY_RETRY_AFTER = timedelta(minutes=10)


class DocumentDeliveryOwnershipLost(RuntimeError):
    """P1-2（opus 审查）：``mark_*`` 系列命中 0 行——这一行的持有权此刻已经不在
    发起本次调用的这个消费者手里。

    典型场景：一次慢速的飞书 HTTP 调用（``REQUEST_TIMEOUT_SECONDS`` = 20 秒/步、
    四步顺序执行）拖过了 ``STALE_PROCESSING_AFTER_SECONDS``（180 秒），
    :meth:`PostgresDocumentDeliveryStore.reclaim_stale_processing` 把这一行判定为
    "卡住"并回收——退回 ``pending`` 等下一次认领，或者（``attempts`` 已耗尽时）
    直接转 ``failed``。这个"慢消费者"随后自己也跑完了，却在写回结果时发现这一行
    早已不再是它认领时的那个 ``processing`` 状态：可能已经被另一次认领接手并跑出
    了不同的结果，也可能已经被判定失败清出了队列。

    此前四个 ``mark_*`` 的 ``UPDATE ... WHERE status = 'processing'`` 都不检查
    ``rowcount``——0 行时静默视为成功，慢消费者会在没有任何信号的情况下**继续往
    下走**：对同一份文档重复写正文、重复授权（``write_paragraphs``/
    ``grant_full_access`` 都没有幂等键），或者把一个已经有了真实终态的行覆盖判为
    自己那次（可能是错的）结论，还可能因为终态判据判成功而发出一条重复的"文档
    已就绪"通知。

    调用方（``apps/gateway/document_delivery.py``）收到这个信号必须**当场中止**
    本行续做——不写正文、不发通知，把这一行"现在究竟是什么状态"完全交给真正
    持有它的那次调用（或它已经落下的终态），见该模块 ``_process_claim`` 的
    对应分支与模块说明。
    """

    def __init__(self, request_id: str) -> None:
        super().__init__(f"task_document_delivery_request id={request_id} 的持有权已丢失")
        self.request_id = request_id


#: 支持的交付类型（迁移 0078 CHECK 同一取值集合）：``docx`` 走
#: ``adapters/feishu_docx_delivery.py``，``sheet`` 走
#: ``adapters/feishu_sheets_delivery.py``（Issue #354 S-H3-2）。
DELIVERY_TYPE_DOCX = "docx"
DELIVERY_TYPE_SHEET = "sheet"


@dataclass(frozen=True)
class DocumentDeliveryClaim:
    """gateway 消费循环认领到的一行文档/表格投递请求。

    ``paragraphs``：docx 时是段落文本数组；sheet 时是行×列的单元格文本二维数组
    （复用同一列存两种内容形状，理由见迁移 0078 文件头部）——调用方
    （``apps/gateway/document_delivery.py``）按 ``delivery_type`` 决定怎么解读。
    ``document_id``：docx 时是 ``document_id``，sheet 时是 ``spreadsheet_token``
    ——同样是复用同一列的检查点标识，见迁移 0078 文件头部「为什么复用」一节。

    ``markdown``（迁移 0079，Issue #408 正式方案接线）：docx 类型的原始 markdown
    全文，``NULL`` 即"这一行没有可转换的原文"（历史行、或 sheet 类型——sheet
    恒为 ``NULL``，迁移 0079 的 CHECK 约束）。gateway 侧
    （``apps/gateway/document_delivery.py::_create_docx_body``）据此在"服务端
    一次建档写全文"与"两步段落路径"之间选择：非 ``None`` 才有资格走一次建档，
    ``None`` 一律回退段落路径——与止损闸是否打开无关，两个条件都满足才会真正
    调用 :meth:`~lingxi.adapters.feishu_docx_delivery.LarkDocxDelivery.
    create_document_with_markdown`。
    """

    id: str
    task_id: str
    requester_open_id: str
    title: str
    paragraphs: tuple[Any, ...]
    document_id: str | None
    attempts: int
    # 默认值指向 docx（迁移 0078 该列自身的 DEFAULT 'docx'）：保持既有直接构造
    # `DocumentDeliveryClaim(...)` 的调用点（本模块之外，测试里手工搭 claim 的
    # 场景）不必因为新增这两个字段而逐个改写——docx 是修改前唯一存在的类型，
    # 默认成它就是"不传等价于旧行为"。
    delivery_type: str = DELIVERY_TYPE_DOCX
    resource_url: str | None = None
    # 默认 None（迁移 0079 该列自身的默认值）：既有直接构造
    # `DocumentDeliveryClaim(...)` 的调用点不传这个字段即等价于"没有 markdown
    # 原文"，与新增列之前逐字相同的行为。
    markdown: str | None = None
    # 迁移 0082（Issue #499）：非 None 即"这一行的正文已经被降级成纯文本段落
    # 路径写入"，取值是原因码。**认领时就要读出来**，因为检查点恢复路径
    # （`read_body_children` 判定正文已写、直接跳过写正文步）永远不会再产生一次
    # 降级信号，拿不到那次调用的内存值——不从库里带进来，这条路径就会发出
    # 不带降级说明的"文档已生成"，退回成裁定明令消灭的静默降级。默认 None 让
    # 既有直接构造 `DocumentDeliveryClaim(...)` 的调用点（测试）零改动。
    body_degraded_reason: str | None = None


@dataclass(frozen=True)
class UnnotifiedSuccess:
    """P2-2：一行已经 ``succeeded`` 但"文档已就绪"通知还没确认送达的请求——
    :meth:`PostgresDocumentDeliveryStore.claim_unnotified_succeeded` 的返回单元。
    只带补发通知这一件事所需的最小字段，不是 :class:`DocumentDeliveryClaim` 的
    子集（``title``/``paragraphs``/``attempts`` 与补发无关）。

    ``delivery_type``/``resource_url``（迁移 0078，Issue #354 S-H3-2）：补发通知
    时要拼哪种链接同样要看类型——docx 本地拼接（不需要 ``resource_url``），
    sheet 直接读这一列（建表时已经落检查点，见
    :meth:`PostgresDocumentDeliveryStore.mark_document_created`）。

    ``body_degraded_reason``（迁移 0082，Issue #499）：补发时要选哪条文案同样
    要看它——非 ``None`` 说明这一行的正文是降级写进去的，必须补发**明示降级**
    的那条就绪文案，而不是普通就绪文案。
    """

    id: str
    task_id: str
    requester_open_id: str
    document_id: str
    delivery_type: str
    resource_url: str | None
    # 迁移 0082（Issue #499）：补发通知是另一次进程调用，看不到原发送路径那次
    # 写正文的内存信号——不带上这一列，补发出去的就是不含"格式已简化"
    # 说明的就绪通知（静默降级）。
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
        ``document_id``——第一次成功写入的那个值才是真正建出来的那篇文档/表格。

        ``resource_url``（迁移 0078，Issue #354 S-H3-2）：**只有 sheet 分支传
        非 None**——sheet 的链接由建表响应直接给出，docx 分支永远不传（保持
        默认 ``None``，docx 调用点零改动，行为逐字不变）。同一条 ``UPDATE``
        原子写入两列，不需要第二次数据库往返。

        P1-2（opus 审查）：命中 0 行时抛出 :class:`DocumentDeliveryOwnershipLost`
        ——见该类文档"慢消费者"场景。此前静默无视 0 行，会让一次迟到的建档检查点
        提交在持有权已经转移之后继续覆盖/绕过后续判断。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE task_document_delivery_request
                   SET document_id = %s, resource_url = %s, updated_at = now()
                 WHERE id = %s AND status = 'processing' AND document_id IS NULL
                """,
                (document_id, resource_url, request_id),
            )
            if cursor.rowcount != 1:
                raise DocumentDeliveryOwnershipLost(request_id)

    def mark_body_degraded(self, *, request_id: str, reason: str) -> None:
        """检查点：正文已经**降级**写入（迁移 0082，Issue #499）——落
        ``body_degraded_reason``，单独提交，不与后续"授权/读回/落终态"共享事务。

        姿态与 :meth:`mark_document_created` 一致，理由也一致：这是一件"外部
        已经发生、本地必须记住"的事实，晚一步提交就可能被一次崩溃带走。差别
        只在守卫——建档那一步用 ``document_id IS NULL`` 防止覆盖已经建出来的
        文档标识；这里不需要那道守卫，因为同一行的降级原因码在同一次交付里只
        会由唯一一次写正文判定产生，重复写入同一个值是幂等的。

        ``status = 'processing'`` 守卫保留：命中 0 行说明这一行的持有权已经不在
        本次调用手里（典型是被 ``reclaim_stale_processing`` 回收过的慢消费者），
        抛出 :class:`DocumentDeliveryOwnershipLost` 让调用方当场中止，不继续
        授权/读回/通知——同 :meth:`mark_document_created` 的 P1-2 处置。

        **不在这里顺手把 ``NULL`` 写回去**：本方法只在真的降级时被调用，没有
        "取消降级"这个动作。一行的正文写过就不会再写第二遍（检查点幂等判据），
        因此不存在"上次降级、这次没降级"需要清位的场景。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE task_document_delivery_request
                   SET body_degraded_reason = %s, updated_at = now()
                 WHERE id = %s AND status = 'processing'
                """,
                (reason, request_id),
            )
            if cursor.rowcount != 1:
                raise DocumentDeliveryOwnershipLost(request_id)

    def mark_succeeded(self, *, request_id: str) -> None:
        """read_members 已确认目标 open_id 具备 full_access：终态 succeeded。

        P1-2：命中 0 行时抛出 :class:`DocumentDeliveryOwnershipLost`。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE task_document_delivery_request
                   SET status = 'succeeded', permission_confirmed_at = now(), updated_at = now()
                 WHERE id = %s AND status = 'processing'
                """,
                (request_id,),
            )
            if cursor.rowcount != 1:
                raise DocumentDeliveryOwnershipLost(request_id)

    def claim_unnotified_succeeded(
        self, *, limit: int, older_than: timedelta = NOTIFY_RETRY_AFTER
    ) -> list[UnnotifiedSuccess]:
        """P2-2（opus 审查）：取 ``succeeded`` 但通知还没确认送达
        （``notified_at IS NULL``）、且已经过了退避窗口的行，供 gateway 消费循环
        每轮**优先**补发。

        与四步流程的"认领"不同，这里**不需要**原子转态/``FOR UPDATE SKIP
        LOCKED``——``mark_notified`` 的 ``WHERE ... AND notified_at IS NULL`` 本身
        就是幂等闸：即使两次调用都读到同一行、都尝试补发，第二次调用的
        ``mark_notified`` 会因为第一次已经置位而命中 0 行，不会把同一条通知的
        "已确认送达"状态覆盖成两次独立的确认（真正的重复发送风险在飞书那一侧的
        ``dedupe_key`` 去重，不在这里）。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
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
        """"文档已就绪"通知确认送达：置位 ``notified_at``（P2-2）。

        ``WHERE ... AND notified_at IS NULL`` 是幂等闸——已经置过位的行再次调用
        是无害的 no-op（0 行），不覆盖第一次确认的时间戳。命中 0 行不视为
        :class:`DocumentDeliveryOwnershipLost`：``succeeded`` 是终态，不存在
        "持有权被别的消费者抢走"这个场景，0 行只可能是"已经补发过了"或"这一行
        已经不是 succeeded"（理论上不会发生，succeeded 是终态），两种情形都不需要
        调用方做任何补救，静默返回即可。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
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

        P1-2：命中 0 行时抛出 :class:`DocumentDeliveryOwnershipLost`。
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
            if cursor.rowcount != 1:
                raise DocumentDeliveryOwnershipLost(request_id)

    def mark_failed(self, *, request_id: str, last_error: str) -> None:
        """飞书明确拒绝（业务错误码）：终态 failed。

        P1-2：命中 0 行时抛出 :class:`DocumentDeliveryOwnershipLost`。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE task_document_delivery_request
                   SET status = 'failed', last_error = %s, updated_at = now()
                 WHERE id = %s AND status = 'processing'
                """,
                (last_error, request_id),
            )
            if cursor.rowcount != 1:
                raise DocumentDeliveryOwnershipLost(request_id)

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

    def fail_expired_pending(
        self, *, older_than: timedelta = PENDING_DEAD_LETTER_AFTER
    ) -> int:
        """R-2 死信面（opus 审查，Issue #341）：把停在 ``pending`` 超过
        ``older_than`` 仍未被任何消费循环认领的行直接转终态 ``failed``
        （``last_error = 'pending_expired_unconsumed'``）。

        与 :meth:`fail_exhausted_pending` 补的是**不同的洞**：那个方法处理的是
        "已经被认领过、attempts 耗尽"的行，前提是 gateway 侧消费循环本来就在跑。
        这个方法处理的是 gateway 独立消费循环（``apps/gateway/document_delivery.
        py``）**从未配置、进程未部署、或已经整条死掉**的情形——那种情况下
        ``attempts`` 永远是 0（``claim_pending`` 从未认领过这一行），
        :meth:`fail_exhausted_pending`/:meth:`reclaim_stale_processing` 的判据都
        摸不到它，行会无限期停在 ``pending``，用户永远等不到文档也永远等不到一句
        "失败了"。调用方是 scheduler 的定时职责（
        ``apps/scheduler/document_delivery_dead_letter.py``）——scheduler 进程与
        gateway 进程各自独立部署、独立崩溃域，只要 scheduler 还活着，这条死信面
        就恒在，不依赖 gateway 是否配置或存活。

        由 ``UPDATE ... WHERE status = 'pending' ...`` 单条原子语句完成，不需要
        ``FOR UPDATE SKIP LOCKED``——判据（``created_at`` 早于窗口）与
        :meth:`claim_pending` 的认领判据（``status = 'pending'``）互斥更新的是
        同一批候选行的交集，Postgres 行级锁本身已经保证不会有第二个并发写者
        同时把同一行既转 ``processing`` 又转 ``failed``。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
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
        """``V-投递-06``（opus 审查 P1-3）：把过了 24 小时上限（迁移 0074 的触发器
        锁定的 ``content_expires_at``）的正文擦空，返回擦除条数。

        擦的是 ``title``/``paragraphs``/``markdown``（问数结果生成的文档标题、正文
        与原始 markdown 全文——迁移 0079 新增的 ``markdown`` 是"原始正文"的另一种
        形态，信息量不小于 ``paragraphs``，同一次擦除必须一起清，不能只擦段落列却
        把原文留在库里过期不清）；``status``/``document_id``/``attempts``/
        ``last_error``/时间戳留下——它们是"谁在什么时候请求过一份文档、结果如何"
        这类运行事实，本身不含用户资料（迁移 0082 的
        ``body_degraded_reason`` 同属这一类：一个固定枚举形状的原因码，不含任何
        用户内容，因此同样不在擦除范围内），形状照 ``adapters/postgres_permission_
        publish.py`` 的 ``redact_expired_payloads``（那张表擦 ``payload`` 成
        ``'{}'::jsonb``，这里擦成空字符串/空数组/``NULL``——三者都是迁移
        0074/0079 那条形状 CHECK 认可的"擦除态"；``markdown`` 擦成 ``NULL`` 而不是
        空字符串，与"从未提供 markdown"是同一个可表达状态，不需要为擦除态另设
        哨兵值）。

        **不区分 succeeded/uncertain/failed/pending**：``V-投递-06`` 覆盖的是"待
        投递、失败、``uncertain`` 或尚未证明清除后可访问的正文"，24 小时上限对
        全部终态与非终态一视同仁——已经 ``succeeded`` 的文档正文本身也已经在飞书
        那一侧独立留存（这里存的只是曾经交付过什么的运行记录，不是唯一副本）。
        """

        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
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
    "DocumentDeliveryOwnershipLost",
    "PostgresDocumentDeliveryStore",
    "UnnotifiedSuccess",
]
