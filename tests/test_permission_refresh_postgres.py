"""每日权限重算的真库断言（Issue #156 / S-C-03a，S-C-03b 补上撤权侧）。

与 ``tests/test_permission_refresh_duty.py`` 一同承担 `V-权限-08` 的刷新侧
（撤权 = 保行清空、幂等、不碰密文）。这里放的是**只有真库能证伪**的那几条：

1. **遍历口径**：``provisioning_state``/``account_state`` 的过滤写在 SQL 里，因此
   guest、matching、deleting、deleted、suspended 五个否定样本**一条发布意图都不
   产生**。在假 baseline 上跑，这条无论实现怎么写都是绿的；
2. **``UNCHANGED`` 从职责层贯穿**：同一天重跑（水位重置模拟进程重启）不推进
   ``app_user.permission_version``、不排第二条意图——判定在 ``record_decision`` 的
   事务与锁里，假实现替不了；
3. **按当前有效批次读银河**：过期批次、被取代的批次都不算当前有效，
   :class:`~lingxi.adapters.postgres_galaxy_snapshot.PostgresGalaxySnapshotReader`
   的返回随之变化；
4. **停用/恢复的可逆性**（Issue #468）：``suspended`` 用户的次日批量重算不得把发布
   基线回填成有效权限，恢复 ``enabled`` 之后必须重新纳入——只有真库能证伪
   ``PostgresPermissionRefreshBaselineReader`` 的行集合随 ``account_state`` 变化。

数据全部为虚构化名，不含任何真实导出内容。
"""

from __future__ import annotations

import os
import pathlib
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.adapters.feishu_roster_bitable import RosterRow
from lingxi.adapters.mcp_token_cipher import McpTokenCipher
from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_galaxy_snapshot import PostgresGalaxySnapshotReader
from lingxi.adapters.postgres_local_permission import local_override_reader
from lingxi.adapters.postgres_mcp_token import PostgresMcpTokenStore
from lingxi.adapters.postgres_permission_publish import (
    PostgresPermissionPublishStore,
    PostgresPermissionRefreshBaselineReader,
)
from lingxi.adapters.postgres_roster_snapshot import PostgresRosterSnapshotStore
from lingxi.apps.scheduler.permission_refresh import (
    PermissionRefreshSources,
    PERMISSION_REVOKE_REASON,
    REASON_FULLY_SUPPRESSED,
    SKIP_ACCOUNT_NOT_ENABLED,
    SKIP_NO_PUBLISHED_ROW,
    PermissionRefreshDuty,
)
from lingxi.core.ids import new_id
from lingxi.core.permission.publish import (
    STATUS_PUBLISHED,
    ExistingPermissionRow,
    PublishAttempt,
    PublishOutcome,
    publish_claim,
)
from lingxi.core.permission.targeted_recompute import (
    ADMIN_TARGETED_REVOKE_REASON,
    RecomputeKind,
    TargetedPermissionRecompute,
)
from lingxi.core.permission.targeted_recompute import (
    SKIP_ACCOUNT_NOT_ENABLED as TARGETED_SKIP_ACCOUNT_NOT_ENABLED,
)


class _ExplodingTransport:
    """任何一次外部调用都算断言失败：用来证明 ``SUPERSEDED`` **一次调用都不发**。"""

    def find_rows(self, *, record_key: str, email: str):  # pragma: no cover - 调用即失败
        raise AssertionError("过期意图不得发起任何外部调用")

    def create_row(self, fields):  # pragma: no cover - 调用即失败
        raise AssertionError("过期意图不得新建行")

    def update_row(self, record_id, fields):  # pragma: no cover - 调用即失败
        raise AssertionError("过期意图不得更新行")

    def read_row(self, record_id):  # pragma: no cover - 调用即失败
        raise AssertionError("过期意图不得读回")


REPOSITORY_ROOT = pathlib.Path(__file__).parents[1]

SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，每日权限重算的真库断言未验证（需真实 PostgreSQL 16）"
    if not os.environ.get("LINGXI_POSTGRES_DSN")
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，每日权限重算的真库断言未验证"
)

#: biai-agent 加密规格 v1 的**公开测试向量**（非生产密钥）。
SPEC_MASTER_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="

NOW = datetime(2026, 8, 17, 3, 0, tzinfo=UTC)

ACTIVE_USER = "usr_refresh_active"
GUEST_USER = "usr_refresh_guest"
MATCHING_USER = "usr_refresh_matching"
DELETING_USER = "usr_refresh_deleting"
DELETED_USER = "usr_refresh_deleted"
#: Issue #468 的否定样本：管理员停用，`account_state='suspended'`，
#: `provisioning_state` 仍是 `active`（停用不改开通状态，见
#: `adapters/postgres_pending_action.py` 的 `suspend_user` 分支）。银河与花名册对
#: 这个人的资料与 `ACTIVE_USER` 逐字节相同，唯一差别是这一个状态列——这样"他没有
#: 产生发布意图"就只可能来自基线排除，不可能来自"他本来就匹配不上"。
SUSPENDED_USER = "usr_refresh_suspended"

