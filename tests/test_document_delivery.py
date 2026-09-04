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
- ⑤：``hooks.py`` 的 ``_is_side_effecting_tool`` 把 ``deliver_document`` 改回显式
  列为侧效例外（P2-1 opus 审查撤销了这条例外，见 ``SideEffectClassificationTest``）。

真实 SDK 触发 ``PreToolUse``/挂载 MCP 服务这件事本身不在本文件验证范围内——那
只有 ``biai-stage`` 的 L4a 能回答，与本仓库既有的假 SDK 装配测试（见
``test_worker_entry.py`` 模块文档）同一条边界声明。
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import unittest

from lingxi.apps.worker.config import WorkerConfigError, load_config
from lingxi.apps.worker.turn import WorkerTurnExecutor
from lingxi.core.execution.document_delivery import (
    DELIVER_DOCUMENT_TOOL_NAME,
    DELIVER_SPREADSHEET_TOOL_NAME,
    DELIVERY_MCP_SERVER_NAME,
    MAX_PARAGRAPHS,
    MAX_RAW_MARKDOWN_CHARS,
    MAX_SHEET_COLUMNS,
    MAX_SHEET_ROWS,
    MAX_SHEET_TOTAL_CHARS,
    MAX_TITLE_CHARS,
    MAX_TOTAL_CHARS,
    DocumentDeliveryError,
    build_document_request,
    build_sheet_request,
    normalize_markdown,
)
from lingxi.core.execution.hooks import ToolGateway
from lingxi.core.execution.tool_policy import DENY_REASON_TEMPLATE, DenyReasonCode

READ_ONLY_TOOL = "mcp__query__list_metrics"


def _claude_agent_sdk_available() -> bool:
    return importlib.util.find_spec("claude_agent_sdk") is not None


# Issue #370 修 1：本文件模块文档已声明"刻意不打桩、直接走真实 claude_agent_sdk"
# 是设计选择（比自造桩更贴近真实失败面），但此前没有对应的 skipUnless 门控——
# 未装 worker extras（`claude_agent_sdk`）的机器上，下面两个类会在
# `build_session_options()` 内部 `ModuleNotFoundError`，表现成 2 ERROR + 2 FAIL
# 而不是清晰的 skip，与代码框架 §四"全量套件须可在无外部依赖机器上运行"的承诺
# 冲突。风格与仓库既有真库门控一致：`importlib.util.find_spec` 探测 + 明确原因。
CLAUDE_AGENT_SDK_SKIP_REASON = (
    "跳过：未安装 claude_agent_sdk（worker extras），真实 SDK 装配形状未验证"
)


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
    def test_splits_on_blank_lines_and_collapses_intra_paragraph_newlines(self) -> None:
        """Issue #408：不再剥离 Markdown 语法字符，`#`/`-`/`*`/反引号原样保留在
        段落里——只做空行切段、段内折叠。"""

        markdown = "# 标题\n\n- 第一条\n- 第二条\n\n**加粗内容** 和 `代码`。"

        paragraphs = normalize_markdown(markdown)

        self.assertEqual(
            paragraphs, ("# 标题", "- 第一条 - 第二条", "**加粗内容** 和 `代码`。")
        )

    def test_blank_input_normalizes_to_no_paragraphs(self) -> None:
        self.assertEqual(normalize_markdown("   \n\n   \n"), ())

    def test_negative_numbers_and_percentage_ranges_are_preserved_verbatim(self) -> None:
        """Issue #408 数据正确性修复的核心断言：此前的字符剥离会把连字符一并
        吃掉——「周环比 -12.85%」变成「周环比 12.85%」（负号丢失）、「3-5%」
        变成「35%」（区间被拼接）。这里必须逐字保真。"""

        markdown = "周环比 -12.85%\n\n销售额同比增长 3-5%"

        paragraphs = normalize_markdown(markdown)

        self.assertEqual(paragraphs, ("周环比 -12.85%", "销售额同比增长 3-5%"))


# ----------------------------------------------------------- 纯逻辑：build_document_request


