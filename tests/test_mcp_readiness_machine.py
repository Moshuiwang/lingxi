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

S-C-03b 追加 **tick 驱动形态**（:class:`ReadinessTicker`）的断言，放在本文件而不是另开
一份：两种形态必须是**同一套语义**，把它们的用例摆在一起，任何一方悄悄分叉都会在同一个
文件里露出来。追加的否定面：

- 没到期时**零探针、零记录、零审计**；终态之后**永不再动**（这同时是"通知只发一次"的载体）；
- tick 形态**源码里没有任何等待**（构造函数不接受 ``sleep``），因此一轮调度不会被占住；
- 最后一次探针**成功就是成功**，不因为调度晚了一轮被丢掉；但**无视自身超时**的成功仍被
  降级为超时；
- 读不懂的历史结论**不**当成终态（方向选多探几次，不选提前收口）。
"""

from __future__ import annotations

import ast
import inspect
import textwrap
import unittest
from datetime import UTC, datetime, timedelta

from lingxi.core.permission.mcp_readiness import (
    CONTRACT_SCHEDULE,
    DEFAULT_BUDGET_SECONDS,
    DEFAULT_INTERVAL_SECONDS,
    MAX_ATTEMPTS,
    MAX_ERROR_CODE_LENGTH,
    McpProbeError,
    McpReadinessConfirmation,
    ReadinessAttempt,
    ReadinessBinding,
    ReadinessOutcome,
    ReadinessProgress,
    ReadinessRecoveryTicker,
    ReadinessSchedule,
    ReadinessSession,
    ReadinessTicker,
    classify_probe,
    evaluate_permission_presence,
    next_probe_due,
)

START = datetime(2026, 8, 17, 3, 0, tzinfo=UTC)
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

    def test_garbage_observations_survive_the_whole_chain(self) -> None:
        """**G7**：探针返回垃圾时，整条链要落成一次**可落库**的技术失败，而不是炸掉。

        逐条覆盖：负数（会撞上迁移 0065 的 ``metric_count >= 0``）、字符串、浮点、布尔、
        ``None``。每一次都必须是 ``technical_failure`` + 观察值为 ``None`` + 带错误码，
        因此 :class:`ReadinessAttempt` 与数据库 CHECK 两侧都收得下。
        """

        for junk in (-1, "3", 3.0, True, None, [], {"count": 1}):
            with self.subTest(junk=repr(junk)):
                confirmation, _, store, _, _ = _confirmation(junk, junk)
                session = confirmation.confirm(BINDING, permissions=PERMISSIONS)
                self.assertEqual(session.outcome, ReadinessOutcome.TIMED_OUT)
                probes = [
                    item
                    for item in store.records
                    if item.outcome is ReadinessOutcome.TECHNICAL_FAILURE
                ]
                self.assertEqual(len(probes), 2)
                for item in probes:
                    self.assertIsNone(item.metric_count)
                    self.assertEqual(item.error_code, "invalid_metric_count")
                self.assertEqual(session.technical_failures, 2)

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

    def test_a_success_returning_after_the_hard_deadline_is_not_ready(self) -> None:
        """**G3**：探针在承诺窗口之外才返回成功 → 不得判就绪，收口 ``timed_out``。

        发起前的检查管不住它（发起那一刻还没超）。最后一次探针可以在 ``deadline - 1s``
        发起、``deadline + 20s`` 才返回，而那时窗口早已关闭。这一条同时是唯一不依赖
        "传输层真的遵守了超时"的防线。
        """

        clock = FakeClock()

        class LateSuccess:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def list_metrics(self, *, user_id: str) -> int:
                self.calls.append(user_id)
                # 单次跑掉 30 秒：超过 20 秒的探针超时约定，也越过 900+20 的硬上界。
                clock.advance(1000)
                return 5

        probe = LateSuccess()
        confirmation, _, store, _, _ = _confirmation(
            schedule=CONTRACT_SCHEDULE, clock=clock, probe=probe
        )
        session = confirmation.confirm(BINDING, permissions=PERMISSIONS)
        self.assertEqual(session.outcome, ReadinessOutcome.TIMED_OUT)
        self.assertFalse(session.ready)
        self.assertFalse(session.applies_to(USER, VERSION))
        # 记录里**不留** ready 行：否则表里会出现"这一轮超时但有一条就绪"的矛盾事实。
        self.assertNotIn(ReadinessOutcome.READY, [item.outcome for item in store.records])
        self.assertEqual(store.records[-1].outcome, ReadinessOutcome.TIMED_OUT)
        self.assertEqual(store.records[-1].error_code, "success_after_deadline")
        self.assertIsNone(store.records[-1].metric_count)

    def test_a_success_inside_the_hard_deadline_still_counts(self) -> None:
        """边界内侧的对照：探针耗时把结束时刻推到预算之后、但仍在硬上界之内 → 就绪。"""

        clock = FakeClock()

        class SlowButInTime:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def list_metrics(self, *, user_id: str) -> int:
                self.calls.append(user_id)
                clock.advance(10 if len(self.calls) < 6 else 15)
                return 0 if len(self.calls) < 6 else 4

        probe = SlowButInTime()
        confirmation, _, _, _, _ = _confirmation(
            schedule=CONTRACT_SCHEDULE, clock=clock, probe=probe
        )
        session = confirmation.confirm(BINDING, permissions=PERMISSIONS)
        # 第六次在 900s 发起、915s 返回：越过预算（900）但仍在硬上界（920）之内。
        self.assertEqual(session.outcome, ReadinessOutcome.READY)
        self.assertEqual(clock.now - START, timedelta(seconds=915))

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

    def test_blank_error_codes_are_rejected(self) -> None:
        """**G5**：空串与纯空白等同于"没写"——它们满足非 ``None``，却什么都没说明。"""

        for blank in ("", "   ", "\t", 42, b"x"):
            with self.subTest(blank=repr(blank)):
                with self.assertRaises(ValueError):
                    self._attempt(
                        outcome=ReadinessOutcome.TECHNICAL_FAILURE,
                        metric_count=None,
                        error_code=blank,
                    )

    def test_over_long_error_codes_are_rejected_in_core(self) -> None:
        """**H2 的 core 侧**：超限值必须在这里就被拒，不能等到数据库 CHECK。

        等到数据库那一侧才失败，抛的是**记账失败**，会中断整轮确认——而这本该只是一次
        可记录的失败。上限与迁移 ``0065`` 的 CHECK 是同一个数。
        """

        self.assertEqual(MAX_ERROR_CODE_LENGTH, 200)
        self._attempt(
            outcome=ReadinessOutcome.TECHNICAL_FAILURE,
            metric_count=None,
            error_code="x" * MAX_ERROR_CODE_LENGTH,
        )
        with self.assertRaises(ValueError):
            self._attempt(
                outcome=ReadinessOutcome.TECHNICAL_FAILURE,
                metric_count=None,
                error_code="x" * (MAX_ERROR_CODE_LENGTH + 1),
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


def _code_without_docstrings(source: str) -> str:
    """去掉全部文档字符串与注释之后的正文（形状断言只该扫代码）。"""

    tree = ast.parse(textwrap.dedent(source))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            del body[0]
            if not body:
                body.append(ast.Pass())
    return ast.unparse(tree)


class _StoredCheck:
    """回读出来的一条判定行的最小形状（``adapters/postgres_mcp_token.StoredCheck``）。

    这里刻意只给 ``ReadinessProgress.from_checks`` 真正会读的两个属性：多给一个字段，
    用例就会开始依赖一份比 ``core`` 实际需要的更宽的契约。
    """

    def __init__(self, result: str, started_at: datetime) -> None:
        self.result = result
        self.started_at = started_at


def _ticker(
    *script: object,
    schedule: ReadinessSchedule = CONTRACT_SCHEDULE,
    clock: FakeClock | None = None,
) -> tuple[ReadinessTicker, FakeProbe, FakeStore, FakeAudit, FakeClock]:
    the_clock = clock or FakeClock()
    the_probe = FakeProbe(*script)
    the_store = FakeStore()
    the_audit = FakeAudit()
    return (
        ReadinessTicker(
            probe=the_probe,
            store=the_store,
            audit=the_audit,
            clock=the_clock,
            schedule=schedule,
        ),
        the_probe,
        the_store,
        the_audit,
        the_clock,
    )


class ReadinessProgressTest(unittest.TestCase):
    """进度从 ``mcp_sync_check`` 重建：不需要第二张表，也不需要进程内状态。"""

    def test_no_rows_means_nothing_has_been_tried(self) -> None:
        progress = ReadinessProgress.from_checks(())

        self.assertEqual(progress.attempt_count, 0)
        self.assertIsNone(progress.first_started_at)
        self.assertFalse(progress.terminal)

    def test_the_start_is_the_first_row_not_the_last(self) -> None:
        """节奏按**绝对时刻**算。取最后一行当起点，窗口会随每一次探针往后漂。"""

        progress = ReadinessProgress.from_checks(
            (
                _StoredCheck("waiting", START),
                _StoredCheck("waiting", START + timedelta(seconds=180)),
            )
        )

        self.assertEqual(progress.attempt_count, 2)
        self.assertEqual(progress.first_started_at, START)
        self.assertFalse(progress.terminal)

    def test_every_terminal_result_is_recognised(self) -> None:
        for result in ("ready", "no_permission", "timed_out"):
            with self.subTest(result):
                progress = ReadinessProgress.from_checks((_StoredCheck(result, START),))
                self.assertTrue(progress.terminal)

    def test_intermediate_results_are_not_terminal(self) -> None:
        for result in ("waiting", "technical_failure"):
            with self.subTest(result):
                progress = ReadinessProgress.from_checks((_StoredCheck(result, START),))
                self.assertFalse(progress.terminal)

    def test_an_unreadable_result_is_not_treated_as_terminal(self) -> None:
        """否定断言：读不懂的结论**不**当成终态。

        方向选"多探几次"，不选"提前宣告这条确认已经结束"——后者会静默丢掉一个人的就绪。
        """

        progress = ReadinessProgress.from_checks((_StoredCheck("something_new", START),))

        self.assertFalse(progress.terminal)

    def test_a_progress_without_a_start_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ReadinessProgress(attempt_count=1)

    def test_a_naive_start_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ReadinessProgress(attempt_count=1, first_started_at=datetime(2026, 8, 17, 3, 0))


class NextProbeDueTest(unittest.TestCase):
    """"下一次什么时候到期"是纯函数，计划表与阻塞形态**同一份**。"""

    def test_the_offsets_match_the_blocking_engine(self) -> None:
        for count, expected in enumerate(CONTRACT_SCHEDULE.attempt_offsets()):
            if count == 0:
                continue
            with self.subTest(count):
                due = next_probe_due(
                    CONTRACT_SCHEDULE,
                    ReadinessProgress(attempt_count=count, first_started_at=START),
                )
                self.assertEqual(due, START + timedelta(seconds=expected))

    def test_the_plan_table_is_the_only_cap(self) -> None:
        due = next_probe_due(
            CONTRACT_SCHEDULE,
            ReadinessProgress(
                attempt_count=len(CONTRACT_SCHEDULE.attempt_offsets()), first_started_at=START
            ),
        )

        self.assertIsNone(due, "计划表用完就不该再有下一次")

    def test_a_terminal_confirmation_has_no_next_probe(self) -> None:
        self.assertIsNone(
            next_probe_due(
                CONTRACT_SCHEDULE,
                ReadinessProgress(attempt_count=2, first_started_at=START, terminal=True),
            )
        )

    def test_the_first_probe_is_not_a_due_question(self) -> None:
        """第一次探针在"发布读回一致后立即"，没有到期判定可言。"""

        with self.assertRaises(ValueError):
            next_probe_due(CONTRACT_SCHEDULE, ReadinessProgress())


class ReadinessTickerTest(unittest.TestCase):
    """tick 形态：同一套语义，只换"等待"的实现。"""

    def test_the_first_probe_goes_out_immediately_after_publication(self) -> None:
        ticker, probe, store, _, clock = _ticker(4)

        attempt = ticker.advance(BINDING, permissions=PERMISSIONS, progress=ReadinessProgress())

        self.assertEqual(attempt.outcome, ReadinessOutcome.READY)
        self.assertEqual(probe.calls, [USER])
        self.assertEqual(clock.slept, [], "tick 形态一次都不 sleep")
        self.assertEqual(len(store.records), 1)

    def test_a_probe_that_is_not_due_yet_makes_no_external_call(self) -> None:
        """否定断言：**没到期就一次外部调用都不发**，也不落任何记录。"""

        ticker, probe, store, audit, clock = _ticker(4)
        clock.advance(179)

        attempt = ticker.advance(
            BINDING,
            permissions=PERMISSIONS,
            progress=ReadinessProgress(attempt_count=1, first_started_at=START),
        )

        self.assertIsNone(attempt)
        self.assertEqual(probe.calls, [])
        self.assertEqual(store.records, [])
        self.assertEqual(audit.entries, [])

    def test_a_due_probe_goes_out_on_this_tick(self) -> None:
        ticker, probe, _, _, clock = _ticker(0)
        clock.advance(180)

        attempt = ticker.advance(
            BINDING,
            permissions=PERMISSIONS,
            progress=ReadinessProgress(attempt_count=1, first_started_at=START),
        )

        self.assertEqual(attempt.outcome, ReadinessOutcome.WAITING)
        self.assertEqual(attempt.attempt_no, 2)
        self.assertEqual(probe.calls, [USER])

    def test_a_terminal_confirmation_is_never_touched_again(self) -> None:
        """否定断言：终态之后**零探针、零记录**——这同时是"通知只发一次"的载体。

        两种进度都要盖住：**还有计划次数没用完**（少了这一条会多探几次），以及
        **计划表已经用尽**（少了这一条会每轮补一条重复的超时记录，那正是"通知只发
        一次"失效的形状）。
        """

        for label, count in (("计划还没用完", 1), ("计划已经用尽", 6)):
            with self.subTest(label):
                ticker, probe, store, audit, clock = _ticker(4, schedule=CONTRACT_SCHEDULE)
                clock.advance(10_000)

                attempt = ticker.advance(
                    BINDING,
                    permissions=PERMISSIONS,
                    progress=ReadinessProgress(
                        attempt_count=count, first_started_at=START, terminal=True
                    ),
                )

                self.assertIsNone(attempt)
                self.assertEqual(probe.calls, [])
                self.assertEqual(store.records, [])
                self.assertEqual(audit.entries, [])

    def test_an_empty_permission_document_never_probes(self) -> None:
        """撤权那一路：``no_permission`` 是终态，**一次探针都不发**（与阻塞形态同一条）。"""

        ticker, probe, store, _, _ = _ticker(4)

        attempt = ticker.advance(BINDING, permissions="{}", progress=ReadinessProgress())

        self.assertEqual(attempt.outcome, ReadinessOutcome.NO_PERMISSION)
        self.assertEqual(attempt.error_code, "no_publishable_permission")
        self.assertTrue(attempt.terminal)
        self.assertEqual(probe.calls, [], "没有可等的东西就不发探针")
        self.assertEqual(len(store.records), 1)

    def test_an_empty_metric_list_is_not_ready(self) -> None:
        """否定断言：明确空结果**不算**就绪（与阻塞形态同一个分类函数）。"""

        ticker, _, _, _, _ = _ticker(0)

        attempt = ticker.advance(BINDING, permissions=PERMISSIONS, progress=ReadinessProgress())

        self.assertEqual(attempt.outcome, ReadinessOutcome.WAITING)
        self.assertEqual(attempt.error_code, "empty_metrics")

    def test_an_unreadable_response_is_a_technical_failure_not_ready(self) -> None:
        ticker, _, _, _, _ = _ticker("三条")

        attempt = ticker.advance(BINDING, permissions=PERMISSIONS, progress=ReadinessProgress())

        self.assertEqual(attempt.outcome, ReadinessOutcome.TECHNICAL_FAILURE)
        self.assertIsNone(attempt.metric_count)

    def test_a_denied_probe_is_waiting_not_a_technical_failure(self) -> None:
        ticker, _, _, _, _ = _ticker(McpProbeError("http_403", denied=True))

        attempt = ticker.advance(BINDING, permissions=PERMISSIONS, progress=ReadinessProgress())

        self.assertEqual(attempt.outcome, ReadinessOutcome.WAITING)

    def test_the_last_planned_probe_closes_the_window_in_the_same_tick(self) -> None:
        """预算耗尽当场收口，不拖到下一个 tick（与阻塞形态"循环走完就判超时"同一时刻）。"""

        ticker, probe, store, _, clock = _ticker(0, schedule=CONTRACT_SCHEDULE)
        clock.advance(900)

        attempt = ticker.advance(
            BINDING,
            permissions=PERMISSIONS,
            progress=ReadinessProgress(attempt_count=5, first_started_at=START),
        )

        self.assertEqual(attempt.outcome, ReadinessOutcome.TIMED_OUT)
        self.assertEqual(attempt.error_code, "budget_exhausted")
        self.assertEqual(len(probe.calls), 1, "最后一次探针照发")
        self.assertEqual(
            [record.outcome for record in store.records],
            [ReadinessOutcome.WAITING, ReadinessOutcome.TIMED_OUT],
        )

    def test_the_last_planned_probe_may_still_succeed(self) -> None:
        """否定断言：最后一次探针**成功就是成功**，不会被顺手降级成超时。"""

        ticker, _, store, _, clock = _ticker(6, schedule=CONTRACT_SCHEDULE)
        clock.advance(900)

        attempt = ticker.advance(
            BINDING,
            permissions=PERMISSIONS,
            progress=ReadinessProgress(attempt_count=5, first_started_at=START),
        )

        self.assertEqual(attempt.outcome, ReadinessOutcome.READY)
        self.assertEqual([record.outcome for record in store.records], [ReadinessOutcome.READY])

    def test_a_late_tick_does_not_throw_away_a_successful_probe(self) -> None:
        """**tick 粒度不该让一个人失去就绪**。

        调度晚了一整轮（60 秒）才把最后一次探针发出去，它仍然成立——阻塞形态的整轮硬
        上界在 tick 形态里换成了"每次探针自己的超时"，理由见 ``ReadinessTicker`` 文档。
        """

        ticker, _, _, _, clock = _ticker(6, schedule=CONTRACT_SCHEDULE)
        clock.advance(960)

        attempt = ticker.advance(
            BINDING,
            permissions=PERMISSIONS,
            progress=ReadinessProgress(attempt_count=5, first_started_at=START),
        )

        self.assertEqual(attempt.outcome, ReadinessOutcome.READY)

    def _slow_ticker(self, overrun_seconds: int, *, clock: FakeClock | None = None):
        """一个**无视自己超时**的探针：返回成功，但花掉比 probe_timeout 更久。"""

        the_clock = clock or FakeClock()

        class SlowProbe:
            calls: list[str] = []

            def list_metrics(self, *, user_id: str) -> int:
                self.calls.append(user_id)
                the_clock.advance(overrun_seconds)
                return 9

        store = FakeStore()
        ticker = ReadinessTicker(
            probe=SlowProbe(),
            store=store,
            audit=FakeAudit(),
            clock=the_clock,
            schedule=CONTRACT_SCHEDULE,
        )
        return ticker, store, the_clock

    def test_a_probe_that_ignores_its_own_timeout_is_still_capped(self) -> None:
        """"探针链失控"那一种仍然挡得住：无视自己超时的成功**不算就绪**。"""

        ticker, _, _ = self._slow_ticker(CONTRACT_SCHEDULE.probe_timeout_seconds + 5)

        attempt = ticker.advance(BINDING, permissions=PERMISSIONS, progress=ReadinessProgress())

        self.assertNotEqual(attempt.outcome, ReadinessOutcome.READY)
        self.assertIsNone(attempt.metric_count, "超窗的观察值一并丢弃")

    def test_a_slow_success_mid_sequence_does_not_close_the_confirmation(self) -> None:
        """否定断言（二级审查 N1）：**中途尝试的慢成功不得把整条确认收成终态**。

        按次超时的计时窗覆盖"取令牌 + 解密 + HTTP 往返"，偶尔超过 20 秒是现实的。
        把它判成 ``timed_out`` 终态，会让第 1 次尝试就把这条确认永久收口——预算还剩
        十四分钟，候选集却从此排除它，通知也一并被吞。它必须落**非终态**的技术失败，
        让计划表继续往下走。
        """

        ticker, store, _ = self._slow_ticker(CONTRACT_SCHEDULE.probe_timeout_seconds + 5)

        attempt = ticker.advance(BINDING, permissions=PERMISSIONS, progress=ReadinessProgress())

        self.assertEqual(attempt.outcome, ReadinessOutcome.TECHNICAL_FAILURE)
        self.assertEqual(attempt.error_code, "probe_overran_timeout")
        self.assertFalse(attempt.terminal, "第一次尝试的慢成功绝不是终态")
        self.assertEqual(
            [record.outcome for record in store.records],
            [ReadinessOutcome.TECHNICAL_FAILURE],
            "不得顺手补一条 timed_out",
        )

    def test_a_slow_success_on_the_last_attempt_still_closes_the_window(self) -> None:
        """反面：计划表最后一次的慢成功仍然收口——预算确实用完了。"""

        clock = FakeClock()
        clock.advance(900)
        ticker, store, _ = self._slow_ticker(
            CONTRACT_SCHEDULE.probe_timeout_seconds + 5, clock=clock
        )

        attempt = ticker.advance(
            BINDING,
            permissions=PERMISSIONS,
            progress=ReadinessProgress(attempt_count=5, first_started_at=START),
        )

        self.assertEqual(attempt.outcome, ReadinessOutcome.TIMED_OUT)
        self.assertEqual(attempt.error_code, "budget_exhausted")
        self.assertEqual(
            [record.outcome for record in store.records],
            [ReadinessOutcome.TECHNICAL_FAILURE, ReadinessOutcome.TIMED_OUT],
        )

    def test_a_success_that_lands_exactly_on_the_limit_is_still_ready(self) -> None:
        """边界是**严格大于**：恰好用满单次超时的成功仍然算就绪。"""

        clock = FakeClock()

        class ExactProbe:
            def list_metrics(self, *, user_id: str) -> int:
                clock.advance(CONTRACT_SCHEDULE.probe_timeout_seconds)
                return 4

        ticker = ReadinessTicker(
            probe=ExactProbe(),
            store=FakeStore(),
            audit=FakeAudit(),
            clock=clock,
            schedule=CONTRACT_SCHEDULE,
        )

        attempt = ticker.advance(BINDING, permissions=PERMISSIONS, progress=ReadinessProgress())

        self.assertEqual(attempt.outcome, ReadinessOutcome.READY)
        self.assertEqual(attempt.metric_count, 4)

    def test_without_a_probe_only_the_revocation_path_advances(self) -> None:
        """`probe=None`（端点未配）：撤权照常收口，需要探针的那一路本轮不动。"""

        store, audit = FakeStore(), FakeAudit()
        ticker = ReadinessTicker(
            probe=None, store=store, audit=audit, clock=FakeClock(), schedule=CONTRACT_SCHEDULE
        )
        self.assertFalse(ticker.probe_wired)

        revoked = ticker.advance(BINDING, permissions="{}", progress=ReadinessProgress())
        self.assertEqual(revoked.outcome, ReadinessOutcome.NO_PERMISSION)

        granted = ticker.advance(BINDING, permissions=PERMISSIONS, progress=ReadinessProgress())
        self.assertIsNone(granted, "需要探针的那一路不推进")
        self.assertEqual(len(store.records), 1, "不落任何假的失败记录")

    def test_without_a_probe_the_budget_is_never_burned(self) -> None:
        """否定断言：端点未配时**连收口都不做**——预算耗尽建立在"真的探过"之上。"""

        store = FakeStore()
        ticker = ReadinessTicker(
            probe=None, store=store, audit=FakeAudit(), clock=FakeClock(), schedule=CONTRACT_SCHEDULE
        )

        for count in (1, 6):
            with self.subTest(count):
                attempt = ticker.advance(
                    BINDING,
                    permissions=PERMISSIONS,
                    progress=ReadinessProgress(attempt_count=count, first_started_at=START),
                )
                self.assertIsNone(attempt)
        self.assertEqual(store.records, [])

    def test_the_blocking_form_still_requires_a_probe(self) -> None:
        """可缺省的探针只对 tick 形态有意义：开通编排拿不到结论就没法继续。"""

        with self.assertRaises(TypeError):
            McpReadinessConfirmation(
                probe=None,
                store=FakeStore(),
                audit=FakeAudit(),
                clock=FakeClock(),
                sleep=lambda seconds: None,
                schedule=FAST,
            )

    def test_an_exhausted_plan_without_a_terminal_row_is_closed_on_sight(self) -> None:
        """进程恰好在最后一次探针与收口之间重启：下一轮直接补上收口，不留半态。"""

        ticker, probe, _, _, clock = _ticker(schedule=CONTRACT_SCHEDULE)
        clock.advance(1_000)

        attempt = ticker.advance(
            BINDING,
            permissions=PERMISSIONS,
            progress=ReadinessProgress(attempt_count=6, first_started_at=START),
        )

        self.assertEqual(attempt.outcome, ReadinessOutcome.TIMED_OUT)
        self.assertEqual(probe.calls, [], "计划表已经用完，不再多发一次探针")

    def test_the_ticker_never_sleeps_and_never_holds_the_round(self) -> None:
        """否定断言：**tick 形态在源码里没有任何等待**。

        形状断言：构造函数不接受 ``sleep``，源码里也不出现 ``sleep`` / ``wait``。
        一个"顺手 sleep 一下"的实现会把整个 SchedulerLoop 占住十五分钟。
        """

        with self.assertRaises(TypeError):
            ReadinessTicker(  # type: ignore[call-arg]
                probe=FakeProbe(),
                store=FakeStore(),
                audit=FakeAudit(),
                clock=FakeClock(),
                sleep=lambda seconds: None,
            )
        # 扫的是**代码**不是文档：类文档里恰恰要写明"阻塞形态靠真实 sleep"这件事，
        # 拿原文扫描会把一份写得越清楚的文档判得越红。
        source = _code_without_docstrings(inspect.getsource(ReadinessTicker))
        for forbidden in ("sleep", "wait(", "time."):
            self.assertNotIn(forbidden, source, f"tick 形态不得等待：{forbidden}")

    def test_the_two_forms_share_one_probe_step(self) -> None:
        """两种形态**共用同一份**"发一次探针并落库"的实现，不是两套语义。"""

        self.assertIs(
            McpReadinessConfirmation._probe_once, ReadinessTicker._probe_once
        )
        self.assertIs(McpReadinessConfirmation._attempt, ReadinessTicker._attempt)

    def test_the_ticker_rejects_a_binding_of_the_wrong_type(self) -> None:
        ticker, _, _, _, _ = _ticker(4)

        with self.assertRaises(TypeError):
            ticker.advance(("usr_A", 7), permissions=PERMISSIONS, progress=ReadinessProgress())  # type: ignore[arg-type]


def _recovery_ticker(
    *script: object,
    schedule: ReadinessSchedule = CONTRACT_SCHEDULE,
    clock: FakeClock | None = None,
) -> tuple[ReadinessRecoveryTicker, FakeProbe, FakeStore, FakeAudit, FakeClock]:
    the_clock = clock or FakeClock()
    the_probe = FakeProbe(*script)
    the_store = FakeStore()
    the_audit = FakeAudit()
    return (
        ReadinessRecoveryTicker(
            probe=the_probe,
            store=the_store,
            audit=the_audit,
            clock=the_clock,
            schedule=schedule,
        ),
        the_probe,
        the_store,
        the_audit,
        the_clock,
    )


class ReadinessRecoveryTickerTest(unittest.TestCase):
    """超时后复检（V-开通-18）：同一套判定，只是不认「终态之后不再动」这条防线。

    首次开通链判过一次 ``timed_out`` 之后，没有任何东西会再回来看这个人
    （``core/identity/onboarding_runner.py`` 已登记的缺口）。本类是那条恢复路径的
    判定层：分类、探针调用、绑定、落库与审计与另外两种形态**同一份实现**
    （:meth:`test_it_shares_the_same_probe_step_as_the_other_two_forms`），新增的只是
    "不认终态、可以在 timed_out 之后继续探"这一条编排层的行为。
    """

    def test_a_late_success_is_ready(self) -> None:
        """核心场景：十五分钟超时之后，权限其实已经同步好了。"""

        ticker, probe, store, audit, _ = _recovery_ticker(3)

        attempt = ticker.probe_after_timeout(BINDING, attempt_no=7)

        self.assertEqual(attempt.outcome, ReadinessOutcome.READY)
        self.assertEqual(attempt.metric_count, 3)
        self.assertIsNone(attempt.error_code)
        self.assertEqual(probe.calls, [USER])
        self.assertEqual(len(store.records), 1, "就绪判定必须落库，供事后复审")
        self.assertEqual(audit.entries[-1][0], "mcp_readiness.ready")

    def test_an_empty_result_is_still_waiting_not_ready(self) -> None:
        """否定断言：明确空结果依旧不算就绪——探针法的核心判据没有因为复检而放宽。"""

        ticker, _, _, _, _ = _recovery_ticker(0)

        attempt = ticker.probe_after_timeout(BINDING, attempt_no=7)

        self.assertEqual(attempt.outcome, ReadinessOutcome.WAITING)

    def test_an_unreadable_result_is_a_technical_failure_not_ready(self) -> None:
        ticker, _, _, _, _ = _recovery_ticker(-1)

        attempt = ticker.probe_after_timeout(BINDING, attempt_no=7)

        self.assertEqual(attempt.outcome, ReadinessOutcome.TECHNICAL_FAILURE)

    def test_a_denied_probe_is_waiting_not_a_technical_failure(self) -> None:
        ticker, _, _, _, _ = _recovery_ticker(McpProbeError("still_syncing", denied=True))

        attempt = ticker.probe_after_timeout(BINDING, attempt_no=7)

        self.assertEqual(attempt.outcome, ReadinessOutcome.WAITING)

    def test_an_unclear_probe_failure_is_a_technical_failure(self) -> None:
        ticker, _, _, _, _ = _recovery_ticker(McpProbeError("network_error", denied=False))

        attempt = ticker.probe_after_timeout(BINDING, attempt_no=7)

        self.assertEqual(attempt.outcome, ReadinessOutcome.TECHNICAL_FAILURE)

    def test_the_recovery_ticker_never_produces_a_second_timeout(self) -> None:
        """否定断言：**本类从不产出 ``timed_out``**——那是原始那一轮唯一的终态，复检
        不该再记第二条同义的终态（模块文档「本类从不产出」一节）。

        用"超窗成功"这唯一会撞上超时判定的路径来证：探针跑得比它自己的超时还久，
        阻塞形态会把它判成 ``timed_out``（预算耗尽），本类必须与 tick 形态同一条纪律，
        把它降级成 ``technical_failure``，而不是升级成第二个 ``timed_out``。
        """

        schedule = ReadinessSchedule(
            interval_seconds=10, budget_seconds=10, probe_timeout_seconds=10
        )
        clock = FakeClock()

        class SlowProbe:
            def list_metrics(self, *, user_id: str) -> int:
                # 让"返回时刻"远远晚于探针自己的超时，模拟探针链失控。
                clock.advance(999)
                return 5

        ticker = ReadinessRecoveryTicker(
            probe=SlowProbe(),
            store=FakeStore(),
            audit=FakeAudit(),
            clock=clock,
            schedule=schedule,
        )

        attempt = ticker.probe_after_timeout(BINDING, attempt_no=7)

        self.assertEqual(attempt.outcome, ReadinessOutcome.TECHNICAL_FAILURE)
        self.assertEqual(attempt.error_code, "probe_overran_timeout")
        self.assertIsNone(attempt.metric_count)

    def test_it_never_re_judges_permission_presence(self) -> None:
        """本类不重复判断"有没有可等的权限"：签名里没有 ``permissions`` 参数可传，
        类型上就写不出"复检顺手判一次 no_permission"这件事。"""

        params = inspect.signature(ReadinessRecoveryTicker.probe_after_timeout).parameters
        self.assertNotIn("permissions", params)

    def test_probe_not_wired_advances_nothing(self) -> None:
        """探针未接线：不落任何记录、不烧预算，端点配好后原样继续。"""

        ticker = ReadinessRecoveryTicker(
            probe=None, store=FakeStore(), audit=FakeAudit(), clock=FakeClock()
        )

        attempt = ticker.probe_after_timeout(BINDING, attempt_no=7)

        self.assertIsNone(attempt)
        self.assertFalse(ticker.probe_wired)

    def test_it_rejects_a_binding_of_the_wrong_type(self) -> None:
        ticker, _, _, _, _ = _recovery_ticker(3)

        with self.assertRaises(TypeError):
            ticker.probe_after_timeout(("usr_A", 7), attempt_no=1)  # type: ignore[arg-type]

    def test_it_rejects_an_illegal_attempt_number(self) -> None:
        ticker, _, _, _, _ = _recovery_ticker(3)

        for bad in (0, -1, True, "7"):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    ticker.probe_after_timeout(BINDING, attempt_no=bad)  # type: ignore[arg-type]

    def test_it_shares_the_same_probe_step_as_the_other_two_forms(self) -> None:
        """三种形态**共用同一份**"发一次探针并落库"的实现，不是第三套判据。"""

        self.assertIs(
            McpReadinessConfirmation._probe_once, ReadinessRecoveryTicker._probe_once
        )
        self.assertIs(McpReadinessConfirmation._attempt, ReadinessRecoveryTicker._attempt)
        self.assertIs(ReadinessTicker._probe_once, ReadinessRecoveryTicker._probe_once)

    def test_it_never_sleeps(self) -> None:
        """否定断言：本类源码里没有任何等待——常驻职责的一轮 tick 不该被它占住。"""

        source = _code_without_docstrings(inspect.getsource(ReadinessRecoveryTicker))
        for forbidden in ("sleep", "wait(", "time."):
            self.assertNotIn(forbidden, source, f"复检形态不得等待：{forbidden}")

    def test_record_processing_failure_writes_a_technical_failure_without_probing(
        self,
    ) -> None:
        """外部独立审查 F4：探针之外的失败（渲染异常、状态推进异常……）也要占住这一次
        调度窗口，否则"毒候选"会在下一个 tick 立刻被重新选中，饿死排在后面的候选。"""

        ticker, probe, store, audit, _ = _recovery_ticker(3)

        attempt = ticker.record_processing_failure(
            BINDING, attempt_no=8, code="recovery_failed_RuntimeError"
        )

        self.assertEqual(attempt.outcome, ReadinessOutcome.TECHNICAL_FAILURE)
        self.assertEqual(attempt.error_code, "recovery_failed_RuntimeError")
        self.assertIsNone(attempt.metric_count)
        self.assertEqual(probe.calls, [], "本方法不发起任何探针调用")
        self.assertEqual(len(store.records), 1, "必须落库，候选查询的到期判据才会前移")
        self.assertEqual(audit.entries[-1][0], "mcp_readiness.technical_failure")

    def test_record_processing_failure_shares_the_same_attempt_helper(self) -> None:
        """与探针路径共用同一份落库 + 审计实现，不是第二套形状。"""

        self.assertIs(
            McpReadinessConfirmation._attempt, ReadinessRecoveryTicker._attempt
        )

    def test_record_processing_failure_rejects_a_binding_of_the_wrong_type(self) -> None:
        ticker, _, _, _, _ = _recovery_ticker(3)

        with self.assertRaises(TypeError):
            ticker.record_processing_failure(
                ("usr_A", 7), attempt_no=1, code="x"  # type: ignore[arg-type]
            )

    def test_record_processing_failure_rejects_an_illegal_attempt_number(self) -> None:
        ticker, _, _, _, _ = _recovery_ticker(3)

        for bad in (0, -1, True):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    ticker.record_processing_failure(
                        BINDING, attempt_no=bad, code="x"  # type: ignore[arg-type]
                    )

    def test_record_processing_failure_requires_a_code(self) -> None:
        ticker, _, _, _, _ = _recovery_ticker(3)

        for bad in ("", "   "):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    ticker.record_processing_failure(BINDING, attempt_no=1, code=bad)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
