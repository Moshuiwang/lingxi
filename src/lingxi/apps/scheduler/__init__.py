"""``lingxi-scheduler``：定时职责进程。

进程现在跑**两个**职责，由 :class:`SchedulerLoop` 按同一个周期依次驱动：

1. **专用授权凭据轮换**（:class:`CredentialRotationLoop`）——「四达文档会议助手」
   ``refresh_token`` 的到期续期；
2. **九十天保留清理**（:class:`RetentionCleanupDuty`）——调用数据库里的受限清理
   函数回收到期内容（Issue #54 / S9）。

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
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from lingxi.core.identity.credentials import AuthorizationGrant, CredentialAction, RefreshOutcome, decide_after_refresh
from lingxi.core.identity.identifiers import redact_identifier

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

    ENVIRONMENT_KEYS = (
        "LINGXI_POSTGRES_DSN",
        "LINGXI_DELEGATED_CREDENTIAL_KEY",
        "LINGXI_DELEGATED_CREDENTIAL_PATH",
        "LINGXI_FEISHU_APP_ID",
        "LINGXI_FEISHU_APP_SECRET",
        "LINGXI_FEISHU_BASE_URL",
        "LINGXI_SCHEDULER_INTERVAL_SECONDS",
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

        return cls(
            postgres_dsn=_Secret(required("LINGXI_POSTGRES_DSN")),
            credential_key=_Secret(required("LINGXI_DELEGATED_CREDENTIAL_KEY")),
            credential_path=required("LINGXI_DELEGATED_CREDENTIAL_PATH"),
            feishu_app_id=required("LINGXI_FEISHU_APP_ID"),
            feishu_app_secret=_Secret(required("LINGXI_FEISHU_APP_SECRET")),
            feishu_base_url=(source.get("LINGXI_FEISHU_BASE_URL") or "").strip() or DEFAULT_FEISHU_BASE_URL,
            interval_seconds=interval,
        )


@dataclass(frozen=True)
class RotationReport:
    claimed: int = 0
    rotated: int = 0
    revoked: int = 0


class _Vault(Protocol):
    def claim_due(self) -> Any: ...
    def save(self, *, subject_open_id: str, grant: AuthorizationGrant, issued_at: Any = ..., replacing_generation: Any = ...) -> Any: ...
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
        logger.info("%s", summary() if callable(summary) else "保留清理：本轮完成")
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
    ) -> None:
        if not duties:
            raise ValueError("定时职责进程至少要有一个职责")
        self._duties = tuple(duties)
        self._interval_seconds = interval_seconds
        self._stop = threading.Event() if stop is None else stop

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


def build_loop(config: SchedulerConfig) -> SchedulerLoop:
    """装配进程的全部定时职责。

    职责顺序有意为之：凭据轮换在前。它处理的是一次性有效、有硬期限的凭据，
    而清理晚一轮没有任何后果——到期行下一轮照删，到期时间不会因为清理迟到而后移
    （断言 V-保留-16）。
    """

    from lingxi.adapters.delegated_credentials import HostFileDelegatedCredentialVault
    from lingxi.adapters.feishu_directory import FeishuAuthorizationClient
    from lingxi.adapters.retention import PostgresRetentionCleaner

    # 一个停止标志贯穿所有职责：SIGTERM 只设它一次，全部职责同时停止领取新工作。
    stop = threading.Event()

    rotation = CredentialRotationLoop(
        vault=HostFileDelegatedCredentialVault(config.postgres_dsn, config.credential_key, config.credential_path),
        authorization=FeishuAuthorizationClient(
            base_url=config.feishu_base_url,
            app_id=config.feishu_app_id,
            app_secret=config.feishu_app_secret,
        ),
        interval_seconds=config.interval_seconds,
        stop=stop,
    )
    cleanup = RetentionCleanupDuty(cleaner=PostgresRetentionCleaner(config.postgres_dsn), stop=stop)

    return SchedulerLoop(duties=(rotation, cleanup), interval_seconds=config.interval_seconds, stop=stop)


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
