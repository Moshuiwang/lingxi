"""固定 Docker 操作与只读回读；不接受 shell、任意管理命令或业务修复。"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from control_bundle import (
    activate,
    canonical,
    digest,
    install,
    install_relay,
    sync_directory,
    verify_install,
)
from deploy_state import DeployError, UnknownError, atomic_write, fingerprint, read_json

SERVICES = ("scheduler", "gateway", "worker-queue")


def control_for(plan, release):
    """历史镜像不伪造新清单，恢复用的控制工具单独绑定。"""
    if release["schema"] == 2:
        return release["control_bundle"]
    return plan["recovery"]["historical"]["control_bundle"]


class Runtime:
    """只提供固定部署步骤，不开放任意管理命令。"""

    def __init__(self, host, config):
        """依赖由调用方固定，不查找浮动配置。"""
        self.host = host
        self.config = config
        self.lock_fd = None
        self.state_directory = None

    def command(self, argv, timeout=180, allowed_failure=False):
        """子作业继承主机锁，错误输出不进入部署账。"""
        try:
            result = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self.environment(),
                pass_fds=(() if self.lock_fd is None else (self.lock_fd,)),
            )
        except subprocess.TimeoutExpired:
            raise UnknownError("command_timeout_reconcile_required") from None
        if result.returncode and not allowed_failure:
            raise DeployError("fixed_command_failed")
        return result.returncode, result.stdout

    def environment(self):
        # 私有 env 文件由 Compose 消费；部署器只持显式非秘密字段。
        """凭据仍由容器加载机制消费。"""
        return {
            "PATH": "/usr/bin:/bin",
            "HOME": self.host["deploy_root"],
            "LINGXI_ENV_ROOT": self.host["config_root"],
            **self.config["values"],
        }

    def docker(self, *args, **kwargs):
        """可执行文件由受保护宿主契约固定。"""
        return self.command([self.host["docker"], *args], **kwargs)

    def compose(self, plan, *args, old=False):
        """固定控制包与 mvp 确保恢复三个常驻服务。"""
        release = plan["old"] if old else plan["new"]
        root = Path(self.host["bundle_root"]) / control_for(plan, release)["sha256"]
        override = "prod" if plan["environment"] == "production" else "stage"
        argv = [
            self.host["docker"],
            "compose",
            "--project-name",
            plan["project"],
            "--project-directory",
            self.host["config_root"],
            "-f",
            str(root / "deploy/compose.yaml"),
            "-f",
            str(root / f"deploy/compose.{override}.yaml"),
            "-f",
            str(self.state_directory / (plan["id"] + ".channel.compose.json")),
            "--profile",
            "mvp",
            *args,
        ]
        previous = self.config
        values = dict(previous["values"])
        repo = release["repository"].lower().rsplit("/", 1)[0]
        values.update(LINGXI_IMAGE_REGISTRY="ghcr.io/" + repo, LINGXI_IMAGE_TAG=release["tag"])
        for service, image in release["images"].items():
            values[f"LINGXI_{service.upper()}_IMAGE_DIGEST"] = "@" + image.split("@")[1]
        self.config = dict(previous, values=values)
        try:
            return self.command(argv, timeout=200)
        finally:
            self.config = previous

    def preflight(self, plan):
        """停止服务前必须核对资源和恢复制品。"""
        if os.geteuid() != 0:
            raise DeployError("root_managed_installation_required")
        if sys.platform != "linux" or sys.version_info < (3, 11):
            raise DeployError("linux_python_311_required")
        if socket.gethostname() != plan["host"]:
            raise DeployError("host_mismatch")
        _, version = self.docker("compose", "version", "--short")
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
        if not match or tuple(map(int, match.groups())) < (2, 24, 0):
            raise DeployError("compose_version_unsupported")
        if fingerprint(self.config) != plan["config_sha256"]:
            raise DeployError("configuration_changed")
        free = shutil.disk_usage(self.host["deploy_root"]).free
        if free < plan["resources"]["required_free_bytes"]:
            raise DeployError("insufficient_space")
        for release in (plan["old"], plan["new"]):
            package = Path(plan["packages"][control_for(plan, release)["sha256"]])
            from control_bundle import verify

            verify(package, control_for(plan, release))
        for image in plan["old"]["images"].values():
            self.image(image)
        if plan["recovery"]["compatible"] is not True:
            raise DeployError("recovery_incompatible")
        for directory in (
            self.host["bundle_root"],
            self.host["relay_root"],
            self.host["config_root"],
        ):
            path = Path(directory)
            if path.is_symlink() or not path.is_dir() or path.stat().st_mode & 0o022:
                raise DeployError("installation_directory_unavailable")
        self.check_revocation(plan)
        self.installation_receipt(plan)
        self.verify_service_inventory(plan)
        self.verify_public_files()
        if plan["operation"] == "recover":
            original = read_json(self.state_directory / (plan["recovery_of"]["id"] + ".plan.json"))
            job = self.job(original)
            if job and job["Running"]:
                raise UnknownError("original_migration_still_running")

    def verify_public_files(self):
        """只核对明确非秘密运行文件，禁止扫描或读取私有 env 文件。"""
        allowed = {
            "scheduler": {"company_function_metric_map.toml", "content.override.toml"},
            "worker": {"system_prompt.md", "content.override.toml"},
        }
        for role, names in allowed.items():
            directory = self.config["values"].get("LINGXI_" + role.upper() + "_RUNTIME_CONFIG_DIR")
            expected = self.config["files"][role]
            if directory is None:
                if expected:
                    raise DeployError("public_file_directory_required")
                continue
            root = Path(directory)
            if (
                not root.is_absolute()
                or root.is_symlink()
                or not root.is_dir()
                or root.stat().st_mode & 0o022
            ):
                raise DeployError("public_file_directory_permissions")
            actual = {}
            for name in names:
                path = root / name
                if not path.exists() and not path.is_symlink():
                    continue
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.stat().st_mode & 0o022
                    or path.stat().st_size > 1024 * 1024
                ):
                    raise DeployError("public_file_permissions_or_size")
                actual[name] = digest(path.read_bytes())
            if actual != expected:
                raise DeployError("public_file_configuration_changed")

    def verify_service_inventory(self, plan):
        """只能停止计划已登记的旧服务，接续只允许旧新两份制品。"""
        current = self.containers(plan["project"])
        ledger = read_json(self.state_directory / (plan["id"] + ".state.json"))
        changed = "start" in ledger["stages"] or plan["operation"] == "recover"
        if not changed and set(current) != set(SERVICES):
            raise DeployError("service_inventory_changed")
        choices = [("old", plan["recovery"]["config_sha256"])]
        if changed:
            choices.append(("new", plan["config_sha256"]))
        for name, actual in current.items():
            service = "worker" if name == "worker-queue" else name
            matched = False
            for side, config_sha in choices:
                release = plan[side]
                same_image = (
                    actual["image"].split("@")[-1] == release["images"][service].split("@")[-1]
                )
                same_config = release["schema"] != 2 or (
                    actual["config_sha256"] == config_sha
                    and actual["bundle_sha256"] == control_for(plan, release)["sha256"]
                )
                matched = matched or (same_image and same_config)
            if not matched:
                raise DeployError("unplanned_service_artifact_or_config")

    def check_revocation(self, plan):
        """已明确撤销的资格在执行与观察期间也不能继续使用。"""
        self.verify_public_files()
        revocation = Path(self.host["config_root"]) / "deployment-revocation.json"
        if revocation.exists():
            record = read_json(revocation)
            if (
                record.get("plan_sha256") == fingerprint(plan)
                or record.get("acceptance_sha256") == plan["acceptance_sha256"]
            ):
                raise UnknownError("explicit_approval_revocation")

    def installation_receipt(self, plan):
        """外部安装事实按固定摘要绑定，不把字符串当作已完成核验。"""
        path = Path(self.host["config_root"]) / "deployment-installation.json"
        receipt = read_json(path)
        channel = plan["channel"]
        if (
            fingerprint(receipt) != channel["installation_receipt_sha256"]
            or set(receipt)
            != {"schema", "environment", "project", "bundle_sha256", "binding_version", "checks"}
            or receipt["schema"] != 1
            or receipt["environment"] != plan["environment"]
            or receipt["project"] != plan["project"]
            or receipt["bundle_sha256"] != control_for(plan, plan["new"])["sha256"]
            or receipt["binding_version"] != channel["binding_version"]
            or receipt["checks"]
            != {
                "sudo_policy": True,
                "sshd_policy": True,
                "credential_owner": True,
                "authorized_peer": True,
                "wrong_uid_rejected": True,
                "container_peer_rejected": True,
            }
        ):
            raise DeployError("installation_receipt_missing_or_mismatched")
        return receipt

    def channel_compose(self, plan):
        """新通道只在完整批准后叠加，不影响无 scope 的既有启动。"""
        labels = {
            "io.lingxi.config-sha256": plan["config_sha256"],
            "io.lingxi.bundle-sha256": control_for(plan, plan["new"])["sha256"],
            "io.lingxi.deployment-id": plan["id"],
        }
        result = {"services": {name: {"labels": labels} for name in SERVICES}}
        if plan["new"]["schema"] != 2:
            return result
        values = self.config["values"]
        common = {
            key: values[key] for key in ("LINGXI_INNERTEST_SCOPE", "LINGXI_INNERTEST_BINDING_ID")
        }
        result["services"]["gateway"]["environment"] = common
        result["services"]["scheduler"].update(
            environment=dict(
                common,
                LINGXI_INNERTEST_SOCKET_PATH="/run/lingxi-innertest/admin.sock",
                LINGXI_INNERTEST_BINDING_PATH="/etc/lingxi/innertest/binding.json",
            ),
            volumes=[
                {
                    "type": "bind",
                    "source": str(Path(plan["channel"]["socket_path"]).parent),
                    "target": "/run/lingxi-innertest",
                    "bind": {"create_host_path": False},
                },
                {
                    "type": "bind",
                    "source": str(Path(self.host["config_root"]) / "innertest"),
                    "target": "/etc/lingxi/innertest",
                    "read_only": True,
                    "bind": {"create_host_path": False},
                },
            ],
        )
        return result

    def relay_configuration(self, plan):
        """配置来自批准计划，客户端不能另选 socket。"""
        channel = plan["channel"]
        return {
            "schema_revision": 1,
            "socket_path": channel["socket_path"],
            "relay_uid": channel["host_uid"],
            "socket_owner_uid": channel["socket_owner_uid"],
        }

    def channel_status(self, plan):
        """冻结relay、实际socket和容器映射必须一致。"""
        import stat

        if plan["new"]["schema"] != 2:
            pointer = Path(self.host["relay_root"]) / "current"
            if pointer.exists() or pointer.is_symlink():
                raise DeployError("historical_relay_must_be_disabled")
            return True
        self.installation_receipt(plan)
        channel = plan["channel"]
        target = Path(self.host["relay_root"]) / "current"
        if not target.is_symlink():
            raise DeployError("relay_reference_missing")
        relay = target / "innertest_relay.py"
        configuration = target / "innertest-relay.json"
        expected_receipt = {
            "schema_revision": 1,
            "bundle_sha256": control_for(plan, plan["new"])["sha256"],
            "relay_sha256": channel["relay_sha256"],
            "config_sha256": digest(canonical(self.relay_configuration(plan))),
            "mode": 0o444,
            "directory_mode": 0o555,
        }
        if (
            os.readlink(target) != digest(canonical(expected_receipt))
            or (target / "installation.json").read_bytes() != canonical(expected_receipt)
            or set(p.name for p in target.iterdir())
            != {"innertest_relay.py", "innertest-relay.json", "installation.json"}
        ):
            raise DeployError("relay_installation_receipt_drift")
        for path in (target.resolve(), relay, configuration, target / "installation.json"):
            info = path.lstat()
            if info.st_uid != 0 or stat.S_ISLNK(info.st_mode) or info.st_mode & 0o222:
                raise DeployError("relay_installation_permissions")

        if digest(relay.read_bytes()) != channel[
            "relay_sha256"
        ] or configuration.read_bytes() != canonical(self.relay_configuration(plan)):
            raise DeployError("relay_configuration_drift")
        path = Path(channel["socket_path"])
        info, parent = path.lstat(), path.parent.lstat()
        if (
            not stat.S_ISSOCK(info.st_mode)
            or info.st_uid != channel["socket_owner_uid"]
            or info.st_gid != channel["socket_gid"]
            or stat.S_IMODE(info.st_mode) != 0o660
            or stat.S_IMODE(parent.st_mode) != 0o750
            or parent.st_gid != channel["socket_gid"]
            or parent.st_uid not in (0, channel["socket_owner_uid"])
        ):
            raise DeployError("socket_identity_or_acl_drift")
        binding = Path(self.host["config_root"]) / "innertest" / "binding.json"

        value = read_json(binding, public=True)
        if (
            value.get("schema_revision") != 1
            or value.get("host_uid") != channel["host_uid"]
            or value.get("peer_uid") != channel["peer_uid"]
            or value.get("uid_map_sha256") != channel["uid_map_sha256"]
            or value.get("socket_gid") != channel["socket_gid"]
            or value.get("binding_id") != self.config["values"].get("LINGXI_INNERTEST_BINDING_ID")
        ):
            raise DeployError("binding_configuration_drift")
        _, uid_map = self.docker(
            "exec",
            self.containers(plan["project"])["scheduler"]["id"],
            "python",
            "-c",
            "from pathlib import Path;import hashlib;print(hashlib.sha256(Path('/proc/self/uid_map').read_bytes()).hexdigest())",
        )
        if uid_map.strip() != channel["uid_map_sha256"]:
            raise DeployError("container_uid_mapping_drift")
        return True

    def image(self, reference):
        """本地存在还必须与固定镜像摘要一致。"""
        _, output = self.docker("image", "inspect", reference, "--format", "{{json .RepoDigests}}")
        if reference not in json.loads(output):
            raise DeployError("image_digest_mismatch")

    def containers(self, project):
        """只读服务运行字段，不读取容器凭据。"""
        _, output = self.docker(
            "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"
        )
        result = {}
        for identifier in output.split():
            _, raw = self.docker(
                "inspect",
                identifier,
                "--format",
                "{{json .Config.Labels}}\n{{json .Config.Image}}\n{{json .State}}\n{{json .RestartCount}}",
            )
            labels, image, state, restarts = map(json.loads, raw.splitlines())
            service = labels.get("com.docker.compose.service")
            if service not in SERVICES:
                continue
            if service in result:
                raise UnknownError("duplicate_service")
            result[service] = {
                "id": identifier,
                "image": image,
                "config_sha256": labels.get("io.lingxi.config-sha256"),
                "bundle_sha256": labels.get("io.lingxi.bundle-sha256"),
                "running": state["Running"],
                "restarts": restarts,
                "health": state.get("Health", {}).get("Status", "missing"),
            }
        return result

    def migration(self, plan):
        """版本回读复用固定迁移镜像的 current，不在宿主解析凭据。"""
        suffix = "prod" if plan["environment"] == "production" else "stage"
        source = plan["new"] if plan["operation"] == "apply" else plan["old"]
        code, output = self.docker(
            "run",
            "--rm",
            "--read-only",
            "--network",
            "host",
            "--env-file",
            str(Path(self.host["config_root"]) / f".env.{suffix}.migrate"),
            source["images"]["migrate"],
            "current",
            allowed_failure=True,
        )
        heads = re.findall(r"^([A-Za-z0-9_]+)(?:\s+\(head\))?\s*$", output, re.M)
        if code or len(heads) != 1:
            raise UnknownError("migration_revision_unknown")
        return heads

    def job(self, plan):
        """固定作业名用于识别断线后仍在运行的迁移。"""
        name = "lingxi-migration-" + plan["id"]
        _, identifiers = self.docker("ps", "-aq", "--filter", "name=^/" + name + "$")
        if not identifiers.strip():
            return None
        _, raw = self.docker(
            "inspect",
            name,
            "--format",
            "{{json .State}}\n{{json .Config.Image}}\n{{json .Config.Labels}}\n{{json .Id}}",
        )
        status, image, labels, identifier = map(json.loads, raw.splitlines())
        if (
            image.split("@")[-1] != plan["new"]["images"]["migrate"].split("@")[-1]
            or labels.get("com.docker.compose.project") != plan["project"]
            or labels.get("com.docker.compose.service") != "migrate"
        ):
            raise UnknownError("migration_job_identity_mismatch")
        return {
            "Running": status["Running"],
            "ExitCode": status["ExitCode"],
            "id": identifier,
            "image": image,
        }

    def business_status(self, plan, containers):
        """只读固定业务接口，不因进程存在就宣告恢复兼容。"""
        scheduler = containers.get("scheduler")
        if not scheduler or not scheduler["running"]:
            if plan["old"]["schema"] != 2:
                raise UnknownError("business_probe_unavailable")
            suffix = "prod" if plan["environment"] == "production" else "stage"
            code, raw = self.docker(
                "run",
                "--rm",
                "--read-only",
                "--network",
                "host",
                "--pids-limit",
                "64",
                "--memory",
                "128m",
                "--env-file",
                str(Path(self.host["config_root"]) / f".env.{suffix}.scheduler"),
                "--env",
                "LINGXI_INNERTEST_SCOPE=" + self.config["values"]["LINGXI_INNERTEST_SCOPE"],
                "--env",
                "LINGXI_INNERTEST_BINDING_ID="
                + self.config["values"]["LINGXI_INNERTEST_BINDING_ID"],
                "--entrypoint",
                "python",
                plan["old"]["images"]["scheduler"],
                "-B",
                "-m",
                "lingxi.apps.innertest_status",
                allowed_failure=True,
            )
        else:
            code, raw = self.docker(
                "exec",
                scheduler["id"],
                "python",
                "-m",
                "lingxi.apps.innertest_status",
                allowed_failure=True,
            )
        if code:
            raise DeployError("business_recovery_interface_unavailable")
        result = json.loads(raw)
        if (
            set(result) != {"ok", "schema_revision", "roster", "binding", "followups"}
            or result["ok"] is not True
            or result["schema_revision"] != 1
        ):
            raise UnknownError("business_probe_invalid")
        roster, binding, followups = result["roster"], result["binding"], result["followups"]
        if (
            set(roster)
            != {"schema_revision", "compatible", "version", "mode", "requires_dynamic_roster"}
            or roster["schema_revision"] != 1
            or roster["compatible"] is not True
            or roster["mode"] not in ("legacy", "database")
            or set(binding) != {"binding_id", "version", "enabled", "authorized"}
            or binding["binding_id"] != self.config["values"].get("LINGXI_INNERTEST_BINDING_ID")
            or binding["version"] != plan["channel"]["binding_version"]
            or binding["enabled"] is not True
            or binding["authorized"] is not True
            or set(followups)
            != {"contract_versions", "compatible", "inflight", "recoverable", "unknown"}
            or followups["compatible"] is not True
        ):
            raise DeployError("business_recovery_or_binding_incompatible")
        if plan["new"]["schema"] != 2 and (
            roster["requires_dynamic_roster"]
            or any(followups[k] for k in ("inflight", "recoverable", "unknown"))
        ):
            raise DeployError("historical_target_cannot_consume_business_state")
        return result

    def recovery_status(self, plan, containers):
        """复用只读业务恢复接口，未知外发不改成未发送。"""
        if plan["old"]["schema"] != 2:
            # 首次引导的旧镜像没有新接口，仅使用独立绑定的历史兼容记录。
            return {"compatible": plan["recovery"]["historical"]["compatible"]}
        return self.business_status(plan, containers)["followups"]

    def snapshot(self, plan, *, readonly=False):
        """状态查询不创建容器或修改状态账。"""
        containers = self.containers(plan["project"])
        if readonly:
            running = next(
                (
                    (name, containers[name])
                    for name in ("scheduler", "worker-queue", "gateway")
                    if containers.get(name, {}).get("running")
                ),
                None,
            )
            if running is None:
                return {"services": containers, "migration_heads": None, "job": self.job(plan)}
            dsn_name = (
                "LINGXI_GATEWAY_POSTGRES_DSN" if running[0] == "gateway" else "LINGXI_POSTGRES_DSN"
            )
            probe = (
                "import json,os;from lingxi.adapters.postgres import connect;"
                f"c=connect(os.environ[{dsn_name!r}]);"
                "print(json.dumps([r[0] for r in c.execute('SELECT version_num FROM alembic_version')]));c.close()"
            )
            _, raw = self.docker("exec", running[1]["id"], "python", "-c", probe)
            heads = json.loads(raw)
        else:
            job = self.job(plan)
            heads = None if job and job["Running"] else self.migration(plan)
        result = {
            "services": containers,
            "migration_heads": heads,
            "job": self.job(plan) if readonly else job,
        }
        if containers.get("scheduler", {}).get("running") and plan["new"]["schema"] == 2:
            try:
                result["business"] = self.business_status(plan, containers)
            except (DeployError, ValueError, KeyError):
                result["business"] = {"status": "unavailable"}
        return result

    def verify_business_modules(self, plan):
        """停止旧服务前确认固定新镜像具备恢复接口，不启动业务进程。"""
        if plan["new"]["schema"] != 2:
            return
        probe = "import importlib.util;assert all(importlib.util.find_spec(n) for n in ('lingxi.apps.innertest_status','lingxi.adapters.innertest_socket','lingxi.adapters.innertest_handlers'))"
        code, _ = self.docker(
            "run",
            "--rm",
            "--read-only",
            "--network",
            "none",
            "--pids-limit",
            "32",
            "--memory",
            "128m",
            "--entrypoint",
            "python",
            plan["new"]["images"]["scheduler"],
            "-B",
            "-c",
            probe,
            allowed_failure=True,
        )
        if code:
            raise DeployError("business_recovery_interface_unavailable")

    def perform(self, stage, plan):
        """只允许预定义的部署副作用。"""
        if stage == "prepare":
            path = self.state_directory / (plan["id"] + ".channel.compose.json")
            document = self.channel_compose(plan)
            if path.exists() and read_json(path) != document:
                raise DeployError("channel_compose_drift")
            atomic_write(path, document)
            for release in (plan["old"], plan["new"]):
                bundle = control_for(plan, release)
                install(
                    Path(plan["packages"][bundle["sha256"]]), bundle, Path(self.host["bundle_root"])
                )
            for image in plan["new"]["images"].values():
                self.docker("pull", image)
                self.image(image)
            self.verify_business_modules(plan)
            if (
                shutil.disk_usage(self.host["deploy_root"]).free
                < plan["resources"]["required_free_bytes"]
            ):
                raise DeployError("insufficient_space_after_materialization")
        elif stage == "stop":
            snapshot = self.containers(plan["project"])
            status = self.recovery_status(plan, snapshot)
            if not status["compatible"]:
                raise DeployError("business_format_incompatible")
            # Compose 的三个服务使用各自停止上限；不添加业务队列管理入口。
            for name, timeout in (("gateway", 60), ("worker-queue", 90), ("scheduler", 150)):
                if name not in snapshot:
                    if plan["operation"] == "recover":
                        continue
                    raise UnknownError("stop_service_missing")
                self.docker(
                    "stop", "--time", str(timeout), snapshot[name]["id"], timeout=timeout + 10
                )
        elif stage == "migrate":
            name = "lingxi-migration-" + plan["id"]
            self.compose(
                plan,
                "run",
                "--no-deps",
                "--name",
                name,
                "-d",
                "migrate",
                "upgrade",
                plan["new"]["migration_heads"][0],
            )
            _, output = self.docker("wait", name, timeout=600)
            if output.strip() != "0":
                raise UnknownError("migration_failed_reconcile_required")
        elif stage == "activate":
            if plan["new"]["schema"] != 2:
                pointer = Path(self.host["relay_root"]) / "current"
                if pointer.is_symlink():
                    pointer.unlink()
                    sync_directory(Path(self.host["relay_root"]))
                elif pointer.exists():
                    raise DeployError("relay_reference_invalid")
                activate(Path(self.host["bundle_root"]), control_for(plan, plan["new"]))
                return
            target, receipt = install_relay(
                Path(self.host["bundle_root"]),
                control_for(plan, plan["new"]),
                self.relay_configuration(plan),
                Path(self.host["relay_root"]),
            )
            temporary = Path(self.host["relay_root"]) / ".current-next"
            if temporary.is_symlink():
                if os.readlink(temporary) != target.name:
                    raise DeployError("relay_pending_reference_mismatch")
            elif temporary.exists():
                raise DeployError("relay_pending_reference_invalid")
            else:
                temporary.symlink_to(target.name)
            try:
                os.replace(temporary, Path(self.host["relay_root"]) / "current")
                sync_directory(Path(self.host["relay_root"]))
            finally:
                temporary.unlink(missing_ok=True)
            activate(Path(self.host["bundle_root"]), control_for(plan, plan["new"]))
        elif stage == "start":
            self.compose(plan, "up", "-d", "--no-build", "--pull", "never", *SERVICES)
            deadline = time.monotonic() + 120
            while True:
                try:
                    if self.complete("start", plan, {"services": self.containers(plan["project"])}):
                        break
                except FileNotFoundError:
                    pass
                if time.monotonic() >= deadline or time.time() > plan["expires_at"]:
                    raise UnknownError("startup_health_deadline")
                time.sleep(2)
        elif stage == "observe":
            return
        else:
            raise DeployError("unknown_stage")

    def complete(self, stage, plan, snapshot):
        """完成标记必须与当前实际状态相符。"""
        if stage == "prepare":
            for release in (plan["old"], plan["new"]):
                verify_install(
                    Path(self.host["bundle_root"]) / control_for(plan, release)["sha256"],
                    control_for(plan, release),
                )
            for image in plan["new"]["images"].values():
                self.image(image)
            return True
        if stage == "stop":
            present = set(snapshot["services"])
            required = (
                present <= set(SERVICES)
                if plan["operation"] == "recover"
                else present == set(SERVICES)
            )
            return required and all(not item["running"] for item in snapshot["services"].values())
        if stage == "migrate":
            job = snapshot["job"]
            return snapshot["migration_heads"] == plan["new"]["migration_heads"] and (
                job is None or (not job["Running"] and job["ExitCode"] == 0)
            )
        if stage == "activate":
            pointer = Path(self.host["bundle_root"]) / "current"
            return (
                pointer.is_symlink()
                and os.readlink(pointer) == control_for(plan, plan["new"])["sha256"]
            )
        if stage in ("start", "observe"):
            services = snapshot["services"]
            healthy = set(services) == set(SERVICES) and all(
                services[name]["running"]
                and services[name]["config_sha256"] == plan["config_sha256"]
                and services[name]["bundle_sha256"] == control_for(plan, plan["new"])["sha256"]
                and services[name]["health"] == "healthy"
                and services[name]["image"].split("@")[-1]
                == plan["new"]["images"]["worker" if name == "worker-queue" else name].split("@")[
                    -1
                ]
                for name in SERVICES
            )
            return (
                healthy
                and self.channel_status(plan)
                and (plan["new"]["schema"] != 2 or bool(self.business_status(plan, services)))
            )
        return False

    def cleanup(self, plan):
        """只移除本部署及获批恢复来源的已结束迁移容器，不删卷。"""
        selected = [plan]
        if plan["operation"] == "recover":
            selected.append(
                read_json(self.state_directory / (plan["recovery_of"]["id"] + ".plan.json"))
            )
        removed = []
        for item in selected:
            job = self.job(item)
            if job is None:
                continue
            if job["Running"] or (item["id"] == plan["id"] and job["ExitCode"] != 0):
                raise UnknownError("migration_job_requires_investigation")
            self.docker("rm", "lingxi-migration-" + item["id"])
            removed.append({"job_id": job["id"], "exit_code": job["ExitCode"]})
        return removed

    def observe(self, plan, save_sample):
        """中断后重新计算连续观察窗口。"""
        started = time.monotonic()
        baseline = None
        while True:
            self.check_revocation(plan)
            if time.time() > plan["expires_at"]:
                raise UnknownError("execution_window_expired")
            snapshot = self.snapshot(plan, readonly=True)
            if not self.complete("start", plan, snapshot):
                raise UnknownError("observation_unhealthy")
            current = {
                name: (item["id"], item["restarts"]) for name, item in snapshot["services"].items()
            }
            if baseline is not None and current != baseline:
                raise UnknownError("observation_service_restart")
            baseline = current
            save_sample(snapshot)
            if time.monotonic() - started >= 900:
                return snapshot
            time.sleep(15)
