"""有限重试的真实二十四小时边界与停止交接计数。"""

import os
import unittest
from datetime import UTC, datetime, timedelta

import test_admin_followup_postgres as followup_fixtures
from postgres_schema import ensure_production_schema, psycopg_available

from lingxi.adapters.postgres import connect

DSN = os.environ.get("LINGXI_POSTGRES_DSN")


@unittest.skipUnless(DSN and psycopg_available(), "需要独占合成PostgreSQL")
class FollowupBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_production_schema(DSN)

    def setUp(self):
        self.fixture = followup_fixtures.FollowupPostgresTests()
        self.fixture.setUp()
        self.store = self.fixture.store

    def seed_claim(self, *, age, failures=0):
        ref = self.fixture.add()
        now = datetime.now(UTC)
        with connect(DSN) as conn, conn.cursor() as cursor:
            cursor.execute(
                "UPDATE admin_action_followup SET created_at=%s,next_attempt_at=%s,"
                "failure_count=%s WHERE id=%s",
                (now - age, now, failures, ref.id),
            )
        item = self.fixture.claim(now=now)
        self.assertIsNotNone(item)
        return item, now

    def status(self):
        with connect(DSN) as conn, conn.cursor() as cursor:
            cursor.execute("SELECT status,attempt,failure_count FROM admin_action_followup")
            return cursor.fetchone()

    def test_exact_twenty_four_hours_finishes_even_with_one_failure(self):
        item, now = self.seed_claim(age=timedelta(hours=24))
        self.store.retry_followup(
            id=item.id, owner="one", attempt=item.attempt, now=now, result_code="temporary"
        )
        self.assertEqual(self.status(), ("failed", 1, 1))

    def test_before_twenty_four_hours_stays_recoverable(self):
        item, now = self.seed_claim(age=timedelta(hours=24) - timedelta(seconds=1))
        self.store.retry_followup(
            id=item.id, owner="one", attempt=item.attempt, now=now, result_code="temporary"
        )
        self.assertEqual(self.status(), ("retry_wait", 1, 1))

    def test_stop_release_preserves_failure_count_and_new_claim_advances_generation(self):
        item, now = self.seed_claim(age=timedelta(hours=1), failures=3)
        self.store.retry_followup(
            id=item.id,
            owner="one",
            attempt=item.attempt,
            now=now,
            result_code="stopping",
            stopped=True,
        )
        self.assertEqual(self.status(), ("retry_wait", 1, 3))
        next_item = self.fixture.claim("two", now)
        self.assertEqual(next_item.attempt, 2)
        self.assertEqual(self.status(), ("running", 2, 3))

    def test_expired_internal_claim_obeys_age_limit_without_extra_failure(self):
        item, now = self.seed_claim(age=timedelta(hours=24), failures=2)
        counts = self.store.recover_expired(now=now + timedelta(seconds=121))
        self.assertEqual(counts.failed, 1)
        self.assertEqual(self.status(), ("failed", 1, 2))
        self.assertIsNone(self.fixture.claim("two", now + timedelta(seconds=122)))