# 五个否定样本与那一个正样本**在花名册和银河里的资料完全一样**，唯一的差别只有
# `app_user` 的两个状态列。这样「它们没有产生发布意图」就只可能来自筛选口径，
# 不可能来自"它们本来就匹配不上"。
PERSONNEL = {
    ACTIVE_USER: "ou_person_active",
    GUEST_USER: "ou_person_guest",
    MATCHING_USER: "ou_person_matching",
    DELETING_USER: "ou_person_deleting",
    DELETED_USER: "ou_person_deleted",
    SUSPENDED_USER: "ou_person_suspended",
}
EMPLOYEE = {
    ACTIVE_USER: "10001",
    GUEST_USER: "10002",
    MATCHING_USER: "10003",
    DELETING_USER: "10004",
    DELETED_USER: "10005",
    SUSPENDED_USER: "10006",
}
EMAIL = {user: f"person{index}@example.invalid" for index, user in enumerate(PERSONNEL, start=1)}
NAME = {user: f"化名{index}" for index, user in enumerate(PERSONNEL, start=1)}

ROLE_FUNCTION_MAP = {"A运营": "运营"}
#: 翻译层（Issue #227）测试夹具：覆盖合成导出用到的唯一「公司 + 职能」组合
#: （``BC-甲`` + ``运营``）。指标名是虚构占位，不对应任何真实指标。
METRIC_NAME = "示例指标-日活"
METRIC_TRANSLATION_MAP = {"BC-甲": {"运营": (METRIC_NAME,)}}


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


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN") and psycopg_available(), SKIP_REASON)
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
            SUSPENDED_USER: ("active", "suspended"),
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

    def _duty(
        self, *, metric_translation_map=None, local_overrides=None, publish_store=None
    ) -> PermissionRefreshDuty:
        # ``publish_store`` 可注入：Issue #483 的交错用例要在真实 store 外面包一层
        # **只控制先后顺序、不改变任何被测判据**的装饰器（见 WindowSuspendTest）。
        publish_store = publish_store or PostgresPermissionPublishStore(self._dsn)
        return PermissionRefreshDuty(
                   sources=PermissionRefreshSources(
                       baseline_reader=PostgresPermissionRefreshBaselineReader(self._dsn),
                       roster_snapshot=PostgresRosterSnapshotStore(self._dsn),
                       galaxy=PostgresGalaxySnapshotReader(self._dsn),
                       decisions=publish_store,
                       publish_history=publish_store,
                       token_ciphers=self._token_store(),
                       local_overrides=local_overrides,
                   ),
                   role_function_map=ROLE_FUNCTION_MAP,
                   metric_translation_map=METRIC_TRANSLATION_MAP if metric_translation_map is None else metric_translation_map,
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

    def _set_account_state(self, user_id: str, account_state: str) -> None:
        """模拟 ``resume_user``：只翻转 ``account_state`` 这一列，与生产的
        ``postgres_pending_action.py`` ``suspend_user``/``resume_user`` 分支同一姿态
        （不动 ``provisioning_state``）。"""

        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE app_user SET account_state = %s, updated_at = now() WHERE id = %s",
                (account_state, user_id),
            )


class _RecordingTransport:
    """记录每一次外部写入，把"发布行有没有被回发"这条断言钉在**外部调用层**。

    为什么不用 :class:`_ExplodingTransport`（一次外部调用即断言失败）：修复之后窗口
    用户仍然有一条**合法的撤权意图**要发出去（``force_revoke`` 排的那条），"零外部
    调用"因此是错的判据。真正要证伪的是「**有没有任何一次外部写入把非空权限写回
    这一行**」——那才是停用承诺被突破时用户侧真正会发生的事。
    """

    def __init__(self, existing: dict[str, dict] | None = None) -> None:
        self._rows: dict[str, dict] = {rid: dict(fields) for rid, fields in (existing or {}).items()}
        #: 每一次外部写入的 (record_id, 字段快照)。``record_id`` 为 ``None`` 表示新建。
        self.writes: list[tuple[str | None, dict]] = []

    def find_rows(self, *, record_key: str, email: str):
        return tuple(
            ExistingPermissionRow(record_id=record_id, fields=dict(fields))
            for record_id, fields in self._rows.items()
            if fields.get("record_key") == record_key or fields.get("email") == email
        )

    def create_row(self, fields):
        record_id = f"rec_created_{len(self._rows) + 1}"
        self._rows[record_id] = dict(fields)
        self.writes.append((None, dict(fields)))
        return record_id

    def update_row(self, record_id, fields):
        self._rows.setdefault(record_id, {}).update(fields)
        self.writes.append((record_id, dict(fields)))

    def read_row(self, record_id):
        return dict(self._rows[record_id])

    def permissions_written(self) -> list[str]:
        return [fields["permissions"] for _record_id, fields in self.writes if "permissions" in fields]


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
    """否定断言：非 active / deleting / deleted / suspended 用户绝不进入重算。"""

    def test_only_the_active_user_gets_a_publish_intent(self) -> None:
        self._insert_users()
        self._write_roster_snapshot()
        self._import_galaxy()
        self._token_store().issue_token(ACTIVE_USER)

        report = self._duty().run_once()

        self.assertEqual(report.examined, 1, "只有一个已开通用户进入本轮")
        self.assertEqual(report.enqueued, 1)
        self.assertEqual([entry[0] for entry in self._outbox()], [ACTIVE_USER])
        for user in (GUEST_USER, MATCHING_USER, DELETING_USER, DELETED_USER, SUSPENDED_USER):
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
        # 翻译层（Issue #227）接线之后，写进 outbox payload 的是翻译产物（指标名），
        # 不是聚合层的原始职能标签「运营」。
        self.assertEqual(payload["permissions"], f'{{"BC-甲":["{METRIC_NAME}"]}}')
        self.assertNotIn(issued.reveal(), str(payload), "令牌明文一步都不进 outbox")


