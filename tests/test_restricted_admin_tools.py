"""只读三工具与准备五工具的纯逻辑与通道分发（假查询口 / 假路由）；真库断言见 test_restricted_admin_postgres 与 test_restricted_admin_write_postgres。"""

import json
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from lingxi.adapters.restricted_admin_prepare import RestrictedAdminPrepare
from lingxi.adapters.restricted_admin_queries import (
    RestrictedAdminQueries,
    RestrictedChannelService,
)
from lingxi.core.admin.commands import AdminCommandKind, parse_admin_command
from lingxi.core.admin.followup import FollowupRef
from lingxi.core.admin.innertest import InnertestError, envelope
from lingxi.core.admin.pending_action import PendingAction, PendingActionStatus, PendingActionType
from lingxi.core.admin.restricted_tools import (
    candidate_lists,
    operation_target,
    pending_action_item,
    pending_actions_result,
    permission_sources_result,
    prepare_code,
    prepare_command_text,
    request_intent,
    row_intents,
    user_status_result,
)
from lingxi.core.admin.router_ports import AdminRouteOutcome
from lingxi.core.admin.views import (
    AdminEventView,
    AdminTraceView,
    AdminUserStatusView,
    GalaxySourceSummary,
    LocalPermissionOverrideView,
)

METRIC_MAP = {
    "1011": {"财务": ("vat_rate", "exchange_rate"), "后台管理员": ("vat_rate",)},
    "*": {"后台管理员": ("vat_rate", "exchange_rate")},
}
PRINCIPAL = SimpleNamespace(binding_id="binding", open_id="ou_admin", version=1)


def override(**kwargs):
    base = dict(
        override_id="lpo_1",
        direction="grant",
        company_id="1011",
        metric_name="vat_rate",
        reason="特批",
        created_at="2026-09-14T00:00:00+00:00",
    )
    return LocalPermissionOverrideView(**{**base, **kwargs})


def status(*, galaxy=None, overrides=()):
    return AdminUserStatusView(
        identifier="ou_target",
        provisioning_state="active",
        account_state="enabled",
        permission_version=3,
        updated_at="2026-09-14T00:00:00+00:00",
        local_overrides=tuple(overrides),
        galaxy_source=galaxy,
    )


