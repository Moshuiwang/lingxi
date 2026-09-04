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
from datetime import UTC

from lingxi.config.content import default_content_catalog
from lingxi.core.admin.pending_action import PendingAction, PendingActionStatus, PendingActionType
from lingxi.core.admin.registry import ALL_ADMIN_ROLES, AdminRegistryEntry, AdminRole
from lingxi.core.admin.router import (
    AdminCommandRouter,
    AdminEventView,
    AdminTraceView,
    AdminUserStatusView,
    LocalPermissionOverrideView,
)
from lingxi.core.ids import new_ulid


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
        traces: dict[str, AdminTraceView] | None = None,
        raise_on_user: bool = False,
        raise_on_trace: bool = False,
        identifier_aliases: dict[str, str] | None = None,
        metric_aliases: dict[str, str] | None = None,
        overrides_by_key: dict[tuple[str, str, str], str] | None = None,
    ) -> None:
        self.user_calls: list[str] = []
        self.event_calls: list[dict[str, object]] = []
        self.trace_calls: list[str] = []
        self._users = users or {}
        self._events = events or []
        self._traces = traces or {}
        self._raise_on_user = raise_on_user
        self._raise_on_trace = raise_on_trace
        # #439 A 档新增三个反查端口的假实现：默认原样透传（``identifier_aliases``/
        # ``metric_aliases`` 为空时对任何输入恒等），与真实 fail-open 语义一致——
        # 既有全部用例因此不需要为这三个新方法各自补一份配置就能继续通过。
        self.resolve_identifier_calls: list[str] = []
        self.resolve_metric_calls: list[str] = []
        self.resolve_override_calls: list[tuple[str, str, str]] = []
        self._identifier_aliases = identifier_aliases or {}
        self._metric_aliases = metric_aliases or {}
        self._overrides_by_key = overrides_by_key or {}

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

    def trace_lookup(self, *, trace_id: str) -> AdminTraceView | None:
        self.trace_calls.append(trace_id)
        if self._raise_on_trace:
            raise RuntimeError("模拟查询失败")
        return self._traces.get(trace_id)

    def resolve_identifier(self, *, identifier: str) -> str:
        self.resolve_identifier_calls.append(identifier)
        return self._identifier_aliases.get(identifier, identifier)

    def resolve_metric_name(self, *, metric_token: str) -> str:
        self.resolve_metric_calls.append(metric_token)
        return self._metric_aliases.get(metric_token, metric_token)

    def resolve_override_id(self, *, open_id: str, company_id: str, metric_name: str) -> str | None:
        key = (open_id, company_id, metric_name)
        self.resolve_override_calls.append(key)
        return self._overrides_by_key.get(key)


class FakeDisplayNames:
    """``AdminDisplayNames`` 的内存假实现（Trace #469 S-1）。

    ``user_label`` 默认退化为通用占位「该用户」——绝不把入参 ``open_id`` 编进
    返回值，与真实实现的"零 ou_"承诺同一姿态；需要展示具体姓名/邮箱的测试
    显式传入 ``user_labels`` 映射。``company_label``/``metric_label`` 默认原样
    透传——公司编号/指标 ID 不是需要隐藏的内部系统标识，多数既有用例不关心这层
    展示翻译，透传让它们不必逐个改断言。
    """

    def __init__(
        self,
        *,
        user_labels: dict[str, str] | None = None,
        company_labels: dict[str, str] | None = None,
        metric_labels: dict[str, str] | None = None,
    ) -> None:
        self._user_labels = user_labels or {}
        self._company_labels = company_labels or {}
        self._metric_labels = metric_labels or {}

    def user_label(self, *, open_id: str) -> str:
        return self._user_labels.get(open_id, "该用户")

    def company_label(self, *, company_id: str) -> str:
        return self._company_labels.get(company_id, company_id)

    def metric_label(self, *, metric_id: str) -> str:
        return self._metric_labels.get(metric_id, metric_id)


class _FakePrepareDecision:
    def __init__(self, *, ok: bool, message: str = "", code: str = "") -> None:
        self.ok = ok
        self.message = message
        self.code = code


class _FakePrepareOutcome:
    def __init__(
        self, *, decision: _FakePrepareDecision, pending: PendingAction | None = None
    ) -> None:
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
    from datetime import datetime, timedelta

    now = datetime(2026, 8, 24, 12, 0, 0, tzinfo=UTC)
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
    management_cards: object | None = None,
    display_names: FakeDisplayNames | None = None,
) -> tuple[AdminCommandRouter, FakeRegistry, FakeQueries, FakeAudit]:
    reg = registry or FakeRegistry({ADMIN_OPEN_ID: _full_admin_entry()})
    qry = queries or FakeQueries()
    aud = audit or FakeAudit()
    return (
        AdminCommandRouter(
            registry=reg,
            queries=qry,
            audit=aud,
            display_names=display_names or FakeDisplayNames(),
            pending_actions=pending_actions,
            confirm_cards=confirm_cards,
            management_cards=management_cards,
        ),
        reg,
        qry,
        aud,
    )


