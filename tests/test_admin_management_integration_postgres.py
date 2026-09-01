"""用户权限管理卡（#439 B 档）表单提交与确认回调层的真库集成测试（需要真实
PostgreSQL 16）。

自证闭环条款要求"表单提交与确认回调层真库集成测试覆盖"：本文件把
``core/admin/card_callback.AdminCardCallbackHandler`` 新增的
``handle_management_form_submit``/``handle_management_revoke`` 两个方法，接上
**真实** ``AdminCommandRouter``（真实 ``PostgresAdminRegistryLookup``/
``PostgresAdminQueries``/``PostgresPendingActionStore``），验证一次表单提交/
按钮点击真的会在真实 Postgres 里写下一条 ``pending_action`` 行——只有卡片发送
本身（``ConfirmCardSender``）按仓库红线（v13 §6.4 第 7 条）注入假实现，不真实
发送任何飞书卡片。

不重复覆盖 ``core/admin/router.py``/``adapters/postgres_pending_action.py`` 各自
已经拥有的真库断言（见 ``tests/test_admin_registry_postgres.py``/
``tests/test_pending_action_postgres.py``）——本文件只钉住"两个新增方法确实把
管理卡的交互正确转译成了一条命令文本并送进了既有链路"这一件事。
"""

from __future__ import annotations

import json
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.adapters.admin_registry import (
    PostgresAdminQueries,
    PostgresAdminRegistryLookup,
    seed_admin_registry_entry,
)
from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_local_permission import PostgresLocalPermissionOverrideStore
from lingxi.adapters.postgres_management_card_context import PostgresManagementCardContextStore
from lingxi.adapters.postgres_pending_action import PostgresPendingActionStore
from lingxi.core.admin.card_callback import AdminCardCallbackHandler
from lingxi.core.admin.card_dispatch import ConfirmCardDispatcher
from lingxi.core.admin.router import AdminCommandRouter
from lingxi.core.ids import new_id
from lingxi.core.permission.local_override import OverrideDirection
from lingxi.core.admin.views import AdminUserStatusView

DSN = os.environ.get("LINGXI_POSTGRES_DSN")
SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，管理卡表单/回调的真库集成断言未验证"
    if not DSN
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，未验证"
)

ADMIN_OPEN_ID = "ou_management_integration_admin"
TARGET_OPEN_ID = "ou_management_integration_target"


class _FakeConfirmCardTransport:
    """``AdminCardTransport`` 的假实现——红线（v13 §6.4 第 7 条）：真库集成测试
    仍然不得真实发送任何飞书卡片，卡片发送本身已由
    ``tests/test_admin_card_dispatch.py``/``tests/test_feishu_admin_card_
    transport.py`` 独立覆盖。"""

    def __init__(self) -> None:
        self.create_calls: list[dict] = []
        self._counter = 0

    def create(self, *, chat_id, thread_id, reply_to_message_id, card):
        self._counter += 1
        self.create_calls.append(
            {"chat_id": chat_id, "reply_to_message_id": reply_to_message_id, "card": card}
        )
        return type("Created", (), {"card_id": f"card_{self._counter}", "message_id": f"msg_{self._counter}"})()

    def update(self, *, card_id, sequence, card):  # pragma: no cover - 本文件不测终态更新
        raise NotImplementedError


class _RecordingAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, dict(fields)))


class _NoGroupNotifier:
    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None:  # pragma: no cover
        raise AssertionError("本文件的用例不应该触发群通知")


