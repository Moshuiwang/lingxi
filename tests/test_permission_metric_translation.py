"""「公司 + 职能 → 指标名」翻译层的纯逻辑断言（Issue #227）。

认领断言：

- 载体存在且可用（:func:`build_company_function_metric_map` 校验解析后的文档）；
- 未覆盖的「公司 + 职能」组合**失败关闭**——不猜、不回落成职能标签、不静默丢弃
  只翻译一部分（``UncoveredCoverageTest``）；
- 映射为**空**时维持现有硬闸——任何用户的任何组合都翻译不出来
  （``EmptyMappingIsLegalTest``）；
- 恒等序列化：次序、重复不影响结果字节（``TranslateOrderingTest``）；
- 通配（``"*"``）与具体公司键各自独立生效，互不覆盖（``WildcardTest``）。

真实映射内容（哪个公司的哪个职能对应哪些指标名）不在本文件——那是产品负责人才能
给的数据，见 ``lingxi/config/company_function_metric_map.toml`` 的模块注释。本文件
用到的公司 ID、职能标签与指标名全部是**测试夹具**，不对应任何真实公司、真实人员或
真实指标名。
"""

from __future__ import annotations

import unittest

from lingxi.core.permission.metric_translation import (
    ALL_COMPANIES_KEY,
    UncoveredPermissionCombination,
    build_company_function_metric_map,
    translate_company_functions,
)

COMPANY_A = "1011"
COMPANY_B = "1012"
FUNCTION_OPS = "运营"
FUNCTION_FINANCE = "财务"
METRIC_DAU = "示例指标-日活"
METRIC_REVENUE = "示例指标-收入"


class BuildMapValidationTest(unittest.TestCase):
    """:func:`build_company_function_metric_map` 的结构校验。"""

    def test_a_well_formed_document_parses_into_nested_tuples(self) -> None:
        document = {
            "companies": {
                COMPANY_A: {FUNCTION_OPS: [METRIC_DAU, METRIC_REVENUE]},
            }
        }

        result = build_company_function_metric_map(document)

        self.assertEqual(result, {COMPANY_A: {FUNCTION_OPS: (METRIC_DAU, METRIC_REVENUE)}})

    def test_an_empty_companies_table_is_legal(self) -> None:
        """空映射是合法内容（产品负责人尚未填入时的正常状态），不是错误。"""

        result = build_company_function_metric_map({"companies": {}})

        self.assertEqual(result, {})

    def test_missing_companies_table_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_company_function_metric_map({})

    def test_a_non_mapping_companies_table_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_company_function_metric_map({"companies": ["not", "a", "mapping"]})

    def test_an_empty_company_key_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_company_function_metric_map({"companies": {" ": {FUNCTION_OPS: [METRIC_DAU]}}})

    def test_a_non_mapping_function_table_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_company_function_metric_map({"companies": {COMPANY_A: [METRIC_DAU]}})

    def test_an_empty_function_label_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_company_function_metric_map({"companies": {COMPANY_A: {"": [METRIC_DAU]}}})

    def test_an_empty_metric_list_is_rejected(self) -> None:
        """空列表在写侧不合法：不覆盖应该表现为"缺这个条目"，不是"给一个空列表"。"""

        with self.assertRaises(ValueError):
            build_company_function_metric_map({"companies": {COMPANY_A: {FUNCTION_OPS: []}}})

    def test_a_non_list_metric_value_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_company_function_metric_map({"companies": {COMPANY_A: {FUNCTION_OPS: METRIC_DAU}}})

    def test_a_none_metric_entry_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_company_function_metric_map(
                {"companies": {COMPANY_A: {FUNCTION_OPS: [METRIC_DAU, None]}}}
            )

    def test_an_empty_string_metric_entry_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_company_function_metric_map({"companies": {COMPANY_A: {FUNCTION_OPS: [""]}}})

    def test_duplicate_metric_entries_are_kept_as_is(self) -> None:
        """校验层不去重——去重是翻译层（:func:`translate_company_functions`）的事，
        校验层只挡形状错误，不悄悄改写内容。"""

        result = build_company_function_metric_map(
            {"companies": {COMPANY_A: {FUNCTION_OPS: [METRIC_DAU, METRIC_DAU]}}}
        )

        self.assertEqual(result[COMPANY_A][FUNCTION_OPS], (METRIC_DAU, METRIC_DAU))

    def test_the_wildcard_company_key_is_accepted_verbatim(self) -> None:
        result = build_company_function_metric_map(
            {"companies": {"*": {FUNCTION_OPS: [METRIC_DAU]}}}
        )

        self.assertIn(ALL_COMPANIES_KEY, result)


