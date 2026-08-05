"""银河权限三条连接链路的陷阱断言（V-银河-01/02/03/04）。

全部使用虚构的合成数据：人名、邮箱、角色名、国家名均为测试化名，
不含任何真实导出内容（[验证与门禁十三、测试数据与身份边界]）。
"""

from __future__ import annotations

import unittest

from lingxi.core.permission.galaxy_scope import (
    SENTINEL_COUNTRY_KEY,
    country_keys_for_user,
    menu_entries_for_roles,
    menu_ids_for_roles,
    resolve_company_scope,
    role_ids_for_user,
)


def _user_role_rows() -> list[dict[str, str]]:
    """`user_name` 列存的是中文姓名，且刻意与本人错位——这正是列名误导的形态。"""

    return [
        # 账号 U1（虚构姓名「化名甲」）真实持有 R-甲；行内姓名列写的却是「化名乙」。
        {"user_id": "U1", "role_id": "R-甲", "source_user_name": "化名乙", "role_name": "A运营"},
        # 账号 U2（虚构姓名「化名乙」）真实持有 R-乙；行内姓名列写的是「化名甲」。
        {"user_id": "U2", "role_id": "R-乙", "source_user_name": "化名甲", "role_name": "A销售"},
    ]


def _join_by_name_column(display_name: str, rows: list[dict[str, str]]) -> set[str]:
    """测试内构造的**错误连接**：按 `user_role.user_name` 连接。"""

    return {row["role_id"] for row in rows if row["source_user_name"] == display_name}


class GalaxyUserIdJoinTest(unittest.TestCase):
    """V-银河-01：三表一律按 user_id 连接；按名称列连接会取到语义相反的值。"""

    def test_roles_are_joined_by_user_id_only(self) -> None:
        rows = _user_role_rows()

        self.assertEqual(role_ids_for_user("U1", rows), ("R-甲",))
        self.assertEqual(role_ids_for_user("U2", rows), ("R-乙",))

    def test_joining_by_the_name_column_returns_the_other_person_roles(self) -> None:
        rows = _user_role_rows()

        # 账号 U1 的中文姓名是「化名甲」。按姓名列连接拿到的是另一个人的角色。
        wrong = _join_by_name_column("化名甲", rows)
        self.assertEqual(wrong, {"R-乙"})
        self.assertNotEqual(set(role_ids_for_user("U1", rows)), wrong)

    def test_user_id_join_ignores_rows_of_other_accounts(self) -> None:
        rows = _user_role_rows() + [
            {"user_id": "U3", "role_id": "R-丙", "source_user_name": "化名甲", "role_name": "临时角色"},
        ]

        self.assertEqual(role_ids_for_user("U1", rows), ("R-甲",))

    def test_country_rows_are_joined_by_user_id_not_by_name(self) -> None:
        rows = [
            {"user_id": "U1", "datacountry_id": "101", "source_user_name": "化名乙", "datacountry_name": "甲国"},
            {"user_id": "U2", "datacountry_id": "202", "source_user_name": "化名甲", "datacountry_name": "乙国"},
        ]

        self.assertEqual(country_keys_for_user("U1", rows), ("101",))
        self.assertEqual(country_keys_for_user("U2", rows), ("202",))


