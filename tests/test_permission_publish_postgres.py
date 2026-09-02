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
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.adapters.mcp_token_cipher import McpTokenCipher, new_token
from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_conversation import _Transaction
from lingxi.adapters.postgres_permission_publish import (
    DecisionOutcome,
    PostgresPermissionPublishStore,
    PublishClaimLost,
)
from lingxi.apps.scheduler.permission_publish import DEFAULT_PUBLISH_LIMIT
from lingxi.core.ids import new_id
from lingxi.core.permission.publish import (
    DEFAULT_MAX_ATTEMPTS,
    PermissionDecisionTransientFailure,
    PermissionGrantBlockedByAccountState,
    PermissionPublishExecutor,
    PermissionTableError,
    PublishAttempt,
    PublishOutcome,
    STATUS_FAILED,
    STATUS_PUBLISHED,
)
from lingxi.core.permission.publish_row import (
    CREATED_FIELD_NAMES,
    PublishRow,
    build_revocation_row,
)

#: biai-agent 加密规格 v1 的**公开测试向量**（非生产密钥、非生产令牌）。
SPEC_MASTER_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
TOKEN_CIPHER = "RklYRURJVjEyMzQ1Njc4OX5gpf2vKqJiLgzu2n4kug1V1rz6DDt1OCgAZVpg1pL+"

SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，权限发布 outbox 的真库断言未验证（需真实 PostgreSQL 16）"
    if not os.environ.get("LINGXI_POSTGRES_DSN")
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，权限发布 outbox 的真库断言未验证"
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
    action: str = "none",
) -> PublishAttempt:
    return PublishAttempt(
        outcome=outcome,
        outbox_id=outbox_id,
        user_id=user_id,
        permission_version=version,
        attempts=attempts,
        action=action,
        external_record_id=record_id,
        mismatch_fields=("email",) if outcome is PublishOutcome.MISMATCH else (),
    )


class _Rollback(Exception):
    """哨兵异常：让持有连接的事务块以**异常**退出，从而真正回滚。

    psycopg3 的 ``connection.transaction()`` 干净退出时提交、只有异常退出才回滚。
    用例里要制造"这次认领没发生过"的状态，就必须显式抛一个自己认得出来的异常——
    用 ``AssertionError`` 之类会与真实断言失败混淆，用它则一眼能看出是刻意回滚。
    """


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN") and psycopg_available(), SKIP_REASON)
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

    def _account_state(self, user_id: str = USER_A) -> str:
        """读**已提交**的账号状态（独立连接、独立事务），供并发用例取证。"""

        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT account_state FROM app_user WHERE id = %s", (user_id,))
            return str(cursor.fetchone()[0])

    def _set_states(
        self,
        user_id: str = USER_A,
        *,
        account_state: str | None = None,
        provisioning_state: str | None = None,
    ) -> None:
        """只翻转这两个状态列，与生产 ``postgres_pending_action.py`` 的
        ``suspend_user``/``resume_user`` 分支同一姿态（不动其他列）。"""

        assignments = []
        parameters: list[str] = []
        if account_state is not None:
            assignments.append("account_state = %s")
            parameters.append(account_state)
        if provisioning_state is not None:
            assignments.append("provisioning_state = %s")
            parameters.append(provisioning_state)
        assert assignments, "至少要翻转一列"
        parameters.append(user_id)
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE app_user SET {', '.join(assignments)}, updated_at = now() WHERE id = %s",
                tuple(parameters),
            )


