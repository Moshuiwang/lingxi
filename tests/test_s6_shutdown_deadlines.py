"""实际进程收到重复SIGTERM后的20秒/120秒绝对截止与持久恢复。"""

import multiprocessing
import os
import signal
import sys
import time
import unittest
from datetime import UTC, datetime, timedelta

import test_admin_followup_postgres as fixture_module
from postgres_schema import ensure_production_schema, psycopg_available
from s6_shutdown_process import shutdown_process

from lingxi.adapters.postgres import connect

DSN = os.environ.get("LINGXI_POSTGRES_DSN")


@unittest.skipUnless(DSN and psycopg_available() and sys.platform == "linux", "需要独占Linux合成库")
class ShutdownDeadlineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_production_schema(DSN)

    def test_actual_gateway_and_scheduler_absolute_deadlines(self):
        for kind, budget in (("gateway", 20), ("scheduler", 120)):
            with self.subTest(kind=kind):
                fixture = fixture_module.FollowupPostgresTests()
                fixture.setUp()
                if kind == "gateway":
                    fixture.add("group_notify")
                else:
                    fixture.add(
                        "innertest_preprovision", subject_key="s6-item", batch_item_id="s6-item"
                    )
                with connect(DSN) as conn, conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE admin_action_followup SET next_attempt_at=now()-interval '1 second'"
                    )
                context = multiprocessing.get_context("spawn")
                parent, child = context.Pipe()
                process = context.Process(target=shutdown_process, args=(DSN, kind, child))
                try:
                    process.start()
                    self.assertTrue(parent.poll(10))
                    ready = parent.recv()
                    self.assertEqual(ready["state"], "ready")
                    started = time.monotonic()
                    os.kill(process.pid, signal.SIGTERM)
                    time.sleep(0.03)
                    os.kill(process.pid, signal.SIGTERM)
                    self.assertTrue(parent.poll(budget + 8))
                    result = parent.recv()
                    process.join(5)
                    elapsed = time.monotonic() - started
                    self.assertEqual(process.exitcode, 0)
                    self.assertFalse(process.is_alive())
                    self.assertTrue(result["deadline_unchanged"])
                    self.assertTrue(result["socket_absent"])
                    self.assertGreaterEqual(result["still_running"], 3 if kind == "gateway" else 4)
                    self.assertGreaterEqual(elapsed, budget - 0.5)
                    self.assertLess(elapsed, budget + 4)
                    self.assertFalse(os.path.exists(f"/proc/{process.pid}"))
                    counts = fixture.store.recover_expired(
                        now=datetime.now(UTC) + timedelta(seconds=121)
                    )
                    self.assertEqual(counts.unknown if kind == "gateway" else counts.recoverable, 1)
                    print(
                        "S6_ACTUAL_DEADLINE",
                        kind,
                        dict(observed_elapsed=elapsed, **result),
                        flush=True,
                    )
                finally:
                    if process.is_alive():
                        process.kill()
                        process.join(5)
                    parent.close()
                    child.close()