class BuildDocumentRequestTest(unittest.TestCase):
    def test_valid_request_round_trips_title_and_paragraphs(self) -> None:
        request = build_document_request(title="周报", markdown="第一段。\n\n第二段。")

        self.assertEqual(request.title, "周报")
        self.assertEqual(request.paragraphs, ("第一段。", "第二段。"))
        self.assertEqual(request.total_chars, len("第一段。") + len("第二段。"))
        # Issue #408 正式方案接线：原始 markdown 全文原样保留（不是从 paragraphs
        # 拼回去的近似值），供 gateway 官方转换路径消费。
        self.assertEqual(request.markdown, "第一段。\n\n第二段。")

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

    def test_markdown_with_only_markdown_syntax_characters_is_accepted_verbatim(self) -> None:
        """Issue #408：不再剥离语法字符，`# \n\n---\n\n***` 每一块都还剩下非空
        白字符，因此不再被判定为"归一化后无可用段落"——与
        ``NormalizeMarkdownTest`` 的新行为对称。"""

        request = build_document_request(title="标题", markdown="# \n\n---\n\n***")

        self.assertEqual(request.paragraphs, ("#", "---", "***"))

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

    def test_raw_markdown_over_limit_is_rejected_even_when_normalized_paragraphs_are_tiny(
        self,
    ) -> None:
        """P2 顺手（独立审查）：只校验归一化后的 ``total_chars`` 挡不住一种
        绕过——``normalize_markdown`` 会把纯空白/纯空行的"段"折叠成空字符串
        直接丢弃（``if collapsed:`` 判据），因此可以塞进海量这样的段落，
        原始 ``markdown`` 长度不受控地膨胀，但归一化后 ``paragraphs``/
        ``total_chars`` 仍然很小、逃过既有的 ``too_many_chars`` 检查——而
        ``DocumentRequest.markdown`` 存的是**原始**入参，会逐字持久化进迁移
        0079 的 ``markdown`` 列。这里构造这种"归一化后几乎为零、原始却超限"
        的输入，必须被独立的原始长度上限拒绝。

        变异锚点：删掉 ``MAX_RAW_MARKDOWN_CHARS`` 校验，本用例会从抛
        ``DocumentDeliveryError`` 变红成正常构造出一个 ``DocumentRequest``。
        """

        # 一段真实内容 + 大量"纯空白行组成的空段落"（每个空段落归一化后长度为
        # 0，不计入 total_chars），让原始字符串远超上限、但归一化后段落极少。
        blank_blocks = "\n\n".join("   " for _ in range(MAX_RAW_MARKDOWN_CHARS))
        markdown = f"真实内容\n\n{blank_blocks}"
        self.assertGreater(len(markdown), MAX_RAW_MARKDOWN_CHARS)

        with self.assertRaises(DocumentDeliveryError) as ctx:
            build_document_request(title="标题", markdown=markdown)

        self.assertEqual(ctx.exception.reason_code, "markdown_too_long")

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


# ----------------------------------------------------------- 纯逻辑：build_sheet_request（Issue #354 S-H3-2）


