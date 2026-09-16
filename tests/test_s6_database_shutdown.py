"""真库持锁制造停机后端尚在的形态，恢复扫描不得抢在事务结束之前。"""

import unittest
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

import test_admin_followup_postgres as fixture_module
from postgres_schema import ensure_production_schema, psycopg_available
from s6_database_shutdown import backend_is_present, recover_after_database_exit

from lingxi.adapters.postgres import connect

DSN = fixture_module.DSN


@unittest.skipUnless(DSN and psycopg_available(), "需要独占合成 PostgreSQL")
class DatabaseShutdownOrderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_production_schema(DSN)

    def setUp(self):
        self.fixture = fixture_module.FollowupPostgresTests()
        self.fixture.setUp()
        self.fixture.add()
        with connect(DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE admin_action_followup SET next_attempt_at=now()-interval '1 second'"
            )
        self.item = self.fixture.claim(now=datetime.now(UTC))
        self.assertIsNotNone(self.item)
        self.tag = "s6-backend-" + uuid4().hex

    def locked_backend(self):
        connection = connect(DSN, dedicated=True, application_name=self.tag)
        self.addCleanup(connection.close)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM admin_action_followup WHERE id=%s FOR UPDATE", (self.item.id,)
            )
            self.assertEqual(cursor.fetchone(), (self.item.id,))
        return connection

    def test_live_backend_fails_explicitly_without_attempting_recovery(self):
        self.locked_backend()
        with self.assertRaisesRegex(TimeoutError, "后端尚未结束"):
            recover_after_database_exit(self.fixture.store, DSN, self.tag, timeout_seconds=0)
        with connect(DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status,lease_owner FROM admin_action_followup WHERE id=%s", (self.item.id,)
            )
            self.assertEqual(cursor.fetchone(), ("running", self.item.lease_owner))

    def test_recovery_waits_for_actual_backend_close_then_recovers_once(self):
        backend = self.locked_backend()
        with connect(DSN, dedicated=True, autocommit=True) as observer:
            self.assertTrue(backend_is_present(observer, self.tag))
        # 只控制轮询间隔：第一次读到真实后端后才释放真实行锁，判据和恢复均不替身。
        with patch(
            "s6_database_shutdown.time.sleep", side_effect=lambda _: backend.close()
        ) as pause:
            result = recover_after_database_exit(self.fixture.store, DSN, self.tag)
        pause.assert_called()
        self.assertEqual(result.recoverable, 1)
        with connect(DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status,result_code,lease_until FROM admin_action_followup WHERE id=%s",
                (self.item.id,),
            )
            self.assertEqual(cursor.fetchone(), ("retry_wait", "lease_expired", None))

    def test_an_unrelated_backend_does_not_block_target_process_recovery(self):
        with connect(DSN, dedicated=True, application_name=self.tag) as other:
            with other.cursor() as cursor:
                cursor.execute("SELECT 1")
            result = recover_after_database_exit(
                self.fixture.store,
                DSN,
                self.tag + "-finished",
                timeout_seconds=0,
            )
        self.assertEqual(result.recoverable, 1)
