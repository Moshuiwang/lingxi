"""Gateway 投递消费循环：读 outbox、驱动 CardKit 流式卡片与文本兜底。

只消费已经**提交**的 ``task_delivery_event``；纯规则（终态分类）住在
``core.delivery.ports``，卡片顺序/限流/失败回退住在 ``core.execution.card_stream``。

**重复投递防线**：建卡、终态卡片更新+关闭、文本兜底发送三类外部调用，外发前都必须
先提交"外发前预留位"，调用有明确结果后立即清空。**只有服务端明确拒绝**才清空、允许
下一轮重试；其余一切异常（含平台回「仍在发送」）都归结果不明，绝不清空，统一转
``uncertain`` 交给人工核对——**这三类外发一个平台去重键都没有**（CardKit 的 ``sequence``
是操作序号不是去重键），没有键的重投是在赌"上一次没送到"，赌错就是用户收到第二份完整
答案。**确认送达的唯一前提是"本次终态的有效回执"**，见 :meth:`._terminal_receipt`。

过程流式正文靠重放 outbox 历史事件重建，没有跨轮次进程内状态；CardKit 10 分钟自动关闭
窗口靠提前收口文案缓解；三条循环级查询各自异常隔离、连续失败达阈值才上报。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from enum import Enum
from typing import Any

from lingxi.config.content import ContentCatalog, RenderedContent, default_content_catalog
from lingxi.core.delivery.ports import TerminalKind
from lingxi.core.execution.card_stream import (
    CardRateLimiter,
    CardStream,
    CardTransport,
    DeliveryRejectedError,
    DeliveryUncertainError,
    ProgressStepSnapshot,
    SendOutcomeCallback,
    TerminalReceipt,
    TextTransport,
    decode_progress_action,
)

logger = logging.getLogger(__name__)

AlertCallback = Callable[[str, str], None]

# 循环级异常发生在"读哪些任务"这一步，此时还不知道是哪个任务，没有
# ``task_id`` 可填；但既有告警回调的形状是 ``(kind, task_id)``，且 ``task_id``
# 会被上报当作 ``trace_id``。用一个固定、符合安全格式的占位标识，让这类告警
# 在管理群里仍然认得出来源，而不是退化成一条没有任何出处的裸告警。
LOOP_ALERT_TRACE_ID = "gateway-delivery-loop"


def _default_alert(kind: str, task_id: str) -> None:
    """默认告警出口：结构化日志。

    真实告警路由（管理群、AlertManager）由调用方注入；本类不持有任何具体的
    告警传输，只保证"发生了"一定会被记下来，不含正文。
    """
    logger.error("投递消费告警 kind=%s task_id=%s", kind, task_id)


class _FallbackOutcome(Enum):
    """``DeliveryConsumer._send_fallback`` 的结果分类。

    只有 ``SENT`` 才意味着这一条终态真的落到了文本通道上（并因此带回一枚
    终态回执）；``RETRY_LATER``（预留位没抢到、明确失败）与 ``UNCERTAIN``
    都不推进游标、都不允许确认送达，区别只在告警口径与是否进入重试退避。
    """

    SENT = "sent"
    RETRY_LATER = "retry_later"
    UNCERTAIN = "uncertain"


class _CardFinishOutcome(Enum):
    """``DeliveryConsumer._finish_card_channel`` 的两态结果。

    ``HALT`` 要求调用方立即从 `_handle_terminal` 返回（预留位没抢到、或终态
    卡片更新结果不明）；``CONTINUE`` 表示卡片终态处理完毕（或本来就不适用），
    继续走文本兜底与游标推进那一段。
    """

    HALT = "halt"
    CONTINUE = "continue"


class DeliveryConsumer:
    """一轮读若干候选任务、逐个驱动到底；异常按任务隔离，一个任务失败不带走整轮。

    **一轮零进展的任务要让出名额**：候选按 ``created_at`` 取前若干个，一条持续失败
    的老任务不让路就会每轮占满整批、把批量上限之外的健康用户永远挡在候选之外。因此
    "没成交"与"抛穿"两种零进展轮次都把下次可再试的时刻写回数据库（见
    :meth:`_record_retry_backoff`），由候选发现查询过滤——扩大批量上限不是修复，
    那只是把"前 N 名"换成更大的 N。
    """

    # uncertain 告警与外发重试原来完全没有退避，默认 `poll_interval=1.0s`
    # 下会变成每秒一次的告警/外发洪流。这两个默认值把它们收敛到"仍然会被
    # 看见，但不再洪泛"——具体数值不是硬性产品承诺，可按需调。
    DEFAULT_ALERT_MIN_INTERVAL_SECONDS = 300.0
    DEFAULT_RETRY_BACKOFF_BASE_SECONDS = 2.0
    DEFAULT_RETRY_BACKOFF_CAP_SECONDS = 300.0
    # 指数退避的档位上限。退避档位现在跨进程重启保持有效，一条持续失败的任务
    # 累积的次数不再自动清零；不封顶时 `2 ** attempts` 会涨成一个大整数，与
    # 浮点秒数相乘直接溢出。封在这里，退避时长本身仍由 cap 决定。
    MAX_RETRY_BACKOFF_EXPONENT = 30
    # 连续多少轮循环级异常才上报。一次瞬时错误下一轮就会自己好，为它告警只会
    # 让真信号被噪音淹没；"连着几轮都失败"才意味着投递能力已经实际停摆。取 3：
    # 默认 1 秒轮询下约 3 秒即报，同时把单点抖动挡在告警之外，数值可按需调。
    DEFAULT_LOOP_FAILURE_ALERT_THRESHOLD = 3
    # 排队可感知：入队后超过这个阈值仍未被任何 worker 领取，就补发一条"前面
    # 还有任务在排队，请稍等"。定值理由：产品要求落在 10~15 秒区间，取中值
    # 12 秒——本轮询默认 1 秒一次，12 秒留出观察窗口且比上限留了余量，可通过
    # `LINGXI_GATEWAY_QUEUE_DELAY_HINT_SECONDS` 覆盖。
    DEFAULT_QUEUE_DELAY_HINT_SECONDS = 12.0
    DEFAULT_QUEUE_DELAY_HINT_LIMIT = 50

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
        uncertain_limit: int = 50,
        monotonic: Callable[[], float] = time.monotonic,
        alert_min_interval_seconds: float = DEFAULT_ALERT_MIN_INTERVAL_SECONDS,
        retry_backoff_base_seconds: float = DEFAULT_RETRY_BACKOFF_BASE_SECONDS,
        retry_backoff_cap_seconds: float = DEFAULT_RETRY_BACKOFF_CAP_SECONDS,
        loop_failure_alert_threshold: int = DEFAULT_LOOP_FAILURE_ALERT_THRESHOLD,
        queue_delay_hint_seconds: float = DEFAULT_QUEUE_DELAY_HINT_SECONDS,
        queue_delay_hint_limit: int = DEFAULT_QUEUE_DELAY_HINT_LIMIT,
    ) -> None:
        """装配一轮消费循环所需的传输、限流与告警/退避策略。"""
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
        self._uncertain_limit = uncertain_limit
        self._monotonic = monotonic
        self._alert_min_interval_seconds = alert_min_interval_seconds
        self._retry_backoff_base_seconds = retry_backoff_base_seconds
        self._retry_backoff_cap_seconds = retry_backoff_cap_seconds
        self._loop_failure_alert_threshold = loop_failure_alert_threshold
        self._queue_delay_hint_seconds = queue_delay_hint_seconds
        self._queue_delay_hint_limit = queue_delay_hint_limit
        # 进程内、非持久化状态：只用来给告警与外发重试限速，重启后清零属于可接受
        # 的降级（重启本身就已经是一次重新评估的机会）。
        self._last_alerted_at: dict[tuple[str, str], float] = {}
        # 当前这一条候选本轮有没有推进过消费游标（见 `_advance_delivery_cursor`）。
        # 单条循环、逐个任务处理，因此一个标志就够，不需要按任务分桶。
        self._advanced_this_task = False
        # 已经发过排队提示的任务 id：同一姿态的进程内、非持久化状态——只用来
        # 避免同一个任务每轮轮询都重发一次提示。每轮与当前候选集合取交集
        # 自然收缩（见 `_notify_stale_queued`），不会无界增长。
        self._queue_delay_notified: set[str] = set()
        # 循环级异常的计数，同样是进程内状态。``consecutive_loop_failures``
        # 决定何时上报，一轮完全正常即归零；``loop_failures_total`` 只增不减，
        # 两者都留成可读属性，供排查时直接读。
        self.consecutive_loop_failures = 0
        self.loop_failures_total = 0

    def _alert_deduped(self, kind: str, task_id: str) -> None:
        """同一个 (kind, task_id) 在 `alert_min_interval_seconds` 内只告警一次。

        默认 1 秒轮询下不去重会变成密集的告警噪音，真实告警路由接上后就是
        刷屏。这里只做"降频"，不做"消音"——问题仍然会被看见，只是不再淹没
        其它信号；数据库里的预留位本身没有变化，人工核对的判断依据不受影响。
        """
        key = (kind, task_id)
        now = self._monotonic()
        last = self._last_alerted_at.get(key)
        if last is not None and now - last < self._alert_min_interval_seconds:
            return
        self._last_alerted_at[key] = now
        self._alert(kind, task_id)

    def _advance_delivery_cursor(self, **fields: Any) -> None:
        """推进消费游标，并记下"这一轮确实有进展"。

        三处推进游标的地方都走这一个口。写回本身会把退避两列清零（这条通道刚被证明
        是通的），因此"推进过"与"该不该记退避"是同一件事的两面：`run_once` 的隔离
        handler 据此区分真正的零进展与"推进过、之后才抛"。
        """
        self._queue.record_delivery_progress(**fields)
        self._advanced_this_task = True

    def _record_retry_backoff(self, task: Any) -> None:
        """记一次"这一轮完全没有进展"，把下次可以再试的时刻**写回数据库**。

        退避落库才谈得上公平：候选发现查询按它过滤，一条正在退避的任务在窗口内
        根本不进候选，不会每轮占掉一个名额却零进展，健康任务因此排得上。写回失败
        只降级这一件事——最坏结果是这条任务下一轮照旧进候选，与本机制加入之前
        一致；它不放宽任何一道防重复的闸，因此不会变成重复投递。
        """
        try:
            # 取值一并纳入 try：本方法只在"单任务异常不带走整轮"那条不变量的隔离
            # handler 里被调用，那里不允许留任何无保护语句——一条读不出字段的候选
            # 会把整轮带走，而这一段本来就是降级路径。
            attempts = task.retry_attempts + 1
            delay = min(
                self._retry_backoff_base_seconds
                * (2 ** min(attempts - 1, self.MAX_RETRY_BACKOFF_EXPONENT)),
                self._retry_backoff_cap_seconds,
            )
            self._queue.record_delivery_retry(
                task_id=task.task_id, attempts=attempts, delay_seconds=delay
            )
        except Exception as error:  # 见方法文档：只降级退避本身
            logger.error(
                "投递重试退避写回失败，该任务下一轮照常进入候选 task_id=%s error=%s",
                task.task_id,
                type(error).__name__,
            )

    def _note_loop_failure(self, error: BaseException, *, stage: str) -> None:
        """记一次循环级异常：计数、结构化日志，连续到阈值就上报。

        只记异常类型不记正文——正文可能带上被处理对象的内容，与本模块单任务隔离、
        ``apps/scheduler`` 定时职责隔离的既有口径一致。告警 kind 用 ``stage`` 而不是
        异常类型：``stage`` 是一个很小的闭集（两条查询 + 整轮兜底），管理员看到的是
        "投递循环的哪一段坏了"这个可据以行动的事实；具体异常类型留在日志里。
        """
        self.consecutive_loop_failures += 1
        self.loop_failures_total += 1
        logger.error(
            "投递消费循环级异常，本轮降级后继续 stage=%s error=%s consecutive=%d total=%d",
            stage,
            type(error).__name__,
            self.consecutive_loop_failures,
            self.loop_failures_total,
        )
        if self.consecutive_loop_failures >= self._loop_failure_alert_threshold:
            # 到阈值后每轮都上报，由 `_alert_deduped` 的最小间隔与告警状态机自己的
            # 去重窗口双重限速——"还在坏"这件事应该持续可见，而不是只在跨过阈值的
            # 那一轮响一声之后就再也不提。
            self._alert_deduped("delivery_loop_failed:" + stage, LOOP_ALERT_TRACE_ID)

    def _note_loop_recovered(self) -> None:
        """一轮完全没有循环级异常：连续计数归零。"""
        if self.consecutive_loop_failures:
            logger.info(
                "投递消费循环已从连续异常中恢复 consecutive_before=%d",
                self.consecutive_loop_failures,
            )
        self.consecutive_loop_failures = 0

    def run_once(self) -> int:
        """跑一轮：先报告仍然卡在"外发前预留位"里的任务，再处理正常候选。

        返回本轮实际处理的候选任务数（不含仅被告警的 uncertain 任务、也不含
        本轮补发的排队提示）。**三条循环级查询各自隔离**：任何一条抛异常都
        只降级它自己那一段，不向上抛、不带走另一段——``list_pending_
        delivery_tasks`` 失败时本轮没有别的事可做，直接返回 0；其余两条失败
        只降级自身，不影响正常候选的消费。
        """
        loop_healthy = True
        try:
            self._notify_stale_queued()
        except Exception as error:  # 见方法文档：只降级这一段，不带走本轮
            self._note_loop_failure(error, stage="queue_delay_hint")
            loop_healthy = False
        try:
            uncertain_tasks = self._queue.list_uncertain_delivery_tasks(limit=self._uncertain_limit)
        except Exception as error:  # 见方法文档：只降级这一段，不带走本轮
            self._note_loop_failure(error, stage="list_uncertain")
            uncertain_tasks = []
            loop_healthy = False
        for uncertain in uncertain_tasks:
            self._alert_deduped("dispatch_uncertain:" + uncertain.reserved_kind, uncertain.task_id)
        if len(uncertain_tasks) >= self._uncertain_limit:
            # `list_uncertain_delivery_tasks` 的 `LIMIT` 会让超过这个数量的
            # uncertain 任务静默无告警。这里没有条件做到精确判断"是否
            # 真的还有更多"（那需要多查一次或改成不设上限），只能在命中上限这个
            # 强信号出现时留一条能被搜到的日志，提醒有更多任务可能没被看到。
            logger.error(
                "投递消费：uncertain 任务数达到查询上限，可能还有未被告警的任务 count=%d",
                len(uncertain_tasks),
            )

        try:
            tasks = self._queue.list_pending_delivery_tasks(limit=self._limit)
        except Exception as error:  # 见方法文档：本轮无事可做，下一轮重来
            self._note_loop_failure(error, stage="list_pending")
            return 0
        for task in tasks:
            try:
                self._process_task(task)
            except Exception as error:  # 一个任务的异常不能带走同一轮的其他任务
                logger.error(
                    "投递消费单个任务异常，本轮其余任务不受影响 task_id=%s error=%s",
                    task.task_id,
                    type(error).__name__,
                )
                # 一轮抛穿＝这个名额白占了。同样按退避让路，否则一条每轮都抛的
                # 任务会一直排在 `ORDER BY created_at` 的最前面，把批量上限之外
                # 的健康任务挡在候选之外。**但抛穿不等于零进展**：游标已经推进过
                # 才抛的那种，退避两列刚被清零，再拿候选时刻读到的旧档位记一次，
                # 会让一条正在推进的投递白等最多一个退避窗口。
                if not self._advanced_this_task:
                    self._record_retry_backoff(task)
        if loop_healthy:
            # 单个任务的失败**不**计入循环级连续计数：它有自己的日志与告警路径，
            # 而且循环本身仍然健康——把两者混在一起会让"某个任务一直失败"伪装成
            # "整条循环坏了"。
            self._note_loop_recovered()
        return len(tasks)

    def _notify_stale_queued(self) -> None:
        """排队可感知：入队超过阈值仍未被领取时，补发一条排队提示。

        只在真正越过阈值时才发，不产生噪音；只发一次，去重靠进程内内存
        集合，不新增数据库列或表——这是一条尽力而为的体验提示。不是 outbox
        事件、不经过 ``CardStream``：只是一次独立的 ``Replies.send_text``
        调用。``getattr`` 取可选队列方法：旧的注入式假队列没有这个方法时
        整体跳过，行为与本功能加入之前逐字节一致。
        """
        list_stale = getattr(self._queue, "list_stale_queued_tasks", None)
        if list_stale is None:
            return
        stale = list_stale(
            older_than=timedelta(seconds=self._queue_delay_hint_seconds),
            limit=self._queue_delay_hint_limit,
        )
        current_ids = {row.task_id for row in stale}
        # 收缩：不再出现在候选集合里的 id（已被领取、或已经等到更长阈值的
        # `queued_timeout` 收口）不需要继续占内存。
        self._queue_delay_notified &= current_ids
        if not stale:
            return
        content = self._catalog.text("gateway.busy_hint_queued")
        for row in stale:
            if row.task_id in self._queue_delay_notified:
                continue
            try:
                self._texts.send_text(
                    chat_id=row.chat_id,
                    thread_id=row.thread_id,
                    reply_to_message_id=row.reply_to_message_id or "",
                    text=content.text,
                )
            except Exception as error:  # 单条发送失败不影响其余候选，下一轮重试
                logger.error(
                    "排队提示发送失败，下一轮重试 task_id=%s error=%s",
                    row.task_id,
                    type(error).__name__,
                )
                continue
            self._queue_delay_notified.add(row.task_id)

    def _prior_progress_history(self, task: Any) -> tuple[ProgressStepSnapshot, ...]:
        """重建这个任务在本轮批次**之前**已经持久化的进度信号历史。

        本消费循环按批次轮询，``_process_task`` 每次都会重新构造一个全新的
        ``CardStream``，没有跨轮次的进程内状态；累积正文因此靠重放 outbox
        里已写入的进度事件重建，与本轮内存里继续累积拼接，效果与"从任务一
        开始就用同一个 CardStream 实例"一致。只取
        ``sequence <= task.consumed_sequence`` 的部分，避免同一条事件被
        算两次；``consumed_sequence == 0``（新任务第一轮）直接跳过查询。
        """
        if task.consumed_sequence <= 0:
            return ()
        try:
            full_history = self._queue.read_delivery_events(task_id=task.task_id, after_sequence=0)
        except Exception as error:  # 重建失败不能带走本轮正常消费；
            # 退回空历史——最坏情况是这一次的卡片正文从当前信号重新开始累积
            # （与本 Story 改动前的单行行为一致），不是任务失败、不影响终态。
            logger.error(
                "进度累积历史重建失败，本轮卡片正文从当前信号重新开始 task_id=%s error=%s",
                task.task_id,
                type(error).__name__,
            )
            return ()
        snapshots: list[ProgressStepSnapshot] = []
        for record in full_history:
            if record.sequence > task.consumed_sequence:
                break  # 按 sequence 升序返回；越过已消费边界后交给本轮主循环处理
            if record.event_type not in ("progress", "safely_releasable_answer"):
                continue
            action, query_count, query_step = decode_progress_action(record.content)
            snapshots.append(
                ProgressStepSnapshot(
                    elapsed_seconds=record.elapsed_seconds or 0,
                    action=action,
                    query_count=query_count,
                    query_step=query_step,
                )
            )
        return tuple(snapshots)

    def _process_task(self, task: Any) -> None:
        self._advanced_this_task = False
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
            initial_progress_history=self._prior_progress_history(task),
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
                # terminal 是该任务在 outbox 里唯一一条终态事件（重复终态被
                # 拒绝），处理完不会再有后续事件需要看。
                break

        receipt = self._terminal_receipt(task, stream, saw_unconsumed_events=bool(events))
        if receipt is not None:
            self._maybe_confirm(task, receipt)

    def _terminal_receipt(
        self, task: Any, stream: CardStream, *, saw_unconsumed_events: bool
    ) -> TerminalReceipt | None:
        """算出"这一条终态确实已经落到某条通道"的回执；拿不出证据就返回 ``None``。

        只有两条来源：**本轮现场取得**（``stream.terminal_receipt``——终态卡片更新
        成功，或文本兜底真的拿回 ``message_id``），以及**更早一轮已取得、只差确认**
        （本轮没有任何未消费事件、任务已在 ``awaiting_delivery``、投递标识已落库）。
        后者成立是因为消费游标只在某条通道**确定受理**终态之后才会越过那条事件，它
        让"已送达但确认没成功"的任务下一轮仍会被重新确认。其余一切情况都没有回执：
        两条通道都失败、预留位没抢到、结果不明——这些轮次里 ``stream.message_id``
        可能只是建进度卡那一轮的旧值（`V-投递-03`）。
        """
        fresh = stream.terminal_receipt
        if fresh is not None:
            return fresh
        if saw_unconsumed_events:
            # 本轮确实读到过未消费事件，却没能取得回执：终态没有落地（或者根本
            # 还没走到终态）。这一轮不确认，等下一轮重新处理同一条事件。
            return None
        if task.status != "awaiting_delivery" or task.message_id is None:
            return None
        return TerminalReceipt(
            kind="text" if task.fallback_text else "card", message_id=task.message_id
        )

    def _handle_started(self, task: Any, stream: CardStream, event: Any) -> bool:
        """返回是否可以继续处理本轮的后续事件（`False` 表示预留位没抢到）。"""
        if stream.card_id is None and not stream.fallback_needed:
            if not self._queue.reserve_dispatch(task_id=task.task_id, kind="card_create"):
                # 抢不到预留位：要么上一轮已经处理到一半崩溃（uncertain，本轮不碰），
                # 要么任务已经不在可处理状态。两种情况都不该继续外发，直接返回，
                # 这个 started 事件的游标本轮不推进，下一轮重新评估。
                return False
            try:
                stream.start()
            except Exception as error:  # 白名单反转：只吞掉明确失败
                # 其余一切异常都原样抛出到这里——服务端可能已经处理，不清
                # 预留位、不推进游标、不降级，转入既有 uncertain 告警路径。
                logger.error(
                    "建卡结果不明，转入 uncertain 等待人工核对 task_id=%s error=%s",
                    task.task_id,
                    type(error).__name__,
                )
                return False
        self._advance_delivery_cursor(
            task_id=task.task_id,
            consumed_sequence=event.sequence,
            card_id=stream.card_id,
            message_id=stream.message_id,
            fallback_text=stream.fallback_needed,
        )
        return True

    def _handle_progress(self, task: Any, stream: CardStream, event: Any) -> None:
        # `safely_releasable_answer` 走同一条状态区更新路径：这里先保证
        # "消费到了、游标推进了"是安全、不会重复的（见模块说明）。
        # `event.content` 编码语义化进度状态：worker 侧写入，这里对称解码；
        # 没有内容或不在白名单里时 `decode_progress_action` 退回空值，
        # 已在其内部把关，这里不用再重复过滤。
        action, query_count, query_step = decode_progress_action(event.content)
        stream.update(
            elapsed_seconds=event.elapsed_seconds or 0,
            action=action,
            query_count=query_count,
            query_step=query_step,
        )
        try:
            self._advance_delivery_cursor(
                task_id=task.task_id,
                consumed_sequence=event.sequence,
                card_sequence=stream.sequence,
                fallback_text=stream.fallback_needed,
            )
        except Exception as error:  # 记一条告警后照常向上抛，交给 run_once 隔离
            # 已知残留风险（未完全消除）：`stream.update()` 已真实发出、序号已
            # 前进，但持久化失败意味着 `card_seq` 没落库；下一轮重放会用落后
            # 序号被 CardKit 拒绝、降级为文本通道——不产生重复投递，只是让
            # 用户看到一张卡停在"正在处理"。这里不引入预留位完全消除这个
            # 窗口（成本需单独评估），只保证它不再是零告警的静默永久降级。
            self._alert_deduped("progress_persist_failed:" + type(error).__name__, task.task_id)
            raise

    def _finish_card_channel(
        self, task: Any, stream: CardStream, event: Any, content: RenderedContent, elapsed: float
    ) -> _CardFinishOutcome:
        """处理卡片终态更新+关闭这两次外部调用；返回三态结果给调用方定夺。

        卡片路径不活跃时直接 ``CONTINUE``。这两次调用整体纳入预留位保护
        （见模块说明），不能只靠 CardKit 的 sequence 拒绝防重复。
        ``finish()`` 只吞掉明确失败，其余异常（结果不明）转 ``EARLY_FALSE``。
        **只在降级时提前清空预留位**：成功路径刻意不清空，继续持有到方法
        末尾把进度推进与预留位清空放进同一次写入，否则中间一次瞬时错误会
        让预留位先清空、游标却停在旧值，下一轮重放已用掉的序号，对已成功
        送达的答案又发一条重复的文本终态。
        """
        if stream.card_id is None or stream.fallback_needed:
            return _CardFinishOutcome.CONTINUE
        if not self._queue.reserve_dispatch(task_id=task.task_id, kind="card_finish"):
            # 抢不到：要么上一轮处理到一半崩溃（uncertain，本轮不碰），要么任务
            # 状态发生了竞态。两种情况都不该再调用 finish()。
            return _CardFinishOutcome.HALT
        try:
            if event.terminal_kind == TerminalKind.SUCCESS.value:
                stream.finish(result=event.content or "", elapsed_seconds=elapsed)
            else:
                stream.finish(failure=content, elapsed_seconds=elapsed)
        except DeliveryUncertainError as error:
            return self._handle_uncertain_card_finish(task, error)
        except Exception as error:  # 白名单反转：只吞掉明确失败
            logger.error(
                "终态卡片更新结果不明，转入 uncertain 等待人工核对 task_id=%s error=%s",
                task.task_id,
                type(error).__name__,
            )
            return _CardFinishOutcome.HALT
        if stream.fallback_needed:
            self._queue.clear_dispatch_reservation(task_id=task.task_id)
        return _CardFinishOutcome.CONTINUE

    def _handle_uncertain_card_finish(
        self, task: Any, error: DeliveryUncertainError
    ) -> _CardFinishOutcome:
        """终态卡片更新拿到"结果不明"：**一律保留预留位**，转人工核对并告警。

        **卡片通道没有"可安全重投"这一档**，与 :meth:`_send_fallback` 同姿态：CardKit 的
        整卡级 ``sequence`` 是操作序号不是去重键（同一序号重投会被判成落后序号拒绝、随即
        降级到文本通道，而卡片里很可能已经有那份答案，用户于是收到第二份完整答案），请求
        体里也没有 ``uuid``——见 ``core.delivery.ports`` 的 ``card_update``/``card_close``。
        因此不清预留位、不推进游标、不确认送达：任务此后每轮都抢不到预留位，停在
        ``awaiting_delivery``，由 ``expire_undelivered_terminals`` 在二十四小时后收敛成
        "投递已过期，请重新提问"。这是常态出口，只写日志等于把一件要人来看的事埋掉，必须告警。
        """
        logger.error(
            "终态卡片更新结果不明，转入 uncertain 等待人工核对 task_id=%s reason=%s",
            task.task_id,
            error.reason,
        )
        self._alert_deduped("card_finish_uncertain:" + error.reason, task.task_id)
        return _CardFinishOutcome.HALT

    def _handle_terminal(self, task: Any, stream: CardStream, event: Any) -> None:
        """处理这一轮的终态事件。

        **不再返回"能不能确认"**：能不能确认只看有没有本次终态的回执，由
        :meth:`_terminal_receipt` 统一裁定。这里只负责把终态推给卡片通道、
        必要时推给文本兜底，并且只在某条通道**确定受理**之后才推进消费游标
        ——游标越过终态事件这件事本身，就是"终态已落地"的持久化证据。
        """
        content = RenderedContent(
            key="delivery.terminal", version=self._catalog.version, text=event.content or ""
        )
        elapsed = event.elapsed_seconds or 0

        card_outcome = self._finish_card_channel(task, stream, event, content, elapsed)
        if card_outcome is not _CardFinishOutcome.CONTINUE:
            # 预留位没抢到，或终态卡片更新结果不明：两种情况都不推进游标，
            # terminal 事件下一轮重新评估。
            return

        if stream.fallback_needed:
            outcome = self._send_fallback(task, stream, content)
            if outcome is not _FallbackOutcome.SENT:
                # 预留位没抢到，或外发同步捕获到明确失败：这一轮不推进游标，
                # terminal 事件下一轮（退避窗口过后）重新处理。
                return

        self._advance_delivery_cursor(
            task_id=task.task_id,
            consumed_sequence=event.sequence,
            card_id=stream.card_id,
            message_id=stream.message_id,
            card_sequence=stream.sequence,
            fallback_text=stream.fallback_needed,
        )

    def _send_fallback(
        self, task: Any, stream: CardStream, content: RenderedContent
    ) -> _FallbackOutcome:
        # 退避窗口本身不在这里判：这个任务能被读成本轮候选，就已经说明窗口过了
        # （`list_pending_delivery_tasks` 按 `delivery_retry_after` 过滤）。判据
        # 只留一处，才不会出现"库里说可以试、进程内的旧簿记说不行"这种分歧。
        if not self._queue.reserve_dispatch(task_id=task.task_id, kind="text_send"):
            return _FallbackOutcome.RETRY_LATER
        try:
            stream.send_fallback(content)
        except DeliveryRejectedError as error:
            # 明确失败（白名单）：清预留位，下一轮按退避重试。
            self._queue.clear_dispatch_reservation(task_id=task.task_id)
            self._record_retry_backoff(task)
            self._alert_deduped("fallback_send_failed:" + type(error).__name__, task.task_id)
            return _FallbackOutcome.RETRY_LATER
        except Exception as error:  # 结果不明（白名单反转）：服务端可能
            # 已经受理并投递——不清预留位、不进入重试退避（那等于"下一轮原样
            # 重试"，会造成重复投递）。转入既有 uncertain 告警路径。**文本通道
            # 没有"可安全重投"这一档**：请求体里没有任何平台幂等键（见
            # ``core.delivery.ports`` 的 ``text_send`` 一行），因此这里不区分
            # ``DeliveryUncertainError.retry_safe``，一律保留预留位转人工核对。
            logger.error(
                "文本兜底发送结果不明，转入 uncertain 等待人工核对 task_id=%s error=%s",
                task.task_id,
                type(error).__name__,
            )
            return _FallbackOutcome.UNCERTAIN
        return _FallbackOutcome.SENT

    def _maybe_confirm(self, task: Any, receipt: TerminalReceipt) -> None:
        """凭**本次终态的回执**确认送达。

        调用方已经用 :meth:`_terminal_receipt` 拿到了回执；这里只再核一次任务
        状态快照——``task.status`` 是本轮开始时读到的值，只有它已经是
        ``awaiting_delivery`` 才说明 Worker 真的写过终态事件（写事件与转状态在
        同一事务提交）。回执里的 ``message_id`` 与 ``kind`` 一律取自回执本身，
        不再回头去读 ``stream``：那正是旧实现把建进度卡时的旧标识当成终态凭据
        的入口。
        """
        if task.status != "awaiting_delivery":
            return
        try:
            confirmed = self._queue.confirm_delivery(
                task_id=task.task_id,
                platform_message_kind=receipt.kind,
                platform_message_id=receipt.message_id,
            )
        except Exception as error:  # 记录后交给下一轮重试
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

    def run_forever(
        self,
        *,
        stop: threading.Event,
        poll_interval_seconds: float,
        heartbeat: Callable[[], None] | None = None,
        on_tick: Callable[[], None] | None = None,
    ) -> None:
        """长期循环：每轮之间固定等待一个轮询间隔。

        没有独立的 NOTIFY 监听，轮询间隔是唯一的发现机制，刻意不做"处理满
        批就立即再跑一轮"的优化，避免变成不带退避的忙轮询。``heartbeat``/
        ``on_tick`` 每轮都调用一次。**本循环不因异常退出**：唯一的退出条件
        是 ``stop`` 被置位——outbox 是持久的，主动退出反而把可自愈的瞬时
        故障变成必须人工重启才能恢复的停摆；连续失败由 ``_note_loop_
        failure`` 计数并在阈值处上报。
        """
        while not stop.is_set():
            if heartbeat is not None:
                try:
                    heartbeat()
                except Exception as error:  # 心跳失败不能带走投递职责
                    logger.error("投递消费心跳记录失败 error=%s", type(error).__name__)
            if on_tick is not None:
                try:
                    on_tick()
                except Exception as error:  # 告警自身失败不能带走投递职责
                    logger.error("投递消费告警状态机推进失败 error=%s", type(error).__name__)
            try:
                self.run_once()
            except Exception as error:  # 一轮异常不得带走整条循环
                # `run_once` 内部已经隔离了两条循环级查询与每个任务，走到这里的是
                # 它们之外的异常（例如注入的告警回调自己抛错）。同样只降级这一轮。
                self._note_loop_failure(error, stage="run_once")
            stop.wait(poll_interval_seconds)
        logger.info("投递消费循环已停止")


def build_delivery_consumer(
    *,
    client: Any,
    queue: Any,
    cards: CardTransport | None = None,
    on_send_outcome: SendOutcomeCallback | None = None,
    on_alert: AlertCallback | None = None,
    limit: int = 20,
    queue_delay_hint_seconds: float = DeliveryConsumer.DEFAULT_QUEUE_DELAY_HINT_SECONDS,
) -> DeliveryConsumer:
    """按已经建好的飞书 SDK 客户端与队列适配器装配消费者。

    ``client`` 同时暴露 ``.im`` 与 ``.cardkit`` 两个命名空间，一个客户端即可
    同时驱动卡片与文本兜底，不需要为投递单独建第二套鉴权。``cards`` 默认
    ``None`` 时装配真实 ``LarkCardTransport(client)``；唯一的调用方只在显式
    命中受控验收开关时才传入一个"确定性拒绝"的实现，不是给业务代码开的通用
    注入口。``queue_delay_hint_seconds`` 未显式传入时使用
    ``DeliveryConsumer`` 自己的默认值。
    """
    from lingxi.adapters.feishu_delivery import LarkCardTransport, LarkDeliveryText

    return DeliveryConsumer(
        queue=queue,
        cards=cards if cards is not None else LarkCardTransport(client),
        texts=LarkDeliveryText(client),
        on_send_outcome=on_send_outcome,
        on_alert=on_alert,
        limit=limit,
        queue_delay_hint_seconds=queue_delay_hint_seconds,
    )
