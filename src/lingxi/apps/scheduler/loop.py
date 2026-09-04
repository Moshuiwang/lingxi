"""按同一周期驱动多个定时职责的 :class:`SchedulerLoop`，以及信号安装。

逐职责隔离异常、``SIGTERM``/``SIGINT`` 只设一次停止标志这两条规则的完整理由
见包的 ``__init__.py`` 模块文档。
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from lingxi.apps.scheduler.config import DEFAULT_INTERVAL_SECONDS

logger = logging.getLogger(__name__)


class SchedulerLoop:
    """按同一周期驱动多个定时职责，并把它们的失败互相隔离。

    ``build_loop`` 此前直接返回单个 :class:`CredentialRotationLoop`，"进程只有一个
    职责"这件事被硬编码在装配里。加入第二个职责必须改的正是这里：需要一个能容纳
    职责集合、并且**逐职责**捕获异常的结构。
    """

    def __init__(
        self,
        *,
        duties: Sequence[Any],
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        stop: threading.Event | None = None,
        heartbeat: Callable[[], None] | None = None,
    ) -> None:
        """按注入的职责集合装配一个调度循环；`duties` 不得为空。"""
        if not duties:
            raise ValueError("定时职责进程至少要有一个职责")
        self._duties = tuple(duties)
        self._interval_seconds = interval_seconds
        self._stop = threading.Event() if stop is None else stop
        self._heartbeat = heartbeat

    @property
    def duties(self) -> tuple[Any, ...]:
        """本循环驱动的全部职责，装配时的顺序原样保留。"""
        return self._duties

    @property
    def stop_event(self) -> threading.Event:
        """本循环使用的停止信号，供调用方与职责共享同一个事件对象。"""
        return self._stop

    @property
    def stopping(self) -> bool:
        """是否已收到停止信号。"""
        return self._stop.is_set()

    def request_stop(self) -> None:
        """置位停止信号：本轮及之后不再有职责领取新工作。"""
        self._stop.set()

    def run_once(self) -> tuple[Any, ...]:
        """依次跑一遍每个职责。任何一个职责抛异常都不影响其余职责本轮执行。"""
        reports: list[Any] = []
        if self._heartbeat is not None:
            try:
                self._heartbeat()
            except Exception as error:  # 心跳失败不能跳过定时职责
                logger.error("scheduler 心跳记录失败，职责继续运行 error=%s", type(error).__name__)
        for duty in self._duties:
            if self._stop.is_set():
                # 已经在停止中：不再让后面的职责领取新工作（断言 V-保留-17）。
                reports.append(None)
                continue
            try:
                reports.append(duty.run_once())
            except Exception as error:  # 一个职责失败不能带走另一个
                # 只记异常类型，不记异常正文：正文可能带上被处理对象的内容。
                logger.error(
                    "定时职责本轮异常，其余职责与下一轮不受影响 duty=%s error=%s",
                    getattr(duty, "name", type(duty).__name__),
                    type(error).__name__,
                )
                reports.append(None)
        return tuple(reports)

    def run_forever(self) -> None:
        """按 `interval_seconds` 循环跑 `run_once`，直到停止信号置位。"""
        while not self._stop.is_set():
            self.run_once()
            if self._stop.is_set():
                break
            self._stop.wait(self._interval_seconds)
        logger.info("定时职责已停止领取并退出")


class _Stoppable(Protocol):
    def request_stop(self) -> None: ...


def install_signal_handlers(loop: _Stoppable) -> None:
    """把 ``SIGTERM`` / ``SIGINT`` 接到"停止领取"上。

    处理函数只设一个事件标志，不做任何 I/O：信号处理函数里写库或发网络请求会在
    退出路径上引入新的失败模式，而这条路径恰恰是最不该出错的地方。
    """

    def handle(signal_number: int, _frame: Any) -> None:
        logger.info("收到信号，停止领取新的到期凭据 signal=%s", signal_number)
        loop.request_stop()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
