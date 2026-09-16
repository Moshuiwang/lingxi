"""固定执行器的命令、配置和 Linux 安装回读；无外部平台调用。"""

import hashlib
import importlib
import json
import os
import socket
import stat
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

    def public_runtime_directory(self, name):
        """公开运行文件按用例声明的权限位建立（目录 0700、文件 0644），不交给进程 umask。"""
        directory = self.root / name
        directory.mkdir(mode=0o700)
        path = directory / "system_prompt.md"
        path.write_text("合成提示词")
        path.chmod(0o644)
        self.config["values"]["LINGXI_WORKER_RUNTIME_CONFIG_DIR"] = str(directory)
        self.config["files"]["worker"]["system_prompt.md"] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        return directory, path

    def public_file_drift_round(self, name):
        directory, path = self.public_runtime_directory(name)
        (directory / ".env.private").write_text("SECRET_SENTINEL")
        self.runtime.verify_public_files()
        path.write_text("发生改变")
        with self.assertRaisesRegex(state.DeployError, "^public_file_configuration_changed$"):
            self.runtime.verify_public_files()

    def test_public_file_content_drift_is_rejected_without_reading_private_env(self):
        self.public_file_drift_round("runtime")

    def test_public_file_conclusion_is_the_same_under_umask_0002_and_0022(self):
        """fixture 自己定公开文件的权限位：运行者 shell 的 umask 是 0002 还是 0022，结论都一样。"""
        original = os.umask(0o022)
        try:
            for mask in (0o002, 0o022):
                os.umask(mask)
                with self.subTest(umask=f"{mask:04o}"):
                    self.public_file_drift_round(f"runtime-umask-{mask:03o}")
        finally:
            os.umask(original)

    def test_public_file_modes_are_explicit_inputs_safe_passes_and_writable_is_rejected(self):
        """公开运行文件及其目录的权限位是测试输入：显式安全位通过，显式组 / 其他可写判红。"""
        directory, path = self.public_runtime_directory("runtime")
        for mode in (0o644, 0o444, 0o600):
            path.chmod(mode)
            with self.subTest(file_mode=f"{mode:04o}"):
                self.runtime.verify_public_files()
        for mode in (0o664, 0o646, 0o666):
            path.chmod(mode)
            with self.subTest(file_mode=f"{mode:04o}"):
                with self.assertRaisesRegex(state.DeployError, "^public_file_permissions_or_size$"):
                    self.runtime.verify_public_files()
        path.chmod(0o644)
        for mode in (0o755, 0o750, 0o700):
            directory.chmod(mode)
            with self.subTest(directory_mode=f"{mode:04o}"):
                self.runtime.verify_public_files()
        for mode in (0o775, 0o757, 0o777):
            directory.chmod(mode)
            with self.subTest(directory_mode=f"{mode:04o}"):
                with self.assertRaisesRegex(
                    state.DeployError, "^public_file_directory_permissions$"
                ):
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

    def takeover_plan(self):
        """new 侧换成不同镜像与控制包摘要，让旧 / 新两侧可区分。"""
        import copy

        self.plan["new"] = copy.deepcopy(self.plan["new"])
        for service, image in self.plan["new"]["images"].items():
            self.plan["new"]["images"][service] = image.replace("c" * 64, "d" * 64)
        self.plan["new"]["control_bundle"] = dict(
            self.plan["new"]["control_bundle"], sha256="f" * 64
        )
        return self.plan

    @staticmethod
    def unlabeled_services(release):
        """部署器接管前由人手工启动的容器：镜像来自 release，被读取的两枚 io.lingxi.* 标签都缺。"""
        return {
            name: {
                "image": release["images"]["worker" if name == "worker-queue" else name],
                "config_sha256": None,
                "bundle_sha256": None,
            }
            for name in runtime_module.SERVICES
        }

    def test_first_takeover_accepts_only_fully_unlabeled_old_image_services(self):
        plan = self.takeover_plan()
        old_config = plan["recovery"]["config_sha256"]
        old_bundle = plan["old"]["control_bundle"]["sha256"]
        state.atomic_write(self.root / (plan["id"] + ".state.json"), {"stages": {}})
        current = self.unlabeled_services(plan["old"])
        with patch.object(self.runtime, "containers", return_value=current):
            # a. 三个无标签旧镜像容器：首次接管通过。
            self.runtime.verify_service_inventory(plan)
            # b. 无标签但镜像已是 new：不是「接管前的旧服务」，拒。
            current["gateway"]["image"] = plan["new"]["images"]["gateway"]
            with self.assertRaisesRegex(state.DeployError, "^unplanned_service"):
                self.runtime.verify_service_inventory(plan)
            current["gateway"]["image"] = plan["old"]["images"]["gateway"]
            # c. 只缺一枚标签（config 正确、bundle 缺）：不算全缺，拒；反过来也拒。
            current["gateway"]["config_sha256"] = old_config
            with self.assertRaisesRegex(state.DeployError, "^unplanned_service"):
                self.runtime.verify_service_inventory(plan)
            current["gateway"]["config_sha256"], current["gateway"]["bundle_sha256"] = (
                None,
                old_bundle,
            )
            with self.assertRaisesRegex(state.DeployError, "^unplanned_service"):
                self.runtime.verify_service_inventory(plan)
            # 空串不是缺失：拒。
            current["gateway"]["config_sha256"], current["gateway"]["bundle_sha256"] = "", ""
            with self.assertRaisesRegex(state.DeployError, "^unplanned_service"):
                self.runtime.verify_service_inventory(plan)
            # d. 标签齐但值不等：拒。
            current["gateway"]["config_sha256"], current["gateway"]["bundle_sha256"] = (
                "0" * 64,
                old_bundle,
            )
            with self.assertRaisesRegex(state.DeployError, "^unplanned_service"):
                self.runtime.verify_service_inventory(plan)
            # 标签齐且相等：原规则照常通过。
            current["gateway"]["config_sha256"] = old_config
            self.runtime.verify_service_inventory(plan)

    def test_first_takeover_continuation_keeps_exception_on_old_side_only(self):
        plan = self.takeover_plan()
        # e. 中断在「首个服务更新后」：start 已在账，剩下的旧容器仍无标签。
        state.atomic_write(
            self.root / (plan["id"] + ".state.json"), {"stages": {"start": {"status": "running"}}}
        )
        current = self.unlabeled_services(plan["old"])
        with patch.object(self.runtime, "containers", return_value=current):
            self.runtime.verify_service_inventory(plan)
            current["gateway"] = {
                "image": plan["new"]["images"]["gateway"],
                "config_sha256": plan["config_sha256"],
                "bundle_sha256": plan["new"]["control_bundle"]["sha256"],
            }
            # 新侧带正确标签的已更新服务与旧侧无标签服务并存：通过。
            self.runtime.verify_service_inventory(plan)
            # 新侧永不放行无标签：拒。
            current["gateway"]["config_sha256"], current["gateway"]["bundle_sha256"] = None, None
            with self.assertRaisesRegex(state.DeployError, "^unplanned_service"):
                self.runtime.verify_service_inventory(plan)
            # 新侧标签值不等：拒。
            current["gateway"]["config_sha256"] = plan["config_sha256"]
            current["gateway"]["bundle_sha256"] = plan["old"]["control_bundle"]["sha256"]
            with self.assertRaisesRegex(state.DeployError, "^unplanned_service"):
                self.runtime.verify_service_inventory(plan)

    def test_first_takeover_exemption_only_before_host_has_been_deployed(self):
        plan = self.takeover_plan()
        state.atomic_write(self.root / (plan["id"] + ".state.json"), {"stages": {}})
        active = Path(self.host["lock_path"]).with_suffix(".active.json")
        current = self.unlabeled_services(plan["old"])
        with patch.object(self.runtime, "containers", return_value=current):
            # a. 没有 host.active.json：本机从未由部署器部署过，首次接管通过。
            self.assertFalse(active.exists())
            self.runtime.verify_service_inventory(plan)
            # b. 标记指向另一个已 verified 的计划：本机已由部署器部署过，无标签容器按原规则拒。
            state.atomic_write(
                active, {"id": "earlier-deploy", "plan_sha256": "0" * 64, "status": "verified"}
            )
            with self.assertRaisesRegex(state.DeployError, "^unplanned_service"):
                self.runtime.verify_service_inventory(plan)
            # 形状异常（没有 id / 不是对象）视为非首次：拒。
            state.atomic_write(active, {"status": "verified"})
            with self.assertRaisesRegex(state.DeployError, "^unplanned_service"):
                self.runtime.verify_service_inventory(plan)
            state.atomic_write(active, [plan["id"]])
            with self.assertRaisesRegex(state.DeployError, "^unplanned_service"):
                self.runtime.verify_service_inventory(plan)
            # c. 标记指向同一计划（running）：同一首次计划中断后接续，仍放行。
            state.atomic_write(
                active, {"id": plan["id"], "plan_sha256": "0" * 64, "status": "running"}
            )
            self.runtime.verify_service_inventory(plan)
            # 收窄只影响无标签容器：带全标签的旧容器在标记指向别的计划时照常通过。
            state.atomic_write(
                active, {"id": "earlier-deploy", "plan_sha256": "0" * 64, "status": "verified"}
            )
            for service in current.values():
                service["config_sha256"] = plan["recovery"]["config_sha256"]
                service["bundle_sha256"] = plan["old"]["control_bundle"]["sha256"]
            self.runtime.verify_service_inventory(plan)

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

    # 预发 env 文件历来给 DSN 加单引号；docker CLI 不去引号，Compose 会去。
    DSN = "postgresql://synthetic:secret@localhost/lingxi?sslmode=require&application_name=x"
    ENV_TEXT = (
        "# 合成私有 env 文件\n"
        "\n"
        f"LINGXI_POSTGRES_DSN='{DSN}'\n"
        'export LINGXI_FEISHU_APP_SECRET="s\\"q"\n'
        "LINGXI_LOG_LEVEL=info # 行内注释\n"
        "PASSTHRU\n"
    )
    NORMALIZED = (
        f"LINGXI_POSTGRES_DSN={DSN}\n"
        'LINGXI_FEISHU_APP_SECRET=s"q\n'
        "LINGXI_LOG_LEVEL=info\n"
        "PASSTHRU\n"
    )

    def write_env(self, name, text=ENV_TEXT):
        """按预发形态写私有 env 文件（目录 0700、文件 0600），返回路径。"""
        root = Path(self.host["config_root"])
        root.mkdir(mode=0o700, exist_ok=True)
        path = root / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)
        return path

    def capturing_docker(self, seen, output):
        """假 docker：在 docker run 进行中读走 --env-file 副本的路径、内容与权限位。"""

        def docker(*args, **kwargs):
            path = Path(args[args.index("--env-file") + 1])
            seen.update(
                path=path,
                text=path.read_text(encoding="utf-8"),
                mode=stat.S_IMODE(path.stat().st_mode),
                directory_mode=stat.S_IMODE(path.parent.stat().st_mode),
            )
            if isinstance(output, Exception):
                raise output
            return 0, output

        return docker

    def assert_private_copy_consumed_and_removed(self, seen, original):
        """副本不是原文件、已按 Compose 口径去引号、0600 放在状态目录下 0700 临时目录，用完即删。"""
        self.assertNotEqual(seen["path"], original)
        self.assertEqual(seen["path"].parent.parent, self.root)
        self.assertTrue(seen["path"].parent.name.startswith(".env-"))
        self.assertEqual(seen["text"], self.NORMALIZED)
        self.assertEqual(seen["mode"], 0o600)
        self.assertEqual(seen["directory_mode"], 0o700)
        self.assertFalse(seen["path"].exists())
        self.assertFalse(seen["path"].parent.exists())
        self.assertEqual(list(self.root.glob(".env-*")), [])
        self.assertEqual(original.read_text(encoding="utf-8"), self.ENV_TEXT)
        self.assertEqual(stat.S_IMODE(original.stat().st_mode), 0o600)

    def test_migration_feeds_docker_a_private_unquoted_env_copy_and_removes_it(self):
        original = self.write_env(".env.stage.migrate")
        seen = {}
        with patch.object(
            self.runtime, "docker", side_effect=self.capturing_docker(seen, "0092_synthetic\n")
        ) as docker:
            self.assertEqual(self.runtime.migration(self.plan), ["0092_synthetic"])
        self.assert_private_copy_consumed_and_removed(seen, original)
        self.assertEqual(docker.call_args.args[0], "run")
        self.assertNotIn(str(original), docker.call_args.args)
        # docker 超时抛出：副本同样不留。
        seen.clear()
        timeout = state.UnknownError("command_timeout_reconcile_required")
        with patch.object(self.runtime, "docker", side_effect=self.capturing_docker(seen, timeout)):
            with self.assertRaisesRegex(state.UnknownError, "^command_timeout_reconcile_required$"):
                self.runtime.migration(self.plan)
        self.assert_private_copy_consumed_and_removed(seen, original)

    def test_business_probe_feeds_docker_the_same_private_env_copy(self):
        original = self.write_env(".env.stage.scheduler")
        response = json.dumps(
            {
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
                    "recoverable": 0,
                    "unknown": 0,
                },
            }
        )
        seen = {}
        stopped = {"scheduler": {"id": "synthetic", "running": False}}
        with patch.object(
            self.runtime, "docker", side_effect=self.capturing_docker(seen, response)
        ) as docker:
            result = self.runtime.business_status(self.plan, stopped)
        self.assertTrue(result["ok"])
        self.assert_private_copy_consumed_and_removed(seen, original)
        self.assertEqual(docker.call_args.args[0], "run")
        self.assertEqual(docker.call_args.args[-1], "lingxi.apps.innertest_status")
        self.assertNotIn(str(original), docker.call_args.args)

    def test_unreadable_malformed_or_empty_env_file_stops_before_docker_without_leftovers(self):
        cases = [
            (None, "env_file_unreadable"),
            ("# 只有注释\n\n", "env_file_empty"),
            ("LINGXI_POSTGRES_DSN='未闭合\n", "env_file_malformed"),
            (b"LINGXI_POSTGRES_DSN=\xff\n", "env_file_malformed"),
        ]
        for text, code in cases:
            with self.subTest(code=code):
                path = Path(self.host["config_root"]) / ".env.stage.migrate"
                path.unlink(missing_ok=True)
                if isinstance(text, bytes):
                    self.write_env(path.name, "")
                    path.write_bytes(text)
                elif text is not None:
                    self.write_env(path.name, text)
                with patch.object(self.runtime, "docker") as docker:
                    with self.assertRaisesRegex(state.UnknownError, "^" + code + "$"):
                        self.runtime.migration(self.plan)
                docker.assert_not_called()
                self.assertEqual(list(self.root.glob(".env-*")), [])
        # 没接上状态目录就没有私有落点：拒绝而不是退到系统临时目录。
        self.write_env(".env.stage.migrate")
        self.runtime.state_directory = None
        with patch.object(self.runtime, "docker") as docker:
            with self.assertRaisesRegex(state.DeployError, "^state_directory_required$"):
                self.runtime.migration(self.plan)
        docker.assert_not_called()

    def test_revision_readback_uses_fixed_native_current_and_rejects_unknown_output(self):
        self.write_env(".env.stage.migrate")
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


