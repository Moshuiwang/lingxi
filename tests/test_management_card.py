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


class FakeDisplayNames:
    """``AdminDisplayNames`` 的内存假实现（Trace #469 S-1）。默认原样透传
    公司/指标编号（不是需要隐藏的内部标识），可选注入映射覆盖单个值。

    批量方法（Trace #469 修复包 B，B-7）就地委托给单个查询——内存假实现没有
    真实实现要收敛的"每次调用新建连接"这个成本，批量与逐个调用在这里语义
    完全等价；真正验证"确实只调用了一次批量端口、不再逐项调用"的是
    ``ConnectionCountingDisplayNames``（见下方）。"""

    def __init__(
        self,
        *,
        company_labels: dict[str, str] | None = None,
        metric_labels: dict[str, str] | None = None,
    ) -> None:
        self._company_labels = company_labels or {}
        self._metric_labels = metric_labels or {}

    def user_label(self, *, open_id: str) -> str:
        return "该用户"

    def company_label(self, *, company_id: str) -> str:
        return self._company_labels.get(company_id, company_id)

    def metric_label(self, *, metric_id: str) -> str:
        return self._metric_labels.get(metric_id, metric_id)

    def company_labels(self, *, company_ids):
        return {company_id: self.company_label(company_id=company_id) for company_id in company_ids}

    def metric_labels(self, *, metric_ids):
        return {metric_id: self.metric_label(metric_id=metric_id) for metric_id in metric_ids}


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
    """递归查找按钮——Trace #469 S-1 起，横排的按钮组不再是顶层元素，而是
    嵌套在 ``column_set`` → ``column`` → ``elements`` 里（见
    ``core/admin/card_layout.button_row``）。"""

    found: list[dict] = []
    for element in elements:
        tag = element.get("tag")
        if tag == "button":
            found.append(element)
        elif tag == "column_set":
            for column in element.get("columns", ()):
                found.extend(_find_buttons(column.get("elements", ())))
    return found


def _find_selects(elements: list[dict]) -> list[dict]:
    return [element for element in elements if element.get("tag") == "select_static"]


def _visible_text(card: dict) -> str:
    """收集整张卡片**管理员真正会看到**的文本——``markdown``/``plain_text``
    的 ``content`` 字段（含按钮文字、下拉选项文字），递归穿过
    ``form``/``column_set``/``column``。**不包含**按钮/组件 ``value`` 或
    ``name`` 这类隐藏在回调载荷里、服务端识别用的字段——那些字段允许携带
    ``lpo_``/``ou_`` 内部 ID（这是它们存在的意义），管理员可见文案零内部 ID
    这条断言只约束"人眼会读到的文字"。
    """

    chunks: list[str] = []

    def walk(elements: list[dict]) -> None:
        for element in elements:
            tag = element.get("tag")
            if tag in ("markdown", "plain_text"):
                content = element.get("content")
                if isinstance(content, str):
                    chunks.append(content)
            elif tag == "form":
                walk(element.get("elements", []))
            elif tag == "column_set":
                for column in element.get("columns", ()):
                    walk(column.get("elements", []))
            elif tag == "button":
                walk([element.get("text", {})])
            elif tag == "select_static":
                walk([element.get("placeholder", {})])
                for option in element.get("options", ()):
                    walk([option.get("text", {})])
            elif tag == "input":
                walk([element.get("placeholder", {})])

    walk(_elements(card))
    return "\n".join(chunks)


class TopLevelShapeTests(unittest.TestCase):
    def test_schema_and_body_shape_matches_the_confirm_card_convention(self) -> None:
        card = render_management_card(
            _status(), display_identifier="ou_target", catalog=FakeCatalog(), display_names=FakeDisplayNames()
        )
        self.assertEqual(card["schema"], "2.0")
        self.assertIn("elements", card["body"])
        self.assertIsInstance(card["body"]["elements"], list)

    def test_all_top_level_elements_are_dicts_not_bare_strings(self) -> None:
        """回归锚点：确认卡渲染层曾经出过"把字符串直接塞进 elements 列表"的
        实现缺陷（本模块开发过程中发现并修复），这里钉住不再发生。"""

        card = render_management_card(
            _status(), display_identifier="ou_target", catalog=FakeCatalog(), display_names=FakeDisplayNames()
        )
        for element in _elements(card):
            self.assertIsInstance(element, dict)

    def test_display_identifier_is_echoed_verbatim(self) -> None:
        card = render_management_card(
            _status(), display_identifier="someone@example.com", catalog=FakeCatalog(), display_names=FakeDisplayNames()
        )
        markdown_texts = "\n".join(
            e["content"] for e in _elements(card) if e.get("tag") == "markdown"
        )
        self.assertIn("someone@example.com", markdown_texts)


