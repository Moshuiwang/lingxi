"""花名册快照写入职责 :class:`RosterSnapshotSyncDuty` 与它跟
:class:`~lingxi.apps.scheduler.roster_audit.RosterAuditDuty` 的互斥装配（Issue #275）。

根因：`roster_snapshot` 表是首次开通链第二步（``core/identity/onboarding_runner.py::
_match``）与每日权限重算的共同数据前提，但此前"写快照"被绑在管理群审计日报职责
内部——三个前置（管理群 chat_id、花名册 Base ``app_token``/``table_id``）任缺其一
都不写，导致一个纯通知配置（管理群）决定"员工能不能被开通"（2026-08-21 首触冒烟
实测坐实：``roster_snapshot`` 为 0 行，三变量全部未声明）。

拆法：把"写"从 `RosterAuditDuty` 里解出来，独立成只依赖 Base 坐标 + 令牌供给的
`RosterSnapshotSyncDuty`；两个职责在装配层**互斥**——管理群与 Base 坐标都齐全时，
`RosterAuditDuty` 仍然一次性完成"读→写→比对→发"（零变化，行为回归见
`RegressionWhenBothConfiguredTests`）；`RosterAuditDuty` 因任何原因未装配时，才尝试
单独装配 `RosterSnapshotSyncDuty`。互斥保证同一时刻至多一个职责触发花名册读取，
不给一次性 ``refresh_token`` 的唯一消费者纪律增加"今天到底读了几次"的分辨负担。

本文件覆盖：

- 单元：`RosterSnapshotSyncDuty` 自己的日水位、停止语义、写入失败不吞（一、二节）；
- 装配：`_build_roster_snapshot_sync_duty` 的前置判定与可分辨的审计原因码（三节）；
- 集成：`build_loop` 层面的互斥注册、零发送与凭据消费安全（四节）。

`RosterAuditDuty` 自身既有的全部行为断言不受影响，见 `tests/test_roster_audit_duty.py`
——本次改动没有修改那个类的任何一行。
"""

from __future__ import annotations

import importlib.util
import pathlib
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from lingxi.apps.scheduler import (
    RosterAuditDuty,
    RosterSnapshotSyncDuty,
    SchedulerConfig,
    SchedulerLoop,
    _build_roster_audit_duty,
    _build_roster_snapshot_sync_duty,
    build_loop,
)
from lingxi.core.identity.roster_snapshot import RosterRound, RosterSnapshotStatus

REPOSITORY_ROOT = pathlib.Path(__file__).parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"

FAKE_CHAT_ID = "oc_fake_admin_group_for_tests"
FAKE_APP_TOKEN = "bascnFakeAppToken"
FAKE_TABLE_ID = "tblFakeTable"

COMPLETE_ENV = {
    "LINGXI_POSTGRES_DSN": "postgresql://user@localhost:5432/lingxi",
    "LINGXI_DELEGATED_CREDENTIAL_KEY": "ZmFrZS1mZXJuZXQta2V5LWZvci11bml0LXRlc3RzLTA9",
    "LINGXI_DELEGATED_CREDENTIAL_PATH": "/var/lib/lingxi/credentials/delegated.enc",
    "LINGXI_FEISHU_APP_ID": "cli_fake",
    "LINGXI_FEISHU_APP_SECRET": "secret_fake",
}


class RecordingAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, object]]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, dict(fields)))

    def actions(self) -> list[str]:
        return [action for action, _ in self.records]


class FakeRosterSource:
    """一轮花名册取用的可注入替身：交出固定的 `RosterRound`，记录被调用的次数。"""

    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.calls: list[datetime] = []

    def current(self, *, now: datetime) -> RosterRound:
        self.calls.append(now)
        if self._error is not None:
            raise self._error
        snapshot = RosterSnapshotStatus(
            action="replace",
            read_status="complete",
            stale_after_seconds=172800.0,
            captured_at=now,
            row_count=1206,
            age_seconds=0.0,
        )
        return RosterRound((), snapshot)


class FixedClock:
    def __init__(self, start: date = date(2026, 8, 21)) -> None:
        self.today = start

    def __call__(self) -> datetime:
        return datetime(self.today.year, self.today.month, self.today.day, 9, 0, tzinfo=timezone.utc)

    def advance(self, days: int = 1) -> None:
        self.today = self.today + timedelta(days=days)


# --------------------------------------------------------------------------
# 一、RosterSnapshotSyncDuty 自身行为
# --------------------------------------------------------------------------


