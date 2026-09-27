"""管理群运维通知（日报、运行告警、文案覆盖告警）的卡片配置与发送选择。

这几类正文由代码按数据动态拼出、分成多段，套不上
:mod:`lingxi.core.delivery.catalog_notice` 的「一个文本键配一张卡」，因此各自登记
一个固定卡片键（同一前缀 ``notice.``，见 :data:`OPS_CARD_KEYS`）。卡片键未进内容
目录、或本地造不出卡片（占位不齐、带按钮文字、撞上链接校验）时
:func:`ops_notice_card` 返回 ``None``，调用方照旧发原文本，逐字不变；进目录后模板
给标题与开头分段，数据分段由调用方逐行给出（已转义），等价纯文本就是原文本。

发送只经 :func:`send_group_notice`：有卡片且发送口支持 ``send_notice`` 才发卡片，
飞书明确拒绝时由发送口补发一次纯文本；结果不明直接抛出，调用方沿用各自现有的
重试与去重，不补发。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from lingxi.config.content import ContentCatalog, ContentError
from lingxi.core.delivery.catalog_notice import notice_card_key
from lingxi.core.delivery.notice_card import NoticeCard, NoticeSection, NoticeTone, escape_markdown

logger = logging.getLogger(__name__)

#: 内测每日通报。模板无占位。
DAILY_REPORT_CARD_KEY = notice_card_key("innertest.daily_report")
#: 花名册审计日报（原文本键 ``roster.daily_report``）。模板占位：``report_date``。
ROSTER_REPORT_CARD_KEY = notice_card_key("roster.daily_report")
#: 进程内运行告警的故障 / 恢复两张卡。模板占位见 :mod:`lingxi.core.alert_card`。
ALERT_FAULT_CARD_KEY = notice_card_key("alert.fault")
ALERT_RECOVERY_CARD_KEY = notice_card_key("alert.recovery")
#: 文案覆盖文件校验告警。模板占位：``reason``。
CONTENT_OVERRIDE_CARD_KEY = notice_card_key("content.override_rejected")

OPS_CARD_KEYS: tuple[str, ...] = (
    DAILY_REPORT_CARD_KEY,
    ROSTER_REPORT_CARD_KEY,
    ALERT_FAULT_CARD_KEY,
    ALERT_RECOVERY_CARD_KEY,
    CONTENT_OVERRIDE_CARD_KEY,
)


def escaped_lines(lines: Iterable[str]) -> tuple[str, ...]:
    """把已渲染的纯文本行逐行转义成卡片正文行；多行文本按换行拆开、空行丢弃。"""
    result: list[str] = []
    for line in lines:
        result.extend(escape_markdown(part) for part in str(line).split("\n") if part.strip())
    return tuple(result)


def _template_sections(body: str) -> list[NoticeSection]:
    """目录模板正文按空行切成分段；小标题由模板自己用粗体写在分段首行。"""
    sections: list[NoticeSection] = []
    for block in re.split(r"\n\s*\n", body.strip()):
        lines = tuple(line.rstrip() for line in block.split("\n") if line.strip())
        if lines:
            sections.append(NoticeSection(heading=None, lines=lines))
    return sections


def ops_notice_card(
    catalog: ContentCatalog,
    key: str,
    *,
    tone: NoticeTone,
    fallback_text: str,
    values: Mapping[str, object] | None = None,
    sections: Sequence[NoticeSection] = (),
    trailing: Sequence[NoticeSection] = (),
) -> NoticeCard | None:
    """按卡片键配一张运维通知卡；卡片键未登记或本地造不出卡片时返回 ``None``。

    成卡顺序：目录模板正文的分段 → ``sections``（数据分段，行已由调用方转义）→
    ``trailing``（放在最后的说明分段）。``values`` 逐个转义后填进模板。
    """
    if not catalog.has_card(key):
        return None
    try:
        escaped = {name: escape_markdown(value) for name, value in (values or {}).items()}
        rendered = catalog.card(key, escaped)
        if rendered.button_labels:
            raise ValueError("通知卡不得带按钮文字")
        return NoticeCard(
            title=rendered.title,
            tone=tone,
            sections=(*_template_sections(rendered.body), *sections, *trailing),
            fallback_text=fallback_text,
        )
    except (ContentError, ValueError) as error:
        logger.warning(
            "运维通知卡构造失败，按原文本发送 key=%s error=%s", key, type(error).__name__
        )
        return None


def send_group_notice(
    sender: Any, *, chat_id: str, text: str, card: NoticeCard | None, dedupe_key: str
) -> None:
    """发一条管理群通知：有卡片且发送口支持卡片就发卡片，否则发原文本。

    卡片被飞书明确拒绝时由发送口的 ``send_notice`` 补发一次 ``card.fallback_text``
    （等于 ``text``）；结果不明直接抛出，不在这里补发。
    """
    if card is not None and callable(getattr(sender, "send_notice", None)):
        sender.send_notice(chat_id=chat_id, card=card, dedupe_key=dedupe_key)
        return
    sender.send_text(chat_id=chat_id, text=text, dedupe_key=dedupe_key)


# --------------------------------------------------------------------------
# 文案覆盖文件校验告警（scheduler 启动时至多一条，入口在
# ``apps/scheduler/content_override_notice.py``）
# --------------------------------------------------------------------------

#: 本条投递语义专用的飞书去重前缀（15 + 32 = 47，在 50 字符上限内）。与花名册
#: 日报等共用同一个群、同一个接口，但不能共用前缀，否则飞书会把两条不同的消息
#: 误判成同一逻辑投递。
CONTENT_OVERRIDE_UUID_PREFIX = "lingxi-content-"

#: 前缀与 ``core/alerting.py`` 的运行告警同型：管理群里同一类"系统在说话"的消息
#: 必须长得一样。正文是面向运维的固定短句，**不经 content.toml 文本键**——正被判定
#: 为不可用的恰好就是那份内容目录的外置覆盖，用它渲染自己的故障通知是自指的。
#: 卡片模板在镜像内目录的 ``cards`` 里，外置覆盖碰不到卡片。
CONTENT_OVERRIDE_ALERT_TEXT = (
    "[BI Plus 运行告警] 宿主机上的用户可见文案覆盖文件未通过校验，已被整份忽略，"
    "用户看到的仍是随镜像发布的那一版文案（不影响任何在跑的服务）。"
    "原因码：{reason}。请用 `python -m lingxi.config.content_check <文件>` "
    "校验后重新放置，并重启相关服务。"
)


def report_content_override_rejection(config: Any, *, audit: Any, sender_type: Any) -> None:
    """核对一次外置文案覆盖的加载结果；只有被拒才留审计并发一条管理群告警。

    未配置管理群时只留审计不报错：尚未接线的告警通道不该让进程起不来。发送失败
    只记审计：文案回退本身已经生效，告警发不出去不该拖停启动。去重键取「原因码 +
    覆盖文件摘要」，同一份坏文件反复重启在飞书侧被认作同一条投递。
    ``sender_type`` 是群消息发送口的构造器（按 ``config`` 的飞书配置构造）。
    """
    from lingxi.config.content_override import log_content_source

    source = log_content_source("scheduler")
    reason = source.rejection
    if reason is None:
        return
    audit.record("content.override_rejected", reason=reason)
    if not config.admin_group_chat_id:
        return
    sender = sender_type(
        base_url=config.feishu_base_url,
        app_id=config.feishu_app_id,
        app_secret=str(config.feishu_app_secret),
        uuid_prefix=CONTENT_OVERRIDE_UUID_PREFIX,
    )
    text = CONTENT_OVERRIDE_ALERT_TEXT.format(reason=reason)
    card = ops_notice_card(
        source.catalog,
        CONTENT_OVERRIDE_CARD_KEY,
        tone=NoticeTone.ATTENTION,
        fallback_text=text,
        values={"reason": reason},
    )
    try:
        send_group_notice(
            sender,
            chat_id=str(config.admin_group_chat_id),
            text=text,
            card=card,
            dedupe_key=f"content-override:{reason}:{source.override_digest or 'none'}",
        )
    except Exception as error:
        audit.record("content.override_alert_failed", reason=reason, error=type(error).__name__)
        return
    audit.record("content.override_alert_sent", reason=reason)


__all__ = [
    "ALERT_FAULT_CARD_KEY",
    "ALERT_RECOVERY_CARD_KEY",
    "CONTENT_OVERRIDE_ALERT_TEXT",
    "CONTENT_OVERRIDE_CARD_KEY",
    "CONTENT_OVERRIDE_UUID_PREFIX",
    "DAILY_REPORT_CARD_KEY",
    "OPS_CARD_KEYS",
    "ROSTER_REPORT_CARD_KEY",
    "escaped_lines",
    "ops_notice_card",
    "report_content_override_rejection",
    "send_group_notice",
]
