"""实际scheduler装配中同时运行扩员、首聊、listener、组织、日报和续期。"""

import hashlib
import json
import multiprocessing
import os
import resource
import sys
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_innertest_postgres as fixtures
from postgres_schema import ensure_production_schema, psycopg_available
from s6_process_fixtures import snapshot_batch
from s6_resource_fixtures import ObservedBudget, SyntheticRunner, socket_clients
from test_daily_report_duty import FakeSource
from test_scheduler_process import FakeAuthorization, FakeVault, credential, replacement_grant

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, close_idle_connections
from lingxi.adapters.postgres_conversation import PostgresGatewayStore
from lingxi.adapters.postgres_daily_report_watermark import PostgresDailyReportWatermark
from lingxi.adapters.postgres_identity import PostgresOrgSnapshotStore
from lingxi.apps.scheduler.credential_rotation import CredentialRotationLoop
from lingxi.apps.scheduler.daily_report import DailyReportDuty
from lingxi.apps.scheduler.innertest import wire_innertest
from lingxi.apps.scheduler.loop import SchedulerLoop
from lingxi.apps.scheduler.onboarding import OnboardingExecutor
from lingxi.apps.scheduler.org_snapshot_sync import OrgSnapshotSyncDuty
from lingxi.core.conversation.onboarding_recovery import OnboardingReconciler

DSN = fixtures.DSN


