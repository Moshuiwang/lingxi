"""``scripts/ci/pip_audit_check.py`` 与依赖漏洞扫描作业的钉住用例。

三组断言：
1. 判定脚本的退出码三态——无漏洞 0；高 / 严重未豁免 1；同条有效豁免 0、到期次日 1；
   中 / 低只告警 0；严重度取不到 2；结果 JSON 或豁免清单格式错 2；环境变量追加豁免无效。
2. 严重度解析——CVSS v3 基础分对照已知向量；GHSA 标签与分数取更高者；PYSEC / GHSA
   重复记录按别名合并；只有 CVSS v4 向量判未知。
3. 工作流形状——``ci.yml`` 的 audit 作业与 extras 同矢量、同 ``if``；``dependency-audit.yml``
   只由定时与手动触发、与 ci.yml 钉同一个 pip-audit 版本、判定都走同一个脚本；
   gate 上限 20 分。

变异实测：把 ``evaluate`` 里的 ``today <= e.expires`` 改成恒真，到期用例应判红；把
``resolve_severity`` 末尾的未知返回改成默认 LOW，严重度取不到的用例应判红；还原后复绿。
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/ci/pip_audit_check.py"
ALLOWLIST = ROOT / "scripts/ci/pip_audit_allowlist.txt"
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
AUDIT_WORKFLOW = ROOT / ".github/workflows/dependency-audit.yml"
SCHEDULING_TESTS = ROOT / "tests/test_ci_dispatch_scheduling.py"

HIGH_ID = "PYSEC-2026-0001"
HIGH_GHSA = "GHSA-aaaa-bbbb-cccc"
MODERATE_ID = "PYSEC-2026-0002"
MODERATE_GHSA = "GHSA-dddd-eeee-ffff"


def _load_by_path(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclass 解析延迟注解时按 __module__ 回查 sys.modules，不登记会在装载期就炸。
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pac = _load_by_path(SCRIPT, "pip_audit_check_under_test")


def _vuln(vuln_id: str, aliases: list[str], fix: str = "9.9.9") -> dict:
    return {"id": vuln_id, "fix_versions": [fix], "aliases": aliases, "description": "示例"}


def _audit_json(*vulns: dict, name: str = "examplepkg", version: str = "1.0.0") -> dict:
    return {
        "dependencies": [
            {"name": "cleanpkg", "version": "2.0.0", "vulns": []},
            {"name": name, "version": version, "vulns": list(vulns)},
        ],
        "fixes": [],
    }


SEVERITIES = {
    HIGH_GHSA: {"database_specific": {"severity": "HIGH"}, "severity": []},
    MODERATE_GHSA: {
        "database_specific": {"severity": "MODERATE"},
        "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:L/A:N"}],
    },
}


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, name: str, content) -> Path:
        path = self.tmp / name
        text = json.dumps(content, ensure_ascii=False) if not isinstance(content, str) else content
        path.write_text(text, encoding="utf-8")
        return path

    def run_check(
        self,
        audit,
        allowlist: str = "",
        *,
        today: str = "2026-09-20",
        severities=SEVERITIES,
        env: dict[str, str] | None = None,
        extra_args: list[str] | None = None,
    ) -> subprocess.CompletedProcess:
        audit_path = audit if isinstance(audit, Path) else self.write("audit.json", audit)
        args = [
            sys.executable,
            "-B",
            str(SCRIPT),
            "--audit-json",
            str(audit_path),
            "--allowlist",
            str(self.write("allowlist.txt", allowlist)),
            "--today",
            today,
            "--label",
            "test",
        ]
        if severities is not None:
            args += ["--severity-file", str(self.write("severities.json", severities))]
        args += extra_args or []
        clean_env = {"PATH": os.environ.get("PATH", ""), **(env or {})}
        return subprocess.run(args, capture_output=True, text=True, env=clean_env)


class ExitCodeTests(_Case):
    def test_no_vulnerabilities_passes(self) -> None:
        result = self.run_check(_audit_json())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("通过（退出码 0）", result.stdout)

    def test_unexempted_high_is_red_and_lists_package_version_id(self) -> None:
        result = self.run_check(_audit_json(_vuln(HIGH_ID, [HIGH_GHSA], fix="1.2.3")))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("判红", result.stdout)
        row = next(line for line in result.stdout.splitlines() if HIGH_ID in line)
        for cell in ("examplepkg", "1.0.0", HIGH_ID, HIGH_GHSA, "HIGH", "1.2.3"):
            self.assertIn(cell, row)

    def test_valid_exemption_by_alias_passes_and_is_listed(self) -> None:
        allow = f"# 注释\n\n{HIGH_GHSA} 2026-12-31 已知影响面外，跟进 #999\n"
        result = self.run_check(_audit_json(_vuln(HIGH_ID, [HIGH_GHSA])), allow)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("已豁免（未到期）", result.stdout)
        self.assertIn("已豁免 1", result.stdout)
        self.assertIn("跟进 #999", result.stdout)

    def test_exemption_valid_through_expiry_day_then_red_the_day_after(self) -> None:
        audit = _audit_json(_vuln(HIGH_ID, [HIGH_GHSA]))
        allow = f"{HIGH_ID} 2026-09-20 到期日当天仍有效\n"
        self.assertEqual(self.run_check(audit, allow, today="2026-09-20").returncode, 0)
        expired = self.run_check(audit, allow, today="2026-09-21")
        self.assertEqual(expired.returncode, 1, expired.stdout)
        self.assertIn("豁免已于 2026-09-20 到期", expired.stdout)
        self.assertIn("判红 1", expired.stdout)

    def test_moderate_only_warns(self) -> None:
        result = self.run_check(_audit_json(_vuln(MODERATE_ID, [MODERATE_GHSA])))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("告警（中 / 低，不判红）", result.stdout)
        self.assertIn("MODERATE", result.stdout)

    def test_unresolvable_severity_is_unknown_not_green(self) -> None:
        only_v4 = {HIGH_GHSA: {"severity": [{"type": "CVSS_V4", "score": "CVSS:4.0/AV:N/AC:L"}]}}
        result = self.run_check(_audit_json(_vuln(HIGH_ID, [HIGH_GHSA])), severities=only_v4)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("未知", result.stdout)
        missing = self.run_check(_audit_json(_vuln(HIGH_ID, [HIGH_GHSA])), severities={})
        self.assertEqual(missing.returncode, 2, missing.stdout)
        self.assertIn("查不到任何编号", missing.stdout)

    def test_unreachable_severity_api_is_unknown(self) -> None:
        result = self.run_check(
            _audit_json(_vuln(HIGH_ID, [HIGH_GHSA])),
            severities=None,
            extra_args=["--osv-url", "http://127.0.0.1:9/"],
        )
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("严重度接口不可用", result.stdout)

    def test_unparseable_inputs_are_unknown(self) -> None:
        broken_json = self.write("broken.json", "{not json")
        self.assertEqual(self.run_check(broken_json).returncode, 2)
        self.assertEqual(self.run_check({"dependencies": "nope"}).returncode, 2)
        self.assertEqual(self.run_check(self.tmp / "absent.json").returncode, 2)
        skipped = {"dependencies": [{"name": "x", "version": "1", "skip_reason": "not found"}]}
        result = self.run_check(skipped)
        self.assertEqual(result.returncode, 2)
        self.assertIn("未能审计", result.stdout)

    def test_malformed_allowlist_is_unknown(self) -> None:
        audit = _audit_json()
        for bad in (
            f"{HIGH_ID} 2026-12-31\n",
            f"{HIGH_ID} 2026-13-45 坏日期\n",
            f"{HIGH_ID} 20261231 不是 ISO 日期\n",
            "NOT-AN-ID 2026-12-31 编号形状不对\n",
            f"{HIGH_ID} 2026-12-31 甲\n{HIGH_ID} 2027-01-01 重复编号\n",
        ):
            with self.subTest(bad=bad):
                result = self.run_check(audit, bad)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn("未知", result.stdout)

    def test_environment_variables_cannot_add_exemptions(self) -> None:
        audit = _audit_json(_vuln(HIGH_ID, [HIGH_GHSA]))
        env = {
            "PIP_AUDIT_ALLOWLIST": f"{HIGH_ID} 2099-01-01 环境变量偷渡",
            "PIP_AUDIT_IGNORE_VULN": HIGH_ID,
            "PIP_AUDIT_ALLOW": HIGH_GHSA,
            "LINGXI_PIP_AUDIT_EXEMPT": HIGH_ID,
        }
        result = self.run_check(audit, env=env)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(self.run_check(audit, extra_args=["--ignore-vuln", HIGH_ID]).returncode, 2)
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(
            re.findall(r"environ[^\n]*", source), ['environ.get("GITHUB_STEP_SUMMARY")']
        )

    def test_summary_is_appended_to_github_step_summary(self) -> None:
        summary = self.tmp / "summary.md"
        summary.write_text("已有内容\n", encoding="utf-8")
        result = self.run_check(_audit_json(), env={"GITHUB_STEP_SUMMARY": str(summary)})
        self.assertEqual(result.returncode, 0)
        text = summary.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("已有内容\n## 依赖漏洞扫描：test"))

    def test_unused_exemption_is_reported_not_red(self) -> None:
        allow = (
            f"{HIGH_GHSA} 2026-01-01 早已到期且未命中\n{MODERATE_GHSA} 2026-12-31 未到期且未命中\n"
        )
        result = self.run_check(_audit_json(), allow)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("未命中的条目（可删除）", result.stdout)
        self.assertIn(f"{HIGH_GHSA}（已到期", result.stdout)
        self.assertIn(f"{MODERATE_GHSA}（未到期", result.stdout)

    def test_exempted_finding_with_unknown_severity_does_not_fail(self) -> None:
        allow = f"{HIGH_ID} 2026-12-31 豁免不依赖严重度\n"
        result = self.run_check(_audit_json(_vuln(HIGH_ID, [HIGH_GHSA])), allow, severities={})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("未知", result.stdout)


class SeverityResolutionTests(unittest.TestCase):
    def test_cvss3_base_scores_match_published_values(self) -> None:
        cases = {
            "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H": 7.5,
            "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H": 9.8,
            "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:L/A:N": 5.3,
            "CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:C/C:L/I:L/A:N": 6.4,
            "CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:N/A:N": 5.9,
            "CVSS:3.0/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H": 7.5,
            "CVSS:3.1/AV:L/AC:H/PR:H/UI:R/S:U/C:N/I:N/A:N": 0.0,
        }
        for vector, expected in cases.items():
            with self.subTest(vector=vector):
                self.assertEqual(pac.cvss3_base_score(vector), expected)
        for malformed in (
            "CVSS:4.0/AV:N/AC:L",
            "CVSS:3.1/AV:X/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H",
            "x",
        ):
            self.assertIsNone(pac.cvss3_base_score(malformed))

    def test_level_thresholds(self) -> None:
        self.assertEqual(pac.level_from_score(9.0), "CRITICAL")
        self.assertEqual(pac.level_from_score(7.0), "HIGH")
        self.assertEqual(pac.level_from_score(6.9), "MODERATE")
        self.assertEqual(pac.level_from_score(3.9), "LOW")

    def test_label_and_score_take_the_higher_level(self) -> None:
        low_label_moderate_score = {
            "database_specific": {"severity": "LOW"},
            "severity": [
                {"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:L/A:N"}
            ],
        }
        self.assertEqual(pac.severity_from_record(low_label_moderate_score, "x").level, "MODERATE")
        high_label_moderate_score = {
            "database_specific": {"severity": "HIGH"},
            "severity": [
                {"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:N/A:N"}
            ],
        }
        self.assertEqual(pac.severity_from_record(high_label_moderate_score, "x").level, "HIGH")
        self.assertIsNone(
            pac.severity_from_record({"severity": [{"type": "CVSS_V4", "score": "x"}]}, "x")
        )
        self.assertIsNone(
            pac.severity_from_record({"database_specific": {"severity": "WEIRD"}}, "x")
        )

    def test_ghsa_record_is_consulted_first_then_other_ids(self) -> None:
        finding = pac.Finding("p", "1", (HIGH_ID, "CVE-2026-1000", HIGH_GHSA), ("2",), "")
        calls: list[str] = []

        def fetch(vuln_id: str):
            calls.append(vuln_id)
            return {"database_specific": {"severity": "CRITICAL"}} if vuln_id == HIGH_GHSA else None

        self.assertEqual(pac.resolve_severity(finding, fetch).level, "CRITICAL")
        self.assertEqual(calls, [HIGH_GHSA])
        cve_only = pac.Finding("p", "1", (HIGH_ID, "CVE-2026-1000"), ("2",), "")
        vector = {
            "severity": [
                {"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}
            ]
        }
        severity = pac.resolve_severity(
            cve_only, lambda i: vector if i == "CVE-2026-1000" else None
        )
        self.assertEqual(
            (severity.level, severity.score, severity.source_id), ("CRITICAL", 9.8, "CVE-2026-1000")
        )

    def test_duplicate_pysec_and_ghsa_records_merge_into_one_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.json"
            path.write_text(
                json.dumps(
                    _audit_json(
                        _vuln(HIGH_ID, [HIGH_GHSA, "CVE-2026-1000"], fix="1.1"),
                        _vuln(HIGH_ID, ["CVE-2026-1000", HIGH_GHSA], fix="1.1"),
                        _vuln(MODERATE_GHSA, [], fix="1.2"),
                        _vuln(MODERATE_ID, [MODERATE_GHSA], fix="1.2"),
                    )
                ),
                encoding="utf-8",
            )
            findings, count = pac.load_findings(path)
        self.assertEqual(count, 2)
        self.assertEqual([f.primary_id for f in findings], [HIGH_ID, MODERATE_GHSA])
        self.assertEqual(set(findings[0].ids), {HIGH_ID, HIGH_GHSA, "CVE-2026-1000"})
        self.assertEqual(set(findings[1].ids), {MODERATE_GHSA, MODERATE_ID})


def _job_block(text: str, name: str) -> str:
    match = re.search(
        r"^  " + re.escape(name) + r":\n(.*?)(?=^  [a-z][a-z0-9_-]*:|\Z)", text, re.M | re.S
    )
    assert match, f"job missing: {name}"
    return match.group(1)


def _matrix_values(block: str, key: str) -> list[str]:
    match = re.search(r"^\s+" + re.escape(key) + r":\s*\[([^\]]*)\]", block, re.M)
    assert match, f"matrix key missing: {key}"
    return [value.strip().strip("'\"") for value in match.group(1).split(",")]


def _condition(block: str) -> str:
    match = re.search(r"^    if: >-\n((?:      [^\n]*\n)+)", block, re.M)
    assert match, "if missing"
    return " ".join(line.strip() for line in match.group(1).splitlines())


class WorkflowShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ci = CI_WORKFLOW.read_text(encoding="utf-8")
        cls.weekly = AUDIT_WORKFLOW.read_text(encoding="utf-8")
        cls.audit = _job_block(cls.ci, "audit")
        cls.extras = _job_block(cls.ci, "extras")

    def test_audit_matrix_matches_extras_in_ci_and_weekly_workflow(self) -> None:
        extras = _matrix_values(self.extras, "extra")
        self.assertEqual(_matrix_values(self.audit, "group"), extras)
        self.assertEqual(_matrix_values(_job_block(self.weekly, "audit"), "group"), extras)
        self.assertEqual(len(re.findall(r"^\s+extra:\s*\[", self.ci, re.M)), 1)

    def test_audit_runs_exactly_when_extras_runs(self) -> None:
        self.assertEqual(_condition(self.audit), _condition(self.extras))
        self.assertIn("needs: [classify]", self.audit)
        scheduling = _load_by_path(SCHEDULING_TESTS, "ci_dispatch_scheduling_for_audit")
        results = {"classify": "success", "gate": "success", "extras": "success"}
        contexts = [
            scheduling.context(),
            scheduling.context(mode="docs", risk="l0"),
            scheduling.context(risk="l1"),
            scheduling.context(head="epic/story"),
            scheduling.context(base="release/2.6", mode="docs", risk="l0"),
            scheduling.context(base="epic/parent"),
            scheduling.context(event="workflow_dispatch", base="", head=""),
        ]
        for values in contexts:
            with self.subTest(values=values):
                classify = "skipped" if values["github.event_name"] != "pull_request" else "success"
                given = dict(results, classify=classify)
                self.assertEqual(
                    scheduling.scheduled(self.ci, "audit", values, given),
                    scheduling.scheduled(self.ci, "extras", values, given),
                )
        for status in ("failure", "cancelled"):
            self.assertFalse(
                scheduling.scheduled(
                    self.ci, "audit", scheduling.context(), dict(results, classify=status)
                )
            )

    def test_audit_job_shape_and_gate_timeout(self) -> None:
        self.assertIn("name: Epic Full / audit (${{ matrix.group }})", self.audit)
        self.assertIn("timeout-minutes: 10", self.audit)
        self.assertIn("fail-fast: false", self.audit)
        self.assertIn("scripts/ci/pip_audit_check.py", self.audit)
        self.assertIn("scripts/ci/pip_audit_allowlist.txt", self.audit)
        self.assertNotIn("|| true", self.audit)
        self.assertNotIn("continue-on-error", self.audit)
        gate = _job_block(self.ci, "gate")
        self.assertIsNotNone(re.search(r"^    timeout-minutes: 20$", gate, re.M))
        self.assertNotIn("pip-audit", gate)
        candidate = _job_block(self.ci, "candidate")
        self.assertIn("needs: [classify, docs, l1, gate, extras, image]", candidate)

    def test_scanner_version_is_pinned_identically_in_both_workflows(self) -> None:
        pins = set(re.findall(r"'pip-audit==([0-9][0-9A-Za-z.]*)'", self.ci + self.weekly))
        self.assertEqual(len(pins), 1, pins)
        self.assertEqual(self.ci.count("'pip-audit=="), 1)
        self.assertEqual(self.weekly.count("'pip-audit=="), 1)
        self.assertNotIn("pip install pip-audit\n", self.ci + self.weekly)
        for text in (self.ci, self.weekly):
            self.assertIn("--vulnerability-service osv", text)
            self.assertIn("--format json", text)
            self.assertIn("scripts/ci/pip_audit_check.py", text)

    def test_weekly_workflow_only_runs_on_schedule_and_manual_trigger(self) -> None:
        on_block = re.search(r"^on:\n((?:  [^\n]*\n|\n)+)", self.weekly, re.M).group(1)
        triggers = re.findall(r"^  ([a-z_]+):", on_block, re.M)
        self.assertEqual(sorted(triggers), ["schedule", "workflow_dispatch"])
        self.assertRegex(on_block, r"- cron: '\d+ \d+ \* \* [0-6]'")
        self.assertIn("permissions:\n  contents: read", self.weekly)
        self.assertNotIn("pull_request", self.weekly)
        self.assertNotIn("push:", self.weekly)
        self.assertNotIn("secrets.", self.weekly)
        self.assertIn("timeout-minutes: 10", _job_block(self.weekly, "audit"))


class AllowlistFileTests(unittest.TestCase):
    def test_repository_allowlist_holds_exactly_the_ruled_cryptography_exemptions(self) -> None:
        # 产品负责人 2026-09-20 裁定 A（#859 评论 5748497593）：cryptography 45.0.7 的 4 条 HIGH
        # 豁免到 2026-10-20；MODERATE / LOW 只告警、不入清单。多一条或换日期都要回到裁定处说明。
        entries = pac.parse_allowlist(ALLOWLIST)
        self.assertEqual(
            {entry.vuln_id for entry in entries},
            {
                "GHSA-r6ph-v2qm-q3c2",
                "GHSA-537c-gmf6-5ccf",
                "GHSA-jwv3-5hgf-82ww",
                "GHSA-g6cj-pr64-35w5",
            },
        )
        self.assertEqual({entry.expires.isoformat() for entry in entries}, {"2026-10-20"})
        for entry in entries:
            self.assertIn("5748497593", entry.reason)
        self.assertTrue(ALLOWLIST.read_text(encoding="utf-8").startswith("#"))


if __name__ == "__main__":
    unittest.main()