class ResultShapeTests(unittest.TestCase):
    def sources(self, *, galaxy, overrides=(), metric_map=METRIC_MAP):
        return permission_sources_result(
            trace_id="trc_1",
            identifier="target@example.test",
            open_id="ou_target",
            status=status(galaxy=galaxy, overrides=overrides),
            metric_map=metric_map,
            candidates=candidate_lists(metric_map=metric_map, positions=("A财务",)),
        )

    def test_not_found_is_explicit_and_never_guesses(self):
        with self.assertRaisesRegex(InnertestError, "not_found"):
            user_status_result(trace_id="t", identifier="x", open_id="x", status=None, events=())
        with self.assertRaisesRegex(InnertestError, "not_found"):
            pending_actions_result(trace_id="t", items=[], single=True)
        self.assertEqual(pending_actions_result(trace_id="t", items=[], single=False)["count"], 0)

    def test_user_status_is_json_serializable_with_events(self):
        result = user_status_result(
            trace_id="trc_1",
            identifier="target@example.test",
            open_id="ou_target",
            status=status(
                galaxy=GalaxySourceSummary(granted=False, reason="no_galaxy_roles"),
                overrides=(override(),),
            ),
            events=(
                AdminEventView(
                    received_at="2026-09-14T00:00:00+00:00",
                    event_type="im.message.receive_v1",
                    handled_as="task_queued",
                    trace_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
                ),
            ),
        )
        encoded = json.loads(json.dumps(result))
        self.assertTrue(encoded["ok"])
        self.assertEqual(encoded["query"]["resolved_open_id"], "ou_target")
        self.assertEqual(encoded["user"]["local_overrides"][0]["override_id"], "lpo_1")
        self.assertEqual(encoded["user"]["galaxy_source"]["reason"], "no_galaxy_roles")
        self.assertEqual(encoded["recent_events"][0]["handled_as"], "task_queued")

    def test_galaxy_expanded_and_merged_with_local_overrides(self):
        galaxy = GalaxySourceSummary(
            granted=True, reason="granted", companies=("1011",), functions=("财务",)
        )
        result = self.sources(
            galaxy=galaxy,
            overrides=(
                override(
                    override_id="lpo_g", company_id="1012", metric_name="vat_rate", group_id="g1"
                ),
                override(override_id="lpo_s", direction="suppress", metric_name="vat_rate"),
            ),
        )
        self.assertEqual(result["galaxy"]["translation"], "translated")
        self.assertEqual(
            result["galaxy"]["company_metrics"], {"1011": ["exchange_rate", "vat_rate"]}
        )
        self.assertEqual(
            result["merged"]["permissions"], {"1011": ["exchange_rate"], "1012": ["vat_rate"]}
        )
        self.assertIsNone(result["merged_reason"])
        groups = {group["group_id"]: group for group in result["local"]["groups"]}
        self.assertEqual(groups["g1"]["entries"][0]["override_id"], "lpo_g")
        self.assertEqual(groups[None]["direction"], "suppress")
        self.assertEqual(result["candidates"]["companies"], ["1011"])
        self.assertEqual(result["candidates"]["positions"], ["A财务"])

    def test_galaxy_unavailable_never_becomes_zero_permission(self):
        for reason in (
            "roster_snapshot_unavailable",
            "galaxy_snapshot_unavailable",
            "role_function_map_unavailable",
        ):
            result = self.sources(
                galaxy=GalaxySourceSummary(granted=False, reason=reason), overrides=(override(),)
            )
            self.assertFalse(result["galaxy"]["available"])
            self.assertIsNone(result["merged"])
            self.assertEqual(result["merged_reason"], reason)
        result = self.sources(galaxy=None, overrides=(override(),))
        self.assertEqual(result["merged_reason"], "galaxy_unavailable")

    def test_zero_galaxy_merges_local_only_and_uncovered_is_reported(self):
        result = self.sources(
            galaxy=GalaxySourceSummary(granted=False, reason="no_galaxy_roles"),
            overrides=(override(),),
        )
        self.assertTrue(result["galaxy"]["available"])
        self.assertEqual(result["merged"]["permissions"], {"1011": ["vat_rate"]})
        uncovered = self.sources(
            galaxy=GalaxySourceSummary(
                granted=True, reason="granted", companies=("9999",), functions=("财务",)
            )
        )
        self.assertEqual(uncovered["galaxy"]["translation"], "uncovered")
        self.assertEqual(uncovered["merged_reason"], "uncovered")
        missing_map = self.sources(
            galaxy=GalaxySourceSummary(
                granted=True, reason="granted", companies=("1011",), functions=("财务",)
            ),
            metric_map=None,
        )
        self.assertEqual(missing_map["merged_reason"], "metric_map_unavailable")
        self.assertEqual(missing_map["candidates"]["reason"], "catalog_unavailable")

    def test_full_access_wildcard_skips_local_like_the_publish_chain(self):
        result = self.sources(
            galaxy=GalaxySourceSummary(
                granted=True,
                reason="granted",
                companies=(),
                functions=("后台管理员",),
                all_companies=True,
            ),
            overrides=(override(),),
        )
        self.assertEqual(result["merged"]["permissions"], {"*": ["exchange_rate", "vat_rate"]})
        self.assertEqual(result["merged"]["skipped_reasons"], ["grant_redundant_wildcard"])

    def test_pending_action_item_reads_overlap_fields_and_never_calls_confirmed_effective(self):
        now = datetime.now(UTC)
        row = dict(
            id="pac_1",
            action_type="local_permission_grant",
            target_open_id="ou_target",
            initiated_by_open_id="ou_admin",
            status="pending",
            card_delivered=False,
            reason=None,
            created_at=now,
            confirm_deadline_at=now,
            decided_at=None,
            decided_by_open_id=None,
            payload='{"pairs": [["1011", "vat_rate"]], "reason": "特批", "reused_count": 2}',
        )
        item = pending_action_item(
            row, (FollowupRef(id="f1", stage="confirmation_card_send", status="unknown"),)
        )
        self.assertEqual(item["payload"]["pairs"], [["1011", "vat_rate"]])
        self.assertEqual(item["followups"][0]["status"], "unknown")
        self.assertEqual(item["created_at"], now.isoformat())
        self.assertEqual((item["new_count"], item["reused_count"]), (1, 2))
        self.assertIsNone(item["retained_by_galaxy"])
        self.assertIsNone(item["publish_state"])
        self.assertFalse(item["card_delivered"])
        broken = pending_action_item({**row, "payload": "{broken"}, ())
        self.assertIsNone(broken["payload"])
        self.assertEqual((broken["new_count"], broken["reused_count"]), (0, 0))
        revoke = pending_action_item(
            {
                **row,
                "action_type": "local_permission_revoke",
                "status": "executed",
                "payload": '{"override_id": "lpo_1", "company_id": "1011", "metric_name": "vat_rate", "reason": "撤", "galaxy_retained": 1}',
            },
            (
                FollowupRef(id="f2", stage="permission_recompute", status="succeeded"),
                FollowupRef(id="f3", stage="publish_observe", status="retry_wait"),
            ),
        )
        self.assertEqual(revoke["retained_by_galaxy"], 1)
        self.assertIsNone(revoke["new_count"])
        self.assertEqual((revoke["status"], revoke["publish_state"]), ("executed", "in_progress"))
        published = pending_action_item(
            {**row, "status": "executed"},
            (FollowupRef(id="f3", stage="publish_observe", status="succeeded"),),
        )
        self.assertEqual(published["publish_state"], "published")
        suspend = pending_action_item({**row, "action_type": "suspend_user", "payload": None}, ())
        self.assertEqual((suspend["new_count"], suspend["reused_count"]), (None, None))


