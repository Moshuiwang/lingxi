"""执行层工具边界与合成审计链的断言（V-执行-01…17、22、23）。

对应 Issue #29。这些用例不依赖 Claude Agent SDK、模型额度或任何外部系统：
判定逻辑全部在 ``lingxi.core.execution`` 里，因此可以在 CI 的 gate 中强制执行。
真实链路上"被拒后确实无副作用"的部分由 L4a 受控验证补齐，不在本文件范围内。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
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

    def test_builtin_and_delegating_tools_cannot_be_added_to_the_core_allowlist(self) -> None:
        """核心层也必须拒绝把内置或子代理工具配置成显式放行项。"""

        for tool_name in ("Agent", "Task", "Write", "Bash"):
            with self.subTest(tool=tool_name), self.assertRaises(ToolPolicyError):
                ToolPolicy(allowed_tools=(tool_name,))


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

    def test_v_zhixing_22_subagent_tool_is_denied_by_default_under_both_known_names(self) -> None:
        """V-执行-22：子代理默认不放行。

        L4a 实测（Issue #29）：子代理工具在 CLI 2.1.220 里叫 ``Agent`` 而不是
        ``Task``，子代理内部的调用**确实经过同一个 PreToolUse 屏障**；但启用它会让
        同一回合出现两个终止结果，与"恰好一次终止结果"冲突，因此首期仍默认拒绝。
        """

        policy = build_policy()

        self.assertIs(policy.decide("Agent", {}).decision, ToolDecision.DENY)
        self.assertIs(policy.decide("Task", {}).decision, ToolDecision.DENY)

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

    def test_reason_forbids_the_model_from_attributing_the_denial_to_the_user(self) -> None:
        """Issue #291：真实事故里模型把本侧白名单配错翻译成"用户账号缺权限"，
        四条回复一致建议"联系数据平台管理员""重新登录后重试"——而用户权限完全
        正常。模板必须显式指向系统侧、显式禁止这两条误导性建议，不能只靠"用
        业务语言说明"这种开放式措辞，那正是旧模板放过这次编造归因的原因。"""

        reason = build_policy().decide("CronCreate", {}).model_reason or ""

        self.assertIn("系统侧", reason)
        self.assertIn("已经被记录", reason)
        self.assertIn("不要说这与用户的账号、权限或登录状态有关", reason)
        self.assertIn("不要建议用户重新登录、联系管理员或自行申请权限", reason)


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

    def test_v_shenji_03_a_credential_used_as_a_field_name_does_not_survive(self) -> None:
        """字段名同样是模型可控文本：只记 `omitted` 不代表键名不会落库。"""

        gateway = build_gateway()
        pre_tool_use(
            gateway,
            "mcp__bi-metric__describe_metric",
            {"sk-live-abcdef1234567890XYZ": "x", "metric": "收视率"},
        )

        recorded = gateway.audit.summary().calls[0].tool_input

        self.assertNotIn("sk-live-abcdef1234567890XYZ", " ".join(recorded))
        self.assertIn("metric", recorded, "合法字段名不受影响")

    def test_v_shenji_03_credentials_inside_error_text_are_redacted_too(self) -> None:
        """V-审计-03 的否定断言：错误原文不经字段白名单，仍不得落下凭据。"""

        gateway = build_gateway()
        pre_tool_use(gateway, "mcp__bi-metric__describe_metric", {"metric": "收视率"}, tool_use_id="toolu_e")
        asyncio.run(
            gateway.on_hook_event(
                {
                    "hook_event_name": "PostToolUseFailure",
                    "tool_name": "mcp__bi-metric__describe_metric",
                    "tool_use_id": "toolu_e",
                    "error": (
                        'upstream 401: {"api_key": "sk-live-DO-NOT-LEAK", "authorization": "Bearer sk-live-DO-NOT-LEAK"} '
                        "retry with Authorization: Bearer sk-live-DO-NOT-LEAK"
                    ),
                }
            )
        )

        recorded = gateway.audit.summary().failed_calls[0].error or ""

        self.assertNotIn("sk-live-DO-NOT-LEAK", recorded)
        self.assertIn("upstream 401", recorded)
        self.assertIn("[REDACTED]", recorded)

    def test_credentials_inside_ungated_tool_result_are_redacted_too(self) -> None:
        """未经本层判定的错误回执同样要脱敏，否则规则层拦截会成为泄露口。"""

        gateway = build_gateway()
        gateway.audit.record_tool_result(
            tool_use_id="toolu_orphan",
            content='auth failed: password=hunter2-DO-NOT-LEAK',
            is_error=True,
        )

        recorded = gateway.audit.summary().ungated_calls[0].error or ""

        self.assertNotIn("hunter2-DO-NOT-LEAK", recorded)
        self.assertIn("[REDACTED]", recorded)


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


