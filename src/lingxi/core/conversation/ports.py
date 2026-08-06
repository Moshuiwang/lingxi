"""gateway 事件管线的数据形状与注入口。

这里定义的 ``Protocol`` 是 ``core/`` 与外部世界之间的**唯一**接触面：管线只按这些签名
调用，真实实现（PostgreSQL、飞书 SDK）住在 ``adapters/``，测试注入假的。仓库既有惯例
是 ``typing.Protocol`` + 构造器关键字参数注入（``adapters/feishu_directory.py`` 的
``Transport``、``feishu_onboarding.py`` 的 ``CardSender``），本模块沿用，不引 DI 框架。

``InboundMessage`` 的字段表是 `V-接入-11` 的实现手段：**它没有第二个用户标识字段**。
任务归属只能由 ``sender_open_id`` 解析而来，事件体里另外声明的 ``user_id``、消息正文里
写的"我是某某"都到不了管线——不是靠管线自觉忽略，是靠这张表根本不搬运它们。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import ContextManager, Protocol


@dataclass(frozen=True)
class InboundMessage:
    """一条已从飞书事件体里解析出来的私聊消息。

    ``thread_id`` 为 ``None`` 表示私聊主窗口（与 ``conversation.feishu_thread_id`` 同义）。
    """

    event_id: str
    event_type: str
    sender_open_id: str
    chat_id: str
    thread_id: str | None
    message_id: str
    text: str
    trace_id: str


class UserState(str, Enum):
    """用户在 gateway 眼里的三种状态。

    刻意是自己的枚举而不是直接用 ``app_user`` 的列值：数据库那两列
    （``provisioning_state`` / ``account_state``）的取值域服务于开通流程，管线只关心
    "能不能给他排任务"。映射写在 adapters 层。
    """

    NOT_PROVISIONED = "not_provisioned"
    SUSPENDED = "suspended"
    ACTIVE = "active"


@dataclass(frozen=True)
class UserRecord:
    user_id: str
    state: UserState


@dataclass(frozen=True)
class ConversationRecord:
    conversation_id: str
    agent_session_id: str | None
    last_task_ended_at: datetime | None
    running_task_id: str | None


class HandledAs(str, Enum):
    """``inbound_event.handled_as`` 的取值，与迁移 013 的 CHECK 一致。"""

    TASK_QUEUED = "task_queued"
    BUSY_HINT = "busy_hint"
    # 未开通用户：本批只记录「收到过、没受理」，**没有**启动自动匹配与开通。
    NOT_PROVISIONED = "not_provisioned"
    # 留给 #65 真正接上正向开通路径时使用；本批不会写出这个值。
    AUTO_PROVISIONING = "auto_provisioning"
    COMMAND = "command"
    DROPPED = "dropped"


@dataclass(frozen=True)
class Outcome:
    """一次事件处理的结论，供调用方与测试断言。

    ``handled_as`` 为 ``None`` 表示这条事件**没有被成功处理完**（重复投递，或处理中
    途失败）——`V-接入-12` 与 `V-队列-03` 都要求这种情况不得被标记为已成功处理。
    """

    handled_as: HandledAs | None
    duplicate: bool = False
    task_id: str | None = None
    resumed_session: bool | None = None
    target_worker_version: str | None = None


class GatewayTransaction(Protocol):
    """一个事务内可用的写操作。

    `V-队列-01` 要求 ``inbound_event`` 插入、``conversation`` 抢占、``task`` 插入落在
    **同一事务**里，因此这些方法刻意只在事务对象上提供，拿不到"事务外顺手写一条"的入口。
    这与代码框架第二节「写路径上 audit.record 必须接收调用方事务对象」是同一条约束。
    """

    def insert_inbound_event(
        self, *, event_id: str, event_type: str, user_open_id: str, trace_id: str
    ) -> bool:
        """插入事件行；已存在返回 ``False``（重复投递），不抛异常。"""

    def mark_handled_as(self, *, event_id: str, handled_as: HandledAs) -> None: ...

    def lookup_user(self, *, open_id: str) -> UserRecord | None: ...

    def ensure_conversation(
        self, *, user_id: str, chat_id: str, thread_id: str | None
    ) -> ConversationRecord: ...

    def claim_conversation(self, *, conversation_id: str, task_id: str) -> bool:
        """条件更新抢占话题；返回是否抢到（影响行数 1）。"""

    def insert_task(
        self,
        *,
        task_id: str,
        conversation_id: str,
        user_id: str,
        inbound_event_id: str,
        prompt: str,
        resumed_session: bool,
        target_worker_version: str,
    ) -> None: ...

    def clear_agent_session(self, *, conversation_id: str) -> None: ...

    def request_stop(self, *, conversation_id: str) -> str | None:
        """给该话题运行中的任务置 ``stop_requested``；返回被置的任务标识或 ``None``。"""

    def notify_task_queued(self) -> None:
        """发出 ``NOTIFY task_queued``。在事务内调用，随提交一起对外可见。"""


class GatewayStore(Protocol):
    def transaction(self) -> ContextManager[GatewayTransaction]: ...


class Reactions(Protocol):
    """加表情。合同：任何消息都加，只表示已收到。"""

    def add(self, *, message_id: str) -> None: ...


class Replies(Protocol):
    """发文本。

    签名里带上 ``reply_to_message_id`` 与 ``thread_id``，是为了让接口设计「四、飞书出站」
    的「必须发到同一私聊或同一话题」由**参数表**保证：实现总是回复触发它的那条消息，
    话题里的消息回进同一话题，不需要调用方额外记住该发到哪。
    """

    def send_text(
        self, *, chat_id: str, thread_id: str | None, reply_to_message_id: str, text: str
    ) -> None: ...


class AuditSink(Protocol):
    """审计出口。

    ``audit_event`` 表尚未建立（属后续切片），当前实现写结构化日志。管线只依赖这个
    签名，届时换实现不动管线。
    """

    def record(self, action: str, /, **fields: object) -> None: ...


class VersionResolver(Protocol):
    """求值任务的目标 worker 版本（#45）。

    在**入队时**按用户发起请求的时间求值一次，结果写进 ``task.target_worker_version``
    后不再改变。本批的分流规则形态未定（#45 决策第 3 条归 S11），默认实现固定返回
    ``stable``；开成注入口是为了让 `V-灰度-02` 能在测试里造出两个版本的任务。
    """

    def __call__(self, *, user_id: str, now: datetime) -> str: ...
