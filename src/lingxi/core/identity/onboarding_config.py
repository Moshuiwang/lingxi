"""首次开通编排的五组注入口。

分成五组而不是三十来个平铺参数，是为了让装配层一眼看出"这一格属于哪一类"：只读源、会
产生外部副作用的写侧、记账与告警、判据与预算、线程与时钟。

**几处刻意没有默认值**：内测名单闸、专用主体读取口、发布闸门、同邮箱绑定回读口。它们各自
决定的是"要不要把一整类人挡在门外"或"要不要往正式权限表写一行"，缺省放行会让闸形同虚设
且没有任何症状，因此漏接在构造期就是 ``TypeError``。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from lingxi.core.identity.onboarding_ports import (
    DirectorySource,
    DispatchLedger,
    EmailBindingSource,
    EmploymentSource,
    FailureReasonRecorder,
    GalaxySource,
    LegacyPermissionImporter,
    LocalOverrideSource,
    PermissionDecisionStore,
    ReadinessConfirmer,
    RosterSource,
    TokenIssuer,
    UserEnvironmentSource,
    UserNotifier,
    UserStateStore,
    _AuditSink,
)
from lingxi.core.identity.preprovision import PositionGrantImporter
from lingxi.core.identity.provisioning import IdentityProvisioning
from lingxi.core.identity.stock_token_source import StockTokenSource


@dataclass(frozen=True)
class OnboardingSources:
    """链上只读的那些外部事实：组织快照、在职状态、花名册、银河权限，以及同邮箱绑定回读口。

    同邮箱回读口**必填、没有哨兵值**——这道闸挡的是「两个人共用同一把问数令牌与同一行正式
    表权限」。本地覆盖读取口留空＝未接线，行为与接线之前逐字节一致；存量令牌源留空＝该能力
    关闭，令牌照常新签发，此时差集导入口也必须一起留空。
    """

    directory: DirectorySource
    employment: EmploymentSource
    roster: RosterSource
    galaxy: GalaxySource
    email_bindings: EmailBindingSource
    local_overrides: LocalOverrideSource | None = None
    stock_tokens: StockTokenSource | None = None
    legacy_importer: LegacyPermissionImporter | None = None

    def __post_init__(self) -> None:
        """结构性防漏接：装了存量令牌源就必须同时装差集导入口。

        只装源不装导入口会静默退回"只复制令牌不读权限"——存量用户首聊后权限被收窄到银河，
        没有任何症状。宁可构造期就拒绝。

        Raises:
            TypeError: 存量令牌源装了、差集导入口没装。
        """
        if self.stock_tokens is not None and self.legacy_importer is None:
            raise TypeError("注入存量令牌源时必须同时注入存量差集导入口 legacy_importer")


@dataclass(frozen=True)
class OnboardingActions:
    """链上会产生外部副作用的那些写侧端口。

    建档、用户状态机、用户环境、令牌签发、发布意图、就绪探针、私聊通知。预授权落库口留空时
    首聊路径一行都不碰它，但系统触发带了预授权却没装它时整链失败关闭。
    """

    provisioning: IdentityProvisioning
    users: UserStateStore
    environment: UserEnvironmentSource
    tokens: TokenIssuer
    decisions: PermissionDecisionStore
    readiness: ReadinessConfirmer
    notifier: UserNotifier
    position_grants: PositionGrantImporter | None = None


@dataclass(frozen=True)
class OnboardingRecords:
    """记账、审计与告警出口。

    Attributes:
        ledger: 派发账本。
        audit: 审计出口。
        failure_reasons: 失败原因落库口；留空＝查不到这条链的失败原因，不影响链本身。
        onboarding_failed: 管理员送达回调，签名是 ``(reason, trace_id)``——两者都是内部
            诊断标识，回调签名里没有传姓名或资料值的位置。留空时用户仍然收到冻结文案，
            只是没有任何东西送到管理群。
    """

    ledger: DispatchLedger
    audit: _AuditSink
    failure_reasons: FailureReasonRecorder | None = None
    onboarding_failed: Callable[[str, str], None] | None = None

    def __post_init__(self) -> None:
        """校验可选回调确实可调用。

        Raises:
            TypeError: 管理员送达回调既不是 ``None`` 也不可调用。
        """
        if self.onboarding_failed is not None and not callable(self.onboarding_failed):
            raise TypeError("onboarding_failed 回调必须可调用或为 None")


@dataclass(frozen=True)
class OnboardingPolicy:
    """判据、映射与预算。

    内测名单闸与发布闸门**都没有默认放行**：前者缺省会让它形同虚设，而名单外的真实用户会
    真实触达；后者决定要不要往正式权限表写一行，而外部表是不可回滚的。专用主体读取口每次
    判定**现读一次**而不是装配时读一次存着——换主体之后旧值会让新账号落回普通员工路径；
    也不该在进程启动那一刻读，那会让进程起不起得来取决于数据库此刻通不通。翻译映射留空与
    空映射按同一个结论处理，交给翻译层自己失败关闭；通知次数至少一次。
    """

    role_function_map: Mapping[str, str]
    innertest_roster_gate: Callable[[str], bool]
    delegated_subject: Callable[[], str | None]
    publish_allowed: Callable[[], bool]
    metric_translation_map: Mapping[str, Mapping[str, Sequence[str]]] | None = None
    publish_wait_seconds: float = 120.0
    notify_attempts: int = 3

    def __post_init__(self) -> None:
        """三道闸必须是真的可调用口，通知次数必须是正整数。

        Raises:
            TypeError: 任何一道闸不可调用。
            ValueError: 通知次数不是正整数。
        """
        if not callable(self.innertest_roster_gate):
            raise TypeError("必须注入内测名单闸：不能有默认放行")
        if not callable(self.delegated_subject):
            raise TypeError("专用主体登记必须是可调用的读取口")
        if not callable(self.publish_allowed):
            raise TypeError("必须注入发布闸门：翻译层不可用时一条发布意图都不能排")
        if isinstance(self.notify_attempts, bool) or not isinstance(self.notify_attempts, int):
            raise ValueError("通知至少要尝试一次")
        if self.notify_attempts < 1:
            raise ValueError("通知至少要尝试一次")


@dataclass(frozen=True)
class OnboardingRuntime:
    """线程与时钟。

    Attributes:
        submit: 本编排**专属**的执行器投递口，不与任何既有循环共用一条线程。缺它就只能在
            调用线程上跑完整条链，而那会把一轮定时 tick 占住十五分钟。
        sleep: 等待实现。缺省会把发布等待压成瞬时，而日志与记录看起来完全正常。
        clock: 注入时钟；留空取当前 UTC 时间。
        should_stop: 停机判定；留空表示永不停机。
    """

    submit: Callable[[Callable[[], None]], bool]
    sleep: Callable[[float], None]
    clock: Callable[[], datetime] | None = None
    should_stop: Callable[[], bool] | None = None

    def __post_init__(self) -> None:
        """执行器与等待实现都不能缺。

        Raises:
            TypeError: 执行器或等待实现不可调用。
        """
        if not callable(self.submit):
            raise TypeError("必须注入执行器：start 绝不能在调用线程上跑完整条链")
        if not callable(self.sleep):
            raise TypeError("sleep 必须可调用：缺省会把发布等待压成瞬时")


def group_runner_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """按字段名把一份扁平的构造参数装进上面五组参数对象；键对不上任何字段就响亮失败。"""
    remaining = dict(kwargs)
    grouped: dict[str, Any] = {}
    for name, cls in (
        ("sources", OnboardingSources),
        ("actions", OnboardingActions),
        ("records", OnboardingRecords),
        ("policy", OnboardingPolicy),
        ("runtime", OnboardingRuntime),
    ):
        fields = {field.name for field in dataclasses.fields(cls)}
        grouped[name] = cls(**{key: remaining.pop(key) for key in list(remaining) if key in fields})
    if remaining:
        raise TypeError(f"AutoOnboardingRunner 不认识的构造参数：{sorted(remaining)}")
    return grouped
