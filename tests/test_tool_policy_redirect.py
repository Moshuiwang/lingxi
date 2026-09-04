"""「查询包装式越界」拒绝文案分支化（Issue #349，2026-08-27 生产事故修复）。

事故经过：qwen3.7-plus 概率性地用 ``Bash: claude mcp call query …`` 包装调用问数
MCP，命中白名单拒绝；旧版 ``DENY_REASON_TEMPLATE`` 里"也不要改用其他方式绕过它"
一句话把模型接下来想改用原生 ``mcp__query__`` 工具的**正确**路径也一并禁止了，
导致模型向用户回复"这一部分暂时无法查询、系统侧临时限制"——而查询能力本身完全
可用。本文件断言的是 :mod:`lingxi.core.execution.tool_policy` 里新增的分支：被拒
调用不是原生查询工具、但白名单里确实有查询工具时，改用导回模板
``DENY_REDIRECT_TEMPLATE``；其余情况维持旧模板不变。
"""

from __future__ import annotations

import unittest

from lingxi.core.execution.tool_policy import (
    DENY_REASON_TEMPLATE,
    DENY_REDIRECT_TEMPLATE,
    DenyReasonCode,
    ToolDecision,
    ToolPolicy,
)


class WrappedQueryCallIsRedirectedToTheNativeToolTest(unittest.TestCase):
    """①：Bash 被拒且白名单含查询工具——必须导回原生查询工具，不得宣称查询不可用。"""

    def test_denied_wrapper_call_is_redirected_when_a_native_query_tool_is_whitelisted(
        self,
    ) -> None:
        policy = ToolPolicy(allowed_tools=("mcp__query__query_metric",))

        verdict = policy.decide("Bash", {"command": "claude mcp call query query_metric"})

        self.assertIs(verdict.decision, ToolDecision.DENY)
        self.assertIs(verdict.reason_code, DenyReasonCode.NOT_IN_WHITELIST_QUERY_REDIRECT)
        reason = verdict.model_reason or ""
        self.assertIn("原生查询工具", reason, "必须把模型导回原生查询工具，这是本次修复的核心语义")
        self.assertNotIn("暂时无法查询", reason, "不得让模型认为查询能力本身不可用")
        self.assertNotIn("不可用", reason, "同上：这类字样即使写在否定句里模型也会直接转述")

    def test_redirect_reason_still_carries_the_shared_discipline_sentences(self) -> None:
        """导回模板不是另起炉灶：不重试原方式、不暴露内部工具名/规则、不甩锅用户三条纪律都要保留。"""

        policy = ToolPolicy(allowed_tools=("mcp__query__query_metric",))

        reason = policy.decide("Bash", {}).model_reason or ""

        self.assertIn("不要重试", reason)
        self.assertIn("不要向用户提及内部工具名称", reason)
        self.assertIn("不要说这与用户的账号、权限或登录状态有关", reason)
        self.assertIn("不要建议用户重新登录、联系管理员或自行申请权限", reason)


class NoQueryToolAvailableKeepsTheOldTemplateTest(unittest.TestCase):
    """②哨兵：白名单里没有任何查询工具时，不得许诺一个不存在的替代路径。"""

    def test_denied_call_keeps_the_original_template_when_no_query_tool_is_whitelisted(
        self,
    ) -> None:
        policy = ToolPolicy(allowed_tools=("mcp__bi-metric__list_metrics",))

        verdict = policy.decide("Bash", {})

        self.assertIs(verdict.decision, ToolDecision.DENY)
        self.assertIs(verdict.reason_code, DenyReasonCode.NOT_IN_WHITELIST)
        self.assertEqual(verdict.model_reason, DENY_REASON_TEMPLATE)
        self.assertNotEqual(verdict.model_reason, DENY_REDIRECT_TEMPLATE)


class DeniedQueryToolItselfIsNotRedirectedTest(unittest.TestCase):
    """③：被拒的调用本身已经是 mcp__query__ 前缀，导回它自己没有意义。"""

    def test_an_unapproved_query_tool_name_does_not_take_the_redirect_branch(self) -> None:
        """白名单只放了 query_metric，模型调用了同前缀下未获批准的 describe_metric。"""

        policy = ToolPolicy(allowed_tools=("mcp__query__query_metric",))

        verdict = policy.decide("mcp__query__describe_metric", {})

        self.assertIs(verdict.decision, ToolDecision.DENY)
        self.assertIs(verdict.reason_code, DenyReasonCode.NOT_IN_WHITELIST)
        self.assertEqual(verdict.model_reason, DENY_REASON_TEMPLATE)


class NeitherTemplatePromisesTheDenialWasRecordedTest(unittest.TestCase):
    """④：判定层本身不写记录/告警系统，两个模板都不得再许诺"问题已经被记录"。"""

    def test_the_original_template_does_not_promise_the_denial_was_recorded(self) -> None:
        policy = ToolPolicy(allowed_tools=("mcp__bi-metric__list_metrics",))

        reason = policy.decide("Bash", {}).model_reason or ""

        self.assertNotIn("已经被记录", reason)

    def test_the_redirect_template_does_not_promise_the_denial_was_recorded(self) -> None:
        policy = ToolPolicy(allowed_tools=("mcp__query__query_metric",))

        reason = policy.decide("Bash", {}).model_reason or ""

        self.assertNotIn("已经被记录", reason)


if __name__ == "__main__":
    unittest.main()
