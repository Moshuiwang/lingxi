"""每日权限重算的真库断言（Issue #156 / S-C-03a，S-C-03b 补上撤权侧）。

与 ``tests/test_permission_refresh_duty.py`` 一同承担 `V-权限-08` 的刷新侧
（撤权 = 保行清空、幂等、不碰密文）。这里放的是**只有真库能证伪**的那几条：

1. **遍历口径**：``provisioning_state``/``account_state`` 的过滤写在 SQL 里，因此
   guest、matching、deleting、deleted 四个否定样本**一条发布意图都不产生**。在假
   baseline 上跑，这条无论实现怎么写都是绿的；
2. **``UNCHANGED`` 从职责层贯穿**：同一天重跑（水位重置模拟进程重启）不推进
   ``app_user.permission_version``、不排第二条意图——判定在 ``record_decision`` 的
   事务与锁里，假实现替不了；
3. **按当前有效批次读银河**：过期批次、被取代的批次都不算当前有效，
   :class:`~lingxi.adapters.postgres_galaxy_snapshot.PostgresGalaxySnapshotReader`
   的返回随之变化。

数据全部为虚构化名，不含任何真实导出内容。
"""

from __future__ import annotations

import os
import pathlib
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from postgres_schema import ensure_production_schema, reset_production_rows

from lingxi.adapters.feishu_roster_bitable import RosterRow
from lingxi.adapters.mcp_token_cipher import McpTokenCipher
from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_galaxy_snapshot import PostgresGalaxySnapshotReader
from lingxi.adapters.postgres_mcp_token import PostgresMcpTokenStore
from lingxi.adapters.postgres_permission_publish import PostgresPermissionPublishStore
from lingxi.adapters.postgres_roster_audit import PostgresRosterBaselineReader
from lingxi.adapters.postgres_roster_snapshot import PostgresRosterSnapshotStore
from lingxi.apps.scheduler.permission_refresh import PermissionRefreshDuty
from lingxi.core.permission.publish import STATUS_PUBLISHED, PublishAttempt, PublishOutcome

REPOSITORY_ROOT = pathlib.Path(__file__).parents[1]

SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，每日权限重算的真库断言未验证（需真实 PostgreSQL 16）"
)

#: biai-agent 加密规格 v1 的**公开测试向量**（非生产密钥）。
SPEC_MASTER_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="

NOW = datetime(2026, 8, 17, 3, 0, tzinfo=timezone.utc)

ACTIVE_USER = "usr_refresh_active"
GUEST_USER = "usr_refresh_guest"
MATCHING_USER = "usr_refresh_matching"
DELETING_USER = "usr_refresh_deleting"
DELETED_USER = "usr_refresh_deleted"

# 四个否定样本与那一个正样本**在花名册和银河里的资料完全一样**，唯一的差别只有
# `app_user` 的两个状态列。这样「它们没有产生发布意图」就只可能来自筛选口径，
# 不可能来自"它们本来就匹配不上"。
PERSONNEL = {
    ACTIVE_USER: "ou_person_active",
    GUEST_USER: "ou_person_guest",
    MATCHING_USER: "ou_person_matching",
    DELETING_USER: "ou_person_deleting",
    DELETED_USER: "ou_person_deleted",
}
EMPLOYEE = {
    ACTIVE_USER: "10001",
    GUEST_USER: "10002",
    MATCHING_USER: "10003",
    DELETING_USER: "10004",
    DELETED_USER: "10005",
}
EMAIL = {user: f"person{index}@example.invalid" for index, user in enumerate(PERSONNEL, start=1)}
NAME = {user: f"化名{index}" for index, user in enumerate(PERSONNEL, start=1)}

ROLE_FUNCTION_MAP = {"A运营": "运营"}


