"""真实短事务验证延后解析、资格原子提交和中断后的身份固定；平台读取用合成状态。"""

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import test_innertest_target_drift as fixture_module
from postgres_schema import ensure_production_schema, psycopg_available
from test_innertest_postgres import DSN
from test_onboarding_runner import EMPLOYED, FROZEN, build_runner

from lingxi.adapters.innertest_handlers import InnertestFollowupHandlers
from lingxi.adapters.postgres_admin_followup import PostgresFollowupStore
from lingxi.adapters.postgres_email_binding import PostgresEmailBindingSource
from lingxi.adapters.postgres_email_identity import RosterRows
from lingxi.adapters.postgres_identity import PostgresOrgSnapshotStore
from lingxi.adapters.postgres_roster_snapshot import PostgresRosterSnapshotStore


@unittest.skipUnless(DSN and psycopg_available(), "需要独占合成 PostgreSQL")
class DeferredEmailIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_production_schema(DSN)

    def setUp(self):
        self.fixture = fixture_module.InnertestTargetDriftTests()
        self.fixture.setUp()
        self.fixture.install_directory()
        self.sql = self.fixture.sql
        self.sql(
            "UPDATE roster_snapshot_row SET email='person1@example.test',employee_no=personnel_id"
        )
        self.statuses = {"ou_person1": FROZEN, "ou_person2": EMPLOYED}
        employment = SimpleNamespace(status=lambda tenant_key, open_id: self.statuses[open_id])
        self.runner, self.parts = build_runner(
            roster=RosterRows(PostgresRosterSnapshotStore(DSN)),
            directory=PostgresOrgSnapshotStore(DSN),
            employment=employment,
            email_bindings=PostgresEmailBindingSource(DSN),
        )
        self.store = PostgresFollowupStore(DSN)

    def claimed(self):
        batch = self.fixture.prepare()
        self.assertEqual(batch["items"][0]["membership"], "identity_resolution_pending")
        prepared = [
            call.kwargs
            for call in self.fixture.audit.record.call_args_list
            if call.args == ("innertest.identity_prepared",)
        ]
        self.assertEqual(len(prepared), 1)
        self.assertEqual(prepared[0]["snapshot_version"], "roster")
        self.assertEqual(prepared[0]["candidate_count"], 2)
        self.assertIsNone(prepared[0]["active_candidate_count"])
        self.assertEqual(prepared[0]["identity_reason"], "execution_resolution_pending")
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_membership"), [(0,)])
        self.fixture.delivered(batch)
        self.fixture.confirm(batch)
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_membership"), [(0,)])
        self.sql("UPDATE admin_action_followup SET next_attempt_at=now()-interval '1 second'")
        return self.store.claim_followup(
            consumer_kind="scheduler", owner="identity-test", now=datetime.now(UTC)
        )

    def handler(self, *, after_resolve=None):
        def resolve(**kwargs):
            target = self.runner.resolve_system_email(**kwargs)
            if after_resolve:
                after_resolve()
            return target

        proxy = SimpleNamespace(
            resolve_system_email=Mock(side_effect=resolve),
            start_system=Mock(side_effect=RuntimeError("synthetic interruption before onboarding")),
        )
        return InnertestFollowupHandlers(
            store=self.store,
            runner=proxy,
            probe=Mock(),
            identity_service=self.fixture.service,
        ), proxy

    def test_confirmed_deferred_item_persists_live_choice_and_replay_keeps_same_subject(self):
        item = self.claimed()
        handler, proxy = self.handler()
        with self.assertRaisesRegex(RuntimeError, "synthetic interruption"):
            handler.handle(item)
        self.assertEqual(
            self.sql("SELECT open_id,email FROM innertest_membership"),
            [("ou_person2", "person1@example.test")],
        )
        self.assertEqual(
            self.sql("SELECT open_id,personnel_id,result_code FROM innertest_batch_item"),
            [("ou_person2", "person2", "added")],
        )
        version = self.sql("SELECT version FROM innertest_roster_version")
        self.statuses = {"ou_person1": EMPLOYED, "ou_person2": FROZEN}
        with self.assertRaisesRegex(RuntimeError, "synthetic interruption"):
            handler.handle(item)
        self.assertEqual(proxy.resolve_system_email.call_count, 1)
        self.assertEqual(proxy.start_system.call_count, 2)
        self.assertTrue(
            all(
                call.kwargs["expected_open_id"] == "ou_person2"
                for call in proxy.start_system.call_args_list
            )
        )
        self.assertEqual(self.sql("SELECT version FROM innertest_roster_version"), version)
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_membership"), [(1,)])

    def test_two_active_or_unknown_candidates_never_write_membership(self):
        for second in (EMPLOYED, None):
            with self.subTest(second=second):
                self.setUp()
                item = self.claimed()
                self.statuses = {"ou_person1": EMPLOYED, "ou_person2": second}
                handler, proxy = self.handler()
                result = handler.handle(item)
                self.assertEqual(result.status, "failed")
                self.assertIn(
                    result.result_code,
                    {"email_identity_active_conflict", "email_identity_unavailable"},
                )
                self.assertEqual(self.sql("SELECT count(*) FROM innertest_membership"), [(0,)])
                self.assertEqual(
                    self.sql("SELECT result_code FROM innertest_batch_item"),
                    [(result.result_code,)],
                )
                self.assertEqual(handler.handle(item).result_code, result.result_code)
                self.assertEqual(proxy.resolve_system_email.call_count, 1)
                proxy.start_system.assert_not_called()

    def test_lease_lost_before_write_cannot_add_membership(self):
        item = self.claimed()
        handler, proxy = self.handler(
            after_resolve=lambda: self.sql(
                "UPDATE admin_action_followup SET lease_owner='new-owner' WHERE id=%s", (item.id,)
            )
        )
        result = handler.handle(item)
        self.assertEqual(result.result_code, "lease_lost")
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_membership"), [(0,)])
        proxy.start_system.assert_not_called()

    def test_admin_binding_revocation_before_write_cannot_add_membership(self):
        item = self.claimed()
        handler, proxy = self.handler(
            after_resolve=lambda: self.sql("UPDATE innertest_admin_binding SET enabled=false")
        )
        result = handler.handle(item)
        self.assertEqual(result.result_code, "binding_disabled")
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_membership"), [(0,)])
        proxy.start_system.assert_not_called()

    def test_snapshot_change_during_live_read_cannot_add_membership(self):
        item = self.claimed()
        handler, proxy = self.handler(
            after_resolve=lambda: self.sql("UPDATE roster_snapshot SET captured_at=now()")
        )
        result = handler.handle(item)
        self.assertEqual(result.result_code, "identity_snapshot_changed")
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_membership"), [(0,)])
        proxy.start_system.assert_not_called()

    def test_audit_failure_rolls_back_identity_membership_and_version(self):
        item = self.claimed()
        version = self.sql("SELECT version FROM innertest_roster_version")
        self.fixture.service.audit.record.side_effect = RuntimeError("synthetic audit unavailable")
        handler, proxy = self.handler()
        with self.assertRaisesRegex(RuntimeError, "synthetic audit"):
            handler.handle(item)
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_membership"), [(0,)])
        self.assertEqual(self.sql("SELECT version FROM innertest_roster_version"), version)
        self.assertEqual(self.sql("SELECT open_id FROM innertest_batch_item"), [(None,)])
        proxy.start_system.assert_not_called()
