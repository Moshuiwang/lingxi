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
import re
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

from .config import OUTPUT_SAFETY_CANARY_SURVIVOR_BODY, WorkerConfig
from .report import build_report

_MAX_FAILURE_TEXT = 500

# MCP 会话级连接失败审计（2026-08-28，Issue #349 剩余范围，Gate G-2 结论 A）：
# 只有这两种状态是明确的故障态，值得响亮告警——`pending`（G-2 已知边界：慢连接
# 时建连返回时可能还没到终态，不是失败）、`disabled`（用户/部署主动关闭，不是
# 故障）、`connected`（正常）都不触发。判定只在这一层，`adapters/claude_agent_
# session.py` 只转发 SDK 原始 dict，不含任何判定逻辑（该模块既有约定）。
_MCP_UNAVAILABLE_STATUSES = frozenset({"failed", "needs-auth"})

# 会话 resume 失配的特征串（2026-08-27 生产事故 #2）：worker 容器 HOME=/tmp，
# CLI 会话文件随容器重建消失，但 conversation.agent_session_id 仍留在库里——
# 部署后每个活跃会话的下一条消息都会尝试 resume 一个已经不存在的会话。真实 CLI
# 子进程对此的失败形状是**自己的 stderr** 里一行 "No conversation found with
# session ID: <uuid>" 后非零退出；Python SDK 侧捕获到的 `ProcessError` **不携带**
# 这行原文（`claude_agent_sdk` 0.2.128 的 `_read_messages_impl` 只写死
# "Check stderr output for details"，读源码实测确认），因此这个特征串只能从
# `_sdk_stderr_sink` 已经落过的 `worker.sdk.stderr` 那份文本里认；`run_turn` 里
# 额外兜底检查抛出的会话异常对象本身，两处任一命中即可，防的是"某个未来 SDK 版本
# 把这行原文塞进了异常文本"这种此刻验证不到、但不该让降级路径悄悄失效的情形。
# 大小写不敏感、只认这一段固定英文前缀，不含冒号后的会话 id 本身——`redact_free_
# text` 的长串脱敏规则会先把 32 字符以上的会话 id 盖成 `[REDACTED:N字符]`，固定
# 前缀不受影响；也不会把其他 session_failed（连接被拒、传输中断等）误判成这一种
# 可自动恢复的失配。
_SESSION_RESUME_MISS_PATTERN = re.compile(
    r"no conversation found with session id", re.IGNORECASE
)


def _looks_like_session_resume_miss(text: str) -> bool:
    """这段文本是否带着「resume 的会话已经不存在」的特征。"""

    return bool(_SESSION_RESUME_MISS_PATTERN.search(text))


