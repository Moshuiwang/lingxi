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

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Protocol

from lingxi.core.user_memory import UserMemoryEntry


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
    # 飞书消息类型（``text`` / ``image`` / ``audio`` / ``post`` …）。本批只处理
    # ``text``；其余类型照常加表情与记审计，但**不入队**——把一条语音当成空问题
    # 排进队列，用户只会拿到一个莫名其妙的失败。
    message_type: str = "text"


class UserState(str, Enum):
    """用户在 gateway 眼里的三种状态。

    刻意是自己的枚举而不是直接用 ``app_user`` 的列值：数据库那两列
    （``provisioning_state`` / ``account_state``）的取值域服务于开通流程，管线只关心
    "能不能给他排任务"。映射写在 adapters 层。
    """

    NOT_PROVISIONED = "not_provisioned"
    # 开通已经启动、正在建环境 / 发权限 / 等 MCP 同步。**与"还没开始"必须分开**：
    # 合同给这两个阶段规定了不同的固定提示（「已收到，正在核对」对前者，「权限正在同步，
    # 预计最多需要十五分钟」对后者），合并会把第一条提示错用在第二个阶段，用户每问一次
    # 都被告知"正在核对身份"，而系统其实早就核对完了。
    PROVISIONING = "provisioning"
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
    #: ``running_task_id`` 指向的那条 ``task`` 当前的 ``status``（Issue #465，
    #: S-3：忙碌提示文案如实区分"排队中"与"处理中"）。``running_task_id`` 为
    #: ``None`` 时恒为 ``None``；否则应为 ``'queued'``/``'running'`` 等
    #: `task.status` 取值之一。放在事务开始时读到的同一次快照里，与
    #: ``running_task_id`` 同源、同一致性边界——不是第二次查询拼出来的。
    #: 新增字段带默认值，兼容既有注入式测试直接用位置参数或旧关键字构造
    #: 本类而不知道这个字段。
    running_task_status: str | None = None


@dataclass(frozen=True)
class PendingPreprovisionNotice:
    """一句还没说的预开通首聊提示（Issue #541），以及渲染它所需的权限快照。

    ``permissions``：该用户当前权限版本已发布的权限文档文本（``publish_outbox``
    的内容快照，与 ``onboarding.completed`` 的公司/职能取值同一来源）；快照不可用
    时为 ``None``——挂起仍然成立，只是这一次渲染不出来（`GatewayTransaction.
    peek_preprovision_notice` 的文档交代了两种成因与调用方的处理姿态）。"""

    permissions: str | None = None


class HandledAs(str, Enum):
    """``inbound_event.handled_as`` 的取值，与迁移 013 的 CHECK 一致。"""

    TASK_QUEUED = "task_queued"
    BUSY_HINT = "busy_hint"
    # 未开通用户：本批只记录「收到过、没受理」，不把正文交给任何下游。
    NOT_PROVISIONED = "not_provisioned"
    # #65：事件提交后触发一次自动匹配与开通编排。
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


class OnboardingState(str, Enum):
    """自动开通编排的结果；具体身份、权限和外部同步由注入层负责。"""

    STARTED = "started"
    MATCHED = "matched"
    COMPLETED = "completed"
    NOT_AUTHORIZED = "not_authorized"
    SYNC_TIMEOUT = "sync_timeout"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class OnboardingMessage:
    """ContentCatalog 的 key 与已经由编排层确定的展示变量。"""

    key: str
    values: tuple[tuple[str, object], ...] = ()

    def as_values(self) -> dict[str, object]:
        return dict(self.values)


@dataclass(frozen=True)
class OnboardingResult:
    """自动匹配/开通编排返回给 gateway 的受控结果。

    ``grant_not_applied``（rc25 修复包 F2，只供批量清单口径）：系统触发（预开通）
    本轮带着名单预授权、却因为用户**已经 active** 在续行前复核处提前收口时为真——
    终态仍是 ``completed``（这个人确实开通着，不是失败），但名单答应的那笔授权
    **没有**落库。ops 清单据此单独归类，不计入"成功预开通"；要不要给已 active
    用户补上名单权限是产品语义，由产品负责人另行裁定，这里绝不静默扩权。首聊
    路径（无预授权）恒为 ``False``，gateway 不读它。"""

    state: OnboardingState
    messages: tuple[OnboardingMessage, ...] = ()
    failure_reason: str | None = None
    grant_not_applied: bool = False


