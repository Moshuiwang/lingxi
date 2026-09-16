"""受限通道五个准备工具的合成 PostgreSQL 验证：五类稳妥性场景、持久审计三阶段行、gateway 分派发卡。

场景对应：准备动作（一次调用 = 待确认行 + 发卡阶段 + ``prepared`` 行，登记失败即无可确认之物）、
未确认不执行、范围澄清、重复与过期确认、结果不明时回查。全部经真实 ``AdminCommandRouter``
与真实仓储，不连接外部平台。
"""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.adapters.admin_confirmation_card import AdminConfirmationCard, ConfirmationCardDispatch
from lingxi.adapters.admin_registry import (
    PostgresAdminQueries,
    PostgresAdminRegistryLookup,
    seed_admin_registry_entry,
)
from lingxi.adapters.innertest_mcp import InnertestMcpSession
from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, connect
from lingxi.adapters.postgres_admin_followup import PostgresFollowupStore
from lingxi.adapters.postgres_innertest import PostgresInnertestService
from lingxi.adapters.postgres_local_permission import PostgresLocalPermissionOverrideStore
from lingxi.adapters.postgres_pending_action import PostgresPendingActionStore
from lingxi.adapters.restricted_admin_prepare import build_restricted_prepare
from lingxi.adapters.restricted_admin_queries import (
    RestrictedAdminQueries,
    RestrictedChannelService,
)
from lingxi.core.admin.followup_consumer import FollowupConsumer
from lingxi.core.admin.pending_action import PendingActionStatus
from lingxi.core.admin.restricted_tools import PREPARE_TOOL_NAMES, READ_ONLY_TOOL_NAMES
from lingxi.core.identity.preprovision import PreprovisionSkip
from lingxi.core.permission.local_override import OverrideDirection

DSN = os.environ.get("LINGXI_POSTGRES_DSN")
ADMIN = "ou_admin"
TARGET = "ou_target"
FORBIDDEN = ("confirm", "cancel", "execute", "suppress")


class DefiniteRejectionError(RuntimeError):
    definite = True


