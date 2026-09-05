"""欢迎卡的三条取值规则与三种样式（纯函数，无网络、无数据库）。

三条取值规则随文案一并批准：示例随范围变（单公司写公司名，多个或
全部写「各公司」）、长列表设上限（公司超过五个折叠成计数）、姓名取花名册原文。
它们决定的是**说什么**，与卡片长什么样无关。

样式（D-1）由产品负责人看过真机样卡后定稿为**乙＝字段列表**；甲与丙仍可用枚举
切换。**唯一的样式切换点**是 :func:`build_card_payload` 与
:data:`DEFAULT_WELCOME_CARD_STYLE`：改判只改这一个常量，文案、取值规则、发送编排、
记录一行都不用动。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from lingxi.config.content import ContentCatalog, default_content_catalog

#: 审计与记录里代表**整张欢迎卡**的内容键。卡片按段落拆成多个文案键（样式可换、
#: 文案不动），但记录里必须有一个能一眼认出"发的是哪张卡"的键；取它们共同的前缀，
#: 由 :func:`welcome_text_keys` 与用例钉住"每一个用到的键都在这个前缀下"。
WELCOME_CONTENT_KEY = "outreach.welcome"

#: 公司列表的折叠阈值：超过这个数量就不再逐个列出（批准的规则二）。
COMPANY_LIST_LIMIT = 5

#: 多个公司或全部公司时，示例里代替公司名的那个词的键。
_KEY_COMPANY_WORD_MULTI = f"{WELCOME_CONTENT_KEY}.company_word_multi"
_KEY_COMPANY_SCOPE_ALL = f"{WELCOME_CONTENT_KEY}.company_scope_all"
_KEY_COMPANY_SCOPE_FOLDED = f"{WELCOME_CONTENT_KEY}.company_scope_folded"

#: 列表分隔符、示例行首的圆点、字段标题与正文之间的冒号。都是标点不是措辞，因此
#: 留在代码里而不是内容目录里（同 ``core/permission/notification.SCOPE_SEPARATOR``
#: 的处置）。
SCOPE_SEPARATOR = "、"
EXAMPLE_BULLET = "· "
FIELD_SEPARATOR = "："

#: 字段列表样式里两列的宽度权重：标题窄、正文宽。
FIELD_LABEL_WEIGHT = 1
FIELD_VALUE_WEIGHT = 4

#: 脚注的灰字包装。是排版而不是措辞——脚注的字由内容目录给，这里只决定它以次要
#: 信息的样子出现。
FOOTNOTE_MARKDOWN = "<font color='grey'>{footnote}</font>"


class WelcomeCardStyle(str, Enum):
    """D-1 的三个候选样式。

    默认乙（产品负责人裁定的定稿样式），改判只动 :data:`DEFAULT_WELCOME_CARD_STYLE`。
    """

    HEADER_MARKDOWN = "header_markdown"
    FIELD_LIST = "field_list"
    PLAIN_MARKDOWN = "plain_markdown"


#: 产品负责人看过真机样卡后的定稿：乙＝标题栏＋字段列表（标题｜正文成对）＋无按钮。
DEFAULT_WELCOME_CARD_STYLE = WelcomeCardStyle.FIELD_LIST


@dataclass(frozen=True)
class WelcomeAudience:
    """一个人的取值输入：姓名取花名册原文，范围取**合成后**权限。

    ``company_ids``/``all_companies`` 与 ``metric_names`` 都来自同一份已经发布出去
    的权限文档，不在这里另算一遍；``company_names`` 是编号→中文名，查不到就失败
    关闭（见 :meth:`company_label`），装配层据此把这个人整条跳过。
    """

    display_name: str
    company_ids: tuple[str, ...]
    all_companies: bool
    metric_names: tuple[str, ...]
    company_names: Mapping[str, str]
    total_company_count: int

    def __post_init__(self) -> None:
        """失败关闭：范围说不清楚的人不该收到一张说错范围的卡。"""
        if not self.display_name.strip():
            raise ValueError("欢迎卡必须带姓名（花名册原文），不自造姓名")
        if not self.metric_names:
            raise ValueError("没有任何可用指标的人不发欢迎卡")
        if self.all_companies:
            if self.total_company_count < 1:
                raise ValueError("全部公司范围必须知道公司总数，否则数字会说错")
        elif not self.company_ids:
            raise ValueError("欢迎卡必须至少有一个公司范围")

    @property
    def single_company(self) -> str | None:
        """恰好一个具体公司时返回它的展示名，否则 ``None``（规则一的判据）。"""
        if self.all_companies or len(self.company_ids) != 1:
            return None
        return self.company_label(self.company_ids[0])

    def company_label(self, company_id: str) -> str:
        """公司的中文名；查不到**不回落编号**，直接失败关闭。

        编号是内部标识：把「15」印在一张给用户看的欢迎卡上，既不是他看得懂的东西，
        也说不清他的范围。装配层（``core/outreach/audience``）会先判掉这种人并给出
        ``company_name_missing``，这里是同一条规则的最后一道。
        """
        name = self.company_names.get(company_id)
        if not name:
            raise ValueError("公司中文名查不到：不回落编号，这个人不发欢迎卡")
        return name


@dataclass(frozen=True)
class WelcomeCard:
    """一张已渲染的欢迎卡：可发送的载荷 + 可进记录的事实（不含正文）。"""

    content_key: str
    content_version: str
    style: WelcomeCardStyle
    title: str
    sections: tuple[str, ...]
    lead: str
    fields: tuple[tuple[str, str], ...]
    footnote: str
    payload: dict[str, Any]

    def audit_facts(self) -> dict[str, str]:
        """可直接进发送记录的事实：**没有正文**。"""
        return {
            "content_key": self.content_key,
            "content_version": self.content_version,
            "card_style": self.style.value,
        }


def company_scope_text(audience: WelcomeAudience, *, catalog: ContentCatalog) -> str:
    """规则二（长列表设上限）：公司位显示成什么。

    通配（全非）一律显示「全部公司（N 家）」——N 是当前可用公司总数，不是这个人
    权限文档里的键数；超过 :data:`COMPANY_LIST_LIMIT` 个具体公司折叠成计数；其余
    逐个列出，中文名优先。
    """
    if audience.all_companies:
        return catalog.text(_KEY_COMPANY_SCOPE_ALL, count=audience.total_company_count).text
    if len(audience.company_ids) > COMPANY_LIST_LIMIT:
        return catalog.text(_KEY_COMPANY_SCOPE_FOLDED, count=len(audience.company_ids)).text
    return SCOPE_SEPARATOR.join(
        audience.company_label(company_id) for company_id in audience.company_ids
    )


def example_company_word(audience: WelcomeAudience, *, catalog: ContentCatalog) -> str:
    """规则一（示例随范围变）：示例句里公司位放什么。

    只管一个公司的人看到「各公司」会问出他拿不到的结果，因此单公司必须把公司名
    写进示例；多个或全部一律用同一个展示词。
    """
    single = audience.single_company
    if single is not None:
        return single
    return catalog.text(_KEY_COMPANY_WORD_MULTI).text


def metric_names_text(audience: WelcomeAudience) -> str:
    """规则二后半句：指标当前最多九个，全列，顿号分隔。"""
    return SCOPE_SEPARATOR.join(audience.metric_names)


def _example_lines(audience: WelcomeAudience, *, catalog: ContentCatalog) -> tuple[str, ...]:
    """三条引导：两条按这个人自己的范围与指标取值，第三条固定。

    第二条取第二个指标；只有一个指标时退回第一个——两句的时间范围不同，不会变成
    重复的一句话。
    """
    company_word = example_company_word(audience, catalog=catalog)
    first = audience.metric_names[0]
    second = audience.metric_names[1] if len(audience.metric_names) > 1 else first
    return (
        catalog.text(
            f"{WELCOME_CONTENT_KEY}.example_recent",
            company_word=company_word,
            metric_name=first,
        ).text,
        catalog.text(
            f"{WELCOME_CONTENT_KEY}.example_last_month",
            company_word=company_word,
            metric_name=second,
        ).text,
        catalog.text(f"{WELCOME_CONTENT_KEY}.example_document").text,
    )


def welcome_greeting(audience: WelcomeAudience, *, catalog: ContentCatalog) -> str:
    """规则三（姓名取花名册原文）：问好那一句。

    只去首尾空白，带英文名后缀的按原文显示、不截断——截断规则待定，自己发明一个
    等于替产品决定这个人叫什么。全模块只有这一处取姓名。
    """
    return catalog.text(f"{WELCOME_CONTENT_KEY}.greeting", name=audience.display_name.strip()).text


def welcome_lead(audience: WelcomeAudience, *, catalog: ContentCatalog) -> str:
    """开头那一段：问好 + 自我介绍，两句合成一个 markdown 元素。"""
    intro = catalog.text(f"{WELCOME_CONTENT_KEY}.intro").text
    return f"{welcome_greeting(audience, catalog=catalog)}\n\n{intro}"


def welcome_fields(
    audience: WelcomeAudience, *, catalog: ContentCatalog | None = None
) -> tuple[tuple[str, str], ...]:
    """四个「标题｜正文」字段：公司、指标、可以这样开始、遇到问题。

    字段列表样式把标题放进左列、正文放进右列；markdown 分段样式用
    :data:`FIELD_SEPARATOR` 把两半拼成一行。两种读法共用同一份字。
    """
    source = catalog or default_content_catalog()
    key = WELCOME_CONTENT_KEY
    examples = "\n".join(
        f"{EXAMPLE_BULLET}{line}" for line in _example_lines(audience, catalog=source)
    )
    return (
        (
            source.text(f"{key}.field_company").text,
            company_scope_text(audience, catalog=source),
        ),
        (source.text(f"{key}.field_metric").text, metric_names_text(audience)),
        (source.text(f"{key}.examples_heading").text, examples),
        (source.text(f"{key}.contact_heading").text, source.text(f"{key}.contact_body").text),
    )


def welcome_sections(
    audience: WelcomeAudience, *, catalog: ContentCatalog | None = None
) -> tuple[str, ...]:
    """按批准文案的五个要素产出正文分段：问好、自我介绍、范围、引导、出问题找谁。

    末段是脚注。分段本身与样式无关；字段列表样式不用这一份，用
    :func:`welcome_fields`——两者取的是同一批文案键与同一批取值。
    """
    source = catalog or default_content_catalog()
    key = WELCOME_CONTENT_KEY
    company, metric, examples, contact = welcome_fields(audience, catalog=source)
    scope = "\n".join(
        (
            source.text(f"{key}.scope_heading").text,
            f"{company[0]}{FIELD_SEPARATOR}{company[1]}",
            f"{metric[0]}{FIELD_SEPARATOR}{metric[1]}",
        )
    )
    return (
        welcome_greeting(audience, catalog=source),
        source.text(f"{key}.intro").text,
        scope,
        "\n".join(examples),
        "\n".join(contact),
        source.text(f"{key}.footnote").text,
    )


def _markdown(content: str) -> dict[str, Any]:
    return {"tag": "markdown", "content": content}


def _field_row(label: str, value: str) -> dict[str, Any]:
    """一行「标题｜正文」两列。

    ``flex_mode`` 用 ``none``：两列的宽窄由各自的 ``weight`` 决定，用
    ``bisect`` 会把它们强行平分，标题那一列因此撑出大片空白。
    """
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": FIELD_LABEL_WEIGHT,
                "elements": [_markdown(f"**{label}**")],
            },
            {
                "tag": "column",
                "width": "weighted",
                "weight": FIELD_VALUE_WEIGHT,
                "elements": [_markdown(value)],
            },
        ],
    }


def build_card_payload(
    *,
    title: str,
    sections: Sequence[str],
    lead: str,
    fields: Sequence[tuple[str, str]],
    footnote: str,
    style: WelcomeCardStyle,
) -> dict[str, Any]:
    """**唯一的样式切换点**：把同一份文案摆成一张 schema 2.0 卡片。

    乙（默认）＝标题栏 + 问好/自我介绍 + 分隔线 + 四行「标题｜正文」+ 灰字脚注；
    甲＝标题栏 + 每段一个 markdown 元素；丙＝无标题栏、全部分段合成一段。三者
    共用同一份文案与同一份取值，改判只换枚举值。
    """
    if not sections or not fields:
        raise ValueError("欢迎卡至少要有一段正文与一个字段")
    card: dict[str, Any] = {"schema": "2.0", "config": {"update_multi": True}}
    if style is WelcomeCardStyle.PLAIN_MARKDOWN:
        card["body"] = {"elements": [_markdown("\n\n".join(sections))]}
        return card
    card["header"] = {"title": {"tag": "plain_text", "content": title}}
    if style is WelcomeCardStyle.FIELD_LIST:
        elements: list[dict[str, Any]] = [_markdown(lead), {"tag": "hr"}]
        elements.extend(_field_row(label, value) for label, value in fields)
        elements.append(_markdown(FOOTNOTE_MARKDOWN.format(footnote=footnote)))
        card["body"] = {"elements": elements}
        return card
    card["body"] = {"elements": [_markdown(section) for section in sections]}
    return card


def render_welcome_card(
    audience: WelcomeAudience,
    *,
    style: WelcomeCardStyle = DEFAULT_WELCOME_CARD_STYLE,
    catalog: ContentCatalog | None = None,
) -> WelcomeCard:
    """把一个人的范围渲染成一张可发送的欢迎卡。

    预检与正式发送必须调用**这一个**函数，否则预检验不出真问题；静态断言见
    ``tests/test_outreach_ops.py``。
    """
    source = catalog or default_content_catalog()
    sections = welcome_sections(audience, catalog=source)
    fields = welcome_fields(audience, catalog=source)
    title = source.text(f"{WELCOME_CONTENT_KEY}.title").text
    lead = welcome_lead(audience, catalog=source)
    footnote = source.text(f"{WELCOME_CONTENT_KEY}.footnote").text
    return WelcomeCard(
        content_key=WELCOME_CONTENT_KEY,
        content_version=source.version,
        style=style,
        title=title,
        sections=sections,
        lead=lead,
        fields=fields,
        footnote=footnote,
        payload=build_card_payload(
            title=title,
            sections=sections,
            lead=lead,
            fields=fields,
            footnote=footnote,
            style=style,
        ),
    )


def welcome_text_keys(catalog: ContentCatalog | None = None) -> tuple[str, ...]:
    """目录里属于这张卡的全部文案键，供"内容键真的代表整张卡"的用例核对。"""
    source = catalog or default_content_catalog()
    prefix = f"{WELCOME_CONTENT_KEY}."
    return tuple(key for key in source.text_keys() if key.startswith(prefix))


__all__ = [
    "COMPANY_LIST_LIMIT",
    "DEFAULT_WELCOME_CARD_STYLE",
    "EXAMPLE_BULLET",
    "FIELD_LABEL_WEIGHT",
    "FIELD_SEPARATOR",
    "FIELD_VALUE_WEIGHT",
    "FOOTNOTE_MARKDOWN",
    "SCOPE_SEPARATOR",
    "WELCOME_CONTENT_KEY",
    "WelcomeAudience",
    "WelcomeCard",
    "WelcomeCardStyle",
    "build_card_payload",
    "company_scope_text",
    "example_company_word",
    "metric_names_text",
    "render_welcome_card",
    "welcome_fields",
    "welcome_greeting",
    "welcome_lead",
    "welcome_sections",
    "welcome_text_keys",
]
