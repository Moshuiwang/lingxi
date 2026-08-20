"""迟到就绪恢复职责：十五分钟同步超时之后仍然把用户捞回来确认（[V-开通-18](../../../../docs/技术设计/验收矩阵.md)）。

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

节奏靠 :meth:`~lingxi.adapters.postgres_permission_publish.
PostgresPermissionPublishStore.late_onboarding_recovery_candidates` 的
``recovery_interval_seconds`` 参数在 SQL 里判"到期了没有"——与
:meth:`~lingxi.adapters.postgres_permission_publish.PostgresPermissionPublishStore.
published_awaiting_readiness` 的既有做法同一个形状，因此本职责可以放心地跟其余职责
共用 :class:`~lingxi.apps.scheduler.loop.SchedulerLoop` 同一个（远比十五分钟短的）
tick 周期：候选查询自己会把没到期的人挡在外面，一轮什么都不到期时只是一次空查询。

## 要试到什么时候为止：**未做产品决定，工程上先做成"不设期限"**

「一个人卡在 ``mcp_syncing`` 多久之后应该彻底放弃、转成需要人工介入的终态」是一个
**产品决定**（例如"三天还没好就转运维，给用户一句不同的话"），编排者与实施者都没有权限
替产品负责人定这个数字——定错方向是"允许一个本来快好了的人被过早放弃"，也可能是"允许
一个真的坏掉的人被无限期地空转"。**本 Story 因此不实现任何放弃期限**：只要这个人还停在
``mcp_syncing``、账号还启用，恢复职责就会按上面的节奏一直回来看，直到就绪或账号被停用
为止（后者会让候选查询的 ``account_state = 'enabled'`` 判据自然把他排除，见该方法文档）。

这个选择的**含义**，如实登记：

1. 一个"真的坏掉"的用户（例如问数 MCP 一侧的权限记录出于本方未知的原因永远不会同步）
   会被无限期地每十五分钟探一次，直到账号被管理员停用为止——这既是探针配额的持续消耗，
   也意味着运维**不会**从本职责这里收到任何"这个人可能需要人工介入"的主动信号（唯一
   的信号仍是原始那一轮 ``timed_out`` 留下的一条 ``onboarding.sync_timeout`` 审计与
   用户收到的冻结文案，本职责不会重复发它，也不会升级它）。
2. 一旦产品负责人给出"多久算彻底放弃"的裁定，落地点是在
   :meth:`late_onboarding_recovery_candidates` 的候选查询上再加一条时间上界过滤
   （或在本职责里做），并需要决定"放弃之后给用户什么终态提示"——那句提示同样需要产品
   负责人逐字批准，不在本 Story 的范围内新造。

## 通知：复用既有的文案与投递，不新造幂等载体

文案是既有的 ``onboarding.completed``（逐字不改，`AGENTS.md` 与本任务的硬性要求），
渲染与发送复用 :class:`~lingxi.apps.scheduler.onboarding.CatalogNotifier`——与
``AutoOnboardingRunner`` 走的是同一条内容目录、同一个发送端口
（:class:`~lingxi.adapters.feishu_user_message.FeishuUserMessages`），只是去重键不同
（本职责没有原始的 ``event_id`` 可用，用 ``(用户, 权限版本)`` 派生一个稳定键，形状与
:func:`~lingxi.core.permission.notification.notice_dedupe_key` 同源）。

**"恰一次"靠的是次序，不是新的幂等表**：先把 ``provisioning_state`` 推进到 ``active``
（:meth:`~lingxi.adapters.postgres_identity.PostgresAppUserStore.
advance_provisioning_state` 的既有条件更新，只前进不回退），再发通知——与
``AutoOnboardingRunner._confirm`` 和 ``PermissionPublishDuty._advance``/``_notify``
是同一条已经在两处验证过的次序（先落终态记录，再发通知）。一旦状态推进成功，这个人
**从此不再是候选**（候选查询要求 ``provisioning_state = 'mcp_syncing'``），因此同一个
人不可能被通知第二次。**残余的窗口，如实登记**：状态推进成功之后、通知真正送达之前
进程崩溃，这条通知就永久丢了——与 ``permission_publish.py`` 的 ``_notify`` 同一条已经
被产品负责人接受的残留（2026-08-18 裁定 4：有限重试 + 审计，不是 exactly-once），
未做通知 outbox 的理由也相同：为一条只会发生一次的告知型消息再建一张表，代价与收益不
成比例。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from lingxi.core.identity.onboarding_runner import KEY_COMPLETED, STATE_ACTIVE
from lingxi.core.permission.mcp_readiness import (
    ReadinessAttempt,
    ReadinessBinding,
    ReadinessOutcome,
)
from lingxi.core.permission.notification import describe_scope
from lingxi.core.permission.publish_row import parse_permissions

logger = logging.getLogger(__name__)

#: 复检节奏（秒）。见模块文档「节奏」一节：与问数 MCP 自己拉取发布表的周期同一个数量级，
#: 是本职责自己的工程选择，不是产品裁定。
DEFAULT_RECOVERY_INTERVAL_SECONDS = 900

#: 单轮最多处理多少个候选。配额保护，不是重试上限——被推迟到下一轮的候选不丢失任何
#: 进度（进度全在 ``mcp_sync_check`` / ``app_user`` 里）。
DEFAULT_RECOVERY_LIMIT = 50

#: 「开通完成」通知的重试次数。形状与
#: ``core/identity/onboarding_runner.AutoOnboardingRunner._notify`` 一致：同一次通知
#: 不该因为一次飞书抖动就再也发不出去，也不该无限重试。
DEFAULT_NOTIFY_ATTEMPTS = 3


class _Candidates(Protocol):
    """恢复候选的读取口（``adapters/postgres_permission_publish.py``）。"""

    def late_onboarding_recovery_candidates(
        self, *, reason: str, recovery_interval_seconds: int, limit: int = ...
    ) -> Sequence[Any]: ...


class _Ticker(Protocol):
    """就绪复检探针（``core/permission/mcp_readiness.ReadinessRecoveryTicker``）。

    **可选**：缺问数 MCP 端点或令牌主密钥时装配层传 ``None``，本职责仍然可以完成
    ``already_ready`` 那一路（上一轮已经探到就绪、只是没来得及推进 + 通知的候选，见
    :meth:`_Candidates.late_onboarding_recovery_candidates` 的文档），需要真探针的
    那一路本轮不推进、不落任何记录（模块文档「节奏」一节同一条纪律）。
    """

    def probe_after_timeout(
        self, binding: ReadinessBinding, *, attempt_no: int
    ) -> ReadinessAttempt | None: ...


class _StateStore(Protocol):
    """``app_user.provisioning_state`` 的条件推进（``adapters/postgres_identity.py``）。"""

    def advance_provisioning_state(self, user_id: str, *, to: str) -> bool: ...


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

    examined: int = 0
    #: 探到（或已经探到过）就绪的候选数——``ready`` + ``already_ready`` 合计。
    ready: int = 0
    #: 真正被推进到 ``active`` 的人数。可能小于 ``ready``（账号在这期间被停用）。
    activated: int = 0
    notified: int = 0
    notice_failed: int = 0
    notice_skipped: int = 0
    advance_refused: int = 0
    waiting: int = 0
    technical_failures: int = 0
    #: 探针未接线、需要真探针才能推进的候选数（见 :class:`_Ticker` 的文档）。
    probe_unwired: int = 0
    failed: int = 0
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
            "notified": self.notified,
            "notice_failed": self.notice_failed,
            "notice_skipped": self.notice_skipped,
            "advance_refused": self.advance_refused,
            "waiting": self.waiting,
            "technical_failures": self.technical_failures,
            "probe_unwired": self.probe_unwired,
            "failed": self.failed,
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
    notified: int = 0
    notice_failed: int = 0
    notice_skipped: int = 0
    advance_refused: int = 0
    waiting: int = 0
    technical_failures: int = 0
    probe_unwired: int = 0
    failed: int = 0

    def freeze(self, *, interrupted: bool, probe_wired: bool) -> LateReadinessRecoveryReport:
        return LateReadinessRecoveryReport(
            examined=self.examined,
            ready=self.ready,
            activated=self.activated,
            notified=self.notified,
            notice_failed=self.notice_failed,
            notice_skipped=self.notice_skipped,
            advance_refused=self.advance_refused,
            waiting=self.waiting,
            technical_failures=self.technical_failures,
            probe_unwired=self.probe_unwired,
            failed=self.failed,
            interrupted=interrupted,
            probe_wired=probe_wired,
        )


class LateReadinessRecoveryDuty:
    """每轮 tick：取到期候选 → 按需再探一次 → 就绪就推进 ``active`` 并通知「开通完成」。

    语义与边界见模块文档。本类**只编排**：候选查询在
    :mod:`lingxi.adapters.postgres_permission_publish`，判定在
    :mod:`lingxi.core.permission.mcp_readiness`，状态推进在
    :mod:`lingxi.adapters.postgres_identity`，通知正文在
    :mod:`lingxi.config.content`，这里一条规则都不复制。
    """

    name = "迟到就绪恢复"

    def __init__(
        self,
        *,
        candidates: _Candidates,
        users: _StateStore,
        recipients: _Recipients,
        notifier: _Notifier,
        audit: _AuditSink,
        sleep: Callable[[float], None],
        reason: str,
        ticker: _Ticker | None = None,
        recovery_interval_seconds: int = DEFAULT_RECOVERY_INTERVAL_SECONDS,
        limit: int = DEFAULT_RECOVERY_LIMIT,
        notify_attempts: int = DEFAULT_NOTIFY_ATTEMPTS,
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
        if not callable(sleep):
            # 与 ``AutoOnboardingRunner``/``PermissionNoticeDispatcher`` 同一条理由：
            # 缺省会让通知重试的退避在生产里静默消失。
            raise TypeError("sleep 必须可调用：缺省会让通知重试的退避静默消失")
        if (
            isinstance(notify_attempts, bool)
            or not isinstance(notify_attempts, int)
            or notify_attempts < 1
        ):
            raise ValueError("通知至少要尝试一次")
        self._candidates = candidates
        self._ticker = ticker
        self._users = users
        self._recipients = recipients
        self._notifier = notifier
        self._audit = audit
        self._sleep = sleep
        self._reason = reason.strip()
        self._interval = recovery_interval_seconds
        self._limit = limit
        self._notify_attempts = notify_attempts
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

        **本方法不会等待**：探针面是 tick 驱动，一次探针最长的等待就是它自己的传输
        超时；候选查询已经把没到期的人挡在外面，一轮什么都不到期时只是一次空查询。
        """

        if self._stop.is_set():
            return None

        tally = _Tally()
        interrupted = False
        candidates = self._candidates.late_onboarding_recovery_candidates(
            reason=self._reason,
            recovery_interval_seconds=self._interval,
            limit=self._limit,
        )
        for item in candidates:
            if self._stop.is_set():
                # 停止信号落在遍历中间：不再为后面的人探针或通知。已经落库的判定各自
                # 是一个完整事务；下一次启动会从库里把进度原样读回来。
                interrupted = True
                break
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

        report = tally.freeze(interrupted=interrupted, probe_wired=self.probe_wired)
        self._audit.record("late_readiness_recovery.completed", **report.audit_facts())
        if report.examined:
            # 摘要只有计数，一轮没有候选时不打日志（本职责每轮都跑）。
            logger.info(
                "迟到就绪恢复完成 候选=%s 就绪=%s 已推进=%s 已通知=%s 等待=%s "
                "技术失败=%s 探针未接线=%s 推进被拒=%s 通知失败=%s 未通知=%s 失败=%s",
                report.examined,
                report.ready,
                report.activated,
                report.notified,
                report.waiting,
                report.technical_failures,
                report.probe_unwired,
                report.advance_refused,
                report.notice_failed,
                report.notice_skipped,
                report.failed,
            )
        return report

    # ------------------------------------------------------------------
    # 单个候选
    # ------------------------------------------------------------------

    def _recover_one(self, item: Any, tally: _Tally) -> None:
        """把一个候选推进至多一步：（可能）再探一次 → 就绪就推进 ``active`` 并通知。

        **不得把还没就绪的人写成 ``active``，也不得给他任何暗示已经可用的消息**——
        这是 V-开通-18 断言的后半句，因此 ``waiting`` / ``technical_failure`` /
        探针未接线三路都在这里**显式返回**，不落到任何会继续往下推进的默认分支。
        """

        binding = ReadinessBinding(
            user_id=item.user_id, permission_version=item.permission_version
        )
        if item.already_ready:
            # 上一轮已经探到 ready，只是没来得及推进 + 通知（进程崩溃）。不再烧一次
            # 探针配额去问一个已经问过的问题——见 ``late_onboarding_recovery_
            # candidates`` 文档「already_ready」一节。
            tally.ready += 1
        else:
            if self._ticker is None:
                # 探针未接线：本轮不落任何记录，端点配好后从库里的进度原样继续。
                tally.probe_unwired += 1
                return
            attempt = self._ticker.probe_after_timeout(
                binding, attempt_no=item.next_attempt_no
            )
            if attempt is None:
                tally.probe_unwired += 1
                return
            if attempt.outcome is ReadinessOutcome.WAITING:
                tally.waiting += 1
                return
            if attempt.outcome is not ReadinessOutcome.READY:
                # ``technical_failure``：探针没跑通，任何数字都是假的，绝不能凑成
                # 就绪。计划表已经不存在，本方法没有第三条可能的结论。
                tally.technical_failures += 1
                return
            tally.ready += 1

        # **只有到这里才推进 active**：先落状态、再通知（模块文档「通知」一节，
        # 与 ``AutoOnboardingRunner._confirm``/``PermissionPublishDuty._advance``
        # 同一条次序）。条件更新自带「账号必须仍然启用」的守卫（`V-开通-04`），
        # 因此不需要在这里再单独复核一次账号状态。
        advanced = self._users.advance_provisioning_state(item.user_id, to=STATE_ACTIVE)
        if not advanced:
            # 推进被拒：这个人在候选查到与这里之间被停用，或已经被别的路径推进过。
            # **不发通知**——advance_refused 不是终态，下一轮候选查询会按当时的
            # 真实状态重新判断（账号仍停用则不再是候选，被别的路径推进过则同样
            # 不再是候选）。
            tally.advance_refused += 1
            self._audit.record(
                "late_readiness_recovery.advance_refused", user=item.user_id
            )
            return
        tally.activated += 1

        open_id = self._recipients.notice_recipient_open_id(item.user_id)
        if not open_id:
            # 推进成功之后账号又被停用/删除，或收件人字段本身缺失：不发，只留痕。
            tally.notice_skipped += 1
            self._audit.record(
                "late_readiness_recovery.recipient_unavailable", user=item.user_id
            )
            return

        company, function = describe_scope(parse_permissions(item.permissions))
        # 去重键：本职责没有原始 event_id 可用，改用 (用户, 权限版本) 派生一个稳定键
        # ——同一个人的同一版权限只会走到这里一次（推进成功之后就不再是候选），
        # 形状与 ``core/permission/notification.notice_dedupe_key`` 同源。
        dedupe_key = f"onboarding:recovery:{item.user_id}:{item.permission_version}"
        if self._notify_completed(
            open_id=open_id,
            company=company,
            function=function,
            dedupe_key=dedupe_key,
            user_id=item.user_id,
        ):
            tally.notified += 1
        else:
            tally.notice_failed += 1

    def _notify_completed(
        self, *, open_id: str, company: str, function: str, dedupe_key: str, user_id: str
    ) -> bool:
        """发一条「开通完成」，**有限重试**，返回是否送达。

        形状与 ``AutoOnboardingRunner._notify`` 一致：重试之间用注入的 ``sleep``，
        因此纯单测里一秒都不用等；每一次失败都留一条可分辨的审计。
        """

        for attempt_no in range(1, self._notify_attempts + 1):
            try:
                self._notifier.send(
                    open_id=open_id,
                    key=KEY_COMPLETED,
                    values={"company_name": company, "function_name": function},
                    dedupe_key=dedupe_key,
                )
                return True
            except Exception as error:  # noqa: BLE001
                self._audit.record(
                    "late_readiness_recovery.notify_failed",
                    user=user_id,
                    attempt=attempt_no,
                    error=type(error).__name__,
                )
                logger.warning(
                    "「开通完成」通知发送失败 user=%s 第%s次 error=%s",
                    user_id,
                    attempt_no,
                    type(error).__name__,
                )
                if attempt_no < self._notify_attempts:
                    self._sleep(float(attempt_no))
        return False


__all__ = [
    "DEFAULT_NOTIFY_ATTEMPTS",
    "DEFAULT_RECOVERY_INTERVAL_SECONDS",
    "DEFAULT_RECOVERY_LIMIT",
    "LateReadinessRecoveryDuty",
    "LateReadinessRecoveryReport",
]
