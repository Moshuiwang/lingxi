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

## 表格分支（Issue #354 S-H3-2，D2 裁定：同构 #341 文档交付路由）

``DELIVER_SPREADSHEET_TOOL_NAME`` 与 :func:`build_sheet_request`/
:class:`SheetRequest` 是「用户要一张表格」的同一机制新增分支，**不是**另开一条
触发通道：同一个 ``DELIVERY_MCP_SERVER_NAME`` MCP 服务、同一个
``document_delivery_enabled`` 装配开关、同一套出口安全检查（
:func:`~lingxi.core.execution.input_safety.constrain_output`）、
``apps/worker/turn.py`` 里同一个回合级请求槽位（一次只登记一份交付请求，
不论类型，同回合内后一次调用替换前一次——见该模块 ``_handle_deliver_document``/
``_handle_deliver_spreadsheet`` 的既有措辞"同一回合内多次调用以最后一次为准"，
这里把它扩展到跨类型：模型这一回合最后调用的是哪个工具，就登记哪一种类型，
不存在同一回合两种请求都非空的状态）。表格与文档唯一的差别是内容形状——
文档是"标题 + 段落文本"，表格是"标题 + 行×列的单元格文本二维数组"——因此没有
复用 :class:`DocumentRequest`/:func:`build_document_request` 本身，而是新增
一对结构对称的 ``Sheet*`` 名字，硬上限独立取值（见 ``MAX_SHEET_ROWS``/
``MAX_SHEET_TOTAL_CHARS`` 各自文档字符串），校验步骤（标题长度、内容非空、
硬上限、出口安全检查）与文档分支逐项对应。
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
#: 表格分支的工具名（Issue #354 S-H3-2）：同一个 MCP 服务名，新增一个并列工具
#: ——见模块文档「表格分支」一节，不是另开服务。
DELIVER_SPREADSHEET_TOOL_NAME = f"mcp__{DELIVERY_MCP_SERVER_NAME}__deliver_spreadsheet"

#: 硬上限：越过它一律 ``DocumentDeliveryError`` 拒绝，不静默截断（产品负责人
#: 审定设计第 2 条）。
MAX_PARAGRAPHS = 80
MAX_TOTAL_CHARS = 20000
MIN_TITLE_CHARS = 1
MAX_TITLE_CHARS = 100

#: 原始 markdown 全文长度硬上限（P2 顺手，独立审查）。``normalize_markdown``
#: 按空行切分段落时，纯空白/纯空行的"段"会折叠成空字符串直接被丢弃（不进
#: ``paragraphs``，见该函数 ``if collapsed:`` 判据）——只校验归一化后的
#: ``total_chars`` 挡不住"正文里塞进海量空白或海量纯空行段落，归一化后总
#: 字符数很小，但原始 ``markdown`` 本身可以无限大"这种绕过：`DocumentRequest.
#: markdown` 会把入参**逐字**存进返回值（不是归一化后的版本），最终经迁移
#: 0079 的 ``markdown`` 列持久化，必须独立设一道原始长度上限，不能只信任
#: 派生值。取 ``MAX_TOTAL_CHARS`` 的 2 倍（同一数量级，不是任意加大）：
#: 归一化只会让字符数变少或持平（逐行 strip、块间分隔符不计入任何段落），
#: 因此正常（未刻意构造）的 markdown 里"归一化后 total_chars"与"原始长度"
#: 通常同一量级、不会相差悬殊——2 倍上限既能继续让 ``too_many_chars``
#: （校验归一化后内容，见下方 ``build_document_request``）覆盖真实内容超限
#: 这一常见场景，又能单独兜住上面这种刻意用空白膨胀原始长度的绕过；若两者
#: 取同一个值，几乎所有会触发 ``too_many_chars`` 的输入都会先撞上这道更早
#: 执行的原始长度检查，`too_many_chars` 这条分支反而变成事实上的死代码
#: （本地验证发现：跑既有 `test_total_chars_over_limit_is_rejected_
#: without_silent_truncation` 用例时曾经因此报错码从 `too_many_chars` 变成
#: `markdown_too_long`）。
MAX_RAW_MARKDOWN_CHARS = MAX_TOTAL_CHARS * 2