@unittest.skipUnless(DSN and psycopg_available(), SKIP_REASON)
class ManagementCardCallbackIntegrationTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._dsn = DSN
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        reset_production_rows(self._dsn)
        seed_admin_registry_entry(self._dsn, feishu_open_id=ADMIN_OPEN_ID, label="test-admin")
        self.execute(
            """INSERT INTO app_user
                 (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name,
                  department, tenant_key, provisioning_state, account_state)
               VALUES (%s, %s, %s, %s, '化名用户', '测试部门', 'tk_test', 'active', 'enabled')""",
            (new_id("usr"), TARGET_OPEN_ID, f"fs_{TARGET_OPEN_ID}", f"un_{TARGET_OPEN_ID}"),
        )

        confirm_audit = _RecordingAudit()
        self.pending_store = PostgresPendingActionStore(self._dsn, audit=confirm_audit)
        # 同一个 PostgresAdminQueries 实例结构性实现 AdminDisplayNames（Trace
        # #469 S-1），与真实 apps/gateway/__init__.py 装配同一姿态——不需要
        # 额外声明或继承，注入到下面三处需要它的构造点。
        self.display_names = PostgresAdminQueries(self._dsn)
        self.confirm_transport = _FakeConfirmCardTransport()
        confirm_dispatcher = ConfirmCardDispatcher(
            transport=self.confirm_transport,
            tracker=self.pending_store,
            audit=confirm_audit,
            display_names=self.display_names,
        )
        self.router_audit = _RecordingAudit()
        self.router = AdminCommandRouter(
            registry=PostgresAdminRegistryLookup(self._dsn),
            queries=PostgresAdminQueries(self._dsn),
            audit=self.router_audit,
            display_names=self.display_names,
            pending_actions=self.pending_store,
            confirm_cards=confirm_dispatcher,
        )
        self.callback_audit = _RecordingAudit()
        self.handler = AdminCardCallbackHandler(
            pending_actions=self.pending_store,
            confirm_cards=_FakeConfirmCardTransport(),
            group_notifier=_NoGroupNotifier(),
            group_chat_id=None,
            audit=self.callback_audit,
            display_names=self.display_names,
            management_actions=self.router,
        )

    def query(self, sql: str, parameters: tuple = ()) -> list[tuple]:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(sql, parameters)
            return cursor.fetchall()

    def execute(self, sql: str, parameters: tuple = ()) -> None:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(sql, parameters)

    def pending_actions_for_target(self, *, action_type: str) -> list[tuple]:
        return self.query(
            "SELECT id, status, payload, reason FROM pending_action"
            " WHERE target_open_id = %s AND action_type = %s",
            (TARGET_OPEN_ID, action_type),
        )


class FormSubmitCreatesARealPendingActionTests(ManagementCardCallbackIntegrationTestCase):
    def test_grant_form_submit_writes_a_real_pending_action_row_and_sends_a_confirm_card(
        self,
    ) -> None:
        response = self.handler.handle_management_form_submit(
            operator_open_id=ADMIN_OPEN_ID,
            admin_action="grant",
            identifier=TARGET_OPEN_ID,
            company_id="1011",
            metric_name="daily_active",
            reason="特批授权",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
            trace_id="trc_1",
        )

        self.assertEqual(response["toast"]["type"], "success")
        rows = self.pending_actions_for_target(action_type="local_permission_grant")
        self.assertEqual(len(rows), 1)
        _, status, payload, reason = rows[0]
        self.assertEqual(status, "pending")
        self.assertIn("1011", payload)
        self.assertIn("daily_active", payload)
        # 确认卡片确实通过既有 ConfirmCardDispatcher 发送了一次（真实
        # pending_action.card_delivered 应为真，能被后续 confirm() 使用）。
        self.assertEqual(len(self.confirm_transport.create_calls), 1)
        # Trace #469 S-1 TOP-1（L1 真库证据）：确认卡「目标：」字段显示真实
        # app_user.display_name（setUp 里写的"化名用户"），不是 TARGET_OPEN_ID
        # 这个 open_id 字面量——真实 SQL 查询、真实 CHECK 约束下的一整条链路。
        sent_card = self.confirm_transport.create_calls[0]["card"]
        self.assertIn("化名用户", sent_card.body)
        self.assertNotIn(TARGET_OPEN_ID, sent_card.body)
        # 公司编号在没有任何银河批次时按设计原样展示（未导入过银河数据，
        # company_label 的既有降级行为，见 core/admin/display_names 模块文档）。
        self.assertIn("公司 1011", sent_card.body)

    def test_suppress_form_submit_writes_the_suppress_action_type(self) -> None:
        self.handler.handle_management_form_submit(
            operator_open_id=ADMIN_OPEN_ID,
            admin_action="suppress",
            identifier=TARGET_OPEN_ID,
            company_id="1011",
            metric_name="daily_active",
            reason="临时抑制",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
            trace_id="trc_1",
        )

        rows = self.pending_actions_for_target(action_type="local_permission_suppress")
        self.assertEqual(len(rows), 1)

    def test_unauthorized_operator_writes_nothing(self) -> None:
        """否定断言：不是登记表里的管理员——真实 ``route()`` 内部重新判定身份，
        不产生任何待确认操作。"""

        response = self.handler.handle_management_form_submit(
            operator_open_id="ou_never_registered",
            admin_action="grant",
            identifier=TARGET_OPEN_ID,
            company_id="1011",
            metric_name="daily_active",
            reason="特批授权",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
            trace_id="trc_1",
        )

        self.assertEqual(response["toast"]["type"], "error")
        self.assertEqual(self.pending_actions_for_target(action_type="local_permission_grant"), [])

    def test_missing_reason_is_rejected_before_touching_the_router(self) -> None:
        response = self.handler.handle_management_form_submit(
            operator_open_id=ADMIN_OPEN_ID,
            admin_action="grant",
            identifier=TARGET_OPEN_ID,
            company_id="1011",
            metric_name="daily_active",
            reason="   ",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
            trace_id="trc_1",
        )

        self.assertEqual(response["toast"]["type"], "error")
        self.assertEqual(self.pending_actions_for_target(action_type="local_permission_grant"), [])
        self.assertEqual(self.router_audit.records, [])

    def test_empty_company_or_metric_is_rejected_before_touching_the_router(self) -> None:
        response = self.handler.handle_management_form_submit(
            operator_open_id=ADMIN_OPEN_ID,
            admin_action="grant",
            identifier=TARGET_OPEN_ID,
            company_id="",
            metric_name="daily_active",
            reason="特批",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
            trace_id="trc_1",
        )

        self.assertEqual(response["toast"]["type"], "error")
        self.assertEqual(self.router_audit.records, [])


