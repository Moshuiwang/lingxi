"""``scripts/ci/run_tests.py`` 的分片选择、汇总门禁与结果记录（Issue #712）。

覆盖五件事：①按文件分桶确实是一次**划分**——每个文件整体落进恰好一个分片，
全部分片的并集等于未分片时的完整用例集合，互不重叠；②贪心装箱按耗时清单把
文件分给总耗时最小的分片，装箱结果确定、与调用顺序无关，清单缺条目的文件按
平均耗时兜底、仍会被分进某个分片而不是被跳过（#712 调粒度批，替换此前按文件
名哈希取模的分桶算法）；③耗时清单的读取容错——文件不存在、内容损坏都退回空
字典而不是报错中断；④汇总口径本身是门禁：任一分片缺汇总行、分片数与预期不
符、分片号重复或越界都必须判红（钉住测试，必做项第 3 条）；⑤逐条结果记录能
区分通过/失败/错误/跳过，并带上失败签名，不是只有一个 `Ran N` 汇总数字（验收
E-3 ⑤）。
"""

from __future__ import annotations

import importlib
import io
import json
import os
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/ci"))
run_tests = importlib.import_module("run_tests")


def _build_fake_module_case_class(module_name: str, file_path: str) -> type[unittest.TestCase]:
    """造一个"住在指定文件里"的 TestCase 类，供分片分桶测试挂测试方法用。

    真实场景里一个文件对应一个已导入的模块，`__file__` 指向磁盘路径；这里用
    `types.ModuleType` 现造模块并塞进 `sys.modules`，让 `_case_source_file`
    能查到同一个伪造路径——不用真的在磁盘上建文件。
    """

    module = types.ModuleType(module_name)
    module.__file__ = file_path
    sys.modules[module_name] = module
    case_class = type("Case", (unittest.TestCase,), {})
    case_class.__module__ = module_name
    module.Case = case_class
    return case_class


