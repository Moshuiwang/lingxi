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

import json
import os
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.adapters.admin_registry import seed_admin_registry_entry
from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_local_permission import PostgresLocalPermissionOverrideStore
from lingxi.adapters.postgres_pending_action import (
    TARGET_HAS_PENDING_ACTION_CODE,
    PendingActionAuditWriteFailed,
    PostgresPendingActionStore,
)
from lingxi.core.admin.pending_action import (
    ConfirmResultKind,
    PendingActionStatus,
    PendingActionTransientFailure,
    PendingActionType,
)
from lingxi.core.ids import new_id
from lingxi.core.permission.local_override import OverrideDirection, resolve_local_overrides

DSN = os.environ.get("LINGXI_POSTGRES_DSN")
SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，待确认操作的真库断言未验证（需真实 PostgreSQL 16）"
    if not DSN
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，待确认操作的真库断言未验证"
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


@unittest.skipUnless(DSN and psycopg_available(), SKIP_REASON)
class PendingActionPostgresTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._dsn = DSN
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        reset_production_rows(self._dsn)
        self.audit = _RecordingAudit()
        self.store = PostgresPendingActionStore(self._dsn, audit=self.audit)
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
                thread_store = PostgresPendingActionStore(self._dsn, audit=thread_audit)
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
            "UPDATE pending_action SET confirm_deadline_at = now() - interval '1 second'"
            " WHERE id = %s",
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
        failing_store = PostgresPendingActionStore(self._dsn, audit=failing_audit)

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
            self._dsn, audit=_RecordingAudit(raise_error=True)
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

    def test_mark_card_delivered_advances_card_sequence_baseline_to_two(self) -> None:
        """Issue #96 卡片回调应答修复：CardKit 建卡 + 发送各自消耗一次整卡级
        sequence（实测依据见 ``mark_card_delivered`` 文档）——序号 1、2 的全量
        更新会被平台幂等吞掉，真正生效的更新必须携带 sequence>=3。"""

        self.add_target_user(account_state="enabled")
        outcome = self.store.prepare(
            action_type=PendingActionType.SUSPEND_USER,
            target_open_id=TARGET_OPEN_ID,
            initiated_by_open_id=ADMIN_OPEN_ID,
        )
        assert outcome.pending is not None

        self.store.mark_card_delivered(
            pending_action_id=outcome.pending.id, card_id="cardkit_seq_baseline"
        )

        rows = self.query(
            "SELECT card_sequence FROM pending_action WHERE id = %s", (outcome.pending.id,)
        )
        self.assertGreaterEqual(rows[0][0], 2, "送达后的 sequence 基线必须至少为 2")
        next_sequence = self.store.next_card_sequence(pending_action_id=outcome.pending.id)
        self.assertEqual(next_sequence, 3, "送达后第一次终态更新必须使用 sequence>=3 才会生效")

    def test_mark_card_delivered_does_not_regress_an_already_advanced_sequence(self) -> None:
        """``GREATEST`` 而非覆盖式赋值：万一 ``mark_card_delivered`` 被重复调用，
        不能把已经因为其它路径前进过的 sequence 倒退回 2。"""

        self.add_target_user(account_state="enabled")
        outcome = self.store.prepare(
            action_type=PendingActionType.SUSPEND_USER,
            target_open_id=TARGET_OPEN_ID,
            initiated_by_open_id=ADMIN_OPEN_ID,
        )
        assert outcome.pending is not None
        self.execute(
            "UPDATE pending_action SET card_sequence = 5 WHERE id = %s", (outcome.pending.id,)
        )

        self.store.mark_card_delivered(
            pending_action_id=outcome.pending.id, card_id="cardkit_seq_no_regress"
        )

        rows = self.query(
            "SELECT card_sequence FROM pending_action WHERE id = %s", (outcome.pending.id,)
        )
        self.assertEqual(rows[0][0], 5, "已经前进过的 sequence 不得被 mark_card_delivered 拉低")

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


class NextCardSequenceRealDbTests(PendingActionPostgresTestCase):
    """外部审查交叉裁定（opus P2-1）：CardKit 整卡级 sequence 记账，见迁移 0068
    文件头部「为什么需要 card_sequence 记账」。"""

    def test_sequence_starts_at_zero_and_increments_by_one_each_call(self) -> None:
        self.add_target_user(account_state="enabled")
        outcome = self.store.prepare(
            action_type=PendingActionType.SUSPEND_USER,
            target_open_id=TARGET_OPEN_ID,
            initiated_by_open_id=ADMIN_OPEN_ID,
        )
        assert outcome.pending is not None
        self.assertEqual(outcome.pending.card_sequence, 0, "建卡不消耗 sequence")

        first = self.store.next_card_sequence(pending_action_id=outcome.pending.id)
        second = self.store.next_card_sequence(pending_action_id=outcome.pending.id)
        third = self.store.next_card_sequence(pending_action_id=outcome.pending.id)

        self.assertEqual((first, second, third), (1, 2, 3))
        rows = self.query(
            "SELECT card_sequence FROM pending_action WHERE id = %s", (outcome.pending.id,)
        )
        self.assertEqual(rows[0][0], 3, "落库的值必须与最后一次取号一致")

    def test_unknown_pending_action_id_raises_lookup_error(self) -> None:
        with self.assertRaises(LookupError):
            self.store.next_card_sequence(pending_action_id="pac_never_existed_seq")


class LockThenFetchTimeRealDbTests(PendingActionPostgresTestCase):
    """否定断言（外部审查交叉裁定，codex P1-3）：``confirm()`` 判定过期用的时钟
    必须在拿到行锁之后才读取，不能用等锁之前的旧时间——等锁期间如果被并发事务
    卡住，锁前的旧时间会让本该过期的操作被误判为仍然有效。"""

    def test_expiry_becomes_effective_while_confirm_is_waiting_for_the_row_lock(self) -> None:
        self.add_target_user(account_state="enabled")
        pending_id = self.prepare_and_deliver()
        # 有效期设成很快过期；真实时间会在下面持锁等待期间越过这条线。
        self.execute(
            "UPDATE pending_action SET confirm_deadline_at = now() + interval '0.4 seconds'"
            " WHERE id = %s",
            (pending_id,),
        )

        lock_acquired = threading.Event()
        release_lock = threading.Event()
        holder_errors: list[BaseException] = []

        def hold_the_row_lock() -> None:
            try:
                with connect(self._dsn) as connection:
                    with connection.transaction():
                        with connection.cursor() as cursor:
                            cursor.execute(
                                "SELECT id FROM pending_action WHERE id = %s FOR UPDATE",
                                (pending_id,),
                            )
                            cursor.fetchone()
                            lock_acquired.set()
                            release_lock.wait(timeout=5)
            except BaseException as error:  # noqa: BLE001 - 收集到主线程再断言
                holder_errors.append(error)

        holder = threading.Thread(target=hold_the_row_lock)
        holder.start()
        self.assertTrue(lock_acquired.wait(timeout=5), "持锁线程未能及时拿到行锁")
        # 此刻真实时间还没到 0.4 秒过期线；立刻发起 confirm()，让它卡在等同一把
        # 行锁上——旧代码会在这里（进入 confirm() 的瞬间）就取走 now()。

        confirm_results: list[object] = []
        confirm_errors: list[BaseException] = []

        def call_confirm() -> None:
            try:
                confirm_results.append(
                    self.store.confirm(pending_action_id=pending_id, clicker_open_id=ADMIN_OPEN_ID)
                )
            except BaseException as error:  # noqa: BLE001
                confirm_errors.append(error)

        confirmer = threading.Thread(target=call_confirm)
        confirmer.start()
        time.sleep(0.9)  # confirm() 应该正卡在等锁上；真实时间已经越过 0.4 秒过期线
        release_lock.set()
        holder.join(timeout=5)
        confirmer.join(timeout=5)

        self.assertEqual(holder_errors, [], f"持锁线程不应抛出未预期的异常：{holder_errors}")
        self.assertEqual(confirm_errors, [], f"confirm 线程不应抛出未预期的异常：{confirm_errors}")
        self.assertEqual(len(confirm_results), 1)
        result = confirm_results[0]

        self.assertFalse(result.decision.ok, "锁前的旧时间不应被用来判定过期")
        self.assertIs(result.decision.kind, ConfirmResultKind.EXPIRE)
        self.assertEqual(self.current_account_state(), "enabled", "过期时不得执行")
        assert result.pending is not None
        self.assertEqual(result.pending.status, PendingActionStatus.EXPIRED)


