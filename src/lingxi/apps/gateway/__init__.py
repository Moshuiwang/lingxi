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
from typing import Any, Callable, Mapping

from lingxi.adapters.feishu_events import (
    MESSAGE_RECEIVE_EVENT,
    EventParseError,
    NonPrivateChatError,
    parse_message_event,
)
from lingxi.adapters.feishu_longconn import (
    BackoffPolicy,
    LongConnectionSupervisor,
    TerminationReason,
)
from lingxi.apps.liveness import touch_liveness
from lingxi.core.conversation.pipeline import EventPipeline
from lingxi.core.conversation.ports import OnboardingResult, OnboardingRunner, OnboardingState

from .config import GatewayConfig, GatewayConfigError, load_config

logger = logging.getLogger(__name__)


def _combined_heartbeat(alerting_duty: Any, liveness_role: str) -> Callable[[], None]:
    """把"记进 AlertManager"与"戳一下活性文件"合成一个心跳回调（Issue #153）。

    两件事共用同一个触发时机——都是"这条循环这一轮还活着"的证据——但服务不同的
    消费者：``AlertManager`` 供跨进程重启也能观察到的阈值/去重状态机，活性文件
    供同容器内的 ``python -m lingxi.apps.healthcheck`` 判断"主循环是否还在跳动"
    （见 ``apps/liveness.py`` 模块说明）。
    """

    beat = alerting_duty.heartbeat_callback(liveness_role)

    def combined() -> None:
        beat()
        touch_liveness(liveness_role)

    return combined


class _LoggingAudit:
    """审计出口的当前实现：结构化日志。

    ``audit_event`` 表属后续切片；管线只依赖 ``AuditSink`` 的签名，届时换实现不动管线。
    这里**不记录消息正文**——未开通用户的内容"不保存"包括不写进日志。
    """

    def record(self, action: str, /, **fields: object) -> None:
        logger.info("audit %s %s", action, fields)


class _UnavailableOnboarding:
    """外部开通编排未装配时的失败关闭实现。

    正向 gateway 接线仍然认领事件，但绝不假装匹配或开通成功；正式部署应把
    #89/#17 编排 runner 传入 ``build_supervisor``。这也让漏装配表现为冻结的
    ``LX-ONBOARD-001`` 终态，而不是静默回到旧的「未开通」分支。
    """

    def start(self, *, event_id: str, open_id: str, trace_id: str) -> OnboardingResult:
        del event_id, open_id, trace_id
        return OnboardingResult(
            state=OnboardingState.INTERNAL_ERROR,
            failure_reason="onboarding_runner_unavailable",
        )


def make_event_handler(
    pipeline: EventPipeline, *, audit: Any, on_parse_error: Callable[[str], None] | None = None
) -> Callable[[dict], None]:
    """把原始事件体接到管线上。

    只处理 ``im.message.receive_v1``。其余类型（``card.action.trigger``、
    ``im.chat.member.bot.deleted_v1``）本批不处理，但收到时必须**不致长连接崩溃**
    ——直接返回，由 supervisor 记审计（`V-接入-12`）。
    """

    def handle(payload: dict) -> None:
        header = payload.get("header") if isinstance(payload, dict) else None
        event_type = header.get("event_type") if isinstance(header, dict) else None
        if event_type != MESSAGE_RECEIVE_EVENT:
            audit.record("event.ignored", event_type=event_type)
            return
        try:
            message = parse_message_event(payload)
        except NonPrivateChatError as error:
            # 群聊越界：合同「问数与多轮对话」只适用于飞书私聊入口。在这里就返回，
            # 因此**不加表情、不回复、不入队**——加表情本身也是一个用户可见动作，
            # 在群里给一条消息加表情等于宣告「我在这个群里工作」。
            audit.record("event.rejected_non_private_chat", chat_type=error.chat_type)
            return
        except EventParseError as error:
            # 读不懂的事件体记审计后继续收下一条，不抛给 supervisor 当成连接故障。
            audit.record("event.unparsable", error=str(error))
            if on_parse_error is not None:
                on_parse_error(str(error))
            return
        pipeline.handle_message(message)

    return handle


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


