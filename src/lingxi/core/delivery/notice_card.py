"""通知卡片：只读告知类消息的共用纯数据类型。

一张通知卡只有标题、色调和若干「小标题 + 正文行」分段，序列化后只含
``header`` 与 ``markdown`` / ``hr`` 元素。按钮、表单、回调和跳转地址在类型上
无处可放，构造时再按文本内容拒绝链接与提及标签；每张卡必须带一段等价纯文本，
供飞书明确拒绝卡片时补发。本模块不做任何收发。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any


class NoticeTone(str, Enum):
    """卡片标题栏的五种色调；取值即中文名，便于调用方直接按语义传入。"""

    PROCESSING = "处理中"
    DONE = "完成"
    ATTENTION = "需注意"
    FAILURE = "故障"
    RECOVERY = "恢复"


#: 色调到飞书卡片标题栏颜色模板的映射。故障与恢复必须一眼可分。
_HEADER_TEMPLATES: Mapping[NoticeTone, str] = {
    NoticeTone.PROCESSING: "blue",
    NoticeTone.DONE: "green",
    NoticeTone.ATTENTION: "orange",
    NoticeTone.FAILURE: "red",
    NoticeTone.RECOVERY: "turquoise",
}

#: 任何协议形态的地址（``https://``、``lark://`` 等）、markdown 链接与 HTML 链接标签。
_LINK_PATTERN = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9+.\-]*://|\[[^\]\n]*\]\([^)\n]*\)|<\s*(?:a|link)\b)",
    re.IGNORECASE,
)
#: 提及与交互类标签：通知卡一律不允许，包括 ``<at id=all>`` 这类提及全体。
_ACTIONABLE_TAG_PATTERN = re.compile(r"<\s*/?\s*(?:at|button|form|input|select)\b", re.IGNORECASE)
#: 载荷里一旦出现这些键，就意味着卡片带了可操作元素。
_FORBIDDEN_PAYLOAD_KEYS = frozenset({"behaviors", "callback", "url", "href", "action", "actions"})
#: 载荷里允许出现的 ``tag`` 白名单：标题文字、markdown 分段与分隔线，别的一律拒绝。
_ALLOWED_TAGS = frozenset({"plain_text", "markdown", "hr"})
_MARKDOWN_SPECIALS = frozenset("\\`*_~[]()#+-!|>{}")
_HTML_ENTITIES = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}


def escape_markdown(value: object) -> str:
    """把自由文本转义成在卡片 markdown 里原样显示的文字。

    用于管理员或用户可填的动态值：markdown 标点加反斜杠，``& < >`` 转成实体，
    地址里的 ``://`` 插入零宽空格断开，使其不能成为可点链接或提及标签。
    """
    text = str(value)
    pieces: list[str] = []
    for character in text:
        if character in _HTML_ENTITIES:
            pieces.append(_HTML_ENTITIES[character])
        elif character in _MARKDOWN_SPECIALS:
            pieces.append("\\" + character)
        else:
            pieces.append(character)
    return "".join(pieces).replace("://", ":​//")


@dataclass(frozen=True)
class NoticeSection:
    """卡片里的一段：可选小标题加若干正文行，行内容已由调用方渲染并转义。"""

    heading: str | None
    lines: tuple[str, ...]


def _check_text(value: object, *, field: str, allow_links: bool) -> str:
    """校验一段将进入卡片的文字；拒绝非文本、链接与交互标签。错误不回显正文。"""
    if not isinstance(value, str):
        raise ValueError(f"通知卡的{field}必须是文本，拒绝按钮、链接或回调元素")
    if _ACTIONABLE_TAG_PATTERN.search(value):
        raise ValueError(f"通知卡的{field}不得包含提及或交互标签")
    if not allow_links and _LINK_PATTERN.search(value):
        raise ValueError(f"通知卡的{field}不得包含链接")
    return value


def _normalize_section(raw: object, *, allow_links: bool) -> NoticeSection:
    """把 ``NoticeSection`` 或 ``(heading, lines)`` 二元组规整成已校验的分段。"""
    if isinstance(raw, NoticeSection):
        heading, lines = raw.heading, raw.lines
    elif isinstance(raw, tuple | list) and len(raw) == 2:
        heading, lines = raw
    else:
        raise ValueError("通知卡分段必须是（小标题, 正文行）二元组，拒绝按钮、链接或回调元素")
    if heading is not None:
        _check_text(heading, field="小标题", allow_links=False)
    if isinstance(lines, str) or not isinstance(lines, Sequence) or not lines:
        raise ValueError("通知卡分段的正文行必须是非空的文本序列")
    checked = tuple(_check_text(line, field="正文行", allow_links=allow_links) for line in lines)
    return NoticeSection(heading=heading, lines=checked)


@dataclass(frozen=True)
class NoticeCard:
    """一张只读通知卡。构造即校验：不合格的卡片不能存在，更不能被发出去。"""

    title: str
    tone: NoticeTone
    sections: tuple[NoticeSection, ...]
    fallback_text: str
    allow_links: bool = False

    def __post_init__(self) -> None:
        """规整色调与分段，并执行全部拒绝规则。"""
        title = _check_text(self.title, field="标题", allow_links=False)
        if not title.strip():
            raise ValueError("通知卡标题不能为空")
        object.__setattr__(self, "tone", NoticeTone(self.tone))
        if isinstance(self.sections, str | Mapping) or not isinstance(self.sections, Sequence):
            raise ValueError("通知卡分段必须是序列")
        if not self.sections:
            raise ValueError("通知卡至少要有一个分段")
        sections = tuple(
            _normalize_section(raw, allow_links=self.allow_links) for raw in self.sections
        )
        object.__setattr__(self, "sections", sections)
        if not isinstance(self.fallback_text, str) or not self.fallback_text.strip():
            raise ValueError("通知卡必须带非空的等价纯文本")

    def to_payload(self) -> dict[str, Any]:
        """序列化成飞书 schema 2.0 卡片：只含标题栏与 markdown / 分隔线元素。"""
        elements: list[dict[str, Any]] = []
        for index, section in enumerate(self.sections):
            if index:
                elements.append({"tag": "hr"})
            body = "\n".join(section.lines)
            if section.heading is not None:
                body = f"**{section.heading}**\n{body}"
            elements.append({"tag": "markdown", "content": body})
        payload = {
            "schema": "2.0",
            "header": {
                "title": {"tag": "plain_text", "content": self.title},
                "template": _HEADER_TEMPLATES[self.tone],
            },
            "body": {"elements": elements},
        }
        assert_no_actionable_elements(payload)
        return payload


def assert_no_actionable_elements(payload: Any) -> None:
    """递归检查卡片载荷：出现可操作字段或白名单以外的元素即抛 ``ValueError``。"""
    if isinstance(payload, Mapping):
        forbidden = _FORBIDDEN_PAYLOAD_KEYS.intersection(payload)
        if forbidden:
            raise ValueError("通知卡载荷不得包含可操作字段：" + ",".join(sorted(forbidden)))
        tag = payload.get("tag")
        if tag is not None and tag not in _ALLOWED_TAGS:
            raise ValueError("通知卡载荷只允许标题、markdown 与分隔线元素")
        for value in payload.values():
            assert_no_actionable_elements(value)
    elif isinstance(payload, list | tuple):
        for item in payload:
            assert_no_actionable_elements(item)


__all__ = [
    "NoticeCard",
    "NoticeSection",
    "NoticeTone",
    "assert_no_actionable_elements",
    "escape_markdown",
]
