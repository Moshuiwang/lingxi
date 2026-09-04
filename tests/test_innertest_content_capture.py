"""内测轮内容级采集的纯逻辑断言（Issue #251/#304 批次 3，V-采集-04/05）。

这些用例不依赖数据库或 Claude Agent SDK：核心逻辑全部在
``lingxi.core.innertest_content_capture`` 与 ``lingxi.core.execution.audit`` 里，
因此可以在 CI 的 gate 中强制执行。真实落库由 ``tests/test_postgres_content_capture.py``
的真库用例补齐（V-采集-08/09）。

伪造值统一使用明显假的占位（不使用任何形似真实凭据的值，协作约定：测试与 CI
不接触真实凭据）。
"""

from __future__ import annotations

import unittest

from lingxi.core.execution.audit import TurnAudit, redact_free_text, redact_free_text_with_count
from lingxi.core.execution.tool_policy import ToolPolicy
from lingxi.core.innertest_content_capture import (
    MAX_TOOL_RESULT_SUMMARY_BYTES,
    CapturedToolCall,
    ContentCaptureRecord,
    RawTurnCapture,
)

# 明显假的占位，形状能命中脱敏规则（16+ 字符且含数字，或 bearer/basic 认证头），
# 但不是任何真实系统会签发的值。
_FAKE_TOKEN = "sk-fake-token-1234567890abcdef"
_FAKE_ASSIGNMENT = f"token={_FAKE_TOKEN}"


def _policy() -> ToolPolicy:
    return ToolPolicy(allowed_tools=("mcp__q__list_metrics",))


