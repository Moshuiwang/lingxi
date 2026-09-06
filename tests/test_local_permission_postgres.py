"""``local_permission_override``（迁移 ``0072``）的真库断言（Issue #319 S-P-1a，
需要真实 PostgreSQL 16）。

约束类断言必须在真库上验证，不用 mock（验证与门禁第五节）：

- 读路径（:meth:`PostgresLocalPermissionOverrideStore.effective_entries`）只返回
  ``entry_status='active'`` 的行，按用户隔离；
- 写路径（:meth:`~.insert`）先做字段校验再写库，撞上同用户同极性同公司同指标的
  部分唯一索引时转译为 :class:`DuplicateActiveOverrideError`；
- **没有确认卡就写不进去**：伪造/不存在的 ``pending_action_id`` 被外键拒绝
  （否定断言，对应 #319「可观察完成标准」第二条）；
- :meth:`~.revoke` 是条件更新，只对当前 ``active`` 的行生效，收回后同一行不再
  出现在 :meth:`~.effective_entries` 的结果里，但历史仍留在表里（不删除）。
"""

from __future__ import annotations

import os
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_local_permission import (
    DuplicateActiveOverrideError,
    PostgresLocalPermissionOverrideStore,
    _revoke_group_locked,
)
from lingxi.core.ids import new_id
from lingxi.core.permission.local_override import OverrideDirection, resolve_local_overrides

DSN = os.environ.get("LINGXI_POSTGRES_DSN")
SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，本地权限覆盖表的真库断言未验证（需真实 PostgreSQL 16）"
    if not DSN
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，本地权限覆盖表的真库断言未验证"
)

TARGET_USER_ID = "usr_local_override_target"
ADMIN_OPEN_ID = "ou_local_override_admin"


@unittest.skipUnless(DSN and psycopg_available(), SKIP_REASON)
class LocalPermissionOverridePostgresTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._dsn = DSN
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        reset_production_rows(self._dsn)
        self.store = PostgresLocalPermissionOverrideStore(self._dsn)
        self.add_user(user_id=TARGET_USER_ID)

    def query(self, sql: str, parameters: tuple = ()) -> list[tuple]:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(sql, parameters)
            return cursor.fetchall()

    def execute(self, sql: str, parameters: tuple = ()) -> None:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(sql, parameters)

    def add_user(self, *, user_id: str, open_id: str | None = None) -> None:
        anchor = open_id or user_id
        self.execute(
            """INSERT INTO app_user
                 (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name,
                  department, tenant_key, provisioning_state, account_state)
               VALUES (%s, %s, %s, %s, '化名用户', '测试部门', 'tk_test', 'active', 'enabled')""",
            (user_id, anchor, f"fs_{anchor}", f"un_{anchor}"),
        )

    def add_pending_action(self, *, pending_id: str, target_open_id: str | None = None) -> str:
        """插入一条最小可用的 ``pending_action`` 行，供本地覆盖的
        ``pending_action_id`` 外键引用。``action_type`` 借用迁移 ``0068`` 现有的
        合法取值之一——S-P-1b 落地前该 CHECK 尚未扩充本地权限专属取值（迁移
        ``0072`` 文件头部「已知边界」），本测试只需要"这一行存在"这个事实成立。

        ``target_open_id`` 默认从 ``pending_id`` 派生、每次调用各不相同：迁移
        ``0068`` 的 ``pending_action_single_pending_target_idx`` 部分唯一索引
        要求同一 ``target_open_id`` 同一时刻只能有一条 ``status='pending'`` 的行
        （本测试的桩数据从不流转到终态），固定复用同一个 ``target_open_id`` 会在
        同一用例内第二次调用时撞上该索引，与本测试要验证的内容无关。
        """

        resolved_target_open_id = target_open_id or f"ou_target_for_{pending_id}"
        now = datetime.now(UTC)
        self.execute(
            """INSERT INTO pending_action
                 (id, action_type, target_open_id, target_state_snapshot,
                  initiated_by_open_id, confirm_deadline_at)
               VALUES (%s, 'suspend_user', %s, 'enabled', %s, %s)""",
            (pending_id, resolved_target_open_id, ADMIN_OPEN_ID, now + timedelta(minutes=10)),
        )
        return pending_id

    def _plan(self, pairs=(), all_scope=(), *, explicit: bool = False):
        from lingxi.core.permission.legacy_diff import (
            SHAPE_ALL_SCOPE_EXPLICIT,
            SHAPE_FULL_WILDCARD,
            SHAPE_SPECIFIC,
            LegacyImportPlan,
        )

        shape = SHAPE_SPECIFIC
        if all_scope:
            shape = SHAPE_ALL_SCOPE_EXPLICIT if explicit else SHAPE_FULL_WILDCARD
        return LegacyImportPlan(
            shape=shape,
            pairs=tuple(pairs),
            all_scope_metrics=tuple(all_scope),
            skipped_reasons=(),
            unmapped_companies_kept=0,
        )

    def _now(self):
        return datetime(2026, 9, 2, 8, 0, tzinfo=UTC)

    def insert_override(
        self,
        *,
        user_id: str = TARGET_USER_ID,
        direction: OverrideDirection = OverrideDirection.GRANT,
        company_id: str = "1011",
        metric_name: str = "日活",
        reason: str = "特批",
        pending_id: str | None = None,
    ):
        resolved_pending_id = pending_id or new_id("pac")
        if pending_id is None:
            self.add_pending_action(pending_id=resolved_pending_id)
        return self.store.insert(
            user_id=user_id,
            direction=direction,
            company_id=company_id,
            metric_name=metric_name,
            reason=reason,
            initiated_by_open_id=ADMIN_OPEN_ID,
            pending_action_id=resolved_pending_id,
        )


