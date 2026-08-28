"""银河五表落库导入的真实 PostgreSQL 测试（V-银河-04/05/06/07/08/12）。

缺少 `LINGXI_POSTGRES_DSN` 时明确跳过，不静默通过。
数据全部为虚构化名，不含任何真实导出内容。
"""

from __future__ import annotations

import os
import unittest

from postgres_schema import force_rebuild_schema, psycopg_available

_DSN = os.environ.get("LINGXI_POSTGRES_DSN")
_SKIP_REASON = (
    "仅在 CI 或本机受控 PostgreSQL 中运行：未设置 LINGXI_POSTGRES_DSN"
    if not _DSN
    else "仅在 CI 或本机受控 PostgreSQL 中运行：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动"
)

_GALAXY_TABLES = (
    "galaxy_user",
    "galaxy_user_role",
    "galaxy_role_menu",
    "galaxy_user_datacountry",
    "galaxy_country",
    "galaxy_import_batch",
)


def _tables() -> dict[str, list[dict[str, str]]]:
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
            {"user_id": "U1", "role_id": "R-甲", "user_name": "化名乙", "role_name": "A运营"},
            {"user_id": "U2", "role_id": "R-乙", "user_name": "化名甲", "role_name": "A销售"},
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


@unittest.skipUnless(bool(_DSN) and psycopg_available(), _SKIP_REASON)
class GalaxyImportPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        cls._psycopg = psycopg
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]
        # 建的是**整条 alembic 链**，不是这几张银河表：保留清理 revision 同时引用
        # 银河与组织快照两侧，按表挑迁移建不起来；而 #53 之后表结构的权威来源就是
        # revision 链本身，这里与生产、与迁移门禁同源。
        cls._head = force_rebuild_schema(cls._dsn)

    def setUp(self) -> None:
        from lingxi.adapters.galaxy_import import PostgresGalaxyImportStore

        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            # TRUNCATE 而非 DELETE：删除防线会拒绝删未到期行，见 0054 revision。
            cursor.execute("TRUNCATE galaxy_import_batch CASCADE")
        self.store = PostgresGalaxyImportStore(self._dsn)

    def _count(self, table: str, where: str = "") -> int:
        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT count(*) FROM {table} {where}")
            return int(cursor.fetchone()[0])

    def _execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(query, parameters)

    def _scalar(self, query: str, parameters: tuple[object, ...] = ()) -> object:
        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(query, parameters)
            row = cursor.fetchone()
        return None if row is None else row[0]

    def _import(self, tables: dict[str, list[dict[str, str]]] | None = None, *, digest: str = "digest-1"):
        return self.store.import_export(
            source_label="合成导出（测试）",
            source_digest=digest,
            tables=tables if tables is not None else _tables(),
        )

    def test_valid_export_is_written_and_read_back(self) -> None:
        result = self._import()

        self.assertEqual(result.outcome, "imported")
        self.assertEqual(self._count("galaxy_user"), 2)
        self.assertEqual(self._count("galaxy_user_role"), 2)
        self.assertEqual(self._count("galaxy_role_menu"), 2)
        self.assertEqual(self._count("galaxy_user_datacountry"), 2)
        self.assertEqual(self._count("galaxy_country"), 2)
        self.assertEqual(self._scalar("SELECT status FROM galaxy_import_batch WHERE id = %s", (result.batch_id,)), "complete")
        self.assertEqual(
            self._scalar("SELECT user_row_count FROM galaxy_import_batch WHERE id = %s", (result.batch_id,)),
            2,
        )
        self.assertEqual(result.row_counts["user"], 2)

    def test_uppercase_source_columns_land_in_normalized_columns(self) -> None:
        """V-银河-08 的落库侧：大写源列名落到唯一的小写列。"""

        self._import()

        self.assertEqual(
            self._scalar("SELECT datacountry_id FROM galaxy_user_datacountry WHERE user_id = 'U1'"),
            "101",
        )
        self.assertEqual(
            self._scalar(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'galaxy_user_datacountry' AND column_name <> lower(column_name)"
            ),
            0,
        )

    def test_all_africa_sentinel_row_is_stored_verbatim(self) -> None:
        """V-银河-04 的落库侧：哨兵行原样保留，不在导入期展开或丢弃。"""

        self._import()

        self.assertEqual(
            self._scalar("SELECT name_cn FROM galaxy_country WHERE country_key = '0'"),
            "全非",
        )
        self.assertEqual(self._scalar("SELECT name FROM galaxy_country WHERE country_key = '0'"), "ALL")
        self.assertEqual(self._count("galaxy_user_datacountry", "WHERE datacountry_id = '0'"), 1)

    def test_misleading_user_name_column_is_stored_under_its_own_name(self) -> None:
        self._import()

        # 落库保留原值，但列名改成 source_user_name，杜绝按它连接。
        self.assertEqual(
            self._scalar("SELECT source_user_name FROM galaxy_user_role WHERE user_id = 'U1'"),
            "化名乙",
        )
        self.assertEqual(self._scalar("SELECT user_name FROM galaxy_user WHERE user_id = 'U1'"), "10001")
        self.assertEqual(
            self._scalar(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'galaxy_user_role' AND column_name = 'user_name'"
            ),
            0,
        )

    def test_boss_company_id_is_kept_and_registered_as_the_mcp_company_field(self) -> None:
        """V-银河-12：列保留且注释里登记了它的用途。"""

        comment = self._scalar(
            "SELECT col_description('galaxy_country'::regclass, ordinal_position) "
            "FROM information_schema.columns "
            "WHERE table_name = 'galaxy_country' AND column_name = 'boss_company_id'"
        )

        self.assertIsNotNone(comment)
        self.assertIn("MCP", str(comment))

    def test_galaxy_tables_do_not_touch_the_identity_slice(self) -> None:
        foreign_tables = self._scalar(
            """
            SELECT count(*)
              FROM information_schema.table_constraints tc
              JOIN information_schema.constraint_column_usage ccu
                ON tc.constraint_name = ccu.constraint_name
             WHERE tc.constraint_type = 'FOREIGN KEY'
               AND tc.table_name LIKE 'galaxy\\_%%'
               AND ccu.table_name NOT LIKE 'galaxy\\_%%'
            """
        )

        self.assertEqual(foreign_tables, 0)

    def test_validation_failure_writes_no_batch_and_no_row(self) -> None:
        """V-银河-05：校验不通过时一行都不写。"""

        tables = _tables()
        tables["user_role"].append({"user_id": "U-未知", "role_id": "R-甲", "user_name": "化名丁", "role_name": "A运营"})

        result = self._import(tables)

        self.assertEqual(result.outcome, "rejected")
        self.assertIsNone(result.batch_id)
        self.assertIn("dangling_user_id", [issue.rule for issue in result.errors])
        for table in _GALAXY_TABLES:
            self.assertEqual(self._count(table), 0, table)

    def test_failure_during_write_leaves_no_half_batch(self) -> None:
        """V-银河-05：写入中途失败时整批回滚，不留半批。"""

        from lingxi.adapters.galaxy_import import PostgresGalaxyImportStore

        class _FailingStore(PostgresGalaxyImportStore):
            def _insert_rows(self, cursor, source_table, rows):  # type: ignore[no-untyped-def]
                if source_table == "sys_country":
                    raise RuntimeError("注入的写入失败")
                return super()._insert_rows(cursor, source_table, rows)

        store = _FailingStore(self._dsn)

        with self.assertRaises(RuntimeError):
            store.import_export(source_label="合成导出（测试）", source_digest="digest-fail", tables=_tables())

        for table in _GALAXY_TABLES:
            self.assertEqual(self._count(table), 0, table)

    def test_readback_mismatch_aborts_the_batch(self) -> None:
        """V-银河-07：回读计数对不上即整批回滚，批次不得标记完成。"""

        from lingxi.adapters.galaxy_import import GalaxyImportVerificationError, PostgresGalaxyImportStore

        class _LosingStore(PostgresGalaxyImportStore):
            def _insert_rows(self, cursor, source_table, rows):  # type: ignore[no-untyped-def]
                written = super()._insert_rows(cursor, source_table, rows)
                if source_table == "user":
                    cursor.execute("DELETE FROM galaxy_user WHERE user_id = 'U2'")
                return written

        store = _LosingStore(self._dsn)

        with self.assertRaises(GalaxyImportVerificationError):
            store.import_export(source_label="合成导出（测试）", source_digest="digest-lost", tables=_tables())

        for table in _GALAXY_TABLES:
            self.assertEqual(self._count(table), 0, table)

    def test_repeated_import_of_the_same_digest_is_idempotent(self) -> None:
        """V-银河-06：同一份导出重复导入不产生第二份数据。"""

        first = self._import()
        second = self._import()

        self.assertEqual(second.outcome, "already_imported")
        self.assertEqual(second.batch_id, first.batch_id)
        self.assertEqual(self._count("galaxy_import_batch"), 1)
        self.assertEqual(self._count("galaxy_user"), 2)

    def test_a_new_export_supersedes_the_previous_complete_batch(self) -> None:
        first = self._import()
        second = self._import(digest="digest-2")

        self.assertEqual(second.outcome, "imported")
        self.assertNotEqual(second.batch_id, first.batch_id)
        self.assertEqual(self._scalar("SELECT status FROM galaxy_import_batch WHERE id = %s", (first.batch_id,)), "superseded")
        self.assertEqual(self._scalar("SELECT status FROM galaxy_import_batch WHERE id = %s", (second.batch_id,)), "complete")
        self.assertEqual(self.store.current_batch_id(), second.batch_id)

    def test_a_superseded_digest_is_still_idempotent_and_never_rolls_back(self) -> None:
        """Codex 复查 P1：导 A → 导 B（A 转 superseded）→ 误重导 A，不得写出
        A 的第二份全量、更不得把 B 降级让过期快照重新生效。"""
        first = self._import()
        second = self._import(digest="digest-2")

        replay = self._import()

        self.assertEqual(replay.outcome, "already_imported")
        self.assertEqual(replay.batch_id, first.batch_id)
        self.assertEqual(self._scalar("SELECT count(*) FROM galaxy_import_batch"), 2)
        self.assertEqual(self._scalar("SELECT status FROM galaxy_import_batch WHERE id = %s", (second.batch_id,)), "complete")
        self.assertEqual(self.store.current_batch_id(), second.batch_id)

    def test_a_confirmed_unchanged_export_becomes_a_fresh_current_batch(self) -> None:
        """终轮 Codex 的另一半：真实的新导出恰与历史内容相同（A→B→A 或连续
        刷新无变化）时，经显式确认应作为**新批次**落库并成为当前有效；
        默认（不确认）仍走防误重导的幂等路径。"""
        first = self._import()
        second = self._import(digest="digest-2")

        blocked = self._import()
        self.assertEqual(blocked.outcome, "already_imported")

        confirmed = self.store.import_export(
            source_label="重放确认",
            source_digest="digest-1",
            tables=_tables(),
            confirm_unchanged=True,
        )

        self.assertEqual(confirmed.outcome, "imported")
        self.assertNotIn(confirmed.batch_id, (first.batch_id, second.batch_id))
        self.assertEqual(self.store.current_batch_id(), confirmed.batch_id)

    def test_an_expired_batch_is_not_the_current_batch(self) -> None:
        """Codex 复查 P1：过期批次不算「当前有效」——清理没跑不等于快照还新鲜。

        构造方式随 #54 改了：到期时间现在由触发器锚定在 `started_at + 2160h` 上，
        直接 UPDATE `expires_at` 会被覆盖回去。要得到一个过期批次，只能让它的
        **来源时间**足够旧——这正是"到期时间不可后移"想要的效果，断言本身没变。
        """
        self._execute(
            "INSERT INTO galaxy_import_batch "
            "(id, source_label, source_digest, status, started_at, completed_at) "
            "VALUES ('gib_stale', '过期导出（测试）', 'digest-stale', 'complete', %s, %s)",
            ("2026-01-01 00:00:00+00", "2026-01-01 00:00:00+00"),
        )

        self.assertEqual(self._count("galaxy_import_batch", "WHERE status = 'complete'"), 1)
        self.assertIsNone(self.store.current_batch_id())

    def test_batch_records_source_label_and_row_counts_per_table(self) -> None:
        result = self._import()

        row = self._scalar(
            "SELECT concat_ws('|', source_label, user_row_count, user_role_row_count, role_menu_row_count, "
            "user_datacountry_row_count, country_row_count) FROM galaxy_import_batch WHERE id = %s",
            (result.batch_id,),
        )

        self.assertEqual(row, "合成导出（测试）|2|2|2|2|2")

    def test_batch_rows_carry_an_expiry_for_controlled_cleanup(self) -> None:
        result = self._import()

        self.assertIsNotNone(
            self._scalar("SELECT expires_at FROM galaxy_import_batch WHERE id = %s", (result.batch_id,))
        )

    def test_the_normal_import_path_is_not_broken_by_the_expiry_trigger(self) -> None:
        """D-04：`staging → complete` 是导入器的正常收尾路径（galaxy_import.py:218）。

        #54 给这张表补上 `BEFORE INSERT OR UPDATE` 触发器之后，最容易出的事故就是
        把这条路径打断——它每次导入都要跑，而触发器会拒绝改来源时间。这里确认
        导入照常成功，且到期时间在 INSERT 与 UPDATE 两次触发后仍锚在同一个来源时间上。
        """
        result = self._import()

        self.assertEqual(result.outcome, "imported")
        self.assertEqual(
            self._scalar("SELECT status FROM galaxy_import_batch WHERE id = %s", (result.batch_id,)),
            "complete",
        )
        self.assertTrue(
            self._scalar(
                "SELECT expires_at = started_at + interval '2160 hours' "
                "FROM galaxy_import_batch WHERE id = %s",
                (result.batch_id,),
            )
        )


if __name__ == "__main__":
    unittest.main()
