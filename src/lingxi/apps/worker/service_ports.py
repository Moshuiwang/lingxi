"""worker 服务层的端口协议与回调签名。

本模块只放形状，不放实现：``apps/worker`` 对 ``adapters`` 的依赖一律经装配层注入，
不在类型签名里写死具体适配器类型。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from lingxi.apps.worker.config import WorkerConfig
from lingxi.core.innertest_content_capture import ContentCaptureRecord
from lingxi.core.user_memory import RenderedUserMemoryPrompt


class QueueListener(Protocol):
    """队列通知的监听口：等一个通知，或等到超时。"""

    def wait(self, *, timeout_seconds: float) -> bool:
        """等一次通知；返回是否真的等到。"""
        ...


class UserMemoryReader(Protocol):
    """用户记忆注入口。

    只依赖这个签名，鸭子类型足够——不 import 具体的适配器类型，保持本层对适配器的依赖
    只经装配层注入。
    """

    def fetch_prompt_segment(self, *, user_id: str) -> RenderedUserMemoryPrompt | None:
        """取这个人的记忆片段；没有记忆时返回 ``None``。"""
        ...


ExecutorFactory = Callable[[WorkerConfig, Callable[[], None]], Any]
HeartbeatCallback = Callable[[], None]
TaskStuckCallback = Callable[[str, int], None]
TerminalOutcomeCallback = Callable[[Mapping[str, object]], None]
YearGroundingSuspectCallback = Callable[[Mapping[str, object]], None]


@dataclass(frozen=True)
class WorkerObservers:
    """装配层注入的六个观测出口，全部可留空。

    心跳、任务滞留告警、告警状态机推进、终态审计、内容采集、年份护栏。留空的那一项整体跳过：
    **没有装配方就没有输出**，不假装写了一条实际被吞掉的记录。本服务是纯组装对象，不知道
    自己会被哪个进程入口装配，也不该假设标准库日志已经配过 handler——真实队列 worker 刻意
    不做日志初始化，默认阈值会把 INFO 悄悄吞掉。
    """

    heartbeat: HeartbeatCallback | None = None
    on_task_stuck: TaskStuckCallback | None = None
    on_alert_tick: Callable[[], None] | None = None
    on_terminal_outcome: TerminalOutcomeCallback | None = None
    content_capture_writer: Callable[[ContentCaptureRecord], None] | None = None
    on_year_grounding_suspect: YearGroundingSuspectCallback | None = None


@dataclass(frozen=True)
class SessionCleanupSettings:
    """会话转录清理的落点与批量。

    ``root`` 为空表示当前环境没有可用的会话根目录（例如缺 HOME）：清理整体跳过，请求
    留着排队等下一个配置正确的进程来处理，而不是假装已清理。
    """

    root: Path | None = None
    batch_limit: int = 20
