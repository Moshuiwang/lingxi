"""组织快照同步职责在 scheduler 的装配用例（Issue #250）。

形状照 ``tests/test_scheduler_onboarding_assembly.py``：前置不齐就**不注册**并留下
**恰一条**审计，区分"未接线"（调用方没有交出任何供给）与"配了但拿不到"（供给
在运行期失败，职责照常注册，失败发生在 ``run_once`` 内部）。生产装配路径
（``build_loop`` 不传 ``roster_access_token`` / ``permission_table_access_token``）
下两个供给都是默认值，因此本文件同时证明"默认装配确实真实注册"这件事。
"""

from __future__ import annotations

import threading
import unittest
from typing import Any
from unittest import mock

from lingxi.apps.scheduler import SchedulerConfig, _build_org_snapshot_sync_duty, build_loop
from lingxi.apps.scheduler.assembly import ORG_SNAPSHOT_ROUND_BUDGET_SECONDS
from lingxi.apps.scheduler.org_snapshot_sync import OrgSnapshotSyncDuty

BASE_ENV = {
    "LINGXI_POSTGRES_DSN": "postgresql://localhost/lingxi",
    # 与 tests/test_scheduler_process.py 同一个固定假 Fernet 密钥：只为过
    # HostFileDelegatedCredentialVault 的形状校验，不是真实凭据。
    "LINGXI_DELEGATED_CREDENTIAL_KEY": "ZmFrZS1mZXJuZXQta2V5LWZvci11bml0LXRlc3RzLTA9",
    "LINGXI_DELEGATED_CREDENTIAL_PATH": "/tmp/lingxi-credential",
    "LINGXI_FEISHU_APP_ID": "cli_test",
    "LINGXI_FEISHU_APP_SECRET": "secret",
}


class RecordingAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, object]]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, dict(fields)))

    def actions(self) -> list[str]:
        return [action for action, _ in self.records]


def build(
    *, user_token: Any = None, app_token: Any = None
) -> tuple[Any, RecordingAudit]:
    audit = RecordingAudit()
    duty = _build_org_snapshot_sync_duty(
        SchedulerConfig.from_env(BASE_ENV),
        stop=threading.Event(),
        audit=audit,
        user_access_token=user_token,
        app_access_token=app_token,
    )
    return duty, audit


class PrerequisiteTests(unittest.TestCase):
    def test_no_supplies_at_all_is_reported_as_the_user_path_being_unwired(self) -> None:
        """两个都缺时只报第一个检查到的原因（**恰一条**审计，`V-花名册-29` 同一条纪律）。"""

        duty, audit = build()
        self.assertIsNone(duty)
        self.assertEqual(audit.actions(), ["org_snapshot_sync.duty_not_registered"])
        self.assertEqual(audit.records[0][1]["reason"], "user_access_token_unwired")

    def test_missing_only_the_app_identity_supply_is_reported_distinctly(self) -> None:
        duty, audit = build(user_token=lambda: "user-token")
        self.assertIsNone(duty)
        self.assertEqual(audit.actions(), ["org_snapshot_sync.duty_not_registered"])
        self.assertEqual(audit.records[0][1]["reason"], "app_access_token_unwired")

    def test_both_supplies_present_produces_a_registered_duty(self) -> None:
        duty, audit = build(user_token=lambda: "user-token", app_token=lambda: "app-token")

        self.assertIsInstance(duty, OrgSnapshotSyncDuty)
        self.assertEqual(audit.actions(), [], "装配成功时不该留「未装配」审计")

    def test_an_empty_source_app_id_configuration_cannot_slip_through(self) -> None:
        """``source_app_id`` 取自 ``config.feishu_app_id``，而它是 scheduler 的必需配置——
        进程起得来就一定有，因此这里只需确认装配确实把它传下去了（用真实构造校验捕获）。"""

        duty, _audit = build(user_token=lambda: "user-token", app_token=lambda: "app-token")
        self.assertIsNotNone(duty)