class SameTransactionTest(PermissionPublishPostgresTestCase):
    def test_decision_and_intent_land_together(self) -> None:
        decision = self.store.record_decision(
            require_enabled_account=True,
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
                    require_enabled_account=True,
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
            require_enabled_account=True,
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

    def test_provenance_is_only_set_by_a_create_that_returned_an_id(self) -> None:
        """**G1**：出身列只由「``create_row`` 明确返回了 ID」这一种事实设置。

        ``external_record_id`` 是审计（任何尝试都写），``created_record_id`` 是出身。
        两者混用会让既有 26 行的一次更新失败在重试时被判成永久冲突。
        """

        decision = self.store.record_decision(
            require_enabled_account=True,
            user_id=USER_A, row=_row(token_cipher=TOKEN_CIPHER), reason="first", decided_at=NOW
        )
        first = self.store.claim_next()
        self.assertIsNone(first.created_record_id)

        # 一次**更新**失败：审计列记下这一行，出身列保持空。
        self.store.complete(
            _attempt(
                decision.outbox_id,
                outcome=PublishOutcome.MISMATCH,
                record_id="rec_7",
                action="update",
            ),
            status="pending",
        )
        second = self.store.claim_next()
        self.assertIsNone(second.created_record_id)
        self.assertEqual(self.store.load(decision.outbox_id).external_record_id, "rec_7")

        # 一次**创建**成功：出身列这才写上。
        self.store.complete(
            _attempt(
                decision.outbox_id,
                outcome=PublishOutcome.MISMATCH,
                record_id="rec_8",
                action="create",
                attempts=2,
            ),
            status="pending",
        )
        third = self.store.claim_next()
        self.assertEqual(third.created_record_id, "rec_8")

    def test_an_update_never_moves_the_provenance(self) -> None:
        """更新尝试不得把出身挪到别的行上——它传的是 ``None``，只能保持不变。"""

        decision = self.store.record_decision(
            require_enabled_account=True,
            user_id=USER_A, row=_row(token_cipher=TOKEN_CIPHER), reason="first", decided_at=NOW
        )
        self.store.claim_next()
        self.store.complete(
            _attempt(decision.outbox_id, outcome=PublishOutcome.MISMATCH, record_id="rec_1",
                     action="create"),
            status="pending",
        )
        self.store.claim_next()
        self.store.complete(
            _attempt(decision.outbox_id, outcome=PublishOutcome.MISMATCH, record_id="rec_2",
                     action="update", attempts=2),
            status="pending",
        )
        self.assertEqual(self.store.claim_next().created_record_id, "rec_1")

    def test_a_second_create_moves_the_provenance_to_the_live_row(self) -> None:
        """重复创建时出身取**最近一次**，不是停在第一次。

        能走到第二次 ``create`` 说明上一行已经查不到了。出身若停在那个失效的标识上，
        "我们建的行密文被改写"这道检查就永远命不中当前这一行——保护静默失效。
        """

        decision = self.store.record_decision(
            require_enabled_account=True,
            user_id=USER_A, row=_row(token_cipher=TOKEN_CIPHER), reason="first", decided_at=NOW
        )
        self.store.claim_next()
        self.store.complete(
            _attempt(decision.outbox_id, outcome=PublishOutcome.MISMATCH, record_id="rec_1",
                     action="create"),
            status="pending",
        )
        self.store.claim_next()
        self.store.complete(
            _attempt(decision.outbox_id, outcome=PublishOutcome.MISMATCH, record_id="rec_2",
                     action="create", attempts=2),
            status="pending",
        )
        self.assertEqual(self.store.claim_next().created_record_id, "rec_2")

    def test_an_uncertain_create_without_an_id_claims_no_provenance(self) -> None:
        """创建结果不明（没拿到 ID）时不认领出身：重试按普通路径收敛。"""

        decision = self.store.record_decision(
            require_enabled_account=True,
            user_id=USER_A, row=_row(token_cipher=TOKEN_CIPHER), reason="first", decided_at=NOW
        )
        self.store.claim_next()
        self.store.complete(
            _attempt(
                decision.outbox_id,
                outcome=PublishOutcome.UNCERTAIN,
                record_id=None,
                action="create",
            ),
            status="pending",
        )
        self.assertIsNone(self.store.claim_next().created_record_id)

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
            require_enabled_account=True,
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
            require_enabled_account=True,
            user_id=USER_A, row=_row(), reason="daily_refresh", decided_at=NOW
        )
        second = self.store.record_decision(
            require_enabled_account=True,
            user_id=USER_A, row=_row(), reason="daily_refresh", decided_at=NOW + timedelta(days=1)
        )
        self.assertEqual(second.outcome, DecisionOutcome.UNCHANGED)
        self.assertEqual(second.outbox_id, first.outbox_id)
        self.assertEqual(self._version(), 1)
        self.assertEqual(self._count(), 1)

    def test_changed_permission_enqueues_a_new_version(self) -> None:
        self.store.record_decision(user_id=USER_A, row=_row(), reason="daily_refresh", decided_at=NOW, require_enabled_account=True)
        second = self.store.record_decision(
            require_enabled_account=True,
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
            require_enabled_account=True,
            user_id=USER_A, row=_row(), reason="first_onboarding", decided_at=NOW
        )
        self.store.claim_next()
        self.store.complete(
            _attempt(first.outbox_id or "", outcome=PublishOutcome.CONFLICT, record_id=None),
            status=STATUS_FAILED,
        )
        second = self.store.record_decision(
            require_enabled_account=True,
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
        self.store.record_decision(user_id=USER_A, row=_row(), reason="x", decided_at=NOW, require_enabled_account=True)
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM app_user WHERE id = %s", (USER_A,))
        self.assertEqual(self._count(), 0)


class DeliveredContentPurgeTest(PermissionPublishPostgresTestCase):
    """``record_decision(clear_delivered_content=True)`` 接入权限变化感知即清
    （Trace #328 S-P-5，迁移 0061 预留的 ``user_cleared`` 分类值）：真正推进版本
    **且 permissions 内容真的变了**时，在**同一个数据库事务**里顺带清空该用户
    已送达、随会话保留的投递正文，并排队失效当前 Agent 会话——与
    ``tests/test_pending_action_postgres.py::SuspendPurgeRealDbTests``
    （suspend_user 复用的同一个底层接口）同一手法、同一组断言形状，这里钉的是
    ``record_decision`` 这一个新增入口自己的行为，以及 opus 审查 P1 收窄的清理
    触发判据（见 :class:`PermissionsUnchangedDoesNotPurgeTest`）。"""

    def seed_delivered_conversation(
        self, *, conversation_id: str, task_id: str, user_id: str, agent_session_id: str
    ) -> None:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO conversation
                       (id, user_id, feishu_chat_id, feishu_thread_id, agent_session_id)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    conversation_id,
                    user_id,
                    f"chat-{conversation_id}",
                    f"topic-{conversation_id}",
                    agent_session_id,
                ),
            )
            cursor.execute(
                """INSERT INTO task
                       (id, conversation_id, user_id, inbound_event_id, prompt, status,
                        target_worker_version, attempts, content_expires_at)
                   VALUES (%s, %s, %s, %s, '问题', 'awaiting_delivery', 'stable', 1, now())""",
                (task_id, conversation_id, user_id, f"event-{task_id}"),
            )
            cursor.execute(
                """INSERT INTO task_delivery_event
                       (id, task_id, sequence, event_type, terminal_kind, worker_id,
                        idempotency_key, platform_received_at, content)
                   VALUES (%s, %s, 1, 'terminal', 'success', 'worker-1', %s, now(), '已送达的答案')""",
                (new_id("tde"), task_id, f"{task_id}:terminal"),
            )

    def delivered_content(self, *, task_id: str) -> str | None:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT content FROM task_delivery_event WHERE task_id = %s", (task_id,))
            return cursor.fetchone()[0]

    def agent_session_id_of(self, *, conversation_id: str) -> str | None:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT agent_session_id FROM conversation WHERE id = %s", (conversation_id,)
            )
            return cursor.fetchone()[0]

    def pending_cleanup_reasons(self, *, agent_session_id: str) -> list[str]:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT reason FROM agent_session_cleanup WHERE agent_session_id = %s",
                (agent_session_id,),
            )
            return [row[0] for row in cursor.fetchall()]

    def remember_memory(self, *, user_id: str, memory_key: str = "k", memory_value: str = "v") -> None:
        """给某个用户直接写一条记忆（Issue #357 S-H3-3），供权限变化清除钩子测试
        复用。"""

        with connect(self._dsn) as connection:
            with connection.transaction():
                memory_id = _Transaction(connection).remember_user_memory(
                    user_id=user_id,
                    memory_type="term_mapping",
                    memory_key=memory_key,
                    memory_value=memory_value,
                )
        assert memory_id is not None

    def memory_count(self, *, user_id: str) -> int:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM user_memory WHERE user_id = %s", (user_id,))
            return cursor.fetchone()[0]

    def test_a_changed_decision_clears_delivered_content_and_queues_session_cleanup(self) -> None:
        """①：权限变化（新版本，非 UNCHANGED，且 permissions 内容真的变了）→ 该
        用户已送达正文被清 + 清出计数进 ``PermissionDecision.cleared_events`` +
        Agent 会话清理入队（0061 队列既有断言形状：``reason='user_cleared'``）+
        用户记忆同一事务一并清除（Issue #357 S-H3-3 c 节、设计文 e 节第 4 项）。"""

        self.seed_delivered_conversation(
            conversation_id="cnv-p1", task_id="tsk-p1", user_id=USER_A, agent_session_id="sess-p1"
        )
        self.remember_memory(user_id=USER_A)

        decision = self.store.record_decision(
            require_enabled_account=True,
            user_id=USER_A,
            row=_row(),
            reason="daily_permission_refresh",
            decided_at=NOW,
            clear_delivered_content=True,
        )

        self.assertEqual(decision.outcome, DecisionOutcome.ENQUEUED)
        self.assertEqual(decision.cleared_events, 1)
        self.assertIsNone(self.delivered_content(task_id="tsk-p1"))
        self.assertIsNone(
            self.agent_session_id_of(conversation_id="cnv-p1"), "硬失效：立即置空，不等两小时规则"
        )
        self.assertEqual(self.pending_cleanup_reasons(agent_session_id="sess-p1"), ["user_cleared"])
        self.assertEqual(self.memory_count(user_id=USER_A), 0)

    def test_an_unchanged_decision_clears_nothing(self) -> None:
        """②（否定）：``UNCHANGED`` 时压根没有清理动作发生，用户记忆同样不受影响
        （设计文 e 节第 4 项「UNCHANGED 不误清」）。"""

        self.store.record_decision(
            require_enabled_account=True,
            user_id=USER_A, row=_row(), reason="daily_permission_refresh", decided_at=NOW
        )
        self.seed_delivered_conversation(
            conversation_id="cnv-p2", task_id="tsk-p2", user_id=USER_A, agent_session_id="sess-p2"
        )
        self.remember_memory(user_id=USER_A)

        decision = self.store.record_decision(
            require_enabled_account=True,
            user_id=USER_A,
            row=_row(),
            reason="daily_permission_refresh",
            decided_at=NOW + timedelta(days=1),
            clear_delivered_content=True,
        )

        self.assertEqual(decision.outcome, DecisionOutcome.UNCHANGED)
        self.assertEqual(decision.cleared_events, 0)
        self.assertEqual(self.delivered_content(task_id="tsk-p2"), "已送达的答案")
        self.assertEqual(self.agent_session_id_of(conversation_id="cnv-p2"), "sess-p2")
        self.assertEqual(self.pending_cleanup_reasons(agent_session_id="sess-p2"), [])
        self.assertEqual(self.memory_count(user_id=USER_A), 1, "UNCHANGED 不误清记忆")

    def test_the_switch_defaults_to_off_for_callers_like_first_onboarding(self) -> None:
        """开关默认关闭：首次开通既有调用形状不传它时，即使这次决定真的
        ``ENQUEUED``，也不触碰任何会话——这是"开通链侧不接"的运行期落点。"""

        self.seed_delivered_conversation(
            conversation_id="cnv-p3", task_id="tsk-p3", user_id=USER_A, agent_session_id="sess-p3"
        )

        decision = self.store.record_decision(
            require_enabled_account=True,
            user_id=USER_A, row=_row(), reason="first_onboarding", decided_at=NOW
        )

        self.assertEqual(decision.outcome, DecisionOutcome.ENQUEUED)
        self.assertEqual(decision.cleared_events, 0)
        self.assertEqual(self.delivered_content(task_id="tsk-p3"), "已送达的答案")
        self.assertEqual(self.agent_session_id_of(conversation_id="cnv-p3"), "sess-p3")
        self.assertEqual(self.pending_cleanup_reasons(agent_session_id="sess-p3"), [])

    def test_a_purge_failure_rolls_back_the_whole_decision(self) -> None:
        """③：清理与决定写入**同一个事务**——注入清理失败，版本推进与发布意图入队
        随事务一起回滚（响亮，不留半套状态），已经"清空"的正文因为从未真正提交，
        回滚后原样还在。这次注入的失败点在 ``clear_delivered_content_for_user``
        （先于 ``clear_user_memory`` 执行，见 adapter 文件头部同一姿态说明），
        因此记忆压根没有被触碰过，同样必须原样还在。"""

        self.seed_delivered_conversation(
            conversation_id="cnv-p4", task_id="tsk-p4", user_id=USER_A, agent_session_id="sess-p4"
        )
        self.remember_memory(user_id=USER_A)

        with mock.patch(
            "lingxi.adapters.postgres_permission_publish._ConversationTransaction"
            ".clear_delivered_content_for_user",
            side_effect=RuntimeError("注入的清理失败"),
        ):
            with self.assertRaises(RuntimeError):
                self.store.record_decision(
                    require_enabled_account=True,
                    user_id=USER_A,
                    row=_row(),
                    reason="daily_permission_refresh",
                    decided_at=NOW,
                    clear_delivered_content=True,
                )

        self.assertEqual(self._version(), 0, "推进的版本必须随事务一起回滚")
        self.assertEqual(self._count(), 0, "入队的发布意图必须随事务一起回滚")
        self.assertEqual(
            self.delivered_content(task_id="tsk-p4"),
            "已送达的答案",
            "清理从未真正提交，正文必须原样还在——不是「清了又恢复」，是「根本没发生过」",
        )
        self.assertEqual(self.pending_cleanup_reasons(agent_session_id="sess-p4"), [])
        self.assertEqual(self.memory_count(user_id=USER_A), 1)

    def test_a_memory_clear_failure_also_rolls_back_the_delivered_content_purge(self) -> None:
        """③的对称面：注入失败点换成 ``clear_user_memory`` 本身（它在
        ``clear_delivered_content_for_user`` **之后**执行，见 adapter 文件头部）
        ——此时正文清理已经真正执行过，断言它同样随事务一起回滚，不出现「正文已经
        清空、记忆清除却失败」的半套状态（设计文 e 节第 5 项精神的延伸：不局限于
        审计出口失败，任何同一事务内的后续步骤失败都必须回滚早前已执行的清理）。"""

        self.seed_delivered_conversation(
            conversation_id="cnv-p4b", task_id="tsk-p4b", user_id=USER_A, agent_session_id="sess-p4b"
        )
        self.remember_memory(user_id=USER_A)

        with mock.patch(
            "lingxi.adapters.postgres_permission_publish._ConversationTransaction"
            ".clear_user_memory",
            side_effect=RuntimeError("注入的记忆清理失败"),
        ):
            with self.assertRaises(RuntimeError):
                self.store.record_decision(
                    require_enabled_account=True,
                    user_id=USER_A,
                    row=_row(),
                    reason="daily_permission_refresh",
                    decided_at=NOW,
                    clear_delivered_content=True,
                )

        self.assertEqual(self._version(), 0, "推进的版本必须随事务一起回滚")
        self.assertEqual(self._count(), 0, "入队的发布意图必须随事务一起回滚")
        self.assertEqual(
            self.delivered_content(task_id="tsk-p4b"),
            "已送达的答案",
            "先执行的正文清理必须跟着后一步的失败一起回滚",
        )
        self.assertEqual(self.memory_count(user_id=USER_A), 1)

    def test_other_users_content_is_not_touched(self) -> None:
        """④：其他用户的正文、会话指针、清理队列与记忆完全不受影响。"""

        self.seed_delivered_conversation(
            conversation_id="cnv-p5a", task_id="tsk-p5a", user_id=USER_A, agent_session_id="sess-p5a"
        )
        self.seed_delivered_conversation(
            conversation_id="cnv-p5b", task_id="tsk-p5b", user_id=USER_B, agent_session_id="sess-p5b"
        )
        self.remember_memory(user_id=USER_A)
        self.remember_memory(user_id=USER_B)

        self.store.record_decision(
            require_enabled_account=True,
            user_id=USER_A,
            row=_row(),
            reason="daily_permission_refresh",
            decided_at=NOW,
            clear_delivered_content=True,
        )

        self.assertIsNone(self.delivered_content(task_id="tsk-p5a"))
        self.assertEqual(self.delivered_content(task_id="tsk-p5b"), "已送达的答案")
        self.assertEqual(self.agent_session_id_of(conversation_id="cnv-p5b"), "sess-p5b")
        self.assertEqual(self.pending_cleanup_reasons(agent_session_id="sess-p5b"), [])
        self.assertEqual(self.memory_count(user_id=USER_A), 0)
        self.assertEqual(self.memory_count(user_id=USER_B), 1, "B 的记忆不受 A 的权限变化影响")


