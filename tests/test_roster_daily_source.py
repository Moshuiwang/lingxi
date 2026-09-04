"""每日花名册取用链与快照超龄告警（Issue #52 / S-B-04，无网络、无数据库）。

认领断言：V-花名册-47（快照超龄按日报告警提醒、不自动删）、V-花名册-48（没有可用
快照时不比对）；另覆盖 V-花名册-16/25/30 在新链路下的形态。

这条链是 S-B-04 补上的那一段：**读一轮 → 更新或保留持久快照 → 交出这一轮用于比对的
行**。此前每一段都已经存在且各自有断言，但没有任何东西回答"日报到底拿哪一份行去比对"
——而那正是唯一一个会把全体已开通用户报成「移除」的接缝。

用的是读取层与快照层**真正的类型**（`RosterReadOutcome`、`SnapshotDecision`），不是自造
的假结果：这条链的全部价值在于它与那两层的语义对齐。
"""

from __future__ import annotations

import threading
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any

from test_roster_audit_duty import (
    COMPLETE_ENV,
    EMAIL,
    EMPLOYEE_NO,
    FAKE_CHAT_ID,
    NAME,
    NEW_NAME,
    PERSON_ONE,
    USER_ONE,
    FixedClock,
    RecordingAudit,
)

from lingxi.adapters.feishu_roster_bitable import (
    RosterFailureKind,
    RosterIntegrity,
    RosterReadFailure,
    RosterReadOutcome,
    RosterReadStatus,
    RosterRow,
)
from lingxi.apps.scheduler import RosterAuditDuty, SchedulerConfig, SchedulerLoop
from lingxi.core.identity.roster_audit import ArchivedIdentity
from lingxi.core.identity.roster_snapshot import (
    DEFAULT_SNAPSHOT_STALE_AFTER,
    DailyRosterSource,
    RosterSnapshotUpdater,
    StoredSnapshotFacts,
)

NOW = datetime(2026, 8, 17, 3, 0, tzinfo=UTC)
STALE_AFTER = DEFAULT_SNAPSHOT_STALE_AFTER


def _row(personnel_id: str = PERSON_ONE, *, name: str = NEW_NAME) -> RosterRow:
    return RosterRow(
        personnel_id=personnel_id,
        email=EMAIL,
        name=name,
        employee_no=EMPLOYEE_NO,
        record_id="rec-0001",
    )


def _complete(rows: tuple[RosterRow, ...] = (_row(),)) -> RosterReadOutcome:
    return RosterReadOutcome(
        status=RosterReadStatus.COMPLETE,
        rows=rows,
        integrity=RosterIntegrity(row_count=len(rows), pages_read=1),
    )


def _empty_source() -> RosterReadOutcome:
    return RosterReadOutcome(
        status=RosterReadStatus.EMPTY_SOURCE, rows=(), integrity=RosterIntegrity(pages_read=1)
    )


def _failed() -> RosterReadOutcome:
    return RosterReadOutcome(
        status=RosterReadStatus.FAILED,
        rows=(),
        integrity=RosterIntegrity(pages_read=2),
        failure=RosterReadFailure(
            code="feishu_code_91403",
            kind=RosterFailureKind.DEFINITE,
            partial_pages=2,
            partial_rows=700,
        ),
    )


class _StoredSnapshot:
    """回读出来的快照（同 ``adapters.postgres_roster_snapshot.StoredRosterSnapshot`` 的形状）。"""

    def __init__(self, facts: StoredSnapshotFacts, rows: tuple[RosterRow, ...]) -> None:
        self.facts = facts
        self.rows = rows


class _Store:
    """假持久载体：记录替换调用，按需交出上一份快照或抛出回读不一致。"""

    def __init__(
        self,
        *,
        stored: _StoredSnapshot | None = None,
        load_error: Exception | None = None,
    ) -> None:
        self.stored = stored
        self.load_error = load_error
        self.replacements: list[dict[str, Any]] = []

    def load_facts(self) -> StoredSnapshotFacts | None:
        return None if self.stored is None else self.stored.facts

    def load(self) -> _StoredSnapshot | None:
        if self.load_error is not None:
            raise self.load_error
        return self.stored

    def replace(self, rows: Any, integrity: Any, *, captured_at: datetime) -> str:
        self.replacements.append({"rows": tuple(rows), "captured_at": captured_at})
        return "rsn_new"


