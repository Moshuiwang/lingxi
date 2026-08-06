"""花名册审计日报职责、群发适配与零迁移守卫（Issue #52 / W4-B）。

认领断言：V-花名册-13、16、17、18、19、20、25、26、27、28、29、30、31、32、33、34。

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
from datetime import date, datetime, timedelta, timezone

from lingxi.apps.scheduler import (
    RosterAuditDuty,
    SchedulerConfig,
    SchedulerLoop,
    StructuredLogAuditSink,
    build_loop,
)
from lingxi.core.identity.roster_audit import ArchivedIdentity, DiffKind

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
    """记录每一次发送的完整载荷。失败次数可控，用来验重试与"不算已发送"。"""

    def __init__(self, *, failures: int = 0) -> None:
        self._failures = failures
        self.payloads: list[dict[str, str]] = []

    def send_text(self, *, chat_id: str, text: str) -> None:
        if self._failures > 0:
            self._failures -= 1
            raise RuntimeError(f"模拟发送失败，正文里有资料值 {EMAIL}")
        self.payloads.append({"chat_id": chat_id, "text": text})


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
        return datetime(self.today.year, self.today.month, self.today.day, 9, 0, tzinfo=timezone.utc)

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
) -> tuple[RosterAuditDuty, FakeSender, RecordingAudit, FixedClock, FakeBaselineReader]:
    sender = sender or FakeSender()
    audit = audit or RecordingAudit()
    clock = clock or FixedClock()
    reader = reader or FakeBaselineReader(baseline if baseline is not None else baseline_of_one())
    rows = rows if rows is not None else changed_rows()
    duty = RosterAuditDuty(
        baseline_reader=reader,
        roster_reader=lambda: rows,
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
    """V-花名册-13：本切片**不新增任何 alembic revision**。

    「零新表」是产品负责人 2026-08-06 的定案（选项 A），这条测试把那个定案变成一个
    **会变红的检查**——否则「不新增表」只是 PR 正文里的一句话，下一个人加一张表时
    没有任何东西会拦住他。

    revision 图用 AST 解析，不 import alembic：这条守卫必须在没装 alembic 的环境里
    照样跑，跳过等于没有守卫。
    """

    VERSIONS_DIRECTORY = REPOSITORY_ROOT / "migrations" / "alembic" / "versions"
    MIGRATIONS_DIRECTORY = REPOSITORY_ROOT / "migrations"

    # 基线 `0ae6991`（含 #53 Alembic 链与 #54 保留清理）的状态。
    # **后续切片若确有新表，改这里的同时必须在 PR 里说明推翻了 #52 的定案 A。**
    EXPECTED_VERSION_FILES = frozenset({"0054_retention_cleanup.py", "20260806_baseline_006_012.py"})
    EXPECTED_HEAD = "0054_retention_cleanup"
    # 编号 SQL 链（`migrations/*.sql`）自 #53 起已被冻结，逐个文件名的断言在
    # `tests/test_postgres_schema_fixture.py` 里已经有一份，这里不再重复一遍——
    # 而且那份守卫禁止其他测试文件写死编号 SQL 的文件名（它自己是唯一豁免）。
    # 本类只负责 revision 侧：**不新增 revision** 才是 #52 定案 A 的落点。
    EXPECTED_NUMBERED_SQL_COUNT = 6

    def _revision_graph(self) -> dict[str, object]:
        graph: dict[str, object] = {}
        for path in sorted(self.VERSIONS_DIRECTORY.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            values: dict[str, object] = {}
            for node in tree.body:
                # 两种写法都认：`revision = "..."` 与带标注的 `revision: str = "..."`。
                # 现网两个 revision 文件用的都是后者（`ast.AnnAssign`）——只认前者的话，
                # 这条守卫会因为一条都没解析到而恒绿。
                if isinstance(node, ast.AnnAssign):
                    if isinstance(node.target, ast.Name) and node.target.id in {"revision", "down_revision"}:
                        values[node.target.id] = ast.literal_eval(node.value) if node.value else None
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                            values[target.id] = ast.literal_eval(node.value)
            self.assertIn("revision", values, f"{path.name} 缺少 revision 标识")
            graph[str(values["revision"])] = values.get("down_revision")
        return graph

    def test_no_new_alembic_revision_file_was_added_by_this_slice(self) -> None:
        present = {path.name for path in self.VERSIONS_DIRECTORY.glob("*.py")}

        self.assertEqual(
            present,
            self.EXPECTED_VERSION_FILES,
            "本切片定案为零新表零迁移；新增或删除 revision 文件必须先推翻 Issue #52 的定案 A",
        )

    def test_the_revision_chain_still_has_exactly_one_unchanged_head(self) -> None:
        graph = self._revision_graph()
        parents = {parent for parent in graph.values() if parent is not None}
        heads = sorted(set(graph) - {str(parent) for parent in parents})

        self.assertEqual(heads, [self.EXPECTED_HEAD], "revision head 必须与基线一致，且恰好 1 个")

    def test_no_new_numbered_sql_file_was_added_by_this_slice(self) -> None:
        """只数个数，不列文件名：列名字会撞上 `test_postgres_schema_fixture.py` 那条
        「真库用例不得写死生产迁移文件名」的守卫，而那条守卫是对的。"""

        present = list(self.MIGRATIONS_DIRECTORY.glob("*.sql"))

        self.assertEqual(len(present), self.EXPECTED_NUMBERED_SQL_COUNT, "本切片不新增编号 SQL 迁移")

    def test_the_slice_declares_no_new_table_anywhere_in_its_own_sources(self) -> None:
        """新代码里不得出现建表语句。零新表要在源码层面也站得住。"""

        for module in ("adapters/postgres_roster_audit.py", "adapters/feishu_group_message.py"):
            text = (SOURCE_ROOT / "lingxi" / module).read_text(encoding="utf-8").upper()
            with self.subTest(module=module):
                for statement in ("CREATE TABLE", "ALTER TABLE", "DROP TABLE"):
                    self.assertNotIn(statement, text)


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
    """V-花名册-17 / 18：职责间失败隔离；连续失败不退进程；日志只记异常类型。"""

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
            def send_text(self, *, chat_id, text):
                if loop.stopping:
                    state["sends_after_stop"] += 1
                state["send_started"] += 1
                time.sleep(0.4)              # 在途发送：SIGTERM 到达时正在做
                state["send_completed"] += 1

        class Audit:
            def record(self, action, /, **fields):
                pass

        # 每轮换一天，让同日判重不挡住后续轮次。
        days = itertools.count()
        base = datetime(2026, 8, 6, 9, 0, tzinfo=timezone.utc)

        # 一个停止标志显式贯穿职责与循环——这正是 SIGTERM 要验的结构本身，
        # 不从职责内部把私有标志掏出来。
        stop = threading.Event()

        duty = RosterAuditDuty(
            baseline_reader=Baseline(),
            roster_reader=lambda: [{"personnel_id": "ou_p1", "name": "花名册姓名",
                                    "employee_no": "E1001", "email": "archived@example.com"}],
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
        source = (SOURCE_ROOT / "lingxi" / "apps" / "scheduler" / "__init__.py").read_text(encoding="utf-8")
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

        零新表定案下没有持久载体，跨重启的真幂等做不到（裁定 C2 / R2 知情接受）。
        能被用例证明、也确实值得保证的是：那次重发不是一份**不同的**日报。
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
    """V-花名册-27 / 28：出站可注入；群 ID 只从环境变量来。"""

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

        sender.send_text(chat_id=FAKE_CHAT_ID, text="脱敏日报正文")

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
        from lingxi.adapters.feishu_group_message import FeishuGroupMessageError, FeishuGroupMessages

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
            sender.send_text(chat_id=FAKE_CHAT_ID, text="脱敏日报正文")

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
        source = (SOURCE_ROOT / "lingxi" / "apps" / "scheduler" / "__init__.py").read_text(encoding="utf-8")

        self.assertIn("LINGXI_ADMIN_GROUP_CHAT_ID", source)
        self.assertIn("LINGXI_ADMIN_GROUP_CHAT_ID", SchedulerConfig.ENVIRONMENT_KEYS)


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
    """V-花名册-29：缺群 ID → 职责不注册、进程照常启动、审计恰 1 条、不回显值。"""

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

        self.assertEqual([duty.name for duty in loop.duties], ["凭据轮换", "保留清理"])
        self.assertEqual(len(audit.records), 1, "缺群 ID 时审计恰 1 条")
        action, fields = audit.records[0]
        self.assertEqual(action, "roster_audit.duty_not_registered")
        self.assertEqual(fields["variable"], "LINGXI_ADMIN_GROUP_CHAT_ID")
        self.assertNotIn("value", fields, "审计里不得回显变量的值")

    def test_with_the_group_variable_but_no_roster_reader_the_duty_is_still_not_registered(self) -> None:
        """R3：真实花名册读取的凭据与 Base ID 属 L4a 前置。

        这里显式不注册并留痕，而不是装一个每轮都炸的假读取——后者会把「还没接线」
        伪装成「接线了但一直失败」。
        """

        audit = RecordingAudit()

        loop = build_loop(self._config(LINGXI_ADMIN_GROUP_CHAT_ID=FAKE_CHAT_ID), audit=audit)

        self.assertEqual([duty.name for duty in loop.duties], ["凭据轮换", "保留清理"])
        self.assertEqual([action for action, _ in audit.records], ["roster_audit.duty_not_registered"])
        self.assertEqual(audit.records[0][1]["reason"], "roster_reader_unwired")

    def test_with_both_prerequisites_the_duty_is_registered_by_the_existing_entry_point(self) -> None:
        """"谁会调用它"的落点：日报职责由**已存在**的 `lingxi-scheduler` 进程装配。

        `build_loop` 是 `main()` 唯一的装配入口，因此这条断言就是"新增的比对、渲染、
        发送三段真的有调用方"的证据。
        """

        audit = RecordingAudit()

        class PageReader:
            def list_records(self, page_token=None):
                return ([], None)

        loop = build_loop(
            self._config(LINGXI_ADMIN_GROUP_CHAT_ID=FAKE_CHAT_ID),
            roster_page_reader=PageReader(),
            audit=audit,
        )

        self.assertEqual([duty.name for duty in loop.duties], ["凭据轮换", "保留清理", "花名册审计日报"])
        self.assertEqual(audit.records, [], "前置齐备时不该有『未注册』审计")
        # 一个停止标志贯穿全部职责。
        loop.request_stop()
        self.assertTrue(all(duty.stopping for duty in loop.duties))


# --------------------------------------------------------------------------
# 七、审计与日志
# --------------------------------------------------------------------------


class AuditTest(unittest.TestCase):
    """V-花名册-32 / 33：日报动作经 AuditSink 记审计；审计与日志不含资料值。"""

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
