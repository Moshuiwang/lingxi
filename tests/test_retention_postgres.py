"""九十天保留清理机制的真实 PostgreSQL 断言（Issue #54 / S9）。

认领断言：V-保留-01…20（[验证与门禁](../docs/技术设计/验证与门禁.md)「数据保留清理」）。

缺 ``LINGXI_POSTGRES_DSN`` 时整类跳过并说明原因，不静默通过：这里几乎每一条
断言的对象都是数据库自己的行为——触发器、级联、函数权限、搜索路径——在没有真库的
环境里"通过"只意味着一行都没执行。

数据全部为虚构化名，不含任何真实人员数据。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
import unittest
from datetime import timedelta

from postgres_schema import (
    REPOSITORY_ROOT,
    RETENTION_REVISION,
    applied_head,
    ensure_production_schema,
    force_rebuild_schema,
    revision_sql,
)

SKIP_REASON = "跳过：未设置 LINGXI_POSTGRES_DSN，保留清理断言未验证（需真实 PostgreSQL 16）"

RETENTION_WINDOW = timedelta(hours=2160)
# DDL 自 #53 起只存在于 revision 文件里（编号 SQL 已冻结），要重放它就从那里取。
UPGRADE_SQL, DOWNGRADE_SQL = revision_sql()

# 8 条 ON DELETE CASCADE 边，逐条列出（子表, 父表, 外键列）。
# 合并成一个总数会让"漏了一张表"和"多删了一张表"表现成同一个数字。
CASCADE_EDGES = (
    ("feishu_org_tenant_snapshot", "feishu_org_sync_run", "sync_run_id"),
    ("feishu_org_department_snapshot", "feishu_org_sync_run", "sync_run_id"),
    ("feishu_org_member_snapshot", "feishu_org_sync_run", "sync_run_id"),
    ("galaxy_user", "galaxy_import_batch", "batch_id"),
    ("galaxy_user_role", "galaxy_import_batch", "batch_id"),
    ("galaxy_role_menu", "galaxy_import_batch", "batch_id"),
    ("galaxy_country", "galaxy_import_batch", "batch_id"),
    ("galaxy_user_datacountry", "galaxy_import_batch", "batch_id"),
)

# 清理绝不能触达的表：它们承载"继续提供服务所需的当前状态"，不在九十天删除范围内
# （数据库设计 :596），而且一条外键都没有，不可能被级联带走。
UNTOUCHED_TABLES = ("feishu_delegated_subject", "app_user")


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN"), SKIP_REASON)
class RetentionPostgresTestCase(unittest.TestCase):
    """所有保留清理用例的共同底座。"""

    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        cls._psycopg = psycopg
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        # 用 TRUNCATE 而不是 DELETE：本 revision 的 BEFORE DELETE 防线会拒绝删除
        # **未到期**的行，而用例造的数大多是未到期的。TRUNCATE 不经过行级触发器，
        # 正好用来做与业务语义无关的清场。这不是绕过防线——防线针对的是持有 DELETE
        # 的业务角色，而没有任何 lingxi_* 角色被授予 TRUNCATE（断言 V-保留-21）。
        # lock_timeout 不是可有可无的保险。TRUNCATE 要 ACCESS EXCLUSIVE 锁，
        # 只要有任何一条用例漏关了持锁连接，这里就会**无限等待**——整个套件挂死，
        # 在 CI 上表现为烧完 job 超时且没有任何指向原因的输出。挂死是最坏的失败形态：
        # 它既不给结论也不给线索。设成 5 秒，泄漏立刻变成一条指名道姓的错误。
        self.execute(
            "SET lock_timeout = '5s';"
            "TRUNCATE galaxy_import_batch, feishu_org_sync_run, app_user, feishu_delegated_subject CASCADE"
        )

    # ---- 基础工具 ---------------------------------------------------------

    def execute(self, sql: str, parameters: tuple = ()) -> None:
        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(sql, parameters or None)

    def query(self, sql: str, parameters: tuple = ()) -> list[tuple]:
        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(sql, parameters or None)
            return cursor.fetchall()

    def scalar(self, sql: str, parameters: tuple = ()):
        rows = self.query(sql, parameters)
        return rows[0][0] if rows else None

    def count(self, table: str, where: str = "", parameters: tuple = ()) -> int:
        return int(self.scalar(f"SELECT count(*) FROM {table} {where}", parameters))

    def database_now(self):
        return self.scalar("SELECT now()")

    def cleanup_with_deadline(
        self, moment, limit: int = 100, deadline: str = "15s"
    ) -> dict[str, tuple[int, object, object, bool]]:
        """带客户端死线的清理调用，只给并发用例使用。

        函数自己有 `lock_timeout`，正常情况下轮不到这条死线。它存在是为了让
        「有人把 lock_timeout 摘掉」这件事表现为**一条失败**而不是**一次挂死**：
        并发用例里持锁连接要等清理返回才释放，没有死线的话两边互等，整个套件停在
        那里烧完 CI 的 job 超时，还不给任何指向原因的输出。
        """

        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(f"SET statement_timeout = '{deadline}'")
            cursor.execute(
                "SELECT target_table, deleted_rows, oldest_expires_at, newest_expires_at, blocked "
                "FROM public.lingxi_retention_cleanup(%s, %s)",
                (moment, limit),
            )
            rows = cursor.fetchall()
        return {str(row[0]): (int(row[1]), row[2], row[3], bool(row[4])) for row in rows}

    def cleanup(self, moment, limit: int = 100) -> dict[str, tuple[int, object, object, bool]]:
        """调用受限清理函数，返回 {表名: (删除数, 最早到期, 最晚到期, 是否因锁让路)}。"""

        rows = self.query(
            "SELECT target_table, deleted_rows, oldest_expires_at, newest_expires_at, blocked "
            "FROM public.lingxi_retention_cleanup(%s, %s)",
            (moment, limit),
        )
        return {str(row[0]): (int(row[1]), row[2], row[3], bool(row[4])) for row in rows}

    # ---- 造数 -------------------------------------------------------------

    def insert_batch(self, batch_id: str, *, started_at, status: str = "complete", with_children: bool = False) -> None:
        """写一个银河导入批次。到期时间由触发器从 `started_at` 推导，不能自己指定。"""

        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO galaxy_import_batch "
                "(id, source_label, source_digest, status, started_at, completed_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (batch_id, f"合成导出 {batch_id}", f"digest-{batch_id}", status, started_at, started_at),
            )
            if not with_children:
                return
            cursor.execute("INSERT INTO galaxy_user (batch_id, user_id, email) VALUES (%s, 'U1', %s)", (batch_id, f"{batch_id}@example.invalid"))
            cursor.execute("INSERT INTO galaxy_user_role (batch_id, user_id, role_id) VALUES (%s, 'U1', 'R1')", (batch_id,))
            cursor.execute("INSERT INTO galaxy_role_menu (batch_id, role_id, menu_id) VALUES (%s, 'R1', 'M1')", (batch_id,))
            cursor.execute("INSERT INTO galaxy_country (batch_id, source_id, country_key) VALUES (%s, '7', '101')", (batch_id,))
            cursor.execute(
                "INSERT INTO galaxy_user_datacountry (batch_id, user_id, datacountry_id) VALUES (%s, 'U1', '101')",
                (batch_id,),
            )

    def insert_sync_run(self, run_id: str, *, started_at, status: str = "complete") -> None:
        """写一个组织快照批次。

        父行与三张子表必须在**同一个事务**里写：007 的 DEFERRABLE 约束触发器在提交
        时核对声明计数与实际子行，分两次提交的话第一次就会被它拒绝。
        """

        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO feishu_org_sync_run "
                "(id, source_app_id, status, started_at, completed_at, expires_at, "
                " tenant_count, department_count, member_count) "
                "VALUES (%s, 'cli_fake', %s, %s, %s, %s, 1, 1, 1)",
                (run_id, status, started_at, started_at, started_at),
            )
            cursor.execute(
                "INSERT INTO feishu_org_tenant_snapshot "
                "(id, sync_run_id, tenant_key, visible_to_user_identity, member_count) "
                "VALUES (%s, %s, 'tenant_a', true, 1)",
                (f"{run_id}_tenant", run_id),
            )
            cursor.execute(
                "INSERT INTO feishu_org_department_snapshot "
                "(id, sync_run_id, tenant_key, department_key, name) "
                "VALUES (%s, %s, 'tenant_a', 'dept_a', '测试部门')",
                (f"{run_id}_dept", run_id),
            )
            cursor.execute(
                "INSERT INTO feishu_org_member_snapshot "
                "(id, sync_run_id, tenant_key, member_key, open_id, user_id, union_id, display_name) "
                "VALUES (%s, %s, 'tenant_a', 'm1', 'ou_hua_ming', 'user_hua_ming', 'union_hua_ming', '化名甲')",
                (f"{run_id}_member", run_id),
            )

    def insert_untouched_rows(self) -> None:
        self.execute(
            "INSERT INTO feishu_delegated_subject (purpose, subject_open_id) "
            "VALUES ('org_directory_sync', 'ou_delegated_subject')"
        )
        self.execute("INSERT INTO app_user (id) VALUES ('usr_untouched')")


# ---------------------------------------------------------------------------
# V-保留-01 / 02 / 03 / 04 / 05：到期回收与边界
# ---------------------------------------------------------------------------
class ExpiryCollectionTest(RetentionPostgresTestCase):
    def test_expired_rows_in_both_parent_tables_are_removed_in_one_call(self) -> None:
        """V-保留-01 / 02（验收 A-01/A-02/A-04）。

        两张父表在同一次调用里各删各的。只实现一张的话，另一张的删除数会是 0。
        """
        now = self.database_now()
        self.insert_batch("gib_old", started_at=now - RETENTION_WINDOW - timedelta(seconds=1))
        self.insert_sync_run("run_old", started_at=now - RETENTION_WINDOW - timedelta(seconds=1))

        summary = self.cleanup(now)

        self.assertEqual(set(summary), {"galaxy_import_batch", "feishu_org_sync_run"})
        self.assertEqual(summary["galaxy_import_batch"][0], 1)
        self.assertEqual(summary["feishu_org_sync_run"][0], 1)
        self.assertEqual(self.count("galaxy_import_batch"), 0)
        self.assertEqual(self.count("feishu_org_sync_run"), 0)

    def test_a_row_that_expires_one_second_later_is_untouched_field_by_field(self) -> None:
        """V-保留-01（验收 A-03）：未到期的行**逐字段**未变，不只是"还在"。"""
        now = self.database_now()
        self.insert_batch("gib_fresh", started_at=now - RETENTION_WINDOW + timedelta(seconds=1))
        before = self.scalar("SELECT to_jsonb(t) FROM galaxy_import_batch t WHERE id = 'gib_fresh'")

        self.cleanup(now)

        after = self.scalar("SELECT to_jsonb(t) FROM galaxy_import_batch t WHERE id = 'gib_fresh'")
        self.assertIsNotNone(after)
        self.assertEqual(before, after)

    def test_tables_outside_the_retention_scope_are_never_touched(self) -> None:
        """V-保留-01（验收 A-05）：`feishu_delegated_subject` 与 `app_user` 一行不动。"""
        now = self.database_now()
        self.insert_untouched_rows()
        self.insert_batch("gib_old", started_at=now - RETENTION_WINDOW - timedelta(hours=1))
        self.insert_sync_run("run_old", started_at=now - RETENTION_WINDOW - timedelta(hours=1))
        before = {table: self.count(table) for table in UNTOUCHED_TABLES}

        self.cleanup(now)

        for table in UNTOUCHED_TABLES:
            with self.subTest(table=table):
                self.assertEqual(self.count(table), before[table])
                self.assertEqual(self.count(table), 1)

    def test_the_expiry_boundary_is_inclusive_and_exact_to_the_second(self) -> None:
        """V-保留-03（验收 A-06/A-07/A-08）：`<=` 语义逐点验证。

        恰好到 2160 小时的那一行**必须**被删——这一点是 `<=` 与 `<` 的唯一区别，
        也是"九十天是上限而不是至少保留九十天"这句话在数据库里的落点。
        """
        now = self.database_now()
        self.insert_batch("gib_before", started_at=now - RETENTION_WINDOW + timedelta(seconds=1))
        self.insert_batch("gib_exact", started_at=now - RETENTION_WINDOW)
        self.insert_batch("gib_after", started_at=now - RETENTION_WINDOW - timedelta(seconds=1))

        self.cleanup(now)

        surviving = {row[0] for row in self.query("SELECT id FROM galaxy_import_batch")}
        self.assertEqual(surviving, {"gib_before"}, "恰好到期与已过期的都应被删，只差 1 秒未到期的应留下")

    def test_the_cleanup_condition_and_the_current_batch_filter_are_complementary(self) -> None:
        """V-保留-04（验收 A-09/B-01）：不存在"既是当前批次、又可被清理"的瞬间。

        `current_batch_id()` 取 `expires_at > now()`，清理取 `expires_at <= p_now`。
        两个条件在同一时刻严格互补，所以"当前批次被 CASCADE 清理"不可能发生。
        本条断言的意义是防止未来有人去掉 `current_batch_id()` 的过期过滤——
        那样这里就会同时看到"它是当前批次"和"它被删掉了"。
        """
        from lingxi.adapters.galaxy_import import PostgresGalaxyImportStore

        now = self.database_now()
        self.insert_batch("gib_expired", started_at=now - RETENTION_WINDOW - timedelta(hours=1), with_children=True)
        self.insert_batch("gib_current", started_at=now - RETENTION_WINDOW + timedelta(hours=1), with_children=True)
        store = PostgresGalaxyImportStore(self._dsn)

        self.assertEqual(store.current_batch_id(), "gib_current")
        summary = self.cleanup(now)

        self.assertEqual(summary["galaxy_import_batch"][0], 1)
        self.assertEqual(store.current_batch_id(), "gib_current", "当前批次在清理前后必须是同一个")
        for child, parent, column in CASCADE_EDGES:
            if parent != "galaxy_import_batch":
                continue
            with self.subTest(child=child):
                self.assertEqual(self.count(child, f"WHERE {column} = 'gib_current'"), 1)

    def test_the_window_is_2160_absolute_hours_not_ninety_calendar_days(self) -> None:
        """V-保留-05（验收 A-10）：跨夏令时的时区里，两种写法差一个小时。

        `interval '90 days'` 是日历量，跨 DST 会漂 1 小时；`interval '2160 hours'`
        是绝对时长，不漂。上限如果会漂，"九十天上限"就不是一个可核对的承诺。
        """
        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            # America/Santiago 在 9 月第一个周日进入夏令时，8 月 1 日起算的 90 天跨过它。
            cursor.execute("SET TIME ZONE 'America/Santiago'")
            cursor.execute(
                "INSERT INTO galaxy_import_batch (id, source_label, source_digest, status, started_at) "
                "VALUES ('gib_dst', 'DST', 'digest-dst', 'staging', %s)",
                ("2026-08-01 00:00:00-04",),
            )
            cursor.execute(
                "SELECT extract(epoch FROM (expires_at - started_at))::bigint, "
                "       expires_at = started_at + interval '90 days' "
                "  FROM galaxy_import_batch WHERE id = 'gib_dst'"
            )
            elapsed_seconds, matches_ninety_days = cursor.fetchone()

        self.assertEqual(elapsed_seconds, 2160 * 3600)
        self.assertFalse(matches_ninety_days, "跨夏令时切换时 90 日历日与 2160 小时必须落在不同时刻")


# ---------------------------------------------------------------------------
# V-保留-20：到期即删，不保留最近完成批次
# ---------------------------------------------------------------------------
class NoUnconditionalReprieveTest(RetentionPostgresTestCase):
    def test_every_expired_batch_disappears_including_the_most_recent_one(self) -> None:
        """V-保留-20（验收 B-02/B-03/B-05）：编排者裁定 ② 选项 B。

        豁免**只**由 `expires_at <= p_now` 表达。构造一个"全部批次都已过期"的库，
        连最近完成的那个也必须被删——任何"排除最近 complete 批次"的无条件保护都会
        让这条变红。下游因此看到 `current_batch_id() is None`，安全失败，
        而它在删除之前就已经是 None 了（过期即不算当前）。
        """
        from lingxi.adapters.galaxy_import import PostgresGalaxyImportStore

        now = self.database_now()
        for index in range(3):
            self.insert_batch(
                f"gib_{index}",
                started_at=now - RETENTION_WINDOW - timedelta(hours=index + 1),
                status="superseded" if index else "complete",
                with_children=True,
            )
        store = PostgresGalaxyImportStore(self._dsn)
        self.assertIsNone(store.current_batch_id(), "过期批次在删除前就已经不算当前有效")

        summary = self.cleanup(now)

        self.assertEqual(summary["galaxy_import_batch"][0], 3)
        self.assertEqual(self.count("galaxy_import_batch"), 0)
        self.assertIsNone(store.current_batch_id())

    def test_removing_the_last_org_snapshot_turns_stale_into_unavailable(self) -> None:
        """V-保留-20（验收 B-04）：组织快照侧的对称后果，知情接受。

        删掉过期的 `feishu_org_sync_run` 之后，可观察状态由 STALE 变 UNAVAILABLE。
        两个状态都不建档、都不产生候选，用户可见结果不变（编排者裁定 ④）。
        """
        from lingxi.adapters.postgres_identity import PostgresOrgSnapshotStore
        from lingxi.core.identity.org_snapshot import DirectoryAvailability, directory_availability

        now = self.database_now()
        self.insert_sync_run("run_stale", started_at=now - RETENTION_WINDOW - timedelta(hours=1))
        store = PostgresOrgSnapshotStore(self._dsn)

        before = directory_availability(store.latest_complete_expiry(), now)
        self.assertIs(before, DirectoryAvailability.STALE)

        self.cleanup(now)

        after = directory_availability(store.latest_complete_expiry(), now)
        self.assertIs(after, DirectoryAvailability.UNAVAILABLE)
        self.assertIsNone(store.latest_complete_expiry())


# ---------------------------------------------------------------------------
# V-保留-06：CASCADE 边界
# ---------------------------------------------------------------------------
class CascadeBoundaryTest(RetentionPostgresTestCase):
    def test_each_child_table_is_emptied_by_its_own_cascade_edge(self) -> None:
        """V-保留-06（验收 C-01…C-08）：8 张子表**逐表**一条断言，不合并计数。"""
        now = self.database_now()
        self.insert_batch("gib_old", started_at=now - RETENTION_WINDOW - timedelta(hours=1), with_children=True)
        self.insert_sync_run("run_old", started_at=now - RETENTION_WINDOW - timedelta(hours=1))
        for child, _parent, column in CASCADE_EDGES:
            self.assertEqual(self.count(child), 1, f"造数阶段 {child} 应有 1 行")

        self.cleanup(now)

        for child, parent, column in CASCADE_EDGES:
            with self.subTest(child=child, parent=parent):
                self.assertEqual(self.count(child), 0, f"{child} 的行应随 {parent} 一起消失")

    def test_no_child_row_survives_without_its_parent(self) -> None:
        """V-保留-06（验收 C-09）：不少——不存在没有父行的子行。"""
        now = self.database_now()
        self.insert_batch("gib_old", started_at=now - RETENTION_WINDOW - timedelta(hours=1), with_children=True)
        self.insert_batch("gib_new", started_at=now, with_children=True)
        self.insert_sync_run("run_old", started_at=now - RETENTION_WINDOW - timedelta(hours=1))
        self.insert_sync_run("run_new", started_at=now)

        self.cleanup(now)

        for child, parent, column in CASCADE_EDGES:
            with self.subTest(child=child):
                orphans = self.count(
                    child,
                    f"c WHERE NOT EXISTS (SELECT 1 FROM {parent} p WHERE p.id = c.{column})",
                )
                self.assertEqual(orphans, 0)

    def test_a_batch_that_has_not_expired_keeps_every_child_row(self) -> None:
        """V-保留-06（验收 C-10）：不多——未到期批次的子行内容一字未改。"""
        now = self.database_now()
        self.insert_batch("gib_old", started_at=now - RETENTION_WINDOW - timedelta(hours=1), with_children=True)
        self.insert_batch("gib_new", started_at=now, with_children=True)
        self.insert_sync_run("run_new", started_at=now)
        before = {
            child: self.query(f"SELECT to_jsonb(t) FROM {child} t ORDER BY 1")
            for child, parent, _column in CASCADE_EDGES
            if parent == "galaxy_import_batch"
        }

        self.cleanup(now)

        for child, rows in before.items():
            with self.subTest(child=child):
                remaining = self.query(f"SELECT to_jsonb(t) FROM {child} t ORDER BY 1")
                self.assertEqual(len(remaining), 1)
                self.assertIn(remaining[0], rows)

    def test_exactly_eight_foreign_keys_point_at_the_parents_and_all_of_them_cascade(self) -> None:
        """V-保留-06（验收 C-11）：catalog 断言。

        未来加第 9 条边而不更新清理器与用例时，这条会变红——它读的是
        `pg_constraint`，不是测试自己维护的清单。
        """
        rows = self.query(
            "SELECT count(*), bool_and(confdeltype = 'c') "
            "  FROM pg_constraint "
            " WHERE contype = 'f' "
            "   AND confrelid IN ('public.galaxy_import_batch'::regclass, 'public.feishu_org_sync_run'::regclass)"
        )

        self.assertEqual(rows[0][0], len(CASCADE_EDGES))
        self.assertTrue(rows[0][1], "指向两张父表的外键必须全部是 ON DELETE CASCADE")


# ---------------------------------------------------------------------------
# V-保留-07 / 08：不可后移的写入触发器
# ---------------------------------------------------------------------------
class ImmutableExpiryTriggerTest(RetentionPostgresTestCase):
    def test_an_inserted_expiry_is_overwritten_with_the_derived_value(self) -> None:
        """V-保留-07（验收 D-01）：调用方传 900 天也没用。"""
        self.execute(
            "INSERT INTO galaxy_import_batch (id, source_label, source_digest, status, started_at, expires_at) "
            "VALUES ('gib_liar', 'L', 'digest-liar', 'staging', now() - interval '10 hours', now() + interval '900 days')"
        )

        self.assertTrue(
            self.scalar(
                "SELECT expires_at = started_at + interval '2160 hours' FROM galaxy_import_batch WHERE id = 'gib_liar'"
            )
        )
        self.assertTrue(
            self.scalar("SELECT expires_at < now() + interval '91 days' FROM galaxy_import_batch WHERE id = 'gib_liar'")
        )

    def test_an_update_cannot_postpone_the_expiry(self) -> None:
        """V-保留-07（验收 D-02）：与 007 既有做法一致——后移被**覆盖**回来。"""
        self.insert_batch("gib_move", started_at=self.database_now() - timedelta(hours=10), status="staging")
        before = self.scalar("SELECT expires_at FROM galaxy_import_batch WHERE id = 'gib_move'")

        self.execute("UPDATE galaxy_import_batch SET expires_at = now() + interval '900 days' WHERE id = 'gib_move'")

        self.assertEqual(self.scalar("SELECT expires_at FROM galaxy_import_batch WHERE id = 'gib_move'"), before)

    def test_an_update_cannot_change_the_source_time(self) -> None:
        """V-保留-07（验收 D-03）：改来源时间等于换一条推导基线，可以无限后移。"""
        self.insert_batch("gib_anchor", started_at=self.database_now() - timedelta(hours=10), status="staging")

        with self.assertRaises(self._psycopg.errors.RaiseException) as raised:
            self.execute("UPDATE galaxy_import_batch SET started_at = now() WHERE id = 'gib_anchor'")

        self.assertIn("不允许修改银河导入批次的来源时间", str(raised.exception))

    def test_the_org_snapshot_trigger_still_behaves_the_same_way(self) -> None:
        """V-保留-07（验收 D-05）：007 既有的同型断言不因本次改动而改变。"""
        self.insert_sync_run("run_same", started_at=self.database_now() - timedelta(hours=10))

        self.assertTrue(
            self.scalar(
                "SELECT expires_at = started_at + interval '2160 hours' FROM feishu_org_sync_run WHERE id = 'run_same'"
            )
        )
        with self.assertRaises(self._psycopg.errors.RaiseException):
            self.execute("UPDATE feishu_org_sync_run SET started_at = now() WHERE id = 'run_same'")

    def test_history_rows_are_realigned_to_the_derived_expiry(self) -> None:
        """V-保留-08（验收 D-06）：迁移前写下的偏离行被回填校正。

        造数方式就是"迁移前的世界"：先把触发器摘掉，写一行到期时间被拉长到 900 天的
        批次，再重新应用本 revision 的 upgrade DDL——回填是其中的一个 `UPDATE`，这里验证它真的跑了。

        同时取证：现有代码路径写出的行**本来就精确满足**该不变式（`started_at` 与
        `expires_at` 两个 DEFAULT 取的是同一个事务时间戳），所以回填在真实数据上是零行改动。
        """
        self.addCleanup(force_rebuild_schema, self._dsn)
        self.execute("DROP TRIGGER galaxy_import_batch_expiry ON galaxy_import_batch")
        self.execute(
            "INSERT INTO galaxy_import_batch (id, source_label, source_digest, status, started_at, expires_at) "
            "VALUES ('gib_legacy', 'L', 'digest-legacy', 'superseded', now() - interval '10 hours', now() + interval '900 days')"
        )
        # 既有代码路径：两个 DEFAULT 同取事务时间戳，写出来就已经对齐。
        self.execute(
            "INSERT INTO galaxy_import_batch (id, source_label, source_digest, status) "
            "VALUES ('gib_default', 'L', 'digest-default', 'staging')"
        )
        self.assertTrue(
            self.scalar(
                "SELECT expires_at = started_at + interval '2160 hours' FROM galaxy_import_batch WHERE id = 'gib_default'"
            )
        )
        self.assertFalse(
            self.scalar(
                "SELECT expires_at = started_at + interval '2160 hours' FROM galaxy_import_batch WHERE id = 'gib_legacy'"
            )
        )

        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(UPGRADE_SQL)

        self.assertEqual(
            self.count("galaxy_import_batch", "WHERE expires_at <> started_at + interval '2160 hours'"),
            0,
        )


# ---------------------------------------------------------------------------
# V-保留-09 / 10 / 11 / 12：函数的批量、幂等、时间下限与搜索路径
# ---------------------------------------------------------------------------
class CleanupFunctionContractTest(RetentionPostgresTestCase):
    def test_a_batch_limit_caps_how_many_rows_a_single_call_removes(self) -> None:
        """V-保留-09（验收 E-01）：10 行到期、限 3 行，恰删 3。"""
        now = self.database_now()
        for index in range(10):
            self.insert_batch(
                f"gib_{index:02d}",
                started_at=now - RETENTION_WINDOW - timedelta(hours=index + 1),
                status="superseded",
            )

        summary = self.cleanup(now, limit=3)

        self.assertEqual(summary["galaxy_import_batch"][0], 3)
        self.assertEqual(self.count("galaxy_import_batch"), 7)

    def test_an_oversized_limit_is_clamped_and_a_non_positive_limit_is_refused(self) -> None:
        """V-保留-09（验收 E-02）：上限夹取、非正值拒绝。

        夹取而不是拒绝超大值：一次清理不该因为调用方传了个离谱的数字就整体失败，
        到期内容会因此继续留在库里。非正值则是调用方缺陷，直接拒绝。
        """
        now = self.database_now()
        self.execute(
            "INSERT INTO galaxy_import_batch (id, source_label, source_digest, status, started_at) "
            "SELECT 'gib_bulk_' || n, 'L', 'digest-bulk-' || n, 'superseded', %s "
            "  FROM generate_series(1, 1200) AS n",
            (now - RETENTION_WINDOW - timedelta(hours=1),),
        )

        summary = self.cleanup(now, limit=10 ** 9)

        self.assertEqual(summary["galaxy_import_batch"][0], 1000, "超大批量必须被夹取到函数自己的上限")
        self.assertEqual(self.count("galaxy_import_batch"), 200)

        for bad_limit in (0, -5):
            with self.subTest(limit=bad_limit):
                with self.assertRaises(self._psycopg.errors.RaiseException):
                    self.cleanup(now, limit=bad_limit)

    def test_repeated_calls_converge_and_then_report_zero(self) -> None:
        """V-保留-10（验收 E-03）：幂等——循环到 0，总数对得上，再调一次不报错。"""
        now = self.database_now()
        for index in range(7):
            self.insert_batch(
                f"gib_{index:02d}",
                started_at=now - RETENTION_WINDOW - timedelta(hours=index + 1),
                status="superseded",
            )

        total = 0
        for _round in range(20):
            deleted = self.cleanup(now, limit=2)["galaxy_import_batch"][0]
            total += deleted
            if deleted == 0:
                break

        self.assertEqual(total, 7)
        self.assertEqual(self.count("galaxy_import_batch"), 0)
        self.assertEqual(self.cleanup(now, limit=2)["galaxy_import_batch"][0], 0)

    def test_a_future_p_now_is_refused_and_deletes_nothing(self) -> None:
        """V-保留-11（验收 E-04）：否则"把现在说晚一点"就能删掉未到期内容。"""
        now = self.database_now()
        self.insert_batch("gib_fresh", started_at=now - timedelta(hours=1))

        with self.assertRaises(self._psycopg.errors.RaiseException) as raised:
            self.cleanup(now + timedelta(hours=1))

        self.assertIn("晚于当前时间", str(raised.exception))
        self.assertEqual(self.count("galaxy_import_batch"), 1)

    def test_rows_that_expired_after_the_given_moment_are_kept(self) -> None:
        """V-保留-11（验收 E-05）：实际条件是 `expires_at <= p_now`，不是"凡过期都删"。"""
        now = self.database_now()
        self.insert_batch("gib_older", started_at=now - RETENTION_WINDOW - timedelta(hours=2), status="superseded")
        self.insert_batch("gib_newer", started_at=now - RETENTION_WINDOW - timedelta(minutes=30), status="superseded")

        self.cleanup(now - timedelta(hours=1))

        surviving = {row[0] for row in self.query("SELECT id FROM galaxy_import_batch")}
        self.assertEqual(surviving, {"gib_newer"}, "到期时间晚于传入 p_now 的行，即使已经过期也不该在这一次被删")

    def test_the_definition_keeps_the_clock_floor(self) -> None:
        """V-保留-11：`LEAST(p_now, clock_timestamp())` 必须留在函数定义里。

        这是一条**源文本**断言，不是行为断言，因为在严格的未来时间校验之下这个
        `LEAST` 当前是冗余的第二道闸：`p_now` 已经不可能大于 `clock_timestamp()`，
        两种写法的可观察行为完全一致。它的价值在校验被放宽的那一天才兑现，
        而那一天没有任何行为断言会提醒有人把它顺手删了。
        """
        definition = self.scalar(
            "SELECT pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.proname = 'lingxi_retention_cleanup'"
        )

        self.assertIn("LEAST(p_now, clock_timestamp())", str(definition))

    def test_a_locked_row_bounds_the_wait_instead_of_blocking_the_whole_duty(self) -> None:
        """V-保留-22（codex 外审 P1-1）：并发持锁时清理在有限时间返回。

        清理与凭据轮换跑在 scheduler 的同一个线程里串行执行——清理一旦无限等待就是
        整个进程停摆，而 SIGTERM 只能设一个事件，解不开一条正在等 transactionid 的锁。
        这里开一条并发连接锁住一行，要求：**有限时间返回**、被锁的行没被删、
        摘要如实报 0（而不是报了个删除数却什么都没删）。

        为什么不是"跳过被锁的行照删其余"：任何行锁子句都要求对表持有 UPDATE 权限，
        而属主角色按数据库设计 :613 只有 SELECT / DELETE。用超时是同一目标下不动
        权限面的做法，代价是这一轮该表整批让路，下一轮再收（下一条用例固定这点）。
        """
        now = self.database_now()
        self.insert_batch("gib_locked", started_at=now - RETENTION_WINDOW - timedelta(hours=2), status="superseded")
        self.insert_batch("gib_other", started_at=now - RETENTION_WINDOW - timedelta(hours=1), status="superseded")
        self.insert_sync_run("run_old", started_at=now - RETENTION_WINDOW - timedelta(hours=1))

        holder = self._psycopg.connect(self._dsn)
        self.addCleanup(holder.close)
        with holder.cursor() as cursor:
            cursor.execute("SELECT id FROM galaxy_import_batch WHERE id = 'gib_locked' FOR UPDATE")
            started = time.monotonic()
            summary = self.cleanup_with_deadline(now)
            elapsed = time.monotonic() - started
        holder.close()

        self.assertLess(elapsed, 10, "清理不得被行锁无限阻塞——它与凭据轮换共用一个线程")
        self.assertGreater(elapsed, 1, "应当是等到超时才放弃，而不是根本没尝试")
        self.assertEqual(summary["galaxy_import_batch"][0], 0, "摘要必须如实报 0，不能报出没发生的删除")
        self.assertTrue(summary["galaxy_import_batch"][3], "让路必须带标记：与「本来就没有到期行」是两回事")
        self.assertFalse(summary["feishu_org_sync_run"][3], "没被占的表不该带让路标记")
        self.assertEqual(self.count("galaxy_import_batch"), 2, "被锁那一批一行都不该删")
        # 逐表捕获的意义：一张表被占，另一张照常清理。
        self.assertEqual(summary["feishu_org_sync_run"][0], 1)
        self.assertEqual(self.count("feishu_org_sync_run"), 0)

    def test_the_blocked_rows_are_collected_on_a_later_round(self) -> None:
        """V-保留-22：让路不是漏掉——锁一放开，下一轮就全部收走。"""
        now = self.database_now()
        self.insert_batch("gib_locked", started_at=now - RETENTION_WINDOW - timedelta(hours=2), status="superseded")
        self.insert_batch("gib_other", started_at=now - RETENTION_WINDOW - timedelta(hours=1), status="superseded")

        holder = self._psycopg.connect(self._dsn)
        self.addCleanup(holder.close)
        with holder.cursor() as cursor:
            cursor.execute("SELECT id FROM galaxy_import_batch WHERE id = 'gib_locked' FOR UPDATE")
            self.assertEqual(self.cleanup_with_deadline(now)["galaxy_import_batch"][0], 0)
        holder.close()

        self.assertEqual(self.cleanup(now)["galaxy_import_batch"][0], 2)
        self.assertEqual(self.count("galaxy_import_batch"), 0)

    def test_two_concurrent_cleanups_never_delete_the_same_row_twice(self) -> None:
        """V-保留-22 的另一面：并发时摘要之和不得超过实际删除量。

        同一批到期行如果被两个并发清理各删一次、各报一次，摘要就会大于真实删除量，
        而运行日志正是靠摘要判断回收进度的。这里让第一条连接删掉一行且**不提交**
        （因此持有行锁），第二条并发清理必须在超时后如实报 0，而不是把同一行再报一遍。
        """
        now = self.database_now()
        self.insert_batch("gib_a", started_at=now - RETENTION_WINDOW - timedelta(hours=2), status="superseded")

        first = self._psycopg.connect(self._dsn)
        self.addCleanup(first.close)
        with first.cursor() as cursor:
            cursor.execute(
                "SELECT deleted_rows FROM public.lingxi_retention_cleanup(%s, 10) "
                "WHERE target_table = 'galaxy_import_batch'",
                (now,),
            )
            deleted_by_first = int(cursor.fetchone()[0])
            deleted_by_second = self.cleanup_with_deadline(now)["galaxy_import_batch"][0]
        first.commit()
        first.close()

        self.assertEqual(deleted_by_first, 1)
        self.assertEqual(deleted_by_second, 0, "第二次并发清理不得把同一行再报一次")
        self.assertEqual(deleted_by_first + deleted_by_second, 1, "摘要之和必须等于实际删除量")
        self.assertEqual(self.count("galaxy_import_batch"), 0)

    def test_a_shadow_schema_cannot_redirect_the_cleanup(self) -> None:
        """V-保留-12（验收 E-06）：函数固定 `search_path = pg_catalog`。

        调用方在自己的会话里把一张同名影子表放到搜索路径最前面，函数删的仍然是
        `public` 里那张真表，影子表分毫未动。
        """
        now = self.database_now()
        self.addCleanup(self.execute, "DROP SCHEMA IF EXISTS retention_shadow CASCADE")
        self.execute("DROP SCHEMA IF EXISTS retention_shadow CASCADE")
        self.execute("CREATE SCHEMA retention_shadow")
        self.execute(
            "CREATE TABLE retention_shadow.galaxy_import_batch "
            "(id text PRIMARY KEY, expires_at timestamptz NOT NULL)"
        )
        self.execute(
            "INSERT INTO retention_shadow.galaxy_import_batch VALUES ('shadow', now() - interval '3000 hours')"
        )
        self.insert_batch("gib_real", started_at=now - RETENTION_WINDOW - timedelta(hours=1), status="superseded")

        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SET search_path = retention_shadow, public")
            cursor.execute("SELECT deleted_rows FROM public.lingxi_retention_cleanup(%s, 100) WHERE target_table = 'galaxy_import_batch'", (now,))
            deleted = int(cursor.fetchone()[0])

        self.assertEqual(deleted, 1)
        self.assertEqual(self.count("retention_shadow.galaxy_import_batch"), 1, "影子表必须分毫未动")
        self.assertEqual(self.count("galaxy_import_batch"), 0)

        proconfig = self.scalar(
            "SELECT proconfig FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.proname = 'lingxi_retention_cleanup'"
        )
        # 两项都必须在：search_path 挡搜索路径劫持，lock_timeout 挡无限阻塞。
        # 用相等而不是"包含"，多出一项配置也要被看见。
        self.assertEqual(
            list(proconfig or []),
            ["search_path=pg_catalog, pg_temp", "lock_timeout=2s"],
        )

    def test_a_shadow_temp_type_neither_diverts_nor_breaks_the_cleanup(self) -> None:
        """V-保留-12：`search_path` 必须以 `pg_temp` 结尾。

        **这条断言的实际内容是"清理照常完成、目标未被改变"，不是"攻击者跑不了代码"**
        （M2 验收指出原名名不副实，已改名以实际断言为准）。要直接断言后者需要在
        属主身份里布一个可观察的副作用，那本身就得先把攻击做成功；这里取的是可稳定
        观察的那一面：加固前该场景会让函数直接报错（`malformed record literal`），
        加固后一切照常。

        为什么表名全限定挡不住：`search_path` 里没有显式写 `pg_temp` 时，PostgreSQL
        会把它**隐式排在最前**来解析关系名与**类型名**；函数 `DECLARE` 里的
        `timestamptz` / `integer` 是未限定类型名，调用方建一张同名临时表就能改变它们的
        解析结果。能被劫持的不是"删哪张表"，是"在属主身份里跑什么"——所以上一条
        影子 schema 用例覆盖不到它。
        """
        now = self.database_now()
        self.insert_batch("gib_atk", started_at=now - RETENTION_WINDOW - timedelta(hours=1), status="superseded")

        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SET SESSION AUTHORIZATION lingxi_scheduler")
            cursor.execute("CREATE TEMP TABLE timestamptz (x int)")
            cursor.execute("CREATE TEMP TABLE integer (x int)")
            cursor.execute(
                "SELECT deleted_rows FROM public.lingxi_retention_cleanup(%s, 5) "
                "WHERE target_table = 'galaxy_import_batch'",
                (now,),
            )
            deleted = int(cursor.fetchone()[0])

        self.assertEqual(deleted, 1, "影子临时类型不得干扰清理函数的正常执行")
        self.assertEqual(self.count("galaxy_import_batch"), 0)


# ---------------------------------------------------------------------------
# V-保留-13 / 14：角色边界与摘要内容
# ---------------------------------------------------------------------------
class RolePrivilegeTest(RetentionPostgresTestCase):
    """角色断言全部用 `SET SESSION AUTHORIZATION` 而不是 `SET ROLE`。

    区别是决定性的：`SET ROLE` 的权限检查针对**会话用户**。测试连接的会话用户是
    超级用户，所以 `SET ROLE lingxi_app` 之后再 `SET ROLE lingxi_retention_owner`
    照样成功——用 `SET ROLE` 写出来的"不能提权"断言是永远绿的假断言。
    `SET SESSION AUTHORIZATION` 会把会话用户本身换掉，后续检查才是真的。
    """

    def _denied(self, role: str, statement: str, parameters: tuple = ()) -> str:
        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(f"SET SESSION AUTHORIZATION {role}")
            with self.assertRaises(self._psycopg.errors.InsufficientPrivilege) as raised:
                cursor.execute(statement, parameters or None)
            connection.rollback()
        return str(raised.exception)

    def test_the_function_is_security_definer_owned_by_the_retention_role(self) -> None:
        """V-保留-13（验收 E-07）。"""
        rows = self.query(
            "SELECT p.prosecdef, pg_get_userbyid(p.proowner) "
            "  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            " WHERE n.nspname = 'public' AND p.proname = 'lingxi_retention_cleanup'"
        )

        self.assertEqual(len(rows), 1, "不应存在同名重载")
        self.assertTrue(rows[0][0], "必须是 SECURITY DEFINER")
        self.assertEqual(rows[0][1], "lingxi_retention_owner")

    def test_public_has_no_execute_and_only_the_scheduler_role_may_call_it(self) -> None:
        """V-保留-13（验收 E-08）。"""
        # aclexplode 把 ACL 拆成行，grantee = 0 就是 PUBLIC——比在 ACL 文本里找
        # 子串可靠：`lingxi_retention_owner=X/...` 本身就含有 "=X/"。
        grantees = self.query(
            "SELECT coalesce(pg_get_userbyid(nullif(a.grantee, 0)), 'PUBLIC'), a.privilege_type "
            "  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, aclexplode(p.proacl) a "
            " WHERE n.nspname = 'public' AND p.proname = 'lingxi_retention_cleanup' "
            " ORDER BY 1"
        )
        self.assertNotIn(("PUBLIC", "EXECUTE"), grantees, "PUBLIC 不得持有 EXECUTE")
        self.assertIn(("lingxi_scheduler", "EXECUTE"), grantees)
        self.assertEqual(
            sorted({name for name, _privilege in grantees}),
            ["lingxi_retention_owner", "lingxi_scheduler"],
            "除属主外，只有 scheduler 能拿到这个函数的任何权限",
        )

        self._denied("lingxi_app", "SELECT * FROM public.lingxi_retention_cleanup(now(), 10)")

        now = self.database_now()
        self.insert_batch("gib_old", started_at=now - RETENTION_WINDOW - timedelta(hours=1), status="superseded")
        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SET SESSION AUTHORIZATION lingxi_scheduler")
            cursor.execute("SELECT target_table, deleted_rows FROM public.lingxi_retention_cleanup(now(), 10)")
            result = dict(cursor.fetchall())
        self.assertEqual(result["galaxy_import_batch"], 1)

    def test_the_application_role_cannot_delete_the_parent_tables_directly(self) -> None:
        """V-保留-13（验收 E-09）：有 SELECT、没有 DELETE——回收只有函数一条路。"""
        now = self.database_now()
        self.insert_batch("gib_old", started_at=now - RETENTION_WINDOW - timedelta(hours=1), status="superseded")

        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SET SESSION AUTHORIZATION lingxi_app")
            cursor.execute("SELECT count(*) FROM public.galaxy_import_batch")
            self.assertEqual(int(cursor.fetchone()[0]), 1, "lingxi_app 应当读得到业务表")

        for table in ("galaxy_import_batch", "feishu_org_sync_run"):
            with self.subTest(table=table):
                message = self._denied("lingxi_app", f"DELETE FROM public.{table}")
                self.assertIn("permission denied", message)
        self.assertEqual(self.count("galaxy_import_batch"), 1)

    def test_the_delete_guard_covers_all_four_quadrants(self) -> None:
        """V-保留-21（编排者裁定的双条件）：到期 **且** 属主，才删得掉。

        四象限逐格验。单验其中一维都会漏：只按到期时间判，误授 lingxi_app DELETE
        之后它能删掉**过期**行（codex 实测）；只按角色判，属主自己能删未到期行。
        """
        now = self.database_now()
        self.addCleanup(force_rebuild_schema, self._dsn)
        self.insert_batch("gib_fresh", started_at=now, status="complete")
        self.insert_batch("gib_old", started_at=now - RETENTION_WINDOW - timedelta(hours=1), status="superseded")
        # 误授权：这一格正是"触发器防误授权"要挡的场景。
        self.execute("GRANT DELETE ON public.galaxy_import_batch TO lingxi_app")

        quadrants = (
            ("lingxi_retention_owner", "gib_old", True),
            ("lingxi_retention_owner", "gib_fresh", False),
            ("lingxi_app", "gib_old", False),
            ("lingxi_app", "gib_fresh", False),
        )
        for role, batch_id, should_succeed in quadrants:
            with self.subTest(role=role, row=batch_id, expected="成功" if should_succeed else "拒绝"):
                with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
                    cursor.execute(f"SET SESSION AUTHORIZATION {role}")
                    if should_succeed:
                        cursor.execute("DELETE FROM public.galaxy_import_batch WHERE id = %s", (batch_id,))
                        self.assertEqual(cursor.rowcount, 1)
                        connection.rollback()
                    else:
                        with self.assertRaises(self._psycopg.errors.RaiseException):
                            cursor.execute("DELETE FROM public.galaxy_import_batch WHERE id = %s", (batch_id,))
                        connection.rollback()

        self.assertEqual(self.count("galaxy_import_batch"), 2, "四象限验完后两行都应还在")

    def test_an_expired_row_is_still_collected_by_the_restricted_function(self) -> None:
        """V-保留-21 的肯定面：双条件不得把受限清理函数自己挡在门外。

        只写否定面的话，一个"什么都拒绝"的触发器同样能让四象限里的三格通过，
        而它会把唯一合法的回收路径也堵死。
        """
        now = self.database_now()
        self.insert_batch("gib_old", started_at=now - RETENTION_WINDOW - timedelta(hours=1), with_children=True)

        summary = self.cleanup(now)

        self.assertEqual(summary["galaxy_import_batch"][0], 1)
        self.assertEqual(self.count("galaxy_import_batch"), 0)
        self.assertEqual(self.count("galaxy_user"), 0, "CASCADE 不得被删除防线挡住")

    def test_no_lingxi_role_holds_truncate_on_the_content_tables(self) -> None:
        """V-保留-21：TRUNCATE 不经过行级触发器，因此必须由权限挡住。

        删除防线是行级的，TRUNCATE 绕过它。这条断言把「没有任何 lingxi_* 角色
        拿得到 TRUNCATE」钉住——否则那道防线可以被一条 TRUNCATE 整体绕开。
        """
        for table in ("galaxy_import_batch", "feishu_org_sync_run"):
            for role in ("lingxi_app", "lingxi_scheduler", "lingxi_retention_owner", "lingxi_migrate"):
                with self.subTest(table=table, role=role):
                    self.assertFalse(
                        self.scalar(
                            "SELECT has_table_privilege(%s, %s, 'TRUNCATE')", (role, f"public.{table}")
                        )
                    )

    def test_the_scheduler_role_holds_no_table_privilege_at_all(self) -> None:
        """V-保留-13（codex 外审 P2-1）：scheduler 只需要函数的 EXECUTE。

        清理函数是 SECURITY DEFINER，读写表的是属主身份；运行摘要来自函数返回值。
        scheduler 因此**一条表权限都不需要**，先前授出的 SELECT 没有任何调用方。
        """
        for table in ("galaxy_import_batch", "feishu_org_sync_run"):
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                with self.subTest(table=table, privilege=privilege):
                    self.assertFalse(
                        self.scalar(
                            "SELECT has_table_privilege('lingxi_scheduler', %s, %s)",
                            (f"public.{table}", privilege),
                        )
                    )
        self.assertTrue(
            self.scalar(
                "SELECT has_function_privilege('lingxi_scheduler', "
                "'public.lingxi_retention_cleanup(timestamptz,integer)', 'EXECUTE')"
            )
        )

    def test_a_pre_existing_login_owner_role_is_normalised(self) -> None:
        """V-保留-23（codex 外审 P1-3）：预建的同名属主角色必须被校验。

        `IF NOT EXISTS` 一跳过，别人预建的同名角色就静默继承本迁移赋予的全部含义——
        其中 lingxi_retention_owner 会直接成为 SECURITY DEFINER 函数的属主。
        一个**可登录**的属主等于把「只能经受限函数删除」变成「可以直接连上来删」。
        """
        self.addCleanup(force_rebuild_schema, self._dsn)
        self.execute("ALTER ROLE lingxi_retention_owner LOGIN")
        self.assertTrue(self.scalar("SELECT rolcanlogin FROM pg_roles WHERE rolname = 'lingxi_retention_owner'"))

        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(UPGRADE_SQL)

        self.assertFalse(
            self.scalar("SELECT rolcanlogin FROM pg_roles WHERE rolname = 'lingxi_retention_owner'"),
            "重新应用迁移必须把属主角色收紧回无登录",
        )

    def test_an_owner_role_that_inherits_other_privileges_is_refused(self) -> None:
        """V-保留-23：属主角色不得是任何角色的成员。

        它一旦继承别的权限，SECURITY DEFINER 的执行身份就不再是「只有两张父表
        SELECT/DELETE」的最小面，而迁移会照常成功、没有任何提示。
        """
        # 收尾顺序要紧：addCleanup 是后进先出，必须**先**收回成员关系再重建结构。
        # 反过来的话，重建时迁移会再一次撞上同一条 RAISE，建库失败，
        # 之后每一条用例都在一个没有清理函数的库上跑——本轮实测就是这样一次红了 30 条。
        self.addCleanup(force_rebuild_schema, self._dsn)
        self.addCleanup(self.execute, "REVOKE lingxi_app FROM lingxi_retention_owner")
        self.execute("GRANT lingxi_app TO lingxi_retention_owner")

        with self.assertRaises(self._psycopg.errors.RaiseException) as raised:
            with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
                cursor.execute(UPGRADE_SQL)

        self.assertIn("可经成员关系取得", str(raised.exception))

    def test_the_handover_restores_the_membership_graph_exactly(self) -> None:
        """V-保留-23（验收二次抓到）：移交前后成员关系三元组与 schema USAGE 完全一致。

        这条断言是**根因**：属主移交要临时给执行者补一个 SET 选项，用完必须精确还原。
        前两版还原写法各留下一点残余（`WITH SET FALSE` 把 inherit=f 变成 t；
        `REVOKE SET OPTION FOR` 撤的是所有授予方那条、留下了本次多出来的整条授予），
        而**没有任何用例在对照成员关系状态**，所以两轮自查都没发现，第三方连着抓了两次。
        现在把状态对照本身钉住。
        """

        def snapshot() -> tuple:
            members = tuple(
                self.query(
                    "SELECT r.rolname, m.rolname, g.rolname, am.admin_option, am.inherit_option, am.set_option "
                    "  FROM pg_auth_members am "
                    "  JOIN pg_roles r ON r.oid = am.roleid "
                    "  JOIN pg_roles m ON m.oid = am.member "
                    "  JOIN pg_roles g ON g.oid = am.grantor "
                    " WHERE r.rolname LIKE 'lingxi\\_%' OR m.rolname LIKE 'lingxi\\_%' "
                    " ORDER BY 1, 2, 3"
                )
            )
            usage = tuple(
                self.query(
                    "SELECT pg_get_userbyid(a.grantee), a.privilege_type "
                    "  FROM pg_namespace n, aclexplode(n.nspacl) a "
                    " WHERE n.nspname = 'public' AND a.grantee <> 0 ORDER BY 1, 2"
                )
            )
            return members, usage

        before = snapshot()

        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(UPGRADE_SQL)

        self.assertEqual(snapshot(), before, "重新应用迁移不得改变成员关系图或 schema 授权面")

    def test_an_indirect_membership_chain_to_the_owner_is_refused(self) -> None:
        """V-保留-23（codex 二轮 P1-2）：间接链也要拒。

        `owner → bridge → app` 这样的链上，属主的**直接**成员行只有 bridge，
        看起来干净，而它实际已经拿到 app 的全部权限；针对 app 的直接 REVOKE 也撤不掉它。
        判定必须用按传递闭包工作的 `pg_has_role`，不能只查一层 `pg_auth_members`。
        """
        self.addCleanup(force_rebuild_schema, self._dsn)
        self.addCleanup(self.execute, "DROP ROLE IF EXISTS lingxi_bridge_probe")
        self.addCleanup(self.execute, "REVOKE lingxi_bridge_probe FROM lingxi_retention_owner")
        self.execute("DROP ROLE IF EXISTS lingxi_bridge_probe")
        self.execute("CREATE ROLE lingxi_bridge_probe NOLOGIN")
        self.execute("GRANT lingxi_app TO lingxi_bridge_probe")
        self.execute("GRANT lingxi_bridge_probe TO lingxi_retention_owner")
        # 直接成员行里看不出问题：属主的直接上游只有 bridge。
        self.assertTrue(self.scalar("SELECT pg_has_role('lingxi_retention_owner', 'lingxi_app', 'USAGE')"))

        with self.assertRaises(self._psycopg.errors.RaiseException) as raised:
            with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
                cursor.execute(UPGRADE_SQL)

        self.assertIn("可能是间接链", str(raised.exception))

    def test_no_business_role_can_become_the_retention_owner(self) -> None:
        """V-保留-13（验收 E-10）：拿不到那个角色，就拿不到它的 DELETE。"""
        for role in ("lingxi_app", "lingxi_scheduler"):
            with self.subTest(role=role):
                message = self._denied(role, "SET ROLE lingxi_retention_owner")
                self.assertIn("permission denied", message)

    def test_the_scheduler_role_cannot_delete_directly_either(self) -> None:
        """V-保留-13：scheduler 能调函数，但没有目标表的 DELETE。"""
        for table in ("galaxy_import_batch", "feishu_org_sync_run"):
            with self.subTest(table=table):
                self._denied("lingxi_scheduler", f"DELETE FROM public.{table}")

    def test_the_four_roles_exist_and_none_of_them_can_log_in(self) -> None:
        """V-保留-13：角色随迁移幂等建立；本切片不接线运行时连接身份。"""
        # 这是一条**集群级**精确集合断言：它假设跑在一个新鲜的、只被本仓库使用的
        # 容器上。共享集群里别人建了 `lingxi_` 前缀的角色就会误红——那种环境下应当
        # 改成子集断言。CI 与本机都用一次性容器，故这里取更严的相等（内审 P3-2）。
        rows = self.query(
            "SELECT rolname, rolcanlogin, rolsuper FROM pg_roles WHERE rolname LIKE 'lingxi\\_%' ORDER BY rolname"
        )

        self.assertEqual(
            [row[0] for row in rows],
            ["lingxi_app", "lingxi_migrate", "lingxi_retention_owner", "lingxi_scheduler"],
        )
        for name, can_login, is_super in rows:
            with self.subTest(role=name):
                self.assertFalse(can_login, f"{name} 本切片不应具备登录能力")
                self.assertFalse(is_super, f"{name} 不得是超级用户")

    def test_reapplying_the_migration_takes_back_an_over_granted_owner_membership(self) -> None:
        """V-保留-13：越权授出的属主成员资格，重新应用迁移会被收回。

        角色成员关系是**集群级**对象，不随表一起重建：表的 ACL 在重建表时自然清空，
        成员关系不会。这条断言存在的理由来自一次真实事故——#54 的变异测试把成员资格
        授给 `lingxi_app` 之后，重跑整条迁移链并没有收回它，于是"应用角色不能删除
        内容表""不能取得属主角色"这一组限权断言在之后的整轮测试里全部静默失效：
        授权面被放宽了，而没有任何东西报错。
        """
        self.execute("GRANT lingxi_retention_owner TO lingxi_app, lingxi_scheduler")
        self.assertEqual(self._owner_members() & {"lingxi_app", "lingxi_scheduler"}, {"lingxi_app", "lingxi_scheduler"})

        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(UPGRADE_SQL)

        self.assertEqual(self._owner_members(), {"lingxi_migrate"}, "只有迁移角色应当是属主角色的成员")
        self._denied("lingxi_app", "SET ROLE lingxi_retention_owner")

    def _owner_members(self) -> set[str]:
        return {
            row[0]
            for row in self.query(
                "SELECT r.rolname FROM pg_auth_members am "
                "  JOIN pg_roles r ON r.oid = am.member "
                "  JOIN pg_roles m ON m.oid = am.roleid "
                " WHERE m.rolname = 'lingxi_retention_owner'"
            )
        }

    def test_applying_the_migration_twice_does_not_duplicate_or_fail(self) -> None:
        """V-保留-13：角色创建幂等——重复应用不报错、不产生第二个角色。"""
        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(UPGRADE_SQL)
            cursor.execute(UPGRADE_SQL)

        self.assertEqual(
            self.count("pg_roles", "WHERE rolname LIKE 'lingxi\\_%'"),
            4,
        )


class BlockedRoundLoggingTest(RetentionPostgresTestCase):
    def test_a_blocked_round_is_logged_as_a_warning_not_a_normal_completion(self) -> None:
        """V-保留-22（codex 二轮 P1-3）：拿不到锁的那一轮不能记成正常完成。

        让路与"本来就没有到期行"的 deleted 都是 0。只有标记与日志级别能把它们分开——
        不分开的话，一张长期被占的表会在 INFO 流水里表现为一切正常，而内容一直没被
        回收。保留违规最不该有的形态就是它悄无声息。
        """
        from lingxi.adapters.retention import PostgresRetentionCleaner
        from lingxi.apps.scheduler import RetentionCleanupDuty

        now = self.database_now()
        self.insert_batch("gib_locked", started_at=now - RETENTION_WINDOW - timedelta(hours=1), status="superseded")

        holder = self._psycopg.connect(self._dsn)
        self.addCleanup(holder.close)
        with holder.cursor() as cursor:
            cursor.execute("SELECT id FROM galaxy_import_batch WHERE id = 'gib_locked' FOR UPDATE")
            duty = RetentionCleanupDuty(cleaner=PostgresRetentionCleaner(self._dsn))
            with self.assertLogs("lingxi.apps.scheduler", level="WARNING") as captured:
                report = duty.run_once()
        holder.close()

        self.assertEqual(report.deleted, 0)
        self.assertEqual(report.blocked_tables, ("galaxy_import_batch",))
        self.assertTrue(any("锁等待超时" in line for line in captured.output))
        self.assertTrue(any(line.startswith("WARNING") for line in captured.output))

    def test_a_normal_empty_round_stays_at_info(self) -> None:
        """对照面：没有到期行的一轮是正常完成，不该被记成 warning。"""
        from lingxi.adapters.retention import PostgresRetentionCleaner
        from lingxi.apps.scheduler import RetentionCleanupDuty

        duty = RetentionCleanupDuty(cleaner=PostgresRetentionCleaner(self._dsn))
        with self.assertLogs("lingxi.apps.scheduler", level="INFO") as captured:
            report = duty.run_once()

        self.assertEqual(report.deleted, 0)
        self.assertEqual(report.blocked_tables, ())
        self.assertTrue(all(not line.startswith("WARNING") for line in captured.output))


class SummaryContentTest(RetentionPostgresTestCase):
    def test_the_summary_carries_table_names_counts_and_a_time_range_only(self) -> None:
        """V-保留-14（验收 E-11）：摘要里不得出现任何人员数据字段值。"""
        from lingxi.adapters.retention import PostgresRetentionCleaner

        now = self.database_now()
        self.insert_batch("gib_old", started_at=now - RETENTION_WINDOW - timedelta(hours=1), with_children=True)
        self.insert_sync_run("run_old", started_at=now - RETENTION_WINDOW - timedelta(hours=1))
        # 先确认这些人员数据值真的在库里——否则"摘要不含它们"是一句空话。
        personal_values = ("化名甲", "ou_hua_ming", "user_hua_ming", "union_hua_ming", "gib_old@example.invalid")
        self.assertEqual(self.count("galaxy_user", "WHERE email = 'gib_old@example.invalid'"), 1)
        self.assertEqual(
            self.count(
                "feishu_org_member_snapshot",
                "WHERE display_name = '化名甲' AND open_id = 'ou_hua_ming' "
                "AND user_id = 'user_hua_ming' AND union_id = 'union_hua_ming'",
            ),
            1,
        )

        cleaner = PostgresRetentionCleaner(self._dsn)
        # 捕获**根 logger**，不是只捕获 scheduler 那一个。清理这条链路上会写日志的
        # 至少有 `lingxi.apps.scheduler` 与 `lingxi.adapters.retention` 两处；只盯住
        # 其中一个，另一处的泄漏就看不见。这不是假设：变异 M-20 把人员数据写进适配器
        # 的日志行时，只捕获 scheduler 的那一版**仍然是绿的**。
        with self.assertLogs(level="INFO") as captured:
            from lingxi.apps.scheduler import RetentionCleanupDuty

            report = RetentionCleanupDuty(cleaner=cleaner).run_once()

        rendered = repr(report) + "\n" + report.summary() + "\n" + "\n".join(captured.output)
        self.assertEqual(report.deleted, 2)
        self.assertIn("galaxy_import_batch", rendered)
        self.assertIn("feishu_org_sync_run", rendered)
        for value in personal_values:
            with self.subTest(value=value):
                self.assertNotIn(value, rendered)
        for result in report.tables:
            with self.subTest(table=result.table):
                self.assertIsNotNone(result.oldest_expires_at)
                self.assertIsNotNone(result.newest_expires_at)


# ---------------------------------------------------------------------------
# V-保留-15 / 16 / 17：scheduler 职责
# ---------------------------------------------------------------------------
class SchedulerDutyTest(RetentionPostgresTestCase):
    def test_the_scheduler_loop_actually_removes_expired_rows(self) -> None:
        """V-保留-15（验收 F-01）：观察点是到期行真的消失，不是"调用了某个方法"。"""
        from lingxi.adapters.retention import PostgresRetentionCleaner
        from lingxi.apps.scheduler import RetentionCleanupDuty, SchedulerLoop

        now = self.database_now()
        self.insert_batch("gib_old", started_at=now - RETENTION_WINDOW - timedelta(hours=1), with_children=True)
        self.insert_batch("gib_new", started_at=now, with_children=True)
        loop = SchedulerLoop(duties=(RetentionCleanupDuty(cleaner=PostgresRetentionCleaner(self._dsn)),), interval_seconds=0.01)

        loop.run_once()

        self.assertEqual({row[0] for row in self.query("SELECT id FROM galaxy_import_batch")}, {"gib_new"})

    def test_a_failing_cleanup_never_postpones_an_expiry(self) -> None:
        """V-保留-16（验收 F-04）：清理失败只能重试，不能把到期时间往后挪。"""
        from lingxi.apps.scheduler import RetentionCleanupDuty, SchedulerLoop

        now = self.database_now()
        self.insert_batch("gib_old", started_at=now - RETENTION_WINDOW - timedelta(hours=1), status="superseded")
        before = self.query("SELECT id, started_at, expires_at FROM galaxy_import_batch ORDER BY id")

        class ExplodingCleaner:
            def run_once(self):
                raise RuntimeError("模拟清理失败")

        loop = SchedulerLoop(duties=(RetentionCleanupDuty(cleaner=ExplodingCleaner()),), interval_seconds=0.01)
        with self.assertLogs("lingxi.apps.scheduler", level="ERROR"):
            for _round in range(3):
                loop.run_once()

        self.assertEqual(self.query("SELECT id, started_at, expires_at FROM galaxy_import_batch ORDER BY id"), before)

    def test_sigterm_leaves_no_half_deleted_state_and_claims_nothing_after_stopping(self) -> None:
        """V-保留-17（验收 F-05/F-06）：真实子进程 + 真实 SIGTERM + 真库。

        无半删状态的机制是"一次调用就是一个事务"，不是"信号来得巧"。这里验证它的
        可观察后果：无论信号落在哪一轮，库里都不存在没有父行的子行，进程报告的
        删除总数与库里实际少掉的行数完全一致。
        """
        total_batches = 12
        now = self.database_now()
        for index in range(total_batches):
            self.insert_batch(
                f"gib_{index:02d}",
                started_at=now - RETENTION_WINDOW - timedelta(hours=index + 1),
                status="superseded",
                with_children=True,
            )

        # 信号必须**确定地**落在一轮的中间，而不是靠时间凑巧。做法是在清理职责
        # 前面放一个闸门职责：它在第 4 轮打印 armed 然后长睡，父进程读到 armed 才发
        # SIGTERM。于是前 3 轮真的删了行（F-05 的观察点），而第 4 轮信号一定落在
        # 闸门里——此时循环是否还让后面的清理职责领取新工作，就是 F-06 本身。
        #
        # 不这样安排的话，单职责版本要靠 SIGTERM 恰好落在 run_once 内部才检验得到
        # 停止检查点；实测把检查点删掉，那一版在 8 个 PYTHONHASHSEED 下**一次都没变红**。
        script = textwrap.dedent(
            """
            import json, os, threading, time
            from lingxi.adapters.retention import PostgresRetentionCleaner
            from lingxi.apps.scheduler import RetentionCleanupDuty, SchedulerLoop, install_signal_handlers

            state = {"calls": 0, "calls_after_stop": 0, "deleted": 0, "gate_rounds": 0}
            ARM_ON_ROUND = 4

            class Gate:
                name = "闸门职责"
                def run_once(self):
                    state["gate_rounds"] += 1
                    if state["gate_rounds"] == ARM_ON_ROUND:
                        print("armed", flush=True)
                        time.sleep(1.5)
                    return None

            class Counting:
                def __init__(self, inner):
                    self._inner = inner
                def run_once(self):
                    state["calls"] += 1
                    if stop.is_set():
                        state["calls_after_stop"] += 1
                    report = self._inner.run_once()
                    state["deleted"] += report.deleted
                    return report

            stop = threading.Event()
            # 每轮只删一行：信号到达时库里一定还剩着没删完的行。
            cleaner = Counting(PostgresRetentionCleaner(os.environ["LINGXI_POSTGRES_DSN"], batch_limit=1))
            duty = RetentionCleanupDuty(cleaner=cleaner, stop=stop)
            loop = SchedulerLoop(duties=(Gate(), duty), interval_seconds=0.01, stop=stop)
            install_signal_handlers(loop)
            print("ready", flush=True)
            loop.run_forever()
            print(json.dumps(state), flush=True)
            """
        )
        environment = {
            **os.environ,
            "PYTHONPATH": str(REPOSITORY_ROOT / "src"),
            "PYTHONUNBUFFERED": "1",
        }
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stdout is not None
            self.assertEqual(process.stdout.readline().strip(), "ready")
            # 等子进程进入闸门的长睡再发信号：这样 SIGTERM 必然落在一轮中间。
            self.assertEqual(process.stdout.readline().strip(), "armed")
            time.sleep(0.1)
            sent_at = time.monotonic()
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=30)
        finally:
            if process.poll() is None:  # pragma: no cover - 只在断言失败路径上发生
                process.kill()
                process.communicate()

        self.assertEqual(process.returncode, 0, msg=stderr)
        self.assertLess(time.monotonic() - sent_at, 10, "SIGTERM 后必须在超时内退出")
        state = json.loads(stdout.strip().splitlines()[-1])

        self.assertEqual(state["calls_after_stop"], 0, "收到 SIGTERM 后不得再领取新的清理批次")
        self.assertGreater(state["calls"], 0, "信号到达前必须真的删过行，否则后面的一致性断言是空的")
        self.assertGreater(remaining_before_assert := self.count("galaxy_import_batch"), 0, "信号必须落在删完之前")
        remaining = remaining_before_assert
        self.assertEqual(state["deleted"], total_batches - remaining, "报告的删除数必须与库里实际少掉的行一致")
        for child, parent, column in CASCADE_EDGES:
            if parent != "galaxy_import_batch":
                continue
            with self.subTest(child=child):
                self.assertEqual(
                    self.count(child, f"c WHERE NOT EXISTS (SELECT 1 FROM {parent} p WHERE p.id = c.{column})"),
                    0,
                    "不得存在没有父行的子行——那才是半删状态",
                )
                self.assertEqual(self.count(child), remaining)


# ---------------------------------------------------------------------------
# V-保留-18：迁移前滚与回滚
# ---------------------------------------------------------------------------
class MigrationRoundTripTest(RetentionPostgresTestCase):
    """整链前滚 → 回滚 → 再前滚，往返两次。

    走的是 revision `0054_retention_cleanup` 内联的 `_UPGRADE_SQL` / `_DOWNGRADE_SQL`
    两段文本，也就是它的 `upgrade()` / `downgrade()` 实际执行的同一段字节。

    分工：**alembic CLI 层的往返由门禁覆盖**——`scripts/ci/check_migration_chain.sh`
    的"逐条 revision 真往返"会真的 `downgrade -1` 再 `upgrade`，并要求结构与往返前
    逐字节一致。本用例覆盖的是 **DDL 文本层**：不经 alembic、直接把两段 SQL 来回跑，
    因此在没有 alembic 的环境里也能发现"downgrade 漏删了某个对象"这类缺陷。
    两层都要，因为它们失效的方式不同。
    """

    def _objects(self) -> dict[str, object]:
        return {
            "function": self.scalar(
                "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'public' AND p.proname = 'lingxi_retention_cleanup'"
            ),
            "trigger_function": self.scalar(
                "SELECT count(*) FROM pg_proc WHERE proname = 'galaxy_import_batch_fix_expiry'"
            ),
            "trigger": self.scalar("SELECT count(*) FROM pg_trigger WHERE tgname = 'galaxy_import_batch_expiry'"),
        }

    def test_upgrade_and_downgrade_round_trip_twice(self) -> None:
        """V-保留-18（验收 G-01/G-02/G-04）。"""
        self.addCleanup(force_rebuild_schema, self._dsn)
        self.assertEqual(self._objects(), {"function": 1, "trigger_function": 1, "trigger": 1})

        for attempt in range(2):
            with self.subTest(attempt=attempt, phase="downgrade"):
                with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
                    cursor.execute(DOWNGRADE_SQL)
                self.assertEqual(self._objects(), {"function": 0, "trigger_function": 0, "trigger": 0})
            with self.subTest(attempt=attempt, phase="upgrade"):
                with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
                    cursor.execute(UPGRADE_SQL)
                self.assertEqual(self._objects(), {"function": 1, "trigger_function": 1, "trigger": 1})

    def test_downgrade_keeps_the_data_and_the_cluster_level_roles(self) -> None:
        """V-保留-18（验收 G-02/G-03）：回滚删对象、不删数据、不删集群级角色。"""
        # 收尾**必须在执行 DOWNGRADE 之前注册**：注册在后面的话，中间任何一条断言
        # 失败都会让这个共享库停在降级态，后面每一条用例跟着连坐（内审 P3-1）。
        self.addCleanup(self._reapply_chain)
        now = self.database_now()
        self.insert_batch("gib_keep", started_at=now - timedelta(hours=1), with_children=True)
        before = self.query("SELECT to_jsonb(t) FROM galaxy_import_batch t ORDER BY 1")

        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(DOWNGRADE_SQL)

        self.assertEqual(self.query("SELECT to_jsonb(t) FROM galaxy_import_batch t ORDER BY 1"), before)
        self.assertEqual(self.count("pg_roles", "WHERE rolname LIKE 'lingxi\\_%'"), 4)

    def test_downgrade_leaves_no_privilege_granted_by_this_revision(self) -> None:
        """V-保留-18（内审 P3-4）：降级后不残留本 revision 授出的任何权限。

        这条断言没有别的东西替代：链门禁的 `pg_dump` 带 `--no-privileges`，对 ACL
        **完全盲**，所以 downgrade 里那一整段 REVOKE 此前无人守——删掉它们，
        两条血统的结构比对依然零差异，而权限面已经留在库里了。
        """
        self.addCleanup(self._reapply_chain)

        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(DOWNGRADE_SQL)

        roles = ("lingxi_app", "lingxi_scheduler", "lingxi_retention_owner", "lingxi_migrate")
        for table in ("galaxy_import_batch", "feishu_org_sync_run"):
            for role in roles:
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                    with self.subTest(table=table, role=role, privilege=privilege):
                        self.assertFalse(
                            self.scalar(
                                "SELECT has_table_privilege(%s, %s, %s)",
                                (role, f"public.{table}", privilege),
                            )
                        )
        # schema 这一项必须查 **ACL 条目**，不能用 has_schema_privilege：
        # public schema 默认把 USAGE 授给了 PUBLIC，而 has_schema_privilege 会把
        # 经 PUBLIC 继承来的权限也算进去，于是不论是否 REVOKE 过它都恒为真——
        # 那样写出来的断言永远不会红。这里看的是"有没有为这些角色单独授过"。
        explicit = {
            row[0]
            for row in self.query(
                "SELECT pg_get_userbyid(a.grantee) FROM pg_namespace n, aclexplode(n.nspacl) a "
                " WHERE n.nspname = 'public' AND a.grantee <> 0"
            )
        }
        for role in roles:
            with self.subTest(role=role, object="schema public"):
                self.assertNotIn(role, explicit)
        # 属主角色的成员关系也应回到只剩「无」：迁移授给 lingxi_migrate 的那条被收回。
        self.assertEqual(self._owner_members_after_downgrade(), set())

    def _owner_members_after_downgrade(self) -> set[str]:
        return {
            row[0]
            for row in self.query(
                "SELECT m.rolname FROM pg_auth_members am "
                "  JOIN pg_roles r ON r.oid = am.roleid "
                "  JOIN pg_roles m ON m.oid = am.member "
                " WHERE r.rolname = 'lingxi_retention_owner' AND m.rolname LIKE 'lingxi\\_%'"
            )
        }

    def test_the_previous_image_still_works_against_the_upgraded_database(self) -> None:
        """V-保留-18（验收 G-05）：触发器不得让既有写路径失败。

        "回滚就是切镜像 tag 重启"要求旧镜像能在新库上工作。这里跑的是**未随本次
        改动的**既有写路径——银河导入器的完整落库流程——它在带触发器的库上必须照常成功。
        """
        from lingxi.adapters.galaxy_import import PostgresGalaxyImportStore

        store = PostgresGalaxyImportStore(self._dsn)
        result = store.import_export(
            source_label="合成导出（测试）",
            source_digest="digest-old-image",
            tables={
                "user": [{"user_id": "U1", "dept_id": "D1", "user_name": "10001", "nick_name": "化名甲", "email": "a@example.invalid", "create_time": "2019-01-02 03:04:05"}],
                "user_role": [{"user_id": "U1", "role_id": "R1", "user_name": "化名甲", "role_name": "A运营"}],
                "role_menu": [{"role_id": "R1", "menu_id": "M1", "role_name": "A运营", "menu_name": "报表"}],
                "sys_user_datacountry": [{"USER_ID": "U1", "DATACOUNTRY_ID": "101", "USER_NAME": "化名甲", "DATACOUNTRY_NAME": "甲国"}],
                "sys_country": [{"id": "7", "country_key": "101", "name": "ALPHA", "code": "AL", "name_cn": "甲国", "region_key": "1", "region_name": "甲区", "boss_company_id": "BC"}],
            },
        )

        self.assertEqual(result.outcome, "imported")
        self.assertEqual(store.current_batch_id(), result.batch_id)

    def _reapply_chain(self) -> None:
        force_rebuild_schema(self._dsn)


# ---------------------------------------------------------------------------
# V-保留-19：清理之后重导同一份导出
# ---------------------------------------------------------------------------
class ReimportAfterCleanupTest(RetentionPostgresTestCase):
    def test_the_same_export_can_be_imported_again_after_its_batch_was_collected(self) -> None:
        """V-保留-19（编排者裁定 ⑥）：**固化现状**，不是新增保护。

        清理删掉批次行的同时也删掉了那条 `source_digest` 记录，因此同一份导出可以
        重新导入并成为新的当前有效批次。这不是缺陷判定——重导要拿到的正是一份
        新鲜的、重新起算九十天的副本。"是否需要摘要墓碑防旧导出复活"是另一个
        产品决策（#69），本条只把当前可观察行为钉住，以便那个决策落地时这里会变红。
        """
        from lingxi.adapters.galaxy_import import PostgresGalaxyImportStore

        store = PostgresGalaxyImportStore(self._dsn)
        tables = {
            "user": [{"user_id": "U1", "dept_id": "D1", "user_name": "10001", "nick_name": "化名甲", "email": "a@example.invalid", "create_time": "2019-01-02 03:04:05"}],
            "user_role": [{"user_id": "U1", "role_id": "R1", "user_name": "化名甲", "role_name": "A运营"}],
            "role_menu": [{"role_id": "R1", "menu_id": "M1", "role_name": "A运营", "menu_name": "报表"}],
            "sys_user_datacountry": [{"USER_ID": "U1", "DATACOUNTRY_ID": "101", "USER_NAME": "化名甲", "DATACOUNTRY_NAME": "甲国"}],
            "sys_country": [{"id": "7", "country_key": "101", "name": "ALPHA", "code": "AL", "name_cn": "甲国", "region_key": "1", "region_name": "甲区", "boss_company_id": "BC"}],
        }
        first = store.import_export(source_label="合成导出（测试）", source_digest="digest-repeat", tables=tables)
        self.assertEqual(first.outcome, "imported")

        # 让它过期：来源时间不能改，所以直接换一条来源时间足够旧的同摘要批次。
        self.execute("TRUNCATE galaxy_import_batch CASCADE")
        now = self.database_now()
        self.insert_batch("gib_repeat", started_at=now - RETENTION_WINDOW - timedelta(hours=1), with_children=True)
        self.execute("UPDATE galaxy_import_batch SET source_digest = 'digest-repeat' WHERE id = 'gib_repeat'")
        first_expiry = self.scalar("SELECT expires_at FROM galaxy_import_batch WHERE id = 'gib_repeat'")

        self.cleanup(now)
        self.assertEqual(self.count("galaxy_import_batch"), 0)

        again = store.import_export(source_label="合成导出（测试）", source_digest="digest-repeat", tables=tables)

        self.assertEqual(again.outcome, "imported")
        self.assertNotEqual(again.batch_id, "gib_repeat")
        self.assertEqual(store.current_batch_id(), again.batch_id)
        self.assertGreater(
            self.scalar("SELECT expires_at FROM galaxy_import_batch WHERE id = %s", (again.batch_id,)),
            first_expiry,
            "重导得到的是重新起算九十天的新副本",
        )


class MigrationChainCoverageTest(RetentionPostgresTestCase):
    def test_the_test_database_was_built_by_the_alembic_chain_up_to_this_revision(self) -> None:
        """建库确实跑到了本 revision——否则上面每一条断言都是在一个没有清理函数的库上跑。

        观察点取建库时实际前滚到的 head，不取测试自己维护的清单；再配一条
        "清理函数真的在库里"，两者合起来才说明链跑到了这条 revision。
        """
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        head = ScriptDirectory.from_config(Config(str(REPOSITORY_ROOT / "alembic.ini"))).get_current_head()

        self.assertEqual(applied_head(), head)
        self.assertEqual(head, "0054_retention_cleanup")
        # 业务测试库不留版本表：它每轮都重建，不是被 alembic 增量维护的库，
        # 而迁移门禁有一条卫生断言专门盯着这一点。
        self.assertEqual(
            self.scalar("SELECT to_regclass('public.alembic_version')::text"),
            None,
        )
        self.assertEqual(
            self.scalar(
                "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'public' AND p.proname = 'lingxi_retention_cleanup'"
            ),
            1,
        )

    def test_the_revision_file_is_the_only_carrier_of_the_retention_ddl(self) -> None:
        """编号 SQL 已冻结：保留清理的 DDL 不得再以 `migrations/*.sql` 的形式存在。

        真回来一个 `013_*.sql` 的话，#53 的逐字节门禁会红（它不在基线的 CHAIN 里），
        但那道门禁要有容器才跑；这条不需要。
        """
        self.assertTrue(RETENTION_REVISION.is_file())
        stray = [path.name for path in (REPOSITORY_ROOT / "migrations").glob("013*.sql")]
        self.assertEqual(stray, [])
        self.assertIn("lingxi_retention_cleanup", UPGRADE_SQL)
        self.assertIn("DROP FUNCTION IF EXISTS public.lingxi_retention_cleanup", DOWNGRADE_SQL)


if __name__ == "__main__":
    unittest.main()
