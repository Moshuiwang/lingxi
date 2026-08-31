"""迟到就绪恢复职责：十五分钟同步超时之后仍然把用户捞回来确认（[V-开通-18](../../../../docs/技术设计/验收矩阵-开通与身份.md)）。

## 补的是哪个洞

首次开通那条链（:class:`~lingxi.core.identity.onboarding_runner.AutoOnboardingRunner`）
把「等发布读回一致后当前用户 MCP 就绪」这件事交给阻塞式的
:class:`~lingxi.core.permission.mcp_readiness.McpReadinessConfirmation`：立即探一次，
之后每三分钟一次，十五分钟预算耗尽仍未成功就返回 ``timed_out``，编排收到它之后**当场
返回**——``provisioning_state`` 留在 ``mcp_syncing``，用户收到冻结文案「权限同步未完成，
已转交处理」。``onboarding_runner`` 的模块文档自己承认了这条缺口：「半开的用户会一直停在
``mcp_syncing`` 而没有任何人会来收拾」。

但十五分钟只是**我们承诺给用户的等待上限**，不是这个人的权限真的同步好了的上限——问数
MCP 按自己的节奏（约十五分钟一次）拉取发布表，一次因为时机不巧错过窗口的用户，权限可能
在超时判定之后几分钟内就真的生效了。验收矩阵 ``V-开通-18`` 的断言因此是：**周期性回来看
那些停在 ``mcp_syncing``、已经超时的用户，重新确认就绪；一旦就绪就写 ``active`` 并主动
通知他「开通完成」；在此之前绝不能给他任何暗示已经可用的消息**。本模块就是这条恢复路径。

## 放在哪：一个新职责，不是塞进既有职责

三个候选位置都想过，选新职责的理由：

- **不塞进 ``core/identity/onboarding_runner.py``**：那是一次**同步链**的编排，一条链
  从「收到首聊」跑到「终态」，最长阻塞十七分钟（发布等待 + 就绪预算）后就必须返回——
  它天然不适合表达"过一阵子再回来看一眼"这种**跨越多次调用、状态全部落库**的语义。
  硬塞进去要么让编排复活一条已经收口的链（违反"一旦选定终态，后置异常不得改写"这条
  `V-开通-13`），要么给编排加一个自己不睡眠的第二模式，两者都会让这个文件同时承担
  两种时间尺度的职责。
- **不塞进 ``apps/scheduler/permission_publish.py``**（S-C-03b 的刷新链就绪确认）：
  该职责的候选查询 :meth:`~lingxi.adapters.postgres_permission_publish.
  PostgresPermissionPublishStore.published_awaiting_readiness` **刻意排除**
  ``first_onboarding`` 这个 reason——两边都捞的话，一个刚开通的用户会在「开通完成」
  之外再收到一条措辞完全不同的「可用范围已更新」，且两个确认可能对同一个
  ``(用户, 权限版本)`` 并发探针。那条排除是产品语义的一部分，不是可以随手打开的开关；
  把首次开通的恢复塞进去，等于在同一个职责里同时维护"首次开通只能我来确认"与"这里也
  确认首次开通"两句互相矛盾的话。
- **新职责**：与花名册审计、每日权限重算、权限发布消费、组织快照同步、首次开通编排
  同一个"职责"量级——按 :mod:`lingxi.apps.scheduler` 既有形状（缺前置就不注册或半装配、
  报告只含计数、审计只含固定原因码）落地，天然复用 :class:`~lingxi.apps.scheduler.
  loop.SchedulerLoop` 的隔离与停止语义，不需要给任何既有职责增加第二套时间模型。

## 判定层怎么复用：一个新的 ticker 子类，不是新判据

硬性要求是复用 :mod:`lingxi.core.permission.mcp_readiness` 的探针与判定，不新造一套
"就绪"的定义。问题是**既有的两种「怎么等」形态都不适用**：

- :class:`~lingxi.core.permission.mcp_readiness.McpReadinessConfirmation` 是阻塞式，
  真的会 ``sleep``——不能塞进 tick。
- :class:`~lingxi.core.permission.mcp_readiness.ReadinessTicker` 是 tick 驱动，但它的
  ``advance`` 一旦从 ``mcp_sync_check`` 回读出 ``ReadinessProgress.terminal`` 为真
  （``ready`` / ``no_permission`` / ``timed_out`` 任意一种）就**永远不再推进**——这条
  "终态之后不再动"的防线对刷新链是对的（通知只发一次靠它），但 ``timed_out`` 对首次
  开通链恰恰不是"处理完了"，是"这一轮没等到，可能下一轮就等到了"。直接复用会让每一个
  超时用户永远卡死，与不修复没有区别。

因此 :mod:`lingxi.core.permission.mcp_readiness` 新增了第三个 ``_ReadinessProbeRunner``
子类 :class:`~lingxi.core.permission.mcp_readiness.ReadinessRecoveryTicker`：**分类
（``classify_probe``）、探针调用、（用户, 权限版本）绑定、落库与审计与另外两个形态是
同一份实现**，只是不认 ``ReadinessProgress`` 的终态防线，也不再判断"这个人有没有可等
的权限"（原始那一轮已经判过，走到 ``timed_out`` 就已经证明过一次）。**判据一个字都没有
新造**，新增的只是"什么时候该再探一次、要不要退出"这个编排层的问题，而这正是本模块
存在的理由。

**候选一律重新探一次，不存在"跳过探针直接判就绪"的分支**（外部独立审查 F2 修复；此前的
``already_ready`` 快捷路径被 ``bool_or(result='ready')`` 命中任意一次历史 ``ready``，
包括那次 ``ready`` 之后账号被停用、CAS 拒绝、账号又被重新启用的情形——这条路径会让一个
"曾经就绪过、但当前这一刻我们没有任何新鲜证据"的人被写成 ``active``，正是红线「绝不把
没就绪的人写成 active」要挡的东西）。删掉这条快捷路径之后，多付出的代价只是"已知就绪的
人偶尔会被多探一次"，换来的是**每一次写 active 之前都有一次当次真实成功的探针**，没有
例外。

## 节奏：十五分钟一次

每次尝试都是一次真实的问数 MCP 调用，不是免费的。合同承诺的三分钟/十五分钟节奏（
:data:`~lingxi.core.permission.mcp_readiness.DEFAULT_INTERVAL_SECONDS` /
:data:`~lingxi.core.permission.mcp_readiness.DEFAULT_BUDGET_SECONDS`）是"首次开通正在
等待时"的节奏，本职责服务的是那个窗口**已经关闭之后**的恢复，没有理由用同样紧凑的节奏
去追——查得更勤不会让结果更快出现（问数 MCP 自己按大约十五分钟一次的周期拉取发布表，
见 :func:`~lingxi.core.permission.mcp_readiness.classify_probe` 的文档），只会白白多
烧探针配额。:data:`DEFAULT_RECOVERY_INTERVAL_SECONDS` 因此取十五分钟——与 MCP 自己的
拉取周期同一个数量级：查得比这更勤没有收益，查得慢很多则会让"权限其实已经同步好了"与
"用户真的被告知"之间的等待被不必要地拉长。**这是本职责自己的工程选择，不是产品裁定**，
如果产品负责人认为应该更快或更慢，改这一个常量的值即可，不影响任何判定语义。

节奏靠 :meth:`~lingxi.adapters.postgres_late_readiness_recovery.
PostgresLateReadinessStore.late_onboarding_recovery_candidates` 的
``recovery_interval_seconds`` 参数在 SQL 里判"到期了没有"——与
:meth:`~lingxi.adapters.postgres_permission_publish.PostgresPermissionPublishStore.
published_awaiting_readiness` 的既有做法同一个形状，因此本职责可以放心地跟其余职责
共用 :class:`~lingxi.apps.scheduler.loop.SchedulerLoop` 同一个（远比十五分钟短的）
tick 周期：候选查询自己会把没到期的人挡在外面，一轮什么都不到期时只是一次空查询。

**"毒候选"不会占死窗口**（外部独立审查 F4 修复）：候选到期判据看的是
``mcp_sync_check`` 里这个人最后一次判定的时刻。探针本身（无论成功与否）总会先写一行，
因此探针**之后**的步骤（推进 ``active``、排通知）如果抛未预期异常，这一次窗口已经不是
"什么都没记下"——但为了不依赖这条隐式保证，探针**之后**的每一步失败都会显式调用
:meth:`~lingxi.core.permission.mcp_readiness.ReadinessRecoveryTicker.
record_processing_failure` 记一条技术失败，占住这一次调度窗口，与真实探针失败走的是
同一张表、同一条到期判据，不会让同一个人在下一个 tick 立刻被重新选中、饿死排在后面的
正常候选。

## 要试到什么时候为止：重试受 publish_outbox 内容快照的九十天保留期约束，不是真的无限期

「一个人卡在 ``mcp_syncing`` 多久之后应该彻底放弃、转成需要人工介入的终态」是一个
**产品决定**（例如"三天还没好就转运维，给用户一句不同的话"），编排者与实施者都没有权限
替产品负责人定这个数字。**本 Story 不实现任何主动放弃期限**：只要这个人还停在
``mcp_syncing``、账号还启用，恢复职责的探针 + 推进这一半就会按上面的节奏一直回来看。

**但"探针 + 推进"这一半不是唯一受时间约束的环节**——外部独立审查 F5 指出，此前「无限期
重试」的措辞与实现不一致：候选查询依赖 ``publish_outbox.payload`` 里的 ``permissions``
文本渲染"公司/职能"展示值，而那份快照会在九十天后被
:meth:`~lingxi.adapters.postgres_permission_publish.PostgresPermissionPublishStore.
redact_expired_payloads` 擦成 ``'{}'``；擦除之后 ``o.payload ? 'permissions'`` 恒假，
这个人从此**永久退出候选集**——不是因为产品裁定了一个期限，是因为承载"当时发布的范围是
什么"这份内容快照本身有一个既有的、与本 Story 无关的到期擦除路径（迁移 ``0064``）。
**如实改写措辞**：探针/推进这一半没有主动施加的上限，但**实际生效的重试窗口受
``publish_outbox`` 内容快照的九十天保留期约束**——超过这个窗口，候选查询会静默把这个人
排除在外，不是因为他被"放弃"了，是因为渲染通知所需的内容已经不在了。

这个约束是**既有的**，不是本 Story 新引入的行为，也不是本 Story 替产品负责人定的期限；
只是此前的措辞把它描述成"无限期"，与真实实现不符。一旦产品负责人给出"多久算彻底放弃"
的正式裁定，那条裁定几乎必然**短于**九十天，届时会直接取代这条隐式约束（在候选查询上
再加一条主动的时间上界过滤），本条也就自然失效，不需要额外改动来"解除"它。

## 通知：状态推进与排通知同一个事务，通知本身是持久 outbox（外部独立审查 F1 修复）

**"active 但永远不告知"与"永远不 active"，对用户是同一种失败**——他仍然收不到「开通
完成」那句话。此前的实现先推进 ``provisioning_state``、再在同一次调用里同步尝试发送
通知：CAS 之后进程崩溃、收件人查询异常、渲染异常、飞书发送三次都失败，任一种都会让
这个人从候选集永久消失、却从未真正被告知。

修法是把"推进 ``active``"与"排一条待发通知"放进**同一个数据库事务**
（:meth:`~lingxi.adapters.postgres_late_readiness_recovery.PostgresLateReadinessStore.
activate_after_late_readiness`）——此后两件事要么一起成立、要么一起不成立，不存在
"状态推进了但通知没排出"的中间态。通知本身消费自一张独立的 outbox
（``onboarding_completion_notice``，迁移 ``0066``），形状照 ``publish_outbox``：
认领即记账（:meth:`~lingxi.adapters.postgres_late_readiness_recovery.
PostgresLateReadinessStore.claim_one_due_notice` 在认领的同一条 ``UPDATE`` 里把
``attempts`` +1、把下一次到期时间前移），发送失败只留错误码、**不重新计算退避**（认领
时已经算好），发送成功标记 ``delivered``。**通知因此会被持久重试直到真正送达为止**，
不再依赖"候选查询把这个人捞回来"这条链路——一旦状态推进成功，候选查询确实不会再选中
他（``provisioning_state`` 不再是 ``mcp_syncing``），但通知 outbox 是**独立**的重试
来源，与候选集脱钩。

文案仍然是既有的 ``onboarding.completed``（逐字不改），渲染与发送复用
:class:`~lingxi.apps.scheduler.onboarding.CatalogNotifier`，去重键用
``(用户, 权限版本)`` 派生一个稳定值（本职责没有原始 ``event_id`` 可用），既写进
``onboarding_completion_notice.dedupe_key`` 这个数据库唯一约束，也传给
``CatalogNotifier.send`` 折成飞书投递的去重键。**这两层去重挡的不是同一件事**：
数据库的 ``UNIQUE`` 约束挡的是"同一个事件被排出两条通知行"（:meth:`~lingxi.adapters.
postgres_late_readiness_recovery.PostgresLateReadinessStore.
activate_after_late_readiness` 反复调用也只会有一条 ``pending`` 行）；**用户会不会
真的看到两条消息，挡的是飞书那一层**——同一条通知行的多次发送尝试（正常重试、或下面
这条残余窗口触发的重发）传的是同一个 ``dedupe_key``。

**残余的窗口，如实登记**：通知**已经真的发送成功**、但 :meth:`mark_notice_delivered`
的写入在那之前崩溃——这条通知会在下一次到期时被重新认领并**用同一个 dedupe_key 再发
一次**。用户会不会因此看到两条「开通完成」，取决于飞书 ``im/v1/messages`` 接口收到
同一个 ``uuid`` 时是否真的去重——**这是本仓库未验证的平台行为**（与
:mod:`lingxi.adapters.feishu_user_message` 模块文档已经登记的边界同一条：「重试携带
同一个 uuid」是代码事实，「飞书因此一定不会重复投递」未验证，属 L4a；同一条件下
:func:`~lingxi.adapters.feishu_group_message.delivery_uuid` 也没有说明飞书那一侧的
去重窗口有多长）。本模块**不为这条残余窗口新增任何补偿逻辑**（不做真正的两阶段提交式
exactly-once，不在应用层再实现一次幂等）——这是"至少一次"投递的既有产品裁定
（2026-08-18 裁定 4：通知失败有限重试、不阻塞权限生效），如实登记为已知边界，不是
遗漏。

## 已知边界（明确接受，不修，外部独立审查 A1）

**候选查询与通知 outbox 的认领都没有带过期时间的持久租约，并发两轮会重复探针 /
重复认领同一条通知。** 判据：本仓库当前是**单实例假设**（Epic C 冻结审查已作为知情
接受登记，组织快照同步 #250 同一口径）。``FOR UPDATE SKIP LOCKED`` 已经挡住"两个消费者
同时抢到同一行"，状态推进的 CAS 也已经挡住"重复写 active / 重复通知"——单实例下并发
两轮唯一可能浪费的是重复的问数 MCP 探针调用（配额浪费，不是正确性问题）。引入带过期
时间的持久租约是为多实例场景设计的，**引入即扩大范围**，与 #250 那次同一条判据保持
一致，账目不能自相矛盾。

**什么条件下必须回来处理**：``lingxi-scheduler`` 转为多实例部署，或滚动发布出现新旧
两个实例同时运行的重叠窗口，或问数 MCP 的调用配额本身成为硬约束（当前不是）。任一条
成立时，两处 ``FOR UPDATE SKIP LOCKED`` 语句都需要补上带心跳的租约。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from lingxi.core.identity.onboarding_runner import KEY_COMPLETED
from lingxi.core.permission.mcp_readiness import (
    ReadinessAttempt,
    ReadinessBinding,
    ReadinessOutcome,
    ReadinessSchedule,
)
from lingxi.core.permission.notification import describe_scope
from lingxi.core.permission.publish_row import parse_permissions

from lingxi.apps.scheduler.audit import AuditSink
from lingxi.apps.scheduler.config import SchedulerConfig

logger = logging.getLogger(__name__)

#: 复检节奏（秒）。见模块文档「节奏」一节：与问数 MCP 自己拉取发布表的周期同一个数量级，
#: 是本职责自己的工程选择，不是产品裁定。
DEFAULT_RECOVERY_INTERVAL_SECONDS = 900

#: 单轮最多处理多少个恢复候选。配额保护，不是重试上限——被推迟到下一轮的候选不丢失任何
#: 进度（进度全在 ``mcp_sync_check`` / ``app_user`` 里）。
DEFAULT_RECOVERY_LIMIT = 50

#: 单轮最多认领并发送多少条待发通知。与恢复候选的预算分开算——两者是不同的外部调用
#: （问数 MCP 探针 vs. 飞书私聊发送），互不挤占对方的配额。
DEFAULT_NOTICE_DRAIN_LIMIT = 50


class _Candidates(Protocol):
    """恢复候选的读取口（``adapters/postgres_late_readiness_recovery.py``）。"""

    def late_onboarding_recovery_candidates(
        self, *, reason: str, recovery_interval_seconds: int, limit: int = ...
    ) -> Sequence[Any]: ...


class _Ticker(Protocol):
    """就绪复检探针（``core/permission/mcp_readiness.ReadinessRecoveryTicker``）。

    **可选**：缺问数 MCP 端点或令牌主密钥时装配层传 ``None``，需要真探针的候选本轮
    不推进、不落任何记录（模块文档「节奏」一节同一条纪律）。
    """

    def probe_after_timeout(
        self, binding: ReadinessBinding, *, attempt_no: int
    ) -> ReadinessAttempt | None: ...

    def record_processing_failure(
        self, binding: ReadinessBinding, *, attempt_no: int, code: str
    ) -> ReadinessAttempt: ...


class _Activator(Protocol):
    """就绪之后「推进 active + 排通知」的原子写口
    （``adapters/postgres_late_readiness_recovery.PostgresLateReadinessStore.
    activate_after_late_readiness``，外部独立审查 F1/F3）。"""

    def activate_after_late_readiness(
        self,
        *,
        user_id: str,
        expected_permission_version: int,
        company_name: str,
        function_name: str,
        dedupe_key: str,
    ) -> bool: ...


class _NoticeOutbox(Protocol):
    """待发「开通完成」通知的持久 outbox（同一适配器）。

    **只有两个终点**：送达（:meth:`mark_notice_delivered`）或留在 ``pending`` 按既有
    退避重试（:meth:`mark_notice_failed`，即使原因是"收件人暂时查不到"也一样——见
    :meth:`mark_notice_failed` 的文档，外部独立审查第三轮 G1）。**没有"永久放弃"这
    个动作**：真正的账号删除由 ``user_id`` 上的 ``ON DELETE CASCADE`` 处理，不需要
    调用方自己判断"这是不是不可逆"——``notice_recipient_open_id`` 的返回值分辨不出。
    """

    def claim_one_due_notice(self) -> Any | None: ...
    def mark_notice_delivered(self, notice_id: str) -> None: ...
    def mark_notice_failed(self, notice_id: str, *, error: str) -> None: ...


class _Recipients(Protocol):
    """通知收件人查询（``adapters/postgres_permission_publish.py``）。"""

    def notice_recipient_open_id(self, user_id: str) -> str | None: ...


class _Notifier(Protocol):
    """终态的主动私聊（``apps/scheduler/onboarding.CatalogNotifier``）。"""

    def send(
        self, *, open_id: str, key: str, values: Mapping[str, object], dedupe_key: str
    ) -> None: ...


class _AuditSink(Protocol):
    def record(self, action: str, /, **fields: object) -> None: ...


@dataclass(frozen=True)
class LateReadinessRecoveryReport:
    """一轮的结果。**只有计数与固定分类，没有任何字段值**（同 ``PermissionPublishReport``
    的纪律：内部用户标识、权限值、open_id 一个都不进报告）。"""

    #: 本轮取到的恢复候选数（探针面）。
    examined: int = 0
    #: 本轮探到就绪的候选数。
    ready: int = 0
    #: 真正被推进到 ``active``（且成功排出待发通知）的人数。可能小于 ``ready``
    #: （账号在这期间被停用，或权限版本已经变了，CAS 拒绝）。
    activated: int = 0
    advance_refused: int = 0
    waiting: int = 0
    technical_failures: int = 0
    #: 探针未接线、需要真探针才能推进的候选数（见 :class:`_Ticker` 的文档）。
    probe_unwired: int = 0
    failed: int = 0
    #: 本轮从通知 outbox 认领到的待发条数（通知面，与探针面的预算各自独立）。
    notices_claimed: int = 0
    notified: int = 0
    notice_failed: int = 0
    #: 收件人暂时查不到的次数（外部独立审查第三轮 G1：**不是**"永久放弃"的计数——
    #: 这些通知仍然留在 ``pending`` 按既有退避重试，这里只是一个可观测的分类，
    #: 不代表它们已经收口）。
    notice_recipient_unavailable: int = 0
    interrupted: bool = False
    #: 探针面装配了没有。缺问数 MCP 端点或令牌主密钥时是 ``False``——本职责仍然注册，
    #: 只是需要真探针的那一路本轮不推进，报告里必须看得出来，否则"候选一直没有进展"
    #: 读起来会像卡死。
    probe_wired: bool = True

    def audit_facts(self) -> dict[str, Any]:
        facts: dict[str, Any] = {
            "examined": self.examined,
            "ready": self.ready,
            "activated": self.activated,
            "advance_refused": self.advance_refused,
            "waiting": self.waiting,
            "technical_failures": self.technical_failures,
            "probe_unwired": self.probe_unwired,
            "failed": self.failed,
            "notices_claimed": self.notices_claimed,
            "notified": self.notified,
            "notice_failed": self.notice_failed,
            "notice_recipient_unavailable": self.notice_recipient_unavailable,
            "probe_wired": self.probe_wired,
        }
        if self.interrupted:
            facts["interrupted"] = True
        return facts


@dataclass
class _Tally:
    """累加器。:class:`LateReadinessRecoveryReport` 是冻结的（它会进审计）。"""

    examined: int = 0
    ready: int = 0
    activated: int = 0
    advance_refused: int = 0
    waiting: int = 0
    technical_failures: int = 0
    probe_unwired: int = 0
    failed: int = 0
    notices_claimed: int = 0
    notified: int = 0
    notice_failed: int = 0
    notice_recipient_unavailable: int = 0

    def freeze(self, *, interrupted: bool, probe_wired: bool) -> LateReadinessRecoveryReport:
        return LateReadinessRecoveryReport(
            examined=self.examined,
            ready=self.ready,
            activated=self.activated,
            advance_refused=self.advance_refused,
            waiting=self.waiting,
            technical_failures=self.technical_failures,
            probe_unwired=self.probe_unwired,
            failed=self.failed,
            notices_claimed=self.notices_claimed,
            notified=self.notified,
            notice_failed=self.notice_failed,
            notice_recipient_unavailable=self.notice_recipient_unavailable,
            interrupted=interrupted,
            probe_wired=probe_wired,
        )

    @property
    def anything_happened(self) -> bool:
        """本轮有没有任何值得记一条完成审计的事——**零候选、零待发通知**时不记
        （外部独立审查 F4：本职责每轮都跑，不去重的话会在健康系统里刷出大量空审计）。"""

        return bool(self.examined or self.notices_claimed)


class LateReadinessRecoveryDuty:
    """每轮 tick 分两个互相独立的阶段：

    1. **探针阶段**：取到期候选 → 按需再探一次 → 就绪就原子地推进 ``active`` + 排一条
       待发通知（:class:`_Activator`）。
    2. **通知阶段**：认领到期的待发通知 → 发送 → 送达就标记 ``delivered``，失败留错误码
       等下一次到期重试。

    两个阶段的失败互不影响：探针阶段的候选处理失败不影响通知阶段照常跑，反之亦然。

    语义与边界见模块文档。本类**只编排**：候选查询、原子推进、通知 outbox 在
    :mod:`lingxi.adapters.postgres_late_readiness_recovery`，判定在
    :mod:`lingxi.core.permission.mcp_readiness`，通知正文在
    :mod:`lingxi.config.content`，这里一条规则都不复制。
    """

    name = "迟到就绪恢复"

    def __init__(
        self,
        *,
        candidates: _Candidates,
        activator: _Activator,
        notices: _NoticeOutbox,
        recipients: _Recipients,
        notifier: _Notifier,
        audit: _AuditSink,
        reason: str,
        ticker: _Ticker | None = None,
        recovery_interval_seconds: int = DEFAULT_RECOVERY_INTERVAL_SECONDS,
        limit: int = DEFAULT_RECOVERY_LIMIT,
        notice_limit: int = DEFAULT_NOTICE_DRAIN_LIMIT,
        stop: threading.Event | None = None,
    ) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("必须指明本职责负责恢复哪一类发布意图")
        if (
            isinstance(recovery_interval_seconds, bool)
            or not isinstance(recovery_interval_seconds, int)
            or recovery_interval_seconds < 1
        ):
            raise ValueError("复检节奏必须是正整数秒")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("单轮候选上限必须是正整数")
        if isinstance(notice_limit, bool) or not isinstance(notice_limit, int) or notice_limit < 1:
            raise ValueError("单轮通知上限必须是正整数")
        self._candidates = candidates
        self._ticker = ticker
        self._activator = activator
        self._notices = notices
        self._recipients = recipients
        self._notifier = notifier
        self._audit = audit
        self._reason = reason.strip()
        self._interval = recovery_interval_seconds
        self._limit = limit
        self._notice_limit = notice_limit
        self._stop = threading.Event() if stop is None else stop

    @property
    def probe_wired(self) -> bool:
        """探针面装配了没有。见 :class:`_Ticker` 的文档。"""

        return self._ticker is not None

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    # 一轮
    # ------------------------------------------------------------------

    def run_once(self) -> LateReadinessRecoveryReport | None:
        """跑一轮。返回 ``None`` 表示本轮没有执行（停止中）。

        **本方法不会等待**：探针面与通知面都是 tick 驱动，单次外部调用最长的等待是
        它自己的传输超时；候选查询与通知认领都已经把没到期的挡在外面，一轮什么都不
        到期时只是两次空查询。
        """

        if self._stop.is_set():
            return None

        tally = _Tally()
        interrupted = self._advance_candidates(tally)
        if not interrupted and not self._stop.is_set():
            interrupted = self._drain_notices(tally) or interrupted

        report = tally.freeze(interrupted=interrupted, probe_wired=self.probe_wired)
        if tally.anything_happened:
            # 零候选、零待发通知时不记审计（外部独立审查 F4）：本职责每轮都跑，健康
            # 系统里绝大多数 tick 什么都不该做，无条件记审计只会刷出海量空事实。
            self._audit.record("late_readiness_recovery.completed", **report.audit_facts())
            logger.info(
                "迟到就绪恢复完成 候选=%s 就绪=%s 已推进=%s 等待=%s 技术失败=%s "
                "探针未接线=%s 推进被拒=%s 失败=%s 已认领通知=%s 已送达=%s "
                "通知失败=%s 收件人暂不可用=%s",
                report.examined,
                report.ready,
                report.activated,
                report.waiting,
                report.technical_failures,
                report.probe_unwired,
                report.advance_refused,
                report.failed,
                report.notices_claimed,
                report.notified,
                report.notice_failed,
                report.notice_recipient_unavailable,
            )
        return report

    # ------------------------------------------------------------------
    # 探针阶段
    # ------------------------------------------------------------------

    def _advance_candidates(self, tally: _Tally) -> bool:
        """取到期候选并逐个推进。返回本轮是否被停止信号中断。"""

        candidates = self._candidates.late_onboarding_recovery_candidates(
            reason=self._reason,
            recovery_interval_seconds=self._interval,
            limit=self._limit,
        )
        for item in candidates:
            if self._stop.is_set():
                # 停止信号落在遍历中间：不再为后面的人探针。已经落库的判定各自是一个
                # 完整事务；下一次启动会从库里把进度原样读回来。
                return True
            tally.examined += 1
            try:
                self._recover_one(item, tally)
            except Exception as error:  # noqa: BLE001 - 一个用户的失败不得带走整轮
                # 只记异常类型：异常正文可能带上被处理对象的内容。
                tally.failed += 1
                self._audit.record(
                    "late_readiness_recovery.user_failed",
                    user=item.user_id,
                    error=type(error).__name__,
                )
                logger.error(
                    "单个用户的迟到就绪恢复失败，其余用户继续 user=%s error=%s",
                    item.user_id,
                    type(error).__name__,
                )
        return False

    def _recover_one(self, item: Any, tally: _Tally) -> None:
        """把一个候选推进至多一步：再探一次 → 就绪就原子推进 ``active`` + 排通知。

        **不得把还没就绪的人写成 ``active``，也不得给他任何暗示已经可用的消息**——
        这是 V-开通-18 断言的后半句，因此 ``waiting`` / ``technical_failure`` /
        探针未接线三路都在这里**显式返回**，不落到任何会继续往下推进的默认分支。

        **候选一律重新探一次**（外部独立审查 F2）：不存在"跳过探针"的分支。

        **F4 的窗口占位覆盖本方法的全程，不只是探针之后那一段**（外部独立审查第三轮
        G2：上一轮的实现只包住了"探针之后"的异常，``probe_after_timeout`` 本身的调用
        留在任何 ``try`` 之外——探针调用或探针落库抛出未预期异常时，外层只会记成
        ``user_failed``，``last_started_at`` 不前移，毒候选会在下一个 tick 立刻重入。
        因此这里对探针调用与探针之后的步骤**分别**加保护，用各自发起时刻真实的
        ``attempt_no``：探针那一次失败时占的是**当前**这次尝试的号（它本来就该占，
        只是没能真的探成）；探针之后的失败沿用原来的"下一个号"（探针那一次已经真的
        成功记过账了）。
        """

        binding = ReadinessBinding(
            user_id=item.user_id, permission_version=item.permission_version
        )
        if self._ticker is None:
            # 探针未接线：本轮不落任何记录，端点配好后从库里的进度原样继续。
            tally.probe_unwired += 1
            return
        try:
            attempt = self._ticker.probe_after_timeout(
                binding, attempt_no=item.next_attempt_no
            )
        except Exception as error:  # noqa: BLE001 - 占住窗口，再让外层记 failed 并上抛
            self._ticker.record_processing_failure(
                binding,
                attempt_no=item.next_attempt_no,
                code=f"recovery_failed_{type(error).__name__}",
            )
            raise
        if attempt is None:
            tally.probe_unwired += 1
            return
        if attempt.outcome is ReadinessOutcome.WAITING:
            tally.waiting += 1
            return
        if attempt.outcome is not ReadinessOutcome.READY:
            # ``technical_failure``：探针没跑通，任何数字都是假的，绝不能凑成就绪。
            # 计划表已经不存在，本方法没有第三条可能的结论。
            tally.technical_failures += 1
            return
        tally.ready += 1

        # **只有到这里才可能推进 active**——外部独立审查 F1/F3：状态推进与排通知在
        # 同一个原子事务里完成，且 CAS 要求 permission_version 与探针绑定的那一版
        # 完全一致，防止拿一版已经过时的 ready 判定把人写成 active。探针之后的步骤
        # 一旦抛出未预期异常，占住这一次调度窗口再上抛（F4：不让"毒候选"立刻被
        # 下一个 tick 重新选中）——探针那一次已经真的记过账了，这里用**下一个**
        # attempt_no。
        try:
            company, function = describe_scope(parse_permissions(item.permissions))
            dedupe_key = f"onboarding:recovery:{item.user_id}:{item.permission_version}"
            activated = self._activator.activate_after_late_readiness(
                user_id=item.user_id,
                expected_permission_version=item.permission_version,
                company_name=company,
                function_name=function,
                dedupe_key=dedupe_key,
            )
        except Exception as error:  # noqa: BLE001 - 占住窗口，再让外层记 failed 并上抛
            self._ticker.record_processing_failure(
                binding,
                attempt_no=item.next_attempt_no + 1,
                code=f"recovery_failed_{type(error).__name__}",
            )
            raise

        if not activated:
            # CAS 失败：这个人在候选查到与这里之间被停用、权限版本变了，或已经被
            # 别的路径推进过。**不发任何通知**——advance_refused 不是终态，下一轮
            # 候选查询会按当时的真实状态重新判断。
            tally.advance_refused += 1
            self._audit.record(
                "late_readiness_recovery.advance_refused", user=item.user_id
            )
            return
        tally.activated += 1
        # 通知已经在同一个事务里排进 outbox（or 已存在同 dedupe_key 的一条），
        # 发送由 _drain_notices 独立完成——这里不再做任何发送尝试。

    # ------------------------------------------------------------------
    # 通知阶段
    # ------------------------------------------------------------------

    def _drain_notices(self, tally: _Tally) -> bool:
        """认领并发送到期的待发通知。返回本轮是否被停止信号中断。"""

        for _ in range(self._notice_limit):
            if self._stop.is_set():
                return True
            notice = self._notices.claim_one_due_notice()
            if notice is None:
                # 没有到期的了：正常收工，不是中断。
                return False
            tally.notices_claimed += 1
            try:
                self._send_notice(notice, tally)
            except Exception as error:  # noqa: BLE001 - 一条通知的失败不得带走整轮
                tally.failed += 1
                self._audit.record(
                    "late_readiness_recovery.notice_processing_failed",
                    user=notice.user_id,
                    error=type(error).__name__,
                )
                logger.error(
                    "单条待发通知处理失败，其余通知继续 user=%s error=%s",
                    notice.user_id,
                    type(error).__name__,
                )
        return False

    def _send_notice(self, notice: Any, tally: _Tally) -> None:
        open_id = self._recipients.notice_recipient_open_id(notice.user_id)
        if not open_id:
            # 外部独立审查第三轮 G1：**不得**转永久放弃。``notice_recipient_open_id``
            # 只能回答"这一刻查不到"，答不出"这是不是永久性的"——账号被停用完全可能
            # 只是暂时的。提前判死会让一个已经 active 的人永远等不到这句话，原路
            # 复活 F1 要堵的洞。真正的"不用再等了"只有 `ON DELETE CASCADE` 一种
            # 事实来源，因此这里与发送失败走同一条路：留在 pending，按既有退避重试。
            self._notices.mark_notice_failed(
                notice.notice_id, error="recipient_unavailable"
            )
            tally.notice_recipient_unavailable += 1
            self._audit.record(
                "late_readiness_recovery.recipient_unavailable", user=notice.user_id
            )
            return
        try:
            self._notifier.send(
                open_id=open_id,
                key=KEY_COMPLETED,
                values={
                    "company_name": notice.company_name,
                    "function_name": notice.function_name,
                },
                dedupe_key=notice.dedupe_key,
            )
        except Exception as error:  # noqa: BLE001 - 记完错误码再留在 pending 等下次到期
            self._notices.mark_notice_failed(notice.notice_id, error=type(error).__name__)
            tally.notice_failed += 1
            self._audit.record(
                "late_readiness_recovery.notify_failed",
                user=notice.user_id,
                error=type(error).__name__,
            )
            logger.warning(
                "「开通完成」通知发送失败，将在下一次到期时重试 user=%s error=%s",
                notice.user_id,
                type(error).__name__,
            )
            return
        self._notices.mark_notice_delivered(notice.notice_id)
        tally.notified += 1


def _build_late_readiness_recovery_duty(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
) -> Any:
    """装配迟到就绪恢复职责（V-开通-18）。**不需要任何可选前置就能注册。**

    补的是 ``core/identity/onboarding_runner.py`` 模块文档「一条链失败中断时」一节
    登记的缺口：首次开通那次阻塞式就绪确认判超时之后，``provisioning_state`` 停在
    ``mcp_syncing``，此前没有任何东西会再回来看这个人。语义、放在哪、节奏怎么定、
    要试到什么时候为止，见 :mod:`lingxi.apps.scheduler.late_readiness_recovery` 的
    模块文档；本函数只做装配。

    形状照 :func:`_build_readiness_follow_up`，但比它宽松一格：候选查询、原子推进
    （:class:`~lingxi.adapters.postgres_late_readiness_recovery.
    PostgresLateReadinessStore`）与通知（:class:`~lingxi.apps.scheduler.onboarding.
    CatalogNotifier`）都只需要 ``LINGXI_POSTGRES_DSN``/飞书应用凭据——两者都是
    :class:`SchedulerConfig` 的必填项，因此本职责**总能**注册。**唯一可选的是探针面**：
    缺 MCP 令牌主密钥或问数 MCP 端点时，需要真探针才能推进的那一路本轮不推进，只把这
    **恰一条**审计记下来（:class:`~lingxi.apps.scheduler.late_readiness_recovery._Ticker`
    的文档）；通知面（认领已经排出的待发通知并重试直到送达）不依赖探针，照常运行。
    """

    from lingxi.adapters.feishu_user_message import FeishuUserMessages
    from lingxi.adapters.postgres_late_readiness_recovery import PostgresLateReadinessStore
    from lingxi.adapters.postgres_permission_publish import PostgresPermissionPublishStore
    from lingxi.apps.scheduler.onboarding import CatalogNotifier
    from lingxi.config.content import default_content_catalog
    from lingxi.core.identity.onboarding_runner import FIRST_ONBOARDING_REASON

    dsn = config.postgres_dsn
    timeouts = config.postgres_timeouts
    store = PostgresLateReadinessStore(dsn, timeouts=timeouts)
    # 通知收件人查询是既有的只读方法，住在权限发布那份存取里
    # （``notice_recipient_open_id``，与刷新链的变化通知共用同一条产品口径）。
    recipients = PostgresPermissionPublishStore(dsn, timeouts=timeouts)

    ticker = None
    if not config.mcp_token_encrypt_key:
        from lingxi.adapters.mcp_token_cipher import MASTER_KEY_ENV

        # 只报变量名，不回显任何值（`V-花名册-29` 的同一条纪律；它还是一把主密钥）。
        audit.record(
            "late_readiness_recovery.probe_not_wired",
            reason="missing_environment_variable",
            variable=MASTER_KEY_ENV,
        )
        logger.warning(
            "未配置 %s，迟到就绪恢复的探针面不装配；候选留在库里等配置齐备，"
            "已经排出但还没送达的通知照常持久重试",
            MASTER_KEY_ENV,
        )
    elif not config.query_mcp_endpoint:
        audit.record(
            "late_readiness_recovery.probe_not_wired",
            reason="missing_environment_variable",
            variable="LINGXI_QUERY_MCP_ENDPOINT",
        )
        logger.warning(
            "未配置 LINGXI_QUERY_MCP_ENDPOINT，迟到就绪恢复的探针面不装配；"
            "候选留在库里等端点配好，已经排出但还没送达的通知照常持久重试"
        )
    else:
        from lingxi.adapters.mcp_token_cipher import McpTokenCipher
        from lingxi.adapters.postgres_mcp_token import (
            PostgresMcpTokenStore,
            token_cipher_provider,
        )
        from lingxi.adapters.query_mcp_probe import QueryMcpProbe, content_text_metrics_reader
        from lingxi.apps.scheduler.onboarding import assert_probe_timeouts_agree
        from lingxi.core.permission.mcp_readiness import ReadinessRecoveryTicker

        tokens = PostgresMcpTokenStore(
            dsn, cipher=McpTokenCipher(config.mcp_token_encrypt_key), timeouts=timeouts
        )
        schedule = ReadinessSchedule(probe_timeout_seconds=config.query_mcp_timeout_seconds)
        probe = QueryMcpProbe(
            endpoint=config.query_mcp_endpoint,
            token_provider=token_cipher_provider(tokens),
            timeout_seconds=config.query_mcp_timeout_seconds,
            # 已验证的 reader（Issue #253 / L4a），同 ``_build_readiness_follow_up`` 与
            # 本文件修复过的 ``_build_onboarding_duty`` 那两份。
            metrics_reader=content_text_metrics_reader,
        )
        assert_probe_timeouts_agree(probe=probe, schedule=schedule)
        ticker = ReadinessRecoveryTicker(
            probe=probe,
            store=tokens,
            audit=audit,
            clock=lambda: datetime.now(timezone.utc),
            schedule=schedule,
        )

    duty = LateReadinessRecoveryDuty(
        candidates=store,
        ticker=ticker,
        activator=store,
        notices=store,
        recipients=recipients,
        notifier=CatalogNotifier(
            sender=FeishuUserMessages(
                base_url=config.feishu_base_url,
                app_id=config.feishu_app_id,
                app_secret=config.feishu_app_secret,
            ),
            catalog=default_content_catalog(),
        ),
        audit=audit,
        reason=FIRST_ONBOARDING_REASON,
        stop=stop,
    )
    logger.info(
        "迟到就绪恢复职责已装配 探针面=%s", "已接线" if ticker is not None else "未接线"
    )
    return duty


__all__ = [
    "DEFAULT_NOTICE_DRAIN_LIMIT",
    "DEFAULT_RECOVERY_INTERVAL_SECONDS",
    "DEFAULT_RECOVERY_LIMIT",
    "LateReadinessRecoveryDuty",
    "LateReadinessRecoveryReport",
]
