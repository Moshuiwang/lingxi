"""恢复扫描故障与停止期间返回的领取分别验证。"""

import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from lingxi.core.admin.followup_consumer import FollowupConsumer, FollowupResult
from lingxi.core.admin.followup_lifecycle import BackgroundLifecycle


class ConsumerRecoveryIsolationTests(unittest.TestCase):
    def test_repeated_recovery_errors_do_not_block_independent_claims(self):
        store, audit = Mock(), Mock()
        store.recover_expired.side_effect = TimeoutError("synthetic")
        item = SimpleNamespace(
            id="f",
            stage="group_notify",
            attempt=1,
            lease_owner="owner",
            pending_action_id="p",
            trace_id="t",
            batch_id=None,
            batch_item_id=None,
        )
        store.claim_followup.return_value = item
        handler = Mock(return_value=FollowupResult())
        consumer = FollowupConsumer(
            store=store,
            consumer_kind="postprocess",
            owner="owner",
            handlers={item.stage: handler},
            audit=audit,
        )
        for expected in range(1, 4):
            self.assertTrue(consumer.run_once())
            self.assertEqual(store.claim_followup.call_count, expected)
            self.assertEqual(handler.call_count, expected)
        errors = [
            c for c in audit.record.call_args_list if c.args[0] == "admin.followup.recovery_failed"
        ]
        self.assertEqual(len(errors), 3)
        self.assertEqual(errors[0].kwargs["error"], "TimeoutError")

    def test_failed_stop_release_is_not_reclassified_as_business_failure(self):
        store = Mock()
        store.retry_followup.side_effect = TimeoutError("synthetic")
        consumer = FollowupConsumer(
            store=store, consumer_kind="postprocess", owner="owner", handlers={}, audit=Mock()
        )
        consumer.request_stop()
        item = SimpleNamespace(id="f", attempt=1)
        with self.assertRaises(TimeoutError):
            consumer._execute(item)
        self.assertEqual(store.retry_followup.call_count, 1)
        self.assertTrue(store.retry_followup.call_args.kwargs["stopped"])

    def test_deadline_is_not_blocked_by_recovery_and_no_later_claim_occurs(self):
        entered, release = threading.Event(), threading.Event()
        store = Mock()

        def recover(**kwargs):
            entered.set()
            release.wait(2)

        store.recover_expired.side_effect = recover
        consumer = FollowupConsumer(
            store=store, consumer_kind="postprocess", owner="owner", handlers={}, audit=Mock()
        )
        lifecycle = BackgroundLifecycle(budget_seconds=0.05)
        lifecycle.register(consumer)
        consumer.start()
        try:
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            report = lifecycle.drain_until()
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertEqual(report.still_running, 1)
            self.assertTrue(consumer._stop.is_set())
            store.claim_followup.assert_not_called()
        finally:
            release.set()
            consumer.drain_until(time.monotonic() + 2)
        store.claim_followup.assert_not_called()
