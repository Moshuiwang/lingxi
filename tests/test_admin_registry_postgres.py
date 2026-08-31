"""管理员角色登记表的真库断言（Issue #95 S-M-01，需要真实 PostgreSQL 16）。

唯一索引、CHECK 约束、真实读表这类断言必须在真库上验证，不用 mock（验证与门禁
第五节）。建库走 `tests/postgres_schema.py` 的整条 alembic 链，与生产同源。

认领断言（opus 批量审查 P3 修复：与 `docs/技术设计/验收矩阵-接入与管理.md` 实际行号核对，
此前四条 V-编号与矩阵实际定义逐条错位）：
- V-管理-21：``RealTimeLookupTests`` 里"未登记 → ``None``"（``test_unknown_open_
  id_returns_none``）与"已登记 → 读回完整条目、三类角色全授予"
  （``test_seeded_identity_reads_back_with_all_roles``）——登记表按
  ``feishu_open_id`` 判定当前是否为有效管理员的数据库落点。
- V-管理-22：``RealTimeLookupTests.test_revoking_takes_effect_on_the_very_next_
  read``——判定实时读表、不缓存：一个此前有效的条目被撤销后，下一次判定立即读到
  拒绝结果（验证与门禁 §八 第 4 点的否定断言：用一个原本有效、随后被撤销的对象
  证明"不缓存"，而不是只证明"从未注册的对象被拒绝"）。
- V-管理-23：``UniqueActiveIdentityConstraintTests``——同一飞书身份同一时刻只
  允许一条 ``active`` 登记，由部分唯一索引强制（不是应用层"恰好没有并发"）；
  已撤销的历史行不阻挡同一身份重新登记为 ``active``。
- V-管理-28：``SeedIdempotencyTests`` + ``SeedConflictDetectionTests``——种子
  命令的写入路径幂等（真库并发下不产生第二条 ``active`` 记录），且"没插入"不再
  无条件当成幂等成功：已有不一致的 ``active`` 行（标签或角色与本次意图播种的
  内容不一致）时响亮拒绝（opus 批量审查 P2 修复）；三类角色合并授予的部分见
  ``test_first_seed_inserts_and_grants_all_three_roles``。

以下三个测试类没有一一对应的矩阵编号，是 B1（唯一管理员 + 三类角色合并授予的
数据库编码）与 V-管理-21/23 判定逻辑的支撑性真库实现验证，不单独认领矩阵行：
``EntryStatusRevokedAtConsistencyCheckTests``（``entry_status``/``revoked_at``
一致性 CHECK、``label`` 非空 CHECK）、``SingleActiveAdminConstraintTests``
（PM 2026-08-24 终裁"唯一管理员"——全表至多一条 ``active`` 行，比
``UniqueActiveIdentityConstraintTests`` 的"同一身份至多一条"更严）、
``MergedRoleGrantCheckConstraintTests``（同一终裁"三类角色合并授予"——``active``
行三类角色列必须全为 ``TRUE``）。``AdminQueriesTests`` 是 V-管理-26 判定逻辑
（该矩阵行的正式评据是"L2 纯逻辑，含伪造查询端口"）在真实 ``app_user``/
``inbound_event`` 上的补充实现验证，同样不是该行的正式评据来源。
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.adapters.admin_registry import (
    PostgresAdminQueries,
    PostgresAdminRegistryLookup,
    seed_admin_registry_entry,
)
from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_local_permission import PostgresLocalPermissionOverrideStore
from lingxi.adapters.postgres_onboarding_failure import PostgresFailureReasonRecorder
from lingxi.core.admin.registry import ALL_ADMIN_ROLES, AdminRegistrySeedConflict
from lingxi.core.ids import new_id
from lingxi.core.permission.local_override import OverrideDirection

DSN = os.environ.get("LINGXI_POSTGRES_DSN")
SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，管理员登记表的真库断言未验证（需真实 PostgreSQL 16）"
    if not DSN
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，管理员登记表的真库断言未验证"
)


@unittest.skipUnless(DSN and psycopg_available(), SKIP_REASON)
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

    def add_user(
        self,
        *,
        user_id: str = "usr_1",
        open_id: str = "ou_1",
        provisioning_state: str = "active",
        permission_version: int = 2,
        email: str | None = None,
        display_name: str | None = "化名",
    ) -> None:
        self.execute(
            """INSERT INTO app_user
                 (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name,
                  department, tenant_key, provisioning_state, permission_version, email)
               VALUES (%s, %s, %s, %s, %s, '测试部门', 'tk_1', %s, %s, %s)""",
            (
                user_id,
                open_id,
                f"fs_{open_id}",
                f"un_{open_id}",
                display_name,
                provisioning_state,
                permission_version,
                email,
            ),
        )

    def insert_current_galaxy_batch(self, *, batch_id: str = "gib_display_names_fixture") -> str:
        """插入一个「当前有效」的银河批次（``status='complete'``、``started_at``
        为当下——``expires_at`` 由迁移触发器从它推导，足够新就不会过期），供
        ``company_label`` 真库断言使用。"""

        self.execute(
            """INSERT INTO galaxy_import_batch
                 (id, source_label, source_digest, status, started_at, completed_at)
               VALUES (%s, %s, %s, 'complete', now(), now())""",
            (batch_id, f"合成导出 {batch_id}", f"digest-{batch_id}"),
        )
        return batch_id

    def add_galaxy_country(
        self, *, batch_id: str, source_id: str, boss_company_id: str, name_cn: str
    ) -> None:
        self.execute(
            """INSERT INTO galaxy_country (batch_id, source_id, boss_company_id, name_cn)
               VALUES (%s, %s, %s, %s)""",
            (batch_id, source_id, boss_company_id, name_cn),
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

    def test_seed_no_longer_accepts_a_roles_argument(self) -> None:
        """三类角色固定合并授予（opus 批量审查 P2 修复）：`seed_admin_registry_entry`
        结构上不再能被调用去只授予部分角色——这条用例钉住"参数已经被移除"这个
        事实本身，取代此前验证"传子集只授予那几列"的
        `test_partial_roles_grant_only_the_selected_columns`（该能力已被移除）。
        """

        with self.assertRaises(TypeError):
            seed_admin_registry_entry(  # type: ignore[call-arg]
                self._dsn,
                feishu_open_id="ou_partial",
                label="future-admin",
                roles=frozenset(),
            )


class SeedConflictDetectionTests(AdminRegistryPostgresTestCase):
    """种子写入检测到已有 active 行与本次意图播种的内容不一致时必须响亮拒绝，
    不能把"没插入"直接当成"幂等成功"（opus 批量审查 P2 修复）。"""

    def test_a_matching_existing_row_is_still_a_quiet_idempotent_success(self) -> None:
        """对照组：字段完全一致时，"没插入"确实就是幂等成功，不抛异常。"""

        seed_admin_registry_entry(self._dsn, feishu_open_id="ou_consistent", label="same-label")

        second = seed_admin_registry_entry(
            self._dsn, feishu_open_id="ou_consistent", label="same-label"
        )

        self.assertFalse(second)

    def test_an_existing_row_with_a_different_label_raises_a_conflict(self) -> None:
        seed_admin_registry_entry(self._dsn, feishu_open_id="ou_relabeled", label="original-label")

        with self.assertRaises(AdminRegistrySeedConflict) as raised:
            seed_admin_registry_entry(
                self._dsn, feishu_open_id="ou_relabeled", label="a-different-label"
            )

        self.assertEqual(raised.exception.mismatched_fields, ("label",))
        # 报错信息只列字段名，不回显任何取到的值——尤其不回显 open_id 或 label。
        self.assertNotIn("ou_relabeled", str(raised.exception))
        self.assertNotIn("original-label", str(raised.exception))
        self.assertNotIn("a-different-label", str(raised.exception))

    def test_roles_mismatch_branch_is_unreachable_now_that_the_check_exists(self) -> None:
        """`seed_admin_registry_entry` 的回读核验同时比对"三类角色是否全真"
        （不只是 label），但迁移 0067 新增的 CHECK 让"active 行角色不全"在
        数据库层面已经无法插入或改出来——这条用例是**诚实的否定证据**：证明
        那条角色分支此刻在真库上确实打不到（被 CHECK 挡在了更早的一层），
        不是靠一个永远不会失败的假分支充数。角色比对因此是文档化的纵深防线，
        不是当前唯一防线——本迁移的 CHECK 才是。"""

        seed_admin_registry_entry(self._dsn, feishu_open_id="ou_role_drift", label="x")

        with self.assertRaises(Exception):
            self.execute(
                "UPDATE admin_registry SET ops_admin_granted = FALSE"
                " WHERE feishu_open_id = %s",
                ("ou_role_drift",),
            )


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


class SingleActiveAdminConstraintTests(AdminRegistryPostgresTestCase):
    """否定断言：唯一管理员（PM 2026-08-24 终裁）由 `admin_registry_
    single_active_admin_idx` 强制——**全表**至多一条 `active` 行，不只是
    "同一 open_id 至多一条"（那是 `UniqueActiveIdentityConstraintTests` 已经
    覆盖的更弱约束）。"""

    def test_a_second_identity_cannot_also_become_active(self) -> None:
        """两个不同的 open_id 都尝试成为当前有效管理员：第一个成功，第二个必须
        被数据库拒绝——即使它们的 open_id 完全不同，不会撞上按身份分区的那条
        部分唯一索引。"""

        seed_admin_registry_entry(self._dsn, feishu_open_id="ou_first_admin", label="a")

        with self.assertRaises(Exception):
            self.execute(
                "INSERT INTO admin_registry"
                " (id, feishu_open_id, label, permission_admin_granted,"
                "  ops_admin_granted, super_admin_granted, entry_status)"
                " VALUES ('adm_second', 'ou_second_admin', 'b', TRUE, TRUE, TRUE, 'active')"
            )

        active_count = self.query(
            "SELECT count(*) FROM admin_registry WHERE entry_status = 'active'"
        )[0][0]
        self.assertEqual(active_count, 1)

    def test_revoking_the_only_active_admin_frees_the_single_active_slot(self) -> None:
        """撤销唯一的当前有效管理员之后，另一个身份可以成为新的唯一有效管理员——
        约束挡的是"同时有两条 active"，不是"永远只能是同一个人"。"""

        seed_admin_registry_entry(self._dsn, feishu_open_id="ou_outgoing_admin", label="a")
        self.execute(
            "UPDATE admin_registry SET entry_status = 'revoked', revoked_at = now()"
            " WHERE feishu_open_id = %s",
            ("ou_outgoing_admin",),
        )

        inserted = seed_admin_registry_entry(
            self._dsn, feishu_open_id="ou_incoming_admin", label="b"
        )

        self.assertTrue(inserted)
        active_count = self.query(
            "SELECT count(*) FROM admin_registry WHERE entry_status = 'active'"
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


class MergedRoleGrantCheckConstraintTests(AdminRegistryPostgresTestCase):
    """否定断言：三类角色合并授予（PM 2026-08-24 终裁）由迁移 0067 新增的 CHECK
    强制——任何 `entry_status = 'active'` 的行，三类角色列必须全部为 TRUE，
    数据库层面拒绝"active 但只授予部分角色"的半授权行。"""

    def test_active_row_with_all_roles_true_is_accepted(self) -> None:
        self.execute(
            "INSERT INTO admin_registry"
            " (id, feishu_open_id, label, permission_admin_granted,"
            "  ops_admin_granted, super_admin_granted, entry_status)"
            " VALUES ('adm_ok', 'ou_ok', 'x', TRUE, TRUE, TRUE, 'active')"
        )

        count = self.query("SELECT count(*) FROM admin_registry WHERE id = 'adm_ok'")[0][0]
        self.assertEqual(count, 1)

    def test_active_row_missing_any_single_role_is_rejected(self) -> None:
        for missing_column in (
            "permission_admin_granted",
            "ops_admin_granted",
            "super_admin_granted",
        ):
            with self.subTest(missing_column=missing_column):
                columns = {
                    "permission_admin_granted": "TRUE",
                    "ops_admin_granted": "TRUE",
                    "super_admin_granted": "TRUE",
                }
                columns[missing_column] = "FALSE"
                with self.assertRaises(Exception):
                    self.execute(
                        "INSERT INTO admin_registry"
                        " (id, feishu_open_id, label, permission_admin_granted,"
                        "  ops_admin_granted, super_admin_granted, entry_status)"
                        f" VALUES ('adm_partial_{missing_column}', 'ou_partial_{missing_column}',"
                        f" 'x', {columns['permission_admin_granted']},"
                        f" {columns['ops_admin_granted']}, {columns['super_admin_granted']},"
                        " 'active')"
                    )

    def test_revoked_row_with_no_roles_is_accepted(self) -> None:
        """已撤销行不受这条 CHECK 约束——撤销时不强制清空角色列，历史记录
        原样保留（迁移 0067 文件头部的既定语义）。"""

        self.execute(
            "INSERT INTO admin_registry"
            " (id, feishu_open_id, label, entry_status, revoked_at)"
            " VALUES ('adm_revoked_no_roles', 'ou_revoked_no_roles', 'x', 'revoked', now())"
        )

        count = self.query(
            "SELECT count(*) FROM admin_registry WHERE id = 'adm_revoked_no_roles'"
        )[0][0]
        self.assertEqual(count, 1)


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
    # ``add_user``/``add_event`` 现由共同基类 ``AdminRegistryPostgresTestCase``
    # 提供（Issue #337 新增的 ``TraceLookupTests`` 同样需要这两个夹具），本类
    # 不再重复定义。

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

    def add_pending_action_for_override(self, *, pending_id: str) -> str:
        """插入一条最小可用的已终态 ``pending_action`` 行，仅用于满足迁移
        ``0072`` 的 ``pending_action_id`` 外键——与
        ``tests/test_pending_action_postgres.py`` 的
        ``add_bystander_pending_action`` 同一手法。"""

        now = datetime.now(timezone.utc)
        self.execute(
            """INSERT INTO pending_action
                   (id, action_type, target_open_id, target_state_snapshot,
                    initiated_by_open_id, status, confirm_deadline_at,
                    decided_at, decided_by_open_id)
                 VALUES (%s, 'suspend_user', %s, 'enabled', %s, 'executed', %s, %s, %s)""",
            (
                pending_id,
                f"ou_bystander_for_{pending_id}",
                "ou_admin",
                now + timedelta(minutes=10),
                now,
                "ou_admin",
            ),
        )
        return pending_id

    def test_user_status_reports_zero_overrides_when_none_exist(self) -> None:
        """⑥/admin user 无覆盖用户输出零回归：既有两个用例（``test_user_status_
        found``/``test_user_status_not_found``）新增字段前完全不知道
        ``local_overrides`` 这回事，本用例显式钉住"没有覆盖行时返回空元组"这个
        契约，防止未来改动让默认值意外变成 ``None`` 或抛异常。"""

        self.add_user(open_id="ou_target", provisioning_state="active", permission_version=1)
        queries = PostgresAdminQueries(self._dsn)

        status = queries.user_status(identifier="ou_target")

        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status.local_overrides, ())

    def test_user_status_lists_active_overrides_and_excludes_revoked_ones(self) -> None:
        """/admin user 回显「当前生效本地覆盖」段（卡 B）：只列 active 行，
        每行含 override_id（``lpo_*``）、方向、公司、指标、原因、创建时间；
        已撤销的历史行不出现。"""

        self.add_user(user_id="usr_target", open_id="ou_target")
        override_store = PostgresLocalPermissionOverrideStore(self._dsn)

        grant_pending_id = self.add_pending_action_for_override(pending_id=new_id("pac"))
        active_override = override_store.insert(
            user_id="usr_target",
            direction=OverrideDirection.GRANT,
            company_id="1011",
            metric_name="daily_active",
            reason="特批授权",
            initiated_by_open_id="ou_admin",
            pending_action_id=grant_pending_id,
        )

        suppress_pending_id = self.add_pending_action_for_override(pending_id=new_id("pac"))
        revoked_override = override_store.insert(
            user_id="usr_target",
            direction=OverrideDirection.SUPPRESS,
            company_id="1012",
            metric_name="revenue",
            reason="临时抑制",
            initiated_by_open_id="ou_admin",
            pending_action_id=suppress_pending_id,
        )
        revoke_pending_id = self.add_pending_action_for_override(pending_id=new_id("pac"))
        override_store.revoke(
            override_id=revoked_override.id, revoked_pending_action_id=revoke_pending_id
        )

        queries = PostgresAdminQueries(self._dsn)
        status = queries.user_status(identifier="ou_target")

        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(len(status.local_overrides), 1, "已撤销的行不应出现")
        override_view = status.local_overrides[0]
        self.assertEqual(override_view.override_id, active_override.id)
        self.assertTrue(override_view.override_id.startswith("lpo_"))
        self.assertEqual(override_view.direction, "grant")
        self.assertEqual(override_view.company_id, "1011")
        self.assertEqual(override_view.metric_name, "daily_active")
        self.assertEqual(override_view.reason, "特批授权")
        self.assertTrue(override_view.created_at)

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


class TraceLookupTests(AdminRegistryPostgresTestCase):
    """``/admin trace <追溯号>`` 真库集成（Issue #337）：事件时间线 + 开通状态
    （复用既有 ``user_status``）+ 失败原因（新表 ``onboarding_failure``，迁移
    ``0077``，写入方 ``PostgresFailureReasonRecorder``）三者拼接。"""

    def test_unknown_trace_id_returns_none(self) -> None:
        """否定断言：查无此追溯号返回 ``None``，供 ``core/admin/router._render_
        trace`` 回复「不存在」——不是空白也不是抛异常。"""

        queries = PostgresAdminQueries(self._dsn)

        self.assertIsNone(queries.trace_lookup(trace_id="trc_missing"))

    def test_trace_without_failure_reason_reports_state_only(self) -> None:
        """追溯号存在（有入站事件、有开通状态）但没有失败记录：如实回报状态，
        ``failure_reason`` 为 ``None``，不是"查无此追溯号"。"""

        self.add_user(user_id="usr_ok", open_id="ou_ok", provisioning_state="active")
        self.add_event(
            event_id="evt_ok",
            open_id="ou_ok",
            received_at=datetime.now(timezone.utc),
            trace_id="trc_ok",
        )
        queries = PostgresAdminQueries(self._dsn)

        trace = queries.trace_lookup(trace_id="trc_ok")

        self.assertIsNotNone(trace)
        assert trace is not None
        self.assertEqual(trace.event_count, 1)
        self.assertEqual(trace.provisioning_state, "active")
        self.assertEqual(trace.account_state, "enabled")
        self.assertIsNone(trace.failure_reason)

    def test_trace_with_failure_reason_reports_it(self) -> None:
        """Issue #337 的验收关键：管理员凭追溯号真的能查回 failure_reason。"""

        self.add_user(user_id="usr_fail", open_id="ou_fail", provisioning_state="provisioning")
        self.add_event(
            event_id="evt_fail",
            open_id="ou_fail",
            received_at=datetime.now(timezone.utc),
            trace_id="trc_fail",
        )
        recorder = PostgresFailureReasonRecorder(self._dsn)
        recorder.record_failure(
            trace_id="trc_fail",
            failure_reason="directory_unavailable",
            event_type="onboarding.result",
        )
        queries = PostgresAdminQueries(self._dsn)

        trace = queries.trace_lookup(trace_id="trc_fail")

        self.assertIsNotNone(trace)
        assert trace is not None
        self.assertEqual(trace.failure_reason, "directory_unavailable")
        self.assertEqual(trace.failure_event_type, "onboarding.result")
        self.assertTrue(trace.failure_occurred_at)

    def test_trace_result_never_carries_open_id(self) -> None:
        """脱敏断言（Issue #337 范围条目 3）：视图字段里没有任何一个是原始
        open_id——只含运维事实。"""

        self.add_user(
            user_id="usr_secret", open_id="ou_should_not_appear", provisioning_state="active"
        )
        self.add_event(
            event_id="evt_secret",
            open_id="ou_should_not_appear",
            received_at=datetime.now(timezone.utc),
            trace_id="trc_secret",
        )
        queries = PostgresAdminQueries(self._dsn)

        trace = queries.trace_lookup(trace_id="trc_secret")

        self.assertIsNotNone(trace)
        assert trace is not None
        values = [
            trace.trace_id,
            trace.last_event_type,
            trace.last_handled_as,
            trace.provisioning_state,
            trace.account_state,
            trace.failure_reason,
            trace.failure_event_type,
            trace.failure_occurred_at,
        ]
        self.assertNotIn("ou_should_not_appear", values)


