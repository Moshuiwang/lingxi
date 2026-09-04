"""权限发布消费与就绪确认职责。

一轮 tick 依次做四件事，次序不可调换：**收殓**崩溃留下的 ``publishing`` 意图放回
``pending``（发布本身幂等，重入安全）→ **发布**驱动执行器消费待发布意图（写发布表
→ 逐字段读回核对）→ **就绪确认**对已发布读回一致、尚未收口的每一条按 tick 节奏
推进至多一步（:class:`~lingxi.core.permission.mcp_readiness.ReadinessTicker`，只
替换阻塞式确认的"等待"实现，判定与落库全部复用同一份代码，避免一个未就绪用户占住
整个 :class:`SchedulerLoop` 十五分钟）→ **通知**探针成功发「范围已更新」、权限为空
（撤权）发「暂无可用范围」，终态记录先落库、通知失败不回头改任何状态。与每日权限
重算是两个职责：重算靠当日水位保证同日至多一轮，发布消费必须每轮都跑。只确认自己
排出来的两类意图（首次开通由开通编排自己确认），超时或收件人不可用一律不发通知、
只留审计。发布/就绪通知/探针三个面各按自身依赖独立装配，缺一面只停那一面；两个
上界（条数挡外部接口配额、时间挡外部劣化拖慢心跳评估）都是"本轮止步、下轮继续"，
发布条数预算数的是"多少条不同的意图"而非认领次数。按单实例设计，不做分布式租约。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from lingxi.apps.scheduler.audit import AuditSink
from lingxi.apps.scheduler.config import SchedulerConfig
from lingxi.apps.scheduler.permission_refresh import (
    PERMISSION_REFRESH_REASON,
    PERMISSION_REVOKE_REASON,
)
from lingxi.core.permission.mcp_readiness import (
    ReadinessAttempt,
    ReadinessBinding,
    ReadinessOutcome,
    ReadinessProgress,
)
from lingxi.core.permission.notification import NoticeResult

logger = logging.getLogger(__name__)

_UTC = UTC

#: **本职责负责确认哪一类发布意图。** 每日刷新与撤权这两条是它自己排出来的；
#: 首次开通那条（``first_onboarding``）**刻意不在其中**——它由 Epic D 的开通编排自己
#: 确认并发"开通完成"。两边都捞的话，一个刚开通的用户会在"开通完成"之外再收到一条
#: 措辞完全不同的"可用范围已更新"，而且两个确认还会对同一个 ``(用户, 权限版本)``
#: 并发发探针。新增一种 ``reason`` 时必须显式决定它归谁，不给默认归属。
FOLLOW_UP_REASONS: tuple[str, ...] = (PERMISSION_REFRESH_REASON, PERMISSION_REVOKE_REASON)

#: **探针未接线时**只认领撤权那一类。缺了这条会饿死撤权通知：候选查询把"还没
#: 探过"的排在最前面，而 ``probe=None`` 时授权候选每轮都只得到 ``None``、不落
#: 记录，于是永远保持最高优先级，后发布的撤权行再也进不了窗口。撤权通知本来
#: 就不依赖探针，把授权候选整个排除在查询之外，它们就不再占用 ``LIMIT``；
#: 端点配好后自然回到窗口里（进度全在库里，一条都没丢）。
REVOKE_ONLY_REASONS: tuple[str, ...] = (PERMISSION_REVOKE_REASON,)

#: 单轮发布预算：一轮最多消费多少条待发布意图。挡的是"一轮把整张表刷完"占住外部接口
#: 配额；剩下的下一轮继续（同 :meth:`PermissionPublishExecutor.run_once` 的 ``limit``）。
DEFAULT_PUBLISH_LIMIT = 50

#: 单轮就绪预算：一轮最多推进多少条待确认。同样是配额保护，不是重试上限——"最多探几次"
#: 由就绪计划表决定，与这个数无关。
DEFAULT_READINESS_LIMIT = 50

#: **单轮时间预算（秒）**。外部劣化时，条数预算挡不住时间：一条发布要写外部表 + 逐字段
#: 读回，一次探针最长 20 秒，把两个条数预算刷满可以到几十分钟。而 scheduler 的活性心跳
#: 每轮才跳一次，默认阈值 180 秒（``LINGXI_ALERT_RUNNING_HEARTBEAT_TIMEOUT_SECONDS``）
#: ——一轮跑过头会让"这一轮很慢"被读成"心跳丢了"。取 60 秒：它等于默认调度周期，留出
#: 三倍于心跳阈值的余量，且发布与就绪都可重入，本轮没做完的下一轮接着做。
DEFAULT_ROUND_BUDGET_SECONDS = 60.0


class _PublishExecutor(Protocol):
    def run_once(self, *, limit: int = ..., exclude: Sequence[str] = ...) -> Sequence[Any]: ...


class _IntentStore(Protocol):
    """发布意图与收件人的读取口（``adapters/postgres_permission_publish.py``）。"""

    def reclaim_stale(self, *, older_than: timedelta = ...) -> int: ...
    def published_awaiting_readiness(
        self,
        *,
        reasons: Sequence[str],
        interval_seconds: int,
        budget_seconds: int,
        limit: int = ...,
    ) -> Sequence[Any]: ...
    def notice_recipient_open_id(self, user_id: str) -> str | None: ...


class _ReadinessChecks(Protocol):
    """就绪判定记录的回读口（``adapters/postgres_mcp_token.py``）。"""

    def load_checks(self, user_id: str, permission_version: int) -> Sequence[Any]: ...


class _Ticker(Protocol):
    schedule: Any
    probe_wired: bool

    def advance(
        self, binding: ReadinessBinding, *, permissions: str, progress: ReadinessProgress
    ) -> ReadinessAttempt | None: ...


class _Notices(Protocol):
    def notify(
        self, *, user_id: str, open_id: str, permission_version: int, permissions: str
    ) -> NoticeResult: ...


class _AuditSink(Protocol):
    def record(self, action: str, /, **fields: object) -> None: ...


@dataclass(frozen=True)
class ReadinessFollowUp:
    """就绪确认与通知这一整面。**要么三件齐全，要么整面不装配。**

    做成一个必须整体注入的对象，而不是三个各自可空的参数：三者少任何一个，这一面都只能
    做半件事——能探不能通知、能通知却说不出该发给谁。半装配的形态在日志里看起来一切正常，
    却在用户侧表现为"权限生效了但从来没人告诉我"。
    """

    ticker: _Ticker
    checks: _ReadinessChecks
    notices: _Notices


@dataclass(frozen=True)
class PermissionPublishReport:
    """一轮的结果。**只有计数与固定分类，没有任何字段值。**"""

    reclaimed: int = 0
    attempts: int = 0
    published: int = 0
    pending_readiness: int = 0
    advanced: int = 0
    ready: int = 0
    revoked: int = 0
    timed_out: int = 0
    notices_sent: int = 0
    notices_failed: int = 0
    notices_skipped: int = 0
    failed: int = 0
    interrupted: bool = False
    publish_wired: bool = True
    readiness_wired: bool = True
    probe_wired: bool = True

    def audit_facts(self) -> dict[str, Any]:
        facts: dict[str, Any] = {
            "reclaimed": self.reclaimed,
            "attempts": self.attempts,
            "published": self.published,
            "pending_readiness": self.pending_readiness,
            "advanced": self.advanced,
            "ready": self.ready,
            "revoked": self.revoked,
            "timed_out": self.timed_out,
            "notices_sent": self.notices_sent,
            "notices_failed": self.notices_failed,
            "notices_skipped": self.notices_skipped,
            "failed": self.failed,
            "publish_wired": self.publish_wired,
            "readiness_wired": self.readiness_wired,
            "probe_wired": self.probe_wired,
        }
        if self.interrupted:
            facts["interrupted"] = True
        return facts


@dataclass
class _Tally:
    """累加器。:class:`PermissionPublishReport` 是冻结的（它会进审计）。"""

    reclaimed: int = 0
    attempts: int = 0
    published: int = 0
    pending_readiness: int = 0
    advanced: int = 0
    ready: int = 0
    revoked: int = 0
    timed_out: int = 0
    notices_sent: int = 0
    notices_failed: int = 0
    notices_skipped: int = 0
    failed: int = 0

    def freeze(
        self,
        *,
        interrupted: bool,
        publish_wired: bool,
        readiness_wired: bool,
        probe_wired: bool,
    ) -> PermissionPublishReport:
        return PermissionPublishReport(
            reclaimed=self.reclaimed,
            attempts=self.attempts,
            published=self.published,
            pending_readiness=self.pending_readiness,
            advanced=self.advanced,
            ready=self.ready,
            revoked=self.revoked,
            timed_out=self.timed_out,
            notices_sent=self.notices_sent,
            notices_failed=self.notices_failed,
            notices_skipped=self.notices_skipped,
            failed=self.failed,
            interrupted=interrupted,
            publish_wired=publish_wired,
            readiness_wired=readiness_wired,
            probe_wired=probe_wired,
        )


class PermissionPublishDuty:
    """每轮 tick：收殓 → 发布 → 推进就绪确认 → 该通知就通知。

    语义与边界见模块文档。本类**只编排**：发布判定在
    :mod:`lingxi.core.permission.publish`，就绪判定在
    :mod:`lingxi.core.permission.mcp_readiness`，通知正文与重试在
    :mod:`lingxi.core.permission.notification`，这里一条规则都不复制。
    """

    name = "权限发布与就绪确认"

    def __init__(
        self,
        *,
        intents: _IntentStore,
        audit: _AuditSink,
        executor: _PublishExecutor | None = None,
        readiness: ReadinessFollowUp | None = None,
        publish_limit: int = DEFAULT_PUBLISH_LIMIT,
        readiness_limit: int = DEFAULT_READINESS_LIMIT,
        round_budget_seconds: float = DEFAULT_ROUND_BUDGET_SECONDS,
        on_alert: Callable[[str, str], None] | None = None,
        on_management_corrections: Callable[[], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        for label, value in (("发布", publish_limit), ("就绪", readiness_limit)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"单轮{label}预算必须是正整数")
        if (
            isinstance(round_budget_seconds, bool)
            or not isinstance(round_budget_seconds, (int, float))
            or round_budget_seconds <= 0
        ):
            raise ValueError("单轮时间预算必须是正数秒")
        if readiness is not None and not isinstance(readiness, ReadinessFollowUp):
            raise TypeError("就绪面必须整体注入 ReadinessFollowUp，不接受半套装配")
        if executor is None and readiness is None:
            # 两面都没装配的职责放进循环里只会每分钟记一条空报告，纯噪声。
            raise ValueError("发布面与就绪面至少要装配一面")
        self._executor = executor
        self._intents = intents
        self._audit = audit
        self._readiness = readiness
        self._publish_limit = publish_limit
        self._readiness_limit = readiness_limit
        self._round_budget = timedelta(seconds=float(round_budget_seconds))
        self._on_alert = on_alert
        self._on_management_corrections = on_management_corrections
        self._clock = clock or (lambda: datetime.now(_UTC))
        # 与同一进程内的其他职责共享停止标志：SIGTERM 一次让所有职责停止领取新工作。
        self._stop = threading.Event() if stop is None else stop

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    @property
    def publish_wired(self) -> bool:
        """发布面装配了没有。缺权限表坐标或令牌供给时它是 ``False``，就绪面照常推进。"""

        return self._executor is not None

    @property
    def readiness_wired(self) -> bool:
        """就绪与通知这一面装配了没有。装配层缺配置时它是 ``False``，发布面照常。"""

        return self._readiness is not None

    def request_stop(self) -> None:
        self._stop.set()

    def _out_of_time(self, started: datetime) -> bool:
        """本轮的时间预算用完了没有。

        它挡的是**外部劣化**：发布一条要写外部表 + 逐字段读回，探针一次最长 20 秒，
        一轮把预算全刷完可以到几十分钟——而 ``lingxi-scheduler`` 的活性心跳每轮才跳一次，
        阈值是 ``LINGXI_ALERT_RUNNING_HEARTBEAT_TIMEOUT_SECONDS``（默认 180 秒）。
        一轮跑过头会让告警状态机把"这个进程还活着、只是这一轮很慢"读成"心跳丢了"。
        因此本轮到点就止步，剩下的下一轮继续——发布与就绪都是可重入的。
        """

        return self._clock() - started >= self._round_budget

    # ------------------------------------------------------------------
    # 一轮
    # ------------------------------------------------------------------

    def run_once(self) -> PermissionPublishReport | None:
        """跑一轮。返回 ``None`` 表示本轮没有执行（停止中）。

        **本方法不会等待**：一条还没就绪的确认只是被推进一步或原样留着，绝不 ``sleep``。
        整轮耗时的上界因此是"外部调用次数 × 单次超时"，与十五分钟的就绪预算无关。
        """

        if self._stop.is_set():
            # 已经在停止中：一条意图都不领，一次探针都不发。
            return None

        started = self._clock()
        tally = _Tally()
        tally.reclaimed = self._intents.reclaim_stale()
        interrupted = self._publish(tally, started)

        readiness = self._readiness
        if readiness is not None and not interrupted and not self._stop.is_set():
            interrupted = self._advance_readiness(readiness, tally, started)

        return self._finish_round(tally, interrupted, readiness)

    def _advance_readiness(
        self, readiness: ReadinessFollowUp, tally: _Tally, started: datetime
    ) -> bool:
        """取待确认候选并逐条推进一步；返回本轮是否被停止信号或时间预算中断。"""

        # 探针没接线时**只取撤权那一类**：授权候选这一轮推进不了，留在查询之外才不会
        # 把窗口占死（:data:`REVOKE_ONLY_REASONS` 的文档写明了饿死是怎么发生的）。
        reasons = FOLLOW_UP_REASONS if readiness.ticker.probe_wired else REVOKE_ONLY_REASONS
        pending = tuple(
            self._intents.published_awaiting_readiness(
                reasons=reasons,
                interval_seconds=readiness.ticker.schedule.interval_seconds,
                budget_seconds=readiness.ticker.schedule.budget_seconds,
                limit=self._readiness_limit,
            )
        )
        tally.pending_readiness = len(pending)
        for item in pending:
            if self._stop.is_set() or self._out_of_time(started):
                # 停止信号或时间预算落在遍历中间：不再为后面的人发探针或发通知。
                # 已经落库的判定各自是一个完整事务；下一次启动会从库里把进度原样
                # 读回来。
                return True
            try:
                self._advance(readiness, item, tally)
            except Exception as error:  # 一个人的失败不得带走整轮
                # 只记异常类型：异常正文可能带上被处理对象的内容。
                tally.failed += 1
                self._audit.record(
                    "permission_publish.user_failed",
                    user=item.user_id,
                    permission_version=item.permission_version,
                    error=type(error).__name__,
                )
                logger.error(
                    "单个用户的就绪确认失败，其余用户继续 user=%s error=%s",
                    item.user_id,
                    type(error).__name__,
                )
        return False

    def _finish_round(
        self, tally: _Tally, interrupted: bool, readiness: ReadinessFollowUp | None
    ) -> PermissionPublishReport:
        """冻结报告、记完成审计、触发管理卡补偿观察、写摘要日志。"""

        report = tally.freeze(
            interrupted=interrupted,
            publish_wired=self.publish_wired,
            readiness_wired=self.readiness_wired,
            # 探针没接线时，需要探针的那一路本轮不推进——报告里必须看得出来，
            # 否则"待确认一直是 20 条、本轮推进 0"读起来像卡死。
            # **整面都没装配时它同样是 False**：给出 ``readiness_wired=False,
            # probe_wired=True`` 会让读报告的人以为探针是好的、只是别处出了问题。
            probe_wired=readiness is not None and readiness.ticker.probe_wired,
        )
        self._audit.record("permission_publish.completed", **report.audit_facts())
        if self._on_management_corrections is not None:
            # 这条观察只收口已经被发布面读回一致的管理卡上下文；它不参与权限
            # 决定，也不把 outbox 入队误报为外部生效。回调自身负责把每日补齐
            # 汇总做成幂等投递，异常不能带走本轮发布/就绪结果。
            try:
                self._on_management_corrections()
            except Exception as error:  # observer is best effort
                self._audit.record(
                    "admin.management_correction_settlement_failed",
                    error=type(error).__name__,
                )
        if report.advanced or report.attempts or report.reclaimed:
            # 摘要只有计数。一轮什么都没做时不打日志：这条职责每分钟跑一次。
            logger.info(
                "权限发布与就绪确认完成 收殓=%s 发布尝试=%s 发布完成=%s 待确认=%s "
                "本轮推进=%s 就绪=%s 撤权=%s 超时=%s 通知成功=%s 通知失败=%s 未通知=%s 失败=%s",
                report.reclaimed,
                report.attempts,
                report.published,
                report.pending_readiness,
                report.advanced,
                report.ready,
                report.revoked,
                report.timed_out,
                report.notices_sent,
                report.notices_failed,
                report.notices_skipped,
                report.failed,
            )
        return report

    # ------------------------------------------------------------------
    # 发布面
    # ------------------------------------------------------------------

    def _publish(self, tally: _Tally, started: datetime) -> bool:
        """消费待发布意图，返回"本轮是不是被中断了"。

        **逐条认领而不是一次 ``limit=50``**：执行器的批量循环里没有停止钩子，
        SIGTERM 之后它会把整批新意图继续认领完，违反"停止领取新工作"。改成每次
        只认领一条、每条之前重新看一眼停止标志与时间预算。**本轮已认领清单必须
        由这里持有**：不把累积清单传下去的话，一条快失败的意图会在同一轮里被
        反复认领、烧完重试额度。清单是每轮新建的局部变量，跨轮持有会让一条
        意图在这个进程里再也轮不到。
        """

        if self._executor is None:
            return False
        claimed: list[str] = []
        for _ in range(self._publish_limit):
            if self._stop.is_set() or self._out_of_time(started):
                return True
            attempts = tuple(self._executor.run_once(limit=1, exclude=tuple(claimed)))
            if not attempts:
                # 没有待发布意图了：这不是中断，是正常收工。
                return False
            claimed.extend(
                str(identifier)
                for identifier in (getattr(attempt, "outbox_id", None) for attempt in attempts)
                if identifier
            )
            tally.attempts += len(attempts)
            tally.published += sum(
                1 for attempt in attempts if getattr(attempt, "published", False)
            )
        return False

    # ------------------------------------------------------------------
    # 单条待确认
    # ------------------------------------------------------------------

    def _advance(self, readiness: ReadinessFollowUp, item: Any, tally: _Tally) -> None:
        """把一条确认推进至多一步，并在它收口时决定要不要通知。

        ``readiness`` 由 :meth:`run_once` 取好再传进来，而不是在这里读 ``self``：
        那一面可能整个没装配，把"它一定在"写成断言等于让一条不总成立的保证承担
        类型收窄。**收件人先查，再推进**：次序反过来的话，收件人查询的一次瞬时
        数据库异常会发生在终态判定已经落库之后，那条确认从此被候选集永久排除、
        用户永远收不到通知。先查则相反：查询失败就本轮不推进、终态不落、下一轮
        原样重来，代价只是"还没到期"的候选多一次只读查询，是有界的。
        """

        binding = ReadinessBinding(user_id=item.user_id, permission_version=item.permission_version)
        # 先查收件人：这一步失败不留任何终态，下一轮重来。
        open_id = self._intents.notice_recipient_open_id(item.user_id)
        progress = ReadinessProgress.from_checks(
            readiness.checks.load_checks(item.user_id, item.permission_version)
        )
        attempt = readiness.ticker.advance(binding, permissions=item.permissions, progress=progress)
        if attempt is None:
            # 还没到期、已经收口，或探针未接线：本轮一次外部调用都不发。
            return
        tally.advanced += 1
        if attempt.outcome is ReadinessOutcome.READY:
            tally.ready += 1
            self._notify(readiness, item, open_id, tally)
            return
        if attempt.outcome is ReadinessOutcome.NO_PERMISSION:
            # 撤权那一路：没有可等的就绪，发布读回一致本身就是通知的触发点。
            tally.revoked += 1
            self._notify(readiness, item, open_id, tally)
            return
        if attempt.outcome is ReadinessOutcome.TIMED_OUT:
            # **不通知**：我们没能确认这个人真的可以问数（模块文档「四条边界」）。
            # 但它必须留下一条**可告警的事实**——否则刷新链的超时只剩日志与计数，
            # 而 S-C-02 的阻塞路径是有告警出口的（`on_alert`）。
            tally.timed_out += 1
            self._alert("permission_readiness_timed_out", item)

    def _notify(
        self, readiness: ReadinessFollowUp, item: Any, open_id: str | None, tally: _Tally
    ) -> None:
        """发一条权限变化通知。**这一步失败不回头改任何状态。**

        通知种类由 ``render_scope_notice`` 从 ``permissions`` 文本自己判定，与
        就绪结论用同一个存在性判据，因此不会出现自相矛盾的组合。发送前的每一条
        异常路径都留一条 ``permission_notice.failed`` 再照常上抛——吞掉它等于
        把可修的 bug 变成"这个人偶尔收不到通知"。**残余窗口，如实登记**：终态
        已落库、通知还没发出去时进程崩溃，这条通知就永久丢了；不做通知 outbox
        是刻意的，要求的是"有限重试 + 审计"，不是 exactly-once。
        """

        if not open_id:
            # 还没开通完、已停用或正在删除的账号：不发，只计数与留痕。
            tally.notices_skipped += 1
            self._audit.record(
                "permission_notice.recipient_unavailable",
                user=item.user_id,
                permission_version=item.permission_version,
            )
            return
        try:
            result = readiness.notices.notify(
                user_id=item.user_id,
                open_id=open_id,
                permission_version=item.permission_version,
                permissions=item.permissions,
            )
        except Exception as error:  # 记完通知侧的痕迹再上抛
            tally.notices_failed += 1
            self._audit.record(
                "permission_notice.failed",
                user=item.user_id,
                permission_version=item.permission_version,
                # 只记异常类型：异常正文可能带上正文或收件人。
                error_code=type(error).__name__,
                stage="render_or_dispatch",
            )
            raise
        if result.delivered:
            tally.notices_sent += 1
        else:
            tally.notices_failed += 1

    def _alert(self, kind: str, item: Any) -> None:
        """把一条可告警事实交给注入的出口；**回调失败不改变任何结果**。"""

        if self._on_alert is None:
            return
        try:
            self._on_alert(kind, item.user_id)
        except Exception as error:  # 观察者不是这条链的一部分
            logger.error("权限就绪告警回调失败 error=%s", type(error).__name__)


def _build_permission_publish_duty(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
    permission_table_access_token: Callable[[], str] | None,
    on_management_corrections: Callable[[], None] | None = None,
) -> PermissionPublishDuty | None:
    """装配权限发布消费职责；**三个面各按自身依赖装配，缺谁只停谁**。

    形状照 :func:`_roster_audit_missing_prerequisite`：缺项只报变量名、每一面
    恰一条审计、其余职责照常运行。**缺发布面前置时职责仍然注册**，只要就绪/
    通知那一面装得起来：已经发布出去的权限还等着被确认、被通知，没有理由因为
    "暂时写不了新的一行"就把它们一起停掉；反过来 MCP 端点没配时发布照常。
    两个面都装不起来才不注册。
    """

    executor, unwired, store = _build_publish_executor(
        config, permission_table_access_token, audit=audit
    )

    # 延迟导入（而不是模块顶层）：`permission_readiness_assembly` 反向 import 本模块的
    # `ReadinessFollowUp`（构造它要用到），模块顶层互相 import 会成环；两个模块分开
    # 的理由本身见 `permission_readiness_assembly` 的模块文档——它承载的
    # `sleep=stop.wait` 会被本文件 `NonBlockingTest` 的全文件级否定扫描连坐命中。
    from lingxi.apps.scheduler.permission_readiness_assembly import _build_readiness_follow_up

    readiness = _build_readiness_follow_up(config, audit=audit, stop=stop)
    if executor is None and readiness is None:
        # 两面都装不起来：注册一个什么都做不了的职责只会每分钟记一条空报告。
        reason, variable = unwired or ("unknown", "")
        facts = {"reason": reason}
        if variable:
            facts["variable"] = variable
        audit.record("permission_publish.duty_not_registered", **facts)
        logger.warning("权限发布与就绪确认两面都未装配，职责不注册；其余定时职责照常运行")
        return None
    if executor is None:
        reason, variable = unwired or ("unknown", "")
        facts = {"reason": reason}
        if variable:
            facts["variable"] = variable
        # **恰一条**：只说发布面没装配，就绪与通知面照常。
        audit.record("permission_publish.publish_not_wired", **facts)
        logger.warning("权限发布面未装配（%s），已发布权限的就绪确认与变化通知照常运行", reason)

    return PermissionPublishDuty(
        executor=executor,
        intents=store,
        audit=audit,
        readiness=readiness,
        on_alert=_permission_readiness_alert(audit),
        on_management_corrections=on_management_corrections,
        stop=stop,
    )


def _build_publish_executor(
    config: SchedulerConfig,
    permission_table_access_token: Callable[[], str] | None,
    *,
    audit: AuditSink,
) -> tuple[Any, tuple[str, str] | None, Any]:
    """装配发布面执行器；返回 ``(executor 或 None, 缺项原因, intents 存取口)``。

    发布 Base ``app_token``/``table_id`` 写哪张表不能进代码，只从环境变量来；
    第三个前置是发布表读写所用的短期令牌供给（``build_loop`` 默认总能建出一条，
    ``None`` 的含义是"调用方真的没有交出任何供给"，不是"还没接线"）。存取口
    ``store`` 无论发布面是否装得起来都要返回——就绪/通知面复用同一个实例。
    """

    unwired: tuple[str, str] | None = None
    for variable, value in (
        ("LINGXI_PERMISSION_BITABLE_APP_TOKEN", config.permission_app_token),
        ("LINGXI_PERMISSION_BITABLE_TABLE_ID", config.permission_table_id),
    ):
        if not value:
            unwired = ("missing_environment_variable", variable)
            break
    if unwired is None and permission_table_access_token is None:
        unwired = ("permission_table_access_token_unwired", "")

    from lingxi.adapters.postgres_permission_publish import PostgresPermissionPublishStore

    store = PostgresPermissionPublishStore(config.postgres_dsn, timeouts=config.postgres_timeouts)
    if unwired is not None:
        return None, unwired, store

    from lingxi.adapters.feishu_permission_bitable import BitablePermissionTable
    from lingxi.core.permission.publish import PermissionPublishExecutor

    executor = PermissionPublishExecutor(
        store=store,
        transport=BitablePermissionTable(
            base_url=config.feishu_base_url,
            app_token=config.permission_app_token,
            table_id=config.permission_table_id,
            access_token=permission_table_access_token,
        ),
        audit=audit,
    )
    return executor, unwired, store


def _permission_readiness_alert(audit: AuditSink) -> Callable[[str, str], None]:
    """刷新链就绪超时的告警出口：一条**可告警的结构化事实**。

    **刻意不接** ``core/alerting.py`` **的状态机**：那套状态机只认心跳、任务
    滞留与飞书发送连续失败三类信号，把"权限同步没能在十五分钟内确认"塞进其中
    任何一类，都会让阈值、去重与恢复计时同时失真。是否新增一类信号还是复用
    一类尚未决定；在那之前，先把事实留成可 grep、可进工单的一条审计 +
    一条 WARNING，不让刷新链的超时只剩计数。用户标识是内部 ULID，不含资料。
    """

    def report(kind: str, user_id: str) -> None:
        audit.record("permission_readiness.alert", kind=kind, user=user_id)
        logger.warning("权限就绪确认需要人工关注 kind=%s user=%s", kind, user_id)

    return report


__all__ = [
    "DEFAULT_PUBLISH_LIMIT",
    "DEFAULT_READINESS_LIMIT",
    "DEFAULT_ROUND_BUDGET_SECONDS",
    "FOLLOW_UP_REASONS",
    "PermissionPublishDuty",
    "PermissionPublishReport",
    "REVOKE_ONLY_REASONS",
    "ReadinessFollowUp",
]
