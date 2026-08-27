"""文档交付触发机制的断言（Issue #341 S-ES-2：触发机制 worker 侧）。

覆盖三层，均不依赖真实模型额度或网络：

1. ``core/execution/document_delivery.py`` 的纯逻辑校验——归一化、硬上限、复用
   ``constrain_output`` 的出口安全检查；
2. ``apps/worker/config.py`` 的 ``LINGXI_WORKER_DOCUMENT_DELIVERY_ENABLED`` 开关
   解析；
3. ``apps/worker/turn.py`` 的装配——ToolPolicy 白名单合入、真实 Claude Agent SDK
   ``create_sdk_mcp_server``/``@tool`` 的实际调用形状（不打桩：``claude_agent_sdk``
   包在本仓库 ``worker`` extras 里是真实依赖，构造 MCP 服务本身是纯 Python 对象
   装配、不发网络请求，直接用真包能验证到"确实按这个签名能调通"，比自造桩更
   贴近真实失败面）、以及只把驱动模型输出的 ``run_single_turn`` 换成脚本化假
   实现来模拟"模型调用了这个工具"。

变异锚点（改坏其一，对应用例必须变红）：
- ①：把字面量工具名从 ``ToolPolicy`` 白名单合入去掉；
- ③：``document_delivery.py`` 跳过硬上限校验（段数/总字符）；
- ④：``document_delivery.py`` 跳过 ``constrain_output`` 调用；
- ⑤：``hooks.py`` 的 ``_is_side_effecting_tool`` 还原成"一切 mcp__ 都无副作用"。

真实 SDK 触发 ``PreToolUse``/挂载 MCP 服务这件事本身不在本文件验证范围内——那
只有 ``biai-stage`` 的 L4a 能回答，与本仓库既有的假 SDK 装配测试（见
``test_worker_entry.py`` 模块文档）同一条边界声明。
"""

from __future__ import annotations

import asyncio
import io
import json
import unittest

from lingxi.apps.worker.config import WorkerConfigError, load_config
from lingxi.apps.worker.turn import WorkerTurnExecutor
from lingxi.core.execution.document_delivery import (
    DELIVER_DOCUMENT_TOOL_NAME,
    DELIVERY_MCP_SERVER_NAME,
    DocumentDeliveryError,
    MAX_PARAGRAPHS,
    MAX_TITLE_CHARS,
    MAX_TOTAL_CHARS,
    build_document_request,
    normalize_markdown,
)
from lingxi.core.execution.hooks import ToolGateway
from lingxi.core.execution.tool_policy import DENY_REASON_TEMPLATE, DenyReasonCode

READ_ONLY_TOOL = "mcp__query__list_metrics"


def _env(**overrides: str | None) -> dict[str, str]:
    env = {
        "LINGXI_WORKER_QUESTION": "帮我写一份上周数据周报",
        "LINGXI_WORKER_READONLY_TOOLS": READ_ONLY_TOOL,
        "LINGXI_WORKER_TRACE_ID": "01J0000000000000000TEST000",
    }
    env.update({key: value for key, value in overrides.items() if value is not None})
    return env


