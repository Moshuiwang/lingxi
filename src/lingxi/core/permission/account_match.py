"""飞书身份到银河账号的匹配结果（纯函数）。

链路（2026-08-05《花名册身份链与工号邮箱匹配》决策，取代 Issue #17 正文的
`nick_name` 匹配与本切片早期的纯邮箱口径）：

```
飞书 user_id → 花名册「人员ID」→ 工号（主）/ 邮箱（辅）→ 银河 user.user_name / user.email
```

银河 `user.user_name` 实测即纯数字工号（银河用户权限数据结构），花名册工号覆盖率
约 99%——这是「工号为主」的依据；邮箱在银河侧少量缺失并混有公共邮箱，只作辅键。

结果与合同分支的对应（本模块是 V-开通-02/03/06/09 的纯函数层）：

| 状态 | 含义 | 上层动作 |
|---|---|---|
| `matched` | 工号唯一命中；或工号缺失/未命中时邮箱唯一命中，且账号主键完整 | 交给后续职能与权限有效性门禁；不能据此直接宣告开通 |
| `not_found` | 零条、多条、两键冲突、花名册重复或资料不完整等任何非唯一成功结果 | 给统一的无可用银河权限提示，**不建待办** |

判定次序：

1. 花名册查无对应记录 → `not_found`（V-开通-06：不写不完整记录）；
2. 同一人员 ID 多行 → `not_found`（V-开通-09：即使字段相同也不是唯一原始记录）；
3. 工号与邮箱均缺失 → `not_found`（V-开通-06 的必要资料缺失分支）；
4. 工号存在：按 `user_name` 精确匹配。唯一命中即采用（决策记录），但邮箱同时存在、
   邮箱命中集非空且**不含**该账号时视为两键结果冲突 → `not_found`（V-开通-02）；
   工号命中多条 → `not_found`；工号未命中 → 邮箱回退（决策记录「工号缺失**或未命中**
   时以邮箱匹配」）；邮箱缺失时按工号单键定论，未命中 → `not_found`
   （产品负责人 2026-08-05 确认「缺邮箱按工号」）；
5. 工号缺失：邮箱唯一命中 → `matched`；命中多条或未命中 → `not_found`。

`nick_name` 只作为辅助核对信息输出，**不参与任何判定**。银河侧存在缺 email 账号
不再把「查无」整体升级为人工——早期纯邮箱口径下的该保守分支已被工号为主的双键
口径取代（决策记录明确两键均查无即给申请指引）。

输入键名约定：花名册行 `personnel_id` / `employee_no` / `email` / `name`
（`adapters.feishu_roster_bitable.RosterPersonRow.work_no` 在接线层映射为
`employee_no`）；银河行 `user_id` / `user_name` / `email` / `nick_name`。
工号比较为去空白后的精确字符串比较，不做数值化或去前导零——两侧同源为字符串，
数值化会把 `0012` 和 `12` 误判为同一账号。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

MATCHED = "matched"
NOT_FOUND = "not_found"


@dataclass(frozen=True)
class MatchAdvisory:
    """仅供内部诊断与审计的辅助信息，不参与判定，也不创建人工待办。"""

    roster_name: str | None
    galaxy_nick_names: tuple[str, ...]


@dataclass(frozen=True)
class AccountMatch:
    state: str
    reason: str
    galaxy_user_id: str | None
    matched_key: str | None
    matched_employee_no: str | None
    matched_email: str | None
    advisory: MatchAdvisory


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_email(value: Any) -> str:
    """邮箱比较前的归一：去首尾空白、转小写。"""

    return _text(value).lower()


def _outcome(
    state: str,
    reason: str,
    *,
    galaxy_user_id: str | None = None,
    matched_key: str | None = None,
    matched_employee_no: str | None = None,
    matched_email: str | None = None,
    roster_name: str | None = None,
    galaxy_nick_names: tuple[str, ...] = (),
) -> AccountMatch:
    return AccountMatch(
        state=state,
        reason=reason,
        galaxy_user_id=galaxy_user_id,
        matched_key=matched_key,
        matched_employee_no=matched_employee_no,
        matched_email=matched_email,
        advisory=MatchAdvisory(roster_name or None, galaxy_nick_names),
    )


def _nick_names(rows: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    return tuple(_text(row.get("nick_name")) for row in rows if _text(row.get("nick_name")))


def match_galaxy_account(
    feishu_user_id: str,
    roster_rows: Iterable[Mapping[str, Any]],
    galaxy_user_rows: Iterable[Mapping[str, Any]],
) -> AccountMatch:
    """按「人员ID → 工号（主）/ 邮箱（辅）」判定结果，语义见模块文档。

    `roster_rows` 与 `galaxy_user_rows` 都不做预去重：花名册实测存在同一人员 ID 的
    重复行；产品规则要求原始记录必须唯一，因此任何多行都按无可用权限结束。
    """

    person_key = _text(feishu_user_id)
    if not person_key:
        raise ValueError("飞书 user_id 不能为空")

    candidates = [row for row in roster_rows if _text(row.get("personnel_id")) == person_key]
    if not candidates:
        return _outcome(NOT_FOUND, "roster_not_found")

    roster_name = _text(candidates[0].get("name"))
    if len(candidates) > 1:
        return _outcome(NOT_FOUND, "roster_multiple_rows", roster_name=roster_name)

    employee_no = _text(candidates[0].get("employee_no"))
    email = normalize_email(candidates[0].get("email"))
    if not employee_no and not email:
        return _outcome(NOT_FOUND, "required_fields_missing", roster_name=roster_name)

    rows = list(galaxy_user_rows)
    employee_no_hits = [
        row for row in rows if employee_no and _text(row.get("user_name")) == employee_no
    ]
    email_hits = [row for row in rows if email and normalize_email(row.get("email")) == email]

    if email and len(email_hits) > 1:
        # 任一键命中多条都不是唯一成功；即使工号同时唯一命中，也不能吞掉辅键的
        # 多义信号。内部保留原因，用户统一走无可用权限出口。
        return _outcome(
            NOT_FOUND,
            "email_multiple_hits",
            matched_employee_no=employee_no or None,
            matched_email=email,
            roster_name=roster_name,
            galaxy_nick_names=_nick_names(email_hits),
        )

    if employee_no:
        if len(employee_no_hits) == 1:
            hit = employee_no_hits[0]
            if email and email_hits and hit not in email_hits:
                # 两键各自唯一命中却指向不同账号：不自动选择任何一条（V-开通-02）。
                return _outcome(
                    NOT_FOUND,
                    "key_conflict",
                    matched_employee_no=employee_no,
                    matched_email=email,
                    roster_name=roster_name,
                    galaxy_nick_names=_nick_names([*employee_no_hits, *email_hits]),
                )
            galaxy_user_id = _text(hit.get("user_id"))
            if not galaxy_user_id:
                return _outcome(
                    NOT_FOUND,
                    "galaxy_record_incomplete",
                    matched_employee_no=employee_no,
                    matched_email=email or None,
                    roster_name=roster_name,
                    galaxy_nick_names=_nick_names(employee_no_hits),
                )
            return _outcome(
                MATCHED,
                "unique_employee_no_match",
                galaxy_user_id=galaxy_user_id,
                matched_key="employee_no",
                matched_employee_no=employee_no,
                matched_email=email or None,
                roster_name=roster_name,
                galaxy_nick_names=_nick_names(employee_no_hits),
            )
        if len(employee_no_hits) > 1:
            return _outcome(
                NOT_FOUND,
                "employee_no_multiple_hits",
                matched_employee_no=employee_no,
                roster_name=roster_name,
                galaxy_nick_names=_nick_names(employee_no_hits),
            )
        if not email:
            # 工号未命中且无邮箱可回退：按工号单键定论（产品负责人确认「缺邮箱按工号」）。
            return _outcome(
                NOT_FOUND,
                "galaxy_account_not_found",
                matched_employee_no=employee_no,
                roster_name=roster_name,
            )

    if email and len(email_hits) == 1:
        hit = email_hits[0]
        galaxy_user_id = _text(hit.get("user_id"))
        if not galaxy_user_id:
            return _outcome(
                NOT_FOUND,
                "galaxy_record_incomplete",
                matched_employee_no=employee_no or None,
                matched_email=email,
                roster_name=roster_name,
                galaxy_nick_names=_nick_names(email_hits),
            )
        return _outcome(
            MATCHED,
            "unique_email_match",
            galaxy_user_id=galaxy_user_id,
            matched_key="email",
            matched_employee_no=employee_no or None,
            matched_email=email,
            roster_name=roster_name,
            galaxy_nick_names=_nick_names(email_hits),
        )

    return _outcome(
        NOT_FOUND,
        "galaxy_account_not_found",
        matched_employee_no=employee_no or None,
        matched_email=email or None,
        roster_name=roster_name,
    )
