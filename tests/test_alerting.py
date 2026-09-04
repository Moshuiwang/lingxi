"""#92 的纯逻辑告警断言（L2；不连接真实飞书或生产数据库）。"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from lingxi.core.alerting import (
    AlertKind,
    AlertManager,
    AlertPolicy,
    HeartbeatRegistry,
    NoticeAction,
)

UTC = UTC
START = datetime(2026, 8, 8, 0, 0, tzinfo=UTC)


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


if __name__ == "__main__":
    unittest.main()
