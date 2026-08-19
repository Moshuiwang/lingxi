"""随包发布的「公司 + 职能 → 指标名」翻译映射配置文件（Issue #227）。

镜像 ``tests/test_galaxy_role_function.py`` 的 ``ShippedRoleFunctionMapFileTest``
写法。2026-08-19 产品负责人给出了七个职能各自对应的指标 ID，编排者按「同一职能全国
统一」机械展开进随包文件，本组断言把**那个决定**钉住：任何人改动映射内容却不同步改
这里，门禁就红。文件缺失或格式错误则仍然按配置错误处理（不静默退化）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lingxi.adapters.company_function_metric_map_file import (
    default_company_function_metric_map_path,
    load_company_function_metric_map,
)
from lingxi.adapters.role_function_map_file import load_role_function_map

#: 产品负责人 2026-08-19 给出的「职能 → 指标 ID 列表」。值是 ``metric_id``（权限发布表
#: permissions 列的权威结构，Issue #155），不是中文指标名。
OWNER_DECISION = {
    "CEO": (
        "sub_new_count",
        "sub_recharge_count",
        "sub_recharge_money",
        "sub_deduction_count",
        "sub_deduction_money",
        "exchange_rate",
        "vat_rate",
        "channel_rate",
        "channel_market_sharing",
    ),
    "OTT": ("exchange_rate", "vat_rate"),
    "内容": ("exchange_rate", "vat_rate", "channel_rate", "channel_market_sharing"),
    "商务": ("exchange_rate", "vat_rate"),
    "财务": (
        "sub_new_count",
        "sub_recharge_count",
        "sub_recharge_money",
        "sub_deduction_count",
        "sub_deduction_money",
        "exchange_rate",
        "vat_rate",
    ),
    "运营": (
        "sub_new_count",
        "sub_recharge_count",
        "sub_recharge_money",
        "sub_deduction_count",
        "sub_deduction_money",
        "exchange_rate",
        "vat_rate",
        "channel_rate",
        "channel_market_sharing",
    ),
    "销售": ("sub_new_count", "exchange_rate", "vat_rate"),
}

#: 2026-08-06 银河受控导出中实际出现的公司编号，外加「全非」通配键。
EXPECTED_COMPANY_KEYS = frozenset(
    [str(number) for number in range(1, 40)] + ["44", "50", "61", "49368", "*"]
)


class ShippedConfigFileTest(unittest.TestCase):
    """随包发布的那份文件：内容必须与产品负责人的决定逐字一致。"""

    def test_the_shipped_file_matches_the_owner_supplied_decision(self) -> None:
        mapping = load_company_function_metric_map(default_company_function_metric_map_path())

        for company, functions in mapping.items():
            for function, metrics in functions.items():
                self.assertIn(function, OWNER_DECISION, f"{company} 下出现未登记的职能")
                self.assertEqual(
                    tuple(metrics),
                    OWNER_DECISION[function],
                    f"公司 {company} 的职能 {function} 与产品负责人 2026-08-19 的决定不一致",
                )

    def test_every_company_key_carries_all_seven_functions(self) -> None:
        """「同一职能全国统一」的机械展开：每个公司键都必须写满七个职能。

        缺一条不是"这个人在这里没有这项指标"，而是翻译层对这个组合 fail-closed
        ——那会让真的持有该职能的用户在这个公司下发布不出去，且现象是静默跳过。
        """
        mapping = load_company_function_metric_map(default_company_function_metric_map_path())

        self.assertEqual(set(mapping), set(EXPECTED_COMPANY_KEYS))
        for company, functions in mapping.items():
            self.assertEqual(
                set(functions),
                set(OWNER_DECISION),
                f"公司 {company} 的职能集合不完整",
            )
        self.assertEqual(
            sum(len(functions) for functions in mapping.values()),
            len(EXPECTED_COMPANY_KEYS) * len(OWNER_DECISION),
        )

    def test_the_function_labels_come_from_the_role_function_map(self) -> None:
        """职能词表必须与银河角色映射的右值完全相同。

        角色映射新增一个职能而这里没跟上，那个职能的持有者会在翻译层 fail-closed；
        反过来这里多一个不存在的职能标签，则是永远查不到的死条目。
        """
        self.assertEqual(set(OWNER_DECISION), set(load_role_function_map().values()))

    def test_the_shipped_file_records_the_decision_and_its_boundaries(self) -> None:
        text = default_company_function_metric_map_path().read_text(encoding="utf-8")

        self.assertIn("2026-08-19", text)
        # 指标 ID 而非中文名的依据，以及清单来源的时效边界，都必须写在文件里——
        # 只活在 PR 正文里的说明，下一个维护者看不到。
        self.assertIn("metric_id", text)
        self.assertIn("list_metrics", text)
        self.assertIn("全国统一", text)

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
