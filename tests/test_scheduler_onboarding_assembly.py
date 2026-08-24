"""首次开通编排在 scheduler 的装配用例（Epic D / S-D-02，搬迁后）。

这份用例守的是**只会在生产暴露**的那几条：Epic C 交办的三条装配断言（执行级硬截止、
探针超时与就绪节奏一致、注入单调时钟）、本轮新增的第四条（认领量必须被执行器容量压住）、
以及「前置不齐就不注册，而不是注册一个会把事件认领走再烧掉的实现」。

**装配不变量（外部集成面审查 F1）**：``AssemblyInvariantTests`` 与
``InvariantBuildLoopTests`` 守 ``onboarding != None ⇒ permission_publish != None 且
permission_publish.publish_wired``——`permission_publish` 的**发布面**前置不齐时开通编排
不能照常注册再把用户接收进来又永远走不到可用。判据从 ``is not None`` 收紧到
``publish_wired`` 是冻结候选审查 2026-08-21 的 F1：缺权限表 Base 坐标时装出来的是一个
**executor=None 的"仅就绪"职责**（非 ``None``），旧判据对它放行。
"""

from __future__ import annotations

import importlib.util
import threading
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from lingxi.apps.scheduler import (
    SchedulerConfig,
    _build_onboarding_duty,
    _build_stalled_provisioning_duty,
)
from lingxi.apps.scheduler.onboarding import (
    DISPATCH_AFTER,
    PROBE_WATCHDOG_MARGIN_SECONDS,
    HardDeadlineProbe,
    OnboardingExecutor,
    assert_claim_limit_follows_capacity,
    assert_probe_timeouts_agree,
    assert_stalled_lease_exceeds_chain_budget,
    monotonic_utc_clock,
)
from lingxi.apps.scheduler.stalled_provisioning import DEFAULT_STALLED_LEASE_SECONDS
from lingxi.core.conversation.onboarding_recovery import OnboardingReconciler
from lingxi.core.permission.mcp_readiness import McpProbeError, ReadinessSchedule

# 32 字节 base64 主密钥（非生产值，只为过形状校验）。
MASTER_KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
BASE_ENV = {
    "LINGXI_POSTGRES_DSN": "postgresql://localhost/lingxi",
    "LINGXI_DELEGATED_CREDENTIAL_KEY": "x" * 44,
    "LINGXI_DELEGATED_CREDENTIAL_PATH": "/tmp/lingxi-credential",
    "LINGXI_FEISHU_APP_ID": "cli_test",
    "LINGXI_FEISHU_APP_SECRET": "secret",
}
WIRED_ENV = {
    **BASE_ENV,
    "LINGXI_MCP_TOKEN_ENCRYPT_KEY": MASTER_KEY,
    "LINGXI_QUERY_MCP_ENDPOINT": "https://mcp.example.internal/query",
    "LINGXI_USER_ENV_ROOT": "/var/lib/lingxi/users",
}


class RecordingAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, object]]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, dict(fields)))

    def actions(self) -> list[str]:
        return [action for action, _ in self.records]


class StubPermissionPublish:
    """``PermissionPublishDuty`` 的最小替身：不变量只读 :attr:`publish_wired`。

    ``publish_wired=False`` 就是生产里那个**只装了就绪面**的半个职责
    （``executor=None``，见 ``_build_permission_publish_duty``）——它不是 ``None``，
    但没有任何东西会把 outbox 里的发布意图写出去。
    """

    def __init__(self, *, publish_wired: bool = True) -> None:
        self.publish_wired = publish_wired


#: 装配不变量测试之外的默认值：代表"已装配、发布面也接线了"。
_WIRED_PERMISSION_PUBLISH = StubPermissionPublish()


def build(
    env: dict[str, str],
    *,
    token: Any = None,
    metric_translation_map: Any = None,
    permission_publish: Any = _WIRED_PERMISSION_PUBLISH,
    onboarding_failed: Any = None,
) -> tuple[Any, RecordingAudit]:
    audit = RecordingAudit()
    duty = _build_onboarding_duty(
        SchedulerConfig.from_env(env),
        stop=threading.Event(),
        audit=audit,
        employment_access_token=token,
        # 这份用例守的是装配级断言（执行级硬截止、探针超时一致、单调时钟、认领量），
        # 不是发布闸——闸的真实来源、共用来源与变异锚点见
        # ``tests/test_onboarding_runner.PublishGateTests``。默认给一个恒不可用的空
        # 映射（与占位期同一个失败关闭方向），不影响本文件任何一条断言。
        metric_translation_map=metric_translation_map if metric_translation_map is not None else {},
        # 装配不变量（F1）：默认给一个非 ``None`` 的哨兵值，代表"权限发布消费职责
        # 已装配"——本文件其余用例守的是别的前置，不该被这条新增的不变量挡住。
        # ``AssemblyInvariantTests`` 单独测这条不变量本身。
        permission_publish=permission_publish,
        onboarding_failed=onboarding_failed,
    )
    return duty, audit


