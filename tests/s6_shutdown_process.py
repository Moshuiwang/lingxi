"""真实20/120秒截止的子进程，用可控长调用代替外部平台。"""

import os
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from s6_process_fixtures import snapshot_batch
from test_daily_report_duty import FakeSource

from lingxi.adapters.innertest_socket import InnertestSocketListener
from lingxi.adapters.postgres_admin_followup import PostgresFollowupStore
from lingxi.adapters.postgres_daily_report_watermark import PostgresDailyReportWatermark
from lingxi.adapters.postgres_identity import PostgresOrgSnapshotStore
from lingxi.apps.gateway import _BackgroundLoops, _install_background_stop
from lingxi.apps.scheduler.daily_report import DailyReportDuty
from lingxi.apps.scheduler.lifecycle import SignalStopEvent
from lingxi.apps.scheduler.loop import SchedulerLoop, install_signal_handlers
from lingxi.apps.scheduler.onboarding import OnboardingExecutor
from lingxi.apps.scheduler.org_snapshot_sync import OrgSnapshotSyncDuty
from lingxi.core.admin.followup_consumer import FollowupConsumer
from lingxi.core.admin.followup_renewal import FollowupLeaseKeeper


def shutdown_process(dsn, kind, pipe):
    budget = 20 if kind == "gateway" else 120
    stop = threading.Event() if kind == "gateway" else SignalStopEvent()
    started_work = threading.Event()

    def slow(*_args, **_kwargs):
        started_work.set()
        time.sleep(budget + 40)

    store = PostgresFollowupStore(dsn)
    if kind == "gateway":
        lifecycle, clock, _request_stop = _install_background_stop(
            SimpleNamespace(shutdown_timeout_seconds=20), stop
        )
        workers = [threading.Thread(target=slow, daemon=True) for _ in range(2)]
        loops = _BackgroundLoops(delivery=workers[0], document_delivery=workers[1], watchdogs=[])
        loops.start()
        stage = "group_notify"
        consumer_kind = "postprocess"
        scheduler = None
        socket_path = None
        socket_directory = None
    else:
        executor = OnboardingExecutor(workers=1, backlog=1, should_stop=stop.is_set)
        executor.start()
        executor.submit(slow)
        owner = SimpleNamespace(onboarding_executor=executor, run_once=lambda: None)

        def read_snapshot():
            slow()
            return snapshot_batch("unused", 1)

        org = OrgSnapshotSyncDuty(
            read_snapshot=read_snapshot,
            store=PostgresOrgSnapshotStore(dsn),
            audit=Mock(),
            source_app_id="synthetic",
            stop=stop,
            round_join_timeout_seconds=0.01,
        )

        class Source(FakeSource):
            def active_user_task_counts(self, **kwargs):
                slow()
                return super().active_user_task_counts(**kwargs)

        daily = DailyReportDuty(
            source=Source(),
            watermark=PostgresDailyReportWatermark(dsn),
            sender=Mock(),
            audit=Mock(),
            chat_id="s6-budget",
            stop=stop,
            aggregation_join_timeout_seconds=0.01,
        )
        scheduler = SchedulerLoop(duties=[owner, org, daily], stop=stop)
        lifecycle = scheduler.lifecycle
        install_signal_handlers(scheduler)
        scheduler.run_once()
        socket_directory = tempfile.TemporaryDirectory(prefix="s6-deadline-")
        socket_path = str(Path(socket_directory.name) / "mcp.sock")
        listener = InnertestSocketListener(
            path=socket_path, service=Mock(), db_slots=scheduler.followup_db_slots
        )
        listener.start()
        scheduler.register_background(listener)
        stage = "innertest_preprovision"
        consumer_kind = "scheduler"
    consumer = FollowupConsumer(
        store=store,
        consumer_kind=consumer_kind,
        owner="s6-deadline",
        handlers={stage: slow},
        audit=Mock(),
    )
    lifecycle.register(consumer)
    lifecycle.register(FollowupLeaseKeeper([consumer], audit=Mock()))
    consumer.start()
    deadline = time.monotonic() + 5
    while consumer._current is None and time.monotonic() < deadline:
        time.sleep(0.01)
    if consumer._current is None:
        raise RuntimeError("consumer_not_running")
    pipe.send(dict(state="ready", pid=os.getpid(), socket=socket_path, budget=budget))
    if scheduler is None:
        while not stop.wait(0.01):
            pass
    else:
        scheduler.run_forever()
    started = time.monotonic()
    first_deadline = lifecycle.deadline
    report = lifecycle.drain_until() if scheduler is None else scheduler.drain_until()
    if kind == "gateway":
        loops.join_within(clock, 20)
        residual = int(loops.delivery.is_alive()) + int(loops.document_delivery.is_alive())
    else:
        residual = 0
    pipe.send(
        dict(
            state="drained",
            elapsed=time.monotonic() - started,
            deadline_unchanged=lifecycle.deadline == first_deadline,
            still_running=report.still_running + residual,
            socket_absent=not (socket_path and Path(socket_path).exists()),
        )
    )
    if socket_directory is not None:
        socket_directory.cleanup()
