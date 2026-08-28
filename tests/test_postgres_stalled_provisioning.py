"""开通中途停摆收口候选查询的真库断言（Issue #282，`V-开通-19`）。

只有真库能证伪它们：候选筛选是一条跨 ``app_user``/``inbound_event``/``mcp_sync_check``
三张表的联合谓词的属性，「取最新一次认领」是横向子查询 ``ORDER BY ... DESC LIMIT 1``
的属性，「与迟到就绪恢复互补」是两条候选查询按同一批数据交叉验证的属性——在假 store
上跑，这几条无论实现怎么写都是绿的。

表结构由 alembic 全链建立（``ensure_production_schema``），测试库与生产同源。
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.adapters.mcp_token_cipher import McpTokenCipher
from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_late_readiness_recovery import PostgresLateReadinessStore
from lingxi.adapters.postgres_mcp_token import PostgresMcpTokenStore
from lingxi.adapters.postgres_permission_publish import PostgresPermissionPublishStore
from lingxi.adapters.postgres_stalled_provisioning import PostgresStalledProvisioningStore
from lingxi.core.permission.mcp_readiness import ReadinessAttempt, ReadinessBinding, ReadinessOutcome
from lingxi.core.permission.publish import PublishAttempt, PublishOutcome, STATUS_PUBLISHED
from lingxi.core.permission.publish_row import PublishRow

SPEC_MASTER_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="

SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，开通中途停摆收口候选查询的真库断言未验证"
    "（需真实 PostgreSQL 16）"
    if not os.environ.get("LINGXI_POSTGRES_DSN")
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，开通中途停摆收口候选查询的真库断言未验证"
)

LEASE_SECONDS = 2700
USER_A = "usr_stalled_a"
USER_B = "usr_stalled_b"
EMAIL_B = "yiming.yi@example.invalid"
LATE_READINESS_REASON = "first_onboarding"


def _row(email: str = EMAIL_B, *, permissions: str = '{"1011":["商务"]}') -> PublishRow:
    return PublishRow(
        record_key=email,
        email=email,
        name="化名乙",
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


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN") and psycopg_available(), SKIP_REASON)
class StalledProvisioningPostgresTestCase(unittest.TestCase):
    """候选查询真库断言的共同底座。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        reset_production_rows(self._dsn)
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            for user_id in (USER_A, USER_B):
                cursor.execute(
                    """INSERT INTO app_user
                         (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name,
                          department, tenant_key, provisioning_state)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, 'provisioning')""",
                    (
                        user_id,
                        f"ou_{user_id}",
                        f"fs_{user_id}",
                        f"on_{user_id}",
                        "化名甲" if user_id == USER_A else "化名乙",
                        "测试部门",
                        "tenant-fake",
                    ),
                )
        self.store = PostgresStalledProvisioningStore(self._dsn)
        self.late_readiness = PostgresLateReadinessStore(self._dsn)
        self.publish_store = PostgresPermissionPublishStore(self._dsn)

    # ------------------------------------------------------------------
    # 夹具
    # ------------------------------------------------------------------

    def _publish(
        self, *, user_id: str, row: PublishRow | None = None, reason: str = LATE_READINESS_REASON
    ) -> int:
        """发布一版权限并读回一致，返回 ``permission_version``——迟到就绪恢复的
        候选查询要求 ``publish_outbox`` 里有一条 ``published`` 的意图（外部独立
        审查 P3-1：没有这份夹具，`late_onboarding_recovery_candidates` 永远返回
        空集，「两职责互补」的断言会变成一句永远成立的空话）。形状照
        ``tests/test_postgres_late_readiness_recovery.py`` 的同名夹具。
        """

        decision = self.publish_store.record_decision(
            user_id=user_id, row=row or _row(), reason=reason, decided_at=datetime.now(timezone.utc)
        )
        claimed = self.publish_store.claim_next()
        assert claimed is not None
        self.publish_store.complete(
            _publish_attempt(claimed.outbox_id, version=claimed.permission_version, user_id=user_id),
            status=STATUS_PUBLISHED,
        )
        return int(decision.permission_version)

    def _set_state(
        self, user_id: str = USER_A, *, state: str = "provisioning", account_state: str = "enabled"
    ) -> None:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE app_user SET provisioning_state = %s, account_state = %s WHERE id = %s",
                (state, account_state, user_id),
            )

    def _dispatch(
        self,
        event_id: str,
        *,
        user_id: str = USER_A,
        dispatched_at: datetime | None = None,
        trace_id: str | None = None,
    ) -> None:
        """插入一条已认领的 ``auto_provisioning`` 事件。``dispatched_at=None`` 表示
        「还没被认领」（``onboarding_dispatched_at IS NULL``）——用来验证不抢
        `OnboardingReconciler` 的候选集合。"""

        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT feishu_open_id FROM app_user WHERE id = %s", (user_id,)
            )
            open_id = cursor.fetchone()[0]
            cursor.execute(
                """INSERT INTO inbound_event
                     (feishu_event_id, received_at, event_type, user_open_id, handled_as,
                      trace_id, onboarding_dispatched_at)
                   VALUES (%s, %s, %s, %s, 'auto_provisioning', %s, %s)""",
                (
                    event_id,
                    dispatched_at or datetime.now(timezone.utc),
                    "im.message.receive_v1",
                    open_id,
                    trace_id or f"trc_{event_id}",
                    dispatched_at,
                ),
            )

    def _bump_permission_version(self, user_id: str, version: int) -> None:
        """`mcp_sync_check.permission_version` 必须与 `app_user.permission_version`
        对齐——候选查询的 `NOT EXISTS` 子句按这两列相等联结，版本不对齐会让一条真实
        存在的 `timed_out` 记录被联结不上、白白记了却挡不住候选。"""

        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE app_user SET permission_version = %s WHERE id = %s", (version, user_id)
            )

    def _record_timed_out(self, user_id: str, version: int, *, at: datetime | None = None) -> None:
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

    def _candidates(self, *, lease_seconds: int = LEASE_SECONDS, limit: int = 50):
        return self.store.stalled_provisioning_candidates(lease_seconds=lease_seconds, limit=limit)

    def _expired(self, extra_seconds: int = 60) -> datetime:
        """比租约边界更早（因此判定为"已超期"）的认领时刻。"""

        return datetime.now(timezone.utc) - timedelta(seconds=LEASE_SECONDS + extra_seconds)


