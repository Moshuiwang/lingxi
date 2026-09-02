"""``core/permission/legacy_diff.py`` 的纯逻辑用例（rc25 S-1，Issue #540）。

不连数据库、不发请求：只钉旧行形状判定、映射全部指标并集、差集计划的三种形状与两种
银河通配形态、以及「全部」组随映射补齐的缺项计算。接线断言（开通链真的在零银河判定
之前调用导入口、fail-closed 零写入）在 ``tests/test_onboarding_runner.py::
LegacyPermissionImportTests``；落库原子性与幂等在 ``tests/test_local_permission_postgres.py``。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from lingxi.core.permission.legacy_diff import (
    ALL_SCOPE_EXPLICIT_POSITION_NAME,
    ALL_SCOPE_POSITION_NAME,
    REASON_ALL_METRICS_UNAVAILABLE,
    REASON_NOTHING_TO_IMPORT,
    REASON_SHAPE_UNSUPPORTED,
    REASON_WILDCARD_GALAXY_CURRENT,
    SHAPE_ALL_SCOPE_EXPLICIT,
    SHAPE_FULL_WILDCARD,
    SHAPE_SPECIFIC,
    SHAPE_UNSUPPORTED_WILDCARD,
    all_metrics,
    classify_legacy_permissions,
    compute_company_diff,
    missing_all_scope_metrics,
    plan_legacy_import,
)
from lingxi.core.permission.local_override import LocalPermissionOverrideEntry, OverrideDirection

MAPPING = {
    "88": {"运营": ("m1", "m2"), "财务": ("m3",)},
    "99": {"运营": ("m1",)},
    "*": {"后台管理员": ("m1", "m2", "m3")},
}


class ClassifyTests(unittest.TestCase):
    def test_specific_shapes(self) -> None:
        self.assertEqual(classify_legacy_permissions({}), SHAPE_SPECIFIC)
        self.assertEqual(classify_legacy_permissions({"88": ("m1",), "40": ("m2",)}), SHAPE_SPECIFIC)

    def test_full_wildcard_is_exactly_star_star(self) -> None:
        self.assertEqual(classify_legacy_permissions({"*": ("*",)}), SHAPE_FULL_WILDCARD)
        self.assertEqual(classify_legacy_permissions({"*": ("*", "*")}), SHAPE_FULL_WILDCARD)
        # 附带具体公司键仍是「全部」形状：具体键按差集另落无组行。
        self.assertEqual(classify_legacy_permissions({"*": ("*",), "40": ("m1",)}), SHAPE_FULL_WILDCARD)

    def test_explicit_all_scope_list(self) -> None:
        self.assertEqual(classify_legacy_permissions({"*": ("m1", "m2")}), SHAPE_ALL_SCOPE_EXPLICIT)
        self.assertEqual(classify_legacy_permissions({"*": ()}), SHAPE_ALL_SCOPE_EXPLICIT)

    def test_other_star_shapes_are_unsupported(self) -> None:
        self.assertEqual(classify_legacy_permissions({"*": ("*", "m1")}), SHAPE_UNSUPPORTED_WILDCARD)
        self.assertEqual(classify_legacy_permissions({"88": ("*",)}), SHAPE_UNSUPPORTED_WILDCARD)
        self.assertEqual(classify_legacy_permissions({"*": ("m1",), "88": ("*",)}), SHAPE_UNSUPPORTED_WILDCARD)

    def test_blank_key_or_metric_is_unparseable(self) -> None:
        with self.assertRaises(ValueError):
            classify_legacy_permissions({" ": ("m1",)})
        with self.assertRaises(ValueError):
            classify_legacy_permissions({"88": ("m1", "  ")})


class AllMetricsTests(unittest.TestCase):
    def test_union_over_every_company_and_function_sorted_deduplicated(self) -> None:
        self.assertEqual(all_metrics(MAPPING), ("m1", "m2", "m3"))
        self.assertEqual(all_metrics({}), ())
        self.assertEqual(all_metrics({"88": {"运营": ("b", "a", "a")}}), ("a", "b"))


class CompanyDiffTests(unittest.TestCase):
    """脚本沿用的保守口径：银河出现 ``"*"`` 整份恒空；其余按公司键求差。"""

    def test_subtracts_per_company_and_drops_empty_keys(self) -> None:
        diff = compute_company_diff({"88": ("m1", "m2"), "99": ("m1",)}, {"88": ("m1",), "99": ("m1",)})
        self.assertEqual(diff, {"88": ("m2",)})

    def test_any_galaxy_wildcard_yields_empty_diff(self) -> None:
        self.assertEqual(compute_company_diff({"88": ("m1",)}, {"*": ("x",)}), {})


class PlanTests(unittest.TestCase):
    def test_specific_rows_import_only_the_difference_without_a_group(self) -> None:
        plan = plan_legacy_import(
            legacy={"88": ("m1", "m9"), "99": ("m1",)},
            galaxy_current={"88": ("m1",), "99": ("m1",)},
            full_access_wildcard=False,
            mapping=MAPPING,
        )
        self.assertEqual(plan.shape, SHAPE_SPECIFIC)
        self.assertEqual(plan.pairs, (("88", "m9"),))
        self.assertEqual(plan.all_scope_metrics, ())
        self.assertEqual(plan.skipped_reasons, ())
        self.assertEqual(plan.unmapped_companies_kept, 0)
        self.assertFalse(plan.nothing_to_import)

    def test_unmapped_companies_are_kept_and_counted(self) -> None:
        """PM「本地是本地的」：映射外公司（40–43）照导入，只计数供审计。"""

        plan = plan_legacy_import(
            legacy={"40": ("m1",), "43": ("m2",)},
            galaxy_current={"88": ("m1",)},
            full_access_wildcard=False,
            mapping=MAPPING,
        )
        self.assertEqual(plan.pairs, (("40", "m1"), ("43", "m2")))
        self.assertEqual(plan.unmapped_companies_kept, 2)

    def test_full_wildcard_expands_to_every_mapped_metric_under_star(self) -> None:
        plan = plan_legacy_import(
            legacy={"*": ("*",)}, galaxy_current={"88": ("m1",)}, full_access_wildcard=False, mapping=MAPPING
        )
        self.assertEqual(plan.shape, SHAPE_FULL_WILDCARD)
        self.assertEqual(plan.all_scope_metrics, ("m1", "m2", "m3"))
        self.assertEqual(plan.pairs, ())

    def test_full_wildcard_with_an_extra_company_keeps_only_metrics_outside_the_group(self) -> None:
        plan = plan_legacy_import(
            legacy={"*": ("*",), "40": ("m1", "x9")},
            galaxy_current={},
            full_access_wildcard=False,
            mapping=MAPPING,
        )
        self.assertEqual(plan.all_scope_metrics, ("m1", "m2", "m3"))
        self.assertEqual(plan.pairs, (("40", "x9"),), "组已覆盖的指标不再逐公司落行")
        self.assertEqual(plan.unmapped_companies_kept, 1)

    def test_explicit_all_scope_list_becomes_the_group_as_listed(self) -> None:
        plan = plan_legacy_import(
            legacy={"*": ("m2", "m1")}, galaxy_current={"88": ("m1",)}, full_access_wildcard=False, mapping=MAPPING
        )
        self.assertEqual(plan.shape, SHAPE_ALL_SCOPE_EXPLICIT)
        self.assertEqual(plan.all_scope_metrics, ("m1", "m2"), "公司 * 保留、指标按表中列出的")
        self.assertEqual(plan.all_scope_position_name, ALL_SCOPE_EXPLICIT_POSITION_NAME)

    def test_full_wildcard_group_carries_the_auto_expanding_label(self) -> None:
        plan = plan_legacy_import(legacy={"*": ("*",)}, galaxy_current={}, full_access_wildcard=False, mapping=MAPPING)
        self.assertEqual(plan.all_scope_position_name, ALL_SCOPE_POSITION_NAME)

    def test_limited_galaxy_wildcard_subtracts_the_star_list_from_group_and_pairs(self) -> None:
        """银河有限 ``*``（v2）：合并层会把本地 grant 指标并进 ``"*"`` 清单，因此组与具体键
        都只导银河 ``"*"`` 清单之外的部分。"""

        plan = plan_legacy_import(
            legacy={"*": ("*",), "88": ("m1", "x9")},
            galaxy_current={"*": ("m1", "m2")},
            full_access_wildcard=False,
            mapping=MAPPING,
        )
        self.assertEqual(plan.all_scope_metrics, ("m3",))
        self.assertEqual(plan.pairs, (("88", "x9"),))

    def test_true_full_access_galaxy_wildcard_imports_nothing(self) -> None:
        plan = plan_legacy_import(
            legacy={"*": ("*",), "88": ("x9",)},
            galaxy_current={"*": ("m1", "m2", "m3")},
            full_access_wildcard=True,
            mapping=MAPPING,
        )
        self.assertTrue(plan.nothing_to_import)
        self.assertEqual(plan.skipped_reasons, (REASON_WILDCARD_GALAXY_CURRENT,))

    def test_unsupported_shape_is_reported_not_partially_imported(self) -> None:
        plan = plan_legacy_import(
            legacy={"*": ("*", "m1"), "88": ("m9",)},
            galaxy_current={},
            full_access_wildcard=False,
            mapping=MAPPING,
        )
        self.assertEqual(plan.shape, SHAPE_UNSUPPORTED_WILDCARD)
        self.assertEqual(plan.skipped_reasons, (REASON_SHAPE_UNSUPPORTED,))
        self.assertTrue(plan.nothing_to_import, "不受支持的形状不得产出任何部分结果")

    def test_full_wildcard_with_an_empty_mapping_is_flagged_unavailable(self) -> None:
        plan = plan_legacy_import(
            legacy={"*": ("*",)}, galaxy_current={}, full_access_wildcard=False, mapping={}
        )
        self.assertIn(REASON_ALL_METRICS_UNAVAILABLE, plan.skipped_reasons)
        self.assertTrue(plan.nothing_to_import)

    def test_nothing_to_import_when_galaxy_already_covers_the_legacy_row(self) -> None:
        plan = plan_legacy_import(
            legacy={"88": ("m1",)}, galaxy_current={"88": ("m1", "m2")}, full_access_wildcard=False, mapping=MAPPING
        )
        self.assertTrue(plan.nothing_to_import)
        self.assertEqual(plan.skipped_reasons, (REASON_NOTHING_TO_IMPORT,))

    def test_zero_galaxy_imports_the_whole_legacy_row(self) -> None:
        plan = plan_legacy_import(
            legacy={"88": ("m2", "m1")}, galaxy_current={}, full_access_wildcard=True, mapping=MAPPING
        )
        self.assertEqual(plan.pairs, (("88", "m1"), ("88", "m2")))


def _entry(
    *,
    company_id: str = "*",
    metric_name: str = "m1",
    position_name: str | None = ALL_SCOPE_POSITION_NAME,
    group_id: str | None = "lpg_1",
    direction: OverrideDirection = OverrideDirection.GRANT,
) -> LocalPermissionOverrideEntry:
    return LocalPermissionOverrideEntry(
        user_id="usr_1",
        direction=direction,
        company_id=company_id,
        metric_name=metric_name,
        reason="2.0 迁移导入",
        initiated_by_open_id="lingxi:legacy_import_2_0",
        pending_action_id="pac_1",
        created_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
        position_name=position_name,
        company_scope="*" if group_id else None,
        permission_group_id=group_id,
    )


class MissingAllScopeMetricsTests(unittest.TestCase):
    def test_reports_metrics_the_mapping_has_but_the_group_lacks(self) -> None:
        entries = (_entry(metric_name="m1"), _entry(metric_name="m2"))
        self.assertEqual(missing_all_scope_metrics(entries, MAPPING), {"lpg_1": ("m3",)})

    def test_complete_group_reports_nothing(self) -> None:
        entries = tuple(_entry(metric_name=metric) for metric in ("m1", "m2", "m3"))
        self.assertEqual(missing_all_scope_metrics(entries, MAPPING), {})

    def test_only_all_scope_groups_participate(self) -> None:
        entries = (
            _entry(company_id="88", position_name="A运营", group_id="lpg_position"),  # 职位组
            _entry(metric_name="m1", position_name=None, group_id=None),  # 历史无组 "*" 行
            _entry(metric_name="m1", direction=OverrideDirection.SUPPRESS),  # 抑制
        )
        self.assertEqual(missing_all_scope_metrics(entries, MAPPING), {})

    def test_an_explicit_list_group_is_never_expanded(self) -> None:
        """独立审核 P1：``{"*":[显式列表]}`` 的组语义是「就这几个指标」，映射里多出来的
        指标不得自动补进去——否则显式列表用户次日就静默拿到映射全部指标。"""

        entries = (_entry(metric_name="m1", position_name=ALL_SCOPE_EXPLICIT_POSITION_NAME, group_id="lpg_explicit"),)
        self.assertEqual(missing_all_scope_metrics(entries, MAPPING), {})

    def test_no_entries_means_no_groups(self) -> None:
        self.assertEqual(missing_all_scope_metrics((), MAPPING), {})


if __name__ == "__main__":
    unittest.main()
