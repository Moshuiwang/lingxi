"""``lingxi-gateway`` 进程：飞书长连接接入与任务入队。

``apps/`` 只做组装：读配置、建连接、把 adapters 注入 core、处理信号与退出，不写业务
规则——处理次序住在 ``lingxi.core.conversation.pipeline``，协议细节住在
``lingxi.adapters``，装配住在 :mod:`~lingxi.apps.gateway.assembly`。

**这个进程不监听任何入站端口**（`V-接入-10`）。事件的唯一来源是那条已认证的长连接，
进程内没有第二个投递入口，也不 ``bind`` / ``listen`` 任何套接字。接口设计 3.2 第 1 步
的"验签"在长连接语义下由握手期的通道级认证承担，逐条事件上没有可验的签名。

停机语义（`V-部署-03`）：收到 ``SIGTERM`` 后停止接收新事件、把在途事件处理完（它们
各自是一个数据库事务，要么落库要么整体回滚）、在超时内退出。信号处理器只置一个标志
位，不做任何 I/O。
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from lingxi.adapters.feishu_longconn import TerminationReason
from lingxi.adapters.postgres import close_idle_connections
from lingxi.core.conversation.ports import OnboardingRunner

from .alerting import LogOnlyAlertSender, build_alerting_duty
from .assembly import ADMIN_NOTICE_UUID_PREFIX, build_supervisor
from .audit_log import LoggingAudit
from .background_loops import (
    combined_heartbeat,
    combined_watchdog,
    delivery_thread_watchdog,
    run_delivery_loop,
)
from .config import GatewayConfig, GatewayConfigError, load_config
from .delivery import LOOP_ALERT_TRACE_ID
from .delivery_assembly import RejectingCards, assemble_delivery_consumer
from .document_delivery import (
    LOOP_ALERT_TRACE_ID as DOCUMENT_DELIVERY_LOOP_ALERT_TRACE_ID,
)
from .document_delivery import (
    assemble_document_delivery_consumer,
)
from .event_handler import make_event_handler
from .group_mention_hint import GroupMentionHintResponder, build_group_mention_hint_throttle
from .log_redaction import install_credential_redaction
from .management_cards import ManagementCardRecoveryScanner, ManagementCardRefresher
from .onboarding import _RecordingOnboarding, assert_gateway_onboarding_is_inert

logger = logging.getLogger(__name__)

# 搬走的符号在本包入口保留转发，既有调用方与测试的 import 路径不变。
_LoggingAudit = LoggingAudit
_LogOnlyAlertSender = LogOnlyAlertSender
_RejectingCards = RejectingCards
_GatewayManagementCardRefresher = ManagementCardRefresher
_ManagementCardRecoveryScanner = ManagementCardRecoveryScanner
_combined_heartbeat = combined_heartbeat
_combined_watchdog = combined_watchdog

__all__ = [
    "ADMIN_NOTICE_UUID_PREFIX",
    "GroupMentionHintResponder",
    "LoggingAudit",
    "LogOnlyAlertSender",
    "ManagementCardRecoveryScanner",
    "ManagementCardRefresher",
    "RejectingCards",
    "assemble_delivery_consumer",
    "build_alerting_duty",
    "build_group_mention_hint_throttle",
    "build_supervisor",
    "combined_heartbeat",
    "combined_watchdog",
    "delivery_thread_watchdog",
    "install_signal_handlers",
    "main",
    "make_event_handler",
    "run_delivery_loop",
]


def install_signal_handlers(
    stop_event: threading.Event, *, on_stop: Callable[[], None] | None = None
) -> None:
    """``SIGTERM`` / ``SIGINT`` 只置一个标志位，外加一个可选的一次性回调。

    处理器里不做 I/O：信号可能落在任意一条语句之间，在那里写库或发网络请求会把"停机"
    变成一个新的故障源。``on_stop`` 只做一件轻量的事——记一个内存里的时间戳，风险与
    置标志位本身同级。
    """

    def handler(signum: int, _frame: Any) -> None:
        logger.info("收到信号 %s，开始停机", signum)
        stop_event.set()
        if on_stop is not None:
            on_stop()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


@dataclass
class _ShutdownClock:
    """停机预算的计时起点：信号到达的那一刻。

    不能用"进入 ``main()`` 之后的耗时"当近似——长连接在收到停机信号之前会正常阻塞
    任意长时间（可能是几天），那段时间不属于停机预算。
    """

    requested_at: float | None = None

    def mark_requested(self) -> None:
        """记下第一次收到停机信号的时刻；重复调用不改写。"""
        if self.requested_at is None:
            self.requested_at = time.monotonic()

    def remaining(self, budget_seconds: float) -> float:
        """还剩多少停机预算。

        两段等待（长连接自己等在途事件、这里 join 后台线程）共用同一份预算：各按完整
        预算计时的话，最坏情况下总停机时间会是承诺值的两倍。
        """
        if self.requested_at is None:
            self.mark_requested()
        assert self.requested_at is not None
        return max(0.0, budget_seconds - (time.monotonic() - self.requested_at))


@dataclass
class _BackgroundLoops:
    """两条后台投递循环的线程与它们各自的看门狗。"""

    delivery: threading.Thread
    document_delivery: threading.Thread | None = None
    watchdogs: list[Callable[[], None]] = field(default_factory=list)

    def start(self) -> None:
        """启动全部后台线程。"""
        self.delivery.start()
        if self.document_delivery is not None:
            self.document_delivery.start()

    def join_within(self, clock: _ShutdownClock, budget_seconds: float) -> None:
        """在**剩余**停机预算内依次等两条线程退出；等不到就放弃并留一条错误日志。"""
        self.delivery.join(timeout=clock.remaining(budget_seconds))
        if self.delivery.is_alive():
            logger.error("投递消费线程未能在停机预算内退出，进程将不再等待它、直接关闭")
        if self.document_delivery is None:
            return
        self.document_delivery.join(timeout=clock.remaining(budget_seconds))
        if self.document_delivery.is_alive():
            logger.error("文档投递消费线程未能在停机预算内退出，进程将不再等待它、直接关闭")


def _thread_death_reporter(
    *,
    alerting_duty: Any,
    delivery_alert: Callable[[str, str], None],
    dead_message: str,
    alert_key: str,
    alert_trace_id: str,
    alert_failure_message: str,
) -> Callable[[str], None]:
    """一条后台循环退出时的唯一上报出口。

    两个调用点（线程自己的退出路径、长连接主线程的看门狗）共用它，告警文案与「只报一次」
    的口径因此只有一份。上报之后**立刻**把告警冲出去：告警投递平时由投递循环驱动，而此刻
    正是那条循环已经不在了，不主动冲一次的话这条告警只会躺在队列里没人发。

    两条循环各建一个实例而不是共用：它们独立部署、独立轮询、互不阻塞，共用同一个出口会让
    管理群收到的告警分不清究竟是哪一条死了。
    """
    reported = [False]

    def report(cause: str) -> None:
        if reported[0]:
            return
        reported[0] = True
        logger.error(dead_message, cause)
        try:
            delivery_alert(alert_key + cause, alert_trace_id)
            alerting_duty.run_once()
        except Exception as error:
            logger.error(alert_failure_message, type(error).__name__)

    return report


def _build_loop_thread(
    consumer: Any,
    *,
    name: str,
    config: GatewayConfig,
    stop_event: threading.Event,
    alerting_duty: Any,
    liveness_role: str,
    on_dead: Callable[[str], None],
) -> threading.Thread:
    """把一个投递消费者包成后台线程。

    守护线程是刻意的：非守护线程会让解释器在退出时一直等它，那样"预算耗尽就不再等"
    这句承诺对进程本身就不成立——一个卡住的外部调用能把整个进程焊死。
    """
    return threading.Thread(
        target=run_delivery_loop,
        args=(consumer,),
        kwargs={
            "stop": stop_event,
            "poll_interval_seconds": config.delivery_poll_interval_seconds,
            "heartbeat": combined_heartbeat(alerting_duty, liveness_role),
            "on_tick": alerting_duty.run_once,
            "on_dead": on_dead,
        },
        name=name,
        daemon=True,
    )


def _start_background_loops(
    config: GatewayConfig, *, stop_event: threading.Event, alerting_duty: Any
) -> _BackgroundLoops:
    """装出两条后台投递循环及其看门狗（线程本身还没启动）。

    文档投递是可选的：未配置它需要的域名时不起第二条线程，看门狗也只看既有那一条，
    行为与这条能力加入之前逐字节一致。两条循环刻意**不共用同一条循环**——建档那几步
    的真实外部调用不得阻塞其他用户的终态送达。
    """
    delivery_alert = alerting_duty.delivery_alert_callback()
    on_delivery_dead = _thread_death_reporter(
        alerting_duty=alerting_duty,
        delivery_alert=delivery_alert,
        dead_message="投递消费线程已退出，Gateway 仍在收消息但不会再有任何投递 cause=%s",
        alert_key="delivery_loop_dead:",
        alert_trace_id=LOOP_ALERT_TRACE_ID,
        alert_failure_message="投递线程退出告警发送失败 error=%s",
    )
    delivery = _build_loop_thread(
        assemble_delivery_consumer(config, alerting_duty=alerting_duty),
        name="lingxi-gateway-delivery",
        config=config,
        stop_event=stop_event,
        alerting_duty=alerting_duty,
        liveness_role="gateway-delivery",
        on_dead=on_delivery_dead,
    )
    loops = _BackgroundLoops(
        delivery=delivery,
        watchdogs=[delivery_thread_watchdog(delivery, stop=stop_event, on_dead=on_delivery_dead)],
    )

    document_consumer = assemble_document_delivery_consumer(config, alerting_duty=alerting_duty)
    if document_consumer is None:
        return loops
    on_document_dead = _thread_death_reporter(
        alerting_duty=alerting_duty,
        delivery_alert=delivery_alert,
        dead_message=("文档投递消费线程已退出，Gateway 仍在收消息但不会再有新文档被交付 cause=%s"),
        alert_key="document_delivery_loop_dead:",
        alert_trace_id=DOCUMENT_DELIVERY_LOOP_ALERT_TRACE_ID,
        alert_failure_message="文档投递线程退出告警发送失败 error=%s",
    )
    loops.document_delivery = _build_loop_thread(
        document_consumer,
        name="lingxi-gateway-document-delivery",
        config=config,
        stop_event=stop_event,
        alerting_duty=alerting_duty,
        liveness_role="gateway-document-delivery",
        on_dead=on_document_dead,
    )
    loops.watchdogs.append(
        delivery_thread_watchdog(loops.document_delivery, stop=stop_event, on_dead=on_document_dead)
    )
    return loops


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    """进程入口。返回退出码。

    **同一进程内跑两条独立职责**：主线程承载长连接接入（阻塞到收到停机信号）；投递
    消费在后台线程里跑，共享同一个停机信号。两者不共享任何可变状态——投递只读写数据库
    与出站接口，不碰长连接管线的内存对象，因此不需要额外的跨线程同步。

    **两条循环互相看着对方**：后台线程的任何非预期退出都会立刻上报告警；长连接主线程
    每一轮心跳顺带确认后台线程还活着。没有这一层时，投递线程死掉之后进程内没有任何东西
    会发现，只能等容器活性文件过期——而那不会触发重启，用户侧看到的就是"发了没反应"。
    """
    del argv
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    # 第三方 SDK 建立长连接后会以 INFO 级别打印带认证查询参数的完整 URL；必须在它有
    # 机会真正记一条日志之前就把脱敏装好，因此紧跟在日志初始化之后、任何可能触发连接
    # 的代码之前调用。
    install_credential_redaction()
    try:
        config = load_config(env if env is not None else os.environ)
    except GatewayConfigError as error:
        print(f"gateway 配置不可用：{error}", file=sys.stderr)
        return 2
    return _run(config)


def _run(config: GatewayConfig) -> int:
    """装配两条循环与长连接，跑到停机，再在预算内收尾。"""
    stop_event = threading.Event()
    shutdown = _ShutdownClock()
    install_signal_handlers(stop_event, on_stop=shutdown.mark_requested)

    # 一份告警职责服务两条循环，各自用不同的心跳与活性角色，因此任一条停摆都能被单独
    # 发现，不会被另一条仍然健康掩盖。
    alerting_duty = build_alerting_duty(config)
    loops = _start_background_loops(config, stop_event=stop_event, alerting_duty=alerting_duty)

    # 心跳回调里挂着后台线程的看门狗，因此 supervisor 必须在线程对象存在之后再装配。
    assembled: list[OnboardingRunner] = []
    supervisor = build_supervisor(
        config,
        should_stop=stop_event.is_set,
        onboarding=_RecordingOnboarding(),
        on_onboarding_assembled=assembled.append,
        heartbeat=combined_heartbeat(
            alerting_duty, "gateway-longconn", watchdog=combined_watchdog(*loops.watchdogs)
        ),
    )
    # 本进程实际接到管线上的开通实现必须是"只记事件"的那一个：接上任何会产生外部副作用
    # 的编排，都会把分钟级的等待放回长连接线程，而现场表现只是"gateway 忽然收不到消息"。
    assert_gateway_onboarding_is_inert(*assembled)
    loops.start()

    try:
        reason = supervisor.run(should_stop=stop_event.is_set)
    finally:
        # 长连接侧已经收到停机信号（或提前异常退出）：确保后台线程也收到同一个信号，
        # 再在停机预算内等它们退出——不能让进程在后台还在跑外部调用时直接走人。
        stop_event.set()
        loops.join_within(shutdown, config.shutdown_timeout_seconds)
        # 所有职责与后台线程都已收口：显式关闭本进程空闲栈里的连接，不再只靠
        # atexit。清理本身的异常不得覆盖上面可能已经在传播的真实故障，只记日志不重抛。
        try:
            close_idle_connections()
        except Exception as error:
            logger.error("gateway 停机清理空闲数据库连接失败 error=%s", type(error).__name__)

    if reason is TerminationReason.TERMINAL_ERROR:
        # 终止型错误（凭据被拒、超连接数上限）：进入明确的终止态，退出码非 0，由编排层
        # 决定是否告警。**不重连**——重试一个被拒的凭据不会变成通过。
        print("gateway 因终止型接入错误退出，不再重连", file=sys.stderr)
        return 3
    return 0