class RawTurnCaptureBuildRecordTests(unittest.TestCase):
    """``RawTurnCapture`` 把审计摘要（结构）与本收集器持有的原始素材（内容）
    合并成一条 ``ContentCaptureRecord`` 的核心行为。"""

    def test_question_and_answer_are_kept_in_full_except_credential_shapes(self) -> None:
        """2026-08-24 裁定：原文与正文不设限，唯一过滤的是凭据形状。"""

        capture = RawTurnCapture()
        capture.on_stream_event(
            {"kind": "assistant_message", "text": f"最终回答，附带一个秘密 {_FAKE_ASSIGNMENT}"}
        )
        audit = TurnAudit()

        record = capture.build_record(
            task_id="tsk-1",
            worker_id="worker-1",
            question=f"用户问题，附带一个秘密 {_FAKE_ASSIGNMENT}",
            summary=audit.summary(),
        )

        self.assertNotIn(_FAKE_TOKEN, record.question_content)
        self.assertIn("用户问题，附带一个秘密", record.question_content)
        self.assertGreater(record.question_redaction_count, 0)

        self.assertNotIn(_FAKE_TOKEN, record.answer_content)
        self.assertIn("最终回答，附带一个秘密", record.answer_content)
        self.assertGreater(record.answer_redaction_count, 0)

    def test_clean_content_has_zero_redaction_count(self) -> None:
        capture = RawTurnCapture()
        capture.on_stream_event({"kind": "assistant_message", "text": "上周新增用户数是 1234"})
        audit = TurnAudit()

        record = capture.build_record(
            task_id="tsk-1",
            worker_id="worker-1",
            question="上周新增用户数是多少",
            summary=audit.summary(),
        )

        self.assertEqual(record.question_redaction_count, 0)
        self.assertEqual(record.answer_redaction_count, 0)
        self.assertEqual(record.question_content, "上周新增用户数是多少")
        self.assertEqual(record.answer_content, "上周新增用户数是 1234")

    def test_tool_call_params_are_kept_in_full_not_field_whitelisted(self) -> None:
        """与 core/execution/audit.py 的 AuditRedactor 字段白名单**刻意不同**：
        采集的 tool_input 是原始参数，未进白名单的字段不会被压成
        ``{"omitted": True}``——那正是"内容级采集"要补的东西（#251 起因：既有
        审计刻意不记录业务正文）。"""

        capture = RawTurnCapture()
        capture.on_pre_tool_use("t1", {"metric": "new_users", "country": "CN"})
        audit = TurnAudit()  # 默认字段白名单为空——用来对照"审计侧确实会省略"
        verdict = _policy().decide("mcp__q__list_metrics", {"metric": "new_users", "country": "CN"})
        audit.record_decision(
            tool_name=verdict.tool_name,
            tool_input={"metric": "new_users", "country": "CN"},
            tool_use_id="t1",
            verdict=verdict,
        )
        audit.record_executed(tool_name="mcp__q__list_metrics", tool_use_id="t1")
        audit.record_tool_result(tool_use_id="t1", content='{"metrics": [{"id": "m1"}]}')
        capture.on_stream_event(
            {
                "kind": "tool_result",
                "tool_use_id": "t1",
                "content": '{"metrics": [{"id": "m1"}]}',
                "is_error": False,
            }
        )

        record = capture.build_record(
            task_id="tsk-1", worker_id="worker-1", question="q", summary=audit.summary()
        )

        self.assertEqual(len(record.tool_calls), 1)
        call = record.tool_calls[0]
        self.assertEqual(call.tool_name, "mcp__q__list_metrics")
        # 审计侧同一字段会被省略（未进白名单），采集侧必须原样保留——两条通道
        # 目的相反，互不影响。
        self.assertEqual(audit.summary().calls[0].tool_input["metric"], {"omitted": True})
        self.assertEqual(call.tool_input, {"metric": "new_users", "country": "CN"})
        self.assertEqual(call.result_summary["result_kind"], "ok")
        self.assertIn("m1", call.result_summary["content"])
        self.assertEqual(call.redaction_count, 0)

    def test_credential_shapes_in_tool_input_and_result_are_redacted_and_counted(self) -> None:
        capture = RawTurnCapture()
        capture.on_pre_tool_use("t1", {"metric": "new_users", "auth": _FAKE_ASSIGNMENT})
        audit = TurnAudit()
        verdict = _policy().decide("mcp__q__list_metrics", {})
        audit.record_decision(
            tool_name=verdict.tool_name, tool_input={}, tool_use_id="t1", verdict=verdict
        )
        audit.record_tool_result(
            tool_use_id="t1", content=f"upstream rejected, saw {_FAKE_ASSIGNMENT}", is_error=True
        )
        capture.on_stream_event(
            {
                "kind": "tool_result",
                "tool_use_id": "t1",
                "content": f"upstream rejected, saw {_FAKE_ASSIGNMENT}",
                "is_error": True,
            }
        )

        record = capture.build_record(
            task_id="tsk-1", worker_id="worker-1", question="q", summary=audit.summary()
        )

        call = record.tool_calls[0]
        self.assertNotIn(_FAKE_TOKEN, str(call.tool_input))
        self.assertNotIn(_FAKE_TOKEN, call.result_summary["content"])
        self.assertIn("upstream rejected", call.result_summary["content"])
        self.assertTrue(call.result_summary["is_error"])
        self.assertGreater(call.redaction_count, 0)
        self.assertEqual(record.tool_calls_redaction_count, call.redaction_count)

    def test_denied_calls_still_capture_the_attempted_params(self) -> None:
        """被拒绝的调用（模型试图调什么）同样是内容级采集要看的信号，不只是
        放行的调用才有价值——与执行层"拒绝也要记账"同一条纪律的另一面。"""

        capture = RawTurnCapture()
        capture.on_pre_tool_use("t1", {"reckless": "true"})
        audit = TurnAudit()
        # 不在白名单里，`decide` 会拒绝。
        verdict = ToolPolicy(allowed_tools=("mcp__q__only_this",)).decide(
            "mcp__q__other", {"reckless": "true"}
        )
        audit.record_decision(
            tool_name=verdict.tool_name,
            tool_input={"reckless": "true"},
            tool_use_id="t1",
            verdict=verdict,
        )

        record = capture.build_record(
            task_id="tsk-1", worker_id="worker-1", question="q", summary=audit.summary()
        )

        call = record.tool_calls[0]
        self.assertFalse(call.result_summary["allowed"])
        self.assertEqual(call.tool_input, {"reckless": "true"})

    def test_uncorrelated_call_is_marked_not_captured_instead_of_an_empty_dict(self) -> None:
        """没有对应 ``tool_use_id`` 的调用（例如 hook 未触发的旁路调用，或审计
        记账自身失败的兜底记录）没有原始入参可用；必须显式标「未捕获」，不能
        留一个空字典冒充"这次调用没有参数"。"""

        capture = RawTurnCapture()  # 没有 on_pre_tool_use 调用
        audit = TurnAudit()
        audit.record_tool_result(tool_use_id=None, content="某个结果", is_error=None)

        record = capture.build_record(
            task_id="tsk-1", worker_id="worker-1", question="q", summary=audit.summary()
        )

        self.assertEqual(len(record.tool_calls), 1)
        self.assertEqual(record.tool_calls[0].tool_input, {"captured": False})

    def test_tool_result_content_is_truncated_but_not_silently(self) -> None:
        """ "结果摘要"是摘要，不是全文——超过字节上限时截断，且显式标注
        truncated=True，不假装截断后的内容是完整的。

        用中文重复句子构造超长正文，**不用**单一字符重复（例如 ``"x" * N``）：
        后者本身会被 `_TOKEN_RUN` 规则当成一整个裸令牌串命中脱敏（≥32 字符的
        任意连串一律抹除），脱敏后反而变短，测不到截断逻辑，见
        `core/execution/audit.py` 的 `_mask_token_run`。中文字符不落在
        `[A-Za-z0-9+/=_-]` 范围内，不会触发这条规则，是更贴近真实业务正文
        （中文问数结果）的超长样本。
        """

        capture = RawTurnCapture()
        capture.on_pre_tool_use("t1", {})
        huge_result = "正常查询结果，不含任何秘密。" * 500
        self.assertGreater(len(huge_result.encode("utf-8")), MAX_TOOL_RESULT_SUMMARY_BYTES)
        capture.on_stream_event(
            {"kind": "tool_result", "tool_use_id": "t1", "content": huge_result, "is_error": False}
        )
        audit = TurnAudit()
        verdict = _policy().decide("mcp__q__list_metrics", {})
        audit.record_decision(
            tool_name=verdict.tool_name, tool_input={}, tool_use_id="t1", verdict=verdict
        )
        audit.record_tool_result(tool_use_id="t1", content=huge_result, is_error=False)

        record = capture.build_record(
            task_id="tsk-1", worker_id="worker-1", question="q", summary=audit.summary()
        )

        call = record.tool_calls[0]
        self.assertTrue(call.result_summary["truncated"])
        self.assertLessEqual(
            len(call.result_summary["content"].encode("utf-8")), MAX_TOOL_RESULT_SUMMARY_BYTES
        )

    def test_question_and_answer_are_not_truncated_even_when_very_long(self) -> None:
        """与工具结果**刻意不同**：问题/回答按裁定不设截断上限。"""

        capture = RawTurnCapture()
        long_answer = "答案内容。" * 2000  # 远超过工具结果的截断上限
        capture.on_stream_event({"kind": "assistant_message", "text": long_answer})
        audit = TurnAudit()

        record = capture.build_record(
            task_id="tsk-1", worker_id="worker-1", question="q", summary=audit.summary()
        )

        self.assertEqual(record.answer_content, long_answer)
        self.assertGreater(
            len(record.answer_content.encode("utf-8")), MAX_TOOL_RESULT_SUMMARY_BYTES
        )