class RoleCheckWithinTransactionRealDbTests(PendingActionPostgresTestCase):
    """否定断言（外部审查交叉裁定，codex P1-4）：确认时刻的角色核对必须在同一
    事务内、对 admin_registry 目标行加锁完成，不能走一条独立、不受锁保护的连接
    ——那样的读法在"撤权事务已经拿到行锁但还没提交"这段窗口里，仍然会读到撤权前
    的旧数据，让确认在事务序列化的意义上"抢跑"在一次真正更早发生的撤权前面。"""

    def test_confirm_sees_a_revocation_that_committed_while_it_was_waiting_on_the_registry_lock(
        self,
    ) -> None:
        self.add_target_user(account_state="enabled")
        pending_id = self.prepare_and_deliver()

        revoke_holding = threading.Event()
        release_revoke = threading.Event()
        revoke_errors: list[BaseException] = []

        def hold_the_revoke_open() -> None:
            try:
                with connect(self._dsn) as connection:
                    with connection.transaction():
                        with connection.cursor() as cursor:
                            cursor.execute(
                                "UPDATE admin_registry SET entry_status = 'revoked',"
                                " revoked_at = now() WHERE feishu_open_id = %s",
                                (ADMIN_OPEN_ID,),
                            )
                            revoke_holding.set()
                            release_revoke.wait(timeout=5)
            except BaseException as error:  # noqa: BLE001
                revoke_errors.append(error)

        holder = threading.Thread(target=hold_the_revoke_open)
        holder.start()
        self.assertTrue(revoke_holding.wait(timeout=5), "撤权持锁线程未能及时拿到行锁")
        # 撤权事务此刻已经拿到 admin_registry 该行的排它锁，但还没提交。

        confirm_results: list[object] = []
        confirm_errors: list[BaseException] = []

        def call_confirm() -> None:
            try:
                confirm_results.append(
                    self.store.confirm(pending_action_id=pending_id, clicker_open_id=ADMIN_OPEN_ID)
                )
            except BaseException as error:  # noqa: BLE001
                confirm_errors.append(error)

        confirmer = threading.Thread(target=call_confirm)
        confirmer.start()
        time.sleep(0.4)  # 给 confirm() 机会跑到（并卡在）角色核对这一步
        release_revoke.set()
        holder.join(timeout=5)
        confirmer.join(timeout=5)

        self.assertEqual(revoke_errors, [], f"撤权线程不应抛出未预期的异常：{revoke_errors}")
        self.assertEqual(confirm_errors, [], f"confirm 线程不应抛出未预期的异常：{confirm_errors}")
        self.assertEqual(len(confirm_results), 1)
        result = confirm_results[0]

        self.assertFalse(result.decision.ok, "撤权先提交，确认必须看到撤权后的角色状态")
        self.assertIs(result.decision.kind, ConfirmResultKind.ROLE_REVOKED)
        self.assertEqual(self.current_account_state(), "enabled", "角色核对失败，目标账号状态不得改变")
        assert result.pending is not None
        self.assertEqual(result.pending.status, PendingActionStatus.FAILED)
        self.assertEqual(result.pending.reason, "role_revoked")


class SameTargetInFlightExclusionRealDbTests(PendingActionPostgresTestCase):
    """否定断言（外部审查交叉裁定，codex P1-5，ABA）：同一目标同一时刻只允许一条
    在途待确认操作；终态后可以重新发起。"""

    def test_second_prepare_for_the_same_target_while_one_is_pending_is_rejected(self) -> None:
        self.add_target_user(account_state="enabled")
        first = self.store.prepare(
            action_type=PendingActionType.SUSPEND_USER,
            target_open_id=TARGET_OPEN_ID,
            initiated_by_open_id=ADMIN_OPEN_ID,
        )
        self.assertTrue(first.decision.ok)

        second = self.store.prepare(
            action_type=PendingActionType.SUSPEND_USER,
            target_open_id=TARGET_OPEN_ID,
            initiated_by_open_id=ADMIN_OPEN_ID,
        )

        self.assertFalse(second.decision.ok)
        self.assertEqual(second.decision.code, TARGET_HAS_PENDING_ACTION_CODE)
        self.assertIsNone(second.pending)
        rows = self.query(
            "SELECT count(*) FROM pending_action WHERE target_open_id = %s", (TARGET_OPEN_ID,)
        )
        self.assertEqual(rows[0][0], 1, "第二次 prepare 不得插入任何行")

    def test_a_different_target_is_not_affected_by_an_in_flight_action(self) -> None:
        other_target = "ou_pending_action_target_2"
        self.add_target_user(open_id=TARGET_OPEN_ID, account_state="enabled")
        self.add_target_user(open_id=other_target, account_state="enabled")
        first = self.store.prepare(
            action_type=PendingActionType.SUSPEND_USER,
            target_open_id=TARGET_OPEN_ID,
            initiated_by_open_id=ADMIN_OPEN_ID,
        )
        self.assertTrue(first.decision.ok)

        second = self.store.prepare(
            action_type=PendingActionType.SUSPEND_USER,
            target_open_id=other_target,
            initiated_by_open_id=ADMIN_OPEN_ID,
        )

        self.assertTrue(second.decision.ok)

    def test_after_the_first_terminates_the_same_target_can_be_prepared_again(self) -> None:
        """同时消除 opus 实测到的「同目标可并存多条 pending」怪象与 codex 的 ABA
        （A 停用→resume→旧 B 卡再次停用）：终态化之后允许重新发起，不是永久锁死。"""

        self.add_target_user(account_state="enabled")
        first = self.store.prepare(
            action_type=PendingActionType.SUSPEND_USER,
            target_open_id=TARGET_OPEN_ID,
            initiated_by_open_id=ADMIN_OPEN_ID,
        )
        assert first.pending is not None
        self.store.mark_card_delivered(pending_action_id=first.pending.id, card_id="cardkit_1")
        cancelled = self.store.cancel(
            pending_action_id=first.pending.id, clicker_open_id=ADMIN_OPEN_ID
        )
        self.assertTrue(cancelled.decision.ok)

        second = self.store.prepare(
            action_type=PendingActionType.SUSPEND_USER,
            target_open_id=TARGET_OPEN_ID,
            initiated_by_open_id=ADMIN_OPEN_ID,
        )

        self.assertTrue(second.decision.ok, "终态之后应当允许对同一目标重新发起")
        rows = self.query(
            "SELECT count(*) FROM pending_action WHERE target_open_id = %s AND status = 'pending'",
            (TARGET_OPEN_ID,),
        )
        self.assertEqual(rows[0][0], 1)


