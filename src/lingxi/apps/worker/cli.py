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
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from lingxi.adapters.postgres_content_capture import PostgresContentCaptureWriter
from lingxi.adapters.postgres_conversation import PostgresTaskQueue, PostgresTaskQueueListener
from lingxi.adapters.postgres_user_memory import PostgresUserMemoryReader
from lingxi.apps.liveness import touch_liveness
from lingxi.core.execution.audit import redact_free_text
from lingxi.core.ids import is_ulid, new_ulid

from .config import ENV_PREFIX, WorkerConfig, WorkerConfigError, load_config
from .report import config_error_report
from .service import WorkerService
from .session_cleanup import default_session_root
from .turn import WorkerTurnExecutor

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
    detached_env_vars: Sequence[str] = (),
) -> int:
    """受控回合 / 队列 worker 的入口逻辑。

    ``detached_env_vars`` 由**真实进程入口**（``apps/worker/__main__.py``）传进来：
    那一层在调用本函数之前已经把这些变量从 ``os.environ`` 里摘掉了（报告 R6-D2），
    这里只负责把这件事记进启动日志。**本函数自己一个字节都不改 os.environ**——
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

    if config.innertest_content_capture_enabled:
        # 与上面的 output_safety_canary 同一纪律：一个默认应为关闭的能力一旦
        # 真的开启，必须在启动日志里足够扎眼（Issue #251/#304 批次 3）。
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
        # 生产（C-7）。采集仍然关闭（结构性保证生效），但这两种情况都值得一条
        # 显眼告警——前者通常是运维"以为开了但其实没开"，后者是**在生产配了
        # 只允许 stage 用的采集开关**，属于必须被看见的部署事故信号。
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

    if queue_mode:
        dsn = env.get("LINGXI_POSTGRES_DSN", "").strip()
        if not dsn:
            message = "队列 worker 缺少 LINGXI_POSTGRES_DSN"
            _log(err, config.trace_id, "error", "worker.queue.config.invalid", message=message)
            _emit(out, config_error_report(trace_id=config.trace_id, message=message))
            return EXIT_CONFIG_ERROR
        # 报告 R6-D2：数据库连接串**已经**在进程入口从 os.environ 里摘掉了，
        # 见 `detach_process_environment` 与 `apps/worker/__main__.py`。这里只是
        # 把这件事记进启动日志——一道没人看得见的安全闸和没有这道闸差别不大。
        # 只记变量名，永不回显取值。
        if detached_env_vars:
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
        # Epic D 闸⑥：queue 模式是唯一真正处理用户任务的路径，每个任务都要按
        # 它的 user_id 读 <user_env_root>/<user_id>/.mcp.json（见
        # apps/worker/service.py 的 _process_task）。缺了这个根目录，队列
        # worker 领到的**每一个**任务都必然失败关闭——与其带着这个必然失败的
        # 配置启动、让每个任务分别撞上同一个原因，不如在启动期一次性拒绝
        # （与 LINGXI_POSTGRES_DSN 同一姿态：恰一条日志、只报变量名、不回显
        # 取到的值——此处本就没有取到值可回显）。
        if not config.user_env_root:
            message = "队列 worker 缺少 LINGXI_USER_ENV_ROOT"
            _log(err, config.trace_id, "error", "worker.queue.config.invalid", message=message)
            _emit(out, config_error_report(trace_id=config.trace_id, message=message))
            return EXIT_CONFIG_ERROR
        # 外部独立审查 F4：光校验形态（绝对且规范化，见 config.py 的
        # _user_env_root）不够——卷没挂、路径写错时此前的行为是每领一个任务
        # 失败一次，运维要靠"任务全部失败"反推"卷没挂对"。这里在启动期就真的
        # 打开一次这个目录，把"路径不存在/不可读"这一类部署失误提前到启动期
        # 暴露。**不 mkdir**：这个目录由 scheduler 经 LocalUserEnvironment 独占
        # 创建（见该模块文档），worker 自己创建它既不是它的职责，也可能带着
        # 错误的权限位创建出一个"看起来存在但用不对"的目录。
        if not _ensure_user_env_root_available(
            config.user_env_root, err=err, trace_id=config.trace_id
        ):
            message = "LINGXI_USER_ENV_ROOT 不可用：路径不存在、不可读，或不是目录"
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
        # S-H1-6（#359 根因取证方案第 2 条）：`claim()` 是主循环每
        # poll_interval_seconds（默认 2s）都会执行一次的发现查询，空转时也不
        # 例外；`claim()` 本身在 `WorkerService.process_once()` 里是单次同步
        # 调用（不经 `asyncio.to_thread`，也不在 `asyncio.gather` 的并发批次
        # 内），因此对这个专属实例打开常驻轮询连接复用是安全的——不会有两次
        # `claim()` 调用真正并发访问同一条连接。
        queue = PostgresTaskQueue(dsn, reuse_polling_connection=True)
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
        # 内测轮内容级采集（Issue #251/#304 批次 3）：只在开关真正开启时才构造
        # 写入方，与该 DSN 建立的是完全独立于队列消费的写路径。开关关闭时
        # `content_capture_writer` 恒为 None，`WorkerService`/`WorkerTurnExecutor`
        # 都不会构造任何采集相关对象——这是"默认关闭不产生额外行为"在启动组装
        # 这一层的体现。
        content_capture_writer = (
            PostgresContentCaptureWriter(dsn).write
            if config.innertest_content_capture_enabled
            else None
        )
        # 用户记忆注入（Issue #357 S-H3-3 d 节）：与 content_capture_writer 同一
        # 姿态，用同一个 dsn 构造一个独立的只读适配器——queue 模式是唯一真正处理
        # 用户任务的路径，因此这里恒装配（不像内容采集那样受开关控制）。
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
            content_capture_writer=content_capture_writer,
            on_year_grounding_suspect=_year_grounding_suspect_sink(err=err, trace_id=config.trace_id),
            user_memory_reader=PostgresUserMemoryReader(dsn),
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

    if config.system_prompt_file:
        # turn 模式同样服务提示词文件，但姿态是**失败关闭**而不是 queue 模式的
        # 逐任务降级（外部独立审查 2026-08-23 P2-1）：一次性受控回合的存在意义
        # 就是验证，"文件读不到就静默跑一个无提示词回合"会让验证结论失真——
        # 无论把结果读成"提示词没效果"还是"已生效"都可能是错的。读不到即
        # 启动失败，与 queue 模式缺 LINGXI_USER_ENV_ROOT 的启动预检同一姿态。
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
            # stdout 契约与其他配置错误一致：恰好一个 JSON 对象（codex 二轮
            # 复验发现首版只写 stderr 就返回，破坏了 turn 模式的公开输出契约）。
            _emit(
                out,
                config_error_report(
                    trace_id=config.trace_id,
                    message=f"system_prompt_file 不可用（{degraded}），受控回合失败关闭",
                ),
            )
            return EXIT_CONFIG_ERROR
        # 与 queue 模式同一细节：注入已解析的提示词时必须清掉文件指针，否则
        # replace 重跑 __post_init__ 会撞上 file 与 prompt 的互斥不变量。
        config = _replace(config, system_prompt=prompt, system_prompt_file=None)

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

    Issue #291 独立审查：``denied_count > 0``（这一回合有工具调用被 ``ToolPolicy``
    拒绝）时把这条事件提到 ``warning`` 级别——与「turn」模式下 ``worker.turn.
    finished`` 按 ``gate_bypassed``/失败/未收口选级别是同一惯例（见本文件
    ``main()`` 里那段），拒绝本身不算失败（回合仍可能正常收口），但白名单
    配错、内置工具被误判为可用这类问题只在这里才留得下痕迹，不该和普通成功
    终态一样淹没在 ``info`` 里。
    """

    def sink(fields: Mapping[str, Any]) -> None:
        denied_count = fields.get("denied_count")
        level = "warning" if isinstance(denied_count, int) and denied_count > 0 else "info"
        _log(err, trace_id, level, "worker.task.terminal", **fields)

    return sink


