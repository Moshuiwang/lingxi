"""投递 outbox（Issue #239 从 ``postgres_conversation.PostgresTaskQueue`` 按读写
边界拆分而来）：Worker 侧写事件（``append_delivery_event``/``write_terminal_event``）、
Gateway 侧确认送达（``confirm_delivery``）、二十四小时到期强制收敛
（``expire_undelivered_terminals``），以及三类会话边界触发共用的投递正文清除与
两小时空闲扫描——原文件里这几个方法紧跟在同一段「投递事件 outbox（Issue #151）」
注释之后，没有另起小节，因此归为同一条读写边界。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from lingxi.adapters.postgres import connect
from lingxi.core.delivery.ports import (
    DeliveryEventType,
    TerminalKind,
    assert_content_allowed,
    resolve_delivered_outcome,
)
from lingxi.core.ids import new_id

from ._dataclasses import AppendedEvent, TerminalTask
from ._transaction import _Transaction

logger = logging.getLogger(__name__)

# ``sweep_idle_conversations`` 每次扫描新增排队的会话数上限（PR #173 独立复核
# P2-5）：查询本身已经用 NOT EXISTS 把候选集收窄到"尚未排过队"的那些，这里只是
# 给单次扫描一个防护上限，避免一次异常的大批量话题同时到点时单条查询/单次事务
# 处理过多行。500 是与 Worker 侧消费能力（`claim_session_cleanups` 默认单批 20、
# 每约 2 秒的收口轮询一次）比较后留出的宽裕量，不是精确调校值。
_IDLE_SESSION_CLEANUP_SWEEP_LIMIT = 500


def _jsonb_or_none(value: Mapping[str, int] | None) -> Any:
    """``task.token_usage``（迁移 ``0070``）的参数适配：``None`` 必须原样作为
    Python ``None`` 传给 psycopg，写出真正的 SQL ``NULL``——``Jsonb(None)`` 写的
    是 JSON 字面量 ``null``（``'null'::jsonb``），那是一个"已知值为空"，与
    ``IS NULL``（"取不到"）在 SQL 里可以被区分但语义完全不同，会让
    ``adapters/postgres_daily_report.py`` 的 ``WHERE token_usage IS NOT NULL``
    把"结构性取不到"误判成"取到了、值是空对象"。延迟导入 ``Jsonb``：与仓库既有
    惯例（``adapters/postgres_content_capture.py`` 等）一致，没有驱动的机器仍能
    import 本模块。
    """

    if value is None:
        return None
    from psycopg.types.json import Jsonb

    return Jsonb(dict(value))


class _OutboxMixin:
    # -----------------------------------------------------------------
    # 投递事件 outbox（Issue #151）
    #
    # append_delivery_event（非终态）与 write_terminal_event（终态）共用同一套
    # 所有权与顺序保证：先 `SELECT ... FOR UPDATE` 锁定 task 行、核对 `worker_id`
    # 与 `status`，再在同一把锁下计算 `MAX(sequence)+1`——两个并发写者
    # 因此天然串行化，不需要额外的咨询锁（与数据库设计第五节「同话题串行靠条件
    # 更新」同一手法，只是这里锁的是行而不是靠影响行数判断）。
    # 幂等由调用方提供的 idempotency_key 承担：命中已有行时原样返回该行的
    # sequence 并标记 duplicate=True，不创建第二条事件、不重复触发调用方的
    # 副作用计数（V-投递-01/02）。
    # -----------------------------------------------------------------

    def append_delivery_event(
        self,
        *,
        task_id: str,
        worker_id: str,
        event_type: str,
        idempotency_key: str,
        elapsed_seconds: int | None = None,
        content: str | None = None,
    ) -> AppendedEvent | None:
        """写入 ``started``/``progress``/``safely_releasable_answer`` 事件。

        只有当前持有该任务的 worker（``worker_id`` 匹配且 ``status='running'``）能
        写；否则返回 ``None``——僵尸 worker 或已经离开 running 态的迟到写入必须被
        拒绝，不能悄悄创建游离事件。``content`` 必须是调用方已经过 #149 安全检查、
        允许展示给当前用户的文本；本方法不做安全判断，只负责持久化。终态请使用
        :meth:`write_terminal_event`（需要同时转移任务状态，语义不同）。

        **幂等判定先于所有权判定**：如果 ``idempotency_key`` 已经写过，即使此刻
        任务已经离开 ``running``（例如同一次终态写入之后又收到一次迟到的
        进度重试），也原样返回那一行并标记 ``duplicate=True``——这正是"重放一次
        已经成功的写入"该有的行为，不能因为状态已经前进就报告失败。只有
        ``idempotency_key`` 全新时才需要真正的所有权校验，防止僵尸 worker 借着
        一个新 key 悄悄插入游离事件。
        """

        if event_type not in (
            DeliveryEventType.STARTED.value,
            DeliveryEventType.PROGRESS.value,
            DeliveryEventType.SAFELY_RELEASABLE_ANSWER.value,
        ):
            raise ValueError("append_delivery_event 只处理非终态事件类型")
        # 写入前自查（Issue #328 opus 审查 R1）：在真正打开数据库连接之前就用
        # ``CONTENT_BEARING_EVENT_TYPES``/``PROGRESS_CONTENT_MAX_LENGTH`` 校验
        # 一遍，命中问题时抛出一个可读的 ``ValueError``——数据库层的 CHECK 仍然
        # 是最终防线，这里只是让"写库前就能发现"，不依赖调用方自己记得遵守合同。
        assert_content_allowed(DeliveryEventType(event_type), content)
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    "SELECT worker_id, status FROM task WHERE id = %s FOR UPDATE",
                    (task_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                existing = self._find_by_idempotency_key(cursor, idempotency_key)
                if existing is not None:
                    return existing
                if row[0] != worker_id or row[1] != "running":
                    return None
                return self._insert_new_event(
                    cursor,
                    task_id=task_id,
                    worker_id=worker_id,
                    event_type=event_type,
                    idempotency_key=idempotency_key,
                    terminal_kind=None,
                    error_kind=None,
                    elapsed_seconds=elapsed_seconds,
                    content=content,
                )

    def write_terminal_event(
        self,
        *,
        task_id: str,
        worker_id: str,
        terminal_kind: str,
        error_kind: str | None,
        content: str | None,
        elapsed_seconds: int | None = None,
        agent_session_id: str | None = None,
        token_usage: Mapping[str, int] | None = None,
        guard_denied_count: int | None = None,
        failure_code: str | None = None,
        failure_signature: str | None = None,
        document_request: Mapping[str, Any] | None = None,
        sheet_request: Mapping[str, Any] | None = None,
    ) -> AppendedEvent | None:
        """写入 ``terminal`` 事件并把任务从 ``running`` 转为 ``awaiting_delivery``。

        与任务状态转换在**同一事务**提交或整体回滚（状态合同第 2 条）；
        ``conversation.running_task_id`` 在这里**不释放**——话题继续占用直到投递
        解析（:meth:`confirm_delivery` 或 :meth:`expire_undelivered_terminals`）。
        返回 ``None`` 表示当前调用方已不再持有该任务（僵尸 worker、任务已被回收
        或重复收口），调用方不应据此产生第二次用户可见的副作用。

        幂等判定同样先于所有权判定（见 :meth:`append_delivery_event` 的说明）：
        重复调用（同一次写入的网络重试）即使任务此刻已经不在 ``running``（正是
        第一次调用成功转移之后的样子），也应原样返回 ``duplicate=True``，而不是
        误判成"所有权已丢失"返回 ``None``。

        ``token_usage``/``guard_denied_count``（迁移 ``0070``，Issue #303/#304
        批次 4）：通报补数——落 ``task.token_usage``/``task.guard_denied_count``
        两列，供 ``adapters/postgres_daily_report.py`` 聚合，脱离
        ``core/daily_report.py`` 此前"恒不可判定"的两段。``None`` 是精确语义
        （这次真的取不到，见迁移 ``0070`` 文件头部），不编造 0 或空对象——调用方
        （``apps/worker/service.py``）已经按同一纪律区分"取到 0"与"取不到"，这里
        原样透传，不做二次判断。``token_usage`` 是一个只含四个已知 token 计数
        字段的普通 dict（不含 ``status``/``source`` 信封，见迁移文件头部），由
        ``_jsonb_or_none`` 适配成 ``JSONB``。

        ``document_request``（迁移 ``0074``，Issue #341 S-ES-3；迁移 ``0079``
        新增可选 ``markdown``，Issue #408 正式方案接线）：``None`` 或
        ``{"title": str, "paragraphs": list[str], "markdown": str | None}``——
        ``apps/worker/service.py``
        只在这一轮终态是 :attr:`~lingxi.core.delivery.ports.TerminalKind.SUCCESS`
        且报告契约的 ``document_request`` 字段非空时才传非 ``None``。非
        ``None`` 时插入一行 ``task_document_delivery_request``（状态
        ``pending``），供 gateway 侧独立消费循环认领；``requester_open_id`` 取自
        这个任务的提问用户（``task.user_id`` JOIN ``app_user.feishu_open_id``）。
        非成功终态传非 ``None`` 是调用方的契约错误，这里直接拒绝（失败关闭，不
        悄悄按成功处理插入一行）。

        ``sheet_request``（迁移 ``0078``，Issue #354 S-H3-2 表格分支）：``None``
        或 ``{"title": str, "rows": list[list[str]]}``——与 ``document_request``
        同一机制新增的并列分支，不是第二套独立通道：同一张表
        （``task_document_delivery_request``）、同一条 ``UNIQUE(task_id)`` 幂等键、
        同一个 SAVEPOINT 隔离、同一条"非成功终态不得传非 None"契约。调用方
        （``apps/worker/turn.py`` 的回合级请求槽位，见该模块「表格分支」文档）
        保证同一次调用 ``document_request``/``sheet_request`` 至多一个非
        ``None``——这里仍然响亮校验这条不变式（纵深防线，不假设上游一定守约）：
        两者都非 ``None`` 视为调用方契约错误，直接拒绝，不猜测该用哪一个。

        ``failure_code``/``failure_signature``（迁移 ``0080``，Issue #495）：落
        ``task.failure_code``/``task.failure_signature`` 两列。前者是这次终态的
        细分失败码（``task.error_kind`` 是被 ``_failure_content`` 压平成用户文案
        分类之后的粗粒度值，``drain_timeout``/``sdk_unavailable``/``cancelled``/
        ``gate_bypassed`` 全都塌进同一个 ``session_failed``，落库之后再也分不
        开）；后者是底层异常收敛出的**固定形状摘要**（`exception.<类别>.<摘要>`——模块名与
        限定类名只进 SHA-256 输入，不落库），是"未分类失败"唯一留得下的线索。
        两者都可空，``NULL`` 是精确语义（这次失败不来自异常、或这一轮压根不是
        失败），不编造占位符。**异常正文永远不落这两列**——``V-花名册-33`` 禁止
        外部标识原值进审计与日志，psycopg 的异常串常见形状正是
        ``DETAIL: Key (feishu_open_id)=(ou_...)``；收敛口径见
        ``apps/worker/report_extraction.py`` 的失败签名一节。

        **这一插入套在一个独立的 SAVEPOINT 里（P2-3，opus 审查），不与终态事件/
        task 状态转移共享失败命运**：文档请求插入失败（结构性、会重复发生的
        情形——例如提问用户 ``app_user.feishu_open_id`` 为 ``NULL``，如组织资料
        同步专用账号）只会回滚这个 SAVEPOINT 本身，降级为"这次问数没有文档"，
        终态答案照常提交，用户仍然拿到他的问数结果；失败记一条响亮审计
        （``worker.document_request_insert_failed``），不吞声。**之前一版会让
        这类失败连坐整个事务回滚**——一次纯粹是"文档交付这个附加功能插不进去"
        的失败，代价是用户连本来已经产生的正常问数答案都拿不到，这与产品合同
        "重启/重试不得造成用户结果丢失"的红线直接冲突。
        """

        if document_request is not None and terminal_kind != TerminalKind.SUCCESS.value:
            raise ValueError("document_request 只能在 terminal_kind='success' 时提供")
        if sheet_request is not None and terminal_kind != TerminalKind.SUCCESS.value:
            raise ValueError("sheet_request 只能在 terminal_kind='success' 时提供")
        if document_request is not None and sheet_request is not None:
            raise ValueError("document_request 与 sheet_request 不能同时提供")

        idempotency_key = f"{task_id}:terminal"
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    "SELECT worker_id, status FROM task WHERE id = %s FOR UPDATE",
                    (task_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                existing = self._find_by_idempotency_key(cursor, idempotency_key)
                if existing is not None:
                    # 已经写过 terminal：拒绝第二条有效终态（V-投递-02），任务状态
                    # 不再改动——上一次写入时已经完成过这个转移。
                    return existing
                if row[0] != worker_id or row[1] != "running":
                    return None
                appended = self._insert_new_event(
                    cursor,
                    task_id=task_id,
                    worker_id=worker_id,
                    event_type=DeliveryEventType.TERMINAL.value,
                    idempotency_key=idempotency_key,
                    terminal_kind=terminal_kind,
                    error_kind=error_kind,
                    elapsed_seconds=elapsed_seconds,
                    content=content,
                    agent_session_id=agent_session_id,
                )
                cursor.execute(
                    """
                    UPDATE task
                       SET status = 'awaiting_delivery',
                           error_kind = COALESCE(%s, error_kind),
                           ended_at = now(),
                           token_usage = %s,
                           guard_denied_count = %s,
                           failure_code = %s,
                           failure_signature = %s
                     WHERE id = %s AND worker_id = %s AND status = 'running'
                    """,
                    (
                        error_kind,
                        _jsonb_or_none(token_usage),
                        guard_denied_count,
                        failure_code,
                        failure_signature,
                        task_id,
                        worker_id,
                    ),
                )
                if cursor.rowcount != 1:
                    # 上面的 FOR UPDATE 已经锁定并校验过持有者与状态；到这里还失败
                    # 说明状态机被绕过，宁可响亮失败也不要悄悄不释放/不占用。
                    raise RuntimeError(f"任务 {task_id} 在写终态事件时状态发生了竞态")
                # P2-3（Trace #373 H3 批量审查）：不能用 `document_request or
                # sheet_request` 判定——`{}` 是合法的非 None 值但布尔求值为假，
                # `or` 会把它当成"没提供"悄悄跳过下面整段插入与失败审计
                # （`worker.document_request_insert_failed` 永远不会为这类输入
                # 触发）。改成显式 `is not None`，`{}` 会照常走到
                # `_insert_document_delivery_request`（因缺少 title/内容字段被
                # 拒绝为 ValueError），命中下面的 `except` 分支记一条响亮审计，
                # 不再静默失踪。
                delivery_request = (
                    document_request if document_request is not None else sheet_request
                )
                delivery_type = (
                    ("docx" if document_request is not None else "sheet")
                    if delivery_request is not None
                    else None
                )
                if delivery_request is not None and not appended.duplicate:
                    # 只在这次真正插入了新终态事件（不是幂等重试命中已有行）时才
                    # 插入文档/表格投递请求——``duplicate=True`` 分支在上面已经
                    # 直接 ``return existing``，走不到这里；这个判断是防御性的，
                    # 理由与本方法其余分支一致：不能让一次网络重试悄悄产生第二行。
                    #
                    # P2-3（opus 审查）：这一步套一层 SAVEPOINT（嵌套
                    # ``connection.transaction()``，psycopg3 在已有外层事务时
                    # 自动降级为 SAVEPOINT/RELEASE/ROLLBACK TO SAVEPOINT），
                    # **不能让文档/表格请求插入失败连坐这次问数已经真实产生的
                    # 终态答案**——上面两条语句（终态事件、task 状态转移到
                    # ``awaiting_delivery``）已经在同一个数据库往返里成立，用户
                    # 已经"拿到答案"是这一刻唯一该被保证的事实。已知会命中这条
                    # 路径的情形：请求发起用户的 ``app_user.feishu_open_id`` 为
                    # ``NULL``（如组织资料同步的专用账号，见
                    # ``onboarding.delegated_subject`` 文案）——迁移 0074 的
                    # ``requester_open_id`` CHECK 会拒绝这类行，属于结构性、
                    # 会重复发生的失败，不是瞬时抖动，因此**不重试**，只降级为
                    # "这次问数没有文档/表格"并响亮记一条审计，让运维能追出
                    # "这个用户请求过文档/表格但没生成"。
                    try:
                        with connection.transaction():
                            self._insert_document_delivery_request(
                                cursor,
                                task_id=task_id,
                                delivery_request=delivery_request,
                                delivery_type=delivery_type,
                            )
                    except Exception as error:  # 降级但绝不吞声
                        # 审计事件名沿用既有 ``worker.document_request_insert_failed``
                        # ——docx 观测口径不变；``delivery_type`` 是新增的附加字段
                        # （Issue #354 S-H3-2），只是多一个可过滤维度，不改变事件
                        # 本身的含义或触发条件。
                        logger.error(
                            "worker.document_request_insert_failed task_id=%s delivery_type=%s error=%s",
                            task_id,
                            delivery_type,
                            type(error).__name__,
                        )
                return appended

    @staticmethod
    def _insert_document_delivery_request(
        cursor: Any, *, task_id: str, delivery_request: Mapping[str, Any], delivery_type: str
    ) -> None:
        """插入一行 ``pending`` 的文档/表格投递请求（迁移 ``0074`` 建表，迁移
        ``0078`` 加 ``delivery_type`` 列，迁移 ``0079`` 加 ``markdown`` 列，
        Issue #341 S-ES-3 / #354 S-H3-2 / #408 正式方案接线）。

        ``delivery_type='docx'`` 时 ``delivery_request`` 形状是
        ``{"title", "paragraphs", "markdown"}``（``markdown`` 见下）；
        ``delivery_type='sheet'`` 时形状是 ``{"title", "rows"}``——两者的第二个
        字段名不同（``paragraphs`` 是段落文本数组，``rows`` 是行×列的单元格
        文本二维数组），但都落进同一个 ``paragraphs`` JSONB 列（复用理由见迁移
        0078 文件头部「为什么复用」）。

        ``markdown``（迁移 0079，Issue #408 正式方案接线）：只在
        ``delivery_type='docx'`` 时读取并落库——``sheet`` 类型没有"markdown
        排版"这个概念，迁移 0079 的 CHECK（``delivery_type = 'docx' OR markdown
        IS NULL``）在数据库层面也会拒绝把这一列的值写进 sheet 行，这里提前按
        类型收窄是纵深防线，不是唯一防线。取不到（缺失、非字符串、空字符串）
        一律落 ``NULL``——不拒绝整条请求：``markdown`` 是段落之外的附加值，
        gateway 侧读到 ``NULL`` 会回退段落路径（零行为变化），比因为这一个
        可选字段让用户连基础的段落交付都拿不到更保守。

        ``requester_open_id`` 直接在 SQL 里用 ``task.user_id`` JOIN
        ``app_user.feishu_open_id`` 求值，不在应用层多打一次往返——调用方
        （:meth:`write_terminal_event`）已经在同一个事务、同一把锁下持有这一行
        ``task``，这里复用同一次数据库往返即可拿到当时的收件人身份，避免"先查
        一次 open_id 再插入"之间出现应用层可见但数据库不可见的窗口（虽然实际上
        ``app_user`` 一旦建档不会被物理删除，这个窗口理论上不产生数据后果，但
        单条语句仍是更简单、更少猜测的写法）。

        ``task_id`` 有 ``UNIQUE`` 约束（迁移 0074）：万一因为应用层 bug 被调用
        第二次，这里让唯一约束冲突原样抛出、整个事务回滚——宁可响亮失败也不要
        用 ``ON CONFLICT DO NOTHING`` 悄悄吞掉一次不该发生的重复插入（真正的
        幂等保护在上面的 ``appended.duplicate`` 判断，这里是纵深防线）。
        """

        if delivery_type not in ("docx", "sheet"):
            raise ValueError(f"delivery_type 必须是 docx 或 sheet，收到：{delivery_type!r}")
        content_field = "paragraphs" if delivery_type == "docx" else "rows"
        title = delivery_request.get("title")
        content = delivery_request.get(content_field)
        if not isinstance(title, str) or not title:
            raise ValueError(f"{delivery_type}_request.title 必须是非空字符串")
        if not isinstance(content, (list, tuple)) or not content:
            raise ValueError(f"{delivery_type}_request.{content_field} 必须是非空列表")

        markdown = delivery_request.get("markdown") if delivery_type == "docx" else None
        if not isinstance(markdown, str) or not markdown:
            markdown = None

        from psycopg.types.json import Jsonb

        cursor.execute(
            """
            INSERT INTO task_document_delivery_request
                (id, task_id, requester_open_id, title, paragraphs, delivery_type, markdown)
            SELECT %s, t.id, u.feishu_open_id, %s, %s, %s, %s
              FROM task AS t
              JOIN app_user AS u ON u.id = t.user_id
             WHERE t.id = %s
            """,
            (new_id("tdd"), title, Jsonb(list(content)), delivery_type, markdown, task_id),
        )
        if cursor.rowcount != 1:
            # task 行已经在调用方的 FOR UPDATE 里被锁定、确认存在；到这里插不进
            # 去只可能是 app_user 那一侧的 JOIN 没有命中（结构性不应发生：task.
            # user_id 有外键约束指向 app_user），宁可响亮失败也不要悄悄不建这行。
            raise RuntimeError(f"任务 {task_id} 写文档/表格投递请求时未能关联到发起用户")

    @staticmethod
    def _find_by_idempotency_key(cursor: Any, idempotency_key: str) -> AppendedEvent | None:
        """已存在则返回该行的 ``sequence`` 并标记 ``duplicate=True``；否则 ``None``。

        调用方必须先用 ``SELECT ... FOR UPDATE`` 锁定对应的 task 行，保证同一任务
        的并发写者在这里天然串行化，不会看到彼此尚未提交的插入。
        """

        cursor.execute(
            "SELECT sequence FROM task_delivery_event WHERE idempotency_key = %s",
            (idempotency_key,),
        )
        existing = cursor.fetchone()
        if existing is None:
            return None
        return AppendedEvent(sequence=existing[0], duplicate=True)

    @staticmethod
    def _insert_new_event(
        cursor: Any,
        *,
        task_id: str,
        worker_id: str,
        event_type: str,
        idempotency_key: str,
        terminal_kind: str | None,
        error_kind: str | None,
        elapsed_seconds: int | None,
        content: str | None,
        agent_session_id: str | None = None,
    ) -> AppendedEvent:
        """插入一条**确定尚不存在**的新事件；调用方必须已经确认
        ``idempotency_key`` 不重复、且已经用 ``SELECT ... FOR UPDATE`` 锁定了对应
        的 task 行（保证 ``sequence`` 的计算不会与并发写者相互覆盖）。
        """

        cursor.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM task_delivery_event WHERE task_id = %s",
            (task_id,),
        )
        next_sequence = cursor.fetchone()[0]
        cursor.execute(
            """
            INSERT INTO task_delivery_event
                (id, task_id, sequence, event_type, terminal_kind, error_kind,
                 elapsed_seconds, content, worker_id, idempotency_key, agent_session_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                new_id("tde"),
                task_id,
                next_sequence,
                event_type,
                terminal_kind,
                error_kind,
                elapsed_seconds,
                content,
                worker_id,
                idempotency_key,
                agent_session_id,
            ),
        )
        return AppendedEvent(sequence=next_sequence, duplicate=False)

    def confirm_delivery(
        self,
        *,
        task_id: str,
        platform_message_kind: str,
        platform_message_id: str,
    ) -> bool:
        """记录 ``platform_received`` 并收口投递：解析业务终态、释放话题。

        这是 Gateway 消费 outbox 后调用的冻结接口（Issue #151 与 #152 之间的调用
        合同）；本 Story 只实现并在 L2 真库验证它，不接入任何生产调用方——真正
        在成功送达后调用它属于 #152（Gateway 消费循环），不在本 Story 范围内。

        业务终态完全来自写终态事件时记录的 ``terminal_kind``/``error_kind``——
        投递确认成功不改写业务结果（`V-投递-04`）：业务失败的任务在这里仍然收敛
        为 ``failed``，不会因为飞书接受了失败卡片就变成 ``succeeded``。

        返回值只有两种合法含义：``True`` 表示三条写入（事件确认、任务收敛、
        会话回写）已经**全部**提交；``False`` 表示第一步查询就没找到可确认的
        东西（任务已经不在 ``awaiting_delivery``，或没有尚未确认的 terminal
        事件）——这种情况下**没有任何写入发生**。``conversation`` 更新失败
        （``running_task_id`` 与预期不符，通常意味着有其他路径已经在这中间改动
        了这个话题）不会走到这两种含义里的任何一种：它是内部不变量被破坏，
        与写终态事件时 ``task`` 行的 ``rowcount != 1`` 检查同一处理方式——整个
        事务 ``raise`` 回滚，不悄悄提交前两步再返回一个和"什么都没确认到"
        看起来一样的 ``False``。此前这里对 ``conversation`` 更新的失败只是
        ``return cursor.rowcount == 1``，会在事件与任务都已提交之后，把
        ``agent_session_id`` 静默丢弃、只留一个无法区分含义的 ``False``
        （内审 P2-4，真库负向用例见
        ``test_confirm_delivery_rolls_back_entirely_when_the_conversation_write_conflicts``）。
        """

        if platform_message_kind not in ("card", "text"):
            raise ValueError("platform_message_kind 只能是 card 或 text")
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    """
                    SELECT t.conversation_id, e.terminal_kind, e.error_kind, e.agent_session_id
                      FROM task AS t
                      JOIN task_delivery_event AS e
                        ON e.task_id = t.id AND e.event_type = 'terminal'
                     WHERE t.id = %s AND t.status = 'awaiting_delivery'
                       AND e.platform_received_at IS NULL
                     FOR UPDATE OF t
                    """,
                    (task_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return False
                conversation_id, terminal_kind, event_error_kind, agent_session_id = row
                outcome = resolve_delivered_outcome(
                    terminal_kind=terminal_kind, error_kind=event_error_kind
                )
                cursor.execute(
                    """
                    UPDATE task_delivery_event
                       SET platform_received_at = now(),
                           platform_message_kind = %s,
                           platform_message_id = %s
                     WHERE task_id = %s AND event_type = 'terminal'
                    """,
                    (platform_message_kind, platform_message_id, task_id),
                )
                cursor.execute(
                    """
                    UPDATE task SET status = %s, error_kind = %s
                     WHERE id = %s AND status = 'awaiting_delivery'
                    """,
                    (outcome.status, outcome.error_kind, task_id),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"任务 {task_id} 在确认送达时状态发生了竞态")
                # 只在业务成功时才有 agent_session_id；COALESCE 保证失败/停止/拒发
                # 终态不会把已有的会话延续状态清空——这些终态压根没有产生新会话，
                # 或者产生的会话按上面的取舍不该被继续使用。
                cursor.execute(
                    """
                    WITH target AS (
                        SELECT id, user_id, agent_session_id AS previous_session_id
                          FROM conversation
                         WHERE id = %s AND running_task_id = %s
                         FOR UPDATE
                    )
                    UPDATE conversation AS c
                       SET running_task_id = NULL,
                           last_task_ended_at = now(),
                           agent_session_id = COALESCE(%s, target.previous_session_id)
                      FROM target
                     WHERE c.id = target.id
                    RETURNING target.user_id, target.previous_session_id
                    """,
                    (conversation_id, task_id, agent_session_id),
                )
                conversation_row = cursor.fetchone()
                if conversation_row is None:
                    # 上面两条写入（事件确认、task 收敛）已经在这个事务里执行，
                    # 但还没提交：整个 with 块以异常退出，psycopg 回滚整个事务，
                    # 三条写入因此要么全部不生效。宁可响亮失败也不要悄悄丢弃
                    # agent_session_id、或者用一个和"没有可确认的东西"含义相同
                    # 的 False 掩盖"已经确认到一半"的竞态（内审 P2-4）。
                    raise RuntimeError(
                        f"任务 {task_id} 确认送达时 conversation {conversation_id} "
                        "的话题占用状态发生了竞态"
                    )
                self._queue_overwritten_session(
                    cursor,
                    user_id=conversation_row[0],
                    previous_session_id=conversation_row[1],
                    new_session_id=agent_session_id,
                )
                return True

    def expire_undelivered_terminals(self) -> list[TerminalTask]:
        """二十四小时到期仍未确认送达：强制收敛为 ``failed``/``delivery_expired``，
        释放话题，清空事件正文，只留低敏事实（状态合同第 8 条、`V-投递-06`）。

        无论原始业务结论是什么都会被覆盖——二十四小时到期时系统不可把任务写成
        用户已取得结果，这是投递状态唯一允许改写业务结论的路径（`V-投递-04`
        的例外情形，在核心 :mod:`lingxi.core.delivery.ports` 中单独命名）。

        判定直接读 ``task_delivery_event.expires_at`` 本身——那一列由迁移 0059
        的触发器锁定为 ``created_at + 24 小时``、调用方写什么都会被覆盖，是这条
        二十四小时上限唯一的真相来源。**不接受调用方传入的窗口参数**：早先这里
        有一个 ``older_than`` 参数，由 ``WorkerConfig.delivery_expiry_seconds``
        （环境变量 ``DELIVERY_EXPIRY_SECONDS``）注入，实际查询因此从来没有读过
        ``expires_at`` 列本身，而是在应用层用这个可配置窗口重新计算
        ``created_at < now() - 窗口``——一次环境变量改动就能把触发器锁定的上限
        抬到任意长度，数据库完全不会阻止（内审 P2-1）。测试需要更短的等待窗口
        时，直接构造一条 ``created_at`` 已经在过去的行（触发器只锁定
        ``UPDATE`` 时的 ``created_at``，``INSERT`` 时调用方仍可以指定任意值），
        不再通过参数放大或缩小 24 小时这个业务常量。
        """

        terminals: list[TerminalTask] = []
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    """
                    SELECT t.id, t.conversation_id
                      FROM task AS t
                      JOIN task_delivery_event AS e
                        ON e.task_id = t.id AND e.event_type = 'terminal'
                     WHERE t.status = 'awaiting_delivery'
                       AND e.platform_received_at IS NULL
                       AND e.expires_at <= now()
                     ORDER BY e.created_at, t.id
                     FOR UPDATE OF t SKIP LOCKED
                    """
                )
                rows = cursor.fetchall()
                for task_id, conversation_id in rows:
                    cursor.execute(
                        """
                        UPDATE task
                           SET status = 'failed', error_kind = 'delivery_expired'
                         WHERE id = %s AND status = 'awaiting_delivery'
                        """,
                        (task_id,),
                    )
                    cursor.execute(
                        """
                        UPDATE conversation SET running_task_id = NULL, last_task_ended_at = now()
                         WHERE id = %s AND running_task_id = %s
                        """,
                        (conversation_id, task_id),
                    )
                    cursor.execute(
                        "UPDATE task_delivery_event SET content = NULL WHERE task_id = %s AND content IS NOT NULL",
                        (task_id,),
                    )
                    terminals.append(
                        TerminalTask(
                            task_id=task_id,
                            conversation_id=conversation_id,
                            status="failed",
                            error_kind="delivery_expired",
                        )
                    )
        return terminals

    def clear_delivered_content_for_conversation(self, *, conversation_id: str) -> int:
        """会话边界触发（``/new``、空闲到点、停用/权限变化感知）时清除该会话已
        送达的投递正文；独立开一个事务，供 scheduler 与非 gateway-事务调用方使用。
        `/new` 走的是 ``_Transaction`` 上的同名方法（同一事务），不经过这里。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                return _Transaction(connection).clear_delivered_content_for_conversation(
                    conversation_id=conversation_id
                )

    def clear_delivered_content_for_user(
        self, *, user_id: str, reason: str = "user_cleared"
    ) -> int:
        """停用感知、权限变化感知触发：清除该用户名下全部会话已送达的投递正文，
        并使该用户全部会话的当前 Agent 会话失效、排队物理清理其 JSONL（Issue #153）。

        独立开一个事务，供 scheduler 与非 gateway-事务调用方使用——与
        ``clear_delivered_content_for_conversation`` 同一分工。**Issue #304 批次
        4 起**：``suspend_user`` 确认执行（``adapters/postgres_pending_action.py``
        的 ``PostgresPendingActionStore.confirm()``）改为在它自己已经开启的事务里
        直接调用 ``_Transaction.clear_delivered_content_for_user``（本方法真正的
        逻辑现在住在那里），不再经过这个独立开事务的入口——这是"停用确认与清理
        排队必须同一事务"的要求。本方法继续存在、签名与行为不变，服务未来会在
        **自己独立事务**里触发这个动作的调用方（例如权限变化感知，若其触发点不
        与某个已有事务共享连接）。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                return _Transaction(connection).clear_delivered_content_for_user(
                    user_id=user_id, reason=reason
                )

    def sweep_idle_conversations(self, *, idle_after: timedelta) -> int:
        """会话空闲满两小时由 scheduler 周期调用：到点主动清除已送达的投递正文，
        不依赖下一次任务入队（2026-08-14 补充决定、`V-投递-10`）；同一轮里，凡是
        到点且仍持有当前 Agent 会话的话题，还会排队物理清理其 JSONL（Issue #153）。

        两类到点动作的候选集**不同**，因此分两条查询、不能合并：投递正文清理只挑
        「仍持有未清正文」的会话（没有正文可清的会话不必碰）；Agent 会话物理清理
        只看「话题两小时规则本身已经到点、且当前仍有一个不会再被 resume 的
        ``agent_session_id``」——与这个会话有没有投递正文无关（架构设计 5.2 节：
        下一次任务领取时会因为超过两小时阈值而不带 ``resume``，这个 session 从那一刻
        起就已经是孤儿，不需要等它同时"有正文可清"才处理）。这里**不**把
        ``conversation.agent_session_id`` 置空——是否 ``resume`` 仍然只由领取任务时
        的时间戳比较决定（`V-会话-04`），本方法只负责让物理文件不在数据库判定之外
        继续占着磁盘。

        天然幂等：清正文与排队清理都只在满足各自条件时才发生，重复调用不产生第二次
        副作用（`agent_session_cleanup` 的 `agent_session_id` 唯一索引兜底去重）。
        返回本轮实际清理的会话数，供 scheduler 写运行日志。

        **两条查询都按 conversation id 升序遍历（批次 4 F1，Issue #304）**：此前
        第一条查询是 ``SELECT DISTINCT`` 且没有 ``ORDER BY``，跨会话的遍历顺序不
        确定。本方法逐个会话调用 ``clear_delivered_content_for_conversation``，
        与 ``_transaction.py`` 的 ``clear_delivered_content_for_user``（停用/权限
        变化感知触发，同样一次处理该用户名下多个会话，已在同一批次改为按 id 排序
        锁定）如果分别以不同顺序触碰同一个用户名下的多个会话的
        ``task_delivery_event`` 行，两个事务可能在这些 tde 行上互相交叉持有对方
        下一步需要的锁，形成死锁——不需要经过 ``conversation`` 表本身：本方法从
        不对 ``conversation`` 加锁（两条查询都是不加锁的 ``SELECT``），纯粹是
        「同一批 tde 行，两个事务遍历顺序不同」就足以成环。固定按 id 升序，与
        ``clear_delivered_content_for_user`` 现在的顺序对齐，消除这一多会话交叉
        死锁面。第二条查询（会话物理清理排队）只 ``INSERT ... ON CONFLICT DO
        NOTHING`` 进 ``agent_session_cleanup``，不参与这组死锁面，但同一批次一并
        排序，避免它成为将来叠加其他写路径时的下一个隐患。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    """
                    SELECT DISTINCT c.id
                      FROM conversation AS c
                      JOIN task AS t ON t.conversation_id = c.id
                      JOIN task_delivery_event AS e
                        ON e.task_id = t.id AND e.event_type = 'terminal'
                     WHERE c.running_task_id IS NULL
                       AND c.last_task_ended_at IS NOT NULL
                       AND c.last_task_ended_at <= now() - %s::interval
                       AND e.platform_received_at IS NOT NULL
                       AND e.content IS NOT NULL
                     ORDER BY c.id
                    """,
                    (idle_after,),
                )
                conversation_ids = [row[0] for row in cursor.fetchall()]
                transaction = _Transaction(connection)
                for conversation_id in conversation_ids:
                    transaction.clear_delivered_content_for_conversation(
                        conversation_id=conversation_id
                    )

                cursor.execute(
                    """
                    SELECT c.user_id, c.agent_session_id
                      FROM conversation AS c
                     WHERE c.running_task_id IS NULL
                       AND c.agent_session_id IS NOT NULL
                       AND c.last_task_ended_at IS NOT NULL
                       AND c.last_task_ended_at <= now() - %s::interval
                       -- PR #173 独立复核 P2-5：这条查询刻意不清空
                       -- agent_session_id（取舍本身是对的，见上方文档），于是每个
                       -- 曾经用过会话、之后闲置的话题会永久留在候选集里；不加这条
                       -- NOT EXISTS，每 60 秒的扫描会把候选集整个重新捞出来，对
                       -- 早就已经排过队的会话再跑一次纯浪费的
                       -- INSERT ... ON CONFLICT DO NOTHING。迁移 0061 头部注释
                       -- 写的就是这个去重谓词，之前代码里没有落地。
                       AND NOT EXISTS (
                           SELECT 1 FROM agent_session_cleanup AS a
                            WHERE a.agent_session_id = c.agent_session_id
                       )
                     ORDER BY c.id
                     LIMIT %s
                    """,
                    (idle_after, _IDLE_SESSION_CLEANUP_SWEEP_LIMIT),
                )
                for user_id, agent_session_id in cursor.fetchall():
                    transaction._queue_session_cleanup(
                        user_id=user_id, agent_session_id=agent_session_id, reason="idle_timeout"
                    )
                return len(conversation_ids)
