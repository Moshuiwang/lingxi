"""#493 职位 + 公司范围管理卡的纯逻辑与安全护栏。"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from lingxi.config.content import default_content_catalog
from lingxi.core.admin.card_dispatch import (
    MANAGEMENT_CARD_CONTEXT_DEFAULT_TTL_SECONDS,
    MANAGEMENT_CARD_CONTEXT_MAX_TTL_SECONDS,
    ManagementCardContextStore,
    management_card_fingerprint,
)
from lingxi.core.admin.commands import AdminCommandKind, parse_admin_command
from lingxi.core.admin.management_card import (
    ADMIN_ACTION_CANCEL,
    ADMIN_ACTION_GRANT,
    render_management_card,
)
from lingxi.core.admin.pending_action import PendingAction, PendingActionStatus, PendingActionType
from lingxi.core.admin.router import AdminRouteOutcome
from lingxi.core.admin.views import AdminUserStatusView, LocalPermissionOverrideView
from lingxi.core.permission.position_override import expand_position_scope
from lingxi.core.permission.targeted_recompute import (
    SKIP_ACCOUNT_NOT_ENABLED,
    RecomputeKind,
    TargetedRecomputeOutcome,
)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


class _PositionCatalog:
    def companies(self) -> list[str]:
        return ["c1", "c2", "c3"]

    def metrics(self) -> list[str]:
        return ["m1", "m2"]

    def positions(self) -> list[str]:
        return ["A运营", "A财务"]


class _DisplayNames:
    def company_labels(self, *, company_ids):
        return {company_id: f"公司-{company_id}" for company_id in company_ids}

    def metric_labels(self, *, metric_ids):
        return {metric_id: f"指标-{metric_id}" for metric_id in metric_ids}


def _status() -> AdminUserStatusView:
    return AdminUserStatusView(
        identifier="ou_target",
        provisioning_state="active",
        account_state="enabled",
        permission_version=1,
        updated_at="2026-08-31T12:00:00+00:00",
    )


def _walk(elements):
    for element in elements:
        yield element
        if element.get("tag") == "form":
            yield from _walk(element.get("elements", ()))
        elif element.get("tag") == "column_set":
            for column in element.get("columns", ()):
                yield from _walk(column.get("elements", ()))


class PositionExpansionTests(unittest.TestCase):
    def test_all_scope_expands_every_actual_company_and_preserves_pairs(self) -> None:
        expansion = expand_position_scope(
            position_name="A运营",
            company_scope="*",
            role_function_map={"A运营": "运营"},
            company_function_metric_map={
                "c1": {"运营": ["m2", "m1"]},
                "c2": {"运营": ["m1"]},
                "c3": {"运营": ["m3"]},
            },
            available_companies=["c1", "c2", "c3"],
        )

        self.assertEqual(expansion.company_scope, "*")
        self.assertEqual(expansion.companies, ("c1", "c2", "c3"))
        self.assertEqual(
            expansion.pairs,
            (("c1", "m2"), ("c1", "m1"), ("c2", "m1"), ("c3", "m3")),
        )

    def test_missing_company_mapping_fails_closed_instead_of_silently_dropping_one(self) -> None:
        with self.assertRaises(ValueError):
            expand_position_scope(
                position_name="A运营",
                company_scope="*",
                role_function_map={"A运营": "运营"},
                company_function_metric_map={"c1": {"运营": ["m1"]}},
                available_companies=["c1", "c2"],
            )

    def test_wildcard_mapping_is_not_a_default_for_a_missing_actual_company(self) -> None:
        """`*` 只服务于全公司语义，不能掩盖单个实际公司的映射缺口。"""

        with self.assertRaises(ValueError):
            expand_position_scope(
                position_name="A运营",
                company_scope="*",
                role_function_map={"A运营": "运营"},
                company_function_metric_map={
                    "c1": {"运营": ["m1"]},
                    "*": {"运营": ["m1"]},
                },
                available_companies=["c1", "c2"],
            )

    def test_position_and_company_scope_are_exact_inputs(self) -> None:
        command = parse_admin_command("/admin grant_position u@example.com A运营 全部 原因")
        self.assertEqual(command.kind, AdminCommandKind.GRANT_POSITION_PERMISSION)
        self.assertEqual(command.position_name, "A运营")
        self.assertEqual(command.company_scope, "*")

        missing_reason = parse_admin_command("/admin grant_position u@example.com A运营 c1")
        self.assertEqual(missing_reason.kind, AdminCommandKind.UNKNOWN)

    def test_permission_group_id_is_a_closed_revoke_target_shape(self) -> None:
        command = parse_admin_command(
            "/admin revoke_permission lpg_01M1C90YDGMTY567GDTZZJ4C5E 管理卡撤销"
        )
        self.assertEqual(command.kind, AdminCommandKind.REVOKE_PERMISSION)
        self.assertEqual(command.identifier, "lpg_01M1C90YDGMTY567GDTZZJ4C5E")


class PositionManagementCardTests(unittest.TestCase):
    def test_new_card_has_required_position_scope_reason_and_actual_all_count(self) -> None:
        card = render_management_card(
            _status(),
            display_identifier="u@example.com",
            catalog=_PositionCatalog(),
            display_names=_DisplayNames(),
        )
        elements = list(_walk(card["body"]["elements"]))
        forms = [element for element in elements if element.get("tag") == "form"]
        self.assertEqual(len(forms), 1)
        form_elements = forms[0]["elements"]
        fields = {element["name"]: element for element in _walk(form_elements) if "name" in element}
        self.assertTrue(fields["position_name"]["required"])
        self.assertTrue(fields["company_scope"]["required"])
        self.assertTrue(fields["reason"]["required"])
        scope = fields["company_scope"]
        self.assertIn("全部（3 家公司）", [option["text"]["content"] for option in scope["options"]])

        buttons = [element for element in elements if element.get("tag") == "button"]
        actions = [button["behaviors"][0]["value"].get("admin_action") for button in buttons]
        self.assertIn(ADMIN_ACTION_GRANT, actions)
        self.assertIn(ADMIN_ACTION_CANCEL, actions)
        self.assertNotIn("屏蔽指标", "\n".join(str(element) for element in elements))
        self.assertNotIn("suppress", actions)

    def test_submitted_card_has_no_editable_form(self) -> None:
        card = render_management_card(
            _status(),
            display_identifier="u@example.com",
            catalog=_PositionCatalog(),
            display_names=_DisplayNames(),
            submitted=True,
            dispatch_status="操作已记录；权限正在下发",
        )
        elements = list(_walk(card["body"]["elements"]))
        self.assertFalse([element for element in elements if element.get("tag") == "form"])
        visible = "\n".join(
            element.get("content", "") for element in elements if element.get("tag") == "markdown"
        )
        self.assertIn("已提交，请在下方确认卡片上确认（10 分钟内有效）", visible)
        self.assertIn("操作已记录；权限正在下发", visible)

    def test_terminal_refresh_restores_form_after_async_result(self) -> None:
        """异步下发终态只更新状态，不能把原管理卡永久锁成只读。"""

        from lingxi.apps.gateway import _GatewayManagementCardRefresher

        class _Transport:
            def __init__(self) -> None:
                self.updated: list[dict] = []

            def update(self, **kwargs):
                self.updated.append(kwargs)

        class _ContextStore:
            def next_card_sequence(self, *, message_id: str) -> int:
                self.message_id = message_id
                return 3

        transport = _Transport()
        refresher = _GatewayManagementCardRefresher(
            transport=transport,
            catalog=_PositionCatalog(),
            display_names=_DisplayNames(),
            context_store=_ContextStore(),
        )
        context = type(
            "Context",
            (),
            {"message_id": "om_1", "card_id": "card_1", "identifier": "u@example.com"},
        )()

        refresher.update(context=context, status=_status(), state="effective")

        self.assertEqual(len(transport.updated), 1)
        elements = list(_walk(transport.updated[0]["card"]["body"]["elements"]))
        self.assertTrue([element for element in elements if element.get("tag") == "form"])
        self.assertIn("已生效", "\n".join(
            element.get("content", "") for element in elements if element.get("tag") == "markdown"
        ))

    def test_cardkit_failure_does_not_advance_visual_watermark_before_success(self) -> None:
        from lingxi.apps.gateway import _GatewayManagementCardRefresher

        class _Transport:
            def __init__(self) -> None:
                self.calls = 0

            def update(self, **kwargs) -> None:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("CardKit transient")

        store = ManagementCardContextStore()
        store.remember(
            message_id="om_cardkit_failure",
            identifier="u@example.com",
            card_id="card_cardkit_failure",
            chat_id="oc_1",
            initiated_by_open_id="ou_admin",
            snapshot_fingerprint="fp",
        )
        store.update_state(
            message_id="om_cardkit_failure", state="effective", dispatch_status="effective"
        )
        transport = _Transport()
        refresher = _GatewayManagementCardRefresher(
            transport=transport,
            catalog=_PositionCatalog(),
            display_names=_DisplayNames(),
            context_store=store,
        )
        context = store.lookup_context(message_id="om_cardkit_failure")
        assert context is not None
        with self.assertRaises(RuntimeError):
            refresher.update(context=context, status=_status(), state="effective")
        self.assertEqual(len(store.list_needing_refresh()), 1)

        context = store.lookup_context(message_id="om_cardkit_failure")
        assert context is not None
        refresher.update(context=context, status=_status(), state="effective")
        self.assertEqual(store.list_needing_refresh(), ())

    def test_visual_update_failure_keeps_persistent_refresh_watermark_for_retry(self) -> None:
        from lingxi.apps.gateway import _ManagementCardRecoveryScanner

        store = ManagementCardContextStore()
        store.remember(
            message_id="om_recovery",
            identifier="u@example.com",
            card_id="card_recovery",
            chat_id="oc_1",
            initiated_by_open_id="ou_admin",
            snapshot_fingerprint="fp",
        )
        store.update_state(message_id="om_recovery", state="effective", dispatch_status="effective")
        calls: list[object] = []
        attempts = [RuntimeError("CardKit transient"), None]

        class _Audit:
            def record(self, action: str, /, **fields: object) -> None:
                calls.append((action, fields))

        class _Refresher:
            def update(self, *, context, **kwargs) -> None:
                calls.append(context.message_id)
                failure = attempts.pop(0)
                if failure is not None:
                    raise failure
                sequence = store.next_card_sequence(message_id=context.message_id)
                store.mark_visual_refreshed(message_id=context.message_id, sequence=sequence)

        scanner = _ManagementCardRecoveryScanner(
            context_store=store,
            refresher=_Refresher(),
            status_lookup=lambda _identifier: _status(),
            audit=_Audit(),
        )
        self.assertEqual(scanner.scan(), 0)
        self.assertEqual(len(store.list_needing_refresh()), 1)
        # 用新的 scanner 实例模拟 gateway 在瞬时 CardKit 失败后重启；重试依据是
        # store 中的持久水位，而不是上一进程的 observer/内存状态。
        restarted_scanner = _ManagementCardRecoveryScanner(
            context_store=store,
            refresher=_Refresher(),
            status_lookup=lambda _identifier: _status(),
            audit=_Audit(),
        )
        self.assertEqual(restarted_scanner.scan(), 1)
        self.assertEqual(store.list_needing_refresh(), ())

    def test_recovery_scanner_drops_old_visual_when_state_changes_after_snapshot(self) -> None:
        """scanner 读旧行后，状态推进必须让旧视觉在取号 CAS 处放弃。"""

        from lingxi.apps.gateway import (
            _GatewayManagementCardRefresher,
            _ManagementCardRecoveryScanner,
        )

        class _Transport:
            def __init__(self) -> None:
                self.updated: list[dict] = []

            def update(self, **kwargs) -> None:
                self.updated.append(kwargs)

        class _Audit:
            def record(self, action: str, /, **fields: object) -> None:
                del action, fields

        store = ManagementCardContextStore()
        store.remember(
            message_id="om_recovery_cas",
            identifier="u@example.com",
            card_id="card_recovery_cas",
            chat_id="oc_1",
            initiated_by_open_id="ou_admin",
            snapshot_fingerprint="fp",
        )
        store.update_state(
            message_id="om_recovery_cas", state="effective", dispatch_status="effective"
        )
        transport = _Transport()
        refresher = _GatewayManagementCardRefresher(
            transport=transport,
            catalog=_PositionCatalog(),
            display_names=_DisplayNames(),
            context_store=store,
        )
        calls = 0

        def status_lookup(_identifier):
            nonlocal calls
            calls += 1
            # This is the concurrent writer between list_needing_refresh() and
            # the stale scanner's sequence claim.
            store.update_state(
                message_id="om_recovery_cas", state="incomplete", dispatch_status="incomplete"
            )
            return _status()

        scanner = _ManagementCardRecoveryScanner(
            context_store=store,
            refresher=refresher,
            status_lookup=status_lookup,
            audit=_Audit(),
        )

        self.assertEqual(scanner.scan(), 0)
        self.assertEqual(calls, 1)
        self.assertEqual(transport.updated, [])
        current = store.lookup_context(message_id="om_recovery_cas")
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.state, "incomplete")
        self.assertTrue(current.needs_refresh)
        # CAS failure must not consume another CardKit sequence.
        self.assertEqual(current.card_sequence, 4)

        # A fresh scanner can now render and deliver the current state after the
        # old snapshot has been rejected, which also exercises restart recovery.
        restarted = _ManagementCardRecoveryScanner(
            context_store=store,
            refresher=refresher,
            status_lookup=lambda _identifier: _status(),
            audit=_Audit(),
        )
        self.assertEqual(restarted.scan(), 1)
        self.assertEqual(len(transport.updated), 1)
        self.assertEqual(transport.updated[0]["sequence"], 5)
        self.assertEqual(store.list_needing_refresh(), ())

    def test_stale_visual_sequence_cannot_clear_new_state_watermark(self) -> None:
        store = ManagementCardContextStore()
        store.remember(
            message_id="om_visual_generation",
            identifier="u@example.com",
            card_id="card_visual_generation",
            chat_id="oc_1",
            initiated_by_open_id="ou_admin",
            snapshot_fingerprint="fp",
        )
        updated = store.update_state(
            message_id="om_visual_generation", state="effective", dispatch_status="effective"
        )
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.card_sequence, 3)
        self.assertTrue(updated.needs_refresh)
        self.assertFalse(
            store.mark_visual_refreshed(message_id="om_visual_generation", sequence=1)
        )
        self.assertEqual(len(store.list_needing_refresh()), 1)
        self.assertTrue(
            store.mark_visual_refreshed(message_id="om_visual_generation", sequence=3)
        )
        self.assertEqual(store.list_needing_refresh(), ())

    def test_closed_card_has_no_form_or_cancel_action(self) -> None:
        """取消/过期后的卡片关闭交互入口，避免按钮状态与服务端状态分叉。"""

        card = render_management_card(
            _status(),
            display_identifier="u@example.com",
            catalog=_PositionCatalog(),
            display_names=_DisplayNames(),
            closed=True,
            dispatch_status="已取消",
        )
        elements = list(_walk(card["body"]["elements"]))
        self.assertFalse([element for element in elements if element.get("tag") == "form"])
        actions = [
            button["behaviors"][0]["value"].get("admin_action")
            for button in elements
            if button.get("tag") == "button"
        ]
        self.assertNotIn(ADMIN_ACTION_CANCEL, actions)
        visible = "\n".join(
            element.get("content", "") for element in elements if element.get("tag") == "markdown"
        )
        self.assertIn("已取消", visible)

    def test_all_scope_override_row_keeps_actual_company_count_visible(self) -> None:
        status = AdminUserStatusView(
            identifier="ou_target",
            provisioning_state="active",
            account_state="enabled",
            permission_version=1,
            updated_at="2026-08-31T12:00:00+00:00",
            local_overrides=(
                LocalPermissionOverrideView(
                    override_id="lpo_position",
                    direction="grant",
                    company_id="c1",
                    metric_name="m1",
                    reason="特批",
                    created_at="2026-08-31T12:00:00+00:00",
                    position_name="A运营",
                    company_scope="*",
                    group_id="pac_group",
                ),
            ),
        )
        card = render_management_card(
            status,
            display_identifier="u@example.com",
            catalog=_PositionCatalog(),
            display_names=_DisplayNames(),
        )
        visible = "\n".join(
            element.get("content", "")
            for element in _walk(card["body"]["elements"])
            if element.get("tag") == "markdown"
        )
        self.assertIn("公司范围 全部（3 家公司）", visible)

    def test_position_group_is_one_visible_item_and_one_group_revoke_action(self) -> None:
        rows = tuple(
            LocalPermissionOverrideView(
                override_id=f"lpo_{index}",
                direction="grant",
                company_id=f"c{index % 3 + 1}",
                metric_name=f"m{index}",
                reason="特批职位范围",
                created_at="2026-08-31T12:00:00+00:00",
                position_name="A运营",
                company_scope="*",
                group_id="lpg_01M1C90YDGMTY567GDTZZJ4C5E",
            )
            for index in range(387)
        )
        status = AdminUserStatusView(
            identifier="ou_target",
            provisioning_state="active",
            account_state="enabled",
            permission_version=1,
            updated_at="2026-08-31T12:00:00+00:00",
            local_overrides=rows,
        )
        card = render_management_card(
            status,
            display_identifier="u@example.com",
            catalog=_PositionCatalog(),
            display_names=_DisplayNames(),
        )
        elements = list(_walk(card["body"]["elements"]))
        revoke_buttons = [
            element
            for element in elements
            if element.get("tag") == "button"
            and element["behaviors"][0]["value"].get("admin_action") == "revoke"
        ]
        self.assertEqual(len(revoke_buttons), 1)
        value = revoke_buttons[0]["behaviors"][0]["value"]
        self.assertEqual(value.get("permission_group_id"), "lpg_01M1C90YDGMTY567GDTZZJ4C5E")
        self.assertNotIn("override_id", value)
        visible = "\n".join(
            element.get("content", "")
            for element in elements
            if element.get("tag") == "markdown"
        )
        self.assertIn("覆盖 387 项权限", visible)


class TerminalOutcomeTextTests(unittest.TestCase):
    @staticmethod
    def _executed(*, payload: str | None) -> PendingAction:
        return PendingAction(
            id="pac_outcome_text",
            action_type=PendingActionType.LOCAL_PERMISSION_GRANT,
            target_open_id="ou_target",
            target_state_snapshot="absent",
            initiated_by_open_id="ou_admin",
            status=PendingActionStatus.EXECUTED,
            card_delivered=True,
            card_id="card_confirm",
            reason="特批",
            created_at=NOW,
            confirm_deadline_at=NOW + timedelta(minutes=10),
            decided_at=NOW,
            decided_by_open_id="ou_admin",
            payload=payload,
        )

    def test_position_scope_confirmation_is_truthfully_waiting(self) -> None:
        from lingxi.core.admin.card_callback import _outcome_text

        self.assertEqual(
            _outcome_text(self._executed(payload='{"position_name":"A运营","company_scope":"c1"}')),
            "操作已记录，权限正在下发",
        )

    def test_legacy_confirmation_also_reports_the_two_phase_result(self) -> None:
        from lingxi.core.admin.card_callback import _outcome_text

        self.assertEqual(_outcome_text(self._executed(payload=None)), "操作已记录，权限正在下发")


class ContextSequenceTests(unittest.TestCase):
    def test_default_context_ttl_is_forty_minutes_and_explicit_deadline_is_hard_capped_at_24h(self) -> None:
        store = ManagementCardContextStore()
        before = datetime.now(UTC)
        store.remember(
            message_id="om_ttl",
            identifier="u@example.com",
            card_id="card_ttl",
            chat_id="oc_1",
            initiated_by_open_id="ou_admin",
            snapshot_fingerprint="fp",
        )
        context = store.lookup_context(message_id="om_ttl")
        self.assertIsNotNone(context)
        assert context is not None
        self.assertGreaterEqual(
            (context.context_deadline_at - before).total_seconds(),
            MANAGEMENT_CARD_CONTEXT_DEFAULT_TTL_SECONDS - 1,
        )
        self.assertLessEqual(
            (context.context_deadline_at - before).total_seconds(),
            MANAGEMENT_CARD_CONTEXT_DEFAULT_TTL_SECONDS + 1,
        )

        store.remember(
            message_id="om_ttl_capped",
            identifier="u@example.com",
            card_id="card_ttl_capped",
            chat_id="oc_1",
            initiated_by_open_id="ou_admin",
            snapshot_fingerprint="fp",
            context_deadline_at=before + timedelta(days=7),
        )
        capped = store.lookup_context(message_id="om_ttl_capped")
        self.assertIsNotNone(capped)
        assert capped is not None
        self.assertLessEqual(
            (capped.context_deadline_at - before).total_seconds(),
            MANAGEMENT_CARD_CONTEXT_MAX_TTL_SECONDS + 1,
        )

        from lingxi.core.admin.card_dispatch import bounded_management_card_deadline

        fixed_now = NOW
        before_24h = fixed_now + timedelta(hours=24) - timedelta(seconds=1)
        self.assertEqual(
            bounded_management_card_deadline(
                now=fixed_now, requested=before_24h, ttl_seconds=1800
            ),
            before_24h,
        )
        exact_24h = fixed_now + timedelta(hours=24)
        self.assertEqual(
            bounded_management_card_deadline(
                now=fixed_now, requested=exact_24h, ttl_seconds=1800
            ),
            exact_24h,
        )
        self.assertEqual(
            bounded_management_card_deadline(
                now=fixed_now, requested=exact_24h + timedelta(seconds=1), ttl_seconds=1800
            ),
            exact_24h,
        )

    def test_context_sequence_is_monotonic_and_expired_context_is_still_recoverable_for_lazy_close(self) -> None:
        clock = [0.0]
        store = ManagementCardContextStore(ttl_seconds=1.0, clock=lambda: clock[0])
        store.remember(
            message_id="om_1",
            card_id="card_1",
            identifier="u@example.com",
            chat_id="oc_1",
            initiated_by_open_id="ou_admin",
            snapshot_fingerprint="fp",
            context_deadline_at=datetime.now(UTC) + timedelta(hours=1),
            card_sequence=2,
        )
        self.assertEqual(store.next_card_sequence(message_id="om_1"), 3)
        self.assertEqual(store.next_card_sequence(message_id="om_1"), 4)
        clock[0] = 1.0
        self.assertIsNone(store.lookup(message_id="om_1"))
        self.assertIsNotNone(store.lookup_context(message_id="om_1"))

    def test_duplicate_registration_does_not_reopen_a_closed_context(self) -> None:
        store = ManagementCardContextStore()
        store.remember(
            message_id="om_1",
            card_id="card_1",
            identifier="u@example.com",
            chat_id="oc_1",
            initiated_by_open_id="ou_admin",
            snapshot_fingerprint="fp",
            context_deadline_at=NOW + timedelta(hours=1),
            card_sequence=4,
        )
        store.update_state(message_id="om_1", state="closed", dispatch_status="idle")

        store.remember(
            message_id="om_1",
            card_id="card_replay",
            identifier="other@example.com",
            chat_id="oc_other",
            initiated_by_open_id="ou_other",
            snapshot_fingerprint="other-fp",
            state="ready",
            card_sequence=2,
        )

        context = store.lookup_context(message_id="om_1")
        self.assertEqual(context.state, "closed")
        self.assertEqual(context.identifier, "u@example.com")
        # 状态关闭本身也占用一个新的整卡版本，避免并发旧 scanner 清掉新的
        # needs_refresh；重复登记仍不能把实体/状态重新打开。
        self.assertEqual(context.card_sequence, 5)


class _Audit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def record(self, action: str, /, **fields) -> None:
        self.records.append((action, fields))


class _Route:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def route(self, **kwargs) -> AdminRouteOutcome:
        self.calls.append(kwargs)
        return AdminRouteOutcome(
            handled=True,
            content_key="admin.write_action_pending",
            content_version="internal",
            reply_text="已提交，请在下方确认卡片上确认（10 分钟内有效）。",
        )


class _Refresh:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def update(self, **kwargs) -> None:
        self.calls.append(kwargs)


class ManagementCardCallbackSecurityTests(unittest.TestCase):
    def _handler(self, *, status: AdminUserStatusView, route: _Route, store, refresh, audit):
        from lingxi.core.admin.card_callback import AdminCardCallbackHandler

        return AdminCardCallbackHandler(
            pending_actions=object(),
            confirm_cards=object(),
            group_notifier=None,
            group_chat_id=None,
            audit=audit,
            display_names=object(),
            management_actions=route,
            management_context_store=store,
            management_state_lookup=lambda _identifier: status,
            management_card_refresher=refresh,
        )

    def _store(self, status: AdminUserStatusView) -> ManagementCardContextStore:
        store = ManagementCardContextStore()
        store.remember(
            message_id="om_1",
            card_id="card_1",
            identifier="u@example.com",
            chat_id="oc_1",
            initiated_by_open_id="ou_admin",
            snapshot_fingerprint=management_card_fingerprint(status),
            context_deadline_at=datetime.now(UTC) + timedelta(hours=1),
        )
        return store

    def _submit(
        self,
        handler,
        *,
        operator="ou_admin",
        identifier="u@example.com",
        position_name="A运营",
        company_scope="c1",
        reason="特批",
    ):
        return handler.handle_management_form_submit(
            operator_open_id=operator,
            admin_action=ADMIN_ACTION_GRANT,
            identifier=identifier,
            company_id="",
            metric_name="",
            reason=reason,
            position_name=position_name,
            company_scope=company_scope,
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
            trace_id="trc_1",
        )

    def test_card_submit_links_pending_action_to_original_message_and_marks_submitted(self) -> None:
        status = _status()
        route = _Route()
        store = self._store(status)
        refresh = _Refresh()
        handler = self._handler(status=status, route=route, store=store, refresh=refresh, audit=_Audit())
        response = self._submit(handler)

        self.assertEqual(response["toast"]["type"], "success")
        self.assertEqual(route.calls[0]["origin_card_message_id"], "om_1")
        self.assertEqual(store.lookup_context(message_id="om_1").state, "submitted")
        self.assertEqual(refresh.calls[-1]["state"], "submitted")

    def test_non_initiator_or_tampered_identifier_cannot_route_write(self) -> None:
        status = _status()
        for operator, identifier in (("ou_other", "u@example.com"), ("ou_admin", "other@example.com")):
            route = _Route()
            store = self._store(status)
            audit = _Audit()
            handler = self._handler(status=status, route=route, store=store, refresh=_Refresh(), audit=audit)
            response = self._submit(handler, operator=operator, identifier=identifier)
            self.assertEqual(response["toast"]["type"], "error")
            self.assertEqual(route.calls, [])

    def test_missing_context_cannot_route_write_from_hidden_identifier(self) -> None:
        status = _status()
        route = _Route()
        handler = self._handler(
            status=status,
            route=route,
            store=ManagementCardContextStore(),
            refresh=_Refresh(),
            audit=_Audit(),
        )

        response = self._submit(handler)

        self.assertEqual(response["toast"]["type"], "error")
        self.assertIn("数据已变化", response["toast"]["content"])
        self.assertEqual(route.calls, [])

    def test_closed_context_replay_cannot_create_another_pending_action(self) -> None:
        status = _status()
        route = _Route()
        store = self._store(status)
        store.update_state(message_id="om_1", state="closed", dispatch_status="idle")
        handler = self._handler(
            status=status,
            route=route,
            store=store,
            refresh=_Refresh(),
            audit=_Audit(),
        )

        response = self._submit(handler)

        self.assertEqual(response["toast"]["type"], "error")
        self.assertEqual(route.calls, [])

    def test_revoke_button_cannot_replace_row_id_with_another_override(self) -> None:
        status = AdminUserStatusView(
            identifier="u@example.com",
            provisioning_state="active",
            account_state="enabled",
            permission_version=1,
            updated_at="2026-08-31T12:00:00+00:00",
            local_overrides=(
                LocalPermissionOverrideView(
                    override_id="lpo_real",
                    direction="grant",
                    company_id="c1",
                    metric_name="m1",
                    reason="r",
                    created_at="2026-08-31T12:00:00+00:00",
                ),
            ),
        )
        route = _Route()
        store = self._store(status)
        handler = self._handler(
            status=status,
            route=route,
            store=store,
            refresh=_Refresh(),
            audit=_Audit(),
        )

        response = handler.handle_management_revoke(
            operator_open_id="ou_admin",
            override_id="lpo_other",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
            trace_id="trc_revoke_mismatch",
        )

        self.assertEqual(response["toast"]["type"], "error")
        self.assertIn("覆盖行", response["toast"]["content"])
        self.assertEqual(route.calls, [])

    def test_changed_snapshot_is_rejected_and_original_card_is_closed(self) -> None:
        original = _status()
        changed = AdminUserStatusView(
            identifier=original.identifier,
            provisioning_state=original.provisioning_state,
            account_state="suspended",
            permission_version=original.permission_version,
            updated_at=original.updated_at,
        )
        route = _Route()
        store = self._store(original)
        refresh = _Refresh()
        handler = self._handler(status=changed, route=route, store=store, refresh=refresh, audit=_Audit())
        response = self._submit(handler)

        self.assertEqual(response["toast"]["type"], "error")
        self.assertIn("数据已变化", response["toast"]["content"])
        self.assertEqual(route.calls, [])
        self.assertEqual(refresh.calls[-1]["state"], "closed")

    def test_confirm_card_cancel_restores_the_management_form(self) -> None:
        """确认卡的取消与管理卡自己的取消是两种状态：前者完成后原卡可继续
        发起新操作，后者才关闭查询卡。"""

        status = _status()
        route = _Route()
        store = self._store(status)
        refresh = _Refresh()
        handler = self._handler(
            status=status, route=route, store=store, refresh=refresh, audit=_Audit()
        )
        pending = PendingAction(
            id="pac_cancelled",
            action_type=PendingActionType.LOCAL_PERMISSION_GRANT,
            target_open_id="ou_target",
            target_state_snapshot="absent",
            initiated_by_open_id="ou_admin",
            status=PendingActionStatus.CANCELLED,
            card_delivered=True,
            card_id="card_confirm",
            reason="cancelled_by_admin",
            created_at=NOW,
            confirm_deadline_at=NOW + timedelta(minutes=10),
            decided_at=NOW,
            decided_by_open_id="ou_admin",
            payload='{"position_name":"A运营","company_scope":"c1","pairs":[["c1","m1"]]}',
            origin_card_message_id="om_1",
        )

        handler._refresh_origin_management_card(pending=pending, trace_id="trc_cancel")

        self.assertEqual(store.lookup_context(message_id="om_1").state, "ready")
        self.assertEqual(refresh.calls[-1]["state"], "ready")
        self.assertIsNone(refresh.calls[-1]["dispatch_status"])

    # ------------------------------------------------------------------
    # #493 P1-2（rc24 缺陷账本 #520 F6）：职位表单两个必填项的服务端否定用例。
    # 此前 `handle_management_form_submit` 的「缺职位 / 缺范围」两条拒绝分支
    # 零测试覆盖——删掉它们全套用例仍全绿。它与 Trace #469 已经付过学费的
    # 「参数静默左移」是同一族路径（见下面的 `PositionCommandShiftReproductionTests`
    # 逐字复现），下游 `_parse_position_permission_command` 的语法门只在取值
    # 恰好不合语法时才兜得住，不能当成这两条校验的替代品。
    # ------------------------------------------------------------------

    def test_missing_position_name_is_rejected_before_routing(self) -> None:
        """缺职位：服务端必须在拼命令文本之前拒绝，并给出「请选择银河职位」。

        变异锚点：删掉 ``card_callback.py`` 里 ``if not position_name.strip():
        return _toast_error("请选择银河职位")`` 两行后，本用例由绿转红。
        """

        status = _status()
        route = _Route()
        store = self._store(status)
        handler = self._handler(
            status=status, route=route, store=store, refresh=_Refresh(), audit=_Audit()
        )

        response = self._submit(handler, position_name="", company_scope="c1")

        self.assertEqual(response["toast"]["type"], "error")
        self.assertEqual(response["toast"]["content"], "请选择银河职位")
        self.assertEqual(route.calls, [], "缺职位必须在拼命令文本、调用 route() 之前拦住")

    def test_missing_company_scope_is_rejected_before_routing(self) -> None:
        """缺范围：服务端必须在拼命令文本之前拒绝，并给出「请选择公司范围」。

        变异锚点：删掉 ``card_callback.py`` 里 ``if not company_scope.strip():
        return _toast_error("请选择公司范围")`` 两行后，本用例由绿转红。
        """

        status = _status()
        route = _Route()
        store = self._store(status)
        handler = self._handler(
            status=status, route=route, store=store, refresh=_Refresh(), audit=_Audit()
        )

        response = self._submit(handler, position_name="A运营", company_scope="")

        self.assertEqual(response["toast"]["type"], "error")
        self.assertEqual(response["toast"]["content"], "请选择公司范围")
        self.assertEqual(route.calls, [], "缺范围必须在拼命令文本、调用 route() 之前拦住")

    def test_whitespace_only_position_or_scope_is_treated_as_missing(self) -> None:
        """只填空白等于没填：``.strip()`` 之后为空的取值必须落回同一条「请选择」
        文案，而不是滑到下游那条更笼统的「职位或公司范围无效」——两者都拒绝，
        但只有前者告诉管理员到底缺哪一项。全角空格（U+3000）同样算空白。"""

        status = _status()
        for position_name, company_scope, expected in (
            ("   ", "c1", "请选择银河职位"),
            ("\u3000", "c1", "请选择银河职位"),
            ("A运营", "   ", "请选择公司范围"),
            ("A运营", "\u3000", "请选择公司范围"),
        ):
            with self.subTest(position_name=position_name, company_scope=company_scope):
                route = _Route()
                store = self._store(status)
                handler = self._handler(
                    status=status, route=route, store=store, refresh=_Refresh(), audit=_Audit()
                )

                response = self._submit(
                    handler, position_name=position_name, company_scope=company_scope
                )

                self.assertEqual(response["toast"]["type"], "error")
                self.assertEqual(response["toast"]["content"], expected)
                self.assertEqual(route.calls, [])

    def test_both_fields_present_still_routes(self) -> None:
        """否定用例的另一半：两项都选齐时新增断言不得误伤主路径。"""

        status = _status()
        route = _Route()
        store = self._store(status)
        handler = self._handler(
            status=status, route=route, store=store, refresh=_Refresh(), audit=_Audit()
        )

        response = self._submit(handler, position_name="A运营", company_scope="c1")

        self.assertEqual(response["toast"]["type"], "success")
        self.assertEqual(len(route.calls), 1)
        self.assertEqual(
            route.calls[0]["text"], "/admin grant_position u@example.com A运营 c1 特批"
        )


class PositionCommandShiftReproductionTests(unittest.TestCase):
    """复现「缺职位 / 缺范围」被放过去之后会发生什么（#493 P1-2 / #520 F6）。

    这里不经过 handler 的校验，直接把「少了一段」的命令文本喂给
    ``parse_admin_command``：连续空白被 ``str.split()`` 吃成一个分隔符，后面
    每个字段整体左移一位，解析结果仍然是一条形状完全合法的
    ``GRANT_POSITION_PERMISSION``——只是职位或公司范围已经换成了管理员从没
    选过的取值，而管理员看到的却是「已提交，请确认」这类正常回执。

    下游语法门只在左移后的取值恰好不合语法时才拒绝，因此它是运气、不是护栏；
    这组用例把这一点钉住，防止有人以为「反正下游会拒绝」就够安全（Trace #469
    已经为同一族根因付过一次学费）。
    """

    def test_a_missing_position_name_shifts_the_company_scope_into_the_position(self) -> None:
        identifier = "u@example.com"
        position_name = ""
        company_scope = "c1"
        reason = "c2 补充授权"
        shifted = f"/admin grant_position {identifier} {position_name} {company_scope} {reason}"

        parsed = parse_admin_command(shifted)

        self.assertEqual(parsed.kind, AdminCommandKind.GRANT_POSITION_PERMISSION, "左移后仍然形状合法")
        self.assertEqual(parsed.position_name, "c1", "管理员选的公司范围被当成了职位")
        self.assertEqual(parsed.company_scope, "c2", "公司范围换成了原因里的第一个词")
        self.assertEqual(parsed.reason, "补充授权")

    def test_a_missing_company_scope_shifts_the_reason_into_the_scope(self) -> None:
        identifier = "u@example.com"
        position_name = "A运营"
        company_scope = ""
        reason = "c2 补充授权"
        shifted = f"/admin grant_position {identifier} {position_name} {company_scope} {reason}"

        parsed = parse_admin_command(shifted)

        self.assertEqual(parsed.kind, AdminCommandKind.GRANT_POSITION_PERMISSION, "左移后仍然形状合法")
        self.assertEqual(parsed.position_name, "A运营")
        self.assertEqual(
            parsed.company_scope, "c2", "授权范围变成了管理员从没选过的公司"
        )
        self.assertEqual(parsed.reason, "补充授权")


class _RefresherTransport:
    def __init__(self) -> None:
        self.updated: list[dict] = []

    def update(self, **kwargs):
        self.updated.append(kwargs)


class _RefresherContextStore:
    def next_card_sequence(self, *, message_id: str) -> int:
        self.message_id = message_id
        return 3


def _refresher_context():
    return type(
        "Context",
        (),
        {
            "message_id": "om_f5",
            "card_id": "card_f5",
            "identifier": "u@example.com",
            "last_trace_id": "trc_f5",
        },
    )()


def _rendered_status(transport: _RefresherTransport) -> str:
    elements = list(_walk(transport.updated[-1]["card"]["body"]["elements"]))
    return "\n".join(
        element.get("content", "") for element in elements if element.get("tag") == "markdown"
    )


def _render_incomplete(*, account_state: str, dispatch_status: str | None) -> str:
    from lingxi.apps.gateway import _GatewayManagementCardRefresher

    transport = _RefresherTransport()
    refresher = _GatewayManagementCardRefresher(
        transport=transport,
        catalog=_PositionCatalog(),
        display_names=_DisplayNames(),
        context_store=_RefresherContextStore(),
    )
    refresher.update(
        context=_refresher_context(),
        status=AdminUserStatusView(
            identifier="ou_target",
            provisioning_state="active",
            account_state=account_state,
            permission_version=1,
            updated_at="2026-08-31T12:00:00+00:00",
        ),
        state="incomplete",
        dispatch_status=dispatch_status,
    )
    return _rendered_status(transport)


class SuspendedUserGetsTheTruthNotADailyBatchPromiseTests(unittest.TestCase):
    """#493 P1-3（Trace #521 F5）：给**已停用**用户补充授权后，管理卡不得再说
    「权限下发未完成，将在次日批处理修正」。

    那句话是假承诺，不是措辞问题：发权每日批遍历的基线是
    ``provisioning_state='active' AND account_state='enabled'``
    （``adapters/postgres_permission_publish.PERMISSION_REFRESH_BASELINE_SQL``），
    停用用户根本不进遍历集合——``tests/test_permission_refresh_postgres.py`` 的
    ``test_a_later_round_no_longer_touches_the_suspended_user_and_the_state_is_correct``
    已经用真库把这条事实钉死了。文案与既有测试互相矛盾，改的是文案。

    本类只钉"管理员看到什么"，不改产品语义：override 照常落库（``prepare`` 不读
    账号状态），本次只是如实告知不下发。
    """

    #: 这三个片段构成"真话"的三要素，缺一条这条修复就没有完成。
    TRUTH_FRAGMENTS = ("已停用", "不会下发", "恢复账号后由次日批处理生效")
    #: 这些片段一旦出现，就等于又向管理员承诺了"当前会自动修正"。
    FALSE_PROMISES = ("将在次日批处理修正", "最迟次日自动纠正")

    def _assert_is_the_truth(self, visible: str) -> None:
        for fragment in self.TRUTH_FRAGMENTS:
            self.assertIn(fragment, visible, f"真话缺了「{fragment}」这一要素")
        for promise in self.FALSE_PROMISES:
            self.assertNotIn(promise, visible, f"仍然向管理员承诺了「{promise}」")

    def test_the_account_not_enabled_skip_renders_the_truth(self) -> None:
        """SKIPPED/account_not_enabled 走新文案。"""

        from lingxi.apps.gateway.management_status import (
            skipped_recompute_status_message,
        )

        message = skipped_recompute_status_message(
            TargetedRecomputeOutcome(
                kind=RecomputeKind.SKIPPED, reason=SKIP_ACCOUNT_NOT_ENABLED
            )
        )
        self.assertIsNotNone(message)
        assert message is not None
        self._assert_is_the_truth(message)
        # 文案必须来自版本化内容目录，不是散落在装配代码里的字面量。
        self.assertEqual(
            message,
            default_content_catalog().text("permission.management_account_not_enabled").text,
        )

    def test_that_truth_actually_reaches_the_rendered_card(self) -> None:
        """真话必须真的出现在管理员看到的那张卡上，不只是回调的返回值。"""

        from lingxi.apps.gateway.management_status import (
            skipped_recompute_status_message,
        )

        message = skipped_recompute_status_message(
            TargetedRecomputeOutcome(
                kind=RecomputeKind.SKIPPED, reason=SKIP_ACCOUNT_NOT_ENABLED
            )
        )
        self._assert_is_the_truth(
            _render_incomplete(account_state="suspended", dispatch_status=message)
        )

    def test_a_real_failure_still_gets_the_original_wording(self) -> None:
        """反向对照一：普通失败（不是跳过）仍走原文案。

        没有这一条，"把所有未完成都改口成已停用"这种停服级误伤仍然是绿的。
        """

        visible = _render_incomplete(
            account_state="enabled",
            dispatch_status="下发未完成，最迟次日自动纠正 · 追溯号 trc_f5",
        )
        self.assertIn("最迟次日自动纠正", visible)
        for fragment in self.TRUTH_FRAGMENTS:
            self.assertNotIn(fragment, visible)

    def test_the_generic_incomplete_fallback_is_unchanged_for_an_enabled_user(self) -> None:
        """反向对照二：账号正常的用户，兜底渲染仍逐字是原来那句。"""

        visible = _render_incomplete(account_state="enabled", dispatch_status="incomplete")
        self.assertIn("权限下发未完成，将在次日批处理修正", visible)

    def test_other_skip_reasons_keep_the_original_wording(self) -> None:
        """反向对照三：其余跳过原因确实可能被日批纠正，行为逐字节不变。"""

        from lingxi.apps.gateway.management_status import (
            skipped_recompute_status_message,
        )

        for reason in ("missing_roster_snapshot", "match_failed", None):
            with self.subTest(reason=reason):
                self.assertIsNone(
                    skipped_recompute_status_message(
                        TargetedRecomputeOutcome(kind=RecomputeKind.SKIPPED, reason=reason)
                    )
                )

    def test_the_recovery_scanner_repaint_cannot_resurrect_the_false_promise(self) -> None:
        """卡片恢复 scanner 重画时也不得回到假承诺。

        持久化的 ``dispatch_status`` 只有四个机器态（迁移 ``0081`` 的 CHECK），装不下
        人类文案；CardKit 那次更新失败、由 scanner 按水位重画时，只剩通用兜底可用。
        判据因此落在**本次刚读回的账号状态**上，而不是那条读不回来的文案。
        """

        self._assert_is_the_truth(
            _render_incomplete(account_state="suspended", dispatch_status="incomplete")
        )

    def test_a_restored_account_goes_back_to_the_generic_wording(self) -> None:
        """账号恢复 ``enabled`` 之后，同一张卡再刷新自动回到通用文案——判据读的是
        当下的账号状态，不是任何缓存下来的结论。"""

        visible = _render_incomplete(account_state="enabled", dispatch_status="incomplete")
        for fragment in self.TRUTH_FRAGMENTS:
            self.assertNotIn(fragment, visible)


if __name__ == "__main__":
    unittest.main()


class SuspendedUserTransientTextTests(unittest.TestCase):
    """#493 块 B 第二条（Trace #544）：**对已停用目标操作时的瞬时文案**。

    终态早已被 rc24 F5 纠正成那句真话，可是**瞬时**这一行还在说「操作已记录，权限
    正在下发」——对一个已停用的目标，下发根本不会发生（发布层在 ``app_user`` 行锁里
    就挡住了非 ``enabled`` 账号的非空授权）。管理员先看到一句不成立的承诺、隔一会儿
    才被终态纠正，是展示面失真。这里让瞬时与终态说同一句话。
    """

    TRUTH_FRAGMENTS = ("已停用", "不会下发")
    PUBLISHING_PROMISE = "权限正在下发"

    def _rendered(self, *, account_state: str, **overrides) -> str | None:
        from lingxi.apps.gateway.management_status import rendered_dispatch_status

        status = AdminUserStatusView(
            identifier="ou_target",
            provisioning_state="active",
            account_state=account_state,
            permission_version=1,
            updated_at="2026-09-02T12:00:00+00:00",
        )
        fields = {"state": "dispatching", "dispatch_status": None, "status_message": None}
        fields.update(overrides)
        return rendered_dispatch_status(status=status, **fields)

    def test_suspended_target_never_sees_the_publishing_promise(self) -> None:
        for name, fields in (
            ("即时路径已算好的文案", {"status_message": "操作已记录，权限正在下发"}),
            ("状态机 dispatching", {"state": "dispatching"}),
            ("状态机 submitted", {"state": "submitted"}),
            ("恢复 scanner 重画", {"state": "unknown", "dispatch_status": "publishing"}),
        ):
            with self.subTest(name=name):
                visible = self._rendered(account_state="suspended", **fields)
                assert visible is not None
                self.assertNotIn(self.PUBLISHING_PROMISE, visible)
                for fragment in self.TRUTH_FRAGMENTS:
                    self.assertIn(fragment, visible)

    def test_enabled_target_still_sees_the_publishing_line(self) -> None:
        """反向对照一：账号正常的用户，瞬时这一行逐字不变。"""

        visible = self._rendered(account_state="enabled")
        self.assertEqual(visible, "操作已记录，权限正在下发")

    def test_other_states_of_a_suspended_target_are_untouched(self) -> None:
        """反向对照二：只改写「正在下发」这一句——「已生效」「已取消」各有自己的判据，
        不在这里顺手一起改写。"""

        self.assertEqual(
            self._rendered(account_state="suspended", state="effective"), "已生效"
        )
        self.assertEqual(self._rendered(account_state="suspended", state="closed"), "已取消")

    def test_a_status_view_without_account_state_keeps_the_old_wording(self) -> None:
        """反向对照三：读不到账号状态时按"没有额外信息"处理，行为逐字不变
        （与 ``is_account_not_enabled`` 同一姿态，保护旧测试替身）。"""

        from lingxi.apps.gateway.management_status import rendered_dispatch_status

        class _StatusWithoutAccountState:
            identifier = "ou_target"

        self.assertEqual(
            rendered_dispatch_status(
                status=_StatusWithoutAccountState(),
                state="dispatching",
                dispatch_status=None,
                status_message=None,
            ),
            "操作已记录，权限正在下发",
        )

    def test_the_publishing_literal_has_exactly_one_home(self) -> None:
        """撤除重复字面量（#493 块 B）：``apps/gateway/__init__.py`` 此前另抄了两份，
        改一处漏两处。"""

        from pathlib import Path

        import lingxi.apps.gateway as gateway_package
        from lingxi.apps.gateway.management_status import PUBLISHING_STATUS_TEXT

        source = (Path(gateway_package.__file__)).read_text(encoding="utf-8")

        self.assertEqual(PUBLISHING_STATUS_TEXT, "操作已记录，权限正在下发")
        self.assertNotIn(f'"{PUBLISHING_STATUS_TEXT}"', source)
