"""gateway 侧的首次开通装配断言，以及它唯一允许的那个惰性实现。

首次开通编排整体住在 ``lingxi-scheduler``（见「首次开通编排住在 scheduler」决策记录）。
搬迁之后 gateway 在这条链上只剩两件事：

1. 把未开通用户的首聊事件落进 ``inbound_event`` 并标成 ``auto_provisioning``（管线的事务）；
2. 立刻回一条合同要求的「已收到，正在核对」。

**它不再持有任何会产生外部副作用的开通实现**，因此原先那条「两处装配必须拿到同一个
编排实例」的对账断言失去了对象。那条断言挡的两个伤害各自有了新的守卫：对账落回失败
关闭桩，由 scheduler 侧的认领容量断言挡；分钟级编排落在长连接线程，由本模块的
:func:`assert_gateway_onboarding_is_inert` 挡。
"""

from __future__ import annotations

from typing import Any

from lingxi.core.conversation.ports import OnboardingResult, OnboardingState

#: gateway 允许接到管线上的开通实现类名。**白名单而不是黑名单**：将来有人在这里接一个
#: 新的执行型实现时，默认结果是启动即失败，而不是静默把分钟级等待放回长连接线程。
INERT_ONBOARDING_TYPES: frozenset[str] = frozenset({"_RecordingOnboarding"})


def assert_gateway_onboarding_is_inert(*runners: Any) -> None:
    """Gateway 接到管线上的开通实现必须是「只记事件」的那一个。

    管线在**长连接事件线程**里同步调用开通入口。真实编排单次耗时可达分钟级（合同允许权限
    同步等到十五分钟），接上去的后果是 gateway 十五分钟收不到任何消息——而现场表现只是
    「机器人不理人」，没有任何一条日志会说明原因。**至少要报告一个**：一个都没有说明回报
    钩子根本没触发，断言会退化成永远成立的空话。

    Raises:
        RuntimeError: 一个都没报告，或其中任何一个不在白名单里。
    """
    if not runners:
        raise RuntimeError(
            "gateway 开通装配断言必须拿到 build_supervisor 实际采用的那个实现："
            "一个都没报告说明回报钩子没有触发，断言会退化成空话"
        )
    for runner in runners:
        if type(runner).__name__ not in INERT_ONBOARDING_TYPES:
            raise RuntimeError(
                "gateway 只能接「只记事件」的开通实现："
                "任何会产生外部副作用的编排都会把分钟级等待放回长连接线程"
            )


class _RecordingOnboarding:
    """gateway 侧的开通"编排"：**只记事件，一个外部动作都不做**。

    gateway 只做两件事：把首聊事件落进 ``inbound_event`` 并标成 ``auto_provisioning``，
    以及立刻回一条合同要求的「已收到，正在核对」。真正的编排由 scheduler 按
    认领水位取走。

    因此本类返回"已接手"：管线对这个状态**刻意不记账**，账本留给真正跑完的那一方，中途
    崩溃的链才能被重新认领。这**不是失败关闭桩**——桩会让每个未开通用户当场看到内部故障码。
    """

    def start(
        self, *, event_id: str, open_id: str, trace_id: str, claim_token: Any = None
    ) -> OnboardingResult:
        """记下这次首聊已被接手，不做任何外部动作。"""
        del event_id, open_id, trace_id, claim_token
        return OnboardingResult(state=OnboardingState.STARTED)
