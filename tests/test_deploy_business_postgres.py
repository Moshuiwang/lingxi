"""固定部署探针与新迁移上的实际进程恢复；外部执行全部为合成替身。"""

import importlib
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_innertest_postgres as innertest_tests
from postgres_schema import ensure_production_schema, psycopg_available
from test_deploy_automation import fixture

from lingxi.adapters.innertest_handlers import InnertestFollowupHandlers
from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_admin_followup import PostgresFollowupStore
from lingxi.core.admin.followup_consumer import FollowupConsumer, FollowupResult

runtime_module = importlib.import_module("deploy_runtime")
state = importlib.import_module("deploy_state")
DSN = innertest_tests.DSN


def assert_installed_artifact():
    root = os.environ.get("LINGXI_TEST_INSTALLED_ROOT")
    if root:
        for name in (
            "lingxi.apps.innertest_status",
            "lingxi.adapters.innertest_socket",
            "lingxi.adapters.innertest_handlers",
            "lingxi.adapters.postgres_admin_followup",
            "lingxi.core.admin.followup_consumer",
        ):
            module = importlib.import_module(name)
            if not Path(module.__file__).resolve().is_relative_to(Path(root).resolve()):
                raise AssertionError("expected_frozen_installed_artifact")


def claim_then_wait(dsn, pipe):
    assert_installed_artifact()
    item = PostgresFollowupStore(dsn).claim_followup(
        consumer_kind="scheduler",
        owner="synthetic-crashed-owner",
        now=datetime.now(UTC),
        lease_seconds=1,
    )
    pipe.send(item.id if item else None)
    pipe.recv()


def recover_process(dsn, queue, external_unknown):
    assert_installed_artifact()
    calls = []

    class Runner:
        def start_system(self, **kwargs):
            calls.append(kwargs["email"])
            with connect(dsn) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO app_user(id,feishu_open_id,feishu_user_id,feishu_union_id,display_name,department,tenant_key,provisioning_state,permission_version) VALUES('usr_recovered','ou_person1','fs_recovered','un_recovered','合成','合成','synthetic','active',1)"
                )
                cursor.execute(
                    "INSERT INTO publish_outbox(id,user_id,permission_version,reason,payload,status,published_at) VALUES('pub_recovered','usr_recovered',1,'synthetic','{}','published',now())"
                )
            return SimpleNamespace(failure_reason=None)

    store = PostgresFollowupStore(dsn)
    if external_unknown:
        kind = "gateway"

        def unknown(_item):
            calls.append("synthetic_send")
            return FollowupResult("unknown", "synthetic_receipt_unknown")

        handlers = {"confirmation_card_send": unknown}
    else:
        kind = "scheduler"
        probe = Mock()
        probe.list_metrics.return_value = 1
        handler = InnertestFollowupHandlers(store=store, runner=Runner(), probe=probe)
        handlers = {
            name: handler.handle for name in ("innertest_preprovision", "innertest_readiness_check")
        }
    consumer = FollowupConsumer(
        store=store,
        consumer_kind=kind,
        owner="synthetic-recovered-process",
        handlers=handlers,
        audit=Mock(),
    )
    consumed = 0
    while consumer.run_once():
        consumed += 1
        if consumed > 8:
            raise RuntimeError("unexpected_unbounded_consumer")
    queue.put({"consumed": consumed, "calls": calls, "recovery": store.recovery_status()})


