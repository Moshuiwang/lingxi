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

import inspect
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta

from lingxi.apps.scheduler import org_snapshot_sync as org_snapshot_sync_module
from lingxi.apps.scheduler.org_snapshot_sync import (
    CONSECUTIVE_ROUND_BUDGET_EXCEEDED_ESCALATION_THRESHOLD,
    READ_FAILURE_BACKOFF_CEILING_SECONDS,
    READ_FAILURE_BACKOFF_STEP_SECONDS,
    ROUND_THREAD_STUCK_AFTER_SECONDS,
    OrgSnapshotSyncDuty,
)
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

    def __init__(
        self,
        *,
        raises: Exception | None = None,
        already_complete_today: bool = False,
        watermark_check_raises: Exception | None = None,
    ) -> None:
        self._raises = raises
        self._already_complete_today = already_complete_today
        self._watermark_check_raises = watermark_check_raises
        self.committed: list[SnapshotBatch] = []
        self.watermark_checks = 0

    def commit_batch(self, batch, *, source_app_id, run_id=None, started_at=None) -> str:
        if self._raises is not None:
            raise self._raises
        self.committed.append(batch)
        return "orgsync_fixed"

    def has_complete_run_on(self, day) -> bool:
        self.watermark_checks += 1
        if self._watermark_check_raises is not None:
            raise self._watermark_check_raises
        return self._already_complete_today


EMPTY_BATCH = SnapshotBatch(tenants=(), departments=(), members=())
FIXED_NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


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
        clock = {"now": FIXED_NOW}
        duty = OrgSnapshotSyncDuty(
            read_snapshot=lambda: EMPTY_BATCH,
            store=store,
            audit=audit,
            source_app_id="cli_test",
            clock=lambda: clock["now"],
        )

        result = duty.run_once()

        self.assertIsNone(result)
        self.assertEqual(store.committed, [])
        self.assertEqual(audit.actions(), ["org_snapshot_sync.integrity_rejected"])
        self.assertIn("no_tenant", audit.records[0][1]["problems"])
        self.assertIsNone(duty.completed_on, "完整性校验不通过时不得置位当日水位")

        # 完整性校验失败也推进退避（Issue #268 F3，与读取失败共用同一条）：同一时刻
        # 立即再跑一轮不会真的重试，见 BackoffTest 的专门用例；这里只证明"水位没被
        # 置位、退避窗口过后确实能够重试"这条纪律仍然成立。
        clock["now"] = FIXED_NOW + timedelta(seconds=audit.records[0][1]["backoff_seconds"])
        again = duty.run_once()
        self.assertIsNone(again)
        self.assertEqual(len(audit.records), 2, "退避窗口过后每次失败都各留一条审计，不吞第二次")

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

    def test_an_unexpected_commit_failure_is_audited_and_leaves_no_committed_trace(self) -> None:
        """写库阶段的非预期异常（连接失败等）不得被吞掉、也不能置位水位。

        Issue #340 后，「跑一轮」整体派进后台线程（见
        ``org_snapshot_sync.py`` 模块文档「为什么整轮要挪出主循环线程」）：
        `_run_round` 内部的 ``raise`` 不再能穿透线程边界同步冒泡回
        ``run_once()`` 的调用方——它被 `run_once` 派发的 `worker()` 接住、
        记同形状日志（``T8``），调用方这次改成同步拿到 ``None``（不是异常）。
        审计与水位两条断言不受影响：无论异常在哪条线程里发生，`commit_failed`
        审计与"不置位水位"这两条产品语义必须逐字不变。"""

        store = FakeStore(raises=RuntimeError("connection_lost"))
        audit = RecordingAudit()
        duty = OrgSnapshotSyncDuty(
            read_snapshot=lambda: _committable_batch(),
            store=store,
            audit=audit,
            source_app_id="cli_test",
            clock=lambda: FIXED_NOW,
        )

        result = duty.run_once()

        self.assertIsNone(result, "写库失败的一轮不再同步抛出异常，而是同步拿到 None")
        self.assertEqual(audit.actions(), ["org_snapshot_sync.commit_failed"])
        self.assertIsNone(duty.completed_on)


class PersistedWatermarkTest(unittest.TestCase):
    """当日水位对进程重启保持（Issue #250 F8）：新建的 duty 实例（模拟重启后的
    内存归零）在库里已有今天的 complete 批次时不得再跑一轮；查询本身失败时按
    未知处理、不阻塞今天的同步。"""

    def test_a_freshly_constructed_duty_skips_a_day_already_completed_in_storage(self) -> None:
        store = FakeStore(already_complete_today=True)
        audit = RecordingAudit()
        duty = OrgSnapshotSyncDuty(
            read_snapshot=lambda: _committable_batch(),
            store=store,
            audit=audit,
            source_app_id="cli_test",
            clock=lambda: FIXED_NOW,
        )
        self.assertIsNone(duty.completed_on, "模拟进程重启：内存水位归零")

        result = duty.run_once()

        self.assertIsNone(result)
        self.assertEqual(store.committed, [], "库里已有今天的完成批次，不该再扫一轮")
        self.assertEqual(duty.completed_on, FIXED_NOW.date(), "读到持久化水位后本进程也该记住")
        self.assertEqual(audit.actions(), ["org_snapshot_sync.already_completed_today"])

    def test_the_persisted_watermark_is_checked_at_most_once_per_day(self) -> None:
        store = FakeStore(already_complete_today=False)
        duty = OrgSnapshotSyncDuty(
            read_snapshot=lambda: _committable_batch(),
            store=store,
            audit=RecordingAudit(),
            source_app_id="cli_test",
            clock=lambda: FIXED_NOW,
        )

        duty.run_once()  # 第一轮真的跑成功，水位已经在内存里
        store._already_complete_today = True  # 之后即便库里状态变化也不该再被问到
        second = duty.run_once()

        self.assertIsNone(second, "同一天第二次调用被内存水位挡住，不该再查一次库")
        self.assertEqual(store.watermark_checks, 1)

    def test_a_watermark_check_failure_does_not_block_todays_sync(self) -> None:
        store = FakeStore(watermark_check_raises=RuntimeError("connection_lost"))
        audit = RecordingAudit()
        duty = OrgSnapshotSyncDuty(
            read_snapshot=lambda: _committable_batch(),
            store=store,
            audit=audit,
            source_app_id="cli_test",
            clock=lambda: FIXED_NOW,
        )

        result = duty.run_once()

        self.assertEqual(result, "orgsync_fixed", "水位查询本身失败不得阻塞今天的同步")
        self.assertEqual(len(store.committed), 1)
        self.assertEqual(
            audit.actions(),
            ["org_snapshot_sync.watermark_check_failed", "org_snapshot_sync.committed"],
        )


