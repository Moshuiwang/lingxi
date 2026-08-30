"""确认卡片的发送编排：渲染 → 通过 ``AdminCardTransport`` 发送 → 把结果落回待确认操作。

只依赖注入的 ``Protocol`` 端口，不 import ``adapters/``。真实装配见
``apps/gateway/__init__.py``。

## 为什么"发送 + 落库标记"合成一个方法

合同"卡片发送失败时本次操作不执行"要求"发送结果"与"待确认操作是否可被确认"两件
事不能出现中间态（发了但没记、记了但其实没发）。把两步都放进同一个类的同一次调用
里，让调用方（``core/admin/router.py``）只需要处理一个返回值（"是否已确认送达"），
不需要自己记得"发送成功之后一定要调 ``mark_card_delivered``"这类跨对象的调用顺序。

## 为什么 ``send()`` 的失败分支现在需要注入 ``AuditSink``

此前 ``except Exception:`` 只把结果归一为"未送达"再调用 ``mark_send_failed``，
异常本身（类名、若是 ``AdminCardDeliveryRejected`` 则还有 ``code``/``log_id``）
从未落到任何审计或日志里。2026-08-25 定位一次真实走查报告的"确认卡片发送失败"
故障时，唯一能读到的痕迹只有 ``pending_action.status='failed'`` 这一行，没有任何
字段能回答"到底是哪一类失败"，被迫改用受控探针直接打 CardKit 接口才定位到
``action`` 容器被 schema 2.0 拒绝（见 ``adapters/feishu_admin_card.py`` 模块文档
"建卡环节已被真实探针证伪并修复"）。这个类现在注入与 ``core/admin/router.
AuditSink``/``core/admin/card_callback.AuditSink`` 结构相同的独立 ``AuditSink``，
在失败分支记一条 ``admin.card_dispatch.send_failed`` 审计（异常类名 + 明确拒绝时
的 ``code``/``log_id``），不带卡片正文、不带 ``chat_id``/``reply_to_message_id``/
目标 ``open_id`` 等外部标识明文——与 ``card_callback.py`` 的
``_update_card_to_terminal``/``_notify_group`` 两处失败分支同一姿态（只记异常
类名，不记可能带资料的异常消息全文）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from lingxi.core.admin.management_card import (
    CompanyMetricCatalog,
    ManagementCardTransport,
    render_management_card,
)
from lingxi.core.admin.notification import (
    AdminCardDeliveryRejected,
    AdminCardTransport,
    render_confirm_card,
)
from lingxi.core.admin.pending_action import PendingAction
from lingxi.core.admin.views import AdminUserStatusView


class PendingActionDeliveryTracker(Protocol):
    """卡片发送结果的落库口——与
    ``adapters.postgres_pending_action.PostgresPendingActionStore`` 的
    ``mark_card_delivered``/``mark_send_failed`` 两个方法结构相同。"""

    def mark_card_delivered(self, *, pending_action_id: str, card_id: str) -> None: ...

    def mark_send_failed(self, *, pending_action_id: str) -> None: ...


class AuditSink(Protocol):
    """与 ``core/admin/router.AuditSink``/``core/admin/card_callback.AuditSink``/
    ``adapters/postgres_pending_action.AuditSink`` 结构相同的独立 Protocol——四处
    不互相 import，见各自模块文档。"""

    def record(self, action: str, /, **fields: object) -> None: ...


@dataclass(frozen=True)
class CardDispatchResult:
    delivered: bool


class ConfirmCardDispatcher:
    """把一条刚 ``prepare`` 好的待确认操作渲染成确认卡片，发到发起管理员本人
    私聊（作为触发这条命令的消息的回复），并把发送结果同步落回待确认操作表。
    """

    def __init__(
        self,
        *,
        transport: AdminCardTransport,
        tracker: PendingActionDeliveryTracker,
        audit: AuditSink,
    ) -> None:
        self._transport = transport
        self._tracker = tracker
        self._audit = audit

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
        except Exception as error:
            # 与 core.execution.card_stream 同一白名单纪律：明确拒绝
            # （AdminCardDeliveryRejected）与结果不明（其余异常）在这里得到同一个
            # 处理——两者都不能确定卡片已经送达，一律按"未送达"处理，让本次操作
            # 作废（合同"卡片发送失败时本次操作不执行，不根据失败原因推断人员
            # 状态"）。区分"明确拒绝"与"结果不明"只影响未来是否值得重试发送这个
            # 决策，不影响当前这条待确认操作的可用性判定。
            #
            # 诊断缺口修复（见模块文档"为什么 send() 的失败分支现在需要注入
            # AuditSink"）：下面的分类判断只影响审计带不带 code/log_id，不改变
            # 上面这条"一律按未送达处理"的判定——两类失败在 tracker 这一侧仍然
            # 完全同构。
            if isinstance(error, AdminCardDeliveryRejected):
                self._audit.record(
                    "admin.card_dispatch.send_failed",
                    pending_action_id=pending.id,
                    error=type(error).__name__,
                    code=error.code,
                    log_id=error.log_id,
                )
            else:
                self._audit.record(
                    "admin.card_dispatch.send_failed",
                    pending_action_id=pending.id,
                    error=type(error).__name__,
                )
            self._tracker.mark_send_failed(pending_action_id=pending.id)
            return CardDispatchResult(delivered=False)

        self._tracker.mark_card_delivered(pending_action_id=pending.id, card_id=created.card_id)
        return CardDispatchResult(delivered=True)


@dataclass(frozen=True)
class ManagementCardDispatchResult:
    delivered: bool


class ManagementCardDispatcher:
    """把 ``/admin user`` 查到的用户权限管理卡（#439 B 档）渲染并发到发起管理员
    本人私聊，作为触发这条查询命令的消息的回复。

    与 :class:`ConfirmCardDispatcher` 是两个独立类：管理卡不是一次待确认操作，
    没有"发送结果需要落回某一行状态"这一步（见 ``core/admin/management_card.py``
    模块文档"两张不同的卡"）——发送成功与否只影响这次查询是否额外附带了一张卡，
    不影响任何数据库行的状态机，因此不需要注入 ``PendingActionDeliveryTracker``
    这一类落库口。发送失败只记一条审计，调用方（``core/admin/router.
    AdminCommandRouter._send_management_card``）据此把失败当 best-effort 处理，
    不影响 ``/admin user`` 既有的文本回复。
    """

    def __init__(
        self,
        *,
        transport: ManagementCardTransport,
        catalog: CompanyMetricCatalog,
        audit: AuditSink,
    ) -> None:
        self._transport = transport
        self._catalog = catalog
        self._audit = audit

    def send(
        self,
        *,
        status: AdminUserStatusView,
        display_identifier: str,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
    ) -> ManagementCardDispatchResult:
        card = render_management_card(
            status, display_identifier=display_identifier, catalog=self._catalog
        )
        try:
            self._transport.create(
                chat_id=chat_id,
                thread_id=thread_id,
                reply_to_message_id=reply_to_message_id,
                card=card,
            )
        except Exception as error:
            # 与 ConfirmCardDispatcher.send 同一白名单纪律：明确拒绝与结果不明
            # 在这里得到同一处理——管理卡发送失败不影响任何业务状态（它本来就
            # 不是一次待确认操作），因此不需要"作废"任何东西，只记一条审计。
            if isinstance(error, AdminCardDeliveryRejected):
                self._audit.record(
                    "admin.management_card_dispatch.send_failed",
                    target=display_identifier,
                    error=type(error).__name__,
                    code=error.code,
                    log_id=error.log_id,
                )
            else:
                self._audit.record(
                    "admin.management_card_dispatch.send_failed",
                    target=display_identifier,
                    error=type(error).__name__,
                )
            return ManagementCardDispatchResult(delivered=False)
        return ManagementCardDispatchResult(delivered=True)
