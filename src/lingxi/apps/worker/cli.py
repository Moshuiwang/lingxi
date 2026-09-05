"""``python -m lingxi.apps.worker`` 的命令行外壳。

``LINGXI_WORKER_MODE=turn`` 执行一次受控回合；``queue`` 启动长期队列消费者。队列
模式只负责数据库任务状态与执行生命周期，真实外部出站 transport 由应用装配层注入。

输出契约（受控验证要引用它，因此写死在这里）：

- **stdout**：恰好一个 JSON 对象，就是回合报告；配置错误时 ``turn`` 为空、
  ``failure.code`` 为 ``config_error``。
- **stderr**：结构化日志，每行一个 JSON 对象，都带 ``trace_id``，不写文件。
- **退出码**：0 正常收口；2 跑完但没收口；3 配置错误；4 会话失败；
  5 检测到绕过屏障的调用（``ungated_count > 0``）。

日志里刻意不出现问题原文与最终正文，只出现字节数、计数与状态。
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from lingxi.adapters.postgres_content_capture import PostgresContentCaptureWriter
from lingxi.adapters.postgres_conversation import PostgresTaskQueue, PostgresTaskQueueListener
from lingxi.adapters.postgres_user_memory import PostgresUserMemoryReader
from lingxi.apps.liveness import touch_liveness
from lingxi.config.content_override import log_content_source
from lingxi.core.execution.audit import redact_free_text
from lingxi.core.ids import is_ulid, new_ulid

from .config import ENV_PREFIX, WorkerConfig, WorkerConfigError, load_config
from .report import config_error_report
from .service import WorkerService
from .service_ports import SessionCleanupSettings, WorkerObservers
from .session_cleanup import default_session_root
from .turn import WorkerTurnExecutor

EXIT_OK = 0
EXIT_TURN_NOT_CLOSED = 2
EXIT_CONFIG_ERROR = 3
EXIT_SESSION_FAILED = 4
EXIT_GATE_BYPASSED = 5


def _emit_config_error(
    *, out: TextIO, err: TextIO, env: Mapping[str, str], error: WorkerConfigError
) -> int:
    """把一次配置加载失败落成结构化日志与 stdout 的配置错误报告。"""
    provided_trace_id = env.get("LINGXI_WORKER_TRACE_ID", "")
    # 只有合法 ULID 才复用：误接进来的令牌不得随错误输出外泄。
    trace_id = provided_trace_id if is_ulid(provided_trace_id) else new_ulid()
    # 配置错误文案可能回显运维写串的原值，与模型侧同一标准：出口过自由文本
    # 脱敏并截断。
    message = redact_free_text(str(error))[:300]
    _log(err, trace_id, "error", "worker.config.invalid", message=message)
    _emit(out, config_error_report(trace_id=trace_id, message=message))
    return EXIT_CONFIG_ERROR


def _warn_if_output_safety_canary_enabled(config: WorkerConfig, err: TextIO) -> None:
    """输出安全 canary 一旦被遗忘在开启状态的显眼告知。

    每条问数结果都会被强制改写成安全遮蔽或 withheld 终态；启动日志必须足够
    扎眼。默认关闭时不输出。
    """
    if config.output_safety_canary is None:
        return
    _log(
        err,
        config.trace_id,
        "warning",
        "worker.output_safety_canary_enabled",
        mode=config.output_safety_canary,
        message=(
            "此开关仅供受控验收使用，默认应为关闭；如果这不是一次"
            "受控验收启动，请立即核实并清空 LINGXI_WORKER_OUTPUT_SAFETY_CANARY"
        ),
    )


def _warn_content_capture_status(config: WorkerConfig, err: TextIO) -> None:
    """内容级采集是默认关闭的能力，真开启或被结构性挡住都要显眼告知。"""
    if config.innertest_content_capture_enabled:
        _log(
            err,
            config.trace_id,
            "warning",
            "worker.innertest_content_capture_enabled",
            message=(
                "内测轮内容级采集已开启，本进程处理的每个任务的用户问题/模型"
                "回答/工具调用详情（凭据形状已过滤）将写入 "
                "innertest_content_capture 表；此开关仅供 stage 内测轮使用，"
                "如果这不是一次内测轮启动，请立即核实并清空 "
                "LINGXI_INNERTEST_CONTENT_CAPTURE"
            ),
        )
    elif config.innertest_content_capture_misconfigured:
        # 主开关已配置但被挡住：要么第二确认变量缺失/不匹配，要么环境自称是
        # 生产。前者通常是运维"以为开了但其实没开"，后者是"在生产配了只允许
        # stage 用的采集开关"，都值得一条显眼告警。
        _log(
            err,
            config.trace_id,
            "warning",
            "worker.innertest_content_capture_blocked",
            message=(
                "已配置 LINGXI_INNERTEST_CONTENT_CAPTURE=1，但被挡住："
                "或者环境经 LINGXI_DEPLOY_ENVIRONMENT 自称是生产（生产一律不采集），"
                "或者第二确认变量 "
                "LINGXI_INNERTEST_CONTENT_CAPTURE_ENVIRONMENT_CONFIRM 缺失或不匹配"
                "（不回显取到的值）。内容级采集本次运行不生效（结构性保证：正式"
                "环境即使误配了主开关也不会生效）；若这是生产，请立即清空主开关，"
                "若这是一次真实要开启采集的 stage 部署，请核对第二确认变量的取值"
            ),
        )


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    detached_env_vars: Sequence[str] = (),
) -> int:
    """受控回合 / 队列 worker 的入口逻辑。

    ``detached_env_vars`` 由**真实进程入口**（``apps/worker/__main__.py``）传进来：
    那一层在调用本函数之前已经把这些变量从 ``os.environ`` 里摘掉了，这里只
    负责把这件事记进启动日志。**本函数自己一个字节都不改 os.environ**——
    理由见 :func:`detach_process_environment` 的文档。
    """
    del argv  # 本切片没有命令行参数：全部输入走 LINGXI_ 前缀环境变量。
    env = os.environ if env is None else env
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr

    try:
        queue_mode = env.get("LINGXI_WORKER_MODE", "turn").strip().lower() == "queue"
        config = load_config(env, require_question=not queue_mode, queue_mode=queue_mode)
    except WorkerConfigError as error:
        return _emit_config_error(out=out, err=err, env=env, error=error)

    _warn_if_output_safety_canary_enabled(config, err)
    _warn_content_capture_status(config, err)

    if queue_mode:
        return _run_queue_mode(
            config, env=env, out=out, err=err, detached_env_vars=detached_env_vars
        )
    return _run_turn_mode(config, out=out, err=err)


def _emit_queue_config_error(
    *, out: TextIO, err: TextIO, config: WorkerConfig, message: str
) -> None:
    _log(err, config.trace_id, "error", "worker.queue.config.invalid", message=message)
    _emit(out, config_error_report(trace_id=config.trace_id, message=message))


def _log_env_detached(err: TextIO, config: WorkerConfig, detached_env_vars: Sequence[str]) -> None:
    """记一条数据库连接串已被进程入口摘除的启动日志；只记变量名，不回显取值。"""
    if not detached_env_vars:
        return
    _log(
        err,
        config.trace_id,
        "info",
        "worker.queue.env_detached",
        variables=list(detached_env_vars),
        message=(
            "进程入口已从 os.environ 移除下列变量，Claude CLI 与 MCP 子进程"
            "不再继承它们；本进程改用启动时读到的值（只记变量名，不回显取值）"
        ),
    )


def _check_queue_mode_prereqs(
    config: WorkerConfig,
    *,
    env: Mapping[str, str],
    out: TextIO,
    err: TextIO,
    detached_env_vars: Sequence[str],
) -> str | None:
    """一次性做完 queue 模式的全部启动预检，通过则返回 DSN。

    不合格时已发出配置错误报告并返回 ``None``。每一项都是"与其带着必然失败
    的配置启动、让每个任务分别撞上同一个原因，不如在启动期一次性拒绝"——
    DSN、``user_env_root``、workspace 三项分别对应队列消费、用户 MCP 配置
    读取、Agent SDK 工作目录三个必要前提。
    """
    dsn = env.get("LINGXI_POSTGRES_DSN", "").strip()
    if not dsn:
        _emit_queue_config_error(
            out=out, err=err, config=config, message="队列 worker 缺少 LINGXI_POSTGRES_DSN"
        )
        return None
    _log_env_detached(err, config, detached_env_vars)
    if not config.user_env_root:
        _emit_queue_config_error(
            out=out, err=err, config=config, message="队列 worker 缺少 LINGXI_USER_ENV_ROOT"
        )
        return None
    if not _ensure_user_env_root_available(config.user_env_root, err=err, trace_id=config.trace_id):
        _emit_queue_config_error(
            out=out,
            err=err,
            config=config,
            message="LINGXI_USER_ENV_ROOT 不可用：路径不存在、不可读，或不是目录",
        )
        return None
    if config.workspace is not None and not _ensure_worker_workspace(
        config.workspace, err=err, trace_id=config.trace_id
    ):
        _emit_queue_config_error(
            out=out,
            err=err,
            config=config,
            message=f"{ENV_PREFIX}WORKSPACE 不可用：既不存在也无法创建，或存在但不是可写目录",
        )
        return None
    return dsn


def _build_worker_service(
    config: WorkerConfig, *, dsn: str, env: Mapping[str, str], err: TextIO
) -> WorkerService:
    """装配 queue 模式所需的 ``WorkerService`` 及其协作对象。

    内容级采集写入方只在开关真正开启时才构造，与该 DSN 建立完全独立于队列
    消费的写路径；用户记忆读取适配器恒装配——queue 模式是唯一真正处理用户
    任务的路径，不像内容采集那样受开关控制。
    """
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
    content_capture_writer = (
        PostgresContentCaptureWriter(dsn).write
        if config.innertest_content_capture_enabled
        else None
    )
    return WorkerService(
        config=config,
        queue=PostgresTaskQueue(dsn, reuse_polling_connection=True),
        listener_factory=lambda: PostgresTaskQueueListener(dsn),
        observers=WorkerObservers(
            heartbeat=_combined_heartbeat(alerting_duty, "worker"),
            on_task_stuck=alerting_duty.task_stuck_callback(),
            on_alert_tick=alerting_duty.run_once,
            on_terminal_outcome=_terminal_outcome_sink(err=err, trace_id=config.trace_id),
            content_capture_writer=content_capture_writer,
            on_year_grounding_suspect=_year_grounding_suspect_sink(
                err=err, trace_id=config.trace_id
            ),
        ),
        session_cleanup=SessionCleanupSettings(
            root=session_root, batch_limit=config.session_cleanup_batch_limit
        ),
        user_memory_reader=PostgresUserMemoryReader(dsn),
    )


def _run_queue_mode(
    config: WorkerConfig,
    *,
    env: Mapping[str, str],
    out: TextIO,
    err: TextIO,
    detached_env_vars: Sequence[str],
) -> int:
    """Queue 模式的完整启动序列：预检、装配、跑消费循环直到停机预算耗尽。"""
    dsn = _check_queue_mode_prereqs(
        config, env=env, out=out, err=err, detached_env_vars=detached_env_vars
    )
    if dsn is None:
        return EXIT_CONFIG_ERROR

    # 内容目录（含可选的宿主机外置覆盖）在领第一个任务之前读一次并记一行来源
    # 事实；管理群告警只由 scheduler 发，理由见
    # `apps/scheduler/content_override_notice.py`。
    source = log_content_source("worker-queue")
    _log(
        err,
        config.trace_id,
        "info",
        "worker.queue.content_catalog",
        content_version=source.catalog.version,
        content_digest=source.digest,
        override_keys=len(source.override_keys),
        rejected=source.rejection,
    )
    _log(
        err,
        config.trace_id,
        "info",
        "worker.queue.start",
        worker_id=config.worker_id,
        target_worker_version=config.target_worker_version,
        max_concurrency=config.max_concurrency,
    )
    service = _build_worker_service(config, dsn=dsn, env=env, err=err)
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


def _resolve_turn_system_prompt(
    config: WorkerConfig, *, out: TextIO, err: TextIO
) -> WorkerConfig | None:
    """若配置了 ``system_prompt_file``，现读内容替换进 config。

    turn 模式姿态是**失败关闭**，不是 queue 模式那样逐任务降级：一次性受控
    回合的存在意义就是验证，读不到文件就静默跑一个无提示词回合会让验证结论
    失真。读取失败时已发出配置错误报告并返回 ``None``。
    """
    if not config.system_prompt_file:
        return config

    from dataclasses import replace as _replace

    from .service import _load_task_system_prompt

    prompt, _digest, degraded = _load_task_system_prompt(config.system_prompt_file)
    if degraded is not None:
        _log(
            err,
            config.trace_id,
            "error",
            "worker.turn.system_prompt_unavailable",
            reason=degraded,
        )
        _emit(
            out,
            config_error_report(
                trace_id=config.trace_id,
                message=f"system_prompt_file 不可用（{degraded}），受控回合失败关闭",
            ),
        )
        return None
    # 注入已解析的提示词时必须清掉文件指针，否则 replace 重跑 __post_init__
    # 会撞上 file 与 prompt 的互斥不变量。
    return _replace(config, system_prompt=prompt, system_prompt_file=None)


def _turn_finished_fields(report: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """从回合报告里拼出 ``worker.turn.finished`` 的日志字段与 gate_bypassed。"""
    turn = report["turn"]
    resources = report["resources"]
    usage = resources["usage"]
    audit = report["audit"]
    gate_bypassed = audit["ungated_count"] > 0
    fields = {
        "closed": turn["closed"],
        "user_result": turn["user_result"],
        "terminal_result_count": turn["terminal_result_count"],
        "sdk_result_message_count": turn["sdk_result_message_count"],
        "sdk_result_is_error": turn["sdk_result_is_error"],
        "sdk_result_subtype": turn["sdk_result_subtype"],
        "sdk_terminal_reason": turn["sdk_terminal_reason"],
        "termination_state": turn["termination_state"],
        "termination_reason": turn["termination_reason"],
        "guard_triggered": turn["guard_triggered"],
        "duration_seconds": resources["duration_seconds"],
        "agent_turns": resources["agent_turns"],
        "tool_call_count": resources["tool_call_count"],
        "executed_tool_call_count": resources["executed_tool_call_count"],
        "usage_status": usage["status"],
        "usage_source": usage["source"],
        "usage_fields": usage.get("fields"),
        "gate_bypassed": gate_bypassed,
        "final_text_bytes": turn["final_text_bytes"],
        "call_count": audit["call_count"],
        "denied_count": audit["denied_count"],
        "failed_count": audit["failed_count"],
        "ungated_count": audit["ungated_count"],
        "failure": report["failure"],
    }
    return fields, gate_bypassed


def _run_turn_mode(config: WorkerConfig, *, out: TextIO, err: TextIO) -> int:
    """跑一次性受控回合：读提示词文件（如有）、执行回合、记终态日志、定退出码。"""
    resolved = _resolve_turn_system_prompt(config, out=out, err=err)
    if resolved is None:
        return EXIT_CONFIG_ERROR
    config = resolved

    _log(
        err,
        config.trace_id,
        "info",
        "worker.turn.start",
        read_only_tools=list(config.read_only_tools),
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
    fields, gate_bypassed = _turn_finished_fields(report)
    _log(
        err,
        config.trace_id,
        "error" if (report["failure"] or gate_bypassed or not fields["closed"]) else "info",
        "worker.turn.finished",
        **fields,
    )
    _emit(out, report)

    if gate_bypassed:
        # 安全边界失效优先于一切其他失败态：绕过之后又超时/抛错时，受控验证
        # 必须先看到 5 而不是通用的 4。
        return EXIT_GATE_BYPASSED
    if report["failure"]:
        return EXIT_SESSION_FAILED
    return EXIT_OK if fields["closed"] else EXIT_TURN_NOT_CLOSED


class _LogOnlyAlertSender:
    """worker 的告警发送出口：只记结构化日志，从不发起网络请求。

    Worker 不获得飞书出站密钥，因此不能像 gateway/scheduler 那样把告警直接
    发进管理群；这里仍然装配完整的 ``AlertManager``/``AlertingDuty`` 状态机
    （阈值、去重、恢复计时都真实生效），只是"发送"这一步落到结构化日志。
    写 ``_log()`` 到 ``err``，不用 stdlib ``logging``——``main()`` 从不调用
    ``logging.basicConfig()``，经由 ``logging`` 的调用没有 handler 时会被
    悄悄吞掉。
    """

    def __init__(self, *, err: TextIO, trace_id: str) -> None:
        """记住输出流与 trace_id，供 :meth:`send_text` 复用。"""
        self._err = err
        self._trace_id = trace_id

    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None:
        del chat_id, dedupe_key
        _log(self._err, self._trace_id, "warning", "worker.alert", text=text)


class _StructuredAuditSink:
    def __init__(self, *, err: TextIO, trace_id: str) -> None:
        """记住输出流与 trace_id，供 :meth:`record` 复用。"""
        self._err = err
        self._trace_id = trace_id

    def record(self, action: str, /, **fields: object) -> None:
        _log(self._err, self._trace_id, "info", f"worker.audit.{action}", **fields)


def _terminal_outcome_sink(*, err: TextIO, trace_id: str) -> Callable[[Mapping[str, Any]], None]:
    """把 ``WorkerService`` 的终态收口低敏审计事件接到本文件的结构化 stderr 出口。

    ``WorkerService`` 是纯组装对象，不该假设 stdlib ``logging`` 有 handler；
    这里复用现成的 ``_log()`` 出口，带上 ``trace_id``。``denied_count > 0``
    （这一回合有工具调用被拒绝）时把事件提到 ``warning`` 级别——拒绝本身不算
    失败，但白名单配错这类问题只在这里才留得下痕迹，不该淹没在 ``info`` 里。
    """

    def sink(fields: Mapping[str, Any]) -> None:
        denied_count = fields.get("denied_count")
        level = "warning" if isinstance(denied_count, int) and denied_count > 0 else "info"
        _log(err, trace_id, level, "worker.task.terminal", **fields)

    return sink


def _year_grounding_suspect_sink(
    *, err: TextIO, trace_id: str
) -> Callable[[Mapping[str, Any]], None]:
    """年份接地护栏第二层的结构化告警出口，复用本文件既有的结构化 stderr 通道。

    事件名固定为 ``worker.year_grounding_suspect``；``fields`` 只有
    ``task_id``/命中的相对时间词/查询年份集合/当前年份四项，不含问句或答案
    正文。
    """

    def sink(fields: Mapping[str, Any]) -> None:
        _log(err, trace_id, "warning", "worker.year_grounding_suspect", **fields)

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
    """把"记进 AlertManager"与"戳一下活性文件"合成一个心跳回调。

    与 ``apps/gateway/__init__.py``、``apps/scheduler/alerting_assembly.py``
    的同名函数同一形状，见那两处的说明。
    """
    beat = alerting_duty.heartbeat_callback(liveness_role)

    def combined() -> None:
        beat()
        touch_liveness(liveness_role)

    return combined


def _ensure_worker_workspace(workspace: str, *, err: TextIO, trace_id: str) -> bool:
    """队列模式启动预检：显式配置的 ``LINGXI_WORKER_WORKSPACE`` 必须存在且可写。

    部署配置显式指向一个容器内不存在的目录时，Agent SDK 子进程起不来，每个
    回合都在约一秒内落成同一种泛化 ``session_failed``，容器 health 仍然正常
    ——运维因此无法从任何可观察面把"工作目录无效"与"会话/模型真的失败了"
    区分开。这里不存在就尝试就地创建（``mkdir -p``，只作用于这一个已经显式
    配置的路径本身）；创建失败或存在但不可写，队列 worker 直接启动失败退出。
    只有**显式**配置了该变量时才会走到这里；不显式配置时 Agent SDK 用自己的
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


