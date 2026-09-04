"""群聊 @ 机器人固定引导：节流判定与三条件应答决策。

留在 ``apps/gateway/`` 而不是 ``core/conversation/``：``GroupMentionHintResponder``
的唯一入参类型是 ``adapters.feishu_events.NonPrivateChatError``，是 adapters
层定义的解析异常类型；`core/` 不得 import `adapters/`，把这两个类移进 `core/`
要么违反这条边界，要么需要引入解耦用的 Protocol 改变公开签名形状。
``GroupMentionHintThrottle`` 本身不依赖 gateway/adapters 类型，但与
``GroupMentionHintResponder`` 是紧耦合的一对（后者持有前者的实例），拆成两处
会打散阅读单元，因此两者放在同一个模块。

外部调用点从 ``lingxi.apps.gateway`` 顶层导入 ``GroupMentionHintResponder``/
``build_group_mention_hint_throttle``，不感知本模块内部结构。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from lingxi.adapters.feishu_events import NonPrivateChatError
from lingxi.config.content import default_content_catalog
from lingxi.core.identity.identifiers import redact_identifier

logger = logging.getLogger(__name__)


#: 内容目录里群聊@机器人固定引导的键。
GROUP_MENTION_HINT_CONTENT_KEY = "gateway.group_mention_hint"
#: 同一个群一小时内最多发一条固定引导，防刷屏。
GROUP_MENTION_HINT_THROTTLE_SECONDS = 3600.0


class GroupMentionHintThrottle:
    """按 ``chat_id`` 节流的进程内存节流器。

    状态只活在进程内存里，不落库：重启会让节流窗口清零，代价止步于「同一个群
    多收到一条固定文案」，是既定取舍不是缺陷。``allow``（判定）与
    ``mark_sent``（记账）拆成两个方法：只有 ``send_text`` 成功之后才应该消耗
    额度，若发送前就记账，异常时额度已耗但消息没发出去，重投又被同一窗口
    拦下，形成静默故障。``last_sent_at`` 只增不减，键域随群数量增长，量级
    有界。
    """

    def __init__(
        self,
        *,
        window_seconds: float = GROUP_MENTION_HINT_THROTTLE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """按 ``window_seconds`` 建立节流窗口；``clock`` 供测试注入。"""
        self._window_seconds = window_seconds
        self._clock = clock
        self._last_sent_at: dict[str, float] = {}

    def allow(self, chat_id: str) -> bool:
        """只读判定，不产生任何副作用——多次调用（例如失败重试）互不影响。"""
        last = self._last_sent_at.get(chat_id)
        if last is None:
            return True
        return self._clock() - last >= self._window_seconds

    def mark_sent(self, chat_id: str) -> None:
        """真正发送成功之后才调用，记下这次节流窗口的起点。"""
        self._last_sent_at[chat_id] = self._clock()


def build_group_mention_hint_throttle(
    *,
    window_seconds: float = GROUP_MENTION_HINT_THROTTLE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> GroupMentionHintThrottle:
    """装配一个默认参数的节流器实例。"""
    return GroupMentionHintThrottle(window_seconds=window_seconds, clock=clock)


class GroupMentionHintResponder:
    """群聊 @ 机器人固定引导：要不要发这条提示的唯一判定入口。

    三个条件同时成立才发送，任一不成立都维持"群聊完全静默"：``bot_open_id``
    已配置；机器人 open_id 精确出现在 ``mentioned_open_ids`` 里；``chat_id``
    没有被节流器拦下。``chat_id`` 只进日志脱敏参数，审计不带身份或正文。

    ``maybe_respond`` 尽力而为、失败关闭：调用方不包 try/except，异常若抛穿
    会被飞书判定处理失败而重投，而节流位已在发送前记上——因此判定与发送包在
    一个 try/except 里，任何异常只记审计后正常返回，额度只在发送成功后消耗。
    """

    def __init__(
        self,
        *,
        bot_open_id: str | None,
        replies: Any,
        audit: Any,
        throttle: GroupMentionHintThrottle,
    ) -> None:
        """装配判定与发送所需的三个协作对象。"""
        self._bot_open_id = bot_open_id
        self._replies = replies
        self._audit = audit
        self._throttle = throttle

    def maybe_respond(self, error: NonPrivateChatError) -> None:
        """外层失败关闭包装——任何异常只记审计，不向上抛出（见类文档）。"""
        try:
            self._maybe_respond(error)
        except Exception as failure:  # 引导是尽力而为的旁路，
            # 失败不得带走事件 ack、不得让飞书判定为处理失败而重投（见类文档）。
            logger.error(
                "gateway.group_mention_hint.failed chat_id=%s error=%s",
                redact_identifier(error.chat_id) if error.chat_id else None,
                type(failure).__name__,
            )
            self._audit.record("event.group_mention_hint_failed", error=type(failure).__name__)

    def _maybe_respond(self, error: NonPrivateChatError) -> None:
        if not self._bot_open_id:
            return
        if self._bot_open_id not in error.mentioned_open_ids:
            return
        if not error.chat_id or not error.message_id:
            # 结构异常导致读不出 chat_id/message_id：没有地方可回，维持静默
            # （与本类其余分支同一条"读不出就当没有"的纪律）。
            return
        if not self._throttle.allow(error.chat_id):
            return

        content = default_content_catalog().text(GROUP_MENTION_HINT_CONTENT_KEY)
        self._replies.send_text(
            chat_id=error.chat_id,
            thread_id=None,
            reply_to_message_id=error.message_id,
            text=content.text,
        )
        # 只有发送真正成功才消耗节流额度——见 `GroupMentionHintThrottle` 文档。
        self._throttle.mark_sent(error.chat_id)
        # `chat_id` 只写进这一行脱敏日志，不进下面的结构化审计字段——见本类文档
        # 「`V-花名册-34`」段落。
        logger.info("gateway.group_mention_hint.sent chat_id=%s", redact_identifier(error.chat_id))
        self._audit.record(
            "event.group_mention_hint_sent",
            content_key=content.key,
            content_version=content.version,
        )
