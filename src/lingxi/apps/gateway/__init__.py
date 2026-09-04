"""``lingxi-gateway`` 进程：飞书长连接接入与任务入队。

[代码框架第一节](../../../../docs/技术设计/代码框架.md)登记的四进程之一，本切片
（Issue #57，S4 前半）建立它。``apps/`` 只做组装：读配置、建连接、把 adapters 注入
core、处理信号与退出，不写业务规则——处理次序住在
``lingxi.core.conversation.pipeline``，协议细节住在 ``lingxi.adapters``。

**这个进程不监听任何入站端口**（`V-接入-10`）。事件的唯一来源是那条已认证的长连接：
``LongConnectionSupervisor`` 从 ``transport.stream()` 拿事件，进程内没有第二个投递入口，
也不 ``bind`` / ``listen`` 任何套接字。接口设计 3.2 第 1 步的"验签"在长连接语义下由
握手期的通道级认证承担，逐条事件上没有可验的签名。

停机语义（`V-部署-03`）：收到 ``SIGTERM`` 后停止接收新事件、把在途事件处理完（它们
各自是一个数据库事务，要么落库要么整体回滚）、在超时内退出。信号处理器只 ``set``
一个 ``Event``，不做任何 I/O。
"""


from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from lingxi.adapters.feishu_longconn import TerminationReason
from lingxi.core.conversation.ports import OnboardingRunner

from .alerting import _LoggingAudit, _LogOnlyAlertSender, build_alerting_duty
from .assembly import ADMIN_NOTICE_UUID_PREFIX, build_supervisor
from .background_loops import (
    _combined_heartbeat,
    _combined_watchdog,
    delivery_thread_watchdog,
    run_delivery_loop,
)
from .config import GatewayConfigError, load_config
from .delivery import LOOP_ALERT_TRACE_ID
from .delivery_assembly import _RejectingCards, assemble_delivery_consumer
from .document_delivery import (
    LOOP_ALERT_TRACE_ID as DOCUMENT_DELIVERY_LOOP_ALERT_TRACE_ID,
    assemble_document_delivery_consumer,
)
from .event_handler import make_event_handler
from .group_mention_hint import GroupMentionHintResponder, build_group_mention_hint_throttle
from .log_redaction import install_credential_redaction
from .management_cards import _GatewayManagementCardRefresher, _ManagementCardRecoveryScanner
from .onboarding import _RecordingOnboarding, assert_gateway_onboarding_is_inert

logger = logging.getLogger(__name__)

#: 搬走的符号在本包入口保留转发，既有调用方与测试的 import 路径不变。
__all__ = [
    "ADMIN_NOTICE_UUID_PREFIX",
    "GroupMentionHintResponder",
    "assemble_delivery_consumer",
    "build_alerting_duty",
    "build_group_mention_hint_throttle",
    "build_supervisor",
    "delivery_thread_watchdog",
    "install_signal_handlers",
    "main",
    "make_event_handler",
    "run_delivery_loop",
    "_GatewayManagementCardRefresher",
    "_LoggingAudit",
    "_LogOnlyAlertSender",
    "_ManagementCardRecoveryScanner",
    "_RecordingOnboarding",
    "_RejectingCards",
    "_combined_heartbeat",
    "_combined_watchdog",
]


