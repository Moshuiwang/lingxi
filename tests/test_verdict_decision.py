"""裁决层 `Epic Verdict` 的钉住用例：判定纯函数、凭证验签、三份工作流的形状、复跑上下文的调度。

四组断言各挡一类退化：
1. `scripts/ci/verdict_decision.py` 的每条规则——事件 / 路径先决、外部仓库判红、
   路径 a 只认 `success`（`skipped` / `neutral` / `cancelled` 都判红）、路径 b 等复跑、
   备用基线只认受保护 base、check run 的查找与请求体。
2. OIDC 身份凭证——本地 RSA 密钥对造 JWKS 与令牌：正确、`base_ref=x-attacker`、
   `run_id` 不符、`aud` 不符、签名错、`sha` 不符、过期但其他都对（采信）、算法 / kid /
   格式不对、制品缺失；只有凭证合格且子树相同才走路径 a。
3. `.github/workflows/verdict.yml` 的安全形状——只由 `workflow_run` 触发、顶层只读、
   App 私钥只进 Environment `verdict` 那一个作业、写检查不用 GITHUB_TOKEN、本工作流
   自己的 checkout 从不检出 PR 头提交、decide 作业有验签与 JWKS 地址。
4. `.github/workflows/ci.yml` 的 `test_ref` 消费与 `id-token: write` 只给 candidate 作业；
   复跑上下文（event_name = workflow_run）下 classify 跳过、gate / extras / image 照跑、
   candidate 仍要求三者全 success 且不写候选证明。

变异实测：把 `verify_signature` 改成不验直接返回声明，签名错的用例应判红；把
`claim_problems` 里的 `base_ref` 检查放开，`x-attacker` 用例应判红；还原后复绿。
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
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
MERGE = "e" * 40
TREE_SAME = "c" * 40
TREE_OTHER = "d" * 40
RUN_ID = 123456


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
        "id": RUN_ID,
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


def good_claims(**overrides):
    claims = {
        "aud": vd.TOKEN_AUDIENCE,
        "iss": vd.TOKEN_ISSUER,
        "repository": REPOSITORY,
        "run_id": str(RUN_ID),
        "event_name": "pull_request",
        "base_ref": "main",
        "head_ref": "feat/x",
        "sha": MERGE,
        "ref": "refs/pull/700/merge",
        "workflow_ref": f"{REPOSITORY}/.github/workflows/ci.yml@refs/pull/700/merge",
        "exp": int(time.time()) + 600,
    }
    claims.update(overrides)
    return claims


OK_TOKEN = vd.TokenCheck(status=vd.TOKEN_OK, claims=good_claims())
MISSING_TOKEN = vd.TokenCheck(status=vd.TOKEN_MISSING)
BAD_TOKEN = vd.TokenCheck(status=vd.TOKEN_INVALID, reasons=("base_ref='x-attacker' 不是默认分支",))


def decide(head_tree=TREE_SAME, base_tree=TREE_SAME, token=OK_TOKEN, **kwargs):
    run_facts = facts(**kwargs)
    trees = vd.WorkflowTrees(head=head_tree, base=base_tree)
    return vd.decide(run_facts, vd.select_baseline(run_facts), trees, token)


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

    def test_fork_head_is_rejected_even_with_a_good_token(self):
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

    def test_path_a_summary_names_token_run_and_trees(self):
        verdict = decide()
        self.assertIn("路径 a", verdict.summary)
        self.assertIn("身份凭证验签通过", verdict.summary)
        self.assertIn("123456", verdict.summary)
        self.assertIn(TREE_SAME, verdict.summary)

    def test_changed_workflow_tree_takes_path_b_even_with_a_good_token(self):
        for head_tree, base_tree in (
            (TREE_OTHER, TREE_SAME),
            (None, TREE_SAME),
            ("", TREE_SAME),
            (TREE_SAME, None),
            (TREE_SAME, ""),
            (None, None),
            ("", ""),
        ):
            with self.subTest(head=head_tree, base=base_tree):
                verdict = decide(head_tree=head_tree, base_tree=base_tree, conclusion="success")
                self.assertEqual(verdict.path, vd.PATH_B)
                self.assertTrue(verdict.write_check)
                self.assertIsNone(verdict.conclusion)
                self.assertIn("不采信", verdict.summary)
                self.assertTrue(vd.definitions_differ(vd.WorkflowTrees(head_tree, base_tree)))
        self.assertFalse(vd.definitions_differ(vd.WorkflowTrees(TREE_SAME, TREE_SAME)))

    def test_missing_or_invalid_token_never_trusts_the_run_even_with_identical_trees(self):
        for token, marker in ((MISSING_TOKEN, "没有身份凭证"), (BAD_TOKEN, "身份凭证不合格")):
            with self.subTest(status=token.status):
                verdict = decide(token=token, conclusion="success")
                self.assertEqual((verdict.path, verdict.conclusion), (vd.PATH_B, None))
                self.assertIn(marker, verdict.summary)
                self.assertIn("不采信", verdict.summary)

    def test_pull_request_lists_do_not_influence_trust(self):
        """事件里的 PR 关联（含攻击者的 PR→x）只影响备用基线，采信只看凭证。"""

        attacker_only = [pull_request(number=1, base_ref="x-attacker", base_sha="1" * 40)]
        self.assertEqual(decide(pull_requests=attacker_only, token=OK_TOKEN).path, vd.PATH_A)
        self.assertEqual(decide(pull_requests=[], token=OK_TOKEN).path, vd.PATH_A)
        self.assertEqual(
            decide(pull_requests=[pull_request()], token=MISSING_TOKEN).path, vd.PATH_B
        )

    def test_path_b_success_of_own_run_is_never_trusted(self):
        verdict = decide(head_tree=TREE_OTHER, conclusion="success")
        self.assertIsNone(verdict.conclusion)
        self.assertEqual(
            vd.finalize(verdict.path, None, "failure", verdict.summary).conclusion, vd.FAILURE
        )


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class TokenFixtures:
    """本地 RSA 密钥对：造 JWKS，签令牌。只在测试里生成，永不进仓库。"""

    @classmethod
    def build(cls):
        from cryptography.hazmat.primitives.asymmetric import rsa

        fixtures = cls()
        fixtures.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        fixtures.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        fixtures.kid = "test-key-1"
        fixtures.jwks = {"keys": [cls.jwk(fixtures.key, fixtures.kid)]}
        return fixtures

    @staticmethod
    def jwk(key, kid: str) -> dict:
        numbers = key.public_key().public_numbers()
        return {
            "kty": "RSA",
            "kid": kid,
            "use": "sig",
            "alg": "RS256",
            "n": _b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
            "e": _b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
        }

    def token(self, claims: dict, *, key=None, header: dict | None = None) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        header = header or {"alg": "RS256", "typ": "JWT", "kid": self.kid}
        signing_input = (
            _b64url(json.dumps(header, separators=(",", ":")).encode())
            + "."
            + _b64url(json.dumps(claims, separators=(",", ":")).encode())
        )
        signature = (key or self.key).sign(
            signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256()
        )
        return signing_input + "." + _b64url(signature)


DEFAULT = object()
EXPECTED = vd.ExpectedClaims(
    repository=REPOSITORY, run_id=str(RUN_ID), head_sha=HEAD, default_branch="main"
)
MERGE_PARENTS = (BASE, HEAD)


class TokenVerificationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = TokenFixtures.build()

    def check(
        self, claims=None, *, token=DEFAULT, jwks=None, parents=MERGE_PARENTS, expected=EXPECTED
    ):
        text = token if token is not DEFAULT else self.fx.token(claims or good_claims())
        return vd.check_token(text, self.fx.jwks if jwks is None else jwks, expected, parents)

    def test_correct_token_is_accepted_and_exposes_base_ref_and_pull_request(self):
        result = self.check()
        self.assertEqual((result.status, result.reasons), (vd.TOKEN_OK, ()))
        self.assertTrue(result.ok)
        self.assertEqual(
            (result.base_ref, result.sha, result.pull_request_number), ("main", MERGE, 700)
        )
        self.assertIn("run 123456", result.summary)

    def test_release_base_ref_is_not_trusted(self):
        for base_ref in ("release/2.5", "refs/heads/release/2.5", "release/evil"):
            with self.subTest(base_ref=base_ref):
                result = self.check(good_claims(base_ref=base_ref))
                self.assertEqual(result.status, vd.TOKEN_INVALID)
                self.assertTrue(
                    any(
                        "base_ref" in reason and "不是默认分支" in reason
                        for reason in result.reasons
                    ),
                    result.reasons,
                )
        for base_ref in ("main", "refs/heads/main"):
            with self.subTest(base_ref=base_ref):
                self.assertTrue(self.check(good_claims(base_ref=base_ref)).ok)

    def test_base_ref_outside_default_branch_is_rejected(self):
        for base_ref in (
            "x-attacker",
            "refs/heads/x-attacker",
            "epic/a",
            "trace/770",
            "release/2.5",
            "refs/heads/release/2.5",
            "",
            None,
        ):
            with self.subTest(base_ref=base_ref):
                result = self.check(good_claims(base_ref=base_ref))
                self.assertEqual(result.status, vd.TOKEN_INVALID)
                self.assertTrue(
                    any("base_ref" in reason for reason in result.reasons), result.reasons
                )
        for base_ref in ("main", "refs/heads/main"):
            with self.subTest(base_ref=base_ref):
                self.assertTrue(self.check(good_claims(base_ref=base_ref)).ok)

    def test_claim_level_prefix_variants_are_rejected(self):
        for base_ref in ("main-evil", "mainx", "Main", "main/"):
            with self.subTest(base_ref=base_ref):
                result = self.check(good_claims(base_ref=base_ref))
                self.assertEqual(result.status, vd.TOKEN_INVALID)

    def test_is_trusted_base_accepts_only_the_default_branch(self):
        self.assertTrue(vd.is_trusted_base("main", "main"))
        # 这里传入的是 normalize_branch 后的分支名，前缀由 normalize_branch 负责剥除。
        for base_ref in (
            "release/2.5",
            "release/",
            "main-evil",
            "",
            "refs/heads/main",
        ):
            with self.subTest(base_ref=base_ref):
                self.assertFalse(vd.is_trusted_base(base_ref, "main"))

    def test_empty_default_branch_trusts_nothing(self):
        for base_ref in ("", "main"):
            with self.subTest(base_ref=base_ref):
                self.assertFalse(vd.is_trusted_base(base_ref, ""))

    def test_run_id_mismatch_is_rejected(self):
        for run_id in ("123457", 123457, "", None):
            with self.subTest(run_id=run_id):
                result = self.check(good_claims(run_id=run_id))
                self.assertEqual(result.status, vd.TOKEN_INVALID)
                self.assertTrue(any(reason.startswith("run_id=") for reason in result.reasons))
        self.assertTrue(self.check(good_claims(run_id=RUN_ID)).ok)

    def test_audience_mismatch_is_rejected(self):
        for aud in ("other", ["other"], "", None, ["lingxi-epic-verdict-2"]):
            with self.subTest(aud=aud):
                result = self.check(good_claims(aud=aud))
                self.assertEqual(result.status, vd.TOKEN_INVALID)
                self.assertTrue(any(reason.startswith("aud=") for reason in result.reasons))
        self.assertTrue(self.check(good_claims(aud=[vd.TOKEN_AUDIENCE, "other"])).ok)

    def test_wrong_signature_is_rejected(self):
        forged = self.fx.token(good_claims(), key=self.fx.other_key)
        result = self.check(token=forged)
        self.assertEqual(result.status, vd.TOKEN_INVALID)
        self.assertEqual(result.reasons, ("凭证签名不符",))
        self.assertEqual(result.claims, {})
        tampered = self.fx.token(good_claims()).rsplit(".", 1)[0] + "." + _b64url(b"\x00" * 256)
        self.assertEqual(self.check(token=tampered).status, vd.TOKEN_INVALID)

    def test_sha_must_bind_to_the_head_commit(self):
        unbound = self.check(good_claims(sha="9" * 40), parents=(BASE, "8" * 40))
        self.assertEqual(unbound.status, vd.TOKEN_INVALID)
        self.assertTrue(any(reason.startswith("sha=") for reason in unbound.reasons))
        self.assertEqual(self.check(good_claims(), parents=None).status, vd.TOKEN_INVALID)
        self.assertEqual(self.check(good_claims(), parents=()).status, vd.TOKEN_INVALID)
        self.assertTrue(self.check(good_claims(sha=HEAD), parents=None).ok)
        self.assertTrue(self.check(good_claims(), parents=MERGE_PARENTS).ok)

    def test_expired_token_is_still_accepted_when_everything_else_matches(self):
        result = self.check(good_claims(exp=int(time.time()) - 86400))
        self.assertTrue(result.ok)

    def test_algorithm_kid_and_format_are_enforced(self):
        none_alg = self.fx.token(
            good_claims(), header={"alg": "none", "typ": "JWT", "kid": self.fx.kid}
        )
        self.assertEqual(self.check(token=none_alg).status, vd.TOKEN_INVALID)
        hs256 = self.fx.token(
            good_claims(), header={"alg": "HS256", "typ": "JWT", "kid": self.fx.kid}
        )
        self.assertEqual(self.check(token=hs256).status, vd.TOKEN_INVALID)
        unknown_kid = self.fx.token(
            good_claims(), header={"alg": "RS256", "typ": "JWT", "kid": "nope"}
        )
        self.assertEqual(self.check(token=unknown_kid).status, vd.TOKEN_INVALID)
        for broken in ("not-a-jwt", "a.b", "a.b.c", ""):
            with self.subTest(broken=broken):
                status = self.check(token=broken).status
                self.assertIn(status, (vd.TOKEN_INVALID, vd.TOKEN_MISSING))
        self.assertEqual(self.check(token=None).status, vd.TOKEN_MISSING)
        self.assertEqual(self.check(jwks={}).status, vd.TOKEN_INVALID)

    def test_other_claims_are_enforced(self):
        cases = {
            "event_name": ("workflow_dispatch", "workflow_run", ""),
            "repository": ("Moshuiwang/other", "someone/lingxi", ""),
            "iss": ("https://token.actions.githubusercontent.com/evil", "", None),
            "workflow_ref": (
                f"{REPOSITORY}/.github/workflows/fake.yml@refs/pull/700/merge",
                "someone/lingxi/.github/workflows/ci.yml@refs/pull/700/merge",
                "",
            ),
        }
        for claim, values in cases.items():
            for value in values:
                with self.subTest(claim=claim, value=value):
                    result = self.check(good_claims(**{claim: value}))
                    self.assertEqual(result.status, vd.TOKEN_INVALID)
                    self.assertTrue(
                        any(reason.startswith(f"{claim}=") for reason in result.reasons)
                    )

    def test_base_commitish_prefers_the_merge_parent(self):
        run_facts = facts()
        self.assertEqual(vd.base_commitish_for(OK_TOKEN, run_facts, MERGE_PARENTS), BASE)
        direct = vd.TokenCheck(status=vd.TOKEN_OK, claims=good_claims(sha=HEAD))
        self.assertEqual(vd.base_commitish_for(direct, run_facts, ()), "main")

    def test_decide_trusts_only_a_verified_token(self):
        verified = self.check()
        self.assertEqual(decide(token=verified).path, vd.PATH_A)
        self.assertEqual(
            decide(token=self.check(good_claims(base_ref="x-attacker"))).path, vd.PATH_B
        )
        self.assertEqual(decide(token=self.check(token=None)).path, vd.PATH_B)


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


class FallbackBaselineTest(unittest.TestCase):
    """凭证不可用时的备用基线：只认受保护 base；它只影响标签与差异清单。"""

    def test_pull_request_to_unprotected_base_is_ignored(self):
        for base_ref in (
            "x-attacker",
            "feat/other",
            "epic/a",
            "trace/770-w1",
            "main-2",
            "releases/1",
        ):
            with self.subTest(base_ref=base_ref):
                baseline = vd.select_baseline(facts(pull_requests=[pull_request(5, base_ref)]))
                self.assertIsNone(baseline.pull_request)
                self.assertEqual(
                    (baseline.base_ref, baseline.base_sha, baseline.label), ("main", "", "main")
                )
        self.assertTrue(vd.is_protected_base("main", "main"))
        self.assertTrue(vd.is_protected_base("release/2.5", "main"))
        # 标签面与信任面分离。
        self.assertFalse(vd.is_trusted_base("release/2.5", "main"))
        self.assertFalse(vd.is_protected_base("x-attacker", "main"))
        self.assertFalse(vd.is_protected_base("", "main"))

    def test_default_branch_base_is_preferred_then_matching_head(self):
        release = pull_request(number=1, base_ref="release/2.5", base_sha="1" * 40, head_sha=HEAD)
        main_stale = pull_request(number=2, base_ref="main", base_sha=BASE, head_sha="9" * 40)
        main_current = pull_request(number=3, base_ref="main", base_sha=BASE, head_sha=HEAD)
        self.assertEqual(
            vd.select_baseline(facts(pull_requests=[release, main_stale])).pull_request.number, 2
        )
        self.assertEqual(
            vd.select_baseline(facts(pull_requests=[main_stale, main_current])).pull_request.number,
            3,
        )
        only_release = vd.select_baseline(facts(pull_requests=[release]))
        self.assertEqual(
            (only_release.pull_request.number, only_release.label), (1, "release/2.5@" + "1" * 12)
        )

    def test_empty_pull_requests_uses_fallback_lookup_then_default_branch(self):
        fallback = [pull_request(number=42), pull_request(number=43, base_ref="x-attacker")]
        with_fallback = vd.parse_run_facts(event(pull_requests=[]), fallback)
        self.assertEqual(vd.select_baseline(with_fallback).pull_request.number, 42)
        without = vd.select_baseline(vd.parse_run_facts(event(pull_requests=[]), []))
        self.assertIsNone(without.pull_request)
        ignored = vd.parse_run_facts(event(pull_requests=[pull_request(number=7)]), fallback)
        self.assertEqual([pr.number for pr in ignored.pull_requests], [7])


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

    @classmethod
    def setUpClass(cls):
        cls.fx = TokenFixtures.build()

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

    def write_fixtures(self, temp: Path, claims: dict, *, parents=MERGE_PARENTS, with_token=True):
        (temp / "event.json").write_text(json.dumps(event(conclusion="success")), encoding="utf-8")
        (temp / "fallback.json").write_text("[]", encoding="utf-8")
        (temp / "jwks.json").write_text(json.dumps(self.fx.jwks), encoding="utf-8")
        if with_token:
            (temp / "token.jwt").write_text(self.fx.token(claims), encoding="utf-8")
        if parents is not None:
            (temp / "parents.json").write_text(json.dumps(list(parents)), encoding="utf-8")
        (temp / "base.json").write_text(
            json.dumps([{"path": "ci.yml", "sha": "1", "type": "blob"}]), encoding="utf-8"
        )
        (temp / "head.json").write_text(
            json.dumps([{"path": "ci.yml", "sha": "2", "type": "blob"}]), encoding="utf-8"
        )

    def situation_args(self, temp: Path) -> list[str]:
        return [
            "--event-file",
            str(temp / "event.json"),
            "--fallback-pulls-file",
            str(temp / "fallback.json"),
            "--token-file",
            str(temp / "token.jwt"),
            "--jwks-file",
            str(temp / "jwks.json"),
            "--merge-parents-file",
            str(temp / "parents.json"),
        ]

    def test_verify_plan_decide_with_a_good_token_take_path_a(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            self.write_fixtures(temp, good_claims())
            verify_out = temp / "verify.txt"
            self.run_cli("verify", *self.situation_args(temp), "--github-output", str(verify_out))
            verify = self.parse_outputs(verify_out)
            self.assertEqual(
                (verify["token_status"], verify["token_sha"], verify["head_sha"]),
                ("ok", MERGE, HEAD),
            )
            plan_out = temp / "plan.txt"
            self.run_cli("plan", *self.situation_args(temp), "--github-output", str(plan_out))
            plan = self.parse_outputs(plan_out)
            self.assertEqual(plan["needs_trees"], "true")
            self.assertEqual(
                (plan["pr_number"], plan["base_sha"], plan["base_label"]),
                ("700", BASE, "main@" + BASE[:12]),
            )
            self.assertEqual(plan["token_status"], "ok")
            decide_out = temp / "decide.txt"
            self.run_cli(
                "decide",
                *self.situation_args(temp),
                "--head-tree",
                TREE_SAME,
                "--base-tree",
                TREE_SAME,
                "--github-output",
                str(decide_out),
            )
            outputs = self.parse_outputs(decide_out)
            self.assertEqual((outputs["path"], outputs["conclusion"]), (vd.PATH_A, vd.SUCCESS))
            self.assertEqual(
                (outputs["definitions_changed"], outputs["pr_number"]), ("false", "700")
            )

    def test_verify_reports_signature_problems_before_merge_parents_are_known(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            self.write_fixtures(temp, good_claims(), parents=None)
            out = temp / "verify.txt"
            self.run_cli("verify", *self.situation_args(temp), "--github-output", str(out))
            self.assertEqual(self.parse_outputs(out)["token_status"], "ok")
            (temp / "token.jwt").write_text(
                self.fx.token(good_claims(), key=self.fx.other_key), encoding="utf-8"
            )
            out2 = temp / "verify2.txt"
            self.run_cli("verify", *self.situation_args(temp), "--github-output", str(out2))
            bad = self.parse_outputs(out2)
            self.assertEqual((bad["token_status"], bad["token_sha"]), ("invalid", ""))
            self.assertIn("签名不符", bad["token_reasons"])

    def test_decide_with_attacker_base_token_or_missing_token_takes_path_b(self):
        for claims, with_token, marker in (
            (good_claims(base_ref="x-attacker"), True, "不是默认分支"),
            (good_claims(base_ref="release/2.5"), True, "不是默认分支"),
            (good_claims(), False, "没有身份凭证"),
        ):
            with self.subTest(marker=marker):
                with tempfile.TemporaryDirectory() as directory:
                    temp = Path(directory)
                    self.write_fixtures(temp, claims, with_token=with_token)
                    decide_out = temp / "decide.txt"
                    self.run_cli(
                        "decide",
                        *self.situation_args(temp),
                        "--head-tree",
                        TREE_SAME,
                        "--base-tree",
                        TREE_SAME,
                        "--github-output",
                        str(decide_out),
                    )
                    outputs = self.parse_outputs(decide_out)
                    self.assertEqual((outputs["path"], outputs["conclusion"]), (vd.PATH_B, ""))
                    self.assertEqual(outputs["definitions_changed"], "false")
                    self.assertIn(marker, outputs["summary"])

    def test_decide_path_b_with_changed_definitions_emits_diff_and_finalize_maps_rerun(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            self.write_fixtures(temp, good_claims())
            decide_out = temp / "decide.txt"
            self.run_cli(
                "decide",
                *self.situation_args(temp),
                "--head-tree",
                TREE_OTHER,
                "--base-tree",
                TREE_SAME,
                "--head-listing-file",
                str(temp / "head.json"),
                "--base-listing-file",
                str(temp / "base.json"),
                "--github-output",
                str(decide_out),
            )
            outputs = self.parse_outputs(decide_out)
            self.assertEqual((outputs["path"], outputs["definitions_changed"]), (vd.PATH_B, "true"))
            self.assertEqual(json.loads(outputs["diff"]), ["修改 `ci.yml`"])
            final_out = temp / "final.txt"
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
                str(final_out),
            )
            final = self.parse_outputs(final_out)
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
            self.assertEqual(
                (plan["needs_trees"], plan["pr_number"], plan["token_status"]),
                ("false", "", "missing"),
            )
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
    kept = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    return "\n".join(kept) + "\n"


def _top_level_block(workflow: str, key: str) -> str:
    match = re.search(
        rf"^{re.escape(key)}:\n(.*?)(?=^[A-Za-z_-]+:|\Z)", workflow, re.MULTILINE | re.DOTALL
    )
    assert match is not None, f"找不到顶层键 {key}"
    return match.group(1)


def _jobs(workflow_code: str) -> list[str]:
    return re.findall(
        r"^  ([A-Za-z0-9_-]+):\n", _top_level_block(workflow_code, "jobs"), re.MULTILINE
    )


def _pyproject_cryptography_pin() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'"cryptography==([0-9][0-9A-Za-z.]*)"', text)
    assert match is not None
    return match.group(1)


class CodeownersShapeTest(unittest.TestCase):
    def test_verdict_ownership_is_narrow_and_covers_all_decision_files(self):
        text = (ROOT / ".github/CODEOWNERS").read_text(encoding="utf-8")
        for line in (
            "/.github/workflows/verdict.yml @Moshuiwang",
            "/scripts/ci/verdict_decision.py @Moshuiwang",
            "/tests/test_verdict_decision.py @Moshuiwang",
        ):
            with self.subTest(line=line):
                self.assertIn(f"\n{line}\n", f"\n{text}\n")
        self.assertNotIn("\n/.github/workflows/ @Moshuiwang\n", f"\n{text}\n")


class VerdictWorkflowShapeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = VERDICT_WORKFLOW.read_text(encoding="utf-8")
        cls.text = _strip_comments(cls.raw)
        cls.jobs = _jobs(cls.text)

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

    def test_comments_do_not_claim_release_branches_are_trusted(self):
        ci = CI_WORKFLOW.read_text(encoding="utf-8")
        for text in (self.raw, ci):
            self.assertNotIn("base 是 main / release/**", text)
            self.assertNotIn("面向受保护分支的 PR 触发的", text)
        self.assertIn("pull_request 事件、base 是默认分支", self.raw)
        self.assertIn("面向默认分支的 PR 触发的", ci)

    def test_top_level_permissions_are_read_only(self):
        self.assertEqual(_top_level_block(self.text, "permissions").strip(), "contents: read")

    def test_every_job_declares_minimal_permissions(self):
        for job in self.jobs:
            body = _job_body(self.text, job)
            with self.subTest(job=job):
                self.assertIn("    permissions:\n", body)
                self.assertNotRegex(body, r"contents:\s*write")
                self.assertNotRegex(body, r"actions:\s*write")
                self.assertNotIn("packages:", body)
                if job != "rerun":
                    self.assertNotIn("id-token", body)

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
        self.assertIn("id-token: write", body)
        self.assertNotIn("secrets:", body)

    def test_own_checkouts_never_take_a_ref(self):
        checkouts = re.findall(
            r"uses: actions/checkout@([0-9a-f]{40})[^\n]*\n((?:        [^\n]*\n)*)", self.text
        )
        self.assertGreaterEqual(len(checkouts), 1)
        for _sha, block in checkouts:
            self.assertNotIn("ref:", block)
            self.assertIn("persist-credentials: false", block)
            self.assertNotIn("allow-unsafe-pr-checkout", block)
        self.assertNotRegex(self.text, r"git (fetch|checkout|clone)")

    def test_decide_job_verifies_the_oidc_token_offline_against_github_jwks(self):
        body = _job_body(self.text, "decide")
        self.assertIn("actions: read", body)
        self.assertIn('gh run download "${RUN_ID}"', body)
        self.assertIn('--name "epic-verdict-token-${HEAD_SHA}"', body)
        self.assertIn(vd.TOKEN_JWKS_URL, body)
        self.assertIn(f"'cryptography=={_pyproject_cryptography_pin()}'", body)
        self.assertRegex(body, r"uses: actions/setup-python@[0-9a-f]{40}")
        self.assertIn("scripts/ci/verdict_decision.py verify", body)
        self.assertIn('--token-file "${RUNNER_TEMP}/verdict-token/token.jwt"', body)
        self.assertIn('--jwks-file "${RUNNER_TEMP}/jwks.json"', body)
        self.assertIn('--merge-parents-file "${RUNNER_TEMP}/merge-parents.json"', body)
        self.assertRegex(body, r'gh api "repos/\$\{REPOSITORY\}/git/commits/\$\{TOKEN_SHA\}"')
        self.assertNotIn("pulls?state=all", body)
        self.assertNotIn("cat ", body)

    def test_label_job_is_independent_of_the_verdict_and_needs_no_app_secret(self):
        body = _job_body(self.text, "label")
        condition = re.search(r"^    if: >-\n((?:      [^\n]*\n)+)", body, re.M).group(1)
        for clause in (
            "needs.decide.outputs.path == 'b'",
            "needs.decide.outputs.definitions_changed == 'true'",
            "needs.decide.outputs.pr_number != ''",
        ):
            self.assertIn(clause, condition, clause)
        self.assertNotIn("environment:", body)
        self.assertIn("issues: write", body)
        self.assertIn("pull-requests: write", body)
        self.assertIn("LABEL: " + vd.CHANGED_GATE_LABEL, body)
        self.assertNotIn("needs: [decide, rerun]", body)

    def test_label_job_tells_gh_which_repository_without_a_checkout(self):
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
        allowed = {"verify", "plan", "decide", "finalize", "check-run"}
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
        self.assertNotRegex(on_block, r"^  push:")

    def test_story_reuse_grants_id_token_but_passes_no_inputs(self):
        story = _strip_comments(STORY_WORKFLOW.read_text(encoding="utf-8"))
        self.assertIn("uses: ./.github/workflows/ci.yml", story)
        self.assertNotIn("test_ref", story)
        full = _job_body(story, "full")
        self.assertIn("id-token: write", full)
        for job in _jobs(story):
            if job != "full":
                self.assertNotIn("id-token", _job_body(story, job), job)

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

    def test_id_token_write_only_on_the_candidate_job(self):
        self.assertEqual(_top_level_block(self.code, "permissions").strip(), "contents: read")
        for job in _jobs(self.code):
            body = _job_body(self.code, job)
            with self.subTest(job=job):
                if job == "candidate":
                    self.assertRegex(
                        body,
                        r"\n    permissions:\n      contents: read\n      id-token: write\n",
                    )
                else:
                    self.assertNotIn("id-token", body)

    def test_candidate_issues_the_oidc_token_only_for_pull_requests_and_never_prints_it(self):
        body = _job_body(self.code, "candidate")
        mint = re.search(
            r"      - name: 签发裁决层身份凭证[^\n]*\n((?:        [^\n]*\n|\n)+?)(?=      - name: )",
            body,
        )
        self.assertIsNotNone(mint)
        mint_block = mint.group(1)
        self.assertIn("if: github.event_name == 'pull_request' && !cancelled()", mint_block)
        self.assertIn("continue-on-error: true", mint_block)
        self.assertIn(f"AUDIENCE: {vd.TOKEN_AUDIENCE}", mint_block)
        self.assertIn("ACTIONS_ID_TOKEN_REQUEST_URL", mint_block)
        self.assertIn("&audience=${AUDIENCE}", mint_block)
        self.assertIn("jq -r '.value' > \"${out}/token.jwt\"", mint_block)
        self.assertNotRegex(mint_block, r"(echo|cat|printf)[^\n]*token\.jwt")
        upload = re.search(r"      - name: 留存身份凭证供裁决层验签\n((?:        [^\n]*\n)+)", body)
        self.assertIsNotNone(upload)
        upload_block = upload.group(1)
        self.assertIn("steps.verdict-token.outcome == 'success'", upload_block)
        self.assertIn("github.event_name == 'pull_request'", upload_block)
        self.assertIn(
            "name: epic-verdict-token-${{ github.event.pull_request.head.sha }}", upload_block
        )
        self.assertIn("overwrite: true", upload_block)
        self.assertEqual(self.code.count("continue-on-error: true"), 2)
        self.assertEqual(body.count("continue-on-error: true"), 2)

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
    def test_gate_doc_names_the_third_tier_the_token_and_the_recovery_path(self):
        text = (ROOT / "docs/技术设计/验证与门禁.md").read_text(encoding="utf-8")
        for marker in (
            "`Epic Verdict`",
            "路径 a",
            "路径 b",
            "恢复路径",
            "OIDC",
            "`id-token: write`",
            vd.CHANGED_GATE_LABEL,
            "只认默认分支",
        ):
            self.assertIn(marker, text, marker)


if __name__ == "__main__":
    unittest.main()
