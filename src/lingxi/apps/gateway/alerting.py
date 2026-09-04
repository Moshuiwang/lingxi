"""gateway 自己的告警状态机装配，以及未配置管理群时的降级出口。

gateway 与 scheduler、worker 是三个独立部署单元，进程间不直接通信，因此各自持有
一份告警状态机——这不是重复造轮子，是"告警状态机跟着部署单元走"这条既有约束的
自然结果。没配管理群时状态机照常运行（阈值、去重、恢复计时都真实生效），只是发送端
退化为结构化日志。
"""

from __future__ import annotations

import logging
from typing import Any

from .audit_log import LoggingAudit
from .config import GatewayConfig


class LogOnlyAlertSender:
    """未配置管理群时的告警出口：只记结构化日志，不发起网络请求。

    与 worker 侧的同名类同一姿态——没有配置目标群不等于告警关闭，
    只是"发送"这一步落到日志（合同要求"告警不可用时主流程行为有明确定义"，
    这里的定义是"继续跑，只是暂时没有群通知"）。
    """

    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None:
        """把本该发进管理群的告警文本降级成一条 ``WARNING`` 日志。"""
        del chat_id, dedupe_key
        logging.getLogger("lingxi.apps.gateway.alert").warning(text)


def build_alerting_duty(config: GatewayConfig) -> Any:
    """装配 gateway 的告警职责对象。

    Args:
        config: 已加载的进程配置；配了管理群会话就真正发进群，没配则只落日志。

    Returns:
        可以直接交给两条循环共用的告警职责对象。
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
        sender = LogOnlyAlertSender()
        chat_id = "gateway-log-only"

    return AlertingDuty(
        manager=AlertManager(policy=config.alert_policy),
        dispatcher=AlertDispatcher(sender=sender, chat_id=chat_id, policy=config.alert_policy),
        audit=LoggingAudit(),
    )
