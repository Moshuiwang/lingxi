"""预开通：名单里的人在第一次对话之前就完成开通，首聊没有等待。

只放预开通独有的东西，其余共用 ``AutoOnboardingRunner._run``（邮箱闸/零银河兜底/
发布闸各只有一份实现，另开链会让防线各出现第二份）。三处差异：系统触发没有
``inbound_event`` 行，无认领账本、不发"正在等"通知；名单给邮箱而非 ``open_id``，
需提前定位（:func:`locate_by_email`）；完成时静默、首聊时才补提示。

两条既有的停滞收口路径在系统触发下会同时失效：当场收口靠通知确认送达、四十五分钟
兜底靠 ``INNER JOIN inbound_event``，系统触发两者皆无——:func:`deliver_silently`
按送达处理，兜底查询改用 ``LEFT JOIN LATERAL`` 并以 ``provisioning_started_at`` 为租约起点。

邮箱准备阶段不持实时凭据；多候选留待 scheduler 回读后解析，未知或冲突不自动选人。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol

from lingxi.core.conversation.ports import OnboardingResult, OnboardingState
from lingxi.core.identity.email_location import locate_current_email, read_identity_snapshot
from lingxi.core.identity.email_resolver import (
    EmailIdentityResolution,
    EmailIdentitySnapshot,
    EmailIdentityState,
    email_candidates,
    resolve_email_identity,
)
from lingxi.core.identity.onboarding_terminal import (
    KEY_COMPLETED,
    OnboardingChainError,
    _internal,
    _not_authorized,
)
from lingxi.core.identity.org_snapshot import DirectoryAvailability, SnapshotMember
from lingxi.core.permission.account_match import normalize_email

#: 系统触发（预开通）合成事件标识的前缀。**只**用作通知去重键与审计字段，
#: **不落任何表**——尤其不往 ``inbound_event`` 插假行：那张表是入站审计与对账的基线
#: （``/admin trace`` 与未开通首聊交接对账都读它），插一行假事件会让对账把它当成真实
#: 首聊重新认领，等于自己给自己制造一条重复开通链。
SYSTEM_EVENT_PREFIX = "preprovision:"

#: ``onboarding.result`` 审计里的来源标识，用来分辨"首聊时开通"与"首聊前预开通"。
ORIGIN_PREPROVISION = "preprovision"
ORIGIN_FIRST_CHAT = "first_chat"


def system_event_id(trace_id: str) -> str:
    """系统触发这一次的合成事件标识。见 :data:`SYSTEM_EVENT_PREFIX`。"""
    return f"{SYSTEM_EVENT_PREFIX}{trace_id}"


def is_system_trigger(event_id: str) -> bool:
    """这次开通是不是系统触发（预开通）的。

    判据落在合成事件标识上而不是一个额外的入口参数：``event_id`` 已经贯穿
    ``start`` → 执行线程 → ``_execute`` → ``_notify`` 的每一层，用它判定不需要在这条
    链的四个方法上各加一个参数，也就不存在"某一层忘了往下传"的漏接面。
    """
    return str(event_id or "").startswith(SYSTEM_EVENT_PREFIX)


def origin_of(event_id: str) -> str:
    """审计用的来源标识。"""
    return ORIGIN_PREPROVISION if is_system_trigger(event_id) else ORIGIN_FIRST_CHAT


class _NullDispatchLedger:
    """系统触发的认领账本：两个方法都是 no-op。

    ``DispatchLedger`` 的两个方法都以 ``inbound_event`` 那一行为对象——记账是
    ``onboarding_dispatched_at``、放回是把它清成 ``NULL``。系统触发**没有那一行**，
    两件事都没有对象，因此这里是真正意义上的"无事可做"，不是把失败吞掉。

    **不插假事件行**（见 :data:`SYSTEM_EVENT_PREFIX`）。
    """

    def mark_onboarding_dispatched(self, *, event_id: str) -> None:
        return None

    def release_onboarding_claim(self, *, event_id: str, claim_token: Any = None) -> None:
        return None


#: 进程内共享的单例：本类无状态。
NULL_DISPATCH_LEDGER = _NullDispatchLedger()


class _NoticeArmer(Protocol):
    def mark_preprovision_notice_pending(self, *, open_id: str) -> bool: ...


def deliver_silently(*, key: str, open_id: str, users: _NoticeArmer) -> bool:
    """预开通的"投递"：不发任何消息，返回 ``True``（按送达处理）。

    静默是结构性的（链上两处 ``_notify`` 都经过这里，不靠调用点自觉）：预开通是
    我们替用户做的事，在他没有上下文时推一条「开通完成」只会造成困惑。按送达处理
    是为了让「当场收口」照常触发（见模块文档）。

    终态是「开通完成」时，把那句话挂起到该用户首聊时再补——挂起是否真的落下由
    ``UserStateStore.mark_preprovision_notice_pending`` 再判一次"此前从没说过话"，
    同一份名单重跑不会重新挂起（幂等）。
    """
    if key != KEY_COMPLETED:
        return True
    users.mark_preprovision_notice_pending(open_id=open_id)
    return True


# ---------------------------------------------------------------------------
# 邮箱 → 飞书身份的提前定位
# ---------------------------------------------------------------------------

#: 逐人跳过的原因码。**互不合并**：产品负责人拿着清单要能分辨"名单写错了邮箱"
#: （前两个）与"这个人我们这边看不见"（后三个），两类的下一步动作完全不同。
SKIP_EMAIL_BLANK = "email_blank"
SKIP_EMAIL_NOT_IN_ROSTER = "email_not_in_roster"
SKIP_EMAIL_MULTIPLE_PERSONNEL = "email_multiple_personnel"
SKIP_DIRECTORY_UNAVAILABLE = "directory_unavailable"
SKIP_PERSONNEL_NOT_IN_DIRECTORY = "personnel_not_in_directory"
SKIP_DIRECTORY_MULTIPLE_MEMBERS = "directory_multiple_members"
#: 花名册快照本身读不出来（``RosterSource.rows()`` 返回 ``None``）。与"这个邮箱不在
#: 花名册里"分开：前者是我们暂时看不见，后者是名单写错了，下一步动作完全不同。
SKIP_ROSTER_UNAVAILABLE = "roster_unavailable"


@dataclass(frozen=True)
class PreprovisionTarget:
    """准备时唯一的组织成员，或执行时经实时在职解析的成员；不代表已授权。"""

    email: str
    personnel_id: str
    member: SnapshotMember
    identity: EmailIdentityResolution | None = None

    @property
    def open_id(self) -> str:
        """定位到的组织快照成员的 open_id。"""
        return self.member.open_id


@dataclass(frozen=True)
class PreprovisionSkip:
    """被拒绝或尚未能确定身份的名单条目，保留原因和可得的解析事实。"""

    email: str
    reason: str
    identity: EmailIdentityResolution | None = None

    def as_result(self) -> OnboardingResult:
        """确定性拒绝与资料不可用分别返回，系统触发不产生私聊消息。"""
        state = OnboardingState.NOT_AUTHORIZED
        if self.identity is not None and self.identity.state is EmailIdentityState.UNAVAILABLE:
            state = OnboardingState.INTERNAL_ERROR
        return OnboardingResult(state=state, failure_reason=self.reason)


class PreprovisionDeferred(PreprovisionSkip):
    """管理员确认的是待执行解析的邮箱，尚未选择人员、写入资格或开通。"""


class DirectoryByUserId(Protocol):
    """按飞书 ``user_id``（＝花名册「人员ID」）回读组织快照成员的只读口。

    真实实现是 ``adapters/postgres_identity.PostgresOrgSnapshotStore.lookup_by_user_id``。
    正式首聊路径的方向与此**相反**（``open_id`` → 成员 → ``user_id`` → 花名册 → 银河），
    组织快照适配器此前只有按 ``open_id`` 查这一条 SQL；预开通是唯一需要反向走的路径。
    """

    def lookup_by_user_id(self, user_id: str) -> Any:
        """按 ``user_id`` 回读组织快照候选成员。"""
        ...


def locate_by_email(
    email: str,
    *,
    snapshot: EmailIdentitySnapshot,
    directory: DirectoryByUserId,
) -> PreprovisionTarget | PreprovisionSkip:
    """准备阶段只定位候选；多行保留待执行解析，绝不假设候选在职。"""
    needle = normalize_email(email)
    if not needle:
        return PreprovisionSkip(email=email, reason=SKIP_EMAIL_BLANK)
    candidates = email_candidates(email, snapshot)
    if candidates is None:
        return PreprovisionSkip(email=email, reason=SKIP_ROSTER_UNAVAILABLE)
    if not candidates:
        return PreprovisionSkip(email=email, reason=SKIP_EMAIL_NOT_IN_ROSTER)
    if len(candidates) > 1:
        result = resolve_email_identity(email, snapshot=snapshot, employment={})
        return PreprovisionDeferred(
            email,
            "identity_resolution_pending",
            replace(result, reason="execution_resolution_pending"),
        )
    personnel = str(candidates[0].get("personnel_id") or "").strip()
    if not personnel:
        return PreprovisionSkip(email=email, reason="identity_field_unknown")
    lookup = directory.lookup_by_user_id(personnel)
    if getattr(lookup, "availability", None) is not DirectoryAvailability.AVAILABLE:
        # 组织资料不可用 / 已过九十天上限时"查不到"不是事实，只是我们暂时看不见——
        # 与 ``AutoOnboardingRunner._locate`` 同一条纪律，不当成"这个人不存在"。
        return PreprovisionSkip(email=email, reason=SKIP_DIRECTORY_UNAVAILABLE)
    members = tuple(getattr(lookup, "members", ()) or ())
    if not members:
        return PreprovisionSkip(email=email, reason=SKIP_PERSONNEL_NOT_IN_DIRECTORY)
    if len(members) > 1:
        return PreprovisionSkip(email=email, reason=SKIP_DIRECTORY_MULTIPLE_MEMBERS)
    return PreprovisionTarget(email=email, personnel_id=personnel, member=members[0])


def plan_preprovision(
    emails: Iterable[str],
    *,
    snapshot: EmailIdentitySnapshot,
    directory: DirectoryByUserId,
) -> tuple[tuple[PreprovisionTarget, ...], tuple[PreprovisionSkip, ...]]:
    """把一份名单逐条定位成「可执行」与「跳过」两张清单，**保持名单顺序**。

    同一个邮箱在名单里出现多次只处理一次（后一次静默丢弃）：那是名单本身的笔误，
    不是两个人；对同一个人跑两次开通链没有任何新结果，只会多一条重复的审计。

    **逐人不阻塞**：本函数只做定位，一条都不会抛异常带走其余人；真正的开通由调用方
    （``scripts/ops`` 的预开通入口）逐人 ``try/except`` 调用
    ``AutoOnboardingRunner.start_system``。
    """
    targets: list[PreprovisionTarget] = []
    skips: list[PreprovisionSkip] = []
    seen: set[str] = set()
    for raw in emails:
        normalized = normalize_email(raw)
        if normalized and normalized in seen:
            continue
        if normalized:
            seen.add(normalized)
        located = locate_by_email(raw, snapshot=snapshot, directory=directory)
        if isinstance(located, PreprovisionTarget):
            targets.append(located)
        else:
            skips.append(located)
    return tuple(targets), tuple(skips)


# ---------------------------------------------------------------------------
# 系统触发入口
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreprovisionGrant:
    """随预开通链一起落库的那一笔预授权，以及谁要为它负责。

    ``plan`` 的具体形态由落库口（``PostgresLocalPermissionOverrideStore.import_position_grant``）
    定义，本模块刻意不去认识它：只负责交给落库口、并保证落库发生在零银河判定之前，
    多认识一层会在此处长出第二份权限展开逻辑。

    ``initiated_by_open_id`` 写进合成 ``pending_action`` 的责任人栏，没有默认值：
    审计栏目里一个无法追溯的假身份比没有审计更糟。
    """

    plan: Any
    initiated_by_open_id: str


class PositionGrantImporter(Protocol):
    """预授权的落库口（``PostgresLocalPermissionOverrideStore.import_position_grant``）。

    形状照 ``LegacyPermissionImporter.import_plan``：每用户一事务——合成一条已终态的
    ``pending_action``（``local_permission_override.pending_action_id`` 是结构性
    NOT NULL 外键，没有确认卡不能写入）与全部 ``local_permission_override`` 行原子
    落库，撞唯一索引降级为 ``already_present``。

    任何异常原样上抛，由 ``AutoOnboardingRunner`` 按本侧故障 fail-closed（外部表零
    写入）：一笔没落下去的预授权绝不能让这个人带着"少了权限"的范围被发布出去。
    """

    def import_position_grant(
        self,
        *,
        user_id: str,
        target_open_id: str,
        grant: Any,
        now: datetime,
        initiated_by_open_id: str,
    ) -> Any:
        """把这一笔预授权原子落库。"""
        ...


def import_preprovision_grant(
    importer: PositionGrantImporter | None,
    grant: PreprovisionGrant,
    *,
    user_id: str,
    open_id: str,
    now: datetime,
    audit: Any,
    trace_id: str,
) -> None:
    """把这一笔预授权落库。**落库口没装配就整链失败关闭**，绝不静默跳过。

    静默跳过的用户可见后果是：这个人被开通了、令牌签了、正式表也写了，但**范围里没有
    名单答应给他的那部分权限**——他能问数，只是问不出该问的东西，而当天没有任何东西
    会报警。宁可响亮失败。
    """
    if importer is None:
        raise OnboardingChainError("preprovision_grant_importer_not_wired")
    report = importer.import_position_grant(
        user_id=user_id,
        target_open_id=open_id,
        grant=grant.plan,
        now=now,
        initiated_by_open_id=grant.initiated_by_open_id,
    )
    # 审计只记计数类事实，不记权限内容、邮箱或姓名（与本链其余审计同一条纪律）。
    audit.record(
        "onboarding.preprovision_grant_imported",
        user=user_id,
        trace_id=trace_id,
        report=str(type(report).__name__),
    )


class _SystemOnboardingHost(Protocol):
    """:func:`run_system_onboarding` 用到的编排内部面，显式写出来而不是隐式 duck-type。

    只有 ``AutoOnboardingRunner`` 实现它。之所以让入口住在本模块而不是编排类里：
    ``onboarding_runner.py`` 贴着体量棘轮阈值（1500 行），而这一段全部是**预开通独有**
    的形状——首聊路径一行都不走它。
    """

    _roster: Any
    _directory: Any
    _employment: Any
    _audit: Any
    _email_bindings: Any
    _lock: Any
    _running: dict[str, str]

    def _execute(
        self,
        *,
        event_id: str,
        open_id: str,
        trace_id: str,
        claim_token: Any = None,
        grant: Any = None,
    ) -> Any: ...

    def _release(self, open_id: str, event_id: str) -> None: ...

    def _report_terminal(self, terminal, *, event_id: str, trace_id: str) -> None: ...


def resolve_system_email(runner: _SystemOnboardingHost, *, email: str, trace_id: str):
    """唯一实时在职成员才可进入系统开通；来源及失败判据一并写审计。"""
    located = locate_current_email(
        email,
        snapshot=read_identity_snapshot(runner._roster),
        directory=runner._directory,
        employment=runner._employment,
    )
    result = located.resolution
    if result.selected is not None:
        result = _check_existing_keys(runner, email, located)
    runner._audit.record("identity.email_resolved", trace_id=trace_id, **result.audit_facts())
    if result.selected is None:
        return PreprovisionSkip(email, "email_identity_" + result.state.value, result)
    return PreprovisionTarget(email, located.member.user_id, located.member, result)


def _check_existing_keys(runner, email, located):
    """同一飞书主体也不能借邮箱重选覆盖已经保存的人员或工号。"""
    result = located.resolution
    try:
        bindings = runner._email_bindings.bindings_for_email(normalize_email(email))
    except Exception:
        return replace(
            result,
            state=EmailIdentityState.UNAVAILABLE,
            reason="binding_unavailable",
            selected=None,
        )
    for binding in bindings:
        if binding.feishu_open_id != located.member.open_id:
            continue  # 不同主体仍由开通链既有邮箱闸拒绝并登记故障。
        result = result.check_binding(
            personnel_id=binding.personnel_id, employee_no=binding.employee_no
        )
    return result


def run_system_onboarding(
    runner: _SystemOnboardingHost,
    *,
    email: str,
    trace_id: str,
    origin: str = ORIGIN_PREPROVISION,
    initiated_by_open_id: str,
    preprovision_grant: Any | None = None,
    expected_open_id: str | None = None,
) -> OnboardingResult:
    """系统触发一次开通，**同步返回终态**。形状与三处差异见模块文档「入口的三处形状」。

    ``origin`` 目前只接受 :data:`ORIGIN_PREPROVISION`：v1 只有"预开通"这一个系统来源，
    而这个值同时决定合成事件标识的前缀（:func:`system_event_id`），也就是链上"要不要
    静默、要不要记账"的判据。收下一个不认识的来源再继续跑，等于让调用方随手把一条链
    变成"不静默但也没有账本"的第三种形态——**失败关闭**，不猜。

    花名册读不出来（``rows()`` 返回 ``None``）时同样跳过而不是当成"查无此人"：那是
    我们暂时看不见，不是这个邮箱不存在，两者的下一步动作完全不同。
    """
    if origin != ORIGIN_PREPROVISION:
        raise ValueError("系统触发目前只有预开通这一个来源")
    if not str(initiated_by_open_id or "").strip():
        raise ValueError("预开通必须带上责任人 open_id：审计里不接受占位身份")
    located = resolve_system_email(runner, email=email, trace_id=trace_id)
    if isinstance(located, PreprovisionSkip):
        return _report_system_rejection(runner, located.as_result(), trace_id)
    if expected_open_id is not None and located.open_id != expected_open_id:
        runner._audit.record("identity.email_binding_mismatch", trace_id=trace_id)
        return _report_system_rejection(
            runner,
            OnboardingResult(
                state=OnboardingState.INTERNAL_ERROR,
                failure_reason="email_identity_binding_mismatch",
            ),
            trace_id,
        )

    event_id = system_event_id(trace_id)
    grant = (
        None
        if preprovision_grant is None
        else PreprovisionGrant(plan=preprovision_grant, initiated_by_open_id=initiated_by_open_id)
    )
    with runner._lock:
        # 与 ``start()`` 共用同一把锁和同一个登记表：一个人同时被脚本和自己的首聊触发
        # 是真实形状（名单里的人恰好在这一刻发了第一条消息），两条链必须互斥。
        if runner._running.setdefault(located.open_id, event_id) != event_id:
            return OnboardingResult(state=OnboardingState.STARTED, failure_reason="already_running")
    try:
        terminal = runner._execute(
            event_id=event_id, open_id=located.open_id, trace_id=trace_id, grant=grant
        )
    finally:
        runner._release(located.open_id, event_id)
    if terminal is None:
        # 停机信号落在链中途：``_execute`` 已经如实记了审计、什么也没收口。
        terminal = _internal("aborted_while_stopping")
    return terminal.as_result(trace_id=trace_id)


def _report_system_rejection(runner, result, trace_id):
    """提前解析失败也写入既有终态与排障记录；系统触发仍不私聊用户。"""
    terminal = (
        _internal(result.failure_reason)
        if result.state is OnboardingState.INTERNAL_ERROR
        else _not_authorized(result.failure_reason)
    )
    runner._report_terminal(terminal, event_id=system_event_id(trace_id), trace_id=trace_id)
    return result


__all__ = [
    "DirectoryByUserId",
    "NULL_DISPATCH_LEDGER",
    "ORIGIN_FIRST_CHAT",
    "ORIGIN_PREPROVISION",
    "PositionGrantImporter",
    "PreprovisionGrant",
    "PreprovisionSkip",
    "PreprovisionTarget",
    "SKIP_DIRECTORY_MULTIPLE_MEMBERS",
    "SKIP_DIRECTORY_UNAVAILABLE",
    "SKIP_EMAIL_BLANK",
    "SKIP_EMAIL_MULTIPLE_PERSONNEL",
    "SKIP_EMAIL_NOT_IN_ROSTER",
    "SKIP_PERSONNEL_NOT_IN_DIRECTORY",
    "SKIP_ROSTER_UNAVAILABLE",
    "SYSTEM_EVENT_PREFIX",
    "deliver_silently",
    "import_preprovision_grant",
    "is_system_trigger",
    "locate_by_email",
    "origin_of",
    "plan_preprovision",
    "run_system_onboarding",
    "system_event_id",
]
