"""职位授权范围重叠的真库断言（需要真实 PostgreSQL 16）。

三件事都必须在真库上证明，不用 mock：差集补齐的准备判定与执行插入（只写缺的
项、已持有的原组一行不动、一项都不缺则拒绝且零写入）；撤销只撤本笔新增行（原组
仍 active，重算阶段已登记并能接上发布观察）；撤销回执的「经银河来源仍持有 K 项」
按当时快照现算并随 payload 持久化（K>0 / K=0 / 读不到三态，含后台管理员通配）。
纯逻辑与渲染见 ``tests/test_local_permission_overlap.py``。
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from postgres_schema import psycopg_available
from test_pending_action_postgres import (
    ADMIN_OPEN_ID,
    TARGET_OPEN_ID,
    PendingActionPostgresTestCase,
)

from lingxi.adapters.feishu_roster_bitable import RosterRow
from lingxi.adapters.postgres_admin_followup import PostgresFollowupStore
from lingxi.adapters.postgres_pending_action import PostgresPendingActionStore
from lingxi.adapters.postgres_roster_snapshot import PostgresRosterSnapshotStore
from lingxi.core.admin.card_callback import _outcome_text
from lingxi.core.admin.notification import render_confirm_card, render_group_notice
from lingxi.core.admin.pending_action import PendingActionStatus, PendingActionType
from lingxi.core.ids import new_id
from lingxi.core.permission.targeted_recompute import RecomputeKind, TargetedRecomputeOutcome

DSN = os.environ.get("LINGXI_POSTGRES_DSN")
NOW = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)
COMPANY_A, COMPANY_B = "BC-甲", "BC-乙"
EMPLOYEE_NO, EMAIL, NAME = "10001", "person1@example.invalid", "化名一"

#: 只覆盖本文件用到的组合：销售 ⊂ 运营，好让两笔职位授权部分重叠；``"*"`` 键
#: 只为后台管理员的全公司通配存在。指标名是虚构占位。
METRIC_MAP_TOML = """
[companies."BC-甲"]
"运营" = ["m_a", "m_b", "m_c"]
"销售" = ["m_a"]

[companies."BC-乙"]
"运营" = ["m_a", "m_b", "m_c"]
"销售" = ["m_a"]

