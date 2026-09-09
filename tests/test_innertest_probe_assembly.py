"""真实配置与两条正式装配均到达逐用户探针，仅替换外部读取和常驻启动。"""

import importlib.util
import json
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_outreach_ops import TOOL
from test_scheduler_onboarding_assembly import MASTER_KEY, WIRED_ENV

from lingxi.adapters.mcp_token_cipher import McpTokenCipher
from lingxi.adapters.query_mcp_probe import McpHttpResponse, QueryMcpProbe
from lingxi.apps.scheduler import SchedulerConfig, build_loop
from lingxi.apps.scheduler.innertest import _build_probe
from lingxi.apps.scheduler.onboarding import HardDeadlineProbe
from lingxi.core.permission.mcp_readiness_base import McpProbeError


@unittest.skipUnless(importlib.util.find_spec("cryptography"), "需要 scheduler 加密依赖")
class ProbeAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = SchedulerConfig.from_env(
            {
                **WIRED_ENV,
                "LINGXI_DELEGATED_CREDENTIAL_KEY": MASTER_KEY,
                "LINGXI_DELEGATED_CREDENTIAL_PATH": str(Path(self.temp.name) / "credential"),
                "LINGXI_USER_ENV_ROOT": self.temp.name,
            }
        )
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.token = self.stack.enter_context(
            patch(
                "lingxi.adapters.postgres_mcp_token.PostgresMcpTokenStore.token_cipher",
                autospec=True,
                return_value=McpTokenCipher(MASTER_KEY).encrypt("synthetic-user-token"),
            )
        )
        self.transport = self.stack.enter_context(
            patch("lingxi.adapters.query_mcp_probe.urllib_mcp_transport", side_effect=self.response)
        )

    def response(self, method, url, *, body, token, timeout):
        self.assertEqual(
            (method, url, token, timeout),
            ("POST", self.config.query_mcp_endpoint, "synthetic-user-token", 5),
        )
        self.assertEqual(body["params"], {"name": "list_metrics", "arguments": {}})
        return McpHttpResponse(
            200,
            {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "metrics": [
                                        {
                                            "metric_id": "synthetic",
                                            "name": "合成",
                                            "name_en": "synthetic",
                                        }
                                    ]
                                }
                            ),
                        }
                    ]
                },
            },
        )

    def assert_user_probe(self, probe):
        self.assertIsInstance(probe, HardDeadlineProbe)
        self.assertIsInstance(probe._probe, QueryMcpProbe)
        self.assertEqual(probe._timeout_seconds, 5)
        self.assertEqual(probe.list_metrics(user_id="usr_synthetic"), 1)
        self.assertEqual(self.token.call_args.args[1], "usr_synthetic")
        self.transport.assert_called_once()

    def test_real_config_and_token_decryption_use_user_read_and_empty_arguments(self):
        self.assert_user_probe(_build_probe(self.config))

    def test_missing_token_zero_transport_and_missing_config_rejects(self):
        probe = _build_probe(self.config)
        self.token.return_value = None
        with self.assertRaises(McpProbeError):
            probe.list_metrics(user_id="usr_synthetic")
        self.transport.assert_not_called()
        for key in ("mcp_token_encrypt_key", "query_mcp_endpoint"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                _build_probe(replace(self.config, **{key: None}))

    def test_outreach_real_builder_in_every_roster_mode(self):
        for scope in (None, "legacy", "database"):
            with (
                self.subTest(scope=scope),
                patch("lingxi.apps.scheduler.alerting_assembly.build_alerting_duty"),
            ):
                self.transport.reset_mock()
                dispatcher, _ = TOOL.build_dispatcher(
                    replace(self.config, innertest_scope=scope),
                    str(self.config.postgres_dsn),
                    initiated_by="ou_admin",
                )
                self.assert_user_probe(dispatcher._sender.probe)

    def test_real_build_loop_wires_real_probe_without_starting_external_consumers(self):
        config = replace(
            self.config,
            innertest_scope="synthetic",
            innertest_binding_id="synthetic",
            innertest_binding_path="synthetic",
            innertest_socket_path=str(Path(self.temp.name) / "admin.sock"),
        )
        onboarding = SimpleNamespace(
            onboarding_runner=Mock(), onboarding_executor=Mock(), name="synthetic"
        )
        with (
            patch("lingxi.apps.scheduler.assembly._build_onboarding_duty", return_value=onboarding),
            patch(
                "lingxi.adapters.innertest_binding.load_binding",
                return_value=SimpleNamespace(
                    binding_id="synthetic", peer_uid=1234, socket_gid=1234
                ),
            ),
            patch("lingxi.adapters.innertest_socket.InnertestSocketListener.start"),
            patch(
                "lingxi.core.admin.followup_consumer.FollowupConsumer.start", autospec=True
            ) as start,
        ):
            loop = build_loop(config, audit=Mock(), roster_access_token=lambda: "synthetic")
            self.addCleanup(loop.request_stop)
            consumer = start.call_args.args[0]
            self.assert_user_probe(consumer.handlers["innertest_readiness_check"].__self__.probe)