def _previous(*, age: timedelta, row_count: int = 1206) -> _StoredSnapshot:
    facts = StoredSnapshotFacts(snapshot_id="rsn_0001", captured_at=NOW - age, row_count=row_count)
    return _StoredSnapshot(facts, tuple(_row(name=NAME) for _ in range(1)))


def _source(
    outcome: RosterReadOutcome,
    *,
    store: _Store,
    audit: RecordingAudit | None = None,
    stale_after: timedelta = STALE_AFTER,
) -> DailyRosterSource:
    return DailyRosterSource(
        read_round=lambda: outcome,
        updater=RosterSnapshotUpdater(store=store, audit=audit or RecordingAudit()),
        load_snapshot=store.load,
        stale_after=stale_after,
    )


class SnapshotRefreshTest(unittest.TestCase):
    """整轮读成功那一天：比对用**本轮读到的行**，快照同时被换掉。"""

    def test_a_complete_round_replaces_the_snapshot_and_compares_this_round_rows(self) -> None:
        store = _Store(stored=_previous(age=timedelta(days=3)))

        round_result = _source(_complete(), store=store).current(now=NOW)

        self.assertEqual(len(store.replacements), 1, "COMPLETE 必须替换快照")
        self.assertEqual([row.name for row in round_result.rows], [NEW_NAME], "比对用本轮读到的行")
        self.assertTrue(round_result.snapshot.refreshed)
        self.assertTrue(round_result.snapshot.available)
        self.assertFalse(round_result.snapshot.stale, "刚换的快照不可能超龄")
        self.assertEqual(round_result.snapshot.age_seconds, 0.0)
        self.assertEqual(round_result.snapshot.captured_at, NOW)

    def test_a_refreshed_round_does_not_read_the_snapshot_back(self) -> None:
        """刚写进去的就是本轮读到的那一份，再回读一次既慢，又多开一个能读到别人
        那一份的并发窗口。用"回读必炸"的载体证明这条路径确实没走。"""

        store = _Store(load_error=AssertionError("替换成功的那一轮不该回读快照"))

        round_result = _source(_complete(), store=store).current(now=NOW)

        self.assertEqual(len(round_result.rows), 1)


class KeepPreviousTest(unittest.TestCase):
    """读取不成功那一天：比对用**库里那一份上一次成功的快照**，并说明保旧原因。"""

    def test_an_empty_source_compares_against_the_previous_snapshot(self) -> None:
        """产品负责人 2026-08-07 决定：空源、失败或半轮读取继续保留上一份有效快照。

        这里的关键不是"快照没被覆盖"（那由 S-B-02 的门槛保证），而是**比对拿到的是
        上一份的行**——拿本轮那个空行集去比，全体已开通用户会被报成「移除」。
        """

        store = _Store(stored=_previous(age=timedelta(hours=5)))

        round_result = _source(_empty_source(), store=store).current(now=NOW)

        self.assertEqual(store.replacements, [], "空源不得替换快照")
        self.assertEqual(len(round_result.rows), 1, "比对用上一份快照的行")
        self.assertFalse(round_result.snapshot.refreshed)
        self.assertTrue(round_result.snapshot.available)
        self.assertEqual(round_result.snapshot.alert, "empty_source")
        self.assertEqual(round_result.snapshot.age_seconds, 5 * 3600)

    def test_a_definite_failure_keeps_the_previous_rows_and_names_the_error_code(self) -> None:
        store = _Store(stored=_previous(age=timedelta(hours=2)))

        round_result = _source(_failed(), store=store).current(now=NOW)

        self.assertEqual(len(round_result.rows), 1)
        self.assertEqual(round_result.snapshot.alert, "failed_definite")
        self.assertEqual(round_result.snapshot.failure_code, "feishu_code_91403")
        # 半轮读到的 700 行一行都不进来：那正是空源保护要挡的形状。
        self.assertEqual([row.name for row in round_result.rows], [NAME])

    def test_a_readback_inconsistency_is_raised_rather_than_half_a_snapshot(self) -> None:
        """回读到的元信息与行对不上是一次并发窗口的信号。这里**不捕获**：
        一份"元信息说 1206 行、实际零行"的快照会被比对读成「全员移除」。

        它由职责层的失败隔离承接（`V-花名册-17`），下一轮重试通常就好了。
        """

        from lingxi.adapters.postgres_roster_snapshot import RosterSnapshotInconsistentError

        store = _Store(load_error=RosterSnapshotInconsistentError("元信息 1206 行，实际回来 0 行"))

        with self.assertRaises(RosterSnapshotInconsistentError):
            _source(_empty_source(), store=store).current(now=NOW)