def _galaxy_tables() -> dict[str, list[dict[str, str]]]:
    """一份合成导出：五个人各自有同样的角色与国家授权。"""

    return {
        "user": [
            {
                "user_id": f"G-{EMPLOYEE[user]}",
                "dept_id": "D1",
                "user_name": EMPLOYEE[user],
                "nick_name": NAME[user],
                "email": EMAIL[user],
                "create_time": "2019-01-02 03:04:05",
            }
            for user in PERSONNEL
        ],
        "user_role": [
            {
                "user_id": f"G-{EMPLOYEE[user]}",
                "role_id": "R-甲",
                "user_name": NAME[user],
                "role_name": "A运营",
            }
            for user in PERSONNEL
        ],
        "role_menu": [
            {"role_id": "R-甲", "menu_id": "M1", "role_name": "A运营", "menu_name": "报表"}
        ],
        "sys_user_datacountry": [
            {
                "USER_ID": f"G-{EMPLOYEE[user]}",
                "DATACOUNTRY_ID": "101",
                "USER_NAME": NAME[user],
                "DATACOUNTRY_NAME": "甲国",
            }
            for user in PERSONNEL
        ],
        "sys_country": [
            {
                "id": "7",
                "country_key": "101",
                "name": "ALPHA",
                "code": "AL",
                "name_cn": "甲国",
                "region_key": "1",
                "region_name": "甲区",
                "boss_company_id": "BC-甲",
            }
        ],
    }


