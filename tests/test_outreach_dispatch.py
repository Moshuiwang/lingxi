"""主动发送的编排：幂等、失败可见、重试不重复送达（Issue #586 完成标准 3/4/6）。

断言跑在注入的假记录口与假出站口上；真库那一半（唯一约束、``ON CONFLICT`` 守卫、
触发器）在 ``tests/test_postgres_outreach.py``，真实飞书那一半属 L4a。

否定面：已经送达的人**不会**被再发一次；发送失败**不会**被记成送达，也不会静默
（沿既有告警接线上报）；重试**必须**带同一个去重键，否则平台侧会出现第二条消息。
"""

from __future__ import annotations

import unittest

from lingxi.config.content import default_content_catalog
from lingxi.core.outreach.dispatch import (
    OUTREACH_ALERT_CHANNEL,
    STATUS_DELIVERED,
    STATUS_FAILED,
    OutreachDispatcher,
    OutreachPurpose,
    OutreachTarget,
    ReservedRecord,
    outreach_dedupe_key,
)
from lingxi.core.outreach.welcome_card import WELCOME_CONTENT_KEY, WelcomeAudience, WelcomeCardStyle

CATALOG = default_content_catalog()
OPEN_ID = "ou_fake_open_id_for_tests"
USER_ID = "usr_outreach_fake"


def _audience() -> WelcomeAudience:
    return WelcomeAudience(
        display_name="王晋 (Joshua Wang)",
        company_ids=("1011",),
        all_companies=False,
        metric_names=("充值金额",),
        company_names={"1011": "尼日利亚"},
        total_company_count=43,
    )


def _target() -> OutreachTarget:
    return OutreachTarget(
        recipient_open_id=OPEN_ID, subject=USER_ID, audience=_audience(), user_id=USER_ID
    )


class FakeStore:
    """假记录口：按 ``dedupe_key`` 记状态，形状与真库那一份一致。"""

    def __init__(self, *, existing: dict[str, str] | None = None) -> None:
        self.rows: dict[str, dict] = {}
        self.reserve_calls: list[dict] = []
        for key, status in (existing or {}).items():
            self.rows[key] = {"id": f"omr_{len(self.rows)}", "status": status, "attempts": 1}

    def reserve(self, **kwargs) -> ReservedRecord:
        self.reserve_calls.append(kwargs)
        key = kwargs["dedupe_key"]
        row = self.rows.get(key)
        if row is None:
            row = {"id": f"omr_{len(self.rows)}", "status": "pending", "attempts": 1}
            self.rows[key] = row
        elif row["status"] != STATUS_DELIVERED:
            row["attempts"] += 1
            row["status"] = "pending"
        return ReservedRecord(
            record_id=row["id"], dedupe_key=key, status=row["status"], attempts=row["attempts"]
        )

    def mark_delivered(self, record_id: str, *, message_id: str | None) -> None:
        for row in self.rows.values():
            if row["id"] == record_id:
                row["status"] = STATUS_DELIVERED
                row["message_id"] = message_id

    def mark_failed(self, record_id: str, *, error: str) -> None:
        for row in self.rows.values():
            if row["id"] == record_id:
                row["status"] = STATUS_FAILED
                row["last_error"] = error


class FakeSender:
    """假出站口：记录每一次调用；``errors`` 按次序注入失败。"""

    def __init__(self, *, errors: list[BaseException | None] | None = None) -> None:
        self.calls: list[dict] = []
        self._errors = list(errors or [])

    def send_card(self, *, open_id, card, dedupe_key):
        self.calls.append({"open_id": open_id, "card": card, "dedupe_key": dedupe_key})
        error = self._errors.pop(0) if self._errors else None
        if error is not None:
            raise error
        return f"om_fake_{len(self.calls)}"


class FakeAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, dict(fields)))


class RejectedByFeishuError(RuntimeError):
    code = "feishu_code_230013"


def _dispatcher(store, sender, audit, outcomes=None):
    return OutreachDispatcher(
        sender=sender,
        store=store,
        audit=audit,
        send_outcome=(lambda channel, ok: outcomes.append((channel, ok)))
        if outcomes is not None
        else None,
        catalog=CATALOG,
    )