class ShardPartitionTests(unittest.TestCase):
    """按文件装箱分桶必须是一次划分：不漏、不重、同文件不拆散。"""

    def setUp(self) -> None:
        self._registered_modules: list[str] = []
        self.addCleanup(self._unregister_fake_modules)

    def _unregister_fake_modules(self) -> None:
        for name in self._registered_modules:
            sys.modules.pop(name, None)

    def _build_suite(self, file_count: int, cases_per_file: int) -> unittest.TestSuite:
        suite = unittest.TestSuite()
        for file_index in range(file_count):
            module_name = f"lingxi_shard_gate_fixture_{file_index}"
            file_path = f"/fake/tests/test_fixture_{file_index}.py"
            self._registered_modules.append(module_name)
            case_class = _build_fake_module_case_class(module_name, file_path)
            for case_index in range(cases_per_file):
                method_name = f"test_{case_index}"
                setattr(case_class, method_name, lambda self: None)
                suite.addTest(case_class(method_name))
        return suite

    def test_union_of_shards_recovers_full_set_exactly_once(self) -> None:
        suite = self._build_suite(file_count=23, cases_per_file=3)
        full_ids = sorted(case.id() for case in run_tests._iter_test_cases(suite))

        shard_count = 4
        seen: dict[str, int] = {}
        for index in range(shard_count):
            selected = run_tests._select_shard(suite, index, shard_count, manifest={})
            for case in run_tests._iter_test_cases(selected):
                seen[case.id()] = seen.get(case.id(), 0) + 1

        self.assertEqual(sorted(seen), full_ids, "分片并集必须等于未分片的完整用例集合")
        self.assertTrue(all(count == 1 for count in seen.values()), "每条用例只应落进一个分片")

    def test_same_file_never_splits_across_shards(self) -> None:
        suite = self._build_suite(file_count=10, cases_per_file=5)
        shard_count = 3
        cases = list(run_tests._iter_test_cases(suite))
        file_keys = sorted({run_tests._manifest_key(run_tests._case_source_file(c)) for c in cases})
        assignment = run_tests._assign_files_to_shards(file_keys, shard_count, {})
        for index in range(shard_count):
            selected = run_tests._select_shard(suite, index, shard_count, manifest={})
            files = {
                run_tests._manifest_key(run_tests._case_source_file(case))
                for case in run_tests._iter_test_cases(selected)
            }
            for file_key in files:
                self.assertEqual(
                    assignment[file_key], index, "同一文件的用例必须全部落进它自己的那个分片"
                )

    def test_assignment_is_deterministic_across_repeated_calls(self) -> None:
        file_keys = [f"tests/test_stability_{i}.py" for i in range(9)]
        manifest = {key: float(index + 1) for index, key in enumerate(file_keys)}
        first = run_tests._assign_files_to_shards(file_keys, 4, manifest)
        second = run_tests._assign_files_to_shards(list(reversed(file_keys)), 4, manifest)
        self.assertEqual(first, second, "装箱结果只应取决于文件与清单内容，与传入顺序无关")
        self.assertTrue(all(0 <= index < 4 for index in first.values()))

    def test_greedy_packing_keeps_bucket_totals_close(self) -> None:
        # 必做项第 4 条钉住①：装箱后各桶耗时估计的极差不超过阈值。用一份耗时
        # 悬殊的合成清单（1 个大户 + 6 个中户 + 8 个小户）钉住贪心装箱的质量：
        # 极差不应超过单个最长文件的耗时——按文件名哈希、或不看耗时的轮流分配
        # 都做不到这一点（哈希可能把大户和中户分进同一片，轮流分配则完全不看
        # 耗时，两者都可能让极差远超过一个最长文件的量级）。
        weights = [
            100.0,
            40.0,
            38.0,
            36.0,
            34.0,
            32.0,
            30.0,
            5.0,
            5.0,
            5.0,
            5.0,
            5.0,
            5.0,
            5.0,
            5.0,
        ]
        manifest = {f"tests/test_synthetic_{i}.py": weight for i, weight in enumerate(weights)}
        file_keys = list(manifest)

        assignment = run_tests._assign_files_to_shards(file_keys, shard_count=4, manifest=manifest)
        bucket_totals = [0.0, 0.0, 0.0, 0.0]
        for key, shard_index in assignment.items():
            bucket_totals[shard_index] += manifest[key]

        spread = max(bucket_totals) - min(bucket_totals)
        threshold = max(weights)
        self.assertLessEqual(
            spread,
            threshold,
            f"装箱后各桶总耗时极差 {spread} 超过了单个最长文件的耗时 {threshold}：{bucket_totals}",
        )

    def test_files_missing_from_manifest_still_get_assigned(self) -> None:
        # 必做项第 4 条钉住②：清单缺条目的文件仍会被分到某个桶，不会被跳过。
        manifest = {"tests/test_known_a.py": 12.0, "tests/test_known_b.py": 8.0}
        file_keys = [
            "tests/test_known_a.py",
            "tests/test_known_b.py",
            "tests/test_brand_new_one.py",
            "tests/test_brand_new_two.py",
        ]
        assignment = run_tests._assign_files_to_shards(file_keys, shard_count=3, manifest=manifest)

        self.assertEqual(set(assignment), set(file_keys), "清单里没有的文件也必须出现在分配结果里")
        for file_key in file_keys:
            self.assertIn(assignment[file_key], range(3), f"{file_key} 的分片号必须落在合法范围内")


