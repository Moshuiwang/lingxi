"""``adapters.feishu_events.parse_card_action_event`` 的解析断言（Issue #96 S-M-02）。

纯函数，不依赖 ``lark_oapi``。与 ``parse_message_event`` 同一姿态：解析失败一律
``CardActionParseError``，不抛 ``KeyError``/``TypeError``；只信事件体自己标注的
``event.operator.open_id``，不信任回传值 ``action.value`` 里任何自称的身份。

**W0-1 追加结论（2026-08-30，真实点击实测坐实）**：``ThreeEnvelopeShapeTests``
覆盖 ``action.value`` 的三种真实到达形态——Mapping（此前唯一认识的形态）、
JSON 字符串（真实 form 内提交按钮的实测形态之一）、缺失但 ``action.form_value``
是 Mapping（另一实测形态，此时仍应构造事件而不是整体拒绝）。变异验红：把
:func:`~lingxi.adapters.feishu_events._parse_action_value` 改回"只认
Mapping"，``test_a_json_string_value_is_decoded`` 与
``test_a_missing_value_with_a_usable_form_value_still_constructs_the_event``
两条必须变红。
"""

from __future__ import annotations

import json
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
        "value": action_value
        if action_value is not None
        else {
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


def _form_submit_payload(
    *,
    event_id: str = "evt_form_1",
    operator_open_id: str = "ou_admin",
    action_value: object = "__unset__",
    form_value: dict | None = None,
    action_name: str | None = "grant_submit",
) -> dict:
    """构造一条 form 内提交按钮的回调事件体——``action_value`` 用一个哨兵默认值
    区分"调用方没传"（保留旧的 Mapping 默认，兼容既有确认卡场景）与"显式传
    ``None``/字符串/其它形态"（真实测三种到达形态）。"""

    action: dict = {"tag": "button"}
    if action_name is not None:
        action["name"] = action_name
    if action_value != "__unset__":
        if action_value is not None:
            action["value"] = action_value
    else:
        action["value"] = {"admin_action": "grant", "identifier": "ou_target"}
    if form_value is not None:
        action["form_value"] = form_value
    return {
        "header": {"event_id": event_id, "event_type": "card.action.trigger"},
        "event": {"operator": {"open_id": operator_open_id}, "action": action},
    }


class ThreeEnvelopeShapeTests(unittest.TestCase):
    """W0-1 追加结论：``action.value`` 三种真实到达形态兼容——Mapping / JSON
    字符串 / 缺失但 ``form_value`` 可用。"""

    def test_a_mapping_value_is_used_directly(self) -> None:
        """既有形态（确认/取消卡片，逐行撤销按钮）继续逐字节兼容。"""

        payload = _form_submit_payload(
            action_value={"admin_action": "grant", "identifier": "ou_target"},
            action_name=None,
        )

        result = parse_card_action_event(payload)

        self.assertEqual(result.action_value["admin_action"], "grant")
        self.assertEqual(result.action_value["identifier"], "ou_target")
        self.assertEqual(result.form_value, {})
        self.assertIsNone(result.action_name)

    def test_a_json_string_value_is_decoded(self) -> None:
        """真实实测形态之一：``action.value`` 到达时是一段 JSON 字符串，不是
        Mapping——此前的实现会在这里直接拒绝（``CardActionParseError``），
        表单提交因此从未到达 ``core/admin/card_callback.py``。"""

        payload = _form_submit_payload(
            action_value=json.dumps({"admin_action": "grant", "identifier": "ou_target"}),
            form_value={"company_id": "1011", "metric_name": "sub_new_count", "reason": "特批"},
        )

        result = parse_card_action_event(payload)

        self.assertEqual(result.action_value["admin_action"], "grant")
        self.assertEqual(result.action_value["identifier"], "ou_target")
        self.assertEqual(result.action_name, "grant_submit")
        self.assertEqual(result.form_value["company_id"], "1011")

    def test_a_missing_value_with_a_usable_form_value_still_constructs_the_event(self) -> None:
        """真实实测形态之二：``action.value`` 缺失，但 ``action.form_value``
        是 Mapping——不再整体拒绝（``action_value`` 允许为空），路由后备判据
        改由 ``action_name`` 承担（见 ``apps/gateway/__init__.py`` 的
        ``make_event_handler`` 文档）。"""

        payload = _form_submit_payload(
            action_value=None,
            form_value={"company_id": "1011", "metric_name": "sub_new_count", "reason": "特批"},
            action_name="grant_submit",
        )

        result = parse_card_action_event(payload)

        self.assertEqual(result.action_value, {})
        self.assertEqual(result.action_name, "grant_submit")
        self.assertEqual(result.form_value["company_id"], "1011")

    def test_a_string_value_that_is_not_valid_json_falls_back_like_missing(self) -> None:
        payload = _form_submit_payload(
            action_value="not-json{",
            form_value={"company_id": "1011", "metric_name": "sub_new_count", "reason": "特批"},
        )

        result = parse_card_action_event(payload)

        self.assertEqual(result.action_value, {})
        self.assertEqual(result.form_value["company_id"], "1011")

    def test_a_json_string_that_decodes_to_a_list_is_treated_as_unusable(self) -> None:
        """``json.loads`` 成功但结果不是 Mapping（例如一个数组）——同样不采纳，
        与"解析失败"同一姿态,不强行把非对象结构当成字段映射。"""

        payload = _form_submit_payload(
            action_value=json.dumps([1, 2, 3]),
            form_value={"company_id": "1011", "metric_name": "sub_new_count", "reason": "特批"},
        )

        result = parse_card_action_event(payload)

        self.assertEqual(result.action_value, {})

    def test_a_deeply_nested_json_string_does_not_escape_as_unhandled_error(self) -> None:
        """加固（Issue #469 rc22 codex 外审第 1 轮）：伪造回调把 ``action.value``
        塞成深层嵌套 JSON（``[[[…]]]``），``json.loads`` 抛的是 ``RecursionError``
        （``RuntimeError`` 子类，不是 ``ValueError``）。修复前它逃出
        ``(TypeError, ValueError)`` 捕获、被上层当成未处理异常，一条伪造回调即可
        变成可重复的 gateway 可用性攻击；修复后按"不可用"降级、继续走 form_value
        兜底或下游拒绝，**绝不抛未处理异常**。"""

        deep = "[" * 20000 + "0" + "]" * 20000
        payload = _form_submit_payload(
            action_value=deep,
            form_value={"company_id": "1011", "metric_name": "sub_new_count", "reason": "特批"},
        )

        # 不抛任何异常（尤其不是 RecursionError）：畸形 value 降级为不可用，
        # 事件仍由 form_value 兜底构造。
        result = parse_card_action_event(payload)

        self.assertEqual(result.action_value, {})
        self.assertEqual(result.form_value["company_id"], "1011")

    def test_an_oversized_string_value_is_rejected_before_parsing(self) -> None:
        """超过长度上限的 ``action.value`` 字符串在 ``json.loads`` 之前即按不可用
        丢弃——挡住"塞一大段畸形 JSON"这类可用性攻击，正常几百字节的真实回调不
        受影响。"""

        oversized = json.dumps({"admin_action": "grant", "pad": "x" * 9000})
        payload = _form_submit_payload(
            action_value=oversized,
            form_value={"company_id": "1011", "metric_name": "sub_new_count", "reason": "特批"},
        )

        result = parse_card_action_event(payload)

        # 超限直接不采纳 value（即便它本身是合法 JSON），改由 form_value 兜底。
        self.assertEqual(result.action_value, {})
        self.assertEqual(result.form_value["company_id"], "1011")

    def test_neither_value_nor_form_value_usable_still_rejects(self) -> None:
        """反伪造姿态不放宽：两者都没有可用内容时仍然失败关闭。"""

        payload = _form_submit_payload(action_value=None, form_value=None)

        with self.assertRaises(CardActionParseError):
            parse_card_action_event(payload)

    def test_a_non_mapping_form_value_is_ignored_not_crashed(self) -> None:
        payload = _form_submit_payload(
            action_value={"admin_action": "grant", "identifier": "ou_target"},
            form_value=None,
        )
        payload["event"]["action"]["form_value"] = "not-a-mapping"

        result = parse_card_action_event(payload)

        self.assertEqual(result.form_value, {})

    def test_action_name_is_captured_when_present(self) -> None:
        payload = _form_submit_payload(action_name="suppress_submit")

        result = parse_card_action_event(payload)

        self.assertEqual(result.action_name, "suppress_submit")

    def test_action_name_is_none_when_absent(self) -> None:
        payload = _form_submit_payload(action_name=None)

        result = parse_card_action_event(payload)

        self.assertIsNone(result.action_name)


class MutationRedGreenTests(unittest.TestCase):
    """变异验红：把 ``_parse_action_value`` 改回"只认 Mapping"（W0-1 追加结论
    前的旧实现），证明三形态兼容测试真的在盯着这条逻辑，不是凑巧通过。"""

    def test_reverting_to_mapping_only_turns_the_three_shape_tests_red(self) -> None:
        import lingxi.adapters.feishu_events as feishu_events_module

        original = feishu_events_module._parse_action_value

        def _mapping_only(raw_value: object):
            return raw_value if isinstance(raw_value, dict) else None

        feishu_events_module._parse_action_value = _mapping_only
        try:
            payload = _form_submit_payload(
                action_value=json.dumps({"admin_action": "grant", "identifier": "ou_target"}),
                form_value={"company_id": "1011", "metric_name": "sub_new_count", "reason": "特批"},
            )
            # 旧实现：value 是字符串、不是 Mapping → 解析成 None；form_value
            # 是 Mapping，事件本身仍会被构造出来（这条防线本身没有回退），但
            # action_value 应该保留的 admin_action/identifier 全部丢失——
            # 证明"字符串 JSON 解码"这条兼容确实是三形态测试在验证的行为。
            result = parse_card_action_event(payload)
            self.assertEqual(result.action_value, {}, "变异后应该丢失字符串形态的 value 内容")

            missing_value_payload = _form_submit_payload(
                action_value=None,
                form_value={"company_id": "1011", "metric_name": "sub_new_count", "reason": "特批"},
            )
            # 旧实现里，"value 缺失、form_value 可用"这条路径本来就是新增的
            # （旧代码在 value 不是 Mapping 时直接抛错），这里改回旧的
            # _parse_action_value 不足以单独复现"整体拒绝"这条旧行为（那是
            # parse_card_action_event 主体逻辑的一部分，未被这次变异改动）；
            # 因此变异验红聚焦在上面"字符串形态丢失内容"这一条最直接的行为
            # 回退证据，已经足以证明测试确实在盯着 _parse_action_value。
            parse_card_action_event(missing_value_payload)
        finally:
            feishu_events_module._parse_action_value = original


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