def _stderr_events(buffer: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


# ----------------------------------------------------------- 纯逻辑：归一化


class NormalizeMarkdownTest(unittest.TestCase):
    def test_strips_markdown_syntax_and_splits_on_blank_lines(self) -> None:
        markdown = "# 标题\n\n- 第一条\n- 第二条\n\n**加粗内容** 和 `代码`。"

        paragraphs = normalize_markdown(markdown)

        self.assertEqual(paragraphs, ("标题", "第一条 第二条", "加粗内容 和 代码。"))

    def test_blank_or_pure_syntax_input_normalizes_to_no_paragraphs(self) -> None:
        self.assertEqual(normalize_markdown("   \n\n   \n"), ())
        self.assertEqual(normalize_markdown("# \n\n---\n\n***"), ())


# ----------------------------------------------------------- 纯逻辑：build_document_request


class BuildDocumentRequestTest(unittest.TestCase):
    def test_valid_request_round_trips_title_and_paragraphs(self) -> None:
        request = build_document_request(title="周报", markdown="第一段。\n\n第二段。")

        self.assertEqual(request.title, "周报")
        self.assertEqual(request.paragraphs, ("第一段。", "第二段。"))
        self.assertEqual(request.total_chars, len("第一段。") + len("第二段。"))

    def test_title_out_of_bounds_is_rejected(self) -> None:
        for bad_title in ("", "   ", "x" * (MAX_TITLE_CHARS + 1)):
            with self.subTest(title=bad_title):
                with self.assertRaises(DocumentDeliveryError) as ctx:
                    build_document_request(title=bad_title, markdown="正文")
                self.assertEqual(ctx.exception.reason_code, "invalid_title")

    def test_non_string_inputs_are_rejected_not_coerced(self) -> None:
        with self.assertRaises(DocumentDeliveryError):
            build_document_request(title=None, markdown="正文")
        with self.assertRaises(DocumentDeliveryError):
            build_document_request(title="标题", markdown=None)

    def test_empty_markdown_is_rejected(self) -> None:
        with self.assertRaises(DocumentDeliveryError) as ctx:
            build_document_request(title="标题", markdown="   ")
        self.assertEqual(ctx.exception.reason_code, "empty_markdown")

    def test_markdown_that_normalizes_to_nothing_is_rejected(self) -> None:
        with self.assertRaises(DocumentDeliveryError) as ctx:
            build_document_request(title="标题", markdown="# \n\n---\n\n***")
        self.assertEqual(ctx.exception.reason_code, "empty_markdown")

    def test_too_many_paragraphs_is_rejected_without_silent_truncation(self) -> None:
        """变异锚点③（上半）：跳过硬上限校验会让这条用例变绿（不再拒绝）。"""

        markdown = "\n\n".join(f"第{i}段内容" for i in range(MAX_PARAGRAPHS + 1))

        with self.assertRaises(DocumentDeliveryError) as ctx:
            build_document_request(title="标题", markdown=markdown)

        self.assertEqual(ctx.exception.reason_code, "too_many_paragraphs")

    def test_total_chars_over_limit_is_rejected_without_silent_truncation(self) -> None:
        """变异锚点③（下半）：段数不超但总字符超限同样必须拒绝，不静默截断。"""

        markdown = "\n\n".join(["A" * 5000] * 5)  # 5 段、总长 25000 > 上限 20000

        with self.assertRaises(DocumentDeliveryError) as ctx:
            build_document_request(title="标题", markdown=markdown)

        self.assertEqual(ctx.exception.reason_code, "too_many_chars")
        self.assertLessEqual(MAX_PARAGRAPHS, 80)
        self.assertLessEqual(MAX_TOTAL_CHARS, 20000)

    def test_body_leaking_an_internal_tool_name_pattern_is_rejected(self) -> None:
        """变异锚点④：跳过 constrain_output 会让这条用例变绿（不再拒绝）。"""

        with self.assertRaises(DocumentDeliveryError) as ctx:
            build_document_request(
                title="标题",
                markdown="正文中不慎写出了内部工具名 mcp__query__list_metrics，应当被拒绝。",
            )

        self.assertEqual(ctx.exception.reason_code, "leak_detected")

    def test_body_leaking_a_configured_forbidden_value_is_rejected(self) -> None:
        """与终态文本同等的出口安全检查：forbidden_values 确实被传导到这里，
        不是只有内置的 mcp__ 通用模式检测在起作用。"""

        with self.assertRaises(DocumentDeliveryError) as ctx:
            build_document_request(
                title="标题",
                markdown="正文提到了绝密业务指标口径ABC，不应该出现在交付文档里。",
                forbidden_values=("绝密业务指标口径ABC",),
            )

        self.assertEqual(ctx.exception.reason_code, "leak_detected")


# ----------------------------------------------------------- 开关解析


class DocumentDeliveryConfigFlagTest(unittest.TestCase):
    def test_default_is_disabled(self) -> None:
        config = load_config(_env())
        self.assertFalse(config.document_delivery_enabled)

    def test_exact_value_one_enables(self) -> None:
        config = load_config(_env(LINGXI_WORKER_DOCUMENT_DELIVERY_ENABLED="1"))
        self.assertTrue(config.document_delivery_enabled)

    def test_any_other_value_fails_closed(self) -> None:
        for bad_value in ("true", "0", "yes", "TRUE", "01"):
            with self.subTest(value=bad_value):
                with self.assertRaises(WorkerConfigError):
                    load_config(_env(LINGXI_WORKER_DOCUMENT_DELIVERY_ENABLED=bad_value))

    def test_delivery_tool_name_is_never_stuffed_into_read_only_tools(self) -> None:
        """名义纪律：即使开关开启，config.read_only_tools 也不含这个工具名——
        那个字段的装配期校验只认 mcp__query__ 前缀。"""

        config = load_config(_env(LINGXI_WORKER_DOCUMENT_DELIVERY_ENABLED="1"))
        self.assertNotIn(DELIVER_DOCUMENT_TOOL_NAME, config.read_only_tools)


# ----------------------------------------------------------- 装配：ToolPolicy 合入与拒绝姿态


class ToolPolicyMergeTest(unittest.TestCase):
    def test_enabled_merges_the_literal_into_the_policy_whitelist(self) -> None:
        """变异锚点①：不合入白名单会让这条用例变绿。"""

        executor = WorkerTurnExecutor(load_config(_env(LINGXI_WORKER_DOCUMENT_DELIVERY_ENABLED="1")))

        self.assertIn(DELIVER_DOCUMENT_TOOL_NAME, executor.policy.allowed_tools)
        self.assertIn(READ_ONLY_TOOL, executor.policy.allowed_tools, "合入不能挤掉既有只读工具")

    def test_disabled_keeps_the_tool_out_of_the_policy_whitelist(self) -> None:
        executor = WorkerTurnExecutor(load_config(_env()))

        self.assertNotIn(DELIVER_DOCUMENT_TOOL_NAME, executor.policy.allowed_tools)

    def test_disabled_denies_the_tool_with_the_existing_wording_posture(self) -> None:
        """②：开关关时，若这个工具名仍被调用，走的是与任何越界工具完全相同的
        既有拒绝分支——不是一条专门为它新写的拒绝路径。"""

        executor = WorkerTurnExecutor(load_config(_env()))

        response = asyncio.run(
            executor.gateway.on_hook_event(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": DELIVER_DOCUMENT_TOOL_NAME,
                    "tool_input": {"title": "x", "markdown": "y"},
                    "tool_use_id": "toolu_delivery_denied",
                }
            )
        )

        decision = response.get("hookSpecificOutput", {})
        self.assertEqual(decision.get("permissionDecision"), "deny")
        self.assertEqual(decision.get("permissionDecisionReason"), DENY_REASON_TEMPLATE)
        denied = executor.gateway.audit.summary().denied_calls
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0].tool_name, DELIVER_DOCUMENT_TOOL_NAME)
        self.assertIs(denied[0].deny_reason_code, DenyReasonCode.NOT_IN_WHITELIST)


