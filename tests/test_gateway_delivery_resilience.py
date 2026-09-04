"""Issue #191：投递消费循环的异常隔离，以及"线程死了一定有人知道"。

**不连数据库、不连飞书**：本文件断的是循环体自身的存活语义与告警接线，用假队列
注入瞬时/持续异常即可完整证伪；投递的正确性语义（严格递增序号、幂等、崩溃恢复、
24 小时到期）仍由 ``tests/test_gateway_delivery.py`` 在真库上覆盖，本文件一条都
不重复，也一条都不改动。

认领既有断言（不新增产品规则）：`V-告警-01`（gateway 一个进程两条循环，任一条
停摆都能被单独发现、不被另一条掩盖）、`V-告警-03`（投递侧的 ``on_alert`` 经
``AlertingDuty.delivery_alert_callback()`` 归入同一条告警通道）、`V-告警-05`
（告警观察或发送失败不停止 gateway 的事件泵）。

复现的缺陷（S-A-06 独立审查 N-1）：两条循环级查询与 ``run_forever()`` 本体此前
没有任何异常隔离，一次普通的瞬时数据库错误就会让后台线程整体退出；``main()``
只在 ``supervisor.run()`` 返回后才 ``join``，不检测线程是否已经提前死亡。用户侧
表现是"发了没反应"——Gateway 照常收消息、照常入队，却一条也投不出去。
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

from lingxi.apps.gateway import (
    combined_heartbeat,
    delivery_thread_watchdog,
    run_delivery_loop,
)
from lingxi.apps.gateway.delivery import LOOP_ALERT_TRACE_ID, DeliveryConsumer


@dataclass
class _FakeTask:
    """``list_pending_delivery_tasks`` 返回的候选任务，只带消费循环真正读到的字段。"""

    task_id: str = "01J00000000000000000000TSK"
    chat_id: str = "chat-1"
    thread_id: str = "topic-1"
    reply_to_message_id: str | None = "reply-1"
    consumed_sequence: int = 0
    card_id: str | None = None
    card_seq: int = 0
    message_id: str | None = None
    fallback_text: bool = False
    # 不是 ``awaiting_delivery``：这一轮不会走到 ``confirm_delivery``，本文件也就
    # 不需要为它准备任何返回值——要断言的是"这个任务被处理到了"，不是投递结果。
    status: str = "running"


@dataclass
class _FakeUncertain:
    task_id: str = "01J00000000000000000000UNC"
    reserved_kind: str = "card_finish"


class _FakeQueue:
    """只实现循环级查询的假队列。

    ``pending_fail_rounds`` / ``uncertain_fail_rounds`` 是"第几轮（1-based）抛异常"
    的集合，空集合表示永远成功；``always_fail_*`` 表示每一轮都抛。抛的是普通
    ``RuntimeError``——模拟语句超时、连接重置、数据库重启这类**瞬时**错误，而不是
    进程崩溃。
    """

    def __init__(
        self,
        *,
        pending_fail_rounds: frozenset[int] = frozenset(),
        uncertain_fail_rounds: frozenset[int] = frozenset(),
        always_fail_pending: bool = False,
        always_fail_uncertain: bool = False,
        pending_tasks: tuple[_FakeTask, ...] = (),
        uncertain_tasks: tuple[_FakeUncertain, ...] = (),
        stop_after_pending_calls: int | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        self._pending_fail_rounds = pending_fail_rounds
        self._uncertain_fail_rounds = uncertain_fail_rounds
        self._always_fail_pending = always_fail_pending
        self._always_fail_uncertain = always_fail_uncertain
        self._pending_tasks = pending_tasks
        self._uncertain_tasks = uncertain_tasks
        self._stop_after_pending_calls = stop_after_pending_calls
        self._stop = stop
        self.pending_calls = 0
        self.uncertain_calls = 0
        self.read_events_for: list[str] = []

    def list_uncertain_delivery_tasks(self, *, limit: int) -> list[_FakeUncertain]:
        del limit
        self.uncertain_calls += 1
        if self._always_fail_uncertain or self.uncertain_calls in self._uncertain_fail_rounds:
            raise RuntimeError("模拟 uncertain 查询的瞬时数据库错误")
        return list(self._uncertain_tasks)

    def list_pending_delivery_tasks(self, *, limit: int) -> list[_FakeTask]:
        del limit
        self.pending_calls += 1
        if (
            self._stop is not None
            and self._stop_after_pending_calls is not None
            and self.pending_calls >= self._stop_after_pending_calls
        ):
            # 让循环自己决定什么时候停：整条断言因此完全确定，不依赖 sleep 或时序。
            self._stop.set()
        if self._always_fail_pending or self.pending_calls in self._pending_fail_rounds:
            raise RuntimeError("模拟候选查询的瞬时数据库错误")
        return list(self._pending_tasks)

    def read_delivery_events(self, *, task_id: str, after_sequence: int) -> list[object]:
        del after_sequence
        self.read_events_for.append(task_id)
        return []


class _SilentCards:
    """本文件的用例都不产生任何外发调用；真被调到就是断言写错了。"""

    def create(self, **kwargs: object) -> object:  # pragma: no cover - 不应被调用
        raise AssertionError("本文件不应发生任何卡片外发调用")

    def update(self, **kwargs: object) -> None:  # pragma: no cover - 不应被调用
        raise AssertionError("本文件不应发生任何卡片外发调用")

    def close(self, **kwargs: object) -> None:  # pragma: no cover - 不应被调用
        raise AssertionError("本文件不应发生任何卡片外发调用")


class _SilentText:
    def send_text(self, **kwargs: object) -> str:  # pragma: no cover - 不应被调用
        raise AssertionError("本文件不应发生任何文本外发调用")


@dataclass
class _RecordingAlerts:
    """记录 ``on_alert`` 的每一次上报；``raise_times`` 让前若干次上报自己抛异常。"""

    raise_times: int = 0
    calls: list[tuple[str, str]] = field(default_factory=list)

    def __call__(self, kind: str, task_id: str) -> None:
        self.calls.append((kind, task_id))
        if self.raise_times > 0:
            self.raise_times -= 1
            raise RuntimeError("模拟告警回调自身失败")

    def kinds(self) -> list[str]:
        return [kind for kind, _task_id in self.calls]


class _ManualMonotonic:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def _consumer(queue: _FakeQueue, alerts: _RecordingAlerts, **kwargs: object) -> DeliveryConsumer:
    return DeliveryConsumer(
        queue=queue,
        cards=_SilentCards(),
        texts=_SilentText(),
        on_alert=alerts,
        **kwargs,  # type: ignore[arg-type]
    )


class LoopLevelQueryIsolationTests(unittest.TestCase):
    """两条循环级查询各自的异常隔离（Issue #191 完成标准第 1 条）。"""

    def test_a_transient_pending_query_failure_does_not_kill_the_loop(self) -> None:
        """审查者复现的那一幕：第 2 轮注入一次 ``RuntimeError``。

        修复前 ``list_pending_delivery_tasks`` 的异常会一路抛穿 ``run_forever``，
        投递线程当场退出、``stop`` 未置位、长连接照常运行。修复后循环必须跑满
        预定轮数并且是因为**收到停机信号**才退出。把 ``run_forever`` 里的
        ``except`` 或 ``run_once`` 里那两处 ``try`` 去掉，本条即变红。
        """

        stop = threading.Event()
        queue = _FakeQueue(
            pending_fail_rounds=frozenset({2}),
            stop_after_pending_calls=5,
            stop=stop,
        )
        alerts = _RecordingAlerts()
        consumer = _consumer(queue, alerts)

        with self.assertLogs("lingxi.apps.gateway.delivery", level="ERROR") as captured:
            consumer.run_forever(stop=stop, poll_interval_seconds=0)

        self.assertTrue(
            any("stage=list_pending" in line for line in captured.output),
            "被吞掉的异常必须留下一条能被搜到的结构化日志，不是静默吸收",
        )
        self.assertEqual(queue.pending_calls, 5, "一次瞬时错误之后循环必须继续轮询")
        self.assertTrue(stop.is_set(), "循环只能因为停机信号退出")
        self.assertEqual(consumer.loop_failures_total, 1)
        self.assertEqual(consumer.consecutive_loop_failures, 0, "后续正常轮次必须清零")
        self.assertEqual(alerts.calls, [], "单次瞬时错误不得惊动任何人")

    def test_a_failing_uncertain_query_still_lets_the_same_round_deliver(self) -> None:
        """两条查询各自隔离：``uncertain`` 查询失败不得连累同一轮的正常候选。

        uncertain 只是"告警哪些任务卡在预留位里"，晚一轮无碍；正常候选才是用户
        真正在等的结果，不能因为前面那条查询坏了就整轮不投。
        """

        queue = _FakeQueue(
            always_fail_uncertain=True,
            pending_tasks=(_FakeTask(task_id="01J00000000000000000000TS1"),),
        )
        alerts = _RecordingAlerts()
        consumer = _consumer(queue, alerts)

        with self.assertLogs("lingxi.apps.gateway.delivery", level="ERROR"):
            processed = consumer.run_once()

        self.assertEqual(processed, 1)
        self.assertEqual(
            queue.read_events_for,
            ["01J00000000000000000000TS1"],
            "uncertain 查询失败时，正常候选必须照常被消费",
        )
        self.assertEqual(consumer.loop_failures_total, 1)

    def test_a_single_task_failure_is_not_counted_as_a_loop_failure(self) -> None:
        """单任务失败有自己的隔离与日志，不得伪装成"整条循环坏了"。"""

        queue = _FakeQueue(pending_tasks=(_FakeTask(),))
        alerts = _RecordingAlerts()
        consumer = _consumer(queue, alerts)
        with patch.object(
            DeliveryConsumer, "_process_task", side_effect=RuntimeError("单任务失败")
        ):
            with self.assertLogs("lingxi.apps.gateway.delivery", level="ERROR"):
                consumer.run_once()

        self.assertEqual(consumer.loop_failures_total, 0)
        self.assertEqual(consumer.consecutive_loop_failures, 0)
        self.assertEqual(alerts.calls, [])