class PermissionsUnchangedDoesNotPurgeTest(PermissionPublishPostgresTestCase):
    """已送达正文清理触发的收窄（Trace #328 opus 审查 P1）：ENQUEUED 不等于"这个人
    的实际可用权限变了"——只有 ``permissions`` 列文本真的变化时才该清理，见
    ``adapters/postgres_permission_publish.py::record_decision`` 文档第 4 步与
    :func:`~lingxi.adapters.postgres_permission_publish._permissions_changed`。"""

    def seed_delivered_conversation(
        self, *, conversation_id: str, task_id: str, user_id: str, agent_session_id: str
    ) -> None:
        # 与 DeliveredContentPurgeTest 同一内容，各自一份——unittest 按类收集用例，
        # 继承会让同一批用例在两个类名下各跑一次（同一条既有理由）。
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO conversation
                       (id, user_id, feishu_chat_id, feishu_thread_id, agent_session_id)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    conversation_id,
                    user_id,
                    f"chat-{conversation_id}",
                    f"topic-{conversation_id}",
                    agent_session_id,
                ),
            )
            cursor.execute(
                """INSERT INTO task
                       (id, conversation_id, user_id, inbound_event_id, prompt, status,
                        target_worker_version, attempts, content_expires_at)
                   VALUES (%s, %s, %s, %s, '问题', 'awaiting_delivery', 'stable', 1, now())""",
                (task_id, conversation_id, user_id, f"event-{task_id}"),
            )
            cursor.execute(
                """INSERT INTO task_delivery_event
                       (id, task_id, sequence, event_type, terminal_kind, worker_id,
                        idempotency_key, platform_received_at, content)
                   VALUES (%s, %s, 1, 'terminal', 'success', 'worker-1', %s, now(), '已送达的答案')""",
                (new_id("tde"), task_id, f"{task_id}:terminal"),
            )

    def delivered_content(self, *, task_id: str) -> str | None:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT content FROM task_delivery_event WHERE task_id = %s", (task_id,))
            return cursor.fetchone()[0]

    def test_a_rename_that_keeps_the_same_permissions_does_not_purge(self) -> None:
        """否定用例①「改名不清」：``email``/``name`` 变了导致**整行**判定为"变化"
        （ENQUEUED），但 ``permissions`` 列文本一个字符都没动，不该清理。"""

        self.store.record_decision(
            require_enabled_account=True,
            user_id=USER_A, row=_row(), reason="daily_permission_refresh", decided_at=NOW
        )
        self.seed_delivered_conversation(
            conversation_id="cnv-rename",
            task_id="tsk-rename",
            user_id=USER_A,
            agent_session_id="sess-rename",
        )
        renamed_row = PublishRow(
            record_key=EMAIL_A,
            email=EMAIL_A,
            name="化名甲（改名后）",
            permissions='{"1011":["商务"]}',  # 与第一次决定逐字节相同
            status="approved",
            updated_at="2026-08-18T03:00:00Z",
        )

        decision = self.store.record_decision(
            require_enabled_account=True,
            user_id=USER_A,
            row=renamed_row,
            reason="daily_permission_refresh",
            decided_at=NOW + timedelta(days=1),
            clear_delivered_content=True,
        )

        self.assertEqual(
            decision.outcome, DecisionOutcome.ENQUEUED, "改名导致整行内容不同，仍应排新意图"
        )
        self.assertEqual(decision.cleared_events, 0, "但 permissions 没变，不该清理")
        self.assertEqual(self.delivered_content(task_id="tsk-rename"), "已送达的答案")

    def test_a_retry_with_identical_content_after_a_failure_does_not_purge(self) -> None:
        """否定用例②「重排不清」：上一条意图已经 ``failed``，重排同样的
        ``permissions`` 内容——这是重试，不是变化，不该清理。"""

        first = self.store.record_decision(
            require_enabled_account=True,
            user_id=USER_A, row=_row(), reason="daily_permission_refresh", decided_at=NOW
        )
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE publish_outbox SET status = 'failed' WHERE id = %s", (first.outbox_id,)
            )
        self.seed_delivered_conversation(
            conversation_id="cnv-retry",
            task_id="tsk-retry",
            user_id=USER_A,
            agent_session_id="sess-retry",
        )

        decision = self.store.record_decision(
            require_enabled_account=True,
            user_id=USER_A,
            row=_row(),  # 与第一次决定逐字节相同
            reason="daily_permission_refresh",
            decided_at=NOW + timedelta(days=1),
            clear_delivered_content=True,
        )

        self.assertEqual(
            decision.outcome,
            DecisionOutcome.ENQUEUED,
            "failed 意图重排，内容相同也照常排新意图（既有语义，本卡不改）",
        )
        self.assertEqual(decision.cleared_events, 0, "但 permissions 没变，这是重试不是变化，不该清理")
        self.assertEqual(self.delivered_content(task_id="tsk-retry"), "已送达的答案")

    def test_an_actual_permission_change_still_purges(self) -> None:
        """正向不回归：``permissions`` 内容确实变化时，清理照常发生——收窄判据不能
        误伤真正的权限变化。"""

        self.store.record_decision(
            require_enabled_account=True,
            user_id=USER_A, row=_row(), reason="daily_permission_refresh", decided_at=NOW
        )
        self.seed_delivered_conversation(
            conversation_id="cnv-real-change",
            task_id="tsk-real-change",
            user_id=USER_A,
            agent_session_id="sess-real-change",
        )

        decision = self.store.record_decision(
            require_enabled_account=True,
            user_id=USER_A,
            row=_row(permissions='{"1011":["商务","日活"]}'),
            reason="daily_permission_refresh",
            decided_at=NOW + timedelta(days=1),
            clear_delivered_content=True,
        )

        self.assertEqual(decision.outcome, DecisionOutcome.ENQUEUED)
        self.assertEqual(decision.cleared_events, 1)
        self.assertIsNone(self.delivered_content(task_id="tsk-real-change"))