class FakeQueries:
    def __init__(self):
        self.status = status(galaxy=GalaxySourceSummary(granted=False, reason="no_galaxy_roles"))
        self.trace = None
        self.calls = []

    def resolve_identifier(self, *, identifier):
        self.calls.append(("resolve", identifier))
        known = {"t@example.test", "target@example.test"}
        return "ou_target" if identifier in known else identifier

    def resolve_metric_name(self, *, metric_token):
        return {"增值税率": "vat_rate"}.get(metric_token, metric_token)

    def user_status(self, *, identifier):
        self.calls.append(("status", identifier))
        return self.status if identifier == "ou_target" else None

    def recent_events(self, *, identifier, window_hours, limit):
        self.calls.append(("events", identifier, window_hours, limit))
        return ()

    def trace_lookup(self, *, trace_id):
        self.calls.append(("trace", trace_id))
        return self.trace


class FakeFollowups:
    def list_for_action(self, *, pending_action_id):
        return (
            FollowupRef(id="f_" + pending_action_id, stage="permission_recompute", status="done"),
        )


class Audit:
    def __init__(self):
        self.rows = []

    def record(self, action, /, **fields):
        self.rows.append((action, fields))


class Innertest:
    def __init__(self):
        self.calls = []

    def authenticate(self, uid):
        if uid != 1234:
            raise InnertestError("not_authenticated")
        return PRINCIPAL

    def call(self, principal, name, args):
        self.calls.append((name, args))
        return envelope(state="pending", batch_id="ibt_1")


