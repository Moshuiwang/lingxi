"""`scripts/ci/check_apps_pure_logic_ratchet.py` 的解析与判定用例（Issue #656）。

跟 ``test_size_ratchet_check.py`` 同一惯例：每个用例先构造一份会违规的输入，断言它被
具体地拒绝，而不是只跑一遍真实基线看它绿——一份只会通过的检查等于没有检查。
"""

from __future__ import annotations

import ast
import importlib.util
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "ci" / "check_apps_pure_logic_ratchet.py"


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "apps_pure_logic_ratchet_check_under_test", SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CHECK = _load_script()


class BaselineParsingTest(unittest.TestCase):
    def test_good_baseline_parses(self) -> None:
        text = (
            "# 注释行\n\n120\tsrc/lingxi/apps/gateway/foo.py\n30\tsrc/lingxi/apps/worker/bar.py\n"
        )
        self.assertEqual(
            CHECK.parse_baseline(text),
            {
                "src/lingxi/apps/gateway/foo.py": 120,
                "src/lingxi/apps/worker/bar.py": 30,
            },
        )

    def test_malformed_row_fails_closed(self) -> None:
        with self.assertRaises(CHECK.BaselineError):
            CHECK.parse_baseline("not-a-number\tsrc/lingxi/apps/x.py\n")

    def test_missing_tab_fails_closed(self) -> None:
        with self.assertRaises(CHECK.BaselineError):
            CHECK.parse_baseline("120 src/lingxi/apps/x.py\n")

    def test_duplicate_path_fails_closed(self) -> None:
        with self.assertRaises(CHECK.BaselineError):
            CHECK.parse_baseline("10\tsrc/lingxi/apps/x.py\n20\tsrc/lingxi/apps/x.py\n")


class EvaluateTest(unittest.TestCase):
    """核心判定逻辑：不依赖磁盘，直接喂 (baseline, current) 字典。"""

    def test_growth_beyond_recorded_ceiling_is_rejected(self) -> None:
        baseline = {"src/lingxi/apps/gateway/foo.py": 100}
        current = {"src/lingxi/apps/gateway/foo.py": 105}
        failures = CHECK.evaluate(baseline, current)
        self.assertTrue(any("超过棘轮基线记录的上限" in f for f in failures), failures)

    def test_shrinking_below_recorded_ceiling_is_rejected_until_refreshed(self) -> None:
        baseline = {"src/lingxi/apps/gateway/foo.py": 100}
        current = {"src/lingxi/apps/gateway/foo.py": 80}
        failures = CHECK.evaluate(baseline, current)
        self.assertTrue(any("与实测" in f and "不一致" in f for f in failures), failures)

    def test_exact_match_passes(self) -> None:
        baseline = {"src/lingxi/apps/gateway/foo.py": 100}
        current = {"src/lingxi/apps/gateway/foo.py": 100}
        self.assertEqual(CHECK.evaluate(baseline, current), [])

    def test_new_unregistered_hit_is_rejected_and_names_the_path(self) -> None:
        baseline: dict[str, int] = {}
        current = {"src/lingxi/apps/gateway/new_judgment.py": 12}
        failures = CHECK.evaluate(baseline, current)
        self.assertTrue(
            any("新命中" in f and "src/lingxi/apps/gateway/new_judgment.py" in f for f in failures),
            failures,
        )

    def test_file_that_never_reaches_current_is_not_a_violation(self) -> None:
        """未达标（<10 行代码或不 import core）的文件根本不会出现在 current 里；
        evaluate() 只看得到已经通过命中门槛的条目。"""

        baseline: dict[str, int] = {}
        current: dict[str, int] = {}
        self.assertEqual(CHECK.evaluate(baseline, current), [])

    def test_stale_registration_for_a_file_no_longer_hitting_fails_and_prompts_refresh(
        self,
    ) -> None:
        baseline = {"src/lingxi/apps/gateway/foo.py": 100}
        current: dict[str, int] = {}
        failures = CHECK.evaluate(baseline, current)
        self.assertTrue(any("已经不再命中" in f and "--refresh" in f for f in failures), failures)

    def test_manually_inflating_the_baseline_without_touching_the_file_is_rejected(self) -> None:
        """自证：试图把基线调大 ⇒ 红。文件实际还是 100 行，基线被手工改成 999。"""

        baseline = {"src/lingxi/apps/gateway/foo.py": 999}
        current = {"src/lingxi/apps/gateway/foo.py": 100}
        failures = CHECK.evaluate(baseline, current)
        self.assertTrue(any("与实测" in f and "不一致" in f for f in failures), failures)


