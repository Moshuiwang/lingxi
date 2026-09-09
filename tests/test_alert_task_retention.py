"""任务投递的闲置告警记录可以释放，计数、投递和恢复仍独立完成。"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from lingxi.core.alerting import (
    AlertDispatcher,
    AlertingDuty,
    AlertKind,
    AlertManager,
    AlertPolicy,
    AlertSignal,
    NoticeAction,
)
from lingxi.core.ids import new_ulid

START = datetime(2026, 9, 9, tzinfo=UTC)
TASK = "tsk_01J00000000000000000000001"


def signal(*, seconds=0, **fields):
    return AlertSignal(
        kind=AlertKind.FEISHU_SEND_FAILED,
        scope="document_delivery_failed",
        observed_at=START + timedelta(seconds=seconds),
        task_id=fields.pop("task_id", TASK),
        final=fields.pop("final", True),
        **fields,
    )


class TaskAlertRetentionTests(unittest.TestCase):
    def test_many_terminal_tasks_expire_without_false_recovery(self):
        manager = AlertManager()
        for index in range(1000):
            task_id = "tsk_" + new_ulid(now_ms=index, randomness=b"\x00" * 10)
            self.assertEqual(len(manager.observe(signal(task_id=task_id))), 1)
        self.assertEqual(len(manager._windows), 1000)
        manager.send_succeeded(channel="document_delivery_failed", at=START + timedelta(seconds=1))
        self.assertEqual(manager.tick(at=START + timedelta(seconds=1799)), ())
        self.assertEqual(len(manager._windows), 1000)
        self.assertEqual(manager.tick(at=START + timedelta(seconds=1800)), ())
        self.assertEqual(len(manager._windows), 0)
        self.assertEqual(manager.tick(at=START + timedelta(days=366)), ())

    def test_continuing_failure_keeps_window_and_reminds_at_existing_interval(self):
        manager = AlertManager()
        first = manager.observe(signal())[0]
        self.assertEqual(manager.observe(signal(seconds=1799)), ())
        self.assertEqual(manager.tick(at=START + timedelta(seconds=1800)), ())
        self.assertEqual(len(manager._windows), 1)
        reminder = manager.observe(signal(seconds=1800))[0]
        self.assertEqual(reminder.dedupe_key, first.dedupe_key)
        self.assertEqual(reminder.count, 3)
        self.assertEqual(manager.tick(at=START + timedelta(seconds=3599)), ())
        self.assertEqual(len(manager._windows), 1)
        self.assertEqual(manager.tick(at=START + timedelta(seconds=3600)), ())
        self.assertEqual(len(manager._windows), 0)
        self.assertEqual(len(manager.observe(signal(seconds=3601))), 1)

    def test_non_task_and_non_delivery_failures_are_not_expired(self):
        manager = AlertManager()
        manager.observe(signal(task_id=None))
        manager.observe(replace(signal(), kind=AlertKind.QUEUED_STUCK, final=False))
        self.assertEqual(manager.tick(at=START + timedelta(days=366)), ())
        self.assertEqual(len(manager._windows), 2)
        self.assertEqual(len(manager.observe(signal(task_id=None, seconds=1800))), 1)

    def test_non_final_delivery_callbacks_expire_without_false_recovery(self):
        manager = AlertManager()
        dispatcher = AlertDispatcher(sender=None, chat_id="synthetic", clock=lambda: START)
        duty = AlertingDuty(manager=manager, dispatcher=dispatcher, clock=lambda: START)
        for kind in ("progress_persist_failed:RuntimeError", "card_finish_uncertain:timeout"):
            duty.delivery_alert_callback()(kind, TASK)
        self.assertEqual(dispatcher.pending_count, 0)
        self.assertEqual(len(manager._windows), 2)
        self.assertEqual(manager.tick(at=START + timedelta(seconds=1799)), ())
        self.assertEqual(len(manager._windows), 2)
        self.assertEqual(manager.tick(at=START + timedelta(seconds=1800)), ())
        self.assertEqual(len(manager._windows), 0)

    def test_short_dedupe_does_not_expire_in_progress_failure_counting(self):
        manager = AlertManager(policy=AlertPolicy(dedupe_window_seconds=10))
        self.assertEqual(manager.observe(signal(final=False)), ())
        self.assertEqual(manager.tick(at=START + timedelta(seconds=10)), ())
        self.assertEqual(len(manager._windows), 1)
        self.assertEqual(manager.observe(signal(final=False, seconds=298)), ())
        self.assertEqual(manager.tick(at=START + timedelta(seconds=299)), ())
        notice = manager.observe(signal(final=False, seconds=299))[0]
        self.assertEqual(notice.count, 3)
        self.assertEqual(manager.tick(at=START + timedelta(seconds=598)), ())
        self.assertEqual(len(manager._windows), 1)
        self.assertEqual(manager.tick(at=START + timedelta(seconds=599)), ())
        self.assertEqual(len(manager._windows), 0)

    def test_explicit_recovery_keeps_its_stable_period_even_after_dedupe_expiry(self):
        manager = AlertManager(
            policy=AlertPolicy(dedupe_window_seconds=10, send_failure_window_seconds=10)
        )
        manager.observe(signal())
        self.assertEqual(manager.resolve(signal(seconds=1)), ())
        self.assertEqual(manager.tick(at=START + timedelta(seconds=300)), ())
        self.assertEqual(len(manager._windows), 1)
        recovered = manager.tick(at=START + timedelta(seconds=301))
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].action, NoticeAction.RECOVERY)
        self.assertEqual(recovered[0].task_id, TASK)
        self.assertEqual(len(manager._windows), 0)
        self.assertEqual(manager.tick(at=START + timedelta(seconds=302)), ())

    def test_new_failure_cancels_recovery_and_restarts_expiry_from_failure(self):
        manager = AlertManager(
            policy=AlertPolicy(dedupe_window_seconds=10, send_failure_window_seconds=10)
        )
        manager.observe(signal())
        manager.resolve(signal(seconds=1))
        self.assertEqual(manager.observe(signal(seconds=5)), ())
        self.assertEqual(manager.tick(at=START + timedelta(seconds=14)), ())
        self.assertEqual(len(manager._windows), 1)
        self.assertEqual(manager.tick(at=START + timedelta(seconds=15)), ())
        self.assertEqual(len(manager._windows), 0)

    def test_expiry_does_not_drop_failed_dispatch_or_change_retry_identity(self):
        now = START
        attempts = []

        class Sender:
            def send_text(self, **fields):
                attempts.append(fields)
                if len(attempts) < 3:
                    raise RuntimeError("synthetic transport failure")

        manager = AlertManager()
        dispatcher = AlertDispatcher(sender=Sender(), chat_id="synthetic", clock=lambda: now)
        duty = AlertingDuty(manager=manager, dispatcher=dispatcher, clock=lambda: now)
        duty.delivery_alert_callback()("document_delivery_failed", TASK)
        duty.run_once()
        self.assertEqual(dispatcher.pending_count, 1)
        now += timedelta(seconds=1800)
        self.assertEqual(duty.run_once(), ())
        self.assertEqual(len(manager._windows), 0)
        self.assertEqual(dispatcher.pending_count, 1)
        self.assertEqual(dispatcher.observed_delays, [1, 2])
        now += timedelta(seconds=1)
        duty.run_once()
        self.assertEqual(len(attempts), 2)
        now += timedelta(seconds=1)
        duty.run_once()
        self.assertEqual(dispatcher.pending_count, 0)
        self.assertEqual(len(attempts), 3)
        self.assertTrue(all(item == attempts[0] for item in attempts))
        self.assertIn("T-01J00000000000000000000001", attempts[0]["text"])


if __name__ == "__main__":
    unittest.main()