class StaleSnapshotTest(unittest.TestCase):
    """`V-花名册-47`：快照超龄按日报告警提醒，且**不自动删**。"""

    def test_a_snapshot_older_than_the_threshold_is_flagged_stale(self) -> None:
        store = _Store(stored=_previous(age=STALE_AFTER + timedelta(hours=1)))

        round_result = _source(_empty_source(), store=store).current(now=NOW)

        self.assertTrue(round_result.snapshot.stale)
        self.assertTrue(round_result.snapshot.needs_attention)
        # 超龄**不删**：载体照旧交出上一份，比对照常能跑。
        self.assertEqual(len(round_result.rows), 1)
        self.assertEqual(store.replacements, [])

    def test_a_snapshot_at_or_under_the_threshold_is_not_stale(self) -> None:
        for age in (STALE_AFTER, STALE_AFTER - timedelta(seconds=1)):
            with self.subTest(age=age):
                store = _Store(stored=_previous(age=age))

                round_result = _source(_empty_source(), store=store).current(now=NOW)

                self.assertFalse(round_result.snapshot.stale)
                self.assertFalse(round_result.snapshot.needs_attention)

    def test_the_threshold_is_configurable_and_actually_used(self) -> None:
        """阈值可配置（花名册的维护节奏是部署事实），但配了必须真的生效——
        一个"读了配置却按默认判"的实现在这里变红。"""

        store = _Store(stored=_previous(age=timedelta(hours=6)))

        round_result = _source(
            _empty_source(), store=store, stale_after=timedelta(hours=5)
        ).current(now=NOW)

        self.assertTrue(round_result.snapshot.stale)
        self.assertEqual(round_result.snapshot.stale_after_seconds, 5 * 3600)

    def test_a_non_positive_threshold_is_refused(self) -> None:
        for bad in (timedelta(0), timedelta(hours=-1), 48):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    DailyRosterSource(
                        read_round=lambda: _complete(),
                        updater=RosterSnapshotUpdater(store=_Store(), audit=RecordingAudit()),
                        load_snapshot=_Store().load,
                        stale_after=bad,
                    )


class NoSnapshotAtAllTest(unittest.TestCase):
    """`V-花名册-48`：一份快照都没有时不比对。"""

    def test_a_first_round_failure_yields_no_rows_and_an_unavailable_snapshot(self) -> None:
        store = _Store(stored=None)

        round_result = _source(_failed(), store=store).current(now=NOW)

        self.assertEqual(round_result.rows, ())
        self.assertFalse(round_result.snapshot.available)
        self.assertFalse(round_result.snapshot.stale, "没有快照不等于快照旧了，两件事分开报")
        self.assertTrue(round_result.snapshot.needs_attention)
        self.assertIsNone(round_result.snapshot.age_seconds, "没有上一份就没有年龄，不能是 0")