class InsertAndReadBackTests(LocalPermissionOverridePostgresTestCase):
    def test_inserted_grant_reads_back_as_effective(self) -> None:
        stored = self.insert_override(company_id="1011", metric_name="日活")

        effective = self.store.effective_entries(user_id=TARGET_USER_ID)

        self.assertEqual(len(effective), 1)
        self.assertEqual(effective[0].id, stored.id)
        self.assertEqual(effective[0].entry.key, ("1011", "日活"))
        self.assertIs(effective[0].entry.direction, OverrideDirection.GRANT)

    def test_effective_entries_are_scoped_by_user(self) -> None:
        self.add_user(user_id="usr_other_user")
        self.insert_override(user_id=TARGET_USER_ID, company_id="1011", metric_name="日活")
        self.insert_override(user_id="usr_other_user", company_id="1012", metric_name="收入")

        effective = self.store.effective_entries(user_id=TARGET_USER_ID)

        self.assertEqual(len(effective), 1)
        self.assertEqual(effective[0].entry.user_id, TARGET_USER_ID)

    def test_effective_entries_feed_directly_into_resolve_local_overrides(self) -> None:
        """读路径的产出可以不经转换直接喂给纯函数聚合——真正验证两层接口对得上。"""

        self.insert_override(
            company_id="1011", metric_name="日活", direction=OverrideDirection.GRANT
        )
        self.insert_override(
            company_id="1012", metric_name="收入", direction=OverrideDirection.SUPPRESS
        )

        effective = self.store.effective_entries(user_id=TARGET_USER_ID)
        resolved = resolve_local_overrides(
            user_id=TARGET_USER_ID, entries=(stored.entry for stored in effective)
        )

        self.assertEqual(resolved.grants, frozenset({("1011", "日活")}))
        self.assertEqual(resolved.suppressions, frozenset({("1012", "收入")}))