class RevokeButtonClickCreatesARealPendingActionTests(ManagementCardCallbackIntegrationTestCase):
    def _insert_active_override(self) -> str:
        user_row = self.query(
            "SELECT id FROM app_user WHERE feishu_open_id = %s", (TARGET_OPEN_ID,)
        )
        user_id = user_row[0][0]
        grant_pending_id = new_id("pac")
        now = datetime.now(timezone.utc)
        self.execute(
            """INSERT INTO pending_action
                   (id, action_type, target_open_id, target_state_snapshot,
                    initiated_by_open_id, status, confirm_deadline_at,
                    decided_at, decided_by_open_id)
                 VALUES (%s, 'suspend_user', %s, 'enabled', %s, 'executed', %s, %s, %s)""",
            (
                grant_pending_id,
                "ou_bystander_for_" + grant_pending_id,
                ADMIN_OPEN_ID,
                now,
                now,
                ADMIN_OPEN_ID,
            ),
        )
        store = PostgresLocalPermissionOverrideStore(self._dsn)
        override = store.insert(
            user_id=user_id,
            direction=OverrideDirection.GRANT,
            company_id="1011",
            metric_name="daily_active",
            reason="特批",
            initiated_by_open_id=ADMIN_OPEN_ID,
            pending_action_id=grant_pending_id,
        )
        return override.id

    def test_revoke_click_writes_a_real_pending_action_row_with_the_default_reason(self) -> None:
        override_id = self._insert_active_override()

        response = self.handler.handle_management_revoke(
            operator_open_id=ADMIN_OPEN_ID,
            override_id=override_id,
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
            trace_id="trc_1",
        )

        self.assertEqual(response["toast"]["type"], "success")
        rows = self.pending_actions_for_target(action_type="local_permission_revoke")
        self.assertEqual(len(rows), 1)
        _, status, payload, reason = rows[0]
        self.assertEqual(status, "pending")
        # 业务原因存在 payload JSON 里（与 grant/suppress 同一存储位置，见
        # adapters/postgres_pending_action.py 模块文档），顶层 reason 列只用于
        # 终态原因码（"expired"/"role_revoked" 等），prepare() 阶段恒为
        # None——不是本次改动引入的行为，是既有存储约定。
        self.assertIsNone(reason)
        self.assertIn("管理卡逐行撤销", payload)

    def test_unauthorized_operator_writes_nothing(self) -> None:
        override_id = self._insert_active_override()

        response = self.handler.handle_management_revoke(
            operator_open_id="ou_never_registered",
            override_id=override_id,
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
            trace_id="trc_1",
        )

        self.assertEqual(response["toast"]["type"], "error")
        self.assertEqual(self.pending_actions_for_target(action_type="local_permission_revoke"), [])

    def test_unknown_override_id_is_rejected_without_crashing(self) -> None:
        response = self.handler.handle_management_revoke(
            operator_open_id=ADMIN_OPEN_ID,
            override_id="lpo_01JGFJJZ008XSHEADGG8V74SPC",
            chat_id="oc_1",
            thread_id=None,
            message_id="om_1",
            trace_id="trc_1",
        )

        self.assertEqual(response["toast"]["type"], "error")


