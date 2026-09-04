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
from dataclasses import dataclass
from typing import Any

from lingxi.core.conversation.ports import InboundMessage
from lingxi.core.ids import new_id

# 本切片消费的事件类型。``im.chat.member.bot.deleted_v1`` 仍不在本批处理，但收到时
# 不得让长连接崩溃（`V-接入-12`）。``card.action.trigger`` 由
# :func:`parse_card_action_event` 解析（管理员确认卡片的按钮回调）。
MESSAGE_RECEIVE_EVENT = "im.message.receive_v1"

#: 管理员确认卡片按钮点击回调（合同"待确认操作"闭环）。真实事件体字段未经真实
#: 验证（证据等级 1，与本模块其余解析函数同一姿态）——解析形状依据飞书卡片
#: 回传交互 2.0 的公开文档结构，真实字段由 `biai-stage` L4a 受控验收核实；
#: 解析失败按 :class:`CardActionParseError` 处理，与 `EventParseError` 同一姿态
#: （记审计后继续收下一条，不当作连接故障）。
CARD_ACTION_TRIGGER_EVENT = "card.action.trigger"


# 飞书私聊的 chat_type。合同「问数与多轮对话」开宗明义：
# 「本节全部规则只适用于飞书私聊入口」——加表情、话题串行、/new、/stop、两小时规则
# 全部以私聊为前提，群聊不在承诺范围内。
PRIVATE_CHAT_TYPE = "p2p"


class EventParseError(ValueError):
    """事件体缺少必需字段或形状不对。

    刻意是一个**可预期**的错误而不是 ``KeyError`` / ``TypeError``：调用方要能把
    "这条事件我读不懂"与"我的代码有 bug"区分开，前者记审计后继续收下一条。
    """


class NonPrivateChatError(EventParseError):
    """非私聊消息。

    单独成类而不是复用 ``EventParseError``：这不是"读不懂"，是"读懂了、而且明确
    不该受理"。调用方要能把它记成一条**越界拒绝**的审计，而不是混进解析失败里。

    ``mentioned_open_ids``/``chat_id``/``message_id``：三者只服务"要不要回
    一句固定引导"这一个判定，不是任务归属来源——`V-接入-11` 仍然只认
    ``sender.sender_id.open_id`` 一个键。三者都按"读不出就是没有"处理，绝不
    会因为这几个新字段读取失败而改变本类原有的抛出行为或消息文案。
    """

    def __init__(
        self,
        chat_type: str | None,
        *,
        mentioned_open_ids: tuple[str, ...] = (),
        chat_id: str | None = None,
        message_id: str | None = None,
    ) -> None:
        """记录被拒绝的 chat_type，及群聊@固定引导判定要用的三个可选字段。"""
        super().__init__(f"非私聊消息，本产品只服务飞书私聊：chat_type={chat_type!r}")
        self.chat_type = chat_type
        self.mentioned_open_ids = mentioned_open_ids
        self.chat_id = chat_id
        self.message_id = message_id


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


def _mentioned_open_ids(message: Mapping[str, Any] | None) -> tuple[str, ...]:
    """从消息体的 ``mentions`` 段读出被 @ 的飞书用户 open_id。

    只服务群聊@机器人固定引导这一条判定路径，不是任务归属来源——`V-接入-11`
    的唯一来源仍然是 ``sender.sender_id.open_id``，本函数读到的值从不进入
    ``InboundMessage``。**证据等级 1**：字段形状依据飞书公开文档结构，真实
    群聊 @ 事件体是否逐字段吻合未经真实回调验证。结构不对、字段缺失一律
    返回空元组而不是抛错，不能因为这一段可选信息影响
    ``NonPrivateChatError`` 本身的抛出。
    """
    if not isinstance(message, Mapping):
        return ()
    mentions = message.get("mentions")
    if not isinstance(mentions, list):
        return ()
    open_ids: list[str] = []
    for item in mentions:
        if not isinstance(item, Mapping):
            continue
        open_id = _text(item.get("id"), "open_id")
        if open_id is not None:
            open_ids.append(open_id)
    return tuple(open_ids)


