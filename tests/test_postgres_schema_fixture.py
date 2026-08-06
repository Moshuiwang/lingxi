"""真库建库底座自身的检查——不需要数据库，因此在任何环境都会执行。

这些用例挡的是 #54 验收清单 H-02 那类**沉默**失效：某个真库测试文件重新用回
硬编码的迁移清单，新迁移建的对象在测试库里根本不存在，而所有断言照样绿。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from postgres_schema import (
    MIGRATIONS_DIRECTORY,
    REPOSITORY_ROOT,
    production_migrations,
    production_objects,
)

TESTS_DIRECTORY = Path(__file__).resolve().parent

# 允许写死迁移文件名的地方：底座自己（它按 glob 取，不含文件名），以及引用
# migrations/testing/ 测试资产的用例（那些资产不属于生产链）。
_PRODUCTION_MIGRATION_REFERENCE = re.compile(r"""migrations/(?!testing/|downgrades/)[0-9]{3}_[a-z_]+\.sql""")


class ProductionChainFixtureTest(unittest.TestCase):
    def test_the_chain_is_taken_from_the_directory_not_from_a_list(self) -> None:
        """底座取到的就是目录里全部顶层 SQL——一个不多，一个不少。"""

        on_disk = {path.name for path in MIGRATIONS_DIRECTORY.glob("*.sql")}
        from_fixture = {path.name for path in production_migrations()}

        self.assertEqual(from_fixture, on_disk)
        self.assertGreater(len(from_fixture), 1)

    def test_the_chain_is_applied_in_file_name_order(self) -> None:
        names = [path.name for path in production_migrations()]

        self.assertEqual(names, sorted(names))

    def test_downgrade_and_testing_assets_stay_out_of_the_production_chain(self) -> None:
        """回滚脚本若落到顶层，空库整链前滚会把刚建好的对象立刻删掉。"""

        names = {path.name for path in production_migrations()}

        self.assertTrue((MIGRATIONS_DIRECTORY / "downgrades").is_dir())
        self.assertFalse(any(name.startswith("downgrade") for name in names))
        for path in (MIGRATIONS_DIRECTORY / "downgrades").glob("*.sql"):
            self.assertNotIn(path.name, names)

    def test_the_clean_up_list_covers_the_objects_the_chain_creates(self) -> None:
        """清场清单来自对同一批文件的扫描，因此不可能漏掉新迁移建的对象。"""

        tables, functions = production_objects()

        for expected_table in ("galaxy_import_batch", "feishu_org_sync_run", "app_user"):
            self.assertIn(expected_table, tables)
        for expected_function in ("lingxi_retention_cleanup", "galaxy_import_batch_fix_expiry"):
            self.assertIn(expected_function, functions)

    def test_no_real_database_test_hardcodes_a_production_migration_file_name(self) -> None:
        """回到硬编码清单的那一刻就变红，而不是等到某条断言在空库上"通过"。"""

        offenders: list[str] = []
        for path in sorted(TESTS_DIRECTORY.glob("test_*.py")):
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if _PRODUCTION_MIGRATION_REFERENCE.search(line):
                    offenders.append(f"{path.relative_to(REPOSITORY_ROOT)}:{line_number}")

        self.assertEqual(
            offenders,
            [],
            "真库用例不得写死生产迁移文件名，改用 postgres_schema.rebuild_production_schema：" + "、".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