class LoopFailureAlertingTests(unittest.TestCase):
    """连续异常的计数与阈值告警（Issue #191 完成标准第 3 条）。"""

    def test_occasional_failures_never_reach_the_alert_threshold(self) -> None:
        """失败、失败、成功、失败、失败……偶发抖动不得刷屏。"""

        queue = _FakeQueue(pending_fail_rounds=frozenset({1, 2, 4, 5}))
        alerts = _RecordingAlerts()
        consumer = _consumer(queue, alerts)

        with self.assertLogs("lingxi.apps.gateway.delivery", level="ERROR"):
            for _ in range(5):
                consumer.run_once()

        self.assertEqual(consumer.loop_failures_total, 4)
        self.assertEqual(consumer.consecutive_loop_failures, 2, "第 3 轮成功必须清零")
        self.assertEqual(alerts.calls, [], "连续次数没到阈值就不该告警")

    def test_persistent_failures_alert_once_the_threshold_is_reached(self) -> None:
        """持续异常必须被看见：连续到阈值即上报，且带得出是哪一段坏了。

        撤掉 ``_note_loop_failure`` 里的上报（或把它接到别处），本条变红。
        """

        queue = _FakeQueue(always_fail_pending=True)
        alerts = _RecordingAlerts()
        clock = _ManualMonotonic()
        consumer = _consumer(queue, alerts, monotonic=clock)

        with self.assertLogs("lingxi.apps.gateway.delivery", level="ERROR"):
            for _ in range(DeliveryConsumer.DEFAULT_LOOP_FAILURE_ALERT_THRESHOLD - 1):
                consumer.run_once()
            self.assertEqual(alerts.calls, [], "没到阈值之前不得告警")
            consumer.run_once()

        self.assertEqual(
            alerts.calls,
            [("delivery_loop_failed:list_pending", LOOP_ALERT_TRACE_ID)],
            "达到阈值必须上报，且 kind 指出坏的是哪一段循环级查询",
        )

    def test_a_still_broken_loop_reports_again_after_the_throttle_window(self) -> None:
        """ "还在坏"必须持续可见：节流窗口过去之后要再报一次，不是只响一声。"""

        queue = _FakeQueue(always_fail_pending=True)
        alerts = _RecordingAlerts()
        clock = _ManualMonotonic()
        consumer = _consumer(queue, alerts, monotonic=clock)

        with self.assertLogs("lingxi.apps.gateway.delivery", level="ERROR"):
            for _ in range(DeliveryConsumer.DEFAULT_LOOP_FAILURE_ALERT_THRESHOLD + 5):
                consumer.run_once()
            self.assertEqual(len(alerts.calls), 1, "节流窗口内不得重复上报")
            clock.value += DeliveryConsumer.DEFAULT_ALERT_MIN_INTERVAL_SECONDS
            consumer.run_once()

        self.assertEqual(len(alerts.calls), 2)

    def test_an_alert_callback_that_raises_does_not_kill_the_loop(self) -> None:
        """`V-告警-05`：告警自身失败不得带走投递职责。

        ``uncertain`` 任务的上报发生在 ``run_once`` 里、两处查询隔离之外，因此它
        抛出的异常只会被 ``run_forever`` 那一层兜住——这条路径必须存在。
        """

        stop = threading.Event()
        queue = _FakeQueue(
            uncertain_tasks=(_FakeUncertain(),),
            stop_after_pending_calls=4,
            stop=stop,
        )
        alerts = _RecordingAlerts(raise_times=1)
        consumer = _consumer(queue, alerts)

        with self.assertLogs("lingxi.apps.gateway.delivery", level="ERROR"):
            consumer.run_forever(stop=stop, poll_interval_seconds=0)

        self.assertEqual(queue.pending_calls, 4, "告警回调抛异常之后循环必须继续")
        self.assertEqual(consumer.loop_failures_total, 1)


