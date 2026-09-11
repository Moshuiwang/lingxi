"""联系可达回调：成功 / 失败两种结局各自落库并留审计，失败时另转管理群待办。

全部用注入式替身，不连库、不发网络请求——失败结局按取证边界只验代码路径。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from lingxi.apps.scheduler.contact_reachability_assembly import _LogOnlyContactNotifier
from lingxi.core.outreach.contact_reachability import (
    AUDIT_MARKED_REACHABLE,
    AUDIT_MARKED_UNAVAILABLE,
    AUDIT_TODO_NOTIFIED,
    AUDIT_TODO_NOTIFY_FAILED,
    TODO_DEDUPE_PREFIX,
    ContactReachabilityRecorder,
    admin_todo_text,
)

OPEN_ID = "ou_0123456789abcdef0123456789abcdef"
EMAIL = "person@example.com"
FIXED_NOW = datetime(2026, 9, 11, 3, 0, tzinfo=UTC)


class RecordingAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, dict(fields)))

    def actions(self) -> list[str]:
        return [action for action, _ in self.records]


class FakeNotifier:
    def __init__(self, *, raises: bool = False) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self._raises = raises

    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None:
        if self._raises:
            raise RuntimeError("group unreachable")
        self.sent.append((chat_id, text, dedupe_key))


class _Row:
    def __init__(self, email: str | None) -> None:
        self.email = email


class FakeStore:
    def __init__(self, *, email: str | None = EMAIL, lookup_raises: bool = False) -> None:
        self.reachable: list[tuple[str, datetime]] = []
        self.unavailable: list[tuple[str, datetime, str]] = []
        self._email = email
        self._lookup_raises = lookup_raises

    def record_contact_reachable(self, *, open_id: str, when: datetime) -> bool:
        self.reachable.append((open_id, when))
        return True

    def record_contact_unavailable(self, *, open_id: str, when: datetime, code: str) -> bool:
        self.unavailable.append((open_id, when, code))
        return True

    def get_by_open_id(self, open_id: str) -> _Row | None:
        if self._lookup_raises:
            raise RuntimeError("db down")
        return _Row(self._email)


def _recorder(store: FakeStore, notifier: FakeNotifier, audit: RecordingAudit | None):
    return ContactReachabilityRecorder(
        store=store, notifier=notifier, chat_id="oc_admins", audit=audit, clock=lambda: FIXED_NOW
    )


class TodoTextTest(unittest.TestCase):
    def test_the_text_names_this_as_a_todo_not_an_alert(self) -> None:
        text = admin_todo_text(open_id=OPEN_ID, email=EMAIL, error_code="feishu_code_230013")

        self.assertIn("联系待办", text)
        self.assertIn("非故障告警", text)
        self.assertIn(OPEN_ID, text)
        self.assertIn(EMAIL, text)
        self.assertIn("feishu_code_230013", text)

    def test_a_missing_email_still_gives_an_actionable_fallback(self) -> None:
        text = admin_todo_text(open_id=OPEN_ID, email=None, error_code="feishu_code_230013")

        self.assertIn("花名册", text)
        self.assertIn(OPEN_ID, text)


class SuccessOutcomeTest(unittest.TestCase):
    def test_success_marks_reachable_at_the_clock_time_and_audits_redacted(self) -> None:
        store, notifier, audit = FakeStore(), FakeNotifier(), RecordingAudit()

        _recorder(store, notifier, audit)(OPEN_ID, True, None)

        self.assertEqual(store.reachable, [(OPEN_ID, FIXED_NOW)])
        self.assertEqual(store.unavailable, [])
        self.assertEqual(notifier.sent, [], "成功结局不该发任何管理群消息")
        self.assertEqual(audit.actions(), [AUDIT_MARKED_REACHABLE])
        self.assertNotIn(OPEN_ID, repr(audit.records), "审计里的标识必须脱敏")


class FailureOutcomeTest(unittest.TestCase):
    def test_failure_marks_unavailable_then_sends_one_todo_with_its_own_dedupe_key(self) -> None:
        store, notifier, audit = FakeStore(), FakeNotifier(), RecordingAudit()

        _recorder(store, notifier, audit)(OPEN_ID, False, "feishu_code_230013")

        self.assertEqual(store.unavailable, [(OPEN_ID, FIXED_NOW, "feishu_code_230013")])
        self.assertEqual(store.reachable, [])
        self.assertEqual(len(notifier.sent), 1)
        chat_id, text, dedupe_key = notifier.sent[0]
        self.assertEqual(chat_id, "oc_admins")
        self.assertIn(EMAIL, text)
        self.assertEqual(dedupe_key, TODO_DEDUPE_PREFIX + OPEN_ID)
        self.assertEqual(audit.actions(), [AUDIT_MARKED_UNAVAILABLE, AUDIT_TODO_NOTIFIED])
        self.assertNotIn(OPEN_ID, repr(audit.records), "审计里的标识必须脱敏")

    def test_a_missing_error_code_is_recorded_as_unknown(self) -> None:
        store, notifier = FakeStore(), FakeNotifier()

        _recorder(store, notifier, None)(OPEN_ID, False, None)

        self.assertEqual(store.unavailable[0][2], "unknown")
        self.assertIn("unknown", notifier.sent[0][1])

    def test_a_lookup_failure_still_sends_the_todo_without_an_email(self) -> None:
        store, notifier, audit = FakeStore(lookup_raises=True), FakeNotifier(), RecordingAudit()

        _recorder(store, notifier, audit)(OPEN_ID, False, "feishu_code_230013")

        self.assertEqual(len(store.unavailable), 1)
        self.assertEqual(len(notifier.sent), 1)
        self.assertNotIn(EMAIL, notifier.sent[0][1])
        self.assertIn("花名册", notifier.sent[0][1])
        self.assertIn(AUDIT_TODO_NOTIFIED, audit.actions())

    def test_a_notifier_failure_is_audited_and_keeps_the_recorded_state(self) -> None:
        store, notifier, audit = FakeStore(), FakeNotifier(raises=True), RecordingAudit()

        _recorder(store, notifier, audit)(OPEN_ID, False, "feishu_code_230013")

        self.assertEqual(len(store.unavailable), 1, "待办没发出去不得带走已落库的状态")
        self.assertEqual(audit.actions(), [AUDIT_MARKED_UNAVAILABLE, AUDIT_TODO_NOTIFY_FAILED])
        self.assertNotIn(AUDIT_TODO_NOTIFIED, audit.actions())

    def test_no_audit_sink_is_a_silent_no_op(self) -> None:
        store, notifier = FakeStore(), FakeNotifier()

        _recorder(store, notifier, None)(OPEN_ID, False, "feishu_code_230013")

        self.assertEqual(len(store.unavailable), 1)
        self.assertEqual(len(notifier.sent), 1)


class LogOnlyOutletTest(unittest.TestCase):
    def test_the_log_only_outlet_writes_no_personal_data(self) -> None:
        text = admin_todo_text(open_id=OPEN_ID, email=EMAIL, error_code="feishu_code_230013")

        with self.assertLogs("lingxi.apps.scheduler.contact_reachability", level="WARNING") as logs:
            _LogOnlyContactNotifier().send_text(
                chat_id="scheduler-log-only", text=text, dedupe_key=TODO_DEDUPE_PREFIX + OPEN_ID
            )

        joined = "\n".join(logs.output)
        self.assertIn("未配置管理群", joined)
        self.assertNotIn(OPEN_ID, joined)
        self.assertNotIn(EMAIL, joined)
        self.assertNotIn("feishu_code_230013", joined)


if __name__ == "__main__":
    unittest.main()
