"""外置文案覆盖文件被整份拒绝时，给管理群发**一条**告警。

日志那一半在 :mod:`lingxi.config.content_override`（``lru_cache`` 天然做到每进程
一条）；这里补管理群那一半。**只由 scheduler 发**：三个常驻进程读的是同一份宿主
机文件，三边各发一条只会刷屏，而 scheduler 是本仓库既有的管理群出站方（花名册
日报、内测通报、管理卡补偿通报都从这里发）。

去重键取「原因码 + 覆盖文件摘要」，因此把同一份坏文件反复重启在飞书侧被认作同一
条投递；换一份仍然坏的文件会得到新的键、重新提醒一次。

告警正文是面向运维的固定短句，**不经 content.toml**——正被判定为不可用的恰好就是
那份内容目录的外置覆盖，用它渲染自己的故障通知是自指的。
"""

from __future__ import annotations

from lingxi.apps.scheduler.audit import AuditSink
from lingxi.apps.scheduler.config import SchedulerConfig

#: 本条投递语义专用的飞书去重前缀（15 + 32 = 47，在 50 字符上限内）。与花名册
#: 日报等共用同一个群、同一个接口，但不能共用前缀，否则飞书会把两条不同的消息
#: 误判成同一逻辑投递。
CONTENT_OVERRIDE_UUID_PREFIX = "lingxi-content-"

#: 前缀与 ``core/alerting.py`` 的运行告警同型：管理群里同一类"系统在说话"的消息
#: 必须长得一样，管理员据此一眼分辨这是系统告警而不是某个人在发言。
_ALERT_TEXT = (
    "[BI Plus 运行告警] 宿主机上的用户可见文案覆盖文件未通过校验，已被整份忽略，"
    "用户看到的仍是随镜像发布的那一版文案（不影响任何在跑的服务）。"
    "原因码：{reason}。请用 `python -m lingxi.config.content_check <文件>` "
    "校验后重新放置，并重启相关服务。"
)


def notify_content_override_rejection(config: SchedulerConfig, *, audit: AuditSink) -> None:
    """Scheduler 启动时核对一次外置文案覆盖的加载结果；只有被拒才发。

    未配置管理群时只留审计不报错，与本进程其它可选出站职责同一取舍：一个尚未
    接线的告警通道不该让整个进程起不来。
    """
    from lingxi.config.content_override import log_content_source

    source = log_content_source("scheduler")
    if source.rejection is None:
        return
    audit.record("content.override_rejected", reason=source.rejection)
    if not config.admin_group_chat_id:
        return
    _send_alert(config, audit=audit, reason=source.rejection, digest=source.override_digest)


def _send_alert(
    config: SchedulerConfig, *, audit: AuditSink, reason: str, digest: str | None
) -> None:
    """发送失败只记审计：文案回退本身已经生效，告警发不出去不该拖停启动。"""
    from lingxi.adapters.feishu_group_message import FeishuGroupMessages

    sender = FeishuGroupMessages(
        base_url=config.feishu_base_url,
        app_id=config.feishu_app_id,
        app_secret=str(config.feishu_app_secret),
        uuid_prefix=CONTENT_OVERRIDE_UUID_PREFIX,
    )
    try:
        sender.send_text(
            chat_id=str(config.admin_group_chat_id),
            text=_ALERT_TEXT.format(reason=reason),
            dedupe_key=f"content-override:{reason}:{digest or 'none'}",
        )
    except Exception as error:
        audit.record("content.override_alert_failed", reason=reason, error=type(error).__name__)
        return
    audit.record("content.override_alert_sent", reason=reason)