class SuspendPurgeRealDbTests(PendingActionPostgresTestCase):
    """停用「感知即清」的接入（Issue #304 批次 4）：``suspend_user`` 确认执行时，
    在 ``confirm()`` 自己的同一个数据库事务里为该用户排队全部会话保留正文的
    清理——复用 ``_Transaction.clear_delivered_content_for_user``（Issue #153
    冻结接口，历史上只被 ``clear_delivered_content_for_user`` 独立开事务的入口
    调用过，从未被真正触发）。``resume_user`` 不做任何清理（不可逆、合同语义）。
    """

    def user_id_for(self, open_id: str) -> str:
        rows = self.query("SELECT id FROM app_user WHERE feishu_open_id = %s", (open_id,))
        self.assertEqual(len(rows), 1)
        return rows[0][0]

    def seed_delivered_conversation(
        self, *, conversation_id: str, task_id: str, user_id: str, agent_session_id: str
    ) -> None:
        """建一条已经送达、仍随会话保留正文、且持有当前 Agent 会话的会话——
        与 `test_delivery_outbox.py::_seed_delivered` 同一手法，这里用原生 SQL
        直写，不为此额外引入 `PostgresTaskQueue` 依赖。"""

        self.execute(
            """INSERT INTO conversation (id, user_id, feishu_chat_id, feishu_thread_id, agent_session_id)
               VALUES (%s, %s, %s, %s, %s)""",
            (conversation_id, user_id, f"chat-{conversation_id}", f"topic-{conversation_id}", agent_session_id),
        )
        self.execute(
            """INSERT INTO task
                   (id, conversation_id, user_id, inbound_event_id, prompt, status,
                    target_worker_version, attempts, content_expires_at)
               VALUES (%s, %s, %s, %s, '问题', 'awaiting_delivery', 'stable', 1, now())""",
            (task_id, conversation_id, user_id, f"event-{task_id}"),
        )
        self.execute(
            """INSERT INTO task_delivery_event
                   (id, task_id, sequence, event_type, terminal_kind, worker_id,
                    idempotency_key, platform_received_at, content)
               VALUES (%s, %s, 1, 'terminal', 'success', 'worker-1', %s, now(), '已送达的答案')""",
            (new_id("tde"), task_id, f"{task_id}:terminal"),
        )

    def pending_reasons(self, *, agent_session_id: str) -> list[str]:
        return [
            row[0]
            for row in self.query(
                "SELECT reason FROM agent_session_cleanup WHERE agent_session_id = %s",
                (agent_session_id,),
            )
        ]

    def test_suspend_confirm_queues_cleanup_for_every_conversation_of_that_user_only(self) -> None:
        self.add_target_user(account_state="enabled")
        target_user_id = self.user_id_for(TARGET_OPEN_ID)
        self.seed_delivered_conversation(
            conversation_id="cnv-a1", task_id="tsk-a1", user_id=target_user_id, agent_session_id="sess-a1"
        )
        self.seed_delivered_conversation(
            conversation_id="cnv-a2", task_id="tsk-a2", user_id=target_user_id, agent_session_id="sess-a2"
        )
        # 另一个用户的会话完全不受影响。
        self.add_target_user(open_id="ou_other_user", account_state="enabled")
        other_user_id = self.user_id_for("ou_other_user")
        self.seed_delivered_conversation(
            conversation_id="cnv-b1", task_id="tsk-b1", user_id=other_user_id, agent_session_id="sess-b1"
        )
        pending_id = self.prepare_and_deliver(action_type=PendingActionType.SUSPEND_USER)

        result = self.store.confirm(pending_action_id=pending_id, clicker_open_id=ADMIN_OPEN_ID)

        self.assertTrue(result.decision.ok)
        self.assertEqual(self.current_account_state(), "suspended")
        # Outbox 正文：目标用户的两个会话都被清空。
        self.assertIsNone(self.query("SELECT content FROM task_delivery_event WHERE task_id='tsk-a1'")[0][0])
        self.assertIsNone(self.query("SELECT content FROM task_delivery_event WHERE task_id='tsk-a2'")[0][0])
        # 会话上下文指针：硬失效，立即置空，不等两小时规则。
        self.assertIsNone(self.query("SELECT agent_session_id FROM conversation WHERE id='cnv-a1'")[0][0])
        self.assertIsNone(self.query("SELECT agent_session_id FROM conversation WHERE id='cnv-a2'")[0][0])
        # Agent 会话 JSONL 物理清理已排队，user_id 是目标用户的内部 id。
        self.assertEqual(self.pending_reasons(agent_session_id="sess-a1"), ["user_cleared"])
        self.assertEqual(self.pending_reasons(agent_session_id="sess-a2"), ["user_cleared"])
        self.assertEqual(
            self.query("SELECT user_id FROM agent_session_cleanup WHERE agent_session_id='sess-a1'")[0][0],
            target_user_id,
        )
        # 另一个用户的会话（正文、会话指针、清理队列）完全不受影响。
        self.assertEqual(
            self.query("SELECT content FROM task_delivery_event WHERE task_id='tsk-b1'")[0][0], "已送达的答案"
        )
        self.assertEqual(
            self.query("SELECT agent_session_id FROM conversation WHERE id='cnv-b1'")[0][0], "sess-b1"
        )
        self.assertEqual(self.pending_reasons(agent_session_id="sess-b1"), [])

    def test_resume_confirm_does_not_purge_anything(self) -> None:
        self.add_target_user(account_state="suspended")
        target_user_id = self.user_id_for(TARGET_OPEN_ID)
        self.seed_delivered_conversation(
            conversation_id="cnv-a1", task_id="tsk-a1", user_id=target_user_id, agent_session_id="sess-a1"
        )
        pending_id = self.prepare_and_deliver(action_type=PendingActionType.RESUME_USER)

        result = self.store.confirm(pending_action_id=pending_id, clicker_open_id=ADMIN_OPEN_ID)

        self.assertTrue(result.decision.ok)
        self.assertEqual(self.current_account_state(), "enabled")
        # resume 不恢复已清正文（本用例里正文从未被清过），也不主动清除任何东西
        # ——合同只对停用/权限变化两类触发感知即清，resume 不在其中。
        self.assertEqual(
            self.query("SELECT content FROM task_delivery_event WHERE task_id='tsk-a1'")[0][0], "已送达的答案"
        )
        self.assertEqual(
            self.query("SELECT agent_session_id FROM conversation WHERE id='cnv-a1'")[0][0], "sess-a1"
        )
        self.assertEqual(self.pending_reasons(agent_session_id="sess-a1"), [])

    def test_repeated_confirm_does_not_queue_cleanup_twice(self) -> None:
        """幂等：`decide_confirm` 早已是终态的第二次点击不再进入 EXECUTE 分支
        （见 `core/admin/pending_action.py`），因此清理排队结构上只可能发生一次；
        即使未来这条不变量被打破，`agent_session_cleanup.agent_session_id` 上的
        唯一索引仍是数据库层面的最终保险。"""

        self.add_target_user(account_state="enabled")
        target_user_id = self.user_id_for(TARGET_OPEN_ID)
        self.seed_delivered_conversation(
            conversation_id="cnv-a1", task_id="tsk-a1", user_id=target_user_id, agent_session_id="sess-a1"
        )
        pending_id = self.prepare_and_deliver(action_type=PendingActionType.SUSPEND_USER)

        first = self.store.confirm(pending_action_id=pending_id, clicker_open_id=ADMIN_OPEN_ID)
        second = self.store.confirm(pending_action_id=pending_id, clicker_open_id=ADMIN_OPEN_ID)

        self.assertTrue(first.decision.ok)
        self.assertEqual(second.decision.kind, ConfirmResultKind.ALREADY_TERMINAL)
        self.assertEqual(
            self.query("SELECT count(*) FROM agent_session_cleanup WHERE agent_session_id='sess-a1'")[0][0],
            1,
            "重复确认（幂等重放）不得排出第二条清理待办",
        )

    def test_resume_confirm_does_not_disturb_a_cleanup_already_queued_by_an_earlier_suspend(
        self,
    ) -> None:
        """resume 不产生清理，**也不阻止已排队清理**：一个此前停用时排出的
        `agent_session_cleanup` 待办，不会因为随后一次 resume 确认而被清空、
        标记完成或改变——resume 的 EXECUTE 分支结构上完全不触碰这张表，物理
        清理仍归 Worker 周期性收口独立处理。"""

        self.add_target_user(account_state="suspended")
        target_user_id = self.user_id_for(TARGET_OPEN_ID)
        # 模拟"此前一次停用已经排过队，但 Worker 还没来得及物理清理"：直接插入
        # 一条待处理的 agent_session_cleanup 行，不经过 confirm()。
        self.execute(
            """INSERT INTO agent_session_cleanup (id, user_id, agent_session_id, reason)
               VALUES (%s, %s, 'sess-already-queued', 'user_cleared')""",
            (new_id("asc"), target_user_id),
        )
        pending_id = self.prepare_and_deliver(action_type=PendingActionType.RESUME_USER)

        result = self.store.confirm(pending_action_id=pending_id, clicker_open_id=ADMIN_OPEN_ID)

        self.assertTrue(result.decision.ok)
        self.assertEqual(self.current_account_state(), "enabled")
        row = self.query(
            "SELECT reason, done_at FROM agent_session_cleanup WHERE agent_session_id='sess-already-queued'"
        )
        self.assertEqual(len(row), 1, "已排队的清理待办必须原样保留，不能被 resume 清空")
        self.assertEqual(row[0][0], "user_cleared")
        self.assertIsNone(row[0][1], "resume 不得替 Worker 提前把这条待办标记完成")

    def test_two_real_concurrent_confirms_queue_cleanup_exactly_once(self) -> None:
        """真实并发用例（与 `DuplicateAndConcurrentConfirmTests.test_two_real_
        concurrent_confirms_only_one_succeeds` 同一手法）：两个线程各自开自己的
        数据库连接，几乎同时对同一条 suspend 待确认操作发起确认——``SELECT ...
        FOR UPDATE`` 序列化后只有一个线程真正进入 `EXECUTE` 分支，清理排队因此
        结构上只可能发生一次；`agent_session_cleanup.agent_session_id` 唯一索引
        是数据库层面的最终保险。"""

        self.add_target_user(account_state="enabled")
        target_user_id = self.user_id_for(TARGET_OPEN_ID)
        self.seed_delivered_conversation(
            conversation_id="cnv-a1", task_id="tsk-a1", user_id=target_user_id, agent_session_id="sess-a1"
        )
        pending_id = self.prepare_and_deliver(action_type=PendingActionType.SUSPEND_USER)

        barrier = threading.Barrier(2)
        results: list[object] = [None, None]
        errors: list[BaseException] = []

        def confirm_from_thread(index: int) -> None:
            try:
                barrier.wait(timeout=5)
                thread_store = PostgresPendingActionStore(self._dsn, audit=_RecordingAudit())
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
        outcomes = sorted(result.decision.ok for result in results)
        self.assertEqual(outcomes, [False, True], "恰好一个线程执行成功，另一个必须被拒绝")
        self.assertEqual(self.current_account_state(), "suspended")
        self.assertEqual(
            self.query("SELECT count(*) FROM agent_session_cleanup WHERE agent_session_id='sess-a1'")[0][0],
            1,
            "真实并发的两次确认，清理待办也只能出现恰好一条",
        )
        self.assertIsNone(self.query("SELECT content FROM task_delivery_event WHERE task_id='tsk-a1'")[0][0])


