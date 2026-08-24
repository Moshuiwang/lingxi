"""内测每日通报职责的装配断言（Issue #303 S-O-01）。

覆盖：唯一前置（管理群 chat_id）缺失时不注册、留恰一条审计、不回显值、其余职责
照常；前置齐备时通过 `build_loop` 真实注册并接上与花名册日报共用的告警出口。

职责本身的行为（判重、节流、送达失败）在 `tests/test_daily_report_duty.py`；
纯渲染与聚合在 `tests/test_daily_report_render.py`；真库读取在
`tests/test_postgres_daily_report.py`。
"""

from __future__ import annotations

import importlib.util
import pathlib
import threading
import unittest

from lingxi.apps.scheduler import SchedulerConfig, _build_daily_report_duty, build_loop
from lingxi.apps.scheduler.daily_report import DailyReportDuty

REPOSITORY_ROOT = pathlib.Path(__file__).parents[1]

FAKE_CHAT_ID = "oc_fake_admin_group_for_tests"

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

    def daily_report_records(self) -> list[tuple[str, dict[str, object]]]:
        return [record for record in self.records if record[0].startswith("daily_report.")]


class BuilderFunctionTests(unittest.TestCase):
    """直接调用 `_build_daily_report_duty`，不经过完整 `build_loop`。"""

    def test_missing_chat_id_refuses_registration_with_exactly_one_audit_record(self) -> None:
        audit = RecordingAudit()
        config = SchedulerConfig.from_env(COMPLETE_ENV)

        duty = _build_daily_report_duty(config, stop=threading.Event(), audit=audit)

        self.assertIsNone(duty)
        self.assertEqual(len(audit.daily_report_records()), 1, "缺前置时审计恰一条")
        action, fields = audit.daily_report_records()[0]
        self.assertEqual(action, "daily_report.duty_not_registered")
        self.assertEqual(fields["reason"], "missing_environment_variable")
        self.assertEqual(fields["variable"], "LINGXI_ADMIN_GROUP_CHAT_ID")
        self.assertNotIn("value", fields, "审计里不得回显变量的值")

    def test_a_configured_chat_id_registers_the_duty(self) -> None:
        audit = RecordingAudit()
        config = SchedulerConfig.from_env({**COMPLETE_ENV, "LINGXI_ADMIN_GROUP_CHAT_ID": FAKE_CHAT_ID})

        duty = _build_daily_report_duty(config, stop=threading.Event(), audit=audit)

        self.assertIsInstance(duty, DailyReportDuty)
        self.assertEqual(duty.name, "内测每日通报")
        self.assertEqual(audit.daily_report_records(), [], "前置齐备时不该有『未注册』审计")

    def test_the_on_send_outcome_callback_is_forwarded_to_the_sender(self) -> None:
        """告警接线断言：注入的 `on_send_outcome` 必须真的被交给发送适配器，
        不能在装配这一层悄悄丢掉——否则「送达失败不静默」只是一句空话。"""

        audit = RecordingAudit()
        config = SchedulerConfig.from_env({**COMPLETE_ENV, "LINGXI_ADMIN_GROUP_CHAT_ID": FAKE_CHAT_ID})
        observed: list[tuple[str, bool]] = []

        def on_send_outcome(operation: str, succeeded: bool) -> None:
            observed.append((operation, succeeded))

        duty = _build_daily_report_duty(
            config, stop=threading.Event(), audit=audit, on_send_outcome=on_send_outcome
        )
        assert duty is not None
        # 白盒核对内部持有的 sender 确实带上了同一个回调，不发真实请求。
        self.assertIs(duty._sender._on_send_outcome, on_send_outcome)  # noqa: SLF001

    def test_the_duty_uses_a_dedicated_uuid_prefix_distinct_from_the_roster_report(self) -> None:
        """`FeishuGroupMessages` 的去重前缀必须与花名册日报不同（见该适配器
        `uuid_prefix` 参数文档）——同一天两边若共用前缀且去重键恰好相同，
        飞书服务端会把其中一条误判成另一条的重试而丢弃。"""

        from lingxi.adapters.feishu_group_message import DAILY_REPORT_UUID_PREFIX, DELIVERY_UUID_PREFIX

        self.assertNotEqual(DAILY_REPORT_UUID_PREFIX, DELIVERY_UUID_PREFIX)

        config = SchedulerConfig.from_env({**COMPLETE_ENV, "LINGXI_ADMIN_GROUP_CHAT_ID": FAKE_CHAT_ID})
        duty = _build_daily_report_duty(config, stop=threading.Event(), audit=RecordingAudit())
        assert duty is not None
        self.assertEqual(duty._sender._uuid_prefix, DAILY_REPORT_UUID_PREFIX)  # noqa: SLF001


