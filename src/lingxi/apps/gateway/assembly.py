"""``lingxi-gateway`` 的装配层：按配置把 adapters 注入 core，装出一个 supervisor。

``apps/`` 只做组装，不写业务规则。第三方 SDK 一律在函数体内延迟 import（与
``apps/scheduler`` 同惯例），让不需要它们的进程不必装这些依赖。

装配开关的统一姿态：**安全落点在数据判定，不在装配开关**。管理命令面、群聊引导、
定向重算都无条件装配——登记表为空、没配机器人标识、没有待办时它们各自什么都不做，
行为与完全不装配逐字节一致；用开关控制这些能力只会多出一种"配错就静默失效"的形态。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from lingxi.adapters.feishu_longconn import BackoffPolicy, LongConnectionSupervisor
from lingxi.core.conversation.pipeline import DispatchGates, EventPipeline
from lingxi.core.conversation.ports import OnboardingRunner

from .audit_log import LoggingAudit
from .config import GatewayConfig
from .event_handler import make_event_handler
from .group_mention_hint import GroupMentionHintResponder, build_group_mention_hint_throttle
from .management_cards import (
    ManagementCardRecoveryScanner,
    ManagementCardRefresher,
    RecomputeResultReporter,
)
from .onboarding import _RecordingOnboarding

logger = logging.getLogger("lingxi.apps.gateway")

#: 管理群终态通知专用的去重前缀。它与花名册日报、内测每日通报共用同一个群发接口，
#: 但是另一条独立投递语义，必须用自己的前缀，否则同一天两条去重键相同的通知会被
#: 服务端误判成重试而丢弃。命名以 ``_UUID_PREFIX`` 结尾，好让全仓的长度预算扫描
#: 覆盖到它：13 + 32 = 45 字符，在 50 字符上限内。
ADMIN_NOTICE_UUID_PREFIX = "lingxi-admin-"


@dataclass(frozen=True)
class _AdminStack:
    """管理命令面这一整套装配好的对象。

    Attributes:
        router: 管理命令路由，同时供管线分流与卡片回调转译复用同一个实例。
        card_callback: 卡片回调处理器。
        context_store: 管理卡的持久上下文端口。
        recovery: 管理卡视觉状态的恢复扫描器。
    """

    router: Any
    card_callback: Any
    context_store: Any
    recovery: ManagementCardRecoveryScanner


def build_supervisor(
    config: GatewayConfig,
    *,
    transport: Any = None,
    should_stop: Callable[[], bool] | None = None,
    onboarding: OnboardingRunner | None = None,
    heartbeat: Callable[[], None] | None = None,
    on_onboarding_assembled: Callable[[OnboardingRunner], None] | None = None,
) -> LongConnectionSupervisor:
    """按配置装出一个 supervisor。

    ``transport`` 留空时按配置建真实长连接——真实长连接属受控验收，全部本机断言注入假
    实现。``onboarding`` 留空时用只记事件的惰性实现，既不会把未开通正文悄悄交给下游，
    也不会误报已开通。``should_stop`` 让停机跳过尽力而为的出站回复，不把停机拖过预算。

    ``on_onboarding_assembled`` 回报本函数**最终采用**的那个开通实现，供装配断言核对。
    用回调而不是从返回的 supervisor 上读回来：它的公开面被结构断言冻结成三个成员，不该
    为了自证装配而长出新的公开属性。
    """
    from lingxi.adapters.feishu_longconn import LarkEventTransport

    audit = LoggingAudit()
    effective_onboarding = onboarding or _RecordingOnboarding()
    if on_onboarding_assembled is not None:
        on_onboarding_assembled(effective_onboarding)

    client = _build_outbound_client(config)
    admin = _build_admin_stack(config, audit=audit, client=client)
    handle_event = _build_event_handler(
        config,
        audit=audit,
        client=client,
        admin=admin,
        onboarding=effective_onboarding,
        should_stop=should_stop,
    )

    def _management_card_heartbeat() -> None:
        admin.recovery.scan_if_due()
        if heartbeat is not None:
            heartbeat()

    return LongConnectionSupervisor(
        transport=transport or LarkEventTransport(**_transport_timeouts(config)),
        handle_event=handle_event,
        backoff=BackoffPolicy(
            base_seconds=config.reconnect_base_seconds,
            factor=config.reconnect_factor,
            ceiling_seconds=config.reconnect_ceiling_seconds,
        ),
        audit=audit.record,
        heartbeat=_management_card_heartbeat,
    )


def _build_event_handler(
    config: GatewayConfig,
    *,
    audit: Any,
    client: Any,
    admin: _AdminStack,
    onboarding: OnboardingRunner,
    should_stop: Callable[[], bool] | None,
) -> Callable[[dict], dict | None]:
    """把管线、管理卡回调与群聊引导接成长连接要的那一个事件处理函数。

    群聊引导复用同一个回复实现与同一个 SDK 客户端，不为这条边界分支单独开一条出站路径。
    """
    from lingxi.adapters.feishu_outbound import LarkReactions, LarkReplies
    from lingxi.adapters.postgres_conversation import PostgresGatewayStore

    replies = LarkReplies(client)
    pipeline = EventPipeline(
        store=PostgresGatewayStore(str(config.postgres_dsn), timeouts=config.postgres_timeouts),
        reactions=LarkReactions(client),
        replies=replies,
        audit=audit,
        onboarding=onboarding,
        gates=_build_dispatch_gates(config, audit=audit, admin_router=admin.router),
        should_stop=should_stop,
    )
    return make_event_handler(
        pipeline,
        audit=audit,
        card_callback_handler=admin.card_callback,
        group_mention_hint=GroupMentionHintResponder(
            bot_open_id=config.bot_open_id,
            replies=replies,
            audit=audit,
            throttle=build_group_mention_hint_throttle(),
        ),
        management_card_context_store=admin.context_store,
    )


def _build_outbound_client(config: GatewayConfig) -> Any:
    """建出站 SDK 客户端；超时从停机预算里分配，不用 SDK 的 30 秒默认值。

    默认值比停机预算本身还长，一次卡住的加表情或回复就能让停机超出承诺。取四分之一：
    一条事件最多经历「加表情 ＋ 一次回复」两次出站，各留一份余量。确认卡片与业务问数
    共用同一个客户端实例，不为两个用途各建一份。
    """
    from lingxi.adapters.feishu_outbound import build_client

    return build_client(
        app_id=config.app_id,
        app_secret=str(config.app_secret),
        timeout_seconds=max(1.0, config.shutdown_timeout_seconds / 4),
    )


def _transport_timeouts(config: GatewayConfig) -> dict[str, Any]:
    """真实长连接的四个参数，三个超时全部由停机超时推导。

    停机信号最晚在一个空闲轮询间隔之后被看见，因此这个间隔必须由停机超时推导，而不是
    取一个与它无关的常数——否则配置里的超时就是一句没有实现的承诺。单条事件的处理
    上限与建连截止时间同理取停机超时本身：比它更长的话，一条卡住的事件就能让停机超出
    承诺；建连超时还堵住了「从未连上」这个活性黑洞。
    """
    return {
        "app_id": config.app_id,
        "app_secret": str(config.app_secret),
        "poll_seconds": max(0.1, config.shutdown_timeout_seconds / 4),
        "ack_timeout_seconds": config.shutdown_timeout_seconds,
        "handshake_timeout_seconds": config.shutdown_timeout_seconds,
    }


def _build_dispatch_gates(config: GatewayConfig, *, audit: Any, admin_router: Any) -> DispatchGates:
    """把三道分流闸在装配期解析成值，管线自己不再持有任何查询能力。

    专用主体在这里读**一次**登记表，结果是一个普通字符串；读不到（登记表还没有行，
    或这一次读取失败）时这道闸整体是惰性的——不是"失败关闭挡住所有消息"，而是"这一次
    没有额外的结构性防护"。数据漂移场景本身极端罕见，用它换"gateway 启动不因为一次
    瞬时数据库故障而整体失败"更划算，真正兜底的仍然是数据库触发器。

    内测名单在装配期已经解析好，这里只把它包成判定口，不重新读环境变量：空集合
    （未配置）＝对任何人返回 ``False``，与「默认关闭＝全拒」的既有语义一致。
    """
    # 刻意从 `delegated_subject_lookup` 而不是 `delegated_credentials` 导入：后者的
    # 其它函数用到 cryptography，而 gateway 的依赖组明确不含它——gateway 不碰 Fernet。
    from lingxi.adapters.delegated_subject_lookup import registered_delegated_subject_open_id
    from lingxi.core.identity.innertest_roster_gate import is_open_id_innertest_allowed

    try:
        delegated_subject_open_id = registered_delegated_subject_open_id(
            str(config.postgres_dsn), timeouts=config.postgres_timeouts
        )
    except Exception as error:
        delegated_subject_open_id = None
        audit.record("gateway.delegated_subject_lookup_failed", error=type(error).__name__)

    def innertest_roster_gate(open_id: str) -> bool:
        return is_open_id_innertest_allowed(open_id, config.innertest_roster_open_ids)

    return DispatchGates(
        admin_router=admin_router,
        innertest_roster_gate=innertest_roster_gate,
        delegated_subject_open_id=delegated_subject_open_id,
    )


def _build_admin_stack(config: GatewayConfig, *, audit: Any, client: Any) -> _AdminStack:
    """装配管理命令面：查询口、待确认操作、确认卡、管理卡、回调处理器。

    查询端口各自开自己的连接，不共享管线的事务——管理查询是只读的，不需要参与入站
    事件那个写事务。三个只读查询（人、公司、指标别名）集中在同一个实例上，没有理由
    为了"各自独立声明端口"而建三份连接。
    """
    from lingxi.adapters.admin_registry import PostgresAdminQueries
    from lingxi.adapters.feishu_admin_card import LarkAdminCardTransport
    from lingxi.adapters.postgres_pending_action import PostgresPendingActionStore

    display_names = PostgresAdminQueries(
        str(config.postgres_dsn), timeouts=config.postgres_timeouts
    )
    # 待确认操作在确认时刻重新读一次登记表（合同「确认时重新读取当前角色」），但**不**
    # 复用上面那个独立查询口：那条查询走另一条连接，读到的角色不受行锁保护，会在"读到
    # 角色"与"提交这次确认"之间留出一个 TOCTOU 窗口。这个 store 自己在确认的同一事务、
    # 同一连接上对登记表加共享锁，因此不需要在这里注入登记表查询口。
    pending_action_store = PostgresPendingActionStore(
        str(config.postgres_dsn),
        timeouts=config.postgres_timeouts,
        audit=audit,
        metric_map_path=config.metric_map_path,
    )
    # 确认卡片的出站发送与回调后的终态更新共用同一个卡片传输实例。
    admin_card_transport = LarkAdminCardTransport(client)
    cards = _build_management_card_stack(
        config, audit=audit, client=client, display_names=display_names
    )
    router = _build_router(
        config,
        audit=audit,
        display_names=display_names,
        pending_actions=pending_action_store,
        confirm_transport=admin_card_transport,
        management_cards=cards.dispatcher,
    )
    return _AdminStack(
        router=router,
        card_callback=_build_card_callback(
            config,
            audit=audit,
            display_names=display_names,
            pending_actions=pending_action_store,
            confirm_cards=admin_card_transport,
            router=router,
            cards=cards,
        ),
        context_store=cards.context_store,
        recovery=cards.recovery,
    )


def _build_router(
    config: GatewayConfig,
    *,
    audit: Any,
    display_names: Any,
    pending_actions: Any,
    confirm_transport: Any,
    management_cards: Any,
) -> Any:
    """装配管理命令路由：登记表判定、只读查询、待确认操作、确认卡与管理卡。"""
    from lingxi.adapters.admin_registry import PostgresAdminRegistryLookup
    from lingxi.core.admin.card_dispatch import ConfirmCardDispatcher
    from lingxi.core.admin.router import AdminCommandRouter

    return AdminCommandRouter(
        registry=PostgresAdminRegistryLookup(
            str(config.postgres_dsn), timeouts=config.postgres_timeouts
        ),
        queries=display_names,
        audit=audit,
        display_names=display_names,
        pending_actions=pending_actions,
        confirm_cards=ConfirmCardDispatcher(
            transport=confirm_transport,
            tracker=pending_actions,
            audit=audit,
            display_names=display_names,
        ),
        management_cards=management_cards,
    )


def _build_card_callback(
    config: GatewayConfig,
    *,
    audit: Any,
    display_names: Any,
    pending_actions: Any,
    confirm_cards: Any,
    router: Any,
    cards: _ManagementCardStack,
) -> Any:
    """装配卡片回调处理器：确认/取消卡、管理卡表单、定向重算触发。

    ``router`` 复用管理命令面同一个实例——管理卡的表单提交与逐行收回被转译成等价的命令
    文本，交给它走全部既有写路径判定（角色核对、自我目标防呆、准备、确认卡、审计），
    不重新实现一遍。回调的网络往返一律排在应答之后。
    """
    from lingxi.adapters.admin_post_callback import BackgroundPostCallbackExecutor
    from lingxi.core.admin.card_callback import AdminCardCallbackHandler

    return AdminCardCallbackHandler(
        pending_actions=pending_actions,
        confirm_cards=confirm_cards,
        group_notifier=_build_group_notifier(config),
        group_chat_id=config.admin_group_chat_id,
        audit=audit,
        display_names=display_names,
        management_actions=router,
        management_context_store=cards.context_store,
        management_state_lookup=cards.status_lookup,
        management_card_refresher=cards.refresher,
        recompute_trigger=_build_recompute_trigger(config, audit=audit, reporter=cards.reporter),
        post_callback_executor=BackgroundPostCallbackExecutor(audit=audit),
    )


@dataclass(frozen=True)
class _ManagementCardStack:
    """管理卡这一支装配好的对象，供路由、回调与心跳各取所需。"""

    dispatcher: Any
    context_store: Any
    refresher: ManagementCardRefresher
    recovery: ManagementCardRecoveryScanner
    reporter: RecomputeResultReporter
    status_lookup: Callable[[str], Any]


def _build_management_card_stack(
    config: GatewayConfig, *, audit: Any, client: Any, display_names: Any
) -> _ManagementCardStack:
    """装配管理卡的发送、刷新、恢复与后台结果回写。

    上下文（目标、卡片实体、快照与序号）由数据库持久保存，发送侧登记与回调侧读取共用
    同一个端口，进程重启后仍能恢复。启动时先恢复一次；失败只留下水位，不能阻止 gateway
    建立长连接，后续每次心跳再重试。
    """
    from lingxi.adapters.feishu_admin_card import (
        LarkAdminManagementCardTransport,
        TomlCompanyMetricCatalog,
    )
    from lingxi.adapters.postgres_management_card_context import (
        PostgresManagementCardContextStore,
    )
    from lingxi.core.admin.card_dispatch import ManagementCardDispatcher

    transport = LarkAdminManagementCardTransport(client)
    catalog = TomlCompanyMetricCatalog(metric_map_path=config.metric_map_path)
    context_store = PostgresManagementCardContextStore(
        str(config.postgres_dsn), timeouts=config.postgres_timeouts
    )
    refresher = ManagementCardRefresher(
        transport=transport,
        catalog=catalog,
        display_names=display_names,
        context_store=context_store,
    )
    status_lookup = _management_status_lookup(display_names)
    recovery = ManagementCardRecoveryScanner(
        context_store=context_store,
        refresher=refresher,
        status_lookup=status_lookup,
        audit=audit,
    )
    recovery.scan()
    return _ManagementCardStack(
        dispatcher=ManagementCardDispatcher(
            transport=transport,
            catalog=catalog,
            audit=audit,
            display_names=display_names,
            context_store=context_store,
        ),
        context_store=context_store,
        refresher=refresher,
        recovery=recovery,
        reporter=RecomputeResultReporter(
            context_store=context_store,
            refresher=refresher,
            status_lookup=status_lookup,
            audit=audit,
        ),
        status_lookup=status_lookup,
    )


def _management_status_lookup(display_names: Any) -> Callable[[str], Any]:
    """按管理卡上下文里的展示标识读取当前状态。

    管理员可能是用邮箱发起的查询，上下文要保留这个原始标识好让卡片刷新继续显示同一个
    输入；读库前则复用已有的邮箱解析，否则重启后或异步刷新时会把邮箱当成用户标识而
    读不到目标。
    """

    def lookup(identifier: str) -> Any:
        resolver = getattr(display_names, "resolve_identifier", None)
        resolved = resolver(identifier=identifier) if callable(resolver) else identifier
        return display_names.user_status(identifier=resolved)

    return lookup


def _build_group_notifier(config: GatewayConfig) -> Any:
    """管理群脱敏通知；没配管理群会话时返回 ``None``，回调侧直接跳过。

    这是"一个尚未接线的可选职责"，不让整个进程起不来。通知走纯文本群发，结构上不支持
    卡片或按钮——管理群只能收到脱敏通知，不能触发管理动作。
    """
    if config.admin_group_chat_id is None:
        return None
    from lingxi.adapters.feishu_group_message import FeishuGroupMessages

    return FeishuGroupMessages(
        base_url=config.feishu_base_url,
        app_id=config.app_id,
        app_secret=str(config.app_secret),
        uuid_prefix=ADMIN_NOTICE_UUID_PREFIX,
    )


def _build_recompute_trigger(
    config: GatewayConfig, *, audit: Any, reporter: RecomputeResultReporter
) -> Any:
    """定向权限重算的触发口，包一层后台执行。

    **不直接注入同步适配器**：它一次要跑五到六个网络往返，同步调用会让卡片回调的应答
    一直等它跑完。包一层之后触发只入队立即返回，真正的重算在后台单线程里串行执行，
    失败仍走同一条审计出口。它内部的每一个依赖都只需要 gateway 本来就有的数据库连接与
    随包发布的静态映射，因此无条件装配；失败降级回每日批，不影响确认结果本身。
    """
    from lingxi.adapters.postgres_permission_recompute_trigger import (
        BackgroundPermissionRecomputeTrigger,
        PermissionRecomputeAdapter,
    )

    return BackgroundPermissionRecomputeTrigger(
        PermissionRecomputeAdapter(
            str(config.postgres_dsn),
            timeouts=config.postgres_timeouts,
            audit=audit,
            metric_map_path=config.metric_map_path,
        ),
        audit=audit,
        on_completed=reporter.on_completed,
        on_queued=reporter.on_queued,
        on_failed=reporter.on_failed,
        on_skipped=reporter.on_skipped,
        on_timeout=reporter.on_timeout,
    )
