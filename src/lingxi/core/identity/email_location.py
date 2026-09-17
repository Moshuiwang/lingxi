"""以既有组织读取口和实时状态读取口装配邮箱解析，不持有平台凭据。"""

from dataclasses import dataclass

from lingxi.core.identity.email_resolver import (
    EmailIdentityResolution,
    EmailIdentitySnapshot,
    email_candidates,
    resolve_email_identity,
)
from lingxi.core.identity.first_contact import EmploymentStatus
from lingxi.core.identity.org_snapshot import DirectoryAvailability, SnapshotMember


@dataclass(frozen=True)
class LocatedEmailIdentity:
    """身份结论与其对应的可信组织成员，失败时不提供可开通主体。"""

    resolution: EmailIdentityResolution
    member: SnapshotMember | None = None


def locate_current_email(email, *, snapshot, directory, employment):
    """逐候选回读实时在职；读取失败保留未知，不能被当成离职后排除。

    只有组织快照可用且新鲜、且该人员 ID 精确对应零个成员，才判为明确非在职：
    快照不可用或过期、成员多于一个、任何异常都保持未知——它们不是"这个人不在"，
    只是我们暂时看不见，折成离职会让另一候选被误认为唯一在职。
    """
    candidates = email_candidates(email, snapshot)
    members, statuses = {}, {}
    for row in candidates or ():
        personnel = str(row.get("personnel_id") or "").strip()
        if not personnel or personnel in statuses:
            continue
        statuses[personnel] = None
        try:
            lookup = directory.lookup_by_user_id(personnel)
            values = tuple(lookup.members)
            if lookup.availability is not DirectoryAvailability.AVAILABLE:
                continue
            if len(values) == 0:
                statuses[personnel] = EmploymentStatus.absent()
                continue
            if len(values) != 1:
                continue
            member = values[0]
            if member.user_id != personnel or not member.tenant_key or not member.open_id:
                continue
            members[personnel] = member
            statuses[personnel] = employment.status(
                tenant_key=member.tenant_key, open_id=member.open_id
            )
        except Exception:
            # 该候选保持未知；另一候选在职也不足以证明它是唯一在职。
            continue
    result = resolve_email_identity(email, snapshot=snapshot, employment=statuses)
    selected = result.selected
    member = None if selected is None else members.get(selected.get("personnel_id"))
    return LocatedEmailIdentity(result, member)


def read_identity_snapshot(roster):
    """源读取失败与缺元信息一律未决，不用本机时间伪造快照来源。"""
    try:
        snapshot = roster.identity_snapshot()
    except Exception:
        return EmailIdentitySnapshot(None, None, None, available=False)
    if not isinstance(snapshot, EmailIdentitySnapshot):
        return EmailIdentitySnapshot(None, None, None, available=False)
    return snapshot
