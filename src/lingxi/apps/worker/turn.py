"""一个受控回合的装配与执行：把已验证的组件真的接起来。

- ``ToolPolicy``：白名单只含配置里那些只读 MCP 工具，其余一律默认拒绝；
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
import re
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from lingxi.adapters.claude_agent_session import (
    AgentSessionInterruptedError,
    DrainTimeoutError,
    build_agent_options,
    is_message_buffer_overflow,
    run_single_turn,
)
from lingxi.core.execution.audit import AuditRedactor, ResultRules, TurnAudit, redact_free_text
from lingxi.core.execution.document_delivery import (
    DELIVER_DOCUMENT_TOOL_NAME,
    DELIVER_SPREADSHEET_TOOL_NAME,
    DELIVERY_MCP_SERVER_NAME,
    DocumentDeliveryError,
    DocumentRequest,
    SheetRequest,
    build_document_request,
    build_sheet_request,
)
from lingxi.core.execution.hooks import WRAPPER_DENIAL_FUSE_THRESHOLD, ToolGateway
from lingxi.core.execution.input_safety import compose_agent_prompt, normalize_external_texts
from lingxi.core.execution.message_stream import TurnStreamRecorder
from lingxi.core.execution.tool_policy import ToolPolicy
from lingxi.core.innertest_content_capture import ContentCaptureRecord, RawTurnCapture
from lingxi.core.mcp_naming import QUERY_MCP_SERVER_NAME

from .config import OUTPUT_SAFETY_CANARY_SURVIVOR_BODY, WorkerConfig
from .report import build_report
from .report_extraction import failure_with_signature

_MAX_FAILURE_TEXT = 500

# MCP 会话级连接失败审计：只有 failed/needs-auth 是明确的故障态，值得响亮
# 告警——pending（慢连接建连时可能还没到终态）、disabled（主动关闭）、
# connected 都不触发；判定只在这一层，适配器只转发 SDK 原始 dict。
_MCP_UNAVAILABLE_STATUSES = frozenset({"failed", "needs-auth"})
#: 下游指标 MCP 建连失败的稳定取值：只在 query 服务报告 failed/needs-auth
#: 且错误文本明确带 HTTP 502 时使用；签名形状与其余失败签名一致，低敏、
#: 可落库、可在 /admin trace 回显。
MCP_BAD_GATEWAY_FAILURE_CODE = "mcp_bad_gateway"
MCP_BAD_GATEWAY_FAILURE_SIGNATURE = "mcp.query.http_502"
_MCP_BAD_GATEWAY_ERROR = re.compile(
    r"(?:\b502\s+bad\s+gateway\b|\bhttp(?:/\d+(?:\.\d+)?)?\s*[:=]?\s*502\b|"
    r"\b(?:status|status[_ -]?code|code)\s*[:=]\s*['\"]?502\b)",
    re.IGNORECASE,
)

# 会话 resume 失配的特征串：worker 容器重建后 CLI 会话文件消失，但数据库里
# 仍记着旧的 agent_session_id，下一条消息会尝试 resume 一个不存在的会话。
# 真实 CLI 子进程的失败形状是自己 stderr 里一行固定英文前缀（SDK 侧异常
# 不携带这行原文），因此只能从 `_sdk_stderr_sink` 落过的文本与异常对象本身
# 两处认；只认固定前缀，不含会话 id 本身（避免被脱敏规则处理成不同形态）。
_SESSION_RESUME_MISS_PATTERN = re.compile(r"no conversation found with session id", re.IGNORECASE)


def _looks_like_session_resume_miss(text: str) -> bool:
    """这段文本是否带着「resume 的会话已经不存在」的特征。"""
    return bool(_SESSION_RESUME_MISS_PATTERN.search(text))


async def _forward_first_signal(sources: tuple[asyncio.Event, ...], target: asyncio.Event) -> None:
    """任一 ``sources`` 先被 set，就把 ``target`` 一起 set。

    包装拒绝熔断需要在不改动 ``run_single_turn`` 现有 "/stop → interrupt()"
    协议的前提下，让"本轮熔断触发"也能走同一条已验证的中断路径。这个协程
    只做转发，不判断"为什么"要中断，也不 ``.set()`` 调用方传入的事件对象；
    事后谁触发的、要不要改写失败原因，由 ``run_turn`` 按 ``fuse_event.
    is_set()`` 另行判断。
    """
    if not sources:
        return
    waiters = [asyncio.ensure_future(source.wait()) for source in sources]
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            if not waiter.done():
                waiter.cancel()
    target.set()


def _inject_output_safety_canary(final_text: str, *, mode: str, system_prompt: str) -> str:
    """把合成 system prompt 确定性注入最终正文，供出口安全约束命中。

    两个档位都不依赖模型行为（早期教训：触发条件寄托在"模型恰好复述提示词"
    上，真实链路一次都没触发过）：``withheld`` 整段替换为 system prompt，
    必然整串命中；``masked`` 固定幸存句 + 模型正文 + system prompt——幸存句
    不含任何已知敏感模式，且配置期已双向拒绝"合成提示与幸存句/固定终态文案
    互为子串"的形态，因此无论模型正文是什么，幸存句都保证命中区间之外有
    真实内容。注入发生在 ``build_report``（出口约束所在地）之前，走的是与
    真实泄露完全相同的检测与投影路径。
    """
    if mode == "withheld":
        return system_prompt
    base = (
        f"{OUTPUT_SAFETY_CANARY_SURVIVOR_BODY}\n{final_text}"
        if final_text
        else OUTPUT_SAFETY_CANARY_SURVIVOR_BODY
    )
    return f"{base}\n{system_prompt}"


def _policy_tool_names(config: WorkerConfig) -> tuple[str, ...]:
    """``ToolPolicy`` 实际判定的完整工具集合：只读工具 + 按需并入的交付工具。

    不塞进 ``config.read_only_tools``——那个字段的装配期校验要求每一项都是
    ``mcp__query__`` 前缀的问数只读工具，是字段名本身的名义纪律；这里只影响
    这一次会话构造，不回写 config。
    """
    tools = config.read_only_tools
    if config.document_delivery_enabled:
        tools = tuple(tools) + (DELIVER_DOCUMENT_TOOL_NAME, DELIVER_SPREADSHEET_TOOL_NAME)
    return tools


@dataclass(frozen=True)
class _TurnAttemptResult:
    """``run_turn`` 循环里一次尝试的结果，供外层判断重试还是收尾。"""

    recorder: TurnStreamRecorder
    failure: dict[str, str] | None
    business_duration_seconds: float | None


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
        """装配会话级对象（策略、审计、网关）与回合级状态槽位。

        回合级状态（文档/表格交付请求、resume 失配与 MCP 502 探测标志）在
        这里只做防御性初始化，真正按次重置在 ``run_turn`` 循环开头。
        """
        self._config = config
        self._policy = ToolPolicy(allowed_tools=_policy_tool_names(config))
        self._audit = TurnAudit(
            rules=ResultRules(failure_text_markers=config.failure_text_markers),
            redactor=AuditRedactor(allowed_input_fields=config.audit_input_fields),
        )
        # 默认 False 时 self._raw_capture 恒为 None，不构造任何收集器——这正是
        # "默认关闭可被断言证明"在这一层的形状。
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
        # 默认 False，保持一次性 turn 模式 CLI 的既有行为（stdout 必须恰好
        # 一个 JSON 报告，取消也要留下可辨认的失败回合）；常驻 queue worker
        # 传 True，见 run_turn 内 CancelledError 分支的说明。
        self._propagate_cancellation = propagate_cancellation
        # 保持这两个属性名/类型/既有语义逐字不变：既有测试直接读
        # executor._document_request，改名会连坐一批与本次改动无关的断言。
        self._document_request: DocumentRequest | None = None
        self._sheet_request: SheetRequest | None = None
        self._resume_miss_detected = False
        self._mcp_bad_gateway_detected = False

    @property
    def policy(self) -> ToolPolicy:
        """本次会话装配好的只读工具白名单判定器。"""
        return self._policy

    @property
    def gateway(self) -> ToolGateway:
        """本次会话装配好的 hooks 判定层，唯一的 PreToolUse 判定入口。"""
        return self._gateway

    def build_session_options(self) -> Any:
        """构造并缓存会话选项。

        ``hooks`` 是会话级配置，因此只建一次；每回合的隔离靠 ``start_turn()``。
        ``allowed_tools`` 传的是 ``self._policy.allowed_tools``（完整判定
        集合），不是 ``self._config.read_only_tools``——SDK 侧 ``allowed_
        tools`` 是纵深防御，真正判定层是 hooks 里的 PreToolUse，但一个
        "whole tool"级别的条目会让 SDK 在到达回调前就自动放行；开关开启时
        如果这里仍只传只读工具，交付工具的 dontAsk 权限会在 PreToolUse 之前
        就被拒绝，功能上根本打不通，两处必须用同一份集合。
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
        """装配进程内 SDK MCP 服务，承载 ``deliver_document``/``deliver_spreadsheet``。

        处理函数**不发任何外部请求**：只做校验并把请求登记为进程内"本轮交付
        请求"，返回明确的成功/失败文案给模型。第三方 SDK 的 import 延迟到
        这里——仓库约定 ``src/lingxi/`` 不做模块级第三方 import，只有真正
        开启这个开关的进程才会付出这次 import 成本。
        """
        from claude_agent_sdk import create_sdk_mcp_server
        from claude_agent_sdk import tool as sdk_tool

        executor = self

        @sdk_tool(
            "deliver_document",
            "登记一次文档交付请求：任务完成后系统会尝试为用户生成该文档；"
            "若生成失败，用户会收到通知（不是无条件承诺一定成功）。"
            "此工具只登记请求本身，不会立即发送任何内容给用户，也不产生任何"
            "外部副作用；同一回合内多次调用（含调用 deliver_spreadsheet）以"
            "最后一次为准。",
            {"title": str, "markdown": str},
        )
        async def deliver_document(args: dict[str, Any]) -> dict[str, Any]:
            return executor._handle_deliver_document(args.get("title"), args.get("markdown"))

        @sdk_tool(
            "deliver_spreadsheet",
            "登记一次电子表格交付请求：任务完成后系统会尝试为用户生成该表格；"
            "若生成失败，用户会收到通知（不是无条件承诺一定成功）。"
            "rows 是行×列的单元格文本二维数组，每一行是一个字符串列表；"
            "此工具只登记请求本身，不会立即发送任何内容给用户，也不产生任何"
            "外部副作用；同一回合内多次调用（含调用 deliver_document）以"
            "最后一次为准。",
            {"title": str, "rows": list},
        )
        async def deliver_spreadsheet(args: dict[str, Any]) -> dict[str, Any]:
            return executor._handle_deliver_spreadsheet(args.get("title"), args.get("rows"))

        return create_sdk_mcp_server(
            name=DELIVERY_MCP_SERVER_NAME, tools=[deliver_document, deliver_spreadsheet]
        )

    def _handle_deliver_document(self, title: Any, markdown: Any) -> dict[str, Any]:
        """``deliver_document`` 工具调用的唯一处理逻辑（同步、无外部请求）。"""
        if not self._config.document_delivery_enabled:
            # 正常装配下这个分支不可达（服务器只在开关开启时才会被挂载）；
            # 保留这道防御是纵深防线，不是主判定层。
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
                # config.external_texts 是 (键, 文本) 对的元组，不是文本本身，
                # 只取文本值——与 run_turn 里 build_report 的拆法一致，避免
                # 把整个二元组当成一条禁止值传给出口安全检查。
                forbidden_values=tuple(text for _, text in self._config.external_texts),
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
        # 跨类型互斥：登记文档请求时清空表格槽位——"同一回合内多次调用以
        # 最后一次为准"扩展到跨类型同样成立，两个槽位不会同时非空。
        replaced = self._document_request is not None or self._sheet_request is not None
        self._document_request = request
        self._sheet_request = None
        if replaced:
            self._emit_stderr_record(level="warning", event="worker.document_request_replaced")
        self._emit_stderr_record(
            level="warning",
            event="worker.document_request_registered",
            paragraph_count=len(request.paragraphs),
            total_chars=request.total_chars,
        )
        # 措辞不再无条件承诺"会生成"，只承诺"已登记；失败会有通知"，与工具
        # 描述、用户可见失败文案同向，模型据此回复用户时不会说出兑现不了的话。
        return {"content": [{"type": "text", "text": "已登记文档请求；若生成失败你会收到通知。"}]}

    def _handle_deliver_spreadsheet(self, title: Any, rows: Any) -> dict[str, Any]:
        """``deliver_spreadsheet`` 工具调用的唯一处理逻辑（同步、无外部请求）。

        与 :meth:`_handle_deliver_document` 逐项对称，差异除校验函数与登记的
        审计字段（``row_count`` 而不是 ``paragraph_count``）外，拒绝事件名
        也不同：本分支记 ``worker.sheet_request_rejected``，不复用文档分支
        的事件名，运维按事件名过滤时才能区分来源。
        """
        if not self._config.document_delivery_enabled:
            self._emit_stderr_record(
                level="warning", event="worker.sheet_request_rejected", reason="disabled"
            )
            return {
                "content": [{"type": "text", "text": "表格交付能力当前未开启。"}],
                "is_error": True,
            }
        try:
            request = build_sheet_request(
                title=title,
                rows=rows,
                forbidden_values=tuple(text for _, text in self._config.external_texts),
                internal_tool_names=self._policy.allowed_tools,
                system_prompt=self._config.system_prompt,
            )
        except DocumentDeliveryError as error:
            self._emit_stderr_record(
                level="warning",
                event="worker.sheet_request_rejected",
                reason=error.reason_code,
            )
            return {
                "content": [{"type": "text", "text": f"表格请求被拒绝：{error}"}],
                "is_error": True,
            }
        # 跨类型互斥：同上，登记表格请求时清空文档槽位。
        replaced = self._sheet_request is not None or self._document_request is not None
        self._sheet_request = request
        self._document_request = None
        if replaced:
            self._emit_stderr_record(level="warning", event="worker.document_request_replaced")
        self._emit_stderr_record(
            level="warning",
            event="worker.document_request_registered",
            row_count=len(request.rows),
            total_chars=request.total_chars,
        )
        return {"content": [{"type": "text", "text": "已登记表格请求；若生成失败你会收到通知。"}]}

    def _sdk_stderr_sink(self, line: object) -> None:
        """SDK 子进程 stderr 的唯一落点：脱敏、截断、结构化，带 trace_id。

        不设回调时子进程直接继承 fd 2，启动失败的原始错误（可能含令牌）会
        绕过全部出口纪律。这里在脱敏截断之后额外匹配会话 resume 失配特征串
        并置位 ``self._resume_miss_detected``，供 ``run_turn`` 决定是否降级
        重试；不改变这个回调本身落 ``worker.sdk.stderr`` 日志的行为。
        """
        text = redact_free_text(str(line))[:500]
        if _looks_like_session_resume_miss(text):
            self._resume_miss_detected = True
        self._emit_stderr_record(level="warning", event="worker.sdk.stderr", line=text)

    def _emit_stderr_record(self, **fields: object) -> None:
        """结构化 stderr 输出的唯一出口：每行一个 JSON 对象，恒带 trace_id。"""
        record = {"trace_id": self._config.trace_id, **fields}
        self._stderr_stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
        self._stderr_stream.write("\n")
        self._stderr_stream.flush()

    def _audit_mcp_status(self, status: object, *, task_id: str | None = None) -> None:
        """把建连后的 MCP server 状态转成响亮的审计事件。

        ``status`` 是适配器原样转发的 SDK 状态 dict，形状不对时如实什么都
        不做，不猜测。只对 ``failed``/``needs-auth`` 逐个 server 发一条
        ``worker.mcp_server_unavailable``（warning 级）——worker 进程结构上
        从不持有飞书出站凭据，唯一的信号出口是这条带 ``trace_id`` 的结构化
        stderr，与本文件其余告警事件同一条通道，不新开一条。字段只含 server
        名、status、error 文本三项，不含可能携带凭据形状内容的字段；
        ``error`` 经脱敏截断，因为它来自外部 CLI/MCP 服务。
        """
        if not isinstance(status, Mapping):
            return
        servers = status.get("mcpServers")
        if not isinstance(servers, (list, tuple)):
            return
        for server in servers:
            if not isinstance(server, Mapping):
                continue
            server_status = server.get("status")
            if server_status not in _MCP_UNAVAILABLE_STATUSES:
                continue
            name = server.get("name")
            error_text = server.get("error")
            if (
                name == QUERY_MCP_SERVER_NAME
                and isinstance(error_text, str)
                and _MCP_BAD_GATEWAY_ERROR.search(error_text)
            ):
                self._mcp_bad_gateway_detected = True
            self._emit_stderr_record(
                level="warning",
                event="worker.mcp_server_unavailable",
                server=name if isinstance(name, str) else None,
                status=server_status,
                error=(
                    redact_free_text(error_text)[:_MAX_FAILURE_TEXT]
                    if isinstance(error_text, str)
                    else None
                ),
                task_id=task_id,
            )

    def _classify_generic_failure(self, error: BaseException) -> dict[str, str]:
        """把一次未归类的异常分成 ``result_too_large`` 或 ``session_failed``。

        工具回执大到超过 SDK 读流缓冲上限时不是"稍后重试"能解决的瞬态故障，
        必须与通用 session_failed 区分，让用户得到"缩小查询范围"的可行动
        建议。正常情况下 resume 失配特征串已经由 ``_sdk_stderr_sink`` 从
        子进程 stderr 里认出来；这里再看一遍异常对象本身兜底，防止某个未来
        SDK 版本把这行原文塞进异常文本时降级路径悄悄失效。
        """
        if is_message_buffer_overflow(error):
            return _failure("result_too_large", error)
        failure = _failure("session_failed", error)
        if _looks_like_session_resume_miss(str(error)):
            self._resume_miss_detected = True
        return failure

    async def _call_run_single_turn(
        self,
        options: Any,
        agent_prompt: str,
        *,
        attempt_resume_session_id: str | None,
        effective_stop_event: asyncio.Event,
        handle_event: Callable[[Mapping[str, Any]], None],
        business_phase: dict[str, float],
        local_interrupt: dict[str, bool],
        task_id: str | None,
    ) -> None:
        """薄封装：把本方法这组尝试级回调接进 ``run_single_turn`` 一次调用。"""
        await run_single_turn(
            options=options,
            prompt=agent_prompt,
            sink=handle_event,
            timeout_seconds=self._config.turn_timeout_seconds,
            resume_session_id=attempt_resume_session_id,
            stop_event=effective_stop_event,
            drain_grace_seconds=self._config.drain_grace_seconds,
            clock=self._clock,
            on_business_duration=lambda seconds: business_phase.__setitem__("seconds", seconds),
            on_interrupt_requested=lambda: local_interrupt.__setitem__("requested", True),
            on_mcp_status=lambda status: self._audit_mcp_status(status, task_id=task_id),
        )

    async def _execute_single_turn(
        self,
        agent_prompt: str,
        *,
        attempt_resume_session_id: str | None,
        effective_stop_event: asyncio.Event,
        handle_event: Callable[[Mapping[str, Any]], None],
        business_phase: dict[str, float],
        local_interrupt: dict[str, bool],
        task_id: str | None,
    ) -> dict[str, str] | None:
        """跑一次 SDK 会话尝试，把已知异常路径分类成失败原因或重新抛出。

        ``CancelledError`` 默认转成可辨认的失败报告（一次性 turn CLI 需要
        stdout 恒有一个 JSON 对象）；``propagate_cancellation=True`` 的常驻
        queue worker 必须原样重新抛出，否则收口层会把"SIGTERM 停机预算耗尽"
        误当成"任务已取消"写终态，绕开心跳超时回收路径。
        """
        try:
            try:
                options = self.build_session_options()
            except ImportError as error:
                # 只有"构造会话选项时就 import 不到 SDK"才叫 sdk_unavailable；
                # 会话中途的 ImportError 是另一种故障，标错码会把排障方向带偏。
                return _failure("sdk_unavailable", error)
            await self._call_run_single_turn(
                options,
                agent_prompt,
                attempt_resume_session_id=attempt_resume_session_id,
                effective_stop_event=effective_stop_event,
                handle_event=handle_event,
                business_phase=business_phase,
                local_interrupt=local_interrupt,
                task_id=task_id,
            )
        except AgentSessionInterruptedError as error:
            return _failure("interrupted", error)
        except DrainTimeoutError as error:  # 收尾超过独立宽限，不得混报成 turn_timeout
            del error
            return _failure_message(
                "drain_timeout", "任务已完成业务执行但收尾超过独立宽限，终态或用量可能不完整"
            )
        except TimeoutError as error:  # 墙钟超时：没有这个分支整个回合会永久等待
            del error
            return _failure_message("turn_timeout", "任务提前结束：达到墙钟上限，结果可能不完整")
        except asyncio.CancelledError:
            if self._propagate_cancellation:
                raise
            return _failure_message("cancelled", "任务已取消，未继续执行")
        except KeyboardInterrupt as error:
            return _failure("interrupted", error)
        except Exception as error:  # 入口必须把任何失败变成一份报告
            return self._classify_generic_failure(error)
        return None

    def _finalize_attempt_failure(
        self,
        failure: dict[str, str] | None,
        *,
        recorder: TurnStreamRecorder,
        local_interrupt: Mapping[str, bool],
        fuse_event: asyncio.Event,
        task_id: str | None,
    ) -> dict[str, str] | None:
        """把 SDK 终止元数据、MCP 502、包装拒绝熔断三种信号依次叠加进失败判定。

        顺序不可换：终止元数据只在本地没有显式失败时才生效；502 是确定性
        下游失败，只在会话失败或未失败时覆盖，从结构上保证本次任务至多
        执行一次；熔断优先级最高、无条件覆盖前两者——它反映"系统主动提前
        收口"，不是会话本身的失败原因，复用现成的 ``max_turns_exceeded``
        失败路径，不新造用户文案。
        """
        if failure is None:
            failure = _sdk_termination_failure(
                recorder, interrupt_requested=bool(local_interrupt.get("requested"))
            )
            if failure is not None and failure["code"] == "interrupted":
                # 只在竞态真的发生时出现一次：本进程已发出 interrupt()，SDK
                # 却抢在它返回之前把这一轮以 aborted_* 收完。留一条可计数的
                # 低敏结构化痕迹，观察该竞态在真实链路的发生频率。
                self._emit_stderr_record(
                    level="warning",
                    event="worker.stop.interrupt_race",
                    terminal_reason=recorder.terminal_reason,
                )

        if self._mcp_bad_gateway_detected and (
            failure is None or failure.get("code") == "session_failed"
        ):
            failure = _mcp_bad_gateway_failure()

        if fuse_event.is_set():
            failure = _failure_message(
                "max_turns_exceeded",
                "任务提前终止：同一回合内包装拒绝达到熔断阈值，结果可能不完整",
            )

        return failure

    def _reset_attempt_state(
        self, fuse_event: asyncio.Event
    ) -> tuple[TurnStreamRecorder, dict[str, float], dict[str, bool]]:
        """每次尝试开头的状态复位，返回这次尝试专属的记录器与两个信号容器。

        ToolGateway 的拒绝计数、``fuse_event``、文档/表格交付槽位、resume
        失配与 MCP 502 探测标志都是"同一回合内"的窗口状态，不得带着上一次
        尝试（resume 降级重试）的痕迹进入这一轮判断；报告契约="仅当模型
        本轮调用过对应交付工具时非空"。
        """
        self._audit.start_turn()
        self._gateway.reset_wrapper_denial_fuse()
        fuse_event.clear()
        self._document_request = None
        self._sheet_request = None
        self._resume_miss_detected = False
        self._mcp_bad_gateway_detected = False
        return TurnStreamRecorder(self._audit), {}, {}

    async def _execute_attempt_with_forwarding(
        self,
        *,
        agent_prompt: str,
        attempt_resume_session_id: str | None,
        stop_event: asyncio.Event | None,
        fuse_event: asyncio.Event,
        handle_event: Callable[[Mapping[str, Any]], None],
        business_phase: dict[str, float],
        local_interrupt: dict[str, bool],
        task_id: str | None,
    ) -> dict[str, str] | None:
        """建立 /stop 与熔断信号的转发，跑一次尝试，收尾时收回转发任务。

        ``effective_stop_event`` 在"外部 /stop"与"本轮熔断"任一触发时都会
        被置位；这里只 ``.wait()`` 调用方传入的 ``stop_event``，从不
        ``.set()`` 它。转发任务只在这次尝试的生命周期内有意义，不管正常
        收口还是任何异常路径退出都必须被收回，不能悬挂到下一次尝试。
        """
        effective_stop_event = asyncio.Event()
        forward_task = asyncio.create_task(
            _forward_first_signal(
                tuple(event for event in (stop_event, fuse_event) if event is not None),
                effective_stop_event,
            )
        )
        try:
            return await self._execute_single_turn(
                agent_prompt,
                attempt_resume_session_id=attempt_resume_session_id,
                effective_stop_event=effective_stop_event,
                handle_event=handle_event,
                business_phase=business_phase,
                local_interrupt=local_interrupt,
                task_id=task_id,
            )
        finally:
            forward_task.cancel()
            try:
                await forward_task
            except asyncio.CancelledError:
                pass

    async def _run_turn_attempt(
        self,
        *,
        agent_prompt: str,
        attempt_resume_session_id: str | None,
        stop_event: asyncio.Event | None,
        fuse_event: asyncio.Event,
        on_stream_event: Callable[[Mapping[str, Any]], None] | None,
        task_id: str | None,
    ) -> _TurnAttemptResult:
        """跑 ``run_turn`` 循环里的一次尝试：重置回合级状态、执行、叠加分类。

        产生的降级信号（resume 失配、MCP 502）落在 ``self`` 的既有状态位
        上，由调用方（``run_turn``）据此决定是否在同一次调用内发起重试。
        """
        recorder, business_phase, local_interrupt = self._reset_attempt_state(fuse_event)

        def handle_event(event: Mapping[str, Any]) -> None:
            recorder.handle(event)
            if self._raw_capture is not None:
                self._raw_capture.on_stream_event(event)
            if on_stream_event is not None:
                on_stream_event(event)

        failure = await self._execute_attempt_with_forwarding(
            agent_prompt=agent_prompt,
            attempt_resume_session_id=attempt_resume_session_id,
            stop_event=stop_event,
            fuse_event=fuse_event,
            handle_event=handle_event,
            business_phase=business_phase,
            local_interrupt=local_interrupt,
            task_id=task_id,
        )
        failure = self._finalize_attempt_failure(
            failure,
            recorder=recorder,
            local_interrupt=local_interrupt,
            fuse_event=fuse_event,
            task_id=task_id,
        )
        return _TurnAttemptResult(
            recorder=recorder,
            failure=failure,
            business_duration_seconds=business_phase.get("seconds"),
        )

    def _finish_turn_report(
        self,
        *,
        question: str,
        recorder: TurnStreamRecorder,
        failure: dict[str, str] | None,
        duration_seconds: float,
        business_duration_seconds: float | None,
        normalized_external_texts: Iterable[tuple[str, str]],
    ) -> dict[str, Any]:
        """算收尾耗时、按需注入输出安全 canary、拼出最终的回合报告。"""
        drain_duration_seconds = (
            max(0.0, duration_seconds - business_duration_seconds)
            if business_duration_seconds is not None
            else None
        )

        final_text = recorder.final_text
        if self._config.output_safety_canary is not None and failure is None:
            # 默认关闭；开启时 __post_init__ 已保证 system_prompt 非空。只对
            # 没有失败的回合注入——真实失败必须保留原样的失败终态，注入会
            # 让 withheld 覆盖真实失败原因，验收拿到的就是假证据。
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

        # 仅当任务成功（failure is None）且模型本轮确实调用过该工具时才非空
        # ——真实失败回合即使工具调用发生在失败之前也不承诺这份请求会被
        # 消费；两个槽位互斥，不会同时非 None（见各自赋值处的说明）。
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
            document_request=self._document_request if failure is None else None,
            sheet_request=self._sheet_request if failure is None else None,
        )

    def _register_wrapper_fuse_listener(
        self, fuse_event: asyncio.Event, task_id: str | None
    ) -> None:
        """挂载包装拒绝熔断监听：阈值达到时置位 ``fuse_event`` 并留痕。

        ToolGateway 只知道"阈值达到了"，不知道 SDK 会话的存在；这个回调把
        纯逻辑层通知转译成"这一次尝试该被中断"，跨越每次尝试保持不变，只在
        循环外挂载一次。
        """

        def _on_wrapper_fuse_tripped(denied_count: int) -> None:
            fuse_event.set()
            self._emit_stderr_record(
                level="warning",
                event="worker.wrapper_denial_fuse_tripped",
                denied_count=denied_count,
                threshold=WRAPPER_DENIAL_FUSE_THRESHOLD,
                task_id=task_id,
            )

        self._gateway.set_wrapper_fuse_listener(_on_wrapper_fuse_tripped)

    def _should_retry_after_resume_miss(
        self,
        *,
        resume_fallback_applied: bool,
        attempt_resume_session_id: str | None,
        failure: dict[str, str] | None,
    ) -> bool:
        """这次失败是否该在同一次 ``run_turn()`` 调用内自动降级重试一次。

        降级只发生一次（哨兵防止把真故障重试掩盖）：容器重建导致的会话
        文件丢失是"这一次尝试"的确定性故障，退回无 resume 的全新会话后
        同样的问题不会再复现；真正无法恢复的 ``session_failed``（网络、
        鉴权等）不匹配特征串，不会命中这条判据。
        """
        return (
            not resume_fallback_applied
            and attempt_resume_session_id is not None
            and failure is not None
            and failure.get("code") == "session_failed"
            and self._resume_miss_detected
        )

    async def _run_turn_with_retries(
        self,
        *,
        agent_prompt: str,
        resume_session_id: str | None,
        stop_event: asyncio.Event | None,
        fuse_event: asyncio.Event,
        on_stream_event: Callable[[Mapping[str, Any]], None] | None,
        task_id: str | None,
    ) -> _TurnAttemptResult:
        """跑尝试直到不需要 resume 降级重试，返回最终采纳的那次尝试结果。"""
        attempt_resume_session_id = resume_session_id
        resume_fallback_applied = False
        while True:
            attempt = await self._run_turn_attempt(
                agent_prompt=agent_prompt,
                attempt_resume_session_id=attempt_resume_session_id,
                stop_event=stop_event,
                fuse_event=fuse_event,
                on_stream_event=on_stream_event,
                task_id=task_id,
            )
            if self._should_retry_after_resume_miss(
                resume_fallback_applied=resume_fallback_applied,
                attempt_resume_session_id=attempt_resume_session_id,
                failure=attempt.failure,
            ):
                self._emit_stderr_record(
                    level="warning", event="worker.session_resume_miss", task_id=task_id
                )
                resume_fallback_applied = True
                attempt_resume_session_id = None
                continue
            return attempt

    async def run_turn(
        self,
        question: str,
        *,
        resume_session_id: str | None = None,
        stop_event: asyncio.Event | None = None,
        on_stream_event: Callable[[Mapping[str, Any]], None] | None = None,
        on_tool_call: Callable[[str], None] | None = None,
        external_texts: Iterable[tuple[str, object]] | Mapping[str, object] | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """执行一个回合，**总是**返回一份报告。

        会话抛错也要出报告：一个只在日志里留下堆栈的失败回合，对上层来说与
        "什么都没发生"无法区分。撞上会话 resume 失配特征时的自动降级重试见
        :meth:`_run_turn_with_retries`。``on_tool_call`` 每次 PreToolUse
        判定之后调用一次；``self._gateway`` 是会话级对象，只在这里、每次
        调用开始时重新挂载，因为这个回调闭包了调用方为**这一次任务**维护
        的进度状态。
        """
        self._gateway.set_tool_call_listener(on_tool_call)
        normalized_external_texts = normalize_external_texts(external_texts)
        agent_prompt = compose_agent_prompt(question, normalized_external_texts)
        started_at = self._clock()

        fuse_event = asyncio.Event()
        self._register_wrapper_fuse_listener(fuse_event, task_id)

        attempt = await self._run_turn_with_retries(
            agent_prompt=agent_prompt,
            resume_session_id=resume_session_id,
            stop_event=stop_event,
            fuse_event=fuse_event,
            on_stream_event=on_stream_event,
            task_id=task_id,
        )

        duration_seconds = max(0.0, self._clock() - started_at)
        return self._finish_turn_report(
            question=question,
            recorder=attempt.recorder,
            failure=attempt.failure,
            duration_seconds=duration_seconds,
            business_duration_seconds=attempt.business_duration_seconds,
            normalized_external_texts=normalized_external_texts,
        )

    def build_content_capture_record(
        self, *, task_id: str, worker_id: str, question: str
    ) -> ContentCaptureRecord | None:
        """内测轮内容级采集的落库记录，只在 ``capture_raw_content=True`` 时非空。

        必须在 :meth:`run_turn` 返回**之后**调用——``self._audit.summary()``
        读的是本回合累积到此刻的记账，与 :meth:`run_turn` 内部传给
        ``build_report`` 的是同一份纯投影。调用方拿到非 None 结果后仍需
        自行按开关决定是否真的写库，这个方法只负责"有没有可采集的内容"。
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
    # 失败签名只生成固定类别摘要，与 message 是两件不同的东西——message 停在
    # 这份报告里，signature 是唯一会进入 queue 链路低敏审计日志与 task 落库
    # 列的那一段，因此不能携带动态类型或任何异常正文。
    return failure_with_signature(code, text, error)


def _failure_message(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": redact_free_text(message)[:_MAX_FAILURE_TEXT]}


def _mcp_bad_gateway_failure() -> dict[str, str]:
    """构造一次来自 MCP 状态探针的 502 失败，不携带外部错误正文。"""
    return {
        "code": MCP_BAD_GATEWAY_FAILURE_CODE,
        "message": "指标服务网关返回 HTTP 502，未取得可用结果",
        "signature": MCP_BAD_GATEWAY_FAILURE_SIGNATURE,
    }


def _sdk_termination_failure(
    recorder: TurnStreamRecorder, *, interrupt_requested: bool = False
) -> dict[str, str] | None:
    """把 SDK 的终止元数据收口成产品可区分的护栏原因码。

    ``interrupt_requested`` 是**本进程已经向 SDK 发出** ``interrupt()`` 这一
    本地事实，只影响 ``aborted_streaming``/``aborted_tools`` 这一档的归类：
    已发出中断 → ``interrupted``（这次 abort 就是本地 ``/stop`` 造成的）；
    未发出中断 → ``cancelled``（``aborted_*`` 是 SDK 自报的，无人 stop 时也
    会出现，**不得**用队列侧"有人请求过停止"反推因果，那会让一次晚到的
    stop 掩盖真实的 SDK 终止失败）。
    """
    if recorder.terminal_reason in {"max_turns"} or recorder.result_subtype == "error_max_turns":
        return _failure_message(
            "max_turns_exceeded", "任务提前结束：达到 Agent 轮数上限，结果可能不完整"
        )
    if recorder.terminal_reason in {"aborted_streaming", "aborted_tools"}:
        if interrupt_requested:
            return _failure_message("interrupted", "任务已按停止请求中断，未继续执行")
        return _failure_message("cancelled", "任务已取消，未继续执行")
    return None
