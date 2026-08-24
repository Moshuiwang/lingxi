"""``adapters.feishu_events.parse_card_action_event`` 的解析断言（Issue #96 S-M-02）。

纯函数，不依赖 ``lark_oapi``。与 ``parse_message_event`` 同一姿态：解析失败一律
``CardActionParseError``，不抛 ``KeyError``/``TypeError``；只信事件体自己标注的
``event.operator.open_id``，不信任回传值 ``action.value`` 里任何自称的身份。
"""

from __future__ import annotations

import unittest

from lingxi.adapters.feishu_events import CardActionParseError, parse_card_action_event


def _payload(
    *,
    event_id: str = "evt_card_1",
    operator_open_id: str | None = "ou_admin",
    action_value: dict | None = None,
) -> dict:
    event: dict = {}
    if operator_open_id is not None:
        event["operator"] = {"open_id": operator_open_id}
    event["action"] = {
        "tag": "button",
        "value": action_value if action_value is not None else {
            "pending_action_id": "pac_1",
            "decision": "confirm",
        },
    }
    return {"header": {"event_id": event_id, "event_type": "card.action.trigger"}, "event": event}


class ParseCardActionEventTests(unittest.TestCase):
    def test_parses_operator_and_action_value(self) -> None:
        result = parse_card_action_event(_payload())

        self.assertEqual(result.event_id, "evt_card_1")
        self.assertEqual(result.operator_open_id, "ou_admin")
        self.assertEqual(result.action_value["pending_action_id"], "pac_1")
        self.assertEqual(result.action_value["decision"], "confirm")

    def test_trace_id_defaults_to_a_generated_value_when_not_supplied(self) -> None:
        result = parse_card_action_event(_payload())
        self.assertTrue(result.trace_id)

    def test_explicit_trace_id_is_preserved(self) -> None:
        result = parse_card_action_event(_payload(), trace_id="trc_fixed")
        self.assertEqual(result.trace_id, "trc_fixed")

    def test_rejects_non_mapping_payload(self) -> None:
        with self.assertRaises(CardActionParseError):
            parse_card_action_event("not-a-dict")  # type: ignore[arg-type]

    def test_rejects_missing_event_id(self) -> None:
        payload = _payload()
        del payload["header"]["event_id"]
        with self.assertRaises(CardActionParseError):
            parse_card_action_event(payload)

    def test_rejects_missing_event_section(self) -> None:
        payload = _payload()
        del payload["event"]
        with self.assertRaises(CardActionParseError):
            parse_card_action_event(payload)

    def test_rejects_missing_operator(self) -> None:
        payload = _payload(operator_open_id=None)
        with self.assertRaises(CardActionParseError):
            parse_card_action_event(payload)

    def test_rejects_missing_action_section(self) -> None:
        payload = _payload()
        del payload["event"]["action"]
        with self.assertRaises(CardActionParseError):
            parse_card_action_event(payload)

    def test_rejects_missing_action_value(self) -> None:
        payload = _payload()
        del payload["event"]["action"]["value"]
        with self.assertRaises(CardActionParseError):
            parse_card_action_event(payload)

    def test_does_not_trust_any_identity_claim_inside_action_value(self) -> None:
        """伪造回调试图在 ``action.value`` 里塞入一个自称的身份字段——本函数结构上
        不会把它当作操作者身份：``operator_open_id`` 只来自 ``event.operator``。"""

        payload = _payload(
            operator_open_id="ou_real_admin",
            action_value={
                "pending_action_id": "pac_1",
                "decision": "confirm",
                "open_id": "ou_forged_identity",
            },
        )

        result = parse_card_action_event(payload)

        self.assertEqual(result.operator_open_id, "ou_real_admin")
        # action_value 原样透传（不做业务判断），但绝不会被读成 operator 身份。
        self.assertEqual(result.action_value.get("open_id"), "ou_forged_identity")

    def test_non_scalar_values_inside_action_value_are_dropped(self) -> None:
        payload = _payload(
            action_value={
                "pending_action_id": "pac_1",
                "decision": "confirm",
                "nested": {"should": "be-dropped"},
            }
        )

        result = parse_card_action_event(payload)

        self.assertNotIn("nested", result.action_value)
        self.assertEqual(result.action_value["pending_action_id"], "pac_1")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