class CandidateQueryTest(StalledProvisioningPostgresTestCase):
    """五条筛选条件（模块文档「候选查询」一节）。"""

    def test_a_stalled_provisioning_user_is_a_candidate(self) -> None:
        self._set_state(state="provisioning")
        self._dispatch("evt_a", dispatched_at=self._expired())

        candidates = self._candidates()

        self.assertEqual([c.user_id for c in candidates], [USER_A])
        self.assertEqual(candidates[0].provisioning_state, "provisioning")
        self.assertEqual(candidates[0].event_id, "evt_a")
        self.assertEqual(candidates[0].trace_id, "trc_evt_a")

    def test_a_stalled_mcp_syncing_user_without_a_timed_out_record_is_a_candidate(self) -> None:
        """Issue #282 §0.2：`mcp_syncing` 上从未判过 `timed_out` 的失败此前没有出口，
        只修 `provisioning` 等于把同一个缺陷留一半。"""

        self._set_state(state="mcp_syncing")
        self._dispatch("evt_a", dispatched_at=self._expired())

        candidates = self._candidates()

        self.assertEqual([c.user_id for c in candidates], [USER_A])
        self.assertEqual(candidates[0].provisioning_state, "mcp_syncing")

    def test_not_yet_over_the_lease_is_never_selected(self) -> None:
        """**否定断言**：租约还没到期的候选绝不能被选中——否则会把一条正在正常跑的
        开通判成僵尸。"""

        self._set_state(state="provisioning")
        self._dispatch(
            "evt_a",
            dispatched_at=datetime.now(timezone.utc) - timedelta(seconds=LEASE_SECONDS - 1),
        )

        self.assertEqual(self._candidates(), ())

    def test_a_timed_out_mcp_syncing_user_is_never_selected(self) -> None:
        """**否定断言**：已经判过 `timed_out` 的候选归迟到就绪恢复职责
        （`V-开通-18`）——两个候选查询的集合按同一个子查询取互补，不能同时命中。"""

        self._set_state(state="mcp_syncing")
        self._dispatch("evt_a", dispatched_at=self._expired())
        self._bump_permission_version(USER_A, 1)
        self._record_timed_out(USER_A, version=1)

        self.assertEqual(self._candidates(), ())

    def test_a_disabled_account_is_never_selected(self) -> None:
        """**否定断言**：与收口写入的 CAS 守卫（`account_state = 'enabled'`）同一口径
        ——不选一个 CAS 注定会拒绝的候选。"""

        self._set_state(state="provisioning", account_state="suspended")
        self._dispatch("evt_a", dispatched_at=self._expired())

        self.assertEqual(self._candidates(), ())

    def test_an_unclaimed_event_is_never_selected(self) -> None:
        """**否定断言**：`onboarding_dispatched_at IS NULL` 的事件属
        `OnboardingReconciler`（未开通首聊交接对账）的候选集合，不是本职责的——两者
        对同一列取互补条件，本查询要求 `onboarding_dispatched_at IS NOT NULL`。"""

        self._set_state(state="provisioning")
        self._dispatch("evt_a", dispatched_at=None)

        self.assertEqual(self._candidates(), ())

    def test_the_latest_dispatch_is_used_not_an_older_one(self) -> None:
        """租约必须从**最近一次**认领起跑算，否则一条历史事件会让刚起跑的链立刻被判
        超时。"""

        self._set_state(state="provisioning")
        self._dispatch("evt_old", dispatched_at=self._expired(extra_seconds=3600))
        self._dispatch("evt_recent", dispatched_at=datetime.now(timezone.utc) - timedelta(seconds=5))

        self.assertEqual(
            self._candidates(),
            (),
            "最新一次认领才 5 秒前，不该因为一条更早的历史事件被判超时",
        )

    def test_a_released_latest_claim_hides_an_older_expired_one(self) -> None:
        """**否定断言**（外部独立审查 P2-5）：这个人名下最新一条事件此刻**没有被
        认领**（``onboarding_dispatched_at IS NULL``——例如通知发不出去被
        ``AutoOnboardingRunner._release_for_notify`` 放回、正等着
        ``OnboardingReconciler`` 重新认领），即使他名下**还有一条更早、认领时刻
        早已超过租约的历史事件**，也绝不能被选中——那条历史认领不是"现在"的认领，
        选中他等于把一条正在被对账重新认领的正常链误判成停摆 45 分钟的僵尸。"""

        self._set_state(state="provisioning")
        self._dispatch("evt_old", dispatched_at=self._expired(extra_seconds=3600))
        self._dispatch("evt_released", dispatched_at=None)

        self.assertEqual(
            self._candidates(),
            (),
            "最新事件当前未被认领，不该退回去用更早那条历史认领的时刻判超时",
        )

    def test_candidates_are_ordered_by_dispatch_time(self) -> None:
        self._set_state(USER_A, state="provisioning")
        self._set_state(USER_B, state="provisioning")
        self._dispatch("evt_b", user_id=USER_B, dispatched_at=self._expired(extra_seconds=10))
        self._dispatch("evt_a", user_id=USER_A, dispatched_at=self._expired(extra_seconds=3600))

        candidates = self._candidates()

        self.assertEqual([c.user_id for c in candidates], [USER_A, USER_B])


