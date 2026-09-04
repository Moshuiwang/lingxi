"""一轮回合的语义化等待进度：把四个信号源节流成有限的几次进度写入。

四个信号源——模型文本输出、工具返回、工具调用开始、兜底计时——共享同一份节流状态，
区分三类文案：问数查询（再按子步骤名细分）、模型生成（含刚拿到工具返回）、其它工具调用。

写库丢进线程池、不在这里等结果：三个调用方都是执行器与监控循环里的同步回调，改不成
``await``，而真实同步写最坏可能卡住数秒，直接拖住事件循环会连带心跳与停止处理一起变慢。
连续几次写入串成一条链，保证同一任务的进度事件严格按调用顺序落库；全部在途写入由
:meth:`TurnProgressReporter.drain` 在回合收尾时一次等齐，终态判定因此不会跑在它们前面。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

from lingxi.apps.worker.config import QUERY_MCP_TOOL_PREFIX
from lingxi.core.execution.card_stream import (
    PROGRESS_ACTION_COMPOSING,
    PROGRESS_ACTION_PROCESSING,
    PROGRESS_ACTION_QUERYING,
    PROGRESS_ACTION_WORKING,
    encode_progress_action,
)

# 兜底周期取 12 秒，来自「卡片文字十几秒内必有变化」这条体验预算。两个数字之间的比例
# 是硬的：事件驱动间隔必须明显短于兜底周期，否则兜底会反过来成为主要更新来源。呈现层
# 的停滞判定阈值恒等于兜底周期的 **2 倍**（两者相等时，一个完全正常的兜底周期静默会被
# 误判成停滞）；那个常量住在呈现层、与这里互不 import，改任一侧都要回头核对另一侧。
PROGRESS_MIN_UPDATE_INTERVAL_SECONDS = 5.0
PROGRESS_FALLBACK_SECONDS = 12.0


class TurnProgressReporter:
    """按信号源更新进度文案，并按最小间隔节流真正的写库。

    节流状态的读取与更新全程不 ``await``：从进入判定到把新写入任务排出去是一段连续的
    同步代码。事件循环单线程、协作式调度只在 ``await`` 处切换，因此三个调用方即使来自
    不同的异步任务也不会交叉，这里不需要锁。
    """

    def __init__(
        self,
        *,
        write_event: Callable[..., None],
        monotonic: Callable[[], float],
        started_at: float,
    ) -> None:
        """以任务开始时刻为节流锚点。

        起始事件已经让下游建卡并展示过一次默认文案，节流窗口紧接着它算起、不是从零开始。
        """
        self._write_event = write_event
        self._monotonic = monotonic
        self._started_at = started_at
        self._last_write_at = started_at
        self._count = 0
        self._action = PROGRESS_ACTION_PROCESSING
        self._query_count = 0
        self._query_step: str | None = None
        self._last_write_task: asyncio.Task[None] | None = None
        self._in_flight: set[asyncio.Task[None]] = set()

    def on_stream_event(self, event: Mapping[str, Any]) -> None:
        """模型输出正文或工具返回：都切到"生成中"。

        工具返回也算：工具发出去之后到模型开始输出下一段文字之间，隔着一段"看完结果、
        组织下一步"的静默。这段时间没有任何信号，进度身份会停在原地不动，一旦它叠加
        工具本身的耗时超过停滞阈值就会被误判成停滞。工具结果一到就切换身份并写一次
        进度，让停滞计时的锚点跟着清零——不必等模型真的吐出字来。
        """
        if event.get("kind") not in {"assistant_message", "tool_result"}:
            return
        self._action = PROGRESS_ACTION_COMPOSING
        self._query_step = None
        self._write_if_due(PROGRESS_MIN_UPDATE_INTERVAL_SECONDS)

    def on_tool_call(self, tool_name: str) -> None:
        """工具调用开始：问数查询单独计数并带子步骤名，其它工具归入"处理中"。

        不回显工具名、参数或任何查询内容；子步骤名是否落进白名单由编码函数统一把关，
        这里刻意不提前过滤——两处各维护一份白名单迟早悄悄漂移。
        """
        if isinstance(tool_name, str) and tool_name.startswith(QUERY_MCP_TOOL_PREFIX):
            self._query_count += 1
            self._action = PROGRESS_ACTION_QUERYING
            self._query_step = tool_name[len(QUERY_MCP_TOOL_PREFIX) :]
        else:
            self._action = PROGRESS_ACTION_WORKING
            self._query_step = None
        self._write_if_due(PROGRESS_MIN_UPDATE_INTERVAL_SECONDS)

    def on_stall_tick(self) -> None:
        """兜底计时：距上次写库超过兜底周期时强制推一次纯用时更新。"""
        self._write_if_due(PROGRESS_FALLBACK_SECONDS)

    async def drain(self) -> None:
        """等齐本回合排出的全部后台写入。

        终态判定依赖"这一轮的进度写入已经落库或明确失败"这个前提，因此在回合收尾时
        一次性等完，而不是每次写入各等各的。
        """
        if self._in_flight:
            await asyncio.gather(*self._in_flight, return_exceptions=True)

    def _write_if_due(self, min_gap_seconds: float) -> None:
        """两次真正写库的间隔 ≥``min_gap_seconds`` 才放行；否则合并，只保留最新状态。"""
        now = self._monotonic()
        if now - self._last_write_at < min_gap_seconds:
            return
        self._last_write_at = now
        self._count += 1
        previous = self._last_write_task
        suffix = f"progress:{self._count}"
        elapsed_seconds = int(max(0.0, now - self._started_at))
        content = self._encode_action()

        async def write_after_previous() -> None:
            if previous is not None:
                # 只是排队等前一次写完，不关心它是否成功——失败已经在写入口内部记过
                # 日志，这里再等一次异常没有意义。
                await asyncio.gather(previous, return_exceptions=True)
            await asyncio.to_thread(
                self._write_event,
                event_type="progress",
                idempotency_key_suffix=suffix,
                elapsed_seconds=elapsed_seconds,
                content=content,
            )

        # 刻意不用 ``add_done_callback`` 提前摘除已完成的任务：那样会让它的异常在被
        # ``drain`` 取走之前就被摘掉，触发 asyncio 的"异常从未被取回"噪音日志。这个
        # 集合按回合生命周期存在，一次回合内的写入次数有限，留着不构成内存问题。
        task = asyncio.create_task(write_after_previous())
        self._last_write_task = task
        self._in_flight.add(task)

    def _encode_action(self) -> str | None:
        """把当前进度身份编码成事件正文；"处理中"没有专属文案，返回 ``None``。"""
        if self._action == PROGRESS_ACTION_QUERYING:
            return encode_progress_action(
                PROGRESS_ACTION_QUERYING,
                query_count=self._query_count,
                query_step=self._query_step,
            )
        if self._action == PROGRESS_ACTION_COMPOSING:
            return encode_progress_action(PROGRESS_ACTION_COMPOSING)
        if self._action == PROGRESS_ACTION_WORKING:
            return encode_progress_action(PROGRESS_ACTION_WORKING)
        return None
