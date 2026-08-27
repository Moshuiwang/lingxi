"""一个受控回合的装配与执行。

本文件是 Issue #37 的全部要点所在：**把已验证的组件真的接起来**。

- ``ToolPolicy``：白名单只含配置里那些只读 MCP 工具（Issue #291 起支持多值），
  其余一律默认拒绝；
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
    is_message_buffer_overflow,
    run_single_turn,
)
from lingxi.core.execution.audit import AuditRedactor, ResultRules, TurnAudit, redact_free_text
from lingxi.core.execution.document_delivery import (
    DELIVER_DOCUMENT_TOOL_NAME,
    DELIVERY_MCP_SERVER_NAME,
    DocumentDeliveryError,
    DocumentRequest,
    build_document_request,
)
from lingxi.core.execution.hooks import ToolGateway
from lingxi.core.execution.input_safety import compose_agent_prompt, normalize_external_texts
from lingxi.core.execution.message_stream import TurnStreamRecorder
from lingxi.core.execution.tool_policy import ToolPolicy
from lingxi.core.innertest_content_capture import ContentCaptureRecord, RawTurnCapture

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
        capture_raw_content: bool = False,
    ) -> None:
        self._config = config
        # 文档交付触发机制（Issue #341 S-ES-2，审定设计第 3 条）：字面量只并入
        # ToolPolicy 的实际判定集合，**不塞进 config.read_only_tools**——那个字段
        # 的装配期校验（apps/worker/config.py::_read_only_tools）要求每一项都是
        # mcp__query__ 前缀的问数只读工具，这是字段名本身的名义纪律；本地变量
        # ``policy_tools`` 只影响这一次会话构造，不回写 config。
        policy_tools = config.read_only_tools
        if config.document_delivery_enabled:
            policy_tools = tuple(policy_tools) + (DELIVER_DOCUMENT_TOOL_NAME,)
        self._policy = ToolPolicy(allowed_tools=policy_tools)
        self._audit = TurnAudit(
            rules=ResultRules(failure_text_markers=config.failure_text_markers),
            redactor=AuditRedactor(allowed_input_fields=config.audit_input_fields),
        )
        # 内测轮内容级采集（Issue #251/#304 批次 3）：默认 False，此时
        # `self._raw_capture` 恒为 None，不构造任何收集器、不产生任何额外开销
        # ——这正是"默认关闭可被断言证明"在这一层的形状。`apps/worker/service.py`
        # 按 `WorkerConfig.innertest_content_capture_enabled` 决定是否传 True。
        self._raw_capture = RawTurnCapture() if capture_raw_content else None
        self._gateway = ToolGateway(
            policy=self._policy,
            audit=self._audit,
            mark_external_side_effect=mark_external_side_effect,
            raw_pre_tool_use=(self._raw_capture.on_pre_tool_use if self._raw_capture else None),
        )
        self._options: Any = None
        self._stderr_stream = sys.stderr if stderr_stream is None else stderr_stream
        self._clock = time.monotonic if clock is None else clock
        # 见 `run_turn` 里 `except asyncio.CancelledError` 分支的说明（PR #173
        # 独立复核 P1-3）：默认 False，保持一次性 turn 模式 CLI 的既有行为
        # （stdout 必须恰好一个 JSON 报告，取消也要留下可辨认的失败回合）。
        self._propagate_cancellation = propagate_cancellation
        # 文档交付触发机制（Issue #341 S-ES-2）：本轮登记的文档请求。回合级状态，
        # 在 run_turn() 开头与 self._audit.start_turn() 同时重置——不跨回合复用，
        # 否则第二回合会带着上一回合遗留的请求。
        self._document_request: DocumentRequest | None = None

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

        ``allowed_tools`` 传的是 ``self._policy.allowed_tools``（ToolPolicy 实际
        判定的完整集合），不是 ``self._config.read_only_tools``——与
        ``run_turn()`` 最终调用 ``build_report(allowed_tools=self._policy.
        allowed_tools, ...)`` 是同一个既有惯例：SDK 侧 ``allowed_tools`` 只是纵深
        防御（真正判定层是 hooks 里的 PreToolUse），但一个"whole tool"级别的
        ``allowed_tools`` 条目会让 SDK 在到达我们的回调前就自动放行——开关开启
        时如果这里仍只传 ``config.read_only_tools``，``mcp__delivery__
        deliver_document`` 就不在这份"已预先批准"的名单里，dontAsk 权限模式会
        在我们的 PreToolUse 判定之前就拒绝这次调用，功能上根本打不通；两处必须
        用同一份集合，不能一个用 config 原始值、一个用合并后的值。
        """

        if self._options is None:
            mcp_servers = dict(self._config.mcp_servers)
            if self._config.document_delivery_enabled:
                mcp_servers[DELIVERY_MCP_SERVER_NAME] = self._build_delivery_mcp_server()
            self._options = build_agent_options(
                self._gateway,
                allowed_tools=self._policy.allowed_tools,
                max_turns=self._config.max_turns,
                mcp_servers=mcp_servers,
                cwd=self._config.workspace,
                model=self._config.model,
                system_prompt=self._config.system_prompt,
                stderr_sink=self._sdk_stderr_sink,
            )
        return self._options

    def _build_delivery_mcp_server(self) -> Any:
        """装配进程内 SDK MCP 服务（Issue #341 S-ES-2 审定设计第 1 条）。

        处理函数**不发任何外部请求**：只做校验（``build_document_request``）并
        把请求登记为进程内"本轮文档请求"，返回明确的成功/失败文案给模型。
        第三方 SDK 的 import 延迟到这里——仓库约定 ``src/lingxi/`` 不做模块级
        第三方 import（见 ``pyproject.toml`` 顶部说明），只有真正开启这个开关的
        进程才会付出这次 import 成本。
        """

        from claude_agent_sdk import create_sdk_mcp_server, tool as sdk_tool

        executor = self

        @sdk_tool(
            "deliver_document",
            "登记一次文档交付请求：任务完成后系统会为用户生成该文档。"
            "此工具只登记请求本身，不会立即发送任何内容给用户，也不产生任何"
            "外部副作用；同一回合内多次调用以最后一次为准。",
            {"title": str, "markdown": str},
        )
        async def deliver_document(args: dict[str, Any]) -> dict[str, Any]:
            return executor._handle_deliver_document(args.get("title"), args.get("markdown"))

        return create_sdk_mcp_server(name=DELIVERY_MCP_SERVER_NAME, tools=[deliver_document])

    def _handle_deliver_document(self, title: Any, markdown: Any) -> dict[str, Any]:
        """``deliver_document`` 工具调用的唯一处理逻辑（同步、无外部请求）。"""

        if not self._config.document_delivery_enabled:
            # 正常装配下这个分支不可达——服务器只在开关开启时才会被挂载
            # （见 build_session_options）。保留这道防御是纵深防线，不是主判定
            # 层：真正阻止未开关时调用这个工具的是它压根不会出现在这一回合的
            # mcp_servers/ToolPolicy 白名单里。
            self._emit_stderr_record(
                level="warning", event="worker.document_request_rejected", reason="disabled"
            )
            return {
                "content": [{"type": "text", "text": "文档交付能力当前未开启。"}],
                "is_error": True,
            }
        try:
            request = build_document_request(
                title=title,
                markdown=markdown,
                forbidden_values=self._config.external_texts,
                internal_tool_names=self._policy.allowed_tools,
                system_prompt=self._config.system_prompt,
            )
        except DocumentDeliveryError as error:
            self._emit_stderr_record(
                level="warning",
                event="worker.document_request_rejected",
                reason=error.reason_code,
            )
            return {
                "content": [{"type": "text", "text": f"文档请求被拒绝：{error}"}],
                "is_error": True,
            }
        replaced = self._document_request is not None
        self._document_request = request
        if replaced:
            self._emit_stderr_record(level="warning", event="worker.document_request_replaced")
        self._emit_stderr_record(
            level="warning",
            event="worker.document_request_registered",
            paragraph_count=len(request.paragraphs),
            total_chars=request.total_chars,
        )
        return {
            "content": [
                {"type": "text", "text": "文档请求已登记，任务完成后为用户生成。"}
            ]
        }

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
        # 文档交付触发机制（Issue #341 S-ES-2）：回合级状态，与审计一起清零——
        # 不清零的话，第二回合在模型没有再次调用这个工具时也会带着上一回合
        # 登记过的请求，与"仅当模型本轮调用过该工具时非空"的报告契约矛盾。
        self._document_request = None
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
            if self._raw_capture is not None:
                self._raw_capture.on_stream_event(event)
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
            if is_message_buffer_overflow(error):
                # 工具回执大到超过 SDK 读流缓冲上限（典型：未加窄过滤条件的全量
                # 指标查询，2026-08-23 真实故障）。这不是"稍后重试"能解决的瞬态
                # 故障——同样的问题重试必然同样失败，必须与通用 session_failed
                # 区分，让用户得到"缩小查询范围"的可行动建议（queue 收口的文案
                # 映射见 apps/worker/service.py 的 _failure_content）。
                failure = _failure("result_too_large", error)
            else:
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
            # 仅当任务成功（本地 ``failure is None``，与上面 output_safety_canary
            # 注入判断同一个信号）且模型本轮确实调用过该工具时才非空——真实失败
            # 回合即使工具调用发生在失败之前也不承诺这份请求会被消费。
            document_request=self._document_request if failure is None else None,
        )

    def build_content_capture_record(
        self, *, task_id: str, worker_id: str, question: str
    ) -> ContentCaptureRecord | None:
        """内测轮内容级采集的落库记录，只在 ``capture_raw_content=True`` 时非空。

        必须在 :meth:`run_turn` 返回**之后**调用——``self._audit.summary()`` 读的是
        本回合累积到此刻的记账，与 :meth:`run_turn` 内部传给 ``build_report`` 的
        是同一份纯投影（``TurnAudit.summary()`` 不产生副作用，调用两次结果一致）。
        调用方（``apps/worker/service.py``）拿到非 None 结果后仍需自行按开关决定
        是否真的写库——这个方法只负责"有没有可采集的内容"，不做写库判断。
        """

        if self._raw_capture is None:
            return None
        return self._raw_capture.build_record(
            task_id=task_id,
            worker_id=worker_id,
            question=question,
            summary=self._audit.summary(),
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
