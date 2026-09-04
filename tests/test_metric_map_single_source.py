"""「公司 × 职能 → 指标」映射在两条路径上只能有**一个**真相（Trace #544 S-2c）。

对抗审查 P-1 坐实的缺陷：``LINGXI_COMPANY_FUNCTION_METRIC_MAP_PATH`` 这个外置映射
文件此前**只有 scheduler 读**，gateway 侧三个调用点硬读随包默认映射（定向重算那处
甚至显式传 ``None``）。外置文件一旦启用，同一个人的权限范围会在"管理员确认动作后
gateway 立即发布"与"次日 scheduler 日批重算"之间来回翻转——每次翻转都是用户可见的
真实权限变化，而两边看起来都"正常工作"。

本文件按调用点逐个钉住三件事：

1. **同源**：两个进程的配置对象把同一个变量值解释成同一份文件；三个 gateway 调用点
   读的是装配层注入的那一份，不是随包默认；
2. **缺失即失败关闭**：配了却读不出来时，三处各自不发布/不授权/不展示，**没有一处
   静默回落随包默认**（回落正是双真相的来源）；
3. **不配置仍然合法**：生产刻意不设这个变量（见 ``deploy/.env.example``），两个进程
   都必须照常启动并落回随包默认——本修复不得把"不设"变成启动失败。

真库不参与：本文件的断言全部落在"读哪一份映射"这条判定上，数据库访问用假连接顶住
（``adapters/postgres_pending_action.connect``）或直接证明**一次都没有发生**。
"""

from __future__ import annotations

import json
import sys
import types
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from lingxi.adapters.company_function_metric_map_file import (
    METRIC_MAP_PATH_ENV,
    default_company_function_metric_map_path,
    load_company_function_metric_map,
    parse_metric_map_path,
)
from lingxi.adapters.feishu_admin_card import TomlCompanyMetricCatalog
from lingxi.adapters.postgres_pending_action import PostgresPendingActionStore
from lingxi.adapters.postgres_permission_recompute_trigger import PermissionRecomputeAdapter
from lingxi.apps.gateway.config import ENV_PREFIX, GatewayConfigError, load_config
from lingxi.apps.scheduler.config import SchedulerConfig
from lingxi.core.admin.pending_action import (
    PendingAction,
    PendingActionStatus,
    PendingActionType,
)

#: 真实随包映射里公司 "1" 的「财务」职能同时含这两个指标；外置文件只保留前者，
#: 于是"读到了哪一份"在断言里是可分辨的事实，而不是一句声称。
COMPANY = "1"
FUNCTION = "财务"
KEPT_METRIC = "sub_new_count"
DELETED_METRIC = "exchange_rate"
#: 随包 ``galaxy_role_function_map.toml`` 里映射到「财务」职能的真实职位名。
POSITION = "A财务"

EXTERNAL_MAP = f"""
[meta]
decision = "Trace #544 S-2c 测试夹具"

[companies."{COMPANY}"]
"{FUNCTION}" = ["{KEPT_METRIC}"]
"""

SCHEDULER_ENV = {
    "LINGXI_POSTGRES_DSN": "postgresql://lingxi:fake-password-for-tests-only@db.invalid/lingxi",
    "LINGXI_DELEGATED_CREDENTIAL_KEY": "fake-key-for-tests-only",
    "LINGXI_DELEGATED_CREDENTIAL_PATH": "/nonexistent/credentials",
    "LINGXI_FEISHU_APP_ID": "cli_fake_app_id",
    "LINGXI_FEISHU_APP_SECRET": "fake-app-secret-for-tests-only",
}

GATEWAY_ENV = {
    f"{ENV_PREFIX}APP_ID": "cli_fake_app_id",
    f"{ENV_PREFIX}APP_SECRET": "fake-app-secret-for-tests-only",
    f"{ENV_PREFIX}POSTGRES_DSN": "postgresql://lingxi:fake-password-for-tests-only@db.invalid/lingxi",
}

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


class _ExternalMapFixture(unittest.TestCase):
    """一份真实存在的外置映射文件 + 一条指向不存在文件的路径。"""

    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.external = self.directory / "company_function_metric_map.toml"
        self.external.write_text(EXTERNAL_MAP, encoding="utf-8")
        self.missing = self.directory / "never-written.toml"

    def packaged_metrics(self) -> set[str]:
        mapping = load_company_function_metric_map(None)
        return {metric for functions in mapping.values() for values in functions.values() for metric in values}


