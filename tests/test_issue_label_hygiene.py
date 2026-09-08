"""验证验收状态互斥、旧标题覆盖及只读检查的身份入口。"""

import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/ci"))
hygiene = importlib.import_module("check_issue_label_hygiene")


class IssueLabelHygieneTests(unittest.TestCase):
    def test_acceptance_and_observation_are_mutually_exclusive(self):
        for state in ("待验收", "观察中"):
            with self.subTest(state=state):
                issue = hygiene.Issue(1, "[feature] 测试", ["变更", state])
                self.assertEqual(hygiene.check_state_label_count([issue]), [])
                issue.labels.append("执行中")
                self.assertEqual(len(hygiene.check_state_label_count([issue])), 1)

    def test_legacy_titles_do_not_escape_state_validation(self):
        issue = hygiene.Issue(587, "预开通名单支持多公司", ["变更"])
        self.assertEqual(len(hygiene.check_state_label_count([issue])), 1)
        issue.labels.append("待验收")
        self.assertEqual(hygiene.check_state_label_count([issue]), [])

    def test_reference_entries_stay_out_of_work_state_chain(self):
        for prefix in ("tracking", "template", "board"):
            issue = hygiene.Issue(1, f"[{prefix}] 入口", ["维护"])
            self.assertEqual(hygiene.check_state_label_count([issue]), [])
        reference = hygiene.Issue(2, "参考", ["长期参考", "观察中"])
        self.assertEqual(len(hygiene.check_long_term_reference_conflict([reference])), 1)

    def test_local_identity_is_explicit_and_not_personal_gh_fallback(self):
        for value in ("", "relative-wrapper"):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {"LINGXI_GH_COMMAND": value}, clear=True),
                patch.object(hygiene.subprocess, "run") as run,
            ):
                with self.assertRaises(RuntimeError):
                    hygiene.run_gh_json(["issue", "list"])
                run.assert_not_called()

    def test_approved_wrapper_and_actions_use_the_expected_read_command(self):
        for env, command in (
            ({"LINGXI_GH_COMMAND": "/approved/gh-wrapper"}, "/approved/gh-wrapper"),
            ({"GITHUB_ACTIONS": "true"}, "gh"),
        ):
            with (
                self.subTest(env=env),
                patch.dict(os.environ, env, clear=True),
                patch.object(
                    hygiene.subprocess,
                    "run",
                    return_value=Mock(returncode=0, stdout="[]"),
                ) as run,
            ):
                self.assertEqual(hygiene.run_gh_json(["issue", "list"]), [])
                self.assertEqual(run.call_args.args[0], [command, "issue", "list"])


if __name__ == "__main__":
    unittest.main()
