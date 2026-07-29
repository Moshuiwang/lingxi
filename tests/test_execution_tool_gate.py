"""执行层工具边界与合成审计链的断言（V-执行-01…06）。

对应 Issue #29。这些用例不依赖 Claude Agent SDK、模型额度或任何外部系统：
判定逻辑全部在 ``lingxi.core.execution`` 里，因此可以在 CI 的 gate 中强制执行。
真实链路上"被拒后确实无副作用"的部分由 L4a 受控验证补齐，不在本文件范围内。
"""

from __future__ import annotations

import asyncio
import unittest

from lingxi.core.execution.audit import (
    AuditRedactor,
    ResultRules,
    ToolResultKind,
    TurnAudit,
    UserResultStatus,
    classify_tool_result,
)
from lingxi.core.execution.hooks import ToolGateway
from lingxi.core.execution.tool_policy import (
    DenyReasonCode,
    ToolDecision,
    ToolPolicy,
    ToolPolicyError,
)

READ_ONLY_TOOLS = (
    "mcp__bi-metric__list_metrics",
    "mcp__bi-metric__describe_metric",
    "Skill",
)
APPROVED_SKILLS = ("lingxi-readonly-bi-report",)


def build_policy() -> ToolPolicy:
    return ToolPolicy(allowed_tools=READ_ONLY_TOOLS, allowed_skills=APPROVED_SKILLS)


def build_gateway(rules: ResultRules | None = None) -> ToolGateway:
    audit = TurnAudit(rules=rules, redactor=AuditRedactor(allowed_input_fields=("metric", "skill")))
    return ToolGateway(policy=build_policy(), audit=audit)


def pre_tool_use(gateway: ToolGateway, tool_name: object, tool_input: object = None, tool_use_id: str = "toolu_1") -> dict:
    return asyncio.run(
        gateway.on_hook_event(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_use_id": tool_use_id,
            }
        )
    )


def deny_decision(result: dict) -> str | None:
    return result.get("hookSpecificOutput", {}).get("permissionDecision")


class ToolPolicyConfigurationTest(unittest.TestCase):
    """白名单配置本身必须在构造期就拒绝不安全写法。"""

    def test_wildcard_entries_are_rejected_so_new_server_tools_cannot_self_authorize(self) -> None:
        with self.assertRaises(ToolPolicyError):
            ToolPolicy(allowed_tools=("mcp__bi-metric__*",))

    def test_empty_whitelist_is_rejected_instead_of_silently_allowing_nothing(self) -> None:
        with self.assertRaises(ToolPolicyError):
            ToolPolicy(allowed_tools=())

    def test_a_bare_string_is_not_mistaken_for_a_set_of_tool_names(self) -> None:
        with self.assertRaises(ToolPolicyError):
            ToolPolicy(allowed_tools="Skill")

    def test_allowing_the_skill_tool_requires_naming_the_approved_skills(self) -> None:
        with self.assertRaises(ToolPolicyError):
            ToolPolicy(allowed_tools=("Skill",))


