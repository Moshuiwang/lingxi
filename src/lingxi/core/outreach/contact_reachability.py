"""主动发送的明确结果 → 「说过话 / 联系不上」状态 + 管理员待办（纯编排，无 I/O）。

``OutreachDispatcher`` 只报告「这次正式发送明确送达 / 明确失败」。本模块决定拿到
报告之后做什么：成功记「说过话」；失败同样记状态，并**另发一条管理群待办**——
它是「联系不上，需要人工触达」的一次性业务待办，**不是故障告警**，因此不进告警
状态机的阈值与去重，只借用同一条管理群通道。

**待办正文要能让管理员真的找到这个人**：带 open_id 与库里的邮箱。它只发进管理群；
审计记录不携带任何标识，可追溯的线索只留在日志里，且一律是脱敏后的标识。

四种结局都不允许反过来打断发送收口：落库、查邮箱、发待办任一步失败，只记审计与
日志，已经落下的状态不回滚——「联系不上」这个事实比「待办没发出去」更重要。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from lingxi.core.identity.identifiers import redact_identifier

logger = logging.getLogger(__name__)

#: 审计动作名。五个都不携带任何标识；可追溯线索只在日志里，且一律脱敏。
AUDIT_MARKED_REACHABLE = "outreach.contact_marked_reachable"
AUDIT_MARKED_UNAVAILABLE = "outreach.contact_marked_unavailable"
AUDIT_TODO_NOTIFIED = "outreach.contact_todo_notified"
AUDIT_TODO_NOTIFY_FAILED = "outreach.contact_todo_notify_failed"
AUDIT_STATE_WRITE_FAILED = "outreach.contact_state_write_failed"

#: 群待办的去重键前缀：同一个人短时间内重复失败只占管理员一条待办。
TODO_DEDUPE_PREFIX = "contact-unavailable:"


class ContactStateStore(Protocol):
    """落「说过话 / 联系不上」状态的存取口（实现在 adapters）。"""

    def record_contact_reachable(self, *, open_id: str, when: datetime) -> bool:
        """这个人在 ``when`` 这一刻被证明可达；返回是否命中了一行。"""

    def record_contact_unavailable(self, *, open_id: str, when: datetime, code: str) -> bool:
        """这个人在 ``when`` 这一刻明确送不进去；返回是否命中了一行。"""

    def get_by_open_id(self, open_id: str) -> Any:
        """按 open_id 查建档投影（要带 ``email``），查无返回 ``None``。"""


class AdminTodoNotifier(Protocol):
    """管理群文本通道；与告警共用适配器类型，但去重前缀是本用途独有的一份。"""

    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None:
        """把一条文本发进指定群；``dedupe_key`` 折成平台去重标识。"""


class _AuditSink(Protocol):
    def record(self, action: str, /, **fields: object) -> None: ...


def admin_todo_text(*, open_id: str, email: str | None, error_code: str | None) -> str:
    """管理员待办正文：给到能真正手动触达这个人的信息，不是一条日志。"""
    contact = email or "邮箱未知，请按 open_id 查花名册"
    return (
        "【联系待办，非故障告警】主动发送未能送达一位用户，机器人当前无法与其"
        f"私聊（错误码 {error_code or 'unknown'}），需要管理员改用其它方式手动"
        f"触达。open_id={open_id}；{contact}。"
    )


class ContactReachabilityRecorder:
    """把一次正式发送的明确结果落成状态；失败时另转一条管理群待办。

    可直接作为 ``OutreachDispatcher(contact_outcome=...)`` 的回调：签名
    ``(open_id, 是否送达, 失败时的错误码)``。
    """

    def __init__(
        self,
        *,
        store: ContactStateStore,
        notifier: AdminTodoNotifier,
        chat_id: str,
        audit: _AuditSink | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """接线状态存取口、管理群通道、目标群与审计出口；时钟可注入以便测试。"""
        self._store = store
        self._notifier = notifier
        self._chat_id = chat_id
        self._audit = audit
        self._clock = clock or (lambda: datetime.now(UTC))

    def __call__(self, open_id: str, succeeded: bool, error_code: str | None) -> None:
        """成功记「说过话」；失败记「联系不上」并发管理员待办。"""
        now = self._clock()
        if succeeded:
            if not self._write_state(
                lambda: self._store.record_contact_reachable(open_id=open_id, when=now)
            ):
                return
            logger.info(
                "联系可达状态已记录 outcome=%s open_id=%s", "reachable", redact_identifier(open_id)
            )
            self._record(AUDIT_MARKED_REACHABLE)
            return
        code = error_code or "unknown"
        if not self._write_state(
            lambda: self._store.record_contact_unavailable(open_id=open_id, when=now, code=code)
        ):
            return
        logger.info(
            "联系可达状态已记录 outcome=%s open_id=%s", "unavailable", redact_identifier(open_id)
        )
        self._record(AUDIT_MARKED_UNAVAILABLE, error_code=code)
        self._send_admin_todo(open_id=open_id, error_code=code)

    def _write_state(self, write: Callable[[], bool]) -> bool:
        """落状态；异常只记审计与日志、不上抛——连状态是否落上都不确定，不发待办。"""
        try:
            write()
        except Exception as error:
            logger.error("联系可达状态落库失败 error=%s", type(error).__name__)
            self._record(AUDIT_STATE_WRITE_FAILED, error=type(error).__name__)
            return False
        return True

    def _send_admin_todo(self, *, open_id: str, error_code: str) -> None:
        """查邮箱与发送任一步失败都不带走已经落库的状态。"""
        try:
            record = self._store.get_by_open_id(open_id)
            email = record.email if record is not None else None
        except Exception as error:  # 查邮箱失败仍要发待办，只是缺一条联系方式
            logger.error("联系待办查邮箱失败 error=%s", type(error).__name__)
            email = None
        try:
            self._notifier.send_text(
                chat_id=self._chat_id,
                text=admin_todo_text(open_id=open_id, email=email, error_code=error_code),
                dedupe_key=TODO_DEDUPE_PREFIX + open_id,
            )
        except Exception as error:  # 群通知失败不得带走已经落库的状态
            logger.error("联系不可达管理员待办发送失败 error=%s", type(error).__name__)
            self._record(AUDIT_TODO_NOTIFY_FAILED, error=type(error).__name__)
            return
        logger.info("联系不可达管理员待办已发送 open_id=%s", redact_identifier(open_id))
        self._record(AUDIT_TODO_NOTIFIED)

    def _record(self, action: str, **fields: object) -> None:
        if self._audit is not None:
            self._audit.record(action, **fields)


__all__ = [
    "AUDIT_MARKED_REACHABLE",
    "AUDIT_MARKED_UNAVAILABLE",
    "AUDIT_STATE_WRITE_FAILED",
    "AUDIT_TODO_NOTIFIED",
    "AUDIT_TODO_NOTIFY_FAILED",
    "TODO_DEDUPE_PREFIX",
    "AdminTodoNotifier",
    "ContactReachabilityRecorder",
    "ContactStateStore",
    "admin_todo_text",
]