@unittest.skipUnless(DSN and psycopg_available(), "需独占合成 PostgreSQL")
class RestrictedAdminWriteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_production_schema(DSN)

    def setUp(self):
        reset_production_rows(DSN)
        seed_admin_registry_entry(DSN, feishu_open_id=ADMIN, label="合成管理员")
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
        self.queries = PostgresAdminQueries(DSN)
        self.followups = PostgresFollowupStore(DSN)
        readonly = RestrictedAdminQueries(
            DSN, queries=self.queries, followups=self.followups, metric_map_path=None
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
                queries=self.queries,
                readonly=readonly,
                store=self.followups,
            ),
        )
        self.session = InnertestMcpSession(peer_uid=1234, service=self.service)
        self.request("initialize", {"protocolVersion": "2025-11-25"})
        self.request("notifications/initialized")
        # gateway 侧的确认仓储：确认事务登记阶段并写账目终态行。
        self.gateway_store = PostgresPendingActionStore(
            DSN, audit=self.audit, metric_map_path=None, durable_followups=True
        )

    def sql(self, text, args=()):
        with connect(DSN) as c, c.cursor() as cur:
            cur.execute(text, args)
            return cur.fetchall() if cur.description else None

    def request(self, method, params=None):
        return self.session.handle(dict(jsonrpc="2.0", id=1, method=method, params=params or {}))

    def tool(self, name, arguments):
        return self.request("tools/call", {"name": name, "arguments": arguments})["result"][
            "structuredContent"
        ]

    def add_user(
        self,
        open_id=TARGET,
        user_id="usr_target",
        email="target@example.test",
        employee_no="job-1",
    ):
        self.sql(
            "INSERT INTO app_user(id,feishu_open_id,feishu_user_id,feishu_union_id,display_name,"
            "department,tenant_key,provisioning_state,permission_version,email,employee_no) "
            "VALUES(%s,%s,%s,%s,'化名','部门','tk','active',2,%s,%s)",
            (user_id, open_id, "fs_" + open_id, "un_" + open_id, email, employee_no),
        )

    def add_roster(self, *rows):
        self.sql(
            "INSERT INTO roster_snapshot(id,captured_at,row_count,pages_read) "
            "VALUES('restricted-write-roster',now(),%s,1)",
            (len(rows),),
        )
        for index, (personnel_id, email, employee_no) in enumerate(rows):
            self.sql(
                "INSERT INTO roster_snapshot_row("
                "snapshot_id,row_index,personnel_id,email,name,employee_no,record_id) "
                "VALUES('restricted-write-roster',%s,%s,%s,'合成员工',%s,%s)",
                (index, personnel_id, email, employee_no, "record-" + str(index)),
            )

    def counts(self):
        return {
            t: self.sql(f"SELECT count(*) FROM {t}")[0][0]
            for t in ("pending_action", "admin_action_followup", "operation_audit")
        }

    def ledger(self, operation_id=None):
        where = "" if operation_id is None else " WHERE operation_id=%s"
        return self.sql(
            "SELECT phase,entry_point,operation,operation_id,initiated_by,actor_roles,decided_by,"
            "executor,target_kind,target_user_id,result_code,result_counts,evidence_ref,"
            "pending_action_id,trace_id FROM operation_audit" + where + " ORDER BY created_at,id",
            () if operation_id is None else (operation_id,),
        )

    def prepare_suspend(self, identifier=TARGET):
        return self.tool("prepare_suspend_user", {"identifier": identifier})

    def deliver(self, pending_id):
        self.gateway_store.mark_card_delivered(pending_action_id=pending_id, card_id="card")

    def account_state(self, open_id=TARGET):
        return self.sql("SELECT account_state FROM app_user WHERE feishu_open_id=%s", (open_id,))[
            0
        ][0]

    def structured(self, action):
        return [c.kwargs for c in self.audit.record.call_args_list if c.args == (action,)]

    # ------------------------------------------------------------------ 场景 1
    def test_prepare_writes_pending_stage_and_prepared_row_and_failure_leaves_nothing_confirmable(
        self,
    ):
        self.add_user()
        self.add_roster(("fs_ou_target", "target@example.test", "job-1"))
        result = self.prepare_suspend("target@example.test")
        self.assertTrue(result["ok"], result)
        pending_id = result["pending_action_id"]
        self.assertEqual(result["state"], "pending")
        self.assertEqual(result["next_action"], "await_admin_confirmation")
        self.assertEqual(result["action"]["target_open_id"], TARGET)
        self.assertFalse(result["action"]["card_delivered"])
        self.assertEqual(result["action"]["followups"][0]["stage"], "confirmation_card_send")
        self.assertEqual(
            self.counts(), {"pending_action": 1, "admin_action_followup": 1, "operation_audit": 1}
        )
        self.assertEqual(
            self.sql("SELECT status,card_delivered FROM pending_action"), [("pending", False)]
        )
        (row,) = self.ledger(pending_id)
        self.assertEqual(
            row[:5], ("prepared", "restricted_channel", "admin.suspend_user", pending_id, ADMIN)
        )
        self.assertEqual(row[5], "permission_admin,ops_admin,super_admin")
        self.assertEqual(
            (row[8], row[9], row[12], row[13]),
            ("user", TARGET, "pending_action:" + pending_id, pending_id),
        )
        self.assertEqual(
            self.structured("admin.restricted.prepare_suspend_user")[0]["pending_action_id"],
            pending_id,
        )
        self.assertEqual(
            self.structured("admin.command.suspend_user")[0]["pending_action_id"], pending_id
        )
        self.assertEqual(self.account_state(), "enabled")

        reset_production_rows(DSN)
        seed_admin_registry_entry(DSN, feishu_open_id=ADMIN, label="合成管理员")
        self.sql("INSERT INTO innertest_roster_version(scope,mode) VALUES('synthetic','database')")
        self.sql(
            "INSERT INTO innertest_admin_binding(id,scope,open_id,version,enabled) "
            "VALUES('binding','synthetic','ou_admin',1,true)"
        )
        self.add_user()
        with patch(
            "lingxi.adapters.followup_confirm_card_sender.enqueue_followups",
            side_effect=RuntimeError("synthetic-enqueue-failure"),
        ):
            failed = self.prepare_suspend()
        self.assertEqual((failed["ok"], failed["code"]), (False, "card_send_failed"))
        self.assertEqual(failed["action"]["status"], "failed")
        failed_id = failed["action"]["pending_action_id"]
        self.assertEqual(
            self.sql("SELECT count(*) FROM pending_action WHERE status='pending'"), [(0,)]
        )
        self.assertEqual(self.counts()["admin_action_followup"], 0)
        # 阶段登记失败：待确认操作已转 failed，账上尽力补一行 rejected 作痕迹（无 prepared 行）。
        self.assertEqual(self.counts()["operation_audit"], 1)
        (rejected,) = self.ledger(failed_id)
        self.assertEqual(
            rejected[:5],
            ("rejected", "restricted_channel", "admin.suspend_user", failed_id, ADMIN),
        )
        self.assertEqual(rejected[5], "permission_admin,ops_admin,super_admin")
        self.assertEqual(
            (rejected[8], rejected[9], rejected[10], rejected[12], rejected[13]),
            (
                "user",
                TARGET,
                "prepare_ledger_failed:RuntimeError",
                "pending_action:" + failed_id,
                failed_id,
            ),
        )
        self.assertEqual(self.structured("operation_audit.write_failed"), [])
        with patch(
            "lingxi.adapters.followup_confirm_card_sender.record_operation_audit",
            side_effect=RuntimeError("synthetic-ledger-failure"),
        ):
            failed = self.prepare_suspend()
        self.assertEqual(failed["code"], "card_send_failed")
        self.assertEqual(failed["action"]["status"], "failed")
        self.assertEqual(self.counts()["admin_action_followup"], 0)
        # 账本身写不进：补 rejected 行同样失败，只留一行结构化日志，结论不变。
        self.assertEqual(self.counts()["operation_audit"], 1)
        self.assertEqual(
            self.structured("operation_audit.write_failed"),
            [
                dict(
                    pending_action_id=failed["action"]["pending_action_id"],
                    phase="rejected",
                    error="RuntimeError",
                )
            ],
        )
        self.assertEqual(self.account_state(), "enabled")

    # ------------------------------------------------------------------ 场景 2
    def test_unconfirmed_never_executes_and_no_tool_can_confirm(self):
        self.add_user()
        pending_id = self.prepare_suspend()["pending_action_id"]
        names = [t["name"] for t in self.request("tools/list")["result"]["tools"]]
        self.assertEqual(len(names), 11)
        self.assertEqual(tuple(names[6:]), PREPARE_TOOL_NAMES)
        for name in names:
            for part in FORBIDDEN:
                self.assertNotIn(part, name)
        for name in ("confirm_pending_action", "cancel_pending_action", "execute_pending_action"):
            self.assertEqual(
                self.tool(name, {"pending_action_id": pending_id})["code"], "invalid_request"
            )
        self.assertEqual(self.account_state(), "enabled")
        self.assertEqual(self.sql("SELECT count(*) FROM local_permission_override"), [(0,)])
        undelivered = self.gateway_store.confirm(
            pending_action_id=pending_id, clicker_open_id=ADMIN
        )
        self.assertEqual(undelivered.decision.code, "not_found")
        self.assertEqual(self.account_state(), "enabled")
        self.sql(
            "UPDATE pending_action SET confirm_deadline_at=now()-interval '1 second' WHERE id=%s",
            (pending_id,),
        )
        again = self.prepare_suspend()
        self.assertTrue(again["ok"])
        self.assertNotEqual(again["pending_action_id"], pending_id)
        self.assertEqual(
            self.sql("SELECT status FROM pending_action WHERE id=%s", (pending_id,)), [("expired",)]
        )
        self.assertEqual(self.account_state(), "enabled")

    # ------------------------------------------------------------------ 场景 3
    def test_scope_clarification_rejects_with_candidates_and_zero_rows(self):
        self.add_user()
        unknown_position = self.tool(
            "prepare_grant_position",
            {
                "identifier": TARGET,
                "position_name": "不存在的职位",
                "company_scope": "*",
                "reason": "特批",
            },
        )
        self.assertEqual(
            (unknown_position["ok"], unknown_position["code"]),
            (False, "position_mapping_unavailable"),
        )
        self.assertIn("A商务", unknown_position["candidates"]["positions"])
        self.assertIn("1", unknown_position["candidates"]["companies"])
        unknown_scope = self.tool(
            "prepare_grant_position",
            {
                "identifier": TARGET,
                "position_name": "A商务",
                "company_scope": "9999",
                "reason": "特批",
            },
        )
        self.assertEqual(unknown_scope["code"], "position_mapping_unavailable")
        nobody = self.tool(
            "prepare_grant_position",
            {
                "identifier": "nobody@example.test",
                "position_name": "A商务",
                "company_scope": "1",
                "reason": "特批",
            },
        )
        self.assertEqual(nobody["code"], "not_found")
        self.assertEqual(self.prepare_suspend("ou_nobody")["code"], "not_found")
        self_grant = self.tool(
            "prepare_grant_position",
            {
                "identifier": ADMIN,
                "position_name": "A商务",
                "company_scope": "1",
                "reason": "给自己",
            },
        )
        self.assertEqual(self_grant["code"], "self_target_forbidden")
        self.assertEqual(self.counts()["pending_action"], 0)
        self.assertEqual(self.counts()["admin_action_followup"], 0)
        rejected = self.ledger()
        self.assertEqual([r[0] for r in rejected], ["rejected"] * 5)
        self.assertEqual(
            [r[10] for r in rejected],
            [
                "position_mapping_unavailable",
                "position_mapping_unavailable",
                "not_found",
                "not_found",
                "self_target_forbidden",
            ],
        )
        self.assertEqual({r[1] for r in rejected}, {"restricted_channel"})
        self.assertIsNone(rejected[2][9])
        self.assertNotIn("nobody@example.test", str(rejected))

        self.sql(
            "INSERT INTO pending_action(id,action_type,target_open_id,target_state_snapshot,"
            "initiated_by_open_id,status,confirm_deadline_at,decided_at,decided_by_open_id) "
            "VALUES('pac_old','suspend_user',%s,'enabled',%s,'executed',now(),now(),%s)",
            (TARGET, ADMIN, ADMIN),
        )
        PostgresLocalPermissionOverrideStore(DSN).insert(
            user_id="usr_target",
            direction=OverrideDirection.GRANT,
            company_id="1",
            metric_name="vat_rate",
            reason="旧授权",
            initiated_by_open_id=ADMIN,
            pending_action_id="pac_old",
        )
        overlap = self.tool(
            "prepare_grant_position",
            {
                "identifier": TARGET,
                "position_name": "A商务",
                "company_scope": "1",
                "reason": "补授",
            },
        )
        self.assertTrue(overlap["ok"], overlap)
        self.assertEqual(
            (overlap["action"]["new_count"], overlap["action"]["reused_count"]), (1, 1)
        )
        self.assertEqual(overlap["action"]["payload"]["pairs"], [["1", "exchange_rate"]])
        (prepared,) = self.ledger(overlap["pending_action_id"])
        self.assertEqual((prepared[0], prepared[11]), ("prepared", {"new": 1, "reused": 1}))
        self.assertEqual(self.sql("SELECT count(*) FROM local_permission_override"), [(1,)])

    # ------------------------------------------------------------------ 场景 4
    def test_repeat_expiry_and_repeated_click(self):
        self.add_user()
        self.add_roster(("fs_ou_target", "target@example.test", "job-1"))
        first = self.prepare_suspend()
        second = self.prepare_suspend("target@example.test")
        self.assertEqual(second["pending_action_id"], first["pending_action_id"])
        self.assertTrue(second["reused_pending_action"])
        self.assertEqual(
            self.counts(), {"pending_action": 1, "admin_action_followup": 1, "operation_audit": 1}
        )
        other = self.tool(
            "prepare_grant_position",
            {
                "identifier": TARGET,
                "position_name": "A商务",
                "company_scope": "1",
                "reason": "补授",
            },
        )
        self.assertEqual((other["ok"], other["code"]), (False, "target_has_pending_action"))
        self.assertIn("停用用户", other["message"])
        self.assertEqual(self.counts()["pending_action"], 1)

        pending_id = first["pending_action_id"]
        self.deliver(pending_id)
        stranger = self.gateway_store.confirm(
            pending_action_id=pending_id, clicker_open_id="ou_stranger"
        )
        self.assertEqual(stranger.decision.code, "not_authorized")
        self.assertEqual(self.account_state(), "enabled")
        self.sql(
            "UPDATE pending_action SET confirm_deadline_at=now()-interval '1 second' WHERE id=%s",
            (pending_id,),
        )
        expired = self.gateway_store.confirm(pending_action_id=pending_id, clicker_open_id=ADMIN)
        self.assertEqual(expired.decision.code, "action_expired")
        self.assertEqual(self.account_state(), "enabled")
        self.assertEqual([r[0] for r in self.ledger(pending_id)], ["prepared", "rejected"])
        self.assertEqual(self.ledger(pending_id)[1][10], "expired")

        fresh = self.prepare_suspend()["pending_action_id"]
        self.deliver(fresh)
        executed = self.gateway_store.confirm(
            pending_action_id=fresh, clicker_open_id=ADMIN, trace_id="01ARZ3NDEKTSV4RRFFQ69G5FAV"
        )
        self.assertTrue(executed.decision.ok)
        self.assertEqual(self.account_state(), "suspended")
        repeated = self.gateway_store.confirm(pending_action_id=fresh, clicker_open_id=ADMIN)
        self.assertEqual(repeated.decision.code, "already_executed")
        rows = self.ledger(fresh)
        self.assertEqual([r[0] for r in rows], ["prepared", "confirmed", "executed"])
        confirmed, done = rows[1], rows[2]
        self.assertEqual(
            (confirmed[1], confirmed[6], confirmed[14]),
            ("feishu_card", ADMIN, "01ARZ3NDEKTSV4RRFFQ69G5FAV"),
        )
        recompute = self.sql(
            "SELECT id FROM admin_action_followup WHERE pending_action_id=%s AND stage='permission_recompute'",
            (fresh,),
        )[0][0]
        self.assertEqual((done[10], done[12]), ("executed", "followup:" + recompute))
        self.assertTrue(done[7].startswith("gateway@"))
        self.assertTrue(done[7].endswith(":01ARZ3NDEKTSV4RRFFQ69G5FAV"))
        item = self.tool("get_pending_actions", {"pending_action_id": fresh})["actions"][0]
        self.assertEqual(
            (item["status"], item["decided_by_open_id"], item["publish_state"]),
            ("executed", ADMIN, None),
        )

    def test_cancel_row_and_ledger_failure_rolls_back_the_confirmation(self):
        self.add_user()
        pending_id = self.prepare_suspend()["pending_action_id"]
        self.deliver(pending_id)
        with patch(
            "lingxi.adapters.postgres_admin_followup_confirmation.record_operation_audit",
            side_effect=RuntimeError("synthetic-ledger-failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.gateway_store.confirm(pending_action_id=pending_id, clicker_open_id=ADMIN)
        self.assertEqual(self.account_state(), "enabled")
        self.assertEqual(
            self.gateway_store.get(pending_action_id=pending_id).status, PendingActionStatus.PENDING
        )
        self.assertEqual([r[0] for r in self.ledger(pending_id)], ["prepared"])
        self.assertEqual(
            self.sql("SELECT stage FROM admin_action_followup"), [("confirmation_card_send",)]
        )
        cancelled = self.gateway_store.cancel(pending_action_id=pending_id, clicker_open_id=ADMIN)
        self.assertTrue(cancelled.decision.ok)
        rows = self.ledger(pending_id)
        self.assertEqual([r[0] for r in rows], ["prepared", "cancelled"])
        self.assertEqual((rows[1][1], rows[1][6]), ("feishu_card", ADMIN))
        self.assertEqual(self.account_state(), "enabled")

    # ------------------------------------------------------------------ 场景 5
    def test_result_unknown_is_reported_honestly_and_never_called_effective(self):
        self.add_user()
        pending_id = self.prepare_suspend()["pending_action_id"]
        self.sql("UPDATE admin_action_followup SET status='unknown',result_code='card_unknown'")
        item = self.tool("get_pending_actions", {"pending_action_id": pending_id})["actions"][0]
        self.assertFalse(item["card_delivered"])
        self.assertEqual(
            [(f["stage"], f["status"]) for f in item["followups"]],
            [("confirmation_card_send", "unknown")],
        )
        self.assertIsNone(item["publish_state"])
        self.assertEqual(self.prepare_suspend()["pending_action_id"], pending_id)
        self.assertEqual(self.counts()["admin_action_followup"], 1)

        self.deliver(pending_id)
        self.assertTrue(
            self.gateway_store.confirm(
                pending_action_id=pending_id, clicker_open_id=ADMIN
            ).decision.ok
        )
        recompute = self.sql(
            "SELECT id FROM admin_action_followup WHERE pending_action_id=%s AND stage='permission_recompute'",
            (pending_id,),
        )[0][0]
        self.sql(
            "INSERT INTO admin_action_followup(id,pending_action_id,subject_key,stage,target_user_id,"
            "target_version,depends_on_id,status) VALUES('afu_observe',%s,'usr_target','publish_observe',"
            "'usr_target',2,%s,'retry_wait')",
            (pending_id, recompute),
        )
        item = self.tool("get_pending_actions", {"pending_action_id": pending_id})["actions"][0]
        self.assertEqual((item["status"], item["publish_state"]), ("executed", "in_progress"))
        self.sql("UPDATE admin_action_followup SET status='succeeded' WHERE id='afu_observe'")
        item = self.tool("get_pending_actions", {"pending_action_id": pending_id})["actions"][0]
        self.assertEqual(item["publish_state"], "published")

    # ------------------------------------------------------------------ gateway 发卡
    def consumer(self, send, create=None):
        dispatch = ConfirmationCardDispatch(
            pending_actions=self.gateway_store,
            admin=AdminConfirmationCard(
                store=self.followups,
                pending_actions=self.gateway_store,
                registry=PostgresAdminRegistryLookup(DSN),
                display_names=self.queries,
                create_card=create or Mock(return_value="card_new"),
                send_card=send,
            ),
        )
        self.sql("UPDATE admin_action_followup SET next_attempt_at=now()-interval '2 seconds'")
        return FollowupConsumer(
            store=self.followups,
            consumer_kind="postprocess",
            owner="synthetic-owner",
            handlers={"confirmation_card_send": dispatch},
            audit=self.audit,
        )

    def test_gateway_dispatch_sends_only_to_the_initiator_and_marks_delivery(self):
        self.add_user()
        pending_id = self.prepare_suspend()["pending_action_id"]
        send = Mock(return_value="om_1")
        create = Mock(return_value="card_new")
        consumer = self.consumer(send, create)
        self.assertTrue(consumer.run_once())
        self.assertFalse(consumer.run_once())
        self.assertEqual(send.call_args.kwargs["open_id"], ADMIN)
        self.assertEqual(
            send.call_args.kwargs["card"], {"type": "card", "data": {"card_id": "card_new"}}
        )
        payload = create.call_args.args[0]
        self.assertIn("停用", str(payload))
        self.assertNotIn(TARGET, str(payload))
        self.assertEqual(
            self.sql("SELECT card_delivered,card_id,status FROM pending_action"),
            [(True, "card_new", "pending")],
        )
        self.assertEqual(
            self.sql("SELECT status,external_ref FROM admin_action_followup"),
            [("succeeded", "om_1")],
        )
        stranger = self.gateway_store.confirm(
            pending_action_id=pending_id, clicker_open_id="ou_stranger"
        )
        self.assertEqual(stranger.decision.code, "not_authorized")
        self.assertTrue(
            self.gateway_store.confirm(
                pending_action_id=pending_id, clicker_open_id=ADMIN
            ).decision.ok
        )
        self.assertEqual(self.account_state(), "suspended")

    def test_gateway_dispatch_refuses_after_role_revocation_and_closes_on_definite_rejection(self):
        self.add_user()
        self.prepare_suspend()
        send = Mock(return_value="om_1")
        self.sql("UPDATE admin_registry SET entry_status='revoked', revoked_at=now()")
        self.consumer(send).run_once()
        send.assert_not_called()
        self.assertEqual(
            self.sql("SELECT status,result_code FROM admin_action_followup"),
            [("skipped", "not_authorized")],
        )
        self.assertEqual(self.sql("SELECT card_delivered FROM pending_action"), [(False,)])

        reset_production_rows(DSN)
        seed_admin_registry_entry(DSN, feishu_open_id=ADMIN, label="合成管理员")
        self.sql("INSERT INTO innertest_roster_version(scope,mode) VALUES('synthetic','database')")
        self.sql(
            "INSERT INTO innertest_admin_binding(id,scope,open_id,version,enabled) "
            "VALUES('binding','synthetic','ou_admin',1,true)"
        )
        self.add_user()
        self.prepare_suspend()
        self.consumer(Mock(side_effect=DefiniteRejectionError("rejected"))).run_once()
        self.assertEqual(
            self.sql("SELECT status,result_code FROM admin_action_followup"),
            [("failed", "card_failed")],
        )
        self.assertEqual(
            self.sql("SELECT status,card_delivered FROM pending_action"), [("failed", False)]
        )

        reset_production_rows(DSN)
        seed_admin_registry_entry(DSN, feishu_open_id=ADMIN, label="合成管理员")
        self.sql("INSERT INTO innertest_roster_version(scope,mode) VALUES('synthetic','database')")
        self.sql(
            "INSERT INTO innertest_admin_binding(id,scope,open_id,version,enabled) "
            "VALUES('binding','synthetic','ou_admin',1,true)"
        )
        self.add_user()
        self.prepare_suspend()
        send = Mock(side_effect=TimeoutError("synthetic"))
        self.consumer(send).run_once()
        self.assertEqual(self.sql("SELECT status FROM admin_action_followup"), [("unknown",)])
        self.assertFalse(self.consumer(send).run_once())
        self.assertEqual(send.call_count, 1)

    def test_gateway_dispatch_leaves_innertest_stage_pending_without_its_service(self):
        self.sql(
            "INSERT INTO pending_action(id,action_type,target_open_id,target_state_snapshot,"
            "initiated_by_open_id,confirm_deadline_at) VALUES('pac_innertest','innertest_additions',"
            "'ou_admin','1','ou_admin',now()+interval '10 minutes')"
        )
        self.sql(
            "INSERT INTO admin_action_followup(id,pending_action_id,subject_key,stage,batch_id) "
            "VALUES('afu_card','pac_innertest','ibt_1','confirmation_card_send','ibt_1')"
        )
        send = Mock()
        self.consumer(send).run_once()
        send.assert_not_called()
        # 外发阶段一旦被领取，消费者不把「没有处理器」当成可证明的未发送：状态由既有
        # 恢复规则决定，这里只钉住结果码与「零发送、待确认行原样」。
        self.assertEqual(
            self.sql("SELECT result_code FROM admin_action_followup"), [("handler_unavailable",)]
        )
        self.assertEqual(
            self.sql("SELECT status,card_delivered FROM pending_action"), [("pending", False)]
        )

    def test_read_only_projection_of_prepared_action_matches_the_tool_result(self):
        self.add_user()
        result = self.prepare_suspend()
        listed = self.tool("get_pending_actions", {})["actions"]
        self.assertEqual(listed[0]["pending_action_id"], result["pending_action_id"])
        self.assertEqual(listed[0]["followups"], result["action"]["followups"])
        self.assertEqual(
            set(READ_ONLY_TOOL_NAMES),
            {"get_user_status", "get_user_permission_sources", "get_pending_actions"},
        )
