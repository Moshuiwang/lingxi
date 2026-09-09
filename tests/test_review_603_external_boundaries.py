"""逐个实际后台外发入口在最后一次回读之后核对当前领取。"""

import contextlib
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lingxi.apps.gateway.admin_followups import GatewayFollowupHandlers
from lingxi.apps.gateway.management_cards import ManagementCardRefresher
from lingxi.core.admin.followup_consumer import FollowupConsumer
from lingxi.core.admin.followup_renewal import FollowupLeaseKeeper


class FollowupExternalBoundaryTests(unittest.TestCase):
    def setup_stage(self, stage):
        item = SimpleNamespace(
            id="f",
            stage=stage,
            attempt=1,
            lease_owner="owner",
            pending_action_id="p",
            trace_id="t",
            batch_id=None,
            batch_item_id=None,
        )
        store = Mock()
        store.db_slots = contextlib.nullcontext()
        store.claim_followup.side_effect = (item, None)
        store.mark_effect_started.return_value = True
        store.renew_lease.return_value = False
        pending = SimpleNamespace(
            id="p", target_open_id="target", card_id="card", origin_card_message_id="message"
        )
        actions = Mock()
        actions.get.return_value = pending
        cb = Mock()
        cb._group_chat_id = "chat"
        cb._resolve_scope_labels.return_value = ("company", "metric")
        cb._display_names.user_label.return_value = "person"
        cb._render_terminal_card.return_value = (Mock(), None)
        cards = Mock()
        handlers = GatewayFollowupHandlers(
            store=store, pending_actions=actions, callback=cb, recompute=Mock(), cards=cards
        )
        consumer = FollowupConsumer(
            store=store,
            consumer_kind="postprocess",
            owner="owner",
            handlers={stage: handlers.handle},
            audit=Mock(),
        )
        keeper = object.__new__(FollowupLeaseKeeper)
        keeper.consumers, keeper.audit = (consumer,), Mock()
        return consumer, keeper, store, handlers

    def test_group_notice_rechecks_after_scope_and_name_reads(self):
        for source in ("scope", "name", "database", "healthy"):
            with self.subTest(source=source):
                consumer, keeper, store, handlers = self.setup_stage("group_notify")
                cb = handlers.callback

                def scope(_pending):
                    keeper.renew_once()
                    return "company", "metric"

                def name(**kwargs):
                    if source == "database":
                        store.mark_effect_started.return_value = False
                    else:
                        keeper.renew_once()
                    return "person"

                if source == "scope":
                    cb._resolve_scope_labels.side_effect = scope
                elif source != "healthy":
                    cb._display_names.user_label.side_effect = name
                with patch(
                    "lingxi.apps.gateway.admin_followups.render_group_notice", return_value="notice"
                ):
                    self.assertTrue(consumer.run_once())
                if source == "healthy":
                    cb._group_notifier.send_text.assert_called_once()
                else:
                    cb._group_notifier.send_text.assert_not_called()
                    self.assertEqual(store.complete_followup.call_args.kwargs["status"], "unknown")
                self.assertFalse(consumer.run_once())

    def test_terminal_card_rechecks_after_sequence_read(self):
        for lost in (False, True):
            with self.subTest(lost=lost):
                consumer, keeper, store, handlers = self.setup_stage("terminal_card_refresh")

                def sequence(**kwargs):
                    if lost:
                        keeper.renew_once()
                    return 2

                handlers.pending_actions.next_card_sequence.side_effect = sequence
                consumer.run_once()
                self.assertEqual(handlers.callback._confirm_cards.update.call_count, int(not lost))
                if lost:
                    self.assertEqual(
                        store.retry_followup.call_args.kwargs["result_code"], "lease_lost"
                    )

    def test_management_card_rechecks_inside_refresher_after_sequence_read(self):
        for source in ("keeper", "database", "healthy"):
            with self.subTest(source=source):
                consumer, keeper, store, handlers = self.setup_stage("management_card_refresh")
                context = SimpleNamespace(
                    identifier="target",
                    message_id="message",
                    card_id="card",
                    card_sequence=1,
                    state="dispatching",
                    dispatch_status="publishing",
                )
                handlers.cards.context_store.lookup_context.return_value = context
                transport = Mock()
                context_store = handlers.cards.context_store

                def sequence(**kwargs):
                    if source == "keeper":
                        keeper.renew_once()
                    elif source == "database":
                        store.mark_effect_started.return_value = False
                    return 2

                context_store.next_card_sequence.side_effect = sequence
                handlers.cards.refresher = ManagementCardRefresher(
                    transport=transport,
                    catalog=Mock(),
                    display_names=Mock(),
                    context_store=context_store,
                )
                with (
                    patch(
                        "lingxi.apps.gateway.management_cards.render_management_card",
                        return_value={},
                    ),
                    patch(
                        "lingxi.apps.gateway.management_cards.rendered_dispatch_status",
                        return_value="publishing",
                    ),
                ):
                    consumer.run_once()
                self.assertEqual(transport.update.call_count, int(source == "healthy"))
                if source != "healthy":
                    context_store.mark_visual_refreshed.assert_not_called()
