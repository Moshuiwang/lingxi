"""真实两槽预算下的等待、停止、SQL 截止与 HTTP 释放边界。"""

import importlib.util
import json
import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import Mock, patch

from test_innertest_probe_assembly import MASTER_KEY, WIRED_ENV

from lingxi.adapters.innertest_probe import InnertestMcpTokens
from lingxi.adapters.mcp_token_cipher import McpTokenCipher
from lingxi.adapters.postgres import connect
from lingxi.adapters.query_mcp_probe import McpHttpResponse
from lingxi.apps.scheduler import SchedulerConfig
from lingxi.apps.scheduler.innertest import _build_probe
from lingxi.core.admin.followup_budget import FollowupDatabaseBudget
from lingxi.core.permission.mcp_readiness_base import McpProbeError


@contextmanager
def occupied(budget, count):
    release = threading.Event()
    ready = [threading.Event() for _ in range(count)]

    def hold(event):
        with budget:
            event.set()
            release.wait(5)

    threads = [threading.Thread(target=hold, args=(event,)) for event in ready]
    for thread in threads:
        thread.start()
    try:
        assert all(event.wait(2) for event in ready)
        yield
    finally:
        release.set()
        for thread in threads:
            thread.join(2)
            assert not thread.is_alive()


@unittest.skipUnless(importlib.util.find_spec("cryptography"), "需要 scheduler 加密依赖")
class ProbeBudgetTests(unittest.TestCase):
    def reader(self, budget, stop, timeout=5):
        return InnertestMcpTokens(
            "synthetic",
            cipher=McpTokenCipher(MASTER_KEY),
            db_slots=budget,
            should_stop=stop.is_set,
            timeout_seconds=timeout,
        )

    def test_two_occupied_slots_stop_waiter_without_database_or_leftover_thread(self):
        budget, stop = FollowupDatabaseBudget(), threading.Event()
        reader = self.reader(budget, stop)
        reader._read_cipher = Mock()
        with occupied(budget, 2), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(reader.read_token, "user")
            time.sleep(0.15)
            self.assertFalse(future.done())
            stop.set()
            with self.assertRaises(McpProbeError):
                future.result(timeout=0.5)
        reader._read_cipher.assert_not_called()

    def test_waiter_expires_even_when_slots_are_never_released(self):
        budget, stop = FollowupDatabaseBudget(), threading.Event()
        reader = self.reader(budget, stop, timeout=0.2)
        reader._read_cipher = Mock()
        with occupied(budget, 2), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(reader.read_token, "user")
            with self.assertRaises(McpProbeError):
                future.result(timeout=0.7)
        reader._read_cipher.assert_not_called()

    def test_database_uses_second_slot_but_http_wait_releases_it(self):
        budget, stop = FollowupDatabaseBudget(), threading.Event()
        db_entered, db_release, http_entered, http_release = [threading.Event() for _ in range(4)]

        def read(*args):
            db_entered.set()
            if not db_release.wait(2):
                raise TimeoutError("synthetic")
            return McpTokenCipher(MASTER_KEY).encrypt("synthetic")

        def http(method, url, *, body, **kwargs):
            http_entered.set()
            if not http_release.wait(2):
                raise TimeoutError("synthetic")
            return McpHttpResponse(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {
                                        "metrics": [
                                            {
                                                "metric_id": "a",
                                                "name": "合成",
                                                "name_en": "synthetic",
                                            }
                                        ]
                                    }
                                ),
                            }
                        ]
                    },
                },
            )

        with (
            occupied(budget, 1),
            patch.object(InnertestMcpTokens, "_read_cipher", side_effect=read),
            patch("lingxi.adapters.query_mcp_probe.urllib_mcp_transport", side_effect=http),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            probe = _build_probe(
                SchedulerConfig.from_env(WIRED_ENV), db_slots=budget, should_stop=stop.is_set
            )
            future = pool.submit(probe.list_metrics, user_id="user")
            try:
                self.assertTrue(db_entered.wait(1))
                available = budget.acquire(blocking=False)
                if available:
                    budget.release()
                self.assertFalse(available)
                db_release.set()
                self.assertTrue(http_entered.wait(1))
                self.assertTrue(budget.acquire(blocking=False))
                budget.release()
                http_release.set()
                self.assertEqual(future.result(timeout=1), 1)
            finally:
                db_release.set()
                http_release.set()

    @unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN"), "需独占合成 PostgreSQL")
    def test_blocked_real_query_obeys_remaining_deadline_and_returns_slot(self):
        from postgres_schema import ensure_production_schema
        from psycopg.errors import QueryCanceled

        dsn = os.environ["LINGXI_POSTGRES_DSN"]
        ensure_production_schema(dsn)
        budget, stop = FollowupDatabaseBudget(), threading.Event()
        reader = InnertestMcpTokens(
            dsn,
            cipher=McpTokenCipher(MASTER_KEY),
            db_slots=budget,
            should_stop=stop.is_set,
            timeout_seconds=0.3,
        )
        with occupied(budget, 1), connect(dsn) as locked, locked.cursor() as cursor:
            cursor.execute("LOCK TABLE mcp_access_token IN ACCESS EXCLUSIVE MODE")
            started = time.monotonic()
            with self.assertRaises(QueryCanceled):
                reader.read_token("synthetic")
            self.assertLess(time.monotonic() - started, 1.2)
            self.assertTrue(budget.acquire(blocking=False))
            budget.release()
