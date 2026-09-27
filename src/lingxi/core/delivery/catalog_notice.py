"""用户侧状态通知：由内容目录里的卡片键决定「发卡片」还是「发原文本」。

开通、忙碌 / 会话 / 命令、记忆命令、权限范围变化这几类入口经这里挑选展示形式：

- 只有登记在 :data:`NOTICE_TONES` 里的文本键才有资格改成卡片，其余键（管理命令
  回显、群内 @ 提示等例外）无论目录里有没有卡片键都照旧发文本；
- 卡片键 ``notice.<原文本键>`` 未进目录时返回 ``None``，调用方照旧发原文本，与卡片化
  之前逐字相同；撤掉卡片键即退回纯文本；卡片的等价纯文本就是原文本；
- 动态值进卡片 markdown 前一律转义，不会变成可点链接或提及标签；
- 本地没造出卡片（占位不齐、带按钮文字、撞上链接校验）同样返回 ``None`` 并留警告
  日志：这不是「飞书拒绝」，发原文本不算回落，用户照旧收到同一句话。

色调由代码按键固定，内容目录的卡片模板只写标题与正文（``button_labels`` 必须为空）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from lingxi.config.content import ContentCatalog, ContentError, RenderedContent
from lingxi.core.delivery.notice_card import NoticeCard, NoticeSection, NoticeTone, escape_markdown

logger = logging.getLogger(__name__)

#: 通知卡片键的前缀：卡片键 = 前缀 + 原文本键。
NOTICE_CARD_PREFIX = "notice."

#: 有资格改成卡片的文本键与各自的色调。键不在这里就永远发文本。
NOTICE_TONES: Mapping[str, NoticeTone] = {
    # 1 开通过程同步回复 / 2 开通终态主动私聊
    "onboarding.checking": NoticeTone.PROCESSING,
    "onboarding.matched": NoticeTone.PROCESSING,
    "onboarding.syncing": NoticeTone.PROCESSING,
    "onboarding.innertest_waiting": NoticeTone.PROCESSING,
    "onboarding.completed": NoticeTone.DONE,
    "onboarding.preprovisioned_first_chat": NoticeTone.DONE,
    "onboarding.not_authorized": NoticeTone.ATTENTION,
    "onboarding.sync_timeout": NoticeTone.ATTENTION,
    "onboarding.innertest_not_open": NoticeTone.ATTENTION,
    "onboarding.delegated_subject": NoticeTone.ATTENTION,
    "onboarding.internal_error": NoticeTone.FAILURE,
    "onboarding.stalled": NoticeTone.FAILURE,
    # 3 忙碌、会话、命令提示
    "gateway.busy_hint": NoticeTone.ATTENTION,
    "gateway.busy_hint_rejected": NoticeTone.ATTENTION,
    "gateway.suspended": NoticeTone.ATTENTION,
    "gateway.delivery_expired": NoticeTone.ATTENTION,
    "gateway.slash_rejected": NoticeTone.ATTENTION,
    "gateway.queue_failed": NoticeTone.FAILURE,
    "gateway.unexpected_error": NoticeTone.FAILURE,
    "gateway.new_session": NoticeTone.DONE,
    "gateway.session_rotated": NoticeTone.DONE,
    # 6 记忆命令回复（列表条目与类型名是片段，不是一条消息，不在此列）
    "memory.usage_help": NoticeTone.ATTENTION,
    "memory.remember_unsafe": NoticeTone.ATTENTION,
    "memory.limit_exceeded": NoticeTone.ATTENTION,
    "memory.forget_not_found": NoticeTone.ATTENTION,
    "memory.remembered": NoticeTone.DONE,
    "memory.forgotten": NoticeTone.DONE,
    "memory.forgotten_unsafe": NoticeTone.DONE,
    "memory.cleared": NoticeTone.DONE,
    "memory.list_empty": NoticeTone.DONE,
    "memory.list": NoticeTone.DONE,
    # 7 权限范围变化通知
    "permission.range_updated": NoticeTone.DONE,
    "permission.range_revoked": NoticeTone.ATTENTION,
}

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def notice_card_key(text_key: str) -> str:
    """原文本键对应的卡片键。"""
    return NOTICE_CARD_PREFIX + text_key


def build_notice_card(
    catalog: ContentCatalog, content: RenderedContent, values: Mapping[str, object]
) -> NoticeCard | None:
    """按卡片键把一条已渲染文本配成通知卡；不该发或发不成卡片时返回 ``None``。

    ``values`` 是渲染 ``content`` 时用的那组变量，逐个转义后填进卡片模板；卡片
    模板的占位集合必须与原文本键相同，否则目录渲染报错、本函数返回 ``None``。
    """
    tone = NOTICE_TONES.get(content.key)
    card_key = notice_card_key(content.key)
    if tone is None or not catalog.has_card(card_key):
        return None
    try:
        rendered = catalog.card(card_key, {name: escape_markdown(v) for name, v in values.items()})
        if rendered.button_labels:
            raise ValueError("通知卡不得带按钮文字")
        return NoticeCard(
            title=rendered.title,
            tone=tone,
            sections=_sections(rendered.body),
            fallback_text=content.text,
        )
    except (ContentError, ValueError) as error:
        logger.warning(
            "通知卡构造失败，按原文本发送 key=%s error=%s", card_key, type(error).__name__
        )
        return None


def _sections(body: str) -> tuple[NoticeSection, ...]:
    """正文按空行切成分段；分段内按行保留，小标题由模板自己用粗体写。"""
    sections = []
    for block in re.split(r"\n\s*\n", body.strip()):
        lines = tuple(line.rstrip() for line in block.split("\n"))
        sections.append(NoticeSection(heading=None, lines=lines))
    return tuple(sections)


def recover_values(catalog: ContentCatalog, content: RenderedContent) -> dict[str, str] | None:
    """从已渲染文本里取回渲染时用的变量；取不回唯一一组时返回 ``None``。

    用于只拿得到成品文本的集中出口（gateway 同步回复）。拆法唯一才算数：最短与
    最长两种匹配必须给出同一组值，且按这组值重新渲染必须逐字等于原文本；文本
    不是目录现渲染的（被注入改写过）或动态值里恰好含有分隔符时一律取不回，
    调用方照旧发原文本。
    """
    template = catalog.text_template(content.key)
    if template is None:
        return None
    names = _PLACEHOLDER.findall(template)
    if not names:
        return {} if template == content.text else None
    if len(set(names)) != len(names):
        return None
    shortest = _match(template, content.text, lazy=True)
    longest = _match(template, content.text, lazy=False)
    if shortest is None or shortest != longest:
        return None
    if template.format_map(shortest) != content.text:
        return None
    return shortest


def _match(template: str, text: str, *, lazy: bool) -> dict[str, str] | None:
    """按模板拼出的正则整体匹配文本，返回各占位的取值。"""
    group = "(?P<{}>.*?)" if lazy else "(?P<{}>.*)"
    pattern, position = [], 0
    for found in _PLACEHOLDER.finditer(template):
        pattern.append(re.escape(template[position : found.start()]))
        pattern.append(group.format(found.group(1)))
        position = found.end()
    pattern.append(re.escape(template[position:]))
    matched = re.fullmatch("".join(pattern), text, flags=re.DOTALL)
    return None if matched is None else matched.groupdict()


def reply_notice_card(catalog: ContentCatalog, content: RenderedContent) -> NoticeCard | None:
    """只有成品文本时的配卡入口：先取回变量，取不回就不配卡。"""
    if content.key not in NOTICE_TONES or not catalog.has_card(notice_card_key(content.key)):
        return None
    values = recover_values(catalog, content)
    if values is None:
        logger.warning("通知卡变量取不回，按原文本发送 key=%s", content.key)
        return None
    return build_notice_card(catalog, content, values)


def send_catalog_notice(
    sender: Any,
    catalog: ContentCatalog,
    open_id: str,
    key: str,
    values: Mapping[str, object],
    dedupe_key: str,
) -> None:
    """渲染一条目录文本并私聊发给用户本人：有卡片键走 ``send_notice``，否则发原文本。"""
    content = catalog.text(key, **values)
    card = build_notice_card(catalog, content, values)
    if card is not None and hasattr(sender, "send_notice"):
        sender.send_notice(open_id=open_id, card=card, dedupe_key=dedupe_key)
        return
    sender.send_text(open_id=open_id, text=content.text, dedupe_key=dedupe_key)


__all__ = [
    "NOTICE_CARD_PREFIX",
    "NOTICE_TONES",
    "build_notice_card",
    "notice_card_key",
    "recover_values",
    "reply_notice_card",
    "send_catalog_notice",
]
