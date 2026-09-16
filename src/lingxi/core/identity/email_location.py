"""以既有组织读取口和实时状态读取口装配邮箱解析，不持有平台凭据。"""

from dataclasses import dataclass

from lingxi.core.identity.email_resolver import (
    EmailIdentityResolution,
    EmailIdentitySnapshot,
    email_candidates,
    resolve_email_identity,
)
from lingxi.core.identity.org_snapshot import DirectoryAvailability, SnapshotMember


@dataclass(frozen=True)
class LocatedEmailIdentity:
    """身份结论与其对应的可信组织成员，失败时不提供可开通主体。"""

    resolution: EmailIdentityResolution
    member: SnapshotMember | None = None


def locate_current_email(email, *, snapshot, directory, employment):
    """逐候选回读实时在职；读取失败保留未知，不能被当成离职后排除。"""
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
            if lookup.availability is not DirectoryAvailability.AVAILABLE or len(values) != 1:
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
