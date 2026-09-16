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

# 阶段账在 preflight 通过时记下的两侧配置指纹：old 侧是上一次已验证部署给容器打的标签值，
# new 侧是本计划的当前 public-config 指纹；接续时 old 侧只从这条记录取，不重新推算。
INVENTORY_KEYS = frozenset({"old_verified_sha256", "old_source", "new_sha256"})


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


def is_sha256(value):
    """摘要字段只认小写十六进制 64 位；别的形状一律不当作可信值。"""
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) is not None


def check_id(value):
    """标识不能改变文件和作业的目标路径。"""
    if not isinstance(value, str) or not re.fullmatch("[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", value):
        raise DeployError("invalid_identifier")
    return value


def host_marker(lock_path, kind):
    """主机锁同目录的两份指针：``active``（在途 / 已验证的当前占用）与 ``verified``（上一次验证成功）。"""
    return Path(lock_path).with_suffix(f".{kind}.json")


def private_directory(root: Path, *, create=False):
    """部署账限制为当前部署主体可访问。"""
    if create:
        root.mkdir(mode=0o700, parents=False, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise DeployError("private_directory_required")
    info = root.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise DeployError("private_directory_permissions")


def read_raw(path: Path, *, public=False):
    """私有材料只允许本人读取；非秘密映射可读但不能由他人改写。返回文件原文字节。"""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or (info.st_mode & 0o022 if public else stat.S_IMODE(info.st_mode) != 0o600)
        ):
            raise DeployError("private_file_permissions")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise DeployError("private_file_too_large")
        return data
    finally:
        os.close(fd)


def read_json(path: Path, *, public=False):
    """按同一权限口径读取并解析 JSON。"""
    return json.loads(read_raw(path, public=public))


def atomic_write(path: Path, value):
    """同步完整内容后才替换旧账。"""
    atomic_write_raw(path, canonical(value))


def atomic_write_raw(path: Path, data: bytes):
    """原文字节按同一私有口径落盘：归档时逐字节保留旧账，不重新编码。"""
    private_directory(path.parent)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
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
        return self.load(plan)[1]

    def load(self, plan):
        """阶段账原文与解析结果一并返回：原文供指纹核对与逐字归档，没有阶段账时原文为 None。"""
        path = self.path(plan["id"], "state")
        if not path.exists():
            return None, {
                "schema": 1,
                "plan_sha256": fingerprint(plan),
                "status": "planned",
                "stages": {},
                "approval_sha256": None,
            }
        raw = read_raw(path)
        result = json.loads(raw)
        if result["plan_sha256"] != fingerprint(plan):
            raise DeployError("state_plan_mismatch")
        return raw, result

    def digest(self, plan):
        """阶段账文件原文的 sha256，供人工操作前后核对是否仍是同一份账；没有阶段账时为 None。"""
        raw, _ = self.load(plan)
        return None if raw is None else hashlib.sha256(raw).hexdigest()

    def verified_configuration(self, pointer):
        """按指针读回此前固定的计划，指纹逐字相符才信其 ``config_sha256``。

        指针是部署器自己写下的 ``{"id", "plan_sha256"}``（主机在途标记或 ``recovery_of``）。
        计划读不到、指纹不符、``config_sha256`` 形状异常都视为可信旧值不可得，失败关闭。
        """
        try:
            previous = self.plan(pointer["id"])
            usable = fingerprint(previous) == pointer["plan_sha256"] and is_sha256(
                previous["config_sha256"]
            )
        except (OSError, ValueError, KeyError, TypeError, DeployError):
            usable = False
        if not usable:
            raise DeployError("previous_verified_configuration_unavailable")
        return previous["config_sha256"]

    def old_configuration(self, marker, plan, ledger):
        """取 old 侧配置指纹：只用部署器自己记下的可信旧值，不取当前配置、不读被核容器的标签。

        返回 ``(指纹或 None, 来源)``。``recover`` 取被恢复计划的 ``config_sha256``；否则看主机
        在途标记 ``marker``：不存在即首次接管、没有旧值；指向本计划即接续，取本计划阶段账在
        preflight 通过时记下的 ``inventory``；指向上一次 ``verified`` 计划则取该计划的
        ``config_sha256``（那次部署给容器打的标签值）。任何一处读不到、指纹不符或形状异常，
        一律 ``previous_verified_configuration_unavailable`` 失败关闭，不回落到当前配置。
        """
        if plan["operation"] == "recover":
            return self.verified_configuration(plan["recovery_of"]), plan["recovery_of"]["id"]
        try:
            active = read_json(marker)
        except FileNotFoundError:
            return None, "first_takeover"
        if not isinstance(active, dict):
            raise DeployError("previous_verified_configuration_unavailable")
        if active.get("id") == plan["id"]:
            record = ledger.get("inventory")
            if (
                not isinstance(record, dict)
                or set(record) != INVENTORY_KEYS
                or not isinstance(record["old_source"], str)
                or not (
                    record["old_verified_sha256"] is None
                    or is_sha256(record["old_verified_sha256"])
                )
            ):
                raise DeployError("previous_verified_configuration_unavailable")
            return record["old_verified_sha256"], record["old_source"]
        if active.get("status") != "verified":
            raise DeployError("previous_verified_configuration_unavailable")
        return self.verified_configuration(active), active["id"]

    def save(self, plan, state):
        """每个阶段执行前后都同步落盘。"""
        atomic_write(self.path(plan["id"], "state"), state)