class UngatedCallAuditTest(unittest.TestCase):
    """V-执行-09：规则层拦下的调用不经过执行层判定，审计必须仍能发现它。

    L4a 实测（Issue #29）：把工具放进 ``disallowed_tools`` 后，模型确实发出了该
    调用，但 CLI 在 ``PreToolUse`` **之前**就挡掉了——hook 一次都没触发。消息流里
    只留下一个 ``is_error=True`` 的工具结果块，其 ``tool_use_id`` 在执行层的记账里
    找不到对应记录。若直接丢弃，这次拦截在审计里就彻底消失了。
    """

    def test_v_zhixing_09_error_result_without_a_pre_tool_use_record_is_kept_as_ungated(self) -> None:
        gateway = build_gateway()
        pre_tool_use(gateway, "mcp__bi-metric__list_metrics", {}, tool_use_id="toolu_ok")
        gateway.audit.record_tool_result(tool_use_id="toolu_ok", content='{"data": [{"metric": "收视率"}]}')
        gateway.audit.record_tool_result(
            tool_use_id="toolu_never_gated",
            content="No such tool available: Write. Write exists but is not enabled in this context.",
            is_error=True,
        )

        summary = gateway.audit.summary()

        self.assertEqual(len(summary.ungated_calls), 1)
        ungated = summary.ungated_calls[0]
        self.assertEqual(ungated.tool_use_id, "toolu_never_gated")
        self.assertFalse(ungated.gated)
        self.assertFalse(ungated.denied, "本层没有拒绝它，不能记成本层拦的")
        self.assertIs(ungated.result_kind, ToolResultKind.TOOL_ERROR)
        self.assertIn("not enabled in this context", ungated.error or "")

    def test_successful_but_ungated_calls_are_recorded_instead_of_silently_dropped(self) -> None:
        """执行成功却没经过本层判定，是屏障失效的信号，必须留痕。

        原实现只给失败回执建记录，成功的直接丢弃——于是"绕过了判定但跑成功了"
        这种最危险的情况恰好隐身，审计还会反过来断言这个回合没有任何工具调用。
        """

        audit = TurnAudit()
        audit.record_tool_result(tool_use_id="toolu_unknown", content='{"data": [{"value": 1}]}')

        summary = audit.summary()

        self.assertEqual(len(summary.ungated_calls), 1)
        self.assertIsNot(summary.user_result, UserResultStatus.NO_TOOL_CALL)
        self.assertIsNone(summary.ungated_calls[0].error, "成功回执不留业务数据正文")

    def test_executed_tool_without_any_decision_is_recorded_as_ungated(self) -> None:
        """PostToolUse 找不到判定记录 = 有工具绕过了屏障并真的执行了。"""

        gateway = build_gateway()
        asyncio.run(
            gateway.on_hook_event(
                {"hook_event_name": "PostToolUse", "tool_name": "Write", "tool_use_id": "toolu_bypass"}
            )
        )

        summary = gateway.audit.summary()

        self.assertEqual(len(summary.ungated_calls), 1)
        self.assertEqual(summary.ungated_calls[0].tool_name, "Write")
        self.assertTrue(summary.ungated_calls[0].executed)
        self.assertIsNot(summary.user_result, UserResultStatus.NO_TOOL_CALL)

    def test_unknown_hook_event_name_carrying_a_tool_leaves_a_trace(self) -> None:
        """认不出的事件名带着工具名回调进来时必须留痕，不得当作无事发生。

        **本用例不证明"SDK 改名会被发现"**：真的改名时我们注册的那个名字压根不会
        再被调用，本分支进不去。它只覆盖「SDK 用认不出的名字回调我们已注册的
        matcher」这一种。边界见 V-执行-11。
        """

        gateway = build_gateway()
        asyncio.run(
            gateway.on_hook_event(
                {"hook_event_name": "PreToolUseV2", "tool_name": "Write", "tool_input": {}}
            )
        )

        self.assertEqual(len(gateway.audit.summary().ungated_calls), 1)

    def test_a_turn_whose_only_failure_was_ungated_is_not_reported_as_denied_by_us(self) -> None:
        gateway = build_gateway()
        gateway.audit.record_tool_result(tool_use_id="toolu_x", content="blocked upstream", is_error=True)

        summary = gateway.audit.summary()

        self.assertEqual(summary.denied_calls, ())
        self.assertEqual(len(summary.failed_calls), 1)
        self.assertIs(summary.user_result, UserResultStatus.NOT_OBTAINED)


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


