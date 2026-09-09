#!/usr/bin/env python3
"""受限 SSH 标准库 relay；仅固定相邻配置，不运行客户端命令。"""

import json
import os
import selectors
import socket
import stat
import sys
from pathlib import Path

MIN_PYTHON = (3, 11)
CONFIG_NAME = "innertest-relay.json"


def load_config():
    if sys.version_info < MIN_PYTHON or len(sys.argv) != 1:
        raise ValueError("runtime_or_arguments_invalid")
    path = Path(__file__).resolve().with_name(CONFIG_NAME)
    for entry in (Path(__file__).resolve(), path, path.parent):
        info = entry.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
            raise ValueError("installation_not_protected")
    config = json.loads(path.read_text())
    if set(config) != {"schema_revision", "socket_path", "relay_uid", "socket_owner_uid"}:
        raise ValueError("configuration_invalid")
    if config["schema_revision"] != 1 or os.getuid() != config["relay_uid"]:
        raise ValueError("uid_mapping_invalid")
    path = Path(config["socket_path"])
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("socket_invalid")
    info = path.lstat()
    parent = path.parent.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != config["socket_owner_uid"]:
        raise ValueError("socket_owner_invalid")
    if parent.st_mode & 0o022 or parent.st_uid not in {0, config["socket_owner_uid"]}:
        raise ValueError("socket_directory_invalid")
    return config


def relay(config):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(config["socket_path"])
        selector = selectors.DefaultSelector()
        selector.register(sys.stdin.buffer, selectors.EVENT_READ)
        selector.register(connection, selectors.EVENT_READ)
        try:
            while True:
                for key, _ in selector.select():
                    if key.fileobj is connection:
                        data = connection.recv(65536)
                        if not data:
                            return
                        sys.stdout.buffer.write(data)
                        sys.stdout.buffer.flush()
                    else:
                        data = os.read(sys.stdin.fileno(), 65536)
                        if not data:
                            connection.shutdown(socket.SHUT_WR)
                            selector.unregister(sys.stdin.buffer)
                        else:
                            connection.sendall(data)
        finally:
            selector.close()


def main():
    try:
        relay(load_config())
    except Exception:
        print("受限管理通道不可用；请按原 request_key 查询，勿重新发起。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
