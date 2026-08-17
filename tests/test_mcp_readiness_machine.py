"""MCP 就绪确认状态机的纯逻辑断言（Issue #156 / S-C-02）。

认领断言：`V-权限-04`（同步未确认前不给出就绪结论——本文件覆盖"就绪只有一条路成立"
这半边；把 ``provisioning_state`` 推到 ``active`` 属 Epic D 的开通编排）、`V-权限-05`
（十五分钟未确认转超时终态，**注入时钟**）、`V-权限-06`（就绪的唯一依据是以目标用户
身份对问数 MCP 真实执行一次 ``list_metrics`` 并看见指标；明确空结果、服务在线、他人可用
均不构成就绪；结论绑定 (用户, 权限版本)）。

`V-权限-06` 的措辞 2026-08-17 由「回读版本比对」改写为「探针法」：发布表侧没有版本字段
（G-155 终判，全表回源核对），回读比对不可行。改写依据与新判定见
``src/lingxi/core/permission/mcp_readiness.py`` 模块文档。

**一秒都不用真的等**：时钟与 ``sleep`` 都是注入的，用例里用假时钟把十五分钟走完。

否定面（合同的"不得 / 不允许"必须有对应否定测试，验证与门禁第八节）：

- 明确空结果**不算**就绪；读不懂的返回值**不算**就绪；
- 技术失败**不被**记成就绪，也**不被**记成"明确无权限"；
- "明确无权限"只从数据库侧得出，**一次探针都不发**，绝不从 MCP 的拒绝反推；
- 预算耗尽**之前**不判超时；
- 非法轮询配置（零、负数、超上限、非整数、间隔大于预算、次数爆炸）一律**拒绝**，
  没有"配错了就回退默认"的分支；
- 就绪结论绑定错误的用户或权限版本时**判否**。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from lingxi.core.permission.mcp_readiness import (
    CONTRACT_SCHEDULE,
    DEFAULT_BUDGET_SECONDS,
    DEFAULT_INTERVAL_SECONDS,
    MAX_ATTEMPTS,
    McpProbeError,
    McpReadinessConfirmation,
    ReadinessAttempt,
    ReadinessBinding,
    ReadinessOutcome,
    ReadinessSchedule,
    ReadinessSession,
    classify_probe,
    evaluate_permission_presence,
)

START = datetime(2026, 8, 17, 3, 0, tzinfo=timezone.utc)
USER = "usr_A"
VERSION = 7
PERMISSIONS = '{"1011":["日活","收入"]}'
WILDCARD = '{"*":["日活"]}'
EMPTY_LIST = '{"1011":[]}'
#: 验收窗口用的**最小合法配置**：两次尝试就走完全程，不依赖真等十五分钟。
FAST = ReadinessSchedule(interval_seconds=1, budget_seconds=1, probe_timeout_seconds=1)


class FakeClock:
    """假时钟：只在 ``sleep`` 与显式 ``advance`` 时前进。

    ``jitter`` 模拟真实 ``time.sleep`` 的行为——它**只保证不早于**请求的时长，实际总要
    多回来一点。这一点点误差累积起来正是"最后一次探针永远发不出去"的成因。
    """

    def __init__(self, start: datetime = START, *, jitter: float = 0.0) -> None:
        self.now = start
        self.slept: list[float] = []
        self._jitter = jitter

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds + self._jitter)

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeProbe:
    """按脚本逐次返回：整数=可见指标条数，异常实例=抛出它。"""

    def __init__(self, *script: object) -> None:
        self.script = list(script)
        self.calls: list[str] = []

    def list_metrics(self, *, user_id: str) -> int:
        self.calls.append(user_id)
        step = self.script.pop(0) if self.script else 0
        if isinstance(step, BaseException):
            raise step
        return step


class FakeStore:
    def __init__(self) -> None:
        self.records: list[ReadinessAttempt] = []
        self.fault: Exception | None = None

    def record_attempt(self, attempt: ReadinessAttempt) -> str:
        if self.fault is not None:
            raise self.fault
        self.records.append(attempt)
        return f"syn_{len(self.records)}"


class FakeAudit:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.entries.append((action, dict(fields)))


def _confirmation(
    *script: object,
    schedule: ReadinessSchedule = FAST,
    clock: FakeClock | None = None,
    store: FakeStore | None = None,
    audit: FakeAudit | None = None,
    probe: object | None = None,
) -> tuple[McpReadinessConfirmation, FakeProbe, FakeStore, FakeAudit, FakeClock]:
    the_clock = clock or FakeClock()
    the_probe = probe if probe is not None else FakeProbe(*script)
    the_store = store or FakeStore()
    the_audit = audit or FakeAudit()
    return (
        McpReadinessConfirmation(
            probe=the_probe,
            store=the_store,
            audit=the_audit,
            clock=the_clock,
            sleep=the_clock.sleep,
            schedule=schedule,
        ),
        the_probe,
        the_store,
        the_audit,
        the_clock,
    )


BINDING = ReadinessBinding(USER, VERSION)


class ScheduleTest(unittest.TestCase):
    """节奏与预算是**受控可配置的合法值**（Trace v12 夹具合同的硬要求）。"""

    def test_default_is_the_contract_rhythm(self) -> None:
        # 立即一次 + 每三分钟一次 + 十五分钟停止（产品合同「首次开通」第 8 条）。
        self.assertEqual(CONTRACT_SCHEDULE.interval_seconds, DEFAULT_INTERVAL_SECONDS)
        self.assertEqual(CONTRACT_SCHEDULE.budget_seconds, DEFAULT_BUDGET_SECONDS)
        self.assertEqual(DEFAULT_INTERVAL_SECONDS, 180)
        self.assertEqual(DEFAULT_BUDGET_SECONDS, 900)
        self.assertEqual(
            CONTRACT_SCHEDULE.attempt_offsets(), (0, 180, 360, 540, 720, 900)
        )
        self.assertEqual(CONTRACT_SCHEDULE.max_attempts, 6)

    def test_minimal_legal_configuration_shortens_the_window(self) -> None:
        self.assertEqual(FAST.attempt_offsets(), (0, 1))
        self.assertEqual(FAST.max_attempts, 2)

    def test_rejects_zero_negative_and_non_integer(self) -> None:
        for kwargs in (
            {"interval_seconds": 0},
            {"interval_seconds": -1},
            {"budget_seconds": 0},
            {"budget_seconds": -60},
            {"interval_seconds": True},
            {"budget_seconds": 900.0},
            {"interval_seconds": "180"},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    ReadinessSchedule(**kwargs)

    def test_rejects_values_above_the_ceiling(self) -> None:
        """**不回退默认**：配错的方向是"无上界地一直探下去"，必须拒绝。"""

        with self.assertRaises(ValueError):
            ReadinessSchedule(interval_seconds=901, budget_seconds=3600)
        with self.assertRaises(ValueError):
            ReadinessSchedule(interval_seconds=180, budget_seconds=3601)

    def test_rejects_interval_larger_than_budget(self) -> None:
        with self.assertRaises(ValueError):
            ReadinessSchedule(interval_seconds=600, budget_seconds=300)

    def test_rejects_configurations_that_would_flood_the_mcp(self) -> None:
        """合法却荒谬的组合（1 秒一次探一小时）要**拒绝**，不是悄悄截断。"""

        with self.assertRaises(ValueError) as caught:
            ReadinessSchedule(interval_seconds=1, budget_seconds=3600, probe_timeout_seconds=1)
        self.assertIn(str(MAX_ATTEMPTS), str(caught.exception))

    def test_confirmation_refuses_raw_numbers(self) -> None:
        clock = FakeClock()
        with self.assertRaises(TypeError):
            McpReadinessConfirmation(
                probe=FakeProbe(),
                store=FakeStore(),
                audit=FakeAudit(),
                clock=clock,
                sleep=clock.sleep,
                schedule=180,  # type: ignore[arg-type]
            )

    def test_probe_timeout_must_not_exceed_the_interval(self) -> None:
        """单次探针比一个间隔还久时，整轮确认会无上界地拖长——配置层就拒绝。"""

        with self.assertRaises(ValueError):
            ReadinessSchedule(interval_seconds=180, budget_seconds=900, probe_timeout_seconds=181)
        for value in (0, -1, True, 20.0):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    ReadinessSchedule(probe_timeout_seconds=value)

    def test_hard_deadline_is_budget_plus_one_probe_timeout(self) -> None:
        """结论最晚落地的时刻 = 预算 + 单次探针超时，这就是对上游承诺的收口上界。"""

        self.assertEqual(CONTRACT_SCHEDULE.hard_deadline, timedelta(seconds=900 + 20))
        self.assertEqual(FAST.hard_deadline, timedelta(seconds=2))


class PermissionPresenceTest(unittest.TestCase):
    """"明确无权限"只从**数据库侧**得出，且按回退制（不取并集）。"""

    def test_present_permissions_are_waitable(self) -> None:
        self.assertTrue(evaluate_permission_presence(PERMISSIONS))
        self.assertTrue(evaluate_permission_presence(PERMISSIONS, company_id="1011"))
        self.assertTrue(evaluate_permission_presence(WILDCARD, company_id="9999"))

    def test_empty_metric_list_means_no_permission(self) -> None:
        self.assertFalse(evaluate_permission_presence(EMPTY_LIST))
        self.assertFalse(evaluate_permission_presence(EMPTY_LIST, company_id="1011"))

    def test_company_key_present_does_not_fall_back_to_wildcard(self) -> None:
        # 回退制：显式键存在（哪怕是空列表）就到此为止，**不并入通配**。
        self.assertFalse(evaluate_permission_presence('{"1011":[],"*":["日活"]}', company_id="1011"))
        self.assertTrue(evaluate_permission_presence('{"1011":[],"*":["日活"]}', company_id="1012"))

    def test_unreadable_permissions_raise_instead_of_denying(self) -> None:
        """读不懂是本侧缺陷，不能折成一个用户可见的"你没有权限"终态。"""

        for text in ("", "不是 JSON", "[]"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    evaluate_permission_presence(text)


class ProbeClassificationTest(unittest.TestCase):
    def test_metrics_seen_is_ready(self) -> None:
        self.assertEqual(classify_probe(3), (ReadinessOutcome.READY, None))

    def test_empty_result_is_waiting_not_ready(self) -> None:
        """`V-权限-06`：明确空结果只证明这次查询没报错，不证明权限已生效。"""

        self.assertEqual(classify_probe(0), (ReadinessOutcome.WAITING, "empty_metrics"))

    def test_unreadable_count_is_technical_failure_not_ready(self) -> None:
        for value in (None, -1, "3", 3.0, True):
            with self.subTest(value=repr(value)):
                outcome, code = classify_probe(value)
                self.assertEqual(outcome, ReadinessOutcome.TECHNICAL_FAILURE)
                self.assertEqual(code, "invalid_metric_count")


class ConfirmationTest(unittest.TestCase):
    def test_ready_on_first_probe(self) -> None:
        confirmation, probe, store, audit, clock = _confirmation(2)
        session = confirmation.confirm(BINDING, permissions=PERMISSIONS)
        self.assertEqual(session.outcome, ReadinessOutcome.READY)
        self.assertTrue(session.ready)
        self.assertEqual(session.attempt_count, 1)
        self.assertEqual(probe.calls, [USER])
        # 立即一次：第一次探针不等待。
        self.assertEqual(clock.slept, [])
        self.assertEqual(store.records[0].metric_count, 2)
        self.assertEqual([action for action, _ in audit.entries], ["mcp_readiness.ready"])

    def test_no_permission_never_probes(self) -> None:
        """`V-权限-06` 否定面：无权限是数据库侧的判定，**一次外部调用都不发**。"""

        confirmation, probe, store, _, _ = _confirmation(5)
        session = confirmation.confirm(BINDING, permissions=EMPTY_LIST)
        self.assertEqual(session.outcome, ReadinessOutcome.NO_PERMISSION)
        self.assertEqual(probe.calls, [])
        self.assertEqual(len(store.records), 1)
        self.assertEqual(store.records[0].outcome, ReadinessOutcome.NO_PERMISSION)
        self.assertIsNone(store.records[0].metric_count)

    def test_denied_probe_waits_then_succeeds(self) -> None:
        confirmation, probe, store, _, clock = _confirmation(
            McpProbeError("http_403", denied=True), 4
        )
        session = confirmation.confirm(BINDING, permissions=PERMISSIONS)
        self.assertEqual(session.outcome, ReadinessOutcome.READY)
        self.assertEqual(session.attempt_count, 2)
        self.assertEqual(
            [item.outcome for item in store.records],
            [ReadinessOutcome.WAITING, ReadinessOutcome.READY],
        )
        # 第二次按节奏等了一个间隔。
        self.assertEqual(clock.slept, [1.0])

    def test_empty_result_does_not_end_the_session_as_ready(self) -> None:
        confirmation, _, store, _, _ = _confirmation(0, 0)
        session = confirmation.confirm(BINDING, permissions=PERMISSIONS)
        self.assertEqual(session.outcome, ReadinessOutcome.TIMED_OUT)
        self.assertFalse(session.ready)
        self.assertEqual(
            [item.outcome for item in store.records],
            [
                ReadinessOutcome.WAITING,
                ReadinessOutcome.WAITING,
                ReadinessOutcome.TIMED_OUT,
            ],
        )
        self.assertEqual(store.records[0].error_code, "empty_metrics")

    def test_technical_failure_is_neither_ready_nor_no_permission(self) -> None:
        confirmation, _, store, audit, _ = _confirmation(
            McpProbeError("transport_error"), McpProbeError("token_unavailable")
        )
        session = confirmation.confirm(BINDING, permissions=PERMISSIONS)
        self.assertEqual(session.outcome, ReadinessOutcome.TIMED_OUT)
        self.assertEqual(session.technical_failures, 2)
        recorded = [item.outcome for item in store.records]
        self.assertNotIn(ReadinessOutcome.READY, recorded)
        self.assertNotIn(ReadinessOutcome.NO_PERMISSION, recorded)
        self.assertEqual(
            [item.error_code for item in store.records[:2]],
            ["transport_error", "token_unavailable"],
        )
        self.assertIn("mcp_readiness.technical_failure", [action for action, _ in audit.entries])

    def test_unexpected_probe_exception_is_not_swallowed(self) -> None:
        """未预期的异常原样上抛：吞成"技术失败"会让真正的缺陷每轮安静地重试下去。"""

        confirmation, _, _, _, _ = _confirmation(RuntimeError("未预期缺陷"))
        with self.assertRaises(RuntimeError):
            confirmation.confirm(BINDING, permissions=PERMISSIONS)

    def test_every_attempt_is_recorded_independently(self) -> None:
        """架构设计 6.4 的 ``mcp_sync_check`` 意图：事后判断十五分钟是否现实。"""

        confirmation, _, store, _, _ = _confirmation(0, 0, schedule=CONTRACT_SCHEDULE)
        session = confirmation.confirm(BINDING, permissions=PERMISSIONS)
        # 六次探针 + 一条超时收口。
        self.assertEqual(len(store.records), CONTRACT_SCHEDULE.max_attempts + 1)
        self.assertEqual(session.attempt_count, CONTRACT_SCHEDULE.max_attempts + 1)
        self.assertEqual([item.attempt_no for item in store.records], list(range(1, 8)))

    def test_company_id_reaches_the_permission_judgement(self) -> None:
        """``company_id`` 必须真的接到回退制判定上（丢掉它会静默变成"任何公司都算"）。"""

        document = '{"1011":["日活"]}'
        confirmation, probe, _, _, _ = _confirmation(3)
        # 1012 在文档里没有键，也没有通配 → 明确无权限，一次探针都不发。
        session = confirmation.confirm(BINDING, permissions=document, company_id="1012")
        self.assertEqual(session.outcome, ReadinessOutcome.NO_PERMISSION)
        self.assertEqual(probe.calls, [])

        confirmation, probe, _, _, _ = _confirmation(3)
        session = confirmation.confirm(BINDING, permissions=document, company_id="1011")
        self.assertEqual(session.outcome, ReadinessOutcome.READY)
        self.assertEqual(probe.calls, [USER])

    def test_technical_failures_counts_only_technical_failures(self) -> None:
        """计数把 ``waiting`` 也算进去，会让"等不到"与"压根没探成"在告警里混成一件事。"""

        confirmation, _, _, _, _ = _confirmation(
            0, McpProbeError("transport_error"), schedule=ReadinessSchedule(
                interval_seconds=1, budget_seconds=2, probe_timeout_seconds=1
            )
        )
        session = confirmation.confirm(BINDING, permissions=PERMISSIONS)
        self.assertEqual(session.attempt_count, 4)  # 空结果 + 技术失败 + 空结果 + 超时
        self.assertEqual(session.technical_failures, 1)

    def test_store_failure_is_not_swallowed(self) -> None:
        store = FakeStore()
        store.fault = RuntimeError("库挂了")
        confirmation, _, _, audit, _ = _confirmation(3, store=store)
        with self.assertRaises(RuntimeError):
            confirmation.confirm(BINDING, permissions=PERMISSIONS)
        self.assertEqual(audit.entries[0][0], "mcp_readiness.record_failed")
        # 只记异常类型，不回显异常正文。
        self.assertEqual(audit.entries[0][1]["error"], "RuntimeError")


class BudgetTest(unittest.TestCase):
    """`V-权限-05`：十五分钟耗尽才判超时——**注入时钟**，不真的等。"""

    def test_full_contract_window_uses_six_probes_over_fifteen_minutes(self) -> None:
        clock = FakeClock()
        confirmation, probe, _, _, _ = _confirmation(0, 0, 0, 0, 0, 0, schedule=CONTRACT_SCHEDULE, clock=clock)
        session = confirmation.confirm(BINDING, permissions=PERMISSIONS)
        self.assertEqual(session.outcome, ReadinessOutcome.TIMED_OUT)
        self.assertEqual(len(probe.calls), 6)
        self.assertEqual(clock.slept, [180.0] * 5)
        self.assertEqual(clock.now - START, timedelta(seconds=900))

    def test_does_not_time_out_before_the_budget_is_spent(self) -> None:
        """预算没耗尽时，最后一次探针成功仍然是就绪，不是超时。"""

        clock = FakeClock()
        confirmation, probe, _, _, _ = _confirmation(
            0, 0, 0, 0, 0, 9, schedule=CONTRACT_SCHEDULE, clock=clock
        )
        session = confirmation.confirm(BINDING, permissions=PERMISSIONS)
        self.assertEqual(session.outcome, ReadinessOutcome.READY)
        self.assertEqual(len(probe.calls), 6)
        # 第六次落在第十五分钟整点，仍在窗口内（"最多等待十五分钟"）。
        self.assertEqual(clock.now - START, timedelta(seconds=900))

    def test_a_jittery_clock_still_sends_the_last_probe(self) -> None:
        """**真实时钟下必须仍是六次。**

        ``time.sleep(180)`` 只保证"不早于"，每次都会多回来一点点。上一版按
        ``now > deadline`` 二次否决尝试，累计抖动到第六次时 ``now`` 必然略过
        ``started + 900``，于是合同要求的最后一次探针永远发不出去——十五分钟的窗口
        实际只用了十二分钟（二级独立审查用 1 毫秒抖动实测到）。
        """

        clock = FakeClock(jitter=0.001)
        confirmation, probe, _, _, _ = _confirmation(
            0, 0, 0, 0, 0, 0, schedule=CONTRACT_SCHEDULE, clock=clock
        )
        session = confirmation.confirm(BINDING, permissions=PERMISSIONS)
        self.assertEqual(len(probe.calls), 6)
        self.assertEqual(session.outcome, ReadinessOutcome.TIMED_OUT)
        # 抖动确实发生了：结束时刻已经越过了纯预算，但探针一次没少。
        self.assertGreater(clock.now - START, timedelta(seconds=900))

    def test_a_success_at_the_boundary_still_counts(self) -> None:
        """第 900 秒整点那一次成功仍然算就绪（"最多等待十五分钟"含边界）。"""

        clock = FakeClock(jitter=0.001)
        confirmation, probe, _, _, _ = _confirmation(
            0, 0, 0, 0, 0, 7, schedule=CONTRACT_SCHEDULE, clock=clock
        )
        session = confirmation.confirm(BINDING, permissions=PERMISSIONS)
        self.assertEqual(session.outcome, ReadinessOutcome.READY)
        self.assertEqual(len(probe.calls), 6)

    def test_offsets_cover_899_900_and_stop_before_901(self) -> None:
        """边界枚举：899 秒的预算给 5 次，900 给 6 次，901 仍是 6 次（下一次要 1080）。"""

        self.assertEqual(
            ReadinessSchedule(budget_seconds=899).attempt_offsets(), (0, 180, 360, 540, 720)
        )
        self.assertEqual(
            ReadinessSchedule(budget_seconds=900).attempt_offsets(), (0, 180, 360, 540, 720, 900)
        )
        self.assertEqual(
            ReadinessSchedule(budget_seconds=901).attempt_offsets(), (0, 180, 360, 540, 720, 900)
        )

    def test_slow_probes_consume_the_budget(self) -> None:
        """探针本身耗时也吃预算：不然一次挂了 14 分钟的调用还能再等十五分钟。"""

        clock = FakeClock()

        class SlowProbe:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def list_metrics(self, *, user_id: str) -> int:
                self.calls.append(user_id)
                clock.advance(400)
                return 0

        probe = SlowProbe()
        confirmation, _, _, _, _ = _confirmation(
            schedule=CONTRACT_SCHEDULE, clock=clock, probe=probe
        )
        session = confirmation.confirm(BINDING, permissions=PERMISSIONS)
        self.assertEqual(session.outcome, ReadinessOutcome.TIMED_OUT)
        # 每次探针 400 秒（一个无视超时约定的注入探针）：第一次结束在 400s，第二次目标
        # 时刻 180s 已过、立即开始并结束在 800s，第三次目标 360s 已过、立即开始并结束在
        # 1200s——此时已越过硬上界 900+20=920s，第四次不再发起。**这条路径只在探针链
        # 真的失控时才走到**：正常与抖动时钟下硬上界永远不触发（见上面两条）。
        self.assertEqual(len(probe.calls), 3)

    def test_timeout_record_carries_no_probe_observation(self) -> None:
        confirmation, _, store, _, _ = _confirmation(0, 0)
        confirmation.confirm(BINDING, permissions=PERMISSIONS)
        timed_out = store.records[-1]
        self.assertEqual(timed_out.outcome, ReadinessOutcome.TIMED_OUT)
        self.assertIsNone(timed_out.metric_count)
        self.assertEqual(timed_out.error_code, "budget_exhausted")


class BindingTest(unittest.TestCase):
    """就绪结论**绑定 (用户, 权限版本)**：绑错了必须判否（变异锚点）。"""

    def _session(self) -> ReadinessSession:
        confirmation, _, _, _, _ = _confirmation(1)
        return confirmation.confirm(BINDING, permissions=PERMISSIONS)

    def test_applies_only_to_the_bound_pair(self) -> None:
        session = self._session()
        self.assertTrue(session.applies_to(USER, VERSION))
        self.assertFalse(session.applies_to("usr_B", VERSION))
        self.assertFalse(session.applies_to(USER, VERSION + 1))
        self.assertFalse(session.applies_to(USER, VERSION - 1))

    def test_non_ready_session_never_applies(self) -> None:
        confirmation, _, _, _, _ = _confirmation(0, 0)
        session = confirmation.confirm(BINDING, permissions=PERMISSIONS)
        self.assertFalse(session.applies_to(USER, VERSION))

    def test_binding_rejects_missing_user_or_version(self) -> None:
        for args in ((" ", 1), ("usr_A", 0), ("usr_A", -1), ("usr_A", True), ("usr_A", "1")):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    ReadinessBinding(*args)  # type: ignore[arg-type]

    def test_confirm_requires_a_binding_object(self) -> None:
        confirmation, _, _, _, _ = _confirmation(1)
        with self.assertRaises(TypeError):
            confirmation.confirm(USER, permissions=PERMISSIONS)  # type: ignore[arg-type]


class AttemptInvariantTest(unittest.TestCase):
    """记录对象自身的不变式：坏形状在构造时就炸，不等落库。"""

    def _attempt(self, **kwargs: object) -> ReadinessAttempt:
        base = {
            "binding": BINDING,
            "attempt_no": 1,
            "outcome": ReadinessOutcome.READY,
            "started_at": START,
            "finished_at": START,
            "metric_count": 1,
        }
        base.update(kwargs)
        return ReadinessAttempt(**base)  # type: ignore[arg-type]

    def test_ready_requires_a_positive_metric_count(self) -> None:
        for value in (None, 0, -1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self._attempt(metric_count=value)

    def test_ready_cannot_carry_an_error_code(self) -> None:
        with self.assertRaises(ValueError):
            self._attempt(error_code="empty_metrics")

    def test_non_probe_outcomes_cannot_carry_observations(self) -> None:
        for outcome in (ReadinessOutcome.NO_PERMISSION, ReadinessOutcome.TIMED_OUT):
            with self.subTest(outcome=outcome):
                with self.assertRaises(ValueError):
                    self._attempt(outcome=outcome, metric_count=3, error_code="x")

    def test_waiting_cannot_claim_it_saw_metrics(self) -> None:
        """一条"等待中却看见 5 条"的记录自相矛盾——看见了就该是就绪。"""

        with self.assertRaises(ValueError):
            self._attempt(outcome=ReadinessOutcome.WAITING, metric_count=5, error_code="x")
        # 合法的两种等待中：明确拒绝（无观察值）与空结果（恰为 0）。
        self._attempt(outcome=ReadinessOutcome.WAITING, metric_count=None, error_code="http_403")
        self._attempt(outcome=ReadinessOutcome.WAITING, metric_count=0, error_code="empty_metrics")

    def test_technical_failure_cannot_carry_observations(self) -> None:
        """探针没跑通，任何数字都是假的；负数还会撞上迁移 0065 的 CHECK。"""

        for value in (0, 3, -1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self._attempt(
                        outcome=ReadinessOutcome.TECHNICAL_FAILURE,
                        metric_count=value,
                        error_code="transport_error",
                    )

    def test_every_non_ready_outcome_requires_an_error_code(self) -> None:
        for outcome in (
            ReadinessOutcome.WAITING,
            ReadinessOutcome.TECHNICAL_FAILURE,
            ReadinessOutcome.NO_PERMISSION,
            ReadinessOutcome.TIMED_OUT,
        ):
            with self.subTest(outcome=outcome):
                with self.assertRaises(ValueError):
                    self._attempt(outcome=outcome, metric_count=None, error_code=None)

    def test_naive_timestamps_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._attempt(started_at=datetime(2026, 8, 17, 3, 0))

    def test_audit_facts_carry_no_person_data(self) -> None:
        facts = self._attempt().audit_facts()
        self.assertEqual(
            set(facts),
            {
                "outcome",
                "user_id",
                "permission_version",
                "attempt_no",
                "error_code",
                "metric_count",
                "elapsed_ms",
            },
        )

    def test_session_must_end_on_a_terminal_outcome(self) -> None:
        for outcome in (ReadinessOutcome.WAITING, ReadinessOutcome.TECHNICAL_FAILURE):
            with self.subTest(outcome=outcome):
                with self.assertRaises(ValueError):
                    ReadinessSession(binding=BINDING, outcome=outcome, attempts=(self._attempt(),))

    def test_session_requires_at_least_one_record(self) -> None:
        with self.assertRaises(ValueError):
            ReadinessSession(binding=BINDING, outcome=ReadinessOutcome.READY, attempts=())

    def test_session_cannot_borrow_another_users_success(self) -> None:
        """否定面：拿别人的 ready 记录 + 自己的 binding 拼不出一个 ``applies_to`` 为真的结论。

        ``applies_to`` 只核 binding、不核证据，因此证据与 binding 的一致性必须在构造时
        就成立（二级独立审查自设计的变异 S2）。
        """

        other = ReadinessAttempt(
            binding=ReadinessBinding("usr_B", VERSION),
            attempt_no=1,
            outcome=ReadinessOutcome.READY,
            started_at=START,
            finished_at=START,
            metric_count=9,
        )
        with self.assertRaises(ValueError):
            ReadinessSession(binding=BINDING, outcome=ReadinessOutcome.READY, attempts=(other,))
        # 版本对不上同样拒绝。
        stale = ReadinessAttempt(
            binding=ReadinessBinding(USER, VERSION + 1),
            attempt_no=1,
            outcome=ReadinessOutcome.READY,
            started_at=START,
            finished_at=START,
            metric_count=9,
        )
        with self.assertRaises(ValueError):
            ReadinessSession(binding=BINDING, outcome=ReadinessOutcome.READY, attempts=(stale,))

    def test_session_outcome_must_match_the_last_attempt(self) -> None:
        """一轮以超时收尾的确认不得对外宣称就绪。"""

        timed_out = ReadinessAttempt(
            binding=BINDING,
            attempt_no=1,
            outcome=ReadinessOutcome.TIMED_OUT,
            started_at=START,
            finished_at=START,
            error_code="budget_exhausted",
        )
        with self.assertRaises(ValueError):
            ReadinessSession(
                binding=BINDING, outcome=ReadinessOutcome.READY, attempts=(timed_out,)
            )


class ClockTest(unittest.TestCase):
    def test_naive_clock_is_rejected(self) -> None:
        clock = FakeClock()
        confirmation = McpReadinessConfirmation(
            probe=FakeProbe(1),
            store=FakeStore(),
            audit=FakeAudit(),
            clock=lambda: datetime(2026, 8, 17, 3, 0),
            sleep=clock.sleep,
            schedule=FAST,
        )
        with self.assertRaises(ValueError):
            confirmation.confirm(BINDING, permissions=PERMISSIONS)

    def test_non_callable_clock_is_rejected(self) -> None:
        clock = FakeClock()
        with self.assertRaises(TypeError):
            McpReadinessConfirmation(
                probe=FakeProbe(1),
                store=FakeStore(),
                audit=FakeAudit(),
                clock=START,  # type: ignore[arg-type]
                sleep=clock.sleep,
                schedule=FAST,
            )


class SleeperTest(unittest.TestCase):
    """``sleep`` **必填**：缺省会把十五分钟的等待压成瞬时，而一切看起来都正常。"""

    def test_sleep_is_required(self) -> None:
        clock = FakeClock()
        with self.assertRaises(TypeError):
            McpReadinessConfirmation(  # type: ignore[call-arg]
                probe=FakeProbe(0, 0),
                store=FakeStore(),
                audit=FakeAudit(),
                clock=clock,
                schedule=FAST,
            )

    def test_non_callable_sleeper_is_rejected(self) -> None:
        clock = FakeClock()
        for sleeper in (None, 0, 1.5, "sleep"):
            with self.subTest(sleeper=repr(sleeper)):
                with self.assertRaises(TypeError) as caught:
                    McpReadinessConfirmation(
                        probe=FakeProbe(0, 0),
                        store=FakeStore(),
                        audit=FakeAudit(),
                        clock=clock,
                        sleep=sleeper,  # type: ignore[arg-type]
                        schedule=FAST,
                    )
                self.assertIn("sleep", str(caught.exception))

    def test_a_real_sleeper_actually_spends_the_budget(self) -> None:
        """反面参照：注入了 sleeper 之后，整轮确认确实走过了完整预算。"""

        clock = FakeClock()
        confirmation, _, _, _, _ = _confirmation(0, 0, 0, 0, 0, 0, schedule=CONTRACT_SCHEDULE, clock=clock)
        confirmation.confirm(BINDING, permissions=PERMISSIONS)
        self.assertEqual(sum(clock.slept), 900.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
