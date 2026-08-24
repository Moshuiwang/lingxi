"""`scripts/dev/local_layer.py` 与 CI 分类器一致性的钉住用例（Issue #236）。

独立审查 F7/F8 指出上一版的两个问题，本文件是修复后的重写：

- **F7**：旧版本用「`LOCAL_LAYER._CLASSIFIER.classify(x)` 是否等于
  `CLASSIFIER.classify(x)`」比对一致性——但两者加载的是**同一个源文件**，对同一个
  输入 `x` 调用两份完全相同的代码，结果恒等，不可能失败，是一条不能证伪任何东西的
  恒真用例。本文件改为在**真实临时 git 仓库**里跑一段真实提交历史，独立调用
  `local_layer.classify_local()`（本机分层实际调用的入口，含它自己的 git diff
  取路径逻辑）与直接调用 `classify_story_changes.changed_paths()` +
  `classify_story_changes.classify()`（CI 实际调用的入口）两条**不同的代码路径**，
  比较它们对同一段真实提交历史的结论——如果 `local_layer.py` 的 git 取路径逻辑
  写错了（参数顺序、多传/漏传一个 ref、路径处理有 bug），这里会真的不一致而失败。
- **F8**：旧版本验证「未提交新文件也会被本机分层看到」的用例只在特定条件成立时
  才跑，合并后这个条件永远不满足，实跑会 `skipped`——一条被跳过的用例覆盖率是
  零，而这恰恰是本机分层与 CI 分类器**唯一**的行为差异点（CI 只比较两个已提交
  SHA，本机默认还要把未提交改动算进去）。本文件改用临时仓库构造未提交/未
  `git add` 的文件，无条件执行，不依赖仓库当前状态。
"""

from __future__ import annotations

import importlib.util
import subprocess
import tempfile
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
# 独立于 local_layer 内部加载的那一份，直接代表「CI 实际会跑什么」——用来对比，
# 不是用来断言「两份加载是同一个文件」（那条断言在下面单独测，且只测一次）。
CI_CLASSIFIER = _load(CLASSIFIER_SCRIPT, "ci_classifier_reference_for_comparison")


def _run_git(repository: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repository, check=True, capture_output=True, text=True
    )


def _init_repo(repository: Path) -> None:
    _run_git(repository, "init", "--quiet", "--initial-branch=main")
    _run_git(repository, "config", "user.email", "dev-check-test@example.invalid")
    _run_git(repository, "config", "user.name", "dev-check-test")


def _commit_file(repository: Path, relative_path: str, content: str, message: str) -> str:
    target = repository / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _run_git(repository, "add", relative_path)
    _run_git(repository, "commit", "--quiet", "-m", message)
    return _run_git(repository, "rev-parse", "HEAD").stdout.strip()


class LocalLayerLoadsTheRealClassifierFileTest(unittest.TestCase):
    """确认 local_layer 内部加载的确实是仓库里那一份 classify_story_changes.py。

    这条只断言「加载路径正确」，不断言「两份加载结果相等」——后者是 F7 指出的
    恒真陷阱，已经改到下面用真实仓库比对。
    """

    def test_local_layer_loads_the_same_classifier_file_not_a_rewrite(self) -> None:
        loaded_from = Path(LOCAL_LAYER._CLASSIFIER.__file__)
        self.assertEqual(loaded_from.resolve(), CLASSIFIER_SCRIPT.resolve())