class DeliveryThreadDeathIsReportedTests(unittest.TestCase):
    """线程真的死了时 ``main()`` 必须知道（Issue #191 完成标准第 2 条）。"""

    class _Consumer:
        def __init__(self, *, error: BaseException | None = None) -> None:
            self._error = error
            self.calls = 0

        def run_forever(self, **kwargs: object) -> None:
            self.calls += 1
            if self._error is not None:
                raise self._error

    def _run(self, consumer: object, *, stop: threading.Event) -> list[str]:
        causes: list[str] = []
        run_delivery_loop(
            consumer,
            stop=stop,
            poll_interval_seconds=0,
            on_dead=causes.append,
        )
        return causes

    def test_an_exception_escaping_the_loop_is_reported_before_the_thread_dies(self) -> None:
        stop = threading.Event()
        causes: list[str] = []

        with self.assertRaises(RuntimeError):
            run_delivery_loop(
                self._Consumer(error=RuntimeError("循环彻底崩了")),
                stop=stop,
                poll_interval_seconds=0,
                on_dead=causes.append,
            )

        self.assertEqual(causes, ["RuntimeError"])

    def test_a_base_exception_is_reported_too(self) -> None:
        """解释器级错误（``MemoryError`` 一类）不是 ``Exception``，只捕获
        ``Exception`` 会让这条最严重的死法恰好漏掉。"""

        stop = threading.Event()
        causes: list[str] = []

        with self.assertRaises(MemoryError):
            run_delivery_loop(
                self._Consumer(error=MemoryError()),
                stop=stop,
                poll_interval_seconds=0,
                on_dead=causes.append,
            )

        self.assertEqual(causes, ["MemoryError"])

    def test_returning_without_a_stop_signal_is_reported(self) -> None:
        """没收到停机信号却返回了：循环没了，进程还在——同样必须上报。"""

        stop = threading.Event()

        causes = self._run(self._Consumer(), stop=stop)

        self.assertEqual(causes, ["returned_without_stop"])

    def test_a_graceful_stop_is_not_reported(self) -> None:
        """优雅停机是预期退出，不得产生告警噪音。"""

        stop = threading.Event()
        stop.set()

        causes = self._run(self._Consumer(), stop=stop)

        self.assertEqual(causes, [])


