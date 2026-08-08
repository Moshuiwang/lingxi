"""worker 的出站投影：卡片失败时回到同一话题的普通文本。"""

from __future__ import annotations

from typing import Callable, Protocol

from lingxi.config.content import ContentCatalog, RenderedContent, default_content_catalog
from lingxi.core.execution.card_stream import CardRateLimiter, CardStream, CardTransport, TextTransport


class TaskDelivery(Protocol):
    def start(self) -> None: ...

    def progress(self, *, elapsed_seconds: int) -> None: ...

    def complete(self, *, result: str, elapsed_seconds: int = 0) -> None: ...

    def fail(self, *, content: RenderedContent) -> None: ...


class NullTaskDelivery:
    """白盒/无卡片适配器；不制造任何外部出站副作用。"""

    def start(self) -> None:
        return None

    def progress(self, *, elapsed_seconds: int) -> None:
        return None

    def complete(self, *, result: str, elapsed_seconds: int = 0) -> None:
        return None

    def fail(self, *, content: RenderedContent) -> None:
        return None


class CardTaskDelivery:
    """把 worker 的阶段结论交给 CardKit adapter，并统一文本回退。"""

    def __init__(
        self,
        *,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
        cards: CardTransport,
        replies: TextTransport,
        catalog: ContentCatalog | None = None,
        mark_external_side_effect: Callable[[], bool | None] | None = None,
        rate_limiter: CardRateLimiter | None = None,
    ) -> None:
        self._catalog = catalog or default_content_catalog()
        self._stream = CardStream(
            chat_id=chat_id,
            thread_id=thread_id,
            reply_to_message_id=reply_to_message_id,
            transport=cards,
            fallback=replies,
            catalog=self._catalog,
            mark_external_side_effect=mark_external_side_effect,
            rate_limiter=rate_limiter,
        )

    def start(self) -> None:
        self._stream.start()

    def progress(self, *, elapsed_seconds: int) -> None:
        self._stream.update(elapsed_seconds=elapsed_seconds)

    def complete(self, *, result: str, elapsed_seconds: int = 0) -> None:
        self._stream.finish(result=result, elapsed_seconds=elapsed_seconds)
        if self._stream.fallback_needed:
            self._stream.send_fallback(self._catalog.text("worker.failed"))

    def fail(self, *, content: RenderedContent) -> None:
        self._stream.finish(failure=content)
        self._stream.send_fallback(content)
