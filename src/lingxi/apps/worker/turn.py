"""一个受控回合的装配与执行。

本文件是 Issue #37 的全部要点所在：**把已验证的组件真的接起来**。

- ``ToolPolicy``：白名单只含配置里那一个只读 MCP 工具，其余一律默认拒绝；
- ``TurnAudit``：每回合开始显式 ``start_turn()``——``hooks`` 是会话级的，不翻页的话
  第二回合的终止计数必然从 2 起跳，回合结论也会互相污染（`V-执行-14`）；
- ``ToolGateway``：经 ``build_hook_matchers()`` 装进真实 ``ClaudeAgentOptions.hooks``，
  它是唯一判定层；
- ``TurnStreamRecorder``：把工具回执与最终正文汇入**同一个** ``TurnAudit``。

判定、归类、脱敏一行都不在这里——那些是 ``core`` 的职责，本层只负责"接对"。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

from collections.abc import Iterable, Mapping
from typing import Any, Callable

from lingxi.adapters.claude_agent_session import (
    AgentSessionInterrupted,
    DrainTimeoutError,
    build_agent_options,
    run_single_turn,
)
from lingxi.core.execution.audit import AuditRedactor, ResultRules, TurnAudit, redact_free_text
from lingxi.core.execution.hooks import ToolGateway
from lingxi.core.execution.input_safety import compose_agent_prompt, normalize_external_texts
from lingxi.core.execution.message_stream import TurnStreamRecorder
from lingxi.core.execution.tool_policy import ToolPolicy

from .config import OUTPUT_SAFETY_CANARY_SURVIVOR_BODY, WorkerConfig
from .report import build_report

_MAX_FAILURE_TEXT = 500

def _inject_output_safety_canary(final_text: str, *, mode: str, system_prompt: str) -> str:
    """把合成 system prompt 确定性注入最终正文，供出口安全约束命中（#142）。

    - ``withheld``：整段替换为 system prompt——整串命中覆盖全部正文、无业务内容
      幸存，``constrain_output`` 必然给出 ``withheld=True``；
    - ``masked``：固定幸存句 + 模型正文（如有）+ system prompt——幸存句不含任何
      已知敏感模式，且配置期已双向拒绝"合成提示与幸存句/固定终态文案互为子串"的形态（独立审核 F2/F3 + 第一级补审 P2-3），
      因此**无论模型正文是什么**（哪怕整段都是可遮蔽的标记，例如模型恰好输出
      ``system prompt`` 字样），幸存句都保证命中区间之外有真实内容，必然
      ``blocked=True`` 且 ``withheld=False``。第一版实现依赖"模型正文有幸存内容"
      的运行期判定，独立审核 F3 实证证明可遮蔽正文会让 masked 滑进 withheld——
      确定性必须由构造保证，不能由模型行为保证。

    两个档位都不依赖模型行为：r17 的教训是把触发条件寄托在"模型恰好复述提示词"
    上，真实链路一次都没有触发过。注入发生在 ``build_report``（出口约束所在地）
    之前，走的是与真实泄露完全相同的检测与投影路径。
    """

    if mode == "withheld":
        return system_prompt
    base = (
        f"{OUTPUT_SAFETY_CANARY_SURVIVOR_BODY}\n{final_text}"
        if final_text
        else OUTPUT_SAFETY_CANARY_SURVIVOR_BODY
    )
    return f"{base}\n{system_prompt}"


class WorkerTurnExecutor:
    """一个会话的执行器。会话级对象（策略、审计、网关、选项）只建一次。

    这个对象本身就是断言 `V-执行-18` 的目标：删掉任何一段接线——不建网关、不装
    hooks、不调 ``start_turn()``、不接消息流——用例都必须变红。
    """

    def __init__(
        self,
        config: WorkerConfig,
        *,
        stderr_stream: Any | None = None,
        clock: Callable[[], float] | None = None,
        mark_external_side_effect: Callable[[], None] | None = None,
        propagate_cancellation: bool = False,
    ) -> None:
        self._config = config
        self._policy = ToolPolicy(allowed_tools=(config.read_only_tool,))
        self._audit = TurnAudit(
            rules=ResultRules(failure_text_markers=config.failure_text_markers),
            redactor=AuditRedactor(allowed_input_fields=config.audit_input_fields),
        )
        self._gateway = ToolGateway(
            policy=self._policy,
            audit=self._audit,
            mark_external_side_effect=mark_external_side_effect,
        )
        self._options: Any = None
        self._stderr_stream = sys.stderr if stderr_stream is None else stderr_stream
        self._clock = time.monotonic if clock is None else clock
        # 见 `run_turn` 里 `except asyncio.CancelledError` 分支的说明（PR #173
        # 独立复核 P1-3）：默认 False，保持一次性 turn 模式 CLI 的既有行为
        # （stdout 必须恰好一个 JSON 报告，取消也要留下可辨认的失败回合）。
        self._propagate_cancellation = propagate_cancellation

    @property
    def policy(self) -> ToolPolicy:
        return self._policy

    @property
    def gateway(self) -> ToolGateway:
        return self._gateway

    def build_session_options(self) -> Any:
        """构造并缓存会话选项。

        ``hooks`` 是会话级配置，因此只建一次；每回合的隔离靠 ``start_turn()``，
        不靠重建会话。
        """

        if self._options is None:
            self._options = build_agent_options(
                self._gateway,
                allowed_tools=(self._config.read_only_tool,),
                max_turns=self._config.max_turns,
                mcp_servers=self._config.mcp_servers,
                cwd=self._config.workspace,
                model=self._config.model,
                system_prompt=self._config.system_prompt,
                stderr_sink=self._sdk_stderr_sink,
            )
        return self._options

    def _sdk_stderr_sink(self, line: object) -> None:
        """SDK 子进程 stderr 的唯一落点：脱敏、截断、结构化，带 trace_id。

        不设回调时子进程直接继承 fd 2，启动失败的原始错误（可能含令牌）会绕过
        全部出口纪律（Codex 复查发现）。"""

        text = redact_free_text(str(line))[:500]
        self._emit_stderr_record(level="warning", event="worker.sdk.stderr", line=text)

    def _emit_stderr_record(self, **fields: object) -> None:
        """结构化 stderr 输出的唯一出口：每行一个 JSON 对象，恒带 trace_id。"""

        record = {"trace_id": self._config.trace_id, **fields}
        self._stderr_stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
        self._stderr_stream.write("\n")
        self._stderr_stream.flush()

    async def run_turn(
        self,
        question: str,
        *,
        resume_session_id: str | None = None,
        stop_event: asyncio.Event | None = None,
        on_stream_event: Callable[[Mapping[str, Any]], None] | None = None,
        external_texts: Iterable[tuple[str, object]] | Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        """执行一个回合，**总是**返回一份报告。

        会话抛错也要出报告：一个只在日志里留下堆栈的失败回合，对上层来说与"什么都
        没发生"无法区分，而这两者的处置完全不同。
        """

        normalized_external_texts = normalize_external_texts(external_texts)
        agent_prompt = compose_agent_prompt(question, normalized_external_texts)
        self._audit.start_turn()
        recorder = TurnStreamRecorder(self._audit)
        failure: dict[str, str] | None = None
        started_at = self._clock()
        # #143：业务耗时由适配器在业务阶段结束时回填一次；收尾耗时事后用
        # "总耗时 - 业务耗时" 推得。适配器没有机会调用回调时（例如构造选项就
        # 失败）保持未知，不能构造数据。
        business_phase: dict[str, float] = {}
        # #201：本进程是否**真的**向 SDK 发出过 `interrupt()`。这是本地事实，不是
        # 对队列侧 stop 标志的推断——只有它成立，才允许把 SDK 自报的 `aborted_*`
        # 收尾判成中断（见 `_sdk_termination_failure`）。
        local_interrupt: dict[str, bool] = {}

        def handle_event(event: Mapping[str, Any]) -> None:
            recorder.handle(event)
            if on_stream_event is not None:
                on_stream_event(event)

        try:
            try:
                options = self.build_session_options()
            except ImportError as error:
                # 只有"构造会话选项时就 import 不到 SDK"才叫 sdk_unavailable；
                # 会话中途的 ImportError（缺传递依赖、缺 CLI 组件）是另一种故障，
                # 标错码会把排障方向带偏成"去装 SDK"（独立复查发现）。
                failure = _failure("sdk_unavailable", error)
                options = None
            if options is not None:
                await run_single_turn(
                    options=options,
                    prompt=agent_prompt,
                    sink=handle_event,
                    timeout_seconds=self._config.turn_timeout_seconds,
                    resume_session_id=resume_session_id,
                    stop_event=stop_event,
                    drain_grace_seconds=self._config.drain_grace_seconds,
                    clock=self._clock,
                    on_business_duration=lambda seconds: business_phase.__setitem__("seconds", seconds),
                    on_interrupt_requested=lambda: local_interrupt.__setitem__("requested", True),
                )
        except AgentSessionInterrupted as error:
            failure = _failure("interrupted", error)
        except DrainTimeoutError as error:
            # 收尾本身超过独立宽限：与业务墙钟超时是不同的失败原因，不得混报
            # 成 turn_timeout（#143：收尾宽限独立且有界）。
            failure = _failure_message(
                "drain_timeout", "任务已完成业务执行但收尾超过独立宽限，终态或用量可能不完整"
            )
            del error
        except TimeoutError as error:
            # 墙钟超时是明确的会话失败：SDK 传输挂住不发终止消息时，
            # 没有这个分支整个回合会永久等待（Codex 复查发现）。
            del error
            failure = _failure_message("turn_timeout", "任务提前结束：达到墙钟上限，结果可能不完整")
        except asyncio.CancelledError:
            # 默认（一次性 turn 模式 CLI）：BaseException 不接住就没有报告，违反
            # cli 的 stdout 契约「恰好一个 JSON 对象」；取消也要留下一份可辨认的
            # 失败回合，不能伪装成正常完成或墙钟超时。
            #
            # `propagate_cancellation=True`（常驻 queue worker，Issue #153 / PR #173
            # 独立复核 P1-3）：**必须原样重新抛出，不能吞。** 这里如果像默认那样
            # 就地生成一份"cancelled"失败报告，`run_turn()` 就会正常返回而不是
            # 让异常继续传播——`_process_task` 随后会把这份"正常返回的报告"当成
            # 一次真实完成的回合，同步写一条 FAILED 终态并把任务转入
            # `awaiting_delivery`。但这次取消来自 `_run_queue_worker` 的 SIGTERM
            # 停机预算耗尽，此时 Agent SDK 传输側可能仍在收尾甚至仍在执行
            # ——写一条"已取消、未继续执行"的终态既可能是假话，也绕开了
            # V-部署-12/`reclaim_stale_with_outcomes` 那条已验证的心跳超时回收
            # 路径。真实证据：`tests/test_worker_process.py` 的
            # ``QueueModeSigtermWithInFlightTaskTest`` 在改成 ``propagate_cancellation=True``
            # 之前会看到任务被写成 ``awaiting_delivery`` 而不是保持 ``running``。
            if self._propagate_cancellation:
                raise
            failure = _failure_message("cancelled", "任务已取消，未继续执行")
        except KeyboardInterrupt as error:
            failure = _failure("interrupted", error)
        except Exception as error:  # noqa: BLE001 - 入口必须把任何失败变成一份报告
            failure = _failure("session_failed", error)

        if failure is None:
            failure = _sdk_termination_failure(
                recorder, interrupt_requested=bool(local_interrupt.get("requested"))
            )
            if failure is not None and failure["code"] == "interrupted":
                # 这条线只在 #201 的竞态真的发生时出现一次：本进程已发出
                # `interrupt()`，SDK 却抢在它返回之前把这一轮以 `aborted_*` 收完，
                # 因此 `AgentSessionInterrupted` 没有抛出。留一条可计数的低敏结构化
                # 痕迹（不含提示词或正文），用来观察该竞态在真实链路的发生频率。
                self._emit_stderr_record(
                    level="warning",
                    event="worker.stop.interrupt_race",
                    terminal_reason=recorder.terminal_reason,
                )

        duration_seconds = max(0.0, self._clock() - started_at)
        business_duration_seconds = business_phase.get("seconds")
        drain_duration_seconds = (
            max(0.0, duration_seconds - business_duration_seconds)
            if business_duration_seconds is not None
            else None
        )

        final_text = recorder.final_text
        if self._config.output_safety_canary is not None and failure is None:
            # 默认关闭；开启时 WorkerConfig.__post_init__ 已保证 system_prompt
            # 非空。**只对没有失败的回合注入**（独立审核 F1）：超时、会话失败、
            # 中断等真实失败必须保留原样的失败终态——注入会让 withheld 覆盖
            # 真实失败原因，验收拿到的就是假证据。注入留一条低敏结构化痕迹
            # （不含提示词或正文原文），让验收能从 Worker 日志确认"这一轮的
            # 安全终态出自 canary，不是真实泄露"。
            final_text = _inject_output_safety_canary(
                final_text,
                mode=self._config.output_safety_canary,
                system_prompt=self._config.system_prompt or "",
            )
            self._emit_stderr_record(
                level="warning",
                event="worker.output_safety_canary_injected",
                mode=self._config.output_safety_canary,
            )

        return build_report(
            trace_id=self._config.trace_id,
            question=question,
            allowed_tools=self._policy.allowed_tools,
            summary=self._audit.summary(),
            stream=recorder,
            final_text=final_text,
            duration_seconds=duration_seconds,
            failure=failure,
            external_texts=tuple(text for _, text in normalized_external_texts),
            system_prompt=self._config.system_prompt,
            business_execution_budget_seconds=self._config.turn_timeout_seconds,
            business_duration_seconds=business_duration_seconds,
            drain_duration_seconds=drain_duration_seconds,
        )


def _failure(code: str, error: BaseException) -> dict[str, str]:
    # 异常正文可能带上连接串、路径或令牌，按自由文本脱敏后再截断。
    text = redact_free_text(f"{type(error).__name__}: {error}")[:_MAX_FAILURE_TEXT]
    return {"code": code, "message": text}


def _failure_message(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": redact_free_text(message)[:_MAX_FAILURE_TEXT]}


def _sdk_termination_failure(
    recorder: TurnStreamRecorder, *, interrupt_requested: bool = False
) -> dict[str, str] | None:
    """把 SDK 的终止元数据收口成产品可区分的护栏原因码。

    ``interrupt_requested`` 是**本进程已经向 SDK 发出 ``interrupt()``** 这一本地
    事实（#201，由适配器的 ``on_interrupt_requested`` 回填），只影响
    ``aborted_streaming``/``aborted_tools`` 这一档的归类：

    - 已发出中断 → ``interrupted``：这次 abort 就是本地 ``/stop`` 造成的，用户看到
      "任务已停止"而不是通用失败文案（终态由 ``apps/worker/service.py`` 按
      ``failure_code == "interrupted"`` 收口成 ``stopped``，本文件不参与那条判定）。
      正常情况下适配器会抛 ``AgentSessionInterrupted``；只有 SDK 抢在 ``interrupt()``
      返回之前就收完这一轮时才会落到这里。
    - 未发出中断 → ``cancelled``：``aborted_*`` 是 SDK 自报的，无人 stop 时也会出现，
      它必须保持失败语义。**不得**用队列侧"有人请求过停止"反推因果——那会让一次
      晚到的 stop 掩盖真实的 SDK 终止失败（PR #198 一级独立审查 P1-2 裁定）。
    """

    if recorder.terminal_reason in {"max_turns"} or recorder.result_subtype == "error_max_turns":
        return _failure_message("max_turns_exceeded", "任务提前结束：达到 Agent 轮数上限，结果可能不完整")
    if recorder.terminal_reason in {"aborted_streaming", "aborted_tools"}:
        if interrupt_requested:
            return _failure_message("interrupted", "任务已按停止请求中断，未继续执行")
        return _failure_message("cancelled", "任务已取消，未继续执行")
    return None
