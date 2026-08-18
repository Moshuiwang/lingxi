"""权限发布消费与就绪确认职责（Issue [#156](https://github.com/Moshuiwang/lingxi/issues/156) 的 S-C-03b）。

这是 Epic C 从"一组各自可用的组件"变成"一条会自己转的链"的那一处接线。一轮 tick 做四件
事，次序不可调换：

1. **收殓**：把崩溃留下的 ``publishing`` 意图放回 ``pending``
   （:meth:`~lingxi.adapters.postgres_permission_publish.PostgresPermissionPublishStore.
   reclaim_stale`）。发布本身幂等（先查后写），因此重入安全；不收殓的话，一次进程崩溃会
   把那个用户的后续发布一直堵着。
2. **发布**：驱动 :class:`~lingxi.core.permission.publish.PermissionPublishExecutor`
   消费待发布意图（写发布表 → 逐字段读回核对）。S-C-01 交付了这个执行器，但在本 Story
   之前**它没有任何生产调用方**。
3. **就绪确认**：对"已经发布读回一致、确认还没收口"的每一条，按 tick 节奏推进**至多
   一步**（:class:`~lingxi.core.permission.mcp_readiness.ReadinessTicker`）。
4. **通知**：探针成功 → 发一条「范围已更新」；权限为空（撤权）→ 在读回一致后直接发
   一条「暂无可用范围」。

## 为什么与「每日权限重算」是两个职责

重算（:mod:`lingxi.apps.scheduler.permission_refresh`）是**每天一次的决定面**，靠一个
当日水位保证同日至多一轮；发布消费是**每轮都要跑的消费面**。合并成一个职责，那个当日
水位就会把每分钟的发布消费也一起关掉——当天第一轮之后，任何新排进来的意图都要等到第二
天才会被消费。两个职责共用同一个停止标志与同一个进程（**单一写入负责人**：发布执行、
就绪探针、通知都在 ``lingxi-scheduler`` 这一个进程里），因此并发边界仍然只有 outbox 认领
那一道。

装配顺序上本职责排在重算**之后**：同一轮里重算先把当天的意图排进来，发布消费紧接着就能
把它推出去，而不是白等一个调度周期。

## 就绪确认为什么必须是 tick 形态

S-C-02 的 :class:`~lingxi.core.permission.mcp_readiness.McpReadinessConfirmation` 是**阻塞**
引擎（注入 ``sleep``，立即 / 每 180 秒 / 900 秒预算）。把它直接放进本职责的 ``run_once``，
一个还没就绪的用户就会把整个 :class:`~lingxi.apps.scheduler.SchedulerLoop` 占住十五分钟
——凭据轮换、保留清理、空闲会话清理全部停摆。因此本职责用同一模块里的
:class:`~lingxi.core.permission.mcp_readiness.ReadinessTicker`：**只替换"等待"的实现**，
判定、五路取值、绑定、落库与审计全部复用同一份代码，节奏也仍然是同一张计划表。
"这一条下一次什么时候到期"由 ``mcp_sync_check`` 里已有的记录重建
（:class:`~lingxi.core.permission.mcp_readiness.ReadinessProgress`），**不新增表、不新增
进程内状态**，因此重启不丢任何一条确认。

## 通知只发一次，靠的是就绪记录的终态

同一 ``(用户, 权限版本)`` 走到终态（``ready`` / ``no_permission`` / ``timed_out``）之后，
:meth:`~lingxi.adapters.postgres_permission_publish.PostgresPermissionPublishStore.
published_awaiting_readiness` 的 SQL 就不再把它取回来。因此"这条变化的通知发过了"这个
事实没有第二个载体，也不需要第二张表。

次序也是刻意的：**先把终态记录落库，再发通知**。反过来（先发再记）会让一次记账失败变成
"用户已经收到通知、系统却认为还没处理完"，下一轮再发一条。通知失败则**不回头改任何状态**
——产品负责人 2026-08-18 裁定 4：通知失败记审计与计数，不阻塞权限生效。

## 三条边界

- **超时不发通知**。``timed_out`` 表示我们没能确认这个人真的可以问数，此时说一句"你的
  可用范围已更新"就是一句我们没验证过的话。它落审计与计数（转运维的编排属 Epic D）。
- **收件人不可用不发通知**：``provisioning_state`` 不是 ``active``、账号正在删除或已删除
  的人一律跳过并计数（判据在 :meth:`~lingxi.adapters.postgres_permission_publish.
  PostgresPermissionPublishStore.notice_recipient_open_id` 的 SQL 里）。
- **就绪面缺配置时整面不装配，发布面照常**（见 :func:`lingxi.apps.scheduler.
  _build_permission_publish_duty`）。发布不依赖探针；而一个装了却每轮都炸的探针，会把
  "还没接线"伪装成"接线了但一直失败"。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from lingxi.core.permission.mcp_readiness import (
    ReadinessAttempt,
    ReadinessBinding,
    ReadinessOutcome,
    ReadinessProgress,
)
from lingxi.core.permission.notification import NoticeResult

logger = logging.getLogger(__name__)

_UTC = timezone.utc

#: 单轮发布预算：一轮最多消费多少条待发布意图。挡的是"一轮把整张表刷完"占住外部接口
#: 配额；剩下的下一轮继续（同 :meth:`PermissionPublishExecutor.run_once` 的 ``limit``）。
DEFAULT_PUBLISH_LIMIT = 50

#: 单轮就绪预算：一轮最多推进多少条待确认。同样是配额保护，不是重试上限——"最多探几次"
#: 由就绪计划表决定，与这个数无关。
DEFAULT_READINESS_LIMIT = 50


class _PublishExecutor(Protocol):
    def run_once(self, *, limit: int = ...) -> Sequence[Any]: ...


class _IntentStore(Protocol):
    """发布意图与收件人的读取口（``adapters/postgres_permission_publish.py``）。"""

    def reclaim_stale(self, *, older_than: timedelta = ...) -> int: ...
    def published_awaiting_readiness(self, *, limit: int = ...) -> Sequence[Any]: ...
    def notice_recipient_open_id(self, user_id: str) -> str | None: ...


class _ReadinessChecks(Protocol):
    """就绪判定记录的回读口（``adapters/postgres_mcp_token.py``）。"""

    def load_checks(self, user_id: str, permission_version: int) -> Sequence[Any]: ...


class _Ticker(Protocol):
    def advance(
        self, binding: ReadinessBinding, *, permissions: str, progress: ReadinessProgress
    ) -> ReadinessAttempt | None: ...


class _Notices(Protocol):
    def notify(
        self, *, user_id: str, open_id: str, permission_version: int, permissions: str
    ) -> NoticeResult: ...


class _AuditSink(Protocol):
    def record(self, action: str, /, **fields: object) -> None: ...


@dataclass(frozen=True)
class ReadinessFollowUp:
    """就绪确认与通知这一整面。**要么三件齐全，要么整面不装配。**

    做成一个必须整体注入的对象，而不是三个各自可空的参数：三者少任何一个，这一面都只能
    做半件事——能探不能通知、能通知却说不出该发给谁。半装配的形态在日志里看起来一切正常，
    却在用户侧表现为"权限生效了但从来没人告诉我"。
    """

    ticker: _Ticker
    checks: _ReadinessChecks
    notices: _Notices


@dataclass(frozen=True)
class PermissionPublishReport:
    """一轮的结果。**只有计数与固定分类，没有任何字段值。**"""

    reclaimed: int = 0
    attempts: int = 0
    published: int = 0
    pending_readiness: int = 0
    advanced: int = 0
    ready: int = 0
    revoked: int = 0
    timed_out: int = 0
    notices_sent: int = 0
    notices_failed: int = 0
    notices_skipped: int = 0
    failed: int = 0
    interrupted: bool = False
    readiness_wired: bool = True

    def audit_facts(self) -> dict[str, Any]:
        facts: dict[str, Any] = {
            "reclaimed": self.reclaimed,
            "attempts": self.attempts,
            "published": self.published,
            "pending_readiness": self.pending_readiness,
            "advanced": self.advanced,
            "ready": self.ready,
            "revoked": self.revoked,
            "timed_out": self.timed_out,
            "notices_sent": self.notices_sent,
            "notices_failed": self.notices_failed,
            "notices_skipped": self.notices_skipped,
            "failed": self.failed,
            "readiness_wired": self.readiness_wired,
        }
        if self.interrupted:
            facts["interrupted"] = True
        return facts


@dataclass
class _Tally:
    """累加器。:class:`PermissionPublishReport` 是冻结的（它会进审计）。"""

    reclaimed: int = 0
    attempts: int = 0
    published: int = 0
    pending_readiness: int = 0
    advanced: int = 0
    ready: int = 0
    revoked: int = 0
    timed_out: int = 0
    notices_sent: int = 0
    notices_failed: int = 0
    notices_skipped: int = 0
    failed: int = 0

    def freeze(self, *, interrupted: bool, readiness_wired: bool) -> PermissionPublishReport:
        return PermissionPublishReport(
            reclaimed=self.reclaimed,
            attempts=self.attempts,
            published=self.published,
            pending_readiness=self.pending_readiness,
            advanced=self.advanced,
            ready=self.ready,
            revoked=self.revoked,
            timed_out=self.timed_out,
            notices_sent=self.notices_sent,
            notices_failed=self.notices_failed,
            notices_skipped=self.notices_skipped,
            failed=self.failed,
            interrupted=interrupted,
            readiness_wired=readiness_wired,
        )


class PermissionPublishDuty:
    """每轮 tick：收殓 → 发布 → 推进就绪确认 → 该通知就通知。

    语义与边界见模块文档。本类**只编排**：发布判定在
    :mod:`lingxi.core.permission.publish`，就绪判定在
    :mod:`lingxi.core.permission.mcp_readiness`，通知正文与重试在
    :mod:`lingxi.core.permission.notification`，这里一条规则都不复制。
    """

    name = "权限发布与就绪确认"

    def __init__(
        self,
        *,
        executor: _PublishExecutor,
        intents: _IntentStore,
        audit: _AuditSink,
        readiness: ReadinessFollowUp | None = None,
        publish_limit: int = DEFAULT_PUBLISH_LIMIT,
        readiness_limit: int = DEFAULT_READINESS_LIMIT,
        clock: Callable[[], datetime] | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        for label, value in (("发布", publish_limit), ("就绪", readiness_limit)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"单轮{label}预算必须是正整数")
        if readiness is not None and not isinstance(readiness, ReadinessFollowUp):
            raise TypeError("就绪面必须整体注入 ReadinessFollowUp，不接受半套装配")
        self._executor = executor
        self._intents = intents
        self._audit = audit
        self._readiness = readiness
        self._publish_limit = publish_limit
        self._readiness_limit = readiness_limit
        self._clock = clock or (lambda: datetime.now(_UTC))
        # 与同一进程内的其他职责共享停止标志：SIGTERM 一次让所有职责停止领取新工作。
        self._stop = threading.Event() if stop is None else stop

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    @property
    def readiness_wired(self) -> bool:
        """就绪与通知这一面装配了没有。装配层缺配置时它是 ``False``，发布面照常。"""

        return self._readiness is not None

    def request_stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    # 一轮
    # ------------------------------------------------------------------

    def run_once(self) -> PermissionPublishReport | None:
        """跑一轮。返回 ``None`` 表示本轮没有执行（停止中）。

        **本方法不会等待**：一条还没就绪的确认只是被推进一步或原样留着，绝不 ``sleep``。
        整轮耗时的上界因此是"外部调用次数 × 单次超时"，与十五分钟的就绪预算无关。
        """

        if self._stop.is_set():
            # 已经在停止中：一条意图都不领，一次探针都不发。
            return None

        tally = _Tally()
        tally.reclaimed = self._intents.reclaim_stale()
        attempts = tuple(self._executor.run_once(limit=self._publish_limit))
        tally.attempts = len(attempts)
        tally.published = sum(1 for attempt in attempts if getattr(attempt, "published", False))

        interrupted = False
        if self._readiness is not None and not self._stop.is_set():
            pending = tuple(
                self._intents.published_awaiting_readiness(limit=self._readiness_limit)
            )
            tally.pending_readiness = len(pending)
            for item in pending:
                if self._stop.is_set():
                    # 停止信号落在遍历中间：不再为后面的人发探针或发通知。已经落库的
                    # 判定各自是一个完整事务；下一次启动会从库里把进度原样读回来。
                    interrupted = True
                    break
                try:
                    self._advance(item, tally)
                except Exception as error:  # noqa: BLE001 - 一个人的失败不得带走整轮
                    # 只记异常类型：异常正文可能带上被处理对象的内容。
                    tally.failed += 1
                    self._audit.record(
                        "permission_publish.user_failed",
                        user=item.user_id,
                        permission_version=item.permission_version,
                        error=type(error).__name__,
                    )
                    logger.error(
                        "单个用户的就绪确认失败，其余用户继续 user=%s error=%s",
                        item.user_id,
                        type(error).__name__,
                    )

        report = tally.freeze(interrupted=interrupted, readiness_wired=self.readiness_wired)
        self._audit.record("permission_publish.completed", **report.audit_facts())
        if report.advanced or report.attempts or report.reclaimed:
            # 摘要只有计数。一轮什么都没做时不打日志：这条职责每分钟跑一次。
            logger.info(
                "权限发布与就绪确认完成 收殓=%s 发布尝试=%s 发布完成=%s 待确认=%s "
                "本轮推进=%s 就绪=%s 撤权=%s 超时=%s 通知成功=%s 通知失败=%s 未通知=%s 失败=%s",
                report.reclaimed,
                report.attempts,
                report.published,
                report.pending_readiness,
                report.advanced,
                report.ready,
                report.revoked,
                report.timed_out,
                report.notices_sent,
                report.notices_failed,
                report.notices_skipped,
                report.failed,
            )
        return report

    # ------------------------------------------------------------------
    # 单条待确认
    # ------------------------------------------------------------------

    def _advance(self, item: Any, tally: _Tally) -> None:
        """把一条确认推进至多一步，并在它收口时决定要不要通知。"""

        assert self._readiness is not None  # 由 run_once 的分支保证
        binding = ReadinessBinding(
            user_id=item.user_id, permission_version=item.permission_version
        )
        progress = ReadinessProgress.from_checks(
            self._readiness.checks.load_checks(item.user_id, item.permission_version)
        )
        attempt = self._readiness.ticker.advance(
            binding, permissions=item.permissions, progress=progress
        )
        if attempt is None:
            # 还没到期，或者已经收口：本轮一次外部调用都不发。
            return
        tally.advanced += 1
        if attempt.outcome is ReadinessOutcome.READY:
            tally.ready += 1
            self._notify(item, tally)
            return
        if attempt.outcome is ReadinessOutcome.NO_PERMISSION:
            # 撤权那一路：没有可等的就绪，发布读回一致本身就是通知的触发点
            # （产品负责人 2026-08-18 裁定 1）。
            tally.revoked += 1
            self._notify(item, tally)
            return
        if attempt.outcome is ReadinessOutcome.TIMED_OUT:
            # **不通知**：我们没能确认这个人真的可以问数（模块文档「三条边界」）。
            tally.timed_out += 1

    def _notify(self, item: Any, tally: _Tally) -> None:
        """发一条权限变化通知。**这一步失败不回头改任何状态。**

        通知的种类由 :func:`lingxi.core.permission.notification.render_scope_notice`
        从 ``permissions`` 文本自己判定，与就绪结论用的是**同一个**存在性判据
        （``lookup_metrics(document)``）。因此"探针成功却发出一条撤权通知"这种自相矛盾
        的组合在结构上不可能出现，不需要在这里再判一次。
        """

        open_id = self._intents.notice_recipient_open_id(item.user_id)
        if not open_id:
            # 还没开通完、已停用或正在删除的账号：不发，只计数与留痕。
            tally.notices_skipped += 1
            self._audit.record(
                "permission_notice.recipient_unavailable",
                user=item.user_id,
                permission_version=item.permission_version,
            )
            return
        result = self._readiness.notices.notify(  # type: ignore[union-attr]
            user_id=item.user_id,
            open_id=open_id,
            permission_version=item.permission_version,
            permissions=item.permissions,
        )
        if result.delivered:
            tally.notices_sent += 1
        else:
            tally.notices_failed += 1


__all__ = [
    "DEFAULT_PUBLISH_LIMIT",
    "DEFAULT_READINESS_LIMIT",
    "PermissionPublishDuty",
    "PermissionPublishReport",
    "ReadinessFollowUp",
]