class LegacyImportPostgresTests(LocalPermissionOverridePostgresTestCase):
    """``import_legacy_plan``/``expand_all_scope_group``（rc25 S-1，Issue #540）的真库断言：
    同事务合成终态 ``pending_action`` + 行、幂等 ``already_present``、「全部」组可整组撤销、
    新指标补行不复活撤销过的指标。"""

    def test_specific_pairs_and_the_all_scope_group_land_in_one_transaction(self) -> None:
        from lingxi.core.permission.legacy_diff import (
            ALL_SCOPE_POSITION_NAME,
            IMPORT_REASON,
            LEGACY_IMPORT_ACTOR,
        )

        report = self.store.import_legacy_plan(
            user_id=TARGET_USER_ID,
            target_open_id=TARGET_USER_ID,
            plan=self._plan(pairs=(("40", "m1"), ("88", "m2")), all_scope=("m1", "m2", "m3")),
            now=self._now(),
        )

        self.assertEqual(
            (report.imported, report.already_present, report.group_created), (5, 0, True)
        )
        self.assertTrue(report.group_id.startswith("lpg_"))
        rows = self.query(
            "SELECT company_id, metric_name, reason, initiated_by_open_id, position_name, company_scope,"
            " permission_group_id, pending_action_id FROM local_permission_override"
            " WHERE user_id = %s AND entry_status = 'active' ORDER BY company_id, metric_name",
            (TARGET_USER_ID,),
        )
        self.assertEqual(
            [(r[0], r[1]) for r in rows],
            [("*", "m1"), ("*", "m2"), ("*", "m3"), ("40", "m1"), ("88", "m2")],
        )
        self.assertTrue(all(r[2] == IMPORT_REASON and r[3] == LEGACY_IMPORT_ACTOR for r in rows))
        for row in rows:
            if row[0] == "*":
                self.assertEqual(
                    (row[4], row[5], row[6]), (ALL_SCOPE_POSITION_NAME, "*", report.group_id)
                )
            else:
                self.assertEqual((row[4], row[5], row[6]), (None, None, None))
        pending_ids = {r[7] for r in rows}
        self.assertEqual(len(pending_ids), 1, "全部行指向同一条合成 pending_action")
        pending = self.query(
            "SELECT action_type, status, card_delivered, reason, target_open_id, decided_by_open_id"
            " FROM pending_action WHERE id = %s",
            (pending_ids.pop(),),
        )[0]
        self.assertEqual(
            pending,
            (
                "local_permission_grant",
                "executed",
                False,
                "legacy_import_2_0",
                TARGET_USER_ID,
                LEGACY_IMPORT_ACTOR,
            ),
        )

    def test_reimport_is_idempotent_and_leaves_no_orphan_pending_action(self) -> None:
        plan = self._plan(pairs=(("88", "m2"),), all_scope=("m1",))
        first = self.store.import_legacy_plan(
            user_id=TARGET_USER_ID, target_open_id="ou_t", plan=plan, now=self._now()
        )
        second = self.store.import_legacy_plan(
            user_id=TARGET_USER_ID, target_open_id="ou_t", plan=plan, now=self._now()
        )

        self.assertEqual((first.imported, first.already_present), (2, 0))
        self.assertEqual(
            (second.imported, second.already_present, second.group_created), (0, 2, False)
        )
        self.assertEqual(second.group_id, first.group_id, "已有「全部」组：沿用组 ID，不建第二组")
        self.assertEqual(self.query("SELECT count(*) FROM local_permission_override")[0][0], 2)
        self.assertEqual(
            self.query("SELECT count(*) FROM pending_action")[0][0],
            1,
            "第二次零写入，不留孤儿终态记录",
        )

    def test_a_partial_reimport_only_adds_the_missing_rows_into_the_same_group(self) -> None:
        self.store.import_legacy_plan(
            user_id=TARGET_USER_ID,
            target_open_id="ou_t",
            plan=self._plan(all_scope=("m1",)),
            now=self._now(),
        )
        report = self.store.import_legacy_plan(
            user_id=TARGET_USER_ID,
            target_open_id="ou_t",
            plan=self._plan(all_scope=("m1", "m2")),
            now=self._now(),
        )
        self.assertEqual(
            (report.imported, report.already_present, report.group_created), (1, 1, False)
        )
        groups = self.query(
            "SELECT DISTINCT permission_group_id FROM local_permission_override WHERE user_id = %s",
            (TARGET_USER_ID,),
        )
        self.assertEqual(len(groups), 1)

    def test_the_all_scope_group_can_be_revoked_as_one_unit(self) -> None:
        report = self.store.import_legacy_plan(
            user_id=TARGET_USER_ID,
            target_open_id="ou_t",
            plan=self._plan(all_scope=("m1", "m2")),
            now=self._now(),
        )
        ids = tuple(
            row[0]
            for row in self.query(
                "SELECT id FROM local_permission_override WHERE permission_group_id = %s ORDER BY id",
                (report.group_id,),
            )
        )
        revoke_pending = self.add_pending_action(pending_id=new_id("pac"))
        self.assertTrue(
            self.store.revoke_group(
                permission_group_id=report.group_id,
                revoked_pending_action_id=revoke_pending,
                expected_override_ids=ids,
            )
        )
        self.assertEqual(self.store.effective_entries(user_id=TARGET_USER_ID), ())

    def test_expand_adds_only_missing_metrics_and_never_revives_a_revoked_one(self) -> None:
        report = self.store.import_legacy_plan(
            user_id=TARGET_USER_ID,
            target_open_id="ou_t",
            plan=self._plan(all_scope=("m1", "m2")),
            now=self._now(),
        )
        # 单独撤销 m2 这一行（模拟管理员逐行收回）。
        m2_id = self.query(
            "SELECT id FROM local_permission_override WHERE permission_group_id = %s AND metric_name = 'm2'",
            (report.group_id,),
        )[0][0]
        self.assertTrue(
            self.store.revoke(
                override_id=m2_id,
                revoked_pending_action_id=self.add_pending_action(pending_id=new_id("pac")),
            )
        )

        added = self.store.expand_all_scope_group(
            user_id=TARGET_USER_ID,
            group_id=report.group_id,
            metrics=("m1", "m2", "m3"),
            now=self._now(),
        )

        self.assertEqual(added, 1, "m1 已有、m2 撤销过不复活，只补 m3")
        active = sorted(
            entry.entry.metric_name
            for entry in self.store.effective_entries(user_id=TARGET_USER_ID)
        )
        self.assertEqual(active, ["m1", "m3"])
        new_row = self.query(
            "SELECT position_name, company_scope, permission_group_id FROM local_permission_override"
            " WHERE metric_name = 'm3'"
        )[0]
        from lingxi.core.permission.legacy_diff import ALL_SCOPE_POSITION_NAME

        self.assertEqual(new_row, (ALL_SCOPE_POSITION_NAME, "*", report.group_id))
        # 按 reason 计数而不是取「最新一条」：撤销用的桩 pending_action 用的是真实
        # 时钟，补行用的是固定时刻，两者先后随运行时刻变化（CI 实测踩过）。
        self.assertEqual(
            self.query(
                "SELECT count(*) FROM pending_action WHERE reason = %s",
                ("legacy_all_scope_refresh",),
            )[0][0],
            1,
        )
        self.assertEqual(
            self.store.expand_all_scope_group(
                user_id=TARGET_USER_ID,
                group_id=report.group_id,
                metrics=("m1", "m2", "m3"),
                now=self._now(),
            ),
            0,
            "再跑一次零写入",
        )

    def test_an_explicit_list_group_carries_its_own_label(self) -> None:
        from lingxi.core.permission.legacy_diff import ALL_SCOPE_EXPLICIT_POSITION_NAME

        report = self.store.import_legacy_plan(
            user_id=TARGET_USER_ID,
            target_open_id="ou_t",
            plan=self._plan(all_scope=("m1", "m2"), explicit=True),
            now=self._now(),
        )
        labels = {
            row[0]
            for row in self.query(
                "SELECT position_name FROM local_permission_override WHERE permission_group_id = %s",
                (report.group_id,),
            )
        }
        self.assertEqual(labels, {ALL_SCOPE_EXPLICIT_POSITION_NAME})

    def test_a_revoked_all_scope_group_is_not_rebuilt_on_reimport(self) -> None:
        """独立审核 P3-5：管理员整组撤销过、当前没有生效组 → 重新导入不重建组；具体行照常。"""

        report = self.store.import_legacy_plan(
            user_id=TARGET_USER_ID,
            target_open_id="ou_t",
            plan=self._plan(all_scope=("m1", "m2")),
            now=self._now(),
        )
        ids = tuple(
            row[0]
            for row in self.query(
                "SELECT id FROM local_permission_override WHERE permission_group_id = %s ORDER BY id",
                (report.group_id,),
            )
        )
        self.assertTrue(
            self.store.revoke_group(
                permission_group_id=report.group_id,
                revoked_pending_action_id=self.add_pending_action(pending_id=new_id("pac")),
                expected_override_ids=ids,
            )
        )

        again = self.store.import_legacy_plan(
            user_id=TARGET_USER_ID,
            target_open_id="ou_t",
            plan=self._plan(pairs=(("88", "m9"),), all_scope=("m1", "m2")),
            now=self._now(),
        )

        self.assertTrue(again.group_skipped_revoked)
        self.assertIsNone(again.group_id)
        self.assertEqual((again.imported, again.already_present), (1, 0))
        active = [
            (e.entry.company_id, e.entry.metric_name)
            for e in self.store.effective_entries(user_id=TARGET_USER_ID)
        ]
        self.assertEqual(active, [("88", "m9")], "组不复活，具体行照常")

    def test_a_singly_revoked_group_metric_or_specific_row_is_not_revived_on_reimport(self) -> None:
        """复核残留项：撤销过的键不复活——组仍在但其中一条被单独撤销、或无组具体行被撤销，
        重新导入同一计划都不重建，计入 ``revoked_skipped``。"""

        plan = self._plan(pairs=(("88", "m9"),), all_scope=("m1", "m2"))
        report = self.store.import_legacy_plan(
            user_id=TARGET_USER_ID, target_open_id="ou_t", plan=plan, now=self._now()
        )
        rows = {
            row[0]: row[1]
            for row in self.query(
                "SELECT metric_name, id FROM local_permission_override WHERE user_id = %s",
                (TARGET_USER_ID,),
            )
        }
        for metric in ("m2", "m9"):
            self.assertTrue(
                self.store.revoke(
                    override_id=rows[metric],
                    revoked_pending_action_id=self.add_pending_action(pending_id=new_id("pac")),
                )
            )

        again = self.store.import_legacy_plan(
            user_id=TARGET_USER_ID, target_open_id="ou_t", plan=plan, now=self._now()
        )

        self.assertEqual((again.imported, again.already_present, again.revoked_skipped), (0, 1, 2))
        self.assertEqual(again.group_id, report.group_id)
        self.assertFalse(again.group_skipped_revoked, "组本身还在，不是整组撤销")
        active = sorted(
            e.entry.metric_name for e in self.store.effective_entries(user_id=TARGET_USER_ID)
        )
        self.assertEqual(active, ["m1"])
        self.assertEqual(
            self.query("SELECT count(*) FROM pending_action WHERE reason = 'legacy_import_2_0'")[0][
                0
            ],
            1,
            "零新增时不留孤儿终态记录",
        )

    def test_reusing_an_existing_group_keeps_its_label(self) -> None:
        from lingxi.core.permission.legacy_diff import ALL_SCOPE_EXPLICIT_POSITION_NAME

        first = self.store.import_legacy_plan(
            user_id=TARGET_USER_ID,
            target_open_id="ou_t",
            plan=self._plan(all_scope=("m1",), explicit=True),
            now=self._now(),
        )
        second = self.store.import_legacy_plan(
            user_id=TARGET_USER_ID,
            target_open_id="ou_t",
            plan=self._plan(all_scope=("m1", "m2")),
            now=self._now(),
        )
        self.assertEqual(second.group_id, first.group_id)
        labels = {
            row[0]
            for row in self.query(
                "SELECT position_name FROM local_permission_override WHERE permission_group_id = %s",
                (first.group_id,),
            )
        }
        self.assertEqual(labels, {ALL_SCOPE_EXPLICIT_POSITION_NAME}, "沿用既有组也沿用其标签")

    def test_expand_for_an_unknown_user_writes_nothing(self) -> None:
        self.assertEqual(
            self.store.expand_all_scope_group(
                user_id="usr_nobody", group_id="lpg_x", metrics=("m1",), now=self._now()
            ),
            0,
        )
        self.assertEqual(self.query("SELECT count(*) FROM pending_action")[0][0], 0)

    def test_the_port_name_import_plan_is_the_same_write_path(self) -> None:
        """端口名 ``import_plan``（开通链调用的名字）必须与 ``import_legacy_plan`` 同一落库路径。"""

        from lingxi.core.permission.legacy_diff import LEGACY_IMPORT_ACTOR

        report = self.store.import_plan(
            user_id=TARGET_USER_ID,
            target_open_id="ou_t",
            plan=self._plan(pairs=(("88", "m1"),)),
            now=self._now(),
        )
        self.assertEqual(report.imported, 1)
        rows = self.query(
            "SELECT initiated_by_open_id FROM local_permission_override WHERE user_id = %s",
            (TARGET_USER_ID,),
        )
        self.assertEqual(rows, [(LEGACY_IMPORT_ACTOR,)])

    def test_an_empty_plan_writes_nothing(self) -> None:
        report = self.store.import_legacy_plan(
            user_id=TARGET_USER_ID, target_open_id="ou_t", plan=self._plan(), now=self._now()
        )
        self.assertEqual((report.imported, report.already_present, report.group_id), (0, 0, None))
        self.assertEqual(self.query("SELECT count(*) FROM pending_action")[0][0], 0)


