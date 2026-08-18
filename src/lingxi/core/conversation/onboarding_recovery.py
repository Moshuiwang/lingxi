"""未开通首聊「已认领、没交接」的对账扫描（Issue #65 轻审 P2-2）。

#65 的接线把两件事分在事务的两侧：**事务内**认领事件（``inbound_event`` 落行、
``handled_as = 'auto_provisioning'``），**提交之后**才把事件交给开通编排。这个顺序
本身不能反过来——在提交前触发，事务一旦回滚就会重复触发一次带外部副作用的开通。

代价是提交与交接之间存在一个窗口。进程在这个窗口里崩溃、被 ``SIGTERM`` 拿走
（``EventPipeline`` 停机时**刻意**不再触发编排，见 P2-3），或者交接调用本身没能落账，
都会留下一条谁都不会再处理的行：飞书重投时被幂等挡在门外，编排永远收不到它，用户
那边只剩一个「已收到」的表情。此前仓库里没有任何东西会发现这种行。

本模块就是那个"发现并重新交接"的动作。它**不做开通判定**：身份、匹配、建档、环境、
权限发布与 MCP 同步全部属于注入的 ``OnboardingRunner``；这里只保证"被认领的事件最终
一定被交出去一次"，以及交不出去时留下响亮的审计。

**已知边界（交给正式 runner 那一步收口）**：走这条恢复路径的事件，用户侧拿不到结论
提示。``inbound_event`` 只留了事件标识、发送者标识与 trace，没有存回复所需的会话与
消息标识，而"要不要为此多存两个路由标识"是一次数据范围决定，不由实施层自己扩大。
今天的实际后果有限：用户再发一条消息就会走正常路径拿到提示，而这条扫描保证的是
"人最终会被开通"，不是"人一定会被通知"。
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Callable

from .ports import (
    AuditSink,
    GatewayStore,
    OnboardingResult,
    OnboardingRunner,
    PendingOnboarding,
)

logger = logging.getLogger(__name__)

#: 一条事件被认领多久之后才算"交接丢了"。
#:
#: 下界由**在途的正常调用**决定，不是拍的：``EventPipeline`` 在同一个线程里同步调用
#: ``OnboardingRunner.start``，而产品合同允许权限同步最长等到十五分钟。窗口若短于它，
#: 扫描会把一条**正在正常执行**的开通判成孤儿，再并发触发一次——对账本身就成了重复
#: 触发外部开通的来源。取三十分钟：十五分钟上限之上留一倍余量。
DEFAULT_STALE_AFTER = timedelta(minutes=30)

#: 一轮最多重新交接多少条。孤儿只应由崩溃与停机产生，正常情况下每轮是 0 条；设上限
#: 是为了让一次异常积压（例如连续崩溃后重启）不会在单轮里长时间占住调用它的循环。
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
    ) -> None:
        self._store = store
        self._onboarding = onboarding
        self._audit = audit
        self._stale_after = stale_after
        self._max_per_sweep = max_per_sweep
        self._min_interval_seconds = min_interval_seconds
        self._should_stop = should_stop or (lambda: False)
        self._monotonic = monotonic
        self._last_sweep: float | None = None

    @property
    def onboarding(self) -> OnboardingRunner:
        """这条对账扫描实际拿到的开通编排。**只读**，供装配层回读。

        对账是两个注入点里更容易被漏掉的那一个：它落回失败关闭桩时，孤儿事件会被
        "认领即平账"地烧掉，唯一的证据是一行 INFO 级 ``onboarding.reconciled
        state=internal_error``（#65 开工卡必含项，PR #205 二级审查 P3-2）。把引用公开出来，
        装配断言才能真的比对两处拿到的是不是同一个实例。
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

        recovered = 0
        for _ in range(self._max_per_sweep):
            # 每条之前重新问一次停机：认领即记账，停机信号到达之后再认领的那一条
            # 就再也没人处理了。逐条认领 + 每条前重判，让"随时停"不丢事件。
            if self._should_stop():
                break
            try:
                pending = self._store.claim_stale_onboarding(older_than=self._stale_after)
            except Exception as error:  # noqa: BLE001 - 认领失败只降级这一轮
                self._audit.record(
                    "onboarding.reconcile_scan_failed",
                    error=type(error).__name__,
                )
                break
            if pending is None:
                break
            recovered += 1
            self._dispatch(pending)
        return recovered

    def _dispatch(self, pending: PendingOnboarding) -> None:
        """把一条已认领的孤儿交给编排，并记下结果。

        **异常不外抛、也不重新排队。** 这一条在认领时就已经被记成"已交接"，因此不会
        被下一轮再捞一次——这是刻意的：正式 runner 带外部副作用，一条反复失败的事件
        每分钟重试一次会持续冲击外部系统，而它失败的原因通常不会因为再试一次而消失。
        代价是这条事件到此为止，所以失败必须留下响亮的审计（``WARNING`` 级别由审计
        实现按 ``failed`` 后缀判定），不能悄无声息。
        """

        try:
            result = self._onboarding.start(
                event_id=pending.event_id,
                open_id=pending.open_id,
                trace_id=pending.trace_id,
            )
            if not isinstance(result, OnboardingResult):
                raise TypeError("onboarding runner returned an invalid result")
        except Exception as error:  # noqa: BLE001 - 见 docstring
            self._audit.record(
                "onboarding.reconcile_failed",
                event_id=pending.event_id,
                error=type(error).__name__,
                trace_id=pending.trace_id,
            )
            return
        # ``notified=False`` 不是凑字数：走这条路径的用户收不到结论提示（见模块头部
        # 的已知边界）。把它写进审计，是为了让"补上通知"这件待办在运行证据里也看得见，
        # 而不是只活在文档里。
        self._audit.record(
            "onboarding.reconciled",
            event_id=pending.event_id,
            state=result.state.value,
            failure_reason=result.failure_reason,
            notified=False,
            trace_id=pending.trace_id,
        )
