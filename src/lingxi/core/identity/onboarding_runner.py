"""正式首次开通编排：把身份、匹配、建档、用户环境、权限发布与就绪确认串成一条链。

[Issue #65](https://github.com/Moshuiwang/lingxi/issues/65) 的编排层，
[Epic D / #160](https://github.com/Moshuiwang/lingxi/issues/160) 的 S-D-02。它替换的是
``apps/gateway`` 里那个失败关闭桩 ``_UnavailableOnboarding``——在它存在之前，生产上每一个
未开通用户的首条消息都只得到冻结的 ``LX-ONBOARD-001``。

本模块住在 ``core/``，因此不 import 任何适配器、不发请求、不连数据库、不读时钟：组织快照
读取、在职状态实时回读、花名册、银河快照、建档、用户环境、令牌签发、权限决定、就绪探针、
用户通知与账本全部以 ``Protocol`` 注入，全部断言可以在没有网络也没有数据库的机器上跑完，
**也不需要真的等十五分钟**。

## 一次开通的固定次序

```
身份定位（组织快照 + 在职实时回读，Epic B）
  → 花名册工号 / 邮箱
  → 银河唯一匹配 + 权限聚合
  → 建档（#89 写侧合同，core/identity/provisioning.py）
  → 复核该用户此刻还该不该继续开通
  → 用户环境创建（含按用户 .mcp.json 的 Bearer 落盘）
  → 令牌签发 + 权限发布意图（Epic C）
  → 等发布真的写出去并读回一致
  → 当前用户 MCP 就绪确认（探针法，Epic C）
  → provisioning_state = active + 成功通知
```

次序不可调换，每一步的失败去向都显式写在 :meth:`AutoOnboardingRunner._run` 里。

## 三类拒绝（[接口设计 §8.1](../../../../docs/技术设计/接口设计.md)）与它们的用户出口

| 类别 | 例子 | 用户看到 |
|---|---|---|
| **确定性业务失败** | 定位不到、多条、双键冲突、资料不完整、非在职、无受支持职能、`incomplete_identity` | 冻结的「无可用银河权限」 |
| **专用主体** | `delegated_subject`（判定层或建档触发器） | 冻结的「专用账号不提供问数服务」 |
| **本侧故障** | 组织资料不可用、`storage_integrity`、用户环境写不出去、发布没能完成、探针技术失败 | 冻结的 `LX-ONBOARD-001` |

外加一条**等待类**终态：十五分钟预算耗尽仍未就绪 → 冻结的「权限同步未完成，已转交处理」。
四类互斥且**先到先得**：一旦选定终态，后置异常不得把它改写成另一种用户结论（`V-开通-13`）。

## 为什么 ``start`` 立刻返回 ``started``

``EventPipeline._start_onboarding`` 在 **gateway 的长连接事件线程**里同步调用
``OnboardingRunner.start``；``OnboardingReconciler`` 在**投递消费线程**里调用同一个实例。
真实编排的单次耗时可达分钟级（产品合同允许权限同步等到十五分钟），把它同步跑在这两条
线程的任何一条上，代价分别是"gateway 十五分钟收不到任何消息"和"十五分钟一条投递都发
不出去"——这正是 Issue #65 钉在开工卡上的「共用线程复核」。

因此 ``start`` 只做三件事：**按 ``open_id`` 去重 → 交给注入的执行器 → 返回
``OnboardingState.STARTED``**。用户刚收到的「已收到，正在核对，请稍候」就是这一轮的完整
交代（``ports.OnboardingState`` 对 ``started`` 的定义），终态由本模块自己**主动私聊**告诉
用户。这同时定夺了 #65 留下的「对账恢复路径的用户通知方案」：**编排自担通知 + 幂等**，
而不是往 ``inbound_event`` 里多存两个路由标识——后者是一次扩大数据范围的决定，而通知本来
就只需要 ``open_id``，它已经在 ``PendingOnboarding`` 里了。

## 并发与共享可变状态

本类的实例状态只有一个 ``dict``（``open_id`` → 正在跑的事件标识）和保护它的 ``Lock``，
其余全是注入的无状态协作者。三条线程会碰它：长连接线程（``start``）、投递线程（对账的
``start``）、执行线程（跑链）。三者对那个 ``dict`` 的每一次读写都在锁内，并且**先登记再
提交任务**——反过来会让两次几乎同时的 ``start`` 各自提交一条链。

链本身跑在执行线程上，**不与任何 gateway 对象共享内存**：它只通过注入的存储与外部接口
产生效果，而那些效果各自的并发边界由它们自己的实现负责（建档按 ``feishu_open_id`` 幂等、
令牌签发 ``ON CONFLICT DO NOTHING``、权限决定按 ``app_user`` 行锁串行、发布意图有
``UNIQUE (user_id, permission_version)``）。

进程内去重不能替代持久幂等：重启会清空那个 ``dict``。跨进程与跨重启的幂等由上面那串数据库
约束承担，本类只保证**同一进程内同一时刻不会有两条链跑同一个人**。

## ``already_provisioned`` ≠ 这个人现在还该被开通

建档合同（``core/identity/provisioning.py``）明说：它只保证「档在、且这次重入没有把它写
坏」，不判断这个人此刻还该不该继续。对账扫描的重交接窗口可以长达一整个扫描周期，管理员在
这段时间里停用账号是真实形状。因此 :meth:`AutoOnboardingRunner._recheck_still_provisionable`
在建档之后、用户环境与权限发布**之前**重新读一次 ``account_state`` 与 ``provisioning_state``：

- ``account_state != 'enabled'`` → 停止开通，不建环境、不发权限，用户按「账号已停用」告知；
- ``provisioning_state == 'active'`` → 这个人已经开通完了，直接收口，**不重复创建环境、
  不重复发布**（`V-开通-14`），也不再发第二条成功提示。

## 发布由谁执行

**本模块只排发布意图，不自己写外部权限表格。** 外部表格的唯一写入方是
``lingxi-scheduler`` 的发布消费职责（单一写入负责人）：两个进程同时写同一张表是这条链上
最贵的并发错误。本模块在排完意图之后只**观察**这条意图的状态，直到它 ``published``
（发布并逐字段读回一致）才进入就绪确认。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol, Sequence

from lingxi.core.conversation.ports import (
    OnboardingMessage,
    OnboardingResult,
    OnboardingState,
)
from lingxi.core.identity.first_contact import (
    EmploymentStatus,
    FirstContactOutcome,
    IdentityRecordDraft,
    decide_first_contact,
    locate_by_open_id,
)
from lingxi.core.identity.org_snapshot import DirectoryAvailability, SnapshotMember
from lingxi.core.identity.provisioning import (
    IdentityProvisioning,
    ProvisioningRejection,
    ProvisioningRequest,
    UserProvisioningStatus,
)
from lingxi.core.permission.account_match import MATCHED, match_galaxy_account
from lingxi.core.permission.mcp_readiness import ReadinessBinding, ReadinessOutcome
from lingxi.core.permission.notification import describe_scope
from lingxi.core.permission.publish_row import (
    aggregate_permission,
    build_publish_row,
    parse_permissions,
)

logger = logging.getLogger(__name__)

_UTC = timezone.utc

#: 权限发布意图的原因码。与每日重算的原因码分开，让审计能一眼看出「这一版是开通排的
#: 还是重算排的」。
FIRST_ONBOARDING_REASON = "first_onboarding"

#: ``app_user.provisioning_state`` 在本链上的推进次序。只前进、不回退（`V-开通-04`）。
STATE_MATCHING = "matching"
STATE_PROVISIONING = "provisioning"
STATE_MCP_SYNCING = "mcp_syncing"
STATE_ACTIVE = "active"

#: 冻结文案的内容目录 key。用常量而不是散落字面量：这几条是产品负责人逐字批准过的终态，
#: 改一个 key 就等于换一条用户可见结论。
KEY_MATCHED = "onboarding.matched"
KEY_COMPLETED = "onboarding.completed"
KEY_NOT_AUTHORIZED = "onboarding.not_authorized"
KEY_SYNC_TIMEOUT = "onboarding.sync_timeout"
KEY_INTERNAL_ERROR = "onboarding.internal_error"
KEY_DELEGATED_SUBJECT = "onboarding.delegated_subject"
KEY_SUSPENDED = "gateway.suspended"


class OnboardingChainError(RuntimeError):
    """链上某一步的**本侧故障**：走 ``LX-ONBOARD-001``，不冒充业务结论。

    ``code`` 只有错误码，从不含身份原值、令牌或外部响应正文——它会进审计与日志。
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ----------------------------------------------------------------------
# 注入口
# ----------------------------------------------------------------------


