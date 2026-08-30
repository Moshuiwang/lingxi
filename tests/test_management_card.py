"""``core/admin/management_card.render_management_card`` 的纯函数渲染断言
（#439 B 档：用户权限管理卡）。

认领断言：卡片 JSON 结构——分区（银河来源/本地覆盖/新增授权与新增抑制表单）、
下拉（公司/指标 select_static）、按钮（逐行收回 + 表单提交）经卡片 JSON 回读
断言，满足 #439 自证闭环条款"管理卡真实送达与卡体结构（分区/下拉/按钮经卡片
JSON 回读断言）"的结构面部分。
"""

from __future__ import annotations

import unittest

from lingxi.core.admin.management_card import (
    ADMIN_ACTION_GRANT,
    ADMIN_ACTION_REVOKE,
    ADMIN_ACTION_SUPPRESS,
    render_management_card,
)
from lingxi.core.admin.views import (
    AdminUserStatusView,
    GalaxySourceSummary,
    LocalPermissionOverrideView,
)


class FakeCatalog:
    def __init__(self, *, companies: list[str] | None = None, metrics: list[str] | None = None) -> None:
        self._companies = companies if companies is not None else ["1011", "1012"]
        self._metrics = metrics if metrics is not None else ["sub_new_count", "exchange_rate"]

    def companies(self) -> list[str]:
        return self._companies

    def metrics(self) -> list[str]:
        return self._metrics


class RaisingCatalog:
    """目录读取失败的假实现——用于验证渲染不因目录不可用而崩溃。"""

    def companies(self):
        raise RuntimeError("模拟目录读取失败")

    def metrics(self):
        raise RuntimeError("模拟目录读取失败")


def _status(
    *,
    local_overrides: tuple[LocalPermissionOverrideView, ...] = (),
    galaxy_source: GalaxySourceSummary | None = None,
) -> AdminUserStatusView:
    return AdminUserStatusView(
        identifier="ou_target",
        provisioning_state="active",
        account_state="enabled",
        permission_version=1,
        updated_at="2026-08-30T00:00:00+00:00",
        local_overrides=local_overrides,
        galaxy_source=galaxy_source,
    )


def _elements(card: dict) -> list[dict]:
    return card["body"]["elements"]


def _find_forms(card: dict) -> list[dict]:
    return [element for element in _elements(card) if element.get("tag") == "form"]


def _find_buttons(elements: list[dict]) -> list[dict]:
    return [element for element in elements if element.get("tag") == "button"]


def _find_selects(elements: list[dict]) -> list[dict]:
    return [element for element in elements if element.get("tag") == "select_static"]


class TopLevelShapeTests(unittest.TestCase):
    def test_schema_and_body_shape_matches_the_confirm_card_convention(self) -> None:
        card = render_management_card(
            _status(), display_identifier="ou_target", catalog=FakeCatalog()
        )
        self.assertEqual(card["schema"], "2.0")
        self.assertIn("elements", card["body"])
        self.assertIsInstance(card["body"]["elements"], list)

    def test_all_top_level_elements_are_dicts_not_bare_strings(self) -> None:
        """回归锚点：确认卡渲染层曾经出过"把字符串直接塞进 elements 列表"的
        实现缺陷（本模块开发过程中发现并修复），这里钉住不再发生。"""

        card = render_management_card(
            _status(), display_identifier="ou_target", catalog=FakeCatalog()
        )
        for element in _elements(card):
            self.assertIsInstance(element, dict)

    def test_display_identifier_is_echoed_verbatim(self) -> None:
        card = render_management_card(
            _status(), display_identifier="someone@example.com", catalog=FakeCatalog()
        )
        markdown_texts = "\n".join(
            e["content"] for e in _elements(card) if e.get("tag") == "markdown"
        )
        self.assertIn("someone@example.com", markdown_texts)


