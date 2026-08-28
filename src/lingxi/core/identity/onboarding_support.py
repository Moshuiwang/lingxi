"""首次开通编排（``onboarding_runner.py``）用到的模块级纯函数：花名册行定位与建档草稿组装。

从 ``core/identity/onboarding_runner.py`` 纯移动拆出（Trace #358 S-H-1，Issue #350 Gate
G-3 裁定 Option A）：只搬定义，不改任何逻辑或文档字符串；``AutoOnboardingRunner`` 通过
``from .onboarding_support import (...)`` 取回这两个名字，因此本模块的公开名字会作为
``onboarding_runner`` 模块的属性再次可见（``tests/test_onboarding_runner.py`` 仍从
``onboarding_runner`` 导入两者）。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from lingxi.core.identity.first_contact import IdentityRecordDraft
from lingxi.core.identity.org_snapshot import SnapshotMember


def roster_row_for(
    personnel_id: str, rows: Sequence[Mapping[str, Any]]
) -> Mapping[str, Any] | None:
    """取该人员 ID 的**唯一**花名册行。

    多行时返回 ``None``——但走到这里已经不可能了：``match_galaxy_account`` 对同一人员 ID
    的多行一律判 ``not_found``（`V-开通-09`）。保留这一格是因为"建档时挑了其中一行"是一种
    会静默把别人的工号挂到这个人身上的错误，不能靠上游记得拦。
    """

    needle = str(personnel_id).strip()
    matched = [row for row in rows if str(row.get("personnel_id", "") or "").strip() == needle]
    return matched[0] if len(matched) == 1 else None


def draft_from_member(member: SnapshotMember) -> IdentityRecordDraft:
    """从快照成员组装建档草稿。

    与 ``decide_first_contact`` 内部的组装**逐字段相同**——那一份是判定的一部分、不外露，
    这一份是编排层拿去建档的。两处必须一致，由 ``tests/test_onboarding_runner.py`` 的专项
    用例钉住：不一致会让"判定说资料齐了"和"实际写进去的资料"分叉。
    """

    department = (
        member.department_names[0].strip()
        if member.department_names and member.department_names[0]
        else ""
    )
    return IdentityRecordDraft(
        feishu_open_id=member.open_id.strip(),
        feishu_user_id=member.user_id.strip(),
        feishu_union_id=member.union_id.strip(),
        display_name=member.display_name.strip(),
        display_name_locale=member.display_name_locale,
        department=department,
        tenant_key=member.tenant_key.strip(),
    )
