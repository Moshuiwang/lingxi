"""花名册资料比对与管理群审计日报职责：:class:`RosterAuditDuty`；以及与它互斥的写侧
职责 :class:`RosterSnapshotSyncDuty`（Issue #275）。

从 :mod:`lingxi.apps.scheduler`（#237 拆分）搬出。同日至多一次、空差异不发、快照保旧
与超龄告警等规则的完整理由见该类自己的文档字符串（未随拆分改动）。

**Issue #275 的解耦**：`roster_snapshot` 表是首次开通链第二步
（`core/identity/onboarding_runner.py::_match`）的硬前提，但此前"写快照"这件事被绑在
`RosterAuditDuty` 内部——它的前置里含 `LINGXI_ADMIN_GROUP_CHAT_ID`，一个纯粹服务
"日报发到哪个群"的通知配置，因此没配管理群会让快照永远不写、员工永远开通不了
（2026-08-21 首触冒烟实测坐实）。`RosterSnapshotSyncDuty` 是解出来的"只写不发"那一半：
只依赖花名册 Base 坐标与令牌供给，与管理群配置无关。两个职责在装配层**互斥**（见
`apps/scheduler/assembly.py` 的 `build_loop`）：管理群与 Base 坐标都齐全时，`RosterAuditDuty`
自己内部完成"读一轮→写快照→立刻比对"（与改动前完全相同，零变化）；`RosterAuditDuty`
因任何原因未装配时，才尝试单独装配 `RosterSnapshotSyncDuty`。这样任何时刻至多只有一个
职责在触发花名册读取，不会出现"同一天读两轮花名册"的形状（`RosterAccessTokenProvider`
的一次性 `refresh_token` 唯一消费者纪律因此不受影响，见
`core/identity/access_token_supply.py`）。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from datetime import date, datetime, timezone
from typing import Protocol

from lingxi.core.identity.roster_audit import ArchivedIdentity, RosterAuditReport, compare_roster
from lingxi.core.identity.roster_report import render_daily_report_content
from lingxi.core.identity.roster_snapshot import RosterRound, SnapshotDecision

from lingxi.apps.scheduler.audit import AuditSink

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
    """每日花名册资料比对与管理群审计日报（Issue #52）。

    一轮做四件事：取本轮花名册（读一整轮 + 更新或保留持久快照）→ 读比对基线 →
    纯函数比对 → 该发就发一条日报。**存档三字段这一侧只读**（`V-花名册-14`）；
    唯一的写库发生在花名册快照那一侧，而它写的是花名册的副本，不是 `app_user`。

    **花名册排在基线之前是刻意的**（冻结候选审查 2026-08-21 的 F6）：第一件事里的
    快照写入是**首次开通链的硬前提**，第二件事只服务日报正文。反过来排会让"日报基线
    读不出来"把"新员工能不能被开通"一起拖停——Issue #275 解开的是配置期的同一条耦合，
    这里解开的是运行期那一半。理由与重复写入的安全性见 :meth:`run_once` 内的注释。

    **"该发就发"不等于"有差异才发"**。空差异日本来不发（`V-花名册-25`），但有两种
    情形下「今天没有差异」这句话本身不可信，沉默因此是最危险的输出：

    - **快照超龄**（`V-花名册-47`）：源头连续读不到，比对用的还是几天前那份花名册。
      产品负责人 2026-08-17 裁定：始终保留最近一份、超龄**按日报告警提醒、不自动删**；
    - **没有任何可用快照**（`V-花名册-48`）：这一天**不比对**。拿空行去比对会把全体
      已开通用户报成「花名册查无此人」——那正是空源保护要挡的形状。

    日报正文按产品负责人 2026-08-08 的 **D2 裁定**渲染：受控管理群可含用于定位的存档
    身份（姓名、工号），并写明快照时间与同步状态、日期一律 UTC（渲染细节见
    :mod:`lingxi.core.identity.roster_report`）。**日志与审计没有随之放宽**
    （`V-花名册-33`）：它们流向排障、CI 输出与工单，不在受控管理群那个范围里。

    **同日至多一次**（`V-花名册-31`）。判重靠 ``_completed_on`` 这个进程内的日期水位：

    - 单轮一次、同进程跨轮一次：由这个水位**硬保证**，与轮询周期无关；
    - 跨重启：**判重水位没有持久载体**，新进程的水位是空的，因此**重启当日会重发一份
      内容完全相同的日报**。这是产品负责人 2026-08-06 知情接受的残留（裁定 C2 / R2）：
      A 方案下报告由「花名册现值 + 存档」唯一确定，补跑产出同一份，重复是噪声不是错误。
      **原表述「零新表定案下没有持久载体」的前提已被 2026-08-08 的 D2 裁定覆盖**——
      仓库现在确有持久载体（`roster_snapshot`，迁移 `0063`），但它存的是**花名册读取
      结果**，不是判重水位；结论因此没变：跨重启的真幂等要等判重水位本身也有持久载体
      （或 ``audit_event`` 表落地）后再补。

      **快照的 ``captured_at`` 不能顶替判重水位**（S-B-04 核对过并放弃了这条捷径）：
      它回答的是"快照哪天换的"，而水位要回答的是"日报哪天发出去的"，两者只在最顺利的
      那条路径上重合。发送失败的那一天快照照样已经换成当天的——拿 ``captured_at``
      当水位，重启后会认为"今天已经做完"，于是**那一天的日报永远不会发出去**。用一个
      每天至多重发一份相同日报的噪声，换一个整天静默的失败，方向是反的。真幂等需要
      判重水位自己的持久列，属新迁移，不在本 Story 的授权范围内。

    水位在**发送成功之后**才置位。发送失败不算已发送，下一轮重试（`V-花名册-30`）。
    空差异日也置位：那一天的审计已经记过了，重复记只是噪声（`V-花名册-25`）。

    **重试与"不确定态"**。"发送失败就重试"这条规则有一个它自己看不见的缺口：HTTP
    请求已经被飞书收下、而响应在回程超时的那一刻，进程拿到的是异常，事实却是消息已经
    发出去了。重试于是重复投递一条日报。同一天的每一次发送——首次与全部重试——因此共用
    一个去重键（当日 UTC 日期），由 :func:`~lingxi.adapters.feishu_group_message.delivery_uuid`
    折成飞书的投递 `uuid` 交服务端去重。平台侧的去重窗口未经验证（属 L4a），所以这里
    承诺的是"重试携带同一个 `uuid`"这个代码事实，不是"平台一定不会重复投递"。
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
        self._clock = clock or (lambda: datetime.now(timezone.utc))
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

        # **先读花名册、后读比对基线**（冻结候选审查 2026-08-21 的 F6）。顺序不是随意的：
        # 快照写入就发生在 `current()` 里，而 `roster_snapshot` 表是首次开通链第二步
        # （`core/identity/onboarding_runner.py::_match`）与每日权限重算的硬前提；
        # `load_active_baseline()` 读的是 `app_user`，只服务**日报正文**。旧顺序把
        # baseline 排在前面，于是 baseline 持续读取失败时异常先冒泡，健康的花名册读取
        # 与快照写入被一起拖停——Issue #275 只解开了"没配管理群 ⇒ 快照不写"那一半，
        # 运行期的这一半耦合还在。调换之后，日报侧的故障只影响日报。
        #
        # 调换是安全的：`compare_roster` 是纯函数，两个输入之间没有依赖；baseline 抛
        # 异常时 `current()` 已经完成、快照已经落库，下一轮重试会**再读一轮花名册、
        # 再整体替换一次快照**——而 `PostgresRosterSnapshotStore.replace()` 是"同一
        # 事务里删光旧行 + 整体写入新行"的整体替换（表里永远只有一份），重复写入只是
        # 把同样的内容换成更新的 `captured_at`，不累积、不产生重复行。
        #
        # 读一整轮花名册并结算持久快照。回读到不自洽的快照
        # （:class:`~lingxi.adapters.postgres_roster_snapshot.RosterSnapshotInconsistent`）
        # 或写快照失败时，异常原样上抛，由 :class:`SchedulerLoop` 做职责级隔离并在
        # 下一轮重试（`V-花名册-17`）；水位不置位，因此这一天还没算做完。
        round_result = self._roster_source.current(now=now)
        snapshot = round_result.snapshot
        baseline = self._baseline_reader.load_active_baseline()

        if snapshot.available:
            report = compare_roster(baseline, round_result.rows)
        else:
            # 一份快照都没有：**不比对**（`V-花名册-48`）。空行集会把全体已开通用户
            # 报成「花名册查无此人」，那是比"今天没日报"严重得多的错误输出。
            report = RosterAuditReport(examined=len(baseline))

        if report.is_empty and not snapshot.needs_attention:
            # 空差异日**不发日报**，只记一条审计。合同 :85 的语义是「通知待办」，
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
            # 停止之后必须 0 次发送（`V-花名册-20`），而入口处那一次检查挡不住
            # "进来时还没停、读完才停"这条时序。重发由裁定 C2 的知情接受覆盖。
            logger.info("停止信号在花名册读取期间到达，本轮不发送日报")
            return report

        content = render_daily_report_content(
            report,
            report_date=today,
            # D2 的存档身份段取自**本轮基线**，不是花名册：管理员要定位的是 Lingxi
            # 这一侧的记录，而花名册当前值本来就要他自己去核实。
            identities={person.app_user_id: person for person in baseline},
            snapshot=snapshot,
        )
        try:
            # 同一天的日报（含失败重试）共用一个去重键：不确定态下的重试因此携带
            # 同一个投递 `uuid`，由飞书服务端去重，而不是必然重复投递。
            self._sender.send_text(
                chat_id=self._chat_id, text=content.text, dedupe_key=today.isoformat()
            )
        except Exception as error:  # noqa: BLE001 - 发送失败不得带走同一轮的其他职责
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
        # 摘要只有计数，依据是 `V-花名册-33`（审计与日志不含花名册字段值）。日报正文的
        # 展示口径已被 2026-08-08 的 D2 裁定放宽到「受控管理群可含原值」，日志侧没有
        # 随之放宽：日志流向排障、CI 输出与工单，不在受控管理群那个范围里。
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
        return report


