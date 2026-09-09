"""所有阶段共用一条续租循环，不为每项任务创建计时器。"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

from lingxi.core.admin.followup import ShutdownReport


class FollowupLeaseKeeper:
    """消费者先停止领取，续租循环保持到消费者完成等待之后。"""

    def __init__(self, consumers, *, audit):
        """每个进程只登记一个集中续租者。"""
        self.consumers = tuple(consumers)
        self.audit = audit
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="lingxi-followup-renewal", daemon=True
        )
        self._thread.start()

    def request_stop(self):
        """本对象不领取工作；继续保护统一截止内的在途阶段。"""

    def drain_until(self, deadline_monotonic):
        """须在消费者之后登记，使在途阶段等待期间仍能续租。"""
        self._stop.set()
        self._thread.join(max(0, deadline_monotonic - time.monotonic()))
        return ShutdownReport(still_running=int(self._thread.is_alive()))

    def renew_once(self):
        """失去租约标记给执行者，禁止它开始后续外发。"""
        for consumer in self.consumers:
            item = consumer._current
            if item is None:
                continue
            try:
                if not consumer.store.renew_lease(
                    id=item.id, owner=item.lease_owner, attempt=item.attempt, now=datetime.now(UTC)
                ):
                    consumer._lease_lost = True
            except Exception as error:
                consumer._lease_lost = True
                self.audit.record(
                    "admin.followup.renew_failed",
                    followup_id=item.id,
                    attempt=item.attempt,
                    error=type(error).__name__,
                )

    def _run(self):
        """每 30 秒续租，停止不会留下迟到 Timer 回调。"""
        while not self._stop.wait(30):
            self.renew_once()
