"""问数卡片的顺序、限流与失败回退。

这里不依赖飞书 SDK。卡片适配器只实现 ``CardTransport``，因此 L2 可以用内存假实现
验证序号、话题隔离和失败回退，L4a 再验证 CardKit 的真实字段与投递效果。

**Issue #152 起支持从持久化状态恢复（resume）**：Gateway 消费循环崩溃重启后不会重新
``start()`` 一张已经建好的卡片——它把上一次持久化的 ``card_id``/``sequence``/
``message_id``/``fallback_needed`` 作为 ``initial_*`` 参数传回来，本类从那一点继续，
不产生第二张有效卡片（`V-卡片-01`、状态合同第 7 条）。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Protocol

from lingxi.config.content import ContentCatalog, RenderedCard, RenderedContent, default_content_catalog


@dataclass(frozen=True)
class CardCreated:
    """建卡并把它作为消息发出后的结果。

    ``card_id`` 用于后续的流式更新/关闭；``message_id`` 是发送消息接口返回的可回读
    标识（G-CARD 实测：卡片与文本共用同一发送接口与响应结构），在卡片整个生命周期内
    只在这里产生一次，后续流式更新不产生新的 message_id。
    """

    card_id: str
    message_id: str


class CardTransport(Protocol):
    def create(
        self,
        *,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
        card: RenderedCard,
    ) -> CardCreated: ...

    def update(self, *, card_id: str, sequence: int, card: RenderedCard) -> None: ...

    def close(self, *, card_id: str, sequence: int, card: RenderedCard) -> None: ...


class TextTransport(Protocol):
    def send_text(
        self, *, chat_id: str, thread_id: str | None, reply_to_message_id: str, text: str
    ) -> str: ...


SendOutcomeCallback = Callable[[str, bool], None]


class CardRateLimiter:
    """一个 worker 进程共享：单话题 500ms、全进程 50 次/秒。"""

    def __init__(self) -> None:
        self._last_by_topic: dict[str, float] = {}
        self._global_updates: deque[float] = deque()

    def allow(self, *, topic: str, now: float) -> bool:
        last = self._last_by_topic.get(topic)
        if last is not None and now - last < 0.5:
            return False
        while self._global_updates and now - self._global_updates[0] >= 1.0:
            self._global_updates.popleft()
        if len(self._global_updates) >= 50:
            return False
        self._last_by_topic[topic] = now
        self._global_updates.append(now)
        return True

    def record(self, *, topic: str, now: float) -> None:
        """无条件记一次已经发生的调用，不做节流判断、不可能返回拒绝。

        终态更新+关闭（``CardStream.finish()``）不经过 ``allow()`` 的单话题 500ms
        节流——那两帧是结果本身，被节流吞掉等于结果丢失，比不节流更糟（独立审核
        P2-2）。但全进程 50 次/秒的预算是 CardKit 的硬限制，终态这两次调用同样要
        计入，否则并发多话题同时终态时全局计数会失真、放过原本该被节流的其它
        话题。只推进全局窗口，不改动单话题的 500ms 记录——终态调用不受单话题
        节流约束，也不应该反过来去占用/影响它。
        """

        while self._global_updates and now - self._global_updates[0] >= 1.0:
            self._global_updates.popleft()
        self._global_updates.append(now)


@dataclass(frozen=True)
class CardStreamResult:
    card_id: str | None
    sequence: int
    fallback_text: bool


class CardStream:
    """一个任务一个实例；绝不跨话题共享卡片序号或限流状态。"""

    def __init__(
        self,
        *,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
        transport: CardTransport,
        fallback: TextTransport,
        catalog: ContentCatalog | None = None,
        mark_external_side_effect: Callable[[], bool | None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        rate_limiter: CardRateLimiter | None = None,
        on_send_outcome: SendOutcomeCallback | None = None,
        initial_card_id: str | None = None,
        initial_sequence: int = 0,
        initial_message_id: str | None = None,
        initial_fallback_needed: bool = False,
    ) -> None:
        self._chat_id = chat_id
        self._thread_id = thread_id
        self._reply_to_message_id = reply_to_message_id
        self._transport = transport
        self._fallback = fallback
        self._catalog = catalog or default_content_catalog()
        self._mark_external_side_effect = mark_external_side_effect
        self._monotonic = monotonic
        # resume 场景：非 None/非零表示接着一次此前已经持久化的进度继续，而不是从零建卡
        # （Issue #152、状态合同第 7 条：重启不产生第二张有效卡片）。
        self._card_id: str | None = initial_card_id
        self._sequence = initial_sequence
        self._message_id: str | None = initial_message_id
        self._fallback_needed = initial_fallback_needed
        self._last_update: float | None = None
        self._rate_limiter = rate_limiter or CardRateLimiter()
        self._on_send_outcome = on_send_outcome

    @property
    def fallback_needed(self) -> bool:
        return self._fallback_needed

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def card_id(self) -> str | None:
        return self._card_id

    @property
    def message_id(self) -> str | None:
        """当前投递通道（卡片或文本兜底）绑定的可回读标识；尚未取得时为 ``None``。"""

        return self._message_id

    def start(self) -> None:
        """建卡并发出。resume 场景下（已经有 ``card_id`` 或已经降级）直接不做事——
        调用方按持久化状态决定要不要调用这一步，这里的判断只是防御性幂等。
        """

        if self._card_id is not None or self._fallback_needed:
            return
        card = self._status_card(action="processing", elapsed_seconds=0)
        try:
            self._before_external()
            created = self._transport.create(
                chat_id=self._chat_id,
                thread_id=self._thread_id,
                reply_to_message_id=self._reply_to_message_id,
                card=card,
            )
            self._card_id = created.card_id
            self._message_id = created.message_id
            self._notify_send("card_non_final", True)
            self._last_update = self._monotonic()
            # 创建本身就是该话题的首帧，后续更新也要遵守 500ms 间隔。
            self._rate_limiter.allow(topic=self._topic, now=self._last_update)
        except Exception:  # noqa: BLE001 - 卡片失败统一走同话题文本回退
            self._notify_send("card_non_final", False)
            self._fallback_needed = True

    def update(self, *, elapsed_seconds: int, action: str = "processing") -> None:
        if self._card_id is None or self._fallback_needed:
            return
        now = self._monotonic()
        if not self._rate_limiter.allow(topic=self._topic, now=now):
            return
        card = self._status_card(action=action, elapsed_seconds=max(0, elapsed_seconds))
        self._sequence += 1
        try:
            self._before_external()
            self._transport.update(card_id=self._card_id, sequence=self._sequence, card=card)
            self._notify_send("card_non_final", True)
            self._last_update = now
        except Exception:  # noqa: BLE001
            self._notify_send("card_non_final", False)
            self._fallback_needed = True

    def finish(
        self,
        *,
        result: str | None = None,
        failure: RenderedContent | None = None,
        elapsed_seconds: int = 0,
    ) -> None:
        """终态更新 + 关闭。**这两步的失败语义并不对称**（独立审核 P1-2）：

        - 终态 **更新** 失败：完整答案还没有确定写进卡片，用户看不到结果——必须
          整体降级为文本兜底，否则结果丢失。
        - 终态更新已经成功之后，仅 **关闭** 失败：答案已经在卡片里对用户可见，
          结果没有丢——按 G-CARD 实测（未手动关闭的流式卡片距上次开启 10 分钟后
          由平台自动关闭），关闭失败本身不构成"结果丢失"，只是收尾没做完。此时
          绝不能再触发文本兜底：那样只会让同一条答案在同一话题里出现两遍，是
          比"卡片没关"更差的用户体验，也直接违反"不得同时形成卡片终态与重复
          文本终态"（`V-卡片-03`）。因此关闭失败**不**置位 ``_fallback_needed``。
        """

        if self._card_id is None or self._fallback_needed:
            return
        if failure is None:
            body = result or self._catalog.card("query.empty").body
            completed = self._catalog.text(
                "worker.status",
                action=self._catalog.text("worker.action.completed").text,
                elapsed_seconds=max(0, elapsed_seconds),
            ).text
            body = f"{completed}\n{body}"
            card = self._catalog.card("query.result", result=body)
        else:
            card = self._catalog.card("query.failure", message=failure.text)

        self._sequence += 1
        try:
            self._before_external()
            self._rate_limiter.record(topic=self._topic, now=self._monotonic())
            self._transport.update(card_id=self._card_id, sequence=self._sequence, card=card)
            self._notify_send("card_final", True)
        except Exception:  # noqa: BLE001 - 终态正文还没确定送达，只能整体降级
            self._notify_send("card_final", False)
            self._fallback_needed = True
            return

        self._sequence += 1
        try:
            self._before_external()
            self._rate_limiter.record(topic=self._topic, now=self._monotonic())
            self._transport.close(card_id=self._card_id, sequence=self._sequence, card=card)
            self._notify_send("card_final", True)
        except Exception:  # noqa: BLE001 - 见上面的方法说明：不降级，避免重复投递
            self._notify_send("card_final", False)

    def send_fallback(self, content: RenderedContent) -> str | None:
        """发一次同话题文本兜底；不需要时（未降级）返回 ``None`` 且不产生外部调用。

        调用方负责在这次调用之前完成"外发前预留位"的持久化（Issue #151 审核 P3-6、
        本类文件头注释）——文本发送没有飞书原生幂等键，这里的异常刻意不吞掉，交由
        调用方区分"同步捕获的明确失败"（可以清预留位、下一轮重试）与"进程崩溃"
        （预留位留在数据库里，下一轮必须识别为 ``uncertain`` 而不是自动重发）。
        """

        if not self._fallback_needed:
            return None
        try:
            self._before_external()
            message_id = self._fallback.send_text(
                chat_id=self._chat_id,
                thread_id=self._thread_id,
                reply_to_message_id=self._reply_to_message_id,
                text=content.text,
            )
        except Exception:
            self._notify_send("message_final", False)
            raise
        self._message_id = message_id
        self._notify_send("message_final", True)
        return message_id

    def _status_card(self, *, action: str, elapsed_seconds: int) -> RenderedCard:
        action_key = "worker.action.completed" if action == "completed" else "worker.action.processing"
        action_text = self._catalog.text(action_key).text
        status = self._catalog.text(
            "worker.status", action=action_text, elapsed_seconds=elapsed_seconds
        ).text
        return self._catalog.card("query.status", status=status)

    @property
    def _topic(self) -> str:
        return f"{self._chat_id}\x00{self._thread_id or ''}"

    def _before_external(self) -> None:
        if self._mark_external_side_effect is not None:
            marked = self._mark_external_side_effect()
            if marked is False:
                raise RuntimeError("任务已不再由当前 worker 持有")

    def _notify_send(self, operation: str, succeeded: bool) -> None:
        """把发送结果交给告警层；告警层故障不能改变用户任务的出站语义。"""

        if self._on_send_outcome is None:
            return
        try:
            self._on_send_outcome(operation, succeeded)
        except Exception:
            # 告警输入失败不能反向把已成功的用户交付改成失败，也不能中断文本回退。
            return
