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
from lingxi.apps.gateway.config import GatewayConfigError
from lingxi.apps.gateway.config import load_config as load_gateway_config
from lingxi.apps.worker.config import WorkerConfigError
from lingxi.apps.worker.config import load_config as load_worker_config

# 最小合法基线环境：字段取值只用于让 load_config 通过必填项校验，不是真实凭据。
GATEWAY_BASE_ENV = {
    "LINGXI_GATEWAY_APP_ID": "cli_fixture_contract_test",
    "LINGXI_GATEWAY_APP_SECRET": "fixture-contract-test-secret-not-real",
    "LINGXI_GATEWAY_POSTGRES_DSN": "postgresql://lingxi:fixture-test@db.invalid/lingxi",
}
WORKER_BASE_ENV = {
    "LINGXI_WORKER_QUESTION": "受控验收夹具契约测试占位问题",
    "LINGXI_WORKER_READONLY_TOOLS": "mcp__query__list_metrics",
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
        """F3 修复（2026-08-18 编排者修复包）：不只检查字符串非空——之前的写法对
        任意文本或已经被删除的路径都判「有测试」。这里逐个解析 ``contract_tests``
        里登记的每个相对路径，并用 ``Path.is_file()`` 核实它真的存在。
        """

        for fixture in self.module.FIXTURES:
            with self.subTest(env_var=fixture.env_var):
                paths = [item.strip() for item in fixture.contract_tests.split(",") if item.strip()]
                self.assertTrue(paths, f"{fixture.env_var} 必须至少登记一个契约测试文件")
                for relative_path in paths:
                    full_path = REPOSITORY_ROOT / relative_path
                    self.assertTrue(
                        full_path.is_file(),
                        f"{fixture.env_var} 登记的契约测试文件不存在：{relative_path}"
                        "（写任意文本或已删除路径都不该被判为「有测试」）",
                    )


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


class CheckFilesFixtureDetectionTests(unittest.TestCase):
    """F2 修复（2026-08-18 编排者修复包）：夹具经 env 文件注入**容器**，不是注入
    执行本脚本的宿主 shell 进程；``--check-clear`` 查的是宿主 shell，查错了地方。
    这里钉住新增的文件/文本解析通道，它们才是 Stage 上真正要查的两面
    （env 文件本身，以及 ``docker compose exec <service> env`` 经标准输入喂进来
    的容器内实际环境）。
    """

    def setUp(self) -> None:
        self.module = _load_script()

    def test_active_fixtures_in_text_parses_env_file_style_lines(self) -> None:
        text = "\n".join(
            [
                "# a comment line",
                "",
                "export LINGXI_GATEWAY_CARD_FAILURE_INJECT=create",
                "FOO=bar",
            ]
        )
        self.assertEqual(
            self.module.active_fixtures_in_text(text),
            {"LINGXI_GATEWAY_CARD_FAILURE_INJECT": "create"},
        )

    def test_active_fixtures_in_text_parses_printenv_style_lines(self) -> None:
        """``docker compose exec <service> env`` 的输出没有注释、没有 export 前缀，
        每行就是 ``NAME=VALUE``——同一个解析函数必须两种来源都吃得下。
        """

        text = "LINGXI_WORKER_OUTPUT_SAFETY_CANARY=masked\nPATH=/usr/bin\n"
        self.assertEqual(
            self.module.active_fixtures_in_text(text),
            {"LINGXI_WORKER_OUTPUT_SAFETY_CANARY": "masked"},
        )

    def test_blank_value_in_text_counts_as_unset(self) -> None:
        text = 'LINGXI_GATEWAY_CARD_FAILURE_INJECT=""\n'
        self.assertEqual(self.module.active_fixtures_in_text(text), {})

    def test_active_fixtures_in_file_reads_a_real_file(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env.stage.gateway"
            path.write_text("LINGXI_GATEWAY_CARD_FAILURE_INJECT=update\n", encoding="utf-8")
            self.assertEqual(
                self.module.active_fixtures_in_file(path),
                {"LINGXI_GATEWAY_CARD_FAILURE_INJECT": "update"},
            )

    def test_active_fixtures_in_file_missing_file_is_empty_not_error(self) -> None:
        self.assertEqual(
            self.module.active_fixtures_in_file(Path("/nonexistent/does-not-exist-xyz")), {}
        )

    def test_cli_check_files_detects_leak_in_a_real_file(self) -> None:
        import io
        import tempfile
        import unittest.mock as mock

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env.stage.worker-queue"
            path.write_text("LINGXI_WORKER_OUTPUT_SAFETY_CANARY=withheld\n", encoding="utf-8")
            with mock.patch("sys.stderr", new=io.StringIO()) as captured:
                self.assertEqual(self.module.main(["--check-files", str(path)]), 1)
            self.assertIn("LINGXI_WORKER_OUTPUT_SAFETY_CANARY", captured.getvalue())
            self.assertNotIn("withheld", captured.getvalue())

    def test_cli_check_files_clean_file_returns_zero(self) -> None:
        import io
        import tempfile
        import unittest.mock as mock

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env.stage.scheduler"
            path.write_text("LINGXI_POSTGRES_DSN=postgresql://fixture-test\n", encoding="utf-8")
            with mock.patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(self.module.main(["--check-files", str(path)]), 0)

    def test_cli_check_files_missing_path_does_not_fail(self) -> None:
        """preflight 之外单独调用时，缺文件本身不是本命令的职责——那是 Stage
        演练脚本 preflight 步骤另外核对的事，这里只负责"存在时读到了什么"。
        """

        import io
        import unittest.mock as mock

        with mock.patch("sys.stderr", new=io.StringIO()) as captured_err, mock.patch(
            "sys.stdout", new=io.StringIO()
        ):
            self.assertEqual(self.module.main(["--check-files", "/no/such/file"]), 0)
        self.assertIn("跳过", captured_err.getvalue())

    def test_cli_check_files_reads_stdin_when_dash_given(self) -> None:
        import io
        import unittest.mock as mock

        stdin_text = "LINGXI_GATEWAY_CARD_FAILURE_INJECT=close\n"
        with mock.patch("sys.stdin", new=io.StringIO(stdin_text)), mock.patch(
            "sys.stderr", new=io.StringIO()
        ) as captured_err:
            self.assertEqual(self.module.main(["--check-files", "-"]), 1)
        self.assertIn("<stdin>", captured_err.getvalue())
        self.assertNotIn("close", captured_err.getvalue())


class ScannerKnownBlindSpotTests(unittest.TestCase):
    """P2-12 修复（2026-08-18 编排者修复包）：诚实钉住扫描器的已知盲区，不只在
    文档里口头承认。扫描器只认「``_text(env, "...")`` + 同文件顶层 ``ENV_PREFIX``」
    这一种写法；用完整变量名字面量直接读取环境变量（不经过这个拼接）的夹具会被
    漏掉。这里用一份合成源码文件证明这个盲区真实存在。
    """

    def setUp(self) -> None:
        self.module = _load_script()

    def test_full_name_literal_read_is_not_discovered(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            src_root = Path(tmp)
            package_dir = src_root / "fake_pkg"
            package_dir.mkdir()
            (package_dir / "blind_spot.py").write_text(
                "import os\n"
                'value = os.environ.get("LINGXI_SCHEDULER_SOMETHING_INJECT")\n',
                encoding="utf-8",
            )
            discovered = self.module.discover_fixture_env_vars_in_source(src_root)
            self.assertEqual(
                discovered,
                set(),
                "这条断言应当为真——full-name 字面量读取不走 _text()+ENV_PREFIX 拼接，"
                "扫描器发现不了它。如果哪天这条断言开始失败，说明扫描器实现变了，"
                "模块文档「已知盲区」那段话需要同步复核，不能留着一句不再成立的免责声明。",
            )


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
