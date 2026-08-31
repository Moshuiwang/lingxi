"""Issue #498 分级资产门禁的正向、伪装负向与影响面单测。"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).parents[1]


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CLASSIFIER = load(ROOT / "scripts/ci/classify_story_changes.py", "asset_classifier_under_test")
L1 = load(ROOT / "scripts/ci/check_l1_assets.py", "l1_gate_under_test")
IMPACT = load(ROOT / "scripts/ci/check_permission_impact.py", "permission_impact_under_test")
PREPARE = load(
    ROOT / "scripts/ci/prepare_permission_impact_counts.py",
    "permission_impact_prepare_under_test",
)
EXPORT = load(
    ROOT / "scripts/ops/export_permission_impact_counts.py",
    "permission_impact_export_under_test",
)


class AssetClassificationTest(unittest.TestCase):
    def test_legacy_modes_and_new_risk_levels_are_both_exposed(self) -> None:
        docs = CLASSIFIER.classify_detail(["docs/协作约定.md", "README.md"])
        self.assertEqual((docs.mode, docs.risk_level), ("docs", "l0"))

        l1 = CLASSIFIER.classify_detail(
            ["src/lingxi/config/content.toml", "docs/文案说明.md"]
        )
        self.assertEqual((l1.mode, l1.risk_level), ("fast", "l1"))
        self.assertTrue(l1.docs_changed)
        self.assertTrue(l1.l1_changed)

        l3 = CLASSIFIER.classify_detail(
            ["src/lingxi/config/company_function_metric_map.toml"]
        )
        self.assertEqual((l3.mode, l3.risk_level), ("full", "l3"))
        self.assertTrue(l3.l3_changed)

    def test_l1_mixed_with_code_keeps_fast_but_cannot_skip_l1_check(self) -> None:
        detail = CLASSIFIER.classify_detail(
            ["src/lingxi/config/content.toml", "src/lingxi/core/ids.py"]
        )
        self.assertEqual(detail.mode, "fast")
        self.assertEqual(detail.risk_level, "fast")
        self.assertTrue(detail.l1_changed)

    def test_docs_mixed_with_l1_keeps_l1_route_and_sets_docs_gate_flag(self) -> None:
        detail = CLASSIFIER.classify_detail(
            ["docs/文案说明.md", "src/lingxi/config/content.toml"]
        )
        self.assertEqual((detail.mode, detail.risk_level), ("fast", "l1"))
        self.assertTrue(detail.docs_changed)
        self.assertTrue(detail.l1_changed)

    def test_l3_wins_over_every_other_safe_or_fast_path(self) -> None:
        detail = CLASSIFIER.classify_detail(
            [
                "src/lingxi/config/galaxy_role_function_map.toml",
                "src/lingxi/config/content.toml",
                "src/lingxi/core/ids.py",
            ]
        )
        self.assertEqual((detail.mode, detail.risk_level), ("full", "l3"))

    def test_unregistered_config_and_filename_tricks_fail_closed(self) -> None:
        for path in (
            "src/lingxi/config/new.toml",
            " src/lingxi/config/content.toml",
            "src/lingxi/config/content.toml ",
            "src/lingxi/config/./content.toml",
        ):
            with self.subTest(path=path):
                detail = CLASSIFIER.classify_detail([path])
                self.assertEqual(detail.mode, "full")
                self.assertEqual(detail.risk_level, "full")

    def test_reserved_l2_extension_is_not_implemented_as_a_light_gate(self) -> None:
        original = CLASSIFIER.L2_FILES
        CLASSIFIER.L2_FILES = frozenset({"src/lingxi/config/prompt.toml"})
        try:
            detail = CLASSIFIER.classify_detail(["src/lingxi/config/prompt.toml"])
        finally:
            CLASSIFIER.L2_FILES = original
        self.assertEqual((detail.mode, detail.risk_level), ("full", "l2"))

    def test_write_output_includes_risk_and_change_flags(self) -> None:
        detail = CLASSIFIER.classify_detail(["src/lingxi/config/content.toml"])
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "github-output"
            CLASSIFIER.write_output(output, detail, ["src/lingxi/config/content.toml"])
            self.assertEqual(
                output.read_text(encoding="utf-8").splitlines(),
                [
                    "mode=fast",
                    "risk_level=l1",
                    "docs_changed=false",
                    "l1_changed=true",
                    "l3_changed=false",
                    "worker_changed=false",
                ],
            )


class L1GateTest(unittest.TestCase):
    def test_real_l1_assets_pass(self) -> None:
        self.assertEqual(L1.check_l1_assets(), [])

    def _paths(self, content: str, lock: str, aliases: str, source: str = "pass\n"):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        directory = Path(tmp)
        content_path = directory / "content.toml"
        lock_path = directory / "content.lock.toml"
        alias_path = directory / "aliases.toml"
        source_root = directory / "admin"
        source_root.mkdir()
        daily_report = directory / "daily_report.py"
        content_path.write_text(content, encoding="utf-8")
        lock_path.write_text(lock, encoding="utf-8")
        alias_path.write_text(aliases, encoding="utf-8")
        (source_root / "sample.py").write_text(source, encoding="utf-8")
        daily_report.write_text("pass\n", encoding="utf-8")
        return content_path, lock_path, alias_path, source_root, daily_report

    def test_bad_alias_shape_is_rejected_even_though_runtime_loader_is_fail_open(self) -> None:
        paths = self._paths(
            '[meta]\nversion = "v1"\n[texts]\ngreeting = "hello"\n[cards]\nmain = "ok"\n',
            'version = "v1"\ndigest = "sha256:bad"\nretired_versions = []\n[keys]\n',
            '[aliases]\n"别名" = "not a metric"\n',
        )
        failures = L1.check_l1_assets(
            paths[0], paths[1], paths[2], admin_source_root=paths[3], daily_report_path=paths[4]
        )
        self.assertTrue(any("指标 token 形状" in failure for failure in failures), failures)

    def test_content_lock_mismatch_is_rejected(self) -> None:
        paths = self._paths(
            '[meta]\nversion = "v1"\n[texts]\ngreeting = "hello"\n[cards]\nmain = "ok"\n',
            'version = "v1"\ndigest = "sha256:wrong"\nretired_versions = []\n[keys]\n',
            '[aliases]\n"别名" = "sub_count"\n',
        )
        failures = L1.check_l1_assets(
            paths[0], paths[1], paths[2], admin_source_root=paths[3], daily_report_path=paths[4]
        )
        self.assertTrue(any("文案变了" in failure or "整体摘要不符" in failure for failure in failures), failures)

    def test_retired_term_in_real_string_literal_is_rejected_but_docstring_is_ignored(self) -> None:
        paths = self._paths(
            '[meta]\nversion = "v1"\n[texts]\ngreeting = "hello"\n[cards]\nmain = "ok"\n',
            'version = "v1"\ndigest = "sha256:bad"\nretired_versions = []\n[keys]\n',
            '[aliases]\n"别名" = "sub_count"\n',
            '"""历史记录：收回不是当前出口术语。"""\n\ndef show():\n    return "收回"\n',
        )
        failures = L1.check_l1_assets(
            paths[0], paths[1], paths[2], admin_source_root=paths[3], daily_report_path=paths[4]
        )
        self.assertTrue(any("退役术语" in failure for failure in failures), failures)


class PermissionImpactTest(unittest.TestCase):
    OLD_ROLE = {"roles": {"银河运营": "运营"}}
    NEW_ROLE = {"roles": {"银河运营": "运营", "银河销售": "销售"}}
    OLD_METRIC = {"companies": {"1": {"运营": ["m1", "m2"], "销售": ["m0"]}}}
    NEW_METRIC = {"companies": {"1": {"运营": ["m2", "m3"], "销售": ["m0"]}}}

    def test_grant_and_shrink_are_separate_and_counts_are_explicit(self) -> None:
        with self.assertRaises(IMPACT.CountEvidenceError):
            IMPACT.build_report(
                self.OLD_ROLE,
                self.NEW_ROLE,
                self.OLD_METRIC,
                self.NEW_METRIC,
            )
        report = IMPACT.build_report(
            self.OLD_ROLE,
            self.NEW_ROLE,
            self.OLD_METRIC,
            self.NEW_METRIC,
            user_counts={"grant": 3, "shrink": 1},
        )
        grants = {(row["role"], tuple(row["metrics"])) for row in report["grant"]}
        shrinks = {(row["role"], tuple(row["metrics"])) for row in report["shrink"]}
        self.assertIn(("银河销售", ("m0",)), grants)
        self.assertIn(("银河运营", ("m3",)), grants)
        self.assertIn(("银河运营", ("m1",)), shrinks)
        self.assertEqual(report["affected_user_counts"]["grant"], 3)
        self.assertEqual(report["affected_user_counts"]["shrink"], 1)
        self.assertEqual(report["affected_user_counts"]["status"], "provided")
        self.assertEqual(report["affected_user_counts"]["source"]["kind"], "test-explicit")
        rendered = IMPACT.render_report(report)
        self.assertIn("新增授予面（grant）", rendered)
        self.assertIn("收缩面（shrink）", rendered)
        self.assertIn("受影响用户数量（仅数量，不含内部 ID）", rendered)
        self.assertNotIn("user-", rendered)
        self.assertNotIn("internal-user", rendered)

    def test_only_explicit_pure_counts_are_accepted(self) -> None:
        report = IMPACT.build_report(
            self.OLD_ROLE,
            self.NEW_ROLE,
            self.OLD_METRIC,
            self.NEW_METRIC,
            user_counts={"grant": 3, "shrink": 1},
        )
        self.assertEqual(report["affected_user_counts"]["grant"], 3)
        with self.assertRaises(ValueError):
            IMPACT.build_report(
                self.OLD_ROLE,
                self.NEW_ROLE,
                self.OLD_METRIC,
                self.NEW_METRIC,
                user_counts={"grant": ["internal-user-1"], "shrink": 0},
            )

    def test_strict_manifest_is_bound_to_candidate_and_contains_only_aggregate_metadata(self) -> None:
        base_ref = "base-sha"
        head_ref = "head-sha"
        base_digest = "a" * 64
        head_digest = "b" * 64
        base_surface = IMPACT.build_surface(
            IMPACT._role_map(self.OLD_ROLE, "base"),
            IMPACT._metric_map(self.OLD_METRIC, "base"),
        )
        head_surface = IMPACT.build_surface(
            IMPACT._role_map(self.NEW_ROLE, "head"),
            IMPACT._metric_map(self.NEW_METRIC, "head"),
        )
        grant = head_surface - base_surface
        shrink = base_surface - head_surface
        manifest = {
            "schema": IMPACT.COUNT_SCHEMA,
            "base_ref": base_ref,
            "head_ref": head_ref,
            "base_facts_sha256": base_digest,
            "head_facts_sha256": head_digest,
            "grant_surface_sha256": IMPACT._surface_digest(grant),
            "shrink_surface_sha256": IMPACT._surface_digest(shrink),
            "counts": {"grant": 7, "shrink": 4},
            "source": {
                "kind": IMPACT.STAGE_COUNT_SOURCE,
                "environment": "biai-stage",
                "dataset": "galaxy_user_role",
                "query_version": "permission-impact-users/v1",
                "captured_at": "2026-08-31T00:00:00+00:00",
            },
        }
        report = IMPACT.build_report(
            self.OLD_ROLE,
            self.NEW_ROLE,
            self.OLD_METRIC,
            self.NEW_METRIC,
            user_counts=manifest,
            base_facts_sha256=base_digest,
            head_facts_sha256=head_digest,
            base_ref=base_ref,
            head_ref=head_ref,
            strict_count_manifest=True,
        )
        self.assertEqual(report["affected_user_counts"]["grant"], 7)
        self.assertEqual(report["affected_user_counts"]["source"]["kind"], IMPACT.STAGE_COUNT_SOURCE)
        self.assertNotIn("user_id", json.dumps(report, ensure_ascii=False))

        forged = dict(manifest)
        forged["source"] = dict(manifest["source"])
        forged["source"]["user_id"] = "internal-user-1"
        with self.assertRaises(IMPACT.CountEvidenceError):
            IMPACT.build_report(
                self.OLD_ROLE,
                self.NEW_ROLE,
                self.OLD_METRIC,
                self.NEW_METRIC,
                user_counts=forged,
                base_facts_sha256=base_digest,
                head_facts_sha256=head_digest,
                base_ref=base_ref,
                head_ref=head_ref,
                strict_count_manifest=True,
            )

    def test_empty_surface_derives_zero_from_static_diff(self) -> None:
        role = {"roles": {"角色": "没有对应指标的职能"}}
        metric = {"companies": {"1": {"另一职能": ["m1"]}}}
        report = IMPACT.build_report(role, role, metric, metric)
        counts = report["affected_user_counts"]
        self.assertEqual((counts["grant"], counts["shrink"]), (0, 0))
        self.assertEqual(counts["status"], "derived")
        self.assertEqual(counts["source"]["kind"], IMPACT.EMPTY_COUNT_SOURCE)

    def test_count_input_symlink_is_rejected_before_reading_runner_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            target = directory / "target.json"
            target.write_text('{"grant": 1, "shrink": 0}\n', encoding="utf-8")
            link = directory / "counts.json"
            link.symlink_to(target)
            with self.assertRaises(ValueError):
                IMPACT._load_user_counts(link)

    def test_malformed_permission_facts_fail_closed(self) -> None:
        with self.assertRaises(IMPACT.ConfigShapeError):
            IMPACT.build_report(
                {"roles": {"r": "f"}},
                {"roles": {"r": "f"}},
                {"companies": {"1": {"f": []}}},
                {"companies": {"1": {"f": ["m"]}}},
            )

    def test_git_ref_cli_reads_only_the_two_permission_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = Path(tmp)
            for relative, body in {
                IMPACT.ROLE_MAP_PATH: '[roles]\n"角色" = "职能"\n',
                IMPACT.METRIC_MAP_PATH: '[companies."1"]\n"职能" = ["m1"]\n',
            }.items():
                path = repository / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body, encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-qm",
                    "base",
                ],
                cwd=repository,
                check=True,
            )
            base = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            (repository / IMPACT.METRIC_MAP_PATH).write_text(
                '[companies."1"]\n"职能" = ["m1", "m2"]\n', encoding="utf-8"
            )
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-qm",
                    "change",
                ],
                cwd=repository,
                check=True,
            )
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/ci/check_permission_impact.py"),
                    "--base-ref",
                    base,
                    "--head-ref",
                    head,
                    "--repository",
                    str(repository),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("失败关闭", result.stderr)
        self.assertNotIn("not_provided", result.stdout + result.stderr)

    def test_prepare_derives_zero_only_when_surface_diff_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = Path(tmp)
            role_path = repository / IMPACT.ROLE_MAP_PATH
            metric_path = repository / IMPACT.METRIC_MAP_PATH
            role_path.parent.mkdir(parents=True, exist_ok=True)
            role_path.write_text('[roles]\n"角色" = "职能"\n', encoding="utf-8")
            metric_path.parent.mkdir(parents=True, exist_ok=True)
            metric_path.write_text(
                '[companies."1"]\n"未绑定职能" = ["m1"]\n', encoding="utf-8"
            )
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-qm",
                    "base",
                ],
                cwd=repository,
                check=True,
            )
            base = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            metric_path.write_text(
                '[companies."1"]\n"未绑定职能" = ["m1", "m2"]\n', encoding="utf-8"
            )
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-qm",
                    "unbound-change",
                ],
                cwd=repository,
                check=True,
            )
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            output = repository / "counts.json"
            evidence = PREPARE.prepare(
                base,
                head,
                repository=repository,
                manifest=repository / "missing-manifest.json",
                output=output,
            )
            self.assertEqual(evidence["counts"], {"grant": 0, "shrink": 0})
            self.assertEqual(evidence["source"]["kind"], IMPACT.EMPTY_COUNT_SOURCE)
            self.assertNotIn("user_id", output.read_text(encoding="utf-8"))

    def test_stage_export_writes_only_pure_counts_and_source_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = Path(tmp)
            role_path = repository / IMPACT.ROLE_MAP_PATH
            metric_path = repository / IMPACT.METRIC_MAP_PATH
            role_path.parent.mkdir(parents=True, exist_ok=True)
            metric_path.parent.mkdir(parents=True, exist_ok=True)
            role_path.write_text('[roles]\n"角色" = "职能"\n', encoding="utf-8")
            metric_path.write_text('[companies."1"]\n"职能" = ["m1"]\n', encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-qm",
                    "base",
                ],
                cwd=repository,
                check=True,
            )
            base = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True
            ).stdout.strip()
            metric_path.write_text('[companies."1"]\n"职能" = ["m1", "m2"]\n', encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-qm",
                    "change",
                ],
                cwd=repository,
                check=True,
            )
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True
            ).stdout.strip()
            original = EXPORT._read_stage_counts
            EXPORT._read_stage_counts = lambda _dsn, *, grant_roles, shrink_roles: (
                12,
                5,
                "2026-08-31T00:00:00+00:00",
            )
            try:
                output = repository / "counts.json"
                with mock.patch.dict(EXPORT.os.environ, {EXPORT.DSN_ENV: "redacted"}):
                    evidence = EXPORT.export(base, head, repository=repository, output=output)
            finally:
                EXPORT._read_stage_counts = original
            self.assertEqual(evidence["counts"], {"grant": 12, "shrink": 5})
            serialized = output.read_text(encoding="utf-8")
            self.assertNotIn("redacted", serialized)
            self.assertNotIn("user_id", serialized)
            self.assertNotIn("user-", serialized)

    def test_stage_export_query_is_aggregate_only(self) -> None:
        source = (ROOT / "scripts/ops/export_permission_impact_counts.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("count(DISTINCT role.user_id)", source)
        self.assertNotIn("SELECT role.user_id", source)
        self.assertNotIn("role.email", source)


if __name__ == "__main__":
    unittest.main()