def _ensure_user_env_root_available(user_env_root: str, *, err: TextIO, trace_id: str) -> bool:
    """Queue 模式启动预检：``LINGXI_USER_ENV_ROOT`` 必须指向已存在、可读的目录。

    与 :func:`_ensure_worker_workspace` **刻意不同**：这里**不 ``mkdir``**。
    用户环境根目录由 scheduler 独占创建，worker 只读不写；worker 自己补建
    既越权，又会用默认权限位创建出一个"存在但形态不对"的目录，把一个本该在
    启动期暴露的部署失误伪装成"看起来挂对了"。卷没挂、路径写错时，此前的
    行为是每领一个任务失败一次，运维要靠"每个任务都失败"反推"卷没挂对"；
    这里把它提前到启动期一次性暴露。
    """
    try:
        fd = os.open(user_env_root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as error:
        _log(
            err,
            trace_id,
            "error",
            "worker.queue.user_env_root.unavailable",
            reason="open_failed",
            error=type(error).__name__,
        )
        return False
    try:
        info = os.fstat(fd)
    finally:
        os.close(fd)
    if not stat.S_ISDIR(info.st_mode):
        # O_DIRECTORY 本该已经挡住这一支；留着是防御性的第二道，不为它单独
        # 编一条测不到的用例。
        _log(
            err,
            trace_id,
            "error",
            "worker.queue.user_env_root.unavailable",
            reason="not_a_directory",
        )
        return False
    return True


#: 不允许被 Claude CLI 及其 MCP 子进程继承的进程环境变量。只列 worker-queue
#: 的 env 文件里真实存在、且本进程读完之后不再需要从环境里取第二次的那些。
#: 加一项之前先确认：本进程没有任何代码路径会在这之后再去 `os.environ` 读它
#: （``main()`` 全程用入口传进来的配置快照，不读 ``os.environ``）。
_UNINHERITABLE_ENV_VARS = ("LINGXI_POSTGRES_DSN",)


def detach_process_environment() -> tuple[dict[str, str], tuple[str, ...]]:
    """**只允许真实进程入口调用**：读走配置快照，再摘掉不该被子进程继承的变量。

    返回 ``(配置快照, 被摘掉的变量名)``。**放在这里而不是 ``main()``**：改的
    是整个进程的环境，而 ``main()`` 会被单测在同一个解释器里反复调用，摘除
    动作放进去会让跑完几条队列模式用例后同进程所有真库用例的 DSN 都没了。
    **必须动 ``os.environ`` 而不是给 SDK 传 ``env``**：Agent SDK 的子进程
    继承逻辑只能往上加、不能往下减。快照是摘除**之前**的完整副本，只返回
    变量名，永不回显取值。
    """
    snapshot = dict(os.environ)
    removed = tuple(
        name for name in _UNINHERITABLE_ENV_VARS if os.environ.pop(name, None) is not None
    )
    return snapshot, removed


def _resolve_session_root(config: WorkerConfig, env: Mapping[str, str]) -> Path | None:
    """解析 Agent 会话 JSONL 物理清理用的根目录。

    显式配置优先；否则退回 ``$HOME/.claude/projects``（当前部署镜像固定
    ``HOME=/tmp``）。两者都取不到时返回 ``None``，调用方据此诚实地跳过物理
    清理，不猜一个可能错误的路径。
    """
    if config.session_root:
        return Path(config.session_root)
    return default_session_root(env)


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop, handler: Callable[[int], None]
) -> list[int]:
    """尝试安装 SIGTERM/SIGINT 处理器，返回实际装上的信号列表。

    非 POSIX 事件循环不支持 ``add_signal_handler``；队列 worker 只在 Linux
    容器里跑，这里退化为不安装处理器，而不是让进程直接崩溃。
    """
    installed: list[int] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, handler, sig)
            installed.append(sig)
        except (NotImplementedError, RuntimeError):
            pass
    return installed


