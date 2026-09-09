"""真实处理器与真实进程覆盖发布等待、失权和信号重入窗口。"""

import contextlib
import os
import signal
import subprocess
import sys
import textwrap
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from lingxi.adapters.innertest_confirmation_card import InnertestConfirmationCard
from lingxi.apps.gateway.admin_followups import GatewayFollowupHandlers
from lingxi.core.admin.followup_consumer import FollowupConsumer
from lingxi.core.admin.followup_renewal import FollowupLeaseKeeper
from lingxi.core.permission.targeted_recompute import RecomputeKind, TargetedRecomputeOutcome


class GatewayRecomputeRecoveryTests(unittest.TestCase):
    def test_unchanged_crash_recovery_waits_for_current_publish(self):
        for state in ("pending", "publishing", "published", "failed", "superseded"):
            with self.subTest(state=state):
                store, reporter = Mock(), Mock()
                store.current_publish_reference.return_value = ("pub_current", 2)
                store.dependency_publish_state.return_value = state
                handlers = GatewayFollowupHandlers(
                    store=store,
                    pending_actions=Mock(),
                    callback=Mock(),
                    recompute=Mock(
                        trigger=Mock(
                            return_value=TargetedRecomputeOutcome(kind=RecomputeKind.UNCHANGED)
                        )
                    ),
                    cards=SimpleNamespace(reporter=reporter),
                )
                item = SimpleNamespace(id="f", subject_key="u", target_user_id="u")
                pending = SimpleNamespace(id="p")
                result = handlers._recompute(item, pending)
                reporter.on_completed.assert_not_called()
                self.assertEqual(result.external_ref, "pub_current")
                self.assertEqual(result.next_items[0].target_version, 2)
                self.assertEqual(result.next_items[0].depends_on_id, "f")
                observed = handlers._observe(result.next_items[0], pending)
                self.assertEqual(reporter.on_completed.call_count, int(state == "published"))
                if state in {"pending", "publishing"}:
                    self.assertEqual(observed.status, "retry_wait")
                elif state in {"failed", "superseded"}:
                    reporter.on_failed.assert_called_once()

    def test_skipped_updates_management_card_with_original_reason(self):
        reporter = Mock()
        outcome = TargetedRecomputeOutcome(
            kind=RecomputeKind.SKIPPED, reason="missing_roster_snapshot"
        )
        handlers = GatewayFollowupHandlers(
            store=Mock(),
            pending_actions=Mock(),
            callback=Mock(),
            recompute=Mock(trigger=Mock(return_value=outcome)),
            cards=SimpleNamespace(reporter=reporter),
        )
        pending = SimpleNamespace(id="p")
        result = handlers._recompute(SimpleNamespace(), pending)
        self.assertEqual(result.status, "skipped")
        reporter.on_skipped.assert_called_once_with(pending, outcome)
        reporter.on_completed.assert_not_called()


class ConfirmationLeaseTests(unittest.TestCase):
    def run_card(self, lose_at, *, existing_card=None, renew_error=False):
        item = SimpleNamespace(
            id="f",
            pending_action_id="p",
            stage="confirmation_card_send",
            attempt=1,
            lease_owner="owner",
            trace_id="t",
            batch_id="b",
            batch_item_id=None,
        )
        store = Mock()
        store.claim_followup.return_value = item
        store.mark_effect_started.return_value = True
        store.renew_lease.return_value = False
        if renew_error:
            store.renew_lease.side_effect = TimeoutError()
        cursor = Mock(rowcount=1)
        connection = SimpleNamespace(cursor=lambda: contextlib.nullcontext(cursor))
        store.transaction.side_effect = lambda: contextlib.nullcontext(connection)
        sender = Mock(return_value="message")
        handler = InnertestConfirmationCard(
            store=store,
            service=Mock(),
            create_card=Mock(return_value="card"),
            send_card=sender,
        )
        consumer = FollowupConsumer(
            store=store,
            consumer_kind="postprocess",
            owner="owner",
            handlers={item.stage: handler},
            audit=Mock(),
        )
        keeper = object.__new__(FollowupLeaseKeeper)
        keeper.consumers, keeper.audit = (consumer,), Mock()
        reads = []

        def read(_item):
            reads.append(1)
            if lose_at == "read" and len(reads) == 2:
                keeper.renew_once()
            return "recipient", Mock(), existing_card

        handler._read = read
        handler._delivered = Mock()
        if lose_at == "create":

            def create(_payload):
                keeper.renew_once()
                return "card"

            handler.create_card.side_effect = create
        if lose_at == "db":
            store.mark_effect_started.side_effect = (True, False)
        from unittest.mock import patch

        with patch(
            "lingxi.adapters.innertest_confirmation_card.render_card_payload", return_value={}
        ):
            consumer.run_once()
        return store, handler, sender

    def test_create_card_then_keeper_loss_prevents_send(self):
        for error in (False, True):
            with self.subTest(renew_error=error):
                store, handler, sender = self.run_card("create", renew_error=error)
                handler.create_card.assert_called_once()
                sender.assert_not_called()
                self.assertEqual(store.complete_followup.call_args.kwargs["status"], "unknown")

    def test_second_read_then_keeper_loss_prevents_send(self):
        store, _, sender = self.run_card("read", existing_card="card")
        sender.assert_not_called()
        self.assertEqual(store.complete_followup.call_args.kwargs["status"], "unknown")

    def test_database_owner_or_expiry_check_prevents_send(self):
        store, _, sender = self.run_card("db", existing_card="card")
        sender.assert_not_called()
        self.assertEqual(store.complete_followup.call_args.kwargs["status"], "unknown")

    def test_current_owner_can_send_once(self):
        _, handler, sender = self.run_card(None, existing_card="card")
        sender.assert_called_once()
        handler._delivered.assert_called_once()


