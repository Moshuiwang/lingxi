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
    WorkerConfig,
    WorkerConfigError,
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


if __name__ == "__main__":
    unittest.main()
