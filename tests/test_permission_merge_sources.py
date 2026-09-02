"""``core/permission/merge_sources.py`` 的纯逻辑测试（Issue #319 S-P-3；
``LimitedWildcardTests``/``PublishRowReadbackSelfProofTests`` 两组新增于
Issue #440 2026-08-30 修复；``RequiredParameterTests`` 新增于 Trace #445 结构性
防复发修复）。

不需要数据库、不需要任何调用点——只测 :func:`merge_permission_sources` 这一个纯
函数。三个调用点各自的接线测试（真实的 ``PermissionRefreshDuty``/
``AutoOnboardingRunner``/``TargetedPermissionRecompute`` 装配 + 审计事件）分别在
``tests/test_permission_refresh_duty.py::LocalOverrideMergeTest``、
``tests/test_onboarding_runner.py::LocalOverrideMergeTests``、
``tests/test_targeted_permission_recompute.py``。

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
3. **Issue #440 形态判别改坏**：临时把 ``if full_access_wildcard:`` 改成
   ``if True:``（形态判别失效，任何通配都当"真全指标通配"整体跳过，等价于修复
   前的行为），``LimitedWildcardTests`` 全部用例与
   ``PublishRowReadbackSelfProofTests`` 由绿转红——正是 `Issue #440` 报告的
   误判本身（有限指标 ``*`` 用户的补授被跳过、理由码错误标成
   ``grant_redundant_wildcard``）；改回后复绿，证据见本卡收口报告。

**存量沿用（legacy source）机制退役**（Issue #441）：本文件原有的
``LegacyIdentityTests``（钉住已删除的 ``legacy`` 参数语义）及其变异锚点随
``merge_permission_sources`` 签名收窄一并删除，不在此保留占位。

**结构性防复发**（Trace #445）：``full_access_wildcard`` 曾经的默认值 ``True``
正是 ``targeted_recompute.py`` 漏接判据这次真实事故的根因——本文件全部调用点
现在都显式传参（非通配场景取值不影响结果，仍必须传，见签名文档），
``RequiredParameterTests`` 钉住"漏传直接 ``TypeError``"这条结构性保证本身。
"""

from __future__ import annotations