class FakeFeishuDirectoryError(RuntimeError):
    """本地假替身，形状照 ``adapters/feishu_directory.py`` 的
    ``FeishuDirectoryError``（``code`` 属性 + 不含凭据的消息），但刻意不 import
    真实类——``org_snapshot_sync.py`` 靠鸭子类型识别 ``.code``，不依赖具体类型，
    测试同样只依赖这个约定，不依赖 adapters 层。"""

    def __init__(self, code: str) -> None:
        super().__init__(f"飞书目录接口调用失败：{code}——这句消息正文不该出现在审计里")
        self.code = code


class ReadFailedAuditTest(unittest.TestCase):
    """Issue #268 F1：``FeishuDirectoryError.code``（安全分类标签）必须进审计与
    日志，不能只剩下没有诊断价值的异常类名；没有 ``code`` 的其他异常仍按类名记。"""

    def test_a_coded_error_puts_its_code_in_the_audit(self) -> None:
        store = FakeStore()
        audit = RecordingAudit()

        def failing_read() -> SnapshotBatch:
            raise FakeFeishuDirectoryError("missing_target_tenant_list")

        duty = OrgSnapshotSyncDuty(
            read_snapshot=failing_read, store=store, audit=audit, source_app_id="cli_test", clock=lambda: FIXED_NOW
        )

        duty.run_once()

        self.assertEqual(audit.actions(), ["org_snapshot_sync.read_failed"])
        fields = audit.records[0][1]
        self.assertEqual(fields["code"], "missing_target_tenant_list")
        self.assertEqual(fields["error"], "FakeFeishuDirectoryError")
        # 消息正文（含"这句消息正文不该出现在审计里"）不得泄漏进任何字段。
        for value in fields.values():
            self.assertNotIn("这句消息正文不该出现在审计里", str(value))

    def test_round_budget_exceeded_lands_in_the_ordinary_read_failed_path(self) -> None:
        """否定断言（设计 §5 #2，二级独立审查修复的一部分）：`round_budget`
        撞线时 `adapters/feishu_directory.py::_PagedClient._request` 抛出的
        `FeishuDirectoryError("round_budget_exceeded")` 不需要、也不应该在本模块
        另开一条处理分支——它必须经 `run_once()` 落进已有的 `read_failed` 审计
        （`code=round_budget_exceeded`），像任何其他读取失败一样推进退避，且
        **不置位当日水位**（不是"提前完成的成功一轮"）。这里用本文件既有的
        ``FakeFeishuDirectoryError`` 假替身构造同样的 ``code``，不依赖 adapters
        层，只验证本模块这一侧对"任意带 code 的异常"一视同仁的既有纪律确实
        覆盖了这一个具体的 code 值。"""

        store = FakeStore()
        audit = RecordingAudit()

        def failing_read() -> SnapshotBatch:
            raise FakeFeishuDirectoryError("round_budget_exceeded")

        duty = OrgSnapshotSyncDuty(
            read_snapshot=failing_read, store=store, audit=audit, source_app_id="cli_test", clock=lambda: FIXED_NOW
        )

        result = duty.run_once()

        self.assertIsNone(result)
        self.assertEqual(store.committed, [], "撞预算的一轮绝不能调用 commit_batch")
        self.assertEqual(audit.actions(), ["org_snapshot_sync.read_failed"])
        fields = audit.records[0][1]
        self.assertEqual(fields["code"], "round_budget_exceeded")
        self.assertEqual(fields["attempt"], 1, "撞预算也要推进退避——与其他读取失败共用同一条状态")
        self.assertEqual(fields["backoff_seconds"], READ_FAILURE_BACKOFF_STEP_SECONDS)
        self.assertIsNone(duty.completed_on, "撞预算的一轮不得置位当日水位，下一轮必须重试")

    def test_a_plain_exception_without_a_code_still_falls_back_to_the_class_name(self) -> None:
        """变异锚点：把上面那条用例的输入换成没有 ``code`` 属性的普通异常，审计
        必须不出现 ``code`` 字段，只剩 ``error=<类名>``（F1 完成标准的另一半）。"""

        store = FakeStore()
        audit = RecordingAudit()

        def failing_read() -> SnapshotBatch:
            raise RuntimeError("transport_error")

        duty = OrgSnapshotSyncDuty(
            read_snapshot=failing_read, store=store, audit=audit, source_app_id="cli_test", clock=lambda: FIXED_NOW
        )

        duty.run_once()

        fields = audit.records[0][1]
        self.assertEqual(fields["error"], "RuntimeError")
        self.assertNotIn("code", fields, "没有 code 属性的异常不得凭空造出一个 code 字段")

    def test_the_token_supply_classification_is_unaffected(self) -> None:
        """``TokenSupplyFailure.supply`` 这条既有分类（F6）必须原样保留，不被 F1
        的 code 逻辑挤掉或覆盖。"""

        from lingxi.apps.scheduler.org_snapshot_sync import TokenSupplyFailure

        store = FakeStore()
        audit = RecordingAudit()

        def failing_read() -> SnapshotBatch:
            raise TokenSupplyFailure("app_access_token")

        duty = OrgSnapshotSyncDuty(
            read_snapshot=failing_read, store=store, audit=audit, source_app_id="cli_test", clock=lambda: FIXED_NOW
        )

        duty.run_once()

        fields = audit.records[0][1]
        self.assertEqual(fields["supply"], "app_access_token")
        self.assertNotIn("code", fields)

    def test_a_token_supply_failures_cause_code_is_recovered(self) -> None:
        """应修 F（独立审查 2026-08-20 可选建议）：``TokenSupplyFailure`` 本身没有
        ``.code``，但它是 ``raise TokenSupplyFailure(...) from error`` 包出来的——
        底层 ``error``（例如 ``FeishuTenantTokenError``）若带 ``code``，应当能透过
        ``__cause__`` 追到，不能让令牌供给失败在审计里只剩 ``supply``、丢了具体
        原因。变异锚点：把 `__cause__` 那段追溯删掉，本用例会从"code 出现"变红成
        "code 缺失"。"""

        from lingxi.apps.scheduler.org_snapshot_sync import TokenSupplyFailure

        store = FakeStore()
        audit = RecordingAudit()

        def failing_read() -> SnapshotBatch:
            try:
                raise FakeFeishuDirectoryError("feishu_code_99991663")
            except FakeFeishuDirectoryError as cause:
                raise TokenSupplyFailure("app_access_token") from cause

        duty = OrgSnapshotSyncDuty(
            read_snapshot=failing_read, store=store, audit=audit, source_app_id="cli_test", clock=lambda: FIXED_NOW
        )

        duty.run_once()

        fields = audit.records[0][1]
        self.assertEqual(fields["supply"], "app_access_token")
        self.assertEqual(fields["code"], "feishu_code_99991663")

    def test_an_unsafe_cause_code_is_dropped_not_sanitized_into_the_audit(self) -> None:
        """必修 C 的第二道防线：`__cause__.code` 来自本模块不掌控构造的异常类
        （例如 `adapters/feishu_tenant_token.py::FeishuTenantTokenError`，它还没
        有做同等的来源收紧），审计写入前必须过白名单——不匹配就整个不写这个
        字段，不做"部分保留"的净化；日志行读的是同一个已过滤的值，不能绕开审计
        单独在日志里泄漏（这是审查实测过的注入手法：`{"code": "0 action=
        org_snapshot_sync.committed run_id=forged tenants=8"}`）。变异锚点：把
        `logger.error` 那行的 `fields.get("code", "unknown")` 改回直接用未过滤的
        `code` 变量，本用例的日志断言会变红。"""

        from lingxi.apps.scheduler.org_snapshot_sync import TokenSupplyFailure

        store = FakeStore()
        audit = RecordingAudit()
        forged = "0 action=org_snapshot_sync.committed run_id=forged tenants=8"

        def failing_read() -> SnapshotBatch:
            try:
                raise FakeFeishuDirectoryError(forged)
            except FakeFeishuDirectoryError as cause:
                raise TokenSupplyFailure("app_access_token") from cause

        duty = OrgSnapshotSyncDuty(
            read_snapshot=failing_read, store=store, audit=audit, source_app_id="cli_test", clock=lambda: FIXED_NOW
        )

        with self.assertLogs("lingxi.apps.scheduler.org_snapshot_sync", level="ERROR") as captured:
            duty.run_once()

        fields = audit.records[0][1]
        self.assertEqual(fields["supply"], "app_access_token")
        self.assertNotIn("code", fields, "不安全字符集的 code 必须整个丢弃，不能截断或替换后仍然写入")
        for line in captured.output:
            self.assertNotIn(forged, line, "未过滤的 code 不得原样出现在日志行里")
            self.assertNotIn("action=org_snapshot_sync.committed", line)


