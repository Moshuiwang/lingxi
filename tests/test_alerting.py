"""#92 的纯逻辑告警断言（L2；不连接真实飞书或生产数据库）。

#685 补充：gateway/scheduler 多条线程共享同一份告警状态机时，加锁前会在
「检查到期」与「产出/删除」之间的窗口里发生竞态（重复发送、`KeyError`、丢
计数）；下面的并发用例断言加锁后这三类竞态都不会再发生。
"""

from __future__ import annotations

import sys
import threading
import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock

from lingxi.core.alerting import (
    AlertDispatcher,
    AlertingDuty,
    AlertKind,
    AlertManager,
    AlertNotice,
    AlertPolicy,
    AlertSignal,
    HeartbeatRegistry,
    NoticeAction,
)

UTC = UTC
START = datetime(2026, 8, 8, 0, 0, tzinfo=UTC)
# 并发用例的重复次数：题面要求 ≥50，确认修复不是偶尔绿（也顺带确认变异后
# 不是偶尔红——两种方向都不能靠运气）。
_CONCURRENCY_ITERATIONS = 50


class AlertPolicyTests(unittest.TestCase):
    def test_confirmed_thresholds_are_defaults_and_can_be_injected(self) -> None:
        policy = AlertPolicy()

        self.assertEqual(policy.heartbeat_timeout_seconds, 120.0)
        self.assertEqual(policy.queued_timeout_seconds, 180.0)
        self.assertEqual(policy.running_heartbeat_timeout_seconds, 90.0)
        self.assertEqual(policy.send_failure_window_seconds, 300.0)
        self.assertEqual(policy.send_failure_threshold, 3)
        self.assertEqual(policy.dedupe_window_seconds, 1800.0)
        self.assertEqual(policy.recovery_stable_seconds, 300.0)

        injected = AlertPolicy.from_mapping(
            {
                "LINGXI_ALERT_HEARTBEAT_TIMEOUT_SECONDS": "121",
                "LINGXI_ALERT_SEND_FAILURE_THRESHOLD": "4",
            }
        )
        self.assertEqual(injected.heartbeat_timeout_seconds, 121.0)
        self.assertEqual(injected.send_failure_threshold, 4)

    def test_backoff_is_positive_and_strictly_increasing_before_ceiling(self) -> None:
        policy = AlertPolicy(retry_base_seconds=2.0, retry_factor=2.0, retry_ceiling_seconds=10.0)

        delays = [policy.retry_delay(attempt) for attempt in range(4)]

        self.assertEqual(delays, [2.0, 4.0, 8.0, 10.0])
        self.assertTrue(all(delay > 0 for delay in delays))
        self.assertGreater(delays[1], delays[0], "固定间隔不是退避")

    def test_zero_or_fixed_backoff_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            AlertPolicy(retry_base_seconds=0)
        with self.assertRaises(ValueError):
            AlertPolicy(retry_factor=1)


class HeartbeatTests(unittest.TestCase):
    def test_process_becomes_inactive_after_the_window_and_flip_is_observable(self) -> None:
        registry = HeartbeatRegistry(default_timeout_seconds=120)
        registry.register("worker")
        registry.beat("worker", at=START)

        active = registry.status("worker", at=START + timedelta(seconds=119))
        inactive = registry.status("worker", at=START + timedelta(seconds=120))
        still_inactive = registry.status("worker", at=START + timedelta(seconds=121))

        self.assertTrue(active.active)
        self.assertFalse(active.changed)
        self.assertFalse(inactive.active, "连续 2 分钟无心跳必须翻转为不活跃")
        self.assertTrue(inactive.changed, "观察点必须是活跃 → 不活跃的判定翻转")
        self.assertFalse(still_inactive.active)
        self.assertFalse(still_inactive.changed)

    def test_heartbeat_alert_and_recovery_are_emitted_once(self) -> None:
        manager = AlertManager()
        manager.register_process("gateway")
        manager.heartbeat("gateway", at=START)

        first = manager.check_heartbeats(at=START + timedelta(seconds=120), trace_id="01JTRACE")
        duplicate = manager.check_heartbeats(at=START + timedelta(seconds=121), trace_id="01JTRACE")
        manager.heartbeat("gateway", at=START + timedelta(seconds=122))
        manager.check_heartbeats(at=START + timedelta(seconds=122), trace_id="01JTRACE")
        recovery = manager.tick(at=START + timedelta(seconds=422))
        after_recovery = manager.tick(at=START + timedelta(seconds=423))

        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].kind, AlertKind.PROCESS_INACTIVE)
        self.assertEqual(duplicate, ())
        self.assertEqual(recovery[0].action, NoticeAction.RECOVERY)
        self.assertEqual(recovery[0].kind, AlertKind.PROCESS_INACTIVE)
        self.assertEqual(after_recovery, ())


