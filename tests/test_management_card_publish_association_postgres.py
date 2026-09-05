"""管理卡复用后「这一次操作到底发布了没有」的真库关联断言（需要真实 PostgreSQL 16）。

同一张管理卡在一次操作收口后仍可继续操作，因此一个 ``message_id`` 下会累积多条
``pending_action``。观察器与后台结算只能认**当前这一次操作**对应的发布记录：历史操作
的 ``published`` 行不得把新操作说成「已生效」，迟到的旧回调也不得覆盖新操作的卡片。

本文件只钉这条关联规则，不重复覆盖 ``tests/test_admin_management_integration_postgres.py``
已有的每日纠偏水位与 ``card_sequence`` CAS 断言。
"""

from __future__ import annotations

import json
import os
import time
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_management_card_context import PostgresManagementCardContextStore
from lingxi.apps.gateway import management_cards
from lingxi.apps.gateway.management_cards import RecomputeResultReporter
from lingxi.core.admin.views import AdminUserStatusView
from lingxi.core.ids import new_id
from lingxi.core.permission.targeted_recompute import RecomputeKind, TargetedRecomputeOutcome

DSN = os.environ.get("LINGXI_POSTGRES_DSN")
SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，管理卡复用后的发布关联断言未验证"
    if not DSN
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，未验证"
)

ADMIN_OPEN_ID = "ou_reuse_association_admin"
TARGET_OPEN_ID = "ou_reuse_association_target"
MESSAGE_ID = "om_reuse_association"


class _RecordingAudit:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, event: str, **fields) -> None:
        self.events.append((event, fields))


