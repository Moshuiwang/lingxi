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

import os
import unittest
from datetime import datetime, timezone

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.adapters.admin_registry import (
    PostgresAdminQueries,
    PostgresAdminRegistryLookup,
    seed_admin_registry_entry,
)
from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_local_permission import PostgresLocalPermissionOverrideStore
from lingxi.adapters.postgres_pending_action import PostgresPendingActionStore
from lingxi.core.admin.card_callback import AdminCardCallbackHandler
from lingxi.core.admin.card_dispatch import ConfirmCardDispatcher
from lingxi.core.admin.router import AdminCommandRouter
from lingxi.core.ids import new_id
from lingxi.core.permission.local_override import OverrideDirection

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