class LocalLayerMatchesCiOnARealCommitHistoryTest(unittest.TestCase):
    """在真实临时仓库的一段提交历史上，独立对比两条不同的代码路径。

    左边：`local_layer.classify_local()`——本机 `check.sh` 实际调用的入口。
    右边：`classify_story_changes.changed_paths()` + `classify_story_changes.classify()`
    ——CI 的 `classify.yml` 步骤实际调用的入口。两条路径各自独立地做「git 取路径」
    与「分类」，只有当 local_layer 的取路径逻辑与 CI 的语义真正一致时，结论才会
    对每一段历史都相等。
    """

    def setUp(self) -> None:
        # `ignore_cleanup_errors=True`：临时目录里是一个真的 git 仓库，提交之后 git 可能
        # 拉起后台 auto-gc / maintenance 继续往 `.git/objects/pack` 写文件，`cleanup()`
        # 的 `rmdir` 撞上它就抛 `OSError: [Errno 39] Directory not empty`（2026-08-21
        # CI 的 Epic Full / gate 真打红过一次，落在下面那个同形状的类上）。它只影响
        # **测试自身的清理**，不改任何被测断言——清理不掉的临时目录交给系统的 /tmp 回收。
        self._tmp = tempfile.TemporaryDirectory(
            prefix="lingxi-dev-check-local-layer-", ignore_cleanup_errors=True
        )
        self.repo = Path(self._tmp.name)
        _init_repo(self.repo)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _assert_local_layer_matches_ci(self, base_sha: str) -> None:
        """对比两条独立代码路径在「base_sha..当前 HEAD 已提交差异」上的结论。"""

        local_mode = LOCAL_LAYER.classify_local(
            base_sha, include_worktree=False, repository=self.repo
        )
        ci_paths = CI_CLASSIFIER.changed_paths(base_sha, "HEAD", repository=self.repo)
        ci_mode = CI_CLASSIFIER.classify(ci_paths)
        self.assertEqual(
            local_mode,
            ci_mode,
            f"local_layer 与 CI 分类器对同一段提交历史（{base_sha}..HEAD）结论不一致",
        )

    def test_docs_only_commit_matches(self) -> None:
        initial_sha = _commit_file(self.repo, "README.md", "# 占位\n", "初始提交")
        _commit_file(self.repo, "docs/notes.md", "占位文档\n", "只改文档")
        self._assert_local_layer_matches_ci(initial_sha)

    def test_source_only_commit_matches(self) -> None:
        docs_sha = _commit_file(self.repo, "docs/notes.md", "占位文档\n", "文档基线")
        _commit_file(self.repo, "src/lingxi/thing.py", "x = 1\n", "只改源码")
        self._assert_local_layer_matches_ci(docs_sha)

    def test_high_risk_pyproject_change_matches(self) -> None:
        src_sha = _commit_file(self.repo, "src/lingxi/thing.py", "x = 1\n", "源码基线")
        _commit_file(self.repo, "pyproject.toml", "[project]\nname='x'\n", "改依赖声明")
        self._assert_local_layer_matches_ci(src_sha)

    def test_unknown_top_level_path_fails_closed_together(self) -> None:
        pyproject_sha = _commit_file(self.repo, "pyproject.toml", "[project]\n", "依赖基线")
        _commit_file(self.repo, "new-top-level/config.toml", "a = 1\n", "未知顶层目录")
        self._assert_local_layer_matches_ci(pyproject_sha)

    def test_ci_data_file_exception_matches(self) -> None:
        # Issue #298：scripts/ci/ 下登记豁免的纯数据文件改动不提级，本机分层
        # 必须自动跟随（复用同一份 classify()，不是另写一份判定）。
        src_sha = _commit_file(self.repo, "src/lingxi/thing.py", "x = 1\n", "源码基线")
        _commit_file(
            self.repo,
            "scripts/ci/size_ratchet_baseline.txt",
            "100\tsrc/lingxi/thing.py\n",
            "刷新体量棘轮基线",
        )
        self._assert_local_layer_matches_ci(src_sha)
        # 不只是两条路径互相一致，也确认这次改动真的没有被提级——否则「两边
        # 一致」有可能是两边都错误地判成了 full。
        mode = LOCAL_LAYER.classify_local(src_sha, include_worktree=False, repository=self.repo)
        self.assertEqual(mode, "fast")

    def test_mixed_docs_and_source_in_one_commit_matches(self) -> None:
        initial_sha = _commit_file(self.repo, "README.md", "# 占位\n", "初始提交")
        target = self.repo / "docs" / "notes.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("占位文档\n", encoding="utf-8")
        source = self.repo / "src" / "lingxi" / "thing.py"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("x = 1\n", encoding="utf-8")
        _run_git(self.repo, "add", "docs/notes.md", "src/lingxi/thing.py")
        _run_git(self.repo, "commit", "--quiet", "-m", "同一提交混合文档与源码")
        self._assert_local_layer_matches_ci(initial_sha)


