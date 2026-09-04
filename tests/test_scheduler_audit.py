"""``apps/scheduler/audit.py`` 的严重度提升规则（Issue #282 §0.3 核实结论修复）。

`core/identity/onboarding_runner.py` 与 `core/conversation/onboarding_recovery.py` 的
文档字符串在多处写下「动作名带 ``failed`` 后缀，由审计实现升到 ``WARNING``」，但
scheduler 的 :class:`~lingxi.apps.scheduler.audit.StructuredLogAuditSink` 此前无条件
``logger.info``——那条承诺从未真正兑现过。本文件钉住修复之后的真实行为，形状照
``apps/gateway/__init__.py`` 同名实现已有的用例（``test_gateway_pipeline.py`` 或等价
文件），不重复实现同一套判据，只验证 scheduler 这一份确实按同一条规则升级。
"""

from __future__ import annotations

import unittest

from lingxi.apps.scheduler.audit import StructuredLogAuditSink


class SeverityPromotionTests(unittest.TestCase):
    def test_a_failed_suffixed_action_is_a_warning(self) -> None:
        sink = StructuredLogAuditSink()

        with self.assertLogs("lingxi.apps.scheduler", level="INFO") as captured:
            sink.record("onboarding.notify_failed", event_id="evt_1")

        self.assertEqual(len(captured.output), 1)
        self.assertTrue(captured.output[0].startswith("WARNING:"))

    def test_an_error_suffixed_action_is_a_warning(self) -> None:
        sink = StructuredLogAuditSink()

        with self.assertLogs("lingxi.apps.scheduler", level="INFO") as captured:
            sink.record(
                "late_readiness_recovery.notice_processing_failed",
                user="usr_1",
                error="RuntimeError",
            )

        self.assertTrue(captured.output[0].startswith("WARNING:"))

    def test_an_unparsable_suffixed_action_is_a_warning(self) -> None:
        sink = StructuredLogAuditSink()

        with self.assertLogs("lingxi.apps.scheduler", level="INFO") as captured:
            sink.record("roster_audit.diff_unparsable")

        self.assertTrue(captured.output[0].startswith("WARNING:"))

    def test_the_extra_warning_list_promotes_stalled_provisioning_notifier_not_wired(self) -> None:
        """外部独立审查 P3-4：`_EXTRA_WARNING_ACTIONS` 扩展位（同
        `apps/gateway/__init__.py` 的同名机制）此前搬迁时被漏掉了。
        `stalled_provisioning.notifier_not_wired` 本身没有失败后缀，但描述的是
        "本轮所有停摆候选一条都不处理"这种需要运维立刻知道的降级状态。"""

        sink = StructuredLogAuditSink()

        with self.assertLogs("lingxi.apps.scheduler", level="INFO") as captured:
            sink.record("stalled_provisioning.notifier_not_wired")

        self.assertTrue(captured.output[0].startswith("WARNING:"))

    def test_an_ordinary_action_stays_at_info(self) -> None:
        sink = StructuredLogAuditSink()

        with self.assertLogs("lingxi.apps.scheduler", level="INFO") as captured:
            sink.record("roster_audit.report_sent", entries=2, examined=9, report_date="2026-08-06")

        self.assertTrue(captured.output[0].startswith("INFO:"))

    def test_onboarding_result_with_a_failure_reason_is_promoted(self) -> None:
        """`onboarding.result` 本身没有失败后缀（成功与失败共用同一个动作名），
        因此单独判它的 `failure_reason` 字段——非空说明这次开通没有成功。"""

        sink = StructuredLogAuditSink()

        with self.assertLogs("lingxi.apps.scheduler", level="INFO") as captured:
            sink.record(
                "onboarding.result",
                event_id="evt_1",
                state="internal_error",
                failure_reason="publish_not_completed",
                content_key="onboarding.internal_error",
                trace_id="trc_1",
            )

        self.assertTrue(captured.output[0].startswith("WARNING:"))

    def test_onboarding_result_without_a_failure_reason_is_not_promoted(self) -> None:
        """**否定断言**：普通动作不得被误升——成功终态（`failure_reason=None`）必须
        仍是 `INFO`，不能因为动作名恰好叫 `onboarding.result` 就被无差别升级。"""

        sink = StructuredLogAuditSink()

        with self.assertLogs("lingxi.apps.scheduler", level="INFO") as captured:
            sink.record(
                "onboarding.result",
                event_id="evt_1",
                state="completed",
                failure_reason=None,
                content_key="onboarding.completed",
                trace_id="trc_1",
            )

        self.assertTrue(captured.output[0].startswith("INFO:"))

    def test_onboarding_result_with_an_empty_string_reason_is_not_promoted(self) -> None:
        """空字符串与 ``None`` 同样不该被判成"有失败原因"。"""

        sink = StructuredLogAuditSink()

        with self.assertLogs("lingxi.apps.scheduler", level="INFO") as captured:
            sink.record(
                "onboarding.result",
                event_id="evt_1",
                state="completed",
                failure_reason="",
                content_key="onboarding.completed",
                trace_id="trc_1",
            )

        self.assertTrue(captured.output[0].startswith("INFO:"))

    def test_fields_remain_sorted_by_key_regardless_of_severity(self) -> None:
        sink = StructuredLogAuditSink()

        with self.assertLogs("lingxi.apps.scheduler", level="INFO") as captured:
            sink.record(
                "onboarding.notify_failed", event_id="evt_1", attempt=2, error="RuntimeError"
            )

        line = captured.output[0]
        self.assertLess(line.index("attempt="), line.index("error="))
        self.assertLess(line.index("error="), line.index("event_id="))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
