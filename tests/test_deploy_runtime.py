"""固定执行器的命令、配置和 Linux 安装回读；无外部平台调用。"""

import hashlib
import importlib
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_deploy_automation import fixture

runtime_module = importlib.import_module("deploy_runtime")
bundle = importlib.import_module("control_bundle")
state = importlib.import_module("deploy_state")


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plan, self.host, self.config, self.approval = fixture(self.root)
        self.config["values"] = {
            "LINGXI_INNERTEST_SCOPE": "synthetic-test",
            "LINGXI_INNERTEST_BINDING_ID": "synthetic-binding",
        }
        self.runtime = runtime_module.Runtime(self.host, self.config)
        self.runtime.state_directory = self.root

    def test_public_file_content_drift_is_rejected_without_reading_private_env(self):
        directory = self.root / "runtime"
        directory.mkdir(mode=0o700)
        path = directory / "system_prompt.md"
        path.write_text("合成提示词")
        (directory / ".env.private").write_text("SECRET_SENTINEL")
        self.config["values"]["LINGXI_WORKER_RUNTIME_CONFIG_DIR"] = str(directory)
        self.config["files"]["worker"]["system_prompt.md"] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        self.runtime.verify_public_files()
        path.write_text("发生改变")
        with self.assertRaisesRegex(state.DeployError, "^public_file_configuration_changed$"):
            self.runtime.verify_public_files()

    def test_unplanned_service_cannot_be_stopped_and_partial_update_is_bounded(self):
        import copy

        current = {
            name: {
                "image": self.plan["old"]["images"]["worker" if name == "worker-queue" else name],
                "config_sha256": self.plan["recovery"]["config_sha256"],
                "bundle_sha256": self.plan["old"]["control_bundle"]["sha256"],
            }
            for name in runtime_module.SERVICES
        }
        ledger = self.root / (self.plan["id"] + ".state.json")
        state.atomic_write(ledger, {"stages": {}})
        with patch.object(self.runtime, "containers", return_value=current):
            self.runtime.verify_service_inventory(self.plan)
            self.plan["new"] = copy.deepcopy(self.plan["new"])
            image = self.plan["new"]["images"]["worker"].replace("c" * 64, "d" * 64)
            self.plan["new"]["images"]["worker"] = image
            current["worker-queue"]["image"] = image
            with self.assertRaisesRegex(state.DeployError, "unplanned_service"):
                self.runtime.verify_service_inventory(self.plan)
            state.atomic_write(ledger, {"stages": {"start": {"status": "running"}}})
            self.runtime.verify_service_inventory(self.plan)
            current["gateway"]["image"] = current["gateway"]["image"].replace("c" * 64, "e" * 64)
            with self.assertRaisesRegex(state.DeployError, "unplanned_service"):
                self.runtime.verify_service_inventory(self.plan)

    def test_missing_business_modules_report_unavailable_without_starting_services(self):
        with patch.object(self.runtime, "docker", return_value=(1, "")) as docker:
            with self.assertRaisesRegex(
                state.DeployError, "^business_recovery_interface_unavailable$"
            ):
                self.runtime.verify_business_modules(self.plan)
        self.assertEqual(docker.call_args.args[0], "run")
        self.assertIn("none", docker.call_args.args)
        self.assertNotIn("lingxi.apps.scheduler", docker.call_args.args)

    def test_generated_compose_has_three_services_and_only_scheduler_socket_mount(self):
        result = self.runtime.channel_compose(self.plan)
        self.assertEqual(set(result["services"]), set(runtime_module.SERVICES))
        self.assertNotIn("volumes", result["services"]["gateway"])
        scheduler = result["services"]["scheduler"]
        self.assertEqual(len(scheduler["volumes"]), 2)
        self.assertTrue(all(not x["bind"]["create_host_path"] for x in scheduler["volumes"]))
        self.assertTrue(scheduler["volumes"][1]["read_only"])
        self.assertEqual(
            scheduler["environment"]["LINGXI_INNERTEST_SCOPE"],
            result["services"]["gateway"]["environment"]["LINGXI_INNERTEST_SCOPE"],
        )

    def test_compose_start_uses_fixed_bundle_mvp_and_digests(self):
        with patch.object(self.runtime, "command", return_value=(0, "")) as command:
            self.runtime.compose(
                self.plan, "up", "-d", "--no-build", "--pull", "never", *runtime_module.SERVICES
            )
        argv = command.call_args.args[0]
        self.assertIn("--profile", argv)
        self.assertIn("mvp", argv)
        self.assertIn("--no-build", argv)
        self.assertEqual(argv[-3:], ["scheduler", "gateway", "worker-queue"])
        self.assertTrue(any(self.plan["new"]["control_bundle"]["sha256"] in x for x in argv))

    def test_revision_readback_uses_fixed_native_current_and_rejects_unknown_output(self):
        with patch.object(
            self.runtime, "docker", return_value=(0, "0092_innertest_membership (head)\n")
        ) as docker:
            self.assertEqual(self.runtime.migration(self.plan), ["0092_innertest_membership"])
        self.assertEqual(
            docker.call_args.args[-2:], (self.plan["new"]["images"]["migrate"], "current")
        )
        self.assertNotIn("--entrypoint", docker.call_args.args)
        for value in [(1, "SECRET_SENTINEL"), (0, ""), (0, "head_a\nhead_b")]:
            with patch.object(self.runtime, "docker", return_value=value):
                with self.assertRaisesRegex(state.UnknownError, "^migration_revision_unknown$"):
                    self.runtime.migration(self.plan)

    def test_migration_preserves_named_job_and_uses_image_entrypoint(self):
        with (
            patch.object(self.runtime, "compose", return_value=(0, "job-id")) as compose,
            patch.object(self.runtime, "docker", return_value=(0, "0")) as docker,
        ):
            self.runtime.perform("migrate", self.plan)
        arguments = compose.call_args.args
        self.assertEqual(
            arguments[-3:], ("migrate", "upgrade", self.plan["new"]["migration_heads"][0])
        )
        self.assertNotIn("--rm", arguments)
        self.assertEqual(docker.call_args.args, ("wait", "lingxi-migration-" + self.plan["id"]))

    def test_active_migration_is_not_queried_or_retried_and_failed_job_is_not_verified(self):
        job = {"Running": True, "ExitCode": 0}
        with (
            patch.object(self.runtime, "containers", return_value={}),
            patch.object(self.runtime, "job", return_value=job),
            patch.object(self.runtime, "migration") as migration,
        ):
            snapshot = self.runtime.snapshot(self.plan)
        self.assertIsNone(snapshot["migration_heads"])
        migration.assert_not_called()
        snapshot = {
            "migration_heads": self.plan["new"]["migration_heads"],
            "job": {"Running": False, "ExitCode": 1},
        }
        self.assertFalse(self.runtime.complete("migrate", self.plan, snapshot))

    def test_status_only_executes_reads_and_never_creates_migration_container(self):
        with (
            patch.object(
                self.runtime, "containers", return_value={"scheduler": {"id": "a", "running": True}}
            ),
            patch.object(self.runtime, "job", return_value=None),
            patch.object(self.runtime, "docker", return_value=(0, '["0092_synthetic"]')) as docker,
        ):
            result = self.runtime.snapshot(self.plan, readonly=True)
        self.assertEqual(result["migration_heads"], ["0092_synthetic"])
        self.assertEqual(docker.call_args.args[0], "exec")
        self.assertNotIn("run", docker.call_args.args)

    def test_no_running_services_status_returns_unknown_head_without_writing(self):
        with (
            patch.object(self.runtime, "containers", return_value={}),
            patch.object(self.runtime, "job", return_value=None),
            patch.object(self.runtime, "docker") as docker,
        ):
            self.assertIsNone(self.runtime.snapshot(self.plan, readonly=True)["migration_heads"])
        docker.assert_not_called()

    def test_observation_requires_full_900_seconds_and_starts_again_on_reconnect(self):
        calls = []
        with (
            patch.object(self.runtime, "snapshot", return_value={"services": {}}),
            patch.object(self.runtime, "complete", return_value=True),
            patch.object(runtime_module.time, "sleep"),
            patch.object(
                runtime_module.time, "monotonic", side_effect=[0, 899, 900, 5000, 5899, 5900]
            ),
        ):
            self.runtime.observe(self.plan, calls.append)
            self.runtime.observe(self.plan, calls.append)
        self.assertEqual(len(calls), 4)

    def test_external_command_error_never_echoes_secret_output(self):
        import subprocess

        with patch.object(
            runtime_module.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 1, "SECRET_SENTINEL", "SECRET_SENTINEL"),
        ):
            with self.assertRaisesRegex(state.DeployError, "^fixed_command_failed$"):
                self.runtime.command(["/synthetic/docker"])

    @unittest.skipUnless(
        sys.platform == "linux" and os.geteuid() == 0, "实际 root 保护安装在隔离 Linux 验证"
    )
    def test_real_readonly_relay_socket_acl_and_uid_map_readback(self):
        package_path = self.root / bundle.ASSET
        metadata = self.plan["new"]["control_bundle"]
        for key in ("bundle_root", "relay_root", "config_root"):
            Path(self.host[key]).mkdir(mode=0o700)
        bundle.install(package_path, metadata, Path(self.host["bundle_root"]))
        socket_dir = self.root / "socket"
        socket_dir.mkdir(mode=0o750)
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(socket_dir / "admin.sock"))
        self.addCleanup(sock.close)
        os.chmod(socket_dir / "admin.sock", 0o660)
        os.chown(socket_dir, -1, 1234)
        os.chown(socket_dir / "admin.sock", -1, 1234)
        channel = self.plan["channel"]
        channel.update(
            socket_path=str(socket_dir / "admin.sock"),
            socket_owner_uid=0,
            socket_gid=1234,
            relay_sha256=hashlib.sha256(
                (
                    Path(__file__).resolve().parents[1] / "scripts/admin/innertest_relay.py"
                ).read_bytes()
            ).hexdigest(),
            uid_map_sha256=hashlib.sha256(Path("/proc/self/uid_map").read_bytes()).hexdigest(),
        )
        target, receipt = bundle.install_relay(
            Path(self.host["bundle_root"]),
            metadata,
            self.runtime.relay_configuration(self.plan),
            Path(self.host["relay_root"]),
        )
        (Path(self.host["relay_root"]) / "current").symlink_to(target.name)
        binding_dir = Path(self.host["config_root"]) / "innertest"
        binding_dir.mkdir(mode=0o700)
        binding = {
            "schema_revision": 1,
            "binding_id": "synthetic-binding",
            "host_uid": channel["host_uid"],
            "peer_uid": channel["peer_uid"],
            "uid_map_sha256": channel["uid_map_sha256"],
            "socket_gid": channel["socket_gid"],
        }
        state.atomic_write(binding_dir / "binding.json", binding)
        # scheduler 以非 root 运行，非秘密主体映射必须可读而不可改。
        (binding_dir / "binding.json").chmod(0o644)
        installation = {
            "schema": 1,
            "environment": self.plan["environment"],
            "project": self.plan["project"],
            "bundle_sha256": metadata["sha256"],
            "binding_version": channel["binding_version"],
            "checks": {
                "sudo_policy": True,
                "sshd_policy": True,
                "credential_owner": True,
                "authorized_peer": True,
                "wrong_uid_rejected": True,
                "container_peer_rejected": True,
            },
        }
        channel["installation_receipt_sha256"] = state.fingerprint(installation)
        state.atomic_write(
            Path(self.host["config_root"]) / "deployment-installation.json", installation
        )
        with (
            patch.object(
                self.runtime, "containers", return_value={"scheduler": {"id": "synthetic"}}
            ),
            patch.object(self.runtime, "docker", return_value=(0, channel["uid_map_sha256"])),
        ):
            self.assertTrue(self.runtime.channel_status(self.plan))
            os.chmod(socket_dir / "admin.sock", 0o666)
            with self.assertRaisesRegex(state.DeployError, "acl_drift"):
                self.runtime.channel_status(self.plan)
            os.chmod(socket_dir / "admin.sock", 0o660)
            with patch.object(self.runtime, "docker", return_value=(0, "wrong-map")):
                with self.assertRaisesRegex(state.DeployError, "uid_mapping_drift"):
                    self.runtime.channel_status(self.plan)
        for root in (Path(self.host["bundle_root"]), Path(self.host["relay_root"])):
            for directory in [root, *root.rglob("*")]:
                if directory.is_dir() and not directory.is_symlink():
                    directory.chmod(0o700)