def build_supervisor(
    config: GatewayConfig,
    *,
    transport: Any = None,
    should_stop: Callable[[], bool] | None = None,
    onboarding: OnboardingRunner | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> LongConnectionSupervisor:
    """按配置装出一个 supervisor。

    ``transport`` 和 ``onboarding`` 都是注入口：真实长连接与真实身份/权限外部链属
    L4a，全部 L2/L3 断言注入假实现。``onboarding`` 未提供时采用失败关闭 runner，
    不会把未开通正文悄悄交给下游，也不会误报已开通。
    adapters 在函数体内延迟 import，与 ``apps/scheduler`` 的 ``build_loop`` 同惯例。
    """

    from lingxi.adapters.feishu_longconn import LarkEventTransport
    from lingxi.adapters.feishu_outbound import LarkReactions, LarkReplies, build_client
    from lingxi.adapters.postgres_conversation import PostgresGatewayStore

    audit = _LoggingAudit()
    effective_onboarding = onboarding or _UnavailableOnboarding()
    # 出站 HTTP 的超时从停机预算里分配，而不是用 SDK 的 30 秒默认值——后者比预算
    # 本身还长，一次卡住的加表情或回复就能让停机超出承诺（codex 二轮 P1-C）。
    # 取四分之一：一条事件最多经历「加表情 + 一次回复」两次出站，各留一份余量。
    outbound_timeout = max(1.0, config.shutdown_timeout_seconds / 4)
    client = build_client(
        app_id=config.app_id,
        app_secret=str(config.app_secret),
        timeout_seconds=outbound_timeout,
    )
    pipeline = EventPipeline(
        store=PostgresGatewayStore(str(config.postgres_dsn), timeouts=config.postgres_timeouts),
        reactions=LarkReactions(client),
        replies=LarkReplies(client),
        audit=audit,
        onboarding=effective_onboarding,
        # 停机时跳过尽力而为的出站回复，不让它把停机拖过预算。
        should_stop=should_stop,
    )
    return LongConnectionSupervisor(
        transport=transport
        or LarkEventTransport(
            app_id=config.app_id,
            app_secret=str(config.app_secret),
            # 停机信号最晚在一个空闲轮询间隔之后被看见，因此这个间隔必须由停机
            # 超时推导，而不是取一个与超时无关的常数——否则配置里的超时就是一句
            # 没有实现的承诺（独立复查 F4）。取四分之一，给「处理完在途事件 + 退出」
            # 留余量：实际退出耗时 ≈ 轮询间隔 + 一条在途事件的处理时间。
            poll_seconds=max(0.1, config.shutdown_timeout_seconds / 4),
            # 单条事件从收到到落库有结果的上限。超过就让 SDK 向飞书回失败、由平台
            # 重投，而不是无限期占住它的接收协程。取停机超时本身：比它更长的话，
            # 一条卡住的事件就能让停机超出承诺。
            ack_timeout_seconds=config.shutdown_timeout_seconds,
            # 建连截止时间：超时未连上即判失败进重连，堵住「从未连上」的活性黑洞。
            handshake_timeout_seconds=config.shutdown_timeout_seconds,
        ),
        handle_event=make_event_handler(pipeline, audit=audit),
        backoff=BackoffPolicy(
            base_seconds=config.reconnect_base_seconds,
            factor=config.reconnect_factor,
            ceiling_seconds=config.reconnect_ceiling_seconds,
        ),
        audit=audit.record,
        heartbeat=heartbeat,
    )


def assemble_delivery_consumer(
    config: GatewayConfig, *, queue: Any = None, alerting_duty: Any = None
) -> Any:
    """装配投递消费循环（Issue #152）：读 outbox、驱动 CardKit 流式卡片与文本兜底。

    独立建一个飞书 SDK 客户端，而不是复用 ``build_supervisor`` 内部那一个——两者
    生命周期不同（这一个要跟着后台线程一起停），共用同一个客户端对象反而会让"谁
    负责关它"变得含糊；多一个轻量 SDK 客户端对象本身不产生任何网络连接，直到第一次
    真正调用才建立 HTTP 连接。

    ``alerting_duty``（Issue #153）不为 ``None`` 时，把它的
    ``delivery_alert_callback()`` 接到 ``DeliveryConsumer.on_alert``——这是最小告警
    装配合同点名的注入点："把 #152 的 on_alert 注入点接到真实告警路由"。
    """

    from lingxi.adapters.feishu_outbound import build_client
    from lingxi.adapters.postgres_conversation import PostgresTaskQueue
    from lingxi.apps.gateway.delivery import build_delivery_consumer

    outbound_timeout = max(1.0, config.shutdown_timeout_seconds / 4)
    client = build_client(
        app_id=config.app_id,
        app_secret=str(config.app_secret),
        timeout_seconds=outbound_timeout,
    )
    return build_delivery_consumer(
        client=client,
        queue=queue
        or PostgresTaskQueue(str(config.postgres_dsn), timeouts=config.postgres_timeouts),
        limit=config.delivery_batch_limit,
        on_alert=alerting_duty.delivery_alert_callback() if alerting_duty is not None else None,
    )


class _LogOnlyAlertSender:
    """gateway 未配置管理群时的告警出口：只记结构化日志，不发起网络请求。

    与 ``apps/worker/cli.py`` 的同名类同一姿态——没有配置目标群不等于告警关闭，
    只是"发送"这一步落到日志（Issue #153：合同要求"告警不可用时主流程行为有
    明确定义"，这里的定义是"继续跑，只是暂时没有群通知"）。
    """

    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None:
        del chat_id, dedupe_key
        logging.getLogger("lingxi.apps.gateway.alert").warning(text)


def build_alerting_duty(config: GatewayConfig) -> Any:
    """装配 gateway 自己的告警状态机（Issue #153）。

    gateway 与 scheduler、worker 是三个独立部署单元，进程间不直接通信，因此各自
    持有一份 ``AlertManager``——这不是重复造轮子，是"告警状态机跟着部署单元走"
    这条既有约束的自然结果（``core/alerting.py`` 模块说明）。配置了
    ``LINGXI_GATEWAY_ADMIN_GROUP_CHAT_ID`` 时真正发进管理群；没配时状态机照常
    运行（阈值、去重、恢复计时都真实生效），只是发送端退化为结构化日志。
    """

    from lingxi.core.alerting import AlertDispatcher, AlertingDuty, AlertManager

    if config.admin_group_chat_id:
        from lingxi.adapters.feishu_group_message import FeishuGroupMessages

        sender: Any = FeishuGroupMessages(
            base_url=config.feishu_base_url,
            app_id=config.app_id,
            app_secret=str(config.app_secret),
        )
        chat_id = config.admin_group_chat_id
    else:
        sender = _LogOnlyAlertSender()
        chat_id = "gateway-log-only"

    return AlertingDuty(
        manager=AlertManager(policy=config.alert_policy),
        dispatcher=AlertDispatcher(sender=sender, chat_id=chat_id, policy=config.alert_policy),
        audit=_LoggingAudit(),
    )


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    """进程入口。返回退出码。

    **同一进程内跑两条独立职责**：主线程承载长连接接入（``supervisor.run``，
    阻塞到收到停机信号）；投递消费循环（Issue #152）在一个后台线程里跑，共享同一个
    ``stop_event``。两者不共享任何可变状态——投递消费只读写数据库与飞书出站接口，
    不碰长连接管线的内存对象，因此不需要额外的跨线程同步。这是 #152 停止条件里
    "同一 Gateway 进程能否在不阻塞长连接的情况下可靠消费" 的落地方式：后台线程的
    数据库轮询与出站调用不会阻塞长连接协程接收下一条事件。
    """

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
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
    supervisor = build_supervisor(
        config,
        should_stop=stop_event.is_set,
        heartbeat=_combined_heartbeat(alerting_duty, "gateway-longconn"),
    )
    consumer = assemble_delivery_consumer(config, alerting_duty=alerting_duty)
    delivery_thread = threading.Thread(
        target=consumer.run_forever,
        kwargs={
            "stop": stop_event,
            "poll_interval_seconds": config.delivery_poll_interval_seconds,
            "heartbeat": _combined_heartbeat(alerting_duty, "gateway-delivery"),
            "on_tick": alerting_duty.run_once,
        },
        name="lingxi-gateway-delivery",
        # 独立审核 P3-5：非守护线程会让解释器在退出时等它结束——如果它没能在
        # 停机预算内 join 完，下面"进程仍将继续关闭"这句话在 daemon=False 时其实
        # 不成立（CPython 会一直等非守护线程）。改成守护线程，让"预算耗尽就不再
        # 等"这句承诺对进程本身也成立：预算内能 join 完就是干净退出；耗尽了就是
        # 进程退出时硬收掉这个线程，不会让一个卡住的外部调用把整个进程焊住。
        daemon=True,
    )
    delivery_thread.start()

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

    if reason is TerminationReason.TERMINAL_ERROR:
        # 终止型错误（403 / 514 超连接数上限）：进程进入明确的终止态，退出码非 0，
        # 由编排层决定是否告警。**不重连**——重试一个被拒的凭据不会变成通过。
        print("gateway 因终止型接入错误退出，不再重连", file=sys.stderr)
        return 3
    return 0
