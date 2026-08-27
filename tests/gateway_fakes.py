"""gateway 断言共用的假实现。

不是测试文件（``unittest discover`` 只收 ``test*.py``），只是三个 gateway 测试模块
共用的注入件，避免同一批假实现抄三遍后各自漂移。

设计要点：**所有调用都记进同一条 ``CallLog``**。接口设计 3.2 的处理次序是合同要求，
断言它就必须能观察到跨组件的调用**顺序**，而不只是"某个方法被调过"。
"""

from __future__ import annotations

import dataclasses

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from lingxi.core.conversation.ports import (
    ConversationRecord,
    HandledAs,
    OnboardingResult,
    OnboardingState,
    PendingOnboarding,
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
    """``fail_with`` 是公开、可在用例中途改写的开关（与 ``RecordingText.fail``
    同一惯例，见 ``test_gateway_delivery.py``）：群聊 @ 引导的失败重试用例需要
    先注入一次失败、再在同一个假实现上切回成功，不必新建第二个假对象。"""

    def __init__(self, log: CallLog, *, fail_with: Exception | None = None) -> None:
        self._log = log
        self.fail_with = fail_with

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
        if self.fail_with is not None:
            raise self.fail_with


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
        #: 每次调用拿到的认领代次。单独记，不混进 ``calls``——那一列断的是「只有事件身份，
        #: 没有用户正文」，多一个键会让那条断言失去意义。
        self.claim_tokens: list[Any] = []
        self._result = result or OnboardingResult(state=OnboardingState.STARTED)
        self._fail_with = fail_with

    def start(
        self, *, event_id: str, open_id: str, trace_id: str, claim_token: Any = None
    ) -> OnboardingResult:
        self.calls.append(
            {"event_id": event_id, "open_id": open_id, "trace_id": trace_id}
        )
        self.claim_tokens.append(claim_token)
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



#: 「已平账」的占位代次：与真库的「时间戳非空」同义，但绝不等于任何一次认领代次，
#: 因此不会被任何 CAS 释放误撤。
_MARKED = object()

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
    # Issue #65 轻审 P2-2：迁移 0062 那一列（``onboarding_dispatched_at``）的内存
    # 对应物——记下哪些事件已经确认交给开通编排。
    # 事件标识 → 认领代次（或 ``_MARKED``，表示「已平账」而不是「被谁认领着」）。
    onboarding_dispatched: dict = field(default_factory=dict)
    claim_generation: int = 0
    # 已认领、超过对账窗口仍未交接的孤儿事件（对账扫描的输入）。认领即摘除，与真库
    # 「UPDATE … RETURNING 同一条语句里记账」同语义：同一条不会被认领两次。
    stale_onboardings: list[PendingOnboarding] = field(default_factory=list)


class FakeTransaction:
    """一个假事务。

    写操作先落在**暂存区**，只有 ``commit`` 时才合进 ``FakeState``——这样"事务失败
    时数据不该留下"这件事在假实现里也是真的，而不是靠测试自己记得清理。
    """

    def __init__(
        self,
        state: FakeState,
        log: CallLog,
        *,
        fail_on: str | None = None,
        force_clear_agent_session_result: bool | None = None,
        force_claim_conversation_result: bool | None = None,
    ) -> None:
        self._state = state
        self._log = log
        self._fail_on = fail_on
        # Issue #175 P2-1：注入「busy 快照读到空闲，但 clear_agent_session 真正写入
        # 时已经影响 0 行」的竞态，不依赖真实并发线程调度。``None`` 时走原有的按
        # staged_claims/running_task_id 计算的语义，与真库条件更新同构。
        self._force_clear_agent_session_result = force_clear_agent_session_result
        # Issue #189 独立审查：普通消息这一侧的同一种竞态——busy 快照读到空闲，但
        # claim_conversation 真正抢占时已经影响 0 行。与上一个开关同构，用来断言这条
        # 分支同样不得漏出「已开启新会话」的告知。
        self._force_claim_conversation_result = force_claim_conversation_result
        self.staged_events: dict[str, str | None] = {}
        self.staged_tasks: list[FakeTask] = []
        self.staged_claims: dict[str, str | None] = {}
        self.staged_session_clears: list[str] = []
        self.staged_session_discards: list[str] = []
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
        if self._force_claim_conversation_result is not None:
            forced = self._force_claim_conversation_result
            if forced:
                self.staged_claims[conversation_id] = task_id
            return forced
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
        if self._force_clear_agent_session_result is not None:
            forced = self._force_clear_agent_session_result
            if forced:
                self.staged_session_clears.append(conversation_id)
            return forced
        conversation = self._find(conversation_id)
        running = self.staged_claims.get(conversation_id, conversation.running_task_id)
        if running is not None:
            # 与真库的条件更新同语义：话题已被占用时影响 0 行。
            return False
        self.staged_session_clears.append(conversation_id)
        return True

    def discard_stale_agent_session(self, *, conversation_id: str) -> None:
        # 与真库同语义：不做忙碌判定（调用点已抢占话题），置空随事务提交生效。
        # 物理清理排队属于真库副作用，由真库用例覆盖，假实现不建第二套队列。
        self._log.add("store.discard_stale_agent_session", conversation_id=conversation_id)
        self._maybe_fail("discard_stale_agent_session")
        self.staged_session_discards.append(conversation_id)

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
        for conversation_id in self.staged_session_discards:
            self._find(conversation_id).agent_session_id = None
        for task_id in self.staged_stops:
            for task in self._state.tasks:
                if task.task_id == task_id:
                    task.stop_requested = True
        self._state.notifies += self.staged_notifies
        self._state.committed = True


class FakeStore:
    """实现 ``GatewayStore``。``fail_on`` 指定在哪一步注入写失败；
    ``force_clear_agent_session_result`` 与 ``force_claim_conversation_result``
    分别注入 ``/new`` 与普通消息抢占这两处的竞态（见 ``FakeTransaction.__init__``）。"""

    def __init__(
        self,
        state: FakeState,
        log: CallLog,
        *,
        fail_on: str | None = None,
        force_clear_agent_session_result: bool | None = None,
        force_claim_conversation_result: bool | None = None,
    ) -> None:
        self._state = state
        self._log = log
        self._fail_on = fail_on
        self._force_clear_agent_session_result = force_clear_agent_session_result
        self._force_claim_conversation_result = force_claim_conversation_result

    def mark_onboarding_dispatched(self, *, event_id: str) -> None:
        """记账：这条事件已经交给开通编排（迁移 0062 那一列的内存对应物）。

        刻意**不在**事务对象上：真实实现同样是事务提交之后的一条独立 ``UPDATE``。
        ``fail_on`` 命中时抛异常，用来断言"记不上账不能带走用户可见的终态提示"。
        """

        self._log.add("store.mark_onboarding_dispatched", event_id=event_id)
        if self._fail_on == "mark_onboarding_dispatched":
            raise RuntimeError("注入失败：mark_onboarding_dispatched")
        self._state.onboarding_dispatched.setdefault(event_id, _MARKED)

    def claim_stale_onboarding(self, *, older_than) -> PendingOnboarding | None:
        """认领一条待交接事件；认领即摘除并记账，与真库的原子 ``UPDATE … RETURNING`` 同语义。"""

        self._log.add("store.claim_stale_onboarding", older_than=older_than)
        if self._fail_on == "claim_stale_onboarding":
            raise RuntimeError("注入失败：claim_stale_onboarding")
        if not self._state.stale_onboardings:
            return None
        pending = self._state.stale_onboardings.pop(0)
        # 认领即记账：真库那条语句在取出的同一瞬间写上 ``onboarding_dispatched_at``，
        # 并把那个时刻当作**认领代次**返回。假实现必须同样做，否则「没跑成就要放回去」
        # 与「不得撤销别人的认领」两条断言在假实现上都恒真。
        self._state.claim_generation += 1
        token = datetime(2026, 8, 19, tzinfo=timezone.utc) + timedelta(
            seconds=self._state.claim_generation
        )
        self._state.onboarding_dispatched[pending.event_id] = token
        return dataclasses.replace(pending, claim_token=token)

    def release_onboarding_claim(self, *, event_id: str, claim_token=None) -> None:
        """把**自己那一次**认领放回去：代次对得上才清，对不上什么都不做。"""

        self._log.add(
            "store.release_onboarding_claim", event_id=event_id, claim_token=claim_token
        )
        if self._fail_on == "release_onboarding_claim":
            raise RuntimeError("注入失败：release_onboarding_claim")
        if claim_token is None:
            return
        if self._state.onboarding_dispatched.get(event_id) != claim_token:
            # ABA：这条已经被别人重新认领了，撤销它等于把别人的认领清掉。
            return
        del self._state.onboarding_dispatched[event_id]
        self._state.stale_onboardings.append(
            PendingOnboarding(
                event_id=event_id,
                open_id=f"ou_{event_id}",
                trace_id=f"trc_{event_id}",
            )
        )

    @contextmanager
    def transaction(self) -> Iterator[FakeTransaction]:
        transaction = FakeTransaction(
            self._state,
            self._log,
            fail_on=self._fail_on,
            force_clear_agent_session_result=self._force_clear_agent_session_result,
            force_claim_conversation_result=self._force_claim_conversation_result,
        )
        # 异常时**不提交**：暂存区里的事件行、抢占、任务一起消失，
        # 与真库的事务回滚同语义。
        yield transaction
        transaction.commit()


def provisioned_user(open_id: str = "ou_1", user_id: str = "usr_1") -> UserRecord:
    return UserRecord(user_id=user_id, state=UserState.ACTIVE)
