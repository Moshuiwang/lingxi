"""投递事件的数据形状与终态解析规则（Issue #151）。

数据库设计[「问数结果投递事件与会话保留 Outbox」]
(../../../../docs/技术设计/数据库设计.md#问数结果投递事件与会话保留-outbox) 冻结的语义在
这里落成可被单测直接证伪的纯函数：终态分类只能来自这张有限表，投递是否成功不得
改写业务结果（`V-投递-04`），到期强制覆盖为 ``delivery_expired`` 是唯一的例外路径。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DeliveryEventType(str, Enum):
    """``task_delivery_event.event_type`` 的取值，与迁移 0059 的 CHECK 一致。"""

    STARTED = "started"
    PROGRESS = "progress"
    SAFELY_RELEASABLE_ANSWER = "safely_releasable_answer"
    TERMINAL = "terminal"


#: 只有这两类事件允许携带正文；其余事件类型的 ``content`` 必须是 ``None``
#: （由迁移 0059 的 CHECK 在数据库层再确认一次，这里的常量供调用方在写入前自查）。
CONTENT_BEARING_EVENT_TYPES = frozenset(
    {DeliveryEventType.SAFELY_RELEASABLE_ANSWER, DeliveryEventType.TERMINAL}
)


class TerminalKind(str, Enum):
    """``task_delivery_event.terminal_kind`` 的取值，与迁移 0059 的 CHECK 一致。

    只有 ``terminal`` 事件携带它；表达的是 Worker 认定的**业务**结论，与是否
    已经送达到用户会话（``platform_received``）是两个独立维度（`V-投递-04`）。
    """

    SUCCESS = "success"
    FAILED = "failed"
    STOPPED = "stopped"
    REDACTED_WITHHELD = "redacted_withheld"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class ResolvedOutcome:
    """任务在投递结论落定后应当收敛到的 ``task.status`` 与 ``task.error_kind``。"""

    status: str
    error_kind: str | None


#: 业务终态到默认 ``task.status``/``error_kind`` 的映射。``error_kind`` 这里给出
#: 的是缺省值——调用方（Worker）在写 ``terminal`` 事件时通常已经算出更具体的分类
#: （例如 ``session_failed``/``context_too_long``），会覆盖这个默认值；只有
#: ``stopped``/``redacted_withheld`` 这类分类本身就等于错误码的情形才依赖默认值。
_TERMINAL_TO_OUTCOME: dict[TerminalKind, ResolvedOutcome] = {
    TerminalKind.SUCCESS: ResolvedOutcome(status="succeeded", error_kind=None),
    TerminalKind.FAILED: ResolvedOutcome(status="failed", error_kind="session_failed"),
    TerminalKind.STOPPED: ResolvedOutcome(status="stopped", error_kind="stopped"),
    TerminalKind.REDACTED_WITHHELD: ResolvedOutcome(status="failed", error_kind="redacted_withheld"),
    TerminalKind.TIMEOUT: ResolvedOutcome(status="failed", error_kind="running_timeout"),
}

#: 二十四小时到期仍未确认送达时的强制终态（数据库设计 :594、issue 状态合同第 8 条）。
#: 无论原始业务结论是什么，都不得把任务写成用户已取得结果——这是唯一允许覆盖
#: 业务结论的路径，因此单独命名为常量而不是走 ``resolve_delivered_outcome``。
DELIVERY_EXPIRED_OUTCOME = ResolvedOutcome(status="failed", error_kind="delivery_expired")


def resolve_delivered_outcome(*, terminal_kind: str, error_kind: str | None) -> ResolvedOutcome:
    """已确认 ``platform_received`` 后，任务应当收敛到的业务终态。

    ``error_kind`` 优先取调用方在写终态事件时记录的具体分类；未提供时退回该
    ``terminal_kind`` 的默认分类。业务结论完全来自写终态事件那一刻的记录，
    投递是否成功、多久之后才确认，都不改变这里算出的结果（`V-投递-04`）。
    """

    try:
        kind = TerminalKind(terminal_kind)
    except ValueError as error:
        raise ValueError(f"未知的投递终态分类：{terminal_kind!r}") from error
    default = _TERMINAL_TO_OUTCOME[kind]
    return ResolvedOutcome(status=default.status, error_kind=error_kind or default.error_kind)