class UncoveredCoverageTest(unittest.TestCase):
    """未覆盖组合：fail-closed，不猜、不静默丢弃。"""

    def test_a_fully_covered_combination_translates(self) -> None:
        result = translate_company_functions(
            companies=(COMPANY_A,),
            functions=(FUNCTION_OPS,),
            all_companies=False,
            mapping={COMPANY_A: {FUNCTION_OPS: (METRIC_DAU,)}},
        )

        self.assertEqual(result, {COMPANY_A: (METRIC_DAU,)})

    def test_an_entirely_uncovered_company_fails_closed(self) -> None:
        with self.assertRaises(UncoveredPermissionCombination) as ctx:
            translate_company_functions(
                companies=(COMPANY_A,),
                functions=(FUNCTION_OPS,),
                all_companies=False,
                mapping={},
            )
        self.assertEqual(ctx.exception.missing, ((COMPANY_A, FUNCTION_OPS),))
        self.assertTrue(ctx.exception.mapping_is_empty, "整份映射为空，属「未配置」")

    def test_an_uncovered_function_within_a_known_company_fails_closed(self) -> None:
        """公司有映射，但这个职能没有——同样失败关闭，不回落成"这个公司下没有这个职能
        对应的指标"这种沉默的猜测。映射本身**不为空**，因此这是「配了但没覆盖到」，
        不是「未配置」——两种运维状态必须可分辨。"""

        with self.assertRaises(UncoveredPermissionCombination) as ctx:
            translate_company_functions(
                companies=(COMPANY_A,),
                functions=(FUNCTION_FINANCE,),
                all_companies=False,
                mapping={COMPANY_A: {FUNCTION_OPS: (METRIC_DAU,)}},
            )
        self.assertFalse(ctx.exception.mapping_is_empty, "映射非空，属「配了但未覆盖」")

    def test_partial_coverage_across_two_companies_fails_the_whole_translation(self) -> None:
        """两家公司，只有一家有映射——**整体**失败，不产出"只翻译出那一家"的部分结果。"""

        with self.assertRaises(UncoveredPermissionCombination) as ctx:
            translate_company_functions(
                companies=(COMPANY_A, COMPANY_B),
                functions=(FUNCTION_OPS,),
                all_companies=False,
                mapping={COMPANY_A: {FUNCTION_OPS: (METRIC_DAU,)}},
            )
        self.assertEqual(ctx.exception.missing, ((COMPANY_B, FUNCTION_OPS),))
        self.assertFalse(ctx.exception.mapping_is_empty)

    def test_an_uncovered_translation_never_falls_back_to_the_function_label(self) -> None:
        """否定断言：一个职能标签恰好在指标名词表以外时，翻译不出来就是失败关闭，
        不会有任何路径让职能标签本身冒充指标名溜进结果——本函数只有两种出口：
        抛 :class:`UncoveredPermissionCombination`，或者返回完全翻译好的结果。"""

        with self.assertRaises(UncoveredPermissionCombination):
            translate_company_functions(
                companies=(COMPANY_A,),
                functions=(FUNCTION_OPS,),
                all_companies=False,
                mapping={},
            )


class EmptyMappingIsLegalTest(unittest.TestCase):
    """映射为空时维持现有硬闸：任何用户的任何组合都翻译不出来（Issue #227 承诺）。"""

    def test_an_empty_mapping_uncoveres_every_combination(self) -> None:
        with self.assertRaises(UncoveredPermissionCombination) as ctx:
            translate_company_functions(
                companies=(COMPANY_A, COMPANY_B),
                functions=(FUNCTION_OPS, FUNCTION_FINANCE),
                all_companies=False,
                mapping={},
            )
        self.assertEqual(
            set(ctx.exception.missing),
            {
                (COMPANY_A, FUNCTION_OPS),
                (COMPANY_A, FUNCTION_FINANCE),
                (COMPANY_B, FUNCTION_OPS),
                (COMPANY_B, FUNCTION_FINANCE),
            },
        )
        self.assertTrue(ctx.exception.mapping_is_empty)

    def test_an_empty_mapping_uncoveres_the_wildcard_scope_too(self) -> None:
        with self.assertRaises(UncoveredPermissionCombination) as ctx:
            translate_company_functions(
                companies=(), functions=(FUNCTION_OPS,), all_companies=True, mapping={}
            )
        self.assertTrue(ctx.exception.mapping_is_empty)

    def test_a_non_empty_mapping_that_still_misses_the_combination_is_distinguishable(self) -> None:
        """映射**非空**（已经有别的公司+职能条目），但恰好没覆盖这一次要用的组合——
        这条状态必须能与"整份映射为空"区分开，运维据此判断是"还没开始填"还是
        "已经在填、还差几条"。"""

        with self.assertRaises(UncoveredPermissionCombination) as ctx:
            translate_company_functions(
                companies=(COMPANY_A,),
                functions=(FUNCTION_OPS,),
                all_companies=False,
                mapping={COMPANY_B: {FUNCTION_FINANCE: (METRIC_REVENUE,)}},
            )
        self.assertFalse(ctx.exception.mapping_is_empty)


