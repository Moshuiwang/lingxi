"""每日权限重算职责的纯逻辑验收（Issue #156 / S-C-03a，S-C-03b 补上撤权侧）。

**本文件承担 `V-权限-08` 刷新侧的纯逻辑面**（产品负责人 2026-08-18 裁定 3：撤权 =
保行、清空 ``permissions``、不碰密文、不新建行）。`V-权限-07` 的调度与发布半边同样
落在这里与 ``tests/test_permission_publish_duty.py``；说明段见
``docs/技术设计/验收矩阵.md`` 的权限发布与同步一节。

本文件钉的是本职责自己的行为，尤其是这几条否定断言：

1. 花名册快照缺失 / 非今日 → 整轮不跑，**一次银河读取都不发起**，零发布意图；
2. 没有当前有效银河批次 → 零发布意图，审计可分辨；
3. **从来没有过发布行的撤权用户** → 零发布意图、零令牌读取，审计原因可分辨；
4. **匹配失败不算撤权信号** → 连"有没有发布行"都不查，更不清空任何人的权限；
5. 撤权行**不携带、不清空、不覆盖 ``token_cipher``**，因此在发布层建不出新行；
6. 重算职责**绝不签发令牌、也不发任何通知**（端口里没有签发口，也没有发送口）；
7. ``UNCHANGED`` 不计入新意图（真库贯穿在 ``tests/test_permission_refresh_postgres.py``）；
8. 单个用户异常不中断整轮；
9. 报告与审计不含权限值明细、邮箱、姓名、工号等敏感原值。

真库那一半（active 筛选的否定样本、版本不推进、意图只有一条、撤权重复判 ``UNCHANGED``）
在 ``tests/test_permission_refresh_postgres.py``。
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
import threading
import unittest
from datetime import date, datetime, timedelta, timezone

from lingxi.adapters.postgres_galaxy_snapshot import GalaxyPermissionSnapshot
from lingxi.adapters.postgres_roster_audit import ACTIVE_BASELINE_SQL
from lingxi.apps.scheduler import (
    PERMISSION_REFRESH_REASON,
    PERMISSION_REVOKE_REASON,
    PermissionRefreshDuty,
    PermissionRefreshReport,
    SchedulerConfig,
    SchedulerLoop,
    build_loop,
)
from lingxi.apps.scheduler.permission_refresh import (
    REASON_FULLY_SUPPRESSED,
    SKIP_ARCHIVED_IDENTITY_INCOMPLETE,
    SKIP_METRIC_TRANSLATION_UNAVAILABLE,
    SKIP_METRIC_TRANSLATION_UNCOVERED,
    SKIP_MISSING_PERSONNEL_ID,
    SKIP_MISSING_SNAPSHOT,
    SKIP_NO_GALAXY_BATCH,
    SKIP_NO_PUBLISHED_ROW,
    SKIP_STALE_SNAPSHOT,
    TRIGGER_GRANT,
    TRIGGER_REVOKE,
)
from lingxi.core.identity.roster_audit import ArchivedIdentity
from lingxi.core.identity.roster_snapshot import StoredSnapshotFacts
from lingxi.core.permission.legacy_source import REASON_LEGACY_READ_FAILED
from lingxi.core.permission.local_override import LocalPermissionOverrideEntry, OverrideDirection
from lingxi.core.permission.merge_sources import (
    REASON_GRANT_REDUNDANT_WILDCARD,
    REASON_LOCAL_OVERRIDE_READ_FAILED,
    REASON_SUPPRESS_INAPPLICABLE_WILDCARD,
)
from lingxi.core.permission.publish import ExistingPermissionRow
from lingxi.core.permission.publish_row import ADMIN_FULL_ACCESS_FUNCTION

REPOSITORY_ROOT = pathlib.Path(__file__).parents[1]
DUTY_SOURCE = REPOSITORY_ROOT / "src" / "lingxi" / "apps" / "scheduler" / "permission_refresh.py"


def code_without_docstrings(path: pathlib.Path) -> str:
    """一个源文件**去掉全部文档字符串与注释**之后的正文。

    形状断言钉的是代码，不是文档：模块文档里恰恰必须写明"不通知、不删行、不签发令牌"
    这些词，拿原文扫描会把一份写得越清楚的文档判得越红。用 ``ast`` 剥掉 docstring 再
    ``unparse``（注释在解析阶段就没了），剩下的就是真正会执行的那部分。

    ``tests/test_permission_refresh_postgres.py`` 也用它扫适配器，因此放在这里共享
    一份——两处各写一份的话，其中一份迟早会退化成"只剥模块文档"的半吊子实现。
    """

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            del body[0]
            if not body:
                body.append(ast.Pass())
    return ast.unparse(tree)


def duty_code() -> str:
    """每日权限重算职责的代码正文（不含文档字符串与注释）。"""

    return code_without_docstrings(DUTY_SOURCE)


TODAY = datetime(2026, 8, 17, 3, 0, tzinfo=timezone.utc)

# 资料值：报告、审计与日志里一个都不许出现。
NAME_ONE = "化名甲"
EMAIL_ONE = "jiaming.jia@example.invalid"
EMPLOYEE_ONE = "E1001"
NAME_TWO = "化名乙"
EMAIL_TWO = "yiming.yi@example.invalid"
EMPLOYEE_TWO = "E1002"
GALAXY_ACCOUNT_ONE = "g-1001"
GALAXY_ACCOUNT_TWO = "g-1002"
COMPANY_ID = "1011"
COMPANY_ID_TWO = "1012"
FUNCTION_LABEL = "运营"
ROLE_NAME = "A运营"
#: 翻译层（Issue #227）的测试夹具指标名——**虚构占位，非真实指标名**，只用于证明
#: "翻译成功之后 permissions 里写的是翻译产物，不是职能标签本身"这条链路是通的。
METRIC_NAME = "示例指标-日活"
METRIC_NAME_TWO = "示例指标-收入"

USER_ONE = "usr_01JQZX3M5N7P9R1T3V5W7Y9A0B"
USER_TWO = "usr_01K2AB4D6F8H0J2M4P6R8T0V2W"

#: 一份形状合法的密文（biai-agent 加密规格 v1 的公开测试向量，非生产密钥、非生产令牌）。
TOKEN_CIPHER = "RklYRURJVjEyMzQ1Njc4OX5gpf2vKqJiLgzu2n4kug1V1rz6DDt1OCgAZVpg1pL+"
#: 合法的 32 字节主密钥（同一份公开测试向量）。
SPEC_MASTER_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="

ROLE_FUNCTION_MAP = {ROLE_NAME: FUNCTION_LABEL}

#: 翻译层的默认测试夹具：覆盖默认场景用到的公司+职能组合（``COMPANY_ID`` +
#: ``FUNCTION_LABEL``）与「全非」通配（``"*"``），足够让既有的"granted user"用例
#: 走完整条链路产出一条发布意图。第二家公司特意映射到**不同**的指标名，供
#: ``MetricTranslationGateTest`` 证明"公司+职能"是联合键、不是只看职能。
METRIC_TRANSLATION_MAP = {
    COMPANY_ID: {FUNCTION_LABEL: (METRIC_NAME,)},
    COMPANY_ID_TWO: {FUNCTION_LABEL: (METRIC_NAME_TWO,)},
    "*": {FUNCTION_LABEL: (METRIC_NAME,)},
}


# --------------------------------------------------------------------------
# 假实现：全部只记调用，不做任何 I/O
# --------------------------------------------------------------------------


class RecordingAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, object]]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, dict(fields)))

    def actions(self) -> list[str]:
        return [action for action, _ in self.records]

    def fields_for(self, action: str) -> list[dict[str, object]]:
        return [fields for name, fields in self.records if name == action]


class FakeBaseline:
    def __init__(self, *identities: ArchivedIdentity) -> None:
        self._identities = identities
        self.calls = 0

    def load_active_baseline(self):
        self.calls += 1
        return self._identities


class FakeRosterSnapshot:
    """花名册持久快照的假实现。``rows`` 直接用映射，形状与 ``RosterRow`` 的键一致。"""

    def __init__(self, *, captured_at: datetime | None, rows=()) -> None:
        self._captured_at = captured_at
        self._rows = tuple(rows)
        self.facts_calls = 0
        self.load_calls = 0

    def _facts(self):
        if self._captured_at is None:
            return None
        return StoredSnapshotFacts(
            snapshot_id="rsn_fake", captured_at=self._captured_at, row_count=max(len(self._rows), 1)
        )

    def load_facts(self):
        self.facts_calls += 1
        return self._facts()

    def load(self):
        self.load_calls += 1
        facts = self._facts()
        if facts is None:
            return None
        return _StoredSnapshot(facts, self._rows)


class _StoredSnapshot:
    def __init__(self, facts, rows) -> None:
        self.facts = facts
        self.rows = rows


class FakeGalaxy:
    def __init__(self, snapshot: GalaxyPermissionSnapshot | None) -> None:
        self._snapshot = snapshot
        self.calls = 0

    def load_current(self):
        self.calls += 1
        return self._snapshot


class FakeTokens:
    """令牌库的假实现。

    ``issue_token`` 存在**而且会炸**：这是"重算职责绝不签发令牌"这条断言的运行期
    抓手——一旦有人在职责里接上签发，那个用户会以异常收场，用例立刻变红。
    """

    def __init__(self, ciphers: dict[str, str] | None = None) -> None:
        self._ciphers = ciphers or {}
        self.read_calls: list[str] = []
        self.issue_calls: list[str] = []

    def token_cipher(self, user_id: str) -> str | None:
        self.read_calls.append(user_id)
        return self._ciphers.get(user_id)

    def issue_token(self, user_id: str):  # pragma: no cover - 被调用即断言失败
        self.issue_calls.append(user_id)
        raise AssertionError("每日权限重算不得签发 MCP 令牌")


class FakePublishHistory:
    """「这个人在发布链上有没有留下过足迹」的假实现。

    默认**谁都没有**：撤权那一路因此照旧跳过，S-C-03a 的既有用例不受影响。
    真实实现里"足迹"= 发布成功过 **或** 当前还有 ``pending``/``publishing`` 的意图在途
    （二级审查 N7），判据见 ``adapters/postgres_permission_publish.py`` 的同名方法。
    """

    def __init__(self, published_users: set[str] | None = None) -> None:
        self._published = published_users or set()
        self.calls: list[str] = []

    def has_publish_footprint(self, user_id: str) -> bool:
        self.calls.append(user_id)
        return user_id in self._published


class FakeLocalOverrides:
    """本地权限覆盖读取口的假实现（S-P-3，Issue #319）。

    ``entries`` 按用户登记条目；``fail_for`` 里的用户读取时直接抛异常，供
    :class:`LocalOverrideMergeTest` 钉住"读取失败只降级、不整用户/整轮失败"。
    """

    def __init__(
        self,
        entries: dict[str, tuple[LocalPermissionOverrideEntry, ...]] | None = None,
        *,
        fail_for: set[str] | None = None,
    ) -> None:
        self._entries = entries or {}
        self._fail_for = fail_for or set()
        self.calls: list[str] = []

    def effective_entries(self, *, user_id: str) -> tuple[LocalPermissionOverrideEntry, ...]:
        self.calls.append(user_id)
        if user_id in self._fail_for:
            raise RuntimeError("注入的本地覆盖读取失败")
        return self._entries.get(user_id, ())


class FakeLegacyTable:
    """存量权限只读源的假实现（S-P-2，Issue #319 / Trace #328）。

    ``rows_by_email`` 按（规范化）邮箱登记该用户存量行的 ``permissions`` 单元格文本；
    ``find_error`` 非空时直接抛异常；``duplicate_for`` 里的邮箱命中两行，供
    :class:`LegacySourceMergeTest` 钉住"读取失败/多行冲突只降级、不整用户/整轮失败"。
    """

    def __init__(
        self,
        rows_by_email: dict[str, str] | None = None,
        *,
        find_error: Exception | None = None,
        duplicate_for: set[str] | None = None,
    ) -> None:
        self._rows = rows_by_email or {}
        self._find_error = find_error
        self._duplicate_for = duplicate_for or set()
        self.find_calls: list[tuple[str, str]] = []

    def find_rows(self, *, record_key: str, email: str) -> tuple[ExistingPermissionRow, ...]:
        self.find_calls.append((record_key, email))
        if self._find_error is not None:
            raise self._find_error
        if email not in self._rows:
            return ()
        row = ExistingPermissionRow(
            record_id=f"rec_{email}",
            fields={"record_key": email, "email": email, "permissions": self._rows[email]},
        )
        return (row, row) if email in self._duplicate_for else (row,)


class _FakeDecision:
    def __init__(self, enqueued: bool, *, cleared_events: int = 0) -> None:
        self.enqueued = enqueued
        self.cleared_events = cleared_events


class FakeDecisions:
    """``record_decision`` 的假实现。``cleared_events_by_user`` 配置「假装真的清出了
    多少条已送达正文事件」（S-P-5，Trace #328）——只在 ``enqueued`` 且调用方传了
    ``clear_delivered_content=True`` 时才会被使用，与真实 ``record_decision`` 的
    "``UNCHANGED`` 恒为 0、清理开关必须调用方显式打开"同一条语义，供
    :class:`DeliveredContentPurgeTest` 钉住职责层的接入面。"""

    def __init__(
        self,
        *,
        unchanged_users: set[str] | None = None,
        failing_users: set[str] | None = None,
        cleared_events_by_user: dict[str, int] | None = None,
    ) -> None:
        self.calls: list[dict] = []
        self._unchanged = unchanged_users or set()
        self._failing = failing_users or set()
        self._cleared_events_by_user = cleared_events_by_user or {}

    def record_decision(
        self, *, user_id, row, reason, decided_at, clear_delivered_content: bool = False
    ):
        if user_id in self._failing:
            raise RuntimeError("注入的落库失败")
        self.calls.append(
            {
                "user_id": user_id,
                "row": row,
                "reason": reason,
                "decided_at": decided_at,
                "clear_delivered_content": clear_delivered_content,
            }
        )
        enqueued = user_id not in self._unchanged
        cleared = self._cleared_events_by_user.get(user_id, 0) if clear_delivered_content and enqueued else 0
        return _FakeDecision(enqueued, cleared_events=cleared)


class FixedClock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, delta: timedelta) -> None:
        self.moment = self.moment + delta


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


def identity(
    app_user_id: str = USER_ONE,
    *,
    personnel_id: str = "ou_person_0001",
    display_name: str = NAME_ONE,
    employee_no: str = EMPLOYEE_ONE,
    email: str = EMAIL_ONE,
) -> ArchivedIdentity:
    return ArchivedIdentity(
        app_user_id=app_user_id,
        personnel_id=personnel_id,
        display_name=display_name,
        employee_no=employee_no,
        email=email,
    )


def roster_row(
    personnel_id: str = "ou_person_0001",
    *,
    employee_no: str = EMPLOYEE_ONE,
    email: str = EMAIL_ONE,
    name: str = NAME_ONE,
) -> dict[str, str]:
    return {
        "personnel_id": personnel_id,
        "employee_no": employee_no,
        "email": email,
        "name": name,
        "record_id": "rec_fake",
    }


def galaxy_snapshot(
    *,
    accounts=((GALAXY_ACCOUNT_ONE, EMPLOYEE_ONE, EMAIL_ONE),),
    roles=((GALAXY_ACCOUNT_ONE, ROLE_NAME),),
    countries=((GALAXY_ACCOUNT_ONE, "KE"),),
    country_rows=(("KE", "KENYA", "肯尼亚", COMPANY_ID),),
) -> GalaxyPermissionSnapshot:
    role_rows: dict[str, list[dict[str, str]]] = {}
    for account, role_name in roles:
        role_rows.setdefault(account, []).append({"user_id": account, "role_name": role_name})
    country_scope: dict[str, list[dict[str, str]]] = {}
    for account, country_key in countries:
        country_scope.setdefault(account, []).append(
            {"user_id": account, "datacountry_id": country_key}
        )
    return GalaxyPermissionSnapshot(
        batch_id="gib_fake",
        user_rows=tuple(
            {"user_id": account, "user_name": user_name, "email": email}
            for account, user_name, email in accounts
        ),
        country_rows=tuple(
            {"country_key": key, "name": name, "name_cn": name_cn, "boss_company_id": company}
            for key, name, name_cn, company in country_rows
        ),
        role_rows_by_user={key: tuple(value) for key, value in role_rows.items()},
        datacountry_rows_by_user={key: tuple(value) for key, value in country_scope.items()},
    )


def build_duty(
    *,
    identities=(),
    roster_captured_at: datetime | None = TODAY,
    roster_rows=None,
    galaxy=None,
    tokens: FakeTokens | None = None,
    decisions: FakeDecisions | None = None,
    audit: RecordingAudit | None = None,
    clock: FixedClock | None = None,
    stop: threading.Event | None = None,
    published_users: set[str] | None = None,
    metric_translation_map=None,
    role_function_map=None,
    local_overrides: FakeLocalOverrides | None = None,
    legacy_source: FakeLegacyTable | None = None,
):
    audit = audit or RecordingAudit()
    tokens = tokens or FakeTokens({USER_ONE: TOKEN_CIPHER, USER_TWO: TOKEN_CIPHER})
    decisions = decisions or FakeDecisions()
    history = FakePublishHistory(published_users)
    snapshot_store = FakeRosterSnapshot(
        captured_at=roster_captured_at,
        rows=roster_rows if roster_rows is not None else (roster_row(),),
    )
    galaxy_reader = FakeGalaxy(galaxy_snapshot() if galaxy is None else galaxy)
    duty = PermissionRefreshDuty(
        baseline_reader=FakeBaseline(*identities),
        roster_snapshot=snapshot_store,
        galaxy=galaxy_reader,
        decisions=decisions,
        publish_history=history,
        token_ciphers=tokens,
        role_function_map=ROLE_FUNCTION_MAP if role_function_map is None else role_function_map,
        metric_translation_map=(
            METRIC_TRANSLATION_MAP if metric_translation_map is None else metric_translation_map
        ),
        audit=audit,
        clock=clock or FixedClock(TODAY),
        stop=stop,
        local_overrides=local_overrides,
        legacy_source=legacy_source,
    )
    return duty, {
        "audit": audit,
        "tokens": tokens,
        "decisions": decisions,
        "history": history,
        "roster": snapshot_store,
        "galaxy": galaxy_reader,
    }


# --------------------------------------------------------------------------
# 一、顺序判据：先花名册、再银河
# --------------------------------------------------------------------------


class RosterFirstOrderingTest(unittest.TestCase):
    """否定断言：花名册快照不是今天的，这一轮**一次银河读取都不发起**，零发布意图。"""

    def test_without_any_snapshot_the_round_does_not_run(self) -> None:
        duty, parts = build_duty(identities=(identity(),), roster_captured_at=None)

        self.assertIsNone(duty.run_once())

        self.assertEqual(parts["galaxy"].calls, 0, "花名册不新鲜时不得读银河")
        self.assertEqual(parts["decisions"].calls, [], "不得产生任何发布意图")
        self.assertEqual(parts["audit"].actions(), ["permission_refresh.skipped_roster_not_fresh"])
        self.assertEqual(
            parts["audit"].fields_for("permission_refresh.skipped_roster_not_fresh")[0]["reason"],
            SKIP_MISSING_SNAPSHOT,
        )
        self.assertIsNone(duty.completed_on, "跳过的一轮不置水位")

    def test_a_snapshot_from_yesterday_does_not_run_either(self) -> None:
        duty, parts = build_duty(
            identities=(identity(),), roster_captured_at=TODAY - timedelta(days=1)
        )

        self.assertIsNone(duty.run_once())

        self.assertEqual(parts["galaxy"].calls, 0)
        self.assertEqual(parts["decisions"].calls, [])
        fields = parts["audit"].fields_for("permission_refresh.skipped_roster_not_fresh")[0]
        self.assertEqual(fields["reason"], SKIP_STALE_SNAPSHOT)
        self.assertEqual(fields["snapshot_date"], "2026-08-16")

    def test_a_snapshot_from_later_today_is_fresh_enough(self) -> None:
        """判据是**当天**（UTC 日界），不是"最近 24 小时"。

        花名册那一轮在同一天早些时候跑过就算数——两个职责在同一个 loop 里依次执行，
        要求"刚刚才换"会让权限重算永远差一轮。
        """

        duty, parts = build_duty(
            identities=(identity(),), roster_captured_at=TODAY - timedelta(hours=2)
        )

        report = duty.run_once()

        self.assertIsNotNone(report)
        self.assertEqual(parts["galaxy"].calls, 1)
        self.assertEqual(len(parts["decisions"].calls), 1)

    def test_the_freshness_gate_is_read_from_the_cheap_metadata_first(self) -> None:
        """每分钟一轮的判据只读元信息，不读一千两百行。

        两种"不新鲜"都要在**元信息那一步**就停下：判据在当前部署下每轮都不成立，
        用整份快照去做一次每分钟都要做的判断，代价与结论完全不成比例。这条同时也是
        第一道日期判据的独立抓手——只留下 ``load()`` 之后那道复核的话，整份快照会被
        白读一遍。
        """

        for captured_at, label in ((None, "没有快照"), (TODAY - timedelta(days=1), "昨天的快照")):
            with self.subTest(label=label):
                duty, parts = build_duty(identities=(identity(),), roster_captured_at=captured_at)

                duty.run_once()

                self.assertEqual(parts["roster"].facts_calls, 1)
                self.assertEqual(parts["roster"].load_calls, 0, "判据不成立时不得读整份快照")

    def test_a_snapshot_replaced_between_the_two_reads_does_not_run_either(self) -> None:
        """元信息说今天、整份快照却是昨天的：**并发替换**恰好落在两条语句之间。

        这是第二道日期判据的抓手。花名册审计职责就在同一个进程里，它替换快照与本职责
        读快照之间没有共同的事务边界；两条语句在 ``READ COMMITTED`` 下各取一次数据库
        快照，因此这个错位是真实可达的，不是防御式编程。
        """

        duty, parts = build_duty(identities=(identity(),))
        fresh = parts["roster"]._facts()

        def facts_today():
            parts["roster"].facts_calls += 1
            return fresh

        parts["roster"].load_facts = facts_today  # type: ignore[assignment]
        parts["roster"]._captured_at = TODAY - timedelta(days=1)

        self.assertIsNone(duty.run_once())

        self.assertEqual(parts["roster"].load_calls, 1)
        self.assertEqual(parts["galaxy"].calls, 0, "错位的快照不得驱动一次银河读取")
        self.assertEqual(parts["decisions"].calls, [])
        self.assertEqual(
            parts["audit"].fields_for("permission_refresh.skipped_roster_not_fresh")[0]["reason"],
            SKIP_STALE_SNAPSHOT,
        )

    def test_the_skip_audit_is_written_once_per_day_and_reason(self) -> None:
        """否则一天会刷出一千四百多条内容相同的审计，真正的信号会被埋掉。"""

        clock = FixedClock(TODAY)
        duty, parts = build_duty(identities=(identity(),), roster_captured_at=None, clock=clock)

        for _ in range(3):
            duty.run_once()
        clock.advance(timedelta(days=1))
        duty.run_once()

        self.assertEqual(len(parts["audit"].records), 2, "同日同因只留一条，隔天再留一条")

    def test_a_reason_that_comes_back_the_same_day_is_not_audited_twice(self) -> None:
        """同一天里原因来回变（A→B→A）：每一种只留一条，回到 A 不重记。

        去重水位记的是当天**已经记过哪些原因**，不是"最后一个原因"。只记最后一个的话，
        这条路径上 A 会被记两次——而原因来回变正是它最可能发生的场景（花名册快照到了
        又被换回旧的、银河批次过期又重导）。
        """

        clock = FixedClock(TODAY)
        duty, parts = build_duty(identities=(identity(),), roster_captured_at=None, clock=clock)

        duty.run_once()  # A：没有快照
        parts["roster"]._captured_at = TODAY  # B：快照有了，但没有银河批次
        parts["galaxy"]._snapshot = None
        duty.run_once()
        parts["roster"]._captured_at = None  # 回到 A
        duty.run_once()

        self.assertEqual(
            [action for action, _ in parts["audit"].records],
            [
                "permission_refresh.skipped_roster_not_fresh",
                "permission_refresh.skipped_no_galaxy_batch",
            ],
            "A→B→A 只留 A、B 各一条",
        )


class UtcDayBoundaryTest(unittest.TestCase):
    """日界一律按 UTC 判定，不按时钟自身的时区。"""

    #: 本地 ``+08:00`` 的 ``00:30``，其 UTC 时刻是**前一天** 16:30。
    LOCAL_MIDNIGHT = datetime(2026, 8, 17, 0, 30, tzinfo=timezone(timedelta(hours=8)))

    def test_a_snapshot_from_the_same_utc_day_runs_even_across_the_local_midnight(self) -> None:
        """快照取于 08-16 20:00Z，与时钟的 UTC 日期（08-16）同一天 → 照跑。

        按时钟自身时区判会把"今天"算成 08-17，于是这份快照被误判成昨天的，整天不重算。
        """

        duty, parts = build_duty(
            identities=(identity(),),
            roster_captured_at=datetime(2026, 8, 16, 20, 0, tzinfo=timezone.utc),
            clock=FixedClock(self.LOCAL_MIDNIGHT),
        )

        report = duty.run_once()

        self.assertIsNotNone(report)
        self.assertEqual(report.enqueued, 1)
        self.assertEqual(duty.completed_on, date(2026, 8, 16), "水位是 UTC 日期")

    def test_a_snapshot_from_the_next_utc_day_does_not_run(self) -> None:
        """快照取于 08-17 01:00Z：本地日历上是"今天"，UTC 日历上是明天 → 不跑。

        与上一条互为镜像：按时钟自身时区判，这两条的结论会正好反过来。
        """

        duty, parts = build_duty(
            identities=(identity(),),
            roster_captured_at=datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc),
            clock=FixedClock(self.LOCAL_MIDNIGHT),
        )

        self.assertIsNone(duty.run_once())

        self.assertEqual(parts["decisions"].calls, [])
        fields = parts["audit"].fields_for("permission_refresh.skipped_roster_not_fresh")[0]
        self.assertEqual(fields["reason"], SKIP_STALE_SNAPSHOT)
        self.assertEqual(fields["report_date"], "2026-08-16", "审计里的日期同一把尺子")
        self.assertEqual(fields["snapshot_date"], "2026-08-17")

    def test_a_naive_clock_fails_fast(self) -> None:
        """没有时区的时钟**直接失败**，不按本地时区静默解读。

        整轮异常按 duty 惯例上抛，由 :class:`SchedulerLoop` 隔离并在下一轮重试——
        比"在跨时区部署上悄悄算错日界"响亮得多。
        """

        duty, parts = build_duty(
            identities=(identity(),), clock=FixedClock(datetime(2026, 8, 17, 3, 0))
        )

        with self.assertRaises(ValueError):
            duty.run_once()
        self.assertEqual(parts["decisions"].calls, [])

    def test_there_is_no_bypass_switch_for_the_ordering_gate(self) -> None:
        """否定断言：判据不得有旁路开关。

        源码级断言——一个"允许用旧花名册重算"的环境变量或参数，会在第一次运维着急时
        被打开，然后再也不会被关上。
        """

        source = duty_code()
        for forbidden in ("os.environ", "getenv", "force", "ignore_stale", "allow_stale"):
            self.assertNotIn(forbidden, source, f"顺序判据不得存在旁路：{forbidden}")


# --------------------------------------------------------------------------
# 二、银河批次
# --------------------------------------------------------------------------


class GalaxyBatchTest(unittest.TestCase):
    def test_without_a_current_batch_nothing_is_enqueued(self) -> None:
        duty, parts = build_duty(identities=(identity(),), galaxy=None)
        parts["galaxy"]._snapshot = None  # 没有当前有效批次

        self.assertIsNone(duty.run_once())

        self.assertEqual(parts["decisions"].calls, [], "没有有效批次时不得产生发布意图")
        self.assertEqual(parts["audit"].actions(), ["permission_refresh.skipped_no_galaxy_batch"])
        self.assertEqual(
            parts["audit"].fields_for("permission_refresh.skipped_no_galaxy_batch")[0]["reason"],
            SKIP_NO_GALAXY_BATCH,
        )
        self.assertIsNone(duty.completed_on)

    def test_the_batch_is_only_read_after_the_roster_gate_passes(self) -> None:
        duty, parts = build_duty(identities=(identity(),))

        duty.run_once()

        self.assertEqual(parts["roster"].facts_calls, 1)
        self.assertEqual(parts["galaxy"].calls, 1)


# --------------------------------------------------------------------------
# 三、有权限的用户
# --------------------------------------------------------------------------


class GrantedUserTest(unittest.TestCase):
    def test_a_granted_user_produces_exactly_one_publish_intent(self) -> None:
        duty, parts = build_duty(identities=(identity(),))

        report = duty.run_once()

        self.assertEqual(report.enqueued, 1)
        self.assertEqual(report.unchanged, 0)
        self.assertEqual(report.revoked, 0)
        self.assertEqual(report.examined, 1)
        self.assertEqual(len(parts["decisions"].calls), 1)
        call = parts["decisions"].calls[0]
        self.assertEqual(call["user_id"], USER_ONE)
        self.assertEqual(call["reason"], PERMISSION_REFRESH_REASON)
        self.assertEqual(call["decided_at"], TODAY)

    def test_the_published_row_is_built_from_the_archived_identity(self) -> None:
        duty, parts = build_duty(identities=(identity(),))

        duty.run_once()

        row = parts["decisions"].calls[0]["row"]
        self.assertEqual(row.record_key, EMAIL_ONE)
        self.assertEqual(row.email, EMAIL_ONE)
        self.assertEqual(row.name, NAME_ONE)
        # 翻译层（Issue #227）接线之后，写进 permissions 的是**翻译产物**（指标名），
        # 不再是聚合层的原始职能标签——这条断言与 MetricTranslationGateTest 互补：
        # 这里证明"翻译成功时结果正确"，那边证明"翻译失败时不发布、不撤权"。
        self.assertEqual(row.permissions, f'{{"{COMPANY_ID}":["{METRIC_NAME}"]}}')
        self.assertEqual(row.status, "approved")
        self.assertEqual(row.updated_at, "2026-08-17T03:00:00Z")
        self.assertEqual(row.token_cipher, TOKEN_CIPHER)

    def test_a_user_without_a_registered_token_gets_a_row_without_one(self) -> None:
        """取不到就传 ``None``——发布层随后以 ``missing_token_cipher`` 失败关闭。

        **这正是预期**：签发属于待裁定的产品结（#203），重算职责不替它做决定。
        """

        tokens = FakeTokens({})
        duty, parts = build_duty(identities=(identity(),), tokens=tokens)

        duty.run_once()

        self.assertIsNone(parts["decisions"].calls[0]["row"].token_cipher)
        self.assertEqual(tokens.read_calls, [USER_ONE])
        self.assertEqual(tokens.issue_calls, [], "重算职责一次都不签发令牌")

    def test_unchanged_users_are_counted_apart_from_new_intents(self) -> None:
        """``UNCHANGED`` 的判定在 ``record_decision`` 里，本职责只如实计数。"""

        decisions = FakeDecisions(unchanged_users={USER_ONE})
        duty, parts = build_duty(identities=(identity(),), decisions=decisions)

        report = duty.run_once()

        self.assertEqual(report.enqueued, 0)
        self.assertEqual(report.unchanged, 1)
        self.assertEqual(report.processed, 1)

    def test_the_duty_never_computes_a_permission_version_itself(self) -> None:
        """版本推进与幂等全部由 ``record_decision`` 承担（源码级断言）。"""

        source = duty_code()
        self.assertNotIn("permission_version", source)


# --------------------------------------------------------------------------
# 三点五、翻译层：未覆盖就跳过，不发布、不撤权（Issue #227）
# --------------------------------------------------------------------------


class RoundLevelTranslationGateTest(unittest.TestCase):
    """翻译映射**整体为空**时的整轮闸门（外部独立审查 2026-08-18 坐实的 P1 修复）。

    这层判据在遍历任何用户之前生效：`run_once` 直接返回 ``None``，与花名册不新鲜、
    没有当前有效银河批次同一姿态——**不是**逐用户判据，因此授权与撤权**一起**被
    挡住，不存在"只挡授权、撤权照旧"的旁路。这正是外部审查抓到的漏洞形状：此前
    翻译闸只接在 `aggregate.granted=True` 的分支上，`_revoke` 完全不经过它，能直接
    把 ``{}`` 写进正式表。
    """

    def test_the_round_produces_no_report_and_no_decisions(self) -> None:
        """整份映射为空：`run_once` 返回 ``None``（不是一份 ``examined=0`` 的报告），
        零发布意图，零遍历——与花名册/银河两组前置判据完全同构。"""

        duty, parts = build_duty(identities=(identity(),), metric_translation_map={})

        report = duty.run_once()

        self.assertIsNone(report, "整轮判据不成立时不产出报告，与其余两组前置判据同构")
        self.assertEqual(parts["decisions"].calls, [], "一条发布意图都不得产出")
        self.assertEqual(parts["history"].calls, [], "撤权足迹查询也不得发生——遍历根本没开始")
        self.assertEqual(parts["tokens"].read_calls, [], "令牌表也不得被查询")

    def test_the_gate_blocks_a_grant_that_would_otherwise_publish(self) -> None:
        """一个原本会被授权发布的用户，翻译层清空映射后一条都不发布——效果与
        "值列表仍是职能标签则不得真实发布"完全一致（Issue #227「TO PM」一节的承诺），
        现在由整轮判据而不是逐用户判据兑现。"""

        duty, parts = build_duty(
            identities=(identity(), identity(USER_TWO, personnel_id="ou_person_0002", email=EMAIL_TWO, display_name=NAME_TWO, employee_no=EMPLOYEE_TWO)),
            roster_rows=(
                roster_row(),
                roster_row("ou_person_0002", employee_no=EMPLOYEE_TWO, email=EMAIL_TWO, name=NAME_TWO),
            ),
            galaxy=galaxy_snapshot(
                accounts=(
                    (GALAXY_ACCOUNT_ONE, EMPLOYEE_ONE, EMAIL_ONE),
                    (GALAXY_ACCOUNT_TWO, EMPLOYEE_TWO, EMAIL_TWO),
                ),
                roles=((GALAXY_ACCOUNT_ONE, ROLE_NAME), (GALAXY_ACCOUNT_TWO, ROLE_NAME)),
                countries=((GALAXY_ACCOUNT_ONE, "KE"), (GALAXY_ACCOUNT_TWO, "KE")),
            ),
            metric_translation_map={},
        )

        report = duty.run_once()

        self.assertIsNone(report)
        self.assertEqual(parts["decisions"].calls, [])

    def test_the_gate_blocks_a_revocation_that_would_otherwise_publish(self) -> None:
        """**P1 的核心钉子**：一个 ``granted=False``（银河侧无角色）且在发布链上有
        足迹（曾经发布过、``has_publish_footprint`` 为真）的用户——按撤权路径的既有
        规则本该排出一条清空 ``permissions`` 的更新意图；映射为空时，这条撤权意图
        同样**排不出来**。对照组（不清空映射）证明"如果没有这道闸，这个人确实会被
        撤权发布"——见 ``RevocationPublishTest`` 用同一套 ``galaxy=galaxy_snapshot(
        roles=())`` + ``published_users={USER_ONE}`` 夹具在映射非空时产出恰一条撤权
        意图。"""

        duty, parts = build_duty(
            identities=(identity(),),
            galaxy=galaxy_snapshot(roles=()),
            published_users={USER_ONE},
            metric_translation_map={},
        )

        report = duty.run_once()

        self.assertIsNone(report, "整轮判据不成立：撤权也不例外")
        self.assertEqual(
            parts["decisions"].calls, [], "翻译层不可用时，撤权意图同样不得排入 outbox"
        )
        self.assertEqual(
            parts["history"].calls, [], "遍历根本没开始，连『是否有发布足迹』都没查过"
        )

    def test_a_single_audit_entry_is_distinguishable_from_uncovered(self) -> None:
        """整轮跳过恰一条审计，动作名与逐用户「未覆盖」判据不同——运维一眼能分辨
        「翻译层这一轮整体没开」与「配了但某个组合漏填」。"""

        duty, parts = build_duty(identities=(identity(),), metric_translation_map={})

        duty.run_once()

        self.assertEqual(
            parts["audit"].actions(), ["permission_refresh.skipped_metric_translation_unavailable"]
        )
        fields = parts["audit"].fields_for(
            "permission_refresh.skipped_metric_translation_unavailable"
        )[0]
        self.assertEqual(fields["reason"], SKIP_METRIC_TRANSLATION_UNAVAILABLE)

    def test_the_gate_does_not_fire_when_the_map_has_any_content(self) -> None:
        """否定断言：映射**非空**（哪怕只覆盖了别的公司+职能）时，整轮判据不生效——
        遍历照常开始，逐用户判据接管；撤权路径完全不受影响（撤权从不查翻译映射）。"""

        duty, parts = build_duty(
            identities=(identity(),),
            galaxy=galaxy_snapshot(roles=()),
            published_users={USER_ONE},
            metric_translation_map={"9999": {"某个无关职能": ("某个无关指标",)}},
        )

        report = duty.run_once()

        self.assertIsNotNone(report, "映射非空时整轮判据不该拦住遍历")
        self.assertEqual(len(parts["decisions"].calls), 1, "撤权路径不查翻译映射，应照常发布")
        self.assertEqual(parts["decisions"].calls[0]["reason"], PERMISSION_REVOKE_REASON)


class MetricTranslationGateTest(unittest.TestCase):
    """「公司 + 职能 → 指标名」翻译层接线之后，逐用户判据的行为（Issue #227）。

    前提是映射**非空**（否则走 ``RoundLevelTranslationGateTest`` 的整轮判据，遍历
    根本不会开始）。钉的是：某个组合未覆盖时只跳过这一个授权用户（不发布、不撤权、
    不查令牌）；一个正面断言证明翻译成功时不同公司允许拿到不同的指标名列表——这是
    「公司 + 职能」联合键存在的意义，不是只按职能。
    """

    def test_a_non_empty_map_missing_this_combination_is_distinguishable_from_unavailable(
        self,
    ) -> None:
        """映射**不为空**（已经有别的公司+职能条目），但没覆盖到这次要用的组合——
        原因码必须是「未覆盖」，不是「未配置」，两种运维状态不能被合并成一种；且
        整轮判据不生效，`run_once` 返回真正的报告而不是 ``None``。"""

        duty, parts = build_duty(
            identities=(identity(),),
            metric_translation_map={"9999": {"某个无关职能": ("某个无关指标",)}},
        )

        report = duty.run_once()

        self.assertIsNotNone(report, "映射非空时整轮判据不生效")
        self.assertEqual(parts["decisions"].calls, [])
        self.assertEqual(report.reasons.get(SKIP_METRIC_TRANSLATION_UNCOVERED), 1)
        self.assertIsNone(report.reasons.get(SKIP_METRIC_TRANSLATION_UNAVAILABLE))

    def test_an_uncovered_grant_is_not_treated_as_a_revocation(self) -> None:
        """否定断言：一个**授权**用户（``granted=True``）翻译不出来时，不查"这个人
        有没有发布足迹"，更不产生撤权更新——它与 ``aggregate.granted=False`` 是两条
        不同的判定分支，从未进入 ``_revoke``。"""

        duty, parts = build_duty(
            identities=(identity(),),
            metric_translation_map={"9999": {"某个无关职能": ("某个无关指标",)}},
            published_users={USER_ONE},
        )

        duty.run_once()

        self.assertEqual(parts["history"].calls, [], "未覆盖组合不得查询撤权足迹")
        self.assertEqual(parts["decisions"].calls, [])

    def test_an_uncovered_user_is_not_charged_a_token_read(self) -> None:
        """翻译判定排在令牌读取之前：注定要跳过的人不该产生一次多余的令牌表查询。"""

        duty, parts = build_duty(
            identities=(identity(),),
            metric_translation_map={"9999": {"某个无关职能": ("某个无关指标",)}},
        )

        duty.run_once()

        self.assertEqual(parts["tokens"].read_calls, [])

    def test_partial_coverage_across_companies_fails_the_whole_user(self) -> None:
        """否定断言：一个人持有两家公司，映射只覆盖其中一家——**整体**翻译失败，
        不产出"只发布覆盖到的那一家"的部分结果（那是静默丢弃，不是失败关闭）。"""

        galaxy = galaxy_snapshot(
            countries=((GALAXY_ACCOUNT_ONE, "KE"), (GALAXY_ACCOUNT_ONE, "NG")),
            country_rows=(
                ("KE", "KENYA", "肯尼亚", COMPANY_ID),
                ("NG", "NIGERIA", "尼日利亚", COMPANY_ID_TWO),
            ),
        )
        duty, parts = build_duty(
            identities=(identity(),),
            galaxy=galaxy,
            metric_translation_map={COMPANY_ID: {FUNCTION_LABEL: (METRIC_NAME,)}},  # 缺 COMPANY_ID_TWO
        )

        duty.run_once()

        self.assertEqual(parts["decisions"].calls, [], "覆盖不全时一条部分发布都不得产出")

    def test_translation_can_differ_by_company_not_just_by_function(self) -> None:
        """正面断言：同一个用户、同一个职能，在两家公司下翻译出**不同**的指标名——
        证明翻译层的键是「公司 + 职能」，不是只看职能。"""

        galaxy = galaxy_snapshot(
            countries=((GALAXY_ACCOUNT_ONE, "KE"), (GALAXY_ACCOUNT_ONE, "NG")),
            country_rows=(
                ("KE", "KENYA", "肯尼亚", COMPANY_ID),
                ("NG", "NIGERIA", "尼日利亚", COMPANY_ID_TWO),
            ),
        )
        duty, parts = build_duty(identities=(identity(),), galaxy=galaxy)

        duty.run_once()

        row = parts["decisions"].calls[0]["row"]
        self.assertEqual(
            row.permissions,
            f'{{"{COMPANY_ID}":["{METRIC_NAME}"],"{COMPANY_ID_TWO}":["{METRIC_NAME_TWO}"]}}',
        )


# --------------------------------------------------------------------------
# 三点六、本地权限覆盖合并（S-P-3，Issue #319）
# --------------------------------------------------------------------------


def _override_entry(
    *,
    user_id: str = USER_ONE,
    direction: OverrideDirection = OverrideDirection.GRANT,
    company_id: str = COMPANY_ID,
    metric_name: str = "本地指标",
) -> LocalPermissionOverrideEntry:
    return LocalPermissionOverrideEntry(
        user_id=user_id,
        direction=direction,
        company_id=company_id,
        metric_name=metric_name,
        reason="U1 特批",
        initiated_by_open_id="ou_admin",
        pending_action_id="pac_fake",
        created_at=TODAY,
    )


class LocalOverrideMergeTest(unittest.TestCase):
    """授权侧翻译成功之后的四源合并接线：真实权限 = (银河 ∪ 本地授权 ∪ 存量沿用) −
    本地抑制，挂在结算发布行之前（``merge_sources.merge_permission_sources`` 模块
    文档「挂点」一节）。"""

    def test_store_absent_matches_todays_behavior(self) -> None:
        """装配层未接本地覆盖 store（``local_overrides=None``，默认值）：产出与
        接线之前逐字节一致——`GrantedUserTest.
        test_the_published_row_is_built_from_the_archived_identity` 的同一断言。"""

        duty, parts = build_duty(identities=(identity(),))

        duty.run_once()

        row = parts["decisions"].calls[0]["row"]
        self.assertEqual(row.permissions, f'{{"{COMPANY_ID}":["{METRIC_NAME}"]}}')

    def test_local_grant_is_unioned_into_the_published_metrics(self) -> None:
        """本地授权后聚合含并集（#319 验收断言）：银河翻译出的指标名与本地授权的
        指标名同时出现在同一个公司键下。"""

        overrides = FakeLocalOverrides({USER_ONE: (_override_entry(),)})
        duty, parts = build_duty(identities=(identity(),), local_overrides=overrides)

        duty.run_once()

        row = parts["decisions"].calls[0]["row"]
        self.assertEqual(row.permissions, f'{{"{COMPANY_ID}":["本地指标","{METRIC_NAME}"]}}')
        self.assertEqual(overrides.calls, [USER_ONE])

    def test_local_suppression_removes_a_galaxy_granted_metric(self) -> None:
        """抑制后不含：本地抑制命中的是**银河翻译产出**的指标名（不是本地授权自己
        加的那条），证明减法作用于并集之后的整体结果，不只是"本地授权 vs 本地抑制"
        内部打架那一层（那一层由 ``resolve_local_overrides`` 早就处理过）。第二家
        公司的指标不受影响，只有被抑制到空的那个公司键从结果里消失
        （`merge_sources` 模块文档「空结果」一节）。"""

        galaxy = galaxy_snapshot(
            countries=((GALAXY_ACCOUNT_ONE, "KE"), (GALAXY_ACCOUNT_ONE, "NG")),
            country_rows=(
                ("KE", "KENYA", "肯尼亚", COMPANY_ID),
                ("NG", "NIGERIA", "尼日利亚", COMPANY_ID_TWO),
            ),
        )
        overrides = FakeLocalOverrides(
            {USER_ONE: (_override_entry(direction=OverrideDirection.SUPPRESS, metric_name=METRIC_NAME),)}
        )
        duty, parts = build_duty(identities=(identity(),), galaxy=galaxy, local_overrides=overrides)

        duty.run_once()

        row = parts["decisions"].calls[0]["row"]
        self.assertEqual(row.permissions, f'{{"{COMPANY_ID_TWO}":["{METRIC_NAME_TWO}"]}}')

    def test_read_failure_skips_local_source_and_audits(self) -> None:
        """读取失败：该用户本轮跳过本地源并响亮审计，不整轮失败、不静默——发布仍然
        照常进行（只是不含本地覆盖），不计入 ``report.failed``。"""

        overrides = FakeLocalOverrides(fail_for={USER_ONE})
        duty, parts = build_duty(identities=(identity(),), local_overrides=overrides)

        report = duty.run_once()

        row = parts["decisions"].calls[0]["row"]
        self.assertEqual(row.permissions, f'{{"{COMPANY_ID}":["{METRIC_NAME}"]}}')
        self.assertEqual(report.failed, 0, "本地覆盖读取失败不得记成整用户失败")
        self.assertIn("permission_refresh.local_override_skipped", parts["audit"].actions())
        fields = parts["audit"].fields_for("permission_refresh.local_override_skipped")[0]
        self.assertEqual(fields["user"], USER_ONE)
        self.assertEqual(fields["reason"], REASON_LOCAL_OVERRIDE_READ_FAILED)

    def test_wildcard_admin_skips_both_grant_and_suppress_with_audit(self) -> None:
        """通配角 v1（编排者裁定）：``all_companies=True`` 时本地授权与抑制**整体
        不参与合并**，结果与银河原始的通配映射逐字节相同；两个理由各记一条
        ``permission_refresh.local_override_skipped`` 审计，动作名与原因码可分辨。"""

        overrides = FakeLocalOverrides(
            {
                USER_ONE: (
                    _override_entry(direction=OverrideDirection.GRANT, metric_name="额外授权"),
                    _override_entry(direction=OverrideDirection.SUPPRESS, metric_name=METRIC_NAME),
                )
            }
        )
        duty, parts = build_duty(
            identities=(identity(),),
            galaxy=galaxy_snapshot(roles=((GALAXY_ACCOUNT_ONE, ADMIN_FULL_ACCESS_FUNCTION),)),
            role_function_map={ADMIN_FULL_ACCESS_FUNCTION: ADMIN_FULL_ACCESS_FUNCTION},
            metric_translation_map={"*": {ADMIN_FULL_ACCESS_FUNCTION: (METRIC_NAME,)}},
            local_overrides=overrides,
        )

        duty.run_once()

        row = parts["decisions"].calls[0]["row"]
        self.assertEqual(row.permissions, f'{{"*":["{METRIC_NAME}"]}}', "通配下本地源整体不生效")
        skipped = parts["audit"].fields_for("permission_refresh.local_override_skipped")
        reasons = {fields["reason"] for fields in skipped}
        self.assertEqual(
            reasons, {REASON_GRANT_REDUNDANT_WILDCARD, REASON_SUPPRESS_INAPPLICABLE_WILDCARD}
        )


class LegacySourceMergeTest(unittest.TestCase):
    """存量权限沿用接线（S-P-2，Issue #319 / Trace #328）：与
    ``tests/test_permission_legacy_source.py`` 的纯函数用例同一组语义，这里钉的是
    ``PermissionRefreshDuty`` 真的把它接进四源合并、真的按失败语义降级审计。"""

    def test_table_absent_matches_todays_behavior(self) -> None:
        """装配层未接线（``legacy_source=None``，默认值）：产出与接线之前逐字节
        一致——`GrantedUserTest.test_the_published_row_is_built_from_the_archived_identity`
        的同一断言。"""

        duty, parts = build_duty(identities=(identity(),))

        duty.run_once()

        row = parts["decisions"].calls[0]["row"]
        self.assertEqual(row.permissions, f'{{"{COMPANY_ID}":["{METRIC_NAME}"]}}')

    def test_no_matching_legacy_row_is_also_identity(self) -> None:
        """接线了但这个用户没有存量行：与"没有接线"同一个结果——新用户的常见形态。"""

        legacy = FakeLegacyTable({})
        duty, parts = build_duty(identities=(identity(),), legacy_source=legacy)

        duty.run_once()

        row = parts["decisions"].calls[0]["row"]
        self.assertEqual(row.permissions, f'{{"{COMPANY_ID}":["{METRIC_NAME}"]}}')
        self.assertEqual(legacy.find_calls, [(EMAIL_ONE, EMAIL_ONE)])

    def test_legacy_permissions_are_unioned_into_the_published_metrics(self) -> None:
        """存量沿用后聚合含并集（本卡验收断言①）。**同时是红线-1 的正向不回归
        用例**（Trace #328 opus 审查）：这个用户从未发布过（默认夹具，
        ``published_users`` 为空），有界化判据必须放行——存量沿用照常生效。"""

        legacy = FakeLegacyTable({EMAIL_ONE: f'{{"{COMPANY_ID}":["legacy_metric"]}}'})
        duty, parts = build_duty(identities=(identity(),), legacy_source=legacy)

        duty.run_once()

        row = parts["decisions"].calls[0]["row"]
        self.assertEqual(row.permissions, f'{{"{COMPANY_ID}":["legacy_metric","{METRIC_NAME}"]}}')

    def test_a_user_with_a_publish_footprint_never_reads_legacy(self) -> None:
        """红线-1 否定用例（Trace #328 opus 审查）：这个用户已经在发布链上留下过
        足迹（``published_users`` 命中），legacy 有界化判据必须挡住存量沿用——
        **连 ``find_rows`` 都不发起**（省读放大，见 ``_resolve_legacy_source``
        文档）。存量行故意携带一个当前银河翻译结果里没有的指标，模拟"Lingxi 昨天
        发布过的内容被当作存量沿用原样读了回来"的自反馈环风险：今天的发布内容
        必须不含它，收窄才是真的生效，不是被自己的存量行悄悄抵消。"""

        legacy = FakeLegacyTable({EMAIL_ONE: f'{{"{COMPANY_ID}":["昨日已发布的指标"]}}'})
        duty, parts = build_duty(
            identities=(identity(),), published_users={USER_ONE}, legacy_source=legacy
        )

        duty.run_once()

        row = parts["decisions"].calls[0]["row"]
        self.assertEqual(
            row.permissions,
            f'{{"{COMPANY_ID}":["{METRIC_NAME}"]}}',
            "有发布足迹时存量沿用不生效，收窄才能真正生效，不被自己昨天的发布内容原样并回来",
        )
        self.assertEqual(legacy.find_calls, [], "有发布足迹时连 find_rows 都不该发起——省读放大")

    def test_read_failure_skips_legacy_source_and_audits(self) -> None:
        """读取失败：该用户本轮跳过存量源并响亮审计，不整轮失败——发布仍然照常
        进行（只是不含存量沿用），不计入 ``report.failed``（本卡验收断言②）。"""

        legacy = FakeLegacyTable(find_error=RuntimeError("注入的存量表读取失败"))
        duty, parts = build_duty(identities=(identity(),), legacy_source=legacy)

        report = duty.run_once()

        row = parts["decisions"].calls[0]["row"]
        self.assertEqual(row.permissions, f'{{"{COMPANY_ID}":["{METRIC_NAME}"]}}')
        self.assertEqual(report.failed, 0, "存量源读取失败不得记成整用户失败")
        self.assertIn("permission_refresh.legacy_source_skipped", parts["audit"].actions())
        fields = parts["audit"].fields_for("permission_refresh.legacy_source_skipped")[0]
        self.assertEqual(fields["user"], USER_ONE)
        self.assertEqual(fields["reason"], REASON_LEGACY_READ_FAILED)
        self.assertEqual(fields["error"], "RuntimeError")

    def test_conflicting_legacy_rows_are_skipped_with_a_distinct_reason(self) -> None:
        """命中多行：不知道该沿用哪一行，失败关闭并跳过——原因码与读取失败
        可分辨（本卡验收断言⑤）。"""

        legacy = FakeLegacyTable(
            {EMAIL_ONE: f'{{"{COMPANY_ID}":["legacy_metric"]}}'}, duplicate_for={EMAIL_ONE}
        )
        duty, parts = build_duty(identities=(identity(),), legacy_source=legacy)

        duty.run_once()

        row = parts["decisions"].calls[0]["row"]
        self.assertEqual(row.permissions, f'{{"{COMPANY_ID}":["{METRIC_NAME}"]}}', "冲突时存量源整体不生效")
        fields = parts["audit"].fields_for("permission_refresh.legacy_source_skipped")[0]
        self.assertEqual(fields["reason"], "multiple_rows")
        self.assertNotIn("error", fields, "多行冲突不源自某个被捕获的异常，没有可报告的错误类名")

    def test_wildcard_admin_skips_legacy_too(self) -> None:
        """通配角 v1：``all_companies=True`` 时存量沿用同样整体不参与合并（本卡验收
        断言④），结果与银河原始的通配映射逐字节相同——即使这个用户确实有一行存量
        权限。**读放大修复**（Issue #319 P2，Trace #328 opus 审查）：合并前就按
        ``all_companies`` 跳过，连 ``find_rows`` 都不发起，不是"读了、合并时才
        丢弃"。"""

        legacy = FakeLegacyTable({EMAIL_ONE: '{"1099":["额外存量指标"]}'})
        duty, parts = build_duty(
            identities=(identity(),),
            galaxy=galaxy_snapshot(roles=((GALAXY_ACCOUNT_ONE, ADMIN_FULL_ACCESS_FUNCTION),)),
            role_function_map={ADMIN_FULL_ACCESS_FUNCTION: ADMIN_FULL_ACCESS_FUNCTION},
            metric_translation_map={"*": {ADMIN_FULL_ACCESS_FUNCTION: (METRIC_NAME,)}},
            legacy_source=legacy,
        )

        duty.run_once()

        row = parts["decisions"].calls[0]["row"]
        self.assertEqual(row.permissions, f'{{"*":["{METRIC_NAME}"]}}', "通配下存量源整体不生效")
        self.assertEqual(legacy.find_calls, [], "通配用户合并前就该跳过，不发起 find_rows")