async def _forward_first_signal(sources: tuple[asyncio.Event, ...], target: asyncio.Event) -> None:
    """任一 ``sources`` 先被 set，就把 ``target`` 一起 set（Issue #352）。

    包装拒绝熔断需要在**不改动** ``run_single_turn`` 现有 "/stop → interrupt()"
    协议的前提下，让"本轮熔断触发"也能走同一条已经过验证的中断路径——那条路径
    比新开一条平行的终止通道风险更低（同样的会话 interrupt 机制已经支撑
    ``/stop`` 生产运行）。这个协程只做转发，不判断"为什么"要中断，也不修改任何
    调用方持有的事件对象（只 ``.wait()``，从不 ``.set()`` 调用方传入的
    ``stop_event``）；事后谁触发的、要不要改写失败原因，由 ``run_turn`` 按
    ``fuse_event.is_set()`` 另行判断。
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
            # 表格分支（Issue #354 S-H3-2）：同一个开关、同一个 MCP 服务，新增
            # 一个并列工具——与 DELIVER_DOCUMENT_TOOL_NAME 同时并入白名单，不是
            # 单独一个开关。见 core/execution/document_delivery.py 模块文档
            # 「表格分支」一节。
            policy_tools = tuple(policy_tools) + (
                DELIVER_DOCUMENT_TOOL_NAME,
                DELIVER_SPREADSHEET_TOOL_NAME,
            )
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
        # 否则第二回合会带着上一回合遗留的请求。**保持这个属性名/类型/既有语义
        # 逐字不变**（Issue #354 S-H3-2 边界：不改 docx 既有行为）——既有测试
        # 直接读 ``executor._document_request``，改名会连坐一批与表格分支无关
        # 的既有断言。
        self._document_request: DocumentRequest | None = None
        # 表格分支（Issue #354 S-H3-2）：与上面并列的独立槽位，不是同一个槽位的
        # 改名——两个工具各自维护自己的槽位，跨类型的"最后一次调用为准"通过
        # "登记自己时清空对方"实现（见 _handle_deliver_document/_handle_deliver_
        # spreadsheet），不需要合并成一个联合类型槽位。
        self._sheet_request: SheetRequest | None = None
        # 会话 resume 失配自动降级（2026-08-27 生产事故 #2）：每次 `run_turn()`
        # 的每次尝试开始时重置为 False；`_sdk_stderr_sink` 命中特征串时置位，
        # `run_turn` 据此决定要不要对这次尝试的 `session_failed` 做一次降级重试。
        # 这里的初始化只是防御性的——真正生效的重置在 `run_turn` 每轮尝试开头。
        self._resume_miss_detected = False

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
        """装配进程内 SDK MCP 服务（Issue #341 S-ES-2 审定设计第 1 条；
        Issue #354 S-H3-2 新增 ``deliver_spreadsheet`` 并列工具）。

        处理函数**不发任何外部请求**：只做校验（``build_document_request``/
        ``build_sheet_request``）并把请求登记为进程内"本轮交付请求"，返回明确的
        成功/失败文案给模型。第三方 SDK 的 import 延迟到这里——仓库约定
        ``src/lingxi/`` 不做模块级第三方 import（见 ``pyproject.toml`` 顶部
        说明），只有真正开启这个开关的进程才会付出这次 import 成本。
        """

        from claude_agent_sdk import create_sdk_mcp_server, tool as sdk_tool

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
                # P1-1（opus 审查）：`config.external_texts` 是
                # `tuple[tuple[str, str], ...]`（`(键, 文本)` 对，见
                # `apps/worker/config.py::_external_texts`），不是文本本身。此前
                # 原样传入 `constrain_output` 会把每个二元组整体当成一条禁止值——
                # `_unique_texts` 只会拿到形如 `('key', 'text')` 的对象，与真实
                # 正文逐字比对必然不命中，等于这条出口安全检查对文档交付**整串
                # 失效**。与 `run_turn` 里 `build_report(external_texts=tuple(text
                # for _, text in normalized_external_texts), ...)` 用同一种拆法，
                # 只取文本值。
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
        # 跨类型互斥（Issue #354 S-H3-2）：登记文档请求时清空表格槽位——"同一
        # 回合内多次调用以最后一次为准"扩展到跨类型同样成立，两个槽位不会同时
        # 非空。docx-only 场景（sheet 槽位从未被写过）这里恒是 no-op，行为不变。
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
        return {
            "content": [
                # opus 审查 R-1 第 4 条：与工具描述、与 `content.toml` 新增的用户可见
                # 失败文案（`delivery.document_failed`）同向措辞——不再无条件承诺
                # "会生成"，只承诺"已登记；失败会有通知"，模型据此措辞回复用户时
                # 不会说出系统兑现不了的承诺。
                {"type": "text", "text": "已登记文档请求；若生成失败你会收到通知。"}
            ]
        }

    def _handle_deliver_spreadsheet(self, title: Any, rows: Any) -> dict[str, Any]:
        """``deliver_spreadsheet`` 工具调用的唯一处理逻辑（同步、无外部请求）。

        与 :meth:`_handle_deliver_document` 逐项对称（Issue #354 S-H3-2），差异
        除校验函数（``build_sheet_request``）与登记的审计事件字段（``row_count``
        而不是 ``paragraph_count``）外，拒绝事件名也不同：本分支记
        ``worker.sheet_request_rejected``，不复用文档分支的 ``worker.document_
        request_rejected``（Trace #373 H3 批量审查 P2-8）——两个事件名各自独立，
        运维按事件名过滤时才能区分"这是表格请求被拒还是文档请求被拒"，不需要再
        去解析 payload 里的其它字段才能分清来源。
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
        # 跨类型互斥（Issue #354 S-H3-2）：同上，登记表格请求时清空文档槽位。
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
        return {
            "content": [
                {"type": "text", "text": "已登记表格请求；若生成失败你会收到通知。"}
            ]
        }

    def _sdk_stderr_sink(self, line: object) -> None:
        """SDK 子进程 stderr 的唯一落点：脱敏、截断、结构化，带 trace_id。

        不设回调时子进程直接继承 fd 2，启动失败的原始错误（可能含令牌）会绕过
        全部出口纪律（Codex 复查发现）。

        会话 resume 失配自动降级（2026-08-27 生产事故 #2）的唯一可靠信号来源：
        真实 CLI 子进程把 "No conversation found with session ID: ..." 写在
        **自己的 stderr**，Python SDK 侧的 `ProcessError` 不携带这行原文。这里在
        脱敏截断之后匹配特征串并置位 `self._resume_miss_detected`，供 `run_turn`
        决定是否降级重试；不改变这个回调本身落 `worker.sdk.stderr` 日志的行为。
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
        """把建连后的 MCP server 状态转成响亮的审计事件（Issue #349，Gate G-2 结论 A）。

        ``status`` 是适配器原样转发的 ``client.get_mcp_status()`` 返回值（SDK
        形状：``{"mcpServers": [{"name": ..., "status": ..., "error": ...}, ...]}``），
        形状不对时如实什么都不做，不猜测——与 ``apps/worker/service.py`` 里
        ``_protocol_breakdown_reasons`` 那类"形状不对就返回空、不当成命中"的
        写法同一纪律。

        只对 ``_MCP_UNAVAILABLE_STATUSES``（``failed``/``needs-auth``）逐个 server
        发一条独立的 ``worker.mcp_server_unavailable`` 审计事件（warning 级）——
        这就是本仓库 worker 侧"计入告警面"的既有姿势：worker 进程结构上从不持有
        飞书出站凭据，唯一的信号出口是带 ``trace_id`` 的结构化 stderr（见
        ``apps/worker/cli.py`` 的 ``_LogOnlyAlertSender``/``_year_grounding_
        suspect_sink`` 同一姿态的完整说明），与本文件里 ``worker.wrapper_denial_
        fuse_tripped``（#352）、``worker.session_resume_miss``（生产事故 #2）走的
        是同一条通道，不新开一条；调查确认 worker 侧没有到 `core/alerting.py`
        `AlertManager` 的现成 `AlertKind`（那套只覆盖进程心跳/任务滞留，且发送
        端一样落到这条结构化 stderr——新增 `AlertKind` 不会多打通到真实管理群
        的一条通路，只会多一层间接），也没有到每日通报统计的现成聚合口径
        （`core/daily_report.py` 只读 `task` 表两个专用列，不扫 stderr 事件）；
        因此不新造聚合通道，直接复用现成的结构化 stderr 出口。

        字段只含 server 名、status、error 文本三项——不包含 ``config``/``tools``/
        ``serverInfo``（SDK 原始形状里这些字段可能带 URL、鉴权配置等凭据形状
        内容），符合代码框架「三、横切约定」的凭据不进日志底线。``error`` 文本
        经 ``redact_free_text`` 脱敏、按其余失败文本同一上限截断——它来自外部
        CLI/MCP 服务，不是本进程自己构造的可信文本。
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

        会话抛错也要出报告：一个只在日志里留下堆栈的失败回合，对上层来说与"什么都
        没发生"无法区分，而这两者的处置完全不同。

        ``on_tool_call``（Issue #321 方向 C，语义化等待进度）：每次 ``PreToolUse``
        判定之后调用一次，传入判定后的规范化工具名——见
        ``ToolGateway.set_tool_call_listener`` 的完整语义。``self._gateway`` 是
        会话级对象（构造时机见 ``__init__``），只在这里、每次 ``run_turn()`` 调用
        开始时重新挂载，理由是这个回调闭包了调用方（``apps/worker/service.py``）
        为**这一次任务**维护的进度状态，不能提前固定在构造期。

        会话 resume 失配自动降级（2026-08-27 生产事故 #2）：`resume_session_id`
        非空的这次尝试如果撞上「会话不存在」特征（见 `_looks_like_session_resume_
        miss`），在**同一次 `run_turn()` 调用内**自动退回无 resume 的全新会话重试
        **恰好一次**——旧会话在库里记的仍是同一个 `agent_session_id`，重试成功后
        新 id 会随成功报告正常落库（既有机制），下一条消息据此续聊，用户不再重复
        撞见硬错。非失配特征的 `session_failed`、或重试后仍然失败，都保持原有失败
        路径不变，不会被这次降级掩盖。`task_id` 只用于给 `worker.session_resume_
        miss` 审计事件带上任务标识（不落库、不参与判定），queue 模式之外的调用方
        （一次性 turn CLI、既有测试）留空即可。
        """

        self._gateway.set_tool_call_listener(on_tool_call)
        normalized_external_texts = normalize_external_texts(external_texts)
        agent_prompt = compose_agent_prompt(question, normalized_external_texts)
        started_at = self._clock()

        attempt_resume_session_id = resume_session_id
        resume_fallback_applied = False

        # 包装拒绝熔断（Issue #352）：`fuse_event` 是本方法内长期持有的信号——
        # `ToolGateway` 只知道"阈值达到了"，不知道 SDK 会话的存在；这里登记的
        # 回调把那个纯逻辑层通知转译成"这一次尝试该被中断"。回调本身跨越每次
        # 尝试保持不变（都只是 set 同一个、每次尝试开头 `.clear()` 的事件），因此
        # 与 `on_tool_call` 一样只在循环外挂载一次即可，不需要每次尝试重新注册。
        fuse_event = asyncio.Event()

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

        while True:
            self._audit.start_turn()
            # 包装拒绝熔断（Issue #352）：与审计同一时机清零——`ToolGateway` 的
            # 拒绝计数和 `fuse_event` 都是"同一回合内"的窗口状态，不得带着上一次
            # 尝试（resume 降级重试）的痕迹进入这一轮判断。
            self._gateway.reset_wrapper_denial_fuse()
            fuse_event.clear()
            # 文档/表格交付触发机制（Issue #341 S-ES-2；Issue #354 S-H3-2）：
            # 回合级状态，与审计一起在**每次尝试**开头清零（resume 降级重试的
            # 第二次尝试同样不得带着第一次尝试登记的请求，报告契约="仅当模型
            # 本轮调用过对应工具时非空"）。
            self._document_request = None
            self._sheet_request = None
            recorder = TurnStreamRecorder(self._audit)
            failure: dict[str, str] | None = None
            # #143：业务耗时由适配器在业务阶段结束时回填一次；收尾耗时事后用
            # "总耗时 - 业务耗时" 推得。适配器没有机会调用回调时（例如构造选项就
            # 失败）保持未知，不能构造数据。
            business_phase: dict[str, float] = {}
            # #201：本进程是否**真的**向 SDK 发出过 `interrupt()`。这是本地事实，不是
            # 对队列侧 stop 标志的推断——只有它成立，才允许把 SDK 自报的 `aborted_*`
            # 收尾判成中断（见 `_sdk_termination_failure`）。
            local_interrupt: dict[str, bool] = {}
            # 每次尝试开头重置（2026-08-27 生产事故 #2）：`_sdk_stderr_sink` 命中
            # 特征串时置位，只反映**这一次**尝试观测到的信号，不带着上一次失败
            # 尝试的痕迹进入这一轮的降级判定。
            self._resume_miss_detected = False

            def handle_event(event: Mapping[str, Any]) -> None:
                recorder.handle(event)
                if self._raw_capture is not None:
                    self._raw_capture.on_stream_event(event)
                if on_stream_event is not None:
                    on_stream_event(event)

            # 包装拒绝熔断（Issue #352）：`effective_stop_event` 是 `run_single_turn`
            # 真正会 `.wait()` 的对象——它在"外部 /stop"与"本轮熔断"任一触发时都会
            # 被置位，但两者的调用方语义完全独立：这里只 `.wait()` 调用方传入的
            # `stop_event`，从不 `.set()` 它，因此不会污染调用方（`apps/worker/
            # service.py`）自己对那个事件对象的其它用途。`stop_event` 为
            # `None`（一次性 turn CLI 等既有调用方）时只由 `fuse_event` 驱动。
            effective_stop_event = asyncio.Event()
            forward_task = asyncio.create_task(
                _forward_first_signal(
                    tuple(event for event in (stop_event, fuse_event) if event is not None),
                    effective_stop_event,
                )
            )
            try:
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
                            resume_session_id=attempt_resume_session_id,
                            stop_event=effective_stop_event,
                            drain_grace_seconds=self._config.drain_grace_seconds,
                            clock=self._clock,
                            on_business_duration=lambda seconds: business_phase.__setitem__("seconds", seconds),
                            on_interrupt_requested=lambda: local_interrupt.__setitem__("requested", True),
                            on_mcp_status=lambda status: self._audit_mcp_status(status, task_id=task_id),
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
                        # 兜底信号源（2026-08-27 生产事故 #2）：正常情况下特征串已经
                        # 由 `_sdk_stderr_sink` 从子进程 stderr 里认出来了；这里再看一遍
                        # 异常对象本身，防的是"某个未来 SDK 版本把这行原文塞进了异常
                        # 文本"这种此刻验证不到、但不该让降级路径悄悄失效的情形。
                        if _looks_like_session_resume_miss(str(error)):
                            self._resume_miss_detected = True
            finally:
                # 包装拒绝熔断（Issue #352）：不管这次尝试是正常收口、被拒绝拦截、
                # 还是任何一种异常路径退出，转发任务都必须被收回——它只在这次尝试
                # 的生命周期内有意义，不能悬挂到下一次尝试或调用方的事件循环里。
                # 与 `run_single_turn` 内部 `monitor.cancel()` 同一姿态。
                forward_task.cancel()
                try:
                    await forward_task
                except asyncio.CancelledError:
                    pass

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

            if fuse_event.is_set():
                # 包装拒绝熔断（Issue #352）：不管这次尝试最终被 SDK 收口成
                # `AgentSessionInterrupted`、`aborted_streaming`/`aborted_tools`，
                # 还是（竞态下）干脆正常收尾，只要是本轮熔断发出的中断请求，产品
                # 语义都不是"用户主动 /stop"（`interrupted`）也不是"外部取消"
                # （`cancelled`）——而是"模型反复越界被拒、系统主动提前收口"。这里
                # 无条件覆盖上面算出的 `failure`，复用现有的 `max_turns_exceeded`
                # 失败路径（`apps/worker/service.py::_failure_content` 现成分支，
                # 映射到 `worker.max_turns` 文案「本次查询步骤较多，达到单次处理
                # 轮数上限，未能完成」）——不新造用户文案，也不让 interrupted/
                # cancelled 掩盖真实原因。`_GUARD_FAILURE_CODES`
                # （`apps/worker/report.py`）已经收了这个 code，`guard_triggered`/
                # `termination_state="guarded"` 不需要额外改动就正确投影。
                failure = _failure_message(
                    "max_turns_exceeded",
                    "任务提前终止：同一回合内包装拒绝达到熔断阈值，结果可能不完整（Issue #352）",
                )

            if (
                not resume_fallback_applied
                and attempt_resume_session_id is not None
                and failure is not None
                and failure.get("code") == "session_failed"
                and self._resume_miss_detected
            ):
                # 降级只发生一次（`resume_fallback_applied` 是本次 `run_turn()`
                # 调用内的哨兵，防止把真故障重试掩盖）：容器重建导致的会话文件
                # 丢失是「这一次尝试」的确定性故障，退回无 resume 的全新会话后
                # 同样的问题不会再复现；真正无法恢复的 session_failed（网络、
                # 鉴权等）不匹配特征串，不会走到这里，仍按原有失败路径收口。
                self._emit_stderr_record(
                    level="warning",
                    event="worker.session_resume_miss",
                    task_id=task_id,
                )
                resume_fallback_applied = True
                attempt_resume_session_id = None
                continue

            break

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

        # 仅当任务成功（本地 ``failure is None``，与上面 output_safety_canary
        # 注入判断同一个信号）且模型本轮确实调用过该工具时才非空——真实失败
        # 回合即使工具调用发生在失败之前也不承诺这份请求会被消费。document_
        # request 这一行与改动前逐字相同（Issue #354 S-H3-2 边界：不改 docx
        # 既有行为）；sheet_request 是并列新增的同构参数，两个槽位互斥见
        # self._document_request/self._sheet_request 赋值处的说明，因此两个
        # 参数不会同时非 None。
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