class MixedTurnResultTest(unittest.TestCase):
    """V-执行-17：一次辅助调用成功，不足以宣告整个回合"用户拿到了结果"。

    这是问数的常态路径：先 ``list_metrics`` 拿目录（成功），再 ``describe_metric``
    查具体指标（失败）。原实现见到任意一条 ``OK`` 就判 OBTAINED，于是审计会写成
    用户拿到了结果——`V-执行-06` 要防的正是这种谎报。本层分不清哪一次承载最终
    业务结果，因此混合回合一律记 UNKNOWN。
    """

    def _turn_after_a_successful_catalog_lookup(
        self, second_content: object, *, is_error: object = None, record_result: bool = True
    ) -> UserResultStatus:
        gateway = build_gateway()
        pre_tool_use(gateway, "mcp__bi-metric__list_metrics", {}, tool_use_id="toolu_catalog")
        gateway.audit.record_tool_result(
            tool_use_id="toolu_catalog", content='{"data": [{"metric": "收视率"}]}'
        )
        pre_tool_use(
            gateway, "mcp__bi-metric__describe_metric", {"metric": "收视率"}, tool_use_id="toolu_query"
        )
        if record_result:
            gateway.audit.record_tool_result(
                tool_use_id="toolu_query", content=second_content, is_error=is_error
            )
        gateway.audit.record_final_text("已按可用范围回答。")
        gateway.audit.record_terminal_result()
        return gateway.audit.summary().user_result

    def test_success_plus_business_failure_is_not_reported_as_obtained(self) -> None:
        self.assertIs(
            self._turn_after_a_successful_catalog_lookup('{"error": "metric not found"}'),
            UserResultStatus.UNKNOWN,
        )

    def test_success_plus_unclassified_result_is_not_reported_as_obtained(self) -> None:
        self.assertIs(
            self._turn_after_a_successful_catalog_lookup("查询失败，请稍后重试"),
            UserResultStatus.UNKNOWN,
        )

    def test_success_plus_missing_result_is_not_reported_as_obtained(self) -> None:
        """回执根本没到：连归类都没有，更不能算送达。"""

        self.assertIs(
            self._turn_after_a_successful_catalog_lookup(None, record_result=False),
            UserResultStatus.UNKNOWN,
        )

    def test_success_plus_tool_error_is_not_reported_as_obtained(self) -> None:
        self.assertIs(
            self._turn_after_a_successful_catalog_lookup("upstream exploded", is_error=True),
            UserResultStatus.UNKNOWN,
        )

    def test_success_plus_empty_result_is_not_reported_as_obtained(self) -> None:
        self.assertIs(
            self._turn_after_a_successful_catalog_lookup('{"data": []}'),
            UserResultStatus.UNKNOWN,
        )

    def test_every_allowed_call_succeeding_is_still_reported_as_obtained(self) -> None:
        """保守化不能把正常回合也一起判掉：全部成功仍然是 OBTAINED。"""

        self.assertIs(
            self._turn_after_a_successful_catalog_lookup('{"data": [{"unit": "%"}]}'),
            UserResultStatus.OBTAINED,
        )

    def test_a_turn_where_everything_was_denied_is_reported_as_not_obtained(self) -> None:
        gateway = build_gateway()
        pre_tool_use(gateway, "Bash", {"command": "ls"}, tool_use_id="toolu_d1")
        pre_tool_use(gateway, "Write", {"file_path": "/tmp/x"}, tool_use_id="toolu_d2")

        self.assertIs(gateway.audit.summary().user_result, UserResultStatus.NOT_OBTAINED)