# --------------------------------------------------------------------------
# 四、撤权与输入不完整：全部失败关闭
# --------------------------------------------------------------------------


class RevocationTest(unittest.TestCase):
    """`V-权限-08` 刷新侧：**从来没发布过的人零发布意图**，审计带可分辨原因。

    产品负责人 2026-08-18 裁定 3 之后，"聚合无权限"不再等于"什么都不做"——但只对
    **我们真的发布成功过**的用户发撤权更新。本类钉的是"不发"那一半（默认夹具里谁都
    没发布过），"发"那一半在 :class:`RevocationPublishTest`。
    """

    def _run(self, **kwargs):
        duty, parts = build_duty(identities=(identity(),), **kwargs)
        report = duty.run_once()
        return report, parts

    def test_a_user_who_matches_no_galaxy_account_is_skipped(self) -> None:
        report, parts = self._run(
            galaxy=galaxy_snapshot(accounts=(("g-9999", "E9999", "other@example.invalid"),))
        )

        self.assertEqual(parts["decisions"].calls, [], "撤权用户零发布意图")
        self.assertEqual(report.revoked, 1)
        self.assertEqual(report.reasons["galaxy_account_not_found"], 1)
        skipped = parts["audit"].fields_for("permission_refresh.user_skipped")
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["stage"], "match")
        self.assertEqual(skipped[0]["reason"], "galaxy_account_not_found")

    def test_a_user_missing_from_the_roster_snapshot_is_skipped(self) -> None:
        report, parts = self._run(roster_rows=())

        self.assertEqual(parts["decisions"].calls, [])
        self.assertEqual(report.reasons["roster_not_found"], 1)

    def test_a_user_whose_roles_are_all_unmapped_is_skipped(self) -> None:
        report, parts = self._run(
            galaxy=galaxy_snapshot(roles=((GALAXY_ACCOUNT_ONE, "某个没配过的角色"),))
        )

        self.assertEqual(parts["decisions"].calls, [])
        self.assertEqual(report.revoked, 1)
        self.assertEqual(report.reasons["no_supported_function"], 1)
        self.assertEqual(
            parts["audit"].fields_for("permission_refresh.user_skipped")[0]["stage"], "aggregate"
        )

    def test_a_user_without_any_role_is_skipped(self) -> None:
        report, parts = self._run(galaxy=galaxy_snapshot(roles=()))

        self.assertEqual(parts["decisions"].calls, [])
        self.assertEqual(report.reasons["no_galaxy_roles"], 1)

    def test_a_user_without_any_resolvable_company_is_skipped(self) -> None:
        report, parts = self._run(
            galaxy=galaxy_snapshot(country_rows=(("KE", "KENYA", "肯尼亚", ""),))
        )

        self.assertEqual(parts["decisions"].calls, [])
        self.assertEqual(report.reasons["no_company_scope"], 1)

    def test_revocation_does_not_notify_anybody(self) -> None:
        """否定断言：撤权不通知。

        形状断言——职责的构造函数里根本没有任何发送端口，因此"顺手通知一声"在装配上
        就写不出来；源码里也不出现任何发送口。
        """

        source = duty_code()
        for forbidden in ("send_text", "sender", "notify", "chat_id"):
            self.assertNotIn(forbidden, source, f"重算职责不得持有通知出口：{forbidden}")

    def test_revocation_does_not_delete_or_disable_any_published_row(self) -> None:
        """否定断言：撤权**不删行、不停用、不自己动 status**。

        裁定 3 允许的唯一动作是"把 permissions 清空"，而它整个发生在
        ``build_revocation_row`` 里；本职责源码里不出现任何删除 / 停用 / 直接改状态
        的动作，也不自己调发布执行器（发布是另一个职责的事）。
        """

        source = duty_code()
        for forbidden in ("delete", "revoke_row", "disable", "STATUS_", "publish_claim"):
            self.assertNotIn(forbidden, source, f"撤权侧不得有外部写动作：{forbidden}")

    def test_a_revoked_user_without_any_published_row_is_skipped(self) -> None:
        """否定断言：**从无发布行的撤权用户零外部调用、零发布意图。**

        为他新建一行空权限没有意义（MCP 对查无此人默认拒绝），而新建需要令牌密文。
        """

        report, parts = self._run(galaxy=galaxy_snapshot(roles=()))

        self.assertEqual(parts["decisions"].calls, [], "无发布行的撤权用户零发布意图")
        self.assertEqual(parts["tokens"].read_calls, [], "撤权路径一次令牌都不读")
        self.assertEqual(report.revoked, 1)
        self.assertEqual(report.revoked_published, 0)
        self.assertEqual(report.reasons["no_published_row"], 1)
        skipped = parts["audit"].fields_for("permission_refresh.user_skipped")
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["revocation"], "no_published_row")

    def test_a_match_failure_never_triggers_a_revocation_publish(self) -> None:
        """**匹配失败不算撤权信号**，哪怕这个人已经有发布行。

        "认不出这个人是谁"不是"银河说他没有权限"；据一次花名册歧义清空权限，与花名册
        侧「仅提示、不做任何自动处置」的既定口径正相反。
        """

        for label, kwargs in (
            ("花名册查无", {"roster_rows": ()}),
            ("银河账号查无", {"galaxy": galaxy_snapshot(accounts=(("g-9", "E9", "o@x.invalid"),))}),
        ):
            with self.subTest(label):
                report, parts = self._run(published_users={USER_ONE}, **kwargs)

                self.assertEqual(parts["decisions"].calls, [], "匹配失败零发布意图")
                self.assertEqual(parts["history"].calls, [], "匹配失败连发布历史都不查")
                self.assertEqual(report.revoked_published, 0)