class ChannelServiceTests(unittest.TestCase):
    def setUp(self):
        self.queries = FakeQueries()
        self.audit = Audit()
        self.innertest = Innertest()
        self.readonly = RestrictedAdminQueries(
            "postgresql://unused",
            queries=self.queries,
            followups=FakeFollowups(),
            metric_map_path=None,
        )
        self.service = RestrictedChannelService(
            innertest=self.innertest, queries=self.readonly, audit=self.audit
        )

    def test_authenticate_is_delegated_unchanged(self):
        self.assertIs(self.service.authenticate(1234), PRINCIPAL)
        with self.assertRaisesRegex(InnertestError, "not_authenticated"):
            self.service.authenticate(999)

    def test_innertest_tools_go_to_the_existing_service_without_read_only_audit(self):
        result = self.service.call(PRINCIPAL, "get_innertest_batch", {"batch_id": "ibt_1"})
        self.assertEqual(result["state"], "pending")
        self.assertEqual(self.innertest.calls, [("get_innertest_batch", {"batch_id": "ibt_1"})])
        self.assertEqual(self.audit.rows, [])

    def test_unknown_name_is_invalid_request(self):
        for name in (
            "confirm_pending_action",
            "cancel_pending_action",
            "execute",
            "prepare_suppress_metric",
        ):
            with self.assertRaisesRegex(InnertestError, "invalid_request"):
                self.service.call(PRINCIPAL, name, {})
        self.assertEqual(self.audit.rows, [])

    def test_prepare_tools_reject_when_no_prepare_service_is_wired(self):
        with self.assertRaisesRegex(InnertestError, "invalid_request"):
            self.service.call(PRINCIPAL, "prepare_suspend_user", {"identifier": "ou_target"})
        self.assertEqual(self.audit.rows, [])

    def test_prepare_tools_are_dispatched_by_name_and_audited_with_pending_id(self):
        class Prepare:
            calls = []

            def prepare_suspend_user(self, principal, *, call_trace_id, identifier):
                self.calls.append((principal, identifier))
                return envelope(trace_id=call_trace_id, state="pending", pending_action_id="pac_9")

            def prepare_grant_position(self, principal, *, call_trace_id, **kwargs):
                raise InnertestError("not_authorized")

        service = RestrictedChannelService(
            innertest=self.innertest, queries=self.readonly, audit=self.audit, prepare=Prepare()
        )
        result = service.call(PRINCIPAL, "prepare_suspend_user", {"identifier": "ou_target"})
        self.assertEqual(result["pending_action_id"], "pac_9")
        self.assertEqual(Prepare.calls, [(PRINCIPAL, "ou_target")])
        rejected = service.call(
            PRINCIPAL,
            "prepare_grant_position",
            {"identifier": "ou_t", "position_name": "A", "company_scope": "*", "reason": "r"},
        )
        self.assertEqual((rejected["ok"], rejected["code"]), (False, "not_authorized"))
        (first, second) = self.audit.rows
        self.assertEqual(first[0], "admin.restricted.prepare_suspend_user")
        self.assertEqual(first[1]["pending_action_id"], "pac_9")
        self.assertEqual(second[1]["result_code"], "not_authorized")
        self.assertIsNone(second[1]["pending_action_id"])
        self.assertEqual(self.innertest.calls, [])

    def test_read_only_call_audits_one_line_without_arguments(self):
        result = self.service.call(PRINCIPAL, "get_user_status", {"identifier": "t@example.test"})
        self.assertTrue(result["ok"])
        self.assertEqual(
            result["query"], {"identifier": "t@example.test", "resolved_open_id": "ou_target"}
        )
        self.assertEqual(self.queries.calls[0], ("resolve", "t@example.test"))
        self.assertEqual(self.queries.calls[2][:2], ("events", "ou_target"))
        ((action, fields),) = self.audit.rows
        self.assertEqual(action, "admin.restricted.get_user_status")
        self.assertEqual(fields["result_code"], "ok")
        self.assertEqual(fields["binding_id"], "binding")
        self.assertEqual(fields["trace_id"], result["trace_id"])
        self.assertIsInstance(fields["elapsed_ms"], int)
        self.assertNotIn("t@example.test", json.dumps(fields))
        self.assertNotIn("ou_target", json.dumps(fields))

    def test_not_found_is_returned_as_rejected_envelope_and_audited(self):
        result = self.service.call(PRINCIPAL, "get_user_status", {"identifier": "ou_nobody"})
        self.assertEqual(
            (result["ok"], result["code"], result["state"]), (False, "not_found", "rejected")
        )
        self.assertEqual(self.audit.rows[0][1]["result_code"], "not_found")
        self.assertNotIn(("events", "ou_nobody", 168, 20), self.queries.calls)

    def test_trace_lookup_path(self):
        self.queries.trace = AdminTraceView(
            trace_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            event_count=1,
            first_received_at="2026-09-14T00:00:00+00:00",
            last_event_type="im.message.receive_v1",
            last_handled_as="task_queued",
            dispatched=True,
            provisioning_state="active",
            account_state="enabled",
            failure_reason=None,
            failure_event_type=None,
            failure_occurred_at=None,
            followups=(
                FollowupRef(id="f1", stage="x", status="done", updated_at=datetime.now(UTC)),
            ),
        )
        result = self.service.call(
            PRINCIPAL, "get_user_status", {"trace_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV"}
        )
        json.dumps(result)
        self.assertEqual(result["trace"]["followups"][0]["id"], "f1")
        self.assertEqual(self.queries.calls, [("trace", "01ARZ3NDEKTSV4RRFFQ69G5FAV")])
        self.queries.trace = None
        result = self.service.call(
            PRINCIPAL, "get_user_status", {"trace_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV"}
        )
        self.assertEqual(result["code"], "not_found")

    def test_permission_sources_uses_packaged_maps_when_no_override_path(self):
        result = self.service.call(
            PRINCIPAL, "get_user_permission_sources", {"identifier": "ou_target"}
        )
        self.assertTrue(result["ok"])
        self.assertIn("1", result["candidates"]["companies"])
        self.assertNotIn("*", result["candidates"]["companies"])
        self.assertTrue(result["candidates"]["positions"])
        self.assertEqual(result["merged"]["permissions"], {})
        self.assertEqual(self.audit.rows[0][0], "admin.restricted.get_user_permission_sources")

    def test_unexpected_failure_is_audited_and_surfaces_as_query_unavailable(self):
        def boom(**kwargs):
            raise RuntimeError("connection reset")

        self.queries.user_status = boom
        with self.assertRaisesRegex(InnertestError, "query_unavailable"):
            self.service.call(PRINCIPAL, "get_user_status", {"identifier": "ou_target"})
        self.assertEqual(self.audit.rows[0][1]["result_code"], "error:RuntimeError")
        self.assertNotIn("connection reset", json.dumps(self.audit.rows[0][1]))


