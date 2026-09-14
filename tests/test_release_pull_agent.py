"""Release 拉取代理的本机隔离用例与变异防线。"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib.util
import io
import json
import os
import stat
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "deploy" / "release_pull_agent.py"
spec = importlib.util.spec_from_file_location("release_pull_agent_under_test", MODULE_PATH)
AGENT = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(AGENT)
print(f"loaded release pull agent: {MODULE_PATH}")


def _write(path: Path, value: str | bytes, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, str):
        path.write_text(value, encoding="utf-8")
    else:
        path.write_bytes(value)
    path.chmod(mode)
    return path


def _json(path: Path, value: object, mode: int = 0o600) -> Path:
    return _write(path, json.dumps(value, ensure_ascii=False, indent=2), mode)


def _exec(path: Path, source: str) -> Path:
    _write(path, textwrap.dedent(source).lstrip(), 0o755)
    return path


class PullHarness:
    """临时主机：假命令只记录固定参数，不接触真实 GitHub、Docker 或飞书。"""

    def __init__(self, environment: str = "stage"):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.state.chmod(0o700)
        self.config_root = self.root / "config"
        self.bundle_root = self.root / "bundles"
        self.relay_root = self.root / "relay"
        self.deploy_root = self.root / "deploy-root"
        for path in (self.config_root, self.bundle_root, self.relay_root, self.deploy_root):
            path.mkdir()
            path.chmod(0o755)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls.jsonl"
        self.releases = self.root / "releases.json"
        self.manifests = self.root / "manifests"
        self.manifests.mkdir()
        self.asset = self.root / "lingxi-control.tar"
        self.index = self.root / "control-index.json"
        self.asset.write_bytes(b"synthetic control package")
        self.index.write_bytes(b'{"synthetic":true}\n')
        self.asset.chmod(0o644)
        self.index.chmod(0o644)
        self.sentinel = "SENTINEL_DO_NOT_LOG_770"
        self.gh = _exec(
            self.bin / "gh-fake",
            r"""
            #!/usr/bin/env python3
            import json, os, shutil, sys
            log = os.environ.get("CALL_LOG")
            with open(log, "a", encoding="utf-8") as stream:
                stream.write(json.dumps({"kind":"gh", "argv":sys.argv[1:], "sentinel":os.environ.get("SECRET_SENTINEL", "")}) + "\n")
            if sys.argv[1:3] == ["release", "list"]:
                print(open(os.environ["RELEASES_FILE"], encoding="utf-8").read())
                raise SystemExit(0)
            if sys.argv[1:3] == ["release", "download"]:
                directory = sys.argv[sys.argv.index("--dir") + 1]
                shutil.copyfile(os.environ["ASSET_FILE"], os.path.join(directory, "lingxi-control.tar"))
                shutil.copyfile(os.environ["INDEX_FILE"], os.path.join(directory, "control-index.json"))
                raise SystemExit(0)
            raise SystemExit(9)
            """,
        )
        self.manifest_script = _exec(
            self.bin / "release-manifest-old.py",
            r"""
            #!/usr/bin/env python3
            import json, os, sys
            with open(os.environ["CALL_LOG"], "a", encoding="utf-8") as stream:
                stream.write(json.dumps({"kind":"manifest", "script":os.path.abspath(__file__), "argv":sys.argv[1:]}) + "\n")
            tag = sys.argv[sys.argv.index("--tag") + 1]
            source = os.path.join(os.environ["MANIFESTS_DIR"], tag + ".json")
            destination = sys.argv[sys.argv.index("--manifest-output") + 1]
            with open(source, encoding="utf-8") as stream:
                data = json.load(stream)
            with open(destination, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
            """,
        )
        self.docker = _exec(
            self.bin / "docker-fake",
            r"""
            #!/usr/bin/env python3
            import json, os, sys
            with open(os.environ["CALL_LOG"], "a", encoding="utf-8") as stream:
                stream.write(json.dumps({"kind":"docker", "argv":sys.argv[1:]}) + "\n")
            args = sys.argv[1:]
            mode = os.environ.get("DOCKER_MODE", "mismatch")
            containers = json.loads(os.environ.get("DOCKER_CONTAINERS_JSON", "{}"))
            images = json.loads(os.environ.get("DOCKER_IMAGES_JSON", "{}"))
            if args[:2] == ["ps", "-aq"]:
                if mode == "daemon":
                    print("Cannot connect to the Docker daemon", file=sys.stderr)
                    raise SystemExit(2)
                if mode in {"match", "records", "stopped", "missing-image"}:
                    print("\n".join(containers))
                raise SystemExit(0)
            if args[:2] == ["image", "inspect"]:
                image = images.get(args[-1])
                if mode == "missing-image" or image is None:
                    print("Error: No such image", file=sys.stderr)
                    raise SystemExit(1)
                print(json.dumps(image["RepoDigests"]))
                raise SystemExit(0)
            if args and args[0] == "inspect":
                container = containers.get(args[1])
                if container is None:
                    print("Error: No such object", file=sys.stderr)
                    raise SystemExit(1)
                print(json.dumps(container["labels"]))
                print(json.dumps(container["config_image"]))
                print(json.dumps(container["state"]))
                print(json.dumps(container["image_id"]))
                raise SystemExit(0)
            raise SystemExit(9)
            """,
        )
        self.old_deployer = _exec(
            self.bin / "deployer-old.py",
            r"""
            #!/usr/bin/env python3
            import json, os
            with open(os.environ["CALL_LOG"], "a", encoding="utf-8") as stream:
                stream.write(json.dumps({"kind":"old-deployer", "argv":__import__("sys").argv[1:]}) + "\n")
            raise SystemExit(88)
            """,
        )
        self.control_tool = _exec(
            self.bin / "control-bundle-fake.py",
            r"""
            #!/usr/bin/env python3
            import hashlib, json, os, shutil
            from pathlib import Path
            def verify(package, expected):
                with open(os.environ["CALL_LOG"], "a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"kind":"bundle-verify", "package":str(package)}) + "\n")
                return {}, {"control-index.json": Path(os.environ["INDEX_FILE"]).read_bytes()}
            def install(package, expected, root):
                target = Path(root) / expected["sha256"]
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(os.environ["INSTALLED_SOURCE"], target)
                with open(os.environ["CALL_LOG"], "a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"kind":"bundle-install", "target":str(target)}) + "\n")
                return target
            """,
        )
        self.installed_source = self.root / "installed-source"
        (self.installed_source / "deploy").mkdir(parents=True)
        (self.installed_source / "scripts" / "ci").mkdir(parents=True)
        self.installed_deployer = _exec(
            self.installed_source / "deploy" / "lingxi_deploy.py",
            r"""
            #!/usr/bin/env python3
            import hashlib, json, os, sys, time
            def canonical(value):
                return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
            def log(kind, extra=None):
                value = {"kind":kind, "script":os.path.abspath(__file__), "argv":sys.argv[1:]}
                if extra:
                    value.update(extra)
                with open(os.environ["CALL_LOG"], "a", encoding="utf-8") as stream:
                    stream.write(json.dumps(value) + "\n")
            args = sys.argv[1:]
            log("deployer")
            operation = args[args.index("--state-directory") + 2]
            state_dir = args[args.index("--state-directory") + 1]
            if operation == "plan":
                request = json.load(open(args[args.index("--request") + 1], encoding="utf-8"))
                plan = dict(request)
                plan.update({"schema":1, "host":"synthetic-host", "environment":"stage", "project":"synthetic-project"})
                path = os.path.join(state_dir, plan["id"] + ".plan.json")
                with open(path, "w", encoding="utf-8") as stream:
                    json.dump(plan, stream, ensure_ascii=False, sort_keys=True, indent=2)
                os.chmod(path, 0o600)
                print(json.dumps({"plan":plan, "plan_sha256":hashlib.sha256(canonical(plan)).hexdigest()}))
            elif operation == "apply":
                if os.environ.get("DEPLOY_MODE") == "timeout":
                    time.sleep(4)
                approval_path = args[args.index("--approval") + 1]
                approval = json.load(open(approval_path, encoding="utf-8"))
                approval_sha = hashlib.sha256(canonical(approval)).hexdigest()
                marker = os.path.join(state_dir, ".fake-approval-sha")
                if os.path.exists(marker):
                    if open(marker, encoding="utf-8").read() != approval_sha:
                        print("approval_changed", file=sys.stderr)
                        raise SystemExit(1)
                else:
                    with open(marker, "w", encoding="utf-8") as stream:
                        stream.write(approval_sha)
                print("{}")
            elif operation == "status":
                print(json.dumps({"status":os.environ.get("DEPLOY_STATUS", "verified")}))
            else:
                raise SystemExit(7)
            """,
        )
        self.installed_manifest = _exec(
            self.installed_source / "scripts" / "ci" / "release_manifest.py",
            r"""
            #!/usr/bin/env python3
            import json, os, sys
            with open(os.environ["CALL_LOG"], "a", encoding="utf-8") as stream:
                stream.write(json.dumps({"kind":"installed-manifest", "script":os.path.abspath(__file__), "argv":sys.argv[1:]}) + "\n")
            tag = sys.argv[sys.argv.index("--tag") + 1]
            with open(os.path.join(os.environ["MANIFESTS_DIR"], tag + ".json"), encoding="utf-8") as source:
                data = json.load(source)
            with open(sys.argv[sys.argv.index("--manifest-output") + 1], "w", encoding="utf-8") as destination:
                json.dump(data, destination, ensure_ascii=False, sort_keys=True, indent=2)
                destination.write("\n")
            """,
        )
        self.hook_fail = _exec(
            self.bin / "hook-fail.py",
            r"""
            #!/usr/bin/env python3
            import json, os, sys
            with open(os.environ["CALL_LOG"], "a", encoding="utf-8") as stream:
                stream.write(json.dumps({"kind":"hook", "argv":sys.argv[1:]}) + "\n")
            raise SystemExit(3)
            """,
        )
        self.alert_env = _write(
            self.root / "host-monitor.env",
            "LINGXI_FEISHU_APP_ID=app-id\n"
            f"LINGXI_FEISHU_APP_SECRET={self.sentinel}\n"
            "LINGXI_ADMIN_GROUP_CHAT_ID=oc_synthetic\n",
            0o600,
        )
        self.public_config = _json(
            self.config_root / "public-config.json",
            {"schema": 1, "values": {}, "files": {"scheduler": {}, "worker": {}}},
        )
        self.host = {
            "schema": 1,
            "host": "synthetic-host",
            "environment": environment,
            "project": "synthetic-project",
            "deploy_root": str(self.deploy_root),
            "config_root": str(self.config_root),
            "bundle_root": str(self.bundle_root),
            "relay_root": str(self.relay_root),
            "lock_path": str(self.root / "host.lock"),
            "docker": str(self.docker),
            "approval_sources": ["https://github.com/Moshuiwang/lingxi/issues/770"],
        }
        self.host_path = _json(self.root / "host-contract.json", self.host)
        self.config = {
            "schema": 1,
            "repository": "Moshuiwang/lingxi",
            "gh_command": str(self.gh),
            "release_manifest": str(self.manifest_script),
            "control_bundle_tool": str(self.control_tool),
            "deployer": str(self.old_deployer),
            "public_config": str(self.public_config),
            "alert_env_file": str(self.alert_env),
            "pre_apply_hooks": [],
            "poll_timeout_seconds": 10,
            "deploy_timeout_seconds": 2,
            "notify_on_success": True,
        }
        self.config_path = _json(self.root / "agent-config.json", self.config)
        self._env = {
            "CALL_LOG": str(self.log),
            "RELEASES_FILE": str(self.releases),
            "MANIFESTS_DIR": str(self.manifests),
            "ASSET_FILE": str(self.asset),
            "INDEX_FILE": str(self.index),
            "INSTALLED_SOURCE": str(self.installed_source),
            "DOCKER_MODE": "mismatch",
            "DOCKER_CONTAINERS_JSON": "{}",
            "DOCKER_IMAGES_JSON": "{}",
            "DEPLOY_STATUS": "verified",
            "DEPLOY_MODE": "normal",
        }
        self.old_tag = "v2.4.3"
        self.target_tag = "v2.5.0-rc.10" if environment == "stage" else "v2.5.0"
        self._make_manifest(self.old_tag, formal=environment == "production", seed="1")
        self._make_manifest(self.target_tag, formal=environment == "production", seed="2")
        self.set_releases([self._release(self.target_tag, environment == "stage")])
        self._write_env()
        self.set_running_containers(self.target_tag)

    def _write_env(self):
        self._old_env = os.environ.copy()
        os.environ.update(self._env)

    def close(self):
        os.environ.clear()
        os.environ.update(self._old_env)
        self.tmp.cleanup()

    def set_running_containers(self, tag: str, running: bool = True):
        manifest = json.loads((self.manifests / (tag + ".json")).read_text())
        containers = {}
        images = {}
        for service, manifest_service in (
            ("scheduler", "scheduler"),
            ("gateway", "gateway"),
            ("worker-queue", "worker"),
        ):
            identifier = "container-" + service
            image_id = "image-" + service
            containers[identifier] = {
                "labels": {
                    "com.docker.compose.project": self.host["project"],
                    "com.docker.compose.service": service,
                },
                "config_image": manifest["images"][manifest_service],
                "state": {"Running": running},
                "image_id": image_id,
            }
            images[image_id] = {
                "RepoDigests": [manifest["images"][manifest_service]],
            }
        os.environ["DOCKER_CONTAINERS_JSON"] = json.dumps(containers)
        os.environ["DOCKER_IMAGES_JSON"] = json.dumps(images)

    def _make_manifest(self, tag: str, formal: bool, seed: str) -> dict:
        digest = (seed * 64)[:64]
        metadata = {
            "asset": "lingxi-control.tar",
            "sha256": hashlib.sha256(self.asset.read_bytes()).hexdigest(),
            "index_sha256": hashlib.sha256(self.index.read_bytes()).hexdigest(),
            "schema_revision": 1,
            "source_commit": (seed * 40)[:40],
            "runtime": {"python_minimum": "3.11", "platform": "linux"},
        }
        version = tag[1:].split("-", 1)[0]
        document = {
            "schema": 2,
            "repository": "Moshuiwang/lingxi",
            "tag": tag,
            "version": version,
            "branch": "release/" + ".".join(version.split(".")[:2]),
            "prerelease": not formal,
            "commit": (seed * 40)[:40],
            "tree": ((str(int(seed) + 3)) * 40)[:40],
            "run_id": int(seed),
            "migration_heads": ["0099_synthetic"],
            "control_bundle": metadata,
            "images": {
                service: f"ghcr.io/moshuiwang/lingxi-{service}@sha256:{digest}"
                for service in AGENT.SERVICES
            },
        }
        if formal:
            document["candidate_tag"] = tag + "-candidate"
            document["candidate_manifest_sha256"] = "a" * 64
            document["acceptance"] = {}
            document["promotion"] = {"run_id": int(seed) + 10, "acceptance_sha256": "b" * 64}
        _json(self.manifests / (tag + ".json"), document, 0o644)
        return document

    def _release(self, tag: str, prerelease: bool) -> dict:
        return {
            "tagName": tag,
            "isPrerelease": prerelease,
            "isDraft": False,
            "publishedAt": "2026-09-14T00:00:00Z",
            "url": f"https://github.com/Moshuiwang/lingxi/releases/tag/{tag}?token=should-not-log",
        }

    def set_releases(self, releases: list[dict]):
        _json(self.releases, releases, 0o644)

    def set_env(self, **values):
        os.environ.update(values)

    def save_state(self, value: dict):
        _json(self.state / "pull-agent.json", value)

    def base_state(self) -> dict:
        return AGENT._base_state(self.host)

    def previous_plan(self, target_tag: str | None = None) -> dict:
        target_tag = target_tag or self.old_tag
        old = json.loads((self.manifests / (target_tag + ".json")).read_text())
        plan = {
            "schema": 1,
            "id": "old-plan",
            "operation": "apply",
            "approval_source": self.host["approval_sources"][0],
            "new": old,
            "old": old,
            "resources": {"required_free_bytes": 1, "evidence_sha256": "a" * 64},
            "channel": {"synthetic": True},
            "recovery": {"synthetic": True},
            "packages": {},
            "not_before": time_value() - 10,
            "expires_at": time_value() + 1000,
        }
        _json(self.state / "old-plan.plan.json", plan)
        return plan

    def state_for_old(self):
        plan = self.previous_plan(self.old_tag)
        value = self.base_state()
        value.update(
            {
                "target_tag": self.old_tag,
                "deployer_state": "verified",
                "plan_id": plan["id"],
            }
        )
        self.save_state(value)

    def state_for_continuation(self):
        plan = self.previous_plan(self.target_tag)
        plan["id"] = "continuing-plan"
        plan["new"] = json.loads((self.manifests / (self.target_tag + ".json")).read_text())
        plan["approval_source"] = self.host["approval_sources"][0]
        _json(self.state / "continuing-plan.plan.json", plan)
        value = self.base_state()
        value.update(
            {
                "target_tag": self.target_tag,
                "deployer_state": "running",
                "plan_id": "continuing-plan",
            }
        )
        self.save_state(value)


def time_value() -> float:
    return __import__("time").time()


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.harness = PullHarness()
        self.addCleanup(self.harness.close)

    def run_agent(self, *, sender=None):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with (
                patch.object(AGENT, "send_alert", side_effect=sender)
                if sender
                else contextlib.nullcontext()
            ):
                code = AGENT.run_once(
                    self.harness.host_path, self.harness.config_path, self.harness.state
                )
        return code, output.getvalue()

    def read_state(self) -> dict:
        return json.loads((self.harness.state / "pull-agent.json").read_text())

    def calls(self) -> list[dict]:
        if not self.harness.log.exists():
            return []
        return [json.loads(line) for line in self.harness.log.read_text().splitlines()]

    def test_config_shape_is_exact_and_paths_absolute(self):
        base = dict(self.harness.config)
        cases = {}
        extra = dict(base)
        extra["extra"] = True
        cases["extra"] = extra
        missing = dict(base)
        del missing["deployer"]
        cases["missing"] = missing
        relative = dict(base)
        relative["gh_command"] = "bin/gh"
        cases["relative"] = relative
        traversal = dict(base)
        traversal["gh_command"] = "/tmp/../bin/gh"
        cases["traversal"] = traversal
        for name, value in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(AGENT.AgentError):
                    AGENT.validate_config(value)

    def test_stage_selects_newest_rc_prerelease_only(self):
        releases = [
            {"tagName": "v2.4.3", "isPrerelease": False, "isDraft": False},
            {"tagName": "v2.5.0-rc.9", "isPrerelease": True, "isDraft": False},
            {"tagName": "v2.5.0-rc.10", "isPrerelease": True, "isDraft": False},
            {"tagName": "v2.6.0", "isPrerelease": False, "isDraft": False},
            {"tagName": "v2.5.0-rc.11", "isPrerelease": True, "isDraft": True},
        ]
        selected = AGENT.select_release(releases, "stage")
        self.assertEqual(selected["tagName"], "v2.5.0-rc.10")

    def test_production_selects_newest_formal_release_only(self):
        releases = [
            {"tagName": "v2.4.3", "isPrerelease": False, "isDraft": False},
            {"tagName": "v2.5.0-rc.10", "isPrerelease": True, "isDraft": False},
            {"tagName": "v2.5.0", "isPrerelease": False, "isDraft": False},
            {"tagName": "v2.4.3-rc.99", "isPrerelease": True, "isDraft": False},
        ]
        selected = AGENT.select_release(releases, "production")
        self.assertEqual(selected["tagName"], "v2.5.0")

    def test_second_run_is_refused_while_lock_is_held(self):
        lock = self.harness.state / ".pull-agent.lock"
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, output = self.run_agent()
        finally:
            os.close(fd)
        self.assertNotEqual(code, 0)
        self.assertIn("concurrent_run_refused", output)
        self.assertFalse((self.harness.state / "pull-agent.json").exists())
        self.assertEqual(self.calls(), [])

    def test_already_in_place_by_state_account_does_nothing(self):
        value = self.harness.base_state()
        value.update({"target_tag": self.harness.target_tag, "deployer_state": "verified"})
        self.harness.save_state(value)
        code, _ = self.run_agent()
        self.assertEqual(code, 0)
        kinds = {call["kind"] for call in self.calls()}
        self.assertEqual(kinds, {"gh"})
        self.assertEqual(self.read_state()["last_result"], "already_in_place")

    def test_already_in_place_by_running_digests_does_nothing(self):
        self.harness.set_env(DOCKER_MODE="match")
        code, _ = self.run_agent()
        self.assertEqual(code, 0)
        kinds = {call["kind"] for call in self.calls()}
        self.assertEqual(kinds, {"gh", "manifest", "docker"})
        self.assertNotIn("deployer", kinds)
        self.assertEqual(self.read_state()["last_result"], "already_in_place_external")
        self.assertEqual(sum(call["kind"] == "docker" for call in self.calls()), 7)

    def test_pulled_but_not_running_images_are_not_in_place(self):
        self.harness.state_for_old()
        self.harness.set_running_containers(self.harness.target_tag, running=False)
        self.harness.set_env(DOCKER_MODE="stopped", DEPLOY_STATUS="running")
        code, _ = self.run_agent()
        self.assertEqual(code, 0)
        self.assertEqual(self.read_state()["last_result"], "running")
        self.assertTrue(
            any(call["kind"] == "deployer" and "apply" in call["argv"] for call in self.calls())
        )

    def test_missing_image_means_not_installed_not_unknown(self):
        self.harness.state_for_old()
        self.harness.set_env(DOCKER_MODE="missing-image", DEPLOY_STATUS="running")
        code, _ = self.run_agent()
        self.assertEqual(code, 0)
        self.assertEqual(self.read_state()["last_result"], "running")

    def test_docker_daemon_unavailable_is_unknown(self):
        self.harness.state_for_old()
        self.harness.set_env(DOCKER_MODE="daemon")
        messages = []
        code, _ = self.run_agent(sender=lambda message, env, timeout: messages.append(message))
        self.assertEqual(code, 0)
        self.assertEqual(self.read_state()["last_result"], "unknown")
        self.assertEqual(len(messages), 1)

    def test_bundle_digest_mismatch_refuses_and_alerts(self):
        self.harness.state_for_old()
        self.harness.asset.write_bytes(b"tampered package")
        messages = []
        code, _ = self.run_agent(sender=lambda message, env, timeout: messages.append(message))
        self.assertEqual(code, 0)
        self.assertEqual(self.read_state()["last_result"], "bundle_digest_mismatch")
        self.assertEqual(len(messages), 1)
        self.assertIn("bundle_digest_mismatch", messages[0])
        self.assertFalse(any(call["kind"] == "bundle-install" for call in self.calls()))
        self.assertFalse(list(self.harness.state.glob(".release-*")))

    def test_uses_the_newly_installed_bundle_scripts(self):
        self.harness.state_for_old()
        messages = []
        code, _ = self.run_agent(sender=lambda message, env, timeout: messages.append(message))
        self.assertEqual(code, 0)
        deployer_calls = [call for call in self.calls() if call["kind"] == "deployer"]
        self.assertTrue(deployer_calls)
        bundle_digest = json.loads(
            (self.harness.manifests / (self.harness.target_tag + ".json")).read_text()
        )["control_bundle"]["sha256"]
        self.assertTrue(all(bundle_digest in call["script"] for call in deployer_calls))
        self.assertNotIn(
            str(self.harness.old_deployer), "\n".join(json.dumps(call) for call in deployer_calls)
        )

    def test_interrupted_plan_is_continued_before_in_place_check(self):
        self.harness.state_for_continuation()
        self.harness.set_env(DOCKER_MODE="match", DEPLOY_STATUS="running")
        calls = []
        code, _ = self.run_agent(sender=lambda message, env, timeout: calls.append(message))
        self.assertEqual(code, 0)
        deployer = [call for call in self.calls() if call["kind"] == "deployer"]
        self.assertTrue(
            any("apply" in call["argv"] and "continuing-plan" in call["argv"] for call in deployer)
        )
        self.assertFalse(any("plan" in call["argv"] for call in deployer))
        self.assertEqual(self.read_state()["plan_id"], "continuing-plan")
        self.assertEqual(self.read_state()["deployer_state"], "running")
        self.assertFalse(any(call["kind"] == "docker" for call in self.calls()))

    def test_resume_reuses_existing_approval_fingerprint(self):
        self.harness.state_for_old()
        self.harness.set_env(DEPLOY_STATUS="running")
        first_code, _ = self.run_agent()
        self.assertEqual(first_code, 0)
        approval_path = next(self.harness.state.glob("*.approval.json"))
        before_bytes = approval_path.read_bytes()
        before_mtime = approval_path.stat().st_mtime_ns
        first_state = self.read_state()
        self.assertEqual(first_state["deployer_state"], "running")

        second_code, output = self.run_agent()
        self.assertEqual(second_code, 0)
        self.assertEqual(approval_path.read_bytes(), before_bytes)
        self.assertEqual(approval_path.stat().st_mtime_ns, before_mtime)
        self.assertEqual(self.read_state()["approval_sha256"], first_state["approval_sha256"])
        self.assertIn("approval_reused", output)
        deployer = [call for call in self.calls() if call["kind"] == "deployer"]
        self.assertEqual(sum("plan" in call["argv"] for call in deployer), 1)
        self.assertEqual(sum("apply" in call["argv"] for call in deployer), 2)

    def test_old_release_comes_from_last_verified_plan_or_running_containers(self):
        self.harness.state_for_old()
        self.harness.set_env(DEPLOY_STATUS="running")
        code, _ = self.run_agent()
        self.assertEqual(code, 0)
        request = next(self.harness.state.glob("*.request.json"))
        self.assertEqual(json.loads(request.read_text())["old"]["tag"], self.harness.old_tag)

    def test_old_release_comes_from_running_containers_when_state_is_not_verified(self):
        previous = self.harness.previous_plan(self.harness.target_tag)
        state = self.harness.base_state()
        state.update(
            {
                "target_tag": self.harness.target_tag,
                "deployer_state": "failed",
                "plan_id": previous["id"],
            }
        )
        self.harness.save_state(state)
        self.harness.set_releases(
            [
                self.harness._release(self.harness.old_tag, False),
                self.harness._release(self.harness.target_tag, True),
            ]
        )
        self.harness.set_running_containers(self.harness.old_tag)
        self.harness.set_env(DOCKER_MODE="records", DEPLOY_STATUS="running")
        code, _ = self.run_agent()
        self.assertEqual(code, 0)
        request = next(self.harness.state.glob("*.request.json"))
        self.assertEqual(json.loads(request.read_text())["old"]["tag"], self.harness.old_tag)

    def test_recovery_target_never_points_at_new(self):
        self.harness.state_for_old()
        self.harness.set_env(DEPLOY_STATUS="running")
        code, _ = self.run_agent()
        self.assertEqual(code, 0)
        request = json.loads(next(self.harness.state.glob("*.request.json")).read_text())
        self.assertEqual(
            request["recovery"]["target_manifest_sha256"], AGENT.fingerprint(request["old"])
        )
        self.assertNotEqual(
            request["recovery"]["target_manifest_sha256"], AGENT.fingerprint(request["new"])
        )

    def test_downloads_use_clobber_and_separate_directories(self):
        self.harness.state_for_old()
        self.harness.set_env(DEPLOY_STATUS="running")
        code, _ = self.run_agent()
        self.assertEqual(code, 0)
        downloads = [
            call
            for call in self.calls()
            if call["kind"] == "gh" and call["argv"][:2] == ["release", "download"]
        ]
        self.assertEqual(len(downloads), 2)
        self.assertTrue(all("--clobber" in call["argv"] for call in downloads))
        directories = {call["argv"][call["argv"].index("--dir") + 1] for call in downloads}
        self.assertEqual(len(directories), 2)
        self.assertTrue(all(path.endswith(("/new", "/old")) for path in directories))

    def test_alert_env_file_must_be_owned_by_runner(self):
        runner = os.getuid()
        self.assertEqual(
            AGENT._load_alert_credentials(self.harness.alert_env)["LINGXI_ADMIN_GROUP_CHAT_ID"],
            "oc_synthetic",
        )
        with patch.object(AGENT.os, "getuid", return_value=runner + 1):
            with self.assertRaises(AGENT.AgentError):
                AGENT._load_alert_credentials(self.harness.alert_env)

    def test_approval_binds_plan_fingerprint_and_contract_source(self):
        self.harness.state_for_old()
        code, output = self.run_agent(sender=lambda message, env, timeout: None)
        self.assertEqual(code, 0)
        approval_files = list(self.harness.state.glob("*.approval.json"))
        self.assertEqual(len(approval_files), 1)
        approval = json.loads(approval_files[0].read_text())
        plan = json.loads((self.harness.state / (approval["plan_id"] + ".plan.json")).read_text())
        self.assertEqual(
            set(approval),
            {
                "schema",
                "plan_id",
                "plan_sha256",
                "operation",
                "source",
                "approved_at",
                "expires_at",
            },
        )
        self.assertEqual(approval["plan_sha256"], AGENT.fingerprint(plan))
        self.assertIn(approval["source"], self.harness.host["approval_sources"])
        self.assertEqual(stat.S_IMODE(approval_files[0].stat().st_mode), 0o600)
        state = self.read_state()
        self.assertEqual(state["candidate_run_id"], 2)
        self.assertEqual(state["approval_sha256"], AGENT.fingerprint(approval))
        self.assertIn("candidate_run_id=2", output)
        self.assertIn("approval_sha256=" + AGENT.fingerprint(approval), output)

    def test_pre_apply_hook_failure_stops_before_apply(self):
        self.harness.state_for_old()
        config = dict(self.harness.config)
        config["pre_apply_hooks"] = [str(self.harness.hook_fail)]
        self.harness.config_path = _json(self.harness.config_path, config)
        messages = []
        code, _ = self.run_agent(sender=lambda message, env, timeout: messages.append(message))
        self.assertEqual(code, 0)
        self.assertEqual(self.read_state()["last_result"], "pre_apply_hook_failed")
        self.assertEqual(len(messages), 1)
        self.assertFalse(
            any(call["kind"] == "deployer" and "apply" in call["argv"] for call in self.calls())
        )

    def test_deploy_timeout_alerts_and_leaves_plan_for_next_run(self):
        self.harness.state_for_old()
        self.harness.set_env(DEPLOY_MODE="timeout")
        config = dict(self.harness.config)
        config["deploy_timeout_seconds"] = 1
        self.harness.config_path = _json(self.harness.config_path, config)
        messages = []
        code, _ = self.run_agent(sender=lambda message, env, timeout: messages.append(message))
        self.assertEqual(code, 0)
        value = self.read_state()
        self.assertEqual(value["last_result"], "deploy_timeout")
        self.assertEqual(value["deployer_state"], "unknown")
        self.assertTrue(list(self.harness.state.glob("*.plan.json")))
        self.assertEqual(len(messages), 1)

    def test_deployer_state_is_copied_verbatim(self):
        self.harness.state_for_old()
        self.harness.set_env(DEPLOY_STATUS="failed")
        messages = []
        code, _ = self.run_agent(sender=lambda message, env, timeout: messages.append(message))
        self.assertEqual(code, 0)
        self.assertEqual(self.read_state()["deployer_state"], "failed")
        self.assertEqual(self.read_state()["last_result"], "failed")
        self.assertEqual(len(messages), 1)

    def test_alert_dedup_and_recovery_notice(self):
        messages = []
        for _ in range(3):
            code, _ = self.run_agent(sender=lambda message, env, timeout: messages.append(message))
            self.assertEqual(code, 0)
        self.assertEqual(len(messages), 1)
        self.harness.set_releases([self.harness._release(self.harness.target_tag, True)])
        self.harness.set_env(DOCKER_MODE="match")
        code, _ = self.run_agent(sender=lambda message, env, timeout: messages.append(message))
        self.assertEqual(code, 0)
        self.assertEqual(len(messages), 2)
        self.assertIn("recovered", messages[1])

    def test_alert_delivery_failure_is_not_swallowed(self):
        self.harness.state_for_old()
        self.harness.asset.write_bytes(b"tampered package")

        def fail(message, env, timeout):
            raise RuntimeError("network failure")

        code, _ = self.run_agent(sender=fail)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.read_state()["last_result"], "alert_delivery_failed")
        self.assertTrue(self.read_state()["alerts"])

    def test_logs_and_state_never_contain_secrets(self):
        self.harness.state_for_old()
        messages = []
        with patch.dict(os.environ, {"SECRET_SENTINEL": self.harness.sentinel}, clear=False):
            code, output = self.run_agent(
                sender=lambda message, env, timeout: messages.append(message)
            )
        self.assertEqual(code, 0)
        state_text = (self.harness.state / "pull-agent.json").read_text()
        log_text = self.harness.log.read_text()
        self.assertNotIn(self.harness.sentinel, state_text)
        self.assertNotIn(self.harness.sentinel, output)
        self.assertNotIn(self.harness.sentinel, "\n".join(messages))
        self.assertIn(self.harness.sentinel, log_text)

    def test_systemd_units_shape(self):
        service = (ROOT / "deploy/monitoring-units/lingxi-release-pull.service").read_text()
        timer = (ROOT / "deploy/monitoring-units/lingxi-release-pull.timer").read_text()
        self.assertIn("Type=oneshot", service)
        self.assertIn("OnCalendar=*:0/5", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("AccuracySec=30s", timer)
        timeout = next(line for line in service.splitlines() if line.startswith("TimeoutStartSec="))
        self.assertGreaterEqual(int(timeout.split("=", 1)[1]), 3600)
        exec_line = next(line for line in service.splitlines() if line.startswith("ExecStart="))
        self.assertFalse(any("=" in arg for arg in exec_line.split()[1:]))
        drop_in = (ROOT / "deploy/monitoring-units/10-local.conf.example").read_text()
        self.assertRegex(drop_in, r"(?m)^User=<部署用户>$")

    def test_control_bundle_files_include_agent_units_doc_and_examples(self):
        spec = importlib.util.spec_from_file_location(
            "control_bundle_for_pull_test", ROOT / "deploy/control_bundle.py"
        )
        bundle = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(bundle)
        expected = {
            "deploy/release_pull_agent.py",
            "deploy/monitoring-units/lingxi-release-pull.service",
            "deploy/monitoring-units/lingxi-release-pull.timer",
            "deploy/拉取代理.md",
            "deploy/control/引导安装.md",
            "deploy/control/examples/host-contract.json",
            "deploy/control/examples/public-config.json",
            "deploy/control/examples/binding.json",
            "deploy/control/examples/innertest-relay.json",
        }
        self.assertTrue(expected.issubset(set(bundle.FILES)))


if __name__ == "__main__":
    unittest.main()
