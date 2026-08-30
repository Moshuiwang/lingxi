"""``core/admin/card_callback.AdminCardCallbackHandler`` 的编排断言（Issue #96 S-M-02）。

只测编排逻辑：卡片终态更新与管理群通知各自在什么条件下触发（含否定断言：非本人点击
不产生任何终态更新或通知；伪造的待确认操作 ID 安全拒绝；审计写入失败时不假装已经
更新了卡片）。真实状态机分支见 ``tests/test_pending_action.py``；真实事务见
``tests/test_pending_action_postgres.py``。

**``handle()`` 的返回值即飞书卡片回调的应答载荷（Issue #96 卡片回调应答修复）**：
本文件的每一条用例都断言 ``outcome`` 这个字典的形状——``outcome["toast"]["type"]``、
``outcome["toast"]["content"]``，以及是否存在 ``outcome["card"]``——而不是像批次二
那样断言一个内部 ``CardCallbackOutcome.ok``/``.message`` 结构。这不是测试风格的
偏好：这个返回值本身就是要直接交给 SDK marshal 进应答帧的东西（见
``core/admin/card_callback.py`` 模块文档），断言错了形状，回归就网不住。
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from lingxi.core.admin.card_callback import AdminCardCallbackHandler
from lingxi.core.admin.notification import DECISION_CANCEL, DECISION_CONFIRM
from lingxi.core.admin.pending_action import (
    ConfirmResultKind,
    PendingAction,
    PendingActionAuditWriteFailed,
    PendingActionStatus,
    PendingActionTransientFailure,
    PendingActionType,
)

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)


def _pending(*, status: PendingActionStatus = PendingActionStatus.EXECUTED) -> PendingAction:
    return PendingAction(
        id="pac_callback_test000000000",
        action_type=PendingActionType.SUSPEND_USER,
        target_open_id="ou_target",
        target_state_snapshot="enabled",
        initiated_by_open_id="ou_admin",
        status=status,
        card_delivered=True,
        card_id="cardkit_id_1",
        reason=None,
        created_at=NOW,
        confirm_deadline_at=NOW + timedelta(minutes=10),
        decided_at=NOW,
        decided_by_open_id="ou_admin",
    )


@dataclass(frozen=True)
class _FakeDecision:
    kind: object
    ok: bool
    message: str
    terminal_status: PendingActionStatus | None


@dataclass(frozen=True)
class _FakeOutcome:
    decision: _FakeDecision
    pending: PendingAction | None


class _FakePendingActions:
    def __init__(self) -> None:
        self.confirm_calls: list[dict] = []
        self.cancel_calls: list[dict] = []
        self.next_card_sequence_calls: list[str] = []
        self._confirm_result: _FakeOutcome | Exception | None = None
        self._cancel_result: _FakeOutcome | Exception | None = None
        self._next_sequence = 0

    def set_confirm_result(self, result) -> None:
        self._confirm_result = result

    def set_cancel_result(self, result) -> None:
        self._cancel_result = result

    def confirm(self, *, pending_action_id: str, clicker_open_id: str):
        self.confirm_calls.append(
            {"pending_action_id": pending_action_id, "clicker_open_id": clicker_open_id}
        )
        if isinstance(self._confirm_result, Exception):
            raise self._confirm_result
        assert self._confirm_result is not None
        return self._confirm_result

    def cancel(self, *, pending_action_id: str, clicker_open_id: str):
        self.cancel_calls.append(
            {"pending_action_id": pending_action_id, "clicker_open_id": clicker_open_id}
        )
        if isinstance(self._cancel_result, Exception):
            raise self._cancel_result
        assert self._cancel_result is not None
        return self._cancel_result

    def next_card_sequence(self, *, pending_action_id: str) -> int:
        self.next_card_sequence_calls.append(pending_action_id)
        self._next_sequence += 1
        return self._next_sequence


class _FakeCardTransport:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self._raises = raises
        self.update_calls: list[dict] = []

    def create(self, **kwargs):  # pragma: no cover - 本类不测试 create
        raise NotImplementedError

    def update(self, *, card_id: str, sequence: int, card) -> None:
        self.update_calls.append({"card_id": card_id, "sequence": sequence, "card": card})
        if self._raises is not None:
            raise self._raises


class _FakeGroupNotifier:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self._raises = raises
        self.sent: list[dict] = []

    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None:
        self.sent.append({"chat_id": chat_id, "text": text, "dedupe_key": dedupe_key})
        if self._raises is not None:
            raise self._raises


class _RecordingAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, fields))


def _build_handler(
    *,
    pending_actions: _FakePendingActions,
    confirm_cards: _FakeCardTransport | None = None,
    group_notifier: _FakeGroupNotifier | None = None,
    group_chat_id: str | None = "oc_admin_group",
    audit: _RecordingAudit | None = None,
    management_actions: object | None = None,
) -> tuple[AdminCardCallbackHandler, _RecordingAudit]:
    audit = audit or _RecordingAudit()
    handler = AdminCardCallbackHandler(
        pending_actions=pending_actions,
        confirm_cards=confirm_cards or _FakeCardTransport(),
        group_notifier=group_notifier,
        group_chat_id=group_chat_id,
        audit=audit,
        management_actions=management_actions,
    )
    return handler, audit


class _FakeRouteOutcome:
    def __init__(self, *, handled: bool, content_key: str = "", reply_text: str = "") -> None:
        self.handled = handled
        self.content_key = content_key
        self.reply_text = reply_text


class _FakeManagementRouter:
    """``ManagementActionRouter`` 的假实现：记录每次 ``route()`` 调用的完整
    命令文本，回放预设结论——用于验证 ``handle_management_form_submit``/
    ``handle_management_revoke`` 是否构造出了正确的等价命令文本，不需要真实
    数据库（真库端到端集成见 ``tests/test_admin_management_integration_
    postgres.py``）。"""

    def __init__(self, *, outcome: _FakeRouteOutcome | None = None) -> None:
        self.route_calls: list[dict[str, object]] = []
        self._outcome = outcome or _FakeRouteOutcome(
            handled=True,
            content_key="admin.write_action_pending",
            reply_text="已生成待确认操作，请查收你的飞书私聊确认卡片（十分钟内有效）。",
        )

    def route(
        self, *, open_id: str, text: str, trace_id: str, chat_id="", thread_id=None, message_id=""
    ) -> _FakeRouteOutcome:
        self.route_calls.append(
            {
                "open_id": open_id,
                "text": text,
                "trace_id": trace_id,
                "chat_id": chat_id,
                "thread_id": thread_id,
                "message_id": message_id,
            }
        )
        return self._outcome


class UnknownDecisionTests(unittest.TestCase):
    def test_unknown_decision_literal_is_rejected_without_touching_the_store(self) -> None:
        pending_actions = _FakePendingActions()
        handler, audit = _build_handler(pending_actions=pending_actions)

        outcome = handler.handle(
            operator_open_id="ou_admin",
            pending_action_id="pac_x",
            decision="delete_everything",
            trace_id="trc_1",
        )

        self.assertEqual(outcome["toast"]["type"], "error")
        self.assertEqual(outcome["toast"]["content"], "操作不存在或已失效")
        self.assertNotIn("card", outcome, "不存在的动作不能带任何卡片")
        self.assertEqual(pending_actions.confirm_calls, [])
        self.assertEqual(pending_actions.cancel_calls, [])
        self.assertIn("admin.card_callback.unknown_decision", [action for action, _ in audit.records])


class ConfirmExecutionTests(unittest.TestCase):
    def test_successful_execution_updates_card_and_notifies_group_once(self) -> None:
        pending = _pending(status=PendingActionStatus.EXECUTED)
        pending_actions = _FakePendingActions()
        pending_actions.set_confirm_result(
            _FakeOutcome(
                decision=_FakeDecision(
                    kind=ConfirmResultKind.EXECUTE,
                    ok=True,
                    message="已确认执行。",
                    terminal_status=PendingActionStatus.EXECUTED,
                ),
                pending=pending,
            )
        )
        cards = _FakeCardTransport()
        group = _FakeGroupNotifier()
        handler, audit = _build_handler(
            pending_actions=pending_actions, confirm_cards=cards, group_notifier=group
        )

        outcome = handler.handle(
            operator_open_id="ou_admin",
            pending_action_id=pending.id,
            decision=DECISION_CONFIRM,
            trace_id="trc_2",
        )

        self.assertEqual(outcome["toast"]["type"], "success", "executed 状态的 toast 必须是 success")
        self.assertEqual(outcome["toast"]["content"], "已确认执行")
        self.assertEqual(outcome["card"]["type"], "raw")
        # 终态卡 data 必须是对象（dict），不是字符串——飞书要的是可以直接被
        # SDK JSON 序列化的结构，不是我们提前 json.dumps 过的字符串。
        self.assertIsInstance(outcome["card"]["data"], dict)
        self.assertEqual(outcome["card"]["data"]["schema"], "2.0")
        self.assertEqual(len(cards.update_calls), 1)
        self.assertEqual(cards.update_calls[0]["card_id"], "cardkit_id_1")
        self.assertTrue(cards.update_calls[0]["card"].is_terminal)
        # sequence 必须真的从 pending_actions 端口换来，不能是硬编码或漏传
        # （外部审查交叉裁定，opus P2-1）。
        self.assertEqual(pending_actions.next_card_sequence_calls, [pending.id])
        self.assertEqual(cards.update_calls[0]["sequence"], 1)
        self.assertEqual(len(group.sent), 1)
        self.assertEqual(group.sent[0]["dedupe_key"], pending.id)


class NotInitiatorTests(unittest.TestCase):
    """否定断言：非本人点击 → 不更新卡片、不通知管理群（``pending`` 仍是
    ``PENDING``，``terminal_status`` 为 ``None``）。"""

    def test_wrong_clicker_does_not_update_card_or_notify_group(self) -> None:
        pending = _pending(status=PendingActionStatus.PENDING)
        pending_actions = _FakePendingActions()
        pending_actions.set_confirm_result(
            _FakeOutcome(
                decision=_FakeDecision(
                    kind=ConfirmResultKind.NOT_INITIATOR,
                    ok=False,
                    message="只有发起该操作的管理员本人可以确认。",
                    terminal_status=None,
                ),
                pending=pending,
            )
        )
        cards = _FakeCardTransport()
        group = _FakeGroupNotifier()
        handler, audit = _build_handler(
            pending_actions=pending_actions, confirm_cards=cards, group_notifier=group
        )

        outcome = handler.handle(
            operator_open_id="ou_someone_else",
            pending_action_id=pending.id,
            decision=DECISION_CONFIRM,
            trace_id="trc_3",
        )

        self.assertEqual(outcome["toast"]["type"], "error")
        self.assertEqual(outcome["toast"]["content"], "只有发起人本人可以操作此卡片")
        self.assertNotIn(
            "card", outcome, "不能把任何卡片（含终态卡）展示给非发起人，也不能改动发起人的卡"
        )
        self.assertEqual(cards.update_calls, [])
        self.assertEqual(group.sent, [])


class IdempotentReplayTests(unittest.TestCase):
    """重复点击一个早已是终态的操作：卡片视觉收敛（允许再次 update，无害），
    但**不**再发第二条群通知（避免刷屏）。"""

    def test_already_terminal_replay_refreshes_card_but_does_not_renotify(self) -> None:
        pending = _pending(status=PendingActionStatus.EXECUTED)
        pending_actions = _FakePendingActions()
        pending_actions.set_confirm_result(
            _FakeOutcome(
                decision=_FakeDecision(
                    kind=ConfirmResultKind.ALREADY_TERMINAL,
                    ok=False,
                    message="该操作已经执行过，不会重复执行。",
                    terminal_status=None,  # 幂等重放不产生新的终态转移
                ),
                pending=pending,
            )
        )
        cards = _FakeCardTransport()
        group = _FakeGroupNotifier()
        handler, audit = _build_handler(
            pending_actions=pending_actions, confirm_cards=cards, group_notifier=group
        )

        outcome = handler.handle(
            operator_open_id="ou_admin",
            pending_action_id=pending.id,
            decision=DECISION_CONFIRM,
            trace_id="trc_4",
        )

        # 幂等重放：这次点击本身没有让状态"新"落进终态（decision.ok=False），
        # 但 pending.status 现在确实是 executed——应答形状只看当前状态，因此仍是
        # success + 终态卡（与首次确认时看到的应答一致，见 handle() 文档分支 1）。
        self.assertEqual(outcome["toast"]["type"], "success")
        self.assertIn("card", outcome)
        self.assertEqual(len(cards.update_calls), 1, "卡片视觉仍应尝试收敛到终态")
        self.assertEqual(group.sent, [], "重复点击不得重复通知管理群")


class ForgedPendingActionIdTests(unittest.TestCase):
    """否定断言：卡片回调伪造（不存在的动作 ID）→ 拒绝，安全处理不崩溃。"""

    def test_never_existed_pending_action_id_is_handled_safely(self) -> None:
        pending_actions = _FakePendingActions()
        pending_actions.set_confirm_result(
            _FakeOutcome(
                decision=_FakeDecision(
                    kind=ConfirmResultKind.NOT_FOUND, ok=False, message="未找到该待确认操作。",
                    terminal_status=None,
                ),
                pending=None,
            )
        )
        cards = _FakeCardTransport()
        group = _FakeGroupNotifier()
        handler, audit = _build_handler(
            pending_actions=pending_actions, confirm_cards=cards, group_notifier=group
        )

        outcome = handler.handle(
            operator_open_id="ou_admin",
            pending_action_id="pac_forged_does_not_exist",
            decision=DECISION_CONFIRM,
            trace_id="trc_5",
        )

        self.assertEqual(outcome["toast"]["type"], "error")
        self.assertEqual(outcome["toast"]["content"], "操作不存在或已失效")
        self.assertNotIn("card", outcome)
        self.assertEqual(cards.update_calls, [])
        self.assertEqual(group.sent, [])
        self.assertIn("admin.card_callback.not_found", [action for action, _ in audit.records])


class AuditWriteFailureTests(unittest.TestCase):
    """否定断言：审计 sink 异常 → 不执行（失败关闭），不更新卡片、不通知管理群，
    因为对应的数据库事务已经整体回滚——这次点击结构上"没有发生过"。"""

    def test_audit_write_failure_does_not_touch_card_or_group(self) -> None:
        pending_actions = _FakePendingActions()
        pending_actions.set_confirm_result(
            PendingActionAuditWriteFailed("确认操作的审计写入失败，事务已回滚，操作未执行")
        )
        cards = _FakeCardTransport()
        group = _FakeGroupNotifier()
        handler, audit = _build_handler(
            pending_actions=pending_actions, confirm_cards=cards, group_notifier=group
        )

        outcome = handler.handle(
            operator_open_id="ou_admin",
            pending_action_id="pac_audit_fail",
            decision=DECISION_CONFIRM,
            trace_id="trc_6",
        )

        self.assertEqual(outcome["toast"]["type"], "error")
        self.assertEqual(outcome["toast"]["content"], "系统繁忙请重试")
        self.assertNotIn("card", outcome)
        self.assertEqual(cards.update_calls, [])
        self.assertEqual(group.sent, [])
        self.assertIn(
            "admin.card_callback.audit_write_failed", [action for action, _ in audit.records]
        )


class TransientFailureTests(unittest.TestCase):
    """批次 4 F1（Issue #304，opus 审查）：数据库瞬时故障（死锁、锁等待超时或其他
    操作性错误）与审计写入失败同一姿态——不更新卡片、不通知管理群，因为对应的
    数据库事务已经整体回滚（这次点击结构上"没有发生过"），但文案与审计动作名
    刻意不同（见 ``card_callback.py`` 的 ``handle()`` 文档分支 5），便于事后按
    故障类别检索。"""

    def test_transient_failure_does_not_touch_card_or_group(self) -> None:
        pending_actions = _FakePendingActions()
        pending_actions.set_confirm_result(PendingActionTransientFailure("DeadlockDetected"))
        cards = _FakeCardTransport()
        group = _FakeGroupNotifier()
        handler, audit = _build_handler(
            pending_actions=pending_actions, confirm_cards=cards, group_notifier=group
        )

        outcome = handler.handle(
            operator_open_id="ou_admin",
            pending_action_id="pac_transient_fail",
            decision=DECISION_CONFIRM,
            trace_id="trc_7",
        )

        self.assertEqual(outcome["toast"]["type"], "error")
        self.assertEqual(outcome["toast"]["content"], "系统繁忙，请稍后重试")
        self.assertNotIn("card", outcome)
        self.assertEqual(cards.update_calls, [])
        self.assertEqual(group.sent, [])
        transient_records = [
            fields for action, fields in audit.records
            if action == "admin.card_callback.transient_failure"
        ]
        self.assertEqual(len(transient_records), 1)
        self.assertEqual(transient_records[0]["classification"], "DeadlockDetected")

    def test_transient_failure_toast_differs_from_audit_write_failure_toast(self) -> None:
        """两条分支产品含义相同（可以直接重试）但文案故意不同——回归防止未来
        有人为了"看起来一致"把两条 toast 文案悄悄合并成同一句。"""

        pending_actions = _FakePendingActions()
        pending_actions.set_confirm_result(PendingActionTransientFailure("LockNotAvailable"))
        handler, _audit = _build_handler(pending_actions=pending_actions)

        outcome = handler.handle(
            operator_open_id="ou_admin",
            pending_action_id="pac_transient_fail_2",
            decision=DECISION_CONFIRM,
            trace_id="trc_8",
        )

        self.assertNotEqual(outcome["toast"]["content"], "系统繁忙请重试")
        self.assertEqual(outcome["toast"]["content"], "系统繁忙，请稍后重试")

    def test_cancel_path_also_translates_transient_failure(self) -> None:
        pending_actions = _FakePendingActions()
        pending_actions.set_cancel_result(PendingActionTransientFailure("OperationalError"))
        handler, audit = _build_handler(pending_actions=pending_actions)

        outcome = handler.handle(
            operator_open_id="ou_admin",
            pending_action_id="pac_transient_fail_3",
            decision=DECISION_CANCEL,
            trace_id="trc_9",
        )

        self.assertEqual(outcome["toast"]["type"], "error")
        self.assertEqual(outcome["toast"]["content"], "系统繁忙，请稍后重试")
        self.assertIn(
            "admin.card_callback.transient_failure", [action for action, _ in audit.records]
        )


class BestEffortSideEffectFailureTests(unittest.TestCase):
    """卡片视觉更新失败、群通知失败都不影响已经落库的业务结果——两者是尽力而为
    的展示层副作用，不是本次点击是否"发生过"的判据。"""

    def test_card_update_failure_does_not_propagate(self) -> None:
        pending = _pending(status=PendingActionStatus.EXECUTED)
        pending_actions = _FakePendingActions()
        pending_actions.set_confirm_result(
            _FakeOutcome(
                decision=_FakeDecision(
                    kind=ConfirmResultKind.EXECUTE, ok=True, message="已确认执行。",
                    terminal_status=PendingActionStatus.EXECUTED,
                ),
                pending=pending,
            )
        )
        cards = _FakeCardTransport(raises=RuntimeError("卡片更新网络异常"))
        group = _FakeGroupNotifier()
        handler, audit = _build_handler(
            pending_actions=pending_actions, confirm_cards=cards, group_notifier=group
        )

        outcome = handler.handle(
            operator_open_id="ou_admin",
            pending_action_id=pending.id,
            decision=DECISION_CONFIRM,
            trace_id="trc_7",
        )

        # 核心回归断言（Issue #96 根因场景）：出带外 update() 调用失败，不得让
        # 应答本身也丢掉终态卡——应答换卡是主路径，出带外更新只是冗余纵深，见
        # core/admin/card_callback.py 模块文档「载体 #96」与
        # _update_card_to_terminal 方法文档。
        self.assertEqual(outcome["toast"]["type"], "success", "卡片视觉更新失败不改变业务结果本身")
        self.assertIn("card", outcome, "出带外 update() 失败不得连累应答本身携带正确的终态卡")
        self.assertEqual(outcome["card"]["data"]["schema"], "2.0")
        self.assertEqual(len(group.sent), 1, "群通知仍应正常发出")
        self.assertIn(
            "admin.card_callback.card_update_failed", [action for action, _ in audit.records]
        )

    def test_group_notify_failure_does_not_propagate(self) -> None:
        pending = _pending(status=PendingActionStatus.EXECUTED)
        pending_actions = _FakePendingActions()
        pending_actions.set_confirm_result(
            _FakeOutcome(
                decision=_FakeDecision(
                    kind=ConfirmResultKind.EXECUTE, ok=True, message="已确认执行。",
                    terminal_status=PendingActionStatus.EXECUTED,
                ),
                pending=pending,
            )
        )
        cards = _FakeCardTransport()
        group = _FakeGroupNotifier(raises=RuntimeError("群消息发送失败"))
        handler, audit = _build_handler(
            pending_actions=pending_actions, confirm_cards=cards, group_notifier=group
        )

        outcome = handler.handle(
            operator_open_id="ou_admin",
            pending_action_id=pending.id,
            decision=DECISION_CONFIRM,
            trace_id="trc_8",
        )

        self.assertEqual(outcome["toast"]["type"], "success")
        self.assertIn("card", outcome)
        self.assertEqual(len(cards.update_calls), 1, "卡片更新仍应正常发出")
        self.assertIn(
            "admin.card_callback.group_notify_failed", [action for action, _ in audit.records]
        )

    def test_missing_group_notifier_skips_notification_without_error(self) -> None:
        pending = _pending(status=PendingActionStatus.EXECUTED)
        pending_actions = _FakePendingActions()
        pending_actions.set_confirm_result(
            _FakeOutcome(
                decision=_FakeDecision(
                    kind=ConfirmResultKind.EXECUTE, ok=True, message="已确认执行。",
                    terminal_status=PendingActionStatus.EXECUTED,
                ),
                pending=pending,
            )
        )
        cards = _FakeCardTransport()
        handler, audit = _build_handler(
            pending_actions=pending_actions, confirm_cards=cards, group_notifier=None
        )

        outcome = handler.handle(
            operator_open_id="ou_admin",
            pending_action_id=pending.id,
            decision=DECISION_CONFIRM,
            trace_id="trc_9",
        )

        self.assertEqual(outcome["toast"]["type"], "success")
        self.assertIn("card", outcome)
        self.assertEqual(len(cards.update_calls), 1)


class CancelPathTests(unittest.TestCase):
    def test_cancel_decision_routes_to_the_cancel_method(self) -> None:
        pending = _pending(status=PendingActionStatus.CANCELLED)
        pending_actions = _FakePendingActions()
        pending_actions.set_cancel_result(
            _FakeOutcome(
                decision=_FakeDecision(
                    kind="cancel", ok=True, message="已取消，未做任何变更。",
                    terminal_status=PendingActionStatus.CANCELLED,
                ),
                pending=pending,
            )
        )
        cards = _FakeCardTransport()
        group = _FakeGroupNotifier()
        handler, audit = _build_handler(
            pending_actions=pending_actions, confirm_cards=cards, group_notifier=group
        )

        outcome = handler.handle(
            operator_open_id="ou_admin",
            pending_action_id=pending.id,
            decision=DECISION_CANCEL,
            trace_id="trc_10",
        )

        # cancelled 不是 executed：toast 类型必须是 info，不是 success（见
        # handle() 文档分支 1 的 success/info 映射）。
        self.assertEqual(outcome["toast"]["type"], "info")
        self.assertEqual(outcome["toast"]["content"], "已取消")
        self.assertIn("card", outcome)
        self.assertEqual(pending_actions.confirm_calls, [])
        self.assertEqual(len(pending_actions.cancel_calls), 1)
        self.assertEqual(len(cards.update_calls), 1)
        self.assertEqual(len(group.sent), 1)


class ManagementFormSubmitTests(unittest.TestCase):
    """``AdminCardCallbackHandler.handle_management_form_submit``（#439 B 档）：
    单元层用假 ``ManagementActionRouter`` 验证命令文本构造是否正确；真实
    prepare()/确认卡发送的真库集成见
    ``tests/test_admin_management_integration_postgres.py``。"""

    def test_grant_submission_constructs_the_equivalent_admin_command_text(self) -> None:
        router = _FakeManagementRouter()
        handler, _ = _build_handler(
            pending_actions=_FakePendingActions(), management_actions=router
        )

        response = handler.handle_management_form_submit(
            operator_open_id="ou_admin",
            admin_action="grant",
            identifier="ou_target",
            company_id="1011",
            metric_name="daily_active",
            reason="特批授权",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
            trace_id="trc_1",
        )

        self.assertEqual(len(router.route_calls), 1)
        call = router.route_calls[0]
        self.assertEqual(
            call["text"], "/admin grant_permission ou_target 1011 daily_active 特批授权"
        )
        self.assertEqual(call["open_id"], "ou_admin")
        self.assertEqual(call["message_id"], "om_1")
        self.assertEqual(response["toast"]["type"], "success")
        self.assertNotIn("card", response)

    def test_suppress_submission_uses_the_suppress_subcommand(self) -> None:
        router = _FakeManagementRouter()
        handler, _ = _build_handler(
            pending_actions=_FakePendingActions(), management_actions=router
        )

        handler.handle_management_form_submit(
            operator_open_id="ou_admin",
            admin_action="suppress",
            identifier="ou_target",
            company_id="1011",
            metric_name="daily_active",
            reason="临时抑制",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
            trace_id="trc_1",
        )

        self.assertIn("suppress_permission", router.route_calls[0]["text"])

    def test_reason_containing_slash_admin_text_cannot_smuggle_a_second_command(self) -> None:
        """否定断言（注入面）：``reason`` 里嵌一段看起来像另一条命令的文本，
        不能让重构后的命令文本被解析成任何别的命令——``reason`` 结构上永远是
        ``grant_permission``/``suppress_permission`` 固定形状的最后一段，
        ``commands.py`` 的语法门只会把它整体当成 reason 拼接消费。"""

        router = _FakeManagementRouter()
        handler, _ = _build_handler(
            pending_actions=_FakePendingActions(), management_actions=router
        )

        handler.handle_management_form_submit(
            operator_open_id="ou_admin",
            admin_action="grant",
            identifier="ou_target",
            company_id="1011",
            metric_name="daily_active",
            reason="正常原因 /admin suspend ou_victim",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
            trace_id="trc_1",
        )

        text = router.route_calls[0]["text"]
        # 整条文本仍然只是一次 grant_permission 调用，注入的 "/admin suspend"
        # 只是 reason 字段内容的一部分，不会被当成第二条命令解析——真正的证明
        # 是 core/admin/commands.py 的语法层面：parse_admin_command(text) 只会
        # 产出一个 GRANT_PERMISSION 结果，reason 整体拼接含那段文本。
        from lingxi.core.admin.commands import AdminCommandKind, parse_admin_command

        parsed = parse_admin_command(text)
        self.assertEqual(parsed.kind, AdminCommandKind.GRANT_PERMISSION)
        self.assertIn("/admin suspend ou_victim", parsed.reason)

    def test_not_wired_replies_unavailable_without_crashing(self) -> None:
        handler, _ = _build_handler(pending_actions=_FakePendingActions())

        response = handler.handle_management_form_submit(
            operator_open_id="ou_admin",
            admin_action="grant",
            identifier="ou_target",
            company_id="1011",
            metric_name="daily_active",
            reason="特批",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
            trace_id="trc_1",
        )

        self.assertEqual(response["toast"]["type"], "error")

    def test_unknown_admin_action_is_rejected_without_calling_the_router(self) -> None:
        router = _FakeManagementRouter()
        handler, audit = _build_handler(
            pending_actions=_FakePendingActions(), management_actions=router
        )

        response = handler.handle_management_form_submit(
            operator_open_id="ou_admin",
            admin_action="delete_everything",
            identifier="ou_target",
            company_id="1011",
            metric_name="daily_active",
            reason="特批",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
            trace_id="trc_1",
        )

        self.assertEqual(response["toast"]["type"], "error")
        self.assertEqual(router.route_calls, [])
        self.assertIn("admin.card_callback.management_unknown_action", [a for a, _ in audit.records])

    def test_blank_reason_is_rejected_without_calling_the_router(self) -> None:
        router = _FakeManagementRouter()
        handler, _ = _build_handler(
            pending_actions=_FakePendingActions(), management_actions=router
        )

        response = handler.handle_management_form_submit(
            operator_open_id="ou_admin",
            admin_action="grant",
            identifier="ou_target",
            company_id="1011",
            metric_name="daily_active",
            reason="   ",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
            trace_id="trc_1",
        )

        self.assertEqual(response["toast"]["type"], "error")
        self.assertEqual(router.route_calls, [])

    def test_empty_company_or_metric_is_rejected_without_calling_the_router(self) -> None:
        router = _FakeManagementRouter()
        handler, _ = _build_handler(
            pending_actions=_FakePendingActions(), management_actions=router
        )

        handler.handle_management_form_submit(
            operator_open_id="ou_admin",
            admin_action="grant",
            identifier="ou_target",
            company_id="",
            metric_name="daily_active",
            reason="特批",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
            trace_id="trc_1",
        )

        self.assertEqual(router.route_calls, [])

    def test_route_not_handled_is_reported_as_an_error_toast(self) -> None:
        """结构上只会发生在"点击这一刻当前角色恰好已被撤销"——``route()``
        内部重新判定身份，``handled=False`` 时不能假装成功。"""

        router = _FakeManagementRouter(outcome=_FakeRouteOutcome(handled=False))
        handler, _ = _build_handler(
            pending_actions=_FakePendingActions(), management_actions=router
        )

        response = handler.handle_management_form_submit(
            operator_open_id="ou_admin",
            admin_action="grant",
            identifier="ou_target",
            company_id="1011",
            metric_name="daily_active",
            reason="特批",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
            trace_id="trc_1",
        )

        self.assertEqual(response["toast"]["type"], "error")


class ManagementRevokeClickTests(unittest.TestCase):
    """``AdminCardCallbackHandler.handle_management_revoke``（#439 B 档）：
    单元层用假 ``ManagementActionRouter``；真库集成同上。"""

    def test_revoke_click_constructs_the_equivalent_revoke_command_with_the_default_reason(
        self,
    ) -> None:
        router = _FakeManagementRouter()
        handler, _ = _build_handler(
            pending_actions=_FakePendingActions(), management_actions=router
        )

        response = handler.handle_management_revoke(
            operator_open_id="ou_admin",
            override_id="lpo_01JGFJJZ008XSHEADGG8V74SPC",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
            trace_id="trc_1",
        )

        self.assertEqual(len(router.route_calls), 1)
        self.assertEqual(
            router.route_calls[0]["text"],
            "/admin revoke_permission lpo_01JGFJJZ008XSHEADGG8V74SPC 管理卡逐行收回",
        )
        self.assertEqual(response["toast"]["type"], "success")
        self.assertNotIn("card", response)

    def test_not_wired_replies_unavailable_without_crashing(self) -> None:
        handler, _ = _build_handler(pending_actions=_FakePendingActions())

        response = handler.handle_management_revoke(
            operator_open_id="ou_admin",
            override_id="lpo_01JGFJJZ008XSHEADGG8V74SPC",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
            trace_id="trc_1",
        )

        self.assertEqual(response["toast"]["type"], "error")

    def test_rejection_from_the_router_is_surfaced_as_an_error_toast(self) -> None:
        router = _FakeManagementRouter(
            outcome=_FakeRouteOutcome(
                handled=True,
                content_key="admin.write_action_rejected",
                reply_text="未找到匹配的当前生效本地覆盖……",
            )
        )
        handler, _ = _build_handler(
            pending_actions=_FakePendingActions(), management_actions=router
        )

        response = handler.handle_management_revoke(
            operator_open_id="ou_admin",
            override_id="lpo_01JGFJJZ008XSHEADGG8V74SPC",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
            trace_id="trc_1",
        )

        self.assertEqual(response["toast"]["type"], "error")
        self.assertIn("未找到匹配", response["toast"]["content"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
