"""受限通道只读三工具的合成 PostgreSQL 验证：真实身份链、零写库、查无与撤销否定。"""

import os
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.adapters.admin_registry import PostgresAdminQueries, seed_admin_registry_entry
from lingxi.adapters.innertest_mcp import InnertestMcpSession
from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, connect
from lingxi.adapters.postgres_admin_followup import PostgresFollowupStore, enqueue_followups
from lingxi.adapters.postgres_innertest import PostgresInnertestService
from lingxi.adapters.postgres_local_permission import PostgresLocalPermissionOverrideStore
from lingxi.adapters.restricted_admin_prepare import build_restricted_prepare
from lingxi.adapters.restricted_admin_queries import (
    RestrictedAdminQueries,
    RestrictedChannelService,
)
from lingxi.core.admin.followup import FollowupSpec
from lingxi.core.admin.innertest import InnertestError
from lingxi.core.admin.restricted_tools import READ_ONLY_TOOL_NAMES
from lingxi.core.identity.preprovision import PreprovisionSkip
from lingxi.core.ids import new_id
from lingxi.core.permission.local_override import OverrideDirection

DSN = os.environ.get("LINGXI_POSTGRES_DSN")
TRACE = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
ELEVEN_TOOLS = (
    ("list_innertest_members", {}),
    ("prepare_innertest_additions", {"request_key": "k", "emails": ["p@example.test"]}),
    ("get_innertest_batch", {"batch_id": "b"}),
    ("get_user_status", {"identifier": "ou_target"}),
    ("get_user_permission_sources", {"identifier": "ou_target"}),
    ("get_pending_actions", {}),
    ("prepare_suspend_user", {"identifier": "ou_target"}),
    ("prepare_resume_user", {"identifier": "ou_target"}),
    (
        "prepare_grant_position",
        {"identifier": "ou_target", "position_name": "A商务", "company_scope": "1", "reason": "r"},
    ),
    (
        "prepare_revoke_permission",
        {"override_id": "lpo_01ARZ3NDEKTSV4RRFFQ69G5FAV", "reason": "r"},
    ),
    (
        "prepare_revoke_permission_by_scope",
        {"identifier": "ou_target", "company_id": "1", "metric_name": "vat_rate", "reason": "r"},
    ),
)
COUNTED_TABLES = (
    "app_user",
    "pending_action",
    "admin_action_followup",
    "local_permission_override",
    "innertest_membership",
    "innertest_batch",
    "innertest_audit",
    "inbound_event",
    "operation_audit",
)