class DailyWatermarkTest(unittest.TestCase):
    """要求 5：同一天内多次 tick 不会重复发起花名册读取；换一天会再读一次。"""

    def test_the_same_day_across_many_ticks_reads_exactly_once(self) -> None:
        clock = FixedClock()
        source = FakeRosterSource()
        duty = RosterSnapshotSyncDuty(roster_source=source, clock=clock)

        for _round in range(5):
            duty.run_once()

        self.assertEqual(len(source.calls), 1, "同一天多次 tick 只应触发一次花名册读取")
        self.assertEqual(duty.completed_on, clock.today)

    def test_a_new_day_triggers_a_fresh_read(self) -> None:
        clock = FixedClock()
        source = FakeRosterSource()
        duty = RosterSnapshotSyncDuty(roster_source=source, clock=clock)

        duty.run_once()
        clock.advance()
        duty.run_once()

        self.assertEqual(len(source.calls), 2, "换一天应当再触发一次读取")

    def test_the_loop_actually_drives_the_duty_each_round(self) -> None:
        clock = FixedClock()
        source = FakeRosterSource()
        duty = RosterSnapshotSyncDuty(roster_source=source, clock=clock)
        loop = SchedulerLoop(duties=(duty,), interval_seconds=0.01)

        for _round in range(3):
            loop.run_once()
            clock.advance()

        self.assertEqual(len(source.calls), 3, "三个不同的日子应当各自触发一次读取")


class StopSemanticsTest(unittest.TestCase):
    def test_a_stopping_duty_reads_nothing(self) -> None:
        stop = threading.Event()
        source = FakeRosterSource()
        duty = RosterSnapshotSyncDuty(roster_source=source, stop=stop)

        duty.request_stop()
        result = duty.run_once()

        self.assertTrue(duty.stopping)
        self.assertIsNone(result)
        self.assertEqual(source.calls, [], "停止后不得再触发新的花名册读取")

    def test_one_stop_signal_reaches_the_duty_together_with_others(self) -> None:
        stop = threading.Event()
        source = FakeRosterSource()
        duty = RosterSnapshotSyncDuty(roster_source=source, stop=stop)
        loop = SchedulerLoop(duties=(duty,), interval_seconds=0.01, stop=stop)

        loop.request_stop()
        loop.run_once()

        self.assertTrue(duty.stopping)
        self.assertEqual(source.calls, [])


class WriteFailureNotSwallowedTest(unittest.TestCase):
    """写入失败（`DailyRosterSource.current()` 内部的 `RosterSnapshotUpdater.apply()`
    写库失败）必须原样上抛、不置位水位，由 `SchedulerLoop` 做职责级隔离并在下一轮重试
    ——与 `RosterAuditDuty` 对 `roster_source.current()` 异常的既有处理同一条纪律。
    """

    def test_a_failing_round_propagates_and_leaves_the_watermark_unset(self) -> None:
        clock = FixedClock()
        source = FakeRosterSource(error=RuntimeError("模拟写库失败"))
        duty = RosterSnapshotSyncDuty(roster_source=source, clock=clock)

        with self.assertRaises(RuntimeError):
            duty.run_once()

        self.assertIsNone(duty.completed_on, "写入失败不得算作当天已完成")

    def test_a_failing_duty_does_not_take_down_other_duties_in_the_loop(self) -> None:
        clock = FixedClock()
        source = FakeRosterSource(error=RuntimeError("模拟写库失败"))
        duty = RosterSnapshotSyncDuty(roster_source=source, clock=clock)

        class RecordingDuty:
            def __init__(self, name: str) -> None:
                self.name = name
                self.calls = 0

            def run_once(self) -> str:
                self.calls += 1
                return f"{self.name}-ok"

        other = RecordingDuty("凭据轮换")
        loop = SchedulerLoop(duties=(other, duty), interval_seconds=0.01)

        with self.assertLogs("lingxi.apps.scheduler", level="ERROR") as captured:
            reports = loop.run_once()

        self.assertEqual(other.calls, 1, "快照同步炸掉不得带走其他职责")
        self.assertIsNone(reports[1])
        self.assertTrue(any("花名册快照同步" in line for line in captured.output))

        # 下一轮（同一天）会重试。
        source_ok = FakeRosterSource()
        duty._roster_source = source_ok  # noqa: SLF001 - 测试内部直接替换协作者
        loop.run_once()
        self.assertEqual(len(source_ok.calls), 1)
        self.assertEqual(duty.completed_on, clock.today)