class GalaxySourceSectionTests(unittest.TestCase):
    def test_none_galaxy_source_renders_unavailable(self) -> None:
        card = render_management_card(
            _status(galaxy_source=None), display_identifier="ou_target", catalog=FakeCatalog()
        )
        markdown_texts = "\n".join(
            e["content"] for e in _elements(card) if e.get("tag") == "markdown"
        )
        self.assertIn("银河来源", markdown_texts)
        self.assertIn("不可用", markdown_texts)

    def test_granted_galaxy_source_shows_companies_and_functions(self) -> None:
        summary = GalaxySourceSummary(
            granted=True, reason="granted", companies=("1011",), functions=("运营",)
        )
        card = render_management_card(
            _status(galaxy_source=summary), display_identifier="ou_target", catalog=FakeCatalog()
        )
        markdown_texts = "\n".join(
            e["content"] for e in _elements(card) if e.get("tag") == "markdown"
        )
        self.assertIn("1011", markdown_texts)
        self.assertIn("运营", markdown_texts)

    def test_all_companies_wildcard_renders_as_全部公司(self) -> None:
        summary = GalaxySourceSummary(
            granted=True, reason="granted", functions=("后台管理员",), all_companies=True
        )
        card = render_management_card(
            _status(galaxy_source=summary), display_identifier="ou_target", catalog=FakeCatalog()
        )
        markdown_texts = "\n".join(
            e["content"] for e in _elements(card) if e.get("tag") == "markdown"
        )
        self.assertIn("全部公司", markdown_texts)


class LocalOverrideSectionTests(unittest.TestCase):
    def test_no_overrides_shows_the_empty_line(self) -> None:
        card = render_management_card(
            _status(local_overrides=()), display_identifier="ou_target", catalog=FakeCatalog()
        )
        markdown_texts = "\n".join(
            e["content"] for e in _elements(card) if e.get("tag") == "markdown"
        )
        self.assertIn("无本地覆盖", markdown_texts)

    def test_each_override_row_gets_a_description_and_a_dedicated_revoke_button(self) -> None:
        override = LocalPermissionOverrideView(
            override_id="lpo_01JGFJJZ008XSHEADGG8V74SPC",
            direction="grant",
            company_id="1011",
            metric_name="sub_new_count",
            reason="特批",
            created_at="2026-08-30T00:00:00+00:00",
        )
        card = render_management_card(
            _status(local_overrides=(override,)),
            display_identifier="ou_target",
            catalog=FakeCatalog(),
        )
        elements = _elements(card)
        buttons = _find_buttons(elements)
        revoke_buttons = [
            b for b in buttons if b["behaviors"][0]["value"].get("admin_action") == ADMIN_ACTION_REVOKE
        ]
        self.assertEqual(len(revoke_buttons), 1)
        value = revoke_buttons[0]["behaviors"][0]["value"]
        self.assertEqual(value["override_id"], "lpo_01JGFJJZ008XSHEADGG8V74SPC")
        self.assertEqual(value["identifier"], "ou_target")
        # 描述行含公司/指标信息。
        markdown_texts = "\n".join(e["content"] for e in elements if e.get("tag") == "markdown")
        self.assertIn("1011", markdown_texts)
        self.assertIn("sub_new_count", markdown_texts)

    def test_multiple_overrides_each_get_their_own_revoke_button(self) -> None:
        overrides = tuple(
            LocalPermissionOverrideView(
                override_id=f"lpo_row{i}00000000000000000000",
                direction="grant",
                company_id="1011",
                metric_name=f"metric_{i}",
                reason="特批",
                created_at="2026-08-30T00:00:00+00:00",
            )
            for i in range(3)
        )
        card = render_management_card(
            _status(local_overrides=overrides),
            display_identifier="ou_target",
            catalog=FakeCatalog(),
        )
        revoke_buttons = [
            b
            for b in _find_buttons(_elements(card))
            if b["behaviors"][0]["value"].get("admin_action") == ADMIN_ACTION_REVOKE
        ]
        self.assertEqual(len(revoke_buttons), 3)
        override_ids = {b["behaviors"][0]["value"]["override_id"] for b in revoke_buttons}
        self.assertEqual(override_ids, {o.override_id for o in overrides})

    def test_long_reason_is_truncated_same_as_the_text_reply(self) -> None:
        long_reason = "这是一段超过二十个字符的很长很长的收回或授权原因说明文本"
        override = LocalPermissionOverrideView(
            override_id="lpo_01JGFJJZ008XSHEADGG8V74SPC",
            direction="suppress",
            company_id="1011",
            metric_name="sub_new_count",
            reason=long_reason,
            created_at="2026-08-30T00:00:00+00:00",
        )
        card = render_management_card(
            _status(local_overrides=(override,)),
            display_identifier="ou_target",
            catalog=FakeCatalog(),
        )
        markdown_texts = "\n".join(
            e["content"] for e in _elements(card) if e.get("tag") == "markdown"
        )
        self.assertNotIn(long_reason, markdown_texts)
        self.assertIn(long_reason[:20], markdown_texts)