class GalaxySourceSectionTests(unittest.TestCase):
    def test_none_galaxy_source_renders_unavailable(self) -> None:
        card = render_management_card(
            _status(galaxy_source=None), display_identifier="ou_target", catalog=FakeCatalog(), display_names=FakeDisplayNames()
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
            _status(galaxy_source=summary), display_identifier="ou_target", catalog=FakeCatalog(), display_names=FakeDisplayNames()
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
            _status(galaxy_source=summary), display_identifier="ou_target", catalog=FakeCatalog(), display_names=FakeDisplayNames()
        )
        markdown_texts = "\n".join(
            e["content"] for e in _elements(card) if e.get("tag") == "markdown"
        )
        self.assertIn("全部公司", markdown_texts)


class LocalOverrideSectionTests(unittest.TestCase):
    def test_no_overrides_shows_the_empty_line(self) -> None:
        card = render_management_card(
            _status(local_overrides=()), display_identifier="ou_target", catalog=FakeCatalog(), display_names=FakeDisplayNames()
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
            display_names=FakeDisplayNames(),
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
            display_names=FakeDisplayNames(),
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
            display_names=FakeDisplayNames(),
        )
        markdown_texts = "\n".join(
            e["content"] for e in _elements(card) if e.get("tag") == "markdown"
        )
        self.assertNotIn(long_reason, markdown_texts)
        self.assertIn(long_reason[:20], markdown_texts)


class LegacyAllScopeGroupSectionTests(unittest.TestCase):
    """「2.0 迁移导入·全部」组（rc25 S-1，Issue #540）：``company_id="*"`` 的一组行在管理卡
    上渲染为**一项**、按钮带组 ID 整组撤销；``"*"`` 不当成真实公司键去查中文名。"""

    def _group(self, size: int = 3) -> tuple[LocalPermissionOverrideView, ...]:
        return tuple(
            LocalPermissionOverrideView(
                override_id=f"lpo_all{i}0000000000000000000000",
                direction="grant",
                company_id="*",
                metric_name=f"metric_{i}",
                reason="2.0 迁移导入",
                created_at="2026-09-02T08:00:00+00:00",
                position_name="2.0 迁移导入·全部",
                company_scope="*",
                group_id="lpg_01LEGACYALL000000000000000",
            )
            for i in range(size)
        )

    def test_the_group_renders_as_one_item_with_a_group_revoke_button(self) -> None:
        display_names = FakeDisplayNames()
        card = render_management_card(
            _status(local_overrides=self._group()),
            display_identifier="ou_target",
            catalog=FakeCatalog(),
            display_names=display_names,
        )
        elements = _elements(card)
        revoke_buttons = [
            b for b in _find_buttons(elements) if b["behaviors"][0]["value"].get("admin_action") == ADMIN_ACTION_REVOKE
        ]
        self.assertEqual(len(revoke_buttons), 1, "一组只渲染一项")
        value = revoke_buttons[0]["behaviors"][0]["value"]
        self.assertEqual(value["permission_group_id"], "lpg_01LEGACYALL000000000000000")
        self.assertNotIn("override_id", value)
        markdown_texts = "\n".join(e["content"] for e in elements if e.get("tag") == "markdown")
        self.assertIn("2.0 迁移导入·全部", markdown_texts)
        self.assertIn("覆盖 3 项权限", markdown_texts)
        self.assertIn("全部", markdown_texts)