class ResolveIdentifierTests(AdminRegistryPostgresTestCase):
    """``PostgresAdminQueries.resolve_identifier``（#439 A 档）：邮箱 → open_id
    真库反查。``app_user.email`` 没有唯一约束（迁移基线只对 ``feishu_open_id``
    建 UNIQUE），零命中/多命中都必须 fail-open（原样返回输入），不猜测。"""

    def test_unique_email_match_resolves_to_open_id(self) -> None:
        self.add_user(open_id="ou_target", email="someone@example.com")
        queries = PostgresAdminQueries(self._dsn)

        self.assertEqual(
            queries.resolve_identifier(identifier="someone@example.com"), "ou_target"
        )

    def test_zero_hits_falls_back_to_the_original_input(self) -> None:
        queries = PostgresAdminQueries(self._dsn)

        self.assertEqual(
            queries.resolve_identifier(identifier="nobody@example.com"), "nobody@example.com"
        )

    def test_multiple_hits_falls_back_to_the_original_input_not_an_arbitrary_pick(self) -> None:
        """否定断言：同一邮箱命中多个 ``app_user`` 行时不猜测选哪一条，原样
        透传，交给下游按既有"未找到"语义处理。"""

        self.add_user(user_id="usr_a", open_id="ou_a", email="dup@example.com")
        self.add_user(user_id="usr_b", open_id="ou_b", email="dup@example.com")
        queries = PostgresAdminQueries(self._dsn)

        resolved = queries.resolve_identifier(identifier="dup@example.com")

        self.assertEqual(resolved, "dup@example.com")
        self.assertNotIn(resolved, ("ou_a", "ou_b"))

    def test_non_email_identifier_is_returned_verbatim_without_querying(self) -> None:
        queries = PostgresAdminQueries(self._dsn)

        self.assertEqual(queries.resolve_identifier(identifier="ou_plain"), "ou_plain")


