"""``adapters.feishu_delivery`` 卡片载荷形状的纯函数断言（Issue #175）。

只测 ``_card_payload``/``_card_markdown`` 两个不依赖 ``lark_oapi`` 的纯函数——
``LarkCardTransport`` 本身在方法内部延迟导入 SDK，真实发送形态留给 Bot-Test/Stage
的 L4a（模块头注释已标注：无 header 卡片的真实接受度是本 Issue 新增的未验证项）。

认领：2026-08-16 产品负责人定稿第 3 条——卡片去掉 ``header``，阶段标题只在正文
（markdown 元素）承载一份；``element_id`` 与整卡结构的其余部分不变。
"""

from __future__ import annotations

import unittest

from lingxi.adapters.feishu_delivery import _card_markdown, _card_payload
from lingxi.config.content import default_content_catalog


def _status_card(status: str = "正在处理 · 3 秒"):
    return default_content_catalog().card("query.status", status=status)


def _result_card(result: str = "本月销售额 100 万元"):
    return default_content_catalog().card("query.result", result=result)


class CardPayloadHasNoHeaderTests(unittest.TestCase):
    def test_payload_top_level_has_no_header_key(self) -> None:
        card = _status_card()
        payload = _card_payload(card)

        self.assertNotIn("header", payload, "Issue #175 定稿：卡片不得单独携带 header 字段")
        self.assertEqual(set(payload), {"schema", "config", "body"})

    def test_payload_shape_is_otherwise_unchanged(self) -> None:
        """去掉 header 不改变其余既有约束：schema、streaming 开关、element_id。"""

        card = _status_card()
        payload = _card_payload(card)

        self.assertEqual(payload["schema"], "2.0")
        self.assertEqual(payload["config"], {"update_multi": True, "streaming_mode": True})
        elements = payload["body"]["elements"]
        self.assertEqual(len(elements), 1)
        self.assertEqual(elements[0]["tag"], "markdown")
        self.assertEqual(elements[0]["element_id"], "lingxi_status")

    def test_markdown_content_carries_the_new_stage_title(self) -> None:
        """标题唯一落点是正文 markdown；去掉的那份 header 标题不能悄悄消失。"""

        status_markdown = _card_markdown(_status_card())
        self.assertIn("正在查询", status_markdown)

        result_markdown = _card_markdown(_result_card())
        self.assertIn("查询完成", result_markdown)

    def test_payload_markdown_content_matches_card_markdown(self) -> None:
        card = _result_card()
        payload = _card_payload(card)

        self.assertEqual(payload["body"]["elements"][0]["content"], _card_markdown(card))


if __name__ == "__main__":
    unittest.main()