class DirectorySource(Protocol):
    """组织快照侧的定位输入：资料可用性 + 候选成员（Epic B 的组织快照读取口）。"""

    def lookup(self, open_id: str) -> Any: ...


class EmploymentSource(Protocol):
    """**实时**在职状态。快照里没有、也不会有这个字段（`V-开通-07`）。

    返回 ``None`` 表示"读到了但判不出来"（字段缺失或非布尔），调用方按不可判定处理；
    读取本身失败请抛异常，由本模块归成本侧故障——把一次网络错误读成"不在职"会给一个
    在职员工发一条错误的终态结论。
    """

    def status(self, *, tenant_key: str, open_id: str) -> EmploymentStatus | None: ...


class RosterSource(Protocol):
    """花名册当前快照的全部行；``None`` 表示库里根本没有快照。"""

    def rows(self) -> Sequence[Mapping[str, Any]] | None: ...


class GalaxySource(Protocol):
    """银河当前有效批次；``None`` 表示没有有效批次。"""

    def load_current(self) -> Any: ...


class UserStateStore(Protocol):
    """``app_user`` 的状态读回与推进。"""

    def read_status(self, user_id: str) -> UserProvisioningStatus | None: ...

    def advance_provisioning_state(self, user_id: str, *, to: str) -> bool:
        """把 ``provisioning_state`` 往前推。**只前进不回退**（`V-开通-04`）。"""