class RevocationPublishTest(unittest.TestCase):
    """`V-权限-08` 刷新侧的「发」那一半：保行、清空 permissions、不碰密文。"""

    def _run(self, **kwargs):
        duty, parts = build_duty(
            identities=(identity(),), published_users={USER_ONE}, **kwargs
        )
        return duty.run_once(), parts

    def test_a_revoked_user_with_a_published_row_gets_an_empty_permissions_update(self) -> None:
        report, parts = self._run(galaxy=galaxy_snapshot(roles=()))

        self.assertEqual(len(parts["decisions"].calls), 1)
        call = parts["decisions"].calls[0]
        self.assertEqual(call["reason"], PERMISSION_REVOKE_REASON)
        row = call["row"]
        self.assertEqual(row.permissions, "{}", "撤权行的 permissions 是空对象")
        self.assertEqual(row.status, "approved", "status 维持消费方唯一认可的取值")
        self.assertEqual(row.record_key, EMAIL_ONE.strip().lower())
        self.assertEqual(row.updated_at, "2026-08-17T03:00:00Z", "取权限决定的时刻")
        self.assertEqual(report.revoked, 1)
        self.assertEqual(report.revoked_published, 1)
        self.assertEqual(report.enqueued, 1)

    def test_the_revocation_row_never_carries_a_token_cipher(self) -> None:
        """否定断言：**撤权不清空、不覆盖、也不携带 token_cipher。**

        快照只有六个字段，因此发布层只能走更新路径；而更新是部分更新，那一列保持原值。
        """

        _report, parts = self._run(galaxy=galaxy_snapshot(roles=()))

        row = parts["decisions"].calls[0]["row"]
        self.assertIsNone(row.token_cipher)
        self.assertNotIn("token_cipher", row.snapshot_fields)
        self.assertEqual(set(row.snapshot_fields), set(row.fields))
        self.assertEqual(parts["tokens"].read_calls, [], "撤权路径一次令牌都不读")

    def test_a_revocation_row_cannot_create_a_new_row(self) -> None:
        """否定断言：**撤权行在发布层根本建不出新行**（缺密文即失败关闭）。"""

        _report, parts = self._run(galaxy=galaxy_snapshot(roles=()))

        row = parts["decisions"].calls[0]["row"]
        with self.assertRaises(ValueError):
            row.create_fields

    def test_repeating_the_revocation_is_counted_as_unchanged(self) -> None:
        """幂等由 record_decision 的内容比对承担：撤权不会每天重发一次。"""

        report, parts = self._run(
            galaxy=galaxy_snapshot(roles=()), decisions=FakeDecisions(unchanged_users={USER_ONE})
        )

        self.assertEqual(report.enqueued, 0)
        self.assertEqual(report.unchanged, 1)
        self.assertEqual(report.revoked_published, 0, "UNCHANGED 不算一次新的撤权发布")
        self.assertEqual(
            parts["audit"].fields_for("permission_refresh.user_revoked")[0]["enqueued"], False
        )

    def test_a_revoked_user_with_an_incomplete_archive_is_skipped(self) -> None:
        """存档不全的撤权用户跳过：撤权行的三列同样来自存档身份。

        **姓名与邮箱各测一次**：只测其中一个的话，把守卫写成单条件也能通过，而
        ``build_revocation_row`` 对两者都会抛错——那时它会被算成一次技术故障。
        """

        for label, kwargs in (("缺邮箱", {"email": ""}), ("缺姓名", {"display_name": ""})):
            with self.subTest(label):
                duty, parts = build_duty(
                    identities=(identity(**kwargs),),
                    published_users={USER_ONE},
                    galaxy=galaxy_snapshot(roles=()),
                )

                report = duty.run_once()

                self.assertEqual(parts["decisions"].calls, [])
                self.assertEqual(parts["history"].calls, [], "存档不全时连发布足迹都不查")
                self.assertEqual(report.reasons["archived_identity_incomplete"], 1)
                self.assertEqual(report.failed, 0, "这是分类，不是技术故障")

    def test_an_in_flight_intent_also_counts_as_a_footprint(self) -> None:
        """N7：**在途的旧授权意图**同样算足迹——撤权决定必须把它 supersede 掉。

        不算的话，那条已经被收回的范围会在发布面消费积压时被写进外部表，还会触发一条
        "范围已更新"通知（最长多给一天权限）。
        """

        duty, parts = build_duty(
            identities=(identity(),),
            published_users={USER_ONE},
            galaxy=galaxy_snapshot(roles=()),
        )

        report = duty.run_once()

        self.assertEqual(len(parts["decisions"].calls), 1)
        self.assertEqual(report.revoked_published, 1)

    def test_the_revocation_audit_carries_no_sensitive_value(self) -> None:
        _report, parts = self._run(galaxy=galaxy_snapshot(roles=()))

        fields = parts["audit"].fields_for("permission_refresh.user_revoked")[0]
        rendered = " ".join(f"{key}={value}" for key, value in fields.items())
        for secret in (EMAIL_ONE, NAME_ONE, EMPLOYEE_ONE, COMPANY_ID, TOKEN_CIPHER):
            self.assertNotIn(secret, rendered)


