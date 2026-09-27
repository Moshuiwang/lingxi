"""``scripts/ci/pip_audit_scan.py``（依赖漏洞扫描调用处的服务错误重试）的钉住用例（#874）。

假世界：用一个假扫描器替身按计数文件决定每次调用的表现（服务端 5xx / 客户端 4xx / 正常出结果），
不走网络。四组断言：
1. 注入一次 5xx → 重试一次后取到结果 → 判定脚本判绿；日志可读出重试次数与间隔。
2. 连续 5xx 至重试用尽 → 退出码 2，信息含「未能取得严重度（外部服务不可用），非漏洞判定」，
   结果文件不存在（判定步骤不可能据残留文件判绿）。
3. 真高危：扫描器退出 1 且结果完整 → 不重试，判定脚本照常判红。
4. 非服务端错误（4xx、结果缺失且无服务错误特征）不重试，交判定脚本按「未知」处理。

变异实测：把 ``_is_service_error`` 改成恒假，用例 1 应判红；把重试次数常量改成 0，用例 1 应判红；
把「结果完整即不重试」去掉，「结果完整但错误输出带 5xx 字样」用例的调用次数断言应判红；还原后复绿。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN = ROOT / "scripts/ci/pip_audit_scan.py"
CHECK = ROOT / "scripts/ci/pip_audit_check.py"
UNAVAILABLE = "未能取得严重度（外部服务不可用），非漏洞判定"

HIGH_ID = "PYSEC-2026-0001"
HIGH_GHSA = "GHSA-aaaa-bbbb-cccc"

# 假扫描器：按调用序号从行为表里取本次表现；序号超出表长时沿用最后一项。
FAKE_SCANNER = textwrap.dedent(
    """
    import json, sys
    from pathlib import Path
    plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    counter = Path(sys.argv[2])
    args = sys.argv[3:]
    output = Path(args[args.index("--output") + 1])
    n = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(n + 1))
    step = plan["steps"][min(n, len(plan["steps"]) - 1)]
    if step == "5xx":
        sys.stderr.write(
            "requests.exceptions.HTTPError: 502 Server Error: Bad Gateway for url: "
            "https://api.osv.dev/v1/query\\n"
            "pip_audit._service.interface.ServiceError\\n"
        )
        sys.exit(1)
    if step == "timeout":
        # pip-audit 2.10.1 真实文本：osv.py 把 requests.ConnectTimeout 转成服务层 ConnectionError，
        # CLI 只把消息写进日志，不打印异常类名。
        sys.stderr.write(
            "ERROR:pip_audit._cli:Could not connect to OSV's vulnerability feed\\n"
            "ERROR:pip_audit._cli:Tip: your network may be blocking this service. "
            "Try another service with `-s SERVICE`\\n"
        )
        sys.exit(1)
    if step == "4xx":
        sys.stderr.write(
            "requests.exceptions.HTTPError: 400 Client Error: Bad Request for url: "
            "https://api.osv.dev/v1/query\\n"
            "pip_audit._service.interface.ServiceError\\n"
        )
        sys.exit(1)
    if step == "crash":
        sys.stderr.write("ValueError: 不是服务错误\\n")
        sys.exit(1)
    if step == "ok_noisy":
        sys.stderr.write("WARNING: 502 Server Error 出现在告警里，但结果完整\\n")
    output.write_text(json.dumps(plan["result"]), encoding="utf-8")
    sys.exit(1 if any(d["vulns"] for d in plan["result"]["dependencies"]) else 0)
    """
)

CLEAN_RESULT = {"dependencies": [{"name": "cleanpkg", "version": "2.0.0", "vulns": []}]}
HIGH_RESULT = {
    "dependencies": [
        {
            "name": "examplepkg",
            "version": "1.0.0",
            "vulns": [
                {
                    "id": HIGH_ID,
                    "aliases": [HIGH_GHSA],
                    "fix_versions": ["9.9.9"],
                    "description": "",
                }
            ],
        }
    ]
}
SEVERITIES = {HIGH_GHSA: {"database_specific": {"severity": "HIGH"}, "severity": []}}


class ScanRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.fake = self.tmp / "fake_pip_audit.py"
        self.fake.write_text(FAKE_SCANNER, encoding="utf-8")
        self.counter = self.tmp / "calls.txt"
        self.output = self.tmp / "audit.json"
        self.summary = self.tmp / "summary.md"

    def scan(self, steps: list[str], result: dict = CLEAN_RESULT) -> subprocess.CompletedProcess:
        plan = self.tmp / "plan.json"
        plan.write_text(json.dumps({"steps": steps, "result": result}), encoding="utf-8")
        args = [
            sys.executable,
            "-B",
            str(SCAN),
            "--output",
            str(self.output),
            "--label",
            "test",
            "--retry-delay-base",
            "0",
            "--",
            sys.executable,
            "-B",
            str(self.fake),
            str(plan),
            str(self.counter),
            "--format",
            "json",
        ]
        env = {"PATH": os.environ.get("PATH", ""), "GITHUB_STEP_SUMMARY": str(self.summary)}
        return subprocess.run(args, capture_output=True, text=True, env=env)

    def check(self) -> subprocess.CompletedProcess:
        severities = self.tmp / "severities.json"
        severities.write_text(json.dumps(SEVERITIES), encoding="utf-8")
        allowlist = self.tmp / "allowlist.txt"
        allowlist.write_text("", encoding="utf-8")
        args = [
            sys.executable,
            "-B",
            str(CHECK),
            "--audit-json",
            str(self.output),
            "--allowlist",
            str(allowlist),
            "--severity-file",
            str(severities),
            "--today",
            "2026-09-27",
            "--label",
            "test",
        ]
        return subprocess.run(args, capture_output=True, text=True, env={"PATH": ""})

    def calls(self) -> int:
        return int(self.counter.read_text())

    def test_one_server_error_then_success_retries_and_turns_green(self) -> None:
        result = self.scan(["5xx", "ok"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls(), 2)
        self.assertIn("第 1 次重试", result.stdout)
        self.assertIn("间隔", result.stdout)
        self.assertIn("共重试 1 次", result.stdout)
        self.assertIn("502 Server Error", result.stdout + result.stderr)
        verdict = self.check()
        self.assertEqual(verdict.returncode, 0, verdict.stdout)

    def test_connection_timeout_is_retried_like_server_error(self) -> None:
        result = self.scan(["timeout", "ok"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls(), 2)

    def test_retries_exhausted_stays_red_with_unavailable_message(self) -> None:
        result = self.scan(["5xx"])
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.calls(), 3)  # 首次 + 最多 2 次重试
        self.assertIn(UNAVAILABLE, result.stdout)
        self.assertIn("共重试 2 次", result.stdout)
        self.assertFalse(self.output.exists())
        self.assertIn(UNAVAILABLE, self.summary.read_text(encoding="utf-8"))
        verdict = self.check()
        self.assertEqual(verdict.returncode, 2, verdict.stdout)

    def test_retry_intervals_increase(self) -> None:
        spec = importlib.util.spec_from_file_location("pip_audit_scan_under_test", SCAN)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        delays = module.retry_delays(10.0)
        self.assertEqual(len(delays), 2)
        self.assertGreater(delays[0], 0)
        self.assertLess(delays[0], delays[1])

    def test_real_high_vulnerability_is_not_retried_and_stays_red(self) -> None:
        result = self.scan(["ok"], result=HIGH_RESULT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls(), 1)
        self.assertIn("共重试 0 次", result.stdout)
        verdict = self.check()
        self.assertEqual(verdict.returncode, 1, verdict.stdout)
        self.assertIn(HIGH_ID, verdict.stdout)

    def test_complete_result_is_never_retried_even_with_server_error_text(self) -> None:
        result = self.scan(["ok_noisy", "ok"], result=HIGH_RESULT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls(), 1)
        self.assertEqual(self.check().returncode, 1)

    def test_client_error_and_non_service_failure_are_not_retried(self) -> None:
        for step in ("4xx", "crash"):
            with self.subTest(step=step):
                self.counter.unlink(missing_ok=True)
                result = self.scan([step, "ok"])
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.calls(), 1)
                self.assertNotIn(UNAVAILABLE, result.stdout)
                self.assertEqual(self.check().returncode, 2)

    def test_stale_output_from_a_failed_attempt_is_not_left_behind(self) -> None:
        self.output.write_text(json.dumps(CLEAN_RESULT), encoding="utf-8")
        result = self.scan(["5xx"])
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