class ParsableButUnrecognisedResultTest(unittest.TestCase):
    """V-执行-06 的否定断言：能解析成 JSON **不等于**用户拿到了结果。

    原实现在结构化规则没命中时直接返回 OK，于是
    ``{"message": "查询失败，请稍后重试"}`` 被判成成功——正是这条断言要防的错误。
    MCP 回执绝大多数是 JSON，所以这不是边角情况，是主路径。
    """

    def test_json_payload_without_recognisable_data_is_unknown_not_ok(self) -> None:
        for payload in (
            '{"message": "查询失败，请稍后重试"}',
            '{"count": 0}',
            '{"detail": {"reason": "unavailable"}}',
        ):
            with self.subTest(payload=payload):
                self.assertIs(classify_tool_result(payload), ToolResultKind.UNCLASSIFIED)

    def test_registered_failure_wording_is_not_short_circuited_by_a_json_array(self) -> None:
        rules = ResultRules(failure_text_markers=("指标不存在",))

        self.assertIs(
            classify_tool_result('["指标不存在，请确认指标名称。"]', rules=rules),
            ToolResultKind.BUSINESS_FAILURE,
        )

    def test_non_empty_recognised_collection_is_still_ok(self) -> None:
        self.assertIs(classify_tool_result('{"data": [{"metric": "收视率"}]}'), ToolResultKind.OK)

    def test_real_list_metrics_contract_is_success_only_when_non_empty(self) -> None:
        """#37 A1：真实成功载荷是顶层 metrics；空或形状模糊仍不得冒充成功。"""

        cases = {
            '{"metrics": [{"metric_id": "revenue"}]}': ToolResultKind.OK,
            '{"metrics": []}': ToolResultKind.EMPTY_RESULT,
            '{"metrics": {"metric_id": "revenue"}}': ToolResultKind.UNCLASSIFIED,
            '{"metrics": [null]}': ToolResultKind.UNCLASSIFIED,
            '{"metrics": [{}]}': ToolResultKind.UNCLASSIFIED,
            '{"metrics": [{"error": "upstream failed"}]}': ToolResultKind.BUSINESS_FAILURE,
            '{"metrics": [{"success": false}]}': ToolResultKind.BUSINESS_FAILURE,
            '{"metrics": [{"status": "failed"}]}': ToolResultKind.BUSINESS_FAILURE,
            '{"metrics": [{"metric_id": "revenue"}], "error": "upstream failed"}': ToolResultKind.BUSINESS_FAILURE,
            '{"data": [{"value": 1}], "metrics": null}': ToolResultKind.UNCLASSIFIED,
            '{"data": [{"value": 1}], "metrics": "not-a-collection"}': ToolResultKind.UNCLASSIFIED,
            '{"data": [{"value": 1}], "metrics": {"metric_id": "revenue"}}': ToolResultKind.UNCLASSIFIED,
            '{"data": [{"value": 1}], "metrics": []}': ToolResultKind.UNCLASSIFIED,
            '{"data": [{"value": 1}], "metrics": [{}]}': ToolResultKind.UNCLASSIFIED,
            '{"data": [{"value": 1}], "metrics": [{"error": "upstream failed"}]}': ToolResultKind.BUSINESS_FAILURE,
        }
        for payload, expected in cases.items():
            with self.subTest(payload=payload):
                self.assertIs(classify_tool_result(payload), expected)

    def test_classification_does_not_depend_on_key_order(self) -> None:
        """多个登记集合同时出现时，分类不得取决于键的遍历顺序。

        直接断言"换个顺序结果不变"，而不是断言某个具体取值：后者只有在 str hash
        随机化恰好排出坏顺序时才会红，实测 8 个 PYTHONHASHSEED 里只有 3 个能抓到
        回归——那是个碰运气的检测器，CI 会漏掉一半以上。
        """

        for payload in ('{"data": [], "items": [1]}', '{"rows": [1], "results": []}'):
            with self.subTest(payload=payload):
                forward = ResultRules(empty_collection_keys=("data", "rows", "items", "results", "metrics"))
                reverse = ResultRules(empty_collection_keys=("metrics", "results", "items", "rows", "data"))
                self.assertIs(
                    classify_tool_result(payload, rules=forward),
                    classify_tool_result(payload, rules=reverse),
                )
                self.assertIs(classify_tool_result(payload, rules=forward), ToolResultKind.OK)

        self.assertIs(classify_tool_result('{"data": [], "rows": []}'), ToolResultKind.EMPTY_RESULT)

    def test_v_zhixing_15_classification_is_stable_across_eight_hash_seeds(self) -> None:
        """V-执行-15：回执归类不能靠某个进程的字符串哈希排列碰巧正确。"""

        source = (
            "from lingxi.core.execution.audit import ResultRules, classify_tool_result; "
            "payload = {'data': [{'value': 1}], 'metrics': [{}]}; "
            "rules = ResultRules(empty_collection_keys=frozenset({'data', 'rows', 'items', 'results', 'metrics'})); "
            "print(classify_tool_result(payload, rules=rules).value)"
        )
        environment = os.environ.copy()
        source_root = os.path.join(os.path.dirname(__file__), "..", "src")
        environment["PYTHONPATH"] = os.path.abspath(source_root)
        observed = []
        for seed in range(8):
            environment["PYTHONHASHSEED"] = str(seed)
            completed = subprocess.run(
                [sys.executable, "-c", source],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )
            observed.append(completed.stdout.strip())

        self.assertEqual(observed, [ToolResultKind.UNCLASSIFIED.value] * 8)

    def test_non_empty_top_level_array_is_not_success_by_itself(self) -> None:
        """顶层数组非空**不等于**用户拿到了结果。

        反例来自 Codex 只读审核（PR #36）：原实现对任何非空顶层数组直接判 OK，
        于是 `[{"error": ...}]` 和 `[{"success": false}]` 都成了"成功"。这是
        字典路径上那个坑的数组版本，我第一次只修了字典。
        """

        cases = {
            '[{"error":"metric not found"}]': ToolResultKind.BUSINESS_FAILURE,
            '[{"success":false}]': ToolResultKind.BUSINESS_FAILURE,
            '[{"status":"failed"}]': ToolResultKind.BUSINESS_FAILURE,
            '["查询失败，请稍后重试"]': ToolResultKind.UNCLASSIFIED,
            '[{"metric":"收视率"}]': ToolResultKind.UNCLASSIFIED,
            "[]": ToolResultKind.EMPTY_RESULT,
        }
        for payload, expected in cases.items():
            with self.subTest(payload=payload):
                self.assertIs(classify_tool_result(payload), expected)

    def test_registered_collection_keys_are_declared_as_an_ordered_sequence(self) -> None:
        """字段本身也不能退回 set：那样默认规则的遍历顺序又会随进程变化。"""

        self.assertIsInstance(ResultRules().empty_collection_keys, tuple)


