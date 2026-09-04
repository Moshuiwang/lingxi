"""进程全部定时职责的装配：各职责的条件注册判定，以及 :func:`build_loop`。

职责顺序有意为之：凭据轮换在前（它处理一次性有效、有硬期限的凭据，三类清理
晚一轮都没有后果）；审计日报与每日权限重算排在清理之后，且两者之间的先后
不是随意的——权限重算要用当天那份花名册快照，换快照的正是审计日报那一轮。
组织快照同步排在权限发布消费之后、首次开通编排之前（开通链的身份定位依赖
它）；告警职责注册在**最后**，汇总本轮观察到的信号，排在被观察者后面才看
得到这一轮的事实。完整理由随每一段装配代码就近写在 `build_loop` 各拆分
函数的注释里。
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from lingxi.apps.scheduler.audit import AuditSink, StructuredLogAuditSink
from lingxi.apps.scheduler.config import SchedulerConfig
from lingxi.apps.scheduler.credential_rotation import CredentialRotationLoop
from lingxi.apps.scheduler.daily_report import _wire_daily_report_duty
from lingxi.apps.scheduler.document_delivery_dead_letter import (
    _wire_document_delivery_maintenance_duty,
)
from lingxi.apps.scheduler.late_readiness_recovery import _build_late_readiness_recovery_duty
from lingxi.apps.scheduler.loop import SchedulerLoop
from lingxi.apps.scheduler.onboarding import _build_onboarding_duty
from lingxi.apps.scheduler.org_snapshot_sync import _build_org_snapshot_sync_duty
from lingxi.apps.scheduler.permission_publish import _build_permission_publish_duty

# 本文件不直接调用它，只是 re-export：``apps/scheduler/__init__.py`` 历史上一直
# 从这里（不是 ``permission_readiness_assembly`` 本身）导入它。冗余别名让 ruff
# 认得这是有意保留的重新导出，不判未用。
from lingxi.apps.scheduler.permission_readiness_assembly import (
    _build_readiness_follow_up as _build_readiness_follow_up,
)
from lingxi.apps.scheduler.permission_refresh import _build_permission_refresh_duty
from lingxi.apps.scheduler.retention import (
    IDLE_CONVERSATION_SWEEP_AFTER,
    IdleConversationSweepDuty,
    RetentionCleanupDuty,
    _build_content_capture_retention_duty,
    _build_permission_retention_duty,
)
from lingxi.apps.scheduler.roster_audit import (
    _build_roster_audit_duty,
    _build_roster_snapshot_sync_duty,
)
from lingxi.apps.scheduler.stalled_provisioning import _build_stalled_provisioning_duty
from lingxi.core.alerting import AlertingDuty
from lingxi.core.identity.access_token_supply import (
    DerivedAccessTokenHolder,
    RosterAccessTokenProvider,
)
from lingxi.core.permission.table_access_token_supply import (
    PermissionTableAccessTokenProvider,
)
from lingxi.core.permission.tenant_token_supply import TenantAccessTokenSupply

logger = logging.getLogger(__name__)


def _build_management_correction_callback(
    config: SchedulerConfig,
    *,
    audit: AuditSink,
    on_send_outcome: Callable[[str, bool], None] | None = None,
) -> Callable[[], None]:
    """装配管理卡权限补偿的真实发布观察与每日汇总出口。

    定向重算与发布消费是两个进程里的异步职责：gateway 只能先显示等待，scheduler
    读回 ``publish_outbox='published'`` 后才把持久上下文收口为生效。这里把观察挂到
    已有的权限发布职责每轮末尾，不新建轮询线程，也不把 outbox 入队误称为外部生效。
    """
    from lingxi.adapters.postgres_management_card_context import (
        PostgresManagementCardContextStore,
    )

    store = PostgresManagementCardContextStore(
        config.postgres_dsn, timeouts=config.postgres_timeouts
    )
    sender = _build_management_correction_sender(config, on_send_outcome)
    channel_missing_reported = False

    def settle() -> None:
        nonlocal channel_missing_reported
        # 先把已读回一致的上下文推进到 effective，再取待通报水位；两步各自幂等，
        # 中间崩溃时下一轮会重复读而不会重复修改权限。
        store.settle_published_contexts()
        message_ids = tuple(sorted(store.unreported_daily_correction_ids()))
        if not message_ids:
            return
        if sender is None:
            channel_missing_reported = _report_management_correction_channel_missing(
                audit, message_ids, already_reported=channel_missing_reported
            )
            return
        _send_management_correction_summary(
            config=config, audit=audit, sender=sender, store=store, message_ids=message_ids
        )

    return settle


def _build_management_correction_sender(
    config: SchedulerConfig, on_send_outcome: Callable[[str, bool], None] | None
) -> Any:
    """未配置管理群时返回 ``None``——``settle()`` 据此只留审计，不报错。"""
    if not config.admin_group_chat_id:
        return None

    from lingxi.adapters.feishu_group_message import (
        MANAGEMENT_CORRECTION_UUID_PREFIX,
        FeishuGroupMessages,
    )

    return FeishuGroupMessages(
        base_url=config.feishu_base_url,
        app_id=config.feishu_app_id,
        app_secret=config.feishu_app_secret,
        on_send_outcome=on_send_outcome,
        uuid_prefix=MANAGEMENT_CORRECTION_UUID_PREFIX,
    )


def _report_management_correction_channel_missing(
    audit: AuditSink, message_ids: tuple[str, ...], *, already_reported: bool
) -> bool:
    """未配置发送出口时只留**恰一条**缺出口审计，返回值供调用方更新"已经报过"的状态。

    保留未通报水位，后续补上群配置后仍可补发。
    """
    if not already_reported:
        audit.record(
            "admin.management_correction_channel_missing",
            count=len(message_ids),
            variable="LINGXI_ADMIN_GROUP_CHAT_ID",
        )
    return True


def _send_management_correction_summary(
    *,
    config: SchedulerConfig,
    audit: AuditSink,
    sender: Any,
    store: Any,
    message_ids: tuple[str, ...],
) -> None:
    """按当前未通报的 ID 集合算稳定去重键，发送成功才推进水位。

    发送成功与水位写入之间若进程崩溃，重试携带同一个键；若后来又有新的补偿
    行，集合变化会得到新的键，避免把新增行静默吞掉。
    """
    from lingxi.config.content import default_content_catalog

    digest = hashlib.sha256("\n".join(message_ids).encode("utf-8")).hexdigest()[:16]
    dedupe_key = f"management-correction:{datetime.now(UTC).date().isoformat()}:{digest}"
    text = (
        default_content_catalog()
        .text("permission.management_correction_summary", count=len(message_ids))
        .text
    )
    try:
        sender.send_text(chat_id=config.admin_group_chat_id, text=text, dedupe_key=dedupe_key)
    except Exception as error:  # 下一轮继续用同一批次重试
        audit.record(
            "admin.management_correction_summary_failed",
            count=len(message_ids),
            error=type(error).__name__,
        )
        return
    # 只有发送返回成功才推进水位；若这里失败，外层职责记录观察失败，下一轮
    # 会用相同 ID 集合/去重键再次尝试，不把不确定态标作已送达。
    store.mark_daily_corrections_reported(message_ids=message_ids)
    audit.record("admin.management_correction_summary_sent", count=len(message_ids))


def build_loop(
    config: SchedulerConfig,
    *,
    roster_access_token: Callable[[], str] | None = None,
    permission_table_access_token: Callable[[], str] | None = None,
    audit: AuditSink | None = None,
    alerting_duty: AlertingDuty | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> SchedulerLoop:
    """装配进程的全部定时职责；顺序理由见模块文档字符串。

    ``roster_access_token``/``permission_table_access_token`` 默认不是
    ``None``——这里会建出真实的进程内令牌供给（前者复用凭据轮换职责派生的
    短期令牌，后者用应用身份换取 tenant_access_token，零新增凭据材料）；
    ``None`` 只是"用默认这条"，不是"没有供给"，两条供给各自的详细理由随
    其装配点写在对应函数的注释里。调用方（主要是测试）传自定义实现即可
    替换默认供给。
    """
    stop = threading.Event()
    sink = audit if audit is not None else StructuredLogAuditSink()

    holder, rotation, cleanup, idle_sweep, permission_retention, content_capture_retention = (
        _build_rotation_and_cleanup_duties(config, stop, sink)
    )
    duties: list[Any] = [
        rotation,
        cleanup,
        idle_sweep,
        permission_retention,
        content_capture_retention,
    ]

    supply = _build_roster_access_token_supply(config, sink, holder, rotation, roster_access_token)
    _wire_roster_audit_or_snapshot(
        duties, config, stop=stop, audit=sink, supply=supply, alerting_duty=alerting_duty
    )
    _wire_daily_report_duty(duties, config, stop=stop, audit=sink, alerting_duty=alerting_duty)
    _wire_permission_and_onboarding_pipeline(
        duties,
        config,
        stop=stop,
        audit=sink,
        supply=supply,
        permission_table_access_token=permission_table_access_token,
        alerting_duty=alerting_duty,
    )
    _wire_document_delivery_maintenance_duty(
        duties, config, stop=stop, audit=sink, alerting_duty=alerting_duty
    )
    if alerting_duty is not None:
        duties.append(alerting_duty)
        if heartbeat is None:
            heartbeat = alerting_duty.heartbeat_callback("scheduler")

    return SchedulerLoop(
        duties=tuple(duties),
        interval_seconds=config.interval_seconds,
        stop=stop,
        heartbeat=heartbeat,
    )


def _wire_permission_and_onboarding_pipeline(
    duties: list[Any],
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
    supply: Callable[[], str],
    permission_table_access_token: Callable[[], str] | None,
    alerting_duty: AlertingDuty | None,
) -> None:
    """权限重算/发布 → 组织快照同步 → 首次开通编排 → 两条恢复收口。

    顺序理由见各自拆分函数的文档字符串，这里只负责按顺序调用并串起共享
    状态（权限发布表令牌供给、权限发布对象、指标映射表）。
    """
    permission_table_supply = _build_permission_table_supply(
        config, audit, permission_table_access_token
    )
    permission_publish, metric_translation_map = _wire_permission_refresh_and_publish(
        duties,
        config,
        stop=stop,
        audit=audit,
        alerting_duty=alerting_duty,
        permission_table_supply=permission_table_supply,
    )
    _wire_org_snapshot_sync(
        duties,
        config,
        stop=stop,
        audit=audit,
        user_access_token=supply,
        app_access_token=permission_table_supply,
    )
    _wire_onboarding(
        duties,
        config,
        stop=stop,
        audit=audit,
        employment_access_token=supply,
        metric_translation_map=metric_translation_map,
        permission_publish=permission_publish,
        permission_table_supply=permission_table_supply,
        alerting_duty=alerting_duty,
    )
    _wire_late_and_stalled_recovery(
        duties, config, stop=stop, audit=audit, alerting_duty=alerting_duty
    )


def _build_rotation_and_cleanup_duties(
    config: SchedulerConfig, stop: threading.Event, sink: AuditSink
) -> tuple[DerivedAccessTokenHolder, CredentialRotationLoop, RetentionCleanupDuty, Any, Any, Any]:
    """凭据轮换 + 三类清理：到期行/空闲会话/到期内容快照。

    三类清理晚一轮都没有任何后果——到期时间不会因为清理迟到而后移（断言
    `V-保留-16`），空闲会话清理本身也是幂等的。权限链到期清理排在前两类清理
    之后、发布消费之前：一条 payload 已经过期的意图应当先被擦成 ``'{}'``，
    再轮到发布面认领它并以 ``invalid`` 失败关闭，反过来会让一份早已过期的
    权限在到期当轮还被写进外部表格一次。内测轮采集内容的到期删除与前面几条
    清理同组，无条件装配：删自己库里的到期内容只需要连接串，生产该表恒空。
    """
    from lingxi.adapters.delegated_credentials import HostFileDelegatedCredentialVault
    from lingxi.adapters.feishu_directory import FeishuAuthorizationClient
    from lingxi.adapters.postgres_conversation import PostgresTaskQueue
    from lingxi.adapters.retention import RETENTION_CLEANUP_TIMEOUTS, PostgresRetentionCleaner

    # 派生短期令牌的进程内持有者：不落盘、不进日志与审计，重启即空。
    holder = DerivedAccessTokenHolder()
    rotation = CredentialRotationLoop(
        vault=HostFileDelegatedCredentialVault(
            config.postgres_dsn,
            config.credential_key,
            config.credential_path,
            timeouts=config.postgres_timeouts,
        ),
        authorization=FeishuAuthorizationClient(
            base_url=config.feishu_base_url,
            app_id=config.feishu_app_id,
            app_secret=config.feishu_app_secret,
        ),
        interval_seconds=config.interval_seconds,
        stop=stop,
        holder=holder,
    )
    cleanup = RetentionCleanupDuty(
        # 清理函数内部两张表各有 2s lock_timeout，不能沿用 scheduler 通用的 3s
        # statement_timeout；适配器专用覆盖要大于 2×2s 累计并留出删批余量。
        cleaner=PostgresRetentionCleaner(config.postgres_dsn, timeouts=RETENTION_CLEANUP_TIMEOUTS),
        stop=stop,
    )
    idle_sweep = IdleConversationSweepDuty(
        queue=PostgresTaskQueue(config.postgres_dsn, timeouts=config.postgres_timeouts),
        idle_after=IDLE_CONVERSATION_SWEEP_AFTER,
        stop=stop,
    )
    permission_retention = _build_permission_retention_duty(config, stop=stop, audit=sink)
    content_capture_retention = _build_content_capture_retention_duty(config, stop=stop, audit=sink)
    return holder, rotation, cleanup, idle_sweep, permission_retention, content_capture_retention


def _build_roster_access_token_supply(
    config: SchedulerConfig,
    sink: AuditSink,
    holder: DerivedAccessTokenHolder,
    rotation: CredentialRotationLoop,
    roster_access_token: Callable[[], str] | None,
) -> Callable[[], str]:
    """花名册读取所用的短期令牌供给。

    持有者里有新鲜令牌就直接给，没有就让**凭据轮换职责**按需换一次一次性
    ``refresh_token``（受最小间隔 + 每日上界双重保护）。**唯一消费者这条
    边界不能破**：日报自己不碰 ``refresh_token``，两个消费者共用一条一次性
    令牌正是历史事故的形状——花名册日报与首次开通编排的在职状态回读都只经
    这条派生短期令牌供给，不各自另换一次。
    """
    if roster_access_token is not None:
        return roster_access_token
    return RosterAccessTokenProvider(holder=holder, refresh=rotation.refresh_for_supply, audit=sink)


def _wire_roster_audit_or_snapshot(
    duties: list[Any],
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
    supply: Callable[[], str],
    alerting_duty: AlertingDuty | None,
) -> None:
    """花名册审计报告与快照同步互斥注册。

    "只写不发"的快照同步与审计报告同一时刻至多一个在触发花名册读取（见
    `_build_roster_snapshot_sync_duty` 文档字符串）。报告职责没装配时——
    不管原因是管理群没配、Base 坐标没配还是令牌供给没配——
    `_build_roster_audit_duty` 已经留下自己的 `roster_audit.*` 审计；这里
    独立判定快照同步能不能装配，只要它自己的前提满足，快照就该被写，不
    因为管理群这个纯通知配置项一起停摆。
    """
    roster_audit = _build_roster_audit_duty(
        config,
        stop=stop,
        audit=audit,
        roster_access_token=supply,
        on_send_outcome=(alerting_duty.send_outcome_callback() if alerting_duty else None),
    )
    if roster_audit is not None:
        duties.append(roster_audit)
        return
    roster_snapshot_sync = _build_roster_snapshot_sync_duty(
        config, stop=stop, audit=audit, roster_access_token=supply
    )
    if roster_snapshot_sync is not None:
        duties.append(roster_snapshot_sync)


def _build_permission_table_supply(
    config: SchedulerConfig,
    sink: AuditSink,
    permission_table_access_token: Callable[[], str] | None,
) -> Callable[[], str]:
    """权限发布表读写所用的短期令牌供给。

    用**应用身份**（tenant_access_token）写入，理由是它没有凭据生命周期，
    不需要用户再点授权、不会过期、不需要轮换；已知代价是写入不绑定到某个
    具体授权人，需要把应用加为该 Base 的协作者。换取用的 ``app_id``/
    ``app_secret`` 就是 scheduler 本来就必需的应用配置——零新增凭据材料。
    花名册与权限发布表在不同的 Base 上、供给来源不同（花名册那条走用户
    授权主体），两条各留各的注入点，互不影响。
    """
    if permission_table_access_token is not None:
        return permission_table_access_token

    from lingxi.adapters.feishu_tenant_token import FeishuTenantTokenClient

    return PermissionTableAccessTokenProvider(
        fetch=TenantAccessTokenSupply(
            fetch=FeishuTenantTokenClient(
                base_url=config.feishu_base_url,
                app_id=config.feishu_app_id,
                app_secret=config.feishu_app_secret,
            ).fetch,
        ),
        audit=sink,
    )


def _wire_permission_refresh_and_publish(
    duties: list[Any],
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
    alerting_duty: AlertingDuty | None,
    permission_table_supply: Callable[[], str],
) -> tuple[Any, Any]:
    """每日权限重算 + 发布消费。

    返回 ``(permission_publish, metric_translation_map)`` 供后续首次开通
    编排复用。重算排在花名册审计（或与它互斥的快照写入）之后：花名册快照
    先被换成今天的那一份，重算才可能通过它自己的新鲜度判据（`V-权限-07`）。
    发布消费排在重算之后，同一轮内先排意图、后消费。``metric_translation_map``
    是本次调用**唯一一次**读取指标映射文件的结果；开通编排的发布闸复用
    同一个对象，不再读一份新文件，避免两份来源漂移导致错误发布。
    """
    permission_refresh, metric_translation_map = _build_permission_refresh_duty(
        config, stop=stop, audit=audit
    )
    if permission_refresh is not None:
        duties.append(permission_refresh)
    management_corrections = _build_management_correction_callback(
        config,
        audit=audit,
        on_send_outcome=(alerting_duty.send_outcome_callback() if alerting_duty else None),
    )
    permission_publish = _build_permission_publish_duty(
        config,
        stop=stop,
        audit=audit,
        permission_table_access_token=permission_table_supply,
        on_management_corrections=management_corrections,
    )
    if permission_publish is not None:
        duties.append(permission_publish)
    return permission_publish, metric_translation_map


def _wire_org_snapshot_sync(
    duties: list[Any],
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
    user_access_token: Callable[[], str],
    app_access_token: Callable[[], str],
) -> None:
    """组织快照同步排在发布消费**之后**、首次开通编排**之前**。

    它是开通链身份定位那一步唯一的数据来源，排在开通之前才可能让**同一轮**
    新装配、第一次真的跑成功的那次同步被紧随其后的开通认领用上，少等一整个
    调度周期。它与花名册审计、每日权限重算之间没有数据依赖，位置只服务这
    一个"同一轮内先后"的收益。用户身份路径复用**同一个**令牌供给（一次性
    ``refresh_token`` 全系统只允许一个消费者）；应用身份路径复用权限发布表
    那条供给（``tenant_access_token`` 无消费者数量上限，复用只省一次换取）。
    """
    org_snapshot_sync = _build_org_snapshot_sync_duty(
        config,
        stop=stop,
        audit=audit,
        user_access_token=user_access_token,
        app_access_token=app_access_token,
    )
    if org_snapshot_sync is not None:
        duties.append(org_snapshot_sync)


def _wire_onboarding(
    duties: list[Any],
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
    employment_access_token: Callable[[], str],
    metric_translation_map: Any,
    permission_publish: Any,
    permission_table_supply: Callable[[], str],
    alerting_duty: AlertingDuty | None,
) -> None:
    """首次开通编排排在发布消费**之后**。

    发布面先把上一轮排出的意图推出去，开通链的发布等待才可能在同一轮内
    看到 ``published``。在职状态回读复用**同一个**令牌供给（一次性
    ``refresh_token`` 全系统只允许一个消费者）。**装配不变量**：
    ``onboarding != None ⇒ permission_publish != None 且
    permission_publish.publish_wired``——这里把刚装配出的
    ``permission_publish`` 原样交给 `_build_onboarding_duty` 校验，唯一的
    真相来源是那个对象本身，完整理由见其文档字符串「装配不变量」一节。
    """
    from lingxi.apps.scheduler.onboarding import build_stock_token_source

    onboarding = _build_onboarding_duty(
        config,
        stop=stop,
        audit=audit,
        employment_access_token=employment_access_token,
        metric_translation_map=metric_translation_map,
        permission_publish=permission_publish,
        stock_tokens=build_stock_token_source(
            config, access_token=permission_table_supply, audit=audit
        ),
        onboarding_failed=(
            alerting_duty.onboarding_failed_callback() if alerting_duty is not None else None
        ),
    )
    if onboarding is None:
        return
    duties.append(onboarding)
    if not config.admin_group_chat_id:
        _warn_onboarding_admin_alert_channel_missing(audit)


def _warn_onboarding_admin_alert_channel_missing(audit: AuditSink) -> None:
    """开通职责已注册但未配管理群时，响亮告知启动期的降级形态。

    开通职责会真的产生 INTERNAL_ERROR / SYNC_TIMEOUT 终态，`onboarding_failed`
    回调也已经接上，但没有配 `LINGXI_ADMIN_GROUP_CHAT_ID` 时 `AlertDispatcher`
    的送达出口会退化成 `_LogOnlyAlertSender`（`V-告警-08` 既定语义：不失败
    关闭，状态机照常运行）——用户看到的「已转交管理员处理」这句承诺因此没有
    任何人会真的看到。这不是一个需要拒绝启动的错误（LogOnly 是产品接受的
    降级形态），但必须在启动期留一条响亮日志，不能让这个组合悄悄运行。
    """
    logger.warning(
        "已注册首次开通编排，但未配置 LINGXI_ADMIN_GROUP_CHAT_ID："
        "「已转交管理员处理」的送达面将退化为仅结构化日志，管理群收不到任何"
        "开通失败 / 同步超时告警（V-告警-08 既定语义，不阻止启动）"
    )
    audit.record(
        "onboarding.admin_alert_channel_missing",
        reason="missing_environment_variable",
        variable="LINGXI_ADMIN_GROUP_CHAT_ID",
    )


def _wire_late_and_stalled_recovery(
    duties: list[Any],
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
    alerting_duty: AlertingDuty | None,
) -> None:
    """迟到就绪恢复排在首次开通编排**之后**；开通中途停摆收口排在迟到就绪恢复**之后**。

    两者都是"回来看已经安静下来的开通"职责，位置只是"同一轮内先声明的
    职责先跑"的自然顺序，不构成数据依赖——各自按自己的候选判据工作，不会
    互相抢候选。两者**总能注册**（没有可选前置会让它们整体不装配），因此
    不需要 ``if ... is not None`` 判断。
    """
    duties.append(_build_late_readiness_recovery_duty(config, stop=stop, audit=audit))
    duties.append(
        _build_stalled_provisioning_duty(
            config,
            stop=stop,
            audit=audit,
            alert=(
                alerting_duty.onboarding_stalled_callback() if alerting_duty is not None else None
            ),
        )
    )
