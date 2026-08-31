"""#493 职位 + 公司范围管理卡的纯逻辑与安全护栏。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from lingxi.core.admin.card_dispatch import ManagementCardContextStore, management_card_fingerprint
from lingxi.core.admin.commands import AdminCommandKind, parse_admin_command
from lingxi.core.admin.management_card import (
    ADMIN_ACTION_CANCEL,
    ADMIN_ACTION_GRANT,
    render_management_card,
)
from lingxi.core.admin.router import AdminRouteOutcome
from lingxi.core.admin.pending_action import PendingAction, PendingActionStatus, PendingActionType
from lingxi.core.admin.views import AdminUserStatusView, LocalPermissionOverrideView
from lingxi.core.permission.position_override import expand_position_scope


NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


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
            context_deadline_at=datetime.now(timezone.utc) + timedelta(hours=1),
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
        self.assertEqual(context.card_sequence, 4)


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
            context_deadline_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        return store

    def _submit(self, handler, *, operator="ou_admin", identifier="u@example.com"):
        return handler.handle_management_form_submit(
            operator_open_id=operator,
            admin_action=ADMIN_ACTION_GRANT,
            identifier=identifier,
            company_id="",
            metric_name="",
            reason="特批",
            position_name="A运营",
            company_scope="c1",
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


if __name__ == "__main__":
    unittest.main()
