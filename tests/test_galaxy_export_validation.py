"""银河导出五张表的落库前校验（V-银河-05/08/13 的纯函数部分）。

数据全部为合成的虚构内容：账号、姓名、邮箱、角色名、国家名均为化名。
"""

from __future__ import annotations

import unittest

from lingxi.core.permission.galaxy_export import (
    SOURCE_TABLES,
    TARGET_TABLES,
    normalize_column_name,
    validate_export,
)


def _valid_tables() -> dict[str, list[dict[str, str]]]:
    """一份最小但完整的合成导出：两个账号、两个角色、两个国家、一行哨兵。"""

    return {
        "user": [
            {
                "user_id": "U1",
                "dept_id": "D1",
                "user_name": "10001",
                "nick_name": "化名甲",
                "email": "jiaming.jia@example.invalid",
                "create_time": "2019-01-02 03:04:05",
            },
            {
                "user_id": "U2",
                "dept_id": "D2",
                "user_name": "10002",
                "nick_name": "化名乙",
                "email": "yiming.yi@example.invalid",
                "create_time": "2020-02-03 04:05:06",
            },
        ],
        "user_role": [
            {"user_id": "U1", "role_id": "R-甲", "user_name": "化名甲", "role_name": "A运营"},
            {"user_id": "U2", "role_id": "R-乙", "user_name": "化名乙", "role_name": "A销售"},
        ],
        "role_menu": [
            {"role_id": "R-甲", "menu_id": "M1", "role_name": "A运营", "menu_name": "报表"},
            {"role_id": "R-乙", "menu_id": "M2", "role_name": "A销售", "menu_name": "报表"},
        ],
        "sys_user_datacountry": [
            {"USER_ID": "U1", "DATACOUNTRY_ID": "101", "USER_NAME": "化名甲", "DATACOUNTRY_NAME": "甲国"},
            {"USER_ID": "U2", "DATACOUNTRY_ID": "0", "USER_NAME": "化名乙", "DATACOUNTRY_NAME": "全非"},
        ],
        "sys_country": [
            {
                "id": "7",
                "country_key": "101",
                "name": "ALPHA",
                "code": "AL",
                "name_cn": "甲国",
                "region_key": "1",
                "region_name": "甲区",
                "boss_company_id": "BC-甲",
            },
            {
                "id": "1",
                "country_key": "0",
                "name": "ALL",
                "code": "",
                "name_cn": "全非",
                "region_key": "",
                "region_name": "",
                "boss_company_id": "",
            },
        ],
    }


class GalaxyExportShapeTest(unittest.TestCase):
    def test_five_source_tables_map_to_five_target_tables(self) -> None:
        self.assertEqual(
            SOURCE_TABLES,
            ("user", "user_role", "role_menu", "sys_user_datacountry", "sys_country"),
        )
        self.assertEqual(
            [TARGET_TABLES[name] for name in SOURCE_TABLES],
            [
                "galaxy_user",
                "galaxy_user_role",
                "galaxy_role_menu",
                "galaxy_user_datacountry",
                "galaxy_country",
            ],
        )

    def test_valid_export_passes_and_reports_row_counts(self) -> None:
        report = validate_export(_valid_tables())

        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.errors, ())
        self.assertEqual(report.row_counts["user"], 2)
        self.assertEqual(report.row_counts["sys_country"], 2)


class GalaxyExportRequiredHeaderTest(unittest.TestCase):
    """终轮 Codex：解释所需表头缺失必须整批拒绝——漏列会整列破坏匹配/职能/公司数据，
    而此前静默补 None 仍能取代有效快照。"""

    def test_dropping_an_interpretation_header_rejects_the_export(self) -> None:
        cases = [
            ("user", "user_name"),
            ("user", "email"),
            ("user_role", "role_name"),
            ("sys_country", "name_cn"),
            ("sys_country", "boss_company_id"),
        ]
        for table, column in cases:
            with self.subTest(table=table, column=column):
                tables = _valid_tables()
                tables[table] = [
                    {key: value for key, value in row.items() if key != column} for row in tables[table]
                ]
                report = validate_export(tables)
                self.assertFalse(report.ok)
                self.assertIn("missing_column", [issue.rule for issue in report.errors])

    def test_duplicate_country_keys_reject_the_export(self) -> None:
        tables = _valid_tables()
        extra = dict(tables["sys_country"][0])
        extra["id"] = "999"
        extra["boss_company_id"] = "BC-别的"
        tables["sys_country"] = tables["sys_country"] + [extra]

        report = validate_export(tables)

        self.assertFalse(report.ok)
        self.assertIn("duplicate_country_key", [issue.rule for issue in report.errors])


