"""银河导出目录的 CSV 读取（标准库 csv，无新增依赖）。

测试在临时目录里现造合成 CSV，仓库内不保存任何导出样本。
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from lingxi.adapters.galaxy_csv_export import (
    EXPORT_FILE_NAMES,
    load_export_directory,
    read_csv_table,
)

_CSV = {
    "user.csv": "﻿user_id,dept_id,user_name,nick_name,email,create_time\n"
    "U1,D1,10001,化名甲,jiaming.jia@example.invalid,2019-01-02 03:04:05\n",
    "user_role.csv": "user_id,role_id,user_name,role_name\nU1,R-甲,化名甲,A运营\n",
    "role_menu.csv": "role_id,menu_id,role_name,menu_name\nR-甲,M1,A运营,报表\n",
    "sys_user_datacountry.csv": "USER_ID,DATACOUNTRY_ID,USER_NAME,DATACOUNTRY_NAME\nU1,101,化名甲,甲国\n",
    "sys_country.csv": "id,country_key,name,code,name_cn,region_key,region_name,boss_company_id\n"
    "7,101,ALPHA,AL,甲国,1,甲区,BC-甲\n",
}


def _write_export(directory: Path, overrides: dict[str, str] | None = None) -> None:
    content = dict(_CSV)
    content.update(overrides or {})
    for name, text in content.items():
        (directory / name).write_text(text, encoding="utf-8")


class GalaxyCsvExportTest(unittest.TestCase):
    def test_five_sheets_map_to_five_csv_files(self) -> None:
        self.assertEqual(
            sorted(EXPORT_FILE_NAMES.values()),
            sorted(["user.csv", "user_role.csv", "role_menu.csv", "sys_user_datacountry.csv", "sys_country.csv"]),
        )

    def test_directory_is_read_into_five_tables(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _write_export(directory)

            bundle = load_export_directory(directory)

        self.assertEqual(sorted(bundle.tables), sorted(EXPORT_FILE_NAMES))
        self.assertEqual(bundle.tables["sys_user_datacountry"][0]["USER_ID"], "U1")

    def test_byte_order_mark_is_stripped_from_the_first_header(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _write_export(directory)

            bundle = load_export_directory(directory)

        self.assertIn("user_id", bundle.tables["user"][0])
        self.assertNotIn("﻿user_id", bundle.tables["user"][0])

    def test_digest_is_stable_for_identical_content_and_changes_with_content(self) -> None:
        with tempfile.TemporaryDirectory() as raw_a, tempfile.TemporaryDirectory() as raw_b:
            _write_export(Path(raw_a))
            _write_export(Path(raw_b))
            first = load_export_directory(Path(raw_a)).digest
            same = load_export_directory(Path(raw_b)).digest

            _write_export(Path(raw_b), {"user_role.csv": "user_id,role_id,user_name,role_name\nU1,R-乙,化名甲,A销售\n"})
            changed = load_export_directory(Path(raw_b)).digest

        self.assertEqual(first, same)
        self.assertNotEqual(first, changed)
        self.assertEqual(len(first), 64)

    def test_missing_file_is_reported_with_the_expected_names(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _write_export(directory)
            (directory / "sys_country.csv").unlink()

            with self.assertRaises(FileNotFoundError) as caught:
                load_export_directory(directory)

        self.assertIn("sys_country.csv", str(caught.exception))

    def test_row_values_are_read_as_text_without_type_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _write_export(directory)

            rows = read_csv_table(directory / "sys_country.csv")

        self.assertEqual(rows[0]["country_key"], "101")
        self.assertIsInstance(rows[0]["country_key"], str)

    def test_short_row_is_padded_with_empty_strings_not_none(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _write_export(directory, {"role_menu.csv": "role_id,menu_id,role_name,menu_name\nR-甲,M1\n"})

            rows = read_csv_table(directory / "role_menu.csv")

        self.assertEqual(rows[0]["menu_name"], "")


if __name__ == "__main__":
    unittest.main()
