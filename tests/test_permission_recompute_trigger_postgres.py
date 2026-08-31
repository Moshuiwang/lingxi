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

from lingxi.adapters.admin_registry import PostgresAdminQueries, seed_admin_registry_entry
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
            # PostgresAdminQueries 结构性实现 AdminDisplayNames（Trace #469
            # S-1），与真实 apps/gateway/__init__.py 装配同一姿态。
            display_names=PostgresAdminQueries(self._dsn),
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
            require_enabled_account=True,
            user_id=user_id,
            row=row,
            reason="test_seed",
            decided_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
        self.assertTrue(decision.enqueued)

    def prepare_and_deliver(
        self,
        *,
        action_type: PendingActionType,
        company_id: str | None = None,
        metric_name: str | None = None,
        reason: str | None = None,
    ) -> str:
        outcome = self.pending_actions.prepare(
            action_type=action_type,
            target_open_id=TARGET_OPEN_ID,
            initiated_by_open_id=ADMIN_OPEN_ID,
            company_id=company_id,
            metric_name=metric_name,
            reason=reason,
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

        # 回调应答本身：确认成功。Trace #469 S-1 TOP-3 接线修复后，toast 用这次
        # 点击的 decide_confirm 友好消息（"已确认执行。"），"即时生效"这句持久化
        # 措辞改为在终态卡片正文里核对（见 core/admin/card_callback.py
        # handle() 的对应注释）。
        self.assertEqual(outcome["toast"]["type"], "success")
        self.assertEqual(outcome["toast"]["content"], "已确认执行。")
        self.assertIn(
            "操作已记录，权限正在下发",
            outcome["card"]["data"]["body"]["elements"][0]["content"],
        )

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


class LocalPermissionGrantResolvesTheOwningUserIdTests(PermissionRecomputeTriggerPostgresTestCase):
    """``LOCAL_PERMISSION_GRANT`` confirm 全链真库断言（Issue #438 补卡）。

    本地权限三类动作里，只有 ``LOCAL_PERMISSION_REVOKE`` 复用 ``target_open_id``
    形参承载 override_id（见 ``adapters/postgres_pending_action.py`` 模块文档
    「本地权限收回如何复用同一套机制」）；``LOCAL_PERMISSION_GRANT``/``SUPPRESS``
    的 ``target_open_id`` 全程是真实飞书 open_id，`confirm()` 的 EXECUTE 分支才
    在 ``local_permission_override`` 表新插入一行，把这次确认卡自己的
    ``pending_action.id`` 写进新行的 ``pending_action_id`` 列（同一文件「本地权限
    授权/抑制如何复用同一套机制」）。

    ``adapters/postgres_targeted_recompute_lookup.py::resolve_local_override_target``
    因此按 ``pending_action_id = pending.id`` 反查出这一行的 ``user_id``（真正的
    ``app_user.id``），不依赖、也不解析 ``target_open_id``——这条反查链路此前只有
    假 store 的单测覆盖，本用例走真实 PostgreSQL 全链（``prepare()`` → 真实
    ``confirm()`` 事务 → 真实 INSERT → 真实反查 → ``TargetedPermissionRecompute``），
    坐实反查结果确实是这一行的 ``user_id``，不是 ``override_id`` 本身、也不是
    ``pending_action_id`` 本身（三者是三个不同前缀的 ULID，历史上一次反查逻辑
    退化成"直接把某个标识透传当 user_id 用"的编码错误，字符串层面的相等断言
    足以拦住）。

    与 ``ResumeDegradesToDailyBatchWhenGalaxyDataIsMissingTests`` 同一降级前提：
    这个真库环境没有铺花名册快照，`recompute_and_publish` 结构上必然停在
    ``missing_roster_snapshot`` 跳过分支（`TargetedPermissionRecompute.
    recompute_and_publish` 判据顺序：花名册基线 → 花名册快照 → 银河批次，本用例
    只保证前两步的判据都成立、真正到达第二步）——本用例的关键断言不是"重算产出
    了新发布行"（这条已经由 SUSPEND 用例覆盖过"重算真的发生"的同族证据），而是
    "跳过审计记的 user 字段是正确的内部用户 id"。
    """

    def test_confirmed_grant_resolves_the_owning_user_id_not_the_override_id(self) -> None:
        user_id = self.add_target_user(account_state="enabled")

        pending_id = self.prepare_and_deliver(
            action_type=PendingActionType.LOCAL_PERMISSION_GRANT,
            company_id="1011",
            metric_name="daily_active",
            reason="真库全链用例特批",
        )

        outcome = self.handler.handle(
            operator_open_id=ADMIN_OPEN_ID,
            pending_action_id=pending_id,
            decision=DECISION_CONFIRM,
            trace_id="trc_grant_1",
        )

        # 确认执行本身必须成功。Trace #469 S-1 TOP-3 接线修复后，toast 用这次
        # 点击的 decide_confirm 友好消息，"即时生效"这句持久化措辞改为在终态
        # 卡片正文里核对（同上一处注释）。
        self.assertEqual(outcome["toast"]["type"], "success")
        self.assertEqual(outcome["toast"]["content"], "已确认执行。")
        self.assertIn(
            "操作已记录，权限正在下发",
            outcome["card"]["data"]["body"]["elements"][0]["content"],
        )

        # 真实反查依赖的形状：confirm() 新插入的这一行，pending_action_id 恰好
        # 是这次确认卡自己的 id，user_id 是目标用户的内部标识，两者与 override_id
        # 本身互不相同（三个不同前缀的 ULID：lpo_/pac_/usr_）。
        rows = self.query(
            "SELECT id, user_id, pending_action_id FROM local_permission_override"
            " WHERE pending_action_id = %s",
            (pending_id,),
        )
        self.assertEqual(len(rows), 1, "确认执行应当恰好新建一条本地授权覆盖行")
        override_id, override_user_id, override_pending_action_id = rows[0]
        self.assertEqual(override_user_id, user_id)
        self.assertEqual(override_pending_action_id, pending_id)
        self.assertNotEqual(override_id, user_id)

        # 关键断言（Issue #438 补卡）：定向重算的跳过审计里，user 字段是反查出的
        # 真正 app_user.id，不是 override_id、也不是触发本次回调的 pending_id。
        fields = self.audit.fields_for("permission_targeted_recompute.skipped")
        self.assertEqual(fields["mode"], "recompute")
        self.assertEqual(fields["reason"], "missing_roster_snapshot")
        self.assertEqual(fields["user"], user_id)
        self.assertNotEqual(fields["user"], override_id)
        self.assertNotEqual(fields["user"], pending_id)

        self.assertNotIn(
            "permission_targeted_recompute.target_unresolved", self.audit.actions()
        )
        self.assertNotIn(
            "admin.card_callback.recompute_trigger_failed", self.audit.actions()
        )
