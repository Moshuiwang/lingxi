"""docx 正文交付的**综合样本**：一份覆盖全部常见 markdown 形态的正文，以及
它应当保真到对面的那些事实。

不是测试文件（``unittest discover`` 只收 ``test*.py``），是一份**与写入路径
无关**的共用夹具——同 ``tests/gateway_fakes.py`` 的姿态。

## 为什么要有它

Issue #538 的写入探针**只做过表格**。只有表格的样本验不出"引用块 / 嵌套列表 /
代码块 / 待办被写入端点拒收或被悄悄拍平"这一类回归，而嵌套列表在模型回答里非常
常见，不是边角场景（[#538 调研评论](
https://github.com/Moshuiwang/lingxi/issues/538) 2026-09-03 第三节）。

## 它**不**绑定任何一条写入路径

正文怎么写进飞书，本仓库前后有过两条路：客户端 ``blocks/convert`` ＋
``children``/``descendant`` 写块，和服务端 ``docs_ai/v1/documents`` 一次建档
写全文。**换路不该让样本跟着搬家**，所以本模块分成互不依赖的两层：

- :data:`COMPREHENSIVE_MARKDOWN`、:data:`COMPREHENSIVE_BLOCK_TYPE_HISTOGRAM`、
  :data:`COMPREHENSIVE_MARKDOWN_SHAPES`、:data:`COMPREHENSIVE_VERBATIM_TEXTS`
  ——**与路径无关**。谁来交付这段 markdown 都要回答同样的问题：十种形态在不在、
  块类型直方图对不对、这几段文字有没有被改写。stage 真实回读与本地用例用的是
  同一份。
- :func:`comprehensive_convert_response` ——**只服务客户端 convert 那条路**，
  是 ``blocks/convert`` 响应形状的夹具。换路之后可以整块删掉，上面四项不受影响。

## 如实标注的证据边界

:data:`COMPREHENSIVE_MARKDOWN` 本身只是一段 markdown，没有任何未验证的断言。
:data:`COMPREHENSIVE_BLOCK_TYPE_HISTOGRAM` 与 :func:`comprehensive_convert_response`
里的块类型取自飞书 docx 公开的 ``block_type`` 表，嵌套关系按 Issue #538 受控探针
实测的**表格**形状类推——**表格之外的形态没有真实 convert 响应做过对照**。这两项
钉住的是本仓库自己的装配与预期，不是"飞书真的会这样返回"。真实形状必须由 stage
真实回读回答；本模块不发起任何真实调用。
"""

from __future__ import annotations

from typing import Any

#: 综合样本正文（stage 复验与本地用例共用**同一份**）。
#:
#: 覆盖形态见 :data:`COMPREHENSIVE_MARKDOWN_SHAPES`：标题 1–3 级、嵌套无序列表、
#: 嵌套有序列表、引用块、代码块、待办（未完成 ＋ 已完成）、加粗、链接、含负号
#: ``-12.85%`` 与区间 ``3-5%`` 的表格、含 ``|`` 的单元格、分隔线。
#:
#: 负号与区间不是凑数：Issue #408 当初要修的正是"周环比 -12.85% 被剥成
#: 12.85%"这个数据正确性缺陷，区间 ``3-5%`` 与单元格里的 ``|`` 则是最容易被
#: 转义/分列逻辑吃掉的两种写法。
COMPREHENSIVE_MARKDOWN = """# 2026 年 8 月经营简报

## 整体表现

### 关键指标

**结论**：整体营收环比 -12.85%，毛利率维持在 3-5% 区间，口径见 [指标说明](https://example.invalid/metrics)。

- 华东大区
  - 上海
  - 杭州
- 华南大区

1. 先看营收
   1. 拆到公司
   2. 拆到月份
2. 再看毛利

> 口径以花名册与正式表为准，历史数据不回溯修正。

```sql
SELECT company, revenue FROM monthly_revenue WHERE month = '2026-08';
```

- [ ] 核对 8 月毛利口径
- [x] 已导出 7 月对照表

| 公司 | 环比 | 毛利率区间 | 备注 |
| --- | --- | --- | --- |
| A 公司 | -12.85% | 3-5% | 旧口径 A \\| B |
| B 公司 | +4.20% | 6-8% | 无 |

---

以上口径与正式表一致。
"""