class TransientFailureRealDbTest(PermissionPublishPostgresTestCase):
    """撞锁记响亮审计不静默丢当日变更（Trace #328 opus 审查 P1，"照停用路径已有的
    捕获形状"，对照 ``tests/test_pending_action_postgres.py::
    TransientFailureRealDbTests``）：``record_decision`` 第一步 ``SELECT ...
    FOR UPDATE`` 锁 ``app_user`` 行，若这一行恰好被另一个事务持锁超过
    ``lock_timeout``（2 秒，``adapters/postgres.py`` 的 ``PostgresTimeouts``
    默认值），必须把裸 psycopg 的 ``LockNotAvailable`` 转译成
    :class:`~lingxi.core.permission.publish.PermissionDecisionTransientFailure`
    （事务已回滚、可重试），不能让它一路抛到调用方、也不能无界等待。"""

    def test_record_decision_raises_transient_failure_when_the_user_row_is_held_past_lock_timeout(
        self,
    ) -> None:
        lock_acquired = threading.Event()
        release_lock = threading.Event()
        holder_errors: list[BaseException] = []

        def hold_the_user_row_lock() -> None:
            # 模拟一个并发事务恰好长时间持有该用户的 app_user 行锁（真实场景是
            # 另一次权限决定或建档写入意外变慢），不提交也不回滚，直到测试主动释放。
            try:
                with connect(self._dsn) as connection:
                    with connection.transaction():
                        with connection.cursor() as cursor:
                            cursor.execute(
                                "SELECT id FROM app_user WHERE id = %s FOR UPDATE", (USER_A,)
                            )
                            cursor.fetchone()
                            lock_acquired.set()
                            release_lock.wait(timeout=10)
            except BaseException as error:  # noqa: BLE001 - 收集到主线程再断言
                holder_errors.append(error)

        holder = threading.Thread(target=hold_the_user_row_lock)
        holder.start()
        self.assertTrue(lock_acquired.wait(timeout=5), "持锁线程未能及时拿到 app_user 行锁")

        started_at = time.monotonic()
        try:
            with self.assertRaises(PermissionDecisionTransientFailure) as raised:
                self.store.record_decision(
                    require_enabled_account=True,
                    user_id=USER_A,
                    row=_row(),
                    reason="daily_permission_refresh",
                    decided_at=NOW,
                )
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
        # 事务已整体回滚：版本没有推进，也没有留下任何孤立的发布意图。
        self.assertEqual(self._version(), 0)
        self.assertEqual(self._count(), 0)


class ClaimTest(PermissionPublishPostgresTestCase):
    def test_claim_returns_the_current_permission_version(self) -> None:
        decision = self.store.record_decision(
            require_enabled_account=True,
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

        self.store.record_decision(user_id=USER_A, row=_row(), reason="x", decided_at=NOW, require_enabled_account=True)
        self.store.record_decision(
            require_enabled_account=True,
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
        self.store.record_decision(user_id=USER_B, row=_row(EMAIL_B), reason="x", decided_at=NOW, require_enabled_account=True)
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

        first = self.store.record_decision(user_id=USER_A, row=_row(), reason="x", decided_at=NOW, require_enabled_account=True)
        self.store.record_decision(
            require_enabled_account=True,
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

        first = self.store.record_decision(user_id=USER_A, row=_row(), reason="x", decided_at=NOW, require_enabled_account=True)
        second = self.store.record_decision(
            require_enabled_account=True,
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
        decision = self.store.record_decision(user_id=USER_A, row=_row(), reason="x", decided_at=NOW, require_enabled_account=True)
        self.store.claim_next()
        stored = self.store.load(decision.outbox_id or "")
        assert stored is not None
        self.assertEqual(stored.status, "publishing")
        self.assertEqual(stored.attempts, 1)

    def test_empty_queue_returns_none(self) -> None:
        self.assertIsNone(self.store.claim_next())


class CompleteTest(PermissionPublishPostgresTestCase):
    def _claimed(self) -> str:
        decision = self.store.record_decision(user_id=USER_A, row=_row(), reason="x", decided_at=NOW, require_enabled_account=True)
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
        self.store.record_decision(user_id=USER_A, row=_row(), reason="x", decided_at=NOW, require_enabled_account=True)
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT content_expires_at - created_at FROM publish_outbox LIMIT 1"
            )
            self.assertEqual(cursor.fetchone()[0], timedelta(days=90))

    def test_caller_supplied_expiry_is_ignored(self) -> None:
        decision = self.store.record_decision(user_id=USER_A, row=_row(), reason="x", decided_at=NOW, require_enabled_account=True)
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
        decision = self.store.record_decision(user_id=USER_A, row=_row(), reason="x", decided_at=NOW, require_enabled_account=True)
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
        decision = self.store.record_decision(user_id=USER_A, row=_row(), reason="x", decided_at=NOW, require_enabled_account=True)
        with self.assertRaises(Exception):
            with connect(self._dsn) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE publish_outbox SET status = 'published' WHERE id = %s",
                    (decision.outbox_id,),
                )

    def test_expired_payload_is_redacted_but_the_run_facts_survive(self) -> None:
        decision = self.store.record_decision(user_id=USER_A, row=_row(), reason="x", decided_at=NOW, require_enabled_account=True)
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

    def test_digests_survive_redaction_and_keep_unchanged_unchanged(self) -> None:
        """P-3（Trace #544）：**擦除之后，同内容仍然判 UNCHANGED**。

        修复前：擦成 ``'{}'`` 的 payload 让 ``_same_content`` 恒假、
        ``_permissions_changed`` 恒真——一份内容完全没变的权限被重排一条发布意图，
        并按「权限变化感知即清」把该用户的 ``user_memory`` 与已送达正文清空。用户侧
        表现为"什么都没做，记忆和历史答案却没了"。摘要列不参与擦除，判据因此还成立。
        """

        row = _row()
        first = self.store.record_decision(
            user_id=USER_A, row=row, reason="x", decided_at=NOW, require_enabled_account=True
        )

        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT content_expires_at, content_digest, permissions_digest "
                "FROM publish_outbox WHERE id = %s",
                (first.outbox_id,),
            )
            expires, content_before, permissions_before = cursor.fetchone()

        # 入队时就算好、且非空——不是"读的时候现算"。
        self.assertTrue(content_before)
        self.assertTrue(permissions_before)

        self.assertEqual(self.store.redact_expired_payloads(now=expires), 1)

        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT payload, content_digest, permissions_digest "
                "FROM publish_outbox WHERE id = %s",
                (first.outbox_id,),
            )
            payload, content_after, permissions_after = cursor.fetchone()

        # 内容没了，摘要还在。
        self.assertEqual(payload, {})
        self.assertEqual(content_after, content_before)
        self.assertEqual(permissions_after, permissions_before)

        second = self.store.record_decision(
            user_id=USER_A,
            row=row,
            reason="x",
            decided_at=NOW,
            require_enabled_account=True,
            clear_delivered_content=True,
        )

        self.assertIs(second.outcome, DecisionOutcome.UNCHANGED)
        self.assertEqual(second.cleared_events, 0)
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM publish_outbox WHERE user_id = %s", (USER_A,))
            self.assertEqual(cursor.fetchone()[0], 1, "同内容不得排出第二条意图")

    def test_a_real_permission_change_after_redaction_is_still_detected(self) -> None:
        """否定断言的另一半：擦除之后**真的变了**仍然要判成变化并重排意图——
        摘要修的是误判，不是把判定整体关掉。"""

        first = self.store.record_decision(
            user_id=USER_A, row=_row(), reason="x", decided_at=NOW, require_enabled_account=True
        )
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT content_expires_at FROM publish_outbox WHERE id = %s", (first.outbox_id,)
            )
            expires = cursor.fetchone()[0]
        self.assertEqual(self.store.redact_expired_payloads(now=expires), 1)

        changed = _row(permissions='{"1011": ["another_metric"]}')
        second = self.store.record_decision(
            user_id=USER_A,
            row=changed,
            reason="x",
            decided_at=NOW,
            require_enabled_account=True,
        )

        self.assertIs(second.outcome, DecisionOutcome.ENQUEUED)


