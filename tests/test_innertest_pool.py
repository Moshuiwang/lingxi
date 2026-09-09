"""扩员复用已有首聊线程池，容量满时持久工作继续等待。"""

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from lingxi.adapters.innertest_runner import SharedOnboardingRunner
from lingxi.apps.scheduler.onboarding import OnboardingExecutor


class SharedPoolTests(unittest.TestCase):
    def test_system_path_uses_existing_pool_and_zero_extra_workers(self):
        stop = threading.Event()
        entered = threading.Event()
        release = threading.Event()
        executor = OnboardingExecutor(workers=1, backlog=1, should_stop=stop.is_set)
        executor.start()
        calls = []

        def first_chat():
            entered.set()
            release.wait(3)

        self.assertTrue(executor.submit(first_chat))
        self.assertTrue(entered.wait(2))
        runner = Mock()
        runner.start_system.side_effect = lambda **kw: (
            calls.append((threading.current_thread().name, kw))
            or SimpleNamespace(failure_reason=None)
        )
        pooled = SharedOnboardingRunner(runner=runner, executor=executor, should_stop=stop.is_set)
        result = []
        request = threading.Thread(
            target=lambda: result.append(
                pooled.start_system(
                    email="x@example.test",
                    trace_id="trc_synthetic",
                    initiated_by_open_id="ou_synthetic",
                )
            )
        )
        try:
            request.start()
            release.set()
            request.join(3)
            self.assertFalse(request.is_alive())
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0][0].startswith("lingxi-gateway-onboarding-"))
            self.assertNotIn("preprovision_grant", calls[0][1])
        finally:
            stop.set()
            release.set()
            executor.stop()
            executor.join(3)
            request.join(3)
            self.assertFalse(executor.alive)

    def test_full_pool_and_stop_return_waiting_without_new_thread(self):
        executor = Mock()
        executor.submit.return_value = False
        runner = Mock()
        result = SharedOnboardingRunner(
            runner=runner, executor=executor, should_stop=lambda: False
        ).start_system(email="x@example.test", trace_id="t", initiated_by_open_id="o")
        self.assertEqual(result.failure_reason, "capacity_pending")
        runner.start_system.assert_not_called()
