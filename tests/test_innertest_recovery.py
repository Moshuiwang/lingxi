"""实际子进程中断、并发确认与有限资源验证；全部合成数据。"""

import multiprocessing
import resource
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

from test_innertest_postgres import DSN, InnertestPostgresTests, locate

from lingxi.adapters.innertest_handlers import InnertestFollowupHandlers
from lingxi.adapters.postgres_admin_followup import PostgresFollowupStore
from lingxi.adapters.postgres_innertest import PostgresInnertestService
from lingxi.adapters.postgres_innertest_confirmation import InnertestPendingActions
from lingxi.adapters.postgres_pending_action import PostgresPendingActionStore
from lingxi.apps.scheduler.loop import SchedulerLoop
from lingxi.core.admin.followup_consumer import FollowupConsumer


def interrupted_confirm(dsn, action, connection, committed):
    class Audit:
        def record(self, action, **_fields):
            if action == "innertest.executed" and not committed:
                connection.send("before_commit")
                connection.recv()

    audit = Audit()
    service = PostgresInnertestService(
        dsn,
        scope="synthetic",
        binding=SimpleNamespace(binding_id="binding", peer_uid=1234),
        locator=locate,
        audit=audit,
    )
    pending = InnertestPendingActions(
        PostgresPendingActionStore(dsn, audit=audit, metric_map_path=None, durable_followups=True),
        service,
    )
    result = pending.confirm(pending_action_id=action, clicker_open_id="ou_admin")
    connection.send(("after_commit", result.decision.ok))
    connection.recv()


class InnertestRecoveryTests(InnertestPostgresTests):
    def test_process_killed_before_and_after_commit(self):
        batch = self.prepare()
        self.delivered(batch)
        context = multiprocessing.get_context("spawn")
        for committed in (False, True):
            parent, child = context.Pipe()
            proc = context.Process(
                target=interrupted_confirm, args=(DSN, batch["pending_action_id"], child, committed)
            )
            try:
                proc.start()
                self.assertTrue(parent.poll(10))
                self.assertEqual(
                    parent.recv(), ("after_commit", True) if committed else "before_commit"
                )
                proc.kill()
                proc.join(5)
                self.assertFalse(proc.is_alive())
                self.assertEqual(
                    self.sql("SELECT count(*) FROM innertest_membership"),
                    [(1 if committed else 0,)],
                )
                self.assertEqual(
                    self.sql(
                        "SELECT count(*) FROM admin_action_followup WHERE stage='innertest_preprovision'"
                    ),
                    [(1 if committed else 0,)],
                )
            finally:
                if proc.is_alive():
                    proc.kill()
                    proc.join()
                parent.close()
                child.close()
        self.assertEqual(
            self.service.get_batch(self.principal, batch_id=batch["batch_id"])["state"], "executed"
        )

    def test_same_version_concurrent_confirmation_only_once(self):
        batch = self.prepare()
        self.delivered(batch)
        barrier = threading.Barrier(2)

        def confirm():
            barrier.wait(5)
            return self.confirm(batch).decision.ok

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _n: confirm(), range(2)))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(self.sql("SELECT version FROM innertest_roster_version"), [(1,)])
        self.assertEqual(
            self.sql(
                "SELECT count(*) FROM admin_action_followup WHERE stage='innertest_preprovision'"
            ),
            [(1,)],
        )

    def test_changed_target_snapshot_and_unknown_new_key_rejected(self):
        batch = self.prepare()
        self.delivered(batch)
        self.sql(
            "UPDATE innertest_batch_item SET email='person2@example.test',open_id='ou_person2',personnel_id='person2'"
        )
        self.assertFalse(self.confirm(batch).decision.ok)
        self.assertEqual(self.sql("SELECT count(*) FROM innertest_membership"), [(0,)])

    def test_twenty_person_consumer_and_scheduler_remain_bounded(self):
        emails = [f"person{i}@example.test" for i in range(20)]
        batch = self.prepare(emails)
        self.delivered(batch)
        self.confirm(batch)
        self.sql("UPDATE admin_action_followup SET next_attempt_at=now()-interval '2 seconds'")
        outer = self

        class Runner:
            def start_system(self, *, email, trace_id, initiated_by_open_id):
                number = email.split("@")[0]
                outer.sql(
                    "INSERT INTO app_user(id,feishu_open_id,feishu_user_id,feishu_union_id,display_name,department,tenant_key,provisioning_state,permission_version) VALUES(%s,%s,%s,%s,'合成','合成','synthetic','active',1)",
                    ("usr_" + number, "ou_" + number, "fs_" + number, "un_" + number),
                )
                outer.sql(
                    "INSERT INTO publish_outbox(id,user_id,permission_version,reason,payload,status,published_at) VALUES(%s,%s,1,'synthetic','{}','published',now())",
                    ("pub_" + number, "usr_" + number),
                )
                return SimpleNamespace(failure_reason=None)

        ticks = []
        duty = Mock()
        duty.run_once.side_effect = lambda: ticks.append(time.monotonic())
        loop = SchedulerLoop(duties=(duty,), interval_seconds=0.01, stop=threading.Event())
        store = PostgresFollowupStore(DSN, db_slots=loop.followup_db_slots)
        probe = Mock()
        probe.list_metrics.return_value = 1
        handler = InnertestFollowupHandlers(store=store, runner=Runner(), probe=probe)
        consumer = FollowupConsumer(
            store=store,
            consumer_kind="scheduler",
            owner="resource",
            handlers={
                s: handler.handle for s in ("innertest_preprovision", "innertest_readiness_check")
            },
            audit=self.audit,
        )
        started = time.monotonic()
        baseline_threads = threading.active_count()
        baseline_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

        def drain():
            while consumer.run_once():
                pass

        thread = threading.Thread(target=drain)
        thread.start()
        peak = baseline_threads
        while thread.is_alive() and time.monotonic() - started < 15:
            loop.run_once()
            peak = max(peak, threading.active_count())
            thread.join(0.01)
        consumer.request_stop()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(probe.list_metrics.call_count, 20)
        self.assertLessEqual(peak - baseline_threads, 1)
        self.assertGreater(len(ticks), 1)
        self.assertLess(max(b - a for a, b in zip(ticks, ticks[1:])), 1)
        self.assertEqual(
            self.sql(
                "SELECT count(*) FROM admin_action_followup WHERE stage LIKE 'innertest_%%' AND status='succeeded'"
            ),
            [(40,)],
        )
        print(
            "SYNTHETIC_20_RESOURCE",
            dict(
                seconds=round(time.monotonic() - started, 3),
                threads_added=peak - baseline_threads,
                rss_before=baseline_rss,
                rss_after=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                ticks=len(ticks),
            ),
        )