@dataclass(frozen=True)
class EnvironmentResult:
    """一次用户环境创建的结果。``created`` 为假表示"本来就在"，同样算成立。"""

    created: bool


class UserEnvironmentSource(Protocol):
    """用户环境创建。落盘凭据的权限纪律由实现负责（440 + 日志脱敏）。"""

    def ensure(self, *, user_id: str, mcp_token: str) -> EnvironmentResult: ...


class TokenIssuer(Protocol):
    """问数 MCP 访问令牌签发（幂等，已存在即返回既有那一份）。"""

    def issue_token(self, user_id: str) -> Any: ...


class PermissionDecisionStore(Protocol):
    """权限决定 + 发布意图（同事务），以及意图状态的回读。"""

    def record_decision(
        self, *, user_id: str, row: Any, reason: str, decided_at: datetime
    ) -> Any: ...

    def load(self, outbox_id: str) -> Any: ...


class ReadinessConfirmer(Protocol):
    """阻塞式 MCP 就绪确认（``core/permission/mcp_readiness.McpReadinessConfirmation``）。"""

    def confirm(self, binding: ReadinessBinding, *, permissions: str) -> Any: ...


class UserNotifier(Protocol):
    """终态的主动私聊。同一逻辑通知的首发与重试必须传同一个 ``dedupe_key``。"""

    def send(
        self, *, open_id: str, key: str, values: Mapping[str, object], dedupe_key: str
    ) -> None: ...


class DispatchLedger(Protocol):
    """"这条事件的开通编排已经跑完"的账本（``inbound_event.onboarding_dispatched_at``）。"""

    def mark_onboarding_dispatched(self, *, event_id: str) -> None: ...


class _AuditSink(Protocol):
    def record(self, action: str, /, **fields: object) -> None: ...


@dataclass(frozen=True)
class _Terminal:
    """一次开通的内部终态：状态 + 要发的那条文案 + 内部原因码。

    ``notify`` 为假的唯一场景是"这个人本来就已经 active"：那时再推一条结论，用户会在
    自己什么都没做的情况下收到第二遍开通提示。
    """

    state: OnboardingState
    key: str
    values: tuple[tuple[str, object], ...] = ()
    reason: str | None = None
    notify: bool = True

    def as_result(self) -> OnboardingResult:
        return OnboardingResult(
            state=self.state,
            messages=(OnboardingMessage(self.key, self.values),),
            failure_reason=self.reason,
        )


def _not_authorized(reason: str) -> _Terminal:
    return _Terminal(OnboardingState.NOT_AUTHORIZED, KEY_NOT_AUTHORIZED, reason=reason)


def _internal(reason: str) -> _Terminal:
    return _Terminal(OnboardingState.INTERNAL_ERROR, KEY_INTERNAL_ERROR, reason=reason)