class PrerequisiteTests(unittest.TestCase):
    """前置不齐就**不注册**，并留下恰一条审计（`V-花名册-29` 的同一条纪律）。

    不注册的后果是**没有任何人认领** `auto_provisioning` 事件——它们原样留在库里。
    这比搬迁前的失败关闭桩安全：桩会「认领即平账」，把事件永久烧掉。
    """

    def test_missing_master_key_is_reported_by_variable_name_only(self) -> None:
        duty, audit = build(BASE_ENV)
        self.assertIsNone(duty)
        self.assertEqual(audit.actions(), ["onboarding.duty_not_registered"])
        self.assertEqual(audit.records[0][1]["variable"], "LINGXI_MCP_TOKEN_ENCRYPT_KEY")

    def test_missing_query_endpoint_stops_the_assembly(self) -> None:
        duty, audit = build({**BASE_ENV, "LINGXI_MCP_TOKEN_ENCRYPT_KEY": MASTER_KEY})
        self.assertIsNone(duty)
        self.assertEqual(audit.records[0][1]["variable"], "LINGXI_QUERY_MCP_ENDPOINT")

    def test_missing_user_environment_root_stops_the_assembly(self) -> None:
        env = {
            **BASE_ENV,
            "LINGXI_MCP_TOKEN_ENCRYPT_KEY": MASTER_KEY,
            "LINGXI_QUERY_MCP_ENDPOINT": "https://mcp.example.internal/query",
        }
        duty, audit = build(env)
        self.assertIsNone(duty)
        self.assertEqual(audit.records[0][1]["variable"], "LINGXI_USER_ENV_ROOT")

    def test_an_unwired_employment_token_supply_stops_the_assembly(self) -> None:
        """在职状态是产品合同的硬门槛（`V-开通-07`），不能「先跳过这一步」。"""

        duty, audit = build(WIRED_ENV)
        self.assertIsNone(duty)
        self.assertEqual(audit.actions(), ["onboarding.duty_not_registered"])
        self.assertEqual(audit.records[0][1]["reason"], "employment_access_token_unwired")

    def test_a_fully_wired_assembly_produces_a_claiming_duty(self) -> None:
        duty, audit = build(WIRED_ENV, token=lambda: "u-token")

        self.assertIsInstance(duty, OnboardingReconciler)
        self.assertEqual(audit.actions(), [], "装配成功时不该留「未装配」审计")
        self.assertIsNotNone(duty.capacity_source, "认领量必须绑上执行器剩余容量")

    def test_the_employment_reader_client_uses_stop_wait_as_its_sleeper(self) -> None:
        """Issue #284 A 组 #4：开通链 employment reader 的 client 也要接上
        `sleep=stop.wait`（设计 §4 的两处接线点之一，另一处是组织快照，见
        ``tests/test_scheduler_org_snapshot_assembly.py::HardeningWiringTests``）。
        `threading.Event.wait` 每次取值都会新建一个绑定方法包装对象，因此用
        `==`——绑定方法比较的是底层函数与所属实例是否相同，同一个 `Event` 的
        `wait` 恒相等；用 `is` 会误判为不等。"""

        stop = threading.Event()
        audit = RecordingAudit()
        duty = _build_onboarding_duty(
            SchedulerConfig.from_env(WIRED_ENV),
            stop=stop,
            audit=audit,
            employment_access_token=lambda: "u-token",
            metric_translation_map={},
            permission_publish=_WIRED_PERMISSION_PUBLISH,
        )

        self.assertIsInstance(duty, OnboardingReconciler)
        client = duty._onboarding._employment._client
        self.assertEqual(client._sleep, stop.wait, "节流/退避的 sleeper 必须绑定这一份 stop，不是默认的 time.sleep")


class AssemblyInvariantTests(unittest.TestCase):
    """F1（外部集成面审查，必修）：``onboarding != None ⇒ permission_publish != None
    且 permission_publish.publish_wired``。

    权限发布消费职责（``_build_permission_publish_duty``）的**发布面**是**唯一**会把
    开通链排进 ``publish_outbox`` 的那条意图真正写进外部权限表的执行者。它前置不齐时
    开通编排不能照常注册——那样会让用户先被认领、建档、建好用户环境，然后**永远**卡在
    半开状态；迟到就绪恢复（V-开通-18）救不了这个缺口，它只能确认"已经进入就绪等待"的人。

    这里**不接受**「``permission_publish`` 与 ``onboarding`` 当前碰巧共用同一个 MCP
    主密钥前置，所以传什么值都无所谓」这种论证：下面每一条用例在 ``permission_publish``
    以外的全部前置都齐全（含在职状态令牌供给）的情况下，只翻动 ``permission_publish``
    这一个参数，直接证明不变量是装配层自己校验出来的，不是靠前置巧合撞上的。

    **``publish_wired=False`` 那一条是冻结候选审查 2026-08-21 F1 的锚点**：它就是缺
    ``LINGXI_PERMISSION_BITABLE_APP_TOKEN``/``TABLE_ID`` 时生产真实装出来的形状——
    职责对象存在（就绪面照常）、发布执行者不在。旧判据 ``is None`` 对它放行。
    """

    def test_a_none_permission_publish_stops_onboarding_before_any_other_check(self) -> None:
        """即使 ``permission_publish`` 以外的前置一个都没配（``BASE_ENV``），报出来的
        原因也必须是「发布职责未装配」，不是其它某个环境变量——这条不变量排在最前面，
        不依赖后面几条检查有没有跑到。"""

        duty, audit = build(BASE_ENV, permission_publish=None)

        self.assertIsNone(duty)
        self.assertEqual(audit.actions(), ["onboarding.duty_not_registered"])
        self.assertEqual(
            audit.records[0][1], {"reason": "permission_publish_not_assembled"}
        )

    def test_a_none_permission_publish_stops_onboarding_even_when_everything_else_is_wired(
        self,
    ) -> None:
        """真正的红线用例：``permission_publish`` 以外的每一项前置（含在职状态令牌
        供给）都齐全，唯独 ``permission_publish`` 是 ``None``——开通编排仍然不能注册。

        变异验红：把 ``_build_onboarding_duty`` 里 `if permission_publish is None:
        ...; return None` 那段删掉（或把条件恒设为假）之后重跑本用例，
        ``duty`` 会变成一个真实的 ``OnboardingReconciler``、`assertIsNone` 失败——
        这就是这条不变量「会变红」的实际证据。
        """

        duty, audit = build(WIRED_ENV, token=lambda: "u-token", permission_publish=None)

        self.assertIsNone(duty, "permission_publish 未装配时开通编排不得注册")
        self.assertEqual(audit.actions(), ["onboarding.duty_not_registered"])
        self.assertEqual(
            audit.records[0][1],
            {"reason": "permission_publish_not_assembled"},
            "审计只报「发布职责未装配」这一个结构性原因，不夹带其它前置的原因",
        )

    def test_a_publish_wired_permission_publish_does_not_block_assembly(self) -> None:
        """发布面接线了就放行——不变量只读 ``publish_wired``，不对
        ``permission_publish`` 的具体类型或其余内容做任何进一步假设。"""

        duty, audit = build(
            WIRED_ENV,
            token=lambda: "u-token",
            permission_publish=StubPermissionPublish(publish_wired=True),
        )

        self.assertIsInstance(duty, OnboardingReconciler)
        self.assertEqual(audit.actions(), [])

    def test_a_readiness_only_permission_publish_stops_onboarding(self) -> None:
        """**冻结候选审查 2026-08-21 F1 的红线用例**：职责对象在、但发布面没装配
        （``executor=None`` 的"仅就绪"职责，缺权限表 Base 坐标时生产真实装出来的形状）
        ——开通编排同样不得注册。

        产品负责人 2026-08-21 第二次真实开通失败（``publish_not_completed``）就是这条：
        用户被认领、建档、建好用户环境，发布意图排进了 ``publish_outbox``，而 outbox
        那一侧根本没有执行者。

        变异验红：把 ``_build_onboarding_duty`` 的不变量改回
        ``if permission_publish is None:``（#279 之前那一版）之后重跑本用例，``duty``
        会变成一个真实的 ``OnboardingReconciler``、``assertIsNone`` 失败。
        """

        duty, audit = build(
            WIRED_ENV,
            token=lambda: "u-token",
            permission_publish=StubPermissionPublish(publish_wired=False),
        )

        self.assertIsNone(duty, "发布面未装配时开通编排不得注册")
        self.assertEqual(audit.actions(), ["onboarding.duty_not_registered"])
        self.assertEqual(
            audit.records[0][1],
            {"reason": "permission_publish_not_wired"},
            "两种拒绝的原因码必须可分辨：这一条要去补权限表 Base 坐标，"
            "而不是 MCP 那一组配置",
        )