@unittest.skipUnless(DSN and psycopg_available(), "需独占合成 PostgreSQL")
class RestrictedAdminPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_production_schema(DSN)

    def setUp(self):
        reset_production_rows(DSN)
        seed_admin_registry_entry(DSN, feishu_open_id="ou_admin", label="合成管理员")
        self.sql("INSERT INTO innertest_roster_version(scope,mode) VALUES('synthetic','database')")
        self.sql(
            "INSERT INTO innertest_admin_binding(id,scope,open_id,version,enabled) "
            "VALUES('binding','synthetic','ou_admin',1,true)"
        )
        self.audit = Mock()
        innertest = PostgresInnertestService(
            DSN,
            scope="synthetic",
            binding=SimpleNamespace(binding_id="binding", peer_uid=1234),
            locator=lambda email, connection: PreprovisionSkip(
                email=email, reason="email_not_in_roster"
            ),
            audit=self.audit,
        )
        admin_queries, followups = PostgresAdminQueries(DSN), PostgresFollowupStore(DSN)
        readonly = RestrictedAdminQueries(
            DSN, queries=admin_queries, followups=followups, metric_map_path=None
        )
        self.service = RestrictedChannelService(
            innertest=innertest,
            queries=readonly,
            audit=self.audit,
            prepare=build_restricted_prepare(
                DSN,
                audit=self.audit,
                metric_map_path=None,
                timeouts=DEFAULT_POSTGRES_TIMEOUTS,
                queries=admin_queries,
                readonly=readonly,
                store=followups,
            ),
        )
        self.session = InnertestMcpSession(peer_uid=1234, service=self.service)
        self.request("initialize", {"protocolVersion": "2025-11-25"})
        self.request("notifications/initialized")

    def sql(self, text, args=()):
        with connect(DSN) as c, c.cursor() as cur:
            cur.execute(text, args)
            return cur.fetchall() if cur.description else None

    def request(self, method, params=None):
        return self.session.handle(dict(jsonrpc="2.0", id=1, method=method, params=params or {}))

    def tool(self, name, arguments):
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def add_user(self, open_id="ou_target", user_id="usr_target", email="target@example.test"):
        self.sql(
            "INSERT INTO app_user(id,feishu_open_id,feishu_user_id,feishu_union_id,display_name,"
            "department,tenant_key,provisioning_state,permission_version,email) "
            "VALUES(%s,%s,%s,%s,'化名','部门','tk',"
            "'active',2,%s)",
            (user_id, open_id, "fs_" + open_id, "un_" + open_id, email),
        )

    def add_pending(self, *, initiated_by="ou_admin", status="pending", target=None):
        pending_id = new_id("pac")
        now = datetime.now(UTC)
        decided = None if status == "pending" else now
        self.sql(
            "INSERT INTO pending_action(id,action_type,target_open_id,target_state_snapshot,"
            "initiated_by_open_id,status,confirm_deadline_at,decided_at,decided_by_open_id) "
            "VALUES(%s,'suspend_user',%s,'enabled',%s,%s,%s,%s,%s)",
            (
                pending_id,
                target or "ou_target_" + pending_id,
                initiated_by,
                status,
                now + timedelta(minutes=10),
                decided,
                None if decided is None else initiated_by,
            ),
        )
        return pending_id

    def counts(self):
        return {t: self.sql(f"SELECT count(*) FROM {t}")[0][0] for t in COUNTED_TABLES}

    def audited(self, tool):
        return [
            call.kwargs
            for call in self.audit.record.call_args_list
            if call.args == ("admin.restricted." + tool,)
        ]

    def test_user_status_by_email_and_trace_with_zero_writes(self):
        self.add_user()
        self.sql(
            "INSERT INTO inbound_event(feishu_event_id,received_at,event_type,user_open_id,"
            "handled_as,trace_id) VALUES('evt_1',now(),'im.message.receive_v1','ou_target',"
            "'task_queued',%s)",
            (TRACE,),
        )
        before = self.counts()
        result = self.tool("get_user_status", {"identifier": "target@example.test"})["result"]
        self.assertFalse(result["isError"])
        body = result["structuredContent"]
        self.assertEqual(body["query"]["resolved_open_id"], "ou_target")
        self.assertEqual(body["user"]["account_state"], "enabled")
        self.assertEqual(body["recent_events"][0]["trace_id"], TRACE)
        trace = self.tool("get_user_status", {"trace_id": TRACE})["result"]["structuredContent"]
        self.assertEqual(trace["trace"]["event_count"], 1)
        self.assertEqual(trace["trace"]["last_handled_as"], "task_queued")
        self.assertEqual(self.counts(), before)
        self.assertEqual(
            [row["result_code"] for row in self.audited("get_user_status")], ["ok", "ok"]
        )

    def test_not_found_never_guesses_a_similar_person(self):
        self.add_user()
        for arguments in (
            {"identifier": "ou_targe"},
            {"identifier": "targe@example.test"},
            {"identifier": "TARGET@example.test"},
            {"trace_id": "01ARZ3NDEKTSV4RRFFQ69G5FAX"},
            {"trace_id": "not-a-reference"},
        ):
            result = self.tool("get_user_status", arguments)["result"]
            self.assertTrue(result["isError"])
            self.assertEqual(result["structuredContent"]["code"], "not_found")
        result = self.tool("get_user_permission_sources", {"identifier": "ou_nobody"})["result"]
        self.assertEqual(result["structuredContent"]["code"], "not_found")
        self.assertEqual(
            {row["result_code"] for row in self.audited("get_user_status")}, {"not_found"}
        )

    def test_permission_sources_report_unreadable_galaxy_and_local_groups(self):
        self.add_user()
        store = PostgresLocalPermissionOverrideStore(DSN)
        pending_id = self.add_pending(status="executed")
        store.insert(
            user_id="usr_target",
            direction=OverrideDirection.GRANT,
            company_id="1",
            metric_name="vat_rate",
            reason="特批",
            initiated_by_open_id="ou_admin",
            pending_action_id=pending_id,
        )
        before = self.counts()
        body = self.tool("get_user_permission_sources", {"identifier": "ou_target"})["result"][
            "structuredContent"
        ]
        self.assertTrue(body["ok"])
        self.assertFalse(body["galaxy"]["available"])
        self.assertEqual(body["galaxy"]["reason"], "roster_snapshot_unavailable")
        self.assertIsNone(body["merged"])
        self.assertEqual(body["merged_reason"], "roster_snapshot_unavailable")
        self.assertEqual(body["local"]["groups"][0]["entries"][0]["metric_name"], "vat_rate")
        self.assertIn("1", body["candidates"]["companies"])
        self.assertEqual(self.counts(), before)

    def test_pending_actions_are_scoped_to_the_principal(self):
        mine = self.add_pending()
        with connect(DSN) as connection, connection.transaction():
            enqueue_followups(
                connection,
                pending_action_id=mine,
                trace_id=new_id("trc"),
                items=(FollowupSpec(subject_key=mine, stage="confirmation_card_send"),),
            )
        executed = self.add_pending(status="executed")
        theirs = self.add_pending(initiated_by="ou_other")
        before = self.counts()
        body = self.tool("get_pending_actions", {})["result"]["structuredContent"]
        self.assertEqual({item["pending_action_id"] for item in body["actions"]}, {mine, executed})
        listed = {item["pending_action_id"]: item for item in body["actions"]}
        self.assertEqual(listed[mine]["followups"][0]["stage"], "confirmation_card_send")
        self.assertEqual(listed[mine]["followups"][0]["status"], "pending")
        self.assertFalse(listed[mine]["card_delivered"])
        self.assertIsNone(listed[mine]["reused_count"])
        only_pending = self.tool("get_pending_actions", {"status": "pending"})["result"]
        self.assertEqual(
            [i["pending_action_id"] for i in only_pending["structuredContent"]["actions"]], [mine]
        )
        single = self.tool("get_pending_actions", {"pending_action_id": mine})["result"]
        self.assertEqual(single["structuredContent"]["count"], 1)
        other = self.tool("get_pending_actions", {"pending_action_id": theirs})["result"]
        self.assertEqual(other["structuredContent"]["code"], "not_found")
        self.assertEqual(self.counts(), before)

    def test_unbound_uid_and_registry_revocation_reject_all_eleven_tools(self):
        self.add_user()
        with self.assertRaisesRegex(InnertestError, "not_authenticated"):
            self.service.authenticate(1235)
        for name, arguments in ELEVEN_TOOLS:
            self.assertIn("result", self.tool(name, arguments))
        prepared = self.tool("get_pending_actions", {})["result"]["structuredContent"]
        self.assertEqual(prepared["count"], 1)
        self.sql(
            "UPDATE admin_registry SET entry_status='revoked', revoked_at=now() "
            "WHERE feishu_open_id='ou_admin'"
        )
        before = self.counts()
        for name, arguments in ELEVEN_TOOLS:
            self.assertEqual(self.tool(name, arguments)["error"]["message"], "not_authorized")
        self.assertEqual(self.request("tools/list")["error"]["message"], "not_authorized")
        self.assertEqual(self.counts(), before)
        seed_admin_registry_entry(DSN, feishu_open_id="ou_admin", label="合成管理员")
        self.assertIn("result", self.tool(*ELEVEN_TOOLS[3]))
        self.sql("UPDATE innertest_admin_binding SET enabled=false WHERE id='binding'")
        for name, arguments in ELEVEN_TOOLS:
            self.assertEqual(self.tool(name, arguments)["error"]["message"], "binding_disabled")
        self.assertEqual(self.counts(), before)
        self.assertEqual(
            {row["result_code"] for row in self.audited(READ_ONLY_TOOL_NAMES[2])}, {"ok"}
        )
