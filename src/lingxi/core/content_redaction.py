"""内容留存共用的凭据形状过滤：递归脱敏 JSON 结构、工具回执转文本、结果摘要截断。

内测轮内容级采集与问答留存语料是两条独立通道（开关、表、生命周期都不同），但「凭据形状
一律排除」是两者唯一共同且必须一致的约束——同一份判据只能有一处实现，复制一份迟早
漂移。这里只放纯函数，不持有任何通道的状态；判据本身来自执行层审计的
``redact_free_text_with_count``，因此已知局限原样成立：纯字母且短于 32 字符的裸秘密
不会被过滤，命中计数是「替换了几处」的可观测值，不是「零命中即无凭据」的证明。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from lingxi.core.execution.audit import redact_free_text_with_count

# "结果摘要"按字面是摘要，不是全文；只有用户问题与模型回答按产品要求"原文/正文
# 不设限"。超过这个字节数的工具结果按 UTF-8 边界截断，并由调用方显式标注
# truncated=True——不静默丢字节，也不假装截断后的内容是完整的。
MAX_TOOL_RESULT_SUMMARY_BYTES = 4000

# 递归脱敏的深度上限：模型可控的工具入参嵌套深度没有上界，
# core/execution/audit.py 的 _dump 对同一类风险靠捕获 RecursionError 兜底，这里
# 用显式深度上限提前避免，不依赖异常回退。
_MAX_JSON_REDACTION_DEPTH = 20


def redact_json(value: Any, *, _depth: int = 0) -> tuple[Any, int]:
    """递归对 JSON 安全结构里的字符串叶子做凭据形状过滤，返回 (结构, 命中次数)。

    与执行层审计的过滤逐字节同源，因此同样的已知局限在这里原样成立：纯字母且
    短于 32 字符的裸秘密、被模型当成合法形态工具名发出的凭据不会被过滤掉——
    这是产品已知情接受的既有边界，内容留存复用同一套规则，不重新承诺更强的
    保证。
    """
    if _depth >= _MAX_JSON_REDACTION_DEPTH:
        text, count = redact_free_text_with_count(stringify_tool_result(value))
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
            redacted_item, item_count = redact_json(item, _depth=_depth + 1)
            result[redacted_key] = redacted_item
            count += key_count + item_count
        return result, count
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items: list[Any] = []
        count = 0
        for item in value:
            redacted_item, item_count = redact_json(item, _depth=_depth + 1)
            items.append(redacted_item)
            count += item_count
        return items, count
    text, count = redact_free_text_with_count(stringify_tool_result(value))
    return text, count


def stringify_tool_result(value: Any) -> str:
    """把工具回执原文（Mapping / 内容块序列 / 字符串 / None）统一成纯文本。

    形状与 ``core/execution/audit.py`` 的 ``_coerce`` 相似，但**不复用**它：那个
    函数的目的是"判定回执分类"，这个函数的目的是"得到一段可读文本存进留存表"，
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
        except Exception:  # 留存是旁路，取值失败不得向上传播中断任务
            return "<unrepresentable>"


def truncate_summary(text: str) -> tuple[str, bool]:
    """按 UTF-8 边界把结果摘要截到字节上限，返回 (文本, 是否截断)。"""
    raw = text.encode("utf-8")
    if len(raw) <= MAX_TOOL_RESULT_SUMMARY_BYTES:
        return text, False
    return raw[:MAX_TOOL_RESULT_SUMMARY_BYTES].decode("utf-8", errors="ignore"), True