class AwaitingReadinessTest(PermissionPublishPostgresTestCase):
    """S-C-03b 的每轮 tick 输入：**只有已发布、且确认还没收口**的那一版。

    只有真库能证伪：三条筛选（已发布 / 没有更新版本 / 没有终态就绪记录）写在一条 SQL
    里，其中第三条还跨读 ``mcp_sync_check``。在假 store 上跑，无论谓词怎么写都是绿的。

    否定面：``pending`` 的意图取不到（**撤权通知只在读回一致之后发**的落点）；被更新
    版本取代的旧意图取不到；已经收口（ready / no_permission / timed_out）的取不到；
    内容擦除后的行取不到。
    """

    #: 本职责负责确认的 reason 集合（``apps/scheduler/permission_publish.FOLLOW_UP_REASONS``）。
    #: 写成字面量而不是 import：这两条字符串是**跨模块的数据契约**，用例里照抄一份，
    #: 才能在有人改了常量却忘了改 SQL 语义时变红。
    OWNED_REASONS = ("daily_permission_refresh", "daily_permission_revoke")

    #: 就绪节奏（合同值）：到期判据必须与 `ReadinessSchedule` 同源。
    INTERVAL_SECONDS = 180
    BUDGET_SECONDS = 900

    def _candidates(self, *, limit: int = 50, interval: int | None = None):
        return self.store.published_awaiting_readiness(
            reasons=self.OWNED_REASONS,
            interval_seconds=interval or self.INTERVAL_SECONDS,
            budget_seconds=self.BUDGET_SECONDS,
            limit=limit,
        )

    def _publish(
        self, *, user_id: str = USER_A, row: PublishRow | None = None, reason: str = "daily_permission_refresh"
    ) -> str:
        decision = self.store.record_decision(
            require_enabled_account=True,
            user_id=user_id, row=row or _row(), reason=reason, decided_at=NOW
        )
        claimed = self.store.claim_next()
        assert claimed is not None
        self.store.complete(
            _attempt(
                claimed.outbox_id,
                version=claimed.permission_version,
                user_id=user_id,
            ),
            status=STATUS_PUBLISHED,
        )
        return decision.outbox_id or ""

    def _record_check(
        self, user_id: str, version: int, result: str, *, at: datetime | None = None
    ) -> None:
        """写一条就绪判定行。

        ``at`` 默认取 :data:`NOW`（一个**过去**的时刻），这样"下一次探针到期了没有"
        在多数用例里恒为真；要表达"刚刚才探过"的用例显式传当前时刻。
        """

        from lingxi.adapters.postgres_mcp_token import PostgresMcpTokenStore
        from lingxi.core.permission.mcp_readiness import (
            ReadinessAttempt,
            ReadinessBinding,
            ReadinessOutcome,
        )

        outcome = ReadinessOutcome(result)
        extra: dict = {}
        if outcome is ReadinessOutcome.READY:
            extra = {"metric_count": 2}
        elif outcome is ReadinessOutcome.WAITING:
            extra = {"error_code": "empty_metrics", "metric_count": 0}
        else:
            extra = {"error_code": "budget_exhausted"}
        moment = at or NOW
        PostgresMcpTokenStore(self._dsn, cipher=McpTokenCipher(SPEC_MASTER_KEY)).record_attempt(
            ReadinessAttempt(
                binding=ReadinessBinding(user_id, version),
                attempt_no=1,
                outcome=outcome,
                started_at=moment,
                finished_at=moment,
                **extra,
            )
        )

    def test_a_published_intent_is_a_candidate(self) -> None:
        self._publish()

        pending = self._candidates()

        self.assertEqual([item.user_id for item in pending], [USER_A])
        self.assertEqual(pending[0].permission_version, 1)
        self.assertEqual(pending[0].permissions, '{"1011":["商务"]}')

    def test_a_pending_intent_is_not_a_candidate(self) -> None:
        """否定断言：**还没发布读回一致的意图不会触发任何通知。**"""

        self.store.record_decision(
            require_enabled_account=True,
            user_id=USER_A, row=_row(), reason="daily_permission_refresh", decided_at=NOW
        )

        self.assertEqual(self._candidates(), ())

    def test_an_intent_owned_by_another_orchestrator_is_not_a_candidate(self) -> None:
        """否定断言：**首次开通那条意图不归每日刷新这一侧确认**。

        它由 Epic D 的开通编排自己确认并发"开通完成"。两边都捞会让一个刚开通的用户
        再收到一条措辞完全不同的"可用范围已更新"，两个确认还会并发探同一个人。
        """

        self._publish(reason="first_onboarding")

        self.assertEqual(self._candidates(), ())

    def test_the_owned_reasons_must_be_stated(self) -> None:
        for reasons in ((), ("",), ("   ",)):
            with self.subTest(reasons):
                with self.assertRaises(ValueError):
                    self.store.published_awaiting_readiness(
                        reasons=reasons,
                        interval_seconds=self.INTERVAL_SECONDS,
                        budget_seconds=self.BUDGET_SECONDS,
                    )

    def test_an_intent_superseded_by_a_newer_version_is_not_a_candidate(self) -> None:
        """否定断言：权限又变了时，**旧的那一版不再确认、也不据它通知**。"""

        self._publish()
        self.store.record_decision(
            require_enabled_account=True,
            user_id=USER_A,
            row=_row(permissions='{"1011":["商务","财务"]}'),
            reason="daily_permission_refresh",
            decided_at=NOW,
        )

        self.assertEqual(self._candidates(), ())

    def test_a_terminal_readiness_record_removes_the_candidate(self) -> None:
        """终态就是"这条变化处理过了"的唯一水位——它同时保证通知只发一次。"""

        for result in ("ready", "no_permission", "timed_out"):
            with self.subTest(result):
                self.setUp()
                self._publish()
                self.assertEqual(len(self._candidates()), 1)

                self._record_check(USER_A, 1, result)

                self.assertEqual(self._candidates(), ())

    def test_an_intermediate_readiness_record_keeps_the_candidate(self) -> None:
        self._publish()

        self._record_check(USER_A, 1, "waiting")

        self.assertEqual(len(self._candidates()), 1)

    def test_a_redacted_payload_is_not_a_candidate(self) -> None:
        """擦过的快照说不出"当时发布的范围是什么"，据它渲染通知只会渲染出一句错话。"""

        self._publish()
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT content_expires_at FROM publish_outbox WHERE user_id = %s", (USER_A,)
            )
            expires = cursor.fetchone()[0]
        self.assertEqual(self.store.redact_expired_payloads(now=expires), 1)

        self.assertEqual(self._candidates(), ())

    def test_a_confirmation_that_is_not_due_is_left_out_of_the_window(self) -> None:
        """N3：**没到期的候选不占窗口**。

        少了这一条，一批"要等三分钟"的候选会每轮占满 ``LIMIT``，新发布的那条永远挤不
        进来——合同要求的"发布读回一致后立即探一次"就名存实亡了。
        """

        self._publish()
        self._record_check(USER_A, 1, "waiting", at=datetime.now(timezone.utc))

        self.assertEqual(self._candidates(), (), "刚探过，三分钟内不该再取回")

    def test_a_confirmation_that_is_due_comes_back(self) -> None:
        """反面：上一次探针已经过了一个间隔，它照常回到窗口里。"""

        self._publish()
        self._record_check(
            USER_A,
            1,
            "waiting",
            at=datetime.now(timezone.utc) - timedelta(seconds=self.INTERVAL_SECONDS + 20),
        )

        self.assertEqual(len(self._candidates()), 1)

    def test_a_never_probed_candidate_comes_first(self) -> None:
        """新发布的排在最前面：它要的是"立即"的第一次探针。"""

        self._publish()
        self._record_check(USER_A, 1, "waiting")
        self._publish(user_id=USER_B, row=_row(EMAIL_B))

        pending = self._candidates()

        self.assertEqual([item.user_id for item in pending], [USER_B, USER_A])

    def test_an_exhausted_plan_is_due_once_the_budget_has_passed(self) -> None:
        """计划表用尽却还没收口的那种行，**过了预算就该被取回来收口**。

        取一个只有"预算封顶"才成立的时刻：起点在 950 秒前，已判定 6 次。
        ``6 × 180 = 1080`` 秒还没到，但预算 900 秒已经过了——少了 ``LEAST(..., 预算)``
        这一层，这条永远没收口的确认要再多等一个间隔才会被取回来。
        """

        self._publish()
        started = datetime.now(timezone.utc) - timedelta(seconds=950)
        for _ in range(6):
            self._record_check(USER_A, 1, "waiting", at=started)

        self.assertEqual(len(self._candidates()), 1)

    def test_an_illegal_schedule_is_rejected(self) -> None:
        for kwargs in ({"interval_seconds": 0}, {"budget_seconds": -1}, {"interval_seconds": True}):
            with self.subTest(kwargs):
                with self.assertRaises(ValueError):
                    self.store.published_awaiting_readiness(
                        reasons=self.OWNED_REASONS,
                        **{
                            "interval_seconds": self.INTERVAL_SECONDS,
                            "budget_seconds": self.BUDGET_SECONDS,
                            **kwargs,
                        },
                    )

    def test_the_budget_is_respected(self) -> None:
        self._publish()
        self._publish(user_id=USER_B, row=_row(EMAIL_B))

        self.assertEqual(len(self._candidates(limit=1)), 1)
        self.assertEqual(len(self._candidates(limit=5)), 2)

    def test_an_illegal_budget_is_rejected(self) -> None:
        for limit in (0, -1, True):
            with self.subTest(limit):
                with self.assertRaises(ValueError):
                    self._candidates(limit=limit)


