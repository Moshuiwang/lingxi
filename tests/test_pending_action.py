"""``core/admin/pending_action.py`` 的纯逻辑断言（Issue #96 S-M-02）。

覆盖 ``decide_prepare``/``decide_confirm``/``decide_cancel`` 的全部分支，含验证与
门禁 §八要求的否定断言：非发起人点击、重复点击、过期、角色被收回、目标状态漂移、
伪造/未送达的待确认操作 ID。真库层面的事务性质（并发确认只成功一次、审计失败整体
回滚）见 ``tests/test_pending_action_postgres.py``。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from lingxi.core.admin.pending_action import (
    PENDING_ACTION_TTL_SECONDS,
    REQUIRED_ROLE,
    TARGET_ACCOUNT_STATE,
    VALID_SOURCE_STATES,
    CancelResultKind,
    ConfirmResultKind,
    PendingAction,
    PendingActionStatus,
    PendingActionType,
    decide_cancel,
    decide_confirm,
    decide_prepare,
)
from lingxi.core.admin.registry import ALL_ADMIN_ROLES, AdminRegistryEntry, AdminRole

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)
INITIATOR = "ou_admin_initiator"
OTHER_OPEN_ID = "ou_not_the_admin"
TARGET_OPEN_ID = "ou_target_user"


def _pending(
    *,
    action_type: PendingActionType = PendingActionType.SUSPEND_USER,
    status: PendingActionStatus = PendingActionStatus.PENDING,
    card_delivered: bool = True,
    target_state_snapshot: str = "enabled",
    initiated_by_open_id: str = INITIATOR,
    confirm_deadline_at: datetime = NOW + timedelta(seconds=PENDING_ACTION_TTL_SECONDS),
    decided_at: datetime | None = None,
    reason: str | None = None,
) -> PendingAction:
    return PendingAction(
        id="pac_test0000000000000000000",
        action_type=action_type,
        target_open_id=TARGET_OPEN_ID,
        target_state_snapshot=target_state_snapshot,
        initiated_by_open_id=initiated_by_open_id,
        status=status,
        card_delivered=card_delivered,
        card_id="cardkit_test_id",
        reason=reason,
        created_at=NOW - timedelta(seconds=5),
        confirm_deadline_at=confirm_deadline_at,
        decided_at=decided_at,
        decided_by_open_id=None,
    )


def _full_admin_entry(*, open_id: str = INITIATOR, active: bool = True) -> AdminRegistryEntry:
    return AdminRegistryEntry(
        feishu_open_id=open_id,
        label="delegated_subject",
        roles=ALL_ADMIN_ROLES if active else frozenset(),
        entry_status="active" if active else "revoked",
    )


class DecidePrepareTests(unittest.TestCase):
    def test_suspend_allowed_from_enabled(self) -> None:
        decision = decide_prepare(
            action_type=PendingActionType.SUSPEND_USER, current_account_state="enabled"
        )
        self.assertTrue(decision.ok)

    def test_resume_allowed_from_suspended(self) -> None:
        decision = decide_prepare(
            action_type=PendingActionType.RESUME_USER, current_account_state="suspended"
        )
        self.assertTrue(decision.ok)

    def test_rejects_unknown_target(self) -> None:
        decision = decide_prepare(
            action_type=PendingActionType.SUSPEND_USER, current_account_state=None
        )
        self.assertFalse(decision.ok)
        self.assertEqual(decision.code, "not_found")

    def test_suspend_rejected_when_already_suspended(self) -> None:
        decision = decide_prepare(
            action_type=PendingActionType.SUSPEND_USER, current_account_state="suspended"
        )
        self.assertFalse(decision.ok)
        self.assertEqual(decision.code, "target_state_changed")

    def test_resume_rejected_when_already_enabled(self) -> None:
        decision = decide_prepare(
            action_type=PendingActionType.RESUME_USER, current_account_state="enabled"
        )
        self.assertFalse(decision.ok)
        self.assertEqual(decision.code, "target_state_changed")

    def test_suspend_rejected_for_deleting_or_deleted(self) -> None:
        for state in ("deleting", "deleted"):
            with self.subTest(state=state):
                decision = decide_prepare(
                    action_type=PendingActionType.SUSPEND_USER, current_account_state=state
                )
                self.assertFalse(decision.ok)

    def test_resume_rejected_for_deleting_or_deleted(self) -> None:
        for state in ("deleting", "deleted"):
            with self.subTest(state=state):
                decision = decide_prepare(
                    action_type=PendingActionType.RESUME_USER, current_account_state=state
                )
                self.assertFalse(decision.ok)


class DecidePrepareLocalPermissionTests(unittest.TestCase):
    """#319 S-P-1b：本地权限授权/抑制复用同一个 ``decide_prepare``，基线语义是
    该 ``(user, direction, company, metric)`` 键当前"无生效行"（``"absent"``）。
    ``adapters/postgres_pending_action.py`` 负责把这个字符串观测出来，本模块
    只验证纯函数对这两个新取值的判定本身。
    """

    def test_grant_allowed_when_key_is_absent(self) -> None:
        decision = decide_prepare(
            action_type=PendingActionType.LOCAL_PERMISSION_GRANT, current_account_state="absent"
        )
        self.assertTrue(decision.ok)

    def test_suppress_allowed_when_key_is_absent(self) -> None:
        decision = decide_prepare(
            action_type=PendingActionType.LOCAL_PERMISSION_SUPPRESS, current_account_state="absent"
        )
        self.assertTrue(decision.ok)

    def test_grant_rejected_when_key_already_present(self) -> None:
        decision = decide_prepare(
            action_type=PendingActionType.LOCAL_PERMISSION_GRANT, current_account_state="present"
        )
        self.assertFalse(decision.ok)
        self.assertEqual(decision.code, "target_state_changed")

    def test_suppress_rejected_when_key_already_present(self) -> None:
        decision = decide_prepare(
            action_type=PendingActionType.LOCAL_PERMISSION_SUPPRESS, current_account_state="present"
        )
        self.assertFalse(decision.ok)
        self.assertEqual(decision.code, "target_state_changed")

    def test_grant_rejects_unknown_target_same_as_suspend_resume(self) -> None:
        decision = decide_prepare(
            action_type=PendingActionType.LOCAL_PERMISSION_GRANT, current_account_state=None
        )
        self.assertFalse(decision.ok)
        self.assertEqual(decision.code, "not_found")

    def test_grant_rejection_message_does_not_mention_resume(self) -> None:
        """拒绝文案按类型分化（Trace #328 opus 审查升级的 P2）：此前本地权限三类
        动作全部落进"suspend 之外都当 resume 处理"的 ``else`` 分支，字面导向
        "请去 /admin resume"——对一个授权命令毫无意义，是一条真实的误导面。"""

        decision = decide_prepare(
            action_type=PendingActionType.LOCAL_PERMISSION_GRANT, current_account_state="present"
        )
        self.assertNotIn("恢复", decision.message)
        self.assertIn("本地权限", decision.message)

    def test_suppress_rejection_message_does_not_mention_resume(self) -> None:
        decision = decide_prepare(
            action_type=PendingActionType.LOCAL_PERMISSION_SUPPRESS, current_account_state="present"
        )
        self.assertNotIn("恢复", decision.message)
        self.assertIn("本地权限", decision.message)


class DecidePrepareRevokeTests(unittest.TestCase):
    """卡 B 新增：``LOCAL_PERMISSION_REVOKE`` 的基线语义与 grant/suppress 相反
    ——要求覆盖行当前 ``entry_status`` 必须是"active"才允许发起收回。
    ``adapters/postgres_pending_action.py`` 负责按 override_id 查出这个字符串，
    本模块只验证纯函数对这个新取值的判定本身（同 grant/suppress 的既有分工）。
    """

    def test_revoke_allowed_when_entry_is_active(self) -> None:
        decision = decide_prepare(
            action_type=PendingActionType.LOCAL_PERMISSION_REVOKE, current_account_state="active"
        )
        self.assertTrue(decision.ok)

    def test_revoke_rejected_when_entry_already_revoked(self) -> None:
        decision = decide_prepare(
            action_type=PendingActionType.LOCAL_PERMISSION_REVOKE, current_account_state="revoked"
        )
        self.assertFalse(decision.ok)
        self.assertEqual(decision.code, "target_state_changed")

    def test_revoke_rejects_unknown_override_id_same_as_suspend_resume(self) -> None:
        decision = decide_prepare(
            action_type=PendingActionType.LOCAL_PERMISSION_REVOKE, current_account_state=None
        )
        self.assertFalse(decision.ok)
        self.assertEqual(decision.code, "not_found")

    def test_revoke_not_found_message_talks_about_the_override_not_a_user(self) -> None:
        """拒绝文案按类型分化：收回场景下 ``current_account_state is None`` 说的是
        "没查到这一条本地权限登记"——``target_open_id`` 形参此刻装的是
        override_id，不是任何人的身份标识，通用的"未找到该用户记录"说错了对象。"""

        decision = decide_prepare(
            action_type=PendingActionType.LOCAL_PERMISSION_REVOKE, current_account_state=None
        )
        self.assertNotIn("用户", decision.message)
        self.assertIn("本地权限", decision.message)

    def test_revoke_rejection_message_does_not_mention_resume(self) -> None:
        decision = decide_prepare(
            action_type=PendingActionType.LOCAL_PERMISSION_REVOKE, current_account_state="revoked"
        )
        self.assertNotIn("恢复", decision.message)
        self.assertIn("本地权限", decision.message)


class SuspendResumeMappingsUnchangedTests(unittest.TestCase):
    """哨兵测试：泛化 ``PendingActionType``/``VALID_SOURCE_STATES``/
    ``REQUIRED_ROLE``/``TARGET_ACCOUNT_STATE`` 以承载本地权限新动作类型时，
    ``SUSPEND_USER``/``RESUME_USER``/既有的 grant/suppress 取值必须逐项相等、
    一个字符都不变——这是"新类型加法式扩展、旧类型零改动"这条设计约束的可执行
    证据（#319 S-P-1b 设计卡；卡 B 沿用同一纪律扩展 revoke 项）。"""

    def test_valid_source_states_unchanged_for_suspend_and_resume(self) -> None:
        self.assertEqual(VALID_SOURCE_STATES[PendingActionType.SUSPEND_USER], frozenset({"enabled"}))
        self.assertEqual(VALID_SOURCE_STATES[PendingActionType.RESUME_USER], frozenset({"suspended"}))

    def test_required_role_unchanged_for_suspend_and_resume(self) -> None:
        self.assertEqual(REQUIRED_ROLE[PendingActionType.SUSPEND_USER], AdminRole.PERMISSION_ADMIN)
        self.assertEqual(REQUIRED_ROLE[PendingActionType.RESUME_USER], AdminRole.PERMISSION_ADMIN)

    def test_target_account_state_unchanged_for_suspend_and_resume(self) -> None:
        self.assertEqual(TARGET_ACCOUNT_STATE[PendingActionType.SUSPEND_USER], "suspended")
        self.assertEqual(TARGET_ACCOUNT_STATE[PendingActionType.RESUME_USER], "enabled")

    def test_local_permission_action_types_map_to_no_account_state_change(self) -> None:
        """新类型的 ``TARGET_ACCOUNT_STATE`` 必须是 ``None``（不改
        ``app_user.account_state``），这是 confirm 侧选择"更新 app_user 还是写
        local_permission_override"分支判断所依赖的不变量。"""

        self.assertIsNone(TARGET_ACCOUNT_STATE[PendingActionType.LOCAL_PERMISSION_GRANT])
        self.assertIsNone(TARGET_ACCOUNT_STATE[PendingActionType.LOCAL_PERMISSION_SUPPRESS])

    def test_grant_and_suppress_mappings_unchanged_by_card_b(self) -> None:
        """卡 B 只新增 revoke 项，不改卡 A 已登记的 grant/suppress 取值——逐值
        断言，抓"改了不该改的既有取值"这类变异。"""

        self.assertEqual(VALID_SOURCE_STATES[PendingActionType.LOCAL_PERMISSION_GRANT], frozenset({"absent"}))
        self.assertEqual(
            VALID_SOURCE_STATES[PendingActionType.LOCAL_PERMISSION_SUPPRESS], frozenset({"absent"})
        )
        self.assertEqual(
            REQUIRED_ROLE[PendingActionType.LOCAL_PERMISSION_GRANT], AdminRole.PERMISSION_ADMIN
        )
        self.assertEqual(
            REQUIRED_ROLE[PendingActionType.LOCAL_PERMISSION_SUPPRESS], AdminRole.PERMISSION_ADMIN
        )

    def test_revoke_is_now_registered_with_active_source_state_and_permission_admin_role(
        self,
    ) -> None:
        """卡 B 新增：``LOCAL_PERMISSION_REVOKE`` 的基线语义与 grant/suppress
        相反——要求当前必须"active"才允许发起收回。"""

        self.assertEqual(
            VALID_SOURCE_STATES[PendingActionType.LOCAL_PERMISSION_REVOKE], frozenset({"active"})
        )
        self.assertEqual(
            REQUIRED_ROLE[PendingActionType.LOCAL_PERMISSION_REVOKE], AdminRole.PERMISSION_ADMIN
        )
        self.assertIsNone(TARGET_ACCOUNT_STATE[PendingActionType.LOCAL_PERMISSION_REVOKE])


