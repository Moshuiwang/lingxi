"""Story / Epic / Publish 分层的纯逻辑验收（Issue #82）。"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CLASSIFIER = load(ROOT / "scripts/ci/classify_story_changes.py", "story_classifier_under_test")
WRITER = load(ROOT / "scripts/ci/write_epic_candidate.py", "candidate_writer_under_test")
VERIFIER = load(ROOT / "scripts/ci/verify_epic_candidate.py", "candidate_verifier_under_test")

HEAD = "a" * 40
TESTED = "b" * 40
TREE = "c" * 40
MERGE = "d" * 40


class StoryClassificationTest(unittest.TestCase):
    def test_pure_docs_do_not_start_full_gate(self) -> None:
        self.assertEqual(CLASSIFIER.classify(["docs/协作约定.md", "AGENTS.md"]), "docs")

    def test_normal_code_uses_fast_gate(self) -> None:
        self.assertEqual(CLASSIFIER.classify(["src/lingxi/core/ids.py", "tests/test_core_ids.py"]), "fast")

    def test_ci_deploy_migration_and_dependency_changes_use_full_gate(self) -> None:
        for path in (
            ".github/workflows/story.yml",
            "scripts/ci/verify_repository.sh",
            "deploy/compose.yaml",
            "migrations/versions/x.py",
            "Dockerfile",
            "pyproject.toml",
        ):
            with self.subTest(path=path):
                self.assertEqual(CLASSIFIER.classify([path]), "full")

    def test_unknown_path_fails_closed_to_full(self) -> None:
        self.assertEqual(CLASSIFIER.classify(["new-top-level/config.toml"]), "full")

    def test_empty_diff_fails_closed_to_full(self) -> None:
        self.assertEqual(CLASSIFIER.classify([]), "full")


class CandidateIdentityTest(unittest.TestCase):
    def pr(self, *, merge_sha: str = MERGE) -> dict:
        return {
            "number": 82,
            "merged_at": "2026-08-07T00:00:00Z",
            "merge_commit_sha": merge_sha,
            "base": {"ref": "main"},
            "head": {"sha": HEAD},
        }

    def document(self, *, tree: str = TREE, run_id: int = 123) -> dict:
        return WRITER.candidate_document(
            repository="Moshuiwang/lingxi",
            pr_number=82,
            head_sha=HEAD,
            tested_sha=TESTED,
            tree_sha=tree,
            run_id=run_id,
        )

    def test_exact_candidate_passes(self) -> None:
        VERIFIER.validate_document(
            self.document(),
            repository="Moshuiwang/lingxi",
            pr=self.pr(),
            tree_sha=TREE,
            run_id=123,
        )

    def test_changed_main_tree_is_rejected(self) -> None:
        with self.assertRaises(VERIFIER.CandidateError):
            VERIFIER.validate_document(
                self.document(),
                repository="Moshuiwang/lingxi",
                pr=self.pr(),
                tree_sha="e" * 40,
                run_id=123,
            )

    def test_candidate_from_another_run_is_rejected(self) -> None:
        with self.assertRaises(VERIFIER.CandidateError):
            VERIFIER.validate_document(
                self.document(run_id=122),
                repository="Moshuiwang/lingxi",
                pr=self.pr(),
                tree_sha=TREE,
                run_id=123,
            )

    def test_direct_push_without_one_merged_pr_is_rejected(self) -> None:
        with self.assertRaises(VERIFIER.CandidateError):
            VERIFIER.select_merged_pr([], MERGE)

    def test_pr_for_a_different_merge_commit_is_rejected(self) -> None:
        with self.assertRaises(VERIFIER.CandidateError):
            VERIFIER.select_merged_pr([self.pr(merge_sha="f" * 40)], MERGE)

    def test_exact_merged_pr_is_selected(self) -> None:
        self.assertEqual(VERIFIER.select_merged_pr([self.pr()], MERGE)["number"], 82)


if __name__ == "__main__":
    unittest.main()
