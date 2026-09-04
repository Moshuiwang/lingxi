"""Issue #593 完成标准 2：worker 的 ``task_queued`` 监听连接被服务端掐断时，进程不退出。

改前 ``WorkerService.run()`` 把 ``listener.wait`` 抛出的 OperationalError 原样冲出，
整个 worker 进程退出、靠容器重启复活（2026-09-04 在未改代码的 main 上用
``pg_terminate_backend`` 实测复现）。改后：监听断开 → 丢掉这条连接、重建监听；
重建失败 → 退回纯轮询一个周期再试；``process_once()`` 的异常仍原样向上抛。
"""

from __future__ import annotations

import asyncio
import unittest

from test_worker_queue_consumer import FakeWorkerQueue, worker_config

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

    def test_process_once_failures_still_propagate(self) -> None:
        class _BrokenQueue(_CountingQueue):
            def claim(self, **kwargs: object) -> list:  # type: ignore[override]
                raise RuntimeError("数据库不可达")

        listener = _FlakyListener(failing_waits=0)
        service = WorkerService(
            config=worker_config(poll_interval_seconds=0.01, max_concurrency=1),
            queue=_BrokenQueue(),
            listener_factory=lambda: listener,
        )
        with self.assertRaises(RuntimeError):
            asyncio.run(service.run(stop_event=asyncio.Event()))
        self.assertEqual(listener.exit_calls, 1, "异常冲出时监听也要被关掉")
