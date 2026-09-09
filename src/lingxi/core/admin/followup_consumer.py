"""每个消费者仅一条线程，领取及停止串行化，任务在数据库中等待。"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from lingxi.core.admin.followup import EXTERNAL_STAGES, ShutdownReport


@dataclass(frozen=True)
class FollowupResult:
    """处理器只返回明确结果或等待，不执行动态代码。"""

    status: str = "succeeded"
    result_code: str = "completed"
    external_ref: str | None = None
    next_items: tuple = ()


class FollowupConsumer:
    """固定处理器表，未知阶段保持待处理直到装配了正确消费者。"""

    def __init__(self, *, store, consumer_kind, owner, handlers, audit):
        """构造不启动线程，由生命周期装配显式启动。"""
        self.store, self.kind, self.owner = store, consumer_kind, owner
        self.handlers, self.audit = dict(handlers), audit
        self._stop = threading.Event()
        self._gate = threading.Lock()
        self._thread = None
        self._accepted = self._finished = self._unknown = 0
        self._current = None

    def start(self):
        """只创建一条消费者线程，重复启动无副作用。"""
        with self._gate:
            if self._thread is not None or self._stop.is_set():
                return
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="lingxi-followup-" + self.kind
            )
            self._thread.start()

    def request_stop(self):
        """返回后绝不再领取；已领取的工作在同一预算内结束。"""
        with self._gate:
            self._stop.set()

    def drain_until(self, deadline_monotonic):
        """超时仍在途如实计数，连接归执行线程持有直至自身退出。"""
        self.request_stop()
        if self._thread is not None:
            self._thread.join(max(0, deadline_monotonic - time.monotonic()))
        running = int(self._thread is not None and self._thread.is_alive())
        return ShutdownReport(self._accepted, self._finished, running, self._unknown, running)

    def run_once(self):
        """停止门与领取原子排序，处理失败不能阻止其他目标。"""
        with self._gate:
            if self._stop.is_set():
                return False
            now = datetime.now(UTC)
            self.store.recover_expired(now=now, limit=32)
            item = self.store.claim_followup(consumer_kind=self.kind, owner=self.owner, now=now)
            if item is None:
                return False
            self._accepted += 1
            self._current = item
        self._execute(item)
        self._current = None
        return True

    def _execute(self, item):
        """外发标记失败零调用；失去领取代数绝不覆盖其他执行者。"""
        try:
            handler = self.handlers.get(item.stage)
            if handler is None:
                result = FollowupResult("retry_wait", "handler_unavailable")
            elif item.stage in EXTERNAL_STAGES and not self.store.mark_effect_started(
                id=item.id, owner=self.owner, attempt=item.attempt, now=datetime.now(UTC)
            ):
                return
            else:
                result = handler(item)
            self._finish(item, result)
        except Exception as error:
            self.store.retry_followup(
                id=item.id,
                owner=self.owner,
                attempt=item.attempt,
                now=datetime.now(UTC),
                result_code=type(error).__name__,
            )
            self._record(item, "execution_failed")

    def _finish(self, item, result):
        """等待和终态用同一领取代数写回。"""
        if result.status == "retry_wait":
            self.store.retry_followup(
                id=item.id,
                owner=self.owner,
                attempt=item.attempt,
                now=datetime.now(UTC),
                result_code=result.result_code,
            )
        elif self.store.complete_followup(
            id=item.id,
            owner=self.owner,
            attempt=item.attempt,
            status=result.status,
            result_code=result.result_code,
            external_ref=result.external_ref,
            next_items=result.next_items,
        ):
            self._finished += 1
            self._unknown += int(result.status == "unknown")
        self._record(item, result.result_code)

    def _record(self, item, result_code):
        """每项工作记录自身追溯号，运行标识独立。"""
        self.audit.record(
            "admin.followup.result",
            trace_id=item.trace_id,
            pending_action_id=item.pending_action_id,
            followup_id=item.id,
            batch_id=item.batch_id,
            batch_item_id=item.batch_item_id,
            attempt=item.attempt,
            run_id=self.owner,
            result_code=result_code,
        )

    def _run(self):
        """没有每卡线程、每项计时器或整批内存载入。"""
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as error:
                self.audit.record("admin.followup.poll_failed", error=type(error).__name__)
            self._stop.wait(1)