class RenderRoundTripTest(unittest.TestCase):
    def test_render_then_parse_round_trips(self) -> None:
        entries = {"src/lingxi/apps/a.py": 42, "src/lingxi/apps/b/c.py": 17}
        rendered = CHECK.render_baseline(entries)
        self.assertEqual(CHECK.parse_baseline(rendered), entries)


class ImportsCoreDetectionTest(unittest.TestCase):
    """AST 判据本身：只认真实 import 语句，不认字符串/注释里出现的同名文本。"""

    def _tree(self, source: str):
        import ast

        return ast.parse(textwrap.dedent(source))

    def test_absolute_from_import_is_detected(self) -> None:
        tree = self._tree("from lingxi.core.ids import new_id\n")
        self.assertTrue(CHECK._imports_lingxi_core(tree, "src/lingxi/apps/gateway/probe.py"))

    def test_absolute_plain_import_is_detected(self) -> None:
        tree = self._tree("import lingxi.core.ids\n")
        self.assertTrue(CHECK._imports_lingxi_core(tree, "src/lingxi/apps/gateway/probe.py"))

    def test_from_lingxi_import_core_is_detected(self) -> None:
        tree = self._tree("from lingxi import core\n")
        self.assertTrue(CHECK._imports_lingxi_core(tree, "src/lingxi/apps/gateway/probe.py"))

    def test_adapters_import_alone_is_not_core(self) -> None:
        tree = self._tree("from lingxi.adapters.postgres import connect\n")
        self.assertFalse(CHECK._imports_lingxi_core(tree, "src/lingxi/apps/gateway/probe.py"))

    def test_a_string_literal_mentioning_core_is_not_an_import(self) -> None:
        tree = self._tree('MESSAGE = "lingxi.core.ids is not imported here"\n')
        self.assertFalse(CHECK._imports_lingxi_core(tree, "src/lingxi/apps/gateway/probe.py"))

    def test_a_comment_mentioning_core_is_not_an_import(self) -> None:
        tree = self._tree("# from lingxi.core.ids import new_id (commented out)\nx = 1\n")
        self.assertFalse(CHECK._imports_lingxi_core(tree, "src/lingxi/apps/gateway/probe.py"))


class CodeLineCountTest(unittest.TestCase):
    """代码行数：总行数减去空行、独占注释行与开头 docstring span。"""

    def test_module_docstring_and_blank_lines_and_pure_comments_are_excluded(self) -> None:
        import ast

        source = textwrap.dedent(
            '''\
            """一个模块级 docstring。

            跨越三行。
            """

            # 独占一行的注释，不计入代码行数
            x = 1  # 行尾注释所在的整行仍然是代码
            y = 2
            '''
        )
        tree = ast.parse(source)
        # 8 行原文：docstring 3 行 + 空行 1 行 + 纯注释 1 行 + 3 行真代码（含空行 1 行）
        # 逐行核对：1 docstring开始/2 docstring/3 docstring结束/4 空行/5 纯注释/
        # 6 x=1（含行尾注释，仍是代码）/7 y=2 → 代码行数应为 2（第 6、7 行）。
        self.assertEqual(CHECK._code_line_count(source, tree), 2)

    def test_function_and_class_docstrings_are_also_excluded(self) -> None:
        import ast

        source = textwrap.dedent(
            '''\
            def f():
                """函数 docstring。"""
                return 1


            class C:
                """类 docstring。"""

                def method(self):
                    """方法 docstring。"""
                    return 2
            '''
        )
        tree = ast.parse(source)
        # 真代码行：def f():/return 1/class C:/def method(self):/return 2 = 5 行。
        self.assertEqual(CHECK._code_line_count(source, tree), 5)

    def test_non_leading_string_literal_statement_counts_as_code(self) -> None:
        """函数体中间、不在首句位置的裸字符串字面量语句不算 docstring，仍是代码行——
        与 check_comment_ratchet.py 的既有约定一致（只有首句才算 docstring）。"""

        import ast

        source = textwrap.dedent(
            """\
            def f():
                x = 1
                "not a docstring, just a statement"
                return x
            """
        )
        tree = ast.parse(source)
        self.assertEqual(CHECK._code_line_count(source, tree), 4)