#: **可重试的不受理原因**：编排根本没有开始跑（或明确让位），因此这条事件的认领必须被
#: 释放、交给下一轮重新捞。与「跑过了、得到了一个失败结论」是两件完全不同的事——后者不
#: 释放（重跑不会改变结论，只会持续冲击外部系统）。
#:
#: 住在 ``ports`` 而不是编排实现里：它是 :class:`OnboardingRunner` **合同**的一部分，
#: 认领方要照它分流；放进实现会把整条权限链拖进每一个只 import ``ports`` 的进程闭包。
RETRYABLE_REASONS: frozenset[str] = frozenset(
    {"executor_unavailable", "stopping", "already_running"}
)


@dataclass(frozen=True)
class PendingOnboarding:
    """一条**已认领、但没能确认交给开通编排**的入站事件（Issue #65 轻审 P2-2）。

    字段与 ``OnboardingRunner.start`` 的参数一一对应，别的都不带：对账扫描重新交接
    时和首次触发走同一条边界，未开通用户的正文同样到不了编排层。
    """

    event_id: str
    open_id: str
    trace_id: str
    #: **这一次认领的代次**（认领语句写进去的 ``onboarding_dispatched_at``）。释放时必须
    #: 带上它做 CAS，否则存在 ABA：A 释放 → B 重新认领 → A 的重试再释放一次 → **B 的认领
    #: 被清掉**，那条链于是在没人看着的情况下被第三方"解锁"，可能被并发认领两次。
    #: ``None`` 表示"不是我们认领的"，此时任何释放都不该发生。
    claim_token: datetime | None = None


class OnboardingRunner(Protocol):
    """提交 gateway 事件后启动既有身份链与开通编排。

    参数刻意只有事件身份；未开通用户的入站正文必须丢弃，不能进入身份或权限链。
    ``start`` 必须按 event_id/open_id 幂等，具体持久化与外部依赖由注入实现负责。
    **幂等在这里不是"最好有"**：提交与触发之间的崩溃会留下孤儿事件，对账扫描
    （``onboarding_recovery.OnboardingReconciler``）会把它重新交接一次，而崩溃点也
    可能落在"编排已经跑了一半"之后。
    """

    def start(
        self,
        *,
        event_id: str,
        open_id: str,
        trace_id: str,
        claim_token: datetime | None = None,
    ) -> OnboardingResult: ...


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
        reply_to_message_id: str | None = None,
    ) -> None: ...

    def clear_agent_session(self, *, conversation_id: str) -> bool:
        """清空该话题的会话上下文；话题已被占用时返回 ``False`` 且不改任何行。"""

    def discard_stale_agent_session(self, *, conversation_id: str) -> None:
        """入队判定「不续用旧会话」时，把已判废的 ``agent_session_id`` 置空并排队
        物理清理（与入队同事务）。与 ``clear_agent_session``（/new）不同：调用时
        本事务已抢占话题，不做忙碌判定，也不触碰已送达正文的保留边界。"""

    def request_stop(self, *, conversation_id: str) -> str | None:
        """给该话题运行中的任务置 ``stop_requested``；返回被置的任务标识或 ``None``。"""

    def notify_task_queued(self) -> None:
        """发出 ``NOTIFY task_queued``。在事务内调用，随提交一起对外可见。"""

    def consume_delivery_expired_notice(self, *, conversation_id: str) -> bool:
        """该话题是否有尚未提示过的「投递已过期」任务；命中即原子标记为已提示
        （Issue #152、`V-投递-06` 后半句）。"""

    def peek_preprovision_notice(self, *, user_id: str) -> PendingPreprovisionNotice | None:
        """这个人有没有一句"你已经被提前开通了"还没说；有则**只读**返回渲染这句话
        所需的权限快照，**不消费**一次性标志（rc25 修复包 F1）。

        与 :meth:`consume_preprovision_notice` 拆成两步的理由：那句话的模板要求带上
        真实的公司/职能范围，渲染可能失败（权限快照过了九十天保留期、内容目录与代码
        分两次合入的中间态）。先消费再渲染，失败会把一次性标志白白烧掉，这个人**永远**
        收不到产品承诺的那句话；因此调用方必须先用本方法拿快照、渲染成功之后才调
        :meth:`consume_preprovision_notice`。并发安全不靠本方法：两个事务同时 peek 到
        同一句挂起，最终只有一个能在 consume 的原子 ``UPDATE`` 上命中。

        ``permissions`` 是该用户**当前权限版本**已发布的权限文档文本（与开通链发
        ``onboarding.completed`` 时 ``describe_scope(parse_permissions(...))`` 用的
        同一来源）；快照不可用（被保留期擦除、当前版本没有已发布意图）时为 ``None``，
        由调用方按渲染失败处理。"""

    def consume_preprovision_notice(self, *, user_id: str) -> bool:
        """把"你已经被提前开通了"那句一次性提示原子标记为已提示
        （Issue #541 预开通，产品负责人裁定 4）。

        与 :meth:`consume_delivery_expired_notice` 同型：查询与标记落在同一条
        ``UPDATE``，与调用方所在的入站消息事务一起提交或回滚，因此"只提示一次"不依赖
        任何额外的读锁。**按用户**而不是按话题：预开通是给这个人开的，他第一次说话
        落在哪个话题上都算首聊。挂起端见
        ``core/identity/onboarding_ports.UserStateStore.mark_preprovision_notice_pending``。
        调用次序见 :meth:`peek_preprovision_notice`：渲染成功之后才允许消费。"""

    def list_user_memory(self, *, user_id: str) -> list[UserMemoryEntry]:
        """按用户取全部记忆，``/memory list`` 用（Issue #357 S-H3-3）。"""

    def remember_user_memory(
        self, *, user_id: str, memory_type: str, memory_key: str, memory_value: str
    ) -> str | None:
        """登记一条记忆（同 key 已存在则更新）；新增触达上限时返回 ``None`` 且
        不写入，不做静默截断。"""

    def forget_user_memory(self, *, user_id: str, memory_id: str) -> UserMemoryEntry | None:
        """删除属于该用户的一条记忆，返回被删除那一行的内容（rc22 B-8-1：调用方
        据此在回执里回显，供用户自校验删对了）；未删除任何行时返回 ``None``。
        跨用户传入他人 memory_id 结构性地不生效。"""

    def clear_user_memory(self, *, user_id: str) -> int:
        """清空该用户的全部记忆，返回清掉的行数；``/memory clear`` 与停用/权限
        真变两处清除钩子共用同一个方法。"""


