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

from typing import Any

from lingxi.adapters.claude_agent_session import build_agent_options, run_single_turn
from lingxi.core.execution.audit import AuditRedactor, ResultRules, TurnAudit, redact_free_text
from lingxi.core.execution.hooks import ToolGateway
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

    def __init__(self, config: WorkerConfig) -> None:
        self._config = config
        self._policy = ToolPolicy(allowed_tools=(config.read_only_tool,))
        self._audit = TurnAudit(
            rules=ResultRules(failure_text_markers=config.failure_text_markers),
            redactor=AuditRedactor(allowed_input_fields=config.audit_input_fields),
        )
        self._gateway = ToolGateway(policy=self._policy, audit=self._audit)
        self._options: Any = None

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
                mcp_servers=self._config.mcp_servers,
                cwd=self._config.workspace,
                model=self._config.model,
                system_prompt=self._config.system_prompt,
            )
        return self._options

    async def run_turn(
        self,
        question: str,
    ) -> dict[str, Any]:
        """执行一个回合，**总是**返回一份报告。

        会话抛错也要出报告：一个只在日志里留下堆栈的失败回合，对上层来说与"什么都
        没发生"无法区分，而这两者的处置完全不同。
        """

        self._audit.start_turn()
        recorder = TurnStreamRecorder(self._audit)
        failure: dict[str, str] | None = None

        try:
            options = self.build_session_options()
            await run_single_turn(options=options, prompt=question, sink=recorder.handle)
        except ImportError as error:
            failure = _failure("sdk_unavailable", error)
        except Exception as error:  # noqa: BLE001 - 入口必须把任何失败变成一份报告
            failure = _failure("session_failed", error)

        return build_report(
            trace_id=self._config.trace_id,
            question=question,
            allowed_tools=self._policy.allowed_tools,
            summary=self._audit.summary(),
            stream=recorder,
            final_text=recorder.final_text,
            failure=failure,
        )


def _failure(code: str, error: BaseException) -> dict[str, str]:
    # 异常正文可能带上连接串、路径或令牌，按自由文本脱敏后再截断。
    text = redact_free_text(f"{type(error).__name__}: {error}")[:_MAX_FAILURE_TEXT]
    return {"code": code, "message": text}
