"""花名册持久快照的替换门槛与保旧告警（Issue #52 / S-B-02，无网络、无数据库）。

认领断言：V-花名册-41（替换门槛只认 `status`）、V-花名册-43（保旧告警四类分开）、
V-花名册-44（首轮无快照与保旧可区分）、V-花名册-46（快照层审计不含字段值）、
V-花名册-47 的**源码扫描面**（快照载体不得出现任何按时间删除快照的路径，落在
`SnapshotHasNoTimeBasedDeletionPathTest`）。另有二级审查 P2-A 的回读自洽性
（元信息与行对不上就响亮失败，不返回半态），落在 `SnapshotReadbackConsistencyTest`。

**用的是读取层真正的结果类型**（`RosterReadOutcome` 等）而不是自造的假对象：门槛
判定的全部价值在于它与读取层的四态语义对齐，拿假对象断言只能证明"我自己想的那套
是自洽的"。

真库那半边（原子替换、单例约束、CASCADE、行原样回读）在
`tests/test_roster_snapshot_postgres.py`，本文件不碰数据库。
"""

from __future__ import annotations

import ast
import re
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from lingxi.adapters.feishu_roster_bitable import (
    ColumnCount,
    DuplicateCount,
    RosterFailureKind,
    RosterIntegrity,
    RosterReadFailure,
    RosterReadOutcome,
    RosterReadStatus,
    RosterRow,
)
from lingxi.core.identity.roster_snapshot import (
    RosterSnapshotUpdater,
    SnapshotAction,
    SnapshotAlertKind,
    StoredSnapshotFacts,
    decide_snapshot_update,
)

# 固定化名与假标识（验证与门禁十三）。它们同时是"不得出现在审计事实里"的探针值。
FAKE_NAME = "化名甲"
FAKE_EMAIL = "jiaming.jia@example.invalid"
FAKE_EMPLOYEE_NO = "700123"
FAKE_PERSONNEL_ID = "fs-u-0001"
FAKE_RECORD_ID = "rec-0001"

NOW = datetime(2026, 8, 17, 3, 0, tzinfo=UTC)
YESTERDAY = NOW - timedelta(days=1)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"


def _row(personnel_id: str = FAKE_PERSONNEL_ID, *, record_id: str = FAKE_RECORD_ID) -> RosterRow:
    return RosterRow(
        personnel_id=personnel_id,
        email=FAKE_EMAIL,
        name=FAKE_NAME,
        employee_no=FAKE_EMPLOYEE_NO,
        record_id=record_id,
    )


def _integrity(*, row_count: int = 1, **overrides: Any) -> RosterIntegrity:
    defaults: dict[str, Any] = {
        "row_count": row_count,
        "pages_read": 1,
        "reported_total": row_count,
        "total_matches_rows": True,
        "blank_column_rows": (ColumnCount("工号", 0),),
        "duplicates": (DuplicateCount("personnel_id", 0, 0),),
    }
    defaults.update(overrides)
    return RosterIntegrity(**defaults)


def _complete(rows: tuple[RosterRow, ...] = (_row(),)) -> RosterReadOutcome:
    return RosterReadOutcome(
        status=RosterReadStatus.COMPLETE,
        rows=rows,
        integrity=_integrity(row_count=len(rows)),
    )


def _empty_source() -> RosterReadOutcome:
    return RosterReadOutcome(
        status=RosterReadStatus.EMPTY_SOURCE,
        rows=(),
        integrity=_integrity(row_count=0, reported_total=0, blank_column_rows=()),
    )


def _incomplete_with_rows() -> RosterReadOutcome:
    """整轮读完、行拿到了，但完整性判定不通过。

    **读取层刻意保留 rows**：那些行确实读到了，只是可信度没有。这个形状就是
    「以 rows 非空作替换判据」会踩中的那一个。
    """

    rows = (_row(), _row("fs-u-0002", record_id="rec-0002"))
    return RosterReadOutcome(
        status=RosterReadStatus.INCOMPLETE,
        rows=rows,
        integrity=_integrity(
            row_count=len(rows),
            reported_total=1206,
            total_matches_rows=False,
            absent_columns=("工号",),
        ),
    )


def _failed(code: str, kind: RosterFailureKind) -> RosterReadOutcome:
    return RosterReadOutcome(
        status=RosterReadStatus.FAILED,
        rows=(),
        integrity=RosterIntegrity(pages_read=2),
        failure=RosterReadFailure(code=code, kind=kind, partial_pages=2, partial_rows=700),
    )


def _previous(*, captured_at: datetime = YESTERDAY, row_count: int = 1206) -> StoredSnapshotFacts:
    return StoredSnapshotFacts(snapshot_id="rsn_0001", captured_at=captured_at, row_count=row_count)


class _RecordingStore:
    """记录调用的假快照载体。不做任何 I/O。"""

    def __init__(self, previous: StoredSnapshotFacts | None = None, *, error: Exception | None = None) -> None:
        self.previous = previous
        self.error = error
        self.replacements: list[dict[str, Any]] = []

    def load_facts(self) -> StoredSnapshotFacts | None:
        return self.previous

    def replace(self, rows: Any, integrity: Any, *, captured_at: datetime) -> str:
        if self.error is not None:
            raise self.error
        self.replacements.append({"rows": tuple(rows), "integrity": integrity, "captured_at": captured_at})
        return "rsn_new"


