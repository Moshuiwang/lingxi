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
from typing import Any, Callable, Mapping

from lingxi.adapters.feishu_events import (
    MESSAGE_RECEIVE_EVENT,
    EventParseError,
    parse_message_event,
)
from lingxi.adapters.feishu_longconn import (
    BackoffPolicy,
    LongConnectionSupervisor,
    TerminationReason,
)
from lingxi.core.conversation.pipeline import EventPipeline

from .config import GatewayConfig, GatewayConfigError, load_config

logger = logging.getLogger(__name__)


class _LoggingAudit:
    """审计出口的当前实现：结构化日志。

    ``audit_event`` 表属后续切片；管线只依赖 ``AuditSink`` 的签名，届时换实现不动管线。
    这里**不记录消息正文**——未开通用户的内容"不保存"包括不写进日志。
    """

    def record(self, action: str, /, **fields: object) -> None:
        logger.info("audit %s %s", action, fields)


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
        except EventParseError as error:
            # 读不懂的事件体记审计后继续收下一条，不抛给 supervisor 当成连接故障。
            audit.record("event.unparsable", error=str(error))
            if on_parse_error is not None:
                on_parse_error(str(error))
            return
        pipeline.handle_message(message)

    return handle


def install_signal_handlers(stop_event: threading.Event) -> None:
    """``SIGTERM`` / ``SIGINT`` 只置一个标志位。

    处理器里不做 I/O：信号可能落在任意一条语句之间，在那里写库或发网络请求会把
    "停机"变成一个新的故障源。
    """

    def handler(signum: int, _frame: Any) -> None:
        logger.info("收到信号 %s，开始停机", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def build_supervisor(config: GatewayConfig, *, transport: Any = None) -> LongConnectionSupervisor:
    """按配置装出一个 supervisor。

    ``transport`` 开成注入口：真实长连接属 L4a，全部 L2 断言注入假传输层。
    adapters 在函数体内延迟 import，与 ``apps/scheduler`` 的 ``build_loop`` 同惯例。
    """

    from lingxi.adapters.feishu_longconn import LarkEventTransport
    from lingxi.adapters.feishu_outbound import LarkReactions, LarkReplies, build_client
    from lingxi.adapters.postgres_conversation import PostgresGatewayStore

    audit = _LoggingAudit()
    client = build_client(app_id=config.app_id, app_secret=str(config.app_secret))
    pipeline = EventPipeline(
        store=PostgresGatewayStore(str(config.postgres_dsn)),
        reactions=LarkReactions(client),
        replies=LarkReplies(client),
        audit=audit,
    )
    return LongConnectionSupervisor(
        transport=transport
        or LarkEventTransport(app_id=config.app_id, app_secret=str(config.app_secret)),
        handle_event=make_event_handler(pipeline, audit=audit),
        backoff=BackoffPolicy(
            base_seconds=config.reconnect_base_seconds,
            factor=config.reconnect_factor,
            ceiling_seconds=config.reconnect_ceiling_seconds,
        ),
        audit=audit.record,
    )


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    """进程入口。返回退出码。"""

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        config = load_config(env if env is not None else os.environ)
    except GatewayConfigError as error:
        print(f"gateway 配置不可用：{error}", file=sys.stderr)
        return 2

    stop_event = threading.Event()
    install_signal_handlers(stop_event)

    supervisor = build_supervisor(config)
    reason = supervisor.run(should_stop=stop_event.is_set)

    if reason is TerminationReason.TERMINAL_ERROR:
        # 终止型错误（403 / 514 超连接数上限）：进程进入明确的终止态，退出码非 0，
        # 由编排层决定是否告警。**不重连**——重试一个被拒的凭据不会变成通过。
        print("gateway 因终止型接入错误退出，不再重连", file=sys.stderr)
        return 3
    return 0
