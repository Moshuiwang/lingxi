"""花名册持久快照的真库断言（Issue #52 / S-B-02）。

认领断言：V-花名册-42（整体替换在单个事务内，中途失败不留半份）、V-花名册-44
（从未有快照时回读得到 ``None``）、V-花名册-45（库里最多一份快照）、V-花名册-46
（元信息里没有任何字段值）。

只有真库能证伪它们：原子性是事务的属性，「最多一份」是唯一约束的属性，CASCADE 是
外键的属性——在假 store 上跑，这三条无论实现怎么写都是绿的。

表结构由 ``migrations/alembic/versions/0063_roster_snapshot.py`` 建立，测试库走
``ensure_production_schema`` 的整条 alembic 链，与生产同源；迁移的逐条真往返
（``downgrade -1`` 之后再 ``upgrade``）由 ``scripts/ci/check_migration_chain.sh``
在 CI 上覆盖，不在这里重复。
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from postgres_schema import ensure_production_schema

from lingxi.adapters import postgres_roster_snapshot
from lingxi.adapters.feishu_roster_bitable import (
    ColumnCount,
    DuplicateCount,
    RosterIntegrity,
    RosterRow,
)
from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_roster_snapshot import PostgresRosterSnapshotStore

SKIP_REASON = "跳过：未设置 LINGXI_POSTGRES_DSN，花名册快照的真库断言未验证（需真实 PostgreSQL 16）"

FAKE_NAME = "化名甲"
FAKE_EMAIL = "jiaming.jia@example.invalid"
FAKE_EMPLOYEE_NO = "700123"
FAKE_PERSONNEL_ID = "fs-u-0001"

NOW = datetime(2026, 8, 17, 3, 0, tzinfo=timezone.utc)


def _row(
    personnel_id: str = FAKE_PERSONNEL_ID,
    *,
    record_id: str = "rec-0001",
    name: str = FAKE_NAME,
    email: str = FAKE_EMAIL,
    employee_no: str = FAKE_EMPLOYEE_NO,
) -> RosterRow:
    return RosterRow(
        personnel_id=personnel_id,
        email=email,
        name=name,
        employee_no=employee_no,
        record_id=record_id,
    )


def _integrity(row_count: int) -> RosterIntegrity:
    return RosterIntegrity(
        row_count=row_count,
        pages_read=3,
        reported_total=row_count,
        total_matches_rows=True,
        blank_column_rows=(ColumnCount("工号", 9), ColumnCount("邮箱", 0)),
        rows_without_personnel_id=1,
        duplicates=(DuplicateCount("personnel_id", 2, 5), DuplicateCount("employee_no", 0, 0)),
    )


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN"), SKIP_REASON)
class RosterSnapshotPostgresTestCase(unittest.TestCase):
    """真库断言的共同底座。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '5s'")
            cursor.execute("TRUNCATE roster_snapshot CASCADE")
        self.store = PostgresRosterSnapshotStore(self._dsn)

    def _counts(self) -> tuple[int, int]:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM roster_snapshot")
            headers = int(cursor.fetchone()[0])
            cursor.execute("SELECT count(*) FROM roster_snapshot_row")
            rows = int(cursor.fetchone()[0])
        return headers, rows


class SnapshotCarrierTest(RosterSnapshotPostgresTestCase):
    def test_a_database_without_any_snapshot_reads_back_as_none(self) -> None:
        # `V-花名册-44`：判定层靠这个 None 区分「保旧」与「还没有任何快照」。
        self.assertIsNone(self.store.load_facts())
        self.assertIsNone(self.store.load())

    def test_rows_are_stored_and_read_back_in_order_without_deduplication(self) -> None:
        rows = (
            _row(),
            # 同一人员 ID 的第二行：实测常态，去重会把歧义变成静默的错误选择。
            _row(record_id="rec-0002", email="jiaming.jia.2@example.invalid"),
            # 没有人员 ID 的行同样照写：它与任何用户都对不上，但这个事实要能查。
            _row("", record_id="rec-0003"),
        )

        self.store.replace(rows, _integrity(len(rows)), captured_at=NOW)
        stored = self.store.load()

        assert stored is not None
        self.assertEqual(stored.rows, rows)
        self.assertEqual(stored.facts.row_count, 3)
        self.assertEqual(stored.facts.captured_at, NOW)

    def test_a_replacement_leaves_exactly_one_snapshot_and_no_orphan_rows(self) -> None:
        # `V-花名册-45`：整体替换，不积累历史版本。
        self.store.replace((_row(), _row(record_id="rec-0002")), _integrity(2), captured_at=NOW)
        first = self.store.load_facts()

        later = NOW + timedelta(days=1)
        self.store.replace((_row("fs-u-0009", record_id="rec-0009"),), _integrity(1), captured_at=later)

        assert first is not None
        self.assertEqual(self._counts(), (1, 1))
        stored = self.store.load()
        assert stored is not None
        self.assertNotEqual(stored.facts.snapshot_id, first.snapshot_id)
        self.assertEqual(stored.facts.captured_at, later)
        self.assertEqual([row.personnel_id for row in stored.rows], ["fs-u-0009"])

    def test_the_singleton_constraint_rejects_a_second_snapshot(self) -> None:
        # 「最多一份」由数据库挡住，而不是靠调用方记得先删。
        self.store.replace((_row(),), _integrity(1), captured_at=NOW)

        with self.assertRaises(Exception):
            with connect(self._dsn) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO roster_snapshot (id, captured_at, row_count, pages_read)"
                    " VALUES ('rsn_second', %s, 1, 1)",
                    (NOW,),
                )

        self.assertEqual(self._counts()[0], 1)

    def test_a_zero_row_snapshot_is_rejected_by_both_layers(self) -> None:
        # 写一份零行快照会静默清空比对基线：适配器先响亮失败，数据库的 CHECK 兜底。
        with self.assertRaises(ValueError):
            self.store.replace((), _integrity(0), captured_at=NOW)

        with self.assertRaises(Exception):
            with connect(self._dsn) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO roster_snapshot (id, captured_at, row_count, pages_read)"
                    " VALUES ('rsn_zero', %s, 0, 1)",
                    (NOW,),
                )

    def test_deleting_the_header_cascades_to_its_rows(self) -> None:
        self.store.replace((_row(), _row(record_id="rec-0002")), _integrity(2), captured_at=NOW)

        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM roster_snapshot")

        self.assertEqual(self._counts(), (0, 0))


