"""管理卡权限补偿收口与每日汇总的纯逻辑验收（Issue #493）。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from lingxi.apps.scheduler.assembly import _build_management_correction_callback


class _Audit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, object]]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, dict(fields)))


class _Store:
    instances: list[_Store] = []

    def __init__(self, *args, **kwargs) -> None:
        del args, kwargs
        self.settle_calls = 0
        self.ids = ["om_card_2", "om_card_1"]
        self.marked: list[tuple[str, ...]] = []
        self.__class__.instances.append(self)

    def settle_published_contexts(self) -> tuple[str, ...]:
        self.settle_calls += 1
        return ()

    def unreported_daily_correction_ids(self) -> tuple[str, ...]:
        return tuple(self.ids)

    def mark_daily_corrections_reported(self, *, message_ids: tuple[str, ...]) -> None:
        self.marked.append(tuple(message_ids))
        self.ids = [item for item in self.ids if item not in message_ids]


class _Sender:
    instances: list[_Sender] = []
    fail = False

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.calls: list[dict[str, object]] = []
        self.__class__.instances.append(self)

    def send_text(self, **kwargs) -> None:
        self.calls.append(dict(kwargs))
        if self.fail:
            raise RuntimeError("simulated transport failure")


def _config(*, chat_id: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        postgres_dsn="postgresql://unit.invalid/lingxi",
        postgres_timeouts=object(),
        admin_group_chat_id=chat_id,
        feishu_base_url="https://open.feishu.invalid/open-apis",
        feishu_app_id="cli_test",
        feishu_app_secret="secret_test",
    )


class ManagementCorrectionCallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        _Store.instances.clear()
        _Sender.instances.clear()
        _Sender.fail = False

    def _callback(self, *, chat_id: str | None, audit: _Audit):
        with mock.patch(
            "lingxi.adapters.postgres_management_card_context.PostgresManagementCardContextStore",
            _Store,
        ), mock.patch(
            "lingxi.adapters.feishu_group_message.FeishuGroupMessages", _Sender
        ):
            callback = _build_management_correction_callback(
                _config(chat_id=chat_id), audit=audit
            )
        return callback, _Store.instances[0]

    def test_success_marks_exact_batch_and_uses_stable_distinct_delivery_prefix(self) -> None:
        audit = _Audit()
        callback, store = self._callback(chat_id="oc_admin", audit=audit)
        sender = _Sender.instances[0]

        callback()

        self.assertEqual(store.settle_calls, 1)
        self.assertEqual(store.marked, [("om_card_1", "om_card_2")])
        self.assertEqual(len(sender.calls), 1)
        call = sender.calls[0]
        self.assertEqual(call["chat_id"], "oc_admin")
        self.assertIn("补齐 2 条", str(call["text"]))
        self.assertTrue(str(call["dedupe_key"]).startswith("management-correction:2026-"))
        self.assertEqual(sender.kwargs["uuid_prefix"], "lingxi-perm-fix-")
        self.assertEqual(audit.records[-1], ("admin.management_correction_summary_sent", {"count": 2}))
        self.assertNotIn("om_card", repr(audit.records))

    def test_send_failure_keeps_watermark_for_retry_with_same_batch(self) -> None:
        audit = _Audit()
        callback, store = self._callback(chat_id="oc_admin", audit=audit)
        sender = _Sender.instances[0]
        _Sender.fail = True

        callback()
        first_key = sender.calls[-1]["dedupe_key"]
        self.assertEqual(store.marked, [])
        self.assertEqual(audit.records[-1], (
            "admin.management_correction_summary_failed",
            {"count": 2, "error": "RuntimeError"},
        ))

        _Sender.fail = False
        callback()
        self.assertEqual(sender.calls[-1]["dedupe_key"], first_key)
        self.assertEqual(store.marked, [("om_card_1", "om_card_2")])

    def test_missing_channel_does_not_mark_unreported_rows_and_is_reported_once(self) -> None:
        audit = _Audit()
        callback, store = self._callback(chat_id=None, audit=audit)

        callback()
        callback()

        self.assertEqual(store.marked, [])
        self.assertEqual(
            audit.records,
            [
                (
                    "admin.management_correction_channel_missing",
                    {"count": 2, "variable": "LINGXI_ADMIN_GROUP_CHAT_ID"},
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
