"""`scripts/ci/check_size_ratchet.py` 的解析与判定用例（Issue #238）。

跟 ``test_acceptance_matrix_check.py`` 同一惯例：每个用例先构造一份会违规的输入，
断言它被具体地拒绝，而不是只跑一遍真实基线看它绿——一份只会通过的检查等于没有检查。
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "ci" / "check_size_ratchet.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("size_ratchet_check_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CHECK = _load_script()


class BaselineParsingTest(unittest.TestCase):
    def test_good_baseline_parses(self) -> None:
        text = "# 注释行\n\n2048\tsrc/lingxi/apps/scheduler/__init__.py\n1951\tsrc/lingxi/adapters/postgres_conversation.py\n"
        self.assertEqual(
            CHECK.parse_baseline(text),
            {
                "src/lingxi/apps/scheduler/__init__.py": 2048,
                "src/lingxi/adapters/postgres_conversation.py": 1951,
            },
        )

    def test_malformed_row_fails_closed(self) -> None:
        with self.assertRaises(CHECK.BaselineError):
            CHECK.parse_baseline("not-a-number\tsrc/lingxi/x.py\n")

    def test_missing_tab_fails_closed(self) -> None:
        with self.assertRaises(CHECK.BaselineError):
            CHECK.parse_baseline("2048 src/lingxi/x.py\n")

    def test_duplicate_path_fails_closed(self) -> None:
        with self.assertRaises(CHECK.BaselineError):
            CHECK.parse_baseline("100\tsrc/lingxi/x.py\n200\tsrc/lingxi/x.py\n")


class EvaluateTest(unittest.TestCase):
    """核心判定逻辑：不依赖磁盘，直接喂 (baseline, current) 字典。"""

    def test_growth_beyond_recorded_ceiling_is_rejected(self) -> None:
        baseline = {"src/lingxi/apps/scheduler/__init__.py": 2048}
        current = {"src/lingxi/apps/scheduler/__init__.py": 2050}
        failures = CHECK.evaluate(baseline, current)
        self.assertTrue(any("超过棘轮基线记录的上限" in f for f in failures), failures)

    def test_shrinking_below_recorded_ceiling_is_rejected_until_refreshed(self) -> None:
        """基线必须与实测精确相等；这正是拒绝「手工把基线调大」的手段（见下一条用例）。"""

        baseline = {"src/lingxi/apps/scheduler/__init__.py": 2048}
        current = {"src/lingxi/apps/scheduler/__init__.py": 1800}
        failures = CHECK.evaluate(baseline, current)
        self.assertTrue(any("与实测" in f and "不一致" in f for f in failures), failures)

    def test_exact_match_passes(self) -> None:
        baseline = {"src/lingxi/apps/scheduler/__init__.py": 2048}
        current = {"src/lingxi/apps/scheduler/__init__.py": 2048}
        self.assertEqual(CHECK.evaluate(baseline, current), [])

    def test_new_file_crossing_threshold_unregistered_is_rejected(self) -> None:
        baseline: dict[str, int] = {}
        current = {"src/lingxi/core/new_giant_module.py": CHECK.THRESHOLD_LINES + 1}
        failures = CHECK.evaluate(baseline, current)
        self.assertTrue(any("新超过体量棘轮阈值" in f for f in failures), failures)

    def test_file_under_threshold_and_unregistered_passes(self) -> None:
        baseline: dict[str, int] = {}
        current = {"src/lingxi/core/small_module.py": 42}
        self.assertEqual(CHECK.evaluate(baseline, current), [])

    def test_deleted_file_leaving_baseline_is_not_a_failure(self) -> None:
        """文件已经不在扫描范围内：棘轮的目的已经达成，不强制立即刷新。"""

        baseline = {"src/lingxi/apps/scheduler/__init__.py": 2048}
        current: dict[str, int] = {}
        self.assertEqual(CHECK.evaluate(baseline, current), [])

    def test_manually_inflating_the_baseline_without_touching_the_file_is_rejected(self) -> None:
        """自证：试图把基线调大 ⇒ 红。文件实际还是 2048 行，基线被手工改成 9999。"""

        baseline = {"src/lingxi/apps/scheduler/__init__.py": 9999}
        current = {"src/lingxi/apps/scheduler/__init__.py": 2048}
        failures = CHECK.evaluate(baseline, current)
        self.assertTrue(any("与实测" in f and "不一致" in f for f in failures), failures)


class RenderRoundTripTest(unittest.TestCase):
    def test_render_then_parse_round_trips(self) -> None:
        entries = {"src/lingxi/x.py": 1600, "src/lingxi/y/z.py": 2000}
        rendered = CHECK.render_baseline(entries)
        self.assertEqual(CHECK.parse_baseline(rendered), entries)


class RealBaselineIsHonestTest(unittest.TestCase):
    """反向验证：仓库里已经提交的基线文件本身必须诚实（与真实行数精确相等）。"""

    def test_committed_baseline_matches_actual_line_counts(self) -> None:
        baseline = CHECK.load_baseline(CHECK.BASELINE_PATH)
        current = CHECK.measure(CHECK.iter_scope_files())
        self.assertEqual(CHECK.evaluate(baseline, current), [])

    def test_committed_baseline_only_contains_the_two_known_files(self) -> None:
        baseline = CHECK.load_baseline(CHECK.BASELINE_PATH)
        self.assertEqual(
            set(baseline),
            {
                "src/lingxi/adapters/postgres_conversation.py",
                "src/lingxi/apps/scheduler/__init__.py",
            },
        )


if __name__ == "__main__":
    unittest.main()