class MeasureAndBootstrapTest(unittest.TestCase):
    """磁盘扫描骨架：用临时目录打桩 APPS_ROOT/BASELINE_PATH，不碰真实仓库。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.apps_root = root / "src" / "lingxi" / "apps"
        self.apps_root.mkdir(parents=True)
        self.baseline_path = root / "baseline.txt"

        self._orig_repository_root = CHECK.REPOSITORY_ROOT
        self._orig_apps_root = CHECK.APPS_ROOT
        self._orig_baseline_path = CHECK.BASELINE_PATH
        CHECK.REPOSITORY_ROOT = root
        CHECK.APPS_ROOT = self.apps_root
        CHECK.BASELINE_PATH = self.baseline_path
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        CHECK.REPOSITORY_ROOT = self._orig_repository_root
        CHECK.APPS_ROOT = self._orig_apps_root
        CHECK.BASELINE_PATH = self._orig_baseline_path

    def _write_module(self, relative: str, body: str) -> None:
        path = self.apps_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body), encoding="utf-8")

    def test_measure_skips_files_that_do_not_import_core(self) -> None:
        self._write_module(
            "gateway/pure_wiring.py",
            """\
            import os

            x = 1
            y = 2
            z = 3
            a = 4
            b = 5
            c = 6
            d = 7
            e = 8
            f = 9
            g = 10
            """,
        )
        current = CHECK.measure(CHECK.iter_scope_files())
        self.assertEqual(current, {})

    def test_measure_skips_short_files_even_if_they_import_core(self) -> None:
        self._write_module(
            "gateway/tiny.py",
            """\
            from lingxi.core.ids import new_id

            def make_id():
                return new_id("x")
            """,
        )
        current = CHECK.measure(CHECK.iter_scope_files())
        self.assertEqual(current, {})

    def test_measure_hits_a_file_that_imports_core_with_enough_code_lines(self) -> None:
        self._write_module(
            "gateway/judgment.py",
            """\
            from lingxi.core.ids import new_id

            def line_1():
                return 1

            def line_2():
                return 2

            def line_3():
                return 3

            def line_4():
                return new_id("x")

            def line_5():
                return 5
            """,
        )
        current = CHECK.measure(CHECK.iter_scope_files())
        self.assertIn("src/lingxi/apps/gateway/judgment.py", current)
        self.assertGreaterEqual(
            current["src/lingxi/apps/gateway/judgment.py"], CHECK.MIN_CODE_LINES
        )

    def test_bootstrap_then_check_passes_on_the_same_tree(self) -> None:
        self._write_module(
            "gateway/judgment.py",
            """\
            from lingxi.core.ids import new_id

            def line_1():
                return 1

            def line_2():
                return 2

            def line_3():
                return 3

            def line_4():
                return new_id("x")
            """,
        )
        self.assertEqual(CHECK.run_bootstrap(), 0)
        self.assertEqual(CHECK.run_check(), 0)

    def test_bootstrap_refuses_when_baseline_already_exists(self) -> None:
        self.baseline_path.write_text(CHECK.render_baseline({}), encoding="utf-8")
        self._write_module("gateway/anything.py", "x = 1\n")
        self.assertEqual(CHECK.run_bootstrap(), 1)

    def test_a_new_module_that_only_imports_core_hits_and_moving_it_to_core_clears_it(
        self,
    ) -> None:
        """对应派发卡里指定的变异实测②：apps/ 下新建一个只 import lingxi.core.* 的
        判定模块 → 判红并报出文件名；移到 core/（即移出 APPS_ROOT 扫描范围）→ 转绿。"""

        self.baseline_path.write_text(CHECK.render_baseline({}), encoding="utf-8")
        # 留一个不命中的占位文件，这样把判定模块「移到 core/」（即从 APPS_ROOT
        # 扫描范围里删掉）之后，apps/ 下仍不是空目录——否则 iter_scope_files()
        # 的「扫描范围下一个 .py 都没有」失败关闭分支会掩盖本用例真正要看的判定。
        self._write_module("gateway/__init__.py", "# 占位\n")
        self._write_module(
            "gateway/new_pure_judgment.py",
            """\
            from lingxi.core.ids import new_id

            def decide(a, b, c):
                if a and b:
                    return new_id("x")
                if b and c:
                    return new_id("y")
                if a and c:
                    return new_id("z")
                return None

            def other(a):
                return a * 2
            """,
        )
        exit_code = CHECK.run_check()
        self.assertEqual(exit_code, 1)

        # 报出文件名：捕获 stderr 核对具体路径出现在失败原因里。
        import contextlib
        import io as io_module

        buffer = io_module.StringIO()
        with contextlib.redirect_stderr(buffer):
            CHECK.run_check()
        self.assertIn("src/lingxi/apps/gateway/new_pure_judgment.py", buffer.getvalue())

        # 移到 core/：从 APPS_ROOT 扫描范围里移走这个文件，相当于「移到 core/」。
        (self.apps_root / "gateway" / "new_pure_judgment.py").unlink()
        self.assertEqual(CHECK.run_check(), 0)


if __name__ == "__main__":
    unittest.main()


class RelativeImportIsNotAnEscapeHatchTests(unittest.TestCase):
    """相对 import 不得成为绕过这条判据的口子。

    本仓大量使用 ``from ..core.x import y``。判据若只认绝对模块名，任何人把
    import 形态换一下就能让一个纯业务判定模块合法地留在 apps/——独立审查用探针
    实测过两种形态都能绕过：普通模块的三级相对 import，以及包 ``__init__.py``
    里的相对 import（后者更隐蔽，因为包的解析基准是它自己，按父包解会少一级）。
    """

    def _tree(self, source: str) -> ast.Module:
        return ast.parse(source)

    def test_relative_import_from_a_plain_module_counts(self) -> None:
        tree = self._tree("from ...core.admin.commands import AdminCommandKind\n")
        self.assertTrue(
            CHECK._imports_lingxi_core(tree, "src/lingxi/apps/gateway/judgment.py"),
            "apps/gateway/judgment.py 里的 `from ...core...` 解出来就是 lingxi.core.*",
        )

    def test_relative_import_inside_a_package_init_counts(self) -> None:
        tree = self._tree("from ...core.admin.commands import AdminCommandKind\n")
        self.assertTrue(
            CHECK._imports_lingxi_core(tree, "src/lingxi/apps/zz_pkg/__init__.py"),
            "包 __init__.py 的解析基准是包自己；按父包解会少一级、漏判",
        )

    def test_relative_import_pointing_elsewhere_is_not_counted(self) -> None:
        tree = self._tree("from ...adapters.postgres import connect\n")
        self.assertFalse(
            CHECK._imports_lingxi_core(tree, "src/lingxi/apps/gateway/judgment.py"),
            "指向 adapters 的相对 import 不算命中——判宽了会把装配件全卷进来",
        )

    def test_relative_import_beyond_the_package_root_is_not_counted(self) -> None:
        tree = self._tree("from ......core.x import y\n")
        self.assertFalse(
            CHECK._imports_lingxi_core(tree, "src/lingxi/apps/gateway/judgment.py"),
            "越过包根的写法在真 Python 里本就 ImportError，按不可能命中处理",
        )
