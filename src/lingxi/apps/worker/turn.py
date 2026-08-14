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

from .config import WorkerConfig
from .report import build_report

_MAX_FAILURE_TEXT = 500


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
        record = {
            "level": "warning",
            "event": "worker.sdk.stderr",
            "trace_id": self._config.trace_id,
            "line": text,
        }
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
            # BaseException 不接住就没有报告，违反 cli 的 stdout 契约
            # 「恰好一个 JSON 对象」；取消也要留下一份可辨认的失败回合，不能伪装成
            # 正常完成或墙钟超时。
            failure = _failure_message("cancelled", "任务已取消，未继续执行")
        except KeyboardInterrupt as error:
            failure = _failure("interrupted", error)
        except Exception as error:  # noqa: BLE001 - 入口必须把任何失败变成一份报告
            failure = _failure("session_failed", error)

        if failure is None:
            failure = _sdk_termination_failure(recorder)

        duration_seconds = max(0.0, self._clock() - started_at)
        business_duration_seconds = business_phase.get("seconds")
        drain_duration_seconds = (
            max(0.0, duration_seconds - business_duration_seconds)
            if business_duration_seconds is not None
            else None
        )

        return build_report(
            trace_id=self._config.trace_id,
            question=question,
            allowed_tools=self._policy.allowed_tools,
            summary=self._audit.summary(),
            stream=recorder,
            final_text=recorder.final_text,
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


def _sdk_termination_failure(recorder: TurnStreamRecorder) -> dict[str, str] | None:
    """把 SDK 的终止元数据收口成产品可区分的护栏原因码。"""

    if recorder.terminal_reason in {"max_turns"} or recorder.result_subtype == "error_max_turns":
        return _failure_message("max_turns_exceeded", "任务提前结束：达到 Agent 轮数上限，结果可能不完整")
    if recorder.terminal_reason in {"aborted_streaming", "aborted_tools"}:
        return _failure_message("cancelled", "任务已取消，未继续执行")
    return None
