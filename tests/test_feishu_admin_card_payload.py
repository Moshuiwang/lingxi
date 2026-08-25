"""``core.admin.notification.render_card_payload`` 卡片载荷形状的纯函数断言
（Issue #96 S-M-02）。

只测 ``render_card_payload``——不依赖 ``lark_oapi``（``LarkAdminCardTransport`` 本身
在方法内部延迟导入 SDK，真实字段与真实发送形态留给 `biai-stage` 的 L4a）。与
``tests/test_feishu_delivery_card_payload.py`` 同一姿态。

**2026-08-25 按钮形状改动（批次三 #96 修复）**：本文件此前断言按钮包在
``{"tag": "action", "actions": [...]}`` 容器里——那个形状已被真实 CardKit 拒绝
（``code=200861``，见 ``adapters/feishu_admin_card.py`` 模块文档），本文件的断言
已同步改为"按钮是 ``body.elements`` 顶层元素、零 ``action`` tag、``behaviors``
回调式回传值"。变异验证（把按钮改回 action 容器包裹，确认形状断言变红，再改回
顶层元素恢复绿）见 PR 描述。

**同日晚些时候：函数从 ``adapters.feishu_admin_card._card_payload`` 迁移到
``core.admin.notification.render_card_payload``**（Issue #96 卡片回调应答修复）：
``card.action.trigger`` 回调应答需要复用同一份终态卡 JSON 构造（见
``core/admin/card_callback.py`` 模块文档「载体 #96」），而应答的构造方按代码框架
第二节不得 import ``adapters/``。本文件的断言随函数一起搬家，内容未变。
"""

from __future__ import annotations

import unittest

from lingxi.core.admin.notification import (
    ConfirmCardButton,
    RenderedConfirmCard,
    render_card_payload,
)


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
        payload = render_card_payload(_card_with_buttons())
        self.assertEqual(set(payload), {"schema", "config", "body"})
        self.assertEqual(payload["schema"], "2.0")

    def test_markdown_element_carries_title_and_body(self) -> None:
        card = _card_with_buttons()
        payload = render_card_payload(card)
        elements = payload["body"]["elements"]
        markdown_elements = [element for element in elements if element["tag"] == "markdown"]
        self.assertEqual(len(markdown_elements), 1)
        self.assertIn(card.title, markdown_elements[0]["content"])
        self.assertIn("ou_target", markdown_elements[0]["content"])

    def test_no_action_container_element(self) -> None:
        """核心回归断言：真实 CardKit 已经拒绝 action 容器形状（``code=200861``），
        elements 里不能再出现任何 ``tag == "action"`` 的元素——这是本次修复要网住
        的具体回归。"""

        payload = render_card_payload(_card_with_buttons())
        tags = [element["tag"] for element in payload["body"]["elements"]]
        self.assertNotIn("action", tags)

    def test_buttons_are_top_level_elements_with_bound_callback_values(self) -> None:
        payload = render_card_payload(_card_with_buttons())
        elements = payload["body"]["elements"]
        button_elements = [element for element in elements if element["tag"] == "button"]
        # button_elements 直接来自 elements 的顶层过滤（不是从某个容器元素的
        # 子字段里取出来的）——这本身就断言了按钮是顶层元素，不嵌在任何容器里。
        self.assertEqual(len(button_elements), 2)
        for button in button_elements:
            self.assertIn("text", button)
            behaviors = button["behaviors"]
            self.assertEqual(len(behaviors), 1)
            self.assertEqual(behaviors[0]["type"], "callback")
            self.assertEqual(behaviors[0]["value"]["pending_action_id"], "pac_1")
        decisions = {button["behaviors"][0]["value"]["decision"] for button in button_elements}
        self.assertEqual(decisions, {"confirm", "cancel"})

    def test_confirm_button_is_primary_type_and_cancel_is_default(self) -> None:
        payload = render_card_payload(_card_with_buttons())
        button_elements = [
            element for element in payload["body"]["elements"] if element["tag"] == "button"
        ]
        by_decision = {
            button["behaviors"][0]["value"]["decision"]: button for button in button_elements
        }
        self.assertEqual(by_decision["confirm"]["type"], "primary")
        self.assertEqual(by_decision["cancel"]["type"], "default")

    def test_terminal_card_has_no_button_elements(self) -> None:
        """终态卡片结构上不存在任何可点击按钮，不是靠禁用态表达"不能再点了"。"""

        payload = render_card_payload(_terminal_card())
        tags = [element["tag"] for element in payload["body"]["elements"]]
        self.assertNotIn("action", tags)
        self.assertNotIn("button", tags)
        self.assertEqual(tags, ["markdown"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
