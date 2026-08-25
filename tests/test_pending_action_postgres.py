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
import time
import unittest
from datetime import datetime, timedelta, timezone

from postgres_schema import ensure_production_schema, reset_production_rows

from lingxi.adapters.admin_registry import seed_admin_registry_entry
from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_pending_action import (
    TARGET_HAS_PENDING_ACTION_CODE,
    PendingActionAuditWriteFailed,
    PostgresPendingActionStore,
)
from lingxi.core.admin.pending_action import (
    ConfirmResultKind,
    PendingActionStatus,
    PendingActionType,
)
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
