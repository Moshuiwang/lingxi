"""飞书身份到银河账号的匹配结果（纯函数）。

链路：飞书 user_id → 花名册「人员ID」→ 工号（主）/ 邮箱（辅）→ 银河
`user.user_name` / `user.email`。花名册工号覆盖率约 99%，是「工号为主」的依据；
邮箱少量缺失并混有公共邮箱，只作辅键（本模块是 V-开通-02/03/06/09 的纯函数层）。

``matched`` 要求工号或邮箱唯一命中且账号主键完整，交给后续门禁，不能据此直接
宣告开通；``not_found`` 覆盖零条、多条、两键冲突、资料不完整等任何非唯一成功
结果，统一给无可用银河权限提示，不建待办。判定次序：花名册查无/多行、或工号与
邮箱均缺失 → `not_found`；工号存在按 `user_name` 精确匹配，两键各自唯一命中且
指向不同账号视为冲突；工号未命中时邮箱回退。`nick_name` 只作辅助核对；工号按
精确字符串比较，不做数值化（`0012` 与 `12` 不是同一账号）。花名册行的
`employee_no` 键由接线层从 `RosterPersonRow.work_no` 映射而来。
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
    """一次匹配的完整结果：状态、原因、命中的银河账号与匹配所用的键。"""

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


def _locate_roster_row(
    person_key: str, roster_rows: Iterable[Mapping[str, Any]]
) -> AccountMatch | tuple[str, str, str]:
    """按人员 ID 定位唯一花名册行；找不到或不唯一时直接给出最终结果。

    返回 ``AccountMatch`` 表示已经是最终结果；返回三元组
    ``(roster_name, employee_no, email)`` 表示定位成功，交给调用方继续按键匹配。
    """
    candidates = [row for row in roster_rows if _text(row.get("personnel_id")) == person_key]
    if not candidates:
        return _outcome(NOT_FOUND, "roster_not_found")

    roster_name = _text(candidates[0].get("name"))
    if len(candidates) > 1:
        return _outcome(NOT_FOUND, "roster_multiple_rows", roster_name=roster_name)

    employee_no = _text(candidates[0].get("employee_no"))
    email = normalize_email(candidates[0].get("email"))
    return roster_name, employee_no, email


def _match_by_employee_no(
    employee_no: str,
    email: str,
    employee_no_hits: list[Mapping[str, Any]],
    email_hits: list[Mapping[str, Any]],
    roster_name: str,
) -> AccountMatch | None:
    """按工号判定；返回 ``None`` 表示工号未命中且有邮箱可回退，交给邮箱匹配继续。"""
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
        # 工号未命中且无邮箱可回退：按工号单键定论（缺邮箱按工号）。
        return _outcome(
            NOT_FOUND,
            "galaxy_account_not_found",
            matched_employee_no=employee_no,
            roster_name=roster_name,
        )
    return None


def _match_by_email(
    email: str,
    email_hits: list[Mapping[str, Any]],
    employee_no: str,
    roster_name: str,
) -> AccountMatch:
    """工号缺失或未命中时的邮箱回退判定，兼作两者都失败时的最终收口。"""
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

    located = _locate_roster_row(person_key, roster_rows)
    if isinstance(located, AccountMatch):
        return located
    roster_name, employee_no, email = located
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
        result = _match_by_employee_no(
            employee_no, email, employee_no_hits, email_hits, roster_name
        )
        if result is not None:
            return result

    return _match_by_email(email, email_hits, employee_no, roster_name)