class FakeManagementCards:
    """``ManagementCardSender`` 的假实现（#439 B 档）：记录每次调用的参数，
    可配置抛出异常模拟发送失败。"""

    def __init__(self, *, raise_error: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self.raise_error = raise_error

    def send(
        self,
        *,
        status,
        display_identifier: str,
        chat_id: str,
        thread_id,
        reply_to_message_id: str,
    ):
        self.calls.append(
            {
                "status": status,
                "display_identifier": display_identifier,
                "chat_id": chat_id,
                "thread_id": thread_id,
                "reply_to_message_id": reply_to_message_id,
            }
        )
        if self.raise_error:
            raise RuntimeError("模拟管理卡发送失败")
        return object()


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
        """ "实时判定、不缓存"的行为证据：同一 open_id 连续两次调用各自触发一次
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

    def test_help_uses_the_bi_plus_external_name(self) -> None:
        """#443 对外名称统一：管理命令帮助首行不得残留旧名「Lingxi」。"""

        router, _, _, _ = _router()

        outcome = router.route(open_id=ADMIN_OPEN_ID, text="/admin help", trace_id="t1")

        self.assertTrue(outcome.reply_text.startswith("BI Plus 管理命令："))
        self.assertNotIn("Lingxi", outcome.reply_text)


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
        display_names = FakeDisplayNames(user_labels={"ou_target": "张三（zhangsan@example.com）"})
        router, _, _, audit = _router(queries=queries, display_names=display_names)

        outcome = router.route(open_id=ADMIN_OPEN_ID, text="/admin user ou_target", trace_id="t1")

        self.assertTrue(outcome.handled)
        # Trace #469 S-1：不再回显 open_id，改经 AdminDisplayNames 展示姓名+邮箱；
        # 开通状态英文码同样翻译成中文。
        self.assertIn("张三（zhangsan@example.com）", outcome.reply_text)
        self.assertNotIn("ou_target", outcome.reply_text)
        self.assertIn("已开通", outcome.reply_text)
        self.assertEqual(queries.user_calls, ["ou_target"])
        self.assertEqual(audit.actions(), ["admin.command.query_user"])
        self.assertTrue(audit.records[0][1]["found"])

    def test_not_found_user_reported_without_crashing(self) -> None:
        router, _, queries, audit = _router()

        outcome = router.route(open_id=ADMIN_OPEN_ID, text="/admin user ou_missing", trace_id="t1")

        self.assertTrue(outcome.handled)
        # Trace #469 S-1：查无记录时，长得像内部 open_id 的输入退化为通用占位
        # 「该用户」，不把 open_id 原样拼回去（管理员可见文案零 ou_）。
        self.assertIn("该用户", outcome.reply_text)
        self.assertNotIn("ou_missing", outcome.reply_text)
        self.assertFalse(audit.records[0][1]["found"])

    def test_query_failure_yields_internal_error_reply_not_crash(self) -> None:
        router, _, _, audit = _router(queries=FakeQueries(raise_on_user=True))

        outcome = router.route(open_id=ADMIN_OPEN_ID, text="/admin user ou_target", trace_id="t1")

        self.assertTrue(outcome.handled)
        self.assertTrue(outcome.reply_text)
        self.assertEqual(audit.actions(), ["admin.command.internal_error"])

    def test_user_with_no_local_overrides_shows_the_empty_line(self) -> None:
        """⑥/admin user 无覆盖用户输出零回归（卡 B）：``local_overrides`` 为空
        元组时回显一行「无本地覆盖」，不留空段、不报错。"""

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
        router, _, _, _ = _router(queries=queries)

        outcome = router.route(open_id=ADMIN_OPEN_ID, text="/admin user ou_target", trace_id="t1")

        self.assertTrue(outcome.handled)
        self.assertIn("无本地覆盖", outcome.reply_text)

    def test_user_with_local_overrides_lists_direction_scope_and_truncated_reason(
        self,
    ) -> None:
        """/admin user 新增「当前生效本地覆盖」段（卡 B，revoke 的 UX 前置）：
        列出方向、company_id、metric_name、创建时间，且 reason 不回显全文
        （截断 20 字）。**自 Trace #469 S-1 起不再列出 override_id**——内部 ID
        只留审计，管理员发起撤销改用「标识+公司+指标」形式或管理卡逐行撤销
        按钮，均不需要先看到这个内部 ID（见 ``core/admin/router.
        _render_local_overrides`` 文档）。"""

        long_reason = "这是一段超过二十个字符的很长很长的收回或授权原因说明文本"
        self.assertGreater(len(long_reason), 20)
        override = LocalPermissionOverrideView(
            override_id="lpo_01JGFJJZ008XSHEADGG8V74SPC",
            direction="grant",
            company_id="1011",
            metric_name="daily_active",
            reason=long_reason,
            created_at="2026-08-24T00:00:00+00:00",
        )
        queries = FakeQueries(
            users={
                "ou_target": AdminUserStatusView(
                    identifier="ou_target",
                    provisioning_state="active",
                    account_state="enabled",
                    permission_version=3,
                    updated_at="2026-08-24T00:00:00+00:00",
                    local_overrides=(override,),
                )
            }
        )
        router, _, _, _ = _router(queries=queries)

        outcome = router.route(open_id=ADMIN_OPEN_ID, text="/admin user ou_target", trace_id="t1")

        self.assertTrue(outcome.handled)
        self.assertNotIn("lpo_01JGFJJZ008XSHEADGG8V74SPC", outcome.reply_text)
        self.assertIn("授权", outcome.reply_text)
        self.assertIn("1011", outcome.reply_text)
        self.assertIn("daily_active", outcome.reply_text)
        self.assertIn(long_reason[:20], outcome.reply_text)
        self.assertNotIn(long_reason, outcome.reply_text, "reason 不得回显全文")
        self.assertNotIn("无本地覆盖", outcome.reply_text)

    def test_user_with_position_group_overrides_shows_one_group_item(self) -> None:
        group_id = "lpg_01M1C90YDGMTY567GDTZZJ4C5E"
        overrides = tuple(
            LocalPermissionOverrideView(
                override_id=f"lpo_{index}",
                direction="grant",
                company_id=f"c{index}",
                metric_name=f"metric_{index}",
                reason="职位范围特批",
                created_at="2026-08-24T00:00:00+00:00",
                position_name="A运营",
                company_scope="*",
                group_id=group_id,
            )
            for index in range(3)
        )
        queries = FakeQueries(
            users={
                "ou_target": AdminUserStatusView(
                    identifier="ou_target",
                    provisioning_state="active",
                    account_state="enabled",
                    permission_version=3,
                    updated_at="2026-08-24T00:00:00+00:00",
                    local_overrides=overrides,
                )
            }
        )
        router, _, _, _ = _router(queries=queries)

        outcome = router.route(open_id=ADMIN_OPEN_ID, text="/admin user ou_target", trace_id="t1")

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.reply_text.count("覆盖 3 项权限"), 1)
        self.assertIn("职位 A运营", outcome.reply_text)
        self.assertIn("公司范围 全部", outcome.reply_text)
        self.assertNotIn("metric_0", outcome.reply_text)

    def test_a_user_with_local_overrides_no_longer_carries_the_zero_galaxy_permission_caveat(
        self,
    ) -> None:
        """否定断言：零银河权限用户的本地授权边界提示（#319 动机场景，Trace #328
        opus 审查 P1）**已随 PM 2026-08-29 裁定（Issue #419）撤销**——四源合并不再
        挂在 `aggregate.granted` 判据之后，有本地覆盖行时输出不得再出现「暂不
        生效」。"""

        override = LocalPermissionOverrideView(
            override_id="lpo_01JGFJJZ008XSHEADGG8V74SPC",
            direction="grant",
            company_id="1011",
            metric_name="daily_active",
            reason="特批",
            created_at="2026-08-24T00:00:00+00:00",
        )
        queries = FakeQueries(
            users={
                "ou_target": AdminUserStatusView(
                    identifier="ou_target",
                    provisioning_state="active",
                    account_state="enabled",
                    permission_version=3,
                    updated_at="2026-08-24T00:00:00+00:00",
                    local_overrides=(override,),
                )
            }
        )
        router, _, _, _ = _router(queries=queries)

        outcome = router.route(open_id=ADMIN_OPEN_ID, text="/admin user ou_target", trace_id="t1")

        self.assertTrue(outcome.handled)
        self.assertNotIn("暂不生效", outcome.reply_text)
        self.assertNotIn("V-权限-15", outcome.reply_text)

    def test_a_user_without_local_overrides_does_not_carry_the_caveat(self) -> None:
        """否定断言：没有任何本地覆盖行时同样不该出现这句提示（提示已整体删除，
        不是条件性的免责声明）。"""

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
        router, _, _, _ = _router(queries=queries)

        outcome = router.route(open_id=ADMIN_OPEN_ID, text="/admin user ou_target", trace_id="t1")

        self.assertNotIn("暂不生效", outcome.reply_text)


class EmailIdentifierResolutionTests(unittest.TestCase):
    """#439 A 档：``/admin user`` 的标识参数经 ``AdminQueries.resolve_identifier``
    反查——命中时按反查出的 open_id 查询。**Trace #469 S-1 起**回复头部改经
    ``AdminDisplayNames.user_label`` 展示姓名+邮箱，不再是"管理员自己输入的
    标识原样回显"（那条既有姿态曾经是为了不强迫管理员看到内部 open_id；现在
    统一升级成更友好的姓名+邮箱展示，同时也满足零 ou_ 这条更严格的要求）。"""

    def test_email_identifier_is_resolved_before_querying_user_status(self) -> None:
        queries = FakeQueries(
            users={
                "ou_target": AdminUserStatusView(
                    identifier="ou_target",
                    provisioning_state="active",
                    account_state="enabled",
                    permission_version=1,
                    updated_at="2026-08-30T00:00:00+00:00",
                )
            },
            identifier_aliases={"someone@example.com": "ou_target"},
        )
        display_names = FakeDisplayNames(user_labels={"ou_target": "李四（someone@example.com）"})
        router, _, _, _ = _router(queries=queries, display_names=display_names)

        outcome = router.route(
            open_id=ADMIN_OPEN_ID, text="/admin user someone@example.com", trace_id="t1"
        )

        self.assertTrue(outcome.handled)
        # 反查确实发生过一次，且 user_status 用的是反查出的 open_id，不是原始邮箱。
        self.assertEqual(queries.resolve_identifier_calls, ["someone@example.com"])
        self.assertEqual(queries.user_calls, ["ou_target"])
        # 回复头部展示 AdminDisplayNames 解析出的姓名+邮箱（这里恰好与查询用的
        # 邮箱相同，真实场景下 app_user.email 与反查命中的邮箱本就应当一致）。
        self.assertIn("李四（someone@example.com）", outcome.reply_text)
        self.assertNotIn("ou_target", outcome.reply_text)
        self.assertNotIn("未找到", outcome.reply_text)

    def test_unresolvable_email_falls_through_to_the_existing_not_found_reply(self) -> None:
        """反查零命中时原样透传输入（fail-open），下游按既有"未找到"语义处理，
        不新增一条并行的"邮箱查无"错误分支。"""

        router, _, queries, _ = _router()

        outcome = router.route(
            open_id=ADMIN_OPEN_ID, text="/admin user nobody@example.com", trace_id="t1"
        )

        self.assertTrue(outcome.handled)
        self.assertIn("未找到", outcome.reply_text)
        self.assertIn("nobody@example.com", outcome.reply_text)
        # 原样透传后仍然去查了一次（用邮箱本身当 identifier），不是提前短路。
        self.assertEqual(queries.user_calls, ["nobody@example.com"])

    def test_open_id_shaped_identifier_skips_resolution_call(self) -> None:
        """非邮箱形态（不含 ``@``）时不发起任何反查调用——既有全部行为的零成本
        路径，见 ``resolve_identifier`` 文档。"""

        router, _, queries, _ = _router()

        router.route(open_id=ADMIN_OPEN_ID, text="/admin user ou_plain", trace_id="t1")

        self.assertEqual(queries.resolve_identifier_calls, ["ou_plain"])
        self.assertEqual(queries.user_calls, ["ou_plain"])