class _RecordingAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, dict(fields)))


class ReplacementGateTest(unittest.TestCase):
    """`V-花名册-41`：门槛只认 `status` / `complete_nonempty`，不看 rows 是否非空。"""

    def test_complete_round_installs_the_first_snapshot(self) -> None:
        decision = decide_snapshot_update(_complete(), previous=None, now=NOW)

        self.assertIs(decision.action, SnapshotAction.INSTALL)
        self.assertTrue(decision.should_replace)
        self.assertIsNone(decision.alert)
        self.assertFalse(decision.kept_previous)

    def test_complete_round_replaces_an_existing_snapshot(self) -> None:
        decision = decide_snapshot_update(_complete(), previous=_previous(), now=NOW)

        self.assertIs(decision.action, SnapshotAction.REPLACE)
        self.assertTrue(decision.should_replace)
        self.assertIsNone(decision.alert)

    def test_incomplete_round_carrying_rows_must_not_replace(self) -> None:
        # **否定用例（PR #208 二级审查钉入的合同条款）**：INCOMPLETE 保留 rows 是有意
        # 设计。以"rows 非空"为判据的实现会在这里替换掉一份好快照——源头自报 1206 行
        # 却只给了 2 行，替换等于把一次骤减包装成"今天大家都离职了"。
        outcome = _incomplete_with_rows()
        self.assertTrue(outcome.rows, "前提：这个形状必须真的带着行，否则这条否定面是空的")

        decision = decide_snapshot_update(outcome, previous=_previous(), now=NOW)

        self.assertIs(decision.action, SnapshotAction.KEEP_PREVIOUS)
        self.assertFalse(decision.should_replace)
        self.assertIs(decision.alert, SnapshotAlertKind.INCOMPLETE)

    def test_empty_source_keeps_the_previous_snapshot(self) -> None:
        decision = decide_snapshot_update(_empty_source(), previous=_previous(), now=NOW)

        self.assertIs(decision.action, SnapshotAction.KEEP_PREVIOUS)
        self.assertFalse(decision.should_replace)
        self.assertIs(decision.alert, SnapshotAlertKind.EMPTY_SOURCE)

    def test_both_failure_classes_keep_the_previous_snapshot(self) -> None:
        for code, kind in (
            ("feishu_code_99991672", RosterFailureKind.DEFINITE),
            ("transport_error", RosterFailureKind.INDETERMINATE),
        ):
            with self.subTest(kind=kind):
                decision = decide_snapshot_update(_failed(code, kind), previous=_previous(), now=NOW)
                self.assertIs(decision.action, SnapshotAction.KEEP_PREVIOUS)
                self.assertFalse(decision.should_replace)

    def test_an_outcome_claiming_complete_without_rows_is_rejected_loudly(self) -> None:
        # 读取层的契约是"COMPLETE 恒非空"。真出现这种对象说明有人改坏了判定；
        # 静默写一份零行快照会清空比对基线。
        broken = RosterReadOutcome(status=RosterReadStatus.COMPLETE, rows=(), integrity=_integrity(row_count=0))

        with self.assertRaises(ValueError):
            decide_snapshot_update(broken, previous=None, now=NOW)

    def test_an_unknown_status_fails_loudly_instead_of_defaulting_to_keep(self) -> None:
        # 读取层将来多一个状态时，这里必须响亮失败：默默归到"保旧"等于让快照在无人
        # 知晓的情况下停更。
        class _Unclassified:
            status = "brand_new_status"
            complete_nonempty = False
            rows = ()
            integrity = RosterIntegrity()
            failure = None

        with self.assertRaises(ValueError):
            decide_snapshot_update(_Unclassified(), previous=None, now=NOW)

    def test_the_clock_must_carry_a_timezone(self) -> None:
        with self.assertRaises(ValueError):
            decide_snapshot_update(_complete(), previous=None, now=datetime(2026, 8, 17, 3, 0))


class AlertClassificationTest(unittest.TestCase):
    """`V-花名册-43`：四类保旧原因互不合并。"""

    def test_each_cause_gets_its_own_alert_kind(self) -> None:
        cases = (
            (_empty_source(), SnapshotAlertKind.EMPTY_SOURCE),
            (_incomplete_with_rows(), SnapshotAlertKind.INCOMPLETE),
            (_failed("feishu_code_99991672", RosterFailureKind.DEFINITE), SnapshotAlertKind.FAILED_DEFINITE),
            (_failed("transport_error", RosterFailureKind.INDETERMINATE), SnapshotAlertKind.FAILED_INDETERMINATE),
        )
        observed = []
        for outcome, expected in cases:
            with self.subTest(expected=expected):
                decision = decide_snapshot_update(outcome, previous=_previous(), now=NOW)
                self.assertIs(decision.alert, expected)
                observed.append(decision.alert)
        self.assertEqual(len(set(observed)), 4, "四类不得合并成同一个告警")

    def test_the_failure_code_and_class_reach_the_audit_facts(self) -> None:
        decision = decide_snapshot_update(
            _failed("feishu_code_99991672", RosterFailureKind.DEFINITE), previous=_previous(), now=NOW
        )

        facts = decision.audit_facts()
        self.assertEqual(facts["failure_code"], "feishu_code_99991672")
        self.assertEqual(facts["failure_kind"], "definite")
        self.assertEqual(facts["alert"], "failed_definite")


