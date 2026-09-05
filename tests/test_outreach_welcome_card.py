"""欢迎卡的三条取值规则与样式切换点（Issue #586 完成标准 1）。

三条规则随文案一并批准，变异任一条这里就该变红：

- **示例随范围变**：单公司把公司名写进示例；多个或全部用「各公司」。只管一个
  公司的人看到「各公司」会问出他拿不到的结果。
- **长列表设上限**：公司超过五个折叠成计数；通配说「全部公司（N 家）」；指标全列。
- **姓名取花名册原文**：带英文名后缀的按原文显示，不截断、不自造。

否定面：没有姓名、没有指标、通配却不知道公司总数——一律构造期失败关闭，不产出
一张说错范围的卡；样式换成乙/丙时文案一个字不变。
"""

from __future__ import annotations

import unittest

from lingxi.config.content import default_content_catalog
from lingxi.core.outreach.welcome_card import (
    COMPANY_LIST_LIMIT,
    DEFAULT_WELCOME_CARD_STYLE,
    FIELD_LABEL_WEIGHT,
    FIELD_VALUE_WEIGHT,
    FOOTNOTE_MARKDOWN,
    WELCOME_CONTENT_KEY,
    WelcomeAudience,
    WelcomeCardStyle,
    company_scope_text,
    example_company_word,
    metric_names_text,
    render_welcome_card,
    welcome_sections,
    welcome_text_keys,
)

CATALOG = default_content_catalog()
ROSTER_NAME = "王晋 (Joshua Wang)"
METRICS = ("充值金额", "日活用户数")


def _audience(
    *,
    company_ids: tuple[str, ...] = ("1011",),
    all_companies: bool = False,
    metric_names: tuple[str, ...] = METRICS,
    company_names: dict[str, str] | None = None,
    total_company_count: int = 43,
) -> WelcomeAudience:
    return WelcomeAudience(
        display_name=ROSTER_NAME,
        company_ids=company_ids,
        all_companies=all_companies,
        metric_names=metric_names,
        company_names=company_names if company_names is not None else {"1011": "尼日利亚"},
        total_company_count=total_company_count,
    )


class ExampleScopeRuleTest(unittest.TestCase):
    """规则一：示例随范围变。"""

    def test_a_single_company_puts_its_name_into_the_example(self) -> None:
        word = example_company_word(_audience(), catalog=CATALOG)
        self.assertEqual(word, "尼日利亚")
        sections = welcome_sections(_audience(), catalog=CATALOG)
        self.assertIn("最近七天尼日利亚的充值金额是多少", sections[3])

    def test_several_companies_use_the_shared_word_instead_of_a_company_name(self) -> None:
        audience = _audience(
            company_ids=("1011", "1012"), company_names={"1011": "尼日利亚", "1012": "肯尼亚"}
        )
        self.assertEqual(example_company_word(audience, catalog=CATALOG), "各公司")
        self.assertIn(
            "最近七天各公司的充值金额是多少", welcome_sections(audience, catalog=CATALOG)[3]
        )

    def test_all_companies_use_the_shared_word_too(self) -> None:
        audience = _audience(company_ids=(), all_companies=True)
        self.assertEqual(example_company_word(audience, catalog=CATALOG), "各公司")

    def test_an_unknown_company_id_is_refused_instead_of_showing_the_number(self) -> None:
        """否定断言：查不到中文名不回落编号。

        编号是内部标识；把「9999」印在一张给用户看的卡上既看不懂，也说不清范围。
        装配层（``core/outreach/audience``）据此把这个人整条跳过。
        """
        audience = _audience(company_ids=("9999",), company_names={})
        with self.assertRaises(ValueError):
            example_company_word(audience, catalog=CATALOG)

    def test_the_second_example_uses_the_second_metric(self) -> None:
        sections = welcome_sections(_audience(), catalog=CATALOG)
        self.assertIn("上个月尼日利亚的日活用户数是多少", sections[3])

    def test_one_metric_only_still_yields_two_distinct_examples(self) -> None:
        sections = welcome_sections(_audience(metric_names=("充值金额",)), catalog=CATALOG)
        self.assertIn("最近七天尼日利亚的充值金额是多少", sections[3])
        self.assertIn("上个月尼日利亚的充值金额是多少", sections[3])


