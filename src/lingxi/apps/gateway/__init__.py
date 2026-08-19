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
from lingxi.core.execution.card_stream import CardCreated, CardTransport, DeliveryRejected

from .config import GatewayConfig, GatewayConfigError, load_config
from .delivery import LOOP_ALERT_TRACE_ID
from .log_redaction import install_credential_redaction
from .onboarding import assert_gateway_onboarding_is_inert

logger = logging.getLogger(__name__)


def _combined_heartbeat(
    alerting_duty: Any, liveness_role: str, *, watchdog: Callable[[], None] | None = None
) -> Callable[[], None]:
    """把"记进 AlertManager"与"戳一下活性文件"合成一个心跳回调（Issue #153）。

    两件事共用同一个触发时机——都是"这条循环这一轮还活着"的证据——但服务不同的
    消费者：``AlertManager`` 供跨进程重启也能观察到的阈值/去重状态机，活性文件
    供同容器内的 ``python -m lingxi.apps.healthcheck`` 判断"主循环是否还在跳动"
    （见 ``apps/liveness.py`` 模块说明）。

    ``watchdog``（Issue #191）是搭在同一个时机上的第三件事：**这条循环还活着的
    时候，顺手确认另一条循环也还活着**。放在心跳之后调用，一次看门狗异常不会
    连累前面两件真正的心跳工作。
    """

    beat = alerting_duty.heartbeat_callback(liveness_role)

    def combined() -> None:
        beat()
        touch_liveness(liveness_role)
        if watchdog is not None:
            watchdog()

    return combined


def run_delivery_loop(
    consumer: Any,
    *,
    stop: threading.Event,
    poll_interval_seconds: float,
    heartbeat: Callable[[], None] | None = None,
    on_tick: Callable[[], None] | None = None,
    on_dead: Callable[[str], None],
) -> None:
    """投递消费后台线程的实际入口：跑循环，并保证它一旦退出一定有人知道（#191）。

    ``DeliveryConsumer.run_forever`` 自己已经把单轮异常隔离掉，不会因为一次瞬时
    数据库错误退出；但"线程没了"这件事不能只由那一层保证——解释器级错误
    （``MemoryError`` 一类 ``BaseException``）、或将来某次改动把隔离改漏，都会让
    这条线程无声消失。此前 ``main()`` 只在 ``supervisor.run()`` 返回后才 ``join``，
    于是"Gateway 照常收消息、却一条也投不出去"要等容器活性文件过期才被健康检查
    间接发现，而 plain Docker Compose 并不会因为 unhealthy 重启容器（Issue #191）。

    因此 ``on_dead`` 覆盖两条退出路径：异常退出（连 ``BaseException`` 一起）、以及
    "没有收到停机信号却正常返回"。停机信号已置位时的正常返回是**预期**退出，
    不上报——那是优雅停机，不是故障。
    """

    try:
        consumer.run_forever(
            stop=stop,
            poll_interval_seconds=poll_interval_seconds,
            heartbeat=heartbeat,
            on_tick=on_tick,
        )
    except BaseException as error:
        # 原样抛回线程：Python 会打印完整栈，排查时那份栈是唯一能定位到具体
        # 语句的证据；告警只带异常类型，不带正文。
        on_dead(type(error).__name__)
        raise
    if not stop.is_set():
        on_dead("returned_without_stop")


def delivery_thread_watchdog(
    thread: threading.Thread, *, stop: threading.Event, on_dead: Callable[[str], None]
) -> Callable[[], None]:
    """给长连接主线程用的看门狗（Issue #191）：确认投递线程还活着。

    ``main()`` 的主线程一直阻塞在 ``supervisor.run()`` 里直到停机，它没有别的机会
    发现后台线程已经死亡。长连接每一轮（含没有用户消息时的空闲心跳）都会调用一次
    心跳回调，把这次检查挂在同一个时机上：不新增线程、不新增定时器，也不改变任何
    既有的停机语义。

    只上报一次——线程死亡是不可逆状态，每一轮重复上报只会刷屏。线程还没 ``start()``
    时 ``ident`` 为 ``None``，那是"尚未开始"而不是"已经死亡"，不上报。
    """

    reported = [False]

    def check() -> None:
        if reported[0] or stop.is_set():
            return
        if thread.ident is None or thread.is_alive():
            return
        reported[0] = True
        on_dead("thread_not_alive")

    return check


