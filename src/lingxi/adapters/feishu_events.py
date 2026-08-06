"""飞书事件体 → ``InboundMessage`` 的解析。

纯函数，不 import ``lark_oapi``：事件体到了这一层已经是 ``dict``，解析它不需要 SDK。
好处是全部解析断言（含 `V-接入-11`）在没装 SDK 的环境里也能跑，而且能直接喂构造出来的
畸形事件体。

**这个模块是 `V-接入-11` 的实际发生点。** 任务归属只能来自
``event.sender.sender_id.open_id``；事件体里其他位置出现的用户标识——
``event.sender.sender_id.user_id`` / ``union_id``、``header`` 里的字段、消息正文里
用户自己写的"我是某某"——一律不被搬运进 ``InboundMessage``，因为那个 dataclass
根本没有可以装它们的字段。不信任事件体自述身份这件事在这里是结构性的，不靠调用方自觉。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from lingxi.core.conversation.ports import InboundMessage
from lingxi.core.ids import new_id

# 本切片消费的事件类型。其余类型（card.action.trigger、
# im.chat.member.bot.deleted_v1）不在本批处理，但收到时不得让长连接崩溃（`V-接入-12`）。
MESSAGE_RECEIVE_EVENT = "im.message.receive_v1"


class EventParseError(ValueError):
    """事件体缺少必需字段或形状不对。

    刻意是一个**可预期**的错误而不是 ``KeyError`` / ``TypeError``：调用方要能把
    "这条事件我读不懂"与"我的代码有 bug"区分开，前者记审计后继续收下一条。
    """


def _text(container: Mapping[str, Any] | None, key: str) -> str | None:
    """取一个非空字符串字段；缺失、类型不对或空白一律当作没有。"""

    if not isinstance(container, Mapping):
        return None
    value = container.get(key)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _require(value: str | None, what: str) -> str:
    if value is None:
        raise EventParseError(f"事件体缺少{what}")
    return value


def message_text(content: object, message_type: object) -> str:
    """从飞书消息的 ``content`` 里取出纯文本。

    ``content`` 是**一段 JSON 字符串**（飞书如此定义），不是对象。非文本消息与解析
    不了的 content 返回空串而不是抛错：本切片只处理文本问数，收到图片或语音时应当
    照常走完管线（加表情、状态判定），而不是把长连接带下去。
    """

    if message_type != "text" or not isinstance(content, str):
        return ""
    try:
        payload = json.loads(content)
    except (ValueError, TypeError):
        return ""
    if not isinstance(payload, Mapping):
        return ""
    text = payload.get("text")
    return text if isinstance(text, str) else ""


def parse_message_event(payload: Mapping[str, Any], *, trace_id: str | None = None) -> InboundMessage:
    """把一条 ``im.message.receive_v1`` 事件体解析成 ``InboundMessage``。

    ``trace_id`` 只为测试可重复而开放；正常调用不传，由本函数生成一个 ULID
    （接口设计「追踪」：每个入站事件生成一个 trace_id，贯穿任务与审计）。
    """

    if not isinstance(payload, Mapping):
        raise EventParseError("事件体不是一个对象")

    header = payload.get("header")
    event = payload.get("event")

    event_id = _require(_text(header, "event_id"), "event_id")
    event_type = _require(_text(header, "event_type"), "event_type")

    if not isinstance(event, Mapping):
        raise EventParseError("事件体缺少 event 段")

    sender = event.get("sender")
    sender_id = sender.get("sender_id") if isinstance(sender, Mapping) else None
    # 唯一的用户来源。刻意只读 open_id 一个键：同一个 sender_id 对象里还有
    # user_id 与 union_id，读它们会让归属依赖飞书三套标识之间的一致性，
    # 而 app_user 的去重键就是 open_id（迁移 008 已如此定义）。
    sender_open_id = _require(_text(sender_id, "open_id"), "发送者 open_id")

    message = event.get("message")
    if not isinstance(message, Mapping):
        raise EventParseError("事件体缺少 message 段")

    message_id = _require(_text(message, "message_id"), "message_id")
    chat_id = _require(_text(message, "chat_id"), "chat_id")
    # thread_id 缺失表示私聊主窗口，不是错误。
    thread_id = _text(message, "thread_id")

    return InboundMessage(
        event_id=event_id,
        event_type=event_type,
        sender_open_id=sender_open_id,
        chat_id=chat_id,
        thread_id=thread_id,
        message_id=message_id,
        text=message_text(message.get("content"), message.get("message_type")),
        trace_id=trace_id or new_id("trc").split("_", 1)[1],
    )