class TransientFailureRealDbTests(PendingActionPostgresTestCase):
    """批次 4 F1（Issue #304，opus 审查）：``confirm()`` 现在对目标用户全部会话
    一次性 ``FOR UPDATE``（见 ``_transaction.py`` 的 ``clear_delivered_content_
    for_user`` 锁序修复）——这是死锁之外的**非死锁变体**：如果其中一个会话恰好
    被另一个事务（本用例模拟 gateway 入站事务）持锁超过 ``lock_timeout``
    （2 秒，``adapters/postgres.py`` 的 ``PostgresTimeouts`` 默认值），
    ``confirm()`` 必须把裸 psycopg 的 ``LockNotAvailable`` 转译成
    :class:`~lingxi.core.admin.pending_action.PendingActionTransientFailure`
    （事务已回滚、可重试），不能让它一路抛到调用方，也不能无界等待——「停用一个
    正在聊天的用户」最容易撞见这一种。
    """

    def _user_id_for(self, open_id: str) -> str:
        # 与 SuspendPurgeRealDbTests.user_id_for 同一内容——不继承那个类，是为了
        # 不把它已有的用例方法当成本类的一部分再跑一遍（unittest 按类收集
        # test_* 方法，继承会让同一批用例在两个类名下各跑一次）。
        rows = self.query("SELECT id FROM app_user WHERE feishu_open_id = %s", (open_id,))
        self.assertEqual(len(rows), 1)
        return rows[0][0]

    def _seed_delivered_conversation(
        self, *, conversation_id: str, task_id: str, user_id: str, agent_session_id: str
    ) -> None:
        # 与 SuspendPurgeRealDbTests.seed_delivered_conversation 同一内容，理由同上。
        self.execute(
            """INSERT INTO conversation (id, user_id, feishu_chat_id, feishu_thread_id, agent_session_id)
               VALUES (%s, %s, %s, %s, %s)""",
            (conversation_id, user_id, f"chat-{conversation_id}", f"topic-{conversation_id}", agent_session_id),
        )
        self.execute(
            """INSERT INTO task
                   (id, conversation_id, user_id, inbound_event_id, prompt, status,
                    target_worker_version, attempts, content_expires_at)
               VALUES (%s, %s, %s, %s, '问题', 'awaiting_delivery', 'stable', 1, now())""",
            (task_id, conversation_id, user_id, f"event-{task_id}"),
        )
        self.execute(
            """INSERT INTO task_delivery_event
                   (id, task_id, sequence, event_type, terminal_kind, worker_id,
                    idempotency_key, platform_received_at, content)
               VALUES (%s, %s, 1, 'terminal', 'success', 'worker-1', %s, now(), '已送达的答案')""",
            (new_id("tde"), task_id, f"{task_id}:terminal"),
        )

    def test_confirm_raises_transient_failure_when_a_conversation_is_held_past_lock_timeout(
        self,
    ) -> None:
        self.add_target_user(account_state="enabled")
        target_user_id = self._user_id_for(TARGET_OPEN_ID)
        self._seed_delivered_conversation(
            conversation_id="cnv-lockwait",
            task_id="tsk-lockwait",
            user_id=target_user_id,
            agent_session_id="sess-lockwait",
        )
        pending_id = self.prepare_and_deliver(action_type=PendingActionType.SUSPEND_USER)

        lock_acquired = threading.Event()
        release_lock = threading.Event()
        holder_errors: list[BaseException] = []

        def hold_the_conversation_lock() -> None:
            # 模拟 gateway 入站事务：抢占并长时间持有该用户某一个会话的行锁
            # （真实场景是 ensure_conversation/claim_conversation 所在的事务
            # 意外变慢），不提交也不回滚，直到测试主动释放。
            try:
                with connect(self._dsn) as connection:
                    with connection.transaction():
                        with connection.cursor() as cursor:
                            cursor.execute(
                                "SELECT id FROM conversation WHERE id = %s FOR UPDATE",
                                ("cnv-lockwait",),
                            )
                            cursor.fetchone()
                            lock_acquired.set()
                            release_lock.wait(timeout=10)
            except BaseException as error:  # noqa: BLE001 - 收集到主线程再断言
                holder_errors.append(error)

        holder = threading.Thread(target=hold_the_conversation_lock)
        holder.start()
        self.assertTrue(lock_acquired.wait(timeout=5), "持锁线程未能及时拿到 conversation 行锁")

        started_at = time.monotonic()
        try:
            with self.assertRaises(PendingActionTransientFailure) as raised:
                self.store.confirm(pending_action_id=pending_id, clicker_open_id=ADMIN_OPEN_ID)
        finally:
            release_lock.set()
            holder.join(timeout=5)

        elapsed = time.monotonic() - started_at
        self.assertEqual(holder_errors, [], f"持锁线程不应抛出未预期的异常：{holder_errors}")
        self.assertEqual(
            raised.exception.classification,
            "LockNotAvailable",
            "必须是锁等待超时（sqlstate 55P03），不是别的操作性故障",
        )
        self.assertGreaterEqual(
            elapsed, 1.9, "应当等到 lock_timeout（2 秒）附近才失败，不能提前放弃或无界等待"
        )

        # 事务已整体回滚：pending_action 与目标账号均保持事务开始前的状态，
        # 调用方（card_callback.py）据此认为"这次点击结构上没有发生过"，可以
        # 直接重新点击重试。
        self.assertEqual(self.current_account_state(TARGET_OPEN_ID), "enabled")
        rows = self.query("SELECT status FROM pending_action WHERE id = %s", (pending_id,))
        self.assertEqual(rows[0][0], "pending")