class DecideConfirmHappyPathTests(unittest.TestCase):
    def test_suspend_executes_when_every_check_passes(self) -> None:
        pending = _pending(action_type=PendingActionType.SUSPEND_USER, target_state_snapshot="enabled")
        decision = decide_confirm(
            pending=pending,
            clicker_open_id=INITIATOR,
            now=NOW,
            registry_entry=_full_admin_entry(),
            current_account_state="enabled",
        )
        self.assertTrue(decision.ok)
        self.assertIs(decision.kind, ConfirmResultKind.EXECUTE)
        self.assertEqual(decision.new_account_state, "suspended")
        self.assertIs(decision.terminal_status, PendingActionStatus.EXECUTED)

    def test_resume_executes_when_every_check_passes(self) -> None:
        pending = _pending(
            action_type=PendingActionType.RESUME_USER, target_state_snapshot="suspended"
        )
        decision = decide_confirm(
            pending=pending,
            clicker_open_id=INITIATOR,
            now=NOW,
            registry_entry=_full_admin_entry(),
            current_account_state="suspended",
        )
        self.assertTrue(decision.ok)
        self.assertEqual(decision.new_account_state, "enabled")

    def test_revoke_executes_when_every_check_passes(self) -> None:
        """卡 B 新增：``LOCAL_PERMISSION_REVOKE`` 走完全相同的核对链——真正的
        写入分支（条件 UPDATE local_permission_override）在
        ``adapters/postgres_pending_action.py``，本函数只判定"是否应该
        EXECUTE"，不关心具体写哪张表（``new_account_state`` 恒为 ``None``，
        与 grant/suppress 同一姿态）。"""

        pending = _pending(
            action_type=PendingActionType.LOCAL_PERMISSION_REVOKE, target_state_snapshot="active"
        )
        decision = decide_confirm(
            pending=pending,
            clicker_open_id=INITIATOR,
            now=NOW,
            registry_entry=_full_admin_entry(),
            current_account_state="active",
        )
        self.assertTrue(decision.ok)
        self.assertIsNone(decision.new_account_state)
        self.assertIs(decision.terminal_status, PendingActionStatus.EXECUTED)


