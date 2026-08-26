"""银河「后台管理员」（role_id 513）→ 全公司 × 全指标（Issue #320）端到端验收。

背景：内测首日实证——用户代号 U1（化名，非真实姓名/工号/邮箱）银河角色仅持有
「后台管理员」这一个角色、没有任何其他业务角色，开通链因此在「银河角色→职能」
这一层查不到受支持职能而三连拒。产品负责人 2026-08-26 裁定「银河管理员理论上
所有指标都可以看，是我忘了建这条通道」，即「全公司 × 全指标」（``*×*``）。

本文件与 ``tests/test_galaxy_role_function.py`` / ``tests/test_company_function_
metric_map_file.py`` 的分工不同：那两个文件各自钉住**单个**随包配置文件的内容，
本文件钉住**两份随包配置文件接起来之后**、经过真实聚合与翻译两层的端到端结果——
即验收断言字面要求的「仅持有 513 的用户聚合结果 granted=True 且覆盖全部公司与
全部指标」。全程使用两份**随包发布的真实文件**（不是测试夹具），只有银河账号本身
的角色/国家授权行是构造出来的合成数据（因为真实银河快照不进仓库）。
"""

from __future__ import annotations

import json
import unittest

from lingxi.adapters.company_function_metric_map_file import load_company_function_metric_map
from lingxi.adapters.role_function_map_file import load_role_function_map
from lingxi.core.permission.metric_translation import translate_company_functions
from lingxi.core.permission.publish_row import (
    ALL_COMPANIES_KEY,
    REASON_GRANTED,
    aggregate_permission,
    serialize_translated_permissions,
)

#: 化名（Issue #320 沿用的用户代号），不是真实姓名/工号/邮箱。仅在测试数据里出现。
U1_GALAXY_USER_ID = "u1-fake-galaxy-user"

#: 银河「后台管理员」角色在 galaxy_user_role 快照行里的原始形态：user_id/role_id/
#: role_name 三列都参与判定路径的不同环节（role_id 只用于人读留痕与审计，真正驱动
#: 职能判定的是 role_name——见 core/permission/galaxy_scope.py 的 role_names_for_user
#: 与 role_ids_for_user 分工）。
ADMIN_ROLE_ID = "513"
ADMIN_ROLE_NAME = "后台管理员"

#: 期望的「全部指标」集合：与 config/company_function_metric_map.toml 里 CEO/运营/
#: 后台管理员三个职能当前共用的取值逐字相同（见该文件「职能 → 指标」表）。
EXPECTED_ALL_METRICS = frozenset(
    {
        "sub_new_count",
        "sub_recharge_count",
        "sub_recharge_money",
        "sub_deduction_count",
        "sub_deduction_money",
        "exchange_rate",
        "vat_rate",
        "channel_rate",
        "channel_market_sharing",
    }
)


def _role_rows(*, role_id: str, role_name: str) -> list[dict[str, str]]:
    return [{"user_id": U1_GALAXY_USER_ID, "role_id": role_id, "role_name": role_name}]


def _all_countries_datacountry_rows() -> list[dict[str, str]]:
    """「全非」通配授权：只登记哨兵键，展开由 ``resolve_company_scope`` 负责。"""

    return [{"user_id": U1_GALAXY_USER_ID, "datacountry_id": "0"}]


def _country_rows_with_sentinel(*extra: tuple[str, str, str]) -> list[dict[str, str]]:
    """一份合法的 ``sys_country`` 快照：哨兵行 + 若干真实国家行。

    ``extra`` 是 ``(country_key, name_cn, boss_company_id)`` 三元组，用来模拟受控导出
    里会出现的若干公司；测试只关心「全非展开之后应当覆盖到这些公司」，不关心真实的
    国家/公司对照表内容（那不是本文件要证明的事）。
    """

    rows = [{"country_key": "0", "name": "ALL", "name_cn": "全非", "boss_company_id": "0"}]
    for country_key, name_cn, boss_company_id in extra:
        rows.append(
            {
                "country_key": country_key,
                "name": country_key,
                "name_cn": name_cn,
                "boss_company_id": boss_company_id,
            }
        )
    return rows


