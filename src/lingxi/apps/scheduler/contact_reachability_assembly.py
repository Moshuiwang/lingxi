"""联系可达状态回调的装配：接上 ``app_user`` 存取口与管理群通道。

判断都在 :class:`lingxi.core.outreach.contact_reachability.ContactReachabilityRecorder`；
这里只有 scheduler 专属的装配细节——没配管理群时的仅日志出口，以及本用途独有的
去重前缀（不与告警共用，飞书才不会把两类消息误判为同一逻辑投递）。
"""

from __future__ import annotations

import logging
from typing import Any

from lingxi.apps.scheduler.audit import AuditSink
from lingxi.apps.scheduler.config import SchedulerConfig
from lingxi.core.outreach.contact_reachability import ContactReachabilityRecorder


class _LogOnlyContactNotifier:
    """未配置管理群时的待办出口：只记一条不含个人资料的结构化日志。

    与 ``alerting_assembly._LogOnlyAlertSender`` 同一姿态：没配置目标群不等于
    这条待办被吞掉，状态照常落库，只是暂时没有群通知。正文里带着这个人的
    open_id 与邮箱，那是给管理群看的；日志里只留去重键的长度，不留正文。
    """

    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None:
        del chat_id
        logging.getLogger("lingxi.apps.scheduler.contact_reachability").warning(
            "联系待办未发送（未配置管理群）dedupe_key_length=%d text_length=%d",
            len(dedupe_key),
            len(text),
        )


def build_contact_reachability_recorder(
    config: SchedulerConfig, dsn: str, *, audit: AuditSink | None = None
) -> ContactReachabilityRecorder:
    """装出联系可达回调，供 ``OutreachDispatcher(contact_outcome=...)`` 使用。"""
    from lingxi.adapters.feishu_group_message import (
        CONTACT_UNAVAILABLE_UUID_PREFIX,
        FeishuGroupMessages,
    )
    from lingxi.adapters.postgres_identity import PostgresAppUserStore

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
    return ContactReachabilityRecorder(
        store=PostgresAppUserStore(dsn), notifier=notifier, chat_id=chat_id, audit=audit
    )


__all__ = ["build_contact_reachability_recorder"]