class SuspendedUserExcludedTest(PermissionRefreshPostgresTestCase):
    """Issue #468：停用抑制不得被次日批量重算突破，恢复 ``enabled`` 后必须重新纳入。

    只有真库能证伪的那一半：``PostgresPermissionRefreshBaselineReader`` 的行集合
    随 ``app_user.account_state`` 实时变化——假 baseline 上这条判据无论实现怎么写
    都是绿的。即时撤销路径（管理员点「停用」当下 ``force_revoke`` 立刻清空发布行）
    是另一条既有链路（``adapters/postgres_permission_recompute_trigger.py``），
    本用例不重复它，只钉「次日批量重算」这一侧——即使完全不依赖即时撤销先跑过，
    停用用户单凭这一条批次本身也不得拿到任何权限。
    """

    def test_a_suspended_user_is_excluded_then_reincluded_after_resume(self) -> None:
        self._insert_users()
        self._write_roster_snapshot()
        self._import_galaxy()
        self._token_store().issue_token(ACTIVE_USER)
        self._token_store().issue_token(SUSPENDED_USER)

        # 第一轮：SUSPENDED_USER 的花名册与银河资料与 ACTIVE_USER 逐字节相同
        # （同一份 PERSONNEL 驱动的合成导出），如果发布基线没有排除 suspended，
        # 这一轮会像 ACTIVE_USER 一样给他一条真实权限的授权意图——这正是 Issue #468
        # 描述的"停用抑制被次日批量重算静默突破"。
        report = self._duty().run_once()

        self.assertEqual(report.examined, 1, "停用用户不得进入本轮遍历——发布基线已排除")
        self.assertEqual(
            [entry[0] for entry in self._outbox()], [ACTIVE_USER], "停用用户的发布行不得回发"
        )
        self.assertEqual(self._version(SUSPENDED_USER), 0, "停用期间权限版本不得被推进")

        # 恢复：管理员执行 resume_user，只翻转 account_state（与生产 `postgres_
        # pending_action.py` 的 resume_user 分支同一姿态，不动 provisioning_state）。
        self._set_account_state(SUSPENDED_USER, "enabled")
        self.clock.advance(timedelta(days=1))
        self._write_roster_snapshot(captured_at=self.clock.moment)

        report = self._duty().run_once()

        self.assertEqual(report.examined, 2, "恢复之后重新进入遍历，与 ACTIVE_USER 一起被检查")
        self.assertEqual(report.enqueued, 1, "只有新纳入的这个人产生新意图，ACTIVE_USER 判 UNCHANGED")
        payload = self._latest_payload(SUSPENDED_USER)
        self.assertEqual(
            payload["permissions"],
            f'{{"BC-甲":["{METRIC_NAME}"]}}',
            "恢复后必须重新拿到与 ACTIVE_USER 一致的真实权限",
        )
        self.assertEqual(self._version(SUSPENDED_USER), 1, "恢复后的这一次决定才是他的第一次权限决定")


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

    def test_a_revocation_is_blocked_when_the_translation_map_is_empty(self) -> None:
        """P1（外部独立审查 2026-08-18 坐实、真库对照）：翻译映射为空时，一个已经
        发布过、如今 ``granted=False`` 的用户，撤权意图同样排不出来——真库层面的
        证据是 ``publish_outbox`` 不多一行、``app_user.permission_version`` 不推进。
        与上一条用例（映射非空）唯一的差异只有 ``metric_translation_map={}``。
        """

        self._insert_users()
        self._write_roster_snapshot()
        self._import_galaxy()
        self._token_store().issue_token(ACTIVE_USER)
        duty = self._duty()
        duty.run_once()
        self._publish_current_intent()
        self._drop_roles()

        # 第二天再跑一轮：这一次聚合是「无可用权限」，但翻译映射清空了。
        self.clock.moment = NOW + timedelta(days=1)
        self._write_roster_snapshot(captured_at=self.clock.moment)
        version_before = self._version(ACTIVE_USER)
        outbox_before = self._outbox()

        report = self._duty(metric_translation_map={}).run_once()

        self.assertIsNone(report, "整轮判据不成立时不产出报告")
        self.assertEqual(self._version(ACTIVE_USER), version_before, "撤权不得推进权限版本")
        self.assertEqual(self._outbox(), outbox_before, "publish_outbox 不得多出撤权那一行")

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

    def test_a_revocation_supersedes_an_in_flight_grant(self) -> None:
        """N7 的时间线：**在途的旧授权意图必须被撤权决定 supersede 掉**。

        场景：昨天排的 granted 意图还堵在 ``pending``（发布面当天没跑通），今天这个人
        在银河被撤权。若撤权因为"还没发布成功过"而跳过，等发布面消费积压时，那份
        **已经被收回的范围**会被写进外部表并触发一条"范围已更新"通知。

        算进足迹之后：撤权决定推进版本 → 旧意图被认领时 ``current_permission_version``
        更大 → 判 ``SUPERSEDED``，**一次外部调用都不发**。
        """

        self._insert_users()
        self._write_roster_snapshot()
        self._import_galaxy()
        self._token_store().issue_token(ACTIVE_USER)
        self._duty().run_once()  # 排出 v1（granted），**故意不发布**
        self.assertEqual(self._version(ACTIVE_USER), 1)
        self._drop_roles()

        self.clock.moment = NOW + timedelta(days=1)
        self._write_roster_snapshot(captured_at=self.clock.moment)
        report = self._duty().run_once()

        self.assertEqual(report.revoked_published, 1, "在途也算足迹，撤权照排")
        self.assertEqual(self._version(ACTIVE_USER), 2)

        # 旧的那条 granted 意图现在被认领时应当直接判过期，不做任何外部写入。
        store = PostgresPermissionPublishStore(self._dsn)
        claimed = store.claim_next()
        assert claimed is not None
        self.assertEqual(claimed.permission_version, 1)
        self.assertEqual(
            claimed.current_permission_version, 2, "认领时看到的当前版本已经是撤权那一版"
        )
        attempt = publish_claim(claimed, transport=_ExplodingTransport())
        self.assertEqual(attempt.outcome, PublishOutcome.SUPERSEDED)

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