class DenyByDefaultTest(unittest.TestCase):
    """V-执行-01 / V-执行-02：白名单外的调用在执行前被拒。"""

    def setUp(self) -> None:
        self.gateway = build_gateway()

    def test_v_zhixing_01_non_whitelisted_tool_is_denied_before_execution(self) -> None:
        for tool_name in ("Bash", "Write", "CronCreate"):
            with self.subTest(tool=tool_name):
                gateway = build_gateway()
                result = pre_tool_use(gateway, tool_name, {"command": "ls"})

                self.assertEqual(deny_decision(result), "deny")
                summary = gateway.audit.summary()
                self.assertEqual(summary.denied_tool_names, (tool_name,))
                self.assertEqual(summary.executed_tool_names, ())

    def test_v_zhixing_02_an_unknown_builtin_tool_falls_into_the_deny_branch(self) -> None:
        """新增内置工具不需要出现在任何禁用名单里，默认就被拒。"""

        verdict = build_policy().decide("SomeToolAddedByAFutureSdkRelease", {})

        self.assertIs(verdict.decision, ToolDecision.DENY)
        self.assertIs(verdict.reason_code, DenyReasonCode.NOT_IN_WHITELIST)

    def test_whitelisted_read_only_tool_is_allowed_and_not_denied(self) -> None:
        result = pre_tool_use(self.gateway, "mcp__bi-metric__list_metrics", {})

        self.assertEqual(result, {})
        self.assertEqual(self.gateway.audit.summary().denied_calls, ())

    def test_subagent_task_tool_is_denied_because_its_inner_boundary_is_unverified(self) -> None:
        """子代理内部的工具拒绝归属尚未验证，因此 Task 不在白名单里。"""

        self.assertIs(build_policy().decide("Task", {}).decision, ToolDecision.DENY)

    def test_missing_or_malformed_tool_name_is_denied_rather_than_allowed(self) -> None:
        for tool_name in (None, "", 42, "mcp__bi-metric__list_metrics; rm -rf /"):
            with self.subTest(tool=tool_name):
                verdict = build_policy().decide(tool_name, {})

                self.assertIs(verdict.decision, ToolDecision.DENY)
                self.assertIs(verdict.reason_code, DenyReasonCode.MALFORMED_TOOL_NAME)

    def test_approved_skill_is_allowed_but_an_unapproved_skill_name_is_denied(self) -> None:
        policy = build_policy()

        approved = policy.decide("Skill", {"skill": "lingxi-readonly-bi-report", "args": "查询本周收视率"})
        unapproved = policy.decide("Skill", {"skill": "some-other-skill"})
        missing = policy.decide("Skill", {"args": "没有指定 skill"})

        self.assertIs(approved.decision, ToolDecision.ALLOW)
        self.assertIs(unapproved.reason_code, DenyReasonCode.SKILL_NOT_APPROVED)
        self.assertIs(missing.reason_code, DenyReasonCode.MISSING_SKILL_NAME)


class DenyReasonTextTest(unittest.TestCase):
    """拒绝理由原样进入模型上下文，因此它的措辞是被断言的产品行为。"""

    def test_reason_tells_the_model_not_to_retry_and_not_to_expose_internal_tool_names(self) -> None:
        verdict = build_policy().decide("CronCreate", {})

        self.assertIsNotNone(verdict.model_reason)
        reason = verdict.model_reason or ""
        self.assertIn("不要重试", reason)
        self.assertIn("不要向用户提及内部工具名称", reason)

    def test_reason_does_not_leak_the_whitelist_contents_into_model_context(self) -> None:
        reason = build_policy().decide("Bash", {}).model_reason or ""

        for tool_name in READ_ONLY_TOOLS + APPROVED_SKILLS:
            self.assertNotIn(tool_name, reason)


class DeniedTurnStillClosesTest(unittest.TestCase):
    """V-执行-03：被拒回合仍取得非空最终正文和恰好一次终止结果。"""

    def test_v_zhixing_03_denied_turn_still_has_final_text_and_exactly_one_terminal_result(self) -> None:
        gateway = build_gateway()
        pre_tool_use(gateway, "Bash", {"command": "ls"}, tool_use_id="toolu_denied")
        pre_tool_use(gateway, "mcp__bi-metric__list_metrics", {}, tool_use_id="toolu_ok")
        gateway.audit.record_executed(tool_name="mcp__bi-metric__list_metrics", tool_use_id="toolu_ok")
        gateway.audit.record_tool_result(tool_use_id="toolu_ok", content='{"data": [{"metric": "收视率"}]}')
        gateway.audit.record_final_text("已按可用范围回答，其中一部分无法查询。")
        gateway.audit.record_terminal_result()

        summary = gateway.audit.summary()

        self.assertTrue(summary.terminal_ok)
        self.assertEqual(summary.terminal_result_count, 1)
        self.assertGreater(summary.final_text_bytes, 0)
        self.assertEqual(summary.denied_tool_names, ("Bash",))
        self.assertEqual(summary.executed_tool_names, ("mcp__bi-metric__list_metrics",))
        self.assertIs(summary.user_result, UserResultStatus.OBTAINED)

    def test_empty_final_text_or_duplicated_terminal_result_is_not_reported_as_closed(self) -> None:
        missing_text = TurnAudit()
        missing_text.record_terminal_result()

        duplicated = TurnAudit()
        duplicated.record_final_text("结果")
        duplicated.record_terminal_result()
        duplicated.record_terminal_result()

        self.assertFalse(missing_text.summary().terminal_ok)
        self.assertFalse(duplicated.summary().terminal_ok)


