"""每日权限重算职责的端口协议、原因码与报告形状。

只放形状与取值域，不放判定：判定住在
:mod:`~lingxi.apps.scheduler.permission_refresh`。

审计与报告**只记计数与分类原因码**，不记姓名、邮箱、权限内容或任何外部标识原值。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Protocol

from lingxi.core.identity.roster_audit import ArchivedIdentity
from lingxi.core.identity.roster_snapshot import StoredSnapshotFacts
from lingxi.core.permission.local_override import LocalPermissionOverrideEntry
from lingxi.core.permission.merge_sources import REASON_LOCAL_OVERRIDE_READ_FAILED

_UTC = UTC


class LocalOverrideReadError(RuntimeError):
    """本地覆盖读取失败的内部标记：本轮跳过这个人，不落撤权、不清已送达正文。

    银河贡献恒为空的那条分支上，读取失败**不能**被无声折叠成"没有本地授权"——那会把
    一次读故障说成一次撤权。
    """


#: 写进发布意图 ``reason`` 列的原因码。它回答"这条意图是谁排的"，与首次开通那条
#: （``first_onboarding``）区分开，让运维能一眼看出某次外部写入来自每日刷新。
PERMISSION_REFRESH_REASON = "daily_permission_refresh"

#: 撤权更新那一条意图的 ``reason``。与授权刷新分开是为了让"这次外部写入是去清空权限的"
#: 在 outbox 里一眼可辨——两者的 payload 差别只有 ``permissions`` 一列的内容，
#: 靠肉眼比对 JSON 文本来分辨一次不可逆的外部写入，不是可接受的运维姿态。
PERMISSION_REVOKE_REASON = "daily_permission_revoke"

#: 银河这一侧原本有效授权，但本地抑制把翻译结果压光到空字典时的撤权原因码
#: （红线-2，Trace #328 opus 审查）。与匹配失败、聚合层 fail-closed 的三个原因码
#: （``no_galaxy_roles``/``no_supported_function``/``no_company_scope``）区分开，
#: 让审计能一眼看出"这个人是被本地抑制清空的"，不是"银河本来就没给他权限"。
REASON_FULLY_SUPPRESSED = "fully_suppressed"

#: ``permission_refresh.delivered_content_cleared`` 审计的 ``trigger`` 字段取值
#: （S-P-5 措辞如实化，Trace #328 opus 审查）：区分这次清理是授权路径（含全抑制
#: 撤权，见 :data:`REASON_FULLY_SUPPRESSED`）还是银河撤权路径触发的，供运维排查
#: 时不必回头核对 ``reason`` 列才能分辨。
TRIGGER_GRANT = "grant"
TRIGGER_REVOKE = "revoke"


SKIP_LOCAL_OVERRIDE_READ_FAILED = REASON_LOCAL_OVERRIDE_READ_FAILED

# ---- 跳过原因码。全部是**固定字面量**，不含任何字段值 -----------------------
#: 花名册快照压根不存在。
SKIP_MISSING_SNAPSHOT = "missing_snapshot"
#: 花名册快照存在，但不是今天取的。
SKIP_STALE_SNAPSHOT = "stale_snapshot"
#: 没有当前有效的银河批次。
SKIP_NO_GALAXY_BATCH = "no_galaxy_batch"
#: 已开通用户的存档里没有人员 ID：匹配链的第一环就断了。
SKIP_MISSING_PERSONNEL_ID = "missing_personnel_id"
#: 已开通用户的存档缺邮箱或姓名：发布行的 ``record_key``/``name`` 两列没有来源。
SKIP_ARCHIVED_IDENTITY_INCOMPLETE = "archived_identity_incomplete"
#: 撤权用户在发布表里**没有我们发布过的行**：不为他新建空权限行（模块文档「撤权」一节）。
SKIP_NO_PUBLISHED_ROW = "no_published_row"
#: 「公司 + 职能 → 指标名」翻译层**整份映射一个条目都没有**（Issue #227）：产品负责人
#: 还没有开始填内容。当前部署下随包发布的配置文件正是这个状态，因此**每一个有效授权
#: 用户**都会落在这个原因码上——这是刻意的硬闸，见模块文档「翻译」一节。与
#: :data:`SKIP_METRIC_TRANSLATION_UNCOVERED` 分开登记，是为了让运维能从审计上分辨
#: 「整体还没开始填」与「已经在填、只是差几条」这两种截然不同的状态。
SKIP_METRIC_TRANSLATION_UNAVAILABLE = "metric_translation_unavailable"
#: 「公司 + 职能 → 指标名」翻译层**有内容，但这一次要用的组合没被覆盖**（Issue #227）：
#: 不发布、不撤权，只跳过。
SKIP_METRIC_TRANSLATION_UNCOVERED = "metric_translation_uncovered"
#: 落授权决定的那把行锁里发现这个人已经不是 ``enabled``（Issue #483）：本轮基线读到
#: 他的那一刻他还有效，轮到处理他的这一刻管理员已经把他停用了。**这不是故障**——
#: ``report.failed`` 不加一，撤权那一侧也不受影响（撤权声明"不要求账号有效"）；它是
#: 「停用」承诺被正确兑现的证据，因此单独登记一个原因码 + 一条专属审计动作，让运维能
#: 从审计上看出"今天真的发生过一次这样的交错"。上线后第一轮批处理应当回看这条计数，
#: 正常为 0。
SKIP_ACCOUNT_NOT_ENABLED = "account_not_enabled"
#: 本地「全部」组（rc25 S-1）下某公司被本地抑制减到空：读侧回退制无法表示，本轮既不
#: 发布也不撤权（`merge_sources.py` 「本地 "*" 组」一节；要完全屏蔽该公司先撤组）。
SKIP_SUPPRESSION_UNREPRESENTABLE = "suppression_on_all_scope_unrepresentable"

#: 逐用户结果的四个分类。``granted`` 之外的三类都**不产生任何发布意图**。
STAGE_MATCH = "match"
STAGE_AGGREGATE = "aggregate"
STAGE_IDENTITY = "identity"
STAGE_TRANSLATE = "translate"

#: 整轮跳过的原因码 → 审计动作名。外部独立审查 2026-08-18 坐实的 P1：翻译层整体
#: 不可用（映射为空）必须让**整轮**一条发布意图都不排，包括撤权——判据不是"这一行
#: 要不要翻译"，是"发布面这一轮开不开"。因此它和花名册/银河两组前置判据同属**整轮**
#: 跳过，不是逐用户判据，与 :data:`SKIP_METRIC_TRANSLATION_UNCOVERED`（映射非空但
#: 某个组合没覆盖到，仍是逐用户判据，见 :meth:`PermissionRefreshDuty._refresh_user`）
#: 是两回事，动作名也刻意不同，以便与"配了但未覆盖"在审计上区分开。
_ROUND_SKIP_ACTIONS: Mapping[str, str] = {
    SKIP_NO_GALAXY_BATCH: "permission_refresh.skipped_no_galaxy_batch",
    SKIP_MISSING_SNAPSHOT: "permission_refresh.skipped_roster_not_fresh",
    SKIP_STALE_SNAPSHOT: "permission_refresh.skipped_roster_not_fresh",
    SKIP_METRIC_TRANSLATION_UNAVAILABLE: "permission_refresh.skipped_metric_translation_unavailable",
}


def _utc_date(moment: datetime) -> date:
    """把一个时刻折成**它所在的 UTC 日期**。

    全模块只有这一处做「时刻 → 日期」的转换，理由是 ``date()`` 会直接取时钟自身时区的
    日期：一个 ``+08:00`` 的时钟在 ``00:30`` 给出的 ``date()`` 已经是新的一天，而按
    UTC 那还是前一天。日界不统一会让"今天已经跑过"与"快照是不是今天的"用两把不同的
    尺子，表现为某些时段整天不重算、或同一天重算两轮。日期一律 UTC（接口设计
    「二、通用约定」，与 :class:`~lingxi.apps.scheduler.RosterAuditDuty` 的日报日期同口径）。

    naive 时间**直接失败**而不是按本地时区解读：那种解读会在跨时区部署上静默算错，
    而算错的方向不可预测。
    """

    if not isinstance(moment, datetime):
        raise ValueError("权限重算的时间必须是时间戳")
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("权限重算的时间必须带时区：日界一律按 UTC 判定")
    return moment.astimezone(_UTC).date()


class _AuditSink(Protocol):
    """审计出口。

    与 :class:`lingxi.apps.scheduler.AuditSink` 是同一个结构化签名，在这里单独写一份
    只是为了避免与装配模块相互 import；两者互相满足，装配时传的就是同一个对象。
    """

    def record(self, action: str, /, **fields: object) -> None: ...


class _BaselineReader(Protocol):
    """已开通用户的读取口。实现是
    :class:`lingxi.adapters.postgres_permission_publish.
    PostgresPermissionRefreshBaselineReader`（Issue #468，2026-08-30 之前是
    :class:`lingxi.adapters.postgres_roster_audit.PostgresRosterBaselineReader`）。

    **两份实现故意不再共用**：本职责需要的口径与花名册审计（日报/审计对比）用的
    ``PostgresRosterBaselineReader.load_active_baseline()`` 曾经逐字节相同
    （``provisioning_state='active'`` 且 ``account_state NOT IN
    ('deleting','deleted')``，`V-花名册-10`、`V-花名册-11`），直到 Issue #468 坐实：
    管理员停用（``account_state='suspended'``）某用户之后，这条共用的过滤没有排除
    ``suspended``，于是次日这里仍会把这个人算进遍历集合——银河与花名册都不知道
    "停用"这件事，照常聚合出他的有效权限并重新发布，管理员的停用承诺在数据库层面
    被这一条批处理静默突破。修复是让**本职责专用**的
    :class:`PostgresPermissionRefreshBaselineReader` 额外排除 ``suspended``，
    花名册审计那份基线保持不变（它仍然需要覆盖停用期间的花名册字段漂移，这是
    另一个产品判据，见该类文档），两个调用方从此各自独立演进各自的过滤条件，
    不再假设"看起来一样"就可以共用同一条 SQL。
    """

    def load_active_baseline(self) -> Sequence[ArchivedIdentity]: ...


class _RosterSnapshotStore(Protocol):
    """花名册持久快照的读取口（:mod:`lingxi.adapters.postgres_roster_snapshot`）。

    先读 :meth:`load_facts` 再读 :meth:`load` 是刻意的：顺序判据在当前部署下每轮都
    不成立，而元信息只有一行——用整份快照（一千两百多行）去做一次每分钟都要做的
    新鲜度判断，代价与结论完全不成比例。
    """

    def load_facts(self) -> StoredSnapshotFacts | None: ...
    def load(self) -> Any: ...


class _GalaxySnapshotReader(Protocol):
    def load_current(self) -> Any: ...


class _TokenCipherReader(Protocol):
    """令牌**密文**的只读口。

    这里刻意只声明 :meth:`token_cipher` 一个方法：签发口
    （``issue_token``）不在这个协议里，因此"在每日刷新里顺手签一份令牌"这件事，
    在类型上就写不出来（模块文档「令牌：只读既有，绝不签发」）。
    """

    def token_cipher(self, user_id: str) -> str | None: ...


class _PublishHistory(Protocol):
    """「这个人在发布链上有没有留下过足迹」的只读口
    （:meth:`~lingxi.adapters.postgres_permission_publish.
    PostgresPermissionPublishStore.has_publish_footprint`）。

    单独声明成一个只有一个方法的协议，理由与 :class:`_TokenCipherReader` 相同：
    撤权那一路只需要回答"有没有"，把整个发布 outbox 的写侧摆在这里，等于让"顺手改一下
    那条意图"在类型上变得可写。装配时传进来的确实是同一个存储对象。
    """

    def has_publish_footprint(self, user_id: str) -> bool: ...


class _Decision(Protocol):
    enqueued: bool
    # 本次决定顺带清掉的已送达、随会话保留投递正文事件数（S-P-5，
    # Trace #328）。只有调用 ``record_decision(clear_delivered_content=True)`` 且
    # 真的走到 ``ENQUEUED`` 时才可能非零；本职责只如实把它写进审计计数，不自己
    # 判断"要不要清"——那条判定连同事务边界只有 ``record_decision`` 一处实现。
    cleared_events: int


class _DecisionStore(Protocol):
    """权限决定的落库口（:meth:`~lingxi.adapters.postgres_permission_publish.
    PostgresPermissionPublishStore.record_decision`）。

    **版本推进与幂等完全由它承担**：本职责不读、不写、不比较 ``permission_version``，
    也不自己判断"这次权限有没有变化"。那条判定连同它的锁与事务边界只有一处实现。

    ``require_enabled_account`` 是**必填**关键字参数（Issue #483，与
    ``merge_permission_sources(full_access_wildcard=...)`` 同一条结构性防复发纪律）：
    授权侧传 ``True``、撤权侧传 ``False``。账号状态复检落在实现那把**已经持有的**
    ``app_user`` 行锁里，本职责一个字都不复制——它只负责在被挡时把结果翻译成自己的
    计数与审计（:data:`SKIP_ACCOUNT_NOT_ENABLED`）。
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


