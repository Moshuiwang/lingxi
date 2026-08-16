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
同样纳入预留位保护（迁移 0060 头部注释）。**进程崩溃、或外发调用抛出任何不是
``core.execution.card_stream.DeliveryRejected`` 的异常（结果不明——JSON 解析
失败、响应缺失可回读标识、``OSError`` 类网络异常等，见该模块说明的独立审核
R-1 白名单）发生在提交预留位与清空之间时**，下一轮
``list_pending_delivery_tasks`` 会把这个任务排除在正常消费之外，
``list_uncertain_delivery_tasks`` 把它路由给告警——消费循环不会替它猜测上一次调用
是否成功后自动重发（issue 状态合同第 6 条）。**只有服务端明确拒绝**（真实 adapter
`response.success()` 为假、业务错误码明确表示未受理时抛出的 ``DeliveryRejected``）
**才走 `clear_dispatch_reservation` 立即清空、允许下一轮重试**；除它以外的一切
异常都归结果不明，绝不清空，统一转 ``uncertain``。

**人工恢复（独立审核 B-2 补全，2026-08-14）**：处理一个 ``uncertain`` 任务之前，
必须先核对对应的外发调用**是否实际已经成功**，核对手段按 ``reserved_kind``：

- ``card_create``：按话题（``chat_id``/``thread_id``）回读飞书侧该会话的最新消息，
  确认是否已经出现一张对应本次问题的卡片消息；找到即可读出其真实 ``card_id`` /
  ``message_id`` 作为幂等标识核对依据。
- ``card_finish``（独立审核 R-2 修正，2026-08-14：此前写的「按 card_id 回读卡片」
  指向一个不存在的接口——CardKit 没有单独的"读卡片当前内容"接口）：建卡阶段已经
  成功，任务的 ``delivery_message_id``（建卡时唯一拿到的消息级可回读标识）已经
  持久化——按这个 ``message_id`` 回读该条消息，人工目视核对卡片当前显示的内容
  是否已经是终态正文（成功结果或失败提示），而不是仍停在处理中的中间状态。
- ``text_send``：按话题回读飞书侧该会话的最新消息，核对是否已经出现一条内容匹配
  的文本终态（没有平台原生幂等标识，只能按内容 + 时间窗口人工比对）。

核对结论决定动作，且只有这两种：

1. **已经送达**（回读证实卡片/文本确实已经到达用户）：**不得**调用
   ``clear_dispatch_reservation`` 后任由消费循环重放——那会照搬本 Story 修复的
   红线场景，重放一条已经成功的终态。正确动作是把回读到的标识通过
   ``record_delivery_progress`` 直接写回（``card_id``/``message_id`` 参数，
   卡片 sequence 走 ``card_sequence`` 参数，连同这条 ``terminal``/``started``
   事件的 ``sequence`` 作为 ``consumed_sequence``），该调用本身会原子清空预留位
   并推进游标，让任务在下一轮被 ``confirm_delivery`` 正常确认送达——不重放任何
   外部调用。**``reserved_kind`` 是 ``text_send`` 时，写回必须同时传
   ``fallback_text=True``**（独立审核 R-3 修正，2026-08-14：不传时默认
   ``False``）——下一轮 ``_maybe_confirm`` 按 ``stream.fallback_needed``（即
   持久化的 ``task.fallback_text``）决定 ``confirm_delivery`` 的
   ``platform_message_kind`` 写 ``"card"`` 还是 ``"text"``，漏传会把一条实际走
   文本通道送达的记录误标成卡片，是记账错标。``card_create``/``card_finish``
   场景则保持 ``fallback_text=False``（或不传，沿用既有默认）。
2. **确认未送达**（回读证实没有任何卡片/消息产生，或产生的内容与本次任务不符）：
   这才允许调用 ``clear_dispatch_reservation`` 清空预留位，交给下一轮按既有逻辑
   重新外发。

