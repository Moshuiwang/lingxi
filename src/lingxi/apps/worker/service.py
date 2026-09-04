"""Agent worker 的队列消费、心跳、停止和终态收口。

一个进程持续消费一个固定目标版本的任务。队列的通知只用于降低延迟，任何一轮都会走轮询与
回收检查，所以丢通知不会让排队中的任务永久悬挂。

**投递意图只落数据库，不直接调用外部平台**：任务收口时写入起始/进度/终态三种事件并把
任务转为"等待投递"；把事件消费成真实卡片或文本、记录平台回执并最终收敛业务状态是 gateway
的职责，本模块不持有任何出站 transport。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from lingxi.adapters.postgres import close_idle_connections
from lingxi.adapters.postgres_conversation import ClaimedTask, TerminalTask
from lingxi.adapters.user_mcp_config import UserMcpConfigError, load_user_mcp_servers
from lingxi.apps.worker.config import WorkerConfig
from lingxi.apps.worker.content_capture import ContentCaptureRecorder
from lingxi.apps.worker.housekeeping import QueueHousekeeper
from lingxi.apps.worker.progress_reporting import TurnProgressReporter
from lingxi.apps.worker.report_extraction import (
    _cap_log_token,
    _load_task_system_prompt,
    failure_with_signature,
    sanitize_failure_signature,
)
from lingxi.apps.worker.service_ports import (
    ExecutorFactory,
    QueueListener,
    SessionCleanupSettings,
    UserMemoryReader,
    WorkerObservers,
)
from lingxi.apps.worker.task_processing import (
    TerminalDecision,
    TurnOutcome,
    decide_terminal,
    empty_outcome,
)
from lingxi.apps.worker.terminal_outcome import TerminalOutcomeAudit
from lingxi.apps.worker.turn import WorkerTurnExecutor
from lingxi.config.content import ContentCatalog, default_content_catalog
from lingxi.core.delivery.ports import DeliveryEventType, TerminalKind, assert_content_allowed

logger = logging.getLogger(__name__)

# 停机信号的送达要跨过事件循环自管道机制的完整链路：信号送达 → 自管道被写入 → 下一轮
# select 才发现可读并排进 reader 回调 → 该回调再把真正的用户回调排进再下一轮。实测必须
# 连续让出 3 轮才能观测到标志位翻转（只让出一次对真实信号无效，对用 call_soon 模拟的
# 假信号却有效——这正是当年用例是绿的、生产路径依旧漏的原因）。这里给到 5 轮留余量。
_STOP_SIGNAL_DRAIN_YIELDS = 5


class WorkerService:
    """一个进程持续消费一个固定目标版本的任务。

    ``process_once`` 是白盒测试和受控演练的入口；``run`` 才是长期 worker 循环。
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
        observers: WorkerObservers | None = None,
        session_cleanup: SessionCleanupSettings | None = None,
        user_memory_reader: UserMemoryReader | None = None,
    ) -> None:
        """装配一个 worker；除 ``config``/``queue`` 外全部可留默认值。"""
        sinks = observers or WorkerObservers()
        sessions = session_cleanup or SessionCleanupSettings()
        self._config = config
        self._queue = queue
        self._executor_factory = executor_factory or _default_executor_factory
        self._listener_factory = listener_factory
        self._catalog = catalog or default_content_catalog()
        self._sleep = sleep
        self._monotonic = monotonic
        self._heartbeat = sinks.heartbeat
        self._on_alert_tick = sinks.on_alert_tick
        self._user_memory_reader = user_memory_reader
        self._session_root = sessions.root
        self._housekeeper = QueueHousekeeper(
            config=config,
            queue=queue,
            monotonic=self._monotonic,
            session_root=sessions.root,
            session_cleanup_batch_limit=sessions.batch_limit,
            on_task_stuck=sinks.on_task_stuck,
        )
        self._terminal_audit = TerminalOutcomeAudit(sinks.on_terminal_outcome)
        self._capture = ContentCaptureRecorder(
            config=config,
            content_capture_writer=sinks.content_capture_writer,
            on_year_grounding_suspect=sinks.on_year_grounding_suspect,
        )
        # 收到停机信号后由 ``run()`` 设置：在途任务的监控循环据此把"进程正在停机"与
        # "用户按了停止"同等看待，主动请求执行层中断当前回合。
        self._global_stop: asyncio.Event | None = None

    async def process_once(self) -> bool:
        """做一轮回收、领取和执行；返回这一轮是否观察到任务。"""
        self._emit_heartbeat()
        self._tick_alerts()
        # 巡检搬离事件循环：占住循环＝只读屏障唯一判定层的工具前置钩子应答不了，而钩子
        # 超时是失败关闭。
        terminal_tasks = await asyncio.to_thread(self._housekeeper.run)

        # 紧贴 claim() 之前再判一次停机信号（见 `_STOP_SIGNAL_DRAIN_YIELDS`）：判定与
        # claim() 之间几乎全同步，信号若落在那段窗口里，标志位读到的还是旧值——会把一条
        # 还在排队、从未执行过的任务领走，直接收口成"已停止"且不会被重排。一旦提前观测
        # 到置位就立即返回，不多空转；没装全局停机信号时整段跳过，行为不变。
        if self._global_stop is not None:
            for _ in range(_STOP_SIGNAL_DRAIN_YIELDS):
                await asyncio.sleep(0)
                if self._global_stop.is_set():
                    return bool(terminal_tasks)

        return await self._run_rolling_claim_loop(terminal_tasks)

    async def _run_rolling_claim_loop(self, terminal_tasks: list[TerminalTask]) -> bool:
        """滚动并发领取：维持一个至多 ``max_concurrency`` 的在途集合，谁终态谁腾槽。

        旧的「整批领、整批等」实现里并发上限实际只是一次领取的批大小，一个长任务会让批内与
        批后到达的所有任务一起干等到它收口。每次等待都用轮询间隔兜底，因此即使一个都没终态
        也会按既有节奏重新尝试领取。停机语义不弱化：一旦置位就不再领取新任务。

        巡检节拍也要自己维持：入口只在进入循环前巡检一次，而持续有任务可领时这个循环要等在途
        集合清空才返回——滚动并发把批次间隙消灭之后，各项巡检会在供给不断时整体停摆到进程
        下一次真正无事可做为止。复用轮询间隔当节拍，不新开配置项。
        """
        pending: set[asyncio.Task[None]] = set()
        claimed_any = False
        last_housekeep_at = self._monotonic()

        def stop_requested() -> bool:
            return self._global_stop is not None and self._global_stop.is_set()

        try:
            while True:
                if not stop_requested():
                    claimed_any = self._claim_into(pending) or claimed_any
                if not pending:
                    break
                done, pending = await asyncio.wait(
                    pending,
                    timeout=self._config.poll_interval_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                self._raise_first_exception(done)
                now = self._monotonic()
                if now - last_housekeep_at >= self._config.poll_interval_seconds:
                    last_housekeep_at = now
                    self._tick_alerts()
                    terminal_tasks.extend(await asyncio.to_thread(self._housekeeper.run))
        except BaseException:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            raise

        return claimed_any or bool(terminal_tasks)

    def _claim_into(self, pending: set[asyncio.Task[None]]) -> bool:
        """把空槽填满；返回这一次是否真的领到了任务。"""
        capacity = self._config.max_concurrency - len(pending)
        if capacity <= 0:
            return False
        newly_claimed = self._queue.claim(
            worker_id=self._config.worker_id,
            target_worker_version=self._config.target_worker_version,
            limit=capacity,
        )
        for task in newly_claimed:
            pending.add(asyncio.create_task(self._process_task(task)))
        return bool(newly_claimed)

    @staticmethod
    def _raise_first_exception(done: set[asyncio.Task[None]]) -> None:
        """**先取全部异常再决定是否抛出**，然后抛第一个。

        取结果与取异常都会把异常标记为"已取回"，但取结果会在第一个失败的任务上就地抛出，
        把同一批里排在它后面的任务的异常晾在原地——那些异常从未被取回，会被运行时在垃圾
        回收时打成一条"异常从未被取回"的噪音日志。正常路径下任务内部已经把一切吞成结构化
        日志或终态，这里只是双保险。

        Raises:
            BaseException: 这一批里第一个真正抛出的异常。
        """
        errors = [error for task in done if (error := task.exception()) is not None]
        if errors:
            raise errors[0]

    def _emit_heartbeat(self) -> None:
        """戳一次活性；失败只记异常类型，不能因为告警输入失败而让 worker 停止消费。"""
        if self._heartbeat is None:
            return
        try:
            self._heartbeat()
        except Exception as error:
            logger.error("worker 心跳记录失败，任务职责继续运行 error=%s", type(error).__name__)

    def _tick_alerts(self) -> None:
        """推进一次告警状态机的恢复计时与投递重试。

        worker 没有独立的定时职责循环，借用队列消费循环每轮调用一次；频率因此等于轮询
        间隔，比定时进程高，但状态机的去重与阈值判定都基于时间戳而非调用次数，不敏感。
        """
        if self._on_alert_tick is None:
            return
        try:
            self._on_alert_tick()
        except Exception as error:
            logger.error(
                "worker 告警状态机推进失败，任务职责继续运行 error=%s", type(error).__name__
            )

    # ------------------------------------------------------------------
    # 一个任务的完整生命周期
    # ------------------------------------------------------------------

    async def _process_task(self, claimed: ClaimedTask) -> None:
        """执行一个已领取的任务，并把结果收口成终态事件。"""
        context = self._queue.task_context(
            task_id=claimed.task_id, worker_id=self._config.worker_id
        )
        if context is None:
            return
        self._append_event(claimed, event_type="started", idempotency_key_suffix="started")

        stop_event = asyncio.Event()
        if context.stop_requested or claimed.stop_requested:
            # 开工前就被停止：原因是确定的，如实记下，不留空白让运维去猜。
            stop_event.set()
            self._finish_terminal(
                claimed,
                TerminalDecision(
                    terminal_kind=TerminalKind.STOPPED.value,
                    error_kind="stopped",
                    content=self._catalog.text("worker.stopped"),
                    failure_code="stopped",
                ),
            )
            return

        started_at = self._monotonic()
        progress = TurnProgressReporter(
            write_event=lambda **fields: self._append_event(claimed, **fields),
            monotonic=self._monotonic,
            started_at=started_at,
        )
        report, executor, system_prompt_digest = await self._run_turn(
            claimed, context, stop_event=stop_event, progress=progress
        )

        outcome = TurnOutcome.from_report(report)
        self._finish_terminal(
            claimed,
            decide_terminal(outcome, catalog=self._catalog),
            outcome=outcome,
            elapsed_seconds=int(max(0.0, self._monotonic() - started_at)),
            system_prompt_digest=system_prompt_digest,
        )
        # 采集排在全部终态分支之后：终态（用户结果）优先于旁路观测，即使采集失败也不能
        # 影响已经写好的终态。失败/超时回合的问题原文与已尝试的工具调用同样有分析价值，
        # 因此不限于成功回合。
        self._capture.capture(claimed, executor=executor, question=context.prompt)

    async def _run_turn(
        self,
        claimed: ClaimedTask,
        context: Any,
        *,
        stop_event: asyncio.Event,
        progress: TurnProgressReporter,
    ) -> tuple[dict[str, Any], WorkerTurnExecutor | None, str | None]:
        """跑一个回合，并保证无论怎么失败都返回一份结构化报告。

        Returns:
            ``(回合报告, 执行器或 None, 提示词版本摘要)``；执行器构造出来之后才非空。
        """
        executor: WorkerTurnExecutor | None = None
        # 提示词摘要在 try 之外初始化。口径：「本轮**选定**并交给执行器装配的版本」——
        # 读不到时为 None；读到之后装配或建连失败的回合会带着摘要落失败终态，此时它回答
        # 的是"失败那一轮试图使用哪版"，不声称模型已经收到。
        system_prompt_digest: str | None = None
        monitor = asyncio.create_task(
            self._monitor(claimed.task_id, stop_event, on_stall_tick=progress.on_stall_tick)
        )
        try:
            # 红线：每个用户的问数必须用他自己的那份 MCP 配置，绝不回退到全进程共用的
            # 那份——回退意味着用一份不属于他的令牌去查数，是越权返回数据。这里**结构性
            # 地没有回退分支**：读取失败在下面单独一支收口成失败报告。
            user_mcp_servers = load_user_mcp_servers(
                root=self._config.user_env_root or "", user_id=claimed.user_id
            )
            task_system_prompt, system_prompt_digest = await self._resolve_system_prompt(claimed)
            task_system_prompt = await self._append_user_memory(claimed, task_system_prompt)
            # ``replace`` 会重跑数据类的后置校验：任务配置携带的是**已解析**的提示词，
            # 必须同时清掉文件指针，否则「文件与提示词互斥」的不变量会把每一个成功读到
            # 提示词的任务当场炸成会话失败。
            task_config = replace(
                self._config,
                mcp_servers=user_mcp_servers,
                system_prompt=task_system_prompt,
                system_prompt_file=None,
            )
            executor = self._executor_factory(task_config, lambda: self._mark_side_effect(claimed))
            report = await self._invoke_executor(
                executor, claimed, context, stop_event=stop_event, progress=progress
            )
        except UserMcpConfigError as error:
            # 失败码是本模块自定的安全码（不含路径、内容或令牌），供运维诊断具体原因。
            report = _failure_only_report(
                "user_mcp_config_unavailable", f"user_mcp_config:{error.code}", error
            )
        except Exception as error:
            # 执行层兜底之外的**第二条**兜底：失败码保持"会话失败"（用户文案不变），
            # 固定类别摘要进签名。
            report = _failure_only_report("session_failed", type(error).__name__, error)
        finally:
            monitor.cancel()
            try:
                await monitor
            except asyncio.CancelledError:
                pass
            await progress.drain()
        return report, executor, system_prompt_digest

    async def _invoke_executor(
        self,
        executor: WorkerTurnExecutor,
        claimed: ClaimedTask,
        context: Any,
        *,
        stop_event: asyncio.Event,
        progress: TurnProgressReporter,
    ) -> dict[str, Any]:
        """跑一次回合，并把进度回调接上去。"""
        return await executor.run_turn(
            context.prompt,
            resume_session_id=(context.agent_session_id if context.resumed_session else None),
            stop_event=stop_event,
            on_stream_event=progress.on_stream_event,
            on_tool_call=progress.on_tool_call,
            external_texts=self._config.external_texts,
            # 会话续用失配的降级审计需要任务标识；本方法是唯一持有它的调用层。
            task_id=claimed.task_id,
        )

    def _mark_side_effect(self, claimed: ClaimedTask) -> None:
        """执行层观察到不可回滚的外部副作用时回调这里。"""
        self._queue.mark_side_effect(task_id=claimed.task_id, worker_id=self._config.worker_id)

    async def _resolve_system_prompt(self, claimed: ClaimedTask) -> tuple[str | None, str | None]:
        """取本轮要用的默认提示词与它的版本摘要。

        提示词**每任务现读**：编辑挂载卷上的文件后下一条消息即生效。读不到就本任务降级为
        无提示词执行并留一条结构化告警——配置层已保证文件与进程级提示词互斥，这次覆盖
        不会吃掉任何别处配置的值。读取本身已有大小上界，但慢挂载下的一次读仍可能停顿，
        因此丢进线程池，不占事件循环（心跳与停止处理都在循环上）。

        Returns:
            ``(本轮提示词, 版本摘要)``；没有配置提示词文件时摘要为 ``None``。
        """
        if not self._config.system_prompt_file:
            return self._config.system_prompt, None
        task_system_prompt, digest, degraded = await asyncio.to_thread(
            _load_task_system_prompt, self._config.system_prompt_file
        )
        if degraded is not None:
            logger.warning(
                "worker.system_prompt.degraded reason=%s task_id=%s（本任务以无提示词执行）",
                degraded,
                claimed.task_id,
            )
        return task_system_prompt, digest

    async def _append_user_memory(self, claimed: ClaimedTask, prompt: str | None) -> str | None:
        """把这个人的记忆拼进提示词尾部。

        **失败放行，与用户 MCP 配置的失败关闭相反**：记忆查询失败或超时不应该拖累用户
        本来能拿到的问数结果，照抄提示词读取失败的降级先例——留一条结构化日志，本任务
        不带记忆继续跑，不中断任务。

        Returns:
            拼好的提示词；没有装配读取口、没有记忆或查询失败时原样返回。
        """
        if self._user_memory_reader is None:
            return prompt
        try:
            memory = await asyncio.to_thread(
                self._user_memory_reader.fetch_prompt_segment, user_id=claimed.user_id
            )
        except Exception as error:
            logger.warning(
                "worker.user_memory.degraded error=%s task_id=%s（本任务不带记忆继续执行）",
                type(error).__name__,
                claimed.task_id,
            )
            return prompt
        if memory is None or not memory.text:
            return prompt
        if memory.truncated:
            logger.warning(
                "worker.user_memory.prompt_truncated kept=%d total=%d task_id=%s",
                memory.kept_entries,
                memory.total_entries,
                claimed.task_id,
            )
        return f"{prompt}\n\n{memory.text}" if prompt else memory.text

    # ------------------------------------------------------------------
    # 事件写入与监控
    # ------------------------------------------------------------------

    def _append_event(
        self,
        claimed: ClaimedTask,
        *,
        event_type: str,
        idempotency_key_suffix: str,
        elapsed_seconds: int | None = None,
        content: str | None = None,
    ) -> None:
        """写入非终态事件；失败不中断任务执行——它是可恢复的运行信号，不是结果。

        写入前在这次调用真正携带的正文上再自查一次，不依赖队列实现是否也做了同一层校验
        （真实队列做了，测试替身未必）。自查不过与真实写入失败走同一条收口路径。
        """
        try:
            assert_content_allowed(DeliveryEventType(event_type), content)
            self._queue.append_delivery_event(
                task_id=claimed.task_id,
                worker_id=self._config.worker_id,
                event_type=event_type,
                idempotency_key=f"{claimed.task_id}:a{claimed.attempts}:{idempotency_key_suffix}",
                elapsed_seconds=elapsed_seconds,
                content=content,
            )
        except Exception as error:
            logger.error(
                "worker.delivery_event_write_failed event_type=%s error=%s",
                event_type,
                type(error).__name__,
            )

    def _finish_terminal(
        self,
        claimed: ClaimedTask,
        decision: TerminalDecision,
        *,
        outcome: TurnOutcome | None = None,
        elapsed_seconds: int = 0,
        system_prompt_digest: str | None = None,
    ) -> None:
        """写终态事件并把任务转入"等待投递"。

        话题继续占用直到投递解析，因此新建立的会话标识（只在业务成功时非空）随终态事件
        一起持久化，留到确认送达时才写回话题行——同一话题在此期间不会有第二个任务插进来
        读它，延后写入是安全的。

        本方法是全部终态写入的唯一收口点，因此低敏审计在这里记一次，覆盖停止／失败／
        拒发／成功全部分支，不必在每个分支各写一遍。
        """
        turn_outcome = outcome if outcome is not None else empty_outcome()
        self._terminal_audit.log(
            task_id=claimed.task_id,
            decision=decision,
            outcome=turn_outcome,
            system_prompt_digest=system_prompt_digest,
        )
        signature = turn_outcome.failure_signature
        safe_signature = sanitize_failure_signature(signature) if signature is not None else None
        failure_code = decision.failure_code
        self._queue.write_terminal_event(
            task_id=claimed.task_id,
            worker_id=self._config.worker_id,
            terminal_kind=decision.terminal_kind,
            error_kind=decision.error_kind,
            content=decision.content.text,
            elapsed_seconds=elapsed_seconds,
            agent_session_id=decision.session_id,
            token_usage=turn_outcome.token_usage,
            guard_denied_count=turn_outcome.guard_denied_count,
            # 失败码与签名同事务落库：worker 与 gateway 不共享文件系统，只进 stderr 的
            # 线索管理员看不到。
            failure_code=(
                _cap_log_token(str(failure_code))[0] if failure_code is not None else None
            ),
            failure_signature=safe_signature,
            document_request=decision.document_request,
            sheet_request=decision.sheet_request,
        )

    async def _monitor(
        self,
        task_id: str,
        stop_event: asyncio.Event,
        *,
        on_stall_tick: Callable[[], None] | None = None,
    ) -> None:
        """在途任务的守望循环：戳活性、传递停止信号、续心跳、驱动进度兜底。

        活性必须在**这条**循环里戳，不能只靠入口那一次：入口要等整批任务才返回，一个
        正常但较长的回合就足以让活性文件年龄超过健康检查阈值，把"完全正常、只是在忙"的
        容器打成不健康。这条循环按停止轮询间隔在跳、贯穿整个在途任务的生命周期，正是这个
        信号该来的地方。

        进程停机与用户按停止对在途回合而言是同一件事——都要求执行层尽快中断当前回合。
        """
        last_heartbeat = self._monotonic()
        while True:
            self._emit_heartbeat()
            if self._queue.stop_requested(task_id=task_id, worker_id=self._config.worker_id) or (
                self._global_stop is not None and self._global_stop.is_set()
            ):
                stop_event.set()
            now = self._monotonic()
            if now - last_heartbeat >= self._config.heartbeat_interval_seconds:
                if not self._queue.heartbeat(task_id=task_id, worker_id=self._config.worker_id):
                    return
                last_heartbeat = now
            if on_stall_tick is not None:
                try:
                    on_stall_tick()
                except Exception as error:
                    logger.error(
                        "语义化进度兜底刷新失败，任务职责继续运行 error=%s",
                        type(error).__name__,
                    )
            await self._sleep(self._config.stop_poll_interval_seconds)

    # ------------------------------------------------------------------
    # 长期循环
    # ------------------------------------------------------------------

    async def run(self, *, stop_event: asyncio.Event | None = None) -> None:
        """长期 worker 循环：有监听就用监听降延迟，没有就纯轮询。"""
        stop = stop_event or asyncio.Event()
        # 在途任务据此把"进程正在停机"与"用户按了停止"同等看待，见 :meth:`_monitor`。
        self._global_stop = stop
        try:
            if self._listener_factory is None:
                while not stop.is_set():
                    await self._poll_once(stop)
                return
            while not stop.is_set():
                await self._consume_with_listener(stop)
        finally:
            # 领取循环已经彻底退出（监听/轮询两条路径都已收口）：显式关闭本进程
            # 空闲栈里的连接，不再只靠 atexit（D-17）。挪去线程池执行，避免同步
            # 数据库调用占住事件循环；清理本身的异常只记日志，不覆盖原始故障
            # （run() 若是异常退出，让那个异常原样传播）。
            await asyncio.to_thread(self._close_idle_connections_quietly)

    @staticmethod
    def _close_idle_connections_quietly() -> None:
        """停机收尾：关闭空闲数据库连接，失败只记日志。"""
        try:
            close_idle_connections()
        except Exception as error:
            logger.error("worker 停机清理空闲数据库连接失败 error=%s", type(error).__name__)

    async def _poll_once(self, stop: asyncio.Event) -> None:
        """跑一轮；无事可做就睡一个轮询间隔。"""
        del stop
        if not await self.process_once():
            await self._sleep(self._config.poll_interval_seconds)

    async def _consume_with_listener(self, stop: asyncio.Event) -> None:
        """建一次监听并在它上面持续消费；监听断开就丢掉这条连接重建。

        监听连接是一条长期持有的独占连接，服务端掐断它（连接池回收、数据库重启、被强制
        终止）时等待会抛异常——此前这个异常一路冲出循环，整个进程退出、靠容器重启复活。
        监听只是"更早醒来"的优化，兜底轮询才是正确性来源，因此正确反应是重建监听；重建
        失败就退回纯轮询一个周期再试。领取路径自己的异常仍原样向上抛，这里不替它兜底。
        """
        try:
            listener_context = self._listener_factory()  # type: ignore[misc]
            listener = listener_context.__enter__()
        except Exception as error:
            logger.warning(
                "task_queued 监听不可用，本轮退回轮询后重试建立监听：%s", type(error).__name__
            )
            await self._poll_once(stop)
            return
        try:
            while not stop.is_set():
                if await self.process_once():
                    continue
                try:
                    await asyncio.to_thread(
                        listener.wait, timeout_seconds=self._config.poll_interval_seconds
                    )
                except Exception as error:
                    logger.warning("task_queued 监听连接断开，重建监听：%s", type(error).__name__)
                    # 退避一拍再重建：监听一建好就断（服务端持续拒绝）时不能形成
                    # "建连→监听→抛→重建"的热循环，那正是本改动要消灭的建连风暴形态。
                    await self._sleep(self._config.poll_interval_seconds)
                    break
        finally:
            listener_context.__exit__(None, None, None)


def _default_executor_factory(
    worker_config: WorkerConfig, marker: Callable[[], None]
) -> WorkerTurnExecutor:
    """默认执行器：常驻队列 worker 的取舍与一次性命令行模式不同。

    取消必须原样传播出来：默认行为会把取消吞成一份"已取消"的失败报告并正常返回，调用方
    就会把它当成真实完成的回合同步写终态——停机预算耗尽后任务本该保持运行中、交给心跳
    超时回收，不该被写成一次可能失真的失败终态。

    采集开关取自装配时的进程配置（按任务覆盖不触碰这一项）；默认关闭时执行器不构造任何
    收集器，与"默认关闭不产生额外行为"同一条纪律。
    """
    return WorkerTurnExecutor(
        worker_config,
        mark_external_side_effect=marker,
        propagate_cancellation=True,
        capture_raw_content=worker_config.innertest_content_capture_enabled,
    )


def _failure_only_report(code: str, signature: str, error: Exception) -> dict[str, Any]:
    """没有跑成回合时的报告形状：只有失败，没有任何回合事实。"""
    return {
        "turn": {"closed": False, "final_text": "", "session_id": None},
        "failure": failure_with_signature(code, signature, error),
    }


WorkerQueueConsumer = WorkerService
