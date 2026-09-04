"""gateway 侧的首次开通装配断言。

首次开通编排整体归属 ``lingxi-scheduler``（决策见 ``docs/决策记录/`` 对应记录：
首次开通编排住在 scheduler）；搬迁之后 gateway 在这条链上只剩两件事：把未开通
用户的首聊事件落进 ``inbound_event`` 并标成 ``auto_provisioning``（管线的事务），
以及立刻回一条合同要求的「已收到，正在核对」。

gateway 不再持有任何会产生外部副作用的开通实现，因此原先那条「装配点必须拿到
同一个 runner 实例」的断言失去了对象——搬迁之后 gateway 只有一个注入点，对账
那一路整个搬走了。那条断言挡的两个伤害各自有了新的守卫：对账落回失败关闭桩、
「认领即平账」把孤儿烧掉，改由 ``apps/scheduler/onboarding`` 的容量断言与释放
认领的反向路径守住；分钟级编排落在长连接线程/投递线程上，由本模块的
:func:`assert_gateway_onboarding_is_inert` 守住。
"""

from __future__ import annotations

from typing import Any

#: gateway 允许接到管线上的开通实现类名。**白名单而不是黑名单**：将来有人在这里接一个
#: 新的执行型实现时，默认结果是启动即失败，而不是静默把分钟级等待放回长连接线程。
INERT_ONBOARDING_TYPES: frozenset[str] = frozenset({"_RecordingOnboarding"})


def assert_gateway_onboarding_is_inert(*runners: Any) -> None:
    """Gateway 接到管线上的开通实现必须是「只记事件」的那一个。

    ``EventPipeline._start_onboarding`` 在**长连接事件线程**里同步调用 ``start``。真实
    编排单次耗时可达分钟级（产品合同允许权限同步等到十五分钟），接上去的后果是 gateway
    十五分钟收不到任何消息——而现场表现只是「机器人不理人」，没有任何一条日志会说明原因。
    这正是本函数要挡住的形状。

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
