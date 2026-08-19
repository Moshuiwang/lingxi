"""组织快照同步职责的编排用例（Issue #250）。

``read_snapshot`` 与 ``store`` 都是注入的假对象，不发起任何网络或数据库调用；
真实两条身份路径的组装形状见 ``tests/test_feishu_org_snapshot_reader.py``，
四张表的落库与批次完整性判据的真库断言见
``tests/test_identity_postgres_records.py`` 的 ``OrgSnapshotTest``。本文件只证明
职责编排本身的三条纪律：每 UTC 日至多一轮、失败不置位水位（下一轮重试）、
**空源 / 半页 / 超时 / 格式异常不得替换基线**——这条纪律的落点是"读失败或校验
不通过时职责绝不调用会真的改变已有完成批次的那条路径"。
"""

from __future__ import annotations

import threading
import unittest
from datetime import datetime, timezone

from lingxi.apps.scheduler.org_snapshot_sync import OrgSnapshotSyncDuty
from lingxi.core.identity.org_snapshot import (
    IntegrityProblem,
    IntegrityReport,
    SnapshotBatch,
    SnapshotIntegrityError,
)


class RecordingAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, object]]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, dict(fields)))

    def actions(self) -> list[str]:
        return [action for action, _ in self.records]


class FakeStore:
    """记录每一次 ``commit_batch`` 调用；不做任何真实持久化。"""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self._raises = raises
        self.committed: list[SnapshotBatch] = []

    def commit_batch(self, batch, *, source_app_id, run_id=None, started_at=None) -> str:
        if self._raises is not None:
            raise self._raises
        self.committed.append(batch)
        return "orgsync_fixed"


EMPTY_BATCH = SnapshotBatch(tenants=(), departments=(), members=())
FIXED_NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


def integrity_error() -> SnapshotIntegrityError:
    report = IntegrityReport(
        problems=(IntegrityProblem.NO_TENANT,), tenant_count=0, department_count=0, member_count=0
    )
    return SnapshotIntegrityError(report)


class DailyGateTest(unittest.TestCase):
    def test_a_second_call_the_same_day_does_nothing(self) -> None:
        store = FakeStore()
        audit = RecordingAudit()
        duty = OrgSnapshotSyncDuty(
            read_snapshot=lambda: _committable_batch(),
            store=store,
            audit=audit,
            source_app_id="cli_test",
            clock=lambda: FIXED_NOW,
        )

        first = duty.run_once()
        second = duty.run_once()

        self.assertEqual(first, "orgsync_fixed")
        self.assertIsNone(second, "同一天第二次调用不该再跑一轮")
        self.assertEqual(len(store.committed), 1)

    def test_a_call_on_a_later_day_runs_again(self) -> None:
        store = FakeStore()
        audit = RecordingAudit()
        clock = {"now": FIXED_NOW}
        duty = OrgSnapshotSyncDuty(
            read_snapshot=lambda: _committable_batch(),
            store=store,
            audit=audit,
            source_app_id="cli_test",
            clock=lambda: clock["now"],
        )

        duty.run_once()
        clock["now"] = FIXED_NOW.replace(day=20)
        second = duty.run_once()

        self.assertEqual(second, "orgsync_fixed")
        self.assertEqual(len(store.committed), 2)

    def test_a_stopped_duty_does_nothing(self) -> None:
        stop = threading.Event()
        stop.set()
        store = FakeStore()
        duty = OrgSnapshotSyncDuty(
            read_snapshot=lambda: _committable_batch(),
            store=store,
            audit=RecordingAudit(),
            source_app_id="cli_test",
            clock=lambda: FIXED_NOW,
            stop=stop,
        )

        self.assertIsNone(duty.run_once())
        self.assertEqual(store.committed, [])