#: 表格分支的硬上限（Issue #354 S-H3-2）：与 ``MAX_PARAGRAPHS``/``MAX_TOTAL_CHARS``
#: 同一取舍量级，独立取值——表格是行×列的单元格集合，不是段落文本，不共用同一个
#: 常量（共用会让两条互不相关的产品规则改一个就影响另一个）。80 行、每行至多
#: 40 列：覆盖问数场景常见的汇总表规模，越过一律拒绝，不静默截断/裁剪。
MAX_SHEET_ROWS = 80
MAX_SHEET_COLUMNS = 40
MAX_SHEET_TOTAL_CHARS = 20000
MIN_SHEET_TITLE_CHARS = MIN_TITLE_CHARS
MAX_SHEET_TITLE_CHARS = MAX_TITLE_CHARS

#: 段落切分用的空行分隔符（Issue #408 之前还有一个 ``_MARKDOWN_SYNTAX_CHARS``
#: 常量：对 ``#*`-[]()`` 八个字符逐字符剔除，本是「触发机制」卡片明确接受的
#: 简化，但连字符也在剔除清单里——正文「周环比 -12.85%」会被剥成「周环比
#: 12.85%」、「3-5%」会被剥成「35%」，负号/区间数字丢失，数据产品交付的文档
#: 可能把负增长呈现为正增长，这是数据正确性缺陷。产品负责人 2026-08-29 裁定
#: 先行停止字符剥离（真正的排版交给 ``adapters/feishu_docx_delivery.py`` 的
#: 官方 markdown→blocks 转换路径，开关状态见该模块文档「markdown 官方转换
#: 开关」一节）：这里现在只按空行切段、段内折叠，不再做任何字符级改写。
_BLANK_LINE_SPLIT = re.compile(r"\n\s*\n")


class DocumentDeliveryError(ValueError):
    """一次文档交付登记请求不满足硬性约束。

    ``reason_code`` 是机器可读的拒绝原因，供调用方记审计——文档分支记
    ``worker.document_request_rejected``，表格分支记 ``worker.sheet_request_
    rejected``（Trace #373 H3 批量审查 P2-8，两个事件名各自独立，不复用同一个
    让消费方无法区分来源）；不进模型上下文、不进用户可见文案。
    """

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class DocumentRequest:
    """一次登记成功的文档交付请求——报告契约 ``document_request`` 字段的来源。

    ``markdown``（Issue #408 正式方案接线）：模型传入的原始 markdown 全文，
    与 ``paragraphs``（由它归一化派生）一起持久化——``paragraphs`` 继续是兜底
    路径与检查点幂等判据（``adapters/feishu_docx_delivery.py::read_body_children``
    对应的写入内容判断）的唯一依据，``markdown`` 只在 gateway 侧官方转换开关
    （``LINGXI_DOCX_MARKDOWN_CONVERT``）打开时才会被消费；开关关闭或这一列取不到
    时行为与「先立即停止字符剥离」批次逐字相同。
    """

    title: str
    paragraphs: tuple[str, ...]
    markdown: str

    @property
    def total_chars(self) -> int:
        return sum(len(paragraph) for paragraph in self.paragraphs)


@dataclass(frozen=True)
class SheetRequest:
    """一次登记成功的表格交付请求（Issue #354 S-H3-2）——报告契约
    ``sheet_request`` 字段的来源。``rows`` 是行×列的单元格文本二维数组，外层是
    行，内层是该行各列的单元格文本；:func:`build_sheet_request` 保证返回的
    ``rows`` 始终是矩形（每一行列数相同，短行已补齐空字符串，见该函数文档）。
    """

    title: str
    rows: tuple[tuple[str, ...], ...]

    @property
    def total_chars(self) -> int:
        return sum(len(cell) for row in self.rows for cell in row)


def normalize_markdown(markdown: str) -> tuple[str, ...]:
    """轻量归一化：按空行切分为段落，段内换行折叠为空格。

    Issue #408 起不再剥离 Markdown 语法字符（原因见 ``_BLANK_LINE_SPLIT``
    上方注释）——用户暂时会看到原样的 ``**``/``#`` 等符号，换来负号、区间
    数字这类正文内容不再被字符级改写破坏。这里产出的仍然是"段落列表"，不是
    要保留原始换行版式的文本块；空段（连续空行）不进入结果。
    """

    paragraphs: list[str] = []
    for block in _BLANK_LINE_SPLIT.split(markdown):
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
    # P2 顺手（独立审查）：先校验原始长度、再做归一化——见 MAX_RAW_MARKDOWN_CHARS
    # 文档字符串。放在 normalize_markdown 调用之前，也避免对一个刻意构造的
    # 超大字符串白跑一次正则切分。
    if len(markdown) > MAX_RAW_MARKDOWN_CHARS:
        raise DocumentDeliveryError(
            "markdown_too_long",
            f"原始正文长度 {len(markdown)} 超过上限 {MAX_RAW_MARKDOWN_CHARS}",
        )

    paragraphs = normalize_markdown(markdown)
    # Issue #408 起 normalize_markdown 不再剥离字符：只要上面的 markdown.strip()
    # 检查已经通过（正文含至少一个非空白字符），空行切分必然保留住那个字符，
    # 这条分支因此不会再被触发——保留作为防御性检查（normalize_markdown 未来
    # 若改变实现，这里仍然兜底），不是判定"有没有用"的主要防线。
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

    return DocumentRequest(title=normalized_title, paragraphs=paragraphs, markdown=markdown)