class SchedulerSignalRegressionTests(unittest.TestCase):
    def test_signal_during_recovery_prevents_followup_claim_before_resource_stop(self):
        from lingxi.apps.scheduler.loop import SchedulerLoop

        loop = SchedulerLoop(duties=[Mock()])
        store = Mock()
        store.recover_expired.side_effect = lambda **kwargs: loop.signal_stop()
        consumer = FollowupConsumer(
            store=store,
            consumer_kind="scheduler",
            owner="owner",
            handlers={},
            audit=Mock(),
            stop=loop.stop_event,
        )
        self.assertFalse(consumer.run_once())
        store.claim_followup.assert_not_called()

    def test_sigterm_inside_submit_lock_returns_and_shares_first_deadline(self):
        script = textwrap.dedent("""
            import os, signal, time
            from types import SimpleNamespace
            from lingxi.apps.scheduler.loop import SchedulerLoop, install_signal_handlers
            from lingxi.apps.scheduler.onboarding import OnboardingExecutor
            executor = OnboardingExecutor(workers=1)
            executor.start()
            calls = []
            loop = SchedulerLoop(duties=[SimpleNamespace(onboarding_executor=executor,
                run_once=lambda: calls.append("unexpected"))])
            install_signal_handlers(loop)
            original = executor._queue.put_nowait
            def put(task):
                with loop.stop_event._cond:
                    os.kill(os.getpid(), signal.SIGTERM)
                return original(task)
            executor._queue.put_nowait = put
            executor.submit(lambda: None)
            executor._queue.put_nowait = original
            assert loop.stopping and loop.stop_event.is_set()
            first = loop._signal_requested_at
            time.sleep(0.01)
            os.kill(os.getpid(), signal.SIGTERM)
            loop.run_once()
            assert calls == []
            assert not executor.submit(lambda: None)
            assert loop.lifecycle.deadline == first + 120
            assert loop.drain_until(time.monotonic()+2).still_running == 0
            print("signal-safe, zero subsequent claims, one deadline")
        """)
        completed = subprocess.run(
            [sys.executable, "-B", "-c", script],
            text=True,
            capture_output=True,
            timeout=5,
            env=os.environ.copy(),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("zero subsequent claims", completed.stdout)

    def test_sigterm_logs_the_stop_notice_outside_the_signal_handler(self):
        """V-部署-05 靠这行日志判定优雅停机；信号处理函数内写日志会与日志锁重入，
        因此断言两件事：处理函数返回时还没有日志，主循环回到安全位置后补记一次。"""
        script = textwrap.dedent("""
            import logging, os, signal, sys
            from types import SimpleNamespace
            from lingxi.apps.scheduler.loop import SchedulerLoop, install_signal_handlers

            logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(message)s")
            loop = SchedulerLoop(duties=[SimpleNamespace(run_once=lambda: None)],
                interval_seconds=0.01)
            install_signal_handlers(loop)
            os.kill(os.getpid(), signal.SIGTERM)
            assert loop.stopping
            print("HANDLER-RETURNED", flush=True)
            loop.run_forever()
            assert loop.drain_until().still_running == 0
        """)
        completed = subprocess.run(
            [sys.executable, "-B", "-c", script],
            text=True,
            capture_output=True,
            timeout=15,
            env=os.environ.copy(),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        notice = f"收到信号，停止领取新的到期凭据 signal={int(signal.SIGTERM)}"
        self.assertIn(notice, completed.stdout)
        # 处理函数自身不写日志：该行必须晚于处理函数返回的标记。
        self.assertLess(completed.stdout.index("HANDLER-RETURNED"), completed.stdout.index(notice))