class ResolveOverrideIdTests(AdminRegistryPostgresTestCase):
    """``PostgresAdminQueries.resolve_override_id``（#439 A 档 revoke 新参数
    形状）：按「open_id + 公司 + 指标」真库反查当前生效的覆盖行 override_id。"""

    def add_pending_action_for_override(self, *, pending_id: str) -> str:
        now = datetime.now(timezone.utc)
        self.execute(
            """INSERT INTO pending_action
                   (id, action_type, target_open_id, target_state_snapshot,
                    initiated_by_open_id, status, confirm_deadline_at,
                    decided_at, decided_by_open_id)
                 VALUES (%s, 'suspend_user', %s, 'enabled', %s, 'executed', %s, %s, %s)""",
            (
                pending_id,
                f"ou_bystander_for_{pending_id}",
                "ou_admin",
                now + timedelta(minutes=10),
                now,
                "ou_admin",
            ),
        )
        return pending_id

    def test_unique_match_resolves_to_the_override_id(self) -> None:
        self.add_user(user_id="usr_target", open_id="ou_target")
        store = PostgresLocalPermissionOverrideStore(self._dsn)
        pending_id = self.add_pending_action_for_override(pending_id=new_id("pac"))
        override = store.insert(
            user_id="usr_target",
            direction=OverrideDirection.GRANT,
            company_id="1011",
            metric_name="daily_active",
            reason="特批",
            initiated_by_open_id="ou_admin",
            pending_action_id=pending_id,
        )
        queries = PostgresAdminQueries(self._dsn)

        resolved = queries.resolve_override_id(
            open_id="ou_target", company_id="1011", metric_name="daily_active"
        )

        self.assertEqual(resolved, override.id)

    def test_no_match_returns_none(self) -> None:
        self.add_user(user_id="usr_target", open_id="ou_target")
        queries = PostgresAdminQueries(self._dsn)

        self.assertIsNone(
            queries.resolve_override_id(
                open_id="ou_target", company_id="1011", metric_name="daily_active"
            )
        )

    def test_revoked_override_no_longer_matches(self) -> None:
        """否定断言：已收回的行不再生效，反查不应该命中它。"""

        self.add_user(user_id="usr_target", open_id="ou_target")
        store = PostgresLocalPermissionOverrideStore(self._dsn)
        grant_pending_id = self.add_pending_action_for_override(pending_id=new_id("pac"))
        override = store.insert(
            user_id="usr_target",
            direction=OverrideDirection.GRANT,
            company_id="1011",
            metric_name="daily_active",
            reason="特批",
            initiated_by_open_id="ou_admin",
            pending_action_id=grant_pending_id,
        )
        revoke_pending_id = self.add_pending_action_for_override(pending_id=new_id("pac"))
        store.revoke(override_id=override.id, revoked_pending_action_id=revoke_pending_id)
        queries = PostgresAdminQueries(self._dsn)

        self.assertIsNone(
            queries.resolve_override_id(
                open_id="ou_target", company_id="1011", metric_name="daily_active"
            )
        )

    def test_ambiguous_match_across_grant_and_suppress_returns_none(self) -> None:
        """否定断言：同一 (公司, 指标) 键理论上可以同时存在一条生效授权与一条
        生效抑制（迁移 ``0072`` 的唯一索引按 ``direction`` 再分），此时反查不应
        猜测该收回哪一条，必须返回 ``None``——调用方据此提示改用管理卡逐行收回
        或直接指定 override_id。"""

        self.add_user(user_id="usr_target", open_id="ou_target")
        store = PostgresLocalPermissionOverrideStore(self._dsn)
        grant_pending_id = self.add_pending_action_for_override(pending_id=new_id("pac"))
        store.insert(
            user_id="usr_target",
            direction=OverrideDirection.GRANT,
            company_id="1011",
            metric_name="daily_active",
            reason="特批",
            initiated_by_open_id="ou_admin",
            pending_action_id=grant_pending_id,
        )
        suppress_pending_id = self.add_pending_action_for_override(pending_id=new_id("pac"))
        store.insert(
            user_id="usr_target",
            direction=OverrideDirection.SUPPRESS,
            company_id="1011",
            metric_name="daily_active",
            reason="临时抑制",
            initiated_by_open_id="ou_admin",
            pending_action_id=suppress_pending_id,
        )
        queries = PostgresAdminQueries(self._dsn)

        self.assertIsNone(
            queries.resolve_override_id(
                open_id="ou_target", company_id="1011", metric_name="daily_active"
            )
        )

    def test_unknown_open_id_returns_none(self) -> None:
        queries = PostgresAdminQueries(self._dsn)

        self.assertIsNone(
            queries.resolve_override_id(
                open_id="ou_never_existed", company_id="1011", metric_name="daily_active"
            )
        )


