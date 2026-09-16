"""部署四操作和控制包以合成输入验证，不触碰真实主机或业务。"""

import errno
import hashlib
import importlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
bundle = importlib.import_module("control_bundle")
state = importlib.import_module("deploy_state")
deploy = importlib.import_module("lingxi_deploy")
# 与 deploy 在同一时刻取同一份模块对象：别的测试模块会按路径重登记这几个无包名模块，
# 运行期再 import 会拿到另一份、异常类也就对不上。
runtime_module = importlib.import_module("deploy_runtime")


def package(root, files=None):
    # 本工作树刻意没有另一张卡交付的五份控制包示例；这里只在临时包内补齐
    # 非秘密占位字节，不能把它们伪造回仓库，否则会掩盖两张卡的交付边界。
    # files 指定包内文件集合（默认当前版本的 FILES），供跨版本用例造子集包 / 超集包。
    contents = {}
    for name in bundle.FILES if files is None else files:
        source = ROOT / name
        contents[name] = (
            source.read_bytes()
            if source.is_file()
            else ("temporary control fixture: " + name + "\n").encode()
        )
    entries = [
        {
            "path": name,
            "sha256": bundle.digest(data),
            "size": len(data),
            "mode": 0o444,
            "source_commit": "a" * 40,
        }
        for name, data in contents.items()
    ]
    index = {
        "schema_revision": 1,
        "source_commit": "a" * 40,
        "runtime": bundle.RUNTIME,
        "files": entries,
    }
    contents[bundle.INDEX] = bundle.canonical(index)
    path = root / bundle.ASSET
    archive(path, contents)
    metadata = {
        "asset": bundle.ASSET,
        "sha256": bundle.digest(path.read_bytes()),
        "index_sha256": bundle.digest(contents[bundle.INDEX]),
        "schema_revision": 1,
        "source_commit": "a" * 40,
        "runtime": bundle.RUNTIME,
    }
    return path, metadata


def archive(path, contents, *, malicious=None):
    with tarfile.open(path, "w") as stream:
        for name, raw in contents.items():
            item = tarfile.TarInfo(name)
            item.size = len(raw)
            item.mode = 0o444
            stream.addfile(item, io.BytesIO(raw))
        if malicious:
            stream.addfile(malicious)


def fixture(root, *, docker="/usr/bin/docker"):
    pkg, metadata = package(root)
    release = {
        "schema": 2,
        "repository": "Moshuiwang/lingxi",
        "tag": "v2.4.0-rc.1",
        "version": "2.4.0",
        "branch": "release/2.4",
        "prerelease": True,
        "commit": "a" * 40,
        "tree": "b" * 40,
        "run_id": 1,
        "migration_heads": ["0092_synthetic"],
        "control_bundle": metadata,
        "images": {
            s: f"ghcr.io/moshuiwang/lingxi-{s}@sha256:" + "c" * 64
            for s in deploy.release_manifest.SERVICES
        },
    }
    host = {
        "schema": 1,
        "host": "synthetic-host",
        "environment": "stage",
        "project": "synthetic",
        "deploy_root": str(root),
        "config_root": str(root / "config"),
        "bundle_root": str(root / "bundles"),
        "relay_root": str(root / "relay"),
        "lock_path": str(root / "host.lock"),
        "docker": docker,
        "approval_sources": ["https://github.com/Moshuiwang/lingxi/issues/566"],
    }
    config = {"schema": 1, "values": {}, "files": {"scheduler": {}, "worker": {}}}
    request = {
        "id": "synthetic-deploy",
        "operation": "apply",
        "recovery_of": None,
        "old": release,
        "new": release,
        "acceptance_sha256": "d" * 64,
        "acceptance_source": host["approval_sources"][0],
        "current_heads": ["0091_synthetic"],
        "resources": {"required_free_bytes": 100000, "evidence_sha256": "d" * 64},
        "not_before": time.time() - 60,
        "expires_at": time.time() + 1000,
        "drain": {"gateway": 20, "scheduler": 120},
        "recovery": {
            "compatible": True,
            "historical": None,
            "evidence_sha256": "e" * 64,
            "target_manifest_sha256": state.fingerprint(release),
            "credential_source": "synthetic-external-config",
            "permissions_sha256": "f" * 64,
            "config_sha256": state.fingerprint(config),
        },
        "packages": {metadata["sha256"]: str(pkg)},
        "channel": {
            "schema_revision": 1,
            "protocol": "2025-11-25",
            "relay_sha256": "1" * 64,
            "socket_path": "/synthetic/admin.sock",
            "socket_mode": 0o660,
            "directory_mode": 0o750,
            "binding_version": 1,
            "uid_map_sha256": "2" * 64,
            "host_uid": 1234,
            "peer_uid": 1234,
            "scheduler_uid": 10001,
            "socket_gid": 1234,
            "socket_owner_uid": 10001,
            "installation_receipt_sha256": "3" * 64,
        },
        "approval_source": host["approval_sources"][0],
    }
    plan = deploy.plan_document(request, host, config)
    approval = {
        "schema": 1,
        "plan_id": plan["id"],
        "plan_sha256": state.fingerprint(plan),
        "operation": "apply",
        "source": plan["approval_source"],
        "approved_at": time.time() - 10,
        "expires_at": plan["expires_at"],
    }
    return plan, host, config, approval


class SyntheticKill(BaseException):
    """整 cgroup SIGKILL：不是 Exception，部署器来不及把 unknown 写进阶段账。"""


