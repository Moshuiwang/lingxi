"""Scheduler 全部后台职责共用 120 秒，listener 在同一 Owner 下登记。"""

from __future__ import annotations

import time

from lingxi.core.admin.followup import ShutdownReport


class DutyLifecycle:
    """适配既有开通线程池、组织快照和日报线程，不重写它们的业务。"""

    def __init__(self, duty):
        """只保存已装配职责，线程仍由原职责负责。"""
        self.duty = duty

    def request_stop(self):
        """先禁止领取；续期的凭据保存仍由原执行路径完成。"""
        executor = getattr(self.duty, "onboarding_executor", None)
        if executor is not None:
            executor.stop()
        stop = getattr(self.duty, "_stop", None)
        if stop is not None:
            stop.set()

    def drain_until(self, deadline_monotonic):
        """所有等待使用剩余预算，不能逐对象重置 120 秒。"""
        self.request_stop()
        running = 0
        executor = getattr(self.duty, "onboarding_executor", None)
        if executor is not None:
            executor.join(timeout=max(0, deadline_monotonic - time.monotonic()))
            running += int(executor.alive)
        thread = getattr(self.duty, "_pending_thread", None)
        if thread is not None:
            thread.join(timeout=max(0, deadline_monotonic - time.monotonic()))
            running += int(thread.is_alive())
        return ShutdownReport(recoverable=running, still_running=running)