class SharedEnvSourceTests(_ExternalMapFixture):
    """同一个变量值，两个进程必须解释成同一份文件。"""

    def test_the_two_processes_resolve_the_same_variable_to_the_same_file(self) -> None:
        value = str(self.external)

        scheduler = SchedulerConfig.from_env({**SCHEDULER_ENV, METRIC_MAP_PATH_ENV: value})
        gateway = load_config({**GATEWAY_ENV, METRIC_MAP_PATH_ENV: value})

        self.assertEqual(
            scheduler.metric_map_path,
            gateway.metric_map_path,
            "两个进程读同一个变量，却解释成不同的文件——这就是双真相本身",
        )
        self.assertEqual(
            load_company_function_metric_map(scheduler.metric_map_path),
            load_company_function_metric_map(gateway.metric_map_path),
            "同一个变量值必须让两条路径拿到逐字相同的映射",
        )

    def test_the_external_file_really_replaces_the_packaged_default(self) -> None:
        """夹具本身必须是可分辨的：被删掉的指标在随包默认里有、在外置文件里没有。"""

        self.assertIn(DELETED_METRIC, self.packaged_metrics())
        mapping = load_company_function_metric_map(
            load_config({**GATEWAY_ENV, METRIC_MAP_PATH_ENV: str(self.external)}).metric_map_path
        )
        self.assertEqual(mapping[COMPANY][FUNCTION], (KEPT_METRIC,))

    def test_an_unset_variable_stays_legal_on_both_sides(self) -> None:
        """生产刻意不设这个变量：两侧都必须照常起、都落回随包默认。"""

        scheduler = SchedulerConfig.from_env(SCHEDULER_ENV)
        gateway = load_config(GATEWAY_ENV)

        self.assertIsNone(scheduler.metric_map_path)
        self.assertIsNone(gateway.metric_map_path)
        self.assertEqual(
            load_company_function_metric_map(gateway.metric_map_path),
            load_company_function_metric_map(default_company_function_metric_map_path()),
            "未配置必须逐字节等于此前行为（读随包默认），不是启动失败",
        )

    def test_a_blank_value_is_the_same_as_unset(self) -> None:
        for raw in ("", "   "):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_metric_map_path(raw))
                self.assertIsNone(load_config({**GATEWAY_ENV, METRIC_MAP_PATH_ENV: raw}).metric_map_path)
                self.assertIsNone(SchedulerConfig.from_env({**SCHEDULER_ENV, METRIC_MAP_PATH_ENV: raw}).metric_map_path)

    def test_a_misconfigured_value_fails_closed_at_startup_on_both_sides(self) -> None:
        """错配不是未配：带空白的路径两侧都启动即失败，且不回显取到的值。"""

        bad = "/etc/ling xi/company_function_metric_map.toml"

        with self.assertRaises(GatewayConfigError) as gateway_error:
            load_config({**GATEWAY_ENV, METRIC_MAP_PATH_ENV: bad})
        with self.assertRaises(ValueError) as scheduler_error:
            SchedulerConfig.from_env({**SCHEDULER_ENV, METRIC_MAP_PATH_ENV: bad})

        for error in (gateway_error, scheduler_error):
            self.assertIn(METRIC_MAP_PATH_ENV, str(error.exception))
            self.assertNotIn("ling xi", str(error.exception))

    def test_the_gateway_prefixed_name_is_not_read(self) -> None:
        """这不是 gateway 私有配置：带 LINGXI_GATEWAY_ 前缀的同名变量不该被读取。"""

        config = load_config({**GATEWAY_ENV, f"{ENV_PREFIX}COMPANY_FUNCTION_METRIC_MAP_PATH": str(self.external)})
        self.assertIsNone(config.metric_map_path)


