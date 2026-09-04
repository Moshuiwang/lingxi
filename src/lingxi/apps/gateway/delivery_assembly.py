"""S-A-07 受控验收用的卡片故障注入，以及投递消费循环的装配。

装配独立建一个飞书 SDK 客户端，而不是复用 supervisor 内部那一个——两者生命周期不同
（这一个要跟着后台线程一起停），共用同一个客户端对象反而会让"谁负责关它"变得含糊。
"""

from __future__ import annotations

import logging
from typing import Any

from lingxi.core.execution.card_stream import CardCreated, CardTransport, DeliveryRejected

from .config import GatewayConfig

logger = logging.getLogger("lingxi.apps.gateway")

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
        or PostgresTaskQueue(
            str(config.postgres_dsn),
            timeouts=config.postgres_timeouts,
            # S-H1-6（#359 根因取证方案第 2 条）：本实例只服务这一条单线程投递
            # 消费循环（见模块说明「同一进程内跑两条独立职责」），安全打开常驻
            # 轮询连接复用——`list_pending_delivery_tasks`/
            # `list_uncertain_delivery_tasks` 从此持有并复用同一条连接，不再
            # 每 poll_interval_seconds（默认 1s）新建一条物理连接。
            reuse_polling_connection=True,
        ),
        cards=cards,
        limit=config.delivery_batch_limit,
        on_alert=alerting_duty.delivery_alert_callback() if alerting_duty is not None else None,
        queue_delay_hint_seconds=config.queue_delay_hint_seconds,
    )
