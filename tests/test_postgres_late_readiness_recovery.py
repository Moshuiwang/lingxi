"""迟到就绪恢复的真库断言（V-开通-18，外部独立审查 F1/F2/F3 修复）。

只有真库能证伪它们：**候选筛选**是一条跨三张表的 SQL 联合谓词的属性，**推进 active
与排通知同一个事务**是事务的属性，**CAS 带版本号**是 ``UPDATE ... WHERE`` 谓词的属性，
**认领即退避**是 ``FOR UPDATE SKIP LOCKED`` 加原地 ``UPDATE`` 的属性——在假 store 上跑，
这几条无论实现怎么写都是绿的。

表结构由 ``migrations/alembic/versions/0066_onboarding_notice_outbox.py`` 建立，
测试库走 ``ensure_production_schema`` 的整条 alembic 链，与生产同源。

本文件承担三条必修的真库半边：

- **F1**：:meth:`~lingxi.adapters.postgres_late_readiness_recovery.
  PostgresLateReadinessStore.activate_after_late_readiness` 推进状态与排通知是
  同一个事务——``test_a_successful_activation_creates_exactly_one_pending_notice``；
  通知发送失败后**仍然留在 pending，下一次到期会被重新认领**——
  ``test_a_notice_that_failed_to_send_is_reclaimed_on_its_next_due_time``（这是
  「推进成功但通知未送达 → 下一轮仍会重发直到送达」的真库半边，编排层的等价断言在
  ``tests/test_late_readiness_recovery.py``）。
- **F2**：候选查询不再暴露"历史上出现过 ready 就跳过探针"的字段或语义——
  ``test_the_candidate_has_no_shortcut_around_a_fresh_probe``。
- **F3**：CAS 带 ``expected_permission_version``，版本不对时**不写任何东西、不排任何
  通知**——``test_a_stale_permission_version_is_refused_and_creates_no_notice``。
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

from postgres_schema import ensure_production_schema, reset_production_rows

from lingxi.adapters.mcp_token_cipher import McpTokenCipher
from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_late_readiness_recovery import PostgresLateReadinessStore
from lingxi.adapters.postgres_mcp_token import PostgresMcpTokenStore
from lingxi.adapters.postgres_permission_publish import PostgresPermissionPublishStore
from lingxi.core.permission.mcp_readiness import (
    ReadinessAttempt,
    ReadinessBinding,
    ReadinessOutcome,
)
from lingxi.core.permission.publish import PublishAttempt, PublishOutcome, STATUS_PUBLISHED
from lingxi.core.permission.publish_row import PublishRow

SPEC_MASTER_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="

SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，迟到就绪恢复的真库断言未验证（需真实 PostgreSQL 16）"
)

NOW = datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)
REASON = "first_onboarding"
USER_A = "usr_late_recovery_a"
USER_B = "usr_late_recovery_b"
EMAIL_A = "jiaming.jia@example.invalid"
EMAIL_B = "yiming.yi@example.invalid"


def _row(email: str = EMAIL_A, *, permissions: str = '{"1011":["商务"]}') -> PublishRow:
    return PublishRow(
        record_key=email,
        email=email,
        name="化名甲",
        permissions=permissions,
        status="approved",
        updated_at="2026-08-20T03:00:00Z",
        token_cipher=None,
    )


def _publish_attempt(outbox_id: str, *, version: int, user_id: str) -> PublishAttempt:
    return PublishAttempt(
        outcome=PublishOutcome.PUBLISHED,
        outbox_id=outbox_id,
        permission_version=version,
        user_id=user_id,
        attempts=1,
        action="create",
        external_record_id="rec_1",
    )


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN"), SKIP_REASON)
class LateReadinessRecoveryPostgresTestCase(unittest.TestCase):
    """真库断言的共同底座。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
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
        self.publish_store = PostgresPermissionPublishStore(self._dsn)
        self.store = PostgresLateReadinessStore(self._dsn)

    # ------------------------------------------------------------------
    # 夹具
    # ------------------------------------------------------------------

    def _publish(
        self, *, user_id: str = USER_A, row: PublishRow | None = None, reason: str = REASON
    ) -> int:
        """发布一版权限并读回一致，返回 ``permission_version``。"""

        decision = self.publish_store.record_decision(
            user_id=user_id, row=row or _row(), reason=reason, decided_at=NOW
        )
        claimed = self.publish_store.claim_next()
        assert claimed is not None
        self.publish_store.complete(
            _publish_attempt(claimed.outbox_id, version=claimed.permission_version, user_id=user_id),
            status=STATUS_PUBLISHED,
        )
        return int(decision.permission_version)

    def _stuck(self, user_id: str = USER_A, *, account_state: str = "enabled") -> None:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE app_user SET provisioning_state = 'mcp_syncing', account_state = %s "
                "WHERE id = %s",
                (account_state, user_id),
            )

    def _record_timed_out(self, user_id: str, version: int, *, at: datetime | None = None) -> None:
        # 用**真实当前时刻**回退一小时，不用固定的 ``NOW`` 常量：候选查询的到期判据
        # 比较的是真库的 ``now()``，固定常量一旦落在真实墙钟之后就会让"已经超时一小时"
        # 变成一句假话，候选查询因此正确地把它判成"还没到期"——这是测试夹具的时间基准
        # 错误，不是候选查询的缺陷。
        moment = at or (datetime.now(timezone.utc) - timedelta(hours=1))
        PostgresMcpTokenStore(self._dsn, cipher=McpTokenCipher(SPEC_MASTER_KEY)).record_attempt(
            ReadinessAttempt(
                binding=ReadinessBinding(user_id, version),
                attempt_no=1,
                outcome=ReadinessOutcome.TIMED_OUT,
                started_at=moment,
                finished_at=moment,
                error_code="budget_exhausted",
            )
        )

    def _provisioning_state(self, user_id: str = USER_A) -> str:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT provisioning_state FROM app_user WHERE id = %s", (user_id,))
            return str(cursor.fetchone()[0])

    def _notice_count(self, user_id: str = USER_A) -> int:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM onboarding_completion_notice WHERE user_id = %s",
                (user_id,),
            )
            return int(cursor.fetchone()[0])

    def _notice_row(self, user_id: str = USER_A):
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status, attempts, next_attempt_at, company_name, function_name, "
                "dedupe_key FROM onboarding_completion_notice WHERE user_id = %s",
                (user_id,),
            )
            return cursor.fetchone()


