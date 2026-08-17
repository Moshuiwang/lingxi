"""花名册持久快照的替换门槛与保旧告警（Issue #52 / S-B-02，无网络、无数据库）。

认领断言：V-花名册-41（替换门槛只认 `status`）、V-花名册-43（保旧告警四类分开）、
V-花名册-44（首轮无快照与保旧可区分）、V-花名册-46（快照层审计不含字段值）。

**用的是读取层真正的结果类型**（`RosterReadOutcome` 等）而不是自造的假对象：门槛
判定的全部价值在于它与读取层的四态语义对齐，拿假对象断言只能证明"我自己想的那套
是自洽的"。

真库那半边（原子替换、单例约束、CASCADE、行原样回读）在
`tests/test_roster_snapshot_postgres.py`，本文件不碰数据库。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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

NOW = datetime(2026, 8, 17, 3, 0, tzinfo=timezone.utc)
YESTERDAY = NOW - timedelta(days=1)

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"


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


if __name__ == "__main__":
    unittest.main()
