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

from gateway_fakes import FakeOnboarding

from lingxi.apps.gateway import (
    build_supervisor,
    main,
)
from lingxi.apps.gateway.config import ENV_PREFIX, GatewayConfigError, load_config
from lingxi.apps.gateway.onboarding import assert_gateway_onboarding_is_inert
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

    def test_card_failure_injection_defaults_to_disabled(self) -> None:
        """默认关闭：不设置该变量时装配路径必须与开关加入之前逐字节一致。"""

        config = load_config(VALID_ENV)
        self.assertIsNone(config.card_failure_injection)

    def test_card_failure_injection_accepts_the_four_legal_values(self) -> None:
        for value in ("create", "update", "close", "all"):
            with self.subTest(value=value):
                env = dict(VALID_ENV, **{f"{ENV_PREFIX}CARD_FAILURE_INJECT": value})
                config = load_config(env)
                self.assertEqual(config.card_failure_injection, value)

    def test_card_failure_injection_rejects_illegal_values_at_startup(self) -> None:
        """S-A-07 卡片故障注入开关第 1 点：非法值必须启动即失败（失败关闭），
        不能等到装配投递消费循环时才发现拼错的值被悄悄当成"未启用"放过。
        """

        env = dict(VALID_ENV, **{f"{ENV_PREFIX}CARD_FAILURE_INJECT": "createx"})
        with self.assertRaises(GatewayConfigError) as raised:
            load_config(env)
        message = str(raised.exception)
        self.assertIn(f"{ENV_PREFIX}CARD_FAILURE_INJECT", message)
        self.assertNotIn("createx", message, "报错不得回显收到的值")

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
                f"{ENV_PREFIX}QUEUE_DELAY_HINT_SECONDS",
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
                f"{ENV_PREFIX}QUEUE_DELAY_HINT_SECONDS",
            ):
                with self.subTest(raw=raw, name=name):
                    with self.assertRaises(GatewayConfigError):
                        load_config(dict(VALID_ENV, **{name: raw}))

    def test_queue_delay_hint_seconds_default_and_override(self) -> None:
        """Issue #465（rc22 S-3）：排队阈值提示定值默认 12 秒（10~15 秒区间
        中值），可用 ``LINGXI_GATEWAY_QUEUE_DELAY_HINT_SECONDS`` 覆盖。"""

        self.assertEqual(load_config(VALID_ENV).queue_delay_hint_seconds, 12.0)

        env = dict(VALID_ENV, **{f"{ENV_PREFIX}QUEUE_DELAY_HINT_SECONDS": "15"})
        self.assertEqual(load_config(env).queue_delay_hint_seconds, 15.0)


class InnertestRosterConfigTests(unittest.TestCase):
    """内测名单闸的 gateway 侧解析（Issue #302 S-N-01 的纵深，opus 批量审查 P1）。

    与 scheduler 侧 ``tests/test_scheduler_onboarding_assembly.py`` 里同名的四条
    用例逐一对应——两边读**同一个**环境变量名 `LINGXI_INNERTEST_ROSTER_OPEN_IDS`
    （不带 ``LINGXI_GATEWAY_`` 前缀），共用同一个解析函数，语义理应逐字一致。
    """

    def test_the_innertest_roster_defaults_to_an_empty_set(self) -> None:
        """未配置＝空集合＝闸对任何人全拒；不是启动失败。"""

        config = load_config(VALID_ENV)
        self.assertEqual(config.innertest_roster_open_ids, frozenset())

    def test_the_innertest_roster_parses_a_valid_list(self) -> None:
        # opus 批量审查 P2 修复：_looks_like_open_id 收紧为 ou_ 后接 20~64 位英文
        # 字母或数字，示例值必须真的满足这个形状，不能再用 "ou_a"/"ou_b" 这类过短
        # 的占位符（那类值现在会被判定为格式非法）。
        first = "ou_rostermembera00000000000"
        second = "ou_rostermemberb00000000000"
        config = load_config(
            {**VALID_ENV, "LINGXI_INNERTEST_ROSTER_OPEN_IDS": f"{first},{second}, {second}"}
        )
        self.assertEqual(config.innertest_roster_open_ids, frozenset({first, second}))

    def test_a_legal_roster_does_not_appear_in_the_config_repr(self) -> None:
        """opus 批量审查 P2 修复：与 scheduler 侧同名字段同一条纪律（
        `field(repr=False)`）——名单本身是一批飞书用户 open_id，不进
        `repr(config)`。"""

        legal_member = "ou_rostermembera00000000000"
        config = load_config({**VALID_ENV, "LINGXI_INNERTEST_ROSTER_OPEN_IDS": legal_member})

        self.assertEqual(config.innertest_roster_open_ids, frozenset({legal_member}))
        self.assertNotIn(legal_member, repr(config))

    def test_an_invalid_innertest_roster_entry_fails_startup(self) -> None:
        """错配不是未配：gateway 拒绝启动，不是悄悄退化成放行或不拦截。"""

        with self.assertRaises(GatewayConfigError):
            load_config(
                {
                    **VALID_ENV,
                    "LINGXI_INNERTEST_ROSTER_OPEN_IDS": (
                        "ou_rostermembera00000000000,not-a-valid-open-id"
                    ),
                }
            )

    def test_an_invalid_innertest_roster_error_does_not_echo_the_raw_value(self) -> None:
        with self.assertRaises(GatewayConfigError) as raised:
            load_config({**VALID_ENV, "LINGXI_INNERTEST_ROSTER_OPEN_IDS": "totally-not-an-open-id"})
        self.assertNotIn("totally-not-an-open-id", str(raised.exception))

    def test_the_variable_name_is_not_prefixed_with_lingxi_gateway(self) -> None:
        """刻意与 `LINGXI_GATEWAY_` 前缀家族分开：两个进程共享同一份名单概念，
        套上 gateway 专属前缀会让运维需要为同一份名单记两个不同的变量名。"""

        env = {**VALID_ENV, f"{ENV_PREFIX}INNERTEST_ROSTER_OPEN_IDS": "ou_should_be_ignored"}
        config = load_config(env)
        self.assertEqual(
            config.innertest_roster_open_ids,
            frozenset(),
            "带 LINGXI_GATEWAY_ 前缀的同名变量不应该被读取",
        )