class AmbiguousAttributionTest(unittest.TestCase):
    """回执认不出属于哪一次调用时，宁可另记一条，也不猜。"""

    def test_failure_without_id_is_not_pinned_onto_one_of_two_same_named_calls(self) -> None:
        gateway = build_gateway()
        pre_tool_use(gateway, "mcp__bi-metric__list_metrics", {}, tool_use_id="t1")
        pre_tool_use(gateway, "mcp__bi-metric__list_metrics", {}, tool_use_id="t2")
        asyncio.run(
            gateway.on_hook_event(
                {
                    "hook_event_name": "PostToolUseFailure",
                    "tool_name": "mcp__bi-metric__list_metrics",
                    "error": "boom",
                }
            )
        )

        summary = gateway.audit.summary()
        gated = [call for call in summary.calls if call.gated]

        self.assertTrue(all(call.error is None for call in gated), "不得把错误挂到某一次放行调用上")
        self.assertEqual(len(summary.ungated_calls), 1)
        self.assertEqual(summary.ungated_calls[0].error, "boom")

    def test_single_pending_call_still_matches_by_name(self) -> None:
        gateway = build_gateway()
        pre_tool_use(gateway, "mcp__bi-metric__list_metrics", {}, tool_use_id="t1")
        asyncio.run(
            gateway.on_hook_event(
                {
                    "hook_event_name": "PostToolUseFailure",
                    "tool_name": "mcp__bi-metric__list_metrics",
                    "error": "boom",
                }
            )
        )

        summary = gateway.audit.summary()

        self.assertEqual(summary.ungated_calls, ())
        self.assertEqual(summary.calls[0].error, "boom")