class DisplayNamesTests(AdminRegistryPostgresTestCase):
    """``PostgresAdminQueries`` 的 ``AdminDisplayNames`` 结构性实现（Trace #469
    S-1）：``user_label``/``company_label``/``metric_label`` 三个真库/真配置
    查询，管理员可见文案「姓名（邮箱）」「中文名（编号）」的数据源。"""

    # ---- user_label ---------------------------------------------------

    def test_user_label_shows_name_and_email_when_both_present(self) -> None:
        self.add_user(open_id="ou_target", display_name="张三", email="zhangsan@example.com")
        queries = PostgresAdminQueries(self._dsn)

        self.assertEqual(
            queries.user_label(open_id="ou_target"), "张三（zhangsan@example.com）"
        )

    def test_user_label_falls_back_to_display_name_only(self) -> None:
        self.add_user(open_id="ou_target", display_name="张三", email=None)
        queries = PostgresAdminQueries(self._dsn)

        self.assertEqual(queries.user_label(open_id="ou_target"), "张三")

    # 不覆盖"email 有值但 display_name 缺失"与"两者都缺失但行存在"这两种
    # 组合——``app_user`` 的 CHECK 约束（迁移基线，identity 字段全有全无）
    # 要求 ``feishu_open_id`` 非空时 ``display_name`` 必须同为非空，真实数据
    # 库结构上不可能出现这样的行；``user_label`` 方法本身的"两者皆空退化为
    # 该用户"分支由下面"查无此用户"（行完全不存在，等价于两者皆空）覆盖。

    def test_user_label_never_echoes_the_open_id_for_an_unknown_user(self) -> None:
        """否定断言（防倒退关卡）：查无此用户时绝不把 open_id 原样拼进返回值。"""

        queries = PostgresAdminQueries(self._dsn)

        label = queries.user_label(open_id="ou_never_registered")

        self.assertEqual(label, "该用户")
        self.assertNotIn("ou_never_registered", label)

    # ---- company_label --------------------------------------------------

    def test_company_label_shows_chinese_name_and_id_from_the_current_batch(self) -> None:
        batch_id = self.insert_current_galaxy_batch()
        self.add_galaxy_country(
            batch_id=batch_id, source_id="7", boss_company_id="1011", name_cn="壹壹测试公司"
        )
        queries = PostgresAdminQueries(self._dsn)

        self.assertEqual(queries.company_label(company_id="1011"), "壹壹测试公司（1011）")

    def test_company_label_falls_back_to_the_raw_id_without_a_current_batch(self) -> None:
        """没有任何有效银河批次（从未导入，或全部过期）时原样展示编号——公司
        编号是业务代码，允许这条兜底（不是 ou_/lpo_/pac_ 一类内部标识）。"""

        queries = PostgresAdminQueries(self._dsn)

        self.assertEqual(queries.company_label(company_id="1011"), "1011")

    def test_company_label_falls_back_to_the_raw_id_when_not_found_in_the_current_batch(
        self,
    ) -> None:
        batch_id = self.insert_current_galaxy_batch()
        self.add_galaxy_country(
            batch_id=batch_id, source_id="7", boss_company_id="1011", name_cn="壹壹测试公司"
        )
        queries = PostgresAdminQueries(self._dsn)

        self.assertEqual(queries.company_label(company_id="9999"), "9999")

    # ---- metric_label -----------------------------------------------------

    def test_metric_label_reverse_resolves_the_packaged_alias_table(self) -> None:
        """真读随包发布的 ``config/admin_metric_alias_map.toml``（产品负责人
        2026-08-30 填入的九条别名，Trace #469 S-1），不是测试自己造的临时表。"""

        queries = PostgresAdminQueries(self._dsn)

        self.assertEqual(queries.metric_label(metric_id="sub_new_count"), "新增订户数")
        self.assertEqual(queries.metric_label(metric_id="exchange_rate"), "汇率")

    def test_metric_label_falls_back_to_the_raw_id_when_no_alias_matches(self) -> None:
        queries = PostgresAdminQueries(self._dsn)

        self.assertEqual(queries.metric_label(metric_id="not_a_real_metric"), "not_a_real_metric")

    # ---- company_labels/metric_labels（批量，Trace #469 修复包 B，B-7）------

    def test_company_labels_batch_matches_per_item_results_for_every_id(self) -> None:
        """批量方法必须与逐个调用 ``company_label`` 结果逐项相同——含命中、
        未命中、以及混合在同一批里的情形，一次真库调用覆盖三种取值。"""

        batch_id = self.insert_current_galaxy_batch()
        self.add_galaxy_country(
            batch_id=batch_id, source_id="7", boss_company_id="1011", name_cn="壹壹测试公司"
        )
        self.add_galaxy_country(
            batch_id=batch_id, source_id="8", boss_company_id="1012", name_cn="壹贰测试公司"
        )
        queries = PostgresAdminQueries(self._dsn)

        result = queries.company_labels(company_ids=["1011", "1012", "9999"])

        self.assertEqual(
            result,
            {
                "1011": "壹壹测试公司（1011）",
                "1012": "壹贰测试公司（1012）",
                "9999": "9999",
            },
        )

    def test_company_labels_without_a_current_batch_falls_back_to_raw_ids(self) -> None:
        queries = PostgresAdminQueries(self._dsn)

        result = queries.company_labels(company_ids=["1011", "1012"])

        self.assertEqual(result, {"1011": "1011", "1012": "1012"})

    def test_company_labels_empty_input_returns_empty_mapping_without_querying(self) -> None:
        queries = PostgresAdminQueries(self._dsn)

        self.assertEqual(queries.company_labels(company_ids=[]), {})

    def test_metric_labels_batch_matches_per_item_results_for_every_id(self) -> None:
        queries = PostgresAdminQueries(self._dsn)

        result = queries.metric_labels(
            metric_ids=["sub_new_count", "exchange_rate", "not_a_real_metric"]
        )

        self.assertEqual(
            result,
            {
                "sub_new_count": "新增订户数",
                "exchange_rate": "汇率",
                "not_a_real_metric": "not_a_real_metric",
            },
        )

    def test_metric_labels_empty_input_returns_empty_mapping(self) -> None:
        queries = PostgresAdminQueries(self._dsn)

        self.assertEqual(queries.metric_labels(metric_ids=[]), {})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
