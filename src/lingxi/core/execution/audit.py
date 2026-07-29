"""执行层的合成审计链。

Issue #23 的实测结论决定了这个模块的形状：单一 hook 还原不了"这一轮到底发生了
什么"，必须由三个来源合成——

===================  ==========================================================
要审计的事           来源
===================  ==========================================================
工具抛错             ``PostToolUseFailure`` 回调
调用被拒             **执行层自己在 ``PreToolUse`` 记账**；权限拒绝没有任何 hook
                     回调（``PermissionDenied`` / ``PermissionRequest`` 实测均为
                     0，且已确认不是事件名未注册）
用户实际没拿到结果   **解析工具回执内容**；被 MCP 包成 ``isError=false`` 正常响应
                     的业务失败不会触发 ``PostToolUseFailure``
===================  ==========================================================

因此本模块刻意不把"没有报错"等同于"用户拿到了结果"。无法归类的回执记为
:attr:`ToolResultKind.UNCLASSIFIED`，回合结论相应记为
:attr:`UserResultStatus.UNKNOWN`——未知是合法结论，不用推测填满。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from .tool_policy import DenyReasonCode, PolicyVerdict

_SECRET_KEY = re.compile(r"(token|secret|password|authorization|api[_-]?key|credential|auth)", re.IGNORECASE)
_MAX_KEPT_TEXT = 200


class ToolResultKind(str, Enum):
    """一次工具回执的归类。"""

    OK = "ok"
    TOOL_ERROR = "tool_error"
    BUSINESS_FAILURE = "business_failure"
    EMPTY_RESULT = "empty_result"
    UNCLASSIFIED = "unclassified"


class UserResultStatus(str, Enum):
    """这一回合里，用户到底有没有拿到业务结果。"""

    OBTAINED = "obtained"
    NOT_OBTAINED = "not_obtained"
    NO_TOOL_CALL = "no_tool_call"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ResultRules:
    """把工具回执归类为成功还是业务失败的规则。

    规则是**显式配置**的，不做自然语言猜测。``failure_text_markers`` 默认为空：
    外部 MCP 用什么措辞表达"指标不存在"这类业务失败，必须先从真实回执里确认再
    登记，否则宁可记为 :attr:`ToolResultKind.UNCLASSIFIED`。
    """

    failure_keys: frozenset[str] = frozenset({"error", "errors"})
    truthy_success_keys: frozenset[str] = frozenset({"ok", "success"})
    status_keys: frozenset[str] = frozenset({"status", "state"})
    failure_status_values: frozenset[str] = frozenset({"error", "failed", "failure"})
    empty_collection_keys: frozenset[str] = frozenset({"data", "rows", "items", "results"})
    failure_text_markers: tuple[str, ...] = ()


UNGATED_TOOL_NAME = "<未经执行层判定>"


@dataclass(frozen=True)
class ToolCallAudit:
    """一次工具调用在审计里的完整投影。

    ``allowed`` 有三个取值：``True`` 放行、``False`` 拒绝、``None`` **本层未判定**。
    第三种不是理论情况：``disallowed_tools`` 这类规则层拦截发生在 ``PreToolUse``
    之前，执行层根本收不到回调，只能从消息流里的错误回执发现它存在（L4a 实测，
    见 Issue #29）。把它记成"放行"会让审计谎报，记成"拒绝"会谎报是本层拦的。
    """

    tool_use_id: str | None
    tool_name: str
    tool_input: Mapping[str, Any]
    allowed: bool | None
    deny_reason_code: DenyReasonCode | None = None
    deny_reason_text: str | None = None
    executed: bool = False
    error: str | None = None
    result_kind: ToolResultKind | None = None

    @property
    def denied(self) -> bool:
        return self.allowed is False

    @property
    def gated(self) -> bool:
        """这次调用是否经过了执行层的判定。"""

        return self.allowed is not None


@dataclass(frozen=True)
class TurnAuditSummary:
    """一个回合的审计结论。可直接落审计表，也可用于门禁断言。"""

    calls: tuple[ToolCallAudit, ...]
    denied_calls: tuple[ToolCallAudit, ...]
    failed_calls: tuple[ToolCallAudit, ...]
    ungated_calls: tuple[ToolCallAudit, ...]
    user_result: UserResultStatus
    final_text_bytes: int
    terminal_result_count: int
    terminal_ok: bool

    @property
    def denied_tool_names(self) -> tuple[str, ...]:
        return tuple(call.tool_name for call in self.denied_calls)

    @property
    def executed_tool_names(self) -> tuple[str, ...]:
        return tuple(call.tool_name for call in self.calls if call.executed)


class AuditRedactor:
    """按显式字段白名单裁剪工具入参。

    默认不收录任何入参字段：新增字段必须显式加入白名单才会进入审计明细，
    避免将来某个工具多一个字段就把业务内容或凭据静默带进审计。
    """

    def __init__(self, allowed_input_fields: Iterable[str] = ()) -> None:
        self._allowed = frozenset(allowed_input_fields)

    def redact(self, tool_input: Any) -> dict[str, Any]:
        if not isinstance(tool_input, Mapping):
            return {}
        redacted: dict[str, Any] = {}
        for key, value in tool_input.items():
            name = str(key)
            if _SECRET_KEY.search(name):
                redacted[name] = "[REDACTED]"
            elif name not in self._allowed:
                redacted[name] = {"omitted": True}
            else:
                redacted[name] = self._summarize(value)
        return redacted

    @staticmethod
    def _summarize(value: Any) -> Any:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str) and len(value) <= _MAX_KEPT_TEXT:
            return value
        return _digest(value)


class TurnAudit:
    """按回合累积事实的审计记账本。

    调用方（worker）负责把 hook 回调和 SDK 消息流分别喂进来；本类不感知 SDK 类型。
    """

    def __init__(self, *, rules: ResultRules | None = None, redactor: AuditRedactor | None = None) -> None:
        self._rules = rules or ResultRules()
        self._redactor = redactor or AuditRedactor()
        self._calls: list[dict[str, Any]] = []
        self._by_tool_use_id: dict[str, dict[str, Any]] = {}
        self._final_text: str = ""
        self._terminal_result_count: int = 0

    # ---- 来源一 / 来源二：hook 记账 ----

    def record_decision(
        self,
        *,
        tool_name: str,
        tool_input: Any,
        tool_use_id: str | None,
        verdict: PolicyVerdict,
    ) -> None:
        """记录 ``PreToolUse`` 的判定。放行和拒绝都记，拒绝额外带上理由。"""

        record = self._new_record(
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            tool_input=tool_input,
            allowed=not verdict.denied,
        )
        record["deny_reason_code"] = verdict.reason_code
        record["deny_reason_text"] = verdict.model_reason if verdict.denied else None

    def record_executed(self, *, tool_name: str, tool_use_id: str | None) -> None:
        """记录 ``PostToolUse``：工具确实执行完毕（不代表业务上成功）。"""

        record = self._locate(tool_use_id, tool_name)
        if record is not None:
            record["executed"] = True

    def record_failure(
        self,
        *,
        tool_name: str,
        tool_input: Any,
        tool_use_id: str | None,
        error: Any,
    ) -> None:
        """记录 ``PostToolUseFailure``：工具抛错。语义是"工具失败"，不是"用户没拿到结果"。"""

        record = self._locate(tool_use_id, tool_name)
        if record is None:
            record = self._new_record(
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                tool_input=tool_input,
                allowed=None,
            )
        record["error"] = _error_text(error)
        record["result_kind"] = ToolResultKind.TOOL_ERROR

    # ---- 来源三：解析工具回执 ----

    def record_tool_result(self, *, tool_use_id: str | None, content: Any, is_error: Any = None) -> ToolResultKind:
        """解析 SDK 消息流里的工具回执，判定用户这一次到底拿到了什么。

        找不到对应 ``PreToolUse`` 记录的失败回执**不丢弃**，而是记成一条"本层未判定"
        的调用。否则规则层（``disallowed_tools``）拦下的调用会在审计里彻底消失——
        L4a 实测中就出现过这一幕。
        """

        kind = classify_tool_result(content, is_error=is_error, rules=self._rules)
        record = self._locate(tool_use_id, None)
        if record is None:
            if kind is ToolResultKind.OK:
                return kind
            record = self._new_record(
                tool_use_id=tool_use_id,
                tool_name=UNGATED_TOOL_NAME,
                tool_input=None,
                allowed=None,
            )
            record["error"] = _error_text(_coerce(content)[0])
        if record["result_kind"] is not ToolResultKind.TOOL_ERROR:
            record["result_kind"] = kind
        return kind

    # ---- 回合收口 ----

    def record_final_text(self, text: Any) -> None:
        self._final_text = text if isinstance(text, str) else ""

    def record_terminal_result(self) -> None:
        self._terminal_result_count += 1

    def summary(self) -> TurnAuditSummary:
        calls = tuple(ToolCallAudit(**record) for record in self._calls)
        denied = tuple(call for call in calls if call.denied)
        failed = tuple(
            call
            for call in calls
            if call.result_kind in {ToolResultKind.TOOL_ERROR, ToolResultKind.BUSINESS_FAILURE}
        )
        return TurnAuditSummary(
            calls=calls,
            denied_calls=denied,
            failed_calls=failed,
            ungated_calls=tuple(call for call in calls if not call.gated),
            user_result=self._user_result(calls),
            final_text_bytes=len(self._final_text.encode("utf-8")),
            terminal_result_count=self._terminal_result_count,
            terminal_ok=bool(self._final_text.strip()) and self._terminal_result_count == 1,
        )

    @staticmethod
    def _user_result(calls: Sequence[ToolCallAudit]) -> UserResultStatus:
        """从各次调用的归类合成回合结论；证据不足时返回 UNKNOWN 而不是猜测。"""

        if not calls:
            return UserResultStatus.NO_TOOL_CALL
        # 被本层拒绝的调用不参与"拿没拿到结果"的判断（它压根没执行）；
        # 本层未判定但确实失败了的调用要参与，否则规则层拦截会让结论偏乐观。
        kinds = [call.result_kind for call in calls if not call.denied]
        if any(kind is ToolResultKind.OK for kind in kinds):
            return UserResultStatus.OBTAINED
        if any(kind is None or kind is ToolResultKind.UNCLASSIFIED for kind in kinds):
            return UserResultStatus.UNKNOWN
        # 剩下的情况：放行的调用全部是抛错、业务失败或空结果；或者全部调用都被拒。
        return UserResultStatus.NOT_OBTAINED

    def _new_record(
        self,
        *,
        tool_use_id: str | None,
        tool_name: str,
        tool_input: Any,
        allowed: bool | None,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "tool_use_id": tool_use_id,
            "tool_name": tool_name,
            "tool_input": self._redactor.redact(tool_input),
            "allowed": allowed,
            "deny_reason_code": None,
            "deny_reason_text": None,
            "executed": False,
            "error": None,
            "result_kind": None,
        }
        self._calls.append(record)
        if tool_use_id:
            self._by_tool_use_id[tool_use_id] = record
        return record

    def _locate(self, tool_use_id: str | None, tool_name: str | None) -> dict[str, Any] | None:
        if tool_use_id and tool_use_id in self._by_tool_use_id:
            return self._by_tool_use_id[tool_use_id]
        if tool_name is None:
            return None
        for record in reversed(self._calls):
            if record["tool_name"] == tool_name and record["allowed"] is True:
                return record
        return None


def classify_tool_result(content: Any, *, is_error: Any = None, rules: ResultRules | None = None) -> ToolResultKind:
    """把一次工具回执归类。

    ``is_error`` 为真时直接是工具错误；否则按结构化规则判断业务失败与空结果。
    既不匹配失败规则、又拿不出可识别数据的回执归为
    :attr:`ToolResultKind.UNCLASSIFIED`，由调用方决定如何处理。
    """

    rules = rules or ResultRules()
    if bool(is_error):
        return ToolResultKind.TOOL_ERROR

    text, parsed = _coerce(content)
    if parsed is not None:
        structured = _classify_structured(parsed, rules)
        if structured is not None:
            return structured
    for marker in rules.failure_text_markers:
        if marker and marker in text:
            return ToolResultKind.BUSINESS_FAILURE
    if parsed is not None:
        return ToolResultKind.OK
    if text.strip():
        return ToolResultKind.UNCLASSIFIED
    return ToolResultKind.EMPTY_RESULT


def _classify_structured(parsed: Any, rules: ResultRules) -> ToolResultKind | None:
    if not isinstance(parsed, Mapping):
        if isinstance(parsed, Sequence) and not isinstance(parsed, (str, bytes)):
            return ToolResultKind.EMPTY_RESULT if len(parsed) == 0 else ToolResultKind.OK
        return None
    for key in rules.failure_keys:
        if key in parsed and parsed[key]:
            return ToolResultKind.BUSINESS_FAILURE
    for key in rules.truthy_success_keys:
        if key in parsed and parsed[key] is False:
            return ToolResultKind.BUSINESS_FAILURE
    for key in rules.status_keys:
        value = parsed.get(key)
        if isinstance(value, str) and value.lower() in rules.failure_status_values:
            return ToolResultKind.BUSINESS_FAILURE
    for key in rules.empty_collection_keys:
        value = parsed.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return ToolResultKind.EMPTY_RESULT if len(value) == 0 else ToolResultKind.OK
    return None


def _coerce(content: Any) -> tuple[str, Any]:
    """把 SDK 的回执内容统一成 (纯文本, 解析出的 JSON 或 None)。"""

    if isinstance(content, Mapping):
        return _dump(content), content
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, Mapping):
                value = block.get("text")
                parts.append(value if isinstance(value, str) else _dump(block))
            elif isinstance(block, str):
                parts.append(block)
        text = "\n".join(parts)
        return text, _try_json(text)
    if isinstance(content, str):
        return content, _try_json(content)
    if content is None:
        return "", None
    return str(content), None


def _try_json(text: str) -> Any:
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        return json.loads(stripped)
    except (ValueError, TypeError):
        return None


def _dump(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _digest(value: Any) -> dict[str, Any]:
    text = value if isinstance(value, str) else _dump(value)
    raw = text.encode("utf-8")
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _error_text(error: Any) -> str:
    if isinstance(error, str):
        return error[:2000]
    return _dump(error)[:2000]
