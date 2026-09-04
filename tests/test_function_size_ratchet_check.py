"""`scripts/ci/check_function_size_ratchet.py` 的解析与判定用例。

跟 `test_size_ratchet_check.py` 同一惯例：每个用例先构造一份会违规的输入，
断言它被具体地拒绝，而不是只跑一遍真实基线看它绿。
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "ci" / "check_function_size_ratchet.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("function_size_ratchet_check_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CHECK = _load_script()


class BaselineParsingTest(unittest.TestCase):
    def test_good_baseline_parses(self) -> None:
        text = "# 注释行\n\n90\tsrc/lingxi/apps/worker/service.py::WorkerService.run\n"
        self.assertEqual(
            CHECK.parse_baseline(text),
            {"src/lingxi/apps/worker/service.py::WorkerService.run": 90},
        )

    def test_malformed_row_fails_closed(self) -> None:
        with self.assertRaises(CHECK.BaselineError):
            CHECK.parse_baseline("not-a-number\tsrc/lingxi/x.py::f\n")

    def test_missing_tab_fails_closed(self) -> None:
        with self.assertRaises(CHECK.BaselineError):
            CHECK.parse_baseline("90 src/lingxi/x.py::f\n")

    def test_duplicate_key_fails_closed(self) -> None:
        with self.assertRaises(CHECK.BaselineError):
            CHECK.parse_baseline("70\tsrc/lingxi/x.py::f\n80\tsrc/lingxi/x.py::f\n")


class EvaluateTest(unittest.TestCase):
    """核心判定逻辑：不依赖磁盘，直接喂 (baseline, current) 字典。"""

    def test_growth_beyond_recorded_ceiling_is_rejected(self) -> None:
        baseline = {"src/lingxi/x.py::f": 70}
        current = {"src/lingxi/x.py::f": 71}
        failures = CHECK.evaluate(baseline, current)
        self.assertTrue(any("超过棘轮基线记录的上限" in f for f in failures), failures)

    def test_shrinking_below_recorded_ceiling_is_rejected_until_refreshed(self) -> None:
        baseline = {"src/lingxi/x.py::f": 70}
        current = {"src/lingxi/x.py::f": 65}
        failures = CHECK.evaluate(baseline, current)
        self.assertTrue(any("与实测" in f and "不一致" in f for f in failures), failures)

    def test_exact_match_passes(self) -> None:
        baseline = {"src/lingxi/x.py::f": 70}
        current = {"src/lingxi/x.py::f": 70}
        self.assertEqual(CHECK.evaluate(baseline, current), [])

    def test_new_function_crossing_threshold_unregistered_is_rejected(self) -> None:
        baseline: dict[str, int] = {}
        current = {"src/lingxi/x.py::new_giant": CHECK.THRESHOLD_LINES + 1}
        failures = CHECK.evaluate(baseline, current)
        self.assertTrue(any("新超过函数体量棘轮阈值" in f for f in failures), failures)

    def test_function_at_or_under_threshold_and_unregistered_passes(self) -> None:
        baseline: dict[str, int] = {}
        current = {"src/lingxi/x.py::small": CHECK.THRESHOLD_LINES}
        self.assertEqual(CHECK.evaluate(baseline, current), [])

    def test_deleted_function_leaving_baseline_now_fails_and_prompts_refresh(self) -> None:
        """独立审核实测坐实：函数已删/改名/搬走后基线登记必须判红并提示
        --refresh，不能静默保留陈旧登记（此前的行为是静默放行）。"""

        baseline = {"src/lingxi/x.py::gone": 70}
        current: dict[str, int] = {}
        failures = CHECK.evaluate(baseline, current)
        self.assertTrue(
            any("已经找不到这个函数" in f and "--refresh" in f for f in failures), failures
        )

    def test_manually_inflating_the_baseline_without_touching_the_function_is_rejected(
        self,
    ) -> None:
        baseline = {"src/lingxi/x.py::f": 9999}
        current = {"src/lingxi/x.py::f": 70}
        failures = CHECK.evaluate(baseline, current)
        self.assertTrue(any("与实测" in f and "不一致" in f for f in failures), failures)


class RenderRoundTripTest(unittest.TestCase):
    def test_render_then_parse_round_trips(self) -> None:
        entries = {"src/lingxi/x.py::a": 70, "src/lingxi/y.py::B.b": 90}
        rendered = CHECK.render_baseline(entries)
        self.assertEqual(CHECK.parse_baseline(rendered), entries)


class RealBaselineIsHonestTest(unittest.TestCase):
    """反向验证：仓库里已经提交的基线文件本身必须诚实（与真实行数精确相等）。"""

    def test_committed_baseline_matches_actual_lengths(self) -> None:
        baseline = CHECK.load_baseline(CHECK.BASELINE_PATH)
        current = CHECK.measure(CHECK.iter_scope_files())
        self.assertEqual(CHECK.evaluate(baseline, current), [])

    def test_committed_baseline_entries_are_all_within_scope_and_shaped_correctly(self) -> None:
        baseline = CHECK.load_baseline(CHECK.BASELINE_PATH)
        for key in baseline:
            self.assertTrue(key.startswith("src/lingxi/"), key)
            self.assertIn("::", key, key)

    def test_committed_baseline_is_not_accidentally_empty(self) -> None:
        """自证：真实仓库当前确实存在超阈值函数（结构性拆分是后续批的工作），
        基线不应该是空的——空基线会让「未登记函数首次超阈值判红」这条规则
        看起来"从没被真正跑到过"。"""

        baseline = CHECK.load_baseline(CHECK.BASELINE_PATH)
        self.assertGreater(len(baseline), 0)


class RunRefreshClassificationTest(unittest.TestCase):
    """`run_refresh` 只挡「超过上限」与「新函数未登记」两类失败，放行
    「基线记录 > 实测」（那正是它该修的陈旧记录）。"""

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

    def _write_function(self, relative: str, name: str, body_line_count: int) -> None:
        path = self.source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"def {name}():"] + [f"    x{i} = {i}" for i in range(body_line_count)]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_refresh_lowers_a_shrunk_entry(self) -> None:
        self._write_function("big.py", "f", 68)  # 69 total lines
        self.baseline_path.write_text(
            CHECK.render_baseline({"src/lingxi/big.py::f": 90}), encoding="utf-8"
        )
        exit_code = CHECK.run_refresh()
        self.assertEqual(exit_code, 0)
        self.assertEqual(CHECK.load_baseline(self.baseline_path), {"src/lingxi/big.py::f": 69})

    def test_refresh_removes_an_entry_that_shrank_below_threshold(self) -> None:
        self._write_function("shrunk.py", "f", 3)  # 4 total lines
        self.baseline_path.write_text(
            CHECK.render_baseline({"src/lingxi/shrunk.py::f": 90}), encoding="utf-8"
        )
        exit_code = CHECK.run_refresh()
        self.assertEqual(exit_code, 0)
        self.assertEqual(CHECK.load_baseline(self.baseline_path), {})

    def test_refresh_refuses_when_an_unregistered_function_newly_crosses_threshold(self) -> None:
        self._write_function("registered.py", "f", 3)  # shrinks, refresh should handle
        self._write_function("new_giant.py", "g", CHECK.THRESHOLD_LINES + 10)  # new violation
        self.baseline_path.write_text(
            CHECK.render_baseline({"src/lingxi/registered.py::f": 90}), encoding="utf-8"
        )
        exit_code = CHECK.run_refresh()
        self.assertEqual(exit_code, 1)
        self.assertEqual(
            CHECK.load_baseline(self.baseline_path), {"src/lingxi/registered.py::f": 90}
        )

    def test_refresh_refuses_when_a_registered_function_grew_past_its_ceiling(self) -> None:
        self._write_function("grown.py", "f", 100)
        self.baseline_path.write_text(
            CHECK.render_baseline({"src/lingxi/grown.py::f": 90}), encoding="utf-8"
        )
        exit_code = CHECK.run_refresh()
        self.assertEqual(exit_code, 1)
        self.assertEqual(CHECK.load_baseline(self.baseline_path), {"src/lingxi/grown.py::f": 90})

    def test_refresh_removes_an_entry_for_a_function_that_no_longer_exists(self) -> None:
        """函数已删/改名/搬走：evaluate() 现在会判红，--refresh 负责移除这类
        陈旧条目——同 `test_refresh_removes_an_entry_that_shrank_below_threshold`
        同一处理路径（该键在 `current` 里找不到，构造 new_baseline 时被滤掉）。"""

        self._write_function("registered.py", "f", 3)  # 仍存在，且已缩小
        self.baseline_path.write_text(
            CHECK.render_baseline(
                {
                    "src/lingxi/registered.py::f": 90,
                    "src/lingxi/gone.py::vanished": 90,
                }
            ),
            encoding="utf-8",
        )
        exit_code = CHECK.run_refresh()
        self.assertEqual(exit_code, 0)
        self.assertEqual(CHECK.load_baseline(self.baseline_path), {})


class BootstrapTest(unittest.TestCase):
    """`--bootstrap` 只在基线文件彻底不存在时可用；文件存在时必须拒绝，不能
    静默覆盖或追加，那样等于给了一个绕过"存量违规先清空再谈"的口子。"""

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

    def _write_function(self, relative: str, name: str, body_line_count: int) -> None:
        path = self.source_root / relative
        lines = [f"def {name}():"] + [f"    x{i} = {i}" for i in range(body_line_count)]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_bootstrap_writes_all_current_violations_when_file_absent(self) -> None:
        self._write_function("a.py", "big", CHECK.THRESHOLD_LINES + 5)
        self._write_function("b.py", "small", 3)
        exit_code = CHECK.run_bootstrap()
        self.assertEqual(exit_code, 0)
        baseline = CHECK.load_baseline(self.baseline_path)
        self.assertEqual(list(baseline), ["src/lingxi/a.py::big"])

    def test_bootstrap_refuses_when_file_already_exists(self) -> None:
        self.baseline_path.write_text(CHECK.render_baseline({}), encoding="utf-8")
        exit_code = CHECK.run_bootstrap()
        self.assertEqual(exit_code, 1)


class FixedSampleMutationTest(unittest.TestCase):
    """固定样本变异验红：一个 59 行函数改成 61 行必须判红。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.source_root = root / "src" / "lingxi"
        self.source_root.mkdir(parents=True)
        self.probe = self.source_root / "probe.py"

        self._orig_repository_root = CHECK.REPOSITORY_ROOT
        self._orig_source_root = CHECK.SOURCE_ROOT
        self._orig_baseline_path = CHECK.BASELINE_PATH
        CHECK.REPOSITORY_ROOT = root
        CHECK.SOURCE_ROOT = self.source_root
        CHECK.BASELINE_PATH = root / "baseline.txt"
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        CHECK.REPOSITORY_ROOT = self._orig_repository_root
        CHECK.SOURCE_ROOT = self._orig_source_root
        CHECK.BASELINE_PATH = self._orig_baseline_path

    def _write(self, body_line_count: int) -> None:
        lines = ["def probe():"] + [f"    x{i} = {i}" for i in range(body_line_count)]
        self.probe.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_59_lines_is_within_threshold(self) -> None:
        self._write(58)  # 1 def line + 58 body lines = 59
        current = CHECK.measure(CHECK.iter_scope_files())
        self.assertEqual(current["src/lingxi/probe.py::probe"], 59)
        self.assertEqual(CHECK.evaluate({}, current), [])

    def test_growing_to_61_lines_is_rejected(self) -> None:
        self._write(60)  # 1 def line + 60 body lines = 61
        current = CHECK.measure(CHECK.iter_scope_files())
        self.assertEqual(current["src/lingxi/probe.py::probe"], 61)
        failures = CHECK.evaluate({}, current)
        self.assertTrue(any("新超过函数体量棘轮阈值" in f for f in failures), failures)


