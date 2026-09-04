"""投递消费循环的装配，以及受控验收用的卡片故障注入。

装配独立建一个飞书 SDK 客户端，而不是复用 supervisor 内部那一个——两者生命周期不同
（这一个要跟着后台线程一起停），共用同一个客户端对象反而会让"谁负责关它"变得含糊；
多一个轻量客户端对象本身不产生任何网络连接，直到第一次真正调用才建立 HTTP 连接。
"""

from __future__ import annotations

import logging
from typing import Any

from lingxi.core.execution.card_stream import CardCreated, CardTransport, DeliveryRejectedError

from .config import GatewayConfig

logger = logging.getLogger("lingxi.apps.gateway")


class RejectingCards:
    """让配置命中的那一步卡片外发确定性收到拒绝，用于证伪降级路径。

    只在显式设置故障注入环境变量时才会被装配。实现刻意最简单——只让被选中的那一步失败，
    未选中的步骤直通真实 transport，不建「命中一次之后整个任务转失败」的状态机。

    四个取值的实测语义：``create`` 与 ``all`` 在正常单轮场景下等价（建卡先被拒、任务立即
    整体降级）；覆盖「建卡成功之后终态更新失败」的是 ``update``；``close`` 单独命中**不
    产生降级**——终态已经正确显示，再补一条文本就成了重复交付，因此它是「关闭失败不得产生
    第二条文本终态」的验收入口。
    """

    def __init__(self, real: CardTransport, *, inject: str) -> None:
        """记住真实 transport 与要注入失败的那一步。"""
        self._real = real
        self._inject = inject

    def create(self, **kwargs: object) -> CardCreated:
        """建卡；命中注入时确定性拒绝。"""
        if self._inject in ("create", "all"):
            raise DeliveryRejectedError("card failure injected for acceptance (create)", code=-1)
        return self._real.create(**kwargs)  # type: ignore[arg-type]

    def update(self, **kwargs: object) -> None:
        """更新卡片；命中注入时确定性拒绝。"""
        if self._inject in ("update", "all"):
            raise DeliveryRejectedError("card failure injected for acceptance (update)", code=-1)
        self._real.update(**kwargs)  # type: ignore[arg-type]

    def close(self, **kwargs: object) -> None:
        """关闭卡片；命中注入时确定性拒绝。"""
        if self._inject in ("close", "all"):
            raise DeliveryRejectedError("card failure injected for acceptance (close)", code=-1)
        self._real.close(**kwargs)  # type: ignore[arg-type]


def assemble_delivery_consumer(
    config: GatewayConfig, *, queue: Any = None, alerting_duty: Any = None
) -> Any:
    """装配投递消费循环：读 outbox、驱动流式卡片与文本兜底。

    Args:
        config: 已加载的进程配置。
        queue: 任务队列，留空时按配置连真库。
        alerting_duty: 不为空时把它的投递告警回调接进消费循环。

    Returns:
        可以直接交给后台线程跑的消费者。
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

        # 这个开关一旦被遗忘在开启状态，生产上每一条问数结果都会被强制降级成文本
        # 终态——必须在启动日志里足够扎眼，而不是混在普通 INFO 审计里被忽略。
        logger.warning(
            "gateway.delivery.card_failure_injection_enabled inject=%s "
            "此开关仅供 S-A-07 受控验收使用，默认应为关闭；如果这不是一次受控验收"
            "启动，请立即核实并清空 LINGXI_GATEWAY_CARD_FAILURE_INJECT",
            config.card_failure_injection,
        )
        cards = RejectingCards(LarkCardTransport(client), inject=config.card_failure_injection)

    return build_delivery_consumer(
        client=client,
        queue=queue
        or PostgresTaskQueue(
            str(config.postgres_dsn),
            timeouts=config.postgres_timeouts,
            # 本实例只服务这一条单线程投递消费循环，因此可以安全打开常驻轮询连接
            # 复用：两个待投递查询从此复用同一条连接，不再每轮新建一条物理连接。
            reuse_polling_connection=True,
        ),
        cards=cards,
        limit=config.delivery_batch_limit,
        on_alert=alerting_duty.delivery_alert_callback() if alerting_duty is not None else None,
        queue_delay_hint_seconds=config.queue_delay_hint_seconds,
    )