class RosterSnapshotSyncDuty:
    """花名册快照写入职责（写侧，Issue #275）：只读一轮花名册 → 更新持久快照，
    **不比对、不渲染、不发送任何消息**。

    这是从 `RosterAuditDuty` 解出来的"写"那一半：`roster_snapshot` 表是首次开通链
    第二步（`core/identity/onboarding_runner.py::_match`）与每日权限重算
    （`permission_refresh.py`）的共同数据前提，两者都直接经
    `PostgresRosterSnapshotStore` 读这张表，从不经过 `RosterAuditDuty`——这条职责
    因此把"写"对齐到它真正的消费者身边，不再要求一个只服务通知的配置项
    （管理群 chat_id）先满足。

    **只在 `RosterAuditDuty` 未装配时才会被 `build_loop` 装配**（见
    `apps/scheduler/assembly.py::_build_roster_snapshot_sync_duty` 与
    `build_loop` 调用点的注释）：管理群与 Base 坐标都齐全时，写入与比对仍然捆在
    `RosterAuditDuty` 内部一次完成——那是已经验证过的路径，没有理由为它另开一次独立
    读取。两者因此互斥，同一时刻至多一个职责在触发花名册读取，"今天到底读了几次
    花名册"永远唯一，不给一次性 `refresh_token` 的唯一消费者纪律增加新的分辨负担。

    形状与 :class:`RosterAuditDuty` 同源：只编排注入的 :class:`~lingxi.core.
    identity.roster_snapshot.DailyRosterSource`（同一天**成功换上一份新快照之后**只
    真正读一次,靠自己的 ``_completed_on`` 日期水位）,不做任何 I/O；写入失败**不吞**——
    `DailyRosterSource.current()` 内部调用的 `RosterSnapshotUpdater.apply()`
    在写库失败时原样上抛（先留一条 ``roster_snapshot.replace_failed`` 审计），
    这里不捕获，交给 :class:`~lingxi.apps.scheduler.loop.SchedulerLoop` 做职责级
    隔离并在下一轮重试——水位在这里也不置位，因此"今天"不会被错误地标记为已完成。

    **水位只在真的换上新快照时才置位**（冻结候选审查 2026-08-21 的 F2）。上一段说的
    "写入失败不置位"只覆盖**写库抛异常**那一路，而花名册读取失败根本不抛：
    `adapters/feishu_roster_bitable.read_roster_snapshot` 把 ``RosterReadError``
    结算成一个 ``status=FAILED`` 的**正常返回值**（空源、不完整同理），
    `RosterSnapshotUpdater.apply()` 于是走"保旧"分支、正常返回，`current()` 也正常
    返回——旧代码在这里无条件置位水位，于是**一次瞬时读取失败就烧掉一整天**：当天不再
    写快照，而 `roster_snapshot` 表正是首次开通链第二步的硬前提，表现成"今天入职的人
    全都开通不了，而且没有任何东西在重试"。判据因此改成
    :attr:`~lingxi.core.identity.roster_snapshot.RosterSnapshotStatus.refreshed`
    （``action`` 是 ``install``/``replace``，即本轮真的写进去了一份）——保旧轮与失败轮
    都不置位，下一 tick 重试。

    **同日重试的重复写入是安全的**：``PostgresRosterSnapshotStore.replace()`` 是"同一
    事务里删光旧行 + 整体写入新行"的**整体替换**，表里永远只有一份快照；重跑一次只是
    把同样的内容换成一个更新的 ``captured_at``，不会累积、不会产生重复行，读侧
    （开通链 `_match`、每日权限重算）也只认库里那唯一一份。

    **刻意不加读取失败退避**（与 `apps/scheduler/org_snapshot_sync.py` 的
    ``READ_FAILURE_BACKOFF_*`` 形成对比，那里加了）。两处的量级不是一回事：

    - 组织快照一轮是**数百次**分页请求，贴着 tick 重试一天约 2880 轮，那是真实成本；
      花名册一轮 500 行/页、受控读取 1206 行只占 **3 页**，最坏情况（整天保旧）按 60 秒
      tick 约 4300 次请求/天，量级可忽略；
    - **令牌换取次数完全不受本次改动影响**：走到"保旧"这一支说明令牌已经拿到了，而
      `DerivedAccessTokenHolder` 缓存的令牌寿命约 2 小时，一天仍然只换约 12 次；
      真正拿不到令牌时 `RosterAccessTokenProvider` 抛 ``AccessTokenUnavailable``、
      `current()` 直接冒泡——那条路径**在改动前就**到不了置位水位那一行，每 tick 重试
      是既有形状，不是本次引入的。

    因此这里选择"简单地下一 tick 再试"，用一点请求噪声换"当天还有机会自愈"；真要加
    退避时，直接复用 `org_snapshot_sync` 那一套形状即可，不必新造机制。

    **本类不持有独立的 ``audit`` 协作者**：写入结果（``roster_snapshot.replaced``/
    ``kept_previous``/``replace_failed``）已经由注入的 `RosterSnapshotUpdater` 自己
    的审计出口记录（见 `_build_roster_snapshot_sync_duty` 的装配代码），本类再存一份
    只会是一个从不被调用的多余协作者。
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
        self._clock = clock or (lambda: datetime.now(timezone.utc))
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

    **面向管理员的那一份提醒走每日日报**（`V-花名册-47` 的裁定原文是「按日报告警提醒、
    不自动删」），这里补的是运维侧那一半——日报一天只发一次，而运维需要在当轮就看到
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