class _LoggingAudit:
    """审计出口的当前实现：结构化日志。

    ``audit_event`` 表属后续切片；管线只依赖 ``AuditSink`` 的签名，届时换实现不动管线。
    这里**不记录消息正文**——未开通用户的内容"不保存"包括不写进日志。

    失败类动作（``reaction.failed``、``reply.failed``、``event.handler_failed``、
    ``event.unparsable`` 等）记 ``WARNING`` 而不是 ``INFO``：S-A-07 r15/r19 真实验收
    发现「已收到」表情缺失（#175/#185）时，唯一能回答"加表情调用到底怎么失败的"
    的证据就是 ``reaction.failed`` 这一行审计——它淹没在 INFO 级正常流水里，验收
    没有捕获到，问题因此无法定位。级别只影响日志可见性，动作名与字段不变，
    重放脚本 ``_AuditCapture``（level=INFO 的 Handler）仍照常收到这些记录。

    后缀规则之外还有一个显式名单（独立审核 F5）：``message.unsupported_type``
    不以失败后缀结尾，但它是"用户发了消息却什么都没发生"的唯一入站侧证据
    （非文本消息被判不支持、不建任务）——r19 首轮误判正是这一类。名单只收
    **用户本应得到回应却什么都没发生**的动作。据此：未开通、已停用这类有明确
    用户回复的拒绝分支不在此列；``event.rejected_non_private_chat`` 也不在此列，
    但理由不同（PR #186 补审 P3-6）——群聊拒绝**不加表情、不回复、不入队**
    （见上方 ``NonPrivateChatError`` 分支），这份静默是刻意的产品边界：机器人
    不在群里暴露任何工作痕迹。既然"什么都不发生"就是这条分支承诺的结果，它就
    不是诊断缺口，维持 ``INFO``。停机期间的 ``reply.skipped_while_stopping``
    属正常停机路径，同样不在此列。
    """

    _EXTRA_WARNING_ACTIONS = frozenset({"message.unsupported_type"})

    def record(self, action: str, /, **fields: object) -> None:
        promote = (
            action.endswith(("failed", "error", "unparsable"))
            or action in self._EXTRA_WARNING_ACTIONS
        )
        log = logger.warning if promote else logger.info
        log("audit %s %s", action, fields)


