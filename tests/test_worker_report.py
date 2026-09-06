"""IN-10/#650：SDK 终止消息里 ``errors``/``api_error_status`` 的保真链路。

真实 ``claude_agent_sdk.ResultMessage``（0.2.128）没有单数 ``error`` 字段——
``adapters/claude_agent_session.py`` 此前读的是这个不存在的字段，恒为
``None``，``apps/worker/report.py`` 靠它识别"上下文过长"的分支在真实 SDK 下
从未真正生效。本文件覆盖修复后的三段链路，各自独立可测：

1. ``core/execution/message_stream.py`` 的 ``TurnStreamRecorder`` 把规范化事件
   收拢成 ``result_errors``/``api_error_status`` 两个属性，并独立于适配器再做
   一次防御性的条数/长度收紧（见该类文档"第二道防线"）。
2. ``apps/worker/report.py`` 的 ``_resolve_effective_failure`` 现在从
   ``result_errors``（而不是从不存在的 ``result_error``）里找"上下文过长"的
   自由文本信号。
3. ``build_report`` 把两个新字段投影进最终报告：``errors`` 过一道出口脱敏
   （``sdk_result_errors``），``api_error_status`` 按 SDK 自身文档"可安全记录"
   原样携带（``sdk_api_error_status``），且不改变既有的 ``is_error`` 阻断行为
   （``turn.closed`` 不因为新字段而被放宽）。

不打桩 SDK：这三段全部是纯 Python 逻辑，接口是已规范化的字典事件，不需要
``claude_agent_sdk``（真实字段名契约由 ``tests/test_claude_agent_session_adapter.py``
的 ``RealResultMessageContractTest`` 单独核对）。
"""

from __future__ import annotations

import json
import unittest

from lingxi.apps.worker.report import _resolve_effective_failure, build_report
from lingxi.core.execution.audit import TurnAudit
from lingxi.core.execution.message_stream import TurnStreamRecorder

READ_ONLY_TOOL = "mcp__query__list_metrics"
# 与 tests/test_input_safety.py 同款假凭据：混了数字的 alnum 长串，能稳定命中
# core/execution/audit.py 的 ``_TOKEN_RUN``（该规则对纯字母短串放行，对这种
# 混合形态不放行），不依赖任何真实凭据形态。
FAKE_CREDENTIAL = "LINGXI_FAKE_SECRET_a1b2c3d4e5f6g7h8"


def _result_event(**overrides: object) -> dict[str, object]:
    """构造一条最小的规范化 ``result`` 事件，字段按各测试需要覆盖。"""
    event: dict[str, object] = {"kind": "result", "subtype": "success", "is_error": False}
    event.update(overrides)
    return event


def _recorder_after(*events: dict[str, object]) -> tuple[TurnAudit, TurnStreamRecorder]:
    """新建一对 ``TurnAudit``/``TurnStreamRecorder``，依次喂完给定事件。"""
    audit = TurnAudit()
    stream = TurnStreamRecorder(audit)
    for event in events:
        stream.handle(event)
    return audit, stream


