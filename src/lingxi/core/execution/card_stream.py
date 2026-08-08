"""问数卡片的顺序、限流与失败回退。

这里不依赖飞书 SDK。卡片适配器只实现 ``CardTransport``，因此 L2 可以用内存假实现
验证序号、话题隔离和失败回退，L4a 再验证 CardKit 的真实字段与投递效果。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Protocol

from lingxi.config.content import ContentCatalog, RenderedCard, RenderedContent, default_content_catalog


class CardTransport(Protocol):
    def create(
        self,
        *,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
        card: RenderedCard,
    ) -> str: ...

    def update(self, *, card_id: str, sequence: int, card: RenderedCard) -> None: ...

    def close(self, *, card_id: str, sequence: int, card: RenderedCard) -> None: ...


class TextTransport(Protocol):
    def send_text(
        self, *, chat_id: str, thread_id: str | None, reply_to_message_id: str, text: str
    ) -> None: ...


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
    ) -> None:
        self._chat_id = chat_id
        self._thread_id = thread_id
        self._reply_to_message_id = reply_to_message_id
        self._transport = transport
        self._fallback = fallback
        self._catalog = catalog or default_content_catalog()
        self._mark_external_side_effect = mark_external_side_effect
        self._monotonic = monotonic
        self._card_id: str | None = None
        self._sequence = 0
        self._fallback_needed = False
        self._last_update: float | None = None
        self._rate_limiter = rate_limiter or CardRateLimiter()
        self._on_send_outcome = on_send_outcome

    @property
    def fallback_needed(self) -> bool:
        return self._fallback_needed

    @property
    def sequence(self) -> int:
        return self._sequence

    def start(self) -> None:
        card = self._status_card(action="processing", elapsed_seconds=0)
        try:
            self._before_external()
            self._card_id = self._transport.create(
                chat_id=self._chat_id,
                thread_id=self._thread_id,
                reply_to_message_id=self._reply_to_message_id,
                card=card,
            )
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
        if self._card_id is None or self._fallback_needed:
            return
        try:
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
            self._before_external()
            self._transport.update(card_id=self._card_id, sequence=self._sequence, card=card)
            self._notify_send("card_final", True)
            self._sequence += 1
            self._before_external()
            self._transport.close(card_id=self._card_id, sequence=self._sequence, card=card)
            self._notify_send("card_final", True)
        except Exception:  # noqa: BLE001
            self._notify_send("card_final", False)
            self._fallback_needed = True

    def send_fallback(self, content: RenderedContent) -> None:
        if not self._fallback_needed:
            return
        try:
            self._before_external()
            self._fallback.send_text(
                chat_id=self._chat_id,
                thread_id=self._thread_id,
                reply_to_message_id=self._reply_to_message_id,
                text=content.text,
            )
            self._notify_send("message_final", True)
        except Exception:
            self._notify_send("message_final", False)
            raise

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
