"""A 前缀角色到职能标签的映射（V-银河-11）。

映射规则来自产品负责人 2026-08-05、2026-08-08 的决定：抽成仓库内可维护的配置文件，
由产品负责人后期持续维护。实现不得对配置未覆盖的角色名做任何猜测。
"""

from __future__ import annotations

import unittest

from lingxi.adapters.role_function_map_file import (
    default_role_function_map_path,
    load_role_function_map,
)
from lingxi.core.permission.role_function import (
    UNMAPPED_LABEL,
    build_role_function_map,
    resolve_role_function,
    resolve_role_functions,
)


class RoleFunctionMappingTest(unittest.TestCase):
    """V-银河-11：只认配置，未覆盖的角色名保留原文并标「未映射」。"""

    def setUp(self) -> None:
        self.mapping = build_role_function_map({"roles": {"A运营": "运营", "A销售": "销售"}})

    def test_configured_role_is_mapped_to_its_function_label(self) -> None:
        resolved = resolve_role_function("A运营", self.mapping)

        self.assertTrue(resolved.mapped)
        self.assertEqual(resolved.function, "运营")
        self.assertEqual(resolved.role_name, "A运营")

    def test_unconfigured_role_keeps_its_original_name_and_is_marked_unmapped(self) -> None:
        resolved = resolve_role_function("A供应链", self.mapping)

        self.assertFalse(resolved.mapped)
        self.assertIsNone(resolved.function)
        self.assertEqual(resolved.role_name, "A供应链")
        self.assertEqual(resolved.label, UNMAPPED_LABEL)

    def test_the_letter_a_is_never_stripped_as_a_guess(self) -> None:
        # 不得把「去掉开头的 A」实现成通用规则：那是猜测，不是配置。
        for role_name in ("A供应链", "ABC分析", "A", "Auditor"):
            resolved = resolve_role_function(role_name, self.mapping)
            self.assertFalse(resolved.mapped, role_name)
            self.assertIsNone(resolved.function, role_name)
            self.assertEqual(resolved.role_name, role_name)

    def test_role_name_matching_is_exact_after_trimming_whitespace(self) -> None:
        self.assertTrue(resolve_role_function("  A运营 ", self.mapping).mapped)
        self.assertFalse(resolve_role_function("A运营（临时）", self.mapping).mapped)

    def test_resolving_a_role_list_keeps_order_and_marks_each_row(self) -> None:
        resolved = resolve_role_functions(("A销售", "临时角色", "A运营"), self.mapping)

        self.assertEqual([item.role_name for item in resolved], ["A销售", "临时角色", "A运营"])
        self.assertEqual([item.function for item in resolved], ["销售", None, "运营"])
        self.assertEqual([item.mapped for item in resolved], [True, False, True])

    def test_empty_role_name_is_rejected_rather_than_silently_mapped(self) -> None:
        with self.assertRaises(ValueError):
            resolve_role_function("   ", self.mapping)


class RoleFunctionMapDocumentTest(unittest.TestCase):
    def test_document_without_roles_table_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_role_function_map({"meta": {"version": 1}})

    def test_non_text_function_label_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_role_function_map({"roles": {"A运营": 1}})

    def test_blank_function_label_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_role_function_map({"roles": {"A运营": "  "}})

    def test_mapping_is_read_only(self) -> None:
        mapping = build_role_function_map({"roles": {"A运营": "运营"}})

        with self.assertRaises(TypeError):
            mapping["A销售"] = "销售"  # type: ignore[index]


class ShippedRoleFunctionMapFileTest(unittest.TestCase):
    """配置文件随包发布，产品负责人在仓库里维护它。"""

    def test_repository_config_file_loads_and_contains_the_decided_rules(self) -> None:
        mapping = load_role_function_map(default_role_function_map_path())

        expected = {
            # Issue #320 / 2026-08-26 裁定：银河「后台管理员」（role_id 513）独立职能，
            # 全公司×全指标，见 config/galaxy_role_function_map.toml 的专节说明。
            "后台管理员": "后台管理员",
            "A商务": "商务",
            "A海外CEO门户": "CEO",
            "A海外收视率样本户": "内容",
            "A海外本地员工收视率报表": "内容",
            "A财务": "财务",
            "A财务-折扣": "财务",
            "A运营": "运营",
            "A运营_CEO": "运营",
            "A运营_DTTDTH": "运营",
            "A运营_万村通": "运营",
            "A运营_呼叫中心": "运营",
            "A运营（所有）": "运营",
            "A运营_OTT专岗": "OTT",
            "A运营_OTT运营分析": "OTT",
            "A销售": "销售",
            "A销售_三级销售体系": "销售",
            "A销售_中文包": "销售",
            "A销售_渠道分析（海外）": "销售",
            "A销售_电视机": "销售",
            "A销售（所有）": "销售",
        }

        self.assertEqual(dict(mapping), expected)
        for unsupported in (
            "APP产品运营",
            "APP收入",
            "APP运营（总部）-收视与媒资",
            "A海外本地员工营业厅",
        ):
            self.assertNotIn(unsupported, mapping)

    def test_config_file_documents_how_to_maintain_it(self) -> None:
        text = default_role_function_map_path().read_text(encoding="utf-8")

        self.assertIn("维护", text)
        self.assertIn("未映射", text)

    def test_config_file_is_shipped_inside_the_python_package(self) -> None:
        path = default_role_function_map_path()

        self.assertEqual(path.parent.name, "config")
        self.assertEqual(path.parent.parent.name, "lingxi")


if __name__ == "__main__":
    unittest.main()