#: 样本里每一种形态，与"它在 :data:`COMPREHENSIVE_MARKDOWN` 里长什么样"。
#:
#: 存在的理由：有人顺手删掉其中一种形态时要能立刻发现，否则"综合样本"会悄悄
#: 退化回"只有表格"——那正是 #538 要摆脱的那种验不出问题的样本。
COMPREHENSIVE_MARKDOWN_SHAPES: dict[str, str] = {
    "一级标题": "# 2026 年 8 月经营简报",
    "二级标题": "## 整体表现",
    "三级标题": "### 关键指标",
    "加粗": "**结论**",
    "链接": "[指标说明](https://example.invalid/metrics)",
    "无序列表": "- 华东大区",
    "嵌套无序列表": "  - 上海",
    "有序列表": "1. 先看营收",
    "嵌套有序列表": "   1. 拆到公司",
    "引用块": "> 口径以花名册与正式表为准",
    "代码块": "```sql",
    "待办（未完成）": "- [ ] 核对 8 月毛利口径",
    "待办（已完成）": "- [x] 已导出 7 月对照表",
    "表格": "| 公司 | 环比 | 毛利率区间 | 备注 |",
    "负号": "-12.85%",
    "区间": "3-5%",
    "单元格内的竖线": "旧口径 A \\| B",
    "分隔线": "\n---\n",
}

#: 交付之后必须**逐字**还能在正文里读到的文字。
#:
#: 与 :data:`COMPREHENSIVE_MARKDOWN_SHAPES` 的分工：那张表问"这段 markdown 里
#: 有没有这种写法"，这张表问"交付出去之后这段文字有没有被改写"。单元格里的
#: 竖线在 markdown 里是转义的 ``\\|``，落到文档里应当是**一个裸竖线**——因此
#: 两张表里的写法刻意不同，不能互相替代。
COMPREHENSIVE_VERBATIM_TEXTS: tuple[str, ...] = (
    "2026 年 8 月经营简报",
    "整体营收环比 -12.85%",
    "毛利率维持在 3-5% 区间",
    "口径以花名册与正式表为准，历史数据不回溯修正。",
    "SELECT company, revenue FROM monthly_revenue WHERE month = '2026-08';",
    "旧口径 A | B",
    "+4.20%",
    "6-8%",
    "以上口径与正式表一致。",
)

#: 综合样本转换后的**块类型直方图**（``block_type`` → 个数），共 44 个块。
#:
#: 用途：stage 真实回读时核对"十种形态一个都没少、也没有被拍平成段落"。**只数
#: 块、不数一级块**——嵌套子块（二级列表项、引用块内文字、单元格与单元格内文字）
#: 都算在内。
COMPREHENSIVE_BLOCK_TYPE_HISTOGRAM: dict[int, int] = {
    2: 15,  # 文本：正文段落 1 ＋ 引用块内文字 1 ＋ 单元格内文字 12 ＋ 结尾段 1
    3: 1,  # 一级标题
    4: 1,  # 二级标题
    5: 1,  # 三级标题
    12: 4,  # 无序列表：2 个一级 ＋ 2 个二级
    13: 4,  # 有序列表：2 个一级 ＋ 2 个二级
    14: 1,  # 代码块
    17: 2,  # 待办：未完成 ＋ 已完成
    22: 1,  # 分隔线
    31: 1,  # 表格
    32: 12,  # 单元格：3 行 × 4 列
    34: 1,  # 引用块容器
}

