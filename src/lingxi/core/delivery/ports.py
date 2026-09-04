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


#: 只有这几类事件允许携带正文；其余事件类型的 ``content`` 必须是 ``None``
#: （由迁移 0059/0075 的 CHECK 在数据库层再确认一次，这里的常量供调用方在写入前
#: 自查——真正调用它自查的写入方见 ``adapters/postgres_conversation/
#: _queue_outbox.py::append_delivery_event`` 与 ``apps/worker/service.py::
#: WorkerService._append_event``；Issue #328 opus 审查 R1 之前这个常量定义了但
#: 零调用方，写入方从未真正用它自查过）。
#:
#: ``PROGRESS``（迁移 0075 新增；Issue #407 增粒度）：语义化进度动作码
#: （Issue #321 方向 C）——``"querying:N"``、``"querying:N:<已知子步骤>"``、
#: ``"composing"``、``"working"`` 这几种固定形状之一，是 worker 侧内部生成的
#: 短令牌，绝不是用户输入或模型输出的自由文本，因此可以在这里放行、同时受
#: ``PROGRESS_CONTENT_MAX_LENGTH`` 这条长度契约约束（迁移 0075 的 CHECK
#: ``char_length(content) <= 32`` 是同一条契约的数据库层落地）。子步骤名只
#: 来自 ``card_stream.KNOWN_QUERY_STEPS`` 这份白名单（最长
#: ``"search_dimension"`` 16 字节），`"querying:" + 计数 + ":" + 子步骤名` 的
#: 最坏长度在计数达到 6 位数之前都不会触顶——远超任何真实任务的问数调用次数。
CONTENT_BEARING_EVENT_TYPES = frozenset(
    {
        DeliveryEventType.PROGRESS,
        DeliveryEventType.SAFELY_RELEASABLE_ANSWER,
        DeliveryEventType.TERMINAL,
    }
)

#: ``progress`` 事件 ``content`` 的长度上限（迁移 0075 的 CHECK 同步约束）。
#: 已知形状（`card_stream.encode_progress_action` 的输出）：``"composing"``
#: （9 字节）、``"working"``（7 字节）、``"querying:" + 计数``、
#: ``"querying:" + 计数 + ":" + 已知子步骤名``（Issue #407，最长子步骤名
#: ``"search_dimension"`` 16 字节，实测两位数计数时 28 字节）。32 留了充裕
#: 余量，不是精确贴着已知最长值算出来的。只约束 ``PROGRESS``——
#: ``SAFELY_RELEASABLE_ANSWER``/``TERMINAL`` 携带的是用户可见的问数结果正文，
#: 篇幅由业务内容决定，不适用这条上限。
PROGRESS_CONTENT_MAX_LENGTH = 32


def assert_content_allowed(event_type: DeliveryEventType, content: str | None) -> None:
    """写入前自查：``content`` 是否被允许出现在这个 ``event_type`` 上。

    与迁移 0059/0075 的 CHECK 约束表达同一条规则的两份独立校验之一——数据库层
    是最终防线（写入方即使跳过这个函数，数据库仍会用 ``CheckViolation`` 拒绝
    违规写入），这里让调用方在真正写库前就能拿到一个可读的 ``ValueError``，
    不必等 CheckViolation 从数据库连接弹回来才发现，也不会被调用方常见的
    "写库失败只记日志、不中断任务"这类宽泛 ``except Exception`` 悄悄吞掉却查
    不出具体是哪条规则触发（Issue #328 opus 审查 R1 的真实事故：progress 事件
    的 content 撞了当时还没放宽的 CHECK，100% 失败，但只留下一条看不出根因的
    ``logger.error``，真实环境卡片完全不动）。
    """

    if content is None:
        return
    if event_type not in CONTENT_BEARING_EVENT_TYPES:
        allowed = sorted(item.value for item in CONTENT_BEARING_EVENT_TYPES)
        raise ValueError(f"{event_type.value} 事件不允许携带 content（仅 {allowed} 可以）")
    if event_type is DeliveryEventType.PROGRESS and len(content) > PROGRESS_CONTENT_MAX_LENGTH:
        raise ValueError(
            f"progress 事件 content 长度 {len(content)} 超过契约上限 "
            f"{PROGRESS_CONTENT_MAX_LENGTH}（应为 querying:N/composing 类内部短令牌）"
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
    TerminalKind.REDACTED_WITHHELD: ResolvedOutcome(
        status="failed", error_kind="redacted_withheld"
    ),
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