class _RecordingRefresher:
    """只记下这次要求卡片显示什么，不真的调用 CardKit。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def update(self, **kwargs) -> bool:
        self.calls.append(kwargs)
        return True


@unittest.skipUnless(DSN and psycopg_available(), SKIP_REASON)
class ManagementCardReuseAssociationTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._dsn = DSN
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        reset_production_rows(self._dsn)
        self.now = datetime.now(UTC)
        self.execute(
            """INSERT INTO app_user
                 (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name,
                  department, tenant_key, provisioning_state, account_state)
               VALUES (%s, %s, %s, %s, '化名用户', '测试部门', 'tk_test', 'active', 'enabled')""",
            (new_id("usr"), TARGET_OPEN_ID, f"fs_{TARGET_OPEN_ID}", f"un_{TARGET_OPEN_ID}"),
        )
        self.store = PostgresManagementCardContextStore(self._dsn)
        self.store.remember(
            message_id=MESSAGE_ID,
            identifier=TARGET_OPEN_ID,
            card_id=f"card_{MESSAGE_ID}",
            chat_id="oc_1",
            initiated_by_open_id=ADMIN_OPEN_ID,
            snapshot_fingerprint="fp",
            context_deadline_at=self.now + timedelta(hours=1),
        )

    def execute(self, sql: str, parameters: tuple = ()) -> None:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(sql, parameters)

    def query(self, sql: str, parameters: tuple = ()) -> list[tuple]:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(sql, parameters)
            return cursor.fetchall()

    def seed_executed_action(self, *, created_at: datetime, decided_at: datetime) -> str:
        """写一条已确认执行、挂在同一张管理卡上的操作，返回它的 id。"""
        action_id = new_id("pac")
        self.execute(
            """INSERT INTO pending_action
                 (id, action_type, target_open_id, target_state_snapshot,
                  initiated_by_open_id, status, card_delivered, card_id,
                  payload, origin_card_message_id, created_at, confirm_deadline_at,
                  decided_at, decided_by_open_id)
               VALUES (%s, 'local_permission_grant', %s, 'absent', %s,
                       'executed', TRUE, %s, %s, %s, %s, %s, %s, %s)""",
            (
                action_id,
                TARGET_OPEN_ID,
                ADMIN_OPEN_ID,
                f"card_confirm_{action_id}",
                json.dumps({"company_id": "1011", "metric_name": "daily_active", "reason": "t"}),
                MESSAGE_ID,
                created_at,
                created_at + timedelta(minutes=10),
                decided_at,
                ADMIN_OPEN_ID,
            ),
        )
        return action_id

    def seed_waiting_action(self, *, created_at: datetime) -> str:
        """写一条确认卡已发出、尚未点确认的操作（管理卡处于 ``submitted``）。"""
        action_id = new_id("pac")
        self.execute(
            """INSERT INTO pending_action
                 (id, action_type, target_open_id, target_state_snapshot,
                  initiated_by_open_id, status, card_delivered, card_id,
                  payload, origin_card_message_id, created_at, confirm_deadline_at)
               VALUES (%s, 'local_permission_grant', %s, 'absent', %s,
                       'pending', TRUE, %s, %s, %s, %s, %s)""",
            (
                action_id,
                TARGET_OPEN_ID,
                ADMIN_OPEN_ID,
                f"card_confirm_{action_id}",
                json.dumps({"company_id": "1011", "metric_name": "daily_active", "reason": "t"}),
                MESSAGE_ID,
                created_at,
                created_at + timedelta(minutes=10),
            ),
        )
        return action_id

    def seed_outbox(
        self,
        *,
        permission_version: int,
        created_at: datetime,
        status: str = "published",
        published_at: datetime | None = None,
        reason: str = "admin_action_instant_recompute",
    ) -> str:
        outbox_id = new_id("pub")
        user_id = self.query(
            "SELECT id FROM app_user WHERE feishu_open_id = %s", (TARGET_OPEN_ID,)
        )[0][0]
        payload = json.dumps(
            {
                "record_key": "target@example.com",
                "email": "target@example.com",
                "name": "化名用户",
                "permissions": '{"1011":["daily_active"]}',
                "status": "approved",
                "updated_at": created_at.isoformat(),
            }
        )
        self.execute(
            """INSERT INTO publish_outbox
                 (id, user_id, permission_version, reason, payload, status,
                  created_at, published_at, content_expires_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                outbox_id,
                user_id,
                permission_version,
                reason,
                payload,
                status,
                created_at,
                published_at,
                created_at + timedelta(days=90),
            ),
        )
        return outbox_id

    def card_state(self) -> tuple[str, str]:
        context = self.store.lookup_context(message_id=MESSAGE_ID)
        assert context is not None
        return context.state, context.dispatch_status

    def build_reporter(self) -> tuple[RecomputeResultReporter, _RecordingRefresher]:
        refresher = _RecordingRefresher()
        status = AdminUserStatusView(
            identifier=TARGET_OPEN_ID,
            provisioning_state="active",
            account_state="enabled",
            permission_version=1,
            updated_at=self.now.isoformat(),
        )
        reporter = RecomputeResultReporter(
            context_store=self.store,
            refresher=refresher,
            status_lookup=lambda _identifier: status,
            audit=_RecordingAudit(),
        )
        return reporter, refresher

    def wait_for_state(self, wanted: set[str], *, timeout: float = 15.0) -> str:
        """等观察线程把卡片写成某个终态；超时直接把当前状态交给断言判红。"""
        deadline = time.monotonic() + timeout
        state = self.card_state()[0]
        while state not in wanted and time.monotonic() < deadline:
            time.sleep(0.02)
            state = self.card_state()[0]
        return state


