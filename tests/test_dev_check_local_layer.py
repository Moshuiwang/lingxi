"""`scripts/dev/local_layer.py` 与 CI 分类器一致性的钉住用例（Issue #236）。

「一致」在这里不是「今天恰好算出同一个字符串」，而是「本机分层判定压根就是同一段
代码在跑」：`local_layer._CLASSIFIER` 直接按路径加载
`scripts/ci/classify_story_changes.py`，不重写判定规则。第一条用例断言这一点
（模块文件路径相同）；其余用例复用 `tests/test_ci_layering.py` 已经覆盖过的代表性
改动集合，逐一核对两侧调用同一批路径时结论相同——这条用例组因此会随
`classify_story_changes.classify` 的规则变化自动保持同步，不需要人工维护第二份
判定表。
"""

from __future__ import annotations

import importlib.util
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
LOCAL_LAYER_SCRIPT = REPO_ROOT / "scripts" / "dev" / "local_layer.py"
CLASSIFIER_SCRIPT = REPO_ROOT / "scripts" / "ci" / "classify_story_changes.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


LOCAL_LAYER = _load(LOCAL_LAYER_SCRIPT, "local_layer_under_test")
CLASSIFIER = _load(CLASSIFIER_SCRIPT, "classifier_loaded_independently_for_comparison")

# 与 tests/test_ci_layering.py 的代表性用例保持同一组路径，覆盖 docs / fast / full
# 三种结论、未知路径与空改动两种兜底。
REPRESENTATIVE_PATH_SETS: list[list[str]] = [
    ["docs/协作约定.md", "AGENTS.md"],
    ["src/lingxi/core/ids.py", "tests/test_core_ids.py"],
    [".github/workflows/story.yml"],
    ["scripts/ci/verify_repository.sh"],
    ["deploy/compose.yaml"],
    ["migrations/versions/x.py"],
    ["Dockerfile"],
    ["pyproject.toml"],
    ["new-top-level/config.toml"],
    [],
    ["docs/x.md", "src/lingxi/core/ids.py"],
    ["scripts/dev/check.sh", "scripts/dev/gate_spec.py"],
]


class LocalLayerReusesCiClassifierTest(unittest.TestCase):
    def test_local_layer_loads_the_same_classifier_file_not_a_rewrite(self) -> None:
        loaded_from = Path(LOCAL_LAYER._CLASSIFIER.__file__)
        self.assertEqual(loaded_from.resolve(), CLASSIFIER_SCRIPT.resolve())

    def test_matches_ci_classifier_on_representative_change_sets(self) -> None:
        for paths in REPRESENTATIVE_PATH_SETS:
            with self.subTest(paths=paths):
                self.assertEqual(
                    LOCAL_LAYER._CLASSIFIER.classify(paths),
                    CLASSIFIER.classify(paths),
                )


class LocalLayerAgainstRealRepositoryTest(unittest.TestCase):
    """用真实仓库自身验证一次实际的 git 调用路径（不是只测纯函数）。"""

    def test_empty_diff_against_head_fails_closed_to_full_like_ci(self) -> None:
        mode = LOCAL_LAYER.classify_local("HEAD", include_worktree=False, repository=REPO_ROOT)
        self.assertEqual(mode, "full")

    def test_changed_paths_against_head_is_empty_without_worktree_changes(self) -> None:
        paths = LOCAL_LAYER.changed_paths_against(
            "HEAD", include_worktree=False, repository=REPO_ROOT
        )
        self.assertEqual(paths, [])

    def test_untracked_new_files_are_counted_when_include_worktree_is_true(self) -> None:
        """`git diff` 默认看不到尚未 `git add` 的新文件；本函数必须能看到。"""

        result = subprocess.run(
            ["git", "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        # 本用例本身就是这次改动新增的未提交文件之一（若已提交则跳过，不构造假前提）。
        if not any(line.endswith("test_dev_check_local_layer.py") for line in result.stdout.splitlines()):
            self.skipTest("本文件已提交，无法用它验证「未提交新文件也会被看到」这条行为")
        paths = LOCAL_LAYER.changed_paths_against("HEAD", include_worktree=True, repository=REPO_ROOT)
        self.assertIn("tests/test_dev_check_local_layer.py", paths)


if __name__ == "__main__":
    unittest.main()