# --------------------------------------------------------------------------
# 三点七、全抑制走撤权出口（红线-2，Trace #328 opus 审查）
# --------------------------------------------------------------------------


class FullSuppressionRevocationTest(unittest.TestCase):
    """银河这一侧原本是有效授权，但本地抑制把翻译出的全部权限压光到空字典：
    不再落 ``build_translated_publish_row`` 对空输入的 ``ValueError``（冒泡成一条
    不可分辨的 ``permission_refresh.user_failed``），而是复用 ``_revoke`` 同一套
    机制（保行清空、只对发布链上留过足迹的人发），带一个可分辨的原因码
    :data:`REASON_FULLY_SUPPRESSED`。"""

    def _suppressed_overrides(self) -> FakeLocalOverrides:
        # 默认夹具的 identity 只持有一个公司（COMPANY_ID）一个指标（METRIC_NAME）——
        # 抑制它就是把合并结果压到空字典，不是"这个公司没了、别的公司还在"。
        return FakeLocalOverrides(
            {USER_ONE: (_override_entry(direction=OverrideDirection.SUPPRESS, metric_name=METRIC_NAME),)}
        )

    def test_a_fully_suppressed_grant_with_a_publish_footprint_gets_an_empty_revocation(self) -> None:
        """否定用例（Trace #328 opus 审查）：抑掉用户全部指标 → 发布 ``{}`` 撤权行 +
        专属审计，不是 ``ValueError`` 冒泡成通用失败。"""

        duty, parts = build_duty(
            identities=(identity(),),
            published_users={USER_ONE},
            local_overrides=self._suppressed_overrides(),
        )

        report = duty.run_once()

        self.assertEqual(report.failed, 0, "全抑制不是技术故障")
        self.assertEqual(len(parts["decisions"].calls), 1)
        call = parts["decisions"].calls[0]
        self.assertEqual(call["reason"], PERMISSION_REVOKE_REASON)
        row = call["row"]
        self.assertEqual(row.permissions, "{}", "全抑制走撤权出口：保行清空")
        revoked = parts["audit"].fields_for("permission_refresh.user_revoked")
        self.assertEqual(len(revoked), 1)
        self.assertEqual(
            revoked[0]["reason"], REASON_FULLY_SUPPRESSED,
            "原因码必须可分辨——不是银河本来就没给他权限，是本地抑制清空的",
        )
        cleared = parts["audit"].fields_for("permission_refresh.delivered_content_cleared")
        self.assertEqual(len(cleared), 1)
        self.assertEqual(cleared[0]["trigger"], TRIGGER_REVOKE)

    def test_a_fully_suppressed_grant_without_any_publish_footprint_is_skipped(self) -> None:
        """从未发布过的用户被全抑制：与银河侧撤权同一条边界——不为他新建一行空
        权限，只跳过并计数，不产生任何发布意图。"""

        duty, parts = build_duty(
            identities=(identity(),), local_overrides=self._suppressed_overrides()
        )

        report = duty.run_once()

        self.assertEqual(parts["decisions"].calls, [], "从未发布过的人不新建空权限行")
        self.assertEqual(report.revoked, 1)
        self.assertEqual(report.reasons[REASON_FULLY_SUPPRESSED], 1)
        skipped = parts["audit"].fields_for("permission_refresh.user_skipped")
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["reason"], REASON_FULLY_SUPPRESSED, "原因码仍是可分辨的全抑制")
        self.assertEqual(skipped[0]["revocation"], SKIP_NO_PUBLISHED_ROW)

    def test_a_partially_suppressed_grant_still_publishes_normally(self) -> None:
        """否定断言：只抑掉**一部分**指标（不是全部）时走的仍是正常授权发布，不是
        撤权出口——证明红线-2 的判据是"合并结果整体为空"，不是"发生过任何抑制"。"""

        overrides = FakeLocalOverrides(
            {
                USER_ONE: (
                    _override_entry(direction=OverrideDirection.GRANT, metric_name="额外授权"),
                    _override_entry(direction=OverrideDirection.SUPPRESS, metric_name=METRIC_NAME),
                )
            }
        )
        duty, parts = build_duty(identities=(identity(),), local_overrides=overrides)

        report = duty.run_once()

        self.assertEqual(report.enqueued, 1)
        self.assertEqual(len(parts["decisions"].calls), 1)
        call = parts["decisions"].calls[0]
        self.assertEqual(call["reason"], PERMISSION_REFRESH_REASON)
        self.assertEqual(call["row"].permissions, f'{{"{COMPANY_ID}":["额外授权"]}}')