class GrantSuppressFormSectionTests(unittest.TestCase):
    def test_form_contains_exactly_two_selects_one_input_and_two_submit_buttons(self) -> None:
        card = render_management_card(
            _status(), display_identifier="ou_target", catalog=FakeCatalog(), display_names=FakeDisplayNames()
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

    def test_submit_buttons_are_labelled_with_the_new_terminology(self) -> None:
        """术语统一（Trace #469 S-1 PM 补充裁定第 4 条）：「新增授权」→「补充
        授权」、「新增抑制」→「屏蔽指标」。"""

        card = render_management_card(
            _status(), display_identifier="ou_target", catalog=FakeCatalog(), display_names=FakeDisplayNames()
        )
        form_elements = _find_forms(card)[0]["elements"]
        buttons = _find_buttons(form_elements)
        labels = {b["text"]["content"] for b in buttons}
        self.assertEqual(labels, {"补充授权", "屏蔽指标"})
        self.assertNotIn("新增授权", labels)
        self.assertNotIn("新增抑制", labels)

    def test_submit_buttons_have_non_empty_unique_names_and_are_horizontally_laid_out(
        self,
    ) -> None:
        """200530 修复 + 按钮横排（PM 补充裁定第 5/6 条，W0-1 探针裁定）：两个
        提交按钮携带非空且互不相同的 ``name``，并横排进一个显式声明
        ``flex_mode`` 的 ``column_set``（2 个按钮用 ``bisect``）。"""

        card = render_management_card(
            _status(), display_identifier="ou_target", catalog=FakeCatalog(), display_names=FakeDisplayNames()
        )
        form_elements = _find_forms(card)[0]["elements"]
        column_sets = [e for e in form_elements if e.get("tag") == "column_set"]
        self.assertEqual(len(column_sets), 1)
        column_set = column_sets[0]
        self.assertEqual(column_set["flex_mode"], "bisect")
        self.assertEqual(len(column_set["columns"]), 2)
        names: list[str] = []
        for column in column_set["columns"]:
            self.assertEqual(column["tag"], "column")
            self.assertEqual(column["width"], "auto")
            self.assertEqual(len(column["elements"]), 1)
            button = column["elements"][0]
            self.assertEqual(button["tag"], "button")
            self.assertTrue(button["name"])
            names.append(button["name"])
        self.assertEqual(len(names), len(set(names)), "两个提交按钮的 name 必须互不相同")

    def test_mutation_missing_button_name_is_caught_at_assembly_time(self) -> None:
        """变异验红①（W0-1 探针裁定：200530 只在真实点击时触发，建卡请求本身
        不会暴露，必须在装配阶段静态钉死）：手工改坏
        ``core/admin/management_card._callback_button``，去掉 form 内提交按钮
        的 ``name``，渲染必须失败（``ValueError``），证明这条防线真的在起作用。
        """

        import lingxi.core.admin.management_card as management_card_module

        original = management_card_module._callback_button

        def _broken_callback_button(*, label, style, value, form_submit=False, name=None):
            # 模拟"忘记传 name"这一类回归：无条件丢弃调用方传入的 name。
            return original(label=label, style=style, value=value, form_submit=False)

        management_card_module._callback_button = _broken_callback_button
        try:
            with self.assertRaises(ValueError):
                render_management_card(
                    _status(),
                    display_identifier="ou_target",
                    catalog=FakeCatalog(),
                    display_names=FakeDisplayNames(),
                )
        finally:
            management_card_module._callback_button = original

    def test_company_and_metric_options_come_from_the_injected_catalog(self) -> None:
        catalog = FakeCatalog(companies=["1011", "1012", "1013"], metrics=["sub_new_count"])
        card = render_management_card(
            _status(), display_identifier="ou_target",
            catalog=catalog,
            display_names=FakeDisplayNames(),
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
            _status(), display_identifier="ou_target", catalog=FakeCatalog(companies=[], metrics=[]),
            display_names=FakeDisplayNames(),
        )
        selects = _find_selects(_find_forms(card)[0]["elements"])
        for select in selects:
            self.assertEqual(len(select["options"]), 1)
            self.assertEqual(select["options"][0]["value"], "")

    def test_catalog_raising_an_exception_does_not_crash_rendering(self) -> None:
        """否定断言：目录端口本身抛异常不得让整张卡渲染失败——目录不可用是
        展示层的可选降级分支，不是渲染函数的前置条件。"""

        card = render_management_card(
            _status(), display_identifier="ou_target", catalog=RaisingCatalog(), display_names=FakeDisplayNames()
        )
        selects = _find_selects(_find_forms(card)[0]["elements"])
        self.assertEqual(len(selects), 2)
        for select in selects:
            self.assertEqual(select["options"][0]["value"], "")


class ZeroInternalIdTests(unittest.TestCase):
    """防倒退关卡（Trace #469 S-1）：管理员可见文本零 ou_/lpo_/pac_。用一份
    真实会触发全部三种内部 ID（open_id/override_id）的输入构造卡片，逐一确认
    可见文本（``_visible_text``：不含隐藏的按钮/组件 value/name 字段）里一个
    都不出现，同时确认公司/指标编号确实经过了 ``AdminDisplayNames`` 翻译。
    """

    def _card(self) -> dict:
        override = LocalPermissionOverrideView(
            override_id="lpo_01JGFJJZ008XSHEADGG8V74SPC",
            direction="suppress",
            company_id="1011",
            metric_name="sub_new_count",
            reason="特批",
            created_at="2026-08-30T00:00:00+00:00",
        )
        summary = GalaxySourceSummary(
            granted=True, reason="granted", companies=("1011",), functions=("运营",)
        )
        display_names = FakeDisplayNames(
            company_labels={"1011": "壹壹测试公司（1011）"},
            metric_labels={"sub_new_count": "新增订户数"},
        )
        return render_management_card(
            _status(local_overrides=(override,), galaxy_source=summary),
            display_identifier="ou_admin_typed_this",
            catalog=FakeCatalog(companies=["1011"], metrics=["sub_new_count"]),
            display_names=display_names,
        )

    def test_visible_text_never_contains_internal_id_prefixes(self) -> None:
        text = _visible_text(self._card())
        self.assertNotIn("ou_", text)
        self.assertNotIn("lpo_", text)
        self.assertNotIn("pac_", text)

    def test_company_and_metric_ids_are_translated_in_visible_text(self) -> None:
        """正向断言：不是简单地"没有出现内部 ID"，而是确实展示了翻译结果——
        防止实现退化成"整段丢弃"而不是"翻译"。精确核对本地覆盖行本身（不是
        银河来源段或下拉选项——那两处走不同的代码路径）。"""

        text = _visible_text(self._card())
        self.assertIn("公司 壹壹测试公司（1011） · 指标 新增订户数", text)

    def test_mutation_dropping_the_display_names_translation_turns_the_assertion_red(
        self,
    ) -> None:
        """变异验红②：改坏 ``_override_row_elements``，让它退回展示原始
        company_id/metric_name（不经过 ``display_names``），零内部 ID 之外的
        「确实翻译了」这条正向断言必须变红，证明
        ``test_company_and_metric_ids_are_translated_in_visible_text`` 真的在
        盯着这条翻译逻辑，不是凑巧通过。"""

        import lingxi.core.admin.management_card as management_card_module

        original = management_card_module._override_row_elements

        def _broken_override_row_elements(
            override, *, display_identifier, company_label_for, metric_label_for
        ):
            # 模拟"忘记调用 display_names"这一类回归：直接展示原始 ID。
            description = management_card_module._markdown(
                f"- 公司 {override.company_id} · 指标 {override.metric_name}"
            )
            button = management_card_module._callback_button(
                label="撤销",
                style="danger",
                value={
                    "admin_action": management_card_module.ADMIN_ACTION_REVOKE,
                    "override_id": override.override_id,
                    "identifier": display_identifier,
                },
            )
            return description, button

        management_card_module._override_row_elements = _broken_override_row_elements
        try:
            text = _visible_text(self._card())
            # 覆盖行本身的原文退回了未翻译的裸编号——变异确实命中了这一行
            # （银河来源段与下拉选项走的是另一条代码路径，未被这次变异影响，
            # 仍然正确展示翻译结果，因此不能用"全卡搜不到翻译结果"这种过粗的
            # 判据，必须精确核对被改坏的那一行本身）。
            self.assertIn("公司 1011 · 指标 sub_new_count", text)
            self.assertNotIn("公司 壹壹测试公司（1011） · 指标 新增订户数", text)
        finally:
            management_card_module._override_row_elements = original


class ConnectionCountingDisplayNames:
    """模拟真实实现连接成本的计数假实现（Trace #469 修复包 B，B-7）：批量
    方法（``company_labels``/``metric_labels``）与单项方法
    （``company_label``/``metric_label``）各自记一次调用花费的"连接数"——
    与真实 ``adapters/admin_registry.PostgresAdminQueries`` 同一数量级
    （批量整批 2 条连接；单项每次调用也是 2 条连接，且不管一次调用翻译
    多少个编号，成本都一样是 2——这正是"批量"与"逐项"唯一的可观察差异：
    调用**次数**，不是每次调用本身的开销）。用于证明渲染函数确实改走了
    批量端口一次性处理整批编号，而不是对每个编号各自触发一次单项调用。
    """

    _COST_PER_CALL = 2

    def __init__(
        self,
        *,
        companies: dict[str, str] | None = None,
        metrics: dict[str, str] | None = None,
    ) -> None:
        self._companies = companies or {}
        self._metrics = metrics or {}
        self.connection_count = 0
        self.company_label_calls = 0
        self.metric_label_calls = 0
        self.company_labels_calls = 0
        self.metric_labels_calls = 0

    def user_label(self, *, open_id: str) -> str:
        return "该用户"

    def company_label(self, *, company_id: str) -> str:
        self.company_label_calls += 1
        self.connection_count += self._COST_PER_CALL
        return self._companies.get(company_id, company_id)

    def metric_label(self, *, metric_id: str) -> str:
        self.metric_label_calls += 1
        self.connection_count += self._COST_PER_CALL
        return self._metrics.get(metric_id, metric_id)

    def company_labels(self, *, company_ids):
        self.company_labels_calls += 1
        self.connection_count += self._COST_PER_CALL
        return {cid: self._companies.get(cid, cid) for cid in company_ids}

    def metric_labels(self, *, metric_ids):
        self.metric_labels_calls += 1
        self.connection_count += self._COST_PER_CALL
        return {mid: self._metrics.get(mid, mid) for mid in metric_ids}


class ConnectionStormRegressionTests(unittest.TestCase):
    """Trace #469 修复包 B，B-7：管理卡渲染此前对下拉里每一个公司/指标编号
    各自调用一次 ``company_label``/``metric_label``——真实实现每次调用都
    新建两条数据库连接，公司目录当前 43 个编号，一张卡片因此打开约 90 条
    连接（审查实测坐实）。修复后单次渲染的连接数应当是一个与目录规模无关
    的常数上界。"""

    def test_a_single_render_stays_within_a_small_connection_budget(self) -> None:
        display_names = ConnectionCountingDisplayNames(
            companies={f"c{i}": f"公司{i}" for i in range(40)},
            metrics={f"m{i}": f"指标{i}" for i in range(9)},
        )
        override = LocalPermissionOverrideView(
            override_id="lpo_01JGFJJZ008XSHEADGG8V74SPC",
            direction="grant",
            company_id="c0",
            metric_name="m0",
            reason="特批",
            created_at="2026-08-30T00:00:00+00:00",
        )
        summary = GalaxySourceSummary(
            granted=True, reason="granted", companies=("c1", "c2"), functions=("运营",)
        )
        catalog = FakeCatalog(
            companies=[f"c{i}" for i in range(40)], metrics=[f"m{i}" for i in range(9)]
        )

        render_management_card(
            _status(local_overrides=(override,), galaxy_source=summary),
            display_identifier="ou_admin_typed_this",
            catalog=catalog,
            display_names=display_names,
        )

        self.assertLessEqual(
            display_names.connection_count,
            5,
            "一次渲染的连接数应当是与目录规模无关的常数上界（本用例用 40 个"
            "公司、9 个指标模拟真实 43/9 规模），不应随公司/指标数量线性增长",
        )
        # 精确核对：批量端口各自恰好只被调用一次，单项端口一次都没被调用——
        # 不是"总连接数凑巧够低"，而是真的走了批量姿势。
        self.assertEqual(display_names.company_labels_calls, 1)
        self.assertEqual(display_names.metric_labels_calls, 1)
        self.assertEqual(display_names.company_label_calls, 0)
        self.assertEqual(display_names.metric_label_calls, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
