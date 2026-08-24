"""``pending_action``（迁移 ``0068``）的真库断言（Issue #96 S-M-02，需要真实
PostgreSQL 16）。

条件更新与事务性质必须在真库上验证，不用 mock（验证与门禁第五节）。覆盖：

- ``prepare()`` 只在目标状态允许时才插入一行；
- ``confirm()`` 的完整核对链在真实事务里生效：非本人点击零业务变更、重复点击/
  真实并发只成功一次、过期后确认不执行、prepare 与 confirm 之间角色被撤销时拒绝、
  目标状态漂移时拒绝、审计写入失败时整个事务回滚（``pending_action``/``app_user``
  均不改变）、伪造/未送达的待确认操作 ID 安全拒绝；
- ``cancel()`` 的对称路径；
- 执行成功后 ``app_user.account_state`` 确实按预期翻转（suspend → 'suspended'，
  resume → 'enabled'）——这是"停用要真的挡住业务使用"断言链的数据库侧证据，实际
  挡住效果由既有 ``postgres_conversation._transaction._user_state`` 消费（未变）。
"""

from __future__ import annotations

import os
import threading
import unittest
from datetime import datetime, timedelta, timezone

from postgres_schema import ensure_production_schema, reset_production_rows

from lingxi.adapters.admin_registry import PostgresAdminRegistryLookup, seed_admin_registry_entry
from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_pending_action import (
    PendingActionAuditWriteFailed,
    PostgresPendingActionStore,
)
from lingxi.core.admin.pending_action import PendingActionStatus, PendingActionType
from lingxi.core.ids import new_id

DSN = os.environ.get("LINGXI_POSTGRES_DSN")
SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，待确认操作的真库断言未验证（需真实 PostgreSQL 16）"
)

ADMIN_OPEN_ID = "ou_pending_action_admin"
TARGET_OPEN_ID = "ou_pending_action_target"


class _RecordingAudit:
    def __init__(self, *, raise_error: bool = False) -> None:
        self.records: list[tuple[str, dict]] = []
        self.raise_error = raise_error

    def record(self, action: str, /, **fields: object) -> None:
        if self.raise_error:
            raise RuntimeError("模拟审计落库失败")
        self.records.append((action, dict(fields)))


@unittest.skipUnless(DSN, SKIP_REASON)
class PendingActionPostgresTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._dsn = DSN
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        reset_production_rows(self._dsn)
        self.audit = _RecordingAudit()
        self.registry_lookup = PostgresAdminRegistryLookup(self._dsn)
        self.store = PostgresPendingActionStore(
            self._dsn, registry=self.registry_lookup, audit=self.audit
        )
        seed_admin_registry_entry(self._dsn, feishu_open_id=ADMIN_OPEN_ID, label="test-admin")

    def query(self, sql: str, parameters: tuple = ()) -> list[tuple]:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(sql, parameters)
            return cursor.fetchall()

    def execute(self, sql: str, parameters: tuple = ()) -> None:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(sql, parameters)

    def add_target_user(
        self, *, open_id: str = TARGET_OPEN_ID, account_state: str = "enabled"
    ) -> None:
        self.execute(
            """INSERT INTO app_user
                 (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name,
                  department, tenant_key, provisioning_state, account_state)
               VALUES (%s, %s, %s, %s, '化名用户', '测试部门', 'tk_test', 'active', %s)""",
            (new_id("usr"), open_id, f"fs_{open_id}", f"un_{open_id}", account_state),
        )

    def current_account_state(self, open_id: str = TARGET_OPEN_ID) -> str:
        rows = self.query(
            "SELECT account_state FROM app_user WHERE feishu_open_id = %s", (open_id,)
        )
        self.assertEqual(len(rows), 1)
        return rows[0][0]

    def prepare_and_deliver(
        self,
        *,
        action_type: PendingActionType = PendingActionType.SUSPEND_USER,
        target_open_id: str = TARGET_OPEN_ID,
        initiated_by_open_id: str = ADMIN_OPEN_ID,
    ) -> str:
        """建一条待确认操作并标记卡片已送达——测试套件里绝大多数用例的共同前置。"""

        outcome = self.store.prepare(
            action_type=action_type,
            target_open_id=target_open_id,
            initiated_by_open_id=initiated_by_open_id,
        )
        self.assertTrue(outcome.decision.ok, outcome.decision.message)
        assert outcome.pending is not None
        self.store.mark_card_delivered(
            pending_action_id=outcome.pending.id, card_id="cardkit_test"
        )
        return outcome.pending.id


