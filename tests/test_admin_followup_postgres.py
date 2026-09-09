"""持久阶段用真实 PostgreSQL 验证事务、领取竞争和未知不重发。"""

import multiprocessing
import os
import unittest
from datetime import UTC, datetime, timedelta

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_admin_followup import PostgresFollowupStore, enqueue_followups
from lingxi.core.admin.followup import FollowupSpec

DSN = os.environ.get("LINGXI_POSTGRES_DSN")


def _claim_in_process(dsn, owner, barrier, results):
    barrier.wait(timeout=5)
    item = PostgresFollowupStore(dsn).claim_followup(
        consumer_kind="gateway", owner=owner, now=datetime.now(UTC) + timedelta(seconds=1)
    )
    results.put(item)


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
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        result_queue = context.Queue()
        processes = [
            context.Process(target=_claim_in_process, args=(DSN, owner, barrier, result_queue))
            for owner in ("one", "two")
        ]
        try:
            for process in processes:
                process.start()
            results = [result_queue.get(timeout=10) for _ in processes]
            for process in processes:
                process.join(timeout=5)
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in processes:
                if process.is_alive():
                    process.kill()
                    process.join(timeout=5)
            result_queue.close()
            result_queue.join_thread()
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

    def test_real_confirmation_enqueues_atomically_and_preserves_trace(self):
        from test_pending_action_postgres import PendingActionPostgresTestCase

        from lingxi.adapters.postgres_pending_action import PostgresPendingActionStore

        fixture = PendingActionPostgresTestCase()
        fixture._dsn = DSN
        fixture.setUp()
        fixture.add_target_user()
        fixture.store = PostgresPendingActionStore(
            DSN,
            audit=fixture.audit,
            metric_map_path=None,
            durable_followups=True,
            notify_group=True,
        )
        action_id = fixture.prepare_and_deliver()
        result = fixture.store.confirm(
            pending_action_id=action_id,
            clicker_open_id="ou_pending_action_admin",
            trace_id="trc_real_confirm",
        )
        self.assertTrue(result.decision.ok)
        refs = self.store.list_for_action(pending_action_id=action_id)
        self.assertEqual(
            {ref.stage for ref in refs},
            {
                "permission_recompute",
                "group_notify",
                "terminal_card_refresh",
                "management_card_refresh",
            },
        )
        with connect(DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT trace_id FROM admin_action_followup")
            self.assertEqual(cur.fetchall(), [("trc_real_confirm",)])
        fixture.store.confirm(
            pending_action_id=action_id, clicker_open_id="ou_pending_action_admin"
        )
        self.assertEqual(self.store.list_for_action(pending_action_id=action_id), refs)

    def test_registration_failure_reverts_real_confirmation(self):
        from unittest.mock import patch

        from test_pending_action_postgres import PendingActionPostgresTestCase

        from lingxi.adapters.postgres_pending_action import PostgresPendingActionStore

        fixture = PendingActionPostgresTestCase()
        fixture._dsn = DSN
        fixture.setUp()
        fixture.add_target_user()
        fixture.store = PostgresPendingActionStore(
            DSN, audit=fixture.audit, metric_map_path=None, durable_followups=True
        )
        action_id = fixture.prepare_and_deliver()
        with patch(
            "lingxi.adapters.postgres_admin_followup_confirmation.enqueue_followups",
            side_effect=RuntimeError("synthetic-enqueue-failure"),
        ):
            with self.assertRaises(RuntimeError):
                fixture.store.confirm(
                    pending_action_id=action_id, clicker_open_id="ou_pending_action_admin"
                )
        self.assertEqual(fixture.current_account_state(), "enabled")
        self.assertEqual(fixture.store.get(pending_action_id=action_id).status.value, "pending")
        self.assertEqual(self.store.list_for_action(pending_action_id=action_id), ())

    def test_deployment_snapshot_rejects_unknown_version(self):
        ref = self.add()
        self.assertTrue(self.store.recovery_status()["compatible"])
        self.assertEqual(self.store.recovery_status()["recoverable"], 1)
        with connect(DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE admin_action_followup SET contract_version=2 WHERE id=%s", (ref.id,)
            )
        self.assertFalse(self.store.recovery_status()["compatible"])

    def test_trace_projection_exact_and_unknown_rendered(self):
        from lingxi.adapters.postgres_admin_followup_projection import fetch_followups
        from lingxi.core.admin.followup_render import render_followups

        self.add("group_notify")
        self.assertEqual(fetch_followups(DSN, trace_id="trc_other"), ())
        item = self.claim()
        self.store.complete_followup(
            id=item.id, owner="one", attempt=item.attempt, status="unknown", result_code="unknown"
        )
        views = fetch_followups(DSN, trace_id="trc_synthetic")
        self.assertEqual(len(views), 1)
        self.assertIn("结果待核实", render_followups(views))
        self.assertIn("trc_synthetic", render_followups(views))
        with connect(DSN) as conn, conn.cursor() as cur:
            cur.execute("UPDATE admin_action_followup SET created_at=now()-interval '91 days'")
        self.assertEqual(fetch_followups(DSN, trace_id="trc_synthetic"), ())

    def test_new_user_resolution_rejects_forged_identity_and_keeps_subject(self):
        ref = self.add("innertest_preprovision", subject_key="item", batch_item_id="item")
        item = self.claim(consumer_kind="scheduler")
        with connect(DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app_user(id,feishu_open_id,feishu_user_id,feishu_union_id,display_name,department,tenant_key,provisioning_state) "
                "VALUES ('usr_real','ou_verified','fs_verified','un_verified','化名新人','合成部门','tk_synthetic','active')"
            )
        self.assertFalse(
            self.store.resolve_target(
                id=ref.id,
                owner="one",
                attempt=item.attempt,
                target_user_id="usr_real",
                batch_identity_lookup=lambda connection, item_id: "ou_forged",
            )
        )
        self.assertTrue(
            self.store.resolve_target(
                id=ref.id,
                owner="one",
                attempt=item.attempt,
                target_user_id="usr_real",
                batch_identity_lookup=lambda connection, item_id: "ou_verified",
            )
        )
        self.assertFalse(
            self.store.resolve_target(
                id=ref.id,
                owner="old",
                attempt=item.attempt,
                target_user_id="usr_real",
                batch_identity_lookup=lambda connection, item_id: "ou_verified",
            )
        )
        with connect(DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id,subject_key,target_user_id,result_code FROM admin_action_followup"
            )
            self.assertEqual(cur.fetchone(), (ref.id, "item", "usr_real", "resolved_target"))

    def test_hundred_rows_and_capacity_rejection_roll_back_all(self):
        from lingxi.core.admin.followup import FollowupCapacityError

        with connect(DSN) as conn:
            refs = enqueue_followups(
                conn,
                pending_action_id="pac_test",
                trace_id="trc_synthetic",
                items=tuple(
                    FollowupSpec(subject_key=str(n), stage="permission_recompute")
                    for n in range(100)
                ),
            )
        self.assertEqual(len(refs), 100)
        with connect(DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO admin_action_followup(id,pending_action_id,subject_key,stage) "
                "SELECT 'f'||n,'pac_test','s'||n,'permission_recompute' FROM generate_series(1,9900) n"
            )
        with self.assertRaises(FollowupCapacityError):
            self.add(subject_key="over_capacity")
        self.assertEqual(self.store.recovery_status()["recoverable"], 10000)

    def test_retention_closes_dependents_without_replaying_old_actions(self):
        old = self.add("permission_recompute")
        self.add("publish_observe", depends_on_id=old.id)
        with connect(DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE admin_action_followup SET created_at=now()-interval '91 days' WHERE id=%s",
                (old.id,),
            )
        self.store.recover_expired(now=datetime.now(UTC) + timedelta(seconds=1))
        refs = self.store.list_for_action(pending_action_id="pac_test")
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].status, "skipped")
        self.assertIsNone(self.claim())

    def test_observer_rejects_superseded_version(self):
        with connect(DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app_user(id,feishu_open_id,feishu_user_id,feishu_union_id,"
                "display_name,department,tenant_key,provisioning_state,permission_version) VALUES "
                "('usr_p','ou_p','fs_p','un_p','化名','合成部门','tk_synthetic','active',2)"
            )
            cur.execute(
                "INSERT INTO publish_outbox(id,user_id,permission_version,reason,payload,"
                "status,published_at) VALUES ('pub_old','usr_p',1,'synthetic','{}','published',now())"
            )
        ref = self.add()
        item = self.claim()
        self.store.complete_followup(
            id=item.id,
            owner="one",
            attempt=item.attempt,
            status="succeeded",
            result_code="queued",
            external_ref="pub_old",
            next_items=(
                FollowupSpec(
                    subject_key="usr_synthetic",
                    stage="publish_observe",
                    depends_on_id=ref.id,
                    target_user_id="usr_p",
                    target_version=1,
                ),
            ),
        )
        observe = self.claim()
        self.assertEqual(self.store.dependency_publish_state(observe), "superseded")
        self.assertIsNone(self.store.current_publish_reference(target_user_id="usr_p"))

    def test_process_death_before_commit_and_after_commit_have_different_recovery(self):
        import subprocess
        import sys

        script = """
import os,sys
from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_admin_followup import enqueue_followups
from lingxi.core.admin.followup import FollowupSpec
with connect(os.environ['LINGXI_POSTGRES_DSN']) as connection:
    with connection.cursor() as cursor:
        cursor.execute("UPDATE pending_action SET reason='process-committed' WHERE id='pac_test'")
    enqueue_followups(connection,pending_action_id='pac_test',trace_id='trc_process',
                      items=(FollowupSpec(subject_key='process',stage='permission_recompute'),))
    if sys.argv[1]=='commit': connection.commit()
    print('ready',flush=True)
    sys.stdin.readline()
"""
        for mode in ("rollback", "commit"):
            process = subprocess.Popen(
                [sys.executable, "-c", script, mode],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(process.stdout.readline().strip(), "ready")
            finally:
                process.kill()
                process.communicate(timeout=5)
            refs = self.store.list_for_action(pending_action_id="pac_test")
            self.assertEqual(len(refs), int(mode == "commit"))
        from unittest.mock import Mock

        from lingxi.core.admin.followup_consumer import FollowupConsumer, FollowupResult

        with connect(DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE admin_action_followup SET next_attempt_at=now()-interval '1 second'"
            )
        consumer = FollowupConsumer(
            store=self.store,
            consumer_kind="recompute",
            owner="recovery-v1",
            handlers={"permission_recompute": lambda item: FollowupResult()},
            audit=Mock(),
        )
        self.assertTrue(consumer.run_once())
        self.assertEqual(
            self.store.list_for_action(pending_action_id="pac_test")[0].status, "succeeded"
        )
        consumer.request_stop()
        self.assertFalse(consumer.run_once())

    def test_recovery_uses_current_account_not_historical_suspend(self):
        from unittest.mock import Mock

        from test_pending_action_postgres import PendingActionPostgresTestCase

        from lingxi.adapters.admin_followup_recompute import CurrentPermissionRecompute
        from lingxi.core.admin.pending_action import PendingActionType

        fixture = PendingActionPostgresTestCase()
        fixture._dsn = DSN
        fixture.setUp()
        fixture.add_target_user()
        action_id = fixture.prepare_and_deliver()
        pending = fixture.store.get(pending_action_id=action_id)
        self.assertEqual(pending.action_type, PendingActionType.SUSPEND_USER)
        delegate = Mock()
        recovery = CurrentPermissionRecompute(delegate, DSN)
        recovery.trigger(pending)
        self.assertEqual(
            delegate.trigger.call_args.args[0].action_type, PendingActionType.RESUME_USER
        )
        with connect(DSN) as conn, conn.cursor() as cur:
            cur.execute("UPDATE app_user SET account_state='suspended'")
        recovery.trigger(pending)
        self.assertEqual(
            delegate.trigger.call_args.args[0].action_type, PendingActionType.SUSPEND_USER
        )

    def test_real_consumer_never_blindly_resends_uncertain_notification(self):
        from unittest.mock import Mock

        from lingxi.core.admin.followup_consumer import FollowupConsumer

        self.add("group_notify")
        with connect(DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE admin_action_followup SET next_attempt_at=now()-interval '1 second'"
            )
        calls = []

        def uncertain(item):
            calls.append(item.id)
            raise TimeoutError("synthetic-receipt-missing")

        consumer = FollowupConsumer(
            store=self.store,
            consumer_kind="postprocess",
            owner="send-owner",
            handlers={"group_notify": uncertain},
            audit=Mock(),
        )
        self.assertTrue(consumer.run_once())
        self.assertEqual(
            self.store.list_for_action(pending_action_id="pac_test")[0].status, "unknown"
        )
        for _ in range(3):
            self.assertFalse(consumer.run_once())
        self.assertEqual(len(calls), 1)
        consumer.request_stop()

    def test_late_state_update_cannot_overwrite_reused_card(self):
        from test_management_card_publish_association_postgres import (
            MESSAGE_ID,
            ManagementCardReuseAssociationTestCase,
        )

        fixture = ManagementCardReuseAssociationTestCase()
        fixture._dsn = DSN
        fixture.setUp()
        old = fixture.seed_executed_action(
            created_at=fixture.now - timedelta(minutes=2),
            decided_at=fixture.now - timedelta(minutes=1),
        )
        new = fixture.seed_waiting_action(created_at=fixture.now)
        fixture.store.update_state(message_id=MESSAGE_ID, state="submitted")
        self.assertIsNone(
            fixture.store.update_state(
                message_id=MESSAGE_ID, state="effective", expected_action_id=old
            )
        )
        self.assertEqual(fixture.store.lookup_context(message_id=MESSAGE_ID).state, "submitted")
        self.assertIsNotNone(
            fixture.store.update_state(
                message_id=MESSAGE_ID, state="dispatching", expected_action_id=new
            )
        )