class ManagementCardCatalogTests(_ExternalMapFixture):
    """调用点三：管理卡的公司/指标下拉目录（展示层）。"""

    def test_the_catalog_reflects_the_injected_external_file(self) -> None:
        catalog = TomlCompanyMetricCatalog(metric_map_path=self.external)

        self.assertEqual(catalog.metrics(), (KEPT_METRIC,))
        self.assertNotIn(
            DELETED_METRIC,
            catalog.metrics(),
            "外置文件里已经删掉的指标不得继续出现在管理员的下拉里",
        )
        self.assertEqual(catalog.companies(), (COMPANY,))

    def test_a_configured_but_missing_file_degrades_to_empty_not_to_the_packaged_default(self) -> None:
        catalog = TomlCompanyMetricCatalog(metric_map_path=self.missing)

        self.assertEqual(catalog.companies(), ())
        self.assertEqual(
            catalog.metrics(),
            (),
            "配了外置文件却读不出来时必须降级为空目录，静默回落随包默认就是第二个真相",
        )

    def test_no_injected_path_still_reads_the_packaged_default(self) -> None:
        catalog = TomlCompanyMetricCatalog(metric_map_path=None)

        self.assertIn(DELETED_METRIC, catalog.metrics())


class _NullContext:
    def __enter__(self) -> _NullContext:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class _FakeCursor:
    """只认 SQL 形状的假游标：本文件断的是"读了哪一份映射"，不是 SQL 本身。"""

    def __init__(self, journal: list[tuple[str, tuple]]) -> None:
        self._journal = journal
        self._last = ""
        self.rowcount = 0

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def execute(self, sql: str, parameters: tuple = ()) -> None:
        self._last = " ".join(sql.split())
        self._journal.append((self._last, parameters))
        self.rowcount = 0

    def fetchone(self):
        if "FROM app_user" in self._last:
            return ("user-uuid-for-tests", "enabled")
        if "FROM local_permission_override" in self._last:
            return None
        if "FROM pending_action WHERE id" in self._last:
            return self._inserted_row()
        return None

    def fetchall(self) -> list:
        return []

    def _inserted_row(self) -> tuple:
        for sql, parameters in self._journal:
            if sql.startswith("INSERT INTO pending_action"):
                (
                    pending_id,
                    action_type,
                    target_open_id,
                    snapshot,
                    initiated_by,
                    created_at,
                    deadline,
                    payload,
                    origin,
                ) = parameters
                return (
                    pending_id,
                    action_type,
                    target_open_id,
                    snapshot,
                    initiated_by,
                    "pending",
                    False,
                    None,
                    None,
                    created_at,
                    deadline,
                    None,
                    None,
                    1,
                    payload,
                    origin,
                )
        raise AssertionError("还没有插入过 pending_action 行")


class _FakeConnection:
    def __init__(self, journal: list[tuple[str, tuple]]) -> None:
        self._cursor = _FakeCursor(journal)

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def transaction(self) -> _NullContext:
        return _NullContext()

    def cursor(self) -> _FakeCursor:
        return self._cursor


class _RecordingAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, dict(fields)))

    def actions(self) -> list[str]:
        return [action for action, _ in self.records]