class BackoffTest(unittest.TestCase):
    """Issue #268 F3：读取失败（与完整性校验失败）后不得每个 tick 立即重试。stage
    实测每 30 秒重试一轮、连续失败数十轮，一天约 2880 次无效外部调用；这里用一个
    计数的假读取函数直接证明"退避窗口内确实没有再发起读取"，不只是看审计条数。
    """

    def test_a_second_call_within_the_backoff_window_does_not_invoke_read_again(self) -> None:
        store = FakeStore()
        audit = RecordingAudit()
        call_count = {"n": 0}

        def failing_read() -> SnapshotBatch:
            call_count["n"] += 1
            raise RuntimeError("transport_error")

        duty = OrgSnapshotSyncDuty(
            read_snapshot=failing_read, store=store, audit=audit, source_app_id="cli_test", clock=lambda: FIXED_NOW
        )

        duty.run_once()
        again = duty.run_once()  # 同一时刻立即再跑一轮（模拟下一个 30 秒 tick）

        self.assertIsNone(again)
        self.assertEqual(call_count["n"], 1, "退避窗口内不得再发起外部读取")
        self.assertEqual(len(audit.records), 1, "退避窗口内跳过的一轮不留审计，不制造噪音")

    def test_a_round_longer_than_the_backoff_still_lands_inside_the_window(self) -> None:
        """**冻结候选审查 2026-08-21 的 F4**：整轮耗时超过退避档位时，下一 tick 仍然
        必须在退避窗口内。

        stage 2026-08-21 实测一轮全量扫描约 **345 秒**，而第一档退避是 300 秒。旧实现
        把 ``run_once`` 顶部取的**轮开始**时刻当退避基准，于是失败发生（轮开始 + 345
        秒）时 ``_next_attempt_at``（轮开始 + 300 秒）已经在过去——下一 tick 的退避门禁
        立刻放行、整轮数百次外部调用照发，而审计里明明白白写着
        ``backoff_seconds=300``。退避档位越靠前、轮越慢，这个洞越大。

        这里用注入时钟精确复现：假读取函数在抛异常**之前**把时钟推进 345 秒。

        变异验红：把 `_advance_backoff` 里的 ``self._clock()`` 换回 ``run_once`` 顶部
        的轮开始时刻（改动前那一版）之后重跑本用例，第二轮会真的再发起一次读取，
        ``call_count["n"]`` 变成 2、``assertEqual(..., 1)`` 失败。
        """

        store = FakeStore()
        audit = RecordingAudit()
        call_count = {"n": 0}
        clock = {"now": FIXED_NOW}
        # stage 2026-08-21 实测的全量轮耗时，刻意大于第一档退避（300 秒）。
        round_duration = timedelta(seconds=345)
        self.assertGreater(
            round_duration.total_seconds(),
            READ_FAILURE_BACKOFF_STEP_SECONDS,
            "用例前提：整轮耗时必须真的超过第一档退避，否则测不出这个洞",
        )

        def slow_failing_read() -> SnapshotBatch:
            call_count["n"] += 1
            clock["now"] = clock["now"] + round_duration  # 整轮真的花了这么久
            raise RuntimeError("transport_error")

        duty = OrgSnapshotSyncDuty(
            read_snapshot=slow_failing_read,
            store=store,
            audit=audit,
            source_app_id="cli_test",
            clock=lambda: clock["now"],
        )

        duty.run_once()
        self.assertEqual(audit.records[0][1]["backoff_seconds"], READ_FAILURE_BACKOFF_STEP_SECONDS)

        # 下一个 tick：从失败那一刻起才过了 30 秒，远没到 300 秒。
        clock["now"] = clock["now"] + timedelta(seconds=30)
        again = duty.run_once()

        self.assertIsNone(again)
        self.assertEqual(
            call_count["n"],
            1,
            "退避基准必须是失败发生的时刻——否则审计写着退避 300 秒，实际一秒都没退",
        )
        self.assertEqual(len(audit.records), 1, "退避窗口内跳过的一轮不留审计")

        # 从失败那一刻起真的过了 300 秒之后，才允许重试。
        clock["now"] = FIXED_NOW + round_duration + timedelta(
            seconds=READ_FAILURE_BACKOFF_STEP_SECONDS
        )
        duty.run_once()
        self.assertEqual(call_count["n"], 2, "退避窗口过后必须真的重试")

    def test_a_retry_happens_once_the_backoff_window_has_elapsed(self) -> None:
        store = FakeStore()
        audit = RecordingAudit()
        call_count = {"n": 0}
        clock = {"now": FIXED_NOW}

        def failing_read() -> SnapshotBatch:
            call_count["n"] += 1
            raise RuntimeError("transport_error")

        duty = OrgSnapshotSyncDuty(
            read_snapshot=failing_read, store=store, audit=audit, source_app_id="cli_test", clock=lambda: clock["now"]
        )

        duty.run_once()
        self.assertEqual(audit.records[0][1]["attempt"], 1)
        self.assertEqual(audit.records[0][1]["backoff_seconds"], READ_FAILURE_BACKOFF_STEP_SECONDS)

        clock["now"] = FIXED_NOW + timedelta(seconds=READ_FAILURE_BACKOFF_STEP_SECONDS)
        duty.run_once()

        self.assertEqual(call_count["n"], 2, "退避窗口过后必须真的重试")
        self.assertEqual(len(audit.records), 2)

    def test_consecutive_failures_grow_the_backoff_linearly_up_to_the_ceiling(self) -> None:
        store = FakeStore()
        audit = RecordingAudit()
        clock = {"now": FIXED_NOW}

        def failing_read() -> SnapshotBatch:
            raise RuntimeError("transport_error")

        duty = OrgSnapshotSyncDuty(
            read_snapshot=failing_read, store=store, audit=audit, source_app_id="cli_test", clock=lambda: clock["now"]
        )

        # 12 次连续失败恰好到达封顶（12 * 300 = 3600）；多跑两次证明封顶之后
        # 真的停在原地，不是"算出来正好等于封顶"的巧合。
        attempts = 14
        observed_backoffs: list[int] = []
        for _ in range(attempts):
            duty.run_once()
            backoff = audit.records[-1][1]["backoff_seconds"]
            observed_backoffs.append(backoff)
            clock["now"] = clock["now"] + timedelta(seconds=backoff)

        expected = [
            min((index + 1) * READ_FAILURE_BACKOFF_STEP_SECONDS, READ_FAILURE_BACKOFF_CEILING_SECONDS)
            for index in range(attempts)
        ]
        self.assertEqual(observed_backoffs, expected)
        self.assertEqual(observed_backoffs[-3:], [READ_FAILURE_BACKOFF_CEILING_SECONDS] * 3, "必须真的封顶，不能无限增长")

    def test_a_successful_commit_resets_the_backoff_streak(self) -> None:
        store = FakeStore()
        audit = RecordingAudit()
        clock = {"now": FIXED_NOW}
        should_fail = {"value": True}

        def flaky_read() -> SnapshotBatch:
            if should_fail["value"]:
                raise RuntimeError("transport_error")
            return _committable_batch()

        duty = OrgSnapshotSyncDuty(
            read_snapshot=flaky_read, store=store, audit=audit, source_app_id="cli_test", clock=lambda: clock["now"]
        )

        duty.run_once()  # 第 1 次失败，streak=1，退避 300 秒
        clock["now"] = clock["now"] + timedelta(seconds=audit.records[-1][1]["backoff_seconds"])
        duty.run_once()  # 第 2 次失败，streak=2，退避 600 秒
        self.assertEqual(audit.records[-1][1]["attempt"], 2)

        clock["now"] = clock["now"] + timedelta(seconds=audit.records[-1][1]["backoff_seconds"])
        should_fail["value"] = False
        result = duty.run_once()  # 成功提交，退避应清零
        self.assertEqual(result, "orgsync_fixed")

        # 下一天再失败一次：streak 必须从 1 重新开始，不是接着上次的 2 继续涨。
        # 用 `timedelta(days=1)` 而不是 `replace(day=day + 1)`——后者在月末会让
        # `day` 超出当月天数，抛 `ValueError`（应修 F，独立审查 2026-08-20）。
        clock["now"] = clock["now"] + timedelta(days=1)
        should_fail["value"] = True
        duty.run_once()
        self.assertEqual(audit.records[-1][1]["attempt"], 1, "成功一轮之后退避必须清零，不能带着旧的失败计数")
        self.assertEqual(audit.records[-1][1]["backoff_seconds"], READ_FAILURE_BACKOFF_STEP_SECONDS)

    def test_integrity_rejection_shares_the_same_backoff_as_read_failure(self) -> None:
        """完整性校验失败也推进退避（与读取失败共用同一条状态），不是只挡读取失败
        这一条路径——两条路径合起来才是"失败也推进退避"的完整覆盖。"""

        store = FakeStore(raises=integrity_error())
        audit = RecordingAudit()
        clock = {"now": FIXED_NOW}
        duty = OrgSnapshotSyncDuty(
            read_snapshot=lambda: EMPTY_BATCH, store=store, audit=audit, source_app_id="cli_test", clock=lambda: clock["now"]
        )

        duty.run_once()
        again = duty.run_once()  # 同一时刻立即再跑一轮

        self.assertIsNone(again)
        self.assertEqual(len(audit.records), 1, "退避窗口内跳过的一轮不留审计")
        self.assertEqual(audit.records[0][1]["backoff_seconds"], READ_FAILURE_BACKOFF_STEP_SECONDS)

    def test_a_commit_failure_also_backs_off_and_blocks_an_immediate_retry(self) -> None:
        """独立审查必修 D：写库失败（`commit_failed`）与读取失败、完整性校验失败
        处在流水线同一个位置——都要先跑完一轮昂贵的外部读取才会撞上——因此也要
        推进同一条退避。**`raise` 本身在 `_run_round` 内部仍然不变**（Issue #340
        后由 `run_once` 派发的后台线程接住、记同形状日志，不再穿透线程边界同步
        冒泡到 `run_once()` 调用方，见 `BaselineProtectionTest` 同一场景的
        专门用例）——这里既要证明状态已经在冒泡前记好，又要证明下一次调用会被
        退避挡住。变异锚点：把 `self._advance_backoff()` 从 `commit_failed`
        分支删掉，本用例的 `call_count` 断言会从 1 变红成 2（且
        `backoff_seconds`/`attempt` 字段会从审计里消失）。"""

        call_count = {"n": 0}

        def counting_read() -> SnapshotBatch:
            call_count["n"] += 1
            return _committable_batch()

        store = FakeStore(raises=RuntimeError("connection_lost"))
        audit = RecordingAudit()
        duty = OrgSnapshotSyncDuty(
            read_snapshot=counting_read, store=store, audit=audit, source_app_id="cli_test", clock=lambda: FIXED_NOW
        )

        result = duty.run_once()

        self.assertIsNone(result)
        self.assertEqual(audit.actions(), ["org_snapshot_sync.commit_failed"])
        fields = audit.records[0][1]
        self.assertEqual(fields["attempt"], 1)
        self.assertEqual(fields["backoff_seconds"], READ_FAILURE_BACKOFF_STEP_SECONDS)

        # 同一时刻立即再跑一轮：退避窗口内不得再发起读取（哪怕上一次是靠异常
        # 冒泡结束的，不是靠 `return None`）。
        again = duty.run_once()
        self.assertIsNone(again)
        self.assertEqual(call_count["n"], 1, "退避窗口内不得再发起外部读取")
        self.assertEqual(len(audit.records), 1, "退避窗口内跳过的一轮不留审计")