class ManagementCardSendTests(unittest.TestCase):
    """#439 B 档：``/admin user`` 附带发送用户权限管理卡，best-effort，不影响
    既有文本回复这条主路径。"""

    def _status(self) -> AdminUserStatusView:
        return AdminUserStatusView(
            identifier="ou_target",
            provisioning_state="active",
            account_state="enabled",
            permission_version=1,
            updated_at="2026-08-30T00:00:00+00:00",
        )

    def test_card_is_sent_as_a_reply_to_the_triggering_message_when_wired(self) -> None:
        cards = FakeManagementCards()
        queries = FakeQueries(users={"ou_target": self._status()})
        router, _, _, _ = _router(queries=queries, management_cards=cards)

        outcome = router.route(
            open_id=ADMIN_OPEN_ID,
            text="/admin user ou_target",
            trace_id="t1",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(len(cards.calls), 1)
        self.assertEqual(cards.calls[0]["display_identifier"], "ou_target")

    def test_admin_user_with_an_email_identifier_resolves_and_sends_the_management_card(
        self,
    ) -> None:
        """自证闭环条款的完整受控注入场景：``/admin user <邮箱>`` 一次调用里
        同时验证①标识按邮箱正确反查、②管理卡确实调出（发送）、③卡片收到的
        ``status`` 就是反查后拿到的那份真实状态——三件事在同一次 ``route()``
        调用里一起成立，不是三个互不相关的独立断言拼出来的假象。"""

        status = self._status()
        cards = FakeManagementCards()
        queries = FakeQueries(
            users={"ou_target": status},
            identifier_aliases={"admin-user-test@example.com": "ou_target"},
        )
        router, _, _, _ = _router(queries=queries, management_cards=cards)

        outcome = router.route(
            open_id=ADMIN_OPEN_ID,
            text="/admin user admin-user-test@example.com",
            trace_id="t1",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(len(cards.calls), 1)
        self.assertEqual(cards.calls[0]["display_identifier"], "admin-user-test@example.com")
        self.assertIs(cards.calls[0]["status"], status)
        self.assertEqual(cards.calls[0]["reply_to_message_id"], "om_1")
        self.assertEqual(cards.calls[0]["reply_to_message_id"], "om_1")
        self.assertEqual(cards.calls[0]["chat_id"], "oc_1")

    def test_not_wired_sends_no_card_but_text_reply_is_unaffected(self) -> None:
        """未装配（``management_cards=None``，既有全部构造点/测试的默认值）时
        不发送任何卡片，`/admin user` 的文本回复行为逐字节不变——既有全部 50
        个测试用例本身就是这条回归的证据（它们从未传 management_cards）。"""

        queries = FakeQueries(users={"ou_target": self._status()})
        router, _, _, _ = _router(queries=queries)

        outcome = router.route(
            open_id=ADMIN_OPEN_ID,
            text="/admin user ou_target",
            trace_id="t1",
            chat_id="oc_1",
            message_id="om_1",
        )

        self.assertTrue(outcome.handled)
        self.assertIn("该用户", outcome.reply_text)

    def test_missing_message_id_skips_card_send(self) -> None:
        """没有可回复的消息 ID（既有全部只读命令调用点的默认值）不发送卡片——
        与写命令"无法回复触发消息就不发确认卡片"同一姿态。"""

        cards = FakeManagementCards()
        queries = FakeQueries(users={"ou_target": self._status()})
        router, _, _, _ = _router(queries=queries, management_cards=cards)

        router.route(open_id=ADMIN_OPEN_ID, text="/admin user ou_target", trace_id="t1")

        self.assertEqual(cards.calls, [])

    def test_card_send_failure_does_not_affect_the_existing_text_reply(self) -> None:
        cards = FakeManagementCards(raise_error=True)
        queries = FakeQueries(users={"ou_target": self._status()})
        router, _, _, audit = _router(queries=queries, management_cards=cards)

        outcome = router.route(
            open_id=ADMIN_OPEN_ID,
            text="/admin user ou_target",
            trace_id="t1",
            chat_id="oc_1",
            message_id="om_1",
        )

        self.assertTrue(outcome.handled)
        self.assertIn("该用户", outcome.reply_text)
        self.assertIn("admin.command.management_card_send_failed", audit.actions())
        # 主查询审计（admin.command.query_user）仍然照常记录，不被卡片失败挤掉。
        self.assertIn("admin.command.query_user", audit.actions())

    def test_no_card_is_sent_when_the_user_is_not_found(self) -> None:
        """查无此人时不发送管理卡——没有内容可以展示。"""

        cards = FakeManagementCards()
        router, _, _, _ = _router(management_cards=cards)

        router.route(
            open_id=ADMIN_OPEN_ID,
            text="/admin user ou_missing",
            trace_id="t1",
            chat_id="oc_1",
            message_id="om_1",
        )

        self.assertEqual(cards.calls, [])


class QueryTraceCommandTests(unittest.TestCase):
    """``/admin trace <追溯号>``（Issue #337）。认领断言：管理员能凭追溯号查回
    ``failure_reason``（`test_failure_reason_reported`）；查无追溯号明确回复
    「不存在」（`test_unknown_trace_id_reported_as_not_found`，且不是空字符串
    也不是异常）；追溯号存在但没有失败记录时如实回「无失败记录」而不是假装
    查无此追溯号（`test_trace_without_failure_reason_reported_honestly`）。
    脱敏断言：回复不带 open_id（`test_reply_never_echoes_open_id`）。"""

    def test_failure_reason_reported(self) -> None:
        trace_id = new_ulid()
        queries = FakeQueries(
            traces={
                trace_id: AdminTraceView(
                    trace_id=trace_id,
                    event_count=1,
                    first_received_at="2026-08-28T01:00:00+00:00",
                    last_event_type="im.message.receive_v1",
                    last_handled_as="auto_provisioning",
                    dispatched=True,
                    provisioning_state="provisioning",
                    account_state="enabled",
                    failure_reason="directory_unavailable",
                    failure_event_type="onboarding.result",
                    failure_occurred_at="2026-08-28T01:05:00+00:00",
                )
            }
        )
        router, _, queries, audit = _router(queries=queries)

        outcome = router.route(
            open_id=ADMIN_OPEN_ID, text=f"/admin trace {trace_id}", trace_id="t1"
        )

        self.assertTrue(outcome.handled)
        self.assertIn("directory_unavailable", outcome.reply_text)
        self.assertIn("onboarding.result", outcome.reply_text)
        self.assertEqual(queries.trace_calls, [trace_id])
        self.assertEqual(audit.actions(), ["admin.command.query_trace"])
        self.assertEqual(audit.records[0][1]["found"], True)
        self.assertEqual(audit.records[0][1]["target"], trace_id)

    def test_unknown_trace_id_reported_as_not_found(self) -> None:
        """否定断言：查无此追溯号是明确的「不存在」文案，不是空白或报错。"""

        trace_id = new_ulid()
        router, _, queries, audit = _router(queries=FakeQueries())

        outcome = router.route(
            open_id=ADMIN_OPEN_ID, text=f"/admin trace {trace_id}", trace_id="t1"
        )

        self.assertTrue(outcome.handled)
        self.assertIn("查无此追溯号", outcome.reply_text)
        self.assertEqual(queries.trace_calls, [trace_id])
        self.assertEqual(audit.records[0][1]["found"], False)

    def test_trace_without_failure_reason_reported_honestly(self) -> None:
        """追溯号存在（例如成功开通、或仍在进行中）但没有失败记录：如实回
        「无开通失败记录」并带上能查到的开通状态，不是假装这条追溯号也查无此人。

        Issue #495 起这句话由「无失败记录」改成「无开通失败记录」：同一条回复
        里现在可能同时出现"开通没失败"与"问数任务失败了"，旧措辞会与紧随其后
        的「任务结果: 失败」直接打架。"""

        trace_id = new_ulid()
        queries = FakeQueries(
            traces={
                trace_id: AdminTraceView(
                    trace_id=trace_id,
                    event_count=1,
                    first_received_at="2026-08-28T01:00:00+00:00",
                    last_event_type="im.message.receive_v1",
                    last_handled_as="auto_provisioning",
                    dispatched=True,
                    provisioning_state="active",
                    account_state="enabled",
                    failure_reason=None,
                    failure_event_type=None,
                    failure_occurred_at=None,
                )
            }
        )
        router, _, _, _ = _router(queries=queries)

        outcome = router.route(
            open_id=ADMIN_OPEN_ID, text=f"/admin trace {trace_id}", trace_id="t1"
        )

        self.assertTrue(outcome.handled)
        self.assertIn("无开通失败记录", outcome.reply_text)
        # Trace #469 S-1：英文状态码翻译成中文。
        self.assertIn("已开通", outcome.reply_text)
        self.assertNotIn("查无此追溯号", outcome.reply_text)

    def test_reply_never_echoes_open_id(self) -> None:
        """脱敏断言（Issue #337 范围条目 3）：回复不带 open_id。"""

        trace_id = new_ulid()
        secret_open_id = "ou_should_never_appear"
        queries = FakeQueries(
            traces={
                trace_id: AdminTraceView(
                    trace_id=trace_id,
                    event_count=1,
                    first_received_at="2026-08-28T01:00:00+00:00",
                    last_event_type="im.message.receive_v1",
                    last_handled_as="auto_provisioning",
                    dispatched=True,
                    provisioning_state="provisioning",
                    account_state="enabled",
                    failure_reason="mcp_sync_timeout",
                    failure_event_type="onboarding.result",
                    failure_occurred_at="2026-08-28T01:20:00+00:00",
                )
            }
        )
        router, _, _, _ = _router(queries=queries)

        outcome = router.route(
            open_id=ADMIN_OPEN_ID, text=f"/admin trace {trace_id}", trace_id="t1"
        )

        self.assertNotIn(secret_open_id, outcome.reply_text)

    def test_known_event_type_handled_as_and_failure_reason_are_humanized(self) -> None:
        """Trace #469 修复包 B，B-6：已登记的机器码不再直出——事件类型、
        处理方式、失败原因三项各自换成中文显示名，且不再包含原始机器码
        字面量（与下面"未登记回退"用例互补：这里验证"认识的"分支，那边
        验证"不认识的"分支）。"""

        trace_id = new_ulid()
        queries = FakeQueries(
            traces={
                trace_id: AdminTraceView(
                    trace_id=trace_id,
                    event_count=1,
                    first_received_at="2026-08-28T01:00:00+00:00",
                    last_event_type="im.message.receive_v1",
                    last_handled_as="not_provisioned",
                    dispatched=True,
                    provisioning_state="provisioning",
                    account_state="enabled",
                    failure_reason="mcp_sync_timeout",
                    failure_event_type="onboarding.result",
                    failure_occurred_at="2026-08-28T01:20:00+00:00",
                )
            }
        )
        router, _, _, _ = _router(queries=queries)

        outcome = router.route(
            open_id=ADMIN_OPEN_ID, text=f"/admin trace {trace_id}", trace_id="t1"
        )

        self.assertIn("用户消息", outcome.reply_text)
        self.assertNotIn("im.message.receive_v1", outcome.reply_text)
        self.assertIn("未开通，未受理", outcome.reply_text)
        self.assertNotIn("not_provisioned", outcome.reply_text)
        self.assertIn("问数权限同步超时", outcome.reply_text)
        self.assertNotIn("mcp_sync_timeout", outcome.reply_text)

    def test_unregistered_handled_as_falls_back_to_the_raw_value_with_a_visible_marker(
        self,
    ) -> None:
        """未登记的机器码不能崩、也不能悄悄消失——回退成"原值（未登记显示名）"
        这个统一样式，管理员至少还能看到原始取值。**变异验红**：把
        ``_display_or_unregistered`` 里的回退分支改成直接返回空字符串或抛
        异常，本用例必红。"""

        trace_id = new_ulid()
        queries = FakeQueries(
            traces={
                trace_id: AdminTraceView(
                    trace_id=trace_id,
                    event_count=1,
                    first_received_at="2026-08-28T01:00:00+00:00",
                    last_event_type="im.message.receive_v1",
                    last_handled_as="some_future_value_not_yet_registered",
                    dispatched=True,
                    provisioning_state="provisioning",
                    account_state="enabled",
                    failure_reason=None,
                    failure_event_type=None,
                    failure_occurred_at=None,
                )
            }
        )
        router, _, _, _ = _router(queries=queries)

        outcome = router.route(
            open_id=ADMIN_OPEN_ID, text=f"/admin trace {trace_id}", trace_id="t1"
        )

        self.assertIn("some_future_value_not_yet_registered（未登记显示名）", outcome.reply_text)

    def _trace_view(self, trace_id: str, **overrides: object) -> AdminTraceView:
        values: dict[str, object] = {
            "trace_id": trace_id,
            "event_count": 1,
            "first_received_at": "2026-08-31T01:00:00+00:00",
            "last_event_type": "im.message.receive_v1",
            "last_handled_as": "task_queued",
            "dispatched": True,
            "provisioning_state": "active",
            "account_state": "enabled",
            "failure_reason": None,
            "failure_event_type": None,
            "failure_occurred_at": None,
        }
        values.update(overrides)
        return AdminTraceView(**values)  # type: ignore[arg-type]

    def _trace_reply(self, **overrides: object) -> str:
        trace_id = new_ulid()
        queries = FakeQueries(traces={trace_id: self._trace_view(trace_id, **overrides)})
        router, _, _, _ = _router(queries=queries)
        outcome = router.route(
            open_id=ADMIN_OPEN_ID, text=f"/admin trace {trace_id}", trace_id="t1"
        )
        self.assertTrue(outcome.handled)
        return outcome.reply_text

    def test_task_failure_is_reported_with_a_readable_reason_and_the_exception_type(
        self,
    ) -> None:
        """Issue #495 完成标准 4：管理员凭追溯号就能拿到「这次问数为什么失败」的
        可读原因，不必再去检索 worker 容器日志（他根本没有那个权限）。

        翻译口径与 Trace #469 修复包 B 一致：状态与失败码换中文，底层异常
        **类型名**原样展示（第三方库类名没有可枚举取值域，翻译只能靠猜；原样
        贴给研发就是最有用的一手信息）。

        **变异验红**（已实测）：删掉 ``_render_trace`` 里
        ``trace.task_status is not None`` 那一段，本用例由绿转红。恢复后复绿。"""

        reply = self._trace_reply(
            task_status="failed",
            task_error_kind="session_failed",
            task_failure_code="session_failed",
            task_failure_signature="psycopg.errors.OperationalError",
            task_ended_at="2026-08-31T01:02:03+00:00",
        )

        self.assertIn("任务结果: 失败", reply)
        self.assertIn("2026-08-31T01:02:03+00:00", reply)
        self.assertIn("会话执行失败（未分类，见底层异常）", reply)
        self.assertIn("psycopg.errors.OperationalError", reply)

    def test_query_mcp_502_has_a_distinct_trace_reason_and_signature(self) -> None:
        reply = self._trace_reply(
            task_status="failed",
            task_error_kind="mcp_bad_gateway",
            task_failure_code="mcp_bad_gateway",
            task_failure_signature="mcp.query.http_502",
        )

        self.assertIn("指标 MCP 网关返回 502（建连失败）", reply)
        self.assertIn("失败签名: mcp.query.http_502", reply)
        self.assertIn("mcp.query.http_502", reply)

    def test_a_trace_without_any_task_omits_the_task_section_entirely(self) -> None:
        """否定测试：这条追溯号没有派生任务（管理命令、未开通用户、重复投递都
        不入队）时整段省略，不摆一排空值——否则上一条用例用一个恒真实现也能过。"""

        reply = self._trace_reply()

        self.assertNotIn("任务结果", reply)
        self.assertNotIn("任务失败原因", reply)
        self.assertNotIn("底层异常类型", reply)

    def test_a_successful_task_shows_its_status_without_inventing_a_failure(self) -> None:
        """成功的任务只展示状态：``failure_code``/``failure_signature`` 在这种
        终态下本来就是 ``NULL``（迁移 ``0080`` 的精确语义），不得凭空补一行
        「失败原因」。"""

        reply = self._trace_reply(
            task_status="succeeded", task_ended_at="2026-08-31T01:02:03+00:00"
        )

        self.assertIn("任务结果: 成功", reply)
        self.assertNotIn("任务失败原因", reply)
        self.assertNotIn("底层异常类型", reply)

    def test_a_failure_without_a_failure_code_falls_back_to_the_error_kind(self) -> None:
        """没有经过 ``write_terminal_event`` 的失败终态（心跳超时回收、投递到期、
        排队超时，写入方是 ``_queue_lifecycle.py``）在 ``failure_code`` 列上恒为
        ``NULL``——回显必须退回 ``error_kind``，不能因此整行消失、让管理员看到
        一个没有任何原因的「任务结果: 失败」。

        **变异验红**（已实测）：把 ``_render_trace`` 里的
        ``trace.task_failure_code or trace.task_error_kind`` 改回只看
        ``task_failure_code``，本用例由绿转红。恢复后复绿。"""

        reply = self._trace_reply(
            task_status="failed", task_error_kind="retry_exhausted", task_failure_code=None
        )

        self.assertIn("任务失败原因: 重试次数耗尽", reply)

    def test_an_unregistered_task_failure_code_falls_back_to_the_visible_marker(self) -> None:
        """未登记的失败码（未来新增但词表忘了同步）走与本文件其余词表同一条
        回退：原值 + 「未登记显示名」，不崩、不假装认识。"""

        reply = self._trace_reply(
            task_status="failed", task_failure_code="some_future_failure_code"
        )

        self.assertIn("some_future_failure_code（未登记显示名）", reply)

    def test_document_delivery_degradation_is_reported_separately_from_task_success(
        self,
    ) -> None:
        """Issue #499：任务成功不等于文档正文按官方排版成功。

        ``body_degraded_reason`` 从文档投递检查点读出后，管理员凭同一个追溯号应
        能看到「文档成功但正文已降级」及可读原因；不能只展示 task 的成功状态，
        也不能把降级静默成普通文档成功。

        **变异验红**：删掉 ``_render_trace`` 的文档投递段落，或把降级字段改成
        恒为空，本用例的两条断言都应变红；恢复后复绿。
        """

        reply = self._trace_reply(
            task_status="succeeded",
            document_delivery_status="succeeded",
            document_body_degraded_reason="unsupported_nested_blocks",
        )

        self.assertIn("任务结果: 成功", reply)
        self.assertIn("文档交付结果: 成功", reply)
        self.assertIn("文档正文处理: 已降级", reply)
        self.assertIn("正文含无法定位的块结构", reply)

    def test_document_delivery_failure_is_reported_when_task_itself_succeeded(self) -> None:
        """文档消费是独立状态机：问数任务成功但文档明确失败时，``/admin trace``
        仍应如实显示文档失败及其原因，不能拿 task 的成功状态遮住用户未拿到文档
        这一事实。"""

        reply = self._trace_reply(
            task_status="succeeded",
            document_delivery_status="failed",
            document_delivery_last_error="permission_not_confirmed",
        )

        self.assertIn("任务结果: 成功", reply)
        self.assertIn("文档交付结果: 失败", reply)
        self.assertIn("文档交付原因: 授权结果未能读回确认", reply)

    def test_non_admin_is_rejected_and_produces_zero_trace_calls(self) -> None:
        """否定断言：非管理员发 `/admin trace` 被拒绝，且不触发任何下游查询
        （与既有 `DefaultDenyTests` 同一姿态，本命令专用取证）。"""

        trace_id = new_ulid()
        router, registry, queries, audit = _router(registry=FakeRegistry({}))

        outcome = router.route(
            open_id="ou_never_registered_9f3e",
            text=f"/admin trace {trace_id}",
            trace_id="t1",
        )

        self.assertFalse(outcome.handled)
        self.assertEqual(queries.trace_calls, [])
        self.assertEqual(audit.actions(), ["admin.command.rejected"])


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
        self.assertEqual(
            queries.event_calls, [{"identifier": None, "window_hours": 24, "limit": 20}]
        )
        self.assertEqual(audit.actions(), ["admin.command.query_audit"])

    def test_no_events_reported_clearly(self) -> None:
        router, _, _, _ = _router()

        outcome = router.route(open_id=ADMIN_OPEN_ID, text="/admin audit ou_x 48", trace_id="t1")

        self.assertTrue(outcome.handled)
        self.assertIn("没有找到", outcome.reply_text)
        # Trace #469 修复包 B，B-5：`_render_audit_query` 的标识遮蔽此前无用例
        # 锁住——审查变异实测去掉 `_safe_identifier_echo` 调用仍全绿。open_id
        # 形状的标识（`ou_` 前缀）不得原样出现在管理员可见回复里。
        self.assertNotIn("ou_x", outcome.reply_text)

    def test_open_id_shaped_identifier_is_masked_in_the_events_header_too(self) -> None:
        """Trace #469 修复包 B，B-5：有事件命中时回复走 header+lines 分支
        （与"没有找到"分支不同的代码路径），标识遮蔽必须同样生效——两条分支
        共用同一个 ``scope = f"标识 {_safe_identifier_echo(identifier)} 的"``
        取值，这里独立锁住有事件分支，防止未来只改了其中一条分支的遮蔽逻辑。
        **变异验红**：把 ``_render_audit_query`` 里的 ``_safe_identifier_echo(identifier)``
        换回裸 ``identifier``，本用例必红。"""

        events = [
            AdminEventView(
                received_at="2026-08-24T01:00:00+00:00",
                event_type="im.message.receive_v1",
                handled_as="not_provisioned",
                trace_id="trc_abc",
            )
        ]
        router, _, _, _ = _router(queries=FakeQueries(events=events))

        outcome = router.route(open_id=ADMIN_OPEN_ID, text="/admin audit ou_x 48", trace_id="t1")

        self.assertTrue(outcome.handled)
        self.assertNotIn("ou_x", outcome.reply_text)
        self.assertIn("该用户", outcome.reply_text)


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
        router, _, _, _ = _router(registry=FakeRegistry({}), audit=FakeAudit(raise_error=True))

        outcome = router.route(open_id="ou_never_registered", text="/admin help", trace_id="t1")

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

        outcome = router.route(open_id=ADMIN_OPEN_ID, text="/admin user ou_target", trace_id="t1")

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
        router, _, _, audit = _router(pending_actions=pending_actions, confirm_cards=confirm_cards)

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
        router, _, _, audit = _router(pending_actions=pending_actions, confirm_cards=confirm_cards)

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
        self.assertEqual(pending_actions.prepare_calls[0]["initiated_by_open_id"], ADMIN_OPEN_ID)

    def test_card_send_failure_is_reported_and_operation_does_not_proceed(self) -> None:
        pending = _prepared_pending()
        pending_actions = FakePendingActions(
            outcome=_FakePrepareOutcome(decision=_FakePrepareDecision(ok=True), pending=pending)
        )
        confirm_cards = FakeConfirmCards(delivered=False)
        router, _, _, audit = _router(pending_actions=pending_actions, confirm_cards=confirm_cards)

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
            outcome=_FakePrepareOutcome(decision=_FakePrepareDecision(ok=True), pending=pending)
        )
        confirm_cards = FakeConfirmCards(delivered=True)
        router, _, _, audit = _router(pending_actions=pending_actions, confirm_cards=confirm_cards)

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
        router, _, _, audit = _router(pending_actions=pending_actions, confirm_cards=confirm_cards)

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


class RetiredPermissionCommandRoutingTests(unittest.TestCase):
    """**否定用例**：``/admin grant_permission`` 与 ``/admin suppress_permission``
    撤除后，已登记管理员发这两条命令得到「无此命令」一类回应，且**审计里不产生任何
    授权动作**（Trace #544 D-5，产品负责人裁定；对抗审查 A-4 / P-4）。

    "不产生任何授权动作"在这一层的判据是三条同时成立：没有 ``prepare()`` 调用（没有
    待确认操作）、没有确认卡发送、审计动作里没有任何 ``admin.command.*_permission``。
    只断言回复文案是不够的——真正要防的是"文案变了但后面那条写路径还通着"。
    """

    RETIRED_TEXTS = (
        "/admin grant_permission ou_target 1011 daily_active 特批",
        "/admin suppress_permission ou_target 1011 daily_active 特批",
        # 目录外的公司与指标——正是这两条命令被撤除的直接理由（审查 3 级复现）。
        "/admin grant_permission ou_target 9999 bogus_metric_xyz 越权尝试",
    )

    def test_retired_commands_are_rejected_with_no_authorization_side_effect(self) -> None:
        for text in self.RETIRED_TEXTS:
            with self.subTest(text=text):
                pending_actions = FakePendingActions(
                    outcome=_FakePrepareOutcome(decision=_FakePrepareDecision(ok=True))
                )
                confirm_cards = FakeConfirmCards(delivered=True)
                router, _, _, audit = _router(
                    pending_actions=pending_actions, confirm_cards=confirm_cards
                )

                outcome = router.route(
                    open_id=ADMIN_OPEN_ID,
                    text=text,
                    trace_id="t1",
                    chat_id="oc_1",
                    thread_id=None,
                    message_id="om_1",
                )

                # 「无此命令」一类回应：管理员得到的是未识别命令的回复，不是一条
                # 「已生成待确认操作」的回执。
                self.assertTrue(outcome.handled)
                self.assertIn("/admin help", outcome.reply_text)
                self.assertNotIn("待确认", outcome.reply_text)
                # 零授权副作用。
                self.assertEqual(pending_actions.prepare_calls, [])
                self.assertEqual(confirm_cards.send_calls, [])
                self.assertEqual(audit.actions(), ["admin.command.unknown"])
                for action in audit.actions():
                    self.assertNotIn("permission", action)

    def test_help_no_longer_advertises_the_retired_commands(self) -> None:
        """帮助文案里不再公开一条已经不受理的命令。"""

        router, _, _, _ = _router()

        outcome = router.route(open_id=ADMIN_OPEN_ID, text="/admin help", trace_id="t1")

        self.assertNotIn("grant_permission", outcome.reply_text)
        self.assertNotIn("suppress_permission", outcome.reply_text)
        # 不误伤：仍在受理的写命令照常公开。
        self.assertIn("revoke_permission", outcome.reply_text)
        self.assertIn("suspend", outcome.reply_text)


#: 与 test_admin_commands.py 同一固定字面量（`lpo_` 前缀 + 26 位 Crockford
#: Base32 ULID），供本文件的收回派发用例复用。
_VALID_OVERRIDE_ID = "lpo_01JGFJJZ008XSHEADGG8V74SPC"


class RevokePermissionDispatchTests(unittest.TestCase):
    """``revoke_permission`` 写命令编排（卡 B 设计卡）：与 suspend/resume/
    grant/suppress 共用同一套 ``_dispatch_write_action`` 骨架，但自我目标防呆
    **不**在这一层（见 ``core/admin/router._dispatch_write_action`` 文档「检查点
    位置不同」）——``target_identifier`` 对 revoke 命令而言是 override_id，不是
    open_id，router 层拿不到属主信息，因此本类不重复卡 A 那条
    ``self_target_grant_is_rejected`` 用例，那条防呆的真库断言在
    ``tests/test_pending_action_postgres.py``。
    """

    def _revoke_text(self, override_id: str = _VALID_OVERRIDE_ID, reason: str = "离职") -> str:
        return f"/admin revoke_permission {override_id} {reason}"

    def test_unregistered_sender_is_default_denied_same_as_read_only_commands(self) -> None:
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
            text=self._revoke_text(),
            trace_id="t1",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
        )

        self.assertFalse(outcome.handled)
        self.assertEqual(pending_actions.prepare_calls, [])
        self.assertEqual(confirm_cards.send_calls, [])

    def test_partial_role_entry_is_rejected_before_reaching_write_dispatch(self) -> None:
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
            text=self._revoke_text(),
            trace_id="t1",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
        )

        self.assertFalse(outcome.handled)
        self.assertEqual(pending_actions.prepare_calls, [])

    def test_successful_revoke_dispatch_forwards_override_id_and_reason_to_prepare(self) -> None:
        """成功路径：``command.identifier``（override_id）原样作为
        ``target_open_id`` 传给 ``prepare()``（真正解析出属主 open_id 是
        adapter 的职责，见 ``adapters/postgres_pending_action.py``），
        ``company_id``/``metric_name`` 保持 ``None``。"""

        pending = _prepared_pending(action_type=PendingActionType.LOCAL_PERMISSION_REVOKE)
        pending_actions = FakePendingActions(
            outcome=_FakePrepareOutcome(decision=_FakePrepareDecision(ok=True), pending=pending)
        )
        confirm_cards = FakeConfirmCards(delivered=True)
        router, _, _, audit = _router(pending_actions=pending_actions, confirm_cards=confirm_cards)

        outcome = router.route(
            open_id=ADMIN_OPEN_ID,
            text=self._revoke_text(reason="离职 交接"),
            trace_id="t1",
            chat_id="oc_1",
            thread_id="thread_1",
            message_id="om_1",
        )

        self.assertTrue(outcome.handled)
        self.assertIn("待确认", outcome.reply_text)
        self.assertEqual(len(pending_actions.prepare_calls), 1)
        call = pending_actions.prepare_calls[0]
        self.assertEqual(call["action_type"], PendingActionType.LOCAL_PERMISSION_REVOKE)
        self.assertEqual(call["target_open_id"], _VALID_OVERRIDE_ID)
        self.assertIsNone(call["company_id"])
        self.assertIsNone(call["metric_name"])
        self.assertEqual(call["reason"], "离职 交接")
        self.assertEqual(audit.actions(), ["admin.command.revoke_permission"])
        self.assertEqual(audit.records[0][1]["pending_action_id"], pending.id)

    def test_prepare_rejection_is_reported_without_sending_a_card(self) -> None:
        """否定断言：override_id 不存在/已撤销/属主等于操作者——adapter 层拒绝，
        router 只负责把拒绝文案透传，不发卡片（真库断言见
        ``tests/test_pending_action_postgres.py``）。"""

        pending_actions = FakePendingActions(
            outcome=_FakePrepareOutcome(
                decision=_FakePrepareDecision(
                    ok=False, code="self_target_forbidden", message="不能对自己发起该操作。"
                )
            )
        )
        confirm_cards = FakeConfirmCards()
        router, _, _, audit = _router(pending_actions=pending_actions, confirm_cards=confirm_cards)

        outcome = router.route(
            open_id=ADMIN_OPEN_ID,
            text=self._revoke_text(),
            trace_id="t1",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.reply_text, "不能对自己发起该操作。")
        self.assertEqual(confirm_cards.send_calls, [])

    def test_not_wired_replies_unavailable_without_crashing(self) -> None:
        router, _, _, audit = _router()  # pending_actions/confirm_cards 均未传入

        outcome = router.route(
            open_id=ADMIN_OPEN_ID,
            text=self._revoke_text(),
            trace_id="t1",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
        )

        self.assertTrue(outcome.handled)
        self.assertIn("不可用", outcome.reply_text)

    def test_help_text_mentions_revoke_permission(self) -> None:
        router, _, _, _ = _router()

        outcome = router.route(open_id=ADMIN_OPEN_ID, text="/admin help", trace_id="t1")

        self.assertIn("revoke_permission", outcome.reply_text)


