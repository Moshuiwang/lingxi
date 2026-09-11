"""Issue #593 完成标准 2：worker 的 ``task_queued`` 监听连接被服务端掐断时，进程不退出。

改前 ``WorkerService.run()`` 把 ``listener.wait`` 抛出的 OperationalError 原样冲出，
整个 worker 进程退出、靠容器重启复活（2026-09-04 在未改代码的 main 上用
``pg_terminate_backend`` 实测复现）。改后：监听断开 → 丢掉这条连接、重建监听；
重建失败 → 退回纯轮询一个周期再试。

Issue #683 起，「``process_once()`` 的异常仍原样向上抛」这条边界收窄：只有
依赖暂时不可用（数据库连不上一类）才降级继续，编码缺陷仍然原样上抛并退出——
见下面 ``HousekeepingAndClaimDependencyRecoveryTests`` 与被显式改写的
``test_a_coding_defect_in_claim_still_propagates_and_exits``。
"""

from __future__ import annotations

import asyncio
import unittest

from test_worker_queue_consumer import FakeWorkerQueue, dependency_unavailable_error, worker_config

from lingxi.apps.worker.service import WorkerService


class _CountingQueue(FakeWorkerQueue):
    def __init__(self) -> None:
        super().__init__()
        self.claimed = None  # type: ignore[assignment]  # 空队列：只观察循环本身
        self.claim_calls = 0

    def claim(self, **kwargs: object) -> list:  # type: ignore[override]
        self.claim_calls += 1
        return []


class _FlakyListener:
    """``wait`` 先失败 ``failing_waits`` 次（模拟服务端掐断 LISTEN 连接），之后正常。"""

    def __init__(self, *, failing_waits: int) -> None:
        self.failing_waits = failing_waits
        self.wait_calls = 0
        self.exit_calls = 0

    def __enter__(self) -> _FlakyListener:
        return self

    def __exit__(self, *_args: object) -> None:
        self.exit_calls += 1

    def wait(self, *, timeout_seconds: float) -> bool:
        self.wait_calls += 1
        if self.wait_calls <= self.failing_waits:
            raise OSError("consuming input failed: server closed the connection unexpectedly")
        return False


def _run_until(service: WorkerService, condition, *, timeout_seconds: float = 5.0) -> None:
    async def scenario() -> None:
        stop_event = asyncio.Event()
        consumer = asyncio.create_task(service.run(stop_event=stop_event))
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while not condition():
            if consumer.done():
                consumer.result()  # 让 run() 的异常原样冒出来
                raise AssertionError("消费者提前退出")
            if asyncio.get_running_loop().time() > deadline:
                stop_event.set()
                await consumer
                raise AssertionError("超时仍未满足条件")
            await asyncio.sleep(0.005)
        stop_event.set()
        await asyncio.wait_for(consumer, timeout=2.0)

    asyncio.run(scenario())


class ListenerDisconnectTests(unittest.TestCase):
    def test_a_broken_listener_is_rebuilt_and_the_process_keeps_claiming(self) -> None:
        queue = _CountingQueue()
        listeners: list[_FlakyListener] = []

        def factory() -> _FlakyListener:
            listener = _FlakyListener(failing_waits=1 if len(listeners) < 2 else 0)
            listeners.append(listener)
            return listener

        service = WorkerService(
            config=worker_config(poll_interval_seconds=0.01, max_concurrency=1),
            queue=queue,
            listener_factory=factory,
        )
        _run_until(service, lambda: len(listeners) >= 3 and listeners[2].wait_calls >= 2)

        self.assertGreaterEqual(len(listeners), 3, "前两个监听各断一次，必须各重建一次")
        self.assertEqual(
            [listener.exit_calls for listener in listeners[:2]], [1, 1], "断掉的监听要被关掉"
        )
        self.assertGreaterEqual(queue.claim_calls, 3, "每次重建前后都照常领取")

    def test_when_the_listener_cannot_be_established_the_loop_polls_and_retries(self) -> None:
        queue = _CountingQueue()
        attempts: list[int] = []
        listeners: list[_FlakyListener] = []

        def factory() -> _FlakyListener:
            attempts.append(1)
            if len(attempts) <= 2:
                raise OSError("connection failed")
            listener = _FlakyListener(failing_waits=0)
            listeners.append(listener)
            return listener

        service = WorkerService(
            config=worker_config(poll_interval_seconds=0.01, max_concurrency=1),
            queue=queue,
            listener_factory=factory,
        )
        _run_until(service, lambda: listeners and listeners[0].wait_calls >= 2)

        self.assertEqual(len(attempts), 3)
        self.assertGreaterEqual(queue.claim_calls, 3, "监听建不起来的两轮也要领取任务")

    def test_a_coding_defect_in_claim_still_propagates_and_exits(self) -> None:
        """Issue #683 收敛后的边界：只有依赖暂时不可用才降级，编码缺陷仍然致命。

        这是 #593 原有 ``test_process_once_failures_still_propagate`` 的**显式
        改写，不是删除**：旧版断言"``claim()`` 抛任何异常都必须让 ``run()``
        退出"，那条钉住的语义在 #683 之后不再整体成立——依赖类异常现在必须
        降级（见 ``HousekeepingAndClaimDependencyRecoveryTests``）。这里改钉住
        更窄但仍然成立的那一半：``TypeError`` 这类显然不是"数据库连不上"的
        编码缺陷，必须原样冲出并让进程退出，不能被新的隔离逻辑一并吞掉。
        """

        class _BrokenQueue(_CountingQueue):
            def claim(self, **kwargs: object) -> list:  # type: ignore[override]
                raise TypeError("claim() 参数用错——这是代码缺陷，不是数据库问题")

        listener = _FlakyListener(failing_waits=0)
        service = WorkerService(
            config=worker_config(poll_interval_seconds=0.01, max_concurrency=1),
            queue=_BrokenQueue(),
            listener_factory=lambda: listener,
        )
        with self.assertRaises(TypeError):
            asyncio.run(service.run(stop_event=asyncio.Event()))
        self.assertEqual(listener.exit_calls, 1, "异常冲出时监听也要被关掉")


