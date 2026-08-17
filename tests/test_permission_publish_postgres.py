"""权限发布 outbox 的真库断言（Issue #156 / S-C-01）。

认领断言：`V-权限-01`（权限变更事务回滚时发布意图一并回滚，不产生孤立发布意图）、
`V-权限-09`（同一用户同一权限版本只有一条意图）、`V-权限-12`（同一用户单飞、认领丢失
不被迟到的记账改写、重启后可回收重入）。

只有真库能证伪它们：**同事务**是事务的属性，**同一用户单飞**是 ``FOR UPDATE SKIP
LOCKED`` 加谓词的属性，**到期时间不可篡改**是触发器的属性——在假 store 上跑，这几条
无论实现怎么写都是绿的。

表结构由 ``migrations/alembic/versions/0064_permission_publish_outbox.py`` 建立，测试库
走 ``ensure_production_schema`` 的整条 alembic 链，与生产同源；迁移的逐条真往返
（``downgrade -1`` 之后再 ``upgrade``）由 ``scripts/ci/check_migration_chain.sh`` 在 CI
上覆盖，不在这里重复。
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from postgres_schema import ensure_production_schema, reset_production_rows

from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_permission_publish import (
    DecisionOutcome,
    PostgresPermissionPublishStore,
    PublishClaimLost,
)
from lingxi.core.permission.publish import (
    PublishAttempt,
    PublishOutcome,
    STATUS_FAILED,
    STATUS_PUBLISHED,
)
from lingxi.core.permission.publish_row import PublishRow

SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，权限发布 outbox 的真库断言未验证（需真实 PostgreSQL 16）"
)

NOW = datetime(2026, 8, 17, 3, 0, tzinfo=timezone.utc)
USER_A = "usr_publish_a"
USER_B = "usr_publish_b"
EMAIL_A = "jiaming.jia@example.invalid"
EMAIL_B = "yiming.yi@example.invalid"


def _row(email: str = EMAIL_A, *, permissions: str = '{"1011":["商务"]}') -> PublishRow:
    return PublishRow(
        record_key=email,
        email=email,
        name="化名甲",
        permissions=permissions,
        status="approved",
        updated_at="2026-08-17T03:00:00Z",
    )


def _attempt(
    outbox_id: str,
    *,
    outcome: PublishOutcome = PublishOutcome.PUBLISHED,
    version: int = 1,
    record_id: str | None = "rec_1",
    user_id: str = USER_A,
) -> PublishAttempt:
    return PublishAttempt(
        outcome=outcome,
        outbox_id=outbox_id,
        user_id=user_id,
        permission_version=version,
        external_record_id=record_id,
        mismatch_fields=("email",) if outcome is PublishOutcome.MISMATCH else (),
    )


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN"), SKIP_REASON)
class PermissionPublishPostgresTestCase(unittest.TestCase):
    """真库断言的共同底座。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        # 结构在进程内只建一次，用例之间只清行（同 IdentityPostgresTestCase 的既有惯例）。
        reset_production_rows(self._dsn)
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            for user_id, email in ((USER_A, EMAIL_A), (USER_B, EMAIL_B)):
                cursor.execute(
                    """INSERT INTO app_user
                         (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name,
                          department, tenant_key, email)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        user_id,
                        f"ou_{user_id}",
                        f"fs_{user_id}",
                        f"on_{user_id}",
                        "化名甲" if user_id == USER_A else "化名乙",
                        "测试部门",
                        "tenant-fake",
                        email,
                    ),
                )
        self.store = PostgresPermissionPublishStore(self._dsn)

    def _version(self, user_id: str = USER_A) -> int:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT permission_version FROM app_user WHERE id = %s", (user_id,))
            return int(cursor.fetchone()[0])

    def _count(self) -> int:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM publish_outbox")
            return int(cursor.fetchone()[0])


class SameTransactionTest(PermissionPublishPostgresTestCase):
    def test_decision_and_intent_land_together(self) -> None:
        decision = self.store.record_decision(
            user_id=USER_A, row=_row(), reason="first_onboarding", decided_at=NOW
        )
        self.assertEqual(decision.outcome, DecisionOutcome.ENQUEUED)
        self.assertEqual(decision.permission_version, 1)
        self.assertEqual(self._version(), 1)

        stored = self.store.load(decision.outbox_id or "")
        assert stored is not None
        self.assertEqual(stored.status, "pending")
        self.assertEqual(stored.payload, _row().fields)
        self.assertEqual(stored.reason, "first_onboarding")

    def test_rollback_leaves_neither_new_version_nor_orphan_intent(self) -> None:
        """`V-权限-01`：权限变更回滚时发布意图一并回滚。

        把 ``record_decision`` 拆成两个事务，这条立刻变红——那正是要挡的形状。
        """

        with mock.patch.object(
            self.store, "enqueue_publish", side_effect=RuntimeError("注入的写入失败")
        ):
            with self.assertRaises(RuntimeError):
                self.store.record_decision(
                    user_id=USER_A, row=_row(), reason="first_onboarding", decided_at=NOW
                )

        self.assertEqual(self._version(), 0)
        self.assertEqual(self._count(), 0)

    def test_enqueue_requires_the_callers_transaction(self) -> None:
        with self.assertRaises(TypeError):
            self.store.enqueue_publish(
                user_id=USER_A,
                reason="x",
                payload=_row().fields,
                permission_version=1,
                tx=None,  # type: ignore[arg-type]
            )

    def test_enqueue_rejects_a_payload_with_extra_fields(self) -> None:
        with connect(self._dsn) as connection:
            with connection.transaction():
                with self.assertRaises(ValueError):
                    self.store.enqueue_publish(
                        user_id=USER_A,
                        reason="x",
                        payload={**_row().fields, "token_cipher": "secret"},
                        permission_version=1,
                        tx=connection,
                    )
        self.assertEqual(self._count(), 0)

    def test_unchanged_permission_does_not_enqueue_again(self) -> None:
        first = self.store.record_decision(
            user_id=USER_A, row=_row(), reason="daily_refresh", decided_at=NOW
        )
        second = self.store.record_decision(
            user_id=USER_A, row=_row(), reason="daily_refresh", decided_at=NOW + timedelta(days=1)
        )
        self.assertEqual(second.outcome, DecisionOutcome.UNCHANGED)
        self.assertEqual(second.outbox_id, first.outbox_id)
        self.assertEqual(self._version(), 1)
        self.assertEqual(self._count(), 1)

    def test_changed_permission_enqueues_a_new_version(self) -> None:
        self.store.record_decision(user_id=USER_A, row=_row(), reason="daily_refresh", decided_at=NOW)
        second = self.store.record_decision(
            user_id=USER_A,
            row=_row(permissions='{"1011":["商务"],"1012":["商务"]}'),
            reason="daily_refresh",
            decided_at=NOW,
        )
        self.assertEqual(second.outcome, DecisionOutcome.ENQUEUED)
        self.assertEqual(second.permission_version, 2)
        self.assertEqual(self._count(), 2)

    def test_failed_intent_is_retried_by_the_next_decision(self) -> None:
        """内容没变不等于已经发布成功：上一条 ``failed`` 时照常排新意图。"""

        first = self.store.record_decision(
            user_id=USER_A, row=_row(), reason="first_onboarding", decided_at=NOW
        )
        self.store.claim_next()
        self.store.complete(
            _attempt(first.outbox_id or "", outcome=PublishOutcome.CONFLICT, record_id=None),
            status=STATUS_FAILED,
        )
        second = self.store.record_decision(
            user_id=USER_A, row=_row(), reason="daily_refresh", decided_at=NOW
        )
        self.assertEqual(second.outcome, DecisionOutcome.ENQUEUED)
        self.assertEqual(second.permission_version, 2)

    def test_same_user_and_version_can_only_have_one_intent(self) -> None:
        with connect(self._dsn) as connection:
            with connection.transaction():
                self.store.enqueue_publish(
                    user_id=USER_A,
                    reason="x",
                    payload=_row().fields,
                    permission_version=1,
                    tx=connection,
                )
        with self.assertRaises(Exception):
            with connect(self._dsn) as connection:
                with connection.transaction():
                    self.store.enqueue_publish(
                        user_id=USER_A,
                        reason="y",
                        payload=_row().fields,
                        permission_version=1,
                        tx=connection,
                    )
        self.assertEqual(self._count(), 1)

    def test_deleting_the_user_removes_the_intent(self) -> None:
        self.store.record_decision(user_id=USER_A, row=_row(), reason="x", decided_at=NOW)
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM app_user WHERE id = %s", (USER_A,))
        self.assertEqual(self._count(), 0)


class ClaimTest(PermissionPublishPostgresTestCase):
    def test_claim_returns_the_current_permission_version(self) -> None:
        decision = self.store.record_decision(
            user_id=USER_A, row=_row(), reason="x", decided_at=NOW
        )
        claim = self.store.claim_next()
        assert claim is not None
        self.assertEqual(claim.outbox_id, decision.outbox_id)
        self.assertEqual(claim.permission_version, 1)
        self.assertEqual(claim.current_permission_version, 1)
        self.assertEqual(claim.attempts, 1)
        self.assertEqual(claim.payload, _row().fields)

    def test_one_user_has_at_most_one_publish_in_flight(self) -> None:
        """`V-权限-12`：同一用户单飞。少了这条，v1 的写入会落后到 v2 之后。"""

        self.store.record_decision(user_id=USER_A, row=_row(), reason="x", decided_at=NOW)
        self.store.record_decision(
            user_id=USER_A,
            row=_row(permissions='{"1012":["商务"]}'),
            reason="x",
            decided_at=NOW,
        )
        first = self.store.claim_next()
        assert first is not None
        self.assertEqual(first.permission_version, 1)
        # 该用户还有一条 pending，但它不能被并发认领。
        self.assertIsNone(self.store.claim_next())

        # 另一个用户不受影响。
        self.store.record_decision(user_id=USER_B, row=_row(EMAIL_B), reason="x", decided_at=NOW)
        other = self.store.claim_next()
        assert other is not None
        self.assertEqual(other.user_id, USER_B)

    def test_claim_marks_publishing_and_counts_attempts(self) -> None:
        decision = self.store.record_decision(user_id=USER_A, row=_row(), reason="x", decided_at=NOW)
        self.store.claim_next()
        stored = self.store.load(decision.outbox_id or "")
        assert stored is not None
        self.assertEqual(stored.status, "publishing")
        self.assertEqual(stored.attempts, 1)

    def test_empty_queue_returns_none(self) -> None:
        self.assertIsNone(self.store.claim_next())


class CompleteTest(PermissionPublishPostgresTestCase):
    def _claimed(self) -> str:
        decision = self.store.record_decision(user_id=USER_A, row=_row(), reason="x", decided_at=NOW)
        self.store.claim_next()
        return decision.outbox_id or ""

    def test_published_records_the_external_record_and_time(self) -> None:
        outbox_id = self._claimed()
        self.store.complete(_attempt(outbox_id), status=STATUS_PUBLISHED)
        stored = self.store.load(outbox_id)
        assert stored is not None
        self.assertEqual(stored.status, "published")
        self.assertEqual(stored.external_record_id, "rec_1")
        self.assertIsNotNone(stored.published_at)
        self.assertIsNone(stored.last_error)

    def test_failure_detail_holds_only_codes_and_field_names(self) -> None:
        outbox_id = self._claimed()
        self.store.complete(
            _attempt(outbox_id, outcome=PublishOutcome.MISMATCH), status=STATUS_FAILED
        )
        stored = self.store.load(outbox_id)
        assert stored is not None
        self.assertEqual(stored.status, "failed")
        self.assertIsNone(stored.published_at)
        assert stored.last_error is not None
        self.assertIn("fields=email", stored.last_error)
        self.assertNotIn(EMAIL_A, stored.last_error)

    def test_a_lost_claim_cannot_be_written_back(self) -> None:
        outbox_id = self._claimed()
        self.store.reclaim_stale(older_than=timedelta(microseconds=1))
        with self.assertRaises(PublishClaimLost):
            self.store.complete(_attempt(outbox_id), status=STATUS_PUBLISHED)
        stored = self.store.load(outbox_id)
        assert stored is not None
        self.assertEqual(stored.status, "pending")

    def test_reclaim_puts_stale_publishing_back(self) -> None:
        outbox_id = self._claimed()
        self.assertEqual(self.store.reclaim_stale(older_than=timedelta(days=1)), 0)
        self.assertEqual(self.store.reclaim_stale(older_than=timedelta(microseconds=1)), 1)
        stored = self.store.load(outbox_id)
        assert stored is not None
        self.assertEqual(stored.status, "pending")
        # 回收之后可以重新认领；重入安全靠"先查后写"的幂等（见 core 的用例）。
        self.assertIsNotNone(self.store.claim_next())


class RetentionAndTriggerTest(PermissionPublishPostgresTestCase):
    def test_expiry_is_pinned_to_ninety_days_after_creation(self) -> None:
        self.store.record_decision(user_id=USER_A, row=_row(), reason="x", decided_at=NOW)
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT content_expires_at - created_at FROM publish_outbox LIMIT 1"
            )
            self.assertEqual(cursor.fetchone()[0], timedelta(days=90))

    def test_caller_supplied_expiry_is_ignored(self) -> None:
        decision = self.store.record_decision(user_id=USER_A, row=_row(), reason="x", decided_at=NOW)
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE publish_outbox SET content_expires_at = now() WHERE id = %s",
                (decision.outbox_id,),
            )
            cursor.execute(
                "SELECT content_expires_at - created_at FROM publish_outbox WHERE id = %s",
                (decision.outbox_id,),
            )
            self.assertEqual(cursor.fetchone()[0], timedelta(days=90))

    def test_the_anchors_cannot_be_rewritten(self) -> None:
        decision = self.store.record_decision(user_id=USER_A, row=_row(), reason="x", decided_at=NOW)
        for column, value in (
            ("created_at", NOW),
            ("user_id", USER_B),
            ("permission_version", 9),
        ):
            with self.assertRaises(Exception):
                with connect(self._dsn) as connection, connection.cursor() as cursor:
                    cursor.execute(
                        f"UPDATE publish_outbox SET {column} = %s WHERE id = %s",
                        (value, decision.outbox_id),
                    )

    def test_published_status_requires_a_publish_time(self) -> None:
        decision = self.store.record_decision(user_id=USER_A, row=_row(), reason="x", decided_at=NOW)
        with self.assertRaises(Exception):
            with connect(self._dsn) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE publish_outbox SET status = 'published' WHERE id = %s",
                    (decision.outbox_id,),
                )

    def test_expired_payload_is_redacted_but_the_run_facts_survive(self) -> None:
        decision = self.store.record_decision(user_id=USER_A, row=_row(), reason="x", decided_at=NOW)
        self.store.claim_next()
        self.store.complete(_attempt(decision.outbox_id or ""), status=STATUS_PUBLISHED)

        # 相对于库里那一行**实际**的到期时刻判定，不依赖跑用例这台机器的日历。
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT content_expires_at FROM publish_outbox WHERE id = %s",
                (decision.outbox_id,),
            )
            expires = cursor.fetchone()[0]

        self.assertEqual(self.store.redact_expired_payloads(now=expires - timedelta(seconds=1)), 0)
        self.assertEqual(self.store.redact_expired_payloads(now=expires), 1)

        stored = self.store.load(decision.outbox_id or "")
        assert stored is not None
        self.assertEqual(stored.payload, {})
        self.assertEqual(stored.status, "published")
        self.assertEqual(stored.permission_version, 1)
        self.assertEqual(stored.external_record_id, "rec_1")
        # 再跑一次不会重复计数（已经擦过的行不再进入候选集）。
        self.assertEqual(self.store.redact_expired_payloads(now=expires), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
