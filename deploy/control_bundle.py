#!/usr/bin/env python3
"""以精确文件集合制作并验证非秘密控制包，安装不覆盖既有版本。"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

FILES = (
    "deploy/lingxi_deploy.py",
    "deploy/deploy_state.py",
    "deploy/deploy_runtime.py",
    "deploy/control_bundle.py",
    "scripts/ci/release_manifest.py",
    "deploy/compose.yaml",
    "deploy/compose.stage.yaml",
    "deploy/compose.prod.yaml",
    "scripts/admin/innertest_relay.py",
    "scripts/admin/innertest_install_check.py",
    "deploy/control/contract.json",
    "deploy/control/sshd_config.example",
    "deploy/control/authorized_keys.example",
    "deploy/control/README.md",
)
INDEX = "control-index.json"
ASSET = "lingxi-control.tar"
RUNTIME = {
    "python_minimum": "3.11",
    "platform": "linux",
    "tools": ["docker"],
    "compose_minimum": "2.24.0",
    "stdlib_only": True,
    "deployment_euid": 0,
}


class BundleError(RuntimeError):
    """控制包校验失败，调用方不得继续安装。"""

    pass


def canonical(value):
    """稳定编码让批准和回读使用相同摘要。"""
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode()


def digest(data):
    """仅对非秘密制品计算内容摘要。"""
    return hashlib.sha256(data).hexdigest()


def safe_source(root, name):
    """拒绝链接和可由其他主体改写的源文件。"""
    path = root / name
    for item in (path, *path.parents):
        if item == root.parent:
            break
        if item.is_symlink():
            raise BundleError("控制包源禁止链接")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o022:
        raise BundleError("控制包源类型或权限不安全")
    return path.read_bytes(), 0o555 if info.st_mode & 0o111 else 0o444


def build(root: Path, output: Path, commit: str):
    """包只能绑定已提交且没有漂移的源码。"""
    if not re.fullmatch("[0-9a-f]{40}", commit):
        raise BundleError("缺少固定来源提交")
    actual = subprocess.check_output(
        ["git", "-c", "core.fsmonitor=false", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    changed = subprocess.check_output(
        ["git", "-c", "core.fsmonitor=false", "status", "--porcelain", "--", *FILES], cwd=root
    )
    if actual != commit or changed:
        raise BundleError("控制包必须来自无修改的固定候选")
    entries, contents = [], {}
    for name in FILES:
        data, mode = safe_source(root, name)
        tracked = subprocess.check_output(["git", "show", f"{commit}:{name}"], cwd=root)
        if tracked != data:
            raise BundleError("文件与来源提交不符")
        contents[name] = data
        entries.append(
            {
                "path": name,
                "sha256": digest(data),
                "mode": mode,
                "size": len(data),
                "source_commit": commit,
            }
        )
    index = {"schema_revision": 1, "source_commit": commit, "runtime": RUNTIME, "files": entries}
    contents[INDEX] = canonical(index)
    with tarfile.open(output, "w", format=tarfile.USTAR_FORMAT) as archive:
        for name, data in contents.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = (
                0o444 if name == INDEX else next(e["mode"] for e in entries if e["path"] == name)
            )
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))
    return {
        "asset": ASSET,
        "sha256": digest(output.read_bytes()),
        "index_sha256": digest(contents[INDEX]),
        "schema_revision": 1,
        "source_commit": commit,
        "runtime": RUNTIME,
    }


def verify(package: Path, expected: dict):
    """完整验证通过前不向目标目录写文件。"""
    raw = package.read_bytes()
    if len(raw) > 10 * 1024 * 1024 or digest(raw) != expected["sha256"]:
        raise BundleError("控制包摘要或体量不符")
    contents = {}
    modes = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if (
                member.name not in (*FILES, INDEX)
                or path.is_absolute()
                or ".." in path.parts
                or member.name in contents
                or not member.isfile()
                or member.linkname
                or member.mode not in (0o444, 0o555)
                or member.uid
                or member.gid
                or member.size > 2 * 1024 * 1024
            ):
                raise BundleError("控制包路径、类型、权限或重复项不安全")
            contents[member.name] = archive.extractfile(member).read()
            modes[member.name] = member.mode
    if set(contents) != set((*FILES, INDEX)):
        raise BundleError("控制包缺少固定文件")
    if digest(contents[INDEX]) != expected["index_sha256"]:
        raise BundleError("包索引摘要不符")
    index = json.loads(contents[INDEX])
    if (
        set(index) != {"schema_revision", "source_commit", "runtime", "files"}
        or index["schema_revision"] != 1
        or index["runtime"] != RUNTIME
        or index["source_commit"] != expected["source_commit"]
        or expected["schema_revision"] != 1
        or expected["runtime"] != RUNTIME
    ):
        raise BundleError("包索引来源或版本不符")
    if len(index["files"]) != len(FILES) or {x["path"] for x in index["files"]} != set(FILES):
        raise BundleError("包索引文件集合不符")
    for entry in index["files"]:
        name = entry["path"]
        if (
            set(entry) != {"path", "sha256", "mode", "size", "source_commit"}
            or entry["sha256"] != digest(contents[name])
            or entry["size"] != len(contents[name])
            or entry["mode"] != modes[name]
            or entry["source_commit"] != index["source_commit"]
        ):
            raise BundleError("包内文件与索引不符")
    return index, contents


def sync_directory(path):
    """目录同步确保引用切换不会只留在缓存。"""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def verify_install(target: Path, expected: dict):
    """既有版本也必须重新核对，不能只凭目录存在跳过。"""
    if target.is_symlink() or not target.is_dir() or stat.S_IMODE(target.stat().st_mode) != 0o555:
        raise BundleError("安装目录无效")
    index_path = target / INDEX
    if (
        index_path.is_symlink()
        or stat.S_IMODE(index_path.stat().st_mode) != 0o444
        or digest(index_path.read_bytes()) != expected["index_sha256"]
    ):
        raise BundleError("安装索引已改变")
    index = json.loads(index_path.read_bytes())
    actual = set()
    directories = {
        str(parent)
        for name in FILES
        for parent in PurePosixPath(name).parents
        if str(parent) != "."
    }
    actual_directories = set()
    for path in target.rglob("*"):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_mode & 0o222:
            raise BundleError("安装目录存在链接或可写项")
        if path.is_file():
            actual.add(path.relative_to(target).as_posix())
        elif path.is_dir():
            actual_directories.add(path.relative_to(target).as_posix())
        else:
            raise BundleError("安装目录文件类型不符")
    if actual != set((*FILES, INDEX)) or actual_directories != directories:
        raise BundleError("安装文件集合不符")
    for entry in index["files"]:
        data, mode = safe_source(target, entry["path"])
        if (
            digest(data) != entry["sha256"]
            or mode != entry["mode"]
            or stat.S_IMODE((target / entry["path"]).stat().st_mode) != entry["mode"]
        ):
            raise BundleError("安装文件已改变")
    return target


def install(package: Path, expected: dict, root: Path):
    """版本目录保留原件，失败不覆盖当前入口。"""
    index, contents = verify(package, expected)
    if root.is_symlink() or not root.is_dir() or root.stat().st_mode & 0o022:
        raise BundleError("安装根必须预先建立且不可由受限账号写入")
    target = root / expected["sha256"]
    if target.exists():
        return verify_install(target, expected)
    temporary = Path(tempfile.mkdtemp(prefix=".install-", dir=root))
    try:
        for name, data in contents.items():
            path = temporary / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            mode = (
                0o444
                if name == INDEX
                else next(x["mode"] for x in index["files"] if x["path"] == name)
            )
            path.chmod(mode)
        directories = sorted((p for p in temporary.rglob("*") if p.is_dir()), reverse=True)
        for directory in [*directories, temporary]:
            directory.chmod(0o555)
            sync_directory(directory)
        os.rename(temporary, target)
        sync_directory(root)
    finally:
        if temporary.exists():
            for path in [temporary, *temporary.rglob("*")]:
                if path.is_dir():
                    path.chmod(0o700)
            shutil.rmtree(temporary)
    return verify_install(target, expected)


def activate(root: Path, expected: dict):
    """启用引用只指向已完整验证的只读版本。"""
    target = verify_install(root / expected["sha256"], expected)
    pointer = root / "current"
    if pointer.exists() and not pointer.is_symlink():
        raise BundleError("当前引用必须是受控链接")
    temporary = root / ".current-next"
    if temporary.is_symlink():
        if os.readlink(temporary) != target.name:
            raise BundleError("待切换引用与固定目标不符")
    elif temporary.exists():
        raise BundleError("待切换引用类型不符")
    else:
        temporary.symlink_to(target.name)
    try:
        os.replace(temporary, pointer)
        sync_directory(root)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    print(canonical(build(args.root, args.output, args.commit)).decode(), end="")


def install_relay(bundle_root: Path, expected: dict, configuration: dict, root: Path):
    """环境装配与包分开；只接纳固定 relay 和唯一非秘密相邻配置。"""
    if (
        set(configuration) != {"schema_revision", "socket_path", "relay_uid", "socket_owner_uid"}
        or configuration["schema_revision"] != 1
        or not Path(configuration["socket_path"]).is_absolute()
        or configuration["relay_uid"] in (0, 65534, configuration["socket_owner_uid"])
    ):
        raise BundleError("relay 安装配置无效")
    source = verify_install(bundle_root / expected["sha256"], expected)
    data = (source / "scripts/admin/innertest_relay.py").read_bytes()
    receipt = {
        "schema_revision": 1,
        "bundle_sha256": expected["sha256"],
        "relay_sha256": digest(data),
        "config_sha256": digest(canonical(configuration)),
        "mode": 0o444,
        "directory_mode": 0o555,
    }
    identity = digest(canonical(receipt))
    target = root / identity
    if root.is_symlink() or not root.is_dir() or root.stat().st_mode & 0o022:
        raise BundleError("relay 安装根不安全")
    wanted = {
        "innertest_relay.py": data,
        "innertest-relay.json": canonical(configuration),
        "installation.json": canonical(receipt),
    }
    if not target.exists():
        temporary = Path(tempfile.mkdtemp(prefix=".relay-", dir=root))
        try:
            for name, contents in wanted.items():
                with (temporary / name).open("xb") as stream:
                    stream.write(contents)
                    stream.flush()
                    os.fsync(stream.fileno())
                (temporary / name).chmod(0o444)
            temporary.chmod(0o555)
            sync_directory(temporary)
            os.rename(temporary, target)
            sync_directory(root)
        finally:
            if temporary.exists():
                temporary.chmod(0o700)
                shutil.rmtree(temporary)
    if (
        target.is_symlink()
        or stat.S_IMODE(target.stat().st_mode) != 0o555
        or set(p.name for p in target.iterdir()) != set(wanted)
    ):
        raise BundleError("relay 安装目录不符")
    for name, contents in wanted.items():
        path = target / name
        if (
            path.is_symlink()
            or stat.S_IMODE(path.stat().st_mode) != 0o444
            or path.read_bytes() != contents
        ):
            raise BundleError("relay 安装内容不符")
    return target, receipt