class HistoryPublishCannotReportNewActionTests(ManagementCardReuseAssociationTestCase):
    """#602 反例：操作 A 的已发布记录不得把复用同一张卡的操作 B 说成「已生效」。"""

    def setUp(self) -> None:
        super().setUp()
        self.action_a = self.seed_executed_action(
            created_at=self.now - timedelta(minutes=31),
            decided_at=self.now - timedelta(minutes=30),
        )
        self.seed_outbox(
            permission_version=1,
            created_at=self.now - timedelta(minutes=29),
            published_at=self.now - timedelta(minutes=28),
        )

    def _start_operation_b(self) -> str:
        action_b = self.seed_executed_action(
            created_at=self.now - timedelta(minutes=2),
            decided_at=self.now - timedelta(minutes=1),
        )
        self.store.update_state(
            message_id=MESSAGE_ID, state="dispatching", dispatch_status="publishing"
        )
        return action_b

    def test_observer_timeout_keeps_reused_card_incomplete_when_b_has_no_publish(self) -> None:
        action_b = self._start_operation_b()
        reporter, refresher = self.build_reporter()
        pending_b = SimpleNamespace(id=action_b, origin_card_message_id=MESSAGE_ID)

        with _ShortObserveWindow():
            reporter.on_queued(pending_b, TargetedRecomputeOutcome(kind=RecomputeKind.ENQUEUED))
            state = self.wait_for_state({"incomplete", "effective"})

        self.assertEqual(state, "incomplete")
        self.assertEqual(self.card_state(), ("incomplete", "incomplete"))
        self.assertNotIn("已生效", [call.get("dispatch_status") for call in refresher.calls])

    def test_settle_does_not_close_reused_card_when_b_has_no_publish(self) -> None:
        self._start_operation_b()

        self.assertEqual(self.store.settle_published_contexts(), ())
        self.assertEqual(self.card_state(), ("dispatching", "publishing"))

    def test_settle_does_not_close_reused_card_while_b_publish_is_pending(self) -> None:
        self._start_operation_b()
        self.seed_outbox(
            permission_version=2, created_at=self.now, status="pending", published_at=None
        )

        self.assertEqual(self.store.settle_published_contexts(), ())
        self.assertEqual(self.card_state(), ("dispatching", "publishing"))

    def test_settle_does_not_close_card_waiting_for_an_unconfirmed_operation(self) -> None:
        self.seed_waiting_action(created_at=self.now - timedelta(minutes=2))
        self.store.update_state(
            message_id=MESSAGE_ID, state="submitted", dispatch_status="publishing"
        )

        self.assertEqual(self.store.settle_published_contexts(), ())
        self.assertEqual(self.card_state(), ("submitted", "publishing"))

    def test_late_success_callback_of_action_a_cannot_mark_b_effective(self) -> None:
        self._start_operation_b()
        reporter, _refresher = self.build_reporter()
        pending_a = SimpleNamespace(id=self.action_a, origin_card_message_id=MESSAGE_ID)

        reporter.on_completed(pending_a)

        self.assertEqual(self.card_state(), ("dispatching", "publishing"))

    def test_late_failure_callback_of_action_a_cannot_downgrade_b(self) -> None:
        self._start_operation_b()
        reporter, _refresher = self.build_reporter()
        pending_a = SimpleNamespace(id=self.action_a, origin_card_message_id=MESSAGE_ID)

        reporter.on_failed(pending_a, None)

        self.assertEqual(self.card_state(), ("dispatching", "publishing"))

    def test_own_publish_still_reports_b_effective(self) -> None:
        action_b = self._start_operation_b()
        self.seed_outbox(
            permission_version=2,
            created_at=self.now,
            published_at=self.now + timedelta(seconds=1),
        )
        reporter, _refresher = self.build_reporter()
        pending_b = SimpleNamespace(id=action_b, origin_card_message_id=MESSAGE_ID)

        with _ShortObserveWindow():
            reporter.on_queued(pending_b, TargetedRecomputeOutcome(kind=RecomputeKind.ENQUEUED))
            state = self.wait_for_state({"incomplete", "effective"})

        self.assertEqual(state, "effective")
        self.assertEqual(self.card_state(), ("effective", "effective"))

    def test_settle_closes_the_card_once_b_is_published(self) -> None:
        self._start_operation_b()
        self.seed_outbox(
            permission_version=2,
            created_at=self.now,
            published_at=self.now + timedelta(seconds=1),
        )

        self.assertEqual(self.store.settle_published_contexts(), ())
        self.assertEqual(self.card_state(), ("effective", "effective"))
        self.assertEqual(self.store.settle_published_contexts(), ())
        self.assertEqual(self.card_state(), ("effective", "effective"))


