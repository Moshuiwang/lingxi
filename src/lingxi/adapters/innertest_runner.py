"""预开通复用已有首聊执行池，扩员不额外增加执行并发。"""

import threading
from types import SimpleNamespace


class SharedOnboardingRunner:
    """持久阶段等待本次已提交工作，既有执行池继续拥有线程与容量。"""

    def __init__(self, *, runner, executor, should_stop):
        """只注入既有对象，不启动第二执行器或长期内存队列。"""
        self.runner, self.executor, self.should_stop = runner, executor, should_stop

    def start_system(self, *, email, trace_id, initiated_by_open_id):
        """提交失败保留等待；停止不延长进程的共同排空预算。"""
        finished = threading.Event()
        outcome = []

        def execute():
            try:
                outcome.append(
                    self.runner.start_system(
                        email=email, trace_id=trace_id, initiated_by_open_id=initiated_by_open_id
                    )
                )
            except Exception:
                outcome.append(SimpleNamespace(failure_reason="provisioning_failed"))
            finally:
                finished.set()

        if not self.executor.submit(execute):
            return SimpleNamespace(failure_reason="capacity_pending")
        while not finished.wait(0.05):
            if self.should_stop():
                return SimpleNamespace(failure_reason="stopping")
        return outcome[0]
