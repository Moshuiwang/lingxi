"""``adapters.feishu_admin_card.TomlCompanyMetricCatalog``（#439 B 档）：真实
公司/指标下拉选项目录，读取随包发布的 ``config/company_function_metric_map.toml``。

本文件只覆盖 ``metric_map_path=None``（未配置外置文件）这一支；外置路径注入、
"外置文件里删掉的指标不得再出现在下拉里"、以及"配了却读不出来降级为空目录而不是
回落随包默认"在 ``tests/test_metric_map_single_source.py``（Trace #544 S-2c）。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from lingxi.adapters.feishu_admin_card import TomlCompanyMetricCatalog


class RealFileReadTests(unittest.TestCase):
    """读取真实随包配置文件——内容已由产品负责人 2026-08-19 填入（见该文件头部
    注释），本测试只钉住"读得到、能提取出非空的公司/指标集合"，不断言具体值
    随内容变化而漂移（配置文件内容不是本模块的契约）。"""

    def test_companies_and_metrics_are_non_empty_and_exclude_the_wildcard_key(self) -> None:
        catalog = TomlCompanyMetricCatalog(metric_map_path=None)

        companies = catalog.companies()
        metrics = catalog.metrics()

        self.assertGreater(len(companies), 0)
        self.assertNotIn("*", companies)
        self.assertGreater(len(metrics), 0)
        # 真实指标目录当前全部是英文 snake_case 内部 ID（#439 卡内证据的痛点
        # 本身）——钉住一个已知一定存在的真实指标 ID，回归就是"目录读取管线
        # 换了一种形状却没人发现"。
        self.assertIn("sub_new_count", metrics)

    def test_companies_are_sorted_and_deduplicated(self) -> None:
        catalog = TomlCompanyMetricCatalog(metric_map_path=None)
        companies = catalog.companies()
        self.assertEqual(companies, tuple(sorted(set(companies))))

    def test_metrics_are_sorted_and_deduplicated(self) -> None:
        catalog = TomlCompanyMetricCatalog(metric_map_path=None)
        metrics = catalog.metrics()
        self.assertEqual(metrics, tuple(sorted(set(metrics))))


class FailOpenTests(unittest.TestCase):
    """读取失败（文件缺失/格式非法）不得让调用方崩溃——展示层降级为空元组，
    见 ``core/admin/management_card.render_management_card`` 对空目录的处理。"""

    def test_missing_file_degrades_to_empty_tuples(self) -> None:
        catalog = TomlCompanyMetricCatalog(metric_map_path=None)
        with mock.patch(
            "lingxi.adapters.company_function_metric_map_file.default_company_function_metric_map_path",
            return_value=Path("/nonexistent/does-not-exist.toml"),
        ):
            self.assertEqual(catalog.companies(), ())
            self.assertEqual(catalog.metrics(), ())

    def test_malformed_document_degrades_to_empty_tuples_not_an_exception(self) -> None:
        catalog = TomlCompanyMetricCatalog(metric_map_path=None)
        with mock.patch(
            "lingxi.adapters.company_function_metric_map_file.load_company_function_metric_map",
            side_effect=ValueError("模拟格式非法"),
        ):
            self.assertEqual(catalog.companies(), ())
            self.assertEqual(catalog.metrics(), ())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
