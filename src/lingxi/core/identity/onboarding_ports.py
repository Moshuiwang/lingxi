"""首次开通编排（``onboarding_runner.py``）的注入口：14 个 ``Protocol`` + ``EnvironmentResult``。

从 ``core/identity/onboarding_runner.py`` 纯移动拆出（Trace #358 S-H-1，Issue #350 Gate
G-3 裁定 Option A）：只搬定义，不改任何签名或文档字符串；``AutoOnboardingRunner`` 通过
``from .onboarding_ports import (...)`` 取回这些名字，因此本模块的公开名字都会作为
``onboarding_runner`` 模块的属性再次可见（含 ``adapters/user_environment.py`` 对
``EnvironmentResult`` 的外部导入）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol, Sequence

from lingxi.core.identity.first_contact import EmploymentStatus
from lingxi.core.identity.provisioning import UserProvisioningStatus
from lingxi.core.permission.local_override import LocalPermissionOverrideEntry
from lingxi.core.permission.mcp_readiness import ReadinessBinding

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

    def abort_stalled_provisioning(
        self, *, user_id: str, expected_states: Sequence[str], reason: str
    ) -> bool:
        """把一条中途停摆的开通收口成 ``aborted``（Issue #282）。条件更新，影响 0 行
        就是没收口——见 ``adapters/postgres_identity.PostgresAppUserStore.
        abort_stalled_provisioning`` 的完整合同。本类只在「跑到失败终态、但早前已经
        把这个人推进到 provisioning」时调用它（见 :meth:`AutoOnboardingRunner.
        _abort_if_stalled`），停摆扫描职责（``apps/scheduler/stalled_provisioning.py``）
        调用的是同一个方法。"""


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

    def adopt_token(self, user_id: str, secret: str) -> Any:
        """采纳一份**已经解密**的存量令牌明文（Issue #281 改道）。语义同 ``issue_token``
        ——已存在即返回既有那一份、绝不覆盖；候选明文的来源不是新生成，而是调用方从
        存量源解出来的那一份。"""
        ...


class PermissionDecisionStore(Protocol):
    """权限决定 + 发布意图（同事务），以及意图状态的回读。"""

    def record_decision(
        self, *, user_id: str, row: Any, reason: str, decided_at: datetime
    ) -> Any: ...

    def load(self, outbox_id: str) -> Any: ...


class LocalOverrideSource(Protocol):
    """本地权限覆盖的按用户读取口（S-P-3 #319）。与 permission_refresh.py 的同名协议各自独立一份：``core/`` 不 import ``apps/``（代码框架第二节）。"""

    def effective_entries(self, *, user_id: str) -> Sequence[LocalPermissionOverrideEntry]: ...


class PublishHistorySource(Protocol):
    """「这个人在发布链上有没有留下过足迹」的只读口（红线-1，legacy 有界化，
    Trace #328 opus 审查；对称于 ``apps/scheduler/permission_refresh._PublishHistory``，
    各自一份因为 ``core/`` 不 import ``apps/``）。"""

    def has_publish_footprint(self, user_id: str) -> bool: ...


class ReadinessConfirmer(Protocol):
    """阻塞式 MCP 就绪确认（``core/permission/mcp_readiness.McpReadinessConfirmation``）。"""

    def confirm(self, binding: ReadinessBinding, *, permissions: str) -> Any: ...


class UserNotifier(Protocol):
    """终态的主动私聊。同一逻辑通知的首发与重试必须传同一个 ``dedupe_key``。"""

    def send(
        self, *, open_id: str, key: str, values: Mapping[str, object], dedupe_key: str
    ) -> None: ...


class DispatchLedger(Protocol):
    """开通交接账本（``inbound_event.onboarding_dispatched_at``）。

    认领这条事件时账本就已经被写上（``claim_stale_onboarding`` 是「取出即记账」），因此本
    协议真正要紧的是**反向**那一个：跑完之后如果用户其实没被通知到，必须把账**放回去**，
    否则这条事件永远不会再被任何人捞起来，用户只剩一个「已收到」的表情。
    """

    def mark_onboarding_dispatched(self, *, event_id: str) -> None: ...

    def release_onboarding_claim(self, *, event_id: str, claim_token: Any = None) -> None:
        """把**自己那一次**认领放回 ``NULL``，让下一轮重新认领。

        ``claim_token`` 是认领代次，实现按它做 CAS——少了它会出现 ABA：本链释放之后另一条
        链重新认领，本链的重试再释放一次就把**别人的**认领清掉了。
        """


class _AuditSink(Protocol):
    def record(self, action: str, /, **fields: object) -> None: ...


class FailureReasonRecorder(Protocol):
    """把一次开通终态的失败原因落库（``onboarding_failure`` 表，迁移 ``0077``，
    [Issue #337](https://github.com/Moshuiwang/lingxi/issues/337)）：供
    ``/admin trace <追溯号>`` 只读命令消费，回答此前只能靠检索 scheduler 容器日志
    才能拿到的 ``failure_reason``。真实实现见
    ``adapters/postgres_onboarding_failure.PostgresFailureReasonRecorder``。

    **可选**：``None`` 表示未装配，行为与接线之前逐字节一致——只是这一条终态查不到
    失败原因（``/admin trace`` 会如实回「无失败记录」，不是崩溃或误报成功）。

    调用方（:class:`~lingxi.core.identity.onboarding_runner.AutoOnboardingRunner`、
    :class:`~lingxi.apps.scheduler.stalled_provisioning.StalledProvisioningDuty`）
    只在真正是失败终态时调用（``failure_reason`` 非空字符串），紧邻各自既有的
    ``self._audit.record(...)`` 调用之后——两处都是「审计侧最佳努力」：落库失败
    不得带走已经决定的用户终态或已完成的收口，调用方自己 try/except 并改记一条
    独立的失败审计，本协议本身不承诺任何重试或补偿。

    ``event_type`` 固定是 ``'onboarding.result'`` 或 ``'stalled_provisioning.
    aborted'``（迁移 ``0077`` 的 ``CHECK`` 约束同一取值范围），标记这一行来自哪一个
    写出点。``trace_id`` 是主键：同一条链正常只产生一次终态，真实实现按
    ``ON CONFLICT (trace_id) DO NOTHING`` 幂等——不覆盖先落的那一行，不报错。
    """

    def record_failure(self, *, trace_id: str, failure_reason: str, event_type: str) -> None: ...
