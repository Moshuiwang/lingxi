"""原操作期限、停止交回和真实Gateway持久装配使用独占合成库。"""

import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import test_admin_followup_postgres as fixtures
from postgres_schema import ensure_production_schema, psycopg_available

from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_admin_followup import PostgresFollowupStore
from lingxi.adapters.postgres_admin_followup_projection import fetch_followups
from lingxi.core.admin.followup_consumer import FollowupConsumer
from lingxi.core.admin.followup_lifecycle import BackgroundLifecycle

DSN = fixtures.DSN


@unittest.skipUnless(DSN and psycopg_available(), "需独占合成PostgreSQL")
class OriginalOperationDeadlineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_production_schema(DSN)

    def setUp(self):
        self.fixture = fixtures.FollowupPostgresTests()
        self.fixture.setUp()
        self.store = self.fixture.store

    def test_new_observation_never_extends_original_ninety_days(self):
        expires = self.fixture.replace_action(datetime.now(UTC) - timedelta(days=90, minutes=1))
        parent = self.fixture.add()
        self.fixture.add("publish_observe", depends_on_id=parent.id)
        self.assertEqual(fetch_followups(DSN, trace_id="trc_synthetic"), ())
        # 即使恢复扫描完全不运行，领取口自身也拒绝已到原操作期限的工作。
        self.assertIsNone(self.fixture.claim())
        self.store.recover_expired(now=expires)
        self.assertEqual(self.store.list_for_action(pending_action_id="pac_test"), ())

    def test_exact_original_deadline_and_before_it_have_different_results(self):
        expires = self.fixture.replace_action(datetime.now(UTC) - timedelta(days=89))
        self.fixture.add()
        self.assertEqual(len(fetch_followups(DSN, trace_id="trc_synthetic")), 1)
        self.store.recover_expired(now=expires - timedelta(microseconds=1))
        self.assertEqual(len(self.store.list_for_action(pending_action_id="pac_test")), 1)
        self.store.recover_expired(now=expires)
        self.assertEqual(self.store.list_for_action(pending_action_id="pac_test"), ())

    def test_bounded_parent_delete_first_closes_late_dependency(self):
        expires = self.fixture.replace_action(datetime.now(UTC) - timedelta(days=90, minutes=1))
        parent = self.fixture.add()
        child = self.fixture.add("publish_observe", depends_on_id=parent.id)
        self.store.recover_expired(now=expires, limit=1)
        refs = self.store.list_for_action(pending_action_id="pac_test")
        self.assertEqual([(r.id, r.status) for r in refs], [(child.id, "skipped")])
        self.store.recover_expired(now=expires, limit=1)
        self.assertEqual(self.store.list_for_action(pending_action_id="pac_test"), ())

    def test_stop_during_real_claim_returns_it_without_effect_or_failure(self):
        self.fixture.add("group_notify")
        with connect(DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE admin_action_followup SET next_attempt_at=now()-interval '5 seconds'"
            )
        entered, release = threading.Event(), threading.Event()
        outer = self

        class HeldClaimStore(PostgresFollowupStore):
            def claim_followup(self, **kwargs):
                item = super().claim_followup(**kwargs)
                if item is not None:
                    entered.set()
                    if not release.wait(2):
                        raise TimeoutError("synthetic response barrier")
                return item

        store = HeldClaimStore(DSN)
        send = Mock()
        consumer = FollowupConsumer(
            store=store,
            consumer_kind="postprocess",
            owner="owner",
            handlers={"group_notify": send},
            audit=Mock(),
        )
        lifecycle = BackgroundLifecycle(budget_seconds=0.05)
        lifecycle.register(consumer)
        consumer.start()
        try:
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            self.assertEqual(lifecycle.drain_until().still_running, 1)
            self.assertLess(time.monotonic() - started, 0.2)
        finally:
            release.set()
            consumer.drain_until(time.monotonic() + 2)
        send.assert_not_called()
        with connect(DSN) as c, c.cursor() as cur:
            cur.execute(
                "SELECT status,failure_count,effect_started_at,lease_owner FROM admin_action_followup"
            )
            self.assertEqual(cur.fetchone(), ("retry_wait", 0, None, None))
        self.assertIsNotNone(outer.fixture.claim(owner="new-owner"))

    def test_real_gateway_config_builds_durable_stack_and_cleans_threads(self):
        from lingxi.apps.gateway.assembly import _build_admin_stack
        from lingxi.apps.gateway.config import GatewayConfig
        from lingxi.core.admin.followup_renewal import FollowupLeaseKeeper

        config = GatewayConfig(app_id="synthetic", app_secret="synthetic", postgres_dsn=DSN)
        lifecycle = BackgroundLifecycle(budget_seconds=2)
        client = Mock()
        try:
            stack = _build_admin_stack(config, audit=Mock(), client=client, lifecycle=lifecycle)
            self.assertTrue(stack.card_callback._pending_actions.durable_followups)
            consumers = [c for c in lifecycle._objects if isinstance(c, FollowupConsumer)]
            self.assertEqual({c.kind for c in consumers}, {"postprocess", "recompute", "observe"})
            self.assertTrue(all(c._thread.is_alive() for c in consumers))
            self.assertEqual(sum(isinstance(c, FollowupLeaseKeeper) for c in lifecycle._objects), 1)
            client.assert_not_called()
        finally:
            self.assertEqual(lifecycle.drain_until().still_running, 0)
