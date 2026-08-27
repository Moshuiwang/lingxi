"""``core/admin/router.AdminCommandRouter``：身份判定 → 角色判定 → 命令解析 →
执行 → 审计（Issue #95 S-M-01）。全部用注入的假端口，不连真实数据库。

认领断言：
- V-管理-21：私聊分流的判定逻辑本体——实时读表、默认拒绝、拒绝也审计。
- V-管理-24：未登记/已撤销/未装配三种"无有效条目"落回既有行为的判定本体
  （本文件覆盖前两种；"未装配"是 gateway 管线层面的可选参数，见
  ``test_gateway_pipeline_admin.py``）。
- V-管理-26：审计覆盖全部结论分支，含拒绝、内部错误、未知命令，不只在成功时留痕。
- 否定断言（验证与门禁 §八 第 4 点）：默认拒绝用一个不在任何名单中的未知对象证明
  （``test_unregistered_open_id_is_rejected_and_produces_zero_query_calls``），不能只
  证明"已知的专用账号被正确处理"这一个已知对象。
"""

from __future__ import annotations

import unittest

from lingxi.core.admin.pending_action import PendingAction, PendingActionStatus, PendingActionType
from lingxi.core.admin.registry import ALL_ADMIN_ROLES, AdminRegistryEntry, AdminRole
from lingxi.core.admin.router import (
    AdminCommandRouter,
    AdminEventView,
    AdminUserStatusView,
)


class FakeAudit:
    def __init__(self, *, raise_error: bool = False) -> None:
        self.records: list[tuple[str, dict]] = []
        self.raise_error = raise_error

    def record(self, action: str, /, **fields: object) -> None:
        if self.raise_error:
            raise RuntimeError("模拟审计器本身故障（例如审计落库失败）")
        self.records.append((action, dict(fields)))

    def actions(self) -> list[str]:
        return [action for action, _ in self.records]


class FakeRegistry:
    def __init__(self, entries: dict[str, AdminRegistryEntry] | None = None) -> None:
        self.calls: list[str] = []
        self._entries = entries or {}
        self.raise_error = False

    def active_entry(self, *, open_id: str) -> AdminRegistryEntry | None:
        self.calls.append(open_id)
        if self.raise_error:
            raise RuntimeError("模拟数据库连接失败")
        return self._entries.get(open_id)


class FakeQueries:
    def __init__(
        self,
        *,
        users: dict[str, AdminUserStatusView] | None = None,
        events: list[AdminEventView] | None = None,
        raise_on_user: bool = False,
    ) -> None:
        self.user_calls: list[str] = []
        self.event_calls: list[dict[str, object]] = []
        self._users = users or {}
        self._events = events or []
        self._raise_on_user = raise_on_user

    def user_status(self, *, identifier: str) -> AdminUserStatusView | None:
        self.user_calls.append(identifier)
        if self._raise_on_user:
            raise RuntimeError("模拟查询失败")
        return self._users.get(identifier)

    def recent_events(
        self, *, identifier: str | None, window_hours: int, limit: int
    ) -> list[AdminEventView]:
        self.event_calls.append(
            {"identifier": identifier, "window_hours": window_hours, "limit": limit}
        )
        return list(self._events)


class _FakePrepareDecision:
    def __init__(self, *, ok: bool, message: str = "", code: str = "") -> None:
        self.ok = ok
        self.message = message
        self.code = code


class _FakePrepareOutcome:
    def __init__(self, *, decision: _FakePrepareDecision, pending: PendingAction | None = None) -> None:
        self.decision = decision
        self.pending = pending


class FakePendingActions:
    """``PendingActionPreparer`` 的内存假实现：只记录调用参数并回放预设结论。"""

    def __init__(self, *, outcome: _FakePrepareOutcome | None = None) -> None:
        self.prepare_calls: list[dict[str, object]] = []
        self._outcome = outcome

    def prepare(
        self,
        *,
        action_type: PendingActionType,
        target_open_id: str,
        initiated_by_open_id: str,
        company_id: str | None = None,
        metric_name: str | None = None,
        reason: str | None = None,
    ) -> _FakePrepareOutcome:
        self.prepare_calls.append(
            {
                "action_type": action_type,
                "target_open_id": target_open_id,
                "initiated_by_open_id": initiated_by_open_id,
                "company_id": company_id,
                "metric_name": metric_name,
                "reason": reason,
            }
        )
        assert self._outcome is not None
        return self._outcome