class PrepareTests(PendingActionPostgresTestCase):
    def test_prepare_inserts_a_pending_row_with_the_current_state_snapshot(self) -> None:
        self.add_target_user(account_state="enabled")

        outcome = self.store.prepare(
            action_type=PendingActionType.SUSPEND_USER,
            target_open_id=TARGET_OPEN_ID,
            initiated_by_open_id=ADMIN_OPEN_ID,
        )

        self.assertTrue(outcome.decision.ok)
        assert outcome.pending is not None
        self.assertEqual(outcome.pending.status, PendingActionStatus.PENDING)
        self.assertEqual(outcome.pending.target_state_snapshot, "enabled")
        self.assertFalse(outcome.pending.card_delivered)
        rows = self.query("SELECT count(*) FROM pending_action")
        self.assertEqual(rows[0][0], 1)

    def test_prepare_rejects_unknown_target_without_inserting_any_row(self) -> None:
        outcome = self.store.prepare(
            action_type=PendingActionType.SUSPEND_USER,
            target_open_id="ou_does_not_exist",
            initiated_by_open_id=ADMIN_OPEN_ID,
        )

        self.assertFalse(outcome.decision.ok)
        self.assertEqual(self.query("SELECT count(*) FROM pending_action")[0][0], 0)

    def test_prepare_rejects_suspend_on_an_already_suspended_user(self) -> None:
        self.add_target_user(account_state="suspended")

        outcome = self.store.prepare(
            action_type=PendingActionType.SUSPEND_USER,
            target_open_id=TARGET_OPEN_ID,
            initiated_by_open_id=ADMIN_OPEN_ID,
        )

        self.assertFalse(outcome.decision.ok)
        self.assertEqual(self.query("SELECT count(*) FROM pending_action")[0][0], 0)


class ConfirmHappyPathTests(PendingActionPostgresTestCase):
    def test_suspend_confirm_flips_account_state_and_marks_executed(self) -> None:
        self.add_target_user(account_state="enabled")
        pending_id = self.prepare_and_deliver(action_type=PendingActionType.SUSPEND_USER)

        result = self.store.confirm(pending_action_id=pending_id, clicker_open_id=ADMIN_OPEN_ID)

        self.assertTrue(result.decision.ok)
        self.assertEqual(self.current_account_state(), "suspended")
        assert result.pending is not None
        self.assertEqual(result.pending.status, PendingActionStatus.EXECUTED)
        self.assertIn("admin.pending_action.confirmed", [action for action, _ in self.audit.records])

    def test_resume_confirm_flips_account_state_back_to_enabled(self) -> None:
        self.add_target_user(account_state="suspended")
        pending_id = self.prepare_and_deliver(action_type=PendingActionType.RESUME_USER)

        result = self.store.confirm(pending_action_id=pending_id, clicker_open_id=ADMIN_OPEN_ID)

        self.assertTrue(result.decision.ok)
        self.assertEqual(self.current_account_state(), "enabled")


class NotInitiatorRealDbTests(PendingActionPostgresTestCase):
    """否定断言：非本人 open_id 点击 → 拒绝且**真库层面**零业务变更。"""

    def test_wrong_clicker_leaves_app_user_and_pending_action_untouched(self) -> None:
        self.add_target_user(account_state="enabled")
        pending_id = self.prepare_and_deliver()

        result = self.store.confirm(
            pending_action_id=pending_id, clicker_open_id="ou_impersonator"
        )

        self.assertFalse(result.decision.ok)
        self.assertEqual(self.current_account_state(), "enabled", "零业务变更")
        assert result.pending is not None
        self.assertEqual(
            result.pending.status, PendingActionStatus.PENDING, "非本人点击不改变状态，真正的发起人随后仍可点对"
        )

        # 真正的发起人随后确实还能点对。
        second = self.store.confirm(pending_action_id=pending_id, clicker_open_id=ADMIN_OPEN_ID)
        self.assertTrue(second.decision.ok)
        self.assertEqual(self.current_account_state(), "suspended")


