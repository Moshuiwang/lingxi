"""通知卡共用类型与内容目录 ``has_card`` 查询。

全部断言是纯数据断言，不发请求。否定面：

- 按钮、链接、回调元素、提及全体**构造不出来**；
- 没有等价纯文本的卡**构造不出来**；
- 自由文本转义后**不产生**可点链接、markdown 样式或提及标签；
- 卡片键未进目录时 ``has_card`` 为 ``False``。
"""

from __future__ import annotations

import json
import unittest

from lingxi.config.content import default_content_catalog
from lingxi.core.delivery.notice_card import (
    NoticeCard,
    NoticeSection,
    NoticeTone,
    assert_no_actionable_elements,
    escape_markdown,
)


def _card(**overrides) -> NoticeCard:
    values = {
        "title": "每日通报",
        "tone": NoticeTone.DONE,
        "sections": [("概况", ("今日新开通 2 人。",)), (None, ("无异常。",))],
        "fallback_text": "每日通报\n概况\n今日新开通 2 人。\n无异常。",
    }
    values.update(overrides)
    return NoticeCard(**values)


class NoticeCardRejectsActionableElementsTest(unittest.TestCase):
    def test_a_button_element_cannot_be_a_section_line(self) -> None:
        button = {"tag": "button", "text": {"tag": "plain_text", "content": "确认"}}
        with self.assertRaises(ValueError):
            _card(sections=[("操作", (button,))])

    def test_a_callback_element_cannot_be_a_section(self) -> None:
        callback = {"tag": "markdown", "behaviors": [{"type": "callback", "value": {}}]}
        with self.assertRaises(ValueError):
            _card(sections=[callback])

    def test_markdown_link_is_rejected_by_default(self) -> None:
        with self.assertRaises(ValueError):
            _card(sections=[("详情", ("点[这里](https://example.invalid/x)查看",))])

    def test_bare_address_is_rejected_by_default(self) -> None:
        for line in ("见 https://example.invalid/a", "打开 lark://applink/x", '<a href="x">y</a>'):
            with self.subTest(line=line), self.assertRaises(ValueError):
                _card(sections=[(None, (line,))])

    def test_link_in_title_or_heading_is_rejected_even_when_links_allowed(self) -> None:
        with self.assertRaises(ValueError):
            _card(title="见 https://example.invalid", allow_links=True)
        with self.assertRaises(ValueError):
            _card(sections=[("https://example.invalid", ("正文",))], allow_links=True)

    def test_link_in_body_line_is_accepted_only_when_allowed(self) -> None:
        card = _card(sections=[(None, ("文档：https://example.invalid/doc",))], allow_links=True)
        self.assertIn("https://example.invalid/doc", json.dumps(card.to_payload()))

    def test_mention_all_is_rejected_even_when_links_allowed(self) -> None:
        with self.assertRaises(ValueError):
            _card(sections=[(None, ("<at id=all></at> 请注意",))], allow_links=True)

    def test_payload_contains_only_header_markdown_and_hr(self) -> None:
        payload = _card().to_payload()
        self.assertEqual(set(payload), {"schema", "header", "body"})
        tags = [element["tag"] for element in payload["body"]["elements"]]
        self.assertEqual(tags, ["markdown", "hr", "markdown"])
        text = json.dumps(payload, ensure_ascii=False)
        for forbidden in ("button", "behaviors", "callback", '"url"'):
            self.assertNotIn(forbidden, text)

    def test_payload_guard_rejects_actionable_payloads(self) -> None:
        for payload in (
            {"body": {"elements": [{"tag": "button"}]}},
            {"body": {"elements": [{"tag": "markdown", "behaviors": []}]}},
            {"card_link": {"url": "https://example.invalid"}},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                assert_no_actionable_elements(payload)


class NoticeCardShapeTest(unittest.TestCase):
    def test_fallback_text_is_required(self) -> None:
        for fallback in ("", "   ", None):
            with self.subTest(fallback=fallback), self.assertRaises(ValueError):
                _card(fallback_text=fallback)

    def test_tone_accepts_chinese_name_and_maps_to_distinct_header_colors(self) -> None:
        colors = {
            tone: _card(tone=tone.value).to_payload()["header"]["template"] for tone in NoticeTone
        }
        self.assertEqual(len(set(colors.values())), 5)
        self.assertNotEqual(colors[NoticeTone.FAILURE], colors[NoticeTone.RECOVERY])
        with self.assertRaises(ValueError):
            _card(tone="紧急")

    def test_sections_are_normalized_to_notice_sections(self) -> None:
        card = _card()
        self.assertEqual(card.sections[0], NoticeSection(heading="概况", lines=("今日新开通 2 人。",)))
        self.assertEqual(card.to_payload()["body"]["elements"][0]["content"], "**概况**\n今日新开通 2 人。")

    def test_empty_sections_or_lines_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _card(sections=[])
        with self.assertRaises(ValueError):
            _card(sections=[("概况", ())])
        with self.assertRaises(ValueError):
            _card(sections=[("概况", "一整段字符串不是行序列")])


class EscapeMarkdownTest(unittest.TestCase):
    def test_escaped_link_and_mention_pass_the_card_guard(self) -> None:
        raw = "[点我](https://example.invalid) <at id=all></at> **加粗** & _斜体_"
        escaped = escape_markdown(raw)
        card = _card(sections=[(None, (escaped,))])
        content = card.to_payload()["body"]["elements"][0]["content"]
        self.assertNotIn("<at", content)
        self.assertNotIn("](", content)
        self.assertNotIn("://", content)
        self.assertNotIn("**加粗**", content)
        self.assertIn("&lt;at id=all&gt;", content)

    def test_plain_chinese_and_digits_are_unchanged(self) -> None:
        self.assertEqual(escape_markdown("公司：星空 2026 年 9 月"), "公司：星空 2026 年 9 月")

    def test_non_text_values_are_stringified(self) -> None:
        self.assertEqual(escape_markdown(42), "42")


class ContentCatalogHasCardTest(unittest.TestCase):
    def test_missing_notice_card_key_is_false(self) -> None:
        catalog = default_content_catalog()
        self.assertFalse(catalog.has_card("notice.gateway.busy_hint"))
        self.assertFalse(catalog.has_card(""))

    def test_registered_card_key_is_true(self) -> None:
        self.assertTrue(default_content_catalog().has_card("query.result"))


if __name__ == "__main__":
    unittest.main()
