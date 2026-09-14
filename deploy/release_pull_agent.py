#!/usr/bin/env python3
"""只出网的 Release 拉取代理：选择、核对、接续，不实现部署状态机。

本文件是控制包成员，故只依赖 Python 标准库，不 import ``lingxi``，也不依赖
工作树中的相对路径。真正的部署仍由同一份版本化控制包中的
``deploy/lingxi_deploy.py`` 完成；本代理只负责把 Release、控制包、计划批准和
部署器回读串成一个可恢复的单轮流程。

告警发送逻辑最小复制自 ``scripts/ops/host_health_alert.py`` 的
``load_credentials`` 与飞书群文本通道：复用同一应用、同一管理群字段、同一
0600/属主检查，不通过导入仓库脚本来获得独立可分发性。
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit

CONFIG_KEYS = frozenset(
    {
        "schema",
        "repository",
        "gh_command",
        "release_manifest",
        "control_bundle_tool",
        "deployer",
        "public_config",
        "alert_env_file",
        "pre_apply_hooks",
        "poll_timeout_seconds",
        "deploy_timeout_seconds",
        "notify_on_success",
    }
)
HOST_KEYS = frozenset(
    {
        "schema",
        "host",
        "environment",
        "project",
        "deploy_root",
        "config_root",
        "bundle_root",
        "relay_root",
        "lock_path",
        "docker",
        "approval_sources",
    }
)
SERVICES = ("scheduler", "migrate", "gateway", "worker")
RUNNING_SERVICES = ("scheduler", "gateway", "worker-queue")
MANIFEST_SERVICE_FOR_CONTAINER = {
    "scheduler": "scheduler",
    "gateway": "gateway",
    "worker-queue": "worker",
}
RELEASE_TAG = re.compile(
    r"^v(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\.(?P<patch>0|[1-9][0-9]*)"
    r"(?:-rc\.(?P<rc>[1-9][0-9]*))?$"
)
# 只请求 gh 2.97 `release list --json` 手册列出的字段；页面地址按仓库 + 标签构造。
RELEASE_LIST_JSON_FIELDS = ("tagName", "isPrerelease", "isDraft", "publishedAt")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
APPROVAL_SOURCE = re.compile(
    r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/"
    r"(?:issues|pull)/[1-9][0-9]*(?:#issuecomment-[0-9]+)?$"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
PLAN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
DEPLOYER_STATES = frozenset({"planned", "running", "verified", "failed", "unknown"})
FAILURE_CODES = frozenset(
    {
        "release_list_unavailable",
        "downgrade_refused",
        "external_change_detected",
        "bundle_digest_mismatch",
        "installation_receipt_unusable",
        "pre_apply_hook_unsafe",
        "pre_apply_hook_failed",
        "deploy_timeout",
        "failed",
        "unknown",
        "alert_delivery_failed",
    }
)
IMMEDIATE_ALERT_CODES = frozenset(
    {
        "downgrade_refused",
        "external_change_detected",
        "bundle_digest_mismatch",
        "installation_receipt_unusable",
        "pre_apply_hook_unsafe",
        "pre_apply_hook_failed",
        "deploy_timeout",
        "failed",
        "unknown",
    }
)
HEALTHY_CODES = frozenset({"verified", "already_in_place", "already_in_place_external"})
# 引导安装收据（`<config_root>/deployment-installation.json`）：键集合与六项人工核对项
# 复制自部署器 `deploy_runtime.Runtime.installation_receipt`，两处必须同形。
INSTALLATION_RECEIPT_NAME = "deployment-installation.json"
INSTALLATION_RECEIPT_KEYS = frozenset(
    {"schema", "environment", "project", "bundle_sha256", "binding_version", "checks"}
)
INSTALLATION_RECEIPT_CHECKS = (
    "sudo_policy",
    "sshd_policy",
    "credential_owner",
    "authorized_peer",
    "wrong_uid_rejected",
    "container_peer_rejected",
)
ALERT_ENV_KEYS = (
    "LINGXI_FEISHU_APP_ID",
    "LINGXI_FEISHU_APP_SECRET",
    "LINGXI_ADMIN_GROUP_CHAT_ID",
)
FEISHU_BASE_URL = "https://open.feishu.cn/open-apis"


class AgentError(RuntimeError):
    """不把外部命令原文带进日志的稳定代理错误。"""

    def __init__(self, code: str):
        """保存不含外部原文的稳定结果码。"""
        self.code = code
        super().__init__(code)


class BundleDigestMismatchError(AgentError):
    """控制包或外部索引摘要不一致。"""

    def __init__(self):
        """固定为篡改拒绝结果码。"""
        super().__init__("bundle_digest_mismatch")


class CommandTimeoutError(AgentError):
    """非部署命令达到本轮上限。"""

    def __init__(self):
        """固定为命令超时结果码。"""
        super().__init__("command_timeout")


def canonical(value: object) -> bytes:
    """与部署状态账相同的稳定 JSON 编码。"""
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def fingerprint(value: object) -> str:
    """对完整非秘密 JSON 材料计算 SHA256。"""
    return hashlib.sha256(canonical(value)).hexdigest()


def timestamp() -> str:
    """输出不含本机路径和凭据的 UTC 时刻。"""
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_path(value: object) -> bool:
    """绝对路径且不含路径穿越段；不替调用方解析符号链接。"""
    return (
        isinstance(value, str)
        and bool(value)
        and "\x00" not in value
        and Path(value).is_absolute()
        and ".." not in Path(value).parts
    )


def _check_cli_path(path: Path) -> Path:
    if not _safe_path(str(path)):
        raise AgentError("cli_path_must_be_absolute")
    return path


def _read_json_bytes(path: Path, *, private: bool) -> bytes:
    """以不跟随链接的方式读取受控 JSON 的原始字节；错误不回显路径内容。"""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise AgentError("json_file_unavailable") from None
    try:
        info = os.fstat(fd)
        mode = stat.S_IMODE(info.st_mode)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise AgentError("json_file_permissions")
        if private and mode != 0o600:
            raise AgentError("json_file_permissions")
        if not private and mode & 0o022:
            raise AgentError("json_file_permissions")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(1024 * 1024 + 1)
    finally:
        os.close(fd)
    if len(raw) > 1024 * 1024:
        raise AgentError("json_file_too_large")
    return raw


def _parse_json(raw: bytes) -> object:
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AgentError("json_file_invalid") from None


def _read_json(path: Path, *, private: bool) -> object:
    """以不跟随链接的方式读取受控 JSON；错误不回显路径内容。"""
    return _parse_json(_read_json_bytes(path, private=private))


def _private_directory(path: Path, *, create: bool = False) -> None:
    if create:
        try:
            path.mkdir(mode=0o700, parents=False, exist_ok=True)
        except OSError:
            raise AgentError("state_directory_unavailable") from None
    try:
        info = path.lstat()
    except OSError:
        raise AgentError("state_directory_unavailable") from None
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise AgentError("state_directory_permissions")


def _atomic_write(path: Path, value: object) -> None:
    """0600 原子替换并同步目录，避免状态账留下半份 JSON。"""
    _private_directory(path.parent)
    try:
        fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        raise AgentError("state_write_failed") from None
    finally:
        if "temporary" in locals():
            Path(temporary).unlink(missing_ok=True)


def _replace_private_file(path: Path, data: bytes) -> None:
    """在 root 保护的目录里以 0600 原子替换一份私有文件并同步目录。

    与 ``_atomic_write`` 的区别只在目录判定：配置根按引导安装约定是 root 属主、无组 /
    其他写权限的 0750 目录，不是状态账那种 0700 私有目录。
    """
    parent = path.parent
    try:
        info = parent.lstat()
    except OSError:
        raise AgentError("installation_receipt_write_failed") from None
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o022
    ):
        raise AgentError("installation_receipt_write_failed")
    try:
        fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=parent)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        raise AgentError("installation_receipt_write_failed") from None
    finally:
        if "temporary" in locals():
            Path(temporary).unlink(missing_ok=True)


def _usable_installation_receipt(host: dict, receipt: object) -> dict:
    """引导安装收据只有形状完整、环境与项目相符、六项人工核对全为真时才可沿用。

    六项 ``checks`` 与 ``binding_version`` 由人在引导安装时写定，代理只核对、不改写；
    不符就拒绝本轮，不替人把「未做」写成已做。
    """
    if (
        not isinstance(receipt, dict)
        or set(receipt) != INSTALLATION_RECEIPT_KEYS
        or receipt["schema"] != 1
        or not isinstance(receipt["bundle_sha256"], str)
        or not SHA256.fullmatch(receipt["bundle_sha256"])
        or type(receipt["binding_version"]) is not int
        or receipt["binding_version"] < 1
    ):
        raise AgentError("installation_receipt_shape")
    if receipt["environment"] != host["environment"] or receipt["project"] != host["project"]:
        raise AgentError("installation_receipt_environment")
    if receipt["checks"] != {name: True for name in INSTALLATION_RECEIPT_CHECKS}:
        raise AgentError("installation_receipt_checks")
    return receipt


def refresh_installation_receipt(host: dict, bundle_sha256: str) -> tuple[dict, bool]:
    """新控制包装好后把收据的 ``bundle_sha256`` 刷成新包摘要，返回收据与是否改写。

    部署器 apply 前要求收据的 ``bundle_sha256`` 等于目标新版本的控制包摘要；无人值守
    时只有代理装新包，所以这一个字段由代理随新包刷新。改写前先把旧收据原样备份到
    同目录 ``deployment-installation.json.prev``，再以 0600 原子替换；其余字段原样保留。
    """
    path = Path(host["config_root"]) / INSTALLATION_RECEIPT_NAME
    raw = _read_json_bytes(path, private=True)
    receipt = _usable_installation_receipt(host, _parse_json(raw))
    if receipt["bundle_sha256"] == bundle_sha256:
        return receipt, False
    refreshed = dict(receipt, bundle_sha256=bundle_sha256)
    _replace_private_file(path.with_name(path.name + ".prev"), raw)
    _replace_private_file(path, canonical(refreshed))
    return refreshed, True


def _base_state(host: dict) -> dict:
    return {
        "schema": 1,
        "host": host["host"],
        "environment": host["environment"],
        "last_run_at": "",
        "last_result": "never",
        "consecutive_failures": 0,
        "target_tag": None,
        "release_url": None,
        "plan_id": None,
        "deployer_state": None,
        "approval_sha256": None,
        "bundle_digest": None,
        "highest_deployed_tag": None,
        "verified_digests": None,
        "external_change_tag": None,
        "alerts": {},
    }


def _load_state(path: Path, host: dict) -> dict:
    if not path.exists():
        return _base_state(host)
    raw = _read_json(path, private=True)
    if not isinstance(raw, dict) or raw.get("schema") != 1:
        raise AgentError("state_shape")
    if raw.get("host") != host["host"] or raw.get("environment") != host["environment"]:
        raise AgentError("state_host_mismatch")
    if not isinstance(raw.get("alerts", {}), dict):
        raise AgentError("state_shape")
    return raw


def _safe_url(value: object, repository: str, tag: str) -> str:
    """只保存 GitHub Release 页面，不保存查询串或片段。"""
    fallback = f"https://github.com/{repository}/releases/tag/{tag}"
    if not isinstance(value, str) or not value:
        return fallback
    try:
        parsed = urlsplit(value)
    except ValueError:
        return fallback
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.path != f"/{repository}/releases/tag/{tag}"
    ):
        return fallback
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _log(stage: str, result: str, **fields: object) -> None:
    """每行只输出固定字段和已脱敏值，不输出子命令原文。"""
    parts = [timestamp(), f"阶段={stage}", f"结果码={result}"]
    for key, value in fields.items():
        if value is None:
            continue
        if key.endswith("url") or key == "release_url":
            try:
                parsed = urlsplit(str(value))
                value = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
            except ValueError:
                value = "github_release_page"
        text = str(value).replace("\r", " ").replace("\n", " ").replace("\t", " ")
        if "?" in text:
            text = text.split("?", 1)[0]
        parts.append(f"{key}={text[:256]}")
    print(" ".join(parts), flush=True)


def validate_host(host: object) -> dict:
    """复制部署器的宿主契约边界，代理不能自行换宿主或授权来源。"""
    if not isinstance(host, dict) or set(host) != HOST_KEYS:
        raise AgentError("host_shape")
    if host["schema"] != 1 or host["environment"] not in ("stage", "production"):
        raise AgentError("host_environment")
    if (
        not isinstance(host["host"], str)
        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", host["host"])
        or not isinstance(host["project"], str)
        or not PLAN_ID.fullmatch(host["project"])
    ):
        raise AgentError("host_identity")
    for key in (
        "deploy_root",
        "config_root",
        "bundle_root",
        "relay_root",
        "lock_path",
        "docker",
    ):
        if not _safe_path(host[key]):
            raise AgentError("host_paths_must_be_absolute")
    sources = host["approval_sources"]
    if (
        not isinstance(sources, list)
        or not sources
        or len(sources) != 1
        or any(not isinstance(item, str) or not APPROVAL_SOURCE.fullmatch(item) for item in sources)
        or len(set(sources)) != len(sources)
    ):
        raise AgentError("approval_sources_required")
    return host


def validate_config(config: object) -> dict:
    """非秘密代理配置必须精确命中固定字段集合。"""
    if not isinstance(config, dict) or set(config) != CONFIG_KEYS or config["schema"] != 1:
        raise AgentError("config_shape")
    if not isinstance(config["repository"], str) or not REPOSITORY.fullmatch(config["repository"]):
        raise AgentError("config_repository")
    for key in (
        "gh_command",
        "release_manifest",
        "control_bundle_tool",
        "deployer",
        "public_config",
        "alert_env_file",
    ):
        if not _safe_path(config[key]):
            raise AgentError("config_paths_must_be_absolute")
    hooks = config["pre_apply_hooks"]
    if (
        not isinstance(hooks, list)
        or any(not _safe_path(item) for item in hooks)
        or len(set(hooks)) != len(hooks)
    ):
        raise AgentError("config_hooks_shape")
    for key in ("poll_timeout_seconds", "deploy_timeout_seconds"):
        value = config[key]
        if type(value) is not int or value <= 0:
            raise AgentError("config_timeout_shape")
    if config["deploy_timeout_seconds"] + 1200 > 3600:
        raise AgentError("config_timeout_exceeds_service_bound")
    if type(config["notify_on_success"]) is not bool:
        raise AgentError("config_notify_shape")
    return config


def _command_argv(path: str | Path, *args: str) -> list[str]:
    path = str(path)
    return ([path] if os.access(path, os.X_OK) else [sys.executable, path]) + list(args)


def _run_command(argv: list[str], *, timeout: int, env: dict[str, str] | None = None) -> str:
    """不使用 shell；失败与超时均只给稳定类别。"""
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            close_fds=True,
        )
    except subprocess.TimeoutExpired:
        raise CommandTimeoutError() from None
    except (OSError, ValueError):
        raise AgentError("command_unavailable") from None
    if result.returncode:
        raise AgentError("command_failed")
    return result.stdout


def _gh_env(config: dict) -> dict[str, str]:
    env = os.environ.copy()
    # release_manifest.py 的机器身份入口只能从此固定值取得；环境中的其他值不进账。
    env["LINGXI_GH_COMMAND"] = config["gh_command"]
    return env


def release_version_key(tag: str) -> tuple[int, int, int, int, int]:
    """返回可用于语义比较的版本数字元组；正式版高于同号的任何候选版。"""
    match = RELEASE_TAG.fullmatch(tag)
    if not match:
        raise AgentError("release_tag_invalid")
    return (
        int(match["major"]),
        int(match["minor"]),
        int(match["patch"]),
        0 if match["rc"] else 1,
        int(match["rc"] or 0),
    )


def is_downgrade(target: str, deployed: str | None) -> bool:
    """目标版本低于已部署版本才算降级；同版本不算。"""
    if deployed is None:
        return False
    return release_version_key(target) < release_version_key(deployed)


def _valid_tag(value: object) -> str | None:
    return value if isinstance(value, str) and RELEASE_TAG.fullmatch(value) else None


def _higher_tag(first: str | None, second: str | None) -> str | None:
    if first is None or second is None:
        return first or second
    return second if is_downgrade(first, second) else first


def _release_tag(item: object) -> str | None:
    if not isinstance(item, dict):
        return None
    value = item.get("tagName", item.get("tag_name", item.get("tag")))
    return value if isinstance(value, str) else None


def _release_prerelease(item: object) -> bool | None:
    if not isinstance(item, dict):
        return None
    value = item.get("isPrerelease", item.get("prerelease"))
    return value if type(value) is bool else None


def select_release(releases: list, environment: str) -> dict | None:
    """按语义版本选择唯一环境候选；不按字符串排序，也不跨环境放行。"""
    if environment not in ("stage", "production"):
        raise AgentError("host_environment")
    matches = []
    for item in releases:
        tag = _release_tag(item)
        prerelease = _release_prerelease(item)
        if not tag or prerelease is None or not isinstance(item, dict):
            continue
        if item.get("isDraft", item.get("draft", False)) is True:
            continue
        match = RELEASE_TAG.fullmatch(tag)
        if not match:
            continue
        candidate = bool(match["rc"])
        wanted = environment == "stage"
        if candidate is not wanted or prerelease is not wanted:
            continue
        matches.append(item)
    if not matches:
        return None
    return max(
        matches,
        key=lambda item: (
            release_version_key(_release_tag(item)),
            str(item.get("publishedAt", item.get("published_at", ""))),
        ),
    )


def _list_releases(config: dict) -> list:
    raw = _run_command(
        _command_argv(
            config["gh_command"],
            "release",
            "list",
            "--repo",
            config["repository"],
            "--limit",
            "100",
            "--json",
            ",".join(RELEASE_LIST_JSON_FIELDS),
        ),
        timeout=config["poll_timeout_seconds"],
        env=_gh_env(config),
    )
    try:
        releases = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AgentError("release_list_unavailable") from None
    if not isinstance(releases, list):
        raise AgentError("release_list_unavailable")
    return releases


def _validate_manifest(doc: object, repository: str, tag: str) -> dict:
    if not isinstance(doc, dict) or doc.get("schema") != 2:
        raise AgentError("release_manifest_invalid")
    if doc.get("repository") != repository or doc.get("tag") != tag:
        raise AgentError("release_manifest_invalid")
    images = doc.get("images")
    if not isinstance(images, dict) or set(images) != set(SERVICES):
        raise AgentError("release_manifest_invalid")
    for value in images.values():
        if (
            not isinstance(value, str)
            or "@" not in value
            or not IMAGE_DIGEST.fullmatch(value.rsplit("@", 1)[1])
        ):
            raise AgentError("release_manifest_invalid")
    bundle = doc.get("control_bundle")
    if (
        not isinstance(bundle, dict)
        or not SHA256.fullmatch(bundle.get("sha256", ""))
        or not SHA256.fullmatch(bundle.get("index_sha256", ""))
        or bundle.get("asset") != "lingxi-control.tar"
    ):
        raise AgentError("release_manifest_invalid")
    if type(doc.get("run_id")) is not int or doc["run_id"] <= 0:
        raise AgentError("release_manifest_invalid")
    return doc


def _resolve_manifest(
    script: str | Path,
    config: dict,
    repository: str,
    tag: str,
    environment: str,
    directory: Path,
) -> dict:
    output = directory / "images.env"
    manifest_output = directory / "release-manifest.json"
    raw = _run_command(
        _command_argv(
            script,
            "resolve",
            "--repository",
            repository,
            "--tag",
            tag,
            "--environment",
            environment,
            "--output",
            str(output),
            "--manifest-output",
            str(manifest_output),
        ),
        timeout=config["poll_timeout_seconds"],
        env=_gh_env(config),
    )
    del raw
    try:
        doc = json.loads(manifest_output.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise AgentError("release_manifest_invalid") from None
    return _validate_manifest(doc, repository, tag)


def _run_inspect_command(argv: list[str], timeout: int) -> str | None:
    """保留 Docker 的缺失对象语义，避免把未安装误判为守护进程故障。"""
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=None,
            close_fds=True,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise AgentError("docker_inspect_unavailable") from None
    if result.returncode == 0:
        return result.stdout
    if result.returncode == 1 and "No such" in result.stderr:
        return None
    raise AgentError("docker_inspect_unavailable")


def _parse_repo_digests(raw: str) -> list[str]:
    try:
        values = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AgentError("docker_inspect_unavailable") from None
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        raise AgentError("docker_inspect_unavailable")
    digests = []
    for value in values:
        if not isinstance(value, str) or "@" not in value:
            continue
        digest = value.rsplit("@", 1)[1]
        if IMAGE_DIGEST.fullmatch(digest):
            digests.append(digest)
    return digests


def _container_digest(host: dict, image_id: object, config_image: object, timeout: int) -> str:
    if not isinstance(image_id, str) or not image_id:
        return ""
    raw = _run_inspect_command(
        [
            host["docker"],
            "image",
            "inspect",
            "--format",
            "{{json .RepoDigests}}",
            image_id,
        ],
        timeout,
    )
    if raw is None:
        return ""
    digests = _parse_repo_digests(raw)
    if not digests:
        return ""
    if isinstance(config_image, str) and "@" in config_image:
        configured = config_image.rsplit("@", 1)[1]
        if configured in digests:
            return configured
    return digests[0] if len(set(digests)) == 1 else ""


def _inspect_running_digests(host: dict, timeout: int) -> dict[str, str]:
    """只把三个运行中服务容器的镜像 RepoDigest作为当前事实。"""
    try:
        raw = _run_command(
            [
                host["docker"],
                "ps",
                "-aq",
                "--filter",
                f"label=com.docker.compose.project={host['project']}",
            ],
            timeout=timeout,
            env=None,
        )
    except AgentError:
        raise AgentError("docker_inspect_unavailable") from None
    result = {service: "" for service in RUNNING_SERVICES}
    seen = set()
    for identifier in raw.split():
        inspected = _run_inspect_command(
            [
                host["docker"],
                "inspect",
                identifier,
                "--format",
                "{{json .Config.Labels}}\n{{json .Config.Image}}\n{{json .State}}\n{{json .Image}}",
            ],
            timeout,
        )
        if inspected is None:
            continue
        lines = inspected.splitlines()
        if len(lines) != 4:
            raise AgentError("docker_inspect_unavailable")
        try:
            labels, config_image, state, image_id = (json.loads(line) for line in lines)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise AgentError("docker_inspect_unavailable") from None
        if not isinstance(labels, dict) or not isinstance(state, dict):
            raise AgentError("docker_inspect_unavailable")
        service = labels.get("com.docker.compose.service")
        if service not in RUNNING_SERVICES:
            continue
        if service in seen:
            raise AgentError("docker_inspect_unavailable")
        seen.add(service)
        if type(state.get("Running")) is not bool:
            raise AgentError("docker_inspect_unavailable")
        if state["Running"]:
            result[service] = _container_digest(host, image_id, config_image, timeout)
    return result


def _inspect_digests(host: dict, manifest: dict, timeout: int) -> dict[str, str]:
    """按运行容器回读镜像摘要；清单只在比较时使用。"""
    del manifest
    return _inspect_running_digests(host, timeout)


def _expected_digests(manifest: dict) -> dict[str, str]:
    """把清单按三个常驻服务容器展开成可与运行回读逐项比较的摘要。"""
    return {
        service: manifest["images"][MANIFEST_SERVICE_FOR_CONTAINER[service]].rsplit("@", 1)[1]
        for service in RUNNING_SERVICES
    }


def _state_digests(state: dict) -> dict[str, str] | None:
    """状态账记录的已验证摘要；形状不完整时视为没有记录。"""
    value = state.get("verified_digests")
    if (
        not isinstance(value, dict)
        or set(value) != set(RUNNING_SERVICES)
        or any(
            not isinstance(item, str) or not IMAGE_DIGEST.fullmatch(item) for item in value.values()
        )
    ):
        return None
    return dict(value)


def _all_digests_match(actual: dict[str, str], manifest: dict) -> bool:
    """三个常驻服务必须逐项命中；拉在本机但未运行不算在位。"""
    if set(actual) != set(RUNNING_SERVICES):
        return False
    return actual == _expected_digests(manifest)


def _bundle_metadata(doc: dict) -> dict:
    return dict(doc["control_bundle"])


def _load_module(path: Path):
    name = "lingxi_pull_bundle_" + hashlib.sha256(str(path).encode()).hexdigest()[:16]
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except (ImportError, OSError, SyntaxError, AttributeError):
        return None


def _copy_stable_package(path: Path, state_directory: Path, expected_sha: str) -> Path:
    packages = state_directory / "packages"
    if not packages.exists():
        try:
            packages.mkdir(mode=0o700)
        except OSError:
            raise AgentError("package_directory_unavailable") from None
    _private_directory(packages)
    destination = packages / (expected_sha + ".tar")
    if destination.exists():
        try:
            if destination.is_symlink() or stat.S_IMODE(destination.stat().st_mode) != 0o600:
                raise AgentError("package_permissions")
            if hashlib.sha256(destination.read_bytes()).hexdigest() != expected_sha:
                raise BundleDigestMismatchError()
        except OSError:
            raise AgentError("package_unavailable") from None
        return destination
    try:
        fd, temporary = tempfile.mkstemp(prefix=".package-", dir=packages)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(path.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(packages, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        raise AgentError("package_write_failed") from None
    finally:
        if "temporary" in locals():
            Path(temporary).unlink(missing_ok=True)
    return destination


def _download_bundle(
    config: dict,
    tag: str,
    metadata: dict,
    state_directory: Path,
    directory: Path,
) -> Path:
    _run_command(
        _command_argv(
            config["gh_command"],
            "release",
            "download",
            tag,
            "--repo",
            config["repository"],
            "--pattern",
            "lingxi-control.tar",
            "--pattern",
            "control-index.json",
            "--dir",
            str(directory),
            "--clobber",
        ),
        timeout=config["poll_timeout_seconds"],
        env=_gh_env(config),
    )
    package = directory / "lingxi-control.tar"
    index = directory / "control-index.json"
    try:
        if hashlib.sha256(package.read_bytes()).hexdigest() != metadata["sha256"]:
            raise BundleDigestMismatchError()
        if hashlib.sha256(index.read_bytes()).hexdigest() != metadata["index_sha256"]:
            raise BundleDigestMismatchError()
    except FileNotFoundError:
        raise BundleDigestMismatchError() from None
    # 先返回临时文件；只有控制包工具完整核对通过后才持久化，篡改包不能留下
    # 看似可接续的稳定副本。
    del state_directory
    return package


def _run_bundle_tool_cli(
    config: dict,
    action: str,
    package: Path,
    metadata: dict,
    index_path: Path,
    root: Path | None = None,
) -> None:
    """兼容控制包工具的命令行适配层；正式工具优先走 Python 入口。"""
    try:
        fd, expected_path = tempfile.mkstemp(prefix=".bundle-expected-", dir=index_path.parent)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(metadata))
            stream.flush()
            os.fsync(stream.fileno())
        argv = _command_argv(
            config["control_bundle_tool"],
            action,
            "--package",
            str(package),
            "--expected",
            expected_path,
        )
        if root is not None:
            argv.extend(["--root", str(root)])
        _run_command(argv, timeout=config["poll_timeout_seconds"], env=None)
    except OSError:
        raise AgentError("control_bundle_tool_unavailable") from None
    finally:
        if "expected_path" in locals():
            Path(expected_path).unlink(missing_ok=True)


def _verify_bundle(config: dict, package: Path, metadata: dict, index_path: Path):
    """调用固定控制包工具核对包和外部索引，不写安装目录。"""
    module = _load_module(Path(config["control_bundle_tool"]))
    if module is None or not callable(getattr(module, "verify", None)):
        _run_bundle_tool_cli(config, "verify", package, metadata, index_path)
        return None
    try:
        verification = module.verify(package, metadata)
    except Exception:
        raise BundleDigestMismatchError() from None
    if isinstance(verification, tuple) and len(verification) == 2:
        _index, contents = verification
        try:
            if contents.get("control-index.json", b"") != index_path.read_bytes():
                raise BundleDigestMismatchError()
        except (AttributeError, OSError):
            raise BundleDigestMismatchError() from None
    return module


def _verify_and_install_bundle(
    config: dict,
    host: dict,
    package: Path,
    metadata: dict,
    index_path: Path,
) -> Path:
    """只调用控制包自身的校验/安装入口，不在代理里解包。"""
    module = _verify_bundle(config, package, metadata, index_path)
    root = Path(host["bundle_root"])
    if module is None or not callable(getattr(module, "install", None)):
        _run_bundle_tool_cli(config, "install", package, metadata, index_path, root)
        target = root / metadata["sha256"]
    else:
        try:
            target = module.install(package, metadata, root)
        except Exception:
            raise AgentError("control_bundle_install_failed") from None
    if target is None:
        target = root / metadata["sha256"]
    target = Path(target)
    try:
        if (
            target.is_symlink()
            or not target.is_dir()
            or target.resolve().parent != root.resolve()
            or not (target / "deploy/lingxi_deploy.py").is_file()
            or not (target / "scripts/ci/release_manifest.py").is_file()
        ):
            raise AgentError("control_bundle_install_failed")
    except OSError:
        raise AgentError("control_bundle_install_failed") from None
    return target


def _load_plan(path: Path) -> dict:
    if not path.exists():
        raise AgentError("plan_unavailable")
    raw = _read_json(path, private=True)
    if not isinstance(raw, dict) or not PLAN_ID.fullmatch(str(raw.get("id", ""))):
        raise AgentError("plan_shape")
    return raw


def _previous_plan(state: dict, state_directory: Path) -> dict | None:
    identifier = state.get("plan_id")
    if not isinstance(identifier, str) or not PLAN_ID.fullmatch(identifier):
        return None
    path = state_directory / f"{identifier}.plan.json"
    if not path.exists():
        return None
    try:
        return _load_plan(path)
    except AgentError:
        return None


def _plan_is_continuable(state: dict, plan: dict, tag: str) -> bool:
    """中断状态只沿用同一计划 ID，禁止借接续名义重新 plan。

    已验证目标被外部改动后，同一目标不再沿旧计划接续：每轮只回读等待人工核对。
    """
    return (
        state.get("deployer_state") in {"planned", "running", "unknown"}
        and state.get("external_change_tag") != tag
        and state.get("plan_id") == plan.get("id")
        and isinstance(plan.get("new"), dict)
        and plan["new"].get("tag") == tag
    )


def _read_public_material(path: Path) -> dict:
    raw = _read_json(path, private=False)
    if not isinstance(raw, dict):
        raise AgentError("deployment_materials_unavailable")
    return raw


def _host_materials(host: dict, config: dict) -> tuple[dict, dict, dict]:
    """从既有安装收据、binding 和 relay 收集计划所需的非秘密通道事实。"""
    root = Path(host["config_root"])
    installation = _read_json(root / INSTALLATION_RECEIPT_NAME, private=True)
    binding = _read_public_material(root / "innertest" / "binding.json")
    current = Path(host["relay_root"]) / "current"
    if not current.is_symlink():
        raise AgentError("deployment_materials_unavailable")
    link = os.readlink(current)
    if Path(link).is_absolute() or ".." in PurePosixPath(link).parts:
        raise AgentError("deployment_materials_unavailable")
    relay_target = current.parent / link
    relay = _read_public_material(relay_target / "innertest-relay.json")
    receipt = _read_public_material(relay_target / "installation.json")
    if (
        installation.get("schema") != 1
        or not isinstance(installation.get("checks"), dict)
        or binding.get("schema_revision") != 1
        or relay.get("schema_revision") != 1
        or receipt.get("relay_sha256") is None
    ):
        raise AgentError("deployment_materials_unavailable")
    channel = {
        "schema_revision": 1,
        "protocol": "2025-11-25",
        "relay_sha256": receipt["relay_sha256"],
        "socket_path": relay.get("socket_path"),
        "socket_mode": 0o660,
        "directory_mode": 0o750,
        "binding_version": installation.get("binding_version"),
        "uid_map_sha256": binding.get("uid_map_sha256"),
        "host_uid": binding.get("host_uid"),
        "peer_uid": binding.get("peer_uid"),
        "scheduler_uid": relay.get("socket_owner_uid"),
        "socket_gid": binding.get("socket_gid"),
        "socket_owner_uid": relay.get("socket_owner_uid"),
        "installation_receipt_sha256": fingerprint(installation),
    }
    required = (
        "relay_sha256",
        "socket_path",
        "binding_version",
        "uid_map_sha256",
        "host_uid",
        "peer_uid",
        "scheduler_uid",
        "socket_gid",
        "socket_owner_uid",
    )
    if any(channel.get(key) is None for key in required):
        raise AgentError("deployment_materials_unavailable")
    return installation, binding, {"channel": channel, "receipt": receipt}


def _build_request(
    host: dict,
    public_config: dict,
    old: dict,
    new: dict,
    package_paths: dict[str, Path],
    source: str,
    previous: dict | None,
    deploy_timeout_seconds: int,
    installation: dict,
) -> dict:
    """组装部署请求；收据指纹与绑定版本一律取自刚刷新的安装收据，不沿用旧计划的值。"""
    if previous is not None:
        resources = dict(previous["resources"])
        channel = dict(previous["channel"])
        recovery = dict(previous["recovery"])
        recovery["target_manifest_sha256"] = fingerprint(old)
        recovery["config_sha256"] = fingerprint(public_config)
        # 收据每装一个新包就变一次；旧计划里的指纹对应的是上一版的收据。
        channel["installation_receipt_sha256"] = fingerprint(installation)
        channel["binding_version"] = installation["binding_version"]
    else:
        on_disk, binding, relay = _host_materials(host, public_config)
        del binding
        if on_disk != installation:
            raise AgentError("deployment_materials_unavailable")
        channel = relay["channel"]
        package_size = sum(path.stat().st_size for path in package_paths.values())
        resources = {
            "required_free_bytes": max(1, package_size * 2),
            "evidence_sha256": fingerprint(
                {"deploy_root": host["deploy_root"], "observed_at": int(time.time())}
            ),
        }
        recovery = {
            "compatible": True,
            "evidence_sha256": fingerprint({"old": old["tag"], "new": new["tag"]}),
            "target_manifest_sha256": fingerprint(old),
            "credential_source": "host-private-env",
            "permissions_sha256": fingerprint(installation.get("checks", {})),
            "config_sha256": fingerprint(public_config),
            "historical": None,
        }
    acceptance_sha = new.get("promotion", {}).get("acceptance_sha256")
    if not isinstance(acceptance_sha, str) or not SHA256.fullmatch(acceptance_sha):
        acceptance_sha = fingerprint(
            {"tag": new["tag"], "run_id": new["run_id"], "commit": new["commit"]}
        )
    identifier = (
        "pull-"
        + re.sub(r"[^A-Za-z0-9_-]", "-", new["tag"])[-35:]
        + "-"
        + format(time.time_ns() & 0xFFFFFFFF, "08x")
    )
    return {
        "id": identifier[:64],
        "operation": "apply",
        "recovery_of": None,
        "old": old,
        "new": new,
        "acceptance_sha256": acceptance_sha,
        "acceptance_source": source,
        "current_heads": old["migration_heads"],
        "resources": resources,
        "not_before": time.time() - 1,
        "expires_at": time.time() + max(1200, deploy_timeout_seconds + 1200),
        "drain": {"gateway": 20, "scheduler": 120},
        "recovery": recovery,
        "packages": {digest: str(path) for digest, path in package_paths.items()},
        "channel": channel,
        "approval_source": source,
    }


def _write_request(state_directory: Path, request: dict) -> Path:
    path = state_directory / (request["id"] + ".request.json")
    _atomic_write(path, request)
    return path


def _checkpoint(  # noqa: PLR0913
    state: dict,
    state_directory: Path,
    *,
    host: dict,
    repository: str,
    result: str,
    tag: str,
    release_url: str,
    plan_id: str,
    deployer_state: str,
    bundle_digest: str | None = None,
    approval_sha256: str | None = None,
    approval_source: str | None = None,
    release_record: dict | None = None,
) -> None:
    """把可接续身份先写账；这里不判断部署成败，也不发送告警。"""
    state.update(
        {
            "schema": 1,
            "host": host["host"],
            "environment": host["environment"],
            "last_run_at": timestamp(),
            "last_result": result,
            "target_tag": tag,
            "release_url": _safe_url(release_url, repository, tag),
            "plan_id": plan_id,
            "deployer_state": deployer_state,
        }
    )
    if bundle_digest is not None:
        state["bundle_digest"] = bundle_digest
    if approval_sha256 is not None:
        state["approval_sha256"] = approval_sha256
    if approval_source is not None:
        state["approval_source"] = approval_source
    if release_record is not None:
        for source, target in (
            ("run_id", "candidate_run_id"),
            ("promotion_run_id", "promotion_run_id"),
            ("release_run_id", "release_run_id"),
        ):
            if release_record.get(source) is not None:
                state[target] = release_record[source]
    _atomic_write(state_directory / "pull-agent.json", state)
    _log(
        "state",
        result,
        tag=tag,
        release_url=release_url,
        plan_id=plan_id,
        **_audit_fields(state),
    )


def _invoke_deployer(
    script: Path,
    host_path: Path,
    config: dict,
    state_directory: Path,
    operation: str,
    args: list[str],
    timeout: int,
) -> str:
    argv = _command_argv(
        script,
        "--host-contract",
        str(host_path),
        "--public-config",
        config["public_config"],
        "--state-directory",
        str(state_directory),
        operation,
        *args,
    )
    return _run_command(argv, timeout=timeout, env=None)


def _parse_deployer_plan(raw: str) -> dict:
    try:
        output = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AgentError("plan_output_invalid") from None
    if (
        not isinstance(output, dict)
        or not isinstance(output.get("plan"), dict)
        or not isinstance(output.get("plan_sha256"), str)
        or output["plan_sha256"] != fingerprint(output["plan"])
    ):
        raise AgentError("plan_output_invalid")
    plan = output["plan"]
    if not PLAN_ID.fullmatch(str(plan.get("id", ""))):
        raise AgentError("plan_output_invalid")
    return plan


def _ensure_plan_file(state_directory: Path, plan: dict) -> Path:
    path = state_directory / f"{plan['id']}.plan.json"
    if path.exists():
        existing = _load_plan(path)
        if existing != plan:
            raise AgentError("plan_output_invalid")
    else:
        _atomic_write(path, plan)
    return path


def validate_approval(plan: dict, approval: object, now: float | None = None) -> str:
    """按部署器的完整时间窗核对批准，保留批准时刻的指纹稳定性。"""
    if now is None:
        now = time.time()
    keys = {"schema", "plan_id", "plan_sha256", "operation", "source", "approved_at", "expires_at"}
    if not isinstance(approval, dict) or set(approval) != keys or approval.get("schema") != 1:
        raise AgentError("approval_shape")
    if (
        approval.get("plan_id") != plan.get("id")
        or approval.get("plan_sha256") != fingerprint(plan)
        or approval.get("operation") != plan.get("operation")
        or approval.get("source") != plan.get("approval_source")
    ):
        raise AgentError("approval_mismatch")
    approved_at = approval.get("approved_at")
    expires_at = approval.get("expires_at")
    not_before = plan.get("not_before")
    plan_expires_at = plan.get("expires_at")
    if any(
        type(value) not in (int, float)
        for value in (not_before, approved_at, now, expires_at, plan_expires_at)
    ):
        raise AgentError("approval_expired")
    if not (not_before <= approved_at <= now <= expires_at <= plan_expires_at):
        raise AgentError("approval_expired")
    return fingerprint(approval)


def _existing_approval(state_directory: Path, plan: dict) -> tuple[dict, Path] | None:
    path = state_directory / (plan["id"] + ".approval.json")
    if not path.exists():
        return None
    try:
        approval = _read_json(path, private=True)
        validate_approval(plan, approval)
    except (AgentError, KeyError, TypeError):
        return None
    return approval, path


def _make_approval(plan: dict, source: str) -> dict:
    if source not in (plan.get("approval_source"),):
        raise AgentError("approval_source_mismatch")
    approved_at = time.time()
    expires_at = plan.get("expires_at")
    not_before = plan.get("not_before")
    if (
        type(not_before) not in (int, float)
        or type(expires_at) not in (int, float)
        or not_before > approved_at
        or approved_at > expires_at
    ):
        raise AgentError("plan_expired")
    approval = {
        "schema": 1,
        "plan_id": plan["id"],
        "plan_sha256": fingerprint(plan),
        "operation": plan["operation"],
        "source": source,
        "approved_at": approved_at,
        "expires_at": expires_at,
    }
    return approval


def _write_approval(state_directory: Path, approval: dict) -> Path:
    path = state_directory / (approval["plan_id"] + ".approval.json")
    _atomic_write(path, approval)
    return path


def _hook_program_protected(path: str) -> bool:
    """钩子程序及其全部祖先目录须为 root 属主、无组 / 其他写权限且非符号链接。

    与受限通道自检同一规则：任何一级可被非 root 主体改写，钩子就可能被替换。
    """
    program = Path(path)
    try:
        for item in (program, *program.parents):
            info = os.lstat(item)
            if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
                return False
    except OSError:
        return False
    return True


def _run_hook(
    path: str,
    host_path: Path,
    config_path: Path,
    state_directory: Path,
    plan_path: Path,
    timeout: int,
) -> None:
    _run_command(
        _command_argv(
            path,
            "--host-contract",
            str(host_path),
            "--config",
            str(config_path),
            "--state-directory",
            str(state_directory),
            "--plan",
            str(plan_path),
        ),
        timeout=timeout,
        env=None,
    )


def _parse_status(raw: str) -> str:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AgentError("deployer_status_unavailable") from None
    status = value.get("status") if isinstance(value, dict) else None
    if status not in DEPLOYER_STATES:
        raise AgentError("deployer_status_unavailable")
    return status


def _parse_env_file(path: Path) -> dict[str, str]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise AgentError("alert_env_file_unavailable") from None
    values = {}
    for line_number, raw_line in enumerate(raw.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise AgentError("alert_env_file_invalid")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if not key or "\x00" in value:
            raise AgentError(f"alert_env_file_invalid_{line_number}")
        values[key] = value
    missing = [key for key in ALERT_ENV_KEYS if not values.get(key)]
    if missing:
        raise AgentError("alert_env_file_missing_keys")
    if not values["LINGXI_ADMIN_GROUP_CHAT_ID"].startswith("oc_") or any(
        char.isspace() for char in values["LINGXI_ADMIN_GROUP_CHAT_ID"]
    ):
        raise AgentError("alert_env_file_invalid_chat")
    return values


def _load_alert_credentials(path: Path) -> dict[str, str]:
    """与 host_health_alert.py 相同的 0600、属主和三个字段校验。"""
    try:
        info = path.stat()
    except OSError:
        raise AgentError("alert_env_file_unavailable") from None
    if stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
        raise AgentError("alert_env_file_permissions")
    return _parse_env_file(path)


def _feishu_token(app_id: str, app_secret: str, timeout: int) -> str:
    request = urllib.request.Request(
        FEISHU_BASE_URL + "/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode(),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        raise AgentError("alert_delivery_failed") from None
    if not isinstance(payload, dict) or payload.get("code") not in (None, 0, "0"):
        raise AgentError("alert_delivery_failed")
    token = payload.get("tenant_access_token")
    if not isinstance(token, str) or not token:
        raise AgentError("alert_delivery_failed")
    return token


def send_alert(message: str, env_file: Path, timeout_seconds: int) -> None:
    """向受控管理群发纯文本；同 host_health_alert.py，不打印响应原文。"""
    credentials = _load_alert_credentials(env_file)
    token = _feishu_token(
        credentials["LINGXI_FEISHU_APP_ID"],
        credentials["LINGXI_FEISHU_APP_SECRET"],
        timeout_seconds,
    )
    body = json.dumps(
        {
            "receive_id": credentials["LINGXI_ADMIN_GROUP_CHAT_ID"],
            "msg_type": "text",
            "content": json.dumps({"text": message}, ensure_ascii=False),
        },
        ensure_ascii=False,
    ).encode()
    request = urllib.request.Request(
        FEISHU_BASE_URL + "/im/v1/messages?receive_id_type=chat_id",
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": "Bearer " + token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode())
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        raise AgentError("alert_delivery_failed") from None
    if not isinstance(payload, dict) or payload.get("code") not in (None, 0, "0"):
        raise AgentError("alert_delivery_failed")


def _alert_message(
    host: dict,
    tag: str | None,
    stage: str,
    result: str,
    plan_id: str | None,
    deployed_tag: str | None = None,
) -> str:
    """告警正文只含合同字段，不带 URL、凭据和日志原文；降级拒绝另附已部署版本。"""
    fields = [
        ("主机", host["host"]),
        ("环境", host["environment"]),
        ("标签", tag or "未知"),
        ("阶段", stage),
        ("结果码", result),
        ("计划 ID", plan_id or "未知"),
    ]
    if deployed_tag is not None:
        fields.append(("最高已部署版本", deployed_tag))
    return "\n".join(f"{key}：{value}" for key, value in fields)


def _alert_key(result: str, tag: str | None, deployed_tag: str | None = None) -> str:
    key = result + ":" + (tag or "none")
    return key if deployed_tag is None else key + ":" + deployed_tag


def _audit_fields(state: dict) -> dict[str, object]:
    return {
        key: state[key]
        for key in (
            "candidate_run_id",
            "promotion_run_id",
            "release_run_id",
            "approval_sha256",
        )
        if state.get(key) is not None
    }


def _send_once(
    state: dict,
    key: str,
    message: str,
    *,
    active: bool,
    config: dict,
    state_directory: Path,
) -> None:
    alerts = state.setdefault("alerts", {})
    record = alerts.setdefault(key, {})
    if record.get("sent") is True and (not active or record.get("active") is True):
        return
    try:
        send_alert(message, Path(config["alert_env_file"]), config["poll_timeout_seconds"])
    except Exception:
        record.update({"sent": False, "delivery_failed": True, "active": active})
        raise AgentError("alert_delivery_failed") from None
    record.update(
        {
            "sent": True,
            "active": active,
            "last_sent_at": timestamp(),
            "delivery_failed": False,
        }
    )
    if not active:
        record["recovered_at"] = timestamp()


def _finish(  # noqa: PLR0913
    state: dict,
    host: dict,
    config: dict,
    state_directory: Path,
    *,
    result: str,
    stage: str,
    tag: str | None = None,
    release_url: str | None = None,
    plan_id: str | None = None,
    deployer_state: str | None = None,
    approval_sha256: str | None = None,
    bundle_digest: str | None = None,
    approval_source: str | None = None,
    release_record: dict | None = None,
    deployed_tag: str | None = None,
    verified_digests: dict[str, str] | None = None,
    remember_target: bool = True,
) -> int:
    """收口一轮：写状态账并按结果码告警。

    ``deployed_tag`` 只会抬高、不会降低状态账的最高已部署版本；降级拒绝时它同时进入
    去重键和正文。``remember_target=False`` 让被拒目标不覆盖状态账里已在位的目标。
    """
    next_state = dict(state)
    next_state.update(
        {
            "schema": 1,
            "host": host["host"],
            "environment": host["environment"],
            "last_run_at": timestamp(),
            "last_result": result,
        }
    )
    if result in FAILURE_CODES:
        next_state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1
    else:
        next_state["consecutive_failures"] = 0
    logged_url = next_state.get("release_url")
    if release_url is not None:
        logged_url = _safe_url(release_url, config["repository"], tag or "unknown")
    if tag is not None and remember_target:
        next_state["target_tag"] = tag
    if release_url is not None and remember_target:
        next_state["release_url"] = logged_url
    if plan_id is not None:
        next_state["plan_id"] = plan_id
    if deployer_state is not None:
        next_state["deployer_state"] = deployer_state
    if approval_sha256 is not None:
        next_state["approval_sha256"] = approval_sha256
    if bundle_digest is not None:
        next_state["bundle_digest"] = bundle_digest
    if approval_source is not None:
        next_state["approval_source"] = approval_source
    if deployed_tag is not None:
        next_state["highest_deployed_tag"] = _higher_tag(
            _valid_tag(state.get("highest_deployed_tag")), deployed_tag
        )
    if verified_digests is not None:
        next_state["verified_digests"] = dict(verified_digests)
    if result in HEALTHY_CODES:
        next_state["external_change_tag"] = None
    elif result == "external_change_detected":
        next_state["external_change_tag"] = tag
    if release_record is not None:
        for source, target in (
            ("run_id", "candidate_run_id"),
            ("promotion_run_id", "promotion_run_id"),
            ("release_run_id", "release_run_id"),
        ):
            if release_record.get(source) is not None:
                next_state[target] = release_record[source]
    _log(
        stage,
        result,
        tag=tag,
        release_url=logged_url,
        plan_id=plan_id,
        **_audit_fields(next_state),
    )

    try:
        alerts = next_state.setdefault("alerts", {})
        # 先处理已确认的故障恢复，再发送本轮新的成功/故障通知。
        if result in HEALTHY_CODES:
            for key, record in list(alerts.items()):
                if not isinstance(record, dict) or record.get("active") is not True:
                    continue
                recovery_message = _alert_message(host, tag, stage, "recovered", plan_id)
                _send_once(
                    next_state,
                    key + ":recovery",
                    recovery_message,
                    active=False,
                    config=config,
                    state_directory=state_directory,
                )
                record["active"] = False
        should_alert = False
        if result in IMMEDIATE_ALERT_CODES:
            should_alert = True
        elif result == "release_list_unavailable":
            should_alert = next_state["consecutive_failures"] >= 3
        elif result == "verified" and config["notify_on_success"]:
            should_alert = True
        if should_alert:
            alert_tag = tag or next_state.get("target_tag")
            detail = deployed_tag if result == "downgrade_refused" else None
            key = _alert_key(result, alert_tag, detail)
            _send_once(
                next_state,
                key,
                _alert_message(host, alert_tag, stage, result, plan_id, detail),
                active=result in IMMEDIATE_ALERT_CODES or result == "release_list_unavailable",
                config=config,
                state_directory=state_directory,
            )
            _log("alert", "sent", tag=alert_tag, plan_id=plan_id)
    except AgentError as error:
        next_state["last_result"] = "alert_delivery_failed"
        next_state["alert_delivery_error"] = error.code
        _atomic_write(state_directory / "pull-agent.json", next_state)
        _log("alert", "alert_delivery_failed", tag=tag, plan_id=plan_id)
        return 1
    _atomic_write(state_directory / "pull-agent.json", next_state)
    _log("state", result, tag=tag, plan_id=plan_id)
    return 0


def _discover_current_release(
    releases: list,
    environment: str,
    target_tag: str,
    actual: dict[str, str],
    script: str,
    config: dict,
    directory: Path,
) -> dict | None:
    """仅用三个已回读 digest 在已列出的 Release 中找当前版本。"""
    candidates = []
    for item in releases:
        tag = _release_tag(item)
        if (
            not tag
            or tag == target_tag
            or not RELEASE_TAG.fullmatch(tag)
            or item.get("isDraft", item.get("draft", False)) is True
            or (environment == "production" and _release_prerelease(item) is not False)
        ):
            continue
        candidates.append(item)
    candidates.sort(key=lambda item: release_version_key(_release_tag(item)), reverse=True)
    for item in candidates:
        tag = _release_tag(item)
        if tag is None:
            continue
        try:
            manifest = _resolve_manifest(
                script, config, config["repository"], tag, environment, directory
            )
        except AgentError:
            continue
        if all(
            actual.get(service) == manifest["images"][manifest_service].rsplit("@", 1)[1]
            for service, manifest_service in MANIFEST_SERVICE_FOR_CONTAINER.items()
        ):
            return manifest
    return None


def _refuse_downgrade(
    state: dict,
    host: dict,
    config: dict,
    state_directory: Path,
    tag: str,
    release_url: str,
    deployed_tag: str,
) -> int:
    """目标低于已部署版本时不 plan、不下载；回退只能走部署器独立批准的恢复路径。"""
    _log(
        "version_order",
        "downgrade_refused",
        tag=tag,
        release_url=release_url,
        deployed=deployed_tag,
    )
    return _finish(
        state,
        host,
        config,
        state_directory,
        result="downgrade_refused",
        stage="version_order",
        tag=tag,
        release_url=release_url,
        deployed_tag=deployed_tag,
        remember_target=False,
    )


def _recheck_verified_target(
    state: dict,
    host: dict,
    config: dict,
    state_directory: Path,
    tag: str,
    release_url: str,
) -> int:
    """已验证目标每轮仍回读运行容器；不一致只告警等待人工核对，不自动重部署。

    回读本身失败记 ``unknown`` 但不改部署器状态，下一轮继续回读；外部改动持续期间
    每轮只比对，直到容器回到已验证摘要或出现新的目标。
    """
    expected = _state_digests(state)
    try:
        if expected is None:
            with tempfile.TemporaryDirectory(prefix=".release-", dir=state_directory) as name:
                manifest = _resolve_manifest(
                    config["release_manifest"],
                    config,
                    config["repository"],
                    tag,
                    host["environment"],
                    Path(name),
                )
            expected = _expected_digests(manifest)
        actual = _inspect_running_digests(host, config["poll_timeout_seconds"])
    except AgentError:
        _log("idempotence", "unknown", tag=tag, release_url=release_url)
        return _finish(
            state,
            host,
            config,
            state_directory,
            result="unknown",
            stage="idempotence",
            tag=tag,
            release_url=release_url,
        )
    if actual == expected:
        _log("idempotence", "already_in_place", tag=tag, release_url=release_url)
        return _finish(
            state,
            host,
            config,
            state_directory,
            result="already_in_place",
            stage="idempotence",
            tag=tag,
            release_url=release_url,
            deployer_state="verified",
            deployed_tag=tag,
            verified_digests=expected,
        )
    _log("idempotence", "external_change_detected", tag=tag, release_url=release_url)
    return _finish(
        state,
        host,
        config,
        state_directory,
        result="external_change_detected",
        stage="idempotence",
        tag=tag,
        release_url=release_url,
        deployer_state="unknown",
    )


def _run_locked(
    host_path: Path, config_path: Path, state_directory: Path, host: dict, config: dict
) -> int:
    state = _load_state(state_directory / "pull-agent.json", host)
    try:
        releases = _list_releases(config)
        selected = select_release(releases, host["environment"])
    except (AgentError, OSError):
        _log("release_list", "release_list_unavailable")
        return _finish(
            state,
            host,
            config,
            state_directory,
            result="release_list_unavailable",
            stage="release_list",
        )
    if selected is None:
        _log("release_list", "release_list_unavailable")
        return _finish(
            state,
            host,
            config,
            state_directory,
            result="release_list_unavailable",
            stage="release_list",
        )
    tag = _release_tag(selected)
    release_url = _safe_url(None, config["repository"], tag)
    release_audit = {
        "run_id": None,
        "promotion_run_id": None,
        "release_run_id": selected.get("run_id", selected.get("runId")),
    }
    _log(
        "release_list",
        "selected",
        tag=tag,
        release_url=release_url,
        release_run_id=release_audit["release_run_id"],
    )
    previous = _previous_plan(state, state_directory)
    continuing = _plan_is_continuable(state, previous or {}, tag)
    highest_deployed = _valid_tag(state.get("highest_deployed_tag"))
    if not continuing and is_downgrade(tag, highest_deployed):
        return _refuse_downgrade(
            state, host, config, state_directory, tag, release_url, highest_deployed
        )
    if (
        not continuing
        and state.get("target_tag") == tag
        and (state.get("deployer_state") == "verified" or state.get("external_change_tag") == tag)
    ):
        return _recheck_verified_target(state, host, config, state_directory, tag, release_url)

    with tempfile.TemporaryDirectory(prefix=".release-", dir=state_directory) as temporary_name:
        temporary = Path(temporary_name)
        new_directory = temporary / "new"
        old_directory = temporary / "old"
        new_directory.mkdir(mode=0o700)
        old_directory.mkdir(mode=0o700)
        try:
            configured_manifest = _resolve_manifest(
                config["release_manifest"],
                config,
                config["repository"],
                tag,
                host["environment"],
                temporary,
            )
            release_audit["run_id"] = configured_manifest.get("run_id")
            release_audit["promotion_run_id"] = configured_manifest.get("promotion", {}).get(
                "run_id"
            )
            actual = (
                None
                if continuing
                else _inspect_digests(host, configured_manifest, config["poll_timeout_seconds"])
            )
        except AgentError:
            _log("idempotence", "unknown", tag=tag, release_url=release_url)
            return _finish(
                state,
                host,
                config,
                state_directory,
                result="unknown",
                stage="idempotence",
                tag=tag,
                release_url=release_url,
                deployer_state="unknown",
                release_record=release_audit,
            )
        if not continuing and _all_digests_match(actual, configured_manifest):
            _log("idempotence", "already_in_place_external", tag=tag, release_url=release_url)
            return _finish(
                state,
                host,
                config,
                state_directory,
                result="already_in_place_external",
                stage="idempotence",
                tag=tag,
                release_url=release_url,
                deployer_state="verified",
                release_record=release_audit,
                deployed_tag=tag,
                verified_digests=_expected_digests(configured_manifest),
            )
        if continuing:
            old_manifest = previous.get("old") if isinstance(previous, dict) else None
        else:
            old_manifest = None
            if state.get("deployer_state") == "verified" and isinstance(previous, dict):
                # 外部在位只更新 target_tag，不换 plan_id：此时该计划的 new 已不是
                # 运行中的版本，只有 new.tag 仍等于状态账 target_tag 才可作为 old。
                candidate = previous.get("new")
                if isinstance(candidate, dict) and candidate.get("tag") == state.get("target_tag"):
                    old_manifest = candidate
            if old_manifest is None:
                try:
                    old_manifest = _discover_current_release(
                        releases,
                        host["environment"],
                        tag,
                        actual,
                        config["release_manifest"],
                        config,
                        temporary,
                    )
                except AgentError:
                    old_manifest = None
        if not isinstance(old_manifest, dict):
            _log("current_release", "unknown", tag=tag, release_url=release_url)
            return _finish(
                state,
                host,
                config,
                state_directory,
                result="unknown",
                stage="current_release",
                tag=tag,
                release_url=release_url,
                deployer_state="unknown",
                release_record=release_audit,
            )
        # 运行中的版本也是已部署版本：状态账没记到的外部安装同样不允许被降级覆盖。
        deployed_tag = _higher_tag(highest_deployed, _valid_tag(old_manifest.get("tag")))
        if not continuing and deployed_tag is not None and is_downgrade(tag, deployed_tag):
            return _refuse_downgrade(
                state, host, config, state_directory, tag, release_url, deployed_tag
            )

        try:
            downloaded_target_package = _download_bundle(
                config,
                tag,
                _bundle_metadata(configured_manifest),
                state_directory,
                new_directory,
            )
            index_path = new_directory / "control-index.json"
            target = _verify_and_install_bundle(
                config,
                host,
                downloaded_target_package,
                _bundle_metadata(configured_manifest),
                index_path,
            )
            target_package = _copy_stable_package(
                downloaded_target_package,
                state_directory,
                configured_manifest["control_bundle"]["sha256"],
            )
        except BundleDigestMismatchError:
            _log("bundle", "bundle_digest_mismatch", tag=tag, release_url=release_url)
            return _finish(
                state,
                host,
                config,
                state_directory,
                result="bundle_digest_mismatch",
                stage="bundle",
                tag=tag,
                release_url=release_url,
                bundle_digest=configured_manifest["control_bundle"]["sha256"],
                release_record=release_audit,
            )
        except AgentError:
            _log("bundle", "unknown", tag=tag, release_url=release_url)
            return _finish(
                state,
                host,
                config,
                state_directory,
                result="unknown",
                stage="bundle",
                tag=tag,
                release_url=release_url,
                deployer_state="unknown",
                release_record=release_audit,
            )
        del target
        release_audit["run_id"] = configured_manifest.get("run_id")
        release_audit["promotion_run_id"] = configured_manifest.get("promotion", {}).get("run_id")
        try:
            installed_manifest = _resolve_manifest(
                str(
                    Path(host["bundle_root"])
                    / configured_manifest["control_bundle"]["sha256"]
                    / "scripts/ci/release_manifest.py"
                ),
                config,
                config["repository"],
                tag,
                host["environment"],
                temporary,
            )
        except AgentError:
            return _finish(
                state,
                host,
                config,
                state_directory,
                result="unknown",
                stage="frozen_manifest",
                tag=tag,
                release_url=release_url,
                deployer_state="unknown",
                bundle_digest=configured_manifest["control_bundle"]["sha256"],
                release_record=release_audit,
            )
        if canonical(installed_manifest) != canonical(configured_manifest):
            return _finish(
                state,
                host,
                config,
                state_directory,
                result="unknown",
                stage="frozen_manifest",
                tag=tag,
                release_url=release_url,
                deployer_state="unknown",
                bundle_digest=configured_manifest["control_bundle"]["sha256"],
                release_record=release_audit,
            )
        installed_deployer = (
            Path(host["bundle_root"])
            / configured_manifest["control_bundle"]["sha256"]
            / "deploy/lingxi_deploy.py"
        )
        plan = None
        if _plan_is_continuable(state, previous or {}, tag):
            plan = previous
            _log("continuation", "apply_same_plan", tag=tag, plan_id=plan["id"])
        else:
            # 新包已核对并安装：先把安装收据的 bundle_sha256 刷成新包摘要，再 plan。
            # 收据缺失、形状不符、六项人工核对不全为真或环境不符都不 plan，状态账
            # 仍指向已在位的目标，由人按引导安装 runbook 修正收据。
            try:
                installation, refreshed = refresh_installation_receipt(
                    host, configured_manifest["control_bundle"]["sha256"]
                )
            except AgentError as error:
                _log(
                    "installation_receipt",
                    "installation_receipt_unusable",
                    tag=tag,
                    release_url=release_url,
                    reason=error.code,
                )
                return _finish(
                    state,
                    host,
                    config,
                    state_directory,
                    result="installation_receipt_unusable",
                    stage="installation_receipt",
                    tag=tag,
                    release_url=release_url,
                    remember_target=False,
                )
            _log(
                "installation_receipt",
                "refreshed" if refreshed else "unchanged",
                tag=tag,
                bundle_sha256=installation["bundle_sha256"],
            )
            try:
                old_package = None
                if (
                    previous is not None
                    and previous.get("new", {}).get("tag") == old_manifest["tag"]
                ):
                    old_package_name = previous.get("packages", {}).get(
                        old_manifest["control_bundle"]["sha256"]
                    )
                    if old_package_name:
                        old_package = Path(old_package_name)
                        if not old_package.is_file():
                            old_package = None
                if old_package is None:
                    downloaded_old_package = _download_bundle(
                        config,
                        old_manifest["tag"],
                        _bundle_metadata(old_manifest),
                        state_directory,
                        old_directory,
                    )
                    _verify_bundle(
                        config,
                        downloaded_old_package,
                        _bundle_metadata(old_manifest),
                        old_directory / "control-index.json",
                    )
                    old_package = _copy_stable_package(
                        downloaded_old_package,
                        state_directory,
                        old_manifest["control_bundle"]["sha256"],
                    )
                package_paths = {
                    old_manifest["control_bundle"]["sha256"]: old_package,
                    configured_manifest["control_bundle"]["sha256"]: target_package,
                }
                request = _build_request(
                    host,
                    _read_json(config["public_config"], private=False),
                    old_manifest,
                    configured_manifest,
                    package_paths,
                    host["approval_sources"][0],
                    previous,
                    config["deploy_timeout_seconds"],
                    installation,
                )
                request_path = _write_request(state_directory, request)
                raw_plan = _invoke_deployer(
                    installed_deployer,
                    host_path,
                    config,
                    state_directory,
                    "plan",
                    ["--request", str(request_path)],
                    config["poll_timeout_seconds"],
                )
                plan = _parse_deployer_plan(raw_plan)
                _ensure_plan_file(state_directory, plan)
                _checkpoint(
                    state,
                    state_directory,
                    host=host,
                    repository=config["repository"],
                    result="planned",
                    tag=tag,
                    release_url=release_url,
                    plan_id=plan["id"],
                    deployer_state="planned",
                    bundle_digest=configured_manifest["control_bundle"]["sha256"],
                    release_record=release_audit,
                )
                _log("plan", "written", tag=tag, plan_id=plan["id"])
            except AgentError:
                return _finish(
                    state,
                    host,
                    config,
                    state_directory,
                    result="unknown",
                    stage="plan",
                    tag=tag,
                    release_url=release_url,
                    deployer_state="unknown",
                    bundle_digest=configured_manifest["control_bundle"]["sha256"],
                    release_record=release_audit,
                )
        plan_path = _ensure_plan_file(state_directory, plan)
        source = plan.get("approval_source")
        if source not in host["approval_sources"] or len(host["approval_sources"]) != 1:
            return _finish(
                state,
                host,
                config,
                state_directory,
                result="unknown",
                stage="approval",
                tag=tag,
                release_url=release_url,
                plan_id=plan.get("id"),
                deployer_state="planned",
                bundle_digest=configured_manifest["control_bundle"]["sha256"],
                release_record=release_audit,
            )
        try:
            existing = _existing_approval(state_directory, plan)
            if existing is None:
                approval = _make_approval(plan, source)
                approval_path = _write_approval(state_directory, approval)
                approval_result = "approval_written"
            else:
                approval, approval_path = existing
                approval_result = "approval_reused"
        except AgentError:
            return _finish(
                state,
                host,
                config,
                state_directory,
                result="unknown",
                stage="approval",
                tag=tag,
                release_url=release_url,
                plan_id=plan.get("id"),
                deployer_state="planned",
                bundle_digest=configured_manifest["control_bundle"]["sha256"],
                release_record=release_audit,
            )
        approval_sha = fingerprint(approval)
        _checkpoint(
            state,
            state_directory,
            host=host,
            repository=config["repository"],
            result=approval_result,
            tag=tag,
            release_url=release_url,
            plan_id=plan["id"],
            deployer_state=state.get("deployer_state")
            if state.get("deployer_state") in DEPLOYER_STATES
            else "planned",
            bundle_digest=configured_manifest["control_bundle"]["sha256"],
            approval_sha256=approval_sha,
            approval_source=source,
            release_record=release_audit,
        )
        _log("approval", approval_result, tag=tag, plan_id=plan["id"])
        if not all(_hook_program_protected(hook) for hook in config["pre_apply_hooks"]):
            _log("pre_apply_hook", "pre_apply_hook_unsafe", tag=tag, plan_id=plan["id"])
            return _finish(
                state,
                host,
                config,
                state_directory,
                result="pre_apply_hook_unsafe",
                stage="pre_apply_hook",
                tag=tag,
                release_url=release_url,
                plan_id=plan["id"],
                deployer_state="planned",
                approval_sha256=approval_sha,
                bundle_digest=configured_manifest["control_bundle"]["sha256"],
                release_record=release_audit,
            )
        try:
            for hook in config["pre_apply_hooks"]:
                _run_hook(
                    hook,
                    host_path,
                    config_path,
                    state_directory,
                    plan_path,
                    config["poll_timeout_seconds"],
                )
                _log("pre_apply_hook", "passed", tag=tag, plan_id=plan["id"])
            if not config["pre_apply_hooks"]:
                _log("pre_apply_hook", "skipped", tag=tag, plan_id=plan["id"])
        except (AgentError, OSError):
            _log("pre_apply_hook", "pre_apply_hook_failed", tag=tag, plan_id=plan["id"])
            return _finish(
                state,
                host,
                config,
                state_directory,
                result="pre_apply_hook_failed",
                stage="pre_apply_hook",
                tag=tag,
                release_url=release_url,
                plan_id=plan["id"],
                deployer_state="planned",
                approval_sha256=approval_sha,
                bundle_digest=configured_manifest["control_bundle"]["sha256"],
                release_record=release_audit,
            )
        try:
            _invoke_deployer(
                installed_deployer,
                host_path,
                config,
                state_directory,
                "apply",
                [plan["id"], "--approval", str(approval_path)],
                config["deploy_timeout_seconds"],
            )
        except CommandTimeoutError:
            _log("apply", "deploy_timeout", tag=tag, plan_id=plan["id"])
            return _finish(
                state,
                host,
                config,
                state_directory,
                result="deploy_timeout",
                stage="apply",
                tag=tag,
                release_url=release_url,
                plan_id=plan["id"],
                deployer_state="unknown",
                approval_sha256=approval_sha,
                bundle_digest=configured_manifest["control_bundle"]["sha256"],
                release_record=release_audit,
            )
        except AgentError:
            _log("apply", "failed", tag=tag, plan_id=plan["id"])
        try:
            status_raw = _invoke_deployer(
                installed_deployer,
                host_path,
                config,
                state_directory,
                "status",
                [plan["id"]],
                config["poll_timeout_seconds"],
            )
            deployer_state = _parse_status(status_raw)
        except AgentError:
            deployer_state = "unknown"
        result = deployer_state
        _log("status", result, tag=tag, plan_id=plan["id"], deployer_state=deployer_state)
        verified = deployer_state == "verified"
        return _finish(
            state,
            host,
            config,
            state_directory,
            result=result,
            stage="status",
            tag=tag,
            release_url=release_url,
            plan_id=plan["id"],
            deployer_state=deployer_state,
            approval_sha256=approval_sha,
            bundle_digest=configured_manifest["control_bundle"]["sha256"],
            release_record=release_audit,
            deployed_tag=tag if verified else None,
            verified_digests=_expected_digests(configured_manifest) if verified else None,
        )


@contextlib.contextmanager
def _run_lock(state_directory: Path):
    path = state_directory / ".pull-agent.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise AgentError("lock_permissions")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AgentError("concurrent_run_refused") from None
        yield
    finally:
        if "fd" in locals():
            os.close(fd)


def run_once(host_path: Path, config_path: Path, state_directory: Path) -> int:
    """执行一轮；配置错误在锁和状态目录创建前拒绝。"""
    host_path, config_path, state_directory = (
        _check_cli_path(host_path),
        _check_cli_path(config_path),
        _check_cli_path(state_directory),
    )
    host = validate_host(_read_json(host_path, private=True))
    config = validate_config(_read_json(config_path, private=False))
    _private_directory(state_directory, create=True)
    try:
        with _run_lock(state_directory):
            _log("lock", "acquired", host=host["host"])
            try:
                return _run_locked(host_path, config_path, state_directory, host, config)
            except AgentError as error:
                state = _load_state(state_directory / "pull-agent.json", host)
                result = error.code if error.code in FAILURE_CODES else "unknown"
                _log("agent", result, host=host["host"])
                return _finish(
                    state,
                    host,
                    config,
                    state_directory,
                    result=result,
                    stage="agent",
                    deployer_state="unknown" if result == "unknown" else None,
                )
            except Exception:
                state = _load_state(state_directory / "pull-agent.json", host)
                _log("agent", "unknown", host=host["host"])
                return _finish(
                    state,
                    host,
                    config,
                    state_directory,
                    result="unknown",
                    stage="agent",
                    deployer_state="unknown",
                )
    except AgentError as error:
        if error.code == "concurrent_run_refused":
            _log("lock", "concurrent_run_refused", host=host["host"])
            return 1
        _log("agent", error.code, host=host["host"])
        return 1


def status_once(host_path: Path, config_path: Path, state_directory: Path) -> int:
    """只读状态账，不创建目录、不获取锁、不访问 Release 或 Docker。"""
    host_path, config_path, state_directory = (
        _check_cli_path(host_path),
        _check_cli_path(config_path),
        _check_cli_path(state_directory),
    )
    host = validate_host(_read_json(host_path, private=True))
    validate_config(_read_json(config_path, private=False))
    if not state_directory.exists():
        print("{}")
        return 0
    _private_directory(state_directory)
    path = state_directory / "pull-agent.json"
    if not path.exists():
        print(canonical(_base_state(host)).decode(), end="")
        return 0
    print(canonical(_load_state(path, host)).decode(), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    """解析单轮运行或只读状态命令。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-contract", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--state-directory", required=True, type=Path)
    sub = parser.add_subparsers(dest="operation", required=True)
    sub.add_parser("run", help="执行一轮发布拉取与部署接续")
    sub.add_parser("status", help="只读打印代理状态账")
    args = parser.parse_args(argv)
    if args.operation == "run":
        return run_once(args.host_contract, args.config, args.state_directory)
    return status_once(args.host_contract, args.config, args.state_directory)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AgentError as error:
        print(f"拉取代理拒绝：{error.code}", file=sys.stderr)
        raise SystemExit(1) from None
