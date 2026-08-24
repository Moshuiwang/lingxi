"""确认卡片的发送编排：渲染 → 通过 ``AdminCardTransport`` 发送 → 把结果落回待确认操作。

只依赖注入的 ``Protocol`` 端口，不 import ``adapters/``。真实装配见
``apps/gateway/__init__.py``。

## 为什么"发送 + 落库标记"合成一个方法

合同"卡片发送失败时本次操作不执行"要求"发送结果"与"待确认操作是否可被确认"两件
事不能出现中间态（发了但没记、记了但其实没发）。把两步都放进同一个类的同一次调用
里，让调用方（``core/admin/router.py``）只需要处理一个返回值（"是否已确认送达"），
不需要自己记得"发送成功之后一定要调 ``mark_card_delivered``"这类跨对象的调用顺序。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from lingxi.core.admin.notification import AdminCardTransport, render_confirm_card
from lingxi.core.admin.pending_action import PendingAction


class PendingActionDeliveryTracker(Protocol):
    """卡片发送结果的落库口——与
    ``adapters.postgres_pending_action.PostgresPendingActionStore`` 的
    ``mark_card_delivered``/``mark_send_failed`` 两个方法结构相同。"""

    def mark_card_delivered(self, *, pending_action_id: str, card_id: str) -> None: ...

    def mark_send_failed(self, *, pending_action_id: str) -> None: ...


@dataclass(frozen=True)
class CardDispatchResult:
    delivered: bool


class ConfirmCardDispatcher:
    """把一条刚 ``prepare`` 好的待确认操作渲染成确认卡片，发到发起管理员本人
    私聊（作为触发这条命令的消息的回复），并把发送结果同步落回待确认操作表。
    """

    def __init__(
        self, *, transport: AdminCardTransport, tracker: PendingActionDeliveryTracker
    ) -> None:
        self._transport = transport
        self._tracker = tracker

    def send(
        self,
        *,
        pending: PendingAction,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
    ) -> CardDispatchResult:
        card = render_confirm_card(pending, target_label=pending.target_open_id)
        try:
            created = self._transport.create(
                chat_id=chat_id,
                thread_id=thread_id,
                reply_to_message_id=reply_to_message_id,
                card=card,
            )
        except Exception:
            # 与 core.execution.card_stream 同一白名单纪律：明确拒绝
            # （AdminCardDeliveryRejected）与结果不明（其余异常）在这里得到同一个
            # 处理——两者都不能确定卡片已经送达，一律按"未送达"处理，让本次操作
            # 作废（合同"卡片发送失败时本次操作不执行，不根据失败原因推断人员
            # 状态"）。区分"明确拒绝"与"结果不明"只影响未来是否值得重试发送这个
            # 决策，不影响当前这条待确认操作的可用性判定。
            self._tracker.mark_send_failed(pending_action_id=pending.id)
            return CardDispatchResult(delivered=False)

        self._tracker.mark_card_delivered(pending_action_id=pending.id, card_id=created.card_id)
        return CardDispatchResult(delivered=True)