class CandidateQueryTest(LateReadinessRecoveryPostgresTestCase):
    """候选查询：与迁移前的行为等价，且**不再**有 ``already_ready`` 快捷路径（F2）。"""

    INTERVAL_SECONDS = 900

    def _candidates(self, *, limit: int = 50):
        return self.store.late_onboarding_recovery_candidates(
            reason=REASON, recovery_interval_seconds=self.INTERVAL_SECONDS, limit=limit
        )

    def test_a_timed_out_stuck_user_is_a_candidate(self) -> None:
        version = self._publish()
        self._stuck()
        self._record_timed_out(USER_A, version)

        candidates = self._candidates()

        self.assertEqual([item.user_id for item in candidates], [USER_A])
        self.assertEqual(candidates[0].permission_version, version)
        self.assertEqual(candidates[0].next_attempt_no, 2)

    def test_the_candidate_has_no_shortcut_around_a_fresh_probe(self) -> None:
        """F2：即使这个人**历史上真的探到过 ready**，候选对象也不携带任何"跳过探针"
        的信号——字段本身已经不存在，调用方没有任何绕过探针的入口。"""

        version = self._publish()
        self._stuck()
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        self._record_timed_out(USER_A, version, at=old)
        PostgresMcpTokenStore(self._dsn, cipher=McpTokenCipher(SPEC_MASTER_KEY)).record_attempt(
            ReadinessAttempt(
                binding=ReadinessBinding(USER_A, version),
                attempt_no=2,
                outcome=ReadinessOutcome.READY,
                started_at=old + timedelta(seconds=1),
                finished_at=old + timedelta(seconds=1),
                metric_count=3,
            )
        )

        candidates = self._candidates()

        self.assertEqual(len(candidates), 1)
        self.assertFalse(hasattr(candidates[0], "already_ready"))
        self.assertEqual(candidates[0].next_attempt_no, 3, "两条历史判定 + 1，不因为曾经 ready 而特殊处理")

    def test_a_suspended_account_is_not_a_candidate(self) -> None:
        version = self._publish()
        self._stuck(account_state="suspended")
        self._record_timed_out(USER_A, version)

        self.assertEqual(self._candidates(), ())

    def test_an_intent_owned_by_another_orchestrator_is_not_a_candidate(self) -> None:
        version = self.publish_store.record_decision(
            user_id=USER_A, row=_row(), reason="daily_permission_refresh", decided_at=NOW
        ).permission_version
        self.publish_store.claim_next()
        self._stuck()

        self.assertEqual(self._candidates(), ())