class AtomicReplacementTest(RosterSnapshotPostgresTestCase):
    """`V-花名册-42`：替换中途失败，库里仍是替换前那一份完整快照。"""

    def test_a_failure_while_writing_rows_leaves_the_previous_snapshot_intact(self) -> None:
        original = (_row(), _row(record_id="rec-0002"))
        self.store.replace(original, _integrity(2), captured_at=NOW)
        before = self.store.load()
        assert before is not None

        # 让"写行"这一步失败：删旧、写元信息都已经在同一个事务里执行过了。
        # 分两次提交的实现会在这里留下"旧快照没了、新快照只有一半"的状态。
        broken = "INSERT INTO roster_snapshot_row (snapshot_id, row_index, no_such_column) VALUES (%s, %s, %s)"
        with mock.patch.object(postgres_roster_snapshot, "INSERT_ROW_SQL", broken):
            with self.assertRaises(Exception):
                self.store.replace(
                    (_row("fs-u-0009", record_id="rec-0009"),), _integrity(1), captured_at=NOW + timedelta(days=1)
                )

        after = self.store.load()
        assert after is not None
        self.assertEqual(after.facts.snapshot_id, before.facts.snapshot_id)
        self.assertEqual(after.rows, original)
        self.assertEqual(self._counts(), (1, 2))

    def test_a_failure_while_writing_the_header_leaves_the_previous_snapshot_intact(self) -> None:
        original = (_row(),)
        self.store.replace(original, _integrity(1), captured_at=NOW)

        broken = "INSERT INTO roster_snapshot (id, no_such_column) VALUES (%s, %s)"
        with mock.patch.object(postgres_roster_snapshot, "INSERT_SNAPSHOT_SQL", broken):
            with self.assertRaises(Exception):
                self.store.replace((_row("fs-u-0009"),), _integrity(1), captured_at=NOW + timedelta(days=1))

        after = self.store.load()
        assert after is not None
        self.assertEqual(after.rows, original)
        self.assertEqual(self._counts(), (1, 1))


class SnapshotMetadataDisciplineTest(RosterSnapshotPostgresTestCase):
    """`V-花名册-46`：元信息只有计数、列名与判定结果，没有任何人的资料值。"""

    def test_the_header_row_contains_no_roster_field_values(self) -> None:
        self.store.replace((_row(),), _integrity(1), captured_at=NOW)

        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, captured_at, row_count, pages_read, reported_total, total_matches_rows,"
                " rows_without_personnel_id, blank_column_rows, duplicate_keys, duplicate_rows"
                " FROM roster_snapshot"
            )
            header = cursor.fetchone()

        rendered = repr(header)
        for probe in (FAKE_NAME, FAKE_EMAIL, FAKE_EMPLOYEE_NO, FAKE_PERSONNEL_ID):
            self.assertNotIn(probe, rendered)
        # 计数本身必须真的落库，否则"不含值"可以靠什么都不存来满足。
        self.assertIn("工号", rendered)
        self.assertEqual(header[6], 1)

    def test_integrity_counts_survive_the_round_trip(self) -> None:
        self.store.replace((_row(),), _integrity(1), captured_at=NOW)

        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pages_read, reported_total, total_matches_rows, blank_column_rows,"
                " duplicate_keys, duplicate_rows FROM roster_snapshot"
            )
            pages, total, matches, blanks, duplicate_keys, duplicate_rows = cursor.fetchone()

        self.assertEqual(pages, 3)
        self.assertEqual(total, 1)
        self.assertIs(matches, True)
        self.assertEqual(blanks, {"工号": 9, "邮箱": 0})
        self.assertEqual(duplicate_keys, {"personnel_id": 2, "employee_no": 0})
        self.assertEqual(duplicate_rows, {"personnel_id": 5, "employee_no": 0})


if __name__ == "__main__":
    unittest.main()
