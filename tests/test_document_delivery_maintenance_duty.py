"""文档投递死信扫描 + 正文到期擦除职责的断言（Issue #341 R-2/`V-投递-06`）。

纯逻辑层（假存储）：每轮各调用一次、失败关闭并留审计、停止中一条都不领、
死信数量 > 0 时告警、数量为 0 时不告警、审计不含任何行内容。真库层的
``fail_expired_pending``/``redact_expired_content`` 断言在
``tests/test_document_delivery_transport.py``（需要真实触发器与 CHECK 约束）。
"""

from __future__ import annotations

import threading
import unittest

from lingxi.apps.scheduler.document_delivery_dead_letter import (
    DocumentDeliveryMaintenanceDuty,
    DocumentDeliveryMaintenanceReport,
)


class FakeStore:
    def __init__(
        self,
        *,
        dead_lettered: int = 0,
        content_redacted: int = 0,
        dead_letter_error: Exception | None = None,
        redact_error: Exception | None = None,
    ) -> None:
        self._dead_lettered = dead_lettered
        self._content_redacted = content_redacted
        self._dead_letter_error = dead_letter_error
        self._redact_error = redact_error
        self.dead_letter_calls = 0
        self.redact_calls = 0

    def fail_expired_pending(self) -> int:
        self.dead_letter_calls += 1
        if self._dead_letter_error is not None:
            raise self._dead_letter_error
        return self._dead_lettered

    def redact_expired_content(self) -> int:
        self.redact_calls += 1
        if self._redact_error is not None:
            raise self._redact_error
        return self._content_redacted


class RecordingAudit:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.entries.append((action, dict(fields)))

    @property
    def actions(self) -> list[str]:
        return [action for action, _ in self.entries]


def build_duty(
    *,
    store: FakeStore | None = None,
    alert=None,
    stop: threading.Event | None = None,
) -> tuple[DocumentDeliveryMaintenanceDuty, dict]:
    parts = {"store": store if store is not None else FakeStore(), "audit": RecordingAudit()}
    duty = DocumentDeliveryMaintenanceDuty(
        store=parts["store"], audit=parts["audit"], alert=alert, stop=stop
    )
    return duty, parts


class SweepTest(unittest.TestCase):
    def test_every_round_sweeps_both_tasks_exactly_once(self) -> None:
        duty, parts = build_duty(store=FakeStore(dead_lettered=2, content_redacted=5))

        report = duty.run_once()

        self.assertEqual(parts["store"].dead_letter_calls, 1)
        self.assertEqual(parts["store"].redact_calls, 1)
        self.assertEqual(
            report, DocumentDeliveryMaintenanceReport(dead_lettered=2, content_redacted=5)
        )
        self.assertIn("document_delivery_maintenance.completed", parts["audit"].actions)

    def test_a_stopping_duty_sweeps_nothing(self) -> None:
        stop = threading.Event()
        stop.set()
        duty, parts = build_duty(store=FakeStore(dead_lettered=3), stop=stop)

        self.assertIsNone(duty.run_once())
        self.assertEqual((parts["store"].dead_letter_calls, parts["store"].redact_calls), (0, 0))
        self.assertEqual(parts["audit"].actions, [])

    def test_zero_candidates_records_no_audit(self) -> None:
        """健康系统里绝大多数 tick 什么都不该做，不能无条件刷审计。"""

        duty, parts = build_duty(store=FakeStore())

        duty.run_once()

        self.assertEqual(parts["audit"].actions, [])

    def test_one_stop_flag_reaches_the_duty(self) -> None:
        stop = threading.Event()
        duty, _ = build_duty(stop=stop)

        self.assertFalse(duty.stopping)
        duty.request_stop()
        self.assertTrue(duty.stopping)
        self.assertTrue(stop.is_set())

    def test_dead_letter_sweep_failure_does_not_block_the_content_sweep(self) -> None:
        """一段查询失败只降级这一段，不带走另一段（同
        ``DocumentDeliveryConsumer.run_once`` 对 ``fail_exhausted_pending``/
        ``reclaim_stale_processing`` 的既有姿态）。"""

        store = FakeStore(dead_letter_error=RuntimeError("boom"), content_redacted=4)
        duty, parts = build_duty(store=store)

        report = duty.run_once()

        self.assertEqual(store.redact_calls, 1)
        self.assertEqual(report, DocumentDeliveryMaintenanceReport(dead_lettered=0, content_redacted=4))
        self.assertIn("document_delivery_maintenance.dead_letter_sweep_failed", parts["audit"].actions)

    def test_content_redaction_failure_does_not_block_the_dead_letter_sweep(self) -> None:
        store = FakeStore(dead_lettered=6, redact_error=RuntimeError("boom"))
        duty, parts = build_duty(store=store)

        report = duty.run_once()

        self.assertEqual(store.dead_letter_calls, 1)
        self.assertEqual(report, DocumentDeliveryMaintenanceReport(dead_lettered=6, content_redacted=0))
        self.assertIn(
            "document_delivery_maintenance.content_redaction_failed", parts["audit"].actions
        )


class AlertTest(unittest.TestCase):
    def test_a_positive_dead_letter_count_alerts(self) -> None:
        alerts: list[tuple[str, str]] = []
        duty, _ = build_duty(
            store=FakeStore(dead_lettered=3), alert=lambda kind, task_id: alerts.append((kind, task_id))
        )

        duty.run_once()

        self.assertEqual(alerts, [("document_delivery_pending_expired", "")])

    def test_a_zero_dead_letter_count_never_alerts(self) -> None:
        alerts: list[tuple[str, str]] = []
        duty, _ = build_duty(
            store=FakeStore(dead_lettered=0), alert=lambda kind, task_id: alerts.append((kind, task_id))
        )

        duty.run_once()

        self.assertEqual(alerts, [])

    def test_no_alert_callback_wired_does_not_crash(self) -> None:
        duty, _ = build_duty(store=FakeStore(dead_lettered=5), alert=None)

        report = duty.run_once()

        self.assertEqual(report.dead_lettered, 5)

    def test_a_failing_alert_callback_does_not_lose_the_completed_sweep_result(self) -> None:
        def broken_alert(kind: str, task_id: str) -> None:
            raise RuntimeError("alert transport down")

        duty, parts = build_duty(store=FakeStore(dead_lettered=2), alert=broken_alert)

        report = duty.run_once()

        self.assertEqual(report.dead_lettered, 2)
        self.assertIn("document_delivery_maintenance.completed", parts["audit"].actions)


if __name__ == "__main__":
    unittest.main()
