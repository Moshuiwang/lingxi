"""scheduler 自己的告警状态机装配：:func:`build_alerting_duty` 与心跳回调。

从 :mod:`lingxi.apps.scheduler`（#237 拆分）搬出。``AlertingDuty`` 状态机本身在
:mod:`lingxi.core.alerting`（Issue #153：gateway / worker / scheduler 三个进程各自
装配同型的一份，互不共享状态）；这里只有 scheduler 专属的装配细节——没配管理群时的
仅日志出口、以及把 ``AlertManager`` 心跳与活性文件戳合成一个回调。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from lingxi.apps.scheduler.audit import AuditSink
from lingxi.apps.scheduler.config import SchedulerConfig
from lingxi.core.alerting import AlertDispatcher, AlertingDuty, AlertManager


class _LogOnlyAlertSender:
    """未配置管理群时的告警出口：只记结构化日志，不发起网络请求。

    与 ``apps/gateway/__init__.py``、``apps/worker/cli.py`` 的同名类同一姿态——
    没有配置目标群不等于告警关闭（Issue #153：合同要求"告警不可用时主流程行为
    有明确定义"，这里的定义是"状态机照常运行，只是暂时没有群通知"）。
    """

    def send_text(self, *, chat_id: str, text: str, dedupe_key: str) -> None:
        del chat_id, dedupe_key
        logging.getLogger("lingxi.apps.scheduler.alert").warning(text)


def build_alerting_duty(config: SchedulerConfig, *, audit: AuditSink | None = None) -> AlertingDuty:
    """装配 scheduler 自己的告警状态机（Issue #153）。

    此前 ``AlertingDuty`` 虽已建成（Issue #92），但 ``main()`` 从未真的实例化它
    ——当前能力已经登记这个缺口："三进程 main() 均未装配告警"。这是补上 scheduler
    这一份的地方；gateway 与 worker 各自在自己的 ``apps/<name>`` 里装配同型的一份，
    三者互不共享状态（各自是独立部署单元）。
    """

    if config.admin_group_chat_id:
        from lingxi.adapters.feishu_group_message import FeishuGroupMessages

        sender: Any = FeishuGroupMessages(
            base_url=config.feishu_base_url,
            app_id=config.feishu_app_id,
            app_secret=config.feishu_app_secret,
        )
        chat_id = config.admin_group_chat_id
    else:
        sender = _LogOnlyAlertSender()
        chat_id = "scheduler-log-only"

    return AlertingDuty(
        manager=AlertManager(policy=config.alert_policy),
        dispatcher=AlertDispatcher(
            sender=sender, chat_id=chat_id, policy=config.alert_policy, audit=audit
        ),
        audit=audit,
    )


def _combined_heartbeat(alerting_duty: AlertingDuty, liveness_role: str) -> Callable[[], None]:
    """把"记进 AlertManager"与"戳一下活性文件"合成一个心跳回调（Issue #153）。

    与 ``apps/gateway/__init__.py`` 的同名函数同一形状：``AlertManager`` 供跨进程
    重启也能观察到的阈值/去重状态机，活性文件供同容器内的
    ``python -m lingxi.apps.healthcheck`` 判断主循环是否还在跳动。
    """

    from lingxi.apps.liveness import touch_liveness

    beat = alerting_duty.heartbeat_callback(liveness_role)

    def combined() -> None:
        beat()
        touch_liveness(liveness_role)

    return combined