class FakeRuntime:
    def __init__(self, host):
        self.host, self.lock_fd = host, None
        self.done, self.calls, self.crash = set(), [], None
        self.heads, self.active_job = ["0091_synthetic"], None
        # 在该阶段的副作用发生之前被杀：阶段账已写 running、作业没起、库头未变。
        self.kill_before = None
        # 在该阶段的副作用发生之后、完成记录写入之前被杀。
        self.kill_after = None
        # 在该阶段以明确失败（DeployError）停住：阶段账记 failed，与 unknown 区分。
        self.fail = None

    def preflight(self, plan):
        # 与真实 preflight 同形：返回两侧配置指纹，execute 把它记进阶段账。
        return {
            "old_verified_sha256": None,
            "old_source": "first_takeover",
            "new_sha256": plan["config_sha256"],
        }

    def check_revocation(self, plan):
        pass

    def cleanup(self, plan):
        pass

    def snapshot(self, plan):
        return {"services": {}, "migration_heads": self.heads, "job": self.active_job}

    def complete(self, stage, plan, snapshot):
        return stage in self.done

    def perform(self, stage, plan):
        if stage == self.kill_before:
            raise SyntheticKill(stage)
        if stage == self.fail:
            raise state.DeployError("synthetic_failure")
        self.calls.append(stage)
        self.done.add(stage)
        if stage == "migrate":
            self.heads = plan["new"]["migration_heads"]
        if stage == self.kill_after:
            raise SyntheticKill(stage)
        if stage == self.crash:
            raise state.UnknownError("synthetic_interruption")

    def observe(self, plan, callback):
        self.calls.append("observe")
        self.done.add("observe")
        callback(self.snapshot(plan))
        return self.snapshot(plan)


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plan, self.host, self.config, self.approval = fixture(self.root)
        self.store = state.StateStore(self.root / "private")
        self.store.save_plan(self.plan)
        self.runtime = FakeRuntime(self.host)

    def test_same_plan_completes_and_repeated_apply_does_not_recreate(self):
        result = deploy.execute(self.plan, self.approval, self.store, self.runtime)
        self.assertEqual(result["status"], "verified")
        calls = self.runtime.calls[:]
        deploy.execute(self.plan, self.approval, self.store, self.runtime)
        self.assertEqual(self.runtime.calls, calls)
        for file in self.store.root.iterdir():
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)

    def test_unknown_migration_is_never_blindly_retried(self):
        self.runtime.active_job = {"Running": True}
        with self.assertRaisesRegex(state.UnknownError, "migration_unknown"):
            deploy.execute(self.plan, self.approval, self.store, self.runtime)
        self.assertNotIn("migrate", self.runtime.calls)

    def test_interruption_after_committed_migration_resumes_same_identity(self):
        self.runtime.crash = "migrate"
        with self.assertRaises(state.UnknownError):
            deploy.execute(self.plan, self.approval, self.store, self.runtime)
        self.runtime.crash = None
        deploy.execute(self.plan, self.approval, self.store, self.runtime)
        self.assertEqual(self.runtime.calls.count("migrate"), 1)
        self.assertEqual(self.runtime.calls.count("stop"), 1)

    def test_completed_stage_drift_is_unknown(self):
        deploy.execute(self.plan, self.approval, self.store, self.runtime)
        self.runtime.done.remove("start")
        with self.assertRaisesRegex(state.UnknownError, "verified_stage_drift"):
            deploy.execute(self.plan, self.approval, self.store, self.runtime)
        self.assertEqual(self.store.state(self.plan)["status"], "unknown")

    def test_wrong_approval_environment_window_and_recovery_rejected_before_action(self):
        for key, value in [
            ("plan_sha256", "a" * 64),
            ("operation", "recover"),
            ("expires_at", 1),
            ("source", "unapproved"),
        ]:
            with self.subTest(key=key), self.assertRaises(state.DeployError):
                deploy.execute(
                    self.plan, dict(self.approval, **{key: value}), self.store, self.runtime
                )
        self.assertEqual(self.runtime.calls, [])
        with self.assertRaises(state.DeployError):
            deploy.validate_plan(dict(self.plan, environment="production"), self.host, self.config)

    def test_host_lock_rejects_second_process_and_survives_parent_descriptor_close(self):
        with state.host_lock(Path(self.host["lock_path"])) as fd:
            child = subprocess.Popen(
                [sys.executable, "-c", "import time;time.sleep(1)"], pass_fds=(fd,)
            )
        try:
            with self.assertRaisesRegex(state.DeployError, "busy"):
                with state.host_lock(Path(self.host["lock_path"])):
                    self.fail("second execution allowed")
        finally:
            child.wait(timeout=3)
        with state.host_lock(Path(self.host["lock_path"])):
            pass

    def test_enospc_keeps_previous_state_and_no_pending_file(self):
        path = self.store.root / "sample.json"
        state.atomic_write(path, {"version": 1})
        with patch.object(state.os, "fsync", side_effect=OSError(errno.ENOSPC, "synthetic")):
            with self.assertRaises(OSError):
                state.atomic_write(path, {"version": 2})
        self.assertEqual(state.read_json(path), {"version": 1})
        self.assertFalse(list(self.store.root.glob(".pending-*")))

    def test_secret_sentinel_refused_without_echo(self):
        with self.assertRaisesRegex(state.DeployError, "^non_public_configuration_rejected$"):
            deploy.public_config(
                {
                    "schema": 1,
                    "values": {"LINGXI_POSTGRES_DSN": "SECRET_SENTINEL_566"},
                    "files": {"scheduler": {}, "worker": {}},
                }
            )

    def _plan_inputs(self) -> Path:
        """把宿主契约、公开配置和计划请求按私有材料形态（0600）写进 0700 目录。"""
        private = self.root / "inputs"
        private.mkdir(mode=0o700)
        request = {
            k: v
            for k, v in self.plan.items()
            if k
            not in {
                "schema",
                "host",
                "environment",
                "project",
                "host_sha256",
                "config_sha256",
                "deployer_sha256",
            }
        }
        for name, data in [("host", self.host), ("config", self.config), ("request", request)]:
            state.atomic_write(private / (name + ".json"), data)
        return private

    def _dry_run_plan(self, private: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                "-B",
                str(ROOT / "deploy/lingxi_deploy.py"),
                "--host-contract",
                str(private / "host.json"),
                "--public-config",
                str(private / "config.json"),
                "--state-directory",
                str(self.root / "not-created"),
                "plan",
                "--request",
                str(private / "request.json"),
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def test_dry_run_cli_creates_no_files_and_no_executor(self):
        private = self._plan_inputs()
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = self._dry_run_plan(private)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "not-created").exists())
        self.assertEqual(
            before, {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        )
        # 摘要把两侧配置指纹分开列出：本机没有在途标记，old 侧是首次接管、没有旧值。
        self.assertEqual(
            json.loads(result.stdout)["summary"]["configuration"],
            {
                "old_verified_sha256": None,
                "old_source": "first_takeover",
                "new_sha256": self.plan["config_sha256"],
            },
        )

    def test_public_config_is_read_as_public_file_and_private_inputs_stay_private(self):
        """预发实读：public-config.json 按非秘密映射装成 root 0644，部署器此前按私有材料要求
        0600，plan 一步就以 private_file_permissions 停住；宿主契约那一侧仍必须是 0600。"""
        private = self._plan_inputs()
        os.chmod(private / "config.json", 0o644)
        result = self._dry_run_plan(private)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"plan_sha256"', result.stdout)
        self.assertFalse((self.root / "not-created").exists())
        # 组 / 其他可写的公开配置仍拒：公开只放宽「本人可读」，不放宽「他人不可写」。
        os.chmod(private / "config.json", 0o664)
        result = self._dry_run_plan(private)
        self.assertEqual(result.returncode, 1)
        self.assertIn('"error": "private_file_permissions"', result.stderr)
        os.chmod(private / "config.json", 0o644)
        # 宿主契约是私有材料：0644 必须仍被拒。
        os.chmod(private / "host.json", 0o644)
        result = self._dry_run_plan(private)
        self.assertEqual(result.returncode, 1)
        self.assertIn('"error": "private_file_permissions"', result.stderr)


RELAY_CONFIGURATION = {
    "schema_revision": 1,
    "socket_path": "/synthetic/admin.sock",
    "relay_uid": 1234,
    "socket_owner_uid": 10001,
}


def install_root(root, name, mode=0o755):
    """安装根由 root 预先建立且不可由受限账号写入：权限位显式给定，不交给进程 umask。"""
    path = root / name
    path.mkdir()
    path.chmod(mode)
    return path


def release(target):
    """只读安装目录在临时目录清理前要放开写权限。"""
    for path in [target, *target.rglob("*")]:
        if path.is_dir():
            path.chmod(0o700)


class BundleTests(unittest.TestCase):
    def test_install_is_readonly_exact_and_repeatable_and_relay_is_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg, metadata = package(root)
            installed = install_root(root, "installed")
            target = bundle.install(pkg, metadata, installed)
            self.assertEqual(bundle.install(pkg, metadata, installed), target)
            relay_root = install_root(root, "relay")
            relay, receipt = bundle.install_relay(
                installed, metadata, RELAY_CONFIGURATION, relay_root
            )
            self.assertEqual(
                set(p.name for p in relay.iterdir()),
                {"innertest_relay.py", "innertest-relay.json", "installation.json"},
            )
            self.assertEqual(receipt["bundle_sha256"], metadata["sha256"])
            self.assertFalse((target / "scripts/admin/innertest-relay.json").exists())
            bundle.activate(installed, metadata)
            self.assertEqual((installed / "current").resolve(), target.resolve())
            release(target)
            release(relay)

    def test_install_conclusion_is_the_same_under_umask_0002_and_0022(self):
        """fixture 自己定安装根的权限位：运行者 shell 的 umask 是 0002 还是 0022，结论都一样。"""
        original = os.umask(0o022)
        try:
            for mask in (0o002, 0o022):
                os.umask(mask)
                with self.subTest(umask=f"{mask:04o}"):
                    self.test_install_is_readonly_exact_and_repeatable_and_relay_is_separate()
        finally:
            os.umask(original)

    def test_install_roots_take_explicit_modes_safe_passes_and_writable_is_rejected(self):
        """安装根与 relay 安装根的权限位是测试输入：显式 0755 通过，显式组 / 其他可写判红且零写入。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg, metadata = package(root)
            installed = install_root(root, "installed")
            for mode in (0o775, 0o757, 0o777):
                installed.chmod(mode)
                with self.subTest(root="installed", mode=f"{mode:04o}"):
                    with self.assertRaisesRegex(bundle.BundleError, "^安装根必须预先建立"):
                        bundle.install(pkg, metadata, installed)
                    self.assertEqual(list(installed.iterdir()), [])
            installed.chmod(0o755)
            target = bundle.install(pkg, metadata, installed)
            relay_root = install_root(root, "relay")
            for mode in (0o775, 0o757, 0o777):
                relay_root.chmod(mode)
                with self.subTest(root="relay", mode=f"{mode:04o}"):
                    with self.assertRaisesRegex(bundle.BundleError, "^relay 安装根不安全$"):
                        bundle.install_relay(installed, metadata, RELAY_CONFIGURATION, relay_root)
                    self.assertEqual(list(relay_root.iterdir()), [])
            relay_root.chmod(0o755)
            relay, receipt = bundle.install_relay(
                installed, metadata, RELAY_CONFIGURATION, relay_root
            )
            self.assertEqual(receipt["bundle_sha256"], metadata["sha256"])
            self.assertTrue((relay / "installation.json").is_file())
            release(target)
            release(relay)

    def test_tamper_missing_paths_links_permissions_and_duplicates_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg, metadata = package(root)
            index, contents = bundle.verify(pkg, metadata)
            pkg.write_bytes(pkg.read_bytes() + b"tamper")
            with self.assertRaises(bundle.BundleError):
                bundle.verify(pkg, metadata)
            cases = []
            for name in ("../outside", "/absolute", bundle.FILES[0]):
                item = tarfile.TarInfo(name)
                item.mode = 0o444
                cases.append(item)
            item = tarfile.TarInfo("link")
            item.type = tarfile.SYMTYPE
            item.linkname = "/outside"
            cases.append(item)
            item = tarfile.TarInfo("permissions")
            item.mode = 0o777
            cases.append(item)
            for item in cases:
                archive(pkg, contents, malicious=item)
                with self.subTest(item=item.name), self.assertRaises(bundle.BundleError):
                    bundle.verify(pkg, dict(metadata, sha256=bundle.digest(pkg.read_bytes())))
            del contents[bundle.FILES[0]]
            archive(pkg, contents)
            with self.assertRaises(bundle.BundleError):
                bundle.verify(pkg, dict(metadata, sha256=bundle.digest(pkg.read_bytes())))

    def test_schema_two_requires_package_and_schema_one_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan, host, config, approval = fixture(Path(tmp))
            candidate = plan["new"]
            broken = dict(candidate)
            del broken["control_bundle"]
            with self.assertRaises(deploy.release_manifest.ReleaseError):
                deploy.release_manifest.validate_manifest(
                    broken, candidate["repository"], prerelease=True
                )
            old = dict(broken, schema=1)
            deploy.release_manifest.validate_manifest(old, old["repository"], prerelease=True)
            with self.assertRaises(state.DeployError):
                deploy.validate_plan(dict(plan, new=old), host, config)


if __name__ == "__main__":
    unittest.main()


class DeployRecoveryTests(unittest.TestCase):
    setUp = DeployTests.setUp

    def test_new_id_cannot_bypass_host_marker_after_process_interruption(self):
        self.runtime.crash = "migrate"
        with self.assertRaises(state.UnknownError):
            deploy.execute(self.plan, self.approval, self.store, self.runtime)
        other = dict(self.plan, id="second-plan")
        other_approval = dict(
            self.approval, plan_id=other["id"], plan_sha256=state.fingerprint(other)
        )
        alternate = state.StateStore(self.root / "another-private-directory")
        alternate.save_plan(other)
        with self.assertRaisesRegex(state.DeployError, "unfinished_host_deployment"):
            deploy.execute(other, other_approval, alternate, self.runtime)

    def test_each_stage_interruption_reconciles_before_repeating(self):
        for stage in ("prepare", "stop", "activate", "start"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                plan, host, config, approval = fixture(root)
                store = state.StateStore(root / "state")
                store.save_plan(plan)
                runtime = FakeRuntime(host)
                runtime.crash = stage
                with self.assertRaises(state.UnknownError):
                    deploy.execute(plan, approval, store, runtime)
                runtime.crash = None
                deploy.execute(plan, approval, store, runtime)
                self.assertEqual(store.state(plan)["status"], "verified")

    def test_recovery_requires_original_target_and_separate_approval_and_no_migration(self):
        self.runtime.crash = "start"
        with self.assertRaises(state.UnknownError):
            deploy.execute(self.plan, self.approval, self.store, self.runtime)
        recovery = dict(
            self.plan,
            id="synthetic-recovery",
            operation="recover",
            recovery_of={"id": self.plan["id"], "plan_sha256": state.fingerprint(self.plan)},
            current_heads=self.runtime.heads,
        )
        self.store.save_plan(recovery)
        with self.assertRaises(state.DeployError):
            deploy.execute(recovery, self.approval, self.store, self.runtime)
        approval = dict(
            self.approval,
            plan_id=recovery["id"],
            operation="recover",
            plan_sha256=state.fingerprint(recovery),
        )
        self.runtime.crash = None
        self.runtime.done.clear()
        self.runtime.calls.clear()
        deploy.execute(recovery, approval, self.store, self.runtime)
        self.assertNotIn("migrate", self.runtime.calls)
        self.assertEqual(self.store.state(recovery)["status"], "verified")

    def test_historical_recovery_explicitly_binds_one_deployment_and_compatibility(self):
        import copy

        plan = copy.deepcopy(self.plan)
        legacy = dict(plan["old"], schema=1)
        del legacy["control_bundle"]
        plan["old"] = legacy
        plan["recovery"]["target_manifest_sha256"] = state.fingerprint(legacy)
        history = {
            "schema": 1,
            "deployment_id": plan["id"],
            "manifest_sha256": state.fingerprint(legacy),
            "release_evidence_sha256": "4" * 64,
            "control_bundle": plan["new"]["control_bundle"],
            "legacy_no_handler": True,
            "entry_disabled_receipt_sha256": "5" * 64,
            "compatible": True,
            "compatibility_sha256": "6" * 64,
        }
        plan["recovery"]["historical"] = history
        deploy.validate_plan(plan, self.host, self.config)
        historic = dict(
            legacy, schema="historical", version="2.3.1", tag="v2.3.1", prerelease=False
        )
        del historic["branch"]
        del historic["run_id"]
        explicit = copy.deepcopy(plan)
        explicit["old"] = historic
        explicit["recovery"]["target_manifest_sha256"] = state.fingerprint(historic)
        explicit["recovery"]["historical"]["manifest_sha256"] = state.fingerprint(historic)
        deploy.validate_plan(explicit, self.host, self.config)
        self.assertNotIn("branch", explicit["old"])
        self.assertNotIn("run_id", explicit["old"])
        for key, value in [
            ("deployment_id", "different"),
            ("compatible", False),
            ("manifest_sha256", "0" * 64),
        ]:
            broken = copy.deepcopy(plan)
            broken["recovery"]["historical"][key] = value
            with self.assertRaises(state.DeployError):
                deploy.validate_plan(broken, self.host, self.config)


def inventory_runtime(host, config, store, services):
    """假世界执行各阶段，preflight 换成真实的服务清单核对；容器由用例给定、可中途换（``services``）。"""
    real = runtime_module.Runtime(host, config)
    real.state_directory = store.root
    runtime = FakeRuntime(host)
    runtime.services = services

    def preflight(plan):
        with patch.object(real, "containers", return_value=runtime.services):
            return real.verify_service_inventory(plan)

    runtime.preflight = preflight
    return runtime


def labeled_services(release, config_sha):
    """部署器自己起过的容器：镜像来自 release，两枚标签是那次部署写下的配置与控制包摘要。"""
    return {
        name: {
            "image": release["images"]["worker" if name == "worker-queue" else name],
            "config_sha256": config_sha,
            "bundle_sha256": release["control_bundle"]["sha256"],
        }
        for name in runtime_module.SERVICES
    }


class ConfigurationFingerprintTests(unittest.TestCase):
    """部署链 old / new 配置指纹分离：old 侧只取部署器账里上一次 verified 计划给容器打的标签值，
    new 侧取本计划的当前 public-config 指纹；假世界跑各阶段，preflight 用真实的服务清单核对。"""

    UNAVAILABLE = "^previous_verified_configuration_unavailable$"

    def setUp(self):
        DeployTests.setUp(self)
        self.marker = state.host_marker(self.host["lock_path"], "active")
        self.x = self.plan["config_sha256"]
        self.y = state.fingerprint(dict(self.config, values={"LINGXI_WORKER_MAX_CONCURRENCY": "2"}))

    def inventory_runtime(self, services):
        return inventory_runtime(self.host, self.config, self.store, services)

    def labeled(self, release, config_sha):
        return labeled_services(release, config_sha)

    def next_plan(self, config_sha):
        """版本 B 的计划：new 侧换镜像；config_sha256 与代理写进请求的 recovery.config_sha256
        都是当前 public-config 指纹——后者正是此前被误当成旧容器标签值去核对的那个字段。"""
        import copy

        plan = copy.deepcopy(self.plan)
        plan["id"] = "synthetic-next"
        plan["config_sha256"] = config_sha
        plan["recovery"]["config_sha256"] = config_sha
        # fixture 的 old 与 new 是同一个对象：先拆开再改 new 侧。
        plan["new"] = copy.deepcopy(plan["new"])
        for service, image in plan["new"]["images"].items():
            plan["new"]["images"][service] = image.replace("c" * 64, "d" * 64)
        self.store.save_plan(plan)
        approval = dict(self.approval, plan_id=plan["id"], plan_sha256=state.fingerprint(plan))
        return plan, approval

    def verified_marker(self, plan):
        return {"id": plan["id"], "plan_sha256": state.fingerprint(plan), "status": "verified"}

    def test_changed_public_config_no_longer_blocks_the_next_version(self):
        """版本 A 部署（配置指纹 X）→ verified → 人工把 public-config 改成 Y → 版本 B 计划
        （config_sha256 = Y）：preflight 以 X 核 old 侧运行容器、以 Y 描述 new 侧，各阶段照常进入。"""
        deploy.execute(self.plan, self.approval, self.store, self.runtime)
        self.assertEqual(state.read_json(self.marker), self.verified_marker(self.plan))
        plan, approval = self.next_plan(self.y)
        self.assertNotEqual(plan["recovery"]["config_sha256"], self.x)
        runtime = self.inventory_runtime(self.labeled(self.plan["new"], self.x))
        result = deploy.execute(plan, approval, self.store, runtime)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(runtime.calls, list(deploy.STAGES))
        self.assertEqual(
            result["inventory"],
            {"old_verified_sha256": self.x, "old_source": self.plan["id"], "new_sha256": self.y},
        )
        self.assertEqual(state.read_json(self.marker), self.verified_marker(plan))

    def test_missing_or_altered_previous_plan_fails_closed_before_any_stage(self):
        """上一次 verified 的计划文件缺失或被改（指纹不再等于标记）：新码零动作，标记与容器都不动，
        阶段账记 failed 且没有两侧指纹记录。"""
        deploy.execute(self.plan, self.approval, self.store, self.runtime)
        plan, approval = self.next_plan(self.y)
        runtime = self.inventory_runtime(self.labeled(self.plan["new"], self.x))
        previous_path = self.store.path(self.plan["id"], "plan")
        for label, action in (
            ("missing", previous_path.unlink),
            ("altered", lambda: state.atomic_write(previous_path, dict(self.plan, id=plan["id"]))),
        ):
            action()
            with self.subTest(previous=label):
                with self.assertRaisesRegex(state.DeployError, self.UNAVAILABLE):
                    deploy.execute(plan, approval, self.store, runtime)
                self.assertEqual(runtime.calls, [])
                record = self.store.state(plan)
                self.assertEqual(record["status"], "failed")
                self.assertEqual(record["error"], "previous_verified_configuration_unavailable")
                self.assertNotIn("inventory", record)
                self.assertEqual(state.read_json(self.marker), self.verified_marker(self.plan))

    def test_continuation_takes_old_side_from_the_recorded_inventory(self):
        """B 在 start 阶段中断后接续：标记已指向 B，old 侧只从 B 阶段账记下的 X 取；记录缺失
        即失败关闭，不重新推算、不回落到当前配置。"""
        deploy.execute(self.plan, self.approval, self.store, self.runtime)
        plan, approval = self.next_plan(self.y)
        old_services = self.labeled(self.plan["new"], self.x)
        runtime = self.inventory_runtime(old_services)
        runtime.crash = "start"
        with self.assertRaises(state.UnknownError):
            deploy.execute(plan, approval, self.store, runtime)
        self.assertEqual(state.read_json(self.marker)["status"], "running")
        expected = {
            "old_verified_sha256": self.x,
            "old_source": self.plan["id"],
            "new_sha256": self.y,
        }
        self.assertEqual(self.store.state(plan)["inventory"], expected)
        # 首个服务已更新：gateway 带 new 侧标签，其余仍是 X 标签的旧容器。
        runtime.services = dict(
            old_services,
            gateway={
                "image": plan["new"]["images"]["gateway"],
                "config_sha256": self.y,
                "bundle_sha256": plan["new"]["control_bundle"]["sha256"],
            },
        )
        runtime.crash = None
        raw, record = self.store.load(plan)
        del record["inventory"]
        self.store.save(plan, record)
        calls = runtime.calls[:]
        with self.assertRaisesRegex(state.DeployError, self.UNAVAILABLE):
            deploy.execute(plan, approval, self.store, runtime)
        self.assertEqual(runtime.calls, calls)
        record["inventory"] = expected
        self.store.save(plan, record)
        result = deploy.execute(plan, approval, self.store, runtime)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["inventory"], expected)
        self.assertEqual(runtime.calls.count("start"), 2)

    def test_recover_checks_old_side_with_the_recovered_plan_fingerprint(self):
        """recover 的 old 侧 = 被恢复计划 B 的 config_sha256（B 给容器打的标签值 Y）；中断后配置
        又改成 Z 的 recover 仍按既有 recovery_package_changed 规则拒绝。"""
        deploy.execute(self.plan, self.approval, self.store, self.runtime)
        plan, approval = self.next_plan(self.y)
        runtime = self.inventory_runtime(self.labeled(self.plan["new"], self.x))
        runtime.crash = "start"
        with self.assertRaises(state.UnknownError):
            deploy.execute(plan, approval, self.store, runtime)
        recovery = dict(
            plan,
            id="synthetic-recovery",
            operation="recover",
            recovery_of={"id": plan["id"], "plan_sha256": state.fingerprint(plan)},
            old=plan["new"],
            new=plan["old"],
            current_heads=runtime.heads,
            # 本计划自己的 recovery.config_sha256 不是 old 侧依据：故意写成别的值。
            recovery=dict(
                plan["recovery"],
                target_manifest_sha256=state.fingerprint(plan["old"]),
                config_sha256="7" * 64,
            ),
        )
        self.store.save_plan(recovery)
        recovery_approval = dict(
            approval,
            plan_id=recovery["id"],
            operation="recover",
            plan_sha256=state.fingerprint(recovery),
        )
        runtime.services = self.labeled(plan["new"], self.y)
        runtime.crash = None
        runtime.done.clear()
        runtime.calls.clear()
        result = deploy.execute(recovery, recovery_approval, self.store, runtime)
        self.assertEqual(result["status"], "verified")
        self.assertNotIn("migrate", runtime.calls)
        self.assertEqual(
            result["inventory"],
            {"old_verified_sha256": self.y, "old_source": plan["id"], "new_sha256": self.y},
        )
        changed = dict(recovery, id="synthetic-recovery-z", config_sha256="3" * 64)
        self.store.save_plan(changed)
        changed_approval = dict(
            recovery_approval, plan_id=changed["id"], plan_sha256=state.fingerprint(changed)
        )
        calls = runtime.calls[:]
        with self.assertRaisesRegex(state.DeployError, "^recovery_package_changed$"):
            deploy.execute(changed, changed_approval, self.store, runtime)
        self.assertEqual(runtime.calls, calls)

    def test_preview_lists_old_and_new_fingerprints_and_only_reports_unavailable(self):
        """plan 摘要把两侧指纹分开列出：首次接管 / 上一次 verified 计划 / 读不到时只如实标
        unavailable 不裁决（裁决在 apply 的 preflight）。"""
        first = deploy.preview(self.plan, self.host, self.config, self.store)["configuration"]
        self.assertEqual(
            first,
            {"old_verified_sha256": None, "old_source": "first_takeover", "new_sha256": self.x},
        )
        deploy.execute(self.plan, self.approval, self.store, self.runtime)
        plan, _ = self.next_plan(self.y)
        self.assertEqual(
            deploy.preview(plan, self.host, self.config, self.store)["configuration"],
            {"old_verified_sha256": self.x, "old_source": self.plan["id"], "new_sha256": self.y},
        )
        self.store.path(self.plan["id"], "plan").unlink()
        self.assertEqual(
            deploy.preview(plan, self.host, self.config, self.store)["configuration"],
            {"old_verified_sha256": None, "old_source": "unavailable", "new_sha256": self.y},
        )


class KillingStore(state.StateStore):
    """在阶段账落盘的前一刻或后一刻整进程被杀：``decide(state)`` 返回 before / after / None。"""

    def __init__(self, root, decide):
        super().__init__(root)
        self.decide = decide

    def save(self, plan, record):
        moment = self.decide(record)
        if moment == "before":
            raise SyntheticKill("before-save")
        super().save(plan, record)
        if moment == "after":
            raise SyntheticKill("after-save")


class FailedMarkerReleaseTests(unittest.TestCase):
    """只在 prepare 失败的计划，其主机占用由下一计划在锁内按五条件收敛；缺任一条件即
    unfinished_host_deployment 零动作。收敛不改失败计划状态、不删证据、不写新的 verified。"""

    UNFINISHED = "^unfinished_host_deployment$"

    def setUp(self):
        DeployTests.setUp(self)
        self.active = state.host_marker(self.host["lock_path"], "active")
        self.verified = state.host_marker(self.host["lock_path"], "verified")

    def pointer(self, plan, status="verified"):
        return {"id": plan["id"], "plan_sha256": state.fingerprint(plan), "status": status}

    def plan_after(self, identifier, config_sha=None):
        """当前 fixture 计划之后的下一份计划：new 侧换镜像，config 可换。"""
        import copy

        plan = copy.deepcopy(self.plan)
        plan["id"] = identifier
        if config_sha is not None:
            plan["config_sha256"] = config_sha
            plan["recovery"]["config_sha256"] = config_sha
        plan["new"] = copy.deepcopy(plan["new"])
        for service, image in plan["new"]["images"].items():
            plan["new"]["images"][service] = image.replace("c" * 64, "d" * 64)
        self.store.save_plan(plan)
        approval = dict(self.approval, plan_id=plan["id"], plan_sha256=state.fingerprint(plan))
        return plan, approval

    def verified_count(self):
        return sum(
            1
            for path in self.store.root.glob("*.state.json")
            if state.read_json(path).get("status") == "verified"
        )

    def observing_runtime(self):
        """假世界执行器；preflight 时抄下在途标记与 verified 账数——收敛在 preflight 之前完成。"""
        runtime = FakeRuntime(self.host)
        observed = {}
        base = runtime.preflight

        def preflight(plan):
            observed["marker"] = state.read_json(self.active) if self.active.exists() else None
            observed["verified_count"] = self.verified_count()
            return base(plan)

        runtime.preflight = preflight
        return runtime, observed

    def deploy_verified(self):
        """fixture 计划 P 部署到 verified：在途标记与 host.verified.json 都指向 P。"""
        deploy.execute(self.plan, self.approval, self.store, self.runtime)
        self.assertEqual(state.read_json(self.active), self.pointer(self.plan))
        self.assertEqual(state.read_json(self.verified), self.pointer(self.plan))

    def fail_in_prepare(self, identifier="synthetic-a", config_sha=None):
        """计划 A 在 prepare 明确失败：阶段账 failed、只有 prepare 意图，在途标记停在 A running。"""
        plan, approval = self.plan_after(identifier, config_sha)
        runtime = FakeRuntime(self.host)
        runtime.fail = "prepare"
        with self.assertRaisesRegex(state.DeployError, "^synthetic_failure$"):
            deploy.execute(plan, approval, self.store, runtime)
        record = self.store.state(plan)
        self.assertEqual((record["status"], set(record["stages"])), ("failed", {"prepare"}))
        self.assertEqual(state.read_json(self.active), self.pointer(plan, "running"))
        return plan, approval

    def assert_refused_without_writes(self, plan, approval, failed):
        """拒绝 = 零动作：标记、失败计划的账、本计划的账（不存在）都逐字不变。"""
        marker = self.active.read_bytes()
        failed_ledger = self.store.path(failed["id"], "state").read_bytes()
        runtime = FakeRuntime(self.host)
        with self.assertRaisesRegex(state.DeployError, self.UNFINISHED):
            deploy.execute(plan, approval, self.store, runtime)
        self.assertEqual(runtime.calls, [])
        self.assertEqual(self.active.read_bytes(), marker)
        self.assertEqual(self.store.path(failed["id"], "state").read_bytes(), failed_ledger)
        self.assertTrue(self.store.path(failed["id"], "plan").exists())
        self.assertFalse(self.store.path(plan["id"], "state").exists())

    def test_prepare_only_failure_is_released_and_next_plan_deploys(self):
        """主路径：P verified → A 在 prepare 失败 → B apply 自动收敛（标记中途恢复为 P，最终指向 B），
        A 的 status 与文件不动，两处审计齐全。"""
        self.deploy_verified()
        failed, _ = self.fail_in_prepare()
        plan, approval = self.plan_after("synthetic-b")
        runtime, observed = self.observing_runtime()
        before = time.time()
        result = deploy.execute(plan, approval, self.store, runtime)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(runtime.calls, list(deploy.STAGES))
        self.assertEqual(observed["marker"], self.pointer(self.plan))
        self.assertEqual(state.read_json(self.active), self.pointer(plan))
        self.assertEqual(state.read_json(self.verified), self.pointer(plan))
        (entry,) = result["released_markers"]
        self.assertGreaterEqual(entry.pop("time"), before)
        self.assertEqual(
            entry,
            {
                "failed_plan_id": failed["id"],
                "failed_plan_sha256": state.fingerprint(failed),
                "failed_status": "failed",
                "failed_stages": ["prepare"],
                "marker_before": self.pointer(failed, "running"),
                "restored_pointer": self.pointer(self.plan),
                "restored_from": "verified_file",
            },
        )
        record = self.store.state(failed)
        self.assertEqual((record["status"], set(record["stages"])), ("failed", {"prepare"}))
        self.assertEqual(record["error"], "synthetic_failure")
        self.assertGreaterEqual(record["released_by"].pop("time"), before)
        self.assertEqual(record["released_by"], {"plan_id": plan["id"]})
        self.assertTrue(self.store.path(failed["id"], "plan").exists())

    def test_release_refused_when_stop_intent_was_recorded(self):
        """A 在 stop 失败：阶段账里有 stop 的意图记录（哪怕容器没动过）→ 不收敛。"""
        self.deploy_verified()
        failed, failed_approval = self.plan_after("synthetic-a")
        runtime = FakeRuntime(self.host)
        runtime.fail = "stop"
        with self.assertRaisesRegex(state.DeployError, "^synthetic_failure$"):
            deploy.execute(failed, failed_approval, self.store, runtime)
        record = self.store.state(failed)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["stages"]["stop"]["status"], "running")
        plan, approval = self.plan_after("synthetic-b")
        self.assert_refused_without_writes(plan, approval, failed)

    def test_release_refused_when_failed_plan_status_is_unknown(self):
        """A 在 prepare 以 unknown 停住（不是明确失败）→ 不收敛：unknown 归接续与人工，不归自动清理。"""
        self.deploy_verified()
        failed, failed_approval = self.plan_after("synthetic-a")
        runtime = FakeRuntime(self.host)
        runtime.crash = "prepare"
        with self.assertRaises(state.UnknownError):
            deploy.execute(failed, failed_approval, self.store, runtime)
        self.assertEqual(self.store.state(failed)["status"], "unknown")
        plan, approval = self.plan_after("synthetic-b")
        self.assert_refused_without_writes(plan, approval, failed)

    def test_release_refused_when_marker_does_not_belong_to_the_failed_plan(self):
        """标记的 id 或 plan_sha256 与失败计划对不上 → 不清别人的占用。"""
        self.deploy_verified()
        failed, _ = self.fail_in_prepare()
        plan, approval = self.plan_after("synthetic-b")
        for label, marker in (
            ("sha", dict(self.pointer(failed, "running"), plan_sha256="0" * 64)),
            ("id", {"id": "someone-else", "plan_sha256": "0" * 64, "status": "running"}),
        ):
            state.atomic_write(self.active, marker)
            with self.subTest(marker=label):
                self.assert_refused_without_writes(plan, approval, failed)

    def test_release_refused_when_verified_pointer_is_untrustworthy(self):
        """host.verified.json 指向的计划读不到 / 指纹不符 / 阶段账不是 verified → 不重建、不删标记。"""
        self.deploy_verified()
        failed, _ = self.fail_in_prepare()
        plan, approval = self.plan_after("synthetic-b")
        untrusted = [
            ("fingerprint", dict(self.pointer(self.plan), plan_sha256="0" * 64)),
            ("ledger_not_verified", self.pointer(failed)),
            ("plan_missing", {"id": "gone", "plan_sha256": "0" * 64, "status": "verified"}),
        ]
        for label, pointer in untrusted:
            state.atomic_write(self.verified, pointer)
            with self.subTest(pointer=label):
                self.assert_refused_without_writes(plan, approval, failed)
                self.assertEqual(state.read_json(self.verified), pointer)

    def test_crash_around_intent_action_and_completion_never_releases(self):
        """A 在 stop 的六个时点整进程被杀（意图落盘前 / 后、动作前 / 后、完成记录前 / 后）：
        阶段账都不是 failed，B 一律拒绝且 A 的证据一个不少；A 自己沿同一计划接续能完成。"""

        def intent(record, moment):
            return moment if record["stages"].get("stop", {}).get("status") == "running" else None

        def completion(record, moment):
            return moment if record["stages"].get("stop", {}).get("status") == "verified" else None

        # 每个时点：落盘决定、执行器钩子、被杀后账上 stop 记录应有的样子（证明钩子真的打在那一点）。
        points = {
            "intent_before": (lambda r: intent(r, "before"), {}, None),
            "intent_after": (lambda r: intent(r, "after"), {}, "running"),
            "action_before": (lambda r: None, {"kill_before": "stop"}, "running"),
            "action_after": (lambda r: None, {"kill_after": "stop"}, "running"),
            "completion_before": (lambda r: completion(r, "before"), {}, "running"),
            "completion_after": (lambda r: completion(r, "after"), {}, "verified"),
        }
        for label, (decide, hooks, stop_status) in points.items():
            with self.subTest(point=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.plan, self.host, self.config, self.approval = fixture(root)
                self.store = state.StateStore(root / "private")
                self.store.save_plan(self.plan)
                self.runtime = FakeRuntime(self.host)
                self.active = state.host_marker(self.host["lock_path"], "active")
                self.verified = state.host_marker(self.host["lock_path"], "verified")
                self.deploy_verified()
                failed, failed_approval = self.plan_after("synthetic-a")
                killing = KillingStore(self.store.root, decide)
                runtime = FakeRuntime(self.host)
                for name, value in hooks.items():
                    setattr(runtime, name, value)
                with self.assertRaises(SyntheticKill):
                    deploy.execute(failed, failed_approval, killing, runtime)
                record = self.store.state(failed)
                self.assertEqual(record["status"], "running")
                self.assertEqual(record["stages"].get("stop", {}).get("status"), stop_status)
                plan, approval = self.plan_after("synthetic-b")
                self.assert_refused_without_writes(plan, approval, failed)
                runtime.kill_before = runtime.kill_after = None
                deploy.execute(failed, failed_approval, self.store, runtime)
                self.assertEqual(self.store.state(failed)["status"], "verified")

    def test_release_is_idempotent_across_crash_and_reentry(self):
        """收敛写到一半被杀（写标记之前 / 之后）→ 重跑收敛恰一次、审计恰一条；已收敛后同一 B 重入或
        另一计划 C 开跑都按普通路径走；另一进程持锁时零动作。"""
        self.deploy_verified()
        failed, _ = self.fail_in_prepare()
        plan, approval = self.plan_after("synthetic-b")
        real_write = deploy.atomic_write

        def killing_write(moment):
            def write(path, value):
                restoring = path == self.active and value == self.pointer(self.plan)
                if restoring and moment == "before":
                    raise SyntheticKill("before-marker")
                real_write(path, value)
                if restoring and moment == "after":
                    raise SyntheticKill("after-marker")

            return write

        with patch.object(deploy, "atomic_write", killing_write("before")):
            with self.assertRaises(SyntheticKill):
                deploy.execute(plan, approval, self.store, FakeRuntime(self.host))
        self.assertEqual(state.read_json(self.active), self.pointer(failed, "running"))
        self.assertEqual(len(self.store.state(plan)["released_markers"]), 1)
        self.assertEqual(self.store.state(failed)["released_by"]["plan_id"], plan["id"])
        with patch.object(deploy, "atomic_write", killing_write("after")):
            with self.assertRaises(SyntheticKill):
                deploy.execute(plan, approval, self.store, FakeRuntime(self.host))
        self.assertEqual(state.read_json(self.active), self.pointer(self.plan))
        self.assertEqual(len(self.store.state(plan)["released_markers"]), 1)
        with state.host_lock(Path(self.host["lock_path"])):
            with self.assertRaisesRegex(state.DeployError, "^host_deployment_busy$"):
                deploy.execute(plan, approval, self.store, FakeRuntime(self.host))
        runtime = FakeRuntime(self.host)
        result = deploy.execute(plan, approval, self.store, runtime)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(len(result["released_markers"]), 1)
        self.assertEqual(runtime.calls, list(deploy.STAGES))
        calls = runtime.calls[:]
        deploy.execute(plan, approval, self.store, runtime)
        self.assertEqual(runtime.calls, calls)
        self.assertEqual(len(self.store.state(plan)["released_markers"]), 1)
        other, other_approval = self.plan_after("synthetic-c")
        result = deploy.execute(other, other_approval, self.store, FakeRuntime(self.host))
        self.assertEqual(result["status"], "verified")
        self.assertNotIn("released_markers", result)
        self.assertEqual(self.store.state(failed)["released_by"]["plan_id"], plan["id"])

    def test_release_never_creates_a_verified_record(self):
        """收敛只恢复既有指针：verified 账的数量不变、失败计划仍是 failed、指针逐字等于上一次的。"""
        self.deploy_verified()
        failed, _ = self.fail_in_prepare()
        plan, approval = self.plan_after("synthetic-b")
        self.assertEqual(self.verified_count(), 1)
        runtime, observed = self.observing_runtime()
        deploy.execute(plan, approval, self.store, runtime)
        self.assertEqual(observed["verified_count"], 1)
        self.assertEqual(observed["marker"], self.pointer(self.plan))
        self.assertEqual(self.store.state(failed)["status"], "failed")
        self.assertEqual(self.verified_count(), 2)

    def test_released_marker_feeds_old_side_from_the_restored_pointer(self):
        """与 #804 联动：收敛后 B 的 preflight 按恢复指针所指计划 P 的 config_sha256 核旧容器——
        容器带 P 的指纹 X 通过；带当前指纹 Y（A / B 的目标配置）不通过。"""
        self.deploy_verified()
        x = self.plan["config_sha256"]
        y = state.fingerprint(dict(self.config, values={"LINGXI_WORKER_MAX_CONCURRENCY": "2"}))
        failed, _ = self.fail_in_prepare(config_sha=y)
        plan, approval = self.plan_after("synthetic-b", y)
        runtime = inventory_runtime(
            self.host, self.config, self.store, labeled_services(self.plan["new"], y)
        )
        with self.assertRaisesRegex(state.DeployError, "^unplanned_service_artifact_or_config$"):
            deploy.execute(plan, approval, self.store, runtime)
        self.assertEqual(state.read_json(self.active), self.pointer(self.plan))
        runtime.services = labeled_services(self.plan["new"], x)
        result = deploy.execute(plan, approval, self.store, runtime)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(
            result["inventory"],
            {"old_verified_sha256": x, "old_source": self.plan["id"], "new_sha256": y},
        )
        self.assertEqual(len(result["released_markers"]), 1)

    def test_pointer_is_rebuilt_from_ledgers_when_verified_file_is_absent(self):
        """旧版本部署器部署过的主机没有 host.verified.json：从 verified 阶段账里取 verified 时刻最大的
        一份重建指针；任一份 verified 账与计划文件对不上就不重建。"""
        self.deploy_verified()
        later, later_approval = self.plan_after("synthetic-later")
        deploy.execute(later, later_approval, self.store, FakeRuntime(self.host))
        self.verified.unlink()
        failed, _ = self.fail_in_prepare()
        plan, approval = self.plan_after("synthetic-b")
        runtime, observed = self.observing_runtime()
        result = deploy.execute(plan, approval, self.store, runtime)
        self.assertEqual(observed["marker"], self.pointer(later))
        (entry,) = result["released_markers"]
        self.assertEqual(entry["restored_from"], "rebuilt_from_ledger")
        self.assertEqual(entry["restored_pointer"], self.pointer(later))
        self.assertEqual(state.read_json(self.verified), self.pointer(plan))
        # 某份 verified 账的计划文件被挪走：不重建、不删标记。
        self.verified.unlink()
        stuck, _ = self.fail_in_prepare("synthetic-a2")
        self.store.path(self.plan["id"], "plan").unlink()
        again, again_approval = self.plan_after("synthetic-c")
        self.assert_refused_without_writes(again, again_approval, stuck)

    def test_equivalent_release_only_for_never_verified_first_takeover(self):
        """本机从未 verified、A 是在无标记状态下（首次接管）通过 preflight 后在 prepare 失败：
        删除标记回到「从未由部署器部署过」，审计 restored_pointer 为 null；A 的账没有首次接管
        记录（例如旧版本部署器写的账）就不解除。"""
        failed, failed_approval = self.plan_after("synthetic-a")
        runtime = FakeRuntime(self.host)
        runtime.fail = "prepare"
        with self.assertRaisesRegex(state.DeployError, "^synthetic_failure$"):
            deploy.execute(failed, failed_approval, self.store, runtime)
        self.assertEqual(self.store.state(failed)["inventory"]["old_source"], "first_takeover")
        plan, approval = self.plan_after("synthetic-b")
        raw, record = self.store.load(failed)
        del record["inventory"]
        self.store.save(failed, record)
        self.assert_refused_without_writes(plan, approval, failed)
        state.atomic_write_raw(self.store.path(failed["id"], "state"), raw)
        runtime, observed = self.observing_runtime()
        result = deploy.execute(plan, approval, self.store, runtime)
        self.assertIsNone(observed["marker"])
        self.assertEqual(observed["verified_count"], 0)
        (entry,) = result["released_markers"]
        self.assertEqual((entry["restored_pointer"], entry["restored_from"]), (None, "none"))
        self.assertEqual(result["status"], "verified")
        self.assertEqual(state.read_json(self.active), self.pointer(plan))
        self.assertEqual(self.store.state(failed)["status"], "failed")


class MigrationResumeTests(unittest.TestCase):
    """迁移提交前中断的人工续跑入口：核对不过零写、通过后只删一条记录并留归档与审计。"""

    setUp = DeployTests.setUp

    def _state_raw(self):
        return state.read_raw(self.store.path(self.plan["id"], "state"))

    def _interrupt_before_migration(self):
        """复现预发接缝 ④：migrate 记 running 后、作业起来前被杀；下一轮 apply 按设计停住。"""
        self.runtime.kill_before = "migrate"
        with self.assertRaises(SyntheticKill):
            deploy.execute(self.plan, self.approval, self.store, self.runtime)
        self.runtime.kill_before = None
        with self.assertRaisesRegex(state.UnknownError, "migration_unknown_no_retry"):
            deploy.execute(self.plan, self.approval, self.store, self.runtime)
        record = self.store.state(self.plan)
        self.assertEqual(record["status"], "unknown")
        self.assertEqual(record["stages"]["migrate"]["status"], "running")
        self.assertNotIn("migrate", self.runtime.calls)
        self.assertEqual(self.runtime.heads, self.plan["current_heads"])

    def _resume(self, acknowledge=None, approval=None):
        acknowledge = self.store.digest(self.plan) if acknowledge is None else acknowledge
        return deploy.resume_migration(
            self.plan, approval or self.approval, self.store, self.runtime, acknowledge
        )

    def _assert_refused(self, code, **kwargs):
        before = self._state_raw()
        with self.assertRaisesRegex(state.DeployError, "^" + code + "$"):
            self._resume(**kwargs)
        self.assertEqual(self._state_raw(), before)
        self.assertFalse((self.store.root / "archive").exists())
        self.assertNotIn("migrate", self.runtime.calls)

    def test_resume_archives_clears_record_and_next_apply_migrates_exactly_once(self):
        self._interrupt_before_migration()
        before = self._state_raw()
        digest = self.store.digest(self.plan)
        result = self._resume()
        archive = Path(result["archive"])
        self.assertEqual(archive.parent, self.store.root / "archive")
        self.assertEqual(archive.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        self.assertEqual(archive.read_bytes(), before)
        self.assertRegex(
            archive.name, r"^synthetic-deploy\.state\.before-resume-\d{8}T\d{6}Z\.json$"
        )
        after = self.store.state(self.plan)
        self.assertNotIn("migrate", after["stages"])
        self.assertEqual(after["status"], "unknown")
        self.assertEqual(after["stages"]["stop"]["status"], "verified")
        self.assertEqual(len(after["resumes"]), 1)
        self.assertEqual(
            set(after["resumes"][0]),
            {"time", "stage", "archived_state_sha256", "acknowledged_sha256"},
        )
        self.assertEqual(after["resumes"][0]["stage"], "migrate")
        self.assertEqual(after["resumes"][0]["archived_state_sha256"], digest)
        self.assertEqual(after["resumes"][0]["acknowledged_sha256"], digest)
        self.assertEqual(result["state_sha256"], self.store.digest(self.plan))
        self.assertNotEqual(result["state_sha256"], digest)
        self.assertEqual(
            result["checked"],
            {
                "job": None,
                "migration_heads": ["0091_synthetic"],
                "current_heads": ["0091_synthetic"],
            },
        )
        self.assertNotIn("migrate", self.runtime.calls)
        # 记录已清，再来一次没有可清的记录：拒绝、不再归档。
        with self.assertRaisesRegex(state.DeployError, "^resume_no_migrate_record$"):
            self._resume()
        self.assertEqual(len(list((self.store.root / "archive").iterdir())), 1)
        # 下一轮 apply 走原逻辑：migrate 无记录 → 正常起迁移作业 → 接续到 verified。
        deploy.execute(self.plan, self.approval, self.store, self.runtime)
        final = self.store.state(self.plan)
        self.assertEqual(final["status"], "verified")
        self.assertEqual(self.runtime.calls.count("migrate"), 1)
        self.assertEqual(self.runtime.calls.count("stop"), 1)
        self.assertEqual(final["stages"]["migrate"]["status"], "verified")
        self.assertEqual(len(final["resumes"]), 1)
        with self.assertRaisesRegex(state.DeployError, "^resume_migrate_not_running$"):
            self._resume()

    def test_resume_refuses_while_a_migration_job_container_exists(self):
        self._interrupt_before_migration()
        for job in (
            {"Running": True, "ExitCode": 0, "id": "j", "image": "i"},
            {"Running": False, "ExitCode": 0, "id": "j", "image": "i"},
            {"Running": False, "ExitCode": 1, "id": "j", "image": "i"},
        ):
            with self.subTest(job=job):
                self.runtime.active_job = job
                self._assert_refused("resume_migration_job_present")

    def test_resume_refuses_when_database_heads_changed(self):
        self._interrupt_before_migration()
        for heads in (["0092_synthetic"], ["0090_synthetic"], [], None):
            with self.subTest(heads=heads):
                self.runtime.heads = heads
                self._assert_refused("resume_database_heads_changed")

    def test_resume_refuses_without_running_migrate_record(self):
        # 没有阶段账：status 也算不出 state_sha256，任何值都对不上。
        with self.assertRaisesRegex(state.DeployError, "^resume_acknowledge_mismatch$"):
            self._resume(acknowledge="0" * 64)
        self.assertFalse(self.store.path(self.plan["id"], "state").exists())
        # 阶段账存在但没有 migrate 记录（停服务前被杀）。
        self.runtime.kill_before = "stop"
        with self.assertRaises(SyntheticKill):
            deploy.execute(self.plan, self.approval, self.store, self.runtime)
        self.runtime.kill_before = None
        self._assert_refused("resume_no_migrate_record")
        # migrate 已 verified。
        deploy.execute(self.plan, self.approval, self.store, self.runtime)
        self.assertEqual(self.store.state(self.plan)["stages"]["migrate"]["status"], "verified")
        self.runtime.calls.clear()
        self._assert_refused("resume_migrate_not_running")

    def test_resume_refuses_stale_or_wrong_acknowledge(self):
        self.runtime.kill_before = "migrate"
        with self.assertRaises(SyntheticKill):
            deploy.execute(self.plan, self.approval, self.store, self.runtime)
        self.runtime.kill_before = None
        stale = self.store.digest(self.plan)
        # 下一轮 apply 把 unknown 写进账：旧指纹就是「对着旧状态操作」。
        with self.assertRaisesRegex(state.UnknownError, "migration_unknown_no_retry"):
            deploy.execute(self.plan, self.approval, self.store, self.runtime)
        self.assertNotEqual(stale, self.store.digest(self.plan))
        self._assert_refused("resume_acknowledge_mismatch", acknowledge=stale)
        self._assert_refused("resume_acknowledge_mismatch", acknowledge="0" * 64)
        self._assert_refused("resume_acknowledge_mismatch", acknowledge="")
        self._assert_refused(
            "resume_acknowledge_mismatch", acknowledge=self.store.digest(self.plan).upper()
        )

    def test_resume_refuses_recover_plans_and_mismatched_approval(self):
        self._interrupt_before_migration()
        recovery = dict(
            self.plan,
            id="synthetic-recovery",
            operation="recover",
            recovery_of={"id": self.plan["id"], "plan_sha256": state.fingerprint(self.plan)},
        )
        self.store.save_plan(recovery)
        recovery_approval = dict(
            self.approval,
            plan_id=recovery["id"],
            operation="recover",
            plan_sha256=state.fingerprint(recovery),
        )
        before = self._state_raw()
        for approval in (recovery_approval, self.approval):
            with self.subTest(approval=approval["operation"]):
                with self.assertRaisesRegex(state.DeployError, "^resume_migration_apply_only$"):
                    deploy.resume_migration(
                        recovery, approval, self.store, self.runtime, self.store.digest(self.plan)
                    )
        self.assertEqual(self._state_raw(), before)
        for key, value, code in [
            ("plan_sha256", "a" * 64, "approval_mismatch"),
            ("operation", "recover", "approval_mismatch"),
            ("source", "unapproved", "approval_mismatch"),
            ("expires_at", 1, "approval_expired"),
            # 形状与窗口都对、但不是部署器已记下的那份批准：本操作只复用原 apply 批准。
            ("approved_at", self.approval["approved_at"] + 1, "approval_changed"),
        ]:
            with self.subTest(key=key):
                self._assert_refused(code, approval=dict(self.approval, **{key: value}))
        self.assertFalse((self.store.root / "archive").exists())

    def test_resume_refuses_when_host_marker_points_elsewhere(self):
        self._interrupt_before_migration()
        marker = Path(self.host["lock_path"]).with_suffix(".active.json")
        original = state.read_json(marker)
        state.atomic_write(marker, dict(original, id="another-plan"))
        self._assert_refused("resume_host_marker_mismatch")
        state.atomic_write(marker, dict(original, status="verified"))
        self._assert_refused("resume_host_marker_mismatch")
        marker.unlink()
        self._assert_refused("resume_host_marker_mismatch")
        state.atomic_write(marker, original)
        self._resume()
        self.assertNotIn("migrate", self.store.state(self.plan)["stages"])


class MigrationResumeCliTests(unittest.TestCase):
    """命令行入口：status 打出 state_sha256，resume-migration 拒绝时退出码 1 且 stderr 只带结果码。"""

    _plan_inputs = DeployTests._plan_inputs

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # 假 docker：ps 一律空（无容器、无作业），run … current 回读未变的库头；其余零输出。
        # run 先核对 --env-file 拿到的是去过引号的副本（预发 env 文件的 DSN 带单引号）。
        fake = self.root / "fake-docker"
        fake.write_text(
            "#!/bin/sh\n"
            '[ "$1" = run ] || exit 0\n'
            "while [ $# -gt 0 ]; do\n"
            '  if [ "$1" = --env-file ]; then\n'
            "    shift\n"
            "    grep -qx 'LINGXI_POSTGRES_DSN=postgresql://synthetic' \"$1\" || exit 1\n"
            "  fi\n"
            "  shift\n"
            "done\n"
            "echo 0091_synthetic\n"
        )
        fake.chmod(0o755)
        self.plan, self.host, self.config, self.approval = fixture(self.root, docker=str(fake))
        config_root = Path(self.host["config_root"])
        config_root.mkdir(mode=0o700)
        (config_root / ".env.stage.migrate").write_text(
            "LINGXI_POSTGRES_DSN='postgresql://synthetic'\n", encoding="utf-8"
        )
        self.store = state.StateStore(self.root / "private")
        self.store.save_plan(self.plan)
        self.runtime = FakeRuntime(self.host)
        self.private = self._plan_inputs()
        state.atomic_write(self.private / "approval.json", self.approval)

    def _cli(self, *tail):
        return subprocess.run(
            [
                sys.executable,
                "-B",
                str(ROOT / "deploy/lingxi_deploy.py"),
                "--host-contract",
                str(self.private / "host.json"),
                "--public-config",
                str(self.private / "config.json"),
                "--state-directory",
                str(self.store.root),
                *tail,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def _status(self):
        result = self._cli("status", self.plan["id"])
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_status_reports_state_sha256_and_resume_round_trips_through_the_cli(self):
        self.assertIsNone(self._status()["state_sha256"])
        self.runtime.kill_before = "migrate"
        with self.assertRaises(SyntheticKill):
            deploy.execute(self.plan, self.approval, self.store, self.runtime)
        self.runtime.kill_before = None
        with self.assertRaisesRegex(state.UnknownError, "migration_unknown_no_retry"):
            deploy.execute(self.plan, self.approval, self.store, self.runtime)
        status = self._status()
        raw = self.store.path(self.plan["id"], "state").read_bytes()
        self.assertEqual(status["state_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(status["stages"]["migrate"]["status"], "running")
        # preflight 通过时记下的两侧配置指纹随阶段账一起读出。
        self.assertEqual(
            status["inventory"],
            {
                "old_verified_sha256": None,
                "old_source": "first_takeover",
                "new_sha256": self.plan["config_sha256"],
            },
        )
        self.assertIsNone(status["actual"]["job"])
        self.assertEqual(status["status"], "unknown")
        # 指纹不对：退出码 1，stderr 只有结果码，阶段账逐字不变。
        result = self._cli(
            "resume-migration",
            self.plan["id"],
            "--approval",
            str(self.private / "approval.json"),
            "--acknowledge",
            "0" * 64,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            json.loads(result.stderr), {"status": "failed", "error": "resume_acknowledge_mismatch"}
        )
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.store.path(self.plan["id"], "state").read_bytes(), raw)
        # 指纹对上：真实 Runtime 用假 docker 回读作业与库头，通过后归档并清记录。
        result = self._cli(
            "resume-migration",
            self.plan["id"],
            "--approval",
            str(self.private / "approval.json"),
            "--acknowledge",
            status["state_sha256"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["result"], "已归档并清除未完成的迁移记录")
        self.assertEqual(output["checked"]["migration_heads"], ["0091_synthetic"])
        self.assertEqual(Path(output["archive"]).read_bytes(), raw)
        self.assertNotIn("migrate", output["stages"])
        after = self._status()
        self.assertEqual(after["state_sha256"], output["state_sha256"])
        self.assertNotIn("migrate", after["stages"])
        self.assertEqual(len(after["resumes"]), 1)
        deploy.execute(self.plan, self.approval, self.store, self.runtime)
        self.assertEqual(self.store.state(self.plan)["status"], "verified")
        self.assertEqual(self.runtime.calls.count("migrate"), 1)


class ControlPromotionTests(unittest.TestCase):
    def test_schema_two_promotion_preserves_package_and_records_main_source(self):
        import os

        from test_release_manifest import receipt

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan, host, config, approval = fixture(root)
            doc = plan["new"]
            release = deploy.release_manifest
            with (
                patch.dict(
                    os.environ,
                    {
                        "GITHUB_REF": "refs/heads/main",
                        "GITHUB_REF_PROTECTED": "true",
                        "GITHUB_SHA": "d" * 40,
                        "GITHUB_RUN_ID": "99",
                    },
                ),
                patch.object(release, "load_release", return_value=({}, doc)),
                patch.object(release, "receipt_for", return_value=receipt(doc)),
                patch.object(release, "write_release") as write,
                patch.object(release, "command") as command,
            ):
                release.promote(doc["repository"], doc["tag"], root / "formal.json", True)
            result = write.call_args.args[0]
            self.assertEqual(result["control_bundle"], doc["control_bundle"])
            self.assertEqual(result["images"], doc["images"])
            self.assertEqual(result["commit"], "a" * 40)
            self.assertEqual(result["promotion"]["main_commit"], "d" * 40)
            self.assertEqual(result["promotion"]["run_id"], 99)
            self.assertEqual(
                result["promotion"]["acceptance_sha256"], release.fingerprint(receipt(doc))
            )
            self.assertEqual(command.call_args.args[:3], ("gh", "release", "download"))
            self.assertIn("lingxi-control.tar", command.call_args.args)

    def test_release_write_rejects_missing_or_changed_package_before_external_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan, host, config, approval = fixture(root)
            release = deploy.release_manifest
            pkg = root / bundle.ASSET
            with patch.object(release, "api") as api, patch.object(release, "command") as command:
                with self.assertRaises(release.ReleaseError):
                    release.write_release(plan["new"], root / "manifest.json")
                pkg.write_bytes(b"changed")
                with self.assertRaises(release.ReleaseError):
                    release.write_release(plan["new"], root / "manifest.json", pkg)
                api.assert_not_called()
                command.assert_not_called()
                self.assertFalse((root / "manifest.json").exists())
