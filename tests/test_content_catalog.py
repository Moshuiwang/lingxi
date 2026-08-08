"""#101 内容目录、占位变量和用户可见出口的白盒断言。"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tomllib
import unittest
from datetime import date
from pathlib import Path

from lingxi.config.content import (
    CONTENT_PATH,
    ContentCatalog,
    ContentRenderError,
    ContentSafetyError,
    ContentValidationError,
    REQUIRED_CARD_KEYS,
    REQUIRED_TEXT_KEYS,
    default_content_catalog,
    validate_user_visible_text,
)
from lingxi.core.identity.roster_audit import ArchivedIdentity, compare_roster
from lingxi.core.identity.roster_report import render_daily_report


_FORMAL_RENDERING_MODULES = (
    "lingxi.core.conversation.pipeline",
    "lingxi.core.identity.first_contact",
    "lingxi.core.identity.roster_report",
)


def _document() -> dict:
    with CONTENT_PATH.open("rb") as stream:
        return tomllib.load(stream)


def _imported_bot_test_modules(source_root: Path) -> tuple[str, ...]:
    """在干净解释器中导入正式入口，再读取其传递导入闭包。"""

    probe = """
import importlib
import sys

for module_name in sys.argv[1:]:
    importlib.import_module(module_name)

for loaded_name in sorted(sys.modules):
    if any(
        loaded_name == forbidden
        or loaded_name.startswith(forbidden + ".")
        for forbidden in (
            "lingxi.core.identity.onboarding",
            "lingxi.adapters.feishu_onboarding",
            "lingxi.adapters.oauth_bridge",
            "lingxi.adapters.refresh_tokens",
            "lingxi.adapters.postgres_onboarding",
        )
    ):
        print(loaded_name)