class HousekeepingAndClaimDependencyRecoveryTests(unittest.TestCase):
    """Issue #683 验收 ①②③⑤⑦：依赖暂时不可用时消费循环降级继续，不退出。"""

    def test_housekeeping_dependency_unavailable_keeps_the_loop_running(self) -> None:
        class _HousekeepingFailsQueue(_CountingQueue):
            def fail_unavailable_versions(self, **kwargs: object) -> list:
                raise dependency_unavailable_error()

        queue = _HousekeepingFailsQueue()
        service = WorkerService(
            config=worker_config(poll_interval_seconds=0.01, max_concurrency=1),
            queue=queue,
            listener_factory=None,
        )
        _run_until(service, lambda: queue.claim_calls >= 3)

        self.assertGreaterEqual(
            queue.claim_calls, 3, "巡检持续依赖不可用不应该拖累领取，领取要照常被调用"
        )

    def test_listener_and_housekeeping_failing_together_does_not_exit(self) -> None:
        """本次生产故障的实际路径：监听建立失败，退回轮询后巡检也失败。"""

        class _HousekeepingFailsQueue(_CountingQueue):
            def fail_unavailable_versions(self, **kwargs: object) -> list:
                raise dependency_unavailable_error()

        def always_failing_listener_factory() -> _FlakyListener:
            raise OSError("监听建立失败——数据库不可达")

        queue = _HousekeepingFailsQueue()
        service = WorkerService(
            config=worker_config(poll_interval_seconds=0.01, max_concurrency=1),
            queue=queue,
            listener_factory=always_failing_listener_factory,
        )
        _run_until(service, lambda: queue.claim_calls >= 3)

        self.assertGreaterEqual(
            queue.claim_calls, 3, "监听建不起来＋巡检失败同时发生时，循环仍要继续领取"
        )

    def test_claim_dependency_unavailable_keeps_the_loop_running(self) -> None:
        class _ClaimDependencyUnavailableQueue(_CountingQueue):
            def claim(self, **kwargs: object) -> list:  # type: ignore[override]
                self.claim_calls += 1
                raise dependency_unavailable_error()

        queue = _ClaimDependencyUnavailableQueue()
        service = WorkerService(
            config=worker_config(poll_interval_seconds=0.01, max_concurrency=1),
            queue=queue,
            listener_factory=None,
        )
        _run_until(service, lambda: queue.claim_calls >= 3)

        self.assertGreaterEqual(queue.claim_calls, 3, "领取持续依赖不可用，循环仍要反复重试")

    def test_process_once_entry_housekeeping_dependency_unavailable_does_not_raise(
        self,
    ) -> None:
        """补点甲：``process_once`` 入口那次巡检（在 ``_run_rolling_claim_loop``
        之外）单独失败也不应该向上抛出——直接调用一次 ``process_once()``，
        不经过 ``run()`` 的长期循环，精确钉住这一个调用点。
        """

        class _HousekeepingFailsQueue(_CountingQueue):
            def fail_unavailable_versions(self, **kwargs: object) -> list:
                raise dependency_unavailable_error()

        queue = _HousekeepingFailsQueue()
        service = WorkerService(
            config=worker_config(poll_interval_seconds=0.01, max_concurrency=1),
            queue=queue,
        )

        result = asyncio.run(service.process_once())

        self.assertFalse(result, "空队列且巡检降级时，这一轮没有观察到任何任务")
        self.assertEqual(queue.claim_calls, 1, "巡检失败不应该连累领取都不执行")

    def test_housekeeping_coding_defect_still_propagates_and_exits(self) -> None:
        """否定断言：巡检抛的如果是编码缺陷（``TypeError``），新隔离逻辑不能把它
        也吞掉——防止这次修复退化成"巡检这一步什么异常都不放过"的新默认。
        """

        class _HousekeepingCodingDefectQueue(_CountingQueue):
            def fail_unavailable_versions(self, **kwargs: object) -> list:
                raise TypeError("巡检参数用错——这是代码缺陷，不是数据库问题")

        queue = _HousekeepingCodingDefectQueue()
        service = WorkerService(
            config=worker_config(poll_interval_seconds=0.01, max_concurrency=1),
            queue=queue,
            listener_factory=None,
        )
        with self.assertRaises(TypeError):
            asyncio.run(service.run(stop_event=asyncio.Event()))