class LongListRuleTest(unittest.TestCase):
    """规则二：长列表设上限。"""

    def test_the_approved_threshold_is_five_companies(self) -> None:
        """批准的规则写死「公司超过 5 个」——阈值本身是产品裁定，不是可调参数。"""
        self.assertEqual(COMPANY_LIST_LIMIT, 5)

    def test_five_companies_are_still_listed_one_by_one(self) -> None:
        ids = ("1011", "1012", "1013", "1014", "1015")
        names = {key: f"公司{key}" for key in ids}
        audience = _audience(company_ids=ids, company_names=names)
        self.assertEqual(
            company_scope_text(audience, catalog=CATALOG), "、".join(names[key] for key in ids)
        )

    def test_six_companies_fold_into_a_count(self) -> None:
        ids = ("1011", "1012", "1013", "1014", "1015", "1016")
        audience = _audience(company_ids=ids, company_names={})
        self.assertEqual(company_scope_text(audience, catalog=CATALOG), "6 家公司")

    def test_the_wildcard_scope_says_all_companies_with_the_real_total(self) -> None:
        audience = _audience(company_ids=(), all_companies=True, total_company_count=43)
        self.assertEqual(company_scope_text(audience, catalog=CATALOG), "全部公司（43 家）")

    def test_every_metric_is_listed_separated_by_the_chinese_enumeration_comma(self) -> None:
        nine = tuple(f"指标{index}" for index in range(9))
        self.assertEqual(metric_names_text(_audience(metric_names=nine)), "、".join(nine))


class RosterNameRuleTest(unittest.TestCase):
    """规则三：姓名取花名册原文。"""

    def test_the_roster_name_is_shown_verbatim_including_the_english_suffix(self) -> None:
        sections = welcome_sections(_audience(), catalog=CATALOG)
        self.assertEqual(sections[0], f"你好，{ROSTER_NAME}。")

    def test_a_blank_name_is_refused_before_anything_is_rendered(self) -> None:
        """否定断言：不自造姓名。"""
        with self.assertRaises(ValueError):
            WelcomeAudience(
                display_name="   ",
                company_ids=("1011",),
                all_companies=False,
                metric_names=METRICS,
                company_names={},
                total_company_count=1,
            )


class FailClosedTest(unittest.TestCase):
    def test_a_person_without_any_metric_gets_no_card(self) -> None:
        with self.assertRaises(ValueError):
            _audience(metric_names=())

    def test_a_wildcard_scope_without_a_known_total_is_refused(self) -> None:
        """否定断言：数字说不清楚就不发，不猜一个公司总数。"""
        with self.assertRaises(ValueError):
            _audience(company_ids=(), all_companies=True, total_company_count=0)

    def test_a_person_without_any_company_gets_no_card(self) -> None:
        with self.assertRaises(ValueError):
            _audience(company_ids=())