class DuplicateAndConcurrentConfirmTests(PendingActionPostgresTestCase):
    """否定断言：重复点击/重复回调/重试 → 只执行一次，含真库并发用例。"""

    def test_sequential_duplicate_confirm_only_executes_once(self) -> None:
        self.add_target_user(account_state="enabled")
        pending_id = self.prepare_and_deliver()

        first = self.store.confirm(pending_action_id=pending_id, clicker_open_id=ADMIN_OPEN_ID)
        second = self.store.confirm(pending_action_id=pending_id, clicker_open_id=ADMIN_OPEN_ID)

        self.assertTrue(first.decision.ok)
        self.assertFalse(second.decision.ok)
        self.assertEqual(self.current_account_state(), "suspended")
        # 第二次点击是幂等重放：不产生新的状态转移，store 层不写第二条
        # admin.pending_action.confirmed（那一步没有任何新信息可记——store 只做了
        # 一次 SELECT 就返回既有结果）。"每次操作都记录"由更上一层的
        # core/admin/card_callback.AdminCardCallbackHandler 对**每一次点击**无条件
        # 记 admin.card_callback.handled 落实（见 tests/test_admin_card_callback.py），
        # 不要求 store 内部为纯粹的只读幂等重放重复写审计。
        confirmed_outcomes = [
            fields["outcome"]
            for action, fields in self.audit.records
            if action == "admin.pending_action.confirmed"
        ]
        self.assertEqual(confirmed_outcomes, ["execute"])

    def test_two_real_concurrent_confirms_only_one_succeeds(self) -> None:
        """真实并发用例：两个线程各自开自己的数据库连接，几乎同时对同一条待确认
        操作发起确认——``SELECT ... FOR UPDATE`` 必须把它们序列化，结果是恰好
        一次执行、恰好一次幂等拒绝，``app_user.account_state`` 只翻转一次。
        """

        self.add_target_user(account_state="enabled")
        pending_id = self.prepare_and_deliver()

        barrier = threading.Barrier(2)
        results: list[object] = [None, None]
        errors: list[BaseException] = []

        def confirm_from_thread(index: int) -> None:
            try:
                barrier.wait(timeout=5)
                # 每个线程用自己的 store（各自独立的审计记录器，避免共享可变状态
                # 本身成为竞态源；数据库层面的序列化才是本用例真正要证明的东西）。
                thread_audit = _RecordingAudit()
                thread_store = PostgresPendingActionStore(
                    self._dsn, registry=self.registry_lookup, audit=thread_audit
                )
                results[index] = thread_store.confirm(
                    pending_action_id=pending_id, clicker_open_id=ADMIN_OPEN_ID
                )
            except BaseException as error:  # noqa: BLE001 - 收集到主线程再断言
                errors.append(error)

        threads = [threading.Thread(target=confirm_from_thread, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [], f"并发确认不应抛出未预期的异常：{errors}")
        outcomes = [result.decision.ok for result in results]
        self.assertEqual(
            sorted(outcomes), [False, True], "恰好一个线程执行成功，另一个必须被拒绝"
        )
        self.assertEqual(self.current_account_state(), "suspended", "只翻转一次，不是两次或零次")
        executed_rows = self.query(
            "SELECT count(*) FROM pending_action WHERE id = %s AND status = 'executed'",
            (pending_id,),
        )
        self.assertEqual(executed_rows[0][0], 1)


class ExpiryRealDbTests(PendingActionPostgresTestCase):
    """否定断言：过期后确认 → 不执行。"""

    def test_expired_pending_action_is_rejected_and_leaves_app_user_untouched(self) -> None:
        self.add_target_user(account_state="enabled")
        pending_id = self.prepare_and_deliver()
        self.execute(
            "UPDATE pending_action SET expires_at = now() - interval '1 second' WHERE id = %s",
            (pending_id,),
        )

        result = self.store.confirm(pending_action_id=pending_id, clicker_open_id=ADMIN_OPEN_ID)

        self.assertFalse(result.decision.ok)
        self.assertEqual(self.current_account_state(), "enabled")
        assert result.pending is not None
        self.assertEqual(result.pending.status, PendingActionStatus.EXPIRED)


class RoleRevokedRealDbTests(PendingActionPostgresTestCase):
    """否定断言：prepare 与 confirm 之间角色被撤销 → 拒绝，要求重新查询发起。"""

    def test_revoking_the_admin_between_prepare_and_confirm_blocks_execution(self) -> None:
        self.add_target_user(account_state="enabled")
        pending_id = self.prepare_and_deliver()

        self.execute(
            "UPDATE admin_registry SET entry_status = 'revoked', revoked_at = now()"
            " WHERE feishu_open_id = %s",
            (ADMIN_OPEN_ID,),
        )

        result = self.store.confirm(pending_action_id=pending_id, clicker_open_id=ADMIN_OPEN_ID)

        self.assertFalse(result.decision.ok)
        self.assertEqual(self.current_account_state(), "enabled")
        assert result.pending is not None
        self.assertEqual(result.pending.status, PendingActionStatus.FAILED)
        self.assertEqual(result.pending.reason, "role_revoked")


class TargetDriftRealDbTests(PendingActionPostgresTestCase):
    """否定断言：目标状态漂移（确认前用户已被另一路径改变）→ 拒绝并提示重新发起。"""

    def test_target_suspended_by_another_path_before_confirm_blocks_execution(self) -> None:
        self.add_target_user(account_state="enabled")
        pending_id = self.prepare_and_deliver(action_type=PendingActionType.SUSPEND_USER)

        # 模拟"另一条路径"在确认前改变了目标状态（真实场景例如未来的删除编排，
        # 本测试直接用 SQL 模拟，不依赖那条编排是否已实现）。
        self.execute(
            "UPDATE app_user SET account_state = 'suspended' WHERE feishu_open_id = %s",
            (TARGET_OPEN_ID,),
        )

        result = self.store.confirm(pending_action_id=pending_id, clicker_open_id=ADMIN_OPEN_ID)

        self.assertFalse(result.decision.ok)
        assert result.pending is not None
        self.assertEqual(result.pending.status, PendingActionStatus.FAILED)
        self.assertEqual(result.pending.reason, "target_drifted")
        # 漂移路径写下的状态不被 confirm() 进一步改动。
        self.assertEqual(self.current_account_state(), "suspended")


class AuditWriteFailureRealDbTests(PendingActionPostgresTestCase):
    """否定断言：审计 sink 异常 → 不执行（失败关闭），且是真实事务回滚——不是
    "函数抛了异常"这个表面现象，而是 ``pending_action``/``app_user`` 在数据库里
    确实都没有发生任何变化。"""

    def test_audit_failure_rolls_back_both_pending_action_and_app_user(self) -> None:
        self.add_target_user(account_state="enabled")
        pending_id = self.prepare_and_deliver()
        failing_audit = _RecordingAudit(raise_error=True)
        failing_store = PostgresPendingActionStore(
            self._dsn, registry=self.registry_lookup, audit=failing_audit
        )

        with self.assertRaises(PendingActionAuditWriteFailed):
            failing_store.confirm(pending_action_id=pending_id, clicker_open_id=ADMIN_OPEN_ID)

        self.assertEqual(self.current_account_state(), "enabled", "审计失败后目标账号必须保持不变")
        status_rows = self.query(
            "SELECT status FROM pending_action WHERE id = %s", (pending_id,)
        )
        self.assertEqual(status_rows[0][0], "pending", "待确认操作必须仍停在 pending，可以重试")

        # 换回正常审计器后重试确实能成功——「调用方可重试」不是一句空话。
        retry = self.store.confirm(pending_action_id=pending_id, clicker_open_id=ADMIN_OPEN_ID)
        self.assertTrue(retry.decision.ok)
        self.assertEqual(self.current_account_state(), "suspended")

    def test_audit_failure_on_cancel_also_rolls_back(self) -> None:
        self.add_target_user(account_state="enabled")
        pending_id = self.prepare_and_deliver()
        failing_store = PostgresPendingActionStore(
            self._dsn, registry=self.registry_lookup, audit=_RecordingAudit(raise_error=True)
        )

        with self.assertRaises(PendingActionAuditWriteFailed):
            failing_store.cancel(pending_action_id=pending_id, clicker_open_id=ADMIN_OPEN_ID)

        status_rows = self.query(
            "SELECT status FROM pending_action WHERE id = %s", (pending_id,)
        )
        self.assertEqual(status_rows[0][0], "pending")


class ForgedOrUndeliveredActionRealDbTests(PendingActionPostgresTestCase):
    """否定断言：卡片回调伪造（不存在的动作 ID / 篡改目标）→ 拒绝。"""

    def test_confirming_a_forged_id_that_never_existed_is_handled_safely(self) -> None:
        result = self.store.confirm(
            pending_action_id="pac_never_existed_00000000", clicker_open_id=ADMIN_OPEN_ID
        )

        self.assertFalse(result.decision.ok)
        self.assertIsNone(result.pending)

    def test_undelivered_action_cannot_be_confirmed_even_by_the_real_initiator(self) -> None:
        self.add_target_user(account_state="enabled")
        outcome = self.store.prepare(
            action_type=PendingActionType.SUSPEND_USER,
            target_open_id=TARGET_OPEN_ID,
            initiated_by_open_id=ADMIN_OPEN_ID,
        )
        assert outcome.pending is not None
        # 刻意不调用 mark_card_delivered：模拟卡片发送失败/结果不明。

        result = self.store.confirm(
            pending_action_id=outcome.pending.id, clicker_open_id=ADMIN_OPEN_ID
        )

        self.assertFalse(result.decision.ok)
        self.assertEqual(self.current_account_state(), "enabled")


class CancelRealDbTests(PendingActionPostgresTestCase):
    def test_cancel_marks_cancelled_and_never_touches_app_user(self) -> None:
        self.add_target_user(account_state="enabled")
        pending_id = self.prepare_and_deliver()

        result = self.store.cancel(pending_action_id=pending_id, clicker_open_id=ADMIN_OPEN_ID)

        self.assertTrue(result.decision.ok)
        assert result.pending is not None
        self.assertEqual(result.pending.status, PendingActionStatus.CANCELLED)
        self.assertEqual(self.current_account_state(), "enabled")

    def test_cancelled_action_cannot_later_be_confirmed(self) -> None:
        self.add_target_user(account_state="enabled")
        pending_id = self.prepare_and_deliver()
        self.store.cancel(pending_action_id=pending_id, clicker_open_id=ADMIN_OPEN_ID)

        result = self.store.confirm(pending_action_id=pending_id, clicker_open_id=ADMIN_OPEN_ID)

        self.assertFalse(result.decision.ok)
        self.assertEqual(self.current_account_state(), "enabled")


class MarkCardDeliveredAndSendFailedTests(PendingActionPostgresTestCase):
    def test_mark_card_delivered_persists_card_id(self) -> None:
        self.add_target_user(account_state="enabled")
        outcome = self.store.prepare(
            action_type=PendingActionType.SUSPEND_USER,
            target_open_id=TARGET_OPEN_ID,
            initiated_by_open_id=ADMIN_OPEN_ID,
        )
        assert outcome.pending is not None

        self.store.mark_card_delivered(
            pending_action_id=outcome.pending.id, card_id="cardkit_abc"
        )

        rows = self.query(
            "SELECT card_delivered, card_id FROM pending_action WHERE id = %s",
            (outcome.pending.id,),
        )
        self.assertEqual(rows[0], (True, "cardkit_abc"))

    def test_mark_send_failed_voids_the_action(self) -> None:
        self.add_target_user(account_state="enabled")
        outcome = self.store.prepare(
            action_type=PendingActionType.SUSPEND_USER,
            target_open_id=TARGET_OPEN_ID,
            initiated_by_open_id=ADMIN_OPEN_ID,
        )
        assert outcome.pending is not None

        self.store.mark_send_failed(pending_action_id=outcome.pending.id)

        rows = self.query(
            "SELECT status, card_delivered, reason FROM pending_action WHERE id = %s",
            (outcome.pending.id,),
        )
        self.assertEqual(rows[0], ("failed", False, "card_send_failed"))

        # 一旦作废，即使发起人本人点击确认也不会执行。
        result = self.store.confirm(
            pending_action_id=outcome.pending.id, clicker_open_id=ADMIN_OPEN_ID
        )
        self.assertFalse(result.decision.ok)
        self.assertEqual(self.current_account_state(), "enabled")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