class ActivationTest(LateReadinessRecoveryPostgresTestCase):
    """F1（同一事务）与 F3（版本守卫）。"""

    def _dedupe(self, user_id: str, version: int) -> str:
        return f"onboarding:recovery:{user_id}:{version}"

    def test_a_successful_activation_creates_exactly_one_pending_notice(self) -> None:
        version = self._publish()
        self._stuck()

        activated = self.store.activate_after_late_readiness(
            user_id=USER_A,
            expected_permission_version=version,
            company_name="1011",
            function_name="商务",
            dedupe_key=self._dedupe(USER_A, version),
        )

        self.assertTrue(activated)
        self.assertEqual(self._provisioning_state(), "active")
        self.assertEqual(self._notice_count(), 1, "F1：状态推进的同时必须排出恰一条通知")
        row = self._notice_row()
        self.assertEqual(row[0], "pending")
        self.assertEqual(row[3], "1011")
        self.assertEqual(row[4], "商务")

    def test_a_stale_permission_version_is_refused_and_creates_no_notice(self) -> None:
        """F3：候选查到的版本与当前真实版本不一致时，CAS 拒绝，**不写任何东西**。"""

        version = self._publish()
        self._stuck()

        activated = self.store.activate_after_late_readiness(
            user_id=USER_A,
            expected_permission_version=version + 1,  # 故意给一个过时/错误的版本
            company_name="1011",
            function_name="商务",
            dedupe_key=self._dedupe(USER_A, version + 1),
        )

        self.assertFalse(activated)
        self.assertEqual(
            self._provisioning_state(), "mcp_syncing", "CAS 失败绝不能推进状态"
        )
        self.assertEqual(self._notice_count(), 0, "CAS 失败绝不能排出任何通知")

    def test_a_suspended_account_is_refused_and_creates_no_notice(self) -> None:
        version = self._publish()
        self._stuck(account_state="suspended")

        activated = self.store.activate_after_late_readiness(
            user_id=USER_A,
            expected_permission_version=version,
            company_name="1011",
            function_name="商务",
            dedupe_key=self._dedupe(USER_A, version),
        )

        self.assertFalse(activated)
        self.assertEqual(self._notice_count(), 0)

    def test_an_already_active_user_is_refused_and_creates_no_second_notice(self) -> None:
        """否定断言：两次调用（模拟重复推进）不会产生第二条通知或第二次状态改写。"""

        version = self._publish()
        self._stuck()
        first = self.store.activate_after_late_readiness(
            user_id=USER_A,
            expected_permission_version=version,
            company_name="1011",
            function_name="商务",
            dedupe_key=self._dedupe(USER_A, version),
        )
        self.assertTrue(first)

        second = self.store.activate_after_late_readiness(
            user_id=USER_A,
            expected_permission_version=version,
            company_name="1011",
            function_name="商务",
            dedupe_key=self._dedupe(USER_A, version),
        )

        self.assertFalse(second, "已经 active 的人不能被再次'推进'")
        self.assertEqual(self._notice_count(), 1, "恰一条通知，不因为重复调用而翻倍")

    def test_the_dedupe_key_prevents_a_duplicate_notice_even_if_the_cas_matched_twice(
        self,
    ) -> None:
        """幂等的最后一道防线：即使调用方（不应该）把状态手动拨回 mcp_syncing 再调用
        一次同一个 dedupe_key，数据库的 UNIQUE 约束仍然只留一条通知。"""

        version = self._publish()
        self._stuck()
        dedupe_key = self._dedupe(USER_A, version)
        self.assertTrue(
            self.store.activate_after_late_readiness(
                user_id=USER_A,
                expected_permission_version=version,
                company_name="1011",
                function_name="商务",
                dedupe_key=dedupe_key,
            )
        )
        # 测试专用：模拟"状态被别的路径拨回 mcp_syncing"这一异常场景，验证 outbox
        # 层的去重不依赖调用方只调用一次。
        self._stuck()

        self.assertTrue(
            self.store.activate_after_late_readiness(
                user_id=USER_A,
                expected_permission_version=version,
                company_name="1011",
                function_name="商务",
                dedupe_key=dedupe_key,
            )
        )
        self.assertEqual(self._notice_count(), 1, "UNIQUE(dedupe_key) 挡住了第二条")

    def test_bad_arguments_are_rejected(self) -> None:
        version = self._publish()
        self._stuck()
        for kwargs in (
            {"user_id": ""},
            {"expected_permission_version": 0},
            {"expected_permission_version": -1},
            {"company_name": ""},
            {"function_name": ""},
            {"dedupe_key": ""},
        ):
            with self.subTest(kwargs):
                base = {
                    "user_id": USER_A,
                    "expected_permission_version": version,
                    "company_name": "1011",
                    "function_name": "商务",
                    "dedupe_key": self._dedupe(USER_A, version),
                    **kwargs,
                }
                with self.assertRaises(ValueError):
                    self.store.activate_after_late_readiness(**base)