class _FakeCardDispatchResult:
    def __init__(self, *, delivered: bool) -> None:
        self.delivered = delivered


class FakeConfirmCards:
    """``ConfirmCardSender`` 的内存假实现。"""

    def __init__(self, *, delivered: bool = True) -> None:
        self.send_calls: list[dict[str, object]] = []
        self._delivered = delivered

    def send(
        self,
        *,
        pending: PendingAction,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
    ) -> _FakeCardDispatchResult:
        self.send_calls.append(
            {
                "pending": pending,
                "chat_id": chat_id,
                "thread_id": thread_id,
                "reply_to_message_id": reply_to_message_id,
            }
        )
        return _FakeCardDispatchResult(delivered=self._delivered)


def _prepared_pending(
    *, action_type: PendingActionType = PendingActionType.SUSPEND_USER
) -> PendingAction:
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)
    return PendingAction(
        id="pac_router_test0000000000",
        action_type=action_type,
        target_open_id="ou_target",
        target_state_snapshot="enabled",
        initiated_by_open_id=ADMIN_OPEN_ID,
        status=PendingActionStatus.PENDING,
        card_delivered=False,
        card_id=None,
        reason=None,
        created_at=now,
        confirm_deadline_at=now + timedelta(minutes=10),
        decided_at=None,
        decided_by_open_id=None,
    )


ADMIN_OPEN_ID = "ou_delegated_subject"


def _full_admin_entry() -> AdminRegistryEntry:
    return AdminRegistryEntry(
        feishu_open_id=ADMIN_OPEN_ID,
        label="delegated_subject",
        roles=ALL_ADMIN_ROLES,
        entry_status="active",
    )


def _router(
    *,
    registry: FakeRegistry | None = None,
    queries: FakeQueries | None = None,
    audit: FakeAudit | None = None,
    pending_actions: FakePendingActions | None = None,
    confirm_cards: FakeConfirmCards | None = None,
) -> tuple[AdminCommandRouter, FakeRegistry, FakeQueries, FakeAudit]:
    reg = registry or FakeRegistry({ADMIN_OPEN_ID: _full_admin_entry()})
    qry = queries or FakeQueries()
    aud = audit or FakeAudit()
    return (
        AdminCommandRouter(
            registry=reg,
            queries=qry,
            audit=aud,
            pending_actions=pending_actions,
            confirm_cards=confirm_cards,
        ),
        reg,
        qry,
        aud,
    )


class DefaultDenyTests(unittest.TestCase):
    def test_unregistered_open_id_is_rejected_and_produces_zero_query_calls(self) -> None:
        """默认拒绝的否定断言：一个不在任何名单中的、彻头彻尾编造的 open_id——
        不是"已知危险对象"，是登记表压根没听说过的标识。"""

        router, registry, queries, audit = _router(registry=FakeRegistry({}))

        outcome = router.route(
            open_id="ou_never_registered_9f3e", text="/admin user ou_1", trace_id="trc_1"
        )

        self.assertFalse(outcome.handled)
        # 登记表确实被实时问过一次（真实读表，不是靠调用方自觉短路）。
        self.assertEqual(registry.calls, ["ou_never_registered_9f3e"])
        # 零业务查询：既没有查用户状态，也没有查审计事件——不存在的管理员不能
        # 触发任何一条下游查询。
        self.assertEqual(queries.user_calls, [])
        self.assertEqual(queries.event_calls, [])
        # 拒绝也审计。
        self.assertEqual(audit.actions(), ["admin.command.rejected"])
        self.assertEqual(audit.records[0][1]["reason"], "not_authorized")

    def test_revoked_entry_is_rejected(self) -> None:
        entry = AdminRegistryEntry(
            feishu_open_id=ADMIN_OPEN_ID,
            label="delegated_subject",
            roles=ALL_ADMIN_ROLES,
            entry_status="revoked",
        )
        router, _, queries, audit = _router(registry=FakeRegistry({ADMIN_OPEN_ID: entry}))

        outcome = router.route(open_id=ADMIN_OPEN_ID, text="/admin help", trace_id="trc_1")

        self.assertFalse(outcome.handled)
        self.assertEqual(queries.user_calls, [])
        self.assertEqual(audit.actions(), ["admin.command.rejected"])

    def test_zero_role_active_entry_is_rejected(self) -> None:
        entry = AdminRegistryEntry(
            feishu_open_id=ADMIN_OPEN_ID, label="x", roles=frozenset(), entry_status="active"
        )
        router, _, _, audit = _router(registry=FakeRegistry({ADMIN_OPEN_ID: entry}))

        outcome = router.route(open_id=ADMIN_OPEN_ID, text="/admin help", trace_id="trc_1")

        self.assertFalse(outcome.handled)
        self.assertEqual(audit.actions(), ["admin.command.rejected"])

    def test_registry_lookup_failure_fails_closed(self) -> None:
        """判定本身失败（例如数据库连接异常）必须失败关闭为"不是管理员"，
        而不是抛出异常打断 gateway 管线。"""

        registry = FakeRegistry({ADMIN_OPEN_ID: _full_admin_entry()})
        registry.raise_error = True
        router, _, queries, audit = _router(registry=registry)

        outcome = router.route(open_id=ADMIN_OPEN_ID, text="/admin help", trace_id="trc_1")

        self.assertFalse(outcome.handled)
        self.assertEqual(queries.user_calls, [])
        self.assertEqual(audit.actions(), ["admin.command.lookup_failed"])


