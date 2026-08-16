"""#92 的跨层白盒断言；不连接真实飞书或生产数据库。"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import subprocess
import unittest
from datetime import datetime, timedelta, timezone

from lingxi.adapters.feishu_group_message import FeishuGroupMessages
from lingxi.adapters.feishu_longconn import LongConnectionSupervisor, TerminationReason
from lingxi.adapters.postgres_conversation import TerminalTask
from lingxi.apps.scheduler import (
    AlertDispatcher,
    AlertingDuty,
    SchedulerConfig,
    SchedulerLoop,
)
from lingxi.apps.worker.config import WorkerConfig
from lingxi.apps.worker.service import WorkerService
from lingxi.core.alerting import AlertManager, AlertPolicy
from lingxi.core.execution.card_stream import CardStream, DeliveryRejected
from lingxi.config.content import default_content_catalog


UTC = timezone.utc
START = datetime(2026, 8, 8, 0, 0, tzinfo=UTC)


class ManualClock:
    def __init__(self, value: datetime = START) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class RecordingAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, object]]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, dict(fields)))

    def actions(self) -> list[str]:
        return [action for action, _fields in self.records]


class FakeAlertSender:
    def __init__(self, *, failures: int = 0) -> None:
        self.failures = failures
        self.calls: list[dict[str, str]] = []
        self.restart_calls = 0

    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None:
        self.calls.append({"chat_id": chat_id, "text": text, "dedupe_key": dedupe_key})
        if self.failures:
            self.failures -= 1
            raise RuntimeError("transport failure")

    def restart(self) -> None:
        self.restart_calls += 1


class AlertDispatcherTests(unittest.TestCase):
    def test_failed_alert_delivery_is_retried_with_positive_backoff(self) -> None:
        clock = ManualClock()
        audit = RecordingAudit()
        sender = FakeAlertSender(failures=2)
        dispatcher = AlertDispatcher(
            sender=sender,
            chat_id="oc_fake_alert_group",
            policy=AlertPolicy(retry_base_seconds=2, retry_ceiling_seconds=8),
            audit=audit,
            clock=clock,
        )
        notice = AlertManager().send_failure(
            channel="message_final", final=True, at=START, trace_id="01JTRACE"
        )[0]

        dispatcher.submit((notice,))
        self.assertEqual(dispatcher.run_once(), 0)
        self.assertEqual(dispatcher.pending_count, 1)
        self.assertEqual(dispatcher.observed_delays, [2.0])

        clock.value = START + timedelta(seconds=2)
        self.assertEqual(dispatcher.run_once(), 0)
        self.assertEqual(dispatcher.observed_delays, [2.0, 4.0])

        clock.value = START + timedelta(seconds=6)
        self.assertEqual(dispatcher.run_once(), 1)
        self.assertEqual(dispatcher.pending_count, 0)
        self.assertIn("alert.send_failed", audit.actions())
        self.assertIn("alert.sent", audit.actions())
        self.assertNotIn("用户正文哨兵", sender.calls[0]["text"])
        self.assertNotIn("http", sender.calls[0]["text"])

    def test_recovery_is_recorded_and_sent_once_without_repair_side_effects(self) -> None:
        clock = ManualClock()
        audit = RecordingAudit()
        sender = FakeAlertSender()
        duty = AlertingDuty(
            manager=AlertManager(),
            dispatcher=AlertDispatcher(
                sender=sender,
                chat_id="oc_fake_alert_group",
                audit=audit,
                clock=clock,
            ),
            audit=audit,
            clock=clock,
        )
        outcome = duty.send_outcome_callback()

        outcome("message_final", False)
        duty.run_once()
        clock.value = START + timedelta(seconds=1)
        outcome("message_final", True)
        duty.run_once()
        clock.value = START + timedelta(seconds=301)
        duty.run_once()
        duty.run_once()

        self.assertEqual(len(sender.calls), 2, "主告警与恢复各只能投递一次")
        self.assertEqual(audit.actions().count("alert.recovery_recorded"), 1)
        self.assertEqual(sender.restart_calls, 0, "告警路径不得重启进程")
        self.assertTrue(all("http" not in call["text"] for call in sender.calls))

    def test_alert_failure_does_not_skip_another_scheduler_duty(self) -> None:
        clock = ManualClock()
        sender = FakeAlertSender(failures=10)
        alert_duty = AlertingDuty(
            manager=AlertManager(),
            dispatcher=AlertDispatcher(
                sender=sender,
                chat_id="oc_fake_alert_group",
                clock=clock,
            ),
            clock=clock,
        )
        alert_duty.send_outcome_callback()("message_final", False)
        other_runs: list[int] = []

        class OtherDuty:
            name = "other"

            def run_once(self) -> int:
                other_runs.append(1)
                return 1

        loop = SchedulerLoop(duties=(alert_duty, OtherDuty()), heartbeat=lambda: None)

        loop.run_once()

        self.assertEqual(other_runs, [1])
        self.assertEqual(alert_duty.dispatcher.pending_count, 1)


class AdapterOutcomeTests(unittest.TestCase):
    def test_group_sender_reports_success_and_failure_without_changing_send_semantics(self) -> None:
        outcomes: list[tuple[str, bool]] = []
        fail_message = [False]

        def transport(_method: str, url: str, **_kwargs: object) -> dict[str, object]:
            if url.endswith("tenant_access_token/internal"):
                return {"tenant_access_token": "token"}
            if fail_message[0]:
                raise TimeoutError("transport")
            return {"code": 0}

        sender = FeishuGroupMessages(
            base_url="https://open.feishu.test/open-apis",
            app_id="cli_fake",
            app_secret="secret_fake",
            transport=transport,
            on_send_outcome=lambda operation, succeeded: outcomes.append(
                (operation, succeeded)
            ),
        )

        sender.send_text(chat_id="oc_fake", text="摘要", dedupe_key="d-1")
        fail_message[0] = True
        with self.assertRaises(TimeoutError):
            sender.send_text(chat_id="oc_fake", text="摘要", dedupe_key="d-2")

        self.assertEqual(outcomes, [("message_final", True), ("message_final", False)])

    def test_card_stream_reports_failed_card_and_successful_text_fallback(self) -> None:
        outcomes: list[tuple[str, bool]] = []

        class Cards:
            def create(self, **_kwargs: object) -> str:
                # 明确失败（白名单，独立审核 R-1）：只有 `DeliveryRejected` 会被
                # `CardStream.start()` 吞掉并置位 `fallback_needed`，其它任何异常
                # 类型都会原样抛出（结果不明），不再走到下面的文本兜底断言。
                raise DeliveryRejected("card unavailable")

            def update(self, **_kwargs: object) -> None:
                return None

            def close(self, **_kwargs: object) -> None:
                return None

        class Text:
            def send_text(self, **_kwargs: object) -> None:
                return None

        stream = CardStream(
            chat_id="chat",
            thread_id="thread",
            reply_to_message_id="message",
            transport=Cards(),
            fallback=Text(),
            on_send_outcome=lambda operation, succeeded: outcomes.append(
                (operation, succeeded)
            ),
        )

        stream.start()
        stream.send_fallback(default_content_catalog().text("worker.failed"))

        self.assertEqual(
            outcomes,
            [("card_non_final", False), ("message_final", True)],
        )


class HeartbeatAndWorkerEntryPointTests(unittest.TestCase):
    def test_long_connection_heartbeat_observer_failure_does_not_stop_the_pump(self) -> None:
        stop = [False]
        heartbeat_calls: list[int] = []
        audit: list[tuple[str, dict[str, object]]] = []

        class Transport:
            def stream(self):
                yield None
                stop[0] = True
                yield None

        def heartbeat() -> None:
            heartbeat_calls.append(1)
            raise RuntimeError("observer")

        supervisor = LongConnectionSupervisor(
            transport=Transport(),
            handle_event=lambda _payload: None,
            heartbeat=heartbeat,
            audit=lambda action, **fields: audit.append((action, fields)),
        )

        result = supervisor.run(should_stop=lambda: stop[0])

        self.assertEqual(result, TerminationReason.STOPPED)
        self.assertGreaterEqual(len(heartbeat_calls), 2)
        self.assertTrue(any(action == "longconn.heartbeat_failed" for action, _ in audit))

    def test_worker_reports_three_stuck_categories_and_keeps_claiming_when_alert_fails(self) -> None:
        events: list[tuple[str, int]] = []

        class Queue:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def fail_unavailable_versions(self, **_kwargs: object) -> list[TerminalTask]:
                self.calls.append("unavailable")
                return [TerminalTask("t1", "c1", "failed", "worker_version_unavailable")]

            def reclaim_queued(self, **_kwargs: object) -> list[TerminalTask]:
                self.calls.append("queued")
                return []

            def reclaim_stale_with_outcomes(self, **_kwargs: object):
                self.calls.append("stale")
                return ["requeued"], [TerminalTask("t2", "c2", "failed", "retry_exhausted")]

            def claim(self, **_kwargs: object) -> list[object]:
                self.calls.append("claim")
                return []

        queue = Queue()
        config = WorkerConfig(
            question="",
            read_only_tool="mcp__q__read",
            trace_id="01J00000000000000000000000",
            turn_timeout_seconds=1,
            worker_id="worker",
            target_worker_version="stable",
        )

        async def run_once() -> bool:
            return await WorkerService(
                config=config,
                queue=queue,
                heartbeat=lambda: events.append(("heartbeat", 1)),
                on_task_stuck=lambda kind, count: events.append((kind, count)) or (_ for _ in ()).throw(
                    RuntimeError("alert observer")
                ),
            ).process_once()

        self.assertTrue(asyncio.run(run_once()))
        self.assertEqual(queue.calls, ["unavailable", "queued", "stale", "claim"])
        self.assertEqual(events[0], ("heartbeat", 1))

    def test_scheduler_heartbeat_failure_does_not_skip_duties(self) -> None:
        runs: list[int] = []

        class Duty:
            def run_once(self) -> None:
                runs.append(1)

        loop = SchedulerLoop(
            duties=(Duty(),),
            heartbeat=lambda: (_ for _ in ()).throw(RuntimeError("heartbeat")),
        )

        loop.run_once()

        self.assertEqual(runs, [1])


class SchedulerConfigAlertTests(unittest.TestCase):
    def test_alert_policy_is_loaded_without_echoing_secrets(self) -> None:
        config = SchedulerConfig.from_env(
            {
                "LINGXI_POSTGRES_DSN": "postgresql://fake",
                "LINGXI_DELEGATED_CREDENTIAL_KEY": "key",
                "LINGXI_DELEGATED_CREDENTIAL_PATH": "/var/lib/lingxi/fake.enc",
                "LINGXI_FEISHU_APP_ID": "cli_fake",
                "LINGXI_FEISHU_APP_SECRET": "secret_fake",
                "LINGXI_ALERT_SEND_FAILURE_THRESHOLD": "4",
                "LINGXI_ALERT_RETRY_BASE_SECONDS": "2",
            }
        )

        self.assertEqual(config.alert_policy.send_failure_threshold, 4)
        self.assertEqual(config.alert_policy.retry_base_seconds, 2)
        self.assertNotIn("secret_fake", repr(config))


class AlertOrderDeterminismTests(unittest.TestCase):
    def test_recovery_order_is_stable_across_hash_seeds(self) -> None:
        repository = pathlib.Path(__file__).parents[1]
        script = """
