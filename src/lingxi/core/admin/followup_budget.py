"""进程内新增活动数据库操作的可重入两槽预算。"""

from __future__ import annotations

import threading


class FollowupDatabaseBudget:
    """同一处理器中的短事务复用已占槽，避免嵌套借槽死锁。"""

    def __init__(self):
        """固定上限为二，listener 另行保证自己至多占一。"""
        self._slots = threading.BoundedSemaphore(2)
        self._local = threading.local()

    def acquire(self, blocking=True, timeout=None):
        """调用者可无等待申请；已持槽的同线程不再次占用。"""
        depth = getattr(self._local, "depth", 0)
        if depth or self._slots.acquire(blocking=blocking, timeout=timeout):
            self._local.depth = depth + 1
            return True
        return False

    def release(self):
        """只在最外层短业务退出时释放进程槽。"""
        depth = getattr(self._local, "depth", 0)
        if depth <= 0:
            raise RuntimeError("数据库预算未被当前线程持有")
        self._local.depth = depth - 1
        if depth == 1:
            self._slots.release()

    def __enter__(self):
        """借用一个活动槽，不开启数据库事务。"""
        self.acquire()
        return self

    def __exit__(self, *_args):
        """异常同样归还活动槽。"""
        self.release()