class PositionGrantPayloadTests(_ExternalMapFixture):
    """调用点二：职位 + 公司范围授权展开成 ``pending_action.payload``。

    这条路径产出的"公司 × 指标"对就是管理员确认后要写出去的真实权限——外置文件里
    删掉的指标必须不出现在 payload 里。
    """

    def setUp(self) -> None:
        super().setUp()
        self.audit = _RecordingAudit()
        self.journal: list[tuple[str, tuple]] = []

    def _prepare(self, *, metric_map_path: Path | None):
        store = PostgresPendingActionStore(
            "postgresql://unused-by-the-fake-connection",
            audit=self.audit,
            metric_map_path=metric_map_path,
        )
        with mock.patch(
            "lingxi.adapters.postgres_pending_action.connect",
            lambda *args, **kwargs: _FakeConnection(self.journal),
        ):
            return store.prepare(
                action_type=PendingActionType.LOCAL_PERMISSION_GRANT,
                target_open_id="ou_target_for_tests",
                initiated_by_open_id="ou_admin_for_tests",
                position_name=POSITION,
                company_scope=COMPANY,
                reason="Trace #544 S-2c",
                now=NOW,
            )

    def _payload(self) -> dict:
        for sql, parameters in self.journal:
            if sql.startswith("INSERT INTO pending_action"):
                return json.loads(parameters[7])
        raise AssertionError("没有插入任何 pending_action 行")

    def test_a_metric_deleted_from_the_external_file_is_absent_from_the_payload(self) -> None:
        outcome = self._prepare(metric_map_path=self.external)

        self.assertTrue(outcome.decision.ok, outcome.decision.message)
        payload = self._payload()
        self.assertEqual(payload["pairs"], [[COMPANY, KEPT_METRIC]])
        self.assertNotIn(
            DELETED_METRIC,
            json.dumps(payload, ensure_ascii=False),
            "外置文件里已经删掉的指标出现在管理动作的 payload 里"
            "——说明这条路径读的仍然是随包默认那份映射",
        )

    def test_without_the_external_file_the_packaged_default_still_applies(self) -> None:
        """不配置外置文件仍然合法：随包默认照常展开，行为与本修复之前一致。"""

        outcome = self._prepare(metric_map_path=None)

        self.assertTrue(outcome.decision.ok, outcome.decision.message)
        self.assertIn(DELETED_METRIC, json.dumps(self._payload(), ensure_ascii=False))

    def test_a_configured_but_missing_file_refuses_the_action_without_touching_the_database(self) -> None:
        store = PostgresPendingActionStore(
            "postgresql://unused-by-the-fake-connection",
            audit=self.audit,
            metric_map_path=self.missing,
        )

        def _forbidden(*args: object, **kwargs: object):
            raise AssertionError("映射读不出来时不得连库，更不得写出任何授权")

        with mock.patch("lingxi.adapters.postgres_pending_action.connect", _forbidden):
            outcome = store.prepare(
                action_type=PendingActionType.LOCAL_PERMISSION_GRANT,
                target_open_id="ou_target_for_tests",
                initiated_by_open_id="ou_admin_for_tests",
                position_name=POSITION,
                company_scope=COMPANY,
                reason="Trace #544 S-2c",
                now=NOW,
            )

        self.assertFalse(outcome.decision.ok)
        self.assertEqual(outcome.decision.code, "position_mapping_unavailable")
        self.assertIn(
            "admin.pending_action.position_mapping_unavailable",
            self.audit.actions(),
            "失败关闭必须留痕：运维要能分辨「外置文件配错」与「这个职位本来就没配映射」",
        )


class _StopBeforeDatabase(Exception):
    """代替第一次真实数据库访问：出现它就说明映射已经读完、库还没被碰。"""


def _pending() -> PendingAction:
    return PendingAction(
        id="pac_s2c_test000000000000000",
        action_type=PendingActionType.LOCAL_PERMISSION_GRANT,
        target_open_id="ou_target_for_tests",
        target_state_snapshot="absent",
        initiated_by_open_id="ou_admin_for_tests",
        status=PendingActionStatus.EXECUTED,
        card_delivered=True,
        card_id="card_for_tests",
        reason=None,
        created_at=NOW,
        confirm_deadline_at=NOW + timedelta(minutes=10),
        decided_at=NOW,
        decided_by_open_id="ou_admin_for_tests",
    )


class TargetedRecomputeTests(_ExternalMapFixture):
    """调用点一：确认执行后的定向重算——真正把权限范围**立即发布**出去的那条路径。"""

    def setUp(self) -> None:
        super().setUp()
        self.audit = _RecordingAudit()

    def _adapter(self, metric_map_path: Path | None) -> PermissionRecomputeAdapter:
        return PermissionRecomputeAdapter(
            "postgresql://unused-in-this-test",
            audit=self.audit,
            metric_map_path=metric_map_path,
        )

    def test_the_injected_external_file_is_the_one_actually_loaded(self) -> None:
        adapter = self._adapter(self.external)

        with mock.patch(
            "lingxi.adapters.postgres_permission_recompute_trigger._resolve_target_user_id",
            side_effect=_StopBeforeDatabase,
        ):
            with self.assertLogs(
                "lingxi.adapters.company_function_metric_map_file", level="INFO"
            ) as logs:
                with self.assertRaises(_StopBeforeDatabase):
                    adapter.trigger(_pending())

        self.assertIn(
            str(self.external),
            "\n".join(logs.output),
            "定向重算读的仍然不是装配层注入的那份文件——管理动作会按另一份映射发布权限",
        )

    def test_a_configured_but_missing_file_fails_closed_before_any_database_access(self) -> None:
        adapter = self._adapter(self.missing)

        def _forbidden(*args: object, **kwargs: object):
            raise AssertionError("映射读不出来时不得连库，更不得发布任何权限范围")

        with mock.patch(
            "lingxi.adapters.postgres_permission_recompute_trigger._resolve_target_user_id",
            _forbidden,
        ):
            with self.assertRaises(FileNotFoundError):
                adapter.trigger(_pending())

    def test_without_the_external_file_the_packaged_default_is_loaded(self) -> None:
        """不配置外置文件仍然合法：读随包默认，与本修复之前逐字节一致。"""

        adapter = self._adapter(None)

        with mock.patch(
            "lingxi.adapters.postgres_permission_recompute_trigger._resolve_target_user_id",
            side_effect=_StopBeforeDatabase,
        ):
            with self.assertLogs(
                "lingxi.adapters.company_function_metric_map_file", level="INFO"
            ) as logs:
                with self.assertRaises(_StopBeforeDatabase):
                    adapter.trigger(_pending())

        self.assertIn(str(default_company_function_metric_map_path()), "\n".join(logs.output))