class DecideConfirmNotFoundTests(unittest.TestCase):
    """否定断言：卡片回调伪造（不存在的动作 ID / 从未送达）→拒绝，且与"未找到"
    不可区分（不给伪造者可利用的信号）。"""

    def test_none_pending_is_not_found(self) -> None:
        decision = decide_confirm(
            pending=None,
            clicker_open_id=INITIATOR,
            now=NOW,
            registry_entry=_full_admin_entry(),
            current_account_state="enabled",
        )
        self.assertFalse(decision.ok)
        self.assertIs(decision.kind, ConfirmResultKind.NOT_FOUND)
        self.assertEqual(decision.code, "not_found")
        self.assertIsNone(decision.terminal_status, "未找到时不产生任何状态写入")

    def test_undelivered_card_is_not_found_even_though_row_exists(self) -> None:
        """``card_delivered=False`` 视为作废：即使这一行真的存在于数据库，确认时
        的判定结果也必须与"未找到"完全一致（迁移 0068 文件头部的既定语义）。"""

        pending = _pending(card_delivered=False)
        decision = decide_confirm(
            pending=pending,
            clicker_open_id=INITIATOR,
            now=NOW,
            registry_entry=_full_admin_entry(),
            current_account_state="enabled",
        )
        self.assertIs(decision.kind, ConfirmResultKind.NOT_FOUND)
        self.assertIsNone(decision.terminal_status)