class DedupeKeyTest(unittest.TestCase):
    def test_the_key_binds_content_purpose_and_recipient(self) -> None:
        key = outreach_dedupe_key(
            content_key=WELCOME_CONTENT_KEY, purpose=OutreachPurpose.APPLY, subject=USER_ID
        )
        self.assertEqual(key, f"{WELCOME_CONTENT_KEY}:apply:{USER_ID}")

    def test_precheck_and_apply_never_share_a_key(self) -> None:
        """否定断言：预检不得占用正式发送的幂等位，否则真发会被当成重复而跳过。"""
        self.assertNotEqual(
            outreach_dedupe_key(
                content_key=WELCOME_CONTENT_KEY, purpose=OutreachPurpose.APPLY, subject=USER_ID
            ),
            outreach_dedupe_key(
                content_key=WELCOME_CONTENT_KEY, purpose=OutreachPurpose.PRECHECK, subject=USER_ID
            ),
        )

    def test_a_key_without_a_recipient_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            outreach_dedupe_key(
                content_key=WELCOME_CONTENT_KEY, purpose=OutreachPurpose.APPLY, subject="  "
            )


class DeliveryTest(unittest.TestCase):
    def test_a_first_send_delivers_and_records_the_platform_id(self) -> None:
        store, sender, audit, outcomes = FakeStore(), FakeSender(), FakeAudit(), []
        outcome = _dispatcher(store, sender, audit, outcomes).deliver(
            _target(), purpose=OutreachPurpose.APPLY
        )
        self.assertEqual(outcome.status, STATUS_DELIVERED)
        self.assertFalse(outcome.skipped)
        self.assertEqual(outcome.message_id, "om_fake_1")
        self.assertEqual(outcomes, [(OUTREACH_ALERT_CHANNEL, True)])
        self.assertEqual(audit.records[0][0], "outreach.delivered")

    def test_the_card_that_goes_out_is_the_rendered_payload(self) -> None:
        store, sender, audit = FakeStore(), FakeSender(), FakeAudit()
        _dispatcher(store, sender, audit).deliver(_target(), purpose=OutreachPurpose.APPLY)
        card = sender.calls[0]["card"]
        self.assertEqual(card["schema"], "2.0")
        self.assertIn("尼日利亚", str(card))

    def test_rerunning_the_same_roster_sends_nothing_and_records_nothing_new(self) -> None:
        """完成标准 3：同名单 ``--apply`` 重跑零新增发送。"""
        store, sender, audit = FakeStore(), FakeSender(), FakeAudit()
        dispatcher = _dispatcher(store, sender, audit)
        dispatcher.deliver(_target(), purpose=OutreachPurpose.APPLY)
        second = dispatcher.deliver(_target(), purpose=OutreachPurpose.APPLY)
        self.assertTrue(second.skipped)
        self.assertEqual(second.status, STATUS_DELIVERED)
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(len(store.rows), 1)
        self.assertEqual(audit.records[-1][0], "outreach.skipped")

    def test_a_retry_carries_the_same_dedupe_key_so_the_platform_still_sees_one_message(
        self,
    ) -> None:
        """完成标准 3 后半：同去重 uuid 重试后收件人消息仍 1 条。"""
        store = FakeStore()
        sender = FakeSender(errors=[RejectedByFeishuError()])
        audit = FakeAudit()
        dispatcher = _dispatcher(store, sender, audit)
        dispatcher.deliver(_target(), purpose=OutreachPurpose.APPLY)
        dispatcher.deliver(_target(), purpose=OutreachPurpose.APPLY)
        self.assertEqual(len(sender.calls), 2)
        self.assertEqual(sender.calls[0]["dedupe_key"], sender.calls[1]["dedupe_key"])