class AutoOnboardingRunner:
    """正式的 ``OnboardingRunner``。装配见 ``apps/gateway/onboarding.py``。"""

    name = "首次开通编排"

    def __init__(
        self,
        *,
        directory: DirectorySource,
        employment: EmploymentSource,
        roster: RosterSource,
        galaxy: GalaxySource,
        provisioning: IdentityProvisioning,
        users: UserStateStore,
        environment: UserEnvironmentSource,
        tokens: TokenIssuer,
        decisions: PermissionDecisionStore,
        readiness: ReadinessConfirmer,
        notifier: UserNotifier,
        ledger: DispatchLedger,
        audit: _AuditSink,
        role_function_map: Mapping[str, str],
        delegated_subject: Callable[[], str | None],
        submit: Callable[[Callable[[], None]], bool],
        sleep: Callable[[float], None],
        clock: Callable[[], datetime] | None = None,
        should_stop: Callable[[], bool] | None = None,
        publish_wait_seconds: float = 120.0,
    ) -> None:
        if not callable(delegated_subject):
            # 每次判定现读一次登记，而不是装配时读一次存着：换主体之后旧值会让
            # 新的专用授权账号落回普通员工路径。读它也不该发生在进程启动那一刻
            # （那会让 gateway 起不起得来取决于数据库此刻通不通）。
            raise TypeError("专用主体登记必须是可调用的读取口")
        if not callable(submit):
            raise TypeError("必须注入执行器：start 绝不能在调用线程上跑完整条链")
        if not callable(sleep):
            # 与 ``McpReadinessConfirmation`` 同一条理由：缺省会把等待压成瞬时，
            # 而日志与记录看起来完全正常。
            raise TypeError("sleep 必须可调用：缺省会把发布等待压成瞬时")
        self._directory = directory
        self._employment = employment
        self._roster = roster
        self._galaxy = galaxy
        self._provisioning = provisioning
        self._users = users
        self._environment = environment
        self._tokens = tokens
        self._decisions = decisions
        self._readiness = readiness
        self._notifier = notifier
        self._ledger = ledger
        self._audit = audit
        self._role_function_map = role_function_map
        self._delegated_subject = delegated_subject
        self._submit = submit
        self._sleep = sleep
        self._clock = clock or (lambda: datetime.now(_UTC))
        self._should_stop = should_stop or (lambda: False)
        self._publish_wait_seconds = float(publish_wait_seconds)
        self._lock = threading.Lock()
        self._running: dict[str, str] = {}

    # ------------------------------------------------------------------
    # OnboardingRunner 合同
    # ------------------------------------------------------------------

    def start(self, *, event_id: str, open_id: str, trace_id: str) -> OnboardingResult:
        """认领并交给执行器。**永远不在调用线程上跑链**（见模块文档「共用线程复核」）。"""

        if self._should_stop():
            # 停机中不再开新链：一条刚起头就被进程退出打断的开通比压根没开始的更难
            # 收拾（外部副作用已经产生、账本却还没写）。
            self._audit.record(
                "onboarding.start_declined_while_stopping",
                event_id=event_id,
                trace_id=trace_id,
            )
            return _internal("stopping").as_result()

        with self._lock:
            running_for = self._running.get(open_id)
            if running_for is not None:
                # 同一个人已经有一条链在跑（例如对账重交接撞上用户的新消息）。
                # 第二次不再开链，也不再发第二条提示。
                self._audit.record(
                    "onboarding.already_running",
                    event_id=event_id,
                    running_event_id=running_for,
                    trace_id=trace_id,
                )
                return OnboardingResult(state=OnboardingState.STARTED)
            # **先登记再提交**：反过来会让两次几乎同时的 start 各自提交一条链。
            self._running[open_id] = event_id

        def task() -> None:
            try:
                self._execute(event_id=event_id, open_id=open_id, trace_id=trace_id)
            finally:
                self._release(open_id, event_id)

        accepted = False
        try:
            accepted = bool(self._submit(task))
        except Exception as error:  # noqa: BLE001 - 提交失败必须撤销登记
            self._audit.record(
                "onboarding.submit_failed",
                event_id=event_id,
                error=type(error).__name__,
                trace_id=trace_id,
            )
        if not accepted:
            self._release(open_id, event_id)
            # 队列满或执行器已停：**不假装接手**，给出明确的内部故障终态。
            self._audit.record(
                "onboarding.rejected_by_executor", event_id=event_id, trace_id=trace_id
            )
            return _internal("executor_unavailable").as_result()
        return OnboardingResult(state=OnboardingState.STARTED)

    def _release(self, open_id: str, event_id: str) -> None:
        with self._lock:
            if self._running.get(open_id) == event_id:
                del self._running[open_id]

    # ------------------------------------------------------------------
    # 执行线程
    # ------------------------------------------------------------------

    def _execute(self, *, event_id: str, open_id: str, trace_id: str) -> None:
        """跑完一条链、通知用户、记账。**异常不外抛**（它跑在执行线程上）。"""

        try:
            terminal = self._run(open_id=open_id, trace_id=trace_id)
        except OnboardingChainError as error:
            terminal = _internal(error.code)
        except Exception as error:  # noqa: BLE001 - 未预料的失败也必须有用户结论
            self._audit.record(
                "onboarding.chain_failed",
                event_id=event_id,
                error=type(error).__name__,
                trace_id=trace_id,
            )
            logger.exception("首次开通编排未预料的失败 event=%s", event_id)
            terminal = _internal(f"unexpected_{type(error).__name__}")

        self._audit.record(
            "onboarding.result",
            event_id=event_id,
            state=terminal.state.value,
            failure_reason=terminal.reason,
            content_key=terminal.key if terminal.notify else "",
            trace_id=trace_id,
        )
        if terminal.notify:
            self._notify(open_id=open_id, event_id=event_id, terminal=terminal, trace_id=trace_id)
        # 记账排在通知**之后**：账本的唯一用途是让对账扫描不再重复交接，而"用户已经拿到
        # 结论"才是这条事件真正处理完的标志。反过来会让一次通知失败变成"系统认为处理完了、
        # 用户什么都没收到"，而且没有任何东西会再来收拾。
        try:
            self._ledger.mark_onboarding_dispatched(event_id=event_id)
        except Exception as error:  # noqa: BLE001 - 记不上账最坏只是被对账再交接一次
            self._audit.record(
                "onboarding.dispatch_record_failed",
                event_id=event_id,
                error=type(error).__name__,
                trace_id=trace_id,
            )

    def _notify(
        self, *, open_id: str, event_id: str, terminal: _Terminal, trace_id: str
    ) -> None:
        """把终态主动私聊给用户。失败只留响亮审计，**不改写终态**。"""

        try:
            self._notifier.send(
                open_id=open_id,
                key=terminal.key,
                values=dict(terminal.values),
                # 去重键绑定**事件**：同一条首聊事件的重复执行（对账重交接、重启后重跑）
                # 只会让用户看到同一条结论一次。
                dedupe_key=f"onboarding:{event_id}",
            )
        except Exception as error:  # noqa: BLE001
            self._audit.record(
                "onboarding.notify_failed",
                event_id=event_id,
                state=terminal.state.value,
                error=type(error).__name__,
                trace_id=trace_id,
            )

    # ------------------------------------------------------------------
    # 链
    # ------------------------------------------------------------------

    def _run(self, *, open_id: str, trace_id: str) -> _Terminal:
        """一次开通的固定次序。每一步的失败去向都在这里显式返回。"""

        located = self._locate(open_id)
        if isinstance(located, _Terminal):
            return located
        member = located

        matched = self._match(member, trace_id=trace_id)
        if isinstance(matched, _Terminal):
            return matched
        request, aggregate = matched

        provisioned = self._provision(request)
        if isinstance(provisioned, _Terminal):
            return provisioned
        user_id = provisioned

        recheck = self._recheck_still_provisionable(user_id, trace_id=trace_id)
        if recheck is not None:
            return recheck

        issued = self._issue_token(user_id)
        self._create_environment(user_id, issued)
        self._users.advance_provisioning_state(user_id, to=STATE_PROVISIONING)

        published = self._publish(user_id, request, aggregate, issued)
        if isinstance(published, _Terminal):
            return published
        permission_version, permissions = published
        self._users.advance_provisioning_state(user_id, to=STATE_MCP_SYNCING)

        return self._confirm(
            user_id=user_id,
            permission_version=permission_version,
            permissions=permissions,
            trace_id=trace_id,
        )

    # ---- 1. 身份定位（Epic B） ------------------------------------------

    def _locate(self, open_id: str) -> _Terminal | SnapshotMember:
        try:
            lookup = self._directory.lookup(open_id)
        except Exception as error:  # noqa: BLE001 - 读不到组织资料是本侧故障
            raise OnboardingChainError(
                f"directory_read_failed_{type(error).__name__}"
            ) from error
        availability = getattr(lookup, "availability", None)
        if not isinstance(availability, DirectoryAvailability):
            raise OnboardingChainError("directory_availability_unreadable")
        members = tuple(getattr(lookup, "members", ()) or ())
        location = locate_by_open_id(open_id, members)

        employment: EmploymentStatus | None = None
        if (
            availability is DirectoryAvailability.AVAILABLE
            and location.member is not None
            and location.member.tenant_key
        ):
            # **在职状态只能实时回读**（`V-开通-07`）：可见范围不做在职过滤，710 人实测
            # 中含 5 名冻结、1 名未加入。读取失败是本侧故障，不是"不在职"。
            try:
                employment = self._employment.status(
                    tenant_key=location.member.tenant_key, open_id=open_id
                )
            except Exception as error:  # noqa: BLE001
                raise OnboardingChainError(
                    f"employment_read_failed_{type(error).__name__}"
                ) from error

        try:
            delegated_subject_open_id = self._delegated_subject()
        except Exception as error:  # noqa: BLE001 - 读不到登记不等于"没有专用主体"
            # 失败开放会让专用授权账号落回普通员工路径并被建档（`V-身份-02` 的反面）。
            raise OnboardingChainError(
                f"delegated_subject_read_failed_{type(error).__name__}"
            ) from error

        decision = decide_first_contact(
            open_id=open_id,
            location=location,
            employment=employment,
            directory=availability,
            delegated_subject_open_id=delegated_subject_open_id,
        )
        if decision.outcome is FirstContactOutcome.RECORD_READY:
            assert location.member is not None  # RECORD_READY 蕴含定位成功
            return location.member
        if decision.outcome is FirstContactOutcome.DELEGATED_SUBJECT_IGNORED:
            return _Terminal(
                OnboardingState.NOT_AUTHORIZED,
                KEY_DELEGATED_SUBJECT,
                reason="delegated_subject",
            )
        if decision.outcome is FirstContactOutcome.DIRECTORY_UNAVAILABLE:
            # 组织资料不可用时"定位不到"不是事实，只是我们暂时看不见 → 本侧故障。
            return _internal("directory_unavailable")
        reason = decision.failure_reason.value if decision.failure_reason else "not_located"
        return _not_authorized(reason)

    # ---- 2. 花名册 + 3. 银河唯一匹配 ------------------------------------

    def _match(
        self, member: SnapshotMember, *, trace_id: str
    ) -> _Terminal | tuple[ProvisioningRequest, Any]:
        roster_rows = self._roster.rows()
        if roster_rows is None:
            # 花名册快照根本不存在：这是**我们**缺一份数据，不是这个人没有权限。归成
            # "无可用银河权限"会把用户引去银河申请一个他其实已经有的权限。
            return _internal("roster_snapshot_missing")
        galaxy = self._galaxy.load_current()
        if galaxy is None:
            return _internal("galaxy_batch_missing")

        match = match_galaxy_account(member.user_id, roster_rows, galaxy.user_rows)
        if match.state != MATCHED or not match.galaxy_user_id:
            # 零条、多条、双键冲突、花名册重复、资料不完整全部走同一个用户出口，内部
            # 原因码仍然互不合并（`V-开通-17`）。
            return _not_authorized(match.reason)

        aggregate = aggregate_permission(
            galaxy_user_id=match.galaxy_user_id,
            user_role_rows=galaxy.role_rows(match.galaxy_user_id),
            datacountry_rows=galaxy.datacountry_rows(match.galaxy_user_id),
            country_rows=galaxy.country_rows,
            role_function_map=self._role_function_map,
        )
        self._audit.record("onboarding.aggregated", trace_id=trace_id, **aggregate.audit_facts())
        if not aggregate.granted:
            # 没有受支持职能 / 没有公司范围：同一条无权限出口。
            return _not_authorized(aggregate.reason)

        roster_row = roster_row_for(member.user_id, roster_rows)
        request = ProvisioningRequest.from_roster_row(draft_from_member(member), roster_row)
        return request, aggregate

    # ---- 4. 建档（#89 写侧） --------------------------------------------

    def _provision(self, request: ProvisioningRequest) -> _Terminal | str:
        result = self._provisioning.provision(request)
        if result.provisioned:
            assert result.app_user_id is not None  # ProvisioningResult 的不变式
            return result.app_user_id
        rejection = result.rejection
        if rejection is ProvisioningRejection.DELEGATED_SUBJECT:
            return _Terminal(
                OnboardingState.NOT_AUTHORIZED,
                KEY_DELEGATED_SUBJECT,
                reason="delegated_subject",
            )
        if rejection is not None and rejection.is_storage_fault:
            # **库把工号吞了**不是"你没有银河权限"：后者会把用户引去银河申请一个他其实
            # 已经有的权限（接口设计 §8.1）。
            return _internal(rejection.value)
        return _not_authorized(rejection.value if rejection else "provision_rejected")

    # ---- 5. 续行前复核 ---------------------------------------------------

    def _recheck_still_provisionable(self, user_id: str, *, trace_id: str) -> _Terminal | None:
        """``already_provisioned`` 不等于「这个人现在还该被开通」（接口设计 §8.1）。"""

        status = self._users.read_status(user_id)
        if status is None:
            # 刚刚建档成功却读不回来：库侧不一致，绝不当成"可以继续"。
            raise OnboardingChainError("user_row_disappeared")
        if status.account_state != "enabled":
            self._audit.record(
                "onboarding.halted_account_state",
                user=user_id,
                account_state=status.account_state,
                trace_id=trace_id,
            )
            return _Terminal(
                OnboardingState.NOT_AUTHORIZED, KEY_SUSPENDED, reason="account_not_enabled"
            )
        if status.provisioning_state == STATE_ACTIVE:
            # 已经开通完了（对账重交接窗口里被另一条链跑完）。不重复建环境、不重复发布，
            # 也不再推第二条成功提示（`V-开通-14`）。
            self._audit.record("onboarding.already_active", user=user_id, trace_id=trace_id)
            return _Terminal(
                OnboardingState.COMPLETED,
                KEY_COMPLETED,
                reason="already_active",
                notify=False,
            )
        return None

    # ---- 6. 令牌 + 用户环境 ---------------------------------------------

    def _issue_token(self, user_id: str) -> Any:
        try:
            return self._tokens.issue_token(user_id)
        except Exception as error:  # noqa: BLE001
            raise OnboardingChainError(f"token_issue_failed_{type(error).__name__}") from error

    def _create_environment(self, user_id: str, issued: Any) -> None:
        """创建用户环境并把该用户的 MCP Bearer 落进它的 ``.mcp.json``。

        **明文只在这一次调用里存在**：``reveal()`` 的返回值不进日志、不进审计、不进异常
        （``adapters/mcp_token_cipher.py`` 的同一条纪律）。
        """

        try:
            self._environment.ensure(user_id=user_id, mcp_token=issued.reveal())
        except Exception as error:  # noqa: BLE001
            raise OnboardingChainError(
                f"user_environment_failed_{type(error).__name__}"
            ) from error

    # ---- 7. 权限发布 ------------------------------------------------------

    def _publish(
        self,
        user_id: str,
        request: ProvisioningRequest,
        aggregate: Any,
        issued: Any,
    ) -> _Terminal | tuple[int, str]:
        if not request.email:
            # 发布行的 record_key/email 两列都来自邮箱；纯工号匹配成功但花名册没有邮箱时
            # 没有"这一行是谁的"的答案。归确定性业务失败，不是本侧故障。
            return _not_authorized("archived_identity_incomplete")
        row = build_publish_row(
            aggregate=aggregate,
            email=request.email,
            display_name=request.identity.display_name,
            decided_at=self._clock(),
            token_cipher=issued.token_cipher,
        )
        decision = self._decisions.record_decision(
            user_id=user_id,
            row=row,
            reason=FIRST_ONBOARDING_REASON,
            decided_at=self._clock(),
        )
        version = int(decision.permission_version)
        if decision.enqueued:
            waited = self._await_published(decision.outbox_id)
            if waited is not None:
                return waited
        # ``UNCHANGED``：内容与上一条仍然有效的意图逐字段相同（重入，或每日重算已经排
        # 过同一版）。那一条自己会被发布面消费，本链直接进入就绪确认。
        return version, row.permissions

    def _await_published(self, outbox_id: str) -> _Terminal | None:
        """等发布意图真的被写出去并逐字段读回一致。

        发布的**唯一**执行者是 ``lingxi-scheduler`` 的发布消费职责（单一写入负责人）。
        因此这里只**观察**意图的状态，不自己去写外部表格。

        等不到既不是"没有权限"，也不是"MCP 同步超时"——十五分钟那条终态说的是 MCP 侧
        同步，而这里是我们自己的发布面还没把行写出去，属本侧故障。
        """

        waited = 0.0
        step = 1.0
        while True:
            intent = self._decisions.load(outbox_id)
            status = getattr(intent, "status", None) if intent is not None else None
            if status == "published":
                return None
            if status in ("failed", "superseded"):
                # ``superseded``：本链排的这一版已经被更新的一版取代（撤权或重算）。
                # 那一版自己会被发布并确认，但**本次开通**不能据此宣告成功。
                return _internal(f"publish_{status}")
            if waited >= self._publish_wait_seconds or self._should_stop():
                return _internal("publish_not_completed")
            self._sleep(step)
            waited += step

    # ---- 8. MCP 就绪确认 + 9. active ------------------------------------

    def _confirm(
        self, *, user_id: str, permission_version: int, permissions: str, trace_id: str
    ) -> _Terminal:
        session = self._readiness.confirm(
            ReadinessBinding(user_id=user_id, permission_version=permission_version),
            permissions=permissions,
        )
        outcome = getattr(session, "outcome", None)
        if outcome is ReadinessOutcome.READY:
            # **只有到这里才写 active**：产品合同要求成功提示在环境创建、权限发布与当前
            # 用户 MCP 确认全部完成之后才发（`V-开通-11`）。
            self._users.advance_provisioning_state(user_id, to=STATE_ACTIVE)
            return self._completed(permissions)
        if outcome is ReadinessOutcome.TIMED_OUT:
            # 十五分钟预算耗尽：专用的等待类终态，**不与 LX-ONBOARD-001 混淆**
            # （`V-开通-13`）。状态留在 mcp_syncing，问数照常被拒（`V-开通-05`）。
            self._audit.record(
                "onboarding.sync_timeout",
                user=user_id,
                version=permission_version,
                trace_id=trace_id,
            )
            return _Terminal(
                OnboardingState.SYNC_TIMEOUT, KEY_SYNC_TIMEOUT, reason="mcp_sync_timeout"
            )
        if outcome is ReadinessOutcome.NO_PERMISSION:
            # 走到这一步的人一定有非空权限（聚合已经 granted），因此这条只可能来自本侧
            # 不一致，不是"去银河申请权限"的业务结论。
            return _internal("readiness_no_permission_after_grant")
        return _internal(
            f"readiness_{outcome.value if isinstance(outcome, ReadinessOutcome) else 'unknown'}"
        )

    def _completed(self, permissions: str) -> _Terminal:
        """成功文案必须报出**实际**公司与职能范围（产品合同「开通成功后」）。"""

        company, function = describe_scope(parse_permissions(permissions))
        return _Terminal(
            OnboardingState.COMPLETED,
            KEY_COMPLETED,
            values=(("company_name", company), ("function_name", function)),
        )


