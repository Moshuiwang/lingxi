"""真库建库底座自身的检查——不需要数据库，因此在任何环境都会执行。

这些用例挡的是 #54 验收清单 H-02 那类**沉默**失效：某个真库测试文件重新用回
硬编码的迁移清单，新 revision 建的对象在测试库里根本不存在，而所有断言照样绿。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from postgres_schema import (
    ALEMBIC_INI,
    MIGRATIONS_DIRECTORY,
    REPOSITORY_ROOT,
    RETENTION_REVISION,
    VERSIONS_DIRECTORY,
    revision_sql,
)

TESTS_DIRECTORY = Path(__file__).resolve().parent

# 直接扫**编号 SQL 的文件名本身**，不要求带 `migrations/` 前缀。
#
# 上一版只匹配 `migrations/010_xxx.sql` 这种带前缀的写法。独立复查把它跑在基线的
# 两个原文件上，结果是：带前缀那个文件命中 3 行，而 `test_identity_postgres_records`
# ——模块文档点名的另一处硬编码——命中 **0 行**，因为它把目录常量和文件名分成两处写。
# 另试三种等价回退写法也全部放过。一条"回到硬编码就会红"的守卫，实际只覆盖两种
# 历史写法中的一种，比没有守卫更危险。
_NUMBERED_SQL_NAME = re.compile(r"[0-9]{3}_[a-z0-9_]+\.sql")

# 唯一豁免的是本文件：下面那条否定用例必须把历史写法当字面量喂给正则。
#
# **建库底座 `postgres_schema.py` 不在豁免名单里。** 它现在一个编号文件名都不含，
# 豁免它等于给"硬编码搬回底座"开了一道无人看守的门——变异复验当场证实：把清单塞回
# 底座时，带豁免的那一版守卫不变红。
_ALLOWED_FILES = {"test_postgres_schema_fixture.py"}
_TESTING_ASSET = re.compile(r"migrations/testing/")

# 需要真库结构的测试模块必须走底座，不得自己拼建库过程。
_REAL_DATABASE_MODULES = (
    "test_galaxy_import_postgres.py",
    "test_identity_postgres_records.py",
    "test_retention_postgres.py",
    # Issue #52 的花名册审计真库断言：它读 app_user，必须由整条 alembic 链建出来的
    # 结构来跑，不能自己拼一套建库过程。
    "test_roster_audit_postgres.py",
)


class ProductionChainFixtureTest(unittest.TestCase):
    def test_the_numbered_sql_chain_is_frozen_and_carries_no_retention_ddl(self) -> None:
        """#53 冻结编号 SQL 之后，保留清理的 DDL 只能在 revision 里。"""

        numbered = sorted(path.name for path in MIGRATIONS_DIRECTORY.glob("*.sql"))

        self.assertEqual(
            numbered,
            [
                "006_create_feishu_delegated_credential.sql",
                "007_create_feishu_org_snapshot.sql",
                "008_create_app_user.sql",
                "010_create_galaxy_import_batch.sql",
                "011_create_galaxy_user_and_role.sql",
                "012_create_galaxy_country_scope.sql",
            ],
            "编号 SQL 已冻结：新增顶层 .sql 不会进入基线的 CHAIN，两条血统会分叉",
        )

    def test_the_retention_revision_exists_and_chains_onto_the_baseline(self) -> None:
        source = RETENTION_REVISION.read_text(encoding="utf-8")

        self.assertTrue(RETENTION_REVISION.is_file())
        self.assertEqual(RETENTION_REVISION.parent, VERSIONS_DIRECTORY)
        self.assertIn('revision: str = "0054_retention_cleanup"', source)
        self.assertIn('down_revision: str | None = "20260806_baseline"', source)

    def test_the_revision_carries_both_directions_of_the_ddl(self) -> None:
        upgrade, downgrade = revision_sql()

        for needle in ("lingxi_retention_cleanup", "galaxy_import_batch_fix_expiry", "CREATE ROLE"):
            with self.subTest(needle=needle):
                self.assertIn(needle, upgrade)
        for needle in (
            "DROP FUNCTION IF EXISTS public.lingxi_retention_cleanup",
            "DROP TRIGGER IF EXISTS",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, downgrade)

    def test_the_alembic_configuration_is_where_the_fixture_expects_it(self) -> None:
        self.assertTrue(ALEMBIC_INI.is_file())
        self.assertTrue(VERSIONS_DIRECTORY.is_dir())

    def test_every_real_database_module_builds_through_the_fixture(self) -> None:
        """真库模块必须从底座取建库能力，不得自己拼一套。"""

        for name in _REAL_DATABASE_MODULES:
            with self.subTest(module=name):
                source = (TESTS_DIRECTORY / name).read_text(encoding="utf-8")
                self.assertIn("from postgres_schema import", source)

    def test_no_test_hardcodes_a_production_migration_file_name(self) -> None:
        """回到硬编码清单的那一刻就变红，而不是等到某条断言在空库上"通过"。

        匹配的是文件名本身，因此把目录常量与文件名拆开写也躲不掉——上一版守卫
        恰恰漏的就是那种写法。
        """

        offenders: list[str] = []
        for path in sorted(TESTS_DIRECTORY.glob("*.py")):
            if path.name in _ALLOWED_FILES:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if _TESTING_ASSET.search(line):
                    continue  # migrations/testing/ 是测试资产，不属于生产链
                if _NUMBERED_SQL_NAME.search(line):
                    offenders.append(f"{path.relative_to(REPOSITORY_ROOT)}:{number}")

        self.assertEqual(
            offenders,
            [],
            "真库用例不得写死生产迁移文件名，改用 postgres_schema 的建库函数："
            + "、".join(offenders),
        )

    def test_the_guard_actually_catches_both_historical_hardcoding_styles(self) -> None:
        """守卫自身的否定用例。

        独立复查发现上一版守卫只认带 `migrations/` 前缀的单行写法，把"目录常量 +
        文件名分开写"整个放过了。守卫是用来防回退的，它自己失效不会有任何提示——
        所以这里直接把两种历史写法喂给它。
        """

        styles = (
            '    "migrations/010_create_galaxy_import_batch.sql",',
            '                "007_create_feishu_org_snapshot.sql",',
            '    MIGRATIONS / "008_create_app_user.sql"',
            "    root / '006_create_feishu_delegated_credential.sql'",
        )
        for style in styles:
            with self.subTest(style=style.strip()):
                self.assertIsNotNone(_NUMBERED_SQL_NAME.search(style))

        # 测试资产的引用必须仍然被放行，否则守卫会误伤 test_refresh_token_postgres。
        asset = '(Path(__file__).parents[1] / "migrations/testing/003_create_feishu_user_refresh_token.sql")'
        self.assertIsNotNone(_TESTING_ASSET.search(asset))


if __name__ == "__main__":
    unittest.main()
