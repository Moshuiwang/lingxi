"""花名册资料比对与管理群审计日报职责，以及与它互斥的写侧职责。

:class:`RosterAuditDuty` 与 :class:`RosterSnapshotSyncDuty`：同日至多一次、空
差异不发、快照保旧与超龄告警等规则的完整理由见该类自己的文档
字符串。`roster_snapshot` 表是首次开通链第二步的硬前提，而"写快照"此前被绑在
`RosterAuditDuty` 内部——它的前置里含管理群 chat_id，一个纯粹服务"日报发到哪个
群"的通知配置，没配管理群会让快照永远不写、员工永远开通不了。
`RosterSnapshotSyncDuty` 是解出来的"只写不发"那一半：只依赖花名册 Base 坐标与
令牌供给，与管理群配置无关。两个职责在装配层**互斥**：管理群与 Base 坐标都齐全
时，`RosterAuditDuty` 自己内部完成"读一轮→写快照→立刻比对"；`RosterAuditDuty`
因任何原因未装配时，才尝试单独装配 `RosterSnapshotSyncDuty`。这样任何时刻至多
只有一个职责在触发花名册读取，一次性 `refresh_token` 的唯一消费者纪律因此不受
影响（见 `core/identity/access_token_supply.py`）。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from typing import Any, Protocol

from lingxi.apps.scheduler.audit import AuditSink
from lingxi.apps.scheduler.config import SchedulerConfig
from lingxi.core.identity.roster_audit import ArchivedIdentity, RosterAuditReport, compare_roster
from lingxi.core.identity.roster_report import render_daily_report_content
from lingxi.core.identity.roster_snapshot import (
    DailyRosterSource,
    RosterRound,
    RosterSnapshotUpdater,
    SnapshotDecision,
)

logger = logging.getLogger(__name__)


class _BaselineReader(Protocol):
    def load_active_baseline(self) -> Sequence[ArchivedIdentity]: ...


class _GroupSender(Protocol):
    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None: ...


class _RosterSource(Protocol):
    """一轮花名册取用：交出用于比对的行与快照状态。

    实现是 :class:`lingxi.core.identity.roster_snapshot.DailyRosterSource`；职责只依赖
    这一个方法，因此全部调度与日报断言都能在没有数据库、没有网络的机器上跑完。
    """

    def current(self, *, now: datetime) -> RosterRound: ...


class RosterAuditDuty:
    """每日花名册资料比对与管理群审计日报。

    一轮做四件事：取本轮花名册（读一轮 + 更新或保留持久快照）→ 读比对基线 →
    纯函数比对 → 该发就发一条日报。花名册排在基线之前：快照写入是首次开通链
    的硬前提。空差异日不发，但**快照超龄**或**完全没有可用快照**（这一天不
    比对，避免把全体已开通用户误报「查无此人」）时沉默是最危险的输出，仍需
    发一条。日报正文受控管理群可含存档身份定位信息，日志与审计不随之放宽。
    判重靠进程内 ``_completed_on`` 水位（同日至多一次，无持久载体，跨重启
    会重发内容相同的日报）；同一天的重试共用一个去重键，交由飞书服务端去重。
    """

    name = "花名册审计日报"

    def __init__(
        self,
        *,
        baseline_reader: _BaselineReader,
        roster_source: _RosterSource,
        sender: _GroupSender,
        audit: AuditSink,
        chat_id: str,
        clock: Callable[[], datetime] | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        self._baseline_reader = baseline_reader
        self._roster_source = roster_source
        self._sender = sender
        self._audit = audit
        self._chat_id = chat_id
        # 时钟注入：跨轮判重的用例要能自己决定「今天」是哪天，不能靠等到明天。
        self._clock = clock or (lambda: datetime.now(UTC))
        self._stop = threading.Event() if stop is None else stop
        self._completed_on: date | None = None

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    @property
    def completed_on(self) -> date | None:
        """已完成日报的那一天。``None`` 表示本进程实例今天还没发过。"""

        return self._completed_on

    def request_stop(self) -> None:
        self._stop.set()

    def run_once(self) -> RosterAuditReport | None:
        """跑一轮。返回 ``None`` 表示本轮没有执行比对（停止中，或今天已经做完）。"""

        if self._stop.is_set():
            # 已经在停止中：一轮都不开。停止之后必须 0 次发送（`V-花名册-20`）。
            return None
        now = self._clock()
        today = now.date()
        if self._completed_on == today:
            return None

        snapshot, baseline, report = self._compare_round(now)

        if report.is_empty and not snapshot.needs_attention:
            # 空差异日**不发日报**，只记一条审计。合同的语义是「通知待办」，
            # 没有待办却每天发一条「今天没事」，会让管理群很快学会忽略这个通知。
            self._audit.record(
                "roster_audit.no_difference",
                report_date=today.isoformat(),
                examined=report.examined,
                **snapshot.audit_facts(),
            )
            self._completed_on = today
            return report

        if self._stop.is_set():
            # 停止信号落在读取阶段（读库 + 读花名册都可能耗时）。这里再看一次，
            # 让这一轮成为**干净中断**：不发送、不置水位，什么都没发生。
            logger.info("停止信号在花名册读取期间到达，本轮不发送日报")
            return report

        return self._render_and_send(report, snapshot, baseline, today)

    def _compare_round(
        self, now: datetime
    ) -> tuple[Any, Sequence[ArchivedIdentity], RosterAuditReport]:
        """读一整轮花名册（写快照）→ 读比对基线 → 纯函数比对。

        **先读花名册、后读比对基线**：快照写入发生在 ``current()`` 里，是首次
        开通链与每日权限重算的硬前提；基线只服务日报正文。调换是安全的
        （``compare_roster`` 是纯函数，两个输入之间没有依赖），基线读取失败因此
        只影响日报，不拖累已经落库的快照。回读到不自洽的快照或写快照失败时，
        异常原样上抛，由 :class:`SchedulerLoop` 做职责级隔离并在下一轮重试
        （`V-花名册-17`）；水位不置位，因此这一天还没算做完。
        """

        round_result = self._roster_source.current(now=now)
        snapshot = round_result.snapshot
        baseline = self._baseline_reader.load_active_baseline()
        if snapshot.available:
            report = compare_roster(baseline, round_result.rows)
        else:
            # 一份快照都没有：**不比对**（`V-花名册-48`）。空行集会把全体已开通用户
            # 报成「花名册查无此人」，那是比"今天没日报"严重得多的错误输出。
            report = RosterAuditReport(examined=len(baseline))
        return snapshot, baseline, report

    def _render_and_send(
        self,
        report: RosterAuditReport,
        snapshot: Any,
        baseline: Sequence[ArchivedIdentity],
        today: date,
    ) -> RosterAuditReport:
        """渲染正文并发送；失败只记审计留给下一轮重试，成功则收口水位与日志。"""

        content = render_daily_report_content(
            report,
            report_date=today,
            # 存档身份段取自**本轮基线**，不是花名册：管理员要定位的是 Lingxi 这一
            # 侧的记录，而花名册当前值本来就要他自己去核实。
            identities={person.app_user_id: person for person in baseline},
            snapshot=snapshot,
        )
        try:
            # 同一天的日报（含失败重试）共用一个去重键：不确定态下的重试因此携带
            # 同一个投递 `uuid`，由飞书服务端去重，而不是必然重复投递。
            self._sender.send_text(
                chat_id=self._chat_id, text=content.text, dedupe_key=today.isoformat()
            )
        except Exception as error:  # 发送失败不得带走同一轮的其他职责
            # 只记审计与异常类型：异常正文可能带上群 ID 或响应体。水位不置位，
            # 因此这一天**不算已发送**，下一轮会重试（`V-花名册-30`）。
            self._audit.record(
                "roster_audit.send_failed",
                report_date=today.isoformat(),
                content_key=content.key,
                content_version=content.version,
                error=type(error).__name__,
                **snapshot.audit_facts(),
            )
            logger.error("管理群审计日报发送失败，下一轮重试 error=%s", type(error).__name__)
            return report

        self._record_sent(report, snapshot, content, today)
        return report

    def _record_sent(
        self, report: RosterAuditReport, snapshot: Any, content: Any, today: date
    ) -> None:
        """发送成功后的收口：置位水位、记完整审计、写摘要日志。

        摘要只有计数（审计与日志不含花名册字段值）；日报正文的展示口径放宽到
        「受控管理群可含原值」，日志侧没有随之放宽——日志流向排障、CI 输出与工单。
        """

        self._audit.record(
            "roster_audit.report_sent",
            report_date=today.isoformat(),
            content_key=content.key,
            content_version=content.version,
            examined=report.examined,
            entries=len(report.entries),
            handover=report.handover_count,
            removed=report.removed_count,
            ambiguous=report.ambiguous_count,
            **snapshot.audit_facts(),
        )
        self._completed_on = today
        logger.info(
            "管理群审计日报已发送 已开通用户=%s 条目=%s 疑似转交=%s 花名册查无=%s "
            "快照可用=%s 快照超龄=%s",
            report.examined,
            len(report.entries),
            report.handover_count,
            report.removed_count,
            snapshot.available,
            snapshot.stale,
        )


class RosterSnapshotSyncDuty:
    """花名册快照写入职责（写侧）：只读一轮花名册 → 更新持久快照。

    **不比对、不渲染、不发送任何消息**，从不经过管理群通知配置。从
    `RosterAuditDuty` 解出来的"写"那一半：`roster_snapshot` 表是首次开通链
    第二步与每日权限重算的共同数据前提。**只在 `RosterAuditDuty` 未装配时才会
    被 `build_loop` 装配**，两者互斥。**水位只在真的换上新快照时才置位**（判据
    是 ``RosterSnapshotStatus.refreshed``），读取失败或保旧都不置位、下一 tick
    重试，否则一次瞬时读取失败会烧掉一整天。同日重试的重复写入安全（整体替换）；
    **刻意不加读取失败退避**（量级远小于组织快照）。
    """

    name = "花名册快照同步"

    def __init__(
        self,
        *,
        roster_source: _RosterSource,
        clock: Callable[[], datetime] | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        self._roster_source = roster_source
        self._clock = clock or (lambda: datetime.now(UTC))
        self._stop = threading.Event() if stop is None else stop
        self._completed_on: date | None = None

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    @property
    def completed_on(self) -> date | None:
        """已完成本轮读取与写入的那一天。``None`` 表示本进程实例今天还没读过。"""

        return self._completed_on

    def request_stop(self) -> None:
        self._stop.set()

    def run_once(self) -> RosterRound | None:
        """跑一轮。返回 ``None`` 表示本轮没有触发读取（停止中，或今天已经做完）。"""

        if self._stop.is_set():
            # 已经在停止中：不开新一轮读取（与 RosterAuditDuty 的停止语义同一条：
            # 停止之后不得再触发新的花名册读取）。
            return None
        now = self._clock()
        today = now.date()
        if self._completed_on == today:
            return None

        # 写入失败原样上抛：水位不置位，交给 SchedulerLoop 隔离并在下一轮重试。
        round_result = self._roster_source.current(now=now)
        if round_result.snapshot.refreshed:
            self._completed_on = today
        return round_result


def _log_snapshot_alert(decision: SnapshotDecision) -> None:
    """快照保旧时的告警出口：一条只含分类与错误码的结构化警告。

    **面向管理员的那一份提醒走每日日报**（`V-花名册-47`：按日报告警提醒、不自动删），
    这里补的是运维侧那一半——日报一天只发一次，而运维需要在当轮就看到
    "今天的花名册读取失败了"。刻意不接 ``core/alerting.py`` 的状态机：它只认心跳、
    任务滞留与发送连续失败三类信号，把一件每日节奏的数据新鲜度事实塞进去，会让阈值、
    去重与恢复计时三套语义同时失真。

    只记分类与错误码，不记任何行内容（`V-花名册-33`）。
    """

    logger.warning(
        "花名册本轮读取未成功，保留上一份快照 status=%s alert=%s failure_code=%s 上一份行数=%s",
        decision.status,
        decision.alert.value if decision.alert is not None else None,
        decision.failure_code,
        decision.previous_row_count,
    )


def _roster_audit_missing_prerequisite(
    config: SchedulerConfig,
    *,
    roster_access_token: Callable[[], str] | None,
    audit: AuditSink,
) -> bool:
    """检查审计日报职责的三项前置，缺项记**恰一条**审计并返回 ``True``。

    按固定次序检查，缺第一个就返回：逐条报会让一个什么都没配的部署一次刷出多条
    审计，反而看不出该先配哪个。
    """

    if not config.admin_group_chat_id:
        # 只报变量名，不回显任何值。
        audit.record(
            "roster_audit.duty_not_registered",
            reason="missing_environment_variable",
            variable="LINGXI_ADMIN_GROUP_CHAT_ID",
        )
        logger.warning(
            "未配置 LINGXI_ADMIN_GROUP_CHAT_ID，花名册审计日报职责不注册；其余定时职责照常运行"
        )
        return True
    for variable, value in (
        ("LINGXI_ROSTER_BITABLE_APP_TOKEN", config.roster_app_token),
        ("LINGXI_ROSTER_BITABLE_TABLE_ID", config.roster_table_id),
    ):
        if not value:
            audit.record(
                "roster_audit.duty_not_registered",
                reason="missing_environment_variable",
                variable=variable,
            )
            logger.warning("未配置 %s，花名册审计日报职责不注册；其余定时职责照常运行", variable)
            return True
    if roster_access_token is None:
        # 调用方没有交出任何令牌供给。这条分支的含义是"真的没有供给"，不是"这条链
        # 还没接线"——正式装配路径下 `build_loop` 总会建出一个供给。"配了但拿不到
        # 令牌"不走这条分支：那时职责照常注册，失败发生在运行期并按分类审计，两者
        # 必须可分辨，否则排障会把运行期授权失败误判成配置缺失。
        audit.record("roster_audit.duty_not_registered", reason="missing_access_token_supply")
        logger.warning("调用方未提供花名册读取令牌供给，花名册审计日报职责不注册")
        return True
    return False


def _build_roster_audit_duty(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
    roster_access_token: Callable[[], str] | None,
    on_send_outcome: Callable[[str, bool], None] | None = None,
) -> RosterAuditDuty | None:
    """装配审计日报职责；前置不齐就**不注册**并留下**恰一条**审计，返回 ``None``。"""

    if _roster_audit_missing_prerequisite(
        config, roster_access_token=roster_access_token, audit=audit
    ):
        return None

    from lingxi.adapters.feishu_group_message import FeishuGroupMessages
    from lingxi.adapters.feishu_roster_bitable import BitableRosterPages, read_roster_snapshot
    from lingxi.adapters.postgres_roster_audit import PostgresRosterBaselineReader
    from lingxi.adapters.postgres_roster_snapshot import PostgresRosterSnapshotStore

    pages = BitableRosterPages(
        base_url=config.feishu_base_url,
        app_token=config.roster_app_token,
        table_id=config.roster_table_id,
        access_token=roster_access_token,
    )
    store = PostgresRosterSnapshotStore(config.postgres_dsn, timeouts=config.postgres_timeouts)
    roster_source = DailyRosterSource(
        # 走 `read_roster_snapshot` 而不是逐页归一：日报必须能区分「花名册真的空了」
        # 与「这一轮没读完」，而只有前者会让保旧判定拿到 `EMPTY_SOURCE`。
        read_round=lambda: read_roster_snapshot(pages),
        updater=RosterSnapshotUpdater(store=store, audit=audit, on_alert=_log_snapshot_alert),
        load_snapshot=store.load,
        stale_after=config.roster_snapshot_stale_after,
    )

    return RosterAuditDuty(
        baseline_reader=PostgresRosterBaselineReader(
            config.postgres_dsn, timeouts=config.postgres_timeouts
        ),
        roster_source=roster_source,
        sender=FeishuGroupMessages(
            base_url=config.feishu_base_url,
            app_id=config.feishu_app_id,
            app_secret=config.feishu_app_secret,
            on_send_outcome=on_send_outcome,
        ),
        audit=audit,
        chat_id=config.admin_group_chat_id,
        stop=stop,
    )


def _build_roster_snapshot_sync_duty(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
    roster_access_token: Callable[[], str] | None,
) -> RosterSnapshotSyncDuty | None:
    """装配花名册快照写入职责（写侧）；前置不齐就**不注册**并留下**恰一条**审计。

    **只有两个前置——花名册 Base 坐标与读取令牌供给，刻意不含管理群 chat_id**，
    从不经过管理群那条通知链路。形状照 :func:`_roster_audit_missing_prerequisite`，
    但**审计动作前缀改成 `roster_snapshot_sync.`**。**调用方只在
    `_build_roster_audit_duty` 返回 `None` 时才会调用本函数**：两个职责互斥
    注册，避免同时跑出两条各自独立的 `DailyRosterSource`。
    """

    for variable, value in (
        ("LINGXI_ROSTER_BITABLE_APP_TOKEN", config.roster_app_token),
        ("LINGXI_ROSTER_BITABLE_TABLE_ID", config.roster_table_id),
    ):
        if not value:
            audit.record(
                "roster_snapshot_sync.duty_not_registered",
                reason="missing_environment_variable",
                variable=variable,
            )
            logger.warning("未配置 %s，花名册快照写入职责不注册；其余定时职责照常运行", variable)
            return None
    if roster_access_token is None:
        # 同 `_roster_audit_missing_prerequisite` 的同一条纪律：这条分支的含义是
        # "调用方真的没有交出任何供给"，不是"这条链还没接线"；"配了但拿不到令牌"
        # 不走这条分支，职责照常注册，失败发生在运行期并按分类审计。
        audit.record(
            "roster_snapshot_sync.duty_not_registered", reason="missing_access_token_supply"
        )
        logger.warning("调用方未提供花名册读取令牌供给，花名册快照写入职责不注册")
        return None

    from lingxi.adapters.feishu_roster_bitable import BitableRosterPages, read_roster_snapshot
    from lingxi.adapters.postgres_roster_snapshot import PostgresRosterSnapshotStore

    pages = BitableRosterPages(
        base_url=config.feishu_base_url,
        app_token=config.roster_app_token,
        table_id=config.roster_table_id,
        access_token=roster_access_token,
    )
    store = PostgresRosterSnapshotStore(config.postgres_dsn, timeouts=config.postgres_timeouts)
    roster_source = DailyRosterSource(
        read_round=lambda: read_roster_snapshot(pages),
        updater=RosterSnapshotUpdater(store=store, audit=audit, on_alert=_log_snapshot_alert),
        load_snapshot=store.load,
        stale_after=config.roster_snapshot_stale_after,
    )
    return RosterSnapshotSyncDuty(roster_source=roster_source, stop=stop)