class DuplicateActiveOverrideTests(LocalPermissionOverridePostgresTestCase):
    """否定断言：迁移 ``0072`` 的部分唯一索引在真库上真实生效。"""

    def test_second_active_grant_for_same_key_is_rejected(self) -> None:
        self.insert_override(
            company_id="1011", metric_name="日活", direction=OverrideDirection.GRANT
        )

        with self.assertRaises(DuplicateActiveOverrideError):
            self.insert_override(
                company_id="1011", metric_name="日活", direction=OverrideDirection.GRANT
            )

        count = self.query(
            "SELECT count(*) FROM local_permission_override"
            " WHERE user_id = %s AND entry_status = 'active'",
            (TARGET_USER_ID,),
        )[0][0]
        self.assertEqual(count, 1)

    def test_grant_and_suppress_can_coexist_for_the_same_key(self) -> None:
        """对照组：不同极性不撞唯一索引——这正是「suppress 赢」判定需要的输入
        （纯函数侧见 ``tests/test_local_override.py`` 的 ``ConflictResolutionTests``）。"""

        self.insert_override(
            company_id="1011", metric_name="日活", direction=OverrideDirection.GRANT
        )
        self.insert_override(
            company_id="1011", metric_name="日活", direction=OverrideDirection.SUPPRESS
        )

        effective = self.store.effective_entries(user_id=TARGET_USER_ID)
        self.assertEqual(len(effective), 2)

    def test_revoking_frees_the_key_for_a_new_active_grant(self) -> None:
        stored = self.insert_override(company_id="1011", metric_name="日活")
        revoke_pending_id = new_id("pac")
        self.add_pending_action(pending_id=revoke_pending_id)
        self.store.revoke(override_id=stored.id, revoked_pending_action_id=revoke_pending_id)

        reinserted = self.insert_override(company_id="1011", metric_name="日活")

        self.assertNotEqual(stored.id, reinserted.id)
        active_count = self.query(
            "SELECT count(*) FROM local_permission_override"
            " WHERE user_id = %s AND entry_status = 'active'",
            (TARGET_USER_ID,),
        )[0][0]
        self.assertEqual(active_count, 1)


class NoConfirmCardNoWriteTests(LocalPermissionOverridePostgresTestCase):
    """否定断言（#319「可观察完成标准」第二条）：没有真实存在的确认卡，写入必须
    在数据库层面被拒绝——用一个从未出现过的 ``pending_action_id`` 证明，而不是
    只证明"正常路径能写入"。"""

    def test_forged_pending_action_id_is_rejected_by_the_foreign_key(self) -> None:
        with self.assertRaises(Exception):
            self.store.insert(
                user_id=TARGET_USER_ID,
                direction=OverrideDirection.GRANT,
                company_id="1011",
                metric_name="日活",
                reason="伪造确认卡",
                initiated_by_open_id=ADMIN_OPEN_ID,
                pending_action_id="pac_never_existed_00000000",
            )

        count = self.query(
            "SELECT count(*) FROM local_permission_override WHERE user_id = %s",
            (TARGET_USER_ID,),
        )[0][0]
        self.assertEqual(count, 0)

    def test_blank_reason_is_rejected_before_any_write_is_attempted(self) -> None:
        """字段校验先于写入（``LocalPermissionOverrideEntry.__post_init__``）：
        非法输入连一次 ``INSERT`` 都不会发出，不依赖数据库 CHECK 兜底。"""

        pending_id = new_id("pac")
        self.add_pending_action(pending_id=pending_id)

        with self.assertRaises(ValueError):
            self.store.insert(
                user_id=TARGET_USER_ID,
                direction=OverrideDirection.GRANT,
                company_id="1011",
                metric_name="日活",
                reason="   ",
                initiated_by_open_id=ADMIN_OPEN_ID,
                pending_action_id=pending_id,
            )

        count = self.query("SELECT count(*) FROM pending_action WHERE id = %s", (pending_id,))[0][0]
        self.assertEqual(count, 1)  # 确认卡本身仍在，只是没有本地覆盖引用它
        override_count = self.query(
            "SELECT count(*) FROM local_permission_override WHERE user_id = %s", (TARGET_USER_ID,)
        )[0][0]
        self.assertEqual(override_count, 0)


