"""联系可达状态回调的装配：写 ``app_user`` 的说过话/不可达状态 + 管理群待办。

``OutreachDispatcher`` 只报告「这次正式发送明确送达 / 明确失败」，不认识落库
与通知细节。本模块决定拿到报告之后做什么：成功记「说过话」；失败同样记状态，
并**另发一条管理群待办**——走既有 ``FeishuGroupMessages`` 通道、独立去重前缀，
不进 ``AlertDispatcher`` 的阈值/去重状态机（那是故障告警，这是「联系不上，需要
人工触达」的一次性业务待办）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from lingxi.apps.scheduler.audit import AuditSink
from lingxi.apps.scheduler.config import SchedulerConfig
from lingxi.core.identity.identifiers import redact_identifier

logger = logging.getLogger(__name__)


class _LogOnlyContactNotifier:
    """未配置管理群时的待办出口：只记结构化日志，不发起网络请求。

    与 ``alerting_assembly._LogOnlyAlertSender`` 同一姿态：没配置目标群不等于
    这条待办被吞掉，状态照常落库，只是暂时没有群通知。
    """

    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None:
        del chat_id, dedupe_key
        logging.getLogger("lingxi.apps.scheduler.contact_reachability").warning(text)


def _todo_text(*, open_id: str, email: str | None, error_code: str | None) -> str:
    """管理员待办正文：给到能真正手动触达这个人的信息，不是一条日志。"""
    contact = email or "邮箱未知，请按 open_id 查花名册"
    return (
        "【联系待办，非故障告警】主动发送未能送达一位用户，机器人当前无法与其"
        f"私聊（错误码 {error_code or 'unknown'}），需要管理员改用其它方式手动"
        f"触达。open_id={open_id}；{contact}。"
    )


def build_contact_reachability_recorder(
    config: SchedulerConfig, dsn: str, *, audit: AuditSink | None = None
) -> Callable[[str, bool, str | None], None]:
    """装出联系可达回调，供 ``OutreachDispatcher(contact_outcome=...)`` 使用。

    状态落库口是 :class:`~lingxi.adapters.postgres_identity.PostgresAppUserStore`；
    通知口与 :func:`~lingxi.apps.scheduler.alerting_assembly.build_alerting_duty`
    同一姿态（配了管理群用真实 ``FeishuGroupMessages``，否则只记日志），但
    ``uuid_prefix`` 换成本用途独有的一份，且完全不接 ``AlertDispatcher``。
    """
    from lingxi.adapters.feishu_group_message import (
        CONTACT_UNAVAILABLE_UUID_PREFIX,
        FeishuGroupMessages,
    )
    from lingxi.adapters.postgres_identity import PostgresAppUserStore

    store = PostgresAppUserStore(dsn)
    if config.admin_group_chat_id:
        notifier: Any = FeishuGroupMessages(
            base_url=config.feishu_base_url,
            app_id=config.feishu_app_id,
            app_secret=config.feishu_app_secret,
            uuid_prefix=CONTACT_UNAVAILABLE_UUID_PREFIX,
        )
        chat_id = config.admin_group_chat_id
    else:
        notifier = _LogOnlyContactNotifier()
        chat_id = "scheduler-log-only"

    def record(open_id: str, succeeded: bool, error_code: str | None) -> None:
        now = datetime.now(UTC)
        if succeeded:
            store.record_contact_reachable(open_id=open_id, when=now)
            if audit is not None:
                audit.record(
                    "outreach.contact_marked_reachable", open_id=redact_identifier(open_id)
                )
            return
        store.record_contact_unavailable(open_id=open_id, when=now, code=error_code or "unknown")
        if audit is not None:
            audit.record(
                "outreach.contact_marked_unavailable",
                open_id=redact_identifier(open_id),
                error_code=error_code,
            )
        _send_admin_todo(
            notifier, store, chat_id=chat_id, open_id=open_id, error_code=error_code, audit=audit
        )

    return record


def _send_admin_todo(
    notifier: Any,
    store: Any,
    *,
    chat_id: str,
    open_id: str,
    error_code: str | None,
    audit: AuditSink | None,
) -> None:
    """发一条群待办；查邮箱与发送任一步失败都不带走已经落库的可达状态。"""
    try:
        record = store.get_by_open_id(open_id)
        email = record.email if record is not None else None
    except Exception as error:  # 查邮箱失败仍要发待办，只是缺一条联系方式
        logger.error("联系待办查邮箱失败 error=%s", type(error).__name__)
        email = None
    try:
        notifier.send_text(
            chat_id=chat_id,
            text=_todo_text(open_id=open_id, email=email, error_code=error_code),
            dedupe_key=f"contact-unavailable:{open_id}",
        )
    except Exception as error:  # 群通知失败不得带走已经落库的状态
        logger.error("联系不可达管理员待办发送失败 error=%s", type(error).__name__)
        if audit is not None:
            audit.record("outreach.contact_todo_notify_failed", error=type(error).__name__)
        return
    if audit is not None:
        audit.record("outreach.contact_todo_notified", open_id=redact_identifier(open_id))


__all__ = ["build_contact_reachability_recorder"]
