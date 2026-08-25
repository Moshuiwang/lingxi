"""正式首次开通编排：把身份、匹配、建档、用户环境、权限发布与就绪确认串成一条链。

[Issue #65](https://github.com/Moshuiwang/lingxi/issues/65) 的编排层，
[Epic D / #160](https://github.com/Moshuiwang/lingxi/issues/160) 的 S-D-02。它替换的是
此前 ``apps/gateway`` 里那个失败关闭桩（``_UnavailableOnboarding``）——在它存在之前，生产上
每一个未开通用户的首条消息都只得到冻结的 ``LX-ONBOARD-001``。

本模块住在 ``core/``，因此不 import 任何适配器、不发请求、不连数据库、不读时钟：组织快照
读取、在职状态实时回读、花名册、银河快照、建档、用户环境、令牌签发、权限决定、就绪探针、
用户通知与账本全部以 ``Protocol`` 注入，全部断言可以在没有网络也没有数据库的机器上跑完，
**也不需要真的等十五分钟**。

## 一次开通的固定次序

```
内测名单闸（Issue #302 S-N-01，见下方「三类拒绝」新增一行）
  → 身份定位（组织快照 + 在职实时回读，Epic B）
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
| **内测名单外**（Issue #302 S-N-01，在最前面拦截，早于以下三类） | `open_id` 不在内测名单（未配置/空白 → 空名单，对任何人全拒；非空且非法 → 进程启动失败，不是退化成空名单，见 `core/identity/innertest_roster_gate.py` 模块文档「默认关闭＝全拒」） | 冻结的「内测未开放」，不建档、不发布权限 |
| **确定性业务失败** | 定位不到、多条、双键冲突、资料不完整、非在职、无受支持职能、`incomplete_identity` | 冻结的「无可用银河权限」 |
| **专用主体** | `delegated_subject`（判定层或建档触发器） | 冻结的「专用账号不提供问数服务」 |
| **本侧故障** | 组织资料不可用、`storage_integrity`、用户环境写不出去、发布没能完成、探针技术失败 | 冻结的 `LX-ONBOARD-001` |

外加一条**等待类**终态：十五分钟预算耗尽仍未就绪 → 冻结的「权限同步未完成，已转交处理」。
四类互斥且**先到先得**：一旦选定终态，后置异常不得把它改写成另一种用户结论（`V-开通-13`）。

## 为什么 ``start`` 立刻返回 ``started``

本编排住在 ``lingxi-scheduler``（产品负责人 2026-08-18 裁定，见
[决策记录](../../../../docs/决策记录/2026-08-18-首次开通编排住在scheduler.md)）。它的调用方
是那个进程里的 ``OnboardingDispatchDuty``——按 ``claim_stale_onboarding`` 从
``inbound_event`` 认领 gateway 记下的未开通首聊事件。**gateway 只记事件、发第一条「已收到，
正在核对」提示，不再持有任何会产生外部副作用的编排。**

即便如此，``start`` 仍然**只做三件事**：按 ``open_id`` 去重 → 交给注入的执行器 → 返回
``OnboardingState.STARTED``。原因不变：真实编排单次耗时可达分钟级（产品合同允许权限同步等到
十五分钟），而 ``SchedulerLoop`` 的一轮 tick 被占住十五分钟，会让凭据轮换、保留清理、权限
发布消费全部停摆。执行器是本编排**专属**的线程池，不与任何既有循环共用一条线程——这是
Issue #65 钉在开工卡上的「共用线程复核」在搬迁之后的同一条结论。

终态由本模块自己**主动私聊**告诉用户。这同时定夺了 #65 留下的「对账恢复路径的用户通知
方案」：**编排自担通知 + 幂等**，而不是往 ``inbound_event`` 里多存两个路由标识——后者是一次
扩大数据范围的决定，而通知本来就只需要 ``open_id``，它已经在 ``PendingOnboarding`` 里了。

## 认领即记账，因此"没跑成"必须**放回去**

``claim_stale_onboarding`` 是「取出即记账」：认领的那一刻 ``onboarding_dispatched_at`` 就被
写上，而全仓没有任何东西会自动把它清回 ``NULL``。因此**凡是没有真正得出结论的路径都必须
显式释放认领**，否则那条事件此后永远不会再被捞起来，用户只剩一个「已收到」的表情：

| 情形 | 处理 |
|---|---|
| 执行器满位 / 已停机 / 同一个人已有链在跑 | ``start`` 返回 :data:`RETRYABLE_REASONS` 里的原因码，由调用方释放 |
| 停机信号落在链的中途 | 抛 :class:`_ChainAborted`，本模块释放，**不通知、不记账** |
| 跑出了结论但通知没送到 | 有限重试仍失败 → 释放**一次**，第二次记账收口并留 ``failed`` 审计 |
| 跑出了结论且通知送到 | 记账收口（重跑不会改变结论，只会持续冲击外部系统） |

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

## 一条链失败中断时，外部世界留下了什么

这条链会在**五个**地方留下 Lingxi 之外或事务之外的痕迹。哪一步失败都不回滚前面几步
（跨系统原子性不在任何一份合同里），因此逐条写清残留与归属，而不是假装它们不存在：

| 步骤 | 留下什么 | 谁清、怎么清 |
|---|---|---|
| 建档（`app_user`） | 一行 `provisioning_state` 停在中途的用户记录 | 不清。它是幂等重入的依据；重跑同一条链会原样复用，账号删除流程负责真正的删除 |
| 令牌签发（`mcp_access_token`） | 一行密文令牌（明文不落库） | 不清。签发幂等且**绝不覆盖**，重跑复用同一份；没有它后续步骤无法重入 |
| 用户环境（`.mcp.json`） | 见 `adapters/user_environment.py` 的残留表（含被强杀时可能留下的带令牌临时文件） | 同表 |
| 权限发布意图（`publish_outbox`） | 一条 `pending` 意图 + `app_user.permission_version` 已推进 | **不清，而且刻意不清**：那一版权限是数据库已确认的事实，发布消费职责照常把它写出去、就绪确认照常跑、`permission.range_updated` 照常通知。见下面那条已登记的缺口 |
| 认领账本（`inbound_event.onboarding_dispatched_at`） | 已认领的标记 | 由本模块的释放路径放回（见上表），或由结论收口 |

**此前已登记、现已由另一个职责补上的缺口**：发布意图排出去之后，如果就绪确认判了
`sync_timeout`，用户停在 `mcp_syncing`；本模块判超时时**当场返回**，此前没有任何东西会
再回来看这个人、也没有任何东西会把 `provisioning_state` 写成 `active`——本模块**曾经**是
`active` 唯一的写入方。合同对这一格的规定是「转交管理员处理，后续确认成功后再主动通知
用户可以开始使用」，那条恢复路径已由
:mod:`lingxi.apps.scheduler.late_readiness_recovery` 的迟到就绪恢复职责补上
（``V-开通-18``，见该模块自己的文档字符串）：它周期性回看停在 `mcp_syncing` 且已经判过
`timed_out` 的用户，复用本模块同一套判定层（新增的
:class:`~lingxi.core.permission.mcp_readiness.ReadinessRecoveryTicker`），就绪就在同一个
数据库事务里把 `provisioning_state` 推进到 `active` 并排一条待发的「开通完成」通知
（:meth:`~lingxi.adapters.postgres_late_readiness_recovery.PostgresLateReadinessStore.
activate_after_late_readiness`，条件更新只在 `provisioning_state = 'mcp_syncing'`
且账号仍启用且权限版本与探针绑定的那一版一致时才推进，同样只前进不回退）——因此
`active` 现在有**两个**写入方，各自的条件更新虽不是同一个方法，但守卫口径一致
（只前进、只在账号启用时推进），不构成竞态。**本模块自身不改判超时时的状态推进**：
它仍然在判超时时当场返回，`provisioning_state` 的推进与恢复不在这条链的调用栈里
发生——**独立审查 codex P1-3 新增的只是告警侧的一次调用**（:meth:`_notify_admin_
of_failure`），让「转交管理员处理」这句话对 `sync_timeout` 也有真实送达，不改变
上面这条恢复语义半分毫。**这条缺口同时登记在**
``docs/当前能力.md``（用户可见后果）与``docs/技术设计/验收矩阵.md`` 的 ``V-开通-18``
——只写在这里不算被守住，冻结验收读的是 ``docs/`` 正文；``docs/当前能力.md`` 的更新属
另一个 Story 的范围，本次改动只更新了验收矩阵。

**同一类缺口的另一半，现已由本模块自己补上**（Issue #282）：把用户推进到
``provisioning``（``_run`` 第 715 行前后的分水岭）之后，本链在 ``_publish``/``_confirm``
里遇到的**任何非 ``SYNC_TIMEOUT`` 失败终态**——本侧故障（发布未完成、发布失败/被
取代）、资料不全（缺邮箱导致发布行拼不出来）、``_confirm`` 里的账号停用复核、状态推进
被拒——都会让 ``provisioning_state`` 永久停在 ``provisioning``/``mcp_syncing``，而管线
的短路分支（``pipeline.py`` 对 ``PROVISIONING`` 状态照发「正在完成…请稍候」）会让用户
从此再也等不到任何结论。修法是 :meth:`AutoOnboardingRunner._abort_if_stalled`：跑到
失败终态且早前已经越过分水岭时，**当场**把 ``provisioning_state`` CAS 收口成
``aborted``（:meth:`~lingxi.adapters.postgres_identity.PostgresAppUserStore.
abort_stalled_provisioning`），不必等待任何扫描周期。``aborted`` 在
``adapters/postgres_identity.py`` 的推进表里与 ``guest`` 同 rank——用户的下一条消息会
被管线判成 ``NOT_PROVISIONED``，自然触发一条全新的开通链（新 ``event_id``、新
``trace_id``），**不是**把状态推回 ``guest``/``matching`` 再释放认领去自动重跑（那会
撞上「不得盲目重跑整个流程」的既有边界，也会引入双跑风险，见
:mod:`lingxi.apps.scheduler.stalled_provisioning` 模块文档的完整论证）。

**「当场收口」覆盖不了链本身死掉的那一半**（进程被强杀、执行线程异常退出到
``_execute`` 都够不到的地方）：这一半由 :class:`~lingxi.apps.scheduler.
stalled_provisioning.StalledProvisioningDuty` 按认领时间 + 四十五分钟租约周期性兜底，
两者共用同一个收口方法，只是触发判据不同（前者是"已经确定失败"，后者是"太久没有
任何进展"）。``SYNC_TIMEOUT`` 在两条路径里都被显式排除——它仍然只属于迟到就绪恢复
职责（``V-开通-18``）。

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
from lingxi.core.identity.stock_token_source import (
    ADOPTABLE,
    DECRYPT_FAILED,
    StockTokenLookup,
    StockTokenSource,
)
from lingxi.core.permission.account_match import MATCHED, match_galaxy_account
from lingxi.core.permission.mcp_readiness import ReadinessBinding, ReadinessOutcome
from lingxi.core.permission.notification import describe_scope
from lingxi.core.permission.publish_row import (
    STATUS_APPROVED,
    aggregate_permission,
    build_publish_row,
    parse_permissions,
    serialize_permissions,
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
#: 权限发布已排出、进入同步等待时的**进度**提示。合同「权限同步期间，卡片明确显示
#: 『权限正在同步，预计最多需要十五分钟』，用户无需重复开通」的落点。
KEY_SYNCING = "onboarding.syncing"
KEY_COMPLETED = "onboarding.completed"
KEY_NOT_AUTHORIZED = "onboarding.not_authorized"
KEY_SYNC_TIMEOUT = "onboarding.sync_timeout"
KEY_INTERNAL_ERROR = "onboarding.internal_error"
#: 开通中途停摆收口（``apps/scheduler/stalled_provisioning.py``，Issue #282）专用文案键
#: （Issue #280 裁定 B2-2）。此前该职责复用 ``KEY_INTERNAL_ERROR`` 逐字发送；产品负责人
#: 要求换成专门说明"等待已久、可再发一条消息重试"的措辞，不再套用一般性的内部故障话术。
KEY_STALLED = "onboarding.stalled"
KEY_DELEGATED_SUBJECT = "onboarding.delegated_subject"
KEY_SUSPENDED = "gateway.suspended"
#: 内测名单闸拒绝时的文案键（Issue #302 S-N-01）。名单外的 open_id 在身份定位、
#: 花名册与银河匹配、建档、用户环境与权限发布**发生之前**得到这句结论——不是
#: 「无可用银河权限」（那会误导用户去银河申请一个与本闸无关的权限），也不带
#: 追溯号（这是确定性业务结论，不是需要管理员介入的故障，见
#: ``lingxi.core.identity.innertest_roster_gate`` 模块文档）。
KEY_INNERTEST_NOT_OPEN = "onboarding.innertest_not_open"

#: 需要追溯号占位（``{reference}``）的文案键集合（Issue #280 §7.1）。占位名**不能**叫
#: ``trace_id``——那会命中 ``config/content.py`` 的内容安全正则，在目录加载期就让三个
#: 进程全部起不来（联合设计 §0.1）。集中在一处维护，供 :func:`_with_reference` 与
#: ``core/conversation/pipeline.py`` 各自的同名辅助函数共用同一份判据来源（后者是
#: 不同的渲染入口，各自维护一份字面量集合，靠这份常量的字面值对齐，不做跨模块 import
#: ——两条渲染路径此前就是各自独立的失败关闭桩，见模块文档「共用线程复核」）。
_KEYS_REQUIRING_REFERENCE: frozenset[str] = frozenset({KEY_INTERNAL_ERROR, KEY_SYNC_TIMEOUT})


def _with_reference(
    key: str, values: Mapping[str, object] | Sequence[tuple[str, object]], trace_id: str
) -> dict[str, object]:
    """给需要追溯号的终态文案补上 ``reference`` 占位值。

    只在键属于 :data:`_KEYS_REQUIRING_REFERENCE` 时补；已经带了值就不覆盖（防止
    调用方已经显式传过一次导致 ``ContentRenderError`` 的重复变量错误——虽然当前
    没有任何调用方会这样做，用 ``setdefault`` 而不是无条件覆盖仍然是更安全的形状）。
    """

    merged = dict(values)
    if key in _KEYS_REQUIRING_REFERENCE:
        merged.setdefault("reference", trace_id)
    return merged


class _ChainAborted(Exception):
    """停机信号落在链的中途：**不通知、不记账、把认领放回去**。

    不能当成一次失败终态告诉用户——那会在每次滚动部署时给正在开通的人推一条
    ``LX-ONBOARD-001``，而他其实什么问题都没有，下一轮就会被重新捞起来跑完。
    """


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


@dataclass(frozen=True)
class _Terminal:
    """一次开通的内部终态：状态 + 要发的那条文案 + 内部原因码。

    **每一个终态都要通知**，没有"这一路不说话"的分支：一条跑完却不说话的链，与一条被
    烧掉的链在用户那边完全一样。重复推送由绑定事件的去重键挡住，不靠这里少发一次。
    """

    state: OnboardingState
    key: str
    values: tuple[tuple[str, object], ...] = ()
    reason: str | None = None

    def as_result(self, *, trace_id: str | None = None) -> OnboardingResult:
        """转成 gateway 消费的受控结果。

        ``trace_id`` 只在调用方真的持有它时传（本类的两个调用点都在
        ``AutoOnboardingRunner.start`` 里，那里天然有它）；缺省 ``None`` 保持旧行为
        不变——不是所有 ``_Terminal`` 都对应一次已知的追溯号（例如尚未真正开始跑链
        就被拒绝的场景仍然有 ``trace_id``，这里留 ``None`` 只是防御性缺省，不代表
        生产中真的会用到它）。
        """

        values = self.values
        if trace_id is not None:
            values = tuple(_with_reference(self.key, values, trace_id).items())
        return OnboardingResult(
            state=self.state,
            messages=(OnboardingMessage(self.key, values),),
            failure_reason=self.reason,
        )


def _not_authorized(reason: str) -> _Terminal:
    return _Terminal(OnboardingState.NOT_AUTHORIZED, KEY_NOT_AUTHORIZED, reason=reason)


def _internal(reason: str) -> _Terminal:
    return _Terminal(OnboardingState.INTERNAL_ERROR, KEY_INTERNAL_ERROR, reason=reason)


class AutoOnboardingRunner:
    """正式的 ``OnboardingRunner``。装配见 ``apps/scheduler/__init__._build_onboarding_duty``。"""

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
        stock_tokens: StockTokenSource | None = None,
        decisions: PermissionDecisionStore,
        readiness: ReadinessConfirmer,
        notifier: UserNotifier,
        ledger: DispatchLedger,
        audit: _AuditSink,
        role_function_map: Mapping[str, str],
        innertest_roster_gate: Callable[[str], bool],
        delegated_subject: Callable[[], str | None],
        submit: Callable[[Callable[[], None]], bool],
        sleep: Callable[[float], None],
        clock: Callable[[], datetime] | None = None,
        should_stop: Callable[[], bool] | None = None,
        publish_wait_seconds: float = 120.0,
        notify_attempts: int = 3,
        publish_allowed: Callable[[], bool] | None = None,
        onboarding_failed: Callable[[str, str], None] | None = None,
    ) -> None:
        if not callable(innertest_roster_gate):
            # **没有默认放行。** 与 ``publish_allowed`` 同一条纪律：这一格决定的是
            # 「名单外的人要不要被挡在整条链最前面」，缺省放行会让内测名单闸形同虚设
            # ——而 Bot-Test 全员可见，名单外任何真实用户都会真实触达（见
            # ``lingxi.core.identity.innertest_roster_gate`` 模块文档「默认关闭＝全拒」）。
            raise TypeError("必须注入内测名单闸：不能有默认放行")
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
        #: 存量令牌只读源（Issue #281 改道）。``None``＝该能力关闭——坐标未配置时装配层
        #: 不会建出这个对象，`_issue_token` 因此原样走 `issue_token`，与改动前逐字节一致。
        self._stock_tokens = stock_tokens
        self._decisions = decisions
        self._readiness = readiness
        self._notifier = notifier
        self._ledger = ledger
        self._audit = audit
        self._role_function_map = role_function_map
        self._innertest_roster_gate = innertest_roster_gate
        self._delegated_subject = delegated_subject
        self._submit = submit
        self._sleep = sleep
        self._clock = clock or (lambda: datetime.now(_UTC))
        self._should_stop = should_stop or (lambda: False)
        self._publish_wait_seconds = float(publish_wait_seconds)
        if isinstance(notify_attempts, bool) or not isinstance(notify_attempts, int) or notify_attempts < 1:
            raise ValueError("通知至少要尝试一次")
        self._notify_attempts = notify_attempts
        if publish_allowed is None or not callable(publish_allowed):
            # **没有默认放行。** 这一格决定的是「要不要往正式权限表写一行」，而在
            # 「职能标签 → 指标名」翻译层（Issue #227）补齐之前，写出去的值列表根本
            # 不是消费方能用的指标名。缺省放行等于把一次配置缺失变成一次真实的错误
            # 发布，而外部表是不可回滚的。
            raise TypeError("必须注入发布闸门：翻译层不可用时一条发布意图都不能排")
        self._publish_allowed = publish_allowed
        if onboarding_failed is not None and not callable(onboarding_failed):
            raise TypeError("onboarding_failed 回调必须可调用或为 None")
        #: 管理员送达回调（Issue #280 §7.3；``SYNC_TIMEOUT`` 覆盖见独立审查
        #: codex P1-3）。**可选**：不注入时行为等价于此前——用户仍然收到冻结
        #: 文案，只是没有任何东西送到管理群。注入时在真正走到 ``INTERNAL_ERROR``
        #: 或 ``SYNC_TIMEOUT`` 终态时各调用一次，签名是 ``(reason, trace_id)``——
        #: 两者都是内部诊断标识，不是 open_id、姓名或任何资料值，回调签名里也
        #: 没有传这些的位置。
        self._onboarding_failed = onboarding_failed
        self._lock = threading.Lock()
        self._running: dict[str, str] = {}
        #: 已经因为「通知没送到」释放过一次认领的事件。**每条事件只放回一次**：释放让下一轮
        #: 把整条链重跑一遍，而链上有可能等满十五分钟的就绪确认；无上界地放回会让一次飞书
        #: 长时间不可用把执行器永久占满。第二次仍然送不到就记账收口，并留一条 ``failed``
        #: 后缀的响亮审计。
        self._released_for_notify: set[str] = set()

    # ------------------------------------------------------------------
    # 装配便捷方法
    # ------------------------------------------------------------------

    @staticmethod
    def build_innertest_roster_gate(roster: frozenset[str]) -> Callable[[str], bool]:
        """把已解析的内测名单集合包成 ``innertest_roster_gate`` 要的判定口。

        纯粹的装配便利：把 ``core/identity/innertest_roster_gate.py`` 的纯判定函数
        ``is_open_id_innertest_allowed`` 绑上一份具体的名单集合，返回本类构造时要的
        ``Callable[[str], bool]``。判据、为什么匹配键是 open_id、为什么空集合＝全拒，
        全部写在该模块自己的文档字符串里，本方法不重复。放在这里（而不是散落在
        ``apps/scheduler/assembly.py`` 里手写一个 lambda）只是为了让装配点的那一行
        足够短——``assembly.py`` 是本仓库棘轮登记的体量封顶文件（`scripts/ci/
        size_ratchet_baseline.txt`），新增依赖时优先把可复用的胶水代码放在有余量的
        地方，而不是继续往它里面堆代码。
        """

        from lingxi.core.identity.innertest_roster_gate import is_open_id_innertest_allowed

        return lambda open_id: is_open_id_innertest_allowed(open_id, roster)

    # ------------------------------------------------------------------
    # OnboardingRunner 合同
    # ------------------------------------------------------------------

    def start(
        self,
        *,
        event_id: str,
        open_id: str,
        trace_id: str,
        claim_token: Any = None,
    ) -> OnboardingResult:
        """认领并交给执行器。**永远不在调用线程上跑链**（见模块文档「共用线程复核」）。"""

        if self._should_stop():
            # 停机中不再开新链：一条刚起头就被进程退出打断的开通比压根没开始的更难
            # 收拾（外部副作用已经产生、账本却还没写）。
            self._audit.record(
                "onboarding.start_declined_while_stopping",
                event_id=event_id,
                trace_id=trace_id,
            )
            self._notify_admin_of_failure(
                reason="stopping", event_id=event_id, trace_id=trace_id
            )
            return _internal("stopping").as_result(trace_id=trace_id)

        with self._lock:
            running_for = self._running.get(open_id)
            if running_for is not None:
                # 同一个人已经有一条链在跑（认领撞上另一条同人事件）。第二次不再开链，
                # 但这条事件**自己从来没有被执行过**——它的认领必须被释放，不放回去
                # 就再也没人捞得到它（原因码在 ``RETRYABLE_REASONS`` 里）。
                self._audit.record(
                    "onboarding.already_running",
                    event_id=event_id,
                    running_event_id=running_for,
                    trace_id=trace_id,
                )
                return OnboardingResult(
                    state=OnboardingState.STARTED, failure_reason="already_running"
                )
            # **先登记再提交**：反过来会让两次几乎同时的 start 各自提交一条链。
            self._running[open_id] = event_id

        def task() -> None:
            try:
                self._execute(
                    event_id=event_id,
                    open_id=open_id,
                    trace_id=trace_id,
                    claim_token=claim_token,
                )
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
            self._notify_admin_of_failure(
                reason="executor_unavailable", event_id=event_id, trace_id=trace_id
            )
            return _internal("executor_unavailable").as_result(trace_id=trace_id)
        return OnboardingResult(state=OnboardingState.STARTED)

    def _release(self, open_id: str, event_id: str) -> None:
        with self._lock:
            if self._running.get(open_id) == event_id:
                del self._running[open_id]

    def _notify_admin_of_failure(
        self, *, reason: str, event_id: str, trace_id: str
    ) -> None:
        """管理员送达（Issue #280 §7.3）的唯一触发点：只在真正走到"承诺过转交
        管理员处理"的终态时调用一次，与用户通知彼此独立——告警回调失败不得
        带走这条链本该有的用户结论。

        独立审查（分支 fix/291-280-user-experience 收尾）：``start()`` 里两条
        **同步**返回 ``INTERNAL_ERROR`` 的分支（``stopping``——停机中拒绝开新链；
        ``executor_unavailable``——提交执行器失败）此前从不调用这个回调，因为它们
        根本不经过 ``_execute``（那里此前是唯一接了这个回调的地方）。用户看到的
        是冻结文案「已转交管理员处理」，管理群却真的什么都没收到——文案承诺与
        实际行为对不上。现在三处（这两条同步分支 + ``_execute`` 自己的
        ``INTERNAL_ERROR`` 分支）共用这一个触发点，不允许再出现第四条漏网路径。

        独立审查 codex P1-3：``_execute`` 的调用点现在同时覆盖
        ``OnboardingState.SYNC_TIMEOUT``——产品合同（``docs/产品合同与外部边界.md``
        「权限同步期间」一节）对十五分钟同步超时的措辞同样是"停止自动等待，
        **转交管理员处理**"，与 ``INTERNAL_ERROR`` 分支承诺的"已转交管理员处理"
        是同一句产品承诺，此前却只有后者真的送达管理群。``reason`` 沿用
        ``_Terminal.reason``（``"mcp_sync_timeout"``），与内部故障的原因码
        （如 ``"directory_unavailable"``）在归一化后的 ``scope`` 里天然可区分，
        管理员据此能分清"这是同步超时在等"还是"这是本侧真的坏了"——**不改变**
        :mod:`lingxi.apps.scheduler.late_readiness_recovery` 的自动恢复语义：
        这条告警只是"让管理群知道"，恢复仍然由该模块的迟到就绪恢复职责独立完成
        （``V-开通-18``），两者不是同一件事，也不互相依赖。
        """

        if self._onboarding_failed is None:
            return
        try:
            self._onboarding_failed(reason, trace_id)
        except Exception as error:  # noqa: BLE001 - 告警是锦上添花，不是链的一部分
            self._audit.record(
                "onboarding.alert_callback_failed",
                event_id=event_id,
                error=type(error).__name__,
                trace_id=trace_id,
            )

    # ------------------------------------------------------------------
    # 执行线程
    # ------------------------------------------------------------------

    def _execute(
        self, *, event_id: str, open_id: str, trace_id: str, claim_token: Any = None
    ) -> None:
        """跑完一条链、通知用户、记账。**异常不外抛**（它跑在执行线程上）。"""

        # **§7.4 编排层当场收口的挂钩**（Issue #282）：``_run`` 在把这个人推进到
        # ``provisioning``（第 715 行的分水岭）之后才会写 ``stalled["user_id"]``——
        # 见 :meth:`_run` 对应那一行的注释。用一个跨异常边界都能读到的可变容器，而不是
        # 给 ``_Terminal`` 加字段：`_run` 既可能正常返回 ``_Terminal``，也可能让
        # ``OnboardingChainError`` 或未预期异常穿透到下面的 ``except``，两条路径都要能
        # 拿到"这条链到底有没有一个可能被卡住的用户"这件事，而后者完全不经过
        # ``_Terminal``。
        stalled: dict[str, str | None] = {"user_id": None}
        try:
            terminal = self._run(
                event_id=event_id, open_id=open_id, trace_id=trace_id, stalled=stalled
            )
        except _ChainAborted:
            # 停机中止：放回认领，下一轮（或下次启动）从头重跑。整条链的每一步都幂等，
            # 重跑不会重复建档、重复发布或重复通知。
            self._audit.record(
                "onboarding.aborted_while_stopping", event_id=event_id, trace_id=trace_id
            )
            self._release_claim(
                event_id=event_id, trace_id=trace_id, claim_token=claim_token
            )
            return
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
            content_key=terminal.key,
            trace_id=trace_id,
        )
        if terminal.state in (OnboardingState.INTERNAL_ERROR, OnboardingState.SYNC_TIMEOUT):
            # SYNC_TIMEOUT 与 INTERNAL_ERROR 是产品合同里两句独立措辞（`docs/产品
            # 合同与外部边界.md`），但都承诺"转交管理员处理"——两者都必须真的送达
            # 管理群（独立审查 codex P1-3）。reason 沿用各自终态的 terminal.reason，
            # 不折叠成同一个值，管理员据此分得清是哪一类。
            self._notify_admin_of_failure(
                reason=terminal.reason or "unknown", event_id=event_id, trace_id=trace_id
            )
        delivered = self._notify(
            open_id=open_id,
            event_id=event_id,
            key=terminal.key,
            values=_with_reference(terminal.key, terminal.values, trace_id),
            suffix="",
            trace_id=trace_id,
        )
        if delivered:
            # **只有真的送达才当场收口**（外部独立审查 P2-1 修复）：此前的判据是
            # ``delivered or not self._release_for_notify(...)``，其中第二个析取项
            # 覆盖"两轮通知全部失败、放弃"的分支——那种情况下用户**一条终态都没
            # 收到**，却已经把状态收口成 ``aborted``。``aborted`` 不在
            # ``StalledProvisioningDuty`` 的候选判据（``provisioning``/
            # ``mcp_syncing``）里，于是这个人从此**结构上不可能再被 45 分钟兜底
            # 捞到**——唯一还欠他一个结论的通道被自己关掉了。收窄到只在
            # ``delivered`` 为真时收口之后，通知彻底失败的这条链原样留在中途格，
            # 45 分钟后 ``StalledProvisioningDuty`` 会用**独立**的通知出口重新尝试
            # 告诉他，而不是被这里提前判死。
            self._abort_if_stalled(stalled["user_id"], terminal, trace_id=trace_id)
        if delivered or not self._release_for_notify(
            event_id=event_id, trace_id=trace_id, claim_token=claim_token
        ):
            # 送到了，或者已经放回过一次仍然送不到：记账收口。第二种情况留了一条
            # ``failed`` 后缀的审计（见 ``_release_for_notify``），不会无声消失——
            # 但**不再**把它当成"当场收口"的触发条件（见上）。
            try:
                self._ledger.mark_onboarding_dispatched(event_id=event_id)
            except Exception as error:  # noqa: BLE001 - 记不上账最坏只是被下一轮再捞一次
                self._audit.record(
                    "onboarding.dispatch_record_failed",
                    event_id=event_id,
                    error=type(error).__name__,
                    trace_id=trace_id,
                )

    def _notify(
        self,
        *,
        open_id: str,
        event_id: str,
        key: str,
        values: Mapping[str, object],
        suffix: str,
        trace_id: str,
    ) -> bool:
        """主动私聊一条消息，**返回是否送达**。失败只留响亮审计，不改写终态。

        有限重试而不是一次定生死：一次飞书抖动就让用户永远停在「已收到」，代价与收益完全
        不成比例。重试之间用注入的 ``sleep``，因此纯单测里一秒都不用等。

        ``dedupe_key`` 绑定**事件 + 用途**：同一条首聊事件的重复执行（重新认领、重启后
        重跑）只会让用户看到同一条结论一次；而进度提示与终态是两个用途，各自一个键，
        不会互相去重掉。
        """

        dedupe_key = f"onboarding:{suffix}{event_id}" if suffix else f"onboarding:{event_id}"
        for attempt in range(1, self._notify_attempts + 1):
            try:
                self._notifier.send(
                    open_id=open_id, key=key, values=dict(values), dedupe_key=dedupe_key
                )
                return True
            except Exception as error:  # noqa: BLE001
                self._audit.record(
                    "onboarding.notify_failed",
                    event_id=event_id,
                    content_key=key,
                    attempt=attempt,
                    error=type(error).__name__,
                    trace_id=trace_id,
                )
                if attempt < self._notify_attempts:
                    self._sleep(float(attempt))
        return False

    def _release_claim(self, *, event_id: str, trace_id: str, claim_token: Any = None) -> None:
        """把认领放回 ``NULL``。停机中止专用，不设次数上限——它每个进程生命周期最多
        发生一次，而且不放回去这条事件就永远没人再看。"""

        try:
            self._ledger.release_onboarding_claim(
                event_id=event_id, claim_token=claim_token
            )
        except Exception as error:  # noqa: BLE001
            self._audit.record(
                "onboarding.release_claim_failed",
                event_id=event_id,
                error=type(error).__name__,
                trace_id=trace_id,
            )

    def _release_for_notify(
        self, *, event_id: str, trace_id: str, claim_token: Any = None
    ) -> bool:
        """通知没送到：把认领放回去，让下一轮重跑整条链。**每条事件只放回一次。**

        返回是否真的放回了。``False`` 表示「这条已经放回过一次、仍然送不到」，调用方据此
        记账收口——本方法在那种情况下留一条 ``failed`` 后缀的审计（审计实现按后缀升到
        ``WARNING``），因此放弃这件事本身不会无声消失。
        """

        with self._lock:
            already = event_id in self._released_for_notify
            if not already:
                self._released_for_notify.add(event_id)
        if already:
            self._audit.record(
                "onboarding.notify_gave_up_failed", event_id=event_id, trace_id=trace_id
            )
            return False
        try:
            self._ledger.release_onboarding_claim(
                event_id=event_id, claim_token=claim_token
            )
        except Exception as error:  # noqa: BLE001 - 放不回去只能记账收口
            self._audit.record(
                "onboarding.release_claim_failed",
                event_id=event_id,
                error=type(error).__name__,
                trace_id=trace_id,
            )
            return False
        self._audit.record(
            "onboarding.claim_released_after_notify_failed",
            event_id=event_id,
            trace_id=trace_id,
        )
        return True

    def _abort_if_stalled(
        self, user_id: str | None, terminal: _Terminal, *, trace_id: str
    ) -> None:
        """本链已经确定失败终态、且早前已经把这个人推进到 ``provisioning``：**当场**
        收口成 ``aborted``，不必等停摆扫描职责的四十五分钟租约（设计「编排层当场收口」，
        Issue #282 §0.2/§2.4 的对称修复）。

        **跳过 ``SYNC_TIMEOUT``**：那一路仍然可能就绪，归属迟到就绪恢复职责
        （``V-开通-18``）继续按自己的节奏等待，本链**绝不能**抢它的活——``provisioning_
        state`` 必须原样留在 ``mcp_syncing``。**跳过 ``COMPLETED``**：那一路已经推进
        到 ``active``，条件更新天然不会命中，跳过只是省一次没有意义的空写。

        ``user_id`` 为 ``None`` 表示这条链在把用户推进到 ``provisioning`` 之前就已经
        失败（身份定位、匹配、建档三步的失败终态）——那些失败已经自然停在
        ``matching``/``guest``，不在 ``_PROVISIONING_IN_FLIGHT`` 里，不需要本方法处理
        （见模块文档「两个洞的共同形状」：卡住的判据不是失败，是失败发生在把用户推进
        到 ``provisioning`` 之后）。

        条件更新本身是幂等且安全的：这个人此刻若已经不在 ``provisioning``/
        ``mcp_syncing``（被停摆扫描先一步收口、被另一条并发链推进到 ``active``、或账号
        已经被停用），这里就是一次 0 行的空写，不会覆盖任何人的真实状态
        （`adapters.postgres_identity.PostgresAppUserStore.abort_stalled_provisioning`
        的 CAS 守卫）。
        """

        if user_id is None or terminal.state in (
            OnboardingState.SYNC_TIMEOUT,
            OnboardingState.COMPLETED,
        ):
            return
        try:
            self._users.abort_stalled_provisioning(
                user_id=user_id,
                expected_states=(STATE_PROVISIONING, STATE_MCP_SYNCING),
                reason=terminal.reason or terminal.key,
            )
        except Exception as error:  # noqa: BLE001 - 收口失败不改写已经决定的终态
            self._audit.record(
                "onboarding.stalled_abort_failed",
                user=user_id,
                error=type(error).__name__,
                trace_id=trace_id,
            )

    # ------------------------------------------------------------------
    # 链
    # ------------------------------------------------------------------

    def _stop_guard(self) -> None:
        """每一步之间问一次停机。**在发起下一个带外部副作用的动作之前**问。"""

        if self._should_stop():
            raise _ChainAborted()

    def _run(
        self,
        *,
        event_id: str,
        open_id: str,
        trace_id: str,
        stalled: dict[str, str | None],
    ) -> _Terminal:
        """一次开通的固定次序。每一步的失败去向都在这里显式返回。

        ``stalled`` 是调用方（``_execute``）传入的可变容器：一旦这个人被推进到
        ``provisioning``（下面那行 ``advance_provisioning_state(to=STATE_PROVISIONING)``
        之后），就把 ``user_id`` 写进去——从这一刻起，本链任何一种失败终态都可能让他
        停在中途格，调用方需要这个信号才能做「当场收口」（见 :meth:`_abort_if_stalled`）。
        """

        self._stop_guard()
        if not self._innertest_roster_gate(open_id):
            # **内测名单闸（Issue #302 S-N-01），挡在整条链最前面。** Bot-Test
            # 全员可见（G-发现性核查，2026-08-24 编排者 API 回读），任何名单外员工
            # 都能真实私聊触达；因此这里必须早于组织快照读取、在职状态实时回读
            # （会消耗全系统独占的专用授权派生令牌）与任何数据库写入——名单外用户
            # 不建档、不发布权限、零业务状态残留，只留一条审计。
            #
            # **审计只带 event_id/trace_id，不带 open_id（含脱敏形式）**：与本文件
            # 其余每一条 `self._audit.record(...)` 同一条纪律——`redact_identifier()`
            # 的返回值按其自身文档字符串**只能进日志**，不可反查也不可比较，放进
            # 结构化审计字段会让人误以为它能用于关联或去重（`V-花名册-34`，
            # `tests/test_roster_audit_duty.py::RedactedIdentifierUsageTest` 拦着）。
            # 需要还原这个人是谁时，凭 `event_id` 回读 `inbound_event.user_open_id`
            # 即可，不需要在这里重复一份。
            self._audit.record(
                "onboarding.innertest_roster_rejected",
                event_id=event_id,
                trace_id=trace_id,
            )
            return _Terminal(
                OnboardingState.NOT_AUTHORIZED,
                KEY_INNERTEST_NOT_OPEN,
                reason="innertest_roster_rejected",
            )

        located = self._locate(open_id)
        if isinstance(located, _Terminal):
            return located
        member = located

        self._stop_guard()
        matched = self._match(member, trace_id=trace_id)
        if isinstance(matched, _Terminal):
            return matched
        request, aggregate = matched

        self._stop_guard()
        provisioned = self._provision(request)
        if isinstance(provisioned, _Terminal):
            return provisioned
        user_id = provisioned

        recheck = self._recheck_still_provisionable(
            user_id, aggregate=aggregate, trace_id=trace_id
        )
        if recheck is not None:
            return recheck

        self._stop_guard()
        issued = self._issue_token(user_id, email=request.email)
        self._create_environment(user_id, issued)
        self._users.advance_provisioning_state(user_id, to=STATE_PROVISIONING)
        # **分水岭**（Issue #282 §0.1）：从这一行起，任何失败终态都会让这个人停在
        # ``provisioning``/``mcp_syncing``，不再自然回到 ``matching``。把 ``user_id``
        # 交给调用方的可变容器，供 ``_abort_if_stalled`` 判断「要不要当场收口」——写在
        # 这里而不是更早，是因为更早的失败（身份定位、匹配、建档）本来就停在
        # ``matching``/``guest``，不在 ``_PROVISIONING_IN_FLIGHT`` 里，不需要收口。
        stalled["user_id"] = user_id

        self._stop_guard()
        published = self._publish(user_id, request, aggregate, issued)
        if isinstance(published, _Terminal):
            return published
        permission_version, permissions = published
        self._users.advance_provisioning_state(user_id, to=STATE_MCP_SYNCING)

        # **合同要求的第二条固定提示**（`V-开通-11`）：权限已经排出去、进入同步等待时，
        # 用户必须被告知"正在同步、最多十五分钟、无需重复开通"，而不是一直停在第一条
        # "正在核对"。它在**阻塞式就绪确认之前**发——那一步最长会等十五分钟，等完再说
        # 就等于没说。用独立的去重键，不与终态互相去重掉。
        self._notify(
            open_id=open_id,
            event_id=event_id,
            key=KEY_SYNCING,
            values={},
            suffix="syncing:",
            trace_id=trace_id,
        )

        self._stop_guard()
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

        if not self._publish_allowed():
            # **翻译层不可用：一条发布意图都不排**（与 Issue #227 在每日重算那一侧的
            # 整轮判据同一条纪律，授权与撤权都不例外）。停在这里而不是继续：
            #
            # - 不是"没有银河权限"——这个人明明有，说成没有会把他引去银河申请一个他
            #   已经有的权限；
            # - 不是"MCP 同步超时"——那条说的是外部同步慢，而这里是我们自己缺一份内容；
            # - 也不能"先建档建环境、发布那步以后再补"：合同要求成功以发布 + 就绪确认
            #   为前提，半开的用户会一直停在 mcp_syncing 而没有任何人会来收拾。
            #
            # 因此按本侧故障收口（`LX-ONBOARD-001`，已转交管理员），且**在建档之前**，
            # 不留下任何半成品。
            self._audit.record("onboarding.publish_gate_closed", trace_id=trace_id)
            return _internal("permission_translation_unavailable")

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

    def _recheck_still_provisionable(
        self, user_id: str, *, aggregate: Any, trace_id: str
    ) -> _Terminal | None:
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
            # 已经开通完了（重新认领的窗口里被另一条链跑完，或上一轮跑完但通知没送到）。
            # **不重复建环境、不重复发布**（`V-开通-14`）——但**照常通知**：这条路径正是
            # "上一次结论没送到、被重新认领"的收敛出口，不通知就等于把它烧掉。重复推送由
            # 绑定事件的去重键挡住（同一条事件的两次执行用同一个键）。
            #
            # 范围**不重新聚合一次外部权限**，用本轮已经算出来的那一份：它与即将发布
            # （或已经发布）的那一版是同一个来源，不会凭空编出一个用户没有的范围。
            self._audit.record("onboarding.already_active", user=user_id, trace_id=trace_id)
            return self._completed(serialize_permissions(aggregate))
        return None

    # ---- 6. 令牌 + 用户环境 ---------------------------------------------

    def _issue_token(self, user_id: str, *, email: str) -> Any:
        """签发或采纳该用户的问数 MCP 访问令牌（adopt-or-issue，#281 改道裁定）。

        ``self._stock_tokens`` 为 ``None``（坐标未配置）时与改动前逐字节一致——直接走
        原签发路径，这是"未接入存量令牌能力＝零行为变化"的哨兵。注入时先按邮箱查存量源：
        无行 / 有行无密文都退回原路径签新；有行含密文则采纳；**解密失败响亮失败、绝不
        退回签新**（签新会让用户环境令牌与正式表错位，造成真实 MCP 认证失败）。
        """

        if self._stock_tokens is not None:
            lookup = self._stock_token_lookup(email)
            if lookup.state == ADOPTABLE:
                return self._adopt_token(user_id, lookup)
            if lookup.state == DECRYPT_FAILED:
                self._audit.record("onboarding.stock_token_decrypt_failed", user=user_id)
                raise OnboardingChainError("stock_token_decrypt_failed")
            self._audit.record(
                "onboarding.stock_token_absent", user=user_id, state=lookup.state
            )
        try:
            return self._tokens.issue_token(user_id)
        except Exception as error:  # noqa: BLE001
            raise OnboardingChainError(f"token_issue_failed_{type(error).__name__}") from error

    def _stock_token_lookup(self, email: str) -> StockTokenLookup:
        try:
            return self._stock_tokens.lookup(email)
        except Exception as error:  # noqa: BLE001
            raise OnboardingChainError(
                f"stock_token_lookup_failed_{type(error).__name__}"
            ) from error

    def _adopt_token(self, user_id: str, lookup: StockTokenLookup) -> Any:
        try:
            adopted = self._tokens.adopt_token(user_id, lookup.secret)
        except Exception as error:  # noqa: BLE001
            raise OnboardingChainError(f"token_adopt_failed_{type(error).__name__}") from error
        # 权限面由银河同步权威决定，不由本步裁量——这里只审计标注，不改变采纳与否
        # （#281 改道裁定第四条）。
        approved = not lookup.status or lookup.status == STATUS_APPROVED
        self._audit.record(
            "onboarding.stock_token_adopted" if adopted.created else "onboarding.stock_token_existing_kept",
            user=user_id,
            status_approved=approved,
        )
        return adopted

    def _create_environment(self, user_id: str, issued: Any) -> None:
        """创建用户环境并把该用户的 MCP Bearer 落进它的 ``.mcp.json``。

        **明文只在这一次调用里存在**：``reveal()`` 的返回值不进日志、不进审计、不进异常
        （``adapters/mcp_token_cipher.py`` 的同一条纪律）。
        """

        try:
            self._environment.ensure(user_id=user_id, mcp_token=issued.reveal())
        except Exception as error:  # noqa: BLE001
            # **透传实现给的错误码**（它已经脱敏，只有 errno 符号名）：`ENOENT`（卷没挂）
            # 与 `EACCES`（权限不对）是两种完全不同的运维动作，把它们一起压成
            # `UserEnvironmentError` 等于让排查从头再来一遍。
            detail = getattr(error, "code", None) or type(error).__name__
            raise OnboardingChainError(f"user_environment_failed_{detail}") from error

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
        # ``UNCHANGED`` 也要等：那一条意图可能还停在 ``pending``（重入、或每日重算刚排出
        # 来还没被消费）。跳过等待会让"发布面根本没跑"表现成十五分钟的 MCP 同步超时——
        # 一个本侧故障被说成了外部同步慢，运维会去查错的地方。``_await_published`` 对
        # 已经 ``published`` 的意图第一次回读就返回，因此这里不引入任何多余等待。
        waited = self._await_published(decision.outbox_id)
        if waited is not None:
            return waited
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
            if intent is None:
                # 意图查不到：**本轮根本没有排出这一条**（例如翻译层整轮判据把它挡住），
                # 与"排了但发布失败"是两件事。两者的用户出口相同（本侧故障），但原因码
                # 必须可分辨——前者要去看内容配置，后者要去看外部表格调用。
                return _internal("publish_intent_missing")
            if status in ("failed", "superseded"):
                # ``superseded``：本链排的这一版已经被更新的一版取代（撤权或重算）。
                # 那一版自己会被发布并确认，但**本次开通**不能据此宣告成功。
                return _internal(f"publish_{status}")
            if self._should_stop():
                # 停机不是"发布没完成"：那一版意图仍然有效，下一轮重跑会等到它。
                raise _ChainAborted()
            if waited >= self._publish_wait_seconds:
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
            #
            # **再复核一次**：从建档后那次复核到这里最长隔了十七分钟（发布等待 +
            # 就绪预算），管理员在这段时间里停用账号是真实形状。只复核一次等于用十七
            # 分钟前的事实宣告"开通完成"，还会把一个已经被停用的人写成 ``active``。
            status = self._users.read_status(user_id)
            if status is None or status.account_state != "enabled":
                self._audit.record(
                    "onboarding.halted_account_state",
                    user=user_id,
                    account_state=status.account_state if status else "missing",
                    trace_id=trace_id,
                )
                return _Terminal(
                    OnboardingState.NOT_AUTHORIZED, KEY_SUSPENDED, reason="account_not_enabled"
                )
            # **推进结果不能忽略**：条件更新影响 0 行意味着这个人当前状态不允许被推到
            # ``active``（被停用、或已经被另一条路径改写）。忽略返回值就会在库里还是
            # ``mcp_syncing`` 的情况下告诉用户"开通完成"，而他下一条消息仍然会被拒。
            if not self._users.advance_provisioning_state(user_id, to=STATE_ACTIVE):
                self._audit.record(
                    "onboarding.state_advance_refused_failed",
                    user=user_id,
                    provisioning_state=status.provisioning_state,
                    trace_id=trace_id,
                )
                return _internal("state_advance_refused")
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
