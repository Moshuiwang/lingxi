"""S6 独立进程故障输入，只有合成数据与持久假传输。"""

from datetime import UTC, datetime
from unittest.mock import Mock

from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_daily_report_watermark import PostgresDailyReportWatermark
from lingxi.adapters.postgres_identity import PostgresOrgSnapshotStore
from lingxi.apps.scheduler.daily_report import DailyReportDuty
from lingxi.apps.scheduler.org_snapshot_sync import OrgSnapshotSyncDuty
from lingxi.core.identity.org_snapshot import SnapshotBatch, SnapshotMember, TenantScope


def snapshot_batch(prefix, count):
    keys = frozenset(f"{prefix}{i}" for i in range(count))
    return SnapshotBatch(
        tenants=(TenantScope("synthetic", True, keys, keys),),
        departments=(),
        members=tuple(
            SnapshotMember("synthetic", key, "ou_" + key, "fs_" + key, "un_" + key, key)
            for key in sorted(keys)
        ),
    )


def stop_at(pipe, window, current):
    if window == current:
        pipe.send(current)
        pipe.recv()


def snapshot_process(dsn, pipe, window):
    class Store(PostgresOrgSnapshotStore):
        def _insert_snapshot_rows(self, cursor, identifier, batch):
            super()._insert_snapshot_rows(cursor, identifier, batch)
            stop_at(pipe, window, "before_commit")

        def commit_batch(self, batch, **kwargs):
            result = super().commit_batch(batch, **kwargs)
            stop_at(pipe, window, "after_commit")
            return result

    def read():
        stop_at(pipe, window, "during_read")
        return snapshot_batch("new", 2)

    duty = OrgSnapshotSyncDuty(
        read_snapshot=read,
        store=Store(dsn),
        audit=Mock(),
        source_app_id="synthetic",
        round_join_timeout_seconds=0.01,
    )
    duty.run_once()
    if duty._pending_thread:
        duty._pending_thread.join(15)
    pipe.send("completed")


def report_process(dsn, pipe, window):
    from test_daily_report_duty import FakeSource

    class Source(FakeSource):
        def active_user_task_counts(self, **kwargs):
            stop_at(pipe, window, "during_aggregation")
            return super().active_user_task_counts(**kwargs)

    class Sender:
        def send_text(self, *, chat_id, text, dedupe_key):
            del text
            stop_at(pipe, window, "before_send")
            # 合成平台按既有同日去重键保留唯一交付；调用与交付分别计数。
            with connect(dsn) as conn, conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO s6_send_call(chat_id,dedupe_key) VALUES(%s,%s)",
                    (chat_id, dedupe_key),
                )
                cursor.execute(
                    "INSERT INTO s6_platform_receipt(chat_id,dedupe_key) VALUES(%s,%s) "
                    "ON CONFLICT DO NOTHING",
                    (chat_id, dedupe_key),
                )
            stop_at(pipe, window, "receipt_unknown")

    class Watermark(PostgresDailyReportWatermark):
        def mark_sent(self, **kwargs):
            stop_at(pipe, window, "before_watermark")
            super().mark_sent(**kwargs)
            stop_at(pipe, window, "after_watermark")

    duty = DailyReportDuty(
        source=Source(),
        watermark=Watermark(dsn),
        sender=Sender(),
        audit=Mock(),
        chat_id="s6-report",
        clock=lambda: datetime.now(UTC),
        aggregation_join_timeout_seconds=0.01,
    )
    duty.run_once()
    if duty._pending_thread:
        duty._pending_thread.join(15)
    pipe.send("completed")