class PublishHistoryAndRecipientTest(PermissionPublishPostgresTestCase):
    """撤权判据与通知收件人：两条只读查询的真库形状。"""

    def _activate(self, user_id: str = USER_A, *, account_state: str = "enabled") -> None:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE app_user SET provisioning_state = 'active', account_state = %s WHERE id = %s",
                (account_state, user_id),
            )

    def test_a_user_with_no_intent_at_all_has_no_footprint(self) -> None:
        self.assertFalse(self.store.has_publish_footprint(USER_A))

    def test_a_pending_intent_already_counts_as_a_footprint(self) -> None:
        """N7：**在途的旧授权意图也算足迹**。

        不算的话，昨天排的 granted 意图今天被跳过，等发布面消费积压时那份**已经被
        收回的范围**会被写进外部表并触发一条"范围已更新"通知。算进来之后，撤权决定
        推进版本，旧意图被认领时判 ``SUPERSEDED``，一次外部调用都不发。
        """

        self.store.record_decision(
            require_enabled_account=True,
            user_id=USER_A, row=_row(), reason="daily_permission_refresh", decided_at=NOW
        )

        self.assertTrue(self.store.has_publish_footprint(USER_A))

    def test_a_claimed_intent_counts_too(self) -> None:
        self.store.record_decision(
            require_enabled_account=True,
            user_id=USER_A, row=_row(), reason="daily_permission_refresh", decided_at=NOW
        )
        self.store.claim_next()

        self.assertTrue(self.store.has_publish_footprint(USER_A), "publishing 也是在途")

    def test_a_published_intent_makes_the_history_true(self) -> None:
        decision = self.store.record_decision(user_id=USER_A, row=_row(), reason="x", decided_at=NOW, require_enabled_account=True)
        self.store.claim_next()
        self.store.complete(_attempt(decision.outbox_id or ""), status=STATUS_PUBLISHED)

        self.assertTrue(self.store.has_publish_footprint(USER_A))
        self.assertFalse(self.store.has_publish_footprint(USER_B), "只看这个人自己的历史")

    def test_a_failed_intent_does_not_count_as_a_footprint(self) -> None:
        """否定断言：``failed`` **不算足迹**——那一版从来没有落到外部表。"""

        decision = self.store.record_decision(user_id=USER_A, row=_row(), reason="x", decided_at=NOW, require_enabled_account=True)
        self.store.claim_next()
        self.store.complete(
            _attempt(decision.outbox_id or "", outcome=PublishOutcome.CONFLICT, record_id=None),
            status=STATUS_FAILED,
        )

        self.assertFalse(self.store.has_publish_footprint(USER_A))

    def test_an_active_user_has_a_notice_recipient(self) -> None:
        self._activate()

        self.assertEqual(self.store.notice_recipient_open_id(USER_A), f"ou_{USER_A}")

    def test_a_user_who_is_not_yet_active_gets_no_notice(self) -> None:
        """否定断言：还没完成开通的人不该收到"你的可用范围已更新"。"""

        self.assertIsNone(self.store.notice_recipient_open_id(USER_A))

    def test_a_deleting_deleted_or_suspended_account_gets_no_notice(self) -> None:
        """Issue #468（2026-08-30）：停用期间同样不该收到"你的可用范围已更新"。"""

        for state in ("deleting", "deleted", "suspended"):
            with self.subTest(state):
                self._activate(account_state=state)
                self.assertIsNone(self.store.notice_recipient_open_id(USER_A))

    def test_an_unknown_user_gets_no_notice(self) -> None:
        self.assertIsNone(self.store.notice_recipient_open_id("usr_does_not_exist"))

    def test_both_queries_require_a_user(self) -> None:
        for call in (self.store.has_publish_footprint, self.store.notice_recipient_open_id):
            for value in ("", "   "):
                with self.subTest(call=call.__name__, value=value):
                    with self.assertRaises(ValueError):
                        call(value)


class _AlwaysFailingTable:
    """恒定快失败的发布表替身：一次 ``find_rows`` 就明确拒绝。

    对应真库实测里烧掉重试额度最快的那种形态（http_500 / 飞书业务错误码非 0）——它
    **不带任何自然节流**，因此"同一轮内被立刻重认领"在它身上最刺眼。
    """

    def __init__(self) -> None:
        self.calls = 0

    def find_rows(self, *, record_key: str, email: str):
        del record_key, email
        self.calls += 1
        raise PermissionTableError("http_500", definite=True)

    def create_row(self, fields):  # pragma: no cover - 走不到
        raise AssertionError("恒失败的替身不应被要求新建")

    def update_row(self, record_id, fields):  # pragma: no cover - 走不到
        raise AssertionError("恒失败的替身不应被要求更新")

    def read_row(self, record_id):  # pragma: no cover - 走不到
        raise AssertionError("恒失败的替身不应被要求读回")


