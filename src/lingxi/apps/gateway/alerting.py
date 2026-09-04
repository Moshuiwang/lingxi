"""gateway 未配置管理群时的告警出口，以及本进程自己的告警状态机装配。

gateway 与 scheduler、worker 是三个独立部署单元，进程间不直接通信，因此各自持有
一份告警状态机——这不是重复造轮子，是"告警状态机跟着部署单元走"的自然结果。
"""

from __future__ import annotations

import logging
from typing import Any

from .audit_log import _LoggingAudit
from .config import GatewayConfig

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