# ----------------------------------------------------------------------
# 纯函数
# ----------------------------------------------------------------------


def roster_row_for(
    personnel_id: str, rows: Sequence[Mapping[str, Any]]
) -> Mapping[str, Any] | None:
    """取该人员 ID 的**唯一**花名册行。

    多行时返回 ``None``——但走到这里已经不可能了：``match_galaxy_account`` 对同一人员 ID
    的多行一律判 ``not_found``（`V-开通-09`）。保留这一格是因为"建档时挑了其中一行"是一种
    会静默把别人的工号挂到这个人身上的错误，不能靠上游记得拦。
    """

    needle = str(personnel_id).strip()
    matched = [row for row in rows if str(row.get("personnel_id", "") or "").strip() == needle]
    return matched[0] if len(matched) == 1 else None


def draft_from_member(member: SnapshotMember) -> IdentityRecordDraft:
    """从快照成员组装建档草稿。

    与 ``decide_first_contact`` 内部的组装**逐字段相同**——那一份是判定的一部分、不外露，
    这一份是编排层拿去建档的。两处必须一致，由 ``tests/test_onboarding_runner.py`` 的专项
    用例钉住：不一致会让"判定说资料齐了"和"实际写进去的资料"分叉。
    """

    department = (
        member.department_names[0].strip()
        if member.department_names and member.department_names[0]
        else ""
    )
    return IdentityRecordDraft(
        feishu_open_id=member.open_id.strip(),
        feishu_user_id=member.user_id.strip(),
        feishu_union_id=member.union_id.strip(),
        display_name=member.display_name.strip(),
        display_name_locale=member.display_name_locale,
        department=department,
        tenant_key=member.tenant_key.strip(),
    )