async def _await_shutdown_budget(
    run_task: asyncio.Task[None], *, shutdown_timeout_seconds: float, err: TextIO, trace_id: str
) -> None:
    """信号触发停机后，给在途任务一个有界预算收口；预算耗尽就取消并放弃等待。"""
    try:
        await asyncio.wait_for(run_task, timeout=shutdown_timeout_seconds)
    except TimeoutError:
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
        except (asyncio.CancelledError, Exception):  # 停机路径，只记录不重抛
            pass


async def _run_queue_worker(
    service: WorkerService, *, shutdown_timeout_seconds: float, err: TextIO, trace_id: str
) -> None:
    """跑队列消费循环，SIGTERM/SIGINT 触发有界预算的优雅停机。

    信号处理器只 ``set`` 一个 ``asyncio.Event``，不做任何 I/O。收到信号后：
    ``WorkerService.run`` 内部已经不再 ``claim`` 新任务，在途任务的
    ``_monitor`` 会把这次停机当作 ``/stop`` 主动请求中断；这里只负责给
    "等它真的收口"设一个总预算——预算内收口就是干净退出；预算耗尽就不再等，
    让在途任务保持 ``running``，交给未来某次心跳超时回收。
    """
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _handle_signal(signum: int) -> None:
        _log(err, trace_id, "info", "worker.queue.signal_received", signum=signum)
        stop.set()

    installed_signals = _install_signal_handlers(loop, _handle_signal)
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
        await _await_shutdown_budget(
            run_task,
            shutdown_timeout_seconds=shutdown_timeout_seconds,
            err=err,
            trace_id=trace_id,
        )
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
