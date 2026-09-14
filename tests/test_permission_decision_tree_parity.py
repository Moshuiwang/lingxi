"""两个逐用户权限入口对共享决策树的固定样本对照与变异验红。

每日批与定向重算的外层报告、审计动作名和原因词汇按入口各自保留；本文件只把两条公共
调用面产生的 ``UserDecision``、发布行全部字段和可归一化的审计事实放在一起比对。这样
对照测试钉住的是第一步真正要保证的共享结论，不会把入口特有的报告口径误当成决策漂移。
"""

from __future__ import annotations

import ast
import importlib
import io
import pathlib
import sys
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime
from functools import partial
from typing import Any, Callable
from unittest import mock

from lingxi.core.permission.targeted_recompute import (
    ADMIN_TARGETED_RECOMPUTE_REASON,
    ADMIN_TARGETED_REVOKE_REASON,
)
from lingxi.core.permission.user_decision_tree import (
    DecisionBranch,
    UserDecision,
    UserPermissionDecisionTree,
)

# ``unittest`` 以 ``tests.test_*`` 加载时不会自动把 tests 目录放进 sys.path；既有两个入口
# 测试互相按顶层模块名复用夹具。补这一条测试侧路径，确保卡面指定的直接命令也能从本工作树
# 运行，而不改变正式包的导入路径。
TESTS_ROOT = pathlib.Path(__file__).parent
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

refresh = importlib.import_module("test_permission_refresh_duty")
targeted = importlib.import_module("test_targeted_permission_recompute")

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
USER = refresh.USER_ONE


@dataclass(frozen=True)
class _FixedSample:
    """一个固定输入的名字、构造键和预期共享出口。"""

    name: str
    key: str
    expected_branch: DecisionBranch


FIXED_SAMPLES = (
    _FixedSample("匹配失败", "matching_failed", DecisionBranch.MATCH_FAILED),
    _FixedSample("翻译未覆盖", "translation_uncovered", DecisionBranch.TRANSLATION_UNCOVERED),
    _FixedSample(
        "本地覆盖不可读", "local_override_read_failed", DecisionBranch.LOCAL_OVERRIDE_READ_FAILED
    ),
    _FixedSample("被抑制清空", "fully_suppressed", DecisionBranch.REVOKED),
    _FixedSample("零银河有本地授权", "zero_galaxy_local_grant", DecisionBranch.PUBLISHED),
    _FixedSample(
        "零银河本地授权被同键抑制", "zero_galaxy_suppressed", DecisionBranch.REVOKED
    ),
    _FixedSample(
        "零银河无本地授权且无足迹",
        "zero_galaxy_without_local_grant",
        DecisionBranch.REVOKE_WITHOUT_FOOTPRINT,
    ),
    _FixedSample(
        "撤销后迟到补齐", "revoked_before_late_backfill", DecisionBranch.REVOKED
    ),
    _FixedSample("正常全通过", "normal", DecisionBranch.PUBLISHED),
)


