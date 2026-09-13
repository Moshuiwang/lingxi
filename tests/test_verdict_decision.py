"""裁决层 `Epic Verdict` 的钉住用例：判定纯函数、两份工作流的形状、复跑上下文的调度。

三组断言各挡一类退化：
1. `scripts/ci/verdict_decision.py` 的每条规则——事件 / 路径先决、外部仓库判红、
   路径 a 只认 `success`（`skipped` / `neutral` / `cancelled` 都判红）、路径 b 等复跑、
   `pull_requests[]` 为空的基线回退、check run 的查找与请求体。
2. `.github/workflows/verdict.yml` 的安全形状——只由 `workflow_run` 触发、顶层只读、
   App 私钥只进 Environment `verdict` 那一个作业、写检查不用 GITHUB_TOKEN、本工作流
   自己的 checkout 从不检出 PR 头提交、复跑经 `test_ref` 交给 main 版 `ci.yml`。
3. `.github/workflows/ci.yml` 的 `test_ref` 消费——每个 checkout 步都带、复跑上下文
   （event_name = workflow_run）下 classify 跳过、gate / extras / image 照跑、candidate
   仍要求三者全 success 且不写候选证明。

变异实测：把 `map_run_conclusion` 改成 `conclusion in ("success", "skipped")`，
`test_path_a_only_success_passes` 应判红；还原后清 `__pycache__` 复跑应绿。
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/ci/verdict_decision.py"
VERDICT_WORKFLOW = ROOT / ".github/workflows/verdict.yml"
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
STORY_WORKFLOW = ROOT / ".github/workflows/story.yml"
SCHEDULING_TESTS = ROOT / "tests/test_ci_dispatch_scheduling.py"

sys.path.insert(0, str(ROOT / "scripts/ci"))
vd = importlib.import_module("verdict_decision")

REPOSITORY = "Moshuiwang/lingxi"
HEAD = "a" * 40
BASE = "b" * 40
TREE_SAME = "c" * 40
TREE_OTHER = "d" * 40


def _load_by_path(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def event(
    *,
    run_event="pull_request",
    path=vd.TRIGGER_WORKFLOW_PATH,
    head_repository=REPOSITORY,
    conclusion="success",
    pull_requests=None,
    head_sha=HEAD,
):
    if pull_requests is None:
        pull_requests = [pull_request()]
    run = {
        "id": 123456,
        "event": run_event,
        "path": path,
        "head_sha": head_sha,
        "head_branch": "feat/x",
        "conclusion": conclusion,
        "html_url": "https://github.com/Moshuiwang/lingxi/actions/runs/123456",
        "pull_requests": pull_requests,
        "head_repository": None if head_repository is None else {"full_name": head_repository},
    }
    return {
        "workflow_run": run,
        "repository": {"full_name": REPOSITORY, "default_branch": "main"},
    }


def pull_request(number=700, base_ref="main", base_sha=BASE, head_sha=HEAD):
    return {"number": number, "base": {"ref": base_ref, "sha": base_sha}, "head": {"sha": head_sha}}


def facts(**kwargs):
    return vd.parse_run_facts(event(**kwargs))


def decide(head_tree=TREE_SAME, base_tree=TREE_SAME, default_tree=TREE_SAME, **kwargs):
    run_facts = facts(**kwargs)
    trees = vd.WorkflowTrees(head=head_tree, base=base_tree, default_branch=default_tree)
    return vd.decide(run_facts, vd.select_baseline(run_facts), trees)


class DecisionRulesTest(unittest.TestCase):
    def test_non_pull_request_events_are_not_judged(self):
        for run_event in ("workflow_dispatch", "push", "workflow_call", "schedule", ""):
            with self.subTest(run_event=run_event):
                verdict = decide(run_event=run_event)
                self.assertEqual(verdict.path, vd.PATH_SKIP)
                self.assertFalse(verdict.write_check)
                self.assertIsNone(verdict.conclusion)
                self.assertIn("不裁决", verdict.summary)

    def test_runs_from_other_workflow_files_are_not_judged(self):
        for path in (".github/workflows/fake.yml", ".github/workflows/ci2.yml", ""):
            with self.subTest(path=path):
                verdict = decide(path=path)
                self.assertEqual(verdict.path, vd.PATH_SKIP)
                self.assertFalse(verdict.write_check)

    def test_fork_head_is_rejected_even_with_identical_trees(self):
        for head_repository in ("someone/lingxi", "Moshuiwang/other", None, ""):
            with self.subTest(head_repository=head_repository):
                verdict = decide(head_repository=head_repository)
                self.assertEqual(verdict.path, vd.PATH_REJECT)
                self.assertTrue(verdict.write_check)
                self.assertEqual(verdict.conclusion, vd.FAILURE)
                self.assertIn("外部仓库不裁决", verdict.summary)

    def test_rules_apply_in_order_event_before_fork(self):
        verdict = decide(run_event="workflow_dispatch", head_repository="someone/lingxi")
        self.assertEqual(verdict.path, vd.PATH_SKIP)

    def test_path_a_only_success_passes(self):
        self.assertEqual(decide(conclusion="success").conclusion, vd.SUCCESS)
        for conclusion in (
            "failure",
            "cancelled",
            "timed_out",
            "skipped",
            "neutral",
            "action_required",
            "stale",
            "startup_failure",
            "",
            None,
        ):
            with self.subTest(conclusion=conclusion):
                verdict = decide(conclusion=conclusion)
                self.assertEqual(verdict.path, vd.PATH_A)
                self.assertTrue(verdict.write_check)
                self.assertEqual(verdict.conclusion, vd.FAILURE)

    def test_path_a_summary_names_run_and_trees(self):
        verdict = decide()
        self.assertIn("路径 a", verdict.summary)
        self.assertIn("123456", verdict.summary)
        self.assertIn(TREE_SAME, verdict.summary)
        self.assertIn("main@" + BASE[:12], verdict.summary)

    def test_changed_workflow_tree_takes_path_b_and_waits_for_rerun(self):
        for head_tree, base_tree, default_tree in (
            (TREE_OTHER, TREE_SAME, TREE_SAME),
            (None, TREE_SAME, TREE_SAME),
            ("", TREE_SAME, TREE_SAME),
            (TREE_SAME, None, None),
            (TREE_SAME, "", ""),
            (None, None, None),
            (TREE_OTHER, TREE_SAME, "e" * 40),
        ):
            with self.subTest(head=head_tree, base=base_tree, default=default_tree):
                verdict = decide(
                    head_tree=head_tree,
                    base_tree=base_tree,
                    default_tree=default_tree,
                    conclusion="success",
                )
                self.assertEqual(verdict.path, vd.PATH_B)
                self.assertTrue(verdict.write_check)
                self.assertIsNone(verdict.conclusion)
                self.assertIn("不采信", verdict.summary)

    def test_head_identical_to_default_branch_takes_path_a_even_if_base_differs(self):
        """main → release/** 的同步 PR：头提交已是默认分支的定义，复跑与自身运行等价。"""

        verdict = decide(
            head_tree=TREE_SAME,
            base_tree=TREE_OTHER,
            default_tree=TREE_SAME,
            conclusion="success",
            pull_requests=[pull_request(base_ref="release/2.5", base_sha="1" * 40)],
        )
        self.assertEqual((verdict.path, verdict.conclusion), (vd.PATH_A, vd.SUCCESS))
        self.assertIn("与默认分支当前提交相同", verdict.summary)
        failed = decide(
            head_tree=TREE_SAME, base_tree=TREE_OTHER, default_tree=TREE_SAME, conclusion="failure"
        )
        self.assertEqual((failed.path, failed.conclusion), (vd.PATH_A, vd.FAILURE))

    def test_empty_head_tree_never_matches_an_empty_default_tree(self):
        for head_tree, default_tree in ((None, None), ("", ""), ("", None), (None, "")):
            with self.subTest(head=head_tree, default=default_tree):
                verdict = decide(
                    head_tree=head_tree, base_tree=TREE_OTHER, default_tree=default_tree
                )
                self.assertEqual(verdict.path, vd.PATH_B)

    def test_path_b_success_of_own_run_is_never_trusted(self):
        verdict = decide(head_tree=TREE_OTHER, conclusion="success")
        self.assertIsNone(verdict.conclusion)
        self.assertEqual(
            vd.finalize(verdict.path, None, "failure", verdict.summary).conclusion, vd.FAILURE
        )


class FinalizeTest(unittest.TestCase):
    def test_path_b_only_rerun_success_passes(self):
        self.assertEqual(vd.finalize(vd.PATH_B, None, "success", "s").conclusion, vd.SUCCESS)
        for rerun_result in ("failure", "cancelled", "skipped", "timed_out", "", None):
            with self.subTest(rerun_result=rerun_result):
                verdict = vd.finalize(vd.PATH_B, None, rerun_result, "s")
                self.assertEqual(verdict.conclusion, vd.FAILURE)
                self.assertIn("复跑结果", verdict.summary)

    def test_path_a_and_reject_keep_their_conclusion(self):
        self.assertEqual(vd.finalize(vd.PATH_A, "success", "skipped", "s").conclusion, vd.SUCCESS)
        self.assertEqual(vd.finalize(vd.PATH_A, "failure", "success", "s").conclusion, vd.FAILURE)
        self.assertEqual(
            vd.finalize(vd.PATH_REJECT, "failure", "success", "s").conclusion, vd.FAILURE
        )

    def test_skip_or_missing_conclusion_cannot_be_finalized(self):
        for path, conclusion in (
            (vd.PATH_SKIP, None),
            (vd.PATH_A, None),
            (vd.PATH_A, "neutral"),
            ("x", "success"),
        ):
            with self.subTest(path=path, conclusion=conclusion):
                with self.assertRaises(ValueError):
                    vd.finalize(path, conclusion, "success", "s")


class BaselineSelectionTest(unittest.TestCase):
    def test_pull_request_to_unprotected_base_is_ignored(self):
        """base 不是默认分支也不是 release/**：不能当基线，也不贴标签（pr 为空）。"""

        for base_ref in (
            "x-attacker",
            "feat/other",
            "epic/a",
            "trace/770-w1",
            "main-2",
            "releases/1",
        ):
            with self.subTest(base_ref=base_ref):
                run_facts = facts(pull_requests=[pull_request(number=5, base_ref=base_ref)])
                baseline = vd.select_baseline(run_facts)
                self.assertIsNone(baseline.pull_request)
                self.assertEqual((baseline.base_ref, baseline.base_sha), ("main", ""))
        self.assertTrue(vd.is_protected_base("main", "main"))
        self.assertTrue(vd.is_protected_base("release/2.5", "main"))
        self.assertFalse(vd.is_protected_base("x-attacker", "main"))
        self.assertFalse(vd.is_protected_base("", "main"))

    def test_attacker_branch_pull_request_cannot_shadow_the_main_pull_request(self):
        """[PR→x-attacker, PR→main]：x 的子树与头提交相同也不算数，按 main 比较走路径 b。"""

        attacker = pull_request(number=1, base_ref="x-attacker", base_sha="1" * 40)
        real = pull_request(number=2, base_ref="main", base_sha=BASE)
        run_facts = facts(pull_requests=[attacker, real], conclusion="success")
        baseline = vd.select_baseline(run_facts)
        self.assertEqual(baseline.pull_request.number, 2)
        trees = vd.WorkflowTrees(head=TREE_OTHER, base=TREE_SAME, default_branch=TREE_SAME)
        verdict = vd.decide(run_facts, baseline, trees)
        self.assertEqual((verdict.path, verdict.conclusion), (vd.PATH_B, None))

    def test_only_attacker_branch_pull_request_falls_back_to_default_branch(self):
        """只有 PR→x：基线是默认分支，头提交与默认分支的子树不同就复跑。"""

        run_facts = facts(
            pull_requests=[pull_request(number=1, base_ref="x-attacker", base_sha="1" * 40)]
        )
        baseline = vd.select_baseline(run_facts)
        self.assertIsNone(baseline.pull_request)
        self.assertEqual(baseline.label, "main")
        trees = vd.WorkflowTrees(head=TREE_OTHER, base=None, default_branch=TREE_SAME)
        self.assertEqual(vd.decide(run_facts, baseline, trees).path, vd.PATH_B)
        same = vd.WorkflowTrees(head=TREE_SAME, base=None, default_branch=TREE_SAME)
        self.assertEqual(vd.decide(run_facts, baseline, same).path, vd.PATH_A)

    def test_default_branch_base_is_preferred_over_release_base(self):
        release = pull_request(number=1, base_ref="release/2.5", base_sha="1" * 40, head_sha=HEAD)
        main = pull_request(number=2, base_ref="main", base_sha=BASE, head_sha="9" * 40)
        baseline = vd.select_baseline(facts(pull_requests=[release, main]))
        self.assertEqual(baseline.pull_request.number, 2)
        only_release = vd.select_baseline(facts(pull_requests=[release]))
        self.assertEqual(
            (only_release.pull_request.number, only_release.base_ref), (1, "release/2.5")
        )

    def test_fallback_lookup_obeys_the_same_base_rule(self):
        fallback = [
            pull_request(number=11, base_ref="x-attacker", base_sha="1" * 40),
            pull_request(number=12, base_ref="main", base_sha=BASE),
        ]
        baseline = vd.select_baseline(vd.parse_run_facts(event(pull_requests=[]), fallback))
        self.assertEqual(baseline.pull_request.number, 12)
        only_bad = vd.select_baseline(vd.parse_run_facts(event(pull_requests=[]), fallback[:1]))
        self.assertIsNone(only_bad.pull_request)

    def test_prefers_pull_request_whose_head_matches_run(self):
        stale = pull_request(number=1, base_ref="release/2.5", base_sha="1" * 40, head_sha="9" * 40)
        current = pull_request(number=2, base_ref="main", base_sha=BASE, head_sha=HEAD)
        baseline = vd.select_baseline(facts(pull_requests=[stale, current]))
        self.assertEqual(baseline.pull_request.number, 2)
        self.assertEqual((baseline.base_ref, baseline.base_sha), ("main", BASE))
        self.assertEqual(baseline.label, "main@" + BASE[:12])

    def test_falls_back_to_first_pull_request_when_no_head_matches(self):
        stale = pull_request(number=1, base_ref="release/2.5", base_sha="1" * 40, head_sha="9" * 40)
        baseline = vd.select_baseline(facts(pull_requests=[stale]))
        self.assertEqual(baseline.pull_request.number, 1)
        self.assertEqual(baseline.base_ref, "release/2.5")

    def test_empty_pull_requests_uses_fallback_lookup_then_default_branch(self):
        fallback = [pull_request(number=42, base_ref="main", base_sha=BASE)]
        with_fallback = vd.parse_run_facts(event(pull_requests=[]), fallback)
        self.assertEqual([pr.number for pr in with_fallback.pull_requests], [42])
        without = vd.parse_run_facts(event(pull_requests=[]), [])
        baseline = vd.select_baseline(without)
        self.assertIsNone(baseline.pull_request)
        self.assertEqual(
            (baseline.base_ref, baseline.base_sha, baseline.label), ("main", "", "main")
        )
        trees = vd.WorkflowTrees(head=TREE_SAME, base=TREE_SAME, default_branch=TREE_SAME)
        verdict = vd.decide(without, baseline, trees)
        self.assertEqual((verdict.path, verdict.conclusion), (vd.PATH_A, vd.SUCCESS))

    def test_fallback_is_ignored_when_event_already_lists_pull_requests(self):
        fallback = [pull_request(number=42)]
        run_facts = vd.parse_run_facts(event(pull_requests=[pull_request(number=7)]), fallback)
        self.assertEqual([pr.number for pr in run_facts.pull_requests], [7])


class TreeDiffAndCheckRunTest(unittest.TestCase):
    def test_workflow_tree_diff_reports_added_removed_modified_only(self):
        base = [
            {"path": "ci.yml", "sha": "1", "type": "blob"},
            {"path": "story.yml", "sha": "2", "type": "blob"},
            {"path": "old.yml", "sha": "3", "type": "blob"},
            {"path": "sub", "sha": "9", "type": "tree"},
        ]
        head = [
            {"path": "ci.yml", "sha": "1x", "type": "blob"},
            {"path": "story.yml", "sha": "2", "type": "blob"},
            {"path": "new.yml", "sha": "4", "type": "blob"},
            {"path": "sub", "sha": "8", "type": "tree"},
        ]
        self.assertEqual(
            vd.workflow_tree_diff(base, head),
            ["修改 `ci.yml`", "新增 `new.yml`", "删除 `old.yml`"],
        )
        self.assertEqual(vd.workflow_tree_diff(base, base), [])

    def test_existing_check_run_only_matches_same_app_and_name(self):
        runs = [
            {"id": 5, "name": vd.CHECK_NAME, "app": {"slug": "github-actions"}},
            {"id": 6, "name": "Epic Verdict / write", "app": {"slug": "lingxi-verdict"}},
            {"id": 7, "name": vd.CHECK_NAME, "app": {"slug": "lingxi-verdict"}},
            {"id": 9, "name": vd.CHECK_NAME, "app": {"slug": "lingxi-verdict"}},
            {"id": 8, "name": vd.CHECK_NAME, "app": None},
        ]
        self.assertEqual(vd.existing_check_run_id(runs, "lingxi-verdict"), 9)
        self.assertIsNone(vd.existing_check_run_id(runs, "other-app"))
        self.assertIsNone(vd.existing_check_run_id([], "lingxi-verdict"))

    def test_check_run_payload_shapes(self):
        spec = vd.CheckRunSpec(HEAD, vd.SUCCESS, "https://x/run/1", "1", "摘要")
        created = vd.check_run_payload(spec, update=False)
        self.assertEqual(created["name"], vd.CHECK_NAME)
        self.assertEqual(created["head_sha"], HEAD)
        self.assertEqual((created["status"], created["conclusion"]), ("completed", vd.SUCCESS))
        self.assertEqual(created["output"]["summary"], "摘要")
        updated = vd.check_run_payload(spec, update=True)
        self.assertNotIn("head_sha", updated)
        self.assertEqual(updated["conclusion"], vd.SUCCESS)
        for bad in ("neutral", "skipped", "", "pending"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    vd.check_run_payload(vd.CheckRunSpec(HEAD, bad, "u", "1", "s"), update=False)
        with self.assertRaises(ValueError):
            vd.check_run_payload(vd.CheckRunSpec("", vd.SUCCESS, "u", "1", "s"), update=False)


class CommandLineTest(unittest.TestCase):
    """命令行入口按工作流的调用方式各跑一遍（`python -B`，不留字节码）。"""

    def run_cli(self, *args, stdin_text=None):
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), *args],
            input=stdin_text,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    @staticmethod
    def parse_outputs(path: Path) -> dict[str, str]:
        outputs: dict[str, str] = {}
        lines = path.read_text(encoding="utf-8").splitlines()
        index = 0
        while index < len(lines):
            key, _, delimiter = lines[index].partition("<<")
            end = lines.index(delimiter, index + 1)
            outputs[key] = "\n".join(lines[index + 1 : end])
            index = end + 1
        return outputs

    def test_plan_then_decide_path_b_writes_outputs_and_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            (temp / "event.json").write_text(
                json.dumps(event(conclusion="success")), encoding="utf-8"
            )
            (temp / "fallback.json").write_text("[]", encoding="utf-8")
            (temp / "base.json").write_text(
                json.dumps([{"path": "ci.yml", "sha": "1", "type": "blob"}]), encoding="utf-8"
            )
            (temp / "head.json").write_text(
                json.dumps([{"path": "ci.yml", "sha": "2", "type": "blob"}]), encoding="utf-8"
            )
            plan_output = temp / "plan.txt"
            self.run_cli(
                "plan",
                "--event-file",
                str(temp / "event.json"),
                "--fallback-pulls-file",
                str(temp / "fallback.json"),
                "--github-output",
                str(plan_output),
            )
            plan = self.parse_outputs(plan_output)
            self.assertEqual(plan["needs_trees"], "true")
            self.assertEqual(
                (plan["head_sha"], plan["pr_number"], plan["base_sha"]), (HEAD, "700", BASE)
            )
            decide_output = temp / "decide.txt"
            self.run_cli(
                "decide",
                "--event-file",
                str(temp / "event.json"),
                "--fallback-pulls-file",
                str(temp / "fallback.json"),
                "--head-tree",
                TREE_OTHER,
                "--base-tree",
                TREE_SAME,
                "--default-tree",
                TREE_SAME,
                "--head-listing-file",
                str(temp / "head.json"),
                "--base-listing-file",
                str(temp / "base.json"),
                "--github-output",
                str(decide_output),
            )
            outputs = self.parse_outputs(decide_output)
            self.assertEqual(outputs["path"], vd.PATH_B)
            self.assertEqual(outputs["write_check"], "true")
            self.assertEqual(outputs["conclusion"], "")
            self.assertEqual(json.loads(outputs["diff"]), ["修改 `ci.yml`"])
            self.assertEqual(outputs["pr_number"], "700")
            final_output = temp / "final.txt"
            self.run_cli(
                "finalize",
                "--path",
                outputs["path"],
                "--conclusion",
                outputs["conclusion"],
                "--rerun-result",
                "cancelled",
                "--summary",
                outputs["summary"],
                "--github-output",
                str(final_output),
            )
            final = self.parse_outputs(final_output)
            self.assertEqual(final["conclusion"], vd.FAILURE)
            self.assertIn("复跑结果 cancelled → failure", final["summary"])

    def test_plan_skips_tree_lookup_for_non_pull_request_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            (temp / "event.json").write_text(
                json.dumps(event(run_event="workflow_dispatch", pull_requests=[])), encoding="utf-8"
            )
            output = temp / "plan.txt"
            self.run_cli(
                "plan", "--event-file", str(temp / "event.json"), "--github-output", str(output)
            )
            plan = self.parse_outputs(output)
            self.assertEqual(plan["needs_trees"], "false")
            self.assertEqual(plan["pr_number"], "")
            decide_output = temp / "decide.txt"
            self.run_cli(
                "decide",
                "--event-file",
                str(temp / "event.json"),
                "--github-output",
                str(decide_output),
            )
            outputs = self.parse_outputs(decide_output)
            self.assertEqual((outputs["path"], outputs["write_check"]), (vd.PATH_SKIP, "false"))

    def test_check_run_existing_and_payload_commands(self):
        listing = json.dumps(
            {
                "check_runs": [
                    {"id": 11, "name": vd.CHECK_NAME, "app": {"slug": "github-actions"}},
                    {"id": 12, "name": vd.CHECK_NAME, "app": {"slug": "lingxi-verdict"}},
                ]
            }
        )
        self.assertEqual(
            self.run_cli(
                "check-run", "existing", "--app-slug", "lingxi-verdict", stdin_text=listing
            ).strip(),
            "12",
        )
        self.assertEqual(
            self.run_cli(
                "check-run", "existing", "--app-slug", "nobody", stdin_text=listing
            ).strip(),
            "",
        )
        payload = json.loads(
            self.run_cli(
                "check-run",
                "payload",
                "--head-sha",
                HEAD,
                "--conclusion",
                vd.FAILURE,
                "--details-url",
                "https://x/run/9",
                "--external-id",
                "9",
                "--summary",
                "第一行\n第二行",
            )
        )
        self.assertEqual((payload["head_sha"], payload["conclusion"]), (HEAD, vd.FAILURE))
        self.assertEqual(payload["output"]["summary"], "第一行\n第二行")
        updated = json.loads(
            self.run_cli(
                "check-run",
                "payload",
                "--update",
                "--conclusion",
                vd.SUCCESS,
                "--details-url",
                "u",
                "--external-id",
                "1",
                "--summary",
                "s",
            )
        )
        self.assertNotIn("head_sha", updated)


def _job_body(workflow: str, job_name: str) -> str:
    match = re.search(
        rf"^  {re.escape(job_name)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
        workflow,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"找不到作业 {job_name}"
    return match.group(1)


def _strip_comments(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _top_level_block(workflow: str, key: str) -> str:
    match = re.search(
        rf"^{re.escape(key)}:\n(.*?)(?=^[A-Za-z_-]+:|\Z)", workflow, re.MULTILINE | re.DOTALL
    )
    assert match is not None, f"找不到顶层键 {key}"
    return match.group(1)


class VerdictWorkflowShapeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = VERDICT_WORKFLOW.read_text(encoding="utf-8")
        cls.text = _strip_comments(cls.raw)
        cls.jobs = re.findall(
            r"^  ([A-Za-z0-9_-]+):\n", _top_level_block(cls.text, "jobs"), re.MULTILINE
        )

    def test_triggered_only_by_completed_epic_full_runs(self):
        on_block = _top_level_block(self.text, "on")
        self.assertRegex(
            on_block,
            r"^  workflow_run:\n    workflows: \[\"Epic Full\"\]\n    types: \[completed\]\n",
        )
        for forbidden in (
            "pull_request",
            "pull_request_target",
            "push:",
            "workflow_dispatch",
            "schedule",
        ):
            self.assertNotIn(forbidden, on_block, forbidden)

    def test_top_level_permissions_are_read_only(self):
        self.assertEqual(_top_level_block(self.text, "permissions").strip(), "contents: read")

    def test_every_job_declares_permissions_without_write_to_contents_or_actions(self):
        for job in self.jobs:
            body = _job_body(self.text, job)
            with self.subTest(job=job):
                self.assertIn("    permissions:\n", body)
                self.assertNotRegex(body, r"contents:\s*write")
                self.assertNotRegex(body, r"actions:\s*write")
                self.assertNotIn("packages:", body)

    def test_app_private_key_only_reaches_the_environment_guarded_job(self):
        guarded = [
            job
            for job in self.jobs
            if re.search(r"^    environment: verdict\s*$", _job_body(self.text, job), re.M)
        ]
        self.assertEqual(guarded, ["verdict"])
        for job in self.jobs:
            body = _job_body(self.text, job)
            with self.subTest(job=job):
                if job == "verdict":
                    self.assertIn("secrets.VERDICT_APP_PRIVATE_KEY", body)
                    self.assertIn("secrets.VERDICT_APP_ID", body)
                else:
                    self.assertNotIn("secrets.", body)
        self.assertEqual(
            set(re.findall(r"secrets\.([A-Z_]+)", self.text)),
            {"VERDICT_APP_ID", "VERDICT_APP_PRIVATE_KEY"},
        )
        self.assertNotIn("-----BEGIN", self.raw)

    def test_check_run_is_written_only_with_the_app_token(self):
        body = _job_body(self.text, "verdict")
        self.assertRegex(body, r"uses: actions/create-github-app-token@[0-9a-f]{40}")
        self.assertIn("permission-checks: write", body)
        self.assertRegex(body, r"GH_TOKEN: \$\{\{ steps\.app-token\.outputs\.token \}\}")
        self.assertNotIn("github.token", body)
        self.assertNotIn("GITHUB_TOKEN", body)
        self.assertIn("check-runs", body)
        self.assertIn("-X PATCH", body)
        self.assertIn("-X POST", body)
        self.assertIn("check-run existing", body)

    def test_write_job_waits_for_rerun_and_only_when_a_verdict_is_due(self):
        body = _job_body(self.text, "verdict")
        self.assertIn("\n    needs: [decide, rerun]\n", body)
        condition = re.search(r"^    if: >-\n((?:      [^\n]*\n)+)", body, re.M).group(1)
        self.assertIn("!cancelled()", condition)
        self.assertIn("needs.decide.result == 'success'", condition)
        self.assertIn("needs.decide.outputs.write_check == 'true'", condition)

    def test_rerun_uses_main_ci_with_test_ref_only_on_path_b(self):
        body = _job_body(self.text, "rerun")
        self.assertIn("uses: ./.github/workflows/ci.yml", body)
        self.assertIn("test_ref: ${{ needs.decide.outputs.head_sha }}", body)
        self.assertIn("if: needs.decide.outputs.path == 'b'", body)
        self.assertNotIn("secrets:", body)

    def test_own_checkouts_never_take_a_ref(self):
        checkouts = re.findall(
            r"uses: actions/checkout@([0-9a-f]{40})[^\n]*\n((?:        [^\n]*\n)*)", self.text
        )
        self.assertGreaterEqual(len(checkouts), 1)
        for sha, block in checkouts:
            self.assertNotIn("ref:", block)
            self.assertIn("persist-credentials: false", block)
            self.assertNotIn("allow-unsafe-pr-checkout", block)
        self.assertNotRegex(self.text, r"git (fetch|checkout|clone)")

    def test_label_job_is_independent_of_the_verdict_and_needs_no_app_secret(self):
        body = _job_body(self.text, "label")
        self.assertIn(
            "if: needs.decide.outputs.path == 'b' && needs.decide.outputs.pr_number != ''", body
        )
        self.assertNotIn("environment:", body)
        self.assertIn("issues: write", body)
        self.assertIn("pull-requests: write", body)
        self.assertIn("LABEL: " + vd.CHANGED_GATE_LABEL, body)
        self.assertNotIn("needs: [decide, rerun]", body)

    def test_label_job_tells_gh_which_repository_without_a_checkout(self):
        """没有 checkout 的作业里 gh 认不出远端：`gh label create` 必须有 GH_REPO 或 --repo。"""

        body = _job_body(self.text, "label")
        self.assertNotIn("actions/checkout", body)
        self.assertIn("gh label create", body)
        has_env = "GH_REPO: ${{ github.repository }}" in body
        has_flag = re.search(r"gh label create[^\n]*--repo ", body) is not None
        self.assertTrue(has_env or has_flag, "label 作业既没设 GH_REPO 也没给 --repo")

    def test_every_action_is_pinned_to_a_commit_sha(self):
        for line in re.findall(r"uses: [^\n]+", self.text):
            with self.subTest(line=line):
                if line.startswith("uses: ./"):
                    continue
                self.assertRegex(line, r"uses: [\w.-]+/[\w.-]+@[0-9a-f]{40} # v\d")

    def test_workflow_calls_only_known_subcommands(self):
        allowed = {"plan", "decide", "finalize", "check-run"}
        calls = re.findall(r"scripts/ci/verdict_decision\.py (\S+)", self.text)
        self.assertEqual(set(calls), allowed)

    def test_concurrency_serialises_per_head_sha_without_cancelling(self):
        block = _top_level_block(self.text, "concurrency")
        self.assertIn("group: verdict-${{ github.event.workflow_run.head_sha }}", block)
        self.assertIn("cancel-in-progress: false", block)


class CiWorkflowTestRefTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = CI_WORKFLOW.read_text(encoding="utf-8")
        cls.code = _strip_comments(cls.text)
        cls.scheduling = _load_by_path(SCHEDULING_TESTS, "ci_dispatch_scheduling_helpers")

    def test_workflow_call_declares_optional_string_test_ref(self):
        on_block = _top_level_block(self.code, "on")
        self.assertRegex(
            on_block,
            r"  workflow_call:\n    inputs:\n      test_ref:\n(?:        [^\n]*\n)*        type: string\n",
        )
        self.assertRegex(on_block, r"      test_ref:\n(?:        [^\n]*\n)*        default: ''\n")
        self.assertRegex(
            on_block, r"      test_ref:\n(?:        [^\n]*\n)*        required: false\n"
        )
        self.assertNotIn("pull_request_target", on_block)
        self.assertNotRegex(
            on_block,
            r"^  push:",
        )

    def test_story_reuse_keeps_calling_without_inputs(self):
        story = STORY_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("uses: ./.github/workflows/ci.yml", story)
        self.assertNotIn("test_ref", story)

    def test_every_checkout_step_consumes_test_ref(self):
        checkouts = re.findall(
            r"uses: actions/checkout@[0-9a-f]{40}[^\n]*\n((?:        [^\n]*\n)*)", self.code
        )
        self.assertEqual(len(checkouts), 8)
        for block in checkouts:
            self.assertIn("ref: ${{ inputs.test_ref || '' }}", block)
            self.assertIn("persist-credentials: false", block)
        self.assertEqual(self.code.count("ref: ${{ inputs.test_ref || '' }}"), 8)

    def test_concurrency_group_keeps_pr_number_first_then_test_ref(self):
        block = _top_level_block(self.code, "concurrency")
        self.assertIn(
            "group: ci-${{ github.workflow }}-${{ github.event.pull_request.number || inputs.test_ref || github.ref }}",
            block,
        )

    def test_rerun_context_skips_classify_and_runs_full_gate(self):
        helpers = self.scheduling
        values = helpers.context(event="workflow_run", base="", head="")
        results = {"classify": "skipped", "gate": "success", "extras": "success"}
        self.assertFalse(helpers.scheduled(self.text, "classify", values, {}))
        for name in ("gate", "extras", "image"):
            self.assertTrue(helpers.scheduled(self.text, name, values, results), name)
        for name in ("docs", "l1"):
            self.assertFalse(helpers.scheduled(self.text, name, values, results), name)
        for dependency in ("gate", "extras"):
            broken = dict(results, **{dependency: "failure"})
            self.assertFalse(helpers.scheduled(self.text, "image", values, broken), dependency)

    def test_rerun_context_candidate_requires_all_three_and_writes_no_proof(self):
        block = _job_body(self.text, "candidate")
        script = re.search(r"^        run: \|\n((?:          [^\n]*\n|\n)+)", block, re.M).group(1)
        environment = dict(
            os.environ,
            EVENT_NAME="workflow_run",
            BASE_REF="",
            HEAD_REF="",
            RUN_ATTEMPT="1",
            MODE="",
            RISK_LEVEL="",
            DOCS_CHANGED="",
            CLASSIFY_RESULT="skipped",
            DOCS_RESULT="skipped",
            L1_RESULT="skipped",
            GATE_RESULT="success",
            EXTRAS_RESULT="success",
            IMAGE_RESULT="success",
        )
        ok = subprocess.run(
            ["bash", "-e", "-c", textwrap.dedent(script)],
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(ok.returncode, 0, ok.stderr)
        for dependency in ("GATE_RESULT", "EXTRAS_RESULT", "IMAGE_RESULT"):
            for status in ("skipped", "failure", "cancelled"):
                result = subprocess.run(
                    ["bash", "-e", "-c", textwrap.dedent(script)],
                    env=dict(environment, **{dependency: status}),
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0, (dependency, status))
        proof_steps = re.findall(
            r"^      - name: (检出代码|写入候选身份|留存候选证明)\n        if: >-\n((?:          [^\n]*\n)+)",
            block,
            re.M,
        )
        self.assertEqual(
            [name for name, _ in proof_steps], ["检出代码", "写入候选身份", "留存候选证明"]
        )
        for _, condition in proof_steps:
            self.assertIn("github.event_name == 'pull_request'", condition)

    def test_image_job_exports_candidates_only_for_pull_requests(self):
        block = _job_body(self.text, "image")
        exports = re.findall(
            r"^      - name: [^\n]*(?:Issue #150)[^\n]*\n        if: ([^\n]+)\n", block, re.M
        )
        self.assertEqual(len(exports), 3)
        for condition in exports:
            self.assertEqual(condition, "github.event_name == 'pull_request'")


class DocumentationTest(unittest.TestCase):
    def test_gate_doc_names_the_third_tier_and_the_recovery_path(self):
        text = (ROOT / "docs/技术设计/验证与门禁.md").read_text(encoding="utf-8")
        for marker in ("`Epic Verdict`", "路径 a", "路径 b", "恢复路径", vd.CHANGED_GATE_LABEL):
            self.assertIn(marker, text, marker)


if __name__ == "__main__":
    unittest.main()
