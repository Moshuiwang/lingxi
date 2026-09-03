"""``tests/docx_body_sample.py`` 这份综合样本自身的自洽性检查。

**刻意不经过任何一条写入路径**：样本是交付物，不是某条路径的附属品。docx 正文
怎么写进飞书前后有过两条路（客户端 ``blocks/convert`` ＋ 写块、服务端一次建档
写全文），本文件的断言在换路前后都成立，不需要跟着搬家。

它守住两件事：

1. **样本没有悄悄退化回"只有表格"**——十八种写法（含标题三级、嵌套两种列表、
   引用块、代码块、待办、加粗、链接、负号、区间、竖线、分隔线）逐条在位。
   Issue #538 的写入探针只做过表格，只有表格的样本验不出其它形态的回归。
2. **样本的三份"预期"彼此对得上**——块类型直方图、表格内容、父子关系必须与
   convert 夹具一致。stage 真实回读拿这张直方图当对照表，它自己先错了，真实
   回读就会照着一个错的预期判绿。
"""

from __future__ import annotations

import unittest

from docx_body_sample import (
    COMPREHENSIVE_BLOCK_TYPE_HISTOGRAM,
    COMPREHENSIVE_FIRST_LEVEL_BLOCK_IDS,
    COMPREHENSIVE_MARKDOWN,
    COMPREHENSIVE_MARKDOWN_SHAPES,
    COMPREHENSIVE_TABLE_ROWS,
    COMPREHENSIVE_VERBATIM_TEXTS,
    comprehensive_convert_response,
)


class ComprehensiveMarkdownTest(unittest.TestCase):
    """样本正文本身：形态齐、文字对。"""

    def test_every_declared_shape_is_actually_in_the_sample(self) -> None:
        """有人顺手删掉一种形态（或改写成另一种）时必须变红，否则"综合样本"会
        悄悄退化回"只有表格"——那正是 #538 要摆脱的、验不出问题的样本。"""

        for shape, fragment in COMPREHENSIVE_MARKDOWN_SHAPES.items():
            with self.subTest(shape=shape):
                self.assertIn(fragment, COMPREHENSIVE_MARKDOWN)

    def test_the_sample_covers_the_shapes_the_probe_never_touched(self) -> None:
        """Issue #538 的写入探针只覆盖过表格。这里显式点名那几种**没被探针
        覆盖过**、且在模型回答里很常见的形态，确保它们不是"碰巧还在"。"""

        for shape in ("嵌套无序列表", "嵌套有序列表", "引用块", "代码块", "待办（未完成）"):
            with self.subTest(shape=shape):
                self.assertIn(shape, COMPREHENSIVE_MARKDOWN_SHAPES)

    def test_the_pipe_inside_a_table_cell_is_escaped_in_markdown_but_bare_in_text(self) -> None:
        """单元格里的竖线是最容易被吃掉的一处：markdown 里必须转义成 ``\\|``
        （否则会被当成列分隔符），交付出去之后必须还原成**一个裸竖线**。两张表
        因此刻意写法不同，谁把它们改成一样就说明有一侧错了。"""

        self.assertIn("旧口径 A \\| B", COMPREHENSIVE_MARKDOWN)
        self.assertIn("旧口径 A | B", COMPREHENSIVE_VERBATIM_TEXTS)
        self.assertNotIn("旧口径 A | B", COMPREHENSIVE_MARKDOWN)

    def test_the_negative_sign_and_the_range_survive_in_the_sample(self) -> None:
        """Issue #408 当初要修的正是"周环比 -12.85% 被剥成 12.85%"这个数据
        正确性缺陷；区间 ``3-5%`` 是同一类写法的另一面。两者必须同时出现在
        正文段落与表格单元格里——只在其中一处出现，验不出"表格里被吃掉"。"""

        self.assertIn("整体营收环比 -12.85%", COMPREHENSIVE_MARKDOWN)
        self.assertIn("毛利率维持在 3-5% 区间", COMPREHENSIVE_MARKDOWN)
        self.assertIn(("A 公司", "-12.85%", "3-5%", "旧口径 A | B"), COMPREHENSIVE_TABLE_ROWS)