class ConstructionTest(unittest.TestCase):
    def test_an_empty_source_app_id_is_refused_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            OrgSnapshotSyncDuty(
                read_snapshot=lambda: EMPTY_BATCH, store=FakeStore(), audit=RecordingAudit(), source_app_id=""
            )


class RoundBudgetEscalationTest(unittest.TestCase):
    """独立审查二轮 P2-B4：单次 `round_budget_exceeded` 只是普通的 `read_failed`，
    靠既有退避静默自愈即可；但如果它**连续**出现，说明的不是一次偶发拥堵，而是
    `LINGXI_ORG_SNAPSHOT_ROUND_BUDGET_SECONDS` 配的预算本身小于真实一轮耗时——
    每一轮都会在完全相同的地方撞线，而失败路径「保留上一份、不覆盖」的语义会让
    快照静默地永远停在旧数据上。这里断言连续撞线达到阈值时必须额外升一条响亮的
    专用审计 action，打破这个静默活锁。
    """

    @staticmethod
    def _advance_past_backoff(clock: dict[str, datetime], attempt: int) -> None:
        clock["now"] = clock["now"] + timedelta(
            seconds=min(attempt * READ_FAILURE_BACKOFF_STEP_SECONDS, READ_FAILURE_BACKOFF_CEILING_SECONDS)
        )

    def test_three_consecutive_rounds_trigger_the_escalation(self) -> None:
        store = FakeStore()
        audit = RecordingAudit()
        clock = {"now": FIXED_NOW}

        def failing_read() -> SnapshotBatch:
            raise FakeFeishuDirectoryError("round_budget_exceeded")

        duty = OrgSnapshotSyncDuty(
            read_snapshot=failing_read,
            store=store,
            audit=audit,
            source_app_id="cli_test",
            clock=lambda: clock["now"],
        )

        for attempt in range(1, CONSECUTIVE_ROUND_BUDGET_EXCEEDED_ESCALATION_THRESHOLD + 1):
            duty.run_once()
            self._advance_past_backoff(clock, attempt)

        escalations = [
            fields
            for action, fields in audit.records
            if action == "org_snapshot_sync.round_budget_persistently_exceeded"
        ]
        self.assertEqual(len(escalations), 1, "恰好在连续撞线达到阈值那一轮升级一次")
        self.assertEqual(
            escalations[0]["consecutive"], CONSECUTIVE_ROUND_BUDGET_EXCEEDED_ESCALATION_THRESHOLD
        )
        # 升级是在既有 read_failed 之外**额外**多记一条，不是替换它——运维仍然
        # 能在审计里看到每一轮各自的 code/attempt/backoff_seconds。
        self.assertEqual(
            audit.actions().count("org_snapshot_sync.read_failed"),
            CONSECUTIVE_ROUND_BUDGET_EXCEEDED_ESCALATION_THRESHOLD,
        )

    def test_a_warning_log_accompanies_the_escalation(self) -> None:
        store = FakeStore()
        audit = RecordingAudit()
        clock = {"now": FIXED_NOW}

        def failing_read() -> SnapshotBatch:
            raise FakeFeishuDirectoryError("round_budget_exceeded")

        duty = OrgSnapshotSyncDuty(
            read_snapshot=failing_read,
            store=store,
            audit=audit,
            source_app_id="cli_test",
            clock=lambda: clock["now"],
        )

        with self.assertLogs("lingxi.apps.scheduler.org_snapshot_sync", level="WARNING") as captured:
            for attempt in range(1, CONSECUTIVE_ROUND_BUDGET_EXCEEDED_ESCALATION_THRESHOLD + 1):
                duty.run_once()
                self._advance_past_backoff(clock, attempt)

        self.assertTrue(
            any("预算" in line and "配置错误" in line for line in captured.output),
            "打破静默活锁的日志必须响亮到能直接说明是配置错误，不能只是又一条 error 级别的失败日志",
        )

    def test_fewer_than_the_threshold_does_not_escalate(self) -> None:
        store = FakeStore()
        audit = RecordingAudit()
        clock = {"now": FIXED_NOW}

        def failing_read() -> SnapshotBatch:
            raise FakeFeishuDirectoryError("round_budget_exceeded")

        duty = OrgSnapshotSyncDuty(
            read_snapshot=failing_read,
            store=store,
            audit=audit,
            source_app_id="cli_test",
            clock=lambda: clock["now"],
        )

        for attempt in range(1, CONSECUTIVE_ROUND_BUDGET_EXCEEDED_ESCALATION_THRESHOLD):
            duty.run_once()
            self._advance_past_backoff(clock, attempt)

        self.assertNotIn(
            "org_snapshot_sync.round_budget_persistently_exceeded", audit.actions()
        )

    def test_an_interleaved_different_failure_resets_the_streak(self) -> None:
        """连续计数只统计"恰好都是撞预算"的连续轮次；中间夹一次别的失败原因
        （例如一次纯网络抖动）不是"预算持续不够"的证据，不能被继续计入。"""

        store = FakeStore()
        audit = RecordingAudit()
        clock = {"now": FIXED_NOW}
        codes = iter(
            [
                "round_budget_exceeded",
                "round_budget_exceeded",
                "transport_error",
                "round_budget_exceeded",
                "round_budget_exceeded",
            ]
        )

        def failing_read() -> SnapshotBatch:
            code = next(codes)
            if code == "transport_error":
                raise RuntimeError("transport_error")
            raise FakeFeishuDirectoryError(code)

        duty = OrgSnapshotSyncDuty(
            read_snapshot=failing_read,
            store=store,
            audit=audit,
            source_app_id="cli_test",
            clock=lambda: clock["now"],
        )

        for attempt in range(1, 6):
            duty.run_once()
            self._advance_past_backoff(clock, attempt)

        self.assertNotIn(
            "org_snapshot_sync.round_budget_persistently_exceeded",
            audit.actions(),
            "五轮里最长的连续撞预算只有两轮（中间夹了一次 transport_error），不该升级",
        )

    def test_a_successful_commit_resets_the_streak(self) -> None:
        """连续两轮撞预算之后，第三轮真的读成功并提交：这是"预算其实够用、
        只是刚好偶发拥堵了两次"的证据，之后若再度连续撞线，必须重新数，不能
        沿用之前累积的计数。"""

        store = FakeStore()
        audit = RecordingAudit()
        clock = {"now": FIXED_NOW}
        outcomes = iter(
            ["round_budget_exceeded", "round_budget_exceeded", None, "round_budget_exceeded"]
        )

        def read_or_succeed() -> SnapshotBatch:
            outcome = next(outcomes)
            if outcome is None:
                return _committable_batch()
            raise FakeFeishuDirectoryError(outcome)

        duty = OrgSnapshotSyncDuty(
            read_snapshot=read_or_succeed,
            store=store,
            audit=audit,
            source_app_id="cli_test",
            clock=lambda: clock["now"],
        )

        duty.run_once()  # 撞预算 #1
        clock["now"] = clock["now"] + timedelta(seconds=READ_FAILURE_BACKOFF_STEP_SECONDS)
        duty.run_once()  # 撞预算 #2
        clock["now"] = clock["now"] + timedelta(seconds=2 * READ_FAILURE_BACKOFF_STEP_SECONDS)
        duty.run_once()  # 成功提交
        clock["now"] = clock["now"].replace(day=clock["now"].day + 1)  # 下一 UTC 日，daily gate 放行
        duty.run_once()  # 撞预算 #1'（重新数）

        self.assertNotIn(
            "org_snapshot_sync.round_budget_persistently_exceeded",
            audit.actions(),
            "中间的成功提交必须清零连续计数，否则第四轮会被误判成第三轮连续撞线",
        )


