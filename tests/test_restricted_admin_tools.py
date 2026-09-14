"""只读三工具的纯逻辑与通道分发（假查询口）；真库断言见 test_restricted_admin_postgres。"""

import json
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace

from lingxi.adapters.restricted_admin_queries import (
    RestrictedAdminQueries,
    RestrictedChannelService,
)
from lingxi.core.admin.followup import FollowupRef
from lingxi.core.admin.innertest import InnertestError, envelope
from lingxi.core.admin.restricted_tools import (
    candidate_lists,
    pending_action_item,
    pending_actions_result,
    permission_sources_result,
    user_status_result,
)
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

    def test_pending_action_item_keeps_reuse_fields_as_placeholders(self):
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
            payload='{"pairs": [["1011", "vat_rate"]], "reason": "特批"}',
        )
        item = pending_action_item(
            row, (FollowupRef(id="f1", stage="confirmation_card_send", status="unknown"),)
        )
        self.assertEqual(item["payload"]["pairs"], [["1011", "vat_rate"]])
        self.assertEqual(item["followups"][0]["status"], "unknown")
        self.assertEqual(item["created_at"], now.isoformat())
        self.assertIsNone(item["reused_count"])
        self.assertIsNone(item["retained_by_galaxy"])
        self.assertFalse(item["card_delivered"])
        self.assertIsNone(pending_action_item({**row, "payload": "{broken"}, ())["payload"])


class FakeQueries:
    def __init__(self):
        self.status = status(galaxy=GalaxySourceSummary(granted=False, reason="no_galaxy_roles"))
        self.trace = None
        self.calls = []

    def resolve_identifier(self, *, identifier):
        self.calls.append(("resolve", identifier))
        return "ou_target" if "@" in identifier else identifier

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
        with self.assertRaisesRegex(InnertestError, "invalid_request"):
            self.service.call(PRINCIPAL, "confirm_pending_action", {})
        self.assertEqual(self.audit.rows, [])

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