class FirstRoundSemanticsTest(unittest.TestCase):
    """`V-花名册-44`：「从未有快照」与「保旧」是两件事，不得互相冒充。"""

    def test_a_failed_first_round_is_not_reported_as_keeping_a_previous_snapshot(self) -> None:
        decision = decide_snapshot_update(_empty_source(), previous=None, now=NOW)

        self.assertIs(decision.action, SnapshotAction.NO_SNAPSHOT_YET)
        self.assertFalse(decision.kept_previous)
        facts = decision.audit_facts()
        self.assertFalse(facts["kept_previous"])
        self.assertIsNone(facts["previous_captured_at"])
        self.assertIsNone(facts["previous_row_count"])
        # 没有上一份时年龄是 None 而不是 0——0 会被读成"刚刚更新过"。
        self.assertIsNone(facts["previous_age_seconds"])

    def test_keeping_a_previous_snapshot_reports_how_old_it_is(self) -> None:
        decision = decide_snapshot_update(_empty_source(), previous=_previous(), now=NOW)

        facts = decision.audit_facts()
        self.assertTrue(facts["kept_previous"])
        self.assertEqual(facts["previous_row_count"], 1206)
        self.assertEqual(facts["previous_age_seconds"], 86400.0)
        self.assertEqual(facts["previous_captured_at"], YESTERDAY.isoformat())

    def test_a_snapshot_facts_object_refuses_a_naive_or_empty_snapshot(self) -> None:
        with self.assertRaises(ValueError):
            StoredSnapshotFacts(snapshot_id="rsn_x", captured_at=datetime(2026, 8, 16, 3, 0), row_count=1)
        with self.assertRaises(ValueError):
            StoredSnapshotFacts(snapshot_id="rsn_x", captured_at=YESTERDAY, row_count=0)


class SnapshotUpdaterTest(unittest.TestCase):
    """编排：替换或保旧、留痕、告警注入点。"""

    def test_a_complete_round_is_written_once_with_the_round_clock(self) -> None:
        store = _RecordingStore()
        audit = _RecordingAudit()
        updater = RosterSnapshotUpdater(store=store, audit=audit)

        decision = updater.apply(_complete(), now=NOW)

        self.assertTrue(decision.should_replace)
        self.assertEqual(len(store.replacements), 1)
        self.assertEqual(store.replacements[0]["captured_at"], NOW)
        self.assertEqual(store.replacements[0]["rows"], (_row(),))
        self.assertEqual([action for action, _ in audit.records], ["roster_snapshot.replaced"])

    def test_no_write_happens_on_any_non_complete_round(self) -> None:
        for outcome in (
            _empty_source(),
            _incomplete_with_rows(),
            _failed("feishu_code_99991672", RosterFailureKind.DEFINITE),
            _failed("transport_error", RosterFailureKind.INDETERMINATE),
        ):
            with self.subTest(status=outcome.status):
                store = _RecordingStore(_previous())
                audit = _RecordingAudit()
                alerts: list[Any] = []

                RosterSnapshotUpdater(store=store, audit=audit, on_alert=alerts.append).apply(outcome, now=NOW)

                self.assertEqual(store.replacements, [], "本轮不可信却写了快照")
                self.assertEqual([action for action, _ in audit.records], ["roster_snapshot.kept_previous"])
                self.assertEqual(len(alerts), 1)

    def test_a_complete_round_raises_no_alert(self) -> None:
        alerts: list[Any] = []

        RosterSnapshotUpdater(
            store=_RecordingStore(_previous()), audit=_RecordingAudit(), on_alert=alerts.append
        ).apply(_complete(), now=NOW)

        self.assertEqual(alerts, [])

    def test_a_write_failure_is_audited_and_reraised(self) -> None:
        # 吞掉写入失败会让"快照其实一直没更新"表现为一切正常。
        store = _RecordingStore(error=RuntimeError("写库失败"))
        audit = _RecordingAudit()

        with self.assertRaises(RuntimeError):
            RosterSnapshotUpdater(store=store, audit=audit).apply(_complete(), now=NOW)

        self.assertEqual([action for action, _ in audit.records], ["roster_snapshot.replace_failed"])
        self.assertEqual(audit.records[0][1]["error"], "RuntimeError")

    def test_alerting_is_optional_and_its_absence_does_not_drop_the_audit(self) -> None:
        audit = _RecordingAudit()

        RosterSnapshotUpdater(store=_RecordingStore(_previous()), audit=audit).apply(_empty_source(), now=NOW)

        self.assertEqual([action for action, _ in audit.records], ["roster_snapshot.kept_previous"])