class RealTimeJudgmentTests(unittest.TestCase):
    def test_every_call_triggers_a_fresh_registry_read(self) -> None:
        """"实时判定、不缓存"的行为证据：同一 open_id 连续两次调用各自触发一次
        独立的 ``active_entry`` 读取，第二次读取可以返回与第一次不同的结果
        （模拟角色收回后新请求立即拒绝）。"""

        registry = FakeRegistry({ADMIN_OPEN_ID: _full_admin_entry()})
        router, _, _, _ = _router(registry=registry)

        first = router.route(open_id=ADMIN_OPEN_ID, text="/admin help", trace_id="t1")
        self.assertTrue(first.handled)

        # 模拟撤销：登记表在两次调用之间发生了变化。
        registry._entries.pop(ADMIN_OPEN_ID)

        second = router.route(open_id=ADMIN_OPEN_ID, text="/admin help", trace_id="t2")

        self.assertFalse(second.handled)
        self.assertEqual(registry.calls, [ADMIN_OPEN_ID, ADMIN_OPEN_ID])


class HelpCommandTests(unittest.TestCase):
    def test_help_lists_current_roles(self) -> None:
        router, _, _, audit = _router()

        outcome = router.route(open_id=ADMIN_OPEN_ID, text="/admin help", trace_id="t1")

        self.assertTrue(outcome.handled)
        self.assertIn("ops_admin", outcome.reply_text)
        self.assertIn("permission_admin", outcome.reply_text)
        self.assertIn("super_admin", outcome.reply_text)
        self.assertEqual(audit.actions(), ["admin.command.help"])


class QueryUserCommandTests(unittest.TestCase):
    def test_found_user_status_reported(self) -> None:
        queries = FakeQueries(
            users={
                "ou_target": AdminUserStatusView(
                    identifier="ou_target",
                    provisioning_state="active",
                    account_state="enabled",
                    permission_version=3,
                    updated_at="2026-08-24T00:00:00+00:00",
                )
            }
        )
        router, _, _, audit = _router(queries=queries)

        outcome = router.route(
            open_id=ADMIN_OPEN_ID, text="/admin user ou_target", trace_id="t1"
        )

        self.assertTrue(outcome.handled)
        self.assertIn("ou_target", outcome.reply_text)
        self.assertIn("active", outcome.reply_text)
        self.assertEqual(queries.user_calls, ["ou_target"])
        self.assertEqual(audit.actions(), ["admin.command.query_user"])
        self.assertTrue(audit.records[0][1]["found"])

    def test_not_found_user_reported_without_crashing(self) -> None:
        router, _, queries, audit = _router()

        outcome = router.route(
            open_id=ADMIN_OPEN_ID, text="/admin user ou_missing", trace_id="t1"
        )

        self.assertTrue(outcome.handled)
        self.assertIn("ou_missing", outcome.reply_text)
        self.assertFalse(audit.records[0][1]["found"])

    def test_query_failure_yields_internal_error_reply_not_crash(self) -> None:
        router, _, _, audit = _router(queries=FakeQueries(raise_on_user=True))

        outcome = router.route(
            open_id=ADMIN_OPEN_ID, text="/admin user ou_target", trace_id="t1"
        )

        self.assertTrue(outcome.handled)
        self.assertTrue(outcome.reply_text)
        self.assertEqual(audit.actions(), ["admin.command.internal_error"])


