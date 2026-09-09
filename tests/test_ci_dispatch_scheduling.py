"""按实际条件和依赖状态演算调度；不运行 Actions、镜像或外部服务。"""

import ast
import json
import os
import re
import subprocess
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/ci.yml"


def job_block(text, name):
    match = re.search(
        r"^  " + re.escape(name) + r":\n(.*?)(?=^  [a-z][a-z0-9_-]*:|\Z)", text, re.M | re.S
    )
    if not match:
        raise AssertionError("job missing: " + name)
    return match.group(1)


def condition(block):
    match = re.search(r"^    if: >-\n((?:      [^\n]*\n)+)", block, re.M)
    if match:
        return " ".join(line.strip() for line in match.group(1).splitlines())
    return re.search(r"^    if: (.+)$", block, re.M).group(1)


def equal(left, right):
    if isinstance(left, str) and isinstance(right, str):
        return left.lower() == right.lower()
    if type(left) is type(right):
        return left == right

    def number(value):
        if isinstance(value, str):
            try:
                return json.loads(value) if value else 0
            except ValueError:
                return float("nan")
        return int(value) if isinstance(value, bool) else value

    return number(left) == number(right)


def evaluate(expression, values, *, successful, cancelled, failed=False):
    # 仅支持此工作流使用的表达式子集；状态函数未出现时附加GitHub默认success守卫。
    tokens = re.findall(r"'(?:[^']|'')*'|!=|==|&&|\|\||[!(),]|[A-Za-z_][A-Za-z0-9_.]*", expression)
    if "".join(tokens) != re.sub(
        r"\s+", "", re.sub(r"'[^']*'", lambda m: m[0].replace(" ", ""), expression)
    ):
        raise AssertionError("unsupported expression token")
    functions = {
        "cancelled": lambda: cancelled,
        "success": lambda: successful,
        "always": lambda: True,
        "failure": lambda: failed,
        "startsWith": lambda value, prefix: value.lower().startswith(prefix.lower()),
    }
    source = []
    for token in tokens:
        if token in ("&&", "||", "!"):
            source.append({"&&": "and", "||": "or", "!": "not"}[token])
        elif token.startswith("'") or token in ("(", ")", ",", "==", "!=") or token in functions:
            source.append(token)
        else:
            source.append(repr(values.get(token, "")))
    tree = ast.parse(" ".join(source), mode="eval")

    def visit(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not visit(node.operand)
        if isinstance(node, ast.BoolOp):
            parts = [bool(visit(value)) for value in node.values]
            return all(parts) if isinstance(node.op, ast.And) else any(parts)
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = visit(node.left), visit(node.comparators[0])
            matched = equal(left, right)
            return matched if isinstance(node.ops[0], ast.Eq) else not matched
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            return functions[node.func.id](*(visit(arg) for arg in node.args))
        raise AssertionError("unsupported expression syntax")

    result = bool(visit(tree.body))
    explicit_status = any(
        re.search(r"\b" + name + r"\s*\(", expression)
        for name in ("success", "failure", "always", "cancelled")
    )
    return result and (explicit_status or successful)


def scheduled(text, name, values, results, cancelled=False):
    block = job_block(text, name)
    found = re.search(r"^    needs: \[([^]]+)\]", block, re.M)
    needs = [value.strip() for value in found.group(1).split(",")] if found else []
    context = dict(values, **{f"needs.{key}.result": value for key, value in results.items()})
    return evaluate(
        condition(block),
        context,
        successful=all(results.get(job) == "success" for job in needs),
        cancelled=cancelled,
        failed=any(results.get(job) == "failure" for job in needs),
    )


def context(
    event="pull_request",
    base="main",
    head="codex/test",
    mode="full",
    risk="l2",
    image="false",
    attempt=1,
):
    return {
        "github.event_name": event,
        "github.base_ref": base,
        "github.head_ref": head,
        "github.run_attempt": attempt,
        "needs.classify.outputs.mode": mode,
        "needs.classify.outputs.risk_level": risk,
        "needs.classify.outputs.image_candidate": image,
    }


class DispatchSchedulingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text()

    def test_manual_skipped_classification_runs_bodies_and_every_extra(self):
        values = context(event="workflow_dispatch", base="", head="")
        results = {"classify": "skipped", "gate": "success", "extras": "success"}
        self.assertFalse(scheduled(self.text, "classify", values, {}))
        for name in ("gate", "extras", "image"):
            self.assertTrue(scheduled(self.text, name, values, results), name)
        extras = re.search(
            r"^        extra: \[([^]]+)\]", job_block(self.text, "extras"), re.M
        ).group(1)
        self.assertEqual(
            {value.strip() for value in extras.split(",")},
            {"scheduler", "reauthorize", "worker", "gateway", "migrate"},
        )

    def test_implicit_success_reproduces_original_manual_skip(self):
        values = context(event="workflow_dispatch", base="", head="")
        results = {"classify": "skipped", "gate": "success", "extras": "success"}
        broken = self.text.replace("!cancelled() &&", "")
        for name in ("gate", "extras", "image"):
            self.assertFalse(scheduled(broken, name, values, results), name)

    def test_classification_failure_or_cancel_cannot_start_any_body(self):
        for event in ("pull_request", "workflow_dispatch"):
            for status in ("failure", "cancelled", "skipped"):
                if event != "pull_request" and status == "skipped":
                    continue
                for name in ("gate", "extras", "image"):
                    with self.subTest(event=event, status=status, name=name):
                        self.assertFalse(
                            scheduled(
                                self.text,
                                name,
                                context(event=event),
                                {"classify": status, "gate": "success", "extras": "success"},
                            )
                        )
        for name in ("gate", "extras", "image"):
            self.assertFalse(
                scheduled(
                    self.text,
                    name,
                    context(event="workflow_dispatch"),
                    {"classify": "skipped", "gate": "success", "extras": "success"},
                    cancelled=True,
                )
            )

    def test_image_requires_both_successful_gate_and_complete_extras(self):
        for event in ("pull_request", "workflow_dispatch"):
            for dependency in ("gate", "extras"):
                for status in ("failure", "cancelled", "skipped", ""):
                    results = {
                        "classify": "success" if event == "pull_request" else "skipped",
                        "gate": "success",
                        "extras": "success",
                    }
                    results[dependency] = status
                    with self.subTest(event=event, dependency=dependency, status=status):
                        self.assertFalse(
                            scheduled(self.text, "image", context(event=event), results)
                        )

    def test_pr_routing_and_image_freeze_choices_remain_the_same(self):
        results = {"classify": "success", "gate": "success", "extras": "success"}
        cases = [
            (context(mode="docs", risk="l0"), (False, False, False)),
            (context(risk="l1"), (False, False, False)),
            (context(), (True, True, True)),
            (context(head="epic/story"), (True, True, False)),
            (context(head="epic/story", image="true"), (True, True, True)),
            (context(head="epic/story", attempt=2), (True, True, True)),
            (context(base="release/2.4", mode="docs", risk="l0"), (True, True, True)),
            (context(base="epic/parent"), (True, True, True)),
        ]
        for values, expected in cases:
            with self.subTest(values=values):
                self.assertEqual(
                    tuple(
                        scheduled(self.text, name, values, results)
                        for name in ("gate", "extras", "image")
                    ),
                    expected,
                )
        docs = context(mode="docs", risk="l0") | {"needs.classify.outputs.docs_changed": "true"}
        self.assertTrue(scheduled(self.text, "docs", docs, results))
        self.assertTrue(scheduled(self.text, "l1", context(risk="l1"), results))

    def test_actual_aggregate_shell_still_rejects_skipped_or_failed_bodies(self):
        block = job_block(self.text, "candidate")
        script = re.search(r"^        run: \|\n((?:          [^\n]*\n|\n)+)", block, re.M).group(1)
        environment = dict(
            os.environ,
            EVENT_NAME="workflow_dispatch",
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
        for dependency in ("GATE_RESULT", "EXTRAS_RESULT", "IMAGE_RESULT"):
            for status in ("skipped", "failure", "cancelled"):
                result = subprocess.run(
                    ["bash", "-e", "-c", textwrap.dedent(script)],
                    env=dict(environment, **{dependency: status}),
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0, (dependency, status))
        result = subprocess.run(
            ["bash", "-e", "-c", textwrap.dedent(script)],
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