class BuildSheetRequestTest(unittest.TestCase):
    """与 ``BuildDocumentRequestTest`` 逐项对称——见
    ``core/execution/document_delivery.py`` 模块文档「表格分支」一节。"""

    def test_valid_request_round_trips_title_and_rows(self) -> None:
        request = build_sheet_request(title="销售汇总", rows=[["月份", "销售额"], ["1月", "100"]])

        self.assertEqual(request.title, "销售汇总")
        self.assertEqual(request.rows, (("月份", "销售额"), ("1月", "100")))
        self.assertEqual(request.total_chars, sum(len(cell) for row in request.rows for cell in row))

    def test_title_out_of_bounds_is_rejected(self) -> None:
        for bad_title in ("", "   ", "x" * (MAX_TITLE_CHARS + 1)):
            with self.subTest(title=bad_title):
                with self.assertRaises(DocumentDeliveryError) as ctx:
                    build_sheet_request(title=bad_title, rows=[["a"]])
                self.assertEqual(ctx.exception.reason_code, "invalid_title")

    def test_non_string_title_is_rejected_not_coerced(self) -> None:
        with self.assertRaises(DocumentDeliveryError):
            build_sheet_request(title=None, rows=[["a"]])

    def test_empty_rows_is_rejected(self) -> None:
        with self.assertRaises(DocumentDeliveryError) as ctx:
            build_sheet_request(title="标题", rows=[])
        self.assertEqual(ctx.exception.reason_code, "empty_rows")

    def test_rows_that_is_not_a_list_is_rejected(self) -> None:
        with self.assertRaises(DocumentDeliveryError) as ctx:
            build_sheet_request(title="标题", rows="不是列表")
        self.assertEqual(ctx.exception.reason_code, "empty_rows")

    def test_an_empty_row_is_rejected(self) -> None:
        with self.assertRaises(DocumentDeliveryError) as ctx:
            build_sheet_request(title="标题", rows=[["a"], []])
        self.assertEqual(ctx.exception.reason_code, "invalid_row")

    def test_a_row_that_is_not_a_list_is_rejected(self) -> None:
        with self.assertRaises(DocumentDeliveryError) as ctx:
            build_sheet_request(title="标题", rows=[["a"], "b"])
        self.assertEqual(ctx.exception.reason_code, "invalid_row")

    def test_a_non_string_cell_is_rejected_not_coerced(self) -> None:
        with self.assertRaises(DocumentDeliveryError) as ctx:
            build_sheet_request(title="标题", rows=[["a", 1]])
        self.assertEqual(ctx.exception.reason_code, "invalid_cell")

    def test_too_many_rows_is_rejected_without_silent_truncation(self) -> None:
        """变异锚点（对称①）：跳过硬上限校验会让这条用例变绿（不再拒绝）。"""

        rows = [[f"第{i}行"] for i in range(MAX_SHEET_ROWS + 1)]

        with self.assertRaises(DocumentDeliveryError) as ctx:
            build_sheet_request(title="标题", rows=rows)

        self.assertEqual(ctx.exception.reason_code, "too_many_rows")

    def test_too_many_columns_is_rejected_without_silent_truncation(self) -> None:
        row = [f"c{i}" for i in range(MAX_SHEET_COLUMNS + 1)]

        with self.assertRaises(DocumentDeliveryError) as ctx:
            build_sheet_request(title="标题", rows=[row])

        self.assertEqual(ctx.exception.reason_code, "too_many_columns")

    def test_total_chars_over_limit_is_rejected_without_silent_truncation(self) -> None:
        rows = [["A" * 5000] for _ in range(5)]  # 5 格、总长 25000 > 上限 20000

        with self.assertRaises(DocumentDeliveryError) as ctx:
            build_sheet_request(title="标题", rows=rows)

        self.assertEqual(ctx.exception.reason_code, "too_many_chars")
        self.assertLessEqual(MAX_SHEET_TOTAL_CHARS, 20000)

    def test_body_leaking_an_internal_tool_name_pattern_is_rejected(self) -> None:
        """变异锚点（对称④）：跳过 constrain_output 会让这条用例变绿。"""

        with self.assertRaises(DocumentDeliveryError) as ctx:
            build_sheet_request(title="标题", rows=[["mcp__query__list_metrics"]])

        self.assertEqual(ctx.exception.reason_code, "leak_detected")

    def test_body_leaking_a_configured_forbidden_value_is_rejected(self) -> None:
        with self.assertRaises(DocumentDeliveryError) as ctx:
            build_sheet_request(
                title="标题",
                rows=[["绝密业务指标口径ABC"]],
                forbidden_values=("绝密业务指标口径ABC",),
            )

        self.assertEqual(ctx.exception.reason_code, "leak_detected")

    def test_short_rows_are_padded_to_the_longest_row_with_empty_strings(self) -> None:
        """P1（Trace #373 H3 批量审查）：短行补齐空字符串到最长行的列数，返回
        的 ``rows`` 始终是矩形——飞书对不规则 ``range`` 输入的真实语义未经验证，
        补齐比原样透传更保守。

        变异锚点：把补齐逻辑改坏（例如漏掉补齐、或补的不是空字符串），本用例
        会从矩形结果变红。
        """

        request = build_sheet_request(title="标题", rows=[["a", "b", "c"], ["d"]])

        self.assertEqual(request.rows, (("a", "b", "c"), ("d", "", "")))
        # 补齐只加空字符串，不改变总字符数。
        self.assertEqual(request.total_chars, 4)

    def test_all_rows_already_the_same_length_are_unaffected_by_padding(self) -> None:
        request = build_sheet_request(title="标题", rows=[["a", "b"], ["c", "d"]])

        self.assertEqual(request.rows, (("a", "b"), ("c", "d")))


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
        self.assertNotIn(DELIVER_SPREADSHEET_TOOL_NAME, config.read_only_tools)


# ----------------------------------------------------------- 装配：ToolPolicy 合入与拒绝姿态