class _FakeCursor:
    """按脚本回答两条查询的假游标：先元信息、后行。"""

    def __init__(self, header: Any, rows: list[Any]) -> None:
        self._header = header
        self._rows = rows
        self.statements: list[str] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def execute(self, statement: str, parameters: Any = None) -> None:
        del parameters
        self.statements.append(statement)

    def fetchone(self) -> Any:
        return self._header

    def fetchall(self) -> list[Any]:
        return self._rows


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def transaction(self) -> _FakeConnection:
        return self

    def cursor(self) -> _FakeCursor:
        return self._cursor


class SnapshotReadbackConsistencyTest(unittest.TestCase):
    """二级审查 P2-A：回读到的元信息与行必须自洽，对不上就响亮失败。

    并发的一次替换恰好落在回读的两条语句之间时，READ COMMITTED 下会读出「元信息说
    N 行、行却是另一份（甚至零行）」。把它原样交出去，比对会把全体已开通用户报成
    「移除」——这正是持久快照要挡的那个形状。

    用可注入的假连接构造这个窗口：真库上它是一个需要并发才能撞到的竞态，用例里
    直接给出竞态**结果**，断言实现拒绝返回半态。
    """

    HEADER = ("rsn_0001", YESTERDAY, 3)

    def _store(self, header: Any, rows: list[Any]) -> Any:
        from lingxi.adapters import postgres_roster_snapshot

        cursor = _FakeCursor(header, rows)
        patcher = mock.patch.object(
            postgres_roster_snapshot, "connect", lambda *a, **k: _FakeConnection(cursor)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return postgres_roster_snapshot.PostgresRosterSnapshotStore("postgresql:///fake")

    def _db_row(self, personnel_id: str) -> tuple[str, str, str, str, str]:
        return (personnel_id, FAKE_EMAIL, FAKE_NAME, FAKE_EMPLOYEE_NO, FAKE_RECORD_ID)

    def test_a_consistent_snapshot_reads_back_whole(self) -> None:
        store = self._store(self.HEADER, [self._db_row(f"fs-u-000{n}") for n in (1, 2, 3)])

        stored = store.load()

        assert stored is not None
        self.assertEqual(stored.facts.row_count, 3)
        self.assertEqual(len(stored.rows), 3)

    def test_rows_lost_to_a_concurrent_replacement_are_rejected_not_returned(self) -> None:
        from lingxi.adapters.postgres_roster_snapshot import RosterSnapshotInconsistent

        for rows, label in (([], "并发替换已删掉旧行"), ([self._db_row("fs-u-0001")], "只回来一部分")):
            with self.subTest(label=label):
                store = self._store(self.HEADER, rows)
                with self.assertRaises(RosterSnapshotInconsistent) as raised:
                    store.load()
                # 只报两个数字，不报任何行内容。
                message = str(raised.exception)
                for probe in (FAKE_NAME, FAKE_EMAIL, FAKE_EMPLOYEE_NO, FAKE_PERSONNEL_ID):
                    self.assertNotIn(probe, message)

    def test_an_empty_carrier_still_reads_back_as_none(self) -> None:
        # 「从未有过快照」不是不一致：没有元信息就没有可核对的行数。
        store = self._store(None, [])

        self.assertIsNone(store.load())


class SnapshotAuditDisciplineTest(unittest.TestCase):
    """`V-花名册-46`：快照层的判定对象与审计事实不含任何花名册字段值。"""

    PROBES = (FAKE_NAME, FAKE_EMAIL, FAKE_EMPLOYEE_NO, FAKE_PERSONNEL_ID, FAKE_RECORD_ID)

    def test_audit_facts_carry_counts_and_codes_only(self) -> None:
        for outcome in (_complete(), _incomplete_with_rows(), _empty_source()):
            with self.subTest(status=outcome.status):
                decision = decide_snapshot_update(outcome, previous=_previous(), now=NOW)
                rendered = repr(decision.audit_facts())
                for probe in self.PROBES:
                    self.assertNotIn(probe, rendered)

    def test_the_decision_object_itself_carries_no_field_values(self) -> None:
        decision = decide_snapshot_update(_incomplete_with_rows(), previous=_previous(), now=NOW)

        rendered = repr(decision)
        for probe in self.PROBES:
            self.assertNotIn(probe, rendered)

    def test_the_updater_audit_lines_carry_no_field_values(self) -> None:
        audit = _RecordingAudit()

        RosterSnapshotUpdater(store=_RecordingStore(), audit=audit).apply(_complete(), now=NOW)

        rendered = repr(audit.records)
        for probe in self.PROBES:
            self.assertNotIn(probe, rendered)

    def test_the_decision_module_does_not_import_adapters(self) -> None:
        # 代码框架第二节第 1 条：`core/` 不 import `adapters/`。判定层靠结构取用读取
        # 结果，因此全部断言可以在没有数据库、没有驱动的机器上跑完。
        source = (SOURCE_ROOT / "lingxi" / "core" / "identity" / "roster_snapshot.py").read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                self.assertNotIn("lingxi.adapters", stripped)
                self.assertNotIn("lingxi.apps", stripped)


# ---- V-花名册-47 的源码扫描面 ------------------------------------------------
#
# 「快照载体不得出现任何按时间删除快照的路径」这半句**不可能靠行为用例守住**：
# 它要证明的是一条路径**不存在**，而不存在的东西没有调用点可以断言。产品负责人
# 2026-08-17 的裁定是「始终保留最近一份、豁免九十天规则、超龄不自动删只按日报告警」，
# 因此这里扫源码：谁哪天顺手给快照加一条到期清理，这条断言必须变红。
#
# **扫描器本身也被测**：下方 `ScannerClassifierTest` 用合成源码文本（不落盘）把四类
# 违规与两类合法写法各钉一遍。没有那组用例，把 `_TIME_TOKENS` 清空、把 `_EXPIRY_COLUMN`
# 改坏，真实文件照样全绿——人工验红只能证明"交付那一刻是好的"，证不了以后。

# 快照载体 = 快照的建表、读写与判定的全部落点。判定层没有 SQL，但它是「超龄」这个
# 概念的所在地，最可能被人顺手加上「超龄就删」，所以一起扫。
SNAPSHOT_CARRIER_FILES = (
    SOURCE_ROOT / "lingxi" / "adapters" / "postgres_roster_snapshot.py",
    SOURCE_ROOT / "lingxi" / "core" / "identity" / "roster_snapshot.py",
)
MIGRATION_DIRECTORY = REPOSITORY_ROOT / "migrations" / "alembic" / "versions"
SNAPSHOT_MIGRATION = MIGRATION_DIRECTORY / "0063_roster_snapshot.py"

_SNAPSHOT_TABLE = re.compile(r"roster_snapshot(_row)?\b", re.IGNORECASE)

# f-string / 拼接里被插值的那一段还原成这个记号。它不可能出现在真实 SQL 里，因此
# 可以当作"这里有一段扫描器看不见的文本"的显式标记。
INTERPOLATION = "⟪插值⟫"

# 只有"像 SQL"的片段才当语句看。中文说明里写「删掉旧的那一份」不是路径，把散文
# 也扫进来，这条断言很快就会因为改一段注释而变红，然后被人加白名单绕过。
_SQL_SHAPE = re.compile(
    r"\b(select\s|insert\s+into\b|update\s+\w|delete\s+from\b|truncate\b|"
    r"create\s+(or\s+replace\s+)?(constraint\s+)?(table|index|trigger|function|procedure|view|rule)\b|"
    r"drop\s+table\b|alter\s+table\b)",
    re.IGNORECASE,
)
_DELETE_VERB = re.compile(r"\b(delete\s+from|truncate|drop\s+table)\b", re.IGNORECASE)
# `ON DELETE CASCADE` 不是"删快照的路径"，它恰恰是「替换即删旧、不积累历史版本」的
# 实现手段（数据库设计「花名册持久快照」）。先摘掉引用完整性动作再找删除动词。
_REFERENTIAL_DELETE = re.compile(
    r"\bon\s+delete\s+(cascade|restrict|no\s+action|set\s+null|set\s+default)", re.IGNORECASE
)
# 出现在"删快照"的语句里就意味着"按时间删"：列名、时间函数、区间字面量。
_TIME_TOKENS = (
    "captured_at",
    "installed_at",
    "created_at",
    "updated_at",
    "occurred_at",
    "started_at",
    "expires_at",
    "expired_at",
    "deleted_at",
    "now()",
    "current_timestamp",
    "current_date",
    "localtimestamp",
    "clock_timestamp",
    "statement_timestamp",
    "transaction_timestamp",
    "interval",
    "age(",
    "date_trunc",
    "older",
    " days",
    " hours",
)
# 到期字段与到期触发器是同一件事的另一种写法：不必写 DELETE 也能让快照被按时清掉
# （数据库设计与迁移 0063 都写明本表**刻意没有** `expires_at`、没有到期触发器）。
_EXPIRY_COLUMN = re.compile(r"\bexpire[sd]?_at\b", re.IGNORECASE)
_CREATE_TRIGGER = re.compile(r"\bcreate\s+(or\s+replace\s+)?(constraint\s+)?trigger\b", re.IGNORECASE)
# 名字层的绊线：SQL 里不带时间词、改由 Python 侧算好时间再删，语句扫描看不见，
# 但这种入口一定会叫成"清理 / 过期 / 保留期"里的某个词。
_TIME_DELETION_NAME = re.compile(
    r"purge|prune|evict|expire|sweep|reap|vacuum|retention|ttl|older_than"
    r"|(delete|remove|drop|discard|clear|clean)[_a-z]*(old|stale|aged|expired|outdated)",
    re.IGNORECASE,
)

# Alembic 的迁移操作不是 SQL 字符串：``op.add_column("roster_snapshot", sa.Column(
# "expires_at", ...))`` 的表名与列名是两个各自清白的字面量，谁都不"像 SQL"。因此
# op.* 调用单独走一条通道，合成成一条可判定的文本。动词按方法名归一，让下面的
# 删除动词判据认得出来。
_ALEMBIC_OBJECTS = ("op", "operations")
_ALEMBIC_VERBS = {
    "drop_table": "drop table",
    "drop_index": "drop index",
    "drop_column": "alter table drop column",
    "drop_constraint": "alter table drop constraint",
    "create_table": "create table",
    "add_column": "alter table add column",
    "alter_column": "alter table alter column",
    "create_index": "create index",
}
# ``op.execute(...)`` 的实参本身就是 SQL 字符串，已由字面量通道按 `;` 正确切句；
# 再合成一遍只会把多条语句粘成一条，制造跨语句的假共现。
_ALEMBIC_PASSTHROUGH = ("execute", "get_bind", "get_context")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _literal_texts(tree: ast.AST) -> list[str]:
    """树里的字符串字面量（跳过 docstring），f-string 还原成带插值记号的整条。

    跳过 docstring 是因为它是散文不是路径：0063 的文件头恰好写着「本表刻意没有
    ``expires_at``」，把说明当语句扫会让"写明不做"变成"违规"。
    """

    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            docstrings.add(id(body[0].value))

    texts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            texts.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            texts.append(
                "".join(
                    part.value
                    if isinstance(part, ast.Constant) and isinstance(part.value, str)
                    else INTERPOLATION
                    for part in node.values
                )
            )
    return texts


def _sql_statements(source: str) -> list[str]:
    """源码里像 SQL 的字符串字面量，按 `;` 切成单条语句。"""

    statements: list[str] = []
    for text in _literal_texts(ast.parse(source)):
        for piece in text.split(";"):
            if _SQL_SHAPE.search(piece):
                statements.append(piece)
    return statements


def _alembic_operations(source: str) -> list[str]:
    """把 ``op.<方法>(...)`` 合成成一条可判定的文本：归一动词 + 调用内全部字面量。

    ``op.add_column("roster_snapshot", sa.Column("expires_at", ...))`` 因此变成
    ``alter table add column roster_snapshot expires_at``——表名与列名重新回到同一条
    语句里，到期列判据才看得见它。
    """

    units: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        root = node.func.value
        if not (isinstance(root, ast.Name) and root.id in _ALEMBIC_OBJECTS):
            continue
        method = node.func.attr
        if method in _ALEMBIC_PASSTHROUGH:
            continue
        literals = [" ".join(text.split()) for text in _literal_texts(node) if text.strip()]
        if not literals:
            continue
        units.append(" ".join([_ALEMBIC_VERBS.get(method, method), *literals]))
    return units


def _ddl_units(source: str) -> list[str]:
    """一份源码里所有可判定的单元：SQL 语句 + Alembic op.* 调用。"""

    return _sql_statements(source) + _alembic_operations(source)


def _snapshot_units(source: str) -> list[str]:
    """其中与快照两张表有关的那些。

    **表名被插值的语句一律算进来**（fail closed）：被扫的这几个文件里表名全是常量，
    出现动态表名本身就该被重新解释，而不是因为"看不出是哪张表"被静默放过。
    """

    return [unit for unit in _ddl_units(source) if _SNAPSHOT_TABLE.search(unit) or INTERPOLATION in unit]


def _time_conditioned_deletes(source: str) -> list[tuple[str, list[str]]]:
    """既删快照、又带时间条件的单元（连同命中的时间词一起返回，便于失败时看清）。"""

    offenders: list[tuple[str, list[str]]] = []
    for unit in _snapshot_units(source):
        body = _REFERENTIAL_DELETE.sub(" ", unit)
        if not _DELETE_VERB.search(body):
            continue
        hits = [token for token in _TIME_TOKENS if token in body.lower()]
        if hits:
            offenders.append((" ".join(unit.split()), hits))
    return offenders


def _expiry_definitions(source: str) -> list[str]:
    """给快照定义到期时间列或到期触发器的单元。不写 DELETE 也能让它被按时清掉。"""

    return [
        " ".join(unit.split())
        for unit in _snapshot_units(source)
        if _EXPIRY_COLUMN.search(unit) or _CREATE_TRIGGER.search(unit)
    ]


def _timed_cleanup_names(source: str) -> list[str]:
    """按时间清理形态的标识符：定义名、变量名、属性名、参数名。"""

    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        for attribute in ("name", "id", "attr", "arg"):
            value = getattr(node, attribute, None)
            if isinstance(value, str) and _TIME_DELETION_NAME.search(value):
                names.add(value)
    return sorted(names)


def _violations(source: str) -> dict[str, list]:
    """一份源码的全部判定结果。空字典值＝这一类没命中。"""

    return {
        "按时间删除快照": _time_conditioned_deletes(source),
        "到期列或到期触发器": _expiry_definitions(source),
        "按时间清理的入口名": _timed_cleanup_names(source),
    }


def _migrations_touching_the_snapshot() -> list[Path]:
    return sorted(path for path in MIGRATION_DIRECTORY.glob("*.py") if _SNAPSHOT_TABLE.search(_read(path)))


# 合成源码（不落盘）：把扫描器的四个分类器逐个钉住。每条都是一个完整的 Python 模块
# 文本，因为扫描器读的就是模块——用片段喂它等于测了一个不存在的输入形态。
_RED_SAMPLES = {
    "SQL 里按时间删": (
        'PURGE = "DELETE FROM roster_snapshot WHERE captured_at < now() - interval \'48 hours\'"\n',
        "按时间删除快照",
    ),
    "表名被插值的按时间删": (
        'def build(table):\n    return f"DELETE FROM {table} WHERE captured_at < %s"\n',
        "按时间删除快照",
    ),
    "op.execute 里按时间删": (
        'def upgrade():\n'
        '    op.execute("DELETE FROM roster_snapshot WHERE captured_at < now() - interval \'90 days\'")\n',
        "按时间删除快照",
    ),
    "建表时就带到期列": (
        'SQL = """\nCREATE TABLE roster_snapshot (\n'
        "    id TEXT PRIMARY KEY,\n    expires_at TIMESTAMPTZ NOT NULL\n);\n\"\"\"\n",
        "到期列或到期触发器",
    ),
    "op.add_column 加到期列": (
        'def upgrade():\n'
        '    op.add_column("roster_snapshot", sa.Column("expires_at", sa.TIMESTAMP(timezone=True)))\n',
        "到期列或到期触发器",
    ),
    "op.create_table 带到期列": (
        'def upgrade():\n'
        '    op.create_table("roster_snapshot", sa.Column("id"), sa.Column("expires_at"))\n',
        "到期列或到期触发器",
    ),
    "到期触发器": (
        'SQL = """\nCREATE TRIGGER roster_snapshot_expiry\n'
        "    BEFORE INSERT ON roster_snapshot\n"
        "    FOR EACH ROW EXECUTE FUNCTION set_expiry();\n\"\"\"\n",
        "到期列或到期触发器",
    ),
    "CREATE OR REPLACE TRIGGER 形态": (
        'SQL = """\nCREATE OR REPLACE TRIGGER roster_snapshot_expiry\n'
        "    BEFORE INSERT ON roster_snapshot FOR EACH ROW EXECUTE FUNCTION set_expiry();\n\"\"\"\n",
        "到期列或到期触发器",
    ),
    "按时间清理的入口名": (
        "class Store:\n    def purge_stale_snapshots(self, cutoff):\n        pass\n",
        "按时间清理的入口名",
    ),
}

_GREEN_SAMPLES = {
    "替换即删旧的整体 DELETE": 'DELETE_SNAPSHOT_SQL = "DELETE FROM roster_snapshot"\n',
    "ON DELETE CASCADE 是保证不是路径": (
        'SQL = """\nCREATE TABLE roster_snapshot_row (\n'
        "    snapshot_id TEXT NOT NULL REFERENCES roster_snapshot(id) ON DELETE CASCADE,\n"
        "    row_index INTEGER NOT NULL\n);\n\"\"\"\n"
    ),
    "downgrade 整表删除": 'def downgrade():\n    op.drop_table("roster_snapshot")\n',
    "回读语句带时间列但不删东西": (
        'LOAD_FACTS_SQL = "SELECT id, captured_at, row_count FROM roster_snapshot"\n'
    ),
    "散文里写明刻意没有到期列": (
        '"""快照表刻意没有 expires_at，也没有到期触发器：始终保留最近一份。"""\n'
    ),
    "超龄判定本身不是删除入口": (
        "DEFAULT_SNAPSHOT_STALE_AFTER = 48\n"
        "class Status:\n    stale_after_seconds = 0.0\n"
        "    def stale(self):\n        return True\n"
    ),
}


class ScannerClassifierTest(unittest.TestCase):
    """扫描器自身的反例用例：四个分类器各自必红，两类合法写法必绿。

    交付时的人工验红（在真实文件里种入违规样本再恢复）只证明**那一刻**扫描器是好的。
    把判据清空、正则改坏、`op.*` 通道摘掉，真实文件照样全绿——因此把分类器钉在这里。
    合成源码只进内存，不落盘。
    """

    def test_every_violation_shape_is_classified_red(self) -> None:
        for label, (source, expected) in _RED_SAMPLES.items():
            with self.subTest(sample=label):
                hits = _violations(source)
                self.assertTrue(hits[expected], f"{label} 应当被判为「{expected}」，扫描器却没看见")

    def test_legitimate_shapes_stay_green(self) -> None:
        for label, source in _GREEN_SAMPLES.items():
            with self.subTest(sample=label):
                hits = {kind: found for kind, found in _violations(source).items() if found}
                self.assertEqual(hits, {}, f"{label} 是合法写法，不该被判违规")


class SnapshotHasNoTimeBasedDeletionPathTest(unittest.TestCase):
    """`V-花名册-47` 的源码扫描面：快照载体里没有任何按时间删除快照的路径。

    裁定（产品负责人 2026-08-17）是「始终保留最近一份、豁免九十天规则；超龄不自动删，
    改为按日报告警提醒」。超龄告警那一半有行为用例（`tests/test_roster_daily_source.py`
    与 `tests/test_roster_daily_report.py`）；**"不自动删"这一半只能扫源码**——要证明的
    是一条路径不存在，不存在的东西没有调用点可以断言。

    **这是一条绊线，不是安全边界。** 它挡的是"顺手加一条到期清理"这种最可能发生的
    形态，不承诺穷举。已登记的盲区（命中不了、需要人工审查兜底）：

    - Python 侧先按时间算出快照 ID、SQL 里只剩 ``WHERE id = %s``；
    - 用 ``gc`` / ``compact`` / ``rotate`` 这类不在词表里的命名；
    - ``psycopg.sql`` 的分段组装、``EXECUTE`` 动态语句、跨函数拼接；
    - ``MERGE ... WHEN MATCHED THEN DELETE``、分区 ``DETACH``、``TRUNCATE`` 的变体写法；
    - 迁移之外的运维脚本、数据库侧的定时作业（不在本仓库）。

    反过来的保守面（可能误红）也是刻意的：``DELETE ... RETURNING captured_at``、与到期
    无关的触发器都会被拦下；表名被插值的语句一律按"可能是快照"处理（fail closed），
    因此同一个文件里针对**别的**表的动态删除也会算到快照头上。命中**不等于**一定有
    bug，等于**必须重新解释这条路径为什么存在**——把它改绿之前先回答"快照会不会因此
    被按时间删掉"。
    """

    def test_the_scanner_really_sees_the_carrier_sql(self) -> None:
        """扫描器自证（适配器侧）：它确实看到了载体里真实存在的删除语句。

        没有这一条，下面几条断言在扫描器坏掉（改了文件名、SQL 换了写法、AST 取空）
        时会全部保持绿色——"什么都没扫到"和"扫了没问题"必须区分开。
        """

        for path in (*SNAPSHOT_CARRIER_FILES, SNAPSHOT_MIGRATION):
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file(), f"快照载体文件不在了：{path}")

        adapter = SNAPSHOT_CARRIER_FILES[0]
        units = _snapshot_units(_read(adapter))
        self.assertTrue(units, f"扫描器在 {adapter.name} 里一条快照 SQL 都没看到")
        self.assertTrue(
            any(_DELETE_VERB.search(_REFERENTIAL_DELETE.sub(" ", unit)) for unit in units),
            "扫描器没看到整体替换用的 DELETE：它已经不认识这个文件里的 SQL 了",
        )

    def test_the_scanner_really_extracts_the_migration_ddl(self) -> None:
        """扫描器自证（迁移侧）：0063 的建表 DDL 真的被提取到了。

        DDL 层最容易"扫了个寂寞"——迁移可以写成 SQL 字符串、也可以写成 `op.*` 调用，
        任何一种没被提取，到期列与到期触发器那两条断言都会变成一条永远为真的空断言。
        """

        units = [" ".join(unit.split()).lower() for unit in _snapshot_units(_read(SNAPSHOT_MIGRATION))]

        self.assertTrue(units, "扫描器没从 0063 提取到任何快照 DDL：这一层等于没扫")
        self.assertTrue(
            any(unit.startswith("create table roster_snapshot ") for unit in units),
            f"没提取到 roster_snapshot 的建表语句，只看到：{units[:3]}",
        )
        self.assertTrue(
            any("roster_snapshot_row" in unit for unit in units),
            "没提取到行表的建表语句：两张表里少扫一张，等于漏掉一半载体",
        )

    def test_the_carrier_never_deletes_a_snapshot_by_time(self) -> None:
        """替换时的整体 DELETE 是允许的（替换即删旧），带时间条件的删除不允许。"""

        for path in SNAPSHOT_CARRIER_FILES:
            with self.subTest(path=path.name):
                self.assertEqual(
                    _time_conditioned_deletes(_read(path)),
                    [],
                    f"{path.name} 出现了按时间删除快照的语句（V-花名册-47：超龄不自动删）",
                )

    def test_no_migration_deletes_the_snapshot_by_time(self) -> None:
        """迁移里同样不许：一条 `DELETE ... WHERE captured_at < now() - interval` 的清理
        任务写进迁移，和写进适配器是同一件事。"""

        migrations = _migrations_touching_the_snapshot()
        self.assertIn(SNAPSHOT_MIGRATION, migrations, "0063 不在扫描范围里：扫描目录或表名不对")
        for path in migrations:
            with self.subTest(migration=path.name):
                self.assertEqual(_time_conditioned_deletes(_read(path)), [], f"{path.name} 按时间删除快照")

    def test_the_snapshot_tables_carry_no_expiry_column_or_trigger(self) -> None:
        """不写 DELETE 也能按时清掉：加一列 `expires_at` 再挂一个到期触发器即可。

        数据库设计「花名册持久快照」与迁移 0063 都写明本表**刻意没有**这两样。
        """

        for path in (*SNAPSHOT_CARRIER_FILES, *_migrations_touching_the_snapshot()):
            with self.subTest(path=path.name):
                self.assertEqual(
                    _expiry_definitions(_read(path)),
                    [],
                    f"{path.name} 给快照定义了到期时间列或到期触发器",
                )

    def test_no_carrier_entry_point_is_named_after_a_timed_cleanup(self) -> None:
        """名字层的绊线：时间在 Python 侧算、SQL 里只剩一条无条件 DELETE，语句扫描
        看不见，但这种入口一定会叫 `purge_stale_*` / `delete_expired_*` / `*_older_than`
        之类的名字。命中不等于一定有 bug，等于**必须重新解释这条路径为什么存在**。
        """

        for path in SNAPSHOT_CARRIER_FILES:
            with self.subTest(path=path.name):
                self.assertEqual(
                    _timed_cleanup_names(_read(path)),
                    [],
                    f"{path.name} 出现了按时间清理快照的入口名",
                )


if __name__ == "__main__":
    unittest.main()