class BaselineProtectionTest(unittest.TestCase):
    """**空源 / 半页 / 超时 / 格式异常不得替换基线**（变异锚点）。

    三种"这一轮不可信"的来源（读取阶段异常、完整性校验不通过、写库阶段异常）都必须
    让 ``store.commit_batch`` 要么完全不被真正提交（读取阶段异常，压根不会被调用），
    要么被调用但由 ``SnapshotIntegrityError`` 中止（下一层已经保证"校验不通过就一行都
    不提交"，见 ``PostgresOrgSnapshotStore.commit_batch`` 的真库用例）——本职责这一侧
    要保证的是：三种情况下都不产生"看起来成功"的假象（不置位当日水位、不记
    ``committed`` 审计），让下一轮能够重试。
    """

    def test_a_read_failure_never_reaches_the_store(self) -> None:
        store = FakeStore()
        audit = RecordingAudit()

        def failing_read() -> SnapshotBatch:
            raise RuntimeError("transport_error")

        duty = OrgSnapshotSyncDuty(
            read_snapshot=failing_read, store=store, audit=audit, source_app_id="cli_test", clock=lambda: FIXED_NOW
        )

        result = duty.run_once()

        self.assertIsNone(result)
        self.assertEqual(store.committed, [], "读取失败时绝不能调用 commit_batch")
        self.assertEqual(audit.actions(), ["org_snapshot_sync.read_failed"])
        self.assertIsNone(duty.completed_on, "失败的一轮不得置位当日水位，下一轮必须重试")

    def test_an_integrity_rejection_leaves_no_committed_trace_and_allows_a_retry(self) -> None:
        store = FakeStore(raises=integrity_error())
        audit = RecordingAudit()
        duty = OrgSnapshotSyncDuty(
            read_snapshot=lambda: EMPTY_BATCH,
            store=store,
            audit=audit,
            source_app_id="cli_test",
            clock=lambda: FIXED_NOW,
        )

        result = duty.run_once()

        self.assertIsNone(result)
        self.assertEqual(store.committed, [])
        self.assertEqual(audit.actions(), ["org_snapshot_sync.integrity_rejected"])
        self.assertIn("no_tenant", audit.records[0][1]["problems"])
        self.assertIsNone(duty.completed_on, "完整性校验不通过时不得置位当日水位")

        # 下一轮（哪怕仍是同一天，因为水位没被置位）必须能够重试。
        again = duty.run_once()
        self.assertIsNone(again)
        self.assertEqual(len(audit.records), 2, "每次失败都各留一条审计，不吞第二次")

    def test_a_successful_round_is_the_only_path_that_marks_committed(self) -> None:
        """正向对照：只有真正提交成功才记 ``committed`` 并置位水位——这是变异锚点的
        另一半，证明"变红"确实能发生（把 store 换成会抛错的，上面两个用例已覆盖）。"""

        store = FakeStore()
        audit = RecordingAudit()
        duty = OrgSnapshotSyncDuty(
            read_snapshot=lambda: _committable_batch(),
            store=store,
            audit=audit,
            source_app_id="cli_test",
            clock=lambda: FIXED_NOW,
        )

        result = duty.run_once()

        self.assertEqual(result, "orgsync_fixed")
        self.assertEqual(len(store.committed), 1)
        self.assertEqual(audit.actions(), ["org_snapshot_sync.committed"])
        self.assertEqual(duty.completed_on, FIXED_NOW.date())

    def test_an_unexpected_commit_failure_is_audited_and_re_raised(self) -> None:
        """写库阶段的非预期异常（连接失败等）不得被吞掉、也不能置位水位。"""

        store = FakeStore(raises=RuntimeError("connection_lost"))
        audit = RecordingAudit()
        duty = OrgSnapshotSyncDuty(
            read_snapshot=lambda: _committable_batch(),
            store=store,
            audit=audit,
            source_app_id="cli_test",
            clock=lambda: FIXED_NOW,
        )

        with self.assertRaises(RuntimeError):
            duty.run_once()

        self.assertEqual(audit.actions(), ["org_snapshot_sync.commit_failed"])
        self.assertIsNone(duty.completed_on)


class ConstructionTest(unittest.TestCase):
    def test_an_empty_source_app_id_is_refused_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            OrgSnapshotSyncDuty(
                read_snapshot=lambda: EMPTY_BATCH, store=FakeStore(), audit=RecordingAudit(), source_app_id=""
            )


def _committable_batch() -> SnapshotBatch:
    from lingxi.core.identity.org_snapshot import SnapshotMember, TenantScope

    member = SnapshotMember(
        tenant_key="tenant_a",
        member_key="ou_1",
        open_id="ou_1",
        user_id="u_1",
        union_id="on_1",
        display_name="张一",
    )
    scope = TenantScope(
        tenant_key="tenant_a",
        visible_to_user_identity=True,
        app_member_keys=frozenset({"ou_1"}),
        user_member_keys=frozenset({"ou_1"}),
    )
    return SnapshotBatch(tenants=(scope,), departments=(), members=(member,))


if __name__ == "__main__":
    unittest.main()
