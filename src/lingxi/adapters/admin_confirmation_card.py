"""Gateway 消费五种管理写动作的持久发卡阶段：现读待确认操作与发起人角色，按本人直发。

与扩员发卡器同一姿态：网络在事务外；CardKit 建卡后先持久保存 ``card_id`` 再发送；
平台明确拒绝才转发送失败，结果不明保持 ``unknown`` 且不重发。卡片只发给发起人本人
（``initiated_by_open_id``），发起人此刻不再是三角色全真的管理员就一张也不发。
"""

from __future__ import annotations

from datetime import UTC, datetime

from lingxi.core.admin.followup_consumer import FollowupResult
from lingxi.core.admin.followup_effect import effect_allowed
from lingxi.core.admin.notification import (
    permission_scope_ids,
    render_card_payload,
    render_confirm_card,
)
from lingxi.core.admin.pending_action import PendingActionStatus, PendingActionType
from lingxi.core.admin.registry import is_authorized_admin

#: 经持久发卡阶段送卡的五种写动作；扩员另有自己的发卡器。
ADMIN_CARD_ACTION_TYPES = frozenset(
    {
        PendingActionType.SUSPEND_USER,
        PendingActionType.RESUME_USER,
        PendingActionType.LOCAL_PERMISSION_GRANT,
        PendingActionType.LOCAL_PERMISSION_SUPPRESS,
        PendingActionType.LOCAL_PERMISSION_REVOKE,
    }
)


class ConfirmationCardDispatch:
    """``confirmation_card_send`` 阶段按待确认动作类型分派；没有对应发卡器就保持待处理。"""

    def __init__(self, *, pending_actions, admin, innertest=None):
        """``innertest`` 为空表示本进程没有装配扩员入口，扩员阶段留待正确装配的进程。"""
        self._pending_actions, self._admin, self._innertest = pending_actions, admin, innertest

    def __call__(self, item):
        """分派表只有两项，动作类型不是可执行字符串。"""
        pending = self._pending_actions.get(pending_action_id=item.pending_action_id)
        if pending is None:
            return FollowupResult("skipped", "action_missing")
        if pending.action_type is PendingActionType.INNERTEST_ADDITIONS:
            handler = self._innertest
        else:
            handler = self._admin if pending.action_type in ADMIN_CARD_ACTION_TYPES else None
        if handler is None:
            return FollowupResult("retry_wait", "handler_unavailable")
        return handler(item)


class AdminConfirmationCard:
    """五种写动作的确认卡发送器；发送前后都现读待确认操作与发起人角色。"""

    def __init__(self, *, store, pending_actions, registry, display_names, create_card, send_card):
        """``registry`` 每次现读登记表；``display_names`` 把目标翻译成姓名与中文标签。"""
        self._store, self._pending_actions, self._registry = store, pending_actions, registry
        self._display_names = display_names
        self._create_card, self._send_card = create_card, send_card

    def __call__(self, item):
        """现读 → 建卡并落 ``card_id`` → 再现读 → 发送 → 送达同事务落库。"""
        pending = self._current(item)
        if pending is None:
            return FollowupResult("skipped", "stale_confirmation")
        if not is_authorized_admin(
            self._registry.active_entry(open_id=pending.initiated_by_open_id)
        ):
            return FollowupResult("skipped", "not_authorized")
        card_id = pending.card_id
        if card_id is None:
            if not self._may_start_effect(item):
                return FollowupResult("unknown", "lease_lost")
            card_id = self._create_card(render_card_payload(self._render(pending)))
            if not self._persist_card_id(item, card_id):
                return FollowupResult("unknown", "card_unknown")
        if self._current(item) is None or not self._may_start_effect(item):
            return FollowupResult("unknown", "lease_lost")
        try:
            message_id = self._send_card(
                open_id=pending.initiated_by_open_id,
                card={"type": "card", "data": {"card_id": card_id}},
                dedupe_key=item.id,
            )
        except Exception as error:
            if getattr(error, "definite", False):
                self._pending_actions.mark_send_failed(pending_action_id=pending.id)
                return FollowupResult("failed", "card_failed")
            return FollowupResult("unknown", "card_unknown")
        if not message_id:
            return FollowupResult("unknown", "card_unknown")
        self._delivered(item, card_id, message_id)
        return FollowupResult(external_ref=message_id)

    def _current(self, item):
        """仍在途、未过期、未送达的那一条；否则 ``None``。"""
        pending = self._pending_actions.get(pending_action_id=item.pending_action_id)
        if (
            pending is None
            or pending.status is not PendingActionStatus.PENDING
            or pending.is_expired(now=datetime.now(UTC))
            or pending.card_delivered
        ):
            return None
        return pending

    def _render(self, pending):
        """标签经展示名口翻译：目标永远是姓名，不回显内部标识。"""
        scope = permission_scope_ids(pending)
        return render_confirm_card(
            pending,
            target_label=self._display_names.user_label(open_id=pending.target_open_id),
            company_label=self._display_names.company_label(company_id=scope[0]) if scope else None,
            metric_label=self._display_names.metric_label(metric_id=scope[1]) if scope else None,
        )

    def _may_start_effect(self, item):
        """失权后不再创建或发送；持久标记成功后再读一次续租失败标志。"""
        return (
            effect_allowed()
            and self._store.mark_effect_started(
                id=item.id, owner=item.lease_owner, attempt=item.attempt, now=datetime.now(UTC)
            )
            and effect_allowed()
        )

    def _persist_card_id(self, item, card_id):
        """``card_id`` 只在本代领取仍有效且动作仍在途时写入。"""
        with self._store.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE pending_action p SET card_id=%s FROM admin_action_followup f "
                "WHERE p.id=%s AND f.id=%s AND f.lease_owner=%s AND f.attempt=%s "
                "AND f.status='running' AND f.lease_until>now() AND p.status='pending'",
                (card_id, item.pending_action_id, item.id, item.lease_owner, item.attempt),
            )
            return cursor.rowcount == 1

    def _delivered(self, item, card_id, message_id):
        """平台接收与阶段成功同事务；中断仍有外发标记而不是未发。"""
        with self._store.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE admin_action_followup SET status='succeeded',result_code='card_delivered',"
                "external_ref=%s,finished_at=now(),updated_at=now(),lease_owner=NULL,lease_until=NULL "
                "WHERE id=%s AND status='running' AND lease_owner=%s AND attempt=%s RETURNING id",
                (message_id, item.id, item.lease_owner, item.attempt),
            )
            if cursor.fetchone() is None:
                return
            cursor.execute(
                "UPDATE pending_action SET card_delivered=true,card_id=%s,"
                "card_sequence=GREATEST(card_sequence,2) WHERE id=%s AND status='pending'",
                (card_id, item.pending_action_id),
            )
