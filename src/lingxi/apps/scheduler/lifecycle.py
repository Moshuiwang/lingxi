"""Scheduler 全部后台职责共用 120 秒，listener 在同一 Owner 下登记。"""

from __future__ import annotations

import threading
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


class SignalStopEvent(threading.Event):
    """信号处理只写停止事实，等待者定期观察，避免重入普通锁。"""

    def __init__(self):
        """构造不安装信号处理器。"""
        super().__init__()
        self.requested_at = None

    def signal_stop(self):
        """信号内不碰条件锁、日志或资源停止接口。"""
        if self.requested_at is None:
            self.requested_at = time.monotonic()

    def is_set(self):
        """全部领取者共享同一停止事实。"""
        return self.requested_at is not None or super().is_set()

    def wait(self, timeout=None):
        """信号内不能通知条件变量，因此最多一百毫秒重新观察。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            remaining = 0.1 if deadline is None else min(0.1, deadline - time.monotonic())
            if remaining <= 0:
                break
            super().wait(remaining)
        return self.is_set()
