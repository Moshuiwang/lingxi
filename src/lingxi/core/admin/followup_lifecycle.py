"""后台对象登记及统一绝对停止预算，包括后续接入的 scheduler listener。"""

from __future__ import annotations

import threading
import time

from lingxi.core.admin.followup import ShutdownReport


class BackgroundLifecycle:
    """先停止全部接收，再用同一截止时刻等待所有对象。"""

    def __init__(self, *, budget_seconds, clock=time.monotonic):
        """仅保存引用，不创建进程、线程或数据库连接。"""
        self.budget_seconds = budget_seconds
        self.clock = clock
        self.deadline = None
        self._objects = []
        self._lock = threading.RLock()

    def register(self, component):
        """登记 listener 或消费者，停止后不再允许新增。"""
        with self._lock:
            if self.deadline is not None:
                raise RuntimeError("服务正在停止，不能新增后台职责")
            if component not in self._objects:
                self._objects.append(component)
        return component

    def request_stop(self):
        """重复停止保留第一次截止，先关闭所有对象接收。"""
        with self._lock:
            if self.deadline is None:
                self.deadline = self.clock() + self.budget_seconds
            objects = tuple(self._objects)
        for component in objects:
            component.request_stop()

    def drain_until(self, deadline_monotonic=None):
        """一个对象花完预算后，后续对象只能立即报告仍在途。"""
        self.request_stop()
        deadline = self.deadline
        if deadline_monotonic is not None:
            deadline = min(deadline, deadline_monotonic)
        totals = [0] * 5
        for component in tuple(self._objects):
            report = component.drain_until(deadline)
            for index, key in enumerate(
                ("accepted", "finished", "recoverable", "unknown", "still_running")
            ):
                totals[index] += getattr(report, key)
        return ShutdownReport(*totals)