class QueryAuditCommandTests(unittest.TestCase):
    def test_events_rendered_and_query_scoped_by_default_window(self) -> None:
        events = [
            AdminEventView(
                received_at="2026-08-24T01:00:00+00:00",
                event_type="im.message.receive_v1",
                handled_as="not_provisioned",
                trace_id="trc_abc",
            )
        ]
        router, _, queries, audit = _router(queries=FakeQueries(events=events))

        outcome = router.route(open_id=ADMIN_OPEN_ID, text="/admin audit", trace_id="t1")

        self.assertTrue(outcome.handled)
        self.assertIn("trc_abc", outcome.reply_text)
        self.assertEqual(queries.event_calls, [{"identifier": None, "window_hours": 24, "limit": 20}])
        self.assertEqual(audit.actions(), ["admin.command.query_audit"])

    def test_no_events_reported_clearly(self) -> None:
        router, _, _, _ = _router()

        outcome = router.route(open_id=ADMIN_OPEN_ID, text="/admin audit ou_x 48", trace_id="t1")

        self.assertTrue(outcome.handled)
        self.assertIn("没有找到", outcome.reply_text)


class AuditFailureFailsClosedTests(unittest.TestCase):
    """审计器本身抛异常时，路由仍必须返回确定性拒绝，不得跟着崩溃或误放行
    （opus 批量审查 P2 修复）。四个场景覆盖 `_safe_record` 的全部调用点：
    判定失败的拒绝分支、默认拒绝分支、成功命令分支（原本 `handled=True`，
    此处必须收敛为 `handled=False`）、以及已确认管理员但执行失败的分支。
    """

    def test_lookup_failure_branch_does_not_propagate_the_audit_exception(self) -> None:
        registry = FakeRegistry({ADMIN_OPEN_ID: _full_admin_entry()})
        registry.raise_error = True
        router, _, _, _ = _router(registry=registry, audit=FakeAudit(raise_error=True))

        # 不抛异常：审计器故障不得从 route() 里逃出去打断 gateway 管线。
        outcome = router.route(open_id=ADMIN_OPEN_ID, text="/admin help", trace_id="t1")

        self.assertFalse(outcome.handled)

    def test_default_deny_branch_does_not_propagate_the_audit_exception(self) -> None:
        router, _, _, _ = _router(
            registry=FakeRegistry({}), audit=FakeAudit(raise_error=True)
        )

        outcome = router.route(
            open_id="ou_never_registered", text="/admin help", trace_id="t1"
        )

        self.assertFalse(outcome.handled)

    def test_a_successful_command_degrades_to_rejection_when_audit_fails(self) -> None:
        """关键场景：判定与执行本来都成功（本会得到 `handled=True` 的帮助文案），
        但审计器这一步坏了——结论必须收敛为确定性拒绝，不能让一次没有审计记录
        的放行悄悄发生。"""

        router, _, _, _ = _router(audit=FakeAudit(raise_error=True))

        outcome = router.route(open_id=ADMIN_OPEN_ID, text="/admin help", trace_id="t1")

        self.assertFalse(outcome.handled)
        self.assertEqual(outcome.reply_text, "")

    def test_dispatch_failure_branch_does_not_propagate_the_audit_exception(self) -> None:
        """已确认是管理员，但执行阶段抛异常（`admin.command.internal_error` 分支）
        ——这一步的审计也失败时，同样必须收敛为确定性拒绝，不能让"执行失败但
        没记上审计"悄悄发生，也不能让异常本身逃出 route()。"""

        router, _, _, _ = _router(
            queries=FakeQueries(raise_on_user=True), audit=FakeAudit(raise_error=True)
        )

        outcome = router.route(
            open_id=ADMIN_OPEN_ID, text="/admin user ou_target", trace_id="t1"
        )

        self.assertFalse(outcome.handled)


