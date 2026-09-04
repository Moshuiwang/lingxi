"""``scripts/ops/monitoring/_resource_sample.py`` 的纯逻辑单测（S-RC20-410，
Issue #410）。

只测不依赖真实 `docker`/宿主 `/proc` 具体取值的部分：docker stats 原始输出
解析、单位换算、增速差分、样本落盘的文件命名与状态持久化。`read_load_avg`/
`read_mem_info`/`read_disk_usage`/`read_net_totals` 这类直接读宿主系统状态的
函数只做"能跑、形状合理"的冒烟检查（与 host_health_alert.py 里同类读取函数的
测试深度一致）——真实取值本身没有可断言的期望，属于 L4a 才能验证的范围。

加载方式沿用既有先例（`tests/test_host_health_alert_script.py`）：
``scripts/ops/monitoring/`` 不是一个包，用
``importlib.util.spec_from_file_location`` 按路径直接装载模块。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "scripts" / "ops" / "monitoring" / "_resource_sample.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("resource_sample_helper_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


resource_sample = _load_module()


class SizeParsingTests(unittest.TestCase):
    def test_binary_units(self) -> None:
        self.assertAlmostEqual(resource_sample._parse_size_to_bytes("1KiB"), 1024)
        self.assertAlmostEqual(resource_sample._parse_size_to_bytes("2MiB"), 2 * 1024**2)
        self.assertAlmostEqual(resource_sample._parse_size_to_bytes("1.5GiB"), 1.5 * 1024**3)

    def test_decimal_units(self) -> None:
        self.assertAlmostEqual(resource_sample._parse_size_to_bytes("1kB"), 1000)
        self.assertAlmostEqual(resource_sample._parse_size_to_bytes("2MB"), 2_000_000)

    def test_bare_bytes(self) -> None:
        self.assertAlmostEqual(resource_sample._parse_size_to_bytes("512B"), 512)

    def test_malformed_returns_none(self) -> None:
        self.assertIsNone(resource_sample._parse_size_to_bytes("not-a-size"))

    def test_slash_pair(self) -> None:
        used, limit = resource_sample._parse_slash_pair("12.3MiB / 512MiB")
        self.assertAlmostEqual(used, 12.3 * 1024**2)
        self.assertAlmostEqual(limit, 512 * 1024**2)

    def test_slash_pair_missing_slash_returns_none_pair(self) -> None:
        self.assertEqual(resource_sample._parse_slash_pair("garbage"), (None, None))

    def test_percent_parsing(self) -> None:
        self.assertAlmostEqual(resource_sample._parse_percent("12.34%"), 12.34)
        self.assertIsNone(resource_sample._parse_percent(None))
        self.assertIsNone(resource_sample._parse_percent("n/a"))


class NormalizeContainerStatTests(unittest.TestCase):
    def test_full_shape(self) -> None:
        raw = {
            "Name": "lingxi-scheduler-1",
            "CPUPerc": "1.23%",
            "MemUsage": "12.3MiB / 512MiB",
            "MemPerc": "2.40%",
            "NetIO": "1kB / 2kB",
            "BlockIO": "3MB / 4MB",
            "PIDs": "7",
        }
        normalized = resource_sample.normalize_container_stat(raw)
        self.assertEqual(normalized["name"], "lingxi-scheduler-1")
        self.assertAlmostEqual(normalized["cpu_percent"], 1.23)
        self.assertAlmostEqual(normalized["mem_percent"], 2.40)
        self.assertEqual(normalized["pids"], 7)
        self.assertAlmostEqual(normalized["net_rx_bytes"], 1000)
        self.assertAlmostEqual(normalized["net_tx_bytes"], 2000)

    def test_malformed_pids_becomes_none(self) -> None:
        normalized = resource_sample.normalize_container_stat({"Name": "x", "PIDs": "not-a-number"})
        self.assertIsNone(normalized["pids"])


class ParseDockerStatsLinesTests(unittest.TestCase):
    def test_multiple_containers_one_per_line(self) -> None:
        text = (
            json.dumps({"Name": "c1", "PIDs": "1"})
            + "\n"
            + json.dumps({"Name": "c2", "PIDs": "2"})
            + "\n"
        )
        containers = resource_sample.parse_docker_stats_lines(text)
        self.assertEqual([c["name"] for c in containers], ["c1", "c2"])

    def test_malformed_line_is_skipped_not_fatal(self) -> None:
        text = "not-json\n" + json.dumps({"Name": "c1"}) + "\n"
        containers = resource_sample.parse_docker_stats_lines(text)
        self.assertEqual([c["name"] for c in containers], ["c1"])

    def test_empty_text_returns_empty_list(self) -> None:
        self.assertEqual(resource_sample.parse_docker_stats_lines(""), [])


class ReadMissingNamesTests(unittest.TestCase):
    def test_strips_blank_lines(self) -> None:
        self.assertEqual(resource_sample.read_missing_names("a\n\nb\n"), ["a", "b"])


class ComputeRateTests(unittest.TestCase):
    def test_normal_delta(self) -> None:
        rate = resource_sample.compute_rate(200.0, 100.0, prev_ts=1000.0, now_ts=1010.0)
        self.assertAlmostEqual(rate, 10.0)

    def test_missing_previous_returns_none(self) -> None:
        self.assertIsNone(resource_sample.compute_rate(200.0, None, prev_ts=None, now_ts=1010.0))

    def test_zero_or_negative_elapsed_returns_none(self) -> None:
        self.assertIsNone(resource_sample.compute_rate(200.0, 100.0, prev_ts=1010.0, now_ts=1010.0))
        self.assertIsNone(resource_sample.compute_rate(200.0, 100.0, prev_ts=1020.0, now_ts=1010.0))


class BuildSampleTests(unittest.TestCase):
    def test_first_sample_has_no_rates(self) -> None:
        now = datetime(2026, 8, 29, 10, 0, 0, tzinfo=UTC)
        sample, next_state = resource_sample.build_sample(
            docker_stats_text=json.dumps({"Name": "c1", "PIDs": "1"}) + "\n",
            missing_text="c2\n",
            disk_mounts=["/"],
            prev_state={},
            now=now,
        )
        self.assertEqual(sample["ts"], "2026-08-29T10:00:00Z")
        self.assertEqual(sample["layer"], "resource")
        self.assertEqual(sample["metrics"]["containers"][0]["name"], "c1")
        self.assertEqual(sample["metrics"]["containers_unavailable"], ["c2"])
        for disk in sample["metrics"]["disk"]:
            self.assertIsNone(disk["growth_bytes_per_sec"])
        self.assertIsNone(sample["metrics"]["net"]["rx_bytes_per_sec"])
        self.assertIn("ts", next_state)
        self.assertIn("disks", next_state)

    def test_second_sample_computes_growth_from_state(self) -> None:
        now1 = datetime(2026, 8, 29, 10, 0, 0, tzinfo=UTC)
        _, state1 = resource_sample.build_sample(
            docker_stats_text="", missing_text="", disk_mounts=["/"], prev_state={}, now=now1
        )
        now2 = datetime(2026, 8, 29, 10, 1, 0, tzinfo=UTC)
        sample2, _ = resource_sample.build_sample(
            docker_stats_text="", missing_text="", disk_mounts=["/"], prev_state=state1, now=now2
        )
        disk = sample2["metrics"]["disk"][0]
        # 60 秒内磁盘用量几乎不变（真实宿主），增速应该是一个很小的有限数值，
        # 不是 None——关键断言是"算出来了"，不是具体数值。
        self.assertIsNotNone(disk["growth_bytes_per_sec"])


class AppendSampleTests(unittest.TestCase):
    def test_appends_one_line_per_call_to_day_partitioned_file(self) -> None:
        now = datetime(2026, 8, 29, 10, 0, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            path1 = resource_sample.append_sample(output_dir, {"a": 1}, now=now)
            path2 = resource_sample.append_sample(output_dir, {"a": 2}, now=now)
            self.assertEqual(path1, path2)
            self.assertEqual(path1.name, "resource-20260829.log")
            lines = path1.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0]), {"a": 1})
            self.assertEqual(json.loads(lines[1]), {"a": 2})


class StatePersistenceTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "resource_prev.json"
            resource_sample.save_state(path, {"ts": 123.0, "disks": [], "net": {}})
            loaded = resource_sample.load_prev_state(path)
        self.assertEqual(loaded, {"ts": 123.0, "disks": [], "net": {}})

    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(resource_sample.load_prev_state(Path("/no/such/file.json")), {})

    def test_corrupt_file_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(resource_sample.load_prev_state(path), {})


class HostReaderSmokeTests(unittest.TestCase):
    """直接读宿主 /proc 与标准库的函数：只做形状合理性冒烟检查。"""

    def test_read_load_avg_shape(self) -> None:
        load = resource_sample.read_load_avg()
        self.assertGreaterEqual(load["load1"], 0.0)
        self.assertGreaterEqual(load["cpu_count"], 1)

    def test_read_mem_info_shape(self) -> None:
        mem = resource_sample.read_mem_info()
        self.assertIn("mem_total_kb", mem)
        self.assertIn("mem_available_kb", mem)

    def test_read_disk_usage_skips_missing_mount(self) -> None:
        disks = resource_sample.read_disk_usage(["/", "/this/path/does/not/exist"])
        self.assertEqual(len(disks), 1)
        self.assertEqual(disks[0]["mount"], "/")

    def test_read_net_totals_shape(self) -> None:
        net = resource_sample.read_net_totals()
        self.assertIn("rx_bytes", net)
        self.assertIn("tx_bytes", net)


if __name__ == "__main__":
    unittest.main()