#: 每个准备工具 ↔ 私聊命令文本，逐字等价；管理员在私聊里敲这一行会得到同一结论。
TRANSLATIONS = (
    ("prepare_suspend_user", {"identifier": "ou_target"}, "/admin suspend ou_target"),
    ("prepare_resume_user", {"identifier": "t@example.test"}, "/admin resume t@example.test"),
    (
        "prepare_grant_position",
        {
            "identifier": "ou_target",
            "position_name": "A商务",
            "company_scope": "全部",
            "reason": "  特批\n一句 ",
        },
        "/admin grant_position ou_target A商务 全部 特批 一句",
    ),
    (
        "prepare_revoke_permission",
        {"override_id": "lpo_01ARZ3NDEKTSV4RRFFQ69G5FAV", "reason": "撤销"},
        "/admin revoke_permission lpo_01ARZ3NDEKTSV4RRFFQ69G5FAV 撤销",
    ),
    (
        "prepare_revoke_permission",
        {"group_id": "lpg_01ARZ3NDEKTSV4RRFFQ69G5FAV", "reason": "撤销 整组"},
        "/admin revoke_permission lpg_01ARZ3NDEKTSV4RRFFQ69G5FAV 撤销 整组",
    ),
    (
        "prepare_revoke_permission_by_scope",
        {
            "identifier": "ou_target",
            "company_id": "1011",
            "metric_name": "增值税率",
            "reason": "撤销",
        },
        "/admin revoke_permission ou_target 1011 增值税率 撤销",
    ),
)