class PositionPermissionGroupRealDbTests(ManagementCardCallbackIntegrationTestCase):
    """#493 P1：真实配置的职位+范围展开、组撤销和别组隔离。"""

    SECOND_TARGET_OPEN_ID = "ou_management_integration_target_2"

    def setUp(self) -> None:
        super().setUp()
        self.execute(
            """INSERT INTO app_user
                 (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name,
                  department, tenant_key, provisioning_state, account_state)
               VALUES (%s, %s, %s, %s, '化名用户二', '测试部门', 'tk_test', 'active', 'enabled')""",
            (
                new_id("usr"),
                self.SECOND_TARGET_OPEN_ID,
                f"fs_{self.SECOND_TARGET_OPEN_ID}",
                f"un_{self.SECOND_TARGET_OPEN_ID}",
            ),
        )

    def _ensure_management_context(self, message_id: str, identifier: str) -> None:
        """职位表单的反向 FK 与生产管理卡发送侧登记保持同一前置。"""

        now = datetime.now(timezone.utc) + timedelta(hours=1)
        self.execute(
            """INSERT INTO management_card_context
                 (message_id, card_id, identifier, chat_id, initiated_by_open_id,
                  snapshot_fingerprint, context_deadline_at)
               VALUES (%s, %s, %s, 'oc_1', %s, 'fp', %s)""",
            (message_id, f"card_{message_id}", identifier, ADMIN_OPEN_ID, now),
        )

    def _submit_and_confirm_position_group(self, target_open_id: str, message_id: str) -> str:
        self._ensure_management_context(message_id, target_open_id)
        response = self.handler.handle_management_form_submit(
            operator_open_id=ADMIN_OPEN_ID,
            admin_action="grant",
            identifier=target_open_id,
            company_id="",
            metric_name="",
            reason="真实配置组测试",
            chat_id="oc_1",
            thread_id=None,
            message_id=message_id,
            trace_id=f"trc_{message_id}",
            position_name="A运营",
            company_scope="*",
        )
        self.assertEqual(response["toast"]["type"], "success")
        pending_rows = self.query(
            "SELECT id, payload FROM pending_action"
            " WHERE target_open_id = %s AND action_type = 'local_permission_grant'"
            " ORDER BY created_at DESC",
            (target_open_id,),
        )
        self.assertEqual(len(pending_rows), 1)
        pending_id, payload = pending_rows[0]
        payload_data = json.loads(payload)
        self.assertEqual(payload_data["permission_group_id"].startswith("lpg_"), True)
        self.assertEqual(len(payload_data["pairs"]), 387)
        outcome = self.pending_store.confirm(
            pending_action_id=pending_id,
            clicker_open_id=ADMIN_OPEN_ID,
        )
        self.assertEqual(outcome.decision.kind.value, "execute")
        return payload_data["permission_group_id"]

    def test_real_387_row_group_is_one_revoke_and_other_group_survives(self) -> None:
        first_group = self._submit_and_confirm_position_group(TARGET_OPEN_ID, "om_group_1")
        second_group = self._submit_and_confirm_position_group(
            self.SECOND_TARGET_OPEN_ID, "om_group_2"
        )

        first_counts = self.query(
            "SELECT count(*), count(DISTINCT permission_group_id)"
            " FROM local_permission_override"
            " WHERE permission_group_id = %s AND entry_status = 'active'",
            (first_group,),
        )
        self.assertEqual(first_counts, [(387, 1)])
        second_counts = self.query(
            "SELECT count(*) FROM local_permission_override"
            " WHERE permission_group_id = %s AND entry_status = 'active'",
            (second_group,),
        )
        self.assertEqual(second_counts, [(387,)])

        response = self.handler.handle_management_revoke(
            operator_open_id=ADMIN_OPEN_ID,
            override_id="",
            permission_group_id=first_group,
            chat_id="oc_1",
            thread_id=None,
            message_id="om_group_1",
            trace_id="trc_group_revoke",
        )
        self.assertEqual(response["toast"]["type"], "success")
        revoke_rows = self.query(
            "SELECT id, payload FROM pending_action"
            " WHERE target_open_id = %s AND action_type = 'local_permission_revoke'"
            " ORDER BY created_at DESC",
            (TARGET_OPEN_ID,),
        )
        self.assertEqual(len(revoke_rows), 1)
        revoke_id, revoke_payload = revoke_rows[0]
        revoke_data = json.loads(revoke_payload)
        self.assertEqual(revoke_data["permission_group_id"], first_group)
        self.assertEqual(len(revoke_data["override_ids"]), 387)
        outcome = self.pending_store.confirm(
            pending_action_id=revoke_id,
            clicker_open_id=ADMIN_OPEN_ID,
        )
        self.assertEqual(outcome.decision.kind.value, "execute")

        revoked_counts = self.query(
            "SELECT count(*) FILTER (WHERE entry_status = 'active'),"
            "       count(*) FILTER (WHERE entry_status = 'revoked'),"
            "       count(*)"
            "  FROM local_permission_override"
            " WHERE permission_group_id = %s",
            (first_group,),
        )
        self.assertEqual(revoked_counts, [(0, 387, 387)])
        # 另一用户的同形职位组不应被第一组的事务性撤销误伤。
        self.assertEqual(
            self.query(
                "SELECT count(*) FROM local_permission_override"
                " WHERE permission_group_id = %s AND entry_status = 'active'",
                (second_group,),
            ),
            [(387,)],
        )