# --------------------------------------------------------------------------
# 三点八、零银河权限用户的本地授权兜底（PM 2026-08-29 裁定，Issue #419）
# --------------------------------------------------------------------------


class ZeroGalaxyLocalGrantTest(unittest.TestCase):
    """`V-权限-15` 已消除的已知限制：`aggregate.granted` 为假不再无条件撤权，先看
    **本地授权**（不含存量沿用，P0-1 独立审查坐实并修复）能否兜底出可发布内容
    ——``_refresh_zero_galaxy_user`` 的否定/正向断言。"""

    def test_zero_galaxy_user_with_a_local_grant_publishes_exactly_the_local_set(
        self,
    ) -> None:
        """正向：零银河权限 + 本地授权（未被同键抑制）→ 发布内容精确等于本地授权
        集合——银河这一侧贡献为空，不出现任何翻译产物（``METRIC_NAME``）。"""

        overrides = FakeLocalOverrides({USER_ONE: (_override_entry(),)})
        duty, parts = build_duty(
            identities=(identity(),),
            galaxy=galaxy_snapshot(roles=()),
            local_overrides=overrides,
        )

        report = duty.run_once()

        self.assertEqual(len(parts["decisions"].calls), 1)
        call = parts["decisions"].calls[0]
        self.assertEqual(call["reason"], PERMISSION_REFRESH_REASON)
        self.assertEqual(
            call["row"].permissions,
            f'{{"{COMPANY_ID}":["本地指标"]}}',
            "精确等于本地授权集合，不含任何银河翻译产物",
        )
        self.assertEqual(report.enqueued, 1)
        self.assertEqual(report.revoked, 0, "走的是发布分支，不计入撤权计数")

    def test_a_same_key_suppressed_grant_still_revokes(self) -> None:
        """否定：本地授权被**同一个键**（同公司同指标）的本地抑制清空后，合并
        结果仍是空字典——维持现行撤权语义不变，`_revoke` 一个字节都不改（走的是
        `aggregate.reason`，不是红线-2 专用的 ``REASON_FULLY_SUPPRESSED``——两者
        原因不同：银河本来就没给，不是本地行政性收回）。"""

        overrides = FakeLocalOverrides(
            {
                USER_ONE: (
                    _override_entry(direction=OverrideDirection.GRANT, metric_name="本地指标"),
                    _override_entry(direction=OverrideDirection.SUPPRESS, metric_name="本地指标"),
                )
            }
        )
        duty, parts = build_duty(
            identities=(identity(),),
            galaxy=galaxy_snapshot(roles=()),
            published_users={USER_ONE},
            local_overrides=overrides,
        )

        report = duty.run_once()

        self.assertEqual(len(parts["decisions"].calls), 1)
        call = parts["decisions"].calls[0]
        self.assertEqual(call["reason"], PERMISSION_REVOKE_REASON)
        self.assertEqual(call["row"].permissions, "{}", "同键抑制清空后仍是撤权行")
        revoked = parts["audit"].fields_for("permission_refresh.user_revoked")
        self.assertEqual(len(revoked), 1)
        self.assertEqual(
            revoked[0]["reason"],
            "no_galaxy_roles",
            "原因是银河本来就没给（aggregate.reason），不是红线-2 的本地行政性收回",
        )

    def test_a_never_published_user_with_no_local_grant_still_skips(self) -> None:
        """否定：既无银河也无本地授权（默认 ``local_overrides=None``，装配层未
        接线）→ 与改动前逐字节一致——从无发布行的撤权用户零发布意图。"""

        duty, parts = build_duty(identities=(identity(),), galaxy=galaxy_snapshot(roles=()))

        report = duty.run_once()

        self.assertEqual(parts["decisions"].calls, [], "既无银河也无本地授权，零发布意图")
        self.assertEqual(report.revoked, 1)
        self.assertEqual(report.reasons["no_galaxy_roles"], 1)

    def test_a_legacy_row_alone_does_not_authorize_a_never_granted_user(self) -> None:
        """否定（P0-1，独立审查坐实并修复）：零银河 + 零本地授权 + 旧系统表遗留行
        → 存量沿用不构成独立授权来源，维持撤权语义。修复前
        ``_refresh_zero_galaxy_user`` 会把 ``_resolve_legacy_source`` 的结果也并进
        判据，让这个人被误判成有效授权、真的排出发布意图；修复后这一步固定传
        ``legacy=None``，连 ``find_rows`` 都不发起。"""

        legacy = FakeLegacyTable({EMAIL_ONE: f'{{"{COMPANY_ID}":["旧系统遗留指标"]}}'})
        duty, parts = build_duty(
            identities=(identity(),), galaxy=galaxy_snapshot(roles=()), legacy_source=legacy
        )

        report = duty.run_once()

        self.assertEqual(parts["decisions"].calls, [], "存量沿用不得单独兜底出发布内容")
        self.assertEqual(report.revoked, 1)
        self.assertEqual(report.reasons["no_galaxy_roles"], 1)
        self.assertEqual(legacy.find_calls, [], "零银河兜底判据不得读取存量沿用")

    def test_a_local_override_read_failure_skips_the_user_without_revoking(self) -> None:
        """否定（P1-1，独立审查坐实并修复）：零银河分支里本地覆盖读取失败 ≠
        「没有本地授权」——修复前两者都落到 ``None``，会让这个人被真的撤权、
        同事务清空已送达正文；修复后读取失败本轮直接跳过这个人：零发布意图、
        零撤权、只留 ``_resolve_local_overrides`` 已经记过的那一条
        ``local_override_skipped`` 审计，不额外多记。"""

        overrides = FakeLocalOverrides(fail_for={USER_ONE})
        duty, parts = build_duty(
            identities=(identity(),), galaxy=galaxy_snapshot(roles=()), local_overrides=overrides
        )

        report = duty.run_once()

        self.assertEqual(parts["decisions"].calls, [], "读取失败时零发布行为变化")
        self.assertEqual(report.revoked, 0, "读取失败不得被误判成撤权")
        self.assertEqual(report.enqueued, 0)
        skipped = parts["audit"].fields_for("permission_refresh.local_override_skipped")
        self.assertEqual(len(skipped), 1, "只留一条审计，不重复记")
        self.assertEqual(skipped[0]["reason"], REASON_LOCAL_OVERRIDE_READ_FAILED)

    def test_galaxy_recovering_restores_the_union_for_the_same_local_grant(self) -> None:
        """正向：同一份本地授权配置，银河一侧从零恢复为有效授权后，发布内容从
        「精确本地集合」变回「银河翻译结果 ∪ 本地授权」——两个分支共用同一个
        ``merge_permission_sources``，不是两套互相独立的合并逻辑。"""

        overrides = FakeLocalOverrides({USER_ONE: (_override_entry(),)})

        zero_duty, zero_parts = build_duty(
            identities=(identity(),),
            galaxy=galaxy_snapshot(roles=()),
            local_overrides=overrides,
        )
        zero_duty.run_once()
        self.assertEqual(
            zero_parts["decisions"].calls[0]["row"].permissions,
            f'{{"{COMPANY_ID}":["本地指标"]}}',
        )

        recovered_duty, recovered_parts = build_duty(
            identities=(identity(),), local_overrides=overrides
        )
        recovered_duty.run_once()
        self.assertEqual(
            recovered_parts["decisions"].calls[0]["row"].permissions,
            f'{{"{COMPANY_ID}":["本地指标","{METRIC_NAME}"]}}',
            "银河恢复后并集恢复",
        )


