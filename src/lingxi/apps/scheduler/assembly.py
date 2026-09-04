"""进程全部定时职责的装配：各职责的条件注册判定，以及 :func:`build_loop`。

从 :mod:`lingxi.apps.scheduler`（#237 拆分）搬出——这是本次拆分要解出的那一块：
九个定时职责的构造、前置判定与失败关闭语义原来全部挤在包的 ``__init__.py`` 里，
不同职责的接线改动因此总是落在同一个文件上。装配顺序的完整理由（凭据轮换在前、
花名册审计先于每日权限重算、发布消费排在重算之后……）见 :func:`build_loop` 自己的
文档字符串，未随拆分改动。
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
    读回 ``publish_outbox='published'`` 后才把持久上下文收口为生效。此前这条收口没有
    常驻调用方，重启后的管理卡只能等下一次人工点击才重新观察。这里把观察挂到已有的
    权限发布职责每轮末尾，不新建轮询线程，也不把 outbox 入队误称为外部生效。

    ``daily_correction_reported_at`` 是按消息 ID 的水位。汇总按当前未通报的 ID 集合
    计算稳定去重键：发送成功与水位写入之间若进程崩溃，重试携带同一个键；若后来又有
    新的补偿行，集合变化会得到新的键，避免把新增行静默吞掉。未配置管理群时只留一条
    缺出口审计而保留未通报水位，后续部署补上群配置后仍可补发。
    """

    from lingxi.adapters.feishu_group_message import (
        MANAGEMENT_CORRECTION_UUID_PREFIX,
        FeishuGroupMessages,
    )
    from lingxi.adapters.postgres_management_card_context import (
        PostgresManagementCardContextStore,
    )
    from lingxi.config.content import default_content_catalog

    store = PostgresManagementCardContextStore(
        config.postgres_dsn, timeouts=config.postgres_timeouts
    )
    sender: Any = None
    if config.admin_group_chat_id:
        sender = FeishuGroupMessages(
            base_url=config.feishu_base_url,
            app_id=config.feishu_app_id,
            app_secret=config.feishu_app_secret,
            on_send_outcome=on_send_outcome,
            uuid_prefix=MANAGEMENT_CORRECTION_UUID_PREFIX,
        )
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
            if not channel_missing_reported:
                audit.record(
                    "admin.management_correction_channel_missing",
                    count=len(message_ids),
                    variable="LINGXI_ADMIN_GROUP_CHAT_ID",
                )
                channel_missing_reported = True
            return

        digest = hashlib.sha256("\n".join(message_ids).encode("utf-8")).hexdigest()[:16]
        dedupe_key = (
            f"management-correction:{datetime.now(UTC).date().isoformat()}:{digest}"
        )
        text = default_content_catalog().text(
            "permission.management_correction_summary", count=len(message_ids)
        ).text
        try:
            sender.send_text(
                chat_id=config.admin_group_chat_id,
                text=text,
                dedupe_key=dedupe_key,
            )
        except Exception as error:  # noqa: BLE001 - 下一轮继续用同一批次重试
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

    return settle


