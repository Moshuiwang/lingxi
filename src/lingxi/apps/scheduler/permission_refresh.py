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
取不到就交给发布执行器失败关闭；通知的触发点在发布读回一致或就绪探针成功之后，属另一个
职责，这里连一个发送端口都没有。
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
    LocalOverrideReadError,
    PERMISSION_REFRESH_REASON,
    PERMISSION_REVOKE_REASON,
    PermissionRefreshReport,
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
    _AuditSink,
    _BaselineReader,
    _DecisionStore,
    _GalaxySnapshotReader,
    _LegacyAllScopeExpander,
    _LocalOverrideReader,
    _PublishHistory,
    _ROUND_SKIP_ACTIONS,
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
        baseline_reader: _BaselineReader,
        roster_snapshot: _RosterSnapshotStore,
        galaxy: _GalaxySnapshotReader,
        decisions: _DecisionStore,
        publish_history: _PublishHistory,
        token_ciphers: _TokenCipherReader,
        role_function_map: Mapping[str, str],
        metric_translation_map: Mapping[str, Mapping[str, Sequence[str]]],
        audit: _AuditSink,
        clock: Callable[[], datetime] | None = None,
        stop: threading.Event | None = None,
        local_overrides: _LocalOverrideReader | None = None,
        legacy_all_scope: _LegacyAllScopeExpander | None = None,
    ) -> None:
        self._baseline_reader = baseline_reader
        self._roster_snapshot = roster_snapshot
        self._galaxy = galaxy
        self._decisions = decisions
        self._publish_history = publish_history
        self._token_ciphers = token_ciphers
        self._role_function_map = role_function_map
        self._metric_translation_map = metric_translation_map
        self._audit = audit
        # 本地权限覆盖读取口（S-P-3）：``None`` 表示装配层还没接这个 store——本轮/
        # 本用户的合并按"没有本地源"处理，产出与今天逐字节一致（模块文档「翻译」
        # 一节旁的「本地覆盖」小节 :func:`merge_permission_sources` 对 ``local=None``
        # 恒等的性质）。装配层的真实实现见 ``apps/scheduler/assembly.py``。
        self._local_overrides = local_overrides
        # 「全部」组补行口（rc25 S-1 方案 E）：``None``＝未装配，不补行。
        self._legacy_all_scope = legacy_all_scope
        # 时钟注入：跨轮判重与"今天"的用例要能自己决定日期，不能靠等到明天。
        self._clock = clock or (lambda: datetime.now(_UTC))
        # 与同一进程内的其他职责共享停止标志：SIGTERM 一次让所有职责停止领取新工作。
        self._stop = threading.Event() if stop is None else stop
        self._completed_on: date | None = None
        # 跳过类审计的**当日去重水位**：当天已经记过哪些原因。顺序判据在当前部署下每轮
        # 都不成立，而调度周期是一分钟——不去重的话，一天会刷出一千四百多条内容完全相同
        # 的审计，真正的信号会被埋掉。
        #
        # 存的是**原因集合**而不是"最后一个原因"：同一天里原因会来回变（花名册快照到了
        # 又被换成旧的、银河批次过期又重导），只记最后一个的话 A→B→A 会把 A 记两次，
        # 去重就在最需要它的那条路径上失效了。
        self._skip_audited: tuple[date, set[str]] | None = None

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    @property
    def completed_on(self) -> date | None:
        """已完成重算的那一天。``None`` 表示本进程实例今天还没跑完过。"""

        return self._completed_on

    def request_stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    # 一轮
    # ------------------------------------------------------------------

    def run_once(self) -> PermissionRefreshReport | None:
        """跑一轮。返回 ``None`` 表示本轮没有重算（停止中、今天已跑完，或前置不成立）。"""

        if self._stop.is_set():
            # 已经在停止中：一轮都不开，一条发布意图都不排。
            return None
        now = self._clock()
        today = _utc_date(now)
        if self._completed_on == today:
            return None

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
            # 元信息与整份快照分两条语句读，中间可能有一次并发替换（花名册审计职责
            # 就在同一进程里）。这里不是防御式编程：读到"元信息说有、整份却没有"时，
            # 唯一安全的动作是本轮不跑，下一轮那份新快照会自己把日期判据带过来。
            self._audit_skip(today, SKIP_MISSING_SNAPSHOT)
            return None
        if _utc_date(snapshot.facts.captured_at) != today:
            self._audit_skip(
                today,
                SKIP_STALE_SNAPSHOT,
                snapshot_date=_utc_date(snapshot.facts.captured_at).isoformat(),
            )
            return None

        # 顺序判据成立之后才碰银河：花名册不新鲜的那一轮**一次银河读取都不发起**。
        galaxy = self._galaxy.load_current()
        if galaxy is None:
            self._audit_skip(today, SKIP_NO_GALAXY_BATCH)
            return None

        if not metric_translation_available(self._metric_translation_map):
            # 外部独立审查 2026-08-18 坐实的 P1：翻译层映射整体为空时，**整轮**
            # 一条发布意图都不排——撤权也不例外。``_revoke`` 从不调用翻译（它写的
            # 是不含指标名的 ``{}``），因此把这条判据放在逐用户层面挡不住撤权；
            # 唯一挡得住的位置是**遍历开始之前**：判据是"翻译层这一轮可不可用"，
            # 不是"这一行要不要翻译"。模块文档「翻译」一节有完整理由——映射为空时
            # 若只挡授权、放行撤权，权限在内容到位之前只能单向减少、不能恢复，
            # 这是最危险的那种不对称。
            #
            # ``metric_translation_available`` 是唯一允许存在的判据实现（见其
            # docstring）：首次开通编排（``apps.scheduler.assembly`` 的
            # ``publish_allowed``，Issue #227 开通侧整合）对同一个已加载对象调用
            # 同一个函数，两个独立写入点因此不会漂移出两套看起来等价的检查。
            self._audit_skip(today, SKIP_METRIC_TRANSLATION_UNAVAILABLE)
            return None

        baseline = self._baseline_reader.load_active_baseline()
        tally = _Tally()
        interrupted = False
        for identity in baseline:
            if self._stop.is_set():
                # 停止信号落在遍历中间：不再为后面的人排新的发布意图。已经落库的那些
                # 决定各自是一个完整事务，不存在半态；水位不置位，因此下一次启动会把
                # 这一轮重跑一遍——重跑对已经处理过的人是 ``UNCHANGED``，不产生第二条意图。
                interrupted = True
                break
            # 计数在**领取时**递增，不在遍历前按基线行数一次性写死：被停止信号挡在外面
            # 的那些人从来没有被看过一眼，把他们算进"已检查"会让中断轮的报告读起来像是
            # "全都查过了、只是什么都没做"。
            tally.examined += 1
            try:
                self._refresh_user(identity, snapshot.rows, galaxy, now, tally)
            except Exception as error:  # noqa: BLE001 - 一个用户的失败不得带走整轮
                # 只记异常类型：异常正文可能带上被处理对象的内容（邮箱、姓名）。
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

        report = tally.freeze(interrupted=interrupted)
        if interrupted:
            self._audit.record(
                "permission_refresh.interrupted",
                report_date=today.isoformat(),
                **galaxy.audit_facts(),
                **report.audit_facts(),
            )
            logger.info("停止信号在权限重算期间到达，本轮未走完，水位不置位")
            return report

        self._audit.record(
            "permission_refresh.completed",
            report_date=today.isoformat(),
            **galaxy.audit_facts(),
            **report.audit_facts(),
        )
        self._completed_on = today
        # 摘要只有计数（`V-花名册-33` 的同一条纪律：日志流向排障、CI 输出与工单）。
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
        """重算一个已开通用户。任何"不发布"的出口都在这里显式返回，不落到默认分支。"""

        if not identity.personnel_id:
            # 建档合同要求人员 ID 必填，但存档里真的没有时，匹配层会直接抛错。
            # 在这里归类成"输入不完整"，而不是让它冒充一次技术故障。
            self._skip(tally, identity, STAGE_IDENTITY, SKIP_MISSING_PERSONNEL_ID, revoked=False)
            return

        match = match_galaxy_account(identity.personnel_id, roster_rows, galaxy.user_rows)
        if match.state != MATCHED or not match.galaxy_user_id:
            # 匹配不上就是"没有可用的银河权限"（`V-开通-02/03/06/09` 的统一出口）。
            # 原因码由匹配层给出且可分辨（``roster_not_found``、``key_conflict`` …），
            # 但用户侧的产品语义是同一个，本职责在这里只跳过并计数。
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
            # **零银河权限：不再无条件撤权**（PM 2026-08-29 裁定，Issue #419，消
            # `V-权限-15` 此前登记的已知限制）。管理员的本地授权是「银河之外的兜底
            # 赋权」，产品语义上与用户此刻有没有银河权限无关——挂在 `aggregate.
            # granted` 判据之后只是实现上的历史顺序，不是产品裁定。因此这里把「银河
            # 这一侧完全没有可翻译的内容」当成 `merge_permission_sources` 的一个
            # 合法空输入（`galaxy={}`），查一次本地授权，合并结果非空才算
            # 数——既无银河也无本地授权（或本地授权已被同键抑制清空）时，合并结果
            # 仍是空字典，维持现行撤权语义不变，`_revoke` 一个字节都不改。
            self._refresh_zero_galaxy_user(tally, identity, aggregate, now)
            return

        if not identity.email or not identity.display_name:
            # 发布行的 ``record_key``/``email``/``name`` 三列都来自存档身份。缺了就
            # 没有"这一行是谁的"的答案——先归类，而不是让 ``build_translated_publish_row``
            # 抛错之后被当成一次技术故障。
            self._skip(
                tally, identity, STAGE_IDENTITY, SKIP_ARCHIVED_IDENTITY_INCOMPLETE, revoked=False
            )
            return

        # 翻译「公司 + 职能」→ 指标名（Issue #227）。未覆盖就跳过——不发布、不撤权，
        # 详见模块文档「翻译」一节。放在令牌读取之前：既然本轮不会发布，没有必要为
        # 一个注定要跳过的人去查令牌表。
        try:
            company_metrics = translate_company_functions(
                companies=aggregate.companies,
                functions=aggregate.functions,
                all_companies=aggregate.all_companies,
                mapping=self._metric_translation_map,
            )
        except UncoveredPermissionCombination as error:
            # `run_once` 的整轮判据已经确保走到这里时映射非空，因此
            # `error.mapping_is_empty` 实践中恒为 False；这个分支仍然按它的真实值
            # 分类而不是硬编码，是为了不让这条逐用户判据的正确性依赖"调用方一定会先
            # 做整轮判据"这条外部不变量——`translate_company_functions` 是纯函数，
            # 直接调用它时映射完全可能是空的（模块文档「翻译」一节两层判据分工）。
            reason = (
                SKIP_METRIC_TRANSLATION_UNAVAILABLE
                if error.mapping_is_empty
                else SKIP_METRIC_TRANSLATION_UNCOVERED
            )
            self._skip(tally, identity, STAGE_TRANSLATE, reason, revoked=False)
            return

        # 本地权限覆盖合并（S-P-3 本地覆盖 #319）：真实权限 =
        # (银河 ∪ 本地授权) − 本地抑制。挂在「翻译完成之后、结算发布行
        # 之前」——`company_metrics` 就是银河那一侧已经翻译好的 `{公司: (指标名, …)}`。
        # 见 `core/permission/merge_sources.py` 模块文档。
        local = self._resolve_local_overrides(identity.app_user_id)
        # 通配角 v2（Issue #440）：`all_companies=True` 有两个互相独立的成因
        # （`scope.all_countries` 或持有 `ADMIN_FULL_ACCESS_FUNCTION`），只有后者
        # 是「真全指标通配」——`merge_permission_sources` 自己不猜测，调用方必须
        # 显式声明（见该函数「通配角 v2」文档）。
        merged = merge_permission_sources(
            galaxy=company_metrics,
            local=local,
            full_access_wildcard=ADMIN_FULL_ACCESS_FUNCTION in aggregate.functions,
        )
        for reason in merged.skipped_reasons:
            # 通配角 v1：本地源在 `all_companies=True` 下整体不参与合并，见
            # `merge_permission_sources` 模块文档「通配角」一节。
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
            # 红线-2（Trace #328 opus 审查）：银河这一侧原本是有效授权
            # （company_metrics 非空，翻译已经成功），但本地抑制把合并结果压光到
            # 空字典——这个人此刻没有任何可发布内容，语义上等同于撤权。走
            # `_revoke` 同一套机制（保行清空、只对发布链上留过足迹的人发），但带一个
            # 可分辨的原因码，不落到 `build_translated_publish_row` 对空输入的
            # `ValueError` → 通用 `user_failed`（模块文档「全抑制」一节）。
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
        """银河这一侧判定"无可用权限"（`no_galaxy_roles`/`no_supported_function`/
        `no_company_scope`）时的新分支（PM 2026-08-29 裁定，Issue #419）：查一次
        **本地授权**，合并结果非空就发布，仍为空才撤权。

        **存档不全时直接走撤权、不先查本地覆盖**：撤权行与发布行都需要
        `email`/`display_name` 这两列，任何合并结果都救不了一个存档不全的人，提前
        判掉能省一次读放大——`_revoke` 自己的完整性检查本就在查发布足迹之前短路
        （模块文档「撤权」一节），这里保持与它逐字节一致的观测行为
        （`tests/test_permission_refresh_duty.py::RevocationPublishTest.
        test_a_revoked_user_with_an_incomplete_archive_is_skipped` 钉住
        "存档不全时连发布足迹都不查"，本方法不得破坏这条既有断言）。

        **本地授权读取失败＝本轮跳过这个人，不落撤权（P1-1，独立审查坐实并
        修复）**：银河对这条分支的合并贡献恒为 `{}`（不翻译，见下），因此本地
        授权是否读到直接决定"发布还是撤权"这件事本身——修复前读取失败与"没有
        本地授权"落到同一个 `None`，会让一次纯粹的数据库抖动被误判成"这个人
        没有权限"，真的撤权并同事务清空已送达正文（不可逆）。改为
        `self._resolve_local_overrides(..., raise_on_failure=True)`，捕获
        :class:`LocalOverrideReadError` 后**本轮直接返回**：不发布、不撤权、
        不清正文，等下一轮数据库恢复后再重新判定；`_resolve_local_overrides`
        已经记过一条 `local_override_skipped` 审计，这里只补计数，不重复记审计
        （见该方法文档）。**否定用例**：读失败 → 零发布行为变化 + 恰一条审计
        （`ZeroGalaxyLocalGrantTest.
        test_a_local_override_read_failure_skips_the_user_without_revoking`）。

        **不翻译**：`aggregate.granted` 为假时 `aggregate.companies`/`functions`
        恒为空（`PermissionAggregate.__post_init__` 的不变式），银河这一侧对合并
        的贡献直接是 `galaxy={}`——不调用 `translate_company_functions`（对空输入
        它会直接拒绝，那是"参数缺失"，不是"没有内容"），因此这条分支与翻译层整轮/
        逐用户两层判据（模块文档「翻译」一节）完全没有交集：翻译层不可用不影响它，
        它也不消费翻译结果。
        """

        if not identity.email or not identity.display_name:
            self._revoke(tally, identity, aggregate.reason, now)
            return

        try:
            local = self._resolve_local_overrides(identity.app_user_id, raise_on_failure=True)
        except LocalOverrideReadError:
            tally.count(SKIP_LOCAL_OVERRIDE_READ_FAILED)
            return

        # `full_access_wildcard` 现在是必填关键字参数（Trace #445 结构性防复发：
        # 默认值曾是一次真实漏接的根因）——这条分支 `galaxy` 恒为空字典，不含
        # `ALL_COMPANIES_KEY`，取值对结果没有作用面，仍必须显式传参。
        merged = merge_permission_sources(galaxy={}, local=local, full_access_wildcard=True)
        for reason in merged.skipped_reasons:
            # 通配角 v1 结构上不会在这条分支出现（`galaxy` 恒为空字典，不含
            # `ALL_COMPANIES_KEY`），保留同一姿态只是让两条分支的代码形状一致。
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
            # 既无银河也无本地授权（或本地授权已被同键抑制清空）：维持现行撤权
            # 语义不变，`_revoke` 一个字节都不改。
            self._revoke(tally, identity, aggregate.reason, now)
            return

        # 本地授权非空：管理员的兜底赋权生效，发布内容=合并结果（精确等于本地
        # 授权集合，因为 galaxy 一侧贡献为空）。
        self._enqueue_publish(tally, identity, merged.permissions, now)

    def _enqueue_publish(
        self,
        tally: _Tally,
        identity: ArchivedIdentity,
        company_metrics: Mapping[str, Sequence[str]],
        now: datetime,
    ) -> None:
        """结算并落一次授权发布决定，供 `_refresh_user`（银河授权路径）与
        `_refresh_zero_galaxy_user`（零银河 + 本地授权兜底路径）共用同一段收尾——
        两条路径殊途同归：都在四源合并之后拿到非空的 `company_metrics`，剩下的
        （只读令牌密文、结算发布行、落决定、计数、清送达正文）与"这份内容是从
        银河翻译来的还是纯本地授权来的"无关。
        """

        # 只取**已有**密文，取不到就是 None（发布层随后以 ``missing_token_cipher``
        # 失败关闭）。这里没有、也不允许有任何签发路径。
        token_cipher = self._token_ciphers.token_cipher(identity.app_user_id)
        row = build_translated_publish_row(
            company_metrics=company_metrics,
            email=identity.email,
            display_name=identity.display_name,
            decided_at=now,
            token_cipher=token_cipher,
        )
        try:
            decision = self._decisions.record_decision(
                user_id=identity.app_user_id,
                row=row,
                reason=PERMISSION_REFRESH_REASON,
                # Issue #483：这是一份**需要账号有效**的授权。基线读取到轮到这个人
                # 被处理之间，管理员可能刚把他停用并排空了权限——判据必须落在
                # ``record_decision`` 那把已经持有的行锁里（同一行、同一把锁 = 与
                # 停用写入串行），不是这里先查一次账号状态（那只会把窗口缩小）。
                require_enabled_account=True,
                decided_at=now,
                # 权限确实变化时，在 record_decision 自己的同一个事务里顺带清空该用户
                # 已送达、随会话保留的投递正文（S-P-5，Trace #328）。
                clear_delivered_content=True,
            )
        except PermissionGrantBlockedByAccountState as blocked:
            # **被挡是正确结果，不是故障**：``tally.failed`` 不加一（那一列是"处理这个
            # 人时抛了异常"，运维按它判断本轮健康度）。这个人本轮什么都没写——事务整体
            # 回滚，版本没推进、意图没入队；他的撤权由停用那一刻的即时撤销路径负责。
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
            # ``UNCHANGED``：权限内容与上一条仍然有效的意图逐字段相同。不推进版本、
            # 不排新意图、不清理——判定在 ``record_decision`` 里，本职责只如实计数。
            tally.unchanged += 1

    def _resolve_local_overrides(
        self, user_id: str, *, raise_on_failure: bool = False
    ) -> ResolvedLocalOverrides | None:
        """读该用户当前生效的本地覆盖条目并解决成 ``ResolvedLocalOverrides``。

        两种情形都返回 ``None``（对 :func:`merge_permission_sources` 恒等），但审计
        姿态不同：

        - **装配层没有接这个 store**（``self._local_overrides is None``）：部署事实，
          不告警——与「store 缺席=行为一致」的既有装配纪律同一姿态。
        - **读取失败**（数据库异常）：该用户本轮跳过本地源，**响亮**记一条
          ``permission_refresh.local_override_skipped``（``reason=local_override_read_failed``），
          异常本身不冒泡——一个用户的本地覆盖读取失败不得带走这个人当轮的银河权限
          发布，更不能带走整轮（`_refresh_user` 外层的 ``run_once`` 也兜底捕获单用户
          异常，这里提前捕获是为了把"翻译失败"与"本地覆盖读取失败"两种原因分开
          审计，而不是让两者都落进同一个笼统的 ``permission_refresh.user_failed``）。

        ``raise_on_failure``（``False`` 默认，P1-1 独立审查修复新增）：``False`` 时
        读取失败与"未装配"对调用方同样返回 ``None``——`_refresh_user` 银河已授权
        路径用这个默认值，理由是银河已经贡献了非空内容，本地源读取失败不改变
        "要不要发布"这件事本身，只是让合并少了本地这一份，行为与改动前逐字节
        一致。``True`` 时读取失败改为抛出 :class:`LocalOverrideReadError`——
        `_refresh_zero_galaxy_user` 用它：那条分支银河对合并的贡献恒为 ``{}``，
        本地源是否读到直接决定"发布还是撤权"，读取失败绝不能被无声折叠成"没有
        本地授权"进而触发撤权（同事务清已送达正文，不可逆）。两种情形都已经在
        这里记过同一条 ``local_override_skipped`` 审计，调用方不需要也不应该
        再重复记一条。
        """

        if self._local_overrides is None:
            return None
        try:
            entries = tuple(self._local_overrides.effective_entries(user_id=user_id))
        except Exception as error:  # noqa: BLE001 - 本地源读取失败只降级，不整轮/整人失败
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
        """「2.0 迁移导入·全部」组随当前映射补齐新指标（rc25 S-1 方案 E）：缺才补、
        同组 ID、撤销过的组不参与（只看生效条目）；补行成功后重读一次条目让本轮合并
        直接带上新行，补行或重读失败都只审计、不影响本轮既有结果。"""

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
            except Exception as error:  # noqa: BLE001 - 补行失败不影响本轮既有合并
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
        except Exception:  # noqa: BLE001 - 重读失败：新行下一轮自然生效
            return entries

    def _revoke(
        self,
        tally: _Tally,
        identity: ArchivedIdentity,
        reason: str,
        now: datetime,
    ) -> None:
        """银河侧明确判定这个人现在没有可用权限：该清空的清空，该跳过的跳过。

        三条出口按次序判，**每一条都显式返回**，不落到默认分支：

        1. **存档缺邮箱或姓名** → 跳过。撤权行的 ``record_key``/``email``/``name``
           三列同样来自存档身份，缺了就没有"这一行是谁的"的答案。这一步在查库之前，
           因为它不需要查库。
        2. **在发布链上一点足迹都没有**（既没发布成功过、也没有在途意图）→ 跳过
           （:data:`SKIP_NO_PUBLISHED_ROW`）。理由见模块文档「撤权」一节：不为一个
           没有发布行的人新建一行空权限。
        3. 否则结算撤权行并落一次权限决定。是否真的排出新意图由
           ``record_decision`` 的内容比对决定——第二天仍然无权限时判 ``UNCHANGED``，
           因此撤权**不会每天重发一次**。
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

        row = build_revocation_row(
            email=identity.email,
            display_name=identity.display_name,
            decided_at=now,
        )
        decision = self._decisions.record_decision(
            user_id=identity.app_user_id,
            row=row,
            reason=PERMISSION_REVOKE_REASON,
            # Issue #483：撤权**任何账号状态都必须放行**。挡住撤权 = 停用彻底失效，
            # 是本次修复方向相反的那个、后果最严重的错误——``force_revoke`` 的服务
            # 对象本来就是已停用用户。声明 ``False`` 时实现侧还会断言这一行确实是
            # 空权限撤权行，传错在运行期就写不出来。
            require_enabled_account=False,
            decided_at=now,
            # 权限确实变化（真的排出撤权意图）时，在 record_decision 自己的同一个
            # 事务里顺带清空该用户已送达、随会话保留的投递正文（S-P-5，Trace #328），
            # 与授权侧同一个开关、同一条理由。
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

    返回 ``(duty, metric_translation_map)``：第二个元素是本函数**唯一一次**读取
    ``lingxi/config/company_function_metric_map.toml`` 得到的对象（前置不齐、连
    读取都没发生时是 ``None``）。``build_loop`` 把它原样转给
    :func:`_build_onboarding_duty` 构造 ``publish_allowed``——**同一个已加载对象**，
    不在开通侧另开一次文件 I/O（见 Issue #227 开通侧整合的取舍说明，
    ``build_loop`` 内注入点上方的注释）。

    形状照 :func:`_build_roster_audit_duty`（`V-花名册-29` 的同一条纪律：缺项只报变量名、
    审计恰一条、其余职责照常运行）。前置有三个，逐个说明为什么它是真前置：

    1. **MCP 令牌主密钥**（``LINGXI_MCP_TOKEN_ENCRYPT_KEY``）。重算要读该用户**已有**的
       令牌密文，而唯一的读取口
       :class:`~lingxi.adapters.postgres_mcp_token.PostgresMcpTokenStore` 只接受已经校验
       过主密钥的加解密对象（它同时承载解密路径，构造时就要求密钥）。**没有它就没有令牌
       读取口**，而"读不到"与"这个人没有令牌"在下游是同一个 ``None``——那会让每个需要
       新建发布行的人都以 ``missing_token_cipher`` 失败关闭，表现成"接线了但一直失败"，
       正是 R3 那条注释要避免的伪装。因此这里显式不注册并留痕。

       **本职责一次都不解密、也不签发**：密钥在这里只用于构造那个读取口
       （见 :mod:`lingxi.apps.scheduler.permission_refresh` 的模块文档）。
    2. **角色职能映射配置**。它随包发布（``lingxi/config/galaxy_role_function_map.toml``）。
       读不出来时**不能**退化成空映射——那会让所有角色变成"未映射"，于是全员被算成无可用
       权限，是一种看起来正常的失败（``role_function_map_file`` 的模块文档同一条理由）。
    3. **公司+职能→指标名翻译映射配置**（Issue #227）。它同样随包发布
       （``lingxi/config/company_function_metric_map.toml``），**文件读不出来或格式不对**
       才不注册——**空映射本身是合法内容**（``[companies]`` 表存在但没有条目，代表映射
       内容尚未由产品负责人填入），不是一种要拒绝注册的前置缺失：职责本该正常跑起来，
       只是每个人都会在翻译那一步 fail-closed 并跳过（模块文档「翻译」一节），这与"配置
       文件本身损坏"是两件不同的事，必须分开判断——前者是"内容还没到"，后者是"部署配置
       本身有问题"，把两者混在一起会让"运维发现配置文件语法错了"和"产品负责人还没填映射"
       表现成同一种"职责不注册"，无从分辨该找谁。

    数据库连接串是必需配置，进程起得来就一定有，因此它不构成一个能变红的前置判定；
    职责真正的运行前置（花名册今天更新过、银河有当前有效批次）是**数据**而不是配置，
    由 ``run_once`` 每轮重新判定。
    """

    if not config.mcp_token_encrypt_key:
        from lingxi.adapters.mcp_token_cipher import MASTER_KEY_ENV

        # 只报变量名，不回显任何值（`V-花名册-29` 的同一条纪律；它还是一把主密钥）。
        audit.record(
            "permission_refresh.duty_not_registered",
            reason="missing_environment_variable",
            variable=MASTER_KEY_ENV,
        )
        logger.warning("未配置 %s，每日权限重算职责不注册；其余定时职责照常运行", MASTER_KEY_ENV)
        return None, None

    from lingxi.adapters.company_function_metric_map_file import (
        load_company_function_metric_map,
    )
    from lingxi.adapters.mcp_token_cipher import McpTokenCipher
    from lingxi.adapters.postgres_galaxy_snapshot import PostgresGalaxySnapshotReader
    from lingxi.adapters.postgres_mcp_token import PostgresMcpTokenStore
    from lingxi.adapters.postgres_permission_publish import (
        PostgresPermissionPublishStore,
        PostgresPermissionRefreshBaselineReader,
    )
    from lingxi.adapters.postgres_roster_snapshot import PostgresRosterSnapshotStore
    from lingxi.adapters.role_function_map_file import load_role_function_map

    try:
        role_function_map = load_role_function_map()
    except (OSError, ValueError) as error:
        # 只记异常类型：配置解析失败的正文可能带上文件内容片段。
        audit.record(
            "permission_refresh.duty_not_registered",
            reason="role_function_map_unavailable",
            error=type(error).__name__,
        )
        logger.error(
            "角色职能映射配置不可用，每日权限重算职责不注册 error=%s", type(error).__name__
        )
        return None, None

    try:
        metric_translation_map = load_company_function_metric_map(config.metric_map_path)
    except (OSError, ValueError) as error:
        # 同上：只记异常类型。**空映射不会走到这里**——它是合法内容，解析成功即返回；这里挡的是文件缺失或格式不对，二者都是部署配置问题，不是"内容还没填"。
        audit.record(
            "permission_refresh.duty_not_registered",
            reason="metric_translation_map_unavailable",
            error=type(error).__name__,
        )
        logger.error(
            "公司+职能→指标名翻译映射配置不可用，每日权限重算职责不注册 error=%s",
            type(error).__name__,
        )
        return None, None

    publish_store = PostgresPermissionPublishStore(
        config.postgres_dsn, timeouts=config.postgres_timeouts
    )
    duty = PermissionRefreshDuty(
        baseline_reader=PostgresPermissionRefreshBaselineReader(
            config.postgres_dsn, timeouts=config.postgres_timeouts
        ),
        roster_snapshot=PostgresRosterSnapshotStore(
            config.postgres_dsn, timeouts=config.postgres_timeouts
        ),
        galaxy=PostgresGalaxySnapshotReader(config.postgres_dsn, timeouts=config.postgres_timeouts),
        decisions=publish_store,
        # 同一个存储对象喂两个端口：一个只写权限决定，一个只读"发布过没有"。分成两个
        # 参数是为了让撤权那条判据在类型上说得清楚（见 permission_refresh 的两个协议）。
        publish_history=publish_store,
        token_ciphers=PostgresMcpTokenStore(
            config.postgres_dsn,
            cipher=McpTokenCipher(config.mcp_token_encrypt_key),
            timeouts=config.postgres_timeouts,
        ),
        role_function_map=role_function_map,
        metric_translation_map=metric_translation_map,
        audit=audit,
        stop=stop,
        local_overrides=local_override_reader(
            config.postgres_dsn, timeouts=config.postgres_timeouts
        ),
        legacy_all_scope=PostgresLocalPermissionOverrideStore(
            config.postgres_dsn, timeouts=config.postgres_timeouts
        ),
    )
    return duty, metric_translation_map


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
]
