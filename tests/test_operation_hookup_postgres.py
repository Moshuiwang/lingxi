"""三件运营操作接入运营审计账 ``operation_audit`` 的真库断言：同一操作号串起各阶段、账里没有秘密。

有 ``LINGXI_POSTGRES_DSN`` 时在真实 PostgreSQL 上跑（建库走 ``postgres_schema`` 整链
前滚，与生产同源），否则明确跳过并写明原因。三件操作各自证明：

1. 预开通 ``--apply``：真实管理员闸 + 真实角色快照 + 真实账，「已准备」在前，逐人
   「已执行」各一行，末尾一行汇总，全部挂在同一个操作号；一个人的异常正文（含样本秘密）
   与邮箱都不在账里。
2. 欢迎卡 ``--apply``：同上，证据指向 ``outreach_message`` 的去重键。
3. 内测扩员 准备 → 确认 → 后台两阶段：同一批次号下「已准备」（受限通道）、「已确认」
   （确认卡、带确认者）、逐人两行「已执行」（后台阶段、各自的证据指针）；取消、旧批过期、
   确认卡过期三条拒绝分支各落一行；审计出口不可用时整笔回滚、账里零行；后台阶段写账失败
   只记日志、阶段结果不丢。

数据全部为虚构化名，不含任何真实人员数据。
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows
from test_innertest_postgres import locate
from test_outreach_ops import TOOL as OUTREACH
from test_outreach_ops import FakeDispatcher, _facts, _recipients
from test_preprovision_ops import COMPANY_MAP_TOML, ROLE_MAP_TOML
from test_preprovision_ops import TOOL as PREPROVISION

from lingxi.adapters.admin_registry import seed_admin_registry_entry
from lingxi.adapters.innertest_handlers import InnertestFollowupHandlers
from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_admin_followup import PostgresFollowupStore
from lingxi.adapters.postgres_innertest import PostgresInnertestService
from lingxi.adapters.postgres_innertest_confirmation import InnertestPendingActions
from lingxi.adapters.postgres_operation_audit import PostgresOperationAudit
from lingxi.adapters.postgres_pending_action import PostgresPendingActionStore
from lingxi.core.admin.followup_consumer import FollowupConsumer
from lingxi.core.admin.innertest import InnertestError
from lingxi.core.admin.registry import ALL_ADMIN_ROLES

DSN = os.environ.get("LINGXI_POSTGRES_DSN")
SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，三件操作接入运营审计账的真库断言未验证（需真实 PostgreSQL 16）"
    if not DSN
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，真库断言未验证"
)
ADMIN = "ou_admin"
SECRET = "hunter2"


class _Dispatcher:
    @staticmethod
    def run_once() -> None:
        return None


class _Alerting:
    dispatcher = _Dispatcher()


@unittest.skipUnless(DSN and psycopg_available(), SKIP_REASON)
class _RealLedgerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ensure_production_schema(DSN)

    def setUp(self) -> None:
        reset_production_rows(DSN)
        seed_admin_registry_entry(DSN, feishu_open_id=ADMIN, label="合成管理员")
        self.ledger = PostgresOperationAudit(DSN)

    def sql(self, text: str, args: tuple = ()) -> Any:
        with connect(DSN) as connection, connection.cursor() as cursor:
            cursor.execute(text, args)
            return cursor.fetchall() if cursor.description else None

    def assert_no_secret_or_person_data(self, *needles: str) -> None:
        for needle in (SECRET, *needles):
            with self.subTest(needle=needle):
                self.assertEqual(
                    self.sql(
                        "SELECT count(*) FROM operation_audit WHERE operation_audit::text ILIKE %s",
                        (f"%{needle}%",),
                    ),
                    [(0,)],
                )

    def patched(self, module: Any, name: str, value: Any) -> None:
        original = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(lambda: setattr(module, name, original))


class PreprovisionRealLedgerTest(_RealLedgerCase):
    """预开通脚本：真实闸门、真实角色快照、真实账。"""

    def _tempfile(self, text: str, suffix: str) -> str:
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=suffix, delete=False, encoding="utf-8", newline=""
        )
        handle.write(text)
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        return handle.name

    def test_apply_leaves_prepared_person_rows_and_a_summary_under_one_operation(self) -> None:
        class _Done:
            state = "completed"
            failure_reason = None

        def start_system(*, email: str, **_: Any) -> Any:
            if email == "c@d.com":
                raise RuntimeError(f"炸在 {email} password={SECRET}")
            return _Done()

        self.patched(PREPROVISION, "resolve_start_system", lambda dsn: (start_system, lambda: None))
        argv = [
            self._tempfile(
                "email,position,company_scope\na@b.com,A国家总经理,1011\nc@d.com,A国家财务总监,1012\n",
                ".csv",
            ),
            "--initiated-by",
            ADMIN,
            "--dsn",
            DSN,
            "--role-function-map",
            self._tempfile(ROLE_MAP_TOML, ".toml"),
            "--company-function-metric-map",
            self._tempfile(COMPANY_MAP_TOML, ".toml"),
            "--apply",
        ]
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = PREPROVISION.main(argv)

        self.assertEqual(code, 0)
        operation_ids = self.sql("SELECT DISTINCT operation_id FROM operation_audit")
        self.assertEqual(len(operation_ids), 1)
        rows = self.ledger.for_operation(operation_ids[0][0])
        self.assertEqual(
            [row.entry.phase.value for row in rows],
            ["prepared", "executed", "executed", "executed"],
        )
        prepared, first, second, summary = (row.entry for row in rows)
        self.assertEqual(prepared.operation, "preprovision.apply")
        self.assertEqual(prepared.entry_point.value, "ops_script")
        self.assertEqual(prepared.initiated_by, ADMIN)
        self.assertEqual(prepared.actor_roles, ALL_ADMIN_ROLES)
        self.assertEqual(prepared.target_count, 2)
        self.assertEqual(prepared.target_digest, PREPROVISION.roster_digest(["a@b.com", "c@d.com"]))
        self.assertEqual(
            (first.result_code, second.result_code), ("provisioned", "failed_RuntimeError")
        )
        self.assertTrue(first.evidence_ref.startswith("trace:"))
        self.assertEqual(first.evidence_ref, "trace:" + first.trace_id)
        self.assertEqual(summary.result_code, "partial")
        self.assertEqual(dict(summary.result_counts)["failed"], 1)
        self.assertIn(prepared.operation_id, out.getvalue())
        self.assert_no_secret_or_person_data("a@b.com", "c@d.com")


class OutreachRealLedgerTest(_RealLedgerCase):
    """欢迎卡脚本：真实闸门、真实角色快照、真实账。"""

    def test_apply_leaves_prepared_person_rows_and_a_summary_under_one_operation(self) -> None:
        recipients = _recipients(
            _facts(user_id="usr_joshua"),
            _facts("yiming.yi@example.invalid", user_id="usr_yiming", roster_names=("李四",)),
        )
        dispatcher = FakeDispatcher(errors={"李四": RuntimeError(f"token={SECRET}")})
        self.patched(OUTREACH, "_prepare", lambda arguments, dsn: (object(), recipients))
        self.patched(OUTREACH, "build_dispatcher", lambda *a, **k: (dispatcher, _Alerting()))
        self.patched(OUTREACH, "resolve_state_at_send", lambda dsn: None)

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = OUTREACH.main(["roster.csv", "--dsn", DSN, "--initiated-by", ADMIN, "--apply"])

        self.assertEqual(code, 0)
        self.assertEqual(len(dispatcher.calls), 2)
        operation_ids = self.sql("SELECT DISTINCT operation_id FROM operation_audit")
        self.assertEqual(len(operation_ids), 1)
        rows = self.ledger.for_operation(operation_ids[0][0])
        self.assertEqual(
            [row.entry.phase.value for row in rows],
            ["prepared", "executed", "executed", "executed"],
        )
        prepared, first, second, summary = (row.entry for row in rows)
        self.assertEqual(prepared.operation, "welcome_card.apply")
        self.assertEqual(prepared.target_kind, "user")
        self.assertEqual(prepared.actor_roles, ALL_ADMIN_ROLES)
        self.assertEqual(first.result_code, "delivered")
        self.assertEqual(first.target_user_id, "usr_joshua")
        self.assertEqual(first.evidence_ref, "outreach_message:outreach.welcome:apply:usr_joshua")
        self.assertEqual(second.result_code, "failed_RuntimeError")
        self.assertEqual(summary.result_code, "partial")
        self.assert_no_secret_or_person_data("example.invalid", "Joshua Wang", "王晋", "李四")


class InnertestRealLedgerTest(_RealLedgerCase):
    """扩员：准备、确认、后台两阶段与三条拒绝分支都落在同一批次号下。"""

    def setUp(self) -> None:
        super().setUp()
        self.sql("INSERT INTO innertest_roster_version(scope,mode) VALUES('synthetic','database')")
        self.sql(
            "INSERT INTO innertest_admin_binding(id,scope,open_id,version,enabled) "
            "VALUES('binding','synthetic',%s,1,true)",
            (ADMIN,),
        )
        self.audit = Mock()
        self.service = PostgresInnertestService(
            DSN,
            scope="synthetic",
            binding=SimpleNamespace(binding_id="binding", peer_uid=1234),
            locator=locate,
            audit=self.audit,
        )
        self.principal = self.service.authenticate(1234)
        original = PostgresPendingActionStore(
            DSN, audit=self.audit, metric_map_path=None, durable_followups=True
        )
        self.pending = InnertestPendingActions(original, self.service)

    def prepare(self, key: str = "same", emails: list[str] | None = None) -> Any:
        return self.service.prepare(
            self.principal, request_key=key, emails=emails or ["person1@example.test"]
        )

    def delivered(self, batch: Any) -> None:
        self.pending.mark_card_delivered(
            pending_action_id=batch["pending_action_id"], card_id="synthetic-card"
        )

    def confirm(self, batch: Any, now: datetime | None = None) -> Any:
        return self.pending.confirm(
            pending_action_id=batch["pending_action_id"], clicker_open_id=ADMIN, now=now
        )

    def user(self, number: str = "person1") -> None:
        self.sql(
            "INSERT INTO app_user(id,feishu_open_id,feishu_user_id,feishu_union_id,display_name,"
            "department,tenant_key,provisioning_state,permission_version) VALUES "
            "(%s,%s,%s,%s,'合成','合成','synthetic','active',1)",
            ("usr_" + number, "ou_" + number, "fs_" + number, "un_" + number),
        )
        self.sql(
            "INSERT INTO publish_outbox(id,user_id,permission_version,reason,payload,status,published_at) "
            "VALUES(%s,%s,1,'synthetic','{}','published',now())",
            ("pub_" + number, "usr_" + number),
        )

    def consumer(self, handler: Any) -> FollowupConsumer:
        self.sql("UPDATE admin_action_followup SET next_attempt_at=now()-interval '2 seconds'")
        return FollowupConsumer(
            store=PostgresFollowupStore(DSN),
            consumer_kind="scheduler",
            owner="synthetic-owner",
            handlers={s: handler for s in ("innertest_preprovision", "innertest_readiness_check")},
            audit=self.audit,
        )

    def handlers(self) -> InnertestFollowupHandlers:
        outer = self

        class Runner:
            def start_system(
                self, *, email: str, trace_id: str, initiated_by_open_id: str, expected_open_id=None
            ) -> Any:
                outer.user()
                return Mock(failure_reason=None)

        probe = Mock()
        probe.list_metrics.return_value = 2
        return InnertestFollowupHandlers(
            store=PostgresFollowupStore(DSN), runner=Runner(), probe=probe
        )

    def test_prepare_confirm_and_both_stages_share_the_batch_id(self) -> None:
        batch = self.prepare()
        self.delivered(batch)
        self.assertTrue(self.confirm(batch).decision.ok)
        consumer = self.consumer(self.handlers().handle)
        self.assertTrue(consumer.run_once())
        self.assertTrue(consumer.run_once())
        self.assertFalse(consumer.run_once())

        rows = self.ledger.for_operation(batch["batch_id"])
        self.assertEqual(
            [row.entry.phase.value for row in rows],
            ["prepared", "confirmed", "executed", "executed"],
        )
        prepared, confirmed, provisioned, checked = (row.entry for row in rows)
        self.assertEqual(
            {row.operation for row in (prepared, confirmed, provisioned, checked)},
            {"innertest.additions"},
        )
        self.assertEqual(prepared.entry_point.value, "restricted_channel")
        self.assertEqual(prepared.initiated_by, ADMIN)
        self.assertEqual(prepared.actor_roles, ALL_ADMIN_ROLES)
        self.assertEqual(prepared.target_kind, "batch")
        self.assertEqual(prepared.target_count, 1)
        self.assertRegex(prepared.target_digest, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(prepared.pending_action_id, batch["pending_action_id"])
        self.assertEqual(prepared.evidence_ref, "innertest_batch:" + batch["batch_id"])
        self.assertEqual(confirmed.entry_point.value, "feishu_card")
        self.assertEqual(confirmed.decided_by, ADMIN)
        self.assertEqual(confirmed.trace_id, prepared.trace_id)
        self.assertEqual(provisioned.entry_point.value, "scheduler_followup")
        self.assertEqual(provisioned.result_code, "succeeded:completed")
        self.assertEqual(provisioned.target_kind, "user")
        self.assertEqual(provisioned.target_user_id, "usr_person1")
        self.assertTrue(provisioned.executor.startswith("scheduler@"))
        self.assertTrue(provisioned.executor.endswith(":synthetic-owner"))
        self.assertEqual(
            provisioned.evidence_ref,
            "admin_action_followup:"
            + self.sql("SELECT id FROM admin_action_followup WHERE stage='innertest_preprovision'")[
                0
            ][0],
        )
        self.assertEqual(checked.result_code, "succeeded:check_passed")
        self.assertEqual(
            checked.evidence_ref,
            "innertest_check:" + self.sql("SELECT id FROM innertest_check")[0][0],
        )
        self.assertEqual(checked.trace_id, prepared.trace_id)
        # 窄表照旧双写，既有计数不受影响。
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_audit"), [(2,)])
        self.assert_no_secret_or_person_data("person1@example.test")

    def test_cancel_and_the_three_rejections_each_leave_one_row(self) -> None:
        cancelled = self.prepare(key="cancel")
        self.delivered(cancelled)
        self.pending.cancel(pending_action_id=cancelled["pending_action_id"], clicker_open_id=ADMIN)
        self.assertEqual(
            [
                (r.entry.phase.value, r.entry.decided_by)
                for r in self.ledger.for_operation(cancelled["batch_id"])
            ],
            [("prepared", None), ("cancelled", ADMIN)],
        )

        stale = self.prepare(key="stale")
        self.sql(
            "UPDATE pending_action SET confirm_deadline_at=now() - interval '1 second' WHERE id=%s",
            (stale["pending_action_id"],),
        )
        fresh = self.prepare(key="fresh")
        rows = self.ledger.for_operation(stale["batch_id"])
        self.assertEqual(
            [(r.entry.phase.value, r.entry.entry_point.value, r.entry.result_code) for r in rows],
            [
                ("prepared", "restricted_channel", None),
                ("rejected", "restricted_channel", "expired"),
            ],
        )

        self.delivered(fresh)
        outcome = self.confirm(fresh, now=datetime.now(UTC) + timedelta(hours=1))
        self.assertFalse(outcome.decision.ok)
        rows = self.ledger.for_operation(fresh["batch_id"])
        self.assertEqual(
            [(r.entry.phase.value, r.entry.entry_point.value, r.entry.result_code) for r in rows],
            [("prepared", "restricted_channel", None), ("rejected", "feishu_card", "expired")],
        )
        self.assertEqual(rows[1].entry.initiated_by, ADMIN)

    def test_an_unavailable_audit_sink_rolls_the_ledger_row_back_too(self) -> None:
        self.audit.record.side_effect = RuntimeError(f"audit {SECRET}")
        with self.assertRaisesRegex(InnertestError, "audit_unavailable"):
            self.prepare()
        self.assertEqual(self.sql("SELECT count(*) FROM operation_audit"), [(0,)])
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_batch"), [(0,)])

    def test_a_ledger_insert_failure_in_the_same_transaction_rolls_everything_back(self) -> None:
        with patch(
            "lingxi.adapters.postgres_innertest.record_operation_audit",
            side_effect=RuntimeError(f"ledger {SECRET}"),
        ):
            with self.assertRaisesRegex(InnertestError, "audit_unavailable"):
                self.prepare()
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_batch"), [(0,)])
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_audit"), [(0,)])
        self.assertEqual(self.sql("SELECT count(*) FROM pending_action"), [(0,)])

    def test_a_stage_ledger_failure_only_logs_and_keeps_the_stage_result(self) -> None:
        """后台阶段写账失败：结构化日志一条、阶段照常成功、检查行照常落下、账里没有秘密。"""

        batch = self.prepare()
        self.delivered(batch)
        self.confirm(batch)
        consumer = self.consumer(self.handlers().handle)
        with patch(
            "lingxi.adapters.innertest_handlers.record_operation_audit",
            side_effect=RuntimeError(f"ledger {SECRET}"),
        ):
            with self.assertLogs("lingxi.adapters.innertest_handlers", level="WARNING") as logs:
                self.assertTrue(consumer.run_once())
                self.assertTrue(consumer.run_once())

        self.assertTrue(any("operation_audit.write_failed" in line for line in logs.output))
        self.assertTrue(all(SECRET not in line for line in logs.output))
        self.assertEqual(
            self.sql(
                "SELECT status FROM admin_action_followup WHERE stage LIKE 'innertest_%%' ORDER BY stage"
            ),
            [("succeeded",), ("succeeded",)],
        )
        self.assertEqual(self.sql("SELECT result_code FROM innertest_check"), [("check_passed",)])
        self.assertEqual(
            [r.entry.phase.value for r in self.ledger.for_operation(batch["batch_id"])],
            ["prepared", "confirmed"],
        )
        self.assert_no_secret_or_person_data()


if __name__ == "__main__":
    unittest.main()
