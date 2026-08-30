"""``adapters.admin_metric_alias_map_file``（#439 A 档：指标中文别名映射表）。"""

from __future__ import annotations

import unittest
from pathlib import Path

from lingxi.adapters.admin_metric_alias_map_file import (
    default_admin_metric_alias_map_path,
    load_admin_metric_alias_map,
)


class RealFileTests(unittest.TestCase):
    def test_packaged_default_is_a_currently_empty_but_valid_alias_table(self) -> None:
        """随包发布的别名表当前是空的（载体先行，内容后填，见该文件模块文档）
        ——空表本身是合法内容，不是错误。"""

        aliases = load_admin_metric_alias_map()
        self.assertEqual(aliases, {})

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