def install_signal_handlers(
    stop_event: threading.Event, *, on_stop: Callable[[], None] | None = None
) -> None:
    """``SIGTERM`` / ``SIGINT`` 只置一个标志位（外加可选的一次性回调）。

    处理器里不做 I/O：信号可能落在任意一条语句之间，在那里写库或发网络请求会把
    "停机"变成一个新的故障源。``on_stop``（独立审核 P3-5）只做一件轻量的事——
    记一个内存里的时间戳，供 ``main()`` 精确计量"停机预算从信号到达那一刻起
    用掉了多少"，不是新的 I/O 故障源，风险与 ``stop_event.set()`` 本身同级。
    """

    def handler(signum: int, _frame: Any) -> None:
        logger.info("收到信号 %s，开始停机", signum)
        stop_event.set()
        if on_stop is not None:
            on_stop()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    """进程入口。返回退出码。

    **同一进程内跑两条独立职责**：主线程承载长连接接入（``supervisor.run``，
    阻塞到收到停机信号）；投递消费循环（Issue #152）在一个后台线程里跑，共享同一个
    ``stop_event``。两者不共享任何可变状态——投递消费只读写数据库与飞书出站接口，
    不碰长连接管线的内存对象，因此不需要额外的跨线程同步。这是 #152 停止条件里
    "同一 Gateway 进程能否在不阻塞长连接的情况下可靠消费" 的落地方式：后台线程的
    数据库轮询与出站调用不会阻塞长连接协程接收下一条事件。

    **两条循环互相看着对方**（Issue #191）：投递线程经 ``run_delivery_loop`` 起跑，
    它的任何非预期退出都会立刻上报告警；长连接主线程每一轮心跳顺带跑一次
    ``delivery_thread_watchdog``。此前投递线程死掉之后没有任何进程内的东西会发现，
    只能等容器活性文件过期、健康检查变红，而 plain Docker Compose 不会因 unhealthy
    重启容器——用户侧看到的就是"发了没反应"。
    """

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    # Issue #176（安全）：第三方飞书 SDK 建立长连接后会以 INFO 级别打印带认证
    # 查询参数的完整 URL；必须在它有机会真正记一条日志之前就把两层脱敏都装好，
    # 因此紧跟在 basicConfig 之后、任何可能触发连接的代码之前调用。见
    # apps/gateway/log_redaction.py 模块头部说明。
    install_credential_redaction()
    try:
        config = load_config(env if env is not None else os.environ)
    except GatewayConfigError as error:
        print(f"gateway 配置不可用：{error}", file=sys.stderr)
        return 2

    stop_event = threading.Event()
    # 独立审核 P3-5：只在信号处理器里记一个时间戳，标记"停机预算从这一刻开始
    # 计"。不能用"进入 main() 之后的耗时"当近似——`supervisor.run()` 在收到
    # 停机信号之前会正常阻塞任意长时间（可能是几天），那段时间不属于停机预算。
    shutdown_requested_at: list[float | None] = [None]

    def _mark_shutdown_requested() -> None:
        if shutdown_requested_at[0] is None:
            shutdown_requested_at[0] = time.monotonic()

    install_signal_handlers(stop_event, on_stop=_mark_shutdown_requested)

    # 最小告警装配（Issue #153）：一份 AlertingDuty 服务两条循环，各自用不同的
    # 心跳/活性 key（见 build_alerting_duty、apps.liveness 的角色说明），因此
    # 任一条循环停摆都能被单独发现，不会被另一条仍然健康掩盖。
    alerting_duty = build_alerting_duty(config)
    delivery_alert = alerting_duty.delivery_alert_callback()
    delivery_death_reported: list[bool] = [False]

    def report_delivery_thread_dead(cause: str) -> None:
        """投递线程退出的唯一上报出口（Issue #191）。

        两个调用点（后台线程自己的退出路径、长连接主线程的看门狗）共用它，告警
        文案与"只报一次"的口径因此只有一份。上报之后**立刻**把告警冲出去：告警
        投递平时由投递循环的 ``on_tick`` 驱动，而此刻正是那条循环已经不在了，
        不主动冲一次的话这条告警只会躺在待投递队列里没人发——那就又回到了
        Issue #191 要消灭的"无声"。
        """

        if delivery_death_reported[0]:
            return
        delivery_death_reported[0] = True
        logger.error(
            "投递消费线程已退出，Gateway 仍在收消息但不会再有任何投递 cause=%s", cause
        )
        try:
            delivery_alert("delivery_loop_dead:" + cause, LOOP_ALERT_TRACE_ID)
            alerting_duty.run_once()
        except Exception as error:  # noqa: BLE001 - 告警自身失败不能再抛回退出路径
            logger.error("投递线程退出告警发送失败 error=%s", type(error).__name__)

    document_delivery_death_reported: list[bool] = [False]

    def report_document_delivery_thread_dead(cause: str) -> None:
        """文档投递消费线程退出的唯一上报出口（Issue #341 S-ES-3）。

        与 ``report_delivery_thread_dead`` 同一姿态、独立成一份——两条循环各自
        独立部署、独立轮询、互不阻塞（见 ``apps/gateway/document_delivery.py``
        模块说明「独立于既有 DeliveryConsumer」），共用同一个上报函数会让管理群
        收到的告警文案分不清究竟是哪一条循环死了。
        """

        if document_delivery_death_reported[0]:
            return
        document_delivery_death_reported[0] = True
        logger.error(
            "文档投递消费线程已退出，Gateway 仍在收消息但不会再有新文档被交付 cause=%s",
            cause,
        )
        try:
            delivery_alert(
                "document_delivery_loop_dead:" + cause, DOCUMENT_DELIVERY_LOOP_ALERT_TRACE_ID
            )
            alerting_duty.run_once()
        except Exception as error:  # noqa: BLE001 - 告警自身失败不能再抛回退出路径
            logger.error("文档投递线程退出告警发送失败 error=%s", type(error).__name__)

    # 首次开通（Epic D / S-D-02）：**gateway 只记事件**。真正的编排住在
    # `lingxi-scheduler`（产品负责人 2026-08-18 裁定），按 `claim_stale_onboarding`
    # 认领这里落下的 `auto_provisioning` 事件。本进程因此不持有任何会产生外部副作用的
    # 开通实现，也不再有开通线程池——分钟级的等待一条都不落在长连接线程或投递线程上。
    onboarding_runner: OnboardingRunner = _RecordingOnboarding()

    consumer = assemble_delivery_consumer(config, alerting_duty=alerting_duty)
    delivery_thread = threading.Thread(
        target=run_delivery_loop,
        args=(consumer,),
        kwargs={
            "stop": stop_event,
            "poll_interval_seconds": config.delivery_poll_interval_seconds,
            "heartbeat": _combined_heartbeat(alerting_duty, "gateway-delivery"),
            "on_tick": alerting_duty.run_once,
            "on_dead": report_delivery_thread_dead,
        },
        name="lingxi-gateway-delivery",
        # 独立审核 P3-5：非守护线程会让解释器在退出时等它结束——如果它没能在
        # 停机预算内 join 完，下面"进程仍将继续关闭"这句话在 daemon=False 时其实
        # 不成立（CPython 会一直等非守护线程）。改成守护线程，让"预算耗尽就不再
        # 等"这句承诺对进程本身也成立：预算内能 join 完就是干净退出；耗尽了就是
        # 进程退出时硬收掉这个线程，不会让一个卡住的外部调用把整个进程焊住。
        daemon=True,
    )

    # 文档投递独立消费循环（Issue #341 S-ES-3）：``assemble_document_delivery_
    # consumer`` 未配置 ``LINGXI_GATEWAY_TENANT_DOMAIN`` 时返回 ``None``——本进程
    # 不起第二条后台线程，watchdog 只看既有的投递消费线程，行为与本能力加入
    # 之前逐字节一致（失败关闭，见该函数文档）。**不与既有 ``delivery_thread``
    # 共用同一条循环**：见 ``apps/gateway/document_delivery.py`` 模块说明——建档
    # 四步里的真实飞书 HTTP 调用不得阻塞其他用户的终态卡片/文本送达。
    document_delivery_consumer = assemble_document_delivery_consumer(
        config, alerting_duty=alerting_duty
    )
    document_delivery_thread: threading.Thread | None = None
    if document_delivery_consumer is not None:
        document_delivery_thread = threading.Thread(
            target=run_delivery_loop,
            args=(document_delivery_consumer,),
            kwargs={
                "stop": stop_event,
                "poll_interval_seconds": config.delivery_poll_interval_seconds,
                "heartbeat": _combined_heartbeat(alerting_duty, "gateway-document-delivery"),
                "on_tick": alerting_duty.run_once,
                "on_dead": report_document_delivery_thread_dead,
            },
            name="lingxi-gateway-document-delivery",
            # 同 delivery_thread 的取舍（独立审核 P3-5）：守护线程，停机预算耗尽
            # 就不再等它，不会让一个卡住的外部调用把整个进程焊住。
            daemon=True,
        )

    # 长连接的心跳回调里挂上投递线程的看门狗（Issue #191），因此 supervisor 必须在
    # 线程对象存在之后再装配。两者之间没有别的依赖，换顺序不改变任何既有行为。
    watchdog_checks = [
        delivery_thread_watchdog(delivery_thread, stop=stop_event, on_dead=report_delivery_thread_dead)
    ]
    if document_delivery_thread is not None:
        watchdog_checks.append(
            delivery_thread_watchdog(
                document_delivery_thread,
                stop=stop_event,
                on_dead=report_document_delivery_thread_dead,
            )
        )
    supervisor_onboarding: list[OnboardingRunner] = []
    supervisor = build_supervisor(
        config,
        should_stop=stop_event.is_set,
        onboarding=onboarding_runner,
        on_onboarding_assembled=supervisor_onboarding.append,
        heartbeat=_combined_heartbeat(
            alerting_duty,
            "gateway-longconn",
            watchdog=_combined_watchdog(*watchdog_checks),
        ),
    )
    # **gateway 侧的开通装配断言**（搬迁后的形态）：本进程实际接到管线上的那个实现
    # 必须是"只记事件"的那一个。接上任何会产生外部副作用的编排，都会把分钟级的等待
    # 放回长连接线程——那正是 #65 开工卡「共用线程复核」要消灭的形状，而它在这里
    # 只会表现为"gateway 忽然收不到消息"。
    assert_gateway_onboarding_is_inert(*supervisor_onboarding)
    delivery_thread.start()
    if document_delivery_thread is not None:
        document_delivery_thread.start()

    try:
        reason = supervisor.run(should_stop=stop_event.is_set)
    finally:
        # 长连接侧已经收到停机信号（或者提前异常退出）：确保投递线程也收到同一个
        # 信号，再在停机预算内等它退出——不能让进程在后台线程还在跑外部调用时
        # 直接退出（`V-部署-03`：完成或安全中断在途工作，不是立刻放弃）。
        stop_event.set()
        if shutdown_requested_at[0] is None:
            # 没收到 SIGTERM/SIGINT 就走到这里——例如 supervisor.run() 自己因
            # 终止型错误提前返回。这种情况下还没有任何"优雅停机"流程被启动过，
            # 预算从现在开始计，而不是当成"已经用掉全部预算"。
            shutdown_requested_at[0] = time.monotonic()
        # 独立审核 P3-5：``supervisor.run()`` 从收到信号到返回，自己内部也会按
        # 同一个 ``shutdown_timeout_seconds`` 等在途事件处理完，已经消耗了停机
        # 预算的一部分。这里的 join 只用**剩余**预算，而不是重新给满一份——否则
        # 两段各按完整预算计时，最坏情况下总停机时间是承诺值的两倍。
        elapsed = time.monotonic() - shutdown_requested_at[0]
        remaining_budget = max(0.0, config.shutdown_timeout_seconds - elapsed)
        delivery_thread.join(timeout=remaining_budget)
        if delivery_thread.is_alive():
            logger.error(
                "投递消费线程未能在停机预算内退出，进程将不再等待它、直接关闭"
            )
        if document_delivery_thread is not None:
            # 与上面同一条纪律：用**剩余**预算，从信号那一刻起算，不是重新给满
            # 一份——两条线程顺序 join，第二次 join 前重新算一次剩余预算，避免
            # 第一条线程的等待时间被第二条线程重复计费。
            elapsed_after_first_join = time.monotonic() - shutdown_requested_at[0]
            remaining_after_first_join = max(
                0.0, config.shutdown_timeout_seconds - elapsed_after_first_join
            )
            document_delivery_thread.join(timeout=remaining_after_first_join)
            if document_delivery_thread.is_alive():
                logger.error(
                    "文档投递消费线程未能在停机预算内退出，进程将不再等待它、直接关闭"
                )

    if reason is TerminationReason.TERMINAL_ERROR:
        # 终止型错误（403 / 514 超连接数上限）：进程进入明确的终止态，退出码非 0，
        # 由编排层决定是否告警。**不重连**——重试一个被拒的凭据不会变成通过。
        print("gateway 因终止型接入错误退出，不再重连", file=sys.stderr)
        return 3
    return 0
