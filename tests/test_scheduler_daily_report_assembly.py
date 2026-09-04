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
        config = SchedulerConfig.from_env(
            {**COMPLETE_ENV, "LINGXI_ADMIN_GROUP_CHAT_ID": FAKE_CHAT_ID}
        )

        duty = _build_daily_report_duty(config, stop=threading.Event(), audit=audit)

        self.assertIsInstance(duty, DailyReportDuty)
        self.assertEqual(duty.name, "内测每日通报")
        # chat_id 仍是本职责**注册**的唯一前置：不产生 `duty_not_registered`。
        self.assertNotIn(
            "daily_report.duty_not_registered",
            [action for action, _ in audit.daily_report_records()],
        )

    def test_a_configured_chat_id_alone_leaves_the_optional_coverage_check_unwired(self) -> None:
        """Issue #320 并入项：`chat_id` 齐备但没有额外配置
        `LINGXI_MCP_TOKEN_ENCRYPT_KEY`/`LINGXI_QUERY_MCP_ENDPOINT` 时，「未覆盖新
        指标」日检这个**可选子段**不接线，留一条与 `duty_not_registered` 不同名的
        信息性审计——职责本身照常注册，这条记录只影响正文里会不会出现「待分配」段。
        """

        audit = RecordingAudit()
        config = SchedulerConfig.from_env(
            {**COMPLETE_ENV, "LINGXI_ADMIN_GROUP_CHAT_ID": FAKE_CHAT_ID}
        )

        duty = _build_daily_report_duty(config, stop=threading.Event(), audit=audit)

        self.assertIsInstance(duty, DailyReportDuty)
        self.assertEqual(
            audit.daily_report_records(),
            [
                (
                    "daily_report.metric_coverage_not_wired",
                    {
                        "reason": "missing_environment_variable",
                        "variables": [
                            "LINGXI_MCP_TOKEN_ENCRYPT_KEY",
                            "LINGXI_QUERY_MCP_ENDPOINT",
                        ],
                    },
                )
            ],
        )

    def test_configuring_both_coverage_prerequisites_wires_the_optional_check(self) -> None:
        audit = RecordingAudit()
        config = SchedulerConfig.from_env(
            {
                **COMPLETE_ENV,
                "LINGXI_ADMIN_GROUP_CHAT_ID": FAKE_CHAT_ID,
                "LINGXI_MCP_TOKEN_ENCRYPT_KEY": "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
                "LINGXI_QUERY_MCP_ENDPOINT": "https://mcp.example.invalid/query",
            }
        )

        duty = _build_daily_report_duty(config, stop=threading.Event(), audit=audit)

        self.assertIsInstance(duty, DailyReportDuty)
        self.assertEqual(audit.daily_report_records(), [], "两项前置齐备时不留『未接线』审计")

    def test_the_on_send_outcome_callback_is_forwarded_to_the_sender(self) -> None:
        """告警接线断言：注入的 `on_send_outcome` 必须真的被交给发送适配器，
        不能在装配这一层悄悄丢掉——否则「送达失败不静默」只是一句空话。"""

        audit = RecordingAudit()
        config = SchedulerConfig.from_env(
            {**COMPLETE_ENV, "LINGXI_ADMIN_GROUP_CHAT_ID": FAKE_CHAT_ID}
        )
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
        飞书服务端会把其中一条误判成另一条的重试而丢弃。

        断言必须**真调用** `delivery_uuid`，不能只比较两个前缀常量本身是否
        相等——只比较常量捕获不到「前缀 + 32 位摘要超过飞书 50 字符上限」这
        类问题：`DAILY_REPORT_UUID_PREFIX` 曾经取值 `"lingxi-daily-report-"`
        （20 字符），与 `DELIVERY_UUID_PREFIX` 确实不相等、这条断言当时也是
        绿的，但 20 + 32 = 52 已经超限，导致 `delivery_uuid` 在发送前必抛
        `ValueError`——通报永远发不出去（opus 批量审查 P1）。
        """

        from lingxi.adapters.feishu_group_message import (
            DAILY_REPORT_UUID_PREFIX,
            DELIVERY_UUID_PREFIX,
            delivery_uuid,
        )

        self.assertNotEqual(DAILY_REPORT_UUID_PREFIX, DELIVERY_UUID_PREFIX)

        value = delivery_uuid(
            FAKE_CHAT_ID, "daily-report:2026-08-24", prefix=DAILY_REPORT_UUID_PREFIX
        )
        self.assertLessEqual(len(value), 50, "投递去重 ID 不得超过飞书 uuid 字段的 50 字符上限")

        config = SchedulerConfig.from_env(
            {**COMPLETE_ENV, "LINGXI_ADMIN_GROUP_CHAT_ID": FAKE_CHAT_ID}
        )
        duty = _build_daily_report_duty(config, stop=threading.Event(), audit=RecordingAudit())
        assert duty is not None
        self.assertEqual(duty._sender._uuid_prefix, DAILY_REPORT_UUID_PREFIX)  # noqa: SLF001

    def test_all_declared_uuid_prefixes_fit_within_the_delivery_budget(self) -> None:
        """预算测试：仓库里任何一个 `..._UUID_PREFIX = "..."` 前缀常量，加上
        32 位摘要都不能超过飞书 `uuid` 字段的 50 字符上限（见 `delivery_uuid`
        的运行期校验）。用 AST 扫描 `src/lingxi` 全部源码而不是导入后枚举
        已知模块的 `vars()`——这样以后在任何模块新增一个前缀常量，哪怕本测试
        文件从没导入过那个模块，也会被这条预算测试自动纳入，不依赖开发者记得
        手工把新前缀加进某个清单（这正是本条修复要补的洞：上一条用例过去只
        比较两个具体常量，第三个前缀完全不会被覆盖）。
        """

        import ast

        from lingxi.adapters.feishu_group_message import DELIVERY_UUID_MAX_LENGTH

        source_root = REPOSITORY_ROOT / "src" / "lingxi"
        found: dict[str, str] = {}

        def _record(path: pathlib.Path, name: str, value: object) -> None:
            if name.endswith("_UUID_PREFIX") and isinstance(value, str):
                found[f"{path.relative_to(REPOSITORY_ROOT)}::{name}"] = value

        for path in sorted(source_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            _record(path, target.id, node.value.value)
                elif (
                    isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name)
                    and isinstance(node.value, ast.Constant)
                ):
                    _record(path, node.target.id, node.value.value)

        # 扫描机制本身的冒烟检查：至少要找到当前已知的三个前缀常量
        # （DELIVERY_UUID_PREFIX / DAILY_REPORT_UUID_PREFIX / NOTICE_UUID_PREFIX），
        # 否则说明扫描逻辑本身失效（比如源码改用了扫描不识别的写法），这条
        # 预算测试会静默失去意义而不自知。
        self.assertGreaterEqual(
            len(found), 3, f"至少应发现三个已知前缀常量，实际只找到：{sorted(found)}"
        )

        for location, prefix in sorted(found.items()):
            with self.subTest(location=location, prefix=prefix):
                self.assertLessEqual(
                    len(prefix) + 32,
                    DELIVERY_UUID_MAX_LENGTH,
                    f"{location} 的前缀 {prefix!r} 加 32 位摘要会超过飞书 uuid 的"
                    f" {DELIVERY_UUID_MAX_LENGTH} 字符上限",
                )


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
                "LINGXI_DELEGATED_CREDENTIAL_PATH": str(
                    pathlib.Path(directory.name) / "delegated.enc"
                ),
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
        # chat_id 齐备 ⇒ 职责本身注册（不产生 `duty_not_registered`）；「未覆盖新
        # 指标」日检这个可选子段没有额外配置 LINGXI_MCP_TOKEN_ENCRYPT_KEY/
        # LINGXI_QUERY_MCP_ENDPOINT，因此**预期**留一条不同名的信息性记录
        # （`test_a_configured_chat_id_alone_leaves_the_optional_coverage_check_
        # unwired` 已经钉住这条记录的完整形状，这里只需确认它不是注册失败）。
        self.assertNotIn(
            "daily_report.duty_not_registered",
            [action for action, _ in audit.daily_report_records()],
        )

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
        self.assertNotIn(
            "daily_report.duty_not_registered",
            [action for action, _ in audit.daily_report_records()],
        )
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