def _year_grounding_suspect_sink(*, err: TextIO, trace_id: str) -> Callable[[Mapping[str, Any]], None]:
    """年份接地护栏第二层的结构化告警出口（Issue #326，批次 5 卡 E）。

    与 :func:`_terminal_outcome_sink` 同一条纪律与同一个理由：``WorkerService``
    不直接调 stdlib ``logging``（本文件 ``main()`` 从不调用 ``logging.
    basicConfig()``），检测结果必须由装配层接到本文件既有的结构化 ``_log()``
    出口才能真正落到运维能看到的地方。这就是本卡"复用既有运行告警进管理群的
    通道"的落点——worker 进程结构上从不直接持有飞书凭据（见
    ``_build_alerting_duty`` 的"只走日志出口"），它对外唯一的信号出口是带
    ``trace_id`` 的结构化 stderr（``V-部署-04``），本函数复用的正是这一条通道，
    不新开一条。事件名固定为 ``worker.year_grounding_suspect``；``fields`` 来自
    ``core/year_grounding_guard.YearGroundingSuspect.to_alert_fields()``，只有
    ``task_id``/命中的相对时间词/查询年份集合/当前年份四项，不含问句或答案正文。
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
    """把"记进 AlertManager"与"戳一下活性文件"合成一个心跳回调（Issue #153）。

    与 ``apps/gateway/__init__.py``、``apps/scheduler/alerting_assembly.py``
    （#237 拆分后的新位置）的同名函数同一形状，见那两处的说明。
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


