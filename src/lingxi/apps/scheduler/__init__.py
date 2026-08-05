"""``lingxi-scheduler``：定时职责进程。

首个职责是专用授权凭据（「四达文档会议助手」``refresh_token``）的到期轮换。
架构设计把定时职责单独分给本进程，理由是"定时职责与请求路径无关，混在一起会让
重启语义不清"。2026-08-05 在 `tz` 的复验实测到这条正好被违反：测试资产把续期扫描
挂在飞书长连接进程内的常驻线程上，把长连接进程 kill 掉后扫描线程无声停止，没有
任何独立信号提示"续期已经不再运行"（[Issue #16 复验记录]
(https://github.com/Moshuiwang/lingxi/issues/16#issuecomment-5188063325)）。

本模块只做组装：配置从环境变量来，轮换规则在
:mod:`lingxi.core.identity.credentials`，存取在
:mod:`lingxi.adapters.postgres_credentials`，飞书调用在
:mod:`lingxi.adapters.feishu_directory`。

退出语义（断言 V-部署-03）：收到 ``SIGTERM`` / ``SIGINT`` 后**停止领取新的到期
凭据**，把已经领取的那一次轮换做完，然后退出。半途中断一次轮换会留下一个
"已经向飞书换过、但没写回数据库"的窗口，而 ``refresh_token`` 一次性有效——
那个窗口等于凭据丢失。
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from lingxi.core.identity.credentials import AuthorizationGrant, CredentialAction, RefreshOutcome, decide_after_refresh
from lingxi.core.identity.identifiers import redact_identifier

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 60
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
    feishu_app_id: str
    feishu_app_secret: str = field(repr=False)
    feishu_base_url: str
    interval_seconds: int

    ENVIRONMENT_KEYS = (
        "LINGXI_POSTGRES_DSN",
        "LINGXI_DELEGATED_CREDENTIAL_KEY",
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
    def save(self, *, subject_open_id: str, grant: AuthorizationGrant, issued_at: Any = ...) -> None: ...
    def revoke(self, *, reason: str) -> bool: ...


class _Authorization(Protocol):
    def refresh(self, current: AuthorizationGrant) -> tuple[AuthorizationGrant, Any]: ...


class CredentialRotationLoop:
    """按飞书返回有效期的 80% 触发轮换的扫描循环。

    循环本身不判断"该不该轮换"——到期判定写在 SQL 的领取条件里（轮换点已由
    :func:`lingxi.core.identity.credentials.rotation_deadline` 算好并落库），
    失败后的处置写在 :func:`decide_after_refresh`。这里只负责编排与退出。
    """

    def __init__(self, *, vault: _Vault, authorization: _Authorization, interval_seconds: float = DEFAULT_INTERVAL_SECONDS) -> None:
        self._vault = vault
        self._authorization = authorization
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        self._stop.set()

    def run_once(self) -> RotationReport:
        """领取至多一条到期凭据并处理它。已经在停止中则一条都不领。"""

        if self._stop.is_set():
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

        if decide_after_refresh(outcome) is CredentialAction.ROTATE and replacement is not None:
            self._vault.save(subject_open_id=claim.subject_open_id, grant=replacement)
            logger.info("专用授权凭据已轮换 subject=%s", redact_identifier(claim.subject_open_id))
            return RotationReport(claimed=1, rotated=1)

        self._vault.revoke(reason=f"refresh_{outcome.value}")
        return RotationReport(claimed=1, revoked=1)

    def run_forever(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            if self._stop.is_set():
                break
            self._stop.wait(self._interval_seconds)
        logger.info("续期扫描已停止领取并退出")


def _is_definite_failure(error: BaseException) -> bool:
    """区分"飞书明确拒绝"与"结果不明确"。

    两者的处置**相同**（都撤销），区分只为了让日志与后续审计能分辨这两件事。
    传输层异常属于"我们不知道飞书那边发生了什么"。
    """

    code = getattr(error, "code", None)
    return isinstance(code, str) and code.startswith("feishu_code_")


def install_signal_handlers(loop: CredentialRotationLoop) -> None:
    """把 ``SIGTERM`` / ``SIGINT`` 接到"停止领取"上。

    处理函数只设一个事件标志，不做任何 I/O：信号处理函数里写库或发网络请求会在
    退出路径上引入新的失败模式，而这条路径恰恰是最不该出错的地方。
    """

    def handle(signal_number: int, _frame: Any) -> None:
        logger.info("收到信号，停止领取新的到期凭据 signal=%s", signal_number)
        loop.request_stop()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)


def build_loop(config: SchedulerConfig) -> CredentialRotationLoop:
    from lingxi.adapters.feishu_directory import FeishuAuthorizationClient
    from lingxi.adapters.postgres_credentials import PostgresDelegatedCredentialVault

    return CredentialRotationLoop(
        vault=PostgresDelegatedCredentialVault(config.postgres_dsn, config.credential_key),
        authorization=FeishuAuthorizationClient(
            base_url=config.feishu_base_url,
            app_id=config.feishu_app_id,
            app_secret=config.feishu_app_secret,
        ),
        interval_seconds=config.interval_seconds,
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