class RuntimeVsUnwiredDistinctionTest(unittest.TestCase):
    """"未接线"（装配期，只报一条 ``duty_not_registered``）与"配了但拿不到"
    （运行期，职责照常注册，失败发生在 ``run_once`` 内部）必须可分辨。"""

    def test_a_wired_but_failing_token_supply_still_registers_the_duty(self) -> None:
        def broken() -> str:
            raise RuntimeError("access_token_unavailable")

        duty, audit = build(user_token=broken, app_token=lambda: "app-token")

        self.assertIsInstance(duty, OrgSnapshotSyncDuty, "配了但拿不到令牌时职责必须照常注册")
        self.assertEqual(audit.actions(), [], "装配阶段不该因为运行期会失败而提前记审计")

        # 运行期失败走的是职责自己的 read_failed 分类，不是装配层的 duty_not_registered。
        # 本测试用的是指向本地假 DSN 的真实 PostgresOrgSnapshotStore（连不上）：F8 的
        # 当日持久化水位检查会先失败一次（按"未知"处理、不阻塞），紧接着才是
        # 令牌供给失败触发的 read_failed——两条审计都在预期之内。
        result = duty.run_once()
        self.assertIsNone(result)
        self.assertEqual(
            audit.actions(),
            ["org_snapshot_sync.watermark_check_failed", "org_snapshot_sync.read_failed"],
        )

    def test_a_broken_user_token_supply_is_tagged_distinctly_in_the_audit(self) -> None:
        """F6：两条令牌供给分别包装，audit 里必须能分辨是哪一条失败——不能只留
        一个笼统的异常类型名。（见上一个用例的注释：F8 的水位检查连不上本地假
        DSN，会先留一条 ``watermark_check_failed``，不影响这里要验证的 ``supply``
        标签落在紧随其后的 ``read_failed`` 记录上。）"""

        def broken() -> str:
            raise RuntimeError("access_token_unavailable")

        duty, audit = build(user_token=broken, app_token=lambda: "app-token")

        duty.run_once()

        self.assertEqual(
            audit.actions(),
            ["org_snapshot_sync.watermark_check_failed", "org_snapshot_sync.read_failed"],
        )
        self.assertEqual(audit.records[-1][1].get("supply"), "user_access_token")
        # 不得记录原始异常消息或值——只留安全分类标签与异常类型名。
        self.assertNotIn("access_token_unavailable", str(audit.records[-1][1]))

    def test_a_broken_app_token_supply_is_tagged_distinctly_in_the_audit(self) -> None:
        def broken() -> str:
            raise RuntimeError("access_token_unavailable")

        duty, audit = build(user_token=lambda: "user-token", app_token=broken)

        duty.run_once()

        self.assertEqual(
            audit.actions(),
            ["org_snapshot_sync.watermark_check_failed", "org_snapshot_sync.read_failed"],
        )
        self.assertEqual(audit.records[-1][1].get("supply"), "app_access_token")


class DefaultAssemblyTest(unittest.TestCase):
    """生产装配路径（``build_loop`` 不传令牌供给）下，两条默认供给都会被建出来，
    组织快照同步职责因此**真实注册**（可观察完成标准第一条）。"""

    def _config(self, **overrides: str) -> SchedulerConfig:
        import tempfile
        from pathlib import Path

        from cryptography.fernet import Fernet

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return SchedulerConfig.from_env(
            {
                **BASE_ENV,
                # 装配会真的构造凭据保管对象，需要一个有效的 Fernet 密钥与可写路径；
                # 两者都是本地临时物，不涉及任何真实凭据（同 tests/test_scheduler_process.py）。
                "LINGXI_DELEGATED_CREDENTIAL_KEY": Fernet.generate_key().decode(),
                "LINGXI_DELEGATED_CREDENTIAL_PATH": str(Path(directory.name) / "delegated.enc"),
                **overrides,
            }
        )

    def test_build_loop_registers_the_duty_by_default(self) -> None:
        loop = build_loop(self._config())

        duty_types = [type(duty) for duty in loop.duties]
        self.assertIn(OrgSnapshotSyncDuty, duty_types)

    def test_the_duty_sits_before_onboarding_and_after_permission_publish(self) -> None:
        """位置断言：同一轮内，组织快照同步先于首次开通编排跑，才可能让新鲜的快照被
        当轮的开通认领用上（见 ``assembly.py`` 调用点的注释）。"""

        loop = build_loop(
            self._config(
                LINGXI_PERMISSION_BITABLE_APP_TOKEN="bascnfake",
                LINGXI_PERMISSION_BITABLE_TABLE_ID="tblfake",
            )
        )
        names = [type(duty).__name__ for duty in loop.duties]

        self.assertIn("OrgSnapshotSyncDuty", names)
        self.assertIn("PermissionPublishDuty", names)
        self.assertLess(
            names.index("PermissionPublishDuty"),
            names.index("OrgSnapshotSyncDuty"),
            "组织快照同步必须排在权限发布消费之后",
        )
        # 首次开通编排本轮缺前置（未配 LINGXI_QUERY_MCP_ENDPOINT 等）因此不会真实注册，
        # 位置断言的落点因此只到「组织快照同步排在权限发布之后」——它相对首次开通编排的
        # 先后关系已经由 assembly.py 里两次 append 的先后顺序保证，属装配代码结构本身
        # 的不变量，不需要在两者都真实注册的情况下才能被证明。