class UnknownCommandTests(unittest.TestCase):
    def test_unknown_command_still_gets_a_reply_and_is_audited(self) -> None:
        router, _, queries, audit = _router()

        outcome = router.route(open_id=ADMIN_OPEN_ID, text="不知道说什么", trace_id="t1")

        self.assertTrue(outcome.handled)
        self.assertTrue(outcome.reply_text)
        self.assertEqual(queries.user_calls, [])
        self.assertEqual(queries.event_calls, [])
        self.assertEqual(audit.actions(), ["admin.command.unknown"])

    def test_injection_shaped_command_is_treated_as_unknown_not_executed(self) -> None:
        """命令面拒绝越权面：即使发送者是完全合法的管理员，注入形态的文本也只会
        落到 UNKNOWN 分支，绝不会被当成查询条件传给任何下游查询函数。"""

        router, _, queries, audit = _router()

        outcome = router.route(
            open_id=ADMIN_OPEN_ID,
            text="/admin user 1; DROP TABLE app_user;--",
            trace_id="t1",
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(queries.user_calls, [])
        self.assertEqual(audit.actions(), ["admin.command.unknown"])


class SuspendResumeDispatchTests(unittest.TestCase):
    """``suspend``/``resume`` 写命令编排（Issue #96 S-M-02）：只建待确认操作 +
    发确认卡片，不直接改变业务状态——本文件全程注入假 ``pending_actions``/
    ``confirm_cards``，真正的状态变更断言在 ``tests/test_pending_action.py``
    （纯逻辑）与 ``tests/test_pending_action_postgres.py``（真库事务）。
    """

    def test_unregistered_sender_gets_default_deny_same_as_read_only_commands(self) -> None:
        """否定断言（S-M-02 完成标准之一）：普通用户/未登记者发 suspend/resume
        默认拒绝，沿用 S-M-01 既有的登记表判定——不因为是写命令就走一条不同的
        身份判定路径。"""

        pending_actions = FakePendingActions(
            outcome=_FakePrepareOutcome(decision=_FakePrepareDecision(ok=True))
        )
        confirm_cards = FakeConfirmCards()
        router, registry, _, audit = _router(
            registry=FakeRegistry({}),
            pending_actions=pending_actions,
            confirm_cards=confirm_cards,
        )

        outcome = router.route(
            open_id="ou_never_registered",
            text="/admin suspend ou_target",
            trace_id="t1",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
        )

        self.assertFalse(outcome.handled)
        self.assertEqual(pending_actions.prepare_calls, [])
        self.assertEqual(confirm_cards.send_calls, [])

    def test_not_wired_replies_unavailable_without_crashing(self) -> None:
        router, _, _, audit = _router()  # pending_actions/confirm_cards 均未传入

        outcome = router.route(
            open_id=ADMIN_OPEN_ID,
            text="/admin suspend ou_target",
            trace_id="t1",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
        )

        self.assertTrue(outcome.handled)
        self.assertIn("不可用", outcome.reply_text)

    def test_missing_message_id_is_rejected_before_touching_pending_actions(self) -> None:
        pending_actions = FakePendingActions(
            outcome=_FakePrepareOutcome(decision=_FakePrepareDecision(ok=True))
        )
        confirm_cards = FakeConfirmCards()
        router, _, _, audit = _router(
            pending_actions=pending_actions, confirm_cards=confirm_cards
        )

        outcome = router.route(
            open_id=ADMIN_OPEN_ID,
            text="/admin suspend ou_target",
            trace_id="t1",
            chat_id="oc_1",
            thread_id=None,
            message_id="",  # 没有可回复的消息
        )

        self.assertTrue(outcome.handled)
        self.assertIn("无法发送确认卡片", outcome.reply_text)
        self.assertEqual(pending_actions.prepare_calls, [])

    def test_partial_role_entry_is_rejected_before_reaching_write_dispatch_at_all(self) -> None:
        """一个只持有部分角色的条目（MVP 结构上不该出现，但核对逻辑不能依赖这个
        假设）在 ``route()`` 顶层的 ``is_authorized_admin`` 就已经被拒绝——`
        suspend`/`resume` 与 `help`/`user`/`audit` 共用同一个身份判定入口，不存在
        绕开顶层判定单独进入写命令分支的路径。"""

        partial_entry = AdminRegistryEntry(
            feishu_open_id=ADMIN_OPEN_ID,
            label="future-admin",
            roles=frozenset({AdminRole.OPS_ADMIN, AdminRole.SUPER_ADMIN}),
            entry_status="active",
        )
        pending_actions = FakePendingActions(
            outcome=_FakePrepareOutcome(decision=_FakePrepareDecision(ok=True))
        )
        confirm_cards = FakeConfirmCards()
        router, _, _, audit = _router(
            registry=FakeRegistry({ADMIN_OPEN_ID: partial_entry}),
            pending_actions=pending_actions,
            confirm_cards=confirm_cards,
        )

        outcome = router.route(
            open_id=ADMIN_OPEN_ID,
            text="/admin suspend ou_target",
            trace_id="t1",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
        )

        self.assertFalse(outcome.handled)
        self.assertEqual(pending_actions.prepare_calls, [])
        self.assertEqual(confirm_cards.send_calls, [])

    def test_prepare_rejection_is_reported_without_sending_a_card(self) -> None:
        pending_actions = FakePendingActions(
            outcome=_FakePrepareOutcome(
                decision=_FakePrepareDecision(
                    ok=False, code="not_found", message="未找到该用户记录。"
                )
            )
        )
        confirm_cards = FakeConfirmCards()
        router, _, _, audit = _router(
            pending_actions=pending_actions, confirm_cards=confirm_cards
        )

        outcome = router.route(
            open_id=ADMIN_OPEN_ID,
            text="/admin suspend ou_missing",
            trace_id="t1",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.reply_text, "未找到该用户记录。")
        self.assertEqual(confirm_cards.send_calls, [])
        self.assertEqual(pending_actions.prepare_calls[0]["target_open_id"], "ou_missing")
        self.assertEqual(
            pending_actions.prepare_calls[0]["action_type"], PendingActionType.SUSPEND_USER
        )
        self.assertEqual(
            pending_actions.prepare_calls[0]["initiated_by_open_id"], ADMIN_OPEN_ID
        )

    def test_card_send_failure_is_reported_and_operation_does_not_proceed(self) -> None:
        pending = _prepared_pending()
        pending_actions = FakePendingActions(
            outcome=_FakePrepareOutcome(
                decision=_FakePrepareDecision(ok=True), pending=pending
            )
        )
        confirm_cards = FakeConfirmCards(delivered=False)
        router, _, _, audit = _router(
            pending_actions=pending_actions, confirm_cards=confirm_cards
        )

        outcome = router.route(
            open_id=ADMIN_OPEN_ID,
            text="/admin suspend ou_target",
            trace_id="t1",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
        )

        self.assertTrue(outcome.handled)
        self.assertIn("发送失败", outcome.reply_text)
        self.assertEqual(len(confirm_cards.send_calls), 1)

    def test_successful_suspend_dispatch_creates_exactly_one_pending_action_and_one_card(
        self,
    ) -> None:
        pending = _prepared_pending(action_type=PendingActionType.SUSPEND_USER)
        pending_actions = FakePendingActions(
            outcome=_FakePrepareOutcome(
                decision=_FakePrepareDecision(ok=True), pending=pending
            )
        )
        confirm_cards = FakeConfirmCards(delivered=True)
        router, _, _, audit = _router(
            pending_actions=pending_actions, confirm_cards=confirm_cards
        )

        outcome = router.route(
            open_id=ADMIN_OPEN_ID,
            text="/admin suspend ou_target",
            trace_id="t1",
            chat_id="oc_1",
            thread_id="thread_1",
            message_id="om_1",
        )

        self.assertTrue(outcome.handled)
        self.assertIn("待确认", outcome.reply_text)
        self.assertEqual(len(pending_actions.prepare_calls), 1)
        self.assertEqual(len(confirm_cards.send_calls), 1)
        send_call = confirm_cards.send_calls[0]
        self.assertIs(send_call["pending"], pending)
        self.assertEqual(send_call["chat_id"], "oc_1")
        self.assertEqual(send_call["thread_id"], "thread_1")
        self.assertEqual(send_call["reply_to_message_id"], "om_1")
        self.assertEqual(audit.actions(), ["admin.command.suspend_user"])
        self.assertEqual(audit.records[0][1]["pending_action_id"], pending.id)

    def test_successful_resume_dispatch(self) -> None:
        pending = _prepared_pending(action_type=PendingActionType.RESUME_USER)
        pending_actions = FakePendingActions(
            outcome=_FakePrepareOutcome(decision=_FakePrepareDecision(ok=True), pending=pending)
        )
        confirm_cards = FakeConfirmCards(delivered=True)
        router, _, _, audit = _router(
            pending_actions=pending_actions, confirm_cards=confirm_cards
        )

        outcome = router.route(
            open_id=ADMIN_OPEN_ID,
            text="/admin resume ou_target",
            trace_id="t1",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(
            pending_actions.prepare_calls[0]["action_type"], PendingActionType.RESUME_USER
        )
        self.assertEqual(audit.actions(), ["admin.command.resume_user"])

    def test_help_text_now_mentions_suspend_and_resume(self) -> None:
        router, _, _, _ = _router()

        outcome = router.route(open_id=ADMIN_OPEN_ID, text="/admin help", trace_id="t1")

        self.assertIn("suspend", outcome.reply_text)
        self.assertIn("resume", outcome.reply_text)


class GrantSuppressPermissionDispatchTests(unittest.TestCase):
    """``grant_permission``/``suppress_permission`` 写命令编排（#319 S-P-1b 设计
    卡）：与 ``suspend``/``resume`` 共用同一套 ``_dispatch_write_action`` 骨架，
    额外核对①新命令不绕过既有身份判定、⑤自我目标防呆。
    """

    def _grant_text(self, target: str = "ou_target") -> str:
        return f"/admin grant_permission {target} 1011 daily_active 特批"

    def _suppress_text(self, target: str = "ou_target") -> str:
        return f"/admin suppress_permission {target} 1011 daily_active 特批"

    def test_unregistered_sender_is_default_denied_same_as_read_only_commands(self) -> None:
        """①新命令不绕过 ``is_authorized_admin``：未登记者发 grant_permission
        必须走与 help/user/audit/suspend/resume 完全相同的默认拒绝路径——不因为
        是新命令就走一条独立的身份判定分支。"""

        pending_actions = FakePendingActions(
            outcome=_FakePrepareOutcome(decision=_FakePrepareDecision(ok=True))
        )
        confirm_cards = FakeConfirmCards()
        router, registry, _, audit = _router(
            registry=FakeRegistry({}),
            pending_actions=pending_actions,
            confirm_cards=confirm_cards,
        )

        outcome = router.route(
            open_id="ou_never_registered",
            text=self._grant_text(),
            trace_id="t1",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
        )

        self.assertFalse(outcome.handled)
        self.assertEqual(pending_actions.prepare_calls, [])
        self.assertEqual(confirm_cards.send_calls, [])

    def test_partial_role_entry_is_rejected_before_reaching_write_dispatch(self) -> None:
        """同 suspend/resume：只持有部分角色的条目在 ``route()`` 顶层就被拒绝，
        走不到 grant/suppress 的写命令分支。"""

        partial_entry = AdminRegistryEntry(
            feishu_open_id=ADMIN_OPEN_ID,
            label="future-admin",
            roles=frozenset({AdminRole.OPS_ADMIN, AdminRole.SUPER_ADMIN}),
            entry_status="active",
        )
        pending_actions = FakePendingActions(
            outcome=_FakePrepareOutcome(decision=_FakePrepareDecision(ok=True))
        )
        confirm_cards = FakeConfirmCards()
        router, _, _, audit = _router(
            registry=FakeRegistry({ADMIN_OPEN_ID: partial_entry}),
            pending_actions=pending_actions,
            confirm_cards=confirm_cards,
        )

        outcome = router.route(
            open_id=ADMIN_OPEN_ID,
            text=self._grant_text(),
            trace_id="t1",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
        )

        self.assertFalse(outcome.handled)
        self.assertEqual(pending_actions.prepare_calls, [])

    def test_self_target_grant_is_rejected_without_calling_prepare(self) -> None:
        """⑤自我目标防呆：管理员对自己发起授权被拒绝，且 ``prepare()`` 从未被
        调用——不合法的意图不应该先创建一条待确认操作再补救。"""

        pending_actions = FakePendingActions(
            outcome=_FakePrepareOutcome(decision=_FakePrepareDecision(ok=True))
        )
        confirm_cards = FakeConfirmCards()
        router, _, _, audit = _router(
            pending_actions=pending_actions, confirm_cards=confirm_cards
        )

        outcome = router.route(
            open_id=ADMIN_OPEN_ID,
            text=self._grant_text(target=ADMIN_OPEN_ID),
            trace_id="t1",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
        )

        self.assertTrue(outcome.handled)
        self.assertIn("不能对自己", outcome.reply_text)
        self.assertEqual(pending_actions.prepare_calls, [])
        self.assertEqual(confirm_cards.send_calls, [])

    def test_self_target_suppress_is_rejected_without_calling_prepare(self) -> None:
        """对称动作同一防呆。"""

        pending_actions = FakePendingActions(
            outcome=_FakePrepareOutcome(decision=_FakePrepareDecision(ok=True))
        )
        confirm_cards = FakeConfirmCards()
        router, _, _, audit = _router(
            pending_actions=pending_actions, confirm_cards=confirm_cards
        )

        outcome = router.route(
            open_id=ADMIN_OPEN_ID,
            text=self._suppress_text(target=ADMIN_OPEN_ID),
            trace_id="t1",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
        )

        self.assertTrue(outcome.handled)
        self.assertIn("不能对自己", outcome.reply_text)
        self.assertEqual(pending_actions.prepare_calls, [])

    def test_self_target_guard_is_always_false_for_suspend_and_resume(self) -> None:
        """自我目标防呆是"显式条件门，suspend/resume 恒为假"：管理员对自己发起
        停用/恢复不会被这条新规则拦截——沿用既有 ``decide_prepare``/角色核对
        判定这次操作是否合理，不属于本条防呆的关注范围。"""

        pending = _prepared_pending(action_type=PendingActionType.SUSPEND_USER)
        pending_actions = FakePendingActions(
            outcome=_FakePrepareOutcome(decision=_FakePrepareDecision(ok=True), pending=pending)
        )
        confirm_cards = FakeConfirmCards(delivered=True)
        router, _, _, audit = _router(
            pending_actions=pending_actions, confirm_cards=confirm_cards
        )

        outcome = router.route(
            open_id=ADMIN_OPEN_ID,
            text=f"/admin suspend {ADMIN_OPEN_ID}",
            trace_id="t1",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(len(pending_actions.prepare_calls), 1)
        self.assertNotIn("不能对自己", outcome.reply_text)

    def test_successful_grant_dispatch_forwards_payload_fields_to_prepare(self) -> None:
        pending = _prepared_pending(action_type=PendingActionType.LOCAL_PERMISSION_GRANT)
        pending_actions = FakePendingActions(
            outcome=_FakePrepareOutcome(decision=_FakePrepareDecision(ok=True), pending=pending)
        )
        confirm_cards = FakeConfirmCards(delivered=True)
        router, _, _, audit = _router(
            pending_actions=pending_actions, confirm_cards=confirm_cards
        )

        outcome = router.route(
            open_id=ADMIN_OPEN_ID,
            text=self._grant_text(),
            trace_id="t1",
            chat_id="oc_1",
            thread_id="thread_1",
            message_id="om_1",
        )

        self.assertTrue(outcome.handled)
        self.assertIn("待确认", outcome.reply_text)
        self.assertEqual(len(pending_actions.prepare_calls), 1)
        call = pending_actions.prepare_calls[0]
        self.assertEqual(call["action_type"], PendingActionType.LOCAL_PERMISSION_GRANT)
        self.assertEqual(call["target_open_id"], "ou_target")
        self.assertEqual(call["company_id"], "1011")
        self.assertEqual(call["metric_name"], "daily_active")
        self.assertEqual(call["reason"], "特批")
        self.assertEqual(audit.actions(), ["admin.command.grant_permission"])

    def test_successful_suppress_dispatch(self) -> None:
        pending = _prepared_pending(action_type=PendingActionType.LOCAL_PERMISSION_SUPPRESS)
        pending_actions = FakePendingActions(
            outcome=_FakePrepareOutcome(decision=_FakePrepareDecision(ok=True), pending=pending)
        )
        confirm_cards = FakeConfirmCards(delivered=True)
        router, _, _, audit = _router(
            pending_actions=pending_actions, confirm_cards=confirm_cards
        )

        outcome = router.route(
            open_id=ADMIN_OPEN_ID,
            text=self._suppress_text(),
            trace_id="t1",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(
            pending_actions.prepare_calls[0]["action_type"],
            PendingActionType.LOCAL_PERMISSION_SUPPRESS,
        )
        self.assertEqual(audit.actions(), ["admin.command.suppress_permission"])

    def test_grant_prepare_rejection_is_reported_without_sending_a_card(self) -> None:
        pending_actions = FakePendingActions(
            outcome=_FakePrepareOutcome(
                decision=_FakePrepareDecision(
                    ok=False, code="target_state_changed", message="该公司×指标已有生效的本地覆盖。"
                )
            )
        )
        confirm_cards = FakeConfirmCards()
        router, _, _, audit = _router(
            pending_actions=pending_actions, confirm_cards=confirm_cards
        )

        outcome = router.route(
            open_id=ADMIN_OPEN_ID,
            text=self._grant_text(),
            trace_id="t1",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.reply_text, "该公司×指标已有生效的本地覆盖。")
        self.assertEqual(confirm_cards.send_calls, [])

    def test_help_text_mentions_grant_and_suppress_permission(self) -> None:
        router, _, _, _ = _router()

        outcome = router.route(open_id=ADMIN_OPEN_ID, text="/admin help", trace_id="t1")

        self.assertIn("grant_permission", outcome.reply_text)
        self.assertIn("suppress_permission", outcome.reply_text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