def build_loop(
    config: SchedulerConfig,
    *,
    roster_access_token: Callable[[], str] | None = None,
    permission_table_access_token: Callable[[], str] | None = None,
    audit: AuditSink | None = None,
    alerting_duty: AlertingDuty | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> SchedulerLoop:
    """装配进程的全部定时职责。

    职责顺序有意为之：凭据轮换在前。它处理的是一次性有效、有硬期限的凭据，
    而三类清理晚一轮都没有任何后果——到期行/空闲会话/到期内容快照下一轮照清，到期时间
    不会因为清理迟到而后移（断言 V-保留-16），空闲会话清理本身也是幂等的。审计日报与每日
    权限重算排在三类清理之后：它们一天只做一次事，晚一轮毫无影响；而这两个之间的先后
    **不是随意的**——权限重算要用今天那份花名册快照，而换快照的是审计日报那一轮
    （`V-权限-07`）。组织快照同步（Issue #250）排在权限发布消费之后、首次开通编排之前，
    理由见 ``_build_org_snapshot_sync_duty`` 调用点上方的注释。告警职责注册在**最后**
    （基线既有形状）：它汇总本轮观察到的信号，排在被观察者后面才看得到这一轮的事实。

    ``roster_access_token`` 是花名册读取所用的**短期令牌供给**（返回已就绪令牌的可调用
    对象）。**默认不再是 ``None``**：Issue #215 主接线之后，这里建出一条进程内供给——
    凭据轮换职责按需消费一次 ``refresh_token``、把派生短期令牌放进进程内持有者，日报侧
    经 :class:`~lingxi.core.identity.access_token_supply.RosterAccessTokenProvider`
    按新鲜度取用。**唯一消费者这条边界没有变**：日报自己不碰 ``refresh_token``，两个
    消费者共用一条一次性令牌正是 2026-08-08 授权码被烧那次事故的形状。

    参数保留是为了让调用方（主要是测试）**替换**供给；``None`` 现在的含义是"用装配好的
    那条"，而不是"没有供给"。这里建的**只有进程内持有者**（``DerivedAccessTokenHolder``，
    每个进程一份、不落盘、重启即空，装的是派生出来的短期令牌）。**频率上界不在这里，
    也没有第二份进程内副本**：它唯一的判据是凭据文件里的 ``refresh_consumed_at`` /
    ``refresh_consumed_count``，由 :meth:`~lingxi.adapters.delegated_credentials.
    HostFileDelegatedCredentialVault.claim_due` 在文件锁内判定（见该方法 docstring 与
    ``delegated_credentials.py`` 里"这是**唯一**的频率上界"那一条）——进程内副本认不出
    凭据代际，会在人工重授权之后继续拿旧账本拒绝一条全新的凭据。

    ``permission_table_access_token`` 是**权限发布表**读写所用的短期令牌供给。
    **默认不再是 ``None``**：产品负责人 2026-08-18 就 #226 裁定方向 3——用**应用身份**
    （``tenant_access_token``）写入，理由是"没有凭据生命周期，不需要用户再点授权、
    不会过期、不需要轮换"（同一天上午刚为专用授权凭据到期紧急处理过一次，再增加一条
    会轮换的凭据是代价最高的选项）；已知代价（产品负责人已知情）：写入不绑定到某个
    具体授权人，需要把应用加为该 Base 的协作者。因此这里建出一条进程内供给
    （:class:`~lingxi.core.permission.table_access_token_supply.
    PermissionTableAccessTokenProvider` 包住
    :class:`~lingxi.core.permission.tenant_token_supply.TenantAccessTokenSupply`
    包住 :class:`~lingxi.adapters.feishu_tenant_token.FeishuTenantTokenClient`），
    换取用的 ``app_id``/``app_secret`` 就是 scheduler 本来就必需的应用配置——**零新增
    凭据材料**，不新增任何环境变量、不需要产品负责人做任何新的授权动作。

    ``None`` 现在的含义与 ``roster_access_token`` 同一条：**"用装配好的那条"**，不是
    "没有供给"；调用方（主要是测试）传自己的实现即可替换。**两条各留各的注入点**：
    权限发布表与花名册在不同的 Base 上，供给形状一样但来源不同——这条走应用身份，
    花名册那条仍是 #215 的专用主体（未来若产品负责人改变权限表的裁定，只需要换这里
    的默认构造，参数与装配点不用动）。

    **本供给不放松 Issue #227 的翻译层硬闸**：翻译内容为空/不完整时，
    ``permission_refresh`` 一条发布意图都不会排进 outbox，与这里的令牌供给完全独立
    （防御纵深，见 ``docs/技术设计/验收矩阵-权限与银河.md`` 的 ``V-权限-13``）——接通这条供给只是
    让"已经在 outbox 里的意图能不能被真正写出去"这件事有了一个真实的执行者，不代表
    "outbox 里会不会出现内容"这件事发生了任何变化。
    """

    from lingxi.adapters.delegated_credentials import HostFileDelegatedCredentialVault
    from lingxi.adapters.feishu_directory import FeishuAuthorizationClient
    from lingxi.adapters.postgres_conversation import PostgresTaskQueue
    from lingxi.adapters.retention import RETENTION_CLEANUP_TIMEOUTS, PostgresRetentionCleaner
    from lingxi.apps.scheduler.onboarding import build_stock_token_source

    # 一个停止标志贯穿所有职责：SIGTERM 只设它一次，全部职责同时停止领取新工作。
    stop = threading.Event()
    sink = audit if audit is not None else StructuredLogAuditSink()

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
    # 权限链到期清理与上面两类清理同组，排在它们之后、发布消费之前。位置的唯一含义是
    # 同一轮内的先后：一条 payload 已经过期的意图应当先被擦成 ``'{}'``，再轮到发布面
    # 认领它并以 ``invalid`` 失败关闭——反过来会让一份九十天前决定的权限在到期当轮还被
    # 写进外部表格一次。晚一轮清理没有任何后果（到期时间不因清理迟到而后移，断言
    # ``V-保留-16``）。
    permission_retention = _build_permission_retention_duty(config, stop=stop, audit=sink)
    # 内测轮采集内容的九十天到期删除（对抗审查 2026-09-02 C-7）。与上面几条清理同组，
    # 无条件装配：删自己库里的到期内容只需要连接串。生产该表恒空，这条每轮删 0 行。
    content_capture_retention = _build_content_capture_retention_duty(
        config, stop=stop, audit=sink
    )

    duties: list[Any] = [
        rotation,
        cleanup,
        idle_sweep,
        permission_retention,
        content_capture_retention,
    ]
    # 令牌供给：日报侧要令牌 → 持有者里有新鲜的就直接给，没有就让**凭据轮换职责**
    # 按需换一次（受最小间隔 + 每日上界双重保护，Issue #276）。日报自己不碰一次性
    # refresh_token。
    supply = (
        roster_access_token
        if roster_access_token is not None
        else RosterAccessTokenProvider(
            holder=holder,
            refresh=rotation.refresh_for_supply,
            audit=sink,
        )
    )
    roster_audit = _build_roster_audit_duty(
        config,
        stop=stop,
        audit=sink,
        roster_access_token=supply,
        on_send_outcome=(alerting_duty.send_outcome_callback() if alerting_duty else None),
    )
    if roster_audit is not None:
        duties.append(roster_audit)
    else:
        # 报告职责没装配——不管原因是管理群没配、Base 坐标没配还是令牌供给没配，
        # `_build_roster_audit_duty` 已经留下自己的 `roster_audit.*` 审计。花名册
        # 快照写入与管理群报告解耦（Issue #275）：这里独立判定"只写不发"的快照同步
        # 职责能不能装配——只要它自己的前提（Base 坐标 + 令牌供给）满足，快照就该
        # 被写，不因为管理群这个纯通知配置项一起停摆。两者互斥注册（同一时刻至多一个
        # 在触发花名册读取），见 `_build_roster_snapshot_sync_duty` 文档字符串。
        roster_snapshot_sync = _build_roster_snapshot_sync_duty(
            config, stop=stop, audit=sink, roster_access_token=supply
        )
        if roster_snapshot_sync is not None:
            duties.append(roster_snapshot_sync)
    _wire_daily_report_duty(duties, config, stop=stop, audit=sink, alerting_duty=alerting_duty)
    # 权限发布表令牌供给：Issue #226 裁定方向 3（应用身份）。换取用的 app_id/app_secret
    # 是 scheduler 本来就必需的应用配置，因此这里**总能**建出一条默认供给——不像花名册
    # 那条依赖凭据轮换职责先跑起来，应用身份没有"还没轮换过一次"这种中间状态。挪到每日
    # 重算之前构造：重算与发布面共用同一条供给、同一张表。
    from lingxi.adapters.feishu_tenant_token import FeishuTenantTokenClient
    permission_table_supply = (
        permission_table_access_token
        if permission_table_access_token is not None
        else PermissionTableAccessTokenProvider(
            fetch=TenantAccessTokenSupply(
                fetch=FeishuTenantTokenClient(
                    base_url=config.feishu_base_url,
                    app_id=config.feishu_app_id,
                    app_secret=config.feishu_app_secret,
                ).fetch,
            ),
            audit=sink,
        )
    )
    # 每日权限重算排在花名册审计（或与它互斥的快照写入）**之后**：同一轮里花名册快照先被换成今天的那一份，重算才可能通过它自己的新鲜度判据（`V-权限-07` 的「先花名册、再银河」）。位置只保证同一轮内的先后；"用的是今天的花名册"由职责自己的判据保证。
    #
    # ``metric_translation_map`` 是这一次调用**唯一一次**读取 ``lingxi/config/company_function_metric_map.toml`` 的结果（前置不齐、连读取都没发生时是 ``None``）；下面首次开通编排的发布闸复用**同一个对象**（Issue #227 开通侧整合），不在那里另读一份文件——两份来源迟早会漂移，而漂移的方向是错误发布，见 ``_build_permission_refresh_duty`` 与 ``_build_onboarding_duty`` 的参数文档。
    permission_refresh, metric_translation_map = _build_permission_refresh_duty(
        config, stop=stop, audit=sink
    )
    if permission_refresh is not None:
        duties.append(permission_refresh)
    # 发布消费排在每日重算**之后**：同一轮里重算先把当天的意图排进来，发布紧接着就能把
    # 它推出去，而不是白等一个调度周期。位置只保证同一轮内的先后；一条意图晚一轮被消费
    # 没有任何产品后果（outbox 本来就是异步的）。
    # S7 管理卡上下文补偿观察；仅新增 scheduler 侧接线，不改变 S8 的 CI/门禁规则。
    management_corrections = _build_management_correction_callback(
        config,
        audit=sink,
        on_send_outcome=(alerting_duty.send_outcome_callback() if alerting_duty else None),
    )
    permission_publish = _build_permission_publish_duty(
        config,
        stop=stop,
        audit=sink,
        permission_table_access_token=permission_table_supply,
        on_management_corrections=management_corrections,
    )
    if permission_publish is not None:
        duties.append(permission_publish)
    # 组织快照同步（Issue #250）排在发布消费**之后**、首次开通编排**之前**：它是开通链
    # 身份定位那一步唯一的数据来源（``PostgresOrgSnapshotStore.lookup``），排在开通
    # 之前才可能让**同一轮**新装配、第一次真的跑成功的那次同步，被紧随其后的开通认领
    # 用上，少等一整个调度周期。它与花名册审计、每日权限重算之间没有数据依赖（互不
    # 读对方的表），因此位置只服务这一个"同一轮内先后"的收益，不构成新的顺序约束。
    #
    # 用户身份路径复用**同一个**令牌供给 ``supply``——那条一次性 refresh_token 全系统
    # 只允许一个消费者，与开通链的在职状态实时回读、花名册日报是同一条（2026-08-08
    # 授权码被烧的事故形状，不新增第二个消费者）。应用身份路径复用权限发布表那条
    # ``permission_table_supply``：tenant_access_token 不是一次性凭据、没有消费者数量
    # 上限，两处消费的是同一类令牌，复用只是省一次多余的换取，不产生新凭据材料。
    org_snapshot_sync = _build_org_snapshot_sync_duty(
        config,
        stop=stop,
        audit=sink,
        user_access_token=supply,
        app_access_token=permission_table_supply,
    )
    if org_snapshot_sync is not None:
        duties.append(org_snapshot_sync)
    # 首次开通编排（Epic D / S-D-02）排在发布消费**之后**：同一轮里发布面先把上一轮排出
    # 的意图推出去，开通链的发布等待才可能在同一轮内看到 ``published``。位置只保证同一轮
    # 内的先后；晚一轮没有产品后果（开通链自己会等）。
    #
    # 在职状态回读复用**同一个**令牌供给 ``supply``：它就是这次把编排搬进本进程的理由
    # ——那条一次性 refresh_token 全系统只允许一个消费者，而它已经在这里了。
    #
    # **装配不变量（外部集成面审查 F1）**：``onboarding != None ⇒ permission_publish !=
    # None 且 permission_publish.publish_wired``。这里把上面刚刚装配出的
    # ``permission_publish``（可能是 ``None``、也可能是只装了就绪面的半个职责）原样交给
    # `_build_onboarding_duty` 校验——不在这里另写一次判断，唯一的真相来源是那个对象
    # 本身，见 `_build_onboarding_duty` 文档字符串「装配不变量」一节的完整理由。
    onboarding = _build_onboarding_duty(
        config,
        stop=stop,
        audit=sink,
        employment_access_token=supply,
        metric_translation_map=metric_translation_map,
        permission_publish=permission_publish,
        stock_tokens=build_stock_token_source(config, access_token=permission_table_supply, audit=sink),
        onboarding_failed=(
            alerting_duty.onboarding_failed_callback() if alerting_duty is not None else None
        ),
    )
    if onboarding is not None:
        duties.append(onboarding)
        if not config.admin_group_chat_id:
            # 独立审查 codex P1-4：开通职责已注册（会真的产生 INTERNAL_ERROR /
            # SYNC_TIMEOUT 终态），`onboarding_failed` 回调也已经接上（只要
            # `alerting_duty` 存在，`main()` 里恒定存在），但没有配
            # `LINGXI_ADMIN_GROUP_CHAT_ID` 时 `AlertDispatcher` 的送达出口会退化成
            # `_LogOnlyAlertSender`（`V-告警-08` 既定语义：不失败关闭，状态机照常
            # 运行）——用户看到的「已转交管理员处理」这句承诺因此没有任何人会真的
            # 看到。这不是一个需要拒绝启动的错误（LogOnly 是产品接受的降级形态），
            # 但必须在启动期留一条响亮日志，不能让这个组合悄悄运行。
            logger.warning(
                "已注册首次开通编排，但未配置 LINGXI_ADMIN_GROUP_CHAT_ID："
                "「已转交管理员处理」的送达面将退化为仅结构化日志，管理群收不到任何"
                "开通失败 / 同步超时告警（V-告警-08 既定语义，不阻止启动）"
            )
            sink.record(
                "onboarding.admin_alert_channel_missing",
                reason="missing_environment_variable",
                variable="LINGXI_ADMIN_GROUP_CHAT_ID",
            )
    # 迟到就绪恢复（V-开通-18）排在首次开通编排**之后**：它服务的是首次开通那次阻塞
    # 确认已经判过超时、后续再也没有人回来看的用户，位置只是"同一轮内先声明的职责先
    # 跑"的自然顺序，不构成数据依赖——它按自己的十五分钟节奏在候选查询里判到期，不是
    # 每轮都真的发探针。**总能注册**（没有可选前置会让它整体不装配），因此不需要
    # `if ... is not None` 判断。
    duties.append(_build_late_readiness_recovery_duty(config, stop=stop, audit=sink))
    # 开通中途停摆收口（Issue #282，`V-开通-19`）排在迟到就绪恢复**之后**：两者是同一
    # 量级的"回来看已经安静下来的开通"职责，位置只是"同一轮内先声明的职责先跑"的自然
    # 顺序，不构成数据依赖——两者的候选集合按各自的判据互补（见
    # `adapters/postgres_stalled_provisioning.py` 模块文档），不会互相抢候选。**总能
    # 注册**（没有可选前置会让它整体不装配），因此不需要 `if ... is not None` 判断。
    duties.append(
        _build_stalled_provisioning_duty(
            config,
            stop=stop,
            audit=sink,
            alert=(
                alerting_duty.onboarding_stalled_callback()
                if alerting_duty is not None
                else None
            ),
        )
    )
    _wire_document_delivery_maintenance_duty(duties, config, stop=stop, audit=sink, alerting_duty=alerting_duty)
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
