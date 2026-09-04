"""Story / Epic / Publish 分层的纯逻辑验收（Issue #82）。"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
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
        self.assertEqual(
            CLASSIFIER.classify(["src/lingxi/core/ids.py", "tests/test_core_ids.py"]), "fast"
        )

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

    def test_registered_ci_data_file_does_not_escalate_to_full_gate(self) -> None:
        # Issue #298：scripts/ci/ 下显式登记的纯数据文件改动不再整目录提级，
        # 单独走 fast——例如体量棘轮基线刷新，不必因此拖起整套 Epic Full。
        self.assertEqual(CLASSIFIER.classify(["scripts/ci/size_ratchet_baseline.txt"]), "fast")

    def test_registered_ci_data_file_mixed_with_source_stays_fast(self) -> None:
        self.assertEqual(
            CLASSIFIER.classify(["scripts/ci/size_ratchet_baseline.txt", "src/lingxi/core/ids.py"]),
            "fast",
        )

    def test_function_size_ratchet_baseline_does_not_escalate_to_full_gate(self) -> None:
        self.assertEqual(
            CLASSIFIER.classify(["scripts/ci/function_size_ratchet_baseline.txt"]), "fast"
        )

    def test_comment_ratchet_baseline_does_not_escalate_to_full_gate(self) -> None:
        self.assertEqual(CLASSIFIER.classify(["scripts/ci/comment_ratchet_baseline.txt"]), "fast")

    def test_classifier_script_itself_still_uses_full_gate(self) -> None:
        # 否定用例：分类器自身的 .py 改动可能改变判定逻辑，必须继续提级，
        # 不能因为「登记了一个数据文件豁免」就连带放松脚本改动。
        self.assertEqual(CLASSIFIER.classify(["scripts/ci/classify_story_changes.py"]), "full")

    def test_size_ratchet_checker_script_still_uses_full_gate(self) -> None:
        # 否定用例：与被登记豁免的 size_ratchet_baseline.txt 同源但不同性质——
        # 这是核对该数据文件的检查脚本本身，改动可能改变门禁判定，必须继续提级。
        self.assertEqual(CLASSIFIER.classify(["scripts/ci/check_size_ratchet.py"]), "full")

    def test_function_size_ratchet_checker_script_still_uses_full_gate(self) -> None:
        self.assertEqual(CLASSIFIER.classify(["scripts/ci/check_function_size_ratchet.py"]), "full")

    def test_comment_ratchet_checker_script_still_uses_full_gate(self) -> None:
        self.assertEqual(CLASSIFIER.classify(["scripts/ci/check_comment_ratchet.py"]), "full")

    def test_ci_gate_shell_script_still_uses_full_gate(self) -> None:
        # 否定用例：scripts/ci/ 下的门禁 .sh 改动同样必须继续提级。
        self.assertEqual(CLASSIFIER.classify(["scripts/ci/verify_repository.sh"]), "full")

    def test_unregistered_ci_data_looking_file_still_uses_full_gate(self) -> None:
        # 防御默认：清单外的 scripts/ci/ 新文件，哪怕文件名看起来也像纯数据，
        # 默认仍然提级——不能靠「看起来像数据」自动放行，必须显式登记才能豁免，
        # 否则新增一个未登记的高风险文件会被静默当成安全路径。
        self.assertEqual(CLASSIFIER.classify(["scripts/ci/another_baseline.txt"]), "full")

    def test_registered_data_file_alongside_another_full_path_still_escalates(self) -> None:
        # 同一次改动里只要还有别的高风险路径，仍以最高风险为准——登记豁免不会
        # 连带放行同一个 PR 里 scripts/ci/ 下的其他改动。
        self.assertEqual(
            CLASSIFIER.classify(
                ["scripts/ci/size_ratchet_baseline.txt", "scripts/ci/verify_repository.sh"]
            ),
            "full",
        )

    def test_unknown_path_fails_closed_to_full(self) -> None:
        self.assertEqual(CLASSIFIER.classify(["new-top-level/config.toml"]), "full")

    def test_empty_diff_fails_closed_to_full(self) -> None:
        self.assertEqual(CLASSIFIER.classify([]), "full")

    def test_markdown_inside_a_high_risk_directory_still_uses_full_gate(self) -> None:
        for path in ("deploy/README.md", "migrations/README.md", "scripts/ci/README.md"):
            with self.subTest(path=path):
                self.assertEqual(CLASSIFIER.classify([path]), "full")

    def test_unlisted_markdown_is_not_silently_treated_as_docs(self) -> None:
        self.assertEqual(CLASSIFIER.classify(["src/lingxi/prompts/runtime.md"]), "fast")

    def test_filename_whitespace_cannot_disguise_an_unknown_path_as_docs(self) -> None:
        self.assertEqual(CLASSIFIER.classify([" docs/验收.md"]), "full")
        self.assertEqual(CLASSIFIER.classify(["README.md "]), "full")
        self.assertEqual(CLASSIFIER.classify(["docs/换\n行.md"]), "docs")

    def test_deleted_high_risk_path_is_kept_with_a_mixed_docs_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "deploy").mkdir()
            (repository / "docs").mkdir()
            (repository / "deploy/compose.yaml").write_text("services: {}\n", encoding="utf-8")
            (repository / "docs/note.md").write_text("before\n", encoding="utf-8")
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
            (repository / "deploy/compose.yaml").unlink()
            (repository / "docs/note.md").write_text("after\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=repository, check=True)
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

            paths = CLASSIFIER.changed_paths(base, head, repository=repository)

        self.assertEqual(paths, ["deploy/compose.yaml", "docs/note.md"])
        self.assertEqual(CLASSIFIER.classify(paths), "full")

    def test_high_risk_rename_keeps_both_old_and_new_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "scripts/ci").mkdir(parents=True)
            (repository / "docs").mkdir()
            (repository / "scripts/ci/check.py").write_text("pass\n", encoding="utf-8")
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
            (repository / "scripts/ci/check.py").rename(repository / "docs/check.py")
            subprocess.run(["git", "add", "-A"], cwd=repository, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-qm",
                    "rename",
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

            paths = CLASSIFIER.changed_paths(base, head, repository=repository)

        self.assertEqual(paths, ["docs/check.py", "scripts/ci/check.py"])
        self.assertEqual(CLASSIFIER.classify(paths), "full")

    def test_non_ascii_docs_path_is_not_quoted_into_full_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "docs/参考证据").mkdir(parents=True)
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "--allow-empty",
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
            (repository / "docs/参考证据/验收.md").write_text("通过\n", encoding="utf-8")
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
                    "docs",
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

            paths = CLASSIFIER.changed_paths(base, head, repository=repository)

        self.assertEqual(paths, ["docs/参考证据/验收.md"])
        self.assertEqual(CLASSIFIER.classify(paths), "docs")

    def test_leading_space_is_preserved_by_real_git_diff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / " docs").mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "--allow-empty",
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
            (repository / " docs/验收.md").write_text("未知目录\n", encoding="utf-8")
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
                    "space",
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

            paths = CLASSIFIER.changed_paths(base, head, repository=repository)

        self.assertEqual(paths, [" docs/验收.md"])
        self.assertEqual(CLASSIFIER.classify(paths), "full")

    def test_file_to_symlink_type_change_remains_visible_and_high_risk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "scripts/ci").mkdir(parents=True)
            changed = repository / "scripts/ci/check.py"
            changed.write_text("pass\n", encoding="utf-8")
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
            changed.unlink()
            changed.symlink_to("target.py")
            subprocess.run(["git", "add", "-A"], cwd=repository, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-qm",
                    "type",
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

            paths = CLASSIFIER.changed_paths(base, head, repository=repository)

        self.assertEqual(paths, ["scripts/ci/check.py"])
        self.assertEqual(CLASSIFIER.classify(paths), "full")


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