class _LegacyAllScopeExpander(Protocol):
    """「2.0 迁移导入·全部」组的补行口（rc25 S-1 方案 E）：新指标进入映射后，每日重算
    把 ``missing_all_scope_metrics`` 算出的缺项按同组 ID 追加成行。实现见
    ``adapters/postgres_local_permission.PostgresLocalPermissionOverrideStore.
    expand_all_scope_group``；``None``＝未装配，行为与接线前逐字节一致。"""

    def expand_all_scope_group(
        self, *, user_id: str, group_id: str, metrics: Sequence[str], now: datetime
    ) -> int: ...


class _LocalOverrideReader(Protocol):
    """本地权限覆盖的按用户读取口（S-P-3，Issue #319）。

    实现是 :meth:`~lingxi.adapters.postgres_local_permission.
    PostgresLocalPermissionOverrideStore.effective_entries` 经装配层适配（返回值从
    ``StoredLocalPermissionOverride`` 解出 ``.entry``——本协议只认纯类型
    :class:`~lingxi.core.permission.local_override.LocalPermissionOverrideEntry`，
    不认数据库分配的行标识，理由与 :class:`_PublishHistory` 只声明一个方法相同：
    本职责只需要"这个用户当前生效的覆盖条目有哪些"，不需要收回单条覆盖的能力）。

    ``None``（装配层未装配）与本方法读取失败在调用方眼里是**不同**的两件事：前者
    静默按"没有本地源"处理（部署事实，不告警）；后者响亮审计
    （:data:`~lingxi.core.permission.merge_sources.REASON_LOCAL_OVERRIDE_READ_FAILED`），
    但结果都是"这一轮/这个用户跳过本地源"——不整轮失败、不静默吞掉异常。
    """

    def effective_entries(self, *, user_id: str) -> Sequence[LocalPermissionOverrideEntry]: ...