def _sample_inputs(sample: _FixedSample) -> dict[str, Any]:
    """用既有权限入口夹具构造一份样本；每次调用都返回可独立消费的对象。"""
    inputs: dict[str, Any] = {
        "roster_rows": (refresh.roster_row(),),
        "galaxy": refresh.galaxy_snapshot(),
        "metric_translation_map": refresh.METRIC_TRANSLATION_MAP,
        "role_function_map": refresh.ROLE_FUNCTION_MAP,
        "local_overrides": refresh.FakeLocalOverrides({USER: ()}),
        "legacy_all_scope": None,
        "published_users": {USER},
        "late_revoke": False,
    }
    if sample.key == "matching_failed":
        inputs["roster_rows"] = ()
    elif sample.key == "translation_uncovered":
        inputs["metric_translation_map"] = {
            refresh.COMPANY_ID_TWO: {refresh.FUNCTION_LABEL: (refresh.METRIC_NAME_TWO,)}
        }
    elif sample.key == "local_override_read_failed":
        inputs["local_overrides"] = refresh.FakeLocalOverrides(fail_for={USER})
    elif sample.key == "fully_suppressed":
        inputs["local_overrides"] = refresh.FakeLocalOverrides(
            {
                USER: (
                    refresh._override_entry(
                        direction=refresh.OverrideDirection.SUPPRESS,
                        metric_name=refresh.METRIC_NAME,
                    ),
                )
            }
        )
    elif sample.key == "zero_galaxy_local_grant":
        inputs["galaxy"] = refresh.galaxy_snapshot(roles=())
        inputs["local_overrides"] = refresh.FakeLocalOverrides(
            {USER: (refresh._override_entry(),)}
        )
    elif sample.key == "zero_galaxy_suppressed":
        inputs["galaxy"] = refresh.galaxy_snapshot(roles=())
        inputs["local_overrides"] = refresh.FakeLocalOverrides(
            {
                USER: (
                    refresh._override_entry(),
                    refresh._override_entry(
                        direction=refresh.OverrideDirection.SUPPRESS,
                        metric_name="本地指标",
                    ),
                )
            }
        )
    elif sample.key == "zero_galaxy_without_local_grant":
        inputs["galaxy"] = refresh.galaxy_snapshot(roles=())
        inputs["local_overrides"] = None
        inputs["published_users"] = set()
    elif sample.key == "revoked_before_late_backfill":
        inputs["galaxy"] = refresh.galaxy_snapshot(roles=())
        overrides = refresh.FakeLocalOverrides(
            {USER: (refresh._all_scope_entry(metric_name=refresh.METRIC_NAME),)}
        )
        inputs["local_overrides"] = overrides
        inputs["legacy_all_scope"] = refresh.FakeLegacyAllScope(overrides=overrides)
        inputs["late_revoke"] = True
    elif sample.key != "normal":
        raise AssertionError(f"未知固定样本：{sample.key}")
    return inputs


def _decision_fields(decision: UserDecision) -> tuple[tuple[str, object], ...]:
    """按返回类型声明顺序取出全部字段，新增字段时对照面自动扩大。"""
    return tuple((field.name, getattr(decision, field.name)) for field in fields(UserDecision))


def _row_fields(row: object) -> tuple[tuple[str, object], ...]:
    """取发布行的全部数据字段，包含更新路径不会写入的密文字段。"""
    return tuple((field.name, getattr(row, field.name)) for field in fields(row))


def _canonical_publish_reason(reason: str) -> str:
    """把两个入口有意不同的发布原因字面量归一成共享的授权/撤权事实。"""
    grant_reasons = {
        refresh.PERMISSION_REFRESH_REASON,
        ADMIN_TARGETED_RECOMPUTE_REASON,
    }
    revoke_reasons = {
        refresh.PERMISSION_REVOKE_REASON,
        ADMIN_TARGETED_REVOKE_REASON,
    }
    if reason in grant_reasons:
        return "grant"
    if reason in revoke_reasons:
        return "revoke"
    raise AssertionError(f"固定样本出现未登记的发布原因码：{reason}")


def _publish_facts(parts: dict[str, Any]) -> tuple[object, ...]:
    """比较落决定调用的行字段、原因类别、账号守卫和清理事实。"""
    return tuple(
        (
            _row_fields(call["row"]),
            _canonical_publish_reason(call["reason"]),
            call["require_enabled_account"],
            call["decided_at"],
            call["clear_delivered_content"],
        )
        for call in parts["decisions"].calls
    )


def _terminal_facts(decision: UserDecision) -> tuple[tuple[str, object], ...]:
    """把两个入口各自的跳过审计投影到共享决策事实。"""
    return (
        ("branch", decision.branch),
        ("match_reason", decision.match_reason),
        ("zero_galaxy_reason", decision.zero_galaxy_reason),
        ("mapping_is_empty", decision.mapping_is_empty),
        ("account_state", decision.account_state),
    )


def _audit_facts(audit: refresh.RecordingAudit, decision: UserDecision) -> tuple[object, ...]:
    """保留两入口都应产生的审计事实，忽略各入口专属动作名与外层报告字段。"""
    facts: list[object] = []
    for action, recorded in audit.records:
        if recorded.get("user") != USER:
            continue
        suffix = action.rsplit(".", 1)[-1]
        if suffix == "local_override_skipped":
            facts.append((suffix, recorded.get("reason")))
        elif suffix in ("legacy_all_scope_refreshed", "legacy_all_scope_refresh_failed"):
            facts.append((suffix, tuple(sorted(recorded.items()))))
        elif suffix == "delivered_content_cleared":
            facts.append(("content_cleared", recorded.get("cleared")))
        elif suffix == "completed":
            facts.append(("content_cleared", recorded.get("cleared")))
            if decision.branch is DecisionBranch.REVOKED:
                facts.append(("revoked", _terminal_facts(decision)))
        elif suffix == "user_revoked":
            if decision.branch is DecisionBranch.REVOKED:
                facts.append(("revoked", _terminal_facts(decision)))
        elif suffix in ("user_skipped", "skipped"):
            facts.append(("skipped", _terminal_facts(decision)))
        elif suffix == "publish_needs_cipher":
            facts.append((suffix, True))
        else:
            facts.append((suffix, tuple(sorted(recorded.items()))))
    return tuple(sorted(facts, key=repr))


