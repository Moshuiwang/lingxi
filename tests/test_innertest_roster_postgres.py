"""受控名单导入工具与真实数据库的隔离验证：正例与五类拒绝各一条。"""

import io
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_innertest_roster import (
    PostgresInnertestRoster,
    RosterImportRejectedError,
    apply_roster_import,
    plan_roster_import,
    roster_import_status,
)
from lingxi.apps import innertest_roster
from lingxi.core.admin.innertest import InnertestError

DSN = os.environ.get("LINGXI_POSTGRES_DSN")
SCOPE = "synthetic"


@unittest.skipUnless(DSN and psycopg_available(), "需独占合成 PostgreSQL")
class _RosterImportPostgresBase(unittest.TestCase):
    """前置与助手；不含用例，避免被子类继承后整批重跑一遍真库。"""

    @classmethod
    def setUpClass(cls):
        ensure_production_schema(DSN)

    def setUp(self):
        reset_production_rows(DSN)

    def sql(self, text, args=()):
        with connect(DSN) as c, c.cursor() as cur:
            cur.execute(text, args)
            return cur.fetchall() if cur.description else None

    def seed_directory(self, members):
        """建一份组织快照与花名册快照。``members`` 是 (人员ID, 邮箱, open_id) 三元组。

        组织快照批次的完成态计数由一条延迟约束触发器在提交时核对声明与实际子行是否
        一致（``feishu_org_sync_run_verify_children``），因此批次行与其租户/成员子行
        必须在同一事务里一并提交，不能分成多条各自提交的语句。
        """
        now = datetime.now(UTC)
        with connect(DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO feishu_org_sync_run(id,source_app_id,status,completed_at,"
                "tenant_count,member_count) VALUES('run1','app','complete',%s,1,%s)",
                (now, len(members)),
            )
            cursor.execute(
                "INSERT INTO feishu_org_tenant_snapshot(id,sync_run_id,tenant_key,"
                "visible_to_user_identity) VALUES('tenant1','run1','t',true)"
            )
            for personnel_id, _email, open_id in members:
                cursor.execute(
                    "INSERT INTO feishu_org_member_snapshot(id,sync_run_id,tenant_key,"
                    "member_key,open_id,user_id,union_id,display_name) VALUES "
                    "(%s,'run1','t',%s,%s,%s,%s,'合成')",
                    (
                        "mem_" + personnel_id,
                        "mk_" + personnel_id,
                        open_id,
                        personnel_id,
                        "un_" + personnel_id,
                    ),
                )
        self.sql(
            "INSERT INTO roster_snapshot(id,captured_at,row_count,pages_read) "
            "VALUES('snap1',%s,%s,1)",
            (now, max(len(members), 1)),
        )
        for index, (personnel_id, email, _open_id) in enumerate(members):
            self.sql(
                "INSERT INTO roster_snapshot_row(snapshot_id,row_index,personnel_id,"
                "email,name,employee_no,record_id) VALUES('snap1',%s,%s,%s,'合成','emp','rec')",
                (index, personnel_id, email),
            )

    def plan(self, emails, gateway=("ou_x",), scheduler=("ou_x",)):
        return plan_roster_import(
            DSN, scope=SCOPE, gateway_legacy=gateway, scheduler_legacy=scheduler, emails=emails
        )

    def apply(self, emails, digest, gateway=("ou_x",), scheduler=("ou_x",), binding="bind1"):
        return apply_roster_import(
            DSN,
            scope=SCOPE,
            gateway_legacy=gateway,
            scheduler_legacy=scheduler,
            emails=emails,
            confirm_digest=digest,
            admin_open_id="ou_admin",
            binding_id=binding,
        )


class RosterImportPostgresTests(_RosterImportPostgresBase):
    """受控导入的正例与逐类拒绝。"""

    def test_plan_accepts_a_one_shot_iterator_without_dropping_the_second_pass(self):
        """``emails`` 只能遍历一次时（如生成器）不得让后一遍悄悄看到空集合。"""
        self.seed_directory([("p1", "a@x.test", "ou_p1"), ("p2", "b@x.test", "ou_p2")])
        plan = self.plan(iter(["a@x.test", "b@x.test"]))
        self.assertEqual(sorted(email for email, _ in plan.members), ["a@x.test", "b@x.test"])

    def test_plan_then_apply_then_status_round_trip(self):
        self.seed_directory([("p1", "a@x.test", "ou_p1"), ("p2", "b@x.test", "ou_p2")])
        plan = self.plan(["b@x.test", "a@x.test"])
        self.assertEqual(sorted(email for email, _ in plan.members), ["a@x.test", "b@x.test"])
        applied = self.apply(["b@x.test", "a@x.test"], plan.digest)
        self.assertEqual(applied.digest, plan.digest)

        status = roster_import_status(DSN, scope=SCOPE)
        self.assertEqual(status["mode"], "database")
        self.assertEqual(status["import_digest"], plan.digest)
        self.assertEqual(status["member_count"], 2)
        self.assertEqual(
            self.sql(
                "SELECT open_id,version,enabled FROM innertest_admin_binding WHERE scope=%s",
                (SCOPE,),
            ),
            [("ou_admin", 1, True)],
        )

    def test_switching_mode_back_to_legacy_falls_back_without_destroying_members(self):
        """把 ``mode`` 改回 ``legacy`` 后准入回落到静态名单，且已导入的成员行不被破坏。

        两半各测一件事：静态名单里的人命中、已导入但不在静态名单里的人不命中，
        证明回落真的换回了另一份名单；成员行与摘要仍在，证明回落不是删数据。

        回落必须是「换一个开关」而不是「删一批数据」——否则切回去就没法再切回来，
        制品先部署后切换的安全性也就不成立了。这里只读断言，不在真实环境执行。
        """
        self.seed_directory([("p1", "a@x.test", "ou_p1"), ("p2", "b@x.test", "ou_p2")])
        plan = self.plan(["a@x.test", "b@x.test"])
        self.apply(["a@x.test", "b@x.test"], plan.digest)
        self.assertEqual(roster_import_status(DSN, scope=SCOPE)["mode"], "database")

        self.sql("UPDATE innertest_roster_version SET mode='legacy' WHERE scope=%s", (SCOPE,))

        # 准入闸门是名单快照：静态模式下它不看已导入的成员行，只认静态名单。
        roster = PostgresInnertestRoster(DSN, scope=SCOPE, legacy=frozenset({"ou_legacy_only"}))
        version = roster_import_status(DSN, scope=SCOPE)["version"]
        self.assertEqual(roster.snapshot("ou_legacy_only"), (version, True))
        self.assertEqual(roster.snapshot("ou_p1"), (version, False))

        # 成员行与摘要都还在，切回 database 不需要重新导入。
        status = roster_import_status(DSN, scope=SCOPE)
        self.assertEqual(status["mode"], "legacy")
        self.assertEqual(status["member_count"], 2)
        self.assertEqual(status["import_digest"], plan.digest)

    def test_plan_rejects_mismatched_legacy_sources_without_writing(self):
        self.seed_directory([("p1", "a@x.test", "ou_p1")])
        with self.assertRaises(InnertestError) as ctx:
            self.plan(["a@x.test"], gateway=("ou_1",), scheduler=("ou_2",))
        self.assertEqual(ctx.exception.code, "legacy_roster_mismatch")
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_roster_version")[0][0], 0)

    def test_plan_rejects_unresolvable_email_without_writing(self):
        self.seed_directory([("p1", "a@x.test", "ou_p1")])
        with self.assertRaises(RosterImportRejectedError) as ctx:
            self.plan(["missing@x.test"])
        self.assertEqual(ctx.exception.code, "email_resolution_failed")
        self.assertEqual(ctx.exception.detail, (("missing@x.test", "email_not_in_roster"),))
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_roster_version")[0][0], 0)

    def test_plan_rejects_email_mapped_to_multiple_personnel_without_writing(self):
        self.seed_directory([("p1", "dup@x.test", "ou_p1"), ("p2", "dup@x.test", "ou_p2")])
        with self.assertRaises(RosterImportRejectedError) as ctx:
            self.plan(["dup@x.test"])
        self.assertEqual(ctx.exception.code, "email_resolution_failed")
        self.assertEqual(ctx.exception.detail, (("dup@x.test", "email_multiple_personnel"),))
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_membership")[0][0], 0)

    def test_apply_rejects_repeat_after_scope_already_database(self):
        self.seed_directory([("p1", "a@x.test", "ou_p1")])
        plan = self.plan(["a@x.test"])
        self.apply(["a@x.test"], plan.digest, binding="bind1")

        with self.assertRaises(RosterImportRejectedError) as ctx:
            self.apply(["a@x.test"], plan.digest, binding="bind2")
        self.assertEqual(ctx.exception.code, "scope_already_database")
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_admin_binding")[0][0], 1)
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_membership")[0][0], 1)

    def test_apply_rejects_when_scope_already_has_members(self):
        self.seed_directory([("p1", "a@x.test", "ou_p1")])
        self.sql(
            "INSERT INTO innertest_membership(scope,open_id,email) VALUES(%s,'ou_existing','existing@x.test')",
            (SCOPE,),
        )

        with self.assertRaises(RosterImportRejectedError) as ctx:
            self.apply(["a@x.test"], "irrelevant-digest")
        self.assertEqual(ctx.exception.code, "scope_already_has_members")
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_roster_version")[0][0], 0)
        self.assertEqual(
            self.sql("SELECT open_id FROM innertest_membership WHERE scope=%s", (SCOPE,)),
            [("ou_existing",)],
        )

    def test_apply_rejects_digest_mismatch_without_writing(self):
        self.seed_directory([("p1", "a@x.test", "ou_p1")])
        with self.assertRaises(RosterImportRejectedError) as ctx:
            self.apply(["a@x.test"], "wrong-digest")
        self.assertEqual(ctx.exception.code, "digest_mismatch")
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_roster_version")[0][0], 0)

    def test_apply_rejects_duplicate_email_in_list_without_writing(self):
        self.seed_directory([("p1", "a@x.test", "ou_p1")])
        with self.assertRaises(RosterImportRejectedError) as ctx:
            self.apply(["a@x.test", "A@X.TEST"], "any-digest")
        self.assertEqual(ctx.exception.code, "duplicate_email_in_list")
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_roster_version")[0][0], 0)


@unittest.skipUnless(DSN and psycopg_available(), "需独占合成 PostgreSQL")
class RosterImportCliDefaultWiringTests(_RosterImportPostgresBase):
    """走 ``run()`` 真正的默认装配（不注入假回调），核对三个工厂函数真的接得上。"""

    def write(self, directory, name, lines):
        path = Path(directory) / name
        path.write_text("\n".join(lines), encoding="utf-8")
        return str(path)

    def test_plan_apply_verify_round_trip_through_real_defaults(self):
        self.seed_directory([("p1", "a@x.test", "ou_p1")])
        self.sql(
            "INSERT INTO feishu_delegated_subject(purpose,subject_open_id) "
            "VALUES('org_directory_sync','ou_delegated')"
        )
        env = {innertest_roster.DSN_ENV_VAR: DSN}

        with tempfile.TemporaryDirectory() as tmp:
            gateway = self.write(tmp, "gateway.txt", ["ou_x"])
            scheduler = self.write(tmp, "scheduler.txt", ["ou_x"])
            emails = self.write(tmp, "emails.txt", ["a@x.test"])
            plan_out = io.StringIO()
            code = innertest_roster.run(
                [
                    "plan",
                    "--scope",
                    SCOPE,
                    "--gateway-legacy-file",
                    gateway,
                    "--scheduler-legacy-file",
                    scheduler,
                    "--emails-file",
                    emails,
                ],
                env=env,
                stdout=plan_out,
            )
            self.assertEqual(code, 0)
            digest_line = next(
                line for line in plan_out.getvalue().splitlines() if line.startswith("摘要：")
            )
            digest = digest_line.removeprefix("摘要：")

            apply_out = io.StringIO()
            code = innertest_roster.run(
                [
                    "apply",
                    "--scope",
                    SCOPE,
                    "--gateway-legacy-file",
                    gateway,
                    "--scheduler-legacy-file",
                    scheduler,
                    "--emails-file",
                    emails,
                    "--confirm-digest",
                    digest,
                ],
                env=env,
                stdout=apply_out,
            )
            self.assertEqual(code, 0)
            self.assertNotIn("ou_delegated", apply_out.getvalue())

            verify_out = io.StringIO()
            code = innertest_roster.run(["verify", "--scope", SCOPE], env=env, stdout=verify_out)
            self.assertEqual(code, 0)
            payload = json.loads(verify_out.getvalue())
            self.assertEqual(payload["mode"], "database")
            self.assertEqual(payload["import_digest"], digest)
            self.assertEqual(payload["member_count"], 1)

        self.assertEqual(
            self.sql("SELECT open_id FROM innertest_admin_binding WHERE scope=%s", (SCOPE,)),
            [("ou_delegated",)],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
