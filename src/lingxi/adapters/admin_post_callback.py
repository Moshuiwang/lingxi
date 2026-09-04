"""管理卡回调应答之后那批后处理的后台执行器。

飞书的回调应答窗口是秒级的，但确认成功后要做的四件事（换终态卡、发管理群
通知、刷新原管理卡、入队定向重算）全是网络往返，可能超出应答窗口——本执行
器把它们搬到应答发出之后异步做；执行本身只发生一次（卡片状态 CAS 挡住重复
执行），这里只解决体验上不超时。

与 ``BackgroundPermissionRecomputeTrigger`` 形态相同、职责不同：那个只包定向
重算并带完成/超时回调；本类不认识业务语义，只负责排队、按次序执行、失败
记审计——不合并，避免任一侧职责变模糊。
单线程按次序执行：四件事的相对次序有意义（原管理卡必须先推进成「下发中」
再入队重算，否则重算完成回调可能被随后到达的「下发中」盖回去）。有界队列、
满了拒绝而非丢弃：``submit`` 在队列满时返回 ``False``，调用方须原地同步
执行——丢一次是用户可见的结果丢失，比一次应答超时更严重。
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from typing import Protocol


class AuditSink(Protocol):
    def record(self, action: str, /, **fields: object) -> None: ...


#: 队列容量上下界（模块文档「有界队列」一节）。
_MIN_QUEUE_MAXSIZE = 1
_MAX_QUEUE_MAXSIZE = 32
_DEFAULT_QUEUE_MAXSIZE = 8

#: 任务本身抛异常时的审计动作名。任务内部各步骤本来就各自 best-effort 捕获，
#: 走到这里说明捕获之外还漏了一种——记下来，但绝不让它带走 worker 线程。
POST_CALLBACK_TASK_FAILED_ACTION = "admin.card_callback.post_callback_task_failed"


class BackgroundPostCallbackExecutor:
    """``core/admin/card_callback.PostCallbackExecutor`` 端口的真实实现。"""

    def __init__(
        self,
        *,
        audit: AuditSink,
        queue_maxsize: int = _DEFAULT_QUEUE_MAXSIZE,
        thread_name: str = "lingxi-gateway-admin-post-callback",
    ) -> None:
        if isinstance(queue_maxsize, bool) or not isinstance(queue_maxsize, int):
            raise ValueError("queue_maxsize 必须是整数")
        if not _MIN_QUEUE_MAXSIZE <= queue_maxsize <= _MAX_QUEUE_MAXSIZE:
            raise ValueError(f"queue_maxsize 必须在 {_MIN_QUEUE_MAXSIZE}~{_MAX_QUEUE_MAXSIZE} 之间")
        self._audit = audit
        self._queue: queue.Queue[Callable[[], None]] = queue.Queue(maxsize=queue_maxsize)
        self._worker = threading.Thread(
            target=self._run,
            name=thread_name,
            # daemon：这批后处理全部是 best-effort 的视觉/通知面，进程退出时没有
            # 必须先跑完的持久化承诺（业务结果早已在确认事务里落库）。
            daemon=True,
        )
        self._worker.start()

    def submit(self, task: Callable[[], None]) -> bool:
        """立即返回。排进队列返回 ``True``；队列已满返回 ``False``（调用方同步执行）。"""
        try:
            self._queue.put_nowait(task)
        except queue.Full:
            return False
        return True

    def _run(self) -> None:
        while True:
            task = self._queue.get()
            try:
                task()
            except Exception as error:  # 单个任务失败不得带走 worker
                try:
                    self._audit.record(POST_CALLBACK_TASK_FAILED_ACTION, error=type(error).__name__)
                except Exception:  # 审计器自身故障同样不得带走 worker
                    pass
            finally:
                self._queue.task_done()


__all__ = ["BackgroundPostCallbackExecutor", "POST_CALLBACK_TASK_FAILED_ACTION"]