class CommandTranslationTests(unittest.TestCase):
    def test_each_prepare_tool_translates_to_the_exact_private_chat_command(self):
        kinds = {
            "prepare_suspend_user": AdminCommandKind.SUSPEND_USER,
            "prepare_resume_user": AdminCommandKind.RESUME_USER,
            "prepare_grant_position": AdminCommandKind.GRANT_POSITION_PERMISSION,
            "prepare_revoke_permission": AdminCommandKind.REVOKE_PERMISSION,
            "prepare_revoke_permission_by_scope": AdminCommandKind.REVOKE_PERMISSION,
        }
        for name, args, text in TRANSLATIONS:
            self.assertEqual(prepare_command_text(name, args), text)
            self.assertEqual(
                parse_admin_command(text), parse_admin_command(prepare_command_text(name, args))
            )
            self.assertIs(parse_admin_command(text).kind, kinds[name])
        self.assertEqual(
            parse_admin_command(TRANSLATIONS[2][2]),
            parse_admin_command("/admin grant_position ou_target A商务 * 特批 一句"),
        )
        with self.assertRaisesRegex(InnertestError, "invalid_request"):
            prepare_command_text("prepare_suppress_metric", {"identifier": "ou_target"})
        with self.assertRaisesRegex(InnertestError, "invalid_request"):
            prepare_command_text("prepare_suspend_user", {"identifier": "ou target"})

    def test_prepare_code_maps_router_outcomes(self):
        self.assertEqual(prepare_code(AdminRouteOutcome(handled=False)), "not_authorized")
        pending = AdminRouteOutcome(
            handled=True, content_key="admin.write_action_pending", pending_action_id="pac_1"
        )
        self.assertEqual(prepare_code(pending), "ok")
        rejected = AdminRouteOutcome(
            handled=True, content_key="admin.write_action_rejected", decision_code="not_found"
        )
        self.assertEqual(prepare_code(rejected), "not_found")
        self.assertEqual(
            prepare_code(
                AdminRouteOutcome(handled=True, content_key="admin.write_action_rejected")
            ),
            "rejected",
        )
        for key, code in (
            ("admin.write_action_unavailable", "unavailable"),
            ("admin.write_action_card_send_failed", "card_send_failed"),
            ("admin.internal_error", "internal_error"),
            ("admin.unknown", "invalid_request"),
        ):
            self.assertEqual(prepare_code(AdminRouteOutcome(handled=True, content_key=key)), code)

    def test_intents_match_only_the_same_action_target_and_scope(self):
        row = dict(
            action_type="local_permission_grant",
            target_open_id="ou_target",
            payload='{"position_name": "A商务", "company_scope": "*", "reason": "特批 一句", "pairs": [["1", "vat_rate"]]}',
        )
        same = request_intent("prepare_grant_position", TRANSLATIONS[2][1], target="ou_target")
        self.assertIn(same, row_intents(row))
        other = request_intent(
            "prepare_grant_position",
            {**TRANSLATIONS[2][1], "reason": "另一个理由"},
            target="ou_target",
        )
        self.assertNotIn(other, row_intents(row))
        self.assertNotIn(
            request_intent("prepare_suspend_user", {"identifier": "x"}, target="ou_target"),
            row_intents(row),
        )
        suspend = dict(action_type="suspend_user", target_open_id="ou_target", payload=None)
        self.assertIn(
            request_intent("prepare_suspend_user", {"identifier": "x"}, target="ou_target"),
            row_intents(suspend),
        )
        self.assertNotIn(
            request_intent("prepare_resume_user", {"identifier": "x"}, target="ou_target"),
            row_intents(suspend),
        )
        revoke = dict(
            action_type="local_permission_revoke",
            target_open_id="ou_owner",
            payload='{"override_id": "lpo_1", "company_id": "1011", "metric_name": "vat_rate", "reason": "撤销"}',
        )
        self.assertIn(
            request_intent(
                "prepare_revoke_permission",
                {"override_id": "lpo_1", "reason": "撤销"},
                target="lpo_1",
            ),
            row_intents(revoke),
        )
        self.assertIn(
            request_intent(
                "prepare_revoke_permission_by_scope",
                {
                    "identifier": "x",
                    "company_id": "1011",
                    "metric_name": "增值税率",
                    "reason": "撤销",
                },
                target="ou_owner",
                metric_name="vat_rate",
            ),
            row_intents(revoke),
        )
        group = dict(
            action_type="local_permission_revoke",
            target_open_id="ou_owner",
            payload='{"permission_group_id": "lpg_1", "override_ids": ["lpo_1", "lpo_2"], "reason": "撤"}',
        )
        self.assertIn(
            request_intent(
                "prepare_revoke_permission", {"group_id": "lpg_1", "reason": "撤"}, target="lpg_1"
            ),
            row_intents(group),
        )

    def test_operation_target_projects_users_overrides_and_groups(self):
        base = dict(
            id="pac_1",
            target_open_id="ou_target",
            target_state_snapshot="enabled",
            initiated_by_open_id="ou_admin",
            status=PendingActionStatus.PENDING,
            card_delivered=False,
            card_id=None,
            reason=None,
            created_at=datetime.now(UTC),
            confirm_deadline_at=datetime.now(UTC) + timedelta(minutes=10),
            decided_at=None,
            decided_by_open_id=None,
        )
        suspend = operation_target(
            PendingAction(action_type=PendingActionType.SUSPEND_USER, **base)
        )
        self.assertEqual(
            (suspend["target_kind"], suspend["target_count"], suspend["target_user_id"]),
            ("user", 1, "ou_target"),
        )
        self.assertTrue(suspend["target_digest"].startswith("sha256:"))
        self.assertEqual(suspend["result_counts"], {})
        grant = operation_target(
            PendingAction(
                action_type=PendingActionType.LOCAL_PERMISSION_GRANT,
                payload='{"pairs": [["1", "vat_rate"]], "reused_count": 1, "reason": "r"}',
                **base,
            )
        )
        self.assertEqual(grant["result_counts"], {"new": 1, "reused": 1})
        group = operation_target(
            PendingAction(
                action_type=PendingActionType.LOCAL_PERMISSION_REVOKE,
                payload='{"permission_group_id": "lpg_1", "override_ids": ["lpo_1", "lpo_2"], "pairs": [["1", "a"], ["1", "b"]], "galaxy_retained": null, "reason": "r"}',
                **{**base, "target_open_id": "owner@example.test"},
            )
        )
        self.assertEqual(
            (group["target_kind"], group["target_count"], group["target_user_id"]),
            ("override_group", 2, None),
        )
        self.assertEqual(group["result_counts"], {"revoked": 2})
        single = operation_target(
            PendingAction(
                action_type=PendingActionType.LOCAL_PERMISSION_REVOKE,
                payload='{"override_id": "lpo_1", "company_id": "1", "metric_name": "a", "galaxy_retained": 1, "reason": "r"}',
                **base,
            )
        )
        self.assertEqual((single["target_kind"], single["target_count"]), ("override", 1))
        self.assertEqual(single["result_counts"], {"revoked": 1, "galaxy_retained": 1})


class FakeRouter:
    def __init__(self, outcome):
        self.outcome, self.calls = outcome, []

    def route(self, **kwargs):
        self.calls.append(kwargs)
        return self.outcome


