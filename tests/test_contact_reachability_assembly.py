"""#673 联系可达状态回调装配：管理员待办文案、通知失败不带走已落库状态。

纯逻辑与假出站口那一半在本文件；``build_contact_reachability_recorder`` 接线
真实 ``PostgresAppUserStore`` 那一半（未配置管理群时的日志兜底分支）在
``tests/test_contact_reachability.py`` 同一批真库用例旁边——本文件不需要数据库。
"""

from __future__ import annotations

import unittest

from lingxi.apps.scheduler.contact_reachability_assembly import _send_admin_todo, _todo_text


class RecordingAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, dict(fields)))

    def actions(self) -> list[str]:
        return [action for action, _ in self.records]


class FakeNotifier:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._error = error

    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None:
        self.calls.append({"chat_id": chat_id, "text": text, "dedupe_key": dedupe_key})
        if self._error is not None:
            raise self._error


class _Row:
    def __init__(self, email: str | None) -> None:
        self.email = email


class FakeStore:
    def __init__(self, *, email: str | None = "person@example.com", raises: bool = False) -> None:
        self._email = email
        self._raises = raises

    def get_by_open_id(self, open_id: str) -> _Row:
        if self._raises:
            raise RuntimeError("库炸了")
        return _Row(self._email)


class TodoTextTest(unittest.TestCase):
    def test_the_text_names_this_as_a_todo_not_an_alert(self) -> None:
        text = _todo_text(open_id="ou_x", email="a@b.com", error_code="feishu_code_230013")
        self.assertIn("联系待办", text)
        self.assertIn("非故障告警", text)
        self.assertIn("feishu_code_230013", text)
        self.assertIn("a@b.com", text)

    def test_a_missing_email_still_gives_an_actionable_fallback(self) -> None:
        text = _todo_text(open_id="ou_x", email=None, error_code="feishu_code_230013")
        self.assertIn("ou_x", text)
        self.assertIn("查花名册", text)


class SendAdminTodoTest(unittest.TestCase):
    def test_a_successful_send_notifies_the_configured_channel_and_audits(self) -> None:
        notifier, store, audit = FakeNotifier(), FakeStore(), RecordingAudit()

        _send_admin_todo(
            notifier,
            store,
            chat_id="oc_admin",
            open_id="ou_x",
            error_code="feishu_code_230013",
            audit=audit,
        )

        self.assertEqual(len(notifier.calls), 1)
        self.assertEqual(notifier.calls[0]["chat_id"], "oc_admin")
        self.assertEqual(notifier.calls[0]["dedupe_key"], "contact-unavailable:ou_x")
        self.assertIn("outreach.contact_todo_notified", audit.actions())

    def test_a_lookup_failure_still_sends_the_todo_without_an_email(self) -> None:
        """查邮箱失败不能带走整条待办：待办本身仍要送到，只是缺一条联系方式。"""
        notifier, store, audit = FakeNotifier(), FakeStore(raises=True), RecordingAudit()

        _send_admin_todo(
            notifier,
            store,
            chat_id="oc_admin",
            open_id="ou_x",
            error_code="feishu_code_230013",
            audit=audit,
        )

        self.assertEqual(len(notifier.calls), 1)
        self.assertIn("查花名册", notifier.calls[0]["text"])

    def test_a_notifier_failure_is_audited_and_does_not_raise(self) -> None:
        notifier = FakeNotifier(error=RuntimeError("群发不出去"))
        store, audit = FakeStore(), RecordingAudit()

        _send_admin_todo(
            notifier,
            store,
            chat_id="oc_admin",
            open_id="ou_x",
            error_code="feishu_code_230013",
            audit=audit,
        )

        self.assertIn("outreach.contact_todo_notify_failed", audit.actions())
        self.assertNotIn("outreach.contact_todo_notified", audit.actions())

    def test_no_audit_sink_is_a_silent_no_op_on_success(self) -> None:
        notifier, store = FakeNotifier(), FakeStore()

        _send_admin_todo(
            notifier,
            store,
            chat_id="oc_admin",
            open_id="ou_x",
            error_code="feishu_code_230013",
            audit=None,
        )

        self.assertEqual(len(notifier.calls), 1)


if __name__ == "__main__":
    unittest.main()
