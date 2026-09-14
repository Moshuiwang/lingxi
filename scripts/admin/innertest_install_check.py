#!/usr/bin/env python3
"""只读安装自检；由部署工具传冻结清单，输出机读结果。"""

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path


def check(manifest):
    """校验安装清单与宿主权限。

    socket 目录不能由 root 持有：scheduler 以容器 UID 运行，需要在目录内创建
    socket 和锁文件；目录的父级仍必须由 root 保护。
    """
    errors = []
    minimum_python = (3, 11)
    if sys.version_info < minimum_python:
        errors.append("python_too_old")
    required = {
        "schema_revision",
        "python",
        "relay",
        "relay_sha256",
        "relay_config",
        "binding",
        "socket_directory",
        "relay_uid",
        "relay_gid",
        "socket_owner_uid",
        "socket_gid",
        "authorized_keys",
    }
    if set(manifest) != required or manifest.get("schema_revision") != 1:
        return {"ok": False, "errors": ["schema_invalid"]}
    if any(type(manifest[key]) is not int for key in ("socket_owner_uid", "socket_gid")):
        return {"ok": False, "errors": ["schema_invalid"]}
    for key in (
        "python",
        "relay",
        "relay_config",
        "binding",
        "authorized_keys",
    ):
        path = Path(manifest[key])
        try:
            for item in (path, *path.parents):
                info = item.lstat()
                if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
                    errors.append(key + "_not_protected")
                    break
        except OSError:
            errors.append(key + "_missing")
    socket_directory = Path(manifest["socket_directory"])
    try:
        info = socket_directory.lstat()
        if stat.S_ISLNK(info.st_mode):
            errors.append("socket_directory_symlink")
        if (
            info.st_uid != manifest["socket_owner_uid"]
            or manifest["socket_owner_uid"] in (0, 65534)
            or manifest["socket_owner_uid"] == manifest["relay_uid"]
        ):
            errors.append("socket_directory_owner_invalid")
        if info.st_gid != manifest["socket_gid"]:
            errors.append("socket_directory_group_invalid")
        if stat.S_IMODE(info.st_mode) != 0o750:
            errors.append("socket_directory_mode_invalid")
    except OSError:
        errors.append("socket_directory_missing")
    try:
        for item in (socket_directory.parent, *socket_directory.parent.parents):
            info = item.lstat()
            if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
                errors.append("socket_directory_parent_not_protected")
                break
    except OSError:
        errors.append("socket_directory_parent_not_protected")
    try:
        if (
            hashlib.sha256(Path(manifest["relay"]).read_bytes()).hexdigest()
            != manifest["relay_sha256"]
        ):
            errors.append("relay_digest_mismatch")
        keys = Path(manifest["authorized_keys"]).read_text().splitlines()
        command = f'command="{manifest["python"]} -I -S {manifest["relay"]}"'
        if len(keys) != 1 or any(
            v not in keys[0] for v in ("restrict,", command, "no-user-rc", "ssh-ed25519 ")
        ):
            errors.append("forced_command_invalid")
    except OSError:
        errors.append("files_unreadable")
    import grp
    import pwd

    try:
        user = pwd.getpwuid(manifest["relay_uid"])
        groups = os.getgrouplist(user.pw_name, user.pw_gid)
        if user.pw_uid == 0 or user.pw_gid != manifest["relay_gid"]:
            errors.append("relay_identity_invalid")
        for group in ("docker", "sudo", "wheel"):
            try:
                if grp.getgrnam(group).gr_gid in groups:
                    errors.append("privileged_group")
            except KeyError:
                pass
    except KeyError:
        errors.append("relay_user_missing")
    return {
        "ok": not errors,
        "errors": sorted(set(errors)),
        "schema_revision": 1,
        "manual_required": [
            "sudo_effective_policy",
            "sshd_effective_policy",
            "credential_owner",
            "three_peer_connection_probes",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest")
    args = parser.parse_args()
    result = check(json.loads(Path(args.manifest).read_text()))
    print(json.dumps(result))
    raise SystemExit(0 if result["ok"] else 1)