class NoticeOutboxTest(LateReadinessRecoveryPostgresTestCase):
    """通知 outbox 的 claim / complete / purge（F1 的持久重试半边）。"""

    def _activate(self, *, user_id: str = USER_A, version: int | None = None) -> int:
        v = version or self._publish(user_id=user_id, row=_row(EMAIL_A if user_id == USER_A else EMAIL_B))
        self._stuck(user_id)
        ok = self.store.activate_after_late_readiness(
            user_id=user_id,
            expected_permission_version=v,
            company_name="1011",
            function_name="商务",
            dedupe_key=f"onboarding:recovery:{user_id}:{v}",
        )
        assert ok
        return v

    def test_a_due_notice_is_claimed(self) -> None:
        self._activate()

        claimed = self.store.claim_one_due_notice()

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.user_id, USER_A)
        self.assertEqual(claimed.company_name, "1011")

    def test_claiming_advances_the_next_attempt_time_so_it_is_not_immediately_reclaimed(
        self,
    ) -> None:
        """认领即退避：紧接着再认领一次，同一条不会被立刻拿到第二次。"""

        self._activate()
        first = self.store.claim_one_due_notice()
        self.assertIsNotNone(first)

        second = self.store.claim_one_due_notice()

        self.assertIsNone(second, "刚认领过的通知在退避窗口内不该被再次认领")

    def test_a_notice_that_failed_to_send_is_reclaimed_on_its_next_due_time(self) -> None:
        """F1 核心场景：**推进成功但通知未送达 → 下一轮仍会重发直到送达**。"""

        self._activate()
        claimed = self.store.claim_one_due_notice()
        assert claimed is not None
        self.store.mark_notice_failed(claimed.notice_id, error="RuntimeError")

        # 还没到退避之后的时间点：不会被立刻捞回来。
        self.assertIsNone(self.store.claim_one_due_notice())

        # 手动把 next_attempt_at 拨回过去，模拟"退避窗口已经过去"（真实场景里是时间
        # 本身流逝，这里用时间穿梭替代真的等待）。
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE onboarding_completion_notice SET next_attempt_at = now() - interval '1 second' "
                "WHERE id = %s",
                (claimed.notice_id,),
            )

        reclaimed = self.store.claim_one_due_notice()

        self.assertIsNotNone(reclaimed, "失败之后到期了必须能被重新认领——绝不能永久丢失")
        self.assertEqual(reclaimed.notice_id, claimed.notice_id)

        self.store.mark_notice_delivered(reclaimed.notice_id)
        row = self._notice_row()
        self.assertEqual(row[0], "delivered")

    def test_a_delivered_notice_is_never_reclaimed(self) -> None:
        self._activate()
        claimed = self.store.claim_one_due_notice()
        assert claimed is not None
        self.store.mark_notice_delivered(claimed.notice_id)

        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE onboarding_completion_notice SET next_attempt_at = now() - interval '1 second'"
            )

        self.assertIsNone(self.store.claim_one_due_notice())

    def test_two_pending_notices_are_claimed_oldest_first(self) -> None:
        # 不能事后拨回 created_at 来模拟"更早创建"——迁移 0066 的触发器不允许 UPDATE
        # 改写它（与 publish_outbox 的不可变纪律同型）。两次真实 INSERT 之间 `now()`
        # 的先后顺序本身就是可靠的："先插入的 created_at 更早"不需要人为构造。
        self._activate(user_id=USER_A)
        self._activate(user_id=USER_B)

        first = self.store.claim_one_due_notice()
        second = self.store.claim_one_due_notice()

        self.assertEqual(first.user_id, USER_A)
        self.assertEqual(second.user_id, USER_B)

    def test_purge_only_removes_terminal_rows_past_expiry(self) -> None:
        self._activate(user_id=USER_A)
        pending = self.store.claim_one_due_notice()
        assert pending is not None
        # pending 保持不变（不 mark_delivered），模拟"还在重试"的状态。

        self._activate(user_id=USER_B)
        delivered_claim = self.store.claim_one_due_notice()
        assert delivered_claim is not None
        self.store.mark_notice_delivered(delivered_claim.notice_id)

        # 用**真实当前时刻**推远，不用固定的 ``NOW`` 常量：到期判据 ``content_expires_at``
        # 是触发器按真库 ``now()``（写入时刻）+ 2160 小时算的，写入时刻随测试运行的真实
        # 墙钟走。固定常量一旦落在真实墙钟 40 小时之后（NOW + 2160h < 写入时刻 + 2160h），
        # ``far_future`` 就会小于 ``content_expires_at``，purge 判它"还没到期"而删 0 行，
        # 这是测试夹具的时间基准错误，不是 purge 的缺陷（日界翻转型 flaky）。
        far_future = datetime.now(timezone.utc) + timedelta(hours=2200)  # 远超过 2160 小时的到期上限
        purged = self.store.purge_expired_notices(now=far_future)

        self.assertEqual(purged, 1, "只删已送达且过期的那一条")
        self.assertEqual(self._notice_count(USER_A), 1, "pending 的那一条绝不会被删——它还在等待送达")
        self.assertEqual(self._notice_count(USER_B), 0)

    def test_purge_requires_a_timezone_aware_moment(self) -> None:
        with self.assertRaises(ValueError):
            self.store.purge_expired_notices(now=datetime(2026, 8, 20, 3, 0))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