class ToolPolicyMergeTest(unittest.TestCase):
    def test_enabled_merges_the_literal_into_the_policy_whitelist(self) -> None:
        """变异锚点①：不合入白名单会让这条用例变绿。"""

        executor = WorkerTurnExecutor(load_config(_env(LINGXI_WORKER_DOCUMENT_DELIVERY_ENABLED="1")))

        self.assertIn(DELIVER_DOCUMENT_TOOL_NAME, executor.policy.allowed_tools)
        self.assertIn(READ_ONLY_TOOL, executor.policy.allowed_tools, "合入不能挤掉既有只读工具")

    def test_enabled_merges_the_spreadsheet_tool_name_too(self) -> None:
        """表格分支（Issue #354 S-H3-2）：同一个开关新增并列工具，不是单独开关。

        变异锚点：不合入白名单会让这条用例变绿。
        """

        executor = WorkerTurnExecutor(load_config(_env(LINGXI_WORKER_DOCUMENT_DELIVERY_ENABLED="1")))

        self.assertIn(DELIVER_SPREADSHEET_TOOL_NAME, executor.policy.allowed_tools)
        self.assertIn(DELIVER_DOCUMENT_TOOL_NAME, executor.policy.allowed_tools)

    def test_disabled_keeps_the_tool_out_of_the_policy_whitelist(self) -> None:
        executor = WorkerTurnExecutor(load_config(_env()))

        self.assertNotIn(DELIVER_DOCUMENT_TOOL_NAME, executor.policy.allowed_tools)
        self.assertNotIn(DELIVER_SPREADSHEET_TOOL_NAME, executor.policy.allowed_tools)

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

    def test_disabled_denies_the_spreadsheet_tool_with_the_existing_wording_posture(self) -> None:
        executor = WorkerTurnExecutor(load_config(_env()))

        response = asyncio.run(
            executor.gateway.on_hook_event(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": DELIVER_SPREADSHEET_TOOL_NAME,
                    "tool_input": {"title": "x", "rows": [["y"]]},
                    "tool_use_id": "toolu_sheet_denied",
                }
            )
        )

        decision = response.get("hookSpecificOutput", {})
        self.assertEqual(decision.get("permissionDecision"), "deny")
        denied = executor.gateway.audit.summary().denied_calls
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0].tool_name, DELIVER_SPREADSHEET_TOOL_NAME)
        self.assertIs(denied[0].deny_reason_code, DenyReasonCode.NOT_IN_WHITELIST)


# ----------------------------------------------------------- 装配：真实 SDK MCP 服务挂载


