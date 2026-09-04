"""文档交付请求的纯逻辑校验：触发机制在 worker 侧。

这个模块只做三件确定性的事，不发任何外部请求、不碰文件系统、不 import Claude
Agent SDK：把模型传入的 markdown 做轻量归一化、核对硬上限、复用出口安全约束对
标题与正文做与终态文本同等的泄露检查。是否挂载这个能力是装配职责，本模块
不知道「开关」这个概念，只回答"这一次登记请求本身合不合法"。工具名常量定义
在这个零依赖模块里而不是各调用方各写一份，避免悄悄分叉、判定悄悄失效。

``DELIVER_SPREADSHEET_TOOL_NAME`` 与 :func:`build_sheet_request`/
:class:`SheetRequest`（「表格分支」）是同一机制新增分支，不是另开触发通道：
同一个 MCP 服务、同一个装配开关、同一套出口安全检查、同一个回合级请求槽位。
表格与文档唯一的差别是内容形状——文档是段落文本，表格是行×列的单元格二维
数组——因此新增一对结构对称的 ``Sheet*`` 名字，校验步骤与文档分支逐项对应。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from .input_safety import constrain_output

#: SDK MCP 服务名——``create_sdk_mcp_server(name=DELIVERY_MCP_SERVER_NAME, ...)``
#: 与 ``mcp_servers`` 装配字典的 key 必须用同一个值，这是 Claude Agent SDK
#: ``mcp__<服务名>__<工具名>`` 命名约定的前提（``core/mcp_naming.py`` 里
#: ``QUERY_MCP_SERVER_NAME`` 之于 ``mcp__query__*`` 是同一形状的先例）。
DELIVERY_MCP_SERVER_NAME = "delivery"
DELIVER_DOCUMENT_TOOL_NAME = f"mcp__{DELIVERY_MCP_SERVER_NAME}__deliver_document"
#: 表格分支的工具名：同一个 MCP 服务名，新增一个并列工具——见模块文档
#: 「表格分支」一节，不是另开服务。
DELIVER_SPREADSHEET_TOOL_NAME = f"mcp__{DELIVERY_MCP_SERVER_NAME}__deliver_spreadsheet"

#: 硬上限：越过它一律 ``DocumentDeliveryError`` 拒绝，不静默截断。
MAX_PARAGRAPHS = 80
MAX_TOTAL_CHARS = 20000
MIN_TITLE_CHARS = 1
MAX_TITLE_CHARS = 100

#: 原始 markdown 全文长度硬上限，独立于归一化后的 ``total_chars``——纯空白/
#: 纯空行的"段"会被归一化丢弃，只查 ``total_chars`` 挡不住"原始 markdown 塞
#: 满空白、归一化后很小"这种绕过（``DocumentRequest.markdown`` 逐字存原始
#: 值，必须单独设一道原始长度上限）。取 ``MAX_TOTAL_CHARS`` 的 2 倍：两者
#: 取同一值会让 ``too_many_chars`` 分支几乎总被这道更早的检查先拦截。
MAX_RAW_MARKDOWN_CHARS = MAX_TOTAL_CHARS * 2

#: 表格分支的硬上限：与 ``MAX_PARAGRAPHS``/``MAX_TOTAL_CHARS`` 同一取舍量级，
#: 独立取值——表格是行×列的单元格集合，不是段落文本，不共用同一个常量（共用
#: 会让两条互不相关的产品规则改一个就影响另一个）。80 行、每行至多 40 列：
#: 覆盖问数场景常见的汇总表规模，越过一律拒绝，不静默截断/裁剪。
MAX_SHEET_ROWS = 80
MAX_SHEET_COLUMNS = 40
MAX_SHEET_TOTAL_CHARS = 20000
MIN_SHEET_TITLE_CHARS = MIN_TITLE_CHARS
MAX_SHEET_TITLE_CHARS = MAX_TITLE_CHARS

#: 段落切分用的空行分隔符。这里只按空行切段、段内折叠，不做任何字符级改写
#: ——曾经逐字符剔除 ``#*`-[]()`` 八个符号，但连字符也在剔除清单里，会把
#: 「周环比 -12.85%」剥成「周环比 12.85%」、「3-5%」剥成「35%」，负号/区间
#: 数字丢失，数据产品交付的文档可能把负增长呈现为正增长，是数据正确性缺陷；
#: 真正的排版交给官方 markdown→blocks 转换路径处理。
_BLANK_LINE_SPLIT = re.compile(r"\n\s*\n")


class DocumentDeliveryError(ValueError):
    """一次文档交付登记请求不满足硬性约束。

    ``reason_code`` 是机器可读的拒绝原因，供调用方记审计——文档分支记
    ``worker.document_request_rejected``，表格分支记
    ``worker.sheet_request_rejected``（两个事件名各自独立，不复用同一个让
    消费方无法区分来源）；不进模型上下文、不进用户可见文案。
    """

    def __init__(self, reason_code: str, message: str) -> None:
        """记录机器可读拒绝原因码与人类可读消息。"""
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class DocumentRequest:
    """一次登记成功的文档交付请求——报告契约 ``document_request`` 字段的来源。

    ``markdown``：模型传入的原始 markdown 全文，与 ``paragraphs``（由它归一化
    派生）一起持久化——``paragraphs`` 继续是兜底路径与检查点幂等判据的唯一
    依据，``markdown`` 只在 gateway 侧官方转换开关打开时才会被消费；开关关闭
    或这一列取不到时行为与"只做归一化、不做字符剥离"逐字相同。
    """

    title: str
    paragraphs: tuple[str, ...]
    markdown: str

    @property
    def total_chars(self) -> int:
        """全部段落的字符总数。"""
        return sum(len(paragraph) for paragraph in self.paragraphs)


@dataclass(frozen=True)
class SheetRequest:
    """一次登记成功的表格交付请求——报告契约 ``sheet_request`` 字段的来源。

    ``rows`` 是行×列的单元格文本二维数组，外层是行，内层是该行各列的单元格
    文本；:func:`build_sheet_request` 保证返回的 ``rows`` 始终是矩形（每一行
    列数相同，短行已补齐空字符串，见 :func:`_pad_sheet_rows` 文档）。
    """

    title: str
    rows: tuple[tuple[str, ...], ...]

    @property
    def total_chars(self) -> int:
        """全部单元格的字符总数。"""
        return sum(len(cell) for row in self.rows for cell in row)


def normalize_markdown(markdown: str) -> tuple[str, ...]:
    """轻量归一化：按空行切分为段落，段内换行折叠为空格。

    不剥离 Markdown 语法字符（原因见 ``_BLANK_LINE_SPLIT`` 上方注释）——用户
    会看到原样的 ``**``/``#`` 等符号，换来负号、区间数字这类正文内容不再被
    字符级改写破坏。这里产出的仍然是"段落列表"，不是要保留原始换行版式的
    文本块；空段（连续空行）不进入结果。
    """
    paragraphs: list[str] = []
    for block in _BLANK_LINE_SPLIT.split(markdown):
        collapsed = " ".join(line.strip() for line in block.splitlines()).strip()
        if collapsed:
            paragraphs.append(collapsed)
    return tuple(paragraphs)


def _validate_document_title(title: object) -> None:
    """标题长度与类型校验，不满足直接拒绝。"""
    if not isinstance(title, str) or not (MIN_TITLE_CHARS <= len(title.strip()) <= MAX_TITLE_CHARS):
        raise DocumentDeliveryError(
            "invalid_title",
            f"标题必须是 {MIN_TITLE_CHARS} 到 {MAX_TITLE_CHARS} 字符之间的字符串",
        )


def _validate_raw_markdown(markdown: object) -> None:
    """归一化之前，对原始 markdown 做类型、非空与长度上限校验。

    先校验原始长度、再做归一化——避免对一个刻意构造的超大字符串白跑一次正则
    切分，也堵住"归一化后字符数很小、但原始 markdown 本身可以无限大"这种
    绕过（见 ``MAX_RAW_MARKDOWN_CHARS`` 文档）。
    """
    if not isinstance(markdown, str) or not markdown.strip():
        raise DocumentDeliveryError("empty_markdown", "正文不能为空")
    if len(markdown) > MAX_RAW_MARKDOWN_CHARS:
        raise DocumentDeliveryError(
            "markdown_too_long",
            f"原始正文长度 {len(markdown)} 超过上限 {MAX_RAW_MARKDOWN_CHARS}",
        )


def _validate_document_paragraphs(paragraphs: tuple[str, ...]) -> None:
    """归一化后的段落数与总字符数校验。

    ``paragraphs`` 为空这条分支理论上不会被触发——只要 ``_validate_raw_
    markdown`` 已经通过（正文含至少一个非空白字符），空行切分必然保留住那个
    字符；保留作为防御性检查，不是判定"有没有用"的主要防线。
    """
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


def build_document_request(
    *,
    title: object,
    markdown: object,
    forbidden_values: Iterable[object] = (),
    internal_tool_names: Iterable[object] = (),
    system_prompt: str | None = None,
) -> DocumentRequest:
    """校验并构造一次文档交付请求；不满足任何一条硬性约束都抛 :class:`DocumentDeliveryError`。

    ``forbidden_values``/``internal_tool_names``/``system_prompt`` 与
    ``apps/worker/report.py::build_report`` 喂给终态正文 ``constrain_output`` 的
    是同一组值（由调用方对齐传入）——文档标题与正文离开进程前必须经过与终态
    正文同等的出口安全检查，不能因为走的是另一个工具就少一道检测。
    """
    _validate_document_title(title)
    _validate_raw_markdown(markdown)
    paragraphs = normalize_markdown(markdown)
    _validate_document_paragraphs(paragraphs)

    normalized_title = title.strip()
    combined_text = "\n".join((normalized_title, *paragraphs))
    output_safety = constrain_output(
        combined_text,
        forbidden_values=forbidden_values,
        internal_tool_names=internal_tool_names,
        system_prompt=system_prompt,
    )
    if output_safety.blocked:
        raise DocumentDeliveryError("leak_detected", "标题或正文命中输出安全检查，登记被拒绝")

    return DocumentRequest(title=normalized_title, paragraphs=paragraphs, markdown=markdown)


def _validate_sheet_title(title: object) -> None:
    """表格标题长度与类型校验，不满足直接拒绝。"""
    if not isinstance(title, str) or not (
        MIN_SHEET_TITLE_CHARS <= len(title.strip()) <= MAX_SHEET_TITLE_CHARS
    ):
        raise DocumentDeliveryError(
            "invalid_title",
            f"标题必须是 {MIN_SHEET_TITLE_CHARS} 到 {MAX_SHEET_TITLE_CHARS} 字符之间的字符串",
        )


def _normalize_sheet_rows(rows: object) -> list[tuple[str, ...]]:
    """校验并规范化每一行/每个单元格；任何一处越界立即拒绝，不静默截断。"""
    if not isinstance(rows, (list, tuple)) or not rows:
        raise DocumentDeliveryError("empty_rows", "表格内容不能为空")

    normalized_rows: list[tuple[str, ...]] = []
    for row_index, row in enumerate(rows):
        if not isinstance(row, (list, tuple)) or not row:
            raise DocumentDeliveryError(
                "invalid_row", f"第 {row_index + 1} 行必须是非空的单元格列表"
            )
        if len(row) > MAX_SHEET_COLUMNS:
            raise DocumentDeliveryError(
                "too_many_columns",
                f"第 {row_index + 1} 行列数 {len(row)} 超过上限 {MAX_SHEET_COLUMNS}",
            )
        normalized_cells: list[str] = []
        for column_index, cell in enumerate(row):
            if not isinstance(cell, str):
                raise DocumentDeliveryError(
                    "invalid_cell",
                    f"第 {row_index + 1} 行第 {column_index + 1} 列必须是字符串",
                )
            normalized_cells.append(cell)
        normalized_rows.append(tuple(normalized_cells))
    return normalized_rows


def _validate_sheet_size(normalized_rows: list[tuple[str, ...]]) -> None:
    """行数与总字符数硬上限校验。"""
    if len(normalized_rows) > MAX_SHEET_ROWS:
        raise DocumentDeliveryError(
            "too_many_rows", f"行数 {len(normalized_rows)} 超过上限 {MAX_SHEET_ROWS}"
        )
    total_chars = sum(len(cell) for row in normalized_rows for cell in row)
    if total_chars > MAX_SHEET_TOTAL_CHARS:
        raise DocumentDeliveryError(
            "too_many_chars", f"表格内容总长度 {total_chars} 超过上限 {MAX_SHEET_TOTAL_CHARS}"
        )


def _pad_sheet_rows(normalized_rows: list[tuple[str, ...]]) -> tuple[tuple[str, ...], ...]:
    """补齐成矩形矩阵：短行用空字符串补到最长行的列数。

    适配器写入覆盖的是整个矩形区域，不规则矩阵原样透传给适配器有静默写出
    错位数据的风险，补齐更保守。放在硬上限校验之后——补齐只会让行更长，若
    放在校验之前会让"是否超过 MAX_SHEET_COLUMNS"这条判断意外受补齐影响；
    补齐前每一行已确认不超限，补齐后的最大列数同样不超限，不会绕过硬上限。
    空字符串不改变字符数，已做过的总字符数校验不需要重算。
    """
    max_row_length = max(len(row) for row in normalized_rows)
    return tuple(
        row if len(row) == max_row_length else row + ("",) * (max_row_length - len(row))
        for row in normalized_rows
    )


def build_sheet_request(
    *,
    title: object,
    rows: object,
    forbidden_values: Iterable[object] = (),
    internal_tool_names: Iterable[object] = (),
    system_prompt: str | None = None,
) -> SheetRequest:
    """校验并构造一次表格交付请求，与 :func:`build_document_request` 逐项对应，见模块文档「表格分支」。

    ``forbidden_values``/``internal_tool_names``/``system_prompt`` 与
    :func:`build_document_request` 同一组值、同一出口安全检查。
    """
    _validate_sheet_title(title)
    normalized_rows = _normalize_sheet_rows(rows)
    _validate_sheet_size(normalized_rows)
    padded_rows = _pad_sheet_rows(normalized_rows)

    normalized_title = title.strip()
    combined_text = "\n".join((normalized_title, *(cell for row in padded_rows for cell in row)))
    output_safety = constrain_output(
        combined_text,
        forbidden_values=forbidden_values,
        internal_tool_names=internal_tool_names,
        system_prompt=system_prompt,
    )
    if output_safety.blocked:
        raise DocumentDeliveryError("leak_detected", "标题或表格内容命中输出安全检查，登记被拒绝")

    return SheetRequest(title=normalized_title, rows=padded_rows)