#: 样本表格的真实内容（3 行 × 4 列，含表头）。单元格里的竖线在这里是**裸**竖线：
#: markdown 侧的 ``\\|`` 转义只属于 markdown 语法，不该出现在文档正文里。
COMPREHENSIVE_TABLE_ROWS: tuple[tuple[str, ...], ...] = (
    ("公司", "环比", "毛利率区间", "备注"),
    ("A 公司", "-12.85%", "3-5%", "旧口径 A | B"),
    ("B 公司", "+4.20%", "6-8%", "无"),
)

#: 综合样本的一级块 ``block_id``，按**文档顺序**（不是 ``blocks`` 数组的物理
#: 顺序——Issue #442 实测两者不同）。只服务客户端 convert 那条路。
COMPREHENSIVE_FIRST_LEVEL_BLOCK_IDS: tuple[str, ...] = (
    "blk-h1",
    "blk-h2",
    "blk-h3",
    "blk-para",
    "blk-ul-1",
    "blk-ul-2",
    "blk-ol-1",
    "blk-ol-2",
    "blk-quote",
    "blk-code",
    "blk-todo-1",
    "blk-todo-2",
    "blk-table",
    "blk-divider",
    "blk-tail",
)


def _elements(*runs: object) -> list[dict[str, Any]]:
    """把 ``文字`` 或 ``(文字, 样式)`` 组装成 convert 响应里的 ``elements`` 数组。"""

    elements: list[dict[str, Any]] = []
    for run in runs:
        content, style = run if isinstance(run, tuple) else (run, None)
        text_run: dict[str, Any] = {"content": content}
        if style is not None:
            text_run["text_element_style"] = style
        elements.append({"text_run": text_run})
    return elements


