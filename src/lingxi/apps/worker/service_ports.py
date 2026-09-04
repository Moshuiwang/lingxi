"""worker 服务层的端口协议与回调签名。

本模块只放形状，不放实现：``apps/worker`` 对 ``adapters`` 的依赖一律经装配层注入，
不在类型签名里写死具体适配器类型。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from lingxi.apps.worker.config import WorkerConfig
from lingxi.core.user_memory import RenderedUserMemoryPrompt


class QueueListener(Protocol):
    def wait(self, *, timeout_seconds: float) -> bool: ...


class UserMemoryReader(Protocol):
    """用户记忆注入口（Issue #357 S-H3-3 d 节）。真实实现是
    ``adapters.postgres_user_memory.PostgresUserMemoryReader``；本模块只依赖这个
    签名，鸭子类型足够，不 import 具体的 adapters 类型（与 `queue: Any` 同一姿态，
    保持 `apps/worker` 对 `adapters` 的依赖只经装配层注入，不在类型签名里写死）。
    """

    def fetch_prompt_segment(self, *, user_id: str) -> RenderedUserMemoryPrompt | None: ...


ExecutorFactory = Callable[[WorkerConfig, Callable[[], None]], Any]
HeartbeatCallback = Callable[[], None]
TaskStuckCallback = Callable[[str, int], None]
# 终态收口低敏审计事件（Issue #90 评论 5306860255 的独立复核 P1）：字段名与取值
# 见 ``WorkerService._log_terminal_outcome``。``WorkerService`` 是纯组装对象，
# 不知道自己会被哪个进程入口装配，也不该假设 stdlib ``logging`` 有 handler——
# 真实队列 worker 的 `apps/worker/cli.py` 刻意从不调用 `logging.basicConfig()`
# （见该文件 `_LogOnlyAlertSender` 的说明：未配置 handler 时默认阈值
# `WARNING` 会把 `logging.info(...)` 悄悄吞掉），因此这条低敏审计事件必须像
# `heartbeat`/`on_task_stuck`/`on_alert_tick` 一样由装配层注入真正的输出出口，
# 不能自己直接调 `logging`。
TerminalOutcomeCallback = Callable[[Mapping[str, object]], None]
# 年份接地护栏第二层的结构化告警出口（Issue #326）：与 ``TerminalOutcomeCallback``
# 同一条纪律——``WorkerService`` 不直接调 stdlib ``logging``（理由同上），检测到
# 的信号必须交给装配层注入的回调，由 ``apps/worker/cli.py`` 接到既有的结构化
# stderr 出口（``worker.year_grounding_suspect``，带 trace_id）。``None`` 时
# ``_check_year_grounding_suspect`` 整体跳过，不做检测、不产生任何额外开销。
YearGroundingSuspectCallback = Callable[[Mapping[str, object]], None]
