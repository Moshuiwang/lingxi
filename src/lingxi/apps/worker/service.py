"""Agent worker 的队列消费、心跳、停止和终态收口。"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from lingxi.adapters.postgres_conversation import ClaimedTask, PostgresTaskQueue, TerminalTask
from lingxi.adapters.user_mcp_config import UserMcpConfigError, load_user_mcp_servers
from lingxi.apps.worker.config import WorkerConfig
from lingxi.apps.worker.session_cleanup import delete_agent_session_files
from lingxi.apps.worker.turn import WorkerTurnExecutor
from lingxi.config.content import ContentCatalog, RenderedContent, default_content_catalog
from lingxi.core.delivery.ports import TerminalKind

logger = logging.getLogger(__name__)

# `claim()` 前重判停机信号需要跨过 `loop.add_signal_handler` 自管道机制的完整
# 投递链路（PR #173 独立复核第二轮 P2-1）：OS 送达信号 → 自管道被写入一个
# 字节 → 事件循环下一轮 `select()` 才发现可读，把**自管道 reader 回调**排进
# 就绪队列并在同一轮执行 → 该回调再把**真正的用户回调**（这里是
# `stop.set()`）通过 `_add_callback_signalsafe` 排进下一轮就绪队列 → 再下一轮
# 才真正执行。经真实 SIGTERM + 真实 `add_signal_handler` 实测（5/5、3/3 稳定
# 复现，见该轮评论）：`await asyncio.sleep(0)` 只能让当前协程前进一轮，必须
# 连续让出 **3 轮**才能观测到 `is_set()` 变为 True——上一版只让出一次，对
# 真实信号无效（对 `loop.call_soon` 模拟的假信号有效，因为它跳过了自管道的
# 两跳，这也是为什么当时新增用例是绿的、生产路径依旧漏的原因）。这里给到
# 5 轮，在实测必需的 3 轮之上留出实现细节漂移的余量；一旦提前观测到
# `is_set()` 就立即返回，不多空转。
_STOP_SIGNAL_DRAIN_YIELDS = 5


class QueueListener(Protocol):
    def wait(self, *, timeout_seconds: float) -> bool: ...


ExecutorFactory = Callable[[WorkerConfig, Callable[[], None]], Any]
HeartbeatCallback = Callable[[], None]
TaskStuckCallback = Callable[[str, int], None]
# 终态收口低敏审计事件（Issue #90 评论 5306860255 的独立复核 P1）：字段名与取值
# 见 ``WorkerService._log_terminal_outcome``。``WorkerService`` 是纯组装对象，
# 不知道自己会被哪个进程入口装配，也不该假设 stdlib ``logging`` 有 handler——
# 真实队列 worker 的 `apps/worker/cli.py` 刻意从不调用 `logging.basicConfig()`
# （见该文件 `_LogOnlyAlertSender` 的说明：未配置 handler 时默认阈值
# `WARNING` 会把 `logging.info(...)` 悄悄吞掉），因此这条低敏审计事件必须像
# `heartbeat`/`on_task_stuck`/`on_alert_tick` 一样由装配层注入真正的输出出口，
# 不能自己直接调 `logging`。
TerminalOutcomeCallback = Callable[[Mapping[str, object]], None]
_MAX_LOG_TOKEN_CHARS = 64


def _cap_log_token(value: str) -> tuple[str, bool]:
    """把失败码/安全原因码这类短标识截到审计安全的长度上界（Issue #90 评论
    5306860255 的独立复核 P3-2）。这些值目前全部来自本仓库固定的枚举式常量
    （``turn.py``/``report.py`` 的失败码、``input_safety`` 的原因码），不是模型
    输出，但收口日志是低敏审计的唯一出口——不给未来新增码值设长度上界，就是
    给"某次改动不小心把一段自由文本塞进这个字段"留了一条不设防的泄漏面。
    """

    if len(value) <= _MAX_LOG_TOKEN_CHARS:
        return value, False
    return value[:_MAX_LOG_TOKEN_CHARS], True


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
        session_root: Path | None = None,
        session_cleanup_batch_limit: int = 20,
        on_alert_tick: Callable[[], None] | None = None,
        on_terminal_outcome: TerminalOutcomeCallback | None = None,
    ) -> None:
        self._config = config
        self._queue = queue
        self._executor_factory = executor_factory or (
            lambda worker_config, marker: WorkerTurnExecutor(
                worker_config,
                mark_external_side_effect=marker,
                # 常驻 queue worker 必须让 SIGTERM 停机预算耗尽时的取消原样传播
                # 出来（PR #173 独立复核 P1-3）：默认行为（一次性 turn 模式 CLI
                # 那份）会把取消吞成一份"已取消"的失败报告并正常返回，
                # `_process_task` 就会把它当成真实完成的回合同步写终态——预算
                # 耗尽后任务本该保持 `running`、交给心跳超时回收
                # （`reclaim_stale_with_outcomes`，已在 #90/#151 验证），不该被
                # 写成一次可能失真的 FAILED 终态。见 `WorkerTurnExecutor.run_turn`
                # 里 `propagate_cancellation` 的完整说明。
                propagate_cancellation=True,
            )
        )
        self._listener_factory = listener_factory
        self._catalog = catalog or default_content_catalog()
        self._sleep = sleep
        self._monotonic = monotonic
        self._heartbeat = heartbeat
        self._on_task_stuck = on_task_stuck
        # Agent 会话 JSONL 物理清理（Issue #153）：``None`` 表示当前环境没有可用的
        # 会话根目录（例如缺 ``HOME``），此时 ``_cleanup_agent_sessions`` 整体跳过，
        # 不触碰清理队列——留着排队等下一个配置正确的进程来处理，而不是假装已清理。
        self._session_root = session_root
        self._session_cleanup_batch_limit = session_cleanup_batch_limit
        # 告警状态机的恢复计时与重试投递都需要被定期"戳一下"（Issue #153）；worker
        # 没有 scheduler 那种专门的定时职责循环，借用每轮收口顺便调用。
        self._on_alert_tick = on_alert_tick
        # 终态收口低敏审计事件的真正输出出口（Issue #90 评论 5306860255 独立复核
        # P1）：``None`` 时 `_log_terminal_outcome` 整体跳过——没有装配方就没有
        # 输出，不假装写了一条实际被吞掉的日志。
        self._on_terminal_outcome = on_terminal_outcome
        # SIGTERM 收到后由 run() 设置：在途任务的 `_monitor` 据此把"进程正在停机"
        # 与"用户发了 /stop"同等看待，主动请求 Agent SDK 中断当前回合（Issue #153
        # 完成标准第 3 条）。默认 None，保持 process_once()/白盒测试的既有行为不变。
        self._global_stop: asyncio.Event | None = None

    async def process_once(self) -> bool:
        """做一轮回收、领取和执行；返回这一轮是否观察到任务。"""

        self._emit_heartbeat()
        self._tick_alerts()
        terminal_tasks = self._housekeep()

        # 再判一次停机信号，紧贴在 claim() 之前（PR #173 独立复核 P2-1，第二轮
        # 复核证明单次 `sleep(0)` 对真实信号无效，见模块顶部
        # `_STOP_SIGNAL_DRAIN_YIELDS` 的机制说明）：`run()` 的
        # `while not stop.is_set()` 判定与这里的 `claim()` 之间没有任何 await
        # （心跳、告警 tick、`_housekeep()` 全是同步代码），信号如果恰好在这段
        # 同步窗口内被操作系统送达，`self._global_stop.is_set()` 在没有真正让
        # 事件循环跑完自管道投递链路之前读到的都是旧值——会把一条还在排队、
        # 从未执行过的任务领走，直接收口成 `stopped` 且不会被重排。因此连续
        # 让出 `_STOP_SIGNAL_DRAIN_YIELDS` 轮（一旦提前观测到 `is_set()` 立即
        # 返回，不多空转），让"停止新领取"（#153 完成标准第 3 条）在这个窗口
        # 里也真正成立。`self._global_stop` 为 ``None`` 时（`process_once()`
        # 的白盒调用方与既有测试）跳过整段，行为不变、不多一次调度。
        if self._global_stop is not None:
            for _ in range(_STOP_SIGNAL_DRAIN_YIELDS):
                await asyncio.sleep(0)
                if self._global_stop.is_set():
                    return bool(terminal_tasks)

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
            # 独立于"排队太久"（queued_stuck）：这一类是"目标 worker 版本压根没有
            # 可用实例"，运维需要看到的诊断动作不同（部署缺一个版本 vs 单纯积压），
            # 因此用独立的告警类型（Issue #153 最小可观测性第四类）。
            self._report_task_stuck("worker_version_unavailable", len(unavailable))
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
            # 与 core.alerting.AlertKind.AWAITING_DELIVERY_STUCK 对齐（Issue #153
            # 最小可观测性第二类：queued/running/awaiting-delivery 滞留三选一）。
            self._report_task_stuck("awaiting_delivery_stuck", len(expired))
        self._cleanup_agent_sessions()
        return terminals

    def _cleanup_agent_sessions(self) -> None:
        """认领并物理删除一批到期的 Agent 会话 JSONL（Issue #153）。

        没有配置可用的会话根目录，或队列适配器不支持这组方法（旧测试用的假队列）
        时整体跳过——不半途认领又做不了事，让请求继续排队给下一个真正能处理它的
        进程。单条删除失败不清 ``done_at``：下一轮的十分钟软领取窗口会重试，见迁移
        0061 头部注释。
        """

        if self._session_root is None:
            return
        claim = getattr(self._queue, "claim_session_cleanups", None)
        mark_done = getattr(self._queue, "mark_session_cleanups_done", None)
        if claim is None or mark_done is None:
            return
        try:
            pending = claim(limit=self._session_cleanup_batch_limit)
        except Exception as error:  # noqa: BLE001 - 清理认领失败不能带走任务职责
            logger.error("Agent 会话清理队列认领失败 error=%s", type(error).__name__)
            return
        if not pending:
            return
        # 根目录本身不存在与"根目录存在、这个会话确实没有文件"是两件不同的事
        # （PR #173 独立复核 P2-6）：`delete_agent_session_files` 对两者都返回
        # `0`（幂等设计，理由见该函数文档），但把这两种情况合并成同一个"标记
        # 完成"分支是错的——`.env.example` 里一个写错的 `LINGXI_WORKER_
        # SESSION_ROOT`（例如示例值 `/var/lib/lingxi/users/.claude/projects`，
        # 而镜像固定 `HOME=/tmp`）会让这一批本该被清理的会话被静默标记完成；
        # `agent_session_cleanup.agent_session_id` 是唯一索引 +
        # `ON CONFLICT DO NOTHING`，标记完成的行不会被重新排队，事后改对配置
        # 也补不回来。这里已经认领（`claimed_at`/`worker_id` 已写），因此不能
        # 简单地什么都不做就返回——十分钟软领取窗口本来就是为这类"认领了但
        # 这次处理不了"设计的重试兜底，只需要不调用 `mark_done` 即可让它到点
        # 被下一个进程重新认领。
        if not self._session_root.is_dir():
            logger.error(
                "Agent 会话清理根目录不存在，本轮跳过、不标记完成 pending=%d",
                len(pending),
            )
            return
        done_ids: list[str] = []
        for item in pending:
            try:
                delete_agent_session_files(self._session_root, item.agent_session_id)
            except Exception as error:  # noqa: BLE001 - 单条失败不影响本轮其余条目
                logger.error(
                    "Agent 会话 JSONL 物理删除失败 reason=%s error=%s",
                    item.reason,
                    type(error).__name__,
                )
                continue
            done_ids.append(item.id)
        if done_ids:
            try:
                mark_done(ids=done_ids)
            except Exception as error:  # noqa: BLE001 - 标记失败只影响是否重试，不影响正确性
                logger.error("Agent 会话清理标记完成失败 error=%s", type(error).__name__)

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

    def _tick_alerts(self) -> None:
        """定期戳一下告警状态机的恢复计时与投递重试（Issue #153）。

        worker 没有 scheduler 那种独立的定时职责循环，借用队列消费循环本身每轮
        调用一次；戳的频率因此等于 ``poll_interval_seconds``（默认 2 秒），比
        scheduler 的告警职责频率高，但告警状态机本身对调用频率不敏感（去重、
        阈值判定都基于时间戳而非调用次数）。
        """

        if self._on_alert_tick is None:
            return
        try:
            self._on_alert_tick()
        except Exception as error:  # noqa: BLE001 - 告警自身失败不能带走任务职责
            import logging

            logging.getLogger(__name__).error(
                "worker 告警状态机推进失败，任务职责继续运行 error=%s", type(error).__name__
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
            # Epic D 闸⑥红线：每个用户的问数必须用他自己的那份 MCP 配置，绝不
            # 回退到全进程共用的 self._config.mcp_servers——回退意味着用户 A
            # 的问数用了一份不属于他的令牌去查数，是越权返回数据。这里**结构性
            # 地没有回退分支**：读取失败（UserMcpConfigError）在下面单独一支
            # except 里收口成失败报告，从不构造 executor、从不调用 run_turn，
            # self._config（携带全进程共用配置）不会被传给 _executor_factory。
            # 唯一能让任务真正执行的路径，是读到了这个用户自己的配置并用它
            # 覆盖出一份 task_config。
            user_mcp_servers = load_user_mcp_servers(
                root=self._config.user_env_root or "", user_id=claimed.user_id
            )
            task_config = replace(self._config, mcp_servers=user_mcp_servers)
            executor = self._executor_factory(task_config, marker)
            report = await executor.run_turn(
                context.prompt,
                resume_session_id=(
                    context.agent_session_id if context.resumed_session else None
                ),
                stop_event=stop_event,
                on_stream_event=on_stream_event,
                external_texts=self._config.external_texts,
            )
        except UserMcpConfigError as error:
            # error.code 是本模块自定的安全码（不含路径 / 内容 / 令牌），供运维
            # 诊断具体失败原因（例如用户还没走完首次开通、配置文件形状不对）。
            report = {
                "turn": {"closed": False, "final_text": "", "session_id": None},
                "failure": {
                    "code": "user_mcp_config_unavailable",
                    "message": f"user_mcp_config:{error.code}",
                },
            }
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

        # 这里刻意**不再**回读队列侧的 stop 标志（此前是
        # `stop_requested = stop_event.is_set() or self._queue.stop_requested(...)`）：
        # 终态只由这一轮实际发生的事实决定，见下方 `stop_is_the_outcome` 的说明。
        # 留着它就是留一个没人用的每回合额外查询和一个会诱使后来者再次「用 stop
        # 覆盖终态」的现成变量。停止在途回合仍由 `_monitor` 置位 `stop_event`
        # 驱动执行层中断，那条路径没有改动。
        turn = report.get("turn") or {}
        failure = report.get("failure") or {}
        failure_code = failure.get("code") if isinstance(failure, Mapping) else None
        final_text = turn.get("final_text") if isinstance(turn, Mapping) else ""
        final_text = final_text if isinstance(final_text, str) else ""
        elapsed_seconds = int(max(0.0, self._monotonic() - started_at))

        output_safety = turn.get("output_safety") if isinstance(turn, Mapping) else None
        withheld = bool(isinstance(output_safety, Mapping) and output_safety.get("withheld"))
        # withheld 只对"本来会成功交付内容"的回合有意义：一个超时/失败/未收口的
        # 回合无论正文如何都不会交付 final_text，其终态必须保留真实失败原因。
        # 此前 withheld 分支排在失败判定之前（独立审核 F1）：失败回合的残余正文
        # 一旦触发出口安全（真实泄露片段或受控 canary 注入都可能），真超时就会被
        # 改写成 redacted_withheld——运维丢失真实失败终态，验收拿到假阳性证据。
        deliverable = bool(turn.get("closed")) and not failure

        # 终态优先级（Issue #195）：真中断 → 其他失败 → withheld → 成功。
        # 此前第一分支是 `stop_requested or failure_code == "interrupted"`，
        # 让一次**并发到达**的 stop 压过所有已经发生的事实：
        #   1. 回合已因 `turn_timeout`/`drain_timeout`/`session_failed` 失败，
        #      终态被改写成 `stopped`，残余正文还会经 `worker.stopped_result`
        #      交付——真实失败原因丢失，与本文件「失败终态保留真实失败原因」
        #      的既有约定（#186 F1）直接冲突；
        #   2. 回合已经 `closed=True` 出结果，晚到的 stop 把成功降级成
        #      `stopped`，只有成功分支才写的 `session_id` 一并丢失，用户拿不到
        #      已经产出的结果、也无法在会话内追问——踩「重启与重试不得造成
        #      用户结果丢失」这条红线。
        # 因此 `stopped` 只认执行层给出的**真中断**：`interrupted` 是
        # `adapters/claude_agent_session.py` 观测到本地 `stop_event` 已置位、
        # 调用过 `client.interrupt()` 之后才抛出的 `AgentSessionInterrupted`
        # （`turn.py` 映射），它是这条链路上唯一带因果的「这一轮真的被 stop
        # 打断了」信号。队列侧的 `stop_requested` 只说明「某一刻有人请求过停止」，
        # 不说明这一轮的结果由它决定，因此**不参与**终态选择。
        #
        # 尤其不能用「没有失败码」反推成 stop（codex 一级独立审查 P1-1）：
        # `closed=False` 本身就是失败事实，`failure_code is None` 只是没人给它
        # 起名字（屏障失效 `gate_bypassed`、failure 映射缺 `code` 都是这种形状）。
        # 也不能把 `cancelled` 当成 stop 的别名（同审查 P1-2）：它来自
        # `turn.py/_sdk_termination_failure` 对 SDK 自报的
        # `aborted_streaming`/`aborted_tools`，与本地 `stop_event` 之间没有任何
        # 因果绑定，SDK 完全可能在没人 stop 的时候自行 abort——认它就等于让一次
        # 晚到的 stop 掩盖真实的 SDK 终止失败。
        #
        # 注意开工前就带着 stop_requested 的任务在 `_process_task` 开头就已收口
        # 成 `stopped`，根本不会走到这里；这里处理的只有「执行途中 stop 与回合
        # 终点赛跑」这一种情况。
        stop_is_the_outcome = failure_code == "interrupted"

        if stop_is_the_outcome:
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
                failure_code=failure_code,
                output_safety=output_safety,
            )
        elif not deliverable:
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
                failure_code=failure_code,
                output_safety=output_safety,
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
                failure_code=failure_code,
                output_safety=output_safety,
            )
        else:
            # 走到这里必然 `deliverable and not withheld`：回合已收口、有结果、
            # 没有失败。即使 stop 晚到，这份已经产出的结果照常交付，
            # `session_id` 照常持久化（Issue #195）。
            self._finish_terminal(
                claimed,
                terminal_kind=TerminalKind.SUCCESS.value,
                error_kind=None,
                content=RenderedContent(key="worker.result", version=self._catalog.version, text=final_text),
                elapsed_seconds=elapsed_seconds,
                session_id=turn.get("session_id") if isinstance(turn, Mapping) else None,
                failure_code=failure_code,
                output_safety=output_safety,
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
        failure_code: object = None,
        output_safety: Mapping[str, Any] | None = None,
    ) -> None:
        """写终态事件、把任务转入 ``awaiting_delivery``（Issue #151 状态合同第 2
        条）。话题继续占用直到投递解析，因此新建立的 ``session_id``（只在业务
        成功时非空）随终态事件一起持久化，留到确认送达时才写回
        ``conversation.agent_session_id``——同一话题在此期间不会有第二个任务插进
        来读它，延后写入是安全的（见 ``core.delivery.ports`` 与
        ``PostgresTaskQueue.confirm_delivery`` 的取舍说明）。

        本方法是 ``_process_task`` 里所有终态写入的唯一收口点，因此低敏审计日志
        （Issue #90 评论 5306860255）放在这里记一次，覆盖 stop/withheld/success/
        failure 全部分支，不必在每个分支各写一遍。
        """

        self._log_terminal_outcome(
            task_id=claimed.task_id,
            failure_code=failure_code,
            error_kind=error_kind,
            terminal_kind=terminal_kind,
            output_safety=output_safety,
        )
        self._queue.write_terminal_event(
            task_id=claimed.task_id,
            worker_id=self._config.worker_id,
            terminal_kind=terminal_kind,
            error_kind=error_kind,
            content=content.text,
            elapsed_seconds=elapsed_seconds,
            agent_session_id=session_id,
        )

    def _log_terminal_outcome(
        self,
        *,
        task_id: str,
        failure_code: object,
        error_kind: str | None,
        terminal_kind: str,
        output_safety: Mapping[str, Any] | None,
    ) -> None:
        """queue 收口低敏结构化审计事件（Issue #90 评论 5306860255）：queue 链路
        此前失败码与安全命中规则完全不可回读，r13 只能靠猜直接原因。这里只记
        分类性的失败码、落库 ``error_kind``、``terminal_kind`` 与安全判定的
        布尔/原因码——**严禁**记录正文内容、用户 open_id、prompt 或模型输出
        片段。

        独立复核 P1：这条事件不能直接调用 stdlib ``logging``——``WorkerService``
        不知道自己会被哪个进程入口装配，而真实队列 worker 的 ``apps/worker/
        cli.py`` 刻意从不调用 ``logging.basicConfig()``（未配置 handler 时默认
        阈值 ``WARNING`` 会把 ``logging.info(...)`` 悄悄吞掉，运维在真实容器
        stderr 里永远看不到）。因此改为调用装配层注入的 ``on_terminal_outcome``
        回调，由 ``cli.py`` 接到本文件既有的结构化 stderr 出口（``worker.task.
        terminal``，带 ``trace_id``）。没有装配方（``None``，例如白盒测试与旧
        调用方）时整体跳过，不假装写了一条实际不存在的日志。
        """

        if self._on_terminal_outcome is None:
            return

        blocked = bool(isinstance(output_safety, Mapping) and output_safety.get("blocked"))
        withheld = bool(isinstance(output_safety, Mapping) and output_safety.get("withheld"))
        reasons: tuple[str, ...] = ()
        if isinstance(output_safety, Mapping):
            raw_reasons = output_safety.get("reasons")
            if isinstance(raw_reasons, (list, tuple)):
                reasons = tuple(str(reason) for reason in raw_reasons)

        # P3-2：失败码与每个原因码入日志前截到长度上界，避免未来某次改动不小心
        # 把自由文本塞进这两个字段时，审计日志变成新的正文泄漏面。
        truncated = False
        capped_failure_code: str | None = None
        if failure_code is not None:
            capped_failure_code, code_truncated = _cap_log_token(str(failure_code))
            truncated = truncated or code_truncated
        capped_reasons: list[str] = []
        for reason in reasons:
            capped_reason, reason_truncated = _cap_log_token(reason)
            capped_reasons.append(capped_reason)
            truncated = truncated or reason_truncated

        fields = {
            "task_id": task_id,
            "failure_code": capped_failure_code,
            "error_kind": error_kind,
            "terminal_kind": terminal_kind,
            "output_safety_blocked": blocked,
            "output_safety_withheld": withheld,
            "output_safety_reasons": tuple(capped_reasons),
            "truncated": truncated,
        }
        try:
            self._on_terminal_outcome(fields)
        except Exception as error:  # noqa: BLE001 - 观测失败不能带走任务职责，参照 _append_event
            logger.error(
                "终态收口审计事件回调失败，任务收口继续 error=%s", type(error).__name__
            )

    async def _monitor(self, task_id: str, stop_event: asyncio.Event) -> None:
        last_heartbeat = self._monotonic()
        while True:
            # 活性文件必须在这条循环里戳，不能只靠 `process_once()` 开头那一次
            # （PR #173 独立复核 P1-5）：`process_once()` 会 `await
            # asyncio.gather(...)` 等完整批任务才返回，一个正常但较长的回合
            # （`turn_timeout_seconds` 默认 600s）足以让活性文件年龄超过 worker
            # 角色的健康检查阈值（默认 60s），把"完全正常、只是在忙"的容器打成
            # unhealthy。`_monitor` 本来就按 `stop_poll_interval_seconds`
            # （默认 1s）在跳、贯穿整个在途任务的生命周期，是"进程仍在做正确的
            # 事"这个信号真正应该来源的地方；每跳一次戳一次是本地文件写入，
            # 成本可忽略。真实证据：`tests/test_worker_queue_consumer.py` 的
            # ``test_liveness_stays_fresh_through_a_long_in_flight_turn`` ——
            # 删掉这一行后该用例会变红。
            self._emit_heartbeat()
            # SIGTERM 与用户 `/stop` 对在途回合而言是同一件事——都要求 Agent SDK
            # 尽快中断当前回合（Issue #153 完成标准第 3 条："通知在途回合停止"）。
            # `self._global_stop` 只在 `run()` 收到停止信号时被置位，process_once()
            # 的白盒调用方与既有测试不传它，行为不变。
            if self._queue.stop_requested(
                task_id=task_id, worker_id=self._config.worker_id
            ) or (self._global_stop is not None and self._global_stop.is_set()):
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
        if code == "max_turns_exceeded":
            # Issue #90 评论 5306860255：turn 模式（apps/worker/turn.py 的
            # `_sdk_termination_failure`）早已把撞满 Agent 轮数上限分类为
            # `max_turns_exceeded`，但 queue 收口此前落进这里的默认分支，
            # 被压平成通用 `session_failed` 文案——用户看到的是「请稍后重试」，
            # 而重试对"问题本身步骤太多"这种失败原因没有意义。这里给它一个
            # 独立、可查询的 error_kind 和产品负责人定稿的专属文案。
            return "max_turns_exceeded", self._catalog.text("worker.max_turns")
        return "session_failed", self._catalog.text("worker.failed")

    async def run(self, *, stop_event: asyncio.Event | None = None) -> None:
        stop = stop_event or asyncio.Event()
        # 见 `_monitor`：在途任务据此把"进程正在停机"与"用户发了 /stop"同等看待。
        self._global_stop = stop
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
