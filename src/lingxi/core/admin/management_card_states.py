"""管理卡持久上下文的状态取值域：六个 ``state`` 与四个 ``dispatch_status``。

取值与迁移 ``0081`` 里 ``management_card_context`` 两列的 CHECK 逐字一致，用例对照
迁移文件钉住。Python 侧凡是写入、比较或翻译这两列的地方都引用这里——同一台状态机
此前在回调编排、重算回写与重启恢复三处各写一份字面量，改一处漏一处没有任何机制
能发现。本模块只有常量，不 import 任何东西，因此任何一层都可以引用而不带入闭包。
"""

#: 卡片刚发出、表单可填。
STATE_READY = "ready"
#: 表单已提交、确认卡已发出，等管理员点确认。
STATE_SUBMITTED = "submitted"
#: 已确认执行，权限正在下发。
STATE_DISPATCHING = "dispatching"
#: 本次操作已生效。
STATE_EFFECTIVE = "effective"
#: 本次操作未完成，等每日批纠正。
STATE_INCOMPLETE = "incomplete"
#: 卡片已关闭，不再接受任何操作。
STATE_CLOSED = "closed"

#: ``state`` 列的全部取值，顺序与迁移 CHECK 一致。
MANAGEMENT_CARD_STATES = (
    STATE_READY,
    STATE_SUBMITTED,
    STATE_DISPATCHING,
    STATE_EFFECTIVE,
    STATE_INCOMPLETE,
    STATE_CLOSED,
)

#: 表单已提交、结果未出的两个等待态：渲染层继续隐藏表单避免重复点击，状态行说
#: 「正在下发」。
SUBMITTED_STATES = frozenset({STATE_SUBMITTED, STATE_DISPATCHING})

#: 没有在途下发。
DISPATCH_IDLE = "idle"
#: 下发进行中。
DISPATCH_PUBLISHING = "publishing"
#: 下发已生效。
DISPATCH_EFFECTIVE = "effective"
#: 下发未完成。
DISPATCH_INCOMPLETE = "incomplete"

#: ``dispatch_status`` 列的全部取值，顺序与迁移 CHECK 一致。
MANAGEMENT_CARD_DISPATCH_STATUSES = (
    DISPATCH_IDLE,
    DISPATCH_PUBLISHING,
    DISPATCH_EFFECTIVE,
    DISPATCH_INCOMPLETE,
)

__all__ = [
    "DISPATCH_EFFECTIVE",
    "DISPATCH_IDLE",
    "DISPATCH_INCOMPLETE",
    "DISPATCH_PUBLISHING",
    "MANAGEMENT_CARD_DISPATCH_STATUSES",
    "MANAGEMENT_CARD_STATES",
    "STATE_CLOSED",
    "STATE_DISPATCHING",
    "STATE_EFFECTIVE",
    "STATE_INCOMPLETE",
    "STATE_READY",
    "STATE_SUBMITTED",
    "SUBMITTED_STATES",
]