class DutyBehaviourTest(unittest.TestCase):
    """职责这一层：什么时候发、什么时候只记审计、什么时候根本不比对。"""

    def _duty(
        self,
        outcome: RosterReadOutcome,
        *,
        store: _Store,
        sender: Any,
        audit: RecordingAudit,
        clock: FixedClock,
        baseline: list[ArchivedIdentity] | None = None,
        stop: threading.Event | None = None,
    ) -> RosterAuditDuty:
        class Baseline:
            def __init__(self, people: list[ArchivedIdentity]) -> None:
                self._people = people

            def load_active_baseline(self) -> list[ArchivedIdentity]:
                return self._people

        people = (
            baseline
            if baseline is not None
            else [ArchivedIdentity(USER_ONE, PERSON_ONE, NAME, EMPLOYEE_NO, EMAIL)]
        )
        return RosterAuditDuty(
            baseline_reader=Baseline(people),
            roster_source=_source(outcome, store=store, audit=audit),
            sender=sender,
            audit=audit,
            chat_id=FAKE_CHAT_ID,
            clock=clock,
            stop=stop,
        )

    class _Sender:
        def __init__(self) -> None:
            self.payloads: list[dict[str, str]] = []

        def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None:
            self.payloads.append({"chat_id": chat_id, "text": text, "dedupe_key": dedupe_key})

    def test_an_empty_day_with_a_fresh_snapshot_still_sends_nothing(self) -> None:
        """`V-花名册-25` 不因为新增告警而松动：快照新鲜、又没有差异，就不打扰管理群。"""

        store = _Store(stored=_previous(age=timedelta(hours=1)))
        sender, audit, clock = self._Sender(), RecordingAudit(), FixedClock(start=NOW.date())
        duty = self._duty(
            _complete((_row(name=NAME),)), store=store, sender=sender, audit=audit, clock=clock
        )

        report = duty.run_once()

        self.assertTrue(report.is_empty)
        self.assertEqual(sender.payloads, [], "空差异日且快照新鲜：不发")
        self.assertIn("roster_audit.no_difference", [action for action, _ in audit.records])

    def test_an_empty_day_with_a_stale_snapshot_sends_the_warning(self) -> None:
        """`V-花名册-47` 的落点：源头连续读不到时，"今天没有差异"这句话不可信。

        沉默是这一天最危险的输出——基线在变旧，而没有任何东西会告诉管理员。
        """

        store = _Store(stored=_previous(age=STALE_AFTER + timedelta(hours=2)))
        sender, audit, clock = self._Sender(), RecordingAudit(), FixedClock(start=NOW.date())
        duty = self._duty(_empty_source(), store=store, sender=sender, audit=audit, clock=clock)

        report = duty.run_once()

        self.assertTrue(report.is_empty, "用例前提：确实没有资料差异")
        self.assertEqual(len(sender.payloads), 1, "快照超龄时即使零差异也要发")
        body = sender.payloads[0]["text"]
        self.assertIn("提醒：", body)
        self.assertIn("不会被自动删除", body)
        self.assertEqual(duty.completed_on, clock.today, "发出去了就算当天做完")

    def test_without_any_snapshot_the_duty_never_reports_everyone_as_removed(self) -> None:
        """`V-花名册-48` 的落点，也是这条链存在的全部理由。

        六名已开通用户 + 一份读不出来的花名册：**旧写法**（直接拿本轮的空行集去比）
        会发出一份"六个人全部从花名册消失"的日报，而事实只是今天没读到。
        """

        store = _Store(stored=None)
        sender, audit, clock = self._Sender(), RecordingAudit(), FixedClock(start=NOW.date())
        people = [
            ArchivedIdentity(
                f"usr_{index:026d}",
                f"ou_p_{index}",
                f"化名{index}",
                f"E100{index}",
                f"p{index}@example.invalid",
            )
            for index in range(6)
        ]
        duty = self._duty(
            _failed(), store=store, sender=sender, audit=audit, clock=clock, baseline=people
        )

        report = duty.run_once()

        self.assertEqual(report.entries, (), "没有基线就不比对，一条『移除』都不许产生")
        self.assertEqual(report.removed_count, 0)
        self.assertEqual(len(sender.payloads), 1, "但必须告警：这一天没看，不等于没变化")
        body = sender.payloads[0]["text"]
        self.assertIn("本次未进行比对", body)
        self.assertNotIn("花名册查无此人", body)
        for person in people:
            with self.subTest(person=person.app_user_id):
                self.assertNotIn(person.app_user_id, body)

    def test_a_readback_inconsistency_isolates_to_this_duty_and_retries_next_round(self) -> None:
        """`V-花名册-17`：响亮失败由循环隔离，其余职责本轮照跑，水位不置位。"""

        from lingxi.adapters.postgres_roster_snapshot import RosterSnapshotInconsistentError

        store = _Store(load_error=RosterSnapshotInconsistentError("元信息 1206 行，实际回来 0 行"))
        sender, audit, clock = self._Sender(), RecordingAudit(), FixedClock(start=NOW.date())
        duty = self._duty(_empty_source(), store=store, sender=sender, audit=audit, clock=clock)

        class Other:
            name = "保留清理"

            def __init__(self) -> None:
                self.calls = 0

            def run_once(self) -> str:
                self.calls += 1
                return "ok"

        other = Other()
        loop = SchedulerLoop(duties=(duty, other), interval_seconds=0.01)

        with self.assertLogs("lingxi.apps.scheduler", level="ERROR") as captured:
            reports = loop.run_once()

        self.assertIsNone(reports[0])
        self.assertEqual(other.calls, 1, "一个职责炸掉不得带走另一个")
        self.assertEqual(sender.payloads, [])
        self.assertIsNone(duty.completed_on, "没做完就不能置水位，下一轮要重试")
        output = "\n".join(captured.output)
        self.assertIn("RosterSnapshotInconsistent", output)
        # 异常消息里只有两个数字，行内容一个都没有。
        self.assertNotIn(NAME, output)