def build_sheet_request(
    *,
    title: object,
    rows: object,
    forbidden_values: Iterable[object] = (),
    internal_tool_names: Iterable[object] = (),
    system_prompt: str | None = None,
) -> SheetRequest:
    """校验并构造一次表格交付请求（Issue #354 S-H3-2）；任何一条硬性约束不满足
    都抛 :class:`DocumentDeliveryError`，绝不静默降级或截断——与
    :func:`build_document_request` 逐项对应，见模块文档「表格分支」一节。

    ``rows`` 必须是非空列表，每一行必须是非空的字符串列表（单元格文本）；校验
    通过后**短行会被补齐空字符串到最长行的列数**，返回的 ``SheetRequest.rows``
    因此始终是矩形矩阵（Trace #373 H3 批量审查 P1）——适配器 ``write_values``
    的 ``range=A1:{end}{rows}`` 覆盖的是整个矩形区域，飞书对「``range`` 宽度大于
    某一行实际长度」这种不规则输入的真实语义本仓库从未验证过（探针只测过单格
    ``A1:A1``），把不规则矩阵原样透传给适配器有静默写出错位数据的风险；补齐成
    矩形更保守，真实多行多列语义留待 S-H3-4 L4a 验证。``forbidden_values``/
    ``internal_tool_names``/``system_prompt`` 与 :func:`build_document_request`
    同一组值、同一出口安全检查。
    """

    if not isinstance(title, str) or not (
        MIN_SHEET_TITLE_CHARS <= len(title.strip()) <= MAX_SHEET_TITLE_CHARS
    ):
        raise DocumentDeliveryError(
            "invalid_title",
            f"标题必须是 {MIN_SHEET_TITLE_CHARS} 到 {MAX_SHEET_TITLE_CHARS} 字符之间的字符串",
        )
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

    if len(normalized_rows) > MAX_SHEET_ROWS:
        raise DocumentDeliveryError(
            "too_many_rows", f"行数 {len(normalized_rows)} 超过上限 {MAX_SHEET_ROWS}"
        )
    total_chars = sum(len(cell) for row in normalized_rows for cell in row)
    if total_chars > MAX_SHEET_TOTAL_CHARS:
        raise DocumentDeliveryError(
            "too_many_chars", f"表格内容总长度 {total_chars} 超过上限 {MAX_SHEET_TOTAL_CHARS}"
        )

    # P1（Trace #373 H3 批量审查）：补齐成矩形——短行在这里用空字符串补到最长行
    # 的列数。放在硬上限校验之后：补齐只会让行更长，若放在校验之前会让"是否超过
    # MAX_SHEET_COLUMNS"这条判断意外受补齐影响；这里补齐前每一行已经确认
    # <= MAX_SHEET_COLUMNS，补齐后的最大列数同样 <= MAX_SHEET_COLUMNS，不会绕过
    # 硬上限。空字符串不改变 total_chars（len("") == 0），已经做过的总字符数校验
    # 不需要重算。
    max_row_length = max(len(row) for row in normalized_rows)
    padded_rows = tuple(
        row if len(row) == max_row_length else row + ("",) * (max_row_length - len(row))
        for row in normalized_rows
    )

    normalized_title = title.strip()
    combined_text = "\n".join(
        (normalized_title, *(cell for row in padded_rows for cell in row))
    )
    output_safety = constrain_output(
        combined_text,
        forbidden_values=forbidden_values,
        internal_tool_names=internal_tool_names,
        system_prompt=system_prompt,
    )
    if output_safety.blocked:
        raise DocumentDeliveryError(
            "leak_detected", "标题或表格内容命中输出安全检查，登记被拒绝"
        )

    return SheetRequest(title=normalized_title, rows=padded_rows)
