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
  → 同邮箱是否已绑给另一个人（rc25 S-2a，core/identity/onboarding_guards.py）
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
| **本侧故障** | 组织资料不可用、`storage_integrity`、`email_already_bound`（同一邮箱已绑给另一个人）、用户环境写不出去、发布没能完成、探针技术失败 | 冻结的 `LX-ONBOARD-001` |

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
``docs/当前能力.md``（用户可见后果）与``docs/技术设计/验收矩阵-开通与身份.md`` 的 ``V-开通-18``
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
import traceback
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from lingxi.core.conversation.ports import (
    OnboardingResult,
    OnboardingState,
)
from lingxi.core.identity.first_contact import (
    EmploymentStatus,
    FirstContactOutcome,
    decide_first_contact,
    locate_by_open_id,
)
from lingxi.core.identity.onboarding_ports import (
    DirectorySource,
    DispatchLedger,
    # 「同邮箱是否已绑给另一个人」的回读口（rc25 S-2a，对抗审查 X-1）。
    EmailBindingSource,
    EmploymentSource,
    # 本文件不直接使用 ``EnvironmentResult``（是 ``_environment.ensure()`` 的返回
    # 类型，方法体不需要按名字标注它），导入只是为了让它继续作为 ``onboarding_
    # runner`` 模块的属性可见——``adapters/user_environment.py:102`` 与
    # ``tests/test_onboarding_runner.py`` 都从这里导入。
    EnvironmentResult,
    # 失败原因落库口（Issue #337，可选，见该协议文档）。
    FailureReasonRecorder,
    GalaxySource,
    # 存量差集导入口（rc25 S-1，Issue #540）。
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
from lingxi.core.identity.legacy_permission_import import (
    import_legacy_permissions,
    translate_galaxy,
)
from lingxi.core.identity.onboarding_guards import (
    reject_email_bound_to_another_person,
    reject_zero_galaxy_without_local_grant,
)
from lingxi.core.identity.onboarding_support import draft_from_member, roster_row_for
from lingxi.core.identity.preprovision import NULL_DISPATCH_LEDGER, ORIGIN_PREPROVISION, PositionGrantImporter, PreprovisionGrant, deliver_silently, import_preprovision_grant, is_system_trigger, origin_of, run_system_onboarding
from lingxi.core.identity.onboarding_terminal import (
    KEY_COMPLETED,
    KEY_DELEGATED_SUBJECT,
    KEY_INNERTEST_NOT_OPEN,
    # ``KEY_INTERNAL_ERROR``/``KEY_NOT_AUTHORIZED``/``KEY_STALLED`` 三个本文件不
    # 直接使用（``_internal``/``_not_authorized`` 两个工厂已经在 onboarding_
    # terminal.py 内部自包含），导入只是为了让它们继续作为 ``onboarding_runner``
    # 模块的属性可见——``tests/test_onboarding_runner.py``（前两个）与
    # ``apps/scheduler/stalled_provisioning.py``/``tests/test_stalled_
    # provisioning.py``（``KEY_STALLED``）都从这里导入。
    KEY_INTERNAL_ERROR,
    KEY_NOT_AUTHORIZED,
    KEY_STALLED,
    KEY_SUSPENDED,
    KEY_SYNCING,
    KEY_SYNC_TIMEOUT,
    STATE_ACTIVE,
    STATE_MCP_SYNCING,
    STATE_PROVISIONING,
    OnboardingChainError,
    _ChainAborted,
    _Terminal,
    _internal,
    _not_authorized,
    _with_reference,
    # ``_KEYS_REQUIRING_REFERENCE`` 本身在本文件不直接使用（``_with_reference`` 已经
    # 在 onboarding_terminal.py 内部自包含），这里导入只是为了让它继续作为
    # ``onboarding_runner`` 模块的属性可见——``tests/test_content_catalog.py`` 按
    # ``onboarding_runner_module._KEYS_REQUIRING_REFERENCE`` 取用。
    _KEYS_REQUIRING_REFERENCE,
)
from lingxi.core.identity.org_snapshot import DirectoryAvailability, SnapshotMember
from lingxi.core.identity.provisioning import (
    IdentityProvisioning,
    ProvisioningRejection,
    ProvisioningRequest,
)
from lingxi.core.identity.stock_token_source import (
    ADOPTABLE,
    DECRYPT_FAILED,
    StockTokenLookup,
    StockTokenSource,
)
from lingxi.core.permission.account_match import MATCHED, match_galaxy_account
from lingxi.core.permission.local_override import ResolvedLocalOverrides, resolve_local_overrides
from lingxi.core.permission.mcp_readiness import ReadinessBinding, ReadinessOutcome
from lingxi.core.permission.merge_sources import REASON_LOCAL_OVERRIDE_READ_FAILED, merge_permission_sources
from lingxi.core.permission.notification import describe_scope
from lingxi.core.permission.publish import PermissionGrantBlockedByAccountState
from lingxi.core.permission.publish_row import (
    ADMIN_FULL_ACCESS_FUNCTION,
    STATUS_APPROVED,
    aggregate_permission,
    build_translated_publish_row,
    parse_permissions,
    serialize_permissions,
)

logger = logging.getLogger(__name__)

_UTC = timezone.utc

#: 权限发布意图的原因码。与每日重算的原因码分开，让审计能一眼看出「这一版是开通排的
#: 还是重算排的」。
FIRST_ONBOARDING_REASON = "first_onboarding"


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
        metric_translation_map: Mapping[str, Mapping[str, Sequence[str]]] | None,
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
        failure_reasons: FailureReasonRecorder | None = None,
        local_overrides: LocalOverrideSource | None = None,
        legacy_importer: LegacyPermissionImporter | None = None,
        email_bindings: EmailBindingSource,
        position_grants: PositionGrantImporter | None = None,
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
        if stock_tokens is not None and legacy_importer is None:
            # **结构性防漏接**（rc25 S-1，Issue #540；与 ``full_access_wildcard`` 必填同一条
            # 纪律）：存量令牌源装了、差集导入口没装，会静默退回 rc21–rc24 的"只复制令牌
            # 不读权限"——存量用户首聊后权限被收窄到银河（PM 本人 9→5 的实证）。宁可
            # 构造期就拒绝。
            raise TypeError("注入存量令牌源时必须同时注入存量差集导入口 legacy_importer")
        #: 存量差集导入口：``None`` 只在存量令牌源也为 ``None`` 时合法（能力整体关闭）。
        self._legacy_importer = legacy_importer
        self._decisions = decisions
        self._readiness = readiness
        self._notifier = notifier
        self._ledger = ledger
        self._audit = audit
        self._role_function_map = role_function_map
        # 「公司+职能→指标名」翻译映射（Issue #227 / #346 修复）：与 ``publish_allowed``
        # 闸门共用装配层加载的**同一个对象**（``apps/scheduler/onboarding.py`` 的
        # ``_build_onboarding_duty``），不在这里另读一份文件。``None``（未加载/加载
        # 失败）与空映射在使用处按同一个结论处理——归一化成 ``{}``，交给
        # ``translate_company_functions`` 自己判定 fail-closed（见 ``_publish``），
        # 不在构造期额外做一次判断，理由与 ``permission_refresh._refresh_user`` 同一
        # 处注释一致：这条逐用户判据的正确性不该依赖"调用方一定会先做整轮判据"。
        self._metric_translation_map = metric_translation_map or {}
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
        #: 失败原因落库口（Issue #337，可选，见 ``FailureReasonRecorder`` 协议
        #: 文档）。``None``＝未装配，行为与接线之前逐字节一致——``/admin trace``
        #: 只是查不到这条链的失败原因，不影响链本身任何结论。
        self._failure_reasons = failure_reasons
        # 本地权限覆盖读取口（S-P-3）：``None``＝装配层未接线，行为与改动前逐字节一致。
        self._local_overrides = local_overrides
        #: 「同邮箱已绑给另一个人」的回读口（rc25 S-2a，对抗审查 X-1）。**必填、
        #: 没有哨兵值**：这道闸挡的是"两个人共用同一把问数令牌与同一行正式表权限"，
        #: 缺省不装等于把它关掉，与 ``publish_allowed``/``innertest_roster_gate``
        #: 同一条"没有默认放行"的纪律；漏接在构造期就是 ``TypeError``。
        self._email_bindings = email_bindings
        #: 预授权落库口（Issue #541，可选）。``None``＝未装配：首聊路径一行都不碰它；
        #: **系统触发带了预授权却没装它时整链失败关闭**（见 ``import_preprovision_grant``）。
        self._position_grants = position_grants
        self._lock = threading.Lock()
        self._running: dict[str, str] = {}
        #: 已经因为「通知没送到」释放过一次认领的事件。**每条事件只放回一次**：释放让下一轮
        #: 把整条链重跑一遍，而链上有可能等满十五分钟的就绪确认；无上界地放回会让一次飞书
        #: 长时间不可用把执行器永久占满。第二次仍然送不到就记账收口，并留一条 ``failed``
        #: 后缀的响亮审计。
        self._released_for_notify: set[str] = set()

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

    def start_system(
        self,
        *,
        email: str,
        trace_id: str,
        origin: str = ORIGIN_PREPROVISION,
        initiated_by_open_id: str,
        preprovision_grant: Any | None = None,
    ) -> OnboardingResult:
        """系统触发的开通入口（Issue #541 预开通，无入站消息）：按邮箱定位、账本 no-op、全程静默、**同步返回终态**；判定与写入逐字节共用 :meth:`_run`。整段实现、参数形状与立论见 :mod:`lingxi.core.identity.preprovision`。"""

        return run_system_onboarding(self, email=email, trace_id=trace_id, origin=origin, initiated_by_open_id=initiated_by_open_id, preprovision_grant=preprovision_grant)

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

    def _record_failure_reason(
        self, *, trace_id: str, failure_reason: str, event_type: str
    ) -> None:
        """把这一次失败终态的原因落库（Issue #337，可选，见
        :class:`~lingxi.core.identity.onboarding_ports.FailureReasonRecorder`
        文档）供 ``/admin trace <追溯号>`` 消费。**最佳努力**：与
        :meth:`_notify_admin_of_failure` 同一条纪律——落库失败不得带走已经决定的
        终态或已经完成的通知/记账，只改记一条自己的失败审计。"""

        if self._failure_reasons is None:
            return
        try:
            self._failure_reasons.record_failure(
                trace_id=trace_id, failure_reason=failure_reason, event_type=event_type
            )
        except Exception as error:  # noqa: BLE001 - 落库失败不得带走已经决定的终态
            self._audit.record(
                "onboarding.failure_reason_record_failed",
                error=type(error).__name__,
                trace_id=trace_id,
            )

    # ------------------------------------------------------------------
    # 执行线程
    # ------------------------------------------------------------------

    def _execute(self, *, event_id: str, open_id: str, trace_id: str, claim_token: Any = None, grant: PreprovisionGrant | None = None) -> _Terminal | None:
        """跑完一条链、通知用户、记账。**异常不外抛**（它跑在执行线程上）。

        返回这一次的终态（停机中止时 ``None``）：``start()`` 那条路不看它；系统触发那条路（Issue #541）要把它同步交回给批量脚本。"""

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
                event_id=event_id, open_id=open_id, trace_id=trace_id, stalled=stalled, grant=grant
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
                "onboarding.chain_failed", event_id=event_id, error=type(error).__name__, trace_id=trace_id
            )
            # C-6：`logger.exception` 连异常正文一起记，psycopg 唯一键冲突正文带着真实 open_id（违 V-花名册-33）；只记类型名与调用栈帧。本行刻意压成单行，见 tests/test_log_exception_body_leak.py。
            logger.error("首次开通编排未预料的失败 event=%s error=%s\n调用栈（不含异常正文）：\n%s", event_id, type(error).__name__, "".join(traceback.format_tb(error.__traceback__)))
            terminal = _internal(f"unexpected_{type(error).__name__}")

        ledger = NULL_DISPATCH_LEDGER if is_system_trigger(event_id) else self._ledger  # Issue #541：系统触发没有 inbound_event 行，两个账本方法都没有对象
        self._audit.record(
            "onboarding.result",
            event_id=event_id,
            origin=origin_of(event_id),
            state=terminal.state.value,
            failure_reason=terminal.reason,
            content_key=terminal.key,
            trace_id=trace_id,
        )
        if terminal.reason is not None:
            # 失败原因落库（Issue #337）：紧邻上面那条既有审计，只在真的是一次
            # 失败终态（``reason`` 非空，成功完成的 ``_completed()`` 从不设置它）
            # 时落一行——见 :meth:`_record_failure_reason` 与 ``FailureReasonRecorder``
            # 协议文档。
            self._record_failure_reason(
                trace_id=trace_id, failure_reason=terminal.reason, event_type="onboarding.result"
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
                ledger.mark_onboarding_dispatched(event_id=event_id)
            except Exception as error:  # noqa: BLE001 - 记不上账最坏只是被下一轮再捞一次
                self._audit.record(
                    "onboarding.dispatch_record_failed",
                    event_id=event_id,
                    error=type(error).__name__,
                    trace_id=trace_id,
                )
        return terminal

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
        系统触发（Issue #541 预开通）**不发消息、按送达处理**，见 :func:`~lingxi.core.identity.preprovision.deliver_silently`。

        有限重试而不是一次定生死：一次飞书抖动就让用户永远停在「已收到」，代价与收益完全
        不成比例。重试之间用注入的 ``sleep``，因此纯单测里一秒都不用等。

        ``dedupe_key`` 绑定**事件 + 用途**：同一条首聊事件的重复执行（重新认领、重启后
        重跑）只会让用户看到同一条结论一次；而进度提示与终态是两个用途，各自一个键，
        不会互相去重掉。
        """

        if is_system_trigger(event_id):
            return deliver_silently(key=key, open_id=open_id, users=self._users)
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
        grant: PreprovisionGrant | None = None,
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

        # **同邮箱已绑给另一个人**（rc25 S-2a，对抗审查 X-1）：挡在**建档之前**——
        # 早于任何数据库写入、早于令牌签发/采纳（``_issue_token`` 会按邮箱把正式表
        # 存量行里**别人的**密文采纳过来）、早于存量差集导入、也早于任何发布意图。
        # 判据是 ``feishu_open_id``（建档之前本人还没有 ``app_user.id``）；"为什么
        # 不等迁移 0085 的唯一索引在写入时拒绝"见
        # ``core/identity/onboarding_guards.reject_email_bound_to_another_person``。
        # 放在 ``_run`` 的公共段而不是某条入口分支里：这条链会被「收到首聊消息」与
        # Issue #541 的「预开通」（系统触发、无入站消息）共同复用，挂在分支上的闸对
        # 第二条路径等于不存在。
        bound_elsewhere = reject_email_bound_to_another_person(
            open_id,
            request.email,
            bindings=self._email_bindings,
            audit=self._audit,
            trace_id=trace_id,
        )
        if bound_elsewhere is not None:
            return bound_elsewhere

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

        # 银河翻译只算一次（rc25 S-1），翻译失败在这里 fail-closed（早于令牌与环境）。
        galaxy_map = self._translate_galaxy(user_id, aggregate)
        if isinstance(galaxy_map, _Terminal):
            return galaxy_map

        # 存量差集导入挂在零银河判定**之前**（rc25 S-1，Issue #540）：正式表只读一次，
        # 查找结果同时供 `_issue_token` 采纳令牌。
        self._stop_guard()
        lookup = self._lookup_stock_token(request.email)
        if lookup is not None and lookup.state == ADOPTABLE:
            self._import_legacy_permissions(
                user_id, lookup, aggregate, galaxy_map, open_id=open_id, trace_id=trace_id
            )

        if grant is not None:
            # 预开通那一笔预授权（Issue #541）：与 S-1 差集导入**同一个挂点**，且同样排在
            # 零银河判定**之前**——名单里"银河零权限、靠预授权吃饭"的人，先判零权限就会被
            # 整批拒绝。落库口没装配时整链失败关闭，见该函数文档。
            import_preprovision_grant(self._position_grants, grant, user_id=user_id, open_id=open_id, now=self._clock(), audit=self._audit, trace_id=trace_id)

        if not aggregate.granted:
            # 零银河权限：现在才有 `app_user.id`，查一次**本地授权**。放在
            # 令牌签发/用户环境创建**之前**——不为一个最终会被拒绝的人签发
            # 问数 MCP 令牌、写一份带凭据的用户环境。
            rejected = reject_zero_galaxy_without_local_grant(
                user_id,
                aggregate,
                resolve_local_overrides=self._resolve_local_overrides,
                audit=self._audit,
                trace_id=trace_id,
            )
            if rejected is not None:
                return rejected

        self._stop_guard()
        issued = self._issue_token(user_id, lookup)
        self._create_environment(user_id, issued)
        self._users.advance_provisioning_state(user_id, to=STATE_PROVISIONING)
        # **分水岭**（Issue #282 §0.1）：从这一行起，任何失败终态都会让这个人停在
        # ``provisioning``/``mcp_syncing``，不再自然回到 ``matching``。把 ``user_id``
        # 交给调用方的可变容器，供 ``_abort_if_stalled`` 判断「要不要当场收口」——写在
        # 这里而不是更早，是因为更早的失败（身份定位、匹配、建档）本来就停在
        # ``matching``/``guest``，不在 ``_PROVISIONING_IN_FLIGHT`` 里，不需要收口。
        stalled["user_id"] = user_id

        self._stop_guard()
        published = self._publish(
            user_id, request, aggregate, issued, galaxy_map=galaxy_map, trace_id=trace_id
        )
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
        # **不再在这里直接判定"无可用银河权限"**（PM 2026-08-29 裁定，Issue #419，
        # 消 `V-权限-15` 此前登记的已知限制）：零银河权限的用户如果存在本地授权
        # （管理员兜底赋权），仍应当继续开通并发布本地授权集合，与每日重算
        # （`permission_refresh.py::_refresh_user`）的语义保持一致。这里还没有
        # `app_user.id`（本地覆盖条目的键，迁移 `0072`；本方法发生在 `_provision`
        # 之前），查不了本地覆盖，因此真正的"这个人到底有没有可发布内容"判定推迟
        # 到建档、账号状态复核之后（`_run` 新增的 `_reject_zero_galaxy_without_
        # local_grant` 调用点），不在这里提前拒绝——但也不为一个注定被拒绝的人
        # （既无银河也无本地授权）签发令牌、建用户环境，见该方法文档。

        if aggregate.granted and not self._publish_allowed():
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
            # 不留下任何半成品。**这道闸门的适用范围没有变**（Issue #419「既有出口
            # 闸门全部保持」）：只保护"银河内容需要翻译才能安全发布"这件事——零银河
            # 权限的用户没有银河内容需要翻译，本地授权本身已经是精确指标名，与改动前
            # "零银河用户结构上从不到达这道检查"逐字节一致（该检查此前挂在
            # `aggregate.granted` 的早退之后，零银河用户从未走到过这里）。
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

    def _lookup_stock_token(self, email: str | None) -> StockTokenLookup | None:
        """按邮箱查一次存量令牌源（#281 改道）；``None``＝源未装配，原样走签新路径。
        rc25 S-1 起提前到零银河判定之前：同一结果供差集导入与 `_issue_token` 共用。"""

        if self._stock_tokens is None:
            return None
        try:
            return self._stock_tokens.lookup(email)
        except Exception as error:  # noqa: BLE001
            raise OnboardingChainError(
                f"stock_token_lookup_failed_{type(error).__name__}"
            ) from error

    def _import_legacy_permissions(
        self,
        user_id: str,
        lookup: StockTokenLookup,
        aggregate: Any,
        galaxy_map: Mapping[str, Sequence[str]],
        *,
        open_id: str,
        trace_id: str,
    ) -> None:
        """存量用户首聊差集导入（rc25 S-1）：见 ``legacy_permission_import.import_legacy_permissions``。"""

        assert self._legacy_importer is not None  # 构造期不变式：源装了导入口必装
        import_legacy_permissions(
            importer=self._legacy_importer,
            audit=self._audit,
            metric_translation_map=self._metric_translation_map,
            now=self._clock(),
            user_id=user_id,
            permissions_text=lookup.permissions,
            full_access_wildcard=ADMIN_FULL_ACCESS_FUNCTION in aggregate.functions,
            galaxy_map=galaxy_map,
            open_id=open_id,
            trace_id=trace_id,
        )

    def _issue_token(self, user_id: str, lookup: StockTokenLookup | None) -> Any:
        """签发或采纳该用户的问数 MCP 访问令牌（adopt-or-issue，#281 改道裁定）。
        ``lookup`` 为 ``None``（源未装配）＝原签发路径，与接入前逐字节一致；无行 / 有行
        无密文退回签新；有行含密文则采纳；**解密失败响亮失败、绝不退回签新**（签新会让
        用户环境令牌与正式表错位，造成真实 MCP 认证失败）。"""

        if lookup is not None:
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

    def _adopt_token(self, user_id: str, lookup: StockTokenLookup) -> Any:
        try:
            adopted = self._tokens.adopt_token(user_id, lookup.secret)
        except Exception as error:  # noqa: BLE001
            raise OnboardingChainError(f"token_adopt_failed_{type(error).__name__}") from error
        # 权限面由银河同步权威决定，不由本步裁量——这里只审计标注，不改变采纳与否
        # （#281 改道裁定第四条）。旧行的 ``permissions`` 由 `_import_legacy_permissions`
        # 作为管理员本地授权导入（rc25 S-1），本步仍只管令牌。
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

    def _translate_galaxy(self, user_id: str, aggregate: Any) -> dict[str, tuple[str, ...]] | _Terminal:
        """银河聚合 → 「公司 → 指标名」，只算一次供导入与发布共用（rc25 S-1）；实现见
        ``core/identity/legacy_permission_import.translate_galaxy``。"""

        return translate_galaxy(
            audit=self._audit,
            metric_translation_map=self._metric_translation_map,
            user_id=user_id,
            aggregate=aggregate,
        )

    def _publish(
        self,
        user_id: str,
        request: ProvisioningRequest,
        aggregate: Any,
        issued: Any,
        *,
        galaxy_map: Mapping[str, Sequence[str]],
        trace_id: str,
    ) -> _Terminal | tuple[int, str]:
        if not request.email:
            # 发布行的 record_key/email 两列都来自邮箱；纯工号匹配成功但花名册没有邮箱时
            # 没有"这一行是谁的"的答案。归确定性业务失败，不是本侧故障。
            return _not_authorized("archived_identity_incomplete")
        # 本地权限覆盖合并（S-P-3 #319），接线点在这里而非更早的 `_match`——本地覆盖的 `user_id` 是内部 `app_user.id`，聚合时还没有它。
        # `galaxy_map` 由 `_run` 经 `_translate_galaxy` 算好传入（rc25 S-1）：已翻译的指标名（`{公司: (指标名, …)}`），与 `permission_refresh.py::_refresh_user` 同一条路径产出；零银河用户则是恒为空的 `{}`。
        local = self._resolve_local_overrides(user_id)
        # 通配角 v2（Issue #440）：`all_companies=True` 有两个互相独立的成因
        # （`scope.all_countries` 或持有 `ADMIN_FULL_ACCESS_FUNCTION`），只有后者
        # 是「真全指标通配」——`merge_permission_sources` 自己不猜测，调用方必须
        # 显式声明（见该函数「通配角 v2」文档）。零银河分支 `aggregate.functions`
        # 恒为空元组，`in` 判据天然为 False，参数在 `galaxy={}` 时本就无效果。
        merged = merge_permission_sources(
            galaxy=galaxy_map,
            local=local,
            full_access_wildcard=ADMIN_FULL_ACCESS_FUNCTION in aggregate.functions,
        )
        for reason in merged.skipped_reasons:  # 通配角 v1：见 merge_sources.py「通配角」一节
            self._audit.record("onboarding.local_override_skipped", user=user_id, reason=reason)
        if merged.unrepresentable_companies:
            # 本地「全部」组下某公司被抑制到空，读侧回退制无法表示（merge_sources.py
            # 「本地 "*" 组」一节）：fail-closed，不发布、不撤权，交管理员先撤组再抑制。
            self._audit.record(
                "onboarding.publish_gate_closed",
                user=user_id,
                reason="suppression_on_all_scope_unrepresentable",
            )
            return _internal("suppression_on_all_scope_unrepresentable")
        if not merged.permissions:
            if not aggregate.granted:
                # 防御性分支，理论上不会发生：`_reject_zero_galaxy_without_local_
                # grant` 已经用同一个合并函数确认过非空结果，两次调用之间只隔着
                # 令牌签发/环境创建（都不写本地覆盖表），除非管理员恰好在这个极短
                # 窗口收回了授权（TOCTOU）。归回"无可用银河权限"而不是下面的
                # `fully_suppressed_by_local_override`——原因不同：不是本地抑制
                # 把银河给的压光到零，是银河本来就没给，且这一刻本地授权也没了。
                return _not_authorized(aggregate.reason)
            # 红线-2：galaxy_map 非空（`_match` 已确保 granted），但本地抑制把合并
            # 结果压光到空字典。onboarding 是首次建行，空内容的新建行对 MCP 无意义，
            # 归类为确定性业务失败（同 `_match` 的 `not granted → _not_authorized`），
            # 不落到 `build_translated_publish_row` 对空输入的 `ValueError`。
            return _not_authorized("fully_suppressed_by_local_override")
        row = build_translated_publish_row(
            company_metrics=merged.permissions,
            email=request.email,
            display_name=request.identity.display_name,
            decided_at=self._clock(),
            token_cipher=issued.token_cipher,
        )
        try:
            decision = self._decisions.record_decision(
                user_id=user_id,
                row=row,
                reason=FIRST_ONBOARDING_REASON,
                # Issue #483：首次开通同样是一份**需要账号有效**的授权。`_recheck_
                # still_provisionable` 已经复核过一次账号状态，但那次复核到这里之间
                # 隔着令牌签发与用户环境创建——管理员恰好在这个窗口停用账号是真实
                # 形状，只有落决定那把行锁里的复检能真正把它串起来。
                require_enabled_account=True,
                decided_at=self._clock(),
            )
        except PermissionGrantBlockedByAccountState as blocked:
            # **必须收敛到既有的停用终态，不能变成通用内部故障**：模块文档
            # 「``account_state != 'enabled'`` → 停止开通，不建环境、不发权限，用户按
            # 「账号已停用」告知」说的正是这一种，用户看到的仍是「你的 BI Plus 账号
            # 当前已停用」。审计动作与 `_recheck_still_provisionable`/`_confirm` 两处
            # 复核逐字相同——运维按同一个动作名检索"这条开通链因为账号状态停下了"，
            # 不需要区分是哪一次复核抓到的。事务整体回滚：版本没推进、意图没入队。
            self._audit.record(
                "onboarding.halted_account_state",
                user=user_id,
                account_state=blocked.account_state,
                trace_id=trace_id,
            )
            return _Terminal(
                OnboardingState.NOT_AUTHORIZED, KEY_SUSPENDED, reason="account_not_enabled"
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

    def _resolve_local_overrides(self, user_id: str) -> ResolvedLocalOverrides | None:
        """读该用户当前生效的本地覆盖条目。``None``（未装配/读取失败）时对
        ``merge_permission_sources`` 恒等；读取失败额外响亮记一条
        ``onboarding.local_override_skipped``（``reason=local_override_read_failed``），
        异常不冒泡——一次开通不因本地覆盖读取失败而整链失败。"""

        if self._local_overrides is None:
            return None
        try:
            entries = tuple(self._local_overrides.effective_entries(user_id=user_id))
        except Exception as error:  # noqa: BLE001 - 本地源读取失败只降级，不整链失败
            self._audit.record("onboarding.local_override_skipped", user=user_id, reason=REASON_LOCAL_OVERRIDE_READ_FAILED)
            logger.error("本地权限覆盖读取失败，本次开通跳过本地源 user=%s error=%s", user_id, type(error).__name__)
            return None
        return resolve_local_overrides(user_id=user_id, entries=entries)

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
