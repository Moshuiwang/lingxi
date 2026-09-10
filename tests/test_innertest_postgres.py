"""扩员同事务与真实状态的合成 PostgreSQL 验证，不连接真实外部平台。"""

import multiprocessing
import os
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.adapters.admin_registry import seed_admin_registry_entry
from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_innertest import PostgresInnertestService
from lingxi.adapters.postgres_innertest_confirmation import InnertestPendingActions
from lingxi.adapters.postgres_innertest_roster import PostgresInnertestRoster
from lingxi.adapters.postgres_pending_action import PostgresPendingActionStore
from lingxi.core.admin.innertest import InnertestError
from lingxi.core.admin.pending_action import ConfirmResultKind
from lingxi.core.identity.org_snapshot import SnapshotMember
from lingxi.core.identity.preprovision import PreprovisionSkip, PreprovisionTarget

DSN = os.environ.get("LINGXI_POSTGRES_DSN")


def locate(email, *, connection):
    del connection
    if email.startswith("missing"):
        return PreprovisionSkip(email=email, reason="email_not_in_roster")
    if email.startswith("duplicate"):
        return PreprovisionSkip(email=email, reason="directory_multiple_members")
    number = email.split("@")[0]
    return PreprovisionTarget(
        email=email,
        personnel_id=number,
        member=SnapshotMember(
            tenant_key="synthetic",
            member_key=number,
            open_id="ou_" + number,
            user_id=number,
            union_id="on_" + number,
            display_name=number,
        ),
    )


def read_roster(dsn, queue):
    queue.put(PostgresInnertestRoster(dsn, scope="synthetic").snapshot("ou_person1"))


