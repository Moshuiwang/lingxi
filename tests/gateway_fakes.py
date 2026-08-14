"""gateway 断言共用的假实现。

不是测试文件（``unittest discover`` 只收 ``test*.py``），只是三个 gateway 测试模块
共用的注入件，避免同一批假实现抄三遍后各自漂移。

设计要点：**所有调用都记进同一条 ``CallLog``**。接口设计 3.2 的处理次序是合同要求，
断言它就必须能观察到跨组件的调用**顺序**，而不只是"某个方法被调过"。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator

from lingxi.core.conversation.ports import (
    ConversationRecord,
    HandledAs,
    OnboardingResult,
    OnboardingState,
    UserRecord,
    UserState,
)


class CallLog:
    """按发生顺序记录所有出站调用与关键写操作。"""

    def __init__(self) -> None:
        self.entries: list[tuple[str, dict]] = []

    def add(self, name: str, **fields: Any) -> None:
        self.entries.append((name, fields))

    def names(self) -> list[str]:
        return [name for name, _ in self.entries]

    def count(self, name: str) -> int:
        return self.names().count(name)

    def index(self, name: str) -> int:
        """第一次出现的位置；没出现则抛 ``ValueError``（比返回 -1 更早暴露问题）。"""

        return self.names().index(name)

    def fields(self, name: str) -> list[dict]:
        return [fields for entry_name, fields in self.entries if entry_name == name]


class FakeReactions:
    def __init__(self, log: CallLog, *, fail_with: Exception | None = None) -> None:
        self._log = log
        self._fail_with = fail_with

    def add(self, *, message_id: str) -> None:
        self._log.add("reaction.add", message_id=message_id)
        if self._fail_with is not None:
            raise self._fail_with


class FakeReplies:
    def __init__(self, log: CallLog) -> None:
        self._log = log

    def send_text(
        self, *, chat_id: str, thread_id: str | None, reply_to_message_id: str, text: str
    ) -> None:
        self._log.add(
            "reply.send_text",
            chat_id=chat_id,
            thread_id=thread_id,
            reply_to_message_id=reply_to_message_id,
            text=text,
        )


class FakeAudit:
    def __init__(self, log: CallLog) -> None:
        self._log = log

    def record(self, action: str, /, **fields: Any) -> None:
        self._log.add(f"audit.{action}", **fields)


class FakeOnboarding:
    """只接收 gateway 的身份三元组，模拟 #89/#17 的编排边界。"""

    def __init__(
        self,
        *,
        result: OnboardingResult | None = None,
        fail_with: Exception | None = None,
    ) -> None:
        self.calls: list[dict[str, str]] = []
        self._result = result or OnboardingResult(state=OnboardingState.STARTED)
        self._fail_with = fail_with

    def start(self, *, event_id: str, open_id: str, trace_id: str) -> OnboardingResult:
        self.calls.append(
            {"event_id": event_id, "open_id": open_id, "trace_id": trace_id}
        )
        if self._fail_with is not None:
            raise self._fail_with
        return self._result


@dataclass
class FakeConversation:
    conversation_id: str
    agent_session_id: str | None = None
    last_task_ended_at: datetime | None = None
    running_task_id: str | None = None


@dataclass
class FakeTask:
    task_id: str
    conversation_id: str
    user_id: str
    inbound_event_id: str
    prompt: str
    resumed_session: bool
    target_worker_version: str
    reply_to_message_id: str | None = None
    stop_requested: bool = False
    status: str = "queued"


@dataclass
class FakeState:
    """假存储的全部状态。测试直接读它做断言。"""

    users: dict[str, UserRecord] = field(default_factory=dict)
    conversations: dict[tuple[str, str, str], FakeConversation] = field(default_factory=dict)
    events: dict[str, str | None] = field(default_factory=dict)
    tasks: list[FakeTask] = field(default_factory=list)
    notifies: int = 0
    committed: bool = False
    # Issue #152：预置某个话题「有一条尚未提示过的投递已过期任务」，供
    # test_gateway_pipeline.py 的专项用例断言 gateway.delivery_expired 提示的触发
    # 与只提示一次；默认空集合，绝大多数既有用例因此不受影响。
    pending_delivery_expired_notices: set[str] = field(default_factory=set)


