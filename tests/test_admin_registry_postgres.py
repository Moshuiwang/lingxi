"""管理员角色登记表的真库断言（Issue #95 S-M-01，需要真实 PostgreSQL 16）。

唯一索引、CHECK 约束、真实读表这类断言必须在真库上验证，不用 mock（验证与门禁
第五节）。建库走 `tests/postgres_schema.py` 的整条 alembic 链，与生产同源。

认领断言：
- V-管理-21：登记表实时判定的数据库落点——``PostgresAdminRegistryLookup`` 每次
  都发起新查询，撤销后立即读到新结果（``test_revoking_takes_effect_on_the_very_
  next_read``，验证与门禁 §八 第 4 点的否定断言：默认拒绝用一个原本有效、随后被
  撤销的对象证明"不缓存"，而不是只证明"从未注册的对象被拒绝"）。
- V-管理-22：唯一活跃身份由部分唯一索引强制，不是应用层"恰好没有并发"；
  ``entry_status``/``revoked_at`` 一致性由 CHECK 约束强制。
- V-管理-27：种子命令的写入路径幂等，真库并发下不产生第二条 active 记录。
- V-管理-28：只读查询命令组对真实 ``app_user``/``inbound_event`` 的读取正确性
  （按标识过滤、按时间窗过滤、限制条数）。
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

from postgres_schema import ensure_production_schema, reset_production_rows

from lingxi.adapters.admin_registry import (
    PostgresAdminQueries,
    PostgresAdminRegistryLookup,
    seed_admin_registry_entry,
)
from lingxi.adapters.postgres import connect
from lingxi.core.admin.registry import ALL_ADMIN_ROLES, AdminRole

DSN = os.environ.get("LINGXI_POSTGRES_DSN")
SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，管理员登记表的真库断言未验证（需真实 PostgreSQL 16）"
)


@unittest.skipUnless(DSN, SKIP_REASON)
class AdminRegistryPostgresTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._dsn = DSN
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        reset_production_rows(self._dsn)

    def query(self, sql: str, parameters: tuple = ()) -> list[tuple]:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(sql, parameters)
            return cursor.fetchall()

    def execute(self, sql: str, parameters: tuple = ()) -> None:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(sql, parameters)


class SeedIdempotencyTests(AdminRegistryPostgresTestCase):
    def test_first_seed_inserts_and_grants_all_three_roles(self) -> None:
        inserted = seed_admin_registry_entry(
            self._dsn, feishu_open_id="ou_delegated", label="delegated_subject"
        )

        self.assertTrue(inserted)
        rows = self.query(
            "SELECT permission_admin_granted, ops_admin_granted, super_admin_granted,"
            " entry_status FROM admin_registry WHERE feishu_open_id = %s",
            ("ou_delegated",),
        )
        self.assertEqual(rows, [(True, True, True, "active")])

    def test_repeated_seed_is_idempotent(self) -> None:
        first = seed_admin_registry_entry(
            self._dsn, feishu_open_id="ou_delegated", label="delegated_subject"
        )
        second = seed_admin_registry_entry(
            self._dsn, feishu_open_id="ou_delegated", label="delegated_subject"
        )

        self.assertTrue(first)
        self.assertFalse(second)
        count = self.query(
            "SELECT count(*) FROM admin_registry WHERE feishu_open_id = %s", ("ou_delegated",)
        )[0][0]
        self.assertEqual(count, 1)

    def test_blank_open_id_rejected_before_any_write(self) -> None:
        with self.assertRaises(ValueError):
            seed_admin_registry_entry(self._dsn, feishu_open_id="   ", label="x")

        count = self.query("SELECT count(*) FROM admin_registry")[0][0]
        self.assertEqual(count, 0)

    def test_partial_roles_grant_only_the_selected_columns(self) -> None:
        seed_admin_registry_entry(
            self._dsn,
            feishu_open_id="ou_partial",
            label="future-admin",
            roles=frozenset({AdminRole.OPS_ADMIN}),
        )

        row = self.query(
            "SELECT permission_admin_granted, ops_admin_granted, super_admin_granted"
            " FROM admin_registry WHERE feishu_open_id = %s",
            ("ou_partial",),
        )[0]
        self.assertEqual(row, (False, True, False))


class UniqueActiveIdentityConstraintTests(AdminRegistryPostgresTestCase):
    """否定断言：唯一活跃身份由数据库约束保证，不是应用层"恰好没有并发写入"。"""

    def test_second_active_row_for_the_same_identity_is_rejected(self) -> None:
        seed_admin_registry_entry(self._dsn, feishu_open_id="ou_dup", label="a")

        with self.assertRaises(Exception):
            self.execute(
                "INSERT INTO admin_registry"
                " (id, feishu_open_id, label, permission_admin_granted, entry_status)"
                " VALUES ('adm_dup_2', 'ou_dup', 'b', TRUE, 'active')"
            )

    def test_revoked_row_does_not_block_a_new_active_row(self) -> None:
        self.execute(
            "INSERT INTO admin_registry (id, feishu_open_id, label, entry_status, revoked_at)"
            " VALUES ('adm_old', 'ou_history', 'old', 'revoked', now())"
        )

        inserted = seed_admin_registry_entry(self._dsn, feishu_open_id="ou_history", label="new")

        self.assertTrue(inserted)
        active_count = self.query(
            "SELECT count(*) FROM admin_registry"
            " WHERE feishu_open_id = 'ou_history' AND entry_status = 'active'"
        )[0][0]
        self.assertEqual(active_count, 1)


class EntryStatusRevokedAtConsistencyCheckTests(AdminRegistryPostgresTestCase):
    def test_revoked_without_timestamp_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            self.execute(
                "INSERT INTO admin_registry (id, feishu_open_id, label, entry_status)"
                " VALUES ('adm_bad', 'ou_bad', 'x', 'revoked')"
            )

    def test_active_with_revoked_timestamp_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            self.execute(
                "INSERT INTO admin_registry"
                " (id, feishu_open_id, label, entry_status, revoked_at)"
                " VALUES ('adm_bad2', 'ou_bad2', 'x', 'active', now())"
            )

    def test_blank_label_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            self.execute(
                "INSERT INTO admin_registry (id, feishu_open_id, label)"
                " VALUES ('adm_bad3', 'ou_bad3', '   ')"
            )


class RealTimeLookupTests(AdminRegistryPostgresTestCase):
    def test_unknown_open_id_returns_none(self) -> None:
        lookup = PostgresAdminRegistryLookup(self._dsn)

        self.assertIsNone(lookup.active_entry(open_id="ou_never_seen_anywhere"))

    def test_seeded_identity_reads_back_with_all_roles(self) -> None:
        seed_admin_registry_entry(
            self._dsn, feishu_open_id="ou_seeded", label="delegated_subject"
        )
        lookup = PostgresAdminRegistryLookup(self._dsn)

        entry = lookup.active_entry(open_id="ou_seeded")

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.feishu_open_id, "ou_seeded")
        self.assertEqual(entry.roles, ALL_ADMIN_ROLES)
        self.assertEqual(entry.entry_status, "active")
        self.assertTrue(entry.active)

    def test_revoking_takes_effect_on_the_very_next_read(self) -> None:
        """否定断言（验证与门禁 §八 第 4 点）：条目撤销后新请求立即拒绝，证明判定
        不缓存——用一个此前确实有效、随后被撤销的对象证明，而不是只证明一个
        从未注册过的对象被拒绝。"""

        seed_admin_registry_entry(self._dsn, feishu_open_id="ou_revoke_me", label="x")
        lookup = PostgresAdminRegistryLookup(self._dsn)
        self.assertIsNotNone(lookup.active_entry(open_id="ou_revoke_me"))

        self.execute(
            "UPDATE admin_registry SET entry_status = 'revoked', revoked_at = now()"
            " WHERE feishu_open_id = %s",
            ("ou_revoke_me",),
        )

        self.assertIsNone(lookup.active_entry(open_id="ou_revoke_me"))


class AdminQueriesTests(AdminRegistryPostgresTestCase):
    def add_user(
        self,
        *,
        user_id: str = "usr_1",
        open_id: str = "ou_1",
        provisioning_state: str = "active",
        permission_version: int = 2,
    ) -> None:
        self.execute(
            """INSERT INTO app_user
                 (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name,
                  department, tenant_key, provisioning_state, permission_version)
               VALUES (%s, %s, %s, %s, '化名', '测试部门', 'tk_1', %s, %s)""",
            (user_id, open_id, f"fs_{open_id}", f"un_{open_id}", provisioning_state, permission_version),
        )

    def add_event(
        self,
        *,
        event_id: str,
        open_id: str = "ou_1",
        received_at: datetime,
        handled_as: str = "task_queued",
        trace_id: str,
    ) -> None:
        self.execute(
            """INSERT INTO inbound_event
                 (feishu_event_id, received_at, event_type, user_open_id, handled_as, trace_id)
               VALUES (%s, %s, 'im.message.receive_v1', %s, %s, %s)""",
            (event_id, received_at, open_id, handled_as, trace_id),
        )

    def test_user_status_found(self) -> None:
        self.add_user(open_id="ou_target", provisioning_state="mcp_syncing", permission_version=3)
        queries = PostgresAdminQueries(self._dsn)

        status = queries.user_status(identifier="ou_target")

        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status.provisioning_state, "mcp_syncing")
        self.assertEqual(status.permission_version, 3)
        self.assertEqual(status.account_state, "enabled")

    def test_user_status_not_found(self) -> None:
        queries = PostgresAdminQueries(self._dsn)

        self.assertIsNone(queries.user_status(identifier="ou_missing"))

    def test_recent_events_scoped_by_identifier_and_window(self) -> None:
        now = datetime.now(timezone.utc)
        self.add_event(
            event_id="evt_in_window", open_id="ou_a", received_at=now - timedelta(hours=1), trace_id="trc_in"
        )
        self.add_event(
            event_id="evt_out_of_window",
            open_id="ou_a",
            received_at=now - timedelta(hours=50),
            trace_id="trc_out",
        )
        self.add_event(
            event_id="evt_other_user", open_id="ou_b", received_at=now - timedelta(hours=1), trace_id="trc_other"
        )
        queries = PostgresAdminQueries(self._dsn)

        events = queries.recent_events(identifier="ou_a", window_hours=24, limit=20)

        self.assertEqual([event.trace_id for event in events], ["trc_in"])

    def test_recent_events_without_identifier_covers_all_users(self) -> None:
        now = datetime.now(timezone.utc)
        self.add_event(
            event_id="evt_1", open_id="ou_a", received_at=now - timedelta(minutes=5), trace_id="trc_1"
        )
        self.add_event(
            event_id="evt_2", open_id="ou_b", received_at=now - timedelta(minutes=10), trace_id="trc_2"
        )
        queries = PostgresAdminQueries(self._dsn)

        events = queries.recent_events(identifier=None, window_hours=24, limit=20)

        self.assertEqual({event.trace_id for event in events}, {"trc_1", "trc_2"})

    def test_recent_events_respects_limit_and_orders_newest_first(self) -> None:
        now = datetime.now(timezone.utc)
        for index in range(5):
            self.add_event(
                event_id=f"evt_{index}",
                open_id="ou_a",
                received_at=now - timedelta(minutes=index),
                trace_id=f"trc_{index}",
            )
        queries = PostgresAdminQueries(self._dsn)

        events = queries.recent_events(identifier="ou_a", window_hours=24, limit=2)

        self.assertEqual(len(events), 2)
        self.assertEqual([event.trace_id for event in events], ["trc_0", "trc_1"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