@unittest.skipUnless(
    DSN and psycopg_available() and sys.platform == "linux" and os.geteuid() == 0,
    "需独占root Linux与合成PostgreSQL",
)
class JointResourcesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_production_schema(DSN)

    def test_twenty_people_and_first_chat_share_real_scheduler_resources(self):
        fixture = fixtures.InnertestPostgresTests()
        fixture.setUp()
        batch = fixture.prepare([f"person{i}@example.test" for i in range(20)])
        fixture.delivered(batch)
        self.assertTrue(fixture.confirm(batch).decision.ok)
        fixture.sql("UPDATE admin_action_followup SET next_attempt_at=now()-interval '1 second'")
        fixture.sql(
            "INSERT INTO inbound_event(feishu_event_id,event_type,user_open_id,trace_id,received_at,handled_as) VALUES('s6-first','im.message.receive_v1','ou_first','trc_first',now()-interval '1 minute','auto_provisioning')"
        )
        stop = threading.Event()
        budget = ObservedBudget()
        executor = OnboardingExecutor(workers=1, backlog=4, should_stop=stop.is_set)
        runner = SyntheticRunner(fixture, executor, budget)
        onboarding = OnboardingReconciler(
            store=PostgresGatewayStore(DSN),
            onboarding=runner,
            audit=Mock(),
            stale_after=timedelta(seconds=0),
            min_interval_seconds=0,
            should_stop=stop.is_set,
            capacity=executor.free_slots,
        )
        onboarding.onboarding_runner = runner
        onboarding.onboarding_executor = executor
        org = OrgSnapshotSyncDuty(
            read_snapshot=lambda: snapshot_batch("joint", 2),
            store=PostgresOrgSnapshotStore(DSN),
            audit=Mock(),
            source_app_id="synthetic",
            stop=stop,
            round_join_timeout_seconds=0.01,
        )
        daily = DailyReportDuty(
            source=FakeSource(),
            watermark=PostgresDailyReportWatermark(DSN),
            sender=Mock(),
            audit=Mock(),
            chat_id="s6-joint",
            stop=stop,
            aggregation_join_timeout_seconds=0.01,
        )
        vault = FakeVault([credential()])
        rotation = CredentialRotationLoop(
            vault=vault, authorization=FakeAuthorization(replacement_grant()), stop=stop
        )
        ticks = []
        loop = SchedulerLoop(
            duties=(onboarding, org, daily, rotation),
            stop=stop,
            interval_seconds=0.01,
            heartbeat=lambda: ticks.append(time.monotonic()),
        )
        loop.followup_db_slots = budget
        baseline_threads = threading.active_count()
        rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_threads = baseline_threads
        peak_fds = len(os.listdir("/proc/self/fd"))
        peer = None
        parent = None
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="s6-joint-", dir="/opt") as directory:
            root = Path(directory)
            os.chown(root, 0, 1234)
            os.chmod(root, 0o750)
            binding = root / "binding.json"
            binding.write_text(
                json.dumps(
                    dict(
                        schema_revision=1,
                        binding_id="binding",
                        host_uid=1234,
                        peer_uid=1234,
                        socket_gid=1234,
                        uid_map_sha256=hashlib.sha256(
                            Path("/proc/self/uid_map").read_bytes()
                        ).hexdigest(),
                    )
                )
            )
            path = str(root / "mcp.sock")
            config = SimpleNamespace(
                innertest_scope="synthetic",
                innertest_binding_path=binding,
                innertest_socket_path=path,
                postgres_dsn=DSN,
                postgres_timeouts=DEFAULT_POSTGRES_TIMEOUTS,
            )
            probe = Mock()
            probe.list_metrics.return_value = 1
            executor.start()
            try:
                with (
                    patch(
                        "lingxi.apps.innertest.build_innertest_service",
                        return_value=fixture.service,
                    ),
                    patch("lingxi.apps.scheduler.innertest._build_probe", return_value=probe),
                ):
                    wire_innertest(config, loop=loop, duties=loop.duties, audit=fixture.audit)
                context = multiprocessing.get_context("spawn")
                parent, child = context.Pipe()
                peer = context.Process(target=socket_clients, args=(path, batch["batch_id"], child))
                peer.start()
                self.assertTrue(parent.poll(10))
                self.assertEqual(parent.recv(), "ready")
                while time.monotonic() - started < 65:
                    loop.run_once()
                    peak_threads = max(peak_threads, threading.active_count())
                    peak_fds = max(peak_fds, len(os.listdir("/proc/self/fd")))
                    done = fixture.sql(
                        "SELECT count(*) FROM admin_action_followup WHERE stage LIKE 'innertest_%%' AND status='succeeded'"
                    )[0][0]
                    if done == 40:
                        break
                    time.sleep(0.03)
                self.assertEqual(done, 40)
                parent.send("stop")
                self.assertTrue(parent.poll(5))
                requests = parent.recv()
                peer.join(5)
                self.assertEqual(peer.exitcode, 0)
                accepted = next(
                    x
                    for x in loop.lifecycle._objects
                    if hasattr(x, "_accepted") and hasattr(x, "path")
                )._accepted
                stop_started = time.monotonic()
                loop.request_stop()
                report = loop.drain_until()
                self.assertEqual(report.still_running, 0)
                self.assertLess(time.monotonic() - stop_started, 120)
                self.assertFalse(Path(path).exists())
                self.assertFalse(executor.alive)
                fixture.sql(
                    "INSERT INTO inbound_event(feishu_event_id,event_type,user_open_id,trace_id,received_at,handled_as) VALUES('s6-after-stop','im.message.receive_v1','ou_after','trc_after',now()-interval '1 minute','auto_provisioning')"
                )
                loop.run_once()
                self.assertEqual(
                    fixture.sql(
                        "SELECT onboarding_dispatched_at FROM inbound_event WHERE feishu_event_id='s6-after-stop'"
                    ),
                    [(None,)],
                )
                consumer = next(
                    x for x in loop.lifecycle._objects if getattr(x, "kind", None) == "scheduler"
                )
                fixture.sql(
                    "UPDATE admin_action_followup SET status='pending',next_attempt_at=now()-interval '1 second' WHERE stage='innertest_preprovision'"
                )
                self.assertFalse(consumer.run_once())
                self.assertEqual(
                    fixture.sql(
                        "SELECT count(*) FROM admin_action_followup WHERE stage='innertest_preprovision' AND status='running'"
                    ),
                    [(0,)],
                )
                self.assertEqual(budget.active, 0)
                self.assertLessEqual(budget.peak, 2)
                self.assertLessEqual(budget.listener_peak, 1)
                self.assertEqual(len(runner.system_calls), 20)
                self.assertEqual(len(runner.normal_calls), 1)
                self.assertEqual(
                    len({x[1] for x in runner.system_calls} | {x[2] for x in runner.normal_calls}),
                    1,
                )
                self.assertEqual(len(vault.saved), 1)
                self.assertLess(max(b - a for a, b in zip(ticks, ticks[1:])), 1)
                self.assertLessEqual(peak_threads - baseline_threads, 6)
                self.assertLess(
                    resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - rss_before, 65536
                )
                print(
                    "S6_JOINT_RESOURCES",
                    json.dumps(
                        dict(
                            seconds=time.monotonic() - started,
                            added_threads=peak_threads - baseline_threads,
                            db_slots_peak=budget.peak,
                            listener_slots_peak=budget.listener_peak,
                            rss_before_kib=rss_before,
                            rss_peak_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                            fd_peak=peak_fds,
                            rpc_requests=requests,
                            accepted=accepted,
                            scheduler_max_gap=max(b - a for a, b in zip(ticks, ticks[1:])),
                        )
                    ),
                )
            finally:
                stop.set()
                loop.drain_until(time.monotonic() + 5)
                if peer is not None and peer.is_alive():
                    peer.kill()
                    peer.join(5)
                if parent is not None:
                    parent.close()
                close_idle_connections()
        self.assertEqual(threading.active_count(), baseline_threads)
        self.assertEqual(
            fixture.sql(
                "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND backend_type='client backend' AND pid<>pg_backend_pid()"
            ),
            [(0,)],
        )
        close_idle_connections()