@dataclass(frozen=True)
class _Observation:
    """一个入口跑完后的共享决策、发布字段和审计事实。"""

    decision: UserDecision
    publish_facts: tuple[object, ...]
    audit_facts: tuple[object, ...]


def _run_entry(sample: _FixedSample, entry: str) -> _Observation:
    """通过入口公开方法执行样本，只在内部包裹决策树以取回共享返回对象。"""
    inputs = _sample_inputs(sample)
    decisions = refresh.FakeDecisions(cleared_events_by_user={USER: 2})
    if entry == "daily":
        runner, parts = refresh.build_duty(
            identities=(refresh.identity(),),
            roster_captured_at=NOW,
            roster_rows=inputs["roster_rows"],
            galaxy=inputs["galaxy"],
            tokens=refresh.FakeTokens({USER: None}),
            decisions=decisions,
            published_users=inputs["published_users"],
            metric_translation_map=inputs["metric_translation_map"],
            role_function_map=inputs["role_function_map"],
            local_overrides=inputs["local_overrides"],
            legacy_all_scope=inputs["legacy_all_scope"],
            clock=refresh.FixedClock(NOW),
        )
        public_call: Callable[[], object] = runner.run_once
        tree = runner._tree
    elif entry == "targeted":
        runner, parts = targeted.build_recompute(
            identities=(refresh.identity(),),
            roster_rows=inputs["roster_rows"],
            galaxy=inputs["galaxy"],
            decisions=decisions,
            published_users=inputs["published_users"],
            metric_translation_map=inputs["metric_translation_map"],
            role_function_map=inputs["role_function_map"],
            local_overrides=inputs["local_overrides"],
            legacy_all_scope=inputs["legacy_all_scope"],
            clock=targeted.FixedClock(NOW),
        )
        public_call = partial(runner.recompute_and_publish, user_id=USER)
        tree = runner._tree
    else:
        raise AssertionError(f"未知入口：{entry}")

    captured: list[UserDecision] = []
    original_decide = tree.decide

    def capture_decision(*args: Any, **kwargs: Any) -> UserDecision:
        decision = original_decide(*args, **kwargs)
        captured.append(decision)
        return decision

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(tree, "decide", side_effect=capture_decision))
        if inputs["late_revoke"]:
            overrides = inputs["local_overrides"]
            expander = inputs["legacy_all_scope"]

            def revoke_after_read(*, user_id: str, group_id: str, metrics, now) -> int:
                del group_id, metrics, now
                overrides._entries[user_id] = ()
                return 0

            stack.enter_context(
                mock.patch.object(expander, "expand_all_scope_group", side_effect=revoke_after_read)
            )
        public_call()
    if len(captured) != 1:
        raise AssertionError(f"固定样本 {sample.name} 未经入口得到恰好一个共享决定：{captured}")
    decision = captured[0]
    return _Observation(
        decision=decision,
        publish_facts=_publish_facts(parts),
        audit_facts=_audit_facts(parts["audit"], decision),
    )


class DecisionTreeParityTests(unittest.TestCase):
    def test_daily_and_targeted_entries_agree_on_every_fixed_sample(self) -> None:
        for sample in FIXED_SAMPLES:
            with self.subTest(sample=sample.name):
                daily = _run_entry(sample, "daily")
                targeted_observation = _run_entry(sample, "targeted")

                self.assertIs(daily.decision.branch, sample.expected_branch)
                self.assertIs(targeted_observation.decision.branch, sample.expected_branch)
                self.assertEqual(
                    _decision_fields(daily.decision),
                    _decision_fields(targeted_observation.decision),
                    "共享 UserDecision 的全部字段必须逐项相同",
                )
                self.assertEqual(
                    daily.publish_facts,
                    targeted_observation.publish_facts,
                    "发布行全部字段与提交事实必须相同",
                )
                self.assertEqual(
                    daily.audit_facts,
                    targeted_observation.audit_facts,
                    "共享决定相关的审计事实必须相同",
                )


