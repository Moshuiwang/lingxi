"""`scripts/ci/check_comment_ratchet.py` 的解析与判定用例。

跟 `test_size_ratchet_check.py` 同一惯例：每个用例先构造一份会违规的输入，
断言它被具体地拒绝，而不是只跑一遍真实基线看它绿。
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "ci" / "check_comment_ratchet.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("comment_ratchet_check_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CHECK = _load_script()


class BaselineParsingTest(unittest.TestCase):
    def test_good_baseline_parses(self) -> None:
        text = "# 注释行\n\n3\tprovenance\tsrc/lingxi/x.py\n"
        self.assertEqual(CHECK.parse_baseline(text), {("provenance", "src/lingxi/x.py"): 3})

    def test_malformed_row_fails_closed(self) -> None:
        with self.assertRaises(CHECK.BaselineError):
            CHECK.parse_baseline("not-a-number\tprovenance\tsrc/lingxi/x.py\n")

    def test_missing_column_fails_closed(self) -> None:
        with self.assertRaises(CHECK.BaselineError):
            CHECK.parse_baseline("3\tsrc/lingxi/x.py\n")

    def test_unknown_category_fails_closed(self) -> None:
        with self.assertRaises(CHECK.BaselineError):
            CHECK.parse_baseline("3\tnot_a_real_category\tsrc/lingxi/x.py\n")

    def test_duplicate_key_fails_closed(self) -> None:
        with self.assertRaises(CHECK.BaselineError):
            CHECK.parse_baseline("3\tprovenance\tsrc/lingxi/x.py\n4\tprovenance\tsrc/lingxi/x.py\n")


class EvaluateTest(unittest.TestCase):
    def test_growth_beyond_recorded_ceiling_is_rejected(self) -> None:
        baseline = {("provenance", "src/lingxi/x.py"): 3}
        current = {("provenance", "src/lingxi/x.py"): 4}
        failures = CHECK.evaluate(baseline, current)
        self.assertTrue(any("超过棘轮基线记录的上限" in f for f in failures), failures)

    def test_shrinking_below_recorded_ceiling_is_rejected_until_refreshed(self) -> None:
        baseline = {("provenance", "src/lingxi/x.py"): 3}
        current = {("provenance", "src/lingxi/x.py"): 1}
        failures = CHECK.evaluate(baseline, current)
        self.assertTrue(any("与实测" in f and "不一致" in f for f in failures), failures)

    def test_exact_match_passes(self) -> None:
        baseline = {("provenance", "src/lingxi/x.py"): 3}
        current = {("provenance", "src/lingxi/x.py"): 3}
        self.assertEqual(CHECK.evaluate(baseline, current), [])

    def test_new_nonzero_unregistered_pair_is_rejected(self) -> None:
        baseline: dict[tuple[str, str], int] = {}
        current = {("hash_block_over", "src/lingxi/new.py"): 1}
        failures = CHECK.evaluate(baseline, current)
        self.assertTrue(any("未登记在基线里" in f for f in failures), failures)

    def test_zero_and_unregistered_passes(self) -> None:
        # current 只保留非零项，因此这里模拟"该 (类别, 路径) 从未出现在 current 里"
        baseline: dict[tuple[str, str], int] = {}
        current: dict[tuple[str, str], int] = {}
        self.assertEqual(CHECK.evaluate(baseline, current), [])

    def test_deleted_or_fully_cleaned_pair_still_requires_a_refresh(self) -> None:
        """与 check_size_ratchet.py 的"文件删除自动放行"不同：这里 current 只保留
        非零计数，一个 (类别, 路径) 从 current 里消失（无论是文件被删还是计数
        真的清零到 0）在语义上等价于"实测 0"，与基线记录的非零值不一致，同样
        需要显式 --refresh 才能清空登记——不允许陈旧登记静默残留。"""

        baseline = {("provenance", "src/lingxi/gone.py"): 3}
        current: dict[tuple[str, str], int] = {}
        failures = CHECK.evaluate(baseline, current)
        self.assertTrue(any("与实测" in f and "不一致" in f for f in failures), failures)

    def test_manually_inflating_the_baseline_without_touching_the_file_is_rejected(self) -> None:
        baseline = {("provenance", "src/lingxi/x.py"): 999}
        current = {("provenance", "src/lingxi/x.py"): 3}
        failures = CHECK.evaluate(baseline, current)
        self.assertTrue(any("与实测" in f and "不一致" in f for f in failures), failures)


class RenderRoundTripTest(unittest.TestCase):
    def test_render_then_parse_round_trips(self) -> None:
        entries = {
            ("provenance", "src/lingxi/x.py"): 3,
            ("docstring_over", "src/lingxi/y.py"): 1,
        }
        rendered = CHECK.render_baseline(entries)
        self.assertEqual(CHECK.parse_baseline(rendered), entries)


class RealBaselineIsHonestTest(unittest.TestCase):
    def test_committed_baseline_matches_actual_counts(self) -> None:
        baseline = CHECK.load_baseline(CHECK.BASELINE_PATH)
        current = CHECK.measure(CHECK.iter_scope_files())
        self.assertEqual(CHECK.evaluate(baseline, current), [])

    def test_committed_baseline_entries_are_all_within_scope_and_shaped_correctly(self) -> None:
        baseline = CHECK.load_baseline(CHECK.BASELINE_PATH)
        for category, path in baseline:
            self.assertIn(category, CHECK.CATEGORIES)
            self.assertTrue(path.startswith("src/lingxi/"), path)

    def test_committed_baseline_is_empty_after_the_cleanup(self) -> None:
        """自证：rc25 B-8a 收官批把最后 4 条 provenance 登记清零之后，基线回到空
        文件——这是棘轮的目标终态（与 function_size_ratchet 归零时同一姿态）。

        空基线下棘轮退化为「任何 (类别, 路径) 首次出现非零计数即判红」，
        「未登记判红」这条规则由 :class:`EvaluateTest` 的固定样本用例覆盖，
        不再依赖真实仓库里恰好存在非零登记。
        """

        baseline = CHECK.load_baseline(CHECK.BASELINE_PATH)
        self.assertEqual(baseline, {})


class RunRefreshClassificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.source_root = root / "src" / "lingxi"
        self.source_root.mkdir(parents=True)
        self.baseline_path = root / "baseline.txt"

        self._orig_repository_root = CHECK.REPOSITORY_ROOT
        self._orig_source_root = CHECK.SOURCE_ROOT
        self._orig_baseline_path = CHECK.BASELINE_PATH
        CHECK.REPOSITORY_ROOT = root
        CHECK.SOURCE_ROOT = self.source_root
        CHECK.BASELINE_PATH = self.baseline_path
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        CHECK.REPOSITORY_ROOT = self._orig_repository_root
        CHECK.SOURCE_ROOT = self._orig_source_root
        CHECK.BASELINE_PATH = self._orig_baseline_path

    def _write(self, relative: str, source: str) -> None:
        path = self.source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    def test_refresh_lowers_a_shrunk_entry(self) -> None:
        # 1 处 provenance 命中（此前登记为 3）
        self._write("x.py", "# Issue #12\nx = 1\n")
        self.baseline_path.write_text(
            CHECK.render_baseline({("provenance", "src/lingxi/x.py"): 3}), encoding="utf-8"
        )
        exit_code = CHECK.run_refresh()
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            CHECK.load_baseline(self.baseline_path), {("provenance", "src/lingxi/x.py"): 1}
        )

    def test_refresh_removes_an_entry_that_reached_zero(self) -> None:
        self._write("clean.py", "x = 1\n")
        self.baseline_path.write_text(
            CHECK.render_baseline({("provenance", "src/lingxi/clean.py"): 3}), encoding="utf-8"
        )
        exit_code = CHECK.run_refresh()
        self.assertEqual(exit_code, 0)
        self.assertEqual(CHECK.load_baseline(self.baseline_path), {})

    def test_refresh_refuses_when_an_unregistered_pair_newly_appears(self) -> None:
        self._write("clean.py", "x = 1\n")  # 会被清零，refresh 本该能处理
        self._write("dirty.py", "# Issue #99\ny = 1\n")  # 全新违规
        self.baseline_path.write_text(
            CHECK.render_baseline({("provenance", "src/lingxi/clean.py"): 3}), encoding="utf-8"
        )
        exit_code = CHECK.run_refresh()
        self.assertEqual(exit_code, 1)
        self.assertEqual(
            CHECK.load_baseline(self.baseline_path), {("provenance", "src/lingxi/clean.py"): 3}
        )

    def test_refresh_refuses_when_a_registered_pair_grew_past_its_ceiling(self) -> None:
        self._write("grown.py", "# Issue #1\n# Issue #2\n# Issue #3\nx = 1\n")
        self.baseline_path.write_text(
            CHECK.render_baseline({("provenance", "src/lingxi/grown.py"): 1}), encoding="utf-8"
        )
        exit_code = CHECK.run_refresh()
        self.assertEqual(exit_code, 1)
        self.assertEqual(
            CHECK.load_baseline(self.baseline_path), {("provenance", "src/lingxi/grown.py"): 1}
        )


class BootstrapTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.source_root = root / "src" / "lingxi"
        self.source_root.mkdir(parents=True)
        self.baseline_path = root / "baseline.txt"

        self._orig_repository_root = CHECK.REPOSITORY_ROOT
        self._orig_source_root = CHECK.SOURCE_ROOT
        self._orig_baseline_path = CHECK.BASELINE_PATH
        CHECK.REPOSITORY_ROOT = root
        CHECK.SOURCE_ROOT = self.source_root
        CHECK.BASELINE_PATH = self.baseline_path
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        CHECK.REPOSITORY_ROOT = self._orig_repository_root
        CHECK.SOURCE_ROOT = self._orig_source_root
        CHECK.BASELINE_PATH = self._orig_baseline_path

    def test_bootstrap_writes_all_current_nonzero_counts_when_file_absent(self) -> None:
        (self.source_root / "dirty.py").write_text("# Issue #1\nx = 1\n", encoding="utf-8")
        (self.source_root / "clean.py").write_text("x = 1\n", encoding="utf-8")
        exit_code = CHECK.run_bootstrap()
        self.assertEqual(exit_code, 0)
        baseline = CHECK.load_baseline(self.baseline_path)
        self.assertEqual(list(baseline), [("provenance", "src/lingxi/dirty.py")])

    def test_bootstrap_refuses_when_file_already_exists(self) -> None:
        self.baseline_path.write_text(CHECK.render_baseline({}), encoding="utf-8")
        exit_code = CHECK.run_bootstrap()
        self.assertEqual(exit_code, 1)


class FixedSampleAnalysisTest(unittest.TestCase):
    """固定样本判定：docstring 11 行判超、`#` 块 6 行判超，`Issue #12` /
    `2026-09-04` / `审查` 各命中来历正则。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.source_root = root / "src" / "lingxi"
        self.source_root.mkdir(parents=True)

        self._orig_repository_root = CHECK.REPOSITORY_ROOT
        self._orig_source_root = CHECK.SOURCE_ROOT
        CHECK.REPOSITORY_ROOT = root
        CHECK.SOURCE_ROOT = self.source_root
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        CHECK.REPOSITORY_ROOT = self._orig_repository_root
        CHECK.SOURCE_ROOT = self._orig_source_root

    def _write(self, name: str, source: str) -> Path:
        path = self.source_root / name
        path.write_text(source, encoding="utf-8")
        return path

    def test_eleven_line_function_docstring_is_over(self) -> None:
        body = "\n".join(f"    line{i}" for i in range(9))
        path = self._write("probe.py", f'def f():\n    """summary\n{body}\n    """\n    return 1\n')
        result = CHECK._analyze_file(path)
        self.assertEqual(result["docstring_over"], 1)

    def test_ten_line_function_docstring_is_not_over(self) -> None:
        body = "\n".join(f"    line{i}" for i in range(8))
        path = self._write("probe.py", f'def f():\n    """summary\n{body}\n    """\n    return 1\n')
        result = CHECK._analyze_file(path)
        self.assertEqual(result["docstring_over"], 0)

    def test_sixteen_line_module_docstring_is_over(self) -> None:
        body = "\n".join(f"line{i}" for i in range(14))
        path = self._write("probe.py", f'"""summary\n{body}\n"""\nx = 1\n')
        result = CHECK._analyze_file(path)
        self.assertEqual(result["docstring_over"], 1)

    def test_six_line_hash_block_is_over(self) -> None:
        source = "\n".join(f"# comment {i}" for i in range(6)) + "\nx = 1\n"
        path = self._write("probe.py", source)
        result = CHECK._analyze_file(path)
        self.assertEqual(result["hash_block_over"], 1)

    def test_five_line_hash_block_is_not_over(self) -> None:
        source = "\n".join(f"# comment {i}" for i in range(5)) + "\nx = 1\n"
        path = self._write("probe.py", source)
        result = CHECK._analyze_file(path)
        self.assertEqual(result["hash_block_over"], 0)

    def test_trailing_inline_comment_does_not_count_toward_hash_block(self) -> None:
        source = "\n".join(f"x{i} = {i}  # trailing {i}" for i in range(6)) + "\n"
        path = self._write("probe.py", source)
        result = CHECK._analyze_file(path)
        self.assertEqual(result["hash_block_over"], 0)

    def test_issue_number_hits_provenance(self) -> None:
        path = self._write("probe.py", "# Issue #12 说明\nx = 1\n")
        result = CHECK._analyze_file(path)
        self.assertEqual(result["provenance"], 1)

    def test_date_hits_provenance(self) -> None:
        path = self._write("probe.py", "# 2026-09-04 记录\nx = 1\n")
        result = CHECK._analyze_file(path)
        self.assertEqual(result["provenance"], 1)

    def test_review_word_hits_provenance(self) -> None:
        path = self._write("probe.py", "# 独立审查确认\nx = 1\n")
        result = CHECK._analyze_file(path)
        self.assertEqual(result["provenance"], 1)

    def test_plain_rationale_comment_does_not_hit_provenance(self) -> None:
        path = self._write("probe.py", "# 单个任务失败不得带走 worker\nx = 1\n")
        result = CHECK._analyze_file(path)
        self.assertEqual(result["provenance"], 0)

    def test_bare_audit_word_in_ordinary_prose_does_not_hit_provenance(self) -> None:
        """独立审核实测坐实：裸「审核」不贴来历标记时会连坐正常语义，必须收窄。"""

        path = self._write("probe.py", "# 内容审核器\nx = 1\n")
        result = CHECK._analyze_file(path)
        self.assertEqual(result["provenance"], 0)

    def test_bare_recheck_word_in_ordinary_prose_does_not_hit_provenance(self) -> None:
        path = self._write("probe.py", "# 这道复核要挡的是\nx = 1\n")
        result = CHECK._analyze_file(path)
        self.assertEqual(result["provenance"], 0)

    def test_qualifier_adjacent_review_word_hits_provenance(self) -> None:
        path = self._write("probe.py", "# codex 审查 P1-2\nx = 1\n")
        result = CHECK._analyze_file(path)
        self.assertEqual(result["provenance"], 1)

    def test_review_word_followed_by_parenthesized_date_hits_provenance(self) -> None:
        path = self._write("probe.py", "# 审查（2026-09-04）\nx = 1\n")
        result = CHECK._analyze_file(path)
        self.assertEqual(result["provenance"], 1)

    def test_ruling_word_followed_by_letter_number_id_hits_provenance(self) -> None:
        path = self._write("probe.py", "# 裁定 D-15\nx = 1\n")
        result = CHECK._analyze_file(path)
        self.assertEqual(result["provenance"], 1)


if __name__ == "__main__":
    unittest.main()
