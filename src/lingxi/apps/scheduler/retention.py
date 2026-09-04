"""到期数据清理职责：保留清理、空闲会话清理、权限链到期处置、内测采集到期删除。

从 :mod:`lingxi.apps.scheduler`（#237 拆分）搬出，几个职责同组的理由——共用「每轮只
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
from lingxi.apps.scheduler.config import SchedulerConfig

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
            logger.warning(
                "%s；本轮未清理完：%s 因锁等待超时让路，下一轮重试", rendered, "、".join(blocked)
            )
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


class _ExpiredNoticePurger(Protocol):
    """``onboarding_completion_notice`` 到期整行删除的最小端口（迁移 ``0066``）。
    实现是 :class:`lingxi.adapters.postgres_late_readiness_recovery.
    PostgresLateReadinessStore`。"""

    def purge_expired_notices(self) -> int: ...


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
    #: 本轮被删掉的 ``onboarding_completion_notice`` 行数（V-开通-18）。
    notices_purged: int = 0
    #: 判定记录那一面这一轮有没有装配（缺 MCP 令牌主密钥时为 ``False``）。
    checks_wired: bool = True

    def audit_facts(self) -> dict[str, Any]:
        return {
            "redacted": self.redacted,
            "purged": self.purged,
            "notices_purged": self.notices_purged,
            "checks_wired": self.checks_wired,
        }


class PermissionRetentionSweepDuty:
    """权限发布链三张表的九十天到期内容处置职责：每轮各调用一次。

    三张表的处置方式不同，因为它们**能擦的东西**不同：

    - ``publish_outbox.payload`` 里有邮箱与姓名 → 擦成 ``'{}'``（
      :meth:`~lingxi.adapters.postgres_permission_publish.PostgresPermissionPublishStore.
      redact_expired_payloads`）。行本身留着——``user_id``、权限版本、状态与时间戳是
      "谁的哪一版权限什么时候发布成功过"这类运行事实。
    - ``mcp_sync_check`` **没有可识别内容列**（只有内部 ULID、权限版本、次序、时间、结论与
      错误码），没有列可擦 → 删整行（
      :meth:`~lingxi.adapters.postgres_mcp_token.PostgresMcpTokenStore.
      purge_expired_checks`）。
    - ``onboarding_completion_notice``（迁移 ``0066``，V-开通-18 的通知 outbox）同样
      没有可识别内容列 → 删整行，但**只删已送达**（``delivered``）的那些
      （:meth:`~lingxi.adapters.postgres_late_readiness_recovery.
      PostgresLateReadinessStore.purge_expired_notices`）——``pending`` 的行无论多老
      都不会被这里删掉，删掉一条还在等待送达的通知等于让一个已经写成 ``active`` 的
      用户永远收不到「开通完成」，这条边界钉在适配器方法自己的 SQL 里，不在这里控制。

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
        notices: _ExpiredNoticePurger,
        audit: AuditSink,
        stop: threading.Event | None = None,
    ) -> None:
        self._outbox = outbox
        self._checks = checks
        self._notices = notices
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
        notices_purged = self._sweep(
            "onboarding_completion_notice", self._notices.purge_expired_notices
        )
        report = PermissionRetentionReport(
            redacted=redacted,
            purged=purged,
            notices_purged=notices_purged,
            checks_wired=self._checks is not None,
        )
        self._audit.record("permission_retention.completed", **report.audit_facts())
        if redacted or purged or notices_purged:
            # 一轮什么都没到期时不打日志：这条职责每分钟跑一次。
            logger.info(
                "权限链到期清理：publish_outbox 擦除 %s 条内容快照，mcp_sync_check 删除 %s 行，"
                "onboarding_completion_notice 删除 %s 行",
                redacted,
                purged,
                notices_purged,
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


class _ExpiredCapturePurger(Protocol):
    """``innertest_content_capture`` 的到期删除口。"""

    def purge_expired(self) -> int: ...


class ContentCaptureRetentionDuty:
    """``innertest_content_capture`` 的九十天到期删除（对抗审查 2026-09-02 C-7）。

    这张表（迁移 ``0069``）保存的是**用户问题原文、模型回答原文与工具调用详情**
    ——全仓库内容密度最高的一张表。迁移里 ``expires_at`` 触发器与到期扫描索引都
    建好了，却**没有任何调用方**：九十天上限只存在于一个没人读的列里。缺的不是
    机制，是职责。

    形状照 :class:`PermissionRetentionSweepDuty`：应用层小批量 DELETE、每轮一次、
    不循环到删空、失败关闭（留一条只含异常类型的审计后原样上抛，由
    ``SchedulerLoop.run_once`` 逐职责隔离）。**不并进那个职责**是因为名字要说真话
    ——它叫「权限链到期清理」，而这张表与权限链无关；两者的表清单混在同一个返回
    摘要里，任何一方将来变化都会污染另一方的运行事实。

    **无条件装配**：删自己库里的到期内容只需要连接串，而连接串是必需配置。给一条
    纯粹回收本方内容的路径加一个能关掉它的开关，等于给保留上界加一个旁路。生产
    这张表是空的（内容采集在生产一律不生效，见 ``apps/worker/config.py`` 的
    ``declares_production``），因此这条职责在生产每轮删 0 行、不打日志。
    """

    name = "内测采集到期删除"

    def __init__(
        self,
        *,
        captures: _ExpiredCapturePurger,
        audit: AuditSink,
        stop: threading.Event | None = None,
    ) -> None:
        self._captures = captures
        self._audit = audit
        self._stop = threading.Event() if stop is None else stop

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        self._stop.set()

    def run_once(self) -> int | None:
        """已经在停止中就一条都不删。返回 ``None`` 表示本轮未执行。"""

        if self._stop.is_set():
            return None
        try:
            purged = int(self._captures.purge_expired())
        except Exception as error:
            # 只记异常类型：异常正文可能带上被删那一行的内容（这张表每一行都是原文）。
            self._audit.record(
                "content_capture_retention.sweep_failed",
                table="innertest_content_capture",
                error=type(error).__name__,
            )
            logger.error(
                "内测采集到期删除失败 table=innertest_content_capture error=%s",
                type(error).__name__,
            )
            raise
        self._audit.record("content_capture_retention.completed", purged=purged)
        if purged:
            # 一轮什么都没到期时不打日志：这条职责每分钟跑一次。
            logger.info("内测采集到期删除：innertest_content_capture 删除 %s 行", purged)
        return purged


def _build_content_capture_retention_duty(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
) -> ContentCaptureRetentionDuty:
    """装配内测采集到期删除职责。**总是装配**，理由见类文档。"""

    from lingxi.adapters.postgres_content_capture_retention import (
        PostgresContentCaptureRetention,
    )

    return ContentCaptureRetentionDuty(
        captures=PostgresContentCaptureRetention(
            config.postgres_dsn, timeouts=config.postgres_timeouts
        ),
        audit=audit,
        stop=stop,
    )


def _build_permission_retention_duty(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
) -> PermissionRetentionSweepDuty:
    """装配权限链到期清理职责。**它总是注册**，只有判定记录那一面按前置条件装配。

    **``publish_outbox`` 那一面没有任何配置前置**：擦 ``payload`` 只需要数据库连接串，
    而连接串是必需配置、进程起得来就一定有。这一面恰恰是带个人数据的那一面（邮箱与
    姓名），因此它必须无条件跑起来——给一条纯粹是"擦自己库里的内容"的路径加一个能让它
    不注册的开关，等于给保留上界加了一个可以被关掉的旁路。

    **``mcp_sync_check`` 那一面的前置是 MCP 令牌主密钥**（``LINGXI_MCP_TOKEN_ENCRYPT_KEY``）：
    唯一的读写口 :class:`~lingxi.adapters.postgres_mcp_token.PostgresMcpTokenStore` 构造时
    就要求一个**已经校验过主密钥**的加解密对象（它同时承载解密路径）。删过期行本身用不到
    密钥，但绕过那个构造约束就得给这个调用点单开一个不校验密钥的口子——那正是该类刻意
    拒绝的事（它对非 :class:`McpTokenCipher` 直接抛 ``TypeError``）。缺密钥时这一面**不装配**
    并留下**恰一条**审计，形状照 :func:`_build_readiness_follow_up` 的探针那一面（缺项只报
    变量名、不回显任何值——它还是一把主密钥），发布 outbox 那一面照常。

    这条取舍的产品后果是可接受的、也已写明：``mcp_sync_check`` **没有可识别内容列**，
    到期不删的后果是一张只含内部 ULID 与结论码的表继续变长；而真正含邮箱与姓名的
    ``publish_outbox.payload`` 一轮都不会少擦。

    **``onboarding_completion_notice``（V-开通-18，迁移 ``0066``）与 ``publish_outbox``
    同一面**：只需要数据库连接串，没有可选前置，因此无条件装配。
    """

    from lingxi.adapters.postgres_late_readiness_recovery import PostgresLateReadinessStore
    from lingxi.adapters.postgres_permission_publish import PostgresPermissionPublishStore

    checks: Any = None
    if config.mcp_token_encrypt_key:
        from lingxi.adapters.mcp_token_cipher import McpTokenCipher
        from lingxi.adapters.postgres_mcp_token import PostgresMcpTokenStore

        checks = PostgresMcpTokenStore(
            config.postgres_dsn,
            cipher=McpTokenCipher(config.mcp_token_encrypt_key),
            timeouts=config.postgres_timeouts,
        )
    else:
        from lingxi.adapters.mcp_token_cipher import MASTER_KEY_ENV

        # **恰一条**审计：只关掉判定记录那一面，发布 outbox 的内容擦除照常。
        audit.record(
            "permission_retention.checks_not_wired",
            reason="missing_environment_variable",
            variable=MASTER_KEY_ENV,
        )
        logger.warning(
            "未配置 %s，mcp_sync_check 的到期删除不装配；publish_outbox 的到期内容擦除照常运行",
            MASTER_KEY_ENV,
        )

    return PermissionRetentionSweepDuty(
        outbox=PostgresPermissionPublishStore(
            config.postgres_dsn, timeouts=config.postgres_timeouts
        ),
        checks=checks,
        # onboarding_completion_notice（迁移 0066，V-开通-18）同样没有可选前置——
        # 只需要数据库连接串，因此这一面与 publish_outbox 那一面同样无条件装配。
        notices=PostgresLateReadinessStore(config.postgres_dsn, timeouts=config.postgres_timeouts),
        audit=audit,
        stop=stop,
    )
