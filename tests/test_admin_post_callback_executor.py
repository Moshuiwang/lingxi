"""管理卡回调后处理执行器的断言（#493 块 B，Trace #544）。

这个执行器存在的唯一理由是"回调应答不该等网络往返"：一次 387 项的补充授权确认，
同步后处理约 4 秒，超出飞书回调应答窗口——管理员看到「回调服务超时未响应」、按钮
重新点亮，于是再点一次。因此本文件钉的是三件事：``submit`` 立即返回、任务真的在
后台按提交次序跑完、队列满时如实回 ``False``（让调用方原地同步做，而不是丢掉）。
"""

from __future__ import annotations

import threading
import unittest

from lingxi.adapters.admin_post_callback import (
    POST_CALLBACK_TASK_FAILED_ACTION,
    BackgroundPostCallbackExecutor,
)


class _RecordingAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, fields))


class BackgroundPostCallbackExecutorTests(unittest.TestCase):
    def test_submit_returns_immediately_and_the_task_runs_in_the_background(self) -> None:
        audit = _RecordingAudit()
        executor = BackgroundPostCallbackExecutor(audit=audit)
        done = threading.Event()

        self.assertTrue(executor.submit(done.set))

        self.assertTrue(done.wait(timeout=5), "任务必须真的在后台跑完")

    def test_submit_does_not_run_the_task_on_the_calling_thread(self) -> None:
        """否定断言：如果 ``submit`` 顺手把任务跑了，这个端口就没有意义。"""

        audit = _RecordingAudit()
        executor = BackgroundPostCallbackExecutor(audit=audit)
        gate = threading.Event()
        started = threading.Event()
        finished = threading.Event()

        def blocked() -> None:
            started.set()
            gate.wait(timeout=5)
            finished.set()

        self.assertTrue(executor.submit(blocked))
        self.assertTrue(started.wait(timeout=5))
        self.assertFalse(finished.is_set(), "submit 不得在调用线程里等任务跑完")
        gate.set()
        self.assertTrue(finished.wait(timeout=5))

    def test_tasks_run_in_submission_order(self) -> None:
        """次序有意义：原管理卡必须先被推进成「下发中」再入队重算。"""

        audit = _RecordingAudit()
        executor = BackgroundPostCallbackExecutor(audit=audit, queue_maxsize=8)
        order: list[int] = []
        done = threading.Event()

        for index in range(5):
            executor.submit(lambda index=index: order.append(index))
        executor.submit(done.set)

        self.assertTrue(done.wait(timeout=5))
        self.assertEqual(order, [0, 1, 2, 3, 4])

    def test_a_full_queue_reports_false_instead_of_dropping(self) -> None:
        audit = _RecordingAudit()
        executor = BackgroundPostCallbackExecutor(audit=audit, queue_maxsize=1)
        gate = threading.Event()
        started = threading.Event()

        def blocked() -> None:
            started.set()
            gate.wait(timeout=5)

        self.assertTrue(executor.submit(blocked))
        self.assertTrue(started.wait(timeout=5), "第一个任务已被取走，队列这时是空的")
        self.assertTrue(executor.submit(lambda: None), "填满这一个槽位")
        self.assertFalse(executor.submit(lambda: None), "满了必须如实回 False")
        gate.set()

    def test_a_failing_task_does_not_kill_the_worker(self) -> None:
        audit = _RecordingAudit()
        executor = BackgroundPostCallbackExecutor(audit=audit)
        done = threading.Event()

        def boom() -> None:
            raise RuntimeError("boom")

        executor.submit(boom)
        executor.submit(done.set)

        self.assertTrue(done.wait(timeout=5), "前一个任务抛异常不得带走 worker")
        self.assertIn(
            POST_CALLBACK_TASK_FAILED_ACTION, [action for action, _ in audit.records]
        )

    def test_a_failing_audit_sink_does_not_kill_the_worker_either(self) -> None:
        class ExplodingAudit:
            def record(self, action: str, /, **fields: object) -> None:
                raise RuntimeError("audit down")

        executor = BackgroundPostCallbackExecutor(audit=ExplodingAudit())
        done = threading.Event()

        executor.submit(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        executor.submit(done.set)

        self.assertTrue(done.wait(timeout=5))

    def test_queue_size_is_validated(self) -> None:
        audit = _RecordingAudit()
        for bad in (0, -1, 33, True):
            with self.subTest(queue_maxsize=bad):
                with self.assertRaises(ValueError):
                    BackgroundPostCallbackExecutor(audit=audit, queue_maxsize=bad)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
