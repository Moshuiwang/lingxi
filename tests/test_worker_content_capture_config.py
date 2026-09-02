"""内测轮内容级采集开关的构造期判定（Issue #251/#304 批次 3，V-采集-01…03）。

只测配置层：``_innertest_content_capture``/``WorkerConfig``/``load_config``。
写入点与真实回合的采集断言见 ``tests/test_worker_queue_consumer.py`` 的
``ContentCaptureWiringTests``；真库落库断言见
``tests/test_postgres_content_capture.py``。
"""

from __future__ import annotations

import unittest

from lingxi.apps.worker.config import (
    CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VALUE,
    CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VAR,
    CONTENT_CAPTURE_FLAG_VAR,
    DEPLOY_ENVIRONMENT_VAR,
    PRODUCTION_ENVIRONMENT_VALUES,
    WorkerConfig,
    WorkerConfigError,
    declares_production,
    load_config,
    _innertest_content_capture,
)

_READ_ONLY_TOOL = "mcp__query__list_metrics"


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "LINGXI_WORKER_QUESTION": "上周活跃用户数是多少？",
        "LINGXI_WORKER_READONLY_TOOLS": _READ_ONLY_TOOL,
        "LINGXI_WORKER_TRACE_ID": "01J0000000000000000TEST000",
    }
    env.update(overrides)
    return env


class InnertestContentCaptureFunctionTests(unittest.TestCase):
    """直接测判定函数的四种分支，覆盖比 ``load_config`` 更细的组合。"""

    def test_unset_is_disabled_and_not_misconfigured(self) -> None:
        self.assertEqual(_innertest_content_capture({}), (False, False))

    def test_blank_is_treated_as_unset(self) -> None:
        self.assertEqual(
            _innertest_content_capture({CONTENT_CAPTURE_FLAG_VAR: "   "}), (False, False)
        )

    def test_exact_match_on_both_variables_enables_capture(self) -> None:
        env = {
            CONTENT_CAPTURE_FLAG_VAR: "1",
            CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VAR: CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VALUE,
        }
        self.assertEqual(_innertest_content_capture(env), (True, False))

    def test_flag_set_but_confirm_missing_stays_disabled_but_flagged(self) -> None:
        env = {CONTENT_CAPTURE_FLAG_VAR: "1"}
        self.assertEqual(_innertest_content_capture(env), (False, True))

    def test_flag_set_but_confirm_wrong_value_stays_disabled_but_flagged(self) -> None:
        """结构性保证的核心：确认变量必须**精确**匹配，"看起来差不多"的值
        （例如只写了 "stage"）不生效——这正是挡"部署配置漂移"的那道门槛。"""

        env = {
            CONTENT_CAPTURE_FLAG_VAR: "1",
            CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VAR: "stage",
        }
        self.assertEqual(_innertest_content_capture(env), (False, True))

    def test_flag_set_to_anything_other_than_exact_one_fails_startup(self) -> None:
        """错配不是未配：宁可拒绝启动也不静默按未启用处理。"""

        for bad_value in ("true", "TRUE", "yes", "0", "2", " 1 x"):
            with self.subTest(bad_value=bad_value):
                with self.assertRaises(WorkerConfigError):
                    _innertest_content_capture({CONTENT_CAPTURE_FLAG_VAR: bad_value})

    def test_confirm_variable_alone_without_the_flag_does_nothing(self) -> None:
        """只填第二确认变量、不填主开关：仍然是未配置，不会意外生效。"""

        env = {CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VAR: CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VALUE}
        self.assertEqual(_innertest_content_capture(env), (False, False))


class WorkerConfigDefaultTests(unittest.TestCase):
    """默认关闭可被断言证明（V-采集-01 的锚点）。"""

    def test_directly_constructed_config_defaults_to_disabled(self) -> None:
        """直接构造 ``WorkerConfig``（测试/嵌入路径）同样默认关闭，不依赖
        必须经过 ``load_config`` 才安全——两条构造路径同等姿态。"""

        config = WorkerConfig(
            question="q", read_only_tools=(_READ_ONLY_TOOL,), trace_id="t", turn_timeout_seconds=1.0
        )

        self.assertFalse(config.innertest_content_capture_enabled)
        self.assertFalse(config.innertest_content_capture_misconfigured)


class LoadConfigContentCaptureTests(unittest.TestCase):
    """经完整 ``load_config`` 入口的端到端判定，含真实必填项装配。"""

    def test_default_env_has_capture_disabled(self) -> None:
        config = load_config(_env())

        self.assertFalse(config.innertest_content_capture_enabled)
        self.assertFalse(config.innertest_content_capture_misconfigured)

    def test_both_variables_matching_enables_capture(self) -> None:
        config = load_config(
            _env(
                **{
                    CONTENT_CAPTURE_FLAG_VAR: "1",
                    CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VAR: CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VALUE,
                }
            )
        )

        self.assertTrue(config.innertest_content_capture_enabled)
        self.assertFalse(config.innertest_content_capture_misconfigured)

    def test_flag_without_confirm_leaves_the_turn_runnable_but_capture_off(self) -> None:
        """结构性保证生效的可观察结果：这不是启动失败，只是采集不生效——
        队列 worker 仍然正常处理问数任务。"""

        config = load_config(_env(**{CONTENT_CAPTURE_FLAG_VAR: "1"}))

        self.assertFalse(config.innertest_content_capture_enabled)
        self.assertTrue(config.innertest_content_capture_misconfigured)
        # 其余必填配置照常解析成功，没有因为这一项被结构性挡住而启动失败。
        self.assertEqual(config.question, "上周活跃用户数是多少？")

    def test_garbage_flag_value_fails_config_loading(self) -> None:
        with self.assertRaises(WorkerConfigError):
            load_config(_env(**{CONTENT_CAPTURE_FLAG_VAR: "enabled-please"}))


