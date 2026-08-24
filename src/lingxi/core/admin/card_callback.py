"""``card.action.trigger`` 回调的编排：解析出的按钮点击 → confirm/cancel → 卡片终态
更新 → 管理群脱敏通知。

只依赖注入的 ``Protocol`` 端口，不 import ``adapters/``（代码框架第二节）。真实装配
见 ``apps/gateway/__init__.py``；测试注入内存假实现。

## 为什么是一个独立类而不是塞进 ``core/admin/router.py``

``AdminCommandRouter`` 处理的是私聊**文本**命令；本类处理的是**卡片按钮回调**，
两者的输入形状（``open_id``+``text`` vs. ``open_id``+``pending_action_id``+
``decision``）、返回形状（一段回复文本 vs. "卡片要不要更新、群里要不要通知"）都不
相同，唯一的耦合点是"发起 suspend/resume 时经 router 调用同一个
``PendingActionPreparer`` 端口"——这条耦合已经在 ``router.py`` 里，不需要两个编排器
互相 import。

## 卡片更新与群通知为什么分别用不同的判据触发

卡片更新（渲染成终态、去掉按钮）只要"这一行现在确实是终态"就该做，即使这次点击
本身没有让它**新**产生终态（例如同一张卡片被点了两次，第二次只是幂等重放）——重复
调用 ``AdminCardTransport.update()`` 是无害的，用它换取"即使第一次更新因为网络原因
失败，后续点击仍有机会让卡片视觉收敛"这一点好处。

群通知则完全不同：向管理群发一条"已确认执行"的通知，如果每次重复点击都重发一条，
管理群会被同一件事刷屏。因此群通知只在"这次点击**首次**让它落进终态"（即
``ConfirmDecision``/``CancelDecision`` 的 ``terminal_status`` 字段非空）时才发送，
幂等重放（``ALREADY_TERMINAL``）与未改变任何状态的拒绝（``NOT_INITIATOR``）都不
触发第二条通知。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from lingxi.core.admin.notification import (
    DECISION_CANCEL,
    DECISION_CONFIRM,
    AdminCardTransport,
    GroupNotifier,
    render_group_notice,
    render_terminal_card,
)
from lingxi.core.admin.pending_action import (
    PendingAction,
    PendingActionAuditWriteFailed,
    PendingActionStatus,
)


class _Decision(Protocol):
    """``ConfirmDecision``/``CancelDecision`` 的公共结构面：本类只用得到这四个
    字段/属性，用结构类型而不是 import 两个具体 dataclass，避免多引入一层耦合。
    """

    ok: bool
    message: str
    terminal_status: PendingActionStatus | None


class _Outcome(Protocol):
    decision: _Decision
    pending: PendingAction | None


class PendingActionDecider(Protocol):
    """confirm()/cancel() 两个真正改变状态的调用面。与
    ``adapters.postgres_pending_action.PostgresPendingActionStore`` 结构相同，
    测试注入内存假实现。真实实现在审计写入失败时抛出
    :class:`~lingxi.core.admin.pending_action.PendingActionAuditWriteFailed`。
    """

    def confirm(self, *, pending_action_id: str, clicker_open_id: str) -> _Outcome: ...

    def cancel(self, *, pending_action_id: str, clicker_open_id: str) -> _Outcome: ...


class AuditSink(Protocol):
    """与 ``core/admin/router.AuditSink``/``adapters/postgres_pending_action.
    AuditSink`` 结构相同的独立 Protocol——三处不互相 import，见模块与那两处各自
    的文档。"""

    def record(self, action: str, /, **fields: object) -> None: ...


@dataclass(frozen=True)
class CardCallbackOutcome:
    """交给调用方（``apps/gateway``）的最终结论。``ok`` 只表示"这次点击是否让
    对应的写动作真正执行成功"——``ok=False`` 覆盖取消、过期、拒绝等全部非执行结果，
    调用方不需要（也不应该）在这个结论之外再做任何业务判断。
    """

    ok: bool
    message: str


class AdminCardCallbackHandler:
    """``card.action.trigger`` 事件的唯一处理入口。见模块文档。"""

    def __init__(
        self,
        *,
        pending_actions: PendingActionDecider,
        confirm_cards: AdminCardTransport,
        group_notifier: GroupNotifier | None,
        group_chat_id: str | None,
        audit: AuditSink,
    ) -> None:
        self._pending_actions = pending_actions
        self._confirm_cards = confirm_cards
        self._group_notifier = group_notifier
        self._group_chat_id = group_chat_id
        self._audit = audit

    def handle(
        self, *, operator_open_id: str, pending_action_id: str, decision: str, trace_id: str
    ) -> CardCallbackOutcome:
        """处理一次卡片按钮点击。

        ``decision`` 只识别 ``"confirm"``/``"cancel"`` 两个字面量（见
        ``core/admin/notification.py`` 的 ``DECISION_CONFIRM``/``DECISION_CANCEL``
        常量）；不认识的取值（篡改的回调 payload）记审计后原样拒绝，不猜测意图。
        """

        if decision not in (DECISION_CONFIRM, DECISION_CANCEL):
            self._audit.record(
                "admin.card_callback.unknown_decision", trace_id=trace_id
            )
            return CardCallbackOutcome(ok=False, message="")

        try:
            if decision == DECISION_CONFIRM:
                outcome = self._pending_actions.confirm(
                    pending_action_id=pending_action_id, clicker_open_id=operator_open_id
                )
            else:
                outcome = self._pending_actions.cancel(
                    pending_action_id=pending_action_id, clicker_open_id=operator_open_id
                )
        except PendingActionAuditWriteFailed:
            # 审计写入失败：事务已回滚，pending_action 与目标账号均未改变。不更新
            # 卡片终态（这次点击结构上"没有发生过"，卡片仍然是可以重新点击的
            # pending 态，符合接口设计错误码 audit_write_failed 的"调用方可重试"），
            # 只记一条审计。
            self._audit.record(
                "admin.card_callback.audit_write_failed",
                pending_action_id=pending_action_id,
                trace_id=trace_id,
            )
            return CardCallbackOutcome(
                ok=False, message="本次操作因内部错误未能完成，请稍后重试。"
            )

        if outcome.pending is None:
            # 从未存在过的待确认操作 ID（含伪造回调）——没有可更新的卡片，只记审计。
            self._audit.record(
                "admin.card_callback.not_found",
                pending_action_id=pending_action_id,
                trace_id=trace_id,
            )
            return CardCallbackOutcome(ok=False, message="")

        pending = outcome.pending
        self._audit.record(
            "admin.card_callback.handled",
            pending_action_id=pending.id,
            decision=decision,
            status=pending.status.value,
            trace_id=trace_id,
        )

        # 卡片视觉收敛：只要这一行**现在**是终态就尝试刷新展示，不要求这次点击本身
        # 是让它第一次落进终态的那一次（见模块文档）。
        if pending.status is not PendingActionStatus.PENDING:
            self._update_card_to_terminal(pending)

        # 群通知：只在这次点击**首次**产生了新的终态时发送，避免幂等重放刷屏。
        if outcome.decision.terminal_status is not None:
            self._notify_group(pending, outcome_message=outcome.decision.message)

        return CardCallbackOutcome(ok=outcome.decision.ok, message=outcome.decision.message)

    def _update_card_to_terminal(self, pending: PendingAction) -> None:
        if pending.card_id is None:
            return
        card = render_terminal_card(
            pending, target_label=pending.target_open_id, outcome_text=_outcome_text(pending)
        )
        try:
            self._confirm_cards.update(card_id=pending.card_id, card=card)
        except Exception as error:  # noqa: BLE001 - 卡片视觉更新失败不影响已经落库的业务结果
            self._audit.record(
                "admin.card_callback.card_update_failed",
                pending_action_id=pending.id,
                error=type(error).__name__,
            )

    def _notify_group(self, pending: PendingAction, *, outcome_message: str) -> None:
        del outcome_message  # 群通知只用脱敏摘要（见 render_group_notice），不转发原文
        if self._group_notifier is None or not self._group_chat_id:
            return
        text = render_group_notice(pending, outcome_text=_outcome_text(pending))
        try:
            self._group_notifier.send_text(
                chat_id=self._group_chat_id, text=text, dedupe_key=pending.id
            )
        except Exception as error:  # noqa: BLE001 - 群通知失败不影响已经落库的业务结果
            self._audit.record(
                "admin.card_callback.group_notify_failed",
                pending_action_id=pending.id,
                error=type(error).__name__,
            )


def _outcome_text(pending: PendingAction) -> str:
    if pending.status is PendingActionStatus.EXECUTED:
        return "已确认执行"
    if pending.status is PendingActionStatus.CANCELLED:
        return "已取消"
    if pending.status is PendingActionStatus.EXPIRED:
        return "已过期，未执行"
    if pending.status is PendingActionStatus.FAILED:
        return f"未执行（{pending.reason or '内部原因'}）"
    return "状态未知"  # pragma: no cover - 调用点已确保 status 非 PENDING