class GalaxyCountryKeyJoinTest(unittest.TestCase):
    """V-银河-02：公司范围连接键是 country_key，不是 sys_country.id。"""

    @staticmethod
    def _country_rows() -> list[dict[str, str | None]]:
        # 刻意让一行的 country_key 等于另一行的 id：按主键连接会取到另一个国家。
        return [
            {
                "source_id": "7",
                "country_key": "101",
                "name": "ALPHA",
                "code": "AL",
                "name_cn": "甲国",
                "region_key": "1",
                "region_name": "甲区",
                "boss_company_id": "BC-甲",
            },
            {
                "source_id": "101",
                "country_key": "202",
                "name": "BETA",
                "code": "BE",
                "name_cn": "乙国",
                "region_key": None,
                "region_name": None,
                "boss_company_id": "BC-乙",
            },
        ]

    def test_company_scope_resolves_through_country_key(self) -> None:
        scope = resolve_company_scope(("101",), self._country_rows())

        self.assertFalse(scope.all_countries)
        self.assertEqual([country.name_cn for country in scope.countries], ["甲国"])
        self.assertEqual([country.boss_company_id for country in scope.countries], ["BC-甲"])

    def test_joining_by_primary_key_would_return_the_wrong_country(self) -> None:
        rows = self._country_rows()
        by_source_id = {row["source_id"]: row for row in rows}

        # 按主键 id 连接同一个授权值 101，得到的是「乙国」——与正确结果不同。
        self.assertEqual(by_source_id["101"]["name_cn"], "乙国")
        scope = resolve_company_scope(("101",), rows)
        self.assertNotIn("乙国", [country.name_cn for country in scope.countries])

    def test_country_key_without_matching_row_is_reported_not_dropped(self) -> None:
        scope = resolve_company_scope(("101", "999"), self._country_rows())

        self.assertEqual(scope.unresolved_country_keys, ("999",))
        self.assertEqual([country.name_cn for country in scope.countries], ["甲国"])

    def test_rows_without_country_key_are_never_reachable_from_authorization(self) -> None:
        rows = self._country_rows() + [
            {
                "source_id": "500",
                "country_key": None,
                "name": "GAMMA",
                "code": "GA",
                "name_cn": "丙国",
                "region_key": None,
                "region_name": None,
                "boss_company_id": "BC-丙",
            },
        ]

        scope = resolve_company_scope(("500",), rows)

        self.assertEqual(scope.countries, ())
        self.assertEqual(scope.unresolved_country_keys, ("500",))


class GalaxyMenuIdTest(unittest.TestCase):
    """V-银河-03：菜单一律以 menu_id 为准，同名不同 id 不得合并。"""

    @staticmethod
    def _role_menu_rows() -> list[dict[str, str]]:
        return [
            {"role_id": "R-甲", "menu_id": "M1", "role_name": "A运营", "menu_name": "报表"},
            {"role_id": "R-乙", "menu_id": "M2", "role_name": "A销售", "menu_name": "报表"},
            {"role_id": "R-乙", "menu_id": "M3", "role_name": "A销售", "menu_name": "台账"},
        ]

    def test_same_menu_name_under_different_ids_is_not_merged(self) -> None:
        rows = self._role_menu_rows()

        menu_ids = menu_ids_for_roles(("R-甲", "R-乙"), rows)

        self.assertEqual(menu_ids, ("M1", "M2", "M3"))
        # 若按 menu_name 归并，两个「报表」会塌成一个，权限被少算。
        self.assertNotEqual(len({row["menu_name"] for row in rows}), len(menu_ids))

    def test_menu_of_another_role_is_not_granted(self) -> None:
        rows = self._role_menu_rows()

        self.assertEqual(menu_ids_for_roles(("R-甲",), rows), ("M1",))

    def test_menu_entries_keep_id_and_name_paired(self) -> None:
        entries = menu_entries_for_roles(("R-甲", "R-乙"), self._role_menu_rows())

        self.assertEqual(
            [(entry.menu_id, entry.menu_name) for entry in entries],
            [("M1", "报表"), ("M2", "报表"), ("M3", "台账")],
        )

    def test_name_based_lookup_is_ambiguous_by_construction(self) -> None:
        rows = self._role_menu_rows()

        by_name = {row["menu_name"] for row in rows if row["menu_name"] == "报表"}
        ids_named_report = {row["menu_id"] for row in rows if row["menu_name"] == "报表"}

        self.assertEqual(len(by_name), 1)
        self.assertEqual(ids_named_report, {"M1", "M2"})


