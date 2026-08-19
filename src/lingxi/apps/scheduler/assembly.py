"""进程全部定时职责的装配：各职责的条件注册判定，以及 :func:`build_loop`。

从 :mod:`lingxi.apps.scheduler`（#237 拆分）搬出——这是本次拆分要解出的那一块：
九个定时职责的构造、前置判定与失败关闭语义原来全部挤在包的 ``__init__.py`` 里，
不同职责的接线改动因此总是落在同一个文件上。装配顺序的完整理由（凭据轮换在前、
花名册审计先于每日权限重算、发布消费排在重算之后……）见 :func:`build_loop` 自己的
文档字符串，未随拆分改动。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from lingxi.core.alerting import AlertingDuty
from lingxi.core.identity.access_token_supply import (
    DerivedAccessTokenHolder,
    RosterAccessTokenProvider,
)
from lingxi.core.identity.roster_snapshot import DailyRosterSource, RosterSnapshotUpdater
from lingxi.core.permission.mcp_readiness import ReadinessSchedule
from lingxi.core.permission.table_access_token_supply import (
    PermissionTableAccessTokenProvider,
)
from lingxi.core.permission.tenant_token_supply import TenantAccessTokenSupply

from lingxi.apps.scheduler.audit import AuditSink, StructuredLogAuditSink
from lingxi.apps.scheduler.config import SchedulerConfig
from lingxi.apps.scheduler.credential_rotation import CredentialRotationLoop
from lingxi.apps.scheduler.loop import SchedulerLoop
from lingxi.apps.scheduler.permission_publish import PermissionPublishDuty, ReadinessFollowUp
from lingxi.apps.scheduler.permission_refresh import PermissionRefreshDuty
from lingxi.apps.scheduler.retention import (
    IDLE_CONVERSATION_SWEEP_AFTER,
    IdleConversationSweepDuty,
    PermissionRetentionSweepDuty,
    RetentionCleanupDuty,
)
from lingxi.apps.scheduler.roster_audit import RosterAuditDuty, _log_snapshot_alert

logger = logging.getLogger(__name__)


def _build_roster_audit_duty(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
    roster_access_token: Callable[[], str] | None,
    on_send_outcome: Callable[[str, bool], None] | None = None,
) -> RosterAuditDuty | None:
    """装配审计日报职责；前置不齐就**不注册**并留下**恰一条**审计，返回 ``None``。

    四个前置按固定次序检查，缺第一个就返回：`V-花名册-29` 要求「缺群 ID → 审计
    **恰 1 条**」，逐条报会让一个什么都没配的部署一次刷出四条审计，反而看不出该先配哪个。
    """

    if not config.admin_group_chat_id:
        # 只报变量名，不回显任何值（`V-花名册-29`）。
        audit.record(
            "roster_audit.duty_not_registered",
            reason="missing_environment_variable",
            variable="LINGXI_ADMIN_GROUP_CHAT_ID",
        )
        logger.warning(
            "未配置 LINGXI_ADMIN_GROUP_CHAT_ID，花名册审计日报职责不注册；其余定时职责照常运行"
        )
        return None
    for variable, value in (
        ("LINGXI_ROSTER_BITABLE_APP_TOKEN", config.roster_app_token),
        ("LINGXI_ROSTER_BITABLE_TABLE_ID", config.roster_table_id),
    ):
        if not value:
            audit.record(
                "roster_audit.duty_not_registered",
                reason="missing_environment_variable",
                variable=variable,
            )
            logger.warning("未配置 %s，花名册审计日报职责不注册；其余定时职责照常运行", variable)
            return None
    if roster_access_token is None:
        # 调用方没有交出任何令牌供给。**这条分支现在的含义是"真的没有供给"**，
        # 不再是"这条链还没接线"：Issue #215 之后 `build_loop` 总会建出一个供给
        # （凭据轮换职责按需换、进程内持有者转交），因此正式装配路径不会走到这里。
        #
        # 「配了但拿不到令牌」不走这条分支——那时职责照常注册，失败发生在运行期并按
        # 分类审计（`roster_access_token.unavailable`）。两者必须可分辨：把运行期的
        # 授权失败记成「未注册」，会让排障去找配置；反过来则会让「还没接线」看起来
        # 像「接线了但一直失败」（R3 的原始教训）。
        audit.record("roster_audit.duty_not_registered", reason="missing_access_token_supply")
        logger.warning("调用方未提供花名册读取令牌供给，花名册审计日报职责不注册")
        return None

    from lingxi.adapters.feishu_group_message import FeishuGroupMessages
    from lingxi.adapters.feishu_roster_bitable import BitableRosterPages, read_roster_snapshot
    from lingxi.adapters.postgres_roster_audit import PostgresRosterBaselineReader
    from lingxi.adapters.postgres_roster_snapshot import PostgresRosterSnapshotStore

    pages = BitableRosterPages(
        base_url=config.feishu_base_url,
        app_token=config.roster_app_token,
        table_id=config.roster_table_id,
        access_token=roster_access_token,
    )
    store = PostgresRosterSnapshotStore(config.postgres_dsn, timeouts=config.postgres_timeouts)
    roster_source = DailyRosterSource(
        # 走 `read_roster_snapshot` 而不是逐页归一：日报必须能区分「花名册真的空了」
        # 与「这一轮没读完」，而只有前者会让保旧判定拿到 `EMPTY_SOURCE`。
        read_round=lambda: read_roster_snapshot(pages),
        updater=RosterSnapshotUpdater(store=store, audit=audit, on_alert=_log_snapshot_alert),
        load_snapshot=store.load,
        stale_after=config.roster_snapshot_stale_after,
    )

    return RosterAuditDuty(
        baseline_reader=PostgresRosterBaselineReader(
            config.postgres_dsn, timeouts=config.postgres_timeouts
        ),
        roster_source=roster_source,
        sender=FeishuGroupMessages(
            base_url=config.feishu_base_url,
            app_id=config.feishu_app_id,
            app_secret=config.feishu_app_secret,
            on_send_outcome=on_send_outcome,
        ),
        audit=audit,
        chat_id=config.admin_group_chat_id,
        stop=stop,
    )


def _build_permission_refresh_duty(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
) -> PermissionRefreshDuty | None:
    """装配每日权限重算职责；前置不齐就**不注册**并留下**恰一条**审计，返回 ``None``。

    形状照 :func:`_build_roster_audit_duty`（`V-花名册-29` 的同一条纪律：缺项只报变量名、
    审计恰一条、其余职责照常运行）。前置有三个，逐个说明为什么它是真前置：

    1. **MCP 令牌主密钥**（``LINGXI_MCP_TOKEN_ENCRYPT_KEY``）。重算要读该用户**已有**的
       令牌密文，而唯一的读取口
       :class:`~lingxi.adapters.postgres_mcp_token.PostgresMcpTokenStore` 只接受已经校验
       过主密钥的加解密对象（它同时承载解密路径，构造时就要求密钥）。**没有它就没有令牌
       读取口**，而"读不到"与"这个人没有令牌"在下游是同一个 ``None``——那会让每个需要
       新建发布行的人都以 ``missing_token_cipher`` 失败关闭，表现成"接线了但一直失败"，
       正是 R3 那条注释要避免的伪装。因此这里显式不注册并留痕。

       **本职责一次都不解密、也不签发**：密钥在这里只用于构造那个读取口
       （见 :mod:`lingxi.apps.scheduler.permission_refresh` 的模块文档）。
    2. **角色职能映射配置**。它随包发布（``lingxi/config/galaxy_role_function_map.toml``）。
       读不出来时**不能**退化成空映射——那会让所有角色变成"未映射"，于是全员被算成无可用
       权限，是一种看起来正常的失败（``role_function_map_file`` 的模块文档同一条理由）。
    3. **公司+职能→指标名翻译映射配置**（Issue #227）。它同样随包发布
       （``lingxi/config/company_function_metric_map.toml``），**文件读不出来或格式不对**
       才不注册——**空映射本身是合法内容**（``[companies]`` 表存在但没有条目，代表映射
       内容尚未由产品负责人填入），不是一种要拒绝注册的前置缺失：职责本该正常跑起来，
       只是每个人都会在翻译那一步 fail-closed 并跳过（模块文档「翻译」一节），这与"配置
       文件本身损坏"是两件不同的事，必须分开判断——前者是"内容还没到"，后者是"部署配置
       本身有问题"，把两者混在一起会让"运维发现配置文件语法错了"和"产品负责人还没填映射"
       表现成同一种"职责不注册"，无从分辨该找谁。

    数据库连接串是必需配置，进程起得来就一定有，因此它不构成一个能变红的前置判定；
    职责真正的运行前置（花名册今天更新过、银河有当前有效批次）是**数据**而不是配置，
    由 ``run_once`` 每轮重新判定。
    """

    if not config.mcp_token_encrypt_key:
        from lingxi.adapters.mcp_token_cipher import MASTER_KEY_ENV

        # 只报变量名，不回显任何值（`V-花名册-29` 的同一条纪律；它还是一把主密钥）。
        audit.record(
            "permission_refresh.duty_not_registered",
            reason="missing_environment_variable",
            variable=MASTER_KEY_ENV,
        )
        logger.warning(
            "未配置 %s，每日权限重算职责不注册；其余定时职责照常运行", MASTER_KEY_ENV
        )
        return None

    from lingxi.adapters.mcp_token_cipher import McpTokenCipher
    from lingxi.adapters.postgres_galaxy_snapshot import PostgresGalaxySnapshotReader
    from lingxi.adapters.postgres_mcp_token import PostgresMcpTokenStore
    from lingxi.adapters.postgres_permission_publish import PostgresPermissionPublishStore
    from lingxi.adapters.postgres_roster_audit import PostgresRosterBaselineReader
    from lingxi.adapters.postgres_roster_snapshot import PostgresRosterSnapshotStore
    from lingxi.adapters.company_function_metric_map_file import (
        load_company_function_metric_map,
    )
    from lingxi.adapters.role_function_map_file import load_role_function_map

    try:
        role_function_map = load_role_function_map()
    except (OSError, ValueError) as error:
        # 只记异常类型：配置解析失败的正文可能带上文件内容片段。
        audit.record(
            "permission_refresh.duty_not_registered",
            reason="role_function_map_unavailable",
            error=type(error).__name__,
        )
        logger.error(
            "角色职能映射配置不可用，每日权限重算职责不注册 error=%s", type(error).__name__
        )
        return None

    try:
        metric_translation_map = load_company_function_metric_map()
    except (OSError, ValueError) as error:
        # 同上：只记异常类型。**空映射不会走到这里**——它是合法内容，解析成功即返回；
        # 这里挡的是文件缺失或格式不对，二者都是部署配置问题，不是"内容还没填"。
        audit.record(
            "permission_refresh.duty_not_registered",
            reason="metric_translation_map_unavailable",
            error=type(error).__name__,
        )
        logger.error(
            "公司+职能→指标名翻译映射配置不可用，每日权限重算职责不注册 error=%s",
            type(error).__name__,
        )
        return None

    publish_store = PostgresPermissionPublishStore(
        config.postgres_dsn, timeouts=config.postgres_timeouts
    )
    return PermissionRefreshDuty(
        baseline_reader=PostgresRosterBaselineReader(
            config.postgres_dsn, timeouts=config.postgres_timeouts
        ),
        roster_snapshot=PostgresRosterSnapshotStore(
            config.postgres_dsn, timeouts=config.postgres_timeouts
        ),
        galaxy=PostgresGalaxySnapshotReader(config.postgres_dsn, timeouts=config.postgres_timeouts),
        decisions=publish_store,
        # 同一个存储对象喂两个端口：一个只写权限决定，一个只读"发布过没有"。分成两个
        # 参数是为了让撤权那条判据在类型上说得清楚（见 permission_refresh 的两个协议）。
        publish_history=publish_store,
        token_ciphers=PostgresMcpTokenStore(
            config.postgres_dsn,
            cipher=McpTokenCipher(config.mcp_token_encrypt_key),
            timeouts=config.postgres_timeouts,
        ),
        role_function_map=role_function_map,
        metric_translation_map=metric_translation_map,
        audit=audit,
        stop=stop,
    )


def _build_permission_retention_duty(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
) -> PermissionRetentionSweepDuty:
    """装配权限链到期清理职责。**它总是注册**，只有判定记录那一面按前置条件装配。

    **``publish_outbox`` 那一面没有任何配置前置**：擦 ``payload`` 只需要数据库连接串，
    而连接串是必需配置、进程起得来就一定有。这一面恰恰是带个人数据的那一面（邮箱与
    姓名），因此它必须无条件跑起来——给一条纯粹是"擦自己库里的内容"的路径加一个能让它
    不注册的开关，等于给保留上界加了一个可以被关掉的旁路。

    **``mcp_sync_check`` 那一面的前置是 MCP 令牌主密钥**（``LINGXI_MCP_TOKEN_ENCRYPT_KEY``）：
    唯一的读写口 :class:`~lingxi.adapters.postgres_mcp_token.PostgresMcpTokenStore` 构造时
    就要求一个**已经校验过主密钥**的加解密对象（它同时承载解密路径）。删过期行本身用不到
    密钥，但绕过那个构造约束就得给这个调用点单开一个不校验密钥的口子——那正是该类刻意
    拒绝的事（它对非 :class:`McpTokenCipher` 直接抛 ``TypeError``）。缺密钥时这一面**不装配**
    并留下**恰一条**审计，形状照 :func:`_build_readiness_follow_up` 的探针那一面（缺项只报
    变量名、不回显任何值——它还是一把主密钥），发布 outbox 那一面照常。

    这条取舍的产品后果是可接受的、也已写明：``mcp_sync_check`` **没有可识别内容列**，
    到期不删的后果是一张只含内部 ULID 与结论码的表继续变长；而真正含邮箱与姓名的
    ``publish_outbox.payload`` 一轮都不会少擦。
    """

    from lingxi.adapters.postgres_permission_publish import PostgresPermissionPublishStore

    checks: Any = None
    if config.mcp_token_encrypt_key:
        from lingxi.adapters.mcp_token_cipher import McpTokenCipher
        from lingxi.adapters.postgres_mcp_token import PostgresMcpTokenStore

        checks = PostgresMcpTokenStore(
            config.postgres_dsn,
            cipher=McpTokenCipher(config.mcp_token_encrypt_key),
            timeouts=config.postgres_timeouts,
        )
    else:
        from lingxi.adapters.mcp_token_cipher import MASTER_KEY_ENV

        # **恰一条**审计：只关掉判定记录那一面，发布 outbox 的内容擦除照常。
        audit.record(
            "permission_retention.checks_not_wired",
            reason="missing_environment_variable",
            variable=MASTER_KEY_ENV,
        )
        logger.warning(
            "未配置 %s，mcp_sync_check 的到期删除不装配；publish_outbox 的到期内容擦除照常运行",
            MASTER_KEY_ENV,
        )

    return PermissionRetentionSweepDuty(
        outbox=PostgresPermissionPublishStore(
            config.postgres_dsn, timeouts=config.postgres_timeouts
        ),
        checks=checks,
        audit=audit,
        stop=stop,
    )


def _build_readiness_follow_up(
    config: SchedulerConfig,
    *,
    audit: AuditSink,
    stop: threading.Event,
) -> ReadinessFollowUp | None:
    """装配「就绪确认 + 变化通知」这一面；前置不齐就**不装配**并留下**恰一条**审计。

    **两个前置各自只关掉自己那一块**（二级审查 N6）：

    1. **MCP 令牌主密钥**（``LINGXI_MCP_TOKEN_ENCRYPT_KEY``）——**整面的前置**。它不只是
       解密令牌用：就绪判定记录（``mcp_sync_check``）的读写口在同一个存储类上，而"这条
       变化通知过了"这个**唯一水位**就落在那张表里。没有它，连撤权通知都没有"只发一次"
       的载体，只能整面不装配。
    2. **问数 MCP 端点**（``LINGXI_QUERY_MCP_ENDPOINT``）——**只关掉探针**。撤权通知不
       依赖探针（权限文本为空的那一路本来就不发探针），因此端点没配时这一面照常装配，
       只是把 ``probe=None`` 交给 :class:`ReadinessTicker`：需要探针的那一路本轮不推进、
       不落任何记录，端点配好后从库里的进度原样继续。装一个指向空地址的假探针则相反，
       会让每条确认以技术失败耗满预算再转运维——把"还没接线"伪装成"接线了但一直失败"。

    **探针超时与就绪节奏用同一个数**：``ReadinessSchedule(probe_timeout_seconds=…)`` 与
    ``QueryMcpProbe(timeout_seconds=…)`` 都取 ``config.query_mcp_timeout_seconds``。
    两边不一致时，就绪那一侧算出来的"结论最晚什么时候落地"就是假的，因此这里在装配后
    立刻断言相等——装配层的错配不该等到生产才暴露。
    """

    if not config.mcp_token_encrypt_key:
        from lingxi.adapters.mcp_token_cipher import MASTER_KEY_ENV

        # 只报变量名，不回显任何值（`V-花名册-29` 的同一条纪律；它还是一把主密钥）。
        audit.record(
            "permission_readiness.not_wired",
            reason="missing_environment_variable",
            variable=MASTER_KEY_ENV,
        )
        logger.warning(
            "未配置 %s，MCP 就绪确认与权限变化通知不装配；权限发布照常运行", MASTER_KEY_ENV
        )
        return None

    from lingxi.adapters.feishu_user_message import FeishuUserMessages
    from lingxi.adapters.mcp_token_cipher import McpTokenCipher
    from lingxi.adapters.postgres_mcp_token import PostgresMcpTokenStore, token_cipher_provider
    from lingxi.adapters.query_mcp_probe import QueryMcpProbe
    from lingxi.core.permission.mcp_readiness import ReadinessTicker
    from lingxi.core.permission.notification import PermissionNoticeDispatcher

    tokens = PostgresMcpTokenStore(
        config.postgres_dsn,
        cipher=McpTokenCipher(config.mcp_token_encrypt_key),
        timeouts=config.postgres_timeouts,
    )
    schedule = ReadinessSchedule(probe_timeout_seconds=config.query_mcp_timeout_seconds)
    probe = None
    if config.query_mcp_endpoint:
        probe = QueryMcpProbe(
            endpoint=config.query_mcp_endpoint,
            token_provider=token_cipher_provider(tokens),
            timeout_seconds=config.query_mcp_timeout_seconds,
        )
        if probe.timeout_seconds != schedule.probe_timeout_seconds:  # pragma: no cover - 装配自证
            raise RuntimeError(
                "探针传输超时必须与就绪节奏的单次超时一致，否则收口上界是假的"
            )
    else:
        # **恰一条**审计：只关掉探针，撤权通知照常。
        audit.record(
            "permission_readiness.probe_not_wired",
            reason="missing_environment_variable",
            variable="LINGXI_QUERY_MCP_ENDPOINT",
        )
        logger.warning(
            "未配置 LINGXI_QUERY_MCP_ENDPOINT，MCP 就绪探针不装配；"
            "撤权通知与权限发布照常运行，已发布的授权待端点配好后继续确认"
        )
    return ReadinessFollowUp(
        ticker=ReadinessTicker(
            probe=probe,
            store=tokens,
            audit=audit,
            clock=lambda: datetime.now(timezone.utc),
            schedule=schedule,
        ),
        checks=tokens,
        notices=PermissionNoticeDispatcher(
            sender=FeishuUserMessages(
                base_url=config.feishu_base_url,
                app_id=config.feishu_app_id,
                app_secret=config.feishu_app_secret,
            ),
            audit=audit,
            # 退避用 `stop.wait` 而不是 `time.sleep`：SIGTERM 能立刻打断它
            # （同 `CredentialRotationLoop._save_with_retry`）。
            sleep=stop.wait,
        ),
    )


def _build_onboarding_duty(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
    employment_access_token: Callable[[], str] | None,
) -> Any | None:
    """装配首次开通编排（Epic D / S-D-02）；前置不齐就**不注册**并留下**恰一条**审计。

    形状照 :func:`_build_permission_refresh_duty`。缺项时返回 ``None``，于是**没有任何人
    认领** ``auto_provisioning`` 事件——它们原样留在 ``inbound_event`` 里等配置齐了再跑。
    这比装一个失败关闭桩安全：桩会「认领即平账」，把事件永久烧掉。

    ``employment_access_token`` 是在职状态实时回读所用的**专用授权主体派生令牌**供给。
    它就是这次搬迁的**唯一理由**：那条一次性 ``refresh_token`` 全系统只允许一个消费者，
    而它已经在本进程里（``CredentialRotationLoop.refresh_for_supply`` +
    ``DerivedAccessTokenHolder``，#215 的形状）。**这里不新建供给**，只消费传进来的那一个。
    """

    from lingxi.adapters.mcp_token_cipher import MASTER_KEY_ENV

    unwired: tuple[str, str] | None = None
    for variable, value in (
        (MASTER_KEY_ENV, config.mcp_token_encrypt_key),
        ("LINGXI_QUERY_MCP_ENDPOINT", config.query_mcp_endpoint),
        ("LINGXI_USER_ENV_ROOT", config.user_env_root),
    ):
        if not value:
            unwired = ("missing_environment_variable", variable)
            break
    if unwired is None and employment_access_token is None:
        # 在职状态是**产品合同的硬门槛**（`V-开通-07`：非在职不建档、不发权限），
        # 没有它就没有合法的开通判定，不能"先跳过这一步"。
        unwired = ("employment_access_token_unwired", "")
    if unwired is not None:
        reason, variable = unwired
        facts: dict[str, str] = {"reason": reason}
        if variable:
            facts["variable"] = variable
        audit.record("onboarding.duty_not_registered", **facts)
        logger.warning(
            "首次开通编排未装配（%s%s）；未开通用户的首聊事件原样留在库里等待配置齐备，"
            "其余定时职责照常运行",
            reason,
            f"：{variable}" if variable else "",
        )
        return None

    from lingxi.adapters.delegated_credentials import registered_delegated_subject_open_id
    from lingxi.adapters.feishu_directory import FeishuDirectoryClient, FeishuEmploymentReader
    from lingxi.adapters.feishu_user_message import FeishuUserMessages
    from lingxi.adapters.mcp_token_cipher import McpTokenCipher
    from lingxi.adapters.postgres_conversation import PostgresGatewayStore
    from lingxi.adapters.postgres_galaxy_snapshot import PostgresGalaxySnapshotReader
    from lingxi.adapters.postgres_identity import PostgresAppUserStore, PostgresOrgSnapshotStore
    from lingxi.adapters.postgres_mcp_token import PostgresMcpTokenStore, token_cipher_provider
    from lingxi.adapters.postgres_permission_publish import PostgresPermissionPublishStore
    from lingxi.adapters.postgres_roster_snapshot import PostgresRosterSnapshotStore
    from lingxi.adapters.query_mcp_probe import QueryMcpProbe
    from lingxi.adapters.role_function_map_file import load_role_function_map
    from lingxi.adapters.user_environment import LocalUserEnvironment, UserEnvironmentError
    from lingxi.apps.scheduler.onboarding import (
        DISPATCH_AFTER,
        CatalogNotifier,
        HardDeadlineProbe,
        OnboardingExecutor,
        RosterRows,
        assert_claim_limit_follows_capacity,
        assert_probe_timeouts_agree,
        monotonic_utc_clock,
        PROBE_WATCHDOG_MARGIN_SECONDS,
    )
    from lingxi.config.content import default_content_catalog
    from lingxi.core.conversation.onboarding_recovery import OnboardingReconciler
    from lingxi.core.identity.onboarding_runner import AutoOnboardingRunner
    from lingxi.core.permission.mcp_readiness import McpReadinessConfirmation

    try:
        role_function_map = load_role_function_map()
    except (OSError, ValueError) as error:
        # 只记异常类型：配置解析失败的正文可能带上文件内容片段。读不出来时**不能**退化成
        # 空映射——那会让所有角色变成"未映射"，于是每个人都被算成无可用权限。
        audit.record(
            "onboarding.duty_not_registered",
            reason="role_function_map_unavailable",
            error=type(error).__name__,
        )
        logger.error("角色职能映射配置不可用，首次开通编排不注册 error=%s", type(error).__name__)
        return None

    dsn = config.postgres_dsn
    timeouts = config.postgres_timeouts
    clock = monotonic_utc_clock()
    should_stop = stop.is_set

    tokens = PostgresMcpTokenStore(
        dsn, cipher=McpTokenCipher(config.mcp_token_encrypt_key), timeouts=timeouts
    )
    schedule = ReadinessSchedule(probe_timeout_seconds=config.query_mcp_timeout_seconds)
    probe = QueryMcpProbe(
        endpoint=config.query_mcp_endpoint,
        token_provider=token_cipher_provider(tokens),
        timeout_seconds=config.query_mcp_timeout_seconds,
    )
    assert_probe_timeouts_agree(probe=probe, schedule=schedule)
    guarded_probe = HardDeadlineProbe(
        probe=probe,
        timeout_seconds=schedule.probe_timeout_seconds + PROBE_WATCHDOG_MARGIN_SECONDS,
    )

    environment = LocalUserEnvironment(
        root=config.user_env_root, mcp_endpoint=config.query_mcp_endpoint
    )
    # **启动期全量清扫**：被 ``SIGKILL`` 留下的写入临时文件里有明文令牌，而目录内清扫只在
    # 「那个用户下一次再走开通」时发生——一个不再重试的用户意味着**没有时间上界**。在这里
    # 跑一次把上界压到「至多一个进程生命周期」。扫不动就**不注册本职责**：那意味着我们管
    # 不了这个目录，而它接下来正要接收明文凭据。
    try:
        environment.sweep_all()
    except UserEnvironmentError as error:
        audit.record(
            "onboarding.duty_not_registered",
            reason="user_environment_sweep_failed",
            error=error.code,
        )
        logger.error("用户环境启动期清扫失败，首次开通编排不注册 code=%s", error.code)
        return None

    store = PostgresGatewayStore(dsn, timeouts=timeouts)
    executor = OnboardingExecutor(workers=config.onboarding_workers, should_stop=should_stop)
    runner = AutoOnboardingRunner(
        directory=PostgresOrgSnapshotStore(dsn, timeouts=timeouts),
        employment=FeishuEmploymentReader(
            client=FeishuDirectoryClient(base_url=config.feishu_base_url),
            access_token=employment_access_token,
        ),
        roster=RosterRows(PostgresRosterSnapshotStore(dsn, timeouts=timeouts)),
        galaxy=PostgresGalaxySnapshotReader(dsn, timeouts=timeouts),
        provisioning=PostgresAppUserStore(dsn, timeouts=timeouts),
        users=PostgresAppUserStore(dsn, timeouts=timeouts),
        environment=environment,
        tokens=tokens,
        decisions=PostgresPermissionPublishStore(dsn, timeouts=timeouts),
        readiness=McpReadinessConfirmation(
            probe=guarded_probe,
            store=tokens,
            audit=audit,
            clock=clock,
            # 阻塞式确认真的会等三分钟，但它跑在开通执行器**自己的**线程上，不挡住
            # SchedulerLoop 的任何一轮 tick。用 `stop.wait` 而不是 `time.sleep`：
            # SIGTERM 能立刻打断等待（同 `PermissionNoticeDispatcher`）。
            sleep=stop.wait,
            schedule=schedule,
        ),
        notifier=CatalogNotifier(
            sender=FeishuUserMessages(
                base_url=config.feishu_base_url,
                app_id=config.feishu_app_id,
                app_secret=config.feishu_app_secret,
            ),
            catalog=default_content_catalog(),
        ),
        ledger=store,
        audit=audit,
        role_function_map=role_function_map,
        # 每次判定现读一次登记表（只读 `feishu_delegated_subject`，不碰凭据文件、不碰
        # refresh_token）：换主体之后旧值会让新的专用授权账号落回普通员工路径。
        delegated_subject=lambda: registered_delegated_subject_open_id(dsn, timeouts=timeouts),
        submit=executor.submit,
        sleep=stop.wait,
        clock=clock,
        should_stop=should_stop,
        publish_wait_seconds=config.onboarding_publish_wait_seconds,
        # ------------------------------------------------------------------
        # **发布闸门：现在是占位，恒为关闭。合并 #227 之后必须回到这一行。**
        # ------------------------------------------------------------------
        # 「职能标签 → 指标名」的翻译层（Issue #227）尚未合入本分支。在它落地之前，
        # 本编排排出去的 ``permissions`` 值列表仍然是职能标签，不是消费方能用的指标名。
        # #227 在每日重算那一侧的判据是「翻译映射整体为空时，本轮一条发布意图都不排，
        # 撤权也不例外」；本编排是 ``record_decision`` 的**第三个**调用点（前两个是
        # 每日重算的授权与撤权），因此必须自己带同一道闸，否则它就是那条判据的绕行入口，
        # 而绕过去的后果是往正式权限表写一行消费方读不懂的记录——外部表不可回滚。
        #
        # **合并 #227 之后要做的整合动作（缺一不可）**：
        #   1. 把这个 ``lambda: False`` 换成 #227 的真实判据——「翻译映射非空」那个
        #      只读检查，与每日重算那一侧**用同一个来源**（不要在这里另读一份文件或
        #      另做一次解析：两份来源迟早会漂移，而漂移的方向是错误发布）。
        #   2. **不动 ``core``**：``AutoOnboardingRunner`` 只认一个 ``Callable[[], bool]``，
        #      判据属装配层。
        #   3. 验证接对了的方法：翻译映射为空时，一个身份与权限都正常的用户走完
        #      ``_match`` 后必须停在 ``permission_translation_unavailable``（用户看到
        #      ``LX-ONBOARD-001``），**且 ``publish_outbox`` 零新增行、``app_user``
        #      零新增行**；映射非空时同一个用户照常推进到发布等待。
        #      现成的变异锚点见 ``tests/test_onboarding_runner.PublishGateTests``：把这
        #      一行改回 ``lambda: True`` 之后那一组必须变红。
        #
        # 关闭期间用户拿到的是 ``LX-ONBOARD-001``（已转交管理员），**不是**「没有银河
        # 权限」——后者会把一个权限完全正常的人引去银河申请一个他已经有的权限。
        publish_allowed=lambda: False,
    )
    duty = OnboardingReconciler(
        store=store,
        onboarding=runner,
        audit=audit,
        stale_after=DISPATCH_AFTER,
        # 本进程的 SchedulerLoop 已经按 `interval_seconds` 定速，认领循环不再自限——
        # 自限会把「首次开通最多等一个扫描周期」这句承诺变成「一个扫描周期或一分钟，
        # 取大的那个」。
        min_interval_seconds=0.0,
        should_stop=should_stop,
        capacity=executor.free_slots,
    )
    assert_claim_limit_follows_capacity(duty, executor)
    executor.start()
    logger.info(
        "首次开通编排已装配 线程数=%s 队列深度=%s 认领窗口=%s 就绪节奏=0/%s/%s",
        config.onboarding_workers,
        config.onboarding_workers * 2,
        DISPATCH_AFTER,
        schedule.interval_seconds,
        schedule.budget_seconds,
    )
    return duty


def _build_permission_publish_duty(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
    permission_table_access_token: Callable[[], str] | None,
) -> PermissionPublishDuty | None:
    """装配权限发布消费职责；**三个面各按自身依赖装配，缺谁只停谁**（二级审查 N6）。

    形状照 :func:`_build_roster_audit_duty`（`V-花名册-29` 的同一条纪律：缺项只报变量名、
    每一面**恰一条**审计、其余职责照常运行）。

    **发布面**的三个前置：

    1. **权限发布 Base ``app_token``** 与 2. **表 ``table_id``**：写哪张表不能进代码，
       只从环境变量来。
    3. **发布表读写所用的短期令牌供给**。产品负责人 2026-08-18 就 Issue
       [#226](https://github.com/Moshuiwang/lingxi/issues/226) 裁定方向 3（应用身份），
       ``build_loop`` 因此**总能**建出一条默认供给（见 ``build_loop`` 文档）——``None``
       现在的含义与花名册那条同一条（Issue #215 之后确立的口径）：**"调用方真的没有
       交出任何供给"**，不再是"这条链还没接线"，正式装配路径不会走到这一分支。
       ``permission_table_access_token_unwired`` 这条原因码因此仍然存在（供直接构造
       本函数的测试与非默认调用方使用），但生产 `main()` → `build_loop` 这条路径上不再
       触发。

    **缺发布面前置时职责仍然注册**，只要就绪/通知那一面装得起来：已经发布出去的那些
    权限还等着被确认、被通知，没有理由因为"暂时写不了新的一行"就把它们一起停掉。
    反过来也一样——MCP 端点没配时发布照常。两个面都装不起来才不注册。
    """

    executor = None
    unwired: tuple[str, str] | None = None
    for variable, value in (
        ("LINGXI_PERMISSION_BITABLE_APP_TOKEN", config.permission_app_token),
        ("LINGXI_PERMISSION_BITABLE_TABLE_ID", config.permission_table_id),
    ):
        if not value:
            unwired = ("missing_environment_variable", variable)
            break
    if unwired is None and permission_table_access_token is None:
        unwired = ("permission_table_access_token_unwired", "")

    from lingxi.adapters.postgres_permission_publish import PostgresPermissionPublishStore

    store = PostgresPermissionPublishStore(
        config.postgres_dsn, timeouts=config.postgres_timeouts
    )
    if unwired is None:
        from lingxi.adapters.feishu_permission_bitable import BitablePermissionTable
        from lingxi.core.permission.publish import PermissionPublishExecutor

        executor = PermissionPublishExecutor(
            store=store,
            transport=BitablePermissionTable(
                base_url=config.feishu_base_url,
                app_token=config.permission_app_token,
                table_id=config.permission_table_id,
                access_token=permission_table_access_token,
            ),
            audit=audit,
        )

    readiness = _build_readiness_follow_up(config, audit=audit, stop=stop)
    if executor is None and readiness is None:
        # 两面都装不起来：注册一个什么都做不了的职责只会每分钟记一条空报告。
        reason, variable = unwired or ("unknown", "")
        facts = {"reason": reason}
        if variable:
            facts["variable"] = variable
        audit.record("permission_publish.duty_not_registered", **facts)
        logger.warning("权限发布与就绪确认两面都未装配，职责不注册；其余定时职责照常运行")
        return None
    if executor is None:
        reason, variable = unwired or ("unknown", "")
        facts = {"reason": reason}
        if variable:
            facts["variable"] = variable
        # **恰一条**：只说发布面没装配，就绪与通知面照常。
        audit.record("permission_publish.publish_not_wired", **facts)
        logger.warning(
            "权限发布面未装配（%s），已发布权限的就绪确认与变化通知照常运行", reason
        )

    return PermissionPublishDuty(
        executor=executor,
        intents=store,
        audit=audit,
        readiness=readiness,
        on_alert=_permission_readiness_alert(audit),
        stop=stop,
    )


def _permission_readiness_alert(audit: AuditSink) -> Callable[[str, str], None]:
    """刷新链就绪超时的告警出口：一条**可告警的结构化事实**。

    **刻意不接 ``core/alerting.py`` 的状态机**（与 ``core/permission/publish.py`` 的
    ``on_alert`` 同一条已登记理由，也与 ``_log_snapshot_alert`` 同一姿态）：那套状态机
    只认心跳、任务滞留与飞书发送连续失败三类信号，把"某个用户的权限同步没能在十五分钟
    内确认"塞进其中任何一类，都会让那一类的阈值、去重与恢复计时同时失真——尤其是塞进
    ``FEISHU_SEND_FAILED``，会让真正的发送故障被权限超时淹没。

    它属于"权限发布失败是新增一类信号还是复用一类"这个尚未做出的决定；在那之前，这里
    先把事实**留成可 grep、可进工单的一条审计 + 一条 WARNING**，而不是让刷新链的超时
    只剩计数。用户标识是内部 ULID，不含人员资料。
    """

    def report(kind: str, user_id: str) -> None:
        audit.record("permission_readiness.alert", kind=kind, user=user_id)
        logger.warning("权限就绪确认需要人工关注 kind=%s user=%s", kind, user_id)

    return report


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
    （`V-权限-07`）。告警职责注册在**最后**（基线既有形状）：它汇总本轮观察到的信号，
    排在被观察者后面才看得到这一轮的事实。

    ``roster_access_token`` 是花名册读取所用的**短期令牌供给**（返回已就绪令牌的可调用
    对象）。**默认不再是 ``None``**：Issue #215 主接线之后，这里建出一条进程内供给——
    凭据轮换职责按需消费一次 ``refresh_token``、把派生短期令牌放进进程内持有者，日报侧
    经 :class:`~lingxi.core.identity.access_token_supply.RosterAccessTokenProvider`
    按新鲜度取用。**唯一消费者这条边界没有变**：日报自己不碰 ``refresh_token``，两个
    消费者共用一条一次性令牌正是 2026-08-08 授权码被烧那次事故的形状。

    参数保留是为了让调用方（主要是测试）**替换**供给；``None`` 现在的含义是"用装配好的
    那条"，而不是"没有供给"。进程内持有者与频率上界都建在这里，**每个进程一份**：它们是
    "这个进程今天换过没有"的账本，跨进程共享会让上界失去意义；而重启也抹不掉的那道上界
    在凭据文件里（``refresh_consumed_at``）。

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
    （防御纵深，见 ``docs/技术设计/验收矩阵.md`` 的 ``V-权限-13``）——接通这条供给只是
    让"已经在 outbox 里的意图能不能被真正写出去"这件事有了一个真实的执行者，不代表
    "outbox 里会不会出现内容"这件事发生了任何变化。
    """

    from lingxi.adapters.delegated_credentials import HostFileDelegatedCredentialVault
    from lingxi.adapters.feishu_directory import FeishuAuthorizationClient
    from lingxi.adapters.postgres_conversation import PostgresTaskQueue
    from lingxi.adapters.retention import RETENTION_CLEANUP_TIMEOUTS, PostgresRetentionCleaner

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

    duties: list[Any] = [rotation, cleanup, idle_sweep, permission_retention]
    # 令牌供给：日报侧要令牌 → 持有者里有新鲜的就直接给，没有就让**凭据轮换职责**
    # 按需换一次（每 UTC 日至多一次）。日报自己不碰一次性 refresh_token。
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
    # 每日权限重算排在花名册审计**之后**：同一轮里花名册快照先被换成今天的那一份，
    # 重算才可能通过它自己的新鲜度判据（`V-权限-07` 的「先花名册、再银河」）。
    # 位置只保证同一轮内的先后；"用的是今天的花名册"由职责自己的判据保证。
    permission_refresh = _build_permission_refresh_duty(config, stop=stop, audit=sink)
    if permission_refresh is not None:
        duties.append(permission_refresh)
    # 发布消费排在每日重算**之后**：同一轮里重算先把当天的意图排进来，发布紧接着就能把
    # 它推出去，而不是白等一个调度周期。位置只保证同一轮内的先后；一条意图晚一轮被消费
    # 没有任何产品后果（outbox 本来就是异步的）。
    #
    # 权限发布表令牌供给：Issue #226 裁定方向 3（应用身份）。换取用的 app_id/app_secret
    # 是 scheduler 本来就必需的应用配置，因此这里**总能**建出一条默认供给——不像花名册
    # 那条依赖凭据轮换职责先跑起来，应用身份没有"还没轮换过一次"这种中间状态。
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
    permission_publish = _build_permission_publish_duty(
        config,
        stop=stop,
        audit=sink,
        permission_table_access_token=permission_table_supply,
    )
    if permission_publish is not None:
        duties.append(permission_publish)
    # 首次开通编排（Epic D / S-D-02）排在发布消费**之后**：同一轮里发布面先把上一轮排出
    # 的意图推出去，开通链的发布等待才可能在同一轮内看到 ``published``。位置只保证同一轮
    # 内的先后；晚一轮没有产品后果（开通链自己会等）。
    #
    # 在职状态回读复用**同一个**令牌供给 ``supply``：它就是这次把编排搬进本进程的理由
    # ——那条一次性 refresh_token 全系统只允许一个消费者，而它已经在这里了。
    onboarding = _build_onboarding_duty(
        config, stop=stop, audit=sink, employment_access_token=supply
    )
    if onboarding is not None:
        duties.append(onboarding)
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
