"""指标 ID→中文名读取口（``lingxi.config.metric_labels``）。

背景：2026-09-07 生产预检当场发现，欢迎卡与「当前可用范围」通知把
``sub_recharge_money`` 这类内部 snake_case 指标 ID 原样给用户看。中文名产品里
本来就有（``config/admin_metric_alias_map.toml``，产品负责人 2026-08-30 逐条裁定），
只是那两条路径从没调用过。本文件钉住这份数据的读取与方向。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from lingxi.adapters.company_function_metric_map_file import load_company_function_metric_map
from lingxi.config.metric_labels import (
    default_metric_labels,
    load_metric_alias_map,
    reverse_metric_labels,
)


class ReverseDirectionTest(unittest.TestCase):
    def test_the_display_side_gets_metric_id_to_chinese_name(self) -> None:
        labels = reverse_metric_labels({"充值金额": "sub_recharge_money"})
        self.assertEqual(labels, {"sub_recharge_money": "充值金额"})

    def test_one_metric_with_several_aliases_takes_the_first_line_in_the_file(self) -> None:
        """同一个 ID 写了多个别名时取文件里第一次出现的那个，与展示顺序同一条依据。"""
        self.assertEqual(reverse_metric_labels({"乙别名": "m1", "甲别名": "m1"})["m1"], "乙别名")

    def test_the_file_order_becomes_the_display_order(self) -> None:
        """键序即用户可见的展示顺序——改别名表的行序就能改卡上的顺序，不必动代码。"""
        labels = reverse_metric_labels({"甲": "m2", "乙": "m1", "丙": "m3"})
        self.assertEqual(list(labels), ["m2", "m1", "m3"])


class FailOpenTest(unittest.TestCase):
    def test_a_missing_file_reads_as_an_empty_map(self) -> None:
        with TemporaryDirectory() as folder:
            self.assertEqual(load_metric_alias_map(Path(folder) / "nope.toml"), {})

    def test_a_broken_file_reads_as_an_empty_map(self) -> None:
        with TemporaryDirectory() as folder:
            path = Path(folder) / "broken.toml"
            path.write_text("[aliases\n", encoding="utf-8")
            self.assertEqual(load_metric_alias_map(path), {})

    def test_one_bad_entry_does_not_take_the_whole_file_down(self) -> None:
        with TemporaryDirectory() as folder:
            path = Path(folder) / "mixed.toml"
            path.write_text(
                '[aliases]\n"充值金额" = "sub_recharge_money"\n"坏的" = "带 空格"\n',
                encoding="utf-8",
            )
            self.assertEqual(load_metric_alias_map(path), {"充值金额": "sub_recharge_money"})


class ShippedCatalogTest(unittest.TestCase):
    """**这条是本次缺陷的长期防线**：目录里新增一个指标却忘了给中文名，当场判红。

    忘了给中文名的后果不是"显示成英文"——欢迎卡按失败关闭整条跳过（见
    ``core/outreach/audience``），那个人会**收不到卡**，而且只有到生产才看得出来。
    """

    def test_every_metric_in_the_shipped_catalog_has_a_chinese_name(self) -> None:
        catalog = load_company_function_metric_map(None)
        metric_ids = {
            metric_id
            for by_function in catalog.values()
            for metric_names in by_function.values()
            for metric_id in metric_names
        }
        self.assertTrue(metric_ids, "随包指标目录不该是空的")
        missing = sorted(metric_ids - set(default_metric_labels()))
        self.assertEqual(missing, [], f"这些指标没有中文名，收到它们的人会被整条跳过：{missing}")

    def test_no_shipped_chinese_name_is_itself_an_internal_id(self) -> None:
        """否定断言：别名列里填一个 snake_case 值等于什么都没翻译。"""
        offenders = sorted(label for label in default_metric_labels().values() if label.isascii())
        self.assertEqual(offenders, [])


class CachedViewTest(unittest.TestCase):
    def test_the_default_map_cannot_be_mutated_by_a_caller(self) -> None:
        """缓存对象被就地改掉会污染同进程内其余全部渲染。"""
        labels = default_metric_labels()
        with self.assertRaises(TypeError):
            labels["sub_recharge_money"] = "改掉它"  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