class DurationManifestTests(unittest.TestCase):
    """耗时清单的读取、默认权重兜底与相对路径换算（支撑必做项第 1 条）。"""

    def test_missing_file_returns_empty_manifest(self) -> None:
        with TemporaryDirectory() as workdir:
            missing = Path(workdir) / "absent.json"
            self.assertEqual(run_tests._load_duration_manifest(missing), {})

    def test_corrupt_json_returns_empty_manifest(self) -> None:
        with TemporaryDirectory() as workdir:
            path = Path(workdir) / "corrupt.json"
            path.write_text("{not valid json", encoding="utf-8")
            self.assertEqual(run_tests._load_duration_manifest(path), {})

    def test_non_object_json_returns_empty_manifest(self) -> None:
        with TemporaryDirectory() as workdir:
            path = Path(workdir) / "list.json"
            path.write_text("[1, 2, 3]", encoding="utf-8")
            self.assertEqual(run_tests._load_duration_manifest(path), {})

    def test_valid_manifest_round_trips_numeric_values(self) -> None:
        with TemporaryDirectory() as workdir:
            path = Path(workdir) / "durations.json"
            path.write_text(
                json.dumps({"tests/test_a.py": 12.5, "tests/test_b.py": 3}), encoding="utf-8"
            )
            manifest = run_tests._load_duration_manifest(path)
            self.assertEqual(manifest, {"tests/test_a.py": 12.5, "tests/test_b.py": 3.0})

    def test_default_weight_is_manifest_mean_when_nonempty(self) -> None:
        manifest = {"a": 2.0, "b": 4.0, "c": 9.0}
        self.assertAlmostEqual(run_tests._default_weight_seconds(manifest), 5.0)

    def test_default_weight_falls_back_when_manifest_empty(self) -> None:
        self.assertEqual(
            run_tests._default_weight_seconds({}), run_tests._DEFAULT_WEIGHT_SECONDS_FALLBACK
        )

    def test_manifest_key_is_repository_relative_posix_path(self) -> None:
        absolute = str(run_tests.REPOSITORY_ROOT / "tests" / "test_example.py")
        self.assertEqual(run_tests._manifest_key(absolute), "tests/test_example.py")

    def test_manifest_key_falls_back_for_non_path_case_id(self) -> None:
        case_id = "unittest.loader._FailedTest.some_broken_module"
        self.assertEqual(run_tests._manifest_key(case_id), case_id)

    def test_manifest_key_falls_back_for_path_outside_repository(self) -> None:
        outside = "/definitely/outside/repo/test_x.py"
        self.assertEqual(run_tests._manifest_key(outside), outside)


