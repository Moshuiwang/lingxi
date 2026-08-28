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
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from lingxi.adapters.postgres_local_permission import local_override_reader
from lingxi.core.alerting import AlertingDuty
from lingxi.core.identity.access_token_supply import (
    DerivedAccessTokenHolder,
    RosterAccessTokenProvider,
)
from lingxi.core.permission.mcp_readiness import ReadinessSchedule
from lingxi.core.permission.metric_translation import metric_translation_available
from lingxi.core.permission.table_access_token_supply import (
    PermissionTableAccessTokenProvider,
)
from lingxi.core.permission.tenant_token_supply import TenantAccessTokenSupply
from lingxi.apps.scheduler.audit import AuditSink, StructuredLogAuditSink
from lingxi.apps.scheduler.config import SchedulerConfig
from lingxi.apps.scheduler.credential_rotation import CredentialRotationLoop
from lingxi.apps.scheduler.daily_report import _wire_daily_report_duty
from lingxi.apps.scheduler.document_delivery_dead_letter import _wire_document_delivery_maintenance_duty
from lingxi.apps.scheduler.loop import SchedulerLoop
from lingxi.apps.scheduler.permission_publish import PermissionPublishDuty, ReadinessFollowUp
from lingxi.apps.scheduler.permission_refresh import (
    PermissionRefreshDuty,
    _build_permission_refresh_duty,
)
from lingxi.apps.scheduler.retention import (
    IDLE_CONVERSATION_SWEEP_AFTER,
    IdleConversationSweepDuty,
    PermissionRetentionSweepDuty,
    RetentionCleanupDuty,
)
from lingxi.apps.scheduler.roster_audit import (
    RosterAuditDuty,
    RosterSnapshotSyncDuty,
    _build_roster_audit_duty,
    _build_roster_snapshot_sync_duty,
    _log_snapshot_alert,
)