@unittest.skipUnless(DSN and psycopg_available(), "需要独占合成 PostgreSQL")
class DeploymentBusinessPostgresTests(unittest.TestCase):
    sql = innertest_tests.InnertestPostgresTests.sql
    prepare = innertest_tests.InnertestPostgresTests.prepare
    delivered = innertest_tests.InnertestPostgresTests.delivered
    confirm = innertest_tests.InnertestPostgresTests.confirm

    @classmethod
    def setUpClass(cls):
        assert_installed_artifact()
        ensure_production_schema(DSN)

    def setUp(self):
        innertest_tests.InnertestPostgresTests.setUp(self)

    def cli(self):
        environment = dict(
            os.environ,
            LINGXI_POSTGRES_DSN=DSN,
            LINGXI_INNERTEST_SCOPE="synthetic",
            LINGXI_INNERTEST_BINDING_ID="binding",
        )
        result = subprocess.run(
            [sys.executable, "-B", "-m", "lingxi.apps.innertest_status"],
            capture_output=True,
            text=True,
            env=environment,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for forbidden in ("ou_admin", "person1@example.test", DSN):
            self.assertNotIn(forbidden, result.stdout)
        return json.loads(result.stdout)

    def child(self, unknown=False):
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        process = context.Process(target=recover_process, args=(DSN, queue, unknown))
        try:
            process.start()
            result = queue.get(timeout=20)
            process.join(5)
            self.assertEqual(process.exitcode, 0)
            return result
        finally:
            if process.is_alive():
                process.kill()
                process.join()
            queue.close()
            queue.join_thread()

    def test_actual_cli_is_readonly_and_enforces_real_binding_version(self):
        before = self.sql("SELECT version,enabled FROM innertest_admin_binding")
        response = self.cli()
        self.assertEqual(
            response["binding"],
            {"binding_id": "binding", "version": 1, "enabled": True, "authorized": True},
        )
        self.assertEqual(self.sql("SELECT version,enabled FROM innertest_admin_binding"), before)
        with tempfile.TemporaryDirectory() as tmp:
            plan, host, config, approval = fixture(Path(tmp))
            config["values"].update(
                LINGXI_INNERTEST_SCOPE="synthetic", LINGXI_INNERTEST_BINDING_ID="binding"
            )
            runtime = runtime_module.Runtime(host, config)

            def invoke(*args, **kwargs):
                self.assertEqual(
                    args,
                    ("exec", "synthetic-container", "python", "-m", "lingxi.apps.innertest_status"),
                )
                return 0, json.dumps(self.cli())

            with patch.object(runtime, "docker", side_effect=invoke):
                actual = runtime.business_status(
                    plan, {"scheduler": {"id": "synthetic-container", "running": True}}
                )
                self.assertEqual(actual["roster"]["mode"], "database")
                legacy = dict(plan, new=dict(plan["new"], schema=1))
                with self.assertRaisesRegex(state.DeployError, "cannot_consume_business_state"):
                    runtime.business_status(
                        legacy, {"scheduler": {"id": "synthetic-container", "running": True}}
                    )
                self.sql("UPDATE innertest_admin_binding SET version=version+1")
                with self.assertRaisesRegex(state.DeployError, "binding_incompatible"):
                    runtime.business_status(
                        plan, {"scheduler": {"id": "synthetic-container", "running": True}}
                    )

    def test_confirmed_dynamic_work_is_consumed_by_new_process_once(self):
        batch = self.prepare()
        self.delivered(batch)
        self.assertTrue(self.confirm(batch).decision.ok)
        self.sql("UPDATE admin_action_followup SET next_attempt_at=now()-interval '2 seconds'")
        before = self.cli()
        self.assertTrue(before["roster"]["requires_dynamic_roster"])
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(target=claim_then_wait, args=(DSN, child))
        try:
            process.start()
            self.assertTrue(parent.poll(10))
            self.assertIsNotNone(parent.recv())
            self.assertEqual(self.cli()["followups"]["inflight"], 1)
            process.kill()
            process.join(5)
            self.assertFalse(process.is_alive())
        finally:
            if process.is_alive():
                process.kill()
                process.join()
            parent.close()
            child.close()
        time.sleep(1.1)
        first = self.child()
        second = self.child()
        self.assertEqual(first["consumed"], 2)
        self.assertEqual(first["calls"], ["person1@example.test"])
        self.assertEqual(second["consumed"], 0)
        self.assertEqual(second["calls"], [])
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_membership"), [(1,)])
        self.assertEqual(
            self.sql(
                "SELECT status FROM admin_action_followup WHERE stage IN ('innertest_preprovision','innertest_readiness_check') ORDER BY stage"
            ),
            [("succeeded",), ("succeeded",)],
        )
        self.assertEqual(self.sql("SELECT result_code FROM innertest_check"), [("check_passed",)])
        self.assertEqual(self.cli()["roster"]["version"], before["roster"]["version"])

    def test_unknown_external_result_survives_new_process_without_resend(self):
        self.prepare()
        self.sql("UPDATE admin_action_followup SET next_attempt_at=now()-interval '2 seconds'")
        first = self.child(unknown=True)
        second = self.child(unknown=True)
        self.assertEqual(first["calls"], ["synthetic_send"])
        self.assertEqual(second["calls"], [])
        self.assertEqual(self.cli()["followups"]["unknown"], 1)
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_membership"), [(0,)])