class EnvFileNormalizationTests(unittest.TestCase):
    """规范化纯函数：对齐 Compose 的 env 文件语义，产物是 docker CLI 能照原义消费的形式。"""

    DSN = "postgresql://synthetic:p%40ss@localhost:5432/lingxi?sslmode=require&application_name=x"

    def normalize(self, text):
        return runtime_module.normalize_env_file_text(text)

    def test_quotes_comments_blank_lines_export_and_values_with_equals_follow_compose(self):
        cases = [
            # 单引号：去引号，内部原样（`\t` 不是转义、`$` 不插值）。
            (f"A='{self.DSN}'\n", f"A={self.DSN}\n"),
            ("A='some\\tvalue $X \\'q\\''\n", "A=some\\tvalue $X 'q'\n"),
            # 双引号：去引号，`\n` / `\t` / `\"` / `\\` / `\$` 按 Compose 处理。
            (f'A="{self.DSN}"\n', f"A={self.DSN}\n"),
            ('A="{\\"k\\": \\"v\\"}\\t\\\\ \\$x"\n', 'A={"k": "v"}\t\\ $x\n'),
            # 无引号：保留原值（含 `=`、`#` 不带前导空格时不是注释），只去首尾空白与 ` #` 注释。
            (f"A={self.DSN}\n", f"A={self.DSN}\n"),
            ("A=  v#x  # 注释\r\n", "A=v#x\n"),
            # 注释行与空行丢弃，`export` 前缀去掉，键两侧空白去掉。
            ("# 注释\n\n   \n  # 缩进注释\nexport  A = 1\n", "A=1\n"),
            # 引号值后允许空白或注释；无 `=` 的行只留键名交 docker 从进程环境取。
            ("A=\"1\"  # 注释\nB='2'#c\n  PASSTHRU  \nC=\n", "A=1\nB=2\nPASSTHRU\nC=\n"),
            ("", ""),
            ("# 只有注释\n", ""),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(self.normalize(text), expected)

    def test_unterminated_quotes_trailing_junk_bad_keys_and_newlines_are_rejected(self):
        for text in [
            'A="未闭合\n',
            "A='未闭合\nB='跨行'\n",
            'A="x"junk\n',
            'A="x\\"\n',
            "A B=1\n",
            "=1\n",
            'A="x\\ny"\n',
        ]:
            with self.subTest(text=text):
                with self.assertRaisesRegex(state.UnknownError, "^env_file_malformed$"):
                    self.normalize(text)
