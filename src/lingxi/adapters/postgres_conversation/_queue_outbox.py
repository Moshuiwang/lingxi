"""投递 outbox。

Worker 侧写事件（``append_delivery_event``/``write_terminal_event``）、
Gateway 侧确认送达（``confirm_delivery``）、二十四小时到期强制收敛
（``expire_undelivered_terminals``），以及三类会话边界触发共用的投递正文
清除与两小时空闲扫描——这几个方法共用同一条读写边界。
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

# ``sweep_idle_conversations`` 每次扫描新增排队的会话数上限：查询本身已经用
# NOT EXISTS 把候选集收窄到"尚未排过队"的那些，这里只是给单次扫描一个防护
# 上限，避免一次异常的大批量话题同时到点时单条查询/单次事务处理过多行。500
# 是与 Worker 侧消费能力（默认单批 20、约 2 秒轮询一次）比较后留出的宽裕量，
# 不是精确调校值。
_IDLE_SESSION_CLEANUP_SWEEP_LIMIT = 500

# _lock_task_for_write 的返回哨兵：ownership/幂等预检通过、调用方可以继续写入。
_PROCEED = object()

_LOCK_TASK_FOR_DELIVERY_WRITE_SQL = "SELECT worker_id, status FROM task WHERE id = %s FOR UPDATE"

_UPDATE_TASK_AFTER_TERMINAL_SQL = """
UPDATE task
   SET status = 'awaiting_delivery',
       error_kind = COALESCE(%s, error_kind),
       ended_at = now(),
       token_usage = %s,
       guard_denied_count = %s,
       failure_code = %s,
       failure_signature = %s
 WHERE id = %s AND worker_id = %s AND status = 'running'
"""

_INSERT_DOCUMENT_DELIVERY_REQUEST_SQL = """
INSERT INTO task_document_delivery_request
    (id, task_id, requester_open_id, title, paragraphs, delivery_type, markdown)
SELECT %s, t.id, u.feishu_open_id, %s, %s, %s, %s
  FROM task AS t
  JOIN app_user AS u ON u.id = t.user_id
 WHERE t.id = %s
"""

_CONFIRM_DELIVERY_LOOKUP_SQL = """
SELECT t.conversation_id, e.terminal_kind, e.error_kind, e.agent_session_id
  FROM task AS t
  JOIN task_delivery_event AS e
    ON e.task_id = t.id AND e.event_type = 'terminal'
 WHERE t.id = %s AND t.status = 'awaiting_delivery'
   AND e.platform_received_at IS NULL
 FOR UPDATE OF t
"""

_MARK_TERMINAL_RECEIVED_SQL = """
UPDATE task_delivery_event
   SET platform_received_at = now(),
       platform_message_kind = %s,
       platform_message_id = %s
 WHERE task_id = %s AND event_type = 'terminal'
"""

_RESOLVE_TASK_AFTER_CONFIRM_SQL = """
UPDATE task SET status = %s, error_kind = %s
 WHERE id = %s AND status = 'awaiting_delivery'
"""

_RELEASE_CONVERSATION_AFTER_CONFIRM_SQL = """
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
"""

_EXPIRE_CANDIDATES_SQL = """
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

_EXPIRE_TASK_SQL = """
UPDATE task
   SET status = 'failed', error_kind = 'delivery_expired'
 WHERE id = %s AND status = 'awaiting_delivery'
"""

_EXPIRE_RELEASE_CONVERSATION_SQL = """
UPDATE conversation SET running_task_id = NULL, last_task_ended_at = now()
 WHERE id = %s AND running_task_id = %s
"""

_EXPIRE_CLEAR_CONTENT_SQL = (
    "UPDATE task_delivery_event SET content = NULL WHERE task_id = %s AND content IS NOT NULL"
)

_STALE_DELIVERED_CONTENT_CONVERSATIONS_SQL = """
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
"""

_IDLE_AGENT_SESSION_CANDIDATES_SQL = """
SELECT c.user_id, c.agent_session_id
  FROM conversation AS c
 WHERE c.running_task_id IS NULL
   AND c.agent_session_id IS NOT NULL
   AND c.last_task_ended_at IS NOT NULL
   AND c.last_task_ended_at <= now() - %s::interval
   AND NOT EXISTS (
       SELECT 1 FROM agent_session_cleanup AS a
        WHERE a.agent_session_id = c.agent_session_id
   )
 ORDER BY c.id
 LIMIT %s
"""