class DecideConfirmAlreadyTerminalTests(unittest.TestCase):
    """否定断言：重复点击/重复回调/重试 → 只执行一次，幂等返回既有结果。"""

    def test_executed_action_is_not_re_executed(self) -> None:
        pending = _pending(status=PendingActionStatus.EXECUTED, decided_at=NOW - timedelta(minutes=1))
        decision = decide_confirm(
            pending=pending,
            clicker_open_id=INITIATOR,
            now=NOW,
            registry_entry=_full_admin_entry(),
            current_account_state="suspended",
        )
        self.assertFalse(decision.ok)
        self.assertIs(decision.kind, ConfirmResultKind.ALREADY_TERMINAL)
        self.assertEqual(decision.code, "already_executed")
        self.assertIsNone(decision.terminal_status, "已是终态，不产生第二次状态写入")

    def test_cancelled_and_expired_and_failed_are_also_already_terminal(self) -> None:
        for status in (
            PendingActionStatus.CANCELLED,
            PendingActionStatus.EXPIRED,
            PendingActionStatus.FAILED,
        ):
            with self.subTest(status=status):
                pending = _pending(status=status, decided_at=NOW - timedelta(minutes=1))
                decision = decide_confirm(
                    pending=pending,
                    clicker_open_id=INITIATOR,
                    now=NOW,
                    registry_entry=_full_admin_entry(),
                    current_account_state="enabled",
                )
                self.assertIs(decision.kind, ConfirmResultKind.ALREADY_TERMINAL)


