"""定向单用户权限重算/发布的真库全链断言（Issue #438，需要真实 PostgreSQL 16）。

覆盖「回调 → 定向重算 → 发布行更新」全链（自证闭环条款硬要求）：本文件直接构造
真实的 :class:`~lingxi.core.admin.card_callback.AdminCardCallbackHandler`，装配
真实的 :class:`~lingxi.adapters.postgres_pending_action.PostgresPendingActionStore`
与真实的 :class:`~lingxi.adapters.postgres_permission_recompute_trigger.
PermissionRecomputeAdapter`，通过 ``handler.handle()`` 这一个公开入口驱动，断言
数据库里 ``publish_outbox`` 的实际发布行——不在任何一层打桩绕过真实事务。

真实链路里「点击确认卡」一步（飞书 ``card.action.trigger`` 回调）不可受控注入
（见 Issue #438 comment「自证闭环条款」），因此本文件用直接调用
``handler.handle(...)`` 模拟这一步——它与真实回调处理的唯一差别是"谁触发了这次
调用"，``handle()`` 内部到 ``PostgresPendingActionStore.confirm()`` 事务、到
``PermissionRecomputeAdapter.trigger()`` 的调用全部是真实代码路径，未打桩。
"""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timezone

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.adapters.admin_registry import seed_admin_registry_entry
from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_pending_action import PostgresPendingActionStore
from lingxi.adapters.postgres_permission_publish import PostgresPermissionPublishStore
from lingxi.adapters.postgres_permission_recompute_trigger import PermissionRecomputeAdapter
from lingxi.core.admin.card_callback import AdminCardCallbackHandler, DECISION_CONFIRM
from lingxi.core.admin.pending_action import PendingActionType
from lingxi.core.ids import new_id
from lingxi.core.permission.publish_row import build_translated_publish_row

DSN = os.environ.get("LINGXI_POSTGRES_DSN")
SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，定向权限重算的真库断言未验证（需真实 PostgreSQL 16）"
    if not DSN
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，定向权限重算的真库断言未验证"
)

ADMIN_OPEN_ID = "ou_recompute_admin"
TARGET_OPEN_ID = "ou_recompute_target"
TARGET_EMAIL = "recompute.target@example.invalid"

#: biai-agent 加密规格 v1 的**公开测试向量**（非生产密钥、非生产令牌），与
#: ``tests/test_permission_publish_postgres.py`` 同一份，仅用于满足"新建发布行
#: 必须带 token_cipher"这条形状校验，不解密、不作为真实凭据使用。
TOKEN_CIPHER = "RklYRURJVjEyMzQ1Njc4OX5gpf2vKqJiLgzu2n4kug1V1rz6DDt1OCgAZVpg1pL+"


class _RecordingAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, dict(fields)))

    def actions(self) -> list[str]:
        return [action for action, _ in self.records]

    def fields_for(self, action: str) -> dict:
        for recorded_action, fields in self.records:
            if recorded_action == action:
                return fields
        raise AssertionError(f"审计里没有 {action} 这条记录：{self.actions()}")


class _NoopCardTransport:
    """终态卡出带外更新的空实现——本文件断言的是发布行，不是卡片渲染。"""

    def update(self, *, card_id: str, sequence: int, card: object) -> None:
        return None


