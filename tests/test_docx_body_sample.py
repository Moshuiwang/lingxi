"""``tests/docx_body_sample.py`` 这份综合样本自身的自洽性检查。

**刻意不经过任何一条写入路径**：样本是交付物，不是某条路径的附属品。docx 正文
怎么写进飞书前后有过两条路（客户端 ``blocks/convert`` ＋ 写块、服务端一次建档
写全文），本文件的断言在换路前后都成立，不需要跟着搬家。

它守住两件事：

1. **样本没有悄悄退化回"只有表格"**——十八种写法（含标题三级、嵌套两种列表、
   引用块、代码块、待办、加粗、链接、负号、区间、竖线、分隔线）逐条在位。
   Issue #538 的写入探针只做过表格，只有表格的样本验不出其它形态的回归。
2. **样本的两份"预期"彼此对得上**——块类型直方图与表格内容必须与正文现数出来
   的结果一致。stage 真实回读拿这张直方图当对照表，它自己先错了，真实回读就会
   照着一个错的预期判绿。
"""

from __future__ import annotations

import re
import unittest

from docx_body_sample import (
    COMPREHENSIVE_BLOCK_TYPE_HISTOGRAM,
    COMPREHENSIVE_MARKDOWN,
    COMPREHENSIVE_MARKDOWN_SHAPES,
    COMPREHENSIVE_TABLE_ROWS,
    COMPREHENSIVE_VERBATIM_TEXTS,
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


class ComprehensiveHistogramTest(unittest.TestCase):
    """块类型直方图与样本正文必须对得上。

    stage 真实回读拿这张直方图当对照表（Trace #544 探针二实测：一次建档后回读
    45 块 ＝ 1 个 page 根块 ＋ 本表的 44 个，逐项相符），**它自己先错了，真实
    回读就会照着一个错的预期判绿**。所以这里从 markdown 正文**现数**一遍能机械
    数出来的那几种形态，与直方图对账——有人往样本里加一条列表、删一个待办却忘了
    改直方图时必须变红。

    只核能从 markdown 无歧义数出来的类型；``block_type=2``（文本）由"单元格
    文字 ＋ 引用块内文字 ＋ 独立段落"三部分构成，靠正则数容易假绿，改为核它与
    表格单元格数之间的关系。
    """

    LINES = COMPREHENSIVE_MARKDOWN.splitlines()

    def _count(self, pattern: str) -> int:
        return sum(1 for line in self.LINES if re.match(pattern, line))

    def test_heading_levels_match_the_histogram(self) -> None:
        self.assertEqual(self._count(r"# [^#]"), COMPREHENSIVE_BLOCK_TYPE_HISTOGRAM[3])
        self.assertEqual(self._count(r"## [^#]"), COMPREHENSIVE_BLOCK_TYPE_HISTOGRAM[4])
        self.assertEqual(self._count(r"### [^#]"), COMPREHENSIVE_BLOCK_TYPE_HISTOGRAM[5])

    def test_list_and_todo_counts_match_the_histogram(self) -> None:
        """无序列表与待办都以 ``- `` 开头，必须分开数——混在一起时"删掉一个待办、
        加一条列表"这种改动会互相抵消，直方图错了也不会红。"""

        self.assertEqual(self._count(r"\s*- \[[ x]\] "), COMPREHENSIVE_BLOCK_TYPE_HISTOGRAM[17])
        self.assertEqual(self._count(r"\s*- (?!\[[ x]\])"), COMPREHENSIVE_BLOCK_TYPE_HISTOGRAM[12])
        self.assertEqual(self._count(r"\s*\d+\. "), COMPREHENSIVE_BLOCK_TYPE_HISTOGRAM[13])

    def test_quote_code_and_divider_counts_match_the_histogram(self) -> None:
        self.assertEqual(self._count(r"> "), COMPREHENSIVE_BLOCK_TYPE_HISTOGRAM[34])
        self.assertEqual(self._count(r"```\w") , COMPREHENSIVE_BLOCK_TYPE_HISTOGRAM[14])
        self.assertEqual(sum(1 for line in self.LINES if line == "---"), COMPREHENSIVE_BLOCK_TYPE_HISTOGRAM[22])

    def test_table_cell_count_matches_the_declared_rows(self) -> None:
        cells = sum(len(row) for row in COMPREHENSIVE_TABLE_ROWS)
        self.assertEqual(cells, COMPREHENSIVE_BLOCK_TYPE_HISTOGRAM[32])
        self.assertEqual(COMPREHENSIVE_BLOCK_TYPE_HISTOGRAM[31], 1)
        # 每个单元格里还有一个文本块，引用块内也有一个——文本块数必须容得下它们。
        self.assertGreater(COMPREHENSIVE_BLOCK_TYPE_HISTOGRAM[2], cells)

    def test_the_declared_rows_appear_verbatim_in_the_markdown_table(self) -> None:
        """表格内容与正文必须是同一份：单元格里的裸竖线在 markdown 侧是 ``\\|``，
        两种写法各自成立，但**除了这一处转义之外不得有第二处差异**。"""

        for row in COMPREHENSIVE_TABLE_ROWS:
            with self.subTest(row=row):
                escaped = tuple(cell.replace("|", "\\|") for cell in row)
                self.assertIn("| " + " | ".join(escaped) + " |", COMPREHENSIVE_MARKDOWN)

    def test_every_verbatim_text_is_actually_in_the_sample(self) -> None:
        """逐字核对项必须真的能在样本里找到——否则"交付之后这段文字有没有被
        改写"这个断言在下游会永远判绿。表格单元格那条走裸竖线，markdown 侧是
        转义写法，两者刻意不同，因此单独处理。"""

        for text in COMPREHENSIVE_VERBATIM_TEXTS:
            with self.subTest(text=text):
                self.assertIn(text.replace("|", "\\|"), COMPREHENSIVE_MARKDOWN)
