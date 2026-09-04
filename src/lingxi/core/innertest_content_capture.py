"""内测轮内容级采集：原始素材收集、凭据形状过滤与落库记录构造。

## 为什么是一个独立模块，不是扩展 ``core/execution/audit.py``

``core/execution/audit.py`` 的 ``TurnAudit``/``AuditRedactor`` 是执行层安全边界的
一部分：按**字段白名单**裁剪工具入参、只留字节数与哈希，目的是"审计明细本身不能
成为第二个泄露业务内容的地方"。本模块要做的是**相反方向**的事——按产品要求
"内容级采集全量开启，用户问题/模型回答/工具调用详情，原文与结果正文不设限"，即
这里要保留执行层审计刻意不保留的东西。把两件目的相反的事塞进同一个模块，迟早会
有一次改动为了满足采集侧的"多留一点"而悄悄放宽了审计侧的"少留一点"，因此是两个
独立通道：只复用凭据形状过滤这一项**唯一必须保留**的约束（结构性"凭据类一律排除"
不因口径全量开启而放松），除此之外不做任何字段白名单裁剪或业务语义脱敏；只在开关
开启时才会被构造，默认关闭状态下这个模块的对象不会被实例化。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from lingxi.core.execution.audit import TurnAuditSummary, redact_free_text_with_count

# "结果摘要"按字面是摘要，不是全文；只有用户问题与模型回答按产品要求"原文/正文
# 不设限"。超过这个字节数的工具结果按 UTF-8 边界截断，并在
# CapturedToolCall.result_summary 里显式标注 truncated=True——不静默丢字节，也
# 不假装截断后的内容是完整的。
MAX_TOOL_RESULT_SUMMARY_BYTES = 4000

# 递归脱敏的深度上限：模型可控的工具入参嵌套深度没有上界，
# core/execution/audit.py 的 _dump 对同一类风险靠捕获 RecursionError 兜底，这里
# 用显式深度上限提前避免，不依赖异常回退。
_MAX_JSON_REDACTION_DEPTH = 20


@dataclass(frozen=True)
class CapturedToolCall:
    """一次工具调用在内容采集里的投影，与执行层审计的 ``ToolCallAudit`` 不是同一份数据。

    ``tool_input`` 是原始参数（只过凭据形状），不是字段白名单裁剪后的版本。
    """

    tool_use_id: str | None
    tool_name: str
    tool_input: Mapping[str, Any]
    result_summary: Mapping[str, Any]
    redaction_count: int


@dataclass(frozen=True)
class ContentCaptureRecord:
    """一次回合的完整采集记录，是交给 adapters 落库前的最终形态。"""

    task_id: str
    worker_id: str
    question_content: str
    question_redaction_count: int
    answer_content: str
    answer_redaction_count: int
    tool_calls: tuple[CapturedToolCall, ...]

    @property
    def tool_calls_redaction_count(self) -> int:
        """全部工具调用的脱敏命中次数之和。"""
        return sum(call.redaction_count for call in self.tool_calls)

    def tool_calls_payload(self) -> list[dict[str, Any]]:
        """给适配器写 JSONB 列用的纯 JSON 安全结构。"""
        return [
            {
                "tool_use_id": call.tool_use_id,
                "tool_name": call.tool_name,
                "tool_input": call.tool_input,
                "result_summary": call.result_summary,
                "redaction_count": call.redaction_count,
            }
            for call in self.tool_calls
        ]


class RawTurnCapture:
    """回合内原始素材的纯内存收集器。

    只在开关开启时才会被构造（见 ``apps/worker/turn.py``）：关闭状态下这是"默认
    关闭可被断言证明"的一部分——不是多一个 if 分支跳过写库，而是这个对象压根
    没被实例化。两个注入点对接两条不同的原始数据来源：:meth:`on_pre_tool_use`
    接 hook 判定时的原始入参（字段白名单裁剪之前）；:meth:`on_stream_event` 接
    消息流（最终正文与工具回执原文，与执行层消息流读的是同一个规范化事件，只是
    另开一条独立内存收集，互不影响）。
    """

    def __init__(self) -> None:
        """建立空的原始入参/回执内存收集状态。"""
        self._raw_tool_input: dict[str, Any] = {}
        self._raw_tool_result: dict[str, tuple[Any, Any]] = {}
        self._final_text = ""

    def on_pre_tool_use(self, tool_use_id: str | None, tool_input: Any) -> None:
        """``ToolGateway`` 的 ``raw_pre_tool_use`` 回调，PreToolUse 时原样递入。

        没有 ``tool_use_id`` 的调用不记录——与 ``TurnAuditSummary`` 一致，无法
        可靠相关联的调用宁可在采集里显式"未捕获"，也不猜测按名字回退（同名并发
        调用会挂错，见 ``core/execution/audit.py`` 的 ``_locate`` 文档）。
        """
        if tool_use_id:
            self._raw_tool_input[tool_use_id] = tool_input

    def on_stream_event(self, event: Mapping[str, Any]) -> None:
        """消息流回调，事件 ``kind`` 取值域与 ``TurnStreamRecorder.handle`` 相同。

        取的是回合内原始正文，不是出口投影 ``report["turn"]["final_text"]``——那份
        投影已经过出口安全约束（防系统提示词/外部文本泄露）与凭据过滤两道处理，是
        "允许离开进程"的版本；内容级采集这条全新、默认关闭、只在 stage 内测轮的
        独立通道要保留"模型真实说了什么"，不经过出口安全约束，只在写入采集表之前
        过一道凭据形状过滤——这样即使模型输出触发了出口安全遮蔽，采集里仍能看到
        原始内容，这正是"以日志分析缺陷"要看的那类信号；日志侧"不含业务正文"的
        既有纪律原样不动。
        """
        kind = event.get("kind")
        if kind == "assistant_message":
            # 后一条覆盖前一条，与 TurnStreamRecorder 同一语义：最终正文是最后
            # 一条助手消息，覆盖包括覆盖成空串。
            text = event.get("text")
            self._final_text = text if isinstance(text, str) else ""
        elif kind == "tool_result":
            tool_use_id = event.get("tool_use_id")
            if isinstance(tool_use_id, str) and tool_use_id:
                self._raw_tool_result[tool_use_id] = (event.get("content"), event.get("is_error"))

    @property
    def final_text(self) -> str:
        """本回合目前观察到的最终正文原文（未经出口安全约束、未经凭据过滤）。"""
        return self._final_text

    def build_record(
        self,
        *,
        task_id: str,
        worker_id: str,
        question: str,
        summary: TurnAuditSummary,
    ) -> ContentCaptureRecord:
        """结合执行层已经算好的回合审计摘要与本收集器持有的原始素材，构造采集记录。

        不自己重新判断哪次结果对应哪次调用——``TurnAuditSummary`` 已经解决过这个
        相关性问题（含并发同名调用不允许模糊回退等边界），本收集器只在旁路收集
        每个 ``tool_use_id`` 对应的原始入参/回执内容，真正的调用—结果配对完全交给
        ``summary.calls``，这里只是把结构与内容合并成一条记录。
        """
        redacted_question, question_count = redact_free_text_with_count(question)
        redacted_answer, answer_count = redact_free_text_with_count(self._final_text)
        calls = tuple(self._build_call(call) for call in summary.calls)
        return ContentCaptureRecord(
            task_id=task_id,
            worker_id=worker_id,
            question_content=redacted_question,
            question_redaction_count=question_count,
            answer_content=redacted_answer,
            answer_redaction_count=answer_count,
            tool_calls=calls,
        )

    def _build_call(self, call: Any) -> CapturedToolCall:
        tool_use_id = call.tool_use_id
        if tool_use_id and tool_use_id in self._raw_tool_input:
            redacted_input, input_count = _redact_json(self._raw_tool_input[tool_use_id])
            if not isinstance(redacted_input, Mapping):
                # tool_input 正常形态恒为 Mapping；防御性兜底一个畸形值时仍保持
                # 外层类型是 Mapping，不让调用方为一个理论上不该发生的形状分叉。
                redacted_input = {"value": redacted_input}
        else:
            # 没有捕获到（本层 PreToolUse 从未触发的旁路调用、或审计记账失败的
            # 兜底记录）：显式标「未捕获」，不留空字典冒充"这次调用没有参数"。
            redacted_input, input_count = {"captured": False}, 0

        raw_result, is_error = self._raw_tool_result.get(tool_use_id or "", (None, None))
        result_text, result_count = redact_free_text_with_count(_stringify(raw_result))
        truncated_text, was_truncated = _truncate(result_text)
        result_summary = {
            "result_kind": call.result_kind.value if call.result_kind else None,
            "allowed": call.allowed,
            "executed": call.executed,
            "is_error": bool(is_error) if is_error is not None else None,
            "content": truncated_text,
            "truncated": was_truncated,
        }
        return CapturedToolCall(
            tool_use_id=tool_use_id,
            tool_name=call.tool_name,
            tool_input=redacted_input,
            result_summary=result_summary,
            redaction_count=input_count + result_count,
        )


def _redact_json(value: Any, *, _depth: int = 0) -> tuple[Any, int]:
    """递归对 JSON 安全结构里的字符串叶子做凭据形状过滤，返回 (结构, 命中次数)。

    与执行层审计的过滤逐字节同源，因此同样的已知局限在这里原样成立：纯字母且
    短于 32 字符的裸秘密、被模型当成合法形态工具名发出的凭据不会被过滤掉——
    这是产品已知情接受的既有边界，内容级采集复用同一套规则，不重新承诺更强的
    保证；``redaction_count`` 只是"命中并替换了几处"的可观测计数，不是"零命中
    即无凭据"的证明。
    """
    if _depth >= _MAX_JSON_REDACTION_DEPTH:
        text, count = redact_free_text_with_count(_stringify(value))
        return {"depth_truncated": True, "value": text}, count
    if value is None or isinstance(value, (bool, int, float)):
        return value, 0
    if isinstance(value, str):
        return redact_free_text_with_count(value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        count = 0
        for key, item in value.items():
            redacted_key, key_count = redact_free_text_with_count(str(key))
            redacted_item, item_count = _redact_json(item, _depth=_depth + 1)
            result[redacted_key] = redacted_item
            count += key_count + item_count
        return result, count
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items: list[Any] = []
        count = 0
        for item in value:
            redacted_item, item_count = _redact_json(item, _depth=_depth + 1)
            items.append(redacted_item)
            count += item_count
        return items, count
    text, count = redact_free_text_with_count(_stringify(value))
    return text, count


def _stringify(value: Any) -> str:
    """把工具回执原文（Mapping / 内容块序列 / 字符串 / None）统一成纯文本。

    形状与 ``core/execution/audit.py`` 的 ``_coerce`` 相似，但**不复用**它：那个
    函数的目的是"判定回执分类"，这个函数的目的是"得到一段可读文本存进采集表"，
    两者分叉是有意的（判定失败不能影响能否落一段可读文本）。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return _safe_json_dumps(value)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        parts: list[str] = []
        for block in value:
            if isinstance(block, Mapping):
                text = block.get("text")
                parts.append(text if isinstance(text, str) else _safe_json_dumps(block))
            elif isinstance(block, str):
                parts.append(block)
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(value)


def _safe_json_dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError, RecursionError):
        try:
            return str(value)
        except Exception:  # 采集是旁路，取值失败不得向上传播中断任务
            return "<unrepresentable>"


def _truncate(text: str) -> tuple[str, bool]:
    raw = text.encode("utf-8")
    if len(raw) <= MAX_TOOL_RESULT_SUMMARY_BYTES:
        return text, False
    return raw[:MAX_TOOL_RESULT_SUMMARY_BYTES].decode("utf-8", errors="ignore"), True
