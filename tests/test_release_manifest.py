"""发布资格与镜像提升的正反用例；网络调用由真实 GitHub 演练另行覆盖。"""

import copy
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/ci"))
guard = importlib.import_module("check_release_flow")
release = importlib.import_module("release_manifest")
proof = importlib.import_module("verify_epic_candidate")
writer = importlib.import_module("write_epic_candidate")


def candidate():
    return {
        "schema": 1,
        "repository": "Moshuiwang/lingxi",
        "tag": "v2.3.2-rc.42",
        "version": "2.3.2",
        "prerelease": True,
        "branch": "release/2.3",
        "commit": "a" * 40,
        "tree": "b" * 40,
        "run_id": 42,
        "migration_heads": ["0090_delivery_retry_backoff"],
        "images": {
            s: f"ghcr.io/moshuiwang/lingxi-{s}@sha256:" + "c" * 64 for s in release.SERVICES
        },
    }


def receipt(doc):
    return {
        "schema": 1,
        "candidate_tag": doc["tag"],
        "manifest_sha256": release.fingerprint(doc),
        "result": "passed",
        "evidence_url": "https://github.com/Moshuiwang/lingxi/issues/669#issuecomment-123",
        "recovery": {
            "previous_tag": "v2.3.1",
            "instructions": "复用已记录镜像，数据库结构未变化",
            "evidence_url": "https://github.com/Moshuiwang/lingxi/issues/669#issuecomment-124",
        },
    }