class BotOpenIdConfigTests(unittest.TestCase):
    """机器人自身 open_id 的 gateway 侧解析（Issue #318 群聊@机器人固定引导，
    #328 v1.0 裁定 #5）。

    刻意不带 ``LINGXI_GATEWAY_`` 前缀——理由见 ``GatewayConfig.bot_open_id`` 的
    字段文档。未配置＝功能整体关闭（失败关闭），不是启动失败：这是一个可选职责，
    与 ``admin_group_chat_id`` 同一取舍。
    """

    def test_bot_open_id_defaults_to_none(self) -> None:
        config = load_config(VALID_ENV)
        self.assertIsNone(config.bot_open_id)

    def test_bot_open_id_is_read_from_the_unprefixed_variable(self) -> None:
        config = load_config({**VALID_ENV, "LINGXI_BOT_OPEN_ID": "ou_bot_0000000000"})
        self.assertEqual(config.bot_open_id, "ou_bot_0000000000")

    def test_blank_bot_open_id_counts_as_not_configured(self) -> None:
        config = load_config({**VALID_ENV, "LINGXI_BOT_OPEN_ID": "   "})
        self.assertIsNone(config.bot_open_id)

    def test_the_variable_name_is_not_prefixed_with_lingxi_gateway(self) -> None:
        env = {**VALID_ENV, f"{ENV_PREFIX}BOT_OPEN_ID": "ou_should_be_ignored"}
        config = load_config(env)
        self.assertIsNone(config.bot_open_id, "带 LINGXI_GATEWAY_ 前缀的同名变量不应该被读取")


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
            lambda: (
                sys.modules.__setitem__("lark_oapi", saved)
                if saved is not None
                else sys.modules.pop("lark_oapi", None)
            )
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
        self.assertEqual(transport._ack_timeout_seconds, 20.0, "单条事件的 ack 上限取停机超时")
        self.assertEqual(
            transport._handshake_timeout_seconds,
            20.0,
            "建连截止时间必须有，否则一条从未连上的连接会让进程静默失聪",
        )
        self.assertEqual(
            self.captured["timeout"],
            5.0,
            "出站 HTTP 超时必须从停机预算里分配，不能用 SDK 的 30 秒默认值——它比停机预算还长",
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

        with patch("lingxi.apps.gateway.assembly.EventPipeline") as pipeline_class:
            build_supervisor(config, transport=object(), onboarding=runner)

        self.assertIs(
            pipeline_class.call_args.kwargs["onboarding"],
            runner,
            "gateway 装配必须把 #89/#17 开通 runner 传入事件管线",
        )

    def test_the_default_onboarding_is_the_recorder_not_a_fail_closed_stub(self) -> None:
        """搬迁之后缺省实现从"失败关闭桩"变成"只记事件"。

        它**不是**放宽了失败关闭：真正的编排在 scheduler，未装配时那边不注册、
        没有任何人认领，事件原样留在库里——比让每个用户当场看到 LX-ONBOARD-001
        更接近事实，也不会把事件"认领即平账"地烧掉。
        """

        config = load_config(VALID_ENV)

        with patch("lingxi.apps.gateway.assembly.EventPipeline") as pipeline_class:
            build_supervisor(config, transport=object())

        fallback = pipeline_class.call_args.kwargs["onboarding"]
        result = fallback.start(event_id="evt", open_id="ou", trace_id="trc")
        self.assertEqual(result.state, OnboardingState.STARTED)


class GatewayOnboardingIsInertTests(unittest.TestCase):
    """搬迁之后 gateway 在开通链上的**全部**职责：记事件 + 回第一条提示。

    产品负责人 2026-08-18 裁定把编排整体移进 ``lingxi-scheduler``。因此这一组断的不再是
    「对账有没有人调」（那一路整个搬走了），而是**这里绝不能再出现会产生外部副作用的
    实现**：``EventPipeline._start_onboarding`` 在长连接事件线程里同步调用 ``start``，
    接上真编排就是 gateway 十五分钟收不到消息，而现场只表现为「机器人不理人」。
    """

    def setUp(self) -> None:
        # 与 BuildSupervisorTests 同一手法：build_supervisor 里的 build_client 会
        # import lark_oapi，而 CI 的 gate 只装 scheduler 组，没有它，用桩顶上。
        #
        # **不用 skipUnless**：跳过等于这条断言在门禁上根本不跑，而
        # `assert_gateway_onboarding_is_inert` 是编排搬进 scheduler 之后**唯一**挡住
        # 「分钟级编排又落回长连接线程」的守卫——它必须每一轮门禁都真的执行。
        # 本用例断的是 gateway 侧的惰性，不是 SDK。
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
            lambda: (
                sys.modules.__setitem__("lark_oapi", saved)
                if saved is not None
                else sys.modules.pop("lark_oapi", None)
            )
        )

    def test_the_default_onboarding_only_records(self) -> None:
        config = load_config(VALID_ENV)

        with patch("lingxi.apps.gateway.assembly.EventPipeline") as pipeline_class:
            build_supervisor(config, transport=object())

        fallback = pipeline_class.call_args.kwargs["onboarding"]
        result = fallback.start(event_id="evt", open_id="ou", trace_id="trc")
        # `started` 的含义正是"编排已异步接手、这一轮没有别的话要说"，而**不是**
        # 失败关闭桩那种当场给用户 LX-ONBOARD-001。
        self.assertEqual(result.state, OnboardingState.STARTED)
        self.assertIsNone(result.failure_reason)
        assert_gateway_onboarding_is_inert(fallback)

    def test_the_builder_reports_the_implementation_it_actually_used(self) -> None:
        config = load_config(VALID_ENV)
        reported: list[object] = []

        build_supervisor(config, transport=object(), on_onboarding_assembled=reported.append)

        self.assertEqual(len(reported), 1)
        assert_gateway_onboarding_is_inert(*reported)

    def test_an_executing_runner_fails_the_assembly(self) -> None:
        """变异形状：有人把真编排接回 gateway。"""

        class ExecutingRunner:
            def start(self, *, event_id: str, open_id: str, trace_id: str):
                raise AssertionError("不该被调用")

        with self.assertRaises(RuntimeError):
            assert_gateway_onboarding_is_inert(ExecutingRunner())

    def test_no_report_at_all_fails_instead_of_passing_vacuously(self) -> None:
        with self.assertRaises(RuntimeError):
            assert_gateway_onboarding_is_inert()

    def test_the_delivery_tick_no_longer_carries_an_onboarding_sweep(self) -> None:
        """投递循环不再顺带跑对账：那条循环上已经没有任何开通职责。"""

        import lingxi.apps.gateway as gateway_module

        self.assertFalse(hasattr(gateway_module, "build_delivery_tick"))
        self.assertFalse(hasattr(gateway_module, "build_onboarding_reconciler"))