class TurnStreamRecorderResultErrorsTest(unittest.TestCase):
    """``core/execution/message_stream.py`` 新增的两个属性。"""

    def test_defaults_are_none_before_any_result_event(self) -> None:
        _audit, stream = _recorder_after()

        self.assertIsNone(stream.result_errors)
        self.assertIsNone(stream.api_error_status)

    def test_result_errors_and_api_error_status_are_exposed(self) -> None:
        _audit, stream = _recorder_after(
            _result_event(errors=["boom"], api_error_status=429, is_error=True)
        )

        self.assertEqual(stream.result_errors, ("boom",))
        self.assertEqual(stream.api_error_status, 429)

    def test_a_later_result_event_without_errors_clears_the_previous_ones(self) -> None:
        """两次尝试（resume 降级重试）不得让上一次的错误残留到这一次。"""
        _audit, stream = _recorder_after(
            _result_event(errors=["first attempt failed"], api_error_status=529),
            _result_event(),
        )

        self.assertIsNone(stream.result_errors)
        self.assertIsNone(stream.api_error_status)

    def test_non_string_and_empty_items_are_dropped(self) -> None:
        _audit, stream = _recorder_after(_result_event(errors=[None, "", 42, "real"]))

        self.assertEqual(stream.result_errors, ("real",))

    def test_api_error_status_bool_and_non_int_are_ignored(self) -> None:
        """``bool`` 是 ``int`` 的子类，必须显式排除，否则 ``True``/``False``
        会被误当成状态码 1/0 记下。"""
        _audit, stream_bool = _recorder_after(_result_event(api_error_status=True))
        _audit2, stream_str = _recorder_after(_result_event(api_error_status="429"))

        self.assertIsNone(stream_bool.api_error_status)
        self.assertIsNone(stream_str.api_error_status)

    def test_defensive_second_bound_caps_items_even_if_upstream_sends_more(self) -> None:
        """本层独立于适配器再收紧一次：直接喂超过适配器上限条数的原始事件
        （绕开 ``adapters/claude_agent_session.py`` 的截断），本层仍必须夹住。

        变异验红锚点：去掉 ``handle()`` 里对 ``errors`` 的切片，本用例应变红。
        """
        from lingxi.core.execution.message_stream import _MAX_RESULT_ERRORS_KEPT

        many = [f"e{i}" for i in range(50)]
        _audit, stream = _recorder_after(_result_event(errors=many))

        self.assertLessEqual(len(stream.result_errors), _MAX_RESULT_ERRORS_KEPT)

    def test_defensive_second_bound_caps_item_length_even_if_upstream_sends_more(self) -> None:
        """同上，针对单条长度；变异验红锚点：去掉 ``item[:_MAX_RESULT_ERROR_CHARS]``。"""
        from lingxi.core.execution.message_stream import _MAX_RESULT_ERROR_CHARS

        oversized = "z" * 5000
        _audit, stream = _recorder_after(_result_event(errors=[oversized]))

        self.assertLessEqual(len(stream.result_errors[0]), _MAX_RESULT_ERROR_CHARS)


class EffectiveFailureContextTooLongTest(unittest.TestCase):
    """``_resolve_effective_failure``：识别改读 ``result_errors``（此前的死代码）。"""

    def test_explicit_failure_wins_over_any_stream_signal(self) -> None:
        _audit, stream = _recorder_after(
            _result_event(errors=["context length exceeded the maximum window"])
        )

        resolved = _resolve_effective_failure({"code": "turn_timeout", "message": "x"}, stream)

        self.assertEqual(resolved, {"code": "turn_timeout", "message": "x"})

    def test_context_too_long_is_detected_from_result_errors_free_text(self) -> None:
        """此前的死代码：真实 SDK 下 ``result_error`` 恒为 ``None``，这条分支
        从未真正生效。改读 ``result_errors`` 后必须重新生效。"""
        _audit, stream = _recorder_after(
            _result_event(
                is_error=True,
                errors=["Error: prompt is too long, context window limit reached"],
            )
        )

        resolved = _resolve_effective_failure(None, stream)

        self.assertEqual(
            resolved, {"code": "context_too_long", "message": "agent_context_too_long"}
        )

    def test_result_subtype_path_still_detects_context_too_long(self) -> None:
        """回归：改动前就存在、且仍然生效的另一条识别路径不能被顺手改坏。"""
        _audit, stream = _recorder_after(
            _result_event(subtype="error_context_too_long", is_error=True)
        )

        resolved = _resolve_effective_failure(None, stream)

        self.assertEqual(
            resolved, {"code": "context_too_long", "message": "agent_context_too_long"}
        )

    def test_unrelated_errors_do_not_trigger_context_too_long(self) -> None:
        _audit, stream = _recorder_after(
            _result_event(is_error=True, errors=["rate limited, please retry later"])
        )

        self.assertIsNone(_resolve_effective_failure(None, stream))

    def test_api_error_status_alone_does_not_trigger_context_too_long(self) -> None:
        """429/529 是限流/过载，不是"上下文过长"——不得被这条识别分支误判。"""
        for status in (429, 529):
            with self.subTest(status=status):
                _audit, stream = _recorder_after(
                    _result_event(is_error=True, api_error_status=status)
                )

                self.assertIsNone(_resolve_effective_failure(None, stream))