class BusinessRecoveryTests(unittest.TestCase):
    def test_fixed_probe_validates_binding_and_preserves_unknown_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan, host, config, approval = fixture(Path(tmp))
            config["values"]["LINGXI_INNERTEST_BINDING_ID"] = "synthetic-binding"
            runtime = runtime_module.Runtime(host, config)
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
                    "inflight": 2,
                    "recoverable": 3,
                    "unknown": 4,
                },
            }
            containers = {"scheduler": {"id": "synthetic", "running": True}}
            with patch.object(runtime, "docker", return_value=(0, json.dumps(response))) as command:
                actual = runtime.business_status(plan, containers)
            self.assertEqual(actual["followups"]["unknown"], 4)
            self.assertEqual(
                command.call_args.args,
                ("exec", "synthetic", "python", "-m", "lingxi.apps.innertest_status"),
            )
            for key, value in [("version", 2), ("enabled", False), ("authorized", False)]:
                broken = dict(response, binding=dict(response["binding"], **{key: value}))
                with patch.object(runtime, "docker", return_value=(0, json.dumps(broken))):
                    with self.assertRaises(state.DeployError):
                        runtime.business_status(plan, containers)
            legacy_target = dict(plan, new=dict(plan["new"], schema=1))
            with patch.object(runtime, "docker", return_value=(0, json.dumps(response))):
                with self.assertRaisesRegex(state.DeployError, "cannot_consume"):
                    runtime.business_status(legacy_target, containers)
