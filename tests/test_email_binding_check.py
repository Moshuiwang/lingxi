"""无实时状态的入口只核对原绑定；结果不能被解释成在职选择。"""

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import Mock

from test_admin_router import ADMIN_OPEN_ID, FakeQueries, _router

from lingxi.core.identity.email_resolver import (
    EmailIdentitySnapshot,
    EmailIdentityUnresolvedError,
    check_email_binding,
)

ROW = {
    "personnel_id": "person-1",
    "employee_no": "job-1",
    "email": "one@example.test",
    "name": "合成员工",
}
SNAPSHOT = EmailIdentitySnapshot((ROW,), "binding-snapshot", datetime(2026, 9, 16, tzinfo=UTC))


class EmailBindingCheckTests(unittest.TestCase):
    def check(self, snapshot=SNAPSHOT, **kwargs):
        return check_email_binding(
            " ONE@example.test ", snapshot=snapshot, personnel_id="person-1", **kwargs
        )

    def test_one_raw_candidate_with_trusted_binding_matches_without_claiming_employment(self):
        check = self.check(employee_no="job-1")
        self.assertTrue(check.matched)
        facts = check.audit_facts()
        self.assertEqual(facts["identity_state"], "existing_binding_verified")
        self.assertEqual(facts["candidate_count"], 1)
        self.assertIsNone(facts["active_candidate_count"])
        self.assertNotIn("one@example.test", str(facts))
        self.assertNotIn("合成员工", str(facts))

    def test_reused_email_or_duplicate_raw_rows_do_not_pick_a_person(self):
        for other in (ROW, {**ROW, "personnel_id": "person-2"}):
            check = self.check(replace(SNAPSHOT, rows=(ROW, other)))
            self.assertFalse(check.matched)
            self.assertEqual(check.reason, "multiple_candidates")
            self.assertEqual(check.candidate_count, 2)

    def test_changed_primary_keys_and_missing_trusted_identity_refuse(self):
        self.assertEqual(self.check(employee_no="previous-job").reason, "binding_mismatch")
        self.assertEqual(
            self.check(replace(SNAPSHOT, rows=({**ROW, "personnel_id": "new"},))).reason,
            "binding_mismatch",
        )
        check = check_email_binding("one@example.test", snapshot=SNAPSHOT, personnel_id=None)
        self.assertEqual(check.reason, "binding_unknown")

    def test_missing_snapshot_is_not_empty_or_inactive(self):
        check = self.check(replace(SNAPSHOT, available=False))
        self.assertFalse(check.matched)
        self.assertEqual(check.reason, "snapshot_unavailable")
        self.assertIsNone(check.candidate_count)

    def test_admin_query_refuses_before_reading_user_or_preparing_action(self):
        queries = FakeQueries()
        queries.resolve_identifier = Mock(
            side_effect=EmailIdentityUnresolvedError(self.check(replace(SNAPSHOT, rows=(ROW, ROW))))
        )
        router, _, _, _ = _router(queries=queries)
        outcome = router.route(
            open_id=ADMIN_OPEN_ID, text="/admin user one@example.test", trace_id="binding-test"
        )
        self.assertTrue(outcome.handled)
        self.assertIn("本次未执行", outcome.reply_text)
        self.assertIn("核对人员资料", outcome.reply_text)
        self.assertEqual(queries.user_calls, [])