def _reject_non_private_chat(message: Mapping[str, Any]) -> None:
    """群聊边界：在构造 ``InboundMessage`` 之前拒绝，非私聊消息因此进不了管线。

    只认显式的 ``p2p``，缺字段也拒绝——默认放行的代价是把群聊内容当私聊
    处理（越界，且可能泄漏到不该看见的人面前），默认拒绝的代价只是漏收，
    前者不可逆、后者可观察可修。抛出之前顺手带上 mentions/chat_id/
    message_id 只服务"要不要回一句群聊@固定引导"，不改变本函数的抛出行为。
    """
    chat_type = _text(message, "chat_type")
    if chat_type != PRIVATE_CHAT_TYPE:
        raise NonPrivateChatError(
            chat_type,
            mentioned_open_ids=_mentioned_open_ids(message),
            chat_id=_text(message, "chat_id"),
            message_id=_text(message, "message_id"),
        )


def parse_message_event(
    payload: Mapping[str, Any], *, trace_id: str | None = None
) -> InboundMessage:
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

    _reject_non_private_chat(message)

    message_id = _require(_text(message, "message_id"), "message_id")
    chat_id = _require(_text(message, "chat_id"), "chat_id")
    # thread_id 缺失表示私聊主窗口，不是错误。
    thread_id = _text(message, "thread_id")

    message_type = _text(message, "message_type") or "unknown"

    return InboundMessage(
        event_id=event_id,
        event_type=event_type,
        sender_open_id=sender_open_id,
        chat_id=chat_id,
        thread_id=thread_id,
        message_id=message_id,
        text=message_text(message.get("content"), message.get("message_type")),
        trace_id=trace_id or new_id("trc").split("_", 1)[1],
        message_type=message_type,
    )


class CardActionParseError(EventParseError):
    """``card.action.trigger`` 事件体缺少必需字段或形状不对。

    与 ``EventParseError`` 同一处理姿态：调用方记审计后继续收下一条，不
    当作连接故障。
    """


@dataclass(frozen=True)
class CardActionEvent:
    """一条已从飞书卡片回调事件体里解析出来的按钮点击。

    ``operator_open_id`` 是唯一的点击身份来源——与 ``parse_message_event``
    对 ``sender_open_id`` 的既有取舍相同（只信事件体里飞书自己标注的操作者
    字段）。``action_value`` 原样透传建卡时写进按钮的键，具体校验交给
    ``core/admin/card_callback.py``。``action_name`` 是按钮自身的
    ``action.name``；``form_value`` 是 form 容器提交的字段值——form 内提交
    按钮的 ``action.value`` 经常不以 Mapping 形态到达，下游需要
    ``action_name`` 作为不依赖 ``value`` 内容的路由后备判据。
    """

    event_id: str
    operator_open_id: str
    action_value: Mapping[str, str]
    action_name: str | None
    form_value: Mapping[str, str]
    trace_id: str


def _stringify_scalars(mapping: Mapping[str, Any]) -> dict[str, str]:
    """只保留字符串/数字这类简单标量并统一转成字符串。

    不信任事件体里出现的任何嵌套结构或意料之外的类型（结构上不给伪造回调
    可乘之机，即便真的出现了也不会在这里崩溃，只会被
    ``core/admin/card_callback.py`` 当成缺少必需字段拒绝）。
    """
    return {
        str(key): str(value)
        for key, value in mapping.items()
        if isinstance(value, (str, int, float)) and not isinstance(value, bool)
    }


# ``action.value`` 是字符串形态时允许的最大字节数。真实表单回调里 value 只装
# 少量标量键，正常在几百字节内；给一个宽松但有限的上限，专门挡住"伪造回调
# 塞一大段畸形 JSON"这类可用性攻击（深层嵌套 JSON 会触发 RecursionError，
# 逃出解析捕获会升级成 gateway 未处理异常）。
_MAX_ACTION_VALUE_JSON_BYTES = 8192


