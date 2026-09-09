"""真实定位快照在准备后变化，确认先完整回滚再给可重做的明确终态。"""

from unittest.mock import patch

from test_innertest_postgres import DSN, InnertestPostgresTests

from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_innertest_locator import locate_transaction_email
from lingxi.core.admin.innertest import InnertestError
from lingxi.core.admin.pending_action import ConfirmResultKind, PendingActionStatus


class InnertestTargetDriftTests(InnertestPostgresTests):
    def install_directory(self):
        with connect(DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO roster_snapshot(id,captured_at,row_count,pages_read) VALUES('roster',now(),2,1)"
            )
            cursor.execute(
                "INSERT INTO feishu_org_sync_run(id,source_app_id,status,expires_at) VALUES('org','synthetic','staging',now())"
            )
            cursor.execute(
                "INSERT INTO feishu_org_tenant_snapshot(id,sync_run_id,tenant_key,visible_to_user_identity,member_count) VALUES('tenant','org','synthetic',true,2)"
            )
            for number in (1, 2):
                person = f"person{number}"
                cursor.execute(
                    "INSERT INTO roster_snapshot_row(snapshot_id,row_index,personnel_id,email,name,employee_no,record_id) VALUES('roster',%s,%s,%s,'合成','synthetic',%s)",
                    (number, person, person + "@example.test", person),
                )
                cursor.execute(
                    "INSERT INTO feishu_org_member_snapshot(id,sync_run_id,tenant_key,member_key,open_id,user_id,union_id,display_name) VALUES(%s,'org','synthetic',%s,%s,%s,%s,'合成')",
                    (person, person, "ou_" + person, person, "un_" + person),
                )
            cursor.execute(
                "UPDATE feishu_org_sync_run SET status='complete',completed_at=now(),tenant_count=1,member_count=2 WHERE id='org'"
            )
        self.service.locator = locate_transaction_email

    def prepare_then_change_second_target(self):
        self.install_directory()
        batch = self.prepare(["person1@example.test", "person2@example.test"])
        self.delivered(batch)
        # 固定后一个才漂移，必须先撤销已经写入的第一个人的资格与两阶段。
        second = self.sql("SELECT email FROM innertest_batch_item ORDER BY id DESC LIMIT 1")[0][0]
        personnel = second.split("@")[0]
        self.sql(
            "UPDATE feishu_org_member_snapshot SET open_id='ou_replacement' WHERE user_id=%s",
            (personnel,),
        )
        return batch

    def assert_zero_business_writes(self):
        for table in (
            "innertest_membership",
            "local_permission_override",
            "publish_outbox",
            "app_user",
        ):
            self.assertEqual(self.sql("SELECT count(*) FROM " + table), [(0,)])
        self.assertEqual(
            self.sql("SELECT count(*) FROM admin_action_followup WHERE stage LIKE 'innertest_%%'"),
            [(0,)],
        )
        self.assertEqual(self.sql("SELECT version FROM innertest_roster_version"), [(0,)])
        self.assertEqual(
            self.sql("SELECT DISTINCT result_code FROM innertest_batch_item"), [("new",)]
        )

    def test_real_directory_target_drift_rolls_back_earlier_item_and_allows_reprepare(self):
        batch = self.prepare_then_change_second_target()
        original = self.pending._enqueue_person
        with patch.object(self.pending, "_enqueue_person", wraps=original) as enqueue:
            result = self.confirm(batch)
        self.assertEqual(enqueue.call_count, 1)
        self.assertEqual(result.decision.kind, ConfirmResultKind.TARGET_DRIFTED)
        self.assertEqual(result.decision.terminal_status, PendingActionStatus.FAILED)
        self.assertIn("重新查询后准备", result.decision.message)
        self.assertEqual(result.pending.status, PendingActionStatus.FAILED)
        self.assertEqual(
            self.sql("SELECT status,reason FROM pending_action"), [("failed", "target_drifted")]
        )
        self.assertEqual(
            self.service.get_batch(self.principal, batch_id=batch["batch_id"])["state"], "failed"
        )
        self.assert_zero_business_writes()
        self.assertEqual(
            self.sql(
                "SELECT count(*) FROM admin_action_followup WHERE stage='terminal_card_refresh'"
            ),
            [(1,)],
        )
        self.assertEqual(self.confirm(batch).decision.kind, ConfirmResultKind.ALREADY_TERMINAL)
        fresh = self.prepare(["person1@example.test", "person2@example.test"], key="reprepare")
        self.assertNotEqual(fresh["batch_id"], batch["batch_id"])
        self.assertEqual(fresh["state"], "pending")
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_membership"), [(0,)])

    def test_unavailable_directory_and_audit_failure_do_not_claim_target_drift(self):
        batch = self.prepare_then_change_second_target()
        with patch.object(self.service, "_audit", side_effect=InnertestError("audit_unavailable")):
            with self.assertRaisesRegex(InnertestError, "audit_unavailable"):
                self.confirm(batch)
        self.assert_zero_business_writes()
        self.assertEqual(self.sql("SELECT status FROM pending_action"), [("pending",)])
        with patch.object(
            self.service, "locator", side_effect=InnertestError("roster_unavailable")
        ):
            with self.assertRaisesRegex(InnertestError, "roster_unavailable"):
                self.confirm(batch)
        self.assertEqual(self.sql("SELECT status FROM pending_action"), [("pending",)])
        self.assertEqual(
            self.sql(
                "SELECT count(*) FROM admin_action_followup WHERE stage='terminal_card_refresh'"
            ),
            [(0,)],
        )
