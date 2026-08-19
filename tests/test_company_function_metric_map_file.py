"""随包发布的「公司 + 职能 → 指标名」翻译映射配置文件（Issue #227）。

镜像 ``tests/test_galaxy_role_function.py`` 的 ``ShippedRoleFunctionMapFileTest``
写法：证明随包发布的那份文件真的能被加载器解析，且当前**内容为空**——这是本 Story
交付时的预期状态（产品负责人尚未填入映射），不是缺陷。文件缺失或格式错误则仍然
按配置错误处理（不静默退化）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lingxi.adapters.company_function_metric_map_file import (
    default_company_function_metric_map_path,
    load_company_function_metric_map,
)


class ShippedConfigFileTest(unittest.TestCase):
    """随包发布的那份文件：当前为空，是合法且预期的状态。"""

    def test_the_shipped_file_parses_and_is_currently_empty(self) -> None:
        mapping = load_company_function_metric_map(default_company_function_metric_map_path())

        self.assertEqual(dict(mapping), {})

    def test_the_shipped_file_documents_the_current_empty_state_and_how_to_fill_it(self) -> None:
        text = default_company_function_metric_map_path().read_text(encoding="utf-8")

        self.assertIn("当前是空的", text)
        self.assertIn("companies", text)
        # 示例内容必须明确标注、且整体注释掉——不得被 tomllib 当成真实条目解析出来。
        self.assertIn("示例", text)
        self.assertEqual(
            dict(load_company_function_metric_map(default_company_function_metric_map_path())),
            {},
            "示例必须整体注释掉，不能被解析成真实映射内容",
        )

    def test_the_config_file_is_shipped_inside_the_python_package(self) -> None:
        path = default_company_function_metric_map_path()

        self.assertEqual(path.parent.name, "config")
        self.assertEqual(path.parent.parent.name, "lingxi")
        self.assertEqual(path.name, "company_function_metric_map.toml")


class LoaderFailureModeTest(unittest.TestCase):
    """文件缺失或格式错误：失败关闭，不静默退化成空映射。

    "空映射合法" 与 "读不出来" 必须可分辨——前者是内容还没填，后者是部署配置本身
    有问题；把两者混成同一种结果，会让运维分不清该找谁。
    """

    def test_a_missing_file_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist.toml"
            with self.assertRaises(OSError):
                load_company_function_metric_map(missing)

    def test_malformed_toml_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.toml"
            broken.write_text("this is not [ valid toml", encoding="utf-8")
            with self.assertRaises(Exception):
                load_company_function_metric_map(broken)

    def test_a_file_missing_the_companies_table_raises_not_defaults_to_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            no_table = Path(tmp) / "no-companies-table.toml"
            no_table.write_text('[meta]\ndecision = "x"\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_company_function_metric_map(no_table)

    def test_a_well_formed_non_empty_file_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            filled = Path(tmp) / "filled.toml"
            filled.write_text(
                '[companies."9001"]\n"运营" = ["示例指标A", "示例指标B"]\n',
                encoding="utf-8",
            )

            mapping = load_company_function_metric_map(filled)

            self.assertEqual(mapping, {"9001": {"运营": ("示例指标A", "示例指标B")}})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