def _parse_action_value(raw_value: object) -> Mapping[str, Any] | None:
    """把 ``action.value`` 解析成 Mapping；三种到达形态兼容。

    已经是 Mapping 直接用；是字符串先卡长度上限（``_MAX_ACTION_VALUE_
    JSON_BYTES``）再 ``json.loads``，解析结果是 Mapping 才采纳（捕获面含
    ``RecursionError``：深层嵌套 JSON 抛的是它，任由它逃出会把一条伪造
    回调升级成 gateway 可用性攻击）；缺失或其它类型返回 ``None``，交给
    调用方决定是否还有 ``action.form_value`` 可以兜底。任何解析失败一律
    按"不可用"返回 ``None``，同一失败关闭姿态。
    """
    if isinstance(raw_value, Mapping):
        return raw_value
    if isinstance(raw_value, str):
        if len(raw_value.encode("utf-8")) > _MAX_ACTION_VALUE_JSON_BYTES:
            return None
        try:
            parsed = json.loads(raw_value)
        except (TypeError, ValueError, RecursionError):
            return None
        return parsed if isinstance(parsed, Mapping) else None
    return None


def parse_card_action_event(
    payload: Mapping[str, Any], *, trace_id: str | None = None
) -> CardActionEvent:
    """把一条 ``card.action.trigger`` 事件体解析成 :class:`CardActionEvent`。

    **证据等级 1→部分 L4a**：结构本身仍未经真实回调验证；``action.value``
    的到达形态已由真实点击坐实——可能不以 Mapping 形态到达，兼容策略见
    :func:`_parse_action_value`。反伪造姿态不放宽：两者都没有可用内容时
    仍然抛 ``CardActionParseError``，不猜测这是不是一次合法的卡片交互。
    """
    if not isinstance(payload, Mapping):
        raise CardActionParseError("事件体不是一个对象")

    header = payload.get("header")
    event = payload.get("event")
    event_id = _text(header, "event_id")
    if event_id is None:
        raise CardActionParseError("事件体缺少 event_id")

    if not isinstance(event, Mapping):
        raise CardActionParseError("事件体缺少 event 段")

    operator = event.get("operator")
    operator_open_id = _text(operator, "open_id")
    if operator_open_id is None:
        raise CardActionParseError("事件体缺少操作者 open_id")

    action = event.get("action")
    if not isinstance(action, Mapping):
        raise CardActionParseError("事件体缺少 action 段")

    action_value, form_value, action_name = _parsed_action_fields(action)

    return CardActionEvent(
        event_id=event_id,
        operator_open_id=operator_open_id,
        action_value=action_value,
        action_name=action_name,
        form_value=form_value,
        trace_id=trace_id or new_id("trc").split("_", 1)[1],
    )


def _parsed_action_fields(
    action: Mapping[str, Any],
) -> tuple[dict[str, str], dict[str, str], str | None]:
    """从 ``action`` 段取出 ``(action_value, form_value, action_name)``。

    两者都没有可用内容（``value`` 解析不出 Mapping 且 ``form_value`` 也
    不是 Mapping）时抛 :class:`CardActionParseError`——"完全没有可用回传
    内容"与"有 form_value 说明这是一次表单提交、只是 value 恰好没带上
    admin_action"是两种不同的情况，前者继续失败关闭。
    """
    value_mapping = _parse_action_value(action.get("value"))
    raw_form_value = action.get("form_value")
    form_value_mapping = raw_form_value if isinstance(raw_form_value, Mapping) else None

    if value_mapping is None and form_value_mapping is None:
        raise CardActionParseError("action 段缺少可用的 value 或 form_value")

    return (
        _stringify_scalars(value_mapping or {}),
        _stringify_scalars(form_value_mapping or {}),
        _text(action, "name"),
    )