def _poll_until(predicate, *, timeout: float = 5.0, interval: float = 0.02) -> bool:
    """轮询直到 ``predicate()`` 为真或超时，返回是否等到。不引入除有界轮询以外
    的额外同步原语，测试范围内可接受（同 ``tests/test_daily_report_duty.py`` 的
    ``_wait_for_completion`` 纪律：后台线程完成的确切时刻本来就不可预知，事件
    通知需要被测代码额外配合，轮询是测试这一侧最简单可靠的做法）。"""

    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)
    return True


class ThreadedRoundTests(unittest.TestCase):
    """Issue #340：「跑一轮」（``_run_round``）整体派进后台线程，``run_once``
    只同步等一个很短的 join 上限（``DEFAULT_ROUND_JOIN_TIMEOUT_SECONDS``）就
    把控制权交还调用方——见 ``org_snapshot_sync.py`` 模块文档「为什么整轮要
    挪出主循环线程」。T1/T2/T3/T6。
    """

    def test_t1_a_slow_round_returns_within_the_join_timeout(self) -> None:
        """T1：慢轮进行中 ``run_once`` 有界时间内返回。

        变异验红 M1：把 ``thread.join(timeout=self._round_join_timeout_seconds)``
        换成裸 ``thread.join()``——本用例会因为 ``run_once()`` 一直卡到闸门放行
        才返回、``elapsed`` 远超断言上限而变红（实测时需要给测试命令本身加一层
        外部超时，防止真的无限期挂起）。
        """

        gate = threading.Event()
        released = threading.Event()

        def blocking_read() -> SnapshotBatch:
            gate.wait(timeout=5.0)
            released.set()
            return _committable_batch()

        store = FakeStore()
        audit = RecordingAudit()
        duty = OrgSnapshotSyncDuty(
            read_snapshot=blocking_read,
            store=store,
            audit=audit,
            source_app_id="cli_test",
            clock=lambda: FIXED_NOW,
            round_join_timeout_seconds=0.05,
        )
        self.addCleanup(gate.set)

        started = time.monotonic()
        result = duty.run_once()
        elapsed = time.monotonic() - started

        self.assertIsNone(result, "慢轮还没收工，调用方本轮拿不到 run_id")
        self.assertLess(elapsed, 1.0, "调用方必须在很短时间内拿回控制权，不等一整轮跑完")
        self.assertFalse(released.is_set(), "调用方返回时，这一轮确实还没跑完（不是恰好跑完）")

        gate.set()
        self.assertTrue(released.wait(timeout=5.0), "放行后后台线程应当很快完成")
        self.assertTrue(_poll_until(lambda: len(store.committed) >= 1))
        self.assertEqual(len(store.committed), 1)

    def test_t2_a_second_call_while_a_round_is_in_flight_does_not_dispatch_twice(
        self,
    ) -> None:
        """T2：连续两次 ``run_once`` 撞同一在飞线程只派一个。

        变异验红 M2：删掉单飞守卫（让 ``run_once`` 无视 ``_pending_thread``
        是否存活、每次调用都派发新线程去读一轮）——本用例的 ``call_count``
        断言会从 1 变红成 2。
        """

        gate = threading.Event()
        call_count = {"n": 0}

        def blocking_read() -> SnapshotBatch:
            call_count["n"] += 1
            gate.wait(timeout=5.0)
            return _committable_batch()

        store = FakeStore()
        duty = OrgSnapshotSyncDuty(
            read_snapshot=blocking_read,
            store=store,
            audit=RecordingAudit(),
            source_app_id="cli_test",
            clock=lambda: FIXED_NOW,
            round_join_timeout_seconds=0.05,
        )
        self.addCleanup(gate.set)

        first = duty.run_once()
        second = duty.run_once()

        self.assertIsNone(first)
        self.assertIsNone(second)
        gate.set()
        self.assertTrue(_poll_until(lambda: len(store.committed) >= 1))
        self.assertEqual(call_count["n"], 1, "同一条在飞线程期间不该派发第二个")
        self.assertEqual(len(store.committed), 1)

    def test_t3_the_background_thread_is_a_daemon(self) -> None:
        """T3：线程为 daemon。变异验红 M3：把 ``daemon=True`` 改成
        ``daemon=False``。"""

        gate = threading.Event()

        def blocking_read() -> SnapshotBatch:
            gate.wait(timeout=5.0)
            return _committable_batch()

        duty = OrgSnapshotSyncDuty(
            read_snapshot=blocking_read,
            store=FakeStore(),
            audit=RecordingAudit(),
            source_app_id="cli_test",
            clock=lambda: FIXED_NOW,
            round_join_timeout_seconds=0.05,
        )
        self.addCleanup(gate.set)

        duty.run_once()

        thread = duty._pending_thread  # noqa: SLF001 - 白盒确认线程属性
        self.assertIsNotNone(thread)
        assert thread is not None  # 收窄类型，便于下面直接访问属性
        self.assertTrue(thread.daemon, "后台线程必须是 daemon，不得阻止进程退出")
        self.assertEqual(thread.name, "lingxi-org-snapshot-round")
        gate.set()

    def test_t6_a_fast_round_still_returns_the_run_id_synchronously(self) -> None:
        """T6：快轮行为与改动前逐字相同：同步返回 ``run_id``、``committed``
        审计字段不变。变异验红 M6：把快路径也改成返回 ``None``（例如无论
        ``thread.is_alive()`` 结果如何都 ``return None``），本用例的
        ``result == "orgsync_fixed"`` 断言会变红。
        """

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