class _Audit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, object]]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, dict(fields)))

    def actions(self) -> list[str]:
        return [action for action, _ in self.records]


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN"), SKIP_REASON)
class PermissionRefreshPostgresTestCase(unittest.TestCase):
    """真库底座：整条 alembic 链建库，用例之间只清行。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        reset_production_rows(self._dsn)
        self.audit = _Audit()
        self.clock = _Clock(NOW)

    # ---- 夹具 --------------------------------------------------------

    def _insert_users(self) -> None:
        states = {
            ACTIVE_USER: ("active", "enabled"),
            GUEST_USER: ("guest", "enabled"),
            MATCHING_USER: ("matching", "enabled"),
            DELETING_USER: ("active", "deleting"),
            DELETED_USER: ("active", "deleted"),
        }
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            for user_id, (provisioning_state, account_state) in states.items():
                cursor.execute(
                    """INSERT INTO app_user
                         (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name,
                          department, tenant_key, employee_no, email,
                          provisioning_state, account_state)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        user_id,
                        f"ou_{user_id}",
                        PERSONNEL[user_id],
                        f"on_{user_id}",
                        NAME[user_id],
                        "测试部门",
                        "tenant-fake",
                        EMPLOYEE[user_id],
                        EMAIL[user_id],
                        provisioning_state,
                        account_state,
                    ),
                )

    def _write_roster_snapshot(self, *, captured_at: datetime = NOW) -> None:
        store = PostgresRosterSnapshotStore(self._dsn)
        rows = [
            RosterRow(
                personnel_id=PERSONNEL[user],
                email=EMAIL[user],
                name=NAME[user],
                employee_no=EMPLOYEE[user],
                record_id=f"rec_{user}",
            )
            for user in PERSONNEL
        ]
        store.replace(
            rows,
            SimpleNamespace(
                pages_read=1,
                reported_total=len(rows),
                total_matches_rows=True,
                rows_without_personnel_id=0,
                blank_column_rows=(),
                duplicates=(),
            ),
            captured_at=captured_at,
        )

    def _import_galaxy(self, *, digest: str = "digest-refresh") -> str:
        from lingxi.adapters.galaxy_import import PostgresGalaxyImportStore

        result = PostgresGalaxyImportStore(self._dsn).import_export(
            source_label="合成导出（测试）", source_digest=digest, tables=_galaxy_tables()
        )
        self.assertEqual(result.outcome, "imported")
        return result.batch_id

    def _token_store(self) -> PostgresMcpTokenStore:
        return PostgresMcpTokenStore(self._dsn, cipher=McpTokenCipher(SPEC_MASTER_KEY))

    def _duty(self) -> PermissionRefreshDuty:
        publish_store = PostgresPermissionPublishStore(self._dsn)
        return PermissionRefreshDuty(
            baseline_reader=PostgresRosterBaselineReader(self._dsn),
            roster_snapshot=PostgresRosterSnapshotStore(self._dsn),
            galaxy=PostgresGalaxySnapshotReader(self._dsn),
            decisions=publish_store,
            publish_history=publish_store,
            token_ciphers=self._token_store(),
            role_function_map=ROLE_FUNCTION_MAP,
            audit=self.audit,
            clock=self.clock,
        )

    # ---- 断言辅助 ----------------------------------------------------

    def _outbox(self) -> list[tuple[str, int, str]]:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT user_id, permission_version, reason FROM publish_outbox ORDER BY user_id"
            )
            return [(str(row[0]), int(row[1]), str(row[2])) for row in cursor.fetchall()]

    def _payload(self, user_id: str) -> dict:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT payload FROM publish_outbox WHERE user_id = %s", (user_id,))
            return dict(cursor.fetchone()[0])

    def _latest_payload(self, user_id: str) -> dict:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT payload FROM publish_outbox
                    WHERE user_id = %s ORDER BY permission_version DESC LIMIT 1""",
                (user_id,),
            )
            return dict(cursor.fetchone()[0])

    def _version(self, user_id: str) -> int:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT permission_version FROM app_user WHERE id = %s", (user_id,))
            return int(cursor.fetchone()[0])


class _Clock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, delta: timedelta) -> None:
        self.moment = self.moment + delta


class _ScriptedSelection(PostgresGalaxySnapshotReader):
    """第一次选择给出指定批次，之后如实重问库里的当前有效批次。

    用它把「选择与读取之间库里换了批次」变成确定性事件：真实并发窗口在测试里没法稳定
    复现，而这段代码要挡的正是那个窗口。第二次调用**不作假**——复核拿到的是库的真实
    答案，因此这组用例证明的是判据本身，不是脚本。
    """

    def __init__(self, dsn: str, *, first: str) -> None:
        super().__init__(dsn)
        self._first = first
        self.calls = 0

    def current_batch_id(self):
        self.calls += 1
        if self.calls == 1:
            return self._first
        return super().current_batch_id()


class ActiveOnlyTest(PermissionRefreshPostgresTestCase):
    """否定断言：非 active / deleting / deleted 用户绝不进入重算。"""

    def test_only_the_active_user_gets_a_publish_intent(self) -> None:
        self._insert_users()
        self._write_roster_snapshot()
        self._import_galaxy()
        self._token_store().issue_token(ACTIVE_USER)

        report = self._duty().run_once()

        self.assertEqual(report.examined, 1, "只有一个已开通用户进入本轮")
        self.assertEqual(report.enqueued, 1)
        self.assertEqual([entry[0] for entry in self._outbox()], [ACTIVE_USER])
        for user in (GUEST_USER, MATCHING_USER, DELETING_USER, DELETED_USER):
            with self.subTest(user=user):
                self.assertEqual(self._version(user), 0, "否定样本的权限版本不得被推进")

    def test_the_intent_carries_the_refresh_reason_and_the_issued_cipher(self) -> None:
        self._insert_users()
        self._write_roster_snapshot()
        self._import_galaxy()
        issued = self._token_store().issue_token(ACTIVE_USER)

        self._duty().run_once()

        entry = self._outbox()[0]
        self.assertEqual(entry[1], 1)
        self.assertEqual(entry[2], "daily_permission_refresh")
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT payload FROM publish_outbox WHERE user_id = %s", (ACTIVE_USER,))
            payload = cursor.fetchone()[0]
        self.assertEqual(payload["token_cipher"], issued.token_cipher)
        self.assertEqual(payload["record_key"], EMAIL[ACTIVE_USER])
        self.assertEqual(payload["permissions"], '{"BC-甲":["运营"]}')
        self.assertNotIn(issued.reveal(), str(payload), "令牌明文一步都不进 outbox")


class UnchangedAcrossRoundsTest(PermissionRefreshPostgresTestCase):
    """否定断言：内容没变的重跑不推进版本、不排第二条意图。"""

    def test_a_later_round_with_a_moved_clock_still_changes_nothing(self) -> None:
        """第二轮**推进时钟**，仍然判 ``UNCHANGED``。

        两轮用同一个 ``NOW`` 会掩盖一类真实回归：只要变化判定的指纹里混进了时间字段
        （``updated_at`` 每轮都不同），"内容没变"就会天天被判成"变了"，于是每天给每个人
        重发一次内容完全相同的权限。时钟不动的用例对这种回归**永远是绿的**。
        """

        self._insert_users()
        self._write_roster_snapshot()
        self._import_galaxy()
        self._token_store().issue_token(ACTIVE_USER)

        first = self._duty()
        first.run_once()
        first_payload = self._payload(ACTIVE_USER)

        # 换一个新的职责实例＝模拟进程重启（水位没有持久载体，当天会再跑一轮），
        # 同时把时钟推到几小时后：产品上这一轮仍然必须什么都不改变。
        self.clock.advance(timedelta(hours=6))
        second = self._duty()
        report = second.run_once()

        self.assertEqual(report.enqueued, 0)
        self.assertEqual(report.unchanged, 1)
        self.assertEqual(len(self._outbox()), 1, "不得排出第二条意图")
        self.assertEqual(self._version(ACTIVE_USER), 1, "版本不得被无变化的一轮推进")
        self.assertEqual(
            self._payload(ACTIVE_USER)["updated_at"],
            first_payload["updated_at"],
            "既有意图的内容快照连时间戳都不该被改写",
        )

    def test_the_next_day_round_is_still_unchanged(self) -> None:
        """跨日再跑一轮（同一进程实例、时钟走到明天）：仍然无变化。

        与上一条的区别是它同时跨过了水位的日界——水位放行之后，产品语义仍由
        ``record_decision`` 的内容比对决定，而不是"新的一天所以重发一次"。
        """

        self._insert_users()
        self._write_roster_snapshot()
        self._import_galaxy()
        self._token_store().issue_token(ACTIVE_USER)

        duty = self._duty()
        duty.run_once()
        # 花名册快照也要跟着到第二天，否则第二轮会停在顺序判据上。
        self.clock.advance(timedelta(days=1))
        self._write_roster_snapshot(captured_at=NOW + timedelta(days=1))
        report = duty.run_once()

        self.assertEqual(report.unchanged, 1)
        self.assertEqual(report.enqueued, 0)
        self.assertEqual(len(self._outbox()), 1)
        self.assertEqual(self._version(ACTIVE_USER), 1)


class RevokedUserTest(PermissionRefreshPostgresTestCase):
    """`V-权限-08` 刷新侧：**从无发布行的人零意图**；有发布行的人得到一条清空更新。

    只有真库能证伪的那半边：撤权的**幂等**（第二天仍然无权限时判 ``UNCHANGED``、
    不推进版本、不排第二条意图）落在 ``record_decision`` 的事务与内容比对里，
    假 store 替不了。
    """

    def _publish_current_intent(self) -> None:
        """把当前这条 pending 意图推到 ``published``，模拟"我们发布成功过"。"""

        store = PostgresPermissionPublishStore(self._dsn)
        claimed = store.claim_next()
        assert claimed is not None
        store.complete(
            PublishAttempt(
                outcome=PublishOutcome.PUBLISHED,
                outbox_id=claimed.outbox_id,
                user_id=claimed.user_id,
                permission_version=claimed.permission_version,
                attempts=claimed.attempts,
                action="create",
                external_record_id="rec_fake",
            ),
            status=STATUS_PUBLISHED,
        )

    def _drop_roles(self) -> None:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM galaxy_user_role WHERE user_id = %s",
                (f"G-{EMPLOYEE[ACTIVE_USER]}",),
            )

    def test_a_revoked_user_with_a_published_row_gets_an_empty_permissions_intent(self) -> None:
        self._insert_users()
        self._write_roster_snapshot()
        self._import_galaxy()
        self._token_store().issue_token(ACTIVE_USER)
        duty = self._duty()
        duty.run_once()
        self._publish_current_intent()
        self._drop_roles()

        # 第二天再跑一轮：这一次聚合是「无可用权限」。
        self.clock.moment = NOW + timedelta(days=1)
        self._write_roster_snapshot(captured_at=self.clock.moment)
        report = self._duty().run_once()

        self.assertEqual(report.revoked, 1)
        self.assertEqual(report.revoked_published, 1)
        self.assertEqual(self._version(ACTIVE_USER), 2, "撤权是一次新的权限决定")
        reasons = [reason for _user, _version, reason in self._outbox()]
        self.assertIn("daily_permission_revoke", reasons)
        payload = self._latest_payload(ACTIVE_USER)
        self.assertEqual(payload["permissions"], "{}")
        self.assertEqual(payload["status"], "approved")
        self.assertNotIn("token_cipher", payload, "撤权快照只有六个字段，密文一个字不动")

    def test_repeating_the_revocation_changes_nothing(self) -> None:
        """幂等：第二次撤权判 ``UNCHANGED``，不推进版本、不排第二条意图。"""

        self._insert_users()
        self._write_roster_snapshot()
        self._import_galaxy()
        self._token_store().issue_token(ACTIVE_USER)
        self._duty().run_once()
        self._publish_current_intent()
        self._drop_roles()
        self.clock.moment = NOW + timedelta(days=1)
        self._write_roster_snapshot(captured_at=self.clock.moment)
        self._duty().run_once()
        before = self._version(ACTIVE_USER)
        intents_before = len(self._outbox())

        self.clock.moment = NOW + timedelta(days=2)
        self._write_roster_snapshot(captured_at=self.clock.moment)
        report = self._duty().run_once()

        self.assertEqual(report.revoked, 1)
        self.assertEqual(report.revoked_published, 0)
        self.assertEqual(report.unchanged, 1)
        self.assertEqual(self._version(ACTIVE_USER), before, "重复撤权不推进版本")
        self.assertEqual(len(self._outbox()), intents_before, "重复撤权不排第二条意图")

    def test_a_user_whose_galaxy_roles_vanish_is_only_skipped(self) -> None:
        self._insert_users()
        self._write_roster_snapshot()
        self._import_galaxy()
        self._token_store().issue_token(ACTIVE_USER)
        # 抹掉该账号的角色：下一轮聚合就是「无可用权限」。
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM galaxy_user_role WHERE user_id = %s",
                (f"G-{EMPLOYEE[ACTIVE_USER]}",),
            )

        report = self._duty().run_once()

        self.assertEqual(report.revoked, 1)
        self.assertEqual(report.enqueued, 0)
        self.assertEqual(self._outbox(), [], "撤权不产生发布意图")
        self.assertEqual(self._version(ACTIVE_USER), 0, "撤权不推进权限版本")
        skipped = [fields for action, fields in self.audit.records if action.endswith("user_skipped")]
        self.assertEqual(skipped[0]["reason"], "no_galaxy_roles")


class RosterFreshnessGateTest(PermissionRefreshPostgresTestCase):
    def test_a_snapshot_from_yesterday_stops_the_round_before_any_galaxy_read(self) -> None:
        self._insert_users()
        self._write_roster_snapshot(captured_at=NOW - timedelta(days=1))
        self._import_galaxy()

        self.assertIsNone(self._duty().run_once())

        self.assertEqual(self._outbox(), [])
        self.assertEqual(self.audit.actions(), ["permission_refresh.skipped_roster_not_fresh"])

    def test_without_any_snapshot_the_round_stops_too(self) -> None:
        self._insert_users()
        self._import_galaxy()

        self.assertIsNone(self._duty().run_once())

        self.assertEqual(self._outbox(), [])


class GalaxySnapshotReaderTest(PermissionRefreshPostgresTestCase):
    """按当前有效批次读回银河快照。"""

    def test_without_a_batch_the_snapshot_is_unavailable(self) -> None:
        self.assertIsNone(PostgresGalaxySnapshotReader(self._dsn).load_current())

    def test_the_rows_of_the_current_batch_are_read_and_grouped(self) -> None:
        batch_id = self._import_galaxy()

        snapshot = PostgresGalaxySnapshotReader(self._dsn).load_current()

        self.assertEqual(snapshot.batch_id, batch_id)
        self.assertEqual(len(snapshot.user_rows), len(PERSONNEL))
        self.assertEqual(len(snapshot.country_rows), 1)
        account = f"G-{EMPLOYEE[ACTIVE_USER]}"
        self.assertEqual(len(snapshot.role_rows(account)), 1)
        self.assertEqual(snapshot.role_rows(account)[0]["role_name"], "A运营")
        self.assertEqual(snapshot.datacountry_rows(account)[0]["datacountry_id"], "101")
        self.assertEqual(snapshot.role_rows("不存在的账号"), ())

    def test_only_the_registered_columns_are_read(self) -> None:
        """取的列就是要用的列：中文姓名（``nick_name``）刻意不取。"""

        self._import_galaxy()

        snapshot = PostgresGalaxySnapshotReader(self._dsn).load_current()

        self.assertEqual(set(snapshot.user_rows[0]), {"user_id", "user_name", "email"})
        self.assertEqual(
            set(snapshot.country_rows[0]), {"country_key", "name", "name_cn", "boss_company_id"}
        )
        account = f"G-{EMPLOYEE[ACTIVE_USER]}"
        self.assertEqual(set(snapshot.role_rows(account)[0]), {"user_id", "role_name"})

    def test_an_expired_batch_is_not_current(self) -> None:
        """过期批次不算「当前有效」——九十天上限之外的权限快照不得再驱动发布。

        制造过期批次只能靠 ``INSERT`` 一条来源时间很旧的行：``expires_at`` 由迁移
        ``0054`` 的触发器从 ``started_at`` 推导，而 ``started_at`` 本身改不了
        （改了就等于可以无限后移到期时间）。
        """

        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO galaxy_import_batch
                     (id, source_label, source_digest, status, started_at, completed_at)
                   VALUES (%s, %s, %s, 'complete', now() - interval '2200 hours',
                           now() - interval '2200 hours')""",
                ("gib_expired_fixture", "合成导出（测试）", "digest-expired"),
            )
            cursor.execute(
                "SELECT expires_at < now() FROM galaxy_import_batch WHERE id = %s",
                ("gib_expired_fixture",),
            )
            self.assertTrue(cursor.fetchone()[0], "夹具本身必须真的过期")

        reader = PostgresGalaxySnapshotReader(self._dsn)
        # 两道判定各有各的抓手：批次选择本身（复用导入层的「未过期的最近一个
        # complete」），以及读完之后的复核。少任何一道，这两条断言里就有一条变红。
        self.assertIsNone(reader.current_batch_id(), "过期批次不是当前有效批次")
        self.assertIsNone(reader.load_current())

    def test_a_batch_that_expires_between_selection_and_read_is_rejected(self) -> None:
        """读完之后**重新问一次当前有效批次**，答案必须仍是刚才那一批。

        九十天保留清理按 ``expires_at`` 连带删除整批行；四条读取语句在
        ``READ COMMITTED`` 下各取一次数据库快照，因此"选批次时还有效、读到一半没了"
        是真实可达的。这里用 :class:`_ScriptedSelection` 确定性地复现它：第一次选择给出
        那个过期批次，复核时如实重问，库里已经没有当前有效批次。
        """

        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO galaxy_import_batch
                     (id, source_label, source_digest, status, started_at, completed_at)
                   VALUES (%s, %s, %s, 'complete', now() - interval '2200 hours',
                           now() - interval '2200 hours')""",
                ("gib_expired_between", "合成导出（测试）", "digest-between"),
            )

        reader = _ScriptedSelection(self._dsn, first="gib_expired_between")

        self.assertIsNone(reader.load_current())
        self.assertEqual(reader.calls, 2, "选择一次、复核一次")

    def test_a_batch_superseded_between_selection_and_read_is_rejected(self) -> None:
        """读取期间**一次新导入完成**：手里这份行是上一批的，整轮失败关闭。

        这是复核判据从「A 仍 complete 未过期」改成「A 仍是**当前**批次」要挡的那条腿：
        旧批次转 ``superseded`` 之后它本身仍然 complete、仍然未过期，旧判据一路放行，
        于是用刚被取代的权限覆盖新权限——而外部发布表没有版本号，谁也发现不了。
        """

        first = self._import_galaxy(digest="digest-a")
        second = self._import_galaxy(digest="digest-b")
        self.assertNotEqual(first, second)

        reader = _ScriptedSelection(self._dsn, first=first)

        self.assertIsNone(reader.load_current(), "选到 A、复核发现当前已是 B → 不可用")
        self.assertEqual(reader.current_batch_id(), second)

    def test_the_recheck_does_not_restate_the_current_batch_predicate(self) -> None:
        """否定断言：适配器里**不得**复写「complete 且未过期」。

        那是 `V-银河-06` 的规则，唯一实现在导入层。复写一份就有了第二处口径，早晚分叉
        ——而分叉的表现是"复核通过了、用的却不是当前那一批"。复核因此只能是"重调一次
        并比对 id"。
        """

        from test_permission_refresh_duty import code_without_docstrings

        code = code_without_docstrings(
            REPOSITORY_ROOT / "src/lingxi/adapters/postgres_galaxy_snapshot.py"
        )
        for forbidden in ("'complete'", "expires_at", "superseded", "status ="):
            self.assertNotIn(forbidden, code, f"批次判据不得在适配器里复写：{forbidden}")

    def test_a_superseded_batch_is_not_read(self) -> None:
        first = self._import_galaxy(digest="digest-old")
        second = self._import_galaxy(digest="digest-new")

        snapshot = PostgresGalaxySnapshotReader(self._dsn).load_current()

        self.assertNotEqual(first, second)
        self.assertEqual(snapshot.batch_id, second)
        # 两批的行数相同，因此只有批次标识能区分——读到的必须是新批次那一份。
        self.assertEqual(len(snapshot.user_rows), len(PERSONNEL))


if __name__ == "__main__":
    unittest.main()
