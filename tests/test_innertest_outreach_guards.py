"""真实装配与完整清单链保留就绪守卫和结果不明状态。"""

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_outreach_ops import TOOL, _facts, _recipients

from lingxi.adapters.innertest_outreach import CheckedInnertestSender
from lingxi.core.admin.innertest import InnertestError
from lingxi.core.outreach.dispatch import OutreachDispatcher, OutreachPurpose, ReservedRecord


class OutreachGuardTests(unittest.TestCase):
    def test_unknown_record_full_cli_path_zero_resend_and_pending_verification(self):
        for existing in (True, False):
            with self.subTest(existing=existing):
                store, sender = Mock(), Mock()
                store.reserve.return_value = ReservedRecord(
                    "record", "key", "unknown" if existing else "pending", 1
                )
                sender.send_card.side_effect = InnertestError("notification_unknown")
                dispatcher = OutreachDispatcher(sender=sender, store=store, audit=Mock())
                results = TOOL.run_outreach(
                    _recipients(_facts()),
                    dispatcher=dispatcher,
                    purpose=OutreachPurpose.APPLY,
                    admin_open_id=None,
                    run_id="test",
                )
                self.assertEqual(results[0].status, "unknown")
                self.assertEqual(results[0].detail, "notification_unknown")
                self.assertEqual(sender.send_card.call_count, 0 if existing else 1)
                output = io.StringIO()
                with redirect_stdout(output), redirect_stderr(output):
                    TOOL.print_results(results, purpose=OutreachPurpose.APPLY)
                    self.assertEqual(TOOL._exit_code(results, alert_error=None), 3)
                self.assertIn("此前已送达 0", output.getvalue())
                self.assertIn("待核实 1", output.getvalue())
                self.assertIn("不要盲目重发", output.getvalue())

    def test_all_roster_modes_assemble_checked_sender_and_probe_errors_refuse(self):
        for scope in (None, "legacy", "database"):
            config = SimpleNamespace(
                innertest_scope=scope,
                feishu_base_url="https://example.test",
                feishu_app_id="synthetic",
                feishu_app_secret="synthetic",
            )
            with (
                self.subTest(scope=scope),
                patch("lingxi.apps.scheduler.alerting_assembly.build_alerting_duty"),
                patch(
                    "lingxi.apps.scheduler.contact_reachability_assembly."
                    "build_contact_reachability_recorder"
                ),
                patch("lingxi.apps.scheduler.innertest._build_probe") as builder,
            ):
                dispatcher, _ = TOOL.build_dispatcher(config, "synthetic", initiated_by="admin")
                self.assertIsInstance(dispatcher._sender, CheckedInnertestSender)
                builder.assert_called_once_with(config)
                guard = dispatcher._sender
                guard.sender = Mock()
                guard._authorized = Mock()
                guard._snapshot = Mock(return_value=("user", 1, "publish"))
                guard._record_check = Mock()
                guard._start_effect = Mock()
                guard.probe.list_metrics.side_effect = PermissionError()
                with self.assertRaisesRegex(InnertestError, "check_unknown"):
                    guard.send_card(
                        open_id="open",
                        card={},
                        dedupe_key="welcome:apply:user",
                        permission_snapshot=("user", 1, "publish"),
                    )
                guard.sender.send_card.assert_not_called()
                guard.probe.list_metrics.assert_called_once_with(user_id="user")
                builder.side_effect = ValueError("missing probe configuration")
                with self.assertRaises(ValueError):
                    TOOL.build_dispatcher(config, "synthetic", initiated_by="admin")

    def test_missing_or_stale_card_snapshot_refuses_before_probe(self):
        guard = CheckedInnertestSender(
            dsn="synthetic", sender=Mock(), probe=Mock(), initiated_by="admin"
        )
        guard._authorized = Mock()
        guard._snapshot = Mock(return_value=("user", 2, "publish2"))
        for snapshot in (None, ("user", 1, "publish1"), ("user", 2, "old_publish")):
            with (
                self.subTest(snapshot=snapshot),
                self.assertRaisesRegex(InnertestError, "check_version_changed"),
            ):
                guard.send_card(
                    open_id="open",
                    card={},
                    dedupe_key="welcome:apply:user",
                    permission_snapshot=snapshot,
                )
        guard.probe.list_metrics.assert_not_called()
        guard.sender.send_card.assert_not_called()
