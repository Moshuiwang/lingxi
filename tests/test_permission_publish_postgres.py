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
import secrets
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from postgres_schema import ensure_production_schema, reset_production_rows

from lingxi.adapters.mcp_token_cipher import McpTokenCipher, new_token
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
from lingxi.core.permission.publish_row import CREATED_FIELD_NAMES, PublishRow

#: biai-agent 加密规格 v1 的**公开测试向量**（非生产密钥、非生产令牌）。
SPEC_MASTER_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
TOKEN_CIPHER = "RklYRURJVjEyMzQ1Njc4OX5gpf2vKqJiLgzu2n4kug1V1rz6DDt1OCgAZVpg1pL+"

SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，权限发布 outbox 的真库断言未验证（需真实 PostgreSQL 16）"
)

NOW = datetime(2026, 8, 17, 3, 0, tzinfo=timezone.utc)
USER_A = "usr_publish_a"
USER_B = "usr_publish_b"
EMAIL_A = "jiaming.jia@example.invalid"
EMAIL_B = "yiming.yi@example.invalid"


def _row(
    email: str = EMAIL_A,
    *,
    permissions: str = '{"1011":["商务"]}',
    token_cipher: str | None = None,
) -> PublishRow:
    return PublishRow(
        record_key=email,
        email=email,
        name="化名甲",
        permissions=permissions,
        status="approved",
        updated_at="2026-08-17T03:00:00Z",
        token_cipher=token_cipher,
    )


def _attempt(
    outbox_id: str,
    *,
    outcome: PublishOutcome = PublishOutcome.PUBLISHED,
    version: int = 1,
    record_id: str | None = "rec_1",
    user_id: str = USER_A,
    attempts: int = 1,
) -> PublishAttempt:
    return PublishAttempt(
        outcome=outcome,
        outbox_id=outbox_id,
        user_id=user_id,
        permission_version=version,
        attempts=attempts,
        external_record_id=record_id,
        mismatch_fields=("email",) if outcome is PublishOutcome.MISMATCH else (),
    )