class PublishAssociationPortTests(ManagementCardReuseAssociationTestCase):
    """关联规则的端口级断言：观察器读到的必须是本次操作自己的发布状态。"""

    def setUp(self) -> None:
        super().setUp()
        self.action_a = self.seed_executed_action(
            created_at=self.now - timedelta(minutes=31),
            decided_at=self.now - timedelta(minutes=30),
        )
        self.seed_outbox(
            permission_version=1,
            created_at=self.now - timedelta(minutes=29),
            published_at=self.now - timedelta(minutes=28),
        )
        self.action_b = self.seed_executed_action(
            created_at=self.now - timedelta(minutes=2),
            decided_at=self.now - timedelta(minutes=1),
        )

    def test_action_b_reads_none_while_only_action_a_has_published(self) -> None:
        self.assertIsNone(
            self.store.latest_publish_state_for_action(
                message_id=MESSAGE_ID, pending_action_id=self.action_b
            )
        )

    def test_action_b_reads_its_own_pending_and_then_published_row(self) -> None:
        outbox_id = self.seed_outbox(
            permission_version=2, created_at=self.now, status="pending", published_at=None
        )
        self.assertEqual(
            self.store.latest_publish_state_for_action(
                message_id=MESSAGE_ID, pending_action_id=self.action_b
            ),
            "pending",
        )

        self.execute(
            "UPDATE publish_outbox SET status = 'published', published_at = now() WHERE id = %s",
            (outbox_id,),
        )
        self.assertEqual(
            self.store.latest_publish_state_for_action(
                message_id=MESSAGE_ID, pending_action_id=self.action_b
            ),
            "published",
        )

    def test_action_id_from_another_card_is_never_answered(self) -> None:
        self.assertIsNone(
            self.store.latest_publish_state_for_action(
                message_id="om_someone_else", pending_action_id=self.action_b
            )
        )
        self.assertIsNone(
            self.store.latest_publish_state_for_action(message_id=MESSAGE_ID, pending_action_id="")
        )

    def test_only_the_newest_action_counts_as_the_current_one(self) -> None:
        self.assertTrue(
            self.store.is_current_card_action(
                message_id=MESSAGE_ID, pending_action_id=self.action_b
            )
        )
        self.assertFalse(
            self.store.is_current_card_action(
                message_id=MESSAGE_ID, pending_action_id=self.action_a
            )
        )

    def test_a_card_without_any_action_refuses_the_callback(self) -> None:
        """否定断言：查不到当前操作时失败关闭。

        一张连"现在是哪一次操作"都答不出来的卡，没有任何依据接受一条迟到回调的
        回写——那条回调只能是孤儿。
        """
        self.assertFalse(
            self.store.is_current_card_action(
                message_id="om_without_actions", pending_action_id=self.action_a
            )
        )

    def test_a_missing_judgement_input_still_answers_yes(self) -> None:
        """参数缺失是调用方没给判据，不是库里没有答案——旧调用方行为不变。"""
        self.assertTrue(
            self.store.is_current_card_action(message_id=MESSAGE_ID, pending_action_id="")
        )
        self.assertTrue(
            self.store.is_current_card_action(message_id="", pending_action_id=self.action_a)
        )


class OrphanCallbackTests(ManagementCardReuseAssociationTestCase):
    """卡上一条 ``pending_action`` 都查不到时，迟到回调一个字都不回写。"""

    def test_a_late_callback_on_a_card_without_any_action_writes_nothing(self) -> None:
        self.store.update_state(
            message_id=MESSAGE_ID, state="dispatching", dispatch_status="publishing"
        )
        reporter, refresher = self.build_reporter()
        orphan = SimpleNamespace(id=new_id("pac"), origin_card_message_id=MESSAGE_ID)

        reporter.on_completed(orphan)

        self.assertEqual(self.card_state(), ("dispatching", "publishing"))
        self.assertEqual(refresher.calls, [])

    def test_a_late_failure_on_such_a_card_cannot_downgrade_it_either(self) -> None:
        self.store.update_state(
            message_id=MESSAGE_ID, state="dispatching", dispatch_status="publishing"
        )
        reporter, refresher = self.build_reporter()
        orphan = SimpleNamespace(id=new_id("pac"), origin_card_message_id=MESSAGE_ID)

        reporter.on_failed(orphan, None)

        self.assertEqual(self.card_state(), ("dispatching", "publishing"))
        self.assertEqual(refresher.calls, [])