class DeliveryThreadWatchdogTests(unittest.TestCase):
    """长连接主线程侧的看门狗：``main()`` 阻塞在 ``supervisor.run()`` 时唯一的
    发现机会（Issue #191）。"""

    def _dead_thread(self) -> threading.Thread:
        thread = threading.Thread(target=lambda: None)
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        return thread

    def test_a_dead_thread_is_reported_exactly_once(self) -> None:
        causes: list[str] = []
        watchdog = delivery_thread_watchdog(
            self._dead_thread(), stop=threading.Event(), on_dead=causes.append
        )

        watchdog()
        watchdog()
        watchdog()

        self.assertEqual(causes, ["thread_not_alive"], "不可逆状态只报一次，不刷屏")

    def test_a_thread_that_has_not_started_yet_is_not_reported(self) -> None:
        """启动竞态：还没 ``start()`` 的线程 ``is_alive()`` 也是假，不能误判成死亡。"""

        causes: list[str] = []
        watchdog = delivery_thread_watchdog(
            threading.Thread(target=lambda: None), stop=threading.Event(), on_dead=causes.append
        )

        watchdog()

        self.assertEqual(causes, [])

    def test_a_dead_thread_during_shutdown_is_not_reported(self) -> None:
        stop = threading.Event()
        stop.set()
        causes: list[str] = []
        watchdog = delivery_thread_watchdog(self._dead_thread(), stop=stop, on_dead=causes.append)

        watchdog()

        self.assertEqual(causes, [])

    def test_a_live_thread_is_not_reported(self) -> None:
        release = threading.Event()
        thread = threading.Thread(target=release.wait)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(release.set)
        causes: list[str] = []
        watchdog = delivery_thread_watchdog(thread, stop=threading.Event(), on_dead=causes.append)

        watchdog()

        self.assertEqual(causes, [])