_BRANCH_RETURN_POINTS = {
    DecisionBranch.MISSING_PERSONNEL_ID: "decide",
    DecisionBranch.MATCH_FAILED: "decide",
    DecisionBranch.ARCHIVE_INCOMPLETE: "decide",
    DecisionBranch.TRANSLATION_UNCOVERED: "_translate",
    DecisionBranch.LOCAL_OVERRIDE_READ_FAILED: "_merge_and_settle",
    DecisionBranch.SUPPRESSION_UNREPRESENTABLE: "_merge_and_settle",
    DecisionBranch.REVOKE_WITHOUT_FOOTPRINT: "_revoke",
    DecisionBranch.REVOKED: "_revoke",
    DecisionBranch.GRANT_BLOCKED: "_publish",
    DecisionBranch.PUBLISHED: "_publish",
}


def _wrong_branch(decision: UserDecision, target: DecisionBranch) -> UserDecision:
    """把命中的共享出口换成另一个可执行但错误的出口。"""
    replacement = (
        DecisionBranch.PUBLISHED
        if target is DecisionBranch.MATCH_FAILED
        else DecisionBranch.MATCH_FAILED
    )
    match_reason = decision.match_reason or "roster_not_found"
    return replace(decision, branch=replacement, match_reason=match_reason)


def _mutated_method(original: Callable[..., Any], target: DecisionBranch, hits: list[int]):
    """生成一个只改共享返回点的临时方法，供两个入口用例集共同承受。"""
    def mutated(self, *args: Any, **kwargs: Any) -> UserDecision:
        decision = original(self, *args, **kwargs)
        if isinstance(decision, UserDecision) and decision.branch is target:
            hits[0] += 1
            return _wrong_branch(decision, target)
        return decision

    return mutated


def _run_existing_entry_suite(module, target: DecisionBranch) -> tuple[int, int, int]:
    """程序化加载一个入口的既有测试模块，并返回命中数、失败数、错误数。"""
    hits = [0]
    method_name = _BRANCH_RETURN_POINTS[target]
    original = getattr(UserPermissionDecisionTree, method_name)
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    result = unittest.TestResult()
    with mock.patch.object(
        UserPermissionDecisionTree,
        method_name,
        new=_mutated_method(original, target, hits),
    ):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            suite.run(result)
    return hits[0], len(result.failures), len(result.errors)


class SharedDecisionTreeMutationTests(unittest.TestCase):
    def test_breaking_any_shared_branch_reddens_both_entries(self) -> None:
        """逐个改坏共享决策树的十个出口；每日批与定向入口的既有用例都必须失败。"""
        entry_modules = (
            ("daily", refresh),
            ("targeted", targeted),
        )
        self.assertEqual(set(_BRANCH_RETURN_POINTS), set(DecisionBranch))
        for branch in tuple(DecisionBranch):
            with self.subTest(branch=branch.value):
                for entry, module in entry_modules:
                    with self.subTest(entry=entry):
                        hits, failures, errors = _run_existing_entry_suite(module, branch)
                        self.assertGreater(hits, 0, f"{entry} 用例没有触发共享出口 {branch.value}")
                        self.assertGreater(
                            failures + errors,
                            0,
                            f"{entry} 入口在共享出口 {branch.value} 被改坏后仍全绿",
                        )


class NegativeAssertionCoverageTests(unittest.TestCase):
    def test_negative_assertions_cover_the_shared_tree_location(self) -> None:
        """六条既有源码否定断言的扫描集合必须包含新的共享决策树路径。"""
        shared_path = (
            pathlib.Path(__file__).parents[1]
            / "src"
            / "lingxi"
            / "core"
            / "permission"
            / "user_decision_tree.py"
        )
        self.assertIn(shared_path, refresh.DUTY_SOURCES)
        self.assertIn("class UserPermissionDecisionTree", refresh.duty_code())

        test_tree = ast.parse((TESTS_ROOT / "test_permission_refresh_duty.py").read_text())
        scanning_methods = []
        for node in ast.walk(test_tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "duty_code"
                for call in ast.walk(node)
            ):
                scanning_methods.append(node.name)
        self.assertGreaterEqual(len(scanning_methods), 6, "六条否定断言的扫描调用不能静默消失")
        self.assertEqual(len(refresh.DUTY_SOURCES), 3)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
