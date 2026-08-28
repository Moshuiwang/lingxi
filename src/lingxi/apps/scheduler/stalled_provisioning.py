"""开通中途停摆收口职责：认领超过租约、仍卡在中途格的用户最终一定离开
``provisioning``/``mcp_syncing``（Issue #282，[V-开通-19](../../../../docs/技术设计/验收矩阵.md)）。

## 补的是哪个洞

首次开通那条链（:class:`~lingxi.core.identity.onboarding_runner.AutoOnboardingRunner`）
把用户推进到 ``provisioning`` 之后失败，会让 ``provisioning_state`` 永久停在
``provisioning``/``mcp_syncing``。编排层自己的「当场收口」
（:meth:`~lingxi.core.identity.onboarding_runner.AutoOnboardingRunner._abort_if_stalled`）
覆盖了"链跑到了一个确定的失败终态"这一半；本职责覆盖**另一半**——链本身死掉、当场收口
够不到的地方：进程被强杀、执行线程异常退出到 ``_execute`` 都拿不到结论、收口写入自己
那一次恰好失败。这些情形下，``core/conversation/pipeline.py`` 对 ``PROVISIONING``
状态的短路分支会让用户从此只收到「正在完成…请稍候」，而**没有任何东西会回来看他**
（Issue #282 原始报告的用户可见形态）。

## 放在哪：一个新职责，对称于迟到就绪恢复

形状照 :class:`~lingxi.apps.scheduler.late_readiness_recovery.LateReadinessRecoveryDuty`：
``run_once()`` 自带幂等、可随时停、异常不外抛、报告只含计数与固定分类（不含任何
open_id / 用户资料值）、零候选时不记审计（该职责外部独立审查 F4 的同一条纪律）。

## 判据：认领时间 + 超时租约，不是状态列

候选查询在 :mod:`lingxi.adapters.postgres_stalled_provisioning`，完整判据与既有三个
恢复/对账职责的边界见该模块文档。

**租约取 45 分钟**（:data:`DEFAULT_STALLED_LEASE_SECONDS`），且由装配断言
:func:`~lingxi.apps.scheduler.onboarding.assert_stalled_lease_exceeds_chain_budget` 守
住——不成立时扫描会把一条正在正常跑的开通判成僵尸。这个数字**不是产品裁定**，是从
代码上界推出来的工程值（照 ``late_readiness_recovery.DEFAULT_RECOVERY_INTERVAL_SECONDS``
的同一条免责：产品负责人认为该更快或更慢，改这一个常量即可，不影响任何判定语义）。

## 收口写入：复用既有专用入口，不新写一份 CAS

真正把用户收口成 ``aborted`` 的写入是
:meth:`~lingxi.adapters.postgres_identity.PostgresAppUserStore.abort_stalled_provisioning`
——与编排层「当场收口」共用**同一个**方法，见该方法文档字符串的完整论证。本职责只负责
"谁该被收口、什么时候收口"，不重复实现"怎么安全地收口"。

## 处置顺序：**先通知，后收口**

对每个候选（逐条处理）：

1. **先发通知，通知送达才 CAS 收口；通知送不到就留在原状态，下一轮重来（幂等）。**
   顺序与 ``late_readiness_recovery`` 的 F1 教训一致但方向相反：那边是"推进 active
   而不告知"会让人永远等不到；这边是"收口 aborted 而不告知"会让一个已经被告知过
   「请不要重复发送」的人从此再也不发消息，于是"下一条消息自动重试"这条自愈路径永远
   不会被触发。
2. ``dedupe_key = f"onboarding:stalled:{event_id}"``——与终态通知的
   ``f"onboarding:{event_id}"``（``onboarding_runner.py`` 的 ``_notify``）刻意不同键，
   两者不会互相去重掉：一条真正死掉的链此前可能从未发出过任何终态通知。
3. 通知发送成功但 CAS 之前进程崩溃 → 下一轮用**同一个** ``dedupe_key`` 再发一次。
   用户会不会看到两条，取决于飞书 ``im/v1/messages`` 对同一 ``uuid`` 是否去重——
   **本仓库未验证的平台行为**，与 ``late_readiness_recovery`` 模块文档已登记的那条
   同型，不新增补偿逻辑。
4. CAS 返回 0 行（有人在这中间改了状态——被另一条并发链推进、被停用、或已经被本职责
   上一轮收口过）→ 计一条 ``advance_refused``，本条到此为止，**不重发**——通知已经
   发出去了，重发只会制造第二条相互矛盾的消息。
5. 全程审计只记 ``user_id`` / ``state`` / ``reason`` / ``trace_id`` / 计数，**不记
   open_id、不记任何资料值**（``LateReadinessRecoveryReport`` 同一条纪律）。

## 通知节奏：进程内退避 + 过采样（外部独立审查 P2-2 修复）

候选查询本身不带"这个人上一次被通知是什么时候"这个事实——通知失败**不会**改变
``provisioning_state``（P2-1 修复：改了状态会让他直接退出候选集合，比通知节流严重
得多），因此只要状态没变，一个持续通知失败的用户会在**每一轮** ``SchedulerLoop``
tick（约 60 秒）都被重新选中、重新打一次飞书——没有节流的话就是无上界的重试速率。

修法是 :data:`DEFAULT_NOTIFY_RETRY_BACKOFF_SECONDS`：职责实例在进程内存里记
"这个用户最近一次被尝试处理是什么时候"，同一个用户在退避窗口内**跳过**，不占用本轮
处理名额、也不发起新的通知调用。零迁移前提下没有持久化面可用，这条退避状态只存在
单个进程的内存里，与「候选查询没有带过期时间的持久租约」同一个单实例假设——见下面
「已知边界」。

退避带来第二个问题：候选查询按最早认领优先排序，如果排在最前面的若干个恰好都在
退避期内，只取 ``limit`` 条会让这些"毒候选"把整轮名额占满，饿死后面真正等待处理的
新候选——与 ``late_readiness_recovery`` 外部独立审查 F4 挡住的"毒候选饿死正常候选"
同一个风险形状。修法是 :data:`STALLED_FETCH_OVERSAMPLE`：候选查询按
``limit × 过采样倍数``（封顶 ``_MAX_FETCH_LIMIT``）多取几条，在进程内跳过退避期的
之后再从剩下的里挑最多 ``limit`` 个真正处理，用一次查询多扫描几行的成本换掉饿死
风险。

## 通知文案：专用键 ``onboarding.stalled``（Issue #280 裁定 B2-2）

PR-1 阶段曾复用 ``onboarding.internal_error`` 逐字发送；产品负责人裁定改成专用文案
``KEY_STALLED``（``onboarding.stalled``），说明"等待已久仍未取得结果、可以再发一条
消息重新开始"，不再套用一般性的内部故障话术。该键带追溯号占位（``{reference}``），
用本次候选自己的 ``item.trace_id`` 渲染——同一条链的用户后续再触发一次新链时，两次
终态提示不再逐字相同，管理员也能拿着这个追溯号去核对是哪一次。

## 停摆计数送达管理群（Issue #280 §7.3 步 2）

``alert`` 是可选注入的计数回调（形状照 ``AlertingDuty.onboarding_stalled_callback()``）。
「有人卡住了」此前没有任何审计信号会送到管理群——本轮真正收口（``aborted``）的候选数
大于零时调用一次，只传聚合计数，不含任何单个候选的 user_id / open_id / 追溯号（聚合
批次没有单一追溯号可代表，见 ``core/alerting.py`` 对应回调文档）。回调失败不得带走
本轮已经做完的收口结果，异常在这里被吞掉、只记审计。

## 缺通知出口时的姿态：绝不"改状态不告知"

``notifier is None`` → 本轮**一条都不处理**，只记一次
``stalled_provisioning.notifier_not_wired``，并在报告里带 ``notifier_wired=False``。
不允许"先改状态、通知以后再补"——那正是本 Issue 要修的那种半开状态。当前部署下这一路
是防御性的：飞书应用凭据是 :class:`~lingxi.apps.scheduler.config.SchedulerConfig` 的
必填项，进程起不来就没有装配的机会，因此装配层（``assembly.py``）总能建出一个真实
notifier；这条分支存在是为了让"以后有人把它改成可选"或"测试里就是要验证这个边界"这两种
情形都有明确定义的行为，不是当前生产会走到的路径。

## 幂等与竞态：扫到的同时那条链恰好还活着

四道独立防线，任意一道单独失效都不会造成用户可见伤害：

| # | 防线 | 挡住什么 |
|---|---|---|
| 1 | 租约 45 分钟 > 链预算约 20 分钟，且由装配断言守住 | 正常慢链被误判 |
| 2 | 不释放认领（``onboarding_dispatched_at`` 保持非空） | 老事件被第二次认领 ⇒ 结构上不可能双跑同一条事件 |
| 3 | 收口写入的 CAS 只接受 ``provisioning``/``mcp_syncing`` 且账号启用 | 覆盖 ``active``（假失败）、覆盖已停用账号 |
| 4 | 编排自己的进程内去重（按 ``open_id``） | 用户重发触发的新链撞上一条真的还活着的老链 |

**真的撞上了会怎样**：扫描在租约到期那一刻把一条仍在 ``mcp_syncing`` 探针中的活链写成
``aborted``，随后那条活链探到 READY 并调用 ``advance_provisioning_state(to='active')``。
``_PROVISIONING_ORDER`` 里 ``aborted`` 与 ``guest`` 同 rank ⇒ ``allowed`` 含
``aborted`` ⇒ 推进成功，用户被正确激活并收到「开通完成」。净结果：用户先收到一条
多余的失败终态，随后收到正确的成功终态。**方向是安全的**（永远不会把没就绪的人写成
``active``，永远不会把已成功的人打回失败），且概率被防线 1 压到接近零。如实登记，
不加补偿。反向（链先 ``active``，扫描后 ``aborted``）被防线 3 在 SQL 层排除。

## 已知边界（明确接受，不修）

- 候选查询的认领没有带过期时间的持久租约（单实例假设），与 ``late_readiness_recovery``
  「已知边界」和 #250 同一判据。多实例部署或滚动发布重叠窗口出现时必须回来补。
- **通知退避同样只在进程内存里**（同一单实例假设）：进程重启会清空
  :attr:`StalledProvisioningDuty._last_attempt`，重启后的下一轮可能比退避窗口更早
  再打一次飞书——最坏代价是多发一次通知（同一 ``dedupe_key``，是否被飞书去重见处置
  顺序第 3 条），不是正确性问题。多实例部署下每个实例各自维护一份退避状态，效果上
  等于把全局重试速率乘以实例数——多实例场景出现时必须回来重新评估。
- 通知发送与 CAS 收口不在同一个数据库事务里（收口写入没有内建的通知 outbox）：这是
  与 ``late_readiness_recovery`` 的 F1 修复不同的形状——那边的通知是持久 outbox、独立
  重试到送达为止；本职责的通知是**一次性**尝试（不送达就整条候选留在原状态，交给
  下一轮候选查询重新捞起，本身已经是一种"重试"，只是重试的是整条处置而不是单独的
  通知）。两种形状都能达到"不会改状态不告知"这条底线，选择更简单的这一种是因为本职责
  的候选集合极小（停摆本身就是罕见事件），不值得为它另建一张 outbox 表。
- **编排层「当场收口」通知送达之后、CAS 之前进程崩溃 → 用户可能收到两条相似的终态
  通知，重复上界 2 条**（外部独立审查 P3-3 登记）：
  :meth:`~lingxi.core.identity.onboarding_runner.AutoOnboardingRunner._abort_if_stalled`
  的收口发生在 ``_notify`` 已经确认送达**之后**（``onboarding:{event_id}`` 这个
  dedupe_key 下的那条终态通知）；如果进程在"通知已送达"与"CAS 真的落库"之间崩溃，
  ``provisioning_state`` 会原样停在中途格，45 分钟后被本职责的候选查询捞到，并用
  **不同的** dedupe_key（``onboarding:stalled:{event_id}``，见「处置顺序」第 2 条）
  再发一条内容相近的 ``LX-ONBOARD-001``。两条 dedupe_key 刻意不同，因此飞书那一层
  不会把它们去重掉——用户这种情况下会看到两条相似的失败通知，不是一条。这与「处置
  顺序」第 3 条（本职责自己"通知已发送、CAS 之前崩溃"的窗口）是**同一种**至少一次
  投递的既有产品裁定（2026-08-18 裁定 4）在两个不同触发点上的体现，如实登记，
  不新增补偿逻辑：崩溃窗口本身极窄（一次数据库写入的耗时），概率接近零，且方向
  安全（多发一条不会误导用户"已经开通完成"，两条说的都是"还没成功"）。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from lingxi.core.identity.onboarding_runner import (
    KEY_STALLED,
    STATE_MCP_SYNCING,
    STATE_PROVISIONING,
)
from lingxi.core.permission.mcp_readiness import ReadinessSchedule

from lingxi.apps.scheduler.audit import AuditSink
from lingxi.apps.scheduler.config import SchedulerConfig

logger = logging.getLogger(__name__)

#: 停摆租约（秒）。见模块文档「判据」一节：由装配断言
#: ``assert_stalled_lease_exceeds_chain_budget`` 守住，必须严格长于一条链在
#: provisioning/mcp_syncing 两格上可能停留的最长时间。
DEFAULT_STALLED_LEASE_SECONDS = 2700

#: 单轮最多**处理**（真的发起通知/收口）多少个停摆候选。配额保护，不是重试
#: 上限——被推迟到下一轮的候选不丢失任何进度（进度全在 ``inbound_event``/
#: ``app_user`` 里）。
DEFAULT_STALLED_LIMIT = 50

#: 同一个用户两次通知尝试之间的最短间隔（秒，外部独立审查 P2-2 修复）。
#: ``SchedulerLoop`` 的 tick 周期约一分钟，而候选查询在通知持续失败、状态没有
#: 任何变化时会**每一轮都重新选中同一个人**——不加节流会对一个持续失败的收件人
#: 无上界地每分钟打一次飞书。取五分钟，与 ``late_readiness_recovery`` 通知退避
#: 的起步档同一个数量级；本实现零迁移、没有持久化面，退避状态只存在**进程内存
#: 里**（单实例假设，见模块文档「已知边界」）——进程重启最多让下一轮多试一次，
#: 不是正确性问题，只是浪费一次配额。
DEFAULT_NOTIFY_RETRY_BACKOFF_SECONDS = 300

#: 候选查询的**过采样**倍数（外部独立审查 P2-2 修复的另一半）：查询按最早认领
#: 优先排序，如果排在最前面的若干候选恰好都在退避期内（持续通知失败的"毒候选"），
#: 只取 ``limit`` 条会让它们把整轮名额占满，后面真正等待处理的新候选永远排不上
#: 号——这正是 ``late_readiness_recovery`` 外部独立审查 F4 挡过的"毒候选饿死正常
#: 候选"在本职责这里的同型风险。查询多取几倍、在进程内跳过退避中的候选后再截到
#: ``limit``，用一次查询多扫描几行的成本换掉这个饿死风险；候选集合本身很小
#: （停摆是罕见事件），过采样代价可以忽略。
STALLED_FETCH_OVERSAMPLE = 4
#: 过采样查询量的硬上限，避免候选集合万一真的很大时一次查询扫描过多行。**这同时是
#: 饿死保护本身的上限**（编排者第二轮定向复核 P2-2 concern）：过采样解决的是"退避中的
#: 候选排在最前面、把 `limit` 名额占满"，但查询本身仍然只取最早的 `_MAX_FETCH_LIMIT`
#: 条——如果同一时刻处于退避期的老候选数量**超过**这个上限，新候选会被再次挤出结果集
#: 之外，回到"过采样也救不了"的原始饿死处境。当前规模下（停摆是罕见事件）不会触及，
#: 一旦触及说明积压的停摆候选已经远超正常水位，需要先处理积压本身，而不是继续加大
#: 这个上限。
_MAX_FETCH_LIMIT = 200

_EXPECTED_STATES: tuple[str, ...] = (STATE_PROVISIONING, STATE_MCP_SYNCING)


class _Candidates(Protocol):
    """停摆候选的读取口（``adapters/postgres_stalled_provisioning.py``）。"""

    def stalled_provisioning_candidates(
        self, *, lease_seconds: int, limit: int = ...
    ) -> Sequence[Any]: ...


class _Aborter(Protocol):
    """收口写口（``adapters/postgres_identity.PostgresAppUserStore.
    abort_stalled_provisioning``，与编排层「当场收口」共用同一份实现）。"""

    def abort_stalled_provisioning(
        self, *, user_id: str, expected_states: Sequence[str], reason: str
    ) -> bool: ...


class _Notifier(Protocol):
    """终态的主动私聊（``apps/scheduler/onboarding.CatalogNotifier``）。

    **可选**：见模块文档「缺通知出口时的姿态」。缺失时本轮一条候选都不处理。
    """

    def send(
        self, *, open_id: str, key: str, values: Mapping[str, object], dedupe_key: str
    ) -> None: ...


class _AuditSink(Protocol):
    def record(self, action: str, /, **fields: object) -> None: ...


@dataclass(frozen=True)
class StalledProvisioningReport:
    """一轮的结果。**只有计数与固定分类，没有任何字段值**（同
    ``LateReadinessRecoveryReport`` 的纪律：内部用户标识、open_id 一个都不进报告）。"""

    #: 本轮**真正处理**（发起了通知尝试）的候选数——**不等于**本轮候选查询取到的
    #: 行数（编排者第二轮定向复核 N-2：过采样查询取回的候选里，处于通知退避期的
    #: 那些被跳过、不计入这里，改记 :attr:`skipped_in_backoff`）。
    examined: int = 0
    #: 通知发送成功的候选数。
    notified: int = 0
    #: 真正被收口成 ``aborted`` 的候选数（通知送达且 CAS 命中）。
    aborted: int = 0
    #: 通知发送失败的候选数——**不收口**，留在原状态等下一轮重来。
    notify_failed: int = 0
    #: 通知送达但 CAS 返回 0 行（状态在候选查到与收口之间被别的路径改写）。
    advance_refused: int = 0
    failed: int = 0
    #: 因为处于通知退避期而被跳过、本轮**没有**真正处理的候选数（N-2 新增）。
    #: 与 :attr:`examined` 分开计数：如果不把它单独记下来，一轮里查询取到的候选
    #: 全部恰好都在退避期时，:attr:`examined` 会是 0，运维读审计会误以为"这一轮
    #: 没有任何停摆候选"，而真实情况是"有 N 个人还停在中途格，只是刚打过一次
    #: 飞书还没到重试时间"——这两件事对运维的含义完全不同，不能被同一个 0 掩盖。
    skipped_in_backoff: int = 0
    interrupted: bool = False
    #: 通知出口装配了没有。见模块文档「缺通知出口时的姿态」。
    notifier_wired: bool = True

    def audit_facts(self) -> dict[str, Any]:
        facts: dict[str, Any] = {
            "examined": self.examined,
            "notified": self.notified,
            "aborted": self.aborted,
            "notify_failed": self.notify_failed,
            "advance_refused": self.advance_refused,
            "failed": self.failed,
            "skipped_in_backoff": self.skipped_in_backoff,
            "notifier_wired": self.notifier_wired,
        }
        if self.interrupted:
            facts["interrupted"] = True
        return facts


@dataclass
class _Tally:
    examined: int = 0
    notified: int = 0
    aborted: int = 0
    notify_failed: int = 0
    advance_refused: int = 0
    failed: int = 0
    skipped_in_backoff: int = 0

    def freeze(self, *, interrupted: bool, notifier_wired: bool) -> StalledProvisioningReport:
        return StalledProvisioningReport(
            examined=self.examined,
            notified=self.notified,
            aborted=self.aborted,
            notify_failed=self.notify_failed,
            advance_refused=self.advance_refused,
            failed=self.failed,
            skipped_in_backoff=self.skipped_in_backoff,
            interrupted=interrupted,
            notifier_wired=notifier_wired,
        )


class StalledProvisioningDuty:
    """按认领时间 + 租约周期性回看停在 ``provisioning``/``mcp_syncing`` 的用户。

    语义与边界见模块文档。本类**只编排**：候选查询在
    :mod:`lingxi.adapters.postgres_stalled_provisioning`，收口写入复用
    :meth:`~lingxi.adapters.postgres_identity.PostgresAppUserStore.
    abort_stalled_provisioning`，通知正文在 :mod:`lingxi.config.content`，这里一条
    规则都不复制。
    """

    name = "开通中途停摆收口"

    def __init__(
        self,
        *,
        candidates: _Candidates,
        aborter: _Aborter,
        audit: _AuditSink,
        notifier: _Notifier | None = None,
        alert: Callable[[int], None] | None = None,
        lease_seconds: int = DEFAULT_STALLED_LEASE_SECONDS,
        limit: int = DEFAULT_STALLED_LIMIT,
        notify_backoff_seconds: int = DEFAULT_NOTIFY_RETRY_BACKOFF_SECONDS,
        clock: Callable[[], float] | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds < 1:
            raise ValueError("停摆租约必须是正整数秒")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("单轮候选上限必须是正整数")
        if (
            isinstance(notify_backoff_seconds, bool)
            or not isinstance(notify_backoff_seconds, int)
            or notify_backoff_seconds < 0
        ):
            raise ValueError("通知退避必须是非负整数秒")
        self._candidates = candidates
        self._aborter = aborter
        self._notifier = notifier
        self._alert = alert
        self._audit = audit
        self._lease_seconds = lease_seconds
        self._limit = limit
        self._notify_backoff_seconds = notify_backoff_seconds
        self._clock = clock or time.monotonic
        #: 每个用户最近一次被**尝试**处理（无论成功与否）的单调时刻。仅进程内存，
        #: 见 ``DEFAULT_NOTIFY_RETRY_BACKOFF_SECONDS`` 的文档。
        self._last_attempt: dict[str, float] = {}
        self._stop = threading.Event() if stop is None else stop

    @property
    def notifier_wired(self) -> bool:
        """通知出口装配了没有。见模块文档「缺通知出口时的姿态」。"""

        return self._notifier is not None

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    # 一轮
    # ------------------------------------------------------------------

    def run_once(self) -> StalledProvisioningReport | None:
        """跑一轮。返回 ``None`` 表示本轮没有执行（停止中）。"""

        if self._stop.is_set():
            return None

        if self._notifier is None:
            # **绝不"改状态不告知"**：缺通知出口时本轮一条候选都不处理，只记一次
            # 恰好一条的审计（见模块文档「缺通知出口时的姿态」）。
            self._audit.record("stalled_provisioning.notifier_not_wired")
            return StalledProvisioningReport(notifier_wired=False)

        tally = _Tally()
        interrupted = self._sweep(tally)
        report = tally.freeze(interrupted=interrupted, notifier_wired=True)
        if tally.examined or tally.skipped_in_backoff:
            # 零候选**且**零退避跳过时不记审计（外部独立审查 F4 同一条纪律）：本职责
            # 每轮都跑，健康系统里绝大多数 tick 什么都不该做，无条件记审计只会刷出
            # 海量空事实。**必须把 `skipped_in_backoff` 也算进这个门槛**（N-2 修复）：
            # 只看 `examined` 的话，一轮取到的候选恰好全部在退避期时会静默不记审计，
            # 运维读到的"这一轮没有停摆候选"其实是"有人被跳过了"，两件事不能同一个
            # 沉默表达。
            self._audit.record("stalled_provisioning.completed", **report.audit_facts())
            logger.info(
                "开通中途停摆收口完成 候选=%s 已通知=%s 已收口=%s 通知失败=%s "
                "推进被拒=%s 失败=%s 退避跳过=%s",
                report.examined,
                report.notified,
                report.aborted,
                report.notify_failed,
                report.advance_refused,
                report.failed,
                report.skipped_in_backoff,
            )
        if report.aborted > 0 and self._alert is not None:
            # 停摆计数送达管理群（Issue #280 §7.3 步 2）：只在本轮真的收口了至少
            # 一个候选时上报，只传聚合计数——见模块文档「停摆计数送达管理群」。
            try:
                self._alert(report.aborted)
            except Exception as error:  # noqa: BLE001 - 告警失败不得带走已完成的收口
                self._audit.record(
                    "stalled_provisioning.alert_callback_failed",
                    error=type(error).__name__,
                )
        return report

    def _sweep(self, tally: _Tally) -> bool:
        """取到期候选并逐个处置。返回本轮是否被停止信号中断。

        **两段式**（外部独立审查 P2-2 修复）：候选查询按 :data:`STALLED_FETCH_OVERSAMPLE`
        过采样，取回的候选先在进程内按每用户通知退避过滤，再从中挑最多 ``self._limit``
        个**真正处理**（发通知、可能收口）——过采样是为了不让排在最前面、正处于退避期
        的候选把 ``limit`` 名额占满，饿死后面真正等待处理的新候选（同型见模块文档）。
        """

        fetch_limit = min(self._limit * STALLED_FETCH_OVERSAMPLE, _MAX_FETCH_LIMIT)
        candidates = self._candidates.stalled_provisioning_candidates(
            lease_seconds=self._lease_seconds, limit=fetch_limit
        )
        now = self._clock()
        processed = 0
        for item in candidates:
            if processed >= self._limit:
                # 本轮真正处理的名额已经用完：过采样只是为了绕开退避期的毒候选，
                # 不是要把 `limit` 本身也放宽。
                break
            if self._stop.is_set():
                # 停止信号落在遍历中间：不再为后面的人处置。已经处理过的各自是完整的
                # 一步（通知、或通知+收口），下一次启动会从库里的候选查询原样继续。
                return True
            last_attempt = self._last_attempt.get(item.user_id)
            if last_attempt is not None and now - last_attempt < self._notify_backoff_seconds:
                # 退避期内：这个人最近刚被尝试过（无论成败），跳过——不占用本轮的
                # 处理名额，也不打一次多余的飞书。**必须计数**（N-2 修复）：不然一轮
                # 取到的候选全部在退避期时，`tally.examined` 会是 0，运维读审计会
                # 误以为这一轮没有任何停摆候选。
                tally.skipped_in_backoff += 1
                continue
            self._last_attempt[item.user_id] = now
            tally.examined += 1
            processed += 1
            try:
                self._process_one(item, tally)
            except Exception as error:  # noqa: BLE001 - 一个用户的失败不得带走整轮
                tally.failed += 1
                self._audit.record(
                    "stalled_provisioning.user_failed",
                    user=item.user_id,
                    error=type(error).__name__,
                )
                logger.error(
                    "单个用户的停摆收口失败，其余用户继续 user=%s error=%s",
                    item.user_id,
                    type(error).__name__,
                )
        return False

    def _process_one(self, item: Any, tally: _Tally) -> None:
        """处置一个候选：**先通知，通知送达才收口**（见模块文档「处置顺序」）。"""

        dedupe_key = f"onboarding:stalled:{item.event_id}"
        try:
            self._notifier.send(
                open_id=item.open_id,
                key=KEY_STALLED,
                values={"reference": item.trace_id},
                dedupe_key=dedupe_key,
            )
        except Exception as error:  # noqa: BLE001 - 通知失败：留在原状态，下一轮重来
            tally.notify_failed += 1
            self._audit.record(
                "stalled_provisioning.notify_failed",
                user=item.user_id,
                error=type(error).__name__,
            )
            return
        tally.notified += 1

        aborted = self._aborter.abort_stalled_provisioning(
            user_id=item.user_id,
            expected_states=_EXPECTED_STATES,
            reason="stalled_lease_expired",
        )
        if not aborted:
            # CAS 0 行：状态在候选查到与这里之间被别的路径改写（被另一条并发链推进
            # 到 active、被停用、或已经被本职责上一轮收口过）。**不重发**——通知已经
            # 发出去了，下一轮候选查询会按当前的真实状态重新判断。这个人此后也不会
            # 再是本查询的候选，进程内的退避记录留着不清理（下一次真正停摆的人
            # 是新的 user_id，不会撞上这条陈旧记录；同一 user_id 罕见地再次停摆时
            # 多等一段退避窗口不构成正确性问题）。
            tally.advance_refused += 1
            self._audit.record(
                "stalled_provisioning.advance_refused", user=item.user_id
            )
            return
        tally.aborted += 1
        self._audit.record(
            "stalled_provisioning.aborted",
            user=item.user_id,
            state=item.provisioning_state,
            reason="stalled_lease_expired",
            trace_id=item.trace_id,
        )


def _build_stalled_provisioning_duty(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
    alert: Callable[[int], None] | None = None,
) -> Any:
    """装配开通中途停摆收口职责（Issue #282，`V-开通-19`）。**总能注册**，不需要任何
    可选前置——候选查询、收口写入（复用
    :meth:`~lingxi.adapters.postgres_identity.PostgresAppUserStore.
    abort_stalled_provisioning`）与通知都只需要 ``LINGXI_POSTGRES_DSN``/飞书应用
    凭据，两者都是 :class:`SchedulerConfig` 的必填项，形状照
    :func:`_build_late_readiness_recovery_duty`。

    补的是 :mod:`lingxi.core.identity.onboarding_runner` 模块文档「同一类缺口的另一半」
    一节登记的缺口：首次开通链在把用户推进到 ``provisioning`` 之后死掉、且编排自己的
    「当场收口」也够不到（进程被强杀、收口写入自己那一次恰好失败），此前没有任何东西
    会再回来看这个人。语义、放在哪、节奏怎么定见
    :mod:`lingxi.apps.scheduler.stalled_provisioning` 的模块文档；本函数只做装配。

    装配断言 5（本轮新增）：停摆租约必须严格长于一条链在 provisioning/mcp_syncing
    两格上可能停留的最长时间——见
    :func:`~lingxi.apps.scheduler.onboarding.assert_stalled_lease_exceeds_chain_budget`。
    这里只是拿一份 :class:`ReadinessSchedule` 来核对预算数字，**不需要真的装配探针**
    （本职责本身也不发探针，与迟到就绪恢复不同），因此这条断言在探针端点是否配置好之前
    就能跑。
    """

    from lingxi.adapters.feishu_user_message import FeishuUserMessages
    from lingxi.adapters.postgres_identity import PostgresAppUserStore
    from lingxi.adapters.postgres_stalled_provisioning import PostgresStalledProvisioningStore
    from lingxi.apps.scheduler.onboarding import (
        CatalogNotifier,
        assert_stalled_lease_exceeds_chain_budget,
    )
    from lingxi.config.content import default_content_catalog

    dsn = config.postgres_dsn
    timeouts = config.postgres_timeouts

    schedule = ReadinessSchedule(probe_timeout_seconds=config.query_mcp_timeout_seconds)
    assert_stalled_lease_exceeds_chain_budget(
        lease_seconds=DEFAULT_STALLED_LEASE_SECONDS,
        publish_wait_seconds=config.onboarding_publish_wait_seconds,
        schedule=schedule,
    )

    duty = StalledProvisioningDuty(
        candidates=PostgresStalledProvisioningStore(dsn, timeouts=timeouts),
        aborter=PostgresAppUserStore(dsn, timeouts=timeouts),
        notifier=CatalogNotifier(
            sender=FeishuUserMessages(
                base_url=config.feishu_base_url,
                app_id=config.feishu_app_id,
                app_secret=config.feishu_app_secret,
            ),
            catalog=default_content_catalog(),
        ),
        alert=alert,
        audit=audit,
        lease_seconds=DEFAULT_STALLED_LEASE_SECONDS,
        stop=stop,
    )
    logger.info(
        "开通中途停摆收口职责已装配 租约=%ss", DEFAULT_STALLED_LEASE_SECONDS
    )
    return duty


__all__ = [
    "DEFAULT_NOTIFY_RETRY_BACKOFF_SECONDS",
    "DEFAULT_STALLED_LEASE_SECONDS",
    "DEFAULT_STALLED_LIMIT",
    "STALLED_FETCH_OVERSAMPLE",
    "StalledProvisioningDuty",
    "StalledProvisioningReport",
]
