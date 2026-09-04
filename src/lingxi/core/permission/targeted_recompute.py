"""管理员写动作确认执行成功后，对单个用户的定向权限重算 + 发布。

复用与每日批、首次开通同一套 ``core/permission/*`` 纯函数，重新编排成只处理
一个身份的入口，不新建第二套判定规则。与每日批三处刻意不同：不做"花名册
今天更新过"的轮级顺序判据，只要求快照存在；不再有 legacy 沿用参数，与每日批
同用两源合并；``token_cipher`` 固定传 ``None``，真正的"缺密文"失败关闭发生在
之后独立一轮的发布执行器，不在这里（见 :meth:`~TargetedPermissionRecompute._settle_publish`）。

停用（:meth:`~TargetedPermissionRecompute.force_revoke`）与恢复/本地权限动作
（:meth:`~TargetedPermissionRecompute.recompute_and_publish`）是两个方法：
停用要"不管银河怎么说，立刻清空"，走合并管线反而会撤销刚做的停用；恢复类
动作必须走完整合并管线才能答对"现在应得的权限"。身份基线有意包含已停用
用户（停用期间日报仍要更新资料），因此授权路径落库前另有一道账号状态复检，
撤权路径不设——服务对象本来就是刚被停用的人。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Protocol

from lingxi.core.identity.roster_audit import ArchivedIdentity
from lingxi.core.permission.account_match import MATCHED, match_galaxy_account
from lingxi.core.permission.legacy_diff import missing_all_scope_metrics
from lingxi.core.permission.local_override import (
    LocalPermissionOverrideEntry,
    ResolvedLocalOverrides,
    resolve_local_overrides,
)
from lingxi.core.permission.merge_sources import merge_permission_sources
from lingxi.core.permission.metric_translation import (
    UncoveredPermissionCombinationError,
    metric_translation_available,
    translate_company_functions,
)
from lingxi.core.permission.publish import PermissionGrantBlockedByAccountStateError
from lingxi.core.permission.publish_row import (
    ADMIN_FULL_ACCESS_FUNCTION,
    aggregate_permission,
    build_revocation_row,
    build_translated_publish_row,
)

#: ``record_decision(reason=...)`` 的取值——与每日批（``daily_permission_refresh``/
#: ``daily_permission_revoke``）、首次开通（``first_onboarding``）各自独立的字面量，
#: 让运维从 outbox 一眼分辨"这条外部写入是管理员即时动作触发的，不是常规轮次"。
ADMIN_TARGETED_RECOMPUTE_REASON = "admin_action_instant_recompute"
ADMIN_TARGETED_REVOKE_REASON = "admin_action_instant_revoke"

# ---- 跳过原因码：固定字面量，不含任何字段值（同 permission_refresh.py 纪律） ----
SKIP_USER_NOT_ACTIVE = "user_not_active"
SKIP_MISSING_PERSONNEL_ID = "missing_personnel_id"
SKIP_MISSING_ROSTER_SNAPSHOT = "missing_roster_snapshot"
SKIP_NO_GALAXY_BATCH = "no_galaxy_batch"
SKIP_METRIC_TRANSLATION_UNAVAILABLE = "metric_translation_unavailable"
SKIP_METRIC_TRANSLATION_UNCOVERED = "metric_translation_uncovered"
SKIP_MATCH_FAILED = "match_failed"
SKIP_ARCHIVED_IDENTITY_INCOMPLETE = "archived_identity_incomplete"
SKIP_NO_PUBLISHED_ROW = "no_published_row"
SKIP_LOCAL_OVERRIDE_READ_FAILED = "local_override_read_failed"
#: 本地「全部」组（rc25 S-1）下某公司被本地抑制减到空：读侧回退制无法表示，本次既不
#: 发布也不撤权（`merge_sources.py` 「本地 "*" 组」一节）。
SKIP_SUPPRESSION_UNREPRESENTABLE = "suppression_on_all_scope_unrepresentable"
#: 落授权决定的那把行锁里发现这个人不是 ``enabled``：本模块的
#: 身份基线**有意包含** ``suspended``，因此这条跳过是常态出口而不是异常——管理员对
#: 一个已停用用户做本地权限动作时就会走到这里。与 :data:`SKIP_USER_NOT_ACTIVE`
#: 刻意分开登记：那一条说的是"这个人不在花名册基线里"（删除中/已删除/未开通完成），
#: 这一条说的是"人在基线里，但账号状态不允许给他排非空授权"。
SKIP_ACCOUNT_NOT_ENABLED = "account_not_enabled"


class RecomputeKind(str, Enum):
    """一次定向重算的最终归类，供调用方决定审计与回执文案的粒度。"""

    ENQUEUED = "enqueued"
    UNCHANGED = "unchanged"
    REVOKED = "revoked"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class TargetedRecomputeOutcome:
    """一次调用的结论。``reason`` 只在 ``SKIPPED`` 时非空。"""

    kind: RecomputeKind
    reason: str | None = None
    #: 权限确实变化时（``ENQUEUED``/``REVOKED``），同事务清掉的已送达正文事件数；
    #: 与 ``permission_refresh.py`` 的 ``delivered_content_cleared`` 同一字段含义。
    cleared_events: int = 0


class _IdentityLookup(Protocol):
    """按内部 ``app_user_id`` 取回一名**已开通且账号未在删除中**用户的存档三字段。

    真实实现只是把 ``PostgresRosterBaselineReader.load_active_baseline()`` 的
    全量结果按 ``app_user_id`` 过滤——复用同一条 SQL 口径（`V-花名册-10`/
    `V-花名册-11`），不重新发明"什么算已开通"。
    """

    def find_active(self, *, user_id: str) -> ArchivedIdentity | None: ...


class _RevocationIdentityLookup(Protocol):
    """撤权专用的身份查找口。

    与 :class:`_IdentityLookup` 分开，是因为两条路径要的判据本来就不同：授权侧只
    服务"已经开通完成"的人（``provisioning_state = 'active'``），撤权侧要服务的是
    **任何还可能有一条发布内容在外面的人**——包括开通到一半就被停用的那个人。
    合并成一个口会逼两条路径共用同一个基线，而放宽那个基线会让开通中的用户被授权
    管线算出非空权限发布出去，正是这条 Protocol 要避免的事。
    """

    def find_for_revocation(self, *, user_id: str) -> ArchivedIdentity | None: ...


class _RosterRows(Protocol):
    """花名册持久快照的行，供匹配用。

    ``None`` 表示快照尚不存在（部署事实，不判断新鲜度）。
    """

    def load_rows(self) -> Sequence[Mapping[str, Any]] | None:
        """返回当前快照的行；快照尚不存在时返回 ``None``。"""
        ...


class _GalaxySnapshot(Protocol):
    def load_current(self) -> Any: ...


class _PublishHistory(Protocol):
    def has_publish_footprint(self, user_id: str) -> bool: ...


class _Decision(Protocol):
    enqueued: bool
    cleared_events: int


class _DecisionStore(Protocol):
    """落权限决定的写入口。

    ``require_enabled_account`` 是必填关键字参数：授权侧传 ``True``、撤权侧传
    ``False``，账号状态复检落在实现那把已经持有的 ``app_user`` 行锁里。
    """

    def record_decision(
        self,
        *,
        user_id: str,
        row: Any,
        reason: str,
        require_enabled_account: bool,
        decided_at: datetime,
        clear_delivered_content: bool = False,
    ) -> _Decision: ...


class _LocalOverrideReader(Protocol):
    def effective_entries(self, *, user_id: str) -> Sequence[LocalPermissionOverrideEntry]: ...


class _LegacyAllScopeExpander(Protocol):
    """「2.0 迁移导入·全部」组的补行口。

    与 ``apps/scheduler/permission_refresh.py`` 的同名协议各自独立一份。
    """

    def expand_all_scope_group(
        self, *, user_id: str, group_id: str, metrics: Sequence[str], now: datetime
    ) -> int:
        """给该组补齐 ``metrics`` 里缺的指标；返回实际新增的行数。"""
        ...


class AuditSink(Protocol):
    """审计出口。"""

    def record(self, action: str, /, **fields: object) -> None:
        """记一条审计事件。"""
        ...


class TargetedPermissionRecompute:
    """管理员动作确认执行成功后，对**一个**用户的即时重算/发布/撤权。

    只编排：所有判定规则复用 ``core/permission/*`` 既有纯函数（模块文档），本类
    一条业务规则都不重新定义。真实装配见
    ``adapters/postgres_permission_recompute_trigger.py``。
    """

    def __init__(
        self,
        *,
        identities: _IdentityLookup,
        roster_snapshot: _RosterRows,
        galaxy: _GalaxySnapshot,
        decisions: _DecisionStore,
        publish_history: _PublishHistory,
        role_function_map: Mapping[str, str],
        metric_translation_map: Mapping[str, Mapping[str, Sequence[str]]],
        audit: AuditSink,
        local_overrides: _LocalOverrideReader | None = None,
        clock: Callable[[], datetime] | None = None,
        legacy_all_scope: _LegacyAllScopeExpander | None = None,
        revocation_identities: _RevocationIdentityLookup | None = None,
    ) -> None:
        """接线身份/花名册/银河/决定存储/发布历史/审计等协作者与可选覆盖项。"""
        self._identities = identities
        # ``None`` = 装配层没接撤权专用查找口：退回 ``identities.find_active``
        # （真实装配一定要接，见 ``adapters/postgres_permission_recompute_trigger.py``）。
        self._revocation_identities = revocation_identities
        self._roster_snapshot = roster_snapshot
        self._galaxy = galaxy
        self._decisions = decisions
        self._publish_history = publish_history
        self._role_function_map = role_function_map
        self._metric_translation_map = metric_translation_map
        self._audit = audit
        self._local_overrides = local_overrides
        self._legacy_all_scope = legacy_all_scope
        self._clock = clock or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------
    # 停用：不管银河怎么说，立刻清空（模块文档「为什么是两个方法」）
    # ------------------------------------------------------------------

    def force_revoke(self, *, user_id: str) -> TargetedRecomputeOutcome:
        """停用触发的即时撤权：把这个人的发布内容清空，并让任何在途的发布意图失效。

        身份查找走撤权专用口，不走授权侧那份只收 ``provisioning_state = 'active'``
        的基线：否则"首聊开通到一半就被停用"会被判成不在基线里直接跳过，已入队的
        ``first_onboarding`` 意图照样发到正式表，用户被停用了却仍然有一行在越权。
        撤权口只排除已删除账号，不看开通进度。撤权决定本身会推进
        ``app_user.permission_version``，比它旧的在途意图在认领时直接判
        ``superseded``——"撤掉在途意图"复用的就是这条既有机制，不新造第二套。
        """
        identity = self._find_revocation_identity(user_id)
        if identity is None:
            return self._skip(user_id, mode="revoke", reason=SKIP_USER_NOT_ACTIVE)
        if not identity.email or not identity.display_name:
            return self._skip(user_id, mode="revoke", reason=SKIP_ARCHIVED_IDENTITY_INCOMPLETE)
        if not self._publish_history.has_publish_footprint(user_id):
            return self._skip(user_id, mode="revoke", reason=SKIP_NO_PUBLISHED_ROW)
        return self._settle_revocation(user_id, identity, cause="admin_suspend")

    def _find_revocation_identity(self, user_id: str) -> ArchivedIdentity | None:
        """撤权侧的身份查找：接了专用口就用它，没接退回授权侧那份基线。"""
        if self._revocation_identities is not None:
            return self._revocation_identities.find_for_revocation(user_id=user_id)
        return self._identities.find_active(user_id=user_id)

    # ------------------------------------------------------------------
    # 恢复 / 本地权限三类动作：完整合并管线
    # ------------------------------------------------------------------

    def _resolve_preconditions(
        self, user_id: str
    ) -> TargetedRecomputeOutcome | tuple[ArchivedIdentity, Sequence[Mapping[str, Any]], Any]:
        """定位身份、花名册快照与银河批次；任何一项缺失直接给出跳过结论。"""
        identity = self._identities.find_active(user_id=user_id)
        if identity is None:
            return self._skip(user_id, mode="recompute", reason=SKIP_USER_NOT_ACTIVE)
        if not identity.personnel_id:
            return self._skip(user_id, mode="recompute", reason=SKIP_MISSING_PERSONNEL_ID)

        roster_rows = self._roster_snapshot.load_rows()
        if roster_rows is None:
            return self._skip(user_id, mode="recompute", reason=SKIP_MISSING_ROSTER_SNAPSHOT)

        galaxy = self._galaxy.load_current()
        if galaxy is None:
            return self._skip(user_id, mode="recompute", reason=SKIP_NO_GALAXY_BATCH)

        if not metric_translation_available(self._metric_translation_map):
            # 与每日批同一条纪律：翻译层整体不可用时，授权与撤权都不排。
            return self._skip(user_id, mode="recompute", reason=SKIP_METRIC_TRANSLATION_UNAVAILABLE)
        return identity, roster_rows, galaxy

    def _match_identity(
        self,
        user_id: str,
        identity: ArchivedIdentity,
        roster_rows: Sequence[Mapping[str, Any]],
        galaxy: Any,
    ) -> TargetedRecomputeOutcome | Any:
        """按花名册+银河匹配账号；匹配失败或存档字段不完整直接给出跳过结论。"""
        match = match_galaxy_account(identity.personnel_id, roster_rows, galaxy.user_rows)
        if match.state != MATCHED or not match.galaxy_user_id:
            # 匹配失败＝"认不出这个人"，与每日批同一姿态：不做任何撤权/发布写入，
            # 保留发布表现状，交给下一轮每日批。
            return self._skip(
                user_id, mode="recompute", reason=SKIP_MATCH_FAILED, match_reason=match.reason
            )
        if not identity.email or not identity.display_name:
            return self._skip(user_id, mode="recompute", reason=SKIP_ARCHIVED_IDENTITY_INCOMPLETE)
        return match

    def _translate_permissions(
        self, user_id: str, match: Any, galaxy: Any
    ) -> TargetedRecomputeOutcome | tuple[Mapping[str, Sequence[str]], str | None, Any]:
        """聚合银河权限并翻译成指标名；零银河分支不调用翻译，贡献恒为空字典。"""
        aggregate = aggregate_permission(
            galaxy_user_id=match.galaxy_user_id,
            user_role_rows=galaxy.role_rows(match.galaxy_user_id),
            datacountry_rows=galaxy.datacountry_rows(match.galaxy_user_id),
            country_rows=galaxy.country_rows,
            role_function_map=self._role_function_map,
        )
        if not aggregate.granted:
            return {}, aggregate.reason, aggregate
        try:
            company_metrics = translate_company_functions(
                companies=aggregate.companies,
                functions=aggregate.functions,
                all_companies=aggregate.all_companies,
                mapping=self._metric_translation_map,
            )
        except UncoveredPermissionCombinationError:
            return self._skip(user_id, mode="recompute", reason=SKIP_METRIC_TRANSLATION_UNCOVERED)
        return company_metrics, None, aggregate

    def recompute_and_publish(self, *, user_id: str) -> TargetedRecomputeOutcome:
        """恢复/本地权限动作触发的完整合并管线：银河 ∪ 本地授权 − 本地抑制。"""
        resolved = self._resolve_preconditions(user_id)
        if isinstance(resolved, TargetedRecomputeOutcome):
            return resolved
        identity, roster_rows, galaxy = resolved

        matched = self._match_identity(user_id, identity, roster_rows, galaxy)
        if isinstance(matched, TargetedRecomputeOutcome):
            return matched

        translated = self._translate_permissions(user_id, matched, galaxy)
        if isinstance(translated, TargetedRecomputeOutcome):
            return translated
        company_metrics, cause, aggregate = translated

        local = self._resolve_local_overrides(user_id)
        # `all_companies=True` 有两个独立成因（国家层面通配，或持有
        # `ADMIN_FULL_ACCESS_FUNCTION`），只有后者是「真全指标通配」——
        # `merge_permission_sources` 自己不猜测，调用方必须显式声明。零银河分支
        # `aggregate.functions` 恒为空元组，这里的 `in` 判据天然为 False。
        merged = merge_permission_sources(
            galaxy=company_metrics,
            local=local,
            full_access_wildcard=ADMIN_FULL_ACCESS_FUNCTION in aggregate.functions,
        )
        for reason in merged.skipped_reasons:
            # 通配用户（银河"后台管理员"，all_companies=True）场景：本地覆盖整体
            # 不参与合并——审计明确说明这次调用为什么没有产生预期变化。
            self._audit.record(
                "permission_targeted_recompute.local_override_skipped",
                user=user_id,
                reason=reason,
            )

        if merged.unrepresentable_companies:
            return self._skip(user_id, mode="recompute", reason=SKIP_SUPPRESSION_UNREPRESENTABLE)

        if not merged.permissions:
            if not self._publish_history.has_publish_footprint(user_id):
                return self._skip(
                    user_id,
                    mode="recompute",
                    reason=SKIP_NO_PUBLISHED_ROW,
                    cause=cause or "fully_suppressed",
                )
            return self._settle_revocation(user_id, identity, cause=cause or "fully_suppressed")

        return self._settle_publish(user_id, identity, merged.permissions)

    # ------------------------------------------------------------------
    # 收尾（授权/撤权共用的落库 + 审计）
    # ------------------------------------------------------------------

    def _settle_publish(
        self, user_id: str, identity: ArchivedIdentity, company_metrics: Mapping[str, Sequence[str]]
    ) -> TargetedRecomputeOutcome:
        now = self._clock()
        if not self._publish_history.has_publish_footprint(user_id):
            # 此刻这个人在发布链上没有任何足迹：即将调用的 record_decision 不会
            # 因此失败（它只把这份没有 token_cipher 的快照原样记成 ENQUEUED），
            # 真正的失败关闭发生在之后独立一轮的发布执行器，运维不会自然地把
            # 两处联系起来——这条审计让这个角落在这里就可分辨。
            self._audit.record(
                "permission_targeted_recompute.publish_needs_cipher",
                user=user_id,
            )
        row = build_translated_publish_row(
            company_metrics=company_metrics,
            email=identity.email,
            display_name=identity.display_name,
            decided_at=now,
            # 只读既有密文的读取口不在本模块（模块文档「三处刻意不同」第 3 条）。
            token_cipher=None,
        )
        try:
            decision = self._decisions.record_decision(
                user_id=user_id,
                row=row,
                reason=ADMIN_TARGETED_RECOMPUTE_REASON,
                # 本模块的身份基线有意包含 ``suspended``，这里是唯一挡住"给已停用
                # 用户重新发权"的地方，判据在实现的行锁里。
                require_enabled_account=True,
                decided_at=now,
                clear_delivered_content=True,
            )
        except PermissionGrantBlockedByAccountStateError as blocked:
            # 常态出口，不是故障：事务整体回滚，这个人的发布内容一个字节都没变。
            return self._skip(
                user_id,
                mode="recompute",
                reason=SKIP_ACCOUNT_NOT_ENABLED,
                account_state=blocked.account_state,
            )
        kind = RecomputeKind.ENQUEUED if decision.enqueued else RecomputeKind.UNCHANGED
        self._audit.record(
            "permission_targeted_recompute.completed",
            user=user_id,
            mode="recompute",
            kind=kind.value,
            cleared=decision.cleared_events,
        )
        return TargetedRecomputeOutcome(kind=kind, cleared_events=decision.cleared_events)

    def _settle_revocation(
        self, user_id: str, identity: ArchivedIdentity, *, cause: str
    ) -> TargetedRecomputeOutcome:
        now = self._clock()
        row = build_revocation_row(
            email=identity.email, display_name=identity.display_name, decided_at=now
        )
        decision = self._decisions.record_decision(
            user_id=user_id,
            row=row,
            reason=ADMIN_TARGETED_REVOKE_REASON,
            # 撤权任何账号状态都必须放行——``force_revoke`` 的服务对象本来就是
            # 刚被停用的人，挡住它等于让停用彻底失效。
            require_enabled_account=False,
            decided_at=now,
            clear_delivered_content=True,
        )
        kind = RecomputeKind.REVOKED if decision.enqueued else RecomputeKind.UNCHANGED
        self._audit.record(
            "permission_targeted_recompute.completed",
            user=user_id,
            mode="revoke",
            kind=kind.value,
            cause=cause,
            cleared=decision.cleared_events,
        )
        return TargetedRecomputeOutcome(kind=kind, cleared_events=decision.cleared_events)

    def _resolve_local_overrides(self, user_id: str) -> ResolvedLocalOverrides | None:
        """读取本地覆盖；失败只降级为没有本地源，不整次调用失败。

        ``None`` 对合并恒等，响亮记一条审计。本模块两条业务路径的银河/既有发布
        内容都已经确定要不要发布，本地源读取失败不改变这件事本身，只是让合并
        少了本地这一份。
        """
        if self._local_overrides is None:
            return None
        try:
            entries = tuple(self._local_overrides.effective_entries(user_id=user_id))
        except Exception:  # 本地源读取失败只降级，不带走整次调用
            self._audit.record(
                "permission_targeted_recompute.local_override_skipped",
                user=user_id,
                reason=SKIP_LOCAL_OVERRIDE_READ_FAILED,
            )
            return None
        entries = self._expand_legacy_all_scope(user_id, entries)
        return resolve_local_overrides(user_id=user_id, entries=entries)

    def _expand_legacy_all_scope(
        self, user_id: str, entries: tuple[LocalPermissionOverrideEntry, ...]
    ) -> tuple[LocalPermissionOverrideEntry, ...]:
        """「2.0 迁移导入·全部」组随当前映射补齐新指标。

        与每日批 ``permission_refresh._expand_legacy_all_scope`` 同一语义：缺才补、
        同组 ID、只看生效条目；补行或重读失败只审计、不影响本次既有结果。
        """
        if self._legacy_all_scope is None:
            return entries
        missing = missing_all_scope_metrics(entries, self._metric_translation_map)
        if not missing:
            return entries
        added_total = 0
        for group_id, metrics in missing.items():
            try:
                added = self._legacy_all_scope.expand_all_scope_group(
                    user_id=user_id, group_id=group_id, metrics=metrics, now=self._clock()
                )
            except Exception as error:
                self._audit.record(
                    "permission_targeted_recompute.legacy_all_scope_refresh_failed",
                    user=user_id,
                    error=type(error).__name__,
                )
                continue
            self._audit.record(
                "permission_targeted_recompute.legacy_all_scope_refreshed",
                user=user_id,
                added=added,
            )
            added_total += added
        if added_total == 0:
            return entries
        try:
            return tuple(self._local_overrides.effective_entries(user_id=user_id))
        except Exception:
            return entries

    def _skip(
        self, user_id: str, *, mode: str, reason: str, **extra: object
    ) -> TargetedRecomputeOutcome:
        self._audit.record(
            "permission_targeted_recompute.skipped",
            user=user_id,
            mode=mode,
            reason=reason,
            **extra,
        )
        return TargetedRecomputeOutcome(kind=RecomputeKind.SKIPPED, reason=reason)


__all__ = [
    "ADMIN_TARGETED_RECOMPUTE_REASON",
    "ADMIN_TARGETED_REVOKE_REASON",
    "AuditSink",
    "RecomputeKind",
    "SKIP_ACCOUNT_NOT_ENABLED",
    "SKIP_ARCHIVED_IDENTITY_INCOMPLETE",
    "SKIP_LOCAL_OVERRIDE_READ_FAILED",
    "SKIP_MATCH_FAILED",
    "SKIP_METRIC_TRANSLATION_UNAVAILABLE",
    "SKIP_METRIC_TRANSLATION_UNCOVERED",
    "SKIP_MISSING_PERSONNEL_ID",
    "SKIP_MISSING_ROSTER_SNAPSHOT",
    "SKIP_NO_GALAXY_BATCH",
    "SKIP_NO_PUBLISHED_ROW",
    "SKIP_USER_NOT_ACTIVE",
    "TargetedPermissionRecompute",
    "TargetedRecomputeOutcome",
]