class ManagementCorrectionRealDbTests(ManagementCardCallbackIntegrationTestCase):
    """#493 P1：只有真实 daily publish 才能产生每日纠偏群摘要水位。"""

    def _seed_context_and_executed_action(self, message_id: str) -> PostgresManagementCardContextStore:
        now = datetime.now(timezone.utc)
        context_store = PostgresManagementCardContextStore(self._dsn)
        context_store.remember(
            message_id=message_id,
            identifier=TARGET_OPEN_ID,
            card_id=f"card_{message_id}",
            chat_id="oc_1",
            initiated_by_open_id=ADMIN_OPEN_ID,
            snapshot_fingerprint="fp",
            context_deadline_at=now + timedelta(hours=1),
        )
        self.execute(
            """INSERT INTO pending_action
                 (id, action_type, target_open_id, target_state_snapshot,
                  initiated_by_open_id, status, card_delivered, card_id,
                  payload, origin_card_message_id, confirm_deadline_at,
                  decided_at, decided_by_open_id)
               VALUES (%s, 'local_permission_grant', %s, 'absent', %s,
                       'executed', TRUE, %s, %s, %s, %s, %s, %s)""",
            (
                new_id("pac"),
                TARGET_OPEN_ID,
                ADMIN_OPEN_ID,
                f"card_confirm_{message_id}",
                json.dumps({"company_id": "1011", "metric_name": "daily_active", "reason": "test"}),
                message_id,
                now + timedelta(minutes=10),
                now,
                ADMIN_OPEN_ID,
            ),
        )
        return context_store

    def _publish_for_context(
        self,
        *,
        reason: str,
        permission_version: int = 1,
    ) -> None:
        now = datetime.now(timezone.utc)
        user_id = self.query(
            "SELECT id FROM app_user WHERE feishu_open_id = %s", (TARGET_OPEN_ID,)
        )[0][0]
        payload = json.dumps(
            {
                "record_key": "target@example.com",
                "email": "target@example.com",
                "name": "化名用户",
                "permissions": "{\"1011\":[\"daily_active\"]}",
                "status": "approved",
                "updated_at": now.isoformat(),
            }
        )
        self.execute(
            """INSERT INTO publish_outbox
                 (id, user_id, permission_version, reason, payload, status,
                  created_at, published_at, content_expires_at)
               VALUES (%s, %s, %s, %s, %s, 'published', %s, %s, %s)""",
            (
                new_id("pub"),
                user_id,
                permission_version,
                reason,
                payload,
                now,
                now + timedelta(seconds=1),
                now + timedelta(days=90),
            ),
        )

    def test_late_instant_publish_does_not_create_daily_correction_watermark(self) -> None:
        message_id = "om_correction_instant"
        context_store = self._seed_context_and_executed_action(message_id)
        context_store.update_state(
            message_id=message_id, state="incomplete", dispatch_status="incomplete"
        )
        self._publish_for_context(
            reason="admin_action_instant_recompute",
        )

        self.assertEqual(context_store.settle_published_contexts(), ())
        context = context_store.lookup_context(message_id=message_id)
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context.state, "effective")
        self.assertFalse(context.daily_correction_pending)
        self.assertEqual(context_store.unreported_daily_correction_ids(), ())

    def test_daily_publish_qualifies_once_and_late_instant_cannot_clear_it(self) -> None:
        message_id = "om_correction_daily"
        context_store = self._seed_context_and_executed_action(message_id)
        context_store.update_state(
            message_id=message_id, state="incomplete", dispatch_status="incomplete"
        )
        self._publish_for_context(
            reason="daily_permission_refresh",
        )

        self.assertEqual(context_store.settle_published_contexts(), (message_id,))
        context = context_store.lookup_context(message_id=message_id)
        self.assertIsNotNone(context)
        assert context is not None
        self.assertTrue(context.daily_correction_pending)
        self.assertIsNone(context.daily_correction_reported_at)
        self.assertTrue(context.needs_refresh)
        self.assertEqual(context_store.unreported_daily_correction_ids(), (message_id,))

        # 重启/迟到的 instant observer 不能把已经由 daily batch 证明的事实清掉。
        context_store.update_state(
            message_id=message_id, state="effective", dispatch_status="effective"
        )
        self.assertEqual(context_store.unreported_daily_correction_ids(), (message_id,))
        context_store.mark_daily_corrections_reported(message_ids=(message_id,))
        context_store.mark_daily_corrections_reported(message_ids=(message_id,))
        self.assertEqual(context_store.unreported_daily_correction_ids(), ())