class StuckTaskTests(unittest.TestCase):
    def test_three_stuck_task_types_have_distinct_event_types(self) -> None:
        manager = AlertManager()

        notices = [
            manager.task_stuck(AlertKind.QUEUED_STUCK, count=2, at=START, scope="worker"),
            manager.task_stuck(
                AlertKind.RUNNING_HEARTBEAT_TIMEOUT, count=1, at=START, scope="worker"
            ),
            manager.task_stuck(AlertKind.RETRY_EXHAUSTED, count=1, at=START, scope="worker"),
        ]

        event_types = {notice[0].event_type for notice in notices}
        self.assertEqual(
            event_types,
            {
                "worker.queued_stuck",
                "worker.running_heartbeat_timeout",
                "worker.retry_exhausted",
            },
        )
        self.assertEqual(len(event_types), 3, "三类滞留不能合并成同一个类型标识")


class SendFailureTests(unittest.TestCase):
    def test_notice_text_has_only_safe_summary_fields(self) -> None:
        notice = AlertManager().send_failure(
            channel="message_final", final=True, at=START, trace_id="01JTRACE"
        )[0]

        # Trace #469 S-1 TOP-2：8 类系统告警人话化，分行中文标签范式（照抄
        # scripts/ops/host_health_alert.py::render_message），不再是一行英文
        # key=value；event_type 组合键仍然对外（供审计/去重），只是不再原样
        # 拼进人类可读正文本身。
        self.assertEqual(notice.event_type, "message_final.feishu_send_failed")
        self.assertIn("飞书发送失败", notice.text)
        self.assertIn("范围：message_final", notice.text)
        self.assertIn("次数：1", notice.text)
        self.assertIn("追溯号：01JTRACE", notice.text)
        # #443 对外名称规范：运行告警群消息不得带内部代号「Lingxi」，对外统一「BI Plus」。
        self.assertIn("BI Plus", notice.text)
        for forbidden in ("用户正文哨兵", "张三", "E1001", "@", "https://", "secret", "Lingxi"):
            self.assertNotIn(forbidden, notice.text)

    def test_non_final_failures_need_three_failures_in_five_minutes(self) -> None:
        manager = AlertManager()

        self.assertEqual(
            manager.send_failure(channel="card", final=False, at=START, trace_id="01JTRACE"), ()
        )
        self.assertEqual(
            manager.send_failure(
                channel="card", final=False, at=START + timedelta(minutes=1), trace_id="01JTRACE"
            ),
            (),
        )
        notices = manager.send_failure(
            channel="card", final=False, at=START + timedelta(minutes=2), trace_id="01JTRACE"
        )

        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].kind, AlertKind.FEISHU_SEND_FAILED)
        self.assertEqual(notices[0].count, 3)

    def test_final_failure_is_immediate_and_fault_window_is_deduplicated(self) -> None:
        manager = AlertManager()

        first = manager.send_failure(channel="message", final=True, at=START, trace_id="01JTRACE")
        duplicate = manager.send_failure(
            channel="message", final=True, at=START + timedelta(minutes=29), trace_id="01JTRACE"
        )

        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].action, NoticeAction.ALERT)
        self.assertEqual(duplicate, (), "30 分钟去重窗口内只能有一条主告警")

    def test_recovery_requires_five_stable_minutes_and_is_exactly_once(self) -> None:
        manager = AlertManager()
        manager.send_failure(channel="message", final=True, at=START, trace_id="01JTRACE")

        self.assertEqual(
            manager.send_succeeded(
                channel="message", at=START + timedelta(seconds=1), trace_id="01JTRACE"
            ),
            (),
        )
        self.assertEqual(manager.tick(at=START + timedelta(minutes=4, seconds=59)), ())
        recovery = manager.tick(at=START + timedelta(minutes=5, seconds=1))

        self.assertEqual(len(recovery), 1)
        self.assertEqual(recovery[0].action, NoticeAction.RECOVERY)
        self.assertEqual(manager.tick(at=START + timedelta(minutes=6)), ())

    def test_a_single_non_final_failure_that_recovers_is_log_only(self) -> None:
        manager = AlertManager()

        manager.send_failure(channel="card", final=False, at=START)

        self.assertEqual(
            manager.send_succeeded(channel="card", at=START + timedelta(seconds=1)), ()
        )
        self.assertEqual(manager.tick(at=START + timedelta(minutes=5)), ())

    def test_failure_after_the_five_minute_window_starts_a_new_sequence(self) -> None:
        manager = AlertManager()

        manager.send_failure(channel="card", final=False, at=START)
        manager.send_failure(channel="card", final=False, at=START + timedelta(minutes=1))
        self.assertEqual(
            manager.send_failure(channel="card", final=False, at=START + timedelta(minutes=6)),
            (),
        )
        self.assertEqual(
            manager.send_failure(channel="card", final=False, at=START + timedelta(minutes=7)),
            (),
        )
        third = manager.send_failure(channel="card", final=False, at=START + timedelta(minutes=8))

        self.assertEqual(len(third), 1)
        self.assertEqual(third[0].count, 3)

    def test_dedupe_window_boundary_alerts_again_exactly_at_threshold(self) -> None:
        # 去重窗口用 `<`（observe: 距上次主告警 < dedupe_window 才压制）。整点到达
        # 阈值时不再算重复，必须重新告警。把 `<` 改成 `<=` 会让整点边界被误判为重复，
        # 本用例随之变红。
        manager = AlertManager()
        window = manager.policy.dedupe_window_seconds  # 默认 1800s / 30 分钟

        first = manager.send_failure(channel="message", final=True, at=START, trace_id="01JTRACE")
        just_inside = manager.send_failure(
            channel="message",
            final=True,
            at=START + timedelta(seconds=window - 1),
            trace_id="01JTRACE",
        )
        at_boundary = manager.send_failure(
            channel="message",
            final=True,
            at=START + timedelta(seconds=window),
            trace_id="01JTRACE",
        )

        self.assertEqual(len(first), 1)
        self.assertEqual(just_inside, (), "去重窗口内(1799s)仍必须去重")
        self.assertEqual(len(at_boundary), 1, "整点到达去重窗口阈值(1800s)必须重新告警")
        self.assertEqual(at_boundary[0].action, NoticeAction.ALERT)

    def test_recovery_window_boundary_recovers_exactly_at_threshold(self) -> None:
        # 恢复稳定窗口用 `<`（_recover_due: 稳定时长 < recovery_stable 就继续等待）。稳定
        # 时长整点到达阈值时必须发恢复。把 `<` 改成 `<=` 会让整点边界漏发恢复，本用例
        # 随之变红。
        manager = AlertManager()
        stable = manager.policy.recovery_stable_seconds  # 默认 300s / 5 分钟
        recovery_start = START + timedelta(minutes=1)

        manager.send_failure(channel="message", final=True, at=START, trace_id="01JTRACE")
        manager.send_succeeded(channel="message", at=recovery_start, trace_id="01JTRACE")
        just_inside = manager.tick(at=recovery_start + timedelta(seconds=stable - 1))
        at_boundary = manager.tick(at=recovery_start + timedelta(seconds=stable))

        self.assertEqual(just_inside, (), "稳定窗口内(299s)不能提前发恢复")
        self.assertEqual(len(at_boundary), 1, "整点到达恢复稳定阈值(300s)必须发恢复")
        self.assertEqual(at_boundary[0].action, NoticeAction.RECOVERY)