class AssembleDeliveryConsumerCardInjectionTests(unittest.TestCase):
    """S-A-07 卡片故障注入开关的装配接线：设置后 consumer 用注入 transport，
    缺省用真实类型——验证的是 ``assemble_delivery_consumer`` 传给
    ``build_delivery_consumer`` 的 ``cards`` 参数本身，不连真实飞书或数据库。
    """

    def setUp(self) -> None:
        # 与 BuildSupervisorTests 同一手法：build_client 会 import lark_oapi，
        # CI 的 gate 只装 scheduler 组，没有它，用桩顶上。
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
            lambda: (
                sys.modules.__setitem__("lark_oapi", saved)
                if saved is not None
                else sys.modules.pop("lark_oapi", None)
            )
        )

    def test_default_configuration_passes_no_injected_transport(self) -> None:
        from lingxi.apps.gateway import assemble_delivery_consumer

        config = load_config(VALID_ENV)
        with patch("lingxi.apps.gateway.delivery.build_delivery_consumer") as builder:
            assemble_delivery_consumer(config, queue=object())

        self.assertIsNone(
            builder.call_args.kwargs["cards"],
            "缺省时必须走 build_delivery_consumer 自己的默认真实类型，"
            "不额外传入任何 cards——装配路径与本开关加入之前逐字节一致",
        )

    def test_queue_delay_hint_seconds_is_wired_from_config(self) -> None:
        """Issue #465：排队阈值提示的定值从 ``GatewayConfig`` 一路传到
        ``build_delivery_consumer``，不是装配时另起一份默认值。"""

        from lingxi.apps.gateway import assemble_delivery_consumer

        env = dict(VALID_ENV, **{f"{ENV_PREFIX}QUEUE_DELAY_HINT_SECONDS": "15"})
        config = load_config(env)
        with patch("lingxi.apps.gateway.delivery.build_delivery_consumer") as builder:
            assemble_delivery_consumer(config, queue=object())

        self.assertEqual(builder.call_args.kwargs["queue_delay_hint_seconds"], 15.0)

    def test_injected_configuration_wires_the_rejecting_transport(self) -> None:
        from lingxi.apps.gateway import assemble_delivery_consumer
        from lingxi.apps.gateway.delivery_assembly import RejectingCards

        env = dict(VALID_ENV, **{f"{ENV_PREFIX}CARD_FAILURE_INJECT": "close"})
        config = load_config(env)
        with patch("lingxi.apps.gateway.delivery.build_delivery_consumer") as builder:
            assemble_delivery_consumer(config, queue=object())

        cards = builder.call_args.kwargs["cards"]
        self.assertIsInstance(cards, RejectingCards)
        self.assertEqual(cards._inject, "close")

    def test_injected_configuration_logs_a_visible_startup_warning(self) -> None:
        """第 3 点：启用时必须有一条显眼的结构化告知，防止开关被遗忘在开启状态。"""

        from lingxi.apps.gateway import assemble_delivery_consumer

        env = dict(VALID_ENV, **{f"{ENV_PREFIX}CARD_FAILURE_INJECT": "all"})
        config = load_config(env)
        with patch("lingxi.apps.gateway.delivery.build_delivery_consumer"):
            with self.assertLogs("lingxi.apps.gateway", level="WARNING") as captured:
                assemble_delivery_consumer(config, queue=object())

        self.assertTrue(
            any("card_failure_injection_enabled" in line for line in captured.output),
            "开关开启时必须打一条能被搜到的结构化告警",
        )


