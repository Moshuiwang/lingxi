"""真实库核验终止后管理卡恢复及确认卡外发前的租约。"""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

from test_admin_followup_postgres import DSN, FollowupPostgresTests
from test_innertest_postgres import InnertestPostgresTests

from lingxi.adapters.innertest_confirmation_card import InnertestConfirmationCard
from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_admin_followup import PostgresFollowupStore
from lingxi.core.admin.followup_consumer import FollowupConsumer


class ManagementFailureRecoveryTests(FollowupPostgresTests):
    def context(self, state="dispatching"):
        with connect(DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO management_card_context(message_id,card_id,identifier,chat_id,"
                "initiated_by_open_id,snapshot_fingerprint,context_deadline_at,state,dispatch_status) "
                "VALUES('msg','card','identifier','chat','admin','fingerprint',now()+interval '1 hour',%s,'publishing')",
                (state,),
            )
            cursor.execute(
                "UPDATE pending_action SET origin_card_message_id='msg' WHERE id='pac_test'"
            )

    def state(self):
        with connect(DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT state,dispatch_status,needs_refresh,card_sequence FROM management_card_context"
            )
            return cursor.fetchone()

    def test_eighth_failure_is_recovered_to_incomplete_and_refresh_is_idempotent(self):
        self.context()
        self.add()
        item = self.claim()
        with connect(DSN) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE admin_action_followup SET failure_count=7")
        self.store.retry_followup(
            id=item.id,
            owner=item.lease_owner,
            attempt=item.attempt,
            now=datetime.now(UTC),
            result_code="synthetic_failure",
        )
        self.assertEqual(self.state()[0], "dispatching")
        restarted = PostgresFollowupStore(DSN)
        restarted.recover_expired(now=datetime.now(UTC))
        self.assertEqual(self.state()[:3], ("incomplete", "incomplete", True))
        before = self.state()
        restarted.recover_expired(now=datetime.now(UTC))
        self.assertEqual(self.state(), before)

    def test_skipped_stage_recovers_card_after_reporter_crash(self):
        self.context()
        self.add()
        item = self.claim()
        self.store.complete_followup(
            id=item.id,
            owner=item.lease_owner,
            attempt=item.attempt,
            status="skipped",
            result_code="missing_roster_snapshot",
        )
        PostgresFollowupStore(DSN).recover_expired(now=datetime.now(UTC))
        self.assertEqual(self.state()[:3], ("incomplete", "incomplete", True))

    def test_old_failure_never_closes_new_action_or_closed_card(self):
        self.context()
        self.add()
        item = self.claim()
        self.store.complete_followup(
            id=item.id,
            owner=item.lease_owner,
            attempt=item.attempt,
            status="failed",
            result_code="synthetic_failure",
        )
        with connect(DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE pending_action SET status='executed',decided_at=now() WHERE id='pac_test'"
            )
            cursor.execute(
                "INSERT INTO pending_action(id,action_type,target_open_id,initiated_by_open_id,"
                "target_state_snapshot,status,created_at,confirm_deadline_at,origin_card_message_id) "
                "VALUES('pac_new','suspend_user','ou_synthetic','ou_admin','enabled','pending',"
                "now()+interval '1 second',now()+interval '1 hour','msg')"
            )
        self.store.recover_expired(now=datetime.now(UTC))
        self.assertEqual(self.state()[0], "dispatching")
        with connect(DSN) as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM pending_action WHERE id='pac_new'")
            cursor.execute("UPDATE management_card_context SET state='closed'")
        self.store.recover_expired(now=datetime.now(UTC))
        self.assertEqual(self.state()[0], "closed")


class ConfirmationLeasePostgresTests(InnertestPostgresTests):
    def test_expired_or_replaced_claim_after_create_never_sends_or_reclaims_unknown(self):
        for boundary in ("expired", "replaced"):
            with self.subTest(boundary=boundary):
                self.setUp()
                self.prepare()
                store = PostgresFollowupStore(DSN)
                sender = Mock(return_value="message")

                def create(_payload):
                    self.sql(
                        "UPDATE admin_action_followup SET lease_until=now()-interval '1 second'"
                    )
                    if boundary == "replaced":
                        self.sql(
                            "UPDATE admin_action_followup SET lease_owner='other',attempt=attempt+1"
                        )
                    return "card"

                handler = InnertestConfirmationCard(
                    service=self.service, store=store, create_card=create, send_card=sender
                )
                self.sql(
                    "UPDATE admin_action_followup SET next_attempt_at=now()-interval '1 second'"
                )
                consumer = FollowupConsumer(
                    store=store,
                    consumer_kind="gateway",
                    owner="original",
                    handlers={"confirmation_card_send": handler},
                    audit=self.audit,
                )
                consumer.run_once()
                sender.assert_not_called()
                store.recover_expired(now=datetime.now(UTC) + timedelta(seconds=121))
                self.assertEqual(
                    self.sql("SELECT status FROM admin_action_followup"), [("unknown",)]
                )
                self.assertFalse(consumer.run_once())
                sender.assert_not_called()