class _BarrierBlockingSender:
    """真实发送方的替身：在发送时阻塞于两方栅栏，逼出未加锁实现的双发竞态。

    未修复的 ``AlertDispatcher.run_once`` 在"检查到期"与"删除"之间隔着一次真实
    网络往返；这里用栅栏模拟那段窗口——如果两条线程都认为同一条告警可以发送，
    它们会在这里撞见彼此，`wait` 立即返回，双双继续发送。修复后同一条告警只会
    被一条线程认领，另一条线程根本不会调用 ``send_text``；栅栏等不到第二方，
    超时后自行放行（`BrokenBarrierError` 是预期的正常退出路径，不是失败）。
    """

    def __init__(self, barrier: threading.Barrier) -> None:
        self._barrier = barrier
        self.calls: list[str] = []

    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None:
        del chat_id, text
        self.calls.append(dedupe_key)
        try:
            self._barrier.wait(timeout=0.05)
        except threading.BrokenBarrierError:
            pass


class DispatcherConcurrencyTests(unittest.TestCase):
    """V-告警-01 ①：两条线程并发 `run_once` 必须恰好发送一次、都不抛异常。"""

    def test_two_threads_running_once_send_exactly_once(self) -> None:
        for iteration in range(_CONCURRENCY_ITERATIONS):
            self._assert_single_send_under_concurrent_run_once(iteration)

    def _assert_single_send_under_concurrent_run_once(self, iteration: int) -> None:
        barrier = threading.Barrier(2)
        sender = _BarrierBlockingSender(barrier)
        dispatcher = AlertDispatcher(sender=sender, chat_id="chat", clock=lambda: START)
        notices = AlertManager().send_failure(
            channel="probe", final=True, at=START, trace_id="01JTRACE"
        )
        dispatcher.submit(notices)

        errors: list[BaseException] = []
        results: list[int] = []

        def run() -> None:
            try:
                results.append(dispatcher.run_once(at=START))
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        label = f"第 {iteration} 轮"
        self.assertEqual(errors, [], f"{label}：run_once 不应抛出异常：{errors!r}")
        self.assertEqual(
            len(sender.calls), 1, f"{label}：并发推进必须恰好发送一次，实际 {sender.calls!r}"
        )
        self.assertEqual(sum(results), 1, f"{label}：两次 run_once 合计成功数必须恰好 1")
        self.assertEqual(dispatcher.pending_count, 0, f"{label}：成功发送后待发队列必须清零")