class RevokePermissionShapeTwoDispatchTests(unittest.TestCase):
    """#439 A 档新增的 revoke 形状 2（``<identifier> <company_id> <metric_name>
    <reason...>``）：router 层在调用 ``prepare()`` 之前先反查 override_id，
    找到后退化成与形状 1 完全相同的下游调用；找不到时直接回复，不产生任何待
    确认操作。"""

    def _revoke_shape_two_text(
        self,
        *,
        identifier: str = "ou_target",
        company_id: str = "1011",
        metric_name: str = "daily_active",
        reason: str = "离职",
    ) -> str:
        return f"/admin revoke_permission {identifier} {company_id} {metric_name} {reason}"

    def test_found_override_id_degrades_to_the_same_prepare_call_as_shape_one(self) -> None:
        pending = _prepared_pending(action_type=PendingActionType.LOCAL_PERMISSION_REVOKE)
        pending_actions = FakePendingActions(
            outcome=_FakePrepareOutcome(decision=_FakePrepareDecision(ok=True), pending=pending)
        )
        confirm_cards = FakeConfirmCards(delivered=True)
        queries = FakeQueries(
            overrides_by_key={("ou_target", "1011", "daily_active"): _VALID_OVERRIDE_ID}
        )
        router, _, _, audit = _router(
            queries=queries, pending_actions=pending_actions, confirm_cards=confirm_cards
        )

        outcome = router.route(
            open_id=ADMIN_OPEN_ID,
            text=self._revoke_shape_two_text(),
            trace_id="t1",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
        )

        self.assertTrue(outcome.handled)
        self.assertIn("待确认", outcome.reply_text)
        self.assertEqual(len(pending_actions.prepare_calls), 1)
        call = pending_actions.prepare_calls[0]
        # 找到 override_id 后，下游调用与形状 1 逐字节相同——company_id/
        # metric_name 依旧保持 None，见 commands.py「退化成与形状 1 完全相同的
        # 下游调用」文档。
        self.assertEqual(call["target_open_id"], _VALID_OVERRIDE_ID)
        self.assertIsNone(call["company_id"])
        self.assertIsNone(call["metric_name"])
        self.assertEqual(
            queries.resolve_override_calls,
            [("ou_target", "1011", "daily_active")],
        )

    def test_email_identifier_and_chinese_metric_alias_are_resolved_before_the_lookup(
        self,
    ) -> None:
        pending = _prepared_pending(action_type=PendingActionType.LOCAL_PERMISSION_REVOKE)
        pending_actions = FakePendingActions(
            outcome=_FakePrepareOutcome(decision=_FakePrepareDecision(ok=True), pending=pending)
        )
        confirm_cards = FakeConfirmCards(delivered=True)
        queries = FakeQueries(
            identifier_aliases={"someone@example.com": "ou_target"},
            metric_aliases={"新增用户数": "sub_new_count"},
            overrides_by_key={("ou_target", "1011", "sub_new_count"): _VALID_OVERRIDE_ID},
        )
        router, _, _, _ = _router(
            queries=queries, pending_actions=pending_actions, confirm_cards=confirm_cards
        )

        outcome = router.route(
            open_id=ADMIN_OPEN_ID,
            text=self._revoke_shape_two_text(
                identifier="someone@example.com", metric_name="新增用户数"
            ),
            trace_id="t1",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(len(pending_actions.prepare_calls), 1)
        self.assertEqual(pending_actions.prepare_calls[0]["target_open_id"], _VALID_OVERRIDE_ID)

    def test_unmatched_lookup_replies_without_touching_prepare(self) -> None:
        """否定断言：零命中/多命中歧义（``resolve_override_id`` 返回 ``None``）
        直接回复，不产生任何待确认操作、不发送任何卡片。"""

        pending_actions = FakePendingActions(
            outcome=_FakePrepareOutcome(decision=_FakePrepareDecision(ok=True))
        )
        confirm_cards = FakeConfirmCards()
        router, _, queries, audit = _router(
            pending_actions=pending_actions, confirm_cards=confirm_cards
        )

        outcome = router.route(
            open_id=ADMIN_OPEN_ID,
            text=self._revoke_shape_two_text(),
            trace_id="t1",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
        )

        self.assertTrue(outcome.handled)
        self.assertIn("未找到匹配的当前生效本地覆盖", outcome.reply_text)
        self.assertEqual(pending_actions.prepare_calls, [])
        self.assertEqual(confirm_cards.send_calls, [])
        self.assertEqual(audit.records[-1][1]["code"], "override_not_found")

    def test_unmatched_lookup_reply_points_to_a_reachable_recovery_path(self) -> None:
        """Trace #469 修复包 B，B-3：此前这句兜底指引说"或改用覆盖ID精确指定
        撤销"——本批起 ``/admin user`` 不再展示 override_id（Trace #469 S-1，
        见 ``_render_local_overrides`` 文档），这条路径已经是死路，管理员看到
        指引也无法照做。修复后必须改指真实可行路径（管理卡逐行「撤销」按钮），
        且不能再提"覆盖ID"这个已经不可获得的东西。"""

        pending_actions = FakePendingActions(
            outcome=_FakePrepareOutcome(decision=_FakePrepareDecision(ok=True))
        )
        confirm_cards = FakeConfirmCards()
        router, _, queries, audit = _router(
            pending_actions=pending_actions, confirm_cards=confirm_cards
        )

        outcome = router.route(
            open_id=ADMIN_OPEN_ID,
            text=self._revoke_shape_two_text(),
            trace_id="t1",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
        )

        self.assertNotIn("覆盖ID", outcome.reply_text, "不得再指向已经死掉的覆盖ID路径")
        self.assertIn("管理卡", outcome.reply_text)
        self.assertIn("撤销", outcome.reply_text)

    def test_unregistered_sender_is_default_denied_same_as_shape_one(self) -> None:
        pending_actions = FakePendingActions(
            outcome=_FakePrepareOutcome(decision=_FakePrepareDecision(ok=True))
        )
        confirm_cards = FakeConfirmCards()
        router, _, queries, _ = _router(
            registry=FakeRegistry({}),
            pending_actions=pending_actions,
            confirm_cards=confirm_cards,
        )

        outcome = router.route(
            open_id="ou_never_registered",
            text=self._revoke_shape_two_text(),
            trace_id="t1",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
        )

        self.assertFalse(outcome.handled)
        # 未通过身份判定，压根不会走到反查这一步。
        self.assertEqual(queries.resolve_override_calls, [])
        self.assertEqual(pending_actions.prepare_calls, [])


#: 一个只出现在测试里的公开形态邮箱（协作约定：夹具不得出现真实内部标识）。
_LINK_TEST_EMAIL = "someone@example.com"


class LinkifiedEmailRoutingTests(unittest.TestCase):
    """Issue #492：链接化的邮箱在**整条路由**上走通，不只在解析器里。

    现场：产品负责人 2026-08-31 真人操作，输入的邮箱被飞书编辑器自动转成了
    ``mailto:`` 链接，连续三条命令都只收到"未识别的管理命令"。裁定原话：
    **不能因为带了 mailto 就未识别**。
    """

    def test_markdown_linkified_email_reaches_the_query_with_the_bare_address(self) -> None:
        queries = FakeQueries(
            users={
                "ou_target": AdminUserStatusView(
                    identifier="ou_target",
                    provisioning_state="active",
                    account_state="enabled",
                    permission_version=1,
                    updated_at="2026-08-31T00:00:00+00:00",
                )
            },
            identifier_aliases={_LINK_TEST_EMAIL: "ou_target"},
        )
        router, _, _, audit = _router(queries=queries)

        outcome = router.route(
            open_id=ADMIN_OPEN_ID,
            text=f"/admin user [{_LINK_TEST_EMAIL}](mailto:{_LINK_TEST_EMAIL})",
            trace_id="t1",
        )

        self.assertTrue(outcome.handled)
        # 反查拿到的是裸邮箱（链接外壳已在解析层剥掉），查询拿到的是反查后的 open_id。
        self.assertEqual(queries.resolve_identifier_calls, [_LINK_TEST_EMAIL])
        self.assertEqual(queries.user_calls, ["ou_target"])
        self.assertEqual(audit.actions(), ["admin.command.query_user"])

    def test_bare_mailto_scheme_also_reaches_the_query(self) -> None:
        """``mailto:a@b.com`` 此前不落 UNKNOWN（``:`` 本来就在字符集里），而是被
        当成标识原样送去反查、查无此人——同一缺陷更隐蔽的一副面孔。"""

        queries = FakeQueries(identifier_aliases={_LINK_TEST_EMAIL: "ou_target"})
        router, _, _, _ = _router(queries=queries)

        router.route(
            open_id=ADMIN_OPEN_ID, text=f"/admin user mailto:{_LINK_TEST_EMAIL}", trace_id="t1"
        )

        self.assertEqual(queries.resolve_identifier_calls, [_LINK_TEST_EMAIL])

    def test_unsupported_link_forms_stop_before_identifier_resolution(self) -> None:
        """#492 的收窄边界在路由层也要保持 fail closed：不把链接化 open_id、
        不一致目标或任意 URL 送到 ``resolve_identifier``/下游查询。"""

        unsupported = (
            f"/admin user <{_LINK_TEST_EMAIL}>",
            f"/admin user `{_LINK_TEST_EMAIL}`",
            "/admin user [ou_abc123](mailto:ou_abc123)",
            f"/admin user [seen@example.com](mailto:{_LINK_TEST_EMAIL})",
            f"/admin user [{_LINK_TEST_EMAIL}](https://example.com/user)",
        )
        for text in unsupported:
            with self.subTest(text=text):
                router, _, queries, audit = _router()
                outcome = router.route(open_id=ADMIN_OPEN_ID, text=text, trace_id="t1")

                self.assertTrue(outcome.handled)
                self.assertIn("用户标识", outcome.reply_text)
                self.assertEqual(queries.resolve_identifier_calls, [])
                self.assertEqual(queries.user_calls, [])
                self.assertEqual(audit.actions(), ["admin.command.unknown"])


class SegmentedUnknownReplyTests(unittest.TestCase):
    """Issue #492 完成标准 4：解析失败时说清**哪一段**没看懂。

    "未识别的管理命令，请发送 /admin help 查看可用命令"这句话不含任何可据以修正的
    信息——产品负责人连踩三次时，无法判断是邮箱被客户端链接化了（假设 1）还是公司
    那一段填了中文名（假设 2），两种情形此前产生**逐字相同**的回复。
    """

    def test_chinese_company_name_reply_names_the_company_segment(self) -> None:
        """假设 2 的自救出口：公司参数期望公司编号，输中文名被拒是**正确行为**
        （不放宽字符集去接受 CJK，那是语义变更）——缺陷只在于没说清楚。"""

        router, _, queries, audit = _router()

        outcome = router.route(
            open_id=ADMIN_OPEN_ID,
            text="/admin revoke_permission ou_target 一零一一 daily_active 特批",
            trace_id="t1",
        )

        self.assertTrue(outcome.handled)
        self.assertIn("公司标识", outcome.reply_text)
        self.assertIn("公司编号", outcome.reply_text)
        self.assertEqual(queries.user_calls, [])
        self.assertEqual(audit.actions(), ["admin.command.unknown"])
        self.assertEqual(audit.records[0][1]["reject_reason"], "bad_company_id")

    def test_unparseable_identifier_reply_names_the_identifier_segment(self) -> None:
        router, _, _, audit = _router()

        outcome = router.route(open_id=ADMIN_OPEN_ID, text="/admin user ou_a;b", trace_id="t1")

        self.assertIn("用户标识", outcome.reply_text)
        self.assertEqual(audit.records[0][1]["reject_reason"], "bad_identifier")

    def test_unknown_subcommand_reply_names_the_command_name_segment(self) -> None:
        router, _, _, audit = _router()

        outcome = router.route(open_id=ADMIN_OPEN_ID, text="/admin delete_user ou_1", trace_id="t1")

        self.assertIn("命令名", outcome.reply_text)
        self.assertEqual(audit.records[0][1]["reject_reason"], "unknown_subcommand")

    def test_plain_chat_text_keeps_the_generic_reply_word_for_word(self) -> None:
        """不误伤（完成标准 3）：管理命令面**没有 ``/admin`` 前缀预检**，已登记
        管理员发的任何一句闲聊都会走到 UNKNOWN 分支。对这些输入做分段报错等于对
        每句闲聊解释命令语法——既有那句笼统文案逐字保留。"""

        router, _, _, audit = _router()

        outcome = router.route(open_id=ADMIN_OPEN_ID, text="不知道说什么", trace_id="t1")

        self.assertEqual(outcome.reply_text, "未识别的管理命令，请发送 /admin help 查看可用命令。")
        self.assertEqual(audit.records[0][1]["reject_reason"], "not_a_command")

    def test_the_reply_never_echoes_the_admin_input(self) -> None:
        """否定断言：分段报错只说段名与期望形状，**不回显输入原文**。

        出站是一条飞书文本消息，飞书文本消息里的 ``<at user_id="all"></at>`` 一类
        标记是有语义的；把输入拼进回复等于把一段可控文本反射进出站消息。段名 +
        期望形状已经够自救，不值得为这点便利开一个反射面。
        """

        router, _, _, _ = _router()
        payload = "ou_a;;;;<at></at>"

        outcome = router.route(open_id=ADMIN_OPEN_ID, text=f"/admin user {payload}", trace_id="t1")

        self.assertNotIn(payload, outcome.reply_text)
        self.assertNotIn(";;;;", outcome.reply_text)
        self.assertNotIn("<at", outcome.reply_text)


class UnknownCommandForensicsTests(unittest.TestCase):
    """Trace #521 F4-1/F4-3：``admin.command.unknown`` 的取证字段与自救文案。

    #492 的 W0-2 调查卡在同一个位置两次：一条失败只留下 ``reject_reason`` 一个枚举
    名，"客户端把邮箱拆成了两段"和"管理员真的多打了一个参数"产生**逐字相同**的
    审计，两个竞争假设无法区分。本组用例把"下一次复现能被指认"钉死。
    """

    _EMAIL = "someone@example.com"

    def test_admin_prefixed_failure_records_shapes_and_raw_text(self) -> None:
        router, _, _, audit = _router()

        router.route(
            open_id=ADMIN_OPEN_ID,
            text=f"/admin audit {self._EMAIL} (mailto:not-an-email) 24",
            trace_id="t1",
        )

        action, fields = audit.records[0]
        self.assertEqual(action, "admin.command.unknown")
        self.assertEqual(fields["reject_reason"], "wrong_argument_count")
        self.assertEqual(fields["token_count"], 3)
        self.assertEqual(
            fields["token_shapes"],
            "admin_prefix,bare_word,email,paren_wrapped,digits",
        )
        self.assertEqual(
            fields["raw_admin_text"],
            f"/admin audit {self._EMAIL} (mailto:not-an-email) 24",
        )

    def test_token_shapes_carry_no_input_text(self) -> None:
        """否定断言：形状串是固定分类名，不含任何输入片段。"""

        router, _, _, audit = _router()

        router.route(
            open_id=ADMIN_OPEN_ID,
            text="/admin user ou_a;;;;<at></at> 多余",
            trace_id="t1",
        )

        shapes = audit.records[0][1]["token_shapes"]
        self.assertNotIn(";;;;", shapes)
        self.assertNotIn("<at", shapes)
        self.assertNotIn("多余", shapes)

    def test_plain_chat_records_no_raw_text_and_no_shapes(self) -> None:
        """不误伤：不是以 ``/admin`` 开头的闲聊一个字都不记（既有"不保存正文"纪律）。"""

        router, _, _, audit = _router()

        router.route(open_id=ADMIN_OPEN_ID, text="今天数据怎么样", trace_id="t1")

        fields = audit.records[0][1]
        self.assertEqual(fields["reject_reason"], "not_a_command")
        self.assertNotIn("raw_admin_text", fields)
        self.assertNotIn("token_shapes", fields)
        self.assertNotIn("token_count", fields)

    def test_raw_admin_text_is_truncated_instead_of_unbounded(self) -> None:
        router, _, _, audit = _router()
        overlong = "/admin audit " + "x" * 4000

        router.route(open_id=ADMIN_OPEN_ID, text=overlong, trace_id="t1")

        raw = audit.records[0][1]["raw_admin_text"]
        self.assertLess(len(raw), len(overlong))
        self.assertTrue(raw.endswith("…[truncated]"))

    def test_reply_tells_the_admin_how_many_segments_arrived(self) -> None:
        """F4-3：管理员发的是"一个邮箱 + 24"两段，看到"实际收到 3 段"才可能自救。"""

        router, _, _, _ = _router()

        outcome = router.route(
            open_id=ADMIN_OPEN_ID,
            text=f"/admin audit {self._EMAIL} (mailto:not-an-email) 24",
            trace_id="t1",
        )

        self.assertIn("实际收到 3 段", outcome.reply_text)

    def test_plain_chat_reply_stays_word_for_word_without_any_count(self) -> None:
        router, _, _, _ = _router()

        outcome = router.route(open_id=ADMIN_OPEN_ID, text="今天数据怎么样", trace_id="t1")

        self.assertEqual(outcome.reply_text, "未识别的管理命令，请发送 /admin help 查看可用命令。")
        self.assertNotIn("实际收到", outcome.reply_text)

    def test_unknown_reply_is_governed_by_the_versioned_content_catalog(self) -> None:
        """F4-3：这两句话进了 ``config/content.toml``，审计版本随目录走。"""

        router, _, _, _ = _router()
        catalog_version = default_content_catalog().version

        detailed = router.route(open_id=ADMIN_OPEN_ID, text="/admin delete_user x", trace_id="t1")
        generic = router.route(open_id=ADMIN_OPEN_ID, text="闲聊", trace_id="t2")

        self.assertEqual(detailed.content_key, "admin.unknown_command_detail")
        self.assertEqual(detailed.content_version, catalog_version)
        self.assertEqual(generic.content_key, "admin.unknown_command")
        self.assertEqual(generic.content_version, catalog_version)


class MultiTokenLinkifiedCommandRoutingTests(unittest.TestCase):
    """Trace #521 W0-1：被客户端拆成多段的邮箱，路由层要真的执行那条查询。"""

    _EMAIL = "someone@example.com"

    def test_anchor_shaped_email_reaches_the_same_audit_query(self) -> None:
        anchor = f'<a href="mailto:{self._EMAIL}">{self._EMAIL}</a>'
        router, _, queries, audit = _router()

        outcome = router.route(
            open_id=ADMIN_OPEN_ID, text=f"/admin audit {anchor} 24", trace_id="t1"
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(audit.actions(), ["admin.command.query_audit"])
        self.assertEqual(
            queries.event_calls, [{"identifier": self._EMAIL, "window_hours": 24, "limit": 20}]
        )

    def test_a_mismatched_pair_is_still_refused_at_the_router(self) -> None:
        """fail closed 一路贯穿到路由：显示≠目标不会变成一次查询。"""

        router, _, queries, audit = _router()

        outcome = router.route(
            open_id=ADMIN_OPEN_ID,
            text=f'/admin audit <a href="mailto:{self._EMAIL}">某人</a> 24',
            trace_id="t1",
        )

        self.assertTrue(outcome.handled)
        self.assertEqual(queries.event_calls, [])
        self.assertEqual(audit.actions(), ["admin.command.unknown"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class DocumentDeliveryReasonLabelCoverageTests(unittest.TestCase):
    """新增降级原因码却忘了登记显示名，`/admin trace` 会退化成「原值（未登记显示名）」。

    这是一条**真实发生过**的遗漏：Trace #544 S-7c 改走服务端一次建档，新增
    `body_too_long` / `title_not_embeddable` / `server_simplified_body` 三个码，
    三个都没进词表——而这恰恰是管理员最常需要解释给用户听的三种情况。

    兜底样式本身是对的（不吞掉、不假装认识），但它是**兜底**，不该成为常态。
    这条用例把「适配器定义了什么码」和「管理台认识什么码」绑在一起：加码不加
    词条就变红，不必依赖谁记得。
    """

    def test_every_degrade_reason_the_adapter_can_emit_has_a_display_name(self) -> None:
        from lingxi.adapters import feishu_docx_delivery
        from lingxi.core.admin.router_render import _DOCUMENT_DELIVERY_REASON_LABEL

        emitted = {
            feishu_docx_delivery.BODY_TOO_LONG,
            feishu_docx_delivery.TITLE_NOT_EMBEDDABLE,
            feishu_docx_delivery.SERVER_SIMPLIFIED_BODY,
        }
        missing = sorted(emitted - set(_DOCUMENT_DELIVERY_REASON_LABEL))
        self.assertEqual(
            missing,
            [],
            "这些降级原因码适配器会发出、但管理台没有显示名，`/admin trace` 会显示成"
            "「原值（未登记显示名）」：" + ", ".join(missing),
        )