class AdminRoleGrantsAllCompaniesAllMetricsTest(unittest.TestCase):
    """核心验收：仅持有 513 的用户聚合结果 ``granted=True`` 且覆盖全部公司与全部指标。"""

    def setUp(self) -> None:
        self.role_function_map = load_role_function_map()
        self.metric_translation_map = load_company_function_metric_map()

    def test_u1_with_only_the_admin_role_is_granted(self) -> None:
        """开通聚合点（``onboarding_runner._match`` 调用的同一个函数）对 U1 的判定：
        单持有「后台管理员」这一个角色，聚合结果必须 ``granted=True``——修复前这里是
        ``REASON_NO_SUPPORTED_FUNCTION``，即内测首日三连拒的直接原因。
        """

        aggregate = aggregate_permission(
            galaxy_user_id=U1_GALAXY_USER_ID,
            user_role_rows=_role_rows(role_id=ADMIN_ROLE_ID, role_name=ADMIN_ROLE_NAME),
            datacountry_rows=_all_countries_datacountry_rows(),
            country_rows=_country_rows_with_sentinel(
                ("11", "示例国家一", "1011"),
                ("12", "示例国家二", "1012"),
            ),
            role_function_map=self.role_function_map,
        )

        self.assertTrue(aggregate.granted)
        self.assertEqual(aggregate.reason, REASON_GRANTED)
        self.assertEqual(aggregate.functions, ("后台管理员",))
        self.assertTrue(aggregate.all_companies, "银河「全非」授权必须展开为 all_companies=True")
        self.assertEqual(aggregate.companies, ("1011", "1012"))
        self.assertEqual(aggregate.role_count, 1)
        self.assertEqual(aggregate.unmapped_role_count, 0, "「后台管理员」必须命中角色映射，不是未映射")

    def test_translation_covers_every_company_and_every_metric(self) -> None:
        """聚合结果经真实翻译映射之后：公司维度用 ``"*"`` 通配键覆盖全公司，指标维度
        是当前已知的全部指标——这就是「全公司 × 全指标」在发布内容里的最终形状。
        """

        aggregate = aggregate_permission(
            galaxy_user_id=U1_GALAXY_USER_ID,
            user_role_rows=_role_rows(role_id=ADMIN_ROLE_ID, role_name=ADMIN_ROLE_NAME),
            datacountry_rows=_all_countries_datacountry_rows(),
            country_rows=_country_rows_with_sentinel(("11", "示例国家一", "1011")),
            role_function_map=self.role_function_map,
        )
        self.assertTrue(aggregate.granted)

        translated = translate_company_functions(
            companies=aggregate.companies,
            functions=aggregate.functions,
            all_companies=aggregate.all_companies,
            mapping=self.metric_translation_map,
        )

        self.assertEqual(set(translated), {ALL_COMPANIES_KEY})
        self.assertEqual(set(translated[ALL_COMPANIES_KEY]), EXPECTED_ALL_METRICS)

        # 落到发布表 permissions 单元格文本的最终形状：{"*": [9 个指标 ID 排序去重]}。
        published_text = serialize_translated_permissions(translated)
        published = json.loads(published_text)
        self.assertEqual(set(published), {"*"})
        self.assertEqual(set(published["*"]), EXPECTED_ALL_METRICS)

    def test_admin_role_alone_without_any_business_role_is_still_granted(self) -> None:
        """「仅持有」是验收断言的关键词：U1 没有任何其他业务角色（不叠加 CEO/运营等），
        必须单独凭「后台管理员」一项拿到有效权限，不依赖任何其他角色兜底。
        """

        aggregate = aggregate_permission(
            galaxy_user_id=U1_GALAXY_USER_ID,
            user_role_rows=_role_rows(role_id=ADMIN_ROLE_ID, role_name=ADMIN_ROLE_NAME),
            datacountry_rows=_all_countries_datacountry_rows(),
            country_rows=_country_rows_with_sentinel(("11", "示例国家一", "1011")),
            role_function_map=self.role_function_map,
        )

        self.assertEqual(aggregate.functions, ("后台管理员",))
        self.assertNotIn("CEO", aggregate.functions)
        self.assertNotIn("运营", aggregate.functions)


class AdminRoleWithoutWildcardCompanyScopeStillTranslatesTest(unittest.TestCase):
    """健壮性：若某个「后台管理员」持有人在银河那一侧不是「全非」而是具体公司枚举
    （不同于 U1 的既知情形，但同一角色的其他 10 名持有人形态未知），翻译层也不能
    对这个组合 fail-closed——这正是「后台管理员」条目写进**每一个**公司键、而不是
    只写 ``[companies."*"]`` 一条的原因（见两份配置文件里的对应说明）。
    """

    def setUp(self) -> None:
        self.role_function_map = load_role_function_map()
        self.metric_translation_map = load_company_function_metric_map()

    def test_specific_company_scope_without_all_non_still_covers_all_metrics(self) -> None:
        aggregate = aggregate_permission(
            galaxy_user_id=U1_GALAXY_USER_ID,
            user_role_rows=_role_rows(role_id=ADMIN_ROLE_ID, role_name=ADMIN_ROLE_NAME),
            datacountry_rows=[{"user_id": U1_GALAXY_USER_ID, "datacountry_id": "11"}],
            country_rows=[
                {"country_key": "11", "name": "示例国家", "name_cn": "示例国家", "boss_company_id": "1"},
            ],
            role_function_map=self.role_function_map,
        )

        self.assertTrue(aggregate.granted)
        self.assertFalse(aggregate.all_companies)
        self.assertEqual(aggregate.companies, ("1",))

        translated = translate_company_functions(
            companies=aggregate.companies,
            functions=aggregate.functions,
            all_companies=aggregate.all_companies,
            mapping=self.metric_translation_map,
        )

        self.assertEqual(set(translated), {"1"})
        self.assertEqual(set(translated["1"]), EXPECTED_ALL_METRICS)


class WithoutTheMappingEntryTheUserWouldStillBeDeniedTest(unittest.TestCase):
    """回归证据：证明这条修复确实解决了「三连拒」——用一份**不含**「后台管理员」的
    角色映射（模拟修复前的随包文件状态）复现 U1 当初被拒的真实原因码。
    """

    def test_before_the_fix_the_role_alone_fails_closed(self) -> None:
        pre_fix_role_function_map = {
            key: value
            for key, value in load_role_function_map().items()
            if key != ADMIN_ROLE_NAME
        }

        aggregate = aggregate_permission(
            galaxy_user_id=U1_GALAXY_USER_ID,
            user_role_rows=_role_rows(role_id=ADMIN_ROLE_ID, role_name=ADMIN_ROLE_NAME),
            datacountry_rows=_all_countries_datacountry_rows(),
            country_rows=_country_rows_with_sentinel(("11", "示例国家一", "1011")),
            role_function_map=pre_fix_role_function_map,
        )

        self.assertFalse(aggregate.granted)
        self.assertEqual(aggregate.unmapped_role_count, 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