class RecoverDueConcurrencyTests(unittest.TestCase):
    """V-告警-01 ②：`_recover_due` 同型——两线程同时 `tick` 必须恰好恢复一次。"""

    def test_two_threads_ticking_together_emit_exactly_one_recovery(self) -> None:
        for iteration in range(_CONCURRENCY_ITERATIONS):
            self._assert_single_recovery_under_concurrent_tick(iteration)

    def _assert_single_recovery_under_concurrent_tick(self, iteration: int) -> None:
        manager = AlertManager()
        manager.send_failure(channel="probe", final=True, at=START, trace_id="01JTRACE")
        recovery_start = START + timedelta(seconds=1)
        manager.send_succeeded(channel="probe", at=recovery_start, trace_id="01JTRACE")
        recover_at = recovery_start + timedelta(seconds=manager.policy.recovery_stable_seconds)

        barrier = threading.Barrier(2)
        original_notice = AlertManager._notice

        # 在"判定恢复到期"之后、"产出通知"这一步阻塞于栅栏——这是 `_recover_due`
        # 里唯一对应 dispatcher 网络往返的位置：两线程都判定到期后才会走到这里。
        def delayed_notice(window: object, action: NoticeAction, at: datetime) -> AlertNotice:
            try:
                barrier.wait(timeout=0.05)
            except threading.BrokenBarrierError:
                pass
            return original_notice(window, action, at)

        errors: list[BaseException] = []
        notices: list[AlertNotice] = []

        def run() -> None:
            try:
                notices.extend(manager.tick(at=recover_at))
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        with mock.patch.object(AlertManager, "_notice", staticmethod(delayed_notice)):
            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

        label = f"第 {iteration} 轮"
        self.assertEqual(errors, [], f"{label}：tick 不应抛出异常：{errors!r}")
        self.assertEqual(len(notices), 1, f"{label}：并发 tick 必须恰好产出 1 条恢复通知")
        self.assertEqual(notices[0].action, NoticeAction.RECOVERY)


