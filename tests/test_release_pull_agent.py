"""Release 拉取代理的本机隔离用例与变异防线。"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
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
# gh 2.97 `gh release list --help` 列出的全部 JSON 字段；假 gh 据此拒绝其余字段。
GH_RELEASE_LIST_FIELDS = (
    "createdAt",
    "isDraft",
    "isImmutable",
    "isLatest",
    "isPrerelease",
    "name",
    "publishedAt",
    "tagName",
)


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
                stream.write(json.dumps({"kind":"gh", "argv":sys.argv[1:], "sentinel":os.environ.get("SECRET_SENTINEL", ""), "no_bytecode":os.environ.get("PYTHONDONTWRITEBYTECODE")}) + "\n")
            if sys.argv[1:3] == ["release", "list"]:
                # 复刻 gh 2.97：--json 只接受手册字段，未知字段退出 1；只回请求到的字段。
                fields = sys.argv[sys.argv.index("--json") + 1].split(",")
                for field in fields:
                    if field not in os.environ["GH_RELEASE_LIST_FIELDS"].split(","):
                        print(f'Unknown JSON field: "{field}"', file=sys.stderr)
                        raise SystemExit(1)
                releases = json.load(open(os.environ["RELEASES_FILE"], encoding="utf-8"))
                print(json.dumps([{k: v for k, v in item.items() if k in fields} for item in releases]))
                raise SystemExit(0)
            if sys.argv[1:3] == ["release", "download"]:
                directory = sys.argv[sys.argv.index("--dir") + 1]
                shutil.copyfile(os.environ["ASSET_FILE"], os.path.join(directory, "lingxi-control.tar"))
                # 复刻预发实读：不可变 Release 上没有 control-index.json 附件时，gh 对匹配不到的
                # 那个 --pattern 仍退出 0，只落下 tar 一个文件。
                if os.environ.get("INDEX_ASSET_MODE", "present") == "present":
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
                stream.write(json.dumps({"kind":"manifest", "script":os.path.abspath(__file__), "argv":sys.argv[1:], "no_bytecode":os.environ.get("PYTHONDONTWRITEBYTECODE")}) + "\n")
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
                if os.environ.get("BUNDLE_INSTALL_MODE") == "refuse":
                    # 复刻 control_bundle.verify_install 对已装目录里出现可写项（__pycache__）的拒绝。
                    raise RuntimeError("安装目录存在链接或可写项")
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
                value = {"kind":kind, "script":os.path.abspath(__file__), "argv":sys.argv[1:], "no_bytecode":os.environ.get("PYTHONDONTWRITEBYTECODE")}
                if extra:
                    value.update(extra)
                with open(os.environ["CALL_LOG"], "a", encoding="utf-8") as stream:
                    stream.write(json.dumps(value) + "\n")
            args = sys.argv[1:]
            log("deployer")
            operation = args[args.index("--state-directory") + 2]
            state_dir = args[args.index("--state-directory") + 1]
            host = json.load(open(args[args.index("--host-contract") + 1], encoding="utf-8"))
            failed_marker = os.path.join(state_dir, ".fake-apply-failed")
            if operation == "plan":
                request = json.load(open(args[args.index("--request") + 1], encoding="utf-8"))
                plan = dict(request)
                plan.update({"schema":1, "host":host["host"], "environment":host["environment"], "project":host["project"]})
                path = os.path.join(state_dir, plan["id"] + ".plan.json")
                with open(path, "w", encoding="utf-8") as stream:
                    json.dump(plan, stream, ensure_ascii=False, sort_keys=True, indent=2)
                os.chmod(path, 0o600)
                print(json.dumps({"plan":plan, "plan_sha256":hashlib.sha256(canonical(plan)).hexdigest()}))
            elif operation == "apply":
                if os.environ.get("DEPLOY_MODE") == "timeout":
                    time.sleep(4)
                # 与 deploy_runtime.Runtime.installation_receipt 相同的六条校验：收据指纹绑进
                # 计划、键集合、schema、环境 / 项目、目标新包摘要、绑定版本、六项核对全真。
                plan_path = os.path.join(state_dir, args[args.index("apply") + 1] + ".plan.json")
                plan = json.load(open(plan_path, encoding="utf-8"))
                channel = plan["channel"]
                receipt_path = os.path.join(host["config_root"], "deployment-installation.json")
                try:
                    receipt = json.load(open(receipt_path, encoding="utf-8"))
                    mismatched = (
                        hashlib.sha256(canonical(receipt)).hexdigest() != channel.get("installation_receipt_sha256")
                        or set(receipt) != {"schema", "environment", "project", "bundle_sha256", "binding_version", "checks"}
                        or receipt["schema"] != 1
                        or receipt["environment"] != plan["environment"]
                        or receipt["project"] != plan["project"]
                        or receipt["bundle_sha256"] != plan["new"]["control_bundle"]["sha256"]
                        or receipt["binding_version"] != channel.get("binding_version")
                        or receipt["checks"] != {
                            "sudo_policy": True,
                            "sshd_policy": True,
                            "credential_owner": True,
                            "authorized_peer": True,
                            "wrong_uid_rejected": True,
                            "container_peer_rejected": True,
                        }
                    )
                except (OSError, ValueError, KeyError, TypeError):
                    mismatched = True
                if mismatched:
                    log("receipt-check", {"result":"installation_receipt_missing_or_mismatched"})
                    open(failed_marker, "w", encoding="utf-8").close()
                    print("installation_receipt_missing_or_mismatched", file=sys.stderr)
                    raise SystemExit(1)
                log("receipt-check", {"result":"ok"})
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
                status = os.environ.get("DEPLOY_STATUS", "verified")
                if os.path.exists(failed_marker):
                    status = "failed"
                print(json.dumps({"status":status}))
            else:
                raise SystemExit(7)
            """,
        )
        # 真实清单工具会加载同版本目录里的 deploy/control_bundle.py；假清单工具同样导入一个
        # 同目录模块，没有禁写字节码缓存时版本目录里就会多出 __pycache__（预发实读的成因）。
        _write(
            self.installed_source / "scripts" / "ci" / "manifest_sibling.py",
            "VALUE = 1\n",
            0o644,
        )
        self.installed_manifest = _exec(
            self.installed_source / "scripts" / "ci" / "release_manifest.py",
            r"""
            #!/usr/bin/env python3
            import json, os, sys
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import manifest_sibling
            with open(os.environ["CALL_LOG"], "a", encoding="utf-8") as stream:
                stream.write(json.dumps({"kind":"installed-manifest", "script":os.path.abspath(__file__), "argv":sys.argv[1:], "no_bytecode":os.environ.get("PYTHONDONTWRITEBYTECODE"), "sibling":manifest_sibling.VALUE}) + "\n")
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
        self.receipt_path = self.config_root / "deployment-installation.json"
        self.bootstrap_bundle_sha256 = "c" * 64
        self.install_receipt()
        self.install_channel_materials()
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
            "GH_RELEASE_LIST_FIELDS": ",".join(GH_RELEASE_LIST_FIELDS),
            "MANIFESTS_DIR": str(self.manifests),
            "ASSET_FILE": str(self.asset),
            "INDEX_FILE": str(self.index),
            "INDEX_ASSET_MODE": "present",
            "INSTALLED_SOURCE": str(self.installed_source),
            "DOCKER_MODE": "mismatch",
            "DOCKER_CONTAINERS_JSON": "{}",
            "DOCKER_IMAGES_JSON": "{}",
            "DEPLOY_STATUS": "verified",
            "DEPLOY_MODE": "normal",
            "BUNDLE_INSTALL_MODE": "normal",
        }
        self.old_tag = "v2.4.3"
        self.target_tag = "v2.5.0-rc.10" if environment == "stage" else "v2.5.0"
        self._make_manifest(self.old_tag, formal=environment == "production", seed="1")
        self._make_manifest(self.target_tag, formal=environment == "production", seed="2")
        # 在位代理是被测源码的一份独立副本：自替换只许改这份副本，绝不能碰源码树；
        # 包内候选默认与它逐字节相同，所以既有用例每轮只多一行 agent_self_update_unchanged。
        self.agent_source = MODULE_PATH.read_bytes()
        self.in_place_agent = self.root / "agent-in-place" / "release_pull_agent.py"
        _write(self.in_place_agent, self.agent_source, 0o644)
        self.in_place_agent.parent.chmod(0o755)
        self._program_path = patch.object(
            AGENT, "_agent_program_path", return_value=self.in_place_agent
        )
        self._program_path.start()
        self.install_bundle_agent(self.agent_source)
        self.set_releases([self._release(self.target_tag, environment == "stage")])
        self._write_env()
        self.set_running_containers(self.target_tag)

    def _write_env(self):
        self._old_env = os.environ.copy()
        os.environ.update(self._env)

    def agent_with_version(self, version: int) -> bytes:
        """把被测源码的版本标记行换成 ``version``；找不到标记行即断言失败，不静默退化。"""
        current = f"\nAGENT_VERSION = {AGENT.AGENT_VERSION}\n".encode()
        assert current in self.agent_source
        return self.agent_source.replace(current, f"\nAGENT_VERSION = {version}\n".encode(), 1)

    def install_bundle_agent(self, data: bytes, *, indexed_sha256: str | None = None) -> str:
        """把一份代理写进假控制包：版本目录源、目录内索引与外部索引附件，并重算两份清单。

        ``indexed_sha256`` 默认为文件真实摘要；传入别的值即模拟索引条目与文件不符。
        """
        sha = hashlib.sha256(data).hexdigest()
        candidate = self.installed_source / "deploy" / "release_pull_agent.py"
        candidate.unlink(missing_ok=True)
        _write(candidate, data, 0o444)
        index = {
            "schema_revision": 1,
            "source_commit": "1" * 40,
            "runtime": {"python_minimum": "3.11", "platform": "linux"},
            "files": [
                {
                    "path": "deploy/release_pull_agent.py",
                    "sha256": indexed_sha256 or sha,
                    "mode": 0o444,
                    "size": len(data),
                    "source_commit": "1" * 40,
                }
            ],
        }
        self.install_bundle_index(AGENT.canonical(index))
        return sha

    def install_bundle_index(self, index_bytes: bytes) -> None:
        """外部索引附件与版本目录内索引写成同一份内容，并重算两份清单的 ``index_sha256``。"""
        self.index.write_bytes(index_bytes)
        installed_index = self.installed_source / "control-index.json"
        installed_index.unlink(missing_ok=True)
        _write(installed_index, index_bytes, 0o444)
        formal = self.host["environment"] == "production"
        self._make_manifest(self.old_tag, formal=formal, seed="1")
        self._make_manifest(self.target_tag, formal=formal, seed="2")

    def in_place_sha256(self) -> str:
        return hashlib.sha256(self.in_place_agent.read_bytes()).hexdigest()

    def backups(self) -> list[Path]:
        return sorted(self.in_place_agent.parent.glob("release_pull_agent.py.bak-*"))

    def write_config(self, **overrides):
        self.config.update(overrides)
        _json(self.config_path, self.config)

    def receipt(self, bundle_sha256: str | None = None, **overrides) -> dict:
        """引导安装时人工写定的收据；六项核对全真，bundle_sha256 默认是引导安装的包摘要。"""
        value = {
            "schema": 1,
            "environment": self.host["environment"],
            "project": self.host["project"],
            "bundle_sha256": bundle_sha256 or self.bootstrap_bundle_sha256,
            "binding_version": 1,
            "checks": {name: True for name in AGENT.INSTALLATION_RECEIPT_CHECKS},
        }
        value.update(overrides)
        return value

    def install_receipt(self, bundle_sha256: str | None = None, **overrides) -> dict:
        value = self.receipt(bundle_sha256, **overrides)
        _write(self.receipt_path, AGENT.canonical(value), 0o600)
        return value

    def read_receipt(self) -> dict:
        return json.loads(self.receipt_path.read_text(encoding="utf-8"))

    def bundle_sha256(self, tag: str) -> str:
        manifest = json.loads((self.manifests / (tag + ".json")).read_text())
        return manifest["control_bundle"]["sha256"]

    def install_channel_materials(self):
        """首次部署（没有旧计划）时 _host_materials 需要的 binding 与 relay 非秘密材料。"""
        _json(
            self.config_root / "innertest" / "binding.json",
            {
                "schema_revision": 1,
                "binding_id": "synthetic-binding",
                "host_uid": 41001,
                "peer_uid": 51001,
                "uid_map_sha256": "d" * 64,
                "socket_gid": 42001,
            },
            0o644,
        )
        (self.config_root / "innertest").chmod(0o755)
        relay_directory = self.relay_root / ("e" * 64)
        _json(
            relay_directory / "innertest-relay.json",
            {
                "schema_revision": 1,
                "socket_path": "/run/lingxi-innertest/admin.sock",
                "relay_uid": 41001,
                "socket_owner_uid": 10001,
            },
            0o644,
        )
        _json(relay_directory / "installation.json", {"relay_sha256": "f" * 64}, 0o644)
        relay_directory.chmod(0o755)
        (self.relay_root / "current").symlink_to(relay_directory.name)

    def close(self):
        self._program_path.stop()
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

    def use_real_package(self, embedded_index: bytes | None = None, *, index_asset: bool = True):
        """把附件换成真实 tar（内嵌 ``control-index.json`` 成员），并重算两份清单的摘要。

        清单 ``index_sha256`` 始终按 ``self.index``（外部附件的内容）计算；``embedded_index``
        默认与之相同，传入不同内容即模拟内嵌索引被篡改。``index_asset=False`` 让假 gh 不再
        落下外部索引附件，复刻不可变 Release 上没有该附件的已发布版本。
        """
        if embedded_index is None:
            embedded_index = self.index.read_bytes()
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name, data in (
                ("deploy/lingxi_deploy.py", b"print('synthetic deployer')\n"),
                ("control-index.json", embedded_index),
            ):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o444
                info.mtime = 0
                archive.addfile(info, io.BytesIO(data))
        self.asset.write_bytes(buffer.getvalue())
        formal = self.host["environment"] == "production"
        self._make_manifest(self.old_tag, formal=formal, seed="1")
        self._make_manifest(self.target_tag, formal=formal, seed="2")
        os.environ["INDEX_ASSET_MODE"] = "present" if index_asset else "absent"

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
            "host": self.host["host"],
            "environment": self.host["environment"],
            "project": self.host["project"],
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

    def expected_digests(self, tag: str) -> dict:
        manifest = json.loads((self.manifests / (tag + ".json")).read_text())
        return AGENT._expected_digests(manifest)

    def state_for_verified_target(self, tag: str | None = None, *, with_digests: bool = True):
        """状态账已证实 tag 在位：verified 计划的 new 就是 tag，容器摘要按开关记录。"""
        tag = tag or self.target_tag
        plan = self.previous_plan(tag)
        value = self.base_state()
        value.update(
            {
                "target_tag": tag,
                "deployer_state": "verified",
                "plan_id": plan["id"],
                "highest_deployed_tag": tag if with_digests else None,
                "verified_digests": self.expected_digests(tag) if with_digests else None,
            }
        )
        self.save_state(value)
        return value

    def state_for_continuation(self):
        plan = self.previous_plan(self.target_tag)
        plan["id"] = "continuing-plan"
        plan["new"] = json.loads((self.manifests / (self.target_tag + ".json")).read_text())
        plan["approval_source"] = self.host["approval_sources"][0]
        # 中断前那一轮已把收据刷成目标新包；接续轮沿用计划里的收据指纹，不再改收据。
        receipt = self.install_receipt(self.bundle_sha256(self.target_tag))
        plan["channel"] = {
            "synthetic": True,
            "binding_version": receipt["binding_version"],
            "installation_receipt_sha256": AGENT.fingerprint(receipt),
        }
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
        # 代理在取锁前要求宿主契约 host 等于内核主机名；夹具契约固定为 synthetic-host。
        hostname = patch.object(AGENT.socket, "gethostname", return_value=self.harness.host["host"])
        hostname.start()
        self.addCleanup(hostname.stop)

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

    def test_host_contract_must_equal_kernel_hostname_before_lock_and_state(self):
        messages = []
        with patch.object(AGENT.socket, "gethostname", return_value="synthetic-host-full.internal"):
            code, output = self.run_agent(
                sender=lambda message, env, timeout: messages.append(message)
            )
        self.assertEqual(code, 1)
        self.assertIn("阶段=agent 结果码=host_mismatch", output)
        self.assertIn("host=synthetic-host actual=synthetic-host-full.internal", output)
        # 状态目录没有任何写入（没有锁文件、没有状态账），不出网、不读 Docker、不告警。
        self.assertEqual(list(self.harness.state.iterdir()), [])
        self.assertEqual(self.calls(), [])
        self.assertEqual(messages, [])
        # 比对在取锁之前：别的进程持锁时仍报 host_mismatch，而不是 concurrent_run_refused。
        lock = self.harness.state / ".pull-agent.lock"
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(AGENT.socket, "gethostname", return_value="another-host"):
                code, output = self.run_agent()
        finally:
            os.close(fd)
        self.assertEqual(code, 1)
        self.assertIn("结果码=host_mismatch", output)
        self.assertNotIn("concurrent_run_refused", output)
        self.assertFalse((self.harness.state / "pull-agent.json").exists())

    def test_already_in_place_by_state_account_does_nothing(self):
        self.harness.state_for_verified_target()
        self.harness.set_env(DOCKER_MODE="match")
        code, _ = self.run_agent()
        self.assertEqual(code, 0)
        # 零动作 = 只列 Release 并只读回读容器；不解析清单、不下载、不调部署器。
        kinds = {call["kind"] for call in self.calls()}
        self.assertEqual(kinds, {"gh", "docker"})
        self.assertFalse(
            any(call["kind"] == "gh" and "download" in call["argv"] for call in self.calls())
        )
        self.assertEqual(self.read_state()["last_result"], "already_in_place")

    def test_release_list_uses_only_valid_gh_json_fields(self):
        # 假 gh 复刻 gh 2.97 的字段校验；先证明它真的会拒绝 url，本用例才有证明力。
        probe = subprocess.run(
            [
                str(self.harness.gh),
                "release",
                "list",
                "--repo",
                "Moshuiwang/lingxi",
                "--limit",
                "1",
                "--json",
                "tagName,url",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(probe.returncode, 1)
        self.assertIn('Unknown JSON field: "url"', probe.stderr)
        self.harness.log.unlink()
        self.harness.state_for_verified_target()
        self.harness.set_env(DOCKER_MODE="match")
        code, _ = self.run_agent()
        self.assertEqual(code, 0)
        state = self.read_state()
        self.assertEqual(state["last_result"], "already_in_place")
        listing = [
            call
            for call in self.calls()
            if call["kind"] == "gh" and call["argv"][:2] == ["release", "list"]
        ]
        self.assertEqual(len(listing), 1)
        fields = listing[0]["argv"][listing[0]["argv"].index("--json") + 1].split(",")
        self.assertTrue(set(fields) <= set(GH_RELEASE_LIST_FIELDS), fields)
        self.assertNotIn("url", fields)
        self.assertEqual(
            state["release_url"],
            f"https://github.com/Moshuiwang/lingxi/releases/tag/{self.harness.target_tag}",
        )

    def test_verified_state_still_checks_running_containers_each_round(self):
        messages = []

        def sender(message, env, timeout):
            messages.append(message)

        with self.subTest(case="state_without_recorded_digests_resolves_manifest_once"):
            self.harness.state_for_verified_target(with_digests=False)
            self.harness.set_env(DOCKER_MODE="match")
            code, _ = self.run_agent(sender=sender)
            self.assertEqual(code, 0)
            state = self.read_state()
            self.assertEqual(state["last_result"], "already_in_place")
            self.assertEqual(
                state["verified_digests"], self.harness.expected_digests(self.harness.target_tag)
            )
            self.assertEqual({call["kind"] for call in self.calls()}, {"gh", "manifest", "docker"})
            self.harness.log.unlink()
        with self.subTest(case="consistent"):
            self.harness.state_for_verified_target()
            self.harness.set_env(DOCKER_MODE="match")
            code, _ = self.run_agent(sender=sender)
            self.assertEqual(code, 0)
            state = self.read_state()
            self.assertEqual(state["last_result"], "already_in_place")
            self.assertEqual(state["deployer_state"], "verified")
            self.assertIsNone(state["external_change_tag"])
            self.assertEqual({call["kind"] for call in self.calls()}, {"gh", "docker"})
            self.assertEqual(messages, [])
        with self.subTest(case="docker_unavailable_keeps_verified_for_next_round"):
            self.harness.set_env(DOCKER_MODE="daemon")
            code, _ = self.run_agent(sender=sender)
            self.assertEqual(code, 0)
            state = self.read_state()
            self.assertEqual(state["last_result"], "unknown")
            self.assertEqual(state["deployer_state"], "verified")
            self.assertEqual(len(messages), 1)
            self.harness.set_env(DOCKER_MODE="match")
            code, _ = self.run_agent(sender=sender)
            self.assertEqual(code, 0)
            self.assertEqual(self.read_state()["last_result"], "already_in_place")
            self.assertEqual(len(messages), 2)
            self.assertIn("recovered", messages[1])
            messages.clear()
        with self.subTest(case="external_change"):
            # 三个容器被外部换成另一版本的镜像：只告警，不重部署、不沿旧计划接续。
            self.harness.set_running_containers(self.harness.old_tag)
            for round_number in (1, 2):
                code, _ = self.run_agent(sender=sender)
                self.assertEqual(code, 0, round_number)
                state = self.read_state()
                self.assertEqual(state["last_result"], "external_change_detected")
                self.assertEqual(state["deployer_state"], "unknown")
                self.assertEqual(state["external_change_tag"], self.harness.target_tag)
                self.assertEqual(state["target_tag"], self.harness.target_tag)
                self.assertEqual(state["plan_id"], "old-plan")
                self.assertEqual(len(messages), 1)
            self.assertIn("external_change_detected", messages[0])
            kinds = {call["kind"] for call in self.calls()}
            self.assertEqual(kinds, {"gh", "docker"})
            self.assertFalse(list(self.harness.state.glob("*.request.json")))
        with self.subTest(case="containers_missing"):
            self.harness.set_running_containers(self.harness.target_tag, running=False)
            self.harness.set_env(DOCKER_MODE="stopped")
            code, _ = self.run_agent(sender=sender)
            self.assertEqual(code, 0)
            self.assertEqual(self.read_state()["last_result"], "external_change_detected")
            self.assertEqual(len(messages), 1)
        with self.subTest(case="recovered"):
            self.harness.set_running_containers(self.harness.target_tag)
            self.harness.set_env(DOCKER_MODE="match")
            code, _ = self.run_agent(sender=sender)
            self.assertEqual(code, 0)
            state = self.read_state()
            self.assertEqual(state["last_result"], "already_in_place")
            self.assertEqual(state["deployer_state"], "verified")
            self.assertIsNone(state["external_change_tag"])
            self.assertEqual(len(messages), 2)
            self.assertIn("recovered", messages[1])
        self.assertFalse(any(call["kind"] == "deployer" for call in self.calls()))

    def test_older_target_than_deployed_is_refused_not_deployed(self):
        key = AGENT.release_version_key
        self.assertLess(key("v2.5.0-rc.3"), key("v2.5.0"))
        self.assertLess(key("v2.5.0"), key("v2.5.1"))
        self.assertLess(key("v2.5.0-rc.9"), key("v2.5.0-rc.10"))
        self.assertTrue(AGENT.is_downgrade("v2.5.0-rc.3", "v2.5.0"))
        self.assertTrue(AGENT.is_downgrade("v2.5.0", "v2.5.1"))
        self.assertFalse(AGENT.is_downgrade("v2.5.0", "v2.5.0"))
        self.assertFalse(AGENT.is_downgrade("v2.5.1", "v2.5.0"))
        self.assertFalse(AGENT.is_downgrade("v2.5.0", "v2.5.0-rc.3"))
        self.assertFalse(AGENT.is_downgrade("v2.5.0", None))
        messages = []

        def sender(message, env, timeout):
            messages.append(message)

        older = "v2.5.0-rc.9"
        self.harness._make_manifest(older, formal=False, seed="4")
        hidden = dict(self.harness._release(self.harness.target_tag, True), isDraft=True)
        self.harness.set_releases([self.harness._release(older, True), hidden])
        self.harness.set_env(DOCKER_MODE="match")
        with self.subTest(case="state_account_knows_highest"):
            # 状态账已验证 rc.10；写权限者把 rc.10 改成草稿后选择器只剩 rc.9。
            self.harness.state_for_verified_target()
            for round_number in (1, 2):
                code, _ = self.run_agent(sender=sender)
                self.assertEqual(code, 0, round_number)
                state = self.read_state()
                self.assertEqual(state["last_result"], "downgrade_refused")
                self.assertEqual(state["target_tag"], self.harness.target_tag)
                self.assertEqual(state["deployer_state"], "verified")
                self.assertEqual(state["highest_deployed_tag"], self.harness.target_tag)
                self.assertEqual(len(messages), 1)
            self.assertIn("downgrade_refused", messages[0])
            self.assertIn("最高已部署版本：" + self.harness.target_tag, messages[0])
            self.assertIn(f"downgrade_refused:{older}:{self.harness.target_tag}", state["alerts"])
            self.assertEqual({call["kind"] for call in self.calls()}, {"gh"})
        with self.subTest(case="verified_plan_without_recorded_highest"):
            # 状态账没记最高版本时，从 verified 计划的 new.tag 反推，同样拒绝。
            self.harness.log.unlink()
            self.harness.state_for_verified_target(with_digests=False)
            code, _ = self.run_agent(sender=sender)
            self.assertEqual(code, 0)
            state = self.read_state()
            self.assertEqual(state["last_result"], "downgrade_refused")
            self.assertEqual(state["highest_deployed_tag"], self.harness.target_tag)
            self.assertEqual({call["kind"] for call in self.calls()}, {"gh", "manifest", "docker"})
        with self.subTest(case="running_formal_release_beats_rc_target"):
            # 状态账为空、容器运行手工装上的正式版 v2.5.0：反查得到的运行版本也算已部署。
            self.harness.log.unlink()
            (self.harness.state / "pull-agent.json").unlink()
            formal = "v2.5.0"
            self.harness._make_manifest(formal, formal=True, seed="6")
            self.harness.set_releases(
                [self.harness._release(older, True), self.harness._release(formal, False)]
            )
            self.harness.set_running_containers(formal)
            self.harness.set_env(DOCKER_MODE="records")
            code, _ = self.run_agent(sender=sender)
            self.assertEqual(code, 0)
            state = self.read_state()
            self.assertEqual(state["last_result"], "downgrade_refused")
            self.assertEqual(state["highest_deployed_tag"], formal)
            self.assertIn(f"downgrade_refused:{older}:{formal}", state["alerts"])
            self.assertEqual({call["kind"] for call in self.calls()}, {"gh", "manifest", "docker"})
        self.assertFalse(list(self.harness.state.glob("*.request.json")))
        self.assertFalse(
            any(call["kind"] == "gh" and "download" in call["argv"] for call in self.calls())
        )
        with self.subTest(case="same_version_is_not_a_downgrade"):
            self.harness.log.unlink()
            self.harness.set_releases([self.harness._release(self.harness.target_tag, True)])
            self.harness.state_for_verified_target()
            self.harness.set_running_containers(self.harness.target_tag)
            self.harness.set_env(DOCKER_MODE="match")
            code, _ = self.run_agent(sender=sender)
            self.assertEqual(code, 0)
            self.assertEqual(self.read_state()["last_result"], "already_in_place")
        with self.subTest(case="verified_deploy_records_highest_and_digests"):
            self.harness.log.unlink()
            self.harness.state_for_old()
            self.harness.set_env(DOCKER_MODE="mismatch", DEPLOY_STATUS="verified")
            code, _ = self.run_agent(sender=sender)
            self.assertEqual(code, 0)
            state = self.read_state()
            self.assertEqual(state["last_result"], "verified")
            self.assertEqual(state["highest_deployed_tag"], self.harness.target_tag)
            self.assertEqual(
                state["verified_digests"],
                self.harness.expected_digests(self.harness.target_tag),
            )

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

    def test_missing_index_asset_falls_back_to_embedded_index(self):
        """预发实读：已发布版本的 Release 上没有 control-index.json 附件（不可变 Release 不能
        事后补），此前折成 bundle_digest_mismatch；现在以摘要已核的 tar 内嵌索引为准。"""
        harness = self.harness
        harness.use_real_package(index_asset=False)
        harness.state_for_old()
        messages = []
        code, output = self.run_agent(sender=lambda message, env, timeout: messages.append(message))
        self.assertEqual(code, 0)
        self.assertEqual(self.read_state()["last_result"], "verified")
        self.assertEqual(self.read_state()["target_tag"], harness.target_tag)
        # 新目标与回读的旧版本两次下载都走内嵌索引，日志只标来源、不带路径。
        self.assertEqual(output.count("阶段=bundle 结果码=index_verified"), 2)
        self.assertEqual(output.count("index_source=embedded"), 2)
        self.assertNotIn("index_source=asset", output)
        self.assertNotIn(str(harness.state), output)
        self.assertNotIn("结果码=bundle_digest_mismatch", output)
        self.assertTrue(any(call["kind"] == "bundle-install" for call in self.calls()))
        self.assertNotIn("bundle_digest_mismatch", "\n".join(messages))

    def test_download_bundle_writes_embedded_index_as_private_copy(self):
        """缺附件时写出的同名副本必须 0600 且逐字节等于内嵌索引，后续核对按同一份文件。"""
        harness = self.harness
        harness.use_real_package(index_asset=False)
        manifest = json.loads((harness.manifests / (harness.target_tag + ".json")).read_text())
        directory = harness.state / "download"
        directory.mkdir(mode=0o700)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            package = AGENT._download_bundle(
                harness.config,
                harness.target_tag,
                manifest["control_bundle"],
                harness.state,
                directory,
            )
        self.assertEqual(package, directory / "lingxi-control.tar")
        self.assertIn("阶段=bundle 结果码=index_verified", output.getvalue())
        self.assertIn("index_source=embedded", output.getvalue())
        self.assertNotIn(str(directory), output.getvalue())
        copy = directory / "control-index.json"
        self.assertEqual(copy.read_bytes(), harness.index.read_bytes())
        self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o600)
        self.assertEqual(
            sorted(p.name for p in directory.iterdir()),
            ["control-index.json", "lingxi-control.tar"],
        )

    def test_index_asset_identical_to_manifest_is_used_as_is(self):
        harness = self.harness
        harness.use_real_package(index_asset=True)
        harness.state_for_old()
        code, output = self.run_agent(sender=lambda message, env, timeout: None)
        self.assertEqual(code, 0)
        self.assertEqual(self.read_state()["last_result"], "verified")
        self.assertEqual(output.count("阶段=bundle 结果码=index_verified"), 2)
        self.assertEqual(output.count("index_source=asset"), 2)
        self.assertNotIn("index_source=embedded", output)

    def test_index_asset_differing_from_manifest_is_refused_before_bundle_tool(self):
        harness = self.harness
        harness.use_real_package(index_asset=True)
        # 清单已按原索引钉住摘要；附件随后被换成另一份内容。
        harness.index.write_bytes(b'{"synthetic":true,"tampered":true}\n')
        harness.state_for_old()
        messages = []
        code, output = self.run_agent(sender=lambda message, env, timeout: messages.append(message))
        self.assertEqual(code, 0)
        self.assertEqual(self.read_state()["last_result"], "bundle_digest_mismatch")
        self.assertIn("阶段=bundle 结果码=bundle_digest_mismatch", output)
        self.assertNotIn("index_source=", output)
        self.assertEqual(len(messages), 1)
        self.assertIn("bundle_digest_mismatch", messages[0])
        # 摘要不符在调用控制包入口之前就拒绝，不靠工具二次发现。
        self.assertFalse(
            any(call["kind"] in {"bundle-verify", "bundle-install"} for call in self.calls())
        )

    def test_missing_index_asset_with_tampered_embedded_index_is_refused(self):
        harness = self.harness
        harness.use_real_package(b'{"synthetic":true,"tampered":true}\n', index_asset=False)
        harness.state_for_old()
        messages = []
        code, output = self.run_agent(sender=lambda message, env, timeout: messages.append(message))
        self.assertEqual(code, 0)
        self.assertEqual(self.read_state()["last_result"], "bundle_digest_mismatch")
        self.assertIn("阶段=bundle 结果码=bundle_digest_mismatch", output)
        self.assertNotIn("index_source=", output)
        self.assertEqual(len(messages), 1)
        self.assertFalse(
            any(call["kind"] in {"bundle-verify", "bundle-install"} for call in self.calls())
        )

    def test_missing_index_asset_with_non_tar_package_is_refused(self):
        """附件缺失且包根本不是 tar（或没有内嵌索引成员）：仍是摘要不符，不抛裸异常。"""
        harness = self.harness
        harness.set_env(INDEX_ASSET_MODE="absent")
        harness.state_for_old()
        code, output = self.run_agent(sender=lambda message, env, timeout: None)
        self.assertEqual(code, 0)
        self.assertEqual(self.read_state()["last_result"], "bundle_digest_mismatch")
        self.assertIn("阶段=bundle 结果码=bundle_digest_mismatch", output)

    def test_bundle_digest_mismatch_then_retry_is_not_skipped(self):
        """探针场景：v2.4.3 已验证且摘要已记、容器仍跑 v2.4.3，rc.10 的附件被篡改。

        修复前第一轮把 ``target_tag`` 记成 rc.10 而 ``deployer_state`` / ``verified_digests``
        仍是 v2.4.3 的，第二轮状态账级幂等就把 rc.10 误判成 ``already_in_place`` 并把最高已
        部署版本抬到 rc.10：新目标从此被静默跳过，且防降级会拿失真的版本号拒绝真正的目标。
        """
        harness = self.harness
        harness.state_for_verified_target(harness.old_tag)
        harness.set_running_containers(harness.old_tag)
        harness.set_env(DOCKER_MODE="records")
        original = harness.asset.read_bytes()
        harness.asset.write_bytes(b"tampered package")
        messages = []

        def sender(message, env, timeout):
            messages.append(message)

        for round_number in (1, 2):
            harness.log.unlink(missing_ok=True)
            code, output = self.run_agent(sender=sender)
            self.assertEqual(code, 0, round_number)
            self.assertIn("阶段=bundle 结果码=bundle_digest_mismatch", output)
            self.assertNotIn("结果码=already_in_place", output)
            state = self.read_state()
            self.assertEqual(state["last_result"], "bundle_digest_mismatch")
            self.assertEqual(state["consecutive_failures"], round_number)
            # 状态账仍指向已在位的 v2.4.3：目标、部署器状态、最高已部署版本、已验证摘要都不动。
            self.assertEqual(state["target_tag"], harness.old_tag)
            self.assertEqual(state["deployer_state"], "verified")
            self.assertEqual(state["highest_deployed_tag"], harness.old_tag)
            self.assertEqual(state["verified_digests"], harness.expected_digests(harness.old_tag))
            self.assertEqual(state["plan_id"], "old-plan")
            # 每轮都重新下载核对，而不是只读回读容器就收口；篡改包不会走到部署器。
            self.assertTrue(
                any(call["kind"] == "gh" and "download" in call["argv"] for call in self.calls())
            )
            self.assertFalse(any(call["kind"] == "deployer" for call in self.calls()))
        self.assertEqual(len(messages), 1)
        # 附件修好后同一目标正常部署，状态账才推进到 rc.10，并发一条恢复。
        harness.asset.write_bytes(original)
        harness.log.unlink()
        code, output = self.run_agent(sender=sender)
        self.assertEqual(code, 0)
        self.assertNotIn("结果码=already_in_place", output)
        state = self.read_state()
        self.assertEqual(state["last_result"], "verified")
        self.assertEqual(state["target_tag"], harness.target_tag)
        self.assertEqual(state["highest_deployed_tag"], harness.target_tag)
        self.assertEqual(state["verified_digests"], harness.expected_digests(harness.target_tag))
        self.assertTrue(
            any(call["kind"] == "deployer" and "apply" in call["argv"] for call in self.calls())
        )
        self.assertEqual(len(messages), 3)
        self.assertIn("recovered", messages[1])
        self.assertIn("verified", messages[2])

    def test_failed_or_refused_rounds_never_advance_deployed_state(self):
        """任何失败、拒绝或未知结果都不把状态账推进到未验证的目标。

        基线：v2.4.3 已验证且摘要已记，容器仍跑 v2.4.3，新目标 rc.10 到来。第一轮按各结果码
        注入故障：``highest_deployed_tag`` / ``verified_digests`` 必须原样，且状态账绝不会同时
        声称「目标 == rc.10 且 verified」；计划落盘前的失败连 ``target_tag`` / ``plan_id`` 也不动，
        计划落盘后的失败只记可接续身份。第二轮排除故障后同一目标必须真正走到 apply 并 ``verified``，
        而不是被状态账级幂等判成 ``already_in_place``。
        """

        def both_releases(h: PullHarness) -> list[dict]:
            return [h._release(h.old_tag, False), h._release(h.target_tag, True)]

        def set_hooks(h: PullHarness, programs: list[str]):
            config = dict(h.config)
            config["pre_apply_hooks"] = programs
            h.config_path = _json(h.config_path, config)

        def start_patch(saved: dict, target: str, **kwargs):
            saved["patch"] = patch.object(AGENT, target, **kwargs)
            saved["patch"].start()
            # 某一例在排除故障前断言失败时，补丁也不能漏到后面的用例；重复 stop 是空操作。
            self.addCleanup(saved["patch"].stop)

        def hide_plan_and_old_release(h: PullHarness, saved: dict):
            # 旧计划文件丢失且旧版本不在 Release 列表：运行容器反查不到当前版本。
            (h.state / "old-plan.plan.json").unlink()
            h.set_releases([h._release(h.target_tag, True)])

        def hide_target_manifest(h: PullHarness, saved: dict):
            (h.manifests / (h.target_tag + ".json")).rename(h.manifests / "hidden.json")

        def restore_target_manifest(h: PullHarness, saved: dict):
            (h.manifests / "hidden.json").rename(h.manifests / (h.target_tag + ".json"))

        def tamper_asset(h: PullHarness, saved: dict):
            saved["asset"] = h.asset.read_bytes()
            h.asset.write_bytes(b"tampered package")

        def break_installed_manifest(h: PullHarness, saved: dict):
            saved["source"] = h.installed_manifest.read_text(encoding="utf-8")
            _exec(h.installed_manifest, "#!/usr/bin/env python3\nraise SystemExit(1)\n")

        def break_public_config(h: PullHarness, saved: dict):
            _write(h.public_config, "{not json", 0o644)

        def restore_public_config(h: PullHarness, saved: dict):
            _json(
                h.public_config,
                {"schema": 1, "values": {}, "files": {"scheduler": {}, "worker": {}}},
                0o644,
            )

        def unsafe_hook(h: PullHarness, saved: dict):
            # 临时目录里的钩子程序不是 root 属主：一个都不执行。
            set_hooks(h, [str(h.hook_fail)])

        def failing_hook(h: PullHarness, saved: dict):
            set_hooks(h, [str(h.hook_fail)])
            start_patch(saved, "_hook_program_protected", return_value=True)

        def clear_failing_hook(h: PullHarness, saved: dict):
            saved["patch"].stop()
            set_hooks(h, [])

        def apply_timeout(h: PullHarness, saved: dict):
            config = dict(h.config)
            config["deploy_timeout_seconds"] = 1
            h.config_path = _json(h.config_path, config)
            h.set_env(DEPLOY_MODE="timeout")

        def status_verified_with_new_approval(h: PullHarness, saved: dict):
            h.set_env(DEPLOY_STATUS="verified")
            # 假部署器把首次 apply 的批准指纹钉在状态目录；failed 后重新 plan 会换批准。
            (h.state / ".fake-approval-sha").unlink()

        # (名称, 结果码, 阶段, 计划已落盘, 注入故障, 排除故障)
        cases = [
            (
                "release_list_unavailable",
                "release_list_unavailable",
                "release_list",
                False,
                lambda h, s: h.set_releases([]),
                lambda h, s: h.set_releases(both_releases(h)),
            ),
            (
                "downgrade_refused",
                "downgrade_refused",
                "version_order",
                False,
                lambda h, s: h.set_releases(
                    [h._release(h.old_tag, False), h._release("v2.4.3-rc.9", True)]
                ),
                lambda h, s: h.set_releases(both_releases(h)),
            ),
            (
                "unknown_idempotence_docker_daemon",
                "unknown",
                "idempotence",
                False,
                lambda h, s: h.set_env(DOCKER_MODE="daemon"),
                lambda h, s: h.set_env(DOCKER_MODE="records"),
            ),
            (
                "unknown_idempotence_manifest_unresolvable",
                "unknown",
                "idempotence",
                False,
                hide_target_manifest,
                restore_target_manifest,
            ),
            (
                "unknown_current_release",
                "unknown",
                "current_release",
                False,
                hide_plan_and_old_release,
                lambda h, s: h.set_releases(both_releases(h)),
            ),
            (
                "bundle_digest_mismatch",
                "bundle_digest_mismatch",
                "bundle",
                False,
                tamper_asset,
                lambda h, s: h.asset.write_bytes(s["asset"]),
            ),
            (
                "unknown_bundle_download_failed",
                "unknown",
                "bundle",
                False,
                lambda h, s: h.set_env(ASSET_FILE=str(h.root / "missing.tar")),
                lambda h, s: h.set_env(ASSET_FILE=str(h.asset)),
            ),
            (
                "unknown_frozen_manifest",
                "unknown",
                "frozen_manifest",
                False,
                break_installed_manifest,
                lambda h, s: _exec(h.installed_manifest, s["source"]),
            ),
            (
                "installation_receipt_unusable",
                "installation_receipt_unusable",
                "installation_receipt",
                False,
                lambda h, s: h.install_receipt(
                    checks=dict(h.receipt()["checks"], sudo_policy=False)
                ),
                lambda h, s: h.install_receipt(),
            ),
            (
                "unknown_plan",
                "unknown",
                "plan",
                False,
                break_public_config,
                restore_public_config,
            ),
            (
                "unknown_approval",
                "unknown",
                "approval",
                True,
                lambda h, s: start_patch(
                    s, "_make_approval", side_effect=AGENT.AgentError("plan_expired")
                ),
                lambda h, s: s["patch"].stop(),
            ),
            (
                "pre_apply_hook_unsafe",
                "pre_apply_hook_unsafe",
                "pre_apply_hook",
                True,
                unsafe_hook,
                lambda h, s: set_hooks(h, []),
            ),
            (
                "pre_apply_hook_failed",
                "pre_apply_hook_failed",
                "pre_apply_hook",
                True,
                failing_hook,
                clear_failing_hook,
            ),
            (
                "deploy_timeout",
                "deploy_timeout",
                "apply",
                True,
                apply_timeout,
                lambda h, s: h.set_env(DEPLOY_MODE="normal"),
            ),
            (
                "failed",
                "failed",
                "status",
                True,
                lambda h, s: h.set_env(DEPLOY_STATUS="failed"),
                status_verified_with_new_approval,
            ),
            (
                "unknown_status",
                "unknown",
                "status",
                True,
                lambda h, s: h.set_env(DEPLOY_STATUS="bogus"),
                lambda h, s: h.set_env(DEPLOY_STATUS="verified"),
            ),
            (
                "running",
                "running",
                "status",
                True,
                lambda h, s: h.set_env(DEPLOY_STATUS="running"),
                lambda h, s: h.set_env(DEPLOY_STATUS="verified"),
            ),
        ]
        for name, result, stage, checkpointed, inject, clear in cases:
            with self.subTest(case=name):
                self.harness.close()
                harness = self.harness = PullHarness()
                self.addCleanup(harness.close)
                harness.state_for_verified_target(harness.old_tag)
                harness.set_running_containers(harness.old_tag)
                harness.set_releases(both_releases(harness))
                harness.set_env(DOCKER_MODE="records")
                messages = []
                saved: dict = {}
                inject(harness, saved)
                code, output = self.run_agent(
                    sender=lambda message, env, timeout: messages.append(message)
                )
                self.assertEqual(code, 0)
                self.assertIn(f"阶段={stage} 结果码={result}", output)
                self.assertNotIn("结果码=already_in_place", output)
                state = self.read_state()
                self.assertEqual(state["last_result"], result)
                self.assertEqual(state["highest_deployed_tag"], harness.old_tag)
                self.assertEqual(
                    state["verified_digests"], harness.expected_digests(harness.old_tag)
                )
                # 核心不变量：没写出 verified 的目标，状态账不会同时说「目标 == 它且 verified」。
                self.assertFalse(
                    state["target_tag"] == harness.target_tag
                    and state["deployer_state"] == "verified"
                )
                if checkpointed:
                    # 计划落盘后只记可接续身份：新目标、新计划、非 verified 的部署器状态。
                    self.assertEqual(state["target_tag"], harness.target_tag)
                    self.assertNotEqual(state["plan_id"], "old-plan")
                    self.assertNotEqual(state["deployer_state"], "verified")
                else:
                    self.assertEqual(state["target_tag"], harness.old_tag)
                    self.assertEqual(state["plan_id"], "old-plan")
                    self.assertFalse(any(call["kind"] == "deployer" for call in self.calls()))
                clear(harness, saved)
                harness.log.unlink(missing_ok=True)
                code, output = self.run_agent(
                    sender=lambda message, env, timeout: messages.append(message)
                )
                self.assertEqual(code, 0)
                self.assertNotIn("结果码=already_in_place", output)
                state = self.read_state()
                self.assertEqual(state["last_result"], "verified")
                self.assertEqual(state["target_tag"], harness.target_tag)
                self.assertEqual(state["deployer_state"], "verified")
                self.assertEqual(state["highest_deployed_tag"], harness.target_tag)
                self.assertEqual(
                    state["verified_digests"], harness.expected_digests(harness.target_tag)
                )
                self.assertTrue(
                    any(
                        call["kind"] == "deployer" and "apply" in call["argv"]
                        for call in self.calls()
                    )
                )
                self.assertIn("verified", messages[-1])

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

    def test_stale_or_tampered_approval_is_rewritten_not_reused(self):
        self.harness.state_for_old()
        self.harness.set_env(DEPLOY_STATUS="running")
        first_code, _ = self.run_agent()
        self.assertEqual(first_code, 0)
        approval_path = next(self.harness.state.glob("*.approval.json"))
        plan_path = self.harness.state / (
            json.loads(approval_path.read_text())["plan_id"] + ".plan.json"
        )
        plan = json.loads(plan_path.read_text())
        good_approval = json.loads(approval_path.read_text())
        # 假部署器把首个批准指纹记在标记文件里；此处只验证代理侧重写，先移除标记。
        marker = self.harness.state / ".fake-approval-sha"
        tampered = dict(good_approval)
        tampered["plan_sha256"] = "f" * 64
        stale = dict(good_approval)
        stale["expires_at"] = time_value() - 5
        for name, broken in (("tampered", tampered), ("stale", stale)):
            with self.subTest(name=name):
                _json(approval_path, broken)
                marker.unlink(missing_ok=True)
                with self.assertRaises(AGENT.AgentError):
                    AGENT.validate_approval(plan, broken)
                code, output = self.run_agent()
                self.assertEqual(code, 0)
                self.assertIn("approval_written", output)
                self.assertNotIn("approval_reused", output)
                rewritten = json.loads(approval_path.read_text())
                self.assertNotEqual(rewritten, broken)
                self.assertEqual(rewritten["plan_sha256"], AGENT.fingerprint(plan))
                AGENT.validate_approval(plan, rewritten)
                self.assertEqual(self.read_state()["approval_sha256"], AGENT.fingerprint(rewritten))
                self.assertEqual(self.read_state()["plan_id"], plan["id"])
        deployer = [call for call in self.calls() if call["kind"] == "deployer"]
        self.assertEqual(sum("plan" in call["argv"] for call in deployer), 1)
        self.assertEqual(sum("apply" in call["argv"] for call in deployer), 3)

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

    def test_external_in_place_then_next_version_takes_old_from_running_containers(self):
        # 第一轮：状态账仍指向 new.tag == v2.4.3 的旧计划，容器却已被外部装到 rc.10。
        self.harness.state_for_old()
        self.harness.set_env(DOCKER_MODE="match", DEPLOY_STATUS="running")
        messages = []
        code, _ = self.run_agent(sender=lambda message, env, timeout: messages.append(message))
        self.assertEqual(code, 0)
        state = self.read_state()
        self.assertEqual(state["last_result"], "already_in_place_external")
        self.assertEqual(state["deployer_state"], "verified")
        self.assertEqual(state["target_tag"], self.harness.target_tag)
        self.assertEqual(state["plan_id"], "old-plan")
        self.assertFalse(list(self.harness.state.glob("*.request.json")))
        # 第二轮：rc.11 到来，容器仍运行 rc.10；old 必须是 rc.10，不是旧计划的 v2.4.3。
        next_tag = "v2.5.0-rc.11"
        self.harness._make_manifest(next_tag, formal=False, seed="5")
        self.harness.set_releases(
            [
                self.harness._release(self.harness.old_tag, False),
                self.harness._release(self.harness.target_tag, True),
                self.harness._release(next_tag, True),
            ]
        )
        code, _ = self.run_agent(sender=lambda message, env, timeout: messages.append(message))
        self.assertEqual(code, 0)
        request = json.loads(next(self.harness.state.glob("*.request.json")).read_text())
        self.assertEqual(request["new"]["tag"], next_tag)
        self.assertEqual(request["old"]["tag"], self.harness.target_tag)
        self.assertEqual(request["current_heads"], request["old"]["migration_heads"])
        self.assertEqual(
            request["recovery"]["target_manifest_sha256"], AGENT.fingerprint(request["old"])
        )
        self.assertNotEqual(
            request["recovery"]["target_manifest_sha256"], AGENT.fingerprint(request["new"])
        )
        self.assertEqual(self.read_state()["target_tag"], next_tag)
        self.assertEqual(self.read_state()["last_result"], "running")
        self.assertEqual(messages, [])

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
        # 临时目录里的钩子过不了属主核对；本用例只看钩子自身失败，属主核对另有用例。
        with patch.object(AGENT, "_hook_program_protected", return_value=True):
            code, _ = self.run_agent(sender=lambda message, env, timeout: messages.append(message))
        self.assertEqual(code, 0)
        self.assertEqual(self.read_state()["last_result"], "pre_apply_hook_failed")
        self.assertEqual(len(messages), 1)
        self.assertTrue(any(call["kind"] == "hook" for call in self.calls()))
        self.assertFalse(
            any(call["kind"] == "deployer" and "apply" in call["argv"] for call in self.calls())
        )

    def test_hook_program_must_be_root_owned_and_unwritable(self):
        hook = self.harness.hook_fail
        # 临时目录：程序属主是当前用户，且祖先目录可被其他主体写入，两条都不许。
        self.assertFalse(AGENT._hook_program_protected(str(hook)))
        self.harness.state_for_old()
        config = dict(self.harness.config)
        config["pre_apply_hooks"] = [str(hook)]
        self.harness.config_path = _json(self.harness.config_path, config)
        messages = []
        code, _ = self.run_agent(sender=lambda message, env, timeout: messages.append(message))
        self.assertEqual(code, 0)
        state = self.read_state()
        self.assertEqual(state["last_result"], "pre_apply_hook_unsafe")
        self.assertEqual(state["deployer_state"], "planned")
        self.assertEqual(len(messages), 1)
        self.assertIn("pre_apply_hook_unsafe", messages[0])
        self.assertFalse(any(call["kind"] == "hook" for call in self.calls()))
        self.assertFalse(
            any(call["kind"] == "deployer" and "apply" in call["argv"] for call in self.calls())
        )
        self.assertTrue(list(self.harness.state.glob("*.plan.json")))

        # 打桩 lstat：整条路径 root 属主、0755、非链接才放行；任一环节有缺陷都拒绝。
        chain = {str(item) for item in (hook, *hook.parents)}
        real_lstat = os.lstat

        def stub(defect: str | None):
            def fake_lstat(path, *args, **kwargs):
                info = real_lstat(path, *args, **kwargs)
                if str(path) not in chain:
                    return info
                kind = stat.S_IFDIR if stat.S_ISDIR(info.st_mode) else stat.S_IFREG
                mode, uid = kind | 0o755, 0
                if str(path) == str(hook.parent):
                    if defect == "symlink":
                        mode = stat.S_IFLNK | 0o777
                    elif defect == "group_writable":
                        mode = kind | 0o775
                    elif defect == "other_writable":
                        mode = kind | 0o757
                    elif defect == "not_root":
                        uid = os.getuid() + 1
                return os.stat_result(
                    (mode, info.st_ino, info.st_dev, info.st_nlink, uid, 0, info.st_size, 0, 0, 0)
                )

            return fake_lstat

        with patch.object(AGENT.os, "lstat", side_effect=stub(None)):
            self.assertTrue(AGENT._hook_program_protected(str(hook)))
        for defect in ("symlink", "group_writable", "other_writable", "not_root"):
            with self.subTest(defect=defect):
                with patch.object(AGENT.os, "lstat", side_effect=stub(defect)):
                    self.assertFalse(AGENT._hook_program_protected(str(hook)))
        self.assertFalse(AGENT._hook_program_protected(str(hook.parent / "missing-hook.py")))

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
        outputs = []
        for _ in range(3):
            code, output = self.run_agent(
                sender=lambda message, env, timeout: messages.append(message)
            )
            self.assertEqual(code, 0)
            outputs.append(output)
        self.assertEqual(len(messages), 1)
        # 同键第二、三轮被去重：日志不得再记 sent，把「没发」写成「已发」。
        self.assertIn("阶段=alert 结果码=sent", outputs[0])
        for output in outputs[1:]:
            self.assertNotIn("结果码=sent", output)
            self.assertIn("阶段=alert 结果码=deduplicated", output)
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

    def receipt_calls(self) -> list[str]:
        return [call["result"] for call in self.calls() if call["kind"] == "receipt-check"]

    def latest_plan(self) -> dict:
        approval = json.loads(next(self.harness.state.glob("pull-*.approval.json")).read_text())
        return json.loads((self.harness.state / (approval["plan_id"] + ".plan.json")).read_text())

    def test_installation_receipt_bundle_sha_is_refreshed_before_plan(self):
        harness = self.harness
        prev_path = harness.receipt_path.with_name(harness.receipt_path.name + ".prev")
        target_sha = harness.bundle_sha256(harness.target_tag)
        self.assertNotEqual(target_sha, harness.bootstrap_bundle_sha256)
        messages = []

        def sender(message, env, timeout):
            messages.append(message)

        def assert_refreshed(output: str):
            # 假部署器按 deploy_runtime.installation_receipt 的六条校验放行：不刷新就会在
            # 这里得到 installation_receipt_missing_or_mismatched。
            self.assertEqual(self.receipt_calls(), ["ok"])
            state = self.read_state()
            self.assertEqual(state["last_result"], "verified")
            self.assertEqual(state["target_tag"], harness.target_tag)
            receipt = harness.read_receipt()
            self.assertEqual(receipt, harness.receipt(target_sha))
            self.assertEqual(stat.S_IMODE(harness.receipt_path.stat().st_mode), 0o600)
            self.assertEqual(prev_path.read_bytes(), AGENT.canonical(harness.receipt()))
            self.assertEqual(stat.S_IMODE(prev_path.stat().st_mode), 0o600)
            # 刷新发生在新包装好之后、plan 之前。
            self.assertLess(
                output.index("阶段=installation_receipt 结果码=refreshed"),
                output.index("阶段=plan 结果码=written"),
            )
            kinds = [call["kind"] for call in self.calls()]
            self.assertLess(kinds.index("bundle-install"), kinds.index("receipt-check"))
            plan = self.latest_plan()
            self.assertEqual(
                plan["channel"]["installation_receipt_sha256"], AGENT.fingerprint(receipt)
            )
            self.assertEqual(plan["channel"]["binding_version"], receipt["binding_version"])

        with self.subTest(case="first_deployment_without_previous_plan_reads_host_materials"):
            # 引导安装后的第一次部署：没有旧计划，通道事实由 _host_materials 从磁盘收集。
            harness.set_releases(
                [
                    harness._release(harness.old_tag, False),
                    harness._release(harness.target_tag, True),
                ]
            )
            harness.set_running_containers(harness.old_tag)
            harness.set_env(DOCKER_MODE="records")
            code, output = self.run_agent(sender=sender)
            self.assertEqual(code, 0)
            assert_refreshed(output)
            self.assertEqual(self.latest_plan()["channel"]["relay_sha256"], "f" * 64)
        with self.subTest(case="next_version_with_previous_plan_rebinds_receipt_fingerprint"):
            # 有旧计划时通道沿用旧计划，但收据指纹与绑定版本必须来自刚刷新的收据。
            for path in (harness.log, harness.state / "pull-agent.json", prev_path):
                path.unlink()
            (harness.state / ".fake-approval-sha").unlink()
            for path in harness.state.glob("pull-*"):
                path.unlink()
            harness.install_receipt()
            harness.state_for_old()
            harness.set_env(DOCKER_MODE="mismatch")
            code, output = self.run_agent(sender=sender)
            self.assertEqual(code, 0)
            assert_refreshed(output)
            plan = self.latest_plan()
            self.assertTrue(plan["channel"]["synthetic"])
            self.assertNotEqual(
                plan["channel"]["installation_receipt_sha256"],
                AGENT.fingerprint(harness.receipt()),
            )
        with self.subTest(case="same_bundle_sha_is_left_unchanged_without_backup"):
            harness.log.unlink()
            prev_path.unlink()
            (harness.state / ".fake-approval-sha").unlink()
            before = harness.receipt_path.stat()
            state = self.read_state()
            state["deployer_state"] = "failed"
            harness.save_state(state)
            harness.set_env(DOCKER_MODE="records")
            code, output = self.run_agent(sender=sender)
            self.assertEqual(code, 0)
            self.assertIn("阶段=installation_receipt 结果码=unchanged", output)
            self.assertEqual(harness.receipt_path.stat().st_mtime_ns, before.st_mtime_ns)
            self.assertEqual(harness.receipt_path.stat().st_ino, before.st_ino)
            self.assertFalse(prev_path.exists())
            self.assertEqual(self.receipt_calls(), ["ok"])
            self.assertEqual(self.read_state()["last_result"], "verified")
        self.assertFalse(list(harness.config_root.glob(".pending-*")))
        self.assertTrue(all("verified" in message for message in messages))

    def test_receipt_with_false_checks_or_wrong_environment_is_not_touched(self):
        harness = self.harness
        prev_path = harness.receipt_path.with_name(harness.receipt_path.name + ".prev")
        messages = []

        def sender(message, env, timeout):
            messages.append(message)

        broken = {
            "one_check_false": dict(
                checks=dict(harness.receipt()["checks"], container_peer_rejected=False)
            ),
            "check_missing": dict(
                checks={
                    name: True
                    for name in AGENT.INSTALLATION_RECEIPT_CHECKS
                    if name != "sudo_policy"
                }
            ),
            "wrong_environment": dict(environment="production"),
            "wrong_project": dict(project="another-project"),
            "binding_version_missing": dict(binding_version=None),
            "extra_key": dict(installed_by="someone"),
            "wrong_schema": dict(schema=2),
        }
        for name, overrides in broken.items():
            with self.subTest(case=name):
                harness.log.unlink(missing_ok=True)
                prev_path.unlink(missing_ok=True)
                harness.state_for_old()
                receipt = harness.install_receipt(**overrides)
                if name == "binding_version_missing":
                    self.assertIsNone(receipt["binding_version"])
                before = harness.receipt_path.stat()
                raw = harness.receipt_path.read_bytes()
                messages.clear()
                for round_number in (1, 2):
                    code, output = self.run_agent(sender=sender)
                    self.assertEqual(code, 0, round_number)
                    self.assertIn(
                        "阶段=installation_receipt 结果码=installation_receipt_unusable", output
                    )
                    state = self.read_state()
                    self.assertEqual(state["last_result"], "installation_receipt_unusable")
                    self.assertEqual(state["consecutive_failures"], round_number)
                    # 状态账仍指向已在位的旧目标，不把没部署的目标记成在位或已验证。
                    self.assertEqual(state["target_tag"], harness.old_tag)
                    self.assertEqual(state["deployer_state"], "verified")
                    self.assertEqual(state["plan_id"], "old-plan")
                # 零写入：收据字节、修改时间、inode 都不变，没有备份、没有临时文件。
                self.assertEqual(harness.receipt_path.read_bytes(), raw)
                self.assertEqual(harness.receipt_path.stat().st_mtime_ns, before.st_mtime_ns)
                self.assertEqual(harness.receipt_path.stat().st_ino, before.st_ino)
                self.assertFalse(prev_path.exists())
                self.assertFalse(list(harness.config_root.glob(".pending-*")))
                # 不 plan、不 apply；告警一次，去重键含结果码与目标 tag。
                self.assertFalse(any(call["kind"] == "deployer" for call in self.calls()))
                self.assertFalse(list(harness.state.glob("*.request.json")))
                self.assertTrue(any(call["kind"] == "bundle-install" for call in self.calls()))
                self.assertEqual(len(messages), 1)
                self.assertIn("installation_receipt_unusable", messages[0])
                self.assertIn("阶段：installation_receipt", messages[0])
                self.assertIn(
                    f"installation_receipt_unusable:{harness.target_tag}", state["alerts"]
                )
        with self.subTest(case="missing_receipt"):
            harness.log.unlink(missing_ok=True)
            harness.state_for_old()
            harness.receipt_path.unlink()
            messages.clear()
            code, output = self.run_agent(sender=sender)
            self.assertEqual(code, 0)
            self.assertIn("reason=json_file_unavailable", output)
            self.assertEqual(self.read_state()["last_result"], "installation_receipt_unusable")
            self.assertFalse(harness.receipt_path.exists())
            self.assertFalse(prev_path.exists())
            self.assertFalse(any(call["kind"] == "deployer" for call in self.calls()))
        with self.subTest(case="group_readable_receipt_is_rejected_like_the_deployer_does"):
            harness.log.unlink(missing_ok=True)
            harness.state_for_old()
            harness.install_receipt()
            harness.receipt_path.chmod(0o640)
            raw = harness.receipt_path.read_bytes()
            code, output = self.run_agent(sender=sender)
            self.assertEqual(code, 0)
            self.assertIn("reason=json_file_permissions", output)
            self.assertEqual(self.read_state()["last_result"], "installation_receipt_unusable")
            self.assertEqual(harness.receipt_path.read_bytes(), raw)
            self.assertEqual(stat.S_IMODE(harness.receipt_path.stat().st_mode), 0o640)
            self.assertFalse(prev_path.exists())
        with self.subTest(case="repaired_receipt_recovers_and_deploys"):
            harness.log.unlink(missing_ok=True)
            messages.clear()
            harness.install_receipt()
            code, _ = self.run_agent(sender=sender)
            self.assertEqual(code, 0)
            self.assertEqual(self.read_state()["last_result"], "verified")
            self.assertEqual(self.receipt_calls(), ["ok"])
            self.assertTrue(any("recovered" in message for message in messages))
            self.assertEqual(
                harness.read_receipt()["bundle_sha256"], harness.bundle_sha256(harness.target_tag)
            )
        with self.subTest(case="continuing_plan_never_touches_the_receipt"):
            harness.log.unlink(missing_ok=True)
            prev_path.unlink(missing_ok=True)
            (harness.state / ".fake-approval-sha").unlink(missing_ok=True)
            harness.state_for_continuation()
            before = harness.receipt_path.stat()
            raw = harness.receipt_path.read_bytes()
            harness.set_env(DOCKER_MODE="match", DEPLOY_STATUS="running")
            code, output = self.run_agent(sender=sender)
            self.assertEqual(code, 0)
            self.assertNotIn("阶段=installation_receipt", output)
            self.assertEqual(self.read_state()["deployer_state"], "running")
            self.assertEqual(self.receipt_calls(), ["ok"])
            self.assertEqual(harness.receipt_path.read_bytes(), raw)
            self.assertEqual(harness.receipt_path.stat().st_mtime_ns, before.st_mtime_ns)
            self.assertFalse(prev_path.exists())

    def test_receipt_refresh_is_atomic_and_private(self):
        harness = self.harness
        path = harness.receipt_path
        prev_path = path.with_name(path.name + ".prev")
        new_sha = harness.bundle_sha256(harness.target_tag)
        # 收据由人手写：字节形态不必是代理的规范编码，备份也必须逐字节保留原样。
        handwritten = json.dumps(harness.receipt(), ensure_ascii=False, indent=4).encode()
        _write(path, handwritten, 0o600)
        before = path.stat()
        refreshed, changed = AGENT.refresh_installation_receipt(harness.host, new_sha)
        self.assertTrue(changed)
        self.assertEqual(refreshed, harness.receipt(new_sha))
        self.assertEqual(path.read_bytes(), AGENT.canonical(harness.receipt(new_sha)))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        # 原子替换：新内容来自另一个 inode，不是原地截断重写；备份等于旧字节且同样私有。
        self.assertNotEqual(path.stat().st_ino, before.st_ino)
        self.assertEqual(prev_path.read_bytes(), handwritten)
        self.assertEqual(stat.S_IMODE(prev_path.stat().st_mode), 0o600)
        self.assertFalse(list(harness.config_root.glob(".pending-*")))
        # 同一摘要再刷一次：不写盘、不动备份。
        after = path.stat()
        prev_before = prev_path.stat()
        self.assertEqual(
            AGENT.refresh_installation_receipt(harness.host, new_sha), (refreshed, False)
        )
        self.assertEqual(path.stat().st_mtime_ns, after.st_mtime_ns)
        self.assertEqual(prev_path.stat().st_mtime_ns, prev_before.st_mtime_ns)

        # 替换失败时旧收据原样保留、不留半份临时文件；备份写成后主文件替换失败亦然。
        _write(path, handwritten, 0o600)
        prev_path.unlink()
        real_replace = os.replace

        def fail_on(target: Path):
            def fake_replace(source, destination, *args, **kwargs):
                if Path(destination) == target:
                    raise OSError("synthetic replace failure")
                return real_replace(source, destination, *args, **kwargs)

            return fake_replace

        for name, target in (("backup", prev_path), ("receipt", path)):
            with self.subTest(failure=name):
                with patch.object(AGENT.os, "replace", side_effect=fail_on(target)):
                    with self.assertRaises(AGENT.AgentError) as raised:
                        AGENT.refresh_installation_receipt(harness.host, new_sha)
                self.assertEqual(raised.exception.code, "installation_receipt_write_failed")
                self.assertEqual(path.read_bytes(), handwritten)
                self.assertFalse(list(harness.config_root.glob(".pending-*")))
                if name == "backup":
                    self.assertFalse(prev_path.exists())
                else:
                    self.assertEqual(prev_path.read_bytes(), handwritten)
        # 配置根可被组 / 其他主体写入时拒绝写回：与部署器 installation_directory 同一条规则。
        prev_path.unlink()
        harness.config_root.chmod(0o775)
        try:
            with self.assertRaises(AGENT.AgentError) as raised:
                AGENT.refresh_installation_receipt(harness.host, new_sha)
        finally:
            harness.config_root.chmod(0o755)
        self.assertEqual(raised.exception.code, "installation_receipt_write_failed")
        self.assertEqual(path.read_bytes(), handwritten)
        self.assertFalse(prev_path.exists())
        # 整轮视角：写回失败与收据不可用同一结果码，日志给出原因，不 plan。
        harness.state_for_old()
        messages = []
        with patch.object(AGENT.os, "replace", side_effect=fail_on(path)):
            code, output = self.run_agent(
                sender=lambda message, env, timeout: messages.append(message)
            )
        self.assertEqual(code, 0)
        self.assertIn("reason=installation_receipt_write_failed", output)
        self.assertEqual(self.read_state()["last_result"], "installation_receipt_unusable")
        self.assertEqual(path.read_bytes(), handwritten)
        self.assertFalse(any(call["kind"] == "deployer" for call in self.calls()))
        self.assertEqual(len(messages), 1)

    def test_command_argv_runs_non_executable_scripts_with_bytecode_disabled(self):
        """版本目录里的清单工具 / 部署器按包内索引装成 0444：由代理解释器带 -B 运行。"""
        script = _write(self.harness.root / "plain-tool.py", "print('synthetic')\n", 0o644)
        self.assertEqual(
            AGENT._command_argv(script, "resolve", "--tag", "v2.5.0"),
            [sys.executable, "-B", str(script), "resolve", "--tag", "v2.5.0"],
        )
        self.assertEqual(
            AGENT._command_argv(self.harness.gh, "release", "list"),
            [str(self.harness.gh), "release", "list"],
        )

    def test_run_command_always_disables_bytecode_cache_for_children(self):
        """-B 不传子进程：无论 env 是 None 还是显式字典，子进程环境都带 PYTHONDONTWRITEBYTECODE=1。"""
        captured = []

        def fake_run(argv, **kwargs):
            captured.append(kwargs)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with patch.object(AGENT.subprocess, "run", side_effect=fake_run):
            AGENT._run_command(["/bin/true"], timeout=1, env=None)
            AGENT._run_command(
                ["/bin/true"], timeout=1, env={"LINGXI_GH_COMMAND": "/x/gh", "PATH": "/usr/bin"}
            )
        inherited, explicit = captured
        self.assertEqual(inherited["env"]["PYTHONDONTWRITEBYTECODE"], "1")
        # env=None 仍以进程环境为底：夹具写进 os.environ 的键原样可见。
        self.assertEqual(inherited["env"]["CALL_LOG"], str(self.harness.log))
        self.assertEqual(explicit["env"]["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(explicit["env"]["LINGXI_GH_COMMAND"], "/x/gh")
        self.assertEqual(explicit["env"]["PATH"], "/usr/bin")
        # 显式字典只补这一个键，不把进程环境的其他键夹带进去。
        self.assertEqual(
            set(explicit["env"]), {"PYTHONDONTWRITEBYTECODE", "LINGXI_GH_COMMAND", "PATH"}
        )
        # 真实子进程：即使本进程环境里没有这个变量，子解释器也已被禁写字节码缓存。
        with patch.dict(os.environ, clear=False):
            os.environ.pop("PYTHONDONTWRITEBYTECODE", None)
            raw = AGENT._run_command(
                [sys.executable, "-c", "import sys; print(sys.dont_write_bytecode)"],
                timeout=30,
                env=None,
            )
        self.assertEqual(raw.strip(), "True")

    def test_agent_process_never_writes_bytecode_cache(self):
        """代理模块顶部的开关不依赖 -B：导入后进程内加载控制包工具不在源码旁留 __pycache__。"""
        self.assertTrue(sys.dont_write_bytecode)
        root = self.harness.root / "bytecode-probe"
        (root / "agent").mkdir(parents=True)
        (root / "tool").mkdir()
        agent_copy = root / "agent" / "release_pull_agent.py"
        tool_copy = root / "tool" / "control_bundle.py"
        shutil.copyfile(MODULE_PATH, agent_copy)
        shutil.copyfile(ROOT / "deploy/control_bundle.py", tool_copy)
        # 先把开关拨回假（模拟不带 -B 启动的解释器），再重新执行代理模块的顶层代码。
        with (
            patch.object(sys, "dont_write_bytecode", False),
            patch.object(sys, "pycache_prefix", None),
        ):
            probe_spec = importlib.util.spec_from_file_location(
                "release_pull_agent_probe", agent_copy
            )
            probe = importlib.util.module_from_spec(probe_spec)
            assert probe_spec.loader is not None
            probe_spec.loader.exec_module(probe)
            # 探针有效性：开关为假时加载代理副本本身会留下缓存，说明不是解释器替我们挡住的。
            self.assertTrue((root / "agent" / "__pycache__").is_dir())
            self.assertTrue(sys.dont_write_bytecode)
            module = probe._load_module(tool_copy)
            self.assertIsNotNone(module)
            self.assertTrue(callable(module.verify))
        self.assertFalse((root / "tool" / "__pycache__").exists())

    def test_child_processes_never_write_bytecode_cache_into_bundle(self):
        """预发实读：清单工具子进程以 root 在版本目录里加载同目录模块，此前留下 __pycache__，
        下一轮 verify_install 就以「存在可写项」拒绝。装成 0444（生产形态）与可执行两种都不留。"""
        for executable in (False, True):
            with self.subTest(executable=executable):
                self.harness.close()
                harness = self.harness = PullHarness()
                self.addCleanup(harness.close)
                mode = 0o755 if executable else 0o444
                harness.installed_manifest.chmod(mode)
                harness.installed_deployer.chmod(mode)
                harness.state_for_old()
                with patch.dict(os.environ, clear=False):
                    os.environ.pop("PYTHONDONTWRITEBYTECODE", None)
                    code, _ = self.run_agent(sender=lambda message, env, timeout: None)
                self.assertEqual(code, 0)
                self.assertEqual(self.read_state()["last_result"], "verified")
                children = [
                    call
                    for call in self.calls()
                    if call["kind"] in {"gh", "manifest", "installed-manifest", "deployer"}
                ]
                self.assertTrue(any(call["kind"] == "installed-manifest" for call in children))
                self.assertTrue(any(call["kind"] == "deployer" for call in children))
                self.assertTrue(all(call["no_bytecode"] == "1" for call in children), children)
                self.assertTrue(
                    all(
                        call["sibling"] == 1
                        for call in children
                        if call["kind"] == "installed-manifest"
                    )
                )
                self.assertEqual([p for p in harness.bundle_root.rglob("__pycache__")], [])
                self.assertEqual([p for p in harness.bundle_root.rglob("*.pyc")], [])

    def test_deployer_entry_never_writes_bytecode_cache_without_b_flag(self):
        """部署器入口自己也关缓存：有人不带 -B 手工在版本目录里跑它，同样不留 __pycache__。"""
        root = self.harness.root / "deployer-probe"
        (root / "deploy").mkdir(parents=True)
        (root / "scripts/ci").mkdir(parents=True)
        for name in (
            "deploy/lingxi_deploy.py",
            "deploy/deploy_runtime.py",
            "deploy/deploy_state.py",
            "deploy/control_bundle.py",
            "scripts/ci/release_manifest.py",
        ):
            shutil.copyfile(ROOT / name, root / name)
        env = {key: value for key, value in os.environ.items() if key != "PYTHONDONTWRITEBYTECODE"}
        env.pop("PYTHONPYCACHEPREFIX", None)
        result = subprocess.run(
            [sys.executable, str(root / "deploy/lingxi_deploy.py"), "--help"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(root.rglob("__pycache__")), [])
        self.assertEqual(sorted(root.rglob("*.pyc")), [])

    def test_bundle_install_refusal_reason_goes_to_journal_only(self):
        """控制包入口拒绝安装（如版本目录里多出可写项）：journal 行带 reason，状态账与告警不带。"""
        harness = self.harness
        harness.state_for_old()
        harness.set_env(BUNDLE_INSTALL_MODE="refuse")
        messages = []
        code, output = self.run_agent(sender=lambda message, env, timeout: messages.append(message))
        self.assertEqual(code, 0)
        lines = [line for line in output.splitlines() if "阶段=bundle 结果码=unknown" in line]
        self.assertTrue(lines)
        self.assertTrue(any("reason=control_bundle_install_failed" in line for line in lines))
        self.assertNotIn(str(harness.bundle_root), output)
        state = self.read_state()
        self.assertEqual(state["last_result"], "unknown")
        self.assertEqual(state["deployer_state"], "unknown")
        self.assertEqual(state["target_tag"], harness.old_tag)
        self.assertNotIn(
            "control_bundle_install_failed", (harness.state / "pull-agent.json").read_text()
        )
        self.assertNotIn("reason", (harness.state / "pull-agent.json").read_text())
        self.assertEqual(len(messages), 1)
        self.assertIn("unknown", messages[0])
        self.assertNotIn("control_bundle_install_failed", messages[0])
        self.assertFalse(any(call["kind"] == "deployer" for call in self.calls()))

    # ---- 代理自替换 ----

    def self_update_lines(self, output: str) -> list[str]:
        return [line for line in output.splitlines() if "阶段=agent_self_update" in line]

    def self_update_alerts(self, messages: list[str]) -> list[str]:
        return [message for message in messages if "agent_self_update" in message]

    def in_place_directory_names(self) -> list[str]:
        return sorted(p.name for p in self.harness.in_place_agent.parent.iterdir())

    def test_agent_replaces_itself_from_verified_bundle_and_keeps_deploy_result(self):
        """包内代理摘要不同且标记更高：原子替换、留 .bak、审计带新标记，本轮部署结果不变。"""
        harness = self.harness
        old_sha = harness.in_place_sha256()
        stale = harness.in_place_agent.with_name("release_pull_agent.py.bak-deadbeef")
        stale.write_bytes(b"stale backup\n")
        newer = harness.agent_with_version(AGENT.AGENT_VERSION + 1)
        self.assertEqual(AGENT.parse_agent_version(newer), AGENT.AGENT_VERSION + 1)
        new_sha = harness.install_bundle_agent(newer)
        self.assertNotEqual(new_sha, old_sha)
        # 状态账与旧计划在候选就位之后写：旧计划里冻着旧版本清单，包索引换了它也得一起重算。
        harness.state_for_old()
        messages = []
        code, output = self.run_agent(sender=lambda message, env, timeout: messages.append(message))
        self.assertEqual(code, 0)
        state = self.read_state()
        self.assertEqual(state["last_result"], "verified")
        self.assertEqual(state["target_tag"], harness.target_tag)
        self.assertTrue(
            any(call["kind"] == "deployer" and "apply" in call["argv"] for call in self.calls())
        )
        # 在位文件已是包内代理，权限位沿用旧文件；旧文件留成 .bak-<旧摘要前 8 位>，更早的副本删掉。
        self.assertEqual(harness.in_place_sha256(), new_sha)
        self.assertEqual(stat.S_IMODE(harness.in_place_agent.stat().st_mode), 0o644)
        backup = harness.in_place_agent.with_name(f"release_pull_agent.py.bak-{old_sha[:8]}")
        self.assertEqual(harness.backups(), [backup])
        self.assertEqual(backup.read_bytes(), harness.agent_source)
        self.assertFalse(stale.exists())
        self.assertEqual(
            self.in_place_directory_names(), sorted([backup.name, "release_pull_agent.py"])
        )
        lines = self.self_update_lines(output)
        self.assertEqual(len(lines), 1, output)
        self.assertIn("结果码=agent_self_update_replaced", lines[0])
        self.assertIn(f"next_round_by={AGENT.AGENT_VERSION + 1}", lines[0])
        self.assertIn(f"candidate_sha256={new_sha}", lines[0])
        self.assertIn(f"in_place_sha256={old_sha}", lines[0])
        # 成功不告警：本轮只有 notify_on_success 的成功通知；进程内仍是旧代码。
        self.assertEqual(len(messages), 1)
        self.assertIn("verified", messages[0])
        self.assertEqual(self.self_update_alerts(messages), [])
        self.assertEqual(AGENT.parse_agent_version(harness.agent_source), AGENT.AGENT_VERSION)
        # 每轮首行带运行中代理的版本标记，下一轮由谁在跑一眼可见。
        self.assertIn(
            f"阶段=lock 结果码=acquired host=synthetic-host agent_version={AGENT.AGENT_VERSION}",
            output,
        )

    def test_candidate_off_the_digest_chain_is_rejected_and_alerted_once(self):
        """候选文件摘要不等于索引条目：不替换、rejected 审计、告警恰一次，同 tag 第二轮去重。"""
        harness = self.harness
        harness.write_config(notify_on_success=False)
        harness.set_env(DEPLOY_STATUS="running")
        old_sha = harness.in_place_sha256()
        harness.install_bundle_agent(
            harness.agent_with_version(AGENT.AGENT_VERSION + 1), indexed_sha256="f" * 64
        )
        harness.state_for_old()
        messages = []
        outputs = []
        for _ in range(2):
            code, output = self.run_agent(
                sender=lambda message, env, timeout: messages.append(message)
            )
            self.assertEqual(code, 0)
            outputs.append(output)
            self.assertEqual(self.read_state()["last_result"], "running")
            self.assertEqual(harness.in_place_sha256(), old_sha)
            self.assertEqual(harness.backups(), [])
            lines = self.self_update_lines(output)
            self.assertEqual(len(lines), 1, output)
            self.assertIn("结果码=agent_self_update_rejected", lines[0])
            self.assertIn("reason=candidate_digest_mismatch", lines[0])
        self.assertEqual(len(messages), 1)
        self.assertIn("agent_self_update_rejected", messages[0])
        self.assertIn("阶段：agent_self_update", messages[0])
        self.assertIn("阶段=alert 结果码=sent", outputs[0])
        self.assertIn("阶段=alert 结果码=deduplicated", outputs[1])
        self.assertNotIn("结果码=sent", outputs[1])
        record = self.read_state()["alerts"][f"agent_self_update_rejected:{harness.target_tag}"]
        self.assertTrue(record["sent"])
        self.assertFalse(record["active"])

    def test_index_off_the_manifest_chain_or_missing_entry_is_rejected(self):
        """索引摘要不等于清单 index_sha256、或索引里没有代理条目：同样拒绝，不替换。"""
        harness = self.harness
        harness.write_config(notify_on_success=False)
        old_sha = harness.in_place_sha256()
        for reason in ("index_digest_mismatch", "index_entry_missing"):
            with self.subTest(reason=reason):
                harness.install_bundle_agent(harness.agent_with_version(AGENT.AGENT_VERSION + 1))
                if reason == "index_digest_mismatch":
                    # 只换版本目录内那份索引：外部附件与清单仍一致，包本身照常通过核对。
                    installed_index = harness.installed_source / "control-index.json"
                    installed_index.unlink()
                    _write(installed_index, AGENT.canonical({"files": [], "x": True}), 0o444)
                else:
                    # 清单钉住的索引本身就没有代理条目。
                    harness.install_bundle_index(
                        AGENT.canonical({"schema_revision": 1, "files": []})
                    )
                harness.state_for_old()
                messages = []
                code, output = self.run_agent(
                    sender=lambda message, env, timeout: messages.append(message)
                )
                self.assertEqual(code, 0)
                self.assertEqual(self.read_state()["last_result"], "verified")
                self.assertEqual(harness.in_place_sha256(), old_sha)
                lines = self.self_update_lines(output)
                self.assertEqual(len(lines), 1, output)
                self.assertIn("结果码=agent_self_update_rejected", lines[0])
                self.assertIn(f"reason={reason}", lines[0])
                self.assertEqual(len(self.self_update_alerts(messages)), 1)

    def test_half_written_replacement_leaves_old_agent_intact(self):
        """换入时崩溃：在位文件逐字节仍是旧文件、无临时文件残留、failed 审计 + 告警一次，部署结果不变。"""
        harness = self.harness
        harness.write_config(notify_on_success=False)
        new_sha = harness.install_bundle_agent(harness.agent_with_version(AGENT.AGENT_VERSION + 1))
        harness.state_for_old()
        real_replace = os.replace

        def crash_on_new_file(src, dst, *args, **kwargs):
            if Path(src).name.startswith(".agent-new-"):
                raise OSError(5, "injected crash")
            return real_replace(src, dst, *args, **kwargs)

        messages = []
        with patch.object(AGENT.os, "replace", side_effect=crash_on_new_file):
            code, output = self.run_agent(
                sender=lambda message, env, timeout: messages.append(message)
            )
        self.assertEqual(code, 0)
        self.assertEqual(self.read_state()["last_result"], "verified")
        self.assertEqual(harness.in_place_agent.read_bytes(), harness.agent_source)
        self.assertNotEqual(harness.in_place_sha256(), new_sha)
        self.assertEqual(self.in_place_directory_names(), ["release_pull_agent.py"])
        lines = self.self_update_lines(output)
        self.assertEqual(len(lines), 1, output)
        self.assertIn("结果码=agent_self_update_failed", lines[0])
        self.assertIn("reason=write_failed", lines[0])
        self.assertEqual(len(messages), 1)
        self.assertIn("agent_self_update_failed", messages[0])

    def test_readback_mismatch_after_replace_restores_old_agent(self):
        """换入「成功」但回读摘要不等于候选（写坏）：把旧副本换回，按 failed 上报、告警一次。"""
        harness = self.harness
        harness.write_config(notify_on_success=False)
        harness.install_bundle_agent(harness.agent_with_version(AGENT.AGENT_VERSION + 1))
        harness.state_for_old()
        real_replace = os.replace

        def torn_write(src, dst, *args, **kwargs):
            if Path(src).name.startswith(".agent-new-"):
                Path(src).unlink()
                Path(dst).write_bytes(b"torn write\n")
                return None
            return real_replace(src, dst, *args, **kwargs)

        messages = []
        with patch.object(AGENT.os, "replace", side_effect=torn_write):
            code, output = self.run_agent(
                sender=lambda message, env, timeout: messages.append(message)
            )
        self.assertEqual(code, 0)
        self.assertEqual(self.read_state()["last_result"], "verified")
        self.assertEqual(harness.in_place_agent.read_bytes(), harness.agent_source)
        self.assertEqual(self.in_place_directory_names(), ["release_pull_agent.py"])
        lines = self.self_update_lines(output)
        self.assertEqual(len(lines), 1, output)
        self.assertIn("结果码=agent_self_update_failed", lines[0])
        self.assertIn("reason=readback_mismatch", lines[0])
        self.assertEqual(len(messages), 1)
        self.assertIn("agent_self_update_failed", messages[0])

    def test_candidate_not_newer_than_in_place_is_skipped_without_alert(self):
        """摘要不同但标记不高于在位（相同 / 更低）：不替换、不告警，只审计 skipped_not_newer。"""
        harness = self.harness
        old_sha = harness.in_place_sha256()
        cases = {
            "same_marker": harness.agent_source + b"\n# trailing change without marker bump\n",
            "lower_marker": harness.agent_with_version(AGENT.AGENT_VERSION - 1),
        }
        for name, candidate in cases.items():
            with self.subTest(name=name):
                new_sha = harness.install_bundle_agent(candidate)
                self.assertNotEqual(new_sha, old_sha)
                harness.state_for_old()
                messages = []
                code, output = self.run_agent(
                    sender=lambda message, env, timeout: messages.append(message)
                )
                self.assertEqual(code, 0)
                self.assertEqual(self.read_state()["last_result"], "verified")
                self.assertEqual(harness.in_place_sha256(), old_sha)
                self.assertEqual(harness.backups(), [])
                lines = self.self_update_lines(output)
                self.assertEqual(len(lines), 1, output)
                self.assertIn("结果码=agent_self_update_skipped_not_newer", lines[0])
                self.assertIn(f"in_place_version={AGENT.AGENT_VERSION}", lines[0])
                self.assertEqual(self.self_update_alerts(messages), [])

    def test_identical_candidate_is_silent_and_disabled_switch_skips_everything(self):
        """摘要相同只记 unchanged；开关关闭时整段跳过、只记 disabled——两者都不告警、不写文件。"""
        harness = self.harness
        old_sha = harness.in_place_sha256()
        harness.state_for_old()
        messages = []
        code, output = self.run_agent(sender=lambda message, env, timeout: messages.append(message))
        self.assertEqual(code, 0)
        lines = self.self_update_lines(output)
        self.assertEqual(len(lines), 1, output)
        self.assertIn("结果码=agent_self_update_unchanged", lines[0])
        self.assertEqual(self.self_update_alerts(messages), [])
        # 关闭开关后换成更高标记的候选：仍不替换。
        harness.write_config(agent_self_update=False)
        harness.install_bundle_agent(harness.agent_with_version(AGENT.AGENT_VERSION + 1))
        harness.state_for_old()
        code, output = self.run_agent(sender=lambda message, env, timeout: messages.append(message))
        self.assertEqual(code, 0)
        self.assertEqual(self.read_state()["last_result"], "verified")
        self.assertEqual(harness.in_place_sha256(), old_sha)
        self.assertEqual(harness.backups(), [])
        lines = self.self_update_lines(output)
        self.assertEqual(len(lines), 1, output)
        self.assertIn("结果码=agent_self_update_disabled", lines[0])
        self.assertEqual(self.self_update_alerts(messages), [])

    def test_self_update_switch_is_optional_and_must_be_bool(self):
        """旧配置（不含新键）必须仍通过校验；键在场时只接受布尔值，其它值以 config_shape 拒绝。"""
        base = dict(self.harness.config)
        self.assertNotIn("agent_self_update", base)
        self.assertIs(AGENT.validate_config(base), base)
        for value in (True, False):
            with self.subTest(value=value):
                config = dict(base, agent_self_update=value)
                self.assertIs(AGENT.validate_config(config), config)
        for value in ("yes", 1, 0, None, [True]):
            with self.subTest(value=value):
                with self.assertRaises(AGENT.AgentError) as caught:
                    AGENT.validate_config(dict(base, agent_self_update=value))
                self.assertEqual(caught.exception.code, "config_shape")
        # 必需键一个都不能少，未登记的额外键仍拒绝。
        with self.assertRaises(AGENT.AgentError):
            AGENT.validate_config(dict(base, agent_self_updatee=True))

    def test_self_update_audit_and_alerts_never_leak_secrets_or_host_paths(self):
        """rejected / failed 两种告警正文与审计行不含凭据哨兵，也不含在位目录以内的任何主机路径。"""
        harness = self.harness
        harness.write_config(notify_on_success=False)
        newer = harness.agent_with_version(AGENT.AGENT_VERSION + 1)
        real_replace = os.replace

        def crash_on_new_file(src, dst, *args, **kwargs):
            if Path(src).name.startswith(".agent-new-"):
                raise OSError(5, "injected crash")
            return real_replace(src, dst, *args, **kwargs)

        for result in ("agent_self_update_rejected", "agent_self_update_failed"):
            with self.subTest(result=result):
                if result == "agent_self_update_rejected":
                    harness.install_bundle_agent(newer, indexed_sha256="f" * 64)
                    context = contextlib.nullcontext()
                else:
                    harness.install_bundle_agent(newer)
                    context = patch.object(AGENT.os, "replace", side_effect=crash_on_new_file)
                harness.state_for_old()
                messages = []
                with (
                    patch.dict(os.environ, {"SECRET_SENTINEL": harness.sentinel}, clear=False),
                    context,
                ):
                    code, output = self.run_agent(
                        sender=lambda message, env, timeout: messages.append(message)
                    )
                self.assertEqual(code, 0)
                self.assertEqual(len(messages), 1)
                self.assertIn(result, messages[0])
                joined = output + "\n" + "\n".join(messages)
                self.assertNotIn(harness.sentinel, joined)
                self.assertNotIn(str(harness.root), joined)
                self.assertNotIn(str(harness.in_place_agent.parent), joined)
                self.assertNotIn(harness.sentinel, (harness.state / "pull-agent.json").read_text())
                self.assertEqual(harness.in_place_agent.read_bytes(), harness.agent_source)

    def test_symlinked_in_place_agent_is_never_replaced(self):
        """在位路径是符号链接：不替换、rejected 审计（reason=in_place_symlink）、告警一次，目标文件不动。"""
        harness = self.harness
        harness.write_config(notify_on_success=False)
        real = harness.in_place_agent.with_name("real_agent.py")
        real.write_bytes(harness.agent_source)
        harness.in_place_agent.unlink()
        harness.in_place_agent.symlink_to(real.name)
        harness.install_bundle_agent(harness.agent_with_version(AGENT.AGENT_VERSION + 1))
        harness.state_for_old()
        messages = []
        code, output = self.run_agent(sender=lambda message, env, timeout: messages.append(message))
        self.assertEqual(code, 0)
        self.assertEqual(self.read_state()["last_result"], "verified")
        self.assertTrue(harness.in_place_agent.is_symlink())
        self.assertEqual(real.read_bytes(), harness.agent_source)
        self.assertEqual(harness.backups(), [])
        lines = self.self_update_lines(output)
        self.assertEqual(len(lines), 1, output)
        self.assertIn("结果码=agent_self_update_rejected", lines[0])
        self.assertIn("reason=in_place_symlink", lines[0])
        self.assertEqual(len(messages), 1)
        self.assertIn("agent_self_update_rejected", messages[0])

    def test_self_update_alert_delivery_failure_does_not_change_round_result(self):
        """自替换告警投递失败：只记日志、本轮结论不变，去重记录不写成已发，下一轮重试。"""
        harness = self.harness
        harness.write_config(notify_on_success=False)
        harness.set_env(DEPLOY_STATUS="running")
        harness.install_bundle_agent(
            harness.agent_with_version(AGENT.AGENT_VERSION + 1), indexed_sha256="f" * 64
        )
        harness.state_for_old()
        attempts = []

        def flaky(message, env, timeout):
            attempts.append(message)
            if len(attempts) == 1:
                raise RuntimeError("network failure")

        code, output = self.run_agent(sender=flaky)
        self.assertEqual(code, 0)
        self.assertEqual(self.read_state()["last_result"], "running")
        self.assertIn("阶段=alert 结果码=alert_delivery_failed", output)
        key = f"agent_self_update_rejected:{harness.target_tag}"
        self.assertFalse(self.read_state()["alerts"][key]["sent"])
        code, output = self.run_agent(sender=flaky)
        self.assertEqual(code, 0)
        self.assertEqual(len(attempts), 2)
        self.assertIn("阶段=alert 结果码=sent", output)
        self.assertTrue(self.read_state()["alerts"][key]["sent"])

    def test_agent_version_marker_is_read_from_source_text_only(self):
        """版本标记只按整行正则从文本读出：没有标记、读不到都视为 0，不导入也不执行文件。"""
        harness = self.harness
        self.assertEqual(AGENT.parse_agent_version(b"x = 1\nAGENT_VERSION = 7\n"), 7)
        self.assertEqual(
            AGENT.parse_agent_version(b"X_AGENT_VERSION = 9\nAGENT_VERSION = 3  # x\n"), 0
        )
        self.assertEqual(AGENT.parse_agent_version(b"import os; os.system('x')\n"), 0)
        self.assertEqual(AGENT.parse_agent_version(MODULE_PATH.read_bytes()), AGENT.AGENT_VERSION)
        self.assertGreaterEqual(AGENT.AGENT_VERSION, 1)
        probe = harness.root / "probe.py"
        probe.write_bytes(b"raise SystemExit(99)\n")
        self.assertEqual(AGENT.read_agent_version(probe), 0)
        self.assertEqual(AGENT.read_agent_version(harness.root / "missing.py"), 0)
        self.assertEqual(AGENT.read_agent_version(harness.in_place_agent), AGENT.AGENT_VERSION)
        # 在位代理没有标记（今天预发 / 生产在跑的那种）时视为 0：带标记的候选可以替换它。
        harness.state_for_old()
        without_marker = harness.agent_source.replace(
            f"\nAGENT_VERSION = {AGENT.AGENT_VERSION}\n".encode(), b"\n", 1
        )
        self.assertEqual(AGENT.parse_agent_version(without_marker), 0)
        harness.in_place_agent.write_bytes(without_marker)
        code, output = self.run_agent(sender=lambda message, env, timeout: None)
        self.assertEqual(code, 0)
        self.assertEqual(harness.in_place_agent.read_bytes(), harness.agent_source)
        lines = self.self_update_lines(output)
        self.assertEqual(len(lines), 1, output)
        self.assertIn("结果码=agent_self_update_replaced", lines[0])
        self.assertIn("in_place_version=0", lines[0])

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
        # 解释器只认引导安装建的固定链接（W4 F17）：主机默认 python3 可低于控制包要求的 3.11，
        # 仓库单元不写死任何一台机器的真实路径。全部宿主侧单元的钉住见 test_monitoring_units_python_pin。
        self.assertEqual(exec_line.split()[0], "ExecStart=/opt/lingxi/bin/python3")
        # 代理自己带 -B；子进程靠 [Service] 的环境变量兜底禁写字节码缓存（两头都堵）。
        self.assertEqual(exec_line.split()[1], "-B")
        self.assertTrue(exec_line.split()[2].endswith("/deploy/release_pull_agent.py"))
        sections: dict[str, list[str]] = {}
        current = ""
        for line in service.splitlines():
            if line.startswith("[") and line.endswith("]"):
                current = line
                sections[current] = []
            elif line and not line.startswith("#"):
                sections.setdefault(current, []).append(line)
        self.assertIn("Environment=PYTHONDONTWRITEBYTECODE=1", sections["[Service]"])
        self.assertIn(exec_line, sections["[Service]"])
        # 拉取代理的 drop-in 必须写 User=root（§八），仓库 service 只以注释说明它来自 drop-in。
        self.assertNotRegex(service, r"(?m)^User=")
        user_comments = [
            line for line in service.splitlines() if line.startswith("#") and "User=" in line
        ]
        self.assertTrue(any("drop-in" in line for line in user_comments))
        self.assertTrue(any("User=root" in line for line in user_comments))
        document = (ROOT / "deploy/拉取代理.md").read_text()
        self.assertIn(
            "本机 drop-in `lingxi-release-pull.service.d/10-local.conf`，其中 `[Service]` 为 `User=root`",
            document,
        )
        self.assertIn("仓库 service 不含活动的 `User=`", document)

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