class ProductionEnvironmentBackstopTests(unittest.TestCase):
    """代码侧的环境判据（对抗审查 2026-09-02 C-7）。

    此前 ``_innertest_content_capture`` 只精确匹配两个开关变量，代码里**没有任何
    一处**能回答"我现在跑在哪个环境"。两侧镜像与 compose 结构完全相同，于是
    "把 stage 的 worker env 文件整份复制进生产"这条已登记的残余风险在代码层面
    完全不可识别——两个变量会一起被抄过去，采集就在生产真的开起来，把用户问题
    原文与模型回答原文写进 ``innertest_content_capture`` 表。

    修法不是"探测"（同构环境探测不出来），是让部署**声明**自己是谁，并把这份
    声明放进入库的 `deploy/compose.prod.yaml` 的 ``environment:``（它覆盖
    ``env_file``，抄来的 env 文件覆盖不了它）。
    """

    def _both_switches_on(self, **overrides: str) -> dict[str, str]:
        env = {
            CONTENT_CAPTURE_FLAG_VAR: "1",
            CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VAR: CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VALUE,
        }
        env.update(overrides)
        return env

    def test_production_wins_over_a_fully_correct_pair_of_switches(self) -> None:
        """核心断言：两个变量都配对了，声明为生产依然不生效。"""

        for value in PRODUCTION_ENVIRONMENT_VALUES:
            with self.subTest(value=value):
                env = self._both_switches_on(**{DEPLOY_ENVIRONMENT_VAR: value})
                self.assertEqual(_innertest_content_capture(env), (False, True))

    def test_the_declaration_is_case_and_whitespace_insensitive(self) -> None:
        """运维手写的值不该因为大小写或一个尾随空格就把兜底整个关掉。"""

        for value in (" PROD ", "Prod", "PRODUCTION", "\tprod\n"):
            with self.subTest(value=value):
                env = self._both_switches_on(**{DEPLOY_ENVIRONMENT_VAR: value})
                self.assertEqual(_innertest_content_capture(env), (False, True))

    def test_stage_and_unset_keep_the_previous_behaviour_byte_for_byte(self) -> None:
        """只朝一个方向收紧：未声明或声明为别的值时行为与加这道兜底之前一致。"""

        self.assertEqual(_innertest_content_capture(self._both_switches_on()), (True, False))
        for value in ("", "   ", "stage", "dev", "prod-like", "preprod"):
            with self.subTest(value=value):
                env = self._both_switches_on(**{DEPLOY_ENVIRONMENT_VAR: value})
                self.assertEqual(_innertest_content_capture(env), (True, False))

    def test_declaring_production_never_blocks_startup(self) -> None:
        """在生产误配了主开关不得让 worker 起不来——采集是旁路能力，不是服务本体。"""

        config = load_config(
            _env(
                **{
                    CONTENT_CAPTURE_FLAG_VAR: "1",
                    CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VAR: (
                        CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VALUE
                    ),
                    DEPLOY_ENVIRONMENT_VAR: "prod",
                }
            )
        )

        self.assertFalse(config.innertest_content_capture_enabled)
        self.assertTrue(config.innertest_content_capture_misconfigured, "必须留下显眼告警")
        self.assertEqual(config.question, "上周活跃用户数是多少？")

    def test_a_garbage_flag_value_still_fails_even_in_production(self) -> None:
        """生产否决**不得**吞掉主开关的形态校验：错配仍然是启动即失败。

        顺序反了（先判生产、后判形态）会让"生产上把主开关写成 enabled-please"
        变成一条静默通过的配置——那正是主开关当初选择启动即失败要防的事。
        """

        with self.assertRaises(WorkerConfigError):
            _innertest_content_capture(
                {CONTENT_CAPTURE_FLAG_VAR: "enabled-please", DEPLOY_ENVIRONMENT_VAR: "prod"}
            )

    def test_declares_production_does_not_guess(self) -> None:
        self.assertFalse(declares_production({}))
        self.assertFalse(declares_production({DEPLOY_ENVIRONMENT_VAR: ""}))
        self.assertFalse(declares_production({DEPLOY_ENVIRONMENT_VAR: "staging"}))
        self.assertTrue(declares_production({DEPLOY_ENVIRONMENT_VAR: "prod"}))


class ProductionDeclarationLivesInComposeTest(unittest.TestCase):
    """声明必须写在**入库的 compose** 里，不是宿主机 env 文件（C-7）。

    这条与 `scripts/ci/check_deploy_contract.py` 的
    `check_prod_declares_deploy_environment` 是同一条断言的两个入口：门禁那条
    保证 CI 会红，这条保证本机 `unittest` 也会红。写进 env 文件毫无意义——
    那正是这道兜底要防的那条路径本身（compose 的 `environment:` 覆盖 `env_file`）。
    """

    def test_prod_compose_declares_production_for_both_worker_services(self) -> None:
        from pathlib import Path

        text = (Path(__file__).parents[1] / "deploy" / "compose.prod.yaml").read_text(
            encoding="utf-8"
        )
        declarations = [
            line.strip()
            for line in text.splitlines()
            if line.strip().startswith(f"{DEPLOY_ENVIRONMENT_VAR}:")
        ]

        self.assertEqual(len(declarations), 2, f"worker 与 worker-queue 各一条，实际 {declarations}")
        for line in declarations:
            value = line.split(":", 1)[1].strip().strip('"')
            self.assertIn(value.casefold(), PRODUCTION_ENVIRONMENT_VALUES)


if __name__ == "__main__":
    unittest.main()
