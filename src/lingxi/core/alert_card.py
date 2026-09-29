"""进程内运行告警 / 恢复的通知卡（R1 样例 05B）与发送选择。

:class:`~lingxi.core.alerting.AlertDispatcher` 每条到期告警都经 :func:`deliver_alert`
发出。卡片键（``notice.alert.fault`` / ``notice.alert.recovery``）未进内容目录时照旧
发 :attr:`AlertNotice.text`，逐字相同；进目录后故障用「故障」色调、恢复用「恢复」
色调，标题由模板给出、须一眼分出故障与恢复。

卡片字段与纯文本同一范围（V-告警-06）：类型、范围、次数、时间、追溯号 / 任务参考号，
不含用户正文、姓名、凭据或链接，也不新增「错误类型」字段（K-3 默认）。卡片不带
按钮、链接或回调；不执行重启、修复或重发。触发、去重、退避与恢复规则全在
:mod:`lingxi.core.alerting`，这里不碰。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from lingxi.config.content import ContentCatalog, ContentError, default_content_catalog
from lingxi.core.alerting import _ALERT_KIND_LABEL, AlertNotice, NoticeAction
from lingxi.core.delivery.notice_card import NoticeCard, NoticeTone
from lingxi.core.delivery.ops_notice import (
    ALERT_FAULT_CARD_KEY,
    ALERT_RECOVERY_CARD_KEY,
    ops_notice_card,
    send_group_notice,
)
from lingxi.core.task_reference import task_reference

_BEIJING_OFFSET = timedelta(hours=8)


def format_utc_with_beijing(moment: datetime) -> str:
    """告警时间的统一写法：UTC 与北京时间并列，精确到分钟。

    北京时间与 UTC 同日时只写时分（``… 03:15 UTC（北京 11:15）``）；落在另一天时
    北京侧带月日。北京时间固定 UTC+8（无夏令时）。
    """
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("告警时间必须带时区")
    utc_moment = moment.astimezone(UTC)
    beijing = utc_moment + _BEIJING_OFFSET
    beijing_text = (
        f"{beijing:%H:%M}" if beijing.date() == utc_moment.date() else f"{beijing:%m-%d %H:%M}"
    )
    return f"{utc_moment:%Y-%m-%d %H:%M} UTC（北京 {beijing_text}）"


def alert_card_values(notice: AlertNotice) -> dict[str, object]:
    """告警卡模板的占位取值；与 :attr:`AlertNotice.text` 同一组字段、同一取值规则。"""
    reference = notice.trace_id or "-"
    reference_label = "追溯号"
    if notice.task_id:
        ref = task_reference(notice.task_id, notice.trace_id)
        reference = ref.reference if ref else "-"
        reference_label = "任务参考号" if ref and ref.reference_kind == "task" else reference_label
    return {
        "kind_label": _ALERT_KIND_LABEL.get(notice.kind, notice.kind.value),
        "scope": notice.scope,
        "count": notice.count,
        "observed_at": format_utc_with_beijing(notice.observed_at),
        "reference_label": reference_label,
        "reference": reference,
    }


def alert_notice_card(
    notice: AlertNotice, catalog: ContentCatalog | None = None
) -> NoticeCard | None:
    """配一张告警 / 恢复卡；对应卡片键未登记时返回 ``None``（照旧发文本）。"""
    recovery = notice.action is NoticeAction.RECOVERY
    if catalog is None:
        try:
            catalog = default_content_catalog()
        except ContentError:  # 目录装不上不能让告警本身发不出去：照旧发文本
            return None
    return ops_notice_card(
        catalog,
        ALERT_RECOVERY_CARD_KEY if recovery else ALERT_FAULT_CARD_KEY,
        tone=NoticeTone.RECOVERY if recovery else NoticeTone.FAILURE,
        fallback_text=notice.text,
        values=alert_card_values(notice),
    )


def deliver_alert(sender: Any, chat_id: str, notice: AlertNotice) -> None:
    """发一条告警：有卡片发卡片（明确拒绝时发送口回落文本一次），否则发原文本。"""
    send_group_notice(
        sender,
        chat_id=chat_id,
        text=notice.text,
        card=alert_notice_card(notice),
        dedupe_key=notice.dedupe_key,
    )


__all__ = ["alert_card_values", "alert_notice_card", "deliver_alert", "format_utc_with_beijing"]