class RoundExclusionPostgresTest(PermissionPublishPostgresTestCase):
    """**一条可重试失败的意图，一轮只认领一次**（Epic C 冻结缺陷 F2）。

    只有真库能证伪它：成因在 ``claim_next`` 的 SQL 里——失败只把状态写回 ``pending``，
    ``created_at`` 一个字不动，于是它仍然是 ``ORDER BY created_at, id`` 的第一名。修复
    前的真库实测是 5 次重试在 **0.195 秒**内烧完转 ``failed``；本组把那条时间线钉成
    "每轮 +1、第 5 轮才终态"。
    """

    def _executor(self, table) -> PermissionPublishExecutor:
        return PermissionPublishExecutor(
            store=self.store, transport=table, audit=_SilentAudit()
        )

    def _intent(self, user_id: str = USER_A, email: str = EMAIL_A) -> str:
        decision = self.store.record_decision(
            require_enabled_account=True,
            user_id=user_id,
            row=_row(email, token_cipher=TOKEN_CIPHER),
            reason="permission_refresh",
            decided_at=NOW,
        )
        assert decision.outbox_id is not None
        return decision.outbox_id

    def test_one_round_claims_a_failing_intent_exactly_once(self) -> None:
        outbox_id = self._intent()
        table = _AlwaysFailingTable()
        executor = self._executor(table)

        attempts = executor.run_once(limit=DEFAULT_PUBLISH_LIMIT)

        self.assertEqual(len(attempts), 1, "一轮只认领一次；修复前这里是 5")
        self.assertEqual(table.calls, 1, "一轮只对外发一次调用")
        stored = self.store.load(outbox_id)
        self.assertEqual(stored.status, "pending", "还能重试，回 pending 等下一轮")
        self.assertEqual(stored.attempts, 1)

    def test_the_attempt_budget_is_spent_one_round_at_a_time(self) -> None:
        """``attempts`` 每轮 +1，**第 5 轮**才转 ``failed``。

        这正是 ``DEFAULT_MAX_ATTEMPTS`` 注释里"与每轮消费节奏配套"那句话的字面含义；
        修复前 5 轮的额度全部消耗在第一轮里。
        """

        outbox_id = self._intent()
        executor = self._executor(_AlwaysFailingTable())

        for round_number in range(1, 5):
            with self.subTest(round=round_number):
                self.assertEqual(len(executor.run_once(limit=DEFAULT_PUBLISH_LIMIT)), 1)
                stored = self.store.load(outbox_id)
                self.assertEqual(stored.attempts, round_number)
                self.assertEqual(stored.status, "pending")

        self.assertEqual(len(executor.run_once(limit=DEFAULT_PUBLISH_LIMIT)), 1)
        stored = self.store.load(outbox_id)
        self.assertEqual(stored.attempts, DEFAULT_MAX_ATTEMPTS)
        self.assertEqual(stored.status, "failed", "第 5 轮才转终态")

        # 终态之后不再被认领：一轮什么都取不到。
        self.assertEqual(executor.run_once(limit=DEFAULT_PUBLISH_LIMIT), ())

    def test_skipping_one_intent_does_not_starve_the_others(self) -> None:
        """否定断言：**跳过不等于少消费**。

        本轮排除只把已经认领过的那条排除在候选之外。少了这一条，"修好 F2"就会造出
        一个更糟的缺陷：一个人失败，本轮剩下的预算全部空转。
        """

        first = self._intent(USER_A, EMAIL_A)
        second = self._intent(USER_B, EMAIL_B)
        table = _AlwaysFailingTable()

        attempts = self._executor(table).run_once(limit=DEFAULT_PUBLISH_LIMIT)

        self.assertEqual(
            sorted(item.outbox_id for item in attempts),
            sorted((first, second)),
            "两个人各被认领一次",
        )
        self.assertEqual(table.calls, 2)
        for outbox_id in (first, second):
            self.assertEqual(self.store.load(outbox_id).attempts, 1)

    def test_the_exclusion_does_not_break_single_flight(self) -> None:
        """否定断言：排除只作用在**候选**上，不作用在"同一用户单飞"那条判据上。

        被跳过的 v1 仍然是 ``pending``、仍然是非终态，因此仍然挡着同一个人的 v2。
        把它从兄弟行判据里一并排除，会让同一轮里同一个人的两条意图一起被认领——v1 的
        写入落后到 v2 之后就把已经收回的权限盖了回来。

        断在 ``claim_next`` 上而不是执行器上：执行器那一层看不出这条性质——v1 被
        认领后会因为版本落后判 ``superseded``（一次外部调用都不发）并转终态，v2 于是
        合法地在同一轮里接着出去。要证伪的是认领语句本身。
        """

        first = self._intent(USER_A, EMAIL_A)
        self.store.record_decision(
            require_enabled_account=True,
            user_id=USER_A,
            row=_row(EMAIL_A, permissions='{"1012":["财务"]}', token_cipher=TOKEN_CIPHER),
            reason="permission_refresh",
            decided_at=NOW + timedelta(minutes=1),
        )

        self.assertEqual(self.store.claim_next().outbox_id, first, "先出去的是更老的那一条")
        self.assertIsNone(
            self.store.claim_next(exclude=(first,)),
            "被跳过的 v1 仍是非终态，仍然挡着同一个人的 v2",
        )

    def test_the_next_round_claims_it_again(self) -> None:
        """排除的作用域是**一轮**——否则重试永远不会发生。"""

        outbox_id = self._intent()
        executor = self._executor(_AlwaysFailingTable())

        executor.run_once(limit=DEFAULT_PUBLISH_LIMIT)
        executor.run_once(limit=DEFAULT_PUBLISH_LIMIT)

        self.assertEqual(self.store.load(outbox_id).attempts, 2)

    def test_an_explicit_exclusion_is_honoured_by_the_sql(self) -> None:
        """``exclude`` 直接作用在认领语句里，不是在 Python 侧过滤出来的。"""

        outbox_id = self._intent()

        self.assertIsNone(self.store.claim_next(exclude=(outbox_id,)))
        self.assertEqual(self.store.load(outbox_id).attempts, 0, "跳过的那条一次都没记账")
        claimed = self.store.claim_next(exclude=())
        self.assertEqual(claimed.outbox_id, outbox_id)


class _SilentAudit:
    def record(self, action: str, /, **fields: object) -> None:
        del action, fields


class AccountStateGuardTest(PermissionPublishPostgresTestCase):
    """Issue #483：落权限决定的那把行锁里复检账号状态。

    只有真库能证伪的那几条：复检读的是 ``SELECT ... FOR UPDATE`` **同一条语句**多取
    的一列，被挡时事务**整体回滚**（版本不推进、outbox 不多行、送达正文不清）。假
    store 上这几条无论实现怎么写都是绿的。

    夹具里的 ``USER_A`` 建档时不写这两个状态列，取数据库默认值
    （``account_state='enabled'``、``provisioning_state='guest'``），用例按需翻转。
    """

    def test_a_grant_for_a_suspended_user_is_blocked_and_writes_nothing(self) -> None:
        """核心断言：声明"需要账号有效"时，停用用户拿不到任何授权，且**零写入**。"""

        self._set_states(account_state="suspended")

        with self.assertRaises(PermissionGrantBlockedByAccountState) as raised:
            self.store.record_decision(
                user_id=USER_A,
                row=_row(),
                reason="daily_permission_refresh",
                require_enabled_account=True,
                decided_at=NOW,
            )

        self.assertEqual(raised.exception.account_state, "suspended")
        # 事务整体回滚：版本没推进、outbox 一行都没有。没有这一半，"被挡"可能只是
        # 抛异常之前已经写了半套状态。
        self.assertEqual(self._version(), 0, "被挡的决定不得推进权限版本")
        self.assertEqual(self._count(), 0, "被挡的决定不得留下任何发布意图")

    def test_the_other_two_terminal_account_states_are_blocked_too(self) -> None:
        """白名单而不是"只挡 suspended"：删除中/已删除同样不得拿到非空授权。

        两个状态各用夹具里的一个用户，不复用同一行——同一行连续翻两次状态会让"第二次
        到底有没有真的被挡"取决于第一次留下的库状态。
        """

        for user_id, email, account_state in (
            (USER_A, EMAIL_A, "deleting"),
            (USER_B, EMAIL_B, "deleted"),
        ):
            with self.subTest(account_state=account_state):
                self._set_states(user_id, account_state=account_state)
                with self.assertRaises(PermissionGrantBlockedByAccountState) as raised:
                    self.store.record_decision(
                        user_id=user_id,
                        row=_row(email),
                        reason="daily_permission_refresh",
                        require_enabled_account=True,
                        decided_at=NOW,
                    )
                self.assertEqual(raised.exception.account_state, account_state)
                self.assertEqual(self._version(user_id), 0)
        self.assertEqual(self._count(), 0, "两次被挡都是零写入")

    def test_a_grant_is_not_blocked_while_the_user_is_still_provisioning(self) -> None:
        """**反向断言，防停服级误伤**：复检只看 ``account_state``，绝不带
        ``provisioning_state``。

        首次开通调用 ``record_decision`` 时这个人还是 ``provisioning``（推进到
        ``mcp_syncing`` 发生在发布**之后**，见 ``core/identity/onboarding_runner.py``
        的 ``_publish`` 调用次序）。守卫里只要多带一个 ``provisioning_state='active'``，
        首次开通就会 100% 全部失败——那是比原缺陷更响的停服级误伤。
        """

        self._set_states(account_state="enabled", provisioning_state="provisioning")

        decision = self.store.record_decision(
            user_id=USER_A,
            row=_row(token_cipher=TOKEN_CIPHER),
            reason="first_onboarding",
            require_enabled_account=True,
            decided_at=NOW,
        )

        self.assertEqual(decision.outcome, DecisionOutcome.ENQUEUED)
        self.assertEqual(self._version(), 1)
        self.assertEqual(self._count(), 1)

    def test_a_revocation_for_a_suspended_user_is_never_blocked(self) -> None:
        """**方向相反、后果最严重的那一处**：撤权必须照常放行。

        ``force_revoke`` 的服务对象本来就是刚被管理员停用的人——守卫如果把撤权也挡了，
        「停用」就彻底失效，比原缺陷更糟。
        """

        self._set_states(account_state="suspended")

        decision = self.store.record_decision(
            user_id=USER_A,
            row=build_revocation_row(email=EMAIL_A, display_name="化名甲", decided_at=NOW),
            reason="admin_action_instant_revoke",
            require_enabled_account=False,
            decided_at=NOW,
        )

        self.assertEqual(decision.outcome, DecisionOutcome.ENQUEUED)
        self.assertEqual(self._version(), 1)
        stored = self.store.load(decision.outbox_id or "")
        assert stored is not None
        self.assertEqual(stored.payload["permissions"], "{}")

    def test_declaring_no_account_check_for_a_non_empty_row_is_refused(self) -> None:
        """声明与内容自洽：传 ``False`` 却排非空授权，在**进事务之前**就被拒。

        这条让"未来某个调用点把授权声明成撤权"从运行期就写不出来——守卫因此不依赖
        调用方声明得对，只依赖它声明得自洽。
        """

        with self.assertRaises(ValueError):
            self.store.record_decision(
                user_id=USER_A,
                row=_row(),
                reason="daily_permission_refresh",
                require_enabled_account=False,
                decided_at=NOW,
            )

        self.assertEqual(self._version(), 0)
        self.assertEqual(self._count(), 0)

    def test_the_declaration_is_a_required_keyword(self) -> None:
        """必填而不是给默认值：忘了传就当场 ``TypeError``，不会静默按"不检查"跑。

        本仓为这条纪律付过一次学费——``merge_permission_sources(full_access_
        wildcard=...)`` 的默认值曾是一次真实漏接的根因（Trace #445）。
        """

        with self.assertRaises(TypeError):
            self.store.record_decision(  # type: ignore[call-arg]
                user_id=USER_A, row=_row(), reason="daily_permission_refresh", decided_at=NOW
            )


