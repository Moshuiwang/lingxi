"""``python -m lingxi.apps.worker`` 的命令行外壳。

``LINGXI_WORKER_MODE=turn`` 执行一次受控回合；``queue`` 启动长期队列消费者。队列
模式只负责数据库任务状态与执行生命周期，真实外部出站 transport 由应用装配层注入。

输出契约（受控验证要引用它，因此写死在这里）：

- **stdout**：恰好一个 JSON 对象，就是回合报告。配置错误时也是一个 JSON 对象，
  只是 ``turn`` 为空、``failure.code`` 为 ``config_error``。
- **stderr**：结构化日志，每行一个 JSON 对象，都带 ``trace_id``。不写日志文件、
  不自行轮转（`V-部署-04`）。
- **退出码**：0 回合正常收口；2 回合跑完但没收口（正文为空、终止结果不是恰好一次、
  SDK 终止消息自报错误）；3 配置错误；4 会话失败（含 SDK 未安装）；
  5 检测到绕过屏障的调用（`ungated_count > 0`，hook 未触发的唯一可观察形状）。

日志里刻意不出现问题原文与最终正文，只出现字节数、计数与状态：受控验证的证据
"只保留事件类型、计数、状态、长度、哈希和脱敏摘要"（Issue #37 验证与证据）。
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

from lingxi.core.execution.audit import redact_free_text
from lingxi.core.ids import is_ulid, new_ulid
from lingxi.adapters.postgres_conversation import PostgresTaskQueue, PostgresTaskQueueListener

from lingxi.apps.liveness import touch_liveness

from .config import ENV_PREFIX, WorkerConfig, WorkerConfigError, load_config
from .report import config_error_report
from .session_cleanup import default_session_root
from .turn import WorkerTurnExecutor
from .service import WorkerService

EXIT_OK = 0
EXIT_TURN_NOT_CLOSED = 2
EXIT_CONFIG_ERROR = 3
EXIT_SESSION_FAILED = 4
EXIT_GATE_BYPASSED = 5


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    del argv  # 本切片没有命令行参数：全部输入走 LINGXI_ 前缀环境变量。
    env = os.environ if env is None else env
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr

    try:
        queue_mode = env.get("LINGXI_WORKER_MODE", "turn").strip().lower() == "queue"
        config = load_config(env, require_question=not queue_mode)
    except WorkerConfigError as error:
        provided_trace_id = env.get("LINGXI_WORKER_TRACE_ID", "")
        # 只有合法 ULID 才复用：误接进来的令牌不得随错误输出外泄（Codex 复查）。
        trace_id = provided_trace_id if is_ulid(provided_trace_id) else new_ulid()
        # 配置错误文案可能回显运维写串的原值（令牌形态也拦不住），与模型侧
        # 同一标准：出口过自由文本脱敏并截断（独立复查发现）。
        message = redact_free_text(str(error))[:300]
        _log(err, trace_id, "error", "worker.config.invalid", message=message)
        _emit(out, config_error_report(trace_id=trace_id, message=message))
        return EXIT_CONFIG_ERROR

    if config.output_safety_canary is not None:
        # 显眼的结构化告知（与 gateway 卡片故障注入开关同一纪律，PR #183 先例）：
        # 这个开关一旦被遗忘在开启状态，每一条问数结果都会被强制改写成安全遮蔽
        # 或 withheld 终态——必须在启动日志里足够扎眼。默认关闭时本分支不执行。
        _log(
            err,
            config.trace_id,
            "warning",
            "worker.output_safety_canary_enabled",
            mode=config.output_safety_canary,
            message=(
                "此开关仅供 S-A-07 受控验收使用，默认应为关闭；如果这不是一次"
                "受控验收启动，请立即核实并清空 LINGXI_WORKER_OUTPUT_SAFETY_CANARY"
            ),
        )

    if queue_mode:
        dsn = env.get("LINGXI_POSTGRES_DSN", "").strip()
        if not dsn:
            message = "队列 worker 缺少 LINGXI_POSTGRES_DSN"
            _log(err, config.trace_id, "error", "worker.queue.config.invalid", message=message)
            _emit(out, config_error_report(trace_id=config.trace_id, message=message))
            return EXIT_CONFIG_ERROR
        # Issue #177：工作目录预检必须在宣告"队列 worker 已启动"之前完成——否则
        # 一条 worker.queue.start 日志会紧跟着一条启动失败，误导成"先启动、后
        # 失败"，而实际上这个进程从未真正进入过可用状态。
        if config.workspace is not None and not _ensure_worker_workspace(
            config.workspace, err=err, trace_id=config.trace_id
        ):
            message = (
                f"{ENV_PREFIX}WORKSPACE 不可用：既不存在也无法创建，或存在但不是"
                "可写目录"
            )
            _emit(out, config_error_report(trace_id=config.trace_id, message=message))
            return EXIT_CONFIG_ERROR
        _log(
            err,
            config.trace_id,
            "info",
            "worker.queue.start",
            worker_id=config.worker_id,
            target_worker_version=config.target_worker_version,
            max_concurrency=config.max_concurrency,
        )
        queue = PostgresTaskQueue(dsn)
        alerting_duty = _build_alerting_duty(err=err, trace_id=config.trace_id)
        session_root = _resolve_session_root(config, env)
        if session_root is None:
            _log(
                err,
                config.trace_id,
                "error",
                "worker.queue.session_cleanup.unconfigured",
                message="取不到会话根目录（HOME 未设置且未显式配置 "
                "LINGXI_WORKER_SESSION_ROOT），Agent 会话 JSONL 物理清理本次运行将不生效",
            )
        service = WorkerService(
            config=config,
            queue=queue,
            listener_factory=lambda: PostgresTaskQueueListener(dsn),
            heartbeat=_combined_heartbeat(alerting_duty, "worker"),
            on_task_stuck=alerting_duty.task_stuck_callback(),
            on_alert_tick=alerting_duty.run_once,
            on_terminal_outcome=_terminal_outcome_sink(err=err, trace_id=config.trace_id),
            session_root=session_root,
            session_cleanup_batch_limit=config.session_cleanup_batch_limit,
        )
        try:
            asyncio.run(
                _run_queue_worker(
                    service,
                    shutdown_timeout_seconds=config.shutdown_timeout_seconds,
                    err=err,
                    trace_id=config.trace_id,
                )
            )
        except KeyboardInterrupt:
            return 0
        return 0

    _log(
        err,
        config.trace_id,
        "info",
        "worker.turn.start",
        read_only_tool=config.read_only_tool,
        question_bytes=len(config.question.encode("utf-8")),
        mcp_servers=sorted(config.mcp_servers),
        workspace_configured=config.workspace is not None,
        max_turns=config.max_turns,
        turn_timeout_seconds=config.turn_timeout_seconds,
    )

    report = asyncio.run(
        WorkerTurnExecutor(config, stderr_stream=err).run_turn(
            config.question,
            external_texts=config.external_texts,
        )
    )
    turn = report["turn"]
    resources = report["resources"]
    usage = resources["usage"]
    gate_bypassed = report["audit"]["ungated_count"] > 0
    _log(
        err,
        config.trace_id,
        "error" if (report["failure"] or gate_bypassed or not turn["closed"]) else "info",
        "worker.turn.finished",
        closed=turn["closed"],
        user_result=turn["user_result"],
        terminal_result_count=turn["terminal_result_count"],
        sdk_result_message_count=turn["sdk_result_message_count"],
        sdk_result_is_error=turn["sdk_result_is_error"],
        sdk_result_subtype=turn["sdk_result_subtype"],
        sdk_terminal_reason=turn["sdk_terminal_reason"],
        termination_state=turn["termination_state"],
        termination_reason=turn["termination_reason"],
        guard_triggered=turn["guard_triggered"],
        duration_seconds=resources["duration_seconds"],
        agent_turns=resources["agent_turns"],
        tool_call_count=resources["tool_call_count"],
        executed_tool_call_count=resources["executed_tool_call_count"],
        usage_status=usage["status"],
        usage_source=usage["source"],
        usage_fields=usage.get("fields"),
        gate_bypassed=gate_bypassed,
        final_text_bytes=turn["final_text_bytes"],
        call_count=report["audit"]["call_count"],
        denied_count=report["audit"]["denied_count"],
        failed_count=report["audit"]["failed_count"],
        ungated_count=report["audit"]["ungated_count"],
        failure=report["failure"],
    )
    _emit(out, report)

    if gate_bypassed:
        # 安全边界失效优先于一切其他失败态：绕过之后又超时/抛错时，受控验证
        # 必须先看到 5 而不是通用的 4（终轮 Codex 复查发现）。
        return EXIT_GATE_BYPASSED
    if report["failure"]:
        return EXIT_SESSION_FAILED
    return EXIT_OK if turn["closed"] else EXIT_TURN_NOT_CLOSED


class _LogOnlyAlertSender:
    """worker 的告警发送出口（Issue #153）：只记结构化日志，从不发起网络请求。

    Worker 不获得飞书出站密钥是架构设计自身的进程职责划分（代码框架第一节
    `apps/worker` 的依赖声明；产品合同正文未规定各进程各自持有哪些凭据，
    2026-08-19 归属核对更正，见 Issue #238），因此不能像 gateway/scheduler
    那样把告警直接发进管理群——真正的跨进程
    可观测需要一个 DB 载体（登记见当前能力，留 S9）。这里仍然装配完整的
    ``AlertManager``/``AlertingDuty`` 状态机（阈值、去重、恢复计时都真实生效），
    只是"发送"这一步落到结构化日志，运维可以从容器日志或未来接入的日志聚合里
    看到它——不是没有告警，是告警路由目前只到日志这一层。

    与本模块其余输出同一惯例——写 ``_log()`` 的结构化 JSON 行到 ``err``，不用
    stdlib ``logging``：``main()`` 从不调用 ``logging.basicConfig()``（它的日志
    完全由这里的显式 ``_log()`` 调用驱动），经由 stdlib ``logging`` 发出的调用
    在没有配置 handler 时会被默认阈值（WARNING）和 lastResort 处理器悄悄吞掉，
    看起来像是"告警从未触发"，其实只是没接上出口。
    """

    def __init__(self, *, err: TextIO, trace_id: str) -> None:
        self._err = err
        self._trace_id = trace_id

    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None:
        del chat_id, dedupe_key
        _log(self._err, self._trace_id, "warning", "worker.alert", text=text)


class _StructuredAuditSink:
    def __init__(self, *, err: TextIO, trace_id: str) -> None:
        self._err = err
        self._trace_id = trace_id

    def record(self, action: str, /, **fields: object) -> None:
        _log(self._err, self._trace_id, "info", f"worker.audit.{action}", **fields)


def _terminal_outcome_sink(*, err: TextIO, trace_id: str) -> Callable[[Mapping[str, Any]], None]:
    """把 ``WorkerService`` 的终态收口低敏审计事件接到本文件的结构化 stderr 出口
    （Issue #90 评论 5306860255 的独立复核 P1）。

    ``WorkerService`` 是纯组装对象，不知道自己会被哪个入口装配，也不该假设
    stdlib ``logging`` 有 handler——``main()`` 从不调用 ``logging.
    basicConfig()``（见 ``_LogOnlyAlertSender`` 的说明），经由 ``logging``
    发出的调用在真实队列 worker 进程里会被默认阈值悄悄吞掉，运维在容器 stderr
    里永远看不到。这里复用现成的结构化 ``_log()`` 出口，让终态收口事件真正
    落到运维能看到的地方，并带上 ``trace_id``（代码框架「三、横切约定」）。
    """

    def sink(fields: Mapping[str, Any]) -> None:
        _log(err, trace_id, "info", "worker.task.terminal", **fields)

    return sink


def _build_alerting_duty(*, err: TextIO, trace_id: str) -> Any:
    """装配一个只走日志出口的 :class:`~lingxi.core.alerting.AlertingDuty`。

    延迟 import：``core.alerting`` 不是队列消费之外路径（``turn`` 模式受控验证）
    的依赖，保持函数内 import 与本文件其余外部依赖同一惯例。
    """

    from lingxi.core.alerting import AlertDispatcher, AlertingDuty, AlertManager

    return AlertingDuty(
        manager=AlertManager(),
        dispatcher=AlertDispatcher(
            sender=_LogOnlyAlertSender(err=err, trace_id=trace_id),
            # 只是一个稳定标识，不是真实投递目标——LogOnlyAlertSender 从不读它。
            chat_id="worker-log-only",
        ),
        audit=_StructuredAuditSink(err=err, trace_id=trace_id),
    )


def _combined_heartbeat(alerting_duty: Any, liveness_role: str) -> Callable[[], None]:
    """把"记进 AlertManager"与"戳一下活性文件"合成一个心跳回调（Issue #153）。

    与 ``apps/gateway/__init__.py``、``apps/scheduler/__init__.py`` 的同名函数
    同一形状，见那两处的说明。
    """

    beat = alerting_duty.heartbeat_callback(liveness_role)

    def combined() -> None:
        beat()
        touch_liveness(liveness_role)

    return combined


def _ensure_worker_workspace(workspace: str, *, err: TextIO, trace_id: str) -> bool:
    """队列模式启动预检：显式配置的 ``LINGXI_WORKER_WORKSPACE`` 必须存在且可写
    （Issue #177）。

    S-A-07 r3 实测：部署配置显式指向一个容器内不存在的目录时，Agent SDK 子进程
    起不来，每个回合都在约一秒内落成同一种泛化 ``session_failed``——容器 health
    仍然正常、Gateway 也正常投递失败卡片，运维因此无法从任何可观察面把"本地
    工作目录无效"与"会话/模型真的失败了"区分开（见 Issue 正文）。

    这里不猜测、不吞掉这类配置错误：不存在就尝试就地创建（``mkdir -p``，只作用
    于这一个已经显式配置的路径本身，不额外派生或改写成别的路径）；创建失败，
    或者路径存在但不是一个可写目录，队列 worker 直接启动失败退出——绝不让
    进程带着一个坏掉的工作目录进入"每个回合都可能重演同一种失败"的运行状态。

    只有**显式**配置了 ``LINGXI_WORKER_WORKSPACE`` 时才会走到这里（调用方已经
    判过 ``config.workspace is not None``）：不显式配置时 Agent SDK 用自己的
    默认工作目录，不属于本次预检的范围。
    """

    path = Path(workspace)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        _log(
            err,
            trace_id,
            "error",
            "worker.queue.workspace.unavailable",
            reason="create_failed",
            workspace_path_length=len(workspace),
            error=type(error).__name__,
        )
        return False
    if not path.is_dir() or not os.access(path, os.W_OK):
        _log(
            err,
            trace_id,
            "error",
            "worker.queue.workspace.unavailable",
            reason="not_a_writable_directory",
            workspace_path_length=len(workspace),
        )
        return False
    return True


def _resolve_session_root(config: WorkerConfig, env: Mapping[str, str]) -> Path | None:
    """解析 Agent 会话 JSONL 物理清理（Issue #153）用的根目录。

    显式配置优先；否则退回 ``$HOME/.claude/projects``（当前部署镜像固定
    ``HOME=/tmp``，见 Dockerfile）。两者都取不到时返回 ``None``，调用方据此
    诚实地跳过物理清理，不猜一个可能错误的路径。
    """

    if config.session_root:
        return Path(config.session_root)
    return default_session_root(env)


async def _run_queue_worker(
    service: WorkerService, *, shutdown_timeout_seconds: float, err: TextIO, trace_id: str
) -> None:
    """跑队列消费循环，SIGTERM/SIGINT 触发有界预算的优雅停机（Issue #153）。

    信号处理器只 ``set`` 一个 ``asyncio.Event``，不做任何 I/O——与 gateway/scheduler
    的既有信号处理惯例一致。收到信号后：``WorkerService.run`` 内部已经不再
    ``claim`` 新任务，且在途任务的 ``_monitor`` 会把这次停机当作 ``/stop`` 主动
    请求 Agent SDK 中断（见 ``WorkerService._monitor``）；这里只负责给"等它真的
    收口"设一个总预算——预算内收口就是干净退出；预算耗尽就不再等，让在途任务
    保持 ``running``，交给未来某次心跳超时回收（`V-部署-03`：留下可恢复、诚实的
    状态，而不是无限期等待或悄悄丢弃）。
    """

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _handle_signal(signum: int) -> None:
        _log(err, trace_id, "info", "worker.queue.signal_received", signum=signum)
        stop.set()

    installed_signals: list[int] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig)
            installed_signals.append(sig)
        except (NotImplementedError, RuntimeError):
            # 非 POSIX 事件循环不支持 add_signal_handler；队列 worker 只在 Linux
            # 容器里跑，这里退化为不安装处理器，而不是让进程直接崩溃。
            pass

    run_task = asyncio.ensure_future(service.run(stop_event=stop))
    stop_wait_task = asyncio.ensure_future(stop.wait())
    try:
        done, _pending = await asyncio.wait(
            {run_task, stop_wait_task}, return_when=asyncio.FIRST_COMPLETED
        )

        if run_task in done:
            # run() 自己结束了——正常情况下只会在收到停止信号后发生；如果是异常
            # 提前结束，这里原样让异常向上传播，不吞掉真实的运行故障。
            stop_wait_task.cancel()
            run_task.result()
            return

        # 走到这里说明是信号触发的停机：run_task 仍在跑，给它一个有界预算收口。
        try:
            await asyncio.wait_for(run_task, timeout=shutdown_timeout_seconds)
        except asyncio.TimeoutError:
            _log(
                err,
                trace_id,
                "error",
                "worker.queue.shutdown_budget_exhausted",
                shutdown_timeout_seconds=shutdown_timeout_seconds,
                message="仍有在途任务未收口；进程将退出，任务保持 running 状态，"
                "等待未来一次心跳超时回收",
            )
            run_task.cancel()
            try:
                await run_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 停机路径，只记录不重抛
                pass
    finally:
        for sig in installed_signals:
            loop.remove_signal_handler(sig)


def _emit(stream: TextIO, payload: Mapping[str, Any]) -> None:
    stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    stream.write("\n")
    stream.flush()


def _log(stream: TextIO, trace_id: str, level: str, event: str, **fields: Any) -> None:
    record = {"level": level, "event": event, "trace_id": trace_id}
    record.update(fields)
    stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
    stream.write("\n")
    stream.flush()