class _Rollback(Exception):
    """哨兵异常：让持有连接的事务块以**异常**退出，从而真正回滚。

    psycopg3 的 ``connection.transaction()`` 干净退出时提交、只有异常退出才回滚。
    用例里要制造"这次认领没发生过"的状态，就必须显式抛一个自己认得出来的异常——
    用 ``AssertionError`` 之类会与真实断言失败混淆，用它则一眼能看出是刻意回滚。
    """


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
                        payload={**_row().fields, "刻意夹带": "x"},
                        permission_version=1,
                        tx=connection,
                    )
        self.assertEqual(self._count(), 0)

    def test_enqueue_accepts_the_create_field_set_with_a_cipher(self) -> None:
        """`V-权限-11` 前半：带令牌密文的七字段快照是合法的内容快照。"""

        decision = self.store.record_decision(
            user_id=USER_A, row=_row(token_cipher=TOKEN_CIPHER), reason="first_onboarding",
            decided_at=NOW,
        )
        stored = self.store.load(decision.outbox_id)
        self.assertEqual(set(stored.payload), set(CREATED_FIELD_NAMES))
        self.assertEqual(stored.payload["token_cipher"], TOKEN_CIPHER)

    def test_enqueue_refuses_a_null_cipher_key(self) -> None:
        """键存在就要合法：``token_cipher: None`` 的七键快照既不是更新快照也不可新建。"""

        with connect(self._dsn) as connection:
            with connection.transaction():
                with self.assertRaises(ValueError):
                    self.store.enqueue_publish(
                        user_id=USER_A,
                        reason="x",
                        payload={**_row().fields, "token_cipher": None},
                        permission_version=1,
                        tx=connection,
                    )
        self.assertEqual(self._count(), 0)

    def test_claim_returns_the_previous_external_record(self) -> None:
        """重试时判定层要能回答"这一行是不是我们建的"——靠认领时带回的外部记录标识。"""

        decision = self.store.record_decision(
            user_id=USER_A, row=_row(token_cipher=TOKEN_CIPHER), reason="first", decided_at=NOW
        )
        first = self.store.claim_next()
        self.assertIsNone(first.external_record_id)
        self.store.complete(
            _attempt(decision.outbox_id, outcome=PublishOutcome.MISMATCH, record_id="rec_7"),
            status="pending",
        )
        second = self.store.claim_next()
        self.assertEqual(second.external_record_id, "rec_7")

    def test_enqueue_refuses_a_plaintext_token_in_the_snapshot(self) -> None:
        """否定面：**明文**进 outbox 的最后一关是形状校验（明文过不了 base64 判据）。"""

        for plaintext in (secrets.token_urlsafe(32), "明文令牌", "not base64!"):
            with self.subTest(plaintext_length=len(plaintext)):
                with connect(self._dsn) as connection:
                    with connection.transaction():
                        with self.assertRaises(ValueError) as caught:
                            self.store.enqueue_publish(
                                user_id=USER_A,
                                reason="x",
                                payload={**_row().fields, "token_cipher": plaintext},
                                permission_version=1,
                                tx=connection,
                            )
                        # 不回显收到的值：它是凭据材料。
                        self.assertNotIn(plaintext, str(caught.exception))
        self.assertEqual(self._count(), 0)

    def test_cipher_travels_but_plaintext_never_reaches_the_outbox(self) -> None:
        """全库扫描：密文在 outbox 里，**明文一处都没有**。"""

        cipher = McpTokenCipher(SPEC_MASTER_KEY)
        plaintext = new_token()
        self.store.record_decision(
            user_id=USER_A,
            row=_row(token_cipher=cipher.encrypt(plaintext)),
            reason="first_onboarding",
            decided_at=NOW,
        )
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT payload::text FROM publish_outbox")
            dumped = "".join(row[0] for row in cursor.fetchall())
        self.assertIn("token_cipher", dumped)
        self.assertNotIn(plaintext, dumped)

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

    def test_an_uncommitted_claim_still_blocks_the_newer_version(self) -> None:
        """二级独立审查 P2-1：**未提交的认领**也必须挡住同一用户的更新版本。

        这是原实现真实存在的漏洞窗口：判据写成「有没有 ``publishing``」时，消费者 A
        认领 v1 的 ``UPDATE`` 尚未提交，它写的 ``publishing`` 对消费者 B 不可见，B 读到
        的还是 v1 提交态的 ``pending``，于是 B 领走 v2；两个消费者同时对外发布，v1 后写
        就把已经收回的权限盖了回去。

        这里用**另一条连接里未提交的同一句认领**精确复现那个窗口：v1 的行被锁住且状态
        在本事务外仍是 ``pending``。改判据为「该用户不存在更早的非终态兄弟行」之后，
        v2 因为 v1 这条更早的 ``pending`` 而拒领——这正是本用例要钉住的行为。
        """

        first = self.store.record_decision(user_id=USER_A, row=_row(), reason="x", decided_at=NOW)
        self.store.record_decision(
            user_id=USER_A,
            row=_row(permissions='{"1012":["商务"]}'),
            reason="x",
            decided_at=NOW,
        )

        with connect(self._dsn) as holder:
            try:
                with holder.transaction():
                    with holder.cursor() as cursor:
                        # 模拟消费者 A 的认领：已经改了行、**还没提交**。
                        cursor.execute(
                            "UPDATE publish_outbox SET status = 'publishing', "
                            "attempts = attempts + 1, claimed_at = now() WHERE id = %s",
                            (first.outbox_id,),
                        )
                        # 消费者 B 走正式认领路径：v1 被锁（SKIP LOCKED 跳过），而 v2
                        # 必须被"更早的非终态兄弟行"挡住。拿到 v2 就是那个 bug 回来了。
                        self.assertIsNone(self.store.claim_next())
                    # **必须主动抛异常才会回滚**：psycopg3 的 `transaction()` 干净退出
                    # 时是**提交**，只有异常退出才回滚（定向复核发现）。不抛的话 v1 会
                    # 以 publishing 落库，下面那句"回滚后照常可认领"永远不成立。
                    # 哨兵异常同时把 attempts 的自增一并回滚，语义正好是"这次认领没发生"。
                    raise _Rollback
            except _Rollback:
                pass

        # A 的事务回滚后，v1 回到 pending，照常可被认领。
        recovered = self.store.claim_next()
        assert recovered is not None
        self.assertEqual(recovered.outbox_id, first.outbox_id)
        self.assertEqual(recovered.attempts, 1)

    def test_a_newer_version_waits_until_the_older_one_reaches_a_terminal_state(self) -> None:
        """已提交路径上的同一条规则：更早的意图没走到终态之前，新版本不被认领。"""

        first = self.store.record_decision(user_id=USER_A, row=_row(), reason="x", decided_at=NOW)
        second = self.store.record_decision(
            user_id=USER_A,
            row=_row(permissions='{"1012":["商务"]}'),
            reason="x",
            decided_at=NOW,
        )
        claimed = self.store.claim_next()
        assert claimed is not None
        self.assertEqual(claimed.outbox_id, first.outbox_id)
        self.assertIsNone(self.store.claim_next())

        # 回收到 pending 也仍然挡着：非终态就算数，不是只有 publishing 才算。
        self.store.reclaim_stale(older_than=timedelta(microseconds=1))
        again = self.store.claim_next()
        assert again is not None
        self.assertEqual(again.outbox_id, first.outbox_id)
        self.assertIsNone(self.store.claim_next())

        # 走到终态之后，v2 才成为该用户最老的非终态意图。
        self.store.complete(
            _attempt(first.outbox_id or "", attempts=again.attempts), status=STATUS_PUBLISHED
        )
        promoted = self.store.claim_next()
        assert promoted is not None
        self.assertEqual(promoted.outbox_id, second.outbox_id)

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

    def test_a_stale_completer_cannot_overwrite_the_new_claimer(self) -> None:
        """二级独立审查 P3-1：记账绑定到**本次认领**（``attempts``），不只看状态。

        只判 ``status='publishing'`` 时的错法：旧认领者迟到的记账会命中**新认领者**
        正在进行的那一行，把它改写成 ``published``；而新认领者随后的记账反而扑空，
        合法的那一方被报成 :class:`PublishClaimLost`。加上 ``attempts`` 守卫之后，
        两边各自归位——旧的失败，新的成功。
        """

        outbox_id = self._claimed()  # 旧认领者：attempts=1
        self.store.reclaim_stale(older_than=timedelta(microseconds=1))
        again = self.store.claim_next()  # 新认领者：attempts=2
        assert again is not None
        self.assertEqual(again.attempts, 2)

        with self.assertRaises(PublishClaimLost):
            self.store.complete(_attempt(outbox_id, attempts=1), status=STATUS_PUBLISHED)
        stale = self.store.load(outbox_id)
        assert stale is not None
        # 新认领者仍在进行中，没有被旧记账改写。
        self.assertEqual(stale.status, "publishing")
        self.assertIsNone(stale.published_at)

        self.store.complete(_attempt(outbox_id, attempts=2), status=STATUS_PUBLISHED)
        stored = self.store.load(outbox_id)
        assert stored is not None
        self.assertEqual(stored.status, "published")

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
