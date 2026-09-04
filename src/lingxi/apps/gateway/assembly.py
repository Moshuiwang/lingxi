"""``lingxi-gateway`` 的装配层：按配置把 adapters 注入 core，装出一个 supervisor。

``apps/`` 只做组装，不写业务规则。第三方 SDK 一律在函数体内延迟 import（与
``apps/scheduler`` 同惯例），让不需要它们的进程不必装这些依赖。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from lingxi.adapters.feishu_longconn import BackoffPolicy, LongConnectionSupervisor
from lingxi.core.admin.card_dispatch import management_card_fingerprint
from lingxi.core.conversation.pipeline import DispatchGates, EventPipeline
from lingxi.core.conversation.ports import OnboardingRunner
from lingxi.core.permission.targeted_recompute import RecomputeKind

from .audit_log import _LoggingAudit
from .config import GatewayConfig
from .event_handler import make_event_handler
from .group_mention_hint import GroupMentionHintResponder, build_group_mention_hint_throttle
from .management_cards import (
    _MANAGEMENT_PUBLISH_OBSERVE_SECONDS,
    _MANAGEMENT_PUBLISH_POLL_SECONDS,
    _GatewayManagementCardRefresher,
    _ManagementCardRecoveryScanner,
)
from .management_status import PUBLISHING_STATUS_TEXT, skipped_recompute_status_message
from .onboarding import _RecordingOnboarding

logger = logging.getLogger("lingxi.apps.gateway")

#: 管理群终态通知（Issue #96 S-M-02）专用去重前缀——与花名册日报
#: （``DELIVERY_UUID_PREFIX``）、内测每日通报（``DAILY_REPORT_UUID_PREFIX``）共用
#: 同一个 `im/v1/messages` 接口，但是另一条独立投递语义，必须使用自己的前缀（同一
#: 纪律见 ``adapters/feishu_group_message.py`` 的 ``uuid_prefix`` 参数文档）。命名
#: 以 ``_UUID_PREFIX`` 结尾，落在 ``tests/test_scheduler_daily_report_assembly.py``
#: 的 AST 预算扫描范围内（外部审查交叉裁定，opus P2-3：此前是函数体内联字符串，
#: 不受该预算测试覆盖）。取值 13 + 32 = 45 字符，在飞书 50 字符上限内。
ADMIN_NOTICE_UUID_PREFIX = "lingxi-admin-"


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

    ``transport`` 和 ``onboarding`` 都是注入口：真实长连接与真实身份/权限外部链属
    L4a，全部 L2/L3 断言注入假实现。``onboarding`` 未提供时采用失败关闭 runner，
    不会把未开通正文悄悄交给下游，也不会误报已开通。
    adapters 在函数体内延迟 import，与 ``apps/scheduler`` 的 ``build_loop`` 同惯例。

    ``on_onboarding_assembled`` 回调**只报告本函数最终采用的那个 runner**，供装配层做
    开通装配断言（``apps/gateway/onboarding.assert_gateway_onboarding_is_inert``）。
    为什么要一个回调而不是从返回的 supervisor 上读回来：``LongConnectionSupervisor``
    的公开面被 `V-接入-10` 的结构断言冻结成 ``{run, reconnect_attempts,
    observed_delays}``——它不得出现第二个可以投递事件的公开入口，因此也不该为了装配
    自证而长出新的公开属性。回调不接触事件通路，只把"本函数决定用哪一个"这件事说出来，
    而那正是断言要验的东西（缺省落桩就发生在这一行）。
    """

    from lingxi.adapters.admin_post_callback import BackgroundPostCallbackExecutor
    from lingxi.adapters.admin_registry import PostgresAdminQueries, PostgresAdminRegistryLookup
    # 刻意从 `delegated_subject_lookup`（不是 `delegated_credentials`）导入：后者
    # 的其它函数用到 cryptography（Fernet 密文读写），而 `pyproject.toml` 的
    # `gateway` extras 组明确不含 cryptography——gateway 不碰 Fernet（2026-08-18
    # 裁定，首次开通编排住在 scheduler）。两个模块名对同一个只读查询各自的取舍
    # 见 `adapters/delegated_subject_lookup.py` 模块文档。
    from lingxi.adapters.delegated_subject_lookup import registered_delegated_subject_open_id
    from lingxi.adapters.feishu_admin_card import (
        LarkAdminCardTransport,
        LarkAdminManagementCardTransport,
        TomlCompanyMetricCatalog,
    )
    from lingxi.adapters.feishu_group_message import FeishuGroupMessages
    from lingxi.adapters.feishu_longconn import LarkEventTransport
    from lingxi.adapters.feishu_outbound import LarkReactions, LarkReplies, build_client
    from lingxi.adapters.postgres_conversation import PostgresGatewayStore
    from lingxi.adapters.postgres_management_card_context import (
        PostgresManagementCardContextStore,
    )
    from lingxi.adapters.postgres_pending_action import PostgresPendingActionStore
    from lingxi.adapters.postgres_permission_recompute_trigger import (
        BackgroundPermissionRecomputeTrigger,
        PermissionRecomputeAdapter,
    )
    from lingxi.core.admin.card_callback import AdminCardCallbackHandler
    from lingxi.core.admin.card_dispatch import ConfirmCardDispatcher, ManagementCardDispatcher
    from lingxi.core.admin.router import AdminCommandRouter
    from lingxi.core.identity.innertest_roster_gate import is_open_id_innertest_allowed

    audit = _LoggingAudit()
    effective_onboarding = onboarding or _RecordingOnboarding()
    if on_onboarding_assembled is not None:
        on_onboarding_assembled(effective_onboarding)

    # 出站 HTTP 的超时从停机预算里分配，而不是用 SDK 的 30 秒默认值——后者比预算
    # 本身还长，一次卡住的加表情或回复就能让停机超出承诺（codex 二轮 P1-C）。取
    # 四分之一：一条事件最多经历「加表情 + 一次回复」两次出站，各留一份余量。
    # 提前到这里构造（此前紧跟在 pipeline 之前）：确认卡片（Issue #96 S-M-02）
    # 与业务问数共用同一个 SDK 客户端实例，不为两个用途各建一份。
    outbound_timeout = max(1.0, config.shutdown_timeout_seconds / 4)
    client = build_client(
        app_id=config.app_id,
        app_secret=str(config.app_secret),
        timeout_seconds=outbound_timeout,
    )

    # 管理命令面（Issue #95 S-M-01）：无条件装配，不受任何 feature flag 控制——安全
    # 落点在数据判定，不在装配开关。登记表为空（尚未播种，例如新环境或 biai-stage
    # 升级前）时 ``active_entry`` 对任何 open_id 都返回 None，`route()` 恒
    # ``handled=False``，管线原样落回既有业务/专用账号提示分支，行为与完全不装配
    # 这个参数时逐字节一致。查询端口各自开自己的连接（与 ``registered_
    # delegated_subject_open_id`` 同型），不共享 pipeline 自己的 ``PostgresGatewayStore``
    # 事务——管理查询是只读的，不需要参与入站事件那个写事务。
    admin_registry_lookup = PostgresAdminRegistryLookup(
        str(config.postgres_dsn), timeouts=config.postgres_timeouts
    )
    # 待确认操作（Issue #96 S-M-02）：confirm() 在确认时刻重新读一次登记表（合同
    # "确认时重新读取……当前角色"），但不复用 admin_registry_lookup 这个独立查询口
    # ——那条查询走另一条连接，读到的角色不受任何行锁保护，会在"读到角色"与"提交
    # 这次确认"之间留出一个 TOCTOU 窗口（外部审查交叉裁定，codex P1-4）。
    # PostgresPendingActionStore 自己在 confirm() 的同一事务、同一连接上对
    # admin_registry 取 FOR SHARE，因此不再需要在这里注入 registry。
    pending_action_store = PostgresPendingActionStore(
        str(config.postgres_dsn),
        timeouts=config.postgres_timeouts,
        audit=audit, metric_map_path=config.metric_map_path,
    )
    # 管理员可见展示名解析口（Trace #469 S-1）：open_id→姓名+邮箱、公司编号→
    # 中文名、指标 ID→中文别名三个真库/真配置查询集中在 ``PostgresAdminQueries``
    # 一个实例上（结构性实现 ``AdminDisplayNames``，不继承），与下面 ``queries=``
    # 复用同一个对象——两个 Protocol 的调用面不同，但没有理由为了"各自独立声明
    # Protocol"这条既有惯例而在这里也建两份连接。
    admin_display_names = PostgresAdminQueries(
        str(config.postgres_dsn), timeouts=config.postgres_timeouts
    )
    # 确认卡片的出站发送与回调后的终态更新共用同一个 CardKit 传输实例。
    admin_card_transport = LarkAdminCardTransport(client)
    confirm_card_dispatcher = ConfirmCardDispatcher(
        transport=admin_card_transport,
        tracker=pending_action_store,
        audit=audit,
        display_names=admin_display_names,
    )
    # 用户权限管理卡发送侧（#439 B 档，Trace #445 opus 审查坐实并修复）：此前
    # 只有渲染层（`core/admin/management_card.render_management_card`）接进
    # `AdminCommandRouter`，从未有任何调用点真正装配 `ManagementCardDispatcher`/
    # 发送 transport——`management_cards` 恒为 ``None``，`/admin user` 的管理卡
    # 因此结构上从未真正发出过（`AdminCommandRouter._send_management_card` 对
    # ``None`` 直接短路返回，见该方法文档）。与确认卡片共用同一个 SDK 客户端
    # 实例（同上 `confirm_card_dispatcher` 的取舍，两者生命周期相同、都随本次
    # 装配一起建立）；发送失败只降级（`ManagementCardDispatcher` 自己的姿态，
    # 见该类文档），不影响 `/admin user` 既有的文本回复这条主路径。
    # 管理卡上下文（#493）由 PostgreSQL 持久保存；发送侧登记与回调侧读取共用同一
    # 个 store，gateway 重启后仍能恢复目标、卡片实体与 sequence。
    management_card_transport = LarkAdminManagementCardTransport(client)
    management_card_catalog = TomlCompanyMetricCatalog(metric_map_path=config.metric_map_path)
    # #493：message_id→目标、card_id、快照和 sequence 必须跨 gateway 重启保留，
    # 生产装配使用 PostgreSQL，而不是旧的进程内 TTL 映射。
    management_card_context_store = PostgresManagementCardContextStore(
        str(config.postgres_dsn), timeouts=config.postgres_timeouts
    )
    management_card_dispatcher = ManagementCardDispatcher(
        transport=management_card_transport,
        catalog=management_card_catalog,
        audit=audit,
        display_names=admin_display_names,
        context_store=management_card_context_store,
    )
    management_card_refresher = _GatewayManagementCardRefresher(
        transport=management_card_transport,
        catalog=management_card_catalog,
        display_names=admin_display_names,
        context_store=management_card_context_store,
    )

    def _lookup_management_status(identifier: str) -> Any:
        """按管理卡上下文中的展示标识读取当前状态。

        ``/admin user`` may have been addressed by邮箱。上下文要保留这个原始标识，
        让卡片刷新时继续显示同一输入；读数据库前则复用已有邮箱→open_id 解析，
        否则重启后/异步刷新时会把邮箱误当成 ``feishu_open_id`` 而读不到目标。
        """

        resolver = getattr(admin_display_names, "resolve_identifier", None)
        resolved = resolver(identifier=identifier) if callable(resolver) else identifier
        return admin_display_names.user_status(identifier=resolved)

    management_card_recovery = _ManagementCardRecoveryScanner(
        context_store=management_card_context_store,
        refresher=management_card_refresher,
        status_lookup=_lookup_management_status,
        audit=audit,
    )
    # 启动时先恢复一次；失败只留 needs_refresh 水位，不能阻止 gateway 建立长连接。
    # 后续每次长连接心跳由 scan_if_due() 重试，CardKit 成功后才清水位。
    management_card_recovery.scan()

    def _refresh_management_after_recompute(
        pending: Any,
        *,
        complete: bool,
        status_message: str | None = None,
        state_override: str | None = None,
    ) -> None:
        """后台重算/发布观察后把原管理卡推进到真实状态。"""

        origin_message_id = getattr(pending, "origin_card_message_id", None)
        if not origin_message_id:
            return
        try:
            context = management_card_context_store.lookup_context(message_id=origin_message_id)
            if context is None:
                return
            # 取消后即使重算线程晚到，也不能把已经关闭的管理卡重新打开；
            # 关闭是持久状态，后台结果只允许推进仍可见的卡片。
            if context.state == "closed":
                return
            status = _lookup_management_status(context.identifier)
            if status is None:
                return
            state = "effective" if complete else (state_override or "incomplete")
            if complete:
                dispatch_status = "已生效"
            elif state == "dispatching":
                dispatch_status = status_message or PUBLISHING_STATUS_TEXT
            else:
                trace = context.last_trace_id or "当前操作"
                dispatch_status = (
                    status_message
                    or f"下发未完成，最迟次日自动纠正 · 追溯号 {trace}"
                )
            machine_dispatch_status = (
                "effective"
                if complete
                else "publishing"
                if state == "dispatching"
                else "incomplete"
            )
            updated = management_card_context_store.update_state(
                message_id=origin_message_id,
                state=state,
                dispatch_status=machine_dispatch_status,
                snapshot_fingerprint=management_card_fingerprint(status),
            )
            if updated is not None:
                management_card_refresher.update(
                    context=updated,
                    status=status,
                    state=state,
                    dispatch_status=dispatch_status,
                )
        except Exception as error:  # noqa: BLE001 - result refresh is best effort
            audit.record(
                "admin.card_callback.management_card_refresh_failed",
                error=type(error).__name__,
                pending_action_id=getattr(pending, "id", ""),
            )

    def _recompute_completed(pending: Any) -> None:
        _refresh_management_after_recompute(pending, complete=True)

    def _start_management_publish_observer(pending: Any) -> None:
        """在 gateway 内部短暂观察 outbox，直到真实发布读回一致。

        定向重算只负责排出意图，不能把 ``ENQUEUED``/``REVOKED`` 直接翻译为「已生效」。
        发布消费在 scheduler 进程中完成，所以这里通过共享 PostgreSQL 状态观察结果；
        观察线程有界且 daemon 化，超时后留下的 ``incomplete`` 由每日批修正。
        """

        origin_message_id = getattr(pending, "origin_card_message_id", None)
        if not origin_message_id:
            return

        def observe() -> None:
            deadline = time.monotonic() + _MANAGEMENT_PUBLISH_OBSERVE_SECONDS
            while time.monotonic() < deadline:
                try:
                    publish_state = management_card_context_store.latest_publish_state_for_message(
                        message_id=origin_message_id
                    )
                except Exception as error:  # noqa: BLE001 - transient reads are retried
                    audit.record(
                        "admin.card_callback.management_publish_state_lookup_failed",
                        error=type(error).__name__,
                    )
                    publish_state = None
                if publish_state == "published":
                    _refresh_management_after_recompute(pending, complete=True)
                    return
                if publish_state in {"failed", "superseded"}:
                    _refresh_management_after_recompute(pending, complete=False)
                    return
                threading.Event().wait(_MANAGEMENT_PUBLISH_POLL_SECONDS)
            _refresh_management_after_recompute(pending, complete=False)

        threading.Thread(
            target=observe,
            name="lingxi-gateway-management-publish-observer",
            daemon=True,
        ).start()

    def _recompute_queued(pending: Any, outcome: Any) -> None:
        # ``UNCHANGED`` means the permission row already represented the desired
        # result and no new outbox intent was created. It is therefore effective
        # immediately; observing an unrelated older published outbox row would
        # otherwise be both racy and misleading, while waiting for the observer
        # would eventually turn a successful no-op into "未完成".
        if getattr(outcome, "kind", None) is RecomputeKind.UNCHANGED:
            _refresh_management_after_recompute(pending, complete=True)
            return
        _refresh_management_after_recompute(
            pending,
            complete=False,
            state_override="dispatching",
            status_message=PUBLISHING_STATUS_TEXT,
        )
        _start_management_publish_observer(pending)

    def _recompute_skipped(pending: Any, outcome: Any) -> None:
        """定向重算判 ``SKIPPED`` 的回执（Trace #521 F5，#493 P1-3）：这是常态出口不是故障，
        只有 ``account_not_enabled`` 有专属真话，其余跳过原因拿到 ``None``、逐字节不变仍走
        原失败文案（判据与措辞见 `.management_status`）。本地覆盖照常落库——``prepare`` 不读
        账号状态是既有产品语义，本次只是如实告知这一次不下发。"""

        _refresh_management_after_recompute(
            pending, complete=False, status_message=skipped_recompute_status_message(outcome)
        )

    def _recompute_failed(pending: Any, error: Exception | None) -> None:
        del error
        _refresh_management_after_recompute(pending, complete=False)

    def _recompute_timeout(pending: Any) -> None:
        _refresh_management_after_recompute(pending, complete=False)
        _start_management_publish_observer(pending)
    admin_router = AdminCommandRouter(
        registry=admin_registry_lookup,
        queries=admin_display_names,
        audit=audit,
        display_names=admin_display_names,
        pending_actions=pending_action_store,
        confirm_cards=confirm_card_dispatcher,
        management_cards=management_card_dispatcher,
    )
    # 管理群脱敏通知（Issue #96 S-M-02）：``admin_group_chat_id`` 未配置时——与既有
    # `admin_group_chat_id: str | None = None` 的既定取舍相同——这是"一个尚未接线
    # 的可选职责"，不让整个进程起不来；``AdminCardCallbackHandler`` 收到
    # ``group_notifier=None`` 时直接跳过群通知，不报错、不重试（V-管理-11：管理群
    # 从真实回调中只能收到脱敏通知，不能触发管理动作——本通知走
    # ``FeishuGroupMessages.send_text``，结构上只发纯文本，不支持卡片或按钮）。
    group_notifier: Any = None
    if config.admin_group_chat_id is not None:
        group_notifier = FeishuGroupMessages(
            base_url=config.feishu_base_url,
            app_id=config.app_id,
            app_secret=str(config.app_secret),
            # 与花名册日报、内测日报各自独立的 uuid 前缀同一纪律（见
            # adapters/feishu_group_message.py 的 delivery_uuid 文档）：13 字符，
            # 在全仓已钉住的 ≤18 字符预算内。
            uuid_prefix=ADMIN_NOTICE_UUID_PREFIX,
        )
    card_callback_handler = AdminCardCallbackHandler(
        pending_actions=pending_action_store,
        confirm_cards=admin_card_transport,
        group_notifier=group_notifier,
        group_chat_id=config.admin_group_chat_id,
        audit=audit,
        display_names=admin_display_names,
        # 管理卡表单提交/逐行收回（Issue #439 B 档接线）：复用已经在上面装好的
        # 同一个 AdminCommandRouter 实例——`handle_management_form_submit`/
        # `handle_management_revoke` 把管理卡交互转译成等价的 `/admin ...`
        # 命令文本，交给它的 `route()` 走全部既有写路径判定（角色核对、自我
        # 目标防呆、prepare()、确认卡发送、审计），不重新实现一遍（见
        # `core/admin/card_callback.py` `ManagementActionRouter` 文档）。
        management_actions=admin_router,
        management_context_store=management_card_context_store,
        management_state_lookup=_lookup_management_status,
        management_card_refresher=management_card_refresher,
        # 定向权限重算（Issue #438）：无条件装配，不受任何前置配置控制——它内部
        # 的每一个依赖都只需要 gateway 本来就有的 Postgres DSN 与随包发布的静态
        # 映射文件（见该适配器模块文档），失败降级回每日批,不影响确认结果本身。
        # **不直接注入 `PermissionRecomputeAdapter`**（Trace #445 opus 审查坐实
        # 并修复）：它的 `.trigger()` 有五到六次网络往返，同步调用会让回调应答
        # 等它跑完——包一层 `BackgroundPermissionRecomputeTrigger`，`trigger()`
        # 只入队立即返回，真正的重算在后台单线程里串行执行，失败仍走同一个
        # `admin.card_callback.recompute_trigger_failed` 审计姿态（见该适配器
        # 模块文档「BackgroundPermissionRecomputeTrigger」一节）。
        recompute_trigger=BackgroundPermissionRecomputeTrigger(
            PermissionRecomputeAdapter(
                str(config.postgres_dsn), timeouts=config.postgres_timeouts, audit=audit, metric_map_path=config.metric_map_path
            ),
            audit=audit,
            on_completed=_recompute_completed,
            on_queued=_recompute_queued,
            on_failed=_recompute_failed,
            on_skipped=_recompute_skipped,
            on_timeout=_recompute_timeout,
        ),
        # 应答之后才做那批网络往返（#493 块 B，见该适配器模块文档）。
        post_callback_executor=BackgroundPostCallbackExecutor(audit=audit),
    )
    # 专用主体结构性出口前置（opus P3-1）：装配期读**一次**登记表，把结果算成一个
    # 普通字符串交给管线——管线自己不再持有任何查询能力，对全体消息都只是内存
    # 比较（见 `EventPipeline.__init__` 的 `delegated_subject_open_id` 文档）。
    # `None`（登记表还没有行，或本次读取失败）时管线这一步整体是惰性的：不是
    # "失败关闭挡住所有消息"，而是"这一次没有额外的结构性防护"，退回到本项加入
    # 之前的行为——数据漂移场景本身极端罕见（结构上专用主体不该有 app_user 行），
    # 用它换取"gateway 启动不因为一次瞬时数据库故障而整体失败"更划算：真正兜底
    # 的仍然是 `V-身份-02` 的数据库触发器，这里只是纵深的一层。
    try:
        delegated_subject_open_id = registered_delegated_subject_open_id(
            str(config.postgres_dsn), timeouts=config.postgres_timeouts
        )
    except Exception as error:  # noqa: BLE001 - 见上方注释，读取失败按"本次无此防护"处理
        delegated_subject_open_id = None
        audit.record(
            "gateway.delegated_subject_lookup_failed", error=type(error).__name__
        )
    # 内测名单闸的 gateway 侧前移一份（Issue #302 S-N-01 的纵深）：装配期把已解析
    # 好的 `config.innertest_roster_open_ids` 包成判定口，不在管线里重新读环境
    # 变量或重新解析。空集合（未配置）＝对任何人返回 False，与该模块「默认关闭
    # ＝全拒」的既有语义一致。
    def innertest_roster_gate(open_id: str) -> bool:
        return is_open_id_innertest_allowed(open_id, config.innertest_roster_open_ids)

    # ``client``/``outbound_timeout`` 已在函数前部构造（供确认卡片传输复用，见上）。
    # 群聊@机器人固定引导（Issue #318）复用同一个 Replies 实现/同一个 SDK 客户端，
    # 不为这条边界分支单独开一条出站路径。无条件装配——与本文件其余「安全落点
    # 在数据判定，不在装配开关」同一姿态（见上方管理命令面同类注释）：未配置
    # `bot_open_id` 时 `GroupMentionHintResponder.maybe_respond` 对任何消息都
    # 直接返回，装配它本身不产生任何外部副作用或可观察行为变化。
    replies = LarkReplies(client)
    group_mention_hint = GroupMentionHintResponder(
        bot_open_id=config.bot_open_id,
        replies=replies,
        audit=audit,
        throttle=build_group_mention_hint_throttle(),
    )
    pipeline = EventPipeline(
        store=PostgresGatewayStore(str(config.postgres_dsn), timeouts=config.postgres_timeouts),
        reactions=LarkReactions(client),
        replies=replies,
        audit=audit,
        onboarding=effective_onboarding,
        gates=DispatchGates(
            admin_router=admin_router,
            innertest_roster_gate=innertest_roster_gate,
            delegated_subject_open_id=delegated_subject_open_id,
        ),
        # 停机时跳过尽力而为的出站回复，不让它把停机拖过预算。
        should_stop=should_stop,
    )
    def _management_card_heartbeat() -> None:
        management_card_recovery.scan_if_due()
        if heartbeat is not None:
            heartbeat()

    return LongConnectionSupervisor(
        transport=transport
        or LarkEventTransport(
            app_id=config.app_id,
            app_secret=str(config.app_secret),
            # 停机信号最晚在一个空闲轮询间隔之后被看见，因此这个间隔必须由停机
            # 超时推导，而不是取一个与超时无关的常数——否则配置里的超时就是一句
            # 没有实现的承诺（独立复查 F4）。取四分之一，给「处理完在途事件 + 退出」
            # 留余量：实际退出耗时 ≈ 轮询间隔 + 一条在途事件的处理时间。
            poll_seconds=max(0.1, config.shutdown_timeout_seconds / 4),
            # 单条事件从收到到落库有结果的上限。超过就让 SDK 向飞书回失败、由平台
            # 重投，而不是无限期占住它的接收协程。取停机超时本身：比它更长的话，
            # 一条卡住的事件就能让停机超出承诺。
            ack_timeout_seconds=config.shutdown_timeout_seconds,
            # 建连截止时间：超时未连上即判失败进重连，堵住「从未连上」的活性黑洞。
            handshake_timeout_seconds=config.shutdown_timeout_seconds,
        ),
        handle_event=make_event_handler(
            pipeline,
            audit=audit,
            card_callback_handler=card_callback_handler,
            group_mention_hint=group_mention_hint,
            management_card_context_store=management_card_context_store,
        ),
        backoff=BackoffPolicy(
            base_seconds=config.reconnect_base_seconds,
            factor=config.reconnect_factor,
            ceiling_seconds=config.reconnect_ceiling_seconds,
        ),
        audit=audit.record,
        heartbeat=_management_card_heartbeat,
    )
