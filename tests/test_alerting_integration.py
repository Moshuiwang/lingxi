"""#92 的跨层白盒断言；不连接真实飞书或生产数据库。"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import subprocess
import unittest
from datetime import UTC, datetime, timedelta

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
from lingxi.apps.worker.service_ports import WorkerObservers
from lingxi.config.content import default_content_catalog
from lingxi.core.alerting import AlertManager, AlertPolicy
from lingxi.core.execution.card_stream import CardCreated, CardStream, DeliveryRejected

UTC = UTC
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


class _RecordingCardTransport:
    """建卡永远成功；``update``/``close`` 只记录调用，不做网络/DB 交互。"""

    def __init__(self) -> None:
        self.update_calls: list[dict] = []
        self.close_calls: list[dict] = []

    def create(self, **kwargs: object) -> CardCreated:
        return CardCreated(card_id="card-1", message_id="msg-1")

    def update(self, **kwargs: object) -> None:
        self.update_calls.append(kwargs)

    def close(self, **kwargs: object) -> None:
        self.close_calls.append(kwargs)


class _RefusingTextTransport:
    """卡片路径预期不降级；一旦真的被调用说明本次修复没有生效，让用例立刻失败。"""

    def send_text(self, **_kwargs: object) -> str:
        raise AssertionError("卡片路径应该成功，不应该降级到文本兜底")


class CardStreamModelGeneratedTextDeliveryTests(unittest.TestCase):
    """Issue #322：``CardStream.finish()`` 对模型生成的终态正文不再用为固定
    模板设计的自然语言词表拦截。纯内存假 transport，不连数据库、不连真实飞书——
    直接证明"含日常措辞的模型终态正文可以正常完成投递路径构建"这条回归。
    """

    def test_a_successful_answer_ending_in_everyday_wording_finishes_without_raising(
        self,
    ) -> None:
        """两次内测真实复现的原句之一：模型答案以「还需」收尾。此前会在
        ``finish()`` 内部抛 ``ContentSafetyError``，被 ``apps.gateway.delivery``
        当作"结果不明"转入 uncertain，任务卡死。"""

        answer = "已完成周环比分析，还需要看其他维度吗？"
        cards = _RecordingCardTransport()
        stream = CardStream(
            chat_id="chat",
            thread_id="thread",
            reply_to_message_id="message",
            transport=cards,
            fallback=_RefusingTextTransport(),
        )
        stream.start()

        stream.finish(result=answer, elapsed_seconds=12)

        self.assertFalse(stream.fallback_needed, "不应该降级为文本兜底")
        self.assertEqual(len(cards.update_calls), 1)
        self.assertEqual(len(cards.close_calls), 1)
        self.assertIn(answer, cards.update_calls[0]["card"].body)

    def test_a_stopped_partial_answer_relayed_as_failure_finishes_without_raising(
        self,
    ) -> None:
        """``/stop`` 中断的残余正文经 ``worker.stopped_result`` 渲染后，以
        ``failure=`` 形状交给 ``finish()``（gateway 侧非 SUCCESS 终态都走这条
        分支，见 ``apps/gateway/delivery.py::_handle_terminal``）——同样不能被
        固定词表拦住。"""

        stopped_content = default_content_catalog().text(
            "worker.stopped_result", result="还需要继续挖掘吗", contains_model_text=True
        )
        cards = _RecordingCardTransport()
        stream = CardStream(
            chat_id="chat",
            thread_id="thread",
            reply_to_message_id="message",
            transport=cards,
            fallback=_RefusingTextTransport(),
        )
        stream.start()

        stream.finish(failure=stopped_content, elapsed_seconds=5)

        self.assertFalse(stream.fallback_needed)
        self.assertIn("还需要继续挖掘吗", cards.update_calls[0]["card"].body)


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
            read_only_tools=("mcp__q__read",),
            trace_id="01J00000000000000000000000",
            turn_timeout_seconds=1,
            worker_id="worker",
            target_worker_version="stable",
        )

        async def run_once() -> bool:
            return await WorkerService(
                             config=config,
                             queue=queue,
                             observers=WorkerObservers(heartbeat=lambda: events.append(("heartbeat", 1)), on_task_stuck=lambda kind, count: events.append((kind, count)) or (_ for _ in ()).throw(
                    RuntimeError("alert observer")
                )),
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
        # Trace #469 S-1 TOP-2：分行中文标签范式，event_type 的"scope.kind"
        # 组合键拆成"范围"（scope 原样）与"类型"（kind 的中文标签）两行。
        self.assertIn("范围：dispatch_uncertain_card_finish", sender.calls[0]["text"])
        self.assertIn("类型：飞书发送失败", sender.calls[0]["text"])

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
        self.assertIn("追溯号：-", sender.calls[0]["text"])

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

    def test_delivery_loop_failures_alert_immediately_under_production_defaults(
        self,
    ) -> None:
        """Issue #191：投递消费循环自身的连续异常与线程死亡必须发得出去。

        这两类在**上报之前**就已经攒过次数了——`delivery_loop_failed:*` 要求连续
        ``DeliveryConsumer.DEFAULT_LOOP_FAILURE_ALERT_THRESHOLD`` 轮失败才上报，
        `delivery_loop_dead:*` 是一次不可逆事件。再让告警状态机按 5 分钟窗口攒
        第二遍，就会原样踩中 PR #173 独立复核 P1-4 那个 300 秒撞 300 秒的陷阱：
        `consecutive_failures` 每次被重置回 1、`threshold=3` 永远到不了，"整条
        投递能力已经停摆"这件事因此永远发不出一条告警——正是 #191 要消灭的
        "无声"。把 ``delivery_alert_callback`` 里的 ``delivery_loop_`` 前缀去掉，
        本用例会变红（发出的告警条数变成 0）。
        """

        for kind in ("delivery_loop_failed:list_pending", "delivery_loop_dead:RuntimeError"):
            with self.subTest(kind=kind):
                sender = FakeAlertSender()
                clock = ManualClock()
                duty = AlertingDuty(
                    manager=AlertManager(policy=AlertPolicy()),
                    dispatcher=AlertDispatcher(sender=sender, chat_id="oc_group", clock=clock),
                    clock=clock,
                )

                duty.delivery_alert_callback()(kind, "gateway-delivery-loop")
                duty.dispatcher.run_once(at=clock.value)

                self.assertEqual(
                    len(sender.calls),
                    1,
                    "投递循环停摆必须在第一次上报时就发出告警，不能等攒够阈值",
                )
                self.assertIn("追溯号：gateway-delivery-loop", sender.calls[0]["text"])

    def test_document_delivery_terminal_kinds_alert_immediately_under_production_defaults(
        self,
    ) -> None:
        """Issue #341 opus 审查 R-1：文档交付独立消费循环的三类单行终态（明确失败、
        结果不明、成功但通知发送失败）与循环死亡必须**单条即达讫**——不能等攒够
        阈值。文档交付是低频动作（见 ``apps/gateway/document_delivery.py`` 的
        ``DEFAULT_BATCH_LIMIT`` 文档），同一个任务在 300 秒窗口内几乎不可能自然
        重复三次触发同一个 ``(kind, task_id)``，原样套用"攒够阈值次数"等于让这
        几类实质上永远发不出告警——与 PR #173 独立复核 P1-4 是同一个 300 秒撞
        300 秒陷阱。把 ``delivery_alert_callback`` 里新增的
        ``document_delivery_*`` 判断删掉，本用例会变红（发出的告警条数变成 0）。
        """

        for kind in (
            "document_delivery_failed",
            "document_delivery_uncertain",
            "document_delivery_notice_failed",
            "document_delivery_loop_dead:RuntimeError",
        ):
            with self.subTest(kind=kind):
                sender = FakeAlertSender()
                clock = ManualClock()
                duty = AlertingDuty(
                    manager=AlertManager(policy=AlertPolicy()),
                    dispatcher=AlertDispatcher(sender=sender, chat_id="oc_group", clock=clock),
                    clock=clock,
                )

                duty.delivery_alert_callback()(kind, "01J00000000000000000000TASK")
                duty.dispatcher.run_once(at=clock.value)

                self.assertEqual(
                    len(sender.calls),
                    1,
                    "文档交付终态告警必须在第一次上报时就发出，不能等攒够阈值",
                )
                self.assertIn("追溯号：01J00000000000000000000TASK", sender.calls[0]["text"])


class OnboardingFailedAlertCallbackTests(unittest.TestCase):
    """Issue #280 §7.3 步 1：开通失败真实送达管理群
    ——``AlertingDuty.onboarding_failed_callback()`` 是编排注入点的落地形式。"""

    def test_a_failure_is_observed_as_an_onboarding_failed_signal(self) -> None:
        sender = FakeAlertSender()
        clock = ManualClock()
        duty = AlertingDuty(
            manager=AlertManager(policy=AlertPolicy(send_failure_threshold=1)),
            dispatcher=AlertDispatcher(sender=sender, chat_id="oc_group", clock=clock),
            clock=clock,
        )
        callback = duty.onboarding_failed_callback()

        callback("publish_not_completed", "01J00000000000000000000TRC1")
        duty.dispatcher.run_once(at=clock.value)

        self.assertEqual(len(sender.calls), 1)
        self.assertIn("范围：publish_not_completed", sender.calls[0]["text"])
        self.assertIn("类型：用户开通失败", sender.calls[0]["text"])
        self.assertIn("追溯号：01J00000000000000000000TRC1", sender.calls[0]["text"])

    def test_the_alert_text_never_contains_open_id_or_profile_values(self) -> None:
        """否定断言：群消息只含故障类别 + 计数 + 追溯号——回调签名里根本没有传
        open_id / 姓名的位置，这里显式证明正文里也没有出现过任何资料值形状的串。"""

        sender = FakeAlertSender()
        clock = ManualClock()
        duty = AlertingDuty(
            manager=AlertManager(policy=AlertPolicy(send_failure_threshold=1)),
            dispatcher=AlertDispatcher(sender=sender, chat_id="oc_group", clock=clock),
            clock=clock,
        )
        callback = duty.onboarding_failed_callback()

        callback("directory_unavailable", "01J00000000000000000000TRC2")
        duty.dispatcher.run_once(at=clock.value)

        text = sender.calls[0]["text"]
        for leak in ("ou_", "usr_", "张", "李", "@"):
            self.assertNotIn(leak, text)

    def test_reason_strings_are_sanitized_into_a_safe_scope(self) -> None:
        """内部原因码有时是 ``unexpected_ZeroDivisionError`` 这种带大写字母的形状
        （见 onboarding_runner._run 的 unexpected_ 分支）——不消毒直接传会在
        AlertSignal 校验里抛 ValueError，告警本身也就不会发生。"""

        sender = FakeAlertSender()
        clock = ManualClock()
        duty = AlertingDuty(
            manager=AlertManager(policy=AlertPolicy(send_failure_threshold=1)),
            dispatcher=AlertDispatcher(sender=sender, chat_id="oc_group", clock=clock),
            clock=clock,
        )
        callback = duty.onboarding_failed_callback()

        callback("unexpected_ZeroDivisionError", "01J00000000000000000000TRC3")
        duty.dispatcher.run_once(at=clock.value)

        self.assertEqual(len(sender.calls), 1)

    def test_an_unsafe_trace_id_degrades_to_no_trace_id_instead_of_dropping_the_alert(
        self,
    ) -> None:
        sender = FakeAlertSender()
        clock = ManualClock()
        duty = AlertingDuty(
            manager=AlertManager(policy=AlertPolicy(send_failure_threshold=1)),
            dispatcher=AlertDispatcher(sender=sender, chat_id="oc_group", clock=clock),
            clock=clock,
        )
        callback = duty.onboarding_failed_callback()

        callback("publish_failed", "带空格的非法-id 值")
        duty.dispatcher.run_once(at=clock.value)

        self.assertEqual(len(sender.calls), 1, "trace_id 格式异常不得让整条告警消失")
        self.assertIn("追溯号：-", sender.calls[0]["text"])


class OnboardingStalledAlertCallbackTests(unittest.TestCase):
    """Issue #280 §7.3 步 2：开通中途停摆收口的聚合计数同样送达管理群
    ——``AlertingDuty.onboarding_stalled_callback()``。"""

    def test_a_positive_count_is_observed(self) -> None:
        sender = FakeAlertSender()
        clock = ManualClock()
        duty = AlertingDuty(
            manager=AlertManager(policy=AlertPolicy(send_failure_threshold=1)),
            dispatcher=AlertDispatcher(sender=sender, chat_id="oc_group", clock=clock),
            clock=clock,
        )
        callback = duty.onboarding_stalled_callback()

        callback(3)
        duty.dispatcher.run_once(at=clock.value)

        self.assertEqual(len(sender.calls), 1)
        self.assertIn("范围：stalled_provisioning", sender.calls[0]["text"])
        self.assertIn("类型：用户开通失败", sender.calls[0]["text"])
        self.assertIn("次数：3", sender.calls[0]["text"])

    def test_a_zero_count_never_produces_an_alert(self) -> None:
        """否定断言：零计数不该报警——「有人卡住了」这句话必须是真的。"""

        sender = FakeAlertSender()
        clock = ManualClock()
        duty = AlertingDuty(
            manager=AlertManager(policy=AlertPolicy(send_failure_threshold=1)),
            dispatcher=AlertDispatcher(sender=sender, chat_id="oc_group", clock=clock),
            clock=clock,
        )
        callback = duty.onboarding_stalled_callback()

        callback(0)
        duty.dispatcher.run_once(at=clock.value)

        self.assertEqual(sender.calls, [])


if __name__ == "__main__":
    unittest.main()
