"""唯一持久阶段与真实数据库的隔离故障、版本与未知发送验证。"""

from unittest.mock import Mock

from test_innertest_postgres import DSN, InnertestPostgresTests

from lingxi.adapters.innertest_confirmation_card import InnertestConfirmationCard
from lingxi.adapters.innertest_handlers import InnertestFollowupHandlers
from lingxi.adapters.innertest_outreach import CheckedInnertestSender
from lingxi.adapters.postgres_admin_followup import PostgresFollowupStore
from lingxi.adapters.postgres_innertest_roster import (
    PostgresInnertestRoster,
    compare_legacy_sources,
)
from lingxi.adapters.postgres_outreach import PostgresOutreachStore
from lingxi.core.admin.followup_consumer import FollowupConsumer
from lingxi.core.admin.innertest import InnertestError


class InnertestStageTests(InnertestPostgresTests):
    def user(self, number="person1"):
        self.sql(
            "INSERT INTO app_user(id,feishu_open_id,feishu_user_id,feishu_union_id,display_name,"
            "department,tenant_key,provisioning_state,permission_version) VALUES "
            "(%s,%s,%s,%s,'合成','合成','synthetic','active',1)",
            ("usr_" + number, "ou_" + number, "fs_" + number, "un_" + number),
        )
        self.sql(
            "INSERT INTO publish_outbox(id,user_id,permission_version,reason,payload,status,published_at) "
            "VALUES(%s,%s,1,'synthetic','{}','published',now())",
            ("pub_" + number, "usr_" + number),
        )

    def consumer(self, handler, kind="scheduler"):
        self.sql("UPDATE admin_action_followup SET next_attempt_at=now()-interval '2 seconds'")
        stages = (
            ("innertest_preprovision", "innertest_readiness_check")
            if kind == "scheduler"
            else ("confirmation_card_send",)
        )
        return FollowupConsumer(
            store=PostgresFollowupStore(DSN),
            consumer_kind=kind,
            owner="synthetic-owner",
            handlers={s: handler for s in stages},
            audit=self.audit,
        )

    def test_real_stage_preprovision_and_user_probe_no_local_grant(self):
        batch = self.prepare()
        self.delivered(batch)
        self.confirm(batch)
        outer = self

        class Runner:
            def start_system(self, *, email, trace_id, initiated_by_open_id):
                outer.assertEqual(initiated_by_open_id, "ou_admin")
                outer.user()
                return Mock(failure_reason=None)

        probe = Mock()
        probe.list_metrics.return_value = 2
        handlers = InnertestFollowupHandlers(
            store=PostgresFollowupStore(DSN), runner=Runner(), probe=probe
        )
        consumer = self.consumer(handlers.handle)
        self.assertTrue(consumer.run_once())
        self.assertTrue(consumer.run_once())
        self.assertFalse(consumer.run_once())
        probe.list_metrics.assert_called_once_with(user_id="usr_person1")
        self.assertEqual(self.sql("SELECT result_code FROM innertest_check"), [("check_passed",)])
        self.assertEqual(self.sql("SELECT count(*) FROM local_permission_override"), [(0,)])
        self.assertEqual(self.sql("SELECT count(*) FROM inbound_event"), [(0,)])
        self.assertEqual(self.sql("SELECT count(*) FROM outreach_message"), [(0,)])
        self.assertEqual(
            self.sql(
                "SELECT DISTINCT target_user_id FROM admin_action_followup WHERE stage LIKE 'innertest_%%'"
            ),
            [("usr_person1",)],
        )

    def test_prepare_card_success_same_transaction_and_unknown_zero_resend(self):
        self.prepare()
        store = PostgresFollowupStore(DSN)
        send = Mock(return_value="message")
        handler = InnertestConfirmationCard(
            service=self.service, store=store, create_card=Mock(return_value="card"), send_card=send
        )
        consumer = self.consumer(handler, "gateway")
        self.assertTrue(consumer.run_once())
        self.assertFalse(consumer.run_once())
        self.assertEqual(self.sql("SELECT card_delivered FROM pending_action"), [(True,)])
        self.assertEqual(self.sql("SELECT status FROM admin_action_followup"), [("succeeded",)])
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_membership"), [(0,)])

    def test_unknown_card_crash_window_never_replayed(self):
        batch = self.prepare()
        send = Mock(side_effect=TimeoutError("synthetic"))
        store = PostgresFollowupStore(DSN)
        handler = InnertestConfirmationCard(
            service=self.service, store=store, create_card=Mock(return_value="card"), send_card=send
        )
        consumer = self.consumer(handler, "gateway")
        self.assertTrue(consumer.run_once())
        self.assertFalse(consumer.run_once())
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.sql("SELECT status FROM admin_action_followup"), [("unknown",)])
        self.assertFalse(self.confirm(batch).decision.ok)
        self.assertEqual(self.prepare()["batch_id"], batch["batch_id"])
        self.assertEqual(send.call_count, 1)

    def test_role_revoked_before_card_zero_send(self):
        self.prepare()
        send = Mock()
        self.sql("UPDATE innertest_admin_binding SET enabled=false")
        handler = InnertestConfirmationCard(
            service=self.service,
            store=PostgresFollowupStore(DSN),
            create_card=Mock(),
            send_card=send,
        )
        self.consumer(handler, "gateway").run_once()
        send.assert_not_called()
        self.assertEqual(self.sql("SELECT status FROM admin_action_followup"), [("skipped",)])

    def test_check_permissions_changed_after_probe_blocks_usable_result(self):
        self.user()
        batch = self.prepare()
        self.delivered(batch)
        self.confirm(batch)

        def drift(**_kwargs):
            self.sql("UPDATE app_user SET permission_version=2")
            return 2

        probe = Mock()
        probe.list_metrics.side_effect = drift
        handlers = InnertestFollowupHandlers(
            store=PostgresFollowupStore(DSN), runner=Mock(), probe=probe
        )
        c = self.consumer(handlers.handle)
        c.run_once()
        c.run_once()
        self.assertEqual(
            self.sql("SELECT result_code FROM innertest_check"), [("check_version_changed",)]
        )
        self.assertEqual(self.sql("SELECT count(*) FROM outreach_message"), [(0,)])

    def test_notification_failure_version_change_and_unknown_are_guarded(self):
        self.user()
        store = PostgresOutreachStore(DSN)

        def reserve():
            return store.reserve(
                recipient_open_id="ou_person1",
                user_id="usr_person1",
                purpose="apply",
                dedupe_key="welcome:apply:usr_person1",
                content_key="welcome",
                content_version="1",
                card_style="concise",
            )

        reserve()
        sender = Mock()
        probe = Mock()
        probe.list_metrics.return_value = 0
        guarded = CheckedInnertestSender(
            dsn=DSN, sender=sender, probe=probe, initiated_by="ou_admin"
        )
        with self.assertRaisesRegex(InnertestError, "check_failed"):
            guarded.send_card(
                open_id="ou_person1",
                card={"synthetic": True},
                dedupe_key="welcome:apply:usr_person1",
            )
        sender.send_card.assert_not_called()
        probe.list_metrics.return_value = 2
        sender.send_card.side_effect = TimeoutError()
        with self.assertRaisesRegex(InnertestError, "notification_unknown"):
            guarded.send_card(
                open_id="ou_person1",
                card={"synthetic": True},
                dedupe_key="welcome:apply:usr_person1",
            )
        self.assertEqual(reserve().status, "unknown")
        with self.assertRaisesRegex(InnertestError, "notification_unknown"):
            guarded.send_card(
                open_id="ou_person1",
                card={"synthetic": True},
                dedupe_key="welcome:apply:usr_person1",
            )
        self.assertEqual(sender.send_card.call_count, 1)

    def test_legacy_switch_restore_and_db_failure_not_empty_roster(self):
        roster = PostgresInnertestRoster(DSN, scope="synthetic", legacy=frozenset({"ou_old"}))
        self.sql("UPDATE innertest_roster_version SET mode='legacy'")
        self.assertEqual(roster.snapshot("ou_old"), (0, True))
        with self.assertRaises(InnertestError):
            compare_legacy_sources({"ou_old"}, {"ou_other"})
        self.sql("UPDATE innertest_roster_version SET mode='database'")
        batch = self.prepare()
        self.delivered(batch)
        self.confirm(batch)
        self.assertEqual(roster.snapshot("ou_person1"), (1, True))
        recovered = PostgresInnertestRoster(DSN, scope="synthetic", legacy=frozenset({"ou_old"}))
        self.assertEqual(recovered.snapshot("ou_person1"), (1, True))
        self.assertEqual(recovered.progress("ou_person1"), "onboarding.innertest_waiting")
        self.assertTrue(recovered.recovery_status()["requires_dynamic_roster"])

    def test_notification_version_guard_immediately_before_send(self):
        self.user()
        PostgresOutreachStore(DSN).reserve(
            recipient_open_id="ou_person1",
            user_id="usr_person1",
            purpose="apply",
            dedupe_key="guard:apply:usr_person1",
            content_key="guard",
            content_version="1",
            card_style="concise",
        )
        sender = Mock()
        probe = Mock()
        probe.list_metrics.return_value = 1
        guard = CheckedInnertestSender(dsn=DSN, sender=sender, probe=probe, initiated_by="ou_admin")
        original = guard._authorized
        calls = []

        def authorization():
            original()
            calls.append(1)
            if len(calls) == 2:
                self.sql("UPDATE app_user SET permission_version=2")
                self.sql(
                    "INSERT INTO publish_outbox(id,user_id,permission_version,reason,payload,status,published_at) VALUES('pub_changed','usr_person1',2,'synthetic','{}','published',now())"
                )

        guard._authorized = authorization
        with self.assertRaisesRegex(InnertestError, "check_version_changed"):
            guard.send_card(
                open_id="ou_person1", card={"synthetic": True}, dedupe_key="guard:apply:usr_person1"
            )
        sender.send_card.assert_not_called()
        self.assertEqual(
            self.sql(
                "SELECT count(*) FROM innertest_check WHERE result_code='check_version_changed'"
            ),
            [(1,)],
        )

    def test_batch_history_expires_without_removing_qualification(self):
        from datetime import UTC, datetime, timedelta

        from lingxi.adapters.postgres import connect
        from lingxi.adapters.postgres_innertest_retention import purge_innertest_history

        batch = self.prepare()
        self.delivered(batch)
        self.confirm(batch)
        with connect(DSN) as connection:
            self.assertEqual(
                purge_innertest_history(
                    connection, now=datetime.now(UTC) + timedelta(days=91), limit=20
                ),
                1,
            )
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_batch"), [(0,)])
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_membership"), [(1,)])

    def test_deleted_user_does_not_leave_check_rows_or_block_existing_deletion(self):
        self.user()
        self.sql(
            "INSERT INTO innertest_check(id,user_id,permission_version,publish_version,started_at,finished_at,result_code,trace_id) VALUES('ich_delete','usr_person1',1,1,now(),now(),'check_passed','trc_synthetic')"
        )
        self.sql("DELETE FROM app_user WHERE id='usr_person1'")
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_check"), [(0,)])

    def test_late_check_retention_is_bound_to_original_batch(self):
        from datetime import UTC, datetime, timedelta

        from lingxi.adapters.postgres import connect
        from lingxi.adapters.postgres_innertest_retention import purge_innertest_history

        self.user()
        batch = self.prepare()
        self.delivered(batch)
        self.confirm(batch)
        now = datetime.now(UTC)
        self.sql(
            "INSERT INTO innertest_check(id,batch_item_id,user_id,permission_version,publish_version,started_at,finished_at,result_code,trace_id) VALUES('ich_late',%s,'usr_person1',1,1,%s,%s,'check_passed','trc_synthetic')",
            (batch["items"][0]["item_id"], now + timedelta(hours=23), now + timedelta(hours=23)),
        )
        with connect(DSN) as connection:
            purge_innertest_history(connection, now=now + timedelta(days=90, minutes=1), limit=20)
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_check"), [(0,)])
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_membership"), [(1,)])