class LocalPermissionGrantSuppressRealDbTests(PendingActionPostgresTestCase):
    """本地权限授权/抑制全链路真库断言（迁移 ``0073``，#319 S-P-1b 设计卡）。

    覆盖设计卡登记的行为锚点：①新命令不绕过身份判定见 ``tests/test_admin_router.py``
    （本文件只覆盖 adapter 层）；②"未确认不落行"；③漂移黑盒（SAVEPOINT 降级）；
    ⑥同目标在途互斥覆盖新类型。变异锚点（登记后已还原，见任务收口说明）：删
    SAVEPOINT 降级会让 ``test_confirm_downgrades_to_target_drifted_when_key_
    is_taken_before_confirm`` 变红（未捕获的 ``DuplicateActiveOverride``
    直接从 ``confirm()`` 冒泡）；把 ``VALID_SOURCE_STATES[GRANT]`` 改成
    ``{"present"}`` 会让 prepare 阶段本身直接拒绝，本类多个用例连锁变红。
    """

    COMPANY_ID = "1011"
    METRIC_NAME = "daily_active"
    REASON = "特批"

    def user_id_for(self, open_id: str = TARGET_OPEN_ID) -> str:
        rows = self.query("SELECT id FROM app_user WHERE feishu_open_id = %s", (open_id,))
        self.assertEqual(len(rows), 1)
        return rows[0][0]

    def active_override_count(
        self, *, user_id: str, company_id: str = COMPANY_ID, metric_name: str = METRIC_NAME
    ) -> int:
        rows = self.query(
            "SELECT count(*) FROM local_permission_override"
            " WHERE user_id = %s AND company_id = %s AND metric_name = %s"
            "   AND entry_status = 'active'",
            (user_id, company_id, metric_name),
        )
        return rows[0][0]

    def prepare_and_deliver_permission(
        self,
        *,
        action_type: PendingActionType = PendingActionType.LOCAL_PERMISSION_GRANT,
        target_open_id: str = TARGET_OPEN_ID,
        company_id: str = COMPANY_ID,
        metric_name: str = METRIC_NAME,
        reason: str = REASON,
    ) -> str:
        outcome = self.store.prepare(
            action_type=action_type,
            target_open_id=target_open_id,
            initiated_by_open_id=ADMIN_OPEN_ID,
            company_id=company_id,
            metric_name=metric_name,
            reason=reason,
        )
        self.assertTrue(outcome.decision.ok, outcome.decision.message)
        assert outcome.pending is not None
        self.store.mark_card_delivered(
            pending_action_id=outcome.pending.id, card_id="cardkit_test"
        )
        return outcome.pending.id

    def add_bystander_pending_action(self, *, pending_id: str) -> str:
        """插入一条已经处于终态的最小 pending_action 行，仅用于满足迁移 ``0072``
        的 ``pending_action_id`` 外键——不代表本用例正在测试的确认卡流程本身，
        与 ``tests/test_local_permission_postgres.py`` 的 ``add_pending_action``
        同一手法（这里额外把它终态化，因为它不该占用
        ``pending_action_single_pending_target_idx`` 的名额）。
        """

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
                ADMIN_OPEN_ID,
                now + timedelta(minutes=10),
                now,
                ADMIN_OPEN_ID,
            ),
        )
        return pending_id

    def test_grant_confirm_inserts_a_local_permission_override_row(self) -> None:
        self.add_target_user(account_state="enabled")
        pending_id = self.prepare_and_deliver_permission()

        result = self.store.confirm(pending_action_id=pending_id, clicker_open_id=ADMIN_OPEN_ID)

        self.assertTrue(result.decision.ok)
        assert result.pending is not None
        self.assertEqual(result.pending.status, PendingActionStatus.EXECUTED)
        user_id = self.user_id_for()
        self.assertEqual(self.active_override_count(user_id=user_id), 1)
        row = self.query(
            "SELECT direction, reason, initiated_by_open_id, pending_action_id"
            " FROM local_permission_override WHERE user_id = %s AND entry_status = 'active'",
            (user_id,),
        )[0]
        self.assertEqual(row[0], "grant")
        self.assertEqual(row[1], self.REASON)
        self.assertEqual(row[2], ADMIN_OPEN_ID)
        self.assertEqual(row[3], pending_id)
        # 本地权限动作不改 account_state（suspend/resume 才改）。
        self.assertEqual(self.current_account_state(), "enabled")

    def test_suppress_confirm_inserts_a_local_permission_override_row(self) -> None:
        self.add_target_user(account_state="enabled")
        pending_id = self.prepare_and_deliver_permission(
            action_type=PendingActionType.LOCAL_PERMISSION_SUPPRESS
        )

        result = self.store.confirm(pending_action_id=pending_id, clicker_open_id=ADMIN_OPEN_ID)

        self.assertTrue(result.decision.ok)
        user_id = self.user_id_for()
        row = self.query(
            "SELECT direction FROM local_permission_override"
            " WHERE user_id = %s AND entry_status = 'active'",
            (user_id,),
        )[0]
        self.assertEqual(row[0], "suppress")

    def test_prepare_alone_does_not_write_any_override_row(self) -> None:
        """②行为级『未确认不落行』：``prepare()`` 只建待确认操作，不写
        ``local_permission_override``——抓「写早了」变异。"""

        self.add_target_user(account_state="enabled")
        self.prepare_and_deliver_permission()

        store = PostgresLocalPermissionOverrideStore(self._dsn)
        self.assertEqual(store.effective_entries(user_id=self.user_id_for()), ())

    def test_cancel_does_not_write_any_override_row(self) -> None:
        """②的另一半：取消同样不落行。"""

        self.add_target_user(account_state="enabled")
        pending_id = self.prepare_and_deliver_permission()

        cancelled = self.store.cancel(pending_action_id=pending_id, clicker_open_id=ADMIN_OPEN_ID)

        self.assertTrue(cancelled.decision.ok)
        store = PostgresLocalPermissionOverrideStore(self._dsn)
        self.assertEqual(store.effective_entries(user_id=self.user_id_for()), ())

    def test_confirm_downgrades_to_target_drifted_when_key_is_taken_before_confirm(self) -> None:
        """③漂移黑盒：prepare grant 后，另一笔（模拟一次已经独立完成的授权）
        抢先在同一个键上落地 → confirm 落 FAILED/target_drifted，且该键最终
        恰好 1 条生效行。真正的检测机制是 confirm() 的 INSERT 直接撞迁移
        ``0072`` 的部分唯一索引，被 SAVEPOINT 捕获后降级——不经过
        ``decide_confirm`` 的字符串比较（``adapters/postgres_pending_action.py``
        模块文档「本地权限授权/抑制如何复用同一套机制」）。
        """

        self.add_target_user(account_state="enabled")
        pending_id = self.prepare_and_deliver_permission()
        user_id = self.user_id_for()

        bystander_pending_id = self.add_bystander_pending_action(pending_id=new_id("pac"))
        other_store = PostgresLocalPermissionOverrideStore(self._dsn)
        other_store.insert(
            user_id=user_id,
            direction=OverrideDirection.GRANT,
            company_id=self.COMPANY_ID,
            metric_name=self.METRIC_NAME,
            reason="抢先落地",
            initiated_by_open_id=ADMIN_OPEN_ID,
            pending_action_id=bystander_pending_id,
        )

        result = self.store.confirm(pending_action_id=pending_id, clicker_open_id=ADMIN_OPEN_ID)

        self.assertFalse(result.decision.ok)
        self.assertIs(result.decision.kind, ConfirmResultKind.TARGET_DRIFTED)
        assert result.pending is not None
        self.assertEqual(result.pending.status, PendingActionStatus.FAILED)
        self.assertEqual(result.pending.reason, "target_drifted")
        self.assertEqual(self.active_override_count(user_id=user_id), 1)
        # 我们自己这次 confirm 没有额外写入——生效的仍然是"抢先落地"那一条。
        row = self.query(
            "SELECT reason FROM local_permission_override"
            " WHERE user_id = %s AND entry_status = 'active'",
            (user_id,),
        )[0]
        self.assertEqual(row[0], "抢先落地")

    def test_second_permission_prepare_for_a_target_already_in_flight_is_rejected(self) -> None:
        """⑥同目标在途互斥覆盖新类型：迁移 ``0068`` 的
        ``pending_action_single_pending_target_idx`` 按 ``target_open_id``
        分区，不区分 ``action_type``——本地权限动作与 suspend/resume 共用同一条
        唯一索引，不需要任何新代码。"""

        self.add_target_user(account_state="enabled")
        self.prepare_and_deliver_permission()

        second_outcome = self.store.prepare(
            action_type=PendingActionType.LOCAL_PERMISSION_SUPPRESS,
            target_open_id=TARGET_OPEN_ID,
            initiated_by_open_id=ADMIN_OPEN_ID,
            company_id="1012",
            metric_name="other_metric",
            reason="另一笔",
        )

        self.assertFalse(second_outcome.decision.ok)
        self.assertEqual(second_outcome.decision.code, TARGET_HAS_PENDING_ACTION_CODE)
        rows = self.query(
            "SELECT count(*) FROM pending_action WHERE target_open_id = %s", (TARGET_OPEN_ID,)
        )
        self.assertEqual(rows[0][0], 1)

    def test_suspend_prepare_is_also_blocked_by_an_in_flight_grant_for_the_same_target(
        self,
    ) -> None:
        """交叉方向同理：一笔本地权限动作在途时，suspend 同样被同一条唯一索引
        挡住——证明这条索引按 ``target_open_id`` 泛化生效，不是"同类型才互斥"。
        """

        self.add_target_user(account_state="enabled")
        self.prepare_and_deliver_permission()

        outcome = self.store.prepare(
            action_type=PendingActionType.SUSPEND_USER,
            target_open_id=TARGET_OPEN_ID,
            initiated_by_open_id=ADMIN_OPEN_ID,
        )

        self.assertFalse(outcome.decision.ok)
        self.assertEqual(outcome.decision.code, TARGET_HAS_PENDING_ACTION_CODE)

    def test_prepare_rejects_when_key_already_has_an_active_override(self) -> None:
        """prepare 的基线观测查询在真库上生效：已经有一条生效覆盖时，
        对同一个键重新发起被 ``decide_prepare`` 拒绝（``target_state_changed``），
        且不产生新的 ``pending_action`` 行。"""

        self.add_target_user(account_state="enabled")
        first_pending_id = self.prepare_and_deliver_permission()
        confirmed = self.store.confirm(pending_action_id=first_pending_id, clicker_open_id=ADMIN_OPEN_ID)
        self.assertTrue(confirmed.decision.ok)

        second_outcome = self.store.prepare(
            action_type=PendingActionType.LOCAL_PERMISSION_GRANT,
            target_open_id=TARGET_OPEN_ID,
            initiated_by_open_id=ADMIN_OPEN_ID,
            company_id=self.COMPANY_ID,
            metric_name=self.METRIC_NAME,
            reason="重复授权",
        )

        self.assertFalse(second_outcome.decision.ok)
        self.assertEqual(second_outcome.decision.code, "target_state_changed")
        rows = self.query(
            "SELECT count(*) FROM pending_action WHERE target_open_id = %s", (TARGET_OPEN_ID,)
        )
        self.assertEqual(rows[0][0], 1, "被拒绝的第二次 prepare 不得插入任何新行")

    def test_prepare_allows_a_different_key_for_the_same_target_after_confirm(self) -> None:
        """观测查询按公司×指标键精确匹配，不是笼统地按用户拒绝一切：同一用户
        confirm 一个键之后，另一个键仍然可以正常发起。"""

        self.add_target_user(account_state="enabled")
        first_pending_id = self.prepare_and_deliver_permission()
        self.store.confirm(pending_action_id=first_pending_id, clicker_open_id=ADMIN_OPEN_ID)

        second_outcome = self.store.prepare(
            action_type=PendingActionType.LOCAL_PERMISSION_GRANT,
            target_open_id=TARGET_OPEN_ID,
            initiated_by_open_id=ADMIN_OPEN_ID,
            company_id="1099",
            metric_name="other_metric",
            reason="另一个指标",
        )

        self.assertTrue(second_outcome.decision.ok, second_outcome.decision.message)