# ----------------------------------------------------------- 装配：真实 SDK MCP 服务挂载


class DeliveryMcpServerMountTest(unittest.TestCase):
    """用真实 ``claude_agent_sdk``（本仓库 worker extras 的真实依赖）验证挂载
    形状；不涉及网络或模型额度，``create_sdk_mcp_server`` 只是纯 Python 对象
    装配。"""

    def test_enabled_mounts_the_server_and_widens_sdk_level_allowed_tools(self) -> None:
        executor = WorkerTurnExecutor(load_config(_env(LINGXI_WORKER_DOCUMENT_DELIVERY_ENABLED="1")))

        options = executor.build_session_options()

        self.assertIn(DELIVERY_MCP_SERVER_NAME, options.mcp_servers)
        self.assertIn(DELIVER_DOCUMENT_TOOL_NAME, options.allowed_tools)
        self.assertIn(READ_ONLY_TOOL, options.allowed_tools)

    def test_disabled_never_builds_the_server(self) -> None:
        executor = WorkerTurnExecutor(load_config(_env()))

        options = executor.build_session_options()

        self.assertNotIn(DELIVERY_MCP_SERVER_NAME, options.mcp_servers or {})


# ----------------------------------------------------------- 处理函数：登记 / 拒绝 / 覆盖