def comprehensive_convert_response() -> dict[str, Any]:
    """:data:`COMPREHENSIVE_MARKDOWN` 在 ``blocks/convert`` 端点的响应夹具。

    **只服务客户端 convert 那条路**（``POST /docx/v1/documents/blocks/convert``
    ＋ ``children``/``descendant`` 写块）；服务端一次建档那条路用不到它，换路
    之后可以整块删除，本模块其余部分不受影响。

    形状按 Issue #442／#538 的实测口径造：块的物理顺序**不等于**文档顺序（真实
    顺序由 ``first_level_block_ids`` 给出）、父块用 ``children`` 指向子块的临时
    ``block_id``、子块不出现在 ``first_level_block_ids`` 里、每个块带恒为空串的
    ``parent_id``、表格带服务端计算的 ``table.cells`` 与 ``table.property.
    merge_info``（后者带上就会被 ``descendant`` 端点以 ``1770001`` 整体拒绝）。

    **如实标注**：表格之外的形态没有真实 convert 响应做过对照，见模块文档
    「如实标注的证据边界」。
    """

    blocks: list[dict[str, Any]] = [
        {
            "block_id": "blk-h1",
            "parent_id": "",
            "block_type": 3,
            "heading1": {"elements": _elements("2026 年 8 月经营简报")},
        },
        {
            "block_id": "blk-h2",
            "parent_id": "",
            "block_type": 4,
            "heading2": {"elements": _elements("整体表现")},
        },
        {
            "block_id": "blk-h3",
            "parent_id": "",
            "block_type": 5,
            "heading3": {"elements": _elements("关键指标")},
        },
        {
            "block_id": "blk-para",
            "parent_id": "",
            "block_type": 2,
            "text": {
                "elements": _elements(
                    ("结论", {"bold": True}),
                    "：整体营收环比 -12.85%，毛利率维持在 3-5% 区间，口径见 ",
                    ("指标说明", {"link": {"url": "https://example.invalid/metrics"}}),
                    "。",
                )
            },
        },
        {
            "block_id": "blk-ul-1",
            "parent_id": "",
            "block_type": 12,
            "children": ["blk-ul-1-1", "blk-ul-1-2"],
            "bullet": {"elements": _elements("华东大区")},
        },
        {
            "block_id": "blk-ul-1-1",
            "parent_id": "",
            "block_type": 12,
            "bullet": {"elements": _elements("上海")},
        },
        {
            "block_id": "blk-ul-1-2",
            "parent_id": "",
            "block_type": 12,
            "bullet": {"elements": _elements("杭州")},
        },
        {
            "block_id": "blk-ul-2",
            "parent_id": "",
            "block_type": 12,
            "bullet": {"elements": _elements("华南大区")},
        },
        {
            "block_id": "blk-ol-1",
            "parent_id": "",
            "block_type": 13,
            "children": ["blk-ol-1-1", "blk-ol-1-2"],
            "ordered": {"elements": _elements("先看营收")},
        },
        {
            "block_id": "blk-ol-1-1",
            "parent_id": "",
            "block_type": 13,
            "ordered": {"elements": _elements("拆到公司")},
        },
        {
            "block_id": "blk-ol-1-2",
            "parent_id": "",
            "block_type": 13,
            "ordered": {"elements": _elements("拆到月份")},
        },
        {
            "block_id": "blk-ol-2",
            "parent_id": "",
            "block_type": 13,
            "ordered": {"elements": _elements("再看毛利")},
        },
        {
            "block_id": "blk-quote",
            "parent_id": "",
            "block_type": 34,
            "children": ["blk-quote-text"],
            "quote_container": {},
        },
        {
            "block_id": "blk-quote-text",
            "parent_id": "",
            "block_type": 2,
            "text": {"elements": _elements("口径以花名册与正式表为准，历史数据不回溯修正。")},
        },
        {
            "block_id": "blk-code",
            "parent_id": "",
            "block_type": 14,
            "code": {
                "elements": _elements(
                    "SELECT company, revenue FROM monthly_revenue WHERE month = '2026-08';"
                ),
                "style": {"language": 30, "wrap": False},
            },
        },
        {
            "block_id": "blk-todo-1",
            "parent_id": "",
            "block_type": 17,
            "todo": {"elements": _elements("核对 8 月毛利口径"), "style": {"done": False}},
        },
        {
            "block_id": "blk-todo-2",
            "parent_id": "",
            "block_type": 17,
            "todo": {"elements": _elements("已导出 7 月对照表"), "style": {"done": True}},
        },
    ]

    cell_ids = [f"cell-{row}-{column}" for row in range(3) for column in range(4)]
    blocks.append(
        {
            "block_id": "blk-table",
            "parent_id": "",
            "block_type": 31,
            "children": list(cell_ids),
            "table": {
                # 服务端计算、与 children 完全重复的只读键。
                "cells": list(cell_ids),
                "property": {
                    "row_size": 3,
                    "column_size": 4,
                    "column_width": [180, 120, 140, 200],
                    # 带着它调 descendant 端点会被 1770001 整体拒绝（探针实测）。
                    "merge_info": [{"col_span": 1, "row_span": 1}] * len(cell_ids),
                },
            },
        }
    )
    for index, cell_id in enumerate(cell_ids):
        blocks.append(
            {
                "block_id": cell_id,
                "parent_id": "",
                "block_type": 32,
                "children": [f"{cell_id}-text"],
                "table_cell": {},
            }
        )
        blocks.append(
            {
                "block_id": f"{cell_id}-text",
                "parent_id": "",
                "block_type": 2,
                "text": {
                    "elements": _elements(COMPREHENSIVE_TABLE_ROWS[index // 4][index % 4])
                },
            }
        )
    blocks.append({"block_id": "blk-divider", "parent_id": "", "block_type": 22, "divider": {}})
    blocks.append(
        {
            "block_id": "blk-tail",
            "parent_id": "",
            "block_type": 2,
            "text": {"elements": _elements("以上口径与正式表一致。")},
        }
    )
    return {
        "code": 0,
        "data": {
            "blocks": blocks,
            "first_level_block_ids": list(COMPREHENSIVE_FIRST_LEVEL_BLOCK_IDS),
            "block_id_to_image_urls": {},
        },
    }
