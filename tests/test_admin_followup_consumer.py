"""停止门和共同截止不依赖真实传输。"""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from lingxi.core.admin.followup import ShutdownReport
from lingxi.core.admin.followup_consumer import FollowupConsumer, FollowupResult
from lingxi.core.admin.followup_lifecycle import BackgroundLifecycle


class FollowupConsumerTests(unittest.TestCase):
    def consumer(self, store, handlers):
        return FollowupConsumer(
            store=store, consumer_kind="postprocess", owner="run", handlers=handlers, audit=Mock()
        )

    def test_stop_means_zero_claims(self):
        store = Mock()
        consumer = self.consumer(store, {})
        consumer.request_stop()
        consumer.request_stop()
        self.assertFalse(consumer.run_once())
        store.claim_followup.assert_not_called()
        store.recover_expired.assert_not_called()
        self.assertEqual(consumer.drain_until(0).still_running, 0)

    def test_failed_effect_mark_prevents_external_call(self):
        item = SimpleNamespace(id="f", stage="group_notify", attempt=1)
        store = Mock()
        store.claim_followup.return_value = item
        store.mark_effect_started.return_value = False
        send = Mock(return_value=FollowupResult())
        consumer = self.consumer(store, {"group_notify": send})
        consumer.run_once()
        send.assert_not_called()
        store.complete_followup.assert_not_called()

    def test_all_objects_share_first_absolute_deadline(self):
        clock = Mock(return_value=10)
        lifecycle = BackgroundLifecycle(budget_seconds=120, clock=clock)
        objects = [Mock(), Mock(), Mock()]
        for obj in objects:
            obj.drain_until.return_value = ShutdownReport()
            lifecycle.register(obj)
        lifecycle.request_stop()
        clock.return_value = 60
        lifecycle.request_stop()
        lifecycle.drain_until()
        for obj in objects:
            obj.drain_until.assert_called_once_with(130)
        with self.assertRaises(RuntimeError):
            lifecycle.register(Mock())