@unittest.skipUnless(_claude_agent_sdk_available(), CLAUDE_AGENT_SDK_SKIP_REASON)
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

    def test_enabled_mounts_the_spreadsheet_tool_on_the_same_server(self) -> None:
        """表格分支（Issue #354 S-H3-2）：同一个 MCP 服务下的并列工具，不是
        另开一个服务——见 core/execution/document_delivery.py 模块文档
        「表格分支」一节。"""

        executor = WorkerTurnExecutor(load_config(_env(LINGXI_WORKER_DOCUMENT_DELIVERY_ENABLED="1")))

        options = executor.build_session_options()

        self.assertIn(DELIVERY_MCP_SERVER_NAME, options.mcp_servers)
        self.assertIn(DELIVER_SPREADSHEET_TOOL_NAME, options.allowed_tools)

    def test_disabled_never_builds_the_server(self) -> None:
        executor = WorkerTurnExecutor(load_config(_env()))

        options = executor.build_session_options()

        self.assertNotIn(DELIVERY_MCP_SERVER_NAME, options.mcp_servers or {})
        self.assertNotIn(DELIVER_SPREADSHEET_TOOL_NAME, options.allowed_tools)


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

    def test_handler_rejects_when_body_leaks_a_configured_external_text_via_real_assembly(
        self,
    ) -> None:
        """P1-1（opus 审查）：``config.external_texts`` 是 ``tuple[tuple[str, str],
        ...]``（`(键, 文本)` 对），把它整体（而不是拆出的文本值）传给
        ``constrain_output`` 会让每一条禁止值变成一个二元组对象，与真实正文逐字
        比对永远不命中——出口安全检查因此对文档交付**整串失效**，且这个错误只有
        通过真实装配路径（``load_config`` 解析 ``LINGXI_WORKER_METRIC_DESCRIPTION``
        → ``WorkerConfig.external_texts`` → ``_handle_deliver_document``）才能被
        测出来；直接调用 ``build_document_request(forbidden_values=(text,))`` 的
        既有用例（见 ``BuildDocumentRequestTest``）传的已经是拆好的文本，测不出
        这个类型不匹配。把 ``turn.py`` 里的 `tuple(text for _, text in ...)` 拆包
        改回直接传 `self._config.external_texts`，本用例会变绿（不再拒绝）。
        """

        buffer = io.StringIO()
        executor = WorkerTurnExecutor(
            load_config(
                _env(
                    LINGXI_WORKER_DOCUMENT_DELIVERY_ENABLED="1",
                    LINGXI_WORKER_METRIC_DESCRIPTION="内部专用指标口径说明，不得外传",
                )
            ),
            stderr_stream=buffer,
        )

        result = executor._handle_deliver_document(
            "标题", "正文里不慎抄了一遍：内部专用指标口径说明，不得外传"
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


# ----------------------------------------------------------- 处理函数：表格分支（Issue #354 S-H3-2）


class DeliverSpreadsheetHandlerTest(unittest.TestCase):
    """与 ``DeliverDocumentHandlerTest`` 逐项对称，外加两个跨类型互斥用例。"""

    def test_handler_rejects_defensively_when_switch_is_off(self) -> None:
        buffer = io.StringIO()
        executor = WorkerTurnExecutor(load_config(_env()), stderr_stream=buffer)

        result = executor._handle_deliver_spreadsheet("标题", [["a"]])

        self.assertTrue(result.get("is_error"))
        self.assertIsNone(executor._sheet_request)
        events = _stderr_events(buffer)
        # P2-8（opus 审查）：表格分支记独立的 worker.sheet_request_rejected，
        # 不复用文档分支的事件名——运维按事件名过滤时能区分来源。
        self.assertEqual(events[-1]["event"], "worker.sheet_request_rejected")
        self.assertEqual(events[-1]["reason"], "disabled")

    def test_handler_registers_a_valid_request_and_audits_it(self) -> None:
        buffer = io.StringIO()
        executor = WorkerTurnExecutor(
            load_config(_env(LINGXI_WORKER_DOCUMENT_DELIVERY_ENABLED="1")), stderr_stream=buffer
        )

        result = executor._handle_deliver_spreadsheet("销售汇总", [["月份", "销售额"], ["1月", "100"]])

        self.assertNotIn("is_error", result)
        self.assertIsNotNone(executor._sheet_request)
        self.assertEqual(executor._sheet_request.title, "销售汇总")
        self.assertEqual(executor._sheet_request.rows, (("月份", "销售额"), ("1月", "100")))
        events = _stderr_events(buffer)
        self.assertEqual(events[-1]["event"], "worker.document_request_registered")
        self.assertEqual(events[-1]["row_count"], 2)
        self.assertNotIn("title", events[-1])
        self.assertNotIn("rows", events[-1])

    def test_handler_rejects_oversize_request_without_silent_truncation(self) -> None:
        buffer = io.StringIO()
        executor = WorkerTurnExecutor(
            load_config(_env(LINGXI_WORKER_DOCUMENT_DELIVERY_ENABLED="1")), stderr_stream=buffer
        )
        oversized_rows = [[f"第{i}行"] for i in range(MAX_SHEET_ROWS + 1)]

        result = executor._handle_deliver_spreadsheet("标题", oversized_rows)

        self.assertTrue(result.get("is_error"))
        self.assertIsNone(executor._sheet_request)
        events = _stderr_events(buffer)
        self.assertEqual(events[-1]["event"], "worker.sheet_request_rejected")
        self.assertEqual(events[-1]["reason"], "too_many_rows")

    def test_handler_rejects_when_body_leaks_internal_tool_name_pattern(self) -> None:
        buffer = io.StringIO()
        executor = WorkerTurnExecutor(
            load_config(_env(LINGXI_WORKER_DOCUMENT_DELIVERY_ENABLED="1")), stderr_stream=buffer
        )

        result = executor._handle_deliver_spreadsheet("标题", [["mcp__query__list_metrics"]])

        self.assertTrue(result.get("is_error"))
        self.assertIsNone(executor._sheet_request)
        events = _stderr_events(buffer)
        self.assertEqual(events[-1]["event"], "worker.sheet_request_rejected")
        self.assertEqual(events[-1]["reason"], "leak_detected")

    def test_handler_rejects_when_body_leaks_a_configured_external_text_via_real_assembly(self) -> None:
        """同 ``DeliverDocumentHandlerTest`` 的 P1-1 用例：``config.external_
        texts`` 拆包必须在表格分支同样生效，不是只有文档分支修过。"""

        buffer = io.StringIO()
        executor = WorkerTurnExecutor(
            load_config(
                _env(
                    LINGXI_WORKER_DOCUMENT_DELIVERY_ENABLED="1",
                    LINGXI_WORKER_METRIC_DESCRIPTION="内部专用指标口径说明，不得外传",
                )
            ),
            stderr_stream=buffer,
        )

        result = executor._handle_deliver_spreadsheet("标题", [["内部专用指标口径说明，不得外传"]])

        self.assertTrue(result.get("is_error"))
        self.assertIsNone(executor._sheet_request)
        events = _stderr_events(buffer)
        self.assertEqual(events[-1]["event"], "worker.sheet_request_rejected")
        self.assertEqual(events[-1]["reason"], "leak_detected")

    def test_second_call_within_the_same_turn_replaces_the_first_and_is_audited(self) -> None:
        buffer = io.StringIO()
        executor = WorkerTurnExecutor(
            load_config(_env(LINGXI_WORKER_DOCUMENT_DELIVERY_ENABLED="1")), stderr_stream=buffer
        )

        executor._handle_deliver_spreadsheet("第一版标题", [["a"]])
        result = executor._handle_deliver_spreadsheet("第二版标题", [["b"]])

        self.assertNotIn("is_error", result)
        self.assertEqual(executor._sheet_request.title, "第二版标题")
        events = _stderr_events(buffer)
        event_names = [event["event"] for event in events]
        self.assertEqual(event_names.count("worker.document_request_registered"), 2)
        self.assertIn("worker.document_request_replaced", event_names)

    def test_calling_deliver_document_then_deliver_spreadsheet_leaves_only_the_sheet_slot_set(
        self,
    ) -> None:
        """跨类型互斥（Issue #354 S-H3-2）：这一回合先调用 deliver_document、
        再调用 deliver_spreadsheet——"最后一次调用为准"跨类型同样成立，文档
        槽位必须被清空，不能两个槽位同时非空（否则 write_terminal_event 会
        同时收到 document_request 与 sheet_request，触发它的互斥校验）。

        变异锚点：把 _handle_deliver_spreadsheet 里 ``self._document_request =
        None`` 这一行删掉，本用例会从"文档槽位为 None"变红成"文档槽位仍是第
        一次调用登记的那个请求"。
        """

        buffer = io.StringIO()
        executor = WorkerTurnExecutor(
            load_config(_env(LINGXI_WORKER_DOCUMENT_DELIVERY_ENABLED="1")), stderr_stream=buffer
        )

        executor._handle_deliver_document("文档标题", "文档正文")
        executor._handle_deliver_spreadsheet("表格标题", [["a"]])

        self.assertIsNone(executor._document_request)
        self.assertIsNotNone(executor._sheet_request)
        self.assertEqual(executor._sheet_request.title, "表格标题")

    def test_calling_deliver_spreadsheet_then_deliver_document_leaves_only_the_document_slot_set(
        self,
    ) -> None:
        """同上，反过来调用顺序。"""

        executor = WorkerTurnExecutor(load_config(_env(LINGXI_WORKER_DOCUMENT_DELIVERY_ENABLED="1")))

        executor._handle_deliver_spreadsheet("表格标题", [["a"]])
        executor._handle_deliver_document("文档标题", "文档正文")

        self.assertIsNone(executor._sheet_request)
        self.assertIsNotNone(executor._document_request)
        self.assertEqual(executor._document_request.title, "文档标题")


# ----------------------------------------------------------- 端到端：run_turn() 报告契约


@unittest.skipUnless(_claude_agent_sdk_available(), CLAUDE_AGENT_SDK_SKIP_REASON)
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
            on_mcp_status=None,
        ):
            del options, prompt, timeout_seconds, resume_session_id
            del stop_event, drain_grace_seconds, clock, on_interrupt_requested, on_mcp_status
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
        """①：报告带 document_request，字段形状是 title + paragraphs + markdown
        （Issue #408 正式方案接线新增 markdown，供 gateway 官方转换路径消费）。"""

        report, _executor = self._run_with_delivery_call()

        self.assertTrue(report["turn"]["closed"])
        self.assertIsNone(report["failure"])
        self.assertEqual(
            report["document_request"],
            {
                "title": "周报",
                "paragraphs": ["第一段。", "第二段。"],
                "markdown": "第一段。\n\n第二段。",
            },
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

    def _run_with_sheet_delivery_call(
        self, *, title: str = "销售汇总", rows=None, fail: bool = False
    ):
        from unittest.mock import patch

        rows = rows if rows is not None else [["月份", "销售额"], ["1月", "100"]]
        executor = WorkerTurnExecutor(load_config(_env(LINGXI_WORKER_DOCUMENT_DELIVERY_ENABLED="1")))

        async def fake_run_single_turn(*, options, prompt, sink, timeout_seconds, **_kwargs):
            del options, prompt, timeout_seconds
            executor._handle_deliver_spreadsheet(title, rows)
            if fail:
                raise TimeoutError("simulated turn timeout")
            sink({"kind": "assistant_message", "text": "表格请求已经登记好了。"})
            sink({"kind": "result", "is_error": False, "subtype": "success"})
            await executor.gateway.on_hook_event({"hook_event_name": "Stop"})

        with patch("lingxi.apps.worker.turn.run_single_turn", fake_run_single_turn):
            report = asyncio.run(executor.run_turn("请给我一张销售汇总表"))
        return report, executor

    def test_successful_turn_carries_the_sheet_request_in_the_report(self) -> None:
        """与 ``test_successful_turn_carries_the_document_request_in_the_report``
        对称：报告带 sheet_request，字段形状是 title + rows；document_request
        恒为 None（两个槽位互斥）。"""

        report, _executor = self._run_with_sheet_delivery_call()

        self.assertTrue(report["turn"]["closed"])
        self.assertIsNone(report["failure"])
        self.assertEqual(
            report["sheet_request"],
            {"title": "销售汇总", "rows": [["月份", "销售额"], ["1月", "100"]]},
        )
        self.assertIsNone(report["document_request"])

    def test_failed_turn_does_not_carry_the_sheet_request(self) -> None:
        report, _executor = self._run_with_sheet_delivery_call(fail=True)

        self.assertIsNotNone(report["failure"])
        self.assertIsNone(report["sheet_request"])


# ----------------------------------------------------------- 侧效判定修正


class SideEffectClassificationTest(unittest.TestCase):
    """P2-1（opus 审查，撤销原 S-ES-2 变异锚点⑤）：``deliver_document`` 只登记
    一次回合级内存状态，没有任何跨进程副作用——真正落库的一步（``write_terminal_
    event``）由 ``task_document_delivery_request.task_id`` 的 UNIQUE 约束保证
    幂等。把它标成"有副作用"的代价是崩溃恢复（``adapters/postgres_conversation/
    _queue_lifecycle.py::reclaim_stale``）会因为 ``side_effect_state='possible'``
    拒绝安全重试，让一个只是想要文档的用户平白收到失败终态。变异锚点：把
    ``_is_side_effecting_tool`` 改回显式把它列为例外（``return True``），本用例
    会变红。``deliver_spreadsheet`` 同一形状（``mcp__`` 前缀），不需要改
    ``_is_side_effecting_tool`` 本身——它对任何 ``mcp__`` 前缀工具都成立，
    见 ``core/execution/document_delivery.py`` 模块文档「表格分支」一节。"""

    def test_deliver_document_tool_is_not_classified_as_side_effecting(self) -> None:
        self.assertFalse(ToolGateway._is_side_effecting_tool(DELIVER_DOCUMENT_TOOL_NAME))

    def test_deliver_spreadsheet_tool_is_not_classified_as_side_effecting(self) -> None:
        self.assertFalse(ToolGateway._is_side_effecting_tool(DELIVER_SPREADSHEET_TOOL_NAME))

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
        self.assertIsNone(report.get("sheet_request"))
        self.assertTrue(report["turn"]["closed"])
        self.assertEqual(report["turn"]["final_text"], "近 7 天活跃用户数是 1024。")
        self.assertNotIn(DELIVER_DOCUMENT_TOOL_NAME, executor.policy.allowed_tools)
        self.assertNotIn(DELIVER_SPREADSHEET_TOOL_NAME, executor.policy.allowed_tools)


if __name__ == "__main__":
    unittest.main()