class ObserveConcurrencyTests(unittest.TestCase):
    """V-告警-01 ③：N 条线程各观察一次，累计计数必须恰好为 N，不丢计数。"""

    def test_concurrent_observations_do_not_lose_the_count(self) -> None:
        for iteration in range(_CONCURRENCY_ITERATIONS):
            self._assert_all_observations_are_counted(iteration)

    def _assert_all_observations_are_counted(self, iteration: int) -> None:
        threads_count = 20
        manager = AlertManager(policy=AlertPolicy(send_failure_threshold=threads_count))
        barrier = threading.Barrier(threads_count)
        errors: list[BaseException] = []
        notices: list[AlertNotice] = []

        def run() -> None:
            try:
                barrier.wait(timeout=2.0)
                notices.extend(manager.send_failure(channel="probe", final=False, at=START))
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=run) for _ in range(threads_count)]
        # 观察窗口内没有网络调用可供阻塞；改用缩短 GIL 切换间隔的办法逼出更多
        # 上下文切换机会，让未加锁的读-改-写在 50 轮里可靠地暴露丢计数。
        previous_interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
        finally:
            sys.setswitchinterval(previous_interval)

        label = f"第 {iteration} 轮"
        self.assertEqual(errors, [], f"{label}：observe 不应抛出异常：{errors!r}")
        self.assertEqual(
            len(notices), 1, f"{label}：{threads_count} 次并发观察必须恰好触发 1 条告警"
        )
        self.assertEqual(notices[0].count, threads_count, f"{label}：并发 observe 不能丢计数")


_GATEWAY_ENV_PREFIX = "LINGXI_GATEWAY_"
_GATEWAY_MINIMAL_ENV = {
    f"{_GATEWAY_ENV_PREFIX}APP_ID": "cli_fake_app_id",
    f"{_GATEWAY_ENV_PREFIX}APP_SECRET": "fake-app-secret-for-tests-only-8Xq2",
    f"{_GATEWAY_ENV_PREFIX}POSTGRES_DSN": (
        "postgresql://lingxi:fake-password-for-tests-only@db.invalid/lingxi"
    ),
}


class GatewayAlertingAuditTests(unittest.TestCase):
    """V-告警-01 ⑤：gateway 装配路径上必须产出 `alert.sent` / `alert.send_failed`。

    改前 `apps/gateway/alerting.py` 构造 `AlertDispatcher` 时没有传 `audit=`，
    这两条审计永远不会产生；本用例断言改后两条都会产生。
    """

    def test_dispatcher_emits_sent_and_failed_audit_events(self) -> None:
        from lingxi.apps.gateway.alerting import LogOnlyAlertSender, build_alerting_duty
        from lingxi.apps.gateway.config import load_config

        config = load_config(_GATEWAY_MINIMAL_ENV)
        duty = build_alerting_duty(config)

        sent_notices = duty.manager.send_failure(
            channel="probe_sent", final=True, at=START, trace_id="01JTRACE"
        )
        duty.dispatcher.submit(sent_notices)
        # 不传 `at=`：`submit` 用的是 dispatcher 自己的真实时钟给 `next_attempt_at`
        # 盖戳，`run_once` 必须用同一把时钟判定到期，否则会被"未到期"误跳过
        # （`send_failure` 的 `at=START` 只落进通知正文的 `observed_at`，与调度
        # 时钟无关）。
        with self.assertLogs("lingxi.apps.gateway", level="INFO") as sent_logs:
            sent_count = duty.dispatcher.run_once()
        self.assertEqual(sent_count, 1)
        self.assertTrue(any("alert.sent" in line for line in sent_logs.output), sent_logs.output)

        failed_notices = duty.manager.send_failure(
            channel="probe_failed", final=True, at=START, trace_id="01JTRACE"
        )
        duty.dispatcher.submit(failed_notices)
        with (
            mock.patch.object(LogOnlyAlertSender, "send_text", side_effect=RuntimeError("boom")),
            self.assertLogs("lingxi.apps.gateway", level="WARNING") as failed_logs,
        ):
            duty.dispatcher.run_once()
        self.assertTrue(
            any("alert.send_failed" in line for line in failed_logs.output), failed_logs.output
        )