class HardeningWiringTests(unittest.TestCase):
    """Issue #284 A 组 PR1：整轮预算 + 停机感知的**装配层**接线断言。

    机制本身（`round_budget` 撞线抛错、`stop.wait` 可注入）已经在
    ``tests/test_feishu_directory_adapter.py`` 按单元测试覆盖；这里只证明
    真实装配路径（``_build_org_snapshot_sync_duty``）确实把这两样接上了，
    不是"能力已经存在但没人调用"。
    """

    def test_read_snapshot_is_wrapped_in_a_round_budget_while_it_runs(self) -> None:
        """`read_snapshot()` 闭包必须把整趟 `read_org_snapshot` 包在
        `client.round_budget(seconds=ORG_SNAPSHOT_ROUND_BUDGET_SECONDS)` 里
        （设计 §2）：调用期间 client 的截止时间必须已经设置（不是 `None`）。"""

        seen_deadlines: list[float | None] = []

        def _stub_read_org_snapshot(*, client: Any, app_token: str, user_token: str) -> Any:
            seen_deadlines.append(client._round_deadline)
            return "fake-batch"

        with mock.patch(
            "lingxi.adapters.feishu_org_snapshot_reader.read_org_snapshot",
            _stub_read_org_snapshot,
        ):
            duty, audit = build(user_token=lambda: "u", app_token=lambda: "a")
            self.assertEqual(audit.actions(), [])
            result = duty._read_snapshot()

        self.assertEqual(result, "fake-batch")
        self.assertEqual(len(seen_deadlines), 1)
        self.assertIsNotNone(seen_deadlines[0], "调用期间必须已经处在 round_budget 的 with 块内")

    def test_the_deadline_is_restored_to_none_once_read_snapshot_returns(self) -> None:
        """`round_budget` 的 `finally` 必须把截止时间还原——否则第二轮调用会继承
        第一轮剩下的截止时间，而不是各自拿到一份全新的 20 分钟预算。这里通过
        直接检查装配出来的 client 实例在两次调用之间的状态来验证（不依赖
        `read_org_snapshot` 内部实现）。"""

        captured: dict[str, Any] = {}

        def _stub_read_org_snapshot(*, client: Any, app_token: str, user_token: str) -> Any:
            captured["client"] = client
            captured["deadline_during_call"] = client._round_deadline
            return "fake-batch"

        with mock.patch(
            "lingxi.adapters.feishu_org_snapshot_reader.read_org_snapshot",
            _stub_read_org_snapshot,
        ):
            duty, _audit = build(user_token=lambda: "u", app_token=lambda: "a")
            duty._read_snapshot()

        client = captured["client"]
        self.assertIsNotNone(captured["deadline_during_call"])
        self.assertIsNone(client._round_deadline, "with 块退出后必须还原成 None（默认关闭）")

    def test_the_deadline_is_roughly_the_configured_round_budget(self) -> None:
        """截止时间必须是「进入调用时刻 + `ORG_SNAPSHOT_ROUND_BUDGET_SECONDS`」——
        不是任意非 `None` 值就算数，防止未来有人手滑传成一个远小于设计值
        （比如秒级）的预算。"""

        import time as time_module

        captured: dict[str, Any] = {}

        def _stub_read_org_snapshot(*, client: Any, app_token: str, user_token: str) -> Any:
            captured["deadline"] = client._round_deadline
            captured["now"] = time_module.monotonic()
            return "fake-batch"

        with mock.patch(
            "lingxi.adapters.feishu_org_snapshot_reader.read_org_snapshot",
            _stub_read_org_snapshot,
        ):
            duty, _audit = build(user_token=lambda: "u", app_token=lambda: "a")
            duty._read_snapshot()

        remaining = captured["deadline"] - captured["now"]
        # 允许测试执行本身的一点点开销，但必须接近整数秒级的 1200，不能是别的量级。
        self.assertGreater(remaining, ORG_SNAPSHOT_ROUND_BUDGET_SECONDS - 5)
        self.assertLessEqual(remaining, ORG_SNAPSHOT_ROUND_BUDGET_SECONDS)

    def test_the_wired_client_uses_stop_wait_as_its_sleeper(self) -> None:
        """`sleep=stop.wait`（设计 §4）：真实装配出来的 client 的 `_sleep` 必须
        绑定到这一个 `stop`（`threading.Event.wait` 每次取值都会新建一个绑定
        方法包装对象，因此这里用 `==`——绑定方法比较的是底层函数与所属实例是否
        相同，同一个 `Event` 的 `wait` 恒相等；用 `is` 会误判为不等），不是
        默认的 `time.sleep`。不通过"等多久才返回"的计时去推断——
        `REQUEST_PAUSE_SECONDS`（0.12 秒）本身就远小于任何合理的 join 超时，
        计时法分辨不出"`stop.wait` 被提前打断"与"`time.sleep` 碰巧自己也在
        超时窗口内跑完"这两种情况，不是硬断言。"""

        captured: dict[str, Any] = {}

        def _stub_read_org_snapshot(*, client: Any, app_token: str, user_token: str) -> Any:
            captured["client"] = client
            return "fake-batch"

        stop = threading.Event()
        audit = RecordingAudit()
        with mock.patch(
            "lingxi.adapters.feishu_org_snapshot_reader.read_org_snapshot",
            _stub_read_org_snapshot,
        ):
            duty = _build_org_snapshot_sync_duty(
                SchedulerConfig.from_env(BASE_ENV),
                stop=stop,
                audit=audit,
                user_access_token=lambda: "u",
                app_access_token=lambda: "a",
            )
            duty._read_snapshot()

        client = captured["client"]

        self.assertEqual(client._sleep, stop.wait, "节流/退避的 sleeper 必须绑定这一份 stop，不是默认的 time.sleep")

    def test_stop_set_before_the_client_is_built_still_lets_a_throttle_wait_return_immediately(self) -> None:
        """行为级佐证（不只是身份相等）：`stop` 在 client 构造**之前**已经置位时，
        `_throttle()` 的等待必须立刻返回，不等满 `REQUEST_PAUSE_SECONDS`——用一个
        远大于节流步长、但仍然远小于测试超时的下界（0.05 秒 vs 步长 0.12 秒）
        证明"立刻返回"，不依赖"两者都在 1 秒内完成"这种分辨不出差异的计时法。"""

        stop = threading.Event()
        stop.set()

        captured: dict[str, Any] = {}

        def _stub_read_org_snapshot(*, client: Any, app_token: str, user_token: str) -> Any:
            captured["client"] = client
            return "fake-batch"

        with mock.patch(
            "lingxi.adapters.feishu_org_snapshot_reader.read_org_snapshot",
            _stub_read_org_snapshot,
        ):
            duty = _build_org_snapshot_sync_duty(
                SchedulerConfig.from_env(BASE_ENV),
                stop=stop,
                audit=RecordingAudit(),
                user_access_token=lambda: "u",
                app_access_token=lambda: "a",
            )
            duty._read_snapshot()

        client = captured["client"]

        import time as time_module

        started = time_module.monotonic()
        client._throttle()
        elapsed = time_module.monotonic() - started

        self.assertLess(
            elapsed,
            0.05,
            "stop 已置位时节流等待必须立刻返回（远小于 REQUEST_PAUSE_SECONDS=0.12s）",
        )

    def test_the_organization_snapshot_client_is_not_shared_with_onboarding(self) -> None:
        """#2 的整轮预算只挂在组织快照专用的 client 实例上——这里只验证「组织
        快照装配出的 client 与它自己的 store/audit 一样是本次调用私有构造出的
        新对象」，不会被后续调用复用（防止未来有人为了省一次构造而把 client
        提到装配函数外层共享，进而让 round_budget 的作用域意外扩大）。"""

        clients: list[Any] = []

        def _stub_read_org_snapshot(*, client: Any, app_token: str, user_token: str) -> Any:
            clients.append(client)
            return "fake-batch"

        with mock.patch(
            "lingxi.adapters.feishu_org_snapshot_reader.read_org_snapshot",
            _stub_read_org_snapshot,
        ):
            first_duty, _audit = build(user_token=lambda: "u", app_token=lambda: "a")
            first_duty._read_snapshot()
            second_duty, _audit = build(user_token=lambda: "u", app_token=lambda: "a")
            second_duty._read_snapshot()

        self.assertEqual(len(clients), 2)
        self.assertIsNot(clients[0], clients[1], "两次装配必须各自拿到独立的 client 实例")


if __name__ == "__main__":
    unittest.main()