class NoSenderCollaboratorTest(unittest.TestCase):
    """结构性证据：`RosterSnapshotSyncDuty` 根本不持有任何可以发消息的协作者
    ——不是"约束住不发"，而是"连发的能力都没有"。"""

    def test_the_class_source_never_references_a_sender_or_send_call(self) -> None:
        import ast

        source = (
            SOURCE_ROOT / "lingxi" / "apps" / "scheduler" / "roster_audit.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        duty_class = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == "RosterSnapshotSyncDuty"
        )
        called_attributes = {
            node.func.attr
            for node in ast.walk(duty_class)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        for forbidden in ("send_text", "send", "sender"):
            self.assertNotIn(forbidden, called_attributes, f"快照同步职责不得调用 {forbidden}")


# --------------------------------------------------------------------------
# 二、装配：_build_roster_snapshot_sync_duty 前置判定
# --------------------------------------------------------------------------


class PrerequisiteTests(unittest.TestCase):
    def _config(self, **extra: str) -> SchedulerConfig:
        return SchedulerConfig.from_env({**COMPLETE_ENV, **extra})

    def test_missing_app_token_is_reported_with_its_own_action_prefix(self) -> None:
        """要求 3：Base 坐标缺失 ⇒ 快照写入路径不装配，且审计原因码与「没配管理群」
        可分辨（不同的 action 前缀：`roster_snapshot_sync.` vs `roster_audit.`）。"""

        audit = RecordingAudit()
        duty = _build_roster_snapshot_sync_duty(
            self._config(LINGXI_ROSTER_BITABLE_TABLE_ID=FAKE_TABLE_ID),
            stop=threading.Event(),
            audit=audit,
            roster_access_token=lambda: "token",
        )

        self.assertIsNone(duty)
        self.assertEqual(audit.actions(), ["roster_snapshot_sync.duty_not_registered"])
        fields = audit.records[0][1]
        self.assertEqual(fields["reason"], "missing_environment_variable")
        self.assertEqual(fields["variable"], "LINGXI_ROSTER_BITABLE_APP_TOKEN")
        self.assertNotIn("value", fields)

    def test_missing_table_id_is_reported_after_app_token_is_present(self) -> None:
        audit = RecordingAudit()
        duty = _build_roster_snapshot_sync_duty(
            self._config(LINGXI_ROSTER_BITABLE_APP_TOKEN=FAKE_APP_TOKEN),
            stop=threading.Event(),
            audit=audit,
            roster_access_token=lambda: "token",
        )

        self.assertIsNone(duty)
        self.assertEqual(audit.records[0][1]["variable"], "LINGXI_ROSTER_BITABLE_TABLE_ID")

    def test_missing_token_supply_is_distinguishable_from_missing_environment_variable(self) -> None:
        """要求 4：令牌供给为 None ⇒ 维持既有语义（`missing_access_token_supply`），
        与「未配置变量」可分辨。"""

        audit = RecordingAudit()
        duty = _build_roster_snapshot_sync_duty(
            self._config(
                LINGXI_ROSTER_BITABLE_APP_TOKEN=FAKE_APP_TOKEN,
                LINGXI_ROSTER_BITABLE_TABLE_ID=FAKE_TABLE_ID,
            ),
            stop=threading.Event(),
            audit=audit,
            roster_access_token=None,
        )

        self.assertIsNone(duty)
        self.assertEqual(audit.actions(), ["roster_snapshot_sync.duty_not_registered"])
        fields = audit.records[0][1]
        self.assertEqual(fields["reason"], "missing_access_token_supply")
        self.assertNotIn("variable", fields)

    def test_with_both_base_coordinates_and_a_token_supply_the_duty_registers(self) -> None:
        """要求 1（前半）：只要 Base 坐标 + 令牌供给齐备，快照写入路径就被装配
        ——**不需要管理群 chat_id**。"""

        audit = RecordingAudit()
        duty = _build_roster_snapshot_sync_duty(
            self._config(
                LINGXI_ROSTER_BITABLE_APP_TOKEN=FAKE_APP_TOKEN,
                LINGXI_ROSTER_BITABLE_TABLE_ID=FAKE_TABLE_ID,
            ),
            stop=threading.Event(),
            audit=audit,
            roster_access_token=lambda: "token",
        )

        self.assertIsInstance(duty, RosterSnapshotSyncDuty)
        self.assertEqual(audit.actions(), [], "装配成功时不该留『未注册』审计")
        # 装配阶段本身不发任何请求（同 `V-花名册-27` 对 RosterAuditDuty 的同一条口径）。


# --------------------------------------------------------------------------
# 三、集成：build_loop 层面的互斥注册
# --------------------------------------------------------------------------


@unittest.skipUnless(
    importlib.util.find_spec("psycopg") and importlib.util.find_spec("cryptography"),
    "跳过：build_loop 会真的构造凭据保管与清理适配器，需要 psycopg 与 cryptography",
)
class MutualExclusionTests(unittest.TestCase):
    def _config(self, **extra: str) -> SchedulerConfig:
        from cryptography.fernet import Fernet

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return SchedulerConfig.from_env(
            {
                **COMPLETE_ENV,
                "LINGXI_DELEGATED_CREDENTIAL_KEY": Fernet.generate_key().decode(),
                "LINGXI_DELEGATED_CREDENTIAL_PATH": str(
                    pathlib.Path(directory.name) / "delegated.enc"
                ),
                **extra,
            }
        )

    def test_only_base_coordinates_registers_sync_but_not_audit_and_sends_nothing(self) -> None:
        """要求 1（完整）：只有 Base 坐标、没有管理群 ⇒ 快照写入路径被装配，日报不注册，
        且没有任何发送动作（用打了桩的群发适配器断言零调用）。"""

        audit = RecordingAudit()
        send_calls: list[tuple[object, ...]] = []

        def stub_send_text(self, *, chat_id, text, dedupe_key):  # noqa: ANN001
            send_calls.append((chat_id, text, dedupe_key))

        with mock.patch(
            "lingxi.adapters.feishu_group_message.FeishuGroupMessages.send_text",
            stub_send_text,
        ):
            loop = build_loop(
                self._config(
                    LINGXI_ROSTER_BITABLE_APP_TOKEN=FAKE_APP_TOKEN,
                    LINGXI_ROSTER_BITABLE_TABLE_ID=FAKE_TABLE_ID,
                ),
                roster_access_token=lambda: "token",
                audit=audit,
            )
            names = [duty.name for duty in loop.duties]
            self.assertIn("花名册快照同步", names)
            self.assertNotIn("花名册审计日报", names)

            # 跑几轮：即使职责被真的驱动，也不该有任何发送动作发生——这个职责
            # 结构上不持有任何 sender（NoSenderCollaboratorTest 已经证明这一点），
            # 这里补一条集成层面的可观察证据。
            for _round in range(3):
                loop.run_once()

        self.assertEqual(send_calls, [], "没有管理群配置时绝不能向任何群发送任何消息")

        roster_audit_records = [r for r in audit.records if r[0].startswith("roster_audit.")]
        self.assertEqual(len(roster_audit_records), 1)
        self.assertEqual(roster_audit_records[0][1]["variable"], "LINGXI_ADMIN_GROUP_CHAT_ID")

    def test_base_coordinates_missing_leaves_a_distinguishable_reason_from_missing_admin_group(
        self,
    ) -> None:
        """要求 3（build_loop 层面）：Base 坐标缺失时，`roster_audit.*` 与
        `roster_snapshot_sync.*` 两条审计都存在，且各自的 `variable` 字段不同，
        证明"没配管理群"与"花名册没接线"在这个部署形状下也能被分辨。"""

        audit = RecordingAudit()

        loop = build_loop(self._config(), audit=audit)

        names = [duty.name for duty in loop.duties]
        self.assertNotIn("花名册审计日报", names)
        self.assertNotIn("花名册快照同步", names)

        roster_audit_records = [r for r in audit.records if r[0] == "roster_audit.duty_not_registered"]
        sync_records = [r for r in audit.records if r[0] == "roster_snapshot_sync.duty_not_registered"]
        self.assertEqual(len(roster_audit_records), 1)
        self.assertEqual(len(sync_records), 1)
        self.assertEqual(roster_audit_records[0][1]["variable"], "LINGXI_ADMIN_GROUP_CHAT_ID")
        self.assertEqual(sync_records[0][1]["variable"], "LINGXI_ROSTER_BITABLE_APP_TOKEN")
        self.assertNotEqual(
            roster_audit_records[0][1].get("variable"), sync_records[0][1].get("variable")
        )

    def test_both_admin_group_and_base_coordinates_registers_only_the_audit_duty(self) -> None:
        """要求 2：管理群与 Base 坐标都有 ⇒ 与改动前行为一致（回归护栏）——只有
        `RosterAuditDuty` 注册，`RosterSnapshotSyncDuty` 完全不装配（互斥）。"""

        audit = RecordingAudit()

        loop = build_loop(
            self._config(
                LINGXI_ADMIN_GROUP_CHAT_ID=FAKE_CHAT_ID,
                LINGXI_ROSTER_BITABLE_APP_TOKEN=FAKE_APP_TOKEN,
                LINGXI_ROSTER_BITABLE_TABLE_ID=FAKE_TABLE_ID,
            ),
            roster_access_token=lambda: "token",
            audit=audit,
        )

        duties_by_name = {duty.name: duty for duty in loop.duties}
        self.assertIn("花名册审计日报", duties_by_name)
        self.assertNotIn("花名册快照同步", duties_by_name)
        self.assertIsInstance(duties_by_name["花名册审计日报"], RosterAuditDuty)

        roster_records = [r for r in audit.records if r[0].startswith(("roster_audit.", "roster_snapshot_sync."))]
        self.assertEqual(roster_records, [], "前置齐备时不该有任何『未注册』审计")

    def test_a_missing_token_supply_is_distinguishable_at_the_build_loop_level(self) -> None:
        """要求 4（build_loop 层面）：Base 坐标齐全但供给为 None 时（通过直接调用
        装配函数模拟"调用方没有交出任何供给"），两个职责各自的 `reason` 都是
        `missing_access_token_supply`，与"缺变量"的场景（上一条用例）在 `reason`
        字段上可分辨。"""

        audit = RecordingAudit()
        config = self._config(
            LINGXI_ADMIN_GROUP_CHAT_ID=FAKE_CHAT_ID,
            LINGXI_ROSTER_BITABLE_APP_TOKEN=FAKE_APP_TOKEN,
            LINGXI_ROSTER_BITABLE_TABLE_ID=FAKE_TABLE_ID,
        )

        roster_audit = _build_roster_audit_duty(
            config, stop=threading.Event(), audit=audit, roster_access_token=None
        )
        sync = _build_roster_snapshot_sync_duty(
            config, stop=threading.Event(), audit=audit, roster_access_token=None
        )

        self.assertIsNone(roster_audit)
        self.assertIsNone(sync)
        reasons = {action: fields["reason"] for action, fields in audit.records}
        self.assertEqual(reasons["roster_audit.duty_not_registered"], "missing_access_token_supply")
        self.assertEqual(reasons["roster_snapshot_sync.duty_not_registered"], "missing_access_token_supply")


# --------------------------------------------------------------------------
# 四、约束 3 的核实：互斥保证同一时刻至多一次花名册读取
# --------------------------------------------------------------------------


class SingleReaderPerDayTests(unittest.TestCase):
    """约束 3 的核心论证：因为两个职责互斥注册，装配层永远不会同时把两个都放进
    `duties` 列表——`build_loop` 的 `if/else` 结构本身保证了这一点（`else` 分支只在
    `roster_audit is None` 时才会被进入）。这里用一次真实装配加断言把这条结构性
    保证钉成一条会变红的用例：无论怎么配置，同一次 `build_loop` 调用产生的 `duties`
    里，"花名册审计日报"与"花名册快照同步"这两个名字不可能同时出现。
    """

    def test_the_two_duty_names_never_coexist_across_all_configuration_combinations(self) -> None:
        import itertools

        try:
            import cryptography  # noqa: F401
            import psycopg  # noqa: F401
        except ImportError:
            self.skipTest("需要 psycopg 与 cryptography")

        from cryptography.fernet import Fernet

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)

        for has_admin_group, has_base in itertools.product((False, True), repeat=2):
            with self.subTest(has_admin_group=has_admin_group, has_base=has_base):
                extra: dict[str, str] = {}
                if has_admin_group:
                    extra["LINGXI_ADMIN_GROUP_CHAT_ID"] = FAKE_CHAT_ID
                if has_base:
                    extra["LINGXI_ROSTER_BITABLE_APP_TOKEN"] = FAKE_APP_TOKEN
                    extra["LINGXI_ROSTER_BITABLE_TABLE_ID"] = FAKE_TABLE_ID

                config = SchedulerConfig.from_env(
                    {
                        **COMPLETE_ENV,
                        "LINGXI_DELEGATED_CREDENTIAL_KEY": Fernet.generate_key().decode(),
                        "LINGXI_DELEGATED_CREDENTIAL_PATH": str(
                            pathlib.Path(directory.name) / f"delegated-{has_admin_group}-{has_base}.enc"
                        ),
                        **extra,
                    }
                )
                loop = build_loop(config, roster_access_token=lambda: "token", audit=RecordingAudit())
                names = {duty.name for duty in loop.duties}

                self.assertFalse(
                    {"花名册审计日报", "花名册快照同步"} <= names,
                    "两个职责不得同时出现在同一次装配结果里",
                )
                if has_admin_group and has_base:
                    self.assertIn("花名册审计日报", names)
                elif has_base:
                    self.assertIn("花名册快照同步", names)
                else:
                    self.assertNotIn("花名册审计日报", names)
                    self.assertNotIn("花名册快照同步", names)


if __name__ == "__main__":
    unittest.main()
