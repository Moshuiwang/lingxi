"""持久阶段用真实 PostgreSQL 验证事务、领取竞争和未知不重发。"""

import os
import threading
import unittest
from datetime import UTC, datetime, timedelta

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_admin_followup import PostgresFollowupStore, enqueue_followups
from lingxi.core.admin.followup import FollowupSpec

DSN = os.environ.get("LINGXI_POSTGRES_DSN")


@unittest.skipUnless(DSN and psycopg_available(), "需要独占合成 PostgreSQL")
class FollowupPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_production_schema(DSN)

    def setUp(self):
        reset_production_rows(DSN)
        self.store = PostgresFollowupStore(DSN)
        self.now = datetime.now(UTC)
        with connect(DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pending_action(id,action_type,target_open_id,initiated_by_open_id,"
                "target_state_snapshot,status,created_at,confirm_deadline_at) VALUES "
                "('pac_test','suspend_user','ou_synthetic','ou_admin','enabled','pending',now(),now()+interval '1 hour')"
            )

    def add(self, stage="permission_recompute", **kwargs):
        with connect(DSN) as conn:
            return enqueue_followups(
                conn,
                pending_action_id="pac_test",
                trace_id="trc_synthetic",
                items=(
                    FollowupSpec(
                        subject_key=kwargs.pop("subject_key", "usr_synthetic"),
                        stage=stage,
                        **kwargs,
                    ),
                ),
            )[0]

    def claim(self, owner="one", now=None, consumer_kind="gateway"):
        return self.store.claim_followup(
            consumer_kind=consumer_kind,
            owner=owner,
            now=now or (datetime.now(UTC) + timedelta(seconds=1)),
        )

    def test_transaction_failure_rolls_back_business_and_all_stages(self):
        with self.assertRaises(RuntimeError):
            with connect(DSN) as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE pending_action SET reason='synthetic-change' WHERE id='pac_test'"
                )
                enqueue_followups(
                    conn,
                    pending_action_id="pac_test",
                    trace_id="trc_test",
                    items=(FollowupSpec(subject_key="s", stage="permission_recompute"),),
                )
                raise RuntimeError("kill-before-commit")
        self.assertEqual(self.store.list_for_action(pending_action_id="pac_test"), ())
        with connect(DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT reason FROM pending_action WHERE id='pac_test'")
            self.assertIsNone(cur.fetchone()[0])

    def test_two_consumers_and_old_owner_cannot_complete(self):
        self.add()
        barrier = threading.Barrier(2)
        results = []

        def claim(owner):
            barrier.wait()
            results.append(self.claim(owner))

        threads = [threading.Thread(target=claim, args=(owner,)) for owner in ("one", "two")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        live = [item for item in results if item]
        self.assertEqual(len(live), 1)
        old = live[0]
        later = datetime.now(UTC) + timedelta(seconds=121)
        self.assertEqual(self.store.recover_expired(now=later).recoverable, 1)
        new = self.claim("new", later)
        self.assertEqual(new.attempt, old.attempt + 1)
        self.assertFalse(
            self.store.complete_followup(
                id=old.id,
                owner=old.lease_owner,
                attempt=old.attempt,
                status="succeeded",
                result_code="late",
            )
        )
        self.assertTrue(
            self.store.complete_followup(
                id=new.id,
                owner="new",
                attempt=new.attempt,
                status="succeeded",
                result_code="completed",
            )
        )
        self.assertEqual(self.add().status, "succeeded")

    def test_unknown_external_effect_is_never_reclaimed(self):
        self.add("group_notify")
        item = self.claim()
        self.assertTrue(
            self.store.mark_effect_started(
                id=item.id, owner="one", attempt=item.attempt, now=datetime.now(UTC)
            )
        )
        later = datetime.now(UTC) + timedelta(seconds=121)
        self.assertEqual(self.store.recover_expired(now=later).unknown, 1)
        self.assertIsNone(self.claim("two", later))
        self.assertEqual(
            self.store.list_for_action(pending_action_id="pac_test")[0].status, "unknown"
        )

    def test_null_first_user_and_unknown_contract(self):
        ref = self.add("innertest_preprovision", subject_key="item_test", batch_item_id="item_test")
        item = self.claim(consumer_kind="scheduler")
        self.assertIsNone(item.target_user_id)
        self.assertEqual(item.id, ref.id)
        with connect(DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE admin_action_followup SET contract_version=2,status='pending' WHERE id=%s",
                (ref.id,),
            )
        self.assertIsNone(self.claim(consumer_kind="scheduler"))

    def test_dependency_only_succeeded_and_retry_limit(self):
        ref = self.add()
        self.add("management_card_refresh", depends_on_id=ref.id)
        item = self.claim()
        self.store.complete_followup(
            id=item.id, owner="one", attempt=item.attempt, status="unknown", result_code="unknown"
        )
        self.store.recover_expired(now=datetime.now(UTC))
        self.assertIsNone(self.claim())
        self.assertEqual(
            {r.status for r in self.store.list_for_action(pending_action_id="pac_test")},
            {"unknown", "skipped"},
        )

    def test_eight_failures_are_terminal(self):
        self.add()
        moment = datetime.now(UTC) + timedelta(seconds=1)
        for _ in range(8):
            item = self.claim(now=moment)
            self.assertIsNotNone(item)
            self.store.retry_followup(
                id=item.id,
                owner="one",
                attempt=item.attempt,
                now=moment,
                result_code="synthetic-failure",
            )
            moment += timedelta(seconds=301)
        self.assertEqual(
            self.store.list_for_action(pending_action_id="pac_test")[0].status, "failed"
        )
        self.assertIsNone(self.claim(now=moment))
