"""邮箱解析的五态、未知来源、不在快照判定及已有绑定防线；仅本机纯逻辑样本。"""

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

from lingxi.core.identity.email_location import locate_current_email
from lingxi.core.identity.email_resolver import (
    EmailIdentitySnapshot,
    resolve_email_identity,
)
from lingxi.core.identity.email_resolver import (
    EmailIdentityState as State,
)
from lingxi.core.identity.first_contact import EmploymentStatus
from lingxi.core.identity.org_snapshot import DirectoryAvailability, SnapshotMember

ACTIVE = EmploymentStatus(True, False, False, False, False)
INACTIVE = EmploymentStatus(True, True, False, False, False)
ABSENT = EmploymentStatus.absent()
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
        self.assertEqual((result.active_candidate_count, result.absent_candidate_count), (2, 0))
        self.assertIsNone(result.selected)

    def test_absent_and_active_selects_the_active_candidate(self):
        for rows in ([row("old"), row("new")], [row("new"), row("old")]):
            with self.subTest(rows=rows):
                result = self.resolve(rows, {"old": ABSENT, "new": ACTIVE})
                self.assertEqual(result.state, State.UNIQUE_ACTIVE)
                self.assertEqual(result.selected["personnel_id"], "new")
                self.assertEqual(
                    (result.candidate_count, result.active_candidate_count),
                    (2, 1),
                )
                self.assertEqual(result.absent_candidate_count, 1)

    def test_all_absent_is_inactive_not_unavailable(self):
        for rows, statuses in (
            ([row("p1"), row("p2")], {"p1": ABSENT, "p2": ABSENT}),
            ([row("p1")], {"p1": ABSENT}),
            ([row("p1"), row("p2")], {"p1": ABSENT, "p2": INACTIVE}),
        ):
            with self.subTest(statuses=statuses):
                result = self.resolve(rows, statuses)
                self.assertEqual(result.state, State.INACTIVE)
                self.assertEqual(result.reason, "all_candidates_inactive")
                self.assertEqual(result.active_candidate_count, 0)
                self.assertEqual(
                    result.absent_candidate_count,
                    sum(1 for status in statuses.values() if status is ABSENT),
                )
                self.assertIsNone(result.selected)

    def test_absent_with_an_unknown_candidate_stays_unavailable(self):
        for unknown in (None, {}, False):
            with self.subTest(unknown=unknown):
                result = self.resolve([row("p1"), row("p2")], {"p1": ABSENT, "p2": unknown})
                self.assertEqual(result.state, State.UNAVAILABLE)
                self.assertEqual(result.reason, "employment_unknown")
                self.assertIsNone(result.active_candidate_count)
                self.assertIsNone(result.absent_candidate_count)
                self.assertIsNone(result.selected)

    def test_absent_is_never_employed_and_cannot_be_read_from_feishu(self):
        self.assertTrue(ABSENT.absent_from_directory)
        self.assertFalse(ABSENT.employed)
        # 即使标志位被改成"在职"的形状，不在快照仍然压倒一切。
        self.assertFalse(replace(ABSENT, is_activated=True, is_exited=False).employed)
        payload = {key: False for key in EmploymentStatus._FLAGS}
        payload["is_activated"] = True
        payload["absent_from_directory"] = True
        live = EmploymentStatus.from_feishu(payload)
        self.assertEqual(live, ACTIVE)
        self.assertFalse(live.absent_from_directory)

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
                "absent_candidate_count": 0,
                "selected_personnel_id": "p1",
            },
        )


def member(personnel, open_id):
    return SnapshotMember(
        tenant_key="tenant-fake",
        member_key=f"tenant-fake:{open_id}",
        open_id=open_id,
        user_id=personnel,
        union_id=f"on_{open_id}",
        display_name="化名甲",
        display_name_locale=None,
        department_names=("测试部门",),
    )


def lookup(*members, availability=DirectoryAvailability.AVAILABLE):
    return SimpleNamespace(availability=availability, members=members)


NEW = member("new", "ou_new")
GONE = lookup()


class Directory:
    """按人员 ID 预置一次 ``lookup_by_user_id`` 的回答；异常值原样抛出。"""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def lookup_by_user_id(self, user_id):
        self.calls.append(user_id)
        answer = self.answers[user_id]
        if isinstance(answer, Exception):
            raise answer
        return answer


class LocateCurrentEmailTests(unittest.TestCase):
    """只有快照可用且精确零成员才判不在快照；其余情形保持未知、不能让另一候选独占。"""

    def locate(self, answers, statuses=None):
        self.employment_calls = []
        statuses = {"ou_new": ACTIVE} if statuses is None else statuses

        def status(*, tenant_key, open_id):
            self.employment_calls.append(open_id)
            return statuses[open_id]

        self.directory = Directory(answers)
        return locate_current_email(
            "sample@example.test",
            snapshot=EmailIdentitySnapshot((row("old"), row("new")), "snapshot-test", STAMP),
            directory=self.directory,
            employment=SimpleNamespace(status=status),
        )

    def test_absent_from_a_fresh_directory_lets_the_active_candidate_win(self):
        located = self.locate({"old": GONE, "new": lookup(NEW)})
        self.assertEqual(located.resolution.state, State.UNIQUE_ACTIVE)
        self.assertIs(located.member, NEW)
        self.assertEqual(located.resolution.absent_candidate_count, 1)
        self.assertEqual(located.resolution.active_candidate_count, 1)
        self.assertEqual(sorted(self.directory.calls), ["new", "old"])
        # 不在快照的候选没有可回读的主体，实时在职只对在快照的候选读一次。
        self.assertEqual(self.employment_calls, ["ou_new"])

    def test_zero_members_outside_a_fresh_directory_stays_unknown(self):
        for availability in (
            DirectoryAvailability.STALE,
            DirectoryAvailability.UNAVAILABLE,
            "available",
            None,
        ):
            with self.subTest(availability=availability):
                located = self.locate(
                    {"old": lookup(availability=availability), "new": lookup(NEW)}
                )
                self.assertEqual(located.resolution.state, State.UNAVAILABLE)
                self.assertEqual(located.resolution.reason, "employment_unknown")
                self.assertIsNone(located.resolution.absent_candidate_count)
                self.assertIsNone(located.member)

    def test_more_than_one_member_is_not_absent(self):
        twins = lookup(member("old", "ou_old_a"), member("old", "ou_old_b"))
        located = self.locate({"old": twins, "new": lookup(NEW)})
        self.assertEqual(located.resolution.state, State.UNAVAILABLE)
        self.assertEqual(located.resolution.reason, "employment_unknown")
        self.assertIsNone(located.member)

    def test_failed_or_malformed_lookups_are_not_absent(self):
        for answer in (
            RuntimeError("synthetic directory failure"),
            SimpleNamespace(availability=DirectoryAvailability.AVAILABLE, members=None),
            SimpleNamespace(availability=DirectoryAvailability.AVAILABLE),
        ):
            with self.subTest(answer=repr(answer)):
                located = self.locate({"old": answer, "new": lookup(NEW)})
                self.assertEqual(located.resolution.state, State.UNAVAILABLE)
                self.assertIsNone(located.resolution.absent_candidate_count)
                self.assertIsNone(located.member)

    def test_every_candidate_absent_is_inactive_without_live_reads(self):
        located = self.locate({"old": GONE, "new": GONE})
        self.assertEqual(located.resolution.state, State.INACTIVE)
        self.assertEqual(located.resolution.absent_candidate_count, 2)
        self.assertIsNone(located.member)
        self.assertEqual(self.employment_calls, [])


if __name__ == "__main__":
    unittest.main()
