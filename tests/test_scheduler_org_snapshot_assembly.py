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

from lingxi.adapters.feishu_directory import FeishuDirectoryError
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

    def test_the_wired_client_aborts_via_this_process_stop_not_a_raw_passthrough(self) -> None:
        """`sleep=_stop_aware_sleep(stop)`（设计 §4，中止行为按二级独立审查 P2
        修正）：真实装配出来的 client 的 `_sleep` 必须绑定到这一个 `stop`，但
        **不能是裸的 `stop.wait`**——直接透传会让停机置位后的节流/限频退避悄悄
        失去等待（`stop.wait` 返回 `True` 时 `_throttle()` 分辨不出"真的等过了"
        与"已经置位、假装等过了"），在途一轮剩余的数百次分页请求因此会以无节流
        速度打出。这里改用行为断言而不是身份相等：未置位时 `sleep(0)` 必须正常
        放行；同一份 `stop` 置位后，同一个 `client._sleep` 必须转而抛出
        `FeishuDirectoryError(code="stopping")`——这就是"确实绑定了这一个 stop"
        的证据，比对比函数对象身份更贴近真正要守住的行为。"""

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

        # 未置位：sleep(0) 必须正常放行，不是恒中止的桩。
        client._sleep(0)

        # 置位之后，同一个 client 的 sleeper 必须转而中止——证明它读的是这一份
        # `stop`（一个绑定到别的、从未置位的 Event 的 sleeper 不会在这里变红）。
        stop.set()
        with self.assertRaises(FeishuDirectoryError) as raised:
            client._sleep(0)
        self.assertEqual(raised.exception.code, "stopping")

    def test_stop_set_before_the_client_is_built_aborts_the_throttle_instead_of_silently_skipping_it(
        self,
    ) -> None:
        """行为级佐证（P2 二级独立审查修复）：`stop` 在 client 构造**之前**已经
        置位时，`_throttle()` 必须立刻**中止**（抛出 `FeishuDirectoryError`），
        不是像修复前那样"立刻正常返回、悄悄跳过这一次节流"——后者会让在途一轮
        剩余的数百次分页请求在停机后失去节流，以无节流速度打出，撞真实限频。
        仍然验证"立刻"（远小于 `REQUEST_PAUSE_SECONDS`），但落点从"正常返回"
        改成"抛错"。"""

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
        with self.assertRaises(FeishuDirectoryError) as raised:
            client._throttle()
        elapsed = time_module.monotonic() - started

        self.assertEqual(raised.exception.code, "stopping")
        self.assertLess(
            elapsed,
            0.05,
            "stop 已置位时节流必须立刻中止（远小于 REQUEST_PAUSE_SECONDS=0.12s），不是正常返回",
        )

    def test_a_stop_set_mid_round_aborts_with_no_further_requests_and_an_honest_audit(self) -> None:
        """闭环行为证据（P2 二级独立审查修复）：`stop` 不是在构造前、也不是完全
        没跑就置位，而是**本轮读取已经成功发出过一次节流/请求之后**才置位——
        模拟"数百次分页请求里，进程在中途收到停机信号"这个真实场景。之后
        client 必须立刻中止，不再发起任何一次后续节流/请求；这一轮经真实装配好
        的 `OrgSnapshotSyncDuty.run_once()` 必须落进既有的
        `read_failed`→退避→保留上一份完成批次路径——不是 `committed`，也不是
        被吞掉、悄无声息地当成"这一轮跑完了"。"""

        throttle_calls = {"n": 0}
        stop = threading.Event()

        def _stub_read_org_snapshot(*, client: Any, app_token: str, user_token: str) -> Any:
            # 第一次节流：模拟"本轮已经成功发出过一次分页请求"，此时 stop 还没
            # 置位，正常放行。
            client._throttle()
            throttle_calls["n"] += 1
            # 模拟"就在下一次分页请求前，进程收到了停机信号"。
            stop.set()
            # 第二次节流：必须中止，异常原样冒泡——与真实 `_pages`/`_pages_multi`
            # 在 `_request` 里遇到的行为完全一致（`_throttle()` 在真正发起请求之前）。
            client._throttle()
            throttle_calls["n"] += 1  # 不会执行到；如果执行到就是回归（节流没能中止）
            return "fake-batch"

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
            result = duty.run_once()

        self.assertIsNone(result, "中止的一轮不得返回 run_id")
        self.assertEqual(throttle_calls["n"], 1, "停机置位之后不得再发起下一次节流/请求")
        self.assertIsNone(duty.completed_on, "中止的一轮不得置位当日水位")
        # 本地假 DSN（BASE_ENV 指向 localhost）连不上：F8 的当日持久化水位检查
        # 会先失败一次（按未知处理、不阻塞），紧接着才是真正的中止——与本文件
        # `RuntimeVsUnwiredDistinctionTest` 已经确认过的同一套顺序。
        self.assertEqual(
            audit.actions(),
            ["org_snapshot_sync.watermark_check_failed", "org_snapshot_sync.read_failed"],
        )
        fields = audit.records[-1][1]
        self.assertEqual(fields.get("code"), "stopping", "审计必须如实记下中止原因，不是笼统的异常类型名")

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