class _RecordingSender:
    """记录每次调用的最小 ``AlertSender`` 实现，不做任何真实网络调用。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None:
        self.calls.append({"chat_id": chat_id, "text": text, "dedupe_key": dedupe_key})


def _observe_via_delivery_callback(kind: str, task_id: str) -> AlertManager:
    """走真实的 ``delivery_alert_callback`` 注入点观察一次，返回底层状态机。"""
    duty = AlertingDuty(
        manager=AlertManager(), dispatcher=AlertDispatcher(sender=_RecordingSender(), chat_id="oc")
    )
    duty.delivery_alert_callback()(kind, task_id)
    return duty.manager


class DeliveryChainAlertReclassificationTests(unittest.TestCase):
    """Issue #684：投递循环/文档投递维护的数据库、循环类故障此前恒定复用
    `feishu_send_failed`，数据库故障因此在管理群里显示成「飞书发送失败」，把
    排查方向指错。下面断言三个被搬走的上报点落新格、真正的飞书发送失败不受
    影响、且新格复用原有节奏（阈值/去重/窗口重置/`final` 校验/窗口释放）。"""

    def test_the_three_moved_report_points_land_in_the_new_kind_not_feishu_send_failed(
        self,
    ) -> None:
        cases = {
            "delivery_loop_failed:list_pending": "gateway-delivery-loop",
            "progress_persist_failed:RuntimeError": "tsk_01J00000000000000000000001",
            "document_delivery_reclaim_failed": "",
        }
        for kind, task_id in cases.items():
            with self.subTest(kind=kind):
                manager = _observe_via_delivery_callback(kind, task_id)
                windows = list(manager._windows.values())
                self.assertEqual(len(windows), 1)
                self.assertEqual(windows[0].kind, AlertKind.DELIVERY_CHAIN_FAILED)
                self.assertNotEqual(
                    windows[0].kind,
                    AlertKind.FEISHU_SEND_FAILED,
                    f"{kind} 是数据库/循环故障，不是飞书发送失败",
                )

    def test_a_real_feishu_send_failure_is_still_feishu_send_failed(self) -> None:
        notice = AlertManager().send_failure(
            channel="card", final=True, at=START, trace_id="01JTRACE"
        )[0]

        self.assertEqual(notice.kind, AlertKind.FEISHU_SEND_FAILED)

    def test_scope_is_unchanged_by_the_reclassification(self) -> None:
        # 逐字取自生产事故复现（Issue #684）与本文件既有 scope 归一化断言。
        cases = {
            "delivery_loop_failed:list_pending": (
                "gateway-delivery-loop",
                "delivery_loop_failed_list_pending",
            ),
            "progress_persist_failed:RuntimeError": (
                "tsk_01J00000000000000000000001",
                "progress_persist_failed_runtimeerror",
            ),
            "document_delivery_reclaim_failed": ("", "document_delivery_reclaim_failed"),
        }
        for kind, (task_id, expected_scope) in cases.items():
            with self.subTest(kind=kind):
                manager = _observe_via_delivery_callback(kind, task_id)
                window = next(iter(manager._windows.values()))
                self.assertEqual(window.scope, expected_scope)

    def test_new_kind_threshold_dedupe_and_window_reset_match_feishu_send_failed(self) -> None:
        for kind in (AlertKind.FEISHU_SEND_FAILED, AlertKind.DELIVERY_CHAIN_FAILED):
            with self.subTest(kind=kind):
                manager = AlertManager()
                self.assertEqual(
                    manager.observe(AlertSignal(kind=kind, observed_at=START, scope="x")), ()
                )
                self.assertEqual(
                    manager.observe(
                        AlertSignal(kind=kind, observed_at=START + timedelta(minutes=1), scope="x")
                    ),
                    (),
                )
                third = manager.observe(
                    AlertSignal(kind=kind, observed_at=START + timedelta(minutes=2), scope="x")
                )
                self.assertEqual(len(third), 1, "非 final 类型必须攒够 3 次 / 5 分钟才告警")
                self.assertEqual(third[0].count, 3)

                reset = AlertManager()
                reset.observe(AlertSignal(kind=kind, observed_at=START, scope="y"))
                reset.observe(
                    AlertSignal(kind=kind, observed_at=START + timedelta(minutes=1), scope="y")
                )
                self.assertEqual(
                    reset.observe(
                        AlertSignal(kind=kind, observed_at=START + timedelta(minutes=6), scope="y")
                    ),
                    (),
                    "五分钟窗口过期必须换成全新窗口，不能延续旧计数",
                )

                immediate = AlertManager()
                first = immediate.observe(
                    AlertSignal(kind=kind, observed_at=START, scope="z", final=True)
                )
                duplicate = immediate.observe(
                    AlertSignal(
                        kind=kind, observed_at=START + timedelta(minutes=29), scope="z", final=True
                    )
                )
                self.assertEqual(len(first), 1)
                self.assertEqual(duplicate, (), "30 分钟去重窗口内 final 类型只能有一条主告警")

    def test_final_true_is_accepted_for_the_new_kind_and_still_rejected_elsewhere(self) -> None:
        AlertSignal(kind=AlertKind.DELIVERY_CHAIN_FAILED, observed_at=START, scope="x", final=True)

        with self.assertRaises(ValueError):
            AlertSignal(kind=AlertKind.PROCESS_INACTIVE, observed_at=START, scope="x", final=True)

    def test_a_delivery_chain_window_with_a_real_task_id_is_released_after_idle_timeout(
        self,
    ) -> None:
        manager = AlertManager()
        task_id = "tsk_01J00000000000000000000001"
        manager.observe(
            AlertSignal(
                kind=AlertKind.DELIVERY_CHAIN_FAILED,
                observed_at=START,
                scope="progress_persist_failed_runtimeerror",
                task_id=task_id,
            )
        )
        self.assertEqual(len(manager._windows), 1)

        idle = max(manager.policy.dedupe_window_seconds, manager.policy.send_failure_window_seconds)
        manager.tick(at=START + timedelta(seconds=idle))
        self.assertEqual(len(manager._windows), 0, "带真实任务号的窗口必须在闲置超时后释放")

        manager.tick(at=START + timedelta(seconds=idle + 1))
        self.assertEqual(len(manager._windows), 0, "释放之后重复 tick 不能让 _windows 重新增长")

    def test_an_unmapped_kind_falls_back_to_feishu_send_failed_without_raising(self) -> None:
        manager = _observe_via_delivery_callback("totally_unregistered_kind:example", "")

        window = next(iter(manager._windows.values()))
        self.assertEqual(window.kind, AlertKind.FEISHU_SEND_FAILED)

    def test_the_rendered_notice_text_says_delivery_chain_not_feishu_send(self) -> None:
        sender = _RecordingSender()
        duty = AlertingDuty(
            manager=AlertManager(),
            dispatcher=AlertDispatcher(sender=sender, chat_id="oc_group", clock=lambda: START),
            clock=lambda: START,
        )

        duty.delivery_alert_callback()("delivery_loop_failed:list_pending", "gateway-delivery-loop")
        duty.dispatcher.run_once(at=START)

        self.assertEqual(len(sender.calls), 1)
        text = sender.calls[0]["text"]
        self.assertIn("类型：投递链路故障", text)
        self.assertNotIn("飞书发送失败", text)

    def test_audit_event_type_reflects_the_new_kind(self) -> None:
        notices = AlertManager().observe(
            AlertSignal(
                kind=AlertKind.DELIVERY_CHAIN_FAILED,
                observed_at=START,
                scope="delivery_loop_failed_list_pending",
                final=True,
            )
        )

        self.assertEqual(len(notices), 1)
        self.assertEqual(
            notices[0].event_type, "delivery_loop_failed_list_pending.delivery_chain_failed"
        )
        self.assertNotEqual(
            notices[0].event_type, "delivery_loop_failed_list_pending.feishu_send_failed"
        )


if __name__ == "__main__":
    unittest.main()
