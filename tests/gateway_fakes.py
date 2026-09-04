"""gateway 断言共用的假实现。

不是测试文件（``unittest discover`` 只收 ``test*.py``），只是三个 gateway 测试模块
共用的注入件，避免同一批假实现抄三遍后各自漂移。

设计要点：**所有调用都记进同一条 ``CallLog``**。接口设计 3.2 的处理次序是合同要求，
断言它就必须能观察到跨组件的调用**顺序**，而不只是"某个方法被调过"。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from lingxi.core.conversation.ports import (
    ConversationRecord,
    HandledAs,
    OnboardingResult,
    OnboardingState,
    PendingOnboarding,
    PendingPreprovisionNotice,
    UserRecord,
    UserState,
)
from lingxi.core.ids import new_id
from lingxi.core.user_memory import MAX_MEMORY_ENTRIES_PER_USER, UserMemoryEntry


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
        self.calls.append({"event_id": event_id, "open_id": open_id, "trace_id": trace_id})
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
    # Issue #541 预开通：预置某个用户「有一句『你已经被提前开通了』还没说」，供
    # test_gateway_pipeline.py 的专项用例断言首聊补一句的触发与只提示一次；默认空
    # 集合，绝大多数既有用例因此不受影响（与上面那条同一条纪律）。
    pending_preprovision_notices: set[str] = field(default_factory=set)
    # rc25 修复包 F1：挂起用户对应的「当前版本已发布权限文档文本」（真库来源是
    # ``publish_outbox.payload->'permissions'``）。缺项 = 快照不可用（保留期擦除、
    # 无已发布意图），peek 会返回 ``permissions=None``——用例据此覆盖"渲染失败不
    # 烧标志"的路径；默认空字典，绝大多数既有用例不受影响。
    preprovision_permissions: dict[str, str] = field(default_factory=dict)
    # Issue #65 轻审 P2-2：迁移 0062 那一列（``onboarding_dispatched_at``）的内存
    # 对应物——记下哪些事件已经确认交给开通编排。
    # 事件标识 → 认领代次（或 ``_MARKED``，表示「已平账」而不是「被谁认领着」）。
    onboarding_dispatched: dict = field(default_factory=dict)
    claim_generation: int = 0
    # 已认领、超过对账窗口仍未交接的孤儿事件（对账扫描的输入）。认领即摘除，与真库
    # 「UPDATE … RETURNING 同一条语句里记账」同语义：同一条不会被认领两次。
    stale_onboardings: list[PendingOnboarding] = field(default_factory=list)
    # 用户记忆（Issue #357 S-H3-3）：user_id → 记忆列表。**简化，不走暂存区**——
    # 与本类其余写方法不同，这里直接改 FakeState（不经 commit 才生效）：/memory
    # 命令面每条消息只触发四个方法之一，没有"同一事务内先写记忆、后面步骤又失败
    # 需要回滚"的组合场景需要在假实现里复现；真正的同事务回滚由真库测试
    # （tests/test_pending_action_postgres.py、tests/test_permission_publish_
    # postgres.py 的清除钩子用例、tests/test_postgres_user_memory.py）覆盖。
    user_memory: dict[str, list[UserMemoryEntry]] = field(default_factory=dict)
    # Issue #465：``claim_queue_failure_notice`` 的内存对应物——按 event_id 只让
    # 第一次调用拿到发送权，与真库 `queue_failure_notice` 表的 `ON CONFLICT DO
    # NOTHING ... RETURNING` 同语义。
    queue_failure_notices_claimed: set[str] = field(default_factory=set)


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
        fail_error: Exception | None = None,
        force_clear_agent_session_result: bool | None = None,
        force_claim_conversation_result: bool | None = None,
    ) -> None:
        self._state = state
        self._log = log
        self._fail_on = fail_on
        # 默认注入一个内容不带外部标识的通用 RuntimeError；``fail_error``
        # （Issue #469 opus 独立审查 P2-5）在用例需要断言"审计不含异常正文里的
        # 外部标识原值"时，替换成一个携带真实驱动异常常见形状（例如 psycopg 的
        # `DETAIL: Key (...)=(...)` 串）的自定义异常——与 ``FakeReactions``/
        # ``FakeReplies`` 的既有 ``fail_with`` 同一惯例。
        self._fail_error = fail_error
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
            raise (
                self._fail_error
                if self._fail_error is not None
                else RuntimeError(f"注入失败：{step}")
            )

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
            running_task_status=self._task_status(running),
        )

    def _task_status(self, task_id: str | None) -> str | None:
        """`ConversationRecord.running_task_status` 的假实现（Issue #465）：与真库
        的 ``LEFT JOIN task`` 同语义——`task_id` 为 ``None`` 时恒 ``None``；否则
        在已提交的任务里按 id 查一次（本次事务自己刚 ``insert_task`` 暂存的行，
        在 ``ensure_conversation`` 这一步永远查不到自己——它发生在入队**之前**，
        与真库同一时序）。查不到（数据不一致，理论上不该发生）保守返回 ``None``。
        """

        if task_id is None:
            return None
        for task in self._state.tasks:
            if task.task_id == task_id:
                return task.status
        return None

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
        self._log.add("store.consume_delivery_expired_notice", conversation_id=conversation_id)
        if conversation_id not in self._state.pending_delivery_expired_notices:
            return False
        # 与真实实现同语义：命中即原子标记为已提示，直接从暂存状态里摘掉，同一次
        # 到期不会被同一个话题的下一条消息再次命中。这里不经过 commit 暂存区——
        # 假实现里没有别的路径会读写这个集合，直接摘除不会破坏"事务失败即回滚"的
        # 测试意图（真实实现的原子性由真库 UPDATE...RETURNING 承担，见
        # adapters.postgres_conversation）。
        self._state.pending_delivery_expired_notices.discard(conversation_id)
        return True

    def peek_preprovision_notice(self, *, user_id: str) -> PendingPreprovisionNotice | None:
        self._log.add("store.peek_preprovision_notice", user_id=user_id)
        if user_id not in self._state.pending_preprovision_notices:
            return None
        # 与真实实现同语义：只读，不摘除；快照缺项时返回 permissions=None。
        return PendingPreprovisionNotice(
            permissions=self._state.preprovision_permissions.get(user_id)
        )

    def consume_preprovision_notice(self, *, user_id: str) -> bool:
        self._log.add("store.consume_preprovision_notice", user_id=user_id)
        if user_id not in self._state.pending_preprovision_notices:
            return False
        # 与上面那条过期提示同语义：命中即摘掉，同一次挂起不会被第二条消息再命中。
        self._state.pending_preprovision_notices.discard(user_id)
        return True

    def list_user_memory(self, *, user_id: str) -> list[UserMemoryEntry]:
        self._log.add("store.list_user_memory", user_id=user_id)
        return list(self._state.user_memory.get(user_id, ()))

    def remember_user_memory(
        self, *, user_id: str, memory_type: str, memory_key: str, memory_value: str
    ) -> str | None:
        self._log.add(
            "store.remember_user_memory",
            user_id=user_id,
            memory_type=memory_type,
            memory_key=memory_key,
        )
        self._maybe_fail("remember_user_memory")
        entries = self._state.user_memory.setdefault(user_id, [])
        for index, entry in enumerate(entries):
            if entry.memory_type == memory_type and entry.memory_key == memory_key:
                updated = dataclasses.replace(entry, memory_value=memory_value)
                entries[index] = updated
                return updated.memory_id
        if len(entries) >= MAX_MEMORY_ENTRIES_PER_USER:
            return None
        new_entry = UserMemoryEntry(
            memory_id=new_id("mem"),
            memory_type=memory_type,
            memory_key=memory_key,
            memory_value=memory_value,
            created_at=datetime.now(UTC),
        )
        entries.append(new_entry)
        return new_entry.memory_id

    def forget_user_memory(self, *, user_id: str, memory_id: str) -> UserMemoryEntry | None:
        self._log.add("store.forget_user_memory", user_id=user_id, memory_id=memory_id)
        entries = self._state.user_memory.get(user_id, [])
        for index, entry in enumerate(entries):
            if entry.memory_id == memory_id:
                del entries[index]
                return entry
        return None

    def clear_user_memory(self, *, user_id: str) -> int:
        self._log.add("store.clear_user_memory", user_id=user_id)
        entries = self._state.user_memory.pop(user_id, [])
        return len(entries)

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
        fail_error: Exception | None = None,
        force_clear_agent_session_result: bool | None = None,
        force_claim_conversation_result: bool | None = None,
    ) -> None:
        self._state = state
        self._log = log
        self._fail_on = fail_on
        self._fail_error = fail_error
        self._force_clear_agent_session_result = force_clear_agent_session_result
        self._force_claim_conversation_result = force_claim_conversation_result

    def claim_queue_failure_notice(self, *, event_id: str) -> bool:
        """在假状态里取得一次队列失败提示的发送权（Issue #465）。

        与真库 `PostgresGatewayStore.claim_queue_failure_notice` 同语义：同一个
        ``event_id`` 只有第一次调用返回 ``True``，之后恒 ``False``——用来断言
        "同一条持续失败的事件被飞书重投多次时，`gateway.queue_failed` 提示只发
        一次"。此前本假实现完全没有这个方法，`getattr(store, ..., None) is None`
        恒真，导致所有注入 `insert_task` 失败的既有用例都走了"没有该能力的旧
        注入 store"分支——原始异常直接穿透 `handle_message`，与生产环境（真实
        store 一定有这个方法）的实际行为不符。补上之后这些用例改为断言生产环境
        真正会发生的事：用户收到一条诚实的 `gateway.queue_failed` 提示。
        """

        self._log.add("store.claim_queue_failure_notice", event_id=event_id)
        if self._fail_on == "claim_queue_failure_notice":
            raise RuntimeError("注入失败：claim_queue_failure_notice")
        if event_id in self._state.queue_failure_notices_claimed:
            return False
        self._state.queue_failure_notices_claimed.add(event_id)
        return True

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
        token = datetime(2026, 8, 19, tzinfo=UTC) + timedelta(seconds=self._state.claim_generation)
        self._state.onboarding_dispatched[pending.event_id] = token
        return dataclasses.replace(pending, claim_token=token)

    def release_onboarding_claim(self, *, event_id: str, claim_token=None) -> None:
        """把**自己那一次**认领放回去：代次对得上才清，对不上什么都不做。"""

        self._log.add("store.release_onboarding_claim", event_id=event_id, claim_token=claim_token)
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
            fail_error=self._fail_error,
            force_clear_agent_session_result=self._force_clear_agent_session_result,
            force_claim_conversation_result=self._force_claim_conversation_result,
        )
        # 异常时**不提交**：暂存区里的事件行、抢占、任务一起消失，
        # 与真库的事务回滚同语义。
        yield transaction
        transaction.commit()


def provisioned_user(open_id: str = "ou_1", user_id: str = "usr_1") -> UserRecord:
    return UserRecord(user_id=user_id, state=UserState.ACTIVE)