class StuckThreadAlertTests(unittest.TestCase):
    """T4：存活超硬上限恰记一条 ``round_thread_stuck`` 审计 + WARNING，重复
    tick 不重复记。变异验红 M4：删掉告警分支，或删掉 ``_round_thread_stuck_alerted``
    这个"只记一次"布尔位（改成每次都记）。
    """

    def test_t4_a_stuck_thread_is_alerted_exactly_once(self) -> None:
        gate = threading.Event()
        clock = {"now": FIXED_NOW}

        def blocking_read() -> SnapshotBatch:
            gate.wait(timeout=5.0)
            return _committable_batch()

        store = FakeStore()
        audit = RecordingAudit()
        duty = OrgSnapshotSyncDuty(
            read_snapshot=blocking_read,
            store=store,
            audit=audit,
            source_app_id="cli_test",
            clock=lambda: clock["now"],
            round_join_timeout_seconds=0.05,
        )
        self.addCleanup(gate.set)

        first = duty.run_once()  # 派发，卡在闸门上，join 超时提前返回
        self.assertIsNone(first)

        # 还没到硬上限：不该告警。
        clock["now"] = FIXED_NOW + timedelta(seconds=ROUND_THREAD_STUCK_AFTER_SECONDS - 1)
        duty.run_once()
        self.assertNotIn("org_snapshot_sync.round_thread_stuck", audit.actions())

        # 越过硬上限：第一次告警，且必须带 WARNING 日志。
        clock["now"] = FIXED_NOW + timedelta(seconds=ROUND_THREAD_STUCK_AFTER_SECONDS + 1)
        with self.assertLogs(
            "lingxi.apps.scheduler.org_snapshot_sync", level="WARNING"
        ) as captured:
            duty.run_once()
        stuck_records = [
            fields for action, fields in audit.records if action == "org_snapshot_sync.round_thread_stuck"
        ]
        self.assertEqual(len(stuck_records), 1)
        self.assertTrue(
            any("存活" in line and "僵" in line for line in captured.output),
            "告警日志必须明确说明这是疑似僵死、长期存活的后台线程",
        )

        # 再来一个 tick：同一条线程仍然存活、仍然超过硬上限——不重复记。
        clock["now"] = clock["now"] + timedelta(seconds=1)
        duty.run_once()
        stuck_records_again = [
            fields for action, fields in audit.records if action == "org_snapshot_sync.round_thread_stuck"
        ]
        self.assertEqual(len(stuck_records_again), 1, "同一条僵死线程只告警一次")

        gate.set()
        self.assertTrue(_poll_until(lambda: len(store.committed) >= 1))