class RevokeTests(LocalPermissionOverridePostgresTestCase):
    def test_revoke_marks_entry_inactive_and_removes_it_from_effective_entries(self) -> None:
        stored = self.insert_override(company_id="1011", metric_name="日活")
        revoke_pending_id = new_id("pac")
        self.add_pending_action(pending_id=revoke_pending_id)

        changed = self.store.revoke(
            override_id=stored.id, revoked_pending_action_id=revoke_pending_id
        )

        self.assertTrue(changed)
        self.assertEqual(self.store.effective_entries(user_id=TARGET_USER_ID), ())
        row = self.query(
            "SELECT entry_status, revoked_at, revoked_pending_action_id"
            " FROM local_permission_override WHERE id = %s",
            (stored.id,),
        )[0]
        self.assertEqual(row[0], "revoked")
        self.assertIsNotNone(row[1])
        self.assertEqual(row[2], revoke_pending_id)

    def test_revoking_an_already_revoked_entry_is_a_no_op(self) -> None:
        stored = self.insert_override(company_id="1011", metric_name="日活")
        first_revoke_pending_id = new_id("pac")
        self.add_pending_action(pending_id=first_revoke_pending_id)
        self.store.revoke(override_id=stored.id, revoked_pending_action_id=first_revoke_pending_id)

        second_revoke_pending_id = new_id("pac")
        self.add_pending_action(pending_id=second_revoke_pending_id)
        changed = self.store.revoke(
            override_id=stored.id, revoked_pending_action_id=second_revoke_pending_id
        )

        self.assertFalse(changed)
        # 第一次收回所用的确认卡不被第二次覆盖——条件更新只在 active 时生效。
        row = self.query(
            "SELECT revoked_pending_action_id FROM local_permission_override WHERE id = %s",
            (stored.id,),
        )[0]
        self.assertEqual(row[0], first_revoke_pending_id)

    def test_revoking_unknown_id_is_a_no_op(self) -> None:
        revoke_pending_id = new_id("pac")
        self.add_pending_action(pending_id=revoke_pending_id)

        changed = self.store.revoke(
            override_id="lpo_never_existed_00000000",
            revoked_pending_action_id=revoke_pending_id,
        )

        self.assertFalse(changed)


class DailyActivityStatsTests(LocalPermissionOverridePostgresTestCase):
    """:meth:`PostgresLocalPermissionOverrideStore.daily_activity_stats`（Issue
    #319 S-P-1c，内测每日通报「本地权限覆盖活动」段的哑聚合）。"""

    def test_an_empty_table_reports_all_zeros(self) -> None:
        window_start = datetime(2026, 8, 26, tzinfo=UTC)
        window_end = datetime(2026, 8, 27, tzinfo=UTC)

        stats = self.store.daily_activity_stats(window_start=window_start, window_end=window_end)

        self.assertEqual(stats, (0, 0, 0, 0, 0, 0))

    def test_grant_and_suppress_created_inside_the_window_are_counted_separately(self) -> None:
        window_start = datetime(2026, 8, 26, tzinfo=UTC)
        window_end = datetime(2026, 8, 27, tzinfo=UTC)
        inside_window = window_start + timedelta(hours=6)

        self.insert_override(
            company_id="1011",
            metric_name="日活",
            direction=OverrideDirection.GRANT,
        )
        self.execute(
            "UPDATE local_permission_override SET created_at = %s WHERE company_id = %s",
            (inside_window, "1011"),
        )
        self.insert_override(
            company_id="1012",
            metric_name="收入",
            direction=OverrideDirection.SUPPRESS,
        )
        self.execute(
            "UPDATE local_permission_override SET created_at = %s WHERE company_id = %s",
            (inside_window, "1012"),
        )

        (
            granted_today,
            suppressed_today,
            revoked_today,
            active_grant_total,
            active_suppress_total,
            affected_user_count,
        ) = self.store.daily_activity_stats(window_start=window_start, window_end=window_end)

        self.assertEqual(granted_today, 1)
        self.assertEqual(suppressed_today, 1)
        self.assertEqual(revoked_today, 0)
        self.assertEqual(active_grant_total, 1)
        self.assertEqual(active_suppress_total, 1)
        self.assertEqual(affected_user_count, 1)  # 同一用户两条覆盖，去重后仍是 1

    def test_a_row_created_outside_the_window_is_excluded_from_todays_counts(self) -> None:
        window_start = datetime(2026, 8, 26, tzinfo=UTC)
        window_end = datetime(2026, 8, 27, tzinfo=UTC)
        before_window = window_start - timedelta(hours=1)

        stored = self.insert_override(company_id="1011", metric_name="日活")
        self.execute(
            "UPDATE local_permission_override SET created_at = %s WHERE id = %s",
            (before_window, stored.id),
        )

        stats = self.store.daily_activity_stats(window_start=window_start, window_end=window_end)

        # 窗口外新增：不计入今日新增，但仍计入当前生效总量（不限时间窗口）。
        self.assertEqual(stats[0], 0)  # granted_today
        self.assertEqual(stats[3], 1)  # active_grant_total

    def test_a_revocation_inside_the_window_is_counted_regardless_of_original_direction(
        self,
    ) -> None:
        window_start = datetime(2026, 8, 26, tzinfo=UTC)
        window_end = datetime(2026, 8, 27, tzinfo=UTC)
        inside_window = window_start + timedelta(hours=3)

        stored = self.insert_override(
            company_id="1011",
            metric_name="日活",
            direction=OverrideDirection.SUPPRESS,
        )
        revoke_pending_id = new_id("pac")
        self.add_pending_action(pending_id=revoke_pending_id)
        self.store.revoke(override_id=stored.id, revoked_pending_action_id=revoke_pending_id)
        self.execute(
            "UPDATE local_permission_override SET revoked_at = %s WHERE id = %s",
            (inside_window, stored.id),
        )

        (
            granted_today,
            suppressed_today,
            revoked_today,
            active_grant_total,
            active_suppress_total,
            affected_user_count,
        ) = self.store.daily_activity_stats(window_start=window_start, window_end=window_end)

        self.assertEqual(revoked_today, 1)
        # 已收回：不再计入当前生效总量，也不再计入受影响用户数。
        self.assertEqual(active_grant_total, 0)
        self.assertEqual(active_suppress_total, 0)
        self.assertEqual(affected_user_count, 0)

    def test_affected_user_count_deduplicates_across_users_and_directions(self) -> None:
        self.add_user(user_id="usr_other_local_override_user")
        self.insert_override(
            user_id=TARGET_USER_ID,
            company_id="1011",
            metric_name="日活",
            direction=OverrideDirection.GRANT,
        )
        self.insert_override(
            user_id=TARGET_USER_ID,
            company_id="1012",
            metric_name="收入",
            direction=OverrideDirection.SUPPRESS,
        )
        self.insert_override(
            user_id="usr_other_local_override_user",
            company_id="1011",
            metric_name="日活",
            direction=OverrideDirection.GRANT,
        )

        window_start = datetime(2020, 1, 1, tzinfo=UTC)
        window_end = datetime(2020, 1, 2, tzinfo=UTC)
        stats = self.store.daily_activity_stats(window_start=window_start, window_end=window_end)

        self.assertEqual(stats[3], 2)  # active_grant_total
        self.assertEqual(stats[4], 1)  # active_suppress_total
        self.assertEqual(stats[5], 2)  # affected_user_count：两个用户，去重后为 2