class AccountStateGuardConcurrencyTest(PermissionPublishPostgresTestCase):
    """Issue #483 P1-1（rc24 缺陷账本 #520 F7）：**真正并发**的那一半。

    ``AccountStateGuardTest`` 的每一条都是"先把状态改成 suspended 并提交，再落
    决定"——那证明的是"读到 suspended 会挡"，不是"挡住了竞态"。#483 的产品承诺
    比这强一级（``record_decision`` 文档「这是消除竞态，不是缩小窗口」一节）：
    管理员的停用写入与这次权限决定抢的是 **同一行 ``app_user`` 的同一把
    ``FOR UPDATE`` 锁**，两个写入者必然串行，先到者提交之后后到者读到的一定是
    提交后的状态。

    这条用例造的正是那个窗口：事务 A 先锁住目标行并把 ``account_state`` 翻成
    ``suspended`` 但**不提交**，事务 B 这时才调 ``record_decision(
    require_enabled_account=True)``。B 必须停在 A 的行锁上（用
    ``pg_blocking_pids`` 现场取证，不靠 sleep 猜时序），A 提交之后 B 才继续，
    并且 **B 必须被挡住**——读到的是 A 刚提交的 ``suspended``，不是它进来那一刻
    的 ``enabled``。

    去掉 ``_record_decision_locked`` 那条 ``SELECT ... FOR UPDATE`` 的 ``FOR
    UPDATE`` 之后本用例必须变红：READ COMMITTED 下无锁的 SELECT 直接读到 A 提交
    前的 ``enabled`` 快照，守卫放行，B 一路把版本推进、把非空授权排进 outbox
    ——正是 #483 要消灭的那次"给一个已经被停用的人发权限"。假 store 与单线程
    真库用例都测不出这一条：它是行锁的属性。
    """

    #: A 持锁的上限。B 的 ``lock_timeout`` 是 2 秒（``adapters/postgres.py`` 的
    #: ``DEFAULT_LOCK_TIMEOUT_SECONDS``），A 必须在此之内提交，否则 B 会以
    #: ``PermissionDecisionTransientFailure`` 退出——那是另一条已有用例
    #: （``TransientFailureRealDbTest``）覆盖的形状，不是本条要证的东西。
    HOLD_BUDGET_SECONDS = 1.0

    def _blocked_backend_count(self) -> int:
        """当前库里有多少个后端正被别的事务挡住（``pg_blocking_pids`` 现场取证）。

        用它而不是 ``sleep``：sleep 只能证明"等过一会儿"，证明不了"B 真的撞在 A
        的行锁上"。探测走独立连接、``autocommit``，不会自己参与竞争。
        """

        with connect(self._dsn, autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT count(*)
                     FROM pg_stat_activity
                    WHERE datname = current_database()
                      AND cardinality(pg_blocking_pids(pid)) > 0"""
            )
            return int(cursor.fetchone()[0])

    def test_a_decision_racing_an_uncommitted_suspension_is_blocked_not_let_through(self) -> None:
        suspension_applied = threading.Event()
        allow_commit = threading.Event()
        holder_errors: list[BaseException] = []
        decision_result: list[object] = []
        decision_errors: list[BaseException] = []

        def suspend_without_committing() -> None:
            """事务 A：管理员那次「停用」写入的形状（先锁 ``app_user`` 行、再翻
            ``account_state``），停在提交之前，等主线程确认 B 已经撞上来。"""

            try:
                with connect(self._dsn) as connection:
                    with connection.transaction():
                        with connection.cursor() as cursor:
                            cursor.execute(
                                "SELECT id FROM app_user WHERE id = %s FOR UPDATE", (USER_A,)
                            )
                            cursor.fetchone()
                            cursor.execute(
                                "UPDATE app_user SET account_state = %s, updated_at = now() "
                                "WHERE id = %s",
                                ("suspended", USER_A),
                            )
                            suspension_applied.set()
                            # 事务块正常退出即提交；异常退出才回滚。这里等主线程
                            # 放行，放行时刻就是 A 的提交时刻。
                            allow_commit.wait(timeout=10)
            except BaseException as error:  # noqa: BLE001 - 收集到主线程再断言
                holder_errors.append(error)

        def record_the_decision() -> None:
            """事务 B：一次正常的每日刷新授权，声明"需要账号有效"。"""

            try:
                decision_result.append(
                    self.store.record_decision(
                        user_id=USER_A,
                        row=_row(),
                        reason="daily_permission_refresh",
                        require_enabled_account=True,
                        decided_at=NOW,
                    )
                )
            except BaseException as error:  # noqa: BLE001 - 收集到主线程再断言
                decision_errors.append(error)

        holder = threading.Thread(target=suspend_without_committing, name="uncommitted-suspension")
        holder.start()
        self.assertTrue(
            suspension_applied.wait(timeout=5), "事务 A 未能及时锁住目标行并写入 suspended"
        )
        # A 还没提交：此刻库里已提交的状态仍然是 enabled。B 如果读的是"进来那一刻
        # 的快照"，它读到的就是这个值——这正是本条要证伪的形状。
        self.assertEqual(self._account_state(), "enabled", "A 尚未提交，已提交状态必须还是 enabled")

        decider = threading.Thread(target=record_the_decision, name="racing-decision")
        decider.start()
        try:
            deadline = time.monotonic() + self.HOLD_BUDGET_SECONDS
            blocked_observed = False
            while time.monotonic() < deadline:
                if self._blocked_backend_count() > 0:
                    blocked_observed = True
                    break
                time.sleep(0.02)
        finally:
            # A 提交（或至少结束）——放行必须发生在 B 的 lock_timeout 之内，
            # 因此无论有没有观察到阻塞都要执行。
            allow_commit.set()
            holder.join(timeout=10)
            decider.join(timeout=15)

        self.assertEqual(holder_errors, [], f"事务 A 不应抛出未预期的异常：{holder_errors}")
        self.assertFalse(holder.is_alive(), "事务 A 未在预算内结束")
        self.assertFalse(decider.is_alive(), "事务 B 未在预算内结束")
        self.assertTrue(
            blocked_observed,
            "事务 B 没有停在 A 的行锁上——这次跑的不是并发路径，断言不成立",
        )
        # A 确实提交了停用，不是回滚掉了。
        self.assertEqual(self._account_state(), "suspended", "事务 A 的停用必须已经提交")

        self.assertEqual(
            decision_result,
            [],
            "被挡的决定不得返回任何结论：拿到结论就意味着这次授权真的排出去了",
        )
        self.assertEqual(len(decision_errors), 1, "事务 B 必须以异常退出")
        blocked = decision_errors[0]
        self.assertIsInstance(
            blocked,
            PermissionGrantBlockedByAccountState,
            "B 必须被账号状态守卫挡住（拿到锁之后读的是 A 刚提交的 suspended）；"
            f"实际是 {type(blocked).__name__}: {blocked}",
        )
        self.assertEqual(blocked.account_state, "suspended")  # type: ignore[attr-defined]
        # 整体回滚：版本没推进、outbox 一行都没有。少了这一半，"被挡"可能只是
        # 抛异常之前已经写了半套状态。
        self.assertEqual(self._version(), 0, "被挡的决定不得推进权限版本")
        self.assertEqual(self._count(), 0, "被挡的决定不得留下任何发布意图")


class NoticeRecipientWhitelistTest(PermissionPublishPostgresTestCase):
    """codex 第 2 轮 P2 的"演进防御"处置（Issue #483）：通知收件人查询改正向白名单。

    行集合与改之前**逐行等价**（``account_state`` 的 CHECK 只有四个取值），这里钉的是
    每一个取值的实际行为，而不是 SQL 文本——文本断言换个写法就假绿。
    """

    def test_only_an_active_enabled_user_is_a_notice_recipient(self) -> None:
        self._set_states(provisioning_state="active")
        self.assertEqual(self.store.notice_recipient_open_id(USER_A), f"ou_{USER_A}")

        for account_state in ("suspended", "deleting", "deleted"):
            with self.subTest(account_state=account_state):
                self._set_states(account_state=account_state)
                self.assertIsNone(
                    self.store.notice_recipient_open_id(USER_A),
                    "非 enabled 的账号一律不是通知收件人",
                )

    def test_an_unfinished_provisioning_user_is_not_a_notice_recipient(self) -> None:
        """既有口径不变：还没开通完成的人不该收到"你的可用范围已更新"。"""

        self._set_states(account_state="enabled", provisioning_state="provisioning")
        self.assertIsNone(self.store.notice_recipient_open_id(USER_A))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