[companies."*"]
"后台管理员" = ["m_a", "m_b", "m_c"]
"""


def _galaxy_tables(role_name: str) -> dict[str, list[dict[str, str]]]:
    return {
        "user": [
            {
                "user_id": "G-1",
                "dept_id": "D1",
                "user_name": EMPLOYEE_NO,
                "nick_name": NAME,
                "email": EMAIL,
                "create_time": "2019-01-02 03:04:05",
            }
        ],
        "user_role": [
            {"user_id": "G-1", "role_id": "R-1", "user_name": NAME, "role_name": role_name}
        ],
        "role_menu": [
            {"role_id": "R-1", "menu_id": "M1", "role_name": role_name, "menu_name": "报表"}
        ],
        "sys_user_datacountry": [
            {
                "USER_ID": "G-1",
                "DATACOUNTRY_ID": "101",
                "USER_NAME": NAME,
                "DATACOUNTRY_NAME": "甲国",
            }
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
                "boss_company_id": COMPANY_A,
            }
        ],
    }


@unittest.skipUnless(DSN and psycopg_available(), "跳过：未设置 LINGXI_POSTGRES_DSN")
class OverlapRealDbTestCase(PendingActionPostgresTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._tmp = tempfile.TemporaryDirectory()
        cls.metric_map_path = pathlib.Path(cls._tmp.name) / "map.toml"
        cls.metric_map_path.write_text(METRIC_MAP_TOML, encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()
        super().tearDownClass()

    def setUp(self) -> None:
        super().setUp()
        self.store = PostgresPendingActionStore(
            self._dsn,
            audit=self.audit,
            metric_map_path=self.metric_map_path,
            durable_followups=True,
        )
        self.add_target_user(account_state="enabled")
        self.execute(
            "UPDATE app_user SET employee_no = %s, email = %s WHERE feishu_open_id = %s",
            (EMPLOYEE_NO, EMAIL, TARGET_OPEN_ID),
        )

    # ---- 夹具 --------------------------------------------------------

    def user_id(self) -> str:
        return self.query("SELECT id FROM app_user WHERE feishu_open_id = %s", (TARGET_OPEN_ID,))[
            0
        ][0]

    def grant(self, position_name: str, company_scope: str, *, reason: str = "特批"):
        return self.store.prepare(
            action_type=PendingActionType.LOCAL_PERMISSION_GRANT,
            target_open_id=TARGET_OPEN_ID,
            initiated_by_open_id=ADMIN_OPEN_ID,
            reason=reason,
            position_name=position_name,
            company_scope=company_scope,
        )

    def confirm(self, pending_id: str):
        self.store.mark_card_delivered(pending_action_id=pending_id, card_id=f"card_{pending_id}")
        result = self.store.confirm(pending_action_id=pending_id, clicker_open_id=ADMIN_OPEN_ID)
        self.assertTrue(result.decision.ok, result.decision.message)
        return result.pending

    def grant_and_confirm(self, position_name: str, company_scope: str) -> dict:
        outcome = self.grant(position_name, company_scope)
        self.assertTrue(outcome.decision.ok, outcome.decision.message)
        self.confirm(outcome.pending.id)
        return json.loads(outcome.pending.payload)

    def revoke(self, target: str):
        outcome = self.store.prepare(
            action_type=PendingActionType.LOCAL_PERMISSION_REVOKE,
            target_open_id=target,
            initiated_by_open_id=ADMIN_OPEN_ID,
            reason="撤销测试",
        )
        self.assertTrue(outcome.decision.ok, outcome.decision.message)
        return outcome.pending

    def active_rows(self) -> set[tuple[str, str, str | None]]:
        return {
            (company, metric, group)
            for company, metric, group in self.query(
                "SELECT company_id, metric_name, permission_group_id"
                "  FROM local_permission_override WHERE user_id = %s AND entry_status = 'active'",
                (self.user_id(),),
            )
        }

    def seed_galaxy(self, role_name: str = "A运营") -> None:
        from lingxi.adapters.galaxy_import import PostgresGalaxyImportStore

        rows = [
            RosterRow(
                personnel_id=f"fs_{TARGET_OPEN_ID}",
                email=EMAIL,
                name=NAME,
                employee_no=EMPLOYEE_NO,
                record_id="rec_1",
            )
        ]
        PostgresRosterSnapshotStore(self._dsn).replace(
            rows,
            SimpleNamespace(
                pages_read=1,
                reported_total=1,
                total_matches_rows=True,
                rows_without_personnel_id=0,
                blank_column_rows=(),
                duplicates=(),
            ),
            captured_at=NOW,
        )
        result = PostgresGalaxyImportStore(self._dsn).import_export(
            source_label="合成导出（测试）",
            source_digest=f"digest-{role_name}",
            tables=_galaxy_tables(role_name),
        )
        self.assertEqual(result.outcome, "imported")


class DifferentialGrantRealDbTests(OverlapRealDbTestCase):
    def test_fresh_scope_registers_everything_without_a_reuse_note(self) -> None:
        payload = self.grant_and_confirm("A销售", COMPANY_A)
        self.assertNotIn("reused_count", payload)
        self.assertEqual({row[:2] for row in self.active_rows()}, {(COMPANY_A, "m_a")})

    def test_partial_overlap_only_registers_the_missing_pairs(self) -> None:
        """变异锚点：把差集判定改回「任一对已存在即整笔拒绝」，本用例由绿转红。"""

        first = self.grant_and_confirm("A销售", COMPANY_A)
        first_group = first["permission_group_id"]

        outcome = self.grant("A运营", COMPANY_A)
        self.assertTrue(outcome.decision.ok, outcome.decision.message)
        payload = json.loads(outcome.pending.payload)
        self.assertEqual(payload["pairs"], [[COMPANY_A, "m_b"], [COMPANY_A, "m_c"]])
        self.assertEqual(payload["reused_count"], 1)
        card = render_confirm_card(outcome.pending, target_label="化名用户")
        self.assertIn("本次新增 2 项、已有 1 项沿用", card.body)

        self.confirm(outcome.pending.id)
        second_group = payload["permission_group_id"]
        self.assertEqual(
            self.active_rows(),
            {
                (COMPANY_A, "m_a", first_group),
                (COMPANY_A, "m_b", second_group),
                (COMPANY_A, "m_c", second_group),
            },
        )

    def test_fully_held_scope_is_rejected_with_the_count_and_writes_nothing(self) -> None:
        self.grant_and_confirm("A销售", COMPANY_A)
        self.grant_and_confirm("A运营", COMPANY_A)
        before = self.query("SELECT count(*) FROM pending_action")[0][0]

        outcome = self.grant("A运营", COMPANY_A, reason="再补一次")

        self.assertFalse(outcome.decision.ok)
        self.assertEqual(outcome.decision.code, "target_state_changed")
        self.assertEqual(outcome.decision.message, "所选职位范围的 3 项均已登记，无需补授。")
        self.assertEqual(self.query("SELECT count(*) FROM pending_action")[0][0], before)
        self.assertEqual(len(self.active_rows()), 3)


class RevokeOnlyThisBatchRealDbTests(OverlapRealDbTestCase):
    def test_revoking_the_differential_group_keeps_the_earlier_group_active(self) -> None:
        """变异锚点：把整组撤销改成按目标用户收回全部生效行，本用例由绿转红。"""

        first = self.grant_and_confirm("A销售", COMPANY_A)
        second = self.grant_and_confirm("A运营", COMPANY_A)

        pending = self.revoke(second["permission_group_id"])
        executed = self.confirm(pending.id)

        self.assertEqual(executed.status, PendingActionStatus.EXECUTED)
        self.assertEqual(self.active_rows(), {(COMPANY_A, "m_a", first["permission_group_id"])})
        revoked = self.query(
            "SELECT permission_group_id FROM local_permission_override"
            " WHERE revoked_pending_action_id = %s",
            (pending.id,),
        )
        self.assertEqual({row[0] for row in revoked}, {second["permission_group_id"]})

        followups = PostgresFollowupStore(self._dsn)
        stages = {ref.stage for ref in followups.list_for_action(pending_action_id=pending.id)}
        self.assertIn("permission_recompute", stages)

        # 重算阶段在真库里接上发布观察：绑定当前版本的发布记录后，处理器登记
        # ``publish_observe`` 作为后继（重算与重推多维表格两步都在）。
        from lingxi.apps.gateway.admin_followups import GatewayFollowupHandlers

        self.execute(
            "UPDATE app_user SET permission_version = 1 WHERE feishu_open_id = %s",
            (TARGET_OPEN_ID,),
        )
        self.execute(
            "INSERT INTO publish_outbox(id,user_id,permission_version,reason,payload,status)"
            " VALUES ('pub_overlap', %s, 1, 'synthetic', '{}', 'pending')",
            (self.user_id(),),
        )
        handlers = GatewayFollowupHandlers(
            store=followups,
            pending_actions=self.store,
            callback=Mock(),
            recompute=Mock(
                trigger=Mock(return_value=TargetedRecomputeOutcome(kind=RecomputeKind.UNCHANGED))
            ),
            cards=Mock(),
        )
        # 前两笔授权的确认也各登记了一条重算阶段；按登记顺序领取，跳过不属于本笔的。
        while True:
            item = followups.claim_followup(
                consumer_kind="recompute",
                owner="test",
                now=datetime.now(UTC) + timedelta(seconds=1),
            )
            self.assertIsNotNone(item)
            if item.pending_action_id == pending.id:
                break
            followups.complete_followup(
                id=item.id, owner="test", attempt=item.attempt, status="skipped", result_code="x"
            )
        result = handlers._recompute(item, executed)
        followups.complete_followup(
            id=item.id,
            owner="test",
            attempt=item.attempt,
            status="succeeded",
            result_code="queued",
            external_ref=result.external_ref,
            next_items=result.next_items,
        )
        stages = {ref.stage for ref in followups.list_for_action(pending_action_id=pending.id)}
        self.assertIn("publish_observe", stages)


class GalaxyRetentionRealDbTests(OverlapRealDbTestCase):
    def test_unreadable_galaxy_source_is_persisted_as_unknown_not_zero(self) -> None:
        """变异锚点：银河读不到时把 K 记成 0，本用例由绿转红。"""

        group = self.grant_and_confirm("A销售", COMPANY_A)["permission_group_id"]
        pending = self.revoke(group)
        payload = json.loads(pending.payload)
        self.assertIn("galaxy_retained", payload)
        self.assertIsNone(payload["galaxy_retained"])
        executed = self.confirm(pending.id)
        self.assertIn("银河来源暂不可读", _outcome_text(executed))
        self.assertNotIn("0 项", _outcome_text(executed))
        self.assertIn("银河来源暂不可读", render_group_notice(executed, target_label="化名用户"))

    def test_pairs_covered_by_galaxy_are_counted(self) -> None:
        self.seed_galaxy("A运营")
        group = self.grant_and_confirm("A运营", COMPANY_A)["permission_group_id"]
        pending = self.revoke(group)
        self.assertEqual(json.loads(pending.payload)["galaxy_retained"], 3)
        executed = self.confirm(pending.id)
        self.assertIn("该用户经银河来源仍持有其中 3 项", _outcome_text(executed))
        self.assertIn(
            "该用户经银河来源仍持有其中 3 项",
            render_group_notice(executed, target_label="化名用户"),
        )

    def test_single_legacy_row_revoke_counts_that_one_pair(self) -> None:
        self.seed_galaxy("A运营")
        anchor = self.query("SELECT id FROM pending_action WHERE status = 'executed'")
        if not anchor:
            self.grant_and_confirm("A销售", COMPANY_B)
            anchor = self.query("SELECT id FROM pending_action WHERE status = 'executed'")
        override_id = new_id("lpo")
        self.execute(
            "INSERT INTO local_permission_override"
            " (id, user_id, direction, company_id, metric_name, reason, initiated_by_open_id,"
            "  pending_action_id, entry_status, created_at)"
            " VALUES (%s, %s, 'grant', %s, 'm_a', '历史行', %s, %s, 'active', now())",
            (override_id, self.user_id(), COMPANY_A, ADMIN_OPEN_ID, anchor[0][0]),
        )
        pending = self.revoke(override_id)
        self.assertEqual(json.loads(pending.payload)["galaxy_retained"], 1)

    def test_pairs_outside_the_galaxy_scope_count_zero(self) -> None:
        self.seed_galaxy("A运营")
        group = self.grant_and_confirm("A销售", COMPANY_B)["permission_group_id"]
        pending = self.revoke(group)
        self.assertEqual(json.loads(pending.payload)["galaxy_retained"], 0)
        self.assertIn("仍持有其中 0 项", _outcome_text(self.confirm(pending.id)))

    def test_full_access_wildcard_role_retains_every_pair(self) -> None:
        self.seed_galaxy("后台管理员")
        group = self.grant_and_confirm("A运营", COMPANY_B)["permission_group_id"]
        pending = self.revoke(group)
        self.assertEqual(json.loads(pending.payload)["galaxy_retained"], 3)


if __name__ == "__main__":
    unittest.main()
