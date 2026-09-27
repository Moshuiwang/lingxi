"""外置文案覆盖文件被整份拒绝时，给管理群发**一条**告警（装配入口）。

日志那一半在 :mod:`lingxi.config.content_override`（``lru_cache`` 天然做到每进程
一条）；这里补管理群那一半。**只由 scheduler 发**：三个常驻进程读的是同一份宿主
机文件，三边各发一条只会刷屏，而 scheduler 是本仓库既有的管理群出站方（花名册
日报、内测通报、管理卡补偿通报都从这里发）。

判定、去重键、正文与卡片（内容目录登记了 ``notice.content.override_rejected`` 时
发通知卡，飞书明确拒绝才回落纯文本一次）都在
:func:`lingxi.core.delivery.ops_notice.report_content_override_rejection`；本模块只
注入群消息发送口。
"""

from lingxi.apps.scheduler.audit import AuditSink
from lingxi.apps.scheduler.config import SchedulerConfig
from lingxi.core.delivery import ops_notice

#: 保留模块级导出：既有用例从这里读告警正文（去重前缀见
#: ``ops_notice.CONTENT_OVERRIDE_UUID_PREFIX``）。
_ALERT_TEXT = ops_notice.CONTENT_OVERRIDE_ALERT_TEXT


def notify_content_override_rejection(config: SchedulerConfig, *, audit: AuditSink) -> None:
    """Scheduler 启动时核对一次外置文案覆盖的加载结果；只有被拒才发。"""
    from lingxi.adapters import feishu_group_message

    ops_notice.report_content_override_rejection(
        config, audit=audit, sender_type=feishu_group_message.FeishuGroupMessages
    )
