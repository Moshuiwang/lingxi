"""Gateway 投递消费循环：读 outbox、驱动 CardKit 流式卡片与文本兜底（Issue #152）。

只消费 #151 已经**提交**的 ``task_delivery_event``；内存回调不算投递（issue 状态合同
第 1 条）。与 ``apps/worker/service.py`` 同一惯例：领域编排放在 ``apps/<name>/`` 而不是
``core/``，纯规则（终态分类）仍然住在 ``core.delivery.ports``；卡片顺序/限流/失败回退
住在 ``core.execution.card_stream``，本模块只负责把 outbox 事件、``CardStream`` 与
Gateway 侧的消费进度持久化（``adapters.postgres_conversation`` 新增的一组方法）接起来。

**重复投递防线**（Issue #151 审核 P3-6，见 ``core.execution.card_stream`` 与
``adapters.postgres_conversation`` 的模块说明）：建卡、终态卡片更新+关闭
（``CardStream.finish()``）与文本兜底发送三类外部调用，在真正外发前都必须先提交
"外发前预留位"（``reserve_dispatch``），调用有了明确结果后立即清空。终态卡片
更新+关闭看似有 CardKit 整卡级 ``sequence`` 天然保护（重放同一序号会被平台拒绝，
不产生第二次可见卡片帧），但那只保证卡片本身不重复，**不保证消费循环的错误处理
不会把一次因崩溃重启导致的序号冲突误判成"卡片链路整体失败"、进而降级到文本兜底**
——那样会在卡片其实已经成功送达之后又多发一条文本终态，是跨通道的重复投递，因此
同样纳入预留位保护（迁移 0060 头部注释）。**进程崩溃发生在提交预留位与清空之间时**，
下一轮 ``list_pending_delivery_tasks`` 会把这个任务排除在正常消费之外，
``list_uncertain_delivery_tasks`` 把它路由给告警——消费循环不会替它猜测上一次调用
是否成功后自动重发（issue 状态合同第 6 条）。恢复需要人工核对后清空预留位。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from lingxi.config.content import ContentCatalog, RenderedContent, default_content_catalog
from lingxi.core.delivery.ports import TerminalKind
from lingxi.core.execution.card_stream import (
    CardRateLimiter,
    CardStream,
    CardTransport,
    SendOutcomeCallback,
    TextTransport,
)

logger = logging.getLogger(__name__)

AlertCallback = Callable[[str, str], None]


def _default_alert(kind: str, task_id: str) -> None:
    """默认告警出口：结构化日志。真实告警路由（管理群、AlertManager）由调用方注入；
    本类不持有任何具体的告警传输，只保证"发生了"一定会被记下来，不含正文。
    """

    logger.error("投递消费告警 kind=%s task_id=%s", kind, task_id)


class DeliveryConsumer:
    """一轮读若干候选任务、逐个驱动到底；异常按任务隔离，一个任务失败不带走整轮。"""

    def __init__(
        self,
        *,
        queue: Any,
        cards: CardTransport,
        texts: TextTransport,
        catalog: ContentCatalog | None = None,
        rate_limiter: CardRateLimiter | None = None,
        on_send_outcome: SendOutcomeCallback | None = None,
        on_alert: AlertCallback | None = None,
        limit: int = 20,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._queue = queue
        self._cards = cards
        self._texts = texts
        self._catalog = catalog or default_content_catalog()
        # 单进程共享同一个限流器：CardKit 全局 50 次/秒的上限是**整进程**共享的，
        # 不是每任务各自计数（`V-卡片-02`）。
        self._rate_limiter = rate_limiter or CardRateLimiter()
        self._on_send_outcome = on_send_outcome
        self._alert = on_alert or _default_alert
        self._limit = limit
        self._monotonic = monotonic

    def run_once(self) -> int:
        """跑一轮：先报告仍然卡在"外发前预留位"里的任务，再处理正常候选。
        返回本轮实际处理的候选任务数（不含仅被告警的 uncertain 任务）。
        """

        for uncertain in self._queue.list_uncertain_delivery_tasks():
            self._alert("dispatch_uncertain:" + uncertain.reserved_kind, uncertain.task_id)

        tasks = self._queue.list_pending_delivery_tasks(limit=self._limit)
        for task in tasks:
            try:
                self._process_task(task)
            except Exception as error:  # noqa: BLE001 - 一个任务的异常不能带走同一轮的其他任务
                logger.error(
                    "投递消费单个任务异常，本轮其余任务不受影响 task_id=%s error=%s",
                    task.task_id,
                    type(error).__name__,
                )
        return len(tasks)

    def _process_task(self, task: Any) -> None:
        events = self._queue.read_delivery_events(
            task_id=task.task_id, after_sequence=task.consumed_sequence
        )
        stream = CardStream(
            chat_id=task.chat_id,
            thread_id=task.thread_id,
            reply_to_message_id=task.reply_to_message_id or "",
            transport=self._cards,
            fallback=self._texts,
            catalog=self._catalog,
            rate_limiter=self._rate_limiter,
            on_send_outcome=self._on_send_outcome,
            monotonic=self._monotonic,
            initial_card_id=task.card_id,
            initial_sequence=task.card_seq,
            initial_message_id=task.message_id,
            initial_fallback_needed=task.fallback_text,
        )

        for event in events:
            if event.event_type == "started":
                if not self._handle_started(task, stream, event):
                    # 预留位没抢到：这个 started 事件本轮无法安全处理完（要么已被
                    # 崩溃恢复判定为 uncertain，要么任务状态发生了竞态），后续事件
                    # 建立在"卡片是否已建成"这个前提上，本轮不能继续往下处理。
                    break
            elif event.event_type in ("progress", "safely_releasable_answer"):
                self._handle_progress(task, stream, event)
            elif event.event_type == "terminal":
                self._handle_terminal(task, stream, event)
                # terminal 是该任务在 outbox 里唯一一条终态事件（#151 状态合同：
                # 重复终态被拒绝），处理完不会再有后续事件需要看。
                break

        self._maybe_confirm(task, stream)

    def _handle_started(self, task: Any, stream: CardStream, event: Any) -> bool:
        """返回是否可以继续处理本轮的后续事件（`False` 表示预留位没抢到）。"""

        if stream.card_id is None and not stream.fallback_needed:
            if not self._queue.reserve_dispatch(task_id=task.task_id, kind="card_create"):
                # 抢不到预留位：要么上一轮已经处理到一半崩溃（uncertain，本轮不碰），
                # 要么任务已经不在可处理状态。两种情况都不该继续外发，直接返回，
                # 这个 started 事件的游标本轮不推进，下一轮重新评估。
                return False
            stream.start()
        self._queue.record_delivery_progress(
            task_id=task.task_id,
            consumed_sequence=event.sequence,
            card_id=stream.card_id,
            message_id=stream.message_id,
            fallback_text=stream.fallback_needed,
        )
        return True

    def _handle_progress(self, task: Any, stream: CardStream, event: Any) -> None:
        # `safely_releasable_answer` 走同一条状态区更新路径：Worker 侧目前没有生产
        # 写入方（#151 已登记留白），流式正文本体的卡片渲染留待该写入方接上之后
        # 再做——这里先保证"消费到了、游标推进了"是安全、不会重复的（见模块说明）。
        stream.update(elapsed_seconds=event.elapsed_seconds or 0)
        self._queue.record_delivery_progress(
            task_id=task.task_id,
            consumed_sequence=event.sequence,
            card_sequence=stream.sequence,
            fallback_text=stream.fallback_needed,
        )

    def _handle_terminal(self, task: Any, stream: CardStream, event: Any) -> None:
        content = RenderedContent(
            key="delivery.terminal", version=self._catalog.version, text=event.content or ""
        )
        elapsed = event.elapsed_seconds or 0

        if stream.card_id is not None and not stream.fallback_needed:
            # 卡片路径仍然存活：终态更新+关闭这两次外部调用整体纳入预留位保护
            # （见模块说明）——不能只靠 CardKit 的 sequence 拒绝来防重复，那只挡得住
            # "第二次可见卡片帧"，挡不住"这次拒绝被误判成卡片链路失败、降级到文本
            # 兜底、跟已经成功的卡片一起构成跨通道重复投递"。
            if not self._queue.reserve_dispatch(task_id=task.task_id, kind="card_finish"):
                # 抢不到：要么上一轮处理到一半崩溃（uncertain，本轮不碰），要么任务
                # 状态发生了竞态。两种情况都不该再调用 finish()。
                return
            if event.terminal_kind == TerminalKind.SUCCESS.value:
                stream.finish(result=event.content or "", elapsed_seconds=elapsed)
            else:
                stream.finish(failure=content, elapsed_seconds=elapsed)
            # finish() 内部吞掉了全部同步异常（card_stream.py 的既有设计），走到这里
            # 就是拿到了明确结果（成功，或已经清晰地降级为需要文本兜底），可以安全清空。
            self._queue.clear_dispatch_reservation(task_id=task.task_id)

        if stream.fallback_needed:
            if not self._send_fallback(task, stream, content):
                # 预留位没抢到，或外发同步捕获到明确失败：这一轮不推进游标，terminal
                # 事件下一轮重新处理（`_send_fallback` 已经在明确失败时清了预留位，
                # 崩溃则留给 list_uncertain_delivery_tasks）。
                return

        self._queue.record_delivery_progress(
            task_id=task.task_id,
            consumed_sequence=event.sequence,
            card_id=stream.card_id,
            message_id=stream.message_id,
            card_sequence=stream.sequence,
            fallback_text=stream.fallback_needed,
        )

    def _send_fallback(self, task: Any, stream: CardStream, content: RenderedContent) -> bool:
        if not self._queue.reserve_dispatch(task_id=task.task_id, kind="text_send"):
            return False
        try:
            stream.send_fallback(content)
        except Exception as error:  # noqa: BLE001 - 明确失败：清预留位，下一轮重试
            self._queue.clear_dispatch_reservation(task_id=task.task_id)
            self._alert("fallback_send_failed:" + type(error).__name__, task.task_id)
            return False
        return True

    def _maybe_confirm(self, task: Any, stream: CardStream) -> None:
        """终态已经落到某个通道且任务处于 ``awaiting_delivery`` 时尝试确认送达。

        ``task.status`` 是本轮开始时的快照；只有它已经是 ``awaiting_delivery`` 才
        说明这一轮（或更早一轮）真的处理过 terminal 事件——Worker 写终态事件与把
        任务转 ``awaiting_delivery`` 在同一事务提交（#151 状态合同第 2 条），因此
        "outbox 里有 terminal" 与 "task.status=='awaiting_delivery'" 在任意读取时刻
        都一致，不需要额外判断"这一轮是不是刚处理完 terminal"。这也让"上一轮已经
        拿到 message_id 但 confirm_delivery 没成功"的任务在没有任何新事件时依然会
        被重试（`list_pending_delivery_tasks` 的第二个 OR 分支）。
        """

        if task.status != "awaiting_delivery" or stream.message_id is None:
            return
        kind = "text" if stream.fallback_needed else "card"
        try:
            confirmed = self._queue.confirm_delivery(
                task_id=task.task_id,
                platform_message_kind=kind,
                platform_message_id=stream.message_id,
            )
        except Exception as error:  # noqa: BLE001 - 记录后交给下一轮重试
            logger.error(
                "confirm_delivery 调用异常，下一轮重试 task_id=%s error=%s",
                task.task_id,
                type(error).__name__,
            )
            return
        if not confirmed:
            # False 不是错误：可能已被别的路径解析（到期强制收敛与本次投递竞态，
            # 见模块说明的已知窄窗口），也可能任务已经不在 awaiting_delivery。
            # 记一条日志便于事后核对，不重试、不报错。
            logger.info(
                "confirm_delivery 未命中可确认的记录，任务可能已由到期路径收敛 task_id=%s",
                task.task_id,
            )

    def run_forever(self, *, stop: threading.Event, poll_interval_seconds: float) -> None:
        """长期循环：每轮之间固定等待一个轮询间隔。

        与 ``apps/worker/service.py`` 的 ``run()`` 同一惯例——没有独立的 NOTIFY 监听
        （outbox 事件的写入方是 Worker 进程，不在本进程内），轮询间隔是唯一的发现
        机制。**刻意不做"这一轮处理满批就立即再跑一轮"的优化**：那样在一批任务反复
        遇到同一个真实失败（而不是积压）时会变成不带退避的忙轮询，对数据库与飞书
        出站接口造成持续压力；积压场景下晚一个轮询间隔被拾取，代价可以接受。
        """

        while not stop.is_set():
            self.run_once()
            stop.wait(poll_interval_seconds)
        logger.info("投递消费循环已停止")


def build_delivery_consumer(
    *,
    client: Any,
    queue: Any,
    on_send_outcome: SendOutcomeCallback | None = None,
    on_alert: AlertCallback | None = None,
    limit: int = 20,
) -> DeliveryConsumer:
    """按已经建好的飞书 SDK 客户端与队列适配器装配消费者。

    ``client`` 传入的是各调用方自行建好的飞书 SDK 客户端实例——飞书 SDK 客户端本身
    同时暴露 ``.im`` 与 ``.cardkit`` 两个命名空间，一个客户端即可同时驱动卡片与
    文本兜底，不需要为投递单独建第二套鉴权。
    """

    from lingxi.adapters.feishu_delivery import LarkCardTransport, LarkDeliveryText

    return DeliveryConsumer(
        queue=queue,
        cards=LarkCardTransport(client),
        texts=LarkDeliveryText(client),
        on_send_outcome=on_send_outcome,
        on_alert=on_alert,
        limit=limit,
    )
