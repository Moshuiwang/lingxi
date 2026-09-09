"""受保护安装映射与数据库启停绑定分别校验，绑定不授予角色。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from lingxi.core.admin.innertest import InnertestError


@dataclass(frozen=True)
class PeerBinding:
    """部署冻结的唯一 UID 映射，不接受溢出 UID 或默认主体。"""

    binding_id: str
    host_uid: int
    peer_uid: int
    uid_map_sha256: str
    socket_gid: int


def load_binding(path):
    """配置与父目录必须 root 管理；userns 摘要不明即拒绝启动。"""
    for entry in (Path(path), Path(path).parent):
        info = entry.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
            raise InnertestError("binding_not_protected")
    raw = json.loads(Path(path).read_text())
    if (
        set(raw)
        != {"schema_revision", "binding_id", "host_uid", "peer_uid", "uid_map_sha256", "socket_gid"}
        or raw.pop("schema_revision") != 1
    ):
        raise InnertestError("binding_invalid")
    value = PeerBinding(**raw)
    for number in (value.host_uid, value.peer_uid, value.socket_gid):
        if type(number) is not int or number <= 0 or number == 65534:
            raise InnertestError("uid_mapping_invalid")
    mapping = Path("/proc/self/uid_map").read_bytes()
    if hashlib.sha256(mapping).hexdigest() != value.uid_map_sha256:
        raise InnertestError("uid_mapping_invalid")
    resolved = [
        inside + value.host_uid - outside
        for inside, outside, length in (
            map(int, line.split()) for line in mapping.decode().splitlines()
        )
        if outside <= value.host_uid < outside + length
    ]
    if resolved != [value.peer_uid] or value.peer_uid == os.geteuid():
        raise InnertestError("uid_mapping_invalid")
    return value