class FakeTransaction:
    """一个假事务。

    写操作先落在**暂存区**，只有 ``commit`` 时才合进 ``FakeState``——这样"事务失败
    时数据不该留下"这件事在假实现里也是真的，而不是靠测试自己记得清理。
    """

    def __init__(self, state: FakeState, log: CallLog, *, fail_on: str | None = None) -> None:
        self._state = state
        self._log = log
        self._fail_on = fail_on
        self.staged_events: dict[str, str | None] = {}
        self.staged_tasks: list[FakeTask] = []
        self.staged_claims: dict[str, str | None] = {}
        self.staged_session_clears: list[str] = []
        self.staged_stops: list[str] = []
        self.staged_notifies = 0

    def _maybe_fail(self, step: str) -> None:
        if self._fail_on == step:
            raise RuntimeError(f"注入失败：{step}")

    def insert_inbound_event(
        self, *, event_id: str, event_type: str, user_open_id: str, trace_id: str
    ) -> bool:
        self._log.add("store.insert_inbound_event", event_id=event_id)
        self._maybe_fail("insert_inbound_event")
        if event_id in self._state.events or event_id in self.staged_events:
            return False
        self.staged_events[event_id] = None
        return True

    def mark_handled_as(self, *, event_id: str, handled_as: HandledAs) -> None:
        self._log.add("store.mark_handled_as", event_id=event_id, handled_as=handled_as.value)
        self.staged_events[event_id] = handled_as.value

    def lookup_user(self, *, open_id: str) -> UserRecord | None:
        self._log.add("store.lookup_user", open_id=open_id)
        return self._state.users.get(open_id)

    def ensure_conversation(
        self, *, user_id: str, chat_id: str, thread_id: str | None
    ) -> ConversationRecord:
        self._log.add("store.ensure_conversation", user_id=user_id, thread_id=thread_id)
        key = (user_id, chat_id, thread_id or "")
        conversation = self._state.conversations.get(key)
        if conversation is None:
            conversation = FakeConversation(conversation_id=f"cnv_{len(self._state.conversations)}")
            self._state.conversations[key] = conversation
        running = self.staged_claims.get(conversation.conversation_id, conversation.running_task_id)
        return ConversationRecord(
            conversation_id=conversation.conversation_id,
            agent_session_id=conversation.agent_session_id,
            last_task_ended_at=conversation.last_task_ended_at,
            running_task_id=running,
        )

    def claim_conversation(self, *, conversation_id: str, task_id: str) -> bool:
        self._log.add("store.claim_conversation", conversation_id=conversation_id, task_id=task_id)
        self._maybe_fail("claim_conversation")
        current = self._find(conversation_id)
        running = self.staged_claims.get(conversation_id, current.running_task_id)
        if running is not None:
            return False
        self.staged_claims[conversation_id] = task_id
        return True

    def insert_task(self, **kwargs: Any) -> None:
        self._log.add("store.insert_task", task_id=kwargs["task_id"])
        self._maybe_fail("insert_task")
        self.staged_tasks.append(FakeTask(**kwargs))

    def clear_agent_session(self, *, conversation_id: str) -> bool:
        self._log.add("store.clear_agent_session", conversation_id=conversation_id)
        conversation = self._find(conversation_id)
        running = self.staged_claims.get(conversation_id, conversation.running_task_id)
        if running is not None:
            # 与真库的条件更新同语义：话题已被占用时影响 0 行。
            return False
        self.staged_session_clears.append(conversation_id)
        return True

    def request_stop(self, *, conversation_id: str) -> str | None:
        self._log.add("store.request_stop", conversation_id=conversation_id)
        conversation = self._find(conversation_id)
        running = self.staged_claims.get(conversation_id, conversation.running_task_id)
        if running is None:
            return None
        self.staged_stops.append(running)
        return running

    def notify_task_queued(self) -> None:
        self._log.add("store.notify_task_queued")
        self.staged_notifies += 1

    def consume_delivery_expired_notice(self, *, conversation_id: str) -> bool:
        self._log.add(
            "store.consume_delivery_expired_notice", conversation_id=conversation_id
        )
        if conversation_id not in self._state.pending_delivery_expired_notices:
            return False
        # 与真实实现同语义：命中即原子标记为已提示，直接从暂存状态里摘掉，同一次
        # 到期不会被同一个话题的下一条消息再次命中。这里不经过 commit 暂存区——
        # 假实现里没有别的路径会读写这个集合，直接摘除不会破坏"事务失败即回滚"的
        # 测试意图（真实实现的原子性由真库 UPDATE...RETURNING 承担，见
        # adapters.postgres_conversation）。
        self._state.pending_delivery_expired_notices.discard(conversation_id)
        return True

    def _find(self, conversation_id: str) -> FakeConversation:
        for conversation in self._state.conversations.values():
            if conversation.conversation_id == conversation_id:
                return conversation
        raise KeyError(conversation_id)

    def commit(self) -> None:
        self._state.events.update(self.staged_events)
        self._state.tasks.extend(self.staged_tasks)
        for conversation_id, task_id in self.staged_claims.items():
            self._find(conversation_id).running_task_id = task_id
        for conversation_id in self.staged_session_clears:
            self._find(conversation_id).agent_session_id = None
        for task_id in self.staged_stops:
            for task in self._state.tasks:
                if task.task_id == task_id:
                    task.stop_requested = True
        self._state.notifies += self.staged_notifies
        self._state.committed = True


class FakeStore:
    """实现 ``GatewayStore``。``fail_on`` 指定在哪一步注入写失败。"""

    def __init__(self, state: FakeState, log: CallLog, *, fail_on: str | None = None) -> None:
        self._state = state
        self._log = log
        self._fail_on = fail_on

    @contextmanager
    def transaction(self) -> Iterator[FakeTransaction]:
        transaction = FakeTransaction(self._state, self._log, fail_on=self._fail_on)
        # 异常时**不提交**：暂存区里的事件行、抢占、任务一起消失，
        # 与真库的事务回滚同语义。
        yield transaction
        transaction.commit()


def provisioned_user(open_id: str = "ou_1", user_id: str = "usr_1") -> UserRecord:
    return UserRecord(user_id=user_id, state=UserState.ACTIVE)
