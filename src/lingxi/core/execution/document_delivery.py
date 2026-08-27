"""文档交付请求的纯逻辑校验（Issue #341 S-ES-2：触发机制 worker 侧）。

这个模块只做三件确定性的事，不发任何外部请求、不碰文件系统、不 import Claude
Agent SDK：把模型传入的 markdown 做轻量归一化、核对硬上限、复用出口安全约束对
标题与正文做与终态文本同等的泄露检查。是否挂载这个能力（开关、MCP 服务装配、
ToolPolicy 白名单合入）是 ``apps/worker/turn.py`` 的装配职责，本模块不知道
「开关」这个概念——它只回答"这一次登记请求本身合不合法"。

``DELIVER_DOCUMENT_TOOL_NAME``/``DELIVERY_MCP_SERVER_NAME`` 定义在这个零依赖的
``core`` 模块里，而不是各自在 ``apps/worker/turn.py`` 与
``core/execution/hooks.py`` 里各写一份字面量——两处都要认得同一个工具名（前者
用于装配 SDK MCP 服务与 ToolPolicy 白名单，后者用于 ``_is_side_effecting_tool``
的侧效判定），字符串一旦分叉，后果与 ``core/mcp_naming.py`` 文档记录的
Issue #291 根因 #1 同一形状：两侧各自维护一份、悄悄不一致，判定在某一层悄悄
失效却没有任何信号。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .input_safety import constrain_output

#: SDK MCP 服务名——``create_sdk_mcp_server(name=DELIVERY_MCP_SERVER_NAME, ...)``
#: 与 ``mcp_servers`` 装配字典的 key 必须用同一个值，这是 Claude Agent SDK
#: ``mcp__<服务名>__<工具名>`` 命名约定的前提（``core/mcp_naming.py`` 里
#: ``QUERY_MCP_SERVER_NAME`` 之于 ``mcp__query__*`` 是同一形状的先例）。
DELIVERY_MCP_SERVER_NAME = "delivery"
DELIVER_DOCUMENT_TOOL_NAME = f"mcp__{DELIVERY_MCP_SERVER_NAME}__deliver_document"

#: 硬上限：越过它一律 ``DocumentDeliveryError`` 拒绝，不静默截断（产品负责人
#: 审定设计第 2 条）。
MAX_PARAGRAPHS = 80
MAX_TOTAL_CHARS = 20000
MIN_TITLE_CHARS = 1
MAX_TITLE_CHARS = 100

#: 轻量归一化会剥离的 Markdown 语法字符——只做字面字符剔除，不是通用 Markdown
#: 解析器；代价（例如正文里的连字符 "3-5%" 会被剥成 "35%"）是这份「触发机制」
#: 卡片明确接受的简化，真正的排版仍由 S-ES-3 消费段落时决定。
_MARKDOWN_SYNTAX_CHARS = "#*`-[]()"
_BLANK_LINE_SPLIT = re.compile(r"\n\s*\n")


class DocumentDeliveryError(ValueError):
    """一次文档交付登记请求不满足硬性约束。

    ``reason_code`` 是机器可读的拒绝原因，供调用方记审计
    ``worker.document_request_rejected``；不进模型上下文、不进用户可见文案。
    """

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class DocumentRequest:
    """一次登记成功的文档交付请求——报告契约 ``document_request`` 字段的来源。"""

    title: str
    paragraphs: tuple[str, ...]

    @property
    def total_chars(self) -> int:
        return sum(len(paragraph) for paragraph in self.paragraphs)


def _strip_markdown_syntax(text: str) -> str:
    for character in _MARKDOWN_SYNTAX_CHARS:
        text = text.replace(character, "")
    return text


def normalize_markdown(markdown: str) -> tuple[str, ...]:
    """轻量归一化：剥离常见 Markdown 语法字符，按空行切分为段落。

    段内换行折叠为空格——这里产出的是"段落列表"，不是要保留原始换行版式的
    文本块；空段（连续空行、纯语法字符行剥完后变空）不进入结果。
    """

    stripped = _strip_markdown_syntax(markdown)
    paragraphs: list[str] = []
    for block in _BLANK_LINE_SPLIT.split(stripped):
        collapsed = " ".join(line.strip() for line in block.splitlines()).strip()
        if collapsed:
            paragraphs.append(collapsed)
    return tuple(paragraphs)


def build_document_request(
    *,
    title: object,
    markdown: object,
    forbidden_values: Iterable[object] = (),
    internal_tool_names: Iterable[object] = (),
    system_prompt: str | None = None,
) -> DocumentRequest:
    """校验并构造一次文档交付请求；任何一条硬性约束不满足都抛
    :class:`DocumentDeliveryError`，绝不静默降级或截断。

    ``forbidden_values``/``internal_tool_names``/``system_prompt`` 与
    ``apps/worker/report.py::build_report`` 喂给终态正文 ``constrain_output`` 的
    是同一组值（由调用方对齐传入）——文档标题与正文离开进程前必须经过与终态
    正文同等的出口安全检查，不能因为走的是另一个工具就少一道检测。
    """

    if not isinstance(title, str) or not (MIN_TITLE_CHARS <= len(title.strip()) <= MAX_TITLE_CHARS):
        raise DocumentDeliveryError(
            "invalid_title",
            f"标题必须是 {MIN_TITLE_CHARS} 到 {MAX_TITLE_CHARS} 字符之间的字符串",
        )
    if not isinstance(markdown, str) or not markdown.strip():
        raise DocumentDeliveryError("empty_markdown", "正文不能为空")

    paragraphs = normalize_markdown(markdown)
    if not paragraphs:
        raise DocumentDeliveryError("empty_markdown", "正文归一化后没有任何可用段落")
    if len(paragraphs) > MAX_PARAGRAPHS:
        raise DocumentDeliveryError(
            "too_many_paragraphs", f"段落数 {len(paragraphs)} 超过上限 {MAX_PARAGRAPHS}"
        )
    total_chars = sum(len(paragraph) for paragraph in paragraphs)
    if total_chars > MAX_TOTAL_CHARS:
        raise DocumentDeliveryError(
            "too_many_chars", f"正文总长度 {total_chars} 超过上限 {MAX_TOTAL_CHARS}"
        )

    normalized_title = title.strip()
    combined_text = "\n".join((normalized_title, *paragraphs))
    output_safety = constrain_output(
        combined_text,
        forbidden_values=forbidden_values,
        internal_tool_names=internal_tool_names,
        system_prompt=system_prompt,
    )
    if output_safety.blocked:
        raise DocumentDeliveryError(
            "leak_detected", "标题或正文命中输出安全检查，登记被拒绝"
        )

    return DocumentRequest(title=normalized_title, paragraphs=paragraphs)
