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
# 不得让长连接崩溃（`V-接入-12`）。``card.action.trigger`` 自 Issue #96 S-M-02 起
# 由 :func:`parse_card_action_event` 解析（管理员确认卡片的按钮回调）。
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

    ``mentioned_open_ids``/``chat_id``/``message_id``（Issue #318 群聊@机器人固定
    引导）：三者只服务"要不要回一句固定引导"这一个判定，不是任务归属来源——
    `V-接入-11` 仍然只认 ``sender.sender_id.open_id`` 一个键，本类完全不参与任务
    归属。三者都按"读不出就是没有"处理：缺字段、类型不对时一律取空值，绝不会
    因为这几个新字段读取失败而改变本类原有的抛出行为或 ``chat_type``/消息文案。
    """

    def __init__(
        self,
        chat_type: str | None,
        *,
        mentioned_open_ids: tuple[str, ...] = (),
        chat_id: str | None = None,
        message_id: str | None = None,
    ) -> None:
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
    """从消息体的 ``mentions`` 段读出被 @ 的飞书用户 open_id（Issue #318）。

    只服务群聊@机器人固定引导这一条判定路径，不是任务归属来源——`V-接入-11`
    的唯一来源仍然是 ``sender.sender_id.open_id``，本函数读到的值从不进入
    ``InboundMessage``。**证据等级 1**：字段形状（``message.mentions[].id.open_id``）
    依据飞书《接收消息》事件回调的公开文档结构
    （https://open.feishu.cn/document/server-docs/im-v1/message/events/receive），
    真实群聊 @ 事件体是否逐字段吻合未经真实回调验证。结构不对、字段缺失一律返回
    空元组而不是抛错——读不出被 @ 的人只意味着"当作没有人被 @"，不能因为这一段
    可选信息影响 ``NonPrivateChatError`` 本身的抛出（见该类文档）。
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

    # 群聊边界：在构造 ``InboundMessage`` **之前**拒绝，因此非私聊消息进不了管线,
    # 既不加表情也不回复、更不会入队。放在这里而不是管线里，是因为管线的每一步
    # 都已经预设了私聊语义（话题串行按 conversation 一行、/new 清当前对话），
    # 让群聊消息走进去再判定，等于给它们建了一条只差最后一步的通路。
    #
    # **只认显式的 `p2p`，缺字段也拒绝。** 与仓库既有的拒绝式白名单同一姿态：
    # 默认放行的代价是把群聊内容当私聊处理（越界、且可能泄漏到不该看见的人面前），
    # 默认拒绝的代价只是漏收——后者可观察、可修，前者不可逆。
    #
    # 抛出之前顺手读一份 mentions/chat_id/message_id（Issue #318 群聊@机器人固定
    # 引导）：只能在这里读，`NonPrivateChatError` 一旦抛出，调用方手里就只有异常
    # 对象本身，没有别的机会再摸一次原始事件体。三者都经 `_text`/`_mentioned_
    # open_ids`——两者都不会抛错，因此这几行不改变本分支原有的抛出行为。
    chat_type = _text(message, "chat_type")
    if chat_type != PRIVATE_CHAT_TYPE:
        raise NonPrivateChatError(
            chat_type,
            mentioned_open_ids=_mentioned_open_ids(message),
            chat_id=_text(message, "chat_id"),
            message_id=_text(message, "message_id"),
        )

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
    """``card.action.trigger`` 事件体缺少必需字段或形状不对。与 ``EventParseError``
    同一处理姿态：调用方记审计后继续收下一条，不当作连接故障。"""


@dataclass(frozen=True)
class CardActionEvent:
    """一条已从飞书卡片回调事件体里解析出来的按钮点击。

    ``operator_open_id`` 是唯一的点击身份来源——与 ``parse_message_event`` 对
    ``sender_open_id`` 的既有取舍相同（只信事件体里飞书自己标注的操作者字段，不
    信任回传值 ``action_value`` 里任何自称的身份）。``action_value`` 只保留
    :func:`~lingxi.core.admin.notification.render_confirm_card` 建卡时写进按钮的
    ``pending_action_id``/``decision`` 两个键（原样透传，具体校验交给
    ``core/admin/card_callback.py``——本函数只负责"读出这段事件体写了什么"，不做
    业务判断）。
    """

    event_id: str
    operator_open_id: str
    action_value: Mapping[str, str]
    trace_id: str


def parse_card_action_event(
    payload: Mapping[str, Any], *, trace_id: str | None = None
) -> CardActionEvent:
    """把一条 ``card.action.trigger`` 事件体解析成 :class:`CardActionEvent`。

    **证据等级 1**：真实事件体的确切字段未经真实回调验证（本切片全部断言跑在
    构造的假事件体上），字段名依据飞书卡片回传交互 2.0 的公开文档结构；真实链路
    验证属 `biai-stage` L4a（本 Story 明确留待验收窗口，见 PR 描述）。解析失败一律
    ``CardActionParseError``，不抛 ``KeyError``/``TypeError``，与 ``parse_message_
    event`` 同一姿态。

    **2026-08-25 与 ``adapters/feishu_admin_card._card_payload`` 的按钮改动（顶层
    元素 + ``behaviors`` 回调，替换此前的 ``action`` 容器）核对兼容性**：飞书
    《配置卡片交互》与《卡片回传交互回调》两篇官方文档
    （https://open.feishu.cn/document/feishu-cards/configuring-card-interactions、
    https://open.feishu.cn/document/feishu-cards/card-callback-communication）的
    示例代码都把 ``behaviors: [{"type": "callback", "value": {...}}]`` 里的
    ``value`` 标注为回调事件 ``event.action.value`` 字段的来源，与本函数已经在读
    的路径（``payload["event"]["action"]["value"]``）一致；两篇文档都没有把这个
    路径描述成随按钮是否套 ``action`` 容器、或按钮是新 2.0 顶层形态还是旧形态而
    变化。本函数因此不需要为新按钮形状新增兼容分支或改动解析路径。**这只是官方
    文档层面的核实，证据等级仍是 1**：真实点击触发的事件体是否逐字符合文档描述，
    仍是 `biai-stage` L4a 受控验收范围，未经真实回调验证。
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
    raw_value = action.get("value")
    if not isinstance(raw_value, Mapping):
        raise CardActionParseError("action 段缺少 value")

    # 只保留字符串/数字这类简单标量并统一转成字符串——回传值本就只应该携带
    # render_confirm_card 建卡时写进去的两个键，不信任事件体里出现的任何嵌套结构
    # 或意料之外的类型（结构上不给伪造回调可乘之机，即便真的出现了也不会在这里
    # 崩溃，只会被 core/admin/card_callback.py 当成缺少必需字段拒绝）。
    action_value = {
        str(key): str(value)
        for key, value in raw_value.items()
        if isinstance(value, (str, int, float)) and not isinstance(value, bool)
    }

    return CardActionEvent(
        event_id=event_id,
        operator_open_id=operator_open_id,
        action_value=action_value,
        trace_id=trace_id or new_id("trc").split("_", 1)[1],
    )
