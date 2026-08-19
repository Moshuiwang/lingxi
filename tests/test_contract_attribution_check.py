"""`scripts/ci/check_contract_attribution.py` 的判定用例（Issue #238）。

跟 ``test_acceptance_matrix_check.py`` 同一惯例：先构造违规输入，断言被具体拒绝；
末尾反过来核对真实仓库的登记表，证明检查不是靠临时夹具"空转"出来的绿灯。
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


class ContractSectionsTest(unittest.TestCase):
    def test_parses_level_two_and_three_headings(self) -> None:
        text = "# 产品合同与外部边界\n\n## 甲\n正文。\n\n### 乙\n正文。\n\n#### 丙\n忽略四级标题。\n"
        self.assertEqual(CHECK.contract_sections(text), {"甲", "乙"})

    def test_skips_fenced_code_blocks(self) -> None:
        text = "## 真章节\n\n```\n## 伪章节（代码块里的示例）\n```\n"
        self.assertEqual(CHECK.contract_sections(text), {"真章节"})


class TriggeredLinesTest(unittest.TestCase):
    def test_finds_both_trigger_phrases(self) -> None:
        with self._tmp_file("凭据不入库（合同明令）。\n别的规则是合同要求。\n无关的一行。\n") as path:
            hits = CHECK.find_triggered_lines(path)
        self.assertEqual([n for n, _ in hits], [1, 2])

    def test_bare_contract_word_without_trigger_phrase_is_ignored(self) -> None:
        """裸的"合同"不触发——本仓库大量用它表示模块自身的接口/服务合同。"""

        with self._tmp_file("OnboardingRunner.start 的服务合同是幂等返回。\n") as path:
            self.assertEqual(CHECK.find_triggered_lines(path), [])

    def _tmp_file(self, content: str):
        import tempfile

        class _Ctx:
            def __enter__(self_inner):
                self_inner.dir = tempfile.TemporaryDirectory()
                path = Path(self_inner.dir.name) / "sample.py"
                path.write_text(content, encoding="utf-8")
                return path

            def __exit__(self_inner, *exc_info):
                self_inner.dir.cleanup()

        return _Ctx()


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
            extra.write_text("# 自测：这句话是产品合同明令，但从未在合同正文出现过\n", encoding="utf-8")
            original = CHECK.tracked_files
            CHECK.tracked_files = self._patched_tracked_files(extra)
            try:
                failures, _ = CHECK.evaluate()
            finally:
                CHECK.tracked_files = original
        self.assertTrue(any("self_test_injected.py" in f and "未登记" in f for f in failures), failures)

    def test_without_the_injected_file_the_gate_is_green(self) -> None:
        original = CHECK.tracked_files
        CHECK.tracked_files = self._patched_tracked_files(None)
        try:
            failures, _ = CHECK.evaluate()
        finally:
            CHECK.tracked_files = original
        self.assertEqual(failures, [])


class RegistryIntegrityTest(unittest.TestCase):
    def test_grounded_entry_pointing_at_nonexistent_section_is_rejected(self) -> None:
        fake = CHECK.GroundedAttribution(
            "src/lingxi/core/ids.py", "ULID", "这一节在合同里并不存在"
        )
        original = CHECK.GROUNDED_ATTRIBUTIONS
        CHECK.GROUNDED_ATTRIBUTIONS = original + (fake,)
        try:
            failures, _ = CHECK.evaluate()
        finally:
            CHECK.GROUNDED_ATTRIBUTIONS = original
        self.assertTrue(any("找不到这个标题" in f for f in failures), failures)

    def test_grounded_entry_with_stale_excerpt_is_rejected(self) -> None:
        fake = CHECK.GroundedAttribution(
            "src/lingxi/core/ids.py",
            "这段摘录绝对不会出现在 ids.py 里__self_test__",
            "统一用户记录与权限变化",
        )
        original = CHECK.GROUNDED_ATTRIBUTIONS
        CHECK.GROUNDED_ATTRIBUTIONS = original + (fake,)
        try:
            failures, _ = CHECK.evaluate()
        finally:
            CHECK.GROUNDED_ATTRIBUTIONS = original
        self.assertTrue(any("找不到了" in f for f in failures), failures)


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
        failures, summary = CHECK.evaluate()
        self.assertEqual(failures, [], failures)
        self.assertIn("归属核对：扫描到", summary)


if __name__ == "__main__":
    unittest.main()