class _RecordingOnboarding:
    """gateway 侧的开通"编排"：**只记事件，一个外部动作都不做**。

    产品负责人 2026-08-18 裁定把首次开通编排整体移进 ``lingxi-scheduler``（决策记录见
    ``docs/决策记录/2026-08-18-首次开通编排住在scheduler.md``）。gateway 从此只做两件事：
    把首聊事件落进 ``inbound_event`` 并标成 ``auto_provisioning``（这一步由管线的事务完成），
    以及立刻回一条合同要求的「已收到，正在核对」。真正的编排由 scheduler 按
    ``claim_stale_onboarding`` 认领。

    因此本类返回 ``STARTED``：它的字面含义正是"编排已异步接手、这一轮没有别的话要说"
    （见 ``ports.OnboardingState``），而管线对 ``started`` **刻意不记账**——账本留给真正
    跑完的那一方，中途崩溃的链因此仍然可以被重新认领。

    **不是失败关闭桩。** 桩会返回 ``INTERNAL_ERROR``，让每个未开通用户当场看到
    ``LX-ONBOARD-001``；而这里的语义是"收到了、正在处理"，是真话。
    """

    def start(self, *, event_id: str, open_id: str, trace_id: str) -> OnboardingResult:
        del event_id, open_id, trace_id
        return OnboardingResult(state=OnboardingState.STARTED)


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
    on_onboarding_assembled: Callable[[OnboardingRunner], None] | None = None,
) -> LongConnectionSupervisor:
    """按配置装出一个 supervisor。

    ``transport`` 和 ``onboarding`` 都是注入口：真实长连接与真实身份/权限外部链属
    L4a，全部 L2/L3 断言注入假实现。``onboarding`` 未提供时采用失败关闭 runner，
    不会把未开通正文悄悄交给下游，也不会误报已开通。
    adapters 在函数体内延迟 import，与 ``apps/scheduler`` 的 ``build_loop`` 同惯例。

    ``on_onboarding_assembled`` 回调**只报告本函数最终采用的那个 runner**，供装配层做
    开通装配断言（``apps/gateway/onboarding.assert_gateway_onboarding_is_inert``）。
    为什么要一个回调而不是从返回的 supervisor 上读回来：``LongConnectionSupervisor``
    的公开面被 `V-接入-10` 的结构断言冻结成 ``{run, reconnect_attempts,
    observed_delays}``——它不得出现第二个可以投递事件的公开入口，因此也不该为了装配
    自证而长出新的公开属性。回调不接触事件通路，只把"本函数决定用哪一个"这件事说出来，
    而那正是断言要验的东西（缺省落桩就发生在这一行）。
    """

    from lingxi.adapters.feishu_longconn import LarkEventTransport
    from lingxi.adapters.feishu_outbound import LarkReactions, LarkReplies, build_client
    from lingxi.adapters.postgres_conversation import PostgresGatewayStore

    audit = _LoggingAudit()
    effective_onboarding = onboarding or _RecordingOnboarding()
    if on_onboarding_assembled is not None:
        on_onboarding_assembled(effective_onboarding)
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