class DecideConfirmExpiryTests(unittest.TestCase):
    """否定断言：过期后确认 → 不执行，转终态 EXPIRED。"""

    def test_expired_pending_action_is_rejected_and_transitions_to_expired(self) -> None:
        pending = _pending(confirm_deadline_at=NOW - timedelta(seconds=1))
        decision = decide_confirm(
            pending=pending,
            clicker_open_id=INITIATOR,
            now=NOW,
            registry_entry=_full_admin_entry(),
            current_account_state="enabled",
        )
        self.assertFalse(decision.ok)
        self.assertIs(decision.kind, ConfirmResultKind.EXPIRE)
        self.assertEqual(decision.code, "action_expired")
        self.assertIs(decision.terminal_status, PendingActionStatus.EXPIRED)

    def test_expiry_boundary_is_inclusive(self) -> None:
        """``confirm_deadline_at`` 恰好等于 ``now`` 时按已过期处理（``<=`` 而不是 ``<``）——
        有效期是一个确定性的边界，不留出"恰好这一秒还能点"的模糊窗口。"""

        pending = _pending(confirm_deadline_at=NOW)
        decision = decide_confirm(
            pending=pending,
            clicker_open_id=INITIATOR,
            now=NOW,
            registry_entry=_full_admin_entry(),
            current_account_state="enabled",
        )
        self.assertIs(decision.kind, ConfirmResultKind.EXPIRE)

    def test_expiry_is_checked_before_clicker_identity(self) -> None:
        """一个过期的操作即使被错误的人点击，结论也是"已过期"而不是"非本人"——
        这是刻意的核对顺序（见 ``decide_confirm`` 文档），本用例把顺序钉死。"""

        pending = _pending(confirm_deadline_at=NOW - timedelta(seconds=1))
        decision = decide_confirm(
            pending=pending,
            clicker_open_id=OTHER_OPEN_ID,
            now=NOW,
            registry_entry=_full_admin_entry(open_id=OTHER_OPEN_ID),
            current_account_state="enabled",
        )
        self.assertIs(decision.kind, ConfirmResultKind.EXPIRE)