class DispatchIsNotCompletionTests(unittest.TestCase):
    """T5：派发 ≠ 完成——线程内读取失败时当日水位不置位、退避推进、下一 tick
    按退避跳过。变异验红 M5：把 ``_completed_on = today`` 提到派发处（即
    ``run_once`` 一旦成功起了线程就乐观置位水位，不等线程真正跑完）。
    """

    def test_t5_a_failing_round_does_not_mark_completed_until_it_actually_finishes(
        self,
    ) -> None:
        gate = threading.Event()
        finished = threading.Event()
        clock = {"now": FIXED_NOW}
        call_count = {"n": 0}

        def blocking_failing_read() -> SnapshotBatch:
            call_count["n"] += 1
            gate.wait(timeout=5.0)
            finished.set()
            raise RuntimeError("transport_error")

        store = FakeStore()
        audit = RecordingAudit()
        duty = OrgSnapshotSyncDuty(
            read_snapshot=blocking_failing_read,
            store=store,
            audit=audit,
            source_app_id="cli_test",
            clock=lambda: clock["now"],
            round_join_timeout_seconds=0.05,
        )
        self.addCleanup(gate.set)

        result = duty.run_once()

        self.assertIsNone(result)
        self.assertIsNone(duty.completed_on, "派发不等于完成：线程还没收工时不该有任何状态更新")
        self.assertEqual(audit.actions(), [], "线程还没收工时也不该有任何审计")

        gate.set()
        self.assertTrue(finished.wait(timeout=5.0))
        self.assertTrue(_poll_until(lambda: "org_snapshot_sync.read_failed" in audit.actions()))

        self.assertIsNone(duty.completed_on, "读取失败的一轮依然不得置位当日水位")
        self.assertIsNotNone(duty._next_attempt_at, "读取失败必须推进退避")  # noqa: SLF001

        # 下一 tick 落在退避窗口内：应该被挡住，不再发起外部读取。
        clock["now"] = clock["now"] + timedelta(seconds=1)
        again = duty.run_once()
        self.assertIsNone(again)
        self.assertEqual(call_count["n"], 1, "退避窗口内不得再发起外部读取")