class GatewayAssemblyWiringTests(_ExternalMapFixture):
    """装配层真的把同一个值透传给了三个调用点——缺陷本身就发生在这一层。"""

    def setUp(self) -> None:
        super().setUp()
        # build_client 会 import lark_oapi，而 Story Fast 只装 scheduler 组，没有它；
        # 与 tests/test_gateway_config.py::BuildSupervisorTests 同一手法用桩顶上，
        # 本用例断的是装配透传，不是 SDK。
        module = types.ModuleType("lark_oapi")

        class _Builder:
            def app_id(self, value):
                return self

            def app_secret(self, value):
                return self

            def timeout(self, value):
                return self

            def build(self):
                return object()

        module.Client = types.SimpleNamespace(builder=lambda: _Builder())
        saved = sys.modules.get("lark_oapi")
        sys.modules["lark_oapi"] = module
        self.addCleanup(
            lambda: sys.modules.__setitem__("lark_oapi", saved)
            if saved is not None
            else sys.modules.pop("lark_oapi", None)
        )

    def _assemble(self, env_value: str | None) -> dict[str, object]:
        import lingxi.adapters.feishu_admin_card as admin_card_module
        import lingxi.adapters.postgres_pending_action as pending_action_module
        import lingxi.adapters.postgres_permission_recompute_trigger as recompute_module
        from lingxi.apps.gateway import build_supervisor

        env = dict(GATEWAY_ENV)
        if env_value is not None:
            env[METRIC_MAP_PATH_ENV] = env_value
        config = load_config(env)

        seen: dict[str, object] = {}

        def _recording(name: str, real):
            def factory(*args: object, **kwargs: object):
                seen[name] = kwargs.get("metric_map_path", "未传这个参数")
                return real(*args, **kwargs)

            return factory

        with mock.patch.object(
            admin_card_module,
            "TomlCompanyMetricCatalog",
            _recording("catalog", admin_card_module.TomlCompanyMetricCatalog),
        ), mock.patch.object(
            pending_action_module,
            "PostgresPendingActionStore",
            _recording("pending_action", pending_action_module.PostgresPendingActionStore),
        ), mock.patch.object(
            recompute_module,
            "PermissionRecomputeAdapter",
            _recording("recompute", recompute_module.PermissionRecomputeAdapter),
        ):
            build_supervisor(config, transport=object())

        self.assertEqual(
            sorted(seen), ["catalog", "pending_action", "recompute"], "三个调用点都必须被装配到"
        )
        return seen

    def test_all_three_call_sites_receive_the_configured_path(self) -> None:
        seen = self._assemble(str(self.external))

        for name, value in seen.items():
            with self.subTest(call_site=name):
                self.assertEqual(
                    value,
                    self.external,
                    f"{name} 没有拿到外置映射路径——它会按随包默认那份映射工作",
                )

    def test_an_unset_variable_assembles_normally_with_none(self) -> None:
        """生产刻意不设：装配照常完成，三处都是显式的 None（落回随包默认）。"""

        seen = self._assemble(None)

        for name, value in seen.items():
            with self.subTest(call_site=name):
                self.assertIsNone(value)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
