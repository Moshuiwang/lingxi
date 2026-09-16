"""邮箱解析的五态、未知来源及已有绑定防线；仅本机纯逻辑样本。"""

import unittest
from dataclasses import replace
from datetime import UTC, datetime

from lingxi.core.identity.email_resolver import (
    EmailIdentitySnapshot,
    resolve_email_identity,
)
from lingxi.core.identity.email_resolver import (
    EmailIdentityState as State,
)
from lingxi.core.identity.first_contact import EmploymentStatus

ACTIVE = EmploymentStatus(True, False, False, False, False)
INACTIVE = EmploymentStatus(True, True, False, False, False)
STAMP = datetime(2026, 9, 16, tzinfo=UTC)


def row(person, *, email="sample@example.test", employee=None):
    return {
        "personnel_id": person,
        "email": email,
        "employee_no": employee or person,
        "name": "测试员工",
    }


class EmailIdentityResolverTest(unittest.TestCase):
    def resolve(self, rows, statuses, **kwargs):
        return resolve_email_identity(
            " SAMPLE@example.test ",
            snapshot=EmailIdentitySnapshot(tuple(rows), "snapshot-test", STAMP),
            employment=statuses,
            **kwargs,
        )

    def test_unique_active(self):
        result = self.resolve([row("p1")], {"p1": ACTIVE})
        self.assertEqual(result.state, State.UNIQUE_ACTIVE)
        self.assertEqual(result.selected["personnel_id"], "p1")
        self.assertEqual((result.candidate_count, result.active_candidate_count), (1, 1))

    def test_active_and_departed_selects_active_regardless_of_order(self):
        for rows in ([row("old"), row("new")], [row("new"), row("old")]):
            with self.subTest(rows=rows):
                result = self.resolve(rows, {"old": INACTIVE, "new": ACTIVE})
                self.assertEqual(result.state, State.UNIQUE_ACTIVE)
                self.assertEqual(result.selected["personnel_id"], "new")
                self.assertEqual((result.candidate_count, result.active_candidate_count), (2, 1))

    def test_all_departed_never_selects_history(self):
        result = self.resolve([row("p1"), row("p2")], {"p1": INACTIVE, "p2": INACTIVE})
        self.assertEqual(result.state, State.INACTIVE)
        self.assertEqual(result.active_candidate_count, 0)
        self.assertIsNone(result.selected)

    def test_two_active_never_selects_first(self):
        result = self.resolve([row("p1"), row("p2")], {"p1": ACTIVE, "p2": ACTIVE})
        self.assertEqual(result.state, State.ACTIVE_CONFLICT)
        self.assertEqual(result.active_candidate_count, 2)
        self.assertIsNone(result.selected)

    def test_not_found_is_not_inactive(self):
        result = self.resolve([row("p1", email="another@example.test")], {"p1": ACTIVE})
        self.assertEqual(result.state, State.NOT_FOUND)
        self.assertEqual(result.candidate_count, 0)

    def test_unknown_status_never_becomes_inactive_or_unique(self):
        for known in (ACTIVE, INACTIVE):
            for unknown in (None, {}, False):
                with self.subTest(known=known, unknown=unknown):
                    result = self.resolve([row("p1"), row("p2")], {"p1": known, "p2": unknown})
                    self.assertEqual(result.state, State.UNAVAILABLE)
                    self.assertIsNone(result.active_candidate_count)
                    self.assertIsNone(result.selected)

    def test_missing_status_is_unknown(self):
        result = self.resolve([row("p1")], {})
        self.assertEqual(result.reason, "employment_unknown")
        self.assertIsNone(result.active_candidate_count)

    def test_unavailable_snapshot_never_reports_zero_candidates(self):
        valid = EmailIdentitySnapshot((row("p1"),), "v1", STAMP)
        for invalid in (
            replace(valid, rows=None),
            replace(valid, available=False),
            replace(valid, version=None),
            replace(valid, captured_at=None),
            replace(valid, captured_at=datetime(2026, 9, 16)),
        ):
            with self.subTest(invalid=invalid):
                result = resolve_email_identity(
                    "sample@example.test", snapshot=invalid, employment={"p1": ACTIVE}
                )
                self.assertEqual(result.state, State.UNAVAILABLE)
                self.assertIsNone(result.candidate_count)
                self.assertIsNone(result.active_candidate_count)

    def test_new_identity_cannot_inherit_old_binding(self):
        result = self.resolve(
            [row("old"), row("new")], {"old": INACTIVE, "new": ACTIVE}, bound_personnel_id="old"
        )
        self.assertEqual(result.state, State.UNAVAILABLE)
        self.assertEqual(result.reason, "binding_mismatch")
        self.assertIsNone(result.selected)
        self.assertEqual(result.active_candidate_count, 1)

    def test_changed_employee_number_cannot_inherit_binding(self):
        result = self.resolve(
            [row("p1", employee="new-job")],
            {"p1": ACTIVE},
            bound_personnel_id="p1",
            bound_employee_no="old-job",
        )
        self.assertEqual(result.reason, "binding_mismatch")
        self.assertIsNone(result.selected)

    def test_matching_binding_preserved(self):
        result = self.resolve(
            [row("p1")], {"p1": ACTIVE}, bound_personnel_id="p1", bound_employee_no="p1"
        )
        self.assertEqual(result.state, State.UNIQUE_ACTIVE)

    def test_duplicate_raw_rows_not_deduplicated(self):
        result = self.resolve([row("p1"), row("p1")], {"p1": ACTIVE})
        self.assertEqual(result.state, State.ACTIVE_CONFLICT)
        self.assertEqual(result.candidate_count, 2)

    def test_missing_identity_field_is_unavailable(self):
        result = self.resolve([row("")], {"": ACTIVE})
        self.assertEqual(result.reason, "identity_field_unknown")
        self.assertIsNone(result.active_candidate_count)

    def test_all_five_employment_flags_must_be_known(self):
        payload = {key: False for key in EmploymentStatus._FLAGS}
        payload["is_activated"] = True
        for key in payload:
            incomplete = {k: v for k, v in payload.items() if k != key}
            result = self.resolve([row("p1")], {"p1": EmploymentStatus.from_feishu(incomplete)})
            self.assertEqual(result.state, State.UNAVAILABLE)

    def test_audit_records_source_counts_and_reason_without_contact_data(self):
        result = self.resolve([row("p1"), row("p2")], {"p1": ACTIVE, "p2": INACTIVE})
        self.assertEqual(
            result.audit_facts(),
            {
                "identity_state": "unique_active",
                "identity_reason": "unique_active_candidate",
                "snapshot_version": "snapshot-test",
                "snapshot_captured_at": STAMP.isoformat(),
                "candidate_count": 2,
                "active_candidate_count": 1,
                "selected_personnel_id": "p1",
            },
        )


if __name__ == "__main__":
    unittest.main()