class LoggingAuditLevelTests(unittest.TestCase):
    """#175/#185：失败类审计动作必须在 WARNING 级可见。

    S-A-07 r15/r19 真实验收里「已收到」表情缺失时，``reaction.failed`` 是唯一能
    回答"加表情调用到底怎么失败的"的证据；它此前记在 INFO 级、淹没在正常流水中，
    验收没有捕获到。这里锁定级别约定：``*failed`` / ``*error`` / ``*unparsable``
    记 WARNING，正常动作保持 INFO。
    """

    def test_failed_actions_log_at_warning(self) -> None:
        from lingxi.apps.gateway.audit_log import LoggingAudit

        with self.assertLogs("lingxi.apps.gateway", level="WARNING") as captured:
            LoggingAudit().record("reaction.failed", error="RuntimeError: 加表情失败")
        self.assertTrue(captured.output[0].startswith("WARNING"))
        self.assertIn("reaction.failed", captured.output[0])

    def test_unsupported_message_type_logs_at_warning(self) -> None:
        """独立审核 F5：``message.unsupported_type`` 不以失败后缀结尾，但它是
        "用户发了消息却什么都没发生"的唯一入站侧证据（r19 首轮误判正是这一类），
        必须进 WARNING 显式名单。"""

        from lingxi.apps.gateway.audit_log import LoggingAudit

        with self.assertLogs("lingxi.apps.gateway", level="WARNING") as captured:
            LoggingAudit().record("message.unsupported_type")
        self.assertTrue(captured.output[0].startswith("WARNING"))

    def test_the_new_onboarding_diagnostics_land_at_warning(self) -> None:
        """Issue #65 轻审三项修复新增的诊断动作必须同样在 WARNING 级可见。

        它们各自是唯一能回答一类问题的证据：账本没记上（下一轮会重复交接）、
        编排返回了渲染不出来的结果（用户拿到的是 LX-ONBOARD-001 而不是真实结论）、
        补交时编排失败（这条事件到此为止、不再重试）。动作名都带 ``failed``
        后缀，靠既有后缀规则升级，不再往显式名单里加条目。
        """

        from lingxi.apps.gateway.audit_log import LoggingAudit

        for action in (
            "onboarding.dispatch_record_failed",
            "onboarding.render_failed",
            "onboarding.reconcile_failed",
            "onboarding.reconcile_scan_failed",
        ):
            with self.subTest(action=action):
                with self.assertLogs("lingxi.apps.gateway", level="WARNING") as captured:
                    LoggingAudit().record(action)
                self.assertTrue(captured.output[0].startswith("WARNING"))

    def test_a_deferred_onboarding_stays_at_info(self) -> None:
        """停机中推迟触发开通属正常停机路径（与 ``reply.skipped_while_stopping``
        同类），不是诊断缺口，维持 INFO。"""

        from lingxi.apps.gateway.audit_log import LoggingAudit

        with self.assertLogs("lingxi.apps.gateway", level="INFO") as captured:
            LoggingAudit().record("onboarding.deferred_while_stopping")
        self.assertTrue(captured.output[0].startswith("INFO"))

    def test_normal_actions_stay_at_info(self) -> None:
        from lingxi.apps.gateway.audit_log import LoggingAudit

        with self.assertLogs("lingxi.apps.gateway", level="INFO") as captured:
            LoggingAudit().record("reply.sent")
        self.assertTrue(captured.output[0].startswith("INFO"))

    def test_group_chat_rejection_stays_at_info(self) -> None:
        """PR #186 补审 P3-6：群聊拒绝**不加表情、不回复、不入队**，用户侧确实
        什么都不会发生；但这份静默是刻意的产品边界（机器人不在群里暴露工作痕迹），
        是产品选择而不是诊断缺口，因此维持 INFO。WARNING 名单只收"用户本应得到
        回应却什么都没发生"的动作。"""

        from lingxi.apps.gateway.audit_log import LoggingAudit

        with self.assertLogs("lingxi.apps.gateway", level="INFO") as captured:
            LoggingAudit().record("event.rejected_non_private_chat", chat_type="group")
        self.assertTrue(captured.output[0].startswith("INFO"))