logger = logging.getLogger(__name__)


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

    **``onboarding_completion_notice``（V-开通-18，迁移 ``0066``）与 ``publish_outbox``
    同一面**：只需要数据库连接串，没有可选前置，因此无条件装配。
    """

    from lingxi.adapters.postgres_late_readiness_recovery import PostgresLateReadinessStore
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
        # onboarding_completion_notice（迁移 0066，V-开通-18）同样没有可选前置——
        # 只需要数据库连接串，因此这一面与 publish_outbox 那一面同样无条件装配。
        notices=PostgresLateReadinessStore(
            config.postgres_dsn, timeouts=config.postgres_timeouts
        ),
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

    **探针的 ``metrics_reader`` 显式注入为已验证的
    :func:`~lingxi.adapters.query_mcp_probe.content_text_metrics_reader`**（Issue #253）：
    2026-08-19 对真实问数 MCP 的第一次实测（``docs/参考证据/问数MCP-list_metrics真实响应形状.md``）
    发现返回里没有 ``structuredContent``，指标挂在 ``result.content[0].text`` 的一段
    JSON 字符串里；``QueryMcpProbe`` 默认的 :func:`~lingxi.adapters.query_mcp_probe.default_metrics_reader`
    只认前者，因此不注入的话就绪探针在真实 MCP 上会**永远**技术失败。
    ``default_metrics_reader`` 本身不改——保留它作为"真实形状还没实测时"的收窄兜底，
    这里只是**装配层按证据放宽**，而不是放宽默认值本身。
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
    from lingxi.adapters.query_mcp_probe import QueryMcpProbe, content_text_metrics_reader
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
            # 已验证的 reader（Issue #253 / L4a）：真实 MCP 的 list_metrics 返回没有
            # structuredContent，见本函数文档与 docs/参考证据/问数MCP-list_metrics真实响应形状.md。
            metrics_reader=content_text_metrics_reader,
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
    metric_translation_map: Mapping[str, Mapping[str, Sequence[str]]] | None,
    permission_publish: PermissionPublishDuty | None,
    stock_tokens: Any | None = None,
    onboarding_failed: Callable[[str, str], None] | None = None, legacy_source: Any | None = None,
) -> Any | None:
    """装配首次开通编排（Epic D / S-D-02）；前置不齐就**不注册**并留下**恰一条**审计。

    形状照 :func:`_build_permission_refresh_duty`。缺项时返回 ``None``，于是**没有任何人
    认领** ``auto_provisioning`` 事件——它们原样留在 ``inbound_event`` 里等配置齐了再跑。
    这比装一个失败关闭桩安全：桩会「认领即平账」，把事件永久烧掉。

    **装配不变量（外部集成面审查坐实的 F1）**：``onboarding != None ⇒ permission_publish
    != None 且 permission_publish.publish_wired``。首次开通链最终会把一条发布意图排进
    ``publish_outbox``（``AutoOnboardingRunner`` 经 ``publish_allowed`` 闸），但真正把
    那条意图从 outbox 写进外部权限表、推进就绪确认的执行者是权限发布消费职责的**发布面**
    （:func:`_build_permission_publish_duty` 里的 ``PermissionPublishExecutor``）。那一面
    如果因为**它自己的**前置不齐而没有装配，开通排出的每一条发布意图此后**没有任何职责
    会再看它一眼**——迟到就绪恢复职责（V-开通-18）救不了，它只能确认"已经进入就绪等待"
    的用户，替代不了缺失的发布执行者。不挡在这里的话，失败关闭会发生在"已经认领、已经
    建档、已经建了用户环境"之后，用户表现成"接受了开通却永远走不到可用"，还可能留下
    需要人工收拾的半开记录。

    **判据是 ``publish_wired``，不是 ``is not None``**（冻结候选审查 2026-08-21 的 F1，
    由产品负责人当天第二次真实开通失败 ``publish_not_completed`` 坐实）：
    :func:`_build_permission_publish_duty` 在缺 ``LINGXI_PERMISSION_BITABLE_APP_TOKEN``
    或 ``LINGXI_PERMISSION_BITABLE_TABLE_ID`` 而就绪面装得起来时，**照常返回一个
    ``executor=None`` 的"仅就绪"职责**（那是它自己刻意的设计：已经发布出去的权限还等着
    被确认、被通知，没有理由因为暂时写不了新的一行就把它们一起停掉）。``is not None``
    这条旧判据会把那个只剩半面的职责当成"发布执行者在"而放行开通，于是用户被认领、
    被建档、发布意图被排进 outbox，而 outbox 那一侧根本没有执行者——正是上面描述的
    半开形状。反过来，``is None`` 这一支在可达配置下几乎不成立：两面都装不起来才返回
    ``None``，而那需要连 MCP 主密钥都缺，那本来就是开通编排自己的前置。两个分支都保留，
    各留一条**可分辨**的审计原因码（``permission_publish_not_assembled`` /
    ``permission_publish_not_wired``），排障时一眼看出该去补哪一组配置。

    因此这里**在认领任何用户之前**校验调用方（``build_loop``）已经装配好的
    ``permission_publish`` 对象本身，而不是重新判断"两者前置是否恰好相同"——即使两者
    前置未来分道扬镳，这条依赖仍然成立；这也是**不能只依赖** ``PermissionPublishDuty.
    _publish()`` 内部 ``publish_allowed`` 闸的原因：那道闸在**认领之后**才会被摸到，
    这里要挡的是认领本身。

    ``employment_access_token`` 是在职状态实时回读所用的**专用授权主体派生令牌**供给。
    它就是这次搬迁的**唯一理由**：那条一次性 ``refresh_token`` 全系统只允许一个消费者，
    而它已经在本进程里（``CredentialRotationLoop.refresh_for_supply`` +
    ``DerivedAccessTokenHolder``，#215 的形状）。**这里不新建供给**，只消费传进来的那一个。

    ``metric_translation_map`` 是「公司+职能→指标名」翻译映射（Issue #227），供构造
    ``publish_allowed`` 用——**不是**新的文件读取点，而是 :func:`_build_permission_refresh_duty`
    已经加载过的**同一个对象**（``None`` 代表那一次加载没有发生或失败，与空映射
    同一个结论：不可用）。调用方（``build_loop``）负责只加载一次、原样转发。
    """

    if permission_publish is None or not permission_publish.publish_wired:
        # 恰一条审计：只报「发布执行者不在」这个结构性原因，不夹带 `permission_publish`
        # 自己那一层的原因（那一层已经在自己的装配点留过审计：`permission_publish.
        # duty_not_registered` 或 `permission_publish.publish_not_wired`）——两条审计
        # 合起来才是完整的因果链，各自只认领自己那一段。两个分支的原因码**可分辨**：
        # 「整个职责没装配」要去补 MCP 那一组配置，「只有发布面没装配」要去补权限表 Base 坐标。
        reason = (
            "permission_publish_not_assembled"
            if permission_publish is None
            else "permission_publish_not_wired"
        )
        audit.record("onboarding.duty_not_registered", reason=reason)
        logger.warning(
            "权限发布执行者不可用（%s），首次开通编排不注册（开通排出的发布意图不会有任何"
            "职责消费）；未开通用户的首聊事件原样留在库里等待配置齐备，其余定时职责照常运行",
            reason,
        )
        return None

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
    from lingxi.adapters.query_mcp_probe import QueryMcpProbe, content_text_metrics_reader
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
        # 已验证的 reader（Issue #253 / L4a），同 ``_build_readiness_follow_up`` 那一份：
        # 真实问数 MCP 的 ``list_metrics`` 返回没有 ``structuredContent``，指标挂在
        # ``result.content[0].text`` 的一段 JSON 字符串里。这里此前遗漏了这一处注入
        # （只有刷新链那一侧在 #253 修复时接上），后果是首次开通那次阻塞式确认在真实
        # MCP 上永远技术失败、每个人都会走满十五分钟同步超时——与 #253 提交说明里描述
        # 的症状（"每个用户走满 15 分钟同步超时也拿不到开通完成"）完全一致，只是那次
        # 修复没有覆盖到这个调用点。本次随 V-开通-18 的恢复路径一并补上：不补的话，
        # 恢复职责会成为整条链上**唯一**能探到真实就绪的地方，首次开通自己的那一轮
        # 阻塞确认形同虚设。
        metrics_reader=content_text_metrics_reader,
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
            # `sleep=stop.wait`（Issue #284 A 组 #4）：节流/限频退避里的等待能被
            # SIGTERM 立刻打断。这里刻意用裸传，不像组织快照那侧包
            # `_stop_aware_sleep` 中止（登记不修，独立审查二轮 P2-B1）：开通链
            # 工作线程处理的是**已认领的用户**，停机预算内完成当前链避免用户
            # 结果丢失（重启不得造成结果丢失的红线）；其单用户请求量级小（数次
            # 调用），与组织快照整轮数百次不同；置位后等待归零的暴露窗口有界。
            # `test_scheduler_onboarding_assembly.py::…uses_stop_wait_as_its_sleeper`
            # 已锁定此行为是刻意的。
            client=FeishuDirectoryClient(base_url=config.feishu_base_url, sleep=stop.wait),
            access_token=employment_access_token,
        ),
        roster=RosterRows(PostgresRosterSnapshotStore(dsn, timeouts=timeouts)),
        galaxy=PostgresGalaxySnapshotReader(dsn, timeouts=timeouts),
        provisioning=PostgresAppUserStore(dsn, timeouts=timeouts),
        users=PostgresAppUserStore(dsn, timeouts=timeouts),
        environment=environment,
        tokens=tokens,
        stock_tokens=stock_tokens,
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
        # 内测名单闸（Issue #302 S-N-01）：判据与理由见该静态方法与 innertest_roster_gate 模块文档。
        innertest_roster_gate=AutoOnboardingRunner.build_innertest_roster_gate(config.innertest_roster_open_ids),
        # 每次判定现读一次登记表（只读 `feishu_delegated_subject`，不碰凭据文件、不碰
        # refresh_token）：换主体之后旧值会让新的专用授权账号落回普通员工路径。
        delegated_subject=lambda: registered_delegated_subject_open_id(dsn, timeouts=timeouts),
        submit=executor.submit,
        sleep=stop.wait,
        clock=clock,
        should_stop=should_stop,
        publish_wait_seconds=config.onboarding_publish_wait_seconds,
        # ------------------------------------------------------------------
        # **发布闸门（Issue #227 开通侧整合）。**
        # ------------------------------------------------------------------
        # 「职能标签 → 指标名」的翻译层判据是「翻译映射整体为空时，本轮一条发布意图
        # 都不排，撤权也不例外」（见 ``permission_refresh`` 模块文档「翻译」一节，
        # 外部独立审查 2026-08-18 坐实的 P1）。本编排是 ``record_decision`` 的**第三个**
        # 调用点（前两个是每日重算的授权与撤权），因此必须自己带同一道闸，否则它就是
        # 那条判据的绕行入口，而绕过去的后果是往正式权限表写一行消费方读不懂的记录
        # ——外部表不可回滚。
        #
        # 判据实现是 :func:`~lingxi.core.permission.metric_translation.
        # metric_translation_available`——两个独立写入点共用的**唯一**一份；
        # ``metric_translation_map`` 是**同一个已加载对象**：``build_loop`` 只调用
        # :func:`_build_permission_refresh_duty` 读取一次
        # ``lingxi/config/company_function_metric_map.toml``，本函数收到的是那次读取
        # 的返回值，不在这里另读一份文件、不另做一次解析（两份来源迟早会漂移，而漂移
        # 的方向是错误发布）。**不碰 ``core``**：``AutoOnboardingRunner`` 只认一个
        # ``Callable[[], bool]``，判据属装配层。
        #
        # 变异锚点见 ``tests/test_onboarding_runner.PublishGateTests``：把这一行改回
        # ``lambda: True`` 必须让它变红；映射为空时，一个身份与权限都正常的用户走完
        # ``_match`` 后必须停在 ``permission_translation_unavailable``（用户看到
        # ``LX-ONBOARD-001``，已转交管理员，**不是**「没有银河权限」——后者会把一个
        # 权限完全正常的人引去银河申请一个他已经有的权限），**且 ``publish_outbox``
        # 零新增行、``app_user`` 零新增行**；映射非空时同一个用户照常推进到发布等待。
        publish_allowed=lambda: metric_translation_available(metric_translation_map),
        # 管理员送达（Issue #280 §7.3）：调用方（``build_loop``）没有装配告警职责时
        # 保持 ``None``——「已转交管理员处理」这句话此前就是这个默认值，行为不变；
        # 生产 main() 总会传一份真实回调（见 ``build_loop`` 调用点）。
        onboarding_failed=onboarding_failed, local_overrides=local_override_reader(dsn, timeouts=timeouts),
        legacy_source=legacy_source, publish_history=PostgresPermissionPublishStore(dsn, timeouts=timeouts),
    )
    duty = OnboardingReconciler(
        store=store,
        onboarding=runner,
        audit=audit,
        stale_after=DISPATCH_AFTER,
        # 本进程的 SchedulerLoop 已经按 `interval_seconds` 定速，认领循环不再自限——自限会把「首次开通最多等一个扫描周期」这句承诺变成「一个扫描周期或一分钟，取大的那个」。
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


def _build_late_readiness_recovery_duty(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
) -> Any:
    """装配迟到就绪恢复职责（V-开通-18）。**不需要任何可选前置就能注册。**

    补的是 ``core/identity/onboarding_runner.py`` 模块文档「一条链失败中断时」一节
    登记的缺口：首次开通那次阻塞式就绪确认判超时之后，``provisioning_state`` 停在
    ``mcp_syncing``，此前没有任何东西会再回来看这个人。语义、放在哪、节奏怎么定、
    要试到什么时候为止，见 :mod:`lingxi.apps.scheduler.late_readiness_recovery` 的
    模块文档；本函数只做装配。

    形状照 :func:`_build_readiness_follow_up`，但比它宽松一格：候选查询、原子推进
    （:class:`~lingxi.adapters.postgres_late_readiness_recovery.
    PostgresLateReadinessStore`）与通知（:class:`~lingxi.apps.scheduler.onboarding.
    CatalogNotifier`）都只需要 ``LINGXI_POSTGRES_DSN``/飞书应用凭据——两者都是
    :class:`SchedulerConfig` 的必填项，因此本职责**总能**注册。**唯一可选的是探针面**：
    缺 MCP 令牌主密钥或问数 MCP 端点时，需要真探针才能推进的那一路本轮不推进，只把这
    **恰一条**审计记下来（:class:`~lingxi.apps.scheduler.late_readiness_recovery._Ticker`
    的文档）；通知面（认领已经排出的待发通知并重试直到送达）不依赖探针，照常运行。
    """

    from lingxi.adapters.feishu_user_message import FeishuUserMessages
    from lingxi.adapters.postgres_late_readiness_recovery import PostgresLateReadinessStore
    from lingxi.adapters.postgres_permission_publish import PostgresPermissionPublishStore
    from lingxi.apps.scheduler.late_readiness_recovery import LateReadinessRecoveryDuty
    from lingxi.apps.scheduler.onboarding import CatalogNotifier
    from lingxi.config.content import default_content_catalog
    from lingxi.core.identity.onboarding_runner import FIRST_ONBOARDING_REASON

    dsn = config.postgres_dsn
    timeouts = config.postgres_timeouts
    store = PostgresLateReadinessStore(dsn, timeouts=timeouts)
    # 通知收件人查询是既有的只读方法，住在权限发布那份存取里
    # （``notice_recipient_open_id``，与刷新链的变化通知共用同一条产品口径）。
    recipients = PostgresPermissionPublishStore(dsn, timeouts=timeouts)

    ticker = None
    if not config.mcp_token_encrypt_key:
        from lingxi.adapters.mcp_token_cipher import MASTER_KEY_ENV

        # 只报变量名，不回显任何值（`V-花名册-29` 的同一条纪律；它还是一把主密钥）。
        audit.record(
            "late_readiness_recovery.probe_not_wired",
            reason="missing_environment_variable",
            variable=MASTER_KEY_ENV,
        )
        logger.warning(
            "未配置 %s，迟到就绪恢复的探针面不装配；候选留在库里等配置齐备，"
            "已经排出但还没送达的通知照常持久重试",
            MASTER_KEY_ENV,
        )
    elif not config.query_mcp_endpoint:
        audit.record(
            "late_readiness_recovery.probe_not_wired",
            reason="missing_environment_variable",
            variable="LINGXI_QUERY_MCP_ENDPOINT",
        )
        logger.warning(
            "未配置 LINGXI_QUERY_MCP_ENDPOINT，迟到就绪恢复的探针面不装配；"
            "候选留在库里等端点配好，已经排出但还没送达的通知照常持久重试"
        )
    else:
        from lingxi.adapters.mcp_token_cipher import McpTokenCipher
        from lingxi.adapters.postgres_mcp_token import (
            PostgresMcpTokenStore,
            token_cipher_provider,
        )
        from lingxi.adapters.query_mcp_probe import QueryMcpProbe, content_text_metrics_reader
        from lingxi.apps.scheduler.onboarding import assert_probe_timeouts_agree
        from lingxi.core.permission.mcp_readiness import ReadinessRecoveryTicker

        tokens = PostgresMcpTokenStore(
            dsn, cipher=McpTokenCipher(config.mcp_token_encrypt_key), timeouts=timeouts
        )
        schedule = ReadinessSchedule(probe_timeout_seconds=config.query_mcp_timeout_seconds)
        probe = QueryMcpProbe(
            endpoint=config.query_mcp_endpoint,
            token_provider=token_cipher_provider(tokens),
            timeout_seconds=config.query_mcp_timeout_seconds,
            # 已验证的 reader（Issue #253 / L4a），同 ``_build_readiness_follow_up`` 与
            # 本文件修复过的 ``_build_onboarding_duty`` 那两份。
            metrics_reader=content_text_metrics_reader,
        )
        assert_probe_timeouts_agree(probe=probe, schedule=schedule)
        ticker = ReadinessRecoveryTicker(
            probe=probe,
            store=tokens,
            audit=audit,
            clock=lambda: datetime.now(timezone.utc),
            schedule=schedule,
        )

    duty = LateReadinessRecoveryDuty(
        candidates=store,
        ticker=ticker,
        activator=store,
        notices=store,
        recipients=recipients,
        notifier=CatalogNotifier(
            sender=FeishuUserMessages(
                base_url=config.feishu_base_url,
                app_id=config.feishu_app_id,
                app_secret=config.feishu_app_secret,
            ),
            catalog=default_content_catalog(),
        ),
        audit=audit,
        reason=FIRST_ONBOARDING_REASON,
        stop=stop,
    )
    logger.info(
        "迟到就绪恢复职责已装配 探针面=%s", "已接线" if ticker is not None else "未接线"
    )
    return duty


def _build_stalled_provisioning_duty(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
    alert: Callable[[int], None] | None = None,
) -> Any:
    """装配开通中途停摆收口职责（Issue #282，`V-开通-19`）。**总能注册**，不需要任何
    可选前置——候选查询、收口写入（复用
    :meth:`~lingxi.adapters.postgres_identity.PostgresAppUserStore.
    abort_stalled_provisioning`）与通知都只需要 ``LINGXI_POSTGRES_DSN``/飞书应用
    凭据，两者都是 :class:`SchedulerConfig` 的必填项，形状照
    :func:`_build_late_readiness_recovery_duty`。

    补的是 :mod:`lingxi.core.identity.onboarding_runner` 模块文档「同一类缺口的另一半」
    一节登记的缺口：首次开通链在把用户推进到 ``provisioning`` 之后死掉、且编排自己的
    「当场收口」也够不到（进程被强杀、收口写入自己那一次恰好失败），此前没有任何东西
    会再回来看这个人。语义、放在哪、节奏怎么定见
    :mod:`lingxi.apps.scheduler.stalled_provisioning` 的模块文档；本函数只做装配。

    装配断言 5（本轮新增）：停摆租约必须严格长于一条链在 provisioning/mcp_syncing
    两格上可能停留的最长时间——见
    :func:`~lingxi.apps.scheduler.onboarding.assert_stalled_lease_exceeds_chain_budget`。
    这里只是拿一份 :class:`ReadinessSchedule` 来核对预算数字，**不需要真的装配探针**
    （本职责本身也不发探针，与迟到就绪恢复不同），因此这条断言在探针端点是否配置好之前
    就能跑。
    """

    from lingxi.adapters.feishu_user_message import FeishuUserMessages
    from lingxi.adapters.postgres_identity import PostgresAppUserStore
    from lingxi.adapters.postgres_stalled_provisioning import PostgresStalledProvisioningStore
    from lingxi.apps.scheduler.onboarding import (
        CatalogNotifier,
        assert_stalled_lease_exceeds_chain_budget,
    )
    from lingxi.apps.scheduler.stalled_provisioning import (
        DEFAULT_STALLED_LEASE_SECONDS,
        StalledProvisioningDuty,
    )
    from lingxi.config.content import default_content_catalog

    dsn = config.postgres_dsn
    timeouts = config.postgres_timeouts

    schedule = ReadinessSchedule(probe_timeout_seconds=config.query_mcp_timeout_seconds)
    assert_stalled_lease_exceeds_chain_budget(
        lease_seconds=DEFAULT_STALLED_LEASE_SECONDS,
        publish_wait_seconds=config.onboarding_publish_wait_seconds,
        schedule=schedule,
    )

    duty = StalledProvisioningDuty(
        candidates=PostgresStalledProvisioningStore(dsn, timeouts=timeouts),
        aborter=PostgresAppUserStore(dsn, timeouts=timeouts),
        notifier=CatalogNotifier(
            sender=FeishuUserMessages(
                base_url=config.feishu_base_url,
                app_id=config.feishu_app_id,
                app_secret=config.feishu_app_secret,
            ),
            catalog=default_content_catalog(),
        ),
        alert=alert,
        audit=audit,
        lease_seconds=DEFAULT_STALLED_LEASE_SECONDS,
        stop=stop,
    )
    logger.info(
        "开通中途停摆收口职责已装配 租约=%ss", DEFAULT_STALLED_LEASE_SECONDS
    )
    return duty


def _stop_aware_sleep(stop: threading.Event) -> Callable[[float], None]:
    """把 `stop.wait` 包一层，让「停机置位后的等待」变成「中止」而不是
    「假装等过了、照样放行」（二级独立审查 2026-08-21 P2）。

    直接把 `stop.wait` 当 `FeishuDirectoryClient` 的 `sleep` 注入时，`stop`
    一旦置位，`_throttle()`/限频重试的等待会立即返回、与"真的等过了"在
    调用方看来完全无法区分——节流因此悄悄失效，在途一轮剩余的数百次分页
    请求会以无节流速度打出，撞上 `_throttle` 文档字符串描述的真实频率限制。
    这里改成：`stop.wait(seconds)` 返回 `True`（已置位）时抛出
    `FeishuDirectoryError("stopping")` 中止当前调用，异常原样冒泡进
    `OrgSnapshotSyncDuty.run_once()` 既有的 `read_failed`→退避→保留上一份
    完成批次路径，符合「停止后不再发起新请求」，不是「停止后不再等待就发」。
    只挡"要不要发起下一次等待/请求"，不中断正在进行中的单次 HTTP 请求，与
    `McpReadinessConfirmation`/`PermissionNoticeDispatcher` 打断等待、不打断
    在途请求的既有纪律一致。
    """

    from lingxi.adapters.feishu_directory import FeishuDirectoryError

    def sleep(seconds: float) -> None:
        if stop.wait(seconds):
            raise FeishuDirectoryError("stopping")

    return sleep


def _build_org_snapshot_sync_duty(
    config: SchedulerConfig,
    *,
    stop: threading.Event,
    audit: AuditSink,
    user_access_token: Callable[[], str] | None,
    app_access_token: Callable[[], str] | None,
) -> Any | None:
    """装配组织快照同步职责（Issue #250）；前置不齐就**不注册**并留下**恰一条**审计。

    形状照 :func:`_build_onboarding_duty`：两个令牌供给都是调用方必须交出的前置，
    ``None`` 表示调用方真的没有交出任何供给（"未接线"）——正式装配路径
    ``build_loop`` 总会建出两条（花名册/开通那条用户身份供给自 Issue #215 起是
    默认值，权限发布表那条应用身份供给自 Issue #226 起也是默认值），因此这两条
    分支正常不会在生产触发。**"配了但拿不到令牌"不走这里**：那时职责照常注册，
    失败发生在 ``run_once`` 内部并按分类审计（``org_snapshot_sync.read_failed``），
    两者必须可分辨——把运行期的授权失败记成"未注册"会让排障去找配置，反过来会让
    "还没接线"看起来像"接线了但一直失败"（`V-花名册-29` 的同一条纪律，R3 的原始
    教训）。
    """

    if user_access_token is None:
        audit.record("org_snapshot_sync.duty_not_registered", reason="user_access_token_unwired")
        logger.warning(
            "调用方未提供组织快照用户身份读取令牌供给，组织快照同步职责不注册；"
            "其余定时职责照常运行"
        )
        return None
    if app_access_token is None:
        audit.record("org_snapshot_sync.duty_not_registered", reason="app_access_token_unwired")
        logger.warning(
            "调用方未提供组织快照应用身份读取令牌供给，组织快照同步职责不注册；"
            "其余定时职责照常运行"
        )
        return None

    from lingxi.adapters.feishu_directory import FeishuDirectoryClient
    from lingxi.adapters.feishu_org_snapshot_reader import read_org_snapshot
    from lingxi.adapters.postgres_identity import PostgresOrgSnapshotStore
    from lingxi.apps.scheduler.org_snapshot_sync import OrgSnapshotSyncDuty, TokenSupplyFailure

    # `sleep=_stop_aware_sleep(stop)`（Issue #284 A 组 #4，中止行为按二级独立审查
    # P2 修正）：不能直接注入裸的 `stop.wait`——那会让节流/限频退避在停机置位后
    # 的每一次等待都立刻返回 `True` 而**不是真的等过那么久**，`_throttle()` 因此
    # 悄悄失去节流，在途一轮剩余的数百次分页请求会以无节流速度打出，正撞上
    # `_throttle` 文档字符串要防的那种突发（真实撞过飞书累计频率限制）。这里改成
    # 「停机置位就中止」而不是「停机置位就假装等过了、照样发下一次请求」：见
    # `_stop_aware_sleep` 的文档字符串。不中断进行中的单次 HTTP 请求——中止点仍然
    # 只在两次请求之间的等待里，同 SIGTERM 只打断等待、不打断在途请求的既有纪律。
    client = FeishuDirectoryClient(base_url=config.feishu_base_url, sleep=_stop_aware_sleep(stop))

    def read_snapshot() -> Any:
        # 令牌各解析一次、用于本轮**整趟**递归遍历（数百次分页请求，Issue #250
        # 编排者 2026-08-19 实测规模），不逐次请求都重新取——两条供给都会在有效期内
        # 直接返回缓存值，这里只是不给每一次分页调用都加一次令牌新鲜度判定的开销。
        #
        # 两个供给分别包一层 `TokenSupplyFailure`（Issue #250 编排者复查 F6）：不这样
        # 做的话，应用令牌、用户令牌、真实扫描三种失败在 `run_once` 的
        # `org_snapshot_sync.read_failed` 审计里全都只剩 `error=<异常类型>`，分辨不出
        # 该去查哪一条。只重分类、不改变失败语义——原始异常仍通过 `from error` 保留
        # 因果链，只是审计只读安全的 `supply` 标签。
        try:
            app_token = app_access_token()
        except Exception as error:  # noqa: BLE001 - 立即重分类，不吞
            raise TokenSupplyFailure("app_access_token") from error
        try:
            user_token = user_access_token()
        except Exception as error:  # noqa: BLE001 - 立即重分类，不吞
            raise TokenSupplyFailure("user_access_token") from error
        # 整轮预算（Issue #284 A 组 #2；取值可运维配置，见
        # `config.org_snapshot_round_budget_seconds` 与
        # `DEFAULT_ORG_SNAPSHOT_ROUND_BUDGET_SECONDS` 的文档）：只包住这一整趟
        # 递归遍历，`round_budget` 是 `client` 这一个实例的作用域化状态，不影响
        # 开通链那个独立的 client。撞线时 `_request` 抛出的
        # `FeishuDirectoryError("round_budget_exceeded")` 原样冒泡，落进本函数
        # 上面两个 `except Exception` 同一条路径之外的、
        # `OrgSnapshotSyncDuty.run_once()` 现有的 `except Exception` 分支——走
        # 完全相同的「记 `read_failed` 审计→推进退避→保留上一份完成批次」路径，
        # 这里不需要再单独处理。
        with client.round_budget(seconds=config.org_snapshot_round_budget_seconds):
            return read_org_snapshot(client=client, app_token=app_token, user_token=user_token)

    return OrgSnapshotSyncDuty(
        read_snapshot=read_snapshot,
        store=PostgresOrgSnapshotStore(config.postgres_dsn, timeouts=config.postgres_timeouts),
        audit=audit,
        source_app_id=config.feishu_app_id,
        stop=stop,
    )


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
    （防御纵深，见 ``docs/技术设计/验收矩阵.md`` 的 ``V-权限-13``）——接通这条供给只是
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

    duties: list[Any] = [rotation, cleanup, idle_sweep, permission_retention]
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
    # 重算之前构造：重算的存量沿用源（S-P-2 #328）与发布面共用同一条供给、同一张表。
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
    # 存量权限只读源（S-P-2，Trace #328）：复用上面同一条供给与坐标，不新增环境变量；坐标缺失时保持 None，两个消费点各自按"没有存量源"降级，不阻塞各自其余前置。
    from lingxi.adapters.feishu_permission_bitable import BitablePermissionTable
    legacy_source = BitablePermissionTable(base_url=config.feishu_base_url, app_token=config.permission_app_token, table_id=config.permission_table_id, access_token=permission_table_supply) if config.permission_app_token and config.permission_table_id else None
    # 每日权限重算排在花名册审计（或与它互斥的快照写入）**之后**：同一轮里花名册快照先被换成今天的那一份，重算才可能通过它自己的新鲜度判据（`V-权限-07` 的「先花名册、再银河」）。位置只保证同一轮内的先后；"用的是今天的花名册"由职责自己的判据保证。
    #
    # ``metric_translation_map`` 是这一次调用**唯一一次**读取 ``lingxi/config/company_function_metric_map.toml`` 的结果（前置不齐、连读取都没发生时是 ``None``）；下面首次开通编排的发布闸复用**同一个对象**（Issue #227 开通侧整合），不在那里另读一份文件——两份来源迟早会漂移，而漂移的方向是错误发布，见 ``_build_permission_refresh_duty`` 与 ``_build_onboarding_duty`` 的参数文档。
    permission_refresh, metric_translation_map = _build_permission_refresh_duty(
        config, stop=stop, audit=sink, legacy_source=legacy_source
    )
    if permission_refresh is not None:
        duties.append(permission_refresh)
    # 发布消费排在每日重算**之后**：同一轮里重算先把当天的意图排进来，发布紧接着就能把
    # 它推出去，而不是白等一个调度周期。位置只保证同一轮内的先后；一条意图晚一轮被消费
    # 没有任何产品后果（outbox 本来就是异步的）。
    permission_publish = _build_permission_publish_duty(
        config,
        stop=stop,
        audit=sink,
        permission_table_access_token=permission_table_supply,
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
        legacy_source=legacy_source,
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
