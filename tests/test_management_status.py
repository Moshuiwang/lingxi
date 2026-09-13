"""管理卡状态判定与状态机映射的纯逻辑用例：不组装 gateway，不碰数据库或 CardKit。

编排侧（起观察线程、调 transport、领序号）的用例留在 ``test_admin_position_card.py``
与 ``test_management_card_publish_association_postgres.py``；这里只钉判定本身。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from types import SimpleNamespace

from lingxi.core.admin.management_card_states import (
    DISPATCH_EFFECTIVE,
    DISPATCH_IDLE,
    DISPATCH_INCOMPLETE,
    DISPATCH_PUBLISHING,
    MANAGEMENT_CARD_DISPATCH_STATUSES,
    MANAGEMENT_CARD_STATES,
    STATE_CLOSED,
    STATE_DISPATCHING,
    STATE_EFFECTIVE,
    STATE_INCOMPLETE,
    STATE_READY,
    STATE_SUBMITTED,
    SUBMITTED_STATES,
)
from lingxi.core.admin.management_status import (
    PUBLISHING_STATUS_TEXT,
    RecomputeResultStatus,
    publish_observation,
    recovery_dispatch_status,
    translate_recompute_result,
)
from lingxi.core.admin.views import AdminUserStatusView

MIGRATION_0081 = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "alembic"
    / "versions"
    / "0081_management_card_context.py"
)


def _check_literals(column: str) -> tuple[str, ...]:
    """从迁移 0081 的建表语句里抠出某一列 CHECK 的取值，顺序保持原样。"""
    source = MIGRATION_0081.read_text(encoding="utf-8")
    match = re.search(rf"CHECK \({column} IN \(([^)]*)\)\)", source)
    assert match is not None, f"迁移 0081 里找不到 {column} 的 CHECK"
    return tuple(re.findall(r"'([a-z_]+)'", match.group(1)))


class ManagementCardStatesMatchMigrationTests(unittest.TestCase):
    """常量模块必须与迁移 0081 的 CHECK 逐字一致：数据库拒绝的值 Python 侧也不许写。"""

    def test_state_values_match_the_check_constraint_in_order(self) -> None:
        self.assertEqual(MANAGEMENT_CARD_STATES, _check_literals("state"))

    def test_dispatch_status_values_match_the_check_constraint_in_order(self) -> None:
        self.assertEqual(MANAGEMENT_CARD_DISPATCH_STATUSES, _check_literals("dispatch_status"))

    def test_named_constants_are_the_members_of_the_two_domains(self) -> None:
        self.assertEqual(
            (
                STATE_READY,
                STATE_SUBMITTED,
                STATE_DISPATCHING,
                STATE_EFFECTIVE,
                STATE_INCOMPLETE,
                STATE_CLOSED,
            ),
            MANAGEMENT_CARD_STATES,
        )
        self.assertEqual(
            (DISPATCH_IDLE, DISPATCH_PUBLISHING, DISPATCH_EFFECTIVE, DISPATCH_INCOMPLETE),
            MANAGEMENT_CARD_DISPATCH_STATUSES,
        )
        self.assertEqual(SUBMITTED_STATES, {STATE_SUBMITTED, STATE_DISPATCHING})

    def test_the_constants_module_imports_nothing(self) -> None:
        """任何一层都能引用它而不带入闭包，前提是它自己一个 import 都没有。"""
        import lingxi.core.admin.management_card_states as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        self.assertNotRegex(source, r"^(from|import) ", "常量模块不得 import 任何东西")


def _context(*, state: str, dispatch_status: str, last_trace_id: str | None = "trc_1"):
    return SimpleNamespace(
        state=state, dispatch_status=dispatch_status, last_trace_id=last_trace_id
    )


class RecoveryDispatchStatusTests(unittest.TestCase):
    """重启后从持久状态反推该显示哪种下发状态（恢复 scanner 用）。"""

    def test_a_persisted_dispatch_status_is_reused_as_is(self) -> None:
        for dispatch in ("publishing", "effective", "incomplete"):
            with self.subTest(dispatch=dispatch):
                context = _context(state="closed", dispatch_status=dispatch)
                self.assertEqual(recovery_dispatch_status(context), dispatch)

    def test_an_idle_dispatch_status_falls_back_to_the_state(self) -> None:
        for state, expected in (
            ("submitted", "publishing"),
            ("dispatching", "publishing"),
            ("effective", "effective"),
            ("incomplete", "incomplete"),
            ("ready", None),
            ("closed", None),
        ):
            with self.subTest(state=state):
                context = _context(state=state, dispatch_status="idle")
                self.assertEqual(recovery_dispatch_status(context), expected)


class TranslateRecomputeResultTests(unittest.TestCase):
    """后台重算 / 发布观察的结果翻译成 ``(state, 展示文本, 机器态)`` 这一组。"""

    def test_a_completed_result_becomes_effective(self) -> None:
        outcome = translate_recompute_result(
            _context(state="dispatching", dispatch_status="publishing"), complete=True
        )
        self.assertEqual(outcome, RecomputeResultStatus("effective", "已生效", "effective"))

    def test_a_completed_result_ignores_any_override_or_message(self) -> None:
        outcome = translate_recompute_result(
            _context(state="ready", dispatch_status="idle"),
            complete=True,
            status_message="随便什么",
            state_override="dispatching",
        )
        self.assertEqual(outcome, RecomputeResultStatus("effective", "已生效", "effective"))

    def test_a_queued_result_says_publishing(self) -> None:
        outcome = translate_recompute_result(
            _context(state="ready", dispatch_status="idle"),
            complete=False,
            status_message=PUBLISHING_STATUS_TEXT,
            state_override="dispatching",
        )
        self.assertEqual(
            outcome, RecomputeResultStatus("dispatching", "操作已记录，权限正在下发", "publishing")
        )

    def test_a_queued_result_without_a_message_still_says_publishing(self) -> None:
        outcome = translate_recompute_result(
            _context(state="ready", dispatch_status="idle"),
            complete=False,
            state_override="dispatching",
        )
        self.assertEqual(
            outcome, RecomputeResultStatus("dispatching", "操作已记录，权限正在下发", "publishing")
        )

    def test_a_failed_result_is_incomplete_with_the_trace_id(self) -> None:
        outcome = translate_recompute_result(
            _context(state="dispatching", dispatch_status="publishing", last_trace_id="trc_9"),
            complete=False,
        )
        self.assertEqual(
            outcome,
            RecomputeResultStatus(
                "incomplete", "下发未完成，最迟次日自动纠正 · 追溯号 trc_9", "incomplete"
            ),
        )

    def test_a_failed_result_without_a_trace_id_names_the_current_operation(self) -> None:
        outcome = translate_recompute_result(
            _context(state="dispatching", dispatch_status="publishing", last_trace_id=None),
            complete=False,
        )
        self.assertEqual(outcome.display, "下发未完成，最迟次日自动纠正 · 追溯号 当前操作")
        self.assertEqual((outcome.state, outcome.machine), ("incomplete", "incomplete"))

    def test_a_skipped_result_keeps_its_own_message_but_is_still_incomplete(self) -> None:
        outcome = translate_recompute_result(
            _context(state="dispatching", dispatch_status="publishing"),
            complete=False,
            status_message="这次不下发",
        )
        self.assertEqual(outcome, RecomputeResultStatus("incomplete", "这次不下发", "incomplete"))


class PublishObservationTests(unittest.TestCase):
    """观察到的 outbox 状态怎么收口：只有已发布算生效，失败 / 被取代算未完成，其余继续等。"""

    def test_published_completes(self) -> None:
        self.assertIs(publish_observation("published"), True)

    def test_failed_and_superseded_settle_as_incomplete(self) -> None:
        for state in ("failed", "superseded"):
            with self.subTest(state=state):
                self.assertIs(publish_observation(state), False)

    def test_pending_publishing_and_unreadable_keep_waiting(self) -> None:
        for state in ("pending", "publishing", None, ""):
            with self.subTest(state=state):
                self.assertIsNone(publish_observation(state))


class SuspendedUserTransientTextTests(unittest.TestCase):
    """#493 块 B 第二条（Trace #544）：**对已停用目标操作时的瞬时文案**。

    终态早已被 rc24 F5 纠正成那句真话，可是**瞬时**这一行还在说「操作已记录，权限
    正在下发」——对一个已停用的目标，下发根本不会发生（发布层在 ``app_user`` 行锁里
    就挡住了非 ``enabled`` 账号的非空授权）。管理员先看到一句不成立的承诺、隔一会儿
    才被终态纠正，是展示面失真。这里让瞬时与终态说同一句话。
    """

    TRUTH_FRAGMENTS = ("已停用", "不会下发")
    PUBLISHING_PROMISE = "权限正在下发"

    def _rendered(self, *, account_state: str, **overrides) -> str | None:
        from lingxi.core.admin.management_status import rendered_dispatch_status

        status = AdminUserStatusView(
            identifier="ou_target",
            provisioning_state="active",
            account_state=account_state,
            permission_version=1,
            updated_at="2026-09-02T12:00:00+00:00",
        )
        fields = {"state": "dispatching", "dispatch_status": None, "status_message": None}
        fields.update(overrides)
        return rendered_dispatch_status(status=status, **fields)

    def test_suspended_target_never_sees_the_publishing_promise(self) -> None:
        for name, fields in (
            ("即时路径已算好的文案", {"status_message": "操作已记录，权限正在下发"}),
            ("状态机 dispatching", {"state": "dispatching"}),
            ("状态机 submitted", {"state": "submitted"}),
            ("恢复 scanner 重画", {"state": "unknown", "dispatch_status": "publishing"}),
        ):
            with self.subTest(name=name):
                visible = self._rendered(account_state="suspended", **fields)
                assert visible is not None
                self.assertNotIn(self.PUBLISHING_PROMISE, visible)
                for fragment in self.TRUTH_FRAGMENTS:
                    self.assertIn(fragment, visible)

    def test_enabled_target_still_sees_the_publishing_line(self) -> None:
        """反向对照一：账号正常的用户，瞬时这一行逐字不变。"""

        visible = self._rendered(account_state="enabled")
        self.assertEqual(visible, "操作已记录，权限正在下发")

    def test_other_states_of_a_suspended_target_are_untouched(self) -> None:
        """反向对照二：只改写「正在下发」这一句——「已生效」「已取消」各有自己的判据，
        不在这里顺手一起改写。"""

        self.assertEqual(self._rendered(account_state="suspended", state="effective"), "已生效")
        self.assertEqual(self._rendered(account_state="suspended", state="closed"), "已取消")

    def test_a_status_view_without_account_state_keeps_the_old_wording(self) -> None:
        """反向对照三：读不到账号状态时按"没有额外信息"处理，行为逐字不变
        （与 ``is_account_not_enabled`` 同一姿态，保护旧测试替身）。"""

        from lingxi.core.admin.management_status import rendered_dispatch_status

        class _StatusWithoutAccountState:
            identifier = "ou_target"

        self.assertEqual(
            rendered_dispatch_status(
                status=_StatusWithoutAccountState(),
                state="dispatching",
                dispatch_status=None,
                status_message=None,
            ),
            "操作已记录，权限正在下发",
        )

    def test_the_publishing_literal_has_exactly_one_home(self) -> None:
        """撤除重复字面量（#493 块 B）：``apps/gateway/__init__.py`` 此前另抄了两份，
        改一处漏两处。"""

        from pathlib import Path

        import lingxi.apps.gateway as gateway_package
        from lingxi.core.admin.management_status import PUBLISHING_STATUS_TEXT

        source = (Path(gateway_package.__file__)).read_text(encoding="utf-8")

        self.assertEqual(PUBLISHING_STATUS_TEXT, "操作已记录，权限正在下发")
        self.assertNotIn(f'"{PUBLISHING_STATUS_TEXT}"', source)