class RunShutdownClosesIdleConnectionsTests(unittest.TestCase):
    """D-17（#593 元守护审核 P2-b）：``_run()`` 停机路径必须在两条后台线程
    join 之后显式调用一次 ``lingxi.adapters.postgres.close_idle_connections``，
    不能只靠进程退出时的 ``atexit``。用轻量桩顶掉真实装配（后台循环、supervisor、
    告警职责、信号安装），只把这一段接线暴露成断言，不真的建长连接或后台线程。

    变异验红：把 ``lingxi/apps/gateway/__init__.py`` 里 ``close_idle_connections()``
    那一行删掉重跑本用例，``close_mock.assert_called_once_with()`` 会因为从未被
    调用而失败。
    """

    def test_run_calls_close_idle_connections_once_after_shutdown(self) -> None:
        from unittest import mock

        config = load_config(VALID_ENV)

        class _StubLoops:
            watchdogs: list = []

            def start(self) -> None:
                return None

            def join_within(self, clock: object, budget_seconds: float) -> None:
                del clock, budget_seconds

        class _StubSupervisor:
            def run(self, *, should_stop: object) -> object:
                del should_stop
                from lingxi.adapters.feishu_longconn import TerminationReason

                return TerminationReason.STOPPED

        with (
            mock.patch("lingxi.apps.gateway.install_signal_handlers"),
            mock.patch("lingxi.apps.gateway.build_alerting_duty", return_value=mock.MagicMock()),
            mock.patch("lingxi.apps.gateway._start_background_loops", return_value=_StubLoops()),
            mock.patch("lingxi.apps.gateway.build_supervisor", return_value=_StubSupervisor()),
            mock.patch("lingxi.apps.gateway.assert_gateway_onboarding_is_inert"),
            mock.patch("lingxi.apps.gateway.close_idle_connections") as close_mock,
        ):
            from lingxi.apps.gateway import _run

            code = _run(config)

        self.assertEqual(code, 0)
        close_mock.assert_called_once_with()


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
