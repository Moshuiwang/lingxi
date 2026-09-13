"""管理员写动作确认执行成功后，对单个用户的定向权限重算 + 发布。

逐用户决策树与每日批共用 :mod:`lingxi.core.permission.user_decision_tree` 同一份，
本模块只负责入口特有的三件事：按内部标识查身份、把决策树的出口翻成本入口的结论与
审计事件、以及三处显式参数的取值——花名册快照只要求存在（不做"今天更新过"的轮级
判据，那是每日批整轮前置的事）；本地覆盖来源由本模块装配；令牌密文读取口**不接**
（本进程没有主密钥，发布行不带密文，真正的"缺密文"失败关闭发生在之后独立一轮的
发布执行器）。

停用（:meth:`~TargetedPermissionRecompute.force_revoke`）与恢复/本地权限动作
（:meth:`~TargetedPermissionRecompute.recompute_and_publish`）是两个方法：停用要
"不管银河怎么说，立刻清空"，走合并管线反而会撤销刚做的停用；恢复类动作必须走完整
合并管线才能答对"现在应得的权限"。身份基线有意包含已停用用户（停用期间日报仍要
更新资料），因此授权路径落库前另有一道账号状态复检，撤权路径不设——服务对象本来就是刚被停用的人。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Protocol

from lingxi.core.identity.roster_audit import ArchivedIdentity
from lingxi.core.permission.decision_chain import (
    AllScopeBackfill,
    AllScopeExpander,
    LocalOverrideDecisionSource,
    LocalOverrideReader,
)
from lingxi.core.permission.metric_translation import metric_translation_available
from lingxi.core.permission.user_decision_tree import (
    DecisionBranch,
    DecisionPorts,
    DecisionStore,
    PublishHistory,
    UserDecision,
    UserPermissionDecisionTree,
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
#: 本地「全部」组下某公司被本地抑制减到空：读侧回退制无法表示，本次既不
#: 发布也不撤权（`merge_sources.py` 「本地 "*" 组」一节）。
SKIP_SUPPRESSION_UNREPRESENTABLE = "suppression_on_all_scope_unrepresentable"
#: 落授权决定的那把行锁里发现这个人不是 ``enabled``：本模块的
#: 身份基线**有意包含** ``suspended``，因此这条跳过是常态出口而不是异常——管理员对
#: 一个已停用用户做本地权限动作时就会走到这里。与 :data:`SKIP_USER_NOT_ACTIVE`
#: 刻意分开登记：那一条说的是"这个人不在花名册基线里"（删除中/已删除/未开通完成），
#: 这一条说的是"人在基线里，但账号状态不允许给他排非空授权"。
SKIP_ACCOUNT_NOT_ENABLED = "account_not_enabled"

#: 决策树里只跳过的出口在本入口的原因码。匹配失败与两条撤权出口另带事实字段，单独翻。
_SKIP_REASONS: Mapping[DecisionBranch, str] = {
    DecisionBranch.MISSING_PERSONNEL_ID: SKIP_MISSING_PERSONNEL_ID,
    DecisionBranch.ARCHIVE_INCOMPLETE: SKIP_ARCHIVED_IDENTITY_INCOMPLETE,
    DecisionBranch.TRANSLATION_UNCOVERED: SKIP_METRIC_TRANSLATION_UNCOVERED,
    DecisionBranch.LOCAL_OVERRIDE_READ_FAILED: SKIP_LOCAL_OVERRIDE_READ_FAILED,
    DecisionBranch.SUPPRESSION_UNREPRESENTABLE: SKIP_SUPPRESSION_UNREPRESENTABLE,
}


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


class AuditSink(Protocol):
    """审计出口。"""

    def record(self, action: str, /, **fields: object) -> None:
        """记一条审计事件。"""
        ...


class TargetedPermissionRecompute:
    """管理员动作确认执行成功后，对**一个**用户的即时重算/发布/撤权。

    只编排：判定规则全部在共用的决策树与它调用的 ``core/permission/*`` 纯函数里
    （模块文档），本类一条业务规则都不重新定义。真实装配见
    ``adapters/postgres_permission_recompute_trigger.py``。
    """

    def __init__(
        self,
        *,
        identities: _IdentityLookup,
        roster_snapshot: _RosterRows,
        galaxy: _GalaxySnapshot,
        decisions: DecisionStore,
        publish_history: PublishHistory,
        role_function_map: Mapping[str, str],
        metric_translation_map: Mapping[str, Mapping[str, Sequence[str]]],
        audit: AuditSink,
        local_overrides: LocalOverrideReader | None = None,
        clock: Callable[[], datetime] | None = None,
        legacy_all_scope: AllScopeExpander | None = None,
        revocation_identities: _RevocationIdentityLookup | None = None,
    ) -> None:
        """接线身份/花名册/银河/决定存储/发布历史/审计等协作者与可选覆盖项。"""
        self._identities = identities
        # ``None`` = 装配层没接撤权专用查找口：退回 ``identities.find_active``
        # （真实装配一定要接，见 ``adapters/postgres_permission_recompute_trigger.py``）。
        self._revocation_identities = revocation_identities
        self._roster_snapshot = roster_snapshot
        self._galaxy = galaxy
        self._metric_translation_map = metric_translation_map
        self._audit = audit
        self._clock = clock or (lambda: datetime.now(UTC))
        local_override_source = LocalOverrideDecisionSource(
            reader=local_overrides,
            on_read_failed=self._audit_local_override_read_failure,
            metric_translation_map=metric_translation_map,
            clock=self._clock,
            backfill=(
                None
                if legacy_all_scope is None
                else AllScopeBackfill(
                    expander=legacy_all_scope,
                    on_failed=self._audit_all_scope_refresh_failure,
                    on_succeeded=self._audit_all_scope_refreshed,
                )
            ),
        )
        self._tree = UserPermissionDecisionTree(
            ports=DecisionPorts(
                decisions=decisions, publish_history=publish_history, token_ciphers=None
            ),
            role_function_map=role_function_map,
            metric_translation_map=metric_translation_map,
            local_override_source=local_override_source,
            publish_reason=ADMIN_TARGETED_RECOMPUTE_REASON,
            revoke_reason=ADMIN_TARGETED_REVOKE_REASON,
            on_local_override_skipped=self._audit_local_override_skipped,
            on_publish_without_cipher=self._audit_publish_needs_cipher,
        )

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
        decision = self._tree.revoke(identity, now=self._clock())
        return self._settle(user_id, decision, cause="admin_suspend")

    def _find_revocation_identity(self, user_id: str) -> ArchivedIdentity | None:
        """撤权侧的身份查找：接了专用口就用它，没接退回授权侧那份基线。"""
        if self._revocation_identities is not None:
            return self._revocation_identities.find_for_revocation(user_id=user_id)
        return self._identities.find_active(user_id=user_id)

    # ------------------------------------------------------------------
    # 恢复 / 本地权限三类动作：完整合并管线
    # ------------------------------------------------------------------

    def recompute_and_publish(self, *, user_id: str) -> TargetedRecomputeOutcome:
        """恢复/本地权限动作触发的完整合并管线：银河 ∪ 本地授权 − 本地抑制。

        入口前置只定位身份、花名册快照与银河批次，并与每日批同一条纪律：翻译层整体
        不可用时授权与撤权都不排；任何一项缺失直接给出跳过结论。其余判定全在决策树。
        """
        identity = self._identities.find_active(user_id=user_id)
        if identity is None:
            return self._skip(user_id, mode="recompute", reason=SKIP_USER_NOT_ACTIVE)
        roster_rows = self._roster_snapshot.load_rows()
        if roster_rows is None:
            return self._skip(user_id, mode="recompute", reason=SKIP_MISSING_ROSTER_SNAPSHOT)
        galaxy = self._galaxy.load_current()
        if galaxy is None:
            return self._skip(user_id, mode="recompute", reason=SKIP_NO_GALAXY_BATCH)
        if not metric_translation_available(self._metric_translation_map):
            return self._skip(user_id, mode="recompute", reason=SKIP_METRIC_TRANSLATION_UNAVAILABLE)
        decision = self._tree.decide(identity, roster_rows, galaxy, now=self._clock())
        return self._settle(user_id, decision, cause=None)

    # ------------------------------------------------------------------
    # 收尾：把决策树的出口翻成本入口的结论与审计
    # ------------------------------------------------------------------

    def _settle(
        self, user_id: str, decision: UserDecision, *, cause: str | None
    ) -> TargetedRecomputeOutcome:
        """``cause`` 只在停用路径给定；合并管线上的撤权原因取自决策树的事实。"""
        branch = decision.branch
        if branch is DecisionBranch.PUBLISHED:
            kind = RecomputeKind.ENQUEUED if decision.enqueued else RecomputeKind.UNCHANGED
            return self._completed(user_id, decision, mode="recompute", kind=kind)
        if branch is DecisionBranch.REVOKED:
            kind = RecomputeKind.REVOKED if decision.enqueued else RecomputeKind.UNCHANGED
            cause = cause or decision.zero_galaxy_reason or "fully_suppressed"
            return self._completed(user_id, decision, mode="revoke", kind=kind, cause=cause)
        if branch is DecisionBranch.REVOKE_WITHOUT_FOOTPRINT:
            if cause is not None:
                return self._skip(user_id, mode="revoke", reason=SKIP_NO_PUBLISHED_ROW)
            return self._skip(
                user_id,
                mode="recompute",
                reason=SKIP_NO_PUBLISHED_ROW,
                cause=decision.zero_galaxy_reason or "fully_suppressed",
            )
        if branch is DecisionBranch.GRANT_BLOCKED:
            # 常态出口，不是故障：事务整体回滚，这个人的发布内容一个字节都没变。
            return self._skip(
                user_id,
                mode="recompute",
                reason=SKIP_ACCOUNT_NOT_ENABLED,
                account_state=decision.account_state,
            )
        if branch is DecisionBranch.MATCH_FAILED:
            # 匹配失败＝"认不出这个人"，与每日批同一姿态：不做任何撤权/发布写入，
            # 保留发布表现状，交给下一轮每日批。
            return self._skip(
                user_id,
                mode="recompute",
                reason=SKIP_MATCH_FAILED,
                match_reason=decision.match_reason,
            )
        return self._skip(user_id, mode="recompute", reason=_SKIP_REASONS[branch])

    def _completed(
        self,
        user_id: str,
        decision: UserDecision,
        *,
        mode: str,
        kind: RecomputeKind,
        cause: str | None = None,
    ) -> TargetedRecomputeOutcome:
        facts: dict[str, object] = {} if cause is None else {"cause": cause}
        self._audit.record(
            "permission_targeted_recompute.completed",
            user=user_id,
            mode=mode,
            kind=kind.value,
            **facts,
            cleared=decision.cleared_events,
        )
        return TargetedRecomputeOutcome(kind=kind, cleared_events=decision.cleared_events)

    def _audit_publish_needs_cipher(self, user_id: str) -> None:
        """这个人在发布链上没有足迹、而本入口不接密文读取口：在这里就让这个角落可分辨。

        即将结算的发布行没有密文，``record_decision`` 不会因此失败（它只把这份快照
        原样记成 ENQUEUED），真正的失败关闭发生在之后独立一轮的发布执行器，运维不会
        自然地把两处联系起来。
        """
        self._audit.record("permission_targeted_recompute.publish_needs_cipher", user=user_id)

    def _audit_local_override_skipped(self, user_id: str, reason: str) -> None:
        """通配用户场景：本地覆盖整体不参与合并——审计明确说明这次调用为什么没有预期变化。"""
        self._audit.record(
            "permission_targeted_recompute.local_override_skipped",
            user=user_id,
            reason=reason,
        )

    def _audit_local_override_read_failure(self, user_id: str, _error: Exception) -> None:
        """读不出来：响亮记一条本入口自己的审计，由调用方决定怎么收敛。

        与另外两条链共用判据、各留各的事件名——运维从审计一眼看出是哪条链读失败的。
        """
        self._audit.record(
            "permission_targeted_recompute.local_override_skipped",
            user=user_id,
            reason=SKIP_LOCAL_OVERRIDE_READ_FAILED,
        )

    def _audit_all_scope_refresh_failure(self, user_id: str, error: Exception) -> None:
        """「全部」组补行失败：只留痕，本次按既有行照常算。"""
        self._audit.record(
            "permission_targeted_recompute.legacy_all_scope_refresh_failed",
            user=user_id,
            error=type(error).__name__,
        )

    def _audit_all_scope_refreshed(self, user_id: str, added: int) -> None:
        """「全部」组补行成功：留下实际新增的行数。"""
        self._audit.record(
            "permission_targeted_recompute.legacy_all_scope_refreshed",
            user=user_id,
            added=added,
        )

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