# --------------------------------------------------------------------------
# 五、已送达正文同步清（S-P-5，Trace #328）
# --------------------------------------------------------------------------


class DeliveredContentPurgeTest(unittest.TestCase):
    """授权、撤权两条路径都必须把 ``clear_delivered_content=True`` 传给
    ``record_decision``；真正"要不要清、清没清、清了多少条"由 ``record_decision``
    自己的同一个事务决定（真库证据在 ``tests/test_permission_publish_postgres.py``），
    本职责这里只钉两件纯逻辑面的事：**接入面**（开关确实传了）与**审计面**（清理
    发生时记一条不含正文的计数审计，``UNCHANGED`` 时不记）。
    """

    def test_a_grant_passes_the_clear_switch(self) -> None:
        duty, parts = build_duty(identities=(identity(),))

        duty.run_once()

        self.assertEqual(len(parts["decisions"].calls), 1)
        self.assertTrue(parts["decisions"].calls[0]["clear_delivered_content"])

    def test_a_revocation_passes_the_clear_switch(self) -> None:
        duty, parts = build_duty(
            identities=(identity(),), published_users={USER_ONE}, galaxy=galaxy_snapshot(roles=())
        )

        duty.run_once()

        self.assertEqual(len(parts["decisions"].calls), 1)
        self.assertTrue(parts["decisions"].calls[0]["clear_delivered_content"])

    def test_a_granted_change_that_actually_clears_something_is_audited_with_the_count(self) -> None:
        decisions = FakeDecisions(cleared_events_by_user={USER_ONE: 3})
        duty, parts = build_duty(identities=(identity(),), decisions=decisions)

        duty.run_once()

        cleared = parts["audit"].fields_for("permission_refresh.delivered_content_cleared")
        self.assertEqual(len(cleared), 1)
        self.assertEqual(cleared[0], {"user": USER_ONE, "cleared": 3, "trigger": "grant"})

    def test_a_grant_that_clears_nothing_is_still_audited_with_a_zero_count(self) -> None:
        """一个刚被授权、还没有任何历史会话的人：清理**发生过**（record_decision 真的
        跑了这一步），只是清出 0 条——审计如实记 0，不因为"没清到东西"就整条略过。"""

        duty, parts = build_duty(identities=(identity(),))

        duty.run_once()

        cleared = parts["audit"].fields_for("permission_refresh.delivered_content_cleared")
        self.assertEqual(cleared, [{"user": USER_ONE, "cleared": 0, "trigger": "grant"}])

    def test_an_unchanged_grant_is_not_audited_as_a_clear(self) -> None:
        """否定断言：``UNCHANGED`` 时清理根本没有发生（``record_decision`` 内部只在
        真的推进版本时才碰 ``clear_delivered_content_for_user``），因此不记这条审计
        ——即使调用方一样传了 ``clear_delivered_content=True``。"""

        decisions = FakeDecisions(unchanged_users={USER_ONE}, cleared_events_by_user={USER_ONE: 5})
        duty, parts = build_duty(identities=(identity(),), decisions=decisions)

        report = duty.run_once()

        self.assertEqual(report.unchanged, 1)
        self.assertEqual(parts["audit"].fields_for("permission_refresh.delivered_content_cleared"), [])

    def test_a_revocation_that_actually_clears_something_is_audited_with_the_count(self) -> None:
        decisions = FakeDecisions(cleared_events_by_user={USER_ONE: 2})
        duty, parts = build_duty(
            identities=(identity(),),
            published_users={USER_ONE},
            galaxy=galaxy_snapshot(roles=()),
            decisions=decisions,
        )

        duty.run_once()

        cleared = parts["audit"].fields_for("permission_refresh.delivered_content_cleared")
        self.assertEqual(cleared, [{"user": USER_ONE, "cleared": 2, "trigger": "revoke"}])

    def test_an_unchanged_revocation_is_not_audited_as_a_clear(self) -> None:
        decisions = FakeDecisions(unchanged_users={USER_ONE}, cleared_events_by_user={USER_ONE: 5})
        duty, parts = build_duty(
            identities=(identity(),),
            published_users={USER_ONE},
            galaxy=galaxy_snapshot(roles=()),
            decisions=decisions,
        )

        report = duty.run_once()

        self.assertEqual(report.unchanged, 1)
        self.assertEqual(parts["audit"].fields_for("permission_refresh.delivered_content_cleared"), [])

    def test_a_skipped_revocation_without_any_published_row_never_reaches_record_decision(self) -> None:
        """从无发布行的撤权用户零发布意图（既有断言）：连 ``record_decision`` 都没被
        调用，自然也不会有任何清理或清理审计——不是"清了 0 条"，是"这一步压根没发生"。
        """

        duty, parts = build_duty(identities=(identity(),), galaxy=galaxy_snapshot(roles=()))

        duty.run_once()

        self.assertEqual(parts["decisions"].calls, [])
        self.assertEqual(parts["audit"].fields_for("permission_refresh.delivered_content_cleared"), [])