class TurnBoundaryTest(unittest.TestCase):
    """``ClaudeAgentOptions.hooks`` 是会话级的，一个会话跑多个回合。"""

    def test_second_turn_does_not_inherit_the_first_turns_terminal_result(self) -> None:
        gateway = build_gateway()
        pre_tool_use(gateway, "mcp__bi-metric__list_metrics", {}, tool_use_id="t1")
        gateway.audit.record_final_text("第一回合")
        gateway.audit.record_terminal_result()
        self.assertTrue(gateway.audit.summary().terminal_ok)

        gateway.audit.start_turn()
        gateway.audit.record_final_text("第二回合")
        gateway.audit.record_terminal_result()

        summary = gateway.audit.summary()

        self.assertEqual(summary.terminal_result_count, 1)
        self.assertTrue(summary.terminal_ok, "不翻页的话第二回合起 terminal_ok 会恒为假")
        self.assertEqual(summary.calls, (), "上一回合的调用不得混进本回合的结论")


class DecisionSurvivesAuditFailureTest(unittest.TestCase):
    """记账处理的是模型可控的入参；它出错不得把拒绝一起带走。"""

    def test_deny_is_still_returned_when_bookkeeping_raises(self) -> None:
        class ExplodingAudit(TurnAudit):
            def record_decision(self, **_: object) -> None:
                raise RuntimeError("SIMULATED_AUDIT_FAULT: controlled test fault probe")

        gateway = ToolGateway(policy=build_policy(), audit=ExplodingAudit())

        result = pre_tool_use(gateway, "Write", {"file_path": "/tmp/x"})

        self.assertEqual(deny_decision(result), "deny")
        self.assertEqual(len(gateway.audit.summary().calls), 1, "明细丢了也要留下一条痕迹")

    def test_deeply_nested_model_controlled_input_does_not_break_the_gate(self) -> None:
        payload: object = "leaf"
        for _ in range(30000):
            payload = {"metric": payload}

        result = pre_tool_use(gateway := build_gateway(), "Write", {"metric": payload})

        self.assertEqual(deny_decision(result), "deny")
        self.assertEqual(len(gateway.audit.summary().calls), 1)


