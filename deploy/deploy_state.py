"""私有部署账与主机全程锁；业务持久状态不属于本模块。"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


class DeployError(RuntimeError):
    """稳定错误码避免泄露外部命令输出。"""

    pass


class UnknownError(DeployError):
    """结果不明必须回读后决定下一步。"""

    pass


def canonical(value):
    """固定编码避免同一批准产生不同摘要。"""
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode()


def fingerprint(value):
    """稳定摘要将批准绑定到完整非秘密材料。"""
    return hashlib.sha256(canonical(value)).hexdigest()


def check_id(value):
    """标识不能改变文件和作业的目标路径。"""
    if not isinstance(value, str) or not re.fullmatch("[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", value):
        raise DeployError("invalid_identifier")
    return value


def private_directory(root: Path, *, create=False):
    """部署账限制为当前部署主体可访问。"""
    if create:
        root.mkdir(mode=0o700, parents=False, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise DeployError("private_directory_required")
    info = root.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise DeployError("private_directory_permissions")


def read_json(path: Path):
    """拒绝链接与宽权限的批准或状态材料。"""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise DeployError("private_file_permissions")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise DeployError("private_file_too_large")
        return json.loads(data)
    finally:
        os.close(fd)


def atomic_write(path: Path, value):
    """同步完整内容后才替换旧账。"""
    private_directory(path.parent)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def host_lock(path: Path):
    # 路径由受保护宿主契约固定，不能为第二计划改换私有状态目录规避互斥。
    """全部部署计划在同一主机互斥。"""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (
            info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or not stat.S_ISREG(info.st_mode)
        ):
            raise DeployError("host_lock_permissions")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeployError("host_deployment_busy") from None
        yield fd
    finally:
        # 子作业继承同一描述符，父进程断线不提前解除互斥。
        os.close(fd)


def validate_approval(plan, approval, now=None):
    """批准必须匹配完整计划、操作和时间窗口。"""
    now = time.time() if now is None else now
    keys = {"schema", "plan_id", "plan_sha256", "operation", "source", "approved_at", "expires_at"}
    if set(approval) != keys or approval["schema"] != 1:
        raise DeployError("approval_shape")
    if (
        approval["plan_id"] != plan["id"]
        or approval["plan_sha256"] != fingerprint(plan)
        or approval["operation"] != plan["operation"]
        or approval["source"] != plan["approval_source"]
    ):
        raise DeployError("approval_mismatch")
    if not (
        plan["not_before"]
        <= approval["approved_at"]
        <= now
        <= approval["expires_at"]
        <= plan["expires_at"]
    ):
        raise DeployError("approval_expired")
    return fingerprint(approval)


class StateStore:
    """已批准计划和运行状态分别保存。"""

    def __init__(self, root: Path):
        """依赖由调用方固定，不查找浮动配置。"""
        self.root = root

    def path(self, identifier, suffix):
        """标识不能越出指定私有目录。"""
        return self.root / f"{check_id(identifier)}.{suffix}.json"

    def save_plan(self, plan):
        """同一部署标识不能改绑另一份计划。"""
        private_directory(self.root, create=True)
        with host_lock(self.root / ".plan.lock"):
            path = self.path(plan["id"], "plan")
            if path.exists():
                if read_json(path) != plan:
                    raise DeployError("plan_id_already_bound")
                return
            atomic_write(path, plan)

    def plan(self, identifier):
        """接续只读取此前固定的计划。"""
        private_directory(self.root)
        return read_json(self.path(identifier, "plan"))

    def state(self, plan):
        """状态只属于同一份完整计划。"""
        path = self.path(plan["id"], "state")
        if not path.exists():
            return {
                "schema": 1,
                "plan_sha256": fingerprint(plan),
                "status": "planned",
                "stages": {},
                "approval_sha256": None,
            }
        result = read_json(path)
        if result["plan_sha256"] != fingerprint(plan):
            raise DeployError("state_plan_mismatch")
        return result

    def save(self, plan, state):
        """每个阶段执行前后都同步落盘。"""
        atomic_write(self.path(plan["id"], "state"), state)