class ToolFailureAuditTest(unittest.TestCase):
    """V-执行-04：工具抛错时审计中可查到工具名、入参与错误。"""

    def test_v_zhixing_04_post_tool_use_failure_is_recorded_with_name_input_and_error(self) -> None:
        gateway = build_gateway()
        pre_tool_use(gateway, "mcp__bi-metric__describe_metric", {"metric": "收视率"}, tool_use_id="toolu_f")
        asyncio.run(
            gateway.on_hook_event(
                {
                    "hook_event_name": "PostToolUseFailure",
                    "tool_name": "mcp__bi-metric__describe_metric",
                    "tool_input": {"metric": "收视率"},
                    "tool_use_id": "toolu_f",
                    "error": "SIMULATED_UPSTREAM_FAILURE: controlled test fault probe",
                }
            )
        )

        summary = gateway.audit.summary()
        failed = summary.failed_calls

        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].tool_name, "mcp__bi-metric__describe_metric")
        self.assertEqual(failed[0].tool_input, {"metric": "收视率"})
        self.assertIn("SIMULATED_UPSTREAM_FAILURE", failed[0].error or "")
        self.assertIs(failed[0].result_kind, ToolResultKind.TOOL_ERROR)
        self.assertIs(summary.user_result, UserResultStatus.NOT_OBTAINED)

    def test_audit_input_fields_are_opt_in_and_credential_like_keys_are_redacted(self) -> None:
        """V-审计-02/03：新增字段默认不入审计明细，凭据一律不落。"""

        gateway = build_gateway()
        pre_tool_use(
            gateway,
            "mcp__bi-metric__describe_metric",
            {"metric": "收视率", "authorization": "Bearer real-token", "new_field": "未来新增"},
        )

        recorded = gateway.audit.summary().calls[0].tool_input

        self.assertEqual(recorded["metric"], "收视率")
        self.assertEqual(recorded["authorization"], "[REDACTED]")
        self.assertEqual(recorded["new_field"], {"omitted": True})


class DenialAuditTest(unittest.TestCase):
    """V-执行-05：调用被拒时审计中可查到该次拒绝及其理由。"""

    def test_v_zhixing_05_denial_is_booked_by_the_executor_itself_with_its_reason(self) -> None:
        gateway = build_gateway()

        pre_tool_use(gateway, "CronCreate", {"schedule": "* * * * *"}, tool_use_id="toolu_cron")

        denied = gateway.audit.summary().denied_calls

        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0].tool_name, "CronCreate")
        self.assertEqual(denied[0].tool_use_id, "toolu_cron")
        self.assertIs(denied[0].deny_reason_code, DenyReasonCode.NOT_IN_WHITELIST)
        self.assertTrue(denied[0].deny_reason_text)
        self.assertFalse(denied[0].executed)

    def test_denial_is_recorded_even_though_no_permission_hook_callback_ever_fires(self) -> None:
        """权限拒绝没有任何 hook 回调，记账必须由执行层自己完成。"""

        gateway = build_gateway()
        pre_tool_use(gateway, "Write", {"file_path": "/tmp/probe"}, tool_use_id="toolu_w")

        # 模拟 SDK 侧的真实观察：拒绝后既无 PostToolUse，也无 PermissionDenied 回调。
        summary = gateway.audit.summary()

        self.assertEqual(summary.denied_tool_names, ("Write",))
        self.assertEqual(summary.executed_tool_names, ())