class DecideConfirmNotInitiatorTests(unittest.TestCase):
    """否定断言：非本人 open_id 点击 → 拒绝且零业务变更（不产生任何状态写入，
    真正的发起人随后仍可能点对）。"""

    def test_wrong_clicker_is_rejected_without_any_state_change(self) -> None:
        pending = _pending()
        decision = decide_confirm(
            pending=pending,
            clicker_open_id=OTHER_OPEN_ID,
            now=NOW,
            registry_entry=_full_admin_entry(open_id=OTHER_OPEN_ID),
            current_account_state="enabled",
        )
        self.assertFalse(decision.ok)
        self.assertIs(decision.kind, ConfirmResultKind.NOT_INITIATOR)
        self.assertEqual(decision.code, "not_authorized")
        self.assertIsNone(
            decision.terminal_status, "非本人点击不得改变 pending_action 的任何字段"
        )
        self.assertIsNone(decision.new_account_state)

    def test_wrong_clicker_is_rejected_even_with_full_admin_roles(self) -> None:
        """即使点击者本身也持有完整管理员角色（结构上不该发生——MVP 只有唯一
        管理员——但核对逻辑本身不能依赖这个前提），仍然必须拒绝：本人核对与角色
        核对是两个独立的关卡，任一失败都不放行。"""

        pending = _pending()
        decision = decide_confirm(
            pending=pending,
            clicker_open_id=OTHER_OPEN_ID,
            now=NOW,
            registry_entry=_full_admin_entry(open_id=OTHER_OPEN_ID, active=True),
            current_account_state="enabled",
        )
        self.assertIs(decision.kind, ConfirmResultKind.NOT_INITIATOR)


class DecideConfirmRoleRevokedTests(unittest.TestCase):
    """否定断言：prepare 与 confirm 之间角色被撤销 → 拒绝，要求重新查询发起。"""

    def test_missing_registry_entry_is_rejected(self) -> None:
        pending = _pending()
        decision = decide_confirm(
            pending=pending,
            clicker_open_id=INITIATOR,
            now=NOW,
            registry_entry=None,
            current_account_state="enabled",
        )
        self.assertFalse(decision.ok)
        self.assertIs(decision.kind, ConfirmResultKind.ROLE_REVOKED)
        self.assertEqual(decision.code, "not_authorized")
        self.assertIs(decision.terminal_status, PendingActionStatus.FAILED)
        self.assertEqual(decision.reason, "role_revoked")

    def test_revoked_registry_entry_is_rejected(self) -> None:
        pending = _pending()
        decision = decide_confirm(
            pending=pending,
            clicker_open_id=INITIATOR,
            now=NOW,
            registry_entry=_full_admin_entry(active=False),
            current_account_state="enabled",
        )
        self.assertIs(decision.kind, ConfirmResultKind.ROLE_REVOKED)

    def test_entry_missing_required_role_is_rejected(self) -> None:
        """结构上当前不会发生（MVP 三类角色合并授予），但核对逻辑本身必须独立
        成立——见 ``pending_action.py`` 的 ``REQUIRED_ROLE`` 文档。"""

        partial_entry = AdminRegistryEntry(
            feishu_open_id=INITIATOR,
            label="future-admin",
            roles=frozenset({AdminRole.OPS_ADMIN, AdminRole.SUPER_ADMIN}),
            entry_status="active",
        )
        pending = _pending()
        decision = decide_confirm(
            pending=pending,
            clicker_open_id=INITIATOR,
            now=NOW,
            registry_entry=partial_entry,
            current_account_state="enabled",
        )
        self.assertIs(decision.kind, ConfirmResultKind.ROLE_REVOKED)


class DecideConfirmTargetDriftTests(unittest.TestCase):
    """否定断言：目标状态漂移（如确认前用户已被另一路径停用）→ 拒绝并提示重新
    发起。"""

    def test_drifted_target_state_is_rejected(self) -> None:
        pending = _pending(target_state_snapshot="enabled")
        decision = decide_confirm(
            pending=pending,
            clicker_open_id=INITIATOR,
            now=NOW,
            registry_entry=_full_admin_entry(),
            current_account_state="suspended",  # 已经被别的路径改变
        )
        self.assertFalse(decision.ok)
        self.assertIs(decision.kind, ConfirmResultKind.TARGET_DRIFTED)
        self.assertEqual(decision.code, "target_state_changed")
        self.assertIs(decision.terminal_status, PendingActionStatus.FAILED)
        self.assertEqual(decision.reason, "target_drifted")

    def test_target_missing_entirely_counts_as_drift(self) -> None:
        pending = _pending(target_state_snapshot="enabled")
        decision = decide_confirm(
            pending=pending,
            clicker_open_id=INITIATOR,
            now=NOW,
            registry_entry=_full_admin_entry(),
            current_account_state=None,
        )
        self.assertIs(decision.kind, ConfirmResultKind.TARGET_DRIFTED)