class EntryStatusConsistencyCheckTests(LocalPermissionOverridePostgresTestCase):
    """否定断言：迁移 ``0072`` 的 ``entry_status``/``revoked_at``/
    ``revoked_pending_action_id`` 一致性 CHECK 由数据库强制，不是应用层自觉。"""

    def test_active_row_with_revoked_at_is_rejected(self) -> None:
        pending_id = new_id("pac")
        self.add_pending_action(pending_id=pending_id)

        with self.assertRaises(Exception):
            self.execute(
                """INSERT INTO local_permission_override
                     (id, user_id, direction, company_id, metric_name, reason,
                      initiated_by_open_id, pending_action_id, entry_status, revoked_at)
                   VALUES (%s, %s, 'grant', '1011', '日活', 'x', %s, %s, 'active', now())""",
                (new_id("lpo"), TARGET_USER_ID, ADMIN_OPEN_ID, pending_id),
            )

    def test_revoked_row_without_revoked_pending_action_id_is_rejected(self) -> None:
        pending_id = new_id("pac")
        self.add_pending_action(pending_id=pending_id)

        with self.assertRaises(Exception):
            self.execute(
                """INSERT INTO local_permission_override
                     (id, user_id, direction, company_id, metric_name, reason,
                      initiated_by_open_id, pending_action_id, entry_status, revoked_at)
                   VALUES (%s, %s, 'grant', '1011', '日活', 'x', %s, %s, 'revoked', now())""",
                (new_id("lpo"), TARGET_USER_ID, ADMIN_OPEN_ID, pending_id),
            )

    def test_blank_metric_name_is_rejected(self) -> None:
        pending_id = new_id("pac")
        self.add_pending_action(pending_id=pending_id)

        with self.assertRaises(Exception):
            self.execute(
                """INSERT INTO local_permission_override
                     (id, user_id, direction, company_id, metric_name, reason,
                      initiated_by_open_id, pending_action_id)
                   VALUES (%s, %s, 'grant', '1011', '   ', 'x', %s, %s)""",
                (new_id("lpo"), TARGET_USER_ID, ADMIN_OPEN_ID, pending_id),
            )