class BuildReportSdkErrorProjectionTest(unittest.TestCase):
    """``build_report`` 把新字段投影进最终报告，且不放宽既有阻断行为。"""

    def _build(self, *, result_event: dict[str, object]) -> dict[str, object]:
        audit, stream = _recorder_after(
            {"kind": "assistant_message", "text": "已完成查询。"}, result_event
        )
        audit.record_terminal_result()
        return build_report(
            trace_id="01J0000000000000000TEST900",
            question="查询日活",
            allowed_tools=(READ_ONLY_TOOL,),
            summary=audit.summary(),
            stream=stream,
            final_text=stream.final_text,
            duration_seconds=0.1,
        )

    def test_sdk_result_errors_none_projects_to_none(self) -> None:
        report = self._build(result_event=_result_event())

        self.assertIsNone(report["turn"]["sdk_result_errors"])
        self.assertIsNone(report["turn"]["sdk_api_error_status"])

    def test_sdk_result_errors_are_redacted_before_leaving_the_process(self) -> None:
        """有界只在更早的层做过；出口脱敏是报告投影这一层新加的一道。"""
        report = self._build(
            result_event=_result_event(
                is_error=True, errors=[f"upstream call failed, token={FAKE_CREDENTIAL}"]
            )
        )

        errors = report["turn"]["sdk_result_errors"]
        self.assertIsNotNone(errors)
        self.assertNotIn(FAKE_CREDENTIAL, errors[0])
        self.assertNotIn(FAKE_CREDENTIAL, json.dumps(report, ensure_ascii=False))

    def test_adapters_truncation_marker_survives_report_redaction(self) -> None:
        """适配器层留下的可见截断标记不应被出口脱敏误伤掉。

        正文特意用短词重复拼接而不是长串重复字符——长度相同的纯字符重复串
        会被 ``_TOKEN_RUN`` 当成候选凭据整体替换（`core/execution/audit.py`
        的既有规则，与本次修复无关），那样会让这条用例测的是脱敏规则本身，
        而不是"标记是否被误伤"。
        """
        already_bounded = [
            ("error detail " * 40)[:500] + "…[TRUNCATED]",
            "[+3 more errors omitted]",
        ]

        report = self._build(result_event=_result_event(is_error=True, errors=already_bounded))

        errors = report["turn"]["sdk_result_errors"]
        self.assertEqual(errors, already_bounded)

    def test_sdk_api_error_status_429_and_529_are_carried_without_side_effects(self) -> None:
        """429/529 要能在报告里查到，但不得被误判成"上下文过长"或改写终止状态。"""
        for status in (429, 529):
            with self.subTest(status=status):
                report = self._build(
                    result_event=_result_event(is_error=True, api_error_status=status)
                )

                self.assertEqual(report["turn"]["sdk_api_error_status"], status)
                self.assertIsNone(report["failure"])
                self.assertNotEqual(report["turn"]["termination_reason"], "context_too_long")

    def test_is_error_blocking_is_preserved_not_widened_into_forced_success(self) -> None:
        """六条款/卡面裁定：``is_error`` 的既有阻断行为原样保留，不得被新字段
        接线顺手放宽——失败依然不会被判定成"已收口"。"""
        report = self._build(
            result_event=_result_event(
                is_error=True, errors=["transient upstream failure"], api_error_status=529
            )
        )

        self.assertFalse(report["turn"]["closed"])
        self.assertTrue(report["turn"]["sdk_result_is_error"])
