"""Agent worker 的队列消费、心跳、停止和终态收口。"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import Any, Callable, Mapping, Protocol

from lingxi.adapters.postgres_conversation import ClaimedTask, PostgresTaskQueue, TaskContext, TerminalTask
from lingxi.apps.worker.config import WorkerConfig
from lingxi.apps.worker.delivery import NullTaskDelivery, TaskDelivery
from lingxi.apps.worker.turn import WorkerTurnExecutor
from lingxi.config.content import ContentCatalog, RenderedContent, default_content_catalog


class QueueListener(Protocol):
    def wait(self, *, timeout_seconds: float) -> bool: ...


ExecutorFactory = Callable[[WorkerConfig, Callable[[], None]], Any]
DeliveryFactory = Callable[[TaskContext, Callable[[], None]], TaskDelivery]


class WorkerService:
    """一个进程持续消费一个固定 target version 的任务。

    ``process_once`` 是白盒测试和受控演练的入口；``run`` 才是长期 worker 循环。队列
    的 LISTEN 只用于降低延迟，任何一轮都会走轮询与回收检查，所以丢 NOTIFY 不会让
    queued 永久悬挂。
    """

    def __init__(
        self,
        *,
        config: WorkerConfig,
        queue: Any,
        executor_factory: ExecutorFactory | None = None,
        delivery_factory: DeliveryFactory | None = None,
        listener_factory: Callable[[], QueueListener] | None = None,
        catalog: ContentCatalog | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._queue = queue
        self._executor_factory = executor_factory or (
            lambda worker_config, marker: WorkerTurnExecutor(
                worker_config, mark_external_side_effect=marker
            )
        )
        self._delivery_factory = delivery_factory or (
            lambda _context, _marker: NullTaskDelivery()
        )
        self._listener_factory = listener_factory
        self._catalog = catalog or default_content_catalog()
        self._sleep = sleep
        self._monotonic = monotonic

    async def process_once(self) -> bool:
        """做一轮回收、领取和执行；返回这一轮是否观察到任务。"""

        terminal_tasks = self._housekeep()
        for terminal in terminal_tasks:
            self._deliver_terminal(terminal)

        tasks = self._queue.claim(
            worker_id=self._config.worker_id,
            target_worker_version=self._config.target_worker_version,
            limit=self._config.max_concurrency,
        )
        if not tasks:
            return bool(terminal_tasks)
        await asyncio.gather(*(self._process_task(task) for task in tasks))
        return True

    def _housekeep(self) -> list[TerminalTask]:
        terminals: list[TerminalTask] = []
        fail_versions = getattr(self._queue, "fail_unavailable_versions", None)
        if fail_versions is not None:
            terminals.extend(
                fail_versions(
                    available_versions=(self._config.target_worker_version,),
                    unavailable_for=timedelta(
                        seconds=self._config.worker_version_unavailable_seconds
                    ),
                )
            )
        reclaim_queued = getattr(self._queue, "reclaim_queued", None)
        if reclaim_queued is not None:
            terminals.extend(
                reclaim_queued(
                    max_wait=timedelta(seconds=self._config.queue_max_wait_seconds)
                )
            )
        reclaim_stale = getattr(self._queue, "reclaim_stale_with_outcomes", None)
        if reclaim_stale is not None:
            _requeued, stale_terminals = reclaim_stale(
                older_than=timedelta(seconds=self._config.running_heartbeat_timeout_seconds),
                max_auto_retries=self._config.max_auto_retries,
            )
            terminals.extend(stale_terminals)
        return terminals

    async def _process_task(self, claimed: ClaimedTask) -> None:
        context = self._queue.task_context(
            task_id=claimed.task_id, worker_id=self._config.worker_id
        )
        if context is None:
            return

        marker = lambda: self._queue.mark_side_effect(
            task_id=claimed.task_id, worker_id=self._config.worker_id
        )
        delivery = self._delivery_factory(context, marker)
        try:
            delivery.start()
        except Exception:
            # delivery 的实现可以选择自己回退；这里保留任务执行，最终会以失败终态
            # 释放话题，且不会把一个未完成的 card 当成成功。
            pass

        stop_event = asyncio.Event()
        if context.stop_requested or claimed.stop_requested:
            stop_event.set()
        if stop_event.is_set():
            self._deliver_failure(delivery, self._catalog.text("worker.stopped"))
            self._queue.finish(
                task_id=claimed.task_id,
                conversation_id=claimed.conversation_id,
                status="stopped",
                worker_id=self._config.worker_id,
                error_kind="stopped",
            )
            return
        monitor = asyncio.create_task(self._monitor(claimed.task_id, stop_event))
        started_at = self._monotonic()

        def on_stream_event(event: Mapping[str, Any]) -> None:
            if event.get("kind") == "assistant_message":
                elapsed = int(max(0.0, self._monotonic() - started_at))
                try:
                    delivery.progress(elapsed_seconds=elapsed)
                except Exception:
                    pass

        try:
            executor = self._executor_factory(self._config, marker)
            report = await executor.run_turn(
                context.prompt,
                resume_session_id=(
                    context.agent_session_id if context.resumed_session else None
                ),
                stop_event=stop_event,
                on_stream_event=on_stream_event,
            )
        except Exception as error:  # noqa: BLE001 - worker 绝不留下 running
            report = {
                "turn": {"closed": False, "final_text": "", "session_id": None},
                "failure": {"code": "session_failed", "message": type(error).__name__},
            }
        finally:
            monitor.cancel()
            try:
                await monitor
            except asyncio.CancelledError:
                pass

        stop_requested = stop_event.is_set() or self._queue.stop_requested(
            task_id=claimed.task_id, worker_id=self._config.worker_id
        )
        turn = report.get("turn") or {}
        failure = report.get("failure") or {}
        failure_code = failure.get("code") if isinstance(failure, Mapping) else None
        final_text = turn.get("final_text") if isinstance(turn, Mapping) else ""
        final_text = final_text if isinstance(final_text, str) else ""

        if stop_requested or failure_code == "interrupted":
            status = "stopped"
            error_kind = "stopped"
            content = (
                self._catalog.text("worker.stopped_result", result=final_text)
                if final_text
                else self._catalog.text("worker.stopped")
            )
            self._deliver_failure(delivery, content)
        elif bool(turn.get("closed")) and not failure:
            status = "succeeded"
            error_kind = None
            try:
                delivery.complete(
                    result=final_text,
                    elapsed_seconds=int(max(0.0, self._monotonic() - started_at)),
                )
            except Exception:
                status = "failed"
                error_kind = "delivery_failed"
                self._deliver_failure(delivery, self._catalog.text("worker.failed"))
        else:
            status = "failed"
            error_kind, content = self._failure_content(failure_code)
            self._deliver_failure(delivery, content)

        self._queue.finish(
            task_id=claimed.task_id,
            conversation_id=claimed.conversation_id,
            status=status,
            worker_id=self._config.worker_id,
            agent_session_id=(
                turn.get("session_id") if status == "succeeded" and isinstance(turn, Mapping) else None
            ),
            error_kind=error_kind,
        )

    async def _monitor(self, task_id: str, stop_event: asyncio.Event) -> None:
        last_heartbeat = self._monotonic()
        while True:
            if self._queue.stop_requested(task_id=task_id, worker_id=self._config.worker_id):
                stop_event.set()
            now = self._monotonic()
            if now - last_heartbeat >= self._config.heartbeat_interval_seconds:
                if not self._queue.heartbeat(
                    task_id=task_id, worker_id=self._config.worker_id
                ):
                    return
                last_heartbeat = now
            await self._sleep(self._config.stop_poll_interval_seconds)

    def _failure_content(self, code: object) -> tuple[str, RenderedContent]:
        if code == "context_too_long":
            return "context_too_long", self._catalog.text("worker.context_too_long")
        if code == "turn_timeout":
            return "running_timeout", self._catalog.text("worker.running_timeout")
        if code == "side_effect_uncertain":
            return "side_effect_uncertain", self._catalog.text("worker.side_effect_uncertain")
        return "session_failed", self._catalog.text("worker.failed")

    @staticmethod
    def _deliver_failure(delivery: TaskDelivery, content: RenderedContent) -> None:
        try:
            delivery.fail(content=content)
        except Exception:
            # 任务状态收口优先；真实出站失败已经不能安全重放，留给审计/人工处理。
            pass

    def _deliver_terminal(self, terminal: TerminalTask) -> None:
        context_getter = getattr(self._queue, "terminal_context", None)
        if context_getter is None:
            return
        context = context_getter(task_id=terminal.task_id)
        if context is None:
            return
        delivery = self._delivery_factory(context, lambda: None)
        content_key = {
            "queued_timeout": "worker.queued_timeout",
            "worker_version_unavailable": "worker.version_unavailable",
            "side_effect_uncertain": "worker.side_effect_uncertain",
            "retry_exhausted": "worker.running_timeout",
        }.get(terminal.error_kind, "worker.failed")
        self._deliver_failure(delivery, self._catalog.text(content_key))

    async def run(self, *, stop_event: asyncio.Event | None = None) -> None:
        stop = stop_event or asyncio.Event()
        listener_context = self._listener_factory() if self._listener_factory else None
        if listener_context is None:
            while not stop.is_set():
                did_work = await self.process_once()
                if not did_work:
                    await self._sleep(self._config.poll_interval_seconds)
            return

        with listener_context as listener:
            while not stop.is_set():
                did_work = await self.process_once()
                if did_work:
                    continue
                await asyncio.to_thread(
                    listener.wait,
                    timeout_seconds=self._config.poll_interval_seconds,
                )


WorkerQueueConsumer = WorkerService