class LocalPermissionRevokeRealDbTests(PendingActionPostgresTestCase):
    """本地权限收回全链路真库断言（迁移 ``0073``，#319 S-P-1b 卡 B）。

    覆盖设计卡登记的行为锚点：①revoke 全链路（grant 确认落行 → revoke prepare
    +confirm → entry_status=revoked → ``resolve_local_overrides`` 不再含该键）；
    ②不存在/已撤销 id → prepare 拒绝；③自我目标：属主==操作者 → 拒绝且不创建
    pending 行；④漂移：prepare 后另一路径先撤销该行 → confirm 落
    FAILED/target_drifted。变异锚点（登记后已还原，见任务收口说明）：删
    ``prepare()`` 里的自我目标判断会让
    ``test_self_target_revoke_is_rejected_without_creating_a_pending_row``
    变红；把 ``VALID_SOURCE_STATES[REVOKE]`` 改成 ``{"revoked"}`` 会让
    ``test_prepare_rejects_an_already_revoked_override`` 或
    ``test_full_revoke_lifecycle_marks_entry_revoked_and_drops_it_from_
    resolution`` 变红。
    """

    COMPANY_ID = "1011"
    METRIC_NAME = "daily_active"
    GRANT_REASON = "特批"
    REVOKE_REASON = "离职交接"

    def user_id_for(self, open_id: str = TARGET_OPEN_ID) -> str:
        rows = self.query("SELECT id FROM app_user WHERE feishu_open_id = %s", (open_id,))
        self.assertEqual(len(rows), 1)
        return rows[0][0]

    def grant_and_confirm(
        self, *, target_open_id: str = TARGET_OPEN_ID, initiated_by_open_id: str = ADMIN_OPEN_ID
    ) -> str:
        """建一笔已确认生效的本地授权，返回其 ``local_permission_override.id``
        （``lpo_*``）——本类几乎所有用例的共同前置：先要有一行"活的"覆盖才能
        测试收回。"""

        outcome = self.store.prepare(
            action_type=PendingActionType.LOCAL_PERMISSION_GRANT,
            target_open_id=target_open_id,
            initiated_by_open_id=initiated_by_open_id,
            company_id=self.COMPANY_ID,
            metric_name=self.METRIC_NAME,
            reason=self.GRANT_REASON,
        )
        self.assertTrue(outcome.decision.ok, outcome.decision.message)
        assert outcome.pending is not None
        self.store.mark_card_delivered(pending_action_id=outcome.pending.id, card_id="cardkit_grant")
        confirmed = self.store.confirm(
            pending_action_id=outcome.pending.id, clicker_open_id=ADMIN_OPEN_ID
        )
        self.assertTrue(confirmed.decision.ok)
        rows = self.query(
            "SELECT id FROM local_permission_override"
            " WHERE user_id = %s AND entry_status = 'active'",
            (self.user_id_for(target_open_id),),
        )
        self.assertEqual(len(rows), 1)
        return rows[0][0]

    def prepare_and_deliver_revoke(
        self, *, override_id: str, initiated_by_open_id: str = ADMIN_OPEN_ID, reason: str = REVOKE_REASON
    ):
        """建一条收回待确认操作并标记卡片已送达，返回完整 ``PrepareOutcome``
        （不像其余 ``prepare_and_deliver*`` 只返回 id——本类多个用例需要直接
        断言 ``outcome.pending.target_open_id``/``payload``）。"""

        outcome = self.store.prepare(
            action_type=PendingActionType.LOCAL_PERMISSION_REVOKE,
            target_open_id=override_id,
            initiated_by_open_id=initiated_by_open_id,
            reason=reason,
        )
        if outcome.decision.ok:
            assert outcome.pending is not None
            self.store.mark_card_delivered(
                pending_action_id=outcome.pending.id, card_id="cardkit_revoke"
            )
        return outcome

    def test_full_revoke_lifecycle_marks_entry_revoked_and_drops_it_from_resolution(self) -> None:
        """①全链路：grant 确认落行 → revoke prepare+confirm → entry_status=
        revoked → ``resolve_local_overrides`` 不再含该键（真正验证「收回」这个
        动作从聚合视角看确实生效，不只是数据库某一列翻转）。"""

        self.add_target_user(account_state="enabled")
        override_id = self.grant_and_confirm()
        user_id = self.user_id_for()

        prepare_outcome = self.prepare_and_deliver_revoke(override_id=override_id)
        self.assertTrue(prepare_outcome.decision.ok, prepare_outcome.decision.message)
        assert prepare_outcome.pending is not None
        # prepare() 已经把 override_id 联表查出真实属主 open_id，写回
        # PendingAction.target_open_id 供确认卡展示（卡 B 设计卡「同时得
        # target_open_id 供卡片展示」）。
        self.assertEqual(prepare_outcome.pending.target_open_id, TARGET_OPEN_ID)
        self.assertEqual(prepare_outcome.pending.target_state_snapshot, "active")
        payload = json.loads(prepare_outcome.pending.payload)
        self.assertEqual(payload["override_id"], override_id)
        self.assertEqual(payload["direction"], "grant")
        self.assertEqual(payload["company_id"], self.COMPANY_ID)
        self.assertEqual(payload["metric_name"], self.METRIC_NAME)
        self.assertEqual(payload["reason"], self.REVOKE_REASON)

        confirm_result = self.store.confirm(
            pending_action_id=prepare_outcome.pending.id, clicker_open_id=ADMIN_OPEN_ID
        )

        self.assertTrue(confirm_result.decision.ok, confirm_result.decision.message)
        assert confirm_result.pending is not None
        self.assertEqual(confirm_result.pending.status, PendingActionStatus.EXECUTED)
        row = self.query(
            "SELECT entry_status, revoked_at, revoked_pending_action_id"
            " FROM local_permission_override WHERE id = %s",
            (override_id,),
        )[0]
        self.assertEqual(row[0], "revoked")
        self.assertIsNotNone(row[1])
        self.assertEqual(row[2], prepare_outcome.pending.id)

        # 聚合视角：resolve_local_overrides 不再看到这个键。
        store = PostgresLocalPermissionOverrideStore(self._dsn)
        effective = store.effective_entries(user_id=user_id)
        resolved = resolve_local_overrides(
            user_id=user_id, entries=(stored.entry for stored in effective)
        )
        self.assertEqual(resolved.grants, frozenset())
        self.assertEqual(resolved.suppressions, frozenset())
        # 本地权限动作不改 account_state（suspend/resume 才改）。
        self.assertEqual(self.current_account_state(), "enabled")

    def test_prepare_rejects_a_never_existed_override_id(self) -> None:
        """②之一：override_id 从未存在过 → prepare 拒绝，不产生任何
        pending_action 行。"""

        self.add_target_user(account_state="enabled")
        never_existed = new_id("lpo")

        outcome = self.store.prepare(
            action_type=PendingActionType.LOCAL_PERMISSION_REVOKE,
            target_open_id=never_existed,
            initiated_by_open_id=ADMIN_OPEN_ID,
            reason=self.REVOKE_REASON,
        )

        self.assertFalse(outcome.decision.ok)
        self.assertEqual(self.query("SELECT count(*) FROM pending_action")[0][0], 0)

    def test_prepare_rejects_an_already_revoked_override(self) -> None:
        """②之二：override 已经处于 revoked 态 → prepare 拒绝，不产生任何新的
        pending_action 行（变异锚点：把 ``VALID_SOURCE_STATES[REVOKE]`` 改成
        ``{"revoked"}`` 会让本用例变红——那样"已撤销"反而会被判定为允许收回）。
        """

        self.add_target_user(account_state="enabled")
        override_id = self.grant_and_confirm()
        first_revoke = self.prepare_and_deliver_revoke(override_id=override_id)
        self.assertTrue(first_revoke.decision.ok)
        assert first_revoke.pending is not None
        first_confirm = self.store.confirm(
            pending_action_id=first_revoke.pending.id, clicker_open_id=ADMIN_OPEN_ID
        )
        self.assertTrue(first_confirm.decision.ok)
        pending_count_before = self.query("SELECT count(*) FROM pending_action")[0][0]

        second_outcome = self.store.prepare(
            action_type=PendingActionType.LOCAL_PERMISSION_REVOKE,
            target_open_id=override_id,
            initiated_by_open_id=ADMIN_OPEN_ID,
            reason="重复收回",
        )

        self.assertFalse(second_outcome.decision.ok)
        self.assertEqual(second_outcome.decision.code, "target_state_changed")
        self.assertEqual(
            self.query("SELECT count(*) FROM pending_action")[0][0], pending_count_before
        )

    def test_self_target_revoke_is_rejected_without_creating_a_pending_row(self) -> None:
        """③自我目标：override 的属主 open_id 与发起收回的管理员 open_id 相同
        → prepare 拒绝，且不创建任何 pending_action 行（变异锚点：删掉
        ``prepare()`` 里的自我目标判断会让本用例变红）。这里用管理员自己给
        自己发起过一笔授权（结构上可能不该发生，但核对逻辑本身不能依赖这个
        前提——同 router 层「查到属主之后再核对」的既有姿态）来构造这个场景。
        """

        self.add_target_user(open_id=ADMIN_OPEN_ID, account_state="enabled")
        override_id = self.grant_and_confirm(target_open_id=ADMIN_OPEN_ID)

        outcome = self.store.prepare(
            action_type=PendingActionType.LOCAL_PERMISSION_REVOKE,
            target_open_id=override_id,
            initiated_by_open_id=ADMIN_OPEN_ID,
            reason=self.REVOKE_REASON,
        )

        self.assertFalse(outcome.decision.ok)
        self.assertEqual(outcome.decision.code, "self_target_forbidden")
        self.assertEqual(self.query("SELECT count(*) FROM pending_action")[0][0], 1, "只有 grant 那一条")

    def test_confirm_downgrades_to_target_drifted_when_another_path_revokes_first(self) -> None:
        """④漂移：prepare 收回后，另一条路径（真实场景例如另一次并发收回，本
        用例直接调用 ``PostgresLocalPermissionOverrideStore.revoke`` 模拟）先
        把这一行标记 revoked → confirm 落 FAILED/target_drifted，且该行最终
        仍然是 revoked（不因为我们这次 confirm 的失败而"复活"）。"""

        self.add_target_user(account_state="enabled")
        override_id = self.grant_and_confirm()
        prepare_outcome = self.prepare_and_deliver_revoke(override_id=override_id)
        self.assertTrue(prepare_outcome.decision.ok)
        assert prepare_outcome.pending is not None

        bystander_pending_id = new_id("pac")
        self.execute(
            """INSERT INTO pending_action
                   (id, action_type, target_open_id, target_state_snapshot,
                    initiated_by_open_id, status, confirm_deadline_at,
                    decided_at, decided_by_open_id)
                 VALUES (%s, 'suspend_user', %s, 'enabled', %s, 'executed', %s, %s, %s)""",
            (
                bystander_pending_id,
                f"ou_bystander_for_{bystander_pending_id}",
                ADMIN_OPEN_ID,
                datetime.now(timezone.utc) + timedelta(minutes=10),
                datetime.now(timezone.utc),
                ADMIN_OPEN_ID,
            ),
        )
        other_store = PostgresLocalPermissionOverrideStore(self._dsn)
        changed = other_store.revoke(
            override_id=override_id, revoked_pending_action_id=bystander_pending_id
        )
        self.assertTrue(changed, "抢先收回本身必须成功，用例前提")

        result = self.store.confirm(
            pending_action_id=prepare_outcome.pending.id, clicker_open_id=ADMIN_OPEN_ID
        )

        self.assertFalse(result.decision.ok)
        self.assertIs(result.decision.kind, ConfirmResultKind.TARGET_DRIFTED)
        assert result.pending is not None
        self.assertEqual(result.pending.status, PendingActionStatus.FAILED)
        self.assertEqual(result.pending.reason, "target_drifted")
        # 我们这次 confirm 失败不改变已经被抢先写下的收回记录。
        row = self.query(
            "SELECT entry_status, revoked_pending_action_id"
            " FROM local_permission_override WHERE id = %s",
            (override_id,),
        )[0]
        self.assertEqual(row[0], "revoked")
        self.assertEqual(row[1], bystander_pending_id)

    def test_second_revoke_prepare_for_a_target_already_in_flight_is_rejected(self) -> None:
        """同目标在途互斥同样覆盖收回：迁移 ``0068`` 的
        ``pending_action_single_pending_target_idx`` 按 ``target_open_id``（这里
        是收回后真实解析出的属主 open_id）分区，不区分 ``action_type``。"""

        self.add_target_user(account_state="enabled")
        override_id = self.grant_and_confirm()
        first = self.prepare_and_deliver_revoke(override_id=override_id)
        self.assertTrue(first.decision.ok)

        second = self.store.prepare(
            action_type=PendingActionType.SUSPEND_USER,
            target_open_id=TARGET_OPEN_ID,
            initiated_by_open_id=ADMIN_OPEN_ID,
        )

        self.assertFalse(second.decision.ok)
        self.assertEqual(second.decision.code, TARGET_HAS_PENDING_ACTION_CODE)