class RedactFreeTextWithCountTests(unittest.TestCase):
    """``redact_free_text_with_count`` 与既有 ``redact_free_text`` 逐字节同文，
    只是额外给出命中次数（V-采集-05 的直接依赖）。"""

    def test_output_text_matches_redact_free_text_exactly(self) -> None:
        samples = (
            "",
            "普通中文文本，没有任何秘密",
            _FAKE_ASSIGNMENT,
            f"Authorization: Bearer {_FAKE_TOKEN}",
            "SIMULATED_UPSTREAM_FAILURE",  # 全字母错误码，不应被当成令牌抹掉
            "a" * 40,  # 32+ 字符裸连串（无数字，靠长度规则命中）
        )
        for text in samples:
            with self.subTest(text=text):
                redacted, _count = redact_free_text_with_count(text)
                self.assertEqual(redacted, redact_free_text(text))

    def test_count_is_zero_for_clean_text(self) -> None:
        _redacted, count = redact_free_text_with_count("上周新增用户数是 1234")
        self.assertEqual(count, 0)

    def test_count_is_positive_when_a_secret_is_redacted(self) -> None:
        _redacted, count = redact_free_text_with_count(_FAKE_ASSIGNMENT)
        self.assertGreater(count, 0)

    def test_count_reflects_multiple_hits(self) -> None:
        text = f"{_FAKE_ASSIGNMENT} 与另一个 password={_FAKE_TOKEN}"
        _redacted, count = redact_free_text_with_count(text)
        self.assertGreaterEqual(count, 2)


class ContentCaptureRecordPayloadTests(unittest.TestCase):
    """``ContentCaptureRecord`` 的 JSON 安全投影，交给适配器落库前的最终形态。"""

    def test_tool_calls_payload_shape_and_aggregate_redaction_count(self) -> None:
        record = ContentCaptureRecord(
            task_id="tsk-1",
            worker_id="worker-1",
            question_content="q",
            question_redaction_count=0,
            answer_content="a",
            answer_redaction_count=1,
            tool_calls=(
                CapturedToolCall(
                    tool_use_id="t1",
                    tool_name="mcp__q__list_metrics",
                    tool_input={"metric": "new_users"},
                    result_summary={"result_kind": "ok", "content": "ok", "truncated": False},
                    redaction_count=2,
                ),
                CapturedToolCall(
                    tool_use_id="t2",
                    tool_name="mcp__q__describe_metric",
                    tool_input={"captured": False},
                    result_summary={"result_kind": None, "content": "", "truncated": False},
                    redaction_count=0,
                ),
            ),
        )

        self.assertEqual(record.tool_calls_redaction_count, 2)
        payload = record.tool_calls_payload()
        self.assertEqual(len(payload), 2)
        self.assertEqual(payload[0]["tool_use_id"], "t1")
        self.assertEqual(payload[0]["tool_name"], "mcp__q__list_metrics")
        self.assertEqual(payload[0]["redaction_count"], 2)
        self.assertEqual(payload[1]["tool_input"], {"captured": False})


if __name__ == "__main__":
    unittest.main()
