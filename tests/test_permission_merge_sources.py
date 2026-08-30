"""``core/permission/merge_sources.py`` 的纯逻辑测试（Issue #319 S-P-3；
``LimitedWildcardTests``/``PublishRowReadbackSelfProofTests`` 两组新增于
Issue #440 2026-08-30 修复）。

不需要数据库、不需要任何调用点——只测 :func:`merge_permission_sources` 这一个纯
函数。两个调用点各自的接线测试（真实的 ``PermissionRefreshDuty``/
``AutoOnboardingRunner`` 装配 + 审计事件）分别在
``tests/test_permission_refresh_duty.py::LocalOverrideMergeTest`` 与
``tests/test_onboarding_runner.py::LocalOverrideMergeTests``。

两处变异锚点已实测验红、验证后原样还原（S-P-3 实施卡要求，结果登记在此，不重复
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


class LimitedWildcardTests(unittest.TestCase):
    """通配角 v2（``full_access_wildcard=False``，`Issue #440` 2026-08-30 修复）：
    ``all_companies=True`` 但成因是 ``scope.all_countries``（职能有限，翻译出的
    ``"*"`` 清单是有限指标，不是「全部指标」）时，本地授权/抑制参与合并——语义是
    在 ``"*"`` 这一份清单上做并集/减集，而不是 ``WildcardRoleTests`` 那种整体
    跳过。变异锚点④（把 ``if full_access_wildcard:`` 改成 ``if True:``）覆盖
    本类全部用例，见模块文档字符串。
    """

    def test_true_full_wildcard_still_skips_with_original_reason_codes(self) -> None:
        """513 维持跳过：显式传 ``full_access_wildcard=True``（与默认值等价）时，
        行为与 ``WildcardRoleTests`` 逐字节相同——两形态判别不改变真通配这一支。"""

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

    def test_before_the_fix_the_same_scenario_reproduces_the_misjudgment(self) -> None:
        """红/绿对照的「红」半边：不传 ``full_access_wildcard``（即修复前两个调用
        点唯一会用到的调用形状）时，同一个补授在有限指标形态下被误判为冗余而不
        生效、理由码错误——这正是 `Issue #440` 的缺陷本身，钉在测试里防止有人
        以为「默认值」也修好了这个场景（默认值的职责只是不破坏 513，见模块文档
        「通配角 v2」"默认值 True"一段）。"""

        baseline = ("净利润", "收入")
        local = _resolved(_entry(company_id="9999", metric_name="客户数"))

        merged_without_the_new_signal = merge_permission_sources(
            galaxy={ALL_COMPANIES_KEY: baseline}, local=local
        )

        self.assertEqual(
            merged_without_the_new_signal.permissions,
            {ALL_COMPANIES_KEY: baseline},
            "不显式声明有限指标形态时，补授的指标不出现在结果里——这就是误判本身",
        )
        self.assertIn(
            REASON_GRANT_REDUNDANT_WILDCARD, merged_without_the_new_signal.skipped_reasons
        )


class ResultTypeTests(unittest.TestCase):
    def test_result_is_a_merged_permission_sources_instance(self) -> None:
        result = merge_permission_sources(galaxy={"1011": ("日活",)}, local=None)

        self.assertIsInstance(result, MergedPermissionSources)


if __name__ == "__main__":
    unittest.main()
