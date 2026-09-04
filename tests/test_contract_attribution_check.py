"""`scripts/ci/check_contract_attribution.py` 的判定用例（Issue #238）。

跟 ``test_acceptance_matrix_check.py`` 同一惯例：先构造违规输入，断言被具体拒绝；
末尾反过来核对真实仓库的登记表，证明检查不是靠临时夹具"空转"出来的绿灯。

2026-08-19 三路独立复查后新增：A7 的两个绕过面各补一条用例——短摘录覆盖未来
新增行、往已登记行后面追加未核对内容；两者都必须在切到整行精确匹配后判红。
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "ci" / "check_contract_attribution.py"


def _load_script():
    name = "contract_attribution_check_under_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # 模块用了 @dataclass：dataclasses 内部按 cls.__module__ 去 sys.modules 找回
    # 定义它的模块做类型解析，不预先登记会在装饰器求值阶段直接抛 AttributeError。
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CHECK = _load_script()


def _tmp_file(content: str):
    import tempfile

    class _Ctx:
        def __enter__(self):
            self.dir = tempfile.TemporaryDirectory()
            path = Path(self.dir.name) / "sample.py"
            path.write_text(content, encoding="utf-8")
            return path

        def __exit__(self, *exc_info):
            self.dir.cleanup()

    return _Ctx()


class ContractSectionsTest(unittest.TestCase):
    def test_parses_level_two_and_three_headings(self) -> None:
        text = (
            "# 产品合同与外部边界\n\n## 甲\n正文。\n\n### 乙\n正文。\n\n#### 丙\n忽略四级标题。\n"
        )
        self.assertEqual(CHECK.contract_sections(text), {"产品合同与外部边界", "甲", "乙"})

    def test_skips_fenced_code_blocks(self) -> None:
        text = "## 真章节\n\n```\n## 伪章节（代码块里的示例）\n```\n"
        self.assertEqual(CHECK.contract_sections(text), {"真章节"})


class TriggeredLinesTest(unittest.TestCase):
    def test_finds_expanded_trigger_phrases(self) -> None:
        content = (
            "凭据不入库（合同明令）。\n"
            "别的规则是合同要求。\n"
            "无关的一行。\n"
            "合同规定必须这样。\n"
            "合同约定的另一件事。\n"
            "合同条款第一条。\n"
            "合同明确排除这一项。\n"
            "按合同处理即可。\n"
        )
        with _tmp_file(content) as path:
            hits = CHECK.find_triggered_lines(path)
        self.assertEqual([n for n, _ in hits], [1, 2, 4, 5, 6, 7, 8])

    def test_bare_contract_word_without_trigger_phrase_is_ignored(self) -> None:
        """裸的"合同"不触发——本仓库大量用它表示模块自身的接口/服务合同。"""

        with _tmp_file("OnboardingRunner.start 的服务合同是幂等返回。\n") as path:
            self.assertEqual(CHECK.find_triggered_lines(path), [])

    def test_coverage_checklist_meta_vocabulary_is_excluded(self) -> None:
        """ "合同条款覆盖清单"是验收矩阵自己的机制名字，不是归属声明。"""

        content = (
            "### 10.3 合同条款覆盖清单\n"
            "2. 合同条款无断言覆盖：忘了登记。\n"
            "维护一份机器可读的映射：`产品合同条款 → 验收断言编号 → 测试用例`。\n"
        )
        with _tmp_file(content) as path:
            self.assertEqual(CHECK.find_triggered_lines(path), [])

    def test_meta_exclusion_does_not_swallow_a_real_attribution_on_a_different_line(self) -> None:
        content = "### 10.3 合同条款覆盖清单\n这一行另外还有一句合同要求单独出现。\n"
        with _tmp_file(content) as path:
            hits = CHECK.find_triggered_lines(path)
        self.assertEqual([n for n, _ in hits], [2])


class EvaluateSelfConsistencyTest(unittest.TestCase):
    """自证：往仓库里植入一条未登记的归属断言 ⇒ 红；移除 ⇒ 绿。

    不直接改动真实被跟踪文件——改用一个临时文件并打桩 ``tracked_files``，
    这样即使用例中途异常退出也不会在工作树里留下改动。
    """

    def _patched_tracked_files(self, extra: Path | None):
        real_tracked_files = CHECK.tracked_files

        def patched():
            files = real_tracked_files()
            if extra is not None:
                files.append(extra)
            return files

        return patched

    def test_unregistered_attribution_turns_the_gate_red(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            extra = Path(tmp) / "self_test_injected.py"
            extra.write_text(
                "# 自测：这句话是产品合同明令，但从未在合同正文出现过\n", encoding="utf-8"
            )
            original = CHECK.tracked_files
            CHECK.tracked_files = self._patched_tracked_files(extra)
            try:
                failures, _exceptions, _summary = CHECK.evaluate()
            finally:
                CHECK.tracked_files = original
        self.assertTrue(
            any("self_test_injected.py" in f and "未登记" in f for f in failures), failures
        )

    def test_without_the_injected_file_the_gate_is_green(self) -> None:
        original = CHECK.tracked_files
        CHECK.tracked_files = self._patched_tracked_files(None)
        try:
            failures, _exceptions, _summary = CHECK.evaluate()
        finally:
            CHECK.tracked_files = original
        self.assertEqual(failures, [])


class ExactLineMatchingClosesBypassesTest(unittest.TestCase):
    """A7（2026-08-19 三路独立复查坐实）：子串匹配的两个绕过面，切到整行精确
    匹配后必须都判红。不改真实文件——打桩 ``tracked_files`` 指向临时文件。
    """

    def _patched_tracked_files(self, extra: Path):
        real_tracked_files = CHECK.tracked_files

        def patched():
            return real_tracked_files() + [extra]

        return patched

    def test_a_short_generic_registered_line_does_not_cover_a_different_new_line(self) -> None:
        """把登记的"行"故意设成裸的"合同要求"四个字，不能覆盖住另一行同样
        提到"合同要求"但内容完全不同的新增断言——旧的子串匹配会让它绿。
        """

        fake = CHECK.GroundedAttribution("__self_test__/short.py", "合同要求", "问数与多轮对话")
        original_grounded = CHECK.GROUNDED_ATTRIBUTIONS
        CHECK.GROUNDED_ATTRIBUTIONS = original_grounded + (fake,)
        try:
            with self._self_test_dir() as tmp:
                extra = Path(tmp) / "short.py"
                extra.write_text(
                    "合同要求\n"  # 逐字等于登记值，应当被覆盖
                    "# 这是一句全新的、从未核对过的合同要求断言\n",  # 不应被覆盖
                    encoding="utf-8",
                )
                self._resolve_self_test_file(extra)
                original_tracked = CHECK.tracked_files
                CHECK.tracked_files = self._patched_tracked_files(extra)
                try:
                    failures, _exceptions, _summary = CHECK.evaluate()
                finally:
                    CHECK.tracked_files = original_tracked
        finally:
            CHECK.GROUNDED_ATTRIBUTIONS = original_grounded
        self.assertTrue(
            any(
                "这是一句全新的、从未核对过的合同要求断言" in f and "未登记" in f for f in failures
            ),
            failures,
        )

    def test_appending_a_new_claim_to_an_already_registered_line_is_rejected(self) -> None:
        """往一行已经登记过的话后面继续追加全新内容：整行文本变了，不再逐字
        等于登记值，必须重新核对——不能因为旧摘录仍是新行的子串就放行。
        """

        original_line = "凭据不入库是产品合同明令。"
        fake = CHECK.GroundedAttribution(
            "__self_test__/append.py", original_line, "统一用户记录与权限变化"
        )
        original_grounded = CHECK.GROUNDED_ATTRIBUTIONS
        CHECK.GROUNDED_ATTRIBUTIONS = original_grounded + (fake,)
        try:
            with self._self_test_dir() as tmp:
                extra = Path(tmp) / "append.py"
                appended_line = original_line + "另外还要求密钥每日轮换（这句从未核对过）"
                extra.write_text(appended_line + "\n", encoding="utf-8")
                original_tracked = CHECK.tracked_files
                CHECK.tracked_files = self._patched_tracked_files(extra)
                try:
                    failures, _exceptions, _summary = CHECK.evaluate()
                finally:
                    CHECK.tracked_files = original_tracked
        finally:
            CHECK.GROUNDED_ATTRIBUTIONS = original_grounded
        self.assertTrue(any("未登记" in f and "密钥每日轮换" in f for f in failures), failures)

    def _self_test_dir(self):
        import tempfile

        return tempfile.TemporaryDirectory()

    def _resolve_self_test_file(self, path: Path) -> None:
        del path


class MinimumExcerptLengthTest(unittest.TestCase):
    def test_a_too_short_registered_line_is_rejected(self) -> None:
        fake = CHECK.GroundedAttribution("src/lingxi/core/ids.py", "合同要求", "问数与多轮对话")
        original = CHECK.GROUNDED_ATTRIBUTIONS
        CHECK.GROUNDED_ATTRIBUTIONS = original + (fake,)
        try:
            failures, _exceptions, _summary = CHECK.evaluate()
        finally:
            CHECK.GROUNDED_ATTRIBUTIONS = original
        self.assertTrue(any("过短" in f for f in failures), failures)


class RegistryIntegrityTest(unittest.TestCase):
    def test_grounded_entry_pointing_at_nonexistent_section_is_rejected(self) -> None:
        fake = CHECK.GroundedAttribution(
            "src/lingxi/core/ids.py", "这一整行显然不存在于 ids.py 里", "这一节在合同里并不存在"
        )
        original = CHECK.GROUNDED_ATTRIBUTIONS
        CHECK.GROUNDED_ATTRIBUTIONS = original + (fake,)
        try:
            failures, _exceptions, _summary = CHECK.evaluate()
        finally:
            CHECK.GROUNDED_ATTRIBUTIONS = original
        self.assertTrue(any("找不到这个标题" in f for f in failures), failures)

    def test_grounded_entry_with_stale_line_is_rejected(self) -> None:
        fake = CHECK.GroundedAttribution(
            "src/lingxi/core/ids.py",
            "这一整行绝对不会出现在 ids.py 里__self_test__",
            "统一用户记录与权限变化",
        )
        original = CHECK.GROUNDED_ATTRIBUTIONS
        CHECK.GROUNDED_ATTRIBUTIONS = original + (fake,)
        try:
            failures, _exceptions, _summary = CHECK.evaluate()
        finally:
            CHECK.GROUNDED_ATTRIBUTIONS = original
        self.assertTrue(any("找不到了" in f for f in failures), failures)


class ExceptionDebtIsAlwaysVisibleTest(unittest.TestCase):
    """B2：例外债务必须在成功与失败两条路径上都能取到，不能只在通过时可见。"""

    def test_exception_notes_are_returned_even_when_the_check_fails(self) -> None:
        fake = CHECK.GroundedAttribution(
            "src/lingxi/core/ids.py", "这一整行显然不存在", "这一节在合同里并不存在"
        )
        original = CHECK.GROUNDED_ATTRIBUTIONS
        CHECK.GROUNDED_ATTRIBUTIONS = original + (fake,)
        try:
            failures, exceptions, _summary = CHECK.evaluate()
        finally:
            CHECK.GROUNDED_ATTRIBUTIONS = original
        self.assertTrue(failures)
        self.assertTrue(exceptions, "真实仓库当前有已登记例外，即使本次判红也应当照常返回")

    def test_exception_entries_must_carry_source_date_and_owner(self) -> None:
        incomplete = CHECK.RegisteredException(
            "src/lingxi/core/ids.py", "这一整行显然不存在于 ids.py 里__x", "", "", "", "缺三项"
        )
        original = CHECK.REGISTERED_EXCEPTIONS
        CHECK.REGISTERED_EXCEPTIONS = original + (incomplete,)
        try:
            failures, _exceptions, _summary = CHECK.evaluate()
        finally:
            CHECK.REGISTERED_EXCEPTIONS = original
        self.assertTrue(any("缺少来源" in f for f in failures), failures)


class FailClosedTest(unittest.TestCase):
    def test_missing_contract_document_fails_closed(self) -> None:
        original = CHECK.CONTRACT_DOCUMENT
        CHECK.CONTRACT_DOCUMENT = Path("/nonexistent/path/does-not-exist.md")
        try:
            with self.assertRaises(CHECK.AttributionCheckError):
                CHECK.evaluate()
        finally:
            CHECK.CONTRACT_DOCUMENT = original


class RealRepositoryTest(unittest.TestCase):
    """反向验证：真实仓库当前的登记表必须与实际扫描结果完全对齐。"""

    def test_real_repository_registry_is_consistent(self) -> None:
        failures, _exceptions, summary = CHECK.evaluate()
        self.assertEqual(failures, [], failures)
        self.assertIn("归属核对：扫描到", summary)

    def test_real_repository_has_the_five_known_exceptions(self) -> None:
        _failures, exceptions, _summary = CHECK.evaluate()
        self.assertEqual(len(exceptions), 5, exceptions)


class MetaExclusionDoesNotSwallowRealAssertionsTest(unittest.TestCase):
    """M1（2026-08-19 三方复查最后一轮）：元排除此前是"整行出现过就整行跳过"，
    一句话前半下断言、后半指向覆盖清单会让整行连同真实断言一起消失。"""

    def test_a_line_with_both_a_real_assertion_and_meta_vocabulary_is_still_caught(self) -> None:
        with _tmp_file("产品合同要求凭据不得入库；详见合同条款覆盖清单。\n") as path:
            hits = CHECK.find_triggered_lines(path)
        self.assertEqual(len(hits), 1, hits)
        self.assertIn("产品合同要求凭据不得入库", hits[0][1])

    def test_a_purely_meta_line_is_still_excluded(self) -> None:
        with _tmp_file("### 10.3 合同条款覆盖清单\n") as path:
            self.assertEqual(CHECK.find_triggered_lines(path), [])


class HtmlCommentsAreSkippedTest(unittest.TestCase):
    """M3：合同文档里注释掉的旧标题不应被当成仍然存在的真实章节。"""

    def test_a_heading_inside_an_html_comment_block_does_not_count(self) -> None:
        text = (
            "# 产品合同与外部边界\n\n"
            "## 真实章节\n正文。\n\n"
            "<!--\n## 已删除的旧章节\n旧正文，改名前留作参考。\n-->\n\n"
            "## 另一节\n正文。\n"
        )
        sections = CHECK.contract_sections(text)
        self.assertIn("真实章节", sections)
        self.assertIn("另一节", sections)
        self.assertNotIn("已删除的旧章节", sections)

    def test_a_single_line_self_contained_comment_does_not_hide_a_real_heading_before_it(
        self,
    ) -> None:
        text = "## 真标题 <!-- 备注 -->\n"
        self.assertEqual(CHECK.contract_sections(text), {"真标题"})


class OccurrenceCountMatchingTest(unittest.TestCase):
    """M4：登记表按出现次数核对，不是"这一行文本在集合里就算"——把已登记的
    整行复制到同文件另一处，多出来的那次出现必须被单独判红。
    """

    def _patched_tracked_files(self, extra: Path):
        real_tracked_files = CHECK.tracked_files

        def patched():
            return real_tracked_files() + [extra]

        return patched

    def test_a_duplicated_registered_line_flags_only_the_excess_occurrence(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            extra = Path(tmp) / "dup.py"
            extra.write_text(
                "合同要求这行是唯一登记过的原文\n"
                "# 中间的无关内容\n"
                "合同要求这行是唯一登记过的原文\n",
                encoding="utf-8",
            )
            display_path = CHECK._display_path(extra)
            fake = CHECK.GroundedAttribution(
                display_path, "合同要求这行是唯一登记过的原文", "问数与多轮对话"
            )
            original_grounded = CHECK.GROUNDED_ATTRIBUTIONS
            CHECK.GROUNDED_ATTRIBUTIONS = original_grounded + (fake,)
            original_tracked = CHECK.tracked_files
            CHECK.tracked_files = self._patched_tracked_files(extra)
            try:
                failures, _exceptions, _summary = CHECK.evaluate()
            finally:
                CHECK.GROUNDED_ATTRIBUTIONS = original_grounded
                CHECK.tracked_files = original_tracked
        dup_failures = [f for f in failures if "dup.py" in f and "出现次数配额" in f]
        self.assertEqual(len(dup_failures), 1, failures)
        self.assertIn(":3：", dup_failures[0])  # 第 3 行才是超出配额的重复出现


class ExceptionFieldContentValidationTest(unittest.TestCase):
    """M5：例外字段不能只是非空占位符——来源必须是「Issue #数字」/「PR #数字」
    格式，裁定日期必须是合法 ISO 日期，理由不能是空字符串。"""

    def test_placeholder_values_are_rejected_on_every_field(self) -> None:
        bad = CHECK.RegisteredException(
            "src/lingxi/core/ids.py", "这一整行显然不存在于 ids.py 里__m5_test", "x", "x", "x", ""
        )
        original = CHECK.REGISTERED_EXCEPTIONS
        CHECK.REGISTERED_EXCEPTIONS = original + (bad,)
        try:
            failures, _exceptions, _summary = CHECK.evaluate()
        finally:
            CHECK.REGISTERED_EXCEPTIONS = original
        self.assertTrue(
            any("不是" in f and ("Issue" in f or "PR" in f) for f in failures), failures
        )
        self.assertTrue(any("日期" in f for f in failures), failures)
        self.assertTrue(any("reason" in f and "空字符串" in f for f in failures), failures)

    def test_a_well_formed_exception_passes_field_validation(self) -> None:
        good = CHECK.RegisteredException(
            "src/lingxi/core/ids.py",
            "这一整行显然不存在于 ids.py 里__m5_test_good",
            "Issue #999",
            "2026-08-19",
            "产品负责人",
            "自测夹具，非真实例外。",
        )
        original = CHECK.REGISTERED_EXCEPTIONS
        CHECK.REGISTERED_EXCEPTIONS = original + (good,)
        try:
            failures, _exceptions, _summary = CHECK.evaluate()
        finally:
            CHECK.REGISTERED_EXCEPTIONS = original
        # 字段格式本身合法；唯一还会报的失败应该是"摘录在源文件里找不到"
        # （这条夹具文本本来就不存在于 ids.py），证明字段校验没有误伤合法输入。
        format_failures = [
            f
            for f in failures
            if "不是" in f or ("日期" in f and "不合法" not in f) or "空字符串" in f
        ]
        self.assertEqual(format_failures, [], failures)


class ExpandedTriggerRegexFamilyTest(unittest.TestCase):
    """M6：触发词从固定短语清单改成正则族，新出现的归属动词（此前清单里没有
    的"禁止""指出"，以及"依据产品合同"这类前缀写法）应当自动被捞到，不需要
    再往清单里逐个添加短语。"""

    def test_previously_unlisted_suffix_verbs_are_caught(self) -> None:
        content = "产品合同禁止这么做。\n产品合同指出另一件事。\n"
        with _tmp_file(content) as path:
            hits = CHECK.find_triggered_lines(path)
        self.assertEqual(len(hits), 2, hits)

    def test_prefix_form_with_a_verb_not_in_the_old_fixed_list_is_caught(self) -> None:
        with _tmp_file("依据产品合同，这里必须这样处理。\n") as path:
            hits = CHECK.find_triggered_lines(path)
        self.assertEqual(len(hits), 1, hits)


if __name__ == "__main__":
    unittest.main()