class _RejectingCards:
    """S-A-07 受控验收缺口专用：让配置命中的那一步卡片外发调用确定性收到
    ``DeliveryRejected``（服务端明确拒绝），用于在没有真实故障可复现的情况下证伪
    #152「关闭卡片路径 + 同话题一次完整文本终态」的降级路径（验收缺口登记于 #152、
    #154 评论 5306860510、#162 E-022）。**只在显式设置
    ``LINGXI_GATEWAY_CARD_FAILURE_INJECT`` 时才会被装配**；未设置（默认）时
    ``assemble_delivery_consumer`` 走 ``build_delivery_consumer`` 的默认参数，
    与本类加入之前的装配路径逐字节一致。

    设计取舍：只让被选中的那一步失败，未选中的步骤直通真实 transport——
    ``create`` 命中时 ``CardStream.start()`` 只会捕获 ``DeliveryRejected`` 并整体
    降级为文本兜底，``update``/``close`` 根本不会再被这次任务调用到。

    **四个值的实测语义（独立审核 P2-1 修正，覆盖此前文档的错误描述）**：

    - ``create``/``all`` 在正常单轮场景下**等价**：``all`` 虽然三步都会拒绝，但
      建卡这一步先被拒、任务立即整体降级，``update``/``close`` 根本没有机会被
      调用到；只有任务从已持久化 ``card_id`` 恢复（上一轮建卡已经成功、这一轮
      从终态更新起步）时，``all`` 才会真的命中 update/close 那一支，此时才与
      ``create`` 单独命中的效果不同。
    - **覆盖"注入发生在建卡成功之后"（终态更新阶段）降级路径的是 ``update``**，
      不是 ``all``：终态更新失败会被 ``CardStream.finish()`` 捕获并整体降级为
      文本兜底。
    - ``close`` 单独命中时**不产生降级**：终态更新已经成功、只是收尾关闭失败，
      ``CardStream.finish()`` 对这种情况刻意不触发文本兜底（否则会在卡片已经
      显示正确答案之后再发一条重复文本，见该方法文档）。``close`` 因此是
      "关闭失败不得产生第二条文本终态"这条否定断言的验收入口，不是产生降级
      的正向用例。

    选最简单、语义最清晰的实现，不建一个"命中一次之后这个任务全部转失败"的
    状态机。
    """

    def __init__(self, real: CardTransport, *, inject: str) -> None:
        self._real = real
        self._inject = inject

    def create(self, **kwargs: object) -> CardCreated:
        if self._inject in ("create", "all"):
            raise DeliveryRejected("card failure injected for acceptance (create)", code=-1)
        return self._real.create(**kwargs)  # type: ignore[arg-type]

    def update(self, **kwargs: object) -> None:
        if self._inject in ("update", "all"):
            raise DeliveryRejected("card failure injected for acceptance (update)", code=-1)
        self._real.update(**kwargs)  # type: ignore[arg-type]

    def close(self, **kwargs: object) -> None:
        if self._inject in ("close", "all"):
            raise DeliveryRejected("card failure injected for acceptance (close)", code=-1)
        self._real.close(**kwargs)  # type: ignore[arg-type]


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

    ``config.card_failure_injection``（S-A-07 受控验收缺口）命中时，装配一个包一层
    "确定性拒绝"的 ``_RejectingCards`` 代替真实 ``LarkCardTransport``；未设置
    （默认）时这段分支完全不执行，装配结果与本开关加入之前逐字节一致。
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

    cards: CardTransport | None = None
    if config.card_failure_injection is not None:
        from lingxi.adapters.feishu_delivery import LarkCardTransport

        # 显眼的结构化告知（S-A-07 卡片故障注入开关第 3 点）：这个开关一旦被遗忘在
        # 开启状态，生产环境里每一条问数结果都会被强制降级成文本终态——必须让它在
        # 启动日志里足够扎眼，而不是混在普通 INFO 审计日志里被忽略。默认关闭时
        # （本分支不执行）不会有这条日志，与既有装配路径完全一致。
        logger.warning(
            "gateway.delivery.card_failure_injection_enabled inject=%s "
            "此开关仅供 S-A-07 受控验收使用，默认应为关闭；如果这不是一次受控验收"
            "启动，请立即核实并清空 LINGXI_GATEWAY_CARD_FAILURE_INJECT",
            config.card_failure_injection,
        )
        cards = _RejectingCards(LarkCardTransport(client), inject=config.card_failure_injection)

    return build_delivery_consumer(
        client=client,
        queue=queue
        or PostgresTaskQueue(str(config.postgres_dsn), timeouts=config.postgres_timeouts),
        cards=cards,
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
    # 长连接的心跳回调里挂上投递线程的看门狗（Issue #191），因此 supervisor 必须在
    # 线程对象存在之后再装配。两者之间没有别的依赖，换顺序不改变任何既有行为。
    supervisor_onboarding: list[OnboardingRunner] = []
    supervisor = build_supervisor(
        config,
        should_stop=stop_event.is_set,
        onboarding=onboarding_runner,
        on_onboarding_assembled=supervisor_onboarding.append,
        heartbeat=_combined_heartbeat(
            alerting_duty,
            "gateway-longconn",
            watchdog=delivery_thread_watchdog(
                delivery_thread, stop=stop_event, on_dead=report_delivery_thread_dead
            ),
        ),
    )
    # **gateway 侧的开通装配断言**（搬迁后的形态）：本进程实际接到管线上的那个实现
    # 必须是"只记事件"的那一个。接上任何会产生外部副作用的编排，都会把分钟级的等待
    # 放回长连接线程——那正是 #65 开工卡「共用线程复核」要消灭的形状，而它在这里
    # 只会表现为"gateway 忽然收不到消息"。
    assert_gateway_onboarding_is_inert(*supervisor_onboarding)
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