class GalaxyExportRowNumberFidelityTest(unittest.TestCase):
    """V-银河-13 配套：结论只报行号，行号必须指向源文件的行（独立复查实测
    此前在丢行/合并后会偏移，运维会照错误行号改错数据）。"""

    def test_row_hints_survive_dropped_and_merged_rows(self) -> None:
        tables = _valid_tables()
        tables["user_role"] = [
            {"user_id": "U1", "role_id": "R1", "user_name": "姓名", "role_name": "A运营"},
            {"user_id": "", "role_id": "R1", "user_name": "空键", "role_name": "A运营"},
            {"user_id": "U1", "role_id": "R1", "user_name": "姓名", "role_name": "A运营"},
            {"user_id": "U_不存在", "role_id": "R1", "user_name": "姓名", "role_name": "A运营"},
        ]
        report = validate_export(tables)

        dangling = [issue for issue in report.errors if issue.rule == "dangling_user_id"]
        self.assertEqual(len(dangling), 1)
        # 源文件里悬空引用在第 4 行；朴素 enumerate 会因第 2 行被丢、第 3 行被合并而报 2。
        self.assertEqual(dangling[0].row_numbers, (4,))


class GalaxyExportColumnNormalizationTest(unittest.TestCase):
    """V-银河-08：源列名大小写与空白差异都能被接受，落库列名唯一。"""

    def test_uppercase_and_padded_source_headers_are_accepted(self) -> None:
        tables = _valid_tables()
        tables["user"] = [
            {" USER_ID ": "U1", "Dept_Id": "D1", "USER_NAME": "10001", "Nick_Name": "化名甲",
             "EMAIL": "jiaming.jia@example.invalid", "CREATE_TIME": "2019-01-02 03:04:05"},
            {" USER_ID ": "U2", "Dept_Id": "D2", "USER_NAME": "10002", "Nick_Name": "化名乙",
             "EMAIL": "yiming.yi@example.invalid", "CREATE_TIME": "2020-02-03 04:05:06"},
        ]

        report = validate_export(tables)

        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.rows["user"][0]["user_id"], "U1")
        self.assertEqual(report.rows["user"][0]["create_time_raw"], "2019-01-02 03:04:05")

    def test_normalized_rows_never_keep_the_source_spelling(self) -> None:
        report = validate_export(_valid_tables())

        for row in report.rows["sys_user_datacountry"]:
            self.assertNotIn("USER_ID", row)
            self.assertNotIn("DATACOUNTRY_ID", row)
            self.assertIn("user_id", row)
            self.assertIn("datacountry_id", row)

    def test_user_role_name_column_is_renamed_to_mark_the_trap(self) -> None:
        report = validate_export(_valid_tables())

        row = report.rows["user_role"][0]
        self.assertEqual(row["source_user_name"], "化名甲")
        self.assertNotIn("user_name", row)

    def test_normalize_column_name_is_case_and_whitespace_insensitive(self) -> None:
        self.assertEqual(normalize_column_name(" DATACOUNTRY_ID "), "datacountry_id")
        self.assertEqual(normalize_column_name("﻿user_id"), "user_id")


