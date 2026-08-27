"""银河「后台管理员」（role_id 513）→ 全公司 × 全指标（Issue #320；B 口径见 Trace
#328 裁定 #1，2026-08-27）端到端验收。

背景：内测首日实证——用户代号 U1（化名，非真实姓名/工号/邮箱）银河角色仅持有
「后台管理员」这一个角色、没有任何其他业务角色，开通链因此在「银河角色→职能」
这一层查不到受支持职能而三连拒。产品负责人 2026-08-26 裁定「银河管理员理论上
所有指标都可以看，是我忘了建这条通道」，即「全公司 × 全指标」（``*×*``）。

**A 口径 → B 口径（2026-08-27）**：首版实现（#320）在配置文件里给每一个已知公司键
各写一条「后台管理员」映射（A 口径）。真实验收发现它不等于「全公司」：具体公司
枚举的持有人，聚合结果只覆盖枚举到的那些公司，银河新增公司时还得回来手改配置——
``AdminRoleWithoutWildcardCompanyScopeStillTranslatesTest`` 曾经钉住的正是这个
（当时是有意为之的健壮性断言，事后被认定不满足「全公司」的字面要求）。产品负责人
裁定改用 B 口径：聚合层（``core/permission/publish_row.aggregate_permission``）
直接认「有没有这个职能」，不看这次快照枚举到了哪些公司——``all_companies`` 强制为
真，天然覆盖未来新增公司。该测试类已按 B 口径反转断言，新增负例钉住特例不会误伤
普通职能用户（见 ``NonAdminFunctionUserIsUnaffectedByTheSpecialCaseTest``）。

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
    ADMIN_FULL_ACCESS_FUNCTION,
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
#: 这是**银河角色名**（映射的左边），恰好与聚合层触发全公司特例的**职能标签**
#: （映射的右边，:data:`~lingxi.core.permission.publish_row.ADMIN_FULL_ACCESS_FUNCTION`）
#: 逐字相同——两者是配置文件里 `"后台管理员" = "后台管理员"` 这一行的左右两侧，
#: 概念不同（一个是银河的输入，一个是 Lingxi 的输出），因此各自留一个常量，不合并；
#: 下面 ``AdminFunctionLabelMatchesGalaxyRoleNameTest`` 钉住这条巧合关系不会静默漂移。
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
    """B 口径核心断言（产品负责人 2026-08-27 裁定，Trace #328 裁定 #1，取代 #320 的
    A 口径）：若某个「后台管理员」持有人在银河那一侧不是「全非」而是具体公司枚举
    （不同于 U1 的既知情形，但同一角色的其他 10 名持有人形态未知），聚合结果**仍然**
    是全公司——``all_companies`` 由聚合层的「角色即全公司」特例强制为真，不依赖这次
    快照解释出的具体公司列表。

    **这条断言在 A 口径下曾经是反过来的**（``all_companies`` 为假、翻译只覆盖枚举到
    的那个具体公司键）：A 口径给每一个已知公司键各写一条「后台管理员」映射，银河将来
    新增公司时还得回来手改 ``config/company_function_metric_map.toml``。B 口径把判据
    从「这个人枚举到了哪些公司」搬到「这个人有没有这个职能」，翻译结果因此恒定落在
    通配键——**含未来新增公司**，因为通配键从一开始就不按公司数量枚举，不需要新公司
    出现时再补配置。
    """

    def setUp(self) -> None:
        self.role_function_map = load_role_function_map()
        self.metric_translation_map = load_company_function_metric_map()

    def test_specific_company_scope_is_overridden_to_all_companies(self) -> None:
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
        # B 口径的核心分歧点：即使银河那一侧只枚举了公司 "1"（不是「全非」），
        # 聚合结果的 all_companies 仍必须是 True——这是本条修复要证明的事。
        self.assertTrue(
            aggregate.all_companies,
            "持有「后台管理员」时 all_companies 必须恒为真，不依赖具体公司枚举（B 口径）",
        )
        # companies 字段不被特例清空或改写：它仍然是这次快照解释出的具体公司列表，
        # 只是序列化/翻译层在 all_companies=True 时根本不会读它（见下面的翻译结果）。
        self.assertEqual(aggregate.companies, ("1",))

        translated = translate_company_functions(
            companies=aggregate.companies,
            functions=aggregate.functions,
            all_companies=aggregate.all_companies,
            mapping=self.metric_translation_map,
        )

        # 翻译结果落在通配键，不是具体公司 "1"——这就是「全公司（含未来新增公司）」
        # 在发布内容里的最终形状：新公司出现时这条判据不需要任何变化。
        self.assertEqual(set(translated), {ALL_COMPANIES_KEY})
        self.assertEqual(set(translated[ALL_COMPANIES_KEY]), EXPECTED_ALL_METRICS)


class NonAdminFunctionUserIsUnaffectedByTheSpecialCaseTest(unittest.TestCase):
    """负例：不持有「后台管理员」的普通职能用户，聚合结果完全不受这条特例影响——
    具体公司枚举依然是具体公司枚举，不会被误判成全公司。

    **变异锚点**：把 ``aggregate_permission`` 里的特例条件
    （``ADMIN_FULL_ACCESS_FUNCTION in functions``）改成恒为 ``True``，本用例必红
    （``all_companies`` 会被误判成 ``True``）——这是钉住「特例只对持有该职能的人生效」
    的唯一手段，前一个测试类只能证明「持有时生效」，证明不了「不持有时不生效」。
    """

    def setUp(self) -> None:
        self.role_function_map = load_role_function_map()

    def test_ordinary_function_holder_keeps_specific_company_scope(self) -> None:
        aggregate = aggregate_permission(
            galaxy_user_id=U1_GALAXY_USER_ID,
            user_role_rows=_role_rows(role_id="8", role_name="A运营"),
            datacountry_rows=[{"user_id": U1_GALAXY_USER_ID, "datacountry_id": "11"}],
            country_rows=[
                {"country_key": "11", "name": "示例国家", "name_cn": "示例国家", "boss_company_id": "1"},
            ],
            role_function_map=self.role_function_map,
        )

        self.assertTrue(aggregate.granted)
        self.assertEqual(aggregate.functions, ("运营",))
        self.assertNotIn(ADMIN_FULL_ACCESS_FUNCTION, aggregate.functions)
        self.assertFalse(
            aggregate.all_companies,
            "不持有「后台管理员」时，all_companies 必须仍由银河侧的实际范围决定",
        )
        self.assertEqual(aggregate.companies, ("1",))


class AdminFunctionLabelMatchesGalaxyRoleNameTest(unittest.TestCase):
    """钉住模块顶部的巧合关系：本文件的 ``ADMIN_ROLE_NAME``（银河角色名，映射左边）
    与 ``publish_row.ADMIN_FULL_ACCESS_FUNCTION``（Lingxi 职能标签，触发全公司特例的
    判据，映射右边）逐字相同。两者是配置文件里同一行的左右两侧，各自独立维护；
    这条用例只是防止其中一边改名时另一边被静默遗漏。
    """

    def test_role_name_and_function_label_are_the_same_literal(self) -> None:
        self.assertEqual(ADMIN_ROLE_NAME, ADMIN_FULL_ACCESS_FUNCTION)


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