class PublishGateWiringTests(unittest.TestCase):
    """发布闸的真实判据必须来自 :func:`_build_onboarding_duty` 收到的
    ``metric_translation_map``，不是硬编码的常量（Issue #227 开通侧整合）。

    ``tests/test_onboarding_runner.PublishGateTests`` 测的是 ``AutoOnboardingRunner``
    收到一个 ``publish_allowed`` 可调用对象之后的行为——注入什么就执行什么，不覆盖
    "装配层怎么算出这个可调用对象"这一段。本类补的正是这一段：把
    ``_build_onboarding_duty`` 里 ``publish_allowed=lambda: metric_translation_available(
    metric_translation_map)`` 写死成 ``lambda: True`` 会被
    ``test_an_empty_translation_map_keeps_the_gate_closed`` 直接测穿——它绕开数据库
    与飞书 I/O（构造期两者都不发起真实请求），只读装配产出的可调用对象本身。
    """

    def test_an_empty_translation_map_keeps_the_gate_closed(self) -> None:
        duty, _ = build(WIRED_ENV, token=lambda: "u-token", metric_translation_map={})

        self.assertFalse(duty._onboarding._publish_allowed())

    def test_a_populated_translation_map_opens_the_gate(self) -> None:
        duty, _ = build(
            WIRED_ENV,
            token=lambda: "u-token",
            metric_translation_map={"co_1": {"运营": ("日活",)}},
        )

        self.assertTrue(duty._onboarding._publish_allowed())

    def test_the_gate_reads_the_shared_object_not_a_copy(self) -> None:
        """装配层只加载一次映射：闸门必须绑定**同一个**对象引用，不是构造时拷贝出的
        一份快照或提前算好的布尔值——否则「共用同一个已加载对象」只是名义上的。"""

        mapping = {"co_1": {"运营": ("日活",)}}
        duty, _ = build(WIRED_ENV, token=lambda: "u-token", metric_translation_map=mapping)

        self.assertTrue(duty._onboarding._publish_allowed())
        mapping.clear()
        self.assertFalse(duty._onboarding._publish_allowed(), "闸门读的不是同一个对象引用")


class OnboardingFailedWiringTests(unittest.TestCase):
    """管理员送达（Issue #280 §7.3 步 1）：`_build_onboarding_duty` 收到的
    ``onboarding_failed`` 必须原样交给 `AutoOnboardingRunner`，不是就地丢弃或
    替换成别的东西。"""

    def test_defaults_to_none_when_not_wired(self) -> None:
        """调用方（``build_loop``）没有装配告警职责时保持 ``None``——「已转交管理员
        处理」这句话此前就是这个默认值，行为不变。"""

        duty, _ = build(WIRED_ENV, token=lambda: "u-token")

        self.assertIsNone(duty._onboarding._onboarding_failed)

    def test_reaches_the_runner_when_provided(self) -> None:
        calls: list[tuple[str, str]] = []

        duty, _ = build(
            WIRED_ENV,
            token=lambda: "u-token",
            onboarding_failed=lambda reason, trace_id: calls.append((reason, trace_id)),
        )

        assert duty._onboarding._onboarding_failed is not None
        duty._onboarding._onboarding_failed("directory_unavailable", "trc_1")
        self.assertEqual(calls, [("directory_unavailable", "trc_1")])


class ClaimCapacityTests(unittest.TestCase):
    """装配断言 4：认领量必须被执行器剩余容量压住。"""

    def test_an_unbound_claim_limit_fails_the_assembly(self) -> None:
        executor = OnboardingExecutor(workers=2)
        duty = OnboardingReconciler(
            store=object(), onboarding=object(), audit=RecordingAudit()
        )
        with self.assertRaises(RuntimeError):
            assert_claim_limit_follows_capacity(duty, executor)

    def test_binding_a_different_executor_fails_the_assembly(self) -> None:
        mine = OnboardingExecutor(workers=2)
        other = OnboardingExecutor(workers=2)
        duty = OnboardingReconciler(
            store=object(),
            onboarding=object(),
            audit=RecordingAudit(),
            capacity=other.free_slots,
        )
        with self.assertRaises(RuntimeError):
            assert_claim_limit_follows_capacity(duty, mine)

    def test_the_correct_binding_passes(self) -> None:
        executor = OnboardingExecutor(workers=2)
        duty = OnboardingReconciler(
            store=object(),
            onboarding=object(),
            audit=RecordingAudit(),
            capacity=executor.free_slots,
        )
        assert_claim_limit_follows_capacity(duty, executor)

    def test_free_slots_shrinks_as_the_queue_fills(self) -> None:
        executor = OnboardingExecutor(workers=2, backlog=3)

        self.assertEqual(executor.free_slots(), 3)
        executor.submit(lambda: None)
        self.assertEqual(executor.free_slots(), 2)
        executor.submit(lambda: None)
        executor.submit(lambda: None)
        self.assertEqual(executor.free_slots(), 0)
        self.assertFalse(executor.submit(lambda: None))

    def test_a_stopping_executor_reports_no_free_slots(self) -> None:
        stopping = {"value": False}
        executor = OnboardingExecutor(workers=2, should_stop=lambda: stopping["value"])

        self.assertGreater(executor.free_slots(), 0)
        stopping["value"] = True
        self.assertEqual(executor.free_slots(), 0)
        self.assertFalse(executor.submit(lambda: None))


class DispatchWindowTests(unittest.TestCase):
    """搬迁之后认领窗口只剩一个用途：让 gateway 的第一条提示先落地。"""

    def test_the_window_is_seconds_not_the_old_half_hour(self) -> None:
        # 旧窗口是三十分钟，为的是不把「正在同步的那条」判成孤儿。搬迁之后认领即记账，
        # 正在跑的那条压根不在候选里，因此窗口不再需要覆盖十五分钟的同步预算。
        self.assertLess(DISPATCH_AFTER, timedelta(minutes=1))
        self.assertGreater(DISPATCH_AFTER, timedelta(0))