class BusinessFailureAuditTest(unittest.TestCase):
    """V-执行-06：被 MCP 包成正常响应的业务失败，审计中可判定用户未拿到结果。"""

    def setUp(self) -> None:
        self.rules = ResultRules(failure_text_markers=("指标不存在",))

    def test_v_zhixing_06_business_failure_wrapped_as_a_normal_response_is_not_obtained(self) -> None:
        gateway = build_gateway(rules=self.rules)
        pre_tool_use(gateway, "mcp__bi-metric__describe_metric", {"metric": "不存在的指标"}, tool_use_id="toolu_b")
        asyncio.run(
            gateway.on_hook_event(
                {
                    "hook_event_name": "PostToolUse",
                    "tool_name": "mcp__bi-metric__describe_metric",
                    "tool_use_id": "toolu_b",
                }
            )
        )
        kind = gateway.audit.record_tool_result(
            tool_use_id="toolu_b",
            content=[{"type": "text", "text": "指标不存在，请确认指标名称。"}],
            is_error=False,
        )
        gateway.audit.record_final_text("没有查到这个指标。")
        gateway.audit.record_terminal_result()

        summary = gateway.audit.summary()

        self.assertIs(kind, ToolResultKind.BUSINESS_FAILURE)
        self.assertTrue(summary.terminal_ok, "回合正常收口，但用户并没有拿到业务结果")
        self.assertIs(summary.user_result, UserResultStatus.NOT_OBTAINED)
        self.assertEqual(summary.executed_tool_names, ("mcp__bi-metric__describe_metric",))

    def test_structured_failure_shapes_are_classified_without_relying_on_wording(self) -> None:
        cases = {
            '{"error": "metric not found"}': ToolResultKind.BUSINESS_FAILURE,
            '{"success": false, "message": "no such metric"}': ToolResultKind.BUSINESS_FAILURE,
            '{"status": "failed"}': ToolResultKind.BUSINESS_FAILURE,
            '{"data": []}': ToolResultKind.EMPTY_RESULT,
            '{"data": [{"value": 1}]}': ToolResultKind.OK,
        }
        for content, expected in cases.items():
            with self.subTest(content=content):
                self.assertIs(classify_tool_result(content, rules=self.rules), expected)

    def test_unrecognised_payload_is_reported_as_unknown_instead_of_assumed_delivered(self) -> None:
        """证据不足写未知：不能因为"没报错"就断定用户拿到了结果。"""

        gateway = build_gateway()
        pre_tool_use(gateway, "mcp__bi-metric__list_metrics", {}, tool_use_id="toolu_u")
        kind = gateway.audit.record_tool_result(
            tool_use_id="toolu_u",
            content="本次查询的说明文字，既不是结构化数据也不是已登记的失败措辞。",
        )
        gateway.audit.record_final_text("回答")
        gateway.audit.record_terminal_result()

        self.assertIs(kind, ToolResultKind.UNCLASSIFIED)
        self.assertIs(gateway.audit.summary().user_result, UserResultStatus.UNKNOWN)

    def test_protocol_level_tool_error_takes_precedence_over_content_parsing(self) -> None:
        self.assertIs(
            classify_tool_result('{"data": [{"value": 1}]}', is_error=True, rules=self.rules),
            ToolResultKind.TOOL_ERROR,
        )

    def test_turn_without_any_tool_call_is_not_claimed_as_a_delivered_result(self) -> None:
        audit = TurnAudit()
        audit.record_final_text("我直接回答了，没有查询任何数据。")
        audit.record_terminal_result()

        self.assertIs(audit.summary().user_result, UserResultStatus.NO_TOOL_CALL)


if __name__ == "__main__":
    unittest.main()
