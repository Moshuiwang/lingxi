"""开通中途停摆收口职责：认领超过租约、仍卡在中途格的用户最终一定离开
``provisioning``/``mcp_syncing``（Issue #282，[V-开通-19](../../../../docs/技术设计/验收矩阵.md)）。

## 补的是哪个洞

首次开通那条链（:class:`~lingxi.core.identity.onboarding_runner.AutoOnboardingRunner`）
把用户推进到 ``provisioning`` 之后失败，会让 ``provisioning_state`` 永久停在
``provisioning``/``mcp_syncing``。编排层自己的「当场收口」
（:meth:`~lingxi.core.identity.onboarding_runner.AutoOnboardingRunner._abort_if_stalled`）
覆盖了"链跑到了一个确定的失败终态"这一半；本职责覆盖**另一半**——链本身死掉、当场收口
够不到的地方：进程被强杀、执行线程异常退出到 ``_execute`` 都拿不到结论、收口写入自己
那一次恰好失败。这些情形下，``core/conversation/pipeline.py`` 对 ``PROVISIONING``
状态的短路分支会让用户从此只收到「正在完成…请稍候」，而**没有任何东西会回来看他**
（Issue #282 原始报告的用户可见形态）。

## 放在哪：一个新职责，对称于迟到就绪恢复

形状照 :class:`~lingxi.apps.scheduler.late_readiness_recovery.LateReadinessRecoveryDuty`：
``run_once()`` 自带幂等、可随时停、异常不外抛、报告只含计数与固定分类（不含任何
open_id / 用户资料值）、零候选时不记审计（该职责外部独立审查 F4 的同一条纪律）。

## 判据：认领时间 + 超时租约，不是状态列

候选查询在 :mod:`lingxi.adapters.postgres_stalled_provisioning`，完整判据与既有三个
恢复/对账职责的边界见该模块文档。

**租约取 45 分钟**（:data:`DEFAULT_STALLED_LEASE_SECONDS`），且由装配断言
:func:`~lingxi.apps.scheduler.onboarding.assert_stalled_lease_exceeds_chain_budget` 守
住——不成立时扫描会把一条正在正常跑的开通判成僵尸。这个数字**不是产品裁定**，是从
代码上界推出来的工程值（照 ``late_readiness_recovery.DEFAULT_RECOVERY_INTERVAL_SECONDS``
的同一条免责：产品负责人认为该更快或更慢，改这一个常量即可，不影响任何判定语义）。

## 收口写入：复用既有专用入口，不新写一份 CAS

真正把用户收口成 ``aborted`` 的写入是
:meth:`~lingxi.adapters.postgres_identity.PostgresAppUserStore.abort_stalled_provisioning`
——与编排层「当场收口」共用**同一个**方法，见该方法文档字符串的完整论证。本职责只负责
"谁该被收口、什么时候收口"，不重复实现"怎么安全地收口"。

## 处置顺序：**先通知，后收口**

对每个候选（逐条处理）：

1. **先发通知，通知送达才 CAS 收口；通知送不到就留在原状态，下一轮重来（幂等）。**
   顺序与 ``late_readiness_recovery`` 的 F1 教训一致但方向相反：那边是"推进 active
   而不告知"会让人永远等不到；这边是"收口 aborted 而不告知"会让一个已经被告知过
   「请不要重复发送」的人从此再也不发消息，于是"下一条消息自动重试"这条自愈路径永远
   不会被触发。
2. ``dedupe_key = f"onboarding:stalled:{event_id}"``——与终态通知的
   ``f"onboarding:{event_id}"``（``onboarding_runner.py`` 的 ``_notify``）刻意不同键，
   两者不会互相去重掉：一条真正死掉的链此前可能从未发出过任何终态通知。
3. 通知发送成功但 CAS 之前进程崩溃 → 下一轮用**同一个** ``dedupe_key`` 再发一次。
   用户会不会看到两条，取决于飞书 ``im/v1/messages`` 对同一 ``uuid`` 是否去重——
   **本仓库未验证的平台行为**，与 ``late_readiness_recovery`` 模块文档已登记的那条
   同型，不新增补偿逻辑。
4. CAS 返回 0 行（有人在这中间改了状态——被另一条并发链推进、被停用、或已经被本职责
   上一轮收口过）→ 计一条 ``advance_refused``，本条到此为止，**不重发**——通知已经
   发出去了，重发只会制造第二条相互矛盾的消息。
5. 全程审计只记 ``user_id`` / ``state`` / ``reason`` / ``trace_id`` / 计数，**不记
   open_id、不记任何资料值**（``LateReadinessRecoveryReport`` 同一条纪律）。

## 通知文案：PR-1 阶段复用既有键，不新增

发的是既有的 ``onboarding.internal_error`` 键（``KEY_INTERNAL_ERROR``，逐字复用，
``values={}``——该键当前没有任何占位变量）。是否新增一条专用文案说明"我们等了很久仍
没有结果，你可以再发一条消息重试"是产品文案裁定，属 #280 的范围（联合设计 §9 第 5
条待裁定），不在本次机制修复里做。

## 缺通知出口时的姿态：绝不"改状态不告知"

``notifier is None`` → 本轮**一条都不处理**，只记一次
``stalled_provisioning.notifier_not_wired``，并在报告里带 ``notifier_wired=False``。
不允许"先改状态、通知以后再补"——那正是本 Issue 要修的那种半开状态。当前部署下这一路
是防御性的：飞书应用凭据是 :class:`~lingxi.apps.scheduler.config.SchedulerConfig` 的
必填项，进程起不来就没有装配的机会，因此装配层（``assembly.py``）总能建出一个真实
notifier；这条分支存在是为了让"以后有人把它改成可选"或"测试里就是要验证这个边界"这两种
情形都有明确定义的行为，不是当前生产会走到的路径。

## 幂等与竞态：扫到的同时那条链恰好还活着

四道独立防线，任意一道单独失效都不会造成用户可见伤害：

| # | 防线 | 挡住什么 |
|---|---|---|
| 1 | 租约 45 分钟 > 链预算约 20 分钟，且由装配断言守住 | 正常慢链被误判 |
| 2 | 不释放认领（``onboarding_dispatched_at`` 保持非空） | 老事件被第二次认领 ⇒ 结构上不可能双跑同一条事件 |
| 3 | 收口写入的 CAS 只接受 ``provisioning``/``mcp_syncing`` 且账号启用 | 覆盖 ``active``（假失败）、覆盖已停用账号 |
| 4 | 编排自己的进程内去重（按 ``open_id``） | 用户重发触发的新链撞上一条真的还活着的老链 |

**真的撞上了会怎样**：扫描在租约到期那一刻把一条仍在 ``mcp_syncing`` 探针中的活链写成
``aborted``，随后那条活链探到 READY 并调用 ``advance_provisioning_state(to='active')``。
``_PROVISIONING_ORDER`` 里 ``aborted`` 与 ``guest`` 同 rank ⇒ ``allowed`` 含
``aborted`` ⇒ 推进成功，用户被正确激活并收到「开通完成」。净结果：用户先收到一条
多余的失败终态，随后收到正确的成功终态。**方向是安全的**（永远不会把没就绪的人写成
``active``，永远不会把已成功的人打回失败），且概率被防线 1 压到接近零。如实登记，
不加补偿。反向（链先 ``active``，扫描后 ``aborted``）被防线 3 在 SQL 层排除。

## 已知边界（明确接受，不修）

- 候选查询的认领没有带过期时间的持久租约（单实例假设），与 ``late_readiness_recovery``
  「已知边界」和 #250 同一判据。多实例部署或滚动发布重叠窗口出现时必须回来补。
- 通知发送与 CAS 收口不在同一个数据库事务里（收口写入没有内建的通知 outbox）：这是
  与 ``late_readiness_recovery`` 的 F1 修复不同的形状——那边的通知是持久 outbox、独立
  重试到送达为止；本职责的通知是**一次性**尝试（不送达就整条候选留在原状态，交给
  下一轮候选查询重新捞起，本身已经是一种"重试"，只是重试的是整条处置而不是单独的
  通知）。两种形状都能达到"不会改状态不告知"这条底线，选择更简单的这一种是因为本职责
  的候选集合极小（停摆本身就是罕见事件），不值得为它另建一张 outbox 表。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from lingxi.core.identity.onboarding_runner import (
    KEY_INTERNAL_ERROR,
    STATE_MCP_SYNCING,
    STATE_PROVISIONING,
)

logger = logging.getLogger(__name__)

#: 停摆租约（秒）。见模块文档「判据」一节：由装配断言
#: ``assert_stalled_lease_exceeds_chain_budget`` 守住，必须严格长于一条链在
#: provisioning/mcp_syncing 两格上可能停留的最长时间。
DEFAULT_STALLED_LEASE_SECONDS = 2700

#: 单轮最多处理多少个停摆候选。配额保护，不是重试上限——被推迟到下一轮的候选不丢失
#: 任何进度（进度全在 ``inbound_event``/``app_user`` 里）。
DEFAULT_STALLED_LIMIT = 50

_EXPECTED_STATES: tuple[str, ...] = (STATE_PROVISIONING, STATE_MCP_SYNCING)


class _Candidates(Protocol):
    """停摆候选的读取口（``adapters/postgres_stalled_provisioning.py``）。"""

    def stalled_provisioning_candidates(
        self, *, lease_seconds: int, limit: int = ...
    ) -> Sequence[Any]: ...


class _Aborter(Protocol):
    """收口写口（``adapters/postgres_identity.PostgresAppUserStore.
    abort_stalled_provisioning``，与编排层「当场收口」共用同一份实现）。"""

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
    """一轮的结果。**只有计数与固定分类，没有任何字段值**（同
    ``LateReadinessRecoveryReport`` 的纪律：内部用户标识、open_id 一个都不进报告）。"""

    #: 本轮取到的停摆候选数。
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
    interrupted: bool = False
    #: 通知出口装配了没有。见模块文档「缺通知出口时的姿态」。
    notifier_wired: bool = True

    def audit_facts(self) -> dict[str, Any]:
        facts: dict[str, Any] = {
            "examined": self.examined,
            "notified": self.notified,
            "aborted": self.aborted,
            "notify_failed": self.notify_failed,
            "advance_refused": self.advance_refused,
            "failed": self.failed,
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

    def freeze(self, *, interrupted: bool, notifier_wired: bool) -> StalledProvisioningReport:
        return StalledProvisioningReport(
            examined=self.examined,
            notified=self.notified,
            aborted=self.aborted,
            notify_failed=self.notify_failed,
            advance_refused=self.advance_refused,
            failed=self.failed,
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
        lease_seconds: int = DEFAULT_STALLED_LEASE_SECONDS,
        limit: int = DEFAULT_STALLED_LIMIT,
        stop: threading.Event | None = None,
    ) -> None:
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds < 1:
            raise ValueError("停摆租约必须是正整数秒")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("单轮候选上限必须是正整数")
        self._candidates = candidates
        self._aborter = aborter
        self._notifier = notifier
        self._audit = audit
        self._lease_seconds = lease_seconds
        self._limit = limit
        self._stop = threading.Event() if stop is None else stop

    @property
    def notifier_wired(self) -> bool:
        """通知出口装配了没有。见模块文档「缺通知出口时的姿态」。"""

        return self._notifier is not None

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
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
        if tally.examined:
            # 零候选时不记审计（外部独立审查 F4 同一条纪律）：本职责每轮都跑，健康
            # 系统里绝大多数 tick 什么都不该做，无条件记审计只会刷出海量空事实。
            self._audit.record("stalled_provisioning.completed", **report.audit_facts())
            logger.info(
                "开通中途停摆收口完成 候选=%s 已通知=%s 已收口=%s 通知失败=%s "
                "推进被拒=%s 失败=%s",
                report.examined,
                report.notified,
                report.aborted,
                report.notify_failed,
                report.advance_refused,
                report.failed,
            )
        return report

    def _sweep(self, tally: _Tally) -> bool:
        """取到期候选并逐个处置。返回本轮是否被停止信号中断。"""

        candidates = self._candidates.stalled_provisioning_candidates(
            lease_seconds=self._lease_seconds, limit=self._limit
        )
        for item in candidates:
            if self._stop.is_set():
                # 停止信号落在遍历中间：不再为后面的人处置。已经处理过的各自是完整的
                # 一步（通知、或通知+收口），下一次启动会从库里的候选查询原样继续。
                return True
            tally.examined += 1
            try:
                self._process_one(item, tally)
            except Exception as error:  # noqa: BLE001 - 一个用户的失败不得带走整轮
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
        """处置一个候选：**先通知，通知送达才收口**（见模块文档「处置顺序」）。"""

        dedupe_key = f"onboarding:stalled:{item.event_id}"
        try:
            self._notifier.send(
                open_id=item.open_id,
                key=KEY_INTERNAL_ERROR,
                values={},
                dedupe_key=dedupe_key,
            )
        except Exception as error:  # noqa: BLE001 - 通知失败：留在原状态，下一轮重来
            tally.notify_failed += 1
            self._audit.record(
                "stalled_provisioning.notify_failed",
                user=item.user_id,
                error=type(error).__name__,
            )
            return
        tally.notified += 1

        aborted = self._aborter.abort_stalled_provisioning(
            user_id=item.user_id,
            expected_states=_EXPECTED_STATES,
            reason="stalled_lease_expired",
        )
        if not aborted:
            # CAS 0 行：状态在候选查到与这里之间被别的路径改写（被另一条并发链推进
            # 到 active、被停用、或已经被本职责上一轮收口过）。**不重发**——通知已经
            # 发出去了，下一轮候选查询会按当前的真实状态重新判断。
            tally.advance_refused += 1
            self._audit.record(
                "stalled_provisioning.advance_refused", user=item.user_id
            )
            return
        tally.aborted += 1
        self._audit.record(
            "stalled_provisioning.aborted",
            user=item.user_id,
            state=item.provisioning_state,
            reason="stalled_lease_expired",
            trace_id=item.trace_id,
        )


__all__ = [
    "DEFAULT_STALLED_LEASE_SECONDS",
    "DEFAULT_STALLED_LIMIT",
    "StalledProvisioningDuty",
    "StalledProvisioningReport",
]