class LongConnectionHeartbeatRunsTheWatchdogTests(unittest.TestCase):
    """看门狗真的挂在长连接的心跳上——否则它只是一个没人调用的函数。"""

    class _FakeDuty:
        def __init__(self) -> None:
            self.beats = 0

        def heartbeat_callback(self, component: str):
            del component

            def beat() -> None:
                self.beats += 1

            return beat

    def test_the_longconn_heartbeat_beats_touches_liveness_and_checks_the_thread(
        self,
    ) -> None:
        duty = self._FakeDuty()
        checks: list[int] = []
        with tempfile.TemporaryDirectory(prefix="issue191-liveness-") as directory:
            with patch.dict(os.environ, {"LINGXI_LIVENESS_DIR": directory}):
                heartbeat = combined_heartbeat(
                    duty, "gateway-longconn", watchdog=lambda: checks.append(1)
                )
                heartbeat()

                self.assertTrue(
                    (Path(directory) / "lingxi-gateway-longconn-liveness").exists(),
                    "看门狗不得挤掉既有的活性文件写入",
                )
        self.assertEqual(duty.beats, 1)
        self.assertEqual(checks, [1], "长连接每一轮心跳都要顺手确认投递线程还活着")

    def test_without_a_watchdog_the_heartbeat_behaves_exactly_as_before(self) -> None:
        duty = self._FakeDuty()
        with tempfile.TemporaryDirectory(prefix="issue191-liveness-") as directory:
            with patch.dict(os.environ, {"LINGXI_LIVENESS_DIR": directory}):
                combined_heartbeat(duty, "gateway-delivery")()

                self.assertTrue((Path(directory) / "lingxi-gateway-delivery-liveness").exists())
        self.assertEqual(duty.beats, 1)


if __name__ == "__main__":
    unittest.main()