class GatewayStore(Protocol):
    def transaction(self) -> AbstractContextManager[GatewayTransaction]: ...

    def claim_queue_failure_notice(self, *, event_id: str) -> bool: ...

    def mark_onboarding_dispatched(self, *, event_id: str) -> None:
        """记下"这条事件已经交给开通编排了"。

        刻意**不在** ``GatewayTransaction`` 上：交接发生在入队事务提交**之后**，
        写进同一个事务在时间上不可能（那时还没调用编排），写进事务反而会让账本
        在编排从未被调用时就宣称交接完成。
        """

    def release_onboarding_claim(
        self, *, event_id: str, claim_token: datetime | None = None
    ) -> None:
        """把**自己那一次**认领放回去（``onboarding_dispatched_at`` 置回 ``NULL``）。

        :meth:`claim_stale_onboarding` 是「取出即记账」，而在它之后**这条事件仍然可能
        根本没被执行**：执行器满位、停机信号刚好落在中间、同一个人已有链在跑。没有这条
        反向路径，那些事件此后永远不会再被任何人认领——用户只剩一个「已收到」的表情，
        不建档、不发权限、也收不到任何终态。这正是失败关闭桩「认领即平账」那条老缺陷
        换了个入口又走回来。

        **只放回真的没跑成的**：一条已经得出结论的事件不释放，重跑不会改变结论，只会
        持续冲击外部系统。

        ``claim_token`` 是**认领代次**，实现必须拿它做 CAS：只有当行上的
        ``onboarding_dispatched_at`` 仍然等于这个值时才清空。少了它就有 ABA——A 释放 →
        B 重新认领 → A 的重试再释放一次 → B 的认领被清掉。``None`` 表示调用方没有认领
        代次，实现必须**什么都不做**（宁可留着不放，也不能撤销别人的认领）。
        """

    def claim_stale_onboarding(self, *, older_than: timedelta) -> PendingOnboarding | None:
        """认领**一条**超过 ``older_than`` 仍未确认交接的事件，并原子标记为已交接。

        标记与取出必须在同一条语句里完成：多个 gateway 实例同时扫描时，一条孤儿只
        能被其中一个拿到，否则对账本身会变成重复触发外部开通的来源。

        **一次只认领一条**，而不是取一批回来慢慢处理：认领即记账，一批取回来之后
        如果停机信号在中途到达，剩下那些已经被记成"已交接"却从未交出去的行会永远
        没人再看。逐条认领让"随时停"天然无损——没轮到的行还没被认领。
        """


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
