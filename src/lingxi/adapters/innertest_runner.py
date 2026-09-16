"""预开通复用已有首聊执行池，扩员不额外增加执行并发。"""

import threading
from types import SimpleNamespace


class SharedOnboardingRunner:
    """持久阶段等待本次已提交工作，既有执行池继续拥有线程与容量。"""

    def __init__(self, *, runner, executor, should_stop):
        """只注入既有对象，不启动第二执行器或长期内存队列。"""
        self.runner, self.executor, self.should_stop = runner, executor, should_stop

    @classmethod
    def from_duties(cls, duties, *, should_stop):
        """只接受唯一既有开通 Owner，缺失或重复均拒绝装配。"""
        owners = [d for d in duties if hasattr(d, "onboarding_runner")]
        if len(owners) != 1:
            raise ValueError("内测后台开通入口未就绪")
        return cls(
            runner=owners[0].onboarding_runner,
            executor=owners[0].onboarding_executor,
            should_stop=should_stop,
        )

    def start_system(self, *, email, trace_id, initiated_by_open_id, expected_open_id=None):
        """开通必须沿用本批已确定的主体，不让重试按邮箱换人。"""
        kwargs = dict(email=email, trace_id=trace_id, initiated_by_open_id=initiated_by_open_id)
        if expected_open_id is not None:
            kwargs["expected_open_id"] = expected_open_id
        return self._submit(lambda: self.runner.start_system(**kwargs))

    def resolve_system_email(self, *, email, trace_id):
        """实时解析也复用同一执行池，不新增凭据消费者或并发额度。"""
        return self._submit(
            lambda: self.runner.resolve_system_email(email=email, trace_id=trace_id)
        )

    def _submit(self, call):
        """提交失败保留等待；停止不延长进程的共同排空预算。"""
        finished = threading.Event()
        outcome = []

        def execute():
            try:
                outcome.append(call())
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
