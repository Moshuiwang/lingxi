"""把 Agent SDK 消息流里的事实汇入**同一个**回合审计。

Issue #29 的结论是审计链必须自己合成，来源有三个：hook 判定、hook 回调、以及
**消息流**。前两个由 :class:`~lingxi.core.execution.hooks.ToolGateway` 接住，第三个
在这里——被 MCP 包成 ``isError=false`` 的业务失败不触发任何 hook，只能从工具回执
里读出来；最终正文同样只在消息流里。

**只接判定、不接消息流的执行器会系统性地谎报**：没有回执就没有 ``result_kind``，
回合结论会停在"未知"或更糟——把一次什么都没查到的回合说成已送达。因此 worker 必须
同时接上两条来源，本类是消息流那一条的唯一入口。

这里刻意不 import SDK：消息已由 ``lingxi.adapters.claude_agent_session`` 规范化成
普通字典，因此"消息流有没有汇入审计"可以在没有 SDK 的 CI 里被完整覆盖。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .audit import TurnAudit

# 规范化事件的取值全集。适配器只允许产出这三种，多一种就说明两层的约定漂了。
STREAM_EVENT_KINDS: tuple[str, ...] = ("assistant_message", "tool_result", "result")
_USAGE_TOKEN_FIELDS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


class TurnStreamRecorder:
    """一个回合的消息流记账。

    终止结果计数**不在这里**：它由 ``Stop`` hook 经 :class:`ToolGateway` 记入审计。
    如果消息流里的 ``ResultMessage`` 也算一次，``terminal_result_count`` 必然是 2，
    "恰好一次终止结果"（V-执行-03）就永远不成立。两个来源分别计数、分别上报，
    由调用方判断是否一致——合并计数只会让不一致隐身。
    """

    def __init__(self, audit: TurnAudit) -> None:
        self._audit = audit
        self._result_message_count = 0
        self._result_is_error: bool | None = None
        self._result_subtype: str | None = None
        self._terminal_reason: str | None = None
        self._tool_result_count = 0
        self._final_text = ""
        self._usage_summary: dict[str, Any] = {
            "status": "unknown",
            "source": "sdk",
            "reason": "no_result_message",
        }
        self._agent_turns: int | None = None
        self._result_error: str | None = None
        self._session_id: str | None = None

    @property
    def final_text(self) -> str:
        """本回合的最终正文原文。

        审计只保留字节数（正文是模型可控文本，不该原样落审计），但执行器要把它交给
        调用方，因此这里留一份。出口脱敏由 ``apps`` 层的报告投影负责。
        """

        return self._final_text

    @property
    def result_message_count(self) -> int:
        """消息流里出现了几次 ``ResultMessage``（与审计的终止结果计数各记各的）。"""

        return self._result_message_count

    @property
    def result_is_error(self) -> bool | None:
        return self._result_is_error

    @property
    def result_subtype(self) -> str | None:
        """SDK 终止消息的子类型（如 ``success`` / ``error_max_turns``）。

        只保存受控投影：非字符串一律记 ``None``，超长截断——它是模型/SDK 侧
        文本，进报告前仍会再过一次出口脱敏。"""
        return self._result_subtype

    @property
    def terminal_reason(self) -> str | None:
        """SDK 回报的终止原因，只保留有限长度的枚举样字符串。"""

        return self._terminal_reason

    @property
    def result_error(self) -> str | None:
        return self._result_error

    @property
    def tool_result_count(self) -> int:
        return self._tool_result_count

    @property
    def usage_summary(self) -> dict[str, Any]:
        """外部 SDK usage 的安全摘要；未知必须显式存在，不能用 0 填洞。"""

        return dict(self._usage_summary)

    @property
    def agent_turns(self) -> int | None:
        """SDK 终止消息提供的实际 Agent 轮数；没有该字段时保持未知。"""

        return self._agent_turns

    @property
    def session_id(self) -> str | None:
        """SDK 终止消息报告的会话标识；没有可靠标识时保持 None。"""

        return self._session_id

    def handle(self, event: Mapping[str, Any]) -> None:
        kind = event.get("kind")
        if kind == "assistant_message":
            # 后一条覆盖前一条：模型常先说"我查一下"再给结论，最终正文是最后那条。
            # 覆盖包括覆盖成空串——适配器只在消息里真的带了文本块时才产出本事件，
            # 因此"最后一条助手消息的正文是空的"是必须如实记录的失败事实。
            text = event.get("text")
            self._final_text = text if isinstance(text, str) else ""
            self._audit.record_final_text(text)
        elif kind == "tool_result":
            self._tool_result_count += 1
            self._audit.record_tool_result(
                tool_use_id=event.get("tool_use_id"),
                content=event.get("content"),
                is_error=event.get("is_error"),
            )
        elif kind == "result":
            self._result_message_count += 1
            self._result_is_error = bool(event.get("is_error"))
            subtype = event.get("subtype")
            self._result_subtype = subtype[:64] if isinstance(subtype, str) else None
            terminal_reason = event.get("terminal_reason")
            self._terminal_reason = terminal_reason[:64] if isinstance(terminal_reason, str) else None
            self._agent_turns = _non_negative_int(event.get("num_turns"))
            self._usage_summary = _usage_summary(
                event.get("usage"),
                source=event.get("usage_source", "sdk"),
            )
            error = event.get("error")
            self._result_error = error[:500] if isinstance(error, str) else None
            session_id = event.get("session_id")
            if isinstance(session_id, str) and session_id:
                self._session_id = session_id


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _usage_summary(value: Any, *, source: Any) -> dict[str, Any]:
    """只保留已知 token 计数字段，拒绝把任意 SDK payload 当成 usage。"""

    safe_source = source if isinstance(source, str) and source in {"sdk", "mock"} else "unknown"
    if not isinstance(value, Mapping):
        return {
            "status": "unknown",
            "source": safe_source,
            "reason": "not_provided",
        }

    fields: dict[str, int] = {}
    for name in _USAGE_TOKEN_FIELDS:
        candidate = value.get(name)
        if _non_negative_int(candidate) is not None:
            fields[name] = candidate
    if not fields:
        return {
            "status": "unknown",
            "source": safe_source,
            "reason": "no_recognized_token_counts",
        }
    return {
        "status": "known",
        "source": safe_source,
        "fields": fields,
    }
