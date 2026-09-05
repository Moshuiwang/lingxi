"""从库里读到的事实装配成欢迎卡的取值输入（纯函数，失败关闭）。

**预检与正式发送必须走这一个装配**（同一渲染函数、同一数据形状），否则预检验不出
真问题。装配的判据只有状态，不猜时间：只对
``provisioning_state=active`` 且 ``account_state=enabled`` 的人产出可发送的取值，
因此经迟到就绪恢复才激活的人在下一次 ``--apply`` 里自然被捞到。

任何一条说不清楚（没有 open_id、权限读不懂、花名册姓名不唯一）都产出跳过原因，
不产出一个"猜出来的"取值——发出去的消息不可撤回。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from lingxi.config.content import ContentCatalog, default_content_catalog
from lingxi.core.outreach.welcome_card import WelcomeAudience, company_scope_text
from lingxi.core.permission.publish_row import ALL_COMPANIES_KEY, lookup_metrics, parse_permissions

#: 可以收到主动告知的两个状态。**账号状态一并核对**：停用的人收到"你现在可以开始
#: 提问"是一句当场就会被证伪的话。
ACTIVE_PROVISIONING_STATE = "active"
ENABLED_ACCOUNT_STATE = "enabled"

#: 跳过原因。只记类别，不记这个人的资料值。
SKIP_NOT_FOUND = "not_in_app_user"
SKIP_NOT_ACTIVE = "not_active"
SKIP_NO_OPEN_ID = "no_open_id"
SKIP_NO_PERMISSIONS = "no_published_permissions"
SKIP_UNREADABLE_PERMISSIONS = "permissions_unreadable"
SKIP_NO_METRICS = "no_metrics"
SKIP_AMBIGUOUS_NAME = "roster_name_ambiguous"
SKIP_COMPANY_NAME_MISSING = "company_name_missing"


@dataclass(frozen=True)
class SubjectFacts:
    """一个人在库里的原始事实，由适配器一次读齐后原样传进来。"""

    email: str
    user_id: str | None = None
    open_id: str | None = None
    provisioning_state: str | None = None
    account_state: str | None = None
    permissions: str | None = None
    roster_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class AudiencePlan:
    """装配结果：要么给出可发送的取值，要么给出不发的原因。

    ``company_scope``/``metric_count`` 即使在不可发送时也尽量给出，dry-run 清单
    因此能显示"这个人本来会看到什么范围"，而不是只显示一句跳过。
    """

    email: str
    audience: WelcomeAudience | None
    skip_reason: str | None
    active: bool
    company_scope: str = ""
    metric_count: int = 0

    @property
    def sendable(self) -> bool:
        """是否可以发送。"""
        return self.audience is not None


def _roster_display_name(facts: SubjectFacts) -> str | None:
    """花名册原文姓名；同一邮箱命中多个不同姓名时返回 ``None``（不猜）。

    重复邮箱在花名册里是实测常态（同一个人离职再入职），姓名一致时按同一个人
    处理；姓名不一致就说明这个邮箱指向的不是同一个人，不能替他决定发给谁。
    """
    names = tuple(dict.fromkeys(name.strip() for name in facts.roster_names if name.strip()))
    if len(names) == 1:
        return names[0]
    return None


def _parse_scope(permissions: str) -> tuple[tuple[str, ...], bool, tuple[str, ...]]:
    """把已发布的权限文本读成「公司编号、是否通配、指标名」。"""
    document = parse_permissions(permissions)
    all_companies = ALL_COMPANIES_KEY in document
    company_ids = () if all_companies else tuple(sorted(document))
    return company_ids, all_companies, lookup_metrics(document)


def _skip(facts: SubjectFacts, reason: str, *, active: bool) -> AudiencePlan:
    return AudiencePlan(email=facts.email, audience=None, skip_reason=reason, active=active)


def plan_outreach(
    facts: SubjectFacts,
    *,
    company_names: Mapping[str, str],
    total_company_count: int,
    catalog: ContentCatalog | None = None,
) -> AudiencePlan:
    """把一个人的库内事实装配成欢迎卡取值，或给出不发的原因。

    ``company_names`` 是编号→中文名（查不到中文名的人整条跳过，见 :func:`_build_plan`）；
    ``total_company_count`` 是当前可用公司总数，只在通配范围下用来把「全部公司」
    说成一个数字。
    """
    if facts.user_id is None:
        return _skip(facts, SKIP_NOT_FOUND, active=False)
    active = (
        facts.provisioning_state == ACTIVE_PROVISIONING_STATE
        and facts.account_state == ENABLED_ACCOUNT_STATE
    )
    if not active:
        return _skip(facts, SKIP_NOT_ACTIVE, active=False)
    if not (facts.open_id or "").strip():
        return _skip(facts, SKIP_NO_OPEN_ID, active=True)
    display_name = _roster_display_name(facts)
    if display_name is None:
        return _skip(facts, SKIP_AMBIGUOUS_NAME, active=True)
    if not (facts.permissions or "").strip():
        return _skip(facts, SKIP_NO_PERMISSIONS, active=True)
    try:
        company_ids, all_companies, metrics = _parse_scope(facts.permissions or "")
    except ValueError:
        return _skip(facts, SKIP_UNREADABLE_PERMISSIONS, active=True)
    if not metrics:
        return _skip(facts, SKIP_NO_METRICS, active=True)
    return _build_plan(
        facts,
        display_name=display_name,
        company_ids=company_ids,
        all_companies=all_companies,
        metrics=metrics,
        company_names=company_names,
        total_company_count=total_company_count,
        catalog=catalog or default_content_catalog(),
    )


def _build_plan(
    facts: SubjectFacts,
    *,
    display_name: str,
    company_ids: tuple[str, ...],
    all_companies: bool,
    metrics: tuple[str, ...],
    company_names: Mapping[str, str],
    total_company_count: int,
    catalog: ContentCatalog,
) -> AudiencePlan:
    """构造取值并顺带算出 dry-run 要显示的折叠结果。

    :class:`WelcomeAudience` 自己会对说不清楚的范围失败关闭；这里把那个异常翻成
    一条跳过原因，不让一个人的资料缺失打断整批。

    公司编号查不到中文名的人**整条跳过**，不回落显示编号：编号是内部标识，把它印在
    一张欢迎卡上既不是这个人看得懂的东西，也说不清楚他的范围到底是什么。判据覆盖他
    范围里的每一个公司，包括会被折叠成计数的那些——连范围都说不全时不发。
    """
    missing = tuple(key for key in company_ids if not company_names.get(key))
    if missing:
        return _skip(facts, SKIP_COMPANY_NAME_MISSING, active=True)
    try:
        audience = WelcomeAudience(
            display_name=display_name,
            company_ids=company_ids,
            all_companies=all_companies,
            metric_names=metrics,
            company_names=dict(company_names),
            total_company_count=total_company_count,
        )
    except ValueError:
        return _skip(facts, SKIP_NO_METRICS if not metrics else SKIP_NO_PERMISSIONS, active=True)
    return AudiencePlan(
        email=facts.email,
        audience=audience,
        skip_reason=None,
        active=True,
        company_scope=company_scope_text(audience, catalog=catalog),
        metric_count=len(metrics),
    )


__all__ = [
    "ACTIVE_PROVISIONING_STATE",
    "ENABLED_ACCOUNT_STATE",
    "SKIP_AMBIGUOUS_NAME",
    "SKIP_COMPANY_NAME_MISSING",
    "SKIP_NOT_ACTIVE",
    "SKIP_NOT_FOUND",
    "SKIP_NO_METRICS",
    "SKIP_NO_OPEN_ID",
    "SKIP_NO_PERMISSIONS",
    "SKIP_UNREADABLE_PERMISSIONS",
    "AudiencePlan",
    "SubjectFacts",
    "plan_outreach",
]