class AllScopeExpandVsRevocationTests(LocalPermissionOverridePostgresTestCase):
    """撤销之后到达的「全部」组补齐**必须零写入**（IN-02，Issue #646）。

    缺项是在**另一条独立连接**上读 ``effective_entries`` 算出来的（每日重算与定向重算
    都是这个形状），读完到补齐落库之间，管理员完全可能把整组撤销掉。**新**指标从来没在
    这个组里出现过，因此不被"同组同指标任何状态已有行都不再插入"那道判据拦住——修复前
    它被直接插成 ``active``，一个已经被整组收回的授权就此复活成一条生效权限。

    修复有两半，本类分别取证：补齐前在同一事务里复核这个组还有没有生效授权依据（前两条
    用例），以及把补齐与整组撤销序列化到同一把 ``app_user`` 行锁上（后两条用例）——只加
    一次读检查而不解决锁序列化，撤销仍可能在检查与插入之间提交。
    """

    #: 「全部」组补齐用的翻译映射：``m1`` 已在组里，``m2`` 是"映射新增、组里还缺"的那条。
    MAPPING = {"88": {"职能": ("m1", "m2")}}

    def _import_group(self, *metrics: str):
        """建一个「全部」组，返回 ``(组 ID, 组内全部行 ID)``。"""
        report = self.store.import_legacy_plan(
            user_id=TARGET_USER_ID,
            target_open_id=TARGET_USER_ID,
            plan=self._plan(all_scope=metrics),
            now=self._now(),
        )
        ids = tuple(
            row[0]
            for row in self.query(
                "SELECT id FROM local_permission_override"
                " WHERE permission_group_id = %s ORDER BY id",
                (report.group_id,),
            )
        )
        return report.group_id, ids

    def _revoke_whole_group(self, group_id: str, ids: tuple[str, ...]) -> None:
        self.assertTrue(
            self.store.revoke_group(
                permission_group_id=group_id,
                revoked_pending_action_id=self.add_pending_action(pending_id=new_id("pac")),
                expected_override_ids=ids,
            )
        )

    def _refresh_pending_actions(self) -> int:
        """补齐路径合成的终态 ``pending_action`` 条数——零写入连它都不该建。"""
        return self.query(
            "SELECT count(*) FROM pending_action WHERE reason = %s",
            ("legacy_all_scope_refresh",),
        )[0][0]

    def _patient_store(self) -> PostgresLocalPermissionOverrideStore:
        """锁等待放宽到上限的 store：并发用例要让补齐**停在锁上**而不是当场超时。"""
        from lingxi.adapters.postgres import PostgresTimeouts

        return PostgresLocalPermissionOverrideStore(
            self._dsn,
            timeouts=PostgresTimeouts(
                connect_timeout_seconds=5, statement_timeout_seconds=5, lock_timeout_seconds=5
            ),
        )

    def _wait_until_someone_waits_on_a_lock(self) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            waiting = self.query(
                "SELECT count(*) FROM pg_stat_activity"
                " WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )[0][0]
            if waiting:
                return
            time.sleep(0.02)
        self.fail("并发的补齐没有停在锁上：本用例要验证的序列化前提根本没成立")

    def test_an_already_revoked_group_is_never_expanded(self) -> None:
        """整组已撤销 → ``expand_all_scope_group`` 零写入（不只是不补旧指标，新指标也不补）。"""

        group_id, ids = self._import_group("m1")
        self._revoke_whole_group(group_id, ids)

        added = self.store.expand_all_scope_group(
            user_id=TARGET_USER_ID, group_id=group_id, metrics=("m1", "m2"), now=self._now()
        )

        self.assertEqual(added, 0, "组已整体撤销，一行都不补")
        self.assertEqual(self.store.effective_entries(user_id=TARGET_USER_ID), ())
        self.assertEqual(self._refresh_pending_actions(), 0, "零写入连合成的确认记录都不建")

    def test_a_revocation_between_the_read_and_the_expand_writes_nothing(self) -> None:
        """真库交错：读 ``effective_entries`` → 管理员整组撤销 → 补齐迟到落库。

        缺项用的是重算侧真正调用的 ``missing_all_scope_metrics``，不是手写的常量——
        复现的就是 ``permission_refresh._expand_legacy_all_scope`` /
        ``targeted_recompute._expand_legacy_all_scope`` 的形状。
        """

        from lingxi.core.permission.legacy_diff import missing_all_scope_metrics

        group_id, ids = self._import_group("m1")

        # 1）重算在独立连接上读当前生效条目，算出"映射有、组里缺"的指标。
        entries = tuple(item.entry for item in self.store.effective_entries(user_id=TARGET_USER_ID))
        missing = missing_all_scope_metrics(entries, self.MAPPING)
        self.assertEqual(missing, {group_id: ("m2",)}, "前提：这一轮确实算出了一条缺项")

        # 2）管理员在这中间把整组撤销掉。
        self._revoke_whole_group(group_id, ids)

        # 3）补齐迟到落库。
        added = self.store.expand_all_scope_group(
            user_id=TARGET_USER_ID, group_id=group_id, metrics=missing[group_id], now=self._now()
        )

        self.assertEqual(added, 0, "撤销之后到达的补齐不得产生任何生效权限")
        self.assertEqual(self.store.effective_entries(user_id=TARGET_USER_ID), ())
        self.assertEqual(
            self.query("SELECT count(*) FROM local_permission_override WHERE metric_name = 'm2'")[
                0
            ][0],
            0,
            "被撤销的组不得因为一条新指标而复活出一行",
        )
        self.assertEqual(self._refresh_pending_actions(), 0)

    def test_a_concurrent_expand_waits_for_the_revocation_and_then_writes_nothing(self) -> None:
        """真并发：撤销事务已持有目标用户行锁时，补齐必须等它、并在它之后判定零写入。

        撤销走的是确认卡执行那条路径的形状（先锁 ``app_user`` 行、再在同一事务里翻转
        组内生效行）。补齐在另一条连接上并发发起：它先停在同一把行锁上，撤销提交后才
        继续，此时组里已经没有生效授权依据 → 一行都不补。
        """

        group_id, ids = self._import_group("m1")
        patient = self._patient_store()
        outcome: dict[str, object] = {}

        def expand() -> None:
            try:
                outcome["added"] = patient.expand_all_scope_group(
                    user_id=TARGET_USER_ID,
                    group_id=group_id,
                    metrics=("m1", "m2"),
                    now=self._now(),
                )
            except Exception as error:  # 记下来交主线程断言，不让线程静默死掉
                outcome["error"] = error

        revoke_pending = self.add_pending_action(pending_id=new_id("pac"))
        worker = threading.Thread(target=expand)
        with connect(self._dsn) as holder, holder.cursor() as cursor:
            cursor.execute("SELECT id FROM app_user WHERE id = %s FOR UPDATE", (TARGET_USER_ID,))
            worker.start()
            self._wait_until_someone_waits_on_a_lock()
            self.assertTrue(
                _revoke_group_locked(
                    cursor,
                    permission_group_id=group_id,
                    revoked_pending_action_id=revoke_pending,
                    moment=self._now(),
                    expected_override_ids=ids,
                )
            )
        worker.join(timeout=30)

        self.assertFalse(worker.is_alive(), "并发补齐没有在撤销提交后收口")
        self.assertIsNone(outcome.get("error"), f"并发补齐异常退出：{outcome.get('error')!r}")
        self.assertEqual(outcome.get("added"), 0, "撤销赢了这把锁之后，补齐一行都不写")
        self.assertEqual(self.store.effective_entries(user_id=TARGET_USER_ID), ())

    def test_revoke_group_takes_the_same_user_lock_the_expand_takes(self) -> None:
        """否定断言：``revoke_group`` 必须真的去拿目标用户那把行锁。

        补齐拿的是 ``app_user`` 行锁，而 ``revoke_group`` 修复前只锁组内生效行——两把
        锁互不序列化，撤销可以在补齐"复核组还有效"与"插入新行"之间提交。这里用一条独立
        连接先占住 ``app_user`` 行：``revoke_group`` 必须因此等到锁超时，证明它确实在
        同一把锁上排队。修复前它会径直成功。
        """

        from psycopg.errors import LockNotAvailable

        group_id, ids = self._import_group("m1")
        revoke_pending = self.add_pending_action(pending_id=new_id("pac"))
        with connect(self._dsn) as holder, holder.cursor() as cursor:
            cursor.execute("SELECT id FROM app_user WHERE id = %s FOR UPDATE", (TARGET_USER_ID,))
            with self.assertRaises(LockNotAvailable):
                self.store.revoke_group(
                    permission_group_id=group_id,
                    revoked_pending_action_id=revoke_pending,
                    expected_override_ids=ids,
                )
        self.assertEqual(
            len(self.store.effective_entries(user_id=TARGET_USER_ID)), 1, "撤销没成，组仍然生效"
        )


class DecisionChainStaleComputeTests(LocalPermissionOverridePostgresTestCase):
    """受控交错：过时的一次计算不得盖掉后来的决定（IN-05）。

    上一类停在补齐这一步自己零写入；这一类把**整段**「读取来源 → 检查完整性」放进
    真实交错里跑：决定链先在真库上读到条目、算出缺项，管理员在这中间把整组撤销，补齐
    才拿到锁落库。三条判据：撤销这个较新决定在库里是终局的（组不复活、新指标一行都不
    插）；那一轮的链一个字节都不往库里写；撤销之后重新开始的一轮交出的是撤销之后的
    事实（空集）——过时不会跨轮存活。

    **本类刻意不断言那一轮交出的集合是什么**：读取与补齐之间那个窗口里，本轮的计算
    结果仍来自撤销前那次读取，这是这条链既有的形状（收拢没有改变它，改变它会改变
    用户可见的发布内容，属产品取舍）。真正的承诺是上面三条：不复活、不写入、不跨轮。
    """

    MAPPING = {"88": {"职能": ("m1", "m2")}}

    def _chain(self, store):
        """按真实装配的形状接一条决定链：读取口要包成条目读回口，补行口是同一个 store。"""
        from lingxi.adapters.postgres_local_permission import LocalOverrideEntryReader
        from lingxi.core.permission.decision_chain import (
            AllScopeBackfill,
            LocalOverrideDecisionSource,
        )

        trace: list[tuple[str, object]] = []
        source = LocalOverrideDecisionSource(
            reader=LocalOverrideEntryReader(store),
            on_read_failed=lambda user, error: trace.append(("read_failed", type(error).__name__)),
            metric_translation_map=self.MAPPING,
            clock=self._now,
            backfill=AllScopeBackfill(
                expander=store,
                on_failed=lambda user, error: trace.append(("expand_failed", type(error).__name__)),
                on_succeeded=lambda user, added: trace.append(("expanded", added)),
            ),
        )
        return source, trace

    def _import_group(self, *metrics: str):
        report = self.store.import_legacy_plan(
            user_id=TARGET_USER_ID,
            target_open_id=TARGET_USER_ID,
            plan=self._plan(all_scope=metrics),
            now=self._now(),
        )
        ids = tuple(
            row[0]
            for row in self.query(
                "SELECT id FROM local_permission_override"
                " WHERE permission_group_id = %s ORDER BY id",
                (report.group_id,),
            )
        )
        return report.group_id, ids

    def _patient_store(self) -> PostgresLocalPermissionOverrideStore:
        from lingxi.adapters.postgres import PostgresTimeouts

        return PostgresLocalPermissionOverrideStore(
            self._dsn,
            timeouts=PostgresTimeouts(
                connect_timeout_seconds=5, statement_timeout_seconds=5, lock_timeout_seconds=5
            ),
        )

    def _wait_until_someone_waits_on_a_lock(self) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            waiting = self.query(
                "SELECT count(*) FROM pg_stat_activity"
                " WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )[0][0]
            if waiting:
                return
            time.sleep(0.02)
        self.fail("决定链的补齐没有停在锁上：本用例要验证的交错前提根本没成立")

    def _run_chain_across_a_revocation(self):
        """让一条决定链停在补齐的行锁上，撤销先提交，再让它继续。"""
        from lingxi.core.permission.legacy_diff import missing_all_scope_metrics

        group_id, ids = self._import_group("m1")
        entries = tuple(item.entry for item in self.store.effective_entries(user_id=TARGET_USER_ID))
        self.assertEqual(
            missing_all_scope_metrics(entries, self.MAPPING),
            {group_id: ("m2",)},
            "前提：这一轮确实算出了一条缺项，交错才有意义",
        )

        source, trace = self._chain(self._patient_store())
        outcome: dict[str, object] = {}

        def run_chain() -> None:
            try:
                outcome["resolved"] = source.resolve(TARGET_USER_ID)
            except Exception as error:  # 交主线程断言，不让线程静默死掉
                outcome["error"] = error

        revoke_pending = self.add_pending_action(pending_id=new_id("pac"))
        worker = threading.Thread(target=run_chain)
        with connect(self._dsn) as holder, holder.cursor() as cursor:
            cursor.execute("SELECT id FROM app_user WHERE id = %s FOR UPDATE", (TARGET_USER_ID,))
            worker.start()
            self._wait_until_someone_waits_on_a_lock()
            self.assertTrue(
                _revoke_group_locked(
                    cursor,
                    permission_group_id=group_id,
                    revoked_pending_action_id=revoke_pending,
                    moment=self._now(),
                    expected_override_ids=ids,
                )
            )
        worker.join(timeout=30)

        self.assertFalse(worker.is_alive(), "决定链没有在撤销提交后收口")
        self.assertIsNone(outcome.get("error"), f"决定链异常退出：{outcome.get('error')!r}")
        return source, trace, outcome

    def test_the_revocation_wins_and_the_group_never_comes_back(self) -> None:
        """较新决定是终局的：撤销之后到达的补齐一行都不插，组不复活。"""

        _source, trace, _outcome = self._run_chain_across_a_revocation()

        self.assertEqual(trace, [("expanded", 0)], "补行口报告零新增，且没有走失败分支")
        self.assertEqual(self.store.effective_entries(user_id=TARGET_USER_ID), ())
        self.assertEqual(
            self.query("SELECT count(*) FROM local_permission_override WHERE metric_name = 'm2'")[
                0
            ][0],
            0,
            "被撤销的组不得因为一条新指标而复活出一行",
        )

    def test_the_stale_round_writes_nothing_at_all(self) -> None:
        """那一轮的链一个字节都不往库里写：连合成的确认记录都不留。"""

        self._run_chain_across_a_revocation()

        self.assertEqual(
            self.query(
                "SELECT count(*) FROM pending_action WHERE reason = %s",
                ("legacy_all_scope_refresh",),
            )[0][0],
            0,
        )

    def test_the_next_round_computes_the_facts_after_the_revocation(self) -> None:
        """过时不跨轮存活：撤销之后重新开始的一轮交出的是空集，不是撤销前那份。"""

        source, _trace, _outcome = self._run_chain_across_a_revocation()

        resolved = source.resolve(TARGET_USER_ID)

        self.assertIsNotNone(resolved, "读得出来的空集不是「未装配」")
        assert resolved is not None
        self.assertEqual((resolved.grants, resolved.suppressions), (frozenset(), frozenset()))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