def _jsonb_or_none(value: Mapping[str, int] | None) -> Any:
    """``task.token_usage``（迁移 ``0070``）的参数适配。

    ``None`` 必须原样作为 Python ``None`` 传给 psycopg，写出真正的 SQL
    ``NULL``——``Jsonb(None)`` 写的是 JSON 字面量 ``null``（``'null'::jsonb``），
    那是一个"已知值为空"，与 ``IS NULL``（"取不到"）语义完全不同，会让
    ``adapters/postgres_daily_report.py`` 的 ``WHERE token_usage IS NOT NULL``
    把"结构性取不到"误判成"取到了、值是空对象"。延迟导入 ``Jsonb``：与仓库
    既有惯例一致，没有驱动的机器仍能 import 本模块。
    """
    if value is None:
        return None
    from psycopg.types.json import Jsonb

    return Jsonb(dict(value))


class _OutboxMixin:
    """投递事件 outbox 写入与消费：读写边界见模块 docstring。

    ``append_delivery_event``（非终态）与 ``write_terminal_event``（终态）
    共用同一套所有权与顺序保证：先 ``SELECT ... FOR UPDATE`` 锁定 task 行、
    核对 ``worker_id`` 与 ``status``，再在同一把锁下计算
    ``MAX(sequence)+1``——两个并发写者因此天然串行化，不需要额外的咨询锁。
    幂等由调用方提供的 idempotency_key 承担：命中已有行时原样返回该行的
    sequence 并标记 ``duplicate=True``，不创建第二条事件、不重复触发调用方
    的副作用计数（`V-投递-01`/`V-投递-02`）。
    """

    def _lock_task_for_write(
        self, cursor: Any, *, task_id: str, worker_id: str, idempotency_key: str
    ) -> Any:
        """加锁并做幂等/所有权预检，供非终态/终态两个写入方法共用。

        返回 ``_PROCEED`` 表示预检通过、调用方可以继续写入；否则返回调用方
        应该直接 ``return`` 的值（命中已有幂等行时是该 ``AppendedEvent``；
        任务不存在或所有权/状态不匹配时是 ``None``）。
        """
        cursor.execute(_LOCK_TASK_FOR_DELIVERY_WRITE_SQL, (task_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        existing = self._find_by_idempotency_key(cursor, idempotency_key)
        if existing is not None:
            return existing
        if row[0] != worker_id or row[1] != "running":
            return None
        return _PROCEED

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

        只有当前持有该任务的 worker（``worker_id`` 匹配且 ``status='running'``）
        能写；否则返回 ``None``——僵尸/迟到的写入必须被拒绝，不能悄悄创建游离
        事件。``content`` 必须是调用方已经过安全检查、允许展示给当前用户的
        文本，本方法不做安全判断。终态请使用 :meth:`write_terminal_event`。
        幂等判定先于所有权判定：命中已有 ``idempotency_key`` 时原样返回该行
        并标记 ``duplicate=True``。
        """
        if event_type not in (
            DeliveryEventType.STARTED.value,
            DeliveryEventType.PROGRESS.value,
            DeliveryEventType.SAFELY_RELEASABLE_ANSWER.value,
        ):
            raise ValueError("append_delivery_event 只处理非终态事件类型")
        # 写入前自查：在真正打开数据库连接之前就校验一遍，命中问题时抛出一个
        # 可读的 ValueError——数据库层的 CHECK 仍然是最终防线，这里只是让
        # "写库前就能发现"，不依赖调用方自己记得遵守合同。
        assert_content_allowed(DeliveryEventType(event_type), content)
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                result = self._lock_task_for_write(
                    cursor, task_id=task_id, worker_id=worker_id, idempotency_key=idempotency_key
                )
                if result is not _PROCEED:
                    return result
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

    @staticmethod
    def _validate_delivery_request_kinds(
        *,
        terminal_kind: str,
        document_request: Mapping[str, Any] | None,
        sheet_request: Mapping[str, Any] | None,
    ) -> None:
        if document_request is not None and terminal_kind != TerminalKind.SUCCESS.value:
            raise ValueError("document_request 只能在 terminal_kind='success' 时提供")
        if sheet_request is not None and terminal_kind != TerminalKind.SUCCESS.value:
            raise ValueError("sheet_request 只能在 terminal_kind='success' 时提供")
        if document_request is not None and sheet_request is not None:
            raise ValueError("document_request 与 sheet_request 不能同时提供")

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

        与任务状态转换在**同一事务**提交或整体回滚；``conversation.running_task_id``
        在这里**不释放**，话题继续占用直到投递解析。返回 ``None`` 表示当前
        调用方已不再持有该任务；幂等判定先于所有权判定（见
        :meth:`append_delivery_event`）。补数/失败细分列语义见
        :meth:`_finalize_terminal_task_row`；``document_request``/
        ``sheet_request`` 互斥、只能在 success 终态提供，见
        :meth:`_insert_document_delivery_request`。
        """
        self._validate_delivery_request_kinds(
            terminal_kind=terminal_kind,
            document_request=document_request,
            sheet_request=sheet_request,
        )
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                return self._write_terminal_event_locked(
                    connection,
                    task_id=task_id,
                    worker_id=worker_id,
                    terminal_kind=terminal_kind,
                    error_kind=error_kind,
                    content=content,
                    elapsed_seconds=elapsed_seconds,
                    agent_session_id=agent_session_id,
                    token_usage=token_usage,
                    guard_denied_count=guard_denied_count,
                    failure_code=failure_code,
                    failure_signature=failure_signature,
                    document_request=document_request,
                    sheet_request=sheet_request,
                )

    def _write_terminal_event_locked(
        self,
        connection: Any,
        *,
        task_id: str,
        worker_id: str,
        terminal_kind: str,
        error_kind: str | None,
        content: str | None,
        elapsed_seconds: int | None,
        agent_session_id: str | None,
        token_usage: Mapping[str, int] | None,
        guard_denied_count: int | None,
        failure_code: str | None,
        failure_signature: str | None,
        document_request: Mapping[str, Any] | None,
        sheet_request: Mapping[str, Any] | None,
    ) -> AppendedEvent | None:
        """在已打开事务的连接里完成终态事件写入与收尾（见 write_terminal_event）。"""
        cursor = connection.cursor()
        idempotency_key = f"{task_id}:terminal"
        result = self._lock_task_for_write(
            cursor, task_id=task_id, worker_id=worker_id, idempotency_key=idempotency_key
        )
        if result is not _PROCEED:
            return result
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
        self._finalize_terminal_event(
            connection,
            cursor,
            appended,
            task_id=task_id,
            worker_id=worker_id,
            error_kind=error_kind,
            token_usage=token_usage,
            guard_denied_count=guard_denied_count,
            failure_code=failure_code,
            failure_signature=failure_signature,
            document_request=document_request,
            sheet_request=sheet_request,
        )
        return appended

    def _finalize_terminal_event(
        self,
        connection: Any,
        cursor: Any,
        appended: AppendedEvent,
        *,
        task_id: str,
        worker_id: str,
        error_kind: str | None,
        token_usage: Mapping[str, int] | None,
        guard_denied_count: int | None,
        failure_code: str | None,
        failure_signature: str | None,
        document_request: Mapping[str, Any] | None,
        sheet_request: Mapping[str, Any] | None,
    ) -> None:
        """终态事件写入之后的收尾：落任务状态列，再尝试插入文档/表格投递请求。"""
        self._finalize_terminal_task_row(
            cursor,
            task_id=task_id,
            worker_id=worker_id,
            error_kind=error_kind,
            token_usage=token_usage,
            guard_denied_count=guard_denied_count,
            failure_code=failure_code,
            failure_signature=failure_signature,
        )
        self._maybe_insert_delivery_request(
            connection,
            cursor,
            task_id=task_id,
            document_request=document_request,
            sheet_request=sheet_request,
            appended=appended,
        )

    def _finalize_terminal_task_row(
        self,
        cursor: Any,
        *,
        task_id: str,
        worker_id: str,
        error_kind: str | None,
        token_usage: Mapping[str, int] | None,
        guard_denied_count: int | None,
        failure_code: str | None,
        failure_signature: str | None,
    ) -> None:
        """把任务从 ``running`` 转为 ``awaiting_delivery``，落通报补数与失败细分列。

        ``failure_code`` 是这次终态的细分失败码（``task.error_kind`` 是压平后
        的粗粒度值）；``failure_signature`` 是底层异常收敛出的固定形状摘要
        （``exception.<类别>.<摘要>``），**异常正文/标识原值永远不落这两列**
        （`V-花名册-33`）。两者与 ``token_usage``/``guard_denied_count`` 一样
        可空，``NULL`` 是精确语义。
        """
        cursor.execute(
            _UPDATE_TASK_AFTER_TERMINAL_SQL,
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

    def _maybe_insert_delivery_request(
        self,
        connection: Any,
        cursor: Any,
        *,
        task_id: str,
        document_request: Mapping[str, Any] | None,
        sheet_request: Mapping[str, Any] | None,
        appended: AppendedEvent,
    ) -> None:
        """``document_request``/``sheet_request`` 非空时尝试插入投递请求。

        ``{}`` 是合法的非 ``None`` 值但布尔求值为假：用 ``or`` 判断会把它当成
        "没提供"悄悄跳过插入与失败审计，因此显式用 ``is not None``。只在这次
        真正插入了新终态事件（``appended.duplicate`` 为假）时才尝试——幂等
        重试命中已有行的分支在上层已经直接返回，走不到这里。
        """
        delivery_request = document_request if document_request is not None else sheet_request
        delivery_type = (
            ("docx" if document_request is not None else "sheet")
            if delivery_request is not None
            else None
        )
        if delivery_request is None or appended.duplicate:
            return
        self._attempt_delivery_request_insert(
            connection,
            cursor,
            task_id=task_id,
            delivery_request=delivery_request,
            delivery_type=delivery_type,
        )

    def _attempt_delivery_request_insert(
        self,
        connection: Any,
        cursor: Any,
        *,
        task_id: str,
        delivery_request: Mapping[str, Any],
        delivery_type: str,
    ) -> None:
        """尝试插入文档/表格投递请求，失败时降级而不连坐终态答案。

        套一层独立 SAVEPOINT（嵌套 ``connection.transaction()``，psycopg3 在
        已有外层事务时自动降级为 SAVEPOINT）：终态事件与 task 状态转移已经
        成立，用户已经"拿到答案"是这一刻唯一该被保证的事实。插入失败（结构性、
        会重复发生，例如提问用户 ``app_user.feishu_open_id`` 为 ``NULL``）只
        回滚这个 SAVEPOINT，降级为"这次问数没有文档/表格"并响亮记一条审计
        （``worker.document_request_insert_failed``），不吞声、也不让一个附加
        功能插不进去连坐用户已经产生的正常问数答案。
        """
        try:
            with connection.transaction():
                self._insert_document_delivery_request(
                    cursor,
                    task_id=task_id,
                    delivery_request=delivery_request,
                    delivery_type=delivery_type,
                )
        except Exception as error:  # 降级但绝不吞声
            logger.error(
                "worker.document_request_insert_failed task_id=%s delivery_type=%s error=%s",
                task_id,
                delivery_type,
                type(error).__name__,
            )

    @staticmethod
    def _insert_document_delivery_request(
        cursor: Any, *, task_id: str, delivery_request: Mapping[str, Any], delivery_type: str
    ) -> None:
        """插入一行 ``pending`` 的文档/表格投递请求。

        ``delivery_type='docx'`` 形状是 ``{"title", "paragraphs", "markdown"}``；
        ``sheet`` 是 ``{"title", "rows"}``——两者都落进同一个 ``paragraphs``
        JSONB 列。``markdown`` 只在 docx 时读取，取不到一律落 ``NULL``（可选
        附加值，不因此拒绝整条请求）。``requester_open_id`` 直接在 SQL 里
        JOIN 求值，复用调用方已持有的锁。``task_id`` 有 ``UNIQUE`` 约束：
        重复插入让冲突原样抛出，不用 ``ON CONFLICT DO NOTHING`` 悄悄吞掉
        （真正的幂等保护是调用方的 ``appended.duplicate`` 判断）。
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
            _INSERT_DOCUMENT_DELIVERY_REQUEST_SQL,
            (new_id("tdd"), title, Jsonb(list(content)), delivery_type, markdown, task_id),
        )
        if cursor.rowcount != 1:
            # task 行已经在调用方的 FOR UPDATE 里被锁定、确认存在；到这里插不
            # 进去只可能是 app_user 那一侧的 JOIN 没有命中（结构性不应发生：
            # task.user_id 有外键约束指向 app_user），宁可响亮失败也不要悄悄
            # 不建这行。
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
        """插入一条**确定尚不存在**的新事件。

        调用方必须已经确认 ``idempotency_key`` 不重复、且已经用
        ``SELECT ... FOR UPDATE`` 锁定了对应的 task 行（保证 ``sequence`` 的
        计算不会与并发写者相互覆盖）。
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

        业务终态完全来自写终态事件时记录的 ``terminal_kind``/``error_kind``——
        投递确认成功不改写业务结果（`V-投递-04`）。返回值只有两种合法含义：
        ``True`` 表示三条写入（事件确认、任务收敛、会话回写）已经全部提交；
        ``False`` 表示第一步查询就没找到可确认的东西，没有任何写入发生。
        ``conversation`` 更新失败不属于这两种含义中的任何一种，见
        :meth:`_release_conversation_after_confirm`。
        """
        if platform_message_kind not in ("card", "text"):
            raise ValueError("platform_message_kind 只能是 card 或 text")
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(_CONFIRM_DELIVERY_LOOKUP_SQL, (task_id,))
                row = cursor.fetchone()
                if row is None:
                    return False
                conversation_id, terminal_kind, event_error_kind, agent_session_id = row
                outcome = resolve_delivered_outcome(
                    terminal_kind=terminal_kind, error_kind=event_error_kind
                )
                cursor.execute(
                    _MARK_TERMINAL_RECEIVED_SQL,
                    (platform_message_kind, platform_message_id, task_id),
                )
                cursor.execute(
                    _RESOLVE_TASK_AFTER_CONFIRM_SQL, (outcome.status, outcome.error_kind, task_id)
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"任务 {task_id} 在确认送达时状态发生了竞态")
                # 只在业务成功时才有 agent_session_id；COALESCE 保证失败/停止/
                # 拒发终态不会把已有的会话延续状态清空。
                return self._release_conversation_after_confirm(
                    cursor,
                    conversation_id=conversation_id,
                    task_id=task_id,
                    agent_session_id=agent_session_id,
                )

    def _release_conversation_after_confirm(
        self,
        cursor: Any,
        *,
        conversation_id: str,
        task_id: str,
        agent_session_id: str | None,
    ) -> bool:
        """释放话题占用；``conversation`` 更新没有命中即整个事务响亮失败。

        上两条写入（事件确认、task 收敛）此刻已经在这个事务里执行但还没
        提交：以异常退出让 psycopg 回滚整个事务，三条写入因此要么全部不
        生效——宁可响亮失败也不要悄悄丢弃 ``agent_session_id``、或者返回一个
        和"没有可确认的东西"含义相同的 ``False`` 掩盖"已经确认到一半"的竞态。
        """
        cursor.execute(
            _RELEASE_CONVERSATION_AFTER_CONFIRM_SQL,
            (conversation_id, task_id, agent_session_id),
        )
        conversation_row = cursor.fetchone()
        if conversation_row is None:
            raise RuntimeError(
                f"任务 {task_id} 确认送达时 conversation {conversation_id} 的话题占用状态发生了竞态"
            )
        self._queue_overwritten_session(
            cursor,
            user_id=conversation_row[0],
            previous_session_id=conversation_row[1],
            new_session_id=agent_session_id,
        )
        return True

    def expire_undelivered_terminals(self) -> list[TerminalTask]:
        """二十四小时到期强制收敛为失败终态，释放话题、清空正文。

        无论原始业务结论是什么都会被覆盖——这是投递状态唯一允许改写业务
        结论的路径（`V-投递-04` 的例外情形）。判定直接读
        ``task_delivery_event.expires_at``——那一列由迁移 0059 的触发器锁定
        为 ``created_at + 24 小时``，**不接受调用方传入的窗口参数**：应用层
        重新计算窗口会让一次环境变量改动就能把这个上限抬到任意长度。测试
        需要更短等待窗口时，直接构造一条 ``created_at`` 已经在过去的行。
        """
        terminals: list[TerminalTask] = []
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(_EXPIRE_CANDIDATES_SQL)
                rows = cursor.fetchall()
                for task_id, conversation_id in rows:
                    terminals.append(
                        self._expire_terminal_row(
                            cursor, task_id=task_id, conversation_id=conversation_id
                        )
                    )
        return terminals

    def _expire_terminal_row(
        self, cursor: Any, *, task_id: str, conversation_id: str
    ) -> TerminalTask:
        cursor.execute(_EXPIRE_TASK_SQL, (task_id,))
        cursor.execute(_EXPIRE_RELEASE_CONVERSATION_SQL, (conversation_id, task_id))
        cursor.execute(_EXPIRE_CLEAR_CONTENT_SQL, (task_id,))
        return TerminalTask(
            task_id=task_id,
            conversation_id=conversation_id,
            status="failed",
            error_kind="delivery_expired",
        )

    def clear_delivered_content_for_conversation(self, *, conversation_id: str) -> int:
        """会话边界触发时清除该会话已送达的投递正文。

        触发场景：``/new``、空闲到点、停用/权限变化感知。独立开一个事务，
        供 scheduler 与非 gateway-事务调用方使用；`/new` 走的是
        ``_Transaction`` 上的同名方法（同一事务），不经过这里。
        """
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                return _Transaction(connection).clear_delivered_content_for_conversation(
                    conversation_id=conversation_id
                )

    def clear_delivered_content_for_user(
        self, *, user_id: str, reason: str = "user_cleared"
    ) -> int:
        """停用/权限变化感知触发：清除该用户全部会话已送达的投递正文。

        并使该用户全部会话的当前 Agent 会话失效、排队物理清理其 JSONL。
        独立开一个事务，供 scheduler 与非 gateway-事务调用方使用——与
        ``clear_delivered_content_for_conversation`` 同一分工。停用确认执行
        改为在它自己已经开启的事务里直接调用
        ``_Transaction.clear_delivered_content_for_user``（真正的逻辑住在
        那里），停用确认与清理排队必须同一事务；本方法继续服务未来在自己
        独立事务里触发这个动作的调用方。
        """
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                return _Transaction(connection).clear_delivered_content_for_user(
                    user_id=user_id, reason=reason
                )

    def sweep_idle_conversations(self, *, idle_after: timedelta) -> int:
        """会话空闲满两小时由 scheduler 周期调用，返回本轮实际清理的会话数。

        分两条独立查询，候选集不同：清正文只挑仍持有未清正文的会话（见
        :meth:`_clear_stale_delivered_content`）；Agent 会话物理清理排队与
        是否有投递正文无关（见 :meth:`_queue_idle_session_cleanups`）。两条
        查询都按 conversation id 升序遍历，避免与
        ``clear_delivered_content_for_user`` 以不同顺序触碰同一批行而成环
        死锁。天然幂等：重复调用不产生第二次副作用。
        """
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cleared_count = self._clear_stale_delivered_content(
                    cursor, connection, idle_after=idle_after
                )
                self._queue_idle_session_cleanups(cursor, connection, idle_after=idle_after)
                return cleared_count

    @staticmethod
    def _clear_stale_delivered_content(
        cursor: Any, connection: Any, *, idle_after: timedelta
    ) -> int:
        """清除已到两小时空闲、仍持有未清正文的会话；返回处理的会话数。

        **不**把 ``conversation.agent_session_id`` 置空——是否 ``resume`` 仍然
        只由领取任务时的时间戳比较决定（`V-会话-04`），本方法只负责让正文
        不再继续占着数据库。
        """
        cursor.execute(_STALE_DELIVERED_CONTENT_CONVERSATIONS_SQL, (idle_after,))
        conversation_ids = [row[0] for row in cursor.fetchall()]
        transaction = _Transaction(connection)
        for conversation_id in conversation_ids:
            transaction.clear_delivered_content_for_conversation(conversation_id=conversation_id)
        return len(conversation_ids)

    @staticmethod
    def _queue_idle_session_cleanups(
        cursor: Any, connection: Any, *, idle_after: timedelta
    ) -> None:
        """两小时到点、仍有 ``agent_session_id`` 的话题排队物理清理其 JSONL。

        候选集用 ``NOT EXISTS`` 排除已经在 ``agent_session_cleanup`` 里的行——
        去重的最终保障是该表的唯一索引，这里只是避免每次周期扫描都把候选集
        整个重新捞出来做一次纯浪费的 ``INSERT ... ON CONFLICT DO NOTHING``。
        """
        cursor.execute(
            _IDLE_AGENT_SESSION_CANDIDATES_SQL,
            (idle_after, _IDLE_SESSION_CLEANUP_SWEEP_LIMIT),
        )
        transaction = _Transaction(connection)
        for user_id, agent_session_id in cursor.fetchall():
            transaction._queue_session_cleanup(
                user_id=user_id, agent_session_id=agent_session_id, reason="idle_timeout"
            )
