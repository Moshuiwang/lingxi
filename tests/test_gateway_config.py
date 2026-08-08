"""gateway 配置与入口的断言。

补于独立复查之后：此前 ``apps/gateway/config.py`` 与 ``main()`` **零覆盖**，
把 ``_Secret.__repr__`` 和两个 ``field(repr=False)`` 一起删掉，全量测试不会有任何
一条变红——而那两样正是「凭据不进日志」这条合同承诺的实现。AGENTS.md 要求安全类
断言必须能在实现被改坏时变红，这里补上。

形状照抄同类先例 ``tests/test_scheduler_process.py``（凭据 repr、缺变量拒绝启动、
``python -m`` 冒烟），不另造一套。

认领断言：`V-部署-01`（不硬编码主机、端口、路径、密钥）的 gateway 侧，
以及合同「凭据不进代码、日志、数据库、用户环境」。
"""

from __future__ import annotations

import dataclasses
import logging
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from lingxi.apps.gateway import build_supervisor, main
from lingxi.apps.gateway.config import ENV_PREFIX, GatewayConfigError, load_config
from gateway_fakes import FakeOnboarding
from lingxi.core.conversation.ports import OnboardingState

REPOSITORY_ROOT = Path(__file__).parents[1]

# 固定假凭据探针：形态足够特殊，出现在任何输出里都能一眼认出。
FAKE_SECRET = "fake-app-secret-for-tests-only-8Xq2"
FAKE_DSN = "postgresql://lingxi:fake-password-for-tests-only@db.invalid/lingxi"

VALID_ENV = {
    f"{ENV_PREFIX}APP_ID": "cli_fake_app_id",
    f"{ENV_PREFIX}APP_SECRET": FAKE_SECRET,
    f"{ENV_PREFIX}POSTGRES_DSN": FAKE_DSN,
}


class SecretRedactionTests(unittest.TestCase):
    """凭据不得随任何一条常规输出路径外泄。"""

    def setUp(self) -> None:
        self.config = load_config(VALID_ENV)

    def test_repr_never_echoes_the_secret_values(self) -> None:
        rendered = repr(self.config)
        self.assertNotIn(FAKE_SECRET, rendered)
        self.assertNotIn(FAKE_DSN, rendered)
        self.assertNotIn("fake-password-for-tests-only", rendered)
        # 非凭据字段仍应可读，否则这个 repr 就没用了
        self.assertIn("cli_fake_app_id", rendered)

    def test_repr_of_the_secret_itself_is_redacted(self) -> None:
        """单独 repr 一个字段时也不能漏——日志里 %r 一个字段很常见。"""

        self.assertNotIn(FAKE_SECRET, repr(self.config.app_secret))
        self.assertNotIn(FAKE_DSN, repr(self.config.postgres_dsn))

    def test_logging_the_config_does_not_leak(self) -> None:
        with self.assertLogs("lingxi.test", level="INFO") as captured:
            logging.getLogger("lingxi.test").info("配置 %s", self.config)
        joined = "\n".join(captured.output)
        self.assertNotIn(FAKE_SECRET, joined)
        self.assertNotIn(FAKE_DSN, joined)

    def test_the_secret_is_still_usable_as_a_string(self) -> None:
        """脱敏只针对展示路径，取值必须照常——否则真去连库时会拿到 '<已隐去>'。"""

        self.assertEqual(str(self.config.app_secret), FAKE_SECRET)
        self.assertEqual(str(self.config.postgres_dsn), FAKE_DSN)

    def test_dataclasses_asdict_is_not_a_bypass(self) -> None:
        """``asdict`` 会绕过 repr——这条记录当前的真实边界，不是承诺它安全。"""

        values = dataclasses.asdict(self.config)
        # 事实：asdict 拿得到原值。因此不得把配置对象整体塞进结构化日志。
        self.assertEqual(str(values["app_secret"]), FAKE_SECRET)


