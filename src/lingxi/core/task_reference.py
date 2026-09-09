"""任务故障的安全号码；只从精确关联和持久任务主键派生。"""

from __future__ import annotations

from dataclasses import dataclass

from lingxi.config.content import ContentCatalog, ContentRenderError, RenderedContent
from lingxi.core.ids import is_ulid

FAILURE_REFERENCE_KEYS = frozenset(
    f"worker.{name}"
    for name in (
        "failed",
        "running_timeout",
        "queued_timeout",
        "version_unavailable",
        "mcp_bad_gateway",
        "side_effect_uncertain",
        "result_too_large",
        "max_turns",
        "redacted_withheld",
    )
)


@dataclass(frozen=True)
class TaskReference:
    """经过封闭形状解析的号码与类别。"""

    reference: str
    reference_kind: str


def valid_trace_id(value: object) -> str | None:
    """非法源值直接丢弃，错误和日志均不得包含原值。"""
    return value if isinstance(value, str) and is_ulid(value) else None


def parse_reference(value: object) -> TaskReference | None:
    """两种封闭形状；不修剪或猜测其它标识。"""
    if is_ulid(value):
        return TaskReference(value, "event")
    if isinstance(value, str) and value.startswith("T-") and is_ulid(value[2:]):
        return TaskReference(value, "task")
    return None


def task_reference(task_id: object, trace_id: object = None) -> TaskReference | None:
    """损坏任务主键不造号；调用方仍可给出原有诚实失败提示。"""
    source = valid_trace_id(trace_id)
    if source is not None:
        return TaskReference(source, "event")
    if isinstance(task_id, str) and task_id.startswith("tsk_") and is_ulid(task_id[4:]):
        return TaskReference("T-" + task_id[4:], "task")
    return None


def reference_fields(task_id: str, trace_id: object = None) -> dict[str, object]:
    """每回合复制一份安全日志字段，不保存跨任务可变状态。"""
    ref = task_reference(task_id, trace_id)
    return {
        "task_id": task_id if task_reference(task_id) is not None else None,
        "trace_id": valid_trace_id(trace_id),
        "reference": ref.reference if ref else None,
        "reference_kind": ref.reference_kind if ref else None,
        **({"reference_integrity_error": True} if task_reference(task_id) is None else {}),
    }


def append_failure_reference(
    catalog: ContentCatalog,
    content: RenderedContent,
    *,
    task_id: object = None,
    trace_id: object = None,
) -> RenderedContent:
    """九类系统失败追加固定目录内容；无任务的独立回合保持原文。"""
    if content.key not in FAILURE_REFERENCE_KEYS or task_id is None:
        return content
    ref = task_reference(task_id, trace_id)
    if ref is None:
        return content
    suffix = render_reference(catalog, ref)
    return RenderedContent(content.key, catalog.version, content.text + "\n" + suffix.text)


def render_reference(catalog: ContentCatalog, ref: TaskReference) -> RenderedContent:
    """渲染前核对类型和值，不能以自由文本占位绕过号码约束。"""
    parsed = parse_reference(ref.reference)
    if parsed is None or parsed.reference_kind != ref.reference_kind:
        raise ContentRenderError("核查号码类型或格式不合法")
    key = (
        "worker.failure_reference"
        if ref.reference_kind == "event"
        else "worker.failure_task_reference"
    )
    return catalog.text(key, reference=ref.reference)