class HandoffAssertionTests(unittest.TestCase):
    """Epic C 交办的三条装配断言。"""

    def test_a_probe_that_never_returns_is_cut_off_at_the_execution_level(self) -> None:
        """①执行级硬截止：就绪状态机管得住「返回得太晚」，管不住「永远不返回」。"""

        started = threading.Event()
        release = threading.Event()

        class HangingProbe:
            timeout_seconds = 1

            def list_metrics(self, *, user_id: str) -> int:
                started.set()
                release.wait(timeout=10)
                return 3

        guarded = HardDeadlineProbe(probe=HangingProbe(), timeout_seconds=0.2)
        try:
            with self.assertRaises(McpProbeError) as caught:
                guarded.list_metrics(user_id="usr_1")
            self.assertTrue(started.is_set())
            self.assertEqual(caught.exception.code, "probe_hard_deadline_exceeded")
            self.assertFalse(caught.exception.denied, "硬截止必须落技术失败，不是「MCP 拒绝了」")
        finally:
            release.set()

    def test_a_normal_probe_passes_through_untouched(self) -> None:
        class Probe:
            timeout_seconds = 20

            def list_metrics(self, *, user_id: str) -> int:
                return 5

        guarded = HardDeadlineProbe(probe=Probe(), timeout_seconds=5)
        self.assertEqual(guarded.list_metrics(user_id="usr_1"), 5)
        self.assertEqual(guarded.timeout_seconds, 20, "传输超时必须原样透出，供断言②比对")

    def test_a_probe_error_is_re_raised_unchanged(self) -> None:
        class Probe:
            timeout_seconds = 20

            def list_metrics(self, *, user_id: str) -> int:
                raise McpProbeError("denied_by_mcp", denied=True)

        guarded = HardDeadlineProbe(probe=Probe(), timeout_seconds=5)
        with self.assertRaises(McpProbeError) as caught:
            guarded.list_metrics(user_id="usr_1")
        self.assertTrue(caught.exception.denied, "看门狗不得把明确拒绝改写成技术失败")

    def test_mismatched_probe_timeouts_fail_the_assembly(self) -> None:
        """②传输超时必须与就绪节奏的单次超时逐值相等。"""

        class Probe:
            timeout_seconds = 30

        with self.assertRaises(RuntimeError):
            assert_probe_timeouts_agree(probe=Probe(), schedule=ReadinessSchedule())

    def test_matching_probe_timeouts_pass(self) -> None:
        class Probe:
            timeout_seconds = 20

        assert_probe_timeouts_agree(probe=Probe(), schedule=ReadinessSchedule())

    def test_the_schedule_itself_refuses_a_timeout_longer_than_the_interval(self) -> None:
        with self.assertRaises(ValueError):
            ReadinessSchedule(interval_seconds=10, budget_seconds=100, probe_timeout_seconds=30)

    def test_the_injected_clock_never_goes_backwards(self) -> None:
        """③注入单调时钟：墙钟回拨会让一次已经超窗的成功被算成有效。"""

        clock = monotonic_utc_clock()
        first = clock()
        second = clock()
        self.assertIsNotNone(first.tzinfo)
        self.assertGreaterEqual(second, first)
        self.assertLess(abs(second - datetime.now(timezone.utc)), timedelta(seconds=5))

    def test_the_watchdog_margin_is_above_the_transport_timeout(self) -> None:
        """看门狗只该在传输层**根本不遵守**超时时动手，不该抢在它前面。"""

        self.assertGreater(PROBE_WATCHDOG_MARGIN_SECONDS, 0)


class StalledLeaseAssertionTests(unittest.TestCase):
    """装配断言 5（本轮新增，Issue #282）：停摆租约必须严格长于链预算。"""

    def test_the_default_lease_passes_with_the_default_schedule(self) -> None:
        assert_stalled_lease_exceeds_chain_budget(
            lease_seconds=DEFAULT_STALLED_LEASE_SECONDS,
            publish_wait_seconds=120.0,
            schedule=ReadinessSchedule(),
        )

    def test_a_lease_shorter_than_the_chain_budget_fails_the_assembly(self) -> None:
        """**否定断言**：以后有人把就绪预算调大而没有同步调整租约，装配必须炸——
        不成立时扫描会把一条正在正常跑的开通判成僵尸。"""

        with self.assertRaises(RuntimeError):
            assert_stalled_lease_exceeds_chain_budget(
                lease_seconds=DEFAULT_STALLED_LEASE_SECONDS,
                publish_wait_seconds=120.0,
                # 把就绪预算调到比默认值大得多（例如从十五分钟调到一小时），
                # 使链预算超过 45 分钟的租约——挡住的正是"以后有人调大预算却没有
                # 碰这个模块一个字"这条时间炸弹。
                schedule=ReadinessSchedule(
                    interval_seconds=180, budget_seconds=3600, probe_timeout_seconds=20
                ),
            )

    def test_a_lease_equal_to_the_budget_is_refused(self) -> None:
        """租约必须**严格**长于链预算，相等也不够——留不出任何余量。"""

        with self.assertRaises(RuntimeError):
            assert_stalled_lease_exceeds_chain_budget(
                lease_seconds=125.0,
                publish_wait_seconds=100.0,
                schedule=_FakeSchedule(budget_seconds=20, probe_timeout_seconds=0),
            )


class _FakeSchedule:
    def __init__(self, *, budget_seconds: float, probe_timeout_seconds: float) -> None:
        self.budget_seconds = budget_seconds
        self.probe_timeout_seconds = probe_timeout_seconds


class StalledProvisioningAssemblyTests(unittest.TestCase):
    """`_build_stalled_provisioning_duty`：总能注册，不需要任何可选前置。"""

    def test_a_minimally_configured_process_still_registers_the_duty(self) -> None:
        audit = RecordingAudit()

        duty = _build_stalled_provisioning_duty(
            SchedulerConfig.from_env(BASE_ENV), stop=threading.Event(), audit=audit
        )

        self.assertEqual(duty.name, "开通中途停摆收口")
        self.assertTrue(duty.notifier_wired, "飞书应用凭据是必填配置，通知出口总能建出来")
        self.assertEqual(audit.records, [], "装配阶段没有任何前置缺项，不该留下任何审计")
        self.assertIsNone(duty._alert, "调用方没有装配告警职责时保持 None，行为不变")

    def test_the_alert_callback_reaches_the_duty_when_provided(self) -> None:
        """Issue #280 §7.3 步 2：`_build_stalled_provisioning_duty` 收到的 ``alert``
        必须原样交给 `StalledProvisioningDuty`。"""

        calls: list[int] = []

        duty = _build_stalled_provisioning_duty(
            SchedulerConfig.from_env(BASE_ENV),
            stop=threading.Event(),
            audit=RecordingAudit(),
            alert=calls.append,
        )

        assert duty._alert is not None
        duty._alert(2)
        self.assertEqual(calls, [2])