class FakeReadonly:
    def __init__(self):
        self.rows = []
        self.catalog_value = ({"1": {}, "*": {}}, ("A商务",))

    def action_item(self, open_id, pending_action_id):
        for row in self.rows:
            if row["id"] == pending_action_id and row["initiated_by_open_id"] == open_id:
                return dict(pending_action_id=row["id"], status=row["status"])
        return None

    def pending_rows(self, open_id, *, pending_action_id, limit, status):
        return [
            r for r in self.rows if r["initiated_by_open_id"] == open_id and r["status"] == status
        ]

    def catalog(self):
        return self.catalog_value


class PrepareAdapterTests(unittest.TestCase):
    """准备适配只经路由写入：文本逐字等价、结论映射、同意图沿用、拒绝落账。"""

    def build(self, outcome):
        self.router, self.readonly = FakeRouter(outcome), FakeReadonly()
        return RestrictedAdminPrepare(
            "postgresql://unused", router=self.router, queries=FakeQueries(), readonly=self.readonly
        )

    def in_flight(self, **fields):
        row = dict(
            id="pac_inflight",
            action_type="suspend_user",
            target_open_id="ou_target",
            initiated_by_open_id="ou_admin",
            status="pending",
            confirm_deadline_at=datetime.now(UTC) + timedelta(minutes=5),
            payload=None,
        )
        row.update(fields)
        self.readonly.rows.append(row)
        return row

    def test_every_tool_routes_the_exact_command_text_with_the_principal_identity(self):
        prepare = self.build(
            AdminRouteOutcome(
                handled=True,
                content_key="admin.write_action_pending",
                pending_action_id="pac_new",
                reply_text="已生成",
            )
        )
        self.in_flight(id="pac_new")
        for name, args, text in TRANSLATIONS:
            self.router.calls.clear()
            result = getattr(prepare, name)(PRINCIPAL, call_trace_id="trc_1", **args)
            self.assertEqual(
                self.router.calls, [dict(open_id="ou_admin", text=text, trace_id="trc_1")]
            )
            self.assertEqual(
                (result["ok"], result["state"], result["pending_action_id"]),
                (True, "pending", "pac_new"),
            )
            self.assertEqual(result["next_action"], "await_admin_confirmation")
            self.assertFalse(result["reused_pending_action"])
            self.assertEqual(result["message"], "已生成")

    def test_not_handled_is_not_authorized_and_nothing_is_projected(self):
        prepare = self.build(AdminRouteOutcome(handled=False))
        with self.assertRaisesRegex(InnertestError, "not_authorized"):
            prepare.prepare_suspend_user(PRINCIPAL, call_trace_id="trc_1", identifier="ou_target")

    def test_rejection_keeps_the_router_code_and_message_and_records_a_rejected_row(self):
        prepare = self.build(
            AdminRouteOutcome(
                handled=True,
                content_key="admin.write_action_rejected",
                decision_code="not_found",
                reply_text="未找到该用户记录。",
            )
        )
        recorded = []
        with (
            patch("lingxi.adapters.restricted_admin_prepare.connect") as connect,
            patch(
                "lingxi.adapters.restricted_admin_prepare.admin_roles_snapshot",
                return_value=frozenset(),
            ),
            patch(
                "lingxi.adapters.restricted_admin_prepare.record_operation_audit",
                side_effect=lambda c, e: recorded.append(e),
            ),
        ):
            connect.return_value.__enter__.return_value.transaction.return_value.__enter__.return_value = None
            result = prepare.prepare_suspend_user(
                PRINCIPAL, call_trace_id="trc_1", identifier="nobody@example.test"
            )
        self.assertEqual(
            (result["ok"], result["code"], result["state"]), (False, "not_found", "rejected")
        )
        self.assertEqual(result["message"], "未找到该用户记录。")
        self.assertIsNone(result["candidates"])
        (entry,) = recorded
        self.assertEqual(
            (entry.phase.value, entry.entry_point.value, entry.result_code),
            ("rejected", "restricted_channel", "not_found"),
        )
        self.assertEqual(
            (entry.operation, entry.operation_id, entry.initiated_by),
            ("admin.suspend_user", "trc_1", "ou_admin"),
        )
        self.assertIsNone(entry.target_user_id)
        self.assertNotIn("nobody@example.test", json.dumps(entry.__dict__, default=str))

    def test_rejected_row_failure_surfaces_as_audit_unavailable(self):
        prepare = self.build(
            AdminRouteOutcome(
                handled=True,
                content_key="admin.write_action_rejected",
                decision_code="target_state_changed",
                reply_text="x",
            )
        )
        with patch(
            "lingxi.adapters.restricted_admin_prepare.connect", side_effect=RuntimeError("down")
        ):
            with self.assertRaisesRegex(InnertestError, "audit_unavailable"):
                prepare.prepare_resume_user(
                    PRINCIPAL, call_trace_id="trc_1", identifier="ou_target"
                )

    def test_unavailable_and_card_send_failed_are_not_written_to_the_ledger(self):
        for key, code in (
            ("admin.write_action_unavailable", "unavailable"),
            ("admin.write_action_card_send_failed", "card_send_failed"),
        ):
            prepare = self.build(
                AdminRouteOutcome(
                    handled=True,
                    content_key=key,
                    pending_action_id="pac_failed" if code != "unavailable" else None,
                    reply_text="m",
                )
            )
            self.in_flight(id="pac_failed", status="failed")
            with patch("lingxi.adapters.restricted_admin_prepare.connect") as connect:
                result = prepare.prepare_suspend_user(
                    PRINCIPAL, call_trace_id="trc_1", identifier="ou_target"
                )
            connect.assert_not_called()
            self.assertEqual(result["code"], code)
            if code == "card_send_failed":
                self.assertEqual(result["action"]["status"], "failed")

    def test_position_rejection_attaches_candidates(self):
        prepare = self.build(
            AdminRouteOutcome(
                handled=True,
                content_key="admin.write_action_rejected",
                decision_code="position_mapping_unavailable",
                reply_text="职位或公司范围当前不可用",
            )
        )
        with (
            patch("lingxi.adapters.restricted_admin_prepare.connect"),
            patch(
                "lingxi.adapters.restricted_admin_prepare.admin_roles_snapshot",
                return_value=frozenset(),
            ),
            patch("lingxi.adapters.restricted_admin_prepare.record_operation_audit"),
        ):
            result = prepare.prepare_grant_position(
                PRINCIPAL,
                call_trace_id="trc_1",
                identifier="ou_target",
                position_name="不存在",
                company_scope="*",
                reason="r",
            )
        self.assertEqual(result["code"], "position_mapping_unavailable")
        self.assertEqual(
            result["candidates"], {"positions": ["A商务"], "companies": ["1"], "reason": None}
        )

    def test_same_intent_in_flight_is_reused_without_a_second_card_and_other_intent_gets_the_summary(
        self,
    ):
        prepare = self.build(
            AdminRouteOutcome(
                handled=True,
                content_key="admin.write_action_rejected",
                decision_code="target_has_pending_action",
                reply_text="该用户当前已有一条待确认操作在途：停用用户",
            )
        )
        self.in_flight()
        with patch("lingxi.adapters.restricted_admin_prepare.connect") as connect:
            reused = prepare.prepare_suspend_user(
                PRINCIPAL, call_trace_id="trc_1", identifier="target@example.test"
            )
            connect.assert_not_called()
            self.assertEqual(
                (reused["ok"], reused["pending_action_id"], reused["reused_pending_action"]),
                (True, "pac_inflight", True),
            )
            with (
                patch(
                    "lingxi.adapters.restricted_admin_prepare.admin_roles_snapshot",
                    return_value=frozenset(),
                ),
                patch("lingxi.adapters.restricted_admin_prepare.record_operation_audit"),
            ):
                other = prepare.prepare_resume_user(
                    PRINCIPAL, call_trace_id="trc_2", identifier="ou_target"
                )
        self.assertEqual((other["ok"], other["code"]), (False, "target_has_pending_action"))
        self.assertIn("停用用户", other["message"])
        self.readonly.rows[0]["confirm_deadline_at"] = datetime.now(UTC) - timedelta(seconds=1)
        with (
            patch("lingxi.adapters.restricted_admin_prepare.connect"),
            patch(
                "lingxi.adapters.restricted_admin_prepare.admin_roles_snapshot",
                return_value=frozenset(),
            ),
            patch("lingxi.adapters.restricted_admin_prepare.record_operation_audit"),
        ):
            expired = prepare.prepare_suspend_user(
                PRINCIPAL, call_trace_id="trc_3", identifier="ou_target"
            )
        self.assertFalse(expired["ok"])
        self.readonly.rows[0]["initiated_by_open_id"] = "ou_other"
        self.readonly.rows[0]["confirm_deadline_at"] = datetime.now(UTC) + timedelta(minutes=5)
        with (
            patch("lingxi.adapters.restricted_admin_prepare.connect"),
            patch(
                "lingxi.adapters.restricted_admin_prepare.admin_roles_snapshot",
                return_value=frozenset(),
            ),
            patch("lingxi.adapters.restricted_admin_prepare.record_operation_audit"),
        ):
            theirs = prepare.prepare_suspend_user(
                PRINCIPAL, call_trace_id="trc_4", identifier="ou_target"
            )
        self.assertFalse(theirs["ok"])
