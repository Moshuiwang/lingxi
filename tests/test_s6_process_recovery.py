"""S6 用实际子进程强杀验证组织整批提交与日报持久去重。"""

import multiprocessing
import os
import unittest
from datetime import UTC, datetime, timedelta

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows
from s6_process_fixtures import report_process, snapshot_batch, snapshot_process

from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_daily_report_watermark import PostgresDailyReportWatermark
from lingxi.adapters.postgres_identity import PostgresOrgSnapshotStore

DSN = os.environ.get("LINGXI_POSTGRES_DSN")


@unittest.skipUnless(DSN and psycopg_available(), "需要独占合成PostgreSQL")
class ProcessRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_production_schema(DSN)

    def setUp(self):
        reset_production_rows(DSN)
        with connect(DSN) as conn, conn.cursor() as cursor:
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS s6_send_call(chat_id text,dedupe_key text);"
                "CREATE TABLE IF NOT EXISTS s6_platform_receipt(chat_id text,dedupe_key text,"
                "PRIMARY KEY(chat_id,dedupe_key));"
                "TRUNCATE s6_send_call,s6_platform_receipt"
            )

    def run_child(self, target, window, kill):
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(target=target, args=(DSN, child, window))
        try:
            process.start()
            self.assertTrue(parent.poll(15), "子进程未到达故障点")
            self.assertEqual(parent.recv(), window if kill else "completed")
            if kill:
                process.kill()
            process.join(5)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, -9 if kill else 0)
        finally:
            if process.is_alive():
                process.kill()
                process.join(5)
            parent.close()
            child.close()

    def query(self, sql):
        with connect(DSN) as conn, conn.cursor() as cursor:
            cursor.execute(sql)
            return cursor.fetchall()

    def test_snapshot_process_windows_keep_only_complete_batches(self):
        for window in ("during_read", "before_commit", "after_commit"):
            with self.subTest(window=window):
                reset_production_rows(DSN)
                store = PostgresOrgSnapshotStore(DSN)
                store.commit_batch(
                    snapshot_batch("old", 1),
                    source_app_id="synthetic",
                    started_at=datetime.now(UTC) - timedelta(days=1),
                )
                self.run_child(snapshot_process, window, True)
                before = self.query(
                    "SELECT member_count FROM feishu_org_sync_run WHERE status='complete'"
                )
                self.assertEqual(before, [(2 if window == "after_commit" else 1,)])
                self.assertEqual(
                    store.has_complete_run_on(datetime.now(UTC).date()), window == "after_commit"
                )
                self.run_child(snapshot_process, "none", False)
                self.assertEqual(
                    self.query(
                        "SELECT member_count FROM feishu_org_sync_run WHERE status='complete'"
                    ),
                    [(2,)],
                )
                self.assertEqual(
                    self.query(
                        "SELECT r.member_count,count(m.id) FROM feishu_org_sync_run r "
                        "LEFT JOIN feishu_org_member_snapshot m ON m.sync_run_id=r.id GROUP BY r.id "
                        "HAVING r.member_count<>count(m.id)"
                    ),
                    [],
                )
                # 当日已提交后，新进程不再扫描/提交第二份同日快照。
                count = self.query("SELECT count(*) FROM feishu_org_sync_run")
                self.run_child(snapshot_process, "none", False)
                self.assertEqual(self.query("SELECT count(*) FROM feishu_org_sync_run"), count)

    def test_report_process_windows_keep_watermark_and_original_dedupe(self):
        for window in (
            "during_aggregation",
            "before_send",
            "receipt_unknown",
            "before_watermark",
            "after_watermark",
        ):
            with self.subTest(window=window):
                self.setUp()
                self.run_child(report_process, window, True)
                receipts = self.query("SELECT count(*) FROM s6_platform_receipt")[0][0]
                self.assertEqual(receipts, int(window not in {"during_aggregation", "before_send"}))
                sent = PostgresDailyReportWatermark(DSN).already_sent(
                    report_date=datetime.now(UTC).date(), chat_id="s6-report"
                )
                self.assertEqual(sent, window == "after_watermark")
                # 回执前死进程只证明未知，不能把未落水位解释为未发送。
                classification = "unknown" if window == "receipt_unknown" else "known_checkpoint"
                self.run_child(report_process, "none", False)
                self.assertEqual(self.query("SELECT count(*) FROM s6_platform_receipt"), [(1,)])
                self.assertEqual(
                    self.query("SELECT count(DISTINCT dedupe_key) FROM s6_send_call"), [(1,)]
                )
                calls = self.query("SELECT count(*) FROM s6_send_call")
                self.run_child(report_process, "none", False)
                self.assertEqual(self.query("SELECT count(*) FROM s6_send_call"), calls)
                print(
                    "S6_REPORT_WINDOW",
                    window,
                    classification,
                    "calls",
                    calls[0][0],
                    "deliveries",
                    1,
                )