def _ensure_user_env_root_available(user_env_root: str, *, err: TextIO, trace_id: str) -> bool:
    """queue 模式启动预检（Epic D 闸⑥，外部独立审查 F4）：
    ``LINGXI_USER_ENV_ROOT`` 必须指向一个**已经存在、可读**的目录。

    与 :func:`_ensure_worker_workspace` **刻意不同**：这里**不 ``mkdir``**。
    用户环境根目录由 scheduler 经 ``LocalUserEnvironment`` 独占创建（见
    ``adapters/user_environment.py`` 模块文档），worker 只读不写；如果 worker
    自己在这里补建一个目录，一来越权做了不属于它的事，二来会用默认权限位
    （而不是根目录要求的 ``0750``）创建出一个"存在但形态不对"的目录，把一个
    本该在启动期暴露的部署失误伪装成"看起来挂对了"。

    卷没挂、路径写错时，此前的行为是每领一个任务在 ``load_user_mcp_servers``
    里失败一次（``root_ENOENT`` 一类错误码），运维要靠"每个任务都失败"反推
    "卷没挂对"。这里把它提前到启动期一次性暴露。
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


#: 不允许被 Claude CLI 及其 MCP 子进程继承的进程环境变量（报告 R6-D2）。
#: 只列 worker-queue 的 env 文件里真实存在、且本进程读完之后不再需要从环境里
#: 取第二次的那些。加一项之前先确认：本进程没有任何代码路径会在这之后再去
#: `os.environ` 读它——否则删掉的是自己的配置，不是子进程的继承面
#: （``main()`` 全程用入口传进来的配置快照，不读 ``os.environ``）。
_UNINHERITABLE_ENV_VARS = ("LINGXI_POSTGRES_DSN",)


def detach_process_environment() -> tuple[dict[str, str], tuple[str, ...]]:
    """**只允许真实进程入口调用**：把配置读走，再把不该被子进程继承的变量从
    ``os.environ`` 里摘掉。返回 ``(配置快照, 被摘掉的变量名)``。

    ## 为什么在这里，不在 ``main()`` 里

    这个函数改的是**整个进程**的环境。``main()`` 不是进程——它是一个会被单测在
    同一个解释器里反复调用的普通函数：``tests/test_worker_workspace_precheck.py``
    等三个文件都用 ``main(env=…)`` 走队列模式的启动路径。把 ``os.environ.pop``
    放在 ``main()`` 里，**跑完那几条用例之后同一个进程里所有真库用例的
    ``LINGXI_POSTGRES_DSN`` 就没了**——CI 上 40 个 ``setUpClass`` 直接
    ``KeyError``，而 ``verify_repository.sh`` 的「容器在、DSN 没了」守卫准确地
    报了红（2026-09-02 实测，本地已复现）。

    这不是"测试写法不好"，是**职责放错了层**：进程级的副作用属于进程入口，不属于
    一个可复用函数。放在入口还有两个额外好处——摘除发生在 ``main()`` 开始之前
    （比原来更早，绝无任何回合已经起来的窗口），而且未来任何新写的
    ``main(env=…)`` 用例都不可能再把它踩回来。

    ## 为什么必须动 ``os.environ`` 而不是给 SDK 传 ``env``

    已回源确认 ``claude-agent-sdk`` 0.2.128 的
    ``_internal/transport/subprocess_cli.py``：
    ``inherited_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}``，
    随后 ``process_env = {**inherited_env, …, **self._options.env, …}``。
    ``options.env`` **只能往上加、不能往下减**，SDK 侧没有任何参数能删掉一个已经
    在进程环境里的变量。唯一能真正生效的位置就是本进程自己的 ``os.environ``。

    ## 为什么摘掉之后本进程还工作

    返回的快照是摘除**之前**的完整副本，``main()`` 用它读全部配置（包括 DSN），
    本进程后续所有数据库连接都用读到的那个值构造。healthcheck 走 ``docker exec``
    另起进程、拿的是容器自己的 env，同样不受影响。

    只返回变量名，永不回显取值。
    """

    snapshot = dict(os.environ)
    removed = tuple(
        name for name in _UNINHERITABLE_ENV_VARS if os.environ.pop(name, None) is not None
    )
    return snapshot, removed


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