class GalaxyAllAfricaSentinelTest(unittest.TestCase):
    """V-银河-04：`全非` 哨兵按产品负责人 2026-08-05 决策展开为所有国家所有公司。"""

    def test_wildcard_includes_countries_without_country_key(self) -> None:
        """全非=所有国家所有公司：无 country_key 的行（约五分之一）同样携带
        boss_company_id，通配展开必须包含，静默丢弃会系统性少权限（独立复查+Codex）。"""
        rows = [
            {"country_key": "0", "name": "ALL", "name_cn": "全非", "boss_company_id": "B0"},
            {"country_key": "11", "name": "Kenya", "name_cn": "肯尼亚", "boss_company_id": "B11"},
            {"country_key": "", "name": "Djibouti", "name_cn": "吉布提", "boss_company_id": "B77"},
            {"country_key": None, "name": "Comoros", "name_cn": "科摩罗", "boss_company_id": "B78"},
        ]
        scope = resolve_company_scope(["0"], rows)
        companies = {country.boss_company_id for country in scope.countries}
        self.assertEqual(companies, {"B11", "B77", "B78"})

    def test_explicit_keys_do_not_pull_in_unkeyed_rows(self) -> None:
        rows = [
            {"country_key": "11", "name": "Kenya", "name_cn": "肯尼亚", "boss_company_id": "B11"},
            {"country_key": "", "name": "Djibouti", "name_cn": "吉布提", "boss_company_id": "B77"},
        ]
        scope = resolve_company_scope(["11"], rows)
        self.assertEqual([country.boss_company_id for country in scope.countries], ["B11"])

    @staticmethod
    def _country_rows() -> list[dict[str, str | None]]:
        return [
            {
                "source_id": "1",
                "country_key": SENTINEL_COUNTRY_KEY,
                "name": "ALL",
                "code": None,
                "name_cn": "全非",
                "region_key": None,
                "region_name": None,
                "boss_company_id": None,
            },
            {
                "source_id": "7",
                "country_key": "101",
                "name": "ALPHA",
                "code": "AL",
                "name_cn": "甲国",
                "region_key": None,
                "region_name": None,
                "boss_company_id": "BC-甲",
            },
            {
                "source_id": "8",
                "country_key": "202",
                "name": "BETA",
                "code": "BE",
                "name_cn": "乙国",
                "region_key": None,
                "region_name": None,
                "boss_company_id": "BC-乙",
            },
            {
                "source_id": "9",
                "country_key": None,
                "name": "GAMMA",
                "code": "GA",
                "name_cn": "丙国",
                "region_key": None,
                "region_name": None,
                "boss_company_id": "BC-丙",
            },
        ]

    def test_sentinel_expands_to_every_connectable_country(self) -> None:
        scope = resolve_company_scope((SENTINEL_COUNTRY_KEY,), self._country_rows())

        self.assertTrue(scope.all_countries)
        self.assertEqual([country.name_cn for country in scope.countries], ["甲国", "乙国", "丙国"])

    def test_sentinel_is_not_presented_as_a_country_of_its_own(self) -> None:
        scope = resolve_company_scope((SENTINEL_COUNTRY_KEY,), self._country_rows())

        self.assertNotIn("全非", [country.name_cn for country in scope.countries])
        self.assertNotIn(SENTINEL_COUNTRY_KEY, [country.country_key for country in scope.countries])

    def test_original_authorization_rows_are_preserved_verbatim(self) -> None:
        scope = resolve_company_scope((SENTINEL_COUNTRY_KEY, "101"), self._country_rows())

        self.assertEqual(scope.explicit_country_keys, (SENTINEL_COUNTRY_KEY, "101"))
        self.assertTrue(scope.all_countries)

    def test_sentinel_row_is_kept_in_the_country_table(self) -> None:
        rows = self._country_rows()

        sentinel = [row for row in rows if row["country_key"] == SENTINEL_COUNTRY_KEY]
        self.assertEqual(len(sentinel), 1)
        # 展开是解释层行为，不改写落库数据：哨兵仍可被按 country_key 查到。
        scope = resolve_company_scope(("101",), rows)
        self.assertFalse(scope.all_countries)

    def test_wildcard_covers_boss_company_ids_of_all_countries(self) -> None:
        scope = resolve_company_scope((SENTINEL_COUNTRY_KEY,), self._country_rows())

        self.assertEqual([country.boss_company_id for country in scope.countries], ["BC-甲", "BC-乙", "BC-丙"])


if __name__ == "__main__":
    unittest.main()
