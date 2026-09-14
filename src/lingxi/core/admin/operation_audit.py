"""运营操作的持久审计条目：一次操作各阶段各一行，只追加，只放固定码与计数。

这张账回答「谁发起、谁确认、谁执行、目的与目标范围、结果、证据指针」，与执行层的
工具调用审计目的相反，不复用它的脱敏。表 ``operation_audit`` 没有自由文本列，所以
这里的原则是**只拒绝、不脱敏**：任何长得像凭据或业务正文的值——含空白、``=`` 赋值
形态、``bearer``、带口令的连接串、超过该列长度——在进入数据库之前就以 ``ValueError``
失败关闭；逐列正则再把取值限定在各自的固定码形状上。

本模块只有类型与纯函数，落库在 ``adapters/postgres_operation_audit.py``。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from importlib import metadata
from types import MappingProxyType
from typing import Protocol

from lingxi.core.admin.registry import AdminRole


class OperationPhase(str, Enum):
    """一次操作可以留下记录的五个阶段，与表的 CHECK 逐字一致。"""

    PREPARED = "prepared"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXECUTED = "executed"


class EntryPoint(str, Enum):
    """操作从哪个入口进来，与表的 CHECK 逐字一致。"""

    OPS_SCRIPT = "ops_script"
    RESTRICTED_CHANNEL = "restricted_channel"
    FEISHU_CARD = "feishu_card"
    SCHEDULER_FOLLOWUP = "scheduler_followup"


#: 确认卡点击者必填的阶段。
PHASES_REQUIRING_DECIDER = frozenset({OperationPhase.CONFIRMED, OperationPhase.CANCELLED})

#: 每一列的形状（正则，长度上限）。上限按该列合法取值的最长形态给，不是统一常量：
#: 摘要列固定 71 个字符，执行者标签带服务名、版本与运行号，其余码列 64 足够。
_IDENTIFIER = (re.compile(r"^[A-Za-z0-9_-]{1,64}$"), 64)
_CODE = (re.compile(r"^[a-z][a-z0-9_]*$"), 64)
_COLUMN_SHAPES: dict[str, tuple[re.Pattern[str], int]] = {
    "operation_id": _IDENTIFIER,
    "operation": (re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$"), 64),
    "initiated_by": _IDENTIFIER,
    "decided_by": _IDENTIFIER,
    "executor": (
        re.compile(
            r"^[a-z][a-z0-9_-]*(:[a-z][a-z0-9_-]*)?@[A-Za-z0-9][A-Za-z0-9._+-]*"
            r":[A-Za-z0-9][A-Za-z0-9_-]*$"
        ),
        128,
    ),
    "purpose": _CODE,
    "target_kind": _CODE,
    "target_digest": (re.compile(r"^sha256:[0-9a-f]{64}$"), 71),
    "target_user_id": _IDENTIFIER,
    "result_code": (re.compile(r"^[a-z][A-Za-z0-9_]*(:[A-Za-z0-9_.-]+)?$"), 64),
    "evidence_ref": (re.compile(r"^[a-z_]+:[A-Za-z0-9_:.-]{1,128}$"), 160),
    "pending_action_id": _IDENTIFIER,
    "trace_id": _IDENTIFIER,
}
_CREDENTIAL_URL = re.compile(r"://[^/@]*:[^/@]*@")


def _refuse_secret_shape(name: str, value: object, limit: int) -> str:
    """凭据与业务正文的五种形状一律拒绝；这里不看列的正则，只看「像不像秘密」。"""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} 必须是非空字符串")
    if any(character.isspace() for character in value):
        raise ValueError(f"{name} 不能含空白")
    if "=" in value:
        raise ValueError(f"{name} 不能是赋值形态")
    if "bearer" in value.lower():
        raise ValueError(f"{name} 不能携带令牌形态")
    if _CREDENTIAL_URL.search(value):
        raise ValueError(f"{name} 不能是带口令的连接串")
    if len(value) > limit:
        raise ValueError(f"{name} 超过 {limit} 个字符")
    return value


def _checked_column(name: str, value: object) -> str:
    pattern, limit = _COLUMN_SHAPES[name]
    _refuse_secret_shape(name, value, limit)
    if not pattern.match(value):
        raise ValueError(f"{name} 不是该列允许的固定码形状")
    return value


def _checked_count(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} 必须是非负整数")
    return value


def _checked_result_counts(value: object) -> Mapping[str, int]:
    """只收计数键：键是固定码，值是非负整数——字符串值可能藏任何东西，一律不收。"""
    if not isinstance(value, Mapping):
        raise ValueError("result_counts 必须是映射")
    counts: dict[str, int] = {}
    for key, count in value.items():
        _refuse_secret_shape("result_counts 的键", key, _CODE[1])
        if not _CODE[0].match(key):
            raise ValueError("result_counts 的键必须是固定码")
        counts[key] = _checked_count(f"result_counts[{key}]", count)
    return MappingProxyType(counts)


def _checked_roles(value: object) -> frozenset[AdminRole]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise ValueError("actor_roles 必须是角色集合")
    roles = frozenset(value)
    if any(not isinstance(role, AdminRole) for role in roles):
        raise ValueError("actor_roles 只能包含登记表定义的角色")
    return roles


def format_actor_roles(roles: Iterable[AdminRole]) -> str:
    """角色快照的落库形态：按登记表的固定顺序逗号连接，与集合的迭代顺序无关。"""
    chosen = _checked_roles(roles)
    return ",".join(role.value for role in AdminRole if role in chosen)


def parse_actor_roles(text: str) -> frozenset[AdminRole]:
    """把落库的逗号列读回角色集合；空串表示判定时刻没有任何角色。"""
    if not text:
        return frozenset()
    return frozenset(AdminRole(item) for item in text.split(","))


def _installed_version() -> str:
    try:
        return metadata.version("lingxi")
    except metadata.PackageNotFoundError:
        return "unknown"


def executor_label(service: str, *, run_id: str | None = None) -> str:
    """执行者标签 ``<服务>@<版本>[:<运行号>]``；包未安装时版本记 ``unknown``。"""
    label = f"{service}@{_installed_version()}"
    return label if run_id is None else f"{label}:{run_id}"


@dataclass(frozen=True, kw_only=True)
class OperationAuditEntry:
    """一行待落库的审计记录。构造即校验，不合法的值到不了数据库。"""

    operation_id: str
    operation: str
    phase: OperationPhase
    initiated_by: str
    actor_roles: frozenset[AdminRole]
    entry_point: EntryPoint
    decided_by: str | None = None
    executor: str | None = None
    purpose: str | None = None
    target_kind: str | None = None
    target_count: int | None = None
    target_digest: str | None = None
    target_user_id: str | None = None
    result_code: str | None = None
    result_counts: Mapping[str, int] = field(default_factory=dict)
    evidence_ref: str | None = None
    pending_action_id: str | None = None
    trace_id: str | None = None

    def __post_init__(self) -> None:
        """逐列校验；阶段与必填列的关系与表的 CHECK 相同，先在这里挡住。"""
        if not isinstance(self.phase, OperationPhase):
            raise ValueError("phase 必须是 OperationPhase")
        if not isinstance(self.entry_point, EntryPoint):
            raise ValueError("entry_point 必须是 EntryPoint")
        for name in ("operation_id", "operation", "initiated_by"):
            _checked_column(name, getattr(self, name))
        for name in _COLUMN_SHAPES.keys() - {"operation_id", "operation", "initiated_by"}:
            if getattr(self, name) is not None:
                _checked_column(name, getattr(self, name))
        if self.target_count is not None:
            _checked_count("target_count", self.target_count)
        object.__setattr__(self, "actor_roles", _checked_roles(self.actor_roles))
        object.__setattr__(self, "result_counts", _checked_result_counts(self.result_counts))
        if self.phase in PHASES_REQUIRING_DECIDER and self.decided_by is None:
            raise ValueError(f"{self.phase.value} 阶段必须记录确认者")
        if self.phase is OperationPhase.EXECUTED and (
            self.executor is None or self.result_code is None
        ):
            raise ValueError("executed 阶段必须记录执行者与结果码")


@dataclass(frozen=True, kw_only=True)
class OperationAuditRecord:
    """从库里读回的一行：条目本身加上数据库赋予的标识与时间。"""

    id: str
    entry: OperationAuditEntry
    created_at: datetime
    expires_at: datetime


class OperationAuditLedger(Protocol):
    """写口：追加一行并返回其标识；写不进去就抛异常，由调用方决定是否继续。"""

    def record(self, entry: OperationAuditEntry) -> str:
        """追加一行，返回数据库里的标识。"""