class PayloadActionTypeConsistencyRealDbTests(PendingActionPostgresTestCase):
    """迁移 ``0073`` 的自洽 CHECK 在真库上生效：本地权限三类动作必须携带非空白
    ``payload``，``suspend_user``/``resume_user`` 必须不携带——否定断言，直接
    对表发裸 SQL，不经过应用层校验（应用层本身不会拼出这种行，但数据库约束
    必须独立成立，不依赖调用方自觉）。"""

    def test_permission_action_type_without_payload_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            self.execute(
                """INSERT INTO pending_action
                       (id, action_type, target_open_id, target_state_snapshot,
                        initiated_by_open_id, confirm_deadline_at)
                     VALUES (%s, 'local_permission_grant', %s, 'absent', %s,
                             now() + interval '10 minutes')""",
                (new_id("pac"), TARGET_OPEN_ID, ADMIN_OPEN_ID),
            )

    def test_suspend_action_type_with_payload_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            self.execute(
                """INSERT INTO pending_action
                       (id, action_type, target_open_id, target_state_snapshot,
                        initiated_by_open_id, confirm_deadline_at, payload)
                     VALUES (%s, 'suspend_user', %s, 'enabled', %s,
                             now() + interval '10 minutes', '{"company_id": "1011"}')""",
                (new_id("pac"), TARGET_OPEN_ID, ADMIN_OPEN_ID),
            )

    def test_permission_action_type_with_blank_payload_is_rejected(self) -> None:
        """``payload`` 非 NULL 但整段都是空白字符——``NULLIF(BTRIM(...), '')``
        把它当成"没有 payload"，与 suspend/resume 用的空白校验同一姿态。"""

        with self.assertRaises(Exception):
            self.execute(
                """INSERT INTO pending_action
                       (id, action_type, target_open_id, target_state_snapshot,
                        initiated_by_open_id, confirm_deadline_at, payload)
                     VALUES (%s, 'local_permission_suppress', %s, 'absent', %s,
                             now() + interval '10 minutes', '   ')""",
                (new_id("pac"), TARGET_OPEN_ID, ADMIN_OPEN_ID),
            )

    def test_local_permission_revoke_is_a_legal_action_type_value(self) -> None:
        """迁移 ``0073`` 的 ``action_type`` CHECK 一次性扩到全部五值（文件头部
        「为什么 revoke 取值本次一并加入」）：``local_permission_revoke`` 本身
        是合法取值，即使卡 B 才会真正执行它。"""

        self.execute(
            """INSERT INTO pending_action
                   (id, action_type, target_open_id, target_state_snapshot,
                    initiated_by_open_id, confirm_deadline_at, payload)
                 VALUES (%s, 'local_permission_revoke', %s, 'present', %s,
                         now() + interval '10 minutes', '{"company_id": "1011", "metric_name": "m", "reason": "r"}')""",
            (new_id("pac"), TARGET_OPEN_ID, ADMIN_OPEN_ID),
        )
        rows = self.query(
            "SELECT action_type FROM pending_action WHERE action_type = 'local_permission_revoke'"
        )
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