class ExistingSettlementRulesStillHoldTests(ManagementCardReuseAssociationTestCase):
    """既有规则的回归：无变化、每日批恢复、重复回调、重启恢复窗口都不受关联收紧影响。"""

    def test_unchanged_recompute_marks_effective_without_any_publish_row(self) -> None:
        action = self.seed_executed_action(
            created_at=self.now - timedelta(minutes=2), decided_at=self.now - timedelta(minutes=1)
        )
        reporter, _refresher = self.build_reporter()

        reporter.on_queued(
            SimpleNamespace(id=action, origin_card_message_id=MESSAGE_ID),
            TargetedRecomputeOutcome(kind=RecomputeKind.UNCHANGED),
        )

        self.assertEqual(self.card_state(), ("effective", "effective"))

    def test_daily_batch_still_corrects_an_incomplete_card_once(self) -> None:
        self.seed_executed_action(
            created_at=self.now - timedelta(minutes=2), decided_at=self.now - timedelta(minutes=1)
        )
        self.store.update_state(
            message_id=MESSAGE_ID, state="incomplete", dispatch_status="incomplete"
        )
        self.seed_outbox(
            permission_version=1,
            created_at=self.now,
            published_at=self.now + timedelta(seconds=1),
            reason="daily_permission_refresh",
        )

        self.assertEqual(self.store.settle_published_contexts(), (MESSAGE_ID,))
        self.assertEqual(self.card_state(), ("effective", "effective"))
        self.assertEqual(self.store.unreported_daily_correction_ids(), (MESSAGE_ID,))
        self.assertEqual(self.store.settle_published_contexts(), ())

    def test_restart_window_still_settles_a_card_left_in_submitted(self) -> None:
        self.seed_executed_action(
            created_at=self.now - timedelta(minutes=2), decided_at=self.now - timedelta(minutes=1)
        )
        self.store.update_state(
            message_id=MESSAGE_ID, state="submitted", dispatch_status="publishing"
        )
        self.seed_outbox(
            permission_version=1,
            created_at=self.now,
            published_at=self.now + timedelta(seconds=1),
        )

        self.assertEqual(self.store.settle_published_contexts(), ())
        self.assertEqual(self.card_state(), ("effective", "effective"))

    def test_publish_before_the_decision_never_settles_the_card(self) -> None:
        self.seed_executed_action(
            created_at=self.now - timedelta(minutes=2), decided_at=self.now - timedelta(minutes=1)
        )
        self.store.update_state(
            message_id=MESSAGE_ID, state="dispatching", dispatch_status="publishing"
        )
        self.seed_outbox(
            permission_version=1,
            created_at=self.now - timedelta(minutes=10),
            published_at=self.now - timedelta(minutes=9),
        )

        self.assertEqual(self.store.settle_published_contexts(), ())
        self.assertEqual(self.card_state(), ("dispatching", "publishing"))

    def test_closed_card_is_never_reopened_by_a_late_callback(self) -> None:
        action = self.seed_executed_action(
            created_at=self.now - timedelta(minutes=2), decided_at=self.now - timedelta(minutes=1)
        )
        self.store.update_state(message_id=MESSAGE_ID, state="closed", dispatch_status="idle")
        reporter, _refresher = self.build_reporter()

        reporter.on_completed(SimpleNamespace(id=action, origin_card_message_id=MESSAGE_ID))

        self.assertEqual(self.card_state(), ("closed", "idle"))


class _ShortObserveWindow:
    """把观察窗口压到测试可等的量级；不改变被测分支本身。"""

    def __enter__(self) -> None:
        self._observe = management_cards.MANAGEMENT_PUBLISH_OBSERVE_SECONDS
        self._poll = management_cards.MANAGEMENT_PUBLISH_POLL_SECONDS
        management_cards.MANAGEMENT_PUBLISH_OBSERVE_SECONDS = 0.3
        management_cards.MANAGEMENT_PUBLISH_POLL_SECONDS = 0.02

    def __exit__(self, *_exception) -> None:
        management_cards.MANAGEMENT_PUBLISH_OBSERVE_SECONDS = self._observe
        management_cards.MANAGEMENT_PUBLISH_POLL_SECONDS = self._poll


if __name__ == "__main__":
    unittest.main()