@unittest.skipUnless(
    importlib.util.find_spec("psycopg") and importlib.util.find_spec("cryptography"),
    "跳过：build_loop 会真的构造凭据保管与清理适配器，需要 psycopg 与 cryptography",
)
class FullAssemblyTests(unittest.TestCase):
    """经完整 `build_loop` 装配，确认「其余职责照常」与真实的注册顺序。"""

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

    def test_without_chat_id_the_process_still_assembles_its_other_duties(self) -> None:
        audit = RecordingAudit()

        loop = build_loop(self._config(), audit=audit)

        names = [duty.name for duty in loop.duties]
        self.assertNotIn("内测每日通报", names)
        # 缺前置时恰一条『未注册』审计（同 `V-花名册-29` 的纪律），不是零条——
        # 装配层必须留痕说明"为什么没有这个职责"，不能悄悄跳过。
        self.assertEqual(len(audit.daily_report_records()), 1)
        action, fields = audit.daily_report_records()[0]
        self.assertEqual(action, "daily_report.duty_not_registered")
        self.assertEqual(fields["variable"], "LINGXI_ADMIN_GROUP_CHAT_ID")
        # 进程仍然真实起得来，其余总能注册的职责一个都不少。
        for expected in ("凭据轮换", "保留清理", "空闲会话清理", "权限链到期清理"):
            self.assertIn(expected, names)

    def test_with_chat_id_the_duty_registers_independently_of_roster_config(self) -> None:
        """`chat_id` 是本职责**唯一**前置，与花名册 Base 坐标无关——本用例只配
        `chat_id`、不配花名册 Base 坐标，因此花名册审计日报不注册，本职责照常注册。
        「紧跟花名册那组之后」的位置断言见下一条用例（两者前置都齐备时）。"""

        audit = RecordingAudit()

        loop = build_loop(self._config(LINGXI_ADMIN_GROUP_CHAT_ID=FAKE_CHAT_ID), audit=audit)

        names = [duty.name for duty in loop.duties]
        self.assertIn("内测每日通报", names)
        self.assertNotIn("花名册审计日报", names)
        self.assertEqual(audit.daily_report_records(), [])

    def test_with_every_prerequisite_the_duty_registers_right_after_roster_audit(self) -> None:
        audit = RecordingAudit()

        loop = build_loop(
            self._config(
                LINGXI_ADMIN_GROUP_CHAT_ID=FAKE_CHAT_ID,
                LINGXI_ROSTER_BITABLE_APP_TOKEN="bascnFakeAppToken",
                LINGXI_ROSTER_BITABLE_TABLE_ID="tblFakeTable",
            ),
            audit=audit,
        )

        names = [duty.name for duty in loop.duties]
        self.assertIn("内测每日通报", names)
        self.assertIn("花名册审计日报", names)
        self.assertEqual(audit.daily_report_records(), [])
        self.assertEqual(
            names.index("内测每日通报"),
            names.index("花名册审计日报") + 1,
            "紧跟花名册那组之后注册，两者是同类『按日发一条到管理群』的职责",
        )

    def test_a_failure_in_the_duty_does_not_take_down_the_rest_of_the_loop(self) -> None:
        """职责级隔离（断言 V-保留-15 同一条纪律）：本职责本轮抛异常，不影响
        `SchedulerLoop.run_once` 里其余职责本轮执行。"""

        loop = build_loop(
            self._config(LINGXI_ADMIN_GROUP_CHAT_ID=FAKE_CHAT_ID), audit=RecordingAudit()
        )
        daily_report_duty = next(duty for duty in loop.duties if duty.name == "内测每日通报")

        def boom() -> None:
            raise RuntimeError("模拟职责崩溃")

        daily_report_duty.run_once = boom  # type: ignore[method-assign]

        # 不抛异常：SchedulerLoop 逐职责隔离，本职责崩溃不得带崩整轮。
        loop.run_once()


if __name__ == "__main__":
    unittest.main()
