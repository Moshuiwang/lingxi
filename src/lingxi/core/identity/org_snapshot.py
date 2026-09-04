"""关联组织成员快照的批次完整性校验与过期规则。

本模块只有规则，没有 I/O：读飞书目录接口在
:mod:`lingxi.adapters.feishu_directory`，落库在
:mod:`lingxi.adapters.postgres_identity`。

**硬规则：完整性校验不通过就不提交半轮快照。** 半轮快照比没有快照更危险——
它会让一部分在职员工"定位不到"，而失败原因（可见范围配置被改回、某个租户
返回 0 条成员）在下游完全看不出来；实测确实出现过关联租户返回 0 条成员的情形。

校验项直接对应已经复验过的上游事实：两条身份路径看到同一批成员、三类标识
100% 可得、``open_user_id`` 跨租户唯一（710 人去重后仍 710，重复 0）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

# 与产品的九十天可识别内容上限一致，用固定小时数避免时区与夏令时造成的漂移。
SNAPSHOT_RETENTION_DAYS = 90
SNAPSHOT_RETENTION_HOURS = SNAPSHOT_RETENTION_DAYS * 24


@dataclass(frozen=True)
class SnapshotMember:
    """一名关联组织成员在快照中的标准化投影。

    刻意**不含在职状态**：可见范围不做状态过滤，能被定位到不等于在职，而把
    ``status`` 存进快照会立刻产生"库里在职、飞书已冻结"的陈旧窗口。在职状态
    在开通判定时实时读取并作为入参传入（见
    :func:`lingxi.core.identity.first_contact.decide_first_contact`）。
    """

    tenant_key: str
    member_key: str
    open_id: str
    user_id: str
    union_id: str
    display_name: str
    display_name_locale: str | None = None
    department_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class SnapshotDepartment:
    """一个部门在快照中的标准化投影。"""

    tenant_key: str
    department_key: str
    name: str | None = None


@dataclass(frozen=True)
class TenantScope:
    """一个关联租户在本轮同步中被两条身份路径分别看到的成员与部门集合。

    ``app_department_keys`` / ``user_department_keys`` 默认空集合，兼容"这个租户
    确实没有下级部门"的合法形态（两侧都为空时不判问题）——只有两侧**不相等**才是
    完整性问题：用户路径漏掉一个空部门时成员集合仍能相等，必须靠部门集合本身的
    交叉校验才挡得住。
    """

    tenant_key: str
    visible_to_user_identity: bool
    app_member_keys: frozenset[str]
    user_member_keys: frozenset[str]
    app_department_keys: frozenset[str] = frozenset()
    user_department_keys: frozenset[str] = frozenset()


@dataclass(frozen=True)
class SnapshotBatch:
    """一轮同步产出的完整快照：全部租户范围、部门与成员行。"""

    tenants: tuple[TenantScope, ...]
    departments: tuple[SnapshotDepartment, ...]
    members: tuple[SnapshotMember, ...]


class IntegrityProblem(str, Enum):
    """一轮快照可能出现的完整性问题分类。"""

    NO_TENANT = "no_tenant"
    EMPTY_TENANT = "empty_tenant"
    TENANT_NOT_USER_VISIBLE = "tenant_not_user_visible"
    PATH_SETS_DIFFER = "path_sets_differ"
    DEPARTMENT_SETS_DIFFER = "department_sets_differ"
    MEMBER_TENANT_UNKNOWN = "member_tenant_unknown"
    MEMBER_ROW_MISSING = "member_row_missing"
    MEMBER_ROW_EXTRA = "member_row_extra"
    IDENTITY_FIELD_MISSING = "identity_field_missing"
    DUPLICATE_OPEN_ID = "duplicate_open_id"
    DUPLICATE_MEMBER_KEY = "duplicate_member_key"


@dataclass(frozen=True)
class IntegrityReport:
    """一轮完整性校验的结果：问题列表 + 三项计数。"""

    problems: tuple[IntegrityProblem, ...]
    tenant_count: int
    department_count: int
    member_count: int

    @property
    def complete(self) -> bool:
        """本轮快照是否可以整体提交（没有任何问题）。"""
        return not self.problems


class SnapshotIntegrityError(RuntimeError):
    """校验不通过。适配器据此中止事务，一行都不提交。"""

    def __init__(self, report: IntegrityReport) -> None:
        """记录导致校验失败的完整报告。"""
        codes = ", ".join(problem.value for problem in report.problems)
        super().__init__(f"组织快照完整性校验未通过：{codes}")
        self.report = report


class DirectoryAvailability(str, Enum):
    """组织资料对开通判定而言是否可用。"""

    AVAILABLE = "available"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


def _blank(value: object) -> bool:
    return not isinstance(value, str) or not value.strip()


def _verify_tenant_scopes(
    tenants: Sequence[TenantScope], members_by_tenant: dict[str, set[str]]
) -> list[IntegrityProblem]:
    """逐租户核对两条身份路径看到的成员/部门集合是否一致。

    部门集合同样必须两条路径相等：成员集合能对上，不代表部门集合也对上——一个
    没有成员的空部门只会出现在部门集合里，不会在成员集合的差异中露面。反方向
    也要查：两条可见路径都没见过的成员行混进批次，提交后会为不该被看到的人建档。
    """
    problems: list[IntegrityProblem] = []
    for scope in tenants:
        if not scope.app_member_keys and not scope.user_member_keys:
            problems.append(IntegrityProblem.EMPTY_TENANT)
            continue
        if not scope.visible_to_user_identity:
            problems.append(IntegrityProblem.TENANT_NOT_USER_VISIBLE)
        if scope.app_member_keys != scope.user_member_keys:
            problems.append(IntegrityProblem.PATH_SETS_DIFFER)
        if scope.app_department_keys != scope.user_department_keys:
            problems.append(IntegrityProblem.DEPARTMENT_SETS_DIFFER)
        if scope.user_member_keys - members_by_tenant.get(scope.tenant_key, set()):
            problems.append(IntegrityProblem.MEMBER_ROW_MISSING)
        if members_by_tenant.get(scope.tenant_key, set()) - scope.user_member_keys:
            problems.append(IntegrityProblem.MEMBER_ROW_EXTRA)
    return problems


def _verify_member_rows(
    members: Sequence[SnapshotMember], declared_tenants: set[str]
) -> list[IntegrityProblem]:
    """逐成员行核对租户归属、必填字段与去重（open_id / (租户, 成员键)）。"""
    problems: list[IntegrityProblem] = []
    seen_open_ids: set[str] = set()
    seen_member_keys: set[tuple[str, str]] = set()
    for item in members:
        if item.tenant_key not in declared_tenants:
            problems.append(IntegrityProblem.MEMBER_TENANT_UNKNOWN)
        if any(
            _blank(value)
            for value in (
                item.open_id,
                item.user_id,
                item.union_id,
                item.display_name,
                item.tenant_key,
                item.member_key,
            )
        ):
            problems.append(IntegrityProblem.IDENTITY_FIELD_MISSING)
            continue
        if item.open_id in seen_open_ids:
            problems.append(IntegrityProblem.DUPLICATE_OPEN_ID)
        seen_open_ids.add(item.open_id)
        key = (item.tenant_key, item.member_key)
        if key in seen_member_keys:
            problems.append(IntegrityProblem.DUPLICATE_MEMBER_KEY)
        seen_member_keys.add(key)
    return problems


def verify_batch(batch: SnapshotBatch) -> IntegrityReport:
    """校验一轮快照是否可以整体提交。返回全部问题，不在第一个问题就短路。"""
    problems: list[IntegrityProblem] = []
    if not batch.tenants:
        problems.append(IntegrityProblem.NO_TENANT)

    declared_tenants = {scope.tenant_key for scope in batch.tenants}
    members_by_tenant: dict[str, set[str]] = {}
    for item in batch.members:
        members_by_tenant.setdefault(item.tenant_key, set()).add(item.member_key)

    problems.extend(_verify_tenant_scopes(batch.tenants, members_by_tenant))
    problems.extend(_verify_member_rows(batch.members, declared_tenants))

    # 去重但保持出现顺序，便于日志与审计逐条对照。
    unique: list[IntegrityProblem] = []
    for problem in problems:
        if problem not in unique:
            unique.append(problem)
    return IntegrityReport(
        problems=tuple(unique),
        tenant_count=len(batch.tenants),
        department_count=len(batch.departments),
        member_count=len(batch.members),
    )


def require_complete_batch(batch: SnapshotBatch) -> IntegrityReport:
    """校验并在不通过时抛错，供写库路径直接调用。"""
    report = verify_batch(batch)
    if not report.complete:
        raise SnapshotIntegrityError(report)
    return report


def snapshot_expires_at(started_at: datetime) -> datetime:
    """一轮快照的到期时刻：来源时间 + 2160 小时，调用方不得自定义或后移。"""
    if (
        not isinstance(started_at, datetime)
        or started_at.tzinfo is None
        or started_at.utcoffset() is None
    ):
        raise ValueError("started_at 必须是带时区的 UTC 时间")
    return started_at + timedelta(hours=SNAPSHOT_RETENTION_HOURS)


def snapshot_is_expired(expires_at: datetime, now: datetime) -> bool:
    """当前时刻是否已经过了快照到期时刻。"""
    return now >= expires_at


def directory_availability(expires_at: datetime | None, now: datetime) -> DirectoryAvailability:
    """当前有没有一份可用于开通判定的组织资料。

    没有任何完成批次（例如专用授权已失效、同步从未成功）→ ``UNAVAILABLE``；
    有但已过九十天上限 → ``STALE``。两者都不得用于建档。
    """
    if expires_at is None:
        return DirectoryAvailability.UNAVAILABLE
    return (
        DirectoryAvailability.STALE
        if snapshot_is_expired(expires_at, now)
        else DirectoryAvailability.AVAILABLE
    )


def index_members_by_open_id(
    members: Sequence[SnapshotMember],
) -> dict[str, tuple[SnapshotMember, ...]]:
    """按完整 ``open_id`` 建索引。**不做前缀、不做大小写归一、不做姓名回退。**"""
    index: dict[str, list[SnapshotMember]] = {}
    for item in members:
        if _blank(item.open_id):
            continue
        index.setdefault(item.open_id.strip(), []).append(item)
    return {key: tuple(value) for key, value in index.items()}