class CardShapeTest(unittest.TestCase):
    """定稿样式（乙）：标题栏 + 问好/自我介绍 + 分隔线 + 四行「标题｜正文」+ 灰字脚注。"""

    def test_the_default_style_is_the_field_list(self) -> None:
        card = render_welcome_card(_audience(), catalog=CATALOG)
        self.assertIs(card.style, WelcomeCardStyle.FIELD_LIST)
        self.assertIs(card.style, DEFAULT_WELCOME_CARD_STYLE)
        self.assertEqual(card.payload["schema"], "2.0")
        self.assertEqual(card.payload["header"]["title"]["content"], "欢迎使用 BI Plus")
        self.assertNotIn("button", str(card.payload))

    def test_the_field_list_lays_out_lead_rule_fields_and_footnote_in_order(self) -> None:
        elements = render_welcome_card(_audience(), catalog=CATALOG).payload["body"]["elements"]
        self.assertEqual(
            [element["tag"] for element in elements],
            ["markdown", "hr", "column_set", "column_set", "column_set", "column_set", "markdown"],
        )
        self.assertIn(ROSTER_NAME, elements[0]["content"])
        self.assertIn("四达时代的经营数据助手", elements[0]["content"])

    def test_every_field_row_is_a_bold_label_beside_its_value(self) -> None:
        elements = render_welcome_card(_audience(), catalog=CATALOG).payload["body"]["elements"]
        rows = [element for element in elements if element["tag"] == "column_set"]
        labels = [row["columns"][0]["elements"][0]["content"] for row in rows]
        self.assertEqual(labels, ["**公司**", "**指标**", "**可以这样开始**", "**遇到问题**"])
        for row in rows:
            self.assertEqual(row["flex_mode"], "none")
            self.assertEqual(row["columns"][0]["weight"], FIELD_LABEL_WEIGHT)
            self.assertEqual(row["columns"][1]["weight"], FIELD_VALUE_WEIGHT)
        values = [row["columns"][1]["elements"][0]["content"] for row in rows]
        self.assertEqual(values[0], "尼日利亚")
        self.assertEqual(values[1], "充值金额、日活用户数")

    def test_the_footnote_is_the_last_element_and_rendered_as_secondary_text(self) -> None:
        elements = render_welcome_card(_audience(), catalog=CATALOG).payload["body"]["elements"]
        self.assertEqual(
            elements[-1]["content"],
            FOOTNOTE_MARKDOWN.format(footnote="本条由 BI Plus 主动发送，你可以直接开始提问。"),
        )

    def test_switching_the_style_changes_the_layout_and_not_a_single_word(self) -> None:
        """样式是唯一的切换点：三种样式的正文分段与字段逐字相同。"""
        cards = {
            style: render_welcome_card(_audience(), style=style, catalog=CATALOG)
            for style in WelcomeCardStyle
        }
        reference = cards[WelcomeCardStyle.FIELD_LIST]
        for style, card in cards.items():
            self.assertEqual(card.sections, reference.sections, style)
            self.assertEqual(card.fields, reference.fields, style)
            self.assertEqual(card.footnote, reference.footnote, style)

    def test_the_plain_markdown_style_has_no_header(self) -> None:
        card = render_welcome_card(
            _audience(), style=WelcomeCardStyle.PLAIN_MARKDOWN, catalog=CATALOG
        )
        self.assertNotIn("header", card.payload)
        self.assertEqual(len(card.payload["body"]["elements"]), 1)

    def test_the_header_markdown_style_is_one_element_per_section(self) -> None:
        card = render_welcome_card(
            _audience(), style=WelcomeCardStyle.HEADER_MARKDOWN, catalog=CATALOG
        )
        self.assertEqual(card.payload["header"]["title"]["content"], "欢迎使用 BI Plus")
        tags = {element["tag"] for element in card.payload["body"]["elements"]}
        self.assertEqual(tags, {"markdown"})
        self.assertEqual(len(card.payload["body"]["elements"]), len(card.sections))


class ContentKeyTest(unittest.TestCase):
    def test_the_audit_key_covers_every_text_key_this_card_uses(self) -> None:
        """记录里那一个内容键必须真的代表整张卡，否则回查指向不到实际发出的字。"""
        keys = welcome_text_keys(CATALOG)
        self.assertTrue(keys)
        for key in keys:
            self.assertTrue(key.startswith(f"{WELCOME_CONTENT_KEY}."))

    def test_the_recorded_facts_carry_no_body_text(self) -> None:
        facts = render_welcome_card(_audience(), catalog=CATALOG).audit_facts()
        self.assertEqual(set(facts), {"content_key", "content_version", "card_style"})
        self.assertNotIn(ROSTER_NAME, str(facts))

    def test_the_contact_line_has_no_clickable_link(self) -> None:
        """#541 裁定：联系方式不放可点击链接。"""
        sections = welcome_sections(_audience(), catalog=CATALOG)
        contact = sections[4]
        self.assertIn("wangzp@startimes.com.cn", contact)
        self.assertNotIn("http", contact)
        self.assertNotIn("](", contact)

    def test_the_card_never_shows_the_internal_product_name(self) -> None:
        """用户可见面只叫 BI Plus。"""
        card = render_welcome_card(_audience(), catalog=CATALOG)
        self.assertNotIn("Lingxi", str(card.payload))
        self.assertNotIn("lingxi", str(card.payload))


if __name__ == "__main__":
    unittest.main()
