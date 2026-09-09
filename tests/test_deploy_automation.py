"""部署四操作和控制包以合成输入验证，不触碰真实主机或业务。"""

import errno
import importlib
import io
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


def package(root):
    contents = {name: (ROOT / name).read_bytes() for name in bundle.FILES}
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


def fixture(root):
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
        "docker": "/usr/bin/docker",
        "approval_sources": ["https://github.com/Moshuiwang/lingxi/issues/566"],
    }
    config = {"schema": 1, "values": {}}
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


class FakeRuntime:
    def __init__(self, host):
        self.host, self.lock_fd = host, None
        self.done, self.calls, self.crash = set(), [], None
        self.heads, self.active_job = ["0091_synthetic"], None

    def preflight(self, plan):
        pass

    def check_revocation(self, plan):
        pass

    def cleanup(self, plan):
        pass

    def snapshot(self, plan):
        return {"services": {}, "migration_heads": self.heads, "job": self.active_job}

    def complete(self, stage, plan, snapshot):
        return stage in self.done

    def perform(self, stage, plan):
        self.calls.append(stage)
        self.done.add(stage)
        if stage == "migrate":
            self.heads = plan["new"]["migration_heads"]
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
                {"schema": 1, "values": {"LINGXI_POSTGRES_DSN": "SECRET_SENTINEL_566"}}
            )

    def test_dry_run_cli_creates_no_files_and_no_executor(self):
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
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = subprocess.run(
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
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "not-created").exists())
        self.assertEqual(
            before, {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        )


class BundleTests(unittest.TestCase):
    def test_install_is_readonly_exact_and_repeatable_and_relay_is_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg, metadata = package(root)
            installed = root / "installed"
            installed.mkdir()
            target = bundle.install(pkg, metadata, installed)
            self.assertEqual(bundle.install(pkg, metadata, installed), target)
            relay_root = root / "relay"
            relay_root.mkdir()
            relay, receipt = bundle.install_relay(
                installed,
                metadata,
                {
                    "schema_revision": 1,
                    "socket_path": "/synthetic/admin.sock",
                    "relay_uid": 1234,
                    "socket_owner_uid": 10001,
                },
                relay_root,
            )
            self.assertEqual(
                set(p.name for p in relay.iterdir()),
                {"innertest_relay.py", "innertest-relay.json", "installation.json"},
            )
            self.assertEqual(receipt["bundle_sha256"], metadata["sha256"])
            self.assertFalse((target / "scripts/admin/innertest-relay.json").exists())
            bundle.activate(installed, metadata)
            self.assertEqual((installed / "current").resolve(), target.resolve())
            for folder in [target, *target.rglob("*"), relay]:
                if folder.is_dir():
                    folder.chmod(0o700)

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
        for key, value in [
            ("deployment_id", "different"),
            ("compatible", False),
            ("manifest_sha256", "0" * 64),
        ]:
            broken = copy.deepcopy(plan)
            broken["recovery"]["historical"][key] = value
            with self.assertRaises(state.DeployError):
                deploy.validate_plan(broken, self.host, self.config)


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
