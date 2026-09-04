"""管理员写动作确认执行成功后，对单个用户的定向权限重算 + 发布（Issue #438）。

## 为什么不是「调用每日批」，而是新写一个单用户入口

``apps/scheduler/permission_refresh.py`` 的 ``PermissionRefreshDuty`` 是**整轮**
职责：花名册新鲜度、银河批次存在性两条前置判据是**轮级**门槛（挡在遍历所有用户
之前），且它按用户 ID 遍历 ``load_active_baseline()`` 的全量结果，没有「只算一个人」
的公开入口——把它硬套到单用户场景要么整轮跑一遍（代价与"定向"二字矛盾，且会绕开
"每日批保底不动"的要求），要么破坏它的封装去调用下划线私有方法。

本模块反过来做：**复用**同一组 ``core/permission/*`` 纯函数（``match_galaxy_account``、
``aggregate_permission``、``translate_company_functions``、``merge_permission_sources``、
``build_translated_publish_row``、``build_revocation_row``——与 ``permission_refresh.py``
的 ``_refresh_user``/``_refresh_zero_galaxy_user`` 及 ``onboarding_runner.py`` 的
``_publish`` 是同一套building blocks，不是第二份实现），重新编排成一个只处理
**一个身份**的入口，不touch每日批一行代码，也不新建第二套判定规则。

## 与每日批的三处刻意不同，都写在这里而不是留给调用方猜

1. **不做「花名册今天更新过」的轮级顺序判据**（`V-权限-07` 只约束**每日批**自己
   连续两步「先刷花名册、再刷银河」的顺序，不约束一次由管理员动作触发的单点
   重算）。本模块只要求快照**存在**，不要求它是今天的——管理员点确认卡时，"用
   当前已知的最新花名册/银河数据重算这一个人"已经是可获得的最快答案，比"因为
   不是今天的快照就交给最多 24 小时后的下一轮"更贴近"即时生效"的产品意图。
   代价：如果快照本身就滞后（例如这个人已经离职但花名册还没刷新），定向重算
   可能用旧数据算出与预期不符的结果——但这与"什么都不做、原样等每日批"相比不
   更差：发布表此刻的内容本来就是上一次某一轮用同样滞后数据算出来的。
2. **存量沿用（legacy source）机制已退役**（PM 2026-08-30 裁定，Issue #441）：
   ``merge_permission_sources`` 不再有 ``legacy`` 参数，本模块与每日批同用
   「银河 ∪ 本地授权 − 本地抑制」两源合并，无需任何多维表格凭据——原设计中
   「gateway 不为存量沿用额外持有多维表格凭据」的边界诉求自然满足。
3. **``token_cipher`` 固定传 ``None``**。新建发布行才需要密文，而触发本模块的
   五种管理员动作里，除了极端边界（一个从未发布过的人被停用又恢复）不会新建行
   ——``RESUME_USER``/``LOCAL_PERMISSION_*`` 只会发生在这个人已经存在开通历史
   之后，绝大多数情况下发布表已有这一行，走的是"更新既有行"路径（``token_cipher``
   一律不碰，见 ``core/permission/publish_row.py`` 模块文档），不需要读取密文；
   真的撞上"要新建行但没有密文"这一角时——**失败不是发生在 ``record_decision``**
   （文档曾经这样写，与实现不符，Trace #445 opus 审查坐实）：``record_decision``
   只把这份不带密文的快照原样记成 ``ENQUEUED`` 写进 outbox（见
   ``PublishRow.snapshot_fields`` 在 ``token_cipher`` 为空时退回六字段更新集，
   不做任何校验），真正抛出"新建发布行必须携带 token_cipher"这条 ``ValueError``
   并归类为 ``missing_token_cipher`` 失败关闭的是**发布执行器**
   （``core/permission/publish.py``，随每日刷新/开通链之后独立消费 outbox 的
   那一层，见其模块文档「``token_cipher``」一节）——本模块与那个执行器之间隔着
   一次 outbox 落库与之后独立的一轮消费，异常不是"原样冒泡"给本模块的调用方，
   而是延迟到发布执行器那一轮才会被观察到。``_settle_publish`` 因此在撞上这一角
   时额外记一条本模块自己的审计（见该方法文档），不再依赖"异常会冒泡"这个不成立
   的假设去暴露这个角落。绝不为了凑齐这一角去 gateway 里接入 MCP 令牌加密主密钥
   （那是一把只应该活在 scheduler 进程里的密钥，见 ``apps/scheduler/
   permission_refresh.py::_build_permission_refresh_duty`` 的同一条纪律）。

## 停用要能撤掉"开通到一半"那条在途意图（Trace #544 P-6）

``force_revoke`` 的身份查找**不走**授权侧那份 ``provisioning_state = 'active'`` 基线，
走独立的 :class:`_RevocationIdentityLookup`（只排除已删除账号）。理由见该方法文档：
用授权侧基线时，"首聊开通到一半被停用"会被判成"这个人不在基线里"直接跳过，已经入队的
``first_onboarding`` 意图照样发到正式表——停用没有生效，而用户仍然用不了（令牌只在
scheduler 侧）。两个口刻意不合并：放宽授权侧基线会让开通中的用户被算出非空权限发出去。

## 已停用用户不会被本模块重新发权（Issue #483 缺口②）

本模块的身份查找口（``_IdentityLookup``）真实实现取的是**花名册审计基线**
（``adapters/postgres_roster_audit.ACTIVE_BASELINE_SQL``），那份基线**有意包含
``suspended``**（停用期间姓名/邮箱/工号漂移仍要进日报，见 Issue #468 留痕），因此
``recompute_and_publish`` 首行的 ``SKIP_USER_NOT_ACTIVE`` 兜底对停用用户**不会触发**，
而它自己全程也不读 ``account_state``。结果是：管理员对一个已停用用户执行「授予 /
抑制 / 收回本地权限」，确认成功后触发的这条重算，会把这个人的真实合并权限重新排出去
——**不需要任何竞态，确定性可复现**。

修法不是在本模块再抄一份账号状态判据（那会变成第二个真相来源，且仍然与停用写入不在
同一把锁里），而是复用落权限决定那把**已经持有的 ``app_user`` 行锁**：
:meth:`_settle_publish` 声明 ``require_enabled_account=True``，非 ``enabled`` 一律
不排非空授权，本模块只负责把被挡翻译成可分辨的 :data:`SKIP_ACCOUNT_NOT_ENABLED`
跳过码。:meth:`_settle_revocation` 相反，声明 ``False``——撤权的服务对象本来就是已停用
用户，挡住它等于让停用彻底失效。

## 为什么 ``force_revoke`` 与 ``recompute_and_publish`` 是两个方法，不是一个

停用（``SUSPEND_USER``）需要的是「不管银河怎么说，立刻清空这个人的发布内容」——
如果走 ``recompute_and_publish`` 的正常合并管线，一个银河仍然判定"有效授权"的
用户会被重新算出非空权限、原样发布回去，等于**撤销了管理员刚做的停用决定**
（银河根本不知道 Lingxi 自己的停用状态）。因此 ``force_revoke`` 是一条独立、更
短的路径：只需要身份的邮箱/姓名与发布链足迹判据，完全不碰花名册快照、银河批次
与职能翻译——这也让它在当前部署（花名册快照持续为空）下依然可靠：停用是本卡
最安全攸关的方向，它的即时生效不应该依赖一整条尚未配置齐的重算管线。

恢复（``RESUME_USER``）与三种本地权限动作则相反：产品语义是"恢复成这个人*现在*
应得的权限"，答案必须来自完整的合并管线（银河 ∪ 本地授权 − 本地抑制），因此走
``recompute_and_publish``。
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
    UncoveredPermissionCombination,
    metric_translation_available,
    translate_company_functions,
)
from lingxi.core.permission.publish import PermissionGrantBlockedByAccountState
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
#: 落授权决定的那把行锁里发现这个人不是 ``enabled``（Issue #483 缺口②）：本模块的
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
    """**撤权专用**的身份查找口（Trace #544 P-6）。

    与 :class:`_IdentityLookup` 分开，是因为两条路径要的判据本来就不同：授权侧只
    服务"已经开通完成"的人（``provisioning_state = 'active'``），撤权侧要服务的是
    **任何还可能有一条发布内容在外面的人**——包括开通到一半就被停用的那个人。
    合并成一个口会逼两条路径共用同一个基线，而放宽那个基线会让开通中的用户被授权
    管线算出非空权限发布出去，正是这条 Protocol 要避免的事。
    """

    def find_for_revocation(self, *, user_id: str) -> ArchivedIdentity | None: ...


class _RosterRows(Protocol):
    """花名册持久快照的行，供匹配用。``None`` 表示快照尚不存在（部署事实，
    不判断新鲜度——见模块文档「三处刻意不同」第 1 条）。
    """

    def load_rows(self) -> Sequence[Mapping[str, Any]] | None: ...


class _GalaxySnapshot(Protocol):
    def load_current(self) -> Any: ...


class _PublishHistory(Protocol):
    def has_publish_footprint(self, user_id: str) -> bool: ...


class _Decision(Protocol):
    enqueued: bool
    cleared_events: int


class _DecisionStore(Protocol):
    """``require_enabled_account`` 是**必填**关键字参数（Issue #483）：授权侧传
    ``True``、撤权侧传 ``False``，账号状态复检落在实现那把已经持有的 ``app_user``
    行锁里（模块文档「已停用用户不会被本模块重新发权」一节）。"""

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
    """「2.0 迁移导入·全部」组的补行口（rc25 S-1 方案 E），与
    ``apps/scheduler/permission_refresh.py`` 的同名协议各自独立一份。"""

    def expand_all_scope_group(
        self, *, user_id: str, group_id: str, metrics: Sequence[str], now: datetime
    ) -> int: ...


class AuditSink(Protocol):
    def record(self, action: str, /, **fields: object) -> None: ...


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
        self._identities = identities
        # ``None`` = 装配层没接撤权专用查找口：退回 ``identities.find_active``，行为
        # 与 Trace #544 P-6 之前逐字相同（真实装配一定要接，见
        # ``adapters/postgres_permission_recompute_trigger.py``）。
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
        """停用触发的即时撤权：把这个人的发布内容清空，并让任何**在途**的发布意图失效。

        **身份查找走撤权专用口**（Trace #544 P-6）：授权侧那份基线只收
        ``provisioning_state = 'active'`` 的人，于是"首聊开通到一半就被停用"这一种
        真实情形会在这里被判成 :data:`SKIP_USER_NOT_ACTIVE` 直接跳过——已经入队的
        ``first_onboarding`` 发布意图因此照样发到正式表上，用户被停用了却仍然在权限
        表里有一行；而他的问数令牌只在 scheduler 侧，本人还是用不了，这一行既没用又
        越权，要等到下一次停用/恢复才会被清掉。撤权口不看开通进度，只排除已经删除的
        账号：停用是本卡最安全攸关的方向，它不该依赖"这个人开通完了没有"。

        撤权决定本身会推进 ``app_user.permission_version``，比它旧的在途意图在认领时
        直接判 ``superseded``、一次外部调用都不发（见 ``core/permission/publish.py``
        「并发边界」一节）——"撤掉在途意图"复用的就是这条既有机制，不新造第二套。
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

    def recompute_and_publish(self, *, user_id: str) -> TargetedRecomputeOutcome:
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
            # 与每日批同一条纪律：翻译层整体不可用时，授权与撤权都不排（模块文档
            # 「三处刻意不同」不含这一条——这一条与每日批完全一致，不是本模块的
            # 简化）。
            return self._skip(user_id, mode="recompute", reason=SKIP_METRIC_TRANSLATION_UNAVAILABLE)

        match = match_galaxy_account(identity.personnel_id, roster_rows, galaxy.user_rows)
        if match.state != MATCHED or not match.galaxy_user_id:
            # 匹配失败＝"认不出这个人"，与每日批同一姿态：不做任何撤权/发布写入，
            # 保留发布表现状，交给下一轮每日批（那时花名册/银河数据可能已经更新）。
            return self._skip(
                user_id, mode="recompute", reason=SKIP_MATCH_FAILED, match_reason=match.reason
            )

        if not identity.email or not identity.display_name:
            return self._skip(user_id, mode="recompute", reason=SKIP_ARCHIVED_IDENTITY_INCOMPLETE)

        aggregate = aggregate_permission(
            galaxy_user_id=match.galaxy_user_id,
            user_role_rows=galaxy.role_rows(match.galaxy_user_id),
            datacountry_rows=galaxy.datacountry_rows(match.galaxy_user_id),
            country_rows=galaxy.country_rows,
            role_function_map=self._role_function_map,
        )

        if aggregate.granted:
            try:
                company_metrics = translate_company_functions(
                    companies=aggregate.companies,
                    functions=aggregate.functions,
                    all_companies=aggregate.all_companies,
                    mapping=self._metric_translation_map,
                )
            except UncoveredPermissionCombination:
                return self._skip(
                    user_id, mode="recompute", reason=SKIP_METRIC_TRANSLATION_UNCOVERED
                )
            cause = None
        else:
            # 零银河权限：银河对合并的贡献恒为空字典，不调用翻译（与
            # permission_refresh.py::_refresh_zero_galaxy_user 同一姿态）。
            company_metrics = {}
            cause = aggregate.reason

        local = self._resolve_local_overrides(user_id)
        # 通配角 v2（Issue #440，本模块 #445 修复前漏接）：`all_companies=True`
        # 有两个互相独立的成因（`scope.all_countries` 或持有
        # `ADMIN_FULL_ACCESS_FUNCTION`），只有后者是「真全指标通配」——
        # `merge_permission_sources` 自己不猜测，调用方必须显式声明（同
        # `permission_refresh.py::_refresh_user`/`onboarding_runner.py::
        # AutoOnboardingRunner._publish` 的同型判据，见该函数「通配角 v2」文档）。
        # 零银河分支（`aggregate.granted` 为假）`aggregate.functions` 恒为空
        # 元组，`in` 判据天然为 False，参数在 `galaxy={}` 时本就无作用面。
        merged = merge_permission_sources(
            galaxy=company_metrics,
            local=local,
            full_access_wildcard=ADMIN_FULL_ACCESS_FUNCTION in aggregate.functions,
        )
        for reason in merged.skipped_reasons:
            # 通配用户（银河「后台管理员」，all_companies=True）场景：本地覆盖整体
            # 不参与合并——审计明确说明这次调用为什么没有产生预期变化（Issue #438
            # 「通配用户等跳过场景」）。
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
            # 模块文档「三处刻意不同」第 3 条角落：这个人此刻在发布链上没有留下
            # 任何足迹（从未发布成功，也没有任何在途意图）——即将调用的
            # ``record_decision`` 不会因此失败（它只把这份没有 ``token_cipher``
            # 的快照原样记成 ``ENQUEUED``），真正的失败关闭发生在之后独立一轮的
            # 发布执行器（``core/permission/publish.py``，见模块文档同一角落的
            # 更正）——那一层的 ``missing_token_cipher`` 审计与本模块的审计流
            # 不是同一处，运维不会自然地把两者联系起来。这条审计让这个角落在
            # **这里**就可分辨：确认卡回执说"已确认执行"的同时，运维已经能看到
            # "这次即将成为一次新建行尝试、而本模块从不持有可以新建行的密文"。
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
                # Issue #483 缺口②：本模块的身份基线有意包含 ``suspended``，这里是
                # 唯一挡住"给已停用用户重新发权"的地方，判据在实现的行锁里。
                require_enabled_account=True,
                decided_at=now,
                clear_delivered_content=True,
            )
        except PermissionGrantBlockedByAccountState as blocked:
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
            # Issue #483：撤权任何账号状态都必须放行——``force_revoke`` 的服务对象
            # 本来就是刚被停用的人，挡住它等于让停用彻底失效。
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
        """读取失败只降级为"没有本地源"（``None`` 对合并恒等），响亮记一条审计、
        不整次调用失败——与 ``permission_refresh.py`` 默认（非 ``raise_on_failure``）
        姿态一致：本模块两条业务路径的银河/既有发布内容都已经确定要不要发布，本地
        源读取失败不改变这件事本身，只是让合并少了本地这一份。
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
        """「2.0 迁移导入·全部」组随当前映射补齐新指标（rc25 S-1 方案 E），与每日批
        ``permission_refresh._expand_legacy_all_scope`` 同一语义：缺才补、同组 ID、只看
        生效条目；补行或重读失败只审计、不影响本次既有结果。"""

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
