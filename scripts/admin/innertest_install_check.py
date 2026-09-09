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
        "authorized_keys",
    }
    if set(manifest) != required or manifest.get("schema_revision") != 1:
        return {"ok": False, "errors": ["schema_invalid"]}
    for key in (
        "python",
        "relay",
        "relay_config",
        "binding",
        "socket_directory",
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
