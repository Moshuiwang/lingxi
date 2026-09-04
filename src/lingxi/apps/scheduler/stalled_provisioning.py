"""开通中途停摆收口职责：认领超过租约、仍卡在中途格的用户最终一定离开 ``provisioning``/``mcp_syncing``。

首次开通链推进到 ``provisioning`` 后死掉（进程被强杀、执行线程异常退出、
「当场收口」自己那一次也失败）时，此前没有任何东西会再回来看这个人——用户
只收到「正在完成…请稍候」。本职责周期性回看这些候选，处置顺序**先通知、
通知送达才原子 CAS 收口成 aborted**（与迟到就绪恢复方向相反：那边缺的是
"推进却不告知"，这边缺的是"收口却不告知"会让用户从此收不到任何消息，
"自动重试"这条自愈路径因此失效）；系统触发（预开通）的候选静默收口、不发
通知。停摆租约默认 45 分钟，由装配断言 :func:`~lingxi.apps.scheduler.
onboarding.assert_stalled_lease_exceeds_chain_budget` 守住必须严格长于一条
链的最长处理时间。形状对称于 :class:`~lingxi.apps.scheduler.
late_readiness_recovery.LateReadinessRecoveryDuty`；候选与通知退避均按单
实例假设设计，不带持久租约；其余已知边界见类与方法自己的文档字符串。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from lingxi.apps.scheduler.audit import AuditSink
from lingxi.apps.scheduler.config import SchedulerConfig
from lingxi.core.identity.onboarding_ports import FailureReasonRecorder
from lingxi.core.identity.onboarding_runner import (
    KEY_STALLED,
    STATE_MCP_SYNCING,
    STATE_PROVISIONING,
)
from lingxi.core.identity.preprovision import is_system_trigger
from lingxi.core.permission.mcp_readiness_base import ReadinessSchedule

logger = logging.getLogger(__name__)

#: 停摆租约（秒）。见模块文档「判据」一节：由装配断言
#: ``assert_stalled_lease_exceeds_chain_budget`` 守住，必须严格长于一条链在
#: provisioning/mcp_syncing 两格上可能停留的最长时间。
DEFAULT_STALLED_LEASE_SECONDS = 2700

#: 单轮最多**处理**（真的发起通知/收口）多少个停摆候选。配额保护，不是重试
#: 上限——被推迟到下一轮的候选不丢失任何进度（进度全在 ``inbound_event``/
#: ``app_user`` 里）。
DEFAULT_STALLED_LIMIT = 50

#: 同一个用户两次通知尝试之间的最短间隔（秒）。``SchedulerLoop`` 的 tick 周期约
#: 一分钟，而候选查询在通知持续失败、状态没有变化时会每一轮都重新选中同一个人，
#: 不节流就是无上界地每分钟打一次飞书。退避状态只存在进程内存里（单实例假设），
#: 进程重启最多让下一轮多试一次，不是正确性问题。
DEFAULT_NOTIFY_RETRY_BACKOFF_SECONDS = 300

#: 候选查询的**过采样**倍数：查询按最早认领优先排序，如果排在最前面的若干候选
#: 恰好都在退避期内（持续通知失败的"毒候选"），只取 ``limit`` 条会让它们把整轮
#: 名额占满，后面真正等待处理的新候选永远排不上号。查询多取几倍、在进程内跳过
#: 退避中的候选后再截到 ``limit``，用一次查询多扫描几行的成本换掉这个饿死风险。
STALLED_FETCH_OVERSAMPLE = 4
#: 过采样查询量的硬上限，避免候选集合万一真的很大时一次查询扫描过多行。**这同时
#: 是饿死保护本身的上限**：如果同一时刻处于退避期的老候选数量超过这个上限，新
#: 候选会被再次挤出结果集之外。当前规模下（停摆是罕见事件）不会触及，一旦触及
#: 说明积压的停摆候选已经远超正常水位，需要先处理积压本身，而不是继续加大上限。
_MAX_FETCH_LIMIT = 200

_EXPECTED_STATES: tuple[str, ...] = (STATE_PROVISIONING, STATE_MCP_SYNCING)


class _Candidates(Protocol):
    """停摆候选的读取口（``adapters/postgres_stalled_provisioning.py``）。"""

    def stalled_provisioning_candidates(
        self, *, lease_seconds: int, limit: int = ...
    ) -> Sequence[Any]: ...


class _Aborter(Protocol):
    """收口写口。

    实现是 ``adapters/postgres_identity.PostgresAppUserStore.
    abort_stalled_provisioning``，与编排层「当场收口」共用同一份实现。
    """

    def abort_stalled_provisioning(
        self, *, user_id: str, expected_states: Sequence[str], reason: str
    ) -> bool: ...


class _Notifier(Protocol):
    """终态的主动私聊（``apps/scheduler/onboarding.CatalogNotifier``）。

    **可选**：见模块文档「缺通知出口时的姿态」。缺失时本轮一条候选都不处理。
    """

    def send(
        self, *, open_id: str, key: str, values: Mapping[str, object], dedupe_key: str
    ) -> None: ...


class _AuditSink(Protocol):
    def record(self, action: str, /, **fields: object) -> None: ...


@dataclass(frozen=True)
class StalledProvisioningReport:
    """一轮的结果。

    **只有计数与固定分类，没有任何字段值**（同 ``LateReadinessRecoveryReport``
    的纪律：内部用户标识、open_id 一个都不进报告）。
    """

    #: 本轮**真正处理**（发起了通知尝试）的候选数——**不等于**本轮候选查询取到的
    #: 行数：过采样查询取回的候选里，处于通知退避期的那些被跳过、不计入这里，
    #: 改记 :attr:`skipped_in_backoff`。
    examined: int = 0
    #: 通知发送成功的候选数。
    notified: int = 0
    #: 真正被收口成 ``aborted`` 的候选数（通知送达且 CAS 命中）。
    aborted: int = 0
    #: 通知发送失败的候选数——**不收口**，留在原状态等下一轮重来。
    notify_failed: int = 0
    #: 通知送达但 CAS 返回 0 行（状态在候选查到与收口之间被别的路径改写）。
    advance_refused: int = 0
    failed: int = 0
    #: 因为处于通知退避期而被跳过、本轮**没有**真正处理的候选数。与
    #: :attr:`examined` 分开计数：如果不单独记下来，一轮里查询取到的候选全部
    #: 恰好都在退避期时 :attr:`examined` 会是 0，运维会误以为"这一轮没有任何
    #: 停摆候选"，而真实情况是"有人还停在中途格，只是刚打过一次飞书"。
    skipped_in_backoff: int = 0
    #: 系统触发（预开通）的候选：**不发通知**、静默收口的人数。单独计数是为了
    #: 让「aborted 大于 notified」读得通——「先通知、送达才收口」的既有不变量
    #: 只约束用户自己发起的链；预开通用户没有发起任何东西、也没有在等任何消息
    #: （全程静默），对他收口不需要也不允许先说一句「你发起的开通……」的假话。
    silenced_system: int = 0
    interrupted: bool = False
    #: 通知出口装配了没有。见模块文档「缺通知出口时的姿态」。
    notifier_wired: bool = True

    def audit_facts(self) -> dict[str, Any]:
        """把计数字段展开成一份可以直接喂给审计记录的字典。"""
        facts: dict[str, Any] = {
            "examined": self.examined,
            "notified": self.notified,
            "aborted": self.aborted,
            "notify_failed": self.notify_failed,
            "advance_refused": self.advance_refused,
            "failed": self.failed,
            "skipped_in_backoff": self.skipped_in_backoff,
            "silenced_system": self.silenced_system,
            "notifier_wired": self.notifier_wired,
        }
        if self.interrupted:
            facts["interrupted"] = True
        return facts


@dataclass
class _Tally:
    examined: int = 0
    notified: int = 0
    aborted: int = 0
    notify_failed: int = 0
    advance_refused: int = 0
    failed: int = 0
    skipped_in_backoff: int = 0
    silenced_system: int = 0

    def freeze(self, *, interrupted: bool, notifier_wired: bool) -> StalledProvisioningReport:
        return StalledProvisioningReport(
            examined=self.examined,
            notified=self.notified,
            aborted=self.aborted,
            notify_failed=self.notify_failed,
            advance_refused=self.advance_refused,
            failed=self.failed,
            skipped_in_backoff=self.skipped_in_backoff,
            silenced_system=self.silenced_system,
            interrupted=interrupted,
            notifier_wired=notifier_wired,
        )


class StalledProvisioningDuty:
    """按认领时间 + 租约周期性回看停在 ``provisioning``/``mcp_syncing`` 的用户。

    语义与边界见模块文档。本类**只编排**：候选查询在
    :mod:`lingxi.adapters.postgres_stalled_provisioning`，收口写入复用
    :meth:`~lingxi.adapters.postgres_identity.PostgresAppUserStore.
    abort_stalled_provisioning`，通知正文在 :mod:`lingxi.config.content`，这里一条
    规则都不复制。
    """

    name = "开通中途停摆收口"

    def __init__(
        self,
        *,
        candidates: _Candidates,
        aborter: _Aborter,
        audit: _AuditSink,
        notifier: _Notifier | None = None,
        alert: Callable[[int], None] | None = None,
        failure_reasons: FailureReasonRecorder | None = None,
        lease_seconds: int = DEFAULT_STALLED_LEASE_SECONDS,
        limit: int = DEFAULT_STALLED_LIMIT,
        notify_backoff_seconds: int = DEFAULT_NOTIFY_RETRY_BACKOFF_SECONDS,
        clock: Callable[[], float] | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        """按注入的候选查询/收口写口装配一个开通中途停摆收口职责实例。"""
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds < 1
        ):
            raise ValueError("停摆租约必须是正整数秒")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("单轮候选上限必须是正整数")
        if (
            isinstance(notify_backoff_seconds, bool)
            or not isinstance(notify_backoff_seconds, int)
            or notify_backoff_seconds < 0
        ):
            raise ValueError("通知退避必须是非负整数秒")
        self._candidates = candidates
        self._aborter = aborter
        self._notifier = notifier
        self._alert = alert
        self._audit = audit
        #: 失败原因落库口（可选，见 ``FailureReasonRecorder`` 协议文档）。
        #: ``None``＝未装配，行为与接线之前逐字节一致。
        self._failure_reasons = failure_reasons
        self._lease_seconds = lease_seconds
        self._limit = limit
        self._notify_backoff_seconds = notify_backoff_seconds
        self._clock = clock or time.monotonic
        #: 每个用户最近一次被**尝试**处理（无论成功与否）的单调时刻。仅进程内存，
        #: 见 ``DEFAULT_NOTIFY_RETRY_BACKOFF_SECONDS`` 的文档。
        self._last_attempt: dict[str, float] = {}
        self._stop = threading.Event() if stop is None else stop

    @property
    def notifier_wired(self) -> bool:
        """通知出口装配了没有。见模块文档「缺通知出口时的姿态」。"""
        return self._notifier is not None

    @property
    def stopping(self) -> bool:
        """是否已收到停止信号。"""
        return self._stop.is_set()

    def request_stop(self) -> None:
        """置位停止信号：本轮及之后不再领取新的候选。"""
        self._stop.set()

    # ------------------------------------------------------------------
    # 一轮
    # ------------------------------------------------------------------

    def run_once(self) -> StalledProvisioningReport | None:
        """跑一轮。返回 ``None`` 表示本轮没有执行（停止中）。"""
        if self._stop.is_set():
            return None

        if self._notifier is None:
            # **绝不"改状态不告知"**：缺通知出口时本轮一条候选都不处理，只记一次
            # 恰好一条的审计（见模块文档「缺通知出口时的姿态」）。
            self._audit.record("stalled_provisioning.notifier_not_wired")
            return StalledProvisioningReport(notifier_wired=False)

        tally = _Tally()
        interrupted = self._sweep(tally)
        report = tally.freeze(interrupted=interrupted, notifier_wired=True)
        if tally.examined or tally.skipped_in_backoff:
            # 零候选**且**零退避跳过时不记审计：本职责每轮都跑，健康系统里绝大多数
            # tick 什么都不该做，无条件记审计只会刷出海量空事实。**必须把
            # `skipped_in_backoff` 也算进这个门槛**：只看 `examined` 的话，一轮取到
            # 的候选恰好全部在退避期时会静默不记审计，运维读到的"这一轮没有停摆候选"
            # 其实是"有人被跳过了"，两件事不能同一个沉默表达。
            self._audit.record("stalled_provisioning.completed", **report.audit_facts())
            logger.info(
                "开通中途停摆收口完成 候选=%s 已通知=%s 已收口=%s 通知失败=%s "
                "推进被拒=%s 失败=%s 退避跳过=%s",
                report.examined,
                report.notified,
                report.aborted,
                report.notify_failed,
                report.advance_refused,
                report.failed,
                report.skipped_in_backoff,
            )
        if report.aborted > 0 and self._alert is not None:
            # 停摆计数送达管理群：只在本轮真的收口了至少一个候选时上报，只传聚合
            # 计数，不含任何单个候选的 user_id/open_id/追溯号。
            try:
                self._alert(report.aborted)
            except Exception as error:  # 告警失败不得带走已完成的收口
                self._audit.record(
                    "stalled_provisioning.alert_callback_failed",
                    error=type(error).__name__,
                )
        return report

    def _sweep(self, tally: _Tally) -> bool:
        """取到期候选并逐个处置。返回本轮是否被停止信号中断。

        **两段式**：候选查询按 :data:`STALLED_FETCH_OVERSAMPLE` 过采样，取回的候选
        先在进程内按每用户通知退避过滤，再从中挑最多 ``self._limit`` 个**真正处理**
        （发通知、可能收口）——过采样是为了不让排在最前面、正处于退避期的候选把
        ``limit`` 名额占满，饿死后面真正等待处理的新候选。
        """
        fetch_limit = min(self._limit * STALLED_FETCH_OVERSAMPLE, _MAX_FETCH_LIMIT)
        candidates = self._candidates.stalled_provisioning_candidates(
            lease_seconds=self._lease_seconds, limit=fetch_limit
        )
        now = self._clock()
        processed = 0
        for item in candidates:
            if processed >= self._limit:
                # 本轮真正处理的名额已经用完：过采样只是为了绕开退避期的毒候选，
                # 不是要把 `limit` 本身也放宽。
                break
            if self._stop.is_set():
                # 停止信号落在遍历中间：不再为后面的人处置。已经处理过的各自是完整的
                # 一步（通知、或通知+收口），下一次启动会从库里的候选查询原样继续。
                return True
            last_attempt = self._last_attempt.get(item.user_id)
            if last_attempt is not None and now - last_attempt < self._notify_backoff_seconds:
                # 退避期内：这个人最近刚被尝试过（无论成败），跳过——不占用本轮的
                # 处理名额，也不打一次多余的飞书。**必须计数**（N-2 修复）：不然一轮
                # 取到的候选全部在退避期时，`tally.examined` 会是 0，运维读审计会
                # 误以为这一轮没有任何停摆候选。
                tally.skipped_in_backoff += 1
                continue
            self._last_attempt[item.user_id] = now
            tally.examined += 1
            processed += 1
            try:
                self._process_one(item, tally)
            except Exception as error:  # 一个用户的失败不得带走整轮
                tally.failed += 1
                self._audit.record(
                    "stalled_provisioning.user_failed",
                    user=item.user_id,
                    error=type(error).__name__,
                )
                logger.error(
                    "单个用户的停摆收口失败，其余用户继续 user=%s error=%s",
                    item.user_id,
                    type(error).__name__,
                )
        return False

    def _process_one(self, item: Any, tally: _Tally) -> None:
        """处置一个候选：**先通知，通知送达才收口**（见模块文档）。

        系统触发（预开通）的候选**全程静默**：``onboarding.stalled`` 文案说的
        是「你发起开通」，预开通用户没有发起过任何东西，这句对他是假话。
        判据是 :func:`~lingxi.core.identity.preprovision.is_system_trigger`；
        静默路径按送达处理，收口写入、审计与失败原因落库照旧。用户自己发起
        的链一字不变。
        """
        if is_system_trigger(item.event_id):
            tally.silenced_system += 1
        else:
            dedupe_key = f"onboarding:stalled:{item.event_id}"
            try:
                self._notifier.send(
                    open_id=item.open_id,
                    key=KEY_STALLED,
                    values={"reference": item.trace_id},
                    dedupe_key=dedupe_key,
                )
            except Exception as error:  # 通知失败：留在原状态，下一轮重来
                tally.notify_failed += 1
                self._audit.record(
                    "stalled_provisioning.notify_failed",
                    user=item.user_id,
                    error=type(error).__name__,
                )
                return
            tally.notified += 1

        self._abort_after_notify(item, tally)

    def _abort_after_notify(self, item: Any, tally: _Tally) -> None:
        """通知已完成（或静默豁免）之后，CAS 收口成 ``aborted`` 并落审计与失败原因。"""
        aborted = self._aborter.abort_stalled_provisioning(
            user_id=item.user_id,
            expected_states=_EXPECTED_STATES,
            reason="stalled_lease_expired",
        )
        if not aborted:
            # CAS 0 行：状态在候选查到与这里之间被别的路径改写（被另一条并发链推进
            # 到 active、被停用、或已经被本职责上一轮收口过）。**不重发**——通知已经
            # 发出去了，下一轮候选查询会按当前的真实状态重新判断。进程内的退避记录
            # 留着不清理：下一次真正停摆的人是新的 user_id，不会撞上这条陈旧记录。
            tally.advance_refused += 1
            self._audit.record("stalled_provisioning.advance_refused", user=item.user_id)
            return
        tally.aborted += 1
        self._audit.record(
            "stalled_provisioning.aborted",
            user=item.user_id,
            state=item.provisioning_state,
            reason="stalled_lease_expired",
            trace_id=item.trace_id,
        )
        # 失败原因落库：紧邻上面那条既有审计。这条路径覆盖的是 ``onboarding.result``
        # 从未写出的那一半——链本身死掉、``_execute`` 从未跑完；``reason`` 固定复用
        # 上面这条审计已经在用的同一个字面量。
        self._record_failure_reason(trace_id=item.trace_id)

    def _record_failure_reason(self, *, trace_id: str) -> None:
        """把这次租约到期收口的失败原因落库。

        **最佳努力**：与
        ``core.identity.onboarding_runner.AutoOnboardingRunner._record_failure_
        reason`` 同一条纪律——落库失败不得带走已经完成的收口，只改记一条自己的
        失败审计。
        """
        if self._failure_reasons is None:
            return
        try:
            self._failure_reasons.record_failure(
                trace_id=trace_id,
                failure_reason="stalled_lease_expired",
                event_type="stalled_provisioning.aborted",
            )
        except Exception as error:  # 落库失败不得带走已完成的收口
            self._audit.record(
                "stalled_provisioning.failure_reason_record_failed",
                error=type(error).__name__,
                trace_id=trace_id,
            )


def _build_stalled_provisioning_duty(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
    alert: Callable[[int], None] | None = None,
) -> Any:
    """装配开通中途停摆收口职责。**总能注册**，不需要任何可选前置。

    候选查询、收口写入与通知都只需要 ``LINGXI_POSTGRES_DSN``/飞书应用凭据，
    两者都是 :class:`SchedulerConfig` 的必填项。装配断言（停摆租约必须严格
    长于一条链在 provisioning/mcp_syncing 两格上可能停留的最长时间，见
    `apps/scheduler/onboarding.py::assert_stalled_lease_exceeds_chain_budget`）
    只是拿一份 :class:`ReadinessSchedule` 来核对预算数字，**不需要真的
    装配探针**，因此在探针端点是否配置好之前就能跑。
    """
    from lingxi.adapters.postgres_identity import PostgresAppUserStore
    from lingxi.adapters.postgres_onboarding_failure import PostgresFailureReasonRecorder
    from lingxi.adapters.postgres_stalled_provisioning import PostgresStalledProvisioningStore
    from lingxi.apps.scheduler.onboarding import assert_stalled_lease_exceeds_chain_budget

    dsn = config.postgres_dsn
    timeouts = config.postgres_timeouts

    schedule = ReadinessSchedule(probe_timeout_seconds=config.query_mcp_timeout_seconds)
    assert_stalled_lease_exceeds_chain_budget(
        lease_seconds=DEFAULT_STALLED_LEASE_SECONDS,
        publish_wait_seconds=config.onboarding_publish_wait_seconds,
        schedule=schedule,
    )

    duty = StalledProvisioningDuty(
        candidates=PostgresStalledProvisioningStore(dsn, timeouts=timeouts),
        aborter=PostgresAppUserStore(dsn, timeouts=timeouts),
        notifier=_build_stalled_notifier(config),
        alert=alert,
        audit=audit,
        failure_reasons=PostgresFailureReasonRecorder(dsn, timeouts=timeouts),
        lease_seconds=DEFAULT_STALLED_LEASE_SECONDS,
        stop=stop,
    )
    logger.info("开通中途停摆收口职责已装配 租约=%ss", DEFAULT_STALLED_LEASE_SECONDS)
    return duty


def _build_stalled_notifier(config: SchedulerConfig) -> Any:
    """装配停摆终态通知的发送口（``CatalogNotifier``）。"""
    from lingxi.adapters.feishu_user_message import FeishuUserMessages
    from lingxi.apps.scheduler.onboarding import CatalogNotifier
    from lingxi.config.content import default_content_catalog

    return CatalogNotifier(
        sender=FeishuUserMessages(
            base_url=config.feishu_base_url,
            app_id=config.feishu_app_id,
            app_secret=config.feishu_app_secret,
        ),
        catalog=default_content_catalog(),
    )


__all__ = [
    "DEFAULT_NOTIFY_RETRY_BACKOFF_SECONDS",
    "DEFAULT_STALLED_LEASE_SECONDS",
    "DEFAULT_STALLED_LIMIT",
    "STALLED_FETCH_OVERSAMPLE",
    "StalledProvisioningDuty",
    "StalledProvisioningReport",
]