class RequiredConfigTests(unittest.TestCase):
    """缺配置必须启动即失败，且只报变量名、不回显值。"""

    def test_missing_variables_are_named(self) -> None:
        with self.assertRaises(GatewayConfigError) as raised:
            load_config({})
        message = str(raised.exception)
        for name in ("APP_ID", "APP_SECRET", "POSTGRES_DSN"):
            self.assertIn(f"{ENV_PREFIX}{name}", message)

    def test_blank_values_count_as_missing(self) -> None:
        env = dict(VALID_ENV, **{f"{ENV_PREFIX}APP_SECRET": "   "})
        with self.assertRaises(GatewayConfigError) as raised:
            load_config(env)
        self.assertIn(f"{ENV_PREFIX}APP_SECRET", str(raised.exception))

    def test_a_bad_number_does_not_echo_its_value(self) -> None:
        env = dict(VALID_ENV, **{f"{ENV_PREFIX}RECONNECT_FACTOR": FAKE_SECRET})
        with self.assertRaises(GatewayConfigError) as raised:
            load_config(env)
        message = str(raised.exception)
        self.assertIn(f"{ENV_PREFIX}RECONNECT_FACTOR", message)
        self.assertNotIn(FAKE_SECRET, message, "报错不得回显环境变量的值")

    def test_busy_loop_backoff_is_rejected_at_startup(self) -> None:
        """零间隔或固定间隔必须在**构造期**失败，不能等到第一次断线才发现。"""

        for name, value in (
            (f"{ENV_PREFIX}RECONNECT_BASE_SECONDS", "0"),
            (f"{ENV_PREFIX}RECONNECT_FACTOR", "1"),
        ):
            with self.subTest(name):
                with self.assertRaises(GatewayConfigError):
                    load_config(dict(VALID_ENV, **{name: value}))

    def test_defaults_are_a_valid_backoff(self) -> None:
        config = load_config(VALID_ENV)
        self.assertGreater(config.reconnect_base_seconds, 0)
        self.assertGreater(config.reconnect_factor, 1)
        self.assertGreaterEqual(config.reconnect_ceiling_seconds, config.reconnect_base_seconds)
        self.assertGreater(config.shutdown_timeout_seconds, 0)


    def test_non_finite_numbers_are_rejected(self) -> None:
        """``nan`` / ``inf`` 是合法的 float 字面量，会一路通过后面所有比较。

        ``nan > 0`` 为假、``nan <= x`` 也为假，于是退避校验放它过去，进程在第一次
        断线时睡 ``inf`` 秒——一个永远不会恢复、也不会报错的挂起。
        """

        for raw in ("nan", "NaN", "inf", "-inf", "Infinity"):
            for name in (
                f"{ENV_PREFIX}RECONNECT_BASE_SECONDS",
                f"{ENV_PREFIX}RECONNECT_FACTOR",
                f"{ENV_PREFIX}RECONNECT_CEILING_SECONDS",
                f"{ENV_PREFIX}SHUTDOWN_TIMEOUT_SECONDS",
            ):
                with self.subTest(raw=raw, name=name):
                    with self.assertRaises(GatewayConfigError):
                        load_config(dict(VALID_ENV, **{name: raw}))


    def test_non_positive_numbers_are_rejected(self) -> None:
        """本组数值全是时长或倍数，0 与负数没有一个有意义。

        停机超时尤其致命：``<= 0`` 让「在超时内退出」退化成「立刻放弃在途事件」，
        而它还被用来推导空闲轮询间隔、ack 上限与出站超时，负值会一路传染。
        """

        for raw in ("0", "-1", "-0.5"):
            for name in (
                f"{ENV_PREFIX}RECONNECT_BASE_SECONDS",
                f"{ENV_PREFIX}RECONNECT_FACTOR",
                f"{ENV_PREFIX}RECONNECT_CEILING_SECONDS",
                f"{ENV_PREFIX}SHUTDOWN_TIMEOUT_SECONDS",
            ):
                with self.subTest(raw=raw, name=name):
                    with self.assertRaises(GatewayConfigError):
                        load_config(dict(VALID_ENV, **{name: raw}))