from datetime import datetime, timedelta, timezone
from lingxi.core.alerting import AlertManager

now = datetime(2026, 8, 8, tzinfo=timezone.utc)
manager = AlertManager()
for channel in {"zeta", "alpha", "middle"}:
    manager.send_failure(channel=channel, final=True, at=now)
for channel in {"zeta", "alpha", "middle"}:
    manager.send_succeeded(channel=channel, at=now + timedelta(seconds=1))
print([notice.event_type for notice in manager.tick(at=now + timedelta(seconds=301))])
"""
        outputs: list[object] = []
        for seed in ("1", "2", "3"):
            environment = os.environ.copy()
            environment["PYTHONHASHSEED"] = seed
            environment["PYTHONPATH"] = str(repository / "src")
            result = subprocess.run(
                ["python3", "-c", script],
                cwd=repository,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            outputs.append(json.loads(result.stdout.replace("'", '"')))

        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[1], outputs[2])
        self.assertEqual(outputs[0], [
            "alpha.feishu_send_failed",
            "middle.feishu_send_failed",
            "zeta.feishu_send_failed",
        ])


class DeliveryAlertCallbackTests(unittest.TestCase):
    """Issue #153：Gateway ``DeliveryConsumer.on_alert`` 注入点接到真实告警路由
    ——``AlertingDuty.delivery_alert_callback()`` 是这条注入点的落地形式。"""

    def test_a_delivery_alert_is_observed_as_a_feishu_send_failed_signal(self) -> None:
        sender = FakeAlertSender()
        clock = ManualClock()
        duty = AlertingDuty(
            manager=AlertManager(policy=AlertPolicy(send_failure_threshold=1)),
            dispatcher=AlertDispatcher(sender=sender, chat_id="oc_group", clock=clock),
            clock=clock,
        )
        callback = duty.delivery_alert_callback()

        callback("dispatch_uncertain:card_finish", "01J00000000000000000000TASK")
        duty.dispatcher.run_once(at=clock.value)

        self.assertEqual(len(sender.calls), 1)
        self.assertIn("dispatch_uncertain_card_finish.feishu_send_failed", sender.calls[0]["text"])

    def test_kind_strings_are_sanitized_into_a_safe_scope(self) -> None:
        """真实 kind 字符串带冒号与大写字母（如 ``fallback_send_failed:
        TimeoutError``），AlertSignal 的 scope 校验只接受小写字母数字与
        ``_.-``——不消毒直接传会在这里抛 ValueError，告警本身也就不会发生。
        """

        sender = FakeAlertSender()
        clock = ManualClock()
        duty = AlertingDuty(
            manager=AlertManager(policy=AlertPolicy(send_failure_threshold=1)),
            dispatcher=AlertDispatcher(sender=sender, chat_id="oc_group", clock=clock),
            clock=clock,
        )
        callback = duty.delivery_alert_callback()

        # 不应抛出 ValueError——这正是本用例要证明的。
        callback("fallback_send_failed:TimeoutError", "01J00000000000000000000TASK")
        duty.dispatcher.run_once(at=clock.value)

        self.assertEqual(len(sender.calls), 1)

    def test_an_unsafe_task_id_degrades_to_no_trace_id_instead_of_dropping_the_alert(
        self,
    ) -> None:
        sender = FakeAlertSender()
        clock = ManualClock()
        duty = AlertingDuty(
            manager=AlertManager(policy=AlertPolicy(send_failure_threshold=1)),
            dispatcher=AlertDispatcher(sender=sender, chat_id="oc_group", clock=clock),
            clock=clock,
        )
        callback = duty.delivery_alert_callback()

        callback("dispatch_uncertain:card_create", "带空格的非法-id 值")
        duty.dispatcher.run_once(at=clock.value)

        self.assertEqual(len(sender.calls), 1, "trace_id 格式异常不得让整条告警消失")
        self.assertIn("trace_id=-", sender.calls[0]["text"])

    def test_different_delivery_kinds_do_not_share_a_dedupe_window(self) -> None:
        sender = FakeAlertSender()
        clock = ManualClock()
        duty = AlertingDuty(
            manager=AlertManager(policy=AlertPolicy(send_failure_threshold=1)),
            dispatcher=AlertDispatcher(sender=sender, chat_id="oc_group", clock=clock),
            clock=clock,
        )
        callback = duty.delivery_alert_callback()

        callback("dispatch_uncertain:card_create", "01J00000000000000000000TAS1")
        callback("progress_persist_failed:RuntimeError", "01J00000000000000000000TAS2")
        duty.dispatcher.run_once(at=clock.value)

        self.assertEqual(
            len(sender.calls), 2, "两类不同的投递失败必须各自独立限流，不能互相压制"
        )

    def test_a_single_uncertain_report_alerts_immediately_under_production_defaults(
        self,
    ) -> None:
        """PR #173 独立复核 P1-4：生产默认 ``AlertPolicy()``（``send_failure_
        window_seconds=300``、``send_failure_threshold=3``）与
        ``DeliveryConsumer.DEFAULT_ALERT_MIN_INTERVAL_SECONDS``（同为 300）两个
        300 秒相等，此前会让 `dispatch_uncertain:*` 无论按真实上报节奏重复多少次
        都恰好落在"窗口刚过期"一侧、``consecutive_failures`` 永远回到 1、
        ``threshold=3`` 永远到不了——用生产默认复现过，两小时内单个 uncertain
        任务被上报 24 次、实际发出的告警条数 = 0。

        修复后 `dispatch_uncertain:*` 走 ``final=True``：**单次**上报、**默认**
        策略，就必须立刻出一条告警——不必等第二次、第三次，也不必绕开生产默认值
        用 ``send_failure_threshold=1`` 这种测试专用配置。这正是本用例要钉住的
        回归：把 ``delivery_alert_callback`` 里的 ``final`` 改回恒为 ``False``，
        本用例会变红（发出的告警条数变成 0）。
        """

        sender = FakeAlertSender()
        clock = ManualClock()
        duty = AlertingDuty(
            manager=AlertManager(policy=AlertPolicy()),
            dispatcher=AlertDispatcher(sender=sender, chat_id="oc_group", clock=clock),
            clock=clock,
        )
        callback = duty.delivery_alert_callback()

        callback("dispatch_uncertain:card_finish", "01J00000000000000000000TASK")
        duty.dispatcher.run_once(at=clock.value)

        self.assertEqual(
            len(sender.calls),
            1,
            "结果不明、绝不自动重发的 uncertain 任务，必须在第一次被观察到时就"
            "报警，不能等到攒够 send_failure_threshold 次——生产默认下这个"
            "阈值在原实现里永远到不了",
        )

    def test_repeated_uncertain_reports_at_the_min_interval_still_respect_dedupe(
        self,
    ) -> None:
        """``final=True`` 不等于"每次上报都发一条新告警"：同一 (kind, task_id)
        仍然只受 ``dedupe_window_seconds``（生产默认 1800 秒）约束一次通知，
        不会因为改成 final 就从"从不告警"变成"刷屏"。"""

        sender = FakeAlertSender()
        clock = ManualClock()
        duty = AlertingDuty(
            manager=AlertManager(policy=AlertPolicy()),
            dispatcher=AlertDispatcher(sender=sender, chat_id="oc_group", clock=clock),
            clock=clock,
        )
        callback = duty.delivery_alert_callback()

        callback("dispatch_uncertain:card_finish", "01J00000000000000000000TASK")
        duty.dispatcher.run_once(at=clock.value)
        # 按 DeliveryConsumer.DEFAULT_ALERT_MIN_INTERVAL_SECONDS（300s）的上报
        # 节奏再上报一次：仍在 dedupe_window_seconds（1800s）之内。
        clock.value = clock.value + timedelta(seconds=300)
        callback("dispatch_uncertain:card_finish", "01J00000000000000000000TASK")
        duty.dispatcher.run_once(at=clock.value)

        self.assertEqual(
            len(sender.calls), 1, "同一 uncertain 任务在去重窗口内重复上报不应刷屏"
        )

    def test_a_fallback_send_rejection_alerts_immediately_under_production_defaults(
        self,
    ) -> None:
        """PR #173 独立复核 P1-4：``fallback_send_failed:*`` 是文本兜底遇到明确
        拒绝错误（``V-告警-03``「终态失败立即告警」覆盖的正是这一类），同样不能
        靠攒够 3 次同一 5 分钟窗口内的失败——生产默认下与 uncertain 类别是
        同一个 300s 撞 300s 的问题。"""

        sender = FakeAlertSender()
        clock = ManualClock()
        duty = AlertingDuty(
            manager=AlertManager(policy=AlertPolicy()),
            dispatcher=AlertDispatcher(sender=sender, chat_id="oc_group", clock=clock),
            clock=clock,
        )
        callback = duty.delivery_alert_callback()

        callback("fallback_send_failed:DeliveryRejected", "01J00000000000000000000TASK")
        duty.dispatcher.run_once(at=clock.value)

        self.assertEqual(len(sender.calls), 1)


if __name__ == "__main__":
    unittest.main()