class IncompleteInputTest(unittest.TestCase):
    def test_an_identity_without_a_personnel_id_is_classified_not_crashed(self) -> None:
        duty, parts = build_duty(identities=(identity(personnel_id=""),))

        report = duty.run_once()

        self.assertEqual(report.incomplete, 1)
        self.assertEqual(report.failed, 0, "输入不完整是分类，不是技术故障")
        self.assertEqual(report.reasons[SKIP_MISSING_PERSONNEL_ID], 1)
        self.assertEqual(parts["decisions"].calls, [])

    def test_an_identity_without_an_email_is_classified_not_crashed(self) -> None:
        duty, parts = build_duty(identities=(identity(email=""),))

        report = duty.run_once()

        self.assertEqual(report.incomplete, 1)
        self.assertEqual(report.failed, 0)
        self.assertEqual(report.reasons[SKIP_ARCHIVED_IDENTITY_INCOMPLETE], 1)
        self.assertEqual(parts["decisions"].calls, [])

    def test_an_identity_without_a_display_name_is_classified_not_crashed(self) -> None:
        duty, parts = build_duty(identities=(identity(display_name=""),))

        report = duty.run_once()

        self.assertEqual(report.reasons[SKIP_ARCHIVED_IDENTITY_INCOMPLETE], 1)
        self.assertEqual(parts["decisions"].calls, [])


# --------------------------------------------------------------------------
# 五、单用户隔离
# --------------------------------------------------------------------------


class UserIsolationTest(unittest.TestCase):
    def test_one_failing_user_does_not_stop_the_round(self) -> None:
        decisions = FakeDecisions(failing_users={USER_ONE})
        duty, parts = build_duty(
            identities=(
                identity(),
                identity(
                    USER_TWO,
                    personnel_id="ou_person_0002",
                    display_name=NAME_TWO,
                    employee_no=EMPLOYEE_TWO,
                    email=EMAIL_TWO,
                ),
            ),
            roster_rows=(
                roster_row(),
                roster_row(
                    "ou_person_0002", employee_no=EMPLOYEE_TWO, email=EMAIL_TWO, name=NAME_TWO
                ),
            ),
            galaxy=galaxy_snapshot(
                accounts=(
                    (GALAXY_ACCOUNT_ONE, EMPLOYEE_ONE, EMAIL_ONE),
                    (GALAXY_ACCOUNT_TWO, EMPLOYEE_TWO, EMAIL_TWO),
                ),
                roles=((GALAXY_ACCOUNT_ONE, ROLE_NAME), (GALAXY_ACCOUNT_TWO, ROLE_NAME)),
                countries=((GALAXY_ACCOUNT_ONE, "KE"), (GALAXY_ACCOUNT_TWO, "KE")),
            ),
            decisions=decisions,
        )

        report = duty.run_once()

        self.assertEqual(report.failed, 1)
        self.assertEqual(report.enqueued, 1, "另一个用户照常处理")
        self.assertEqual([call["user_id"] for call in decisions.calls], [USER_TWO])
        failed = parts["audit"].fields_for("permission_refresh.user_failed")
        self.assertEqual(failed[0]["user"], USER_ONE)
        self.assertEqual(failed[0]["error"], "RuntimeError")
        self.assertNotIn("detail", failed[0], "异常正文不进审计")

    def test_a_failing_round_still_reports_and_marks_the_day_done(self) -> None:
        """有失败也置水位：失败逐条留痕、计入报告，等下一天那一轮。

        "有失败就整轮重来"会让一次持续的数据库故障变成每分钟重跑一遍全员。
        """

        decisions = FakeDecisions(failing_users={USER_ONE})
        duty, _ = build_duty(identities=(identity(),), decisions=decisions)

        report = duty.run_once()

        self.assertEqual(report.failed, 1)
        self.assertEqual(duty.completed_on, date(2026, 8, 17))


# --------------------------------------------------------------------------
# 六、水位与停止
# --------------------------------------------------------------------------


class WatermarkAndStopTest(unittest.TestCase):
    def test_the_second_round_of_the_same_day_does_nothing(self) -> None:
        duty, parts = build_duty(identities=(identity(),))

        first = duty.run_once()
        second = duty.run_once()

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(parts["decisions"].calls), 1)
        self.assertEqual(parts["roster"].facts_calls, 1, "今天已经做完，连判据都不再读")

    def test_a_new_day_runs_again(self) -> None:
        clock = FixedClock(TODAY)
        duty, parts = build_duty(identities=(identity(),), clock=clock)

        duty.run_once()
        clock.advance(timedelta(days=1))
        parts["roster"]._captured_at = TODAY + timedelta(days=1)
        duty.run_once()

        self.assertEqual(len(parts["decisions"].calls), 2)

    def test_a_stopping_duty_does_not_open_a_round(self) -> None:
        stop = threading.Event()
        stop.set()
        duty, parts = build_duty(identities=(identity(),), stop=stop)

        self.assertIsNone(duty.run_once())
        self.assertEqual(parts["roster"].facts_calls, 0)
        self.assertEqual(parts["decisions"].calls, [])

    def test_a_stop_signal_during_the_walk_interrupts_without_marking_the_day(self) -> None:
        stop = threading.Event()
        decisions = FakeDecisions()
        second = identity(
            USER_TWO,
            personnel_id="ou_person_0002",
            display_name=NAME_TWO,
            employee_no=EMPLOYEE_TWO,
            email=EMAIL_TWO,
        )
        duty, parts = build_duty(
            identities=(identity(), second),
            roster_rows=(
                roster_row(),
                roster_row(
                    "ou_person_0002", employee_no=EMPLOYEE_TWO, email=EMAIL_TWO, name=NAME_TWO
                ),
            ),
            galaxy=galaxy_snapshot(
                accounts=(
                    (GALAXY_ACCOUNT_ONE, EMPLOYEE_ONE, EMAIL_ONE),
                    (GALAXY_ACCOUNT_TWO, EMPLOYEE_TWO, EMAIL_TWO),
                ),
                roles=((GALAXY_ACCOUNT_ONE, ROLE_NAME), (GALAXY_ACCOUNT_TWO, ROLE_NAME)),
                countries=((GALAXY_ACCOUNT_ONE, "KE"), (GALAXY_ACCOUNT_TWO, "KE")),
            ),
            decisions=decisions,
            stop=stop,
        )
        original = decisions.record_decision

        def stopping_record(**kwargs):
            stop.set()
            return original(**kwargs)

        decisions.record_decision = stopping_record  # type: ignore[assignment]

        report = duty.run_once()

        self.assertTrue(report.interrupted)
        self.assertEqual(len(decisions.calls), 1, "停止后不再为后面的人排意图")
        self.assertEqual(
            report.examined, 1, "被停止挡在外面的人从来没有被看过一眼，不算已检查"
        )
        self.assertIsNone(duty.completed_on, "没走完的一轮不置水位")
        self.assertIn("permission_refresh.interrupted", parts["audit"].actions())
        self.assertNotIn("permission_refresh.completed", parts["audit"].actions())

    def test_the_duty_is_isolated_by_the_scheduler_loop(self) -> None:
        """整轮异常按 duty 惯例上抛，由 :class:`SchedulerLoop` 隔离。"""

        class Exploding:
            name = "炸弹"

            def run_once(self):
                raise RuntimeError("整轮失败")

        duty, parts = build_duty(identities=(identity(),))
        loop = SchedulerLoop(duties=(Exploding(), duty), interval_seconds=0.01)

        reports = loop.run_once()

        self.assertIsNone(reports[0])
        self.assertIsNotNone(reports[1])
        self.assertEqual(len(parts["decisions"].calls), 1)


# --------------------------------------------------------------------------
# 七、遍历口径：过滤写在 SQL 里
# --------------------------------------------------------------------------


class ActiveFilterTest(unittest.TestCase):
    def test_the_filter_lives_in_sql_and_is_shared_with_the_roster_audit(self) -> None:
        """否定断言的一半：本职责**不复制**筛选条件，直接复用同一条只读 SQL。

        另一半（非 active / deleting / deleted 用户绝不进入重算）只有真库能证伪，
        在 ``tests/test_permission_refresh_postgres.py``。
        """

        self.assertIn("provisioning_state = 'active'", ACTIVE_BASELINE_SQL)
        self.assertIn("account_state NOT IN ('deleting', 'deleted')", ACTIVE_BASELINE_SQL)
        source = duty_code()
        for forbidden in ("provisioning_state", "account_state", "SELECT"):
            self.assertNotIn(forbidden, source, f"筛选不得在职责里再写一份：{forbidden}")

    def test_every_row_the_reader_returns_is_examined(self) -> None:
        baseline = FakeBaseline(identity(), identity(USER_TWO, personnel_id="ou_person_0002"))
        duty, _ = build_duty(identities=())
        duty._baseline_reader = baseline  # type: ignore[attr-defined]

        report = duty.run_once()

        self.assertEqual(report.examined, 2, "读到几行就重算几行，职责里不再过滤")


