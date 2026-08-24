"""``core/admin/card_dispatch.ConfirmCardDispatcher`` 的编排断言（Issue #96 S-M-02）。

只测编排本身（发送成功/失败分别调用哪个落库方法），不测真实 CardKit 字段——
``AdminCardTransport`` 全程注入内存假实现。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from lingxi.core.admin.card_dispatch import CardDispatchResult, ConfirmCardDispatcher
from lingxi.core.admin.notification import AdminCardCreated, AdminCardDeliveryRejected
from lingxi.core.admin.pending_action import PendingAction, PendingActionStatus, PendingActionType

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)


def _pending() -> PendingAction:
    return PendingAction(
        id="pac_dispatch_test0000000000",
        action_type=PendingActionType.SUSPEND_USER,
        target_open_id="ou_target",
        target_state_snapshot="enabled",
        initiated_by_open_id="ou_admin",
        status=PendingActionStatus.PENDING,
        card_delivered=False,
        card_id=None,
        reason=None,
        created_at=NOW,
        confirm_deadline_at=NOW + timedelta(minutes=10),
        decided_at=None,
        decided_by_open_id=None,
    )


class _FakeTransport:
    def __init__(self, *, raises: Exception | None = None, card_id: str = "cardkit_id") -> None:
        self._raises = raises
        self._card_id = card_id
        self.create_calls: list[dict] = []
        self.update_calls: list[dict] = []

    def create(self, *, chat_id, thread_id, reply_to_message_id, card):
        self.create_calls.append(
            {
                "chat_id": chat_id,
                "thread_id": thread_id,
                "reply_to_message_id": reply_to_message_id,
                "card": card,
            }
        )
        if self._raises is not None:
            raise self._raises
        return AdminCardCreated(card_id=self._card_id, message_id="msg_1")

    def update(self, *, card_id, sequence, card):
        self.update_calls.append({"card_id": card_id, "sequence": sequence, "card": card})


class _FakeTracker:
    def __init__(self) -> None:
        self.delivered: list[tuple[str, str]] = []
        self.failed: list[str] = []

    def mark_card_delivered(self, *, pending_action_id: str, card_id: str) -> None:
        self.delivered.append((pending_action_id, card_id))

    def mark_send_failed(self, *, pending_action_id: str) -> None:
        self.failed.append(pending_action_id)


class ConfirmCardDispatcherTests(unittest.TestCase):
    def test_successful_send_marks_delivered_with_the_returned_card_id(self) -> None:
        transport = _FakeTransport(card_id="cardkit_abc")
        tracker = _FakeTracker()
        dispatcher = ConfirmCardDispatcher(transport=transport, tracker=tracker)
        pending = _pending()

        result = dispatcher.send(
            pending=pending, chat_id="oc_1", thread_id=None, reply_to_message_id="om_1"
        )

        self.assertEqual(result, CardDispatchResult(delivered=True))
        self.assertEqual(tracker.delivered, [(pending.id, "cardkit_abc")])
        self.assertEqual(tracker.failed, [])
        self.assertEqual(len(transport.create_calls), 1)
        self.assertEqual(transport.create_calls[0]["reply_to_message_id"], "om_1")

    def test_explicit_rejection_marks_send_failed_not_delivered(self) -> None:
        transport = _FakeTransport(raises=AdminCardDeliveryRejected("拒绝", code="9999"))
        tracker = _FakeTracker()
        dispatcher = ConfirmCardDispatcher(transport=transport, tracker=tracker)
        pending = _pending()

        result = dispatcher.send(
            pending=pending, chat_id="oc_1", thread_id=None, reply_to_message_id="om_1"
        )

        self.assertEqual(result, CardDispatchResult(delivered=False))
        self.assertEqual(tracker.failed, [pending.id])
        self.assertEqual(tracker.delivered, [])

    def test_uncertain_result_also_marks_send_failed(self) -> None:
        """结果不明（非 AdminCardDeliveryRejected 的任意异常，例如网络类异常、
        响应缺可回读标识）与明确拒绝在这里得到同一处理：合同"卡片发送失败时本次
        操作不执行，不根据失败原因推断"——本方法不区分"确定失败"与"结果不明"，
        两者都不能确定已经送达。"""

        transport = _FakeTransport(raises=RuntimeError("网络超时"))
        tracker = _FakeTracker()
        dispatcher = ConfirmCardDispatcher(transport=transport, tracker=tracker)
        pending = _pending()

        result = dispatcher.send(
            pending=pending, chat_id="oc_1", thread_id=None, reply_to_message_id="om_1"
        )

        self.assertFalse(result.delivered)
        self.assertEqual(tracker.failed, [pending.id])

    def test_card_is_sent_as_a_reply_to_the_triggering_message(self) -> None:
        transport = _FakeTransport()
        tracker = _FakeTracker()
        dispatcher = ConfirmCardDispatcher(transport=transport, tracker=tracker)
        pending = _pending()

        dispatcher.send(
            pending=pending, chat_id="oc_1", thread_id="tid_1", reply_to_message_id="om_specific"
        )

        call = transport.create_calls[0]
        self.assertEqual(call["reply_to_message_id"], "om_specific")
        self.assertEqual(call["thread_id"], "tid_1")
        self.assertEqual(call["chat_id"], "oc_1")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