class DuplicateQualifiedNameTakesMaxTest(unittest.TestCase):
    """独立审核实测坐实：property 的 getter/setter 同名不同定义时，此前后一次
    定义会静默覆盖前一次的登记——一个真实超过阈值的 getter 因为后面跟着一个
    3 行的 setter 而在门禁眼里"缩水"。键的形状不变，取这组同名定义里的 max()。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.source_root = root / "src" / "lingxi"
        self.source_root.mkdir(parents=True)

        self._orig_repository_root = CHECK.REPOSITORY_ROOT
        self._orig_source_root = CHECK.SOURCE_ROOT
        self._orig_baseline_path = CHECK.BASELINE_PATH
        CHECK.REPOSITORY_ROOT = root
        CHECK.SOURCE_ROOT = self.source_root
        CHECK.BASELINE_PATH = root / "baseline.txt"
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        CHECK.REPOSITORY_ROOT = self._orig_repository_root
        CHECK.SOURCE_ROOT = self._orig_source_root
        CHECK.BASELINE_PATH = self._orig_baseline_path

    def test_property_getter_100_lines_and_setter_3_lines_registers_the_getter_length(
        self,
    ) -> None:
        getter_body = "\n".join(f"        x{i} = {i}" for i in range(99))  # 100 行 getter
        setter_body = "        self._x = value"  # 3 行 setter
        source = (
            "class Widget:\n"
            "    @property\n"
            "    def value(self):\n"
            f"{getter_body}\n"
            "        return self._x\n"
            "\n"
            "    @value.setter\n"
            "    def value(self, value):\n"
            f"{setter_body}\n"
        )
        path = self.source_root / "widget.py"
        path.write_text(source, encoding="utf-8")

        current = CHECK.measure(CHECK.iter_scope_files())
        self.assertEqual(current["src/lingxi/widget.py::Widget.value"], 101)
        failures = CHECK.evaluate({}, current)
        self.assertTrue(any("新超过函数体量棘轮阈值" in f for f in failures), failures)


if __name__ == "__main__":
    unittest.main()
