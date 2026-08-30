"""``adapters.admin_metric_alias_map_file``（#439 A 档：指标中文别名映射表）。"""

from __future__ import annotations

import unittest
from pathlib import Path

from lingxi.adapters.admin_metric_alias_map_file import (
    default_admin_metric_alias_map_path,
    load_admin_metric_alias_map,
)


class RealFileTests(unittest.TestCase):
    def test_packaged_default_has_the_nine_pm_approved_aliases(self) -> None:
        """随包发布的别名表已由产品负责人 2026-08-30 填入九条别名（Trace #469
        S-1），见该文件模块文档。"""

        aliases = load_admin_metric_alias_map()
        self.assertEqual(
            aliases,
            {
                "新增订户数": "sub_new_count",
                "充值订户数": "sub_recharge_count",
                "充值金额": "sub_recharge_money",
                "扣费订户数": "sub_deduction_count",
                "扣费金额": "sub_deduction_money",
                "渠道市场份额": "channel_market_sharing",
                "渠道费率": "channel_rate",
                "汇率": "exchange_rate",
                "增值税率": "vat_rate",
            },
        )

    def test_default_path_points_at_an_existing_file(self) -> None:
        self.assertTrue(default_admin_metric_alias_map_path().is_file())


class FailOpenTests(unittest.TestCase):
    """读取或格式失败一律返回空映射（fail-open，与
    ``company_function_metric_map_file`` 的响亮失败纪律刻意相反，见模块文档）。"""

    def test_missing_file_returns_empty_mapping(self) -> None:
        aliases = load_admin_metric_alias_map(Path("/nonexistent/does-not-exist.toml"))
        self.assertEqual(aliases, {})

    def test_malformed_toml_returns_empty_mapping_not_an_exception(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            bad_file = Path(tmp) / "bad.toml"
            bad_file.write_text("this is not [ valid toml", encoding="utf-8")
            aliases = load_admin_metric_alias_map(bad_file)
        self.assertEqual(aliases, {})

    def test_missing_aliases_table_returns_empty_mapping(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            no_table_file = Path(tmp) / "no_table.toml"
            no_table_file.write_text("[something_else]\nfoo = 1\n", encoding="utf-8")
            aliases = load_admin_metric_alias_map(no_table_file)
        self.assertEqual(aliases, {})


class ContentShapeTests(unittest.TestCase):
    def test_valid_entries_are_returned_verbatim(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            valid_file = Path(tmp) / "valid.toml"
            valid_file.write_text(
                '[aliases]\n"新增用户数" = "sub_new_count"\n"充值金额" = "sub_recharge_money"\n',
                encoding="utf-8",
            )
            aliases = load_admin_metric_alias_map(valid_file)
        self.assertEqual(
            aliases, {"新增用户数": "sub_new_count", "充值金额": "sub_recharge_money"}
        )

    def test_entries_with_non_string_values_are_skipped_not_fatal(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            mixed_file = Path(tmp) / "mixed.toml"
            mixed_file.write_text(
                '[aliases]\n"好的别名" = "sub_new_count"\n"坏的别名" = 123\n',
                encoding="utf-8",
            )
            aliases = load_admin_metric_alias_map(mixed_file)
        self.assertEqual(aliases, {"好的别名": "sub_new_count"})

    def test_empty_string_key_or_value_is_skipped(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            edge_file = Path(tmp) / "edge.toml"
            edge_file.write_text('[aliases]\n"" = "x"\n"y" = ""\n', encoding="utf-8")
            aliases = load_admin_metric_alias_map(edge_file)
        self.assertEqual(aliases, {})


class MetricValueShapeTests(unittest.TestCase):
    """opus 审查坐实并修复：模块文档此前声称"命中路径的右值下游还会走
    ``_METRIC_TOKEN_PATTERN`` 校验"，但那条校验实际只发生在解析**原始**
    token 那一刻——命中别名表之后的右值从未再被任何下游校验过，加载器自己
    是唯一的把关点。变异锚点：把 ``load_admin_metric_alias_map`` 里的
    ``_METRIC_VALUE_PATTERN.fullmatch(value)`` 改回恒 ``True``（即改回修复前
    只判断"是不是非空字符串"）后，本类全部用例按预期变红。"""

    def _load(self, aliases_toml_body: str) -> dict:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "shape.toml"
            config_file.write_text(f"[aliases]\n{aliases_toml_body}\n", encoding="utf-8")
            return dict(load_admin_metric_alias_map(config_file))

    def test_a_value_containing_whitespace_is_skipped(self) -> None:
        """空白不在 ``_METRIC_TOKEN_PATTERN`` 的合法字符集里——一条形状脏的
        配置（例如误粘贴了一段说明文字）不该原样流向下游。"""

        aliases = self._load('"新增用户数" = "sub new count"\n"充值金额" = "sub_recharge_money"\n')
        self.assertEqual(aliases, {"充值金额": "sub_recharge_money"})

    def test_a_value_containing_a_semicolon_is_skipped(self) -> None:
        aliases = self._load('"坏别名" = "sub;drop"\n"好别名" = "sub_ok"\n')
        self.assertEqual(aliases, {"好别名": "sub_ok"})

    def test_a_value_longer_than_128_characters_is_skipped(self) -> None:
        too_long = "a" * 129
        aliases = self._load(f'"过长" = "{too_long}"\n"正常" = "sub_ok"\n')
        self.assertEqual(aliases, {"正常": "sub_ok"})

    def test_a_value_that_is_exactly_128_characters_is_kept(self) -> None:
        exactly_128 = "a" * 128
        aliases = self._load(f'"刚好" = "{exactly_128}"\n')
        self.assertEqual(aliases, {"刚好": exactly_128})

    def test_chinese_characters_in_the_value_are_still_legal(self) -> None:
        """形状与 ``_METRIC_TOKEN_PATTERN`` 逐字一致：中文字符本身合法（真实
        指标 ID 当前全是英文，但形状校验不该比下游更严）。"""

        aliases = self._load('"别名" = "中文指标ID"\n')
        self.assertEqual(aliases, {"别名": "中文指标ID"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