class ConfirmResultKindDistinctionTests(unittest.TestCase):
    """守住一个真实存在过的设计陷阱：``NOT_INITIATOR`` 与 ``ROLE_REVOKED`` 在
    接口设计的统一错误码表里映射到同一个 ``not_authorized``，但两者在
    ``decide_confirm`` 里必须走**不同**的分支（前者不改变 ``pending_action`` 任何
    字段，后者转终态 ``FAILED``）。如果不小心让这两个 ``Enum`` 成员取相同的字符串
    取值，Python 会把第二个悄悄合并成第一个的别名，两个分支就会被 ``is``/``==``
    判成"同一件事"——这正是本 Story 交付中主动识别并避免的一类缺陷，用一条显式
    断言钉住，防止未来的改动无意中重新引入。"""

    def test_role_revoked_and_not_initiator_are_distinct_enum_members(self) -> None:
        self.assertIsNot(ConfirmResultKind.ROLE_REVOKED, ConfirmResultKind.NOT_INITIATOR)
        self.assertNotEqual(ConfirmResultKind.ROLE_REVOKED.value, ConfirmResultKind.NOT_INITIATOR.value)

    def test_both_still_report_the_same_wire_error_code(self) -> None:
        role_revoked = decide_confirm(
            pending=_pending(),
            clicker_open_id=INITIATOR,
            now=NOW,
            registry_entry=None,
            current_account_state="enabled",
        )
        not_initiator = decide_confirm(
            pending=_pending(),
            clicker_open_id=OTHER_OPEN_ID,
            now=NOW,
            registry_entry=_full_admin_entry(open_id=OTHER_OPEN_ID),
            current_account_state="enabled",
        )
        self.assertEqual(role_revoked.code, "not_authorized")
        self.assertEqual(not_initiator.code, "not_authorized")
        # 但两者在 adapter 应当采取的动作上必须不同：见各自的 terminal_status。
        self.assertIsNotNone(role_revoked.terminal_status)
        self.assertIsNone(not_initiator.terminal_status)


class DecideCancelTests(unittest.TestCase):
    def test_cancel_succeeds_on_a_pending_action(self) -> None:
        pending = _pending()
        decision = decide_cancel(pending=pending, clicker_open_id=INITIATOR, now=NOW)
        self.assertTrue(decision.ok)
        self.assertIs(decision.kind, CancelResultKind.CANCEL)
        self.assertIs(decision.terminal_status, PendingActionStatus.CANCELLED)
        self.assertEqual(decision.reason, "cancelled_by_admin")

    def test_cancel_rejects_forged_or_undelivered_action(self) -> None:
        for pending in (None, _pending(card_delivered=False)):
            with self.subTest(pending=pending):
                decision = decide_cancel(pending=pending, clicker_open_id=INITIATOR, now=NOW)
                self.assertIs(decision.kind, CancelResultKind.NOT_FOUND)

    def test_cancel_is_idempotent_on_already_terminal_action(self) -> None:
        pending = _pending(status=PendingActionStatus.EXECUTED, decided_at=NOW - timedelta(minutes=1))
        decision = decide_cancel(pending=pending, clicker_open_id=INITIATOR, now=NOW)
        self.assertIs(decision.kind, CancelResultKind.ALREADY_TERMINAL)
        self.assertIsNone(decision.terminal_status)

    def test_cancel_rejects_expired_action(self) -> None:
        pending = _pending(confirm_deadline_at=NOW - timedelta(seconds=1))
        decision = decide_cancel(pending=pending, clicker_open_id=INITIATOR, now=NOW)
        self.assertIs(decision.kind, CancelResultKind.EXPIRE)
        self.assertIs(decision.terminal_status, PendingActionStatus.EXPIRED)

    def test_cancel_rejects_wrong_clicker_without_any_state_change(self) -> None:
        pending = _pending()
        decision = decide_cancel(pending=pending, clicker_open_id=OTHER_OPEN_ID, now=NOW)
        self.assertIs(decision.kind, CancelResultKind.NOT_INITIATOR)
        self.assertIsNone(decision.terminal_status)

    def test_cancel_does_not_require_role_recheck(self) -> None:
        """取消不执行任何业务变更，因此即使角色已经被撤销也应当总是能取消
        （``decide_cancel`` 的签名本身不接受 ``registry_entry`` 参数——结构上
        不存在"角色核对"这一步，不是遗漏）。"""

        pending = _pending()
        decision = decide_cancel(pending=pending, clicker_open_id=INITIATOR, now=NOW)
        self.assertTrue(decision.ok)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