class ManagementCardStateCasRealDbTests(ManagementCardCallbackIntegrationTestCase):
    """#493 P1：两个独立连接上的 scanner/writer 必须按状态代数 CAS。"""

    def _seed_refreshable_context(self, message_id: str) -> PostgresManagementCardContextStore:
        store = PostgresManagementCardContextStore(self._dsn)
        store.remember(
            message_id=message_id,
            identifier=TARGET_OPEN_ID,
            card_id=f"card_{message_id}",
            chat_id="oc_1",
            initiated_by_open_id=ADMIN_OPEN_ID,
            snapshot_fingerprint="fp",
            context_deadline_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        store.update_state(
            message_id=message_id, state="effective", dispatch_status="effective"
        )
        return store

    def test_concurrent_state_write_rejects_stale_sequence_claim_without_consuming_it(self) -> None:
        scanner_store = self._seed_refreshable_context("om_state_cas_claim")
        writer_store = PostgresManagementCardContextStore(self._dsn)
        snapshot = scanner_store.lookup_context(message_id="om_state_cas_claim")
        self.assertIsNotNone(snapshot)
        assert snapshot is not None

        both_ready = threading.Barrier(2)
        writer_done = threading.Event()

        def write_new_state():
            both_ready.wait(timeout=5)
            updated = writer_store.update_state(
                message_id="om_state_cas_claim",
                state="incomplete",
                dispatch_status="incomplete",
            )
            writer_done.set()
            return updated

        def claim_old_snapshot():
            both_ready.wait(timeout=5)
            self.assertTrue(writer_done.wait(timeout=5))
            return scanner_store.next_card_sequence(
                message_id="om_state_cas_claim",
                expected_state_version=snapshot.state_version,
                expected_card_sequence=snapshot.card_sequence,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            writer_future = pool.submit(write_new_state)
            scanner_future = pool.submit(claim_old_snapshot)
            self.assertIsNotNone(writer_future.result(timeout=10))
            self.assertIsNone(scanner_future.result(timeout=10))

        current = scanner_store.lookup_context(message_id="om_state_cas_claim")
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.state, "incomplete")
        self.assertEqual(current.state_version, snapshot.state_version + 1)
        self.assertEqual(current.card_sequence, snapshot.card_sequence + 1)
        self.assertTrue(current.needs_refresh)

    def test_concurrent_state_write_rejects_late_watermark_clear(self) -> None:
        scanner_store = self._seed_refreshable_context("om_state_cas_mark")
        writer_store = PostgresManagementCardContextStore(self._dsn)
        snapshot = scanner_store.lookup_context(message_id="om_state_cas_mark")
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        claimed = scanner_store.next_card_sequence(
            message_id="om_state_cas_mark",
            expected_state_version=snapshot.state_version,
            expected_card_sequence=snapshot.card_sequence,
        )
        self.assertEqual(claimed, snapshot.card_sequence + 1)
        assert claimed is not None

        writer_done = threading.Event()

        def write_new_state():
            updated = writer_store.update_state(
                message_id="om_state_cas_mark",
                state="incomplete",
                dispatch_status="incomplete",
            )
            writer_done.set()
            return updated

        def mark_old_visual():
            self.assertTrue(writer_done.wait(timeout=5))
            return scanner_store.mark_visual_refreshed(
                message_id="om_state_cas_mark",
                sequence=claimed,
                expected_state_version=snapshot.state_version,
                expected_card_sequence=claimed,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            writer_future = pool.submit(write_new_state)
            mark_future = pool.submit(mark_old_visual)
            self.assertIsNotNone(writer_future.result(timeout=10))
            self.assertFalse(mark_future.result(timeout=10))

        current = scanner_store.lookup_context(message_id="om_state_cas_mark")
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.state, "incomplete")
        self.assertEqual(current.card_sequence, claimed + 1)
        self.assertEqual(current.visual_sequence, snapshot.visual_sequence)
        self.assertTrue(current.needs_refresh)

    def test_gateway_refresher_uses_real_context_port_and_clears_only_after_update(self) -> None:
        from lingxi.apps.gateway import _GatewayManagementCardRefresher

        class _Catalog:
            def companies(self):
                return ["1011"]

            def metrics(self):
                return ["daily_active"]

            def positions(self):
                return ["A运营"]

        class _DisplayNames:
            def company_labels(self, *, company_ids):
                return {company_id: company_id for company_id in company_ids}

            def metric_labels(self, *, metric_ids):
                return {metric_id: metric_id for metric_id in metric_ids}

        class _Transport:
            def __init__(self) -> None:
                self.updates: list[dict] = []

            def update(self, **kwargs) -> None:
                self.updates.append(kwargs)

        store = self._seed_refreshable_context("om_state_cas_port")
        context = store.lookup_context(message_id="om_state_cas_port")
        self.assertIsNotNone(context)
        assert context is not None
        status = AdminUserStatusView(
            identifier=TARGET_OPEN_ID,
            provisioning_state="active",
            account_state="enabled",
            permission_version=1,
            updated_at="2026-09-01T00:00:00+00:00",
        )
        transport = _Transport()
        refresher = _GatewayManagementCardRefresher(
            transport=transport,
            catalog=_Catalog(),
            display_names=_DisplayNames(),
            context_store=store,
        )

        self.assertTrue(
            refresher.update(
                context=context,
                status=status,
                state=context.state,
                dispatch_status=context.dispatch_status,
            )
        )
        self.assertEqual(len(transport.updates), 1)
        self.assertEqual(transport.updates[0]["card_id"], context.card_id)
        self.assertEqual(transport.updates[0]["sequence"], context.card_sequence + 1)
        current = store.lookup_context(message_id="om_state_cas_port")
        self.assertIsNotNone(current)
        assert current is not None
        self.assertFalse(current.needs_refresh)
        self.assertEqual(current.visual_sequence, transport.updates[0]["sequence"])

if __name__ == "__main__":  # pragma: no cover
    unittest.main()