class ReleaseManifestTests(unittest.TestCase):
    def test_complete_candidate_is_valid(self):
        release.validate_manifest(candidate(), "Moshuiwang/lingxi", prerelease=True)

    def test_wrong_version_branch_repo_missing_image_and_mutable_image_rejected(self):
        for key, value in [
            ("version", "2.4.0"),
            ("branch", "main"),
            ("branch", "release/2.4"),
            ("prerelease", False),
            ("commit", "main"),
            ("run_id", 0),
            ("migration_heads", []),
        ]:
            with self.subTest(key=key, value=value):
                doc = dict(candidate(), **{key: value})
                with self.assertRaises(release.ReleaseError):
                    release.validate_manifest(doc, "Moshuiwang/lingxi", prerelease=True)
        for images in [
            {},
            {"scheduler": "latest"},
            dict(candidate()["images"], worker="ghcr.io/other/worker@sha256:" + "a" * 64),
        ]:
            with self.assertRaises(release.ReleaseError):
                release.validate_manifest(
                    dict(candidate(), images=images), "Moshuiwang/lingxi", prerelease=True
                )
        with self.assertRaises(release.ReleaseError):
            release.validate_manifest(candidate(), "other/repository", prerelease=True)

    def test_version_parser_rejects_shell_text_and_noncanonical_versions(self):
        for value in [
            "v2.03.2",
            "2.3.2",
            "v2.3.2-rc.0",
            "v2.3.2;false",
            "../../file",
            "v2.3.2\n",
            "v2.3.2-beta.1",
        ]:
            with self.subTest(value=value), self.assertRaises(release.ReleaseError):
                release.release_version(value)

    def test_receipt_binds_exact_images_and_real_evidence_location(self):
        doc = candidate()
        release.validate_acceptance(receipt(doc), doc)
        for key, value in [
            ("result", "unknown"),
            ("candidate_tag", "v2.3.2-rc.41"),
            ("manifest_sha256", "d" * 64),
            ("evidence_url", "https://example.com/pass"),
            ("recovery", {}),
        ]:
            with self.subTest(key=key), self.assertRaises(release.ReleaseError):
                release.validate_acceptance(dict(receipt(doc), **{key: value}), doc)
        old = receipt(doc)
        old["recovery"]["previous_tag"] = "v2.4.0"
        with self.assertRaises(release.ReleaseError):
            release.validate_acceptance(old, doc)
        changed = copy.deepcopy(doc)
        changed["images"]["worker"] = changed["images"]["worker"].replace("c" * 64, "d" * 64)
        with self.assertRaises(release.ReleaseError):
            release.validate_acceptance(receipt(doc), changed)

    def test_prerelease_cannot_be_selected_for_production(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(release, "load_release", return_value=({}, candidate())),
        ):
            output = Path(tmp) / "images.env"
            with self.assertRaises(release.ReleaseError):
                release.resolve("Moshuiwang/lingxi", candidate()["tag"], "production", output)
            self.assertFalse(output.exists())

    def test_stage_selection_uses_digests(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(release, "load_release", return_value=({}, candidate())),
        ):
            output = Path(tmp) / "images.env"
            release.resolve("Moshuiwang/lingxi", candidate()["tag"], "stage", output)
            text = output.read_text()
            self.assertEqual(text.count("@sha256:"), 4)
            self.assertNotIn("GITHUB_TOKEN", text)

    def test_promotion_preserves_every_image_and_does_not_build(self):
        doc = candidate()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                os.environ, {"GITHUB_REF": "refs/heads/main", "GITHUB_REF_PROTECTED": "true"}
            ),
            patch.object(release, "load_release", return_value=({}, doc)),
            patch.object(release, "receipt_for", return_value=receipt(doc)),
            patch.object(release, "write_release") as write,
            patch.object(release, "command") as command,
        ):
            release.promote(
                doc["repository"], doc["tag"], Path(tmp) / "release-manifest.json", True
            )
            promoted = write.call_args.args[0]
            self.assertEqual(promoted["images"], doc["images"])
            self.assertEqual(promoted["commit"], doc["commit"])
            self.assertEqual(promoted["tag"], "v2.3.2")
            self.assertFalse(promoted["prerelease"])
            command.assert_not_called()

    def test_missing_receipt_rejects_promotion_without_writes(self):
        doc = candidate()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                os.environ, {"GITHUB_REF": "refs/heads/main", "GITHUB_REF_PROTECTED": "true"}
            ),
            patch.object(release, "load_release", return_value=({}, doc)),
            patch.object(release, "write_release") as write,
        ):
            with self.assertRaises(release.ReleaseError):
                release.promote(
                    doc["repository"], doc["tag"], Path(tmp) / "release-manifest.json", True
                )
            write.assert_not_called()

    def test_retry_of_existing_release_checks_same_content_without_requiring_own_run_complete(self):
        doc = candidate()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(release, "find_release", return_value={"draft": False}),
            patch.object(release, "load_release", return_value=({}, doc)) as load,
            patch.object(release, "api") as api,
        ):
            release.write_release(doc, Path(tmp) / "release-manifest.json")
            load.assert_called_once_with(doc["repository"], doc["tag"], require_completed=False)
            api.assert_not_called()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(release, "find_release", return_value={"draft": False}),
            patch.object(release, "load_release", return_value=({}, dict(doc, commit="d" * 40))),
            self.assertRaises(release.ReleaseError),
        ):
            release.write_release(doc, Path(tmp) / "release-manifest.json")

    def test_unprotected_or_nonmain_cannot_promote(self):
        for ref, protected in [("refs/heads/main", "false"), ("refs/heads/release/2.3", "true")]:
            with (
                patch.dict(os.environ, {"GITHUB_REF": ref, "GITHUB_REF_PROTECTED": protected}),
                self.assertRaises(release.ReleaseError),
            ):
                release.promote("Moshuiwang/lingxi", "v2.3.2-rc.1", Path("/not-written"), True)

    def test_public_release_requires_exact_markers(self):
        doc = candidate()
        valid = {
            "tag_name": doc["tag"],
            "prerelease": True,
            "draft": False,
            "assets": [{"name": "release-manifest.json"}],
        }
        release.validate_release_record(valid, doc)
        for k, v in [
            ("draft", True),
            ("prerelease", False),
            ("tag_name", "v2.3.2"),
            ("assets", []),
        ]:
            with self.subTest(key=k), self.assertRaises(release.ReleaseError):
                release.validate_release_record(dict(valid, **{k: v}), doc)

    def test_receipt_cannot_validate_other_branch_proof(self):
        pr = {
            "number": 1,
            "head": {"sha": "a" * 40},
            "base": {"ref": "release/2.3"},
            "merged_at": "now",
            "merge_commit_sha": "b" * 40,
        }
        doc = writer.candidate_document(
            repository="Moshuiwang/lingxi",
            pr_number=1,
            head_sha="a" * 40,
            tested_sha="b" * 40,
            tree_sha="c" * 40,
            run_id=1,
            base_ref="release/2.3",
        )
        proof.select_merged_pr([pr], "b" * 40, "release/2.3")
        proof.validate_document(
            doc, repository="Moshuiwang/lingxi", pr=pr, tree_sha="c" * 40, run_id=1
        )
        with self.assertRaises(proof.CandidateError):
            proof.select_merged_pr([pr], "b" * 40, "release/2.4")
        with self.assertRaises(proof.CandidateError):
            proof.validate_document(
                dict(doc, base_ref="main"),
                repository="Moshuiwang/lingxi",
                pr=pr,
                tree_sha="c" * 40,
                run_id=1,
            )

    def test_migrations_are_discovered_from_current_chain(self):
        self.assertEqual(release.migration_heads(), ["0090_delivery_retry_backoff"])


class ReleaseWorkflowTests(unittest.TestCase):
    def test_real_workflows(self):
        self.assertEqual(guard.check(), [])

    def test_each_missing_protection_turns_check_red(self):
        mutations = [
            (".github/workflows/publish.yml", "      - 'release/**'", "      - main"),
            (".github/workflows/ci.yml", '--base-ref "${{ github.base_ref }}"', ""),
            (".github/workflows/release.yml", "needs: [candidate]", "needs: []"),
            (".github/workflows/release.yml", "github.ref_protected", "true"),
            (".github/CODEOWNERS", "/deploy/releases/acceptance/ @Moshuiwang", ""),
        ]
        paths = [
            ".github/workflows/publish.yml",
            ".github/workflows/ci.yml",
            ".github/workflows/release.yml",
            ".github/CODEOWNERS",
            "deploy/生产部署runbook.md",
        ]
        for name, old, new in mutations:
            with self.subTest(name=name, old=old), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for path in paths:
                    dest = root / path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text((ROOT / path).read_text())
                file = root / name
                self.assertIn(old, file.read_text())
                file.write_text(file.read_text().replace(old, new))
                self.assertTrue(guard.check(root))


if __name__ == "__main__":
    unittest.main()
