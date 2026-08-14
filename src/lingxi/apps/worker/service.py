"""Agent worker 的队列消费、心跳、停止和终态收口。"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import Any, Callable, Mapping, Protocol

from lingxi.adapters.postgres_conversation import ClaimedTask, PostgresTaskQueue, TerminalTask
from lingxi.apps.worker.config import WorkerConfig
from lingxi.apps.worker.turn import WorkerTurnExecutor
from lingxi.config.content import ContentCatalog, RenderedContent, default_content_catalog
from lingxi.core.delivery.ports import TerminalKind


class QueueListener(Protocol):
    def wait(self, *, timeout_seconds: float) -> bool: ...


ExecutorFactory = Callable[[WorkerConfig, Callable[[], None]], Any]
HeartbeatCallback = Callable[[], None]
TaskStuckCallback = Callable[[str, int], None]


class WorkerService:
    """一个进程持续消费一个固定 target version 的任务。

    ``process_once`` 是白盒测试和受控演练的入口；``run`` 才是长期 worker 循环。队列
    的 LISTEN 只用于降低延迟，任何一轮都会走轮询与回收检查，所以丢 NOTIFY 不会让
    queued 永久悬挂。

    **投递意图只落数据库，不直接调用飞书**（Issue #151 状态合同）：任务收口时写入
    ``task_delivery_event`` 的 ``started``/``progress``/``terminal`` 事件并把任务
    转为 ``awaiting_delivery``；把事件消费为真实飞书卡片/文本、记录
    ``platform_received`` 并最终收敛业务状态是 Gateway（#152）的职责，本类不再
    持有任何出站 transport。
    """

    def __init__(
        self,
        *,
        config: WorkerConfig,
        queue: Any,
        executor_factory: ExecutorFactory | None = None,
        listener_factory: Callable[[], QueueListener] | None = None,
        catalog: ContentCatalog | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        heartbeat: HeartbeatCallback | None = None,
        on_task_stuck: TaskStuckCallback | None = None,
    ) -> None:
        self._config = config
        self._queue = queue
        self._executor_factory = executor_factory or (
            lambda worker_config, marker: WorkerTurnExecutor(
                worker_config, mark_external_side_effect=marker
            )
        )
        self._listener_factory = listener_factory
        self._catalog = catalog or default_content_catalog()
        self._sleep = sleep
        self._monotonic = monotonic
        self._heartbeat = heartbeat
        self._on_task_stuck = on_task_stuck

    async def process_once(self) -> bool:
        """做一轮回收、领取和执行；返回这一轮是否观察到任务。"""

        self._emit_heartbeat()
        terminal_tasks = self._housekeep()

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
            unavailable = fail_versions(
                available_versions=(self._config.target_worker_version,),
                unavailable_for=timedelta(
                    seconds=self._config.worker_version_unavailable_seconds
                )
            )
            terminals.extend(unavailable)
            self._report_task_stuck("queued_stuck", len(unavailable))
        reclaim_queued = getattr(self._queue, "reclaim_queued", None)
        if reclaim_queued is not None:
            queued = reclaim_queued(
                max_wait=timedelta(seconds=self._config.queue_max_wait_seconds)
            )
            terminals.extend(queued)
            self._report_task_stuck("queued_stuck", len(queued))
        reclaim_stale = getattr(self._queue, "reclaim_stale_with_outcomes", None)
        if reclaim_stale is not None:
            requeued, stale_terminals = reclaim_stale(
                older_than=timedelta(seconds=self._config.running_heartbeat_timeout_seconds),
                max_auto_retries=self._config.max_auto_retries,
            )
            terminals.extend(stale_terminals)
            self._report_task_stuck(
                "running_heartbeat_timeout", len(requeued) + len(stale_terminals)
            )
            self._report_task_stuck(
                "retry_exhausted",
                sum(item.error_kind == "retry_exhausted" for item in stale_terminals),
            )
        # 二十四小时到期仍未确认送达的投递终态：状态合同第 8 条、V-投递-06。
        # 这一步只强制收敛任务状态、释放话题并清空事件正文；把清理结果对外展现
        # 为"投递已过期，请重新提问"仍是 Gateway（下一次用户主动消息触发）的职责。
        # 二十四小时上限不接受这里传参：它由迁移 0059 的触发器锁定在
        # task_delivery_event.expires_at 列上，调用方不再持有另一份可以让它
        # 漂移的窗口配置（内审 P2-1）。
        expire_undelivered = getattr(self._queue, "expire_undelivered_terminals", None)
        if expire_undelivered is not None:
            expired = expire_undelivered()
            terminals.extend(expired)
            self._report_task_stuck("delivery_expired", len(expired))
        return terminals

    def _emit_heartbeat(self) -> None:
        if self._heartbeat is None:
            return
        try:
            self._heartbeat()
        except Exception as error:  # noqa: BLE001 - 心跳失败不能带走任务职责
            # 只记异常类型；心跳是告警输入，不能因为告警输入失败而让 worker 停止消费。
            import logging

            logging.getLogger(__name__).error(
                "worker 心跳记录失败，任务职责继续运行 error=%s", type(error).__name__
            )

    def _report_task_stuck(self, kind: str, count: int) -> None:
        if self._on_task_stuck is None or count <= 0:
            return
        try:
            self._on_task_stuck(kind, count)
        except Exception as error:  # noqa: BLE001 - 告警失败不应改变任务状态
            import logging

            logging.getLogger(__name__).error(
                "任务滞留告警记录失败，任务状态保持由队列收口 error=%s", type(error).__name__
            )

    async def _process_task(self, claimed: ClaimedTask) -> None:
        context = self._queue.task_context(
            task_id=claimed.task_id, worker_id=self._config.worker_id
        )
        if context is None:
            return

        marker = lambda: self._queue.mark_side_effect(
            task_id=claimed.task_id, worker_id=self._config.worker_id
        )
        self._append_event(claimed, event_type="started", idempotency_key_suffix="started")

        stop_event = asyncio.Event()
        if context.stop_requested or claimed.stop_requested:
            stop_event.set()
        if stop_event.is_set():
            self._finish_terminal(
                claimed,
                terminal_kind=TerminalKind.STOPPED.value,
                error_kind="stopped",
                content=self._catalog.text("worker.stopped"),
            )
            return
        monitor = asyncio.create_task(self._monitor(claimed.task_id, stop_event))
        started_at = self._monotonic()
        progress_count = 0

        def on_stream_event(event: Mapping[str, Any]) -> None:
            nonlocal progress_count
            if event.get("kind") == "assistant_message":
                elapsed = int(max(0.0, self._monotonic() - started_at))
                progress_count += 1
                self._append_event(
                    claimed,
                    event_type="progress",
                    idempotency_key_suffix=f"progress:{progress_count}",
                    elapsed_seconds=elapsed,
                )

        try:
            executor = self._executor_factory(self._config, marker)
            report = await executor.run_turn(
                context.prompt,
                resume_session_id=(
                    context.agent_session_id if context.resumed_session else None
                ),
                stop_event=stop_event,
                on_stream_event=on_stream_event,
                external_texts=self._config.external_texts,
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
        elapsed_seconds = int(max(0.0, self._monotonic() - started_at))

        output_safety = turn.get("output_safety") if isinstance(turn, Mapping) else None
        withheld = bool(isinstance(output_safety, Mapping) and output_safety.get("withheld"))

        if stop_requested or failure_code == "interrupted":
            content = (
                self._catalog.text("worker.stopped_result", result=final_text)
                if final_text
                else self._catalog.text("worker.stopped")
            )
            self._finish_terminal(
                claimed,
                terminal_kind=TerminalKind.STOPPED.value,
                error_kind="stopped",
                content=content,
                elapsed_seconds=elapsed_seconds,
            )
        elif withheld:
            # #141/#149：整段正文因安全策略被拒发，即使 closed=True 也不得记
            # succeeded——用户没有拿到结果，必须走独立、可查询的 redacted_withheld
            # 终态（status 沿用既有取值域，用 error_kind 承载可查询原因）。
            self._finish_terminal(
                claimed,
                terminal_kind=TerminalKind.REDACTED_WITHHELD.value,
                error_kind="redacted_withheld",
                content=self._catalog.text("worker.redacted_withheld"),
                elapsed_seconds=elapsed_seconds,
            )
        elif bool(turn.get("closed")) and not failure:
            self._finish_terminal(
                claimed,
                terminal_kind=TerminalKind.SUCCESS.value,
                error_kind=None,
                content=RenderedContent(key="worker.result", version=self._catalog.version, text=final_text),
                elapsed_seconds=elapsed_seconds,
                session_id=turn.get("session_id") if isinstance(turn, Mapping) else None,
            )
        else:
            error_kind, content = self._failure_content(failure_code)
            terminal_kind = (
                TerminalKind.TIMEOUT.value if failure_code == "turn_timeout" else TerminalKind.FAILED.value
            )
            self._finish_terminal(
                claimed,
                terminal_kind=terminal_kind,
                error_kind=error_kind,
                content=content,
                elapsed_seconds=elapsed_seconds,
            )

    def _append_event(
        self,
        claimed: ClaimedTask,
        *,
        event_type: str,
        idempotency_key_suffix: str,
        elapsed_seconds: int | None = None,
        content: str | None = None,
    ) -> None:
        """写入非终态事件；失败不中断任务执行——它是可恢复的运行信号，不是结果。"""

        try:
            self._queue.append_delivery_event(
                task_id=claimed.task_id,
                worker_id=self._config.worker_id,
                event_type=event_type,
                idempotency_key=f"{claimed.task_id}:a{claimed.attempts}:{idempotency_key_suffix}",
                elapsed_seconds=elapsed_seconds,
                content=content,
            )
        except Exception as error:  # noqa: BLE001 - 事件是可恢复的运行信号，不能带走任务
            import logging

            logging.getLogger(__name__).error(
                "投递事件写入失败，任务继续执行 event_type=%s error=%s",
                event_type,
                type(error).__name__,
            )

    def _finish_terminal(
        self,
        claimed: ClaimedTask,
        *,
        terminal_kind: str,
        error_kind: str | None,
        content: RenderedContent,
        elapsed_seconds: int = 0,
        session_id: str | None = None,
    ) -> None:
        """写终态事件、把任务转入 ``awaiting_delivery``（Issue #151 状态合同第 2
        条）。话题继续占用直到投递解析，因此新建立的 ``session_id``（只在业务
        成功时非空）随终态事件一起持久化，留到确认送达时才写回
        ``conversation.agent_session_id``——同一话题在此期间不会有第二个任务插进
        来读它，延后写入是安全的（见 ``core.delivery.ports`` 与
        ``PostgresTaskQueue.confirm_delivery`` 的取舍说明）。
        """

        self._queue.write_terminal_event(
            task_id=claimed.task_id,
            worker_id=self._config.worker_id,
            terminal_kind=terminal_kind,
            error_kind=error_kind,
            content=content.text,
            elapsed_seconds=elapsed_seconds,
            agent_session_id=session_id,
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
