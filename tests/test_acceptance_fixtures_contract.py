"""夹具契约测试（Epic E 验收资产第 4 件，S-E-01，Issue #161）。

Trace v12（Issue #147）新增「受控触发夹具确定性合同」：受控失败、安全、降级类
旅程必须用确定性触发机制而不是模型行为或偶发外部故障来构造，且满足默认关闭、
失败关闭、用后撤除并回读、夹具输入契约与生产解析同口径且自身有测试。

仓库里已有的两个正式夹具——``LINGXI_GATEWAY_CARD_FAILURE_INJECT`` 与
``LINGXI_WORKER_OUTPUT_SAFETY_CANARY``——各自的业务分支已经在
``tests/test_gateway_config.py``、``tests/test_gateway_delivery.py`` 与
``tests/test_worker_entry.py`` 里有相当完整的契约测试。本文件**不重复**验证它们
各自的业务分支，只钉住此前没有任何地方钉住的三件事：

1. ``scripts/acceptance_fixtures.py`` 的登记表与源码实际存在的夹具变量**不漂移**
   ——新增一个跟随既有 ``*_INJECT`` / ``*_CANARY`` 命名纪律的夹具却忘记登记时，
   本文件必须变红；
2. 每个已登记夹具**确实**满足「默认关闭」与「非法值失败关闭」这两条硬要求——
   直接调用生产配置加载器（与 Stage 上实际启动时同一份解析逻辑），不是重新
   实现一遍判断逻辑；
3. 「用后撤除并回读」这条硬要求本身有可执行、可测试的落点
   （``active_fixtures`` / ``assert_no_active_fixtures``），供 Stage 演练脚本在
   验收窗口开窗前与收窗后调用。

加载方式照抄既有先例 ``tests/test_replay_inbound_event_script.py``：``scripts/``
不是一个包，用 ``importlib.util.spec_from_file_location`` 按路径直接装载模块。
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[1]
SCRIPT = REPOSITORY_ROOT / "scripts" / "acceptance_fixtures.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("acceptance_fixtures_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # 模块用了 @dataclass；dataclasses 内部按 ``sys.modules[cls.__module__]`` 解析
    # 注解，装载期必须先在 sys.modules 挂号，否则会在类体求值时抛
    # ``AttributeError: 'NoneType' object has no attribute '__dict__'``。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# 两个生产配置加载器与本文件同套断言复用同一份解析逻辑，不是重新实现一遍。
from lingxi.apps.gateway.config import GatewayConfigError, load_config as load_gateway_config
from lingxi.apps.worker.config import WorkerConfigError, load_config as load_worker_config

# 最小合法基线环境：字段取值只用于让 load_config 通过必填项校验，不是真实凭据。
GATEWAY_BASE_ENV = {
    "LINGXI_GATEWAY_APP_ID": "cli_fixture_contract_test",
    "LINGXI_GATEWAY_APP_SECRET": "fixture-contract-test-secret-not-real",
    "LINGXI_GATEWAY_POSTGRES_DSN": "postgresql://lingxi:fixture-test@db.invalid/lingxi",
}
WORKER_BASE_ENV = {
    "LINGXI_WORKER_QUESTION": "受控验收夹具契约测试占位问题",
    "LINGXI_WORKER_READONLY_TOOL": "mcp__bi-metric__list_metrics",
}


class RegistryMatchesSourceTests(unittest.TestCase):
    """登记表必须和源码里实际存在的 *_INJECT / *_CANARY 变量精确一致。"""

    def setUp(self) -> None:
        self.module = _load_script()

    def test_scanner_is_not_a_dead_probe(self) -> None:
        """防空转（验证与门禁第八节）：扫描器必须真的提取到东西，否则下面的相等
        断言在扫描器被改坏、恒返回空集时也会"通过"。
        """

        discovered = self.module.discover_fixture_env_vars_in_source()
        self.assertGreaterEqual(
            len(discovered), 2, "扫描器应至少发现 CARD_FAILURE_INJECT 与 OUTPUT_SAFETY_CANARY"
        )

    def test_registry_matches_what_source_actually_defines(self) -> None:
        discovered = self.module.discover_fixture_env_vars_in_source()
        registered = set(self.module.FIXTURE_ENV_VARS)
        self.assertEqual(
            discovered,
            registered,
            "scripts/acceptance_fixtures.py 的 FIXTURES 登记表与源码实际存在的"
            "*_INJECT / *_CANARY 环境变量不一致——新增或改名夹具时必须同步更新登记表。",
        )

    def test_each_fixture_has_at_least_one_named_contract_test_file(self) -> None:
        for fixture in self.module.FIXTURES:
            with self.subTest(env_var=fixture.env_var):
                self.assertTrue(fixture.contract_tests.strip())


class ActiveFixtureDetectionTests(unittest.TestCase):
    """「用后撤除并回读」这条硬要求的可执行落点。"""

    def setUp(self) -> None:
        self.module = _load_script()

    def test_clean_environment_has_no_active_fixtures(self) -> None:
        self.assertEqual(self.module.active_fixtures({}), {})
        self.module.assert_no_active_fixtures({})  # 不抛异常

    def test_each_registered_fixture_is_detected_when_set(self) -> None:
        for var in self.module.FIXTURE_ENV_VARS:
            with self.subTest(env_var=var):
                env = {var: "some-value"}
                self.assertEqual(self.module.active_fixtures(env), {var: "some-value"})
                with self.assertRaises(RuntimeError) as raised:
                    self.module.assert_no_active_fixtures(env)
                # 报错不回显具体取值，只回显变量名（与业务侧夹具同一纪律）。
                self.assertIn(var, str(raised.exception))
                self.assertNotIn("some-value", str(raised.exception))

    def test_blank_value_counts_as_unset(self) -> None:
        """与各夹具自身的 ``_text()`` 解析口径一致：空字符串/纯空白视为未设置。"""

        for var in self.module.FIXTURE_ENV_VARS:
            with self.subTest(env_var=var):
                self.assertEqual(self.module.active_fixtures({var: "   "}), {})

    def test_multiple_active_fixtures_are_all_reported(self) -> None:
        env = {var: "x" for var in self.module.FIXTURE_ENV_VARS}
        self.assertEqual(set(self.module.active_fixtures(env)), set(self.module.FIXTURE_ENV_VARS))


class GatewayCardFailureInjectFixtureContractTests(unittest.TestCase):
    """默认关闭 + 非法值失败关闭，直接调用生产配置加载器。"""

    def test_default_is_disabled(self) -> None:
        config = load_gateway_config(GATEWAY_BASE_ENV)
        self.assertIsNone(config.card_failure_injection)

    def test_illegal_value_fails_closed_at_load_time(self) -> None:
        env = dict(GATEWAY_BASE_ENV, LINGXI_GATEWAY_CARD_FAILURE_INJECT="not-a-real-value")
        with self.assertRaises(GatewayConfigError):
            load_gateway_config(env)

    def test_legal_values_are_accepted(self) -> None:
        for value in ("create", "update", "close", "all"):
            with self.subTest(value=value):
                env = dict(GATEWAY_BASE_ENV, LINGXI_GATEWAY_CARD_FAILURE_INJECT=value)
                config = load_gateway_config(env)
                self.assertEqual(config.card_failure_injection, value)


class WorkerOutputSafetyCanaryFixtureContractTests(unittest.TestCase):
    """默认关闭 + 非法值失败关闭，直接调用生产配置加载器。"""

    def test_default_is_disabled(self) -> None:
        config = load_worker_config(WORKER_BASE_ENV)
        self.assertIsNone(config.output_safety_canary)

    def test_illegal_value_fails_closed_at_load_time(self) -> None:
        env = dict(WORKER_BASE_ENV, LINGXI_WORKER_OUTPUT_SAFETY_CANARY="not-a-real-value")
        with self.assertRaises(WorkerConfigError):
            load_worker_config(env)

    def test_legal_value_requires_a_paired_system_prompt(self) -> None:
        """合法档位缺配套 system prompt 时同样失败关闭（不是"悄悄不生效」）。"""

        env = dict(WORKER_BASE_ENV, LINGXI_WORKER_OUTPUT_SAFETY_CANARY="masked")
        with self.assertRaises(WorkerConfigError):
            load_worker_config(env)


