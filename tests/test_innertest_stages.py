"""唯一持久阶段与真实数据库的隔离故障、版本与未知发送验证。"""

from unittest.mock import Mock, patch

from test_innertest_postgres import DSN, InnertestPostgresTests

from lingxi.adapters.innertest_confirmation_card import InnertestConfirmationCard
from lingxi.adapters.innertest_handlers import InnertestFollowupHandlers
from lingxi.adapters.innertest_outreach import CheckedInnertestSender
from lingxi.adapters.postgres_admin_followup import PostgresFollowupStore
from lingxi.adapters.postgres_innertest_confirmation import InnertestPendingActions
from lingxi.adapters.postgres_innertest_roster import (
    PostgresInnertestRoster,
    compare_legacy_sources,
)
from lingxi.adapters.postgres_outreach import PostgresOutreachStore
from lingxi.core.admin.followup_consumer import FollowupConsumer
from lingxi.core.admin.innertest import InnertestError
from lingxi.core.admin.pending_action import ConfirmResultKind


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

    def test_binding_version_changed_before_click_refuses_and_closes_out(self):
        """发起后绑定版本变了：明确拒绝、落终态、零资格。

        停在 ``pending`` 是不行的——卡片按钮还在，绑定若已转给他人，这批就再也
        没人能收口它。
        """
        batch = self.prepare()
        self.delivered(batch)
        self.sql("UPDATE innertest_admin_binding SET version=version+1")

        outcome = self.confirm(batch)

        self.assertFalse(outcome.decision.ok)
        self.assertIs(outcome.decision.kind, ConfirmResultKind.ROLE_REVOKED)
        self.assertIn("请重新发起", outcome.decision.message)
        self.assertEqual(
            self.sql("SELECT status,reason FROM pending_action"), [("failed", "role_revoked")]
        )
        self.assertEqual(self.sql("SELECT status FROM innertest_batch"), [("failed",)])
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_membership"), [(0,)])

    def test_unknown_guard_error_propagates_instead_of_being_called_authorization_changed(self):
        """收口只认授权类两个码；将来 ``_principal`` 若新增别的码，不得被顺手吞掉。

        ``_principal`` 今天只会抛这两个码，所以这条分支靠集成路径打不到；直接
        对着 ``_guarded_code`` 打，才能把「不认识的码要原样抛」这条钉住。
        """
        batch = self.prepare()
        self.delivered(batch)
        with patch.object(
            InnertestPendingActions, "_guard", side_effect=InnertestError("synthetic_unknown_code")
        ):
            with self.assertRaisesRegex(InnertestError, "synthetic_unknown_code"):
                self.confirm(batch)
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_membership"), [(0,)])

    def test_cancel_path_also_refuses_definitely_when_authorization_changed(self):
        """取消点击走同一条守卫，授权变化时同样要给确定结果。"""
        batch = self.prepare()
        self.delivered(batch)
        self.sql("UPDATE innertest_admin_binding SET version=version+1")

        outcome = self.pending.cancel(
            pending_action_id=batch["pending_action_id"], clicker_open_id="ou_admin"
        )

        self.assertFalse(outcome.decision.ok)
        self.assertIs(outcome.decision.kind, ConfirmResultKind.ROLE_REVOKED)
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_membership"), [(0,)])

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
                permission_snapshot=("usr_person1", 1, "pub_person1"),
                dedupe_key="welcome:apply:usr_person1",
            )
        sender.send_card.assert_not_called()
        probe.list_metrics.return_value = 2
        sender.send_card.side_effect = TimeoutError()
        with self.assertRaisesRegex(InnertestError, "notification_unknown"):
            guarded.send_card(
                open_id="ou_person1",
                card={"synthetic": True},
                permission_snapshot=("usr_person1", 1, "pub_person1"),
                dedupe_key="welcome:apply:usr_person1",
            )
        self.assertEqual(reserve().status, "unknown")
        with self.assertRaisesRegex(InnertestError, "notification_unknown"):
            guarded.send_card(
                open_id="ou_person1",
                card={"synthetic": True},
                permission_snapshot=("usr_person1", 1, "pub_person1"),
                dedupe_key="welcome:apply:usr_person1",
            )
        self.assertEqual(sender.send_card.call_count, 1)

    def test_prepared_card_permission_or_publish_change_before_first_probe_refuses(self):
        from dataclasses import replace

        from test_outreach_ops import PERMISSIONS, TOOL, _recipients

        from lingxi.adapters.postgres_outreach import PostgresOutreachSubjects
        from lingxi.core.outreach.dispatch import OutreachDispatcher, OutreachPurpose

        self.user()
        self.sql("UPDATE app_user SET email='person1@example.test'")
        self.sql(
            "UPDATE publish_outbox SET payload=jsonb_build_object('permissions', %s::text)",
            (PERMISSIONS,),
        )
        facts = PostgresOutreachSubjects(DSN).facts_for("person1@example.test")
        self.assertEqual((facts.permission_version, facts.publish_id), (1, "pub_person1"))
        recipients = _recipients(replace(facts, roster_names=("合成",)))
        self.assertTrue(recipients[0].plan.sendable)
        self.sql("UPDATE app_user SET permission_version=2")
        self.sql(
            "INSERT INTO publish_outbox(id,user_id,permission_version,reason,payload,status,published_at) VALUES('pub_new','usr_person1',2,'synthetic','{}','published',now())"
        )
        probe, sender = Mock(), Mock()
        probe.list_metrics.return_value = 1
        sender.send_card.return_value = "synthetic_message"
        guard = CheckedInnertestSender(dsn=DSN, sender=sender, probe=probe, initiated_by="ou_admin")
        dispatcher = OutreachDispatcher(
            sender=guard, store=PostgresOutreachStore(DSN), audit=self.audit
        )
        results = TOOL.run_outreach(
            recipients,
            dispatcher=dispatcher,
            purpose=OutreachPurpose.APPLY,
            admin_open_id=None,
            run_id="synthetic",
        )
        self.assertEqual(results[0].detail, "check_version_changed")
        probe.list_metrics.assert_not_called()
        sender.send_card.assert_not_called()
        self.assertEqual(
            self.sql("SELECT count(*) FROM outreach_message WHERE effect_started_at IS NOT NULL"),
            [(0,)],
        )

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
                open_id="ou_person1",
                card={"synthetic": True},
                permission_snapshot=("usr_person1", 1, "pub_person1"),
                dedupe_key="guard:apply:usr_person1",
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