class WildcardTest(unittest.TestCase):
    """通配（``"*"``）与具体公司键各自独立生效。"""

    def test_all_companies_only_consults_the_wildcard_key(self) -> None:
        result = translate_company_functions(
            companies=(),
            functions=(FUNCTION_OPS,),
            all_companies=True,
            mapping={"*": {FUNCTION_OPS: (METRIC_DAU,)}, COMPANY_A: {FUNCTION_OPS: (METRIC_REVENUE,)}},
        )

        self.assertEqual(result, {"*": (METRIC_DAU,)})

    def test_a_specific_company_mapping_does_not_leak_into_the_wildcard(self) -> None:
        """否定断言：即便具体公司键有映射，全非通配也不会"顺手"用它兜底——
        通配必须有自己显式的 ``"*"`` 条目。"""

        with self.assertRaises(UncoveredPermissionCombination):
            translate_company_functions(
                companies=(),
                functions=(FUNCTION_OPS,),
                all_companies=True,
                mapping={COMPANY_A: {FUNCTION_OPS: (METRIC_DAU,)}},
            )

    def test_companies_can_translate_to_different_metrics(self) -> None:
        """正面断言：同一职能在不同公司下允许翻译出不同的指标名——
        这是「公司 + 职能」联合键存在的意义。"""

        result = translate_company_functions(
            companies=(COMPANY_A, COMPANY_B),
            functions=(FUNCTION_OPS,),
            all_companies=False,
            mapping={
                COMPANY_A: {FUNCTION_OPS: (METRIC_DAU,)},
                COMPANY_B: {FUNCTION_OPS: (METRIC_REVENUE,)},
            },
        )

        self.assertEqual(result, {COMPANY_A: (METRIC_DAU,), COMPANY_B: (METRIC_REVENUE,)})


class TranslateOrderingTest(unittest.TestCase):
    """恒等序列化的上游保证：次序与重复不影响结果。"""

    def test_duplicate_functions_do_not_duplicate_metrics(self) -> None:
        result = translate_company_functions(
            companies=(COMPANY_A,),
            functions=(FUNCTION_OPS, FUNCTION_OPS),
            all_companies=False,
            mapping={COMPANY_A: {FUNCTION_OPS: (METRIC_DAU,)}},
        )

        self.assertEqual(result, {COMPANY_A: (METRIC_DAU,)})

    def test_the_metric_list_is_sorted_and_deduplicated_across_functions(self) -> None:
        result = translate_company_functions(
            companies=(COMPANY_A,),
            functions=(FUNCTION_OPS, FUNCTION_FINANCE),
            all_companies=False,
            mapping={
                COMPANY_A: {
                    FUNCTION_OPS: (METRIC_DAU, METRIC_REVENUE),
                    FUNCTION_FINANCE: (METRIC_REVENUE,),
                }
            },
        )

        self.assertEqual(result[COMPANY_A], tuple(sorted({METRIC_DAU, METRIC_REVENUE})))

    def test_function_order_does_not_change_the_result(self) -> None:
        forward = translate_company_functions(
            companies=(COMPANY_A,),
            functions=(FUNCTION_OPS, FUNCTION_FINANCE),
            all_companies=False,
            mapping={
                COMPANY_A: {
                    FUNCTION_OPS: (METRIC_DAU,),
                    FUNCTION_FINANCE: (METRIC_REVENUE,),
                }
            },
        )
        reversed_input = translate_company_functions(
            companies=(COMPANY_A,),
            functions=(FUNCTION_FINANCE, FUNCTION_OPS),
            all_companies=False,
            mapping={
                COMPANY_A: {
                    FUNCTION_OPS: (METRIC_DAU,),
                    FUNCTION_FINANCE: (METRIC_REVENUE,),
                }
            },
        )

        self.assertEqual(forward, reversed_input)

    def test_no_companies_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            translate_company_functions(
                companies=(), functions=(FUNCTION_OPS,), all_companies=False, mapping={}
            )

    def test_no_functions_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            translate_company_functions(
                companies=(COMPANY_A,), functions=(), all_companies=False, mapping={}
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