class StaleThresholdConfigTest(unittest.TestCase):
    """阈值来自环境变量；默认 48 小时，配错则快速失败。"""

    def test_the_default_is_forty_eight_hours(self) -> None:
        config = SchedulerConfig.from_env(COMPLETE_ENV)

        self.assertEqual(config.roster_snapshot_stale_after, timedelta(hours=48))
        self.assertEqual(DEFAULT_SNAPSHOT_STALE_AFTER, timedelta(hours=48))

    def test_a_configured_value_is_taken(self) -> None:
        config = SchedulerConfig.from_env(
            {**COMPLETE_ENV, "LINGXI_ROSTER_SNAPSHOT_STALE_AFTER_HOURS": "12"}
        )

        self.assertEqual(config.roster_snapshot_stale_after, timedelta(hours=12))

    def test_a_malformed_or_out_of_range_value_fails_fast(self) -> None:
        """0、负数、非数字、以及大到等于"把告警关掉"的值都拒绝。

        上界不是洁癖：一个 100 年的阈值会让超龄告警永远不触发，而"关掉了"这件事
        不该由一个看起来像数字的配置悄悄完成。
        """

        for bad in ("0", "-1", "abc", "876000", "nan"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as raised:
                    SchedulerConfig.from_env(
                        {**COMPLETE_ENV, "LINGXI_ROSTER_SNAPSHOT_STALE_AFTER_HOURS": bad}
                    )

                self.assertIn("LINGXI_ROSTER_SNAPSHOT_STALE_AFTER_HOURS", str(raised.exception))

    def test_the_roster_identifiers_are_only_ever_read_from_the_environment(self) -> None:
        config = SchedulerConfig.from_env(
            {
                **COMPLETE_ENV,
                "LINGXI_ROSTER_BITABLE_APP_TOKEN": "bascnFakeAppToken",
                "LINGXI_ROSTER_BITABLE_TABLE_ID": "tblFakeTable",
            }
        )

        self.assertEqual(config.roster_app_token, "bascnFakeAppToken")
        self.assertEqual(config.roster_table_id, "tblFakeTable")
        for key in ("LINGXI_ROSTER_BITABLE_APP_TOKEN", "LINGXI_ROSTER_BITABLE_TABLE_ID"):
            with self.subTest(key=key):
                self.assertIn(key, SchedulerConfig.ENVIRONMENT_KEYS)

    def test_a_roster_identifier_with_whitespace_fails_fast_without_echoing_it(self) -> None:
        """错配不是未配：一个带了换行的 Base token 静默降级成"没配"，会让日报职责
        悄悄不注册，而运维那边看到的是"我明明配了"。"""

        with self.assertRaises(ValueError) as raised:
            SchedulerConfig.from_env(
                {**COMPLETE_ENV, "LINGXI_ROSTER_BITABLE_APP_TOKEN": "bascn Fake Token"}
            )

        message = str(raised.exception)
        self.assertIn("LINGXI_ROSTER_BITABLE_APP_TOKEN", message)
        self.assertNotIn("bascn Fake Token", message, "错误消息不得回显取到的值")


if __name__ == "__main__":
    unittest.main()