class ComplementaryCandidateSetsTest(StalledProvisioningPostgresTestCase):
    """`V-开通-19` 测试矩阵「两职责互补」一条的可执行形式：同一批构造数据同时喂给
    两个候选查询，断言返回的用户集合交集为空。**两个候选集合都必须真的非空**
    （外部独立审查 P3-1 修复）：此前的版本没有造 `publish_outbox` 夹具，
    `late_onboarding_recovery_candidates` 永远返回空集，`stalled & late_readiness
    == set()` 因此是一句永远成立、测不出任何东西的空话——正确的用例必须先证明
    USER_B 真的出现在对方的候选集合里，再证明它不出现在本职责的候选集合里。
    """

    def test_stalled_and_late_readiness_candidate_sets_never_overlap(self) -> None:
        # USER_A：停在 provisioning，超过停摆租约——只应出现在停摆收口的候选集合。
        self._set_state(USER_A, state="provisioning")
        self._dispatch("evt_a", user_id=USER_A, dispatched_at=self._expired())

        # USER_B：停在 mcp_syncing、已经发布过一版权限且已经判过 timed_out——只应
        # 出现在迟到就绪恢复的候选集合（本职责的 NOT EXISTS 子句必须把它挡在外面）。
        self._set_state(USER_B, state="mcp_syncing")
        self._dispatch("evt_b", user_id=USER_B, dispatched_at=self._expired())
        version = self._publish(user_id=USER_B)
        self._record_timed_out(USER_B, version=version)

        stalled = {c.user_id for c in self._candidates()}
        late_readiness = {
            c.user_id
            for c in self.late_readiness.late_onboarding_recovery_candidates(
                reason=LATE_READINESS_REASON, recovery_interval_seconds=1
            )
        }

        self.assertEqual(stalled, {USER_A})
        self.assertEqual(
            late_readiness, {USER_B}, "夹具必须让 USER_B 真的出现在对方的候选集合里"
        )
        self.assertEqual(stalled & late_readiness, set())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