class CliSelfCheckTests(unittest.TestCase):
    """Stage 演练脚本调用的 CLI 出口：受控子进程环境验证。"""

    def setUp(self) -> None:
        self.module = _load_script()

    def test_check_clear_returns_zero_on_clean_environment(self) -> None:
        import io
        import unittest.mock as mock

        with mock.patch.object(self.module.os, "environ", {}):
            with mock.patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(self.module.main(["--check-clear"]), 0)

    def test_check_clear_returns_nonzero_when_a_fixture_leaks(self) -> None:
        import io
        import unittest.mock as mock

        leaked_env = {self.module.FIXTURE_ENV_VARS[0]: "left-on-by-mistake"}
        with mock.patch.object(self.module.os, "environ", leaked_env):
            with mock.patch("sys.stderr", new=io.StringIO()) as captured_err:
                self.assertEqual(self.module.main(["--check-clear"]), 1)
            self.assertIn(self.module.FIXTURE_ENV_VARS[0], captured_err.getvalue())
            self.assertNotIn("left-on-by-mistake", captured_err.getvalue())

    def test_list_prints_every_registered_fixture(self) -> None:
        import io
        import unittest.mock as mock

        with mock.patch("sys.stdout", new=io.StringIO()) as captured_out:
            self.assertEqual(self.module.main(["--list"]), 0)
        output = captured_out.getvalue()
        for var in self.module.FIXTURE_ENV_VARS:
            self.assertIn(var, output)


if __name__ == "__main__":
    unittest.main()