class UncommittedChangesAreIncludedOnlyWhenAskedTest(unittest.TestCase):
    """F8：本机分层与 CI 分类器唯一的行为差异——未提交改动是否算进去——必须无条件覆盖。

    用临时仓库构造：已提交的基线、一次已提交的改动、以及**未 `git add` 过的新文件**
    与**已修改未暂存的既有文件**。不依赖本仓库当前工作树状态，因此不会被跳过。
    """

    def setUp(self) -> None:
        # `ignore_cleanup_errors=True`：**2026-08-21 CI 的 Epic Full / gate 就是在这里
        # 打红的**——`tearDown` 的 `cleanup()` 与 git 提交后的后台进程抢
        # `.git/objects/pack`，抛 `OSError: [Errno 39] Directory not empty`。用例体的三条
        # 断言全部通过，`unittest` 只是把 `tearDown` 的异常算在了用例名下。理由同上一个
        # 类，只影响测试自身清理，不改被测断言。
        self._tmp = tempfile.TemporaryDirectory(
            prefix="lingxi-dev-check-worktree-", ignore_cleanup_errors=True
        )
        self.repo = Path(self._tmp.name)
        _init_repo(self.repo)
        self.base_sha = _commit_file(self.repo, "README.md", "# 占位\n", "初始提交")
        _commit_file(self.repo, "docs/notes.md", "占位文档\n", "已提交的文档改动")

        # 未提交、未 add 过的新文件——`git diff` 默认看不到它。
        untracked = self.repo / "src" / "lingxi" / "new_thing.py"
        untracked.parent.mkdir(parents=True, exist_ok=True)
        untracked.write_text("y = 2\n", encoding="utf-8")

        # 已提交文件的未暂存修改。
        (self.repo / "docs" / "notes.md").write_text("改过还没提交\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_committed_only_mode_sees_only_the_committed_commit(self) -> None:
        paths = LOCAL_LAYER.changed_paths_against(
            self.base_sha, include_worktree=False, repository=self.repo
        )
        self.assertEqual(paths, ["docs/notes.md"])

    def test_include_worktree_adds_the_untracked_and_the_unstaged_modification(self) -> None:
        paths = LOCAL_LAYER.changed_paths_against(
            self.base_sha, include_worktree=True, repository=self.repo
        )
        self.assertIn("docs/notes.md", paths)
        self.assertIn("src/lingxi/new_thing.py", paths)

    def test_classify_local_with_worktree_reflects_the_uncommitted_source_change(self) -> None:
        # 未提交改动只有 src/**（fast 前缀）与已提交的 docs/**，两者都是
        # 「文档或快速」路径，因此结论仍是 fast——但必须真的把未提交那一个文件
        # 纳入判断依据，而不是只看已提交的那一次 docs 改动。
        mode = LOCAL_LAYER.classify_local(
            self.base_sha, include_worktree=True, repository=self.repo
        )
        self.assertEqual(mode, "fast")


class LocalLayerAgainstThisRepositoryTest(unittest.TestCase):
    """用本仓库自身跑一次真实调用路径，确认对真实 git 环境不会异常退出。"""

    def test_empty_diff_against_head_fails_closed_to_full_like_ci(self) -> None:
        mode = LOCAL_LAYER.classify_local("HEAD", include_worktree=False, repository=REPO_ROOT)
        self.assertEqual(mode, "full")

    def test_changed_paths_against_head_is_empty_without_worktree_changes(self) -> None:
        paths = LOCAL_LAYER.changed_paths_against(
            "HEAD", include_worktree=False, repository=REPO_ROOT
        )
        self.assertEqual(paths, [])


if __name__ == "__main__":
    unittest.main()
