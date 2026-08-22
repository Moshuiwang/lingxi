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
from datetime import datetime, timedelta, timezone

from lingxi.apps.scheduler.org_snapshot_sync import (
    CONSECUTIVE_ROUND_BUDGET_EXCEEDED_ESCALATION_THRESHOLD,
    READ_FAILURE_BACKOFF_CEILING_SECONDS,
    READ_FAILURE_BACKOFF_STEP_SECONDS,
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
        推进同一条退避。`raise` 本身不变：这里既要证明异常照常冒泡，又要证明
        状态已经在冒泡前记好，下一次调用会被退避挡住。变异锚点：把
        `self._advance_backoff()` 从 `commit_failed` 分支删掉，本用例的
        `call_count` 断言会从 1 变红成 2（且 `backoff_seconds`/`attempt` 字段
        会从审计里消失）。"""

        call_count = {"n": 0}

        def counting_read() -> SnapshotBatch:
            call_count["n"] += 1
            return _committable_batch()

        store = FakeStore(raises=RuntimeError("connection_lost"))
        audit = RecordingAudit()
        duty = OrgSnapshotSyncDuty(
            read_snapshot=counting_read, store=store, audit=audit, source_app_id="cli_test", clock=lambda: FIXED_NOW
        )

        with self.assertRaises(RuntimeError):
            duty.run_once()

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