@unittest.skipUnless(DSN and psycopg_available(), "需独占合成 PostgreSQL")
class InnertestPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_production_schema(DSN)

    def setUp(self):
        reset_production_rows(DSN)
        seed_admin_registry_entry(DSN, feishu_open_id="ou_admin", label="合成管理员")
        with connect(DSN) as c, c.cursor() as cur:
            cur.execute(
                "INSERT INTO innertest_roster_version(scope,mode) VALUES('synthetic','database')"
            )
            cur.execute(
                "INSERT INTO innertest_admin_binding(id,scope,open_id,version,enabled) "
                "VALUES('binding','synthetic','ou_admin',1,true)"
            )
        self.audit = Mock()
        self.service = PostgresInnertestService(
            DSN,
            scope="synthetic",
            binding=SimpleNamespace(binding_id="binding", peer_uid=1234),
            locator=locate,
            audit=self.audit,
        )
        self.principal = self.service.authenticate(1234)
        original = PostgresPendingActionStore(
            DSN, audit=self.audit, metric_map_path=None, durable_followups=True
        )
        self.pending = InnertestPendingActions(original, self.service)

    def sql(self, text, args=()):
        with connect(DSN) as c, c.cursor() as cur:
            cur.execute(text, args)
            return cur.fetchall() if cur.description else None

    def prepare(self, emails=None, key="same"):
        return self.service.prepare(
            self.principal, request_key=key, emails=emails or ["person1@example.test"]
        )

    def delivered(self, batch):
        self.pending.mark_card_delivered(
            pending_action_id=batch["pending_action_id"], card_id="synthetic-card"
        )

    def confirm(self, batch, who="ou_admin", now=None):
        return self.pending.confirm(
            pending_action_id=batch["pending_action_id"], clicker_open_id=who, now=now
        )

    def test_twenty_mixed_prepare_then_atomic_confirmation(self):
        emails = [f"person{i}@example.test" for i in range(17)] + [
            "missing@example.test",
            "duplicate@example.test",
            "person1@example.test",
        ]
        self.sql(
            "INSERT INTO innertest_membership(scope,open_id,email) VALUES('synthetic','ou_person0','person0@example.test')"
        )
        batch = self.prepare(emails)
        self.assertEqual(len(batch["items"]), 19)
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_membership")[0][0], 1)
        self.assertEqual(
            self.sql("SELECT stage FROM admin_action_followup"), [("confirmation_card_send",)]
        )
        self.delivered(batch)
        result = self.confirm(batch)
        self.assertTrue(result.decision.ok)
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_membership")[0][0], 17)
        self.assertEqual(
            self.sql(
                "SELECT count(*) FROM admin_action_followup WHERE stage='innertest_preprovision'"
            )[0][0],
            16,
        )
        self.assertEqual(
            self.sql(
                "SELECT count(*) FROM admin_action_followup WHERE stage='innertest_readiness_check'"
            )[0][0],
            16,
        )
        self.assertFalse(self.confirm(batch).decision.ok)
        self.assertEqual(self.sql("SELECT version FROM innertest_roster_version"), [(1,)])

    def test_request_key_same_intent_and_conflict(self):
        batch = self.prepare()
        self.assertEqual(self.prepare([" PERSON1@EXAMPLE.TEST "])["batch_id"], batch["batch_id"])
        with self.assertRaisesRegex(InnertestError, "idempotency_conflict"):
            self.prepare(["person2@example.test"])
        self.assertEqual(self.sql("SELECT count(*) FROM admin_action_followup"), [(1,)])

    def test_not_delivered_other_person_expired_version_drift_and_revoke(self):
        batch = self.prepare()
        self.assertFalse(self.confirm(batch).decision.ok)
        self.delivered(batch)
        self.assertFalse(self.confirm(batch, "ou_other").decision.ok)
        self.assertFalse(
            self.confirm(batch, now=datetime.now(UTC) + timedelta(hours=1)).decision.ok
        )
        self.sql("UPDATE innertest_roster_version SET version=version+1")
        self.assertFalse(self.confirm(batch).decision.ok)
        self.sql("UPDATE innertest_admin_binding SET enabled=false")
        # 绑定被停用后点确认：明确拒绝并落终态，不再把异常冒给调用方当「结果不明」。
        outcome = self.confirm(batch)
        self.assertFalse(outcome.decision.ok)
        self.assertIs(outcome.decision.kind, ConfirmResultKind.ROLE_REVOKED)
        self.assertEqual(
            self.sql("SELECT status,reason FROM pending_action"), [("failed", "role_revoked")]
        )
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_membership"), [(0,)])

    def test_transaction_rolls_back_when_enqueue_or_audit_fails(self):
        batch = self.prepare()
        self.delivered(batch)
        with patch(
            "lingxi.adapters.postgres_innertest_confirmation.enqueue_followups",
            side_effect=RuntimeError("synthetic"),
        ):
            with self.assertRaises(RuntimeError):
                self.confirm(batch)
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_membership"), [(0,)])
        self.assertEqual(self.sql("SELECT version FROM innertest_roster_version"), [(0,)])
        self.audit.record.side_effect = RuntimeError("audit")
        with self.assertRaisesRegex(InnertestError, "audit_unavailable"):
            self.confirm(batch)
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_membership"), [(0,)])

    def test_two_processes_read_same_version_without_restart(self):
        batch = self.prepare()
        self.delivered(batch)
        self.confirm(batch)
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        processes = [context.Process(target=read_roster, args=(DSN, queue)) for _ in range(2)]
        try:
            for p in processes:
                p.start()
            self.assertEqual([queue.get(timeout=10) for _ in processes], [(1, True), (1, True)])
        finally:
            for p in processes:
                p.join(timeout=5)
                if p.is_alive():
                    p.kill()
                    p.join()
            queue.close()

    def test_no_change_and_cancel_have_no_person_stages(self):
        result = self.prepare(["missing@example.test"])
        self.assertEqual(result["state"], "no_change")
        self.assertEqual(self.sql("SELECT count(*) FROM pending_action"), [(0,)])
        batch = self.prepare(key="new")
        self.delivered(batch)
        self.pending.cancel(
            pending_action_id=batch["pending_action_id"], clicker_open_id="ou_admin"
        )
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_membership"), [(0,)])
        self.assertEqual(
            self.sql(
                "SELECT count(*) FROM admin_action_followup WHERE stage='innertest_preprovision'"
            ),
            [(0,)],
        )

    def test_prepare_audit_failure_zero_batch_and_card(self):
        self.audit.record.side_effect = RuntimeError("audit")
        with self.assertRaisesRegex(InnertestError, "audit_unavailable"):
            self.prepare()
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_batch"), [(0,)])
        self.assertEqual(self.sql("SELECT count(*) FROM pending_action"), [(0,)])
        self.assertEqual(self.sql("SELECT count(*) FROM admin_action_followup"), [(0,)])

    def test_request_uses_remaining_deadline_for_database_statement(self):
        import time

        from lingxi.adapters.innertest_request import (
            _LOCAL,
            request_window,
        )
        from lingxi.adapters.innertest_request import (
            connect as request_connect,
        )

        started = time.monotonic()
        with request_window():
            _LOCAL.deadline = time.monotonic() + 0.05
            with self.assertRaises(Exception):
                with request_connect(DSN) as connection, connection.cursor() as cursor:
                    cursor.execute("SELECT pg_sleep(1)")
        self.assertLess(time.monotonic() - started, 0.5)

    def test_binding_version_change_can_prepare_new_but_unknown_card_cannot(self):
        old = self.prepare()
        self.sql("UPDATE innertest_admin_binding SET version=2")
        self.principal = self.service.authenticate(1234)
        new = self.prepare(key="replacement")
        self.assertNotEqual(new["batch_id"], old["batch_id"])
        self.assertEqual(
            self.service.get_batch(self.principal, batch_id=old["batch_id"])["state"], "failed"
        )
        self.sql(
            "UPDATE admin_action_followup SET status='unknown' WHERE pending_action_id=%s",
            (new["pending_action_id"],),
        )
        with self.assertRaisesRegex(InnertestError, "card_unknown"):
            self.prepare(key="no-blind-resend")
