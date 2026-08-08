"""外部文本的角色边界与模型输出的最后一道约束。

这不是通用内容审核器，也不是把模型行为假设成可靠的 prompt firewall。它只做两件
确定性的事：把外部系统返回的自由文本包成不可伪造的「待分析内容」数据段，并在
worker 输出离开进程前移除已知的敏感值、内部工具标识和系统提示。工具是否能执行
仍由 ``ToolGateway`` 判定；这里不复制工具白名单或权限规则。

模块保持在 ``core``，不读环境、不访问网络或文件，便于用固定夹具证明负向边界。
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TypeAlias


EXTERNAL_TEXT_LABEL = "待分析内容"
SAFE_OUTPUT_FALLBACK = "本次未取得可确认结果，请稍后重试。"

ExternalTextItems: TypeAlias = Mapping[str, object] | Iterable[tuple[str, object]]

_SOURCE_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_PROCESS_MARKERS = re.compile(
    r"(?:\bmcp__[A-Za-z0-9_.-]+\b|\b(?:pretooluse|posttooluse|posttoolfailure)\b|"
    r"\b(?:tool_use_id|trace_id)\s*=?)",
    re.IGNORECASE,
)
_SYSTEM_PROMPT_MARKERS = re.compile(r"\b(?:system[_ -]?prompt|system prompt)\b", re.IGNORECASE)


class InputSafetyError(ValueError):
    """外部文本封装请求不满足可判定的结构约束。"""


@dataclass(frozen=True)
class OutputConstraintResult:
    """输出约束的结果；只保留原因码，不把被拦截的原文带到出口。"""

    text: str
    blocked: bool
    reasons: tuple[str, ...]


def normalize_external_texts(values: ExternalTextItems | None) -> tuple[tuple[str, str], ...]:
    """规范化外部文本并固定遍历顺序。

    MCP / 花名册字段的来源名是受控的调用方元数据，文本本身完全不可信。映射输入
    按来源名排序，避免调用方把 ``set`` / ``dict`` 的遍历顺序带进 prompt 或证据。
    """

    if values is None:
        return ()
    if isinstance(values, Mapping):
        items = tuple(values.items())
    else:
        items = tuple(values)

    normalized: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, tuple) or len(item) != 2:
            raise InputSafetyError("外部文本必须是 source 与 text 的二元组")
        source, text = item
        if not isinstance(source, str) or _SOURCE_NAME.fullmatch(source) is None:
            raise InputSafetyError("外部文本来源名不合法，不能进入上下文")
        normalized.append((source, text if isinstance(text, str) else str(text)))
    return tuple(sorted(normalized, key=lambda pair: pair[0]))


def wrap_external_text(source: str, text: object) -> str:
    """把一段外部自由文本标为数据，而不是系统指令。

    载荷中的 XML / 方括号边界都经过实体编码：恶意文本即使包含完整的开闭标签或
    ``[/待分析内容]``，也只能成为数据字符，不能闭合本侧边界。这里保留可读正文，
    不把外部输入丢弃或当成权限授予。
    """

    normalized = normalize_external_texts(((source, text),))
    safe_source, safe_text = normalized[0]
    escaped_text = _escape_external_payload(safe_text)
    return (
        f'<lingxi-external-content role="data" source="{safe_source}">\n'
        f"[{EXTERNAL_TEXT_LABEL}]\n"
        f"{escaped_text}\n"
        f"[/{EXTERNAL_TEXT_LABEL}]\n"
        "</lingxi-external-content>"
    )


def render_external_context(values: ExternalTextItems | None) -> str:
    """渲染一组外部文本；来源顺序固定，内容边界逐段独立。"""

    return "\n\n".join(wrap_external_text(source, text) for source, text in normalize_external_texts(values))


def compose_agent_prompt(question: str, external_texts: ExternalTextItems | None = None) -> str:
    """为受控测试 / 编排调用构造带数据边界的 Agent 输入。"""

    if not isinstance(question, str):
        raise InputSafetyError("Agent 问题必须是字符串")
    context = render_external_context(external_texts)
    return question if not context else f"{question}\n\n{context}"


def constrain_output(
    text: object,
    *,
    forbidden_values: Iterable[object] = (),
    internal_tool_names: Iterable[object] = (),
    system_prompt: str | None = None,
    fallback_text: str = SAFE_OUTPUT_FALLBACK,
) -> OutputConstraintResult:
    """约束模型最终正文，使已知内部 / 敏感内容不能离开 worker。

    调用方把固定假凭据、其他用户标识、外部注入原文和系统提示作为
    ``forbidden_values`` 传入；内部工具名单独列出，便于报告同时覆盖放行与拒绝的
    调用。替换按长度倒序执行，避免短值先替换后留下更长敏感值的尾部。
    """

    candidate = text if isinstance(text, str) else ""
    reasons: list[str] = []
    values = _unique_texts((*forbidden_values, system_prompt))
    tool_names = _unique_texts(internal_tool_names)

    for value in sorted(values, key=lambda item: (-len(item), item)):
        if value in candidate:
            candidate = candidate.replace(value, "【已隐藏】")
            _append_reason(reasons, "forbidden_value")

    for tool_name in sorted(tool_names, key=lambda item: (-len(item), item)):
        if tool_name in candidate:
            candidate = candidate.replace(tool_name, "【内部能力已隐藏】")
            _append_reason(reasons, "internal_tool_name")

    candidate, marker_changed = _PROCESS_MARKERS.subn("【内部标识已隐藏】", candidate)
    if marker_changed:
        _append_reason(reasons, "process_marker")
    candidate, prompt_marker_changed = _SYSTEM_PROMPT_MARKERS.subn("【内部提示已隐藏】", candidate)
    if prompt_marker_changed:
        _append_reason(reasons, "system_prompt_marker")

    if reasons or not candidate.strip():
        if not isinstance(fallback_text, str) or not fallback_text.strip():
            raise InputSafetyError("输出约束的安全终态不能为空")
        if any(value in fallback_text for value in (*values, *tool_names)):
            raise InputSafetyError("输出约束的安全终态包含被禁止的原文")
        candidate = fallback_text
        if not isinstance(text, str) or not text.strip():
            _append_reason(reasons, "empty_output")

    return OutputConstraintResult(text=candidate, blocked=bool(reasons), reasons=tuple(reasons))


def _escape_external_payload(text: str) -> str:
    escaped = html.escape(text, quote=True)
    # ``html.escape`` 不处理方括号；编码它们，避免载荷伪造本侧可读边界标记。
    return escaped.replace("[", "&#91;").replace("]", "&#93;")


def _unique_texts(values: Iterable[object]) -> tuple[str, ...]:
    unique: set[str] = set()
    for value in values:
        if isinstance(value, str) and value:
            unique.add(value)
    return tuple(unique)


def _append_reason(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)
