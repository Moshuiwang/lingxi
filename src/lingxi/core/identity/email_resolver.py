"""邮箱只定位候选；实时在职事实决定唯一身份，不代替认证或迁移已有绑定。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any

from lingxi.core.identity.first_contact import EmploymentStatus
from lingxi.core.permission.account_match import normalize_email


class EmailIdentityState(str, Enum):
    """未决与明确非在职分开，调用方不得据未决结果撤权。"""

    UNIQUE_ACTIVE = "unique_active"
    INACTIVE = "inactive"
    ACTIVE_CONFLICT = "active_conflict"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class EmailIdentitySnapshot:
    """同一份完整花名册的行与来源；入口负责其既有新鲜度要求。"""

    rows: Sequence[Mapping[str, Any]] | None
    version: str | None
    captured_at: datetime | None
    available: bool = True


@dataclass(frozen=True)
class EmailIdentityResolution:
    """只返回判定事实，不产生账号、令牌、会话或权限变更。"""

    state: EmailIdentityState
    reason: str
    snapshot_version: str | None
    snapshot_captured_at: datetime | None
    candidate_count: int | None
    active_candidate_count: int | None
    selected: Mapping[str, Any] | None = None

    def check_binding(self, *, personnel_id=None, employee_no=None):
        """有可信旧主键时只能核对，不允许解析结果自动替换它。"""
        if self.selected is not None and not identity_binding_matches(
            self.selected, personnel_id, employee_no
        ):
            return replace(
                self, state=EmailIdentityState.UNAVAILABLE, reason="binding_mismatch", selected=None
            )
        return self

    def audit_facts(self) -> dict[str, Any]:
        """审计只带来源、计数和所选主键，不复制姓名或邮箱。"""
        return {
            "identity_state": self.state.value,
            "identity_reason": self.reason,
            "snapshot_version": self.snapshot_version,
            "snapshot_captured_at": (
                self.snapshot_captured_at.isoformat() if self.snapshot_captured_at else None
            ),
            "candidate_count": self.candidate_count,
            "active_candidate_count": self.active_candidate_count,
            "selected_personnel_id": self.selected.get("personnel_id") if self.selected else None,
        }


def email_candidates(
    email: str, snapshot: EmailIdentitySnapshot
) -> tuple[Mapping[str, Any], ...] | None:
    """保留原始候选行；来源不可用时不返回会被误当查无的空集合。"""
    if (
        not snapshot.available
        or snapshot.rows is None
        or not snapshot.version
        or snapshot.captured_at is None
        or snapshot.captured_at.utcoffset() is None
    ):
        return None
    needle = normalize_email(email)
    return tuple(
        row for row in snapshot.rows if needle and normalize_email(row.get("email")) == needle
    )


def identity_binding_matches(row, personnel_id, employee_no):
    """共同的可信主键核对；缺省绑定不新增匹配条件。"""
    return (personnel_id is None or row.get("personnel_id") == personnel_id) and (
        employee_no is None or row.get("employee_no") == employee_no
    )


@dataclass(frozen=True)
class EmailBindingCheck:
    """既有绑定的只读核对结果；不声称实时在职，也不选择新的身份。"""

    reason: str
    snapshot: EmailIdentitySnapshot
    candidate_count: int | None
    row: Mapping[str, Any] | None = None

    @property
    def matched(self):
        """只能使用原绑定，不能据此创建或转移身份。"""
        return self.row is not None

    def audit_facts(self):
        """在职数保持未知，避免把绑定核对伪装成实时在职解析。"""
        facts = EmailIdentityResolution(
            EmailIdentityState.UNAVAILABLE,
            self.reason,
            self.snapshot.version,
            self.snapshot.captured_at,
            self.candidate_count,
            None,
        ).audit_facts()
        facts["identity_state"] = "existing_binding_verified" if self.matched else "unavailable"
        facts["bound_personnel_id"] = self.row.get("personnel_id") if self.row is not None else None
        return facts


class EmailIdentityUnresolvedError(ValueError):
    """管理员入口明确拒绝身份未决；异常正文不含任何人员资料。"""

    def __init__(self, check):
        """保留最小审计事实供入口回执，不携带查询正文。"""
        self.check = check
        super().__init__(check.reason)


def check_email_binding(email, *, snapshot, personnel_id, employee_no=None):
    """无实时状态的入口只接受一个原始候选且与既有可信绑定一致。"""
    candidates = email_candidates(email, snapshot)
    if candidates is None:
        return EmailBindingCheck("snapshot_unavailable", snapshot, None)
    if len(candidates) != 1:
        return EmailBindingCheck(
            "email_not_found" if not candidates else "multiple_candidates",
            snapshot,
            len(candidates),
        )
    row = candidates[0]
    if not personnel_id or not row.get("personnel_id"):
        return EmailBindingCheck("binding_unknown", snapshot, 1)
    if not identity_binding_matches(row, personnel_id, employee_no):
        return EmailBindingCheck("binding_mismatch", snapshot, 1)
    return EmailBindingCheck("existing_binding_verified", snapshot, 1, row)


def resolve_email_identity(
    email: str,
    *,
    snapshot: EmailIdentitySnapshot,
    employment: Mapping[str, EmploymentStatus | None],
    bound_personnel_id: str | None = None,
    bound_employee_no: str | None = None,
) -> EmailIdentityResolution:
    """仅唯一明确在职的原始行可被选择；任何未知状态都不能折成离职。

    employment 由调用方实时回读，以人员 ID 为键。本函数不相信邮箱自报身份；
    已有可信绑定传入时必须一致，不能借一次邮箱解析转移历史凭据或授权。
    """
    candidates = email_candidates(email, snapshot)
    state, reason, active, selected = _select(candidates, employment)
    return EmailIdentityResolution(
        state,
        reason,
        snapshot.version,
        snapshot.captured_at,
        None if candidates is None else len(candidates),
        active,
        selected,
    ).check_binding(personnel_id=bound_personnel_id, employee_no=bound_employee_no)


def _select(candidates, employment):
    """完整性先于计数；一条未知候选足以使唯一在职结论不成立。"""
    if candidates is None:
        return EmailIdentityState.UNAVAILABLE, "snapshot_unavailable", None, None
    if not candidates:
        return EmailIdentityState.NOT_FOUND, "email_not_found", 0, None
    states = [employment.get(str(row.get("personnel_id") or "").strip()) for row in candidates]
    if any(not row.get("personnel_id") for row in candidates):
        return EmailIdentityState.UNAVAILABLE, "identity_field_unknown", None, None
    if any(not isinstance(status, EmploymentStatus) for status in states):
        return EmailIdentityState.UNAVAILABLE, "employment_unknown", None, None
    active = [row for row, status in zip(candidates, states) if status.employed]
    if not active:
        return EmailIdentityState.INACTIVE, "all_candidates_inactive", 0, None
    if len(active) > 1:
        return EmailIdentityState.ACTIVE_CONFLICT, "multiple_active_candidates", len(active), None
    return EmailIdentityState.UNIQUE_ACTIVE, "unique_active_candidate", 1, active[0]