class NoFakeHeartbeatTest(unittest.TestCase):
    """T7：反假心跳——源码级断言本模块不依赖"进程活性"心跳机制的任何符号
    （Issue #340 明令禁止的 B1 变体：把心跳回调埋进读取回调的中途，会让活性
    语义退化成"最近发过一次 HTTP 请求"）。变异验红 M7：在读取回调（或
    ``_run_round``）里插入一次 ``touch_liveness(...)`` 调用/对应的 import。
    """

    def test_t7_the_module_source_never_references_the_liveness_module(self) -> None:
        source = inspect.getsource(org_snapshot_sync_module)
        self.assertNotIn(
            "apps.liveness", source, "本模块不得依赖进程活性心跳机制所在的模块"
        )
        self.assertNotIn(
            "touch_liveness",
            source,
            "本模块不得调用 touch_liveness——这是设计文明令禁止的假心跳变体",
        )

    def test_t7_the_read_chain_modules_never_reference_the_liveness_module(self) -> None:
        """B1 变体的字面位置在读取链路（分页请求/读取回调），不在本模块——
        把断言扩到真正发 HTTP 请求与组装读取回调的三个模块，堵住"心跳埋进
        分页中途"的每一处落点（Epic G 批次审查 P2-1）。"""
        from lingxi.adapters import feishu_directory, feishu_org_snapshot_reader
        from lingxi.apps.scheduler import assembly
        from lingxi.apps.scheduler import org_snapshot_sync as org_snapshot_sync_home

        # S-H-2 把组织快照装配从 assembly 搬进 org_snapshot_sync 本体——读取回调
        # 组装的现居地一并纳入扫描（S-H-2 批审查 P3-1）
        for module in (
            feishu_directory,
            feishu_org_snapshot_reader,
            assembly,
            org_snapshot_sync_home,
        ):
            with self.subTest(module=module.__name__):
                source = inspect.getsource(module)
                self.assertNotIn(
                    "apps.liveness", source,
                    f"{module.__name__} 不得依赖进程活性心跳机制所在的模块",
                )
                self.assertNotIn(
                    "touch_liveness", source,
                    f"{module.__name__} 不得调用 touch_liveness（假心跳变体）",
                )


class BaseExceptionInThreadTests(unittest.TestCase):
    """T8：线程内抛 ``BaseException`` 留同形状日志不静默。变异验红 M8：把
    ``except BaseException`` 缩成 ``except Exception``（或整个删掉这层
    try/except）——``SystemExit`` 不是 ``Exception`` 的子类，不会被
    ``_run_round`` 内部既有的 ``except Exception`` 分支捕获，本用例的
    ``assertLogs`` 断言会因为"没有任何日志被记录"直接报 AssertionError 而变红。
    """

    def test_t8_a_base_exception_escaping_the_round_is_logged_not_silenced(
        self,
    ) -> None:
        def read_and_exit() -> SnapshotBatch:
            raise SystemExit("boom-should-not-appear-in-logs")

        store = FakeStore()
        audit = RecordingAudit()
        duty = OrgSnapshotSyncDuty(
            read_snapshot=read_and_exit,
            store=store,
            audit=audit,
            source_app_id="cli_test",
            clock=lambda: FIXED_NOW,
        )

        with self.assertLogs(
            "lingxi.apps.scheduler.org_snapshot_sync", level="ERROR"
        ) as captured:
            result = duty.run_once()

        self.assertIsNone(result)
        self.assertEqual(store.committed, [])
        self.assertEqual(audit.actions(), [], "BaseException 不经过既有任何一条审计分支")
        self.assertTrue(
            any("未预期异常" in line and "SystemExit" in line for line in captured.output)
        )
        for line in captured.output:
            self.assertNotIn("boom-should-not-appear-in-logs", line, "只记异常类型，不记正文")


class FullLoopIntegrationTest(unittest.TestCase):
    """T9：整循环集成——慢组织快照职责 + 记录型职责并存，记录型职责每 tick
    都跑、``heartbeat`` 每 tick 都被调用。变异验红 M9：把职责改回同步执行
    （即撤销 Issue #340 本次改动），``SchedulerLoop.run_once()`` 会被一整轮
    阻塞式读取拖住。真正承担红点的是 ``assertLess(elapsed, 2.0)``——假读取的
    ``gate.wait(timeout=5.0)`` 超时后照样返回批次，三个 tick 最终都会跑完、
    两个计数仍是 3（Epic G 批次审查 P2-2 实测更正），但整体耗时会从毫秒级
    涨到 ≥5 秒，被时长断言可靠抓红。
    """

    def test_t9_other_duties_and_heartbeat_keep_ticking_while_a_round_is_in_flight(
        self,
    ) -> None:
        from lingxi.apps.scheduler.loop import SchedulerLoop

        gate = threading.Event()

        def blocking_read() -> SnapshotBatch:
            gate.wait(timeout=5.0)
            return _committable_batch()

        store = FakeStore()
        audit = RecordingAudit()
        org_duty = OrgSnapshotSyncDuty(
            read_snapshot=blocking_read,
            store=store,
            audit=audit,
            source_app_id="cli_test",
            clock=lambda: FIXED_NOW,
            round_join_timeout_seconds=0.05,
        )
        self.addCleanup(gate.set)

        counters = {"heartbeat": 0, "recorded_duty": 0}

        class RecordingDuty:
            name = "记录型职责"

            def run_once(self) -> None:
                counters["recorded_duty"] += 1
                return None

        loop = SchedulerLoop(
            duties=[org_duty, RecordingDuty()],
            heartbeat=lambda: counters.__setitem__("heartbeat", counters["heartbeat"] + 1),
        )

        started = time.monotonic()
        for _ in range(3):
            loop.run_once()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2.0, "组织快照全量轮在飞期间不得拖慢整轮调度")
        self.assertEqual(counters["heartbeat"], 3, "心跳每 tick 都必须被调用，不受慢轮影响")
        self.assertEqual(
            counters["recorded_duty"], 3, "排在组织快照之后的记录型职责每 tick 都要照常运行"
        )

        gate.set()
        self.assertTrue(_poll_until(lambda: len(store.committed) >= 1))


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