# --------------------------------------------------------------------------
# 八、绝不签发令牌
# --------------------------------------------------------------------------


class NeverIssuesTokensTest(unittest.TestCase):
    def test_the_source_contains_no_issuing_call(self) -> None:
        source = duty_code()
        self.assertNotIn("issue_token", source)
        self.assertNotIn("new_token", source)

    def test_the_read_port_is_the_only_token_call(self) -> None:
        tokens = FakeTokens({USER_ONE: TOKEN_CIPHER})
        duty, _ = build_duty(identities=(identity(),), tokens=tokens)

        duty.run_once()

        self.assertEqual(tokens.read_calls, [USER_ONE])
        self.assertEqual(tokens.issue_calls, [])

    def test_no_token_is_read_for_a_user_without_permissions(self) -> None:
        """撤权用户连令牌都不查：那一步只服务于"要写出去的那一行"。"""

        tokens = FakeTokens({USER_ONE: TOKEN_CIPHER})
        duty, _ = build_duty(
            identities=(identity(),), tokens=tokens, galaxy=galaxy_snapshot(roles=())
        )

        duty.run_once()

        self.assertEqual(tokens.read_calls, [])


# --------------------------------------------------------------------------
# 九、审计与报告的形状：不含敏感原值
# --------------------------------------------------------------------------


SENSITIVE_VALUES = (
    NAME_ONE,
    NAME_TWO,
    EMAIL_ONE,
    EMAIL_TWO,
    EMPLOYEE_ONE,
    EMPLOYEE_TWO,
    GALAXY_ACCOUNT_ONE,
    GALAXY_ACCOUNT_TWO,
    COMPANY_ID,
    FUNCTION_LABEL,
    ROLE_NAME,
    TOKEN_CIPHER,
)


class AuditShapeTest(unittest.TestCase):
    def _round_with_every_class(self):
        """一轮里同时出现：有权限、撤权、输入不完整、技术失败四类用户。"""

        third = identity("usr_third", personnel_id="ou_person_0003")
        fourth = identity("usr_fourth", personnel_id="")
        decisions = FakeDecisions(failing_users={"usr_third"})
        duty, parts = build_duty(
            identities=(
                identity(),
                identity(
                    USER_TWO,
                    personnel_id="ou_person_0002",
                    display_name=NAME_TWO,
                    employee_no=EMPLOYEE_TWO,
                    email=EMAIL_TWO,
                ),
                third,
                fourth,
            ),
            roster_rows=(
                roster_row(),
                roster_row(
                    "ou_person_0002", employee_no=EMPLOYEE_TWO, email=EMAIL_TWO, name=NAME_TWO
                ),
                roster_row("ou_person_0003"),
            ),
            decisions=decisions,
        )
        return duty.run_once(), parts

    def test_no_audit_field_carries_a_sensitive_value(self) -> None:
        _, parts = self._round_with_every_class()

        rendered = " ".join(
            f"{action} " + " ".join(f"{key}={value}" for key, value in fields.items())
            for action, fields in parts["audit"].records
        )
        for value in SENSITIVE_VALUES:
            self.assertNotIn(value, rendered, f"审计不得出现敏感原值：{value}")

    def test_the_report_only_carries_counts_and_reason_codes(self) -> None:
        report, _ = self._round_with_every_class()

        facts = report.audit_facts()
        for key, value in facts.items():
            self.assertIsInstance(key, str)
            self.assertIsInstance(value, (int, bool), f"报告字段 {key} 必须是计数或标志")
        rendered = " ".join(f"{key}={value}" for key, value in facts.items())
        for value in SENSITIVE_VALUES:
            self.assertNotIn(value, rendered)

    def test_the_completed_audit_carries_the_batch_and_the_counts(self) -> None:
        duty, parts = build_duty(identities=(identity(),))

        duty.run_once()

        completed = parts["audit"].fields_for("permission_refresh.completed")
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["batch"], "gib_fake")
        self.assertEqual(completed[0]["examined"], 1)
        self.assertEqual(completed[0]["enqueued"], 1)
        self.assertEqual(completed[0]["report_date"], "2026-08-17")

    def test_an_empty_report_renders_without_reason_keys(self) -> None:
        self.assertEqual(
            PermissionRefreshReport().audit_facts(),
            {
                "examined": 0,
                "processed": 0,
                "enqueued": 0,
                "unchanged": 0,
                "revoked": 0,
                "revoked_published": 0,
                "incomplete": 0,
                "failed": 0,
            },
        )


# --------------------------------------------------------------------------
# 十、装配：条件注册与职责顺序
# --------------------------------------------------------------------------


COMPLETE_ENV = {
    "LINGXI_POSTGRES_DSN": "postgresql://user@localhost:5432/lingxi",
    "LINGXI_FEISHU_APP_ID": "cli_fake",
    "LINGXI_FEISHU_APP_SECRET": "secret_fake",
}


@unittest.skipUnless(
    importlib.util.find_spec("psycopg") and importlib.util.find_spec("cryptography"),
    "跳过：build_loop 会真的构造凭据保管与清理适配器，需要 psycopg 与 cryptography",
)
class DutyRegistrationTest(unittest.TestCase):
    """前置缺项 → 职责不注册、进程照常启动、审计恰 1 条、不回显值。"""

    def _config(self, **extra: str) -> SchedulerConfig:
        import tempfile

        from cryptography.fernet import Fernet

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return SchedulerConfig.from_env(
            {
                **COMPLETE_ENV,
                "LINGXI_DELEGATED_CREDENTIAL_KEY": Fernet.generate_key().decode(),
                "LINGXI_DELEGATED_CREDENTIAL_PATH": str(pathlib.Path(directory.name) / "delegated.enc"),
                **extra,
            }
        )

    def _own_records(self, audit: RecordingAudit):
        return [record for record in audit.records if record[0].startswith("permission_refresh.")]

    def test_without_the_master_key_the_duty_is_not_registered(self) -> None:
        audit = RecordingAudit()

        loop = build_loop(self._config(), audit=audit)

        self.assertNotIn("每日权限重算", [duty.name for duty in loop.duties])
        records = self._own_records(audit)
        self.assertEqual(len(records), 1, "前置缺项时本职责的审计恰 1 条")
        action, fields = records[0]
        self.assertEqual(action, "permission_refresh.duty_not_registered")
        self.assertEqual(fields["reason"], "missing_environment_variable")
        self.assertEqual(fields["variable"], "LINGXI_MCP_TOKEN_ENCRYPT_KEY")
        self.assertNotIn("value", fields, "审计里不得回显变量的值——它还是一把主密钥")

    def test_with_the_master_key_the_duty_is_registered_after_the_roster_audit(self) -> None:
        """顺序保证：两个职责都注册时，重算**排在花名册审计之后**。

        同一轮里花名册快照先被换成今天的那一份，重算才可能通过自己的新鲜度判据
        （`V-权限-07` 的「先花名册、再银河」）。位置反了这条用例立刻变红。
        """

        audit = RecordingAudit()

        loop = build_loop(
            self._config(
                LINGXI_MCP_TOKEN_ENCRYPT_KEY=SPEC_MASTER_KEY,
                LINGXI_ADMIN_GROUP_CHAT_ID="oc_fake_admin_group_for_tests",
                LINGXI_ROSTER_BITABLE_APP_TOKEN="bascnFakeAppToken",
                LINGXI_ROSTER_BITABLE_TABLE_ID="tblFakeTable",
            ),
            roster_access_token=lambda: "u-fake-token",
            audit=audit,
        )

        names = [duty.name for duty in loop.duties]
        self.assertIn("每日权限重算", names)
        self.assertIn("花名册审计日报", names)
        self.assertGreater(
            names.index("每日权限重算"),
            names.index("花名册审计日报"),
            "重算必须排在花名册审计之后",
        )
        # 发布消费职责随主密钥一起装配（它的通知面用同一把密钥），排在重算之后。
        self.assertIn("权限发布与就绪确认", names)
        self.assertGreater(
            names.index("权限发布与就绪确认"), names.index("每日权限重算")
        )
        # 组织快照同步（Issue #250）两个令牌供给默认总能建出来，因此总会真实注册，
        # 排在权限发布消费之后（本文件不是这条位置断言的权威归属，只需不被带崩）。
        self.assertIn("组织快照同步", names)
        # 迟到就绪恢复（V-开通-18）、开通中途停摆收口（Issue #282，V-开通-19）与
        # 文档投递死信扫描（Issue #341 R-2）都没有可选前置会让它们整体不装配，
        # 因此**总是**排在最后（本用例没有传 alerting_duty；本文件同样不是这条
        # 位置断言的权威归属，只需不被带崩）。
        self.assertEqual(names[-1], "文档投递死信扫描与正文到期擦除")
        self.assertEqual(self._own_records(audit), [], "前置齐备时不该有『未注册』审计")
        loop.request_stop()
        self.assertTrue(all(duty.stopping for duty in loop.duties))

    def test_a_malformed_master_key_fails_fast_at_configuration_time(self) -> None:
        """错配不是未配：静默降级会让运维以为权限每天在刷新。"""

        with self.assertRaises(ValueError):
            self._config(LINGXI_MCP_TOKEN_ENCRYPT_KEY="not-a-32-byte-key")

    def test_the_master_key_is_not_printed_by_the_configuration_object(self) -> None:
        config = self._config(LINGXI_MCP_TOKEN_ENCRYPT_KEY=SPEC_MASTER_KEY)

        self.assertNotIn(SPEC_MASTER_KEY, repr(config))

    def test_the_variable_is_registered_in_the_environment_key_list(self) -> None:
        self.assertIn("LINGXI_MCP_TOKEN_ENCRYPT_KEY", SchedulerConfig.ENVIRONMENT_KEYS)

    # ------------------------------------------------------------------
    # 外置指标映射路径（Issue #320）：存在则优先、未配置回落包内默认、
    # 配置了但缺失/非法响亮失败——装配层这一侧的证据。loader 本身的三条
    # 独立证据在 tests/test_company_function_metric_map_file.py 的
    # ExternalPathOverrideTest；这里只证明 SchedulerConfig 字段确实流到了
    # load_company_function_metric_map 的 path 参数，而不是被装配层悄悄忽略。
    # ------------------------------------------------------------------

    def test_the_external_metric_map_path_variable_is_registered(self) -> None:
        self.assertIn(
            "LINGXI_COMPANY_FUNCTION_METRIC_MAP_PATH", SchedulerConfig.ENVIRONMENT_KEYS
        )

    def test_an_invalid_external_metric_map_path_is_not_registered(self) -> None:
        """配了但指向的文件不存在：**必须**响亮失败——如果装配层没有真的把这个变量
        传给 loader（例如实现时手误漏接线），包内默认文件永远有效，这条用例就会
        观察不到 ``duty_not_registered``，从而直接暴露"变量声明了但没有被使用"这
        类接线遗漏。
        """

        import tempfile

        audit = RecordingAudit()
        with tempfile.TemporaryDirectory() as tmp:
            bogus_path = str(pathlib.Path(tmp) / "does-not-exist.toml")

            loop = build_loop(
                self._config(
                    LINGXI_MCP_TOKEN_ENCRYPT_KEY=SPEC_MASTER_KEY,
                    LINGXI_COMPANY_FUNCTION_METRIC_MAP_PATH=bogus_path,
                ),
                audit=audit,
            )

        self.assertNotIn("每日权限重算", [duty.name for duty in loop.duties])
        records = self._own_records(audit)
        self.assertEqual(len(records), 1, "前置缺项时本职责的审计恰 1 条")
        action, fields = records[0]
        self.assertEqual(action, "permission_refresh.duty_not_registered")
        self.assertEqual(fields["reason"], "metric_translation_map_unavailable")
        self.assertNotIn("path", fields, "审计里不回显外置路径的取值")

    def test_a_valid_external_metric_map_path_registers_the_duty(self) -> None:
        """配了且指向一份合法的外置文件：职责正常注册，不因为路径来自环境变量就
        被额外挡住——外置能力只改变"从哪里读"，不改变"读到合法内容就该正常工作"。
        """

        import tempfile

        audit = RecordingAudit()
        with tempfile.TemporaryDirectory() as tmp:
            external_path = pathlib.Path(tmp) / "override.toml"
            external_path.write_text(
                '[companies."9001"]\n"运营" = ["外置专用指标"]\n', encoding="utf-8"
            )

            loop = build_loop(
                self._config(
                    LINGXI_MCP_TOKEN_ENCRYPT_KEY=SPEC_MASTER_KEY,
                    LINGXI_COMPANY_FUNCTION_METRIC_MAP_PATH=str(external_path),
                ),
                audit=audit,
            )

        self.assertIn("每日权限重算", [duty.name for duty in loop.duties])
        self.assertEqual(self._own_records(audit), [], "前置齐备时不该有『未注册』审计")


if __name__ == "__main__":
    unittest.main()