@unittest.skipUnless(DSN and psycopg_available(), SKIP_REASON)
class PermissionRecomputeTriggerPostgresTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._dsn = DSN
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        reset_production_rows(self._dsn)
        self.audit = _RecordingAudit()
        self.pending_actions = PostgresPendingActionStore(self._dsn, audit=self.audit)
        self.publish_store = PostgresPermissionPublishStore(self._dsn)
        seed_admin_registry_entry(self._dsn, feishu_open_id=ADMIN_OPEN_ID, label="test-admin")
        self.handler = AdminCardCallbackHandler(
            pending_actions=self.pending_actions,
            confirm_cards=_NoopCardTransport(),
            group_notifier=None,
            group_chat_id=None,
            audit=self.audit,
            recompute_trigger=PermissionRecomputeAdapter(self._dsn, audit=self.audit),
        )

    def query(self, sql: str, parameters: tuple = ()) -> list[tuple]:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(sql, parameters)
            return cursor.fetchall()

    def execute(self, sql: str, parameters: tuple = ()) -> None:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(sql, parameters)

    def add_target_user(self, *, account_state: str = "enabled") -> str:
        user_id = new_id("usr")
        self.execute(
            """INSERT INTO app_user
                 (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name,
                  department, tenant_key, provisioning_state, account_state, email)
               VALUES (%s, %s, %s, %s, '化名用户', '测试部门', 'tk_test', 'active', %s, %s)""",
            (user_id, TARGET_OPEN_ID, f"fs_{TARGET_OPEN_ID}", f"un_{TARGET_OPEN_ID}", account_state, TARGET_EMAIL),
        )
        return user_id

    def seed_granted_publish_row(self, user_id: str) -> None:
        """给目标用户先落一条"已发布授权"的发布行——``force_revoke`` 的前置
        （只对发布链上留过足迹的人发撤权）。"""

        row = build_translated_publish_row(
            company_metrics={"1011": ("daily_active",)},
            email=TARGET_EMAIL,
            display_name="化名用户",
            decided_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            token_cipher=TOKEN_CIPHER,
        )
        decision = self.publish_store.record_decision(
            user_id=user_id,
            row=row,
            reason="test_seed",
            decided_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
        self.assertTrue(decision.enqueued)

    def prepare_and_deliver(self, *, action_type: PendingActionType) -> str:
        outcome = self.pending_actions.prepare(
            action_type=action_type,
            target_open_id=TARGET_OPEN_ID,
            initiated_by_open_id=ADMIN_OPEN_ID,
        )
        self.assertTrue(outcome.decision.ok, outcome.decision.message)
        assert outcome.pending is not None
        self.pending_actions.mark_card_delivered(
            pending_action_id=outcome.pending.id, card_id="cardkit_test"
        )
        return outcome.pending.id

    def latest_publish_row(self, user_id: str) -> tuple[int, dict]:
        rows = self.query(
            "SELECT permission_version, payload FROM publish_outbox"
            " WHERE user_id = %s ORDER BY permission_version DESC LIMIT 1",
            (user_id,),
        )
        self.assertEqual(len(rows), 1, "目标用户应当恰好有一条最新发布行")
        version, payload = rows[0]
        return int(version), dict(payload)

    def publish_row_count(self, user_id: str) -> int:
        return self.query(
            "SELECT count(*) FROM publish_outbox WHERE user_id = %s", (user_id,)
        )[0][0]


class SuspendTriggersInstantRevokeTests(PermissionRecomputeTriggerPostgresTestCase):
    """回调 → 定向重算（撤权）→ 发布行更新，全链真库断言（自证闭环条款硬要求）。"""

    def test_confirmed_suspend_clears_the_published_permissions_row(self) -> None:
        user_id = self.add_target_user(account_state="enabled")
        self.seed_granted_publish_row(user_id)
        before_version, before_payload = self.latest_publish_row(user_id)
        # ``payload.permissions`` 本身是一段预先序列化好的 JSON 文本（``PublishRow.
        # permissions: str``），嵌进外层 JSONB 时不会被数据库再解析一层——回读到的
        # 是字符串，不是嵌套对象，见 core/permission/publish_row.py 模块文档。
        self.assertEqual(json.loads(before_payload["permissions"]), {"1011": ["daily_active"]})

        pending_id = self.prepare_and_deliver(action_type=PendingActionType.SUSPEND_USER)
        outcome = self.handler.handle(
            operator_open_id=ADMIN_OPEN_ID,
            pending_action_id=pending_id,
            decision=DECISION_CONFIRM,
            trace_id="trc_suspend_1",
        )

        # 回调应答本身：确认成功，回执带"即时生效"信息（Issue #438 回执文案要求）。
        self.assertEqual(outcome["toast"]["type"], "success")
        self.assertIn("即时生效", outcome["toast"]["content"])

        # 账号状态：既有行为不变（本卡不改动这一层）。
        account_state = self.query(
            "SELECT account_state FROM app_user WHERE id = %s", (user_id,)
        )[0][0]
        self.assertEqual(account_state, "suspended")

        # 发布行：新的一版已经写入，permissions 清空为 {}——定向重算真的发生了，
        # 不是等下一次每日批。
        after_version, after_payload = self.latest_publish_row(user_id)
        self.assertGreater(after_version, before_version)
        self.assertEqual(json.loads(after_payload["permissions"]), {})
        self.assertEqual(after_payload["email"], TARGET_EMAIL)

        # 审计：定向重算完成、归类为撤权（kind=revoked）。
        fields = self.audit.fields_for("permission_targeted_recompute.completed")
        self.assertEqual(fields["mode"], "revoke")
        self.assertEqual(fields["kind"], "revoked")
        self.assertEqual(fields["user"], user_id)

    def test_suspending_a_user_with_no_publish_footprint_skips_with_a_clear_reason(self) -> None:
        """通配/无发布足迹等跳过场景：审计明确说明跳过原因（Issue #438 要求）。"""

        self.add_target_user(account_state="enabled")
        pending_id = self.prepare_and_deliver(action_type=PendingActionType.SUSPEND_USER)

        outcome = self.handler.handle(
            operator_open_id=ADMIN_OPEN_ID,
            pending_action_id=pending_id,
            decision=DECISION_CONFIRM,
            trace_id="trc_suspend_2",
        )

        self.assertEqual(outcome["toast"]["type"], "success")
        fields = self.audit.fields_for("permission_targeted_recompute.skipped")
        self.assertEqual(fields["mode"], "revoke")
        self.assertEqual(fields["reason"], "no_published_row")


class ResumeDegradesToDailyBatchWhenGalaxyDataIsMissingTests(
    PermissionRecomputeTriggerPostgresTestCase
):
    """恢复（走完整合并管线）在花名册/银河快照尚未配置的当前部署下的真实降级
    行为——确认操作本身不受影响，定向重算清楚地跳过并留痕，与每日批遇到同一
    前置缺口时的既有姿态一致（`permission_refresh.py` 的 `SKIP_MISSING_
    ROSTER_SNAPSHOT`同族原因码）。
    """

    def test_confirmed_resume_does_not_fail_even_though_recompute_is_skipped(self) -> None:
        self.add_target_user(account_state="suspended")
        pending_id = self.prepare_and_deliver(action_type=PendingActionType.RESUME_USER)

        outcome = self.handler.handle(
            operator_open_id=ADMIN_OPEN_ID,
            pending_action_id=pending_id,
            decision=DECISION_CONFIRM,
            trace_id="trc_resume_1",
        )

        # 确认操作本身必须成功——降级不得让已成功的确认操作本身报错（硬纪律）。
        self.assertEqual(outcome["toast"]["type"], "success")
        account_state = self.query(
            "SELECT account_state FROM app_user WHERE feishu_open_id = %s", (TARGET_OPEN_ID,)
        )[0][0]
        self.assertEqual(account_state, "enabled")

        fields = self.audit.fields_for("permission_targeted_recompute.skipped")
        self.assertEqual(fields["mode"], "recompute")
        self.assertEqual(fields["reason"], "missing_roster_snapshot")
        self.assertNotIn(
            "admin.card_callback.recompute_trigger_failed", self.audit.actions()
        )
