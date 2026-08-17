"""每日权限重算职责（Issue [#156](https://github.com/Moshuiwang/lingxi/issues/156) 的 S-C-03a）。

一轮做的事，次序不可调换：

1. **顺序判据**：花名册持久快照的 ``captured_at`` 必须是**今天**（UTC 日界）。不是就
   整轮不跑，只留一条审计——**不触银河、不产生任何发布意图**；
2. 读当前有效批次的银河快照（:mod:`lingxi.adapters.postgres_galaxy_snapshot`）；
   没有有效批次同样整轮不跑，审计可分辨；
3. 遍历**已开通**用户（``provisioning_state='active'`` 且 ``account_state NOT IN
   ('deleting','deleted')``，过滤写在 SQL 里，与花名册审计同一口径），逐人：
   花名册身份 → 银河账号匹配 → 聚合当前有效权限 → 结算发布行 → 记一次权限决定；
4. 记一条只含计数的职责报告审计。

## 为什么顺序判据是「花名册今天更新过」而不是一个开关

合同要求每日刷新**严格先刷新花名册、再刷新银河快照**（`V-权限-07`）。「先」如果只靠
职责在列表里的位置来保证，那么花名册那一轮失败、或者压根没注册时，权限重算照样会跑
——用的是几天前的花名册。那不是"顺序对了"，那是"顺序看起来对了"。因此这里把顺序变成
一条**数据判据**：只有当库里那份花名册快照是今天取的，才允许重算。

**这条判据在当前部署下恒为假，这是刻意的失败关闭，不是缺陷。** 花名册读取所用的短期
令牌供给尚未接线（登记见 [#215](https://github.com/Moshuiwang/lingxi/issues/215)），
因此花名册审计日报职责不注册、``roster_snapshot`` 表恒空，本职责每轮都会在第一步停下
并留一条 ``permission_refresh.skipped_roster_not_fresh``。**不提供任何旁路开关**：
一个"允许用旧花名册重算"的环境变量，会在第一次运维着急时被打开，然后再也不会被关上。

## 撤权：本切片只跳过，不实现产品语义

聚合结果 ``not granted``（银河账号匹配不上、角色全部未映射、解释不出公司范围、银河
记录不完整……）时，本职责**跳过该用户**：不发布、不通知、不删除发布表里已有的行，
只按可分辨的原因分类记一条审计并计数。

**"权限被收回时那一行发布行该怎么处置"属 `V-权限-08` 的刷新侧语义，尚待产品负责人
裁定**（登记在 Trace [#203](https://github.com/Moshuiwang/lingxi/issues/203)）。可选项
至少有三种——把 ``status`` 改成停用、把 ``permissions`` 清空、整行删除——它们对既有
26 行旧系统记录的影响各不相同，且都不可逆。在裁定之前，**什么都不做**是唯一不会造成
不可恢复外部副作用的选择。

## 令牌：只读既有，绝不签发

需要新建发布行的用户，其 ``token_cipher`` 只取该用户**已经登记在令牌表里**的那一份
（:meth:`lingxi.adapters.postgres_mcp_token.PostgresMcpTokenStore.token_cipher`，只取
密文、不解密）。取不到就传 ``None``，随后由发布执行器以 ``missing_token_cipher``
失败关闭——**这正是预期结果，不绕过**。

**本职责一次都不签发令牌。** 「存量用户的令牌归属」（那 26 行是旧系统 biai-agent 签发
的，我们不知其明文）同样是 [#203](https://github.com/Moshuiwang/lingxi/issues/203) 待
裁定的产品结；在每日刷新里顺手给他们签一份我方令牌，等于替产品负责人做了那个决定，
而且方向不可逆——密文一旦写进外部表，那 26 个人下一次问数就会失败。

## 幂等与水位

同日至多一轮，靠 :attr:`~PermissionRefreshDuty.completed_on` 这个**进程内**的 UTC 日期
水位。跨重启不幂等（新进程水位为空，当天会再跑一轮），这与
:class:`~lingxi.apps.scheduler.RosterAuditDuty` 是同一条已知情接受的残留（迁移
``0063`` 的注释与产品负责人 2026-08-06 的 C2/R2 裁定）。差别是本职责的重跑**在产品上
真正幂等**：权限内容没变时 :meth:`~lingxi.adapters.postgres_permission_publish.
PostgresPermissionPublishStore.record_decision` 判 ``UNCHANGED``，不推进版本、不排新
意图，因此重跑既不会重复发布，也不会让消费方看到任何变化。

**水位在一轮走完之后置位，即使这一轮里有个别用户失败。** 单个用户的失败已经逐条留痕
并计入报告，而"有失败就整轮重来"会让一次持续的数据库故障变成每分钟重跑一遍全员——
那既救不了那个用户，又会把其余职责的时间预算吃掉。失败的用户等下一天那一轮，这正是
"每日刷新"的语义。收到停止信号而中断的那一轮**不置位**：它没走完。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Protocol

from lingxi.core.identity.roster_audit import ArchivedIdentity
from lingxi.core.identity.roster_snapshot import StoredSnapshotFacts
from lingxi.core.permission.account_match import MATCHED, match_galaxy_account
from lingxi.core.permission.publish_row import aggregate_permission, build_publish_row

logger = logging.getLogger(__name__)

_UTC = timezone.utc

#: 写进发布意图 ``reason`` 列的原因码。它回答"这条意图是谁排的"，与首次开通那条
#: （``first_onboarding``）区分开，让运维能一眼看出某次外部写入来自每日刷新。
PERMISSION_REFRESH_REASON = "daily_permission_refresh"

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

#: 逐用户结果的三个分类。``granted`` 之外的两类都**不产生任何发布意图**。
STAGE_MATCH = "match"
STAGE_AGGREGATE = "aggregate"
STAGE_IDENTITY = "identity"


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
    :class:`lingxi.adapters.postgres_roster_audit.PostgresRosterBaselineReader`——
    **复用**而不是照抄它的 SQL：``provisioning_state``/``account_state`` 这两条过滤
    是产品口径（`V-花名册-10`、`V-花名册-11`），第二份实现迟早会与它分叉，而分叉的
    方向可能是"给一个正在删除的用户重新发了权限"。
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


class _Decision(Protocol):
    enqueued: bool


class _DecisionStore(Protocol):
    """权限决定的落库口（:meth:`~lingxi.adapters.postgres_permission_publish.
    PostgresPermissionPublishStore.record_decision`）。

    **版本推进与幂等完全由它承担**：本职责不读、不写、不比较 ``permission_version``，
    也不自己判断"这次权限有没有变化"。那条判定连同它的锁与事务边界只有一处实现。
    """

    def record_decision(
        self, *, user_id: str, row: Any, reason: str, decided_at: datetime
    ) -> _Decision: ...


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
    # 聚合结果为"无可用权限"而被跳过的人数（匹配失败也算在内：它是无权限的一种）。
    revoked: int = 0
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
            incomplete=self.incomplete,
            failed=self.failed,
            interrupted=interrupted,
            reasons=dict(self.reasons),
        )


class PermissionRefreshDuty:
    """每日权限重算：花名册新鲜 → 银河当前批次 → 逐个已开通用户重算并排发布意图。

    语义与边界见模块文档。本类**只编排**：匹配、聚合、发布行结算三条规则分别在
    :mod:`lingxi.core.permission.account_match`、
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
        token_ciphers: _TokenCipherReader,
        role_function_map: Mapping[str, str],
        audit: _AuditSink,
        clock: Callable[[], datetime] | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        self._baseline_reader = baseline_reader
        self._roster_snapshot = roster_snapshot
        self._galaxy = galaxy
        self._decisions = decisions
        self._token_ciphers = token_ciphers
        self._role_function_map = role_function_map
        self._audit = audit
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
                today, SKIP_STALE_SNAPSHOT, snapshot_date=_utc_date(snapshot.facts.captured_at).isoformat()
            )
            return None

        # 顺序判据成立之后才碰银河：花名册不新鲜的那一轮**一次银河读取都不发起**。
        galaxy = self._galaxy.load_current()
        if galaxy is None:
            self._audit_skip(today, SKIP_NO_GALAXY_BATCH)
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
            "输入不完整=%s 失败=%s",
            report.examined,
            report.enqueued,
            report.unchanged,
            report.revoked,
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
            # 撤权侧：**不发布、不通知、不删行**（模块文档「撤权」一节）。
            self._skip(tally, identity, STAGE_AGGREGATE, aggregate.reason, revoked=True)
            return

        if not identity.email or not identity.display_name:
            # 发布行的 ``record_key``/``email``/``name`` 三列都来自存档身份。缺了就
            # 没有"这一行是谁的"的答案——先归类，而不是让 ``build_publish_row`` 抛错
            # 之后被当成一次技术故障。
            self._skip(tally, identity, STAGE_IDENTITY, SKIP_ARCHIVED_IDENTITY_INCOMPLETE, revoked=False)
            return

        # 只取**已有**密文，取不到就是 None（发布层随后以 ``missing_token_cipher``
        # 失败关闭）。这里没有、也不允许有任何签发路径。
        token_cipher = self._token_ciphers.token_cipher(identity.app_user_id)
        row = build_publish_row(
            aggregate=aggregate,
            email=identity.email,
            display_name=identity.display_name,
            decided_at=now,
            token_cipher=token_cipher,
        )
        decision = self._decisions.record_decision(
            user_id=identity.app_user_id,
            row=row,
            reason=PERMISSION_REFRESH_REASON,
            decided_at=now,
        )
        if decision.enqueued:
            tally.enqueued += 1
        else:
            # ``UNCHANGED``：权限内容与上一条仍然有效的意图逐字段相同。不推进版本、
            # 不排新意图——判定在 ``record_decision`` 里，本职责只如实计数。
            tally.unchanged += 1

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
            self._skip_audited if self._skip_audited is not None and self._skip_audited[0] == today
            else (today, set())
        )
        if reason in reasons:
            return
        reasons.add(reason)
        self._skip_audited = (day, reasons)
        action = (
            "permission_refresh.skipped_no_galaxy_batch"
            if reason == SKIP_NO_GALAXY_BATCH
            else "permission_refresh.skipped_roster_not_fresh"
        )
        self._audit.record(action, report_date=today.isoformat(), reason=reason, **facts)
        logger.warning("每日权限重算本轮不执行 reason=%s", reason)


__all__ = [
    "PERMISSION_REFRESH_REASON",
    "PermissionRefreshDuty",
    "PermissionRefreshReport",
]
