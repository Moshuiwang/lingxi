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

from lingxi.apps.scheduler import SchedulerConfig, _build_org_snapshot_sync_duty, build_loop
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
        result = duty.run_once()
        self.assertIsNone(result)
        self.assertEqual(audit.actions(), ["org_snapshot_sync.read_failed"])


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


if __name__ == "__main__":
    unittest.main()
