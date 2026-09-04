"""gateway 侧的首次开通装配断言（Epic D / S-D-02 搬迁后）。

产品负责人 2026-08-18 裁定把首次开通编排整体移进 ``lingxi-scheduler``（决策记录见
``docs/决策记录/2026-08-18-首次开通编排住在scheduler.md``）。搬迁之后 gateway 在这条链上
只剩两件事：

1. 把未开通用户的首聊事件落进 ``inbound_event`` 并标成 ``auto_provisioning``（管线的事务）；
2. 立刻回一条合同要求的「已收到，正在核对」。

**它不再持有任何会产生外部副作用的开通实现**，因此原先那条「``build_supervisor`` 与
``build_onboarding_reconciler`` 必须拿到同一个 runner 实例」的装配断言失去了对象——搬迁
之后 gateway 只有一个注入点，对账那一路整个搬走了。那条断言**不是被删掉了事**，它挡的两个
伤害各自有了新的守卫：

| 原来挡的 | 搬迁后由谁挡 |
|---|---|
| 对账落回失败关闭桩、「认领即平账」把孤儿烧掉 | ``apps/scheduler/onboarding.assert_claim_limit_follows_capacity`` + 释放认领的反向路径 |
| 分钟级编排落在长连接线程 / 投递线程上 | 本模块的 :func:`assert_gateway_onboarding_is_inert` |
"""

from __future__ import annotations

from typing import Any

from lingxi.core.conversation.ports import OnboardingResult, OnboardingState

#: gateway 允许接到管线上的开通实现类名。**白名单而不是黑名单**：将来有人在这里接一个
#: 新的执行型实现时，默认结果是启动即失败，而不是静默把分钟级等待放回长连接线程。
INERT_ONBOARDING_TYPES: frozenset[str] = frozenset({"_RecordingOnboarding"})


def assert_gateway_onboarding_is_inert(*runners: Any) -> None:
    """gateway 接到管线上的开通实现必须是「只记事件」的那一个。

    ``EventPipeline._start_onboarding`` 在**长连接事件线程**里同步调用 ``start``。真实
    编排单次耗时可达分钟级（产品合同允许权限同步等到十五分钟），接上去的后果是 gateway
    十五分钟收不到任何消息——而现场表现只是「机器人不理人」，没有任何一条日志会说明原因。
    这正是 Issue #65 开工卡「共用线程复核」要消灭的形状。

    **至少要一个**：一个都没报告说明 ``build_supervisor`` 的回报钩子根本没触发，断言会
    退化成永远成立的空话。
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

    产品负责人 2026-08-18 裁定把首次开通编排整体移进 ``lingxi-scheduler``（决策记录见
    ``docs/决策记录/2026-08-18-首次开通编排住在scheduler.md``）。gateway 从此只做两件事：
    把首聊事件落进 ``inbound_event`` 并标成 ``auto_provisioning``（这一步由管线的事务完成），
    以及立刻回一条合同要求的「已收到，正在核对」。真正的编排由 scheduler 按
    ``claim_stale_onboarding`` 认领。

    因此本类返回 ``STARTED``：它的字面含义正是"编排已异步接手、这一轮没有别的话要说"
    （见 ``ports.OnboardingState``），而管线对 ``started`` **刻意不记账**——账本留给真正
    跑完的那一方，中途崩溃的链因此仍然可以被重新认领。

    **不是失败关闭桩。** 桩会返回 ``INTERNAL_ERROR``，让每个未开通用户当场看到
    ``LX-ONBOARD-001``；而这里的语义是"收到了、正在处理"，是真话。
    """

    def start(
        self, *, event_id: str, open_id: str, trace_id: str, claim_token: Any = None
    ) -> OnboardingResult:
        del event_id, open_id, trace_id, claim_token
        return OnboardingResult(state=OnboardingState.STARTED)
