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


def _pending(
    *, status: PendingActionStatus = PendingActionStatus.EXECUTED, reason: str | None = None
) -> PendingAction:
    return PendingAction(
        id="pac_callback_test000000000",
        action_type=PendingActionType.SUSPEND_USER,
        target_open_id="ou_target",
        target_state_snapshot="enabled",
        initiated_by_open_id="ou_admin",
        status=status,
        card_delivered=True,
        card_id="cardkit_id_1",
        reason=reason,
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


class _FakeRecomputeTrigger:
    """``PermissionRecomputeTrigger`` 的假实现（Issue #438）：只记调用，不做任何
    真实重算——真实实现的编排在
    ``adapters/postgres_permission_recompute_trigger.py``，真库全链断言在
    ``tests/test_permission_recompute_trigger_postgres.py``。"""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self._raises = raises
        self.calls: list[PendingAction] = []

    def trigger(self, pending: PendingAction) -> None:
        self.calls.append(pending)
        if self._raises is not None:
            raise self._raises


class FakeDisplayNames:
    """``AdminDisplayNames`` 的内存假实现（Trace #469 S-1）：记录调用参数，
    返回不含入参 open_id 子串的固定展示值——同 ``tests/test_admin_card_
    dispatch.py`` 的同名假实现同一姿态与理由。"""

    def __init__(self) -> None:
        self.user_label_calls: list[str] = []
        self.company_label_calls: list[str] = []
        self.metric_label_calls: list[str] = []

    def user_label(self, *, open_id: str) -> str:
        self.user_label_calls.append(open_id)
        return "某某人（masked@example.com）"

    def company_label(self, *, company_id: str) -> str:
        self.company_label_calls.append(company_id)
        return f"某某公司（{company_id}）"

    def metric_label(self, *, metric_id: str) -> str:
        self.metric_label_calls.append(metric_id)
        return f"某某指标（{metric_id}）"


def _build_handler(
    *,
    pending_actions: _FakePendingActions,
    confirm_cards: _FakeCardTransport | None = None,
    group_notifier: _FakeGroupNotifier | None = None,
    group_chat_id: str | None = "oc_admin_group",
    audit: _RecordingAudit | None = None,
    management_actions: object | None = None,
    recompute_trigger: "_FakeRecomputeTrigger | None" = None,
    display_names: "FakeDisplayNames | None" = None,
) -> tuple[AdminCardCallbackHandler, _RecordingAudit]:
    audit = audit or _RecordingAudit()
    handler = AdminCardCallbackHandler(
        pending_actions=pending_actions,
        confirm_cards=confirm_cards or _FakeCardTransport(),
        group_notifier=group_notifier,
        group_chat_id=group_chat_id,
        audit=audit,
        display_names=display_names or FakeDisplayNames(),
        management_actions=management_actions,
        recompute_trigger=recompute_trigger,
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
        # Trace #469 S-1 TOP-3 接线修复：toast 直接用这次点击产生的
        # ``decision.message``（这里由 ``_FakeDecision.message`` 注入），不再
        # 由 ``_outcome_text(pending)`` 重新按持久状态派生一句不同的文案——
        # 见 ``core/admin/card_callback.py`` ``handle()`` 的对应注释。
        self.assertEqual(outcome["toast"]["content"], "已确认执行。")
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


class DisplayNamesWiringTests(unittest.TestCase):
    """Trace #469 S-1 TOP-1/C 接线断言：终态卡「目标：」字段与管理群广播都经
    ``AdminDisplayNames.user_label`` 解析 ``pending.target_open_id``，不回显
    open_id；待确认操作内部 ID（``pac_*``）只用于出站 ``dedupe_key`` 参数
    （审计/去重用途），不进入群通知的可见正文。"""

    def _executed_outcome(self, pending: PendingAction) -> _FakeOutcome:
        return _FakeOutcome(
            decision=_FakeDecision(
                kind=ConfirmResultKind.EXECUTE, ok=True, message="已确认执行。",
                terminal_status=PendingActionStatus.EXECUTED,
            ),
            pending=pending,
        )

    def test_terminal_card_and_group_notice_show_the_resolved_label_not_the_open_id(
        self,
    ) -> None:
        pending = _pending(status=PendingActionStatus.EXECUTED)
        pending_actions = _FakePendingActions()
        pending_actions.set_confirm_result(self._executed_outcome(pending))
        cards = _FakeCardTransport()
        group = _FakeGroupNotifier()
        display_names = FakeDisplayNames()
        handler, _audit = _build_handler(
            pending_actions=pending_actions,
            confirm_cards=cards,
            group_notifier=group,
            display_names=display_names,
        )

        handler.handle(
            operator_open_id="ou_admin",
            pending_action_id=pending.id,
            decision=DECISION_CONFIRM,
            trace_id="trc_display_1",
        )

        self.assertIn(pending.target_open_id, display_names.user_label_calls)
        terminal_card = cards.update_calls[0]["card"]
        self.assertIn("某某人（masked@example.com）", terminal_card.body)
        self.assertNotIn(pending.target_open_id, terminal_card.body)

        group_text = group.sent[0]["text"]
        self.assertIn("某某人（masked@example.com）", group_text)
        self.assertNotIn(pending.target_open_id, group_text)
        # 待确认操作内部 ID（pac_*）只留在 dedupe_key 里，不进入可见正文
        # （Trace #469 S-1 C 项）。
        self.assertEqual(group.sent[0]["dedupe_key"], pending.id)
        self.assertNotIn(pending.id, group_text)

    def test_failed_reason_is_translated_to_chinese_in_terminal_card_and_toast(self) -> None:
        pending = _pending(status=PendingActionStatus.FAILED, reason="role_revoked")
        pending_actions = _FakePendingActions()
        pending_actions.set_confirm_result(
            _FakeOutcome(
                decision=_FakeDecision(
                    kind=ConfirmResultKind.ROLE_REVOKED,
                    ok=False,
                    message="当前角色已无权执行该操作，请重新查询后再发起。",
                    terminal_status=PendingActionStatus.FAILED,
                ),
                pending=pending,
            )
        )
        cards = _FakeCardTransport()
        handler, _audit = _build_handler(pending_actions=pending_actions, confirm_cards=cards)

        outcome = handler.handle(
            operator_open_id="ou_admin",
            pending_action_id=pending.id,
            decision=DECISION_CONFIRM,
            trace_id="trc_display_2",
        )

        # 接线级修复（TOP-3）：toast 用这次点击的友好 decision.message。
        self.assertEqual(
            outcome["toast"]["content"], "当前角色已无权执行该操作，请重新查询后再发起。"
        )
        # 终态卡片正文用持久化的 pending.reason 翻译（describe_failed_reason），
        # 不直出机器码 "role_revoked"。
        terminal_card = cards.update_calls[0]["card"]
        self.assertIn("发起人当时角色已被撤销", terminal_card.body)
        self.assertNotIn("role_revoked", terminal_card.body)


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
        # handle() 文档分支 1 的 success/info 映射）。toast 内容用这次点击的
        # ``decision.message``（Trace #469 S-1 TOP-3），即 ``_FakeDecision`` 上面
        # 注入的那句话。
        self.assertEqual(outcome["toast"]["type"], "info")
        self.assertEqual(outcome["toast"]["content"], "已取消，未做任何变更。")
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

    def test_empty_identifier_is_rejected_without_calling_the_router(self) -> None:
        """opus 审查坐实并修复：空 ``identifier`` 此前不做任何校验就直接拼命令
        文本——``f"/admin grant_permission  {company_id} {metric_name} {reason}"``
        （两个连续空格）经 ``commands.py`` 的 ``str.split()`` 解析会把
        ``company_id`` 当成 ``identifier``、``metric_name`` 当成
        ``company_id``、``reason`` 的第一个词当成 ``metric_name``——只要
        ``reason`` 有至少两个词，这仍然是一条形状合法的 ``grant_permission``
        命令，只是目标已经完全变成别人。变异锚点：删掉本用例前的
        ``if not identifier`` 分支后，本用例（连同下面的复现用例）会由绿转红。
        """

        router = _FakeManagementRouter()
        handler, audit = _build_handler(
            pending_actions=_FakePendingActions(), management_actions=router
        )

        response = handler.handle_management_form_submit(
            operator_open_id="ou_admin",
            admin_action="grant",
            identifier="",
            company_id="1011",
            metric_name="daily_active",
            reason="特批",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
            trace_id="trc_1",
        )

        self.assertEqual(response["toast"]["type"], "error")
        self.assertEqual(router.route_calls, [], "校验必须在拼命令文本、调用 route() 之前拦住")
        self.assertIn(
            "admin.card_callback.management_missing_identifier", [a for a, _ in audit.records]
        )

    def test_before_the_fix_an_empty_identifier_would_shift_into_a_wellformed_but_wrong_command(
        self,
    ) -> None:
        """复现修复前的缺陷本身（不经过 handler 的校验，直接验证
        ``commands.py`` 会怎么解析那条被拼错的文本）：``identifier=""`` 时的
        命令文本左移之后，只要 ``reason`` 里有至少两个词，解析结果仍然是一条
        看起来完全合法的 ``GRANT_PERMISSION``，目标却已经变成了原本的
        ``company_id`` 取值——这正是"察觉不到目标已经变成别人"这句话的具体
        证据，钉在测试里防止有人以为"反正下游会拒绝"就足够安全。"""

        from lingxi.core.admin.commands import AdminCommandKind, parse_admin_command

        identifier = ""
        company_id = "1011"
        metric_name = "daily_active"
        reason = "特批 授权"
        shifted_text = f"/admin grant_permission {identifier} {company_id} {metric_name} {reason}"

        parsed = parse_admin_command(shifted_text)

        self.assertEqual(parsed.kind, AdminCommandKind.GRANT_PERMISSION, "被左移后仍然形状合法")
        self.assertEqual(parsed.identifier, company_id, "目标标识被错误地换成了原本的公司 ID")
        self.assertEqual(parsed.company_id, metric_name, "公司被错误地换成了原本的指标名")
        self.assertEqual(parsed.metric_name, "特批", "指标名被错误地换成了原因的第一个词")

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


class ManagementFormSubmitWhitespaceValidationTests(unittest.TestCase):
    """Trace #469 修复包 B，B-2：``identifier``/``company_id``/``metric_name``
    非空但含空白字符（含全角空格 U+3000）时，纵深校验必须在拼接命令文本之前
    拦住——审查实测 ``company_id="1011 sub_new_count"`` 能把 ``metric_name``
    静默左移替换成 ``sub_new_count``、把真正的 ``metric_name`` 挤进 ``reason``
    首词，当前调用点全部受控故不可达，本组用例是纵深加固。"""

    def test_identifier_containing_a_space_is_rejected_without_calling_the_router(self) -> None:
        router = _FakeManagementRouter()
        handler, audit = _build_handler(
            pending_actions=_FakePendingActions(), management_actions=router
        )

        response = handler.handle_management_form_submit(
            operator_open_id="ou_admin",
            admin_action="grant",
            identifier="ou_target extra_token",
            company_id="1011",
            metric_name="daily_active",
            reason="特批",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
            trace_id="trc_1",
        )

        self.assertEqual(response["toast"]["type"], "error")
        self.assertEqual(router.route_calls, [], "含空白字符的 identifier 不得拼进命令文本")
        self.assertIn(
            "admin.card_callback.management_missing_identifier", [a for a, _ in audit.records]
        )

    def test_company_id_containing_a_space_is_rejected_without_calling_the_router(self) -> None:
        """审查实测的具体复现场景：``company_id`` 里嵌一个空格 + 别的指标名，
        企图让 ``str.split()`` 把它左移进 ``metric_name``。"""

        router = _FakeManagementRouter()
        handler, _ = _build_handler(
            pending_actions=_FakePendingActions(), management_actions=router
        )

        response = handler.handle_management_form_submit(
            operator_open_id="ou_admin",
            admin_action="grant",
            identifier="ou_target",
            company_id="1011 sub_new_count",
            metric_name="daily_active",
            reason="特批",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
            trace_id="trc_1",
        )

        self.assertEqual(response["toast"]["type"], "error")
        self.assertEqual(router.route_calls, [], "含空白字符的 company_id 不得拼进命令文本")

    def test_metric_name_containing_a_space_is_rejected_without_calling_the_router(self) -> None:
        router = _FakeManagementRouter()
        handler, _ = _build_handler(
            pending_actions=_FakePendingActions(), management_actions=router
        )

        response = handler.handle_management_form_submit(
            operator_open_id="ou_admin",
            admin_action="suppress",
            identifier="ou_target",
            company_id="1011",
            metric_name="daily active",
            reason="特批",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
            trace_id="trc_1",
        )

        self.assertEqual(response["toast"]["type"], "error")
        self.assertEqual(router.route_calls, [], "含空白字符的 metric_name 不得拼进命令文本")

    def test_fullwidth_space_in_identifier_is_also_rejected(self) -> None:
        """全角空格（U+3000）与半角空格同样落进 ``str.isspace()``，不能被
        当成一个"看起来正常"的字符漏过校验。"""

        router = _FakeManagementRouter()
        handler, _ = _build_handler(
            pending_actions=_FakePendingActions(), management_actions=router
        )

        response = handler.handle_management_form_submit(
            operator_open_id="ou_admin",
            admin_action="grant",
            identifier="ou_target　extra",
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

    def test_well_formed_fields_without_whitespace_still_pass(self) -> None:
        """否定断言的另一半：正常取值（不含任何空白）不应该被这条新增校验
        误伤——与既有 ``test_grant_submission_constructs_the_equivalent_
        admin_command_text`` 覆盖同一条主路径，这里只确认新校验不引入
        误报。"""

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
            reason="特批 授权说明",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
            trace_id="trc_1",
        )

        self.assertEqual(response["toast"]["type"], "success")
        self.assertEqual(len(router.route_calls), 1)


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
            "/admin revoke_permission lpo_01JGFJJZ008XSHEADGG8V74SPC 管理卡逐行撤销",
        )
        self.assertEqual(response["toast"]["type"], "success")
        self.assertNotIn("card", response)

    def test_position_group_revoke_uses_group_id_and_group_reason(self) -> None:
        router = _FakeManagementRouter()
        handler, _ = _build_handler(
            pending_actions=_FakePendingActions(), management_actions=router
        )

        response = handler.handle_management_revoke(
            operator_open_id="ou_admin",
            override_id="",
            permission_group_id="lpg_01M1C90YDGMTY567GDTZZJ4C5E",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
            trace_id="trc_group_revoke",
        )

        self.assertEqual(response["toast"]["type"], "success")
        self.assertEqual(
            router.route_calls[0]["text"],
            "/admin revoke_permission lpg_01M1C90YDGMTY567GDTZZJ4C5E 管理卡撤销职位范围授权",
        )

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

    def test_empty_override_id_is_rejected_without_calling_the_router(self) -> None:
        """与表单提交同一条纪律：``override_id`` 为空时不校验就拼命令文本，
        变异锚点：删掉本用例前的 ``if not override_id`` 分支后，本用例会由绿
        转红。"""

        router = _FakeManagementRouter()
        handler, audit = _build_handler(
            pending_actions=_FakePendingActions(), management_actions=router
        )

        response = handler.handle_management_revoke(
            operator_open_id="ou_admin",
            override_id="",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
            trace_id="trc_1",
        )

        self.assertEqual(response["toast"]["type"], "error")
        self.assertEqual(router.route_calls, [], "校验必须在拼命令文本、调用 route() 之前拦住")
        self.assertIn(
            "admin.card_callback.management_missing_override_id", [a for a, _ in audit.records]
        )


class ManagementCardTerminologyTests(unittest.TestCase):
    """管理卡撤销路径的术语统一（Trace #469 S-1 遗漏，收尾批 L4a 实测发现）。

    #439 的 PM 裁定是**全链路**统一为「补充授权 / 屏蔽指标 / 撤销」，S-1 只扫到
    三张展示词表；管理卡逐行按钮写「撤销」、服务端补的固定原因却仍写「收回」，
    同一次操作在确认卡「原因」行 / 终态卡 / 群通知里出现两套说法——产品负责人
    点一次撤销按钮就会当场看到（TOP-7 防倒退）。本类把两处出口逐字钉死。
    """

    def test_default_revoke_reason_uses_the_unified_verb_not_the_retired_one(self) -> None:
        """变异锚点：把 ``_MANAGEMENT_CARD_REVOKE_REASON`` 改回「管理卡逐行收回」
        （或任何含「收回」的取值），本用例立即由绿转红。"""

        from lingxi.core.admin.card_callback import _MANAGEMENT_CARD_REVOKE_REASON
        from lingxi.core.admin.notification import _ACTION_LABEL

        revoke_label = _ACTION_LABEL[PendingActionType.LOCAL_PERMISSION_REVOKE]
        self.assertIn(
            revoke_label,
            _MANAGEMENT_CARD_REVOKE_REASON,
            "固定原因必须逐字包含 notification._ACTION_LABEL 里的撤销术语",
        )
        self.assertNotIn("收回", _MANAGEMENT_CARD_REVOKE_REASON)

    def test_default_revoke_reason_stays_free_of_whitespace(self) -> None:
        """形状不变量（``handle_management_revoke`` 的注释依赖它）：固定原因不含
        空白，才不会在拼进 ``/admin revoke_permission <id> <reason...>`` 时改变
        token 切分行为。改文案是文案改动，不得顺带改变解析形状。"""

        from lingxi.core.admin.card_callback import _MANAGEMENT_CARD_REVOKE_REASON

        self.assertTrue(_MANAGEMENT_CARD_REVOKE_REASON.strip())
        self.assertEqual(_MANAGEMENT_CARD_REVOKE_REASON.split(), [_MANAGEMENT_CARD_REVOKE_REASON])

    def test_missing_override_id_toast_uses_the_unified_verb(self) -> None:
        """变异锚点：把这句 toast 改回「未识别到待收回的授权行……」，本用例由绿
        转红。管理员看到的每一句提示都不得把他导向一个已经不存在的「收回」入口。
        """

        handler, _ = _build_handler(
            pending_actions=_FakePendingActions(), management_actions=_FakeManagementRouter()
        )

        response = handler.handle_management_revoke(
            operator_open_id="ou_admin",
            override_id="",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
            trace_id="trc_1",
        )

        content = response["toast"]["content"]
        self.assertIn("撤销", content)
        self.assertNotIn("收回", content)


class RecomputeTriggerWiringTests(unittest.TestCase):
    """确认执行成功后的定向权限重算钩子（Issue #438）：只在这次点击**首次**让
    操作真正执行成功（``status is EXECUTED`` 且 ``terminal_status`` 非空）时触发；
    失败降级、不影响已经落库的确认结果；未装配时静默跳过（与
    ``group_notifier=None`` 同一姿态）。真实重算规则本身不在这里测（见
    ``tests/test_targeted_permission_recompute.py``），本文件只测"什么时候调用
    它一次"这条编排。
    """

    def _confirm_result(self, pending: PendingAction) -> _FakeOutcome:
        return _FakeOutcome(
            decision=_FakeDecision(
                kind=ConfirmResultKind.EXECUTE, ok=True, message="已确认执行。",
                terminal_status=PendingActionStatus.EXECUTED,
            ),
            pending=pending,
        )

    def test_executed_confirm_triggers_recompute_exactly_once(self) -> None:
        pending = _pending(status=PendingActionStatus.EXECUTED)
        pending_actions = _FakePendingActions()
        pending_actions.set_confirm_result(self._confirm_result(pending))
        trigger = _FakeRecomputeTrigger()
        handler, audit = _build_handler(pending_actions=pending_actions, recompute_trigger=trigger)

        outcome = handler.handle(
            operator_open_id="ou_admin",
            pending_action_id=pending.id,
            decision=DECISION_CONFIRM,
            trace_id="trc_recompute_1",
        )

        self.assertEqual(outcome["toast"]["type"], "success")
        # toast 内容用这次点击的 decision.message（Trace #469 S-1 TOP-3），即
        # 上面 `_confirm_result` 注入的那句话；"即时生效"这句持久化措辞仍然
        # 出现在终态卡片正文里（`_outcome_text`，见 `test_terminal_card_...`
        # 一类断言），toast 与卡片正文自本批起可以是两句不同的话，见
        # `card_callback.py::handle()` 的对应注释。
        self.assertEqual(outcome["toast"]["content"], "已确认执行。")
        self.assertEqual(trigger.calls, [pending])

    def test_cancel_does_not_trigger_recompute(self) -> None:
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
        trigger = _FakeRecomputeTrigger()
        handler, _audit = _build_handler(pending_actions=pending_actions, recompute_trigger=trigger)

        handler.handle(
            operator_open_id="ou_admin",
            pending_action_id=pending.id,
            decision=DECISION_CANCEL,
            trace_id="trc_recompute_2",
        )

        self.assertEqual(trigger.calls, [])

    def test_idempotent_replay_does_not_trigger_recompute_again(self) -> None:
        """幂等重放（``terminal_status`` 为空，与群通知同一去重判据）不重复触发。"""

        pending = _pending(status=PendingActionStatus.EXECUTED)
        pending_actions = _FakePendingActions()
        pending_actions.set_confirm_result(
            _FakeOutcome(
                decision=_FakeDecision(
                    kind=ConfirmResultKind.ALREADY_TERMINAL, ok=False, message="已确认执行。",
                    terminal_status=None,
                ),
                pending=pending,
            )
        )
        trigger = _FakeRecomputeTrigger()
        handler, _audit = _build_handler(pending_actions=pending_actions, recompute_trigger=trigger)

        handler.handle(
            operator_open_id="ou_admin",
            pending_action_id=pending.id,
            decision=DECISION_CONFIRM,
            trace_id="trc_recompute_3",
        )

        self.assertEqual(trigger.calls, [])

    def test_recompute_failure_does_not_propagate_and_is_audited(self) -> None:
        """降级回每日批：失败只记审计，不影响已经落库的确认结果（硬纪律）。"""

        pending = _pending(status=PendingActionStatus.EXECUTED)
        pending_actions = _FakePendingActions()
        pending_actions.set_confirm_result(self._confirm_result(pending))
        trigger = _FakeRecomputeTrigger(raises=RuntimeError("重算适配器炸了"))
        handler, audit = _build_handler(pending_actions=pending_actions, recompute_trigger=trigger)

        outcome = handler.handle(
            operator_open_id="ou_admin",
            pending_action_id=pending.id,
            decision=DECISION_CONFIRM,
            trace_id="trc_recompute_4",
        )

        self.assertEqual(outcome["toast"]["type"], "success", "定向重算失败不改变确认结果本身")
        self.assertIn(
            "admin.card_callback.recompute_trigger_failed",
            [action for action, _ in audit.records],
        )

    def test_missing_recompute_trigger_is_a_silent_noop(self) -> None:
        """未装配（``None``）时静默跳过——与 ``group_notifier=None`` 同一姿态。"""

        pending = _pending(status=PendingActionStatus.EXECUTED)
        pending_actions = _FakePendingActions()
        pending_actions.set_confirm_result(self._confirm_result(pending))
        handler, audit = _build_handler(pending_actions=pending_actions, recompute_trigger=None)

        outcome = handler.handle(
            operator_open_id="ou_admin",
            pending_action_id=pending.id,
            decision=DECISION_CONFIRM,
            trace_id="trc_recompute_5",
        )

        self.assertEqual(outcome["toast"]["type"], "success")
        self.assertNotIn(
            "admin.card_callback.recompute_trigger_failed",
            [action for action, _ in audit.records],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
