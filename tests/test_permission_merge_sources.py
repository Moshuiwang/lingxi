"""``core/permission/merge_sources.py`` 的纯逻辑测试（Issue #319 S-P-3）。

不需要数据库、不需要任何调用点——只测 :func:`merge_permission_sources` 这一个纯
函数。两个调用点各自的接线测试（真实的 ``PermissionRefreshDuty``/
``AutoOnboardingRunner`` 装配 + 审计事件）分别在
``tests/test_permission_refresh_duty.py::LocalOverrideMergeTest`` 与
``tests/test_onboarding_runner.py::LocalOverrideMergeTests``。

三处变异锚点已实测验红、验证后原样还原（S-P-3 实施卡要求，结果登记在此，不重复
登记在别处）：

1. **把 ``−`` 改成 ``∪``**：临时把 ``values -= set(local_suppressions.get(key, ()))``
   改成 ``values |= set(local_suppressions.get(key, ()))`` 后，
   ``UnionThenSubtractTests.test_suppression_wins_even_over_a_galaxy_granted_metric``
   由绿转红（被抑制的指标反而出现在结果里）；改回后复绿。
2. **删掉通配跳过审计的判定**：临时把 ``if local is not None and local.grants:``/
   ``if local is not None and local.suppressions:`` 两行的条件各自改成恒 ``False``
   （不再登记任何 skip_reasons）后，``WildcardRoleTests`` 的两个正面断言
   （``test_wildcard_skips_grant_with_a_reason``/
   ``test_wildcard_skips_suppress_with_a_reason``）由绿转红（``skipped_reasons``
   变回空元组）；改回后复绿。
3. **legacy 参数改成参与通配下的合并**（临时删掉通配分支里"不参与 legacy"的隐含
   行为，让通配分支也去合并 ``legacy``）：
   ``LegacyIdentityTests.test_legacy_is_ignored_under_the_wildcard_too`` 由绿转红
   （通配下传入 ``legacy`` 会在结果里多出一个具体公司键，逐字节比对失败）；
   改回后复绿。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from lingxi.core.permission.local_override import (
    LocalPermissionOverrideEntry,
    OverrideDirection,
    ResolvedLocalOverrides,
    resolve_local_overrides,
)
from lingxi.core.permission.merge_sources import (
    ALL_COMPANIES_KEY,
    REASON_GRANT_REDUNDANT_WILDCARD,
    REASON_SUPPRESS_INAPPLICABLE_WILDCARD,
    MergedPermissionSources,
    merge_permission_sources,
)

_NOW = datetime(2026, 8, 27, 3, 0, 0, tzinfo=timezone.utc)


def _entry(
    *,
    user_id: str = "usr_1",
    direction: OverrideDirection = OverrideDirection.GRANT,
    company_id: str = "1011",
    metric_name: str = "日活",
) -> LocalPermissionOverrideEntry:
    return LocalPermissionOverrideEntry(
        user_id=user_id,
        direction=direction,
        company_id=company_id,
        metric_name=metric_name,
        reason="测试夹具",
        initiated_by_open_id="ou_admin",
        pending_action_id="pac_1",
        created_at=_NOW,
    )


def _resolved(*entries: LocalPermissionOverrideEntry) -> ResolvedLocalOverrides:
    return resolve_local_overrides(user_id="usr_1", entries=entries)


class UnionThenSubtractTests(unittest.TestCase):
    """真实权限 = (银河 ∪ 本地授权 ∪ 存量沿用) − 本地抑制（非通配分支）。"""

    def test_local_grant_is_unioned_in(self) -> None:
        local = _resolved(_entry(metric_name="收入"))

        result = merge_permission_sources(galaxy={"1011": ("日活",)}, local=local)

        self.assertEqual(result.permissions, {"1011": ("收入", "日活")})
        self.assertEqual(result.skipped_reasons, ())

    def test_local_grant_can_introduce_a_company_the_galaxy_side_never_had(self) -> None:
        """本地授权可以点出一个银河这一侧完全没有覆盖到的公司——这正是"本地覆盖"
        存在的意义：在银河权限之外追加一条精确授权。"""

        local = _resolved(_entry(company_id="1099", metric_name="收入"))

        result = merge_permission_sources(galaxy={"1011": ("日活",)}, local=local)

        self.assertEqual(result.permissions, {"1011": ("日活",), "1099": ("收入",)})

    def test_suppression_wins_even_over_a_galaxy_granted_metric(self) -> None:
        """抑制优先否定用例：被抑制的指标名来自**银河**这一侧（不是本地授权自己
        加的那条），减法必须作用于并集之后的整体结果。变异锚点①。"""

        local = _resolved(_entry(direction=OverrideDirection.SUPPRESS, metric_name="日活"))

        result = merge_permission_sources(
            galaxy={"1011": ("日活", "收入")}, local=local
        )

        self.assertEqual(result.permissions, {"1011": ("收入",)})

    def test_a_key_suppressed_to_nothing_is_dropped_not_written_as_an_empty_list(self) -> None:
        result = merge_permission_sources(
            galaxy={"1011": ("日活",), "1012": ("收入",)},
            local=_resolved(_entry(direction=OverrideDirection.SUPPRESS, metric_name="日活")),
        )

        self.assertEqual(result.permissions, {"1012": ("收入",)})
        self.assertNotIn("1011", result.permissions, "被抑制到空的公司键必须被丢弃，不写空列表")

    def test_same_key_grant_and_suppress_never_coexist(self) -> None:
        """同键 grant+suppress=不出现：``resolve_local_overrides`` 已经把这条规则
        钉死（suppress 赢，键从 ``grants`` 里剔除），本函数只是复用那个已经解决好
        的结果——这里证明合并层不会意外让它重新出现。"""

        local = _resolved(
            _entry(direction=OverrideDirection.GRANT, metric_name="日活"),
            _entry(direction=OverrideDirection.SUPPRESS, metric_name="日活"),
        )

        result = merge_permission_sources(galaxy={"1011": ()}, local=local)

        self.assertNotIn("1011", result.permissions)

    def test_output_values_are_sorted_and_deduplicated(self) -> None:
        local = _resolved(_entry(metric_name="日活"))

        result = merge_permission_sources(galaxy={"1011": ("收入", "日活")}, local=local)

        self.assertEqual(result.permissions["1011"], ("收入", "日活"))


class NoLocalSourceIsIdentityTests(unittest.TestCase):
    """``local=None``：合并结果与 ``galaxy`` 逐字节相同（store 未装配 / 读取失败后
    调用方降级的哨兵值）。"""

    def test_none_is_identity(self) -> None:
        result = merge_permission_sources(galaxy={"1011": ("收入", "日活")}, local=None)

        self.assertEqual(result.permissions, {"1011": ("收入", "日活")})
        self.assertEqual(result.skipped_reasons, ())

    def test_an_empty_but_present_resolved_overrides_is_also_identity(self) -> None:
        """``local`` 非 ``None`` 但内容为空（该用户当前没有任何生效条目）时结果同样
        恒等——只是走的是"参与合并、恰好没有任何贡献"这条路径，不是"跳过"。"""

        result = merge_permission_sources(
            galaxy={"1011": ("日活",)}, local=_resolved()
        )

        self.assertEqual(result.permissions, {"1011": ("日活",)})


class LegacyIdentityTests(unittest.TestCase):
    """``legacy=None`` 恒等（S-P-3 只定签名，数据源留给 S-P-2 批二）。"""

    def test_legacy_none_is_identity(self) -> None:
        result = merge_permission_sources(
            galaxy={"1011": ("日活",)}, local=_resolved(_entry(metric_name="收入")), legacy=None
        )

        self.assertEqual(result.permissions, {"1011": ("收入", "日活")})

    def test_legacy_participates_in_the_union_when_provided(self) -> None:
        """非 ``None`` 时的正面断言：``legacy`` 与本地授权对称——参与并集，不参与
        抑制豁免（被本地抑制命中同样会被减掉）。"""

        result = merge_permission_sources(
            galaxy={"1011": ("日活",)},
            local=None,
            legacy={"1011": ("存量指标",), "1012": ("另一存量指标",)},
        )

        self.assertEqual(
            result.permissions, {"1011": ("存量指标", "日活"), "1012": ("另一存量指标",)}
        )

    def test_legacy_is_ignored_under_the_wildcard_too(self) -> None:
        """通配下 ``legacy`` 同样不参与合并——理由与本地授权相同：往通配映射里
        追加一个具体公司键会让读侧 ``lookup_metrics`` 对那个公司不再回退通配，
        造成一处极难发现的窄范围回归（模块文档「通配角」一节）。变异锚点③。"""

        result = merge_permission_sources(
            galaxy={ALL_COMPANIES_KEY: ("全部指标",)},
            local=None,
            legacy={"1011": ("存量指标",)},
        )

        self.assertEqual(result.permissions, {ALL_COMPANIES_KEY: ("全部指标",)})
        self.assertNotIn("1011", result.permissions, "通配下不得凭空长出具体公司键")


class WildcardRoleTests(unittest.TestCase):
    """通配角 v1 语义（编排者裁定）：``galaxy`` 出现 ``"*"`` 键（当前唯一形态=513
    后台管理员，``all_companies=True``）时，本地授权与抑制整体不参与合并，产出与
    ``galaxy`` 逐字节相同；跳过的理由各自登记，供调用点各自审计。"""

    def test_wildcard_with_no_local_source_is_untouched_and_silent(self) -> None:
        result = merge_permission_sources(
            galaxy={ALL_COMPANIES_KEY: ("全部指标",)}, local=None
        )

        self.assertEqual(result.permissions, {ALL_COMPANIES_KEY: ("全部指标",)})
        self.assertEqual(result.skipped_reasons, (), "没有本地源时不该凭空登记跳过原因")

    def test_wildcard_skips_grant_with_a_reason(self) -> None:
        """变异锚点②（正面半边）。"""

        local = _resolved(_entry(metric_name="额外授权"))

        result = merge_permission_sources(
            galaxy={ALL_COMPANIES_KEY: ("全部指标",)}, local=local
        )

        self.assertEqual(result.permissions, {ALL_COMPANIES_KEY: ("全部指标",)}, "通配下 grant 是冗余")
        self.assertIn(REASON_GRANT_REDUNDANT_WILDCARD, result.skipped_reasons)

    def test_wildcard_skips_suppress_with_a_reason(self) -> None:
        """变异锚点②（正面半边）：``suppress`` 拦不住通配——抑制的是具体公司键，
        通配走的是 ``"*"`` 键，两者互不相交。"""

        local = _resolved(_entry(direction=OverrideDirection.SUPPRESS, metric_name="全部指标"))

        result = merge_permission_sources(
            galaxy={ALL_COMPANIES_KEY: ("全部指标",)}, local=local
        )

        self.assertEqual(
            result.permissions, {ALL_COMPANIES_KEY: ("全部指标",)}, "通配下 suppress 拦不住通配"
        )
        self.assertIn(REASON_SUPPRESS_INAPPLICABLE_WILDCARD, result.skipped_reasons)

    def test_wildcard_with_both_grant_and_suppress_reports_both_reasons(self) -> None:
        local = _resolved(
            _entry(metric_name="额外授权"),
            _entry(direction=OverrideDirection.SUPPRESS, metric_name="全部指标"),
        )

        result = merge_permission_sources(
            galaxy={ALL_COMPANIES_KEY: ("全部指标",)}, local=local
        )

        self.assertEqual(
            set(result.skipped_reasons),
            {REASON_GRANT_REDUNDANT_WILDCARD, REASON_SUPPRESS_INAPPLICABLE_WILDCARD},
        )

    def test_a_normal_user_is_unaffected_by_the_wildcard_rule(self) -> None:
        """否定断言：普通用户（``galaxy`` 没有 ``"*"`` 键）不受通配规则影响，本地
        授权/抑制照常按非通配分支的并集/减法生效。"""

        local = _resolved(_entry(metric_name="收入"))

        result = merge_permission_sources(galaxy={"1011": ("日活",)}, local=local)

        self.assertEqual(result.permissions, {"1011": ("收入", "日活")})
        self.assertEqual(result.skipped_reasons, ())


class ResultTypeTests(unittest.TestCase):
    def test_result_is_a_merged_permission_sources_instance(self) -> None:
        result = merge_permission_sources(galaxy={"1011": ("日活",)}, local=None)

        self.assertIsInstance(result, MergedPermissionSources)


if __name__ == "__main__":
    unittest.main()
