"""#101 内容目录、占位变量和用户可见出口的白盒断言。"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
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


#: #443 对外名称规范：复用 `scripts/ci/check_content_version.py` 已有的
#: `flatten_visible`（摊平 [texts]/[cards] 的全部用户可见文案），不在这里重写
#: 一份第二份摊平逻辑——两处实现分叉是这类检查最容易悄悄失效的方式。
_CONTENT_VERSION_CHECK_PATH = (
    Path(__file__).parents[1] / "scripts" / "ci" / "check_content_version.py"
)
_INTERNAL_CODENAME_PATTERN = re.compile(r"灵犀|lingxi", re.IGNORECASE)


def _load_content_version_check():
    spec = importlib.util.spec_from_file_location(
        "check_content_version_under_test_443", _CONTENT_VERSION_CHECK_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


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
            # 2026-08-23 #146 清退：feishu_onboarding/refresh_tokens/postgres_onboarding
            # 三个 Bot-Test 资产模块已删除，不再需要在这里列出。2026-08-24 #203
            # 清退：oauth_bridge 已随之删除，同样不再需要列出。core.identity.onboarding
            # 仍由 scheduler 多处消费，继续保留在禁止名单里——它不属于正式渲染入口的
            # 传递依赖闭包。
            "lingxi.core.identity.onboarding",
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

    def test_card_titles_match_the_2026_08_16_pm_final_copy(self) -> None:
        """Issue #175：处理中「正在查询」、成功「查询完成」、失败「查询未完成」，
        空结果沿用「查询完成」；标题只在正文承载一份，卡片不再单独带 header
        （header 去留由 `adapters.feishu_delivery` 的 `_card_payload` 断言覆盖）。"""

        catalog = default_content_catalog()
        self.assertEqual(catalog.card("query.status", status="处理中").title, "正在查询")
        self.assertEqual(catalog.card("query.result", result="结果").title, "查询完成")
        self.assertEqual(catalog.card("query.failure", message="失败").title, "查询未完成")
        self.assertEqual(catalog.card("query.empty", elapsed_seconds=1).title, "查询完成")

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

    def test_a_template_naming_the_trace_id_placeholder_is_rejected_at_load_time(self) -> None:
        """#280 联合设计 §0.1【阻断级】：占位名如果叫 ``trace_id``，会在目录**加载期**
        被内容安全规则拒绝——`pipeline.py` 在 import 期就调用 `default_content_catalog()`，
        按字面实现会让 gateway/worker/scheduler 三个进程全部起不来，不是"某条消息发不
        出去"。占位名必须换成 ``{reference}``（本次改动的实际选择）。这是设计要求补的
        主动违规测试，堵住"以后有人把占位名改回 trace_id"这个回归。"""

        document = _document()
        document["texts"]["onboarding.internal_error"] = (
            document["texts"]["onboarding.internal_error"] + "追溯号：{trace_id}。"
        )
        with self.assertRaises(ContentValidationError):
            ContentCatalog.from_mapping(document)

    def test_a_rendered_text_smuggling_the_forbidden_marker_through_a_variable_is_rejected(
        self,
    ) -> None:
        """占位名本身安全（``reference``），但如果调用方往里塞入含 ``trace_id=`` 的值，
        渲染出口仍必须拦截——不能只在目录加载期防模板本身，调用方传入的资料值同样要防。"""

        with self.assertRaises(ContentSafetyError):
            default_content_catalog().text("onboarding.internal_error", reference="trace_id=fake")

    def test_every_key_declaring_reference_renders_with_it_and_rejects_without_it(self) -> None:
        """占位变量集合的自动化对账（Issue #280 §10.2）：遍历 ``REQUIRED_TEXT_KEYS``，
        任何一个键只要在 content.toml 里声明了 ``{reference}``，本测试就强制它必须
        用 ``reference=`` 渲染成功、且省略时必须抛 ``ContentRenderError``——不逐个
        手写键名，防止未来第五个需要追溯号的键悄悄漏了调用方传值。"""

        from string import Formatter

        document = _document()
        catalog = default_content_catalog()
        exercised = 0
        for key in REQUIRED_TEXT_KEYS:
            template = document["texts"][key]
            variables = {
                field
                for _literal, field, _spec, _conv in Formatter().parse(template)
                if field
            }
            if "reference" not in variables:
                continue
            exercised += 1
            with self.subTest(key=key):
                others = {name: "x" for name in variables if name != "reference"}
                with self.assertRaises(ContentRenderError):
                    catalog.text(key, **others)
                rendered = catalog.text(key, reference="ref-x", **others)
                self.assertIn("ref-x", rendered.text)
        self.assertGreaterEqual(
            exercised, 3, "本用例应至少覆盖 internal_error/sync_timeout/stalled 三个键"
        )

    def test_reference_requiring_key_gates_stay_reconciled_across_render_entry_points(
        self,
    ) -> None:
        """独立审查（分支 fix/291-280-user-experience 收尾）：哪些键需要
        ``{reference}`` 占位这件事，在 content.toml 之外还被三处代码各自维护了
        一份字面量集合——``pipeline._KEYS_REQUIRING_REFERENCE``、
        ``onboarding_runner._KEYS_REQUIRING_REFERENCE``、
        ``first_contact._DEFERRED_RENDER_KEYS``——此前只靠模块文档里"靠字面值
        对齐"这句话互相担保，没有任何门禁真正比对过。这里把 content.toml 的声明
        当作唯一事实，对账全部四处：

        1. ``onboarding_runner`` 与 ``pipeline`` 两份"需要补 reference"的集合
           必须完全相等——它们描述的是同一件事（哪些终态文案在调用方没有显式
           给出 reference 时要由渲染层自动补上），只是各自在不同层维护了一份
           拷贝（见两个模块里对「共用线程复核」的引用）。
        2. ``first_contact`` 的判定层是纯函数，拿不到 ``trace_id``，只处理它
           自己 ``_MESSAGE_KEYS`` 能产出的那个子集；它的集合必须**恰好**是
           "全量需要补 reference 的键" 与 "它自己能产出的键" 的交集——多一个
           说明留了一条永远不会触发的死判据，少一个说明有一条它能产出的终态
           会在真正渲染时因为缺 reference 而失败关闭，却没有任何一处代码为
           这个后果留过痕迹。
        3. content.toml 里声明 ``{reference}`` 的全部键，必须等于 "全量需要补
           reference 的集合" 并上一组已知、已文档化的例外——它们各自在调用处
           **直接**传 ``reference``，不经过 onboarding/first_contact 这条三层
           补值分支，因此不需要出现在任何一份"需要补"集合里：
           ``onboarding.stalled``（``apps/scheduler/stalled_provisioning.py``
           直传 ``values={"reference": ...}``）；``delivery.sheet_failed``/
           ``delivery.sheet_uncertain``（Issue #354 S-H3-2，
           ``apps/gateway/document_delivery.py::DocumentDeliveryConsumer.
           _fail``/``_uncertain`` 在调用处直传 ``template_variables=
           {"reference": claim.task_id}``——这条渲染路径是 gateway 独立消费
           循环的终态通知，与 onboarding 首聊/开通通知完全是两条不相交的调用
           链，天然不会、也不该经过 onboarding 那三层的补值分支）。每新增一个
           例外都必须像这样指名调用点、说明它为什么走的是一条不同的渲染路径；
           只登记数字上限、不写清楚理由的新增例外应当被拒绝。

        改动任意一处集合、或在 content.toml 新增/移除一个 ``{reference}`` 键
        却没有同步其余各处，本用例都会变红。
        """

        from string import Formatter

        from lingxi.core.conversation import pipeline as pipeline_module
        from lingxi.core.identity import first_contact as first_contact_module
        from lingxi.core.identity import onboarding_runner as onboarding_runner_module

        document = _document()
        catalog_reference_keys = {
            key
            for key, template in document["texts"].items()
            if any(
                field == "reference"
                for _literal, field, _spec, _conv in Formatter().parse(template)
            )
        }

        onboarding_keys = onboarding_runner_module._KEYS_REQUIRING_REFERENCE
        pipeline_keys = pipeline_module._KEYS_REQUIRING_REFERENCE
        first_contact_keys = first_contact_module._DEFERRED_RENDER_KEYS

        self.assertEqual(
            onboarding_keys,
            pipeline_keys,
            "onboarding_runner 与 pipeline 各自维护的『需要补 reference』集合已经分叉",
        )

        reachable_by_first_contact = onboarding_keys & set(
            first_contact_module._MESSAGE_KEYS.values()
        )
        self.assertEqual(
            first_contact_keys,
            reachable_by_first_contact,
            "first_contact 的『需要延迟渲染』集合必须恰好是它自己能产出的终态键"
            "与全量『需要补 reference』集合的交集",
        )

        # 已知、已文档化的直传值例外，见本用例说明第 3 条：
        # - onboarding.stalled → apps/scheduler/stalled_provisioning.py 的
        #   KEY_STALLED 渲染调用；
        # - delivery.sheet_failed/delivery.sheet_uncertain（Issue #354
        #   S-H3-2）→ apps/gateway/document_delivery.py 的
        #   DocumentDeliveryConsumer._fail/_uncertain，走 gateway 独立消费
        #   循环的终态通知，与 onboarding 三层补值分支不相交；
        # - gateway.unexpected_error（Issue #465，100% 响应覆盖）→
        #   core/conversation/pipeline.py 的
        #   EventPipeline._handle_unexpected_failure 在调用处**直传**
        #   ``reference=message.trace_id``——这是 handle_message 的方法级异常
        #   兜底出口，不经过开通结果渲染（_render_onboarding_result）那条三层
        #   补值分支，天然不相交，理由同 delivery.sheet_* 两条。
        known_direct_value_exceptions = frozenset(
            {
                "onboarding.stalled",
                "delivery.sheet_failed",
                "delivery.sheet_uncertain",
                "gateway.unexpected_error",
            }
        )
        self.assertEqual(
            catalog_reference_keys,
            onboarding_keys | known_direct_value_exceptions,
            "content.toml 新增或移除了一个 {reference} 占位键，但代码侧的集合"
            "（或本用例登记的直传值例外）没有同步更新",
        )

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

    def test_the_six_naming_keys_render_bi_plus_and_never_the_internal_codename(self) -> None:
        """#443 对外名称规范（产品负责人 2026-08-30 裁定）：「灵犀/lingxi/Lingxi」是
        内部代号，不得出现在任何用户可见面；对外统一「BI Plus」。这六个键是当次
        扫明的全部 content.toml 暴露面（开通链四条 + 内测未开放一条 + 停用一条），
        逐一渲染断言必须包含「BI Plus」，且不含内部代号（大小写不敏感的 lingxi
        与中文「灵犀」都要挡住）。"""

        catalog = default_content_catalog()
        cases: dict[str, dict[str, str]] = {
            "onboarding.matched": {},
            "onboarding.syncing": {},
            "onboarding.completed": {"company_name": "测试公司", "function_name": "测试职能"},
            "onboarding.not_authorized": {},
            "onboarding.innertest_not_open": {},
            "gateway.suspended": {},
        }
        for key, kwargs in cases.items():
            with self.subTest(key=key):
                rendered = catalog.text(key, **kwargs).text
                self.assertIn("BI Plus", rendered)
                self.assertIsNone(
                    _INTERNAL_CODENAME_PATTERN.search(rendered),
                    f"{key} 渲染文本仍包含内部代号：{rendered!r}",
                )

    def test_content_toml_user_visible_text_has_zero_internal_codename_hits(self) -> None:
        """低成本的全局兜底（#443）：即便未来在 content.toml 新增第七个键时不小心
        带回内部代号，这条检查也会先于人工审计发现，不必逐键手写。只查
        content.toml 的用户可见文案（[texts]/[cards]，复用门禁自己的摊平逻辑），
        不外溢到内部日志/代码标识/文档技术名——那些按 #443 白名单继续允许使用
        「Lingxi」。"""

        check = _load_content_version_check()
        pairs, errors = check.flatten_visible(_document())
        self.assertEqual(errors, [])
        hits = sorted(key for key, text in pairs if _INTERNAL_CODENAME_PATTERN.search(text))
        self.assertEqual(hits, [], f"content.toml 用户可见文案仍命中内部代号的键：{hits}")


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


class ModelGeneratedTextValidationTests(unittest.TestCase):
    """Issue #322：投递层模板校验器误伤模型正文的根因回归。

    两次内测实测：模型终态答案以「还需」收尾或提到「过程日志」，被为固定模板
    设计的 ``_UNSAFE_FIXED_MARKERS`` 词表拦截成 ``ContentSafetyError`` →
    投递层判定失败 → 任务卡死 uncertain，用户等不到答案。产品负责人裁定的目标
    设计：模型生成的终态正文只保留协议泄漏级防线（``_PROCESS_MARKER_PATTERN``），
    调用方用 ``contains_model_text=True`` 显式声明；``content.toml`` 模板本身
    的校验一字不动（见 ``test_content_toml_template_gate_...``）。
    """

    def test_model_final_answer_with_everyday_wording_delivers_through_the_result_card(
        self,
    ) -> None:
        """两次内测真实复现的原句：模型答案以「还需」收尾、或提到「过程日志」，
        都必须能正常渲染进终态卡片——这正是本次要修的两起 uncertain 卡死。"""

        catalog = default_content_catalog()
        for answer in (
            "已完成周环比分析，还需要看其他维度吗？",
            "已核对排查过程日志，结论如下：无异常。",
        ):
            with self.subTest(answer=answer):
                card = catalog.card("query.result", result=answer, contains_model_text=True)
                self.assertEqual(card.body, answer)

    def test_model_stopped_partial_answer_with_everyday_wording_still_renders(self) -> None:
        """``/stop`` 中断时的残余正文同样是模型生成的终态正文
        （``worker.stopped_result``），同一类日常措辞不应该在 worker 侧就抛
        ``ContentSafetyError``（否则连投递层都到不了）。"""

        rendered = default_content_catalog().text(
            "worker.stopped_result", result="还需要继续挖掘吗", contains_model_text=True
        )
        self.assertIn("还需要继续挖掘吗", rendered.text)

    def test_model_text_still_faces_the_protocol_leak_defense(self) -> None:
        """放宽只针对自然语言固定词表；协议泄漏（内部工具名/trace_id 等）对模型
        正文仍然是响亮拒绝——这是刻意保留的唯一防线，不能被这次修复一并放开。"""

        for leak in ("mcp__bi_metric__list_metrics 调用失败", "trace_id=trc_fake 排查完成"):
            with self.subTest(leak=leak):
                with self.assertRaises(ContentSafetyError):
                    default_content_catalog().card(
                        "query.result", result=leak, contains_model_text=True
                    )

    def test_calls_without_the_model_text_flag_still_reject_the_fixed_marker_word_list(
        self,
    ) -> None:
        """默认行为（省略 ``contains_model_text``）必须保持原有强度不变——放宽
        只对显式声明"这是模型正文"的调用生效，不是全局降级。"""

        with self.assertRaises(ContentSafetyError):
            default_content_catalog().card("query.result", result="还需要继续吗")
        with self.assertRaises(ContentSafetyError):
            default_content_catalog().text("worker.stopped_result", result="还需要继续吗")

    def test_content_toml_template_gate_still_rejects_fixed_markers_at_load_time(self) -> None:
        """模板闸不回退：``content.toml`` 里我们自己写的固定文案一旦混入固定
        词表标记，仍必须在目录**加载期**拒绝——这条闸不经过运行时渲染、也不受
        ``contains_model_text`` 影响，本次放宽只发生在渲染期的模型正文变量上。"""

        for key, poisoned in (
            ("gateway.busy_hint", "当前任务还需要处理"),
            ("worker.failed", "本次任务权限不足，请稍后重试。"),
        ):
            with self.subTest(key=key):
                document = _document()
                document["texts"][key] = poisoned
                with self.assertRaises(ContentValidationError):
                    ContentCatalog.from_mapping(document)

        card_document = _document()
        card_document["cards"]["query.result"]["title"] = "查询完成，还需确认"
        with self.assertRaises(ContentValidationError):
            ContentCatalog.from_mapping(card_document)


if __name__ == "__main__":
    unittest.main()
