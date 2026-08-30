"""``PostgresTaskQueue``/Gateway 消费共用的返回值形状（Issue #239 拆分）。

这些 dataclass 本身不承载读写边界，只是多个 mixin 共用的返回类型，因此单独放在
一个模块里，避免任一 mixin 反过来 import 另一个 mixin 才能拿到类型定义。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ClaimedTask:
    task_id: str
    conversation_id: str
    user_id: str
    prompt: str
    resumed_session: bool
    target_worker_version: str
    attempts: int
    reply_to_message_id: str | None = None
    stop_requested: bool = False
    side_effect_state: str = "none"


@dataclass(frozen=True)
class TaskContext:
    """worker 收口所需的同话题投递与会话上下文。"""

    task_id: str
    conversation_id: str
    user_id: str
    prompt: str
    resumed_session: bool
    target_worker_version: str
    attempts: int
    reply_to_message_id: str | None
    chat_id: str
    thread_id: str | None
    agent_session_id: str | None
    stop_requested: bool
    side_effect_state: str


@dataclass(frozen=True)
class TerminalTask:
    task_id: str
    conversation_id: str
    status: str
    error_kind: str


@dataclass(frozen=True)
class AppendedEvent:
    """一次投递事件写入的结果（Issue #151）。

    ``duplicate=True`` 表示 ``idempotency_key`` 已存在——调用方的这次写入是对同一
    次逻辑动作的重试，``sequence`` 是**已经写入的那一条**的序号，不是新分配的；
    调用方不应把它当作"又发生了一次"来计数或重复触发外部副作用。
    """

    sequence: int
    duplicate: bool


@dataclass(frozen=True)
class DeliveryEventRecord:
    """Gateway 消费循环读回的一条 outbox 事件（Issue #152）。"""

    sequence: int
    event_type: str
    terminal_kind: str | None
    content: str | None
    elapsed_seconds: int | None


@dataclass(frozen=True)
class PendingDeliveryTask:
    """Gateway 一轮消费的候选任务：既带上投递目的地，也带上它自己的消费进度。

    ``card_id``/``card_seq``/``message_id``/``fallback_text`` 是上一轮（或崩溃前）
    持久化的进度，供 :class:`~lingxi.core.execution.card_stream.CardStream` 以
    ``initial_*`` 恢复，不从零建卡（`V-卡片-01`、状态合同第 7 条）。
    """

    task_id: str
    conversation_id: str
    chat_id: str
    thread_id: str | None
    reply_to_message_id: str | None
    status: str
    card_id: str | None
    card_seq: int
    message_id: str | None
    fallback_text: bool
    consumed_sequence: int


@dataclass(frozen=True)
class StaleQueuedTask:
    """一条已入队超过排队阈值、仍未被任何 worker 领取的任务（Issue #465，S-3：
    排队可感知）。只读扫描的结果形状，供 ``apps/gateway/delivery.py`` 的
    ``DeliveryConsumer`` 尽力而为发一条"前面还有任务在排队"的提示——不改变
    ``task``/``conversation`` 任何一行，因此不需要携带消费进度这类字段。"""

    task_id: str
    chat_id: str
    thread_id: str | None
    reply_to_message_id: str | None


@dataclass(frozen=True)
class UncertainDeliveryTask:
    """外发前预留位没有被清空、原因不明的任务（Issue #151 审核 P3-6）。

    只用于告警与人工核对；消费循环不会自动处理这类任务，见
    :meth:`PostgresTaskQueue.reserve_dispatch` 的说明。
    """

    task_id: str
    reserved_kind: str


@dataclass(frozen=True)
class SessionCleanupTask:
    """一条待物理清理的 Agent 会话 JSONL 请求（Issue #153）。"""

    id: str
    user_id: str
    agent_session_id: str
    reason: str