class DeliverDocumentHandlerTest(unittest.TestCase):
    def test_handler_rejects_defensively_when_switch_is_off(self) -> None:
        """开关关时这个处理函数原则上不可达（服务器根本不会被挂载）；这里直接
        调用它验证纵深防御分支本身没有被悄悄拆掉。"""

        buffer = io.StringIO()
        executor = WorkerTurnExecutor(load_config(_env()), stderr_stream=buffer)

        result = executor._handle_deliver_document("标题", "正文")

        self.assertTrue(result.get("is_error"))
        self.assertIsNone(executor._document_request)
        events = _stderr_events(buffer)
        self.assertEqual(events[-1]["event"], "worker.document_request_rejected")
        self.assertEqual(events[-1]["reason"], "disabled")

    def test_handler_registers_a_valid_request_and_audits_it(self) -> None:
        buffer = io.StringIO()
        executor = WorkerTurnExecutor(
            load_config(_env(LINGXI_WORKER_DOCUMENT_DELIVERY_ENABLED="1")), stderr_stream=buffer
        )

        result = executor._handle_deliver_document("周报", "第一段。\n\n第二段。")

        self.assertNotIn("is_error", result)
        self.assertIsNotNone(executor._document_request)
        self.assertEqual(executor._document_request.title, "周报")
        self.assertEqual(executor._document_request.paragraphs, ("第一段。", "第二段。"))
        events = _stderr_events(buffer)
        self.assertEqual(events[-1]["event"], "worker.document_request_registered")
        self.assertEqual(events[-1]["paragraph_count"], 2)
        self.assertNotIn("title", events[-1])
        self.assertNotIn("markdown", events[-1])

    def test_handler_rejects_oversize_request_without_silent_truncation(self) -> None:
        buffer = io.StringIO()
        executor = WorkerTurnExecutor(
            load_config(_env(LINGXI_WORKER_DOCUMENT_DELIVERY_ENABLED="1")), stderr_stream=buffer
        )
        oversized_markdown = "\n\n".join(f"第{i}段内容" for i in range(MAX_PARAGRAPHS + 1))

        result = executor._handle_deliver_document("标题", oversized_markdown)

        self.assertTrue(result.get("is_error"))
        self.assertIsNone(executor._document_request)
        events = _stderr_events(buffer)
        self.assertEqual(events[-1]["event"], "worker.document_request_rejected")
        self.assertEqual(events[-1]["reason"], "too_many_paragraphs")

    def test_handler_rejects_when_body_leaks_internal_tool_name_pattern(self) -> None:
        buffer = io.StringIO()
        executor = WorkerTurnExecutor(
            load_config(_env(LINGXI_WORKER_DOCUMENT_DELIVERY_ENABLED="1")), stderr_stream=buffer
        )

        result = executor._handle_deliver_document(
            "标题", "正文中不慎写出了内部工具名 mcp__query__list_metrics，应当被拒绝。"
        )

        self.assertTrue(result.get("is_error"))
        self.assertIsNone(executor._document_request)
        events = _stderr_events(buffer)
        self.assertEqual(events[-1]["event"], "worker.document_request_rejected")
        self.assertEqual(events[-1]["reason"], "leak_detected")

    def test_second_call_within_the_same_turn_replaces_the_first_and_is_audited(self) -> None:
        buffer = io.StringIO()
        executor = WorkerTurnExecutor(
            load_config(_env(LINGXI_WORKER_DOCUMENT_DELIVERY_ENABLED="1")), stderr_stream=buffer
        )

        executor._handle_deliver_document("第一版标题", "第一版正文")
        result = executor._handle_deliver_document("第二版标题", "第二版正文")

        self.assertNotIn("is_error", result)
        self.assertEqual(executor._document_request.title, "第二版标题")
        events = _stderr_events(buffer)
        event_names = [event["event"] for event in events]
        self.assertEqual(event_names.count("worker.document_request_registered"), 2)
        self.assertIn("worker.document_request_replaced", event_names)


# ----------------------------------------------------------- 端到端：run_turn() 报告契约