**已知残留风险：G-CARD 10 分钟流式自动关闭**（独立审核 P2-5，#162 评论
5290545953/5291111636 实测；[验收矩阵](../../../../docs/技术设计/验收矩阵.md)
``V-卡片-02``/``V-卡片-03`` 补充段落有同一份登记）。未手动关闭的流式卡片距上次
开启 10 分钟后由平台自动关闭；本消费循环允许卡片跨任意时长恢复（任务寿命只受
24 小时到期约束，问数任务运行超过 10 分钟并不罕见）。一旦流式已经被平台自动
关闭，恢复后的 ``update``/``close`` 会失败——中途 ``update`` 失败按既有语义整体
降级为文本兜底（不构成重复投递），用户会看到一张停在某个中间状态的旧卡片、
外加一条文本答案。未消除，留待 Bot-Test/Stage 真实验证平台侧关闭行为后再评估。
"""

from __future__ import annotations

import logging
import threading
import time
from enum import Enum
from typing import Any, Callable

from lingxi.config.content import ContentCatalog, RenderedContent, default_content_catalog
from lingxi.core.delivery.ports import TerminalKind
from lingxi.core.execution.card_stream import (
    CardRateLimiter,
    CardStream,
    CardTransport,
    DeliveryRejected,
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


class _FallbackOutcome(Enum):
    """``DeliveryConsumer._send_fallback`` 的结果分类。

    独立审核 B-1 之前只有两种结果（成功 / 失败），用 ``bool`` 就够；现在需要
    第三种——"结果不明"——单独区分开，因为它决定 `_handle_terminal` 之后能不能
    安全尝试 `_maybe_confirm`（见该方法文档），而 `RETRY_LATER` 的几种成因
    （退避未到期、预留位没抢到、明确失败）都不影响这一点，与本次修复之前的
    既有行为保持一致。
    """

    SENT = "sent"
    RETRY_LATER = "retry_later"
    UNCERTAIN = "uncertain"


class DeliveryConsumer:
    """一轮读若干候选任务、逐个驱动到底；异常按任务隔离，一个任务失败不带走整轮。"""

    # 独立审核 P2-4：uncertain 告警与文本兜底重试原来完全没有退避，默认
    # `poll_interval=1.0s` 下会变成每秒一次的告警/外发洪流。这两个默认值把它们
    # 收敛到"仍然会被看见，但不再洪泛"——具体数值不是硬性产品承诺，可按需调。
    DEFAULT_ALERT_MIN_INTERVAL_SECONDS = 300.0
    DEFAULT_FALLBACK_BACKOFF_BASE_SECONDS = 2.0
    DEFAULT_FALLBACK_BACKOFF_CAP_SECONDS = 300.0

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
        fallback_backoff_base_seconds: float = DEFAULT_FALLBACK_BACKOFF_BASE_SECONDS,
        fallback_backoff_cap_seconds: float = DEFAULT_FALLBACK_BACKOFF_CAP_SECONDS,
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
        self._uncertain_limit = uncertain_limit
        self._monotonic = monotonic
        self._alert_min_interval_seconds = alert_min_interval_seconds
        self._fallback_backoff_base_seconds = fallback_backoff_base_seconds
        self._fallback_backoff_cap_seconds = fallback_backoff_cap_seconds
        # 进程内、非持久化状态：只用来给告警与外发重试限速，重启后清零属于可接受
        # 的降级（重启本身就已经是一次重新评估的机会）。
        self._last_alerted_at: dict[tuple[str, str], float] = {}
        self._fallback_attempts: dict[str, int] = {}
        self._fallback_next_attempt_at: dict[str, float] = {}

    def _alert_deduped(self, kind: str, task_id: str) -> None:
        """同一个 (kind, task_id) 在 `alert_min_interval_seconds` 内只告警一次。

        独立审核 P2-4：默认 1 秒轮询下不去重会变成约 8.6 万条/天/任务的告警噪音，
        真实告警路由接上后就是刷屏。这里只做"降频"，不做"消音"——问题仍然会被
        看见，只是不再淹没其它信号；数据库里的预留位本身没有变化，人工核对的
        判断依据不受影响。
        """

        key = (kind, task_id)
        now = self._monotonic()
        last = self._last_alerted_at.get(key)
        if last is not None and now - last < self._alert_min_interval_seconds:
            return
        self._last_alerted_at[key] = now
        self._alert(kind, task_id)

    def _fallback_backoff_ready(self, task_id: str) -> bool:
        deadline = self._fallback_next_attempt_at.get(task_id)
        return deadline is None or self._monotonic() >= deadline

    def _record_fallback_attempt_failed(self, task_id: str) -> None:
        attempts = self._fallback_attempts.get(task_id, 0) + 1
        self._fallback_attempts[task_id] = attempts
        backoff = min(
            self._fallback_backoff_base_seconds * (2 ** (attempts - 1)),
            self._fallback_backoff_cap_seconds,
        )
        self._fallback_next_attempt_at[task_id] = self._monotonic() + backoff

    def _clear_fallback_backoff(self, task_id: str) -> None:
        self._fallback_attempts.pop(task_id, None)
        self._fallback_next_attempt_at.pop(task_id, None)

    def run_once(self) -> int:
        """跑一轮：先报告仍然卡在"外发前预留位"里的任务，再处理正常候选。
        返回本轮实际处理的候选任务数（不含仅被告警的 uncertain 任务）。
        """

        uncertain_tasks = self._queue.list_uncertain_delivery_tasks(limit=self._uncertain_limit)
        for uncertain in uncertain_tasks:
            self._alert_deduped(
                "dispatch_uncertain:" + uncertain.reserved_kind, uncertain.task_id
            )
        if len(uncertain_tasks) >= self._uncertain_limit:
            # 独立审核 P2-4：`list_uncertain_delivery_tasks` 的 `LIMIT` 会让超过
            # 这个数量的 uncertain 任务静默无告警。这里没有条件做到精确判断"是否
            # 真的还有更多"（那需要多查一次或改成不设上限），只能在命中上限这个
            # 强信号出现时留一条能被搜到的日志，提醒有更多任务可能没被看到。
            logger.error(
                "投递消费：uncertain 任务数达到查询上限，可能还有未被告警的任务 count=%d",
                len(uncertain_tasks),
            )

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

        confirm_safe = True
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
                confirm_safe = self._handle_terminal(task, stream, event)
                # terminal 是该任务在 outbox 里唯一一条终态事件（#151 状态合同：
                # 重复终态被拒绝），处理完不会再有后续事件需要看。
                break

        if confirm_safe:
            self._maybe_confirm(task, stream)
        # confirm_safe 为假只发生在独立审核 B-1 的"结果不明"分支：此时
        # `stream.message_id` 可能只是更早建卡时就拿到、与这一轮真正想确认的
        # 终态结果无关的旧值，用它调用 `confirm_delivery` 会把一个应该转
        # `uncertain` 的任务提前误标成已确认送达（`list_uncertain_delivery_tasks`
        # 只看 `status IN ('running','awaiting_delivery')`，一旦被误标成
        # `succeeded`/`failed` 就会从这条告警路径里永久消失，而预留位却还留着，
        # 变成一个无人再看得到的孤儿）。

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
            except Exception as error:  # noqa: BLE001 - 白名单反转（独立审核 R-1）：
                # `CardStream.start()` 只吞掉 `DeliveryRejected`（明确失败），其余
                # 一切异常（JSON 解析失败/响应缺失可回读标识/网络类异常等）都原样
                # 抛出到这里——服务端可能已经处理，不清预留位、不推进游标、不降级。
                # 预留位原样留在数据库里，转入既有 uncertain 告警路径，等待人工核对。
                logger.error(
                    "建卡结果不明，转入 uncertain 等待人工核对 task_id=%s error=%s",
                    task.task_id,
                    type(error).__name__,
                )
                return False
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
        try:
            self._queue.record_delivery_progress(
                task_id=task.task_id,
                consumed_sequence=event.sequence,
                card_sequence=stream.sequence,
                fallback_text=stream.fallback_needed,
            )
        except Exception as error:  # noqa: BLE001 - 记一条告警后照常向上抛，交给 run_once 隔离
            # 独立审核 P2-3（已知残留风险，未完全消除）：`stream.update()` 的外部
            # 调用已经真实发出、序号已经在内存里前进，但这次持久化失败意味着
            # `card_seq` 没有落库。下一轮如果把这个 progress 事件当正常候选重放，
            # 会用落后的序号再调用一次 `update()`，被 CardKit 拒绝、永久降级为
            # 文本通道——不产生重复投递（进度帧本身不是"结果"），但会让用户看到
            # 一张卡在"正在处理"的卡片。这里没有像终态/建卡那样引入预留位来完全
            # 消除这个窗口（会给每个 progress 帧都加两次额外的数据库往返，成本/
            # 收益需要单独评估），只先保证它不再是"零告警的静默永久降级"。
            self._alert_deduped(
                "progress_persist_failed:" + type(error).__name__, task.task_id
            )
            raise

    def _handle_terminal(self, task: Any, stream: CardStream, event: Any) -> bool:
        """处理这一轮的终态事件。返回值是"这一轮结束后尝试 `_maybe_confirm`
        是否安全"——只有独立审核 B-1 的"结果不明"分支会返回 ``False``：此时
        ``stream.message_id`` 可能只是更早建卡时就拿到、与这一轮终态结果无关的
        旧值，`_maybe_confirm` 用它去 `confirm_delivery` 会把一个应该转
        ``uncertain`` 的任务提前误标成已确认送达（见 `_process_task` 的调用点）。
        除此之外的所有分支（含预留位没抢到、明确失败）都返回 ``True``，与本次
        修复之前"`_handle_terminal` 之后总是尝试 `_maybe_confirm`"的既有行为
        完全一致。
        """

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
                return True
            try:
                if event.terminal_kind == TerminalKind.SUCCESS.value:
                    stream.finish(result=event.content or "", elapsed_seconds=elapsed)
                else:
                    stream.finish(failure=content, elapsed_seconds=elapsed)
            except Exception as error:  # noqa: BLE001 - 白名单反转（独立审核 R-1）：
                # `CardStream.finish()` 的终态更新只吞掉 `DeliveryRejected`（明确
                # 失败），其余一切异常都原样抛出到这里——平台可能已经处理，不能猜
                # 是成功还是失败。不清预留位、不降级、不推进游标，转入既有
                # uncertain 告警路径，等待人工核对。
                logger.error(
                    "终态卡片更新结果不明，转入 uncertain 等待人工核对 task_id=%s error=%s",
                    task.task_id,
                    type(error).__name__,
                )
                return False
            # finish() 内部吞掉了明确失败（card_stream.py 的既有设计；结果不明的
            # 异常会原样抛出，已经在上面 except 里处理），走到这里就是拿到了
            # 明确结果：成功，或已经清晰地降级为需要文本兜底。
            #
            # **只在降级时提前清空预留位**（独立审核 P1-1，2026-08-14 修复）：降级
            # 意味着卡片终态还没有确定送达，需要立即腾出预留位给下面的
            # `_send_fallback` 去拿 `text_send`；此时清空不产生重复投递风险，因为
            # 卡片本来就没有成功。**成功路径刻意不在这里清空**——继续持有
            # `card_finish` 预留位，直到方法末尾的 `record_delivery_progress` 把
            # "进度已经推进"与"预留位被清空"放进同一次写入。如果不这样做：终态
            # 更新+关闭全部成功之后、`record_delivery_progress` 落库之前发生一次
            # 普通瞬时错误（无需进程崩溃），预留位会先被清空、游标却还停在旧值；
            # 下一轮会把这个任务当正常候选、用落后的 `card_seq` 重放已经用掉的
            # 序号，被 CardKit 拒绝、误判成"卡片链路整体失败"，对一个其实已经成功
            # 送达的答案又发一条重复的文本终态——这正是本次审核复现的红线场景。
            # 预留位继续持有则让同样的崩溃/瞬时错误落到 `list_uncertain_delivery_tasks`
            # 的告警路径，不会被下一轮自动重放。
            if stream.fallback_needed:
                self._queue.clear_dispatch_reservation(task_id=task.task_id)

        if stream.fallback_needed:
            outcome = self._send_fallback(task, stream, content)
            if outcome is not _FallbackOutcome.SENT:
                # 预留位没抢到、退避未到期，或外发同步捕获到明确失败：这一轮不
                # 推进游标，terminal 事件下一轮重新处理（`_send_fallback` 已经在
                # 明确失败时清了预留位）。只有 `UNCERTAIN`（独立审核 B-1）时
                # `_maybe_confirm` 才不安全——见本方法文档。
                return outcome is not _FallbackOutcome.UNCERTAIN

        self._queue.record_delivery_progress(
            task_id=task.task_id,
            consumed_sequence=event.sequence,
            card_id=stream.card_id,
            message_id=stream.message_id,
            card_sequence=stream.sequence,
            fallback_text=stream.fallback_needed,
        )
        return True

    def _send_fallback(
        self, task: Any, stream: CardStream, content: RenderedContent
    ) -> _FallbackOutcome:
        if not self._fallback_backoff_ready(task.task_id):
            # 独立审核 P2-4：明确失败原来是"清预留位、下一轮原样重试"，默认
            # 1 秒轮询下等于对飞书出站接口每秒重试一次，最长可以持续到 24 小时
            # 到期——真实平台限流会先被打上去。这里不改变"最终会重试"这个语义，
            # 只是不在退避窗口内再去抢预留位、不产生新的外发尝试。
            return _FallbackOutcome.RETRY_LATER
        if not self._queue.reserve_dispatch(task_id=task.task_id, kind="text_send"):
            return _FallbackOutcome.RETRY_LATER
        try:
            stream.send_fallback(content)
        except DeliveryRejected as error:
            # 明确失败（白名单，独立审核 R-1）：清预留位，下一轮按退避重试。
            self._queue.clear_dispatch_reservation(task_id=task.task_id)
            self._record_fallback_attempt_failed(task.task_id)
            self._alert_deduped("fallback_send_failed:" + type(error).__name__, task.task_id)
            return _FallbackOutcome.RETRY_LATER
        except Exception as error:  # noqa: BLE001 - 结果不明（白名单反转，独立审核
            # R-1）：JSON 解析失败/响应缺失可回读标识/网络类异常等，服务端可能
            # 已经受理并投递——不清预留位、不进入重试退避（那等于"下一轮原样
            # 重试"，正是本次复现的重复投递）。转入既有 uncertain 告警路径。
            logger.error(
                "文本兜底发送结果不明，转入 uncertain 等待人工核对 task_id=%s error=%s",
                task.task_id,
                type(error).__name__,
            )
            return _FallbackOutcome.UNCERTAIN
        self._clear_fallback_backoff(task.task_id)
        return _FallbackOutcome.SENT

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

    def run_forever(
        self,
        *,
        stop: threading.Event,
        poll_interval_seconds: float,
        heartbeat: Callable[[], None] | None = None,
        on_tick: Callable[[], None] | None = None,
    ) -> None:
        """长期循环：每轮之间固定等待一个轮询间隔。

        与 ``apps/worker/service.py`` 的 ``run()`` 同一惯例——没有独立的 NOTIFY 监听
        （outbox 事件的写入方是 Worker 进程，不在本进程内），轮询间隔是唯一的发现
        机制。**刻意不做"这一轮处理满批就立即再跑一轮"的优化**：那样在一批任务反复
        遇到同一个真实失败（而不是积压）时会变成不带退避的忙轮询，对数据库与飞书
        出站接口造成持续压力；积压场景下晚一个轮询间隔被拾取，代价可以接受。

        ``heartbeat``/``on_tick``（Issue #153）每轮都调用一次，且在 ``stop`` 已置位
        时也会跳过——语义与 ``WorkerService.process_once`` 的
        ``_emit_heartbeat``/``_tick_alerts`` 一致：本循环独立于长连接主线程，
        必须有自己的活性信号，否则长连接仍在正常收信、而这条后台线程已经卡死时，
        进程级心跳会撒谎说"健康"。
        """

        while not stop.is_set():
            if heartbeat is not None:
                try:
                    heartbeat()
                except Exception as error:  # noqa: BLE001 - 心跳失败不能带走投递职责
                    logger.error("投递消费心跳记录失败 error=%s", type(error).__name__)
            if on_tick is not None:
                try:
                    on_tick()
                except Exception as error:  # noqa: BLE001 - 告警自身失败不能带走投递职责
                    logger.error("投递消费告警状态机推进失败 error=%s", type(error).__name__)
            self.run_once()
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
) -> DeliveryConsumer:
    """按已经建好的飞书 SDK 客户端与队列适配器装配消费者。

    ``client`` 传入的是各调用方自行建好的飞书 SDK 客户端实例——飞书 SDK 客户端本身
    同时暴露 ``.im`` 与 ``.cardkit`` 两个命名空间，一个客户端即可同时驱动卡片与
    文本兜底，不需要为投递单独建第二套鉴权。

    ``cards``（S-A-07 受控验收缺口，Issue #152/#154）默认 ``None``：这时装配的是
    真实 ``LarkCardTransport(client)``，与本参数加入之前逐字节一致。唯一的调用方
    ``apps.gateway.assemble_delivery_consumer`` 只在显式命中
    ``LINGXI_GATEWAY_CARD_FAILURE_INJECT`` 时才传入一个包一层"确定性拒绝"的实现，
    用来验证卡片降级路径——不是给业务代码开的通用注入口。
    """

    from lingxi.adapters.feishu_delivery import LarkCardTransport, LarkDeliveryText

    return DeliveryConsumer(
        queue=queue,
        cards=cards if cards is not None else LarkCardTransport(client),
        texts=LarkDeliveryText(client),
        on_send_outcome=on_send_outcome,
        on_alert=on_alert,
        limit=limit,
    )