class FailureTest(unittest.TestCase):
    def test_a_failed_send_is_recorded_as_failed_and_reported_to_the_admin_group(self) -> None:
        """完成标准 4：失败不静默——落 ``failed``、报告警、可重试。"""
        store = FakeStore()
        sender = FakeSender(errors=[RejectedByFeishuError()])
        audit, outcomes = FakeAudit(), []
        outcome = _dispatcher(store, sender, audit, outcomes).deliver(
            _target(), purpose=OutreachPurpose.APPLY
        )
        self.assertEqual(outcome.status, STATUS_FAILED)
        self.assertEqual(outcome.error_code, "feishu_code_230013")
        self.assertEqual(outcomes, [(OUTREACH_ALERT_CHANNEL, False)])
        self.assertEqual(audit.records[0][0], "outreach.failed")
        self.assertEqual(next(iter(store.rows.values()))["status"], STATUS_FAILED)

    def test_a_retry_after_a_failure_reaches_delivered(self) -> None:
        store = FakeStore()
        sender = FakeSender(errors=[RejectedByFeishuError(), None])
        audit = FakeAudit()
        dispatcher = _dispatcher(store, sender, audit)
        dispatcher.deliver(_target(), purpose=OutreachPurpose.APPLY)
        second = dispatcher.deliver(_target(), purpose=OutreachPurpose.APPLY)
        self.assertEqual(second.status, STATUS_DELIVERED)
        self.assertEqual(next(iter(store.rows.values()))["status"], STATUS_DELIVERED)
        self.assertEqual(next(iter(store.rows.values()))["attempts"], 2)

    def test_an_alert_callback_that_blows_up_does_not_change_the_result(self) -> None:
        """告警回调坏了不能反过来把一次成功送达变成失败。"""

        def explode(channel: str, ok: bool) -> None:
            raise RuntimeError("告警通道故障")

        dispatcher = OutreachDispatcher(
            sender=FakeSender(),
            store=FakeStore(),
            audit=FakeAudit(),
            send_outcome=explode,
            catalog=CATALOG,
        )
        self.assertEqual(
            dispatcher.deliver(_target(), purpose=OutreachPurpose.APPLY).status, STATUS_DELIVERED
        )

    def test_nothing_recorded_carries_the_card_body(self) -> None:
        """正文不进审计（记录里只有键、版本、样式、结果）。"""
        audit = FakeAudit()
        _dispatcher(FakeStore(), FakeSender(), audit).deliver(
            _target(), purpose=OutreachPurpose.APPLY
        )
        self.assertNotIn("尼日利亚", str(audit.records))
        self.assertNotIn("Joshua", str(audit.records))


class PrecheckSameSourceTest(unittest.TestCase):
    def test_precheck_and_apply_produce_a_byte_identical_card(self) -> None:
        """完成标准 5 的行为半边：同一渲染函数、同一数据形状。"""
        sender_apply, sender_precheck = FakeSender(), FakeSender()
        _dispatcher(FakeStore(), sender_apply, FakeAudit()).deliver(
            _target(), purpose=OutreachPurpose.APPLY
        )
        admin_target = OutreachTarget(
            recipient_open_id="ou_admin_fake",
            subject="ou_admin_fake:run1",
            audience=_audience(),
            user_id=None,
        )
        _dispatcher(FakeStore(), sender_precheck, FakeAudit()).deliver(
            admin_target, purpose=OutreachPurpose.PRECHECK
        )
        self.assertEqual(sender_apply.calls[0]["card"], sender_precheck.calls[0]["card"])

    def test_a_precheck_record_is_marked_as_such(self) -> None:
        """预检不算正式送达：记录里的用途是一列事实，不靠推断。"""
        store = FakeStore()
        _dispatcher(store, FakeSender(), FakeAudit()).deliver(
            OutreachTarget(
                recipient_open_id="ou_admin_fake",
                subject="ou_admin_fake:run1",
                audience=_audience(),
            ),
            purpose=OutreachPurpose.PRECHECK,
        )
        self.assertEqual(store.reserve_calls[0]["purpose"], "precheck")
        self.assertIsNone(store.reserve_calls[0]["user_id"])

    def test_the_style_reaches_the_record(self) -> None:
        store = FakeStore()
        OutreachDispatcher(
            sender=FakeSender(),
            store=store,
            audit=FakeAudit(),
            catalog=CATALOG,
            style=WelcomeCardStyle.PLAIN_MARKDOWN,
        ).deliver(_target(), purpose=OutreachPurpose.APPLY)
        self.assertEqual(store.reserve_calls[0]["card_style"], "plain_markdown")


if __name__ == "__main__":
    unittest.main()