"""
    environment = os.environ.copy()
    inherited_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        path for path in (str(source_root), inherited_pythonpath) if path
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe, *_FORMAL_RENDERING_MODULES],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "正式渲染入口导入失败：\n"
            + (completed.stderr or completed.stdout).strip()
        )
    return tuple(line for line in completed.stdout.splitlines() if line)


def _code_string_literals(path: Path):
    """只取代码常量，不把模块/类/函数 docstring 当作用户展示字面量。"""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def visit(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = list(node.body)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                body = body[1:]
            for child in body:
                yield from visit(child)
            for field_name in ("decorator_list", "returns", "type_comment"):
                value = getattr(node, field_name, None)
                if isinstance(value, list):
                    for child in value:
                        yield from visit(child)
                elif isinstance(value, ast.AST):
                    yield from visit(value)
            return
        for child in ast.iter_child_nodes(node):
            yield from visit(child)

    yield from visit(tree)


class ContentDirectoryTests(unittest.TestCase):
    def test_default_catalog_has_exact_registered_keys_and_a_version(self) -> None:
        catalog = default_content_catalog()

        self.assertTrue(catalog.version)
        self.assertEqual(catalog.text_keys(), REQUIRED_TEXT_KEYS)
        self.assertEqual(catalog.card_keys(), REQUIRED_CARD_KEYS)

    def test_missing_or_unregistered_text_key_fails_before_rendering(self) -> None:
        missing = _document()
        del missing["texts"]["gateway.busy_hint"]
        with self.assertRaises(ContentValidationError):
            ContentCatalog.from_mapping(missing)

        extra = _document()
        extra["texts"]["not_registered"] = "不应被静默接受"
        with self.assertRaises(ContentValidationError):
            ContentCatalog.from_mapping(extra)

    def test_card_configuration_is_display_only(self) -> None:
        catalog = default_content_catalog()
        card = catalog.card("query.result", result="已取得结果")

        self.assertEqual(set(card.display_fields), {"title", "body", "button_labels"})
        self.assertNotIn("action", card.display_fields)
        self.assertNotIn("url", card.display_fields)
        self.assertNotIn("permission", card.display_fields)

        invalid = _document()
        invalid["cards"]["query.result"]["action"] = "execute"
        with self.assertRaises(ContentValidationError):
            ContentCatalog.from_mapping(invalid)

    def test_card_configuration_rejects_a_button_type_or_url(self) -> None:
        for field in ("button_type", "url", "permission"):
            with self.subTest(field=field):
                invalid = _document()
                invalid["cards"]["query.result"][field] = "unexpected"
                with self.assertRaises(ContentValidationError):
                    ContentCatalog.from_mapping(invalid)

    def test_placeholder_variables_must_match_in_both_directions(self) -> None:
        catalog = default_content_catalog()
        with self.assertRaises(ContentRenderError):
            catalog.text("onboarding.completed", company_name="公司")

        secret = "fake-secret-value"
        with self.assertRaises(ContentRenderError) as raised:
            catalog.text(
                "onboarding.completed",
                company_name="公司",
                function_name="职能",
                unexpected=secret,
            )
        self.assertNotIn(secret, str(raised.exception))

        rendered = catalog.text(
            "onboarding.completed", company_name="测试公司", function_name="测试职能"
        )
        self.assertEqual(rendered.key, "onboarding.completed")
        self.assertEqual(rendered.version, catalog.version)
        self.assertIn("测试公司", rendered.text)

    def test_a_content_only_change_changes_rendered_text_without_code_change(self) -> None:
        document = _document()
        document["texts"]["gateway.busy_hint"] = "内容目录中的新提示"
        changed = ContentCatalog.from_mapping(document)

        self.assertNotEqual(
            default_content_catalog().text("gateway.busy_hint").text,
            changed.text("gateway.busy_hint").text,
        )

    def test_a_content_only_change_reaches_a_formal_renderer(self) -> None:
        document = _document()
        document["texts"]["roster.report_title"] = "目录中的日报标题"
        changed = ContentCatalog.from_mapping(document)
        report = compare_roster(
            [ArchivedIdentity("usr_1", "person_1", "张三", "E1", "a@example.com")],
            [{"personnel_id": "person_1", "name": "张三改名", "employee_no": "E1", "email": "a@example.com"}],
        )

        rendered = render_daily_report(report, report_date=date(2026, 8, 8), catalog=changed)
        self.assertTrue(rendered.startswith("目录中的日报标题 "))

    def test_formal_renderers_do_not_embed_chinese_user_text_literals(self) -> None:
        source_root = Path(__file__).parents[1] / "src" / "lingxi"
        formal_renderers = (
            source_root / "core" / "conversation" / "pipeline.py",
            source_root / "core" / "identity" / "first_contact.py",
            source_root / "core" / "identity" / "roster_report.py",
        )
        for path in formal_renderers:
            for literal in _code_string_literals(path):
                with self.subTest(path=path, literal=literal):
                    self.assertFalse(
                        any("一" <= character <= "鿿" for character in literal),
                        "正式渲染入口的用户可见中文字面量必须来自内容目录",
                    )

    def test_formal_renderers_do_not_import_bot_test_assets_transitively(self) -> None:
        source_root = Path(__file__).parents[1] / "src" / "lingxi"
        imported = _imported_bot_test_modules(source_root.parent)
        self.assertEqual(
            imported,
            (),
            "正式渲染入口的传递导入闭包不得包含 Bot-Test 资产模块："
            + ", ".join(imported),
        )


class UserVisibleOutputTests(unittest.TestCase):
    def test_output_checker_rejects_internal_tools_process_logs_and_remaining_time(self) -> None:
        invalid = (
            "mcp__query__list_metrics",
            "trace_id=trc_fake",
            "预计剩余 2 分钟",
            "权限不足",
        )
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ContentSafetyError):
                validate_user_visible_text(text)

    def test_card_output_checker_also_rejects_injected_internal_terms(self) -> None:
        with self.assertRaises(ContentSafetyError):
            default_content_catalog().card(
                "query.status", status="internal_marker", internal_terms=("internal_marker",)
            )

    def test_safe_business_text_is_accepted(self) -> None:
        self.assertEqual(validate_user_visible_text("本次未取得可用结果。"), "本次未取得可用结果。")


if __name__ == "__main__":
    unittest.main()