class CredentialsNeverReachTheAuditTest(unittest.TestCase):
    """产品合同：不在审计中保存凭据、完整令牌。这是绝对要求，不是尽力而为。

    认键名永远盖不住没有键名上下文的裸令牌，所以自由文本另加一道**结构性上界**：
    16 字符以上的令牌字符连串一律抹掉。反例来自 Codex 只读审核（PR #36）。
    """

    def test_whitelisted_field_values_are_not_stored_verbatim(self) -> None:
        """字段进白名单只决定"记不记这个字段"，不代表它的值可以原样落库。"""

        redactor = AuditRedactor(allowed_input_fields=("metric",))

        recorded = redactor.redact({"metric": "sk-live-abcdef1234567890XYZ"})

        self.assertNotIn("sk-live-abcdef1234567890XYZ", str(recorded))

    def test_bare_token_without_any_key_context_is_masked(self) -> None:
        gateway = build_gateway()
        pre_tool_use(gateway, "mcp__bi-metric__list_metrics", {}, tool_use_id="t1")
        asyncio.run(
            gateway.on_hook_event(
                {
                    "hook_event_name": "PostToolUseFailure",
                    "tool_name": "mcp__bi-metric__list_metrics",
                    "tool_use_id": "t1",
                    "error": "upstream rejected sk-live-abcdef1234567890XYZ",
                }
            )
        )

        recorded = gateway.audit.summary().calls[0].error or ""

        self.assertNotIn("sk-live-abcdef1234567890XYZ", recorded)
        self.assertIn("upstream rejected", recorded, "脱敏不该把可诊断信息一起吃掉")

    def test_ungated_result_body_is_masked_too(self) -> None:
        audit = TurnAudit()
        audit.record_tool_result(
            tool_use_id="orphan",
            content="blocked: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
            is_error=True,
        )

        recorded = audit.summary().ungated_calls[0].error or ""

        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", recorded)

    def test_legitimate_mcp_tool_names_survive_intact(self) -> None:
        """脱敏不得吃掉合法工具名——那是必须保留的审计事实。

        用带数字的版本化工具名，因为它**会**命中长串规则：不带数字的名字
        本来就不会被抹，用它做断言等于什么都没测（变异验证时发现的）。
        """

        policy = ToolPolicy(allowed_tools=("mcp__bi_metric__list_metrics_v2",))
        gateway = ToolGateway(policy=policy, audit=TurnAudit())
        pre_tool_use(gateway, "mcp__bi_metric__list_metrics_v2", {}, tool_use_id="t1")

        self.assertEqual(gateway.audit.summary().calls[0].tool_name, "mcp__bi_metric__list_metrics_v2")

    def test_useful_error_wording_survives(self) -> None:
        """L4a 靠这段原文才发现规则层拦截；脱敏不能把它抹没。"""

        audit = TurnAudit()
        audit.record_tool_result(
            tool_use_id="orphan",
            content="Error: No such tool available: Write. Write exists but is not enabled in this context.",
            is_error=True,
        )

        recorded = audit.summary().ungated_calls[0].error or ""

        self.assertIn("No such tool available: Write", recorded)


class MalformedToolNameRedactionTest(unittest.TestCase):
    """畸形工具名是模型可控文本直达持久记录的一条路径。"""

    def test_credentials_in_a_malformed_tool_name_do_not_reach_the_audit(self) -> None:
        gateway = build_gateway()
        pre_tool_use(gateway, "Bash access_key=sk-live-DO-NOT-LEAK", {})

        recorded = gateway.audit.summary().calls[0].tool_name

        self.assertNotIn("sk-live-DO-NOT-LEAK", recorded)
        self.assertIn("[REDACTED]", recorded)


if __name__ == "__main__":
    unittest.main()
