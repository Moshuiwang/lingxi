"""``scripts/ci/run_tests.py`` 的分片选择、汇总门禁与结果记录（Issue #712）。

覆盖三件事：①按文件分桶确实是一次**划分**——每个文件整体落进恰好一个分片，
全部分片的并集等于未分片时的完整用例集合，互不重叠；②汇总口径本身是门禁：
任一分片缺汇总行、分片数与预期不符、分片号重复或越界都必须判红（钉住测试，
必做项第 3 条）；③逐条结果记录能区分通过/失败/错误/跳过，并带上失败签名，
不是只有一个 `Ran N` 汇总数字（验收 E-3 ⑤）。
"""

from __future__ import annotations

import importlib
import io
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
    """按文件哈希分桶必须是一次划分：不漏、不重、同文件不拆散。"""

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
            selected = run_tests._select_shard(suite, index, shard_count)
            for case in run_tests._iter_test_cases(selected):
                seen[case.id()] = seen.get(case.id(), 0) + 1

        self.assertEqual(sorted(seen), full_ids, "分片并集必须等于未分片的完整用例集合")
        self.assertTrue(all(count == 1 for count in seen.values()), "每条用例只应落进一个分片")

    def test_same_file_never_splits_across_shards(self) -> None:
        suite = self._build_suite(file_count=10, cases_per_file=5)
        shard_count = 3
        for index in range(shard_count):
            selected = run_tests._select_shard(suite, index, shard_count)
            files = {
                run_tests._case_source_file(case) for case in run_tests._iter_test_cases(selected)
            }
            for file_path in files:
                bucket = run_tests._shard_bucket(file_path, shard_count)
                self.assertEqual(bucket, index, "同一文件的用例必须全部落进它自己的那个分片")

    def test_bucket_is_stable_across_repeated_calls(self) -> None:
        key = "/fake/tests/test_stability.py"
        first = run_tests._shard_bucket(key, 6)
        second = run_tests._shard_bucket(key, 6)
        self.assertEqual(first, second)
        self.assertTrue(0 <= first < 6)


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