class ComprehensiveConvertFixtureTest(unittest.TestCase):
    """convert 夹具与样本的三份"预期"必须互相对得上。

    这些断言**不调用任何适配器**，只核夹具自身：直方图、表格内容、父子关系。
    stage 真实回读拿这张直方图当对照表，它自己先错了，真实回读会照着一个错的
    预期判绿。
    """

    def setUp(self) -> None:
        self.response = comprehensive_convert_response()
        self.blocks = self.response["data"]["blocks"]
        self.by_id = {block["block_id"]: block for block in self.blocks}

    def test_the_block_type_histogram_matches_the_fixture(self) -> None:
        histogram: dict[int, int] = {}
        for block in self.blocks:
            histogram[block["block_type"]] = histogram.get(block["block_type"], 0) + 1

        self.assertEqual(histogram, COMPREHENSIVE_BLOCK_TYPE_HISTOGRAM)
        self.assertEqual(sum(histogram.values()), 44, "15 个一级块 ＋ 5 个嵌套子块 ＋ 24 个表格块")
        self.assertEqual(len(self.by_id), len(self.blocks), "block_id 不得重复")

    def test_every_block_is_reachable_exactly_once(self) -> None:
        """每个块要么是一级块、要么恰好被一个父块的 ``children`` 认领一次。

        漏认领 = 有块无处安放（真实响应里这会触发 ``unsupported_nested_blocks``
        整篇降级）；被两个父块认领 = 结构自相矛盾。夹具自己先不能犯这两种错。
        """

        claimed: dict[str, int] = {block_id: 0 for block_id in self.by_id}
        for block_id in COMPREHENSIVE_FIRST_LEVEL_BLOCK_IDS:
            self.assertIn(block_id, self.by_id, f"一级块 {block_id} 不在 blocks 里")
            claimed[block_id] += 1
        for block in self.blocks:
            for child_id in block.get("children", ()):
                self.assertIn(child_id, self.by_id, f"子块 {child_id} 不在 blocks 里")
                claimed[child_id] += 1

        self.assertEqual(
            [block_id for block_id, count in claimed.items() if count != 1],
            [],
            "每个块必须恰好被认领一次（0 次＝无处安放，2 次＝结构自相矛盾）",
        )

    def test_the_physical_order_is_deliberately_not_the_document_order(self) -> None:
        """Issue #442 实测：``blocks`` 数组的物理顺序**不是**文档顺序，真实顺序
        由 ``first_level_block_ids`` 给出。夹具必须保留这个差异——两者恰好一致
        的夹具验不出"按响应原始顺序写入"这个缺陷。"""

        physical = [block["block_id"] for block in self.blocks]
        self.assertNotEqual(physical[: len(COMPREHENSIVE_FIRST_LEVEL_BLOCK_IDS)],
                            list(COMPREHENSIVE_FIRST_LEVEL_BLOCK_IDS))
        self.assertEqual(
            self.response["data"]["first_level_block_ids"],
            list(COMPREHENSIVE_FIRST_LEVEL_BLOCK_IDS),
        )

    def test_the_table_cells_carry_the_declared_rows_verbatim(self) -> None:
        table = self.by_id["blk-table"]
        cell_ids = table["children"]
        self.assertEqual(len(cell_ids), 12, "3 行 × 4 列")
        self.assertEqual(table["table"]["property"]["row_size"], len(COMPREHENSIVE_TABLE_ROWS))
        self.assertEqual(table["table"]["property"]["column_size"], len(COMPREHENSIVE_TABLE_ROWS[0]))
        for index, cell_id in enumerate(cell_ids):
            with self.subTest(cell=cell_id):
                text_id = self.by_id[cell_id]["children"][0]
                content = self.by_id[text_id]["text"]["elements"][0]["text_run"]["content"]
                self.assertEqual(content, COMPREHENSIVE_TABLE_ROWS[index // 4][index % 4])

    def test_the_fixture_keeps_the_readonly_keys_the_write_path_has_to_strip(self) -> None:
        """夹具必须**带上** ``table.cells`` 与 ``table.property.merge_info``：
        它们是服务端计算的只读键，``merge_info`` 一个字段就换来 ``1770001``
        整体拒绝（Issue #538 探针实测）。夹具不带它们，就验不出剥字段这一步。"""

        table = self.by_id["blk-table"]["table"]
        self.assertEqual(table["cells"], self.by_id["blk-table"]["children"])
        self.assertEqual(len(table["property"]["merge_info"]), 12)
        self.assertTrue(all(block["parent_id"] == "" for block in self.blocks))

    def test_every_verbatim_text_appears_somewhere_in_the_fixture(self) -> None:
        """:data:`COMPREHENSIVE_VERBATIM_TEXTS` 是交付后要逐字核对的清单——每一
        条都必须真的能在转换结果里找到，否则 stage 复验会照着一条根本不存在的
        文字去核对，永远判红或永远判绿。"""

        contents = {
            element["text_run"]["content"]
            for block in self.blocks
            for value in block.values()
            if isinstance(value, dict)
            for element in value.get("elements", ())
        }
        joined = "\n".join(contents)
        for text in COMPREHENSIVE_VERBATIM_TEXTS:
            with self.subTest(text=text):
                self.assertIn(text, joined)