class _FullSuppressionFixture(PermissionRefreshPostgresTestCase):
    """全抑制走撤权出口（红线-2，Trace #328 opus 审查）真库实测的共同底座：种一条
    真实 ``local_permission_override`` 生效抑制行（迁移 0072），FK 要求的
    ``pending_action`` 行一并种下（迁移 0068/0073），只用直接 SQL——完整的管理员
    命令面/确认卡流程不在本用例覆盖范围。"""

    def _publish_current_intent(self) -> None:
        """把当前这条 pending 意图推到 ``published``，模拟"我们发布成功过"。
        与 ``RevokedUserTest`` 同一内容，各自一份（同一条既有理由）。"""

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

    def _seed_active_suppression(self, *, user_id: str) -> None:
        # status='pending'：迁移 0068 的 CHECK 要求 (status='pending') = (decided_at
        # IS NULL)——本用例只需要这一行满足外键与 payload 自洽两条约束，不需要
        # 真的走完确认卡流程，因此不必额外填 decided_at/decided_by_open_id。
        pending_id = new_id("pac")
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO pending_action
                       (id, action_type, target_open_id, target_state_snapshot,
                        initiated_by_open_id, status, card_delivered, confirm_deadline_at, payload)
                   VALUES (%s, 'local_permission_suppress', %s, 'absent', 'ou_admin_fake',
                           'pending', TRUE, now() + interval '10 minutes',
                           '{"company_id":"BC-甲","metric_name":"' || %s || '","reason":"真库用例"}')""",
                (pending_id, f"ou_{user_id}", METRIC_NAME),
            )
        store = local_override_reader(self._dsn)
        # 直接调用底层 store（不经 LocalOverrideEntryReader 适配）以拿到 insert()。
        from lingxi.adapters.postgres_local_permission import PostgresLocalPermissionOverrideStore
        from lingxi.core.permission.local_override import OverrideDirection

        PostgresLocalPermissionOverrideStore(self._dsn).insert(
            user_id=user_id,
            direction=OverrideDirection.SUPPRESS,
            company_id="BC-甲",
            metric_name=METRIC_NAME,
            reason="真库用例：抑掉这个人唯一的指标",
            initiated_by_open_id="ou_admin_fake",
            pending_action_id=pending_id,
        )
        return store


class FullSuppressionRealDbTest(_FullSuppressionFixture):
    """红线-2 真库实测：抑掉用户全部指标 → 发布 ``{}`` 撤权行 + 专属审计。只有真库
    能证伪的那一半是 ``record_decision`` 的撤权分支真的把行写成 ``permissions={}``
    并推进版本——假 store 上这条判据无论实现怎么写都是绿的。"""

    def test_a_fully_suppressed_grant_with_a_real_publish_footprint_gets_an_empty_revocation(
        self,
    ) -> None:
        self._insert_users()
        self._write_roster_snapshot()
        self._import_galaxy()
        self._token_store().issue_token(ACTIVE_USER)

        # 第一轮：正常授权发布，建立真实发布足迹。
        self._duty().run_once()
        self._publish_current_intent()

        # 第二轮：管理员对这个人本地抑制了唯一的指标——合并结果压光到空字典。
        store = self._seed_active_suppression(user_id=ACTIVE_USER)
        self.clock.moment = NOW + timedelta(days=1)
        self._write_roster_snapshot(captured_at=self.clock.moment)

        report = self._duty(local_overrides=store).run_once()

        self.assertEqual(report.failed, 0, "全抑制不是技术故障")
        self.assertEqual(report.revoked, 1)
        self.assertEqual(self._version(ACTIVE_USER), 2, "撤权是一次新的权限决定")
        reasons = [reason for _user, _version, reason in self._outbox()]
        self.assertIn(PERMISSION_REVOKE_REASON, reasons)
        payload = self._latest_payload(ACTIVE_USER)
        self.assertEqual(payload["permissions"], "{}", "全抑制走撤权出口：保行清空")
        revoked_audit = [
            fields for action, fields in self.audit.records if action.endswith("user_revoked")
        ]
        self.assertEqual(len(revoked_audit), 1)
        self.assertEqual(
            revoked_audit[0]["reason"],
            REASON_FULLY_SUPPRESSED,
            "原因码必须可分辨——不是银河本来就没给他权限，是本地抑制清空的",
        )

    def test_a_fully_suppressed_grant_without_any_publish_footprint_is_skipped(self) -> None:
        self._insert_users()
        self._write_roster_snapshot()
        self._import_galaxy()
        self._token_store().issue_token(ACTIVE_USER)
        store = self._seed_active_suppression(user_id=ACTIVE_USER)

        report = self._duty(local_overrides=store).run_once()

        self.assertEqual(self._outbox(), [], "从未发布过的人不新建空权限行")
        self.assertEqual(report.revoked, 1)
        self.assertEqual(report.reasons[REASON_FULLY_SUPPRESSED], 1)
        skipped = [
            fields for action, fields in self.audit.records if action.endswith("user_skipped")
        ]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["reason"], REASON_FULLY_SUPPRESSED)
        self.assertEqual(skipped[0]["revocation"], SKIP_NO_PUBLISHED_ROW)


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

# --------------------------------------------------------------------------
# Issue #483：落决定的行锁里复检账号状态
# --------------------------------------------------------------------------


class _SuspendDuringBatchStore:
    """**只控制先后顺序、不改变任何被测判据**的装饰器（Issue #483 交错构造）。

    在真实 :class:`PostgresPermissionPublishStore` 外面包一层：第一次为窗口用户调用
    ``record_decision`` **之前**，先用生产同一姿态把这个人停用、再跑一次**生产装配**
    的 ``TargetedPermissionRecompute.force_revoke``，然后才委托给真实实现。

    **为什么这与真实竞态逐字等价**：``record_decision`` 自己建连接、自己开事务，它
    只可能看到**已提交**的库状态。真实竞态里"停用 + 撤权"这两次写入同样是在批处理
    读完基线之后、轮到这个人之前提交的。装饰器控制的只有"谁先提交"，停用写入与撤权
    都是生产代码跑出来的，批处理读到的也是真实提交后的库状态——没有任何一处判据被
    替换成假实现。不用线程、不用 sleep，因此确定性可复现。
    """

    def __init__(self, dsn: str, *, window_user: str, on_suspend) -> None:
        self._inner = PostgresPermissionPublishStore(dsn)
        self._dsn = dsn
        self._window_user = window_user
        self._on_suspend = on_suspend
        self.suspended = False

    def __getattr__(self, name):  # 其余方法原样委托给真实 store
        return getattr(self._inner, name)

    def record_decision(self, *, user_id: str, **kwargs):
        if user_id == self._window_user and not self.suspended:
            self.suspended = True
            self._on_suspend()
        return self._inner.record_decision(user_id=user_id, **kwargs)


class AccountStateWindowTestCase(PermissionRefreshPostgresTestCase):
    """#483 的共同底座：两个**都在基线里**的用户，资料逐字节相同。

    ``SUSPENDED_USER`` 在本组用例里一开始被翻成 ``enabled``（他与 ``ACTIVE_USER``
    的花名册/银河资料本就同源），这样"他最后没拿到权限"就只可能来自本次修复，不可能
    来自"他本来就在基线之外"。基线按 ``id`` 排序，``usr_refresh_active`` 排在
    ``usr_refresh_suspended`` 前面——对照用户先被处理，窗口正好落在他之后。
    """

    def _setup_two_active_users(self) -> None:
        self._insert_users()
        self._set_account_state(SUSPENDED_USER, "enabled")
        self._write_roster_snapshot()
        self._import_galaxy()
        self._token_store().issue_token(ACTIVE_USER)
        self._token_store().issue_token(SUSPENDED_USER)

    def _publish_all_pending(self) -> dict[str, dict]:
        """把当前全部 pending 意图推到 ``published``（模拟"我们真的发布成功过"），
        返回 ``外部记录标识 → 字段快照``，供 :class:`_RecordingTransport` 预置。"""

        store = PostgresPermissionPublishStore(self._dsn)
        rows: dict[str, dict] = {}
        seen: list[str] = []
        while True:
            claimed = store.claim_next(exclude=tuple(seen))
            if claimed is None:
                break
            seen.append(claimed.outbox_id)
            record_id = f"rec_{claimed.user_id}"
            rows[record_id] = dict(claimed.payload)
            store.complete(
                PublishAttempt(
                    outcome=PublishOutcome.PUBLISHED,
                    outbox_id=claimed.outbox_id,
                    user_id=claimed.user_id,
                    permission_version=claimed.permission_version,
                    attempts=claimed.attempts,
                    action="create",
                    external_record_id=record_id,
                ),
                status=STATUS_PUBLISHED,
            )
        return rows

    def _force_revoke_like_production(self, user_id: str) -> None:
        """跑一次**生产装配**的 ``force_revoke``——依赖照
        ``adapters/postgres_permission_recompute_trigger.PermissionRecomputeAdapter``
        构造（同一个 ``PostgresRosterBaselineReader``、同一个 publish store）。"""

        recompute = self._production_recompute()
        outcome = recompute.force_revoke(user_id=user_id)
        assert outcome.kind is RecomputeKind.REVOKED, outcome

    def _production_recompute(self) -> TargetedPermissionRecompute:
        """按 ``PermissionRecomputeAdapter.trigger`` 的装配复刻一份定向重算。

        **唯一的偏离**是两份内容配置（角色→职能、公司+职能→指标名）取本文件的夹具
        而不是随包发布的文件：那两份文件在当前部署里是空的，用它们会让整条链停在
        ``metric_translation_unavailable``，用例就变成"什么都没测"的假绿。判定链上的
        每一个适配器（花名册基线、花名册快照、银河快照、发布 store、本地覆盖）都是
        真实实现。
        """

        from lingxi.adapters.postgres_permission_recompute_trigger import (
            _BaselineIdentityLookup,
            _RosterRowsAdapter,
        )
        from lingxi.adapters.postgres_roster_audit import PostgresRosterBaselineReader

        publish_store = PostgresPermissionPublishStore(self._dsn)
        return TargetedPermissionRecompute(
            identities=_BaselineIdentityLookup(
                PostgresRosterBaselineReader(self._dsn).load_active_baseline()
            ),
            roster_snapshot=_RosterRowsAdapter(PostgresRosterSnapshotStore(self._dsn)),
            galaxy=PostgresGalaxySnapshotReader(self._dsn),
            decisions=publish_store,
            publish_history=publish_store,
            role_function_map=ROLE_FUNCTION_MAP,
            metric_translation_map=METRIC_TRANSLATION_MAP,
            audit=self.audit,
            local_overrides=local_override_reader(self._dsn),
            clock=self.clock,
        )

    def _blocked_audits(self) -> list[dict]:
        return [
            fields
            for action, fields in self.audit.records
            if action == "permission_refresh.grant_blocked_account_state"
        ]


class WindowSuspendTest(AccountStateWindowTestCase):
    """Issue #483：**基线读取之后、轮到这个人之前**被停用 + 排空权限。

    这条用例只有真库能证伪：判据落在 ``record_decision`` 那把 ``SELECT ... FOR UPDATE``
    里，与停用写入争的是**同一行的同一把锁**。假 store 上无论实现怎么写都是绿的。
    """

    def test_a_user_suspended_inside_the_window_never_gets_a_grant_back(self) -> None:
        self._setup_two_active_users()

        # 第一轮：两个人都拿到真实权限，并且真的"发布成功过"——撤权侧的
        # `has_publish_footprint` 因此为真，`force_revoke` 才会真的排撤权行。
        first = self._duty().run_once()
        self.assertEqual(first.enqueued, 2)
        external_rows = self._publish_all_pending()
        self.assertEqual(len(external_rows), 2)

        # 第二轮：批基线读到两个人都还有效；轮到窗口用户时，管理员刚好完成"停用 +
        # 即时撤权"。
        self.clock.advance(timedelta(days=1))
        self._write_roster_snapshot(captured_at=self.clock.moment)
        revoked_version: dict[str, int] = {}

        def suspend_and_revoke() -> None:
            self._set_account_state(SUSPENDED_USER, "suspended")
            self._force_revoke_like_production(SUSPENDED_USER)
            revoked_version["value"] = self._version(SUSPENDED_USER)
            self.assertEqual(
                self._latest_payload(SUSPENDED_USER)["permissions"],
                "{}",
                "即时撤权必须先把发布内容清空——这是本用例要防止被批处理盖回去的那一版",
            )

        decorated = _SuspendDuringBatchStore(
            self._dsn, window_user=SUSPENDED_USER, on_suspend=suspend_and_revoke
        )
        report = self._duty(publish_store=decorated).run_once()

        self.assertTrue(decorated.suspended, "交错必须真的发生过，否则这条用例什么都没测")

        # 1. 最终态：最新一条意图仍然是那条空权限撤权，批处理没有盖回非空授权。
        self.assertEqual(self._latest_payload(SUSPENDED_USER)["permissions"], "{}")
        reasons_after_revoke = [
            reason
            for user, version, reason in self._outbox()
            if user == SUSPENDED_USER and version >= revoked_version["value"]
        ]
        self.assertEqual(
            reasons_after_revoke,
            [ADMIN_TARGETED_REVOKE_REASON],
            "撤权之后不得再出现任何一条每日批授权意图",
        )

        # 2. 版本停在 force_revoke 推进后的那一版：批处理**一次都没有再推进**。
        self.assertEqual(self._version(SUSPENDED_USER), revoked_version["value"])

        # 3. 报告与审计：被挡计入专属原因码，`failed` 不加一（被挡是正确结果）。
        self.assertEqual(report.failed, 0, "被账号状态挡住不是故障")
        self.assertEqual(report.reasons.get(SKIP_ACCOUNT_NOT_ENABLED), 1)
        blocked = self._blocked_audits()
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["user"], SUSPENDED_USER)
        self.assertEqual(blocked[0]["account_state"], "suspended")

        # 4. 对照用户不受影响：一个人被挡不带走整轮。
        self.assertEqual(report.examined, 2)
        self.assertEqual(report.unchanged, 1, "对照用户内容没变，判 UNCHANGED")

        # 5. **外部调用层**：把剩下的意图真的发一遍，没有任何一次写入把非空权限
        #    写回这一行——发布行无回发钉在外部调用层，不是只看数据库。
        transport = _RecordingTransport(external_rows)
        store = PostgresPermissionPublishStore(self._dsn)
        seen: list[str] = []
        while True:
            claimed = store.claim_next(exclude=tuple(seen))
            if claimed is None:
                break
            seen.append(claimed.outbox_id)
            attempt = publish_claim(claimed, transport=transport)
            store.complete(attempt, status=attempt.next_status(max_attempts=3))
        self.assertEqual(
            transport.permissions_written(),
            ["{}"],
            "外部表只应收到那一次清空写入；任何一条非空权限写回都说明停用被突破了",
        )
        self.assertEqual(
            transport.read_row(f"rec_{SUSPENDED_USER}")["permissions"],
            "{}",
            "外部表里这一行最终必须是空权限",
        )

    def test_a_later_round_no_longer_touches_the_suspended_user_and_the_state_is_correct(
        self,
    ) -> None:
        """持续性坐实：命中窗口之后，后续批**不再评估也不再纠正**这个人——因为修复
        之后已经不需要纠正了，他的终态本来就是空权限。

        这条钉的是 issue 草案里标为"待进一步坐实"的那一环：下一轮批处理基线已经排除
        ``suspended``，这个人根本不进遍历集合。修复前这意味着错误授权会一直挂着；修复
        后同一条事实变成"没有什么需要纠正"。两件事的代码行为一样，产品含义相反，所以
        必须连同**修复后的正确终态**一起钉死。
        """

        self._setup_two_active_users()
        self._duty().run_once()
        self._publish_all_pending()

        self.clock.advance(timedelta(days=1))
        self._write_roster_snapshot(captured_at=self.clock.moment)

        def suspend_and_revoke() -> None:
            self._set_account_state(SUSPENDED_USER, "suspended")
            self._force_revoke_like_production(SUSPENDED_USER)

        decorated = _SuspendDuringBatchStore(
            self._dsn, window_user=SUSPENDED_USER, on_suspend=suspend_and_revoke
        )
        self._duty(publish_store=decorated).run_once()
        version_after_window = self._version(SUSPENDED_USER)
        payload_after_window = self._latest_payload(SUSPENDED_USER)

        # 再跑一轮：这个人已经是 suspended，基线把他排除在外。
        self.clock.advance(timedelta(days=1))
        self._write_roster_snapshot(captured_at=self.clock.moment)
        report = self._duty().run_once()

        self.assertEqual(report.examined, 1, "停用用户不再进入遍历集合")
        self.assertNotIn(SKIP_ACCOUNT_NOT_ENABLED, report.reasons, "他连被挡的机会都没有")
        self.assertEqual(self._version(SUSPENDED_USER), version_after_window, "后续批不再推进版本")
        self.assertEqual(
            self._latest_payload(SUSPENDED_USER),
            payload_after_window,
            "终态就是即时撤权那一版空权限，后续批既不纠正也不需要纠正",
        )
        self.assertEqual(payload_after_window["permissions"], "{}")


class SuspendedUserLocalPermissionActionTest(AccountStateWindowTestCase):
    """Issue #483 缺口②：对**已停用**用户做本地权限动作触发的定向重算，不得把他的
    真实权限重新排出去。**不需要任何竞态，确定性可复现。**

    机制（编排者与设计稿各自独立回源坐实）：定向重算的身份基线是**花名册审计基线**
    （``ACTIVE_BASELINE_SQL``，**有意包含** ``suspended``，见 Issue #468 留痕），
    ``find_active`` 只是 ``dict.get`` 无二次过滤，``recompute_and_publish`` 主体全程
    不读 ``account_state``——三环叠加，停用用户能一路走到发布。

    真库不可替代的那一半：花名册审计基线的行集合随 ``account_state`` 实时变化，假
    identities 上这条判据无论实现怎么写都是绿的。
    """

    def test_a_recompute_for_a_suspended_user_never_publishes_permissions_again(self) -> None:
        self._setup_two_active_users()
        # 先让这个人有真实的发布足迹与非空权限，再停用 + 即时撤权——这正是管理员点
        # 「停用」之后的真实库状态。
        self._duty().run_once()
        self._publish_all_pending()
        self._set_account_state(SUSPENDED_USER, "suspended")
        self._force_revoke_like_production(SUSPENDED_USER)
        version_after_revoke = self._version(SUSPENDED_USER)
        self.assertEqual(self._latest_payload(SUSPENDED_USER)["permissions"], "{}")

        # 管理员随后对这个已停用的人做一次本地权限动作：确认成功后触发的正是
        # `recompute_and_publish`（`PermissionRecomputeAdapter.trigger` 对一切非
        # SUSPEND_USER 的动作都走这一条），身份仍然能从花名册审计基线里查到。
        outcome = self._production_recompute().recompute_and_publish(user_id=SUSPENDED_USER)

        self.assertIs(outcome.kind, RecomputeKind.SKIPPED)
        self.assertEqual(outcome.reason, TARGETED_SKIP_ACCOUNT_NOT_ENABLED)
        skipped = [
            fields
            for action, fields in self.audit.records
            if action == "permission_targeted_recompute.skipped"
            and fields.get("reason") == TARGETED_SKIP_ACCOUNT_NOT_ENABLED
        ]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["user"], SUSPENDED_USER)
        self.assertEqual(skipped[0]["account_state"], "suspended")

        # 零写入：版本不推进、不多一条意图、发布内容仍然是空的。
        self.assertEqual(self._version(SUSPENDED_USER), version_after_revoke)
        self.assertEqual(self._latest_payload(SUSPENDED_USER)["permissions"], "{}")

    def test_the_same_recompute_still_publishes_for_an_enabled_user(self) -> None:
        """正向对照：同一条定向重算路径对 ``enabled`` 用户照常发权。

        没有这一条，上一条用例在"守卫把所有人都挡住"这种停服级误伤下**仍然是绿的**。
        """

        self._setup_two_active_users()
        outcome = self._production_recompute().recompute_and_publish(user_id=ACTIVE_USER)

        self.assertIs(outcome.kind, RecomputeKind.ENQUEUED)
        self.assertEqual(
            self._latest_payload(ACTIVE_USER)["permissions"], f'{{"BC-甲":["{METRIC_NAME}"]}}'
        )

    def test_force_revoke_for_a_suspended_user_is_never_blocked(self) -> None:
        """方向相反的那一处：停用即时撤销必须照常生效（挡住它 = 停用彻底失效）。

        这也是「停用即时撤销」既有回归的真库复核：它走的是同一个
        ``record_decision``，只是声明"不要求账号有效"。
        """

        self._setup_two_active_users()
        self._duty().run_once()
        self._publish_all_pending()
        before = self._version(SUSPENDED_USER)

        self._set_account_state(SUSPENDED_USER, "suspended")
        outcome = self._production_recompute().force_revoke(user_id=SUSPENDED_USER)

        self.assertIs(outcome.kind, RecomputeKind.REVOKED)
        self.assertEqual(self._version(SUSPENDED_USER), before + 1)
        self.assertEqual(self._latest_payload(SUSPENDED_USER)["permissions"], "{}")


if __name__ == "__main__":
    unittest.main()
