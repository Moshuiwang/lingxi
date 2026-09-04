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
    LocalOverrideReadError,
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
from lingxi.core.permission.account_match import MATCHED, match_galaxy_account
from lingxi.core.permission.legacy_diff import missing_all_scope_metrics
from lingxi.core.permission.local_override import (
    LocalPermissionOverrideEntry,
    ResolvedLocalOverrides,
    resolve_local_overrides,
)
from lingxi.core.permission.merge_sources import (
    REASON_LOCAL_OVERRIDE_READ_FAILED,
    merge_permission_sources,
)
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

logger = logging.getLogger(__name__)

_UTC = UTC


class PermissionRefreshDuty:
    """每日权限重算：花名册新鲜 → 银河当前批次 → 逐个已开通用户重算并排发布意图。

    语义与边界见模块文档。本类**只编排**：匹配、聚合、翻译、发布行结算四条规则分别在
    :mod:`lingxi.core.permission.account_match`、
    :mod:`lingxi.core.permission.metric_translation`、
    :mod:`lingxi.core.permission.publish_row` 里，版本推进与幂等在
    :mod:`lingxi.adapters.postgres_permission_publish`，这里一条都不复制。
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
        self._decisions = sources.decisions
        self._publish_history = sources.publish_history
        self._token_ciphers = sources.token_ciphers
        self._local_overrides = sources.local_overrides
        self._legacy_all_scope = sources.legacy_all_scope
        self._role_function_map = role_function_map
        self._metric_translation_map = metric_translation_map
        self._audit = audit
        # 时钟注入：跨轮判重与"今天"的用例要能自己决定日期，不能靠等到明天。
        self._clock = clock or (lambda: datetime.now(_UTC))
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
        """重算一个已开通用户。任何"不发布"的出口都在这里显式返回，不落到默认分支。

        匹配阶段的失败只跳过、不撤权：它说的是"我们认不出这个人是谁"，不是"银河说他没有
        权限"。据一次花名册歧义或数据陈旧去清空一个人的权限，方向与花名册那一侧「查无此人
        仅提示、不做任何自动处置」的既定口径正好相反。
        """
        if not identity.personnel_id:
            # 建档合同要求人员 ID 必填，但存档里真的没有时，匹配层会直接抛错。
            # 在这里归类成"输入不完整"，而不是让它冒充一次技术故障。
            self._skip(tally, identity, STAGE_IDENTITY, SKIP_MISSING_PERSONNEL_ID, revoked=False)
            return

        match = match_galaxy_account(identity.personnel_id, roster_rows, galaxy.user_rows)
        if match.state != MATCHED or not match.galaxy_user_id:
            self._skip(tally, identity, STAGE_MATCH, match.reason, revoked=True)
            return

        aggregate = aggregate_permission(
            galaxy_user_id=match.galaxy_user_id,
            user_role_rows=galaxy.role_rows(match.galaxy_user_id),
            datacountry_rows=galaxy.datacountry_rows(match.galaxy_user_id),
            country_rows=galaxy.country_rows,
            role_function_map=self._role_function_map,
        )
        if not aggregate.granted:
            self._refresh_zero_galaxy_user(tally, identity, aggregate, now)
            return

        if not identity.email or not identity.display_name:
            # 发布行的三列都来自存档身份。缺了就没有"这一行是谁的"的答案——先归类，
            # 而不是让渲染函数抛错之后被当成一次技术故障。
            self._skip(
                tally, identity, STAGE_IDENTITY, SKIP_ARCHIVED_IDENTITY_INCOMPLETE, revoked=False
            )
            return

        company_metrics = self._translate(tally, identity, aggregate)
        if company_metrics is None:
            return
        self._publish_or_revoke(tally, identity, aggregate, company_metrics, now)

    def _translate(
        self, tally: _Tally, identity: ArchivedIdentity, aggregate: Any
    ) -> Mapping[str, Sequence[str]] | None:
        """把「公司 + 职能」翻成指标名；未覆盖就跳过这一个人——不发布，也不撤权。

        放在令牌读取之前：既然本轮不会发布，没必要为一个注定要跳过的人去查令牌表。整轮判据已经
        确保走到这里时映射非空，因此「映射为空」实践中恒假；这个分支仍然按真实取值分类而不是
        硬编码，是为了不让这条逐用户判据的正确性依赖「调用方一定会先做整轮判据」这条外部不变量
        ——翻译本身是纯函数，直接调用它时映射完全可能是空的。未覆盖时返回 ``None``。
        """
        try:
            return translate_company_functions(
                companies=aggregate.companies,
                functions=aggregate.functions,
                all_companies=aggregate.all_companies,
                mapping=self._metric_translation_map,
            )
        except UncoveredPermissionCombination as error:
            reason = (
                SKIP_METRIC_TRANSLATION_UNAVAILABLE
                if error.mapping_is_empty
                else SKIP_METRIC_TRANSLATION_UNCOVERED
            )
            self._skip(tally, identity, STAGE_TRANSLATE, reason, revoked=False)
            return None

    def _publish_or_revoke(
        self,
        tally: _Tally,
        identity: ArchivedIdentity,
        aggregate: Any,
        company_metrics: Mapping[str, Sequence[str]],
        now: datetime,
    ) -> None:
        """合并本地覆盖之后决定发布还是撤权：真实权限 =（银河 ∪ 本地授权）− 本地抑制。

        通配全指标有两个互相独立的成因（范围覆盖全部国家，或持有全量访问职能），只有后者
        是真的全指标通配——合并函数自己不猜，调用方必须显式声明。

        合并结果被本地抑制压光到空时**走撤权出口**：这个人银河这一侧原本是有效授权，本地
        行政性地收回到零，语义上等同于撤权，因此复用同一套机制但带一个**可分辨**的原因码。
        不这么做的话，空字典会让渲染函数抛错，被记成一条不可分辨的通用失败——审计上完全
        看不出"这个人是被本地抑制清空的"。
        """
        local = self._resolve_local_overrides(identity.app_user_id)
        merged = merge_permission_sources(
            galaxy=company_metrics,
            local=local,
            full_access_wildcard=ADMIN_FULL_ACCESS_FUNCTION in aggregate.functions,
        )
        for reason in merged.skipped_reasons:
            # 通配全指标时本地源整体不参与合并，逐条留痕。
            self._audit.record(
                "permission_refresh.local_override_skipped",
                user=identity.app_user_id,
                reason=reason,
            )
        if merged.unrepresentable_companies:
            self._skip(
                tally, identity, STAGE_AGGREGATE, SKIP_SUPPRESSION_UNREPRESENTABLE, revoked=False
            )
            return
        if not merged.permissions:
            self._revoke(tally, identity, REASON_FULLY_SUPPRESSED, now)
            return
        self._enqueue_publish(tally, identity, merged.permissions, now)

    def _refresh_zero_galaxy_user(
        self,
        tally: _Tally,
        identity: ArchivedIdentity,
        aggregate: Any,
        now: datetime,
    ) -> None:
        """银河这一侧判定「无可用权限」时：查一次**本地授权**，非空就发布、仍为空才撤权。

        管理员的本地授权是「银河之外的兜底赋权」，与这个人此刻有没有银河权限无关。**存档不全时
        直接走撤权、不先查本地覆盖**：两种行都需要邮箱和姓名，任何合并结果都救不了一个存档不全
        的人，提前判掉能省一次读放大。**不翻译**：这条分支上银河的公司与职能恒为空，对合并的
        贡献直接是空字典——不调用翻译（对空输入它会直接拒绝，那是「参数缺失」不是「没有内容」），
        因此它与翻译层的两层判据完全没有交集。
        """
        if not identity.email or not identity.display_name:
            self._revoke(tally, identity, aggregate.reason, now)
            return

        try:
            local = self._resolve_local_overrides(identity.app_user_id, raise_on_failure=True)
        except LocalOverrideReadError:
            # 读取失败＝本轮跳过这个人：银河贡献恒为空，本地授权是否读到直接决定"发布
            # 还是撤权"这件事本身。把读失败折叠成"没有本地授权"，会让一次纯粹的数据库
            # 抖动变成真的撤权并同事务清空已送达正文——不可逆。读取口已经记过一条跳过
            # 审计，这里只补计数。
            tally.count(SKIP_LOCAL_OVERRIDE_READ_FAILED)
            return

        # 通配标志现在是必填关键字参数（默认值曾是一次真实漏接的根因）——这条分支的银河
        # 侧恒为空字典，取值对结果没有作用面，仍必须显式传参。
        merged = merge_permission_sources(galaxy={}, local=local, full_access_wildcard=True)
        for reason in merged.skipped_reasons:
            self._audit.record(
                "permission_refresh.local_override_skipped",
                user=identity.app_user_id,
                reason=reason,
            )

        if merged.unrepresentable_companies:
            self._skip(
                tally, identity, STAGE_AGGREGATE, SKIP_SUPPRESSION_UNREPRESENTABLE, revoked=False
            )
            return
        if not merged.permissions:
            # 既无银河也无本地授权（或本地授权已被同键抑制清空）：维持撤权语义。
            self._revoke(tally, identity, aggregate.reason, now)
            return
        # 本地授权非空：发布内容精确等于合并结果，因为银河一侧贡献为空。
        self._enqueue_publish(tally, identity, merged.permissions, now)

    def _enqueue_publish(
        self,
        tally: _Tally,
        identity: ArchivedIdentity,
        company_metrics: Mapping[str, Sequence[str]],
        now: datetime,
    ) -> None:
        """结算并落一次授权发布决定，两条授权路径共用这一段收尾。

        银河授权路径与"零银河 ＋ 本地授权兜底"路径殊途同归：都在合并之后拿到非空内容，
        剩下的（只读令牌密文、结算发布行、落决定、计数、清已送达正文）与"这份内容是从银河
        翻译来的还是纯本地授权来的"无关。

        令牌只取**已有**密文，取不到就留空，由发布执行器失败关闭；这里没有、也不允许有
        任何签发路径。
        """
        row = build_translated_publish_row(
            company_metrics=company_metrics,
            email=identity.email,
            display_name=identity.display_name,
            decided_at=now,
            token_cipher=self._token_ciphers.token_cipher(identity.app_user_id),
        )
        try:
            decision = self._decisions.record_decision(
                user_id=identity.app_user_id,
                row=row,
                reason=PERMISSION_REFRESH_REASON,
                # 这是一份**需要账号有效**的授权。基线读取到轮到这个人被处理之间，管理员
                # 可能刚把他停用并排空了权限——判据必须落在落决定那把已经持有的行锁里
                # （同一行同一把锁＝与停用写入串行），而不是这里先查一次账号状态（那只会
                # 把窗口缩小）。
                require_enabled_account=True,
                decided_at=now,
                # 权限确实变化时，在落决定自己的同一个事务里顺带清空这个人已送达、随会话
                # 保留的投递正文。
                clear_delivered_content=True,
            )
        except PermissionGrantBlockedByAccountState as blocked:
            self._count_grant_blocked(tally, identity, blocked)
            return
        if decision.enqueued:
            tally.enqueued += 1
            self._audit.record(
                "permission_refresh.delivered_content_cleared",
                user=identity.app_user_id,
                cleared=decision.cleared_events,
                trigger=TRIGGER_GRANT,
            )
        else:
            # 权限内容与上一条仍然有效的意图逐字段相同：不推进版本、不排新意图、不清理。
            tally.unchanged += 1

    def _count_grant_blocked(self, tally: _Tally, identity: ArchivedIdentity, blocked: Any) -> None:
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
            account_state=blocked.account_state,
        )
        logger.warning(
            "本轮基线读取之后该用户已被停用，授权决定整体回滚 user=%s account_state=%s",
            identity.app_user_id,
            blocked.account_state,
        )

    def _resolve_local_overrides(
        self, user_id: str, *, raise_on_failure: bool = False
    ) -> ResolvedLocalOverrides | None:
        """读这个人当前生效的本地覆盖条目。未装配与读取失败都返回 ``None``，但审计姿态不同。

        **未装配**是部署事实，不告警；**读取失败**响亮记一条跳过审计，异常本身不冒泡——一个人的
        本地覆盖读不出来不得带走他当轮的银河权限发布，更不能带走整轮。提前捕获是为了把「翻译
        失败」与「本地覆盖读取失败」两种原因分开审计。

        ``raise_on_failure=True`` 时读取失败改为抛出：零银河那条分支上本地源是否读到直接决定
        「发布还是撤权」，读取失败绝不能被无声折叠成「没有本地授权」进而触发撤权（同事务清已
        送达正文，不可逆）。两种情形都已经在这里记过审计，调用方不该再重复记一条。
        """
        if self._local_overrides is None:
            return None
        try:
            entries = tuple(self._local_overrides.effective_entries(user_id=user_id))
        except Exception as error:  # 本地源读取失败只降级，不整轮/整人失败
            self._audit.record(
                "permission_refresh.local_override_skipped",
                user=user_id,
                reason=REASON_LOCAL_OVERRIDE_READ_FAILED,
            )
            logger.error(
                "本地权限覆盖读取失败，本轮该用户跳过本地源 user=%s error=%s",
                user_id,
                type(error).__name__,
            )
            if raise_on_failure:
                raise LocalOverrideReadError() from error
            return None
        entries = self._expand_legacy_all_scope(user_id, entries)
        return resolve_local_overrides(user_id=user_id, entries=entries)

    def _expand_legacy_all_scope(
        self, user_id: str, entries: tuple[LocalPermissionOverrideEntry, ...]
    ) -> tuple[LocalPermissionOverrideEntry, ...]:
        """给「全部」组随当前映射补齐新指标。

        缺才补、同组标识、撤销过的组不参与（只看生效条目）；补行成功后重读一次条目让本轮
        合并直接带上新行。补行或重读失败都只审计，不影响本轮既有结果。
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
            except Exception as error:  # 补行失败不影响本轮既有合并
                self._audit.record(
                    "permission_refresh.legacy_all_scope_refresh_failed",
                    user=user_id,
                    error=type(error).__name__,
                )
                continue
            self._audit.record(
                "permission_refresh.legacy_all_scope_refreshed", user=user_id, added=added
            )
            added_total += added
        if added_total == 0:
            return entries
        try:
            return tuple(self._local_overrides.effective_entries(user_id=user_id))
        except Exception:  # 重读失败：新行下一轮自然生效
            return entries

    def _revoke(
        self,
        tally: _Tally,
        identity: ArchivedIdentity,
        reason: str,
        now: datetime,
    ) -> None:
        """这个人现在没有可用权限：该清空的清空，该跳过的跳过。

        撤权是**保行、清空权限内容**：发布表那一行留着，权限写成空对象，状态与令牌密文都不碰。
        三条出口按次序判、每一条都显式返回：存档缺邮箱或姓名 → 跳过（这一步在查库之前，因为它
        不需要查库）；在发布链上一点足迹都没有 → 跳过（不为一个没有发布行的人新建一行空权限）；
        否则结算撤权行并落一次权限决定。

        「在途也算足迹」这一半是必需的：昨天排的授权意图还堵在待发布、今天这个人被撤权时若跳过，
        等发布面消费积压时**已经被收回的范围**会被写进外部表并触发一条「范围已更新」通知。
        """
        tally.revoked += 1
        tally.count(reason)
        if not identity.email or not identity.display_name:
            tally.count(SKIP_ARCHIVED_IDENTITY_INCOMPLETE)
            self._audit.record(
                "permission_refresh.user_skipped",
                user=identity.app_user_id,
                stage=STAGE_IDENTITY,
                reason=SKIP_ARCHIVED_IDENTITY_INCOMPLETE,
            )
            return
        if not self._publish_history.has_publish_footprint(identity.app_user_id):
            tally.count(SKIP_NO_PUBLISHED_ROW)
            self._audit.record(
                "permission_refresh.user_skipped",
                user=identity.app_user_id,
                stage=STAGE_AGGREGATE,
                reason=reason,
                revocation=SKIP_NO_PUBLISHED_ROW,
            )
            return
        self._record_revocation(tally, identity, reason, now)

    def _record_revocation(
        self, tally: _Tally, identity: ArchivedIdentity, reason: str, now: datetime
    ) -> None:
        """落一次撤权决定。是否真的排出新意图由落决定的内容比对决定。

        第二天仍然无权限时判"无变化"，因此撤权**不会每天重发一次**。

        撤权**任何账号状态都必须放行**：挡住撤权＝停用彻底失效，是方向相反、后果最严重的
        那种错误——强制撤权的服务对象本来就是已停用用户。声明放行时实现侧还会断言这一行
        确实是空权限撤权行，传错在运行期就写不出来。
        """
        decision = self._decisions.record_decision(
            user_id=identity.app_user_id,
            row=build_revocation_row(
                email=identity.email,
                display_name=identity.display_name,
                decided_at=now,
            ),
            reason=PERMISSION_REVOKE_REASON,
            require_enabled_account=False,
            decided_at=now,
            # 与授权侧同一个开关、同一条理由：权限确实变化时在同一个事务里顺带清空这个人
            # 已送达、随会话保留的投递正文。
            clear_delivered_content=True,
        )
        if decision.enqueued:
            tally.enqueued += 1
            tally.revoked_published += 1
            self._audit.record(
                "permission_refresh.delivered_content_cleared",
                user=identity.app_user_id,
                cleared=decision.cleared_events,
                trigger=TRIGGER_REVOKE,
            )
        else:
            # 上一条意图已经是同一份空权限：不推进版本、不排新意图、不清理。
            tally.unchanged += 1
        self._audit.record(
            "permission_refresh.user_revoked",
            user=identity.app_user_id,
            reason=reason,
            enqueued=decision.enqueued,
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
        """记一次"这个人本轮不发布"，并计数。

        审计字段只有**内部用户标识、阶段与原因码**：``app_user.id`` 是内部 ULID，
        离开数据库就映射不到人；邮箱、姓名、工号、银河账号、公司编号与职能标签
        一个都不写（`V-花名册-33` 的同一条纪律）。
        """
        if revoked:
            tally.revoked += 1
        else:
            tally.incomplete += 1
        tally.count(reason)
        self._audit.record(
            "permission_refresh.user_skipped",
            user=identity.app_user_id,
            stage=stage,
            reason=reason,
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