class BuildSupervisorTests(unittest.TestCase):
    """``build_supervisor`` 的装配，含空闲轮询间隔的推导。"""

    def setUp(self) -> None:
        # build_client 会 import lark_oapi；CI 的 gate 只装 scheduler 组，没有它。
        # 用桩顶上，本用例断的是装配而不是 SDK。
        module = types.ModuleType("lark_oapi")

        captured = self.captured = {}

        class _Builder:
            def app_id(self, value):
                return self

            def app_secret(self, value):
                return self

            def timeout(self, value):
                # 出站超时必须被显式设置：SDK 默认 30 秒比停机预算还长。
                captured["timeout"] = value
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

    def test_poll_interval_is_derived_from_the_shutdown_timeout(self) -> None:
        config = load_config(dict(VALID_ENV, **{f"{ENV_PREFIX}SHUTDOWN_TIMEOUT_SECONDS": "20"}))
        supervisor = build_supervisor(config)

        transport = supervisor._transport
        self.assertEqual(
            transport._poll_seconds,
            5.0,
            "空闲轮询间隔必须由停机超时推导，否则配置里的超时是一句没实现的承诺",
        )
        self.assertEqual(
            transport._ack_timeout_seconds, 20.0, "单条事件的 ack 上限取停机超时"
        )
        self.assertEqual(
            transport._handshake_timeout_seconds,
            20.0,
            "建连截止时间必须有，否则一条从未连上的连接会让进程静默失聪",
        )
        self.assertEqual(
            self.captured["timeout"],
            5.0,
            "出站 HTTP 超时必须从停机预算里分配，不能用 SDK 的 30 秒默认值"
            "——它比停机预算还长",
        )
        self.assertLess(
            self.captured["timeout"],
            20.0,
            "出站超时不得大于等于停机预算，否则一次卡住的回复就能让停机超出承诺",
        )

    def test_poll_interval_has_a_floor(self) -> None:
        config = load_config(dict(VALID_ENV, **{f"{ENV_PREFIX}SHUTDOWN_TIMEOUT_SECONDS": "0.01"}))
        supervisor = build_supervisor(config)

        self.assertGreaterEqual(
            supervisor._transport._poll_seconds, 0.1, "轮询间隔不得小到变成忙循环"
        )

    def test_backoff_comes_from_the_configuration(self) -> None:
        config = load_config(
            dict(
                VALID_ENV,
                **{
                    f"{ENV_PREFIX}RECONNECT_BASE_SECONDS": "2",
                    f"{ENV_PREFIX}RECONNECT_FACTOR": "3",
                    f"{ENV_PREFIX}RECONNECT_CEILING_SECONDS": "40",
                },
            )
        )
        supervisor = build_supervisor(config)

        self.assertEqual(supervisor._backoff.delay_for(0), 2.0)
        self.assertEqual(supervisor._backoff.delay_for(1), 6.0)
        self.assertEqual(supervisor._backoff.delay_for(10), 40.0)

    def test_an_injected_transport_is_used_instead_of_the_real_one(self) -> None:
        config = load_config(VALID_ENV)
        sentinel = object()

        supervisor = build_supervisor(config, transport=sentinel)

        self.assertIs(supervisor._transport, sentinel, "注入的传输层必须被采用")

    def test_injected_onboarding_runner_reaches_the_event_pipeline(self) -> None:
        config = load_config(VALID_ENV)
        runner = FakeOnboarding()

        with patch("lingxi.apps.gateway.EventPipeline") as pipeline_class:
            build_supervisor(config, transport=object(), onboarding=runner)

        self.assertIs(
            pipeline_class.call_args.kwargs["onboarding"],
            runner,
            "gateway 装配必须把 #89/#17 开通 runner 传入事件管线",
        )

    def test_missing_onboarding_runner_fails_closed(self) -> None:
        config = load_config(VALID_ENV)

        with patch("lingxi.apps.gateway.EventPipeline") as pipeline_class:
            build_supervisor(config, transport=object())

        fallback = pipeline_class.call_args.kwargs["onboarding"]
        result = fallback.start(event_id="evt", open_id="ou", trace_id="trc")
        self.assertEqual(result.state, OnboardingState.INTERNAL_ERROR)


class EntryPointTests(unittest.TestCase):
    def test_main_refuses_an_empty_configuration(self) -> None:
        self.assertEqual(main(env={}), 2, "缺配置必须以退出码 2 拒绝启动")

    def test_python_m_starts_and_refuses_an_empty_configuration(self) -> None:
        """``python -m`` 真的能起来——测试全绿但进程起不来正是 V-部署-10 的形状。"""

        process = subprocess.run(
            [sys.executable, "-m", "lingxi.apps.gateway"],
            cwd=REPOSITORY_ROOT,
            env={"PYTHONPATH": str(REPOSITORY_ROOT / "src"), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(process.returncode, 2)
        self.assertIn(f"{ENV_PREFIX}APP_ID", process.stderr)
        self.assertNotIn(FAKE_SECRET, process.stderr)


if __name__ == "__main__":
    unittest.main()
