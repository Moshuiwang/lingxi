"""确认卡片与管理群终态通知的展示层：纯函数渲染 + 卡片出站 Protocol。

只负责"展示什么"，不负责"怎么发出去"：真实 CardKit 调用住在
``adapters/feishu_admin_card.py``，真实群消息复用既有
``adapters/feishu_group_message.FeishuGroupMessages``。本模块因此不 import 任何飞书
SDK，可以在没有装 SDK 的环境里测试全部渲染断言。

## 为什么不复用 ``core/execution/card_stream.RenderedCard``

那个类型是流式问数结果卡片的展示形状（``button_labels`` 只是纯展示文案，不支持按钮
各自绑定回调值）。确认卡的两个按钮必须分别携带 ``pending_action_id`` 与 ``decision``
（``"confirm"``/``"cancel"``）供 ``card.action.trigger`` 回调识别是哪一个待确认操作、
点的是哪个按钮——这是 ``RenderedCard`` 结构上不支持的新形状，因此为确认卡新增一个
平行的最小类型，而不是给业务结果卡塞进它不需要的字段。出站的"发送失败按明确错误/
结果不明两类"判别方式、按 Protocol 注入换取可测试性这些**做法**仍然完全复用
（``adapters/feishu_admin_card.py`` 与 ``adapters/feishu_delivery.py`` 同构）。

## 本模块生成的文案不经过 ``config/content.toml``

与 ``core/admin/router.py`` 的既有取舍相同：确认卡与管理群通知只面向已登记管理员，
内容随待确认操作动态变化（目标标识、有效期、终态文案），不适合模板变量的强匹配
版本约束。管理群的既有花名册/内测日报（``core/identity/roster_report.py``、
``core/daily_report.py``）同样用纯 Python 函数渲染，不经过 content.toml——本模块与
这两处是同一惯例，不是新例外。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

from lingxi.core.admin.pending_action import (
    PENDING_ACTION_TTL_SECONDS,
    PendingAction,
    PendingActionType,
)

_ACTION_LABEL: dict[PendingActionType, str] = {
    PendingActionType.SUSPEND_USER: "停用",
    PendingActionType.RESUME_USER: "恢复",
}

_IMPACT_TEXT: dict[PendingActionType, str] = {
    PendingActionType.SUSPEND_USER: "该用户将立即无法发起新的问数或开通；已在进行中的任务不受影响、正常完成交付。",
    PendingActionType.RESUME_USER: "该用户将恢复可以正常问数；此前被收回的权限不会自动恢复。",
}

#: 卡片按钮点击回传的 ``decision`` 取值，与
#: ``adapters/feishu_events.parse_card_action_event``/``core/admin/card_callback``
#: 三处共用同一对字面量。
DECISION_CONFIRM = "confirm"
DECISION_CANCEL = "cancel"


@dataclass(frozen=True)
class ConfirmCardButton:
    label: str
    #: 绑定在按钮上的回传值；``card.action.trigger`` 事件的 ``action.value`` 原样
    #: 带回这个 mapping。只放 ``pending_action_id`` 与 ``decision`` 两个字段——
    #: 不带凭据、不带目标资料。
    value: Mapping[str, str]


@dataclass(frozen=True)
class RenderedConfirmCard:
    """一张确认卡片的展示内容。``buttons`` 为空元组表示终态卡片（不可再操作）。"""

    title: str
    body: str
    buttons: tuple[ConfirmCardButton, ...]

    @property
    def is_terminal(self) -> bool:
        return not self.buttons


@dataclass(frozen=True)
class AdminCardCreated:
    """建卡并作为消息发出后的结果，与 ``core.execution.card_stream.CardCreated``
    同一形状（``card_id`` 用于后续更新，``message_id`` 是可回读标识）。"""

    card_id: str
    message_id: str


class AdminCardDeliveryRejected(Exception):
    """服务端已经给出完整响应、并以明确的业务错误码拒绝这次外发——不是"结果不明"。

    与 ``core.execution.card_stream.DeliveryRejected`` 同一白名单纪律：真实 adapter
    只在能读到 ``code``/``msg`` 的明确拒绝响应时才抛出它；其余异常（网络类、JSON
    解析失败、响应缺可回读标识）一律不属于这个类型，落进调用方的"结果不明"分支
    （待确认操作因此保持 ``card_delivered=False``，视为作废，不冒险当成已送达）。
    """

    def __init__(
        self, message: str = "", *, code: int | str | None = None, log_id: str | None = None
    ) -> None:
        self.code = code
        self.message = message
        self.log_id = log_id
        super().__init__(message or f"服务端明确拒绝：code={code} log_id={log_id}")


class AdminCardTransport(Protocol):
    """确认卡片的出站端口。真实实现见 ``adapters/feishu_admin_card.LarkAdminCardTransport``；
    测试注入内存假实现。"""

    def create(
        self,
        *,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
        card: RenderedConfirmCard,
    ) -> AdminCardCreated: ...

    def update(self, *, card_id: str, card: RenderedConfirmCard) -> None: ...


class GroupNotifier(Protocol):
    """管理群通知端口。真实实现是既有
    ``adapters.feishu_group_message.FeishuGroupMessages.send_text``——本 Protocol
    只声明调用方（``core/admin/card_callback.py``）实际用到的这一个方法，不要求
    注入完整的 ``FeishuGroupMessages`` 类型。"""

    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None: ...


def render_confirm_card(pending: PendingAction, *, target_label: str) -> RenderedConfirmCard:
    """建卡时的初始展示：动作、目标（回显管理员自己刚输入的标识，不引入新的资料
    披露）、影响范围与有效期。"""

    action_label = _ACTION_LABEL[pending.action_type]
    ttl_minutes = PENDING_ACTION_TTL_SECONDS // 60
    body = (
        f"动作：{action_label}用户\n"
        f"目标：{target_label}\n"
        f"影响：{_IMPACT_TEXT[pending.action_type]}\n"
        f"有效期：{ttl_minutes} 分钟内有效，过期后需重新查询并发起。"
    )
    return RenderedConfirmCard(
        title=f"待确认：{action_label}用户",
        body=body,
        buttons=(
            ConfirmCardButton(
                label="确认执行",
                value={"pending_action_id": pending.id, "decision": DECISION_CONFIRM},
            ),
            ConfirmCardButton(
                label="取消",
                value={"pending_action_id": pending.id, "decision": DECISION_CANCEL},
            ),
        ),
    )


def render_terminal_card(
    pending: PendingAction, *, target_label: str, outcome_text: str
) -> RenderedConfirmCard:
    """终态更新：不再带任何按钮（合同"更新为不可再次操作的最终状态"）。"""

    action_label = _ACTION_LABEL[pending.action_type]
    body = f"目标：{target_label}\n结果：{outcome_text}"
    return RenderedConfirmCard(title=f"{action_label}用户 · 已结束", body=body, buttons=())


def render_group_notice(pending: PendingAction, *, outcome_text: str) -> str:
    """管理群脱敏通知正文：不含 open_id 明文、不含姓名，只含内部标识（``pac_*``）
    与结果摘要——与 ``core/identity/roster_report.py`` 用内部 ULID 而非外部平台
    标识做定位的既有取舍一致。群里没有任何按钮、命令提示或可执行入口（`V-管理-11`
    同一要求，复用 ``FeishuGroupMessages.send_text`` 本身就结构性不支持卡片）。
    """

    action_label = _ACTION_LABEL[pending.action_type]
    return f"管理操作 {pending.id}：{action_label}用户 · {outcome_text}"