@dataclass(frozen=True)
class PermissionRefreshReport:
    """一轮重算的结果。**只有计数与固定原因码，没有任何字段值。**

    :attr:`reasons` 的键全部来自 :mod:`lingxi.core.permission` 的固定原因码
    （``roster_not_found``、``no_supported_function`` 一类）与本模块顶部的常量，
    它们描述的是**为什么**，不是**是谁**——邮箱、姓名、工号、银河账号、公司编号与
    职能标签一个都不在这里（纪律同 `V-花名册-33`、`V-银河-13`）。
    """

    examined: int = 0
    enqueued: int = 0
    unchanged: int = 0
    # 本轮判定为"无可用权限"的人数（匹配失败也算在内：它是无权限的一种）。
    revoked: int = 0
    # 其中**真的排出了一条撤权更新意图**的人数（`V-权限-08` 的刷新侧）。它一定
    # ≤ :attr:`revoked`：匹配失败、从来没发布过、存档不全的那些人都只跳过。
    revoked_published: int = 0
    # 输入不完整而被跳过的人数：存档缺人员 ID / 缺邮箱或姓名。
    incomplete: int = 0
    # 处理过程中抛异常的人数。一个人的异常不影响其余人（模块文档）。
    failed: int = 0
    # 收到停止信号而中断：这一轮没走完，水位不置位。
    interrupted: bool = False
    reasons: Mapping[str, int] = field(default_factory=dict)

    @property
    def processed(self) -> int:
        """真正走完整条链并落了一次权限决定的人数。"""

        return self.enqueued + self.unchanged

    def audit_facts(self) -> dict[str, Any]:
        """可直接进审计与日志的事实。键与值都不含任何人员资料。"""

        facts: dict[str, Any] = {
            "examined": self.examined,
            "processed": self.processed,
            "enqueued": self.enqueued,
            "unchanged": self.unchanged,
            "revoked": self.revoked,
            "revoked_published": self.revoked_published,
            "incomplete": self.incomplete,
            "failed": self.failed,
        }
        if self.interrupted:
            facts["interrupted"] = True
        # 原因分类逐项展开成 `reason.<码>=<计数>`：审计行会被 grep 与比对，
        # 一个嵌套字典在结构化日志里只会变成一串引号。
        for reason, count in sorted(self.reasons.items()):
            facts[f"reason.{reason}"] = count
        return facts


@dataclass
class _Tally:
    """累加器。:class:`PermissionRefreshReport` 是冻结的（它会进审计），
    因此计数在这里累加，最后一次性结算成不可变的报告。"""

    examined: int = 0
    enqueued: int = 0
    unchanged: int = 0
    revoked: int = 0
    revoked_published: int = 0
    incomplete: int = 0
    failed: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def count(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def freeze(self, *, interrupted: bool = False) -> PermissionRefreshReport:
        return PermissionRefreshReport(
            examined=self.examined,
            enqueued=self.enqueued,
            unchanged=self.unchanged,
            revoked=self.revoked,
            revoked_published=self.revoked_published,
            incomplete=self.incomplete,
            failed=self.failed,
            interrupted=interrupted,
            reasons=dict(self.reasons),
        )
