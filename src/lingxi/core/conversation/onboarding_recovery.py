"""未开通首聊事件的**认领与交接**。

gateway 首聊落 ``inbound_event`` 并立刻回一条「已收到」，不持有任何外部副作用的
编排；本模块每轮在 scheduler 侧认领若干条，交给 ``OnboardingRunner``（专属线程
池），由编排自己通知用户。住在 scheduler 而不是 gateway 是凭据边界：在职状态
须实时回读飞书成员详情所需的短期令牌，其一次性 ``refresh_token`` 全系统只允许
一个消费者，已经在 scheduler 进程里。

**认领即记账，因此「没跑成」必须放回去**：``claim_stale_onboarding`` 取出那一刻
就写上 ``onboarding_dispatched_at``，没有任何东西会自动清回 ``NULL``——认领了却
没真的跑，就是一次永久烧掉（用户只剩「已收到」表情）。:meth:`OnboardingReconciler.
_dispatch` 因此对可重试原因显式释放认领，:meth:`~OnboardingReconciler.run_once`
把认领量压在执行器剩余容量之内。本模块不做开通判定：身份、匹配、建档、权限与
MCP 同步全部属于注入的 ``OnboardingRunner``，这里只保证事件最终被交出去一次。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import timedelta

from .ports import (
    RETRYABLE_REASONS,
    AuditSink,
    GatewayStore,
    OnboardingResult,
    OnboardingRunner,
    PendingOnboarding,
)

logger = logging.getLogger(__name__)

#: 一条事件落库多久之后才可以被认领。**这是保守的类默认值，生产用的不是它**——
#: scheduler 侧传入 ``apps/scheduler/onboarding.DISPATCH_AFTER``（秒级）。认领即
#: 记账后正在跑的那一条 ``onboarding_dispatched_at`` 已非空、压根不在候选里，
#: 保留这个值只是给"不传窗口"的调用方一个最保守的默认值。
DEFAULT_STALE_AFTER = timedelta(minutes=30)

#: 一轮最多认领多少条。**它只是硬上限，不是本轮真正会认领的条数**——真正的条数由注入的
#: ``capacity()``（执行器剩余容量）压住，见 :meth:`OnboardingReconciler.run_once`。
#: 认领即记账，因此「认领得比执行器能收下的多」等于按差额把事件逐条烧掉；这个默认值
#: 曾经是 20 而执行器容量只有 12，第 13 条起必然被烧。
DEFAULT_MAX_PER_SWEEP = 20

#: 两轮扫描之间的最小间隔。挂在投递循环的每轮回调上（默认一秒一轮），不自限就会变成
#: 每秒一次的空查询。
DEFAULT_MIN_INTERVAL_SECONDS = 60.0


class OnboardingReconciler:
    """周期性把"认领了却没交接"的事件重新交给开通编排。

    形状与 ``apps/scheduler`` 的各个 Duty 一致：``run_once()`` 自带幂等、可被随时
    停止、异常不外抛。调用方只要在自己的循环里每轮调一次即可。
    """

    name = "未开通首聊交接对账"

    def __init__(
        self,
        *,
        store: GatewayStore,
        onboarding: OnboardingRunner,
        audit: AuditSink,
        stale_after: timedelta = DEFAULT_STALE_AFTER,
        max_per_sweep: int = DEFAULT_MAX_PER_SWEEP,
        min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
        should_stop: Callable[[], bool] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        capacity: Callable[[], int] | None = None,
    ) -> None:
        """注入依赖端口与本轮认领的各项预算参数。"""
        self._store = store
        self._onboarding = onboarding
        self._audit = audit
        self._stale_after = stale_after
        self._max_per_sweep = max_per_sweep
        self._min_interval_seconds = min_interval_seconds
        self._should_stop = should_stop or (lambda: False)
        self._monotonic = monotonic
        # 本轮还能收下几条。``None`` 表示不限（只对同步执行的注入实现成立）。
        self._capacity = capacity
        self._last_sweep: float | None = None

    @property
    def capacity_source(self) -> Callable[[], int] | None:
        """本轮认领上限的来源（执行器的 ``free_slots``）。**只读**，供装配断言回读。

        ``apps/scheduler/onboarding.assert_claim_limit_follows_capacity`` 用它证明认领循环真的
        绑上了**那一个**执行器：没绑、或绑错了对象，多认领的事件就会被永久烧掉。
        """
        return self._capacity

    @property
    def onboarding(self) -> OnboardingRunner:
        """这条认领循环实际拿到的开通编排。**只读**，供装配层回读。

        落回失败关闭桩时孤儿事件会被「认领即平账」地烧掉，唯一的证据是一行 INFO
        级审计；本模块是唯一入口，同一类伤害由 :attr:`capacity_source` 那条断言
        与释放认领的反向路径挡住。
        """
        return self._onboarding

    def run_once(self) -> int | None:
        """跑一轮对账。

        返回本轮重新交接的条数；``None`` 表示这一轮**根本没跑**——要么在停机中，要么
        距上一轮不足最小间隔。两者刻意与"跑了但一条都没有"（返回 ``0``）区分开：
        调用方与断言据此能分辨"扫描停了"和"确实没有孤儿"。
        """
        if self._should_stop():
            return None
        now = self._monotonic()
        if self._last_sweep is not None and now - self._last_sweep < self._min_interval_seconds:
            return None
        self._last_sweep = now

        # **认领量必须被执行器的剩余容量压住。** 认领即记账（``claim_stale_onboarding``
        # 取出即写 ``onboarding_dispatched_at``），而执行器满位时 ``start`` 只能拒绝；
        # 多认领的那些即使被释放也白白多写两次库，而释放路径一旦有任何一处失效，差额就是
        # 被永久烧掉的事件数。取两者的较小值，从源头上不产生这个差额。
        budget = self._max_per_sweep
        if self._capacity is not None:
            budget = min(budget, max(0, int(self._capacity())))
        if budget <= 0:
            return 0

        recovered = 0
        for _ in range(budget):
            # 每条之前重新问一次停机：认领即记账，停机信号到达之后再认领的那一条
            # 就再也没人处理了。逐条认领 + 每条前重判，让"随时停"不丢事件。
            if self._should_stop():
                break
            try:
                pending = self._store.claim_stale_onboarding(older_than=self._stale_after)
            except Exception as error:  # 认领失败只降级这一轮
                self._audit.record(
                    "onboarding.reconcile_scan_failed",
                    error=type(error).__name__,
                )
                break
            if pending is None:
                break
            recovered += 1
            if not self._dispatch(pending):
                # 刚放回去一条：**本轮到此为止**。放回之后它立刻又是候选，继续循环就会在
                # 同一轮里把同一条反复认领 / 反复释放——而它没跑成的原因（执行器满位、
                # 正在停机）在这一轮里不会改变。下一轮再捞。
                break
        return recovered

    def _dispatch(self, pending: PendingOnboarding) -> bool:
        """把一条已认领的事件交给编排，并记下结果。**返回本轮是否继续认领下一条。**

        **异常不外抛，但「不外抛」不等于「到此为止」**：编排根本没有开始跑（执行器
        满位、已停机、同一个人已有链在跑，或 ``start`` 自己抛了异常）时**把认领放
        回去**，让下一轮重新捞——不放回去这条事件就永远不会再被任何人看到；编排跑
        了并得出结论（含失败终态）则不放回，重跑不会改变结论，只会持续冲击外部
        系统，结论本身由编排负责通知用户。两类都留审计，放回那一类的动作名带
        ``failed`` 后缀（审计实现升到 ``WARNING``）。
        """
        try:
            result = self._onboarding.start(
                event_id=pending.event_id,
                open_id=pending.open_id,
                trace_id=pending.trace_id,
                # 认领代次一路传给编排：它在链的后段（通知送不到）也要放回自己那一次认领。
                claim_token=pending.claim_token,
            )
            if not isinstance(result, OnboardingResult):
                raise TypeError("onboarding runner returned an invalid result")
        except Exception as error:  # 见 docstring
            self._audit.record(
                "onboarding.dispatch_failed",
                event_id=pending.event_id,
                error=type(error).__name__,
                trace_id=pending.trace_id,
            )
            self._release(pending)
            return False
        if result.failure_reason in RETRYABLE_REASONS:
            self._audit.record(
                "onboarding.dispatch_declined_failed",
                event_id=pending.event_id,
                reason=result.failure_reason,
                trace_id=pending.trace_id,
            )
            self._release(pending)
            return False
        self._audit.record(
            "onboarding.dispatched",
            event_id=pending.event_id,
            state=result.state.value,
            failure_reason=result.failure_reason,
            trace_id=pending.trace_id,
        )
        return True

    def _release(self, pending: PendingOnboarding) -> None:
        """把没跑成的那一条放回去。放不回去也只记审计——它已经是降级路径了。"""
        try:
            self._store.release_onboarding_claim(
                event_id=pending.event_id, claim_token=pending.claim_token
            )
        except Exception as error:
            self._audit.record(
                "onboarding.release_claim_failed",
                event_id=pending.event_id,
                error=type(error).__name__,
                trace_id=pending.trace_id,
            )