class RunTurnReportContractTest(unittest.TestCase):
    """只把驱动模型输出的 ``run_single_turn`` 换成脚本化假实现——``build_session_
    options()`` 仍然照常调用真实 ``claude_agent_sdk``，只是不真的建立传输连接。"""

    def _run_with_delivery_call(
        self, *, title: str = "周报", markdown: str = "第一段。\n\n第二段。", fail: bool = False
    ):
        from unittest.mock import patch

        executor = WorkerTurnExecutor(load_config(_env(LINGXI_WORKER_DOCUMENT_DELIVERY_ENABLED="1")))

        async def fake_run_single_turn(
            *,
            options,
            prompt,
            sink,
            timeout_seconds,
            resume_session_id=None,
            stop_event=None,
            drain_grace_seconds=30.0,
            clock=None,
            on_business_duration=None,
            on_interrupt_requested=None,
        ):
            del options, prompt, timeout_seconds, resume_session_id
            del stop_event, drain_grace_seconds, clock, on_interrupt_requested
            executor._handle_deliver_document(title, markdown)
            if fail:
                raise TimeoutError("simulated turn timeout")
            sink({"kind": "assistant_message", "text": "文档请求已经登记好了。"})
            sink({"kind": "result", "is_error": False, "subtype": "success"})
            await executor.gateway.on_hook_event({"hook_event_name": "Stop"})
            if on_business_duration is not None:
                on_business_duration(0.01)

        with patch("lingxi.apps.worker.turn.run_single_turn", fake_run_single_turn):
            report = asyncio.run(executor.run_turn("请帮我写一份周报"))
        return report, executor

    def test_successful_turn_carries_the_document_request_in_the_report(self) -> None:
        """①：报告带 document_request，字段形状是 title + paragraphs。"""

        report, _executor = self._run_with_delivery_call()

        self.assertTrue(report["turn"]["closed"])
        self.assertIsNone(report["failure"])
        self.assertEqual(
            report["document_request"],
            {"title": "周报", "paragraphs": ["第一段。", "第二段。"]},
        )

    def test_failed_turn_does_not_carry_the_document_request(self) -> None:
        """"仅当任务成功"：工具调用发生在真实失败之前，报告仍不得带上它。"""

        report, _executor = self._run_with_delivery_call(fail=True)

        self.assertIsNotNone(report["failure"])
        self.assertIsNone(report["document_request"])

    def test_next_turn_on_the_same_executor_resets_the_pending_request(self) -> None:
        """回合级状态：第二回合模型没有再调用这个工具时，不带上第一回合遗留的
        请求。"""

        from unittest.mock import patch

        executor = WorkerTurnExecutor(load_config(_env(LINGXI_WORKER_DOCUMENT_DELIVERY_ENABLED="1")))

        async def deliver_then_finish(*, options, prompt, sink, timeout_seconds, **_kwargs):
            del options, prompt, timeout_seconds
            executor._handle_deliver_document("周报", "第一段。")
            sink({"kind": "assistant_message", "text": "已登记"})
            sink({"kind": "result", "is_error": False, "subtype": "success"})
            await executor.gateway.on_hook_event({"hook_event_name": "Stop"})

        async def finish_without_delivery(*, options, prompt, sink, timeout_seconds, **_kwargs):
            del options, prompt, timeout_seconds
            sink({"kind": "assistant_message", "text": "这次没有生成文档。"})
            sink({"kind": "result", "is_error": False, "subtype": "success"})
            await executor.gateway.on_hook_event({"hook_event_name": "Stop"})

        with patch("lingxi.apps.worker.turn.run_single_turn", deliver_then_finish):
            first_report = asyncio.run(executor.run_turn("请帮我写一份周报"))
        with patch("lingxi.apps.worker.turn.run_single_turn", finish_without_delivery):
            second_report = asyncio.run(executor.run_turn("再问个别的问题"))

        self.assertIsNotNone(first_report["document_request"])
        self.assertIsNone(second_report["document_request"])


# ----------------------------------------------------------- 侧效判定修正


class SideEffectClassificationTest(unittest.TestCase):
    """变异锚点⑤：把 ``_is_side_effecting_tool`` 还原成"一切 mcp__ 都无副作用"
    会让这条用例变红。"""

    def test_deliver_document_tool_is_classified_as_side_effecting(self) -> None:
        self.assertTrue(ToolGateway._is_side_effecting_tool(DELIVER_DOCUMENT_TOOL_NAME))

    def test_read_only_query_tools_remain_non_side_effecting(self) -> None:
        self.assertFalse(ToolGateway._is_side_effecting_tool("mcp__query__list_metrics"))
        self.assertFalse(ToolGateway._is_side_effecting_tool("mcp__query__describe_metric"))


# ----------------------------------------------------------- 哨兵：关闭时普通问数逐字节不变


class SentinelNormalQaUnaffectedTest(unittest.TestCase):
    """②后半句：开关默认关闭时，把既有假 SDK 装配路径（见 test_worker_entry.py）
    原样跑一遍普通问数，只新增对 document_request 字段的断言——证明这次改动
    没有触碰既有行为。"""

    def test_normal_turn_without_the_flag_reports_no_document_request(self) -> None:
        from test_worker_entry import ok_result, run_turn

        report, _fake, executor = run_turn(
            self,
            [
                {
                    "kind": "tool",
                    "tool": READ_ONLY_TOOL,
                    "input": {"metric": "dau"},
                    "result": ok_result(),
                },
                {"kind": "text", "text": "近 7 天活跃用户数是 1024。"},
            ],
        )

        self.assertIsNone(report.get("document_request"))
        self.assertTrue(report["turn"]["closed"])
        self.assertEqual(report["turn"]["final_text"], "近 7 天活跃用户数是 1024。")
        self.assertNotIn(DELIVER_DOCUMENT_TOOL_NAME, executor.policy.allowed_tools)


if __name__ == "__main__":
    unittest.main()