class ExecutorTests(unittest.TestCase):
    def test_zero_workers_is_refused(self) -> None:
        for value in (0, -1, True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    OnboardingExecutor(workers=value)  # type: ignore[arg-type]

    def test_a_task_failure_does_not_kill_the_worker_thread(self) -> None:
        executor = OnboardingExecutor(workers=1, backlog=4)
        executor.start()
        self.addCleanup(executor.stop)
        done = threading.Event()

        self.assertTrue(executor.submit(lambda: (_ for _ in ()).throw(RuntimeError("boom"))))
        self.assertTrue(executor.submit(done.set))
        self.assertTrue(done.wait(timeout=5), "一条链的失败不得带走执行线程")

    def test_work_actually_runs_on_another_thread(self) -> None:
        executor = OnboardingExecutor(workers=1, backlog=4)
        executor.start()
        self.addCleanup(executor.stop)
        seen: list[int] = []
        done = threading.Event()

        def task() -> None:
            seen.append(threading.get_ident())
            done.set()

        executor.submit(task)
        self.assertTrue(done.wait(timeout=5))
        self.assertNotEqual(seen[0], threading.get_ident())

    def test_an_accepted_task_is_never_stranded_behind_the_sentinels(self) -> None:
        """确定性竞态：submit 检查停机为假 → stop() 置位并放哨兵 → 工作线程取走哨兵全部
        退出 → submit 恢复并入队成功 → **已经没有人会执行它**，那条链既不通知也不释放认领。

        用另一条线程在「submit 已进入临界区」的那一刻调 ``stop()`` 来复现。入队与置位共用
        同一把闸之后，那条线程只能等 submit 放手，于是哨兵必然排在**我们这条任务之后**，
        任务照常被执行。断言的是不变量本身：**被受理的任务一定会跑**。
        """

        executor = OnboardingExecutor(workers=1, backlog=8)
        task = object()
        original_put = executor._queue.put_nowait

        raced = threading.Event()

        def racing_put(item):  # type: ignore[no-untyped-def]
            # 只在**我们这一次** submit 时插入竞态；`stop()` 自己放哨兵时也会走到这里，
            # 不设这道闸就会递归地再起一条 stopper 线程，测出来的东西就不是那个交错了。
            if not raced.is_set():
                raced.set()
                # 在「检查停机之后、入队之前」让另一条线程去调 stop()。有闸时它拿不到锁、
                # join 超时；没有闸时它会抢先把哨兵排进去。
                thread = threading.Thread(target=executor.stop, daemon=True)
                thread.start()
                thread.join(timeout=0.3)
                self.addCleanup(thread.join, 5)
            return original_put(item)

        executor._queue.put_nowait = racing_put  # type: ignore[assignment]
        accepted = executor.submit(task)  # type: ignore[arg-type]
        executor._queue.put_nowait = original_put  # type: ignore[assignment]

        self.assertTrue(accepted, "检查停机时还没停机，这一次必须被受理")
        queued = list(executor._queue.queue)
        self.assertIs(
            queued[0],
            task,
            "被受理的任务必须排在任何哨兵之前，否则工作线程取到哨兵就退出，它永远不会被执行",
        )

    def test_a_stopped_executor_refuses_even_when_the_queue_has_room(self) -> None:
        executor = OnboardingExecutor(workers=2, backlog=8)
        executor.stop()

        self.assertEqual(executor.free_slots(), 0)
        self.assertFalse(executor.submit(lambda: None))

    def test_stopping_does_not_discard_already_queued_work(self) -> None:
        """已排队的任务对应的事件**已经被记账**，丢掉就是永久烧掉。

        它们照常出队，各自在第一步看到停机标志、中止并把认领放回去。
        """

        executor = OnboardingExecutor(workers=1, backlog=4)
        ran = threading.Event()
        executor.submit(ran.set)
        executor.stop()
        executor.start()
        self.addCleanup(executor.join, 2.0)

        self.assertTrue(ran.wait(timeout=5), "停机不得丢弃已排队的任务")


class StopSentinelRaceTests(unittest.TestCase):
    """F2（外部集成面审查，应修）：``stop()`` 在队列满时哨兵全部丢失、``_loop`` 有
    check-then-get 竞态导致工作线程无超时地永久卡死。

    编排者核实后的准确形状（比审查描述的窄）：不是"必然阻塞"，而是一个
    check-then-get 竞态——线程检查队列时还没空于是继续循环去 ``get()``，而在检查与
    ``get()`` 之间最后一条任务被别的线程抢走，这条线程就永久卡在无超时的
    ``queue.get()`` 上（哨兵已在 ``stop()`` 时被丢弃，不会再补）。**不丢用户事件**
    （卡住的线程手里没有任务），真实代价是停机会耗尽整个停机预算。
    """

    def test_stop_silently_drops_sentinels_when_the_queue_is_completely_full(self) -> None:
        """"队列满"：``stop()`` 尝试放的每一个哨兵都因为 ``queue.Full`` 被丢弃，
        且不得因此清空或顶掉已经排队的任务（``stop()`` 的既有产品语义）。"""

        executor = OnboardingExecutor(workers=2, backlog=2)
        placeholder_a, placeholder_b = object(), object()
        executor._queue.put_nowait(placeholder_a)
        executor._queue.put_nowait(placeholder_b)
        self.assertEqual(executor._queue.qsize(), 2, "队列此刻已经满了")

        executor.stop()  # 不应该抛异常

        self.assertTrue(executor._stopping.is_set())
        self.assertEqual(
            list(executor._queue.queue),
            [placeholder_a, placeholder_b],
            "队列已满时两次哨兵投递都必须静默失败，已排队的任务原样保留、不被顶掉",
        )

    def test_stop_fills_only_the_available_slots_when_the_queue_is_partially_full(
        self,
    ) -> None:
        """"只剩部分槽位"：三条线程只有一个空位，``stop()`` 只能放进恰好一个哨兵，
        另外两次因队列满被丢弃；已排队的两个任务同样必须原样保留。"""

        executor = OnboardingExecutor(workers=3, backlog=3)
        placeholder_a, placeholder_b = object(), object()
        executor._queue.put_nowait(placeholder_a)
        executor._queue.put_nowait(placeholder_b)
        self.assertEqual(executor._queue.qsize(), 2, "三格队列只占了两格，留一个空位")

        executor.stop()

        remaining = list(executor._queue.queue)
        self.assertEqual(executor._queue.qsize(), 3, "唯一的空位应该被恰好一个哨兵占掉")
        self.assertEqual(remaining[:2], [placeholder_a, placeholder_b], "原有任务顺序不变")
        self.assertIsNone(remaining[2], "剩下那个位置放进去的是哨兵")

    def test_a_check_then_get_race_does_not_strand_a_worker_forever(self) -> None:
        """精确复现审查描述的竞态：线程在"排空判定"和真正调用 ``get()`` 之间，
        队列其实已经空了（模拟哨兵因队列满被丢弃、也没有别的任务补上）。

        用故意撒谎一次的 ``queue.empty()`` 制造这个窗口：真实队列已经空了，但
        ``empty()`` 仍然汇报一次"非空"，迫使线程按旧路径再走一次 ``get()``。

        **变异验红**：把 :meth:`OnboardingExecutor._loop` 里 ``self._queue.get(
        timeout=self._stop_poll_seconds)`` 改回没有超时的 ``self._queue.get()``，
        本用例会在 ``thread.join(timeout=5)`` 处观察到线程仍然存活而失败——
        实测输出见收口回报。
        """

        executor = OnboardingExecutor(workers=1, backlog=4, stop_poll_seconds=0.05)
        ran = threading.Event()
        executor.submit(ran.set)

        real_empty = executor._queue.empty
        lied_once = threading.Event()

        def lying_empty() -> bool:
            # 只撒谎一次：模拟"检查那一刻队列被判定非空"，即使真实队列这时已经空了
            # （哨兵已经因为队列满被丢弃，此后也不会再有任何东西入队）。
            if not lied_once.is_set():
                lied_once.set()
                return False
            return real_empty()

        executor._queue.empty = lying_empty  # type: ignore[assignment]
        executor._stopping.set()  # 模拟 stop() 已经置位、但哨兵因为队列满没能投进去
        executor.start()
        thread = executor._threads[0]
        self.addCleanup(thread.join, 5.0)

        self.assertTrue(ran.wait(timeout=5), "真实任务必须先正常执行")
        thread.join(timeout=5.0)
        self.assertFalse(
            thread.is_alive(),
            "check-then-get 竞态窗口触发之后，线程必须在有限时间内自行退出，"
            "不能永久卡在没有超时的 get() 上",
        )

    def test_a_full_queue_at_stop_time_still_lets_every_worker_exit_and_drains_queued_work(
        self,
    ) -> None:
        """端到端场景：真实并发下，"队列满"时停机——哨兵全部因 ``queue.Full`` 丢失，
        工作线程必须靠自己的超时轮询发现停机，而不是永久卡在 ``get()`` 上；同时已经
        排队的业务任务一条都不能丢（``stop()`` 的既有产品语义）。
        """

        workers = 3
        executor = OnboardingExecutor(workers=workers, backlog=workers, stop_poll_seconds=0.05)
        executor.start()
        self.addCleanup(executor.join, 5.0)

        gate = threading.Event()
        entered = threading.Barrier(workers + 1, timeout=5)

        def block() -> None:
            entered.wait(timeout=5)
            gate.wait(timeout=5)

        for _ in range(workers):
            self.assertTrue(executor.submit(block))
        entered.wait(timeout=5)  # 三条线程都已经取到各自的阻塞任务、真正在跑了

        ran: list[int] = []
        lock = threading.Lock()

        def record(index: int) -> None:
            with lock:
                ran.append(index)

        for index in range(workers):
            self.assertTrue(executor.submit(lambda i=index: record(i)), "队列此刻必须还有空位")
        self.assertEqual(executor._queue.qsize(), workers, "三条业务任务把队列灌满")

        executor.stop()  # 三次 put_nowait(None) 全部因为 queue.Full 被静默丢弃

        gate.set()  # 放行三条卡住的线程，让它们回去取排队的业务任务
        executor.join(5.0)

        self.assertFalse(executor.alive, "工作线程必须在有限时间内自行退出")
        self.assertEqual(sorted(ran), list(range(workers)), "已排队的业务任务一条都不能丢")


@unittest.skipUnless(
    importlib.util.find_spec("psycopg") and importlib.util.find_spec("cryptography"),
    "跳过：build_loop 会真的构造凭据保管与清理适配器，需要 psycopg 与 cryptography",
)
class InvariantBuildLoopTests(unittest.TestCase):
    """F1：不只在 ``_build_onboarding_duty`` 单元级校验，也在 ``build_loop`` 真实装配
    出的职责列表上核实这条不变量——证明 `build_loop` 把它刚刚构造出的
    ``permission_publish`` 对象原样交给了 `_build_onboarding_duty`，而不是重新推导
    出一个「看起来应该差不多」的判断。

    今天的生产配置下，``permission_publish`` 与 ``onboarding`` 恰好共用同一把 MCP
    令牌主密钥前置，靠环境变量本身很难只让前者变 ``None`` 而后者的其它前置仍然齐全
    ——这正是外部审查警告的「巧合」。这里改用 ``unittest.mock.patch`` 直接让
    ``_build_permission_publish_duty`` 返回 ``None``，把两者的前置解耦开来验证，
    不依赖那个巧合。
    """

    def test_onboarding_is_absent_from_the_assembled_loop_when_permission_publish_is_none(
        self,
    ) -> None:
        import tempfile
        from pathlib import Path
        from unittest import mock

        from cryptography.fernet import Fernet

        from lingxi.apps.scheduler import build_loop

        credential_dir = tempfile.TemporaryDirectory()
        self.addCleanup(credential_dir.cleanup)
        user_env_dir = tempfile.TemporaryDirectory()
        self.addCleanup(user_env_dir.cleanup)

        env = {
            **WIRED_ENV,
            "LINGXI_DELEGATED_CREDENTIAL_KEY": Fernet.generate_key().decode(),
            "LINGXI_DELEGATED_CREDENTIAL_PATH": str(Path(credential_dir.name) / "delegated.enc"),
            "LINGXI_USER_ENV_ROOT": user_env_dir.name,
        }
        config = SchedulerConfig.from_env(env)
        audit = RecordingAudit()

        with mock.patch(
            "lingxi.apps.scheduler.assembly._build_permission_publish_duty",
            return_value=None,
        ):
            loop = build_loop(
                config,
                # 在职状态令牌供给非 None：不能让开通因为**自己**的另一条前置缺失
                # 而不注册，否则测不出这条不变量单独起作用。
                roster_access_token=lambda: "employment-token",
                audit=audit,
            )
        self.addCleanup(loop.request_stop)

        names = [duty.name for duty in loop.duties]
        self.assertNotIn("权限发布与就绪确认", names, "本用例把 permission_publish 强制置空")
        self.assertNotIn(
            "未开通首聊交接对账",
            names,
            "permission_publish 未装配时，开通编排不得出现在真实装配出的职责列表里",
        )
        self.assertIn(
            ("onboarding.duty_not_registered", {"reason": "permission_publish_not_assembled"}),
            audit.records,
        )

    def test_onboarding_is_absent_when_only_the_readiness_side_is_wired(self) -> None:
        """**冻结候选审查 2026-08-21 F1 在真实装配上的锚点**：不打桩、不 patch，直接用
        「缺权限表 Base 坐标」这一组真实环境变量跑 ``build_loop``。

        这正是产品负责人 2026-08-21 第二次真实开通失败时生产的配置形状：MCP 那一组配齐
        （所以就绪面装得起来、``PermissionPublishDuty`` 照常注册），但
        ``LINGXI_PERMISSION_BITABLE_APP_TOKEN``/``TABLE_ID`` 没配（所以发布面
        ``executor=None``）。职责列表里因此**有**「权限发布与就绪确认」、但**不能有**
        开通编排。

        变异验红：把不变量改回 ``if permission_publish is None:`` 之后重跑本用例，
        「未开通首聊交接对账」会重新出现在职责列表里、``assertNotIn`` 失败。
        """

        import tempfile
        from pathlib import Path

        from cryptography.fernet import Fernet

        from lingxi.apps.scheduler import build_loop

        credential_dir = tempfile.TemporaryDirectory()
        self.addCleanup(credential_dir.cleanup)
        user_env_dir = tempfile.TemporaryDirectory()
        self.addCleanup(user_env_dir.cleanup)

        env = {
            **WIRED_ENV,
            "LINGXI_DELEGATED_CREDENTIAL_KEY": Fernet.generate_key().decode(),
            "LINGXI_DELEGATED_CREDENTIAL_PATH": str(Path(credential_dir.name) / "delegated.enc"),
            "LINGXI_USER_ENV_ROOT": user_env_dir.name,
        }
        audit = RecordingAudit()

        loop = build_loop(
            SchedulerConfig.from_env(env),
            roster_access_token=lambda: "employment-token",
            audit=audit,
        )
        self.addCleanup(loop.request_stop)

        names = [duty.name for duty in loop.duties]
        self.assertIn(
            "权限发布与就绪确认",
            names,
            "就绪面装得起来时职责本身照常注册——这正是 `is None` 判据看不见的那半个",
        )
        self.assertNotIn(
            "未开通首聊交接对账",
            names,
            "发布面未装配时，开通编排不得出现在真实装配出的职责列表里",
        )
        self.assertIn(
            ("onboarding.duty_not_registered", {"reason": "permission_publish_not_wired"}),
            audit.records,
        )

    def test_a_provided_alerting_duty_wires_both_onboarding_alert_callbacks(self) -> None:
        """Issue #280 §7.3：`build_loop(alerting_duty=...)` 必须把
        `onboarding_failed_callback()`/`onboarding_stalled_callback()` 接到刚构造出的
        开通编排与停摆收口职责上——不是构造了 `AlertingDuty` 却没人真的用它。"""

        import tempfile
        from pathlib import Path

        from cryptography.fernet import Fernet

        from lingxi.apps.scheduler import build_loop
        from lingxi.core.alerting import AlertDispatcher, AlertingDuty, AlertManager

        class _NoopSender:
            def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None:
                pass

        credential_dir = tempfile.TemporaryDirectory()
        self.addCleanup(credential_dir.cleanup)
        user_env_dir = tempfile.TemporaryDirectory()
        self.addCleanup(user_env_dir.cleanup)

        env = {
            **WIRED_ENV,
            "LINGXI_DELEGATED_CREDENTIAL_KEY": Fernet.generate_key().decode(),
            "LINGXI_DELEGATED_CREDENTIAL_PATH": str(Path(credential_dir.name) / "delegated.enc"),
            "LINGXI_USER_ENV_ROOT": user_env_dir.name,
            # 发布面也要接线，否则 permission_publish.publish_wired 为 False，
            # 装配不变量会让开通编排整体不注册——本用例要的是「onboarding 真的在
            # 职责列表里」，不是复现 `test_onboarding_is_absent_when_only_the_
            # readiness_side_is_wired` 那条反向用例。
            "LINGXI_PERMISSION_BITABLE_APP_TOKEN": "bascnFakeToken",
            "LINGXI_PERMISSION_BITABLE_TABLE_ID": "tblFakeTable",
        }
        alerting_duty = AlertingDuty(
            manager=AlertManager(),
            dispatcher=AlertDispatcher(sender=_NoopSender(), chat_id="oc_group"),
        )

        loop = build_loop(
            SchedulerConfig.from_env(env),
            roster_access_token=lambda: "employment-token",
            alerting_duty=alerting_duty,
        )
        self.addCleanup(loop.request_stop)

        onboarding = next(duty for duty in loop.duties if duty.name == "未开通首聊交接对账")
        stalled = next(duty for duty in loop.duties if duty.name == "开通中途停摆收口")

        self.assertIsNotNone(onboarding._onboarding._onboarding_failed)
        self.assertIsNotNone(stalled._alert)
        # 两个回调必须真的可用（不是占位符）：调用一次不应该抛异常。
        onboarding._onboarding._onboarding_failed("directory_unavailable", "trc_1")
        stalled._alert(1)

    def _wired_env(self) -> dict[str, str]:
        """独立审查 codex P1-4 两条用例共用的装配夹具：`WIRED_ENV` 加上让开通编排
        真的注册所需的凭据卷路径与权限表 Base 坐标（同
        ``test_a_provided_alerting_duty_wires_both_onboarding_alert_callbacks``）。"""

        import tempfile
        from pathlib import Path

        from cryptography.fernet import Fernet

        credential_dir = tempfile.TemporaryDirectory()
        self.addCleanup(credential_dir.cleanup)
        user_env_dir = tempfile.TemporaryDirectory()
        self.addCleanup(user_env_dir.cleanup)

        return {
            **WIRED_ENV,
            "LINGXI_DELEGATED_CREDENTIAL_KEY": Fernet.generate_key().decode(),
            "LINGXI_DELEGATED_CREDENTIAL_PATH": str(Path(credential_dir.name) / "delegated.enc"),
            "LINGXI_USER_ENV_ROOT": user_env_dir.name,
            "LINGXI_PERMISSION_BITABLE_APP_TOKEN": "bascnFakeToken",
            "LINGXI_PERMISSION_BITABLE_TABLE_ID": "tblFakeTable",
        }

    def test_onboarding_registered_without_an_admin_group_warns_once(self) -> None:
        """独立审查 codex P1-4：`LINGXI_ADMIN_GROUP_CHAT_ID` 未配置时，「已转交管理员
        处理」这句用户承诺背后的送达出口会退化为仅日志（`V-告警-08`）——开通职责一旦
        真的注册（会真的产生 `INTERNAL_ERROR`/`SYNC_TIMEOUT` 终态），装配期必须留一条
        响亮 WARNING，不能让这个组合悄悄运行。不失败关闭：`build_loop` 正常返回。"""

        from lingxi.apps.scheduler import build_loop

        env = self._wired_env()
        self.assertNotIn("LINGXI_ADMIN_GROUP_CHAT_ID", env)
        audit = RecordingAudit()

        with self.assertLogs("lingxi.apps.scheduler.assembly", level="WARNING") as captured:
            loop = build_loop(
                SchedulerConfig.from_env(env),
                roster_access_token=lambda: "employment-token",
                audit=audit,
            )
        self.addCleanup(loop.request_stop)

        names = [duty.name for duty in loop.duties]
        self.assertIn("未开通首聊交接对账", names, "本用例的前提是开通编排真的注册了")
        self.assertTrue(
            any("LINGXI_ADMIN_GROUP_CHAT_ID" in line for line in captured.output),
            captured.output,
        )
        self.assertIn(
            (
                "onboarding.admin_alert_channel_missing",
                {
                    "reason": "missing_environment_variable",
                    "variable": "LINGXI_ADMIN_GROUP_CHAT_ID",
                },
            ),
            audit.records,
        )

    def test_onboarding_registered_with_an_admin_group_does_not_warn(self) -> None:
        """否定断言的对照组：配了 `LINGXI_ADMIN_GROUP_CHAT_ID` 之后，同一条组合不再
        产生这条警告或审计——证明判据真的挂在配置上，不是恒定触发。"""

        from lingxi.apps.scheduler import build_loop

        env = {**self._wired_env(), "LINGXI_ADMIN_GROUP_CHAT_ID": "oc_admin_group"}
        audit = RecordingAudit()

        # 本用例的环境仍然没配花名册 Base 坐标，会各自留一条与本条无关的 WARNING
        # （见 ``_build_roster_audit_duty``/``_build_roster_snapshot_sync_duty``）；
        # 因此不能用「完全没有 WARNING」做判据，只断言**这一条**（提到
        # ``LINGXI_ADMIN_GROUP_CHAT_ID`` 的那条）没有出现。
        with self.assertLogs("lingxi.apps.scheduler.assembly", level="WARNING") as captured:
            loop = build_loop(
                SchedulerConfig.from_env(env),
                roster_access_token=lambda: "employment-token",
                audit=audit,
            )
        self.addCleanup(loop.request_stop)

        names = [duty.name for duty in loop.duties]
        self.assertIn("未开通首聊交接对账", names)
        self.assertFalse(
            any("LINGXI_ADMIN_GROUP_CHAT_ID" in line for line in captured.output),
            captured.output,
        )
        self.assertNotIn("onboarding.admin_alert_channel_missing", audit.actions())


class SchedulerConfigTests(unittest.TestCase):
    def test_defaults_keep_the_duty_optional(self) -> None:
        config = SchedulerConfig.from_env(BASE_ENV)
        self.assertIsNone(config.user_env_root)
        self.assertGreater(config.onboarding_workers, 0)

    def test_the_worker_count_is_bounded(self) -> None:
        with self.assertRaises(ValueError):
            SchedulerConfig.from_env({**BASE_ENV, "LINGXI_ONBOARDING_WORKERS": "0"})
        with self.assertRaises(ValueError):
            SchedulerConfig.from_env({**BASE_ENV, "LINGXI_ONBOARDING_WORKERS": "9999"})
        with self.assertRaises(ValueError):
            SchedulerConfig.from_env({**BASE_ENV, "LINGXI_ONBOARDING_WORKERS": "x"})

    def test_the_worker_count_is_configurable(self) -> None:
        config = SchedulerConfig.from_env({**BASE_ENV, "LINGXI_ONBOARDING_WORKERS": "16"})
        self.assertEqual(config.onboarding_workers, 16)

    def test_the_innertest_roster_defaults_to_an_empty_set(self) -> None:
        """未配置＝空集合＝闸对任何人全拒（Issue #302 S-N-01）；不是启动失败。"""

        config = SchedulerConfig.from_env(BASE_ENV)
        self.assertEqual(config.innertest_roster_open_ids, frozenset())

    def test_the_innertest_roster_parses_a_valid_list(self) -> None:
        # opus 批量审查 P2 修复：_looks_like_open_id 收紧为 ou_ 后接 20~64 位英文
        # 字母或数字，示例值必须真的满足这个形状，不能再用 "ou_a"/"ou_b" 这类过短
        # 的占位符（那类值现在会被判定为格式非法）。
        first = "ou_rostermembera00000000000"
        second = "ou_rostermemberb00000000000"
        config = SchedulerConfig.from_env(
            {**BASE_ENV, "LINGXI_INNERTEST_ROSTER_OPEN_IDS": f"{first},{second}, {second}"}
        )
        self.assertEqual(config.innertest_roster_open_ids, frozenset({first, second}))

    def test_an_invalid_innertest_roster_entry_fails_startup(self) -> None:
        """错配不是未配：整个 scheduler 启动失败，而不是悄悄退化成放行或不拦截。"""

        with self.assertRaises(ValueError):
            SchedulerConfig.from_env(
                {**BASE_ENV, "LINGXI_INNERTEST_ROSTER_OPEN_IDS": "ou_a,not-a-valid-open-id"}
            )

    def test_an_invalid_innertest_roster_error_does_not_echo_the_raw_value(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            SchedulerConfig.from_env(
                {**BASE_ENV, "LINGXI_INNERTEST_ROSTER_OPEN_IDS": "totally-not-an-open-id"}
            )
        self.assertNotIn("totally-not-an-open-id", str(ctx.exception))


class InnerTestRosterGateWiringTests(unittest.TestCase):
    """`_build_onboarding_duty` 把 `SchedulerConfig` 已解析校验过的名单集合装配成
    `AutoOnboardingRunner` 要的判定口，认领 `V-开通-21…23`。"""

    def test_a_listed_open_id_is_allowed_and_an_unlisted_one_is_not(self) -> None:
        # opus 批量审查 P2 修复：示例值必须满足收紧后的正则（ou_ 后接 20~64 位英文
        # 字母或数字），"ou_listed_member" 这类带下划线的占位符不再是合法配置。
        listed = "ou_listedmember00000000000"
        env = {**WIRED_ENV, "LINGXI_INNERTEST_ROSTER_OPEN_IDS": listed}
        duty, _ = build(env, token=lambda: "u-token")

        gate = duty._onboarding._innertest_roster_gate
        self.assertTrue(gate(listed))
        self.assertFalse(gate("ou_neverlistedanywhere00000"))

    def test_an_unconfigured_roster_rejects_everyone(self) -> None:
        """默认关闭＝全拒：`WIRED_ENV` 本身不含该变量，闸必须仍然拒绝一切输入。"""

        duty, _ = build(WIRED_ENV, token=lambda: "u-token")

        gate = duty._onboarding._innertest_roster_gate
        self.assertFalse(gate("ou_never_listed_anywhere"))
        self.assertFalse(gate("ou_listed_member"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
