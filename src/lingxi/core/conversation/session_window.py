"""两小时规则：新任务是否续用上一个 Agent 会话。

合同原文（[产品合同与外部边界](../../../../docs/产品合同与外部边界.md)「问数与多轮对话」）：

> 会话是否延续按"上一条任务结束到下一条新任务开始"的间隔判断：间隔超过两小时时，
> 新任务不恢复旧会话，直接以新会话服务用户。**任务执行本身耗时多久都不触发新会话。**

最后那一句是这个模块存在的全部理由，也是它唯一的易错点（`V-会话-03`）。判定只允许读
``last_task_ended_at``：一旦有人把任务开始时间或执行时长掺进来，一个跑了三小时的任务
结束后紧接着的追问就会被当成新会话，用户上下文凭空丢失。函数签名里因此**没有**任务
开始时间和时长可读——不是靠注释约束，是靠参数表约束。
"""

from __future__ import annotations

from datetime import datetime, timedelta

# 合同写死的两小时。放常量是为了让测试引用同一个值，不是为了让它可配置：
# 改这个数字等于改一条用户可见承诺，必须走合同修订。
SESSION_IDLE_WINDOW = timedelta(hours=2)


def should_resume_session(
    *,
    last_task_ended_at: datetime | None,
    agent_session_id: str | None,
    now: datetime,
    window: timedelta = SESSION_IDLE_WINDOW,
) -> bool:
    """新任务是否续用旧会话。

    ``now`` 是必填关键字参数而不是内部取当前时间——注入时钟是 `V-会话-02` 的前提，
    也是 ``core/`` 不做 I/O 的要求（代码框架第二节）。

    三种不续用的情形：
    - 没有旧会话（``agent_session_id`` 为空）：``/new`` 之后与首次对话都在此列；
    - 从未结束过任务（``last_task_ended_at`` 为空）；
    - 间隔**超过**窗口。边界取「严格大于」：正好两小时仍续用，与合同「超过两小时」
      的措辞一致。
    """

    if not agent_session_id:
        return False
    if last_task_ended_at is None:
        return False
    return (now - last_task_ended_at) <= window
