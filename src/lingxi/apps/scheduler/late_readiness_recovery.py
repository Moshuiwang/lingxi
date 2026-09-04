"""迟到就绪恢复职责：十五分钟同步超时之后仍然把用户捞回来确认。

首次开通链的阻塞式就绪确认十五分钟预算耗尽仍未成功就返回 ``timed_out``，
``provisioning_state`` 留在 ``mcp_syncing``，此前没有任何东西会再回来看这个
人。但十五分钟只是承诺给用户的等待上限，不是权限真的同步好的上限：问数 MCP
按自己的节奏（约十五分钟一次）拉取发布表，一次错过窗口的用户可能几分钟后
就真的就绪了。本模块周期性回看这些候选、重新探测；就绪就写 ``active`` 并
主动通知「开通完成」，在此之前绝不给任何暗示已经可用的消息。
判定复用 :class:`~lingxi.core.permission.mcp_readiness_tick.ReadinessRecoveryTicker`
（与阻塞/tick 式就绪确认同一份判定实现，不认终态防线），不新造"就绪"定义。
重试没有主动放弃期限，但实际生效窗口受 ``publish_outbox`` 内容快照九十天
保留期约束——超期候选查询会静默排除，因为渲染通知所需内容已被到期擦除。
状态推进与排通知在同一个数据库事务里完成，通知是持久 outbox、按既有退避
重试直到送达。已知边界：候选与通知认领都不带持久租约（单实例假设）。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from lingxi.apps.scheduler.audit import AuditSink
from lingxi.apps.scheduler.config import SchedulerConfig
from lingxi.core.identity.onboarding_runner import KEY_COMPLETED
from lingxi.core.permission.mcp_readiness_base import (
    ReadinessAttempt,
    ReadinessBinding,
    ReadinessOutcome,
    ReadinessSchedule,
)
from lingxi.core.permission.notification import describe_scope
from lingxi.core.permission.publish_row import parse_permissions

logger = logging.getLogger(__name__)

#: 复检节奏（秒）。见模块文档「节奏」一节：与问数 MCP 自己拉取发布表的周期同一个数量级，
#: 是本职责自己的工程选择，不是产品裁定。
DEFAULT_RECOVERY_INTERVAL_SECONDS = 900

#: 单轮最多处理多少个恢复候选。配额保护，不是重试上限——被推迟到下一轮的候选不丢失任何
#: 进度（进度全在 ``mcp_sync_check`` / ``app_user`` 里）。
DEFAULT_RECOVERY_LIMIT = 50

#: 单轮最多认领并发送多少条待发通知。与恢复候选的预算分开算——两者是不同的外部调用
#: （问数 MCP 探针 vs. 飞书私聊发送），互不挤占对方的配额。
DEFAULT_NOTICE_DRAIN_LIMIT = 50


class _Candidates(Protocol):
    """恢复候选的读取口（``adapters/postgres_late_readiness_recovery.py``）。"""

    def late_onboarding_recovery_candidates(
        self, *, reason: str, recovery_interval_seconds: int, limit: int = ...
    ) -> Sequence[Any]: ...


class _Ticker(Protocol):
    """就绪复检探针（``core/permission/mcp_readiness_tick.ReadinessRecoveryTicker``）。

    **可选**：缺问数 MCP 端点或令牌主密钥时装配层传 ``None``，需要真探针的候选本轮
    不推进、不落任何记录（模块文档「节奏」一节同一条纪律）。
    """

    def probe_after_timeout(
        self, binding: ReadinessBinding, *, attempt_no: int
    ) -> ReadinessAttempt | None: ...

    def record_processing_failure(
        self, binding: ReadinessBinding, *, attempt_no: int, code: str
    ) -> ReadinessAttempt: ...


class _Activator(Protocol):
    """就绪之后「推进 active + 排通知」的原子写口。

    实现是 ``adapters/postgres_late_readiness_recovery.PostgresLateReadinessStore.
    activate_after_late_readiness``。
    """

    def activate_after_late_readiness(
        self,
        *,
        user_id: str,
        expected_permission_version: int,
        company_name: str,
        function_name: str,
        dedupe_key: str,
        silent_system_trigger: bool = False,
    ) -> bool: ...


class _NoticeOutbox(Protocol):
    """待发「开通完成」通知的持久 outbox（同一适配器）。

    **只有两个终点**：送达（:meth:`mark_notice_delivered`）或留在 ``pending`` 按既有
    退避重试（:meth:`mark_notice_failed`，即使原因是"收件人暂时查不到"也一样）。
    **没有"永久放弃"这个动作**：真正的账号删除由 ``user_id`` 上的
    ``ON DELETE CASCADE`` 处理，不需要调用方自己判断"这是不是不可逆"——
    ``notice_recipient_open_id`` 的返回值分辨不出。
    """

    def claim_one_due_notice(self) -> Any | None: ...
    def mark_notice_delivered(self, notice_id: str) -> None: ...
    def mark_notice_failed(self, notice_id: str, *, error: str) -> None: ...


class _Recipients(Protocol):
    """通知收件人查询（``adapters/postgres_permission_publish.py``）。"""

    def notice_recipient_open_id(self, user_id: str) -> str | None: ...


class _Notifier(Protocol):
    """终态的主动私聊（``apps/scheduler/onboarding.CatalogNotifier``）。"""

    def send(
        self, *, open_id: str, key: str, values: Mapping[str, object], dedupe_key: str
    ) -> None: ...


class _AuditSink(Protocol):
    def record(self, action: str, /, **fields: object) -> None: ...


@dataclass(frozen=True)
class LateReadinessRecoveryReport:
    """一轮的结果。

    **只有计数与固定分类，没有任何字段值**（同 ``PermissionPublishReport``
    的纪律：内部用户标识、权限值、open_id 一个都不进报告）。
    """

    #: 本轮取到的恢复候选数（探针面）。
    examined: int = 0
    #: 本轮探到就绪的候选数。
    ready: int = 0
    #: 真正被推进到 ``active`` 的人数（用户发起的链同时排出待发通知；系统触发的
    #: 链改为静默挂起首聊补一句，另计入 :attr:`activated_silently`）。可能小于
    #: ``ready``（账号在这期间被停用，或权限版本已经变了，CAS 拒绝）。
    activated: int = 0
    #: 其中系统触发（预开通）**静默**完成的人数：不发「开通完成」私聊（预开通
    #: 全程静默、首聊时补一句）。单独计数是为了让「activated 有增长、通知面
    #: 却一条都没经手」读得通。
    activated_silently: int = 0
    advance_refused: int = 0
    waiting: int = 0
    technical_failures: int = 0
    #: 探针未接线、需要真探针才能推进的候选数（见 :class:`_Ticker` 的文档）。
    probe_unwired: int = 0
    failed: int = 0
    #: 本轮从通知 outbox 认领到的待发条数（通知面，与探针面的预算各自独立）。
    notices_claimed: int = 0
    notified: int = 0
    notice_failed: int = 0
    #: 收件人暂时查不到的次数。**不是**"永久放弃"的计数——这些通知仍然留在
    #: ``pending`` 按既有退避重试，这里只是一个可观测的分类，不代表已经收口。
    notice_recipient_unavailable: int = 0
    interrupted: bool = False
    #: 探针面装配了没有。缺问数 MCP 端点或令牌主密钥时是 ``False``——本职责仍然注册，
    #: 只是需要真探针的那一路本轮不推进，报告里必须看得出来，否则"候选一直没有进展"
    #: 读起来会像卡死。
    probe_wired: bool = True

    def audit_facts(self) -> dict[str, Any]:
        """把计数字段展开成一份可以直接喂给审计记录的字典。"""
        facts: dict[str, Any] = {
            "examined": self.examined,
            "ready": self.ready,
            "activated": self.activated,
            "activated_silently": self.activated_silently,
            "advance_refused": self.advance_refused,
            "waiting": self.waiting,
            "technical_failures": self.technical_failures,
            "probe_unwired": self.probe_unwired,
            "failed": self.failed,
            "notices_claimed": self.notices_claimed,
            "notified": self.notified,
            "notice_failed": self.notice_failed,
            "notice_recipient_unavailable": self.notice_recipient_unavailable,
            "probe_wired": self.probe_wired,
        }
        if self.interrupted:
            facts["interrupted"] = True
        return facts


@dataclass
class _Tally:
    """累加器。:class:`LateReadinessRecoveryReport` 是冻结的（它会进审计）。"""

    examined: int = 0
    ready: int = 0
    activated: int = 0
    activated_silently: int = 0
    advance_refused: int = 0
    waiting: int = 0
    technical_failures: int = 0
    probe_unwired: int = 0
    failed: int = 0
    notices_claimed: int = 0
    notified: int = 0
    notice_failed: int = 0
    notice_recipient_unavailable: int = 0

    def freeze(self, *, interrupted: bool, probe_wired: bool) -> LateReadinessRecoveryReport:
        return LateReadinessRecoveryReport(
            examined=self.examined,
            ready=self.ready,
            activated=self.activated,
            activated_silently=self.activated_silently,
            advance_refused=self.advance_refused,
            waiting=self.waiting,
            technical_failures=self.technical_failures,
            probe_unwired=self.probe_unwired,
            failed=self.failed,
            notices_claimed=self.notices_claimed,
            notified=self.notified,
            notice_failed=self.notice_failed,
            notice_recipient_unavailable=self.notice_recipient_unavailable,
            interrupted=interrupted,
            probe_wired=probe_wired,
        )

    @property
    def anything_happened(self) -> bool:
        """本轮有没有任何值得记一条完成审计的事。

        **零候选、零待发通知**时不记，本职责每轮都跑，不去重的话会在健康
        系统里刷出大量空审计。
        """
        return bool(self.examined or self.notices_claimed)


class LateReadinessRecoveryDuty:
    """每轮 tick 分两个互相独立、失败互不影响的阶段。

    **探针阶段**：取到期候选 → 按需再探一次 → 就绪就原子地推进 ``active`` + 排一条
    待发通知。**通知阶段**：认领到期的待发通知 → 发送 → 送达就标记 ``delivered``，
    失败留错误码等下一次到期重试。语义与边界见模块文档；本类**只编排**：候选查询、
    原子推进、通知 outbox 在 :mod:`lingxi.adapters.postgres_late_readiness_recovery`，
    判定在 :mod:`lingxi.core.permission.mcp_readiness_base`，这里一条规则都不复制。
    """

    name = "迟到就绪恢复"

    def __init__(
        self,
        *,
        candidates: _Candidates,
        activator: _Activator,
        notices: _NoticeOutbox,
        recipients: _Recipients,
        notifier: _Notifier,
        audit: _AuditSink,
        reason: str,
        ticker: _Ticker | None = None,
        recovery_interval_seconds: int = DEFAULT_RECOVERY_INTERVAL_SECONDS,
        limit: int = DEFAULT_RECOVERY_LIMIT,
        notice_limit: int = DEFAULT_NOTICE_DRAIN_LIMIT,
        stop: threading.Event | None = None,
    ) -> None:
        """按注入的候选/写口/通知协作者装配一个迟到就绪恢复职责实例。"""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("必须指明本职责负责恢复哪一类发布意图")
        if (
            isinstance(recovery_interval_seconds, bool)
            or not isinstance(recovery_interval_seconds, int)
            or recovery_interval_seconds < 1
        ):
            raise ValueError("复检节奏必须是正整数秒")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("单轮候选上限必须是正整数")
        if isinstance(notice_limit, bool) or not isinstance(notice_limit, int) or notice_limit < 1:
            raise ValueError("单轮通知上限必须是正整数")
        self._candidates = candidates
        self._ticker = ticker
        self._activator = activator
        self._notices = notices
        self._recipients = recipients
        self._notifier = notifier
        self._audit = audit
        self._reason = reason.strip()
        self._interval = recovery_interval_seconds
        self._limit = limit
        self._notice_limit = notice_limit
        self._stop = threading.Event() if stop is None else stop

    @property
    def probe_wired(self) -> bool:
        """探针面装配了没有。见 :class:`_Ticker` 的文档。"""
        return self._ticker is not None

    @property
    def stopping(self) -> bool:
        """是否已收到停止信号。"""
        return self._stop.is_set()

    def request_stop(self) -> None:
        """置位停止信号：本轮及之后不再推进新的候选。"""
        self._stop.set()

    # ------------------------------------------------------------------
    # 一轮
    # ------------------------------------------------------------------

    def run_once(self) -> LateReadinessRecoveryReport | None:
        """跑一轮。返回 ``None`` 表示本轮没有执行（停止中）。

        **本方法不会等待**：探针面与通知面都是 tick 驱动，单次外部调用最长的等待是
        它自己的传输超时；候选查询与通知认领都已经把没到期的挡在外面，一轮什么都不
        到期时只是两次空查询。
        """
        if self._stop.is_set():
            return None

        tally = _Tally()
        interrupted = self._advance_candidates(tally)
        if not interrupted and not self._stop.is_set():
            interrupted = self._drain_notices(tally) or interrupted

        report = tally.freeze(interrupted=interrupted, probe_wired=self.probe_wired)
        if tally.anything_happened:
            # 零候选、零待发通知时不记审计：本职责每轮都跑，健康系统里绝大多数
            # tick 什么都不该做，无条件记审计只会刷出海量空事实。
            self._audit.record("late_readiness_recovery.completed", **report.audit_facts())
            logger.info(
                "迟到就绪恢复完成 候选=%s 就绪=%s 已推进=%s 等待=%s 技术失败=%s "
                "探针未接线=%s 推进被拒=%s 失败=%s 已认领通知=%s 已送达=%s "
                "通知失败=%s 收件人暂不可用=%s",
                report.examined,
                report.ready,
                report.activated,
                report.waiting,
                report.technical_failures,
                report.probe_unwired,
                report.advance_refused,
                report.failed,
                report.notices_claimed,
                report.notified,
                report.notice_failed,
                report.notice_recipient_unavailable,
            )
        return report

    # ------------------------------------------------------------------
    # 探针阶段
    # ------------------------------------------------------------------

    def _advance_candidates(self, tally: _Tally) -> bool:
        """取到期候选并逐个推进。返回本轮是否被停止信号中断。"""
        candidates = self._candidates.late_onboarding_recovery_candidates(
            reason=self._reason,
            recovery_interval_seconds=self._interval,
            limit=self._limit,
        )
        for item in candidates:
            if self._stop.is_set():
                # 停止信号落在遍历中间：不再为后面的人探针。已经落库的判定各自是一个
                # 完整事务；下一次启动会从库里把进度原样读回来。
                return True
            tally.examined += 1
            try:
                self._recover_one(item, tally)
            except Exception as error:  # 一个用户的失败不得带走整轮
                # 只记异常类型：异常正文可能带上被处理对象的内容。
                tally.failed += 1
                self._audit.record(
                    "late_readiness_recovery.user_failed",
                    user=item.user_id,
                    error=type(error).__name__,
                )
                logger.error(
                    "单个用户的迟到就绪恢复失败，其余用户继续 user=%s error=%s",
                    item.user_id,
                    type(error).__name__,
                )
        return False

    def _recover_one(self, item: Any, tally: _Tally) -> None:
        """把一个候选推进至多一步：再探一次 → 就绪就原子推进 ``active`` + 排通知。

        **不得把还没就绪的人写成 ``active``，也不得给他任何暗示已经可用的消息**：
        ``waiting``/``technical_failure``/探针未接线三路都在这里**显式返回**。
        **候选一律重新探一次**：不存在"跳过探针"的分支。探针调用与探针之后的
        步骤分别加保护，各自用发起时刻真实的 ``attempt_no``，避免一个抛异常
        的候选在下一个 tick 立刻重入、饿死其余候选。
        """
        binding = ReadinessBinding(user_id=item.user_id, permission_version=item.permission_version)
        if self._ticker is None:
            # 探针未接线：本轮不落任何记录，端点配好后从库里的进度原样继续。
            tally.probe_unwired += 1
            return
        try:
            attempt = self._ticker.probe_after_timeout(binding, attempt_no=item.next_attempt_no)
        except Exception as error:  # 占住窗口，再让外层记 failed 并上抛
            self._ticker.record_processing_failure(
                binding,
                attempt_no=item.next_attempt_no,
                code=f"recovery_failed_{type(error).__name__}",
            )
            raise
        if attempt is None:
            tally.probe_unwired += 1
            return
        if attempt.outcome is ReadinessOutcome.WAITING:
            tally.waiting += 1
            return
        if attempt.outcome is not ReadinessOutcome.READY:
            # ``technical_failure``：探针没跑通，任何数字都是假的，绝不能凑成就绪。
            # 计划表已经不存在，本方法没有第三条可能的结论。
            tally.technical_failures += 1
            return
        tally.ready += 1
        self._advance_ready_candidate(item, binding, tally)

    def _advance_ready_candidate(self, item: Any, binding: ReadinessBinding, tally: _Tally) -> None:
        """探针确认就绪之后，原子推进 ``active`` + 排通知，并按结果收尾。

        状态推进与排通知在同一个原子事务里完成，CAS 要求 permission_version
        与探针绑定的那一版完全一致，防止拿一版已经过时的 ready 判定把人写成
        active。探针之后的步骤一旦抛出未预期异常，占住这一次调度窗口再上抛，
        用**下一个** attempt_no（探针那一次已经真的成功记过账了）。
        """
        try:
            company, function = describe_scope(parse_permissions(item.permissions))
            dedupe_key = f"onboarding:recovery:{item.user_id}:{item.permission_version}"
            activated = self._activator.activate_after_late_readiness(
                user_id=item.user_id,
                expected_permission_version=item.permission_version,
                company_name=company,
                function_name=function,
                dedupe_key=dedupe_key,
                # 系统触发（预开通）的链**不发**「开通完成」私聊——全程静默、首聊
                # 时补一句；适配器在同一个原子事务里改挂首聊补一句。用户自己发起
                # 的链一字不变。
                silent_system_trigger=item.system_triggered,
            )
        except Exception as error:  # 占住窗口，再让外层记 failed 并上抛
            self._ticker.record_processing_failure(
                binding,
                attempt_no=item.next_attempt_no + 1,
                code=f"recovery_failed_{type(error).__name__}",
            )
            raise

        if not activated:
            # CAS 失败：这个人在候选查到与这里之间被停用、权限版本变了，或已经被
            # 别的路径推进过。**不发任何通知**——advance_refused 不是终态，下一轮
            # 候选查询会按当时的真实状态重新判断。
            tally.advance_refused += 1
            self._audit.record("late_readiness_recovery.advance_refused", user=item.user_id)
            return
        tally.activated += 1
        if item.system_triggered:
            # 静默完成：没有任何通知会进 outbox，这条审计是这次恢复在观测面上
            # 唯一的"完成"记录，缺了它运维只能从状态对比里猜。
            tally.activated_silently += 1
            self._audit.record("late_readiness_recovery.activated_silently", user=item.user_id)
        # 用户发起的链：通知已经在同一个事务里排进 outbox（or 已存在同 dedupe_key 的
        # 一条），发送由 _drain_notices 独立完成——这里不再做任何发送尝试。

    # ------------------------------------------------------------------
    # 通知阶段
    # ------------------------------------------------------------------

    def _drain_notices(self, tally: _Tally) -> bool:
        """认领并发送到期的待发通知。返回本轮是否被停止信号中断。"""
        for _ in range(self._notice_limit):
            if self._stop.is_set():
                return True
            notice = self._notices.claim_one_due_notice()
            if notice is None:
                # 没有到期的了：正常收工，不是中断。
                return False
            tally.notices_claimed += 1
            try:
                self._send_notice(notice, tally)
            except Exception as error:  # 一条通知的失败不得带走整轮
                tally.failed += 1
                self._audit.record(
                    "late_readiness_recovery.notice_processing_failed",
                    user=notice.user_id,
                    error=type(error).__name__,
                )
                logger.error(
                    "单条待发通知处理失败，其余通知继续 user=%s error=%s",
                    notice.user_id,
                    type(error).__name__,
                )
        return False

    def _send_notice(self, notice: Any, tally: _Tally) -> None:
        open_id = self._recipients.notice_recipient_open_id(notice.user_id)
        if not open_id:
            # **不得**转永久放弃。``notice_recipient_open_id`` 只能回答"这一刻查
            # 不到"，答不出"这是不是永久性的"——账号被停用完全可能只是暂时的。
            # 提前判死会让一个已经 active 的人永远等不到这句话。真正的"不用再
            # 等了"只有 `ON DELETE CASCADE` 一种事实来源，因此这里与发送失败走
            # 同一条路：留在 pending，按既有退避重试。
            self._notices.mark_notice_failed(notice.notice_id, error="recipient_unavailable")
            tally.notice_recipient_unavailable += 1
            self._audit.record("late_readiness_recovery.recipient_unavailable", user=notice.user_id)
            return
        try:
            self._notifier.send(
                open_id=open_id,
                key=KEY_COMPLETED,
                values={
                    "company_name": notice.company_name,
                    "function_name": notice.function_name,
                },
                dedupe_key=notice.dedupe_key,
            )
        except Exception as error:  # 记完错误码再留在 pending 等下次到期
            self._notices.mark_notice_failed(notice.notice_id, error=type(error).__name__)
            tally.notice_failed += 1
            self._audit.record(
                "late_readiness_recovery.notify_failed",
                user=notice.user_id,
                error=type(error).__name__,
            )
            logger.warning(
                "「开通完成」通知发送失败，将在下一次到期时重试 user=%s error=%s",
                notice.user_id,
                type(error).__name__,
            )
            return
        self._notices.mark_notice_delivered(notice.notice_id)
        tally.notified += 1


def _build_late_readiness_recovery_duty(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
) -> Any:
    """装配迟到就绪恢复职责。**不需要任何可选前置就能注册。**

    补的是首次开通那次阻塞式就绪确认判超时之后的缺口：``provisioning_state``
    停在 ``mcp_syncing``，此前没有任何东西会再回来看这个人。语义、节奏、要试
    到什么时候为止，见模块文档；本函数只做装配。候选查询、原子推进与通知都
    只需要 ``LINGXI_POSTGRES_DSN``/飞书应用凭据（两者都是必填项），因此本职责
    **总能**注册。**唯一可选的是探针面**：缺 MCP 令牌主密钥或问数 MCP 端点时，
    需要真探针才能推进的那一路本轮不推进，只留**恰一条**审计；通知面（认领
    已排出的待发通知并重试直到送达）不依赖探针，照常运行。
    """
    from lingxi.adapters.feishu_user_message import FeishuUserMessages
    from lingxi.adapters.postgres_late_readiness_recovery import PostgresLateReadinessStore
    from lingxi.adapters.postgres_permission_publish import PostgresPermissionPublishStore
    from lingxi.apps.scheduler.onboarding import CatalogNotifier
    from lingxi.config.content import default_content_catalog
    from lingxi.core.identity.onboarding_runner import FIRST_ONBOARDING_REASON

    dsn = config.postgres_dsn
    timeouts = config.postgres_timeouts
    store = PostgresLateReadinessStore(dsn, timeouts=timeouts)
    # 通知收件人查询是既有的只读方法，住在权限发布那份存取里
    # （``notice_recipient_open_id``，与刷新链的变化通知共用同一条产品口径）。
    recipients = PostgresPermissionPublishStore(dsn, timeouts=timeouts)
    ticker = _build_late_readiness_ticker(config, audit=audit)

    duty = LateReadinessRecoveryDuty(
        candidates=store,
        ticker=ticker,
        activator=store,
        notices=store,
        recipients=recipients,
        notifier=CatalogNotifier(
            sender=FeishuUserMessages(
                base_url=config.feishu_base_url,
                app_id=config.feishu_app_id,
                app_secret=config.feishu_app_secret,
            ),
            catalog=default_content_catalog(),
        ),
        audit=audit,
        reason=FIRST_ONBOARDING_REASON,
        stop=stop,
    )
    logger.info("迟到就绪恢复职责已装配 探针面=%s", "已接线" if ticker is not None else "未接线")
    return duty


def _build_late_readiness_ticker(config: SchedulerConfig, *, audit: AuditSink) -> Any:
    """装配探针面；缺 MCP 令牌主密钥或问数 MCP 端点时只留**恰一条**审计并返回 ``None``。"""
    if not config.mcp_token_encrypt_key:
        from lingxi.adapters.mcp_token_cipher import MASTER_KEY_ENV

        # 只报变量名，不回显任何值（它还是一把主密钥）。
        audit.record(
            "late_readiness_recovery.probe_not_wired",
            reason="missing_environment_variable",
            variable=MASTER_KEY_ENV,
        )
        logger.warning(
            "未配置 %s，迟到就绪恢复的探针面不装配；候选留在库里等配置齐备，"
            "已经排出但还没送达的通知照常持久重试",
            MASTER_KEY_ENV,
        )
        return None
    if not config.query_mcp_endpoint:
        audit.record(
            "late_readiness_recovery.probe_not_wired",
            reason="missing_environment_variable",
            variable="LINGXI_QUERY_MCP_ENDPOINT",
        )
        logger.warning(
            "未配置 LINGXI_QUERY_MCP_ENDPOINT，迟到就绪恢复的探针面不装配；"
            "候选留在库里等端点配好，已经排出但还没送达的通知照常持久重试"
        )
        return None

    from lingxi.adapters.mcp_token_cipher import McpTokenCipher
    from lingxi.adapters.postgres_mcp_token import PostgresMcpTokenStore, token_cipher_provider
    from lingxi.adapters.query_mcp_probe import QueryMcpProbe, content_text_metrics_reader
    from lingxi.apps.scheduler.onboarding import assert_probe_timeouts_agree
    from lingxi.core.permission.mcp_readiness_tick import ReadinessRecoveryTicker

    dsn = config.postgres_dsn
    timeouts = config.postgres_timeouts
    tokens = PostgresMcpTokenStore(
        dsn, cipher=McpTokenCipher(config.mcp_token_encrypt_key), timeouts=timeouts
    )
    schedule = ReadinessSchedule(probe_timeout_seconds=config.query_mcp_timeout_seconds)
    probe = QueryMcpProbe(
        endpoint=config.query_mcp_endpoint,
        token_provider=token_cipher_provider(tokens),
        timeout_seconds=config.query_mcp_timeout_seconds,
        # 已验证的 reader：真实 MCP 的 list_metrics 返回没有 structuredContent。
        metrics_reader=content_text_metrics_reader,
    )
    assert_probe_timeouts_agree(probe=probe, schedule=schedule)
    return ReadinessRecoveryTicker(
        probe=probe,
        store=tokens,
        audit=audit,
        clock=lambda: datetime.now(UTC),
        schedule=schedule,
    )


__all__ = [
    "DEFAULT_NOTICE_DRAIN_LIMIT",
    "DEFAULT_RECOVERY_INTERVAL_SECONDS",
    "DEFAULT_RECOVERY_LIMIT",
    "LateReadinessRecoveryDuty",
    "LateReadinessRecoveryReport",
]
