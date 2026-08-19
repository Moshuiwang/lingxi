"""到期数据清理职责：保留清理、空闲会话清理、权限链到期处置。

从 :mod:`lingxi.apps.scheduler`（#237 拆分）搬出，三个职责同组的理由——共用「每轮只
处理一批、不循环到清空」的纪律与幂等前提——见各类自己的文档字符串与包的
``__init__.py`` 模块文档。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from lingxi.apps.scheduler.audit import AuditSink

logger = logging.getLogger(__name__)


class _Cleaner(Protocol):
    def run_once(self) -> Any: ...


class RetentionCleanupDuty:
    """九十天保留清理职责：每轮调用一次受限清理函数。

    每轮**只调用一次**，不在职责内部循环到删空。理由有两条：一次调用就是一个
    数据库事务，单次调用因此天然没有半删状态；而"删空为止"会让一个积压了很多
    到期行的库在单轮里长时间持锁，也让 ``SIGTERM`` 的退出时间不再有上界。
    积压由下一轮继续，清理本来就是幂等的（断言 V-保留-10）。
    """

    name = "保留清理"

    def __init__(self, *, cleaner: _Cleaner, stop: threading.Event | None = None) -> None:
        self._cleaner = cleaner
        self._stop = threading.Event() if stop is None else stop

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        self._stop.set()

    def run_once(self) -> Any:
        """已经在停止中就一批都不领。返回 ``None`` 表示本轮未执行。"""

        if self._stop.is_set():
            return None
        report = self._cleaner.run_once()
        # 摘要只有表名与计数。清理函数的返回里根本没有行内容，日志因此不可能
        # 带出人员数据（断言 V-保留-14）。
        summary = getattr(report, "summary", None)
        rendered = summary() if callable(summary) else "保留清理：本轮完成"
        # 有表因为拿不到锁而让路时，这一轮**没有做完**，不能记成正常完成。
        # 两者的删除数都可能是 0，只有日志级别与标记能把它们分开：一张长期被占的表
        # 会在 INFO 流水里表现为一切正常，而内容一直没被回收——保留违规最不该有的
        # 形态就是它悄无声息（codex 二轮 P1-3）。
        blocked = getattr(report, "blocked_tables", ())
        if blocked:
            logger.warning("%s；本轮未清理完：%s 因锁等待超时让路，下一轮重试", rendered, "、".join(blocked))
        else:
            logger.info("%s", rendered)
        return report


class IdleConversationSweepDuty:
    """会话空闲满两小时的到点清理职责：每轮调用一次
    ``PostgresTaskQueue.sweep_idle_conversations``。

    2026-08-14 补充决定（数据库设计「问数结果投递事件与会话保留 Outbox」、
    `V-投递-10`）：会话空闲满两小时后，即使用户未再发起新的问数任务，已经送达
    的安全结果正文也必须由定时清理机制主动清除，不依赖下一次任务入队。#151 落地
    时只交付了应用层方法本身，没有接上任何生产调用方（内审 P2-2）；本职责补上
    这一条调用点，写法与 :class:`RetentionCleanupDuty` 同型——每轮只清一次，
    天然幂等，被打断只回滚这一批 ``UPDATE``，不留半清状态。
    """

    name = "空闲会话清理"

    def __init__(
        self, *, queue: Any, idle_after: timedelta, stop: threading.Event | None = None
    ) -> None:
        self._queue = queue
        self._idle_after = idle_after
        self._stop = threading.Event() if stop is None else stop

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        self._stop.set()

    def run_once(self) -> int | None:
        """已经在停止中就一批都不领。返回 ``None`` 表示本轮未执行，否则返回本轮
        清除了已送达正文的会话数（供日志/断言，不承载业务语义）。"""

        if self._stop.is_set():
            return None
        cleared = self._queue.sweep_idle_conversations(idle_after=self._idle_after)
        if cleared:
            logger.info("空闲会话清理：本轮清除 %s 个会话的已送达投递正文", cleared)
        return cleared


#: 会话空闲清理的固定窗口（产品合同「数据保留与删除」、`V-投递-10`）：不设配置，
#: 与 P2-1 修复同一取舍——业务常量不应该有一个能让它漂移的环境变量。
IDLE_CONVERSATION_SWEEP_AFTER = timedelta(hours=2)


class _ExpiredPayloadRedactor(Protocol):
    """``publish_outbox`` 到期内容擦除的最小端口。实现是
    :class:`lingxi.adapters.postgres_permission_publish.PostgresPermissionPublishStore`。"""

    def redact_expired_payloads(self) -> int: ...


class _ExpiredCheckPurger(Protocol):
    """``mcp_sync_check`` 到期整行删除的最小端口。实现是
    :class:`lingxi.adapters.postgres_mcp_token.PostgresMcpTokenStore`。"""

    def purge_expired_checks(self) -> int: ...


@dataclass(frozen=True)
class PermissionRetentionReport:
    """一轮权限链到期处置的摘要。**只有计数与接线状态，没有任何行内容**。

    与 :class:`~lingxi.adapters.retention.RetentionReport` 同一条纪律（断言
    ``V-保留-14``）：进日志与审计的只能是"擦了几条、删了几行"，两个适配器方法的返回值
    本来也只有条数。
    """

    #: 本轮被擦成 ``'{}'`` 的 ``publish_outbox.payload`` 条数。
    redacted: int = 0
    #: 本轮被删掉的 ``mcp_sync_check`` 行数。
    purged: int = 0
    #: 判定记录那一面这一轮有没有装配（缺 MCP 令牌主密钥时为 ``False``）。
    checks_wired: bool = True

    def audit_facts(self) -> dict[str, Any]:
        return {
            "redacted": self.redacted,
            "purged": self.purged,
            "checks_wired": self.checks_wired,
        }


class PermissionRetentionSweepDuty:
    """权限发布链两张表的九十天到期内容处置职责：每轮各调用一次。

    两张表的处置方式不同，因为它们**能擦的东西**不同：

    - ``publish_outbox.payload`` 里有邮箱与姓名 → 擦成 ``'{}'``（
      :meth:`~lingxi.adapters.postgres_permission_publish.PostgresPermissionPublishStore.
      redact_expired_payloads`）。行本身留着——``user_id``、权限版本、状态与时间戳是
      "谁的哪一版权限什么时候发布成功过"这类运行事实。
    - ``mcp_sync_check`` **没有可识别内容列**（只有内部 ULID、权限版本、次序、时间、结论与
      错误码），没有列可擦 → 删整行（
      :meth:`~lingxi.adapters.postgres_mcp_token.PostgresMcpTokenStore.
      purge_expired_checks`）。

    到期判据在两个适配器方法自己的 SQL 里（各自的 ``content_expires_at``，由迁移
    ``0064``/``0065`` 的触发器固定），本职责**一个条件都不加**：清理职责能扩大删除面的
    唯一方式就是"顺手多删一点"，而多删掉的东西没有任何恢复路径。

    **为什么是一个独立职责，而不是并进 :class:`RetentionCleanupDuty`**：后者的删除逻辑
    整个在数据库里——迁移 ``0054`` 建的受限清理函数，属主是无登录角色
    ``lingxi_retention_owner``，而适配器 :mod:`lingxi.adapters.retention` **一条 DELETE 都
    不写**；"应用与 scheduler 角色即使误获普通 DELETE 也删不掉那些表"这条保障靠的正是
    这条分工。本职责这两条是**应用层**语句（为什么它们没有进那个受限函数，理由在迁移
    ``0064``/``0065`` 的文件头部），塞进那个适配器会让它模块文档第一句话当场不成立，
    两套权限模型也会混进同一个返回摘要里。形状照 :class:`IdleConversationSweepDuty`
    ——它是**同一个缺陷的先例**：#151 只交付了应用层清理方法、没有接任何生产调用方
    （内审 P2-2），补救办法就是在这里给它一个每轮都会跑的调用点。

    **每轮只调用一次，不循环到擦空 / 删空**（同 :class:`RetentionCleanupDuty` 的理由）：
    一次调用就是一个数据库事务，因此没有半擦状态；积压由下一轮继续，两条都是幂等的
    （断言 ``V-保留-10``），到期时间也不会因为处置迟到而后移（断言 ``V-保留-16``）。

    **失败关闭**：任何一面抛异常都先留一条**只有异常类型**的审计，再原样上抛，由
    :meth:`SchedulerLoop.run_once` 逐职责隔离。既不吞掉也不折成 0——一条被吞掉的异常会
    让"到期内容一直没被处置"表现为每轮一条正常的完成审计，而保留违规最不该有的形态
    就是它悄无声息。
    """

    name = "权限链到期清理"

    def __init__(
        self,
        *,
        outbox: _ExpiredPayloadRedactor,
        checks: _ExpiredCheckPurger | None,
        audit: AuditSink,
        stop: threading.Event | None = None,
    ) -> None:
        self._outbox = outbox
        self._checks = checks
        self._audit = audit
        self._stop = threading.Event() if stop is None else stop

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    @property
    def checks_wired(self) -> bool:
        return self._checks is not None

    def request_stop(self) -> None:
        self._stop.set()

    def run_once(self) -> PermissionRetentionReport | None:
        """已经在停止中就一条都不处置。返回 ``None`` 表示本轮未执行。"""

        if self._stop.is_set():
            return None
        redacted = self._sweep("publish_outbox", self._outbox.redact_expired_payloads)
        purged = 0
        if self._checks is not None:
            purged = self._sweep("mcp_sync_check", self._checks.purge_expired_checks)
        report = PermissionRetentionReport(
            redacted=redacted, purged=purged, checks_wired=self._checks is not None
        )
        self._audit.record("permission_retention.completed", **report.audit_facts())
        if redacted or purged:
            # 一轮什么都没到期时不打日志：这条职责每分钟跑一次。
            logger.info(
                "权限链到期清理：publish_outbox 擦除 %s 条内容快照，mcp_sync_check 删除 %s 行",
                redacted,
                purged,
            )
        return report

    def _sweep(self, table: str, call: Callable[[], int]) -> int:
        try:
            return int(call())
        except Exception as error:
            # 只记表名与异常类型：异常正文可能带上被处置那一行的内容。
            self._audit.record(
                "permission_retention.sweep_failed",
                table=table,
                error=type(error).__name__,
            )
            logger.error("权限链到期清理失败 table=%s error=%s", table, type(error).__name__)
            raise
