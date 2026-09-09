"""Gateway 固定管理后处理装配，确认事实从数据库重新读取。"""

from __future__ import annotations

from lingxi.core.admin.followup import FollowupSpec
from lingxi.core.admin.followup_budget import FollowupDatabaseBudget
from lingxi.core.admin.followup_consumer import FollowupConsumer, FollowupResult
from lingxi.core.admin.notification import render_group_notice
from lingxi.core.ids import new_id
from lingxi.core.permission.targeted_recompute import RecomputeKind


class GatewayFollowupHandlers:
    """刷新读最新状态；权限重算保留现有实时版本与发布守卫。"""

    def __init__(self, *, store, pending_actions, callback, recompute, cards):
        """只复用既有端口，不复制权限正文或创建观察线程。"""
        self.store, self.pending_actions = store, pending_actions
        self.callback, self.recompute, self.cards = callback, recompute, cards

    def handle(self, item):
        """阶段白名单由注册时固定，未知执行字符串没有入口。"""
        with self.store.db_slots:
            return self._handle_with_budget(item)

    def _handle_with_budget(self, item):
        """业务读取也属于新增活动操作；HTTP 期间没有数据库事务。"""
        pending = self.pending_actions.get(pending_action_id=item.pending_action_id)
        if pending is None:
            return FollowupResult("skipped", "action_missing")
        handlers = {
            "permission_recompute": self._recompute,
            "publish_observe": self._observe,
            "terminal_card_refresh": self._terminal,
            "management_card_refresh": self._management,
            "group_notify": self._notify,
        }
        return handlers[item.stage](item, pending)

    def _recompute(self, item, pending):
        """重算现有权限，不重放确认时的权限快照。"""
        outcome = self.recompute.trigger(pending)
        if outcome.kind is RecomputeKind.SKIPPED:
            return FollowupResult("skipped", outcome.reason or "recompute_skipped")
        if outcome.kind is RecomputeKind.UNCHANGED:
            self.cards.reporter.on_completed(pending)
            return FollowupResult()
        reference = self.store.current_publish_reference(target_user_id=item.target_user_id)
        if reference is None:
            return FollowupResult("retry_wait", "publish_reference_pending")
        next_item = FollowupSpec(
            subject_key=item.subject_key,
            stage="publish_observe",
            target_user_id=item.target_user_id,
            target_version=int(reference[1]),
            depends_on_id=item.id,
        )
        return FollowupResult(external_ref=reference[0], next_items=(next_item,))

    def _observe(self, item, pending):
        """集中循环只查本次重算绑定的发布引用，不因旧发布误报成功。"""
        state = self.store.dependency_publish_state(item)
        if state == "published":
            self.cards.reporter.on_completed(pending)
            return FollowupResult()
        if state in {"failed", "superseded"}:
            self.cards.reporter.on_failed(pending, None)
            return FollowupResult("skipped" if state == "superseded" else "failed", state)
        return FollowupResult("retry_wait", "publish_pending")

    def _terminal(self, item, pending):
        """终态卡使用当前持久确认结果和递增序号。"""
        del item
        card, _ = self.callback._render_terminal_card(pending)
        if card is None:
            return FollowupResult("skipped", "card_missing")
        sequence = self.pending_actions.next_card_sequence(pending_action_id=pending.id)
        self.callback._confirm_cards.update(card_id=pending.card_id, sequence=sequence, card=card)
        return FollowupResult()

    def _management(self, item, pending):
        """只刷新当前管理卡持久状态，不把完成态改回下发中。"""
        del item
        message_id = pending.origin_card_message_id
        if not message_id or not self.cards.reporter._is_current_action(pending, message_id):
            return FollowupResult("skipped", "card_superseded")
        context = self.cards.context_store.lookup_context(message_id=message_id)
        if context is None:
            return FollowupResult("skipped", "card_missing")
        status = self.cards.status_lookup(context.identifier)
        if status is None:
            return FollowupResult("retry_wait", "status_unavailable")
        self.cards.refresher.update(
            context=context,
            status=status,
            state=context.state,
            dispatch_status=context.dispatch_status,
        )
        return FollowupResult()

    def _notify(self, item, pending):
        """通知单独登记、独立授权，异常交给持久未知出口。"""
        del item
        cb = self.callback
        if cb._group_notifier is None or not cb._group_chat_id:
            return FollowupResult("skipped", "notification_not_authorized")
        company, metric = cb._resolve_scope_labels(pending)
        text = render_group_notice(
            pending,
            target_label=cb._display_names.user_label(open_id=pending.target_open_id),
            company_label=company,
            metric_label=metric,
        )
        cb._group_notifier.send_text(chat_id=cb._group_chat_id, text=text, dedupe_key=pending.id)
        return FollowupResult()


def assemble_followups(config, *, pending_actions, callback, cards, lifecycle, audit):
    """三个固定消费者共用最多两个活动数据库操作，不接受内存业务队列。"""
    from lingxi.adapters.postgres_admin_followup import PostgresFollowupStore
    from lingxi.adapters.postgres_permission_recompute_trigger import PermissionRecomputeAdapter

    store = PostgresFollowupStore(
        str(config.postgres_dsn),
        timeouts=config.postgres_timeouts,
        db_slots=FollowupDatabaseBudget(),
    )
    handlers = GatewayFollowupHandlers(
        store=store,
        pending_actions=pending_actions,
        callback=callback,
        cards=cards,
        recompute=PermissionRecomputeAdapter(
            str(config.postgres_dsn),
            timeouts=config.postgres_timeouts,
            audit=audit,
            metric_map_path=config.metric_map_path,
        ),
    )
    consumers = []
    for kind, stages in (
        ("postprocess", ("terminal_card_refresh", "management_card_refresh", "group_notify")),
        ("recompute", ("permission_recompute",)),
        ("observe", ("publish_observe",)),
    ):
        consumer = FollowupConsumer(
            store=store,
            consumer_kind=kind,
            owner=new_id("run"),
            handlers={stage: handlers.handle for stage in stages},
            audit=audit,
        )
        lifecycle.register(consumer)
        consumer.start()
        consumers.append(consumer)
    from lingxi.core.admin.followup_renewal import FollowupLeaseKeeper

    lifecycle.register(FollowupLeaseKeeper(consumers, audit=audit))
