"""花名册审计日报职责、群发适配与零迁移守卫（Issue #52 / W4-B）。

认领断言：V-花名册-13、V-花名册-16、V-花名册-17、V-花名册-18、V-花名册-19、
V-花名册-20、V-花名册-25、V-花名册-26、V-花名册-27、V-花名册-28、V-花名册-29、
V-花名册-30、V-花名册-31、V-花名册-32、V-花名册-33、V-花名册-34。

真库侧（数据范围、存档不写回、端到端）在 `tests/test_roster_audit_postgres.py`；
比对与渲染的纯函数断言在 `tests/test_roster_audit_diff.py` 与
`tests/test_roster_daily_report.py`。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from datetime import UTC, date, datetime, timedelta

from lingxi.apps.scheduler import (
    RosterAuditDuty,
    SchedulerConfig,
    SchedulerLoop,
    StructuredLogAuditSink,
    build_loop,
)
from lingxi.core.identity.access_token_supply import AccessTokenUnavailable
from lingxi.core.identity.roster_audit import ArchivedIdentity, DiffKind
from lingxi.core.identity.roster_snapshot import (
    DEFAULT_SNAPSHOT_STALE_AFTER,
    RosterRound,
    RosterSnapshotStatus,
)

REPOSITORY_ROOT = pathlib.Path(__file__).parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"

# 假群 ID。刻意不是真实形状（真实 chat_id 是 `oc_` + 32 位十六进制），
# 这样 `V-花名册-28` 的扫描既能抓到真值入码，又不会把测试夹具误报成泄露。
FAKE_CHAT_ID = "oc_fake_admin_group_for_tests"

USER_ONE = "usr_01JQZX3M5N7P9R1T3V5W7Y9A0B"
USER_TWO = "usr_01K2AB4D6F8H0J2M4P6R8T0V2W"
PERSON_ONE = "ou_person_0001"
PERSON_TWO = "ou_person_0002"

# 资料值：审计、日志与日报正文里都不许出现。
NAME = "张三"
EMPLOYEE_NO = "E1001"
EMAIL = "zhangsan@example.com"
NEW_NAME = "张三改名"
NEW_EMAIL = "zhangsan.new@example.com"

COMPLETE_ENV = {
    "LINGXI_POSTGRES_DSN": "postgresql://user@localhost:5432/lingxi",
    "LINGXI_DELEGATED_CREDENTIAL_KEY": "ZmFrZS1mZXJuZXQta2V5LWZvci11bml0LXRlc3RzLTA9",
    "LINGXI_DELEGATED_CREDENTIAL_PATH": "/var/lib/lingxi/credentials/delegated.enc",
    "LINGXI_FEISHU_APP_ID": "cli_fake",
    "LINGXI_FEISHU_APP_SECRET": "secret_fake",
}


def baseline_of_one() -> list[ArchivedIdentity]:
    return [ArchivedIdentity(USER_ONE, PERSON_ONE, NAME, EMPLOYEE_NO, EMAIL)]


def changed_rows() -> list[dict[str, object]]:
    return [{"personnel_id": PERSON_ONE, "name": NEW_NAME, "employee_no": EMPLOYEE_NO, "email": EMAIL}]


def unchanged_rows() -> list[dict[str, object]]:
    return [{"personnel_id": PERSON_ONE, "name": NAME, "employee_no": EMPLOYEE_NO, "email": EMAIL}]


# 快照的假行数。用一个接近实测规模的数字（2026-08-05 受控读取：1206 行）而不是
# `len(rows)`：夹具里的一两行是"比对输入"，与"这一份快照有多大"不是一回事。
SNAPSHOT_ROWS = 1206

STALE_AFTER_SECONDS = DEFAULT_SNAPSHOT_STALE_AFTER.total_seconds()


def fresh_snapshot(*, captured_at: datetime | None = None) -> RosterSnapshotStatus:
    """本轮刚整体替换过的一份快照：可用、不超龄、无保旧告警。"""

    return RosterSnapshotStatus(
        action="replace",
        read_status="complete",
        stale_after_seconds=STALE_AFTER_SECONDS,
        captured_at=captured_at or datetime(2026, 8, 6, 9, 0, tzinfo=UTC),
        row_count=SNAPSHOT_ROWS,
        age_seconds=0.0,
    )


def stale_snapshot(*, age_seconds: float = STALE_AFTER_SECONDS + 3600) -> RosterSnapshotStatus:
    """保留下来的上一份快照，且已经超龄（`V-花名册-47`）。"""

    return RosterSnapshotStatus(
        action="keep_previous",
        read_status="failed",
        stale_after_seconds=STALE_AFTER_SECONDS,
        alert="failed_indeterminate",
        failure_code="pagination_stalled",
        failure_kind="indeterminate",
        captured_at=datetime(2026, 8, 3, 9, 0, tzinfo=UTC),
        row_count=SNAPSHOT_ROWS,
        age_seconds=age_seconds,
    )


def absent_snapshot() -> RosterSnapshotStatus:
    """库里从未有过快照，且本轮也没读成功（`V-花名册-48`）。"""

    return RosterSnapshotStatus(
        action="no_snapshot_yet",
        read_status="empty_source",
        stale_after_seconds=STALE_AFTER_SECONDS,
        alert="empty_source",
    )


class FakeRosterSource:
    """一轮花名册取用的可注入替身：交出注入的行与快照状态，并记录被调用的时刻。"""

    def __init__(
        self,
        rows: list[dict[str, object]],
        *,
        snapshot: RosterSnapshotStatus | None = None,
        error: Exception | None = None,
    ) -> None:
        self._rows = rows
        self._snapshot = snapshot
        self._error = error
        self.calls: list[datetime] = []

    def current(self, *, now: datetime) -> RosterRound:
        self.calls.append(now)
        if self._error is not None:
            raise self._error
        return RosterRound(tuple(self._rows), self._snapshot or fresh_snapshot(captured_at=now))


class FakeBaselineReader:
    def __init__(self, baseline: list[ArchivedIdentity], *, explode: bool = False) -> None:
        self._baseline = baseline
        self._explode = explode
        self.calls = 0

    def load_active_baseline(self) -> list[ArchivedIdentity]:
        self.calls += 1
        if self._explode:
            raise RuntimeError(f"模拟读取失败，正文里有资料值 {EMAIL}")
        return self._baseline


class FakeSender:
    """记录每一次发送的完整载荷。失败次数可控，用来验重试与"不算已发送"。

    `attempts` 与 `payloads` 分开记：不确定态的重试要能看见"这一次请求发生过"，
    而它恰恰不会出现在 `payloads` 里（因为它抛了异常）。
    """

    def __init__(self, *, failures: int = 0, error: Exception | None = None) -> None:
        self._failures = failures
        self._error = error
        self.payloads: list[dict[str, str]] = []
        self.attempts: list[dict[str, str]] = []

    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None:
        self.attempts.append({"chat_id": chat_id, "text": text, "dedupe_key": dedupe_key})
        if self._failures > 0:
            self._failures -= 1
            raise self._error or RuntimeError(f"模拟发送失败，正文里有资料值 {EMAIL}")
        self.payloads.append({"chat_id": chat_id, "text": text, "dedupe_key": dedupe_key})


class RecordingAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, object]]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, dict(fields)))

    def actions(self) -> list[str]:
        return [action for action, _ in self.records]


class FixedClock:
    """从某天开始，可以手动推进的时钟。跨日判重的用例要能自己决定"今天"。"""

    def __init__(self, start: date = date(2026, 8, 6)) -> None:
        self.today = start

    def __call__(self) -> datetime:
        return datetime(self.today.year, self.today.month, self.today.day, 9, 0, tzinfo=UTC)

    def advance(self, days: int = 1) -> None:
        self.today = self.today + timedelta(days=days)


def build_duty(
    *,
    baseline: list[ArchivedIdentity] | None = None,
    rows: list[dict[str, object]] | None = None,
    sender: FakeSender | None = None,
    audit: RecordingAudit | None = None,
    clock: FixedClock | None = None,
    reader: FakeBaselineReader | None = None,
    stop: threading.Event | None = None,
    snapshot: RosterSnapshotStatus | None = None,
    source: FakeRosterSource | None = None,
) -> tuple[RosterAuditDuty, FakeSender, RecordingAudit, FixedClock, FakeBaselineReader]:
    sender = sender or FakeSender()
    audit = audit or RecordingAudit()
    clock = clock or FixedClock()
    reader = reader or FakeBaselineReader(baseline if baseline is not None else baseline_of_one())
    rows = rows if rows is not None else changed_rows()
    duty = RosterAuditDuty(
        baseline_reader=reader,
        roster_source=source or FakeRosterSource(rows, snapshot=snapshot),
        sender=sender,
        audit=audit,
        chat_id=FAKE_CHAT_ID,
        clock=clock,
        stop=stop,
    )
    return duty, sender, audit, clock, reader


# --------------------------------------------------------------------------
# 三、零迁移守卫
# --------------------------------------------------------------------------


class ZeroMigrationGuardTest(unittest.TestCase):
    """`V-花名册-13`：**迁移是 DDL 的唯一载体**。

    **旧口径已作废。** 这条守卫原名「零迁移」，依据是 2026-08-06 的「零新表」定案；
    产品负责人 2026-08-08 的 **D2 裁定**推翻了那一半——花名册读取需要一份跨重启的持久
    快照，表与 DDL 现在真实存在于 `0063_roster_snapshot`。守卫本身**没有失去意义，
    只是断言的东西变了**：不再是"本组一行 DDL 都没有"，而是"本组的运行时源码一行 DDL
    都没有，建表只在 revision 里"。

    机器面断言本组**全部运行时源文件**：不 import alembic / sqlalchemy，不含建表语句。
    `SLICE_SOURCES` 覆盖比对、渲染、职责、群发、花名册读取与快照读写六个方向——
    漏掉哪个文件，那个文件里偷偷写一句 `CREATE TABLE` 就没有任何东西会红，
    而矩阵 `V-花名册-13` 声称的正是"四层源码都不含建表语句"。

    **刻意不枚举 `versions/` 的文件集合，也不钉住 head 的取值。** 那样写等于把一次性的
    diff 范围固化成永久不变量：别的切片（例如 #57 的 `0057`）合并一条与花名册毫无关系的
    revision 时，这条守卫会红——而它红了并不说明「花名册这一组多了一张表」。
    一个总在别人改动时误报的守卫，最后一定是被人删掉或加豁免，那时它连本来能挡的
    那一类问题也挡不住了。

    revision 链的通用健康度（恰一个 head、恰一个 base、无孤儿、id 长度、README 同步）
    由 #53 建立的 `scripts/ci/check_alembic_revisions.py` 承担，逐条真往返由
    `scripts/ci/check_migration_chain.sh` 承担，`verify_repository.sh` 无条件执行前者。
    下面第二条用例断言这层委托是**真的**接上了，而不是我假设它接上了。
    """

    # 本组（花名册审计日报 + 分页 reader + 持久快照）的全部运行时源文件。
    SLICE_SOURCES = (
        "core/identity/roster_audit.py",
        "core/identity/roster_report.py",
        "adapters/postgres_roster_audit.py",
        "adapters/feishu_group_message.py",
        # #237 拆分后 `RosterAuditDuty` 本身搬进 roster_audit.py；构造它、注入
        # `PostgresRosterSnapshotStore`/`BitableRosterPages`/`PostgresRosterBaselineReader`
        # 的装配代码（`_build_roster_audit_duty`）搬进 assembly.py。两个文件都是本组的
        # 运行时源码，都要接着被这条守卫覆盖——否则拆分本身就会把这条守卫拆成两个哑
        # 分支：一个继续扫一个只剩重导出的空壳（`__init__.py` 177 行，不再含任何本组
        # 实现），另一个（真正持有实现的新文件）从未被扫过。不纳入 `config.py`：它只是
        # `SchedulerConfig` 的纯环境变量读取，不 import 任何适配器、不构造任何存储对象，
        # 是全部九个职责共享的配置外壳而不是本组专属源码，纳入它不会让这条守卫更有效，
        # 只会让"本组"这个概念失去边界。
        "apps/scheduler/roster_audit.py",
        "apps/scheduler/assembly.py",
        # S-B-01：花名册分页 reader 与四态完整性判定。
        "adapters/feishu_roster_bitable.py",
        # S-B-02：替换门槛与保旧告警（core）、持久快照读写（adapters）。
        # 后者是本组**唯一**写库的模块，也因此最需要这条守卫：它离"顺手在代码里
        # 建表"只有一步之遥，而建表必须留在 revision 里才可回滚、可对账。
        "core/identity/roster_snapshot.py",
        "adapters/postgres_roster_snapshot.py",
    )

    def test_no_source_of_this_slice_can_perform_a_migration(self) -> None:
        """既不 import 迁移工具，也不自己写 DDL——建表只在 revision 里。"""

        for module in self.SLICE_SOURCES:
            path = SOURCE_ROOT / "lingxi" / module
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])

            with self.subTest(module=module):
                # 与 `V-迁移-04`（`src/` 里不得出现 sqlalchemy / alembic）同向，
                # 这里是本切片自己的那一份。
                self.assertNotIn("alembic", imported)
                self.assertNotIn("sqlalchemy", imported)
                upper = source.upper()
                for statement in ("CREATE TABLE", "ALTER TABLE", "DROP TABLE"):
                    self.assertNotIn(statement, upper)

    def test_the_shared_revision_chain_guard_is_really_wired_into_the_gate(self) -> None:
        """委托必须可核对：#53 的检查器存在，且被门禁脚本无条件调用。

        「那件事由别人管」如果没人核对，等价于没人管。
        """

        checker = REPOSITORY_ROOT / "scripts" / "ci" / "check_alembic_revisions.py"
        gate = REPOSITORY_ROOT / "scripts" / "ci" / "verify_repository.sh"

        self.assertTrue(checker.is_file(), "revision 链检查器不存在，V-花名册-13 的委托落空")
        gate_text = gate.read_text(encoding="utf-8")
        self.assertIn("check_alembic_revisions.py", gate_text, "门禁脚本没有调用 revision 链检查器")
        # 检查器自己就断言"恰好 1 个 head"，这里核对那句话确实在它里面。
        self.assertIn("get_heads()", checker.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 四、scheduler 职责
# --------------------------------------------------------------------------


class DutyDrivenByTheLoopTest(unittest.TestCase):
    """V-花名册-16：周期实际调用；观察点是注入接口真的被调到。"""

    def test_the_loop_actually_drives_the_roster_duty_each_round(self) -> None:
        clock = FixedClock()
        duty, sender, _audit, _clock, reader = build_duty(clock=clock)
        loop = SchedulerLoop(duties=(duty,), interval_seconds=0.01)

        for _round in range(3):
            loop.run_once()
            clock.advance()

        # 三个不同的日子＝三轮都真的执行了比对与发送。
        self.assertEqual(reader.calls, 3)
        self.assertEqual(len(sender.payloads), 3)


class DutyIsolationTest(unittest.TestCase):
    """V-花名册-17：职责间失败隔离。V-花名册-18：连续失败不退进程，日志只记异常类型。"""

    class RecordingDuty:
        def __init__(self, name: str, *, explode: bool = False) -> None:
            self.name = name
            self.calls = 0
            self._explode = explode

        def run_once(self) -> str:
            self.calls += 1
            if self._explode:
                raise RuntimeError("模拟失败")
            return f"{self.name}-ok"

    def test_a_failing_report_duty_does_not_skip_rotation_or_cleanup(self) -> None:
        duty, _sender, _audit, _clock, _reader = build_duty(
            reader=FakeBaselineReader(baseline_of_one(), explode=True)
        )
        others = [self.RecordingDuty("凭据轮换"), self.RecordingDuty("保留清理")]
        loop = SchedulerLoop(duties=(others[0], others[1], duty), interval_seconds=0.01)

        with self.assertLogs("lingxi.apps.scheduler", level="ERROR") as captured:
            reports = loop.run_once()

        self.assertEqual([other.calls for other in others], [1, 1], "日报炸掉不得带走其他职责")
        self.assertIsNone(reports[2])
        self.assertTrue(any("花名册审计日报" in line for line in captured.output))

    def test_a_failing_rotation_does_not_skip_the_report_duty(self) -> None:
        """反方向也验：把 try 写在循环外面时，只验一个方向仍有一半会过。"""

        duty, sender, _audit, _clock, _reader = build_duty()
        exploding = self.RecordingDuty("凭据轮换", explode=True)
        loop = SchedulerLoop(duties=(exploding, duty), interval_seconds=0.01)

        with self.assertLogs("lingxi.apps.scheduler", level="ERROR"):
            loop.run_once()

        self.assertEqual(len(sender.payloads), 1, "轮换炸掉时日报本轮仍须发出")

    def test_repeated_report_failures_never_stop_the_loop_and_never_log_the_body(self) -> None:
        duty, _sender, _audit, clock, reader = build_duty(
            reader=FakeBaselineReader(baseline_of_one(), explode=True)
        )
        loop = SchedulerLoop(duties=(duty,), interval_seconds=0.01)

        with self.assertLogs("lingxi.apps.scheduler", level="ERROR") as captured:
            for _round in range(5):
                loop.run_once()
                clock.advance()

        self.assertEqual(reader.calls, 5, "连续失败后仍要继续下一轮")
        output = "\n".join(captured.output)
        self.assertIn("RuntimeError", output)
        # 异常正文里带着一个邮箱值——它绝不能进日志。
        self.assertNotIn(EMAIL, output)
        self.assertNotIn("模拟读取失败", output)


class SnapshotWriteIsNotHostageToTheBaselineTest(unittest.TestCase):
    """**冻结候选审查 2026-08-21 的 F6**：日报基线读取失败**不得**拖停快照写入。

    `roster_snapshot` 表是首次开通链第二步（``core/identity/onboarding_runner.py::
    _match``）与每日权限重算的硬前提，而 `load_active_baseline()` 读的 `app_user`
    只服务日报正文。Issue #275 把"没配管理群 ⇒ 快照不写"这条**配置期**耦合解开了，
    但运行期还剩一条：旧代码先读基线、后取花名册，于是基线持续读不出来时，健康的
    花名册读取与快照写入被一起拖停——表现成"今天入职的人全都开通不了"，而根因在一条
    只影响日报的读库上。

    变异验红：把 `RosterAuditDuty.run_once` 里 `load_active_baseline()` 那一行移回
    `roster_source.current(...)` **之前**（改动前的顺序）之后重跑本类，
    ``source.calls`` 会是空的、``assertEqual(len(source.calls), 1)`` 失败。
    """

    def test_a_failing_baseline_read_still_lets_the_snapshot_round_happen(self) -> None:
        source = FakeRosterSource(changed_rows())
        duty, sender, audit, _clock, reader = build_duty(
            reader=FakeBaselineReader(baseline_of_one(), explode=True), source=source
        )

        with self.assertRaises(RuntimeError):
            duty.run_once()

        self.assertEqual(
            len(source.calls),
            1,
            "花名册读取与快照写入必须先发生：它服务的是开通链，不是日报",
        )
        self.assertEqual(reader.calls, 1, "用例前提：基线读取确实被调用并且炸了")
        self.assertEqual(sender.payloads, [], "比对没做成，这一轮不发日报")
        self.assertIsNone(duty.completed_on, "异常上抛，水位不置位，下一轮重试")

    def test_the_error_body_never_reaches_the_audit_or_the_logs(self) -> None:
        """基线读取失败的异常正文里带着资料值（`FakeBaselineReader` 刻意如此）。

        它原样上抛给 `SchedulerLoop` 做职责级隔离（既有纪律，`V-花名册-17`），本职责
        自己**不记任何审计**——调换顺序没有给它新开一个会回显异常正文的出口。
        """

        source = FakeRosterSource(changed_rows())
        duty, _sender, audit, _clock, _reader = build_duty(
            reader=FakeBaselineReader(baseline_of_one(), explode=True), source=source
        )

        with self.assertRaises(RuntimeError):
            duty.run_once()

        self.assertEqual(audit.records, [], "基线读取失败由循环层隔离，本职责不另记审计")

    def test_an_unavailable_snapshot_still_reads_the_baseline_for_the_examined_count(
        self,
    ) -> None:
        """新顺序下 `snapshot.available is False` 的分支照旧成立：不比对，但
        `examined` 仍然取自基线人数（`V-花名册-48`）。"""

        source = FakeRosterSource([], snapshot=absent_snapshot())
        duty, sender, audit, _clock, reader = build_duty(
            baseline=baseline_of_one(), source=source
        )

        report = duty.run_once()

        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(reader.calls, 1, "基线仍然被读到——只是排在花名册之后")
        self.assertEqual(report.examined, len(baseline_of_one()))
        self.assertEqual(report.entries, (), "没有可用快照时一条差异都不比对")
        self.assertEqual(len(sender.payloads), 1, "没有快照必须发告警日报，沉默最危险")


class StopSemanticsTest(unittest.TestCase):
    """V-花名册-20：停止之后 `sends_after_stop == 0`。"""

    def test_a_stopping_duty_sends_nothing_and_does_not_even_read_the_baseline(self) -> None:
        stop = threading.Event()
        duty, sender, audit, _clock, reader = build_duty(stop=stop)

        duty.request_stop()
        result = duty.run_once()

        self.assertTrue(duty.stopping)
        self.assertIsNone(result)
        self.assertEqual(sender.payloads, [], "停止后发送次数必须是 0")
        self.assertEqual(reader.calls, 0, "停止后连基线都不该再读")
        self.assertEqual(audit.records, [])

    def test_a_stop_arriving_during_the_read_phase_still_prevents_the_send(self) -> None:
        """入口那一次检查挡不住"进来时还没停、读完才停"这条时序。

        读库与读花名册都可能耗时（真实花名册是分页的网络读取），停止信号很容易落在
        中间。此时必须干净中断：不发送、不置水位——半路发出去的那一份日报，
        既不在停机预算内，也让"停止之后 0 次发送"变成一句只在快路径上成立的话。
        """

        stop = threading.Event()

        class StoppingReader(FakeBaselineReader):
            def load_active_baseline(self):
                result = super().load_active_baseline()
                stop.set()  # 读取期间收到 SIGTERM
                return result

        duty, sender, audit, _clock, reader = build_duty(
            reader=StoppingReader(baseline_of_one()), stop=stop
        )

        with self.assertLogs("lingxi.apps.scheduler", level="INFO") as captured:
            report = duty.run_once()

        self.assertEqual(reader.calls, 1, "用例前提：这一轮确实进到了读取阶段")
        self.assertIsNotNone(report, "比对已经做完了，返回的是这一轮的结果")
        self.assertEqual(sender.payloads, [], "停止之后必须 0 次发送")
        self.assertEqual(sender.attempts, [], "连一次发送尝试都不该发生")
        self.assertIsNone(duty.completed_on, "干净中断不置水位，重发由裁定 C2 覆盖")
        self.assertEqual(audit.records, [])
        self.assertTrue(any("停止信号" in line for line in captured.output))

    def test_one_stop_signal_reaches_the_report_duty_together_with_the_others(self) -> None:
        stop = threading.Event()
        duty, sender, _audit, _clock, _reader = build_duty(stop=stop)
        loop = SchedulerLoop(duties=(duty,), interval_seconds=0.01, stop=stop)

        loop.request_stop()
        loop.run_once()

        self.assertTrue(duty.stopping)
        self.assertEqual(sender.payloads, [])


class SigtermTest(unittest.TestCase):
    """V-花名册-19：真实子进程 + 真实 SIGTERM。

    不开新轮、在途发送完整做完（无半发送）、超时内退出。mock 出来的信号证明不了
    这几件事——尤其是"在途的那一次没有被截断"。
    """

    SCRIPT = textwrap.dedent(
        """
        import itertools, json, threading, time
        from datetime import datetime, timedelta, timezone
        from lingxi.apps.scheduler import RosterAuditDuty, SchedulerLoop, install_signal_handlers
        from lingxi.core.identity.roster_audit import ArchivedIdentity
        from lingxi.core.identity.roster_snapshot import RosterRound, RosterSnapshotStatus

        state = {"rounds": 0, "rounds_after_stop": 0,
                 "send_started": 0, "send_completed": 0, "sends_after_stop": 0}

        class Baseline:
            def load_active_baseline(self):
                state["rounds"] += 1
                if loop.stopping:
                    state["rounds_after_stop"] += 1
                return [ArchivedIdentity("usr_01JQZX3M5N7P9R1T3V5W7Y9A0B", "ou_p1",
                                         "存档姓名", "E1001", "archived@example.com")]

        class Sender:
            def send_text(self, *, chat_id, text, dedupe_key):
                if loop.stopping:
                    state["sends_after_stop"] += 1
                state["send_started"] += 1
                time.sleep(0.4)              # 在途发送：SIGTERM 到达时正在做
                state["send_completed"] += 1

        class Audit:
            def record(self, action, /, **fields):
                pass

        class Source:
            def current(self, *, now):
                snapshot = RosterSnapshotStatus(
                    action="replace",
                    read_status="complete",
                    stale_after_seconds=172800.0,
                    captured_at=now,
                    row_count=1206,
                    age_seconds=0.0,
                )
                rows = ({"personnel_id": "ou_p1", "name": "花名册姓名",
                         "employee_no": "E1001", "email": "archived@example.com"},)
                return RosterRound(rows, snapshot)

        # 每轮换一天，让同日判重不挡住后续轮次。
        days = itertools.count()
        base = datetime(2026, 8, 6, 9, 0, tzinfo=timezone.utc)

        # 一个停止标志显式贯穿职责与循环——这正是 SIGTERM 要验的结构本身，
        # 不从职责内部把私有标志掏出来。
        stop = threading.Event()

        duty = RosterAuditDuty(
            baseline_reader=Baseline(),
            roster_source=Source(),
            sender=Sender(),
            audit=Audit(),
            chat_id="oc_fake_admin_group_for_tests",
            clock=lambda: base + timedelta(days=next(days)),
            stop=stop,
        )
        loop = SchedulerLoop(duties=(duty,), interval_seconds=0.05, stop=stop)
        install_signal_handlers(loop)
        print("ready", flush=True)
        loop.run_forever()
        print(json.dumps(state), flush=True)
        """
    )

    def test_sigterm_stops_new_rounds_finishes_the_in_flight_send_and_exits_cleanly(self) -> None:
        environment = {**os.environ, "PYTHONPATH": str(SOURCE_ROOT), "PYTHONUNBUFFERED": "1"}
        process = subprocess.Popen(
            [sys.executable, "-c", self.SCRIPT],
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stdout is not None
            self.assertEqual(process.stdout.readline().strip(), "ready")
            # 让信号落在一次在途发送的中间。
            time.sleep(0.6)
            sent_at = time.monotonic()
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=10)
        finally:
            if process.poll() is None:  # pragma: no cover - 只在断言失败路径上发生
                process.kill()
                process.communicate()

        self.assertEqual(process.returncode, 0, msg=stderr)
        self.assertLess(time.monotonic() - sent_at, 5, "SIGTERM 后必须在超时内退出")
        state = json.loads(stdout.strip().splitlines()[-1])
        self.assertEqual(state["rounds_after_stop"], 0, "收到 SIGTERM 后不得再开新一轮比对")
        self.assertEqual(state["sends_after_stop"], 0, "收到 SIGTERM 后不得再发起新的发送")
        self.assertEqual(
            state["send_started"], state["send_completed"], "在途发送必须做完，不得留下半发送"
        )
        self.assertGreaterEqual(state["send_completed"], 1)
        # 日报正文与资料值都不该出现在进程输出里。
        self.assertNotIn("archived@example.com", stdout + stderr)


# --------------------------------------------------------------------------
# 五、日报内容与发送次数
# --------------------------------------------------------------------------


class SendCountTest(unittest.TestCase):
    """V-花名册-25：空差异日出站 0 次 + 审计恰 1 条；非空发送恰 1 次。"""

    def test_a_day_without_differences_sends_nothing_and_audits_exactly_once(self) -> None:
        duty, sender, audit, _clock, _reader = build_duty(rows=unchanged_rows())

        report = duty.run_once()

        self.assertTrue(report.is_empty)
        self.assertEqual(sender.payloads, [], "空差异日不得出站")
        self.assertEqual(len(audit.records), 1, "空差异日审计恰 1 条")
        self.assertEqual(audit.records[0][0], "roster_audit.no_difference")

    def test_a_day_with_differences_sends_exactly_once(self) -> None:
        duty, sender, audit, _clock, _reader = build_duty()

        duty.run_once()

        self.assertEqual(len(sender.payloads), 1, "非空差异日发送恰 1 次")
        self.assertEqual(sender.payloads[0]["chat_id"], FAKE_CHAT_ID)
        self.assertEqual(audit.actions(), ["roster_audit.report_sent"])
        self.assertEqual(audit.records[0][1]["content_key"], "roster.daily_report")
        self.assertTrue(audit.records[0][1]["content_version"])


class RemovalTakesNoActionTest(unittest.TestCase):
    """V-花名册-26：移除单列且零自动动作。

    职责只有三个协作者：**只读**的基线读取、花名册读取、群发。结构上就没有可以
    改 app_user、停用账号、发布权限或建待办的地方——这条断言把那个结构钉住。
    """

    def test_a_person_missing_from_the_roster_produces_one_report_line_and_nothing_else(self) -> None:
        duty, sender, audit, _clock, _reader = build_duty(rows=[])

        report = duty.run_once()

        self.assertEqual([entry.kind for entry in report.entries], [DiffKind.REMOVED])
        self.assertEqual(len(sender.payloads), 1)
        body = sender.payloads[0]["text"]
        self.assertIn("花名册查无此人", body)
        self.assertIn("未做任何自动处置", body)
        # 审计里只有"发了一份日报"这一件事，没有任何处置动作。
        self.assertEqual(audit.actions(), ["roster_audit.report_sent"])

    def test_the_duty_exposes_no_collaborator_that_could_mutate_anything(self) -> None:
        # #237 拆分后 `RosterAuditDuty` 搬进了 roster_audit 子模块。
        source = (
            SOURCE_ROOT / "lingxi" / "apps" / "scheduler" / "roster_audit.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        duty_class = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == "RosterAuditDuty"
        )
        called_attributes = {
            node.func.attr
            for node in ast.walk(duty_class)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }

        for forbidden in ("save", "update", "commit_batch", "record_identity", "revoke", "publish", "execute"):
            self.assertNotIn(forbidden, called_attributes, f"日报职责不得调用 {forbidden}")


class IdempotenceTest(unittest.TestCase):
    """V-花名册-31：不重复发送（三面）。"""

    def test_one_round_sends_once(self) -> None:
        """第①面。"""

        duty, sender, _audit, _clock, _reader = build_duty()

        duty.run_once()

        self.assertEqual(len(sender.payloads), 1)

    def test_the_same_process_sends_once_per_day_across_rounds(self) -> None:
        """第②面：同进程、同一天、多轮 → 只发一次；换一天才再发。"""

        clock = FixedClock()
        duty, sender, audit, _clock, reader = build_duty(clock=clock)

        for _round in range(4):
            duty.run_once()

        self.assertEqual(len(sender.payloads), 1, "同一天跨轮只发一次")
        self.assertEqual(reader.calls, 1, "已经做完的那天不再重复读库")
        self.assertEqual(len(audit.records), 1)

        clock.advance()
        duty.run_once()

        self.assertEqual(len(sender.payloads), 2, "换一天后应当再发一次")
        self.assertEqual(duty.completed_on, clock.today)

    def test_a_restart_resends_the_same_day_with_a_byte_identical_payload(self) -> None:
        """第③面（验收者定稿）：每个进程实例同一日最多一次；重启当日的重发载荷
        与首次**逐字段完全一致**。

        **判重水位没有持久载体**，跨重启的真幂等做不到（裁定 C2 / R2 知情接受）。
        能被用例证明、也确实值得保证的是：那次重发不是一份**不同的**日报。

        原表述是「零新表定案下没有持久载体」，那个前提已被 2026-08-08 的 D2 裁定覆盖
        （仓库现有 `roster_snapshot`，迁移 `0063`）；但那份持久载体存的是花名册**读取
        结果**、不是判重水位，所以这条用例要证明的东西一个字都没变。
        """

        clock = FixedClock()
        sender = FakeSender()
        rows = changed_rows()

        first_instance, _s, _a, _c, _r = build_duty(clock=clock, sender=sender, rows=rows)
        first_instance.run_once()

        # 模拟重启：同一天、同样的输入，换一个全新的职责实例（水位是空的）。
        second_instance, _s2, _a2, _c2, _r2 = build_duty(clock=clock, sender=sender, rows=rows)
        second_instance.run_once()

        self.assertEqual(len(sender.payloads), 2, "重启当日会重发一次（R2 知情接受）")
        self.assertEqual(
            sender.payloads[0], sender.payloads[1], "重发的载荷必须与首次逐字段一致"
        )

        # 新实例的第三轮不再发送：判重在这个实例内照样生效。
        second_instance.run_once()

        self.assertEqual(len(sender.payloads), 2, "同一实例同一天不得发第三次")


class SendFailureTest(unittest.TestCase):
    """V-花名册-30：发送失败不影响其他职责、只记审计、不算已发送。"""

    def test_a_failed_send_is_audited_swallowed_and_retried_next_round(self) -> None:
        clock = FixedClock()
        sender = FakeSender(failures=1)
        duty, _sender, audit, _clock, _reader = build_duty(clock=clock, sender=sender)

        with self.assertLogs("lingxi.apps.scheduler", level="ERROR") as captured:
            # 不抛异常：抛出去就变成"整轮中断"，而合同要的是"只记审计"。
            duty.run_once()

        self.assertEqual(sender.payloads, [])
        self.assertEqual(audit.actions(), ["roster_audit.send_failed"])
        self.assertEqual(audit.records[0][1]["content_key"], "roster.daily_report")
        self.assertTrue(audit.records[0][1]["content_version"])
        self.assertIsNone(duty.completed_on, "发送失败不得算作当日已发送")

        # 同一天的下一轮会重试，并且这次成功。
        duty.run_once()

        self.assertEqual(len(sender.payloads), 1)
        self.assertEqual(audit.actions(), ["roster_audit.send_failed", "roster_audit.report_sent"])
        self.assertEqual(duty.completed_on, clock.today)

        output = "\n".join(captured.output)
        self.assertIn("RuntimeError", output)
        self.assertNotIn(EMAIL, output, "异常正文不得进日志")

    def test_a_failing_send_leaves_the_other_duties_untouched(self) -> None:
        duty, _sender, _audit, _clock, _reader = build_duty(sender=FakeSender(failures=1))
        other = DutyIsolationTest.RecordingDuty("保留清理")
        loop = SchedulerLoop(duties=(duty, other), interval_seconds=0.01)

        with self.assertLogs("lingxi.apps.scheduler", level="ERROR"):
            reports = loop.run_once()

        self.assertEqual(other.calls, 1)
        self.assertIsNotNone(reports[0], "发送失败不该被当成整个职责失败")


# --------------------------------------------------------------------------
# 六、发送适配
# --------------------------------------------------------------------------


class GroupSenderTest(unittest.TestCase):
    """V-花名册-27：出站可注入。V-花名册-28：群 ID 只从环境变量来。"""

    ADAPTER_PATH = SOURCE_ROOT / "lingxi" / "adapters" / "feishu_group_message.py"

    def test_the_constructor_imports_nothing_and_builds_no_client(self) -> None:
        """反例是 `LarkCardSender`：它在 `__init__` 里就把 SDK client 建了出来，
        于是任何想构造它的测试都被迫装上整个 SDK，注入也就无从谈起。"""

        tree = ast.parse(self.ADAPTER_PATH.read_text(encoding="utf-8"))
        sender_class = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == "FeishuGroupMessages"
        )
        constructor = next(
            node for node in sender_class.body if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        )

        imports = [node for node in ast.walk(constructor) if isinstance(node, (ast.Import, ast.ImportFrom))]
        self.assertEqual(imports, [], "构造函数里不得有任何 import")
        called = {
            node.func.attr
            for node in ast.walk(constructor)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertNotIn("builder", called, "构造函数里不得建 client")

    def test_constructing_and_sending_never_touches_the_network(self) -> None:
        from lingxi.adapters.feishu_group_message import FeishuGroupMessages

        calls: list[dict[str, object]] = []

        def transport(method: str, url: str, *, body=None, token=None):
            calls.append({"method": method, "url": url, "body": body, "token": token})
            if "tenant_access_token" in url:
                return {"code": 0, "tenant_access_token": "t-fake"}
            return {"code": 0, "msg": "ok"}

        sender = FeishuGroupMessages(
            base_url="https://open.feishu.cn/open-apis",
            app_id="cli_fake",
            app_secret="secret_fake",
            transport=transport,
        )
        self.assertEqual(calls, [], "构造本身不得发任何请求")

        sender.send_text(chat_id=FAKE_CHAT_ID, text="脱敏日报正文", dedupe_key="2026-08-06")

        self.assertEqual(len(calls), 2, "一次发送＝取令牌 + 发消息")
        message = calls[1]
        self.assertEqual(message["method"], "POST")
        self.assertIn("receive_id_type=chat_id", str(message["url"]))
        # 纯文本，不是卡片：卡片能带按钮，而管理群通知不得有可执行入口。
        self.assertEqual(message["body"]["msg_type"], "text")
        self.assertEqual(set(json.loads(message["body"]["content"])), {"text"})
        # app_secret 只在取令牌的请求体里，不进 URL。
        self.assertNotIn("secret_fake", str(message["url"]))
        self.assertNotIn("secret_fake", str(calls[0]["url"]))

    def test_a_business_error_code_is_raised_and_carries_no_credential_or_chat_id(self) -> None:
        from lingxi.adapters.feishu_group_message import (
            FeishuGroupMessageError,
            FeishuGroupMessages,
        )

        def transport(method: str, url: str, *, body=None, token=None):
            if "tenant_access_token" in url:
                return {"code": 0, "tenant_access_token": "t-fake"}
            return {"code": 230001, "msg": "bot is not in the chat"}

        sender = FeishuGroupMessages(
            base_url="https://open.feishu.cn/open-apis",
            app_id="cli_fake",
            app_secret="secret_fake",
            transport=transport,
        )

        with self.assertRaises(FeishuGroupMessageError) as raised:
            sender.send_text(chat_id=FAKE_CHAT_ID, text="脱敏日报正文", dedupe_key="2026-08-06")

        message = str(raised.exception)
        self.assertIn("230001", message)
        self.assertNotIn(FAKE_CHAT_ID, message)
        self.assertNotIn("secret_fake", message)
        self.assertTrue(raised.exception.definite)

    def test_a_non_https_base_url_is_refused_without_echoing_it(self) -> None:
        from lingxi.adapters.feishu_group_message import FeishuGroupMessages

        with self.assertRaises(ValueError) as raised:
            FeishuGroupMessages(base_url="http://evil.example.com", app_id="a", app_secret="b")

        self.assertNotIn("evil.example.com", str(raised.exception))

    def test_no_real_group_chat_id_literal_exists_anywhere_in_the_repository(self) -> None:
        """V-花名册-28：真实群 ID 的形状是 `oc_` + 32 位十六进制。

        扫描按**真实形状**而不是按 `oc_` 前缀，这样测试夹具（`oc_fake_...`）不会被
        误报，而任何一次把真值粘进代码都会被抓到。
        """

        realistic = re.compile(r"oc_[0-9a-f]{32}")
        loose = re.compile(r"oc_[0-9a-zA-Z]{24,}")
        offenders: list[str] = []
        for directory in ("src", "tests", "scripts", "migrations", "docs", ".github", "workers"):
            root = REPOSITORY_ROOT / directory
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if not path.is_file() or "__pycache__" in path.parts:
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                if realistic.search(text) or loose.search(text):
                    offenders.append(str(path.relative_to(REPOSITORY_ROOT)))

        self.assertEqual(offenders, [], "群 ID 必须从环境变量注入，不得出现在代码库任何文件里")

    def test_the_chat_id_is_only_ever_read_from_the_environment(self) -> None:
        # #237 拆分后 `SchedulerConfig`（读这个环境变量的地方）搬进了 config 子模块。
        source = (
            SOURCE_ROOT / "lingxi" / "apps" / "scheduler" / "config.py"
        ).read_text(encoding="utf-8")

        self.assertIn("LINGXI_ADMIN_GROUP_CHAT_ID", source)
        self.assertIn("LINGXI_ADMIN_GROUP_CHAT_ID", SchedulerConfig.ENVIRONMENT_KEYS)


class DeliveryIdempotenceTest(unittest.TestCase):
    """V-花名册-31 的投递面：不确定态下的重试必须携带同一个投递 `uuid`。

    「发送失败就重试」有一个它自己看不见的缺口：POST 已经被飞书收下、响应在回程超时时，
    进程拿到的是异常，事实却是消息已经发出去了。没有 `uuid`，这条路径**确定会**重复
    投递一份日报到管理群。
    """

    def _sender(self, transport):
        from lingxi.adapters.feishu_group_message import FeishuGroupMessages

        return FeishuGroupMessages(
            base_url="https://open.feishu.cn/open-apis",
            app_id="cli_fake",
            app_secret="secret_fake",
            transport=transport,
        )

    def test_the_uuid_is_stable_per_chat_and_day_and_differs_across_both(self) -> None:
        from lingxi.adapters.feishu_group_message import DELIVERY_UUID_MAX_LENGTH, delivery_uuid

        same = delivery_uuid(FAKE_CHAT_ID, "2026-08-06")

        self.assertEqual(same, delivery_uuid(FAKE_CHAT_ID, "2026-08-06"), "同群同日必须完全相同")
        self.assertNotEqual(same, delivery_uuid(FAKE_CHAT_ID, "2026-08-07"), "跨日必须不同")
        self.assertNotEqual(same, delivery_uuid("oc_another_group", "2026-08-06"), "跨群必须不同")
        self.assertLessEqual(len(same), DELIVERY_UUID_MAX_LENGTH, "飞书对 uuid 的长度上限是 50")
        # 群 ID 是外部标识，不该原样出现在请求体的第二个位置。
        self.assertNotIn(FAKE_CHAT_ID, same)

    def test_two_sends_on_the_same_day_carry_the_same_uuid_and_the_next_day_does_not(self) -> None:
        bodies: list[dict] = []

        def transport(method: str, url: str, *, body=None, token=None):
            if "tenant_access_token" in url:
                return {"code": 0, "tenant_access_token": "t-fake"}
            bodies.append(body)
            return {"code": 0, "msg": "ok"}

        sender = self._sender(transport)
        sender.send_text(chat_id=FAKE_CHAT_ID, text="第一次", dedupe_key="2026-08-06")
        sender.send_text(chat_id=FAKE_CHAT_ID, text="同日重试", dedupe_key="2026-08-06")
        sender.send_text(chat_id=FAKE_CHAT_ID, text="第二天", dedupe_key="2026-08-07")

        self.assertEqual(bodies[0]["uuid"], bodies[1]["uuid"], "同日两次请求的 uuid 必须相同")
        self.assertNotEqual(bodies[1]["uuid"], bodies[2]["uuid"], "跨日的 uuid 必须不同")

    def test_a_timeout_on_the_way_back_retries_under_the_very_same_uuid(self) -> None:
        """不确定态的完整形状：POST 到达了飞书，响应超时，进程重试。"""

        bodies: list[dict] = []
        state = {"first": True}

        def transport(method: str, url: str, *, body=None, token=None):
            if "tenant_access_token" in url:
                return {"code": 0, "tenant_access_token": "t-fake"}
            bodies.append(body)
            if state["first"]:
                state["first"] = False
                # 飞书已经收下了，只是我们没听见回音。
                raise TimeoutError("响应回程超时")
            return {"code": 0, "msg": "ok"}

        sender = self._sender(transport)
        with self.assertRaises(TimeoutError):
            sender.send_text(chat_id=FAKE_CHAT_ID, text="同一份日报", dedupe_key="2026-08-06")
        sender.send_text(chat_id=FAKE_CHAT_ID, text="同一份日报", dedupe_key="2026-08-06")

        self.assertEqual(len(bodies), 2, "用例前提：确实发生了两次 POST")
        self.assertEqual(bodies[0]["uuid"], bodies[1]["uuid"], "重试必须复用同一个投递 uuid")

    def test_the_duty_reuses_one_dedupe_key_across_the_failed_attempt_and_the_retry(self) -> None:
        """职责这一层的落点：失败那一次与重试那一次共用同一个去重键。"""

        clock = FixedClock()
        sender = FakeSender(failures=1, error=TimeoutError("响应回程超时"))
        duty, _sender, _audit, _clock, _reader = build_duty(clock=clock, sender=sender)

        with self.assertLogs("lingxi.apps.scheduler", level="ERROR"):
            duty.run_once()
        duty.run_once()

        self.assertEqual(len(sender.attempts), 2, "用例前提：失败一次后确实重试了")
        self.assertEqual(
            sender.attempts[0]["dedupe_key"],
            sender.attempts[1]["dedupe_key"],
            "同一天的重试必须复用同一个去重键",
        )
        self.assertEqual(sender.attempts[0]["dedupe_key"], clock.today.isoformat())

    def test_a_new_day_gets_a_new_dedupe_key(self) -> None:
        clock = FixedClock()
        duty, sender, _audit, _clock, _reader = build_duty(clock=clock)

        duty.run_once()
        first_day = clock.today.isoformat()
        clock.advance()
        duty.run_once()

        self.assertEqual([payload["dedupe_key"] for payload in sender.payloads], [first_day, clock.today.isoformat()])


class ChatIdValidationTest(unittest.TestCase):
    """群 ID 可选；配了但格式不对＝错配，快速失败。"""

    def test_an_absent_variable_leaves_the_configuration_valid(self) -> None:
        config = SchedulerConfig.from_env(COMPLETE_ENV)

        self.assertIsNone(config.admin_group_chat_id)

    def test_a_wellformed_variable_is_accepted(self) -> None:
        config = SchedulerConfig.from_env({**COMPLETE_ENV, "LINGXI_ADMIN_GROUP_CHAT_ID": FAKE_CHAT_ID})

        self.assertEqual(config.admin_group_chat_id, FAKE_CHAT_ID)

    def test_a_malformed_variable_fails_fast_without_echoing_the_value(self) -> None:
        # 刻意不把裸前缀 `oc_` 放进这一组：它本身就出现在错误消息里（消息要说明期望的
        # 形状），断言"不回显"会被它误报。裸前缀由下一个用例单独验。
        for bad in ("ou_this_is_a_user_not_a_group", "oc_has space", "just-wrong", "  "):
            with self.subTest(value=bad):
                if bad.strip():
                    with self.assertRaises(ValueError) as raised:
                        SchedulerConfig.from_env({**COMPLETE_ENV, "LINGXI_ADMIN_GROUP_CHAT_ID": bad})

                    message = str(raised.exception)
                    self.assertIn("LINGXI_ADMIN_GROUP_CHAT_ID", message)
                    self.assertNotIn(bad, message, "错误消息不得回显取到的值")
                else:
                    # 纯空白＝没配，走"可选"那条路，不是错配。
                    config = SchedulerConfig.from_env({**COMPLETE_ENV, "LINGXI_ADMIN_GROUP_CHAT_ID": bad})
                    self.assertIsNone(config.admin_group_chat_id)

    def test_the_bare_prefix_alone_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SchedulerConfig.from_env({**COMPLETE_ENV, "LINGXI_ADMIN_GROUP_CHAT_ID": "oc_"})


@unittest.skipUnless(
    importlib.util.find_spec("psycopg") and importlib.util.find_spec("cryptography"),
    "跳过：build_loop 会真的构造凭据保管与清理适配器，需要 psycopg 与 cryptography",
)
class DutyRegistrationTest(unittest.TestCase):
    """V-花名册-29：前置缺项 → 职责不注册、进程照常启动、审计恰 1 条、不回显值。

    S-B-04 起前置从两个变成四个（群 ID、Base ``app_token``、``table_id``、读取令牌
    供给），且**全部由配置驱动**——此前"花名册读取未接线"是装配里写死的 ``None``，
    一个把配置全填对的部署也永远注册不上这个职责。

    **「恰 1 条」的范围是花名册那一条**（#156 / S-C-03a、S-C-03b 起）：同一个 ``build_loop``
    现在还会为每日权限重算与权限发布各留一条自己的未注册审计（缺 MCP 令牌主密钥、缺发布表
    配置与发布表令牌供给）。断言因此按
    ``roster_audit.`` 前缀取本职责的审计——`V-花名册-29` 要挡的是"一个什么都没配的部署
    为**花名册**刷出四条审计"，不是"整个进程只许有一条审计"。别的职责的留痕由它自己的
    用例（``tests/test_permission_refresh_duty.py``）钉住。
    """

    def _roster_records(self, audit: RecordingAudit) -> list[tuple[str, dict]]:
        """只取花名册审计日报职责留下的那些审计行。"""

        return [record for record in audit.records if record[0].startswith("roster_audit.")]

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

    def test_without_the_group_variable_the_process_still_assembles_its_other_duties(self) -> None:
        audit = RecordingAudit()

        loop = build_loop(self._config(), audit=audit)

        self.assertEqual(
            [duty.name for duty in loop.duties],
            # 组织快照同步（Issue #250）的两个令牌供给与花名册配置无关，`build_loop`
            # 总能建出默认供给，因此总会真实注册；迟到就绪恢复（V-开通-18）、开通
            # 中途停摆收口（Issue #282，V-开通-19）与文档投递死信扫描（Issue #341
            # R-2）都没有可选前置会让它们整体不装配，因此也总会真实注册，排在最后。
            [
                "凭据轮换",
                "保留清理",
                "空闲会话清理",
                "权限链到期清理",
                "内测采集到期删除",
                "组织快照同步",
                "迟到就绪恢复",
                "开通中途停摆收口",
                "文档投递死信扫描与正文到期擦除",
            ],
        )
        roster_records = self._roster_records(audit)
        self.assertEqual(len(roster_records), 1, "缺群 ID 时花名册职责的审计恰 1 条")
        action, fields = roster_records[0]
        self.assertEqual(action, "roster_audit.duty_not_registered")
        self.assertEqual(fields["variable"], "LINGXI_ADMIN_GROUP_CHAT_ID")
        self.assertNotIn("value", fields, "审计里不得回显变量的值")

    def test_each_missing_roster_variable_is_named_one_at_a_time(self) -> None:
        """Base 与表标识各自缺失时都要指名，且**只报第一个缺的那个**。

        逐条报会让一个什么都没配的部署一次刷出四条审计，反而看不出该先配哪个；
        `V-花名册-29` 要的也正是「恰 1 条」。
        """

        cases = (
            ({}, "LINGXI_ROSTER_BITABLE_APP_TOKEN"),
            ({"LINGXI_ROSTER_BITABLE_APP_TOKEN": "bascnFakeAppToken"}, "LINGXI_ROSTER_BITABLE_TABLE_ID"),
        )
        for extra, expected in cases:
            with self.subTest(variable=expected):
                audit = RecordingAudit()

                loop = build_loop(
                    self._config(LINGXI_ADMIN_GROUP_CHAT_ID=FAKE_CHAT_ID, **extra), audit=audit
                )

                self.assertEqual(
                    [duty.name for duty in loop.duties],
                    [
                        "凭据轮换",
                        "保留清理",
                        "空闲会话清理",
                        "权限链到期清理",
                        "内测采集到期删除",
                        # 内测每日通报（Issue #303 S-O-01）：唯一前置是管理群
                        # chat_id，与花名册 Base 坐标无关——本夹具配了 chat_id、
                        # 缺花名册那两个变量，因此本职责照常注册，花名册那个不注册。
                        "内测每日通报",
                        "组织快照同步",
                        "迟到就绪恢复",
                        "开通中途停摆收口",
                        "文档投递死信扫描与正文到期擦除",
                    ],
                )
                roster_records = self._roster_records(audit)
                self.assertEqual(len(roster_records), 1, "前置缺项时花名册职责的审计恰 1 条")
                action, fields = roster_records[0]
                self.assertEqual(action, "roster_audit.duty_not_registered")
                self.assertEqual(fields["reason"], "missing_environment_variable")
                self.assertEqual(fields["variable"], expected)
                self.assertNotIn("value", fields, "审计里不得回显变量的值")

    def test_with_the_table_configured_the_built_in_token_supply_registers_the_duty(self) -> None:
        """R3 已消解（Issue #215 主接线）：三个环境变量配齐即注册。

        令牌供给不再是"整条链上唯一没有安全实现的部件"——`build_loop` 自己建出一条：
        凭据轮换职责按需换一次、把派生短期令牌放进进程内持有者，日报侧按新鲜度取用。
        **这条断言是"注册完全由配置驱动"的证据**（`V-花名册-29` 的后半句）：装配里不再
        有一个永远不满足的前置。
        """

        audit = RecordingAudit()

        loop = build_loop(
            self._config(
                LINGXI_ADMIN_GROUP_CHAT_ID=FAKE_CHAT_ID,
                LINGXI_ROSTER_BITABLE_APP_TOKEN="bascnFakeAppToken",
                LINGXI_ROSTER_BITABLE_TABLE_ID="tblFakeTable",
            ),
            audit=audit,
        )

        self.assertEqual(
            [duty.name for duty in loop.duties],
            [
                "凭据轮换",
                "保留清理",
                "空闲会话清理",
                "权限链到期清理",
                "内测采集到期删除",
                "花名册审计日报",
                # 内测每日通报（Issue #303 S-O-01）：唯一前置同样是管理群 chat_id，
                # 本夹具已配好，因此总会真实注册，紧跟花名册那一组之后。
                "内测每日通报",
                "组织快照同步",
                "迟到就绪恢复",
                "开通中途停摆收口",
                "文档投递死信扫描与正文到期擦除",
            ],
        )
        # 按 ``roster_audit.`` 前缀取本职责的审计：同一个 `build_loop` 还会为每日权限重算
        # 与权限发布各留一条自己的未注册审计（本夹具没配 MCP 主密钥与发布表）。
        self.assertEqual(self._roster_records(audit), [], "前置齐备时不该有『未注册』审计")

    def test_a_missing_token_supply_is_distinguishable_from_a_failing_one(self) -> None:
        """R3 的语义修正：「没有供给」与「有供给但拿不到令牌」必须可分辨。

        前者是装配问题、职责不注册、审计指名原因；后者是运行期的授权问题、职责照常
        注册、失败按分类审计。压成同一条会让排障走错方向——这正是 R3 当初宁可显式
        不注册、也不装一个每轮都炸的假读取的理由。
        """

        from lingxi.apps.scheduler import _build_roster_audit_duty

        config = self._config(
            LINGXI_ADMIN_GROUP_CHAT_ID=FAKE_CHAT_ID,
            LINGXI_ROSTER_BITABLE_APP_TOKEN="bascnFakeAppToken",
            LINGXI_ROSTER_BITABLE_TABLE_ID="tblFakeTable",
        )

        missing_audit = RecordingAudit()
        missing = _build_roster_audit_duty(
            config, stop=threading.Event(), audit=missing_audit, roster_access_token=None
        )

        self.assertIsNone(missing, "没有供给时不注册")
        self.assertEqual(
            [action for action, _ in missing_audit.records], ["roster_audit.duty_not_registered"]
        )
        self.assertEqual(missing_audit.records[0][1]["reason"], "missing_access_token_supply")

        failing_audit = RecordingAudit()

        def always_failing() -> str:
            raise AccessTokenUnavailable("no_credential_available")

        registered = _build_roster_audit_duty(
            config,
            stop=threading.Event(),
            audit=failing_audit,
            roster_access_token=always_failing,
        )

        self.assertIsNotNone(registered, "供给存在但会失败时，职责照常注册")
        self.assertEqual(failing_audit.records, [], "运行期失败不产生『未注册』审计")

    def test_with_every_prerequisite_the_duty_is_registered_by_the_existing_entry_point(self) -> None:
        """"谁会调用它"的落点：日报职责由**已存在**的 `lingxi-scheduler` 进程装配。

        `build_loop` 是 `main()` 唯一的装配入口，因此这条断言就是"读取、快照、比对、
        渲染、发送整条链真的有调用方"的证据。装配过程不发任何请求：令牌供给这里没被
        调用过一次（`V-花名册-27` 的同一条口径）。
        """

        audit = RecordingAudit()
        token_calls: list[int] = []

        loop = build_loop(
            self._config(
                LINGXI_ADMIN_GROUP_CHAT_ID=FAKE_CHAT_ID,
                LINGXI_ROSTER_BITABLE_APP_TOKEN="bascnFakeAppToken",
                LINGXI_ROSTER_BITABLE_TABLE_ID="tblFakeTable",
            ),
            roster_access_token=lambda: token_calls.append(1) or "u-fake-token",
            audit=audit,
        )

        self.assertEqual(
            [duty.name for duty in loop.duties],
            [
                "凭据轮换",
                "保留清理",
                "空闲会话清理",
                "权限链到期清理",
                "内测采集到期删除",
                "花名册审计日报",
                # 内测每日通报（Issue #303 S-O-01）：唯一前置同样是管理群 chat_id，
                # 本夹具已配好，因此总会真实注册，紧跟花名册那一组之后。
                "内测每日通报",
                "组织快照同步",
                "迟到就绪恢复",
                "开通中途停摆收口",
                "文档投递死信扫描与正文到期擦除",
            ],
        )
        self.assertEqual(self._roster_records(audit), [], "前置齐备时不该有『未注册』审计")
        self.assertEqual(token_calls, [], "装配阶段不得取令牌，更不得发请求")
        # 一个停止标志贯穿全部职责。
        loop.request_stop()
        self.assertTrue(all(duty.stopping for duty in loop.duties))


# --------------------------------------------------------------------------
# 七、审计与日志
# --------------------------------------------------------------------------


class AuditTest(unittest.TestCase):
    """V-花名册-32：日报动作经 AuditSink 记审计。V-花名册-33：审计与日志不含资料值。"""

    def test_the_report_action_goes_through_the_audit_sink(self) -> None:
        duty, _sender, audit, clock, _reader = build_duty()

        duty.run_once()

        self.assertEqual(len(audit.records), 1)
        action, fields = audit.records[0]
        self.assertEqual(action, "roster_audit.report_sent")
        self.assertEqual(fields["report_date"], clock.today.isoformat())
        self.assertEqual(fields["entries"], 1)
        self.assertEqual(fields["examined"], 1)

    def test_neither_the_audit_records_nor_the_logs_carry_any_roster_value(self) -> None:
        baseline = [
            ArchivedIdentity(USER_ONE, PERSON_ONE, NAME, EMPLOYEE_NO, EMAIL),
            ArchivedIdentity(USER_TWO, PERSON_TWO, "李四", "E1002", "lisi@example.com"),
        ]
        rows = [
            {"personnel_id": PERSON_ONE, "name": NEW_NAME, "employee_no": EMPLOYEE_NO, "email": NEW_EMAIL},
            {"personnel_id": PERSON_TWO, "name": "李四", "employee_no": "E1002", "email": "lisi@example.com"},
        ]
        duty, _sender, audit, _clock, _reader = build_duty(baseline=baseline, rows=rows)

        with self.assertLogs("lingxi.apps.scheduler", level="INFO") as captured:
            duty.run_once()

        haystack = repr(audit.records) + "\n" + "\n".join(captured.output)
        for value in (NAME, EMPLOYEE_NO, EMAIL, NEW_NAME, NEW_EMAIL, "李四", "E1002", "lisi@example.com"):
            with self.subTest(value=value):
                self.assertNotIn(value, haystack, f"审计与日志里不得出现资料值：{value}")
        # 外部标识同样不进审计与日志的明文。
        self.assertNotIn(PERSON_ONE, haystack)
        self.assertNotIn(PERSON_TWO, haystack)

    def test_the_default_sink_writes_one_sorted_structured_line(self) -> None:
        sink = StructuredLogAuditSink()

        with self.assertLogs("lingxi.apps.scheduler", level="INFO") as captured:
            sink.record("roster_audit.report_sent", entries=2, examined=9, report_date="2026-08-06")

        self.assertEqual(len(captured.output), 1)
        line = captured.output[0]
        self.assertIn("action=roster_audit.report_sent", line)
        # 字段按键名排序：顺序随 PYTHONHASHSEED 变化的审计行没法稳定断言。
        self.assertLess(line.index("entries="), line.index("examined="))
        self.assertLess(line.index("examined="), line.index("report_date="))


class RedactedIdentifierUsageTest(unittest.TestCase):
    """V-花名册-34：`redact_identifier()` 的返回值只能进日志。

    它的 docstring 写明返回值**不可反查也不可比较**（6 位前缀在 710 人里有 57 组碰撞）。
    拿它当键去比较、去重或定位，会把"看起来一样"当成"就是同一个人"。
    """

    def _redaction_calls(self, tree: ast.AST) -> tuple[list[ast.Call], list[ast.Call]]:
        every: list[ast.Call] = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "redact_identifier"
        ]
        inside_logging: list[ast.Call] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            target = node.func.value
            if not (isinstance(target, ast.Name) and target.id in {"logger", "logging"}):
                continue
            for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
                inside_logging.extend(
                    child
                    for child in ast.walk(argument)
                    if isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                    and child.func.id == "redact_identifier"
                )
        return every, inside_logging

    def test_every_redaction_call_in_the_runtime_sources_is_a_logging_argument(self) -> None:
        checked = 0
        for path in sorted(SOURCE_ROOT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            every, inside_logging = self._redaction_calls(tree)
            if not every:
                continue
            checked += len(every)
            stray = [node for node in every if node not in inside_logging]
            self.assertEqual(
                [node.lineno for node in stray],
                [],
                f"{path.relative_to(REPOSITORY_ROOT)}：脱敏标识只能作为日志参数使用",
            )

        # 反向自检：这条扫描确实扫到了东西，不是因为一处调用都没找到才绿的。
        self.assertGreaterEqual(checked, 3)

    def test_the_report_body_never_uses_the_redacted_form(self) -> None:
        report_source = (SOURCE_ROOT / "lingxi" / "core" / "identity" / "roster_report.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(report_source)
        every, _inside = self._redaction_calls(tree)

        self.assertEqual(every, [], "日报渲染不得调用 redact_identifier（裁定 C1）")


if __name__ == "__main__":
    unittest.main()
