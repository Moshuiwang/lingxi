"""管理卡回调应答之后那批后处理的后台执行器（#493 块 B）。

``core/admin/card_callback.AdminCardCallbackHandler.handle()`` 的返回值就是飞书要的
卡片回调应答帧，飞书的应答窗口是秒级的。确认成功之后要做的四件事全是网络往返——出带外
把确认卡换成终态卡、发管理群通知、刷新原管理卡（还要先读一次目标用户状态）、入队定向
重算。一次 43 家公司 × 9 指标（387 项）的补充授权实测约 4 秒同步后处理，超出应答窗口：
管理员看到「回调服务超时未响应」，「确认执行」按钮重新点亮，于是再点一次。执行本身只
发生一次（迁移 ``0084`` 的卡片状态 CAS 挡住重复执行），但"数据没坏"不等于"体验可以
接受"——这个执行器把那四件事搬到应答发出之后。

## 与 ``BackgroundPermissionRecomputeTrigger`` 的关系

两者形态相同、职责不同：那一个只包定向重算这一个动作并带完成/超时回调；本类是一个
**不认识任何业务语义**的通用单线程串行执行器，只负责"排队、按提交次序执行、失败记审计"。
不合并成一个：合并要么让通用执行器认识 ``PendingAction``，要么让重算执行器接受任意
callable 并丢掉它的完成回调，两种都会让某一侧变模糊。

## 为什么是单线程、按次序

那四件事的**相对次序有意义**：原管理卡必须先被推进成「下发中」再入队重算，否则重算
完成回调刷出的「已生效」会被随后到达的「下发中」盖回去。同一条队列、单个 worker、
先进先出，这个次序在跨点击之间也保持成立。

## 有界队列 + 拒绝而不是丢弃

队列容量存在的意义是扛住管理员短时间内连续几次点击，不是扛住持续高吞吐。满了时
``submit`` 返回 ``False``——调用方据此**原地同步执行**，不是丢掉。这批后处理里有终态卡、
群通知和原卡刷新，丢掉是用户可见的结果丢失，比一次回调超时严重得多。
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
            raise ValueError(
                f"queue_maxsize 必须在 {_MIN_QUEUE_MAXSIZE}~{_MAX_QUEUE_MAXSIZE} 之间"
            )
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
                    self._audit.record(
                        POST_CALLBACK_TASK_FAILED_ACTION, error=type(error).__name__
                    )
                except Exception:  # 审计器自身故障同样不得带走 worker
                    pass
            finally:
                self._queue.task_done()


__all__ = ["BackgroundPostCallbackExecutor", "POST_CALLBACK_TASK_FAILED_ACTION"]