class GalaxyExportRejectionTest(unittest.TestCase):
    """V-银河-05 的校验侧：任何一条校验失败都不产生可写入的批次。"""

    def test_missing_table_is_rejected(self) -> None:
        tables = _valid_tables()
        del tables["sys_country"]

        report = validate_export(tables)

        self.assertFalse(report.ok)
        self.assertIn("missing_table", [issue.rule for issue in report.errors])

    def test_empty_sheet_is_rejected_rather_than_imported_as_zero_rows(self) -> None:
        # 空表最可能是「导错了 sheet」：放行会写出一份缺一条授权链的快照。
        tables = _valid_tables()
        tables["role_menu"] = []

        report = validate_export(tables)

        self.assertFalse(report.ok)
        self.assertIn("empty_table", [issue.rule for issue in report.errors])

    def test_missing_required_column_is_rejected(self) -> None:
        tables = _valid_tables()
        tables["role_menu"] = [{"role_id": "R-甲", "role_name": "A运营", "menu_name": "报表"}]

        report = validate_export(tables)

        self.assertFalse(report.ok)
        self.assertIn("missing_column", [issue.rule for issue in report.errors])

    def test_empty_key_value_is_rejected(self) -> None:
        tables = _valid_tables()
        tables["user_role"].append({"user_id": "", "role_id": "R-甲", "user_name": "化名甲", "role_name": "A运营"})

        report = validate_export(tables)

        self.assertFalse(report.ok)
        self.assertIn("empty_key", [issue.rule for issue in report.errors])

    def test_duplicate_primary_key_with_conflicting_payload_is_rejected(self) -> None:
        tables = _valid_tables()
        tables["user"].append(
            {
                "user_id": "U1",
                "dept_id": "D9",
                "user_name": "19999",
                "nick_name": "化名丙",
                "email": "bingming.bing@example.invalid",
                "create_time": "2021-03-04 05:06:07",
            }
        )

        report = validate_export(tables)

        self.assertFalse(report.ok)
        self.assertIn("conflicting_duplicate", [issue.rule for issue in report.errors])

    def test_exact_duplicate_row_is_deduplicated_with_a_warning(self) -> None:
        tables = _valid_tables()
        tables["user_role"].append(dict(tables["user_role"][0]))

        report = validate_export(tables)

        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.row_counts["user_role"], 2)
        self.assertIn("duplicate_row", [issue.rule for issue in report.warnings])

    def test_exact_duplicate_user_row_rejects_the_whole_export(self) -> None:
        # 账号实体的原始多行不能在导入期折叠成“唯一命中”；字段相同也拒绝整批。
        tables = _valid_tables()
        tables["user"].append(dict(tables["user"][0]))

        report = validate_export(tables)

        self.assertFalse(report.ok)
        self.assertIn("duplicate_row", [issue.rule for issue in report.errors])
        self.assertNotIn("duplicate_row", [issue.rule for issue in report.warnings])

    def test_role_row_of_unknown_account_is_rejected(self) -> None:
        tables = _valid_tables()
        tables["user_role"].append({"user_id": "U-未知", "role_id": "R-甲", "user_name": "化名丁", "role_name": "A运营"})

        report = validate_export(tables)

        self.assertFalse(report.ok)
        self.assertIn("dangling_user_id", [issue.rule for issue in report.errors])

    def test_country_authorization_that_cannot_be_joined_is_rejected(self) -> None:
        tables = _valid_tables()
        tables["sys_user_datacountry"].append(
            {"USER_ID": "U1", "DATACOUNTRY_ID": "999", "USER_NAME": "化名甲", "DATACOUNTRY_NAME": "未知国"}
        )

        report = validate_export(tables)

        self.assertFalse(report.ok)
        self.assertIn("dangling_country_key", [issue.rule for issue in report.errors])

    def test_country_row_without_country_key_is_only_a_warning(self) -> None:
        tables = _valid_tables()
        tables["sys_country"].append(
            {
                "id": "500",
                "country_key": "",
                "name": "GAMMA",
                "code": "GA",
                "name_cn": "丙国",
                "region_key": "",
                "region_name": "",
                "boss_company_id": "BC-丙",
            }
        )

        report = validate_export(tables)

        self.assertTrue(report.ok, report.errors)
        self.assertIn("country_without_key", [issue.rule for issue in report.warnings])

    def test_role_without_any_menu_is_only_a_warning(self) -> None:
        tables = _valid_tables()
        tables["user_role"].append({"user_id": "U1", "role_id": "R-空", "user_name": "化名甲", "role_name": "临时角色"})

        report = validate_export(tables)

        self.assertTrue(report.ok, report.errors)
        self.assertIn("role_without_menu", [issue.rule for issue in report.warnings])

    def test_unknown_source_columns_are_ignored_with_a_warning(self) -> None:
        tables = _valid_tables()
        for row in tables["sys_country"]:
            row["mall_company_id"] = "X"

        report = validate_export(tables)

        self.assertTrue(report.ok, report.errors)
        self.assertIn("ignored_column", [issue.rule for issue in report.warnings])
        self.assertNotIn("mall_company_id", report.rows["sys_country"][0])

    def test_boss_company_id_is_kept_because_mcp_permission_requests_need_it(self) -> None:
        report = validate_export(_valid_tables())

        self.assertEqual(report.rows["sys_country"][0]["boss_company_id"], "BC-甲")


class GalaxyExportIssueRedactionTest(unittest.TestCase):
    """V-银河-13：校验信息只含表名、列名、行号与计数，不回显人员数据。"""

    def test_issue_text_never_echoes_personal_values(self) -> None:
        tables = _valid_tables()
        tables["user_role"].append(
            {"user_id": "U-未知", "role_id": "R-甲", "user_name": "化名丁", "role_name": "A运营"}
        )
        tables["user"].append(
            {
                "user_id": "",
                "dept_id": "D3",
                "user_name": "10003",
                "nick_name": "化名丙",
                "email": "bingming.bing@example.invalid",
                "create_time": "2021-03-04 05:06:07",
            }
        )

        report = validate_export(tables)
        text = "\n".join(f"{issue.table}|{issue.rule}|{issue.detail}" for issue in report.errors + report.warnings)

        self.assertFalse(report.ok)
        for secret in ("化名丁", "化名丙", "bingming.bing@example.invalid", "U-未知", "10003"):
            self.assertNotIn(secret, text)
        self.assertIn("user_role", text)

    def test_issue_carries_row_numbers_for_locating_without_values(self) -> None:
        tables = _valid_tables()
        tables["role_menu"].append({"role_id": "R-甲", "menu_id": "", "role_name": "A运营", "menu_name": "报表"})

        report = validate_export(tables)
        empty_key = [issue for issue in report.errors if issue.rule == "empty_key"]

        self.assertEqual(len(empty_key), 1)
        self.assertEqual(empty_key[0].row_numbers, (3,))


if __name__ == "__main__":
    unittest.main()
