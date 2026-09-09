"""部署中断接续与非秘密身份映射的真实文件权限回归。"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from test_deploy_runtime import runtime_module, state


class DeploymentReadbackTests(unittest.TestCase):
    def test_status_uses_the_selected_service_database_configuration(self):
        runtime = runtime_module.Runtime({}, {})
        plan = {"project": "synthetic", "new": {"schema": 1}}
        cases = (
            ({"gateway": {"id": "g", "running": True}}, "g", "LINGXI_GATEWAY_POSTGRES_DSN"),
            (
                {
                    "gateway": {"id": "g", "running": True},
                    "scheduler": {"id": "s", "running": True},
                },
                "s",
                "LINGXI_POSTGRES_DSN",
            ),
        )
        for containers, identifier, variable in cases:
            with self.subTest(service=identifier):
                connection = Mock()
                connection.execute.return_value = [("0092_synthetic",)]

                def docker(*args):
                    self.assertEqual(args[:4], ("exec", identifier, "python", "-c"))
                    output = io.StringIO()
                    with (
                        patch.dict(os.environ, {variable: "synthetic-dsn"}, clear=True),
                        patch(
                            "lingxi.adapters.postgres.connect", return_value=connection
                        ) as connect,
                        redirect_stdout(output),
                    ):
                        exec(args[-1], {})
                    connect.assert_called_once_with("synthetic-dsn")
                    connection.execute.assert_called_once_with(
                        "SELECT version_num FROM alembic_version"
                    )
                    connection.close.assert_called_once_with()
                    return 0, output.getvalue()

                with (
                    patch.object(runtime, "containers", return_value=containers),
                    patch.object(runtime, "job", return_value=None),
                    patch.object(runtime, "docker", side_effect=docker),
                ):
                    actual = runtime.snapshot(plan, readonly=True)
                self.assertEqual(actual["migration_heads"], ["0092_synthetic"])

    def test_public_mapping_is_readable_without_relaxing_private_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "binding.json"
            state.atomic_write(path, {"binding_id": "synthetic-binding"})
            self.assertEqual(state.read_json(path)["binding_id"], "synthetic-binding")
            path.chmod(0o644)
            with self.assertRaisesRegex(state.DeployError, "private_file_permissions"):
                state.read_json(path)
            self.assertEqual(state.read_json(path, public=True)["binding_id"], "synthetic-binding")
            path.chmod(0o664)
            with self.assertRaisesRegex(state.DeployError, "private_file_permissions"):
                state.read_json(path, public=True)
            path.chmod(0o644)
            link = Path(directory) / "linked.json"
            link.symlink_to(path.name)
            with self.assertRaises(OSError):
                state.read_json(link, public=True)

    def test_apply_resumes_after_stop_without_restarting_a_business_consumer(self):
        plan = {
            "operation": "apply",
            "environment": "stage",
            "old": {"schema": 2, "images": {"scheduler": "synthetic@sha256:old"}},
            "new": {"schema": 2},
            "channel": {"binding_version": 1},
            "project": "synthetic",
        }
        config = {
            "values": {
                "LINGXI_INNERTEST_SCOPE": "synthetic-test",
                "LINGXI_INNERTEST_BINDING_ID": "synthetic-binding",
            }
        }
        runtime = runtime_module.Runtime({"config_root": "/synthetic"}, config)
        response = {
            "ok": True,
            "schema_revision": 1,
            "roster": {
                "schema_revision": 1,
                "compatible": True,
                "version": 3,
                "mode": "database",
                "requires_dynamic_roster": True,
            },
            "binding": {
                "binding_id": "synthetic-binding",
                "version": 1,
                "enabled": True,
                "authorized": True,
            },
            "followups": {
                "contract_versions": [1],
                "compatible": True,
                "inflight": 0,
                "recoverable": 2,
                "unknown": 1,
            },
        }
        stopped = {
            name: {"id": "synthetic-" + name, "running": False} for name in runtime_module.SERVICES
        }
        calls = []

        def docker(*args, **kwargs):
            calls.append(args)
            return (0, json.dumps(response)) if args[0] == "run" else (0, "")

        with (
            patch.object(runtime, "containers", return_value=stopped),
            patch.object(runtime, "docker", side_effect=docker),
        ):
            runtime.perform("stop", plan)
        probe = calls[0]
        self.assertEqual(probe[0], "run")
        self.assertIn("--read-only", probe)
        self.assertIn("--rm", probe)
        self.assertIn("synthetic@sha256:old", probe)
        self.assertEqual(probe[-1], "lingxi.apps.innertest_status")
        self.assertNotIn("lingxi.apps.scheduler", probe)
        self.assertEqual(
            [args[-1] for args in calls[1:]],
            [stopped[n]["id"] for n in ("gateway", "worker-queue", "scheduler")],
        )
        response["followups"]["compatible"] = False
        calls.clear()
        with (
            patch.object(runtime, "containers", return_value=stopped),
            patch.object(runtime, "docker", side_effect=docker),
            self.assertRaisesRegex(state.DeployError, "business_recovery_or_binding_incompatible"),
        ):
            runtime.perform("stop", plan)
        self.assertEqual(len(calls), 1)
