"""``lingxi-scheduler``：定时职责进程。

进程现在跑**四个**职责，由 :class:`SchedulerLoop` 按同一个周期依次驱动：

1. **专用授权凭据轮换**（:class:`CredentialRotationLoop`）——「四达文档会议助手」
   ``refresh_token`` 的到期续期；
2. **九十天保留清理**（:class:`RetentionCleanupDuty`）——调用数据库里的受限清理
   函数回收到期内容（Issue #54 / S9）；
3. **空闲会话到点清理**（:class:`IdleConversationSweepDuty`）——会话空闲满两小时
   由 scheduler 周期扫描并主动清除已送达的投递正文，不依赖下一次任务入队
   （Issue #151、2026-08-14 补充决定、`V-投递-10`）；
4. **花名册资料比对与管理群审计日报**（:class:`RosterAuditDuty`）——每天比对一次
   已开通用户的花名册当前值与建档存档三字段，有差异就向管理群发一条脱敏日报
   （Issue #52 / W4-B）。第四个职责是**条件注册**的，两个前置任缺其一就不注册：
   群 ID 环境变量缺失（可选配置，见 :class:`SchedulerConfig`），或花名册读取传输
   未接线（真实读取的凭据与 Base ID 属 L4a 前置，登记为 R3）。不注册时进程照常
   启动、其余职责照常运行，并留下一条指名变量的审计（断言 ``V-花名册-29``）。

架构设计把定时职责单独分给本进程，理由是"定时职责与请求路径无关，混在一起会让
重启语义不清"。2026-08-05 在 `tz` 的复验实测到这条正好被违反：测试资产把续期扫描
挂在飞书长连接进程内的常驻线程上，把长连接进程 kill 掉后扫描线程无声停止，没有
任何独立信号提示"续期已经不再运行"（[Issue #16 复验记录]
(https://github.com/Moshuiwang/lingxi/issues/16#issuecomment-5188063325)）。

**职责之间互不牵连**（断言 V-保留-15）。``SchedulerLoop.run_once`` 逐个职责地捕获
异常：清理连续失败不会让凭据轮换这一轮被跳过，反之亦然。这一条必须由代码结构
保证而不是靠"两个职责都不会抛异常"——保留清理会因为数据库权限、连接、锁等待失败，
而它失败时最不该发生的事情就是把一条一次性凭据的续期窗口一起拖没。

本模块只做组装：配置从环境变量来，轮换规则在
:mod:`lingxi.core.identity.credentials`，存取在
:mod:`lingxi.adapters.delegated_credentials`（宿主机文件保管，选项 A 决策），飞书调用在
:mod:`lingxi.adapters.feishu_directory`，清理函数调用在
:mod:`lingxi.adapters.retention`。

退出语义（断言 V-部署-03、V-保留-17）：收到 ``SIGTERM`` / ``SIGINT`` 后**停止领取
新工作**，把已经领取的那一次做完，然后退出。半途中断一次轮换会留下一个"已经向飞书
换过、但没写回数据库"的窗口，而 ``refresh_token`` 一次性有效——那个窗口等于凭据丢失。
清理侧没有对应窗口：一次调用就是一个数据库事务，被打断只会整体回滚。
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Protocol

from lingxi.adapters.postgres import (
    DEFAULT_POSTGRES_TIMEOUTS,
    PostgresTimeoutConfigError,
    PostgresTimeouts,
)
from lingxi.core.identity.credentials import AuthorizationGrant, CredentialAction, RefreshOutcome, decide_after_refresh
from lingxi.core.identity.identifiers import redact_identifier
from lingxi.core.identity.roster_audit import ArchivedIdentity, RosterAuditReport, compare_roster
from lingxi.core.identity.roster_report import render_daily_report_content
from lingxi.core.alerting import AlertKind, AlertManager, AlertNotice, AlertPolicy, AlertSignal

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 60

# 新凭据写库的退避重试间隔（秒）。飞书侧旧凭据在续期成功那一刻已作废，
# 这里的每一次重试都是在挽救一条一次性凭据；用 _stop.wait 而不是 sleep，
# 让 SIGTERM 仍能立即打断等待。
SAVE_RETRY_BACKOFF_SECONDS = (0.2, 1.0, 3.0)
# 飞书开放平台地址来自配置，代码里只有一个可被覆盖的默认值（断言 V-部署-01）。
DEFAULT_FEISHU_BASE_URL = "https://open.feishu.cn/open-apis"


class _Secret(str):
    """只影响 ``repr`` 的字符串子类：配置对象被打印时不吐出凭据。"""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 行为由 SchedulerConfigTest 覆盖
        return "'<已脱敏>'"


@dataclass(frozen=True)
class SchedulerConfig:
    postgres_dsn: str = field(repr=False)
    credential_key: str = field(repr=False)
    # 凭据文件的宿主机路径。部署契约：必须指向跨部署持久的挂载路径，
    # 镜像替换与重启不得丢失——否则每次部署都要重新授权（产品负责人
    # 2026-08-05 明确以「无需特殊处理」为目标、重新授权仅作保底）。
    credential_path: str
    feishu_app_id: str
    feishu_app_secret: str = field(repr=False)
    feishu_base_url: str
    interval_seconds: int
    postgres_timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS
    # 管理群 chat_id。**可选**：没有它进程照常启动，只是不注册审计日报职责。
    # 做成可选而不是必需，是因为它只服务三个职责中的一个——为一个尚未接线的职责
    # 让整个 scheduler 起不来，会把「日报没配」升级成「凭据轮换也停了」。
    # 配了但格式不对则**快速失败**：那是错配，不是未配，静默降级会让人以为在发日报。
    admin_group_chat_id: str | None = None
    alert_policy: AlertPolicy = field(default_factory=AlertPolicy)

    ENVIRONMENT_KEYS = (
        "LINGXI_POSTGRES_DSN",
        "LINGXI_POSTGRES_CONNECT_TIMEOUT_SECONDS",
        "LINGXI_POSTGRES_STATEMENT_TIMEOUT_SECONDS",
        "LINGXI_POSTGRES_LOCK_TIMEOUT_SECONDS",
        "LINGXI_DELEGATED_CREDENTIAL_KEY",
        "LINGXI_DELEGATED_CREDENTIAL_PATH",
        "LINGXI_FEISHU_APP_ID",
        "LINGXI_FEISHU_APP_SECRET",
        "LINGXI_FEISHU_BASE_URL",
        "LINGXI_SCHEDULER_INTERVAL_SECONDS",
        "LINGXI_ADMIN_GROUP_CHAT_ID",
        "LINGXI_ALERT_HEARTBEAT_TIMEOUT_SECONDS",
        "LINGXI_ALERT_QUEUED_TIMEOUT_SECONDS",
        "LINGXI_ALERT_RUNNING_HEARTBEAT_TIMEOUT_SECONDS",
        "LINGXI_ALERT_SEND_FAILURE_WINDOW_SECONDS",
        "LINGXI_ALERT_SEND_FAILURE_THRESHOLD",
        "LINGXI_ALERT_DEDUPE_WINDOW_SECONDS",
        "LINGXI_ALERT_RECOVERY_STABLE_SECONDS",
        "LINGXI_ALERT_RETRY_BASE_SECONDS",
        "LINGXI_ALERT_RETRY_FACTOR",
        "LINGXI_ALERT_RETRY_CEILING_SECONDS",
    )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "SchedulerConfig":
        """一次性读完全部配置。缺项只报变量名，绝不回显取到的值。"""

        source = os.environ if environ is None else environ

        def required(name: str) -> str:
            value = (source.get(name) or "").strip()
            if not value:
                raise ValueError(f"缺少必需的环境变量：{name}")
            return value

        raw_interval = (source.get("LINGXI_SCHEDULER_INTERVAL_SECONDS") or "").strip()
        if raw_interval:
            try:
                interval = int(raw_interval)
            except ValueError as error:
                raise ValueError("环境变量 LINGXI_SCHEDULER_INTERVAL_SECONDS 必须是正整数秒") from error
            if interval <= 0:
                raise ValueError("环境变量 LINGXI_SCHEDULER_INTERVAL_SECONDS 必须是正整数秒")
        else:
            interval = DEFAULT_INTERVAL_SECONDS

        raw_chat_id = (source.get("LINGXI_ADMIN_GROUP_CHAT_ID") or "").strip()
        if raw_chat_id:
            from lingxi.adapters.feishu_group_message import validate_group_chat_id

            # 校验函数不回显取到的值，只报变量名与期望形状。
            admin_group_chat_id: str | None = validate_group_chat_id(raw_chat_id)
        else:
            admin_group_chat_id = None

        try:
            postgres_timeouts = PostgresTimeouts.from_env(source)
        except PostgresTimeoutConfigError as error:
            raise ValueError(str(error)) from None
        try:
            alert_policy = AlertPolicy.from_mapping(source)
        except ValueError as error:
            raise ValueError(str(error)) from None

        return cls(
            postgres_dsn=_Secret(required("LINGXI_POSTGRES_DSN")),
            postgres_timeouts=postgres_timeouts,
            credential_key=_Secret(required("LINGXI_DELEGATED_CREDENTIAL_KEY")),
            credential_path=required("LINGXI_DELEGATED_CREDENTIAL_PATH"),
            feishu_app_id=required("LINGXI_FEISHU_APP_ID"),
            feishu_app_secret=_Secret(required("LINGXI_FEISHU_APP_SECRET")),
            feishu_base_url=(source.get("LINGXI_FEISHU_BASE_URL") or "").strip() or DEFAULT_FEISHU_BASE_URL,
            interval_seconds=interval,
            admin_group_chat_id=admin_group_chat_id,
            alert_policy=alert_policy,
        )


@dataclass(frozen=True)
class RotationReport:
    claimed: int = 0
    rotated: int = 0
    revoked: int = 0


class _Vault(Protocol):
    def claim_due(self) -> Any: ...
    def save(
        self,
        *,
        subject_open_id: str,
        grant: AuthorizationGrant,
        issued_at: Any = ...,
        replacing_generation: Any = ...,
        expected_registered_subject_open_id: Any = ...,
    ) -> Any: ...
    def revoke(self, *, reason: str, generation: Any = ...) -> bool: ...


class _Authorization(Protocol):
    def refresh(self, current: AuthorizationGrant) -> tuple[AuthorizationGrant, Any]: ...


class CredentialRotationLoop:
    """按飞书返回有效期的 80% 触发轮换的扫描循环。

    循环本身不判断"该不该轮换"——到期判定写在 SQL 的领取条件里（轮换点已由
    :func:`lingxi.core.identity.credentials.rotation_deadline` 算好并落库），
    失败后的处置写在 :func:`decide_after_refresh`。这里只负责编排与退出。
    """

    name = "凭据轮换"

    def __init__(
        self,
        *,
        vault: _Vault,
        authorization: _Authorization,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        stop: threading.Event | None = None,
    ) -> None:
        self._vault = vault
        self._authorization = authorization
        self._interval_seconds = interval_seconds
        # 与同一进程内的其他职责共享停止标志：SIGTERM 必须一次让所有职责都停止
        # 领取新工作，而不是只停下恰好持有信号处理函数的那一个。
        self._stop = threading.Event() if stop is None else stop

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        self._stop.set()

    def run_once(self) -> RotationReport:
        """领取至多一条到期凭据并处理它。已经在停止中则一条都不领。"""

        if self._stop.is_set():
            return RotationReport()
        # 先收殓崩溃窗口留下的「已消费未落库」行：它们的旧令牌已被飞书作废，
        # 不收殓就会在租期结束后被当成正常凭据再领取一次（Codex 复查发现）。
        stale_collector = getattr(self._vault, "revoke_stale_consumed", None)
        if callable(stale_collector):
            stale_collector()
        if self._stop.is_set():
            # SIGTERM 可能在收殓等待文件锁期间到达：领取前必须再看一次，
            # 否则会在关闭宽限期里再启动一条最长 20 秒的续期请求（终轮 Codex）。
            return RotationReport()
        claim = self._vault.claim_due()
        if claim is None:
            return RotationReport()

        try:
            replacement, _access_token = self._authorization.refresh(claim.grant)
            outcome = RefreshOutcome.ROTATED
        except Exception as error:  # noqa: BLE001 - 任何异常都不足以证明"旧凭据还能用"
            replacement = None
            outcome = RefreshOutcome.FAILED if _is_definite_failure(error) else RefreshOutcome.INDETERMINATE
            logger.warning("专用授权续期未成功 outcome=%s error=%s", outcome.value, type(error).__name__)

        claim_generation = getattr(claim, "generation", None) or None
        if decide_after_refresh(outcome) is CredentialAction.ROTATE and replacement is not None:
            if self._save_with_retry(
                subject_open_id=claim.subject_open_id,
                replacement=replacement,
                replacing_generation=claim_generation,
            ):
                logger.info("专用授权凭据已轮换 subject=%s", redact_identifier(claim.subject_open_id))
                return RotationReport(claimed=1, rotated=1)
            # 新凭据没能落库：旧的此刻已被飞书作废，继续留着只会让下一轮拿死
            # 凭据再撞一次墙。撤销并用可与普通失败区分的日志请求人工重新授权
            # （独立复查发现：此前这里的异常会带着仅存于内存的新凭据一起消失）。
            logger.error(
                "不可恢复：续期成功但新凭据写库失败，旧凭据已被飞书作废，需人工重新授权 subject=%s",
                redact_identifier(claim.subject_open_id),
            )
            self._vault.revoke(reason="rotation_persist_failed", generation=claim_generation)
            return RotationReport(claimed=1, revoked=1)

        # 只撤销领取到的那一代：期间的新授权不得被旧链失败连带删除（终轮 Codex）。
        self._vault.revoke(reason=f"refresh_{outcome.value}", generation=claim_generation)
        return RotationReport(claimed=1, revoked=1)

    def _save_with_retry(self, *, subject_open_id: str, replacement: Any, replacing_generation: str | None = None) -> bool:
        """新凭据落盘带短退避重试：一次瞬时抖动不该报废一条一次性凭据。"""

        for delay_seconds in (0.0, *SAVE_RETRY_BACKOFF_SECONDS):
            if delay_seconds:
                self._stop.wait(delay_seconds)
            try:
                saved = self._vault.save(
                    subject_open_id=subject_open_id,
                    grant=replacement,
                    replacing_generation=replacing_generation,
                    expected_registered_subject_open_id=subject_open_id,
                )
                if saved is False:
                    # 世代不符＝期间有新授权：旧链结果作废，视为已妥善收尾。
                    return True
                return True
            except Exception as error:  # noqa: BLE001 - 记录后重试，最终失败由调用方处置
                logger.warning("新凭据写库失败，将重试 error=%s", type(error).__name__)
        return False

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as error:  # noqa: BLE001 - 定时职责不因一轮异常而终止
                logger.error("本轮续期扫描异常，下一轮继续 error=%s", type(error).__name__)
            if self._stop.is_set():
                break
            self._stop.wait(self._interval_seconds)
        logger.info("续期扫描已停止领取并退出")


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


class AuditSink(Protocol):
    """审计出口。

    ``audit_event`` 表属 S9，尚未建立；当前实现写结构化日志。职责只依赖这个签名，
    届时换实现不动职责代码。签名与 #57 网关侧的同名 Protocol 一致（结构化类型，
    两边互相满足），合并后可收敛成一份，不需要现在跨切片耦合。
    """

    def record(self, action: str, /, **fields: object) -> None: ...


class StructuredLogAuditSink:
    """把审计动作写成一行结构化日志。

    字段按**键名排序**输出：审计行会被比对和 grep，顺序随 ``PYTHONHASHSEED`` 变化的
    日志没法稳定断言。本类原样输出收到的字段——「不带资料值」这条约束属于调用方，
    对应断言 ``V-花名册-33`` 因此断在调用方产生的那几行上。
    """

    def record(self, action: str, /, **fields: object) -> None:
        rendered = " ".join(f"{key}={value}" for key, value in sorted(fields.items()))
        logger.info("审计 action=%s %s", action, rendered)


class _AlertSender(Protocol):
    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None: ...


@dataclass
class _PendingAlert:
    notice: AlertNotice
    next_attempt_at: datetime
    attempt: int = 0


class AlertDispatcher:
    """把安全告警摘要投递给管理群，并隔离投递失败。

    这里是唯一会调用发送适配器的告警编排层。发送失败只保留摘要和下一次重试时间，
    不把异常抛回 ``SchedulerLoop``，也不保存告警正文之外的业务对象。真实飞书行为
    仍由注入的 sender 与 E4 受控窗口验证。
    """

    def __init__(
        self,
        *,
        sender: _AlertSender,
        chat_id: str,
        policy: AlertPolicy | None = None,
        audit: AuditSink | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not chat_id:
            raise ValueError("告警管理群不能为空")
        self._sender = sender
        self._chat_id = chat_id
        self._policy = policy or AlertPolicy()
        self._audit = audit
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._pending: dict[str, _PendingAlert] = {}
        self.observed_delays: list[float] = []

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def submit(self, notices: Sequence[AlertNotice]) -> None:
        """加入待投递队列；相同去重键只保留一条。"""

        now = _alert_utc(self._clock())
        for notice in sorted(notices, key=lambda item: item.dedupe_key):
            self._pending.setdefault(
                notice.dedupe_key,
                _PendingAlert(notice=notice, next_attempt_at=now),
            )

    def run_once(self, *, at: datetime | None = None) -> int:
        """投递当前到期的告警，返回本轮成功数。"""

        now = _alert_utc(self._clock() if at is None else at)
        sent = 0
        for dedupe_key in sorted(tuple(self._pending)):
            pending = self._pending.get(dedupe_key)
            if pending is None or pending.next_attempt_at > now:
                continue
            notice = pending.notice
            try:
                self._sender.send_text(
                    chat_id=self._chat_id,
                    text=notice.text,
                    dedupe_key=notice.dedupe_key,
                )
            except Exception as error:  # noqa: BLE001 - 告警失败只进入本地重试队列
                delay = self._policy.retry_delay(pending.attempt)
                pending.attempt += 1
                pending.next_attempt_at = now + timedelta(seconds=delay)
                self.observed_delays.append(delay)
                self._record(
                    "alert.send_failed",
                    event_type=notice.event_type,
                    action=notice.action.value,
                    attempt=pending.attempt,
                    error=type(error).__name__,
                )
                logger.error(
                    "运行告警发送失败，将重试 event=%s attempt=%s error=%s",
                    notice.event_type,
                    pending.attempt,
                    type(error).__name__,
                )
                continue
            del self._pending[dedupe_key]
            sent += 1
            self._record(
                "alert.sent",
                event_type=notice.event_type,
                action=notice.action.value,
                count=notice.count,
            )
        return sent

    def _record(self, action: str, /, **fields: object) -> None:
        if self._audit is None:
            return
        try:
            self._audit.record(action, **fields)
        except Exception as error:  # noqa: BLE001 - 审计观察失败不能改变告警重试
            logger.error("运行告警审计失败 action=%s error=%s", action, type(error).__name__)


class AlertingDuty:
    """把告警状态机、恢复计时和管理群投递接在一个定时职责上。"""

    name = "运行告警"

    def __init__(
        self,
        *,
        manager: AlertManager,
        dispatcher: AlertDispatcher,
        audit: AuditSink | None = None,
        clock: Callable[[], datetime] | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        self._manager = manager
        self._dispatcher = dispatcher
        self._audit = audit
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._stop = threading.Event() if stop is None else stop

    @property
    def manager(self) -> AlertManager:
        return self._manager

    @property
    def dispatcher(self) -> AlertDispatcher:
        return self._dispatcher

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        self._stop.set()

    def heartbeat_callback(self, component: str) -> Callable[[], None]:
        """返回给常驻进程的无参数心跳回调。"""

        def beat() -> None:
            self._manager.heartbeat(component, at=_alert_utc(self._clock()))

        return beat

    def task_stuck_callback(self) -> Callable[[str, int], None]:
        """返回给 Worker 的任务滞留回调，只接受类别与计数。"""

        def report(kind: str, count: int) -> None:
            notices = self._manager.task_stuck(
                AlertKind(kind), count=count, at=_alert_utc(self._clock())
            )
            self._submit(notices)

        return report

    def send_outcome_callback(self) -> Callable[[str, bool], None]:
        """返回给卡片/群消息适配器的发送结果回调。"""

        def outcome(operation: str, succeeded: bool) -> None:
            at = _alert_utc(self._clock())
            if succeeded:
                notices = self._manager.send_succeeded(channel=operation, at=at)
            else:
                notices = self._manager.send_failure(
                    channel=operation,
                    final=operation.endswith("_final"),
                    at=at,
                )
            self._submit(notices)

        return outcome

    def observe(self, signal: AlertSignal) -> tuple[AlertNotice, ...]:
        notices = self._manager.observe(signal)
        self._submit(notices)
        return notices

    def run_once(self) -> tuple[AlertNotice, ...] | None:
        if self._stop.is_set():
            return None
        now = _alert_utc(self._clock())
        notices = self._manager.check_heartbeats(at=now)
        notices += self._manager.tick(at=now)
        self._submit(notices)
        self._dispatcher.run_once(at=now)
        return notices

    def _submit(self, notices: Sequence[AlertNotice]) -> None:
        if not notices:
            return
        self._dispatcher.submit(notices)
        if self._audit is None:
            return
        for notice in sorted(notices, key=lambda item: item.dedupe_key):
            action = "alert.recovery_recorded" if notice.action.value == "recovery" else "alert.recorded"
            try:
                self._audit.record(
                    action,
                    event_type=notice.event_type,
                    count=notice.count,
                    trace_id=notice.trace_id or "-",
                )
            except Exception as error:  # noqa: BLE001 - 审计失败不能丢待投递告警
                logger.error("运行告警记录失败 action=%s error=%s", action, type(error).__name__)


def _alert_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("告警时间必须带时区")
    return value.astimezone(timezone.utc)


class _BaselineReader(Protocol):
    def load_active_baseline(self) -> Sequence[ArchivedIdentity]: ...


class _GroupSender(Protocol):
    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None: ...


class RosterAuditDuty:
    """每日花名册资料比对与管理群审计日报（Issue #52 / W4-B）。

    一轮做四件事：读比对基线 → 读花名册当前值 → 纯函数比对 → 有差异就发一条脱敏日报。
    四件事里只有第一件碰数据库，而且**只读**（`V-花名册-14`）。

    **同日至多一次**（`V-花名册-31`）。判重靠 ``_completed_on`` 这个进程内的日期水位：

    - 单轮一次、同进程跨轮一次：由这个水位**硬保证**，与轮询周期无关；
    - 跨重启：零新表定案下没有持久载体，新进程的水位是空的，因此**重启当日会重发一份
      内容完全相同的日报**。这是产品负责人 2026-08-06 知情接受的残留（裁定 C2 / R2）：
      A 方案下报告由「花名册现值 + 存档」唯一确定，补跑产出同一份，重复是噪声不是错误。
      真幂等等 ``audit_event`` 表（S9）落地后再补。

    水位在**发送成功之后**才置位。发送失败不算已发送，下一轮重试（`V-花名册-30`）。
    空差异日也置位：那一天的审计已经记过了，重复记只是噪声（`V-花名册-25`）。

    **重试与"不确定态"**。"发送失败就重试"这条规则有一个它自己看不见的缺口：HTTP
    请求已经被飞书收下、而响应在回程超时的那一刻，进程拿到的是异常，事实却是消息已经
    发出去了。重试于是重复投递一条日报。同一天的每一次发送——首次与全部重试——因此共用
    一个去重键（当日 UTC 日期），由 :func:`~lingxi.adapters.feishu_group_message.delivery_uuid`
    折成飞书的投递 `uuid` 交服务端去重。平台侧的去重窗口未经验证（属 L4a），所以这里
    承诺的是"重试携带同一个 `uuid`"这个代码事实，不是"平台一定不会重复投递"。
    """

    name = "花名册审计日报"

    def __init__(
        self,
        *,
        baseline_reader: _BaselineReader,
        roster_reader: Callable[[], Sequence[Mapping[str, object]]],
        sender: _GroupSender,
        audit: AuditSink,
        chat_id: str,
        clock: Callable[[], datetime] | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        self._baseline_reader = baseline_reader
        self._roster_reader = roster_reader
        self._sender = sender
        self._audit = audit
        self._chat_id = chat_id
        # 时钟注入：跨轮判重的用例要能自己决定「今天」是哪天，不能靠等到明天。
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._stop = threading.Event() if stop is None else stop
        self._completed_on: date | None = None

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    @property
    def completed_on(self) -> date | None:
        """已完成日报的那一天。``None`` 表示本进程实例今天还没发过。"""

        return self._completed_on

    def request_stop(self) -> None:
        self._stop.set()

    def run_once(self) -> RosterAuditReport | None:
        """跑一轮。返回 ``None`` 表示本轮没有执行比对（停止中，或今天已经做完）。"""

        if self._stop.is_set():
            # 已经在停止中：一轮都不开。停止之后必须 0 次发送（`V-花名册-20`）。
            return None
        today = self._clock().date()
        if self._completed_on == today:
            return None

        baseline = self._baseline_reader.load_active_baseline()
        report = compare_roster(baseline, self._roster_reader())

        if report.is_empty:
            # 空差异日**不发日报**，只记一条审计。合同 :85 的语义是「通知待办」，
            # 没有待办却每天发一条「今天没事」，会让管理群很快学会忽略这个通知。
            self._audit.record(
                "roster_audit.no_difference",
                report_date=today.isoformat(),
                examined=report.examined,
            )
            self._completed_on = today
            return report

        if self._stop.is_set():
            # 停止信号落在读取阶段（读库 + 读花名册都可能耗时）。这里再看一次，
            # 让这一轮成为**干净中断**：不发送、不置水位，什么都没发生。
            # 停止之后必须 0 次发送（`V-花名册-20`），而入口处那一次检查挡不住
            # "进来时还没停、读完才停"这条时序。重发由裁定 C2 的知情接受覆盖。
            logger.info("停止信号在花名册读取期间到达，本轮不发送日报")
            return report

        content = render_daily_report_content(report, report_date=today)
        try:
            # 同一天的日报（含失败重试）共用一个去重键：不确定态下的重试因此携带
            # 同一个投递 `uuid`，由飞书服务端去重，而不是必然重复投递。
            self._sender.send_text(
                chat_id=self._chat_id, text=content.text, dedupe_key=today.isoformat()
            )
        except Exception as error:  # noqa: BLE001 - 发送失败不得带走同一轮的其他职责
            # 只记审计与异常类型：异常正文可能带上群 ID 或响应体。水位不置位，
            # 因此这一天**不算已发送**，下一轮会重试（`V-花名册-30`）。
            self._audit.record(
                "roster_audit.send_failed",
                report_date=today.isoformat(),
                content_key=content.key,
                content_version=content.version,
                error=type(error).__name__,
            )
            logger.error("管理群审计日报发送失败，下一轮重试 error=%s", type(error).__name__)
            return report

        self._audit.record(
            "roster_audit.report_sent",
            report_date=today.isoformat(),
            content_key=content.key,
            content_version=content.version,
            examined=report.examined,
            entries=len(report.entries),
            handover=report.handover_count,
            removed=report.removed_count,
            ambiguous=report.ambiguous_count,
        )
        self._completed_on = today
        # 摘要只有计数。任何一个人的标识或资料值进日志，都等于绕过日报的脱敏口径。
        logger.info(
            "管理群审计日报已发送 已开通用户=%s 条目=%s 疑似转交=%s 花名册查无=%s",
            report.examined,
            len(report.entries),
            report.handover_count,
            report.removed_count,
        )
        return report


class SchedulerLoop:
    """按同一周期驱动多个定时职责，并把它们的失败互相隔离。

    ``build_loop`` 此前直接返回单个 :class:`CredentialRotationLoop`，"进程只有一个
    职责"这件事被硬编码在装配里。加入第二个职责必须改的正是这里：需要一个能容纳
    职责集合、并且**逐职责**捕获异常的结构。
    """

    def __init__(
        self,
        *,
        duties: Sequence[Any],
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        stop: threading.Event | None = None,
        heartbeat: Callable[[], None] | None = None,
    ) -> None:
        if not duties:
            raise ValueError("定时职责进程至少要有一个职责")
        self._duties = tuple(duties)
        self._interval_seconds = interval_seconds
        self._stop = threading.Event() if stop is None else stop
        self._heartbeat = heartbeat

    @property
    def duties(self) -> tuple[Any, ...]:
        return self._duties

    @property
    def stop_event(self) -> threading.Event:
        return self._stop

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        self._stop.set()

    def run_once(self) -> tuple[Any, ...]:
        """依次跑一遍每个职责。任何一个职责抛异常都不影响其余职责本轮执行。"""

        reports: list[Any] = []
        if self._heartbeat is not None:
            try:
                self._heartbeat()
            except Exception as error:  # noqa: BLE001 - 心跳失败不能跳过定时职责
                logger.error("scheduler 心跳记录失败，职责继续运行 error=%s", type(error).__name__)
        for duty in self._duties:
            if self._stop.is_set():
                # 已经在停止中：不再让后面的职责领取新工作（断言 V-保留-17）。
                reports.append(None)
                continue
            try:
                reports.append(duty.run_once())
            except Exception as error:  # noqa: BLE001 - 一个职责失败不能带走另一个
                # 只记异常类型，不记异常正文：正文可能带上被处理对象的内容。
                logger.error(
                    "定时职责本轮异常，其余职责与下一轮不受影响 duty=%s error=%s",
                    getattr(duty, "name", type(duty).__name__),
                    type(error).__name__,
                )
                reports.append(None)
        return tuple(reports)

    def run_forever(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            if self._stop.is_set():
                break
            self._stop.wait(self._interval_seconds)
        logger.info("定时职责已停止领取并退出")


def _is_definite_failure(error: BaseException) -> bool:
    """区分"飞书明确拒绝"与"结果不明确"。

    两者的处置**相同**（都撤销），区分只为了让日志与后续审计能分辨这两件事。
    分类是协议细节，由 adapters 层以 ``definite`` 属性给出（代码框架第二节：
    协议细节不进 apps 层）；没有该属性的异常一律视为"结果不明确"。
    """

    definite = getattr(error, "definite", None)
    return definite is True


class _Stoppable(Protocol):
    def request_stop(self) -> None: ...


def install_signal_handlers(loop: _Stoppable) -> None:
    """把 ``SIGTERM`` / ``SIGINT`` 接到"停止领取"上。

    处理函数只设一个事件标志，不做任何 I/O：信号处理函数里写库或发网络请求会在
    退出路径上引入新的失败模式，而这条路径恰恰是最不该出错的地方。
    """

    def handle(signal_number: int, _frame: Any) -> None:
        logger.info("收到信号，停止领取新的到期凭据 signal=%s", signal_number)
        loop.request_stop()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)


def _build_roster_audit_duty(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
    roster_page_reader: Any | None,
    on_send_outcome: Callable[[str, bool], None] | None = None,
) -> RosterAuditDuty | None:
    """装配审计日报职责；前置不齐就**不注册**并留下一条审计，返回 ``None``。

    两个前置的顺序不能换：先看群 ID。两者都缺时也只记一条审计，`V-花名册-29`
    要求「缺群 ID → 审计**恰 1 条**」。
    """

    if not config.admin_group_chat_id:
        # 只报变量名，不回显任何值（`V-花名册-29`）。
        audit.record(
            "roster_audit.duty_not_registered",
            reason="missing_environment_variable",
            variable="LINGXI_ADMIN_GROUP_CHAT_ID",
        )
        logger.warning(
            "未配置 LINGXI_ADMIN_GROUP_CHAT_ID，花名册审计日报职责不注册；其余定时职责照常运行"
        )
        return None
    if roster_page_reader is None:
        # 真实花名册读取的传输、凭据与 Base ID 属 L4a 前置（登记为 R3）。这里显式
        # 不注册并留痕，而不是装一个每轮都炸的假读取——后者会把「还没接线」伪装成
        # 「接线了但一直失败」。
        audit.record("roster_audit.duty_not_registered", reason="roster_reader_unwired")
        logger.warning("花名册读取传输未接线（真实读取属 L4a 前置），花名册审计日报职责不注册")
        return None

    from lingxi.adapters.feishu_group_message import FeishuGroupMessages
    from lingxi.adapters.feishu_roster_bitable import read_roster_records
    from lingxi.adapters.postgres_roster_audit import PostgresRosterBaselineReader

    return RosterAuditDuty(
        baseline_reader=PostgresRosterBaselineReader(
            config.postgres_dsn, timeouts=config.postgres_timeouts
        ),
        roster_reader=lambda: read_roster_records(roster_page_reader),
        sender=FeishuGroupMessages(
            base_url=config.feishu_base_url,
            app_id=config.feishu_app_id,
            app_secret=config.feishu_app_secret,
            on_send_outcome=on_send_outcome,
        ),
        audit=audit,
        chat_id=config.admin_group_chat_id,
        stop=stop,
    )


def build_loop(
    config: SchedulerConfig,
    *,
    roster_page_reader: Any | None = None,
    audit: AuditSink | None = None,
    alerting_duty: AlertingDuty | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> SchedulerLoop:
    """装配进程的全部定时职责。

    职责顺序有意为之：凭据轮换在前。它处理的是一次性有效、有硬期限的凭据，
    而两类清理晚一轮都没有任何后果——到期行/空闲会话下一轮照清，到期时间不会
    因为清理迟到而后移（断言 V-保留-16），空闲会话清理本身也是幂等的。审计日报
    排在最后：它一天只做一次事，晚一轮毫无影响。

    ``roster_page_reader`` 是花名册多维表格的分页读取传输
    （:class:`lingxi.adapters.feishu_roster_bitable.RecordPageReader`）。**默认 ``None``，
    因此当前部署下审计日报职责不会注册**——真实读取所需的凭据与 Base ID 属 L4a 前置，
    本切片交付的是比对、渲染、发送与调度这四段，登记为 R3。
    """

    from lingxi.adapters.delegated_credentials import HostFileDelegatedCredentialVault
    from lingxi.adapters.feishu_directory import FeishuAuthorizationClient
    from lingxi.adapters.postgres_conversation import PostgresTaskQueue
    from lingxi.adapters.retention import RETENTION_CLEANUP_TIMEOUTS, PostgresRetentionCleaner

    # 一个停止标志贯穿所有职责：SIGTERM 只设它一次，全部职责同时停止领取新工作。
    stop = threading.Event()
    sink = audit if audit is not None else StructuredLogAuditSink()

    rotation = CredentialRotationLoop(
        vault=HostFileDelegatedCredentialVault(
            config.postgres_dsn,
            config.credential_key,
            config.credential_path,
            timeouts=config.postgres_timeouts,
        ),
        authorization=FeishuAuthorizationClient(
            base_url=config.feishu_base_url,
            app_id=config.feishu_app_id,
            app_secret=config.feishu_app_secret,
        ),
        interval_seconds=config.interval_seconds,
        stop=stop,
    )
    cleanup = RetentionCleanupDuty(
        # 清理函数内部两张表各有 2s lock_timeout，不能沿用 scheduler 通用的 3s
        # statement_timeout；适配器专用覆盖要大于 2×2s 累计并留出删批余量。
        cleaner=PostgresRetentionCleaner(config.postgres_dsn, timeouts=RETENTION_CLEANUP_TIMEOUTS),
        stop=stop,
    )
    idle_sweep = IdleConversationSweepDuty(
        queue=PostgresTaskQueue(config.postgres_dsn, timeouts=config.postgres_timeouts),
        idle_after=IDLE_CONVERSATION_SWEEP_AFTER,
        stop=stop,
    )

    duties: list[Any] = [rotation, cleanup, idle_sweep]
    roster_audit = _build_roster_audit_duty(
        config,
        stop=stop,
        audit=sink,
        roster_page_reader=roster_page_reader,
        on_send_outcome=(alerting_duty.send_outcome_callback() if alerting_duty else None),
    )
    if roster_audit is not None:
        duties.append(roster_audit)
    if alerting_duty is not None:
        duties.append(alerting_duty)
        if heartbeat is None:
            heartbeat = alerting_duty.heartbeat_callback("scheduler")

    return SchedulerLoop(
        duties=tuple(duties),
        interval_seconds=config.interval_seconds,
        stop=stop,
        heartbeat=heartbeat,
    )


def main(argv: list[str] | None = None) -> int:
    # 日志只到 stdout / stderr，不写文件、不自行轮转（断言 V-部署-04）。
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        config = SchedulerConfig.from_env()
    except ValueError as error:
        print(f"lingxi-scheduler 启动失败：{error}", file=sys.stderr)
        return 2
    try:
        loop = build_loop(config)
    except (RuntimeError, ValueError) as error:
        print(f"lingxi-scheduler 启动失败：{error}", file=sys.stderr)
        return 2

    install_signal_handlers(loop)
    logger.info("lingxi-scheduler 已启动 interval_seconds=%s", config.interval_seconds)
    loop.run_forever()
    return 0