class GrantSuppressFormSectionTests(unittest.TestCase):
    def test_form_contains_exactly_two_selects_one_input_and_two_submit_buttons(self) -> None:
        card = render_management_card(
            _status(), display_identifier="ou_target", catalog=FakeCatalog()
        )
        forms = _find_forms(card)
        self.assertEqual(len(forms), 1)
        form_elements = forms[0]["elements"]
        selects = _find_selects(form_elements)
        self.assertEqual(len(selects), 2)
        self.assertEqual({s["name"] for s in selects}, {"company_id", "metric_name"})
        inputs = [e for e in form_elements if e.get("tag") == "input"]
        self.assertEqual(len(inputs), 1)
        self.assertEqual(inputs[0]["name"], "reason")
        buttons = _find_buttons(form_elements)
        self.assertEqual(len(buttons), 2)
        actions = {b["behaviors"][0]["value"]["admin_action"] for b in buttons}
        self.assertEqual(actions, {ADMIN_ACTION_GRANT, ADMIN_ACTION_SUPPRESS})
        for button in buttons:
            self.assertEqual(button["form_action_type"], "submit")
            self.assertEqual(button["behaviors"][0]["value"]["identifier"], "ou_target")

    def test_company_and_metric_options_come_from_the_injected_catalog(self) -> None:
        catalog = FakeCatalog(companies=["1011", "1012", "1013"], metrics=["sub_new_count"])
        card = render_management_card(
            _status(), display_identifier="ou_target", catalog=catalog
        )
        selects = _find_selects(_find_forms(card)[0]["elements"])
        company_select = next(s for s in selects if s["name"] == "company_id")
        metric_select = next(s for s in selects if s["name"] == "metric_name")
        self.assertEqual(
            [o["value"] for o in company_select["options"]], ["1011", "1012", "1013"]
        )
        self.assertEqual([o["value"] for o in metric_select["options"]], ["sub_new_count"])

    def test_empty_catalog_degrades_to_a_placeholder_option_not_a_missing_dropdown(self) -> None:
        """目录不可用（读取失败/为空）时下拉退化为一个不可选占位项，而不是
        省略整个表单——见 ``render_management_card`` 模块文档「组件选择」。"""

        card = render_management_card(
            _status(), display_identifier="ou_target", catalog=FakeCatalog(companies=[], metrics=[])
        )
        selects = _find_selects(_find_forms(card)[0]["elements"])
        for select in selects:
            self.assertEqual(len(select["options"]), 1)
            self.assertEqual(select["options"][0]["value"], "")

    def test_catalog_raising_an_exception_does_not_crash_rendering(self) -> None:
        """否定断言：目录端口本身抛异常不得让整张卡渲染失败——目录不可用是
        展示层的可选降级分支，不是渲染函数的前置条件。"""

        card = render_management_card(
            _status(), display_identifier="ou_target", catalog=RaisingCatalog()
        )
        selects = _find_selects(_find_forms(card)[0]["elements"])
        self.assertEqual(len(selects), 2)
        for select in selects:
            self.assertEqual(select["options"][0]["value"], "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