class NoOptInEnvironmentBehaviorTests(unittest.TestCase):
    """不设任何 `LINGXI_TEST_*` 环境变量时，`_run()` 必须与改造前逐字相同。

    这是本次改造能否合并的前提：新增的耗时清单、贪心装箱、耗时记录全部必须是
    纯粹的 opt-in，默认路径（CI 与本机大多数调用）不能有任何新副作用——不写
    耗时文件、不打印分片汇总行、照常发现并跑完全部用例。用一个只有一条用例的
    临时目录顶替真正的 `tests/`，让这条钉住测试本身跑得够快、不依赖真库。
    """

    def _write_trivial_fixture(self, directory: Path) -> None:
        (directory / "test_fixture_ok.py").write_text(
            "import unittest\n\n\n"
            "class FixtureOkTests(unittest.TestCase):\n"
            "    def test_pass(self) -> None:\n"
            "        self.assertTrue(True)\n",
            encoding="utf-8",
        )

    def test_run_without_any_env_var_matches_pre_change_behavior(self) -> None:
        opt_in_vars = (
            "LINGXI_TEST_SHARD_INDEX",
            "LINGXI_TEST_SHARD_COUNT",
            "LINGXI_TEST_DURATIONS_DIR",
            "LINGXI_TEST_REPORT_PATH",
        )
        with TemporaryDirectory() as workdir:
            fixture_dir = Path(workdir) / "fixture_tests"
            fixture_dir.mkdir()
            self._write_trivial_fixture(fixture_dir)

            with (
                mock.patch.object(run_tests, "TESTS_DIRECTORY", fixture_dir),
                mock.patch.dict(os.environ, {}, clear=False),
            ):
                for key in opt_in_vars:
                    os.environ.pop(key, None)
                captured_stderr = io.StringIO()
                with mock.patch("sys.stderr", captured_stderr):
                    exit_code = run_tests._run()

            self.assertEqual(exit_code, 0, "没有环境变量时应跑通全部用例，返回码为 0")
            self.assertNotIn(
                run_tests._SHARD_SUMMARY_PREFIX,
                captured_stderr.getvalue(),
                "不分片时不应该打印分片汇总行——这是改造前就有的行为，不应该被新代码改变",
            )
            # 没有 LINGXI_TEST_DURATIONS_DIR 时，装箱/清单相关的新代码路径必须完全
            # 不触碰文件系统：不该多出任何 durations-*.json（`__pycache__` 是
            # Python 导入 fixture 模块自己产生的字节码缓存，与本次改造无关）。
            self.assertEqual(list(fixture_dir.glob("durations-*.json")), [])

    def test_duration_output_path_is_none_without_the_opt_in_env_var(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LINGXI_TEST_DURATIONS_DIR", None)
            self.assertIsNone(run_tests._duration_output_path(None))
            self.assertIsNone(run_tests._duration_output_path((0, 2)))

    def test_run_without_env_var_actually_runs_cases_and_reports_failure(self) -> None:
        """二审 P1：上一条只断言「退出码为 0、没有多余输出」，三处变异能存活。

        独立审查实测存活的三处：把 `_run()` 改成恒 `return 0`、把 discover 的
        pattern 改成匹配不到任何文件（跑 0 条仍返回 0）、把 verbosity 调成 0。
        前两处正是假绿形态——「什么都没跑」和「跑了但结论被吞掉」在只看退出码
        的断言下与「全部通过」不可区分。本条补两件事：**真的跑到了用例**，
        以及**用例失败时真的返回 1**。
        """

        opt_in_vars = (
            "LINGXI_TEST_SHARD_INDEX",
            "LINGXI_TEST_SHARD_COUNT",
            "LINGXI_TEST_DURATIONS_DIR",
            "LINGXI_TEST_REPORT_PATH",
        )

        def run_against(fixture_dir: Path) -> tuple[int, str]:
            # unittest 的 discover 按模块名缓存：本文件里另一条用例也写过同名的
            # 夹具模块，两者的临时目录不同，第二次 discover 会直接 ImportError
            # （"module incorrectly imported from ..."）。这里的模块名刻意与那条
            # 不同，并在每次跑之前把可能的残留清掉。
            for stale in [m for m in sys.modules if m.startswith("test_premise_")]:
                sys.modules.pop(stale, None)
            with (
                mock.patch.object(run_tests, "TESTS_DIRECTORY", fixture_dir),
                mock.patch.dict(os.environ, {}, clear=False),
            ):
                for key in opt_in_vars:
                    os.environ.pop(key, None)
                captured = io.StringIO()
                with mock.patch("sys.stderr", captured):
                    code = run_tests._run()
            return code, captured.getvalue()

        with TemporaryDirectory() as workdir:
            passing_dir = Path(workdir) / "passing_tests"
            passing_dir.mkdir()
            # 独立复核 P2-6：夹具只有一条用例时，`Ran [1-9]\d*` 只能证明「至少跑了
            # 一条」。实测把发现结果截断成只保留第一条，真实仓库 6563 条只跑 1 条，
            # 门禁照常打印「通过」而钉住测试全绿。三条用例 + 精确匹配才挡得住截断。
            (passing_dir / "test_premise_ok.py").write_text(
                "import unittest\n\n\n"
                "class PremiseOkTests(unittest.TestCase):\n"
                "    def test_premise_pass(self) -> None:\n"
                "        self.assertTrue(True)\n\n"
                "    def test_premise_pass_second(self) -> None:\n"
                "        self.assertTrue(True)\n\n"
                "    def test_premise_pass_third(self) -> None:\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            code, output = run_against(passing_dir)

            self.assertEqual(code, 0)
            # 「真的跑到了用例」：unittest 的汇总行形如 `Ran 1 test in ...`。
            # 恒返回 0 或 discover 匹配不到文件时，这一行会是 `Ran 0 tests`。
            self.assertRegex(
                output,
                r"Ran 3 tests",
                "默认路径必须把夹具里三条用例全部跑到——`Ran 0` 是空跑、"
                "`Ran 1` 是发现结果被截断，两者在只看退出码时都伪装成全绿",
            )
            self.assertIn(
                "test_premise_pass",
                output,
                "verbosity 被调低会让逐条用例名消失，那是改造前没有的行为差异",
            )

        with TemporaryDirectory() as workdir:
            failing_dir = Path(workdir) / "failing_tests"
            failing_dir.mkdir()
            # 独立复核 P2-7：只有 self.fail()（failure 一类）时，把返回码判定改成
            # 只看 failures 就能静默吞掉 error 一类。两类各放一条才挡得住。
            (failing_dir / "test_premise_fail.py").write_text(
                "import unittest\n\n\n"
                "class PremiseFailTests(unittest.TestCase):\n"
                "    def test_premise_fail(self) -> None:\n"
                "        self.fail('钉住：失败必须传播成非零退出码')\n\n"
                "    def test_premise_error(self) -> None:\n"
                "        raise ValueError('钉住：抛异常同样必须传播成非零退出码')\n",
                encoding="utf-8",
            )
            code, output = run_against(failing_dir)

            self.assertEqual(code, 1, "用例失败时默认路径必须返回 1——吞掉失败是本条要挡的假绿")
            self.assertRegex(output, r"Ran 2 tests")
            self.assertIn("failures=1", output)
            self.assertIn("errors=1", output)

        # 上面那个夹具里 failure 与 error 各一条，于是「只看 failures」的吞法照样
        # 返回 1，抓不住——独立复核实测该变异存活。这里再要一个**只有 error**的
        # 场景：把返回码判定改成只看 failures，这一段立刻判红。
        with TemporaryDirectory() as workdir:
            error_only_dir = Path(workdir) / "error_only_tests"
            error_only_dir.mkdir()
            (error_only_dir / "test_premise_error_only.py").write_text(
                "import unittest\n\n\n"
                "class PremiseErrorOnlyTests(unittest.TestCase):\n"
                "    def test_premise_raises(self) -> None:\n"
                "        raise ValueError('钉住：只有 error 时同样必须返回 1')\n",
                encoding="utf-8",
            )
            code, output = run_against(error_only_dir)

            self.assertEqual(
                code,
                1,
                "只有 error、没有 failure 时也必须返回 1——「只看 failures」是本条要挡的吞法",
            )
            self.assertIn("errors=1", output)
            self.assertNotIn("failures=1", output)

    def test_default_path_keeps_unittest_cli_warning_behaviour(self) -> None:
        """独立复核 P2-8：`warnings="default"` 这个修复本身没有被钉住。

        `python -m unittest` 经 TestProgram 传 `warnings='default'`；
        `TextTestRunner` 不传时默认是 None，于是 DeprecationWarning /
        ResourceWarning 在门禁日志里彻底消失。退出码不受影响，所以它是一处
        **静默的**判据强度下降——正因为静默，一次「清理多余参数」的改动就能
        把它退回去而无人察觉。这条直接钉住那个参数的效果，不是钉住它的写法。
        """

        opt_in_vars = (
            "LINGXI_TEST_SHARD_INDEX",
            "LINGXI_TEST_SHARD_COUNT",
            "LINGXI_TEST_DURATIONS_DIR",
            "LINGXI_TEST_REPORT_PATH",
        )
        with TemporaryDirectory() as workdir:
            fixture_dir = Path(workdir) / "warning_tests"
            fixture_dir.mkdir()
            (fixture_dir / "test_premise_warn.py").write_text(
                "import unittest\n"
                "import warnings\n\n\n"
                "class PremiseWarnTests(unittest.TestCase):\n"
                "    def test_emits_deprecation_warning(self) -> None:\n"
                "        warnings.warn('钉住：默认路径必须照常打印这条', DeprecationWarning)\n",
                encoding="utf-8",
            )
            for stale in [m for m in sys.modules if m.startswith("test_premise_")]:
                sys.modules.pop(stale, None)
            recorded: dict[str, object] = {}
            real_runner = run_tests.unittest.TextTestRunner

            def spy(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
                recorded.update(kwargs)
                return real_runner(*args, **kwargs)

            with (
                mock.patch.object(run_tests, "TESTS_DIRECTORY", fixture_dir),
                mock.patch.object(run_tests.unittest, "TextTestRunner", spy),
                mock.patch.dict(os.environ, {}, clear=False),
            ):
                for key in opt_in_vars:
                    os.environ.pop(key, None)
                captured = io.StringIO()
                with mock.patch("sys.stderr", captured):
                    code = run_tests._run()
            output = captured.getvalue()

        self.assertEqual(code, 0, output)
        # 本来直接断言输出里有 DeprecationWarning，但**那条断言在这个跑法下没有
        # 区分力**：外层是 `python -m unittest` 起的，TestProgram 已把 warnings
        # 过滤全局设成 default，内层 _run() 继承了它——去掉参数照样打印。独立复核
        # 实测该变异存活，因此改成直接钉住传给 runner 的那个参数本身。
        self.assertEqual(
            recorded.get("warnings"),
            "default",
            "TextTestRunner 必须显式收到 warnings='default'，与 `python -m unittest` 一致；"
            "不传时弃用告警会在门禁日志里静默消失",
        )
        self.assertIn(
            "DeprecationWarning",
            output,
            "默认路径必须与 `python -m unittest` 一样打印弃用告警；"
            "少打印的告警同样是可观察行为差异",
        )


class ShardEnvironmentTests(unittest.TestCase):
    """两个分片环境变量必须同时给出或同时不给，且值必须落在合法范围。"""

    def test_neither_set_means_no_sharding(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LINGXI_TEST_SHARD_INDEX", None)
            os.environ.pop("LINGXI_TEST_SHARD_COUNT", None)
            self.assertIsNone(run_tests._read_shard_env())

    def test_only_one_set_is_rejected(self) -> None:
        with mock.patch.dict(os.environ, {"LINGXI_TEST_SHARD_INDEX": "0"}, clear=False):
            os.environ.pop("LINGXI_TEST_SHARD_COUNT", None)
            with self.assertRaises(run_tests.ShardEnvironmentError):
                run_tests._read_shard_env()

    def test_index_out_of_range_is_rejected(self) -> None:
        env = {"LINGXI_TEST_SHARD_INDEX": "4", "LINGXI_TEST_SHARD_COUNT": "4"}
        with mock.patch.dict(os.environ, env, clear=False):
            with self.assertRaises(run_tests.ShardEnvironmentError):
                run_tests._read_shard_env()

    def test_non_integer_value_is_rejected(self) -> None:
        env = {"LINGXI_TEST_SHARD_INDEX": "x", "LINGXI_TEST_SHARD_COUNT": "4"}
        with mock.patch.dict(os.environ, env, clear=False):
            with self.assertRaises(run_tests.ShardEnvironmentError):
                run_tests._read_shard_env()

    def test_valid_pair_round_trips(self) -> None:
        env = {"LINGXI_TEST_SHARD_INDEX": "2", "LINGXI_TEST_SHARD_COUNT": "4"}
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertEqual(run_tests._read_shard_env(), (2, 4))


class ShardLogAggregationGateTests(unittest.TestCase):
    """汇总口径本身是门禁：这是必做项第 3 条要求的钉住测试。"""

    def _write_log(self, directory: Path, name: str, body: str) -> Path:
        path = directory / name
        path.write_text(body, encoding="utf-8")
        return path

    def test_all_shards_present_is_clean(self) -> None:
        with TemporaryDirectory() as workdir:
            directory = Path(workdir)
            logs = [
                self._write_log(
                    directory,
                    f"shard-{i}.log",
                    f"noise\nRUN_TESTS_SHARD_SUMMARY index={i} count=3 ran=7 ok=true\n",
                )
                for i in range(3)
            ]
            self.assertEqual(run_tests.verify_shard_logs(logs, 3), [])

    def test_missing_summary_line_is_caught(self) -> None:
        with TemporaryDirectory() as workdir:
            directory = Path(workdir)
            logs = [
                self._write_log(directory, "shard-0.log", "只有噪声，没有汇总行\n"),
                self._write_log(
                    directory,
                    "shard-1.log",
                    "RUN_TESTS_SHARD_SUMMARY index=1 count=2 ran=5 ok=true\n",
                ),
            ]
            problems = run_tests.verify_shard_logs(logs, 2)
            self.assertTrue(any("没有找到" in problem for problem in problems))
            self.assertTrue(any("分片 0 没有任何日志" in problem for problem in problems))

    def test_shard_count_mismatch_is_caught(self) -> None:
        with TemporaryDirectory() as workdir:
            directory = Path(workdir)
            logs = [
                self._write_log(
                    directory,
                    f"shard-{i}.log",
                    f"RUN_TESTS_SHARD_SUMMARY index={i} count=4 ran=1 ok=true\n",
                )
                for i in range(3)
            ]
            problems = run_tests.verify_shard_logs(logs, 4)
            self.assertTrue(any("与期望的分片数" in problem for problem in problems))
            self.assertTrue(any("分片 3 没有任何日志" in problem for problem in problems))

    def test_duplicate_index_is_caught(self) -> None:
        with TemporaryDirectory() as workdir:
            directory = Path(workdir)
            logs = [
                self._write_log(
                    directory,
                    "shard-0a.log",
                    "RUN_TESTS_SHARD_SUMMARY index=0 count=2 ran=3 ok=true\n",
                ),
                self._write_log(
                    directory,
                    "shard-0b.log",
                    "RUN_TESTS_SHARD_SUMMARY index=0 count=2 ran=3 ok=true\n",
                ),
            ]
            problems = run_tests.verify_shard_logs(logs, 2)
            self.assertTrue(any("出现了 2 次" in problem for problem in problems))
            self.assertTrue(any("分片 1 没有任何日志" in problem for problem in problems))

    def test_a_failed_shard_process_still_reports_its_summary(self) -> None:
        # 分片跑批失败（ok=false）不等于"没跑完"：汇总行仍然存在，门禁靠退出码
        # 判红，这条只确认核对逻辑不会把"分片红"误判成"分片缺席"。
        with TemporaryDirectory() as workdir:
            directory = Path(workdir)
            logs = [
                self._write_log(
                    directory,
                    "shard-0.log",
                    "RUN_TESTS_SHARD_SUMMARY index=0 count=1 ran=9 ok=false\n",
                )
            ]
            self.assertEqual(run_tests.verify_shard_logs(logs, 1), [])


class OutcomeRecordTests(unittest.TestCase):
    """逐条结论必须能区分通过/失败/错误/跳过，且失败带签名（验收 E-3 ⑤）。"""

    def test_records_cover_every_outcome_kind(self) -> None:
        # 故意失败/报错的用例类定义在方法内部，不放到模块顶层：放到顶层会被
        # `unittest discover` 当成真正的测试收集并执行，把这条钉住测试自己的
        # 门禁跑红——它们的失败是本用例伪造的输入，不是分片机制真的坏了。
        class _Fixture(unittest.TestCase):
            def test_pass(self) -> None:
                pass

            def test_fail(self) -> None:
                self.fail("故意失败")

            def test_error(self) -> None:
                raise ValueError("故意抛错")

            @unittest.skip("故意跳过")
            def test_skip(self) -> None:
                pass

        suite = unittest.TestLoader().loadTestsFromTestCase(_Fixture)
        all_ids = [case.id() for case in run_tests._iter_test_cases(suite)]
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)

        records = {record["id"]: record for record in run_tests._outcome_records(all_ids, result)}
        prefix = f"{_Fixture.__module__}.{_Fixture.__qualname__}"

        self.assertEqual(records[f"{prefix}.test_pass"]["outcome"], "pass")
        fail_record = records[f"{prefix}.test_fail"]
        self.assertEqual(fail_record["outcome"], "fail")
        self.assertIn("故意失败", fail_record["signature"])
        error_record = records[f"{prefix}.test_error"]
        self.assertEqual(error_record["outcome"], "error")
        self.assertIn("ValueError", error_record["signature"])
        skip_record = records[f"{prefix}.test_skip"]
        self.assertEqual(skip_record["outcome"], "skip")
        self.assertEqual(skip_record["signature"], "故意跳过")


if __name__ == "__main__":
    unittest.main()