import json
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
from lingxi.core.permission.publish_row import lookup_metrics, serialize_translated_permissions

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
    """真实权限 = (银河 ∪ 本地授权) − 本地抑制（非通配分支）。"""

    def test_local_grant_is_unioned_in(self) -> None:
        local = _resolved(_entry(metric_name="收入"))

        result = merge_permission_sources(
            galaxy={"1011": ("日活",)}, local=local, full_access_wildcard=True
        )

        self.assertEqual(result.permissions, {"1011": ("收入", "日活")})
        self.assertEqual(result.skipped_reasons, ())

    def test_local_grant_can_introduce_a_company_the_galaxy_side_never_had(self) -> None:
        """本地授权可以点出一个银河这一侧完全没有覆盖到的公司——这正是"本地覆盖"
        存在的意义：在银河权限之外追加一条精确授权。"""

        local = _resolved(_entry(company_id="1099", metric_name="收入"))

        result = merge_permission_sources(
            galaxy={"1011": ("日活",)}, local=local, full_access_wildcard=True
        )

        self.assertEqual(result.permissions, {"1011": ("日活",), "1099": ("收入",)})

    def test_suppression_wins_even_over_a_galaxy_granted_metric(self) -> None:
        """抑制优先否定用例：被抑制的指标名来自**银河**这一侧（不是本地授权自己
        加的那条），减法必须作用于并集之后的整体结果。变异锚点①。"""

        local = _resolved(_entry(direction=OverrideDirection.SUPPRESS, metric_name="日活"))

        result = merge_permission_sources(
            galaxy={"1011": ("日活", "收入")}, local=local, full_access_wildcard=True
        )

        self.assertEqual(result.permissions, {"1011": ("收入",)})

    def test_a_key_suppressed_to_nothing_is_dropped_not_written_as_an_empty_list(self) -> None:
        result = merge_permission_sources(
            galaxy={"1011": ("日活",), "1012": ("收入",)},
            local=_resolved(_entry(direction=OverrideDirection.SUPPRESS, metric_name="日活")),
            full_access_wildcard=True,
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

        result = merge_permission_sources(
            galaxy={"1011": ()}, local=local, full_access_wildcard=True
        )

        self.assertNotIn("1011", result.permissions)

    def test_output_values_are_sorted_and_deduplicated(self) -> None:
        local = _resolved(_entry(metric_name="日活"))

        result = merge_permission_sources(
            galaxy={"1011": ("收入", "日活")}, local=local, full_access_wildcard=True
        )

        self.assertEqual(result.permissions["1011"], ("收入", "日活"))


class NoLocalSourceIsIdentityTests(unittest.TestCase):
    """``local=None``：合并结果与 ``galaxy`` 逐字节相同（store 未装配 / 读取失败后
    调用方降级的哨兵值）。"""

    def test_none_is_identity(self) -> None:
        result = merge_permission_sources(
            galaxy={"1011": ("收入", "日活")}, local=None, full_access_wildcard=True
        )

        self.assertEqual(result.permissions, {"1011": ("收入", "日活")})
        self.assertEqual(result.skipped_reasons, ())

    def test_an_empty_but_present_resolved_overrides_is_also_identity(self) -> None:
        """``local`` 非 ``None`` 但内容为空（该用户当前没有任何生效条目）时结果同样
        恒等——只是走的是"参与合并、恰好没有任何贡献"这条路径，不是"跳过"。"""

        result = merge_permission_sources(
            galaxy={"1011": ("日活",)}, local=_resolved(), full_access_wildcard=True
        )

        self.assertEqual(result.permissions, {"1011": ("日活",)})


class LocalAllScopeTests(unittest.TestCase):
    """本地「全部」组（rc25 S-1，Issue #540；``merge_sources`` 模块文档「本地 ``"*"`` 组」
    一节）：本地授权带 ``"*"`` 公司键而银河侧没有 ``"*"`` 时，结果只产出 ``"*"`` 键，
    **从不产出比 ``"*"`` 更窄的具体公司键**；抑制以具体键表达，减到空不可表示。

    变异锚点：把 ``if ALL_COMPANIES_KEY in local_grants:`` 这一支整体删掉（退回非通配
    代数），``test_specific_galaxy_collapses_into_the_star_key_only`` 与
    ``test_never_emits_a_key_narrower_than_star`` 变红（结果长出具体公司键）。"""

    def test_specific_galaxy_collapses_into_the_star_key_only(self) -> None:
        local = _resolved(_entry(company_id="*", metric_name="m1"), _entry(company_id="*", metric_name="m2"))

        result = merge_permission_sources(
            galaxy={"88": ("g1",), "99": ("g2",)}, local=local, full_access_wildcard=False
        )

        self.assertEqual(result.permissions, {"*": ("g1", "g2", "m1", "m2")})
        self.assertEqual(result.skipped_reasons, ())
        self.assertEqual(result.unrepresentable_companies, ())

    def test_zero_galaxy_publishes_exactly_the_group_under_star(self) -> None:
        local = _resolved(_entry(company_id="*", metric_name="m1"))

        result = merge_permission_sources(galaxy={}, local=local, full_access_wildcard=True)

        self.assertEqual(result.permissions, {"*": ("m1",)})

    def test_other_local_specific_grants_are_widened_into_star(self) -> None:
        """行来源无关（与 #440 v2 同一口径）：一条具体公司的本地授权对一个基线已横跨
        全公司的用户而言，就是「额外看到这个指标」。"""

        local = _resolved(_entry(company_id="*", metric_name="m1"), _entry(company_id="40", metric_name="x9"))

        result = merge_permission_sources(galaxy={"88": ("g1",)}, local=local, full_access_wildcard=False)

        self.assertEqual(result.permissions, {"*": ("g1", "m1", "x9")})

    def test_never_emits_a_key_narrower_than_star(self) -> None:
        """自证明：任何具体公司经读侧回退制查到的集合 ⊇ 银河该公司 ∪ 本地全部授权指标。"""

        from lingxi.core.permission.publish_row import lookup_metrics

        galaxy = {"88": ("g1", "g2"), "99": ("g3",)}
        local = _resolved(_entry(company_id="*", metric_name="m1"), _entry(company_id="99", metric_name="x9"))
        result = merge_permission_sources(galaxy=galaxy, local=local, full_access_wildcard=False)

        self.assertEqual(set(result.permissions), {"*"}, "不得出现任何具体公司键")
        for company, metrics in galaxy.items():
            seen = set(lookup_metrics(result.permissions, company))
            self.assertTrue(set(metrics) <= seen, company)
            self.assertTrue({"m1", "x9"} <= seen, company)
        self.assertTrue({"g1", "g2", "g3", "m1", "x9"} <= set(lookup_metrics(result.permissions, "40")))

    def test_limited_galaxy_wildcard_keeps_v2_behaviour(self) -> None:
        local = _resolved(_entry(company_id="*", metric_name="m1"))

        result = merge_permission_sources(galaxy={"*": ("g1",)}, local=local, full_access_wildcard=False)

        self.assertEqual(result.permissions, {"*": ("g1", "m1")})
        self.assertEqual(result.skipped_reasons, ())

    def test_true_full_access_galaxy_wildcard_still_skips_the_group(self) -> None:
        local = _resolved(_entry(company_id="*", metric_name="m1"))

        result = merge_permission_sources(galaxy={"*": ("g1",)}, local=local, full_access_wildcard=True)

        self.assertEqual(result.permissions, {"*": ("g1",)})
        self.assertIn(REASON_GRANT_REDUNDANT_WILDCARD, result.skipped_reasons)

    def test_a_company_suppression_becomes_a_specific_key_of_star_minus_suppressed(self) -> None:
        local = _resolved(
            _entry(company_id="*", metric_name="m1"),
            _entry(company_id="*", metric_name="m2"),
            _entry(company_id="88", metric_name="m2", direction=OverrideDirection.SUPPRESS),
        )

        result = merge_permission_sources(galaxy={"99": ("g1",)}, local=local, full_access_wildcard=False)

        self.assertEqual(result.permissions, {"*": ("g1", "m1", "m2"), "88": ("g1", "m1")})
        self.assertEqual(result.unrepresentable_companies, ())

    def test_a_suppression_that_does_not_bite_emits_no_specific_key(self) -> None:
        local = _resolved(
            _entry(company_id="*", metric_name="m1"),
            _entry(company_id="88", metric_name="nope", direction=OverrideDirection.SUPPRESS),
        )

        result = merge_permission_sources(galaxy={}, local=local, full_access_wildcard=True)

        self.assertEqual(result.permissions, {"*": ("m1",)})

    def test_suppressing_everything_for_one_company_is_unrepresentable(self) -> None:
        """减到空：写侧不产出空列表、读侧缺键会回退 ``"*"``——两条路都表达不了"这家公司
        一个指标都没有"，登记进 ``unrepresentable_companies`` 交调用方 fail-closed。"""

        local = _resolved(
            _entry(company_id="*", metric_name="m1"),
            _entry(company_id="88", metric_name="m1", direction=OverrideDirection.SUPPRESS),
        )

        result = merge_permission_sources(galaxy={}, local=local, full_access_wildcard=True)

        self.assertEqual(result.unrepresentable_companies, ("88",))
        self.assertEqual(result.permissions, {"*": ("m1",)})

    def test_suppressing_on_the_star_key_subtracts_from_the_list(self) -> None:
        local = _resolved(
            _entry(company_id="*", metric_name="m1"),
            _entry(company_id="*", metric_name="m2"),
            _entry(company_id="*", metric_name="m2", direction=OverrideDirection.SUPPRESS),
        )
        # 同键 grant+suppress 已由 resolve_local_overrides 判 suppress 赢；这里只剩 m1。
        result = merge_permission_sources(galaxy={"88": ("m2",)}, local=local, full_access_wildcard=False)

        self.assertEqual(result.permissions, {"*": ("m1",)}, "银河给的 m2 也被 * 抑制减掉")

    def test_a_fully_suppressed_group_falls_back_to_the_plain_algebra(self) -> None:
        """整组被同键抑制清空后本地不再有 ``"*"`` 授权：回到非通配代数，银河具体键原样
        产出；``"*"`` 键上的抑制不跨到具体公司键（既有语义，不在本卡范围）。"""

        local = _resolved(
            _entry(company_id="*", metric_name="m1"),
            _entry(company_id="*", metric_name="m1", direction=OverrideDirection.SUPPRESS),
        )

        result = merge_permission_sources(galaxy={"88": ("g1",)}, local=local, full_access_wildcard=False)

        self.assertEqual(result.permissions, {"88": ("g1",)})
        self.assertEqual(result.unrepresentable_companies, ())

    def test_star_group_suppressed_to_nothing_with_zero_galaxy_is_empty(self) -> None:
        local = _resolved(
            _entry(company_id="*", metric_name="m1"),
            _entry(company_id="*", metric_name="m2"),
            _entry(company_id="*", metric_name="m1", direction=OverrideDirection.SUPPRESS),
            _entry(company_id="*", metric_name="m2", direction=OverrideDirection.SUPPRESS),
        )

        result = merge_permission_sources(galaxy={}, local=local, full_access_wildcard=True)

        self.assertEqual(result.permissions, {})


class WildcardRoleTests(unittest.TestCase):
    """通配角 v1 语义（编排者裁定）：``galaxy`` 出现 ``"*"`` 键（当前唯一形态=513
    后台管理员，``all_companies=True``）时，本地授权与抑制整体不参与合并，产出与
    ``galaxy`` 逐字节相同；跳过的理由各自登记，供调用点各自审计。"""

    def test_wildcard_with_no_local_source_is_untouched_and_silent(self) -> None:
        result = merge_permission_sources(
            galaxy={ALL_COMPANIES_KEY: ("全部指标",)}, local=None, full_access_wildcard=True
        )

        self.assertEqual(result.permissions, {ALL_COMPANIES_KEY: ("全部指标",)})
        self.assertEqual(result.skipped_reasons, (), "没有本地源时不该凭空登记跳过原因")

    def test_wildcard_skips_grant_with_a_reason(self) -> None:
        """变异锚点②（正面半边）。"""

        local = _resolved(_entry(metric_name="额外授权"))

        result = merge_permission_sources(
            galaxy={ALL_COMPANIES_KEY: ("全部指标",)}, local=local, full_access_wildcard=True
        )

        self.assertEqual(result.permissions, {ALL_COMPANIES_KEY: ("全部指标",)}, "通配下 grant 是冗余")
        self.assertIn(REASON_GRANT_REDUNDANT_WILDCARD, result.skipped_reasons)

    def test_wildcard_skips_suppress_with_a_reason(self) -> None:
        """变异锚点②（正面半边）：``suppress`` 拦不住通配——抑制的是具体公司键，
        通配走的是 ``"*"`` 键，两者互不相交。"""

        local = _resolved(_entry(direction=OverrideDirection.SUPPRESS, metric_name="全部指标"))

        result = merge_permission_sources(
            galaxy={ALL_COMPANIES_KEY: ("全部指标",)}, local=local, full_access_wildcard=True
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
            galaxy={ALL_COMPANIES_KEY: ("全部指标",)}, local=local, full_access_wildcard=True
        )

        self.assertEqual(
            set(result.skipped_reasons),
            {REASON_GRANT_REDUNDANT_WILDCARD, REASON_SUPPRESS_INAPPLICABLE_WILDCARD},
        )

    def test_a_normal_user_is_unaffected_by_the_wildcard_rule(self) -> None:
        """否定断言：普通用户（``galaxy`` 没有 ``"*"`` 键）不受通配规则影响，本地
        授权/抑制照常按非通配分支的并集/减法生效。"""

        local = _resolved(_entry(metric_name="收入"))

        result = merge_permission_sources(
            galaxy={"1011": ("日活",)}, local=local, full_access_wildcard=True
        )

        self.assertEqual(result.permissions, {"1011": ("收入", "日活")})
        self.assertEqual(result.skipped_reasons, ())


class LimitedWildcardTests(unittest.TestCase):
    """通配角 v2（``full_access_wildcard=False``，`Issue #440` 2026-08-30 修复）：
    ``all_companies=True`` 但成因是 ``scope.all_countries``（职能有限，翻译出的
    ``"*"`` 清单是有限指标，不是「全部指标」）时，本地授权/抑制参与合并——语义是
    在 ``"*"`` 这一份清单上做并集/减集，而不是 ``WildcardRoleTests`` 那种整体
    跳过。变异锚点④（把 ``if full_access_wildcard:`` 改成 ``if True:``）覆盖
    本类全部用例，见模块文档字符串。
    """

    def test_true_full_wildcard_still_skips_with_original_reason_codes(self) -> None:
        """513 维持跳过：显式传 ``full_access_wildcard=True``时，行为与
        ``WildcardRoleTests`` 逐字节相同——两形态判别不改变真通配这一支。"""

        local = _resolved(_entry(metric_name="额外授权"))

        result = merge_permission_sources(
            galaxy={ALL_COMPANIES_KEY: ("全部指标",)},
            local=local,
            full_access_wildcard=True,
        )

        self.assertEqual(result.permissions, {ALL_COMPANIES_KEY: ("全部指标",)})
        self.assertIn(REASON_GRANT_REDUNDANT_WILDCARD, result.skipped_reasons)

    def test_limited_wildcard_grant_widens_the_wildcard_list(self) -> None:
        """核心正向断言：清单外的补授生效——`Issue #440` 报告的缺陷本身。"""

        local = _resolved(_entry(company_id="1011", metric_name="客户数"))

        result = merge_permission_sources(
            galaxy={ALL_COMPANIES_KEY: ("成本", "收入")},
            local=local,
            full_access_wildcard=False,
        )

        self.assertEqual(result.permissions, {ALL_COMPANIES_KEY: ("客户数", "成本", "收入")})
        self.assertEqual(result.skipped_reasons, ())

    def test_limited_wildcard_suppress_narrows_the_wildcard_list(self) -> None:
        local = _resolved(_entry(direction=OverrideDirection.SUPPRESS, metric_name="成本"))

        result = merge_permission_sources(
            galaxy={ALL_COMPANIES_KEY: ("成本", "收入")},
            local=local,
            full_access_wildcard=False,
        )

        self.assertEqual(result.permissions, {ALL_COMPANIES_KEY: ("收入",)})
        self.assertEqual(result.skipped_reasons, ())

    def test_merge_semantics_is_independent_of_which_company_id_the_row_carries(
        self,
    ) -> None:
        """验收断言原话「合并语义与行来源无关」：两条本地条目分别记着完全不同的
        ``company_id``（一个具体公司、一个字面量 ``"*"`` 本身），对参与合并这件事
        没有任何区别——都只看 ``metric_name``，都并入同一个 ``"*"`` 键。"""

        local = _resolved(
            _entry(company_id="1011", metric_name="客户数"),
            _entry(company_id=ALL_COMPANIES_KEY, metric_name="留存率"),
        )

        result = merge_permission_sources(
            galaxy={ALL_COMPANIES_KEY: ("收入",)},
            local=local,
            full_access_wildcard=False,
        )

        self.assertEqual(
            result.permissions, {ALL_COMPANIES_KEY: ("客户数", "收入", "留存率")}
        )

    def test_no_narrowing_regression_no_specific_company_key_is_ever_produced(
        self,
    ) -> None:
        """窄化回归否定用例：无论本地条目的 ``company_id`` 是什么具体公司，合并
        结果里都**不能**出现那个具体公司键——出现就意味着读侧 ``lookup_metrics``
        对那个公司不再回退 ``"*"``，是当年通配角设计要防的窄范围回归。"""

        local = _resolved(
            _entry(company_id="1011", metric_name="客户数"),
            _entry(company_id="1099", direction=OverrideDirection.SUPPRESS, metric_name="收入"),
        )

        result = merge_permission_sources(
            galaxy={ALL_COMPANIES_KEY: ("收入", "成本")},
            local=local,
            full_access_wildcard=False,
        )

        self.assertEqual(set(result.permissions), {ALL_COMPANIES_KEY})
        self.assertNotIn("1011", result.permissions)
        self.assertNotIn("1099", result.permissions)

    def test_a_grant_that_is_already_covered_does_not_change_the_result_and_is_not_mislabeled(
        self,
    ) -> None:
        """理由码修正的核心断言：即便这次补授的指标碰巧已经在清单里（合并后结果
        不变），也绝不登记 ``grant_redundant_wildcard``——这个理由码在有限指标
        形态下不成立（模块文档「通配角 v2」），登记它会重新暗示"清单外指标不能
        被补授"这个已经被坐实为误判的假设。"""

        local = _resolved(_entry(metric_name="收入"))

        result = merge_permission_sources(
            galaxy={ALL_COMPANIES_KEY: ("成本", "收入")},
            local=local,
            full_access_wildcard=False,
        )

        self.assertEqual(result.permissions, {ALL_COMPANIES_KEY: ("成本", "收入")})
        self.assertEqual(result.skipped_reasons, ())
        self.assertNotIn(REASON_GRANT_REDUNDANT_WILDCARD, result.skipped_reasons)

    def test_fully_suppressed_wildcard_drops_the_key_not_an_empty_list(self) -> None:
        local = _resolved(_entry(direction=OverrideDirection.SUPPRESS, metric_name="收入"))

        result = merge_permission_sources(
            galaxy={ALL_COMPANIES_KEY: ("收入",)},
            local=local,
            full_access_wildcard=False,
        )

        self.assertEqual(result.permissions, {})
        self.assertNotIn(
            ALL_COMPANIES_KEY, result.permissions, "抑制到空必须丢弃键，不写空列表"
        )

    def test_no_local_source_is_identity_under_the_limited_form_too(self) -> None:
        result = merge_permission_sources(
            galaxy={ALL_COMPANIES_KEY: ("收入",)}, local=None, full_access_wildcard=False
        )

        self.assertEqual(result.permissions, {ALL_COMPANIES_KEY: ("收入",)})
        self.assertEqual(result.skipped_reasons, ())


class PublishRowReadbackSelfProofTests(unittest.TestCase):
    """自证闭环（Issue #440 评论「自证闭环条款」，PM 2026-08-30）：受控直造一个
    「有限指标 ``*``」用户的本地授权行（``company_id`` 与真实业务公司无关，证
    「合并语义与行来源无关」）→ 触发合并（即「重算」——`merge_permission_sources`
    正是两个调用点在重算/开通时实际调用的那一步）→ 断言合并结果为「"*" 清单 ∪
    补授」且无窄化回归 → 序列化成发布行 ``permissions`` 文本、反序列化模拟「发布
    表回读」，用 :func:`lookup_metrics` 核对任意具体公司都能看到并集后的指标。
    全程只调用本仓库既有纯函数，不连数据库/stage，`全程不依赖 PM`。
    """

    def test_a_supplementary_grant_survives_serialization_and_readback_with_no_narrowing(
        self,
    ) -> None:
        baseline = ("净利润", "收入")
        # company_id="9999" 是一个与该用户实际业务范围无关的占位公司——刻意证明
        # 合并是否生效不取决于这个字段的取值。
        local = _resolved(_entry(company_id="9999", metric_name="客户数"))

        merged = merge_permission_sources(
            galaxy={ALL_COMPANIES_KEY: baseline},
            local=local,
            full_access_wildcard=False,
        )

        self.assertEqual(merged.permissions, {ALL_COMPANIES_KEY: ("净利润", "客户数", "收入")})
        self.assertEqual(merged.skipped_reasons, ())

        published_text = serialize_translated_permissions(merged.permissions)
        read_back = json.loads(published_text)

        self.assertEqual(read_back, {ALL_COMPANIES_KEY: ["净利润", "客户数", "收入"]})
        for company_id in ("1011", "1099", "任意未曾出现过的公司"):
            with self.subTest(company_id=company_id):
                self.assertEqual(
                    lookup_metrics(read_back, company_id), ("净利润", "客户数", "收入")
                )
                self.assertNotIn(
                    company_id, read_back, "不得因为合并而凭空长出具体公司键"
                )

    def test_a_caller_that_hardcodes_true_reproduces_the_pre_440_misjudgment(self) -> None:
        """红/绿对照的「红」半边：模拟一个调用点没有做「真全指标通配 vs 有限指标
        通配」这一步判断、直接传 ``full_access_wildcard=True``——`Issue #440`
        修复前两个既有调用点的唯一调用形状，`Issue #445` 又在第三个新调用点
        ``targeted_recompute.py`` 里原样复现过一次，都是"漏接判据"这同一类
        缺陷（不是签名收紧就能挡住的那一类——调用方即使显式传参，也可能选错值，
        见 ``RequiredParameterTests`` 钉的是另一半："完全不传"才会被结构性拦住）。
        同一个补授在有限指标形态下被误判为冗余而不生效、理由码错误——这正是
        `Issue #440` 的缺陷本身。"""

        baseline = ("净利润", "收入")
        local = _resolved(_entry(company_id="9999", metric_name="客户数"))

        misjudged = merge_permission_sources(
            galaxy={ALL_COMPANIES_KEY: baseline}, local=local, full_access_wildcard=True
        )

        self.assertEqual(
            misjudged.permissions,
            {ALL_COMPANIES_KEY: baseline},
            "错误地传 full_access_wildcard=True 时，补授的指标不出现在结果里——这就是误判本身",
        )
        self.assertIn(
            REASON_GRANT_REDUNDANT_WILDCARD, misjudged.skipped_reasons
        )


class RequiredParameterTests(unittest.TestCase):
    """结构性防复发（Trace #445）：``full_access_wildcard`` 曾经的默认值 ``True``
    正是 ``targeted_recompute.py`` 漏接判据这次真实事故的根因——签名收紧为必填
    关键字参数之后，任何调用点漏传都应该在开发期就被 ``TypeError`` 拦住，不再
    悄悄退回成"当作真全指标通配"这条可能错误的行为。变异锚点：临时把签名的
    ``full_access_wildcard: bool`` 改回 ``full_access_wildcard: bool = True``
    后，本用例会由绿转红（不再抛出 ``TypeError``）。
    """

    def test_omitting_the_parameter_raises_type_error_instead_of_silently_defaulting(
        self,
    ) -> None:
        with self.assertRaises(TypeError):
            merge_permission_sources(galaxy={"1011": ("日活",)}, local=None)  # type: ignore[call-arg]


class ResultTypeTests(unittest.TestCase):
    def test_result_is_a_merged_permission_sources_instance(self) -> None:
        result = merge_permission_sources(
            galaxy={"1011": ("日活",)}, local=None, full_access_wildcard=True
        )

        self.assertIsInstance(result, MergedPermissionSources)


if __name__ == "__main__":
    unittest.main()
