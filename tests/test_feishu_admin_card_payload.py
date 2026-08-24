"""``adapters.feishu_admin_card`` 卡片载荷形状的纯函数断言（Issue #96 S-M-02）。

只测 ``_card_payload``——不依赖 ``lark_oapi``（``LarkAdminCardTransport`` 本身在方法
内部延迟导入 SDK，真实字段与真实发送形态留给 `biai-stage` 的 L4a）。与
``tests/test_feishu_delivery_card_payload.py`` 同一姿态。
"""

from __future__ import annotations

import unittest

from lingxi.adapters.feishu_admin_card import _card_payload
from lingxi.core.admin.notification import ConfirmCardButton, RenderedConfirmCard


def _card_with_buttons() -> RenderedConfirmCard:
    return RenderedConfirmCard(
        title="待确认：停用用户",
        body="动作：停用用户\n目标：ou_target\n有效期：10 分钟内有效。",
        buttons=(
            ConfirmCardButton(
                label="确认执行", value={"pending_action_id": "pac_1", "decision": "confirm"}
            ),
            ConfirmCardButton(
                label="取消", value={"pending_action_id": "pac_1", "decision": "cancel"}
            ),
        ),
    )


def _terminal_card() -> RenderedConfirmCard:
    return RenderedConfirmCard(title="停用用户 · 已结束", body="结果：已确认执行", buttons=())


class CardPayloadShapeTests(unittest.TestCase):
    def test_top_level_shape(self) -> None:
        payload = _card_payload(_card_with_buttons())
        self.assertEqual(set(payload), {"schema", "config", "body"})
        self.assertEqual(payload["schema"], "2.0")

    def test_markdown_element_carries_title_and_body(self) -> None:
        card = _card_with_buttons()
        payload = _card_payload(card)
        elements = payload["body"]["elements"]
        markdown_elements = [element for element in elements if element["tag"] == "markdown"]
        self.assertEqual(len(markdown_elements), 1)
        self.assertIn(card.title, markdown_elements[0]["content"])
        self.assertIn("ou_target", markdown_elements[0]["content"])

    def test_action_element_carries_two_buttons_with_bound_values(self) -> None:
        payload = _card_payload(_card_with_buttons())
        elements = payload["body"]["elements"]
        action_elements = [element for element in elements if element["tag"] == "action"]
        self.assertEqual(len(action_elements), 1)
        actions = action_elements[0]["actions"]
        self.assertEqual(len(actions), 2)
        decisions = {action["value"]["decision"] for action in actions}
        self.assertEqual(decisions, {"confirm", "cancel"})
        for action in actions:
            self.assertEqual(action["value"]["pending_action_id"], "pac_1")
            self.assertIn("text", action)
            self.assertEqual(action["tag"], "button")

    def test_confirm_button_is_primary_type_and_cancel_is_default(self) -> None:
        payload = _card_payload(_card_with_buttons())
        actions = payload["body"]["elements"][1]["actions"]
        by_decision = {action["value"]["decision"]: action for action in actions}
        self.assertEqual(by_decision["confirm"]["type"], "primary")
        self.assertEqual(by_decision["cancel"]["type"], "default")

    def test_terminal_card_has_no_action_element(self) -> None:
        """终态卡片结构上不存在任何可点击按钮，不是靠禁用态表达"不能再点了"。"""

        payload = _card_payload(_terminal_card())
        tags = [element["tag"] for element in payload["body"]["elements"]]
        self.assertNotIn("action", tags)
        self.assertEqual(tags, ["markdown"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
