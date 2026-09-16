"""邮箱只定位候选；实时在职事实决定唯一身份，不代替认证或迁移已有绑定。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
    if selected is not None and (
        (bound_personnel_id is not None and selected.get("personnel_id") != bound_personnel_id)
        or (bound_employee_no is not None and selected.get("employee_no") != bound_employee_no)
    ):
        state, reason, selected = EmailIdentityState.UNAVAILABLE, "binding_mismatch", None
    return EmailIdentityResolution(
        state,
        reason,
        snapshot.version,
        snapshot.captured_at,
        None if candidates is None else len(candidates),
        active,
        selected,
    )


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
