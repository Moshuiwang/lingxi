"""每日权限重算职责：把银河与本地授权的当前结论排成发布意图。

一轮的次序不可调换：花名册新鲜度判据 → 银河当前批次 → 翻译层整体可用性判据 → 逐个
已开通且未停用的用户重算 → 一条只含计数的职责报告。前三条任一不过就整轮不跑，只留一条
可分辨的审计——**不触银河、一条发布意图都不排，撤权也不例外**。

合同要求每日刷新**严格先刷新花名册、再刷新银河快照**（`V-权限-07`）。「先」如果只靠
职责在列表里的位置来保证，花名册那一轮失败或压根没注册时权限重算照样会跑——用的是几天前
的花名册。因此这里把顺序变成一条**数据判据**：只有库里那份花名册快照是今天取的才允许
重算，且**不提供任何旁路开关**——一个"允许用旧花名册重算"的变量会在第一次运维着急时被
打开，然后再也不会被关上。

**本职责一次都不签发令牌，也不通知任何人**：需要新建发布行时只取该用户已经登记的密文，
取不到就交给发布执行器失败关闭；通知属另一个职责，这里连一个发送端口都没有。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

from lingxi.adapters.postgres_local_permission import (
    PostgresLocalPermissionOverrideStore,
    local_override_reader,
)
from lingxi.apps.scheduler.audit import AuditSink
from lingxi.apps.scheduler.config import SchedulerConfig
from lingxi.apps.scheduler.permission_refresh_ports import (
    _ROUND_SKIP_ACTIONS,
    PERMISSION_REFRESH_REASON,
    PERMISSION_REVOKE_REASON,
    REASON_FULLY_SUPPRESSED,
    SKIP_ACCOUNT_NOT_ENABLED,
    SKIP_ARCHIVED_IDENTITY_INCOMPLETE,
    SKIP_LOCAL_OVERRIDE_READ_FAILED,
    SKIP_METRIC_TRANSLATION_UNAVAILABLE,
    SKIP_METRIC_TRANSLATION_UNCOVERED,
    SKIP_MISSING_PERSONNEL_ID,
    SKIP_MISSING_SNAPSHOT,
    SKIP_NO_GALAXY_BATCH,
    SKIP_NO_PUBLISHED_ROW,
    SKIP_STALE_SNAPSHOT,
    SKIP_SUPPRESSION_UNREPRESENTABLE,
    STAGE_AGGREGATE,
    STAGE_IDENTITY,
    STAGE_MATCH,
    STAGE_TRANSLATE,
    TRIGGER_GRANT,
    TRIGGER_REVOKE,
    PermissionRefreshReport,
    PermissionRefreshSources,
    _AuditSink,
    _BaselineReader,
    _DecisionStore,
    _GalaxySnapshotReader,
    _LegacyAllScopeExpander,
    _LocalOverrideReader,
    _PublishHistory,
    _RosterSnapshotStore,
    _Tally,
    _TokenCipherReader,
    _utc_date,
)
from lingxi.core.identity.roster_audit import ArchivedIdentity
from lingxi.core.permission.decision_chain import (
    AllScopeBackfill,
    LocalOverrideDecisionSource,
)
from lingxi.core.permission.merge_sources import REASON_LOCAL_OVERRIDE_READ_FAILED
from lingxi.core.permission.metric_translation import metric_translation_available
from lingxi.core.permission.user_decision_tree import (
    DecisionBranch,
    DecisionPorts,
    UserDecision,
    UserPermissionDecisionTree,
)

logger = logging.getLogger(__name__)

_UTC = UTC

#: 决策树四条只跳过的出口在本职责里落到的阶段与原因码；匹配失败、翻译未覆盖与零银河
#: 存档不全三条要按事实取值，不在这张表里。
_SKIP_STAGES: Mapping[DecisionBranch, tuple[str, str]] = {
    DecisionBranch.MISSING_PERSONNEL_ID: (STAGE_IDENTITY, SKIP_MISSING_PERSONNEL_ID),
    DecisionBranch.ARCHIVE_INCOMPLETE: (STAGE_IDENTITY, SKIP_ARCHIVED_IDENTITY_INCOMPLETE),
    DecisionBranch.LOCAL_OVERRIDE_READ_FAILED: (STAGE_AGGREGATE, SKIP_LOCAL_OVERRIDE_READ_FAILED),
    DecisionBranch.SUPPRESSION_UNREPRESENTABLE: (
        STAGE_AGGREGATE,
        SKIP_SUPPRESSION_UNREPRESENTABLE,
    ),
}


class PermissionRefreshDuty:
    """每日权限重算：花名册新鲜 → 银河当前批次 → 逐个已开通用户重算并排发布意图。

    语义与边界见模块文档。本类**只编排整轮**：三条整轮前置判据在这里，逐用户的决策树
    （匹配 → 聚合 → 翻译 → 合并 → 发布或撤权）在
    :mod:`lingxi.core.permission.user_decision_tree`，与管理员即时动作共用同一份；
    版本推进与幂等在 :mod:`lingxi.adapters.postgres_permission_publish`。这里只把
    决策树的出口翻成本职责的计数与审计事件，一条判定规则都不复制。
    """

    name = "每日权限重算"

    def __init__(
        self,
        *,
        sources: PermissionRefreshSources,
        role_function_map: Mapping[str, str],
        metric_translation_map: Mapping[str, Mapping[str, Sequence[str]]],
        audit: _AuditSink,
        clock: Callable[[], datetime] | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        """装配一轮重算需要的读写端口、两份映射与时钟。"""
        self._baseline_reader = sources.baseline_reader
        self._roster_snapshot = sources.roster_snapshot
        self._galaxy = sources.galaxy
        self._metric_translation_map = metric_translation_map
        self._audit = audit
        # 时钟注入：跨轮判重与"今天"的用例要能自己决定日期，不能靠等到明天。
        self._clock = clock or (lambda: datetime.now(_UTC))
        local_override_source = LocalOverrideDecisionSource(
            reader=sources.local_overrides,
            on_read_failed=self._audit_local_override_read_failure,
            metric_translation_map=metric_translation_map,
            clock=self._clock,
            backfill=(
                None
                if sources.legacy_all_scope is None
                else AllScopeBackfill(
                    expander=sources.legacy_all_scope,
                    on_failed=self._audit_all_scope_refresh_failure,
                    on_succeeded=self._audit_all_scope_refreshed,
                )
            ),
        )
        # 逐用户决策树与管理员即时动作共用一份；本职责的三处显式参数：花名册行与银河
        # 快照在整轮前置通过「今天更新过」判据后传入、本地覆盖来源含「全部」组补行、
        # 令牌密文只读既有（读取口在这里接上，即时动作那一侧没有）。
        self._tree = UserPermissionDecisionTree(
            ports=DecisionPorts(
                decisions=sources.decisions,
                publish_history=sources.publish_history,
                token_ciphers=sources.token_ciphers,
            ),
            role_function_map=role_function_map,
            metric_translation_map=metric_translation_map,
            local_override_source=local_override_source,
            publish_reason=PERMISSION_REFRESH_REASON,
            revoke_reason=PERMISSION_REVOKE_REASON,
            on_local_override_skipped=self._audit_local_override_skipped,
        )
        # 与同一进程内的其他职责共享停止标志：一次信号让所有职责停止领取新工作。
        self._stop = threading.Event() if stop is None else stop
        self._completed_on: date | None = None
        # 跳过类审计的**当日去重水位**：当天已经记过哪些原因。顺序判据在某些部署下每轮
        # 都不成立，而调度周期是一分钟——不去重的话一天会刷出上千条内容完全相同的审计，
        # 真正的信号会被埋掉。存的是**原因集合**而不是"最后一个原因"：同一天里原因会
        # 来回变，只记最后一个的话 A→B→A 会把 A 记两次，去重就在最需要它的路径上失效。
        self._skip_audited: tuple[date, set[str]] | None = None

    @property
    def stopping(self) -> bool:
        """是否已经收到停止信号。"""
        return self._stop.is_set()

    @property
    def completed_on(self) -> date | None:
        """已完成重算的那一天。``None`` 表示本进程实例今天还没跑完过。"""
        return self._completed_on

    def request_stop(self) -> None:
        """请求停止：本轮不再领取新的用户。"""
        self._stop.set()

    # ------------------------------------------------------------------
    # 一轮
    # ------------------------------------------------------------------

    def run_once(self) -> PermissionRefreshReport | None:
        """跑一轮。

        Returns:
            这一轮的报告；停止中、今天已跑完、或任一前置判据不成立时返回 ``None``。
        """
        if self._stop.is_set():
            # 已经在停止中：一轮都不开，一条发布意图都不排。
            return None
        now = self._clock()
        today = _utc_date(now)
        if self._completed_on == today:
            return None
        inputs = self._load_round_inputs(today)
        if inputs is None:
            return None
        snapshot, galaxy = inputs
        tally, interrupted = self._refresh_all(snapshot, galaxy, now, today)
        return self._finish_round(tally, galaxy, today, interrupted=interrupted)

    def _load_round_inputs(self, today: date) -> tuple[Any, Any] | None:
        """三条整轮前置判据；任一不过就整轮不跑，只留一条可分辨的审计。

        花名册的元信息与整份快照分两条语句读，中间可能有一次并发替换（花名册审计职责就在同一
        进程里）。读到「元信息说有、整份却没有」时唯一安全的动作是本轮不跑，下一轮那份新快照会
        自己把日期判据带过来。

        **顺序判据成立之后才碰银河**：花名册不新鲜的那一轮一次银河读取都不发起。任一判据不过时
        返回 ``None``。
        """
        facts = self._roster_snapshot.load_facts()
        if facts is None:
            self._audit_skip(today, SKIP_MISSING_SNAPSHOT)
            return None
        if _utc_date(facts.captured_at) != today:
            self._audit_skip(
                today, SKIP_STALE_SNAPSHOT, snapshot_date=_utc_date(facts.captured_at).isoformat()
            )
            return None
        snapshot = self._roster_snapshot.load()
        if snapshot is None:
            self._audit_skip(today, SKIP_MISSING_SNAPSHOT)
            return None
        if _utc_date(snapshot.facts.captured_at) != today:
            self._audit_skip(
                today,
                SKIP_STALE_SNAPSHOT,
                snapshot_date=_utc_date(snapshot.facts.captured_at).isoformat(),
            )
            return None
        galaxy = self._galaxy.load_current()
        if galaxy is None:
            self._audit_skip(today, SKIP_NO_GALAXY_BATCH)
            return None
        if not self._translation_available(today):
            return None
        return snapshot, galaxy

    def _translation_available(self, today: date) -> bool:
        """翻译映射整体为空时，**整轮**一条发布意图都不排——撤权也不例外。

        撤权从不调用翻译（它写的是不含指标名的空对象），因此把这条判据放在逐用户层面挡不住
        撤权；唯一挡得住的位置是**遍历开始之前**：判据是"翻译层这一轮可不可用"，不是"这一行
        要不要翻译"。映射为空时若只挡授权、放行撤权，权限在内容到位之前只能单向减少、不能
        恢复——这是最危险的那种不对称。

        判据实现是**唯一允许存在的那一份**：首次开通的发布闸对同一个已加载对象调用同一个
        函数，两个独立写入点因此不会漂移出两套看起来等价的检查。
        """
        if metric_translation_available(self._metric_translation_map):
            return True
        self._audit_skip(today, SKIP_METRIC_TRANSLATION_UNAVAILABLE)
        return False

    def _refresh_all(
        self, snapshot: Any, galaxy: Any, now: datetime, today: date
    ) -> tuple[_Tally, bool]:
        """遍历这一轮的全部已开通用户；单个用户的失败不得带走整轮。

        计数在**领取时**递增，不在遍历前按基线行数一次性写死：被停止信号挡在外面的人从来
        没有被看过一眼，把他们算进"已检查"会让中断轮的报告读起来像是"全都查过了、只是什么
        都没做"。停止信号落在遍历中间时不再为后面的人排新意图；已经落库的决定各自是一个
        完整事务，不存在半态。

        Returns:
            ``(计数器, 是否被停止信号中断)``。
        """
        del today
        tally = _Tally()
        for identity in self._baseline_reader.load_active_baseline():
            if self._stop.is_set():
                return tally, True
            tally.examined += 1
            try:
                self._refresh_user(identity, snapshot.rows, galaxy, now, tally)
            except Exception as error:
                # 只记异常类型：异常正文可能带上被处理对象的姓名或邮箱。
                tally.failed += 1
                tally.count(f"failed_{type(error).__name__}")
                self._audit.record(
                    "permission_refresh.user_failed",
                    user=identity.app_user_id,
                    error=type(error).__name__,
                )
                logger.error(
                    "单个用户的权限重算失败，其余用户继续 user=%s error=%s",
                    identity.app_user_id,
                    type(error).__name__,
                )
        return tally, False

    def _finish_round(
        self, tally: _Tally, galaxy: Any, today: date, *, interrupted: bool
    ) -> PermissionRefreshReport:
        """收口一轮：记一条只含计数的报告审计，并决定要不要置位当日水位。

        **水位在一轮走完之后置位，即使这一轮里有个别用户失败**：失败已经逐条留痕并计入
        报告，而"有失败就整轮重来"会让一次持续的数据库故障变成每分钟重跑一遍全员——既救不了
        那个用户，又会把其余职责的时间预算吃掉。被停止信号中断的那一轮**不置位**：它没走完，
        下一次启动会重跑，而重跑对已经处理过的人是"无变化"，不产生第二条意图。
        """
        report = tally.freeze(interrupted=interrupted)
        action = "interrupted" if interrupted else "completed"
        self._audit.record(
            f"permission_refresh.{action}",
            report_date=today.isoformat(),
            **galaxy.audit_facts(),
            **report.audit_facts(),
        )
        if interrupted:
            logger.info("停止信号在权限重算期间到达，本轮未走完，水位不置位")
            return report
        self._completed_on = today
        # 摘要只有计数：日志流向排障、CI 输出与工单，不含任何业务内容。
        logger.info(
            "每日权限重算完成 已开通用户=%s 新发布意图=%s 无变化=%s 无可用权限=%s "
            "其中已排撤权=%s 输入不完整=%s 失败=%s",
            report.examined,
            report.enqueued,
            report.unchanged,
            report.revoked,
            report.revoked_published,
            report.incomplete,
            report.failed,
        )
        return report

    # ------------------------------------------------------------------
    # 单个用户
    # ------------------------------------------------------------------

    def _refresh_user(
        self,
        identity: ArchivedIdentity,
        roster_rows: Sequence[Any],
        galaxy: Any,
        now: datetime,
        tally: _Tally,
    ) -> None:
        """重算一个已开通用户：决策树在 core，这里只把出口翻成本职责的计数与留痕。"""
        decision = self._tree.decide(identity, roster_rows, galaxy, now=now)
        branch = decision.branch
        if branch is DecisionBranch.PUBLISHED:
            self._count_publish(tally, identity, decision)
        elif branch is DecisionBranch.GRANT_BLOCKED:
            self._count_grant_blocked(tally, identity, decision.account_state)
        elif branch in (DecisionBranch.REVOKED, DecisionBranch.REVOKE_WITHOUT_FOOTPRINT):
            self._count_revocation(tally, identity, decision)
        else:
            self._count_skip(tally, identity, decision)

    def _count_skip(
        self, tally: _Tally, identity: ArchivedIdentity, decision: UserDecision
    ) -> None:
        """只跳过、不发布的六条出口各自落到的阶段与原因码。

        匹配失败计入撤权（它是无权限的一种，但只跳过、不撤权）；零银河且存档不全的人
        同样计入撤权并留下银河给出的原因——没有邮箱姓名写不出撤权行，只能跳过。
        """
        branch = decision.branch
        if branch is DecisionBranch.MATCH_FAILED:
            self._skip(tally, identity, STAGE_MATCH, decision.match_reason, revoked=True)
            return
        if branch is DecisionBranch.TRANSLATION_UNCOVERED:
            reason = (
                SKIP_METRIC_TRANSLATION_UNAVAILABLE
                if decision.mapping_is_empty
                else SKIP_METRIC_TRANSLATION_UNCOVERED
            )
            self._skip(tally, identity, STAGE_TRANSLATE, reason, revoked=False)
            return
        if branch is DecisionBranch.ARCHIVE_INCOMPLETE and decision.zero_galaxy_reason:
            tally.revoked += 1
            tally.count(decision.zero_galaxy_reason)
            tally.count(SKIP_ARCHIVED_IDENTITY_INCOMPLETE)
            self._audit_user_skipped(identity, STAGE_IDENTITY, SKIP_ARCHIVED_IDENTITY_INCOMPLETE)
            return
        stage, reason = _SKIP_STAGES[branch]
        self._skip(tally, identity, stage, reason, revoked=False)

    def _count_revocation(
        self, tally: _Tally, identity: ArchivedIdentity, decision: UserDecision
    ) -> None:
        """撤权出口的计数与留痕：从无发布足迹只跳过，落了决定才可能算一次撤权发布。

        原因码区分"银河本来就没给"（聚合层的三个原因）与"银河给了、本地抑制清空"
        （:data:`REASON_FULLY_SUPPRESSED`），审计据此一眼看出这个人是被谁收回的。
        """
        reason = decision.zero_galaxy_reason or REASON_FULLY_SUPPRESSED
        tally.revoked += 1
        tally.count(reason)
        if decision.branch is DecisionBranch.REVOKE_WITHOUT_FOOTPRINT:
            tally.count(SKIP_NO_PUBLISHED_ROW)
            self._audit_user_skipped(
                identity, STAGE_AGGREGATE, reason, revocation=SKIP_NO_PUBLISHED_ROW
            )
            return
        if decision.enqueued:
            tally.enqueued += 1
            tally.revoked_published += 1
            self._audit_content_cleared(identity, decision.cleared_events, TRIGGER_REVOKE)
        else:
            # 上一条意图已经是同一份空权限：不推进版本、不排新意图、不清理。
            tally.unchanged += 1
        self._audit.record(
            "permission_refresh.user_revoked",
            user=identity.app_user_id,
            reason=reason,
            enqueued=decision.enqueued,
        )

    def _count_publish(
        self, tally: _Tally, identity: ArchivedIdentity, decision: UserDecision
    ) -> None:
        """授权出口：真的排出新意图才计入并留痕，与上一条逐字段相同就只算无变化。"""
        if decision.enqueued:
            tally.enqueued += 1
            self._audit_content_cleared(identity, decision.cleared_events, TRIGGER_GRANT)
        else:
            tally.unchanged += 1

    def _count_grant_blocked(
        self, tally: _Tally, identity: ArchivedIdentity, account_state: str | None
    ) -> None:
        """基线读取之后这个人才被停用：**被挡是正确结果，不是故障**。

        失败计数不加一——那一列是"处理这个人时抛了异常"，运维按它判断本轮健康度。这个人
        本轮什么都没写：事务整体回滚，版本没推进、意图没入队；他的撤权由停用那一刻的即时
        撤销路径负责。
        """
        tally.count(SKIP_ACCOUNT_NOT_ENABLED)
        self._audit.record(
            "permission_refresh.grant_blocked_account_state",
            user=identity.app_user_id,
            stage=STAGE_IDENTITY,
            reason=SKIP_ACCOUNT_NOT_ENABLED,
            account_state=account_state,
        )
        logger.warning(
            "本轮基线读取之后该用户已被停用，授权决定整体回滚 user=%s account_state=%s",
            identity.app_user_id,
            account_state,
        )

    def _audit_content_cleared(
        self, identity: ArchivedIdentity, cleared: int, trigger: str
    ) -> None:
        """权限确实变化时，落决定的同一个事务里顺带清掉了这个人已送达的投递正文。"""
        self._audit.record(
            "permission_refresh.delivered_content_cleared",
            user=identity.app_user_id,
            cleared=cleared,
            trigger=trigger,
        )

    def _audit_local_override_skipped(self, user_id: str, reason: str) -> None:
        """通配全指标时本地源整体不参与合并：决策树回调到这里，逐条留痕。"""
        self._audit.record(
            "permission_refresh.local_override_skipped",
            user=user_id,
            reason=reason,
        )

    def _audit_local_override_read_failure(self, user_id: str, error: Exception) -> None:
        """读不出来：响亮记一条本职责自己的审计。

        与另外两条链共用判据、各留各的事件名——运维从审计一眼看出是哪条链读失败的。
        本方法记的是 ``local_override_skipped``；调用方随后按跳过出口再记一条
        ``user_skipped``，两条各记各的，缺一条运维就断链。
        """
        self._audit.record(
            "permission_refresh.local_override_skipped",
            user=user_id,
            reason=REASON_LOCAL_OVERRIDE_READ_FAILED,
        )
        logger.error(
            "本地权限覆盖读取失败，本轮该用户跳过 user=%s error=%s",
            user_id,
            type(error).__name__,
        )

    def _audit_all_scope_refresh_failure(self, user_id: str, error: Exception) -> None:
        """「全部」组补行失败：只留痕，本轮按既有行照常发布。"""
        self._audit.record(
            "permission_refresh.legacy_all_scope_refresh_failed",
            user=user_id,
            error=type(error).__name__,
        )

    def _audit_all_scope_refreshed(self, user_id: str, added: int) -> None:
        """「全部」组补行成功：留下实际新增的行数。"""
        self._audit.record(
            "permission_refresh.legacy_all_scope_refreshed", user=user_id, added=added
        )

    def _skip(
        self,
        tally: _Tally,
        identity: ArchivedIdentity,
        stage: str,
        reason: str,
        *,
        revoked: bool,
    ) -> None:
        """记一次"这个人本轮不发布"，并计数。"""
        if revoked:
            tally.revoked += 1
        else:
            tally.incomplete += 1
        tally.count(reason)
        self._audit_user_skipped(identity, stage, reason)

    def _audit_user_skipped(
        self, identity: ArchivedIdentity, stage: str, reason: str, **facts: object
    ) -> None:
        """审计字段只有**内部用户标识、阶段与原因码**。

        ``app_user.id`` 是内部 ULID，离开数据库就映射不到人；邮箱、姓名、工号、银河账号、
        公司编号与职能标签一个都不写（`V-花名册-33` 的同一条纪律）。
        """
        self._audit.record(
            "permission_refresh.user_skipped",
            user=identity.app_user_id,
            stage=stage,
            reason=reason,
            **facts,
        )

    # ------------------------------------------------------------------
    # 跳过整轮
    # ------------------------------------------------------------------

    def _audit_skip(self, today: date, reason: str, **facts: object) -> None:
        """整轮跳过时留痕，**同一天同一原因只留一条**（构造函数里的水位注释）。

        去重只影响审计条数，不影响判据本身：下一轮照样重新判一次，前置一旦成立
        就立刻开跑。**同一天里出现过的每一种原因都会被记到**，包括来回切换后又回到
        先前那一种（A→B→A 只留 A、B 各一条，不会因为"最后一次记的不是 A"而把 A 记两次）。
        """
        day, reasons = (
            self._skip_audited
            if self._skip_audited is not None and self._skip_audited[0] == today
            else (today, set())
        )
        if reason in reasons:
            return
        reasons.add(reason)
        self._skip_audited = (day, reasons)
        action = _ROUND_SKIP_ACTIONS.get(reason, "permission_refresh.skipped_roster_not_fresh")
        self._audit.record(action, report_date=today.isoformat(), reason=reason, **facts)
        logger.warning("每日权限重算本轮不执行 reason=%s", reason)


def _build_permission_refresh_duty(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
) -> tuple[
    PermissionRefreshDuty | None,
    Mapping[str, Mapping[str, Sequence[str]]] | None,
]:
    """装配每日权限重算职责；前置不齐就**不注册**并留下**恰一条**审计。

    数据库连接串不构成能变红的前置——进程起得来就一定有；真正的运行前置（花名册今天更新
    过、银河有当前有效批次）是**数据**而不是配置，由每一轮重新判定。

    Returns:
        ``(职责, 翻译映射)``。第二个元素是本函数**唯一一次**读取翻译映射文件得到的对象，
        供装配层原样转给首次开通的发布闸，不在开通侧另开一次文件 I/O。
    """
    if not config.mcp_token_encrypt_key:
        return _refuse_registration(audit, missing_master_key=True)
    maps = _load_permission_maps(audit, config)
    if maps is None:
        return None, None
    role_function_map, metric_translation_map = maps
    duty = PermissionRefreshDuty(
        sources=_build_sources(config),
        role_function_map=role_function_map,
        metric_translation_map=metric_translation_map,
        audit=audit,
        stop=stop,
    )
    return duty, metric_translation_map


def _build_sources(config: SchedulerConfig) -> PermissionRefreshSources:
    """把八个读写端口接到真库上。

    同一个存储对象喂两个字段：一个只写权限决定，一个只读"发布过没有"。分成两个字段是为了
    让撤权那条判据在类型上说得清楚。
    """
    from lingxi.adapters.mcp_token_cipher import McpTokenCipher
    from lingxi.adapters.postgres_galaxy_snapshot import PostgresGalaxySnapshotReader
    from lingxi.adapters.postgres_mcp_token import PostgresMcpTokenStore
    from lingxi.adapters.postgres_permission_publish import (
        PostgresPermissionPublishStore,
        PostgresPermissionRefreshBaselineReader,
    )
    from lingxi.adapters.postgres_roster_snapshot import PostgresRosterSnapshotStore

    dsn = config.postgres_dsn
    timeouts = config.postgres_timeouts
    publish_store = PostgresPermissionPublishStore(dsn, timeouts=timeouts)
    return PermissionRefreshSources(
        baseline_reader=PostgresPermissionRefreshBaselineReader(dsn, timeouts=timeouts),
        roster_snapshot=PostgresRosterSnapshotStore(dsn, timeouts=timeouts),
        galaxy=PostgresGalaxySnapshotReader(dsn, timeouts=timeouts),
        decisions=publish_store,
        publish_history=publish_store,
        token_ciphers=PostgresMcpTokenStore(
            dsn,
            cipher=McpTokenCipher(config.mcp_token_encrypt_key),
            timeouts=timeouts,
        ),
        local_overrides=local_override_reader(dsn, timeouts=timeouts),
        legacy_all_scope=PostgresLocalPermissionOverrideStore(dsn, timeouts=timeouts),
    )


def _refuse_registration(audit: AuditSink, *, missing_master_key: bool) -> tuple[None, None]:
    """缺令牌主密钥时不注册。

    重算要读这个人**已有**的令牌密文，而唯一的读取口只接受已经校验过主密钥的加解密对象。
    没有密钥就没有读取口，而"读不到"与"这个人没有令牌"在下游是同一个空值——那会让每个需要
    新建发布行的人都失败关闭，表现成"接线了但一直失败"。本职责一次都不解密、也不签发，
    密钥在这里只用于构造那个读取口。审计只报变量名，不回显任何值：它还是一把主密钥。
    """
    del missing_master_key
    from lingxi.adapters.mcp_token_cipher import MASTER_KEY_ENV

    audit.record(
        "permission_refresh.duty_not_registered",
        reason="missing_environment_variable",
        variable=MASTER_KEY_ENV,
    )
    logger.warning("未配置 %s，每日权限重算职责不注册；其余定时职责照常运行", MASTER_KEY_ENV)
    return None, None


def _load_permission_maps(
    audit: AuditSink, config: SchedulerConfig
) -> tuple[Mapping[str, str], Mapping[str, Mapping[str, Sequence[str]]]] | None:
    """读两份随包发布的映射；任一读不出来就不注册。

    角色职能映射读不出来时**不能**退化成空映射——那会让所有角色变成「未映射」，于是全员被算
    成无可用权限，是一种看起来正常的失败。翻译映射只有**文件缺失或格式不对**才不注册：
    **空映射本身是合法内容**，职责本该正常跑起来，只是每个人都会在翻译那一步失败关闭并跳过。
    两者必须分开判断——把「内容还没到」和「部署配置本身有问题」混成同一种「职责不注册」，就
    无从分辨该找谁。审计与日志只记异常类型：配置解析失败的正文可能带上文件内容片段。
    """
    from lingxi.adapters.company_function_metric_map_file import (
        load_company_function_metric_map,
    )
    from lingxi.adapters.role_function_map_file import load_role_function_map

    try:
        role_function_map = load_role_function_map()
    except (OSError, ValueError) as error:
        audit.record(
            "permission_refresh.duty_not_registered",
            reason="role_function_map_unavailable",
            error=type(error).__name__,
        )
        logger.error(
            "角色职能映射配置不可用，每日权限重算职责不注册 error=%s", type(error).__name__
        )
        return None
    try:
        metric_translation_map = load_company_function_metric_map(config.metric_map_path)
    except (OSError, ValueError) as error:
        audit.record(
            "permission_refresh.duty_not_registered",
            reason="metric_translation_map_unavailable",
            error=type(error).__name__,
        )
        logger.error(
            "公司+职能→指标名翻译映射配置不可用，每日权限重算职责不注册 error=%s",
            type(error).__name__,
        )
        return None
    return role_function_map, metric_translation_map


#: 端口协议、原因码与报告形状搬到 ``permission_refresh_ports``；旧 import 路径继续可用。
__all__ = [
    "PERMISSION_REFRESH_REASON",
    "PERMISSION_REVOKE_REASON",
    "PermissionRefreshDuty",
    "PermissionRefreshReport",
    "REASON_FULLY_SUPPRESSED",
    "SKIP_ACCOUNT_NOT_ENABLED",
    "SKIP_METRIC_TRANSLATION_UNAVAILABLE",
    "SKIP_METRIC_TRANSLATION_UNCOVERED",
    "SKIP_NO_PUBLISHED_ROW",
    "TRIGGER_GRANT",
    "TRIGGER_REVOKE",
    "PermissionRefreshSources",
    "_BaselineReader",
    "_DecisionStore",
    "_GalaxySnapshotReader",
    "_LegacyAllScopeExpander",
    "_LocalOverrideReader",
    "_PublishHistory",
    "_RosterSnapshotStore",
    "_TokenCipherReader",
]
